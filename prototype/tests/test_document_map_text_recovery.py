"""Atomic recovery of an original clause misclassified as a structure title."""

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from copy import deepcopy
import unittest

from .test_document_map_revision import fixture
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.document_map_revision import (
    compile_document_map_revision, rebind_baseline_for_document_map_revision,
    map_rebind_added_source_ids,
)
from app.application.agent_workflow.source_item_decomposition import _map_rebind_context_changes


class MapTextRecoveryTests(unittest.TestCase):
    def setUp(self):
        ref, self.before_map, self.before, self.validation, self.tables = fixture(misclassified_clause=True)
        node = ref["document_tree"]["nodes"][-1]
        node.update(label="", title="Torque requirements", title_authority="AGENT_DISPLAY_LABEL")
        node["source_content"][0]["is_title"] = False
        self.mapped = compile_document_map_revision(self.before_map, core.compile_document_map_artifact(ref))
        self.after_map = core.DocumentMapArtifactV1.model_validate(self.mapped["document_map"])
        old = next(r for r in self.before.records if r.text_bindings)
        fields = ("label", "source_form", "shared_source_content_ids", "requirementness", "requirement_kinds",
                  "non_requirement_role", "confidence", "decision_summary", "reason_codes")
        retained = {key: old.model_dump(mode="json")[key] for key in fields}
        retained.update(item_key="keep-existing", text_spans=[{"content_id": b.content_id,
            "start_offset": b.start_offset, "end_offset": b.end_offset} for b in old.text_bindings])
        text = node["source_content"][0]["text"]
        recovered = {"item_key": "recover-original-clause", "label": "3", "source_form": "NUMBERED_CLAUSE",
            "text_spans": [{"content_id": "heading-torque", "start_offset": 0, "end_offset": len(text)}],
            "shared_source_content_ids": [], "requirementness": "REQUIREMENT", "requirement_kinds": ["CONSTRAINT"],
            "confidence": .95, "decision_summary": "The actual source clause requires grounding.", "reason_codes": ["source_clause"]}
        self.selection = {"operation": "REPLACE", "affected_scope_ids": ["text:group"],
            "text_decisions": [{"scope_id": "text:group", "items": [recovered, retained], "structure_only_spans": []}]}

    def run_recovery(self, selection=None):
        return rebind_baseline_for_document_map_revision(self.before_map, self.after_map, self.before,
            self.validation, self.tables, text_scope_recovery=selection or self.selection,
            feedback_ids=("agent-observed-title-role-error",))

    def test_demoted_clause_is_recovered_in_one_direct_successor(self):
        with self.assertRaisesRegex(ValueError, "STRUCTURE_ONLY_SPAN_NOT_TITLE"):
            rebind_baseline_for_document_map_revision(self.before_map, self.after_map, self.before, self.validation, self.tables)
        result = self.run_recovery()
        after = core.SourceRequirementBaselineArtifactV1.model_validate(result["baseline"])
        self.assertEqual(after.revision, self.before.revision + 1)
        self.assertEqual(after.predecessor_artifact_sha256, self.before.artifact_sha256)
        additions = [r for r in after.records if r.source_record_id not in {r.source_record_id for r in self.before.records}]
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0].text_bindings[0].text, "3 The housing shall be grounded.")
        self.assertEqual(additions[0].source_display_id, f"SRC-{self.before.source_display_high_watermark + 1:04d}")
        self.assertEqual(map_rebind_added_source_ids(result["receipt"], current_baseline=after), {additions[0].source_record_id})
        self.assertNotIn("heading-torque", {c.content_id for c in after.text_context_dispositions})
        changed = _map_rebind_context_changes(self.before, after, result["receipt"], self.tables, result["table_previews"])
        self.assertEqual(changed, {r.source_record_id for r in self.before.records})
        self.assertEqual(self.before.records[0].source_display_id, "SRC-0040")

    def test_recovery_cannot_drop_original_text(self):
        selection = deepcopy(self.selection)
        selection["text_decisions"][0]["items"][0]["text_spans"][0]["end_offset"] -= 5
        with self.assertRaisesRegex(ValueError, "UNCOVERED|COVERAGE|PARTITION"):
            self.run_recovery(selection)

    def test_recovery_cannot_reclassify_an_existing_item(self):
        selection = deepcopy(self.selection)
        selection["text_decisions"][0]["items"][1]["requirement_kinds"] = ["CONSTRAINT"]
        with self.assertRaisesRegex(ValueError, "CANNOT_CHANGE_EXISTING_ITEMS"):
            self.run_recovery(selection)

    def test_recovery_needs_recorded_feedback(self):
        with self.assertRaisesRegex(ValueError, "FEEDBACK"):
            rebind_baseline_for_document_map_revision(self.before_map, self.after_map, self.before, self.validation,
                self.tables, text_scope_recovery=self.selection)

    def test_new_clause_is_unassessed_and_old_changed_context_is_stale(self):
        from app.application.agent_workflow import source_item_analysis as analysis
        catalog_value = {"schema_version": "project-knowledge-facts/v1", "project_id": "synthetic", "knowledge_base_version": 0,
            "documents": [], "evidence_catalog": {}, "gaps": []}
        catalog = {**catalog_value, "catalog_sha256": core.canonical_json_sha256(catalog_value)}
        before_packet = analysis.build_source_item_analysis_packet(self.before_map, self.before, self.tables, catalog)
        assessments = analysis.compile_source_item_assessments(before_packet, {"decisions": [
            {"unit_id": unit["unit_id"], "decision": "PENDING_EVIDENCE", "reason": "Internal validation is absent.",
             "evidence": [], "clarification_question": "Provide validation evidence."} for unit in before_packet["units"]]})
        result = self.run_recovery()
        after_packet = analysis.build_source_item_analysis_packet(self.after_map, result["baseline"], result["table_previews"], catalog)
        carried = analysis.rebind_source_item_assessments(before_packet, assessments, after_packet, map_revision_receipt=result["receipt"], next_baseline=result["baseline"])
        self.assertEqual(len(after_packet["units"]), len(before_packet["units"]) + 1)
        self.assertEqual(carried["retained_unit_ids"], [])
        self.assertEqual(set(carried["stale_unit_ids"]), set(assessments["records"]))
        self.assertFalse(carried["artifact"]["records"])
        analysis.validate_source_item_assessments(after_packet, carried["artifact"])

    def test_downstream_refuses_resigned_unproven_added_identity(self):
        result = self.run_recovery()
        after = core.SourceRequirementBaselineArtifactV1.model_validate(result["baseline"])
        proof = deepcopy(result["receipt"])
        proof["text_scope_recovery"]["added_source_item_ids"][0] = self.before.records[0].source_record_id
        proof["receipt_sha256"] = core.canonical_json_sha256({k: v for k, v in proof.items() if k != "receipt_sha256"})
        with self.assertRaisesRegex(ValueError, "proof is invalid"):
            _map_rebind_context_changes(self.before, after, proof, self.tables, result["table_previews"])


if __name__ == "__main__":
    unittest.main()
