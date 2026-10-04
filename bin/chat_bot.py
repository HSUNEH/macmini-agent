#!/usr/bin/env python3
"""Discord front-end for the real Claude Code / Codex CLIs on the Mac mini.

Everything lives in one channel (local/config.json "chat.channel") plus DMs:
a new message opens a thread, and each thread is one CLI session, a general assistant in ~.
When a request is development work on a project in "chat.projects", the model says so and
ends its reply with a <<project:name>> line; the bot turns that into a button that opens a
"[name] ..." thread whose session runs inside that repo, so its CLAUDE.md / AGENTS.md /
skills apply exactly as in a terminal.
Each thread keeps one session per engine plus a short log of recent turns, so switching
engines (!claude / !codex) keeps the conversation: the engine is sent the turns it missed. Progress (files read and edited, commands run) is shown live.
The scheduled jobs post into threads of the same channel; replying there starts a chat.

Commands (in any message):
  !claude / !codex [text]  switch engine, keeping the conversation; optionally ask right away
  !<project> [text]        point this thread at a project repo (new session); !home for ~
  !new                     forget the conversation: new sessions, same engine and folder
  !resume <session-id>     attach this thread to an existing session (e.g. one started in Orca)
  !stop                    stop the running task
  !kakao                   Kakao re-login; the phone code is posted in this thread
  !restart                 restart the bot after running tasks finish; it says so here when it is back
  !status / !help
Runs as a launchd KeepAlive service with ~/.macmini-agent/venv/bin/python (needs discord.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import engines  # noqa: E402
import orca_session  # noqa: E402
from discord_api import load_env, split_text  # noqa: E402

HOME = Path.home() / ".macmini-agent"
SESSIONS = HOME / "state" / "chat_sessions.json"
# Thread to tell "back online" after a restart; written by !restart (or by hand before a kickstart).
RESTART_MARK = HOME / "state" / "chat_restart.json"
FILES = HOME / "chat" / "files"
LOCAL = ROOT / "local"  # your config and prompts (not in git; start from examples/)
CONFIG = json.loads((LOCAL / "config.json").read_text(encoding="utf-8"))
CHAT = CONFIG["chat"]
PROJECTS: Dict[str, dict] = CHAT.get("projects", {})
GENERAL_PROMPT = (LOCAL / "chat" / "PROMPT.md").read_text(encoding="utf-8").strip()
# Project threads get only this note, so the session behaves like a plain terminal session.
DISCORD_NOTE = "(Discord에서 보낸 메시지입니다. 답은 Discord에 그대로 올라가니 표 대신 목록을 쓰고, 바꾼 파일 경로를 알려주세요.)"
# The model ends a reply with this line to suggest moving the work to a project thread.
HANDOFF = re.compile(r"^[ \t`]*<<project:([\w-]+)>>[ \t]*(.*?)[ \t`]*$", re.M)
CARRY_TURNS, CARRY_CHARS = 12, 1500  # recent turns kept per thread for engine switches, chars per message
STATUS_EVERY = 3.0  # seconds between status-message edits
HOME_DIR = os.path.expanduser(CHAT.get("workdir", "~"))
# "orca": Claude conversations run in visible Orca tabs (see orca_session.py); anything else: headless CLI.
USE_ORCA = CHAT.get("backend") == "orca"
HELP = ("프로젝트 개발 얘기면 그 프로젝트 스레드로 옮길지 버튼으로 물어봅니다: " + ", ".join(PROJECTS) + "\n"
        "`!<프로젝트>` 이 스레드를 그 프로젝트로 · `!home` 일반 대화로 · `!claude` / `!codex` 모델 변경\n"
        "`!new` 새 세션 · `!resume <세션ID>` Orca 등에서 하던 세션 이어받기 · `!stop` 중단 · `!status` 상태\n"
        "`!kakao` 카카오 재로그인 (코드가 이 스레드로 옴) · `!restart` 봇 재시작 (다시 켜지면 이 스레드에 알림)")


def load_sessions() -> Dict[str, dict]:
    try:
        data = json.loads(SESSIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for conv in data.values():  # older format: one "session" for the current engine
        if "session" in conv:
            sid = conv.pop("session")
            conv["sessions"] = {conv.get("engine", "codex"): sid} if sid else {}
    return data


def save_sessions() -> None:
    SESSIONS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSIONS.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SESSIONS)


def engine_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = ":".join(os.path.expanduser(p) for p in CONFIG["path"])
    env.update(engines.credentials())
    return env


def served(channel: discord.abc.Messageable) -> bool:
    base = channel.parent if isinstance(channel, discord.Thread) else channel
    return isinstance(base, discord.DMChannel) or str(getattr(base, "id", "")) == str(CHAT["channel"])


def first_prompt(conv: dict) -> str:
    """Prefix for a session's first message: tells the model which projects exist and how to hand off."""
    others = [(n, p) for n, p in PROJECTS.items() if n != conv["project"]]
    if not others:
        return DISCORD_NOTE if conv["project"] else GENERAL_PROMPT
    listing = "\n".join(f"- {n} (`{p['workdir']}`, {', '.join(p.get('aliases', []))})" for n, p in others)
    handoff = ("다른 프로젝트 전용 스레드가 따로 있습니다:\n" + listing + "\n"
               "요청이 이 중 한 프로젝트의 개발·수정 작업이면 여기서 하지 말고, 어느 프로젝트로 보이는지 한 줄로 말한 뒤 "
               "답의 맨 끝 줄에 `<<project:이름>> 그 스레드에서 이어갈 작업 요청`을 쓰세요. 요청은 지금까지의 대화 맥락과 "
               "첨부 파일 경로를 담아 그 자체로 이해되게 씁니다. 봇이 그 줄을 '스레드로 이어가기' 버튼으로 바꿉니다. "
               "단순 질문이거나 어느 프로젝트인지 애매하면 쓰지 마세요.")
    return f"{DISCORD_NOTE if conv['project'] else GENERAL_PROMPT}\n\n{handoff}"


def take_handoff(text: str, conv: dict) -> Tuple[str, Optional[Tuple[str, str]]]:
    """Strip <<project:...>> lines from a reply; return the last valid one as (project, request)."""
    picks = [(m.group(1).lower(), m.group(2).strip()) for m in HANDOFF.finditer(text)]
    picks = [(n, r) for n, r in picks if n in PROJECTS and n != conv["project"]]
    return HANDOFF.sub("", text).strip(), (picks[-1] if picks else None)


class HandoffView(discord.ui.View):
    """A button that moves the request to a project thread. Clicks are handled in on_interaction,
    keyed by custom_id, so the button keeps working after a bot restart."""

    def __init__(self, name: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label=f"{name} 새 세션으로 이어가기", style=discord.ButtonStyle.primary,
                                        custom_id=f"handoff:{name}"))


def reset_session(conv: dict) -> None:
    """Forget the conversation: no engine sessions, empty log."""
    conv.update(sessions={}, seen={}, log=[], turns=0)


async def close_tab(conv: dict) -> None:
    """Close the thread's Orca tab (if any) before the thread starts over or moves folders."""
    handle = (conv.pop("orca", None) or {}).get("handle")
    if handle:
        try:
            await orca_session.orca("terminal", "close", "--terminal", handle, timeout=30)
        except orca_session.OrcaError:
            pass


def uses_orca(conv: dict) -> bool:
    return USE_ORCA and conv.get("engine") == "claude"


def clip(text: str) -> str:
    return text if len(text) <= CARRY_CHARS else text[:CARRY_CHARS] + " …(생략)"


def carryover(conv: dict, engine: str) -> str:
    """Turns of this thread that the engine's session has not seen (they ran on another engine)."""
    seen = conv.get("seen", {}).get(engine, 0)
    missed = [t for t in conv.get("log", []) if t["n"] > seen]
    if not missed:
        return ""
    turns = "\n\n".join(f"[나] {t['user']}\n[{t['engine']}] {t['reply']}" for t in missed)
    return f"(이 스레드에서 다른 모델과 나눈 대화입니다. 이 맥락을 이어서 답하세요.)\n\n{turns}\n\n---\n\n"


def set_project(conv: dict, name: Optional[str]) -> None:
    """Point a conversation at a project repo (or back home with None); starts a new session."""
    if name:
        p = PROJECTS[name]
        conv.update(project=name, workdir=os.path.expanduser(p["workdir"]), engine=p.get("engine", "claude"))
    else:
        conv.update(project=None, workdir=HOME_DIR, engine=CHAT.get("default_engine", "codex"))
    reset_session(conv)


class Status:
    """One Discord message that shows the task's latest steps, edited at most every few seconds."""

    def __init__(self, target: discord.abc.Messageable, engine: str):
        self.target, self.engine = target, engine
        self.steps: List[str] = []
        self.msg: Optional[discord.Message] = None
        self.started = time.monotonic()
        self.last_edit = 0.0

    def render(self, head: str) -> str:
        body = "\n".join(self.steps[-10:])
        more = f"\n… 외 {len(self.steps) - 10}단계" if len(self.steps) > 10 else ""
        return f"{head}\n```\n{body[-1700:] or '…'}\n```{more}" if self.steps else head

    def elapsed(self) -> str:
        sec = int(time.monotonic() - self.started)
        return f"{sec // 60}분 {sec % 60}초" if sec >= 60 else f"{sec}초"

    async def add(self, step: str) -> None:
        self.steps.extend(step.splitlines())
        if time.monotonic() - self.last_edit >= STATUS_EVERY:
            await self.show(f"⏳ `{self.engine}` 작업 중 · {self.elapsed()}")

    async def show(self, head: str) -> None:
        self.last_edit = time.monotonic()
        text = self.render(head)
        try:
            if self.msg is None:
                self.msg = await self.target.send(text, allowed_mentions=discord.AllowedMentions.none())
            else:
                await self.msg.edit(content=text)
        except discord.HTTPException:
            pass

    async def finish(self, ok: bool) -> None:
        if self.msg is None and not self.steps:
            return
        mark = "✅" if ok else "⚠️"
        await self.show(f"{mark} `{self.engine}` {len(self.steps)}단계 · {self.elapsed()}")


async def run_engine(key: str, engine: str, session: Optional[str], workdir: str, prompt: str,
                     status: Status) -> Tuple[str, Optional[str]]:
    eng = engines.ENGINES[engine]
    proc = await asyncio.create_subprocess_exec(
        *eng.build(Path(workdir), session, True, []), cwd=workdir, env=engine_env(), start_new_session=True,
        limit=32 * 1024 * 1024,  # stream-json lines can carry whole files
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    running[key] = proc
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()
    stderr_task = asyncio.ensure_future(proc.stderr.read())
    lines: List[str] = []

    async def pump() -> None:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace")
            lines.append(line)
            step = eng.progress(line)
            if step:
                await status.add(step)
        await proc.wait()

    try:
        await asyncio.wait_for(pump(), timeout=int(CHAT.get("timeout", 3600)))
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        raise engines.EngineError(f"{CHAT.get('timeout', 3600)}초 안에 끝나지 않아 중단했습니다")
    finally:
        running.pop(key, None)
    stdout, stderr = "".join(lines), (await stderr_task).decode("utf-8", "replace")
    if proc.returncode in (-signal.SIGTERM, -signal.SIGKILL):
        raise engines.EngineError("중단했습니다 (`!stop`)")
    result = eng.parse(stdout)  # raises with the engine's own message (e.g. not logged in)
    if proc.returncode != 0:
        raise engines.EngineError(f"{engine} exit {proc.returncode}: {(stderr or stdout).strip()[-400:]}")
    return result


async def save_attachments(msg: discord.Message, key: str) -> str:
    if not msg.attachments:
        return ""
    folder = FILES / key
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for att in msg.attachments:
        path = folder / f"{msg.id}-{att.filename}"
        await att.save(path)
        paths.append(str(path))
    return "\n\n첨부 파일 (맥미니 경로):\n" + "\n".join(f"- {p}" for p in paths)


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
locks: Dict[str, asyncio.Lock] = {}
running: Dict[str, asyncio.subprocess.Process] = {}
sessions = load_sessions()
announced = False


@client.event
async def on_ready() -> None:
    """Say the bot is back: in the thread that asked for the restart, else in the alert channel."""
    global announced
    print(f"{datetime.now():%F %T} logged in as {client.user}; projects: {', '.join(PROJECTS) or '-'}", flush=True)
    if announced:  # on_ready also fires on reconnects
        return
    announced = True
    try:
        mark = json.loads(RESTART_MARK.read_text(encoding="utf-8"))
        RESTART_MARK.unlink()
    except (OSError, ValueError):
        mark = {}
    where = mark.get("thread") or CONFIG["channels"].get(CONFIG.get("alert_channel", ""), "")
    try:
        channel = client.get_channel(int(where)) or await client.fetch_channel(int(where))
        why = "요청한 재시작" if mark else "재시작(배포·재부팅·오류 등)"
        await channel.send(f"🟢 {why} 후 다시 켜졌어요 · {datetime.now():%H:%M:%S} · 프로젝트 {', '.join(PROJECTS) or '-'}")
    except (ValueError, discord.HTTPException):
        pass


@client.event
async def on_message(msg: discord.Message) -> None:
    if msg.author.bot or str(msg.author.id) not in CHAT["users"] or not served(msg.channel):
        return
    text = msg.content.strip()
    target = msg.channel
    m = re.match(r"^!(\w[\w-]*)\s*(.*)$", text, re.S)
    if not isinstance(msg.channel, (discord.Thread, discord.DMChannel)):  # new topic -> its own thread
        title = (text.splitlines()[0] if text else "대화")[:80] or "대화"
        if m and m.group(1).lower() in PROJECTS:
            title = f"[{m.group(1).lower()}] {m.group(2).strip() or '대화'}"
        try:
            target = await msg.create_thread(name=title[:95])
        except discord.HTTPException:
            pass  # no thread permission: converse in the channel itself
    key = str(target.id)
    conv = sessions.get(key)
    if conv is None:
        conv = {}
        set_project(conv, None)
    conv.setdefault("workdir", HOME_DIR)
    conv.setdefault("project", None)

    if m:
        cmd, rest = m.group(1).lower(), m.group(2).strip()
        reply = None
        if cmd in PROJECTS or cmd == "home":
            await close_tab(conv)
            set_project(conv, None if cmd == "home" else cmd)
            reply = f"`{conv['project'] or '일반'}` (`{conv['workdir']}`)에서 `{conv['engine']}`로 새 세션을 시작합니다."
        elif cmd in engines.ENGINES:
            conv["engine"] = cmd
            how = "전에 쓰던 세션에 그 사이 대화를 넘겨" if conv.get("sessions", {}).get(cmd) else "새 세션에 지금까지 대화를 넘겨"
            reply = f"이제 `{cmd}`로 이어갑니다 ({how}줍니다, 폴더 `{conv['workdir']}`)."
        elif cmd == "new":
            await close_tab(conv)
            reset_session(conv)
            reply = f"`{conv['engine']}` 새 세션으로 시작합니다 (이전 대화는 넘기지 않습니다)."
        elif cmd == "resume" and rest:
            sid, rest = rest.split()[0], ""
            conv.setdefault("sessions", {})[conv["engine"]] = sid
            if uses_orca(conv):  # the next message reopens this session in an Orca tab
                await close_tab(conv)
                conv["orca"] = {"workdir": conv["workdir"], "session": sid}
            conv.setdefault("seen", {})[conv["engine"]] = conv.get("turns", 0)
            reply = f"`{conv['engine']}` 세션 `{sid}`을 이어갑니다. 다른 모델 세션이면 먼저 `!claude`/`!codex`로 바꾸세요."
        elif cmd == "stop":
            proc = running.get(key)
            if isinstance(proc, tuple):  # ("orca", handle): interrupt the tab's turn
                await orca_session.interrupt(proc[1])
                await target.send("중단했습니다. 같은 세션에서 이어서 말하면 됩니다.")
            elif proc and proc.returncode is None:
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                await target.send("실행 중인 작업이 없습니다.")
            return
        elif cmd == "status":
            sid = conv.get("sessions", {}).get(conv["engine"])
            hint = f"\n터미널에서 이어가기: `cd {conv['workdir']} && {engines.ENGINES[conv['engine']].resume_hint(sid)}`" if sid else ""
            busy = " · 작업 중" if key in running else ""
            if uses_orca(conv) and conv.get("orca", {}).get("handle"):
                busy += " · Orca 탭에서 진행 (같은 대화를 Orca에서 바로 이어 쓸 수 있음)"
            await target.send(f"`{conv['project'] or '일반'}` · 모델 `{conv['engine']}` · 폴더 `{conv['workdir']}` · 세션 `{sid or '없음'}`{busy}{hint}")
            return
        elif cmd == "restart":
            RESTART_MARK.parent.mkdir(parents=True, exist_ok=True)
            RESTART_MARK.write_text(json.dumps({"thread": key, "at": datetime.now().isoformat(timespec="seconds")}),
                                    encoding="utf-8")
            if running:
                await target.send(f"진행 중인 작업 {len(running)}개가 끝나면 재시작합니다.")
                while running:
                    await asyncio.sleep(5)
            await target.send("재시작합니다. 다시 켜지면 여기에 알려드릴게요.")
            await client.close()  # launchd KeepAlive starts the bot again
            return
        elif cmd == "help":
            await target.send(HELP)
            return
        elif cmd == "kakao":  # phone-only Kakao re-login: the code is posted here, type it into KakaoTalk
            await target.send("카카오 재로그인을 시작합니다. 잠시 뒤 이 스레드에 코드가 옵니다.")
            env = dict(engine_env(), NOTIFY_CHANNEL=key)
            proc = await asyncio.create_subprocess_exec("bun", str(ROOT / "bin" / "kakao_relogin.mjs"), cwd=str(Path.home()),
                                                        env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            asyncio.ensure_future(proc.wait())
            return
        if reply is not None:  # a state-changing command
            sessions[key] = conv
            save_sessions()
            if not rest and not msg.attachments:
                await target.send(reply)
                return
            text = rest

    prompt = text + await save_attachments(msg, key)
    if prompt.strip():
        await ask(target, key, conv, prompt, msg)


async def ask(target: discord.abc.Messageable, key: str, conv: dict, prompt: str,
              msg: Optional[discord.Message] = None) -> None:
    """Run one prompt in the conversation's engine session and post the reply."""
    lock = locks.setdefault(key, asyncio.Lock())
    if lock.locked() and msg:
        await msg.add_reaction("⏳")  # queued behind the running task
    async with lock:
        engine = conv["engine"]
        sid = conv.setdefault("sessions", {}).get(engine)
        full = carryover(conv, engine) + prompt
        status = Status(target, engine)
        try:
            async with target.typing():
                if uses_orca(conv):
                    title = (getattr(target, "name", None) or "discord")[:60]
                    try:
                        reply_text, session = await orca_session.run(
                            conv, title, full, first_prompt(conv), status.add,
                            lambda handle: running.__setitem__(key, ("orca", handle)))
                    finally:
                        running.pop(key, None)
                else:
                    if not sid:
                        full = f"{first_prompt(conv)}\n\n---\n\n{full}"
                    reply_text, session = await run_engine(key, engine, sid, conv["workdir"], full, status)
        except (engines.EngineError, orca_session.OrcaError) as exc:
            await status.finish(False)
            hint = " 세션이 꼬였으면 `!new`로 새로 시작하세요." if sid and "!stop" not in str(exc) else ""
            await target.send(f"⚠️ {str(exc)[:1500]}{hint}")
            return
        await status.finish(True)
        reply_text, handoff = take_handoff(reply_text, conv)
        if session:
            conv["sessions"][engine] = session
        conv["turns"] = conv.get("turns", 0) + 1
        conv["log"] = (conv.get("log", []) + [{"n": conv["turns"], "engine": engine,
                                               "user": clip(prompt), "reply": clip(reply_text)}])[-CARRY_TURNS:]
        conv.setdefault("seen", {})[engine] = conv["turns"]
        if handoff:
            conv.setdefault("handoffs", {})[handoff[0]] = handoff[1]
        conv["updated_at"] = datetime.now().isoformat(timespec="seconds")
        sessions[key] = conv
        save_sessions()
        for chunk in split_text(reply_text) or ["(빈 응답)"]:
            await target.send(chunk, allowed_mentions=discord.AllowedMentions.none())
        if handoff:
            await target.send(f"🔎 `{handoff[0]}` 프로젝트로 감지됐어요! 새 세션으로 이어서 작업할까요?\n> {handoff[1][:300]}",
                              view=HandoffView(handoff[0]), allowed_mentions=discord.AllowedMentions.none())


@client.event
async def on_interaction(inter: discord.Interaction) -> None:
    """The handoff button: open a [project] thread with a fresh repo session and send it the request."""
    custom_id = (inter.data or {}).get("custom_id", "")
    if inter.type is not discord.InteractionType.component or not custom_id.startswith("handoff:"):
        return
    if str(inter.user.id) not in CHAT["users"]:
        await inter.response.send_message("이 버튼은 쓸 수 없습니다.", ephemeral=True)
        return
    name, origin = custom_id.split(":", 1)[1], inter.channel
    request = sessions.get(str(origin.id), {}).get("handoffs", {}).pop(name, None)
    if name not in PROJECTS or not request:
        await inter.response.send_message("이미 처리했거나 만료된 버튼입니다.", ephemeral=True)
        return
    await inter.response.edit_message(view=None)
    parent = origin.parent if isinstance(origin, discord.Thread) else origin
    target = origin
    if isinstance(parent, discord.TextChannel):
        try:
            target = await parent.create_thread(name=f"[{name}] {request.splitlines()[0]}"[:95],
                                                type=discord.ChannelType.public_thread)
            await origin.send(f"➡️ {target.mention}에서 이어갑니다.")
        except discord.HTTPException:
            pass  # no thread permission: switch this conversation in place
    key = str(target.id)
    conv = sessions.get(key) if target is origin else None
    conv = conv or {}
    set_project(conv, name)
    sessions[key] = conv
    save_sessions()
    await ask(target, key, conv, request)


def main() -> int:
    token = load_env().get("DISCORD_BOT_TOKEN", "")
    if not token:
        print("DISCORD_BOT_TOKEN missing in ~/.macmini-agent/.env", file=sys.stderr)
        return 1
    client.run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
