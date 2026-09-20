"""Typed service changes run real compilers/review/export on synthetic sources."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / 'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0] = [str(ENTRY.parent), str(ROOT / 'prototype'), str(Path(__file__).parent)]
from test_portable import make_fixture, item_decisions
from task_service import TaskService


class TaskChangeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(); self.directory = Path(self.temporary.name)
        source, _, mapping = make_fixture(self.directory / 'fixture')
        self.service = TaskService(self.directory, ENTRY, ROOT)
        opened = self.service.invoke('open', {'mode': 'create', 'source': str(source),
            'goal': '按原文整理、核对分类和原文形式，允许核对拆分粒度并KEEP完整目标；自主交付，无内部依据则跳过符合性分析。',
            'creation_key': 'typed-change-integration'})
        self.task = opened['task_id']; self.root = Path(opened['task_root'])
        self.write('prepare', mode='capture')
        edit = {key: value for key, value in mapping.items() if key != 'source_sha256'}
        edit['schema_version'] = 'reqpilot-map-edit/v1'
        mapped, _ = self.write('map', decisions=edit)
        decisions = item_decisions(Path(mapped['selected']['MAP']['output_dir']))
        for key in ('source_sha256', 'document_map_artifact_sha256'): decisions.pop(key)
        self.write('items', decisions=decisions)

    def tearDown(self): self.temporary.cleanup()

    def inspect(self): return self.service.invoke('inspect', {'task_id': self.task, 'include_history': True})

    def write(self, action, **extra):
        payload = {'task_id': self.task, 'expected_revision': self.inspect()['revision'], **extra}
        value = self.service.invoke(action, payload)
        self.assertEqual(value['operation_state'], 'COMMITTED', value)
        return value, payload

    def baseline(self):
        path = self.inspect()['selected']['ITEMS']['path']
        return json.loads(Path(path).read_text())

    def assert_same_version_delivery(self, result):
        from openpyxl import load_workbook
        folder = Path(result['delivery'])
        snapshot = json.loads((folder / 'matrix-snapshot.json').read_text())
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        expected = hashlib.sha256(canonical.encode()).hexdigest()
        with (folder / 'matrix.xlsx').open('rb') as stream:
            workbook = load_workbook(stream, read_only=True, data_only=True)
            try:
                self.assertEqual(workbook['_ReqPilotXMeta']['B2'].value, expected)
                self.assertEqual(workbook['_ReqPilotXMeta']['B3'].value, self.baseline()['artifact_sha256'])
            finally: workbook.close()
        head = json.loads((self.root / 'review/workspace-manifest.json').read_text())
        self.assertEqual(head['review_sha256'], result['new_review_sha256'])
        self.assertEqual(head['delivery']['xlsx_sha256'], hashlib.sha256((folder / 'matrix.xlsx').read_bytes()).hexdigest())
        self.assertEqual(snapshot['source_item_baseline']['artifact_sha256'], self.baseline()['artifact_sha256'])
        check = self.service.invoke('check', {'task_id': self.task})
        self.assertTrue(check['review_verified']); self.assertTrue(check['current_delivery_registered'])
        self.assertEqual(check['workbench_status'], 'NOT_BOUND')
        self.assertFalse(result['handoff_ready'])

    def test_review_classification_then_scope_self_finding_delivers_and_replays(self):
        self.write('review', analysis_policy='skip_no_internal_evidence')
        before = self.baseline(); first = before['records'][0]
        classified, class_request = self.write('revise', command_id='typed-classification', changes={
            'model_id': 'synthetic-service-test-not-model-run',
            'decisions': [{'source_record_id': first['source_display_id'], 'requirementness': 'REQUIREMENT',
                'requirement_kinds': ['QUALITY', 'CONSTRAINT'], 'confidence': 0.9,
                'decision_summary': 'The original target includes timing and its voltage condition.', 'reason_codes': ['complete_source_target']}]})
        self.assert_same_version_delivery(classified)
        classified_baseline = self.baseline()
        self.assertEqual(classified_baseline['records'][0]['source_record_id'], first['source_record_id'])
        self.assertEqual(classified_baseline['records'][1:], before['records'][1:])
        self.assertEqual(classified_baseline['records'][0]['requirement_kinds'], ['CONSTRAINT', 'QUALITY'])
        detail = self.service.invoke('read', {'task_id': self.task, 'view': 'item', 'source_item_id': first['source_display_id'], 'part': 'scope', 'max_chars': 24000})
        self.assertTrue(detail['complete'], detail)
        template = detail['data']['candidate_template']
        candidate = {key: deepcopy(value) for key, value in template.items() if key in {'scope_id', 'items', 'structure_only_spans', 'diagnostic_codes'}}
        candidate['items'][0]['source_form'] = 'PARAGRAPH'
        scoped, scope_request = self.write('revise', command_id='typed-scope-self-finding',
            self_review_finding={'text': 'Correct the synthetic target source form while preserving its full original text.',
                'source_item_ids': [first['source_display_id']], 'rationale': 'Explicit Agent self-review finding for the synthetic service test.'},
            changes={'scope_revision': {'affected_scope_ids': [candidate['scope_id']], 'text_decisions': [candidate]}})
        self.assert_same_version_delivery(scoped)
        after = self.baseline()
        self.assertEqual(after['records'][0]['source_form'], 'PARAGRAPH')
        self.assertEqual(after['records'][0]['source_record_id'], first['source_record_id'])
        self.assertEqual(after['records'][0]['text_bindings'], before['records'][0]['text_bindings'])
        feedback = self.service.invoke('read', {'task_id': self.task, 'view': 'feedback'})
        self.assertEqual(len(feedback['feedback']), 1)
        self.assertEqual(feedback['feedback'][0]['origin'], 'AGENT_SELF_REVIEW')
        self.assertEqual(feedback['feedback'][0]['status'], 'ADDRESSED')
        initial_state = self.inspect()
        with patch('task_changes.bind_existing_item_changes', side_effect=AssertionError('replay must use frozen decisions')), patch('revision_tools.revise_review', side_effect=AssertionError('no recompilation')):
            replay = self.service.invoke('revise', scope_request)
            self.assertTrue(replay['replayed']); self.assertTrue(replay['review_still_current'])
            self.assertEqual(replay['operation_id'], scoped['operation_id'])
            historical = self.service.invoke('revise', class_request)
            self.assertTrue(historical['historical_success']); self.assertFalse(historical['review_still_current'])
            self.assertEqual(historical['operation_id'], classified['operation_id'])
        final_state = self.inspect()
        self.assertEqual(final_state['revision'], initial_state['revision'])
        self.assertEqual(final_state['history_total'], initial_state['history_total'])
        self.assertEqual(len(json.loads((self.root / 'review/workspace-manifest.json').read_text())['feedback']), 1)

    def test_initial_annotation_and_decomposition_replay_keep_original_route_after_review(self):
        first = self.baseline()['records'][0]
        annotations, annotation_request = self.write('annotations', command_id='initial-annotation', changes={
            'annotation_updates': [{'source_item_id': first['source_display_id'], 'topics': ['VERIFICATION'],
                                   'reason': 'Explicit verification topic for the synthetic source.'}]})
        decomposition, decomposition_request = self.write('decomposition', command_id='initial-decomposition', changes={
            'authorization': {'source_item_ids': [first['source_display_id']]},
            'decisions': [{'source_item_id': first['source_display_id'], 'action': 'KEEP', 'reason': 'Retain one complete source target.'}]})
        self.write('review', analysis_policy='skip_no_internal_evidence')
        current = self.inspect(); head = (self.root / 'review/workspace-manifest.json').read_bytes()
        index = (self.root / 'workspace-index.json').read_bytes()
        with patch('task_review.apply_review', side_effect=AssertionError('initial replay must not become a review mutation')), patch('task_changes.compiler_decisions', side_effect=AssertionError('initial replay must not rebind decisions')):
            annotation_replay = self.service.invoke('annotations', annotation_request)
            decomposition_replay = self.service.invoke('decomposition', decomposition_request)
        for original, replay, operation in ((annotations, annotation_replay, 'compile-annotations'), (decomposition, decomposition_replay, 'compile-decomposition')):
            self.assertTrue(replay['replayed']); self.assertEqual(replay['operation_state'], 'COMMITTED')
            self.assertEqual(replay['operation_id'], original['operation_id'])
            descriptor = json.loads((self.root / 'operations' / (original['operation_id'] + '.json')).read_text())
            self.assertEqual(descriptor['operation'], operation)
        self.assertEqual((self.root / 'review/workspace-manifest.json').read_bytes(), head)
        self.assertEqual((self.root / 'workspace-index.json').read_bytes(), index)
        after = self.inspect()
        self.assertEqual(after['revision'], current['revision']); self.assertEqual(after['history_total'], current['history_total'])
        self.assertIn('ANNOTATIONS', after['selected']); self.assertIn('DECOMPOSITION', after['selected'])

    def test_user_feedback_can_drive_annotation_and_decomposition_with_actual_resolution_ids(self):
        from argparse import Namespace
        from revision_tools import feedback_review,load_head
        self.write('review',analysis_policy='skip_no_internal_evidence')
        first=self.baseline()['records'][0]
        for action,changes in [('annotations',{'annotation_updates':[{'source_item_id':first['source_display_id'],
            'topics':['VERIFICATION'],'reason':'An actual recorded review asks for this topic.'}]}),
            ('decomposition',{'authorization':{'source_item_ids':[first['source_display_id']]},'decisions':[{
                'source_item_id':first['source_display_id'],'action':'KEEP','reason':'The complete source target is retained for analysis.'}]})]:
            review=self.root/'review';head=load_head(review)
            input_path=self.directory/(action+'-feedback.json')
            input_path.write_text(json.dumps({'schema_version':'reqpilot-review-feedback/v1','expected_review_sha256':head['review_sha256'],
                'feedback':[{'origin':'USER','text':'Synthetic fixture asks for '+action,'source_item_ids':[first['source_record_id']]}]}))
            saved=feedback_review(Namespace(review_dir=review,input=input_path))
            fid=saved['added_feedback_ids'][0]
            result,_=self.write(action,changes=changes,origin='USER',resolutions=[{'feedback_id':fid,'disposition':'ADDRESSED',
                'reason':'Applied the explicit synthetic feedback to the current source.'}])
            self.assert_same_version_delivery(result)
            self.assertEqual(next(f for f in load_head(review)['feedback'] if f['feedback_id']==fid)['status'],'ADDRESSED')

    def test_initial_finding_returns_an_executable_review_step_without_mutating_state(self):
        from task_bridge import TaskBridge
        bridge=TaskBridge(self.directory,ENTRY,project_root=ROOT)
        first=self.baseline()['records'][0];before=self.inspect()
        request={'task_id':self.task,'expected_revision':before['revision'],'command_id':'finding-with-initial-annotation',
            'self_review_finding':{'text':'The source target needs its verification topic recorded.','source_item_ids':[first['source_display_id']]},
            'changes':{'annotation_updates':[{'source_item_id':first['source_display_id'],'topics':['VERIFICATION'],'reason':'The source has an explicit measured target.'}]}}
        rejected=bridge.invoke('annotations',request)
        self.assertEqual(rejected['error_code'],'REVIEW_REQUIRED',rejected)
        self.assertEqual(self.inspect()['revision'],before['revision'])
        self.assertEqual(rejected['next_action']['tool'],'review_prepare')
        ready=bridge.invoke('review',rejected['next_action']['request'])
        self.assertEqual(ready['operation_state'],'COMMITTED',ready)
        applied=bridge.invoke('annotations',{**request,'expected_revision':ready['revision']})
        self.assertEqual(applied['operation_state'],'COMMITTED',applied)
        self.assert_same_version_delivery(applied)

    def test_imported_review_is_readable_without_a_map_compiler_directory(self):
        from app.application.agent_workflow import task_workspace as core
        from revision_tools import open_review
        from test_review_loop import args
        current=self.inspect();baseline_dir=Path(current['selected']['ITEMS']['output_dir'])
        source=self.root/current['source']['path']
        opened=self.service.invoke('open',{'mode':'create','source':str(source),'goal':'Read this existing source-bound review.','creation_key':'import-review'})
        root=Path(opened['task_root'])
        operation=core.begin_operation(root,'review-open',{'baseline_dir':baseline_dir})
        open_review(args(review_dir=root/'review',source=root/opened['source']['path'],baseline_dir=baseline_dir))
        core.commit_operation(root,operation['operation_id'])
        _,state=self.service.state(opened['task_id'])
        self.assertIsNone(state['selected']['MAP']['output_dir'])
        scopes=json.loads((Path(state['selected']['MAP']['read_bundle_dir'])/'scope-requests.json').read_text())
        scope=scopes['table_requests'][0]['logical_table_scope_id']
        description=self.service.invoke('describe',{'task_id':opened['task_id'],'action':'items','scope_id':scope,'part':'examples'})
        self.assertEqual(description['status'],'DESCRIBED')
        read=self.service.invoke('read',{'task_id':opened['task_id'],'view':'scope','scope_id':scope,'part':'template'})
        self.assertEqual(read['data']['scope_id'],scope)
        detail=self.service.invoke('read',{'task_id':opened['task_id'],'view':'item','source_item_id':'SRC-0001'})
        self.assertTrue(detail['source'])


if __name__ == '__main__': unittest.main()
