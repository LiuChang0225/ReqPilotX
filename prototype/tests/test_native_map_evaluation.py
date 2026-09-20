
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from evaluate_native_map import validate_case, stage_observations, verify_bundle_files, verified_artifact_handoff, run


class NativeMapEvaluationTests(unittest.TestCase):
    def case(self):
        return dict(case_id='fixture',source_path='source.pdf',source_sha256='a'*64,goal='Map this source scope.',
                    split='development_regression',reviewed_in_development=True,observation_seconds=[180,360],
                    hard_timeout_seconds=900,model='gpt-5.6-luna',reasoning_effort='max')

    def test_tuned_case_and_observation_threshold_cannot_be_misrepresented(self):
        case=self.case();validate_case(case)
        held=copy.deepcopy(case);held['split']='held_out'
        with self.assertRaisesRegex(ValueError,'tune'):validate_case(held)
        case['hard_timeout_seconds']=360
        with self.assertRaisesRegex(ValueError,'later'):validate_case(case)

    def test_observation_is_separate_from_later_completion_and_empty_root(self):
        result=stage_observations([{'since_model_start_seconds':170,'has_source_content':False},
                                   {'since_model_start_seconds':390,'has_source_content':True}], [180,360,600], 500)
        self.assertEqual([r['content_stage_saved'] for r in result],[False,False,True])
        self.assertEqual([r['run_had_ended'] for r in result],[False,False,True])

    def test_source_only_setup_keeps_rubric_out_and_uses_production_goal_builder(self):
        from app.application.agent_workflow.source_document_loop_jobs import source_document_goal
        repo=Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source.pdf';source.write_bytes(b'%PDF-source-fixture')
            binary=root/'codex';binary.write_bytes(b'fake executable is never invoked')
            bundle=root/'bundle';(bundle/'scripts').mkdir(parents=True)
            (bundle/'scripts/reqpilot.py').write_text('')
            (bundle/'build-receipt.json').write_text(json.dumps({'bundle_sha256':'b'*64,'runtime_files':[],
                'plugin_files':[{'path':'scripts/reqpilot.py','sha256':hashlib.sha256(b'').hexdigest()}]}))
            rubric=root/'rubric.json';rubric.write_text('{"private_answer":"never in model workspace"}')
            case=self.case();case.update(source_path=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
            case_path=root/'case.json';case_path.write_text(json.dumps(case))
            args=SimpleNamespace(case=case_path,bundle=bundle,output=root/'run',rubric=rubric,
                                 preparation='source_only',codex_bin=binary,codex_home=root/'home')
            with patch('app.adapters.agents.codex_app_server.native_loop_driver.LocalCodexAppServerNativeLoopDriver') as cls, \
                 patch('app.adapters.agents.codex_app_server.native_loop_pdf.native_loop_skill_paths',return_value={'reqpilot':repo/'plugins/reqpilot/skills/reqpilot/SKILL.md'}), patch('builtins.print'):
                cls.return_value.run.return_value=SimpleNamespace(status='COMPLETED',final_message='fixture',receipt={})
                self.assertEqual(run(args),0)
                actual=cls.return_value.run.call_args.kwargs
                self.assertEqual(actual['goal'],source_document_goal(case['goal'],origin='USER_REQUEST',phase='INITIAL_SOURCE',reading_v1=False,brief=actual['tool_host'].task_brief()))
                self.assertEqual(actual['tool_host'].task_brief()['run_limit_seconds'],900)
            workspace=args.output/'workspace'
            self.assertFalse((workspace/'capture').exists())
            self.assertFalse(any('private_answer' in p.read_text(errors='ignore') for p in workspace.rglob('*') if p.is_file()))
            summary=json.loads((args.output/'summary.json').read_text())
            self.assertEqual(summary['semantic_status'],'NOT_EVALUATED_NO_MODEL_RUN')
            self.assertEqual(summary['host_files_changed_on_disk'],[])
            self.assertTrue((args.output/'host-source-snapshot/prototype/scripts/evaluate_native_map.py').is_file())

    def test_bundle_change_is_caught_before_a_model_can_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'script.py').write_text('changed')
            receipt={'runtime_files':[],'plugin_files':[{'path':'script.py','sha256':hashlib.sha256(b'original').hexdigest()}]}
            with self.assertRaisesRegex(ValueError,'differs from its build receipt'):verify_bundle_files(root,receipt)

    def test_handoff_uses_pinned_files_and_rejects_wrong_paths_or_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);w=root/'workspace';w.mkdir();source=w/'source.pdf';source.write_bytes(b'original')
            mapping=w/'map.json';mapping.write_text('{}')
            source_sha=hashlib.sha256(source.read_bytes()).hexdigest();map_sha=hashlib.sha256(mapping.read_bytes()).hexdigest()
            stage={'stage':'MAP','path':'map.json','source_sha256':source_sha,'file_sha256':map_sha,'artifact_sha256':'a'*64}
            value=verified_artifact_handoff(w,'source.pdf',source_sha,{'MAP':stage})
            self.assertEqual(value['files'][1]['path'],str(mapping.resolve()))
            self.assertFalse(value['model_message_rewritten'])
            for change in [{'path':'../outside.json'},{'source_sha256':'0'*64},{'file_sha256':'0'*64}]:
                (root/'outside.json').write_text('{}')
                with self.assertRaises((ValueError,OSError)):
                    verified_artifact_handoff(w,'source.pdf',source_sha,{'MAP':{**stage,**change}})


if __name__=='__main__':unittest.main()
