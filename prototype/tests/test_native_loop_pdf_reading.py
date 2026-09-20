
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from app.adapters.agents.codex_app_server.native_loop_driver import NativeLoopEventLedger
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.adapters.agents.codex_app_server.agent_native_read_only_runtime import configure_agent_native_read_only_runtime
from tests.test_agent_native_read_only_runtime import _runtime


class NativePdfReadingTests(unittest.TestCase):
    def test_native_reads_are_scoped_and_source_bearing_stdout_is_not_logged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();allowed=root/'workspace';allowed.mkdir();image=allowed/'page.png';image.write_bytes(b'synthetic-image')
            events=[];ledger=NativeLoopEventLedger(emit=events.append,native_read_roots=(allowed,))
            ledger.bind_thread('thread-1');ledger.bind_turn('turn-1')
            def event(method,item):ledger.observe({'method':method,'params':{'threadId':'thread-1','turnId':'turn-1','item':item}})
            command={'id':'cmd','type':'commandExecution','command':'python -c "SENSITIVE_SOURCE"','cwd':str(allowed),'status':'inProgress'}
            event('item/started',command)
            ledger.observe({'method':'item/commandExecution/outputDelta','params':{'threadId':'thread-1','turnId':'turn-1','itemId':'cmd','delta':'SENSITIVE_SOURCE'}})
            event('item/completed',{**command,'status':'completed','exitCode':0,'aggregatedOutput':'SENSITIVE_SOURCE'})
            item={'id':'image','type':'imageView','path':str(image)}
            event('item/started',item);event('item/completed',item)
            self.assertEqual(len(ledger.native_read_receipts),2)
            self.assertNotIn('SENSITIVE_SOURCE',json.dumps(events))
            self.assertEqual(ledger.native_read_receipts[-1]['image_sha256'],hashlib.sha256(image.read_bytes()).hexdigest())
            with self.assertRaisesRegex(LifecycleProbeError,'OUTSIDE_ROOT'):
                event('item/started',{**command,'id':'outside','cwd':str(root)})
            legacy=NativeLoopEventLedger();legacy.bind_thread('thread-1');legacy.bind_turn('turn-1')
            with self.assertRaisesRegex(LifecycleProbeError,'SECURITY_POLICY_VIOLATION'):
                legacy.observe({'method':'item/started','params':{'threadId':'thread-1','turnId':'turn-1','item':command}})

    def test_attested_python_is_explicitly_set_for_the_non_inheriting_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            task=Path(temporary).resolve();state=task/'runtime';state.mkdir();evidence=task/'workspace';evidence.mkdir()
            dependencies=task/'dependencies';dependencies.mkdir()
            env={'PATH':str(dependencies/'python/bin')+':/usr/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'}
            pdf=SimpleNamespace(dependencies_root=dependencies,
                attestation=SimpleNamespace(dependency_root_path_sha256=hashlib.sha256(str(dependencies).encode()).hexdigest()),
                environment=lambda base:{**base,**env})
            runtime=_runtime(state)
            configured=configure_agent_native_read_only_runtime(runtime,readable_root=runtime.sterile_cwd,pdf_dependencies=pdf,evidence_root=evidence)
            self.assertIn(evidence,configured.additional_readable_roots)
            for key,value in env.items():
                self.assertIn('shell_environment_policy.set.'+key+'='+json.dumps(value),configured.runtime.command)
            outside=task/'outside';outside.mkdir()
            with self.assertRaisesRegex(LifecycleProbeError,'EVIDENCE_ROOT_INVALID'):
                configure_agent_native_read_only_runtime(runtime,readable_root=runtime.sterile_cwd,evidence_root=outside)


if __name__=='__main__':unittest.main()
