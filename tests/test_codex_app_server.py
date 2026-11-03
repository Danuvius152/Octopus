"""One accepted native root through the owned subprocess interface."""

import asyncio
import sys
from dataclasses import replace

import pytest

from server.harness import Harness, codex_app_server
from server.harness.codex import CODEX


@pytest.mark.parametrize("confirmation", ["delayed_ack", "input_echo", "unicode_echo", "none"])
async def test_native_root_binding_requires_correlated_acceptance(tmp_path, monkeypatch, confirmation):
    monkeypatch.setattr(codex_app_server, "verify_binary", lambda path: None)
    monkeypatch.setattr("server.harness.codex_rpc.RPC_TIMEOUT", 0.5)
    received = tmp_path / "business-input"
    script = '''import json,sys
from pathlib import Path
def send(value): print(json.dumps(value,ensure_ascii=False),flush=True)
for line in sys.stdin:
 f=json.loads(line); method=f.get('method'); params=f.get('params',{}); result={}
 if method=='initialized': continue
 if method=='config/read': result={'config':{'model':'fixture','model_reasoning_effort':'low','features':{'fast_mode':True,'wake_on_agent_messages':False,'goals':False,'sleep_tool':False,'unbounded_connection_retries':False,'multi_agent':True,'multi_agent_v2':{'enabled':True,'wait_agent_enabled':True}}}}
 if method=='thread/start': result={'thread':{'id':'thread-1'},'model':'fixture','reasoningEffort':'low','serviceTier':'default','cwd':params['cwd'],'approvalPolicy':'never','sandbox':{'type':'dangerFullAccess'}}
 if method=='turn/start':
  Path(sys.argv[1]).write_text(params['input'][0]['text'])
  send({'method':'turn/started','params':{'threadId':'thread-1','turn':{'id':'turn-1'}}})
  if sys.argv[2] in {'input_echo','unicode_echo'}: send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'type':'userMessage','id':'user-1','clientId':params['clientUserMessageId'],'content':params['input']}}})
  send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'type':'agentMessage','id':'answer-1','phase':'final_answer','text':'Bound answer'}}})
  send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}})
  result={'turn':{'id':'turn-1'}}
  if sys.argv[2]!='delayed_ack': continue
 if 'id' in f: send({'id':f['id'],'result':result})
'''
    profile = replace(
        codex_app_server.profile(CODEX),
        build_turn_argv=lambda context: ([sys.executable, "-u", "-c", script, str(received), confirmation], {"cwd": context.working_dir}),
    )
    run = Harness(profile).create_run()
    run.bind_tool_context("input-1", "attempt-1")
    evidence = []

    async def record(item):
        if item.state == "unknown":
            assert not received.exists(), "The original submission must commit before business write"
        evidence.append(item)

    run.bind_input_recorder(record)
    try:
        if confirmation == "none":
            with pytest.raises(codex_app_server.NativeProtocolError, match="acceptance is unknown"):
                await run.start("Original input", str(tmp_path))
            assert received.read_text() == "Original input"
            assert evidence[-1].state == "unknown"
            assert evidence[-1].turn_id is None
            return
        prompt = "🙂" * 750000 if confirmation == "unicode_echo" else "Original input"
        await asyncio.wait_for(run.start(prompt, str(tmp_path)), 3)
        async with asyncio.timeout(3):
            events = [event async for event in run.stream()]
        assert received.read_text() == prompt
        assert [event.content for event in events if event.type == "text"] == ["Bound answer"]
        assert [event.type for event in events].count("result") == 1
        assert events[-1].type == "result", "Internal retirement observations must precede the terminal result"
        assert not events[-1].is_error
        assert evidence[-1].state == "accepted"
        assert evidence[-1].turn_id == "turn-1"
        assert evidence[-1].input_id == "input-1"
        assert evidence[-1].rpc_id != evidence[-1].turn_id
        assert evidence[-1].acceptance_source == ("user_message" if confirmation in {"input_echo", "unicode_echo"} else "start_response")
    finally:
        await run.stop()
    assert run.retirement_state == "proven"
