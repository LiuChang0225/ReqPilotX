from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

from reqpilotx_server.adapters.agents.codex_app_server.canonical_dynamic_tools import (
    CanonicalStructureRevisionPublishError,
    CanonicalStructureRevisionSubmissionInputV1,
)
from reqpilotx_server.application.agent_workflow import native_structure_bridge
from reqpilotx_server.application.agent_workflow.structure_tool_loop import (
    NativeStructureHandoffAttemptReceiptV1,
    NativeStructureHandoffAttemptRequestV1,
    StructureAgentCandidateRevisionV1,
    StructureHandoffAttemptStatus,
    stable_structure_agent_revision_id,
)
from reqpilotx_server.application.errors import ApplicationError


class NativeStructureRevisionPublisher:
    """Host-bound App Server publisher for one acknowledged structure handoff."""

    def __init__(
        self,
        *,
        project_id: str,
        document_id: str,
        handoff_id: str,
        before_publish: Callable[[], object] | None = None,
    ) -> None:
        self.project_id = project_id
        self.document_id = document_id
        self.handoff_id = handoff_id
        self.before_publish = before_publish or (lambda: None)
        self.last_result: dict[str, object] | None = None

    def publish(
        self,
        submission: CanonicalStructureRevisionSubmissionInputV1,
    ) -> dict[str, object]:
        candidate_revisions = tuple(
            _compile_revision(item) for item in submission.candidate_revisions
        )
        document = native_structure_bridge._require_project_document(
            self.project_id,
            self.document_id,
        )
        source_sha256 = native_structure_bridge._document_source_sha256(document)
        bundle = native_structure_bridge._load_latest_reference(source_sha256)
        artifact_id = str(bundle["artifact"]["artifact_id"])
        handoff = next(
            (
                item
                for item in native_structure_bridge._read_handoffs(
                    source_sha256=source_sha256,
                    artifact_id=artifact_id,
                )
                if str(item.get("handoff_id") or "") == self.handoff_id
            ),
            None,
        )
        if handoff is None:
            raise CanonicalStructureRevisionPublishError(
                "dynamic_revision_handoff_not_found"
            )
        expected_work_items = {
            *(("FEEDBACK", str(item)) for item in handoff.get("feedback_ids", [])),
            *(
                ("PROPOSAL_DECISION", str(item))
                for item in handoff.get("proposal_decision_ids", [])
            ),
        }
        submitted_work_items = {
            (item.input_kind, item.input_id)
            for item in submission.work_item_dispositions
        }
        if submitted_work_items != expected_work_items:
            raise CanonicalStructureRevisionPublishError(
                "dynamic_revision_work_items_incomplete"
            )
        latest_events = native_structure_bridge._latest_handoff_events(
            native_structure_bridge._read_handoff_events(
                source_sha256=source_sha256,
                artifact_id=artifact_id,
            )
        )
        if latest_events.get(self.handoff_id, {}).get("status") != "ACKNOWLEDGED":
            raise CanonicalStructureRevisionPublishError(
                "dynamic_revision_handoff_not_acknowledged"
            )
        attempts = tuple(
            NativeStructureHandoffAttemptReceiptV1.model_validate(item)
            for item in native_structure_bridge._read_all_handoff_attempts(
                project_id=self.project_id,
                document_id=self.document_id,
                source_sha256=source_sha256,
                artifact_id=artifact_id,
            )
            if str(item.get("handoff_id") or "") == self.handoff_id
        )
        previous = attempts[-1] if attempts else None
        if (
            previous is not None
            and previous.status is StructureHandoffAttemptStatus.VERIFICATION_REQUIRED
        ):
            raise CanonicalStructureRevisionPublishError(
                "dynamic_revision_render_verification_pending"
            )
        affected_scope_ids = tuple(
            sorted({revision.target_scope_id for revision in candidate_revisions})
        )
        allowed_scopes = {
            str(item) for item in handoff.get("affected_node_ids", []) if str(item)
        }
        if not affected_scope_ids or not set(affected_scope_ids).issubset(
            allowed_scopes
        ):
            raise CanonicalStructureRevisionPublishError(
                "dynamic_revision_scope_outside_handoff"
            )
        materialized_candidate_sha256 = native_structure_bridge._canonical_sha256(
            {
                "base_artifact_sha256": native_structure_bridge._canonical_sha256(
                    bundle["artifact"]
                ),
                "previous_attempt_receipt_sha256": (
                    previous.receipt_sha256 if previous is not None else None
                ),
                "candidate_revisions": [
                    item.model_dump(mode="json") for item in candidate_revisions
                ],
            }
        )
        rendered_output_sha256 = native_structure_bridge._canonical_sha256(
            {
                "materialized_candidate_sha256": materialized_candidate_sha256,
                "operations": [
                    operation.model_dump(mode="json")
                    for revision in candidate_revisions
                    for operation in revision.operations
                ],
            }
        )
        claimed_tool_ids = {
            call_id
            for revision in candidate_revisions
            for call_id in revision.tool_call_ids
        }
        stored_tool_calls = {
            receipt.tool_call.call_id: receipt.tool_call
            for receipt in (
                native_structure_bridge.NativeStructureHostToolReceiptV1.model_validate(
                    item
                )
                for item in native_structure_bridge._read_host_tool_receipts(
                    source_sha256=source_sha256,
                    artifact_id=artifact_id,
                )
            )
            if receipt.handoff_id == self.handoff_id
        }
        if not claimed_tool_ids.issubset(stored_tool_calls):
            raise CanonicalStructureRevisionPublishError(
                "dynamic_revision_tool_receipt_missing"
            )
        command_id = (
            "canonical-structure-revision:"
            + native_structure_bridge._canonical_sha256(
                submission.model_dump(mode="json")
            )
        )
        request = NativeStructureHandoffAttemptRequestV1(
            command_id=command_id,
            expected_source_sha256=source_sha256,
            expected_artifact_id=artifact_id,
            handoff_id=self.handoff_id,
            attempt_ordinal=1 if previous is None else previous.attempt_ordinal + 1,
            previous_attempt_receipt_sha256=(
                previous.receipt_sha256 if previous is not None else None
            ),
            affected_scope_ids=affected_scope_ids,
            tool_calls=tuple(
                stored_tool_calls[call_id] for call_id in sorted(claimed_tool_ids)
            ),
            candidate_revisions=candidate_revisions,
            materialized_candidate_sha256=materialized_candidate_sha256,
            rendered_output_sha256=rendered_output_sha256,
            materialization_observation={
                "confirmed_actions": [
                    {
                        "action_id": revision.revision_id,
                        "materialization_receipt_id": (
                            "canonical-materialization-"
                            + materialized_candidate_sha256[:32]
                        ),
                        "verification_receipt_id": "",
                        "rendered_output_reopened": False,
                    }
                    for revision in candidate_revisions
                ]
            },
            work_item_dispositions=submission.work_item_dispositions,
            current_action=submission.current_action,
            public_summary=submission.public_summary,
        )
        self.before_publish()
        try:
            result = native_structure_bridge.submit_native_structure_handoff_attempt(
                self.project_id,
                self.document_id,
                request,
                SimpleNamespace(headers={}),
                decision_evidence_state=submission.evidence_state,
                decision_policy="FIRST_HOST_VALIDATED_PROPOSAL",
            )
        except ApplicationError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            code = str(detail.get("code") or "dynamic_revision_publish_rejected")
            raise CanonicalStructureRevisionPublishError(code) from exc
        attempt = result.get("attempt")
        if not isinstance(attempt, dict):
            raise CanonicalStructureRevisionPublishError(
                "dynamic_revision_publish_receipt_missing"
            )
        public_result: dict[str, object] = {
            "attempt_id": str(attempt.get("attempt_id") or ""),
            "status": str(attempt.get("status") or ""),
            "validation_status": str(attempt.get("validation_status") or ""),
            "current_action": str(attempt.get("current_action") or ""),
            "public_summary": str(attempt.get("public_summary") or ""),
            "evidence_state": submission.evidence_state,
            "decision_policy": "FIRST_HOST_VALIDATED_PROPOSAL",
            "next_action": str(result.get("next_action") or ""),
        }
        self.last_result = public_result
        return public_result


def _compile_revision(draft) -> StructureAgentCandidateRevisionV1:  # type: ignore[no-untyped-def]
    revised_candidate_sha256 = native_structure_bridge._canonical_sha256(
        draft.model_dump(mode="json")
    )
    values = {
        "target_scope_id": draft.target_scope_id,
        "basis_role": draft.basis_role,
        "basis_sha256": draft.basis_sha256,
        "revised_candidate_sha256": revised_candidate_sha256,
        "operations": draft.operations,
        "evidence_refs": draft.evidence_refs,
        "tool_call_ids": draft.tool_call_ids,
    }
    return StructureAgentCandidateRevisionV1(
        revision_id=stable_structure_agent_revision_id(**values),
        **values,
    )


__all__ = ["NativeStructureRevisionPublisher"]
