from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from copy import deepcopy
import unittest

from app.application.agent_workflow.source_item_workbench_metadata import numbering_findings, project_workbench_metadata
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot
from app.application.agent_workflow import source_item_decomposition as decomp
from app.application.agent_workflow import source_requirement_baseline as core
from .test_source_item_delivery_numbers import fixture as number_fixture
from .test_source_item_delivery_order import fixture as order_fixture


def map_fixture(labels=('2.6', '2.10')):
    return {'source_sha256': 'a'*64, 'artifact_sha256': 'b'*64, 'nodes': [
        {'node_id': 'root', 'parent_id': None, 'order': 0, 'kind': 'DOCUMENT_ROOT', 'label': '', 'pages': [1, 2]},
        *[{'node_id': f'n{i}', 'parent_id': 'root', 'order': i, 'kind': 'CLAUSE', 'label': label, 'pages': [i+1],
           'source_anchors': [{'page': i+1, 'bbox': [10, 10, 100, 20], 'source_bounds': [0, 0, 595, 842]}]}
          for i, label in enumerate(labels)]]}


class SourceWorkbenchMetadataTests(unittest.TestCase):
    def test_gap_is_a_located_nonblocking_lead_and_never_fills_source(self):
        mapping = map_fixture(); before = deepcopy(mapping)
        report = numbering_findings(mapping)
        self.assertEqual(len(report['findings']), 1)
        finding = report['findings'][0]
        self.assertEqual(finding['expected_number_range'], ['2.7', '2.9'])
        self.assertEqual(finding['pages'], [1, 2])
        self.assertEqual(finding['status'], 'PENDING_SOURCE_REVIEW')
        self.assertFalse(finding['blocks_progress'])
        self.assertIsNone(report['original_document_complete'])
        self.assertEqual(mapping, before)
        self.assertEqual(numbering_findings(mapping)['findings'][0]['finding_id'], finding['finding_id'])

    def test_chapter_reset_front_matter_and_non_numeric_labels_do_not_trigger_body_gap(self):
        for labels in [('2.6', '3.1'), ('2.6', '2.7'), ('Figure 1', 'Figure 3'), ('20 ms', '40 ms')]:
            self.assertEqual(numbering_findings(map_fixture(labels))['findings'], [])
        mapping = map_fixture(); mapping['nodes'][0]['kind'] = 'FRONT_MATTER'
        self.assertEqual(numbering_findings(mapping)['findings'], [])
        self.assertEqual(numbering_findings(mapping)['scan_state'], 'NOT_APPLICABLE')

    def test_repetition_is_distinct_and_source_order_is_not_lexically_sorted(self):
        repeated = numbering_findings(map_fixture(('2.6', '2.6')))['findings'][0]
        self.assertEqual(repeated['kind'], 'NUMBER_SEQUENCE_DUPLICATE')
        reverse = numbering_findings(map_fixture(('2.10', '2.6')))['findings'][0]
        self.assertEqual(reverse['kind'], 'NUMBER_SEQUENCE_REVERSED')
        self.assertEqual(reverse['observed'], '2.10 → 2.6')

    def test_numbers_and_snn_match_existing_matrix_with_full_number_evidence(self):
        mapping, baseline, split, annotations = number_fixture(body_only=True, extents=True)
        before = deepcopy((split, annotations))
        metadata = project_workbench_metadata(mapping, baseline, annotations=annotations, decomposition=split)
        matrix = build_delivery_snapshot(mapping, baseline, {}, annotations=annotations, decomposition=split)
        for node in matrix['nodes']:
            if node.get('source_item'):
                self.assertEqual(metadata['numbers'][node['source_item']['source_item_id']]['source_number'], node['source_number'])
            if node.get('decomposition'):
                projected = metadata['derived_numbers'][node['decomposition']['singular_requirement_id']]
                self.assertEqual(projected['source_number'], node['source_number'])
                self.assertEqual(projected['source_number_projection'], node['decomposition']['source_number_projection'])
        self.assertEqual((split, annotations), before)

    def test_counts_distinguish_direct_subtree_and_replacement_analysis_units(self):
        mapping, baseline, split = order_fixture()
        meta = project_workbench_metadata(mapping, baseline, decomposition=split)
        root, clause = meta['scope_summaries']['root'], meta['scope_summaries']['clause']
        self.assertEqual((root['direct_source_item_count'], root['subtree_source_item_count']), (0, 2))
        self.assertEqual((clause['direct_source_item_count'], clause['subtree_requirement_count']), (2, 2))
        self.assertEqual((root['derived_count'], root['analysis_unit_count']), (4, 4))
        unknown = project_workbench_metadata(mapping, baseline, decomposition_known=False)['scope_summaries']['root']
        self.assertIsNone(unknown['derived_count']); self.assertIsNone(unknown['analysis_unit_count'])

    def test_partial_authorized_review_persists_keep_reason_and_remaining_scope(self):
        _, baseline, _ = order_fixture(); a, b = [r.source_record_id for r in baseline.records]
        first = decomp.compile_source_item_decomposition(baseline,
            [{'source_item_id': a, 'action': 'KEEP', 'reason': 'Keep the complete paired operation for this review.'}],
            authorized_source_item_ids=[a, b])
        self.assertEqual(first['status'], 'PARTIAL')
        self.assertEqual(first['review_scope']['remaining_source_item_ids'], [b])
        self.assertEqual(first['dispositions'][a]['reason'], 'Keep the complete paired operation for this review.')
        second = decomp.compile_source_item_decomposition(baseline,
            [{'source_item_id': b, 'action': 'KEEP', 'reason': 'One complete operation.'}], previous=first, authorized_source_item_ids=[a, b])
        self.assertEqual(second['status'], 'COMPLETE')
        self.assertEqual(second['review_scope']['remaining_count'], 0)
        self.assertEqual(second['dispositions'][a], first['dispositions'][a])
        decomp.validate_source_item_decomposition(baseline, second)
        wrong = deepcopy(first); wrong['status'] = 'COMPLETE'
        payload = {k:v for k,v in wrong.items() if k not in {'artifact_id', 'artifact_sha256'}}
        digest = core.canonical_json_sha256(payload); wrong.update(artifact_sha256=digest, artifact_id='source-item-decomposition-'+digest[:32])
        with self.assertRaisesRegex(ValueError, 'falsely reports completion'):
            decomp.validate_source_item_decomposition(baseline, wrong)

    def test_undo_reason_and_legacy_artifact_compatibility(self):
        _, baseline, split = order_fixture(); sid = baseline.records[0].source_record_id
        undo = decomp.compile_source_item_decomposition(baseline, [{'source_item_id': sid, 'action': 'UNDO', 'reason': 'Return to the complete parent after source review.'}], previous=split)
        self.assertEqual(undo['dispositions'][sid]['action'], 'UNDO')
        self.assertNotIn(sid, undo['records']); self.assertIn(sid, undo['kept_source_ids'])
        self.assertEqual(len(undo['retired_singular_ids']), 2)
        legacy = {k:v for k,v in split.items() if k not in {'dispositions', 'review_scope', 'artifact_id', 'artifact_sha256'}}
        digest=core.canonical_json_sha256(legacy); legacy.update(artifact_sha256=digest, artifact_id='source-item-decomposition-'+digest[:32])
        decomp.validate_source_item_decomposition(baseline, legacy)
