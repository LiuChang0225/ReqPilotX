"""Coverage evidence is authenticated against the current Map, never guessed empty."""
from argparse import Namespace
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / 'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0] = [str(ENTRY.parent), str(ROOT / 'prototype'), str(Path(__file__).parent)]
from portable_fixtures import item_decisions, make_fixture
from revision_tools import open_review
from task_service import TaskService


class ReviewCoverageAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.source, _, mapping = make_fixture(self.directory / 'fixture')
        self.service = TaskService(self.directory, ENTRY, ROOT)
        opened = self.service.invoke('open', {'mode': 'create', 'source': str(self.source),
            'goal': 'Authenticate source coverage before review.', 'creation_key': 'coverage-auth'})
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
        self.partial_dir, self.partial_baseline = self._make_partial()
        self.complete_dir, self.complete_baseline = self._make_complete(self.partial_baseline)

    def tearDown(self):
        self.temporary.cleanup()

    def inspect(self):
        return self.service.invoke('inspect', {'task_id': self.task})

    def write(self, action, **extra):
        payload = {'task_id': self.task, 'expected_revision': self.inspect()['revision'], **extra}
        value = self.service.invoke(action, payload)
        self.assertEqual(value.get('operation_state'), 'COMMITTED', value)
        return value, payload

    def _make_partial(self):
        decision = next(item for item in self.decisions['text_decisions'] if item.get('items'))
        saved, _ = self.write('items', command_id='coverage-partial',
                              decisions={'text_decisions': [decision]})
        baseline = json.loads(Path(saved['selected']['ITEMS']['path']).read_text())
        self.assertEqual(len(baseline['records']), 1)
        self.assertGreater(len(saved['selected']['ITEMS']['unprocessed_scope_ids']), 0)
        return Path(saved['selected']['ITEMS']['output_dir']), baseline

    def _make_complete(self, partial_baseline):
        covered = set(partial_baseline['covered_scope_ids'])
        remaining = deepcopy(self.decisions)
        remaining['text_decisions'] = [item for item in self.decisions['text_decisions']
                                       if item['scope_id'] not in covered]
        remaining['table_decisions'] = [item for item in self.decisions['table_decisions']
                                        if item['logical_table_scope_id'] not in covered]
        saved, _ = self.write('items', command_id='coverage-complete', decisions=remaining)
        baseline = json.loads(Path(saved['selected']['ITEMS']['path']).read_text())
        self.assertFalse(saved['selected']['ITEMS'].get('unprocessed_scope_ids'))
        return Path(saved['selected']['ITEMS']['output_dir']), baseline

    def _copy(self, source_dir, name):
        target = self.directory / name
        shutil.copytree(source_dir, target)
        return target

    def _mutate(self, path, change):
        value = json.loads(path.read_text(encoding='utf-8'))
        change(value)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def _reject(self, baseline_dir, name, code):
        review_dir = self.directory / name
        with self.assertRaisesRegex(ValueError, code):
            open_review(Namespace(review_dir=review_dir, baseline_dir=baseline_dir, source=self.source))
        self.assertFalse(review_dir.exists())
        return review_dir

    def test_missing_evidence_is_proven_from_current_map_or_rejected(self):
        bundle = self._copy(self.partial_dir, 'partial-no-evidence')
        (bundle / 'item-compilation-receipt.json').unlink()
        (bundle / 'scope-requests.json').unlink()
        self._reject(bundle, 'partial-no-evidence-review', 'ITEMS_INCOMPLETE')

        legacy = self._copy(self.complete_dir, 'complete-legacy')
        (legacy / 'item-compilation-receipt.json').unlink()
        (legacy / 'scope-requests.json').unlink()
        review_dir = self.directory / 'legacy-review'
        open_review(Namespace(review_dir=review_dir, baseline_dir=legacy, source=self.source))
        head = json.loads((review_dir / 'workspace-manifest.json').read_text())
        saved = json.loads((review_dir / head['current']['version_path']
                            / 'source-requirement-baseline.json').read_text())
        self.assertEqual(len(saved['records']), len(self.complete_baseline['records']))

    def test_foreign_malformed_or_inconsistent_scope_requests_fail_closed(self):
        foreign = self._copy(self.partial_dir, 'partial-foreign-scope')
        self._mutate(foreign / 'scope-requests.json',
                     lambda value: value['text_requests'][0].update(source_sha256='f' * 64))
        self._reject(foreign, 'partial-foreign-scope-review', 'ITEMS_COVERAGE_UNVERIFIED')

        malformed = self._copy(self.complete_dir, 'complete-malformed-scope')
        self._mutate(malformed / 'scope-requests.json',
                     lambda value: value.update(text_requests={}))
        self._reject(malformed, 'complete-malformed-scope-review', 'ITEMS_COVERAGE_UNVERIFIED')

        wrong_map = self._copy(self.complete_dir, 'complete-wrong-scope-map')
        self._mutate(wrong_map / 'scope-requests.json',
                     lambda value: value['table_requests'][0].update(
                         document_map_artifact_sha256='f' * 64))
        self._reject(wrong_map, 'complete-wrong-scope-map-review', 'ITEMS_COVERAGE_UNVERIFIED')

    def test_foreign_or_malformed_receipt_fails_closed(self):
        foreign = self._copy(self.complete_dir, 'complete-foreign-receipt')
        self._mutate(foreign / 'item-compilation-receipt.json',
                     lambda value: value.update(source_sha256='f' * 64, unprocessed_scope_ids=[]))
        self._reject(foreign, 'complete-foreign-receipt-review', 'ITEMS_COVERAGE_UNVERIFIED')

        malformed = self._copy(self.complete_dir, 'complete-malformed-receipt')
        self._mutate(malformed / 'item-compilation-receipt.json',
                     lambda value: value.update(unprocessed_scope_ids='not-a-list'))
        self._reject(malformed, 'complete-malformed-receipt-review', 'ITEMS_COVERAGE_UNVERIFIED')

        wrong_id = self._copy(self.complete_dir, 'complete-wrong-receipt-id')
        self._mutate(wrong_id / 'item-compilation-receipt.json',
                     lambda value: value.update(baseline_artifact_id='source-requirement-baseline-' + '0' * 32))
        self._reject(wrong_id, 'complete-wrong-receipt-id-review', 'ITEMS_COVERAGE_UNVERIFIED')

    def test_complete_receipt_cannot_hide_current_missing_scopes(self):
        hidden = self._copy(self.partial_dir, 'partial-hidden-gap')
        self._mutate(hidden / 'item-compilation-receipt.json',
                     lambda value: value.update(unprocessed_scope_ids=[]))
        self._reject(hidden, 'partial-hidden-gap-review', 'ITEMS_INCOMPLETE')

    def test_public_cli_review_open_fails_closed_and_complete_path_succeeds(self):
        env = os.environ.copy()
        engine = str(ROOT / 'packages/engine/src')
        env['PYTHONPATH'] = engine + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')

        def invoke(baseline_dir):
            process = subprocess.run([sys.executable, str(ENTRY), '--project-root', str(ROOT),
                'workspace', 'run', 'review-open', '--workspace', str(self.root),
                '--baseline-dir', str(baseline_dir), '--source', str(self.source)],
                cwd=ROOT, env=env, text=True, capture_output=True)
            payload = None
            for stream in (process.stdout, process.stderr):
                start = stream.find('{')
                if start < 0:
                    continue
                try:
                    payload, _ = json.JSONDecoder().raw_decode(stream[start:])
                except ValueError:
                    payload = None
                if payload is not None:
                    break
            return process, payload

        partial = self._copy(self.partial_dir, 'cli-partial')
        process, payload = invoke(partial)
        self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn('ITEMS_INCOMPLETE', json.dumps(payload, ensure_ascii=False))
        self.assertFalse((self.root / 'review' / 'workspace-manifest.json').exists())

        complete = self._copy(self.complete_dir, 'cli-complete')
        process, payload = invoke(complete)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn(payload.get('status'), {'WORKSPACE_OPERATION_SAVED', 'WORKSPACE_OPERATION_REPLAYED'}, payload)
        head = json.loads((self.root / 'review' / 'workspace-manifest.json').read_text())
        saved = json.loads((self.root / 'review' / head['current']['version_path']
                            / 'source-requirement-baseline.json').read_text())
        self.assertEqual(len(saved['records']), len(self.complete_baseline['records']))


if __name__ == '__main__':
    unittest.main()
