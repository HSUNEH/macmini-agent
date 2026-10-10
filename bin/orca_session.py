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
POLL_SECONDS = 0.5  # transcript polling: reads only what was appended, so it is cheap
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
# Discord key buttons -> bytes typed into the tab
KEYS = {"up": "\x1b[A", "down": "\x1b[B", "left": "\x1b[D", "right": "\x1b[C", "enter": "\r", "esc": "\x1b",
        "space": " ", "tab": "\t", "stab": "\x1b[Z", **{str(n): str(n) for n in range(1, 10)}}


# key hints under a menu or choice ("esc to interrupt" while working is not one)
MENU_HINT = re.compile(r"(?i)esc(?: to)? (?:cancel|close|exit|go back|back|dismiss)|enter(?: to)? (?:select|confirm|submit|continue)")
MODEL_CHANGED = re.compile(r"(?i)\bmodel changed to\s+(.+?)\s*$")


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
    """Final answers of the turns in these lines (several when a message was typed in mid-turn)."""
    answers = [p.get("last_agent_message") for p in map(_payload, lines) if p.get("type") == "task_complete"]
    return "\n\n".join(a for a in answers if a)


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


async def exit_tab(handle: Optional[str]) -> None:
    """Quit the CLI with /exit (so it saves and ends the session cleanly), then close the tab."""
    if not handle:
        return
    try:
        await type_in(handle, "/exit")
        await orca("terminal", "wait", "--terminal", handle, "--for", "exit", "--timeout-ms", "5000", timeout=20)
    except OrcaError:
        pass
    await close(handle)


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
        if not o.get("path") and o.get("session"):  # a tab opened by an older version of the bot
            path = claude_transcript(workdir, o["session"]) if engine == "claude" else codex_rollout(o["session"])
            o["path"] = str(path) if path else None
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
    # The first prompt in a just-created tab is the one most likely to be
    # dropped while the TUI finishes attaching.  Send that prompt through
    # Orca's acknowledged submit path (rather than raw keys) below.
    o["fresh"] = True
    cmd = tab_command(engine, o, title, system_prompt, conv.get("effort"))
    t = (await orca("terminal", "create", "--worktree", f"path:{workdir}", "--title", title,
                    "--command", " ".join(shlex.quote(c) for c in cmd)))["terminal"]
    o["handle"] = t["handle"]
    wait = (await orca("terminal", "wait", "--terminal", t["handle"], "--for", "tui-idle",
                       "--timeout-ms", str(READY_TIMEOUT_MS), timeout=READY_TIMEOUT_MS / 1000 + 30))["wait"]
    if not wait.get("satisfied") and not wait.get("blockedReason"):  # a dialog on start shows up as a screen
        raise OrcaError(f"Orca의 {engine} 탭이 준비되지 않았습니다 ({wait.get('status')})")
    await asyncio.sleep(1.0)  # a just-started CLI can drop keys typed the moment it reports idle
    return o


_typing: Dict[str, asyncio.Lock] = {}  # one typist per tab, so concurrent messages don't interleave


async def type_in(handle: str, text: str, *, confirm_submit: bool = False) -> None:
    """Type text and press Enter as raw keys. `orca terminal send --enter` is much slower (it watches
    for the turn to start before returning, ~8s, and handles one send at a time); a short pause
    before Enter keeps codex from taking a fast paste's Enter as a newline."""
    async with _typing.setdefault(handle, asyncio.Lock()):
        await _type(handle, text, confirm_submit=confirm_submit)


async def _type(handle: str, text: str, *, confirm_submit: bool = False) -> None:
    if confirm_submit:
        # `--enter` waits until Orca has accepted the prompt.  It is slower
        # than raw keys, so reserve it for the first message in a new tab.
        sent = await orca("terminal", "send", "--terminal", handle, "--text", text,
                          "--enter", "--wait-submit", "20", timeout=60)
        if not sent.get("send", {}).get("accepted"):
            raise OrcaError("Orca 탭이 첫 메시지를 받지 않았습니다")
        return
    await orca("terminal", "send", "--terminal", handle, "--text", text, timeout=60)
    await asyncio.sleep(0.3)
    await orca("terminal", "send", "--terminal", handle, "--text", "\r", timeout=30)


async def screen(handle: str, rows: int = 30) -> str:
    """What the tab shows right now (rendered screen, trailing blank lines dropped)."""
    t = (await orca("terminal", "read", "--terminal", handle, "--screen", timeout=30))["terminal"]
    lines = [line.rstrip() for line in t.get("tail") or []]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines[-rows:])


async def waiting(handle: str) -> bool:
    """True only for a choice/approval menu, not Codex's ordinary input prompt.

    Orca marks both states as ``agentWait``.  Treating every agentWait as a dialog
    made Discord show a useless bank of arrow-key buttons below a normal
    ``Ask Codex to do anything`` prompt.
    """
    try:
        t = (await orca("terminal", "show", "--terminal", handle, timeout=30))["terminal"]
    except OrcaError:
        return False
    if not t.get("connected") or t.get("orphaned"):
        return False
    # Hints from earlier menus in scrollback must not turn an ordinary prompt
    # into a choice. Some Codex versions also omit agentWait for real menus.
    return bool(MENU_HINT.search("\n".join((await screen(handle)).splitlines()[-8:])))


def mark(o: dict) -> None:
    """Remember where the transcript ends, so the next collect() reads only what comes after."""
    path = Path(o["path"]) if o.get("path") else None
    o["offset"] = path.stat().st_size if path and path.exists() else 0
    o.pop("screen_before", None)
    o.pop("command_before", None)


async def mark_screen(o: dict) -> None:
    mark(o)
    if o["engine"] == "codex":
        rendered = await screen(o["handle"], rows=60)
        o["screen_before"] = screen_reply(rendered)
        o["command_before"] = screen_command_output(rendered)


DONE_ON_SCREEN = re.compile(r"(?i)\b(?:worked|crunched)\s+for\b")


def screen_reply(rendered: str) -> str:
    """Best-effort final answer from Codex's visible TUI when no rollout JSONL exists.

    A few Codex/Orca combinations do not create a local rollout file.  The
    completed answer is still on screen immediately before its ``Worked for``
    or ``Crunched for`` footer, so use that as a delivery fallback instead of
    falsely reporting that no answer exists.
    """
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip() for line in rendered.splitlines()]
    done = max((i for i, line in enumerate(lines) if DONE_ON_SCREEN.search(line)), default=-1)
    if done < 0:
        return ""
    start = max((i for i in range(done) if lines[i].lstrip().startswith("•")), default=-1)
    if start < 0:
        return ""
    answer = lines[start:done]
    answer[0] = re.sub(r"^\s*•\s*", "", answer[0])
    return "\n".join(answer).strip()


async def collect(conv: dict, on_step: Callable[[str], Awaitable[None]], timeout: int) -> dict:
    """Wait for the tab to settle while streaming progress; return reply, session id and waiting."""
    o = conv["orca"]
    engine, handle = o["engine"], o["handle"]
    progress = engines.ENGINES["claude"].progress if engine == "claude" else codex_progress
    lines: List[str] = []
    state = {"last": "", "queued": 0}  # last prompt/answer seen, messages waiting in the CLI's queue

    def track(line: str) -> None:
        """Follow prompts and answers so the turn counts as done only when every message the bot
        typed in (see inject) has been taken in and answered."""
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        got = None
        if engine == "claude":
            kind = ev.get("type")
            if kind == "queue-operation":
                state["queued"] += 1 if ev.get("operation") == "enqueue" else -1
                got = ev.get("content") if ev.get("operation") == "enqueue" else None
            elif kind == "user" and isinstance((ev.get("message") or {}).get("content"), str):
                got = ev["message"]["content"]
                if not got.startswith("<"):  # <command-name>/<local-command-stdout>: a /command, no answer comes
                    state["last"] = "user"
            elif kind == "assistant":
                state["last"] = "answer"
            elif kind == "system" and ev.get("subtype") == "turn_duration":
                state["last"] = "done"  # written right after the turn's final message
            elif kind == "attachment" and (ev.get("attachment") or {}).get("type") == "queued_command":
                got = ev["attachment"].get("prompt")
        else:
            p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            if ev.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                got = " ".join(c.get("text", "") for c in p.get("content") or [])
                state["last"] = "user"
            elif p.get("type") == "task_complete":
                state["last"] = "done"
        if got:
            inflight = o.get("inflight") or []
            for i, text in enumerate(inflight):
                if text.strip() and text.strip()[:60] in got:
                    del inflight[i]
                    break

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
            track(line)
            step = progress(line)
            if step:
                await on_step(step)

    stuck_since = None
    while True:
        waiter = asyncio.ensure_future(orca("terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                                            "--timeout-ms", str(timeout * 1000), timeout=timeout + 60))
        try:
            wait = None
            while not waiter.done():
                await drain()
                if await waiting(handle):
                    reply = claude_reply(lines) if engine == "claude" else codex_reply(lines)
                    return {"reply": reply, "session": o.get("session"), "waiting": True}
                # the transcript marks the end of a turn (claude turn_duration, codex task_complete), well
                # before the tab looks idle to Orca (codex ~10s later): finish on that
                if state["last"] == "done" and not o.get("inflight") and state["queued"] <= 0:
                    wait = {"satisfied": True}
                    break
                await asyncio.sleep(POLL_SECONDS)
            wait = wait or waiter.result()["wait"]
        finally:
            waiter.cancel()
        await asyncio.sleep(0.5)
        await drain()
        # Some Codex tabs have no local rollout JSONL.  Once the tab is idle,
        # the answer is nevertheless visible in its TUI; do not spend 30
        # seconds waiting for a transcript that will never arrive.
        if engine == "codex" and not o.get("path") and wait.get("satisfied"):
            visible_reply = screen_reply(await screen(handle, rows=60))
            if visible_reply and visible_reply != o.get("screen_before"):
                o["inflight"] = []
                return {"reply": visible_reply, "session": o.get("session"), "waiting": await waiting(handle)}
        if wait.get("blockedReason") or not (o.get("inflight") or state["queued"] > 0 or state["last"] == "user"):
            break
        # idle, but a message typed in mid-turn is still queued or unanswered: it starts one more turn
        stuck_since = stuck_since or time.time()
        if time.time() - stuck_since > 30 and not state["queued"] and state["last"] != "user":
            o["inflight"] = []  # never showed up in the transcript (swallowed); stop waiting for it
            break
        await asyncio.sleep(POLL_SECONDS)
    if not wait.get("satisfied") and not wait.get("blockedReason"):
        raise OrcaError(f"{engine} 탭이 {timeout}초 안에 끝나지 않았습니다. Orca에서 확인해 주세요.")
    reply = claude_reply(lines) if engine == "claude" else codex_reply(lines)
    if not reply and engine == "codex":
        rendered = await screen(handle, rows=60)
        reply = screen_reply(rendered)
        if reply == o.get("screen_before"):
            reply = ""
        if not reply:
            result = screen_command_output(rendered)
            if result != o.get("command_before"):
                reply = result
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
    # a choice or dialog on screen (e.g. how to resume a long session) blocks typed prompts:
    # report it so the bot shows the screen with key buttons instead of failing
    if await waiting(o["handle"]):
        return {"reply": "", "session": o.get("session"), "waiting": True, "blocked": True}
    await mark_screen(o)
    o["inflight"] = []  # anything left from an earlier, interrupted turn
    if not prompt.startswith("/"):  # a /command gets no answer in the transcript, so nothing to wait for
        o.setdefault("inflight", []).append(prompt)
    await type_in(o["handle"], prompt, confirm_submit=not prompt.startswith("/"))
    o.pop("fresh", None)
    if prompt.startswith("/"):
        return await settle_command(conv, timeout)
    return await collect(conv, on_step, timeout)


async def settle_command(conv: dict, timeout: int) -> dict:
    """After a /command: done as soon as a menu shows its key hints (codex menus never look idle to
    Orca) or the tab is idle again (e.g. /compact finished); after `timeout`, show what is there."""
    o, start = conv["orca"], time.time()
    while time.time() - start < timeout:
        await asyncio.sleep(1.0)
        tail = (await screen(o["handle"])).splitlines()[-6:]
        if any(MENU_HINT.search(line) for line in tail):
            return {"reply": "", "session": o.get("session"), "waiting": True}
        try:
            wait = (await orca("terminal", "wait", "--terminal", o["handle"], "--for", "tui-idle",
                               "--timeout-ms", "1000", timeout=30))["wait"]
        except OrcaError:
            continue
        if wait.get("satisfied"):
            out = ""
            for _ in range(6):  # its output reaches the transcript a moment after the tab goes idle
                out = command_output(o)
                if out:
                    break
                await asyncio.sleep(0.5)
            # Codex's /model confirmation is drawn only in the TUI, not in
            # the session transcript as a local-command result.
            if not out:
                out = screen_command_output(await screen(o["handle"]))
                if out == o.get("command_before"):
                    out = ""
            return {"reply": out, "session": o.get("session"), "waiting": await waiting(o["handle"])}
    return {"reply": "", "session": o.get("session"), "waiting": True}


def command_output(o: dict) -> str:
    """What a /command printed (claude records it as <local-command-stdout>), e.g. "Compacted"."""
    path = Path(o["path"]) if o.get("path") else None
    if not path or not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(o.get("offset", 0))
        lines = f.read().decode("utf-8", "replace").splitlines()
    outs = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        content = ev.get("content") if ev.get("type") == "system" else (ev.get("message") or {}).get("content")
        if isinstance(content, str):
            for chunk in re.findall(r"<local-command-stdout>(.*?)</local-command-stdout>", content, re.S):
                first = next((ln.strip() for ln in re.sub(r"\x1b\[[0-9;]*m", "", chunk).splitlines() if ln.strip()), "")
                if first:  # only the first line: hooks (e.g. PostCompact) append long command dumps
                    outs.append(first)
    return "\n".join(outs)[:500]


def screen_command_output(rendered: str) -> str:
    """Return concise results that Codex draws only in its TUI."""
    for line in reversed(rendered.splitlines()):
        plain = re.sub(r"\x1b\[[0-9;]*m", "", line).strip().lstrip("•").strip()
        match = MODEL_CHANGED.search(plain)
        if match:
            return f"모델을 `{match.group(1)}`로 변경했어요."
    return ""


async def inject(conv: dict, text: str) -> bool:
    """Type a message into the tab while a turn runs; the CLI takes it in mid-turn (or right after),
    and the running collect() follows until that is answered too. False while the tab shows a
    choice or dialog, where typed text would pick options."""
    o = conv["orca"]
    async with _typing.setdefault(o["handle"], asyncio.Lock()):  # taken first, so messages keep their order
        if await waiting(o["handle"]):
            return False
        o.setdefault("inflight", []).append(text)
        try:
            await _type(o["handle"], text)
        except OrcaError:
            o["inflight"].remove(text)
            raise
    return True


async def press(conv: dict, key: str, on_step: Callable[[str], Awaitable[None]], timeout: int = 3600) -> dict:
    """Type one key (see KEYS) into the tab, e.g. to pick a choice, and collect what follows."""
    o = conv["orca"]
    await mark_screen(o)
    await orca("terminal", "send", "--terminal", o["handle"], "--text", KEYS[key], timeout=30)
    await asyncio.sleep(1.0)  # let a turn start (or the menu redraw) before waiting for idle
    return await collect(conv, on_step, timeout)


async def press_keys(conv: dict, keys: List[str], on_step: Callable[[str], Awaitable[None]],
                     timeout: int = 3600) -> dict:
    """Send a short sequence of navigation keys, then collect the result.

    Used for a Discord option button: move from the highlighted row to that
    row and confirm it, rather than pretending a displayed row number is a
    keyboard shortcut.
    """
    o = conv["orca"]
    await mark_screen(o)
    text = "".join(KEYS[key] for key in keys)
    await orca("terminal", "send", "--terminal", o["handle"], "--text", text, timeout=30)
    await asyncio.sleep(1.0)
    return await collect(conv, on_step, timeout)


async def interrupt(handle: str) -> None:
    await orca("terminal", "send", "--terminal", handle, "--interrupt", timeout=30)
