from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import runpy
import unittest

from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.adapters.agents.stage_context import compile_agent_stage_context
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KIT_ROOT = REPOSITORY_ROOT / "agent-kit"
CATALOG_PATH = KIT_ROOT / "references/structure-failure-patterns.json"
SELECTOR = runpy.run_path(
    str(KIT_ROOT / "scripts/select_structure_failure_patterns.py")
)
VALIDATOR = runpy.run_path(
    str(KIT_ROOT / "scripts/validate_structure_materialization.py")
)
load_catalog = SELECTOR["load_catalog"]
select_patterns = SELECTOR["select_patterns"]
validate_materialization = VALIDATOR["validate_materialization"]


class StructureFailurePatternTests(unittest.TestCase):
    def test_selector_returns_only_matching_patterns_with_a_hard_cap(self) -> None:
        catalog = load_catalog(CATALOG_PATH)

        result = select_patterns(
            catalog,
            (
                "accepted_action_has_no_materialization_receipt",
                "cell_text_contains_following_heading",
                "duplicate_source_handle_across_visible_owners",
                "image_not_dispositioned",
                "per_fragment_column_inference",
            ),
        )

        self.assertEqual(len(result["selected_patterns"]), 3)
        self.assertEqual(
            [item["pattern_id"] for item in result["selected_patterns"]],
            [
                "confirmed_proposal_not_verified",
                "visual_subcell_scope_leakage",
                "parent_child_evidence_duplication",
            ],
        )
        self.assertTrue(result["selection_truncated"])
        self.assertEqual(len(result["selection_sha256"]), 64)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("continuation_column_topology_drift", serialized)
        self.assertNotIn("cross_cell_visual_placement_drop", serialized)

    def test_catalog_is_abstract_and_not_loaded_as_a_stage_reference(self) -> None:
        catalog_text = CATALOG_PATH.read_text(encoding="utf-8")
        for customer_specific in (
            "PDS",
            "JAC",
            "减振器",
            "5.8",
            "P23",
        ):
            self.assertNotIn(customer_specific, catalog_text)
        manifest = json.loads((KIT_ROOT / "manifest.json").read_text(encoding="utf-8"))
        structure_stage = next(
            item
            for item in manifest["stages"]
            if item["stage"] == "D2_LOGICAL_STRUCTURE"
        )
        self.assertNotIn(
            "references/structure-failure-patterns.json",
            structure_stage["reference_paths"],
        )
        skill_text = (
            KIT_ROOT
            / ".agents/skills/reqpilotx-logical-structure-reconstruction/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("select_structure_failure_patterns.py", skill_text)
        compiled = compile_agent_stage_context(
            load_portable_agent_kit(KIT_ROOT),
            AgentExtractionStage.LOGICAL_STRUCTURE,
        )
        self.assertNotIn(
            "references/structure-failure-patterns.json",
            tuple(item.path for item in compiled.references),
        )
        self.assertNotIn(
            "Do not reuse one mixed physical cell",
            compiled.context_text,
        )

    def test_materialization_validator_detects_all_abstract_failure_shapes(
        self,
    ) -> None:
        result = validate_materialization(
            {
                "schema_version": "structure-materialization-observation/v1",
                "confirmed_actions": [
                    {
                        "action_id": "action-1",
                        "materialization_receipt_id": "",
                        "verification_receipt_id": "",
                        "rendered_output_reopened": False,
                    }
                ],
                "parent_direct_source_refs": ["source-a"],
                "child_owned_source_refs": ["source-a"],
                "node_title": "Repeated title",
                "visible_title_lines": ["Repeated title", "Repeated title"],
                "visual_scope_bbox": [0, 0, 10, 10],
                "rendered_cells": [
                    {
                        "cell_id": "cell-1",
                        "bbox": [0, 0, 12, 5],
                        "text": "value Next heading",
                    }
                ],
                "following_heading_texts": ["Next heading"],
                "continuation_segments": [
                    {"segment_id": "segment-1", "canonical_columns": ["a", "b"]},
                    {
                        "segment_id": "segment-2",
                        "canonical_columns": ["a", "b", "c"],
                    },
                ],
                "expected_image_ids": ["image-1", "image-2"],
                "image_dispositions": [
                    {"image_id": "image-1", "disposition": "PLACED_IN_CELL"}
                ],
                "navigation_children": [
                    {
                        "child_id": "child-1",
                        "source_kind": "INLINE_LIST_ITEM",
                        "independently_navigable": False,
                    }
                ],
            }
        )

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(
            {item["pattern_id"] for item in result["findings"]},
            {
                "confirmed_proposal_not_verified",
                "continuation_column_topology_drift",
                "cross_cell_visual_placement_drop",
                "minor_list_navigation_oversegmentation",
                "parent_child_evidence_duplication",
                "repeated_title_extraction",
                "visual_subcell_scope_leakage",
            },
        )

    def test_materialization_validator_accepts_closed_evidence(self) -> None:
        result = validate_materialization(
            {
                "schema_version": "structure-materialization-observation/v1",
                "confirmed_actions": [
                    {
                        "action_id": "action-1",
                        "materialization_receipt_id": "materialized-1",
                        "verification_receipt_id": "verified-1",
                        "rendered_output_reopened": True,
                    }
                ],
                "parent_direct_source_refs": ["source-parent"],
                "child_owned_source_refs": ["source-child"],
                "node_title": "One title",
                "visible_title_lines": ["One title"],
                "visual_scope_bbox": [0, 0, 10, 10],
                "rendered_cells": [
                    {"cell_id": "cell-1", "bbox": [1, 1, 9, 9], "text": "value"}
                ],
                "following_heading_texts": ["Later heading"],
                "continuation_segments": [
                    {"segment_id": "segment-1", "canonical_columns": ["a", "b"]},
                    {"segment_id": "segment-2", "canonical_columns": ["a", "b"]},
                ],
                "expected_image_ids": ["image-1"],
                "image_dispositions": [
                    {"image_id": "image-1", "disposition": "FLOATING_IN_TABLE"}
                ],
                "navigation_children": [
                    {
                        "child_id": "child-1",
                        "source_kind": "HEADING",
                        "independently_navigable": True,
                    }
                ],
            }
        )

        self.assertEqual(
            result,
            {
                "schema_version": "structure-materialization-validation-report/v1",
                "status": "PASS",
                "finding_count": 0,
                "findings": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
