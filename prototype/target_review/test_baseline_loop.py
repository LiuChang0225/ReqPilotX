from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import fitz

from prototype.target_review.baseline_loop import (
    apply_decisions, audit, digest, load_bundle, metadata, normalize_coordinates,
    project_tables, stage,
)
from prototype.target_review.server import ReviewStore


def fixture():
    return {'schema': 'reqpilot-target-review/v1', 'source': {},
            'pages': [{'number': 1, 'width': 200, 'height': 400}],
            'outline': [{'id': 's', 'page': 1, 'parentId': None, 'label': '1', 'title': 'Title'}],
            'lines': {'l': {'id': 'l', 'page': 1, 'text': 'Original', 'bbox': [20, 40, 80, 60]}},
            'items': [{'id': 'i', 'sectionId': 's', 'text': 'Original', 'class': 'Q', 'label': 'Label',
                       'sourceRefs': [{'page': 1, 'lineIds': ['l'], 'rects': [[20, 40, 80, 60]]}], 'derived': []}],
            'tables': [], 'issues': []}


class BaselineLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        doc = fitz.open()
        doc.new_page(width=100, height=200)
        doc.save(self.root / 'source.pdf')
        doc.close()
        self.bundle = fixture()
        self.bundle['source'] = {'sha256': hashlib.sha256((self.root/'source.pdf').read_bytes()).hexdigest(),
                                 'name': 'test.pdf', 'pageCount': 1}
        self.bundle['revision'] = digest(self.bundle)
        (self.root/'review-bundle.json').write_text(json.dumps(self.bundle))

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        return {'sourceRevision': self.bundle['revision'], 'caseId': 'test', 'scope': 'test',
                'initialNodeId': 'i', 'decisions': [{'targetId': 'i', 'expectedTextSha256': digest('Original'),
                'metadata': {'classifications': ['PROJECT']}, 'evidence': 'Source-reviewed delivery clause'}]}

    def test_coordinate_conversion_preserves_text_and_precision(self):
        bundle = deepcopy(self.bundle)
        transform = normalize_coordinates(bundle, self.root/'source.pdf')
        self.assertEqual(transform[1]['x'], .5)
        self.assertEqual(bundle['items'][0]['text'], 'Original')
        self.assertEqual(bundle['items'][0]['sourceRefs'][0]['rects'][0], [10, 20, 40, 30])
        self.assertEqual(bundle['lines']['l']['bbox'], [10, 20, 40, 30])
        self.assertFalse(audit(bundle, self.root/'source.pdf')['errors'])

    def test_decision_rejects_stale_source_text(self):
        metadata(self.bundle, self.bundle['items'][0])
        plan = self.plan()['decisions']
        plan[0]['expectedTextSha256'] = digest('Different')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            apply_decisions(self.bundle, plan)

    def test_nonrequirement_content_and_original_text_survive(self):
        metadata(self.bundle, self.bundle['items'][0])
        p = self.plan()['decisions']
        p[0]['metadata'].update(classifications=[], requirementness='NON_REQUIREMENT', normative=True,
                                 contentRole='RELATED_VERIFICATION')
        changes = apply_decisions(self.bundle, p)
        self.assertEqual(changes[0]['before']['text'], self.bundle['items'][0]['text'])
        self.assertTrue(self.bundle['items'][0]['reviewMetadata']['normative'])

    def test_source_gaps_and_wrong_line_page_are_detected(self):
        bundle = deepcopy(self.bundle)
        bundle['items'][0]['sourceRefs'][0]['lineIds'] = ['unknown']
        kinds = {e['kind'] for e in audit(bundle, self.root/'source.pdf')['errors']}
        self.assertIn('BROKEN_LINE_REF', kinds)
        self.assertIn('COORDINATE_FRAME_MISMATCH', kinds)

    def test_mixed_legacy_is_not_forced_into_product_constraints(self):
        item = self.bundle['items'][0]
        item.update(text='', **{'class': 'M'})
        self.assertEqual(metadata(self.bundle, item)['classifications'], [])
        self.assertEqual(item['reviewMetadata']['classificationStatus'], 'LEGACY_RETAINED')

    def test_staged_revision_roundtrips_with_coordinate_receipt(self):
        path = self.root/'decisions.json'
        path.write_text(json.dumps(self.plan()))
        out = self.root/'next'
        result = stage(self.root, path, out)
        store = ReviewStore(out, self.root/'qa', 'qa')
        self.assertEqual(store.source_revision, result['revision'])
        self.assertEqual(load_bundle(self.root)['items'][0]['text'], 'Original')
        self.assertEqual(result['coordinatePagesFixed'], 1)
        self.assertTrue(all('sourceOrder' in section for section in store.bundle['outline']))
        with self.assertRaisesRegex(ValueError, 'immutable'):
            stage(self.root, path, out)

    def test_new_feedback_blocks_stale_successor(self):
        feedback = self.root/'reviews.jsonl'
        feedback.write_text('new feedback')
        plan = self.plan()
        plan['feedbackInputs'] = [{'path': str(feedback), 'sha256': hashlib.sha256(b'').hexdigest()}]
        path = self.root/'decisions.json'
        path.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, 'Feedback changed'):
            stage(self.root, path, self.root/'next')
        self.assertFalse((self.root/'next').exists())

    def test_audit_detects_reading_order_regression_despite_intact_text(self):
        bundle = deepcopy(self.bundle)
        bundle['outline'].append({'id': 'late', 'page': 1, 'parentId': None,
                                  'title': 'Later chapter', 'sourceOrder': 0})
        for number in (1, 2):
            item = deepcopy(bundle['items'][0])
            item.update(id=f'late-{number}', sectionId='late')
            item['sourceRefs'] = [{'page': 1, 'rects': [[20, 100 + number * 10, 80, 110 + number * 10]]}]
            bundle['items'].append(item)
        normalize_coordinates(bundle, self.root/'source.pdf')
        for item in bundle['items']:
            metadata(bundle, item)
        bundle['ui'] = {'caseId': 'order-test', 'scope': 'synthetic', 'topics': {}, 'initialNodeId': 'i'}
        result = audit(bundle, self.root/'source.pdf')
        self.assertIn('SOURCE_ORDER_BACKTRACK', {error['kind'] for error in result['errors']})

    def test_tables_keep_cell_identity_and_whole_context(self):
        self.bundle['tables'] = [{'id': 't', 'section': 's', 'label': 'Table', 'fragments': [
            {'tableId': 't1', 'page': 1, 'bbox': [1, 1, 80, 100], 'cellsFlat': [
                {'id': 'c', 'row': 0, 'column': 0, 'columnSpan': 2, 'rowSpan': 1, 'text': 'Keep', 'bbox': [1, 1, 80, 100]}]}]}]
        project_tables(self.bundle)
        table = self.bundle['reviewTables'][0]
        self.assertEqual(table['records'], [])
        self.assertEqual(table['segments'][0]['cells'][0]['id'], 'c')
        self.assertEqual(table['segments'][0]['column_count'], 2)
        self.assertEqual(len(self.bundle['items']), 1)

    def test_whole_table_already_on_source_is_not_projected_twice(self):
        self.bundle['items'][0]['displayBlocks'] = [{'id': 'existing', 'type': 'TABLE', 'logical_table_id': 't'}]
        self.bundle['tables'] = [{'id': 't', 'section': 's', 'fragments': [
            {'tableId': 't1', 'page': 1, 'bbox': [1, 1, 80, 100], 'cellsFlat': [
                {'id': 'c', 'row': 0, 'col': 0, 'text': 'Keep', 'bbox': [1, 1, 80, 100]}]}]}]
        project_tables(self.bundle)
        self.assertNotIn('reviewTables', self.bundle)


if __name__ == '__main__':
    unittest.main()
