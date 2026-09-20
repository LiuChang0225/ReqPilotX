"""Collector behavior uses real compiler receipts; never a document answer oracle."""
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'prototype'), str(ROOT/'plugins/reqpilot/scripts')]
from tests import test_task_workspace as backend_fixture
from app.application.agent_workflow import task_workspace as core
import summarize_execution as collector


class ExecutionSummaryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = backend_fixture.TaskWorkspaceTests('test_map_only_recovers_without_baseline_or_export_and_can_move')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.workspace = self.fixture.base
        (self.workspace/'.reqpilot').mkdir()
        (self.workspace/'.reqpilot/build-receipt.json').write_text('{}')
        runtime = self.workspace/'runtime-data'; runtime.mkdir()
        (runtime/'input-manifest.json').write_text('{"prior_results_provided":false}')
        self.output = runtime/'cold-session'; self.output.mkdir()
        self.events = self.output/'events.jsonl'
        self.events.write_text('')

    def summarize(self):
        with patch.object(core, '_atomic_text', side_effect=AssertionError('Evaluator must not refresh TASK.md')):
            return collector.summarize(self.workspace, self.events, 0)

    def test_discovery_outside_legacy_runtime_keeps_committed_candidate_and_half_bundle_distinct(self):
        first = self.fixture.compile_map(); second = self.fixture.compile_map()
        core.commit_operation(self.fixture.root, first['operation_id'])
        core.commit_operation(self.fixture.root, second['operation_id'])
        partial = core.begin_operation(self.fixture.root, 'compile-map')
        Path(partial['output_dir']).mkdir()
        (Path(partial['output_dir'])/'document-map.json').write_text('{}')
        task_before = (self.fixture.root/'TASK.md').read_bytes()
        result = self.summarize()
        task = result['task_workspaces'][0]
        self.assertTrue(task['task_root'].startswith('working/runtime-data/projects/'))
        self.assertEqual(task['validation'], 'PASSED')
        self.assertEqual({op['commit_state'] for op in task['operations']}, {'COMMITTED','CANDIDATE_CONFLICT','NOT_COMMITTED'})
        half = next(op for op in task['operations'] if op['operation_id'] == partial['operation_id'])
        self.assertEqual(half['output_state'], 'PARTIAL_OR_UNCOMMITTED')
        self.assertEqual(half['validation'], 'NOT_VALIDATED')
        self.assertEqual(task['first_committed_artifacts']['DOCUMENT_MAP']['operation_id'], first['operation_id'])
        self.assertEqual(task['first_committed_artifacts']['DOCUMENT_MAP']['time']['source'], 'OPERATION_RECEIPT_SAVED_AT')
        self.assertFalse(task['first_committed_artifacts']['DOCUMENT_MAP']['time']['estimated'])
        self.assertIsNone(task['first_committed_artifacts']['DOCUMENT_MAP']['time']['elapsed_seconds'])
        self.assertEqual((self.fixture.root/'TASK.md').read_bytes(), task_before)
        self.assertEqual(result['semantic_pass'], 'REQUIRES_SOURCE_AND_OUTPUT_REVIEW')

    def test_saved_receipt_without_index_is_not_labeled_committed_or_recovered(self):
        operation = self.fixture.compile_map()
        with patch.object(core, '_write_index', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError): core.commit_operation(self.fixture.root, operation['operation_id'])
        before = (self.fixture.root/'workspace-index.json').read_bytes()
        task = self.summarize()['task_workspaces'][0]
        self.assertEqual(task['operations'][0]['commit_state'], 'SAVED_UNINDEXED')
        self.assertEqual(task['operations'][0]['validation'], 'PASSED')
        self.assertEqual(task['first_committed_artifacts'], {})
        self.assertEqual(task['selected'], {})
        self.assertEqual((self.fixture.root/'workspace-index.json').read_bytes(), before)

    def test_same_source_multiple_tasks_are_reported_without_a_latest_winner(self):
        self.fixture.saved_map()
        second = core.initialize_workspace(self.workspace/'different-root', self.fixture.source, 'Independent source task',
            project_key='another-project', document_key='another-document', require_map_confirmation=False)
        result = self.summarize(); tasks = result['task_workspaces']
        self.assertEqual(len(tasks), 2)
        self.assertEqual(len({task['source_sha256'] for task in tasks}), 1)
        self.assertEqual(len({task['work_id'] for task in tasks}), 2)
        self.assertEqual({task['work_state'] for task in tasks}, {'SOURCE_TO_MAP','MAP_SAVED'})
        self.assertNotIn('current_task', result)
        self.assertIn(second['context']['work_id'], {task['work_id'] for task in tasks})

    def test_actual_review_owner_wins_over_a_later_unclosed_candidate(self):
        saved = self.fixture.saved_items()
        operation = core.begin_operation(self.fixture.root, 'review-open', {'baseline_dir': Path(saved['selected']['ITEMS']['output_dir'])})
        backend_fixture.revision_tools.open_review(Namespace(source=self.fixture.original,
            baseline_dir=Path(saved['selected']['ITEMS']['output_dir']), review_dir=Path(operation['output_dir'])))
        current = core.commit_operation(self.fixture.root, operation['operation_id'])
        candidate = self.fixture.compile_map(); core.commit_operation(self.fixture.root, candidate['operation_id'])
        task = self.summarize()['task_workspaces'][0]
        self.assertEqual(task['current_owner'], 'portable_review')
        self.assertEqual(task['review']['review_sha256'], current['current_working']['review_sha256'])
        self.assertIn('/review/versions/', task['selected']['MAP']['path'])
        self.assertEqual(task['selected']['MAP']['compiler_bundle_availability'], 'AVAILABLE')
        self.assertNotIn('/review/versions/', task['selected']['MAP']['compiler_output_dir'])
        self.assertEqual(next(op for op in task['operations'] if op['operation_id']==candidate['operation_id'])['commit_state'], 'SAVED_CANDIDATE')

    def test_usage_and_actual_public_operation_time_are_observed_not_invented(self):
        saved = self.fixture.saved_map()
        receipt = json.loads(Path(saved['receipt_path']).read_text())
        saved_at = datetime.fromisoformat(receipt['saved_at']).timestamp()
        (self.output/'execution-start.json').write_text(json.dumps({'started_epoch_seconds':saved_at-20}))
        observed = datetime.fromtimestamp(saved_at+10, timezone.utc).isoformat()
        output = json.dumps({'status':'WORKSPACE_OPERATION_COMPLETE','state':'COMMITTED','operation_id':saved['operation_id']})
        events = [{'type':'thread.started','thread_id':'fresh-thread'},
            {'type':'item.completed','timestamp':observed,'item':{'type':'command_execution','command':'actual workspace run',
                'exit_code':0,'status':'completed','aggregated_output':output}},
            {'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':90,'output_tokens':4,'reasoning_output_tokens':2}}]
        self.events.write_text(''.join(json.dumps(value)+'\n' for value in events))
        result = self.summarize()
        self.assertEqual(result['usage']['totals']['input_tokens'], 100)
        self.assertEqual(result['usage']['totals']['output_tokens'], 4)
        self.assertEqual(result['usage']['totals']['reasoning_output_tokens'], 2)
        self.assertIsNone(result['usage']['totals']['cache_write_input_tokens'])
        self.assertEqual(result['completed_commands'][0]['output_excerpt'], output)
        timing = result['task_workspaces'][0]['first_saved_artifacts']['DOCUMENT_MAP']['time']
        self.assertEqual(timing['source'], 'PUBLIC_OPERATION_RESULT_TIMESTAMP')
        self.assertEqual(timing['elapsed_seconds'], 30)
        with self.events.open('a') as stream: stream.write(json.dumps({'type':'turn.completed'})+'\n')
        partial = self.summarize()['usage']
        self.assertEqual(partial['reported_turns'], 1)
        self.assertIsNone(partial['totals']['input_tokens'])
        self.events.write_text(json.dumps({'type':'turn.failed','error':'interrupted'})+'\n')
        self.assertIsNone(self.summarize()['usage'])

    def test_symlinks_and_external_task_dependencies_are_never_read(self):
        self.fixture.saved_map()
        with TemporaryDirectory() as external:
            outside = Path(external)/'outside.json'; outside.write_text('{}')
            (self.workspace/'linked-task').symlink_to(external, target_is_directory=True)
            context_path = self.fixture.root/'task-context.json'
            context = json.loads(context_path.read_text()); context['binding_file'] = str(outside)
            context_path.write_text(json.dumps(core._seal(context, 'context_sha256')))
            original_read_text = Path.read_text
            def guarded(path, *args, **kwargs):
                if path.resolve().is_relative_to(Path(external)):
                    raise AssertionError('Evaluator crossed its read boundary')
                return original_read_text(path, *args, **kwargs)
            with patch.object(Path, 'read_text', guarded): result = self.summarize()
        self.assertEqual(result['task_workspaces'][0]['validation'], 'UNAVAILABLE')
        self.assertIn('outside the cold workspace', result['task_workspaces'][0]['error'])
        self.assertEqual(result['skipped_paths'], [{'path':'linked-task','reason':'SYMLINK_NOT_FOLLOWED'}])

    def test_legacy_files_remain_estimates_and_package_examples_are_excluded(self):
        legacy = self.workspace/'runtime-data/old-run/map'; legacy.mkdir(parents=True)
        (legacy/'document-map.json').write_text('{}')
        for directory in (self.workspace/'.reqpilot/examples', self.workspace/'.venv/data'):
            directory.mkdir(parents=True); (directory/'document-map.json').write_text('{}')
        result = self.summarize()
        self.assertEqual(len(result['observed_artifacts']), 1)
        artifact = result['observed_artifacts'][0]
        self.assertEqual(artifact['evidence_role'], 'LEGACY_FILE_OBSERVATION')
        self.assertEqual(artifact['file_time']['source'], 'FILE_MTIME_ESTIMATE')
        self.assertIsNone(result['usage'])

    def test_corrupt_committed_artifact_does_not_hide_another_valid_task_or_claim_current(self):
        saved = self.fixture.saved_map()
        (Path(saved['selected']['MAP']['path'])).write_text('{}')
        core.initialize_workspace(self.workspace/'other', self.fixture.source, 'Independent task', require_map_confirmation=False)
        tasks = self.summarize()['task_workspaces']
        broken = next(task for task in tasks if task['operations'])
        valid = next(task for task in tasks if not task['operations'])
        self.assertEqual(broken['validation'], 'UNAVAILABLE')
        self.assertEqual(broken['operations'][0]['validation'], 'FAILED')
        self.assertEqual(broken['selected'], {})
        self.assertIsNone(broken['current_owner'])
        self.assertEqual(valid['validation'], 'PASSED')
