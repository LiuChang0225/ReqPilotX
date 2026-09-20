from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import TypeAdapter, ValidationError

from app import db
from app.adapters.persistence.assessment_results import (
    claim_analysis_job,
    claim_analysis_node_attempt,
    fail_analysis_node_attempt,
    finalize_incremental_analysis_job,
    publish_staged_analysis_node_result,
    publish_serving_results,
    stage_analysis_node_result,
)
from app.application import assessment
from app.application.assessment_contracts import (
    RequirementAnalysisScopeRequest,
    RequirementAnalysisScopeRequestV1,
    RequirementAnalysisScopeRequestV2,
    RunAIAnalysisRequest,
)
from app.application.assessment_scope import (
    ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
    ATOMIC_BATCH_PUBLICATION_MODE,
    PER_NODE_RECOVERABLE_PUBLICATION_MODE,
    AssessmentScopeError,
    assessment_scope_preview,
    build_assessment_scope,
    parse_assessment_scope,
    select_rows_for_assessment_scope,
)
from app.application.errors import ApplicationError
from app.domain.requirement_matrix import (
    MatrixAnalysisEligibility,
    MatrixAnalysisStatus,
    MatrixCountsV1,
    MatrixNodeDisposition,
    MatrixNodeKind,
    MatrixNodeStateV1,
    MatrixNodeVisibility,
    MatrixSnapshotState,
    RequirementMatrixSnapshotV1,
)


_TREE_SHA256 = "a" * 64


def _node(
    node_id: str,
    source_order: int,
    *,
    visibility: MatrixNodeVisibility = MatrixNodeVisibility.VISIBLE,
    disposition: MatrixNodeDisposition = MatrixNodeDisposition.RETAIN,
    requirement_id: str = "",
) -> MatrixNodeStateV1:
    eligible = disposition is MatrixNodeDisposition.ANALYZE
    return MatrixNodeStateV1(
        node_id=node_id,
        parent_id=None,
        depth=0,
        source_order=source_order,
        display_role=(
            MatrixNodeKind.ATOMIC_REQUIREMENT if eligible else MatrixNodeKind.CONTEXT
        ),
        label=node_id,
        display_text=node_id,
        requirement_entry_id=requirement_id or None,
        requirement_id=requirement_id or None,
        requirement_code=requirement_id.upper(),
        requirement_text=node_id if requirement_id else "",
        matrix_visibility=visibility,
        disposition=disposition,
        analysis_eligibility=(
            MatrixAnalysisEligibility.ELIGIBLE
            if eligible
            else MatrixAnalysisEligibility.NOT_ELIGIBLE
        ),
        analysis_status=(
            MatrixAnalysisStatus.NOT_STARTED
            if eligible
            else MatrixAnalysisStatus.NOT_APPLICABLE
        ),
    )


def _matrix(
    *,
    project_id: str = "project-1",
    document_id: str = "document-1",
    requirement_set_id: str = "set-1",
    first_requirement_id: str = "requirement-1",
    second_requirement_id: str = "requirement-2",
) -> RequirementMatrixSnapshotV1:
    nodes = (
        _node(
            "target-1",
            0,
            disposition=MatrixNodeDisposition.ANALYZE,
            requirement_id=first_requirement_id,
        ),
        _node("title-1", 1),
        _node(
            "target-2",
            2,
            disposition=MatrixNodeDisposition.ANALYZE,
            requirement_id=second_requirement_id,
        ),
        _node(
            "human-excluded",
            3,
            visibility=MatrixNodeVisibility.HUMAN_EXCLUDED,
            disposition=MatrixNodeDisposition.EXCLUDE,
        ),
        _node(
            "system-cover",
            4,
            visibility=MatrixNodeVisibility.SYSTEM_NON_BUSINESS,
        ),
    )
    return RequirementMatrixSnapshotV1(
        state=MatrixSnapshotState.READY,
        project_id=project_id,
        document_id=document_id,
        requirement_set_id=requirement_set_id,
        requirement_set_status="LOCKED",
        tree_sha256=_TREE_SHA256,
        root_node_ids=tuple(node.node_id for node in nodes),
        nodes=nodes,
        counts=MatrixCountsV1(
            total_nodes=5,
            visible_nodes=3,
            system_non_business_nodes=1,
            human_excluded_nodes=1,
            analyze_nodes=2,
            retained_nodes=2,
            excluded_nodes=1,
            analysis_eligible_nodes=2,
        ),
    )


class AssessmentScopeProjectionTests(unittest.TestCase):
    def test_v1_contract_remains_the_default_atomic_scope(self) -> None:
        default_request = TypeAdapter(RequirementAnalysisScopeRequest).validate_python(
            {
                "command_id": "legacy-command",
                "requirement_set_id": "set-1",
                "expected_tree_sha256": _TREE_SHA256,
                "scope": "ALL",
            }
        )
        envelope = build_assessment_scope(
            _matrix(),
            requirement_set_version=3,
            scope="ALL",
            requested_node_ids=(),
        )

        self.assertEqual(envelope["schema_version"], "requirement-analysis-scope/v1")
        self.assertIsInstance(default_request, RequirementAnalysisScopeRequestV1)
        self.assertEqual(envelope["publication_mode"], ATOMIC_BATCH_PUBLICATION_MODE)
        self.assertNotIn("source_job_id", envelope)
        self.assertNotIn("retry_summary", envelope)
        self.assertEqual(
            assessment_scope_preview(envelope, command_id="legacy-command")[
                "schema_version"
            ],
            "requirement-analysis-scope-preview/v1",
        )
        self.assertEqual(
            parse_assessment_scope(
                envelope["scope_payload_json"],
                expected_scope_sha256=envelope["scope_sha256"],
            )["publication_mode"],
            ATOMIC_BATCH_PUBLICATION_MODE,
        )

    def test_v2_regular_scope_freezes_recoverable_zero_retry_state(self) -> None:
        envelope = build_assessment_scope(
            _matrix(),
            requirement_set_version=4,
            scope="SELECTED",
            requested_node_ids=("target-2", "target-1"),
            schema_version=ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
        )
        preview = assessment_scope_preview(envelope, command_id="v2-command")

        self.assertEqual(
            envelope["publication_mode"], PER_NODE_RECOVERABLE_PUBLICATION_MODE
        )
        self.assertEqual(envelope["source_job_id"], "")
        self.assertEqual(envelope["failed_snapshot_sha256"], "")
        self.assertEqual(
            envelope["retry_summary"],
            {
                "source_failed_count": 0,
                "target": 0,
                "already_current": 0,
                "skipped": 0,
            },
        )
        self.assertEqual(
            preview["schema_version"], "requirement-analysis-scope-preview/v2"
        )
        self.assertEqual(
            preview["publication_mode"], PER_NODE_RECOVERABLE_PUBLICATION_MODE
        )
        self.assertEqual(preview["source_job_id"], "")
        self.assertEqual(preview["source_failed_count"], 0)
        self.assertEqual(preview["already_current_count"], 0)

    def test_failed_only_scope_uses_server_resolved_source_order_and_snapshot(
        self,
    ) -> None:
        failed_snapshot_sha256 = "b" * 64
        envelope = build_assessment_scope(
            _matrix(),
            requirement_set_version=4,
            scope="FAILED_ONLY",
            requested_node_ids=(),
            schema_version=ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
            source_job_id="source-job-1",
            retry_node_ids=("target-1", "target-2"),
            retry_metadata={
                "failed_snapshot_sha256": failed_snapshot_sha256,
                "source_failed_count": 4,
                "already_current": 1,
                "skipped": 1,
            },
        )
        parsed = parse_assessment_scope(
            envelope["scope_payload_json"],
            expected_scope_sha256=envelope["scope_sha256"],
        )
        preview = assessment_scope_preview(envelope, command_id="retry-command")

        self.assertEqual(parsed["scope"], "FAILED_ONLY")
        self.assertEqual(parsed["source_job_id"], "source-job-1")
        self.assertEqual(parsed["requested_node_ids"], ["target-1", "target-2"])
        self.assertEqual(
            parsed["target_pairs"],
            [
                {"node_id": "target-1", "requirement_id": "requirement-1"},
                {"node_id": "target-2", "requirement_id": "requirement-2"},
            ],
        )
        self.assertEqual(preview["failed_snapshot_sha256"], failed_snapshot_sha256)
        self.assertEqual(preview["source_failed_count"], 4)
        self.assertEqual(preview["target_count"], 2)
        self.assertEqual(preview["already_current_count"], 1)
        self.assertEqual(preview["retry_skipped_count"], 1)

    def test_v2_scope_fails_closed_on_mode_order_and_retry_count_drift(self) -> None:
        matrix = _matrix()
        with self.assertRaisesRegex(
            AssessmentScopeError, "分析范围版本与发布模式不匹配"
        ):
            build_assessment_scope(
                matrix,
                requirement_set_version=1,
                scope="ALL",
                requested_node_ids=(),
                schema_version=ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
                publication_mode=ATOMIC_BATCH_PUBLICATION_MODE,
            )
        with self.assertRaises(AssessmentScopeError) as order_error:
            build_assessment_scope(
                matrix,
                requirement_set_version=1,
                scope="FAILED_ONLY",
                requested_node_ids=(),
                schema_version=ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
                source_job_id="source-job-1",
                retry_node_ids=("target-2", "target-1"),
                retry_metadata={
                    "failed_snapshot_sha256": "b" * 64,
                    "source_failed_count": 2,
                    "already_current": 0,
                    "skipped": 0,
                },
            )
        self.assertEqual(
            order_error.exception.code, "analysis_scope_retry_order_invalid"
        )
        with self.assertRaises(AssessmentScopeError) as count_error:
            build_assessment_scope(
                matrix,
                requirement_set_version=1,
                scope="FAILED_ONLY",
                requested_node_ids=(),
                schema_version=ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
                source_job_id="source-job-1",
                retry_node_ids=("target-1",),
                retry_metadata={
                    "failed_snapshot_sha256": "b" * 64,
                    "source_failed_count": 2,
                    "already_current": 0,
                    "skipped": 0,
                },
            )
        self.assertEqual(
            count_error.exception.code, "analysis_scope_retry_summary_invalid"
        )

    def test_v2_request_contract_requires_frozen_retry_identity(self) -> None:
        retry_preview = RequirementAnalysisScopeRequestV2(
            command_id="retry-command",
            requirement_set_id="set-1",
            expected_tree_sha256=_TREE_SHA256,
            scope="FAILED_ONLY",
            source_job_id="source-job-1",
        )
        retry_start = RunAIAnalysisRequest(
            schema_version="requirement-analysis-scope-request/v2",
            command_id="retry-command",
            requirement_set_id="set-1",
            expected_tree_sha256=_TREE_SHA256,
            expected_scope_sha256="b" * 64,
            scope="FAILED_ONLY",
            source_job_id="source-job-1",
        )
        self.assertEqual(retry_preview.node_ids, ())
        self.assertEqual(retry_start.source_job_id, "source-job-1")

        invalid_payloads = (
            {
                "command_id": "retry-command",
                "requirement_set_id": "set-1",
                "expected_tree_sha256": _TREE_SHA256,
                "scope": "FAILED_ONLY",
            },
            {
                "command_id": "retry-command",
                "requirement_set_id": "set-1",
                "expected_tree_sha256": _TREE_SHA256,
                "scope": "FAILED_ONLY",
                "source_job_id": "source-job-1",
                "node_ids": ("target-1",),
            },
            {
                "command_id": "regular-command",
                "requirement_set_id": "set-1",
                "expected_tree_sha256": _TREE_SHA256,
                "scope": "ALL",
                "source_job_id": "source-job-1",
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    RequirementAnalysisScopeRequestV2(**payload)

        with self.assertRaises(ValidationError):
            RunAIAnalysisRequest(
                schema_version="requirement-analysis-scope-request/v2",
                requirement_set_id="set-1",
                scope="ALL",
            )
        with self.assertRaises(ValidationError):
            RunAIAnalysisRequest(
                requirement_set_id="set-1",
                scope="FAILED_ONLY",
                source_job_id="source-job-1",
            )

    def test_current_selected_and_all_project_canonical_business_scope(self) -> None:
        matrix = _matrix()

        current = build_assessment_scope(
            matrix,
            requirement_set_version=3,
            scope="CURRENT",
            requested_node_ids=("title-1",),
        )
        self.assertEqual(current["requested_node_ids"], ["title-1"])
        self.assertEqual(current["target_pairs"], [])
        self.assertEqual(
            current["skipped_reason_counts"], {"NOT_MARKED_FOR_ANALYSIS": 1}
        )

        selected = build_assessment_scope(
            matrix,
            requirement_set_version=3,
            scope="SELECTED",
            requested_node_ids=("target-2", "target-1"),
        )
        self.assertEqual(selected["requested_node_ids"], ["target-1", "target-2"])
        self.assertEqual(
            selected["target_pairs"],
            [
                {"node_id": "target-1", "requirement_id": "requirement-1"},
                {"node_id": "target-2", "requirement_id": "requirement-2"},
            ],
        )
        self.assertEqual(
            selected["analysis_universe_pairs"],
            [
                {"node_id": "target-1", "requirement_id": "requirement-1"},
                {"node_id": "target-2", "requirement_id": "requirement-2"},
            ],
        )

        all_scope = build_assessment_scope(
            matrix,
            requirement_set_version=3,
            scope="ALL",
            requested_node_ids=(),
        )
        preview = assessment_scope_preview(all_scope, command_id="command-1")
        self.assertEqual(
            all_scope["requested_node_ids"],
            ["target-1", "title-1", "target-2", "human-excluded"],
        )
        self.assertEqual(preview["requested_count"], 4)
        self.assertEqual(preview["target_count"], 2)
        self.assertEqual(preview["skipped_count"], 2)
        self.assertEqual(
            preview["skipped_reason_counts"],
            {"NOT_MARKED_FOR_ANALYSIS": 1, "NOT_VISIBLE": 1},
        )

    def test_scope_rejects_unknown_duplicate_and_illegal_node_shapes(self) -> None:
        matrix = _matrix()
        invalid_cases = (
            ("CURRENT", ("target-1", "target-2"), "current_requires_one"),
            ("SELECTED", (), "selected_requires_nodes"),
            ("SELECTED", ("target-1", "target-1"), "node_ids_invalid"),
            ("SELECTED", ("missing",), "node_not_in_requirement_set"),
            ("ALL", ("target-1",), "all_rejects_nodes"),
        )
        for scope, node_ids, expected_code in invalid_cases:
            with self.subTest(scope=scope, node_ids=node_ids):
                with self.assertRaises(AssessmentScopeError) as raised:
                    build_assessment_scope(
                        matrix,
                        requirement_set_version=1,
                        scope=scope,
                        requested_node_ids=node_ids,
                    )
                self.assertIn(expected_code, raised.exception.code)

        with self.assertRaises(ValidationError):
            RequirementAnalysisScopeRequestV1(
                command_id="unsafe command text",
                requirement_set_id="set-1",
                expected_tree_sha256=_TREE_SHA256,
                scope="ALL",
                node_ids=(),
            )

    def test_scope_hash_and_worker_binding_fail_closed_on_tamper(self) -> None:
        envelope = build_assessment_scope(
            _matrix(),
            requirement_set_version=1,
            scope="SELECTED",
            requested_node_ids=("target-1", "target-2"),
        )
        parsed = parse_assessment_scope(
            envelope["scope_payload_json"],
            expected_scope_sha256=envelope["scope_sha256"],
        )
        selected = select_rows_for_assessment_scope(
            [
                {"id": "requirement-2", "matrix_node_id": "target-2"},
                {"id": "requirement-1", "matrix_node_id": "target-1"},
            ],
            parsed,
        )
        self.assertEqual(
            [row["id"] for row in selected],
            ["requirement-1", "requirement-2"],
        )

        with self.assertRaises(AssessmentScopeError) as hash_error:
            parse_assessment_scope(
                envelope["scope_payload_json"],
                expected_scope_sha256="f" * 64,
            )
        self.assertEqual(
            hash_error.exception.code, "analysis_scope_snapshot_hash_mismatch"
        )

        with self.assertRaises(AssessmentScopeError) as binding_error:
            select_rows_for_assessment_scope(
                [
                    {"id": "requirement-1", "matrix_node_id": "target-1"},
                    {"id": "wrong", "matrix_node_id": "target-2"},
                ],
                parsed,
            )
        self.assertEqual(
            binding_error.exception.code,
            "analysis_scope_worker_membership_changed",
        )


class AssessmentScopePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="reqpilotx-assessment-scope-preview-"
        )
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        db.init_db()

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.temporary.cleanup()

    def test_preview_is_read_only_and_never_prepares_ai_runtime(self) -> None:
        project = db.create_project("Scope preview", "Customer")
        source = self.root / "source.txt"
        source.write_text("Supplier shall retain evidence.", encoding="utf-8")
        document = db.add_sor_document(
            project["id"], source.name, "txt", str(source), source.read_text()
        )
        requirement = db.insert_requirement(
            project["id"],
            document["id"],
            "REQ0001",
            1,
            "Page 1",
            "Supplier shall retain evidence.",
            "REGULATION",
            "MEDIUM",
        )
        requirement_set_id = str(requirement["requirement_set_id"])
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_sets
                SET requirement_tree_sha256 = ?
                WHERE id = ? AND status = 'DRAFT'
                """,
                (_TREE_SHA256, requirement_set_id),
            )
        db.lock_sor_document_extraction(
            document["id"],
            expected_requirement_set_id=requirement_set_id,
        )
        matrix = _matrix(
            project_id=project["id"],
            document_id=document["id"],
            requirement_set_id=requirement_set_id,
            first_requirement_id=requirement["id"],
        )
        before_document = db.get_sor_document(document["id"])
        with db.get_conn() as connection:
            before_counts = tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "ai_analysis_jobs",
                    "ai_analysis_events",
                    "assessments",
                    "knowledge_document_syncs",
                )
            )

        request = RequirementAnalysisScopeRequestV1(
            command_id="preview-command-1",
            requirement_set_id=requirement_set_id,
            expected_tree_sha256=_TREE_SHA256,
            scope="ALL",
            node_ids=(),
        )
        with (
            patch(
                "app.application.requirement_matrix.build_requirement_matrix_snapshot",
                return_value=matrix,
            ),
            patch.object(
                assessment, "_prepare_document_ai_analysis_request"
            ) as prepare_ai,
            patch.object(assessment, "load_ai_copilot_config") as load_profile,
            patch.object(assessment, "run_ai_copilot_assessment") as run_provider,
            patch.object(assessment, "claim_analysis_job") as claim_job,
        ):
            preview = assessment.preview_document_ai_analysis_scope(
                project["id"], document["id"], request
            )

        self.assertEqual(preview["target_count"], 2)
        self.assertEqual(preview["skipped_count"], 2)
        prepare_ai.assert_not_called()
        load_profile.assert_not_called()
        run_provider.assert_not_called()
        claim_job.assert_not_called()
        self.assertEqual(db.get_sor_document(document["id"]), before_document)
        with db.get_conn() as connection:
            after_counts = tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "ai_analysis_jobs",
                    "ai_analysis_events",
                    "assessments",
                    "knowledge_document_syncs",
                )
            )
        self.assertEqual(after_counts, before_counts)


class AssessmentScopePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="reqpilotx-assessment-scope-publication-"
        )
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        db.init_db()

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.temporary.cleanup()

    def _locked_fixture(
        self,
    ) -> tuple[dict, dict, dict, list[dict], RequirementMatrixSnapshotV1]:
        project = db.create_project("Scoped publication", "Customer")
        source = self.root / "source.txt"
        source.write_text("Requirements", encoding="utf-8")
        document = db.add_sor_document(
            project["id"], source.name, "txt", str(source), "Requirements"
        )
        requirements = [
            db.insert_requirement(
                project["id"],
                document["id"],
                f"REQ000{index}",
                index,
                f"Page {index}",
                f"Requirement {index} shall remain traceable.",
                "REGULATION",
                "MEDIUM",
            )
            for index in (1, 2)
        ]
        requirement_set_id = str(requirements[0]["requirement_set_id"])
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_sets
                SET requirement_tree_sha256 = ?
                WHERE id = ? AND status = 'DRAFT'
                """,
                (_TREE_SHA256, requirement_set_id),
            )
        locked_document = db.lock_sor_document_extraction(
            document["id"],
            expected_requirement_set_id=requirement_set_id,
        )
        requirement_set = db.get_requirement_set(requirement_set_id) or {}
        matrix = _matrix(
            project_id=project["id"],
            document_id=document["id"],
            requirement_set_id=requirement_set_id,
            first_requirement_id=requirements[0]["id"],
            second_requirement_id=requirements[1]["id"],
        )
        for index, requirement in enumerate(requirements, start=1):
            db.upsert_assessment(
                requirement_id=requirement["id"],
                profile_id="profile-1",
                provider="provider-1",
                ai_decision="ACCEPT",
                ai_reason=f"old-{index}",
                ai_confidence=0.8,
                assessment_mode="heuristic",
                retrieval_backend="local",
                analysis_label="REQUIREMENT",
                analysis_basis="last good",
                normalized_requirement=f"old-{index}",
                risk_level="LOW",
                clarification_question="",
                model_name="old-model",
                prompt_version="old-prompt",
            )
        db.update_sor_document_ai_state(
            document["id"],
            ai_status="READY",
            ai_assessed_at="2026-08-08T00:00:00+00:00",
            ai_requirement_set_version=int(requirement_set["version"]),
            ai_knowledge_base_version=0,
            workflow_stage="AI_READY",
        )
        return project, locked_document, requirement_set, requirements, matrix

    def _scoped_job(
        self,
        *,
        project: dict,
        document: dict,
        requirement_set: dict,
        matrix: RequirementMatrixSnapshotV1,
        knowledge_base_version: int,
    ) -> tuple[dict, dict]:
        envelope = build_assessment_scope(
            matrix,
            requirement_set_version=int(requirement_set["version"]),
            scope="CURRENT",
            requested_node_ids=("target-1",),
        )
        job = db.create_ai_analysis_job(
            project["id"],
            document["id"],
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
            requirement_set_id=requirement_set["id"],
            requirement_set_version=int(requirement_set["version"]),
            requirement_tree_sha256=_TREE_SHA256,
            knowledge_base_version=knowledge_base_version,
            command_id="command-1",
            command_sha256="b" * 64,
            scope_schema_version=str(envelope["schema_version"]),
            scope_kind="CURRENT",
            scope_payload_json=str(envelope["scope_payload_json"]),
            scope_sha256=str(envelope["scope_sha256"]),
            publication_mode="ATOMIC_BATCH",
            total_requirements=1,
            status="RUNNING",
            current_stage="assessment",
        )
        return job, envelope

    @staticmethod
    def _result(reason: str) -> dict:
        return {
            "assessment": {
                "ai_decision": "REJECT",
                "ai_reason": reason,
                "ai_confidence": 0.9,
                "assessment_mode": "heuristic",
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "new result",
                "normalized_requirement": reason,
                "risk_level": "HIGH",
                "clarification_question": "",
                "model_name": "new-model",
                "prompt_version": "new-prompt",
            },
            "evidences": [],
        }

    @staticmethod
    def _completion(job_id: str) -> dict:
        return {
            "job_id": job_id,
            "total_requirements": 1,
            "completed_requirements": 1,
            "analyzed_requirements": 1,
            "info_requirements": 0,
            "screened_requirements": 0,
            "message": "done",
            "event_payload": {"published": True},
        }

    def _publish_current(
        self,
        *,
        job: dict,
        envelope: dict,
        document: dict,
        requirement_set: dict,
        requirements: list[dict],
        knowledge_base_version: int,
        pending_target_pairs: list[tuple[str, str]] | None = None,
        all_analysis_target_requirement_ids: list[str] | None = None,
    ) -> dict:
        return publish_serving_results(
            [(requirements[0]["id"], self._result("new-1"))],
            document_id=document["id"],
            requirement_set_id=requirement_set["id"],
            requirement_set_version=int(requirement_set["version"]),
            knowledge_base_version=knowledge_base_version,
            profile_id="profile-1",
            provider="provider-1",
            retrieval_backend="local",
            scope_sha256=str(envelope["scope_sha256"]),
            requirement_tree_sha256=_TREE_SHA256,
            pending_target_pairs=(
                pending_target_pairs
                if pending_target_pairs is not None
                else [("target-1", requirements[0]["id"])]
            ),
            all_analysis_target_requirement_ids=(
                all_analysis_target_requirement_ids
                if all_analysis_target_requirement_ids is not None
                else [requirement["id"] for requirement in requirements]
            ),
            job_completion=self._completion(job["id"]),
        )

    def test_partial_publish_after_knowledge_change_does_not_promote_ready(
        self,
    ) -> None:
        project, document, requirement_set, requirements, matrix = (
            self._locked_fixture()
        )
        outside_before = db.get_assessment(requirements[1]["id"])
        bumped = db.bump_project_knowledge_base_version(project["id"])
        knowledge_version = int(bumped["knowledge_base_version"])
        job, envelope = self._scoped_job(
            project=project,
            document=document,
            requirement_set=requirement_set,
            matrix=matrix,
            knowledge_base_version=knowledge_version,
        )

        updated = self._publish_current(
            job=job,
            envelope=envelope,
            document=document,
            requirement_set=requirement_set,
            requirements=requirements,
            knowledge_base_version=knowledge_version,
        )

        self.assertEqual(updated["ai_status"], "STALE")
        self.assertEqual(updated["workflow_stage"], "EXTRACTION_LOCKED")
        self.assertEqual(int(updated["ai_knowledge_base_version"]), 0)
        self.assertEqual(db.get_assessment(requirements[1]["id"]), outside_before)
        completed = db.get_ai_analysis_job(job["id"]) or {}
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(int(completed["published_requirements"]), 1)

    def test_partial_publish_preserves_current_ready_projection(self) -> None:
        project, document, requirement_set, requirements, matrix = (
            self._locked_fixture()
        )
        outside_before = db.get_assessment(requirements[1]["id"])
        job, envelope = self._scoped_job(
            project=project,
            document=document,
            requirement_set=requirement_set,
            matrix=matrix,
            knowledge_base_version=0,
        )

        updated = self._publish_current(
            job=job,
            envelope=envelope,
            document=document,
            requirement_set=requirement_set,
            requirements=requirements,
            knowledge_base_version=0,
        )

        self.assertEqual(updated["ai_status"], "READY")
        self.assertEqual(updated["workflow_stage"], "AI_READY")
        self.assertEqual(db.get_assessment(requirements[1]["id"]), outside_before)

    def test_scoped_publish_rejects_nonexact_target_pairs_before_write(self) -> None:
        project, document, requirement_set, requirements, matrix = (
            self._locked_fixture()
        )
        before = db.get_assessment(requirements[0]["id"])
        job, envelope = self._scoped_job(
            project=project,
            document=document,
            requirement_set=requirement_set,
            matrix=matrix,
            knowledge_base_version=0,
        )

        with patch(
            "app.adapters.persistence.assessment_results._persist_result"
        ) as persist_result:
            with self.assertRaisesRegex(RuntimeError, "exactly match"):
                self._publish_current(
                    job=job,
                    envelope=envelope,
                    document=document,
                    requirement_set=requirement_set,
                    requirements=requirements,
                    knowledge_base_version=0,
                    pending_target_pairs=[("wrong-node", requirements[0]["id"])],
                )
        persist_result.assert_not_called()
        self.assertEqual(db.get_assessment(requirements[0]["id"]), before)
        self.assertEqual((db.get_ai_analysis_job(job["id"]) or {})["status"], "RUNNING")

    def test_scoped_publish_rejects_truncated_analysis_universe_before_write(
        self,
    ) -> None:
        project, document, requirement_set, requirements, matrix = (
            self._locked_fixture()
        )
        bumped = db.bump_project_knowledge_base_version(project["id"])
        knowledge_version = int(bumped["knowledge_base_version"])
        before_document = db.get_sor_document(document["id"])
        before_outside = db.get_assessment(requirements[1]["id"])
        job, envelope = self._scoped_job(
            project=project,
            document=document,
            requirement_set=requirement_set,
            matrix=matrix,
            knowledge_base_version=knowledge_version,
        )

        with patch(
            "app.adapters.persistence.assessment_results._persist_result"
        ) as persist_result:
            with self.assertRaisesRegex(RuntimeError, "target universe"):
                self._publish_current(
                    job=job,
                    envelope=envelope,
                    document=document,
                    requirement_set=requirement_set,
                    requirements=requirements,
                    knowledge_base_version=knowledge_version,
                    all_analysis_target_requirement_ids=[requirements[0]["id"]],
                )

        persist_result.assert_not_called()
        self.assertEqual(db.get_sor_document(document["id"]), before_document)
        self.assertEqual(db.get_assessment(requirements[1]["id"]), before_outside)

    def test_scoped_claim_rejects_knowledge_change_without_creating_job(self) -> None:
        project, document, requirement_set, _requirements, matrix = (
            self._locked_fixture()
        )
        envelope = build_assessment_scope(
            matrix,
            requirement_set_version=int(requirement_set["version"]),
            scope="CURRENT",
            requested_node_ids=("target-1",),
        )
        db.bump_project_knowledge_base_version(project["id"])
        before_document = db.get_sor_document(document["id"])

        with self.assertRaisesRegex(ValueError, "analysis_knowledge_base_changed"):
            claim_analysis_job(
                project_id=project["id"],
                document_id=document["id"],
                requirement_set_id=requirement_set["id"],
                profile_id="profile-1",
                provider="provider-1",
                assessment_mode="heuristic",
                retrieval_backend="local",
                chat_model="model-1",
                embedding_model="embedding-1",
                total_requirements=1,
                queued_message="queued",
                command_id="command-kb-race-1",
                command_sha256="b" * 64,
                scope_schema_version=str(envelope["schema_version"]),
                scope_kind="CURRENT",
                scope_payload_json=str(envelope["scope_payload_json"]),
                scope_sha256=str(envelope["scope_sha256"]),
                requirement_set_version=int(requirement_set["version"]),
                requirement_tree_sha256=_TREE_SHA256,
                expected_knowledge_base_version=0,
            )

        self.assertEqual(db.get_sor_document(document["id"]), before_document)
        self.assertIsNone(db.get_latest_ai_analysis_job(document["id"]))

    def test_start_replays_exact_command_and_rejects_changed_scope(self) -> None:
        project, document, requirement_set, requirements, matrix = (
            self._locked_fixture()
        )
        rows = [
            {
                **(db.get_requirement(requirement["id"]) or {}),
                "matrix_node_id": f"target-{index}",
            }
            for index, requirement in enumerate(requirements, start=1)
        ]
        ai_config = SimpleNamespace(
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
            chat_model="model-1",
            embedding_model="embedding-1",
        )
        prepared = (project, document, rows, rows, [], ai_config)
        all_envelope = build_assessment_scope(
            matrix,
            requirement_set_version=int(requirement_set["version"]),
            scope="ALL",
            requested_node_ids=(),
        )
        current_envelope = build_assessment_scope(
            matrix,
            requirement_set_version=int(requirement_set["version"]),
            scope="CURRENT",
            requested_node_ids=("target-1",),
        )
        all_request = RunAIAnalysisRequest(
            command_id="command-replay-1",
            requirement_set_id=requirement_set["id"],
            expected_tree_sha256=_TREE_SHA256,
            scope="ALL",
            node_ids=(),
            expected_scope_sha256=all_envelope["scope_sha256"],
        )
        changed_request = RunAIAnalysisRequest(
            command_id="command-replay-1",
            requirement_set_id=requirement_set["id"],
            expected_tree_sha256=_TREE_SHA256,
            scope="CURRENT",
            node_ids=("target-1",),
            expected_scope_sha256=current_envelope["scope_sha256"],
        )

        class HoldingThread:
            def __init__(self, *args, **kwargs) -> None:
                self._alive = False

            def start(self) -> None:
                self._alive = True

            def is_alive(self) -> bool:
                return self._alive

        try:
            with (
                patch(
                    "app.application.requirement_matrix.build_requirement_matrix_snapshot",
                    return_value=matrix,
                ),
                patch.object(
                    assessment,
                    "_prepare_document_ai_analysis_request",
                    return_value=prepared,
                ),
                patch.object(assessment, "Thread", HoldingThread),
                patch.object(assessment, "run_ai_copilot_assessment") as run_provider,
            ):
                first = assessment.start_document_ai_analysis(
                    project["id"], document["id"], all_request
                )
                replay = assessment.start_document_ai_analysis(
                    project["id"], document["id"], all_request
                )
                with self.assertRaises(ApplicationError) as raised:
                    assessment.start_document_ai_analysis(
                        project["id"], document["id"], changed_request
                    )
        finally:
            assessment._clear_ai_analysis_thread(document["id"])

        self.assertEqual(first["job"]["id"], replay["job"]["id"])
        self.assertEqual(
            first["capabilities"],
            {
                "schema_version": "ai-analysis-capabilities/v2",
                "analysis_scope_contract": "requirement-analysis-scope/v2",
                "scope_preview": True,
                "scoped_start": True,
                "atomic_batch_publication": True,
                "per_node_recoverable_publication": True,
                "node_execution_status": True,
                "failed_only_retry": True,
                "controlled_parallelism": True,
            },
        )
        self.assertEqual(first["job"]["scope"], "ALL")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"], "analysis_command_id_conflict"
        )
        projected_document = db.get_sor_document(document["id"]) or {}
        self.assertEqual(projected_document["ai_status"], "READY")
        self.assertEqual(projected_document["workflow_stage"], "AI_READY")
        run_provider.assert_not_called()
        with db.get_conn() as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM ai_analysis_jobs WHERE sor_document_id = ?",
                        (document["id"],),
                    ).fetchone()[0]
                ),
                1,
            )

    def test_failed_only_start_replays_after_its_own_partial_publication(self) -> None:
        project, document, requirement_set, requirements, matrix = (
            self._locked_fixture()
        )
        rows = [
            {
                **(db.get_requirement(requirement["id"]) or {}),
                "matrix_node_id": f"target-{index}",
            }
            for index, requirement in enumerate(requirements, start=1)
        ]
        ai_config = SimpleNamespace(
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
            chat_model="model-1",
            embedding_model="embedding-1",
        )
        source_envelope = build_assessment_scope(
            matrix,
            requirement_set_version=int(requirement_set["version"]),
            scope="ALL",
            requested_node_ids=(),
            schema_version=ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
        )
        source_job, source_claimed = claim_analysis_job(
            project_id=project["id"],
            document_id=document["id"],
            requirement_set_id=requirement_set["id"],
            profile_id=ai_config.profile_id,
            provider=ai_config.provider,
            assessment_mode=ai_config.assessment_mode,
            retrieval_backend=ai_config.retrieval_backend,
            chat_model=ai_config.chat_model,
            embedding_model=ai_config.embedding_model,
            total_requirements=2,
            queued_message="source queued",
            command_id="failed-only-source-command",
            command_sha256="b" * 64,
            scope_schema_version=str(source_envelope["schema_version"]),
            scope_kind="ALL",
            scope_payload_json=str(source_envelope["scope_payload_json"]),
            scope_sha256=str(source_envelope["scope_sha256"]),
            requirement_set_version=int(requirement_set["version"]),
            requirement_tree_sha256=_TREE_SHA256,
            expected_knowledge_base_version=0,
            publication_mode=PER_NODE_RECOVERABLE_PUBLICATION_MODE,
            target_descriptors=[
                {
                    "node_id": f"target-{index}",
                    "requirement_id": requirement["id"],
                    "requirement_code": requirement["req_code"],
                    "source_order": index - 1,
                }
                for index, requirement in enumerate(requirements, start=1)
            ],
        )
        self.assertTrue(source_claimed)
        source_job = db.update_ai_analysis_job(
            str(source_job["id"]),
            status="RUNNING",
            current_stage="assessment",
        )
        for target in db.list_ai_analysis_job_targets(str(source_job["id"])):
            attempt = claim_analysis_node_attempt(
                job_id=str(source_job["id"]),
                target_id=str(target["id"]),
            )
            fail_analysis_node_attempt(
                job_id=str(source_job["id"]),
                target_id=str(target["id"]),
                attempt_id=str(attempt["attempt_id"]),
                fence_token=str(attempt["fence_token"]),
                error_code="provider_timeout",
                error_message="provider timed out",
                retryable=True,
            )
        source_job = finalize_incremental_analysis_job(str(source_job["id"]))
        self.assertEqual(source_job["status"], "FAILED")

        with patch(
            "app.application.requirement_matrix.build_requirement_matrix_snapshot",
            return_value=matrix,
        ):
            _project, _document, _set, retry_envelope = (
                assessment._resolve_document_ai_analysis_scope(
                    project["id"],
                    document["id"],
                    requirement_set_id=requirement_set["id"],
                    expected_tree_sha256=_TREE_SHA256,
                    scope="FAILED_ONLY",
                    node_ids=(),
                    scope_schema_version=ASSESSMENT_SCOPE_SCHEMA_VERSION_V2,
                    source_job_id=str(source_job["id"]),
                )
            )
        request = RunAIAnalysisRequest(
            schema_version="requirement-analysis-scope-request/v2",
            command_id="failed-only-retry-command",
            requirement_set_id=requirement_set["id"],
            expected_tree_sha256=_TREE_SHA256,
            scope="FAILED_ONLY",
            source_job_id=str(source_job["id"]),
            expected_scope_sha256=str(retry_envelope["scope_sha256"]),
        )
        changed_request = request.model_copy(update={"profile_id": "profile-2"})

        class HoldingThread:
            def __init__(self, *args, **kwargs) -> None:
                self._alive = False

            def start(self) -> None:
                self._alive = True

            def is_alive(self) -> bool:
                return self._alive

        prepared = (project, document, rows, rows, [], ai_config)

        def prepare_request(
            _project_id,
            _document_id,
            profile_id=None,
            **_kwargs,
        ):
            if profile_id == "profile-2":
                return (
                    project,
                    document,
                    rows,
                    rows,
                    [],
                    SimpleNamespace(
                        **{
                            **ai_config.__dict__,
                            "profile_id": "profile-2",
                        }
                    ),
                )
            return prepared

        try:
            with (
                patch(
                    "app.application.requirement_matrix.build_requirement_matrix_snapshot",
                    return_value=matrix,
                ),
                patch.object(
                    assessment,
                    "_prepare_document_ai_analysis_request",
                    side_effect=prepare_request,
                ),
                patch.object(assessment, "Thread", HoldingThread),
            ):
                first = assessment.start_document_ai_analysis(
                    project["id"], document["id"], request
                )
                retry_job_id = str(first["job"]["id"])
                db.update_ai_analysis_job(
                    retry_job_id,
                    status="RUNNING",
                    current_stage="assessment",
                )
                retry_targets = db.list_ai_analysis_job_targets(retry_job_id)
                first_attempt = claim_analysis_node_attempt(
                    job_id=retry_job_id,
                    target_id=str(retry_targets[0]["id"]),
                )
                stage_analysis_node_result(
                    job_id=retry_job_id,
                    target_id=str(retry_targets[0]["id"]),
                    attempt_id=str(first_attempt["attempt_id"]),
                    fence_token=str(first_attempt["fence_token"]),
                    result={
                        "assessment": {
                            "ai_decision": "ACCEPT",
                            "ai_reason": "partial retry result",
                            "ai_confidence": 0.9,
                            "assessment_mode": "heuristic",
                            "retrieval_backend": "local",
                            "analysis_label": "REQUIREMENT",
                            "analysis_basis": "source-bound retry",
                            "normalized_requirement": "partial retry result",
                            "risk_level": "LOW",
                            "clarification_question": "",
                            "model_name": "model-1",
                            "prompt_version": "prompt-1",
                        },
                        "evidences": [],
                        "runs": [],
                    },
                    analysis_input_schema_version="analysis-input/v1",
                    analysis_input_sha256="c" * 64,
                )
                publish_staged_analysis_node_result(
                    job_id=retry_job_id,
                    target_id=str(retry_targets[0]["id"]),
                    attempt_id=str(first_attempt["attempt_id"]),
                    fence_token=str(first_attempt["fence_token"]),
                )
                with db.get_conn() as connection:
                    jobs_before_replay = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM ai_analysis_jobs WHERE sor_document_id = ?",
                            (document["id"],),
                        ).fetchone()[0]
                    )
                    events_before_replay = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM ai_analysis_events
                            WHERE sor_document_id = ? AND event_type = 'job_created'
                            """,
                            (document["id"],),
                        ).fetchone()[0]
                    )

                replay = assessment.start_document_ai_analysis(
                    project["id"], document["id"], request
                )
                with self.assertRaises(ApplicationError) as changed:
                    assessment.start_document_ai_analysis(
                        project["id"], document["id"], changed_request
                    )
        finally:
            assessment._clear_ai_analysis_thread(document["id"])

        self.assertEqual(replay["job"]["id"], retry_job_id)
        self.assertEqual(changed.exception.status_code, 409)
        self.assertEqual(
            changed.exception.detail["code"], "analysis_command_id_conflict"
        )
        with db.get_conn() as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM ai_analysis_jobs WHERE sor_document_id = ?",
                        (document["id"],),
                    ).fetchone()[0]
                ),
                jobs_before_replay,
            )
            self.assertEqual(
                int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM ai_analysis_events
                        WHERE sor_document_id = ? AND event_type = 'job_created'
                        """,
                        (document["id"],),
                    ).fetchone()[0]
                ),
                events_before_replay,
            )

    def test_scoped_failure_keeps_last_good_document_and_results(self) -> None:
        project, document, requirement_set, requirements, matrix = (
            self._locked_fixture()
        )
        rows = [
            {
                **(db.get_requirement(requirement["id"]) or {}),
                "matrix_node_id": f"target-{index}",
            }
            for index, requirement in enumerate(requirements, start=1)
        ]
        ai_config = SimpleNamespace(
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
            chat_model="model-1",
            embedding_model="embedding-1",
        )
        prepared = (project, document, rows, rows, [], ai_config)
        envelope = build_assessment_scope(
            matrix,
            requirement_set_version=int(requirement_set["version"]),
            scope="CURRENT",
            requested_node_ids=("target-1",),
        )
        request = RunAIAnalysisRequest(
            command_id="command-failure-1",
            requirement_set_id=requirement_set["id"],
            expected_tree_sha256=_TREE_SHA256,
            scope="CURRENT",
            node_ids=("target-1",),
            expected_scope_sha256=envelope["scope_sha256"],
        )
        before_document = db.get_sor_document(document["id"])
        before_assessments = {
            requirement["id"]: db.get_assessment(requirement["id"])
            for requirement in requirements
        }

        class ImmediateThread:
            def __init__(self, *, target, args, **kwargs) -> None:
                self._target = target
                self._args = args

            def start(self) -> None:
                self._target(*self._args)

            def is_alive(self) -> bool:
                return False

        with (
            patch(
                "app.application.requirement_matrix.build_requirement_matrix_snapshot",
                return_value=matrix,
            ),
            patch.object(
                assessment,
                "_prepare_document_ai_analysis_request",
                return_value=prepared,
            ),
            patch.object(assessment, "Thread", ImmediateThread),
            patch.object(
                assessment,
                "run_ai_copilot_assessment",
                side_effect=RuntimeError("provider failed"),
            ),
        ):
            status = assessment.start_document_ai_analysis(
                project["id"], document["id"], request
            )

        self.assertEqual(status["job"]["status"], "FAILED")
        self.assertIn("provider failed", status["job"]["error_message"])
        self.assertEqual(db.get_sor_document(document["id"]), before_document)
        self.assertEqual(
            {
                requirement["id"]: db.get_assessment(requirement["id"])
                for requirement in requirements
            },
            before_assessments,
        )

    def test_completed_durable_redelivery_does_not_rerun_provider(self) -> None:
        durable = SimpleNamespace(
            job_id="runtime-job-1",
            payload={
                "analysis_job_id": "analysis-job-1",
                "project_id": "project-1",
                "document_id": "document-1",
                "requirement_set_id": "set-1",
                "command_id": "command-1",
                "scope_sha256": "a" * 64,
            },
        )
        completed = {
            "id": "analysis-job-1",
            "project_id": "project-1",
            "sor_document_id": "document-1",
            "requirement_set_id": "set-1",
            "runtime_job_id": "runtime-job-1",
            "command_id": "command-1",
            "scope_sha256": "a" * 64,
            "status": "COMPLETED",
            "completed_requirements": 2,
        }
        with (
            patch.object(db, "get_ai_analysis_job", return_value=completed),
            patch.object(assessment, "_run_document_ai_analysis_worker") as run_worker,
            patch.object(assessment, "run_ai_copilot_assessment") as run_provider,
        ):
            result = assessment.run_durable_document_assessment(
                durable,
                SimpleNamespace(),
            )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["completed_requirements"], 2)
        run_worker.assert_not_called()
        run_provider.assert_not_called()

    def test_scoped_claim_rejects_missing_tree_identity_without_mutation(self) -> None:
        project, document, requirement_set, _requirements, matrix = (
            self._locked_fixture()
        )
        envelope = build_assessment_scope(
            matrix,
            requirement_set_version=int(requirement_set["version"]),
            scope="CURRENT",
            requested_node_ids=("target-1",),
        )
        with db.get_conn() as connection:
            connection.execute(
                "UPDATE requirement_sets SET requirement_tree_sha256 = '' WHERE id = ?",
                (requirement_set["id"],),
            )
        before = db.get_requirement_set(requirement_set["id"])

        with self.assertRaisesRegex(ValueError, "requirement_set_selection_changed"):
            claim_analysis_job(
                project_id=project["id"],
                document_id=document["id"],
                requirement_set_id=requirement_set["id"],
                profile_id="profile-1",
                provider="provider-1",
                assessment_mode="heuristic",
                retrieval_backend="local",
                chat_model="model-1",
                embedding_model="embedding-1",
                total_requirements=1,
                queued_message="queued",
                command_id="command-tree-1",
                command_sha256="b" * 64,
                scope_schema_version=str(envelope["schema_version"]),
                scope_kind="CURRENT",
                scope_payload_json=str(envelope["scope_payload_json"]),
                scope_sha256=str(envelope["scope_sha256"]),
                requirement_set_version=int(requirement_set["version"]),
                requirement_tree_sha256=_TREE_SHA256,
                publication_mode="ATOMIC_BATCH",
            )

        self.assertEqual(db.get_requirement_set(requirement_set["id"]), before)
        with db.get_conn() as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM ai_analysis_jobs"
                    ).fetchone()[0]
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
