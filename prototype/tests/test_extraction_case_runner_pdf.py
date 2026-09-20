from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

if __package__:
    from .private_eval import require_private_eval
else:
    from private_eval import require_private_eval

import copy
import json
import unittest
from unittest.mock import patch

from app.application.extraction.classify_content import (
    DEFAULT_SEMANTIC_CLASSIFIER_ID,
    DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    classify_content,
)
from app.application.extraction.project_requirements import (
    DEFAULT_REQUIREMENT_PROJECTOR_ID,
    DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
    project_requirements,
)
from app.application.extraction.resolve_structure import (
    DEFAULT_STRUCTURE_RESOLVER_ID,
    DEFAULT_STRUCTURE_RESOLVER_VERSION,
    build_structure_tree,
)
from app.domain.extraction.requirement_view import RequirementProjectionPolicy
from app.domain.extraction.source_ir import SourceNodeKind
from app.domain.extraction.structure import (
    StructureFindingSeverity,
    StructureNodeKind,
    StructureOptionState,
    StructureRelationKind,
    StructureSourceRefKind,
)
from scripts import extraction_case_runner as runner

if __package__:
    from .test_pdf_shadow_silver import (
        _build_f06_native_pipeline,
        _build_pds_active_damper_pipeline,
        _build_pds_native_pipeline,
        _build_qsk_native_pipeline,
    )
else:
    from test_pdf_shadow_silver import (
        _build_f06_native_pipeline,
        _build_pds_active_damper_pipeline,
        _build_pds_native_pipeline,
        _build_qsk_native_pipeline,
    )


class ExtractionCaseRunnerPdfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_ir, self.tree, self.semantics, self.view = (
            _build_qsk_native_pipeline()
        )
        self.case = {
            "document_ids": ["DOC-QSK-J02-311"],
            "locators": [
                {
                    "document_id": "DOC-QSK-J02-311",
                    "kind": "section",
                    "pointer": "8.3.1",
                },
                {
                    "document_id": "DOC-QSK-J02-311",
                    "kind": "table",
                    "pointer": "Table 55; pages 66-67",
                },
            ],
        }
        self.fact = _pdf_table_fact(self.source_ir)
        self.oracle = {
            "oracle_id": "ORACLE-STR-002-A3",
            "document_id": "DOC-QSK-J02-311",
            "evaluator_id": runner.ORACLE_V2_EVALUATOR_ID,
            "fact": self.fact,
        }
        self.document_results = {
            "DOC-QSK-J02-311": {
                "source_ir_status": "COMPLETE",
                "source_sha256": self.source_ir.source_sha256,
                "detected_format": "pdf",
            }
        }
        self.source_ir_snapshots = {
            (self.source_ir.source_sha256, "pdf"): self.source_ir
        }
        self.structure_key = (
            self.source_ir.source_ir_id,
            DEFAULT_STRUCTURE_RESOLVER_ID,
            DEFAULT_STRUCTURE_RESOLVER_VERSION,
        )
        self.semantic_key = (
            self.tree.structure_snapshot_id,
            DEFAULT_SEMANTIC_CLASSIFIER_ID,
            DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        )
        self.view_key = (
            self.semantics.semantic_snapshot_id,
            DEFAULT_REQUIREMENT_PROJECTOR_ID,
            DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
        )

    def test_qsk_table_case_passes_all_four_formal_evaluators(self) -> None:
        oracle_outcome = runner._evaluate_oracle_fidelity(
            oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
        )
        structure_outcome = runner._evaluate_pdf_table_continuation(
            case=self.case,
            source_oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: self.tree},
        )
        main_outcome = runner._evaluate_section_requirement_projection(
            case=self.case,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: self.tree},
            semantic_snapshots={self.semantic_key: self.semantics},
            requirement_view_snapshots={self.view_key: self.view},
            evaluator_id=runner.SECTION_MAIN_REQUIREMENT_EVALUATOR_ID,
        )
        row_projection_outcome = runner._evaluate_pdf_table_rows_not_requirements(
            case=self.case,
            source_oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: self.tree},
            semantic_snapshots={self.semantic_key: self.semantics},
            requirement_view_snapshots={self.view_key: self.view},
        )

        self.assertEqual(oracle_outcome["status"], "PASS")
        self.assertEqual(
            oracle_outcome["evaluator"],
            runner.ORACLE_V2_EVALUATOR_ID,
        )
        self.assertEqual(
            oracle_outcome["evidence"]["expected_logical_slot_count"],
            8,
        )
        self.assertEqual(
            oracle_outcome["evidence"]["observed_logical_slot_count"],
            8,
        )
        self.assertEqual(structure_outcome["status"], "PASS")
        self.assertEqual(main_outcome["status"], "PASS")
        self.assertEqual(row_projection_outcome["status"], "PASS")
        self.assertEqual(
            structure_outcome["evidence"]["observed_table_row_boundary_count"],
            4,
        )
        self.assertEqual(
            structure_outcome["evaluator"],
            runner.PDF_TABLE_CONTINUATION_EVALUATOR_ID,
        )
        self.assertEqual(
            structure_outcome["evidence"]["presenter_table_segment_count"],
            2,
        )
        self.assertEqual(
            structure_outcome["evidence"]["presenter_logical_table_count"],
            1,
        )
        self.assertEqual(
            structure_outcome["evidence"]["presenter_context_table_count"],
            2,
        )
        self.assertEqual(
            structure_outcome["evidence"]["presenter_mismatch_count"],
            0,
        )
        self.assertEqual(
            row_projection_outcome["evidence"]["projected_table_row_count"],
            0,
        )
        self.assertEqual(
            row_projection_outcome["evaluator"],
            runner.PDF_TABLE_ROWS_NOT_REQUIREMENTS_EVALUATOR_ID,
        )

    def test_changed_row_fingerprint_fails_source_oracle(self) -> None:
        oracle = copy.deepcopy(self.oracle)
        oracle["fact"]["segments"][0]["rows"][1]["row_fingerprint"] = "f" * 64
        oracle["fact"]["group_fingerprint"] = (
            runner._fingerprint_pdf_table_segment_records(oracle["fact"]["segments"])
        )

        outcome = runner._evaluate_oracle_fidelity(
            oracle=oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["reason_code"], "oracle_fidelity_mismatch")
        self.assertGreater(outcome["evidence"]["mismatch_count"], 0)

    def test_changed_merge_topology_fails_v2_source_oracle(self) -> None:
        oracle = copy.deepcopy(self.oracle)
        oracle["fact"]["segments"][0]["merged_regions"] = [
            {"row": 0, "column": 0, "row_span": 1, "column_span": 2}
        ]
        oracle["fact"]["group_fingerprint"] = (
            runner._fingerprint_pdf_table_segment_records(oracle["fact"]["segments"])
        )

        outcome = runner._evaluate_oracle_fidelity(
            oracle=oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["reason_code"], "oracle_fidelity_mismatch")
        self.assertEqual(
            outcome["evidence"]["merge_topology_mismatch_count"],
            1,
        )

    def test_changed_source_content_fails_v2_source_oracle(self) -> None:
        table_node = next(
            node
            for node in self.source_ir.nodes
            if node.kind is SourceNodeKind.TABLE and node.table_payload is not None
        )
        table = table_node.table_payload
        changed_cell = table.cells[0].model_copy(
            update={"raw_text": f"{table.cells[0].raw_text} changed"}
        )
        changed_table = table.model_copy(
            update={"cells": (changed_cell, *table.cells[1:])}
        )
        changed_node = table_node.model_copy(update={"table_payload": changed_table})
        changed_source_ir = self.source_ir.model_copy(
            update={
                "nodes": tuple(
                    changed_node if node is table_node else node
                    for node in self.source_ir.nodes
                )
            }
        )

        outcome = runner._evaluate_oracle_fidelity(
            oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots={
                (changed_source_ir.source_sha256, "pdf"): changed_source_ir
            },
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["reason_code"], "oracle_fidelity_mismatch")
        self.assertGreater(outcome["evidence"]["mismatch_count"], 0)

    def test_changed_logical_slot_count_fails_v2_source_oracle(self) -> None:
        oracle = copy.deepcopy(self.oracle)
        oracle["fact"]["expected_logical_slot_count"] += 1

        outcome = runner._evaluate_oracle_fidelity(
            oracle=oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["reason_code"], "oracle_fidelity_mismatch")
        self.assertNotEqual(
            outcome["evidence"]["expected_logical_slot_count"],
            outcome["evidence"]["observed_logical_slot_count"],
        )

    def test_missing_continuation_relation_fails_structure_evaluator(self) -> None:
        tree = self.tree.model_copy(
            update={
                "relations": tuple(
                    relation
                    for relation in self.tree.relations
                    if relation.kind is not StructureRelationKind.CONTINUES
                )
            }
        )

        outcome = runner._evaluate_pdf_table_continuation(
            case=self.case,
            source_oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: tree},
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["evidence"]["continuation_relation_count"], 0)

    def test_duplicate_table_row_boundary_fails_structure_evaluator(self) -> None:
        row = next(
            node for node in self.tree.nodes if node.kind is StructureNodeKind.TABLE_ROW
        )
        tree = self.tree.model_copy(update={"nodes": (*self.tree.nodes, row)})

        outcome = runner._evaluate_pdf_table_continuation(
            case=self.case,
            source_oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: tree},
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertGreater(outcome["evidence"]["invalid_row_boundary_count"], 0)

    def test_broken_document_view_link_fails_v2_evaluator(self) -> None:
        document_view = runner.build_document_view(self.source_ir, self.tree)
        first_table = document_view["tables"][0]
        first_table["continuation"] = {
            **first_table["continuation"],
            "next_table_id": None,
        }

        with patch.object(
            runner,
            "build_document_view",
            return_value=document_view,
        ):
            outcome = runner._evaluate_pdf_table_continuation(
                case=self.case,
                source_oracle=self.oracle,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
                structure_snapshots={self.structure_key: self.tree},
            )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["reason_code"],
            "pdf_table_continuation_document_view_mismatch",
        )
        self.assertGreater(outcome["evidence"]["presenter_mismatch_count"], 0)

    def test_changed_logical_grid_count_fails_v2_scope(self) -> None:
        oracle = copy.deepcopy(self.oracle)
        oracle["fact"]["expected_logical_slot_count"] += 1

        outcome = runner._evaluate_pdf_table_continuation(
            case=self.case,
            source_oracle=oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: self.tree},
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["reason_code"],
            "pdf_table_representation_neutral_scope_mismatch",
        )
        self.assertGreater(
            outcome["evidence"]["representation_neutral_scope_mismatch_count"],
            0,
        )

    def test_changed_cell_text_fails_v2_logical_grid_fingerprint(self) -> None:
        table_node = next(
            node
            for node in self.source_ir.nodes
            if node.kind is SourceNodeKind.TABLE and node.table_payload is not None
        )
        table = table_node.table_payload
        first_cell = table.cells[0]
        changed_cell = first_cell.model_copy(
            update={"raw_text": f"{first_cell.raw_text} changed"}
        )
        changed_table = table.model_copy(
            update={"cells": (changed_cell, *table.cells[1:])}
        )
        changed_node = table_node.model_copy(update={"table_payload": changed_table})
        changed_source_ir = self.source_ir.model_copy(
            update={
                "nodes": tuple(
                    changed_node if node is table_node else node
                    for node in self.source_ir.nodes
                )
            }
        )
        changed_tree = build_structure_tree(changed_source_ir)

        outcome = runner._evaluate_pdf_table_continuation(
            case=self.case,
            source_oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots={
                (changed_source_ir.source_sha256, "pdf"): changed_source_ir
            },
            structure_snapshots={self.structure_key: changed_tree},
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["reason_code"],
            "pdf_table_representation_neutral_scope_mismatch",
        )
        self.assertTrue(outcome["evidence"]["source_fidelity_evaluated"])
        self.assertGreater(
            outcome["evidence"]["source_fidelity_mismatch_count"],
            0,
        )

    def test_presenter_measurement_error_is_structured_failure(self) -> None:
        with patch.object(
            runner,
            "_measure_document_view_table_continuation",
            side_effect=ValueError("invalid presenter payload"),
        ):
            outcome = runner._evaluate_pdf_table_continuation(
                case=self.case,
                source_oracle=self.oracle,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
                structure_snapshots={self.structure_key: self.tree},
            )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["reason_code"],
            "pdf_table_continuation_document_view_mismatch",
        )
        self.assertEqual(
            outcome["evidence"]["presenter_measurement_error_type"],
            "ValueError",
        )


class ExtractionCaseRunnerPdfTableNoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_ir, self.tree, self.semantics, self.view = (
            _build_f06_native_pipeline()
        )
        self.case = {
            "document_ids": ["DOC-F06-ACTIVE-SUSPENSION"],
            "locators": [
                {
                    "document_id": "DOC-F06-ACTIVE-SUSPENSION",
                    "kind": "section",
                    "pointer": "9.16.2",
                },
                {
                    "document_id": "DOC-F06-ACTIVE-SUSPENSION",
                    "kind": "page_region",
                    "pointer": "PDF page 28 / printed page 20; Table 5",
                },
            ],
        }
        self.fact = _pdf_table_note_fact(self.source_ir)
        self.oracle = {
            "oracle_id": "ORACLE-TAB-005-A5",
            "document_id": "DOC-F06-ACTIVE-SUSPENSION",
            "fact": self.fact,
        }
        self.document_results = {
            "DOC-F06-ACTIVE-SUSPENSION": {
                "source_ir_status": "COMPLETE",
                "source_sha256": self.source_ir.source_sha256,
                "detected_format": "pdf",
            }
        }
        self.source_ir_snapshots = {
            (self.source_ir.source_sha256, "pdf"): self.source_ir
        }
        self.structure_key = (
            self.source_ir.source_ir_id,
            DEFAULT_STRUCTURE_RESOLVER_ID,
            DEFAULT_STRUCTURE_RESOLVER_VERSION,
        )
        self.semantic_key = (
            self.tree.structure_snapshot_id,
            DEFAULT_SEMANTIC_CLASSIFIER_ID,
            DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        )
        self.view_key = (
            self.semantics.semantic_snapshot_id,
            DEFAULT_REQUIREMENT_PROJECTOR_ID,
            DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
        )

    def _projection_kwargs(self, *, view: object | None = None) -> dict[str, object]:
        return {
            "case": self.case,
            "source_oracle": self.oracle,
            "document_results": self.document_results,
            "source_ir_snapshots": self.source_ir_snapshots,
            "structure_snapshots": {self.structure_key: self.tree},
            "semantic_snapshots": {self.semantic_key: self.semantics},
            "requirement_view_snapshots": {
                self.view_key: self.view if view is None else view
            },
        }

    def test_f06_table_note_case_passes_all_six_formal_evaluators(self) -> None:
        outcomes = [
            runner._evaluate_section_requirement_projection(
                case=self.case,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
                structure_snapshots={self.structure_key: self.tree},
                semantic_snapshots={self.semantic_key: self.semantics},
                requirement_view_snapshots={self.view_key: self.view},
                evaluator_id=runner.SECTION_MAIN_ROLLUP_EVALUATOR_ID,
                expected_decision_mode=runner.RequirementDecisionMode.ROLLUP,
            ),
            runner._evaluate_pdf_table_parameter_count(**self._projection_kwargs()),
            runner._evaluate_pdf_table_note_applies_to(
                case=self.case,
                source_oracle=self.oracle,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
                structure_snapshots={self.structure_key: self.tree},
            ),
            runner._evaluate_pdf_section_child_assertion(**self._projection_kwargs()),
            runner._evaluate_oracle_fidelity(
                oracle=self.oracle,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
            ),
            runner._evaluate_pdf_duplicate_table_label(
                case=self.case,
                source_oracle=self.oracle,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
                structure_snapshots={self.structure_key: self.tree},
            ),
        ]

        self.assertEqual([outcome["status"] for outcome in outcomes], ["PASS"] * 6)

    def test_table_note_relation_missing_one_target_fails_closed(self) -> None:
        note_row_id = next(
            node.structure_node_id
            for node in self.tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.table_row_index == self.fact["note_row_index"]
        )
        removed = False
        relations = []
        for relation in self.tree.relations:
            if (
                relation.kind is StructureRelationKind.APPLIES_TO
                and relation.source_structure_node_id == note_row_id
                and not removed
            ):
                removed = True
                continue
            relations.append(relation)
        self.assertTrue(removed)
        tree = self.tree.model_copy(update={"relations": tuple(relations)})

        outcome = runner._evaluate_pdf_table_note_applies_to(
            case=self.case,
            source_oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: tree},
        )

        self.assertEqual(outcome["status"], "FAIL")

    def test_third_child_order_tampering_fails_closed(self) -> None:
        child_record = self.fact["child_assertion"]
        child_locator = child_record["locator"]
        child_source = next(
            node
            for node in self.source_ir.nodes
            if node.native_locator.block_index == child_locator["block_index"]
        )
        child_structure = next(
            node
            for node in self.tree.nodes
            if node.identity_key == f"source:{child_source.source_node_id}"
        )
        view = self.view.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(update={"source_order": 0})
                    if node.target_structure_node_id
                    == child_structure.structure_node_id
                    else node
                    for node in self.view.nodes
                )
            }
        )

        outcome = runner._evaluate_pdf_section_child_assertion(
            **self._projection_kwargs(view=view)
        )

        self.assertEqual(outcome["status"], "FAIL")

    def test_duplicate_caption_error_severity_fails_closed(self) -> None:
        tree = self.tree.model_copy(
            update={
                "findings": tuple(
                    finding.model_copy(
                        update={
                            "severity": StructureFindingSeverity.ERROR,
                            "blocking": True,
                        }
                    )
                    if finding.code == "duplicate_table_caption_label"
                    else finding
                    for finding in self.tree.findings
                )
            }
        )

        outcome = runner._evaluate_pdf_duplicate_table_label(
            case=self.case,
            source_oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: tree},
        )

        self.assertEqual(outcome["status"], "FAIL")

    def test_changed_label_hash_fails_source_oracle(self) -> None:
        oracle = copy.deepcopy(self.oracle)
        oracle["fact"]["label_records"][0]["raw_sha256"] = "f" * 64
        oracle["fact"]["label_fingerprint"] = (
            runner._fingerprint_pdf_text_locator_records(
                oracle["fact"]["label_records"]
            )
        )
        oracle["fact"]["group_fingerprint"] = (
            runner._fingerprint_pdf_table_note_label_group(
                table=oracle["fact"]["table"],
                note_row_index=oracle["fact"]["note_row_index"],
                parameter_row_indices=oracle["fact"]["parameter_row_indices"],
                label_records=oracle["fact"]["label_records"],
                duplicate_caption_records=oracle["fact"]["duplicate_caption_records"],
                child_assertion=oracle["fact"]["child_assertion"],
            )
        )

        outcome = runner._evaluate_oracle_fidelity(
            oracle=oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
        )

        self.assertEqual(outcome["status"], "FAIL")

    def test_manifest_rejects_parameter_count_oracle_self_redefinition(self) -> None:
        manifest = json.loads(
            require_private_eval("extraction_cases/manifest.json").read_text()
        )
        registry = json.loads(
            require_private_eval("extraction_cases/oracles.json").read_text()
        )
        fact = registry["oracles"]["ORACLE-TAB-005-A5"]["fact"]
        fact["parameter_row_indices"] = [1]
        fact["group_fingerprint"] = runner._fingerprint_pdf_table_note_label_group(
            table=fact["table"],
            note_row_index=fact["note_row_index"],
            parameter_row_indices=fact["parameter_row_indices"],
            label_records=fact["label_records"],
            duplicate_caption_records=fact["duplicate_caption_records"],
            child_assertion=fact["child_assertion"],
        )

        with self.assertRaises(runner.ManifestValidationError):
            runner.validate_manifest(manifest, oracle_registry=registry)


class ExtractionCaseRunnerPdfOptionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_ir, self.tree, self.semantics, self.view = (
            _build_pds_native_pipeline()
        )
        self.fact = _pdf_option_state_fact(self.source_ir, self.tree)
        self.oracle = {
            "oracle_id": "ORACLE-FMT-007-A1",
            "document_id": "DOC-PDS-OIL-PUMP",
            "evaluator_id": runner.ORACLE_EVALUATOR_ID,
            "fact": self.fact,
        }
        self.document_results = {
            "DOC-PDS-OIL-PUMP": {
                "source_ir_status": "COMPLETE",
                "source_sha256": self.source_ir.source_sha256,
                "detected_format": "pdf",
            }
        }
        self.source_ir_snapshots = {
            (self.source_ir.source_sha256, "pdf"): self.source_ir
        }
        self.structure_key = (
            self.source_ir.source_ir_id,
            DEFAULT_STRUCTURE_RESOLVER_ID,
            DEFAULT_STRUCTURE_RESOLVER_VERSION,
        )
        self.semantic_key = (
            self.tree.structure_snapshot_id,
            DEFAULT_SEMANTIC_CLASSIFIER_ID,
            DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        )
        self.view_key = (
            self.semantics.semantic_snapshot_id,
            DEFAULT_REQUIREMENT_PROJECTOR_ID,
            DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
        )

    def _structure_kwargs(self, *, tree: object | None = None) -> dict[str, object]:
        return {
            "source_oracle": self.oracle,
            "document_results": self.document_results,
            "source_ir_snapshots": self.source_ir_snapshots,
            "structure_snapshots": {
                self.structure_key: self.tree if tree is None else tree
            },
        }

    def _semantic_kwargs(self) -> dict[str, object]:
        return {
            **self._structure_kwargs(),
            "semantic_snapshots": {self.semantic_key: self.semantics},
        }

    def _projection_kwargs(self, *, view: object | None = None) -> dict[str, object]:
        return {
            **self._semantic_kwargs(),
            "requirement_view_snapshots": {
                self.view_key: self.view if view is None else view
            },
        }

    def test_pds_option_case_passes_all_eight_formal_evaluators(self) -> None:
        outcomes = [
            runner._evaluate_oracle_fidelity(
                oracle=self.oracle,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
            ),
            runner._evaluate_pdf_option_section_semantic(
                evaluator_id=runner.PDF_OPTION_RISK_SEMANTIC_EVALUATOR_ID,
                section_label="3.3",
                expected_semantic_class=runner.SemanticClass.PROJECT_RISK,
                expected_requirement_subtype=None,
                **self._semantic_kwargs(),
            ),
            runner._evaluate_pdf_option_section_no_requirements(
                **self._projection_kwargs()
            ),
            runner._evaluate_pdf_option_section_semantic(
                evaluator_id=runner.PDF_OPTION_REGULATION_SEMANTIC_EVALUATOR_ID,
                section_label="4.1",
                expected_semantic_class=runner.SemanticClass.REQUIREMENT,
                expected_requirement_subtype=(
                    runner.RequirementSubtype.REGULATION_REQUIREMENT
                ),
                **self._semantic_kwargs(),
            ),
            runner._evaluate_pdf_checked_option_applicability(
                **self._structure_kwargs()
            ),
            runner._evaluate_pdf_option_top_level_requirement(
                **self._projection_kwargs()
            ),
            runner._evaluate_pdf_unchecked_option_not_effective(
                **self._projection_kwargs()
            ),
            runner._evaluate_pdf_option_uncertainty_policy(**self._structure_kwargs()),
        ]

        self.assertEqual([outcome["status"] for outcome in outcomes], ["PASS"] * 8)
        self.assertEqual(
            outcomes[0]["evidence"]["observed_primary_count"],
            18,
        )
        self.assertEqual(
            outcomes[4]["evidence"]["observed_checked_region_count"],
            3,
        )
        self.assertEqual(
            outcomes[5]["evidence"]["top_level_requirement_count"],
            1,
        )
        self.assertEqual(
            outcomes[6]["evidence"]["projected_nonchecked_option_count"],
            0,
        )

    def test_changed_visible_option_state_fails_source_oracle(self) -> None:
        changed_source_ir = self.source_ir.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(
                        update={"raw_text": node.raw_text.replace("√", " ", 1)}
                    )
                    if node.native_locator.page_number == 9
                    and node.native_locator.block_index == 11
                    else node
                    for node in self.source_ir.nodes
                )
            }
        )

        outcome = runner._evaluate_oracle_fidelity(
            oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots={
                (changed_source_ir.source_sha256, "pdf"): changed_source_ir
            },
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["reason_code"], "oracle_fidelity_mismatch")

    def test_changed_option_relation_state_fails_structure_scope(self) -> None:
        changed = False
        relations = []
        for relation in self.tree.relations:
            if relation.kind is StructureRelationKind.OPTION_STATE and not changed:
                relations.append(
                    relation.model_copy(
                        update={
                            "option_state": (
                                StructureOptionState.UNCHECKED
                                if relation.option_state is StructureOptionState.CHECKED
                                else StructureOptionState.CHECKED
                            )
                        }
                    )
                )
                changed = True
            else:
                relations.append(relation)
        tree = self.tree.model_copy(update={"relations": tuple(relations)})

        outcome = runner._evaluate_pdf_checked_option_applicability(
            **self._structure_kwargs(tree=tree)
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["reason_code"],
            "pdf_option_structure_scope_mismatch",
        )

    def test_removed_primary_projection_fails_top_level_contract(self) -> None:
        section = next(
            node
            for node in self.tree.nodes
            if node.kind is StructureNodeKind.SECTION and node.explicit_label == "4.1"
        )
        view = self.view.model_copy(
            update={
                "nodes": tuple(
                    node
                    for node in self.view.nodes
                    if node.target_structure_node_id != section.structure_node_id
                )
            }
        )

        outcome = runner._evaluate_pdf_option_top_level_requirement(
            **self._projection_kwargs(view=view)
        )

        self.assertEqual(outcome["status"], "FAIL")

    def test_uncertain_option_requires_blocking_manual_confirmation(self) -> None:
        uncertain_source_ir = self.source_ir.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(
                        update={"raw_text": node.raw_text.replace("[ √ ]", "[ ? ]", 1)}
                    )
                    if node.native_locator.page_number == 9
                    and node.native_locator.block_index == 11
                    else node
                    for node in self.source_ir.nodes
                )
            }
        )
        tree = build_structure_tree(uncertain_source_ir)
        semantics = classify_content(uncertain_source_ir, tree)
        view = project_requirements(uncertain_source_ir, tree, semantics)
        oracle = {
            **self.oracle,
            "fact": _pdf_option_state_fact(uncertain_source_ir, tree),
        }
        structure_key = (
            uncertain_source_ir.source_ir_id,
            DEFAULT_STRUCTURE_RESOLVER_ID,
            DEFAULT_STRUCTURE_RESOLVER_VERSION,
        )
        semantic_key = (
            tree.structure_snapshot_id,
            DEFAULT_SEMANTIC_CLASSIFIER_ID,
            DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        )
        view_key = (
            semantics.semantic_snapshot_id,
            DEFAULT_REQUIREMENT_PROJECTOR_ID,
            DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
        )
        common = {
            "source_oracle": oracle,
            "document_results": self.document_results,
            "source_ir_snapshots": {
                (uncertain_source_ir.source_sha256, "pdf"): uncertain_source_ir
            },
            "structure_snapshots": {structure_key: tree},
        }

        finding_outcome = runner._evaluate_pdf_option_uncertainty_policy(**common)
        projection_outcome = runner._evaluate_pdf_unchecked_option_not_effective(
            **common,
            semantic_snapshots={semantic_key: semantics},
            requirement_view_snapshots={view_key: view},
        )
        tree_without_finding = tree.model_copy(update={"findings": ()})
        missing_finding_outcome = runner._evaluate_pdf_option_uncertainty_policy(
            **{
                **common,
                "structure_snapshots": {structure_key: tree_without_finding},
            }
        )

        self.assertEqual(finding_outcome["status"], "PASS")
        self.assertEqual(
            finding_outcome["evidence"]["valid_manual_confirmation_finding_count"],
            1,
        )
        self.assertEqual(projection_outcome["status"], "PASS")
        self.assertEqual(missing_finding_outcome["status"], "FAIL")


class ExtractionCaseRunnerPdfRevisionHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        active_source_ir = _complete_revision_table_grid(
            _build_pds_active_damper_pipeline()[0]
        )
        oil_source_ir = _complete_revision_table_grid(_build_pds_native_pipeline()[0])
        self.pipelines = {
            "DOC-PDS-ACTIVE-DAMPER": _rebuild_native_pipeline(active_source_ir),
            "DOC-PDS-OIL-PUMP": _rebuild_native_pipeline(oil_source_ir),
        }
        self.fact = _pdf_revision_history_fact(
            {
                document_id: pipeline[0]
                for document_id, pipeline in self.pipelines.items()
            }
        )
        self.oracle = {
            "oracle_id": "ORACLE-FMT-002-A1",
            "evaluator_id": runner.ORACLE_EVALUATOR_ID,
            "document_ids": list(self.pipelines),
            "source_sha256_by_document": {
                document_id: pipeline[0].source_sha256
                for document_id, pipeline in self.pipelines.items()
            },
            "fact": self.fact,
        }
        self.document_results = {
            document_id: {
                "source_ir_status": "COMPLETE",
                "source_sha256": pipeline[0].source_sha256,
                "detected_format": "pdf",
            }
            for document_id, pipeline in self.pipelines.items()
        }
        self.source_ir_snapshots = {
            (pipeline[0].source_sha256, "pdf"): pipeline[0]
            for pipeline in self.pipelines.values()
        }
        self.structure_snapshots = {
            (
                pipeline[0].source_ir_id,
                DEFAULT_STRUCTURE_RESOLVER_ID,
                DEFAULT_STRUCTURE_RESOLVER_VERSION,
            ): pipeline[1]
            for pipeline in self.pipelines.values()
        }
        self.semantic_snapshots = {
            (
                pipeline[1].structure_snapshot_id,
                DEFAULT_SEMANTIC_CLASSIFIER_ID,
                DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            ): pipeline[2]
            for pipeline in self.pipelines.values()
        }
        self.requirement_view_snapshots = {
            (
                pipeline[2].semantic_snapshot_id,
                DEFAULT_REQUIREMENT_PROJECTOR_ID,
                DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
                RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
            ): pipeline[3]
            for pipeline in self.pipelines.values()
        }

    def _source_kwargs(self) -> dict[str, object]:
        return {
            "source_oracle": self.oracle,
            "document_results": self.document_results,
            "source_ir_snapshots": self.source_ir_snapshots,
        }

    def _projection_kwargs(self) -> dict[str, object]:
        return {
            **self._source_kwargs(),
            "structure_snapshots": self.structure_snapshots,
            "semantic_snapshots": self.semantic_snapshots,
            "requirement_view_snapshots": self.requirement_view_snapshots,
        }

    def _semantic_kwargs(self) -> dict[str, object]:
        return {
            **self._source_kwargs(),
            "structure_snapshots": self.structure_snapshots,
            "semantic_snapshots": self.semantic_snapshots,
        }

    def test_two_pds_versions_pass_all_five_formal_evaluators(self) -> None:
        outcomes = [
            runner._evaluate_oracle_fidelity(
                oracle=self.oracle,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
            ),
            runner._evaluate_pdf_revision_history_semantic(**self._semantic_kwargs()),
            runner._evaluate_pdf_revision_history_not_requirements(
                **self._projection_kwargs()
            ),
            runner._evaluate_pdf_revision_history_no_inference(
                **self._projection_kwargs()
            ),
            runner._evaluate_pdf_revision_snapshots_distinct(**self._source_kwargs()),
        ]

        self.assertEqual([outcome["status"] for outcome in outcomes], ["PASS"] * 5)
        self.assertEqual(
            outcomes[0]["evidence"]["observed_logical_slot_count"],
            1254,
        )
        self.assertEqual(
            outcomes[1]["evidence"]["valid_version_metadata_annotation_count"],
            3,
        )
        self.assertEqual(
            outcomes[2]["evidence"]["projected_revision_row_count"],
            0,
        )
        self.assertEqual(
            outcomes[4]["evidence"]["distinct_revision_table_fingerprint_count"],
            2,
        )

    def test_recomputed_wrong_revision_row_scope_fails_source_oracle(self) -> None:
        oracle = copy.deepcopy(self.oracle)
        document = oracle["fact"]["documents"][0]
        document["revision_row_indices"] = [8]
        document["table_fingerprint"] = (
            runner._fingerprint_pdf_revision_history_document(document)
        )
        oracle["fact"]["group_fingerprint"] = (
            runner._fingerprint_pdf_revision_history_group(oracle["fact"]["documents"])
        )

        outcome = runner._evaluate_oracle_fidelity(
            oracle=oracle,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["reason_code"], "oracle_fidelity_mismatch")

    def test_snapshot_distinction_is_owned_by_workflow_evaluator(self) -> None:
        first_source_ir_id = next(iter(self.pipelines.values()))[0].source_ir_id
        changed_snapshots = {}
        for index, pipeline in enumerate(self.pipelines.values()):
            source_ir = pipeline[0]
            changed = (
                source_ir
                if index == 0
                else source_ir.model_copy(update={"source_ir_id": first_source_ir_id})
            )
            changed_snapshots[(changed.source_sha256, "pdf")] = changed

        source_outcome = runner._evaluate_oracle_fidelity(
            oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots=changed_snapshots,
        )
        workflow_outcome = runner._evaluate_pdf_revision_snapshots_distinct(
            source_oracle=self.oracle,
            document_results=self.document_results,
            source_ir_snapshots=changed_snapshots,
        )

        self.assertEqual(source_outcome["status"], "PASS")
        self.assertEqual(workflow_outcome["status"], "FAIL")
        self.assertEqual(
            workflow_outcome["evidence"]["distinct_source_ir_count"],
            1,
        )

    def test_manifest_rejects_composite_oracle_document_drift(self) -> None:
        manifest = json.loads(
            require_private_eval("extraction_cases/manifest.json").read_text()
        )
        registry = json.loads(
            require_private_eval("extraction_cases/oracles.json").read_text()
        )
        registry["oracles"]["ORACLE-FMT-002-A1"]["document_ids"].reverse()

        with self.assertRaises(runner.ManifestValidationError):
            runner.validate_manifest(manifest, oracle_registry=registry)


def _complete_revision_table_grid(source_ir: object) -> object:
    nodes = []
    for node in source_ir.nodes:
        table = node.table_payload
        if node.kind is not SourceNodeKind.TABLE or table is None:
            nodes.append(node)
            continue
        expanded_cells = tuple(
            cell.model_copy(update={"column_span": table.column_count})
            for cell in table.cells
        )
        nodes.append(
            node.model_copy(
                update={
                    "table_payload": table.model_copy(update={"cells": expanded_cells})
                }
            )
        )
    return source_ir.model_copy(update={"nodes": tuple(nodes)})


def _rebuild_native_pipeline(
    source_ir: object,
) -> tuple[object, object, object, object]:
    tree = build_structure_tree(source_ir)
    semantics = classify_content(source_ir, tree)
    view = project_requirements(source_ir, tree, semantics)
    return source_ir, tree, semantics, view


def _pdf_revision_history_fact(
    source_irs_by_document: dict[str, object],
) -> dict[str, object]:
    documents = []
    for document_id, source_ir in sorted(source_irs_by_document.items()):
        document, _, _ = runner._measure_pdf_revision_history_document(
            source_ir,
            {
                "document_id": document_id,
                "table": {
                    "locator": {
                        "format": "pdf",
                        "page_number": 1,
                        "table_index": 0,
                    }
                },
            },
        )
        if document is None:
            raise AssertionError("revision-history fixture table is unavailable")
        documents.append(document)
    fact = {
        "kind": "pdf_revision_history_group",
        "expected_document_count": len(documents),
        "expected_revision_row_count": sum(
            len(document["revision_row_indices"]) for document in documents
        ),
        "documents": documents,
    }
    fact["group_fingerprint"] = runner._fingerprint_pdf_revision_history_group(
        documents
    )
    return fact


def _pdf_table_fact(source_ir: object) -> dict[str, object]:
    segments: list[dict[str, object]] = []
    for page_number in (66, 67):
        node = next(
            item
            for item in source_ir.nodes
            if item.kind is SourceNodeKind.TABLE
            and item.native_locator.page_number == page_number
            and item.native_locator.table_index == 0
        )
        table = node.table_payload
        coverage = {
            (covered_row, covered_column): cell
            for cell in table.cells
            for covered_row in range(cell.row, cell.row + cell.row_span)
            for covered_column in range(
                cell.column,
                cell.column + cell.column_span,
            )
        }
        rows: list[dict[str, object]] = []
        for row_index in range(table.row_count):
            cell_records = []
            for column_index in range(table.column_count):
                anchor = coverage[(row_index, column_index)]
                raw_text = (
                    anchor.raw_text
                    if (anchor.row, anchor.column) == (row_index, column_index)
                    else ""
                )
                cell_records.append(
                    {
                        "column": column_index,
                        "row_span": 1,
                        "column_span": 1,
                        "raw_sha256": runner._sha256_text(raw_text),
                    }
                )
            rows.append(
                {
                    "row_index": row_index,
                    "logical_slot_count": len(cell_records),
                    "row_fingerprint": runner._sha256_text(
                        runner._canonical_json_payload(cell_records)
                    ),
                }
            )
        segments.append(
            {
                "locator": {
                    "format": "pdf",
                    "page_number": page_number,
                    "table_index": 0,
                },
                "row_count": table.row_count,
                "column_count": table.column_count,
                "logical_slot_count": table.row_count * table.column_count,
                "merged_regions": [
                    {
                        "row": region.row,
                        "column": region.column,
                        "row_span": region.row_span,
                        "column_span": region.column_span,
                    }
                    for region in sorted(
                        table.merged_regions,
                        key=lambda item: (
                            item.row,
                            item.column,
                            item.row_span,
                            item.column_span,
                        ),
                    )
                ],
                "rows": rows,
            }
        )
    return {
        "kind": "pdf_table_continuation_logical_grid",
        "expected_segment_count": 2,
        "expected_row_count": sum(int(segment["row_count"]) for segment in segments),
        "expected_logical_slot_count": sum(
            int(segment["logical_slot_count"]) for segment in segments
        ),
        "continuation_from_page": 66,
        "continuation_to_page": 67,
        "segments": segments,
        "group_fingerprint": runner._fingerprint_pdf_table_segment_records(segments),
    }


def _pdf_table_note_fact(source_ir: object) -> dict[str, object]:
    table_node = next(
        node
        for node in source_ir.nodes
        if node.kind is SourceNodeKind.TABLE
        and node.native_locator.page_number == 28
        and node.native_locator.table_index == 0
    )
    table = table_node.table_payload
    rows: list[dict[str, object]] = []
    for row_index in range(table.row_count):
        cells = sorted(
            (cell for cell in table.cells if cell.row == row_index),
            key=lambda cell: (cell.column, cell.row_span, cell.column_span),
        )
        cell_records = [
            {
                "column": cell.column,
                "row_span": cell.row_span,
                "column_span": cell.column_span,
                "raw_sha256": runner._sha256_text(cell.raw_text),
            }
            for cell in cells
        ]
        rows.append(
            {
                "row_index": row_index,
                "cell_count": len(cells),
                "row_fingerprint": runner._sha256_text(
                    runner._canonical_json_payload(cell_records)
                ),
            }
        )
    table_record = {
        "locator": {"format": "pdf", "page_number": 28, "table_index": 0},
        "row_count": table.row_count,
        "column_count": table.column_count,
        "cell_count": len(table.cells),
        "merged_region_count": len(table.merged_regions),
        "rows": rows,
    }

    paragraphs = [
        node for node in source_ir.nodes if node.kind is SourceNodeKind.PARAGRAPH
    ]

    def paragraph_starting(prefix: str, *, last: bool = False) -> object:
        matches = [node for node in paragraphs if node.raw_text.startswith(prefix)]
        return matches[-1] if last else matches[0]

    def source_record(node: object) -> dict[str, object]:
        return {
            "kind": "source_node",
            "locator": {
                "format": "pdf",
                "page_number": node.native_locator.page_number,
                "block_index": node.native_locator.block_index,
                "source_node_kind": node.kind.value,
            },
            "raw_sha256": runner._sha256_text(node.raw_text),
        }

    first_caption = paragraph_starting("Table 5")
    second_caption = paragraph_starting("Table 5", last=True)
    label_records = [
        source_record(paragraph_starting("9.16.2")),
        source_record(first_caption),
        source_record(paragraph_starting("Synthetic descriptive context alpha")),
        source_record(paragraph_starting("9.16.4")),
        source_record(second_caption),
    ]
    note_cell = next(cell for cell in table.cells if cell.row == 3 and cell.column == 0)
    label_records.append(
        {
            "kind": "table_cell",
            "locator": {
                "format": "pdf",
                "page_number": note_cell.native_locator.page_number,
                "table_index": note_cell.native_locator.table_index,
                "row_index": note_cell.native_locator.row_index,
                "column_index": note_cell.native_locator.column_index,
                "row_span": note_cell.row_span,
                "column_span": note_cell.column_span,
            },
            "raw_sha256": runner._sha256_text(note_cell.raw_text),
        }
    )
    label_records.sort(key=runner._pdf_text_locator_record_key)
    duplicate_caption_records = [
        source_record(first_caption),
        source_record(second_caption),
    ]
    child_assertion = source_record(paragraph_starting("Surface shall"))
    fact = {
        "kind": "pdf_table_note_label_group",
        "section_label": "9.16.2",
        "table": table_record,
        "note_row_index": 3,
        "parameter_row_indices": [1, 2],
        "expected_label_record_count": len(label_records),
        "label_records": label_records,
        "label_fingerprint": runner._fingerprint_pdf_text_locator_records(
            label_records
        ),
        "duplicate_caption_records": duplicate_caption_records,
        "child_assertion": child_assertion,
    }
    fact["group_fingerprint"] = runner._fingerprint_pdf_table_note_label_group(
        table=table_record,
        note_row_index=3,
        parameter_row_indices=[1, 2],
        label_records=label_records,
        duplicate_caption_records=duplicate_caption_records,
        child_assertion=child_assertion,
    )
    return fact


def _pdf_option_state_fact(source_ir: object, tree: object) -> dict[str, object]:
    source_nodes_by_id = {node.source_node_id: node for node in source_ir.nodes}
    sections: list[dict[str, object]] = []
    for section_label in ("3.3", "4.1"):
        section = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.SECTION
            and node.explicit_label == section_label
        )
        descendant_ids = runner._structure_descendant_ids(
            tree,
            section.structure_node_id,
        )
        records: list[dict[str, object]] = []
        for structure_node in sorted(
            tree.nodes,
            key=lambda node: (node.source_order, node.structure_node_id),
        ):
            if (
                structure_node.structure_node_id not in descendant_ids
                or len(structure_node.source_refs) != 1
                or structure_node.source_refs[0].kind
                is not StructureSourceRefKind.SOURCE_NODE
            ):
                continue
            source_node = source_nodes_by_id.get(
                structure_node.source_refs[0].source_id
            )
            if (
                source_node is None
                or source_node.native_locator.page_number != 9
                or source_node.native_locator.block_index is None
            ):
                continue
            state = runner._pdf_option_state_from_text(source_node.raw_text)
            if state is None:
                continue
            records.append(
                {
                    "locator": {
                        "format": "pdf",
                        "page_number": 9,
                        "block_index": source_node.native_locator.block_index,
                        "source_node_kind": source_node.kind.value,
                    },
                    "raw_sha256": runner._sha256_text(source_node.raw_text),
                    "state": state.value,
                }
            )
        sections.append(
            {
                "section_label": section_label,
                "expected_option_count": len(records),
                "options": records,
                "option_fingerprint": (
                    runner._fingerprint_pdf_option_state_records(records)
                ),
            }
        )
    fact = {
        "kind": "pdf_option_state_group",
        "page_number": 9,
        "expected_section_count": len(sections),
        "expected_option_count": sum(
            int(section["expected_option_count"]) for section in sections
        ),
        "sections": sections,
    }
    fact["group_fingerprint"] = runner._fingerprint_pdf_option_state_group(
        page_number=9,
        sections=sections,
    )
    return fact


if __name__ == "__main__":
    unittest.main()
