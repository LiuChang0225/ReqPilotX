"""Independent checks for the final Map/text recovery proof boundary."""

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from copy import deepcopy
import unittest

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_decomposition import _map_rebind_context_changes
from . import test_document_map_text_recovery as fixtures


class MapRecoveryProofAuditTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.MapTextRecoveryTests()
        self.case.setUp()

    def test_scope_output_identity_must_bind_actual_final_successor(self):
        case = self.case
        result = case.run_recovery()
        final = core.SourceRequirementBaselineArtifactV1.model_validate(result["baseline"])
        receipt = deepcopy(result["receipt"])
        scope = receipt["text_scope_recovery"]["scope_receipt"]
        scope.update(artifact_id="source-requirement-baseline-" + "f" * 32, artifact_sha256="f" * 64)
        scope["receipt_sha256"] = core.canonical_json_sha256({k: v for k, v in scope.items() if k != "receipt_sha256"})
        receipt["receipt_sha256"] = core.canonical_json_sha256({k: v for k, v in receipt.items() if k != "receipt_sha256"})
        with self.assertRaises(ValueError):
            _map_rebind_context_changes(case.before, final, receipt, case.tables, result["table_previews"])

    def test_recovery_respects_reserved_number_floor_and_existing_manual_judgment(self):
        case = self.case
        records = tuple(record.model_copy(update={"decision_origin": "USER_OVERRIDE", "review_state": "REVIEWED",
            "decision_summary": "Existing engineer-reviewed source judgment."}) if record.text_bindings else record
            for record in case.before.records)
        draft = case.before.model_copy(update={"records": records, "source_display_high_watermark": 200})
        digest = core.canonical_json_sha256(core._baseline_hash_payload(draft))
        case.before = core.SourceRequirementBaselineArtifactV1.model_validate(draft.model_copy(update={
            "artifact_id": "source-requirement-baseline-" + digest[:32], "artifact_sha256": digest}).model_dump(mode="json"))
        case.validation = core._compile_validation_receipt_from_artifact(document_map=case.before_map, artifact=case.before)
        result = case.run_recovery()
        final = core.SourceRequirementBaselineArtifactV1.model_validate(result["baseline"])
        before = {record.source_record_id: record for record in case.before.records}
        additions = [record for record in final.records if record.source_record_id not in before]
        self.assertEqual([record.source_display_id for record in additions], ["SRC-0201"])
        self.assertEqual(final.source_display_high_watermark, 201)
        for record in final.records:
            if record.source_record_id in before:
                previous = before[record.source_record_id]
                self.assertEqual((record.decision_origin, record.review_state, record.decision_summary),
                    (previous.decision_origin, previous.review_state, previous.decision_summary))
        _map_rebind_context_changes(case.before, final, result["receipt"], case.tables, result["table_previews"])


if __name__ == "__main__":
    unittest.main()
