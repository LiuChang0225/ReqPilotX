import unittest

from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow import source_requirement_baseline as core
from . import test_source_requirement_baseline as fixtures


class ExistingManualJudgmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SourceItemScopeRevisionTests()
        self.fixture.setUp()

    def baseline(self, *, origin='USER_OVERRIDE'):
        fixture = self.fixture
        records = tuple(record.model_copy(update={'decision_origin': origin, 'review_state': 'REVIEWED',
            'decision_summary': 'Existing engineer-reviewed judgment.'}) if record.node_id == 'section-1.2' else record
            for record in fixture.baseline.records)
        draft = fixture.baseline.model_copy(update={'records': records})
        digest = core.canonical_json_sha256(core._baseline_hash_payload(draft))
        baseline = core.SourceRequirementBaselineArtifactV1.model_validate(draft.model_copy(update={
            'artifact_id': 'source-requirement-baseline-' + digest[:32], 'artifact_sha256': digest}).model_dump(mode='json'))
        receipt = core._compile_validation_receipt_from_artifact(document_map=fixture.document_map, artifact=baseline)
        return baseline, receipt

    def coordinate(self, baseline, receipt, *, kinds):
        record = next(record for record in baseline.records if record.node_id == 'section-1.2')
        decision = core.SourceRequirementClassificationCoordinationDecisionV1(source_record_id=record.source_record_id,
            requirementness='REQUIREMENT', requirement_kinds=kinds, confidence=0.91,
            decision_summary='New Agent rationale.', reason_codes=('source_review',))
        return core.compile_source_requirement_classification_coordination(previous_artifact=baseline,
            previous_validation_receipt=receipt, decisions=(decision,), model_id='synthetic', coordinator_role='PORTABLE_AGENT')

    def test_representation_correction_retains_manual_judgment_and_review_status(self):
        baseline, receipt = self.baseline()
        candidate = self.fixture.initial_candidate.model_copy(update={'items': tuple(item.model_copy(update={
            'source_form': core.SourceRecordForm.PARAGRAPH, 'decision_summary': 'Incidental fresh Agent wording.', 'confidence': 0.5})
            for item in self.fixture.initial_candidate.items)})
        revised, _, _ = self.fixture._replace(current=baseline, validation=receipt, candidate=candidate)
        old = next(record for record in baseline.records if record.node_id == 'section-1.2')
        new = next(record for record in revised.records if record.source_record_id == old.source_record_id)
        self.assertEqual(new.source_form.value, 'PARAGRAPH')
        self.assertEqual((new.decision_origin, new.review_state, new.decision_summary, new.confidence),
                         (old.decision_origin, old.review_state, old.decision_summary, old.confidence))

    def test_automatic_boundary_split_cannot_remove_a_manual_override(self):
        baseline, receipt = self.baseline()
        with self.assertRaisesRegex(ValueError, 'USER_OVERRIDE_BOUNDARY'):
            self.fixture._replace(current=baseline, validation=receipt, candidate=self.fixture._split_candidate())

    def test_classification_cannot_replace_manual_override(self):
        baseline, receipt = self.baseline()
        with self.assertRaisesRegex(ValueError, 'USER_OVERRIDE'):
            self.coordinate(baseline, receipt, kinds=('QUALITY',))

    def test_same_classification_does_not_relabel_new_ai_explanation_as_human(self):
        baseline, receipt = self.baseline()
        old = next(record for record in baseline.records if record.node_id == 'section-1.2')
        revised, _, coordination, _ = self.coordinate(baseline, receipt, kinds=old.requirement_kinds)
        self.assertEqual(next(record for record in revised.records if record.source_record_id == old.source_record_id), old)
        self.assertEqual(coordination.coordinator_role, 'PORTABLE_AGENT')

    def test_changed_agent_judgment_is_not_marked_as_previously_reviewed(self):
        baseline, receipt = self.baseline(origin='AGENT_DEFAULT')
        revised, _, _, _ = self.coordinate(baseline, receipt, kinds=('QUALITY',))
        record = next(record for record in revised.records if record.node_id == 'section-1.2')
        self.assertEqual((record.decision_origin, record.review_state), ('AGENT_DEFAULT', 'UNREVIEWED'))
