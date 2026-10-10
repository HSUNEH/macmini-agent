"""Regression checks for native transport and Discord interaction semantics."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from codex_server import CodexServer, ServerError
from discord_native import NativeUI, ChoiceView


FAKE_SERVER = r'''
import sys,json
pending={}
def emit(value):
    print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line)
    method=m.get('method')
    p=m.get('params',{})
    if method=='initialize':
        emit({'id':m['id'],'result':{}})
    elif method=='model/list':
        emit({'id':m['id'],'result':{'data':[{'model':'test-model','displayName':'Test','isDefault':True,'supportedReasoningEfforts':[{'reasoningEffort':'low'}]}]}})
    elif method in ('thread/start','thread/resume','thread/fork'):
        emit({'id':m['id'],'result':{'thread':{'id':p.get('threadId','thread-new')},'model':'test-model'}})
    elif method=='turn/start':
        t=p['threadId']; turn='turn-'+t
        emit({'id':m['id'],'result':{'turn':{'id':turn}}})
        emit({'method':'turn/completed','params':{'threadId':t,'turn':{'id':'old-turn','status':'completed','items':[{'id':'stale','type':'agentMessage','text':'OLD ANSWER'}]}}})
        if p['input'][0]['text']=='question':
            pending['request-'+t]=(t,turn)
            emit({'id':'request-'+t,'method':'item/tool/requestUserInput','params':{'threadId':t,'turnId':turn,'itemId':'q','questions':[{'id':'q','header':'Choice','question':'Choose','options':[{'label':'A','description':''}]}]}})
        elif p['input'][0]['text']=='disconnect':
            sys.exit(0)
        elif p['input'][0]['text']=='wait':
            pass
        else:
            emit({'method':'item/completed','params':{'threadId':t,'turnId':turn,'item':{'id':'new','type':'agentMessage','phase':'final_answer','text':'NEW ANSWER'}}})
            emit({'method':'turn/completed','params':{'threadId':t,'turn':{'id':turn,'status':'completed','items':[]}}})
    elif method=='turn/interrupt':
        emit({'id':m['id'],'result':{}})
        emit({'method':'turn/completed','params':{'threadId':p['threadId'],'turn':{'id':p['turnId'],'status':'interrupted','items':[]}}})
    elif method is None and m.get('id') in pending:
        t,turn=pending.pop(m['id'])
        emit({'method':'serverRequest/resolved','params':{'threadId':t,'requestId':m['id']}})
        emit({'method':'turn/completed','params':{'threadId':t,'turn':{'id':turn,'status':'completed','items':[{'id':'new','type':'agentMessage','phase':'final_answer','text':'CHOSEN'}]}}})
'''


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = CodexServer(command=[sys.executable, "-u", "-c", FAKE_SERVER])

    async def asyncTearDown(self):
        await self.s.close()

    async def run_turn(self, thread="thread-new", prompt="hello", request=None, timeout=5):
        conv = {"engine": "codex", "workdir": "/tmp", "sessions": {"codex": thread}}
        return await self.s.run(conv, prompt, "", AsyncMock(), request or AsyncMock(),
            AsyncMock(), lambda: None, lambda t: None, timeout)

    async def test_completion_excludes_stale_turn(self):
        out = await self.run_turn()
        self.assertEqual(out["reply"], "NEW ANSWER")

    async def test_concurrent_threads_do_not_mix(self):
        a, b = await asyncio.gather(self.run_turn("a"), self.run_turn("b"))
        self.assertEqual((a["session"], b["session"]), ("a", "b"))
        self.assertEqual((a["reply"], b["reply"]), ("NEW ANSWER", "NEW ANSWER"))

    async def test_question_does_not_continue_until_response(self):
        ready = asyncio.Event()
        request_id = []
        async def request(ev):
            request_id.append(ev["id"])
            ready.set()
        task = asyncio.create_task(self.run_turn(prompt="question", request=request))
        await asyncio.wait_for(ready.wait(), 2)
        self.assertFalse(task.done())
        await self.s.respond(request_id[0], {"answers": {"q": {"answers": ["A"]}}})
        self.assertEqual((await task)["reply"], "CHOSEN")
        with self.assertRaises(ServerError):
            await self.s.respond(request_id[0], {})

    async def test_disconnect_fails_turn_instead_of_replaying(self):
        with self.assertRaisesRegex(ServerError, "연결"):
            await self.run_turn(prompt="disconnect")

    async def test_timeout_interrupts_and_clears_routes(self):
        with self.assertRaisesRegex(ServerError, "시간"):
            await self.run_turn(prompt="wait", timeout=.1)
        self.assertFalse(self.s.streams)
        self.assertFalse(self.s.active)


class FakeTarget:
    id = "channel"
    def __init__(self):
        self.messages = []
    async def send(self, *args, **kwargs):
        message = SimpleNamespace(edit=AsyncMock(), kwargs=kwargs)
        self.messages.append(message)
        return message


class UITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = SimpleNamespace(requests={7: "thread"}, respond=AsyncMock(), reject=AsyncMock())
        self.ui = NativeUI(self.s, lambda: None, AsyncMock(), ["user"])
        self.target = FakeTarget()

    def inter(self):
        return SimpleNamespace(user=SimpleNamespace(id="user"), channel=self.target,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    async def test_sequential_questions_wait_for_each_click(self):
        ev = {"id": 7, "method": "item/tool/requestUserInput", "params": {"questions": [
            {"id": "q1", "header": "First", "question": "One?", "options": [{"label": "A"}]},
            {"id": "q2", "header": "Second", "question": "Two?", "options": [{"label": "B"}]}]}}
        await self.ui.request(self.target, ev)
        self.assertEqual(len(self.target.messages), 1)
        first = next(iter(self.ui.entries))
        await self.ui.answer(self.inter(), first, 0, index=True)
        self.assertEqual(len(self.target.messages), 2)
        self.s.respond.assert_not_awaited()
        second = next(iter(self.ui.entries))
        await self.ui.answer(self.inter(), second, 0, index=True)
        self.s.respond.assert_awaited_once_with(7, {"answers": {"q1": {"answers": ["A"]}, "q2": {"answers": ["B"]}}})
        await self.ui.answer(self.inter(), second, 0, index=True)
        self.assertEqual(self.s.respond.await_count, 1)

    async def test_request_resolution_disables_buttons(self):
        await self.ui.post(self.target, "Question", "Choose", [{"label": "A", "value": "A"}], AsyncMock(), request=7)
        await self.ui.resolved(7)
        self.assertFalse(self.ui.entries)
        self.target.messages[0].edit.assert_awaited_with(view=None)

    async def test_simultaneous_requests_have_independent_buttons(self):
        for ident in (7, 8):
            await self.ui.post(self.target, "Question", "Choose", [{"label": "A", "value": "A"}], AsyncMock(), request=ident)
        self.assertEqual(len(self.ui.entries), 2)

    async def test_model_then_effort_is_two_explicit_choices(self):
        self.s.models = AsyncMock(return_value=[{"model": "test", "displayName": "Test", "supportedReasoningEfforts": [
            {"reasoningEffort": "low"}, {"reasoningEffort": "high"}]}])
        conv = {}
        await self.ui.show_models(self.target, conv)
        await self.ui.answer(self.inter(), next(iter(self.ui.entries)), 0, index=True)
        self.assertNotIn("model", conv)
        await self.ui.answer(self.inter(), next(iter(self.ui.entries)), 1, index=True)
        self.assertEqual(conv["model"], "test")
        self.assertEqual(conv["effort"], "high")
        self.assertEqual(conv["models"]["codex"], "test")

    async def test_plan_never_executes_without_click(self):
        conv = {"mode": "plan"}
        await self.ui.plan_choice(self.target, "channel", conv)
        self.ui.submit.assert_not_awaited()
        await self.ui.answer(self.inter(), next(iter(self.ui.entries)), 0, index=True)
        self.ui.submit.assert_awaited_once()
        self.assertEqual(conv["mode"], "default")

    async def test_choices_do_not_duplicate_terminal_screen(self):
        await self.ui.post(self.target, "Question", "Choose", [{"label": "A", "value": "A"}], AsyncMock())
        self.assertEqual(self.target.messages[-1].kwargs["embed"].description, "Choose")
        view = ChoiceView("token", [{"label": str(i), "value": str(i)} for i in range(25)])
        self.assertEqual(len(view.children[0].options), 25)


if __name__ == "__main__":
    unittest.main()
