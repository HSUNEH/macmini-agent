"""Discord-native controls for Codex RPC requests and local model/plan settings."""
from __future__ import annotations

import json
import uuid
import discord

from codex_server import ServerError


class ChoiceView(discord.ui.View):
    def __init__(self, token, options, freeform=False, multi=False):
        super().__init__(timeout=None)
        if len(options) > 10 or multi:
            self.add_item(discord.ui.Select(custom_id=f"native:{token}:select", row=0,
                placeholder="여러 항목을 선택하세요" if multi else "항목을 선택하세요",
                max_values=min(25, len(options)) if multi else 1, options=[discord.SelectOption(
                    label=o["label"][:100], description=(o.get("description") or "")[:100] or None,
                    value=str(i)) for i, o in enumerate(options[:25])]))
            row = 1
        else:
            for i, option in enumerate(options):
                self.add_item(discord.ui.Button(label=option["label"][:80],
                    custom_id=f"native:{token}:{i}", row=i // 5, style=discord.ButtonStyle.primary))
            row = 2 if len(options) > 5 else (1 if options else 0)
        if freeform:
            self.add_item(discord.ui.Button(label="직접 입력", custom_id=f"native:{token}:text", row=row))


class AnswerModal(discord.ui.Modal):
    def __init__(self, owner, token, title):
        super().__init__(title=title[:45] or "답변 입력", timeout=600)
        self.owner, self.token = owner, token
        self.answer = discord.ui.TextInput(label="답변", style=discord.TextStyle.paragraph, max_length=2000)
        self.add_item(self.answer)

    async def on_submit(self, inter):
        await self.owner.answer(inter, self.token, str(self.answer))


class NativeUI:
    def __init__(self, server, save, submit, authorized):
        self.server, self.save, self.submit, self.authorized = server, save, submit, authorized
        self.entries = {}
        self.current = {}

    async def post(self, target, title, question, options, callback, *, freeform=False,
                   request=None, group=None, multi=False):
        group = group or (f"rpc:{request}" if request is not None else "settings")
        key = (str(target.id), group)
        old = self.current.pop(key, None)
        if old:
            await self.invalidate(old)
        token = uuid.uuid4().hex[:12]
        entry = {"target": target, "callback": callback, "options": options,
                 "request": request, "title": title, "group": group, "message": None,
                 "freeform": freeform, "multi": multi}
        self.entries[token] = entry
        self.current[key] = token
        embed = discord.Embed(title=title[:256], description=question[:4000], colour=discord.Colour.blurple())
        embed.set_footer(text="선택 뒤 추가 질문이 나오면 다시 묻습니다.")
        # No raw terminal screenshots or duplicate choice lists.
        try:
            entry["message"] = await target.send(embed=embed, view=ChoiceView(token, options, freeform, multi),
                                                allowed_mentions=discord.AllowedMentions.none())
        except BaseException:
            self.entries.pop(token, None)
            if self.current.get(key) == token:
                self.current.pop(key, None)
            raise
        return token

    async def invalidate(self, token):
        entry = self.entries.pop(token, None)
        if not entry:
            return
        key = (str(entry["target"].id), entry["group"])
        if self.current.get(key) == token:
            self.current.pop(key, None)
        if entry.get("message"):
            try:
                await entry["message"].edit(view=None)
            except discord.HTTPException:
                pass

    async def resolved(self, ident):
        for token, entry in list(self.entries.items()):
            if entry.get("request") == ident:
                await self.invalidate(token)

    async def invalidate_channel(self, key):
        for token, entry in list(self.entries.items()):
            if str(entry["target"].id) == key:
                await self.invalidate(token)

    async def invalidate_group(self, key, group):
        for token, entry in list(self.entries.items()):
            if str(entry["target"].id) == key and entry["group"] == group:
                await self.invalidate(token)

    async def answer(self, inter, token, value, index=False):
        if str(inter.user.id) not in self.authorized:
            await inter.response.send_message("이 버튼은 쓸 수 없습니다.", ephemeral=True)
            return
        entry = self.entries.get(token)
        if not entry or str(entry["target"].id) != str(inter.channel.id):
            await inter.response.send_message("지난 선택 화면입니다. `!model` 또는 `!screen`으로 다시 열어주세요.", ephemeral=True)
            return
        if entry["request"] is not None and entry["request"] not in self.server.requests:
            await inter.response.send_message("이 질문은 이미 끝났거나 취소됐습니다.", ephemeral=True)
            await self.invalidate(token)
            return
        if index:
            try:
                if isinstance(value, list):
                    value = [entry["options"][int(i)]["value"] for i in value]
                    if not entry["multi"] and len(value) != 1:
                        raise ValueError()
                    if not entry["multi"]:
                        value = value[0]
                else:
                    value = entry["options"][int(value)]["value"]
            except (IndexError, ValueError):
                await inter.response.send_message("선택값을 읽지 못했습니다.", ephemeral=True)
                return
        elif not entry["freeform"]:
            await inter.response.send_message("이 질문은 버튼으로 선택해주세요.", ephemeral=True)
            return
        # Consume before awaiting: a double click cannot answer twice.
        self.entries.pop(token, None)
        await inter.response.defer()
        await self.invalidate_message(entry)
        try:
            await entry["callback"](value)
        except ServerError as exc:
            await inter.followup.send(str(exc), ephemeral=True)

    async def invalidate_message(self, entry):
        key = (str(entry["target"].id), entry["group"])
        self.current.pop(key, None)
        if entry.get("message"):
            try:
                await entry["message"].edit(view=None)
            except discord.HTTPException:
                pass

    async def interaction(self, inter):
        custom = (inter.data or {}).get("custom_id", "")
        if not custom.startswith("native:"):
            return False
        _, token, action = custom.split(":", 2)
        if action == "text":
            entry = self.entries.get(token)
            if str(inter.user.id) not in self.authorized or not entry or not entry["freeform"]:
                await inter.response.send_message("사용할 수 없거나 만료된 입력입니다.", ephemeral=True)
            else:
                await inter.response.send_modal(AnswerModal(self, token, entry["title"]))
        else:
            value = (inter.data or {}).get("values") or [] if action == "select" else action
            await self.answer(inter, token, value, index=True)
        return True

    async def text_answer(self, target, text):
        eligible = [(token, e) for token, e in self.entries.items()
                    if str(e["target"].id) == str(target.id) and e["freeform"]
                    and e["request"] is not None and e["request"] in self.server.requests]
        if len(eligible) != 1:
            return False
        token, entry = eligible[0]
        self.entries.pop(token, None)
        await self.invalidate_message(entry)
        await entry["callback"](text)
        return True

    async def show_models(self, target, conv, chosen=None):
        models = await self.server.models()
        if chosen:
            models = [m for m in models if m["model"].casefold() == chosen.casefold()
                      or m["displayName"].replace(" ", "-").casefold() == chosen.casefold()]
            if not models:
                raise ServerError("현재 계정의 모델 목록에 없는 모델입니다. `!model`로 다시 선택해주세요.")
        async def choose(model_id):
            model = next(m for m in models if m["model"] == model_id)
            efforts = model.get("supportedReasoningEfforts") or []
            async def effort(level):
                conv.update(model=model_id, effort=level)
                engine = getattr(self.server, "engine", conv.get("engine", "codex"))
                conv.setdefault("models", {})[engine] = model_id
                conv.setdefault("efforts", {})[engine] = level
                self.save()
                await target.send(f"모델 `{model['displayName']}` · 추론 강도 `{level}`로 설정했습니다. 다음 요청부터 적용됩니다.")
            if efforts:
                await self.post(target, "추론 강도 선택", f"{model['displayName']}의 추론 강도를 선택해주세요.",
                    [{"label": e["reasoningEffort"], "value": e["reasoningEffort"],
                      "description": e.get("description", "")} for e in efforts], effort)
            else:
                conv["model"] = model_id
                conv.setdefault("models", {})[getattr(self.server, "engine", conv.get("engine", "codex"))] = model_id
                conv.pop("effort", None)
                conv.setdefault("efforts", {}).pop(getattr(self.server, "engine", conv.get("engine", "codex")), None)
                self.save()
                await target.send(f"모델 `{model['displayName']}`로 설정했습니다.")
        if chosen:
            await choose(models[0]["model"])
        else:
            engine = getattr(self.server, "engine", "codex")
            await self.post(target, "모델 선택", f"사용할 {engine.title()} 모델을 선택해주세요.",
                [{"label": m["displayName"], "value": m["model"], "description": m.get("description", "")}
                 for m in models], choose)

    async def plan_choice(self, target, key, conv):
        async def choose(value):
            conv["mode"] = value
            self.save()
            if value == "default":
                await self.submit(target, key, conv, "방금 제안한 플랜을 승인합니다. 그 플랜대로 구현해주세요.")
            else:
                await target.send("플랜 모드를 유지합니다. 수정할 내용을 보내주세요.")
        await self.post(target, "플랜 확인", "이 플랜을 실행할까요?", [
            {"label": "플랜 실행", "value": "default"}, {"label": "플랜 모드 유지", "value": "plan"}],
            choose, group="plan")

    async def request(self, target, ev):
        ident, method, p = ev["id"], ev["method"], ev.get("params") or {}
        if method == "item/tool/requestUserInput":
            questions, answers = p.get("questions", []), {}
            async def question(i):
                if i >= len(questions):
                    await self.server.respond(ident, {"answers": answers})
                    return
                q = questions[i]
                async def choose(value):
                    answers[q["id"]] = {"answers": value if isinstance(value, list) else [value]}
                    await question(i+1)
                opts = [{"label": o["label"], "description": o.get("description", ""), "value": o["label"]}
                        for o in q.get("options") or []]
                await self.post(target, q.get("header") or "질문",
                    q["question"], opts, choose, freeform=bool(q.get("isOther") or not opts), request=ident,
                    multi=bool(q.get("multiSelect")) and bool(opts))
            await question(0)
        elif method == "claude/exitPlanMode":
            async def choose(value):
                await self.server.respond(ident, {"decision": value})
            await self.post(target, "플랜 확인", (p.get("plan") or "제안한 플랜대로 실행할까요?")[:3500],
                [{"label": "플랜 실행", "value": "accept"}, {"label": "플랜 모드 유지", "value": "decline"},
                 {"label": "취소", "value": "cancel"}], choose, request=ident)
        elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            available = p.get("availableDecisions") or ["accept", "decline", "cancel"]
            labels = {"accept": "이번만 승인", "acceptForSession": "세션 동안 승인", "decline": "거절", "cancel": "작업 취소"}
            options = [{"label": labels[d], "value": d} for d in available if isinstance(d, str) and d in labels]
            async def choose(decision):
                await self.server.respond(ident, {"decision": decision})
            body = p.get("reason") or "이 작업을 승인할까요?"
            if p.get("command"):
                body += "\n\n" + p["command"][:2000]
            await self.post(target, "작업 승인", body, options, choose, request=ident)
        elif method == "item/permissions/requestApproval":
            async def choose(accept):
                await self.server.respond(ident, {"permissions": p["permissions"] if accept else {}, "scope": "turn"})
            await self.post(target, "권한 요청", (p.get("reason") or "요청한 권한을 허용할까요?") +
                "\n\n" + json.dumps(p["permissions"], ensure_ascii=False)[:1500],
                [{"label": "이번 작업에 허용", "value": True}, {"label": "거절", "value": False}], choose, request=ident)
        elif method == "mcpServer/elicitation/request" and p.get("mode") == "url":
            async def choose(action):
                await self.server.respond(ident, {"action": action})
            await self.post(target, "서비스 확인", p.get("message", "") + "\n" + p["url"],
                [{"label": "확인 완료", "value": "accept"}, {"label": "취소", "value": "cancel"}], choose, request=ident)
        elif method == "mcpServer/elicitation/request" and p.get("mode") in ("form", "openai/form", "openaiForm"):
            schema = p.get("requestedSchema") or {}
            fields = list(schema.get("properties", {}).items())
            content = {}
            async def field(i):
                if i >= len(fields):
                    async def confirm(value):
                        await self.server.respond(ident, {"action": value, "content": content if value == "accept" else None})
                    await self.post(target, "입력 확인", p.get("message", "입력한 내용을 제출할까요?") +
                        "\n\n" + json.dumps(content, ensure_ascii=False)[:2000],
                        [{"label": "제출", "value": "accept"}, {"label": "취소", "value": "cancel"}], confirm, request=ident)
                    return
                name, spec = fields[i]
                async def choose(value):
                    kind = spec.get("type", "string")
                    try:
                        if kind in ("number", "integer"):
                            value = int(value) if kind == "integer" else float(value)
                            if spec.get("minimum") is not None and value < spec["minimum"]:
                                raise ValueError()
                            if spec.get("maximum") is not None and value > spec["maximum"]:
                                raise ValueError()
                        elif kind == "array":
                            value = json.loads(value) if isinstance(value, str) else value
                            if not isinstance(value, list):
                                raise ValueError()
                        elif kind == "string":
                            if len(value) < (spec.get("minLength") or 0) or len(value) > (spec.get("maxLength") or 2000):
                                raise ValueError()
                    except (ValueError, TypeError):
                        await target.send("입력 형식이나 범위가 맞지 않습니다. 다시 입력해주세요.")
                        await field(i)
                        return
                    content[name] = value
                    await field(i+1)
                options = [{"label": str(v), "value": v} for v in spec.get("enum", [])]
                options.extend({"label": o.get("title", o["const"]), "value": o["const"]} for o in spec.get("oneOf", []))
                if spec.get("type") == "boolean":
                    options = [{"label": "예", "value": True}, {"label": "아니요", "value": False}]
                await self.post(target, spec.get("title") or name,
                    spec.get("description") or p.get("message", "값을 입력해주세요."), options, choose,
                    freeform=not options, request=ident)
            await field(0)
        else:
            await self.server.reject(ident, "This request type is not supported by the Discord client")
            await target.send(f"⚠️ 아직 지원하지 않는 확인 요청입니다: `{method}`. 자동 승인하지 않았습니다.")

    async def show_pending(self, target):
        entries = [e for e in self.entries.values() if str(e["target"].id) == str(target.id)]
        if entries:
            await target.send("선택을 기다리고 있습니다. 위의 최신 버튼을 눌러주세요.")
        else:
            await target.send("현재 기다리는 선택이 없습니다. 모델은 `!model`, 플랜 모드는 `!plan`으로 설정합니다.")
