"""A review cannot open before all original source scopes are processed."""
from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / 'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0] = [str(ENTRY.parent), str(ROOT / 'prototype'), str(Path(__file__).parent)]
from portable_fixtures import item_decisions, make_fixture
from task_service import TaskService


class ReviewEmptyBaselineGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.source, _, mapping = make_fixture(self.directory / 'fixture')
        self.service = TaskService(self.directory, ENTRY, ROOT)
        opened = self.service.invoke('open', {'mode': 'create', 'source': str(self.source),
            'goal': 'Keep every original source scope before preparing the working review.',
            'creation_key': 'empty-review-guard'})
        self.task = opened['task_id']
        self.root = Path(opened['task_root'])
        self.write('prepare', mode='capture')
        edit = {key: value for key, value in mapping.items() if key != 'source_sha256'}
        edit['schema_version'] = 'reqpilot-map-edit/v1'
        mapped, _ = self.write('map', decisions=edit)
        self.map_dir = Path(mapped['selected']['MAP']['output_dir'])
        self.decisions = item_decisions(self.map_dir)
        for key in ('source_sha256', 'document_map_artifact_sha256'):
            self.decisions.pop(key, None)

    def tearDown(self):
        self.temporary.cleanup()

    def inspect(self):
        return self.service.invoke('inspect', {'task_id': self.task})

    def write(self, action, **extra):
        payload = {'task_id': self.task, 'expected_revision': self.inspect()['revision'], **extra}
        value = self.service.invoke(action, payload)
        self.assertEqual(value.get('operation_state'), 'COMMITTED', value)
        return value, payload

    def retain_structure_only(self):
        retained, _ = self.write('items', command_id='retain-structure-only',
                                 decisions={'retain_map_structure': True})
        baseline = json.loads(Path(retained['selected']['ITEMS']['path']).read_text())
        receipt = json.loads((Path(retained['selected']['ITEMS']['path']).parent
                              / 'item-compilation-receipt.json').read_text())
        return retained, baseline, receipt

    def remaining_decisions(self, baseline):
        covered = set(baseline['covered_scope_ids'])
        remaining = deepcopy(self.decisions)
        remaining['text_decisions'] = [item for item in self.decisions['text_decisions']
                                       if item['scope_id'] not in covered]
        remaining['table_decisions'] = [item for item in self.decisions['table_decisions']
                                        if item['logical_table_scope_id'] not in covered]
        return remaining

    def test_prepare_rejects_unprocessed_items_and_safe_completion_succeeds(self):
        retained, baseline, receipt = self.retain_structure_only()
        self.assertEqual(baseline['records'], [])
        self.assertTrue(receipt['unprocessed_scope_ids'])

        index_before = (self.root / 'workspace-index.json').read_bytes()
        selected_before = self.inspect()['selected']['ITEMS']
        with self.assertRaisesRegex(ValueError, 'ITEMS_INCOMPLETE'):
            self.service.invoke('review', {'task_id': self.task,
                'expected_revision': self.inspect()['revision'],
                'analysis_policy': 'skip_no_internal_evidence'})
        self.assertFalse((self.root / 'review' / 'workspace-manifest.json').exists())
        self.assertEqual((self.root / 'workspace-index.json').read_bytes(), index_before)
        self.assertEqual(self.inspect()['selected']['ITEMS'], selected_before)
        self.assertEqual(self.inspect()['current_owner'], 'task_workspace')

        from revision_tools import open_review
        with self.assertRaisesRegex(ValueError, 'ITEMS_INCOMPLETE'):
            open_review(Namespace(review_dir=self.root / 'direct-review', source=self.source,
                baseline_dir=Path(retained['selected']['ITEMS']['output_dir'])))
        self.assertFalse((self.root / 'direct-review').exists())

        # A missing receipt must not turn the same baseline into "complete":
        # the saved Map scope requests are the fallback coverage source.
        import shutil
        from revision_tools import bundle, require_complete_items_for_review
        no_receipt = self.directory / 'baseline-without-receipt'
        shutil.copytree(Path(retained['selected']['ITEMS']['output_dir']), no_receipt)
        (no_receipt / 'item-compilation-receipt.json').unlink()
        document_map, baseline_object, _, tables = bundle(no_receipt)
        with self.assertRaisesRegex(ValueError, 'ITEMS_INCOMPLETE'):
            require_complete_items_for_review(no_receipt, baseline_object, document_map, tables)

        completed, _ = self.write('items', command_id='finish-remaining-before-review',
                                  decisions=self.remaining_decisions(baseline))
        completed_baseline = json.loads(Path(completed['selected']['ITEMS']['path']).read_text())
        completed_receipt = json.loads((Path(completed['selected']['ITEMS']['path']).parent
                                        / 'item-compilation-receipt.json').read_text())
        self.assertTrue(completed_baseline['records'])
        self.assertEqual(completed_receipt['unprocessed_scope_ids'], [])

        prepared, _ = self.write('review', analysis_policy='skip_no_internal_evidence')
        self.assertEqual(prepared.get('operation_state'), 'COMMITTED', prepared)
        head = json.loads((self.root / 'review' / 'workspace-manifest.json').read_text())
        current = self.root / 'review' / head['current']['version_path']
        saved = json.loads((current / 'source-requirement-baseline.json').read_text())
        saved_receipt = json.loads((current / 'item-compilation-receipt.json').read_text())
        self.assertEqual(head['current']['baseline_sha256'], completed['selected']['ITEMS']['artifact_sha256'])
        self.assertTrue(saved['records'])
        self.assertEqual(saved_receipt['unprocessed_scope_ids'], [])
        self.assertEqual(self.inspect()['current_owner'], 'portable_review')

    def test_items_after_review_is_rejected_before_creating_a_candidate(self):
        retained, baseline, receipt = self.retain_structure_only()
        self.assertTrue(receipt['unprocessed_scope_ids'])
        # A complete review is the safe source path; this test isolates only the
        # late-items call so no current public route can produce an unadopted bundle.
        completed, _ = self.write('items', command_id='finish-before-review',
                                  decisions=self.remaining_decisions(baseline))
        prepared, _ = self.write('review', analysis_policy='skip_no_internal_evidence')
        self.assertEqual(prepared.get('operation_state'), 'COMMITTED', prepared)
        head_before = (self.root / 'review' / 'workspace-manifest.json').read_bytes()
        index_before = (self.root / 'workspace-index.json').read_bytes()
        outputs_before = set((self.root / 'outputs').glob('op-*')) if (self.root / 'outputs').is_dir() else set()

        with self.assertRaisesRegex(ValueError, 'ITEMS_REVIEW_OWNED'):
            self.service.invoke('items', {'task_id': self.task,
                'expected_revision': self.inspect()['revision'], 'command_id': 'late-new-scopes',
                'decisions': self.decisions})
        self.assertEqual((self.root / 'review' / 'workspace-manifest.json').read_bytes(), head_before)
        self.assertEqual((self.root / 'workspace-index.json').read_bytes(), index_before)
        outputs_after = set((self.root / 'outputs').glob('op-*')) if (self.root / 'outputs').is_dir() else set()
        self.assertEqual(outputs_after, outputs_before)
        self.assertEqual(self.inspect()['selected']['ITEMS']['artifact_sha256'],
                         completed['selected']['ITEMS']['artifact_sha256'])


if __name__ == '__main__':
    unittest.main()
