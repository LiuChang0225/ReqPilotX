"""RX03 public task path: bounded Map correction, preserved decisions and delivery."""
from argparse import Namespace
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / 'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0] = [str(ENTRY.parent), str(ROOT / 'prototype'), str(Path(__file__).parent)]
from portable_fixtures import make_fixture, item_decisions
from manual_tools import context_for, run_manual
from revision_tools import bundle, load_head, open_review
from task_service import TaskService
from test_review_loop import args
from reqpilotx.application.agent_workflow import task_workspace as core


class Rx03ConversationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.source, _, self.mapping = make_fixture(self.directory / 'fixture')
        self.service = TaskService(self.directory, ENTRY, ROOT)
        opened = self.service.invoke('open', {'mode': 'create', 'source': str(self.source),
            'goal': '按原文整理并通过会话核对结构与交付。', 'creation_key': 'rx03-owner'})
        self.task = opened['task_id']
        self.root = Path(opened['task_root'])
        self.write('prepare')
        wrong = self.edit()
        for node in wrong['nodes']:
            if node['node_id'] == 's4':
                node['parent_id'] = 's2'
        self.write('map', decisions=wrong)
        state = self.inspect()
        decisions = item_decisions(Path(state['selected']['MAP']['output_dir']))
        for key in ('schema_version', 'source_sha256', 'document_map_artifact_sha256'):
            decisions.pop(key, None)
        self.write('items', decisions=decisions)
        self.items_dir = Path(self.inspect()['selected']['ITEMS']['output_dir'])
        self.manual = self.import_manual_decision()
        self.review = self.root / 'review'
        operation = core.begin_operation(self.root, 'review-open', {'baseline_dir': self.items_dir})
        open_review(args(review_dir=self.review, source=self.root / self.inspect()['source']['path'],
            baseline_dir=self.items_dir, manual_decisions=self.manual / 'manual-decisions.json'))
        core.commit_operation(self.root, operation['operation_id'])

    def tearDown(self):
        self.temporary.cleanup()

    def edit(self):
        value = {key: deepcopy(item) for key, item in self.mapping.items() if key != 'source_sha256'}
        value['schema_version'] = 'reqpilot-map-edit/v1'
        return value

    def import_manual_decision(self):
        context = context_for(self.items_dir)
        unit_id = next(iter(context['units']))
        user_input = self.directory / 'user-decisions.json'
        user_input.write_text(json.dumps({'schema_version': 'source-item-user-decisions/v1',
            'expected_source_sha256': context['source_sha256'],
            'expected_baseline_sha256': context['baseline_sha256'],
            'user_input_reference': 'Explicit synthetic engineer work opinion',
            'decisions': [{'unit_id': unit_id, 'decision': 'NON_COMPLIANT',
                'reason': 'Synthetic formal human decision retained across the Map correction',
                'conditions': 'Only the current interface', 'evidence_requests': 'Provide the formal drawing'}]}),
            encoding='utf-8')
        imported = self.directory / 'manual-import'
        run_manual(Namespace(command='import-manual-decisions', baseline_dir=self.items_dir,
            input=user_input, output_dir=imported, decomposition=None, annotations=None,
            previous_manual_decisions=None))
        self.manual_unit = unit_id
        return imported

    def inspect(self, **extra):
        return self.service.invoke('inspect', {'task_id': self.task, 'include_history': True, **extra})

    def write(self, action, **extra):
        revision = self.inspect()['revision']
        value = self.service.invoke(action, {'task_id': self.task, 'expected_revision': revision, **extra})
        self.assertEqual(value['operation_state'], 'COMMITTED', value)
        return value

    def test_bounded_map_correction_preserves_items_manual_decisions_and_same_version_delivery(self):
        before_state = self.inspect()
        before_head = load_head(self.review)
        before_baseline = bundle(self.review / 'versions/initial')[1]
        result = self.service.invoke('revise', {'task_id': self.task,
            'expected_revision': before_state['revision'], 'command_id': 'rx03-map-correction',
            'self_review_finding': {'text': 'Restore the actual Construction owner after review.',
                'source_item_ids': [before_baseline.records[0].source_display_id]},
            'changes': {'map_revision': {'decisions': self.edit()}}})
        self.assertEqual(result['operation_state'], 'COMMITTED', result)
        self.assertTrue(result['review_still_current'], result)
        self.assertNotEqual(result['new_review_sha256'], before_head['review_sha256'])
        candidate = Path(result['candidate'])
        after_map, after_baseline, _, _ = bundle(candidate)
        self.assertEqual(next(node for node in after_map.nodes if node.node_id == 's4').parent_id, 'main')
        self.assertEqual({record.source_record_id for record in after_baseline.records},
            {record.source_record_id for record in before_baseline.records})
        self.assertEqual([(record.source_record_id, record.text_bindings) for record in after_baseline.records[1:]],
            [(record.source_record_id, record.text_bindings) for record in before_baseline.records[1:]])
        manual = json.loads((candidate / 'manual-decisions.json').read_text())
        self.assertEqual(manual['records'][self.manual_unit]['state'], 'CURRENT')
        self.assertEqual(manual['records'][self.manual_unit]['decision'], 'NON_COMPLIANT')
        head = load_head(self.review)
        feedback = next(item for item in head['feedback'] if item['origin'] == 'AGENT_SELF_REVIEW')
        self.assertEqual(feedback['status'], 'ADDRESSED')
        self.assertEqual(result['human_acceptance'], 'NOT_CLAIMED')
        delivery = Path(result['delivery'])
        snapshot = json.loads((delivery / 'matrix-snapshot.json').read_text())
        self.assertEqual(snapshot['source_item_baseline']['artifact_sha256'], after_baseline.artifact_sha256)
        self.assertEqual(snapshot['source_item_baseline']['source_sha256'], before_baseline.source_sha256)
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        expected_snapshot = hashlib.sha256(canonical.encode()).hexdigest()
        from openpyxl import load_workbook
        with (delivery / 'matrix.xlsx').open('rb') as stream:
            workbook = load_workbook(stream, read_only=True, data_only=True)
            try:
                self.assertEqual(workbook['_ReqPilotXMeta']['B2'].value, expected_snapshot)
                self.assertEqual(workbook['_ReqPilotXMeta']['B3'].value, after_baseline.artifact_sha256)
            finally:
                workbook.close()
        self.assertEqual(head['delivery']['xlsx_sha256'], hashlib.sha256((delivery / 'matrix.xlsx').read_bytes()).hexdigest())
        repeated = self.service.invoke('deliver', {'task_id': self.task, 'expected_revision': result['revision'],
            'command_id': 'rx03-deliver-again'})
        self.assertEqual(repeated['operation_state'], 'COMMITTED', repeated)
        repeated_head = load_head(self.review)
        self.assertEqual(repeated_head['current']['baseline_sha256'], after_baseline.artifact_sha256)
        self.assertEqual(repeated_head['current']['document_map_sha256'], after_map.artifact_sha256)
        self.assertEqual(repeated_head['source']['sha256'], before_baseline.source_sha256)
        reopened = self.inspect()
        self.assertEqual(reopened['selected']['ITEMS']['artifact_sha256'], after_baseline.artifact_sha256)
        self.assertEqual(reopened['selected']['MAP']['artifact_sha256'], after_map.artifact_sha256)
        self.assertTrue(any(item['operation'] == 'review-apply' and item['state'] == 'COMMITTED'
            for item in reopened['history']))
        self.assertEqual(len(load_head(self.review)['feedback']), 1)

    def test_read_only_reads_do_not_write_and_stale_or_foreign_revision_refuses(self):
        state = self.inspect()
        item_id = bundle(self.review / 'versions/initial')[1].records[0].source_display_id
        index_bytes = (self.root / 'workspace-index.json').read_bytes()
        head_bytes = (self.review / 'workspace-manifest.json').read_bytes()
        self.service.invoke('read', {'task_id': self.task, 'view': 'item', 'source_item_id': item_id})
        self.service.invoke('read', {'task_id': self.task, 'view': 'feedback'})
        self.assertEqual((self.root / 'workspace-index.json').read_bytes(), index_bytes)
        self.assertEqual((self.review / 'workspace-manifest.json').read_bytes(), head_bytes)
        self.assertEqual(self.inspect()['revision'], state['revision'])
        result = self.service.invoke('revise', {'task_id': self.task,
            'expected_revision': state['revision'], 'command_id': 'rx03-map-first',
            'changes': {'map_revision': {'decisions': self.edit()}}})
        self.assertEqual(result['operation_state'], 'COMMITTED', result)
        current_head = (self.review / 'workspace-manifest.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'REVISION_CONFLICT') as stale:
            self.service.invoke('revise', {'task_id': self.task, 'expected_revision': state['revision'],
                'command_id': 'rx03-map-stale', 'changes': {'map_revision': {'decisions': self.edit()}}})
        self.assertEqual(stale.exception.next_action['tool'], 'task_inspect')
        self.assertEqual((self.review / 'workspace-manifest.json').read_bytes(), current_head)
        other = self.service.invoke('open', {'mode': 'create', 'source': str(self.source),
            'goal': 'Unrelated task revision must not retarget this work.', 'creation_key': 'rx03-other'})
        with self.assertRaisesRegex(ValueError, 'REVISION_CONFLICT'):
            self.service.invoke('revise', {'task_id': self.task, 'expected_revision': other['revision'],
                'command_id': 'rx03-foreign-revision', 'changes': {'map_revision': {'decisions': self.edit()}}})
        self.assertEqual((self.review / 'workspace-manifest.json').read_bytes(), current_head)
        replay = self.service.invoke('revise', {'task_id': self.task, 'expected_revision': state['revision'],
            'command_id': 'rx03-map-first', 'changes': {'map_revision': {'decisions': self.edit()}}})
        self.assertTrue(replay['replayed'], replay)
        self.assertEqual(replay['operation_id'], result['operation_id'])
        self.assertEqual((self.review / 'workspace-manifest.json').read_bytes(), current_head)


if __name__ == '__main__':
    unittest.main()
