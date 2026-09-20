from .runtime_isolation import activate_test_runtime
activate_test_runtime()
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from app.adapters.agents.codex_app_server.native_loop_driver import NativeLoopEventLedger, LifecycleProbeError
from app.adapters.agents.codex_app_server.public_messages import PublicMessageStore, read_public_messages
from app.application.agent_workflow import source_item_loop_jobs as loop
from app.api.routers import source_item_stream as api
from app.application.errors import ApplicationError


class PublicStreamTests(unittest.TestCase):
    def test_real_protocol_delta_identity_and_durable_authoritative_completion(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'messages.json';store=PublicMessageStore(path);events=[]
            def emit(event):
                event={**event,'attempt_id':'a'};events.append(event);store.observe(event)
            ledger=NativeLoopEventLedger(emit=emit);ledger.bind_thread('t');ledger.bind_turn('r')
            item={'id':'m','type':'agentMessage','phase':'commentary','text':''}
            ledger.observe({'method':'item/started','params':{'threadId':'t','turnId':'r','item':item}})
            delta={'method':'item/agentMessage/delta','params':{'threadId':'t','turnId':'r','itemId':'m','delta':'表格正在核对'}}
            ledger.observe(delta)
            self.assertEqual(store.messages[0]['text'],'表格正在核对')
            with self.assertRaises(LifecycleProbeError):ledger.observe({**delta,'params':{**delta['params'],'turnId':'other'}})
            ledger.observe({'method':'item/completed','params':{'threadId':'t','turnId':'r','item':{**item,'text':'核对完成。','phase':'final_answer'}}})
            self.assertEqual(read_public_messages(path)[0]['text'],'核对完成。')
            self.assertEqual(read_public_messages(path)[0]['status'],'complete')
            with self.assertRaises(LifecycleProbeError):ledger.observe(delta)
            for method in ('item/started','item/completed'):
                ledger.observe({'method':method,'params':{'threadId':'t','turnId':'r','item':{'id':'secret','type':'reasoning','summary':['private']}}})
            self.assertNotIn('private',json.dumps(events))
            with self.assertRaises(LifecycleProbeError): ledger.observe({'method':'item/reasoning/textDelta','params':{'threadId':'t','turnId':'r','delta':'private'}})

    def test_public_read_cursor_and_snapshot_are_bound_and_filter_hidden_phases(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);(root/'runtime').mkdir()
            events=[{'type':'agent_message_start','message_id':'m','phase':'commentary','version':0},
                    {'type':'agent_message_delta','message_id':'m','phase':'commentary','version':1,'delta':'实际进展'},
                    {'type':'agent_message','phase':'analysis','text':'private'},
                    {'type':'agent_message','message_id':'m','phase':'final','version':2,'text':'done'}]
            (root/'runtime/events.jsonl').write_text(''.join(json.dumps(event)+'\n' for event in events))
            with patch.object(loop,'_existing',return_value=(None,root,{'source_sha256':'a'*64})),patch.object(loop,'get_source_item_loop',return_value={'job_status':'SUCCEEDED'}):
                first=api.read_batch('p','d','t','a'*64,0,True)
                self.assertEqual(len(first['events']),3);self.assertNotIn('private',json.dumps(first))
                tail=api.read_batch('p','d','t','a'*64,first['next_cursor']);self.assertEqual(tail['events'],[])
                legacy=loop.get_source_item_loop_events('p','d','t',SimpleNamespace(headers={}))
                self.assertEqual(len(legacy['events']),1)
                with self.assertRaises(ApplicationError):api.read_batch('p','d','t','b'*64,0)

class SSETests(unittest.IsolatedAsyncioTestCase):
    async def test_last_event_id_and_terminal_close(self):
        async def connected():return False
        request=SimpleNamespace(headers={'last-event-id':'17'},is_disconnected=connected)
        batch={'events':[],'next_cursor':17,'messages':[],'job_status':'SUCCEEDED'}
        with patch.object(api,'read_batch',return_value=batch) as read:
            result=await api.stream_source_item_events('p','d','t',request,'a'*64,0)
            frames=[frame async for frame in result.body_iterator]
        self.assertEqual(read.call_args.args[4],17)
        self.assertIn('event: activity',frames[0]);self.assertIn('event: complete',frames[-1])
