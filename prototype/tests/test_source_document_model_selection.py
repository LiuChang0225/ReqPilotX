"""An explicitly selected test model/effort reaches the real native driver boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow import source_document_loop_jobs as source

class SourceDocumentModelSelectionTests(TestCase):
    def test_task_selection_reaches_driver_and_legacy_defaults_remain(self):
        for selection, expected in [({'model_id':'gpt-5.6-luna','reasoning_effort':'max'}, ('gpt-5.6-luna','max')),
                                     ({}, ('gpt-5.6-sol','high'))]:
            with self.subTest(selection=selection), TemporaryDirectory() as directory:
                root=Path(directory)
                settings=SimpleNamespace(codex_bin=root/'codex',codex_home=root/'home',agent_model_id='gpt-5.6-sol',development_root=root)
                container=SimpleNamespace(settings=settings)
                meta={'latest_job_id':'job','task_id':'task','project_id':'p','document_id':'d','source_sha256':'a'*64,'request':selection}
                job=SimpleNamespace(job_id='job',aggregate_id='d',payload={'project_id':'p','task_id':'task',
                    'application_entrypoint':source.ENTRYPOINT,'phase':'INITIAL_SOURCE','goal':'Read the original','origin':'USER_REQUEST'})
                with patch.object(source,'_raw_existing',return_value=(container,root,meta)), \
                     patch('app.application.agent_workflow.native_loop_capabilities.native_loop_plugin_for_job',return_value=root/'plugin'), \
                     patch.object(source,'SourceDocumentToolHost') as host, patch.object(source,'LocalCodexAppServerNativeLoopDriver') as driver:
                    from app.application.agent_workflow.native_task_brief import build_task_brief
                    host.return_value.task_brief.return_value=build_task_brief(goal='Read the original',origin='USER_REQUEST',require_map_confirmation=False)
                    driver.return_value.run.return_value=SimpleNamespace(status='COMPLETED',receipt={},final_message='Map is ready')
                    result=source.run_durable_source_document_loop(job,Mock())
                self.assertEqual((driver.call_args.kwargs['model_id'],driver.call_args.kwargs['reasoning_effort']),expected)
                self.assertEqual(result['status'],'NEEDS_CONTINUATION')
                self.assertFalse(result['application_saved'])
    def test_optional_fields_preserve_legacy_request_serialization(self):
        request=source.SourceDocumentLoopStartV1(command_id='legacy',expected_source_sha256='a'*64,goal='Read')
        self.assertNotIn('model_id',request.model_dump(mode='json'))
        self.assertNotIn('reasoning_effort',request.model_dump(mode='json'))
        with self.assertRaises(ValueError):
            source.SourceDocumentLoopStartV1(command_id='bad',expected_source_sha256='a'*64,goal='Read',reasoning_effort='unknown')
