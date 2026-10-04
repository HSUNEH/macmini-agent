"""LLM CLIs that macmini_agent can drive, all on subscriptions (no API keys).

Each engine knows how to start a session, resume one, describe a progress step from one
line of its streamed stdout, and read the final reply + session id. To add a model, write
those three functions and register them in ENGINES.
Used by run_job.py (scheduled jobs) and chat_bot.py (Discord conversations).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

ENV_FILE = Path.home() / ".macmini-agent" / ".env"
# Engine credentials forwarded from ~/.macmini-agent/.env. Discord tokens are never passed to an engine.
CREDENTIAL_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN",)


class EngineError(Exception):
    pass


class Engine(NamedTuple):
    # build(cwd, session_id or None, web_search, extra_dirs, effort or None) -> argv; the prompt goes to stdin
    build: Callable[..., List[str]]
    # parse(full stdout) -> (reply text, session id)
    parse: Callable[[str], Tuple[str, Optional[str]]]
    # progress(one stdout line) -> short human-readable step, or None
    progress: Callable[[str], Optional[str]]
    # how to continue this session in a terminal (e.g. in Orca)
    resume_hint: Callable[[str], str]


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json(line: str) -> Optional[dict]:
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


# --- codex -------------------------------------------------------------------

def _codex_build(cwd: Path, session: Optional[str], search: bool, dirs: Sequence[Path],
                 effort: Optional[str] = None) -> List[str]:
    cmd = ["codex"] + (["--search"] if search else []) + (["-c", f'model_reasoning_effort="{effort}"'] if effort else [])
    cmd += ["--dangerously-bypass-approvals-and-sandbox", "exec"]
    if session:
        return cmd + ["resume", "--skip-git-repo-check", "--json", session, "-"]
    return cmd + ["--skip-git-repo-check", "--json", "-C", str(cwd), "-"]


def _codex_parse(out: str) -> Tuple[str, Optional[str]]:
    session, text, error = None, "", ""
    for line in out.splitlines():
        ev = _json(line) or {}
        kind = ev.get("type")
        if kind == "thread.started":
            session = ev.get("thread_id")
        elif kind == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            text = ev["item"].get("text") or text  # the last agent message is the reply
        elif kind in ("error", "turn.failed"):
            error = json.dumps(ev.get("error") or ev.get("message") or ev, ensure_ascii=False)[:500]
    if error and not text:
        raise EngineError(f"codex: {error}")
    return text, session


def _codex_progress(line: str) -> Optional[str]:
    ev = _json(line) or {}
    item = ev.get("item") or {}
    kind = item.get("type")
    if ev.get("type") == "item.started" and kind == "command_execution":
        cmd = str(item.get("command") or "")
        if cmd.startswith("/bin/zsh -lc ") or cmd.startswith("/bin/bash -lc "):
            cmd = cmd.split(" ", 2)[2].strip("'\"")
        return f"$ {_short(cmd)}"
    if ev.get("type") != "item.completed":
        return None
    if kind == "file_change":
        return "✏️ " + ", ".join(_short(c.get("path", "").split("/")[-1], 40) for c in item.get("changes") or [])
    if kind == "web_search":
        return f"🌐 {_short(item.get('query') or '')}"
    if kind == "mcp_tool_call":
        return f"🔌 {item.get('server', '')}.{item.get('tool', '')}"
    if kind == "agent_message":
        return f"💬 {_short(item.get('text') or '')}"
    return None


# --- claude ------------------------------------------------------------------

def _claude_build(cwd: Path, session: Optional[str], search: bool, dirs: Sequence[Path],
                  effort: Optional[str] = None) -> List[str]:
    # WebSearch/WebFetch are built in, so `search` needs no flag.
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "bypassPermissions"]
    if effort:
        cmd += ["--effort", effort]
    for d in dirs:
        cmd += ["--add-dir", str(d)]
    return cmd + (["--resume", session] if session else [])


def _claude_parse(out: str) -> Tuple[str, Optional[str]]:
    result = None
    for line in out.splitlines():
        ev = _json(line)
        if ev and ev.get("type") == "result":
            result = ev
    if result is None:
        raise EngineError(f"claude: no result in output: {out.strip()[-300:]}")
    if result.get("is_error"):
        raise EngineError(f"claude: {result.get('result') or result.get('subtype')}")
    return str(result.get("result") or ""), result.get("session_id")


_CLAUDE_TOOL_ICON = {"Read": "📖", "Edit": "✏️", "MultiEdit": "✏️", "Write": "✏️", "NotebookEdit": "✏️",
                     "Grep": "🔎", "Glob": "🔎", "WebSearch": "🌐", "WebFetch": "🌐", "Task": "🤖", "Agent": "🤖"}


def _claude_progress(line: str) -> Optional[str]:
    ev = _json(line) or {}
    if ev.get("type") != "assistant":
        return None
    steps = []
    for block in (ev.get("message") or {}).get("content") or []:
        if block.get("type") == "text" and block.get("text", "").strip():
            steps.append(f"💬 {_short(block['text'])}")
        elif block.get("type") == "tool_use":
            name, inp = block.get("name", ""), block.get("input") or {}
            if name == "Bash":
                steps.append(f"$ {_short(inp.get('command', ''))}")
            elif name == "TodoWrite":
                steps.append("📝 할 일 목록 갱신")
            else:
                arg = inp.get("file_path") or inp.get("pattern") or inp.get("query") or inp.get("url") \
                    or inp.get("description") or ""
                arg = str(arg).split("/")[-1] if "file_path" in inp else arg
                steps.append(f"{_CLAUDE_TOOL_ICON.get(name, '🔧')} {name} {_short(arg, 60)}".rstrip())
    return "\n".join(steps) or None


ENGINES: Dict[str, Engine] = {
    "codex": Engine(_codex_build, _codex_parse, _codex_progress, lambda s: f"codex resume {s}"),
    "claude": Engine(_claude_build, _claude_parse, _claude_progress, lambda s: f"claude --resume {s}"),
}


def credentials() -> Dict[str, str]:
    found: Dict[str, str] = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, sep, val = raw.strip().partition("=")
            if sep and key.strip() in CREDENTIAL_KEYS and val.strip():
                found[key.strip()] = val.strip().strip('"').strip("'")
    return found
