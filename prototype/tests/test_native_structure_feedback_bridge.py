from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.application.agent_workflow import native_structure_bridge
from app.application.agent_workflow.native_structure_bridge import (
    NativeStructureContinueRequestV1,
    NativeStructureFeedbackRequestV1,
    NativeStructureHandoffAckRequestV1,
    NativeStructureProposalDecisionRequestV1,
    NativeStructureRenderedReopenRequestV1,
)
from app.application.agent_workflow.structure_tool_loop import (
    NativeStructureHostToolRequestV1,
    NativeStructureHandoffAttemptReceiptV1,
    NativeStructureHandoffAttemptRequestV1,
    StructureAgentCandidateRevisionV1,
    StructureProjectionOperationKind,
    StructureProjectionOperationV1,
    StructureRevisionBasisRole,
    StructureToolCallReceiptV1,
    StructureToolIntent,
    StructureWorkItemDispositionV1,
    build_native_structure_handoff_attempt_receipt,
    stable_structure_agent_revision_id,
)
from app.application.agent_workflow.native_structure_working_revision import (
    NativeStructureWorkingRevisionHeadV1,
)
from app.application.errors import ApplicationError
from app.adapters.persistence.memory_agent_structure_loop import (
    InMemoryAgentStructureLoopRepository,
)
from app.adapters.agents.codex_app_server.canonical_dynamic_tools import (
    CanonicalStructureRevisionPublishError,
    CanonicalStructureRevisionDraftV1,
    CanonicalStructureRevisionSubmissionInputV1,
)
from app.adapters.agents.codex_app_server.native_structure_revision_publisher import (
    NativeStructureRevisionPublisher,
)
from app.ports.agent_structure_loop_repository import AgentStructureLoopConflict


_SOURCE_SHA256 = "a" * 64
_RUN_ID = "native-structure-test-run-v1"


class NativeStructureFeedbackBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.reference_root = root / "references"
        self.feedback_root = root / "feedback"
        self.run_root = self.reference_root / _RUN_ID
        self.run_root.mkdir(parents=True)
        self._write_reference()
        self.patches = (
            patch.object(
                native_structure_bridge,
                "REFERENCE_ROOT",
                self.reference_root,
            ),
            patch.object(
                native_structure_bridge,
                "FEEDBACK_ROOT",
                self.feedback_root,
            ),
            patch.object(
                native_structure_bridge,
                "_require_project_document",
                return_value={
                    "id": "document-1",
                    "project_id": "project-1",
                    "source_sha256": _SOURCE_SHA256,
                },
            ),
        )
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_reading_projection_is_outside_document_map_identity(self) -> None:
        from app.application.agent_workflow.source_requirement_baseline import compile_document_map_artifact

        original = native_structure_bridge.get_native_structure_reference(
            "project-1", "document-1", SimpleNamespace(headers={}))
        proof = {"content-1": {"display_text": "Readable source", "raw_text_preserved": True}}
        with patch.object(native_structure_bridge, "_content_reading_projections", return_value=proof):
            viewed = native_structure_bridge.get_native_structure_reference(
                "project-1", "document-1", SimpleNamespace(headers={}))
        self.assertEqual(viewed["reading_projections"], proof)
        self.assertEqual(viewed["document_tree"], original["document_tree"])
        self.assertEqual(compile_document_map_artifact(viewed).artifact_sha256,
                         compile_document_map_artifact(original).artifact_sha256)

    def test_get_returns_verified_reference_without_feedback(self) -> None:
        response = SimpleNamespace(headers={})

        result = native_structure_bridge.get_native_structure_reference(
            "project-1",
            "document-1",
            response,
        )

        self.assertEqual(
            result["schema_version"], "native-structure-reference-public/v1"
        )
        self.assertEqual(result["artifact_id"], _RUN_ID)
        self.assertEqual(result["summary"]["top_level_logical_tables"], 1)
        self.assertEqual(result["logical_tables"][0]["pages"], [1])
        self.assertEqual(result["unresolved"][0]["pages"], [1])
        self.assertEqual(
            result["document_tree"]["root_ids"],
            ["structure-root"],
        )
        self.assertEqual(
            result["document_tree"]["nodes"][1]["content_blocks"],
            [
                {
                    "kind": "LOGICAL_TABLE",
                    "logical_table_scope_id": "lt-1",
                }
            ],
        )
        self.assertEqual(result["document_tree"]["nodes"][1]["order"], 0)
        self.assertEqual(
            result["document_tree"]["nodes"][0]["source_anchors"][0]["precision"],
            "EXACT_NATIVE_SPAN",
        )
        self.assertEqual(
            result["document_tree"]["nodes"][1]["source_anchors"][0]["anchor_kind"],
            "TABLE_REGION",
        )
        self.assertEqual(
            result["document_tree"]["nodes"][1]["source_content"][0]["text"],
            "1 概述",
        )
        self.assertEqual(result["feedbacks"], [])
        self.assertEqual(len(result["structure_proposals"]), 1)
        self.assertEqual(
            result["structure_proposals"][0]["status"],
            "AUTO_APPLIED_PENDING_REVIEW",
        )
        self.assertEqual(result["feedback_capability"]["status"], "QUEUE_READY")
        self.assertEqual(result["continuation"]["status"], "IDLE")
        self.assertFalse(result["continuation"]["can_continue"])
        self.assertEqual(
            result["structure_projection"],
            {
                "schema_version": "native-structure-projection-state/v1",
                "state": "ACCEPTED",
                "base_artifact_id": _RUN_ID,
                "attempt_id": None,
                "attempt_ordinal": None,
                "affected_scope_ids": [],
                "pending_rendered_scope_ids": [],
                "validation_status": "PASS",
                "projection_sha256": native_structure_bridge._canonical_sha256(
                    result["document_tree"]
                ),
            },
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    def test_scope_pages_expand_disjoint_ranges_and_table_tokens(self) -> None:
        pages = native_structure_bridge._scope_pages(
            "P3-P5,P11-P12,T0010",
            table_pages={"T0010": 10},
            page_count=29,
        )

        self.assertEqual(pages, [3, 4, 5, 10, 11, 12])

    def test_activity_exposes_the_lightweight_working_revision_head(self) -> None:
        head = NativeStructureWorkingRevisionHeadV1(
            run_id="agent-run-" + "1" * 32,
            source_sha256=_SOURCE_SHA256,
            base_artifact_id=_RUN_ID,
            base_projection_sha256="2" * 64,
            evidence_artifact_id="evidence-artifact-1",
            revision_id="native-structure-working-revision-" + "3" * 32,
            revision=2,
            previous_revision_sha256="4" * 64,
            revision_sha256="5" * 64,
            validator_kind="D2_HOST_COMPILER",
            d2_compiler_receipt_sha256="6" * 64,
            state="WORKING",
            quality_claim="D2_HOST_VERIFIED",
            created_at_utc=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        )
        repository = Mock()
        repository.get_working_revision_head.return_value = head
        repository.list_attempts.return_value = ()
        repository.list_activity.return_value = ()

        with patch.object(
            native_structure_bridge,
            "_structure_loop_repository",
            return_value=repository,
        ):
            stream = native_structure_bridge.get_native_structure_activity(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )

        self.assertEqual(
            stream["working_revision_head"],
            head.model_dump(mode="json"),
        )
        repository.get_working_revision_object.assert_not_called()

    def test_attempts_use_global_creation_order_across_handoffs(self) -> None:
        def attempt(
            *,
            handoff_id: str,
            command_id: str,
            created_at_utc: datetime,
            operation_kind: StructureProjectionOperationKind,
            candidate_sha256: str,
        ) -> dict[str, object]:
            operation = StructureProjectionOperationV1(
                operation=operation_kind,
                target_scope_id="structure-section-1",
            )
            revision_values = {
                "target_scope_id": "structure-section-1",
                "basis_role": StructureRevisionBasisRole.CURRENT_MATERIALIZATION,
                "basis_sha256": "1" * 64,
                "revised_candidate_sha256": candidate_sha256,
                "operations": (operation,),
                "evidence_refs": ("source-section-1",),
                "tool_call_ids": (),
            }
            revision = StructureAgentCandidateRevisionV1(
                revision_id=stable_structure_agent_revision_id(**revision_values),
                **revision_values,
            )
            request = NativeStructureHandoffAttemptRequestV1(
                command_id=command_id,
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=handoff_id,
                attempt_ordinal=1,
                affected_scope_ids=("structure-section-1",),
                candidate_revisions=(revision,),
                materialized_candidate_sha256=candidate_sha256,
                rendered_output_sha256="9" * 64,
                materialization_observation={
                    "confirmed_actions": [
                        {
                            "action_id": command_id,
                            "materialization_receipt_id": "materialized",
                            "verification_receipt_id": "verified",
                            "rendered_output_reopened": True,
                        }
                    ]
                },
                current_action="已完成 Host 复验。",
                public_summary="当前结构投影已通过 Host 复验。",
            )
            receipt = build_native_structure_handoff_attempt_receipt(
                request,
                validation_report={"status": "PASS", "findings": []},
                selected_failure_patterns={"selected_patterns": []},
                kit_version="0.8.10",
                kit_bundle_sha256="8" * 64,
            ).model_dump(mode="json")
            receipt["created_at_utc"] = created_at_utc.isoformat()
            return receipt

        older = attempt(
            handoff_id="handoff-z-older",
            command_id="attempt-older",
            created_at_utc=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
            operation_kind=StructureProjectionOperationKind.PRESERVE_SOURCE_LABEL,
            candidate_sha256="2" * 64,
        )
        newer = attempt(
            handoff_id="handoff-a-newer",
            command_id="attempt-newer",
            created_at_utc=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
            operation_kind=(
                StructureProjectionOperationKind.COLLAPSE_REPEATED_SOURCE_LINES
            ),
            candidate_sha256="3" * 64,
        )
        with (
            patch.object(
                native_structure_bridge,
                "_read_handoff_attempts",
                return_value=[older, newer],
            ),
            patch.object(
                native_structure_bridge,
                "_structure_loop_repository",
                return_value=None,
            ),
        ):
            ordered = native_structure_bridge._read_all_handoff_attempts(
                project_id="project-1",
                document_id="document-1",
                source_sha256=_SOURCE_SHA256,
                artifact_id=_RUN_ID,
            )

        adopted = native_structure_bridge._adopted_projection_attempts(ordered)
        operations = native_structure_bridge._projection_operations_from_attempts(
            adopted
        )
        self.assertEqual(
            [item["handoff_id"] for item in ordered],
            ["handoff-z-older", "handoff-a-newer"],
        )
        self.assertEqual(
            [item.operation for item in operations],
            [
                StructureProjectionOperationKind.PRESERVE_SOURCE_LABEL,
                StructureProjectionOperationKind.COLLAPSE_REPEATED_SOURCE_LINES,
            ],
        )
        self.assertEqual(adopted[-1].handoff_id, "handoff-a-newer")

    def test_projection_state_unions_two_unresolved_working_handoffs(self) -> None:
        root_working = self._projection_attempt_receipt(
            handoff_id="handoff-root-working",
            command_id="attempt-root-working",
            scope_id="structure-root",
            created_at_utc=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
            candidate_sha256="4" * 64,
            verification_required=True,
        )
        section_working = self._projection_attempt_receipt(
            handoff_id="handoff-section-working",
            command_id="attempt-section-working",
            scope_id="structure-section-1",
            created_at_utc=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
            candidate_sha256="5" * 64,
            verification_required=True,
        )

        public = native_structure_bridge._serialize_public_reference(
            native_structure_bridge._load_latest_reference(_SOURCE_SHA256),
            feedbacks=[],
            handoffs=[
                {
                    "handoff_id": "handoff-section-working",
                    "feedback_ids": [],
                    "proposal_decision_ids": [],
                },
                {
                    "handoff_id": "handoff-root-working",
                    "feedback_ids": [],
                    "proposal_decision_ids": [],
                },
            ],
            handoff_attempts=[section_working, root_working],
        )

        projection = public["structure_projection"]
        self.assertEqual(projection["state"], "WORKING_VERIFICATION_REQUIRED")
        self.assertEqual(
            projection["attempt_id"],
            root_working["attempt_id"],
        )
        self.assertEqual(
            projection["affected_scope_ids"],
            ["structure-section-1", "structure-root"],
        )
        self.assertEqual(
            projection["pending_rendered_scope_ids"],
            ["structure-section-1", "structure-root"],
        )
        self.assertEqual(projection["validation_status"], "FAIL")
        self.assertEqual(
            projection["projection_sha256"],
            native_structure_bridge._canonical_sha256(public["document_tree"]),
        )

    def test_later_verified_handoff_does_not_hide_earlier_working_handoff(
        self,
    ) -> None:
        earlier_working = self._projection_attempt_receipt(
            handoff_id="handoff-z-working",
            command_id="attempt-z-working",
            scope_id="structure-root",
            created_at_utc=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
            candidate_sha256="6" * 64,
            verification_required=True,
        )
        later_verified = self._projection_attempt_receipt(
            handoff_id="handoff-a-verified",
            command_id="attempt-a-verified",
            scope_id="structure-section-1",
            created_at_utc=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
            candidate_sha256="7" * 64,
            verification_required=False,
        )

        public = native_structure_bridge._serialize_public_reference(
            native_structure_bridge._load_latest_reference(_SOURCE_SHA256),
            feedbacks=[],
            handoffs=[
                {
                    "handoff_id": "handoff-z-working",
                    "feedback_ids": [],
                    "proposal_decision_ids": [],
                },
                {
                    "handoff_id": "handoff-a-verified",
                    "feedback_ids": [],
                    "proposal_decision_ids": [],
                },
            ],
            handoff_attempts=[earlier_working, later_verified],
        )

        projection = public["structure_projection"]
        self.assertEqual(projection["state"], "WORKING_VERIFICATION_REQUIRED")
        self.assertEqual(projection["attempt_id"], earlier_working["attempt_id"])
        self.assertEqual(projection["affected_scope_ids"], ["structure-root"])
        self.assertEqual(
            projection["pending_rendered_scope_ids"],
            ["structure-root"],
        )
        self.assertEqual(projection["validation_status"], "FAIL")
        self.assertEqual(
            projection["projection_sha256"],
            native_structure_bridge._canonical_sha256(public["document_tree"]),
        )

    def test_activity_and_continuation_surface_earlier_working_handoff(
        self,
    ) -> None:
        handoffs = [
            {
                "handoff_id": "handoff-z-working",
                "status": "IN_PROGRESS",
                "feedback_ids": [],
                "proposal_decision_ids": [],
                "created_at_utc": "2026-08-31T08:00:00+00:00",
            },
            {
                "handoff_id": "handoff-a-verified",
                "status": "IN_PROGRESS",
                "feedback_ids": [],
                "proposal_decision_ids": [],
                "created_at_utc": "2026-08-31T09:00:00+00:00",
            },
        ]
        earlier_working = self._projection_attempt_receipt(
            handoff_id="handoff-z-working",
            command_id="activity-attempt-z-working",
            scope_id="structure-root",
            created_at_utc=datetime(2026, 8, 31, 8, 30, tzinfo=UTC),
            candidate_sha256="a" * 64,
            verification_required=True,
        )
        later_verified = self._projection_attempt_receipt(
            handoff_id="handoff-a-verified",
            command_id="activity-attempt-a-verified",
            scope_id="structure-section-1",
            created_at_utc=datetime(2026, 8, 31, 9, 30, tzinfo=UTC),
            candidate_sha256="b" * 64,
            verification_required=False,
        )
        attempts = [earlier_working, later_verified]

        with (
            patch.object(
                native_structure_bridge,
                "_read_handoffs",
                return_value=handoffs,
            ),
            patch.object(
                native_structure_bridge,
                "_read_all_handoff_attempts",
                return_value=attempts,
            ),
            patch.object(
                native_structure_bridge,
                "_list_structure_loop_activity",
                return_value=[],
            ) as list_activity,
        ):
            activity = native_structure_bridge.get_native_structure_activity(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )

        self.assertEqual(activity["handoff_id"], "handoff-z-working")
        self.assertEqual(
            activity["continuation"]["latest_handoff"]["handoff_id"],
            "handoff-z-working",
        )
        self.assertEqual(activity["continuation"]["status"], "VERIFYING")
        self.assertEqual(
            list_activity.call_args.kwargs["handoff_id"],
            "handoff-z-working",
        )

    def test_verifying_one_working_handoff_promotes_the_next_unresolved_lineage(
        self,
    ) -> None:
        handoffs = [
            {
                "handoff_id": "handoff-1",
                "status": "IN_PROGRESS",
                "feedback_ids": [],
                "proposal_decision_ids": [],
            },
            {
                "handoff_id": "handoff-2",
                "status": "IN_PROGRESS",
                "feedback_ids": [],
                "proposal_decision_ids": [],
            },
        ]
        handoff_1_working = self._projection_attempt_receipt(
            handoff_id="handoff-1",
            command_id="handoff-1-working",
            scope_id="structure-root",
            created_at_utc=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
            candidate_sha256="c" * 64,
            verification_required=True,
        )
        handoff_2_working = self._projection_attempt_receipt(
            handoff_id="handoff-2",
            command_id="handoff-2-working",
            scope_id="structure-section-1",
            created_at_utc=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
            candidate_sha256="d" * 64,
            verification_required=True,
        )
        both_working = [handoff_1_working, handoff_2_working]
        initial = native_structure_bridge._public_continuation(
            feedbacks=[],
            proposal_decisions=[],
            handoffs=handoffs,
            handoff_events=[],
            handoff_attempts=both_working,
            proposals=[],
        )
        self.assertEqual(initial["latest_handoff"]["handoff_id"], "handoff-2")

        handoff_2_verified = self._projection_attempt_receipt(
            handoff_id="handoff-2",
            command_id="handoff-2-verified",
            scope_id="structure-section-1",
            created_at_utc=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
            candidate_sha256="e" * 64,
            verification_required=False,
            previous_attempt=handoff_2_working,
        )
        promoted = native_structure_bridge._public_continuation(
            feedbacks=[],
            proposal_decisions=[],
            handoffs=handoffs,
            handoff_events=[],
            handoff_attempts=[*both_working, handoff_2_verified],
            proposals=[],
        )
        self.assertEqual(promoted["latest_handoff"]["handoff_id"], "handoff-1")
        self.assertEqual(promoted["status"], "VERIFYING")

        handoff_1_verified = self._projection_attempt_receipt(
            handoff_id="handoff-1",
            command_id="handoff-1-verified",
            scope_id="structure-root",
            created_at_utc=datetime(2026, 8, 31, 11, 0, tzinfo=UTC),
            candidate_sha256="f" * 64,
            verification_required=False,
            previous_attempt=handoff_1_working,
        )
        resolved = native_structure_bridge._public_continuation(
            feedbacks=[],
            proposal_decisions=[],
            handoffs=handoffs,
            handoff_events=[],
            handoff_attempts=[
                *both_working,
                handoff_2_verified,
                handoff_1_verified,
            ],
            proposals=[],
        )
        self.assertEqual(resolved["latest_handoff"]["handoff_id"], "handoff-2")
        self.assertEqual(resolved["status"], "CONSUMED")

    def test_rendered_reopen_requires_authoritative_global_projection_head(
        self,
    ) -> None:
        handoffs = [
            {
                "handoff_id": "handoff-1-working",
                "status": "IN_PROGRESS",
                "feedback_ids": [],
                "proposal_decision_ids": [],
                "affected_node_ids": ["structure-root"],
            },
            {
                "handoff_id": "handoff-2-working",
                "status": "IN_PROGRESS",
                "feedback_ids": [],
                "proposal_decision_ids": [],
                "affected_node_ids": ["structure-root"],
            },
        ]
        handoff_1_raw = self._projection_attempt_receipt(
            handoff_id="handoff-1-working",
            command_id="handoff-1-global-working",
            scope_id="structure-root",
            created_at_utc=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
            candidate_sha256="4" * 64,
            verification_required=True,
            operation=StructureProjectionOperationV1(
                operation=(
                    StructureProjectionOperationKind.COLLAPSE_NAVIGATION_CHILDREN
                ),
                target_scope_id="structure-root",
                related_refs=("structure-section-1",),
            ),
        )
        handoff_2_raw = self._projection_attempt_receipt(
            handoff_id="handoff-2-working",
            command_id="handoff-2-global-working",
            scope_id="structure-root",
            created_at_utc=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
            candidate_sha256="5" * 64,
            verification_required=True,
        )
        raw_attempts = [handoff_1_raw, handoff_2_raw]
        attempts = tuple(
            NativeStructureHandoffAttemptReceiptV1.model_validate(item)
            for item in raw_attempts
        )
        bundle = native_structure_bridge._load_latest_reference(_SOURCE_SHA256)
        global_public = native_structure_bridge._serialize_public_reference(
            bundle,
            feedbacks=[],
            handoffs=handoffs,
            handoff_attempts=raw_attempts,
        )
        lineage_only_public = native_structure_bridge._serialize_public_reference(
            bundle,
            feedbacks=[],
            handoffs=[handoffs[-1]],
            handoff_attempts=[handoff_2_raw],
        )
        global_root = global_public["document_tree"]["nodes"][0]
        lineage_root = lineage_only_public["document_tree"]["nodes"][0]
        self.assertNotEqual(
            native_structure_bridge._canonical_sha256(global_root),
            native_structure_bridge._canonical_sha256(lineage_root),
        )

        def original_request(
            attempt: NativeStructureHandoffAttemptReceiptV1,
        ) -> NativeStructureHandoffAttemptRequestV1:
            revision = attempt.candidate_revisions[0]
            return NativeStructureHandoffAttemptRequestV1(
                command_id=f"original-{attempt.command_id}",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=attempt.handoff_id,
                attempt_ordinal=attempt.attempt_ordinal,
                affected_scope_ids=attempt.affected_scope_ids,
                candidate_revisions=attempt.candidate_revisions,
                materialized_candidate_sha256=(
                    attempt.materialized_candidate_sha256
                ),
                rendered_output_sha256=attempt.rendered_output_sha256,
                materialization_observation={
                    "confirmed_actions": [
                        {
                            "action_id": revision.revision_id,
                            "materialization_receipt_id": "materialized",
                            "verification_receipt_id": "",
                            "rendered_output_reopened": False,
                        }
                    ]
                },
                current_action="等待全局投影复验。",
                public_summary="全局工作投影尚待重新打开。",
            )

        durable_attempts = (attempts[1],)
        requests = {
            attempts[1].attempt_id: original_request(attempts[1])
        }
        repository = SimpleNamespace(
            get_working_revision_head=lambda **_kwargs: None,
            list_attempts=lambda **_kwargs: durable_attempts,
            get_attempt_request=lambda **kwargs: requests.get(kwargs["attempt_id"]),
        )
        expected_projection_sha256 = global_public["structure_projection"][
            "projection_sha256"
        ]
        expected_projection_attempts_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "schema_version": (
                        "native-structure-projection-attempt-set/v1"
                    ),
                    "attempts": [
                        {
                            "attempt_id": attempt.attempt_id,
                            "receipt_sha256": attempt.receipt_sha256,
                            "status": attempt.status.value,
                        }
                        for attempt in durable_attempts
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        def successful_submit(*_args, **kwargs) -> dict[str, object]:
            self.assertTrue(native_structure_bridge._FEEDBACK_LOCK._is_owned())
            self.assertIs(kwargs["structure_loop_repository"], repository)
            self.assertEqual(
                kwargs["expected_global_projection_attempts_sha256"],
                expected_projection_attempts_sha256,
            )
            return {
                "attempt": {"status": "VERIFIED"},
                "next_action": "COMPLETE",
            }

        with (
            patch.object(
                native_structure_bridge,
                "_structure_loop_repository",
                return_value=repository,
            ),
            patch.object(
                native_structure_bridge,
                "_read_handoffs",
                return_value=handoffs,
            ),
            patch.object(
                native_structure_bridge,
                "_read_handoff_attempts",
                return_value=[handoff_1_raw],
            ),
            patch.object(
                native_structure_bridge,
                "submit_native_structure_handoff_attempt",
                side_effect=successful_submit,
            ) as submit_attempt,
        ):
            with self.assertRaises(ApplicationError) as stale_attempt:
                native_structure_bridge.acknowledge_native_structure_rendered_reopen(
                    "project-1",
                    "document-1",
                    NativeStructureRenderedReopenRequestV1(
                        command_id="ack-stale-working-attempt",
                        expected_source_sha256=_SOURCE_SHA256,
                        expected_artifact_id=_RUN_ID,
                        handoff_id=attempts[0].handoff_id,
                        attempt_id=attempts[0].attempt_id,
                        scope_id="structure-root",
                        expected_projection_sha256=expected_projection_sha256,
                        expected_rendered_output_sha256=(
                            attempts[0].rendered_output_sha256
                        ),
                    ),
                    SimpleNamespace(headers={}),
                )
            self.assertEqual(
                stale_attempt.exception.detail["code"],
                "native_structure_rendered_reopen_projection_not_current",
            )

            with self.assertRaises(ApplicationError) as wrong_projection:
                native_structure_bridge.acknowledge_native_structure_rendered_reopen(
                    "project-1",
                    "document-1",
                    NativeStructureRenderedReopenRequestV1(
                        command_id="ack-wrong-projection-sha",
                        expected_source_sha256=_SOURCE_SHA256,
                        expected_artifact_id=_RUN_ID,
                        handoff_id=attempts[1].handoff_id,
                        attempt_id=attempts[1].attempt_id,
                        scope_id="structure-root",
                        expected_projection_sha256="0" * 64,
                        expected_rendered_output_sha256=(
                            attempts[1].rendered_output_sha256
                        ),
                    ),
                    SimpleNamespace(headers={}),
                )
            self.assertEqual(
                wrong_projection.exception.detail["code"],
                "native_structure_rendered_reopen_projection_changed",
            )

            accepted = (
                native_structure_bridge.acknowledge_native_structure_rendered_reopen(
                    "project-1",
                    "document-1",
                    NativeStructureRenderedReopenRequestV1(
                        command_id="ack-authoritative-global-projection",
                        expected_source_sha256=_SOURCE_SHA256,
                        expected_artifact_id=_RUN_ID,
                        handoff_id=attempts[1].handoff_id,
                        attempt_id=attempts[1].attempt_id,
                        scope_id="structure-root",
                        expected_projection_sha256=expected_projection_sha256,
                        expected_rendered_output_sha256=(
                            attempts[1].rendered_output_sha256
                        ),
                    ),
                    SimpleNamespace(headers={}),
                )
            )

        expected_reopen_sha256 = native_structure_bridge._canonical_sha256(
            {
                "attempt_id": attempts[1].attempt_id,
                "rendered_output_sha256": attempts[1].rendered_output_sha256,
                "scope_id": "structure-root",
                "public_node_sha256": native_structure_bridge._canonical_sha256(
                    global_root
                ),
                "rendered_table_projections": [],
            }
        )
        lineage_only_reopen_sha256 = native_structure_bridge._canonical_sha256(
            {
                "attempt_id": attempts[1].attempt_id,
                "rendered_output_sha256": attempts[1].rendered_output_sha256,
                "scope_id": "structure-root",
                "public_node_sha256": native_structure_bridge._canonical_sha256(
                    lineage_root
                ),
                "rendered_table_projections": [],
            }
        )
        self.assertEqual(accepted["reopen_receipt_sha256"], expected_reopen_sha256)
        self.assertNotEqual(
            accepted["reopen_receipt_sha256"],
            lineage_only_reopen_sha256,
        )
        submit_attempt.assert_called_once()

        append_attempt = Mock(
            side_effect=AgentStructureLoopConflict(
                "global projection head changed"
            )
        )
        cas_repository = SimpleNamespace(
            get_working_revision_head=lambda **_kwargs: None,
            list_attempts=lambda **_kwargs: durable_attempts,
            get_attempt_request=lambda **kwargs: requests.get(kwargs["attempt_id"]),
            append_attempt=append_attempt,
        )
        with (
            patch.object(
                native_structure_bridge,
                "_structure_loop_repository",
                return_value=cas_repository,
            ),
            patch.object(
                native_structure_bridge,
                "_read_handoffs",
                return_value=handoffs,
            ),
            patch.object(
                native_structure_bridge,
                "_read_handoff_attempts",
                return_value=[handoff_1_raw],
            ),
            patch.object(
                native_structure_bridge,
                "_read_handoff_events",
                return_value=[
                    {
                        "handoff_id": attempts[1].handoff_id,
                        "status": "ACKNOWLEDGED",
                    }
                ],
            ),
        ):
            with self.assertRaises(ApplicationError) as cas_conflict:
                native_structure_bridge.acknowledge_native_structure_rendered_reopen(
                    "project-1",
                    "document-1",
                    NativeStructureRenderedReopenRequestV1(
                        command_id="ack-global-projection-cas-conflict",
                        expected_source_sha256=_SOURCE_SHA256,
                        expected_artifact_id=_RUN_ID,
                        handoff_id=attempts[1].handoff_id,
                        attempt_id=attempts[1].attempt_id,
                        scope_id="structure-root",
                        expected_projection_sha256=expected_projection_sha256,
                        expected_rendered_output_sha256=(
                            attempts[1].rendered_output_sha256
                        ),
                    ),
                    SimpleNamespace(headers={}),
                )
        self.assertEqual(cas_conflict.exception.status_code, 409)
        self.assertEqual(
            cas_conflict.exception.detail["code"],
            "native_structure_handoff_attempt_persistence_conflict",
        )
        self.assertEqual(
            append_attempt.call_args.kwargs["expected_global_head_attempt_id"],
            attempts[1].attempt_id,
        )
        self.assertEqual(
            append_attempt.call_args.kwargs[
                "expected_global_projection_attempts_sha256"
            ],
            expected_projection_attempts_sha256,
        )

    def test_canonical_column_projection_absorbs_sub_point_page_drift(self) -> None:
        def cell(
            cell_id: str,
            column: int,
            text: str,
            left: float,
            right: float,
        ) -> dict[str, object]:
            return {
                "cell_id": cell_id,
                "row": 0,
                "column": column,
                "row_span": 1,
                "column_span": 1,
                "text": text,
                "bbox": [left, 0.0, right, 20.0],
            }

        front_projection = native_structure_bridge._canonical_table_column_projection(
            [
                {
                    "fragment_id": "front-page-3",
                    "row_count": 1,
                    "bbox": [31.44, 0.0, 562.56, 20.0],
                    "cells": [
                        cell("front-header-sequence", 0, "序号", 31.44, 75.12),
                        cell("front-header-item", 2, "项目", 75.12, 183.72),
                        cell(
                            "front-header-requirement",
                            4,
                            "要求",
                            183.72,
                            562.56,
                        ),
                    ],
                },
                {
                    "fragment_id": "front-page-4",
                    "row_count": 1,
                    "bbox": [31.98, 0.0, 562.772, 20.0],
                    "cells": [
                        cell("front-7", 0, "7", 31.98, 53.82),
                        cell("front-7-gap", 1, "", 53.82, 75.518),
                        cell("front-space", 2, "空间硬点坐标", 75.518, 97.32),
                        cell("front-space-gap", 3, "", 97.32, 184.119),
                        cell("front-value", 4, "", 184.119, 205.92),
                        cell("front-value-gap", 5, "", 205.92, 562.772),
                    ],
                },
            ],
            canonical_column_count=3,
        )
        front_mappings = {
            item["cell_id"]: (item["column"], item["column_span"])
            for item in front_projection["fragments"][1]["cell_mappings"]
        }
        self.assertEqual(front_mappings["front-7"], (0, 1))
        self.assertEqual(front_mappings["front-7-gap"], (0, 1))
        self.assertEqual(front_mappings["front-space"], (1, 1))
        self.assertEqual(front_mappings["front-space-gap"], (1, 1))
        self.assertEqual(front_mappings["front-value"], (2, 1))
        self.assertEqual(front_mappings["front-value-gap"], (2, 1))
        self.assertEqual(front_projection["status"], "EXACT")
        self.assertEqual(len(front_projection["projection_sha256"]), 64)

        rear_projection = native_structure_bridge._canonical_table_column_projection(
            [
                {
                    "fragment_id": "rear-page-4",
                    "row_count": 1,
                    "bbox": [31.98, 0.0, 562.772, 20.0],
                    "cells": [
                        cell("rear-header-sequence", 0, "序号", 31.98, 75.518),
                        cell("rear-header-item", 2, "项目", 75.518, 184.119),
                        cell(
                            "rear-header-requirement",
                            4,
                            "要求",
                            184.119,
                            562.772,
                        ),
                    ],
                },
                {
                    "fragment_id": "rear-page-5",
                    "row_count": 1,
                    "bbox": [31.44, 0.0, 562.56, 20.0],
                    "cells": [
                        cell("rear-8", 0, "8", 31.44, 75.12),
                        cell("rear-space", 1, "空间硬点坐标", 75.12, 183.72),
                        cell("rear-value", 2, "", 183.72, 562.56),
                    ],
                },
            ],
            canonical_column_count=3,
        )
        rear_mappings = {
            item["cell_id"]: (item["column"], item["column_span"])
            for item in rear_projection["fragments"][1]["cell_mappings"]
        }
        self.assertEqual(
            rear_mappings,
            {
                "rear-8": (0, 1),
                "rear-space": (1, 1),
                "rear-value": (2, 1),
            },
        )

        with self.assertRaisesRegex(ValueError, "does not exactly cover"):
            native_structure_bridge._canonical_table_column_projection(
                [
                    {
                        "fragment_id": "missing-column",
                        "row_count": 1,
                        "bbox": [0.0, 0.0, 300.0, 20.0],
                        "cells": [
                            cell("header-1", 0, "序号", 0.0, 50.0),
                            cell("header-2", 1, "项目", 50.0, 150.0),
                            cell("header-3", 2, "要求", 150.0, 300.0),
                        ],
                    },
                    {
                        "fragment_id": "missing-column-continuation",
                        "row_count": 1,
                        "bbox": [0.0, 0.0, 300.0, 20.0],
                        "cells": [
                            cell("value-1", 0, "1", 0.0, 50.0),
                            cell("value-2", 1, "项目", 50.0, 150.0),
                        ],
                    },
                ],
                canonical_column_count=3,
            )

    def test_verified_tree_projection_collapses_repeated_lines_and_leaf_navigation(
        self,
    ) -> None:
        tree = {
            "schema_version": "native-document-structure-tree/v1",
            "root_ids": ["parent"],
            "nodes": [
                {
                    "node_id": "parent",
                    "parent_id": None,
                    "source_content": [
                        {
                            "content_id": "content-parent",
                            "page": 1,
                            "bbox": [0, 0, 10, 10],
                            "text": "标题\n标题\n正文\n项目\n项目：YX\nYX",
                        }
                    ],
                    "source_anchors": [],
                },
                {
                    "node_id": "child",
                    "parent_id": "parent",
                    "source_content": [
                        {
                            "content_id": "content-child",
                            "page": 1,
                            "bbox": [0, 11, 10, 20],
                            "text": "附件一",
                        }
                    ],
                    "source_anchors": [],
                },
            ],
        }
        operations = (
            StructureProjectionOperationV1(
                operation=(
                    StructureProjectionOperationKind.COLLAPSE_REPEATED_SOURCE_LINES
                ),
                target_scope_id="parent",
            ),
            StructureProjectionOperationV1(
                operation=(
                    StructureProjectionOperationKind.COLLAPSE_NAVIGATION_CHILDREN
                ),
                target_scope_id="parent",
                related_refs=("child",),
            ),
        )

        projected = native_structure_bridge._apply_tree_projection_operations(
            tree,
            operations=operations,
        )

        self.assertEqual(
            [item["node_id"] for item in projected["nodes"]],
            ["parent"],
        )
        parent = projected["nodes"][0]
        self.assertNotIn("display_text", parent["source_content"][0])
        self.assertEqual(
            parent["source_content"][0]["text"],
            "标题\n标题\n正文\n项目\n项目：YX\nYX",
        )
        self.assertEqual(parent["source_content"][1]["text"], "附件一")

    def test_verified_tree_projection_excludes_descendant_table_content(self) -> None:
        tree = {
            "schema_version": "native-document-structure-tree/v1",
            "root_ids": ["parent"],
            "nodes": [
                {
                    "node_id": "parent",
                    "parent_id": None,
                    "source_content": [
                        {
                            "content_id": "duplicate-table-text",
                            "page": 1,
                            "bbox": [0, 0, 10, 10],
                            "text": "表格内文字",
                        }
                    ],
                    "source_anchors": [],
                },
                {
                    "node_id": "child-table",
                    "parent_id": "parent",
                    "source_content": [],
                    "source_anchors": [
                        {
                            "anchor_kind": "TABLE_REGION",
                            "page": 1,
                            "bbox": [0, 0, 10, 10],
                            "logical_table_scope_id": "table-1",
                        }
                    ],
                },
            ],
        }
        operation = StructureProjectionOperationV1(
            operation=(
                StructureProjectionOperationKind.EXCLUDE_TABLE_OWNED_DIRECT_CONTENT
            ),
            target_scope_id="parent",
            related_refs=("child-table",),
        )

        projected = native_structure_bridge._apply_tree_projection_operations(
            tree,
            operations=(operation,),
        )

        self.assertEqual(projected["nodes"][0]["source_content"], [])

    def test_table_preview_returns_verified_cells_for_one_selected_scope(self) -> None:
        response = SimpleNamespace(headers={})

        result = native_structure_bridge.get_native_structure_table_preview(
            "project-1",
            "document-1",
            "lt-1",
            response,
        )

        self.assertEqual(result["schema_version"], "native-structure-table-preview/v1")
        self.assertEqual(
            result["summary"],
            {
                "fragment_count": 1,
                "row_count": 1,
                "cell_count": 2,
            },
        )
        self.assertEqual(result["fragments"][0]["cells"][1]["text"], "Value")
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    def test_feedback_is_scope_bound_persisted_and_idempotent(self) -> None:
        payload = self._feedback_payload()
        first = native_structure_bridge.submit_native_structure_feedback(
            "project-1",
            "document-1",
            payload,
            SimpleNamespace(headers={}),
        )
        second = native_structure_bridge.submit_native_structure_feedback(
            "project-1",
            "document-1",
            payload,
            SimpleNamespace(headers={}),
        )

        self.assertEqual(first["feedback"], second["feedback"])
        self.assertEqual(first["feedback"]["status"], "PENDING_CODEX_APP")
        feedback_path = self.feedback_root / _SOURCE_SHA256 / f"{_RUN_ID}.jsonl"
        records = [
            json.loads(line)
            for line in feedback_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["scope_id"], "structure-section-1")
        self.assertEqual(stat.S_IMODE(feedback_path.stat().st_mode), 0o600)

        public = native_structure_bridge.get_native_structure_reference(
            "project-1",
            "document-1",
            SimpleNamespace(headers={}),
        )
        self.assertEqual(public["feedbacks"], records)

    def test_feedback_rejects_stale_source_and_unknown_scope(self) -> None:
        stale = self._feedback_payload(
            expected_source_sha256="b" * 64,
        )
        with self.assertRaises(ApplicationError) as caught:
            native_structure_bridge.submit_native_structure_feedback(
                "project-1",
                "document-1",
                stale,
                SimpleNamespace(headers={}),
            )
        self.assertEqual(caught.exception.status_code, 409)

        unknown = self._feedback_payload(scope_id="structure-missing")
        with self.assertRaises(ApplicationError) as caught:
            native_structure_bridge.submit_native_structure_feedback(
                "project-1",
                "document-1",
                unknown,
                SimpleNamespace(headers={}),
            )
        self.assertEqual(caught.exception.status_code, 422)

    def test_proposal_decision_is_reversible_and_changes_public_projection(
        self,
    ) -> None:
        payload = NativeStructureProposalDecisionRequestV1.model_validate(
            {
                "schema_version": "native-structure-proposal-decision-request/v1",
                "command_id": "proposal-decision-command-1",
                "expected_source_sha256": _SOURCE_SHA256,
                "expected_artifact_id": _RUN_ID,
                "proposal_id": "proposal-image-1",
                "action": "UNDO",
            }
        )
        result = native_structure_bridge.submit_native_structure_proposal_decision(
            "project-1",
            "document-1",
            payload,
            SimpleNamespace(headers={}),
        )

        self.assertEqual(result["proposal_status"], "UNDONE")
        public = native_structure_bridge.get_native_structure_reference(
            "project-1",
            "document-1",
            SimpleNamespace(headers={}),
        )
        self.assertEqual(public["structure_proposals"][0]["status"], "UNDONE")
        root = public["document_tree"]["nodes"][0]
        self.assertFalse(
            any(anchor["anchor_kind"] == "IMAGE" for anchor in root["source_anchors"])
        )
        decision_path = (
            self.feedback_root / _SOURCE_SHA256 / f"{_RUN_ID}.proposal-decisions.jsonl"
        )
        self.assertEqual(stat.S_IMODE(decision_path.stat().st_mode), 0o600)

    def test_continue_queues_only_new_inputs_and_exposes_honest_status(self) -> None:
        native_structure_bridge.submit_native_structure_feedback(
            "project-1",
            "document-1",
            self._feedback_payload(),
            SimpleNamespace(headers={}),
        )
        decision_payload = NativeStructureProposalDecisionRequestV1.model_validate(
            {
                "schema_version": "native-structure-proposal-decision-request/v1",
                "command_id": "proposal-decision-command-continue",
                "expected_source_sha256": _SOURCE_SHA256,
                "expected_artifact_id": _RUN_ID,
                "proposal_id": "proposal-image-1",
                "action": "CONFIRM",
            }
        )
        native_structure_bridge.submit_native_structure_proposal_decision(
            "project-1",
            "document-1",
            decision_payload,
            SimpleNamespace(headers={}),
        )
        before = native_structure_bridge.get_native_structure_reference(
            "project-1",
            "document-1",
            SimpleNamespace(headers={}),
        )
        self.assertTrue(before["continuation"]["can_continue"])
        self.assertEqual(before["continuation"]["pending_feedback_count"], 1)
        self.assertEqual(
            before["continuation"]["pending_proposal_decision_count"],
            1,
        )

        payload = NativeStructureContinueRequestV1.model_validate(
            {
                "schema_version": "native-structure-continue-request/v1",
                "command_id": "continue-command-1",
                "expected_source_sha256": _SOURCE_SHA256,
                "expected_artifact_id": _RUN_ID,
            }
        )
        first = native_structure_bridge.continue_native_structure_reference(
            "project-1",
            "document-1",
            payload,
            SimpleNamespace(headers={}),
        )
        second = native_structure_bridge.continue_native_structure_reference(
            "project-1",
            "document-1",
            payload,
            SimpleNamespace(headers={}),
        )

        self.assertEqual(first["handoff"], second["handoff"])
        self.assertEqual(first["handoff"]["status"], "QUEUED_FOR_CODEX_APP")
        self.assertEqual(len(first["handoff"]["feedback_ids"]), 1)
        self.assertTrue(
            first["handoff"]["feedback_ids"][0].startswith("native-structure-feedback-")
        )
        self.assertEqual(first["handoff"]["decision_counts"]["CONFIRM"], 1)
        self.assertTrue(first["handoff"]["preserve_unaffected_nodes"])
        public = native_structure_bridge.get_native_structure_reference(
            "project-1",
            "document-1",
            SimpleNamespace(headers={}),
        )
        self.assertFalse(public["continuation"]["can_continue"])
        self.assertEqual(
            public["continuation"]["status"],
            "QUEUED_FOR_CODEX_APP",
        )
        handoff_path = (
            self.feedback_root / _SOURCE_SHA256 / f"{_RUN_ID}.agent-handoffs.jsonl"
        )
        self.assertEqual(stat.S_IMODE(handoff_path.stat().st_mode), 0o600)

        ack_payload = NativeStructureHandoffAckRequestV1.model_validate(
            {
                "schema_version": "native-structure-handoff-ack-request/v1",
                "command_id": "handoff-ack-command-1",
                "expected_source_sha256": _SOURCE_SHA256,
                "expected_artifact_id": _RUN_ID,
                "handoff_id": first["handoff"]["handoff_id"],
                "current_action": "正在核对受影响节点。",
            }
        )
        acknowledged = native_structure_bridge.acknowledge_native_structure_handoff(
            "project-1",
            "document-1",
            ack_payload,
            SimpleNamespace(headers={}),
        )
        replay = native_structure_bridge.acknowledge_native_structure_handoff(
            "project-1",
            "document-1",
            ack_payload,
            SimpleNamespace(headers={}),
        )
        self.assertEqual(acknowledged["event"], replay["event"])
        public = native_structure_bridge.get_native_structure_reference(
            "project-1",
            "document-1",
            SimpleNamespace(headers={}),
        )
        self.assertEqual(public["continuation"]["status"], "ACKNOWLEDGED")
        self.assertEqual(
            public["continuation"]["latest_handoff"]["current_action"],
            "正在核对受影响节点。",
        )
        self.assertEqual(public["feedbacks"][0]["status"], "ACKNOWLEDGED")
        event_path = (
            self.feedback_root
            / _SOURCE_SHA256
            / f"{_RUN_ID}.agent-handoff-events.jsonl"
        )
        self.assertEqual(stat.S_IMODE(event_path.stat().st_mode), 0o600)

        outcome = native_structure_bridge.record_native_structure_handoff_outcome(
            project_id="project-1",
            document_id="document-1",
            source_sha256=_SOURCE_SHA256,
            artifact_id=_RUN_ID,
            handoff_id=first["handoff"]["handoff_id"],
            execution_status="NO_CHANGE",
            reason_code="current_structure_selected",
            evidence_state="INSUFFICIENT",
            decision_policy="KEEP_CURRENT_STRUCTURE",
            public_summary="证据有限，Agent 已明确选择保留当前结构继续。",
        )
        replay_outcome = (
            native_structure_bridge.record_native_structure_handoff_outcome(
                project_id="project-1",
                document_id="document-1",
                source_sha256=_SOURCE_SHA256,
                artifact_id=_RUN_ID,
                handoff_id=first["handoff"]["handoff_id"],
                execution_status="NO_CHANGE",
                reason_code="current_structure_selected",
                evidence_state="INSUFFICIENT",
                decision_policy="KEEP_CURRENT_STRUCTURE",
                public_summary="证据有限，Agent 已明确选择保留当前结构继续。",
            )
        )
        self.assertEqual(outcome, replay_outcome)
        public = native_structure_bridge.get_native_structure_reference(
            "project-1",
            "document-1",
            SimpleNamespace(headers={}),
        )
        self.assertEqual(
            public["continuation"]["status"],
            "NO_CHANGE",
        )
        self.assertTrue(public["continuation"]["advancement_allowed"])
        self.assertEqual(
            public["continuation"]["default_resolution_policy"],
            "KEEP_CURRENT_STRUCTURE",
        )
        self.assertEqual(public["continuation"]["evidence_state"], "INSUFFICIENT")
        self.assertEqual(
            public["continuation"]["nonblocking_unresolved_count"],
            2,
        )
        self.assertEqual(
            public["continuation"]["latest_handoff"]["outcome_reason_code"],
            "current_structure_selected",
        )
        self.assertEqual(public["feedbacks"][0]["status"], "ACKNOWLEDGED")

    def test_agent_can_revise_tool_output_until_host_validation_passes(self) -> None:
        feedback = native_structure_bridge.submit_native_structure_feedback(
            "project-1",
            "document-1",
            self._feedback_payload(),
            SimpleNamespace(headers={}),
        )["feedback"]
        handoff = native_structure_bridge.continue_native_structure_reference(
            "project-1",
            "document-1",
            NativeStructureContinueRequestV1(
                command_id="continue-for-attempts",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
            ),
            SimpleNamespace(headers={}),
        )["handoff"]
        native_structure_bridge.acknowledge_native_structure_handoff(
            "project-1",
            "document-1",
            NativeStructureHandoffAckRequestV1(
                command_id="ack-for-attempts",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
                current_action="正在检查工具候选。",
            ),
            SimpleNamespace(headers={}),
        )
        tool_result = native_structure_bridge.invoke_native_structure_host_tool(
            "project-1",
            "document-1",
            NativeStructureHostToolRequestV1(
                command_id="restore-table-1",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
                scope_id="structure-section-1",
                intent=StructureToolIntent.RESTORE_TABLE_CANDIDATE,
                reason_signals=("cell_text_contains_following_heading",),
                logical_table_scope_id="lt-1",
            ),
            SimpleNamespace(headers={}),
        )
        tool_call = StructureToolCallReceiptV1.model_validate(
            tool_result["receipt"]["tool_call"]
        )
        self.assertEqual(tool_result["result"]["authority"], "ADVISORY_CANDIDATE")
        first_revision = self._candidate_revision(
            basis_role=StructureRevisionBasisRole.ADVISORY_CANDIDATE,
            basis_sha256=str(tool_call.result_sha256),
            revised_candidate_sha256="c" * 64,
            tool_call_ids=(tool_call.call_id,),
        )
        first = native_structure_bridge.submit_native_structure_handoff_attempt(
            "project-1",
            "document-1",
            NativeStructureHandoffAttemptRequestV1(
                command_id="handoff-attempt-1",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
                attempt_ordinal=1,
                affected_scope_ids=("structure-section-1",),
                tool_calls=(tool_call,),
                candidate_revisions=(first_revision,),
                materialized_candidate_sha256="c" * 64,
                rendered_output_sha256="d" * 64,
                materialization_observation={
                    "confirmed_actions": [
                        {
                            "action_id": "change-1",
                            "materialization_receipt_id": "",
                            "verification_receipt_id": "",
                            "rendered_output_reopened": False,
                        }
                    ]
                },
                work_item_dispositions=(
                    StructureWorkItemDispositionV1(
                        input_kind="FEEDBACK",
                        input_id=feedback["feedback_id"],
                        outcome="RESOLVED",
                        reason_code="candidate_replaced",
                    ),
                ),
                current_action="正在修正未通过复验的局部表格。",
                public_summary="工具候选已修改，但 Host 后置检查尚未通过。",
            ),
            SimpleNamespace(headers={}),
        )
        self.assertEqual(first["attempt"]["status"], "REVISION_REQUIRED")
        self.assertEqual(first["next_action"], "REVISE_AND_REMATERIALIZE")
        self.assertEqual(first["attempt"]["accepted_work_item_count"], 0)
        public = native_structure_bridge.get_native_structure_reference(
            "project-1",
            "document-1",
            SimpleNamespace(headers={}),
        )
        self.assertEqual(public["continuation"]["status"], "REVISION_REQUIRED")
        self.assertEqual(public["feedbacks"][0]["status"], "ACKNOWLEDGED")
        self.assertEqual(public["structure_projection"]["state"], "ACCEPTED")
        self.assertIsNone(public["structure_projection"]["attempt_id"])

        first_receipt_sha256 = native_structure_bridge._read_handoff_attempts(
            source_sha256=_SOURCE_SHA256,
            artifact_id=_RUN_ID,
        )[-1]["receipt_sha256"]
        second_revision = self._candidate_revision(
            basis_role=StructureRevisionBasisRole.PREVIOUS_MATERIALIZATION,
            basis_sha256="c" * 64,
            revised_candidate_sha256="e" * 64,
        )
        second = native_structure_bridge.submit_native_structure_handoff_attempt(
            "project-1",
            "document-1",
            NativeStructureHandoffAttemptRequestV1(
                command_id="handoff-attempt-2",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
                attempt_ordinal=2,
                previous_attempt_receipt_sha256=first_receipt_sha256,
                affected_scope_ids=("structure-section-1",),
                candidate_revisions=(second_revision,),
                materialized_candidate_sha256="e" * 64,
                rendered_output_sha256="f" * 64,
                materialization_observation={
                    "confirmed_actions": [
                        {
                            "action_id": "change-1",
                            "materialization_receipt_id": "materialized-2",
                            "verification_receipt_id": "verified-2",
                            "rendered_output_reopened": True,
                        }
                    ]
                },
                work_item_dispositions=(
                    StructureWorkItemDispositionV1(
                        input_kind="FEEDBACK",
                        input_id=feedback["feedback_id"],
                        outcome="RESOLVED",
                        reason_code="rendered_scope_verified",
                    ),
                ),
                current_action="局部表格已经重新物化并复验。",
                public_summary="Agent 修改了工具候选，重新渲染后通过 Host 检查。",
            ),
            SimpleNamespace(headers={}),
        )
        self.assertEqual(second["attempt"]["status"], "VERIFIED")
        self.assertEqual(second["next_action"], "COMPLETE")
        public = native_structure_bridge.get_native_structure_reference(
            "project-1",
            "document-1",
            SimpleNamespace(headers={}),
        )
        self.assertEqual(public["continuation"]["status"], "CONSUMED")
        self.assertEqual(public["feedbacks"][0]["status"], "CONSUMED")
        self.assertEqual(
            public["continuation"]["latest_handoff"]["attempt_summary"],
            {
                "attempt_count": 2,
                "tool_call_count": 1,
                "candidate_revision_count": 2,
                "tool_intents": ["RESTORE_TABLE_CANDIDATE"],
                "validation_status": "PASS",
            },
        )
        preview = native_structure_bridge.get_native_structure_table_preview(
            "project-1",
            "document-1",
            "lt-1",
            SimpleNamespace(headers={}),
        )
        self.assertEqual(
            preview["presentation"]["canonical_column_count"],
            2,
        )
        projection = preview["presentation"]["canonical_projection"]
        self.assertEqual(projection["status"], "EXACT")
        self.assertEqual(projection["canonical_column_count"], 2)
        self.assertEqual(len(projection["projection_sha256"]), 64)
        self.assertEqual(
            {
                item["cell_id"]: (item["column"], item["column_span"])
                for item in projection["fragments"][0]["cell_mappings"]
            },
            {
                "preview-cell-1": (0, 1),
                "preview-cell-2": (1, 1),
            },
        )
        attempt_path = (
            self.feedback_root
            / _SOURCE_SHA256
            / f"{_RUN_ID}.agent-handoff-attempts.jsonl"
        )
        self.assertEqual(stat.S_IMODE(attempt_path.stat().st_mode), 0o600)

    def test_attempt_rejects_an_agent_claim_without_a_host_tool_receipt(self) -> None:
        native_structure_bridge.submit_native_structure_feedback(
            "project-1",
            "document-1",
            self._feedback_payload(),
            SimpleNamespace(headers={}),
        )
        handoff = native_structure_bridge.continue_native_structure_reference(
            "project-1",
            "document-1",
            NativeStructureContinueRequestV1(
                command_id="continue-for-fabricated-tool",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
            ),
            SimpleNamespace(headers={}),
        )["handoff"]
        native_structure_bridge.acknowledge_native_structure_handoff(
            "project-1",
            "document-1",
            NativeStructureHandoffAckRequestV1(
                command_id="ack-for-fabricated-tool",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
                current_action="正在核对工具回执。",
            ),
            SimpleNamespace(headers={}),
        )
        fabricated = StructureToolCallReceiptV1(
            call_id="fabricated-tool-call",
            intent=StructureToolIntent.RESTORE_TABLE_CANDIDATE,
            reason_signals=("cell_text_contains_following_heading",),
            outcome="SUCCEEDED",
            result_role="ADVISORY_CANDIDATE",
            result_sha256="b" * 64,
        )

        with self.assertRaises(ApplicationError) as caught:
            native_structure_bridge.submit_native_structure_handoff_attempt(
                "project-1",
                "document-1",
                NativeStructureHandoffAttemptRequestV1(
                    command_id="fabricated-attempt",
                    expected_source_sha256=_SOURCE_SHA256,
                    expected_artifact_id=_RUN_ID,
                    handoff_id=handoff["handoff_id"],
                    attempt_ordinal=1,
                    affected_scope_ids=("structure-section-1",),
                    tool_calls=(fabricated,),
                    materialized_candidate_sha256="c" * 64,
                    rendered_output_sha256="d" * 64,
                    materialization_observation={},
                    current_action="正在核对工具回执。",
                    public_summary="不应接受未由 Host 签发的工具回执。",
                ),
                SimpleNamespace(headers={}),
            )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(
            caught.exception.detail["code"],
            "native_structure_handoff_attempt_tool_receipt_invalid",
        )

    def test_durable_attempt_drives_incremental_public_activity(self) -> None:
        repository = InMemoryAgentStructureLoopRepository()
        feedback = native_structure_bridge.submit_native_structure_feedback(
            "project-1",
            "document-1",
            self._feedback_payload(),
            SimpleNamespace(headers={}),
        )["feedback"]
        handoff = native_structure_bridge.continue_native_structure_reference(
            "project-1",
            "document-1",
            NativeStructureContinueRequestV1(
                command_id="continue-for-durable-attempt",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
            ),
            SimpleNamespace(headers={}),
        )["handoff"]
        native_structure_bridge.acknowledge_native_structure_handoff(
            "project-1",
            "document-1",
            NativeStructureHandoffAckRequestV1(
                command_id="ack-for-durable-attempt",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
                current_action="正在生成局部候选。",
            ),
            SimpleNamespace(headers={}),
        )
        artifact = native_structure_bridge._load_latest_reference(_SOURCE_SHA256)[
            "artifact"
        ]
        revision = self._candidate_revision(
            basis_role=StructureRevisionBasisRole.CURRENT_MATERIALIZATION,
            basis_sha256=native_structure_bridge._canonical_sha256(artifact),
            revised_candidate_sha256="7" * 64,
        )
        request = NativeStructureHandoffAttemptRequestV1(
            command_id="durable-handoff-attempt-1",
            expected_source_sha256=_SOURCE_SHA256,
            expected_artifact_id=_RUN_ID,
            handoff_id=handoff["handoff_id"],
            attempt_ordinal=1,
            affected_scope_ids=("structure-section-1",),
            candidate_revisions=(revision,),
            materialized_candidate_sha256="7" * 64,
            rendered_output_sha256="8" * 64,
            materialization_observation={
                "confirmed_actions": [
                    {
                        "action_id": "change-durable-1",
                        "materialization_receipt_id": "materialized-durable-1",
                        "verification_receipt_id": "",
                        "rendered_output_reopened": False,
                    }
                ]
            },
            work_item_dispositions=(
                StructureWorkItemDispositionV1(
                    input_kind="FEEDBACK",
                    input_id=feedback["feedback_id"],
                    outcome="RESOLVED",
                    reason_code="candidate_materialized",
                ),
            ),
            current_action="局部候选已生成，正在等待渲染复验。",
            public_summary="Agent 已提交局部修订，Host 正在检查渲染结果。",
        )
        with patch.object(
            native_structure_bridge,
            "_structure_loop_repository",
            return_value=repository,
        ):
            result = native_structure_bridge.submit_native_structure_handoff_attempt(
                "project-1",
                "document-1",
                request,
                SimpleNamespace(headers={}),
            )
            public = native_structure_bridge.get_native_structure_reference(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )
            stream = native_structure_bridge.get_native_structure_activity(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )
            incremental = native_structure_bridge.get_native_structure_activity(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
                after_sequence=1,
            )

        self.assertEqual(result["attempt"]["status"], "VERIFICATION_REQUIRED")
        self.assertEqual(public["continuation"]["status"], "VERIFYING")
        self.assertEqual(stream["cursor"], 2)
        self.assertEqual(
            [event["kind"] for event in stream["events"]],
            [
                "STRUCTURE_CANDIDATE_REVISION_PUBLISHED",
                "STRUCTURE_MATERIALIZATION_REOPEN_REQUIRED",
            ],
        )
        self.assertEqual(len(incremental["events"]), 1)
        self.assertNotIn(
            "feedback_items",
            stream["continuation"]["latest_handoff"],
        )
        self.assertNotIn(
            "proposal_decision_items",
            stream["continuation"]["latest_handoff"],
        )
        self.assertFalse(
            (
                self.feedback_root
                / _SOURCE_SHA256
                / f"{_RUN_ID}.agent-handoff-attempts.jsonl"
            ).exists()
        )

    def test_canonical_publisher_injects_handoff_identity_and_stops_at_verify(
        self,
    ) -> None:
        repository = InMemoryAgentStructureLoopRepository()
        feedback = native_structure_bridge.submit_native_structure_feedback(
            "project-1",
            "document-1",
            self._feedback_payload(),
            SimpleNamespace(headers={}),
        )["feedback"]
        handoff = native_structure_bridge.continue_native_structure_reference(
            "project-1",
            "document-1",
            NativeStructureContinueRequestV1(
                command_id="continue-for-canonical-publisher",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
            ),
            SimpleNamespace(headers={}),
        )["handoff"]
        native_structure_bridge.acknowledge_native_structure_handoff(
            "project-1",
            "document-1",
            NativeStructureHandoffAckRequestV1(
                command_id="ack-for-canonical-publisher",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
                current_action="Agent 正在核对局部证据。",
            ),
            SimpleNamespace(headers={}),
        )
        artifact = native_structure_bridge._load_latest_reference(_SOURCE_SHA256)[
            "artifact"
        ]
        revision = self._candidate_revision(
            basis_role=StructureRevisionBasisRole.CURRENT_MATERIALIZATION,
            basis_sha256=native_structure_bridge._canonical_sha256(artifact),
            revised_candidate_sha256="9" * 64,
        )
        publisher = NativeStructureRevisionPublisher(
            project_id="project-1",
            document_id="document-1",
            handoff_id=handoff["handoff_id"],
        )
        with self.assertRaisesRegex(
            CanonicalStructureRevisionPublishError,
            "dynamic_revision_work_items_incomplete",
        ):
            publisher.publish(
                CanonicalStructureRevisionSubmissionInputV1(
                    candidate_revisions=(
                        CanonicalStructureRevisionDraftV1.model_validate(
                            revision.model_dump(
                                mode="json",
                                exclude={"revision_id", "revised_candidate_sha256"},
                            )
                        ),
                    ),
                    evidence_state="SUFFICIENT",
                    current_action="不完整处置不应发布。",
                    public_summary="Agent 不能遗漏当前 handoff 的工作项。",
                )
            )
        with patch.object(
            native_structure_bridge,
            "_structure_loop_repository",
            return_value=repository,
        ):
            result = publisher.publish(
                CanonicalStructureRevisionSubmissionInputV1(
                    candidate_revisions=(
                        CanonicalStructureRevisionDraftV1.model_validate(
                            revision.model_dump(
                                mode="json",
                                exclude={"revision_id", "revised_candidate_sha256"},
                            )
                        ),
                    ),
                    work_item_dispositions=(
                        StructureWorkItemDispositionV1(
                            input_kind="FEEDBACK",
                            input_id=feedback["feedback_id"],
                            outcome="RESOLVED",
                            reason_code="candidate_materialized",
                        ),
                    ),
                    evidence_state="SUFFICIENT",
                    current_action="局部候选已物化，正在等待渲染复验。",
                    public_summary="Agent 已替换候选，但尚未把复验当作完成。",
                )
            )
            working_public = native_structure_bridge.get_native_structure_reference(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )
            verification = (
                native_structure_bridge.acknowledge_native_structure_rendered_reopen(
                    "project-1",
                    "document-1",
                    NativeStructureRenderedReopenRequestV1(
                        command_id="rendered-reopen-canonical-publisher",
                        expected_source_sha256=_SOURCE_SHA256,
                        expected_artifact_id=_RUN_ID,
                        handoff_id=handoff["handoff_id"],
                        attempt_id=str(result["attempt_id"]),
                        scope_id="structure-section-1",
                        expected_projection_sha256=working_public[
                            "structure_projection"
                        ]["projection_sha256"],
                        expected_rendered_output_sha256=(
                            repository.list_attempts(
                                project_id="project-1",
                                document_id="document-1",
                                source_sha256=_SOURCE_SHA256,
                                artifact_id=_RUN_ID,
                                handoff_id=handoff["handoff_id"],
                            )[-1].rendered_output_sha256
                        ),
                    ),
                    SimpleNamespace(headers={}),
                )
            )
            accepted_public = native_structure_bridge.get_native_structure_reference(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )

        self.assertEqual(result["status"], "VERIFICATION_REQUIRED")
        self.assertEqual(result["next_action"], "REOPEN_AND_VERIFY")
        self.assertEqual(
            working_public["structure_projection"],
            {
                "schema_version": "native-structure-projection-state/v1",
                "state": "WORKING_VERIFICATION_REQUIRED",
                "base_artifact_id": _RUN_ID,
                "attempt_id": result["attempt_id"],
                "attempt_ordinal": 1,
                "affected_scope_ids": ["structure-section-1"],
                "pending_rendered_scope_ids": ["structure-section-1"],
                "validation_status": "FAIL",
                "projection_sha256": native_structure_bridge._canonical_sha256(
                    working_public["document_tree"]
                ),
            },
        )
        attempts = repository.list_attempts(
            project_id="project-1",
            document_id="document-1",
            source_sha256=_SOURCE_SHA256,
            artifact_id=_RUN_ID,
            handoff_id=handoff["handoff_id"],
        )
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].source_sha256, _SOURCE_SHA256)
        self.assertEqual(attempts[0].artifact_id, _RUN_ID)
        self.assertEqual(attempts[0].handoff_id, handoff["handoff_id"])
        self.assertEqual(attempts[0].accepted_work_item_dispositions, ())
        self.assertEqual(verification["attempt"]["status"], "VERIFIED")
        self.assertEqual(len(attempts[1].accepted_work_item_dispositions), 1)
        self.assertEqual(
            accepted_public["structure_projection"],
            {
                "schema_version": "native-structure-projection-state/v1",
                "state": "ACCEPTED",
                "base_artifact_id": _RUN_ID,
                "attempt_id": verification["attempt"]["attempt_id"],
                "attempt_ordinal": 2,
                "affected_scope_ids": ["structure-section-1"],
                "pending_rendered_scope_ids": [],
                "validation_status": "PASS",
                "projection_sha256": native_structure_bridge._canonical_sha256(
                    accepted_public["document_tree"]
                ),
            },
        )

    def test_multi_scope_default_proposal_reopens_in_source_order_and_continues(
        self,
    ) -> None:
        repository = InMemoryAgentStructureLoopRepository()
        section_feedback = native_structure_bridge.submit_native_structure_feedback(
            "project-1",
            "document-1",
            self._feedback_payload(),
            SimpleNamespace(headers={}),
        )["feedback"]
        root_feedback = native_structure_bridge.submit_native_structure_feedback(
            "project-1",
            "document-1",
            self._feedback_payload(
                command_id="feedback-command-root",
                scope_id="structure-root",
                message="保留原文标题作为默认显示。",
            ),
            SimpleNamespace(headers={}),
        )["feedback"]
        handoff = native_structure_bridge.continue_native_structure_reference(
            "project-1",
            "document-1",
            NativeStructureContinueRequestV1(
                command_id="continue-for-multi-scope-default",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
            ),
            SimpleNamespace(headers={}),
        )["handoff"]
        native_structure_bridge.acknowledge_native_structure_handoff(
            "project-1",
            "document-1",
            NativeStructureHandoffAckRequestV1(
                command_id="ack-for-multi-scope-default",
                expected_source_sha256=_SOURCE_SHA256,
                expected_artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
                current_action="Agent 正在选择首个 Host-valid 可逆方案。",
            ),
            SimpleNamespace(headers={}),
        )
        artifact = native_structure_bridge._load_latest_reference(_SOURCE_SHA256)[
            "artifact"
        ]
        basis_sha256 = native_structure_bridge._canonical_sha256(artifact)
        section_draft = CanonicalStructureRevisionDraftV1(
            target_scope_id="structure-section-1",
            basis_role=StructureRevisionBasisRole.CURRENT_MATERIALIZATION,
            basis_sha256=basis_sha256,
            operations=(
                StructureProjectionOperationV1(
                    operation=(
                        StructureProjectionOperationKind.SET_CANONICAL_TABLE_COLUMNS
                    ),
                    target_scope_id="structure-section-1",
                    logical_table_scope_id="lt-1",
                    canonical_column_count=2,
                ),
            ),
            evidence_refs=("source-node-1",),
        )
        root_draft = CanonicalStructureRevisionDraftV1(
            target_scope_id="structure-root",
            basis_role=StructureRevisionBasisRole.CURRENT_MATERIALIZATION,
            basis_sha256=basis_sha256,
            operations=(
                StructureProjectionOperationV1(
                    operation=StructureProjectionOperationKind.PRESERVE_SOURCE_LABEL,
                    target_scope_id="structure-root",
                ),
            ),
            evidence_refs=("source-root",),
        )
        publisher = NativeStructureRevisionPublisher(
            project_id="project-1",
            document_id="document-1",
            handoff_id=handoff["handoff_id"],
        )
        with patch.object(
            native_structure_bridge,
            "_structure_loop_repository",
            return_value=repository,
        ):
            publication = publisher.publish(
                CanonicalStructureRevisionSubmissionInputV1(
                    candidate_revisions=(section_draft, root_draft),
                    work_item_dispositions=(
                        StructureWorkItemDispositionV1(
                            input_kind="FEEDBACK",
                            input_id=section_feedback["feedback_id"],
                            outcome="KEPT_UNRESOLVED",
                            reason_code="default_first_host_validated_proposal",
                        ),
                        StructureWorkItemDispositionV1(
                            input_kind="FEEDBACK",
                            input_id=root_feedback["feedback_id"],
                            outcome="RESOLVED",
                            reason_code="source_label_preserved",
                        ),
                    ),
                    evidence_state="CONFLICTING",
                    current_action="首选可逆方案已物化，正在按来源顺序复验。",
                    public_summary="已默认采用首个 Host-valid proposal；未决标记保留。",
                )
            )
            first_attempt = repository.list_attempts(
                project_id="project-1",
                document_id="document-1",
                source_sha256=_SOURCE_SHA256,
                artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
            )[-1]
            working_public = native_structure_bridge.get_native_structure_reference(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )
            first_reopen = (
                native_structure_bridge.acknowledge_native_structure_rendered_reopen(
                    "project-1",
                    "document-1",
                    NativeStructureRenderedReopenRequestV1(
                        command_id="rendered-reopen-multi-root",
                        expected_source_sha256=_SOURCE_SHA256,
                        expected_artifact_id=_RUN_ID,
                        handoff_id=handoff["handoff_id"],
                        attempt_id=first_attempt.attempt_id,
                        scope_id="structure-root",
                        expected_projection_sha256=working_public[
                            "structure_projection"
                        ]["projection_sha256"],
                        expected_rendered_output_sha256=(
                            first_attempt.rendered_output_sha256
                        ),
                    ),
                    SimpleNamespace(headers={}),
                )
            )
            second_attempt = repository.list_attempts(
                project_id="project-1",
                document_id="document-1",
                source_sha256=_SOURCE_SHA256,
                artifact_id=_RUN_ID,
                handoff_id=handoff["handoff_id"],
            )[-1]
            remaining_working_public = (
                native_structure_bridge.get_native_structure_reference(
                    "project-1",
                    "document-1",
                    SimpleNamespace(headers={}),
                )
            )
            second_reopen = (
                native_structure_bridge.acknowledge_native_structure_rendered_reopen(
                    "project-1",
                    "document-1",
                    NativeStructureRenderedReopenRequestV1(
                        command_id="rendered-reopen-multi-section",
                        expected_source_sha256=_SOURCE_SHA256,
                        expected_artifact_id=_RUN_ID,
                        handoff_id=handoff["handoff_id"],
                        attempt_id=second_attempt.attempt_id,
                        scope_id="structure-section-1",
                        expected_projection_sha256=remaining_working_public[
                            "structure_projection"
                        ]["projection_sha256"],
                        expected_rendered_output_sha256=(
                            second_attempt.rendered_output_sha256
                        ),
                    ),
                    SimpleNamespace(headers={}),
                )
            )
            public = native_structure_bridge.get_native_structure_reference(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )

        self.assertEqual(publication["status"], "VERIFICATION_REQUIRED")
        self.assertEqual(
            first_attempt.validation_report["rendered_scope_progress"],
            {
                "verified_scope_ids": [],
                "pending_scope_ids": ["structure-root", "structure-section-1"],
            },
        )
        self.assertEqual(first_reopen["attempt"]["status"], "VERIFICATION_REQUIRED")
        self.assertEqual(
            first_reopen["attempt"]["rendered_reopened_scope_ids"],
            ["structure-root"],
        )
        self.assertEqual(
            first_reopen["attempt"]["pending_rendered_scope_ids"],
            ["structure-section-1"],
        )
        self.assertEqual(second_reopen["attempt"]["status"], "VERIFIED")
        self.assertEqual(second_reopen["attempt"]["evidence_state"], "CONFLICTING")
        self.assertEqual(
            second_reopen["attempt"]["decision_policy"],
            "FIRST_HOST_VALIDATED_PROPOSAL",
        )
        self.assertEqual(second_reopen["next_action"], "COMPLETE")
        self.assertEqual(
            second_reopen["attempt"]["default_applied_work_item_count"],
            1,
        )
        self.assertEqual(public["continuation"]["status"], "CONSUMED")
        self.assertTrue(public["continuation"]["advancement_allowed"])
        self.assertEqual(
            public["continuation"]["default_resolution_policy"],
            "FIRST_HOST_VALIDATED_PROPOSAL",
        )
        self.assertEqual(public["continuation"]["nonblocking_unresolved_count"], 1)

    def test_product_loop_records_queue_ack_and_tool_activity(self) -> None:
        repository = InMemoryAgentStructureLoopRepository()
        with patch.object(
            native_structure_bridge,
            "_structure_loop_repository",
            return_value=repository,
        ):
            native_structure_bridge.submit_native_structure_feedback(
                "project-1",
                "document-1",
                self._feedback_payload(),
                SimpleNamespace(headers={}),
            )
            handoff = native_structure_bridge.continue_native_structure_reference(
                "project-1",
                "document-1",
                NativeStructureContinueRequestV1(
                    command_id="continue-for-product-activity",
                    expected_source_sha256=_SOURCE_SHA256,
                    expected_artifact_id=_RUN_ID,
                ),
                SimpleNamespace(headers={}),
            )["handoff"]
            native_structure_bridge.acknowledge_native_structure_handoff(
                "project-1",
                "document-1",
                NativeStructureHandoffAckRequestV1(
                    command_id="ack-for-product-activity",
                    expected_source_sha256=_SOURCE_SHA256,
                    expected_artifact_id=_RUN_ID,
                    handoff_id=handoff["handoff_id"],
                    current_action="正在检查局部表格证据。",
                ),
                SimpleNamespace(headers={}),
            )
            native_structure_bridge.invoke_native_structure_host_tool(
                "project-1",
                "document-1",
                NativeStructureHostToolRequestV1(
                    command_id="tool-for-product-activity",
                    expected_source_sha256=_SOURCE_SHA256,
                    expected_artifact_id=_RUN_ID,
                    handoff_id=handoff["handoff_id"],
                    scope_id="structure-section-1",
                    intent=StructureToolIntent.INSPECT_LOGICAL_TABLE,
                    logical_table_scope_id="lt-1",
                ),
                SimpleNamespace(headers={}),
            )
            stream = native_structure_bridge.get_native_structure_activity(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )

        self.assertEqual(
            [item["kind"] for item in stream["events"]],
            [
                "STRUCTURE_HANDOFF_QUEUED",
                "STRUCTURE_HANDOFF_ACKNOWLEDGED",
                "STRUCTURE_HOST_TOOL_COMPLETED",
            ],
        )
        self.assertEqual(stream["cursor"], 3)
        self.assertNotIn(
            "result_sha256",
            stream["events"][-1]["payload"]["authorized_parameters"],
        )

    def test_manifest_hash_drift_fails_closed(self) -> None:
        (self.run_root / "validation.json").write_text(
            '{"status":"tampered"}',
            encoding="utf-8",
        )
        with self.assertRaises(ApplicationError) as caught:
            native_structure_bridge.get_native_structure_reference(
                "project-1",
                "document-1",
                SimpleNamespace(headers={}),
            )
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(
            caught.exception.detail["code"],
            "native_structure_reference_invalid",
        )

    def _feedback_payload(self, **updates) -> NativeStructureFeedbackRequestV1:
        payload = {
            "schema_version": "native-structure-feedback-request/v1",
            "command_id": "feedback-command-1",
            "expected_source_sha256": _SOURCE_SHA256,
            "expected_artifact_id": _RUN_ID,
            "scope_kind": "STRUCTURE_NODE",
            "scope_id": "structure-section-1",
            "message": "请把标题保留在主表之外。",
            **updates,
        }
        return NativeStructureFeedbackRequestV1.model_validate(payload)

    def _projection_attempt_receipt(
        self,
        *,
        handoff_id: str,
        command_id: str,
        scope_id: str,
        created_at_utc: datetime,
        candidate_sha256: str,
        verification_required: bool,
        previous_attempt: dict[str, object] | None = None,
        operation: StructureProjectionOperationV1 | None = None,
    ) -> dict[str, object]:
        operation = operation or StructureProjectionOperationV1(
            operation=StructureProjectionOperationKind.PRESERVE_SOURCE_LABEL,
            target_scope_id=scope_id,
        )
        revision_values = {
            "target_scope_id": scope_id,
            "basis_role": StructureRevisionBasisRole.CURRENT_MATERIALIZATION,
            "basis_sha256": "1" * 64,
            "revised_candidate_sha256": candidate_sha256,
            "operations": (operation,),
            "evidence_refs": (f"source-{scope_id}",),
            "tool_call_ids": (),
        }
        revision = StructureAgentCandidateRevisionV1(
            revision_id=stable_structure_agent_revision_id(**revision_values),
            **revision_values,
        )
        request = NativeStructureHandoffAttemptRequestV1(
            command_id=command_id,
            expected_source_sha256=_SOURCE_SHA256,
            expected_artifact_id=_RUN_ID,
            handoff_id=handoff_id,
            attempt_ordinal=(
                int(previous_attempt["attempt_ordinal"]) + 1
                if previous_attempt is not None
                else 1
            ),
            previous_attempt_receipt_sha256=(
                str(previous_attempt["receipt_sha256"])
                if previous_attempt is not None
                else None
            ),
            affected_scope_ids=(scope_id,),
            candidate_revisions=(revision,),
            materialized_candidate_sha256=candidate_sha256,
            rendered_output_sha256="9" * 64,
            materialization_observation={
                "confirmed_actions": [
                    {
                        "action_id": command_id,
                        "materialization_receipt_id": "materialized",
                        "verification_receipt_id": (
                            "" if verification_required else "verified"
                        ),
                        "rendered_output_reopened": not verification_required,
                    }
                ]
            },
            current_action="正在复验结构投影。",
            public_summary="当前结构投影等待或已通过 Host 复验。",
        )
        pending_scope_ids = [scope_id] if verification_required else []
        verified_scope_ids = [] if verification_required else [scope_id]
        validation_report = {
            "status": "FAIL" if verification_required else "PASS",
            "findings": (
                [{"pattern_id": "confirmed_proposal_not_verified"}]
                if verification_required
                else []
            ),
            "rendered_scope_progress": {
                "verified_scope_ids": verified_scope_ids,
                "pending_scope_ids": pending_scope_ids,
            },
        }
        receipt = build_native_structure_handoff_attempt_receipt(
            request,
            validation_report=validation_report,
            selected_failure_patterns={"selected_patterns": []},
            kit_version="0.8.10",
            kit_bundle_sha256="8" * 64,
            previous_attempt=(
                NativeStructureHandoffAttemptReceiptV1.model_validate(
                    previous_attempt
                )
                if previous_attempt is not None
                else None
            ),
        ).model_dump(mode="json")
        receipt["created_at_utc"] = created_at_utc.isoformat()
        return receipt

    def _candidate_revision(
        self,
        *,
        basis_role: StructureRevisionBasisRole,
        basis_sha256: str,
        revised_candidate_sha256: str,
        tool_call_ids: tuple[str, ...] = (),
    ) -> StructureAgentCandidateRevisionV1:
        values = {
            "target_scope_id": "structure-section-1",
            "basis_role": basis_role,
            "basis_sha256": basis_sha256,
            "revised_candidate_sha256": revised_candidate_sha256,
            "operations": (
                StructureProjectionOperationV1(
                    operation=(
                        StructureProjectionOperationKind.SET_CANONICAL_TABLE_COLUMNS
                    ),
                    target_scope_id="structure-section-1",
                    logical_table_scope_id="lt-1",
                    canonical_column_count=2,
                ),
            ),
            "evidence_refs": ("source-node-1",),
            "tool_call_ids": tool_call_ids,
        }
        return StructureAgentCandidateRevisionV1(
            revision_id=stable_structure_agent_revision_id(**values),
            **values,
        )

    def _write_reference(self) -> None:
        artifact = {
            "schema_version": "reqpilotx-native-document-structure-reference/v1",
            "artifact_id": _RUN_ID,
            "status": "PARTIAL",
            "coverage_status": "COMPLETE",
            "boundary_status": "PARTIAL_WITH_EXPLICIT_UNRESOLVED",
            "source": {"sha256": _SOURCE_SHA256, "page_count": 1},
            "scope": {"workflow_step": "DOCUMENT_STRUCTURE_RECOVERY"},
            "summary": {
                "primary_document_units": 1,
                "top_level_logical_tables": 1,
            },
            "page_ownership": [
                {
                    "unit_id": "unit-1",
                    "title": "文档",
                    "pages": [1],
                }
            ],
            "main_document_sections": [],
            "logical_tables": [
                {
                    "scope_id": "lt-1",
                    "title": "表格",
                    "owner_unit_id": "unit-1",
                    "role": "TEST",
                    "fragments": [
                        {"table_token": "T0001", "page": 1, "row_range": [0, 1]}
                    ],
                }
            ],
            "physical_table_dispositions": [],
            "nested_physical_tables": [],
            "nested_visual_tables": [],
            "source_anomalies": [],
            "unresolved": [{"code": "uncertain-1", "scope": "P1", "detail": "待核对"}],
            "implementation_findings": [],
            "visual_receipts": [
                {
                    "page": 1,
                    "path": "renders/high/P0001.png",
                    "sha256": "c" * 64,
                    "state": "OPENED",
                    "purpose": "边界",
                }
            ],
        }
        document_tree = {
            "schema_version": "native-document-structure-tree/v1",
            "root_ids": ["structure-root"],
            "nodes": [
                {
                    "node_id": "structure-root",
                    "parent_id": None,
                    "order": 0,
                    "kind": "DOCUMENT_ROOT",
                    "label": "",
                    "title": "文档",
                    "title_authority": "VISIBLE_TITLE",
                    "pages": [1],
                    "content_blocks": [],
                    "finding_codes": [],
                },
                {
                    "node_id": "structure-section-1",
                    "parent_id": "structure-root",
                    "order": 0,
                    "kind": "SECTION",
                    "label": "1",
                    "title": "概述",
                    "title_authority": "VISIBLE_TITLE",
                    "pages": [1],
                    "content_blocks": [
                        {
                            "kind": "LOGICAL_TABLE",
                            "logical_table_scope_id": "lt-1",
                        }
                    ],
                    "finding_codes": ["uncertain-1"],
                },
            ],
        }
        files = {
            "native-structure-reference.json": artifact,
            "document-structure-tree.json": document_tree,
            "document-structure-evidence.json": {
                "schema_version": "native-structure-node-evidence/v1",
                "source_sha256": _SOURCE_SHA256,
                "tree_sha256": native_structure_bridge._canonical_sha256(document_tree),
                "nodes": [
                    {
                        "node_id": "structure-root",
                        "anchors": [
                            {
                                "anchor_id": "anchor-root",
                                "page": 1,
                                "bbox": [10, 10, 80, 24],
                                "source_bounds": [0, 0, 100, 100],
                                "anchor_kind": "TITLE",
                                "precision": "EXACT_NATIVE_SPAN",
                                "source_handle": "source-root",
                                "source_text_sha256": "d" * 64,
                            },
                            {
                                "anchor_id": "anchor-image-1",
                                "page": 1,
                                "bbox": [20, 50, 40, 70],
                                "source_bounds": [0, 0, 100, 100],
                                "anchor_kind": "IMAGE",
                                "precision": "EXACT_IMAGE",
                                "placement_id": "placement-1",
                                "asset_id": "asset-1",
                            },
                        ],
                    },
                    {
                        "node_id": "structure-section-1",
                        "anchors": [
                            {
                                "anchor_id": "anchor-section-1",
                                "page": 1,
                                "bbox": [10, 30, 90, 80],
                                "source_bounds": [0, 0, 100, 100],
                                "anchor_kind": "TABLE_REGION",
                                "precision": "EXACT_TABLE_REGION",
                                "logical_table_scope_id": "lt-1",
                                "table_token": "T0001",
                                "row_range": [0, 1],
                            }
                        ],
                        "source_content": [
                            {
                                "content_id": "content-section-1",
                                "page": 1,
                                "bbox": [10, 30, 90, 44],
                                "source_bounds": [0, 0, 100, 100],
                                "source_handles": ["source-section-1"],
                                "text": "1 概述",
                                "text_sha256": hashlib.sha256(
                                    "1 概述".encode("utf-8")
                                ).hexdigest(),
                                "precision": "EXACT_SOURCE_RANGE",
                                "is_title": True,
                            }
                        ],
                    },
                ],
                "proposals": [
                    {
                        "proposal_id": "proposal-image-1",
                        "finding_code": "visual_asset_parent_binding_missing",
                        "node_id": "structure-root",
                        "object_kind": "IMAGE_PLACEMENT",
                        "object_id": "placement-1",
                        "page": 1,
                        "bbox": [20, 50, 40, 70],
                        "source_bounds": [0, 0, 100, 100],
                        "observation": "图片唯一落在当前表格区域。",
                        "recommended_action": "ATTACH_IMAGE_TO_NODE",
                        "recommended_target_node_id": "structure-root",
                        "impact": "只改变证据归属。",
                        "confidence": "HIGH",
                        "default_policy": "AUTO_APPLY_REVERSIBLE",
                        "status": "AUTO_APPLIED_PENDING_REVIEW",
                        "overlap_ratio": 1.0,
                    }
                ],
                "table_previews": [
                    {
                        "fragment_id": "table-preview-1",
                        "logical_table_scope_id": "lt-1",
                        "table_token": "T0001",
                        "page": 1,
                        "row_range": [0, 1],
                        "row_count": 1,
                        "column_count": 2,
                        "bbox": [10, 30, 90, 80],
                        "source_bounds": [0, 0, 100, 100],
                        "cells": [
                            {
                                "cell_id": "preview-cell-1",
                                "row": 0,
                                "column": 0,
                                "row_span": 1,
                                "column_span": 1,
                                "text": "Header",
                                "text_sha256": hashlib.sha256(
                                    "Header".encode("utf-8")
                                ).hexdigest(),
                                "bbox": [10, 30, 40, 50],
                            },
                            {
                                "cell_id": "preview-cell-2",
                                "row": 0,
                                "column": 1,
                                "row_span": 1,
                                "column_span": 1,
                                "text": "Value",
                                "text_sha256": hashlib.sha256(
                                    "Value".encode("utf-8")
                                ).hexdigest(),
                                "bbox": [40, 30, 90, 50],
                            },
                        ],
                    }
                ],
            },
            "repeatability.json": {
                "status": "PASS_WITH_BOUNDARY_REFINEMENTS",
                "stable_semantics": {},
                "refinements_applied": [],
                "unresolved_not_settled_by_replay": ["uncertain-1"],
                "quality_claim": "NOT_GOLD",
            },
            "validation.json": {
                "artifact_id": _RUN_ID,
                "status": "PASS",
                "checks": {},
                "quality_claim": "NOT_GOLD",
            },
            "private-oracle-diagnostic.json": {
                "oracle_authority": "NOT_GOLD",
                "oracle_expected_region_count": 1,
                "native_region_disposition_count": 1,
                "count_is_correctness_signal": False,
                "native_boundary_corrections": [],
            },
        }
        manifest_files = []
        for name, value in files.items():
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            (self.run_root / name).write_bytes(payload)
            manifest_files.append(
                {"path": name, "sha256": hashlib.sha256(payload).hexdigest()}
            )
        activity = (
            json.dumps(
                {
                    "schema_version": "activity/v1",
                    "sequence": 1,
                    "summary": "完成",
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        (self.run_root / "activity.jsonl").write_bytes(activity)
        manifest_files.append(
            {
                "path": "activity.jsonl",
                "sha256": hashlib.sha256(activity).hexdigest(),
            }
        )
        manifest = {
            "schema_version": "reqpilotx-native-structure-run-manifest/v1",
            "run_id": _RUN_ID,
            "source_sha256": _SOURCE_SHA256,
            "workflow_step": "DOCUMENT_STRUCTURE_RECOVERY",
            "files": manifest_files,
        }
        (self.run_root / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
