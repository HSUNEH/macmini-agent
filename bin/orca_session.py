"""Run a Discord thread's Claude conversation in a visible Orca terminal tab on this Mac.

Each thread gets one interactive `claude` session in an Orca tab, opened in the thread's folder
(the main workspace or a project repo), so the same conversation can be watched and continued in
Orca. The session id is chosen up front (`--session-id`) so the bot knows which transcript to read:
  ~/.claude/projects/<folder with non-alphanumerics as "-">/<session-id>.jsonl
Flow per message: ensure the tab is alive (reopen with `--resume` after an Orca restart or a
closed tab) -> `orca terminal send` -> tail the transcript for progress while
`orca terminal wait --for tui-idle` blocks -> the reply is the turn's final assistant text.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import engines

ORCA = "/Applications/Orca.app/Contents/Resources/bin/orca"
READY_TIMEOUT_MS = 90_000
POLL_SECONDS = 1.5


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


def transcript_path(workdir: str, session_id: str) -> Path:
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", workdir) / f"{session_id}.jsonl"


async def alive(handle: str) -> bool:
    try:
        t = (await orca("terminal", "show", "--terminal", handle, timeout=30))["terminal"]
    except OrcaError:
        return False
    return bool(t.get("connected")) and not t.get("orphaned") and t.get("agentIdentity") == "claude"


async def open_tab(workdir: str, title: str, session_id: str, resume: bool, system_prompt: str,
                   effort: Optional[str] = None) -> str:
    cmd = ["claude", "--dangerously-skip-permissions", "--resume" if resume else "--session-id", session_id,
           "--name", title] + (["--effort", effort] if effort else [])
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    t = (await orca("terminal", "create", "--worktree", f"path:{workdir}", "--title", title,
                    "--command", " ".join(shlex.quote(c) for c in cmd)))["terminal"]
    wait = (await orca("terminal", "wait", "--terminal", t["handle"], "--for", "tui-idle",
                       "--timeout-ms", str(READY_TIMEOUT_MS), timeout=READY_TIMEOUT_MS / 1000 + 30))["wait"]
    if not wait.get("satisfied"):
        raise OrcaError(f"Orca의 Claude 탭이 준비되지 않았습니다 ({wait.get('blockedReason') or wait.get('status')})")
    return t["handle"]


def final_text(lines: List[str]) -> str:
    """The turn's answer: assistant text after its last tool call (all text if it used no tools)."""
    blocks: List[Tuple[str, str]] = []
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


async def run(conv: dict, title: str, prompt: str, system_prompt: str,
              on_step: Callable[[str], Awaitable[None]], on_handle: Callable[[str], None]) -> Tuple[str, str]:
    """Send one message to the thread's Orca tab and return (reply, session_id)."""
    o: Dict = conv.setdefault("orca", {})
    workdir = conv["workdir"]
    if o.get("workdir") != workdir:  # thread moved to another folder: start a fresh session there
        o.clear()
        o["workdir"] = workdir
    if not o.get("session") and conv.get("sessions", {}).get("claude"):
        o["session"] = conv["sessions"]["claude"]  # thread used headless `claude -p` before: resume that session
    session_id = o.get("session") or str(uuid.uuid4())
    handle = o.get("handle")
    if not handle or not await alive(handle):
        handle = await open_tab(workdir, title, session_id, resume=bool(o.get("session")), system_prompt=system_prompt,
                                effort=conv.get("effort"))
    o.update(handle=handle, session=session_id)
    on_handle(handle)

    path = transcript_path(workdir, session_id)
    offset = path.stat().st_size if path.exists() else 0
    send = (await orca("terminal", "send", "--terminal", handle, "--text", prompt, "--enter",
                       "--wait-submit", "20", timeout=60))["send"]
    if not send.get("accepted"):
        raise OrcaError("Orca 탭이 메시지를 받지 않았습니다")

    waiter = asyncio.ensure_future(orca("terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                                        "--timeout-ms", "3600000", timeout=3700))
    new_lines: List[str] = []
    progress = engines.ENGINES["claude"].progress

    async def drain() -> None:
        nonlocal offset
        if not path.exists():
            return
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read()
        complete = chunk[: chunk.rfind(b"\n") + 1]  # only whole lines; the rest comes next poll
        offset += len(complete)
        for line in complete.decode("utf-8", "replace").splitlines():
            new_lines.append(line)
            step = progress(line)
            if step:
                await on_step(step)

    while not waiter.done():
        await drain()
        await asyncio.sleep(POLL_SECONDS)
    wait = waiter.result()["wait"]
    await asyncio.sleep(0.5)
    await drain()
    if not wait.get("satisfied"):
        raise OrcaError(f"Claude 탭이 멈췄습니다 ({wait.get('blockedReason') or wait.get('status')}). Orca에서 확인해 주세요.")
    return final_text(new_lines), session_id


async def interrupt(handle: str) -> None:
    await orca("terminal", "send", "--terminal", handle, "--interrupt", timeout=30)
