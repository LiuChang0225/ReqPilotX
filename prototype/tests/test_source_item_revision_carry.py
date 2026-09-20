from copy import deepcopy
import unittest

from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from app.application.agent_workflow import source_requirement_baseline as source
from app.application.agent_workflow import source_item_decomposition as decomposition
from app.application.agent_workflow import source_item_analysis as analysis
from .test_source_item_lineage import source_fixture
from .test_source_item_analysis import packet, decision


def seal_packet(value):
    value['packet_sha256'] = source.canonical_json_sha256({k: v for k, v in value.items() if k != 'packet_sha256'})
    return value


class DecompositionCarryTests(unittest.TestCase):
    def test_keep_reason_survives_unchanged_source_but_changed_judgment_requires_review(self):
        kept = decomposition.compile_source_item_decomposition(self.baseline,
            [{'source_item_id': self.sid, 'action': 'KEEP', 'reason': 'Treat the complete operating sequence as one review unit.'}])
        carried = decomposition.rebind_source_item_decomposition(self.baseline, self.successor(), kept)['artifact']
        self.assertEqual(carried['dispositions'][self.sid]['reason'], kept['dispositions'][self.sid]['reason'])
        self.assertEqual(carried['review_scope']['remaining_count'], 0)
        changed = decomposition.rebind_source_item_decomposition(self.baseline, self.successor(('QUALITY',)), kept)['artifact']
        self.assertEqual(changed['status'], 'PARTIAL')
        self.assertEqual(changed['review_scope']['remaining_source_item_ids'], [self.sid])
        self.assertNotIn(self.sid, changed['dispositions'])
        self.assertEqual(changed['kept_source_ids'], [])
        self.assertEqual(kept['dispositions'][self.sid]['reason'], 'Treat the complete operating sequence as one review unit.')

    def setUp(self):
        text = 'Open the valve. Close the valve.'
        _, _, _, self.baseline, self.receipt = source_fixture(text)
        self.sid = self.baseline.records[0].source_record_id
        self.decision = {'source_item_id': self.sid, 'action': 'SPLIT', 'reason': 'Separate operational outcomes',
            'children': [{'text': text[a:b], 'source_ranges': [{'segment_index': 0, 'start_offset': a, 'end_offset': b}]} for a, b in ((0, 15), (16, len(text)))]}
        self.original = decomposition.compile_source_item_decomposition(self.baseline, [self.decision])

    def successor(self, kinds=None):
        record = self.baseline.records[0]
        change = source.SourceRequirementClassificationCoordinationDecisionV1(
            source_record_id=self.sid, requirementness='REQUIREMENT',
            requirement_kinds=kinds or record.requirement_kinds, confidence=0.95,
            decision_summary='Clarified rationale; source retained.', reason_codes=('source_review',))
        return source.compile_source_requirement_classification_coordination(previous_artifact=self.baseline,
            previous_validation_receipt=self.receipt, decisions=(change,), model_id='synthetic')[0]

    def test_rationale_revision_retains_exact_children_and_real_new_lineage_proof(self):
        successor = self.successor()
        result = decomposition.rebind_source_item_decomposition(self.baseline, successor, self.original)
        artifact = result['artifact']
        self.assertEqual(result['stale_parent_ids'], [])
        self.assertEqual(artifact['records'], self.original['records'])
        self.assertEqual(artifact['predecessor_sha256'], self.original['artifact_sha256'])
        self.assertNotEqual(artifact['validation_receipts'][self.sid]['lineage_sha256'], self.original['validation_receipts'][self.sid]['lineage_sha256'])
        decomposition.validate_source_item_decomposition(successor, artifact)

    def test_changed_parent_judgment_is_explicitly_stale_and_numbers_stay_reserved(self):
        successor = self.successor(('QUALITY',))
        result = decomposition.rebind_source_item_decomposition(self.baseline, successor, self.original)
        self.assertEqual(result['stale_parent_ids'], [self.sid])
        self.assertEqual(result['artifact']['records'], {})
        self.assertEqual(result['artifact']['identity_registry'], self.original['identity_registry'])
        self.assertEqual(result['artifact']['status'], 'PARTIAL')
        self.assertEqual(len(result['artifact']['retired_singular_ids']), 2)

    def test_undo_history_survives_an_unrelated_followup(self):
        undone = decomposition.compile_source_item_decomposition(self.baseline,
            [{'source_item_id': self.sid, 'action': 'UNDO', 'reason': 'Review as one parent'}], previous=self.original)
        next_version = decomposition.compile_source_item_decomposition(self.baseline, [], previous=undone)
        self.assertEqual(next_version['retired_singular_ids'], undone['retired_singular_ids'])
        self.assertEqual(next_version['kept_source_ids'], [self.sid])

    def test_stale_split_survives_another_baseline_and_requires_explicit_review(self):
        successor = self.successor(('QUALITY',))
        first = decomposition.rebind_source_item_decomposition(self.baseline, successor, self.original)['artifact']
        further = self.successor(('FUNCTIONAL',))
        again = decomposition.rebind_source_item_decomposition(successor, further, first)['artifact']
        self.assertEqual(again['carry_forward']['stale_parent_ids'], [self.sid])
        self.assertEqual(again['status'], 'PARTIAL')

    def test_changed_parent_req_cannot_reuse_children_from_old_namespace(self):
        record = self.baseline.records[0].model_copy(update={'requirement_display_id': 'REQ-0002'})
        draft = self.baseline.model_copy(update={'records': (record,)})
        digest = source.canonical_json_sha256(source._baseline_hash_payload(draft))
        newer = source.SourceRequirementBaselineArtifactV1.model_validate(draft.model_copy(update={
            'artifact_sha256': digest, 'artifact_id': 'source-requirement-baseline-' + digest[:32]}).model_dump(mode='json'))
        carried = decomposition.rebind_source_item_decomposition(self.baseline, newer, self.original)['artifact']
        with self.assertRaisesRegex(ValueError, 'identity migration'):
            decomposition.compile_source_item_decomposition(newer, [self.decision], previous=carried)

    def test_correcting_a_text_form_without_changing_source_keeps_derived_work(self):
        record = self.baseline.records[0].model_copy(update={'source_form': source.SourceRecordForm.PARAGRAPH})
        draft = self.baseline.model_copy(update={'records': (record,)})
        digest = source.canonical_json_sha256(source._baseline_hash_payload(draft))
        newer = source.SourceRequirementBaselineArtifactV1.model_validate(draft.model_copy(update={
            'artifact_sha256': digest, 'artifact_id': 'source-requirement-baseline-' + digest[:32]}).model_dump(mode='json'))
        result = decomposition.rebind_source_item_decomposition(self.baseline, newer, self.original)
        self.assertEqual(result['stale_parent_ids'], [])
        self.assertEqual(result['artifact']['records'], self.original['records'])


class AssessmentCarryTests(unittest.TestCase):
    def setUp(self):
        self.old = packet()
        self.artifact = analysis.compile_source_item_assessments(self.old, {'decisions': [decision('a'), decision('b')]})

    def test_local_classification_change_keeps_other_result_with_original_provenance(self):
        new = deepcopy(self.old)
        new['baseline_sha256'] = 'c' * 64
        new['units'][0]['classes'] = ['QUALITY', 'CONSTRAINT']
        new['unit_registry']['a']['unit_sha256'] = source.canonical_json_sha256(new['units'][0])
        result = analysis.rebind_source_item_assessments(self.old, self.artifact, seal_packet(new))
        self.assertEqual(result['stale_unit_ids'], ['a'])
        self.assertEqual(result['retained_unit_ids'], ['b'])
        self.assertEqual(result['artifact']['records']['b'], self.artifact['records']['b'])
        self.assertEqual(result['artifact']['status'], 'PARTIAL')
        self.assertEqual(result['artifact']['unit_input_receipts']['b']['origin_input_receipt'], self.artifact['unit_input_receipts']['b'])
        analysis.validate_source_item_assessments(new, result['artifact'])

    def test_new_knowledge_invalidates_old_conclusions_including_unchanged_units(self):
        new = deepcopy(self.old)
        new['catalog_sha256'] = 'd' * 64
        new['knowledge_base_version'] = 2
        result = analysis.rebind_source_item_assessments(self.old, self.artifact, seal_packet(new))
        self.assertEqual(result['retained_unit_ids'], [])
        self.assertEqual(result['stale_unit_ids'], ['a', 'b'])
        self.assertEqual(result['artifact']['unassessed_unit_count'], 2)

    def test_visual_version_wrapper_is_ignored_but_changed_reading_is_not(self):
        old = deepcopy(self.old)
        old['units'][0]['visual_interpretation'] = {'visual_interpretation': {'baseline_sha256': old['baseline_sha256'], 'text': 'Original dimension 5 mm'}}
        old['unit_registry']['a']['unit_sha256'] = source.canonical_json_sha256(old['units'][0])
        seal_packet(old)
        artifact = analysis.compile_source_item_assessments(old, {'decisions': [decision('a'), decision('b')]})
        new = deepcopy(old)
        new['baseline_sha256'] = 'c' * 64
        new['units'][0]['visual_interpretation']['visual_interpretation']['baseline_sha256'] = new['baseline_sha256']
        new['unit_registry']['a']['unit_sha256'] = source.canonical_json_sha256(new['units'][0])
        self.assertEqual(analysis.rebind_source_item_assessments(old, artifact, seal_packet(new))['stale_unit_ids'], [])
        new['units'][0]['visual_interpretation']['visual_interpretation']['text'] = 'Dimension unreadable'
        new['unit_registry']['a']['unit_sha256'] = source.canonical_json_sha256(new['units'][0])
        self.assertEqual(analysis.rebind_source_item_assessments(old, artifact, seal_packet(new))['stale_unit_ids'], ['a'])

    def test_altered_prior_judgment_cannot_be_carried(self):
        bad = deepcopy(self.artifact)
        bad['records']['a']['decision'] = 'NON_COMPLIANT'
        with self.assertRaises(ValueError):
            analysis.rebind_source_item_assessments(self.old, bad, self.old)

    def test_number_and_topic_annotation_does_not_discard_unchanged_analysis(self):
        new = deepcopy(self.old)
        new['units'][0]['visual_interpretation'] = {'source_number': '3.2', 'topics': ['VERIFICATION'],
            'annotation_source_evidence_sha256': 'c' * 64, 'annotation_update_reason': 'Expose the existing source number.',
            'source_number_evidence': {'source_number_ref': {'content_id': 'source-heading'}}}
        # Real packets consistently carry an empty annotation object before any
        # display annotation is present, rather than omitting the field.
        old = deepcopy(self.old)
        old['units'][0]['visual_interpretation'] = {}
        old['unit_registry']['a']['unit_sha256'] = source.canonical_json_sha256(old['units'][0])
        seal_packet(old)
        prior = analysis.compile_source_item_assessments(old, {'decisions': [decision('a'), decision('b')]})
        new['unit_registry']['a']['unit_sha256'] = source.canonical_json_sha256(new['units'][0])
        result = analysis.rebind_source_item_assessments(old, prior, seal_packet(new))
        self.assertEqual(result['stale_unit_ids'], [])
        self.assertEqual(result['artifact']['records'], prior['records'])
