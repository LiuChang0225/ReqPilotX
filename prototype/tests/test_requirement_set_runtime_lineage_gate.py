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

from app.application.assessment import start_document_ai_analysis
from app.application.assessment_contracts import RunAIAnalysisRequest
from app.application.errors import ApplicationError
from app.application.requirement_matrix import (
    _requirement_set_has_current_runtime_lineage,
    build_requirement_matrix_snapshot,
    patch_requirement_matrix_node_disposition,
)
from app.application.requirement_matrix_contracts import MatrixDispositionPatchV1
from app.domain.extraction.requirement_tree import (
    RequirementTreeNodeV1,
    RequirementTreeState,
)
from app.domain.requirement_matrix import (
    MatrixNodeDisposition,
    MatrixSnapshotState,
    RequirementMatrixSnapshotV1,
)


_SOURCE_SHA256 = "a" * 64
_TREE_SHA256 = "b" * 64
_REQUIREMENT_TREE_SHA256 = "c" * 64


def _requirement_set() -> dict[str, object]:
    return {
        "id": "set-1",
        "project_id": "project-1",
        "sor_document_id": "document-1",
        "status": "LOCKED",
        "is_active": 1,
        "version": 1,
        "source_hash": _SOURCE_SHA256,
        "structure_snapshot_id": "effective-1",
        "structure_tree_sha256": _TREE_SHA256,
        "structure_revision": 2,
        "requirement_tree_sha256": _REQUIREMENT_TREE_SHA256,
    }


def _formal_result() -> dict[str, object]:
    return {
        "project_id": "project-1",
        "document_id": "document-1",
        "source_sha256": _SOURCE_SHA256,
        "structure_agent": {
            "base_structure_identity": {
                "source_sha256": _SOURCE_SHA256,
            },
            "effective_structure_identity": {
                "snapshot_id": "effective-1",
                "tree_sha256": _TREE_SHA256,
                "revision_number": 2,
            },
        },
    }


def _empty_ready_tree() -> SimpleNamespace:
    root = RequirementTreeNodeV1(
        id="root-1",
        parent_id=None,
        kind="DOCUMENT",
        depth=0,
        source_order=0,
        label="历史需求版本",
        summary="历史需求版本",
        projection_role="STRUCTURE_PROXY",
    )
    return SimpleNamespace(
        state=RequirementTreeState.READY,
        requirement_evidence_bundle=SimpleNamespace(requirement_bindings=()),
        nodes=(root,),
        root_node_ids=(root.id,),
        table_record_projections=(),
        table_evidence_bindings=(),
    )


def _reextraction_matrix() -> RequirementMatrixSnapshotV1:
    return RequirementMatrixSnapshotV1(
        state=MatrixSnapshotState.REQUIRES_REEXTRACTION,
        project_id="project-1",
        document_id="document-1",
        requirement_set_id="set-1",
        requirement_set_status="LOCKED",
        tree_sha256=_REQUIREMENT_TREE_SHA256,
    )


class RequirementSetRuntimeLineageGateTests(unittest.TestCase):
    def test_incomplete_requirement_set_identity_fails_closed_before_lookup(
        self,
    ) -> None:
        requirement_set = _requirement_set()
        requirement_set.pop("structure_revision")
        with patch(
            "app.application.extraction.document_jobs._get_llm_extraction_job"
        ) as get_current_job:
            self.assertFalse(
                _requirement_set_has_current_runtime_lineage(requirement_set)
            )

        get_current_job.assert_not_called()

    def test_exact_formal_result_lineage_is_accepted_only_when_runtime_matches(
        self,
    ) -> None:
        job = {"status": "COMPLETED", "result": _formal_result()}
        with (
            patch(
                "app.application.extraction.document_jobs._get_llm_extraction_job",
                return_value=job,
            ),
            patch(
                "app.application.extraction.document_jobs._get_latest_successful_llm_extraction_job",
                return_value=None,
            ),
            patch(
                "app.application.extraction.document_jobs._restored_llm_extraction_job",
                return_value=None,
            ),
            patch(
                "app.application.extraction.document_jobs._llm_extraction_result_matches_runtime",
                return_value=True,
            ),
            patch(
                "app.application.extraction.document_jobs._llm_job_visual_strategy",
                return_value={},
            ),
        ):
            self.assertTrue(
                _requirement_set_has_current_runtime_lineage(_requirement_set())
            )

        with (
            patch(
                "app.application.extraction.document_jobs._get_llm_extraction_job",
                return_value=job,
            ),
            patch(
                "app.application.extraction.document_jobs._get_latest_successful_llm_extraction_job",
                return_value=None,
            ),
            patch(
                "app.application.extraction.document_jobs._restored_llm_extraction_job",
                return_value=None,
            ),
            patch(
                "app.application.extraction.document_jobs._llm_extraction_result_matches_runtime",
                return_value=False,
            ),
            patch(
                "app.application.extraction.document_jobs._llm_job_visual_strategy",
                return_value={},
            ),
        ):
            self.assertFalse(
                _requirement_set_has_current_runtime_lineage(_requirement_set())
            )

    def test_historical_matrix_get_projection_remains_readable(self) -> None:
        requirement_set = _requirement_set()
        with (
            patch(
                "app.application.requirement_matrix._require_matrix_scope",
                return_value=({}, requirement_set),
            ),
            patch(
                "app.application.requirement_matrix.load_requirement_set_tree_snapshot",
                return_value=_empty_ready_tree(),
            ),
            patch(
                "app.application.requirement_matrix.db.list_requirements",
                return_value=[],
            ),
            patch(
                "app.application.requirement_matrix.list_requirement_matrix_node_states",
                return_value=[],
            ),
            patch(
                "app.application.requirement_matrix._requirement_set_has_current_runtime_lineage",
                return_value=False,
            ) as runtime_gate,
        ):
            matrix = build_requirement_matrix_snapshot(
                "project-1",
                "document-1",
                "set-1",
            )

        self.assertEqual(matrix.state, MatrixSnapshotState.READY)
        runtime_gate.assert_not_called()

    def test_current_matrix_build_fails_closed_for_stale_runtime(self) -> None:
        requirement_set = _requirement_set()
        with (
            patch(
                "app.application.requirement_matrix._require_matrix_scope",
                return_value=({}, requirement_set),
            ),
            patch(
                "app.application.requirement_matrix.load_requirement_set_tree_snapshot",
                return_value=_empty_ready_tree(),
            ),
            patch(
                "app.application.requirement_matrix._requirement_set_has_current_runtime_lineage",
                return_value=False,
            ),
        ):
            matrix = build_requirement_matrix_snapshot(
                "project-1",
                "document-1",
                "set-1",
                require_current_runtime=True,
            )

        self.assertEqual(
            matrix.state,
            MatrixSnapshotState.REQUIRES_REEXTRACTION,
        )
        self.assertEqual(matrix.nodes, ())

    def test_matrix_mutation_rejects_stale_runtime_before_persistence(self) -> None:
        with patch(
            "app.application.requirement_matrix.build_requirement_matrix_snapshot",
            return_value=_reextraction_matrix(),
        ) as builder:
            with self.assertRaises(ApplicationError) as raised:
                patch_requirement_matrix_node_disposition(
                    "project-1",
                    "document-1",
                    "set-1",
                    "node-1",
                    MatrixDispositionPatchV1(
                        disposition=MatrixNodeDisposition.RETAIN,
                        expected_revision=0,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "matrix_requires_reextraction",
        )
        builder.assert_called_once_with(
            "project-1",
            "document-1",
            "set-1",
            require_current_runtime=True,
        )

    def test_ai_start_rejects_stale_runtime_before_job_claim(self) -> None:
        document = {
            "id": "document-1",
            "project_id": "project-1",
            "active_requirement_set_id": "set-1",
            "requirement_set_version": 1,
            "extraction_locked_at": "2026-08-19T00:00:00+00:00",
        }
        with (
            patch(
                "app.application.projects._require_project",
                return_value={"id": "project-1", "knowledge_base_version": 0},
            ),
            patch(
                "app.application.assessment.db.get_sor_document",
                return_value=document,
            ),
            patch(
                "app.application.assessment.db.get_requirement_set",
                return_value=_requirement_set(),
            ),
            patch(
                "app.application.requirement_matrix.build_requirement_matrix_snapshot",
                return_value=_reextraction_matrix(),
            ) as builder,
            patch(
                "app.application.assessment.claim_analysis_job"
            ) as claim_job,
        ):
            with self.assertRaises(ApplicationError) as raised:
                start_document_ai_analysis(
                    "project-1",
                    "document-1",
                    RunAIAnalysisRequest(
                        requirement_set_id="set-1",
                        scope="ALL",
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "matrix_requires_reextraction",
        )
        builder.assert_called_once_with(
            "project-1",
            "document-1",
            "set-1",
            require_current_runtime=True,
        )
        claim_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
