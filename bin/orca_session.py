"""Run a Discord thread's conversation in a visible Orca terminal tab on this Mac.

Each thread gets one interactive `claude` or `codex` session in an Orca tab, opened in the thread's
folder (the main workspace or a project repo), so the same conversation can be watched and continued
in Orca. The bot reads the CLI's own transcript for progress and the reply:
  claude: ~/.claude/projects/<folder with non-alphanumerics as "-">/<session-id>.jsonl (id chosen up front)
  codex:  ~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<session-id>.jsonl (found after the first message)
Flow per message: ensure the tab is alive (reopen with resume after an Orca restart or a closed tab)
-> `orca terminal send` -> tail the transcript for progress while `orca terminal wait --for tui-idle`
blocks -> the reply is the turn's final answer. When the CLI is showing a screen instead (a choice,
plan approval, /mcp, ...), `waiting` is set and the bot posts the rendered screen with key buttons.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import engines

ORCA = "/Applications/Orca.app/Contents/Resources/bin/orca"
READY_TIMEOUT_MS = 90_000
POLL_SECONDS = 1.5
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
# Discord key buttons -> bytes typed into the tab
KEYS = {"up": "\x1b[A", "down": "\x1b[B", "enter": "\r", "esc": "\x1b", "tab": "\t", "stab": "\x1b[Z",
        "1": "1", "2": "2", "3": "3", "4": "4"}


class OrcaError(Exception):
    pass


async def orca(*args: str, timeout: float = 120) -> dict:
    """Run one `orca ... --json` command and return its result, raising OrcaError on failure."""
    proc = await asyncio.create_subprocess_exec(ORCA, *args, "--json", stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise OrcaError(f"orca {args[0]} {args[1]}: {timeout:.0f}초 안에 응답이 없습니다")
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        raise OrcaError(f"orca {args[0]} {args[1]}: {(err or out).decode('utf-8', 'replace').strip()[-300:]}")
    if not data.get("ok", True):
        e = data.get("error") or {}
        raise OrcaError(f"orca {args[0]} {args[1]}: {e.get('code', '')} {e.get('message', '')}".strip())
    return data.get("result", data)


# --- engines -----------------------------------------------------------------

def claude_transcript(workdir: str, session_id: str) -> Path:
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", workdir) / f"{session_id}.jsonl"


def claude_reply(lines: List[str]) -> str:
    """The turn's answer: assistant text after its last tool call (all text if it used no tools)."""
    blocks = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "assistant":
            continue
        for b in (ev.get("message") or {}).get("content") or []:
            if b.get("type") == "tool_use":
                blocks.append(("tool", ""))
            elif b.get("type") == "text" and b.get("text", "").strip():
                blocks.append(("text", b["text"].strip()))
    last_tool = max((i for i, (kind, _) in enumerate(blocks) if kind == "tool"), default=-1)
    return "\n\n".join(text for kind, text in blocks[last_tool + 1:] if kind == "text")


def _payload(line: str) -> dict:
    try:
        ev = json.loads(line)
    except ValueError:
        return {}
    p = ev.get("payload") if isinstance(ev, dict) and ev.get("type") == "event_msg" else None
    return p if isinstance(p, dict) else {}


def codex_progress(line: str) -> Optional[str]:
    p = _payload(line)
    if p.get("type") != "item_completed":
        return None
    item = p.get("item") or {}
    kind = item.get("type")
    if kind == "CommandExecution":
        cmd = item.get("command") or []
        return "$ " + engines._short(cmd[-1] if isinstance(cmd, list) and cmd else str(cmd))
    if kind == "FileChange":
        return "✏️ " + ", ".join(engines._short(path.split("/")[-1], 40) for path in item.get("changes") or {})
    if kind == "Extension" and item.get("kind") == "web.search":
        return f"🌐 {engines._short(item.get('query') or '')}"
    if kind == "AgentMessage" and item.get("phase") != "final_answer":
        text = " ".join(c.get("text", "") for c in item.get("content") or [])
        return f"💬 {engines._short(text)}" if text.strip() else None
    return None


def codex_reply(lines: List[str]) -> str:
    reply = ""
    for line in lines:
        p = _payload(line)
        if p.get("type") == "task_complete":
            reply = p.get("last_agent_message") or reply
    return reply


def codex_rollout(session_id: str) -> Optional[Path]:
    found = sorted(CODEX_SESSIONS.glob(f"*/*/*/rollout-*{session_id}.jsonl"))
    return found[-1] if found else None


def codex_new_rollout(workdir: str, since: float) -> Optional[Path]:
    """The rollout a just-opened codex tab started: newest one created after `since` in this folder."""
    for path in sorted(CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.stat().st_mtime < since - 5:
            break
        try:
            meta = json.loads(path.open(encoding="utf-8").readline()).get("payload") or {}
        except (OSError, ValueError):
            continue
        if meta.get("cwd") == workdir and meta.get("originator") == "codex-tui":
            return path
    return None


def codex_trust(workdir: str) -> None:
    """Codex asks "Trust this folder?" on first open; answer it ahead of time in its config."""
    text = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    if f'[projects."{workdir}"]' not in text:
        with CODEX_CONFIG.open("a", encoding="utf-8") as f:
            f.write(f'\n[projects."{workdir}"]\ntrust_level = "trusted"\n')


def tab_command(engine: str, o: dict, title: str, system_prompt: str, effort: Optional[str]) -> List[str]:
    if engine == "claude":
        cmd = ["claude", "--dangerously-skip-permissions", "--resume" if o.get("resume") else "--session-id",
               o["session"], "--name", title]
        cmd += ["--effort", effort] if effort else []
        return cmd + (["--append-system-prompt", system_prompt] if system_prompt else [])
    cmd = ["codex", "--dangerously-bypass-approvals-and-sandbox"]
    cmd += ["-c", f'model_reasoning_effort="{effort}"'] if effort else []
    cmd += ["-c", f"developer_instructions={json.dumps(system_prompt, ensure_ascii=False)}"] if system_prompt else []
    return cmd + (["resume", o["session"]] if o.get("session") else [])


# --- tabs --------------------------------------------------------------------

async def alive(handle: str, engine: str) -> bool:
    try:
        t = (await orca("terminal", "show", "--terminal", handle, timeout=30))["terminal"]
    except OrcaError:
        return False
    return bool(t.get("connected")) and not t.get("orphaned") and t.get("agentIdentity") == engine


async def close(handle: Optional[str]) -> None:
    if handle:
        try:
            await orca("terminal", "close", "--terminal", handle, timeout=30)
        except OrcaError:
            pass


async def ensure_tab(conv: dict, title: str, system_prompt: str) -> dict:
    """The thread's live tab for its current engine and folder, opening (or resuming) one if needed."""
    o: Dict = conv.setdefault("orca", {})
    engine, workdir = conv["engine"], conv["workdir"]
    if o.get("workdir") != workdir or o.get("engine", "claude") != engine:  # moved folder or switched model
        await close(o.get("handle"))
        o.clear()
    o.update(workdir=workdir, engine=engine)
    if not o.get("session") and conv.get("sessions", {}).get(engine):
        o["session"] = conv["sessions"][engine]  # resume the thread's earlier session for this engine
    if o.get("handle") and await alive(o["handle"], engine):
        return o
    o["resume"] = bool(o.get("session"))
    if engine == "claude":
        o["session"] = o.get("session") or str(uuid.uuid4())
        o["path"] = str(claude_transcript(workdir, o["session"]))
    else:
        codex_trust(workdir)
        path = codex_rollout(o["session"]) if o.get("session") else None
        o["path"] = str(path) if path else None
    o["opened"] = time.time()
    cmd = tab_command(engine, o, title, system_prompt, conv.get("effort"))
    t = (await orca("terminal", "create", "--worktree", f"path:{workdir}", "--title", title,
                    "--command", " ".join(shlex.quote(c) for c in cmd)))["terminal"]
    o["handle"] = t["handle"]
    wait = (await orca("terminal", "wait", "--terminal", t["handle"], "--for", "tui-idle",
                       "--timeout-ms", str(READY_TIMEOUT_MS), timeout=READY_TIMEOUT_MS / 1000 + 30))["wait"]
    if not wait.get("satisfied") and not wait.get("blockedReason"):  # a dialog on start shows up as a screen
        raise OrcaError(f"Orca의 {engine} 탭이 준비되지 않았습니다 ({wait.get('status')})")
    return o


async def screen(handle: str, rows: int = 30) -> str:
    """What the tab shows right now (rendered screen, trailing blank lines dropped)."""
    t = (await orca("terminal", "read", "--terminal", handle, "--screen", timeout=30))["terminal"]
    lines = [line.rstrip() for line in t.get("tail") or []]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines[-rows:])


async def waiting(handle: str) -> bool:
    """True when the CLI waits on a screen (choice, approval, menu) rather than for a new message."""
    try:
        t = (await orca("terminal", "show", "--terminal", handle, timeout=30))["terminal"]
    except OrcaError:
        return False
    return bool(t.get("agentWait"))


def mark(o: dict) -> None:
    """Remember where the transcript ends, so the next collect() reads only what comes after."""
    path = Path(o["path"]) if o.get("path") else None
    o["offset"] = path.stat().st_size if path and path.exists() else 0


async def collect(conv: dict, on_step: Callable[[str], Awaitable[None]], timeout: int) -> dict:
    """Wait for the tab to settle while streaming progress; return reply, session id and waiting."""
    o = conv["orca"]
    engine, handle = o["engine"], o["handle"]
    progress = engines.ENGINES["claude"].progress if engine == "claude" else codex_progress
    waiter = asyncio.ensure_future(orca("terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                                        "--timeout-ms", str(timeout * 1000), timeout=timeout + 60))
    lines: List[str] = []

    async def drain() -> None:
        if not o.get("path") and engine == "codex":
            found = codex_new_rollout(o["workdir"], o.get("opened", time.time()))
            if found:
                o.update(path=str(found), offset=0)
                o["session"] = found.stem[-36:]
        path = Path(o["path"]) if o.get("path") else None
        if not path or not path.exists():
            return
        with path.open("rb") as f:
            f.seek(o.get("offset", 0))
            chunk = f.read()
        complete = chunk[: chunk.rfind(b"\n") + 1]  # only whole lines; the rest comes next poll
        o["offset"] = o.get("offset", 0) + len(complete)
        for line in complete.decode("utf-8", "replace").splitlines():
            lines.append(line)
            step = progress(line)
            if step:
                await on_step(step)

    try:
        while not waiter.done():
            await drain()
            await asyncio.sleep(POLL_SECONDS)
        wait = waiter.result()["wait"]
    finally:
        waiter.cancel()
    await asyncio.sleep(0.5)
    await drain()
    if not wait.get("satisfied") and not wait.get("blockedReason"):
        raise OrcaError(f"{engine} 탭이 {timeout}초 안에 끝나지 않았습니다. Orca에서 확인해 주세요.")
    reply = claude_reply(lines) if engine == "claude" else codex_reply(lines)
    return {"reply": reply, "session": o.get("session"), "waiting": await waiting(handle)}


async def run(conv: dict, title: str, prompt: str, system_prompt: str, on_step: Callable[[str], Awaitable[None]],
              on_handle: Callable[[str], None], timeout: int = 3600) -> dict:
    """Send one message (or a raw /command) to the thread's tab and collect the outcome."""
    o = await ensure_tab(conv, title, system_prompt)
    on_handle(o["handle"])
    # the tab may still be busy (a turn typed in Orca, or one the bot lost track of across a restart):
    # text sent now would sit in the input box, so wait for it to finish first
    await orca("terminal", "wait", "--terminal", o["handle"], "--for", "tui-idle",
               "--timeout-ms", str(timeout * 1000), timeout=timeout + 60)
    mark(o)
    send = (await orca("terminal", "send", "--terminal", o["handle"], "--text", prompt, "--enter",
                       "--wait-submit", "20", timeout=60))["send"]
    if not send.get("accepted"):
        raise OrcaError("Orca 탭이 메시지를 받지 않았습니다")
    return await collect(conv, on_step, timeout)


async def press(conv: dict, key: str, on_step: Callable[[str], Awaitable[None]], timeout: int = 3600) -> dict:
    """Type one key (see KEYS) into the tab, e.g. to pick a choice, and collect what follows."""
    o = conv["orca"]
    mark(o)
    await orca("terminal", "send", "--terminal", o["handle"], "--text", KEYS[key], timeout=30)
    await asyncio.sleep(1.0)  # let a turn start (or the menu redraw) before waiting for idle
    return await collect(conv, on_step, timeout)


async def interrupt(handle: str) -> None:
    await orca("terminal", "send", "--terminal", handle, "--interrupt", timeout=30)
