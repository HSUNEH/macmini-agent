"""Local Codex app-server transport. No TUI, Orca, screen scraping, or API key.

One process hosts multiple durable threads. RPC replies, server requests and
turn events are routed separately; completion is always scoped to a turn ID.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from typing import Callable, Optional


class ServerError(Exception):
    pass


class CodexServer:
    engine = "codex"
    def __init__(self, env: Optional[dict] = None, command=None):
        self.env = dict(env or os.environ)
        self.env.pop("DISCORD_BOT_TOKEN", None)
        self.command = command or ["codex", "app-server", "--listen", "stdio://"]
        self.proc = None
        self.reader = None
        self.stderr_reader = None
        self.errors = deque(maxlen=10)
        self.ready = asyncio.Lock()
        self.writes = asyncio.Lock()
        self.seq = 0
        self.calls = {}
        self.streams = {}
        self.loaded = set()
        self.active = {}
        self.requests = {}

    async def start(self):
        async with self.ready:
            if self.proc and self.proc.returncode is None and self.reader and not self.reader.done():
                return
            self.loaded.clear()
            self.proc = await asyncio.create_subprocess_exec(
                *self.command, env=self.env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=32 * 1024 * 1024)
            self.reader = asyncio.create_task(self._read(self.proc))
            self.stderr_reader = asyncio.create_task(self._stderr(self.proc))
            try:
                await self._call("initialize", {"clientInfo": {"name": "macmini_discord",
                    "title": "Mac mini Discord agent", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True}}, 30)
                await self._write({"method": "initialized"})
            except BaseException:
                await self.close()
                raise

    async def _stderr(self, proc):
        async for line in proc.stderr:
            self.errors.append(line.decode("utf-8", "replace").strip()[-400:])

    async def _write(self, message):
        async with self.writes:
            if not self.proc or self.proc.returncode is not None:
                raise ServerError("Codex 서버가 종료됐습니다. 다음 메시지에서 다시 연결합니다.")
            try:
                self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise ServerError("Codex 서버 연결이 끊겼습니다.") from exc

    async def _call(self, method, params, timeout=60):
        self.seq += 1
        ident = f"client-{self.seq}"
        future = asyncio.get_running_loop().create_future()
        self.calls[ident] = future
        try:
            await self._write({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ServerError(f"Codex {method}: 응답 시간이 초과됐습니다. 입력을 자동 재전송하지 않습니다.") from exc
        finally:
            self.calls.pop(ident, None)

    async def call(self, method, params=None, timeout=60):
        await self.start()
        return await self._call(method, params or {}, timeout)

    async def _read(self, proc):
        try:
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                if "method" not in ev and ev.get("id") in self.calls:
                    future = self.calls[ev["id"]]
                    if not future.done():
                        if "error" in ev:
                            future.set_exception(ServerError(str(ev["error"].get("message", ev["error"]))[:1000]))
                        else:
                            future.set_result(ev.get("result", {}))
                    continue
                params = ev.get("params") or {}
                thread = params.get("threadId") or (params.get("thread") or {}).get("id")
                if "id" in ev and "method" in ev:
                    self.requests[ev["id"]] = thread
                    if thread not in self.streams:
                        await self.reject(ev["id"], "No active Discord turn for this request")
                        continue
                if ev.get("method") == "serverRequest/resolved":
                    self.requests.pop(params.get("requestId"), None)
                if thread in self.streams:
                    self.streams[thread].put_nowait(ev)
        finally:
            error = ServerError("Codex 서버 연결이 끊겼습니다. 다음 메시지에서 세션을 다시 연결합니다.")
            for future in list(self.calls.values()):
                if not future.done():
                    future.set_exception(error)
            for queue in list(self.streams.values()):
                queue.put_nowait({"transportError": str(error)})
            self.loaded.clear()
            self.requests.clear()

    async def respond(self, ident, result):
        if ident not in self.requests:
            raise ServerError("이 질문은 이미 끝났거나 취소됐습니다.")
        await self._write({"id": ident, "result": result})
        self.requests.pop(ident, None)

    async def reject(self, ident, message):
        await self._write({"id": ident, "error": {"code": -32601, "message": message}})
        self.requests.pop(ident, None)

    async def models(self):
        data, cursor = [], None
        while True:
            result = await self.call("model/list", {"limit": 100, "cursor": cursor})
            data.extend(result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return data

    async def ensure_thread(self, conv, instructions, on_session):
        await self.start()
        sid = conv.setdefault("sessions", {}).get("codex")
        if sid in self.loaded:
            return sid
        params = {"cwd": conv["workdir"], "developerInstructions": instructions,
                  "approvalPolicy": "never", "sandbox": "danger-full-access"}
        if conv.get("model"):
            params["model"] = conv["model"]
        migration = conv.get("codex_migrate")
        if migration:
            result = await self.call("thread/fork", dict(params, threadId=migration["session"],
                path=migration["path"], excludeTurns=True, deferGoalContinuation=True))
            conv.pop("codex_migrate", None)
        elif sid:
            result = await self.call("thread/resume", dict(params, threadId=sid, excludeTurns=True))
        else:
            result = await self.call("thread/start", params)
        thread = result["thread"]
        sid = thread["id"]
        conv["sessions"]["codex"] = sid
        if not conv.get("model"):
            conv["model"] = result.get("model") or thread.get("model")
        self.loaded.add(sid)
        on_session()
        return sid

    async def interrupt(self, thread):
        turn = self.active.get(thread)
        if turn:
            await self.call("turn/interrupt", {"threadId": thread, "turnId": turn})

    async def run(self, conv, prompt, instructions, on_step, on_request,
                  on_resolved, on_session: Callable, on_active: Callable, timeout=3600):
        thread = await self.ensure_thread(conv, instructions, on_session)
        if thread in self.streams:
            raise ServerError("이 Codex 세션은 이미 실행 중입니다.")
        queue = asyncio.Queue()
        self.streams[thread] = queue
        items, deltas = {}, {}
        turn = None
        try:
            params = {"threadId": thread, "input": [{"type": "text", "text": prompt}]}
            if conv.get("model"):
                params["model"] = conv["model"]
            if conv.get("effort"):
                params["effort"] = conv["effort"]
            if conv.get("mode") in ("plan", "default"):
                model = conv.get("model")
                if not model:
                    model = next(m["model"] for m in await self.models() if m.get("isDefault"))
                params["collaborationMode"] = {"mode": conv["mode"], "settings": {
                    "model": model, "reasoning_effort": conv.get("effort"), "developer_instructions": None}}
            result = await self.call("turn/start", params)
            turn = result["turn"]["id"]
            self.active[thread] = turn
            if on_active(thread) is False:
                await self.interrupt(thread)
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                ev = await asyncio.wait_for(queue.get(), max(0, remaining))
                if ev.get("transportError"):
                    raise ServerError(ev["transportError"])
                method, p = ev.get("method"), ev.get("params") or {}
                event_turn = p.get("turnId") or (p.get("turn") or {}).get("id")
                if event_turn and event_turn != turn:
                    if "id" in ev:
                        await self.reject(ev["id"], "Stale turn")
                    continue
                if "id" in ev:
                    await on_request(ev)
                elif method == "serverRequest/resolved":
                    await on_resolved(p.get("requestId"))
                elif method in ("item/agentMessage/delta", "item/plan/delta"):
                    ident = p.get("itemId", "")
                    deltas[ident] = deltas.get(ident, "") + p.get("delta", "")
                elif method in ("item/started", "item/completed"):
                    item = p.get("item") or {}
                    if method == "item/completed":
                        items[item.get("id")] = item
                    kind = item.get("type")
                    if kind == "commandExecution" and method == "item/started":
                        await on_step("$ " + item.get("command", "")[:180])
                    elif kind == "mcpToolCall" and method == "item/started":
                        await on_step(f"🔌 {item.get('server', '')}.{item.get('tool', '')}")
                    elif kind == "agentMessage" and method == "item/completed" and item.get("phase") == "commentary":
                        await on_step(item.get("text", ""))
                    elif kind == "fileChange" and method == "item/completed":
                        await on_step("✏️ " + ", ".join(c.get("path", "") for c in item.get("changes", [])))
                elif method == "turn/completed":
                    finished = p["turn"]
                    for item in finished.get("items", []):
                        items[item.get("id")] = item
                    if finished["status"] != "completed":
                        err = finished.get("error") or {}
                        raise ServerError(err.get("message") or ("중단했습니다." if finished["status"] == "interrupted" else "Codex 작업이 실패했습니다."))
                    finals = [i.get("text", "") for i in items.values()
                              if i.get("type") == "agentMessage" and i.get("phase") != "commentary"]
                    plans = [i.get("text", "") for i in items.values() if i.get("type") == "plan"]
                    reply = "\n\n".join(finals or plans).strip()
                    # Delta-only output is a compatibility fallback, never an older turn's text.
                    if not reply and not items:
                        reply = "\n\n".join(deltas.values()).strip()
                    return {"reply": reply, "session": thread, "waiting": False,
                            "plan": bool(plans) or conv.get("mode") == "plan"}
        except asyncio.TimeoutError as exc:
            await self.interrupt(thread)
            raise ServerError("Codex 작업 시간이 초과되어 중단했습니다.") from exc
        finally:
            self.active.pop(thread, None)
            self.streams.pop(thread, None)
            for ident, request_thread in list(self.requests.items()):
                if request_thread == thread:
                    self.requests.pop(ident, None)
                    await on_resolved(ident)

    async def close(self):
        proc = self.proc
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self.reader, self.stderr_reader):
            if task and task is not asyncio.current_task():
                task.cancel()
        self.proc = None
