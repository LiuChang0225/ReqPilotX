from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.application.requirement_set_impact_service import (
    REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
    _impact_input_nodes,
    requirement_analysis_input_sha256,
    requirement_node_intrinsic_sha256,
)


def _analysis_row(**updates):
    row = {
        "id": "requirement-old",
        "req_code": "REQ0001",
        "entry_kind": "REQUIREMENT",
        "requirement_type": "FUNCTIONAL",
        "priority": "MEDIUM",
        "requirement_text": "控制器应在 100 ms 内进入安全状态。",
        "raw_text": "控制器应在 100 ms 内进入安全状态。",
        "display_text": "控制器应在 100 ms 内进入安全状态。",
        "matrix_node_id": "stable-node-1",
        "matrix_display_role": "ATOMIC_REQUIREMENT",
        "matrix_disposition": "ANALYZE",
        "matrix_analysis_eligibility": "ELIGIBLE",
        "matrix_source_refs": [{"source_id": "source-1", "page": 3}],
        "source_refs": [{"source_id": "source-1", "page": 3}],
        "content_blocks": [
            {"kind": "paragraph", "text": "控制器应在 100 ms 内进入安全状态。"}
        ],
        "table_refs": [],
        "ancestor_path": [{"node_id": "section-1", "label": "安全状态"}],
        "direct_parent": {"node_id": "section-1", "label": "安全状态"},
        "adjacent_context": [],
    }
    row.update(updates)
    return row


def _tree_node(**updates):
    fields = {
        "classification": "TECHNICAL",
        "projection_role": "REQUIREMENT",
        "semantic_state": "ACCEPTED",
        "table_evidence_binding_id": "",
        "requirement_evidence_binding_id": "evidence-binding-1",
        "deterministic_rule_evidence": (),
    }
    fields.update(updates)
    return SimpleNamespace(**fields)


class RequirementSetImpactServiceHashTests(unittest.TestCase):
    def test_set_local_requirement_identity_does_not_change_analysis_input(self) -> None:
        old = _analysis_row()
        new = _analysis_row(id="requirement-new", req_code="REQ0042")

        self.assertEqual(
            requirement_analysis_input_sha256(old),
            requirement_analysis_input_sha256(new),
        )

    def test_real_context_change_changes_analysis_input(self) -> None:
        old = _analysis_row()
        new = _analysis_row(
            requirement_text="控制器应在 50 ms 内进入安全状态。",
            raw_text="控制器应在 50 ms 内进入安全状态。",
            display_text="控制器应在 50 ms 内进入安全状态。",
        )

        self.assertNotEqual(
            requirement_analysis_input_sha256(old),
            requirement_analysis_input_sha256(new),
        )

    def test_classification_and_evidence_are_intrinsic_to_the_node(self) -> None:
        row = _analysis_row()
        baseline = requirement_node_intrinsic_sha256(
            row,
            tree_node=_tree_node(),
        )
        reclassified = requirement_node_intrinsic_sha256(
            row,
            tree_node=_tree_node(classification="INFORMATION"),
        )
        rebound = requirement_node_intrinsic_sha256(
            row,
            tree_node=_tree_node(
                requirement_evidence_binding_id="evidence-binding-2"
            ),
        )
        changed_evidence = requirement_node_intrinsic_sha256(
            row,
            tree_node=_tree_node(),
            requirement_evidence={
                "schema_version": "requirement-evidence-binding/v1",
                "source_refs": [{"source_id": "source-2", "page": 4}],
            },
        )

        self.assertNotEqual(baseline, reclassified)
        self.assertEqual(baseline, rebound)
        self.assertNotEqual(baseline, changed_evidence)

    def test_current_result_without_exact_input_provenance_fails_closed(self) -> None:
        row = _analysis_row()
        current_hash = requirement_analysis_input_sha256(row)
        base_assessment = {
            "id": "assessment-1",
            "reference_status": "CURRENT",
            "ai_decision": "COMPLIANT",
            "ai_reason": "verified",
            "result_origin": "NATIVE",
        }
        with patch(
            "app.application.requirement_set_impact_service.db.get_assessment",
            return_value=base_assessment,
        ):
            unproven = _impact_input_nodes(
                targets=[row],
                tree_nodes={"stable-node-1": _tree_node()},
                evidence_by_node={},
                current_result_node_ids={"stable-node-1"},
            )[0]
        self.assertTrue(unproven.has_current_result)
        self.assertIsNone(unproven.analysis_input_sha256)

        with patch(
            "app.application.requirement_set_impact_service.db.get_assessment",
            return_value={
                **base_assessment,
                "analysis_input_schema_version": (
                    REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
                ),
                "analysis_input_sha256": current_hash,
            },
        ):
            proven = _impact_input_nodes(
                targets=[row],
                tree_nodes={"stable-node-1": _tree_node()},
                evidence_by_node={},
                current_result_node_ids={"stable-node-1"},
            )[0]
        self.assertEqual(proven.analysis_input_sha256, current_hash)


if __name__ == "__main__":
    unittest.main()
