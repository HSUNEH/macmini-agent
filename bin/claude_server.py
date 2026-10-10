"""Claude Code's bidirectional SDK transport with durable session resume.

The process is scoped to a turn and closes while idle; the session stays on
disk. Questions and approvals are sent through the same Discord-native UI as
Codex. No replacement agent loop or screen-reading layer is introduced.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import uuid

from codex_server import ServerError


class ClaudeServer:
    engine = "claude"
    def __init__(self, env=None):
        self.env = dict(env or os.environ)
        self.env.pop("DISCORD_BOT_TOKEN", None)
        self.env.pop("CLAUDECODE", None)
        self.clients = {}
        self.requests = {}
        self.futures = {}
        self.catalog = None

    def options(self, **kwargs):
        from claude_agent_sdk import ClaudeAgentOptions
        return ClaudeAgentOptions(cli_path=shutil.which("claude", path=self.env.get("PATH")),
            env=self.env, setting_sources=["user", "project", "local"],
            max_buffer_size=32 * 1024 * 1024, **kwargs)

    async def models(self):
        from claude_agent_sdk import ClaudeSDKClient
        if self.catalog is None:
            async with ClaudeSDKClient(options=self.options(cwd=os.path.expanduser("~"))) as client:
                info = await client.get_server_info()
                self.catalog = [{"model": m["value"], "displayName": m["displayName"],
                    "description": m.get("description", ""), "isDefault": m["value"] == "default",
                    "supportedReasoningEfforts": [{"reasoningEffort": e} for e in m.get("supportedEffortLevels", [])]}
                    for m in (info or {}).get("models", [])]
            if not self.catalog:
                raise ServerError("Claude가 모델 목록을 반환하지 않았습니다.")
        return self.catalog

    async def respond(self, ident, result):
        future = self.futures.get(ident)
        if ident not in self.requests or not future or future.done():
            raise ServerError("이 Claude 질문은 이미 끝났거나 취소됐습니다.")
        self.requests.pop(ident, None)
        future.set_result(result)

    async def reject(self, ident, message):
        await self.respond(ident, {"decision": "cancel", "message": message})

    async def interrupt(self, key):
        for ident, request_key in list(self.requests.items()):
            if request_key == key:
                await self.respond(ident, {"decision": "cancel"})
        if key in self.clients:
            await self.clients[key].interrupt()

    async def run(self, key, conv, prompt, instructions, on_step, on_request,
                  on_resolved, on_session, on_active, timeout=3600):
        from claude_agent_sdk import (ClaudeSDKClient, AssistantMessage, SystemMessage,
            ResultMessage, TextBlock, ToolUseBlock, PermissionResultAllow, PermissionResultDeny)
        plan_decided = False

        async def permission(tool, data, context):
            nonlocal plan_decided
            if tool not in ("AskUserQuestion", "ExitPlanMode"):  # like the old Orca tabs (--dangerously-skip-permissions)
                return PermissionResultAllow(updated_input=data)
            ident = "claude-" + uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.requests[ident], self.futures[ident] = key, future
            if tool == "AskUserQuestion":
                questions = [{**q, "id": q["question"], "isOther": True} for q in data.get("questions", [])]
                ev = {"id": ident, "method": "item/tool/requestUserInput", "params": {"questions": questions}}
            elif tool == "ExitPlanMode":
                ev = {"id": ident, "method": "claude/exitPlanMode", "params": data}
            else:
                ev = {"id": ident, "method": "item/commandExecution/requestApproval", "params": {
                    "reason": context.description or context.title or f"Claude의 `{tool}` 작업을 허용할까요?",
                    "command": data.get("command") or str(data)[:1500],
                    "availableDecisions": ["accept", "decline", "cancel"]}}
            try:
                await on_request(ev)
                result = await future
                if result.get("decision") in ("decline", "cancel"):
                    if tool == "ExitPlanMode":
                        plan_decided = True
                    return PermissionResultDeny(message=result.get("message", "사용자가 승인하지 않았습니다."),
                                                interrupt=result.get("decision") == "cancel")
                if tool == "AskUserQuestion":
                    answers = {qid: ", ".join(a.get("answers", [])) for qid, a in result.get("answers", {}).items()}
                    return PermissionResultAllow(updated_input={**data, "answers": answers})
                if tool == "ExitPlanMode":
                    plan_decided = True
                    conv["mode"] = "default"
                    on_session()
                return PermissionResultAllow(updated_input=data)
            finally:
                self.requests.pop(ident, None)
                self.futures.pop(ident, None)
                await on_resolved(ident)

        async def work():
            options = self.options(cwd=conv["workdir"], resume=conv.get("sessions", {}).get("claude"),
                fork_session=bool(conv.get("claude_migrate")), model=conv.get("model"),
                effort=conv.get("effort"), permission_mode="plan" if conv.get("mode") == "plan" else "default",
                system_prompt={"type": "preset", "preset": "claude_code", "append": instructions},
                can_use_tool=permission)
            async with ClaudeSDKClient(options=options) as client:
                self.clients[key] = client
                if on_active(key) is False:
                    raise ServerError("전환 또는 종료 요청으로 중단했습니다.")
                await client.query(prompt)
                text = []
                async for message in client.receive_response():
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        sid = message.data.get("session_id")
                        if sid:
                            conv.setdefault("sessions", {})["claude"] = sid
                            conv.pop("claude_migrate", None)
                            on_session()
                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                text.clear()  # only the answer after the last tool call is final
                                await on_step(("$ " + block.input.get("command", "")) if block.name == "Bash" else "🔧 " + block.name)
                    elif isinstance(message, ResultMessage):
                        conv.setdefault("sessions", {})["claude"] = message.session_id
                        on_session()
                        if message.is_error:
                            raise ServerError(message.result or "Claude 작업이 실패했습니다.")
                        return {"reply": message.result or "\n\n".join(text), "session": message.session_id,
                                "waiting": False, "plan": conv.get("mode") == "plan" and not plan_decided}
                raise ServerError("Claude 연결이 완료 응답 없이 종료됐습니다.")
        try:
            return await asyncio.wait_for(work(), timeout)
        except asyncio.TimeoutError as exc:
            raise ServerError("Claude 작업 시간이 초과되어 종료했습니다.") from exc
        except ServerError:
            raise
        except Exception as exc:
            raise ServerError(f"Claude 연결 오류: {str(exc)[:1000]}") from exc
        finally:
            self.clients.pop(key, None)

    async def close(self):
        # The task owning the SDK context performs the actual process teardown.
        for key in list(self.clients):
            await self.interrupt(key)
