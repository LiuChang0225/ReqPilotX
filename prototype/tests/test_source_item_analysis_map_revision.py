from copy import deepcopy
import unittest

from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow import source_item_analysis as analysis
from app.application.agent_workflow.document_map_revision import compile_document_map_revision, rebind_baseline_for_document_map_revision
from .test_source_item_decomposition_map_revision import table_fixture


class AnalysisMapCarryTests(unittest.TestCase):
    def setUp(self):
        self.reference, self.map, self.base, self.validation, self.tables, _ = table_fixture()
        value = {'schema_version': 'project-knowledge-facts/v1', 'project_id': 'synthetic', 'knowledge_base_version': 0,
                 'documents': [], 'evidence_catalog': {}, 'gaps': []}
        self.catalog = {**value, 'catalog_sha256': core.canonical_json_sha256(value)}
        self.old = analysis.build_source_item_analysis_packet(self.map, self.base, self.tables, self.catalog)
        self.artifact = analysis.compile_source_item_assessments(self.old, {'decisions': [{'unit_id': item['unit_id'],
            'decision': 'PENDING_EVIDENCE', 'reason': 'No internal valve test is supplied.', 'evidence': [],
            'clarification_question': 'Provide the current valve operation verification.'} for item in self.old['units']]})

    def revise(self, *, moved=False):
        reference = deepcopy(self.reference)
        group = reference['document_tree']['nodes'][-1]
        group['node_id'] = 'renamed-node'
        if moved:
            group['parent_id'] = 'correct'
        group['content_blocks'][0]['logical_table_scope_id'] = 'renamed-table'
        next(anchor for anchor in group['source_anchors'] if anchor.get('table_token'))['logical_table_scope_id'] = 'renamed-table'
        reference['logical_tables'][0]['scope_id'] = 'renamed-table'
        new_map = compile_document_map_revision(self.map, core.compile_document_map_artifact(reference))['document_map']
        result = rebind_baseline_for_document_map_revision(self.map, new_map, self.base, self.validation, self.tables)
        packet = analysis.build_source_item_analysis_packet(new_map, result['baseline'], result['table_previews'], self.catalog)
        return result, packet

    def test_pure_map_identifier_change_keeps_the_existing_assessment(self):
        result, packet = self.revise()
        carried = analysis.rebind_source_item_assessments(self.old, self.artifact, packet, map_revision_receipt=result['receipt'])
        self.assertEqual(carried['stale_unit_ids'], [])
        self.assertEqual(carried['artifact']['records'], self.artifact['records'])
        analysis.validate_source_item_assessments(packet, carried['artifact'])
        self.assertEqual(analysis.rebind_source_item_assessments(self.old, self.artifact, packet)['retained_unit_ids'], [])

    def test_actual_parent_context_change_requires_reassessment(self):
        result, packet = self.revise(moved=True)
        self.assertTrue(result['receipt']['context_changed_source_item_ids'])
        carried = analysis.rebind_source_item_assessments(self.old, self.artifact, packet, map_revision_receipt=result['receipt'])
        self.assertEqual(carried['retained_unit_ids'], [])
        self.assertEqual(carried['stale_unit_ids'], list(self.artifact['records']))

    def test_map_receipt_for_a_different_baseline_is_rejected(self):
        result, packet = self.revise()
        proof = {**result['receipt'], 'baseline_sha256': '9' * 64}
        proof['receipt_sha256'] = core.canonical_json_sha256({k: v for k, v in proof.items() if k != 'receipt_sha256'})
        with self.assertRaisesRegex(ValueError, 'exact source versions'):
            analysis.rebind_source_item_assessments(self.old, self.artifact, packet, map_revision_receipt=proof)

    def test_annotation_binding_hash_change_does_not_masquerade_as_changed_reading(self):
        result, packet = self.revise()
        previous = deepcopy(self.old)
        for value, digest in ((previous, 'a' * 64), (packet, 'b' * 64)):
            for unit in value['units']:
                unit['visual_interpretation'] = {'annotation_source_evidence_sha256': digest,
                    'visual_interpretation': {'text': 'The original drawing is unchanged.', 'baseline_sha256': value['baseline_sha256']}}
                value['unit_registry'][unit['unit_id']]['unit_sha256'] = core.canonical_json_sha256(unit)
            value['packet_sha256'] = core.canonical_json_sha256({k: v for k, v in value.items() if k != 'packet_sha256'})
        old_artifact = analysis.compile_source_item_assessments(previous, {'decisions': [
            {'unit_id': uid, **entry} for uid, entry in self.artifact['records'].items()]})
        carried = analysis.rebind_source_item_assessments(previous, old_artifact, packet, map_revision_receipt=result['receipt'])
        self.assertEqual(carried['stale_unit_ids'], [])
        changed = deepcopy(packet)
        changed['units'][0]['visual_interpretation']['visual_interpretation']['text'] = 'A drawing dimension remains unreadable.'
        changed['unit_registry'][changed['units'][0]['unit_id']]['unit_sha256'] = core.canonical_json_sha256(changed['units'][0])
        changed['packet_sha256'] = core.canonical_json_sha256({k: v for k, v in changed.items() if k != 'packet_sha256'})
        self.assertEqual(analysis.rebind_source_item_assessments(previous, old_artifact, changed,
            map_revision_receipt=result['receipt'])['stale_unit_ids'], list(old_artifact['records']))
