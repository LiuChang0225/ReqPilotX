from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import shutil
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'plugins/reqpilot/scripts'), str(ROOT / 'plugins/reqpilot/tests')]
from test_portable import make_fixture, item_decisions, write
import artifact_tools
import revision_tools
from app.application.agent_workflow import task_workspace as workspace


class TaskWorkspaceTests(unittest.TestCase):
    def test_revision_token_rejects_stale_caller_and_is_not_a_host_receipt_head(self):
        initial=workspace.workspace_status(self.root,refresh=False)
        saved=self.saved_map()
        self.assertNotEqual(initial['revision'],saved['revision'])
        with self.assertRaisesRegex(ValueError,'REVISION_CONFLICT'):
            workspace.begin_operation(self.root,'compile-map',expected_revision=initial['revision'],command_id='old-read',request={'decision':'same'})
        self.assertIsNone(workspace.lookup_operation_command(self.root,'old-read','compile-map',{'decision':'same'}))
        workspace.record_host_receipt(self.root,saved['operation_id'],{'status':'NOT_BOUND'})
        self.assertEqual(workspace.workspace_status(self.root,refresh=False)['revision'],saved['revision'])

    def test_command_replay_precedes_old_revision_check_and_rejects_changed_request(self):
        revision=workspace.workspace_status(self.root,refresh=False)['revision']
        request={'decision':'prepare exact map','expected_revision':revision}
        operation=workspace.begin_operation(self.root,'compile-map',{'source_ir':self.capture/'source_ir.json','input':self.map_input},
            expected_revision=revision,command_id='same-command',request=request)
        repeated=workspace.begin_operation(self.root,'compile-map',expected_revision=revision,command_id='same-command',request=request)
        self.assertEqual(repeated['operation_id'],operation['operation_id']);self.assertTrue(repeated['replayed'])
        self.compile_map(begin=operation);saved=workspace.commit_operation(self.root,operation['operation_id'],result={'status':'MAP_COMPILED'})
        replay=workspace.begin_operation(self.root,'compile-map',expected_revision=revision,command_id='same-command',request=request)
        self.assertEqual(replay['state'],'COMMITTED');self.assertEqual(replay['receipt_sha256'],saved['receipt_sha256'])
        self.assertEqual(replay['result'],{'status':'MAP_COMPILED'})
        with self.assertRaisesRegex(ValueError,'COMMAND_ID_CONFLICT'):
            workspace.begin_operation(self.root,'compile-map',expected_revision=revision,command_id='same-command',request={**request,'decision':'different'})
        self.assertEqual(len(workspace.workspace_status(self.root)['index']['committed']),1)

    def test_command_replay_finds_commit_even_when_operation_descriptor_write_was_lost(self):
        operation=workspace.begin_operation(self.root,'compile-map',{'input':self.map_input,'source_ir':self.capture/'source_ir.json'},
            command_id='lost-reply',request={'scope':'current'})
        self.compile_map(begin=operation)
        original=workspace._atomic_json
        def write(path,value):
            if path.name==operation['operation_id']+'.json' and value.get('state')=='COMMITTED':raise OSError('descriptor reply lost')
            return original(path,value)
        with patch.object(workspace,'_atomic_json',side_effect=write),self.assertRaisesRegex(OSError,'reply lost'):
            workspace.commit_operation(self.root,operation['operation_id'],result={'status':'MAP_COMPILED'})
        replay=workspace.lookup_operation_command(self.root,'lost-reply','compile-map',{'scope':'current'})
        self.assertEqual(replay['state'],'COMMITTED');self.assertEqual(replay['result']['status'],'MAP_COMPILED')
        recovered=workspace.recover_workspace(self.root)
        self.assertEqual(len(recovered['index']['committed']),1)

    def test_review_only_change_also_changes_revision_without_index_generation(self):
        saved=self.saved_items()
        op=workspace.begin_operation(self.root,'review-open',{'baseline_dir':Path(saved['selected']['ITEMS']['output_dir'])})
        revision_tools.open_review(Namespace(source=self.original,baseline_dir=Path(saved['selected']['ITEMS']['output_dir']),review_dir=Path(op['output_dir'])))
        before=workspace.commit_operation(self.root,op['operation_id'])
        payload=self.root/'notes/new-feedback.json';write(payload,{'expected_review_sha256':before['current_working']['review_sha256'],
            'feedback':[{'origin':'AGENT_SELF_REVIEW','source_item_ids':[],'text':'A new source question.'}]})
        revision_tools.feedback_review(Namespace(review_dir=self.root/'review',input=payload))
        after=workspace.workspace_status(self.root,refresh=False)
        self.assertEqual(before['index']['generation'],after['index']['generation'])
        self.assertNotEqual(before['revision'],after['revision'])

    def test_brief_projects_verified_current_policy_and_keeps_missing_policy_unknown(self):
        saved = self.saved_items()
        self.assertEqual(saved['brief']['analysis_policy'], {'status': 'UNKNOWN'})
        map_dir = Path(saved['selected']['MAP']['output_dir'])
        source_sha = saved['context']['source']['sha256']
        policy = {'source_sha256': source_sha, 'status': 'SKIPPED', 'reason': 'NO_INTERNAL_EVIDENCE'}
        operation = workspace.begin_operation(self.root, 'compile-items', {'map_dir': map_dir, 'input': self.root / 'notes/items-input.json'})
        output = Path(operation['output_dir'])
        artifact_tools.compile_items(Namespace(map_dir=map_dir, input=self.root / 'notes/items-input.json', baseline_dir=None, output_dir=output))
        write(output / 'analysis-policy.json', policy)
        baseline = workspace.commit_operation(self.root, operation['operation_id'])
        self.assertEqual(baseline['brief']['analysis_policy'], policy)
        review_policy = {**policy, 'reason': 'USER_SKIPPED'}
        policy_input = self.root / 'notes/review-policy.json'; write(policy_input, review_policy)
        opening = workspace.begin_operation(self.root, 'review-open', {'baseline_dir': output, 'analysis_policy': policy_input})
        revision_tools.open_review(Namespace(source=self.original, baseline_dir=output, analysis_policy=policy_input, review_dir=Path(opening['output_dir'])))
        current = workspace.commit_operation(self.root, opening['operation_id'])
        self.assertEqual(current['brief']['analysis_policy'], review_policy)
        self.assertIn('内部依据策略：SKIPPED', (self.root / 'TASK.md').read_text())
        self.assertEqual(workspace.workspace_status(self.root, refresh=False)['revision'], current['revision'])

    def test_creation_key_reuses_generated_task_and_rejects_different_request(self):
        first=workspace.initialize_workspace(self.work,self.source,'One creation request',creation_key='create-once')
        replay=workspace.initialize_workspace(self.work,self.source,'One creation request',creation_key='create-once')
        self.assertEqual(first['task_root'],replay['task_root']);self.assertTrue(replay['replayed'])
        with self.assertRaisesRegex(ValueError,'CREATION_KEY_CONFLICT'):
            workspace.initialize_workspace(self.work,self.source,'Another request',creation_key='create-once')
        document=json.loads((Path(first['task_root']).parent.parent/'document.json').read_text())
        self.assertEqual(len(document['tasks']),1)

    def test_creation_reservation_retries_before_write_and_never_replaces_partial_root(self):
        with patch.object(workspace,'_initialize_workspace',side_effect=OSError('before task write')),self.assertRaises(OSError):
            workspace.initialize_workspace(self.work,self.source,'Reserved task',creation_key='reserved')
        completed=workspace.initialize_workspace(self.work,self.source,'Reserved task',creation_key='reserved')
        self.assertTrue(Path(completed['task_root']).is_dir())
        def partial(working,source,goal,**kwargs):
            root=Path(working)/'runtime-data/projects'/kwargs['project_key']/'documents'/kwargs['document_key']/'tasks'/kwargs['_work_id']
            root.mkdir(parents=True)
            raise OSError('partial initialization')
        with patch.object(workspace,'_initialize_workspace',side_effect=partial),self.assertRaises(OSError):
            workspace.initialize_workspace(self.work,self.source,'Partial task',creation_key='partial')
        with self.assertRaisesRegex(ValueError,'CREATION_INCOMPLETE'):
            workspace.initialize_workspace(self.work,self.source,'Partial task',creation_key='partial')

    def test_default_new_task_is_autonomous_without_changing_explicit_or_existing_policy(self):
        automatic=workspace.initialize_workspace(self.work,self.source,'自主完成原文整理与当前能力支持的交付。',project_key='policy',document_key='document')
        required=workspace.initialize_workspace(self.work,self.source,'结构确认后再处理条目。',project_key='policy',document_key='document',require_map_confirmation=True)
        self.assertFalse(automatic['context']['request']['require_map_confirmation'])
        self.assertTrue(required['context']['request']['require_map_confirmation'])
        for state in (automatic,required):
            self.assertEqual(workspace.workspace_status(state['task_root'])['context']['request'],state['context']['request'])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source, self.capture, self.decisions = make_fixture(self.base / 'fixture')
        self.work = self.base / 'working'
        initialized = workspace.initialize_workspace(self.work, self.source, '按原序高保真整理原件，先保存结构。', project_key='project-a', document_key='doc-a', require_map_confirmation=False)
        self.root = Path(initialized['task_root'])
        self.original = self.root / initialized['context']['source']['path']
        self.map_input = self.root / 'notes/map-input.json'
        write(self.map_input, self.decisions)

    def tearDown(self):
        self.temp.cleanup()

    def compile_map(self, begin=None):
        operation = begin or workspace.begin_operation(self.root, 'compile-map', {'source_ir': self.capture / 'source_ir.json', 'input': self.map_input})
        artifact_tools.compile_map(Namespace(source=self.original, source_ir=self.capture / 'source_ir.json', input=self.map_input, output_dir=Path(operation['output_dir'])))
        return operation

    def saved_map(self):
        operation = self.compile_map()
        return workspace.commit_operation(self.root, operation['operation_id'])

    def saved_items(self, mapping=None, partial=False):
        mapping = mapping or self.saved_map()
        map_dir = Path(mapping['selected']['MAP']['output_dir'])
        decisions = item_decisions(map_dir)
        if partial:
            decisions['text_decisions'] = decisions['text_decisions'][:1]
            decisions['table_decisions'] = []
        input_path = self.root / 'notes/items-input.json'; write(input_path, decisions)
        operation = workspace.begin_operation(self.root, 'compile-items', {'map_dir': map_dir, 'input': input_path})
        artifact_tools.compile_items(Namespace(map_dir=map_dir, input=input_path, baseline_dir=None, output_dir=Path(operation['output_dir'])))
        return workspace.commit_operation(self.root, operation['operation_id'])

    def test_real_project_documents_and_same_original_second_task_do_not_merge(self):
        second = workspace.initialize_workspace(self.work, self.source, '第二文档委托', project_key='project-a', document_key='doc-b')
        third = workspace.initialize_workspace(self.work, self.source, '同一文档的独立任务', project_key='project-a', document_key='doc-a')
        self.assertEqual(len({str(self.root), second['task_root'], third['task_root']}), 3)
        self.assertEqual(second['context']['source']['sha256'], third['context']['source']['sha256'])
        self.assertNotEqual(second['context']['work_id'], third['context']['work_id'])
        project = json.loads((self.work / 'runtime-data/projects/project-a/project-index.json').read_text())
        self.assertEqual(len(project['documents']), 2)
        document = json.loads((self.root.parent.parent / 'document.json').read_text())
        self.assertEqual(len(document['tasks']), 2)
        self.assertIsNone(third['context']['document_id'])
        self.assertEqual(workspace.find_task_workspace(self.root / 'reading')[0], self.root)
        self.assertIsNone(workspace.find_task_workspace(self.base / 'fixture'))
        self.assertIn('尚无已提交工件', (self.root / 'TASK.md').read_text())

    def test_no_filename_based_document_merge_and_changed_original_refused(self):
        one = workspace.initialize_workspace(self.work, self.source, 'new one', project_key='project-a')
        two = workspace.initialize_workspace(self.work, self.source, 'new two', project_key='project-a')
        self.assertNotEqual(one['context']['document_key'], two['context']['document_key'])
        other = self.base / 'other.pdf'; other.write_bytes(b'new source bytes')
        with self.assertRaisesRegex(ValueError, 'another original'):
            workspace.initialize_workspace(self.work, other, 'changed', project_key='project-a', document_key='doc-a')
        self.original.chmod(0o600); self.original.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            workspace.find_task_workspace(self.root / 'reading')

    def test_invalid_marker_does_not_fall_back_and_source_escape_is_rejected(self):
        path = self.root / 'task-context.json'; data = json.loads(path.read_text())
        data['source']['path'] = '../outside.pdf'; data.pop('context_sha256')
        write(path, data)
        with self.assertRaisesRegex(ValueError, 'relative|inside'):
            workspace.find_task_workspace(self.root / 'reading')
        data['schema_version'] = 'unknown'; write(path, data)
        with self.assertRaisesRegex(ValueError, 'schema'):
            workspace.find_task_workspace(self.root)

    def test_map_only_recovers_without_baseline_or_export_and_can_move(self):
        saved = self.saved_map()
        self.assertEqual(saved['state'], 'COMMITTED')
        self.assertEqual(set(saved['selected']), {'MAP', 'SOURCE_FINDINGS'})
        self.assertEqual(saved['brief']['work_state'], 'MAP_SAVED')
        self.assertEqual(workspace.recover_workspace(self.root)['recovered'], [])
        self.assertNotIn('ITEMS', saved['selected'])
        moved = self.base / 'moved-task'; shutil.copytree(self.root, moved)
        status = workspace.workspace_status(moved)
        self.assertTrue(Path(status['selected']['MAP']['path']).is_relative_to(moved.resolve()))
        self.assertTrue((Path(status['selected']['MAP']['output_dir']) / 'map-source-ir.json').is_file())
        self.assertIn('人工接受：未声称', (moved / 'TASK.md').read_text())

    def test_half_bundle_is_not_saved_and_receipt_before_index_is_recovered(self):
        partial = workspace.begin_operation(self.root, 'compile-map')
        Path(partial['output_dir']).mkdir(); write(Path(partial['output_dir']) / 'document-map.json', {})
        workspace.fail_operation(self.root, partial['operation_id'], 'intentional interruption')
        self.assertEqual(workspace.recover_workspace(self.root)['selected'], {})
        operation = self.compile_map()
        with patch.object(workspace, '_write_index', side_effect=OSError('intentional index write interruption')):
            with self.assertRaises(OSError): workspace.commit_operation(self.root, operation['operation_id'])
        self.assertTrue((Path(operation['output_dir']) / 'operation-receipt.json').is_file())
        recovered = workspace.recover_workspace(self.root)
        self.assertEqual(recovered['recovered'], [{'operation_id': operation['operation_id'], 'state': 'COMMITTED'}])
        self.assertIn('MAP', recovered['selected'])
        self.assertEqual(len(recovered['incomplete_operations']), 1)

    def test_index_before_operation_descriptor_interruption_recovers_without_recommit(self):
        op = self.compile_map()
        actual = workspace._atomic_json
        def interrupt_descriptor(path, value):
            if path.parent == self.root / 'operations' and value.get('state') == 'COMMITTED':
                raise OSError('intentional interruption after index commit')
            return actual(path, value)
        with patch.object(workspace, '_atomic_json', side_effect=interrupt_descriptor):
            with self.assertRaises(OSError): workspace.commit_operation(self.root, op['operation_id'])
        index = workspace.workspace_status(self.root)['index']
        self.assertEqual(index['generation'], 1)
        recovered = workspace.recover_workspace(self.root)
        self.assertEqual(recovered['incomplete_operations'], [])
        self.assertEqual(recovered['index']['generation'], 1)
        self.assertEqual(len(recovered['committed_operations']), 1)
        self.assertEqual(recovered['recovered'][0]['action'], 'RECONCILED_OPERATION_DESCRIPTOR')

    def test_missing_replay_dependency_and_tampered_receipt_are_rejected(self):
        operation = self.compile_map()
        path = Path(operation['output_dir']); (path / 'map-source-ir.json').unlink()
        with self.assertRaisesRegex(ValueError, 'dependency'):
            workspace.commit_operation(self.root, operation['operation_id'])
        saved = self.saved_map()
        receipt = Path(saved['receipt_path']); data = json.loads(receipt.read_text()); data['files'] = {}; write(receipt, data)
        with self.assertRaisesRegex(ValueError, 'integrity'):
            workspace.workspace_status(self.root)

    def test_same_task_concurrent_commits_keep_conflict_candidate(self):
        first = self.compile_map(); second = self.compile_map()
        with ThreadPoolExecutor(2) as pool:
            values = list(pool.map(lambda op: workspace.commit_operation(self.root, op['operation_id']), [first, second]))
        self.assertEqual(sorted(value['state'] for value in values), ['CANDIDATE_CONFLICT', 'COMMITTED'])
        status = workspace.workspace_status(self.root)
        self.assertEqual(len(status['index']['committed']), 2)
        self.assertEqual(len(status['index']['candidates']), 1)
        self.assertEqual(status['index']['generation'], 1)
        self.assertEqual(workspace.commit_operation(self.root, first['operation_id'])['index']['generation'], 1)

    def test_concurrent_retry_of_same_operation_keeps_one_immutable_receipt(self):
        op = self.compile_map()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: workspace.commit_operation(self.root, op['operation_id']), range(2)))
        self.assertEqual(results[0]['receipt_sha256'], results[1]['receipt_sha256'])
        self.assertEqual(len(workspace.workspace_status(self.root)['committed_operations']), 1)

    def test_partial_items_report_remaining_and_retain_full_map_bundle(self):
        saved = self.saved_items(partial=True)
        self.assertGreater(len(saved['selected']['ITEMS']['unprocessed_scope_ids']), 0)
        self.assertIn('未处理范围', (self.root / 'TASK.md').read_text())
        mapping = next(item for item in saved['index']['committed'] if item['operation'] == 'compile-map')
        self.assertTrue((self.root / mapping['receipt_path']).is_file())

    def test_review_head_remains_authoritative_over_unclosed_candidate(self):
        saved = self.saved_items()
        baseline_dir = Path(saved['selected']['ITEMS']['output_dir'])
        op = workspace.begin_operation(self.root, 'review-open', {'baseline_dir': baseline_dir})
        revision_tools.open_review(Namespace(source=self.original, baseline_dir=baseline_dir, review_dir=Path(op['output_dir'])))
        opened = workspace.commit_operation(self.root, op['operation_id'])
        head = opened['current_working']['review_sha256']
        candidate = self.compile_map()
        workspace.commit_operation(self.root, candidate['operation_id'])
        status = workspace.workspace_status(self.root)
        self.assertEqual(status['current_working']['review_sha256'], head)
        self.assertEqual(status['selected']['MAP']['owner'], 'portable_review')
        self.assertIn('未 close', (self.root / 'TASK.md').read_text())

    def test_capture_reading_and_same_version_delivery_are_indexed(self):
        from app.application.fast_source_capture import capture_fast_source
        from reading_workspace_tools import reading_workspace
        op = workspace.begin_operation(self.root, 'capture', {'source': self.original})
        capture_fast_source(self.original, output_dir=Path(op['output_dir']), include_structure=False)
        captured = workspace.commit_operation(self.root, op['operation_id'])
        source_ir = Path(captured['selected']['READING']['path'])
        op = workspace.begin_operation(self.root, 'reading-workspace', {'source': self.original, 'source_ir': source_ir})
        reading_workspace(Namespace(source=self.original, source_ir=source_ir, output_dir=Path(op['output_dir'])))
        reading = workspace.commit_operation(self.root, op['operation_id'])
        self.assertTrue(Path(reading['selected']['READING']['manifest_path']).is_file())
        saved = self.saved_items()
        baseline_dir = Path(saved['selected']['ITEMS']['output_dir'])
        op = workspace.begin_operation(self.root, 'deliver', {'baseline_dir': baseline_dir, 'source': self.original})
        artifact_tools.deliver(Namespace(baseline_dir=baseline_dir, source=self.original, input=None, output_dir=Path(op['output_dir'])))
        delivered = workspace.commit_operation(self.root, op['operation_id'])
        self.assertEqual(delivered['selected']['DELIVERY']['baseline_sha256'], saved['selected']['ITEMS']['artifact_sha256'])
        self.assertEqual(delivered['selected']['MAP']['output_dir'], saved['selected']['MAP']['output_dir'])
        self.assertTrue((Path(delivered['selected']['MAP']['output_dir']) / 'map-source-ir.json').is_file())

    def test_two_unindexed_receipts_are_conflicts_instead_of_arbitrary_winner(self):
        first = self.compile_map(); second = self.compile_map()
        with patch.object(workspace, '_write_index', side_effect=OSError('intentional index interruption')):
            for op in (first, second):
                with self.assertRaises(OSError): workspace.commit_operation(self.root, op['operation_id'])
        recovered = workspace.recover_workspace(self.root)
        self.assertEqual(recovered['selected'], {})
        self.assertEqual([item['state'] for item in recovered['recovered']], ['CANDIDATE_CONFLICT', 'CANDIDATE_CONFLICT'])
        self.assertEqual(len(recovered['candidates']), 2)

    def test_decisions_are_frozen_and_blocked_does_not_claim_failure(self):
        op = workspace.begin_operation(self.root, 'compile-map', {'input': self.map_input})
        frozen = Path(op['resolved_inputs']['input'])
        self.assertNotEqual(frozen, self.map_input)
        before = frozen.read_bytes(); self.map_input.write_text('{}')
        self.assertEqual(frozen.read_bytes(), before)
        result = workspace.fail_operation(self.root, op['operation_id'], 'waiting for required Map confirmation', state='BLOCKED')
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertEqual(workspace.workspace_status(self.root)['selected'], {})

    def test_old_map_items_are_preserved_as_candidate(self):
        saved = self.saved_map()
        old_map = Path(saved['selected']['MAP']['output_dir'])
        # Produce another Map through the real compiler, preserving original bytes.
        changed = json.loads(self.map_input.read_text())
        changed['nodes'][0]['node_id'] = 'main-b'
        for node in changed['nodes'][1:]: node['parent_id'] = 'main-b'
        write(self.map_input, changed)
        op = self.compile_map()
        second = json.loads((Path(op['output_dir']) / 'document-map.json').read_text())
        workspace.commit_operation(self.root, op['operation_id'])
        decisions = self.root / 'notes/old-map-items.json'; write(decisions, item_decisions(old_map))
        op = workspace.begin_operation(self.root, 'compile-items', {'map_dir': old_map, 'input': decisions})
        artifact_tools.compile_items(Namespace(map_dir=old_map, input=decisions, baseline_dir=None, output_dir=Path(op['output_dir'])))
        status = workspace.commit_operation(self.root, op['operation_id'])
        self.assertEqual(status['state'], 'SAVED_CANDIDATE')
        self.assertNotIn('ITEMS', status['selected'])
        self.assertEqual(status['selected']['MAP']['artifact_sha256'], second['artifact_sha256'])
        self.assertEqual(status['candidates'][-1]['reason'], 'ITEMS_MAP_IS_NOT_CURRENT')

    def test_review_feedback_changes_owner_head_without_invalidating_open_receipt(self):
        saved = self.saved_items()
        op = workspace.begin_operation(self.root, 'review-open', {'baseline_dir': Path(saved['selected']['ITEMS']['output_dir'])})
        review_root = Path(op['output_dir'])
        revision_tools.open_review(Namespace(source=self.original, baseline_dir=Path(saved['selected']['ITEMS']['output_dir']), review_dir=review_root))
        first = workspace.commit_operation(self.root, op['operation_id'])
        head = first['current_working']['review_sha256']
        feedback = self.root / 'notes/feedback.json'
        write(feedback, {'expected_review_sha256': head, 'feedback': [{'origin': 'USER', 'source_item_ids': [], 'text': '请核对编号和原文顺序'}]})
        revision_tools.feedback_review(Namespace(review_dir=review_root, input=feedback))
        status = workspace.workspace_status(self.root)
        self.assertNotEqual(status['current_working']['review_sha256'], head)
        self.assertEqual(status['index']['committed'][-1]['operation_id'], op['operation_id'])

    def test_old_delivery_does_not_replace_newer_items_selection(self):
        partial = self.saved_items(partial=True)
        map_dir = Path(partial['selected']['MAP']['output_dir'])
        old_dir = Path(partial['selected']['ITEMS']['output_dir'])
        decisions = item_decisions(map_dir)
        covered = set(partial['selected']['ITEMS']['covered_scope_ids'])
        decisions['text_decisions'] = [x for x in decisions['text_decisions'] if x['scope_id'] not in covered]
        input_path = self.root / 'notes/growth.json'; write(input_path, decisions)
        op = workspace.begin_operation(self.root, 'compile-items', {'map_dir': map_dir, 'baseline_dir': old_dir, 'input': input_path})
        artifact_tools.compile_items(Namespace(map_dir=map_dir, baseline_dir=old_dir, input=input_path, output_dir=Path(op['output_dir'])))
        current = workspace.commit_operation(self.root, op['operation_id'])
        op = workspace.begin_operation(self.root, 'deliver', {'baseline_dir': old_dir, 'source': self.original})
        artifact_tools.deliver(Namespace(baseline_dir=old_dir, source=self.original, input=None, output_dir=Path(op['output_dir'])))
        exported = workspace.commit_operation(self.root, op['operation_id'])
        self.assertEqual(exported['state'], 'SAVED_CANDIDATE')
        self.assertEqual(exported['selected']['ITEMS']['artifact_sha256'], current['selected']['ITEMS']['artifact_sha256'])
        self.assertNotIn('DELIVERY', exported['selected'])
        self.assertEqual(exported['candidates'][-1]['reason'], 'BASELINE_IS_NOT_CURRENT')

    def test_actual_review_close_preserves_complete_map_navigation_and_new_owner(self):
        from test_review_loop import args
        saved = self.saved_items()
        complete_map = saved['selected']['MAP']['output_dir']
        base_dir = Path(saved['selected']['ITEMS']['output_dir'])
        op = workspace.begin_operation(self.root, 'review-open', {'baseline_dir': base_dir})
        review = Path(op['output_dir'])
        initial = revision_tools.open_review(args(source=self.original, baseline_dir=base_dir, review_dir=review))
        workspace.commit_operation(self.root, op['operation_id'])
        old_baseline = revision_tools.bundle(base_dir)[1]
        request = {'schema_version': 'reqpilot-review-revision/v1', 'expected_review_sha256': initial['review_sha256'], 'expected_baseline_sha256': initial['baseline_sha256'], 'model_id': 'test-explicit-decisions',
            'decisions': [{'source_record_id': old_baseline.records[0].source_record_id, 'requirementness': 'REQUIREMENT', 'requirement_kinds': ['CONSTRAINT', 'QUALITY'], 'confidence': .9, 'decision_summary': 'The complete response target specifies operating supply and timing.', 'reason_codes': ['complete_target']}]}
        revision_input = self.root / 'notes/revision.json'; write(revision_input, request)
        candidate = review / 'versions/candidate'
        revision_tools.revise_review(args(review_dir=review, input=revision_input, output_dir=candidate))
        delivery = review / 'deliveries/close'
        artifact_tools.deliver(args(baseline_dir=candidate, source=self.original, output_dir=delivery))
        closing = self.root / 'notes/close.json'; write(closing, {'expected_review_sha256': initial['review_sha256'], 'candidate_path': 'versions/candidate', 'delivery_path': 'deliveries/close'})
        revision_tools.close_review(args(review_dir=review, input=closing))
        status = workspace.workspace_status(self.root)
        self.assertEqual(status['selected']['MAP']['output_dir'], complete_map)
        self.assertEqual(Path(status['selected']['MAP']['path']).parent, candidate)
        self.assertEqual(status['selected']['MAP']['compiler_bundle_availability'], 'AVAILABLE')
        self.assertNotEqual(status['selected']['ITEMS']['artifact_sha256'], old_baseline.artifact_sha256)
        operation = workspace.begin_operation(self.root, 'deliver', {'baseline_dir': candidate, 'source': self.original})
        artifact_tools.deliver(args(baseline_dir=candidate, source=self.original, output_dir=Path(operation['output_dir'])))
        committed = workspace.commit_operation(self.root, operation['operation_id'])
        self.assertEqual(committed['state'], 'COMMITTED')
        self.assertEqual(committed['selected']['DELIVERY']['baseline_sha256'], status['selected']['ITEMS']['artifact_sha256'])
        moved = self.base / 'moved-review'; shutil.copytree(self.root, moved)
        recovered = workspace.recover_workspace(moved)
        self.assertTrue(Path(recovered['selected']['MAP']['output_dir']).is_relative_to(moved.resolve()))
        self.assertEqual(recovered['current_working']['review_sha256'], status['current_working']['review_sha256'])

    def test_imported_review_does_not_claim_complete_map_bundle(self):
        from test_review_loop import args
        saved = self.saved_items()
        imported = workspace.initialize_workspace(self.work, self.source, 'Continue an explicitly imported review.', project_key='project-a', document_key='doc-import', require_map_confirmation=False)
        root = Path(imported['task_root'])
        baseline_dir = Path(saved['selected']['ITEMS']['output_dir'])
        op = workspace.begin_operation(root, 'review-open', {'baseline_dir': baseline_dir})
        revision_tools.open_review(args(source=root / imported['context']['source']['path'], baseline_dir=baseline_dir, review_dir=Path(op['output_dir'])))
        status = workspace.commit_operation(root, op['operation_id'])
        self.assertEqual(status['selected']['MAP']['compiler_bundle_availability'], 'UNAVAILABLE')
        self.assertIsNone(status['selected']['MAP']['output_dir'])
        self.assertTrue(Path(status['selected']['MAP']['path']).is_file())

    def test_closed_review_overlays_replace_same_baseline_local_annotations_and_undo(self):
        from test_review_loop import args
        from app.application.agent_workflow.source_item_decomposition import build_source_item_decomposition_packet
        saved = self.saved_items()
        base = Path(saved['selected']['ITEMS']['output_dir'])
        mapping, baseline, _, tables = revision_tools.bundle(base)
        sid = baseline.records[0].source_record_id
        annotation_input = self.root / 'notes/initial-topic.json'
        write(annotation_input, {'schema_version': 'reqpilot-annotation-decisions/v1', 'source_sha256': baseline.source_sha256, 'baseline_sha256': baseline.artifact_sha256, 'document_map_artifact_sha256': mapping.artifact_sha256,
            'annotation_updates': [{'source_item_id': sid, 'topics': ['VERIFICATION'], 'reason': 'Initial synthetic review topic.'}]})
        op = workspace.begin_operation(self.root, 'compile-annotations', {'baseline_dir': base, 'input': annotation_input})
        artifact_tools.annotation_operation(args(baseline_dir=base, input=annotation_input, output_dir=Path(op['output_dir'])))
        state = workspace.commit_operation(self.root, op['operation_id'])
        old_annotations = Path(state['selected']['ANNOTATIONS']['path'])
        packet = build_source_item_decomposition_packet(baseline, sid, table_previews=tables)
        text = packet['segments'][0]['text']; cut = text.index('\n') + 1
        children = [{'text': text[lo:hi].strip(), 'source_ranges': [{'segment_index': 0, 'start_offset': lo, 'end_offset': hi}], 'requirement_kinds': ['QUALITY']} for lo, hi in ((0, cut), (cut, len(text)))]
        decision_input = self.root / 'notes/split.json'
        write(decision_input, {'schema_version': 'reqpilot-decomposition-decisions/v1', 'baseline_sha256': baseline.artifact_sha256,
            'authorization': {'request': 'Review this complete source scope for explicit derivation.', 'source_item_ids': [sid]},
            'decisions': [{'source_item_id': sid, 'action': 'SPLIT', 'reason': 'Two independently stated response targets.', 'children': children}]})
        op = workspace.begin_operation(self.root, 'compile-decomposition', {'baseline_dir': base, 'input': decision_input})
        artifact_tools.decomposition_operation(args(command='compile-decomposition', baseline_dir=base, input=decision_input, output_dir=Path(op['output_dir'])))
        state = workspace.commit_operation(self.root, op['operation_id']); old_decomposition = Path(state['selected']['DECOMPOSITION']['path'])
        op = workspace.begin_operation(self.root, 'review-open', {'baseline_dir': base, 'annotations_bundle': old_annotations.parent, 'decomposition_bundle': old_decomposition.parent})
        review = Path(op['output_dir'])
        head = revision_tools.open_review(args(source=self.original, baseline_dir=base, annotations=old_annotations, decomposition=old_decomposition, review_dir=review))
        workspace.commit_operation(self.root, op['operation_id'])
        undo_input = self.root / 'notes/undo.json'
        value = json.loads(decision_input.read_text()); value['decisions'] = [{'source_item_id': sid, 'action': 'UNDO', 'reason': 'Explicit synthetic user feedback retracts the derived children.'}]; write(undo_input, value)
        undo = self.root / 'outputs/explicit-undo'
        artifact_tools.decomposition_operation(args(command='compile-decomposition', baseline_dir=base, input=undo_input, previous_decomposition=old_decomposition, output_dir=undo))
        revision_input = self.root / 'notes/review-overlay-change.json'
        write(revision_input, {'schema_version': 'reqpilot-review-revision/v1', 'expected_review_sha256': head['review_sha256'], 'expected_baseline_sha256': head['baseline_sha256'],
            'annotation_updates': [{'source_item_id': sid, 'topics': [], 'reason': 'Source review retracts the prior synthetic topic.'}]})
        candidate = review / 'versions/updated-overlays'
        revision_tools.revise_review(args(review_dir=review, input=revision_input, decomposition=undo / 'source-item-decomposition.json', output_dir=candidate))
        delivery = review / 'deliveries/updated-overlays'
        artifact_tools.deliver(args(baseline_dir=candidate, source=self.original, output_dir=delivery))
        close = self.root / 'notes/close-overlays.json'; write(close, {'expected_review_sha256': head['review_sha256'], 'candidate_path': 'versions/updated-overlays', 'delivery_path': 'deliveries/updated-overlays'})
        revision_tools.close_review(args(review_dir=review, input=close))
        state = workspace.workspace_status(self.root)
        self.assertEqual(state['selected']['ITEMS']['artifact_sha256'], baseline.artifact_sha256)
        self.assertEqual(json.loads(Path(state['selected']['ANNOTATIONS']['path']).read_text())['records'][sid]['topics'], [])
        self.assertEqual(json.loads(Path(state['selected']['DECOMPOSITION']['path']).read_text())['records'], {})
        self.assertEqual(Path(state['selected']['ANNOTATIONS']['path']).parent, candidate)
        self.assertEqual(Path(state['selected']['DECOMPOSITION']['path']).parent, candidate)
        self.assertEqual(json.loads(old_annotations.read_text())['records'][sid]['topics'], ['VERIFICATION'])
        self.assertEqual(len(json.loads(old_decomposition.read_text())['records'][sid]), 2)

    def test_host_adapter_receipt_is_separate_idempotent_and_recoverable(self):
        saved = self.saved_map()
        local_receipt = Path(saved['receipt_path']).read_bytes()
        host = {'status': 'WORKBENCH_SYNCHRONIZED', 'session_id': 'plugin-' + 'a' * 32, 'revision_id': 'a' * 64}
        first = workspace.record_host_receipt(self.root, saved['operation_id'], host)
        self.assertEqual(first, workspace.record_host_receipt(self.root, saved['operation_id'], host))
        self.assertEqual(Path(saved['receipt_path']).read_bytes(), local_receipt)
        self.assertEqual(len(workspace.workspace_status(self.root)['index']['host_receipts']), 1)
        other = {'status': 'WORKBENCH_OFFLINE', 'message': 'actual adapter exception'}
        with patch.object(workspace, '_write_index', side_effect=OSError('host receipt index interruption')):
            with self.assertRaises(OSError): workspace.record_host_receipt(self.root, saved['operation_id'], other)
        recovered = workspace.recover_workspace(self.root)
        self.assertEqual(len(recovered['index']['host_receipts']), 2)
        self.assertEqual(recovered['index']['generation'], saved['index']['generation'])
        with self.assertRaisesRegex(ValueError, 'scope'):
            workspace.record_host_receipt(self.root, saved['operation_id'], {'source_sha256': '0' * 64})
        pending = workspace.begin_operation(self.root, 'compile-map')
        with self.assertRaisesRegex(ValueError, 'committed'):
            workspace.record_host_receipt(self.root, pending['operation_id'], host)

    def test_task_binding_cannot_replace_application_identity_and_preserves_custom(self):
        path = self.base / 'custom-binding.json'; write(path, {'session_id': 'one'})
        bound = workspace.bind_task_workspace(self.root, path, workspace.workspace_status(self.root)['context']['source']['sha256'], 'p', 'd')
        self.assertEqual(bound['context']['binding_portability'], 'EXTERNAL_PATH')
        with self.assertRaisesRegex(ValueError, 'identity'):
            workspace.bind_task_workspace(self.root, path, bound['context']['source']['sha256'], 'p', 'other')
        second = self.base / 'other-binding.json'; write(second, {'session_id': 'two'})
        with self.assertRaisesRegex(ValueError, 'session'):
            workspace.bind_task_workspace(self.root, second, bound['context']['source']['sha256'], 'p', 'd')


if __name__ == '__main__':
    unittest.main()
