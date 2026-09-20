
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.native_loop_driver import (
    LocalCodexAppServerNativeLoopDriver, STATE_SCHEMA, _digest,
)


class RuntimeRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.binary=self.root/'codex';self.binary.write_bytes(b'verified new binary fixture')
        self.home=self.root/'home';self.home.mkdir(mode=0o700)
        self.repo=self.root/'repo';self.repo.mkdir()
        self.runtime=self.root/'runtime';self.runtime.mkdir()
        self.state={'schema_version':STATE_SCHEMA,'status':'COMPLETED','thread_id':'thread-old','turn_id':'turn-old',
                    'pid':None,'app_server_pid':None,
                    'identity':{'binary_sha256':'a'*64,'model_id':'gpt-5.6-luna','reasoning_effort':'max',
                                'codex_home_sha256':_digest(str(self.home)), 'binding_sha256':'b'*64,
                                'skills_sha256':'c'*64,'tools_sha256':'d'*64,'native_pdf_sha256':'e'*64},
                    'last_receipt':{'identity':{'binary_sha256':'a'*64},'terminal_snapshot_verified':True}}
        self.path=self.runtime/'run-state.json';self.path.write_text(json.dumps(self.state))
        self.driver=LocalCodexAppServerNativeLoopDriver(codex_bin=self.binary,codex_home=self.home,
            repository_root=self.repo,model_id='gpt-5.6-luna',reasoning_effort='max')
        self.observation={'verified_terminal':True,'thread_id':'thread-old','turn_id':'turn-old',
                          'new_model_turn_started':False,'runtime_protocol':{'binary_sha256':hashlib.sha256(self.binary.read_bytes()).hexdigest()}}

    def tearDown(self):self.temp.cleanup()

    def refresh(self):
        return self.driver.revalidate_runtime_identity(session_dir=self.runtime,
            expected_identity_sha256=_digest(self.state['identity']),reason='Validate a known binary update')

    def test_only_binary_pin_changes_and_old_receipt_is_preserved(self):
        original=copy.deepcopy(self.state)
        with patch.object(self.driver,'_inspect_session_locked',return_value=self.observation) as inspect:
            result=self.refresh();inspect.assert_called_once()
        current=json.loads(self.path.read_text())
        changed={k for k,v in current['identity'].items() if v!=original['identity'][k]}
        self.assertEqual(changed,{'binary_sha256'})
        self.assertEqual(current['last_receipt'],original['last_receipt'])
        self.assertFalse(result['new_model_turn_started'])
        proof=json.loads((self.runtime/result['reference']).read_text())
        self.assertEqual(proof['previous_identity'],original['identity'])
        self.assertEqual(current['runtime_revalidations'][0]['sha256'],_digest(proof))

    def test_unverified_or_changed_thread_and_binary_cannot_refresh(self):
        for updates in [{'verified_terminal':False},{'thread_id':'another-thread'},
                        {'new_model_turn_started':True},{'runtime_protocol':{'binary_sha256':'0'*64}}]:
            with self.subTest(updates=updates), patch.object(self.driver,'_inspect_session_locked',return_value={**self.observation,**updates}):
                with self.assertRaises(Exception):self.refresh()
                self.assertEqual(json.loads(self.path.read_text())['identity'],self.state['identity'])

    def test_running_context_change_and_stale_predecessor_are_rejected(self):
        with patch.object(self.driver,'_inspect_session_locked') as inspect:
            state=copy.deepcopy(self.state);state['pid']=42;self.path.write_text(json.dumps(state))
            with self.assertRaisesRegex(Exception,'REQUIRES_STOPPED'):self.refresh()
            self.path.write_text(json.dumps(self.state));self.driver.model_id='different-model'
            with self.assertRaisesRegex(Exception,'CHANGED_CONTEXT'):self.refresh()
            self.driver.model_id='gpt-5.6-luna'
            with self.assertRaisesRegex(Exception,'PREDECESSOR_CHANGED'):
                self.driver.revalidate_runtime_identity(session_dir=self.runtime,expected_identity_sha256='f'*64,reason='stale')
            inspect.assert_not_called()

    def test_failed_inspection_does_not_update_identity(self):
        with patch.object(self.driver,'_inspect_session_locked',side_effect=RuntimeError('protocol mismatch')):
            with self.assertRaisesRegex(RuntimeError,'protocol mismatch'):self.refresh()
        self.assertEqual(json.loads(self.path.read_text())['identity'],self.state['identity'])


if __name__=='__main__':unittest.main()
