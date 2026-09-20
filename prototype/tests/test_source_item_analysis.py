from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from copy import deepcopy
import hashlib
import unittest

from app.application.agent_workflow.source_item_analysis import (
    SourceItemAnalysisError,
    compile_source_item_assessments,
    validate_source_item_assessments,
    validate_knowledge_catalog,
)
from app.application.agent_workflow.source_requirement_baseline import (
    canonical_json_sha256,
)


def catalog(*, verified=True, same_source=False):
    text = "The measured standby current was 4 mA at 12 V and 25 C."
    fact = {
        "fact_id": "fact-1",
        "document_id": "knowledge-1",
        "source_sha256": "a" * 64 if same_source else "f" * 64,
        "role": "PRODUCT_EVIDENCE",
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_ref": {"page": 1},
        "authority": "APPROVED" if verified else "UNKNOWN",
        "claim_role": "TEST_RESULT",
        "eligible": True,
        "authority_verified": verified,
    }
    value = {
        "schema_version": "project-knowledge-facts/v1",
        "project_id": "project-synthetic",
        "knowledge_base_version": 1,
        "evidence_catalog": {"fact-1": fact},
        "documents": [],
        "gaps": [],
    }
    return {**value, "catalog_sha256": canonical_json_sha256(value)}


def packet(*, selected=("a", "b"), knowledge=None):
    kb = catalog() if knowledge is None else knowledge
    units = [
        {
            "unit_id": key,
            "source_segments": [
                {"text": "Standby current shall be <= 5 mA at 12 V and 25 C."}
            ],
        }
        for key in ("a", "b")
    ]
    value = {
        "schema_version": "source-item-analysis-packet/v1",
        "project_id": "project-synthetic",
        "baseline_sha256": "b" * 64,
        "source_sha256": "a" * 64,
        "decomposition_sha256": "",
        "catalog_sha256": kb["catalog_sha256"],
        "knowledge_base_version": 1,
        "catalog_gaps": [],
        "knowledge_documents": [],
        "units": [u for u in units if u["unit_id"] in selected],
        "unit_registry": {
            u["unit_id"]: {"unit_sha256": canonical_json_sha256(u)} for u in units
        },
        "selected_unit_ids": list(selected),
        "evidence_catalog": kb["evidence_catalog"],
    }
    return {**value, "packet_sha256": canonical_json_sha256(value)}


def decision(unit, kind="COMPLIANT"):
    return {
        "unit_id": unit,
        "decision": kind,
        "reason": "同一工况下实测值小于上限。",
        "evidence": [{"fact_id": "fact-1", "quote": "4 mA at 12 V and 25 C"}],
        "clarification_question": "请补充当前交付版本的确认。"
        if kind == "PENDING_EVIDENCE"
        else "",
    }


class SourceItemAnalysisTests(unittest.TestCase):
    def test_local_analysis_keeps_unselected_units_and_their_input_receipts(self):
        original = compile_source_item_assessments(
            packet(),
            {"decisions": [decision("a"), decision("b")]},
            input_packet_ref={"object_key": "old"},
        )
        scoped = packet(selected=("a",))
        changed = compile_source_item_assessments(
            scoped,
            {"decisions": [decision("a", "PENDING_EVIDENCE")]},
            previous=original,
            input_packet_ref={"object_key": "new"},
        )
        validate_source_item_assessments(scoped, changed)
        self.assertEqual(changed["records"]["b"], original["records"]["b"])
        self.assertEqual(
            changed["unit_input_receipts"]["b"], original["unit_input_receipts"]["b"]
        )
        self.assertEqual(changed["affected_unit_ids"], ["a"])
        self.assertEqual(
            changed["unit_input_receipts"]["a"]["input_packet_ref"],
            {"object_key": "new"},
        )
        self.assertEqual(changed["status"], "COMPLETE")

    def test_changed_catalog_cannot_inherit_previous_analysis(self):
        original = compile_source_item_assessments(
            packet(), {"decisions": [decision("a"), decision("b")]}
        )
        changed_packet = packet(selected=("a",), knowledge=catalog(verified=False))
        changed = compile_source_item_assessments(
            changed_packet,
            {"decisions": [decision("a", "PENDING_EVIDENCE")]},
            previous=original,
        )
        self.assertEqual(set(changed["records"]), {"a"})
        self.assertEqual(changed["unassessed_unit_count"], 1)
        self.assertEqual(changed["status"], "PARTIAL")

    def test_unknown_authority_and_customer_source_cannot_establish_compliance(self):
        for kb in (catalog(verified=False), catalog(same_source=True)):
            with self.assertRaisesRegex(SourceItemAnalysisError, "authority-verified"):
                compile_source_item_assessments(
                    packet(selected=("a",), knowledge=kb),
                    {"decisions": [decision("a")]},
                )

    def test_quote_catalog_injection_and_tampered_source_are_rejected(self):
        p = packet(selected=("a",))
        false = decision("a")
        false["evidence"][0]["quote"] = "3 mA at 12 V"
        with self.assertRaisesRegex(SourceItemAnalysisError, "registered knowledge"):
            compile_source_item_assessments(p, {"decisions": [false]})
        with self.assertRaisesRegex(
            SourceItemAnalysisError, "replacement evidence catalog"
        ):
            compile_source_item_assessments(
                p, {"decisions": [decision("a")], "evidence_catalog": {}}
            )
        kb = catalog()
        kb["evidence_catalog"]["fact-1"]["text"] = "changed"
        kb["catalog_sha256"] = canonical_json_sha256(
            {k: v for k, v in kb.items() if k != "catalog_sha256"}
        )
        with self.assertRaises(SourceItemAnalysisError):
            validate_knowledge_catalog(kb)

    def test_pending_question_is_required_and_artifact_authority_is_not_forgeable(self):
        p = packet(selected=("a",))
        d = decision("a", "PENDING_EVIDENCE")
        d["clarification_question"] = ""
        with self.assertRaisesRegex(SourceItemAnalysisError, "concrete"):
            compile_source_item_assessments(p, {"decisions": [d]})
        artifact = compile_source_item_assessments(p, {"decisions": [decision("a")]})
        tampered = deepcopy(artifact)
        tampered["human_accepted"] = True
        digest = canonical_json_sha256(
            {
                k: v
                for k, v in tampered.items()
                if k not in {"artifact_id", "artifact_sha256"}
            }
        )
        tampered.update(
            artifact_id="source-item-assessments-" + digest[:32], artifact_sha256=digest
        )
        with self.assertRaisesRegex(SourceItemAnalysisError, "human-authority"):
            validate_source_item_assessments(p, tampered)


if __name__ == "__main__":
    unittest.main()
