
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import revalidate_native_task_runtime as entry
from app.application.agent_workflow.source_document_loop_jobs import SCHEMA


class RuntimeMaintenanceEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.repo=Path(self.temp.name).resolve()
        self.root=self.repo/'prototype/runtime-data/objects/runtime/native-loop-tasks/task'
        (self.root/'workspace/source').mkdir(parents=True);(self.root/'runtime').mkdir()
        source=self.root/'workspace/source/original.pdf';source.write_bytes(b'original fixture')
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
        self.meta={'schema_version':SCHEMA,'project_id':'project','document_id':'doc','task_id':'task',
                   'source_path':'source/original.pdf','source_sha256':digest}
        (self.root/'task.json').write_text(json.dumps(self.meta))
        (self.root/'runtime/run-state.json').write_text(json.dumps({'identity':{'model_id':'gpt-5.6-luna','reasoning_effort':'max'}}))
        self.public={'task_id':'task','source_sha256':digest,'job_status':'FAILED'}
        self.argv=['maintenance','--task-root',str(self.root),'--codex-bin',str(self.repo/'binary'),
                   '--codex-home',str(self.repo/'home'),'--reason','Test controlled runtime maintenance']

    def tearDown(self):self.temp.cleanup()

    def invoke(self, public):
        with patch.object(entry,'REPO',self.repo),patch.object(sys,'argv',self.argv), \
             patch.object(entry,'urlopen',return_value=io.StringIO(json.dumps(public))),patch('builtins.print'), \
             patch('app.adapters.agents.codex_app_server.native_loop_driver.LocalCodexAppServerNativeLoopDriver') as cls:
            cls.return_value.revalidate_runtime_identity.return_value={'status':'UNCHANGED','thread_id':'thread','new_model_turn_started':False}
            try:return entry.main(),cls
            finally:self.driver_called=cls.return_value.revalidate_runtime_identity.called

    def test_receipt_is_written_and_business_source_is_preserved(self):
        before=(self.root/'workspace/source/original.pdf').read_bytes()
        self.invoke(self.public)
        receipts=list((self.root/'runtime-maintenance').glob('*.json'))
        self.assertEqual(len(receipts),1)
        value=json.loads(receipts[0].read_text())
        self.assertTrue(value['business_files_unchanged']);self.assertFalse(value['customer_model_resumed'])
        self.assertEqual((self.root/'workspace/source/original.pdf').read_bytes(),before)

    def test_active_or_mismatched_task_is_rejected_before_runtime_change(self):
        for change in [{'job_status':'RUNNING'},{'source_sha256':'0'*64},{'task_id':'other'}]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.invoke({**self.public,**change})
            self.assertFalse(self.driver_called)


if __name__=='__main__':unittest.main()
