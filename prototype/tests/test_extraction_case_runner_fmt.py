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
from app.domain.extraction.semantics import SemanticClass
from app.domain.extraction.structure import StructureBoundaryBasis
from scripts import extraction_case_runner as runner

if __package__:
    from .test_structure_tree import _fidelity_layout_source_snapshot
else:
    from test_structure_tree import _fidelity_layout_source_snapshot


class ExtractionCaseRunnerFmtTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_ir = _fidelity_layout_source_snapshot()
        self.tree = build_structure_tree(self.source_ir)
        self.semantics = classify_content(self.source_ir, self.tree)
        self.view = project_requirements(
            self.source_ir,
            self.tree,
            self.semantics,
        )
        self.document_ids = ["DOC-FMT-A", "DOC-FMT-B"]
        self.case = {
            "document_ids": self.document_ids,
            "locators": [
                {
                    "document_id": document_id,
                    "kind": "page_region",
                    "pointer": "toc=2-3;body=4-5",
                }
                for document_id in self.document_ids
            ],
        }
        self.manifest_documents = {
            document_id: {
                "format": "pdf",
                "source_sha256": self.source_ir.source_sha256,
            }
            for document_id in self.document_ids
        }
        self.document_results = {
            document_id: {
                "declared_format": "pdf",
                "detected_format": "pdf",
                "identity_status": "VERIFIED",
                "preflight_status": "READY",
                "page_count": 5,
                "source_ir_status": "COMPLETE",
                "source_sha256": self.source_ir.source_sha256,
            }
            for document_id in self.document_ids
        }
        self.source_ir_snapshots = {
            (self.source_ir.source_sha256, "pdf"): self.source_ir
        }
        candidate_fingerprint = runner._pdf_toc_candidate_fingerprint(
            self.source_ir,
            runner._pdf_toc_candidate_source_refs(
                self.source_ir,
                expected_pages={2, 3},
            ),
        )
        self.expected_candidate_fingerprints = {
            document_id: candidate_fingerprint
            for document_id in self.document_ids
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

    def test_registered_source_toc_and_noise_evaluators_pass(self) -> None:
        source_outcome = runner._evaluate_pdf_registered_source_identity(
            case=self.case,
            manifest_documents=self.manifest_documents,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
        )
        toc_outcome = runner._evaluate_pdf_single_toc_noise_block(
            case=self.case,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: self.tree},
            semantic_snapshots={self.semantic_key: self.semantics},
            expected_candidate_fingerprints_by_document=(
                self.expected_candidate_fingerprints
            ),
        )
        noise_outcome = runner._evaluate_pdf_page_noise_not_requirements(
            case=self.case,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: self.tree},
            semantic_snapshots={self.semantic_key: self.semantics},
            requirement_view_snapshots={self.view_key: self.view},
        )

        self.assertEqual(source_outcome["status"], "PASS")
        self.assertEqual(toc_outcome["status"], "PASS")
        self.assertEqual(noise_outcome["status"], "PASS")
        self.assertEqual(toc_outcome["evidence"]["verified_document_count"], 2)
        self.assertEqual(
            noise_outcome["evidence"]["projected_noise_full_context_ref_count"],
            0,
        )

    def test_one_bad_document_fails_multi_document_source_identity(self) -> None:
        document_results = copy.deepcopy(self.document_results)
        document_results["DOC-FMT-B"]["page_count"] = 4

        outcome = runner._evaluate_pdf_registered_source_identity(
            case=self.case,
            manifest_documents=self.manifest_documents,
            document_results=document_results,
            source_ir_snapshots=self.source_ir_snapshots,
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["evidence"]["verified_document_count"], 1)
        self.assertEqual(outcome["evidence"]["page_inventory_mismatch_count"], 1)

    def test_changed_toc_page_scope_fails(self) -> None:
        case = copy.deepcopy(self.case)
        case["locators"][0]["pointer"] = "toc=2-2;body=4-5"

        outcome = runner._evaluate_pdf_single_toc_noise_block(
            case=case,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: self.tree},
            semantic_snapshots={self.semantic_key: self.semantics},
            expected_candidate_fingerprints_by_document=(
                self.expected_candidate_fingerprints
            ),
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["evidence"]["toc_page_scope_mismatch_count"], 1)

    def test_non_noise_toc_semantics_fail_closed(self) -> None:
        toc_node = next(
            node
            for node in self.tree.nodes
            if node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK
        )
        tampered_semantics = self.semantics.model_copy(
            update={
                "annotations": tuple(
                    annotation.model_copy(
                        update={"semantic_class": SemanticClass.HEADING}
                    )
                    if annotation.target_structure_node_id
                    == toc_node.structure_node_id
                    else annotation
                    for annotation in self.semantics.annotations
                )
            }
        )

        outcome = runner._evaluate_pdf_single_toc_noise_block(
            case=self.case,
            document_results=self.document_results,
            source_ir_snapshots=self.source_ir_snapshots,
            structure_snapshots={self.structure_key: self.tree},
            semantic_snapshots={self.semantic_key: tampered_semantics},
            expected_candidate_fingerprints_by_document=(
                self.expected_candidate_fingerprints
            ),
        )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["evidence"]["toc_semantic_mismatch_count"], 2)

    def test_toc_evaluator_fails_when_declared_pages_have_uncovered_text(self) -> None:
        tampered_tree, tampered_semantics = self._toc_context_without_two_refs()

        with patch.object(
            runner,
            "_pdf_semantic_context_for_document",
            return_value=(self.source_ir, tampered_tree, tampered_semantics),
        ):
            outcome = runner._evaluate_pdf_single_toc_noise_block(
                case=self.case,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
                structure_snapshots={},
                semantic_snapshots={},
                expected_candidate_fingerprints_by_document=(
                    self.expected_candidate_fingerprints
                ),
            )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["evidence"]["toc_uncovered_source_ref_count"],
            4,
        )

    def test_toc_evaluator_fails_when_source_candidate_set_shrinks(self) -> None:
        dropped_source_id = next(
            node.source_node_id
            for node in self.source_ir.nodes
            if node.raw_text == "1.1 项目时间节点"
            and node.native_locator.page_number == 2
        )
        tampered_source = self.source_ir.model_copy(
            update={
                "nodes": tuple(
                    node
                    for node in self.source_ir.nodes
                    if node.source_node_id != dropped_source_id
                )
            }
        )
        tampered_tree = build_structure_tree(tampered_source)
        tampered_semantics = classify_content(tampered_source, tampered_tree)

        with patch.object(
            runner,
            "_pdf_semantic_context_for_document",
            return_value=(tampered_source, tampered_tree, tampered_semantics),
        ):
            outcome = runner._evaluate_pdf_single_toc_noise_block(
                case=self.case,
                document_results=self.document_results,
                source_ir_snapshots={},
                structure_snapshots={},
                semantic_snapshots={},
                expected_candidate_fingerprints_by_document=(
                    self.expected_candidate_fingerprints
                ),
            )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["evidence"]["toc_candidate_fingerprint_mismatch_count"],
            2,
        )

    def test_toc_evaluator_fails_when_candidate_text_shrinks_in_place(self) -> None:
        target = next(
            node
            for node in self.source_ir.nodes
            if node.raw_text == "1.1 项目时间节点"
            and node.native_locator.page_number == 2
        )
        tampered_source = self.source_ir.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(update={"raw_text": "1.1"})
                    if node.source_node_id == target.source_node_id
                    else node
                    for node in self.source_ir.nodes
                )
            }
        )

        with patch.object(
            runner,
            "_pdf_semantic_context_for_document",
            return_value=(tampered_source, self.tree, self.semantics),
        ):
            outcome = runner._evaluate_pdf_single_toc_noise_block(
                case=self.case,
                document_results=self.document_results,
                source_ir_snapshots={},
                structure_snapshots={},
                semantic_snapshots={},
                expected_candidate_fingerprints_by_document=(
                    self.expected_candidate_fingerprints
                ),
            )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["evidence"]["toc_candidate_fingerprint_mismatch_count"],
            2,
        )

    def test_noise_evaluator_fails_when_toc_page_text_is_unclassified(self) -> None:
        tampered_tree, tampered_semantics = self._toc_context_without_two_refs()

        with (
            patch.object(
                runner,
                "_pdf_requirement_context_for_document",
                return_value=(
                    self.source_ir,
                    tampered_tree,
                    tampered_semantics,
                    self.view,
                ),
            ),
            patch.object(
                runner,
                "build_document_view",
                return_value={"document_outline": []},
            ),
        ):
            outcome = runner._evaluate_pdf_page_noise_not_requirements(
                case=self.case,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
                structure_snapshots={},
                semantic_snapshots={},
                requirement_view_snapshots={},
            )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["evidence"]["unclassified_page_noise_ref_count"],
            4,
        )

    def test_noise_in_full_context_fails_even_without_direct_projection(self) -> None:
        toc_node = next(
            node
            for node in self.tree.nodes
            if node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK
        )
        noise_ref = toc_node.source_refs[0]
        first = self.view.nodes[0]
        tampered_first = first.model_copy(
            update={
                "full_context_source_refs": (
                    *first.full_context_source_refs,
                    noise_ref,
                )
            }
        )
        tampered_view = self.view.model_copy(
            update={"nodes": (tampered_first, *self.view.nodes[1:])}
        )

        with patch.object(
            runner,
            "validate_requirement_view_against_inputs",
            return_value=None,
        ):
            outcome = runner._evaluate_pdf_page_noise_not_requirements(
                case=self.case,
                document_results=self.document_results,
                source_ir_snapshots=self.source_ir_snapshots,
                structure_snapshots={self.structure_key: self.tree},
                semantic_snapshots={self.semantic_key: self.semantics},
                requirement_view_snapshots={self.view_key: tampered_view},
            )

        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(
            outcome["evidence"]["projected_noise_full_context_ref_count"],
            2,
        )

    def test_manifest_rejects_missing_document_region_scope(self) -> None:
        manifest = json.loads(
            require_private_eval("extraction_cases/manifest.json").read_text()
        )
        registry = json.loads(
            require_private_eval("extraction_cases/oracles.json").read_text()
        )
        case = manifest["cases"]["FMT-005"]
        case["locators"] = [
            locator
            for locator in case["locators"]
            if not (
                locator["document_id"] == "DOC-PEF-SOR"
                and locator["kind"] == "page_region"
            )
        ]

        with self.assertRaises(runner.ManifestValidationError):
            runner.validate_manifest(manifest, oracle_registry=registry)

    def test_manifest_rejects_incomplete_toc_candidate_fingerprint_binding(self) -> None:
        manifest = json.loads(
            require_private_eval("extraction_cases/manifest.json").read_text()
        )
        registry = json.loads(
            require_private_eval("extraction_cases/oracles.json").read_text()
        )
        evaluation = manifest["cases"]["FMT-005"]["assertions"][1][
            "evaluation"
        ]
        del evaluation["expected_candidate_fingerprints_by_document"][
            "DOC-PEF-SOR"
        ]

        with self.assertRaises(runner.ManifestValidationError):
            runner.validate_manifest(manifest, oracle_registry=registry)

    def _toc_context_without_two_refs(self):
        toc_node = next(
            node
            for node in self.tree.nodes
            if node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK
        )
        dropped_source_ids = {
            node.source_node_id
            for node in self.source_ir.nodes
            if node.raw_text in {"1.1 项目时间节点", "1.2 产品信息"}
            and node.native_locator.page_number == 2
        }
        kept_refs = tuple(
            ref for ref in toc_node.source_refs if ref.source_id not in dropped_source_ids
        )
        tampered_toc = toc_node.model_copy(update={"source_refs": kept_refs})
        tampered_tree = self.tree.model_copy(
            update={
                "nodes": tuple(
                    tampered_toc
                    if node.structure_node_id == toc_node.structure_node_id
                    else node
                    for node in self.tree.nodes
                )
            }
        )
        tampered_semantics = self.semantics.model_copy(
            update={
                "annotations": tuple(
                    annotation.model_copy(
                        update={
                            "grounding_spans": tuple(
                                span
                                for span in annotation.grounding_spans
                                if span.source_ref in kept_refs
                            )
                        }
                    )
                    if annotation.target_structure_node_id
                    == toc_node.structure_node_id
                    else annotation
                    for annotation in self.semantics.annotations
                )
            }
        )
        return tampered_tree, tampered_semantics


if __name__ == "__main__":
    unittest.main()
