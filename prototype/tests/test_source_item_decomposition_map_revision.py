from __future__ import annotations

from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from copy import deepcopy
import unittest

from app.application.agent_workflow import source_item_decomposition as decomp
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.document_map_revision import (
    compile_document_map_revision, rebind_baseline_for_document_map_revision,
)
from .test_document_map_revision import fixture


def seal(value):
    value = {k: v for k, v in value.items() if k not in {'artifact_id', 'artifact_sha256'}}
    sha = core.canonical_json_sha256(value)
    return {**value, 'artifact_id': 'source-item-decomposition-' + sha[:32], 'artifact_sha256': sha}


def seal_receipt(value):
    value = {k: v for k, v in value.items() if k != 'receipt_sha256'}
    return {**value, 'receipt_sha256': core.canonical_json_sha256(value)}


def table_fixture():
    reference, _, _, _, original_previews = fixture()
    preview = deepcopy(original_previews['lt-torque'])
    fragment = preview['fragments'][0]
    fragment.update(row_range=[0, 3], row_count=3, column_count=1, bbox=[10.0, 180.0, 290.0, 240.0],
        cells=[{'cell_id': f'operation-{index}', 'row': index, 'column': 0, 'row_span': 1, 'column_span': 1,
                'text': text, 'bbox': [10.0, float(180 + index * 20), 290.0, float(200 + index * 20)]}
               for index, text in enumerate(['Operation', 'Open the valve.', 'Close the valve.'])])
    reference['logical_tables'][0]['fragments'][0]['row_range'] = [0, 3]
    anchor = next(a for a in reference['document_tree']['nodes'][-1]['source_anchors'] if a.get('table_token'))
    anchor.update(row_range=[0, 3], bbox=[10.0, 180.0, 290.0, 240.0])
    document_map = core.compile_document_map_artifact(reference)
    request = core.build_source_requirement_scope_request(document_map, preview)
    candidate = core.SourceRequirementSliceCandidateV1(source_sha256=request.source_sha256,
        document_map_artifact_id=document_map.artifact_id, document_map_artifact_sha256=document_map.artifact_sha256,
        request_sha256=request.request_sha256, logical_table_scope_id='lt-torque',
        records=(core.SourceRequirementBoundaryCandidateV1(record_key='valve-sequence', label='Valve sequence', source_form='TABLE_BRANCH',
            row_groups=(core.SourceRequirementRowGroupV1(fragment_id=fragment['fragment_id'], row_start=1, row_end=3),),
            requirementness='REQUIREMENT', requirement_kinds=('FUNCTIONAL',), confidence=0.95,
            decision_summary='Separate operating commands in a complete sequence.', reason_codes=('source_review',)),),
        context_row_groups=(core.SourceRequirementContextRowGroupV1(role='HEADER', reason_codes=('shared_table_header',),
            row_group=core.SourceRequirementRowGroupV1(fragment_id=fragment['fragment_id'], row_start=0, row_end=1)),))
    baseline, validation = core.compile_source_requirement_baseline(document_map=document_map, requests=(request,), candidates=(candidate,))
    previews = {'lt-torque': preview}
    parent = baseline.records[0].source_record_id
    packet = decomp.build_source_item_decomposition_packet(baseline, parent, table_previews=previews)
    decision = {'source_item_id': parent, 'action': 'SPLIT', 'reason': 'Separate independently observable valve commands',
        'children': [{'text': s['text'], 'source_ranges': [{'segment_index': s['segment_index'], 'start_offset': 0, 'end_offset': len(s['text'])}]} for s in packet['segments']]}
    artifact = decomp.compile_source_item_decomposition(baseline, [decision], table_previews=previews)
    artifact['reserved_display_ids'] = [{'id': 'REQ-0001-S09', 'historical_id': 'legacy-withdrawn', 'reason': 'Never reissue'}]
    return reference, document_map, baseline, validation, previews, seal(artifact)


class DecompositionMapRevisionTests(unittest.TestCase):
    def setUp(self):
        self.reference, self.map, self.base, self.validation, self.previews, self.artifact = table_fixture()
        self.sid = self.base.records[0].source_record_id

    def revise(self, edit):
        reference = deepcopy(self.reference)
        edit(reference)
        revised = compile_document_map_revision(self.map, core.compile_document_map_artifact(reference))
        return rebind_baseline_for_document_map_revision(self.map, revised['document_map'], self.base, self.validation, self.previews)

    def rename(self):
        def change(reference):
            group = reference['document_tree']['nodes'][-1]
            group['node_id'] = 'renamed-section'
            group['content_blocks'][0]['logical_table_scope_id'] = 'renamed-table'
            next(a for a in group['source_anchors'] if a.get('table_token'))['logical_table_scope_id'] = 'renamed-table'
            reference['logical_tables'][0]['scope_id'] = 'renamed-table'
        return self.revise(change)

    def carry(self, revision, **overrides):
        kwargs = {'table_previews': revision['table_previews'], 'previous_table_previews': self.previews,
                  'map_revision_receipt': revision['receipt']}
        kwargs.update(overrides)
        return decomp.rebind_source_item_decomposition(self.base, revision['baseline'], self.artifact, **kwargs)

    def test_pure_node_and_table_scope_names_keep_exact_children_and_reserved_ids(self):
        revision = self.rename()
        old = deepcopy((self.artifact, self.previews, revision))
        result = self.carry(revision)
        self.assertEqual(result['stale_parent_ids'], [])
        self.assertEqual(result['artifact']['records'], self.artifact['records'])
        self.assertEqual(result['artifact']['identity_registry'], self.artifact['identity_registry'])
        self.assertEqual(result['artifact']['reserved_display_ids'], self.artifact['reserved_display_ids'])
        self.assertFalse(result['artifact']['human_accepted'])
        self.assertEqual(result['artifact']['carry_forward']['map_revision_receipt_sha256'], revision['receipt']['receipt_sha256'])
        decomp.validate_source_item_decomposition(revision['baseline'], result['artifact'], table_previews=revision['table_previews'])
        self.assertEqual(old, (self.artifact, self.previews, revision))

    def test_ancestor_body_change_forces_stale_even_when_packet_path_and_source_are_unchanged(self):
        def move_context(reference):
            other, correct = reference['document_tree']['nodes'][1:3]
            content = next(c for c in correct['source_content'] if c['content_id'] == 'context-b')
            anchor = next(a for a in correct['source_anchors'] if a['anchor_id'] == 'context-b')
            correct['source_content'].remove(content); other['source_content'].append(content)
            correct['source_anchors'].remove(anchor); other['source_anchors'].append(anchor)
        revision = self.revise(move_context)
        self.assertEqual(revision['baseline']['records'][0]['ancestor_path'], list(self.base.records[0].ancestor_path))
        self.assertIn(self.sid, revision['receipt']['context_changed_source_item_ids'])
        result = self.carry(revision)
        self.assertEqual(result['stale_parent_ids'], [self.sid])
        self.assertEqual(result['artifact']['records'], {})
        self.assertEqual(result['artifact']['status'], 'PARTIAL')
        self.assertEqual(len(result['artifact']['retired_singular_ids']), 2)
        self.assertEqual(result['artifact']['identity_registry'], self.artifact['identity_registry'])
        self.assertEqual(result['artifact']['reserved_display_ids'], self.artifact['reserved_display_ids'])

    def test_actual_new_table_text_is_compared_after_old_decomposition_is_verified_with_old_table(self):
        revision = self.rename()
        changed = deepcopy(revision['table_previews'])
        changed['renamed-table']['fragments'][0]['cells'][1]['text'] = 'Lock the valve.'
        result = self.carry(revision, table_previews=changed)
        self.assertEqual(result['stale_parent_ids'], [self.sid])
        self.assertEqual(result['artifact']['records'], {})

    def test_shared_header_geometry_change_is_not_hidden_by_packet_v1_fields(self):
        revision = self.rename()
        changed = deepcopy(revision['table_previews'])
        changed['renamed-table']['fragments'][0]['cells'][0]['bbox'][0] = 11.0
        self.assertEqual(self.carry(revision, table_previews=changed)['stale_parent_ids'], [self.sid])

    def test_same_baseline_hash_does_not_hide_changed_external_table_evidence(self):
        changed = deepcopy(self.previews)
        changed['lt-torque']['fragments'][0]['cells'][1]['text'] = 'Lock the valve.'
        result = decomp.rebind_source_item_decomposition(self.base, self.base, self.artifact,
            table_previews=changed, previous_table_previews=self.previews)
        self.assertEqual(result['stale_parent_ids'], [self.sid])

    def test_cross_map_requires_old_table_and_receipt(self):
        revision = self.rename()
        with self.assertRaisesRegex(ValueError, 'previous_table_previews'):
            self.carry(revision, previous_table_previews=None)
        with self.assertRaisesRegex(ValueError, 'shared Map rebind receipt'):
            self.carry(revision, map_revision_receipt=None)

    def test_rehashed_receipt_with_wrong_identity_or_table_proof_is_rejected(self):
        revision = self.rename()
        for key in ('source_sha256', 'previous_document_map_sha256', 'document_map_sha256', 'previous_baseline_sha256',
                    'baseline_sha256', 'table_previews_sha256'):
            bad = deepcopy(revision['receipt']); bad[key] = 'f' * 64
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'receipt .*binding differs'):
                self.carry(revision, map_revision_receipt=seal_receipt(bad))
        bad = deepcopy(revision['receipt']); bad['receipt_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'receipt hash changed'):
            self.carry(revision, map_revision_receipt=bad)
        bad = deepcopy(revision['receipt']); bad['schema_version'] = 'arbitrary-rebind/v1'
        with self.assertRaisesRegex(ValueError, 'shared Map rebind receipt'):
            self.carry(revision, map_revision_receipt=seal_receipt(bad))

    def test_receipt_cannot_hide_a_changed_ancestor_path_or_invent_context_ids(self):
        revision = self.revise(lambda reference: reference['document_tree']['nodes'][-1].update(parent_id='correct'))
        for ids in ([], ['unknown-source']):
            bad = deepcopy(revision['receipt']); bad['context_changed_source_item_ids'] = ids
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, 'context changes are invalid or incomplete'):
                self.carry(revision, map_revision_receipt=seal_receipt(bad))

    def test_stale_history_survives_same_baseline_noop_and_later_revision(self):
        revision = self.revise(lambda reference: reference['document_tree']['nodes'][-1].update(parent_id='correct'))
        first = self.carry(revision)['artifact']
        same = decomp.rebind_source_item_decomposition(revision['baseline'], revision['baseline'], first,
            table_previews=revision['table_previews'])
        self.assertEqual(same['stale_parent_ids'], [self.sid])
        baseline = core.SourceRequirementBaselineArtifactV1.model_validate(revision['baseline'])
        validation = core.SourceRequirementBaselineValidationReceiptV1.model_validate(revision['validation_receipt'])
        newer, _, _, _ = core.compile_source_requirement_classification_coordination(previous_artifact=baseline,
            previous_validation_receipt=validation, decisions=(core.SourceRequirementClassificationCoordinationDecisionV1(
                source_record_id=self.sid, requirementness='REQUIREMENT', requirement_kinds=('FUNCTIONAL',), confidence=0.95,
                decision_summary='Clarified wording; old split still awaits review.', reason_codes=('source_review',)),), model_id='synthetic')
        again = decomp.rebind_source_item_decomposition(baseline, newer, first, table_previews=revision['table_previews'])
        self.assertEqual(again['stale_parent_ids'], [self.sid])
        self.assertEqual(again['artifact']['retired_singular_ids'], first['retired_singular_ids'])
        self.assertEqual(again['artifact']['reserved_display_ids'], first['reserved_display_ids'])


if __name__ == '__main__':
    unittest.main()
