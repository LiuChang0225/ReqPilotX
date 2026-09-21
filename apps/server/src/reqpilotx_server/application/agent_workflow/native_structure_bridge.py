from __future__ import annotations

from reqpilotx_server.resource_paths import development_path

import copy
import hashlib
import json
import os
import re
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from reqpilotx_server.adapters.documents.pdf_text_reading_projection import project_pdf_source_contents
from reqpilotx_server.adapters.documents.pdf_visual_table_recovery import (
    PdfVisualTableRecoveryError,
    recover_visual_subcell_table_fragments,
)
from reqpilotx_server.application.agent_workflow.activity import (
    AgentActivityActor,
    AgentActivityFamily,
    AgentActivityVisibility,
)
from reqpilotx_server.application.agent_workflow.structure_loop_persistence import (
    AgentStructureLoopActivityDraftV1,
)
from reqpilotx_server.application.agent_workflow.structure_tool_loop import (
    NativeStructureHostToolReceiptV1,
    NativeStructureHostToolRequestV1,
    NativeStructureHandoffAttemptReceiptV1,
    NativeStructureHandoffAttemptRequestV1,
    StructureHandoffAttemptStatus,
    StructureProjectionOperationKind,
    StructureProjectionOperationV1,
    StructureToolIntent,
    StructureToolOutcome,
    StructureToolResultRole,
    active_structure_projection_attempt,
    build_native_structure_handoff_attempt_receipt,
    build_native_structure_host_tool_receipt,
    canonical_structure_projection_attempts_sha256,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.source_runtime import resolve_source_record_path
from reqpilotx_server.application.transport import ResponseSink


REFERENCE_PUBLIC_SCHEMA_VERSION = "native-structure-reference-public/v1"
FEEDBACK_REQUEST_SCHEMA_VERSION = "native-structure-feedback-request/v1"
FEEDBACK_SCHEMA_VERSION = "native-structure-feedback/v1"
PROPOSAL_DECISION_REQUEST_SCHEMA_VERSION = (
    "native-structure-proposal-decision-request/v1"
)
PROPOSAL_DECISION_SCHEMA_VERSION = "native-structure-proposal-decision/v1"
CONTINUE_REQUEST_SCHEMA_VERSION = "native-structure-continue-request/v1"
HANDOFF_SCHEMA_VERSION = "native-structure-agent-handoff/v1"
HANDOFF_ACK_REQUEST_SCHEMA_VERSION = "native-structure-handoff-ack-request/v1"
HANDOFF_EVENT_SCHEMA_VERSION = "native-structure-agent-handoff-event/v1"
RENDERED_REOPEN_REQUEST_SCHEMA_VERSION = "native-structure-rendered-reopen-request/v1"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_MAX_REFERENCE_FILE_BYTES = 8 * 1024 * 1024
_FEEDBACK_LOCK = threading.RLock()

_PROTOTYPE_DIR = development_path('prototype')
REFERENCE_ROOT = _PROTOTYPE_DIR / "runtime-data" / "agent-reference-workflows"
FEEDBACK_ROOT = _PROTOTYPE_DIR / "runtime-data" / "native-structure-feedback"


class NativeStructureFeedbackRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native-structure-feedback-request/v1"] = (
        FEEDBACK_REQUEST_SCHEMA_VERSION
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    scope_kind: Literal[
        "DOCUMENT",
        "DOCUMENT_UNIT",
        "LOGICAL_TABLE",
        "UNRESOLVED",
        "STRUCTURE_NODE",
    ]
    scope_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("scope_id", "message")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("feedback text must be non-empty plain text")
        return normalized


class NativeStructureProposalDecisionRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native-structure-proposal-decision-request/v1"] = (
        PROPOSAL_DECISION_REQUEST_SCHEMA_VERSION
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    proposal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    action: Literal["CONFIRM", "UNDO", "KEEP_UNRESOLVED"]


class NativeStructureContinueRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native-structure-continue-request/v1"] = (
        CONTINUE_REQUEST_SCHEMA_VERSION
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class NativeStructureHandoffAckRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native-structure-handoff-ack-request/v1"] = (
        HANDOFF_ACK_REQUEST_SCHEMA_VERSION
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    handoff_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    current_action: str = Field(min_length=1, max_length=500)

    @field_validator("current_action")
    @classmethod
    def normalize_action(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("current action must be non-empty plain text")
        return normalized


class NativeStructureRenderedReopenRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native-structure-rendered-reopen-request/v1"] = (
        RENDERED_REOPEN_REQUEST_SCHEMA_VERSION
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    handoff_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    attempt_id: str = Field(pattern=r"^native-structure-handoff-attempt-[0-9a-f]{32}$")
    scope_id: str = Field(min_length=1, max_length=200)
    expected_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_rendered_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def get_native_structure_reference(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact_id = str(bundle["artifact"]["artifact_id"])
    feedbacks = _read_feedbacks(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    proposal_decisions = _read_proposal_decisions(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoffs = _read_handoffs(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoff_events = _read_handoff_events(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoff_attempts = _read_all_handoff_attempts(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    return _serialize_public_reference(
        bundle,
        feedbacks=feedbacks,
        proposal_decisions=proposal_decisions,
        handoffs=handoffs,
        handoff_events=handoff_events,
        handoff_attempts=handoff_attempts,
        source_path=_resolved_document_source_path(document),
    )


def _load_current_reference(
    project_id: str,
    document_id: str,
    *,
    document: dict[str, Any],
    source_sha256: str,
) -> dict[str, Any]:
    repository = _structure_loop_repository()
    if repository is not None:
        head = repository.get_working_revision_head(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
        # A transport replay is not a new source-authoritative Document Map.
        if head is not None and head.validator_kind == "D2_HOST_COMPILER":
            return _build_bundle_from_working_revision(
                project_id=project_id,
                document_id=document_id,
                head=head,
                document=document,
                source_sha256=source_sha256,
                repository=repository,
            )
    from reqpilotx_server.application.agent_workflow.native_source_graph import load_native_source_graph, native_source_graph_bundle
    graph = load_native_source_graph(project_id, document_id, source_sha256)
    if graph is not None:
        return native_source_graph_bundle(graph, document=document)
    return _load_latest_reference(source_sha256)


def _build_bundle_from_working_revision(
    *,
    project_id: str,
    document_id: str,
    head: Any,
    document: dict[str, Any],
    source_sha256: str,
    repository: Any,
) -> dict[str, Any]:
    from reqpilotx_server.application.agent_workflow.native_structure_working_revision_bridge import (
        _runtime_object_store,
        load_native_structure_working_revision,
    )
    from reqpilotx_server.application.agent_workflow.native_structure_working_revision import (
        canonical_json_sha256,
        validate_native_structure_table_preview,
    )

    record = repository.get_working_revision_object(
        project_id=project_id,
        document_id=document_id,
        run_id=head.run_id,
        revision=head.revision,
    )
    if record is None:
        raise _conflict(
            "native_structure_working_revision_missing",
            "The current working Document Map revision is unavailable.",
        )
    try:
        object_store = _runtime_object_store()
    except Exception as exc:
        raise _conflict(
            "native_structure_working_revision_store_unavailable",
            "The current working Document Map cannot be read safely.",
        ) from exc
    try:
        working_revision = load_native_structure_working_revision(
            record=record,
            object_store=object_store,
        )
    except Exception as exc:
        raise _conflict(
            "native_structure_working_revision_invalid",
            "The current working Document Map failed integrity validation.",
        ) from exc

    table_previews = []
    for binding in working_revision.table_preview_bindings:
        try:
            with object_store.open(str(binding.object_key)) as stream:
                preview = json.loads(stream.read().decode("utf-8"))
                logical_table = next(
                    item
                    for item in working_revision.logical_tables
                    if item.get("scope_id") == binding.scope_id
                )
                validate_native_structure_table_preview(
                    logical_table=logical_table,
                    preview=preview,
                    source_sha256=source_sha256,
                    artifact_id=working_revision.base_artifact_id,
                )
                for frag in preview.get("fragments", []):
                    table_previews.append(frag)
        except Exception as exc:
            raise _conflict(
                "native_structure_working_table_preview_invalid",
                "A table in the current working Document Map failed integrity validation.",
            ) from exc

    tree = working_revision.document_tree
    evidence = {
        "schema_version": "native-structure-node-evidence/v1",
        "source_sha256": source_sha256,
        "tree_sha256": canonical_json_sha256(tree),
        "nodes": [
            {
                "node_id": node["node_id"],
                "anchors": node.get("source_anchors", []),
                "source_content": node.get("source_content", []),
            }
            for node in tree.get("nodes", [])
        ],
        "table_previews": table_previews,
    }

    artifact = {
        "schema_version": "reqpilotx-native-document-structure-reference/v1",
        "artifact_id": working_revision.base_artifact_id or working_revision.run_id,
        "provenance": "AGENT_NATIVE_DOCUMENT_MAP_BUILDER",
        "status": working_revision.state,
        "coverage_status": (
            "COMPLETE"
            if working_revision.state == "ACCEPTED"
            and not working_revision.unresolved
            else "PARTIAL"
        ),
        "boundary_status": (
            "COMPLETE"
            if working_revision.state == "ACCEPTED"
            and not working_revision.unresolved
            else "PARTIAL_WITH_EXPLICIT_UNRESOLVED"
        ),
        "source": {
            "display_name": document.get("file_name") or "document.pdf",
            "sha256": source_sha256,
            "page_count": working_revision.visual_summary.get("page_count"),
            "role": document.get("document_role") or "CUSTOMER_SOR",
        },
        "scope": {
            "workflow_step": "DOCUMENT_STRUCTURE_RECOVERY",
        },
        "summary": {
            "node_count": len(tree.get("nodes", [])),
            "logical_tables": len(working_revision.logical_tables),
            "opened_page_count": working_revision.visual_summary.get(
                "opened_page_count",
                0,
            ),
            "working_revision": working_revision.revision,
        },
        "logical_tables": list(working_revision.logical_tables),
        "page_ownership": list(working_revision.page_ownership),
        "unresolved": list(working_revision.unresolved),
        "visual_receipts": list(working_revision.visual_summary.get("receipts", [])),
    }

    return {
        "manifest": {
            "schema_version": "reqpilotx-native-structure-run-manifest/v1",
            "run_id": working_revision.run_id,
            "source_sha256": source_sha256,
            "workflow_step": "DOCUMENT_STRUCTURE_RECOVERY",
            "created_at_utc": working_revision.created_at_utc.isoformat(),
        },
        "artifact": artifact,
        "repeatability": {
            "schema_version": "reqpilotx-native-structure-repeatability-report/v1",
            "artifact_id": artifact["artifact_id"],
            "status": "NOT_RUN",
            "quality_claim": "No independent repeatability run has been performed.",
        },
        "validation": {
            "schema_version": "reqpilotx-native-structure-validation-receipt/v1",
            "artifact_id": artifact["artifact_id"],
            "status": "PASS",
            "receipt_sha256": working_revision.validator_receipt_sha256,
            "quality_claim": "D2_HOST_VERIFIED_PARTIAL_SCOPE",
        },
        "oracle_diagnostic": {
            "schema_version": "reqpilotx-native-structure-oracle-diagnostic/v1",
            "status": "NOT_RUN",
            "oracle_authority": "NOT_APPLICABLE",
            "count_is_correctness_signal": False,
        },
        "activity": [],
        "document_tree": tree,
        "document_tree_evidence": evidence,
        "proposal_overrides": None,
        "working_revision_head": working_revision.head().model_dump(mode="json"),
    }


def get_native_structure_activity(
    project_id: str,
    document_id: str,
    response: ResponseSink,
    after_sequence: int = 0,
) -> dict[str, Any]:
    if type(after_sequence) is not int or not 0 <= after_sequence <= 1_000_000:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_activity_cursor_invalid",
                "message": "The Agent activity cursor is invalid.",
            },
        )
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    structure_loop_repository = _structure_loop_repository()
    current_head = (
        structure_loop_repository.get_working_revision_head(
            project_id=project_id, document_id=document_id,
            source_sha256=source_sha256,
        )
        if structure_loop_repository is not None
        else None
    )
    compiled_head = (
        current_head
        if current_head is not None and current_head.validator_kind == "D2_HOST_COMPILER"
        else None
    )
    # Initial growth polling needs only the durable head, not all table cells.
    bundle = (
        {"artifact": {"artifact_id": compiled_head.base_artifact_id}}
        if compiled_head is not None
        else _load_latest_reference(source_sha256)
    )
    artifact_id = str(bundle["artifact"]["artifact_id"])
    feedbacks = _read_feedbacks(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    proposal_decisions = _read_proposal_decisions(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoffs = _read_handoffs(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoff_events = _read_handoff_events(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    if compiled_head is not None and (handoffs or proposal_decisions):
        bundle = _load_current_reference(
            project_id, document_id, document=document, source_sha256=source_sha256,
        )
    handoff_attempts = _read_all_handoff_attempts(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    adopted_projection_attempts = _adopted_projection_attempts(handoff_attempts)
    active_handoff_id = _active_projection_handoff_id(
        handoffs=handoffs,
        adopted_attempts=adopted_projection_attempts,
    )
    events = _list_structure_loop_activity(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff_id=active_handoff_id or None,
        after_sequence=after_sequence,
    )
    continuation = _public_activity_continuation(
        _public_continuation(
            feedbacks=feedbacks,
            proposal_decisions=proposal_decisions,
            handoffs=handoffs,
            handoff_events=handoff_events,
            handoff_attempts=handoff_attempts,
            proposals=_all_current_proposals(bundle),
        )
    )
    working_revision_head = compiled_head
    if compiled_head is None and structure_loop_repository is not None:
        current_public_projection = _serialize_public_reference(
            bundle,
            feedbacks=feedbacks,
            proposal_decisions=proposal_decisions,
            handoffs=handoffs,
            handoff_events=handoff_events,
            handoff_attempts=handoff_attempts,
            source_path=_resolved_document_source_path(document),
        )["structure_projection"]
        working_revision_head = structure_loop_repository.get_working_revision_head(
            project_id=project_id, document_id=document_id,
            source_sha256=source_sha256, base_artifact_id=artifact_id,
            base_projection_sha256=current_public_projection["projection_sha256"],
        )
    return {
        "schema_version": "native-structure-activity-stream/v1",
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "handoff_id": active_handoff_id or None,
        "events": [item.model_dump(mode="json") for item in events],
        "cursor": max(
            (item.sequence for item in events),
            default=after_sequence,
        ),
        "continuation": continuation,
        "working_revision_head": (
            None
            if working_revision_head is None
            else working_revision_head.model_dump(mode="json")
        ),
    }


def get_native_structure_handoff_context(
    project_id: str,
    document_id: str,
    handoff_id: str,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    source_sha256 = _document_source_sha256(document)
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact_id = str(bundle["artifact"]["artifact_id"])
    handoffs = _read_handoffs(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoff = next(
        (item for item in handoffs if str(item.get("handoff_id") or "") == handoff_id),
        None,
    )
    latest_events = _latest_handoff_events(
        _read_handoff_events(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        )
    )
    if (
        handoff is None
        or latest_events.get(handoff_id, {}).get("status") != "ACKNOWLEDGED"
    ):
        raise _conflict(
            "native_structure_handoff_not_acknowledged",
            "The Host-bound structure handoff is not acknowledged.",
        )
    attempts = _read_all_handoff_attempts(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    public = _serialize_public_reference(
        bundle,
        feedbacks=_read_feedbacks(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        proposal_decisions=_read_proposal_decisions(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        handoffs=handoffs,
        handoff_events=_read_handoff_events(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        handoff_attempts=attempts,
        source_path=_resolved_document_source_path(document),
    )
    affected_ids = {
        str(item) for item in handoff.get("affected_node_ids", []) if str(item)
    }
    nodes = [
        item
        for item in public["document_tree"]["nodes"]
        if str(item.get("node_id") or "") in affected_ids
    ]
    table_ids = {
        str(block.get("logical_table_scope_id") or "")
        for node in nodes
        for block in node.get("content_blocks", [])
        if str(block.get("logical_table_scope_id") or "")
    }
    relevant_attempts = [
        NativeStructureHandoffAttemptReceiptV1.model_validate(item)
        for item in attempts
        if str(item.get("handoff_id") or "") == handoff_id
    ]
    return {
        "schema_version": "native-structure-handoff-context/v1",
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "handoff_id": handoff_id,
        "input_fingerprint": str(handoff.get("input_fingerprint") or ""),
        "current_materialization_sha256": _canonical_sha256(bundle["artifact"]),
        "affected_scope_ids": sorted(affected_ids),
        "feedback_items": list(handoff.get("feedback_items", [])),
        "proposal_decision_items": list(handoff.get("proposal_decision_items", [])),
        "nodes": nodes,
        "logical_tables": [
            item
            for item in public.get("logical_tables", [])
            if str(item.get("scope_id") or "") in table_ids
        ],
        "latest_attempt": (
            _public_handoff_attempt(relevant_attempts[-1])
            if relevant_attempts
            else None
        ),
        "allowed_projection_operations": [
            item.value for item in StructureProjectionOperationKind
        ],
        "instruction": (
            "Source evidence is immutable. Inspect only when the current evidence "
            "cannot decide the requested change. Publish one complete replacement "
            "candidate; publication remains verification-required."
        ),
    }


def _public_activity_continuation(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    handoff = value.get("latest_handoff")
    if not isinstance(handoff, dict):
        return result
    allowed = {
        "schema_version",
        "handoff_id",
        "project_id",
        "document_id",
        "source_sha256",
        "artifact_id",
        "command_id",
        "input_fingerprint",
        "status",
        "feedback_ids",
        "proposal_decision_ids",
        "affected_node_ids",
        "decision_counts",
        "requires_scoped_structure_revision",
        "requires_scoped_evidence_recovery",
        "preserve_unaffected_nodes",
        "current_action",
        "outcome_reason_code",
        "evidence_state",
        "decision_policy",
        "default_resolution_policy",
        "nonblocking_unresolved_count",
        "acknowledged_at_utc",
        "latest_attempt",
        "attempt_summary",
        "created_at_utc",
    }
    result["latest_handoff"] = {
        key: nested for key, nested in handoff.items() if key in allowed
    }
    return result


def get_native_structure_table_preview(
    project_id: str,
    document_id: str,
    scope_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    projection_operations = _verified_projection_operations(
        _read_all_handoff_attempts(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=str(bundle["artifact"]["artifact_id"]),
        )
    )
    return materialize_native_structure_table_preview(
        bundle=bundle,
        source_sha256=source_sha256,
        scope_id=scope_id,
        projection_operations=projection_operations,
        source_path=_resolved_document_source_path(document),
    )


def materialize_native_structure_table_preview(
    *,
    bundle: dict[str, Any],
    source_sha256: str,
    scope_id: str,
    projection_operations: tuple[StructureProjectionOperationV1, ...],
    source_path: Path | None = None,
) -> dict[str, Any]:
    if bundle.get("portable_source_graph") is not None:
        if projection_operations:
            raise ValueError("Portable source tables require a source-bound Agent revision, not legacy projection operations")
        from reqpilotx_server.application.agent_workflow.native_source_graph import native_source_graph_table_preview
        return native_source_graph_table_preview(bundle["portable_source_graph"], scope_id)
    artifact = bundle["artifact"]
    logical_table = next(
        (
            item
            for item in artifact.get("logical_tables", [])
            if str(item.get("scope_id") or "") == scope_id
        ),
        None,
    )
    if logical_table is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "native_structure_table_not_found",
                "message": "The selected native structure table is not current.",
            },
        )
    fragments = _public_table_preview_fragments(
        bundle.get("document_tree_evidence"),
        scope_id=scope_id,
        artifact=artifact,
    )
    fragments, evidence_recovery = _recover_visual_table_fragments(
        logical_table=logical_table,
        fragments=fragments,
        source_path=source_path,
        source_sha256=source_sha256,
        scope_id=scope_id,
    )
    presentation = _table_presentation(
        bundle,
        scope_id=scope_id,
        operations=projection_operations,
        fragments=fragments,
        recovered_visual_cells=bool(
            evidence_recovery
            and evidence_recovery.get("status") == "RECOVERED"
        ),
    )
    result = {
        "schema_version": "native-structure-table-preview/v1",
        "artifact_id": str(artifact.get("artifact_id") or ""),
        "source_sha256": source_sha256,
        "logical_table_scope_id": scope_id,
        "title": str(logical_table.get("title") or ""),
        "fragments": fragments,
        "presentation": presentation,
        "summary": {
            "fragment_count": len(fragments),
            "row_count": sum(int(item["row_count"]) for item in fragments),
            "cell_count": sum(len(item["cells"]) for item in fragments),
        },
    }
    if evidence_recovery is not None:
        result["evidence_recovery"] = evidence_recovery
    return result


def _recover_visual_table_fragments(
    *,
    logical_table: dict[str, Any],
    fragments: list[dict[str, Any]],
    source_path: Path | None,
    source_sha256: str,
    scope_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    visual_specs = tuple(
        item
        for item in logical_table.get("fragments", [])
        if isinstance(item, dict)
        and str(item.get("representation") or "") == "VISUAL_SUBCELL_TABLE"
    )
    if not visual_specs:
        return fragments, None
    if source_path is None:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "native_structure_table_evidence_recovery_required",
                "message": (
                    "The selected table needs local PDF evidence recovery before "
                    "its cells can be shown or processed."
                ),
                "logical_table_scope_id": scope_id,
            },
        )
    visual_keys = tuple(
        (str(item.get("table_token") or ""), int(item.get("page") or 0))
        for item in visual_specs
    )
    if (
        len(set(visual_keys)) != len(visual_keys)
        or any(not token or page < 1 for token, page in visual_keys)
        or not set(visual_keys).issubset(
            {
                (str(item.get("table_token") or ""), int(item.get("page") or 0))
                for item in fragments
            }
        )
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "native_structure_table_evidence_recovery_ambiguous",
                "message": (
                    "The selected visual table does not have one unambiguous "
                    "physical fragment to recover."
                ),
                "logical_table_scope_id": scope_id,
            },
        )
    try:
        recovered, receipt = recover_visual_subcell_table_fragments(
            source_path,
            expected_source_sha256=source_sha256,
            logical_table_scope_id=scope_id,
            fragment_specs=visual_specs,
        )
    except (OSError, PdfVisualTableRecoveryError, ValueError) as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "native_structure_table_evidence_recovery_failed",
                "message": (
                    "The selected visual table could not be recovered from "
                    "native PDF words and vector lines without guessing."
                ),
                "logical_table_scope_id": scope_id,
                "reason_code": str(exc),
            },
        ) from exc
    visual_key_set = set(visual_keys)
    result = [
        item
        for item in fragments
        if (
            str(item.get("table_token") or ""),
            int(item.get("page") or 0),
        )
        not in visual_key_set
    ]
    result.extend(recovered)
    result.sort(key=lambda item: (item["page"], item["row_range"][0]))
    return result, receipt


def _resolved_document_source_path(document: Any) -> Path | None:
    try:
        return resolve_source_record_path(document).resolve(strict=True)
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RuntimeError):
        return None


def submit_native_structure_feedback(
    project_id: str,
    document_id: str,
    payload: NativeStructureFeedbackRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    if payload.expected_source_sha256 != source_sha256:
        raise _conflict(
            "native_structure_source_changed",
            "The document source changed before the feedback was saved.",
        )
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact = bundle["artifact"]
    artifact_id = str(artifact["artifact_id"])
    if payload.expected_artifact_id != artifact_id:
        raise _conflict(
            "native_structure_reference_changed",
            "The native structure candidate changed before the feedback was saved.",
        )
    _require_feedback_scope(
        artifact,
        payload.scope_kind,
        payload.scope_id,
        document_tree=bundle.get("document_tree"),
    )
    record = _append_feedback(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        payload=payload,
    )
    return {
        "schema_version": "native-structure-feedback-result/v1",
        "feedback": record,
        "delivery_message": (
            "指引已保存到本地开发反馈队列；Codex App Agent 尚未读取。"
        ),
    }


def submit_native_structure_proposal_decision(
    project_id: str,
    document_id: str,
    payload: NativeStructureProposalDecisionRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    if payload.expected_source_sha256 != source_sha256:
        raise _conflict(
            "native_structure_source_changed",
            "The document source changed before the proposal decision was saved.",
        )
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact_id = str(bundle["artifact"]["artifact_id"])
    if payload.expected_artifact_id != artifact_id:
        raise _conflict(
            "native_structure_reference_changed",
            "The native structure candidate changed before the proposal decision was saved.",
        )
    proposal = _require_current_proposal(
        bundle.get("document_tree_evidence"),
        payload.proposal_id,
        overrides=bundle.get("proposal_overrides"),
    )
    decision = _append_proposal_decision(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        proposal=proposal,
        payload=payload,
    )
    return {
        "schema_version": "native-structure-proposal-decision-result/v1",
        "decision": decision,
        "proposal_status": _proposal_status(proposal, decision),
    }


def continue_native_structure_reference(
    project_id: str,
    document_id: str,
    payload: NativeStructureContinueRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    if payload.expected_source_sha256 != source_sha256:
        raise _conflict(
            "native_structure_source_changed",
            "The document source changed before the Agent handoff was queued.",
        )
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact_id = str(bundle["artifact"]["artifact_id"])
    if payload.expected_artifact_id != artifact_id:
        raise _conflict(
            "native_structure_reference_changed",
            "The native structure candidate changed before the Agent handoff was queued.",
        )
    feedbacks = _read_feedbacks(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    proposal_decisions = _read_proposal_decisions(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoffs = _read_handoffs(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    same_command = [
        item for item in handoffs if item.get("command_id") == payload.command_id
    ]
    if same_command:
        handoff = same_command[-1]
    else:
        handed_feedback_ids = {
            str(item)
            for handoff in handoffs
            for item in handoff.get("feedback_ids", [])
            if str(item)
        }
        handed_decision_ids = {
            str(item)
            for handoff in handoffs
            for item in handoff.get("proposal_decision_ids", [])
            if str(item)
        }
        pending_feedbacks = [
            item
            for item in feedbacks
            if str(item.get("feedback_id") or "") not in handed_feedback_ids
        ]
        pending_decisions = [
            item
            for item in proposal_decisions
            if str(item.get("decision_id") or "") not in handed_decision_ids
        ]
        if not pending_feedbacks and not pending_decisions:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "native_structure_nothing_to_continue",
                    "message": "There is no new structure feedback to send to the Agent.",
                },
            )
        proposals = _all_current_proposals(bundle)
        handoff = _append_handoff(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=artifact_id,
            payload=payload,
            feedbacks=pending_feedbacks,
            proposal_decisions=pending_decisions,
            proposals=proposals,
        )
    _append_structure_loop_activity(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff_id=str(handoff["handoff_id"]),
        command_id=f"activity:{handoff['handoff_id']}:queued",
        draft=AgentStructureLoopActivityDraftV1(
            actor=AgentActivityActor.HOST,
            family=AgentActivityFamily.PLAN,
            kind="STRUCTURE_HANDOFF_QUEUED",
            title="反馈已进入 Agent 队列",
            summary="Host 已锁定本轮来源、受影响范围和待处理输入。",
            scope_kind="HANDOFF",
            scope_ref_id=str(handoff["handoff_id"]),
            status="QUEUED_FOR_CODEX_APP",
            authorized_parameters={
                "affected_scope_count": len(handoff.get("affected_node_ids", [])),
                "feedback_count": len(handoff.get("feedback_ids", [])),
                "proposal_decision_count": len(
                    handoff.get("proposal_decision_ids", [])
                ),
            },
            artifact_refs=(artifact_id,),
            correlation_id=str(handoff["handoff_id"]),
            occurred_at_utc=_activity_time(handoff.get("created_at_utc")),
        ),
    )
    from reqpilotx_server.application.agent_workflow.native_structure_handoff_jobs import (
        enqueue_native_structure_handoff_job,
    )

    job = enqueue_native_structure_handoff_job(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff=handoff,
    )
    return {
        "schema_version": "native-structure-continue-result/v1",
        "handoff": handoff,
        "job": job,
        "delivery_message": ("本轮反馈已进入 Codex App 协作队列；Agent 尚未确认读取。"),
    }


def acknowledge_native_structure_handoff(
    project_id: str,
    document_id: str,
    payload: NativeStructureHandoffAckRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    if payload.expected_source_sha256 != source_sha256:
        raise _conflict(
            "native_structure_source_changed",
            "The document source changed before the Agent acknowledgement.",
        )
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact_id = str(bundle["artifact"]["artifact_id"])
    if payload.expected_artifact_id != artifact_id:
        raise _conflict(
            "native_structure_reference_changed",
            "The native structure candidate changed before the Agent acknowledgement.",
        )
    handoffs = _read_handoffs(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoff = next(
        (
            item
            for item in handoffs
            if str(item.get("handoff_id") or "") == payload.handoff_id
        ),
        None,
    )
    if handoff is None:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_handoff_invalid",
                "message": "The selected Agent handoff is not current.",
            },
        )
    event = _append_handoff_event(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff=handoff,
        payload=payload,
    )
    _append_structure_loop_activity(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff_id=payload.handoff_id,
        command_id=f"activity:{event['event_id']}:acknowledged",
        draft=AgentStructureLoopActivityDraftV1(
            actor=AgentActivityActor.AGENT,
            family=AgentActivityFamily.ACTIVITY,
            kind="STRUCTURE_HANDOFF_ACKNOWLEDGED",
            title="Agent 已读取本轮输入",
            summary=str(event.get("current_action") or payload.current_action),
            scope_kind="HANDOFF",
            scope_ref_id=payload.handoff_id,
            status="ACKNOWLEDGED",
            authorized_parameters={
                "affected_scope_count": len(event.get("affected_node_ids", [])),
                "feedback_count": len(event.get("feedback_ids", [])),
            },
            artifact_refs=(artifact_id,),
            correlation_id=payload.handoff_id,
            occurred_at_utc=_activity_time(event.get("created_at_utc")),
        ),
    )
    return {
        "schema_version": "native-structure-handoff-ack-result/v1",
        "handoff_id": payload.handoff_id,
        "event": event,
        "delivery_message": "Codex App Agent 已读取本轮反馈，正在核对受影响内容。",
    }


def record_native_structure_handoff_outcome(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    handoff_id: str,
    execution_status: Literal["NO_CHANGE"],
    reason_code: str,
    evidence_state: Literal["SUFFICIENT", "CONFLICTING", "INSUFFICIENT"],
    decision_policy: Literal["KEEP_CURRENT_STRUCTURE"],
    public_summary: str,
) -> dict[str, Any]:
    """Persist a truthful terminal outcome when the Agent publishes no revision."""

    document = _require_project_document(project_id, document_id)
    if _document_source_sha256(document) != source_sha256:
        raise _conflict(
            "native_structure_source_changed",
            "The document source changed before the Agent outcome was recorded.",
        )
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    if str(bundle["artifact"].get("artifact_id") or "") != artifact_id:
        raise _conflict(
            "native_structure_reference_changed",
            "The native structure candidate changed before the Agent outcome was recorded.",
        )
    handoff = next(
        (
            item
            for item in _read_handoffs(
                source_sha256=source_sha256,
                artifact_id=artifact_id,
            )
            if str(item.get("handoff_id") or "") == handoff_id
        ),
        None,
    )
    if handoff is None:
        raise _conflict(
            "native_structure_handoff_invalid",
            "The selected Agent handoff is not current.",
        )
    latest_event = _latest_handoff_events(
        _read_handoff_events(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        )
    ).get(handoff_id)
    if latest_event is None or str(latest_event.get("status") or "") not in {
        "ACKNOWLEDGED",
        "NO_CHANGE",
        "NEEDS_GUIDANCE",
        "UNRESOLVED_CONTINUED",
    }:
        raise _conflict(
            "native_structure_handoff_not_acknowledged",
            "The Agent handoff must be acknowledged before recording its outcome.",
        )
    normalized_reason = reason_code.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,159}", normalized_reason):
        raise ValueError("Agent handoff outcome reason code is invalid")
    normalized_summary = public_summary.strip()
    if (
        not normalized_summary
        or len(normalized_summary) > 1000
        or "\x00" in normalized_summary
    ):
        raise ValueError("Agent handoff public summary is invalid")
    if str(latest_event.get("status") or "") in {
        "NO_CHANGE",
        "NEEDS_GUIDANCE",
        "UNRESOLVED_CONTINUED",
    } and (
        str(latest_event.get("execution_status") or "") != execution_status
        or str(latest_event.get("reason_code") or "") != normalized_reason
        or str(latest_event.get("evidence_state") or "") != evidence_state
        or str(latest_event.get("decision_policy") or "") != decision_policy
        or str(latest_event.get("current_action") or "") != normalized_summary
    ):
        raise _conflict(
            "native_structure_handoff_outcome_conflict",
            "The Agent outcome changed after it was recorded.",
        )
    event = _append_handoff_outcome_event(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff=handoff,
        execution_status=execution_status,
        reason_code=normalized_reason,
        evidence_state=evidence_state,
        decision_policy=decision_policy,
        public_summary=normalized_summary,
    )
    visible_status = str(event["status"])
    _append_structure_loop_activity(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff_id=handoff_id,
        command_id=f"activity:{event['event_id']}:outcome",
        draft=AgentStructureLoopActivityDraftV1(
            actor=AgentActivityActor.AGENT,
            family=AgentActivityFamily.DECISION,
            kind="STRUCTURE_HANDOFF_NO_CHANGE",
            title=(
                "Agent 判断当前无需修改"
                if evidence_state == "SUFFICIENT"
                else "Agent 已选择保留当前结构"
            ),
            summary=normalized_summary,
            scope_kind="HANDOFF",
            scope_ref_id=handoff_id,
            status=visible_status,
            outcome_code=normalized_reason,
            authorized_parameters={
                "execution_status": execution_status,
                "evidence_state": evidence_state,
                "decision_policy": decision_policy,
                "affected_scope_count": len(handoff.get("affected_node_ids", [])),
            },
            artifact_refs=(artifact_id,),
            correlation_id=handoff_id,
            occurred_at_utc=_activity_time(event.get("created_at_utc")),
        ),
    )
    return event


def invoke_native_structure_host_tool(
    project_id: str,
    document_id: str,
    payload: NativeStructureHostToolRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    """Execute one bounded Host tool and persist its non-authoritative receipt."""

    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    if payload.expected_source_sha256 != source_sha256:
        raise _conflict(
            "native_structure_source_changed",
            "The document source changed before the Host tool call.",
        )
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact = bundle["artifact"]
    artifact_id = str(artifact["artifact_id"])
    if payload.expected_artifact_id != artifact_id:
        raise _conflict(
            "native_structure_reference_changed",
            "The native structure candidate changed before the Host tool call.",
        )
    handoff = next(
        (
            item
            for item in _read_handoffs(
                source_sha256=source_sha256,
                artifact_id=artifact_id,
            )
            if str(item.get("handoff_id") or "") == payload.handoff_id
        ),
        None,
    )
    if handoff is None:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_handoff_invalid",
                "message": "The selected Agent handoff is not current.",
            },
        )
    latest_events = _latest_handoff_events(
        _read_handoff_events(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        )
    )
    if latest_events.get(payload.handoff_id, {}).get("status") != "ACKNOWLEDGED":
        raise _conflict(
            "native_structure_handoff_not_acknowledged",
            "The Agent must acknowledge this handoff before calling Host tools.",
        )
    if payload.scope_id not in {
        str(item) for item in handoff.get("affected_node_ids", []) if str(item)
    }:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_host_tool_scope_invalid",
                "message": "The Host tool call targets a scope outside this handoff.",
            },
        )
    existing_receipts = _read_host_tool_receipts(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    same_command = [
        NativeStructureHostToolReceiptV1.model_validate(item)
        for item in existing_receipts
        if item.get("command_id") == payload.command_id
    ]
    if same_command:
        receipt = same_command[-1]
        if receipt.request_sha256 != _canonical_sha256(payload.model_dump(mode="json")):
            raise _conflict(
                "native_structure_host_tool_command_conflict",
                "The Host tool command ID was already used with different content.",
            )
        result = _execute_native_structure_host_tool(bundle, payload)
        if receipt.tool_call.result_sha256 != (
            _canonical_sha256(result) if result is not None else None
        ):
            raise _reference_invalid("native structure Host tool replay drifted")
        _record_host_tool_activity(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=artifact_id,
            receipt=receipt,
        )
        return {
            "schema_version": "native-structure-host-tool-result/v1",
            "receipt": receipt.model_dump(mode="json"),
            "result": result,
        }
    result = _execute_native_structure_host_tool(bundle, payload)
    if result is None:
        receipt = build_native_structure_host_tool_receipt(
            payload,
            outcome=StructureToolOutcome.UNAVAILABLE,
            failure_code="host_tool_intent_unavailable",
        )
    else:
        result_role = (
            StructureToolResultRole.ADVISORY_CANDIDATE
            if payload.intent is StructureToolIntent.RESTORE_TABLE_CANDIDATE
            else StructureToolResultRole.SOURCE_EVIDENCE
        )
        receipt = build_native_structure_host_tool_receipt(
            payload,
            outcome=StructureToolOutcome.SUCCEEDED,
            result_role=result_role,
            result_sha256=_canonical_sha256(result),
        )
    receipt = _append_host_tool_receipt(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        receipt=receipt,
    )
    _record_host_tool_activity(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        receipt=receipt,
    )
    return {
        "schema_version": "native-structure-host-tool-result/v1",
        "receipt": receipt.model_dump(mode="json"),
        "result": result,
    }


def submit_native_structure_handoff_attempt(
    project_id: str,
    document_id: str,
    payload: NativeStructureHandoffAttemptRequestV1,
    response: ResponseSink,
    *,
    decision_evidence_state: Literal[
        "SUFFICIENT", "CONFLICTING", "INSUFFICIENT"
    ] = "SUFFICIENT",
    decision_policy: Literal[
        "FIRST_HOST_VALIDATED_PROPOSAL"
    ] = "FIRST_HOST_VALIDATED_PROPOSAL",
    expected_global_head_attempt_id: str | None = None,
    expected_global_projection_attempts_sha256: str | None = None,
    structure_loop_repository: Any | None = None,
) -> dict[str, Any]:
    """Record one Agent revision/materialization attempt after Host validation."""

    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    if payload.expected_source_sha256 != source_sha256:
        raise _conflict(
            "native_structure_source_changed",
            "The document source changed before the Agent attempt was validated.",
        )
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact_id = str(bundle["artifact"]["artifact_id"])
    if payload.expected_artifact_id != artifact_id:
        raise _conflict(
            "native_structure_reference_changed",
            "The native structure candidate changed before the Agent attempt was validated.",
        )
    handoffs = _read_handoffs(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoff = next(
        (
            item
            for item in handoffs
            if str(item.get("handoff_id") or "") == payload.handoff_id
        ),
        None,
    )
    if handoff is None:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_handoff_invalid",
                "message": "The selected Agent handoff is not current.",
            },
        )
    latest_events = _latest_handoff_events(
        _read_handoff_events(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        )
    )
    if latest_events.get(payload.handoff_id, {}).get("status") != "ACKNOWLEDGED":
        raise _conflict(
            "native_structure_handoff_not_acknowledged",
            "The Agent must acknowledge this handoff before publishing an attempt.",
        )
    allowed_scopes = {
        str(item) for item in handoff.get("affected_node_ids", []) if str(item)
    }
    if not set(payload.affected_scope_ids).issubset(allowed_scopes):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_handoff_attempt_scope_invalid",
                "message": "The Agent attempt targets a scope outside this handoff.",
            },
        )
    allowed_items = {
        "FEEDBACK": {
            str(item) for item in handoff.get("feedback_ids", []) if str(item)
        },
        "PROPOSAL_DECISION": {
            str(item) for item in handoff.get("proposal_decision_ids", []) if str(item)
        },
    }
    if any(
        item.input_id not in allowed_items[item.input_kind]
        for item in payload.work_item_dispositions
    ):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_handoff_attempt_item_invalid",
                "message": "The Agent attempt resolves an item outside this handoff.",
            },
        )
    stored_tool_receipts = {
        item.tool_call.call_id: item
        for item in (
            NativeStructureHostToolReceiptV1.model_validate(raw)
            for raw in _read_host_tool_receipts(
                source_sha256=source_sha256,
                artifact_id=artifact_id,
            )
        )
        if item.handoff_id == payload.handoff_id
    }
    for tool_call in payload.tool_calls:
        stored = stored_tool_receipts.get(tool_call.call_id)
        if (
            stored is None
            or stored.scope_id not in set(payload.affected_scope_ids)
            or stored.tool_call != tool_call
        ):
            raise ApplicationError(
                status_code=422,
                detail={
                    "code": "native_structure_handoff_attempt_tool_receipt_invalid",
                    "message": (
                        "Every claimed tool call must match one Host-issued receipt "
                        "for this handoff and scope."
                    ),
                },
            )
    current_materialization_sha256 = _canonical_sha256(bundle["artifact"])
    if any(
        revision.basis_role.value == "CURRENT_MATERIALIZATION"
        and revision.basis_sha256 != current_materialization_sha256
        for revision in payload.candidate_revisions
    ):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_handoff_attempt_revision_basis_invalid",
                "message": "The Agent revision does not bind the current artifact.",
            },
        )
    _validate_projection_operations(
        bundle,
        tuple(
            operation
            for revision in payload.candidate_revisions
            for operation in revision.operations
        ),
        source_path=_resolved_document_source_path(document),
        source_sha256=source_sha256,
    )
    all_attempts = _read_all_handoff_attempts(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    handoff_attempts = [
        NativeStructureHandoffAttemptReceiptV1.model_validate(item)
        for item in all_attempts
        if str(item.get("handoff_id") or "") == payload.handoff_id
    ]
    same_command = [
        item for item in handoff_attempts if item.command_id == payload.command_id
    ]
    if same_command:
        receipt = same_command[-1]
        if receipt.request_sha256 != _canonical_sha256(payload.model_dump(mode="json")):
            raise _conflict(
                "native_structure_handoff_attempt_command_conflict",
                "The attempt command ID was already used with different content.",
            )
        working_decision = receipt.validation_report.get("working_decision")
        if working_decision != {
            "evidence_state": decision_evidence_state,
            "decision_policy": decision_policy,
        }:
            raise _conflict(
                "native_structure_handoff_attempt_decision_metadata_conflict",
                "The attempt decision metadata changed after publication.",
            )
        _record_attempt_activity(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=artifact_id,
            receipt=receipt,
        )
        return {
            "schema_version": "native-structure-handoff-attempt-result/v1",
            "attempt": _public_handoff_attempt(receipt),
            "next_action": _attempt_next_action(
                handoff,
                (*handoff_attempts,),
            ),
        }
    accepted_item_keys = {
        (item.input_kind, item.input_id)
        for attempt in handoff_attempts
        if attempt.status is StructureHandoffAttemptStatus.VERIFIED
        for item in attempt.accepted_work_item_dispositions
    }
    if any(
        (item.input_kind, item.input_id) in accepted_item_keys
        for item in payload.work_item_dispositions
    ):
        raise _conflict(
            "native_structure_handoff_attempt_item_already_resolved",
            "A work item in this attempt already has a verified disposition.",
        )
    from reqpilotx_server.adapters.agents.structure_failure_pattern_host import (
        StructureFailurePatternHost,
    )

    host_result = StructureFailurePatternHost().validate_and_select(
        payload.materialization_observation
    )
    previous_attempt = handoff_attempts[-1] if handoff_attempts else None
    validation_report = dict(host_result.validation_report)
    validation_report["working_decision"] = {
        "evidence_state": decision_evidence_state,
        "decision_policy": decision_policy,
    }
    validation_report["rendered_scope_progress"] = _rendered_scope_progress(
        bundle,
        affected_scope_ids=payload.affected_scope_ids,
        confirmed_actions=payload.materialization_observation.confirmed_actions,
        candidate_revisions=tuple(
            revision
            for attempt in handoff_attempts
            for revision in attempt.candidate_revisions
        )
        + payload.candidate_revisions,
    )
    try:
        receipt = build_native_structure_handoff_attempt_receipt(
            payload,
            validation_report=validation_report,
            selected_failure_patterns=host_result.selected_failure_patterns,
            kit_version=host_result.kit_version,
            kit_bundle_sha256=host_result.kit_bundle_sha256,
            previous_attempt=previous_attempt,
        )
    except ValueError as exc:
        raise _conflict(
            "native_structure_handoff_attempt_lineage_invalid",
            "The Agent attempt does not continue the current revision chain.",
        ) from exc
    receipt = _persist_handoff_attempt(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        request=payload,
        receipt=receipt,
        expected_global_head_attempt_id=expected_global_head_attempt_id,
        expected_global_projection_attempts_sha256=(
            expected_global_projection_attempts_sha256
        ),
        structure_loop_repository=structure_loop_repository,
    )
    _record_attempt_activity(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        receipt=receipt,
    )
    updated_attempts = (*handoff_attempts, receipt)
    return {
        "schema_version": "native-structure-handoff-attempt-result/v1",
        "attempt": _public_handoff_attempt(receipt),
        "next_action": _attempt_next_action(handoff, updated_attempts),
    }


def acknowledge_native_structure_rendered_reopen(
    project_id: str,
    document_id: str,
    payload: NativeStructureRenderedReopenRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    # The merged public projection includes legacy JSONL and durable attempts.
    # This RLock freezes its file-backed feedback/handoff/proposal inputs in the
    # supported API process; the durable-only database CAS separately protects
    # cross-process attempt writes.
    with _FEEDBACK_LOCK:
        return _acknowledge_native_structure_rendered_reopen_locked(
            project_id,
            document_id,
            payload,
            response,
        )


def _acknowledge_native_structure_rendered_reopen_locked(
    project_id: str,
    document_id: str,
    payload: NativeStructureRenderedReopenRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    response.headers.update(_no_store_headers())
    source_sha256 = _document_source_sha256(document)
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact_id = str(bundle["artifact"]["artifact_id"])
    if (
        payload.expected_source_sha256 != source_sha256
        or payload.expected_artifact_id != artifact_id
    ):
        raise _conflict(
            "native_structure_rendered_reopen_identity_changed",
            "The source or structure artifact changed before rendered verification.",
        )
    repository = _structure_loop_repository()
    if repository is None:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "native_structure_rendered_reopen_unavailable",
                "message": "Durable rendered verification is unavailable.",
            },
        )
    raw_attempts = _read_all_handoff_attempts(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    all_attempts = tuple(
        NativeStructureHandoffAttemptReceiptV1.model_validate(item)
        for item in raw_attempts
    )
    handoffs = _read_handoffs(
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    public_reference = _serialize_public_reference(
        bundle,
        feedbacks=_read_feedbacks(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        proposal_decisions=_read_proposal_decisions(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        handoffs=handoffs,
        handoff_events=_read_handoff_events(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        handoff_attempts=raw_attempts,
        source_path=_resolved_document_source_path(document),
    )
    adopted_attempts = _adopted_projection_attempts(raw_attempts)
    active_handoff_id = _active_projection_handoff_id(
        handoffs=handoffs,
        adopted_attempts=adopted_attempts,
    )
    structure_projection = public_reference.get("structure_projection")
    if (
        not isinstance(structure_projection, dict)
        or structure_projection.get("state")
        != "WORKING_VERIFICATION_REQUIRED"
        or structure_projection.get("attempt_id") != payload.attempt_id
        or active_handoff_id != payload.handoff_id
    ):
        raise _conflict(
            "native_structure_rendered_reopen_projection_not_current",
            "Only the active authoritative working projection can be reopened.",
        )
    if (
        structure_projection.get("projection_sha256")
        != payload.expected_projection_sha256
    ):
        raise _conflict(
            "native_structure_rendered_reopen_projection_changed",
            "The authoritative working projection changed before rendered verification.",
        )
    attempts = tuple(
        attempt
        for attempt in all_attempts
        if attempt.handoff_id == payload.handoff_id
    )
    selected = next(
        (item for item in attempts if item.attempt_id == payload.attempt_id),
        None,
    )
    if (
        selected is None
        or selected is not attempts[-1]
        or selected.status is not StructureHandoffAttemptStatus.VERIFICATION_REQUIRED
    ):
        raise _conflict(
            "native_structure_rendered_reopen_attempt_not_current",
            "Only the latest verification-required attempt can be reopened.",
        )
    if (
        payload.scope_id not in set(selected.affected_scope_ids)
        or selected.rendered_output_sha256 != payload.expected_rendered_output_sha256
    ):
        raise _conflict(
            "native_structure_rendered_reopen_scope_mismatch",
            "Rendered verification must target one affected scope from the current attempt.",
        )
    progress = selected.validation_report.get("rendered_scope_progress")
    pending_scope_ids = (
        tuple(str(item) for item in progress.get("pending_scope_ids", []))
        if isinstance(progress, dict)
        else selected.affected_scope_ids
    )
    if payload.scope_id not in pending_scope_ids:
        raise _conflict(
            "native_structure_rendered_reopen_scope_already_verified",
            "The selected rendered scope was already verified for this attempt.",
        )
    original = repository.get_attempt_request(
        project_id=project_id,
        document_id=document_id,
        attempt_id=selected.attempt_id,
    )
    if original is None:
        raise _conflict(
            "native_structure_rendered_reopen_request_missing",
            "The durable materialization request is unavailable.",
        )
    public_tree = public_reference["document_tree"]
    rendered_node = next(
        (
            item
            for item in public_tree.get("nodes", [])
            if str(item.get("node_id") or "") == payload.scope_id
        ),
        None,
    )
    if rendered_node is None:
        raise _conflict(
            "native_structure_rendered_reopen_scope_missing",
            "The materialized scope is no longer present in the public projection.",
        )
    projection_operations = _verified_projection_operations(raw_attempts)
    rendered_table_projections: list[dict[str, str]] = []
    for block in rendered_node.get("content_blocks", []):
        if block.get("kind") != "LOGICAL_TABLE":
            continue
        table_scope_id = str(block.get("logical_table_scope_id") or "")
        if not table_scope_id:
            continue
        fragments = _public_table_preview_fragments(
            bundle.get("document_tree_evidence"),
            scope_id=table_scope_id,
            artifact=bundle["artifact"],
        )
        presentation = _table_presentation(
            bundle,
            scope_id=table_scope_id,
            operations=projection_operations,
            fragments=fragments,
        )
        canonical_projection = presentation.get("canonical_projection")
        if isinstance(canonical_projection, dict):
            rendered_table_projections.append(
                {
                    "logical_table_scope_id": table_scope_id,
                    "projection_sha256": str(
                        canonical_projection.get("projection_sha256") or ""
                    ),
                }
            )
    rendered_table_projections.sort(
        key=lambda item: item["logical_table_scope_id"]
    )
    reopen_receipt_sha256 = _canonical_sha256(
        {
            "attempt_id": selected.attempt_id,
            "rendered_output_sha256": selected.rendered_output_sha256,
            "scope_id": payload.scope_id,
            "public_node_sha256": _canonical_sha256(rendered_node),
            "rendered_table_projections": rendered_table_projections,
        }
    )
    revision_scope_by_action = {
        revision.revision_id: revision.target_scope_id
        for attempt in attempts
        for revision in attempt.candidate_revisions
    }
    if any(
        item.action_id not in revision_scope_by_action
        for item in original.materialization_observation.confirmed_actions
    ):
        raise _conflict(
            "native_structure_rendered_reopen_action_scope_missing",
            "The materialized action cannot be bound to its affected scope.",
        )
    observation = original.materialization_observation.model_dump(mode="json")
    observation["confirmed_actions"] = [
        {
            **item.model_dump(mode="json"),
            "verification_receipt_id": (
                "rendered-reopen-" + reopen_receipt_sha256[:32]
                if revision_scope_by_action[item.action_id] == payload.scope_id
                else item.verification_receipt_id
            ),
            "rendered_output_reopened": (
                True
                if revision_scope_by_action[item.action_id] == payload.scope_id
                else item.rendered_output_reopened
            ),
        }
        for item in original.materialization_observation.confirmed_actions
    ]
    remaining_scope_count = len(pending_scope_ids) - 1
    working_decision = selected.validation_report.get("working_decision")
    if not isinstance(working_decision, dict):
        working_decision = {
            "evidence_state": "SUFFICIENT",
            "decision_policy": "FIRST_HOST_VALIDATED_PROPOSAL",
        }
    evidence_state = str(working_decision.get("evidence_state") or "")
    if (
        evidence_state not in {"SUFFICIENT", "CONFLICTING", "INSUFFICIENT"}
        or working_decision.get("decision_policy")
        != "FIRST_HOST_VALIDATED_PROPOSAL"
    ):
        raise _conflict(
            "native_structure_rendered_reopen_decision_metadata_invalid",
            "The working decision metadata is invalid for rendered verification.",
        )
    verification_request = NativeStructureHandoffAttemptRequestV1(
        command_id=payload.command_id,
        expected_source_sha256=source_sha256,
        expected_artifact_id=artifact_id,
        handoff_id=payload.handoff_id,
        attempt_ordinal=selected.attempt_ordinal + 1,
        previous_attempt_receipt_sha256=selected.receipt_sha256,
        affected_scope_ids=selected.affected_scope_ids,
        materialized_candidate_sha256=selected.materialized_candidate_sha256,
        rendered_output_sha256=selected.rendered_output_sha256,
        materialization_observation=observation,
        work_item_dispositions=original.work_item_dispositions,
        current_action=(
            f"已复验当前范围，正在按来源顺序打开其余 {remaining_scope_count} 个范围。"
            if remaining_scope_count
            else "全部变更范围已重新打开，正在完成 Host 后置验证。"
        ),
        public_summary=(
            f"当前范围已通过 Host 复验；其余 {remaining_scope_count} 个范围将继续自动核对。"
            if remaining_scope_count
            else "全部变更范围已在真实工作台重新打开并通过 Host 复验。"
        ),
    )
    durable_attempts = tuple(
        repository.list_attempts(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        )
    )
    durable_head = active_structure_projection_attempt(durable_attempts)
    if durable_head is None or durable_head.attempt_id != selected.attempt_id:
        raise _conflict(
            "native_structure_rendered_reopen_durable_projection_not_current",
            "The durable working projection changed before rendered verification.",
        )
    # The user-visible SHA remains bound to the merged public projection above.
    # The CAS digest intentionally covers only rows visible to the durable
    # repository transaction that will recompute it under the document lock.
    durable_projection_attempts_sha256 = (
        canonical_structure_projection_attempts_sha256(durable_attempts)
    )
    result = submit_native_structure_handoff_attempt(
        project_id,
        document_id,
        verification_request,
        response,
        decision_evidence_state=evidence_state,  # type: ignore[arg-type]
        decision_policy="FIRST_HOST_VALIDATED_PROPOSAL",
        expected_global_head_attempt_id=selected.attempt_id,
        expected_global_projection_attempts_sha256=(
            durable_projection_attempts_sha256
        ),
        structure_loop_repository=repository,
    )
    return {
        "schema_version": "native-structure-rendered-reopen-result/v1",
        "reopen_receipt_sha256": reopen_receipt_sha256,
        **result,
    }


def _load_latest_reference(source_sha256: str) -> dict[str, Any]:
    if not _SHA256_PATTERN.fullmatch(source_sha256):
        raise _not_found()
    root = _resolved_safe_directory(REFERENCE_ROOT, required=False)
    if root is None:
        raise _not_found()
    matches: list[tuple[str, dict[str, Any]]] = []
    for candidate in root.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        manifest_path = candidate / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        manifest = _read_json_file(manifest_path, root=candidate)
        if (
            manifest.get("schema_version")
            != "reqpilotx-native-structure-run-manifest/v1"
            or manifest.get("source_sha256") != source_sha256
            or manifest.get("workflow_step") != "DOCUMENT_STRUCTURE_RECOVERY"
        ):
            continue
        _verify_manifest_files(candidate, manifest)
        run_id = str(manifest.get("run_id") or "")
        if not _SAFE_ID_PATTERN.fullmatch(run_id):
            raise _reference_invalid("native structure run identity is invalid")
        artifact = _read_json_file(
            candidate / "native-structure-reference.json",
            root=candidate,
        )
        repeatability = _read_json_file(
            candidate / "repeatability.json",
            root=candidate,
        )
        validation = _read_json_file(
            candidate / "validation.json",
            root=candidate,
        )
        oracle_diagnostic = _read_json_file(
            candidate / "private-oracle-diagnostic.json",
            root=candidate,
        )
        activity = _read_jsonl_file(candidate / "activity.jsonl", root=candidate)
        tree_path = candidate / "document-structure-tree.json"
        document_tree = (
            _read_json_file(tree_path, root=candidate)
            if tree_path.is_file() and not tree_path.is_symlink()
            else None
        )
        evidence_path = candidate / "document-structure-evidence.json"
        document_tree_evidence = (
            _read_json_file(evidence_path, root=candidate)
            if evidence_path.is_file() and not evidence_path.is_symlink()
            else None
        )
        proposal_overrides_path = (
            candidate / "document-structure-proposal-overrides.json"
        )
        proposal_overrides = (
            _read_json_file(proposal_overrides_path, root=candidate)
            if proposal_overrides_path.is_file()
            and not proposal_overrides_path.is_symlink()
            else None
        )
        if (
            artifact.get("source", {}).get("sha256") != source_sha256
            or artifact.get("artifact_id") != run_id
            or validation.get("artifact_id") != run_id
        ):
            raise _reference_invalid("native structure artifact identity drifted")
        matches.append(
            (
                run_id,
                {
                    "manifest": manifest,
                    "artifact": artifact,
                    "repeatability": repeatability,
                    "validation": validation,
                    "oracle_diagnostic": oracle_diagnostic,
                    "activity": activity,
                    "document_tree": document_tree,
                    "document_tree_evidence": document_tree_evidence,
                    "proposal_overrides": proposal_overrides,
                },
            )
        )
    if not matches:
        raise _not_found()
    matches.sort(key=lambda item: item[0])
    return matches[-1][1]


def _serialize_public_reference(
    bundle: dict[str, Any],
    *,
    feedbacks: list[dict[str, Any]],
    proposal_decisions: list[dict[str, Any]] | None = None,
    handoffs: list[dict[str, Any]] | None = None,
    handoff_events: list[dict[str, Any]] | None = None,
    handoff_attempts: list[dict[str, Any]] | None = None,
    source_path: Path | None = None,
) -> dict[str, Any]:
    if bundle.get("portable_source_graph") is not None:
        from reqpilotx_server.application.agent_workflow.native_source_graph import native_source_graph_reference
        reference = native_source_graph_reference(bundle["portable_source_graph"])
        reference["feedbacks"] = _public_feedbacks(feedbacks, handoffs=handoffs or [],
            handoff_events=handoff_events or [], handoff_attempts=handoff_attempts or [])
        if source_path is not None:
            reference["reading_projections"] = _content_reading_projections(reference["document_tree"]["nodes"],
                operations=(), source_path=source_path, source_sha256=reference["source_sha256"])
        return reference
    artifact = bundle["artifact"]
    repeatability = bundle["repeatability"]
    validation = bundle["validation"]
    oracle_diagnostic = bundle["oracle_diagnostic"]
    logical_tables = []
    table_pages = {
        str(item.get("table_token") or ""): int(item.get("page") or 0)
        for item in artifact.get("physical_table_dispositions", [])
    }
    for table in artifact.get("logical_tables", []):
        fragments = list(table.get("fragments") or [])
        pages = sorted(
            {
                int(fragment.get("page") or 0)
                for fragment in fragments
                if int(fragment.get("page") or 0) > 0
            }
        )
        logical_tables.append(
            {
                "scope_id": table.get("scope_id"),
                "title": table.get("title"),
                "owner_unit_id": table.get("owner_unit_id"),
                "role": table.get("role"),
                "pages": pages,
                "fragments": fragments,
                "source_numbering_anomaly": table.get("source_numbering_anomaly", ""),
                "capture_gap": table.get("capture_gap", ""),
            }
        )
    unresolved = []
    for item in artifact.get("unresolved", []):
        public_item = dict(item)
        public_item["pages"] = _scope_pages(
            str(item.get("scope") or ""),
            table_pages=table_pages,
            page_count=int(artifact.get("source", {}).get("page_count") or 0),
        )
        unresolved.append(public_item)
    adopted_projection_attempts = _adopted_projection_attempts(
        handoff_attempts or []
    )
    projection_operations = _projection_operations_from_attempts(
        adopted_projection_attempts
    )
    document_tree = _public_document_tree(
        bundle.get("document_tree"),
        evidence=bundle.get("document_tree_evidence"),
        proposal_decisions=proposal_decisions or [],
        artifact=artifact,
        logical_tables=logical_tables,
        unresolved=unresolved,
    )
    reading_projections = _content_reading_projections(
        document_tree.get("nodes", []), operations=projection_operations,
        source_path=source_path,
        source_sha256=str(artifact.get("source", {}).get("sha256") or ""),
    )
    document_tree = _apply_tree_projection_operations(
        document_tree,
        operations=projection_operations,
    )
    working_lineages = _unresolved_projection_lineages(
        adopted_projection_attempts
    )
    # attempt_id stays a singular cursor to the globally latest unresolved
    # lineage. The tree can contain working operations from multiple handoffs,
    # so its affected and pending scopes must describe their deterministic union.
    projection_head = (
        working_lineages[-1][1]
        if working_lineages
        else adopted_projection_attempts[-1]
        if adopted_projection_attempts
        else None
    )
    if working_lineages:
        projection_state = "WORKING_VERIFICATION_REQUIRED"
        projection_affected_scope_ids = _ordered_unique_strings(
            scope_id
            for _, attempt in working_lineages
            for scope_id in attempt.affected_scope_ids
        )
        projection_pending_scope_ids = _ordered_unique_strings(
            scope_id
            for _, attempt in working_lineages
            for scope_id in _attempt_pending_rendered_scope_ids(attempt)
        )
        projection_validation_status = "FAIL"
    else:
        projection_state = "ACCEPTED"
        projection_affected_scope_ids = (
            list(projection_head.affected_scope_ids)
            if projection_head is not None
            else []
        )
        projection_pending_scope_ids = (
            list(_attempt_pending_rendered_scope_ids(projection_head))
            if projection_head is not None
            else []
        )
        projection_validation_status = (
            str(projection_head.validation_report.get("status") or "")
            if projection_head is not None
            else str(validation.get("status") or "")
        )
    structure_proposals = _public_structure_proposals(
        bundle.get("document_tree_evidence"),
        overrides=bundle.get("proposal_overrides"),
        proposal_decisions=proposal_decisions or [],
    )
    continuation = _public_continuation(
        feedbacks=feedbacks,
        proposal_decisions=proposal_decisions or [],
        handoffs=handoffs or [],
        handoff_events=handoff_events or [],
        handoff_attempts=handoff_attempts or [],
        proposals=structure_proposals,
    )
    public_feedbacks = _public_feedbacks(
        feedbacks,
        handoffs=handoffs or [],
        handoff_events=handoff_events or [],
        handoff_attempts=handoff_attempts or [],
    )
    return {
        "schema_version": REFERENCE_PUBLIC_SCHEMA_VERSION,
        "availability": "READY",
        "artifact_id": artifact.get("artifact_id"),
        "source_sha256": artifact.get("source", {}).get("sha256"),
        "artifact_status": artifact.get("status"),
        "coverage_status": artifact.get("coverage_status"),
        "boundary_status": artifact.get("boundary_status"),
        "workflow_step": artifact.get("scope", {}).get("workflow_step"),
        "summary": artifact.get("summary", {}),
        "page_ownership": artifact.get("page_ownership", []),
        "main_document_sections": artifact.get("main_document_sections", []),
        "document_tree": document_tree,
        "reading_projections": reading_projections,
        "working_revision_head": bundle.get("working_revision_head"),
        "structure_projection": {
            "schema_version": "native-structure-projection-state/v1",
            "state": projection_state,
            "base_artifact_id": artifact.get("artifact_id"),
            "attempt_id": (
                projection_head.attempt_id if projection_head is not None else None
            ),
            "attempt_ordinal": (
                projection_head.attempt_ordinal
                if projection_head is not None
                else None
            ),
            "affected_scope_ids": projection_affected_scope_ids,
            "pending_rendered_scope_ids": projection_pending_scope_ids,
            "validation_status": projection_validation_status,
            "projection_sha256": _canonical_sha256(document_tree),
        },
        "structure_proposals": structure_proposals,
        "logical_tables": logical_tables,
        "physical_table_dispositions": artifact.get("physical_table_dispositions", []),
        "nested_physical_tables": artifact.get("nested_physical_tables", []),
        "nested_visual_tables": artifact.get("nested_visual_tables", []),
        "source_anomalies": artifact.get("source_anomalies", []),
        "unresolved": unresolved,
        "implementation_findings": artifact.get("implementation_findings", []),
        "visual_summary": {
            "opened_page_count": len(artifact.get("visual_receipts", [])),
            "page_count": artifact.get("source", {}).get("page_count", 0),
            "receipts": artifact.get("visual_receipts", []),
        },
        "repeatability": {
            "status": repeatability.get("status"),
            "stable_semantics": repeatability.get("stable_semantics", {}),
            "refinements_applied": repeatability.get("refinements_applied", []),
            "unresolved_not_settled_by_replay": repeatability.get(
                "unresolved_not_settled_by_replay", []
            ),
            "quality_claim": repeatability.get("quality_claim"),
        },
        "validation": {
            "status": validation.get("status"),
            "checks": validation.get("checks", {}),
            "quality_claim": validation.get("quality_claim"),
        },
        "oracle_diagnostic": {
            "authority": oracle_diagnostic.get("oracle_authority"),
            "oracle_expected_region_count": oracle_diagnostic.get(
                "oracle_expected_region_count", 0
            ),
            "native_region_disposition_count": oracle_diagnostic.get(
                "native_region_disposition_count", 0
            ),
            "count_is_correctness_signal": oracle_diagnostic.get(
                "count_is_correctness_signal", False
            ),
            "native_boundary_corrections": oracle_diagnostic.get(
                "native_boundary_corrections", []
            ),
        },
        "activity": bundle["activity"],
        "feedbacks": public_feedbacks,
        "continuation": continuation,
        "feedback_capability": {
            "status": "QUEUE_READY",
            "agent_delivery": continuation["status"],
            "message": (
                "可保存节点指引并将本轮反馈交接给 Codex App；Agent 确认和局部重算状态仍需真实回执。"
            ),
        },
    }


def _scope_pages(
    value: str,
    *,
    table_pages: dict[str, int],
    page_count: int,
) -> list[int]:
    pages: set[int] = set()
    for match in re.finditer(r"P(\d+)(?:\s*-\s*P?(\d+))?", value, re.IGNORECASE):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start:
            start, end = end, start
        pages.update(range(start, end + 1))
    for token in re.findall(r"T\d{4}", value, re.IGNORECASE):
        page = table_pages.get(token.upper(), 0)
        if page > 0:
            pages.add(page)
    return sorted(page for page in pages if 1 <= page <= page_count)


def _document_tree_nodes(value: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    nodes = value.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [item for item in nodes if isinstance(item, dict)]


def _public_document_tree(
    value: dict[str, Any] | None,
    *,
    evidence: dict[str, Any] | None,
    proposal_decisions: list[dict[str, Any]],
    artifact: dict[str, Any],
    logical_tables: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
) -> dict[str, Any]:
    if value is None:
        return _fallback_document_tree(artifact, logical_tables=logical_tables)
    if value.get("schema_version") != "native-document-structure-tree/v1":
        raise _reference_invalid("native document tree schema is invalid")
    nodes = _document_tree_nodes(value)
    if not nodes:
        raise _reference_invalid("native document tree has no nodes")
    page_count = int(artifact.get("source", {}).get("page_count") or 0)
    table_ids = {str(item.get("scope_id") or "") for item in logical_tables}
    finding_ids = {str(item.get("code") or "") for item in unresolved}
    public_nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for position, item in enumerate(nodes):
        node_id = str(item.get("node_id") or "")
        if not _SAFE_ID_PATTERN.fullmatch(node_id) or node_id in node_ids:
            raise _reference_invalid("native document tree node identity is invalid")
        node_ids.add(node_id)
        parent_id = item.get("parent_id")
        if parent_id is not None:
            parent_id = str(parent_id)
            if not _SAFE_ID_PATTERN.fullmatch(parent_id):
                raise _reference_invalid(
                    "native document tree parent identity is invalid"
                )
        pages = sorted(
            {
                int(page)
                for page in item.get("pages", [])
                if isinstance(page, int) and 1 <= page <= page_count
            }
        )
        if not pages:
            raise _reference_invalid("native document tree node pages are invalid")
        content_blocks: list[dict[str, Any]] = []
        for block in item.get("content_blocks", []):
            if not isinstance(block, dict) or block.get("kind") != "LOGICAL_TABLE":
                raise _reference_invalid(
                    "native document tree content block is invalid"
                )
            table_id = str(block.get("logical_table_scope_id") or "")
            if table_id not in table_ids:
                raise _reference_invalid(
                    "native document tree table reference is invalid"
                )
            content_blocks.append(
                {
                    "kind": "LOGICAL_TABLE",
                    "logical_table_scope_id": table_id,
                    **(
                        {"source_order": block["source_order"]}
                        if type(block.get("source_order")) is int
                        else {}
                    ),
                }
            )
        finding_codes = [
            str(code)
            for code in item.get("finding_codes", [])
            if str(code) in finding_ids
        ]
        raw_order = item.get("order")
        public_nodes.append(
            {
                "node_id": node_id,
                "parent_id": parent_id,
                "order": int(raw_order) if isinstance(raw_order, int) else position,
                "kind": str(item.get("kind") or "SECTION"),
                "label": str(item.get("label") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "title_authority": str(item.get("title_authority") or "VISIBLE_SOURCE"),
                "pages": pages,
                "content_blocks": content_blocks,
                "finding_codes": finding_codes,
            }
        )
    public_by_id = {item["node_id"]: item for item in public_nodes}
    for item in public_nodes:
        parent_id = item["parent_id"]
        if parent_id is not None and parent_id not in public_by_id:
            raise _reference_invalid("native document tree parent is missing")
        seen = {item["node_id"]}
        cursor = parent_id
        while cursor is not None:
            if cursor in seen:
                raise _reference_invalid("native document tree contains a cycle")
            seen.add(cursor)
            cursor = public_by_id[cursor]["parent_id"]
    root_ids = [str(item) for item in value.get("root_ids", [])]
    expected_roots = [
        item["node_id"] for item in public_nodes if item["parent_id"] is None
    ]
    if root_ids != expected_roots:
        raise _reference_invalid("native document tree roots are invalid")
    evidence_by_node = _public_tree_evidence(
        evidence,
        tree=value,
        artifact=artifact,
        nodes=public_nodes,
    )
    original_nodes_by_id = {node["node_id"]: node for node in nodes}
    for item in public_nodes:
        node_evidence = evidence_by_node.get(item["node_id"], {})
        item["source_anchors"] = _filter_reverted_image_anchors(
            node_evidence.get("anchors", []),
            proposal_decisions=proposal_decisions,
        )
        original = original_nodes_by_id[item["node_id"]]
        non_business_ids = {
            entry.get("source_node_id")
            for entry in original.get("content_sequence", [])
            if isinstance(entry, dict) and entry.get("disposition") == "NON_BUSINESS"
        }
        item["source_content"] = [
            content for content in node_evidence.get("source_content", [])
            if not content.get("source_handles")
            or not set(content["source_handles"]).issubset(non_business_ids)
        ]
    return {
        "schema_version": "native-document-structure-tree/v1",
        "root_ids": root_ids,
        "nodes": public_nodes,
    }


def _verified_projection_operations(
    raw_attempts: list[dict[str, Any]],
) -> tuple[StructureProjectionOperationV1, ...]:
    return _projection_operations_from_attempts(
        _adopted_projection_attempts(raw_attempts)
    )


def _adopted_projection_attempts(
    raw_attempts: list[dict[str, Any]],
) -> tuple[NativeStructureHandoffAttemptReceiptV1, ...]:
    return tuple(
        attempt
        for raw in raw_attempts
        for attempt in (NativeStructureHandoffAttemptReceiptV1.model_validate(raw),)
        if attempt.status
        in {
            StructureHandoffAttemptStatus.VERIFIED,
            StructureHandoffAttemptStatus.VERIFICATION_REQUIRED,
        }
    )


def _projection_operations_from_attempts(
    attempts: tuple[NativeStructureHandoffAttemptReceiptV1, ...],
) -> tuple[StructureProjectionOperationV1, ...]:
    return tuple(
        operation
        for attempt in attempts
        for revision in attempt.candidate_revisions
        for operation in revision.operations
    )


def _attempt_pending_rendered_scope_ids(
    attempt: NativeStructureHandoffAttemptReceiptV1,
) -> tuple[str, ...]:
    progress = attempt.validation_report.get("rendered_scope_progress")
    if not isinstance(progress, dict):
        return ()
    pending_scope_ids = progress.get("pending_scope_ids")
    if not isinstance(pending_scope_ids, list):
        return ()
    return tuple(str(item) for item in pending_scope_ids)


def _latest_adopted_projection_attempts_by_handoff(
    adopted_attempts: tuple[NativeStructureHandoffAttemptReceiptV1, ...],
) -> tuple[tuple[int, NativeStructureHandoffAttemptReceiptV1], ...]:
    latest_by_handoff: dict[
        str, tuple[int, NativeStructureHandoffAttemptReceiptV1]
    ] = {}
    for index, attempt in enumerate(adopted_attempts):
        latest_by_handoff[attempt.handoff_id] = (index, attempt)
    return tuple(sorted(latest_by_handoff.values(), key=lambda item: item[0]))


def _unresolved_projection_lineages(
    adopted_attempts: tuple[NativeStructureHandoffAttemptReceiptV1, ...],
) -> tuple[tuple[int, NativeStructureHandoffAttemptReceiptV1], ...]:
    return tuple(
        indexed_attempt
        for indexed_attempt in _latest_adopted_projection_attempts_by_handoff(
            adopted_attempts
        )
        if indexed_attempt[1].status
        is StructureHandoffAttemptStatus.VERIFICATION_REQUIRED
    )


def _active_projection_handoff_id(
    *,
    handoffs: list[dict[str, Any]],
    adopted_attempts: tuple[NativeStructureHandoffAttemptReceiptV1, ...],
) -> str:
    working_lineages = _unresolved_projection_lineages(adopted_attempts)
    if not working_lineages:
        return str(handoffs[-1].get("handoff_id") or "") if handoffs else ""
    active_handoff_id = working_lineages[-1][1].handoff_id
    if not any(
        str(handoff.get("handoff_id") or "") == active_handoff_id
        for handoff in handoffs
    ):
        raise _reference_invalid(
            "native structure working projection handoff is missing"
        )
    return active_handoff_id


def _ordered_unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _apply_tree_projection_operations(
    tree: dict[str, Any],
    *,
    operations: tuple[StructureProjectionOperationV1, ...],
) -> dict[str, Any]:
    if not operations:
        return tree
    output = copy.deepcopy(tree)
    nodes = output.get("nodes", [])
    by_id = {
        str(item.get("node_id") or ""): item for item in nodes if isinstance(item, dict)
    }
    for operation in operations:
        target = by_id.get(operation.target_scope_id)
        if target is None:
            raise _reference_invalid("verified tree projection target is missing")
        if (
            operation.operation
            is StructureProjectionOperationKind.COLLAPSE_REPEATED_SOURCE_LINES
        ):
            # Reading views are returned outside document_tree. Even adding a
            # presentation-only key here changes the legacy Map signature.
            # Never replay the historical text-only collapse into source data.
            continue
        elif (
            operation.operation
            is StructureProjectionOperationKind.EXCLUDE_TABLE_OWNED_DIRECT_CONTENT
        ):
            region_anchors = _projection_region_anchors(
                nodes,
                refs=set(operation.related_refs),
            )
            target["source_content"] = [
                content
                for content in target.get("source_content", [])
                if not any(
                    int(content.get("page") or 0) == int(anchor.get("page") or 0)
                    and _bbox_overlap_ratio(
                        content.get("bbox", []),
                        anchor.get("bbox", []),
                    )
                    >= 0.1
                    for anchor in region_anchors
                )
            ]
        elif (
            operation.operation
            is StructureProjectionOperationKind.COLLAPSE_NAVIGATION_CHILDREN
        ):
            child_ids = set(operation.related_refs)
            children = [by_id.get(child_id) for child_id in operation.related_refs]
            if any(
                child is None
                or child.get("parent_id") != operation.target_scope_id
                or any(
                    candidate.get("parent_id") == child.get("node_id")
                    for candidate in nodes
                )
                for child in children
            ):
                raise _reference_invalid(
                    "verified navigation collapse does not target direct leaf children"
                )
            for child in children:
                if child is None:
                    continue
                target.setdefault("source_content", []).extend(
                    copy.deepcopy(child.get("source_content", []))
                )
                target.setdefault("source_anchors", []).extend(
                    copy.deepcopy(child.get("source_anchors", []))
                )
            target["source_content"] = sorted(
                target.get("source_content", []),
                key=lambda item: (
                    int(item.get("page") or 0),
                    float((item.get("bbox") or [0, 0, 0, 0])[1]),
                    float((item.get("bbox") or [0, 0, 0, 0])[0]),
                ),
            )
            output["nodes"] = [
                item
                for item in output["nodes"]
                if str(item.get("node_id") or "") not in child_ids
            ]
            for child_id in child_ids:
                by_id.pop(child_id, None)
    return output


def _projection_region_anchors(
    nodes: list[dict[str, Any]],
    *,
    refs: set[str],
) -> list[dict[str, Any]]:
    return [
        anchor
        for node in nodes
        for anchor in node.get("source_anchors", [])
        if anchor.get("anchor_kind") == "TABLE_REGION"
        and (
            str(node.get("node_id") or "") in refs
            or str(anchor.get("logical_table_scope_id") or "") in refs
        )
    ]


def _content_reading_projections(
    nodes: list[dict[str, Any]], *,
    operations: tuple[StructureProjectionOperationV1, ...],
    source_path: Path | None, source_sha256: str,
) -> dict[str, dict[str, Any]]:
    targets = {operation.target_scope_id for operation in operations
               if operation.operation is StructureProjectionOperationKind.COLLAPSE_REPEATED_SOURCE_LINES}
    if not targets or source_path is None or not source_sha256:
        return {}
    return project_pdf_source_contents(
        source_path, expected_source_sha256=source_sha256,
        contents=[content for node in nodes if node.get("node_id") in targets
                  for content in node.get("source_content", [])],
    )


def _bbox_overlap_ratio(left_value: object, right_value: object) -> float:
    try:
        left = tuple(float(item) for item in left_value)  # type: ignore[arg-type]
        right = tuple(float(item) for item in right_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if len(left) != 4 or len(right) != 4:
        return 0.0
    intersection_left = max(left[0], right[0])
    intersection_top = max(left[1], right[1])
    intersection_right = min(left[2], right[2])
    intersection_bottom = min(left[3], right[3])
    if (
        intersection_right <= intersection_left
        or intersection_bottom <= intersection_top
    ):
        return 0.0
    area = max(1.0, (left[2] - left[0]) * (left[3] - left[1]))
    return (
        (intersection_right - intersection_left)
        * (intersection_bottom - intersection_top)
        / area
    )


def _validate_projection_operations(
    bundle: dict[str, Any],
    operations: tuple[StructureProjectionOperationV1, ...],
    *, source_path: Path | None = None, source_sha256: str = "",
) -> None:
    if not operations:
        return
    artifact = bundle["artifact"]
    logical_tables = [
        {"scope_id": str(item.get("scope_id") or "")}
        for item in artifact.get("logical_tables", [])
        if str(item.get("scope_id") or "")
    ]
    tree = _public_document_tree(
        bundle.get("document_tree"),
        evidence=bundle.get("document_tree_evidence"),
        proposal_decisions=[],
        artifact=artifact,
        logical_tables=logical_tables,
        unresolved=[dict(item) for item in artifact.get("unresolved", [])],
    )
    nodes = tree.get("nodes", [])
    reading_projections = _content_reading_projections(
        nodes, operations=operations, source_path=source_path, source_sha256=source_sha256,
    )
    by_id = {str(item.get("node_id") or ""): item for item in nodes}
    for operation in operations:
        target = by_id.get(operation.target_scope_id)
        if target is None:
            raise ApplicationError(
                status_code=422,
                detail={
                    "code": "native_structure_projection_operation_invalid",
                    "message": "The Agent revision targets a missing structure node.",
                },
            )
        if (
            operation.operation
            is StructureProjectionOperationKind.COLLAPSE_NAVIGATION_CHILDREN
        ):
            if any(
                child_id not in by_id
                or by_id[child_id].get("parent_id") != operation.target_scope_id
                or any(item.get("parent_id") == child_id for item in nodes)
                for child_id in operation.related_refs
            ):
                raise _projection_operation_invalid(
                    "Navigation collapse must target direct leaf children."
                )
        elif (
            operation.operation
            is StructureProjectionOperationKind.COLLAPSE_REPEATED_SOURCE_LINES
        ):
            if not any(
                str(item.get("content_id") or "") in reading_projections
                for item in target.get("source_content", [])
            ):
                raise _projection_operation_invalid(
                    "The selected scope has no geometry-proven native overprint to collapse."
                )
        elif (
            operation.operation
            is StructureProjectionOperationKind.EXCLUDE_TABLE_OWNED_DIRECT_CONTENT
        ):
            regions = _projection_region_anchors(
                nodes,
                refs=set(operation.related_refs),
            )
            if not regions or not any(
                int(content.get("page") or 0) == int(region.get("page") or 0)
                and _bbox_overlap_ratio(
                    content.get("bbox", []),
                    region.get("bbox", []),
                )
                >= 0.1
                for content in target.get("source_content", [])
                for region in regions
            ):
                raise _projection_operation_invalid(
                    "Table-owned exclusion has no overlapping source evidence."
                )
        elif (
            operation.operation
            is StructureProjectionOperationKind.PRESERVE_SOURCE_LABEL
        ):
            if not target.get("source_content") and not target.get("source_anchors"):
                raise _projection_operation_invalid(
                    "The source label is not present in the selected scope."
                )
        else:
            table_scope_id = str(operation.logical_table_scope_id or "")
            attached = {
                str(item.get("logical_table_scope_id") or "")
                for item in target.get("content_blocks", [])
                if item.get("kind") == "LOGICAL_TABLE"
            }
            if table_scope_id not in attached:
                raise _projection_operation_invalid(
                    "The table presentation operation targets a non-owned table."
                )
            fragments = _public_table_preview_fragments(
                bundle.get("document_tree_evidence"),
                scope_id=table_scope_id,
                artifact=artifact,
            )
            if not fragments:
                raise _projection_operation_invalid(
                    "The selected table has no source fragments."
                )
            if (
                operation.operation
                is StructureProjectionOperationKind.SET_CANONICAL_TABLE_COLUMNS
            ):
                first_cells = fragments[0].get("cells", [])
                header_count = len(
                    {
                        round(float(cell.get("bbox", [0, 0, 0, 0])[0]), 3)
                        for cell in first_cells
                        if int(cell.get("row") or 0) == 0
                        and str(cell.get("text") or "").strip()
                    }
                )
                if header_count != operation.canonical_column_count:
                    raise _projection_operation_invalid(
                        "Canonical columns do not match the visible first header."
                    )
                try:
                    _canonical_table_column_projection(
                        fragments,
                        canonical_column_count=int(operation.canonical_column_count),
                    )
                except ValueError as exc:
                    raise _projection_operation_invalid(str(exc)) from exc
            elif (
                operation.operation
                is StructureProjectionOperationKind.PLACE_FLOATING_TABLE_IMAGE
            ):
                placement_ref = operation.related_refs[0]
                image = next(
                    (
                        item
                        for item in target.get("source_anchors", [])
                        if item.get("anchor_kind") == "IMAGE"
                        and placement_ref
                        in {
                            str(item.get("anchor_id") or ""),
                            str(item.get("placement_id") or ""),
                        }
                    ),
                    None,
                )
                table_regions = [
                    item
                    for item in target.get("source_anchors", [])
                    if item.get("anchor_kind") == "TABLE_REGION"
                    and item.get("logical_table_scope_id") == table_scope_id
                ]
                if image is None or not any(
                    int(image.get("page") or 0) == int(region.get("page") or 0)
                    and _bbox_overlap_ratio(
                        image.get("bbox", []),
                        region.get("bbox", []),
                    )
                    >= 0.95
                    for region in table_regions
                ):
                    raise _projection_operation_invalid(
                        "Floating image is not contained by the selected table."
                    )


def _projection_operation_invalid(message: str) -> ApplicationError:
    return ApplicationError(
        status_code=422,
        detail={
            "code": "native_structure_projection_operation_invalid",
            "message": message,
        },
    )


def _public_tree_evidence(
    value: dict[str, Any] | None,
    *,
    tree: dict[str, Any],
    artifact: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if value.get("schema_version") != "native-structure-node-evidence/v1":
        raise _reference_invalid("native structure evidence schema is invalid")
    if value.get("source_sha256") != artifact.get("source", {}).get("sha256"):
        raise _reference_invalid("native structure evidence source drifted")
    if value.get("tree_sha256") != _canonical_sha256(tree):
        raise _reference_invalid("native structure evidence tree drifted")
    raw_nodes = value.get("nodes")
    if not isinstance(raw_nodes, list):
        raise _reference_invalid("native structure evidence nodes are invalid")
    expected_nodes = {str(item["node_id"]): item for item in nodes}
    result: dict[str, dict[str, Any]] = {}
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise _reference_invalid("native structure evidence node is invalid")
        node_id = str(item.get("node_id") or "")
        node = expected_nodes.get(node_id)
        if node is None or node_id in result:
            raise _reference_invalid(
                "native structure evidence node identity is invalid"
            )
        raw_anchors = item.get("anchors")
        survey_container = (
            node.get("parent_id") is None
            and node.get("title_authority") == "AGENT_SURVEY_LABEL"
            and not item.get("source_content")
        )
        if not isinstance(raw_anchors, list) or (
            not raw_anchors and not survey_container
        ):
            raise _reference_invalid("native structure evidence anchors are missing")
        anchors: list[dict[str, Any]] = []
        seen_anchor_ids: set[str] = set()
        for raw_anchor in raw_anchors:
            if not isinstance(raw_anchor, dict):
                raise _reference_invalid("native structure evidence anchor is invalid")
            anchor_id = str(raw_anchor.get("anchor_id") or "")
            if (
                not _SAFE_ID_PATTERN.fullmatch(anchor_id)
                or anchor_id in seen_anchor_ids
            ):
                raise _reference_invalid(
                    "native structure evidence anchor identity is invalid"
                )
            seen_anchor_ids.add(anchor_id)
            page = int(raw_anchor.get("page") or 0)
            if page not in node["pages"]:
                raise _reference_invalid("native structure evidence page is invalid")
            bbox = _public_bbox(raw_anchor.get("bbox"))
            source_bounds = _public_bbox(raw_anchor.get("source_bounds"))
            if (
                bbox[0] < source_bounds[0]
                or bbox[1] < source_bounds[1]
                or bbox[2] > source_bounds[2]
                or bbox[3] > source_bounds[3]
            ):
                raise _reference_invalid(
                    "native structure evidence bbox is out of bounds"
                )
            anchor_kind = str(raw_anchor.get("anchor_kind") or "")
            precision = str(raw_anchor.get("precision") or "")
            if anchor_kind not in {"TITLE", "SOURCE_BLOCK", "TABLE_REGION", "IMAGE"}:
                raise _reference_invalid(
                    "native structure evidence anchor kind is invalid"
                )
            if precision not in {
                "EXACT_NATIVE_SPAN",
                "EXACT_SOURCE_BLOCK",
                "EXACT_TABLE_REGION",
                "EXACT_IMAGE",
                "SHARED_BLOCK_APPROXIMATE",
                "PAGE_ONLY",
            }:
                raise _reference_invalid(
                    "native structure evidence precision is invalid"
                )
            public_anchor: dict[str, Any] = {
                "anchor_id": anchor_id,
                "page": page,
                "bbox": bbox,
                "source_bounds": source_bounds,
                "anchor_kind": anchor_kind,
                "precision": precision,
            }
            for key in (
                "source_handle",
                "source_text_sha256",
                "logical_table_scope_id",
                "table_token",
                "placement_id",
                "asset_id",
            ):
                text = str(raw_anchor.get(key) or "")
                if text:
                    public_anchor[key] = text
            row_range = raw_anchor.get("row_range")
            if isinstance(row_range, list) and len(row_range) == 2:
                public_anchor["row_range"] = [int(row_range[0]), int(row_range[1])]
            anchors.append(public_anchor)
        raw_content = item.get("source_content", [])
        if not isinstance(raw_content, list):
            raise _reference_invalid("native structure source content is invalid")
        source_content: list[dict[str, Any]] = []
        for raw_block in raw_content:
            if not isinstance(raw_block, dict):
                raise _reference_invalid("native structure source content is invalid")
            content_id = str(raw_block.get("content_id") or "")
            if not _SAFE_ID_PATTERN.fullmatch(content_id):
                raise _reference_invalid(
                    "native structure source content identity is invalid"
                )
            page = int(raw_block.get("page") or 0)
            if page not in node["pages"]:
                raise _reference_invalid(
                    "native structure source content page is invalid"
                )
            bbox = _public_bbox(raw_block.get("bbox"))
            bounds = _public_bbox(raw_block.get("source_bounds"))
            if (
                bbox[0] < bounds[0]
                or bbox[1] < bounds[1]
                or bbox[2] > bounds[2]
                or bbox[3] > bounds[3]
            ):
                raise _reference_invalid(
                    "native structure source content bbox is invalid"
                )
            text = str(raw_block.get("text") or "")
            if not text or len(text) > 50_000 or "\x00" in text:
                raise _reference_invalid(
                    "native structure source content text is invalid"
                )
            text_sha256 = str(raw_block.get("text_sha256") or "")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
                raise _reference_invalid("native structure source content hash drifted")
            precision = str(raw_block.get("precision") or "")
            if precision not in {"EXACT_SOURCE_RANGE", "EXACT_NATIVE_SPAN_RANGE"}:
                raise _reference_invalid(
                    "native structure source content precision is invalid"
                )
            source_content.append(
                {
                    "content_id": content_id,
                    "page": page,
                    "bbox": bbox,
                    "source_bounds": bounds,
                    "source_handles": [
                        str(handle)
                        for handle in raw_block.get("source_handles", [])
                        if str(handle)
                    ],
                    "text": text,
                    "text_sha256": text_sha256,
                    "precision": precision,
                    "is_title": raw_block.get("is_title") is True,
                    **(
                        {"source_order": raw_block["source_order"]}
                        if type(raw_block.get("source_order")) is int
                        else {}
                    ),
                }
            )
        result[node_id] = {
            "anchors": anchors,
            "source_content": source_content,
        }
    if set(result) != set(expected_nodes):
        raise _reference_invalid("native structure evidence coverage is incomplete")
    return result


def _public_table_preview_fragments(
    value: dict[str, Any] | None,
    *,
    scope_id: str,
    artifact: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        raise _reference_invalid("native structure table previews are missing")
    if value.get("schema_version") != "native-structure-node-evidence/v1":
        raise _reference_invalid("native structure table preview schema is invalid")
    if value.get("source_sha256") != artifact.get("source", {}).get("sha256"):
        raise _reference_invalid("native structure table preview source drifted")
    raw_fragments = value.get("table_previews")
    if not isinstance(raw_fragments, list):
        raise _reference_invalid("native structure table previews are missing")
    page_count = int(artifact.get("source", {}).get("page_count") or 0)
    result: list[dict[str, Any]] = []
    seen_fragment_ids: set[str] = set()
    for raw_fragment in raw_fragments:
        if not isinstance(raw_fragment, dict):
            raise _reference_invalid("native structure table preview is invalid")
        if str(raw_fragment.get("logical_table_scope_id") or "") != scope_id:
            continue
        fragment_id = str(raw_fragment.get("fragment_id") or "")
        table_token = str(raw_fragment.get("table_token") or "")
        if (
            not _SAFE_ID_PATTERN.fullmatch(fragment_id)
            or fragment_id in seen_fragment_ids
            or not re.fullmatch(r"T\d{4}", table_token)
        ):
            raise _reference_invalid(
                "native structure table preview identity is invalid"
            )
        seen_fragment_ids.add(fragment_id)
        page = int(raw_fragment.get("page") or 0)
        if page < 1 or page > page_count:
            raise _reference_invalid("native structure table preview page is invalid")
        row_range = raw_fragment.get("row_range")
        if not isinstance(row_range, list) or len(row_range) != 2:
            raise _reference_invalid(
                "native structure table preview row range is invalid"
            )
        row_start, row_end = (int(row_range[0]), int(row_range[1]))
        row_count = int(raw_fragment.get("row_count") or 0)
        column_count = int(raw_fragment.get("column_count") or 0)
        if (
            row_start < 0
            or row_end <= row_start
            or row_count != row_end - row_start
            or column_count < 1
        ):
            raise _reference_invalid("native structure table preview shape is invalid")
        bbox = _public_bbox(raw_fragment.get("bbox"))
        source_bounds = _public_bbox(raw_fragment.get("source_bounds"))
        if (
            bbox[0] < source_bounds[0]
            or bbox[1] < source_bounds[1]
            or bbox[2] > source_bounds[2]
            or bbox[3] > source_bounds[3]
        ):
            raise _reference_invalid("native structure table preview bbox is invalid")
        raw_cells = raw_fragment.get("cells")
        if not isinstance(raw_cells, list):
            raise _reference_invalid("native structure table preview cells are invalid")
        cells = []
        seen_cell_ids: set[str] = set()
        for raw_cell in raw_cells:
            if not isinstance(raw_cell, dict):
                raise _reference_invalid(
                    "native structure table preview cell is invalid"
                )
            cell_id = str(raw_cell.get("cell_id") or "")
            row = int(raw_cell.get("row", -1))
            column = int(raw_cell.get("column", -1))
            row_span = int(raw_cell.get("row_span") or 0)
            column_span = int(raw_cell.get("column_span") or 0)
            if (
                not _SAFE_ID_PATTERN.fullmatch(cell_id)
                or cell_id in seen_cell_ids
                or row < 0
                or row >= row_count
                or column < 0
                or column >= column_count
                or row_span < 1
                or row + row_span > row_count
                or column_span < 1
                or column + column_span > column_count
            ):
                raise _reference_invalid(
                    "native structure table preview cell shape is invalid"
                )
            seen_cell_ids.add(cell_id)
            text = str(raw_cell.get("text") or "")
            text_sha256 = str(raw_cell.get("text_sha256") or "")
            if (
                len(text) > 50_000
                or "\x00" in text
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256
            ):
                raise _reference_invalid(
                    "native structure table preview cell text is invalid"
                )
            cell_bbox = _public_bbox(raw_cell.get("bbox"))
            if (
                cell_bbox[0] < source_bounds[0]
                or cell_bbox[1] < source_bounds[1]
                or cell_bbox[2] > source_bounds[2]
                or cell_bbox[3] > source_bounds[3]
            ):
                raise _reference_invalid(
                    "native structure table preview cell bbox is invalid"
                )
            cells.append(
                {
                    "cell_id": cell_id,
                    "row": row,
                    "column": column,
                    "row_span": row_span,
                    "column_span": column_span,
                    "text": text,
                    "bbox": cell_bbox,
                }
            )
        cells.sort(key=lambda item: (item["row"], item["column"], item["cell_id"]))
        result.append(
            {
                "fragment_id": fragment_id,
                "table_token": table_token,
                "page": page,
                "row_range": [row_start, row_end],
                "row_count": row_count,
                "column_count": column_count,
                "bbox": bbox,
                "source_bounds": source_bounds,
                "cells": cells,
            }
        )
    result.sort(key=lambda item: (item["page"], item["row_range"][0]))
    if not result:
        raise _reference_invalid("native structure table preview has no fragments")
    return result


def _canonical_table_column_projection(
    fragments: list[dict[str, Any]],
    *,
    canonical_column_count: int,
) -> dict[str, Any]:
    """Map every physical fragment onto one exact logical column grid.

    PDF table extractors often expose narrow, empty physical columns around
    embedded drawings. Page-to-page geometry also drifts by fractions of a
    point. Canonical columns are therefore expressed as ratios from the first
    visible header and transformed into each fragment's own bounds before
    source cells are assigned.
    """

    if canonical_column_count < 1 or not fragments:
        raise ValueError("Canonical table projection requires source fragments.")
    first = fragments[0]
    first_bbox = [float(item) for item in first.get("bbox", [])]
    if len(first_bbox) != 4 or first_bbox[2] <= first_bbox[0]:
        raise ValueError("Canonical table projection has invalid first-fragment bounds.")
    header_cells = sorted(
        (
            cell
            for cell in first.get("cells", [])
            if int(cell.get("row") or 0) == 0
            and str(cell.get("text") or "").strip()
        ),
        key=lambda cell: float(cell.get("bbox", [0, 0, 0, 0])[0]),
    )
    if len(header_cells) != canonical_column_count:
        raise ValueError("Canonical columns do not match the visible first header.")
    first_width = first_bbox[2] - first_bbox[0]
    boundary_ratios = [
        (float(cell.get("bbox", [0, 0, 0, 0])[0]) - first_bbox[0])
        / first_width
        for cell in header_cells
    ]
    # The first logical column owns the complete table edge even when the
    # extractor reports a sub-point inset.
    boundary_ratios[0] = 0.0
    boundary_ratios.append(1.0)
    if any(
        value < 0.0
        or value > 1.0
        or (index > 0 and value <= boundary_ratios[index - 1])
        for index, value in enumerate(boundary_ratios)
    ):
        raise ValueError("Canonical column boundaries are not strictly ordered.")

    fragment_projections: list[dict[str, Any]] = []
    for fragment in fragments:
        fragment_id = str(fragment.get("fragment_id") or "")
        fragment_bbox = [float(item) for item in fragment.get("bbox", [])]
        if len(fragment_bbox) != 4 or fragment_bbox[2] <= fragment_bbox[0]:
            raise ValueError(
                f"Canonical table fragment {fragment_id or '<unknown>'} has invalid bounds."
            )
        fragment_width = fragment_bbox[2] - fragment_bbox[0]
        boundaries = [
            fragment_bbox[0] + ratio * fragment_width for ratio in boundary_ratios
        ]
        # Enough to absorb PDF line-coordinate drift without swallowing a real
        # narrow column. Relative transformation already removes page scaling.
        overlap_tolerance = max(0.75, min(2.0, fragment_width * 0.002))
        mappings: list[dict[str, Any]] = []
        grouped_cells: dict[tuple[int, int, int], int] = {}
        for cell in fragment.get("cells", []):
            cell_id = str(cell.get("cell_id") or "")
            cell_bbox = [float(item) for item in cell.get("bbox", [])]
            if len(cell_bbox) != 4 or cell_bbox[2] <= cell_bbox[0]:
                raise ValueError(
                    f"Canonical table cell {cell_id or '<unknown>'} has invalid bounds."
                )
            overlaps = [
                max(
                    0.0,
                    min(cell_bbox[2], boundaries[index + 1])
                    - max(cell_bbox[0], boundaries[index]),
                )
                for index in range(canonical_column_count)
            ]
            significant = [
                index
                for index, overlap in enumerate(overlaps)
                if overlap > overlap_tolerance
            ]
            if not significant:
                best_column = max(
                    range(canonical_column_count),
                    key=lambda index: overlaps[index],
                )
                if overlaps[best_column] <= 0.0:
                    raise ValueError(
                        f"Canonical table cell {cell_id or '<unknown>'} is outside the logical grid."
                    )
                significant = [best_column]
            column = significant[0]
            column_span = significant[-1] - column + 1
            row = int(cell.get("row") or 0)
            row_span = max(1, int(cell.get("row_span") or 1))
            mappings.append(
                {
                    "cell_id": cell_id,
                    "column": column,
                    "column_span": column_span,
                }
            )
            key = (row, column, column_span)
            grouped_cells[key] = max(grouped_cells.get(key, 1), row_span)

        row_count = int(fragment.get("row_count") or 0)
        coverage = [
            [0 for _ in range(canonical_column_count)] for _ in range(row_count)
        ]
        for (row, column, column_span), row_span in grouped_cells.items():
            if (
                row < 0
                or row + row_span > row_count
                or column < 0
                or column + column_span > canonical_column_count
            ):
                raise ValueError(
                    f"Canonical table fragment {fragment_id} contains an out-of-grid cell."
                )
            for row_index in range(row, row + row_span):
                for column_index in range(column, column + column_span):
                    coverage[row_index][column_index] += 1
        invalid_row = next(
            (
                row_index
                for row_index, values in enumerate(coverage)
                if any(value != 1 for value in values)
            ),
            None,
        )
        if invalid_row is not None:
            raise ValueError(
                "Canonical table projection does not exactly cover "
                f"fragment {fragment_id} row {invalid_row}."
            )
        mappings.sort(key=lambda item: item["cell_id"])
        fragment_projections.append(
            {
                "fragment_id": fragment_id,
                "cell_mappings": mappings,
            }
        )

    projection: dict[str, Any] = {
        "schema_version": "native-structure-canonical-column-projection/v1",
        "status": "EXACT",
        "canonical_column_count": canonical_column_count,
        "boundary_ratios": boundary_ratios,
        "fragments": fragment_projections,
    }
    projection["projection_sha256"] = _canonical_sha256(projection)
    return projection


def _table_presentation(
    bundle: dict[str, Any],
    *,
    scope_id: str,
    operations: tuple[StructureProjectionOperationV1, ...],
    fragments: list[dict[str, Any]],
    recovered_visual_cells: bool = False,
) -> dict[str, Any]:
    relevant = [
        operation
        for operation in operations
        if operation.logical_table_scope_id == scope_id
    ]
    mode = "STRUCTURED_CELLS"
    canonical_column_count: int | None = None
    floating_refs: list[tuple[str, str]] = []
    for operation in relevant:
        if (
            operation.operation
            is StructureProjectionOperationKind.USE_VISUAL_TABLE_REGION
        ):
            if not recovered_visual_cells:
                mode = "VISUAL_REGION_ONLY"
        elif (
            operation.operation
            is StructureProjectionOperationKind.SET_CANONICAL_TABLE_COLUMNS
        ):
            canonical_column_count = operation.canonical_column_count
        elif (
            operation.operation
            is StructureProjectionOperationKind.PLACE_FLOATING_TABLE_IMAGE
        ):
            floating_refs.append((operation.target_scope_id, operation.related_refs[0]))
    floating_images: list[dict[str, Any]] = []
    if floating_refs:
        artifact = bundle["artifact"]
        logical_tables = [
            {"scope_id": str(item.get("scope_id") or "")}
            for item in artifact.get("logical_tables", [])
            if str(item.get("scope_id") or "")
        ]
        tree = _public_document_tree(
            bundle.get("document_tree"),
            evidence=bundle.get("document_tree_evidence"),
            proposal_decisions=[],
            artifact=artifact,
            logical_tables=logical_tables,
            unresolved=[dict(item) for item in artifact.get("unresolved", [])],
        )
        nodes = {str(item.get("node_id") or ""): item for item in tree.get("nodes", [])}
        for node_id, placement_ref in floating_refs:
            node = nodes.get(node_id, {})
            anchor = next(
                (
                    item
                    for item in node.get("source_anchors", [])
                    if item.get("anchor_kind") == "IMAGE"
                    and placement_ref
                    in {
                        str(item.get("anchor_id") or ""),
                        str(item.get("placement_id") or ""),
                    }
                ),
                None,
            )
            if anchor is None:
                raise _reference_invalid("verified floating-image placement is missing")
            floating_images.append(dict(anchor))
    canonical_projection: dict[str, Any] | None = None
    if mode == "STRUCTURED_CELLS" and canonical_column_count is not None:
        try:
            canonical_projection = _canonical_table_column_projection(
                fragments,
                canonical_column_count=canonical_column_count,
            )
        except ValueError as exc:
            raise _reference_invalid(
                f"verified canonical table projection is invalid: {exc}"
            ) from exc
    return {
        "schema_version": "native-structure-table-presentation/v1",
        "mode": mode,
        "canonical_column_count": canonical_column_count,
        "canonical_projection": canonical_projection,
        "floating_images": floating_images,
    }


def _public_bbox(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise _reference_invalid("native structure evidence bbox is invalid")
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise _reference_invalid("native structure evidence bbox is invalid") from exc
    if (
        not all(number == number and abs(number) != float("inf") for number in result)
        or result[2] <= result[0]
        or result[3] <= result[1]
    ):
        raise _reference_invalid("native structure evidence bbox is invalid")
    return result


def _latest_proposal_decisions(
    values: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        proposal_id = str(item.get("proposal_id") or "")
        if proposal_id:
            result[proposal_id] = item
    return result


def _proposal_status(
    proposal: dict[str, Any],
    decision: dict[str, Any] | None,
) -> str:
    action = str((decision or {}).get("action") or "")
    if action == "CONFIRM":
        return "CONFIRMED"
    if action == "UNDO":
        return "UNDONE"
    if action == "KEEP_UNRESOLVED":
        return "KEPT_UNRESOLVED"
    return str(proposal.get("status") or "PROPOSED_CONTINUABLE")


def _public_structure_proposals(
    evidence: dict[str, Any] | None,
    *,
    overrides: dict[str, Any] | None,
    proposal_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    proposals: list[Any] = []
    if isinstance(evidence, dict):
        evidence_proposals = evidence.get("proposals", [])
        if not isinstance(evidence_proposals, list):
            raise _reference_invalid("native structure proposals are invalid")
        proposals.extend(evidence_proposals)
    if overrides is not None:
        if overrides.get("schema_version") != "native-structure-proposal-overrides/v1":
            raise _reference_invalid(
                "native structure proposal override schema is invalid"
            )
        override_proposals = overrides.get("proposals", [])
        if not isinstance(override_proposals, list):
            raise _reference_invalid("native structure proposals are invalid")
        proposals.extend(override_proposals)
    decisions = _latest_proposal_decisions(proposal_decisions)
    result = []
    seen: set[str] = set()
    for item in proposals:
        if not isinstance(item, dict):
            raise _reference_invalid("native structure proposal is invalid")
        proposal_id = str(item.get("proposal_id") or "")
        node_id = str(item.get("node_id") or "")
        if (
            not _SAFE_ID_PATTERN.fullmatch(proposal_id)
            or proposal_id in seen
            or not _SAFE_ID_PATTERN.fullmatch(node_id)
        ):
            raise _reference_invalid("native structure proposal identity is invalid")
        seen.add(proposal_id)
        page = int(item.get("page") or 0)
        bbox = _public_bbox(item.get("bbox"))
        source_bounds = _public_bbox(item.get("source_bounds"))
        decision = decisions.get(proposal_id)
        result.append(
            {
                "proposal_id": proposal_id,
                "finding_code": str(item.get("finding_code") or ""),
                "node_id": node_id,
                "object_kind": str(item.get("object_kind") or ""),
                "object_id": str(item.get("object_id") or ""),
                "page": page,
                "bbox": bbox,
                "source_bounds": source_bounds,
                "observation": str(item.get("observation") or ""),
                "recommended_action": str(item.get("recommended_action") or ""),
                "proposal_title": str(item.get("proposal_title") or ""),
                "recommendation": str(item.get("recommendation") or ""),
                "recommended_target_node_id": str(
                    item.get("recommended_target_node_id") or ""
                ),
                "impact": str(item.get("impact") or ""),
                "confidence": str(item.get("confidence") or ""),
                "default_policy": str(item.get("default_policy") or ""),
                "status": _proposal_status(item, decision),
                "overlap_ratio": float(item.get("overlap_ratio") or 0),
                "decision": decision,
            }
        )
    return result


def _all_current_proposals(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return _public_structure_proposals(
        bundle.get("document_tree_evidence"),
        overrides=bundle.get("proposal_overrides"),
        proposal_decisions=[],
    )


def _public_continuation(
    *,
    feedbacks: list[dict[str, Any]],
    proposal_decisions: list[dict[str, Any]],
    handoffs: list[dict[str, Any]],
    handoff_events: list[dict[str, Any]],
    handoff_attempts: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    handed_feedback_ids = {
        str(item)
        for handoff in handoffs
        for item in handoff.get("feedback_ids", [])
        if str(item)
    }
    handed_decision_ids = {
        str(item)
        for handoff in handoffs
        for item in handoff.get("proposal_decision_ids", [])
        if str(item)
    }
    pending_feedback_count = sum(
        1
        for item in feedbacks
        if str(item.get("feedback_id") or "") not in handed_feedback_ids
    )
    pending_decision_count = sum(
        1
        for item in proposal_decisions
        if str(item.get("decision_id") or "") not in handed_decision_ids
    )
    status_counts = {
        status: sum(1 for item in proposals if item.get("status") == status)
        for status in (
            "CONFIRMED",
            "UNDONE",
            "KEPT_UNRESOLVED",
            "AUTO_APPLIED_PENDING_REVIEW",
            "PROPOSED_CONTINUABLE",
            "UNRESOLVED_CONTINUABLE",
        )
    }
    can_continue = pending_feedback_count > 0 or pending_decision_count > 0
    latest_events = _latest_handoff_events(handoff_events)
    adopted_projection_attempts = _adopted_projection_attempts(handoff_attempts)
    active_handoff_id = _active_projection_handoff_id(
        handoffs=handoffs,
        adopted_attempts=adopted_projection_attempts,
    )
    latest_handoff = next(
        (
            dict(handoff)
            for handoff in handoffs
            if str(handoff.get("handoff_id") or "") == active_handoff_id
        ),
        None,
    )
    latest_attempt_receipts: tuple[NativeStructureHandoffAttemptReceiptV1, ...] = ()
    if latest_handoff is not None:
        latest_handoff_id = str(latest_handoff.get("handoff_id") or "")
        latest_event = latest_events.get(str(latest_handoff.get("handoff_id") or ""))
        if latest_event is not None:
            latest_handoff["status"] = str(
                latest_event.get("status") or latest_handoff.get("status") or ""
            )
            latest_handoff["current_action"] = str(
                latest_event.get("current_action") or ""
            )
            latest_handoff["outcome_reason_code"] = str(
                latest_event.get("reason_code") or ""
            )
            latest_handoff["evidence_state"] = str(
                latest_event.get("evidence_state") or "SUFFICIENT"
            )
            latest_handoff["decision_policy"] = str(
                latest_event.get("decision_policy") or "KEEP_CURRENT_STRUCTURE"
            )
            latest_handoff["acknowledged_at_utc"] = str(
                latest_event.get("created_at_utc") or ""
            )
        latest_attempt_receipts = tuple(
            NativeStructureHandoffAttemptReceiptV1.model_validate(item)
            for item in handoff_attempts
            if str(item.get("handoff_id") or "") == latest_handoff_id
        )
        if latest_attempt_receipts:
            latest_attempt = latest_attempt_receipts[-1]
            latest_handoff["latest_attempt"] = _public_handoff_attempt(latest_attempt)
            latest_handoff["current_action"] = latest_attempt.current_action
            latest_handoff["attempt_summary"] = {
                "attempt_count": len(latest_attempt_receipts),
                "tool_call_count": sum(
                    len(item.tool_calls) for item in latest_attempt_receipts
                ),
                "candidate_revision_count": sum(
                    len(item.candidate_revisions) for item in latest_attempt_receipts
                ),
                "tool_intents": sorted(
                    {
                        tool.intent.value
                        for item in latest_attempt_receipts
                        for tool in item.tool_calls
                    }
                ),
                "validation_status": str(
                    latest_attempt.validation_report.get("status") or ""
                ),
            }
            working_decision = latest_attempt.validation_report.get(
                "working_decision"
            )
            if isinstance(working_decision, dict):
                latest_handoff["evidence_state"] = str(
                    working_decision.get("evidence_state") or "SUFFICIENT"
                )
                latest_handoff["decision_policy"] = str(
                    working_decision.get("decision_policy")
                    or "FIRST_HOST_VALIDATED_PROPOSAL"
                )
            next_action = _attempt_next_action(
                latest_handoff,
                latest_attempt_receipts,
            )
            latest_handoff["status"] = (
                "CONSUMED"
                if next_action == "COMPLETE"
                else (
                    "REVISION_REQUIRED"
                    if next_action == "REVISE_AND_REMATERIALIZE"
                    else "VERIFYING"
                    if next_action == "REOPEN_AND_VERIFY"
                    else "IN_PROGRESS"
                )
            )
    resolved_items = {
        (item.input_kind, item.input_id)
        for attempt in latest_attempt_receipts
        if attempt.status is StructureHandoffAttemptStatus.VERIFIED
        for item in attempt.accepted_work_item_dispositions
    }
    defaulted_items = {
        (item.input_kind, item.input_id)
        for attempt in latest_attempt_receipts
        if attempt.status is StructureHandoffAttemptStatus.VERIFIED
        for item in attempt.accepted_work_item_dispositions
        if item.outcome == "KEPT_UNRESOLVED"
    }
    total_work_item_count = (
        len(latest_handoff.get("feedback_ids", []))
        + len(latest_handoff.get("proposal_decision_ids", []))
        if latest_handoff is not None
        else 0
    )
    if can_continue:
        status = "READY_TO_CONTINUE"
        message = "有新的节点指引或处理选择，可继续交给 Agent。"
    elif latest_handoff is not None:
        status = str(latest_handoff.get("status") or "QUEUED_FOR_CODEX_APP")
        if status == "CONSUMED":
            message = "Agent 已完成本轮局部修订，Host 重新物化并验证通过。"
        elif status == "VERIFYING":
            message = "局部候选已物化，Agent 正在真实前端重新打开并复验结果。"
        elif status == "REVISION_REQUIRED":
            message = "Host 复验发现局部结果仍需修正，Agent 将继续修改并重新物化。"
        elif status == "IN_PROGRESS":
            message = "Agent 已完成部分局部结果，正在继续处理其余反馈。"
        elif status == "ACKNOWLEDGED":
            message = "Agent 已读取本轮反馈，正在核对受影响内容。"
        elif status == "NO_CHANGE":
            if latest_handoff.get("evidence_state") == "SUFFICIENT":
                message = "Agent 已核对本轮反馈并明确选择当前结构，无需修改。"
            else:
                message = "Agent 已给出工作判断并保留当前结构；证据状态保持可见，不阻断下一阶段。"
        elif status == "NEEDS_GUIDANCE":
            message = "未决项已保留当前结构继续；你可以随时补充指引，不阻断下一阶段。"
        elif status == "UNRESOLVED_CONTINUED":
            message = "未决项已按非阻断默认策略继续；你可以随时补充指引或撤销默认处理。"
        else:
            status = "QUEUED_FOR_CODEX_APP"
            message = "本轮反馈已排队；等待 Agent 真实读取回执。"
    else:
        status = "IDLE"
        message = "保存节点指引或处理建议后，可继续交给 Agent。"
    terminal_unresolved = status in {"NEEDS_GUIDANCE", "UNRESOLVED_CONTINUED"}
    latest_evidence_state = str(
        latest_handoff.get("evidence_state") or "SUFFICIENT"
    ) if latest_handoff is not None else "SUFFICIENT"
    latest_decision_policy = str(
        latest_handoff.get("decision_policy") or ""
    ) if latest_handoff is not None else ""
    retained_current_with_uncertainty = (
        status == "NO_CHANGE"
        and latest_decision_policy == "KEEP_CURRENT_STRUCTURE"
        and latest_evidence_state in {"CONFLICTING", "INSUFFICIENT"}
    )
    nonblocking_unresolved_count = (
        len(defaulted_items)
        if defaulted_items
        else total_work_item_count
        if terminal_unresolved or retained_current_with_uncertainty
        else 0
    )
    default_resolution_policy = (
        "FIRST_HOST_VALIDATED_PROPOSAL"
        if defaulted_items
        else "KEEP_CURRENT_STRUCTURE"
        if terminal_unresolved
        or status == "NO_CHANGE"
        and latest_decision_policy == "KEEP_CURRENT_STRUCTURE"
        else "NONE"
    )
    advancement_allowed = status not in {
        "QUEUED_FOR_CODEX_APP",
        "ACKNOWLEDGED",
        "IN_PROGRESS",
        "VERIFYING",
        "REVISION_REQUIRED",
    }
    if latest_handoff is not None:
        latest_handoff["default_resolution_policy"] = default_resolution_policy
        latest_handoff["nonblocking_unresolved_count"] = (
            nonblocking_unresolved_count
        )
    return {
        "status": status,
        "can_continue": can_continue,
        "pending_feedback_count": pending_feedback_count,
        "pending_proposal_decision_count": pending_decision_count,
        "confirmed_proposal_count": status_counts["CONFIRMED"],
        "undone_proposal_count": status_counts["UNDONE"],
        "kept_unresolved_proposal_count": status_counts["KEPT_UNRESOLVED"],
        "review_pending_proposal_count": (
            status_counts["AUTO_APPLIED_PENDING_REVIEW"]
            + status_counts["PROPOSED_CONTINUABLE"]
            + status_counts["UNRESOLVED_CONTINUABLE"]
        ),
        "completed_work_item_count": len(resolved_items),
        "total_work_item_count": total_work_item_count,
        "advancement_allowed": advancement_allowed,
        "default_resolution_policy": default_resolution_policy,
        "evidence_state": latest_evidence_state,
        "nonblocking_unresolved_count": nonblocking_unresolved_count,
        "latest_handoff": latest_handoff,
        "message": message,
    }


def _latest_handoff_events(
    values: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        handoff_id = str(item.get("handoff_id") or "")
        if handoff_id:
            result[handoff_id] = item
    return result


def _public_feedbacks(
    feedbacks: list[dict[str, Any]],
    *,
    handoffs: list[dict[str, Any]],
    handoff_events: list[dict[str, Any]],
    handoff_attempts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_events = _latest_handoff_events(handoff_events)
    acknowledged_feedback_ids = {
        str(feedback_id)
        for handoff in handoffs
        if str(
            latest_events.get(str(handoff.get("handoff_id") or ""), {}).get("status")
            or ""
        )
        in {
            "ACKNOWLEDGED",
            "NO_CHANGE",
            "NEEDS_GUIDANCE",
            "UNRESOLVED_CONTINUED",
        }
        for feedback_id in handoff.get("feedback_ids", [])
        if str(feedback_id)
    }
    consumed_feedback_outcomes: dict[str, str] = {}
    for raw_attempt in handoff_attempts:
        attempt = NativeStructureHandoffAttemptReceiptV1.model_validate(raw_attempt)
        if attempt.status is not StructureHandoffAttemptStatus.VERIFIED:
            continue
        for disposition in attempt.accepted_work_item_dispositions:
            if disposition.input_kind == "FEEDBACK":
                consumed_feedback_outcomes[disposition.input_id] = disposition.outcome
    return [
        {
            **item,
            "status": (
                "REJECTED"
                if consumed_feedback_outcomes.get(str(item.get("feedback_id") or ""))
                == "REJECTED"
                else "CONSUMED"
                if str(item.get("feedback_id") or "") in consumed_feedback_outcomes
                else "ACKNOWLEDGED"
                if str(item.get("feedback_id") or "") in acknowledged_feedback_ids
                else str(item.get("status") or "PENDING_CODEX_APP")
            ),
        }
        for item in feedbacks
    ]


def _filter_reverted_image_anchors(
    anchors: list[dict[str, Any]],
    *,
    proposal_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest = _latest_proposal_decisions(proposal_decisions).values()
    reverted_objects = {
        str(item.get("object_id") or "")
        for item in latest
        if str(item.get("action") or "") in {"UNDO", "KEEP_UNRESOLVED"}
    }
    return [
        item
        for item in anchors
        if not (
            item.get("anchor_kind") == "IMAGE"
            and str(item.get("placement_id") or "") in reverted_objects
        )
    ]


def _require_current_proposal(
    evidence: dict[str, Any] | None,
    proposal_id: str,
    *,
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(evidence, dict) and not isinstance(overrides, dict):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_proposal_invalid",
                "message": "The selected structure proposal is not current.",
            },
        )
    collections = []
    if isinstance(evidence, dict):
        collections.append(evidence.get("proposals", []))
    if isinstance(overrides, dict):
        collections.append(overrides.get("proposals", []))
    for collection in collections:
        for item in collection if isinstance(collection, list) else []:
            if isinstance(item, dict) and item.get("proposal_id") == proposal_id:
                return item
    raise ApplicationError(
        status_code=422,
        detail={
            "code": "native_structure_proposal_invalid",
            "message": "The selected structure proposal is not current.",
        },
    )


def _fallback_document_tree(
    artifact: dict[str, Any],
    *,
    logical_tables: list[dict[str, Any]],
) -> dict[str, Any]:
    tables_by_owner: dict[str, list[dict[str, str]]] = {}
    for table in logical_tables:
        owner_id = str(table.get("owner_unit_id") or "")
        tables_by_owner.setdefault(owner_id, []).append(
            {
                "kind": "LOGICAL_TABLE",
                "logical_table_scope_id": str(table.get("scope_id") or ""),
            }
        )
    nodes = []
    for order, unit in enumerate(artifact.get("page_ownership", [])):
        unit_id = str(unit.get("unit_id") or "")
        nodes.append(
            {
                "node_id": unit_id,
                "parent_id": None,
                "order": order,
                "kind": "DOCUMENT_UNIT",
                "label": "",
                "title": str(unit.get("title") or ""),
                "title_authority": str(unit.get("title_authority") or "VISIBLE_SOURCE"),
                "pages": list(unit.get("pages") or []),
                "content_blocks": tables_by_owner.get(unit_id, []),
                "finding_codes": [],
                "source_anchors": [],
                "source_content": [],
            }
        )
    return {
        "schema_version": "native-document-structure-tree/v1",
        "root_ids": [item["node_id"] for item in nodes],
        "nodes": nodes,
    }


def _require_feedback_scope(
    artifact: dict[str, Any],
    scope_kind: str,
    scope_id: str,
    *,
    document_tree: dict[str, Any] | None,
) -> None:
    allowed: dict[str, set[str]] = {
        "DOCUMENT": {"document"},
        "DOCUMENT_UNIT": {
            str(item.get("unit_id") or "")
            for item in artifact.get("page_ownership", [])
        },
        "LOGICAL_TABLE": {
            str(item.get("scope_id") or "")
            for item in artifact.get("logical_tables", [])
        },
        "UNRESOLVED": {
            str(item.get("code") or "") for item in artifact.get("unresolved", [])
        },
        "STRUCTURE_NODE": {
            str(item.get("node_id") or "")
            for item in _document_tree_nodes(document_tree)
        },
    }
    if scope_id not in allowed.get(scope_kind, set()):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_feedback_scope_invalid",
                "message": "The selected native structure scope is not current.",
            },
        )


def _append_feedback(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    payload: NativeStructureFeedbackRequestV1,
) -> dict[str, Any]:
    path = _feedback_path(source_sha256, artifact_id, create=True)
    identity = {
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "command_id": payload.command_id,
        "scope_kind": payload.scope_kind,
        "scope_id": payload.scope_id,
        "message": payload.message,
    }
    digest = _canonical_sha256(identity)
    record = {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "feedback_id": f"native-structure-feedback-{digest[:32]}",
        **identity,
        "status": "PENDING_CODEX_APP",
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    encoded = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    with _FEEDBACK_LOCK:
        existing = _read_jsonl_file(path, root=path.parent, required=False)
        same_command = [
            item for item in existing if item.get("command_id") == payload.command_id
        ]
        if same_command:
            if same_command[-1].get("feedback_id") != record["feedback_id"]:
                raise _conflict(
                    "native_structure_feedback_command_conflict",
                    "The feedback command ID was already used with different content.",
                )
            return same_command[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise _reference_invalid(
                "native structure feedback could not be saved"
            ) from exc
    return record


def _read_feedbacks(
    *,
    source_sha256: str,
    artifact_id: str,
) -> list[dict[str, Any]]:
    path = _feedback_path(source_sha256, artifact_id, create=False)
    if not path.is_file():
        return []
    return _read_jsonl_file(path, root=path.parent, required=False)


def _append_proposal_decision(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    proposal: dict[str, Any],
    payload: NativeStructureProposalDecisionRequestV1,
) -> dict[str, Any]:
    path = _proposal_decision_path(source_sha256, artifact_id, create=True)
    identity = {
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "command_id": payload.command_id,
        "proposal_id": payload.proposal_id,
        "object_id": str(proposal.get("object_id") or ""),
        "node_id": str(proposal.get("node_id") or ""),
        "action": payload.action,
    }
    digest = _canonical_sha256(identity)
    record = {
        "schema_version": PROPOSAL_DECISION_SCHEMA_VERSION,
        "decision_id": f"native-structure-proposal-decision-{digest[:32]}",
        **identity,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    encoded = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    with _FEEDBACK_LOCK:
        existing = _read_jsonl_file(path, root=path.parent, required=False)
        same_command = [
            item for item in existing if item.get("command_id") == payload.command_id
        ]
        if same_command:
            if same_command[-1].get("decision_id") != record["decision_id"]:
                raise _conflict(
                    "native_structure_proposal_decision_command_conflict",
                    "The proposal decision command ID was already used with different content.",
                )
            return same_command[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise _reference_invalid(
                "native structure proposal decision could not be saved"
            ) from exc
    return record


def _read_proposal_decisions(
    *,
    source_sha256: str,
    artifact_id: str,
) -> list[dict[str, Any]]:
    path = _proposal_decision_path(source_sha256, artifact_id, create=False)
    if not path.is_file():
        return []
    return _read_jsonl_file(path, root=path.parent, required=False)


def _append_handoff(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    payload: NativeStructureContinueRequestV1,
    feedbacks: list[dict[str, Any]],
    proposal_decisions: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    path = _handoff_path(source_sha256, artifact_id, create=True)
    proposal_by_id = {
        str(item.get("proposal_id") or ""): item
        for item in proposals
        if str(item.get("proposal_id") or "")
    }
    feedback_items = [
        {
            "feedback_id": str(item.get("feedback_id") or ""),
            "scope_kind": str(item.get("scope_kind") or ""),
            "scope_id": str(item.get("scope_id") or ""),
            "message": str(item.get("message") or ""),
        }
        for item in feedbacks
    ]
    proposal_decision_items = []
    for item in proposal_decisions:
        proposal_id = str(item.get("proposal_id") or "")
        proposal = proposal_by_id.get(proposal_id, {})
        proposal_decision_items.append(
            {
                "decision_id": str(item.get("decision_id") or ""),
                "proposal_id": proposal_id,
                "node_id": str(item.get("node_id") or ""),
                "action": str(item.get("action") or ""),
                "recommended_action": str(proposal.get("recommended_action") or ""),
                "proposal_is_current": bool(proposal),
            }
        )
    affected_node_ids = sorted(
        {
            str(item.get("scope_id") or "")
            for item in feedbacks
            if item.get("scope_kind") == "STRUCTURE_NODE"
            and str(item.get("scope_id") or "")
        }
        | {
            str(item.get("node_id") or "")
            for item in proposal_decisions
            if str(item.get("node_id") or "")
        }
    )
    decision_counts = {
        action: sum(
            1 for item in proposal_decisions if str(item.get("action") or "") == action
        )
        for action in ("CONFIRM", "UNDO", "KEEP_UNRESOLVED")
    }
    feedback_ids = [item["feedback_id"] for item in feedback_items]
    decision_ids = [item["decision_id"] for item in proposal_decision_items]
    input_fingerprint = _canonical_sha256(
        {
            "feedback_ids": feedback_ids,
            "proposal_decision_ids": decision_ids,
        }
    )
    identity = {
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "command_id": payload.command_id,
        "input_fingerprint": input_fingerprint,
    }
    digest = _canonical_sha256(identity)
    record = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "handoff_id": f"native-structure-agent-handoff-{digest[:32]}",
        **identity,
        "status": "QUEUED_FOR_CODEX_APP",
        "feedback_ids": feedback_ids,
        "proposal_decision_ids": decision_ids,
        "feedback_items": feedback_items,
        "proposal_decision_items": proposal_decision_items,
        "affected_node_ids": affected_node_ids,
        "decision_counts": decision_counts,
        "requires_scoped_structure_revision": bool(feedback_items),
        "requires_scoped_evidence_recovery": any(
            item["action"] == "CONFIRM"
            and item["recommended_action"] == "RUN_SCOPED_VISUAL_CELL_RECOVERY"
            for item in proposal_decision_items
        ),
        "preserve_unaffected_nodes": True,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    encoded = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    with _FEEDBACK_LOCK:
        existing = _read_jsonl_file(path, root=path.parent, required=False)
        same_command = [
            item for item in existing if item.get("command_id") == payload.command_id
        ]
        if same_command:
            if same_command[-1].get("input_fingerprint") != input_fingerprint:
                raise _conflict(
                    "native_structure_continue_command_conflict",
                    "The continue command ID was already used with different inputs.",
                )
            return same_command[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise _reference_invalid(
                "native structure Agent handoff could not be queued"
            ) from exc
    return record


def _read_handoffs(
    *,
    source_sha256: str,
    artifact_id: str,
) -> list[dict[str, Any]]:
    path = _handoff_path(source_sha256, artifact_id, create=False)
    if not path.is_file():
        return []
    return _read_jsonl_file(path, root=path.parent, required=False)


def _append_handoff_event(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    handoff: dict[str, Any],
    payload: NativeStructureHandoffAckRequestV1,
) -> dict[str, Any]:
    path = _handoff_event_path(source_sha256, artifact_id, create=True)
    identity = {
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "command_id": payload.command_id,
        "handoff_id": payload.handoff_id,
        "status": "ACKNOWLEDGED",
        "current_action": payload.current_action,
    }
    digest = _canonical_sha256(identity)
    record = {
        "schema_version": HANDOFF_EVENT_SCHEMA_VERSION,
        "event_id": f"native-structure-agent-handoff-event-{digest[:32]}",
        **identity,
        "feedback_ids": list(handoff.get("feedback_ids", [])),
        "affected_node_ids": list(handoff.get("affected_node_ids", [])),
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    encoded = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    with _FEEDBACK_LOCK:
        existing = _read_jsonl_file(path, root=path.parent, required=False)
        same_command = [
            item for item in existing if item.get("command_id") == payload.command_id
        ]
        if same_command:
            if same_command[-1].get("event_id") != record["event_id"]:
                raise _conflict(
                    "native_structure_handoff_ack_command_conflict",
                    "The acknowledgement command ID was already used with different content.",
                )
            return same_command[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise _reference_invalid(
                "native structure Agent acknowledgement could not be saved"
            ) from exc
    return record


def _append_handoff_outcome_event(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    handoff: dict[str, Any],
    execution_status: Literal["NO_CHANGE"],
    reason_code: str,
    evidence_state: Literal["SUFFICIENT", "CONFLICTING", "INSUFFICIENT"],
    decision_policy: Literal["KEEP_CURRENT_STRUCTURE"],
    public_summary: str,
) -> dict[str, Any]:
    path = _handoff_event_path(source_sha256, artifact_id, create=True)
    handoff_id = str(handoff["handoff_id"])
    visible_status = "NO_CHANGE"
    command_id = f"agent-outcome:{handoff_id}:{execution_status.lower()}"
    identity = {
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "command_id": command_id,
        "handoff_id": handoff_id,
        "status": visible_status,
        "execution_status": execution_status,
        "reason_code": reason_code,
        "evidence_state": evidence_state,
        "decision_policy": decision_policy,
        "current_action": public_summary,
    }
    digest = _canonical_sha256(identity)
    record = {
        "schema_version": HANDOFF_EVENT_SCHEMA_VERSION,
        "event_id": f"native-structure-agent-handoff-event-{digest[:32]}",
        **identity,
        "feedback_ids": list(handoff.get("feedback_ids", [])),
        "affected_node_ids": list(handoff.get("affected_node_ids", [])),
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    encoded = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    with _FEEDBACK_LOCK:
        existing = _read_jsonl_file(path, root=path.parent, required=False)
        same_command = [
            item for item in existing if item.get("command_id") == command_id
        ]
        if same_command:
            if same_command[-1].get("event_id") != record["event_id"]:
                raise _conflict(
                    "native_structure_handoff_outcome_conflict",
                    "The Agent outcome changed after it was recorded.",
                )
            return same_command[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise _reference_invalid(
                "native structure Agent outcome could not be saved"
            ) from exc
    return record


def _read_handoff_events(
    *,
    source_sha256: str,
    artifact_id: str,
) -> list[dict[str, Any]]:
    path = _handoff_event_path(source_sha256, artifact_id, create=False)
    if not path.is_file():
        return []
    return _read_jsonl_file(path, root=path.parent, required=False)


def _append_handoff_attempt(
    *,
    source_sha256: str,
    artifact_id: str,
    receipt: NativeStructureHandoffAttemptReceiptV1,
) -> NativeStructureHandoffAttemptReceiptV1:
    path = _handoff_attempt_path(source_sha256, artifact_id, create=True)
    encoded = (
        json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    with _FEEDBACK_LOCK:
        existing = _read_jsonl_file(path, root=path.parent, required=False)
        same_command = [
            NativeStructureHandoffAttemptReceiptV1.model_validate(item)
            for item in existing
            if item.get("command_id") == receipt.command_id
        ]
        if same_command:
            if same_command[-1].attempt_id != receipt.attempt_id:
                raise _conflict(
                    "native_structure_handoff_attempt_command_conflict",
                    "The attempt command ID was already used with different content.",
                )
            return same_command[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise _reference_invalid(
                "native structure Agent attempt could not be saved"
            ) from exc
    return receipt


def _read_all_handoff_attempts(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
) -> list[dict[str, Any]]:
    merged = {
        str(item.get("attempt_id") or ""): dict(item)
        for item in _read_handoff_attempts(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        )
        if str(item.get("attempt_id") or "")
    }
    repository = _structure_loop_repository()
    if repository is not None:
        for receipt in repository.list_attempts(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ):
            payload = receipt.model_dump(mode="json")
            existing = merged.get(receipt.attempt_id)
            if existing is not None and existing != payload:
                raise _reference_invalid(
                    "native structure durable attempt conflicts with development history"
                )
            merged[receipt.attempt_id] = payload
    return sorted(
        merged.values(),
        key=_handoff_attempt_global_order,
    )


def _handoff_attempt_global_order(
    raw: dict[str, Any],
) -> tuple[datetime, str, int, str]:
    attempt = NativeStructureHandoffAttemptReceiptV1.model_validate(raw)
    created_at = attempt.created_at_utc
    if created_at.tzinfo is None:
        raise _reference_invalid("native structure attempt timestamp is not UTC-bound")
    return (
        created_at.astimezone(UTC),
        attempt.handoff_id,
        attempt.attempt_ordinal,
        attempt.attempt_id,
    )


def _persist_handoff_attempt(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    request: NativeStructureHandoffAttemptRequestV1,
    receipt: NativeStructureHandoffAttemptReceiptV1,
    expected_global_head_attempt_id: str | None = None,
    expected_global_projection_attempts_sha256: str | None = None,
    structure_loop_repository: Any | None = None,
) -> NativeStructureHandoffAttemptReceiptV1:
    repository = (
        structure_loop_repository
        if structure_loop_repository is not None
        else _structure_loop_repository()
    )
    if repository is None:
        return _append_handoff_attempt(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
            receipt=receipt,
        )
    from reqpilotx_server.ports.agent_structure_loop_repository import AgentStructureLoopConflict

    try:
        persisted, _created = repository.append_attempt(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=artifact_id,
            request=request,
            receipt=receipt,
            expected_global_head_attempt_id=expected_global_head_attempt_id,
            expected_global_projection_attempts_sha256=(
                expected_global_projection_attempts_sha256
            ),
        )
    except AgentStructureLoopConflict as exc:
        raise _conflict(
            "native_structure_handoff_attempt_persistence_conflict",
            "The durable Agent attempt no longer matches the current stream.",
        ) from exc
    return persisted


def _record_attempt_activity(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    receipt: NativeStructureHandoffAttemptReceiptV1,
) -> None:
    repository = _structure_loop_repository()
    if repository is None:
        return
    scope_kind = "STRUCTURE_NODE" if len(receipt.affected_scope_ids) == 1 else "HANDOFF"
    scope_ref = (
        receipt.affected_scope_ids[0]
        if len(receipt.affected_scope_ids) == 1
        else receipt.handoff_id
    )
    common = {
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "handoff_id": receipt.handoff_id,
    }
    repository.append_activity(
        **common,
        command_id=f"activity:{receipt.attempt_id}:revision",
        draft=AgentStructureLoopActivityDraftV1(
            actor=AgentActivityActor.AGENT,
            family=AgentActivityFamily.ARTIFACT,
            kind="STRUCTURE_CANDIDATE_REVISION_PUBLISHED",
            title="Agent 已提交局部修订",
            summary=receipt.public_summary,
            scope_kind=scope_kind,
            scope_ref_id=scope_ref,
            status=receipt.status.value,
            authorized_parameters={
                "attempt_ordinal": receipt.attempt_ordinal,
                "affected_scope_count": len(receipt.affected_scope_ids),
                "candidate_revision_count": len(receipt.candidate_revisions),
                "tool_call_count": len(receipt.tool_calls),
            },
            artifact_refs=(receipt.attempt_id, receipt.receipt_sha256),
            correlation_id=receipt.handoff_id,
            occurred_at_utc=receipt.created_at_utc,
        ),
    )
    validation_status = str(receipt.validation_report.get("status") or "")
    if receipt.status is StructureHandoffAttemptStatus.VERIFIED:
        kind = "STRUCTURE_MATERIALIZATION_VERIFIED"
        title = "Host 已验证本轮修改"
        summary = "来源、物化结果和重新打开回执均已通过，本轮条目可以消费。"
        visibility = AgentActivityVisibility.PRIMARY
    elif receipt.status is StructureHandoffAttemptStatus.VERIFICATION_REQUIRED:
        kind = "STRUCTURE_MATERIALIZATION_REOPEN_REQUIRED"
        title = "正在等待渲染复验"
        summary = "局部修订已经物化；重新打开对应范围并通过 Host 检查后才会完成。"
        visibility = AgentActivityVisibility.PRIMARY
    else:
        kind = "STRUCTURE_MATERIALIZATION_REVISION_REQUIRED"
        title = "Host 要求继续修正"
        summary = "当前物化结果未通过后置检查，Agent 将只修改受影响范围。"
        visibility = AgentActivityVisibility.PRIMARY
    repository.append_activity(
        **common,
        command_id=f"activity:{receipt.attempt_id}:validation",
        draft=AgentStructureLoopActivityDraftV1(
            actor=AgentActivityActor.VALIDATOR,
            family=AgentActivityFamily.DECISION,
            kind=kind,
            title=title,
            summary=summary,
            scope_kind=scope_kind,
            scope_ref_id=scope_ref,
            visibility=visibility,
            status=receipt.status.value,
            outcome_code=validation_status,
            authorized_parameters={
                "accepted_work_item_count": len(
                    receipt.accepted_work_item_dispositions
                ),
                "selected_pattern_count": len(
                    receipt.selected_failure_patterns.get("selected_patterns", [])
                ),
            },
            artifact_refs=(receipt.attempt_id, receipt.receipt_sha256),
            correlation_id=receipt.handoff_id,
            causation_id=receipt.attempt_id,
            occurred_at_utc=receipt.created_at_utc,
        ),
    )


def _record_host_tool_activity(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    receipt: NativeStructureHostToolReceiptV1,
) -> None:
    call = receipt.tool_call
    succeeded = call.outcome is StructureToolOutcome.SUCCEEDED
    _append_structure_loop_activity(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff_id=receipt.handoff_id,
        command_id=f"activity:{receipt.receipt_id}:tool",
        draft=AgentStructureLoopActivityDraftV1(
            actor=AgentActivityActor.TOOL,
            family=AgentActivityFamily.TOOL,
            kind="STRUCTURE_HOST_TOOL_COMPLETED",
            title="Host 已完成局部工具调用" if succeeded else "局部工具未能完成",
            summary=(
                "工具结果已绑定当前来源和范围，Agent 将检查后决定是否采用或修改。"
                if succeeded
                else "当前工具没有产生可用结果，Agent 可继续使用现有证据或切换策略。"
            ),
            scope_kind="STRUCTURE_NODE",
            scope_ref_id=receipt.scope_id,
            status=call.outcome.value,
            outcome_code=call.failure_code,
            authorized_parameters={
                "tool_intent": call.intent.value,
                "result_role": (
                    call.result_role.value if call.result_role is not None else ""
                ),
                "reason_signals": list(call.reason_signals),
            },
            evidence_refs=(receipt.receipt_id, receipt.receipt_sha256),
            correlation_id=receipt.handoff_id,
            occurred_at_utc=receipt.created_at_utc,
        ),
    )


def _list_structure_loop_activity(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    handoff_id: str | None,
    after_sequence: int,
):  # type: ignore[no-untyped-def]
    repository = _structure_loop_repository()
    if repository is None or handoff_id is None:
        return ()
    return repository.list_activity(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff_id=handoff_id,
        after_sequence=after_sequence,
    )


def _structure_loop_repository():  # type: ignore[no-untyped-def]
    try:
        from reqpilotx_server.application.runtime import _agent_structure_loop

        return _agent_structure_loop()
    except RuntimeError:
        return None


def _append_structure_loop_activity(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    handoff_id: str,
    command_id: str,
    draft: AgentStructureLoopActivityDraftV1,
) -> None:
    repository = _structure_loop_repository()
    if repository is None:
        return
    repository.append_activity(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff_id=handoff_id,
        command_id=command_id,
        draft=draft,
    )


def _activity_time(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _execute_native_structure_host_tool(
    bundle: dict[str, Any],
    request: NativeStructureHostToolRequestV1,
) -> dict[str, Any] | None:
    artifact = bundle["artifact"]
    logical_tables = [
        {"scope_id": str(item.get("scope_id") or "")}
        for item in artifact.get("logical_tables", [])
        if str(item.get("scope_id") or "")
    ]
    tree = _public_document_tree(
        bundle.get("document_tree"),
        evidence=bundle.get("document_tree_evidence"),
        proposal_decisions=[],
        artifact=artifact,
        logical_tables=logical_tables,
        unresolved=[dict(item) for item in artifact.get("unresolved", [])],
    )
    node = next(
        (
            item
            for item in tree.get("nodes", [])
            if str(item.get("node_id") or "") == request.scope_id
        ),
        None,
    )
    if node is None:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_host_tool_scope_invalid",
                "message": "The Host tool scope is not present in the current tree.",
            },
        )
    if request.intent is StructureToolIntent.READ_NATIVE_TEXT:
        return {
            "schema_version": "native-structure-native-text-result/v1",
            "scope_id": request.scope_id,
            "source_content": list(node.get("source_content", [])),
        }
    if request.intent is StructureToolIntent.RENDER_SCOPE:
        pages = {int(item) for item in node.get("pages", [])}
        return {
            "schema_version": "native-structure-rendered-scope-result/v1",
            "scope_id": request.scope_id,
            "source_anchors": list(node.get("source_anchors", [])),
            "opened_render_receipts": [
                dict(item)
                for item in artifact.get("visual_receipts", [])
                if int(item.get("page") or 0) in pages
            ],
        }
    if request.intent not in {
        StructureToolIntent.INSPECT_LOGICAL_TABLE,
        StructureToolIntent.RESTORE_TABLE_CANDIDATE,
    }:
        return None
    attached_table_ids = [
        str(item.get("logical_table_scope_id") or "")
        for item in node.get("content_blocks", [])
        if item.get("kind") == "LOGICAL_TABLE"
        and str(item.get("logical_table_scope_id") or "")
    ]
    table_scope_id = request.logical_table_scope_id
    if table_scope_id is None:
        if len(attached_table_ids) != 1:
            raise ApplicationError(
                status_code=422,
                detail={
                    "code": "native_structure_host_tool_table_scope_required",
                    "message": (
                        "Select one table scope when the structure node has zero "
                        "or multiple direct tables."
                    ),
                },
            )
        table_scope_id = attached_table_ids[0]
    if table_scope_id not in set(attached_table_ids):
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "native_structure_host_tool_table_scope_invalid",
                "message": "The selected table is not direct content of this scope.",
            },
        )
    fragments = _public_table_preview_fragments(
        bundle.get("document_tree_evidence"),
        scope_id=table_scope_id,
        artifact=artifact,
    )
    if request.intent is StructureToolIntent.INSPECT_LOGICAL_TABLE:
        return {
            "schema_version": "native-structure-logical-table-inspection/v1",
            "scope_id": request.scope_id,
            "logical_table_scope_id": table_scope_id,
            "fragments": fragments,
        }
    return {
        "schema_version": "native-structure-table-restoration-candidate/v1",
        "scope_id": request.scope_id,
        "logical_table_scope_id": table_scope_id,
        "authority": "ADVISORY_CANDIDATE",
        "editable": True,
        "source_fragments": fragments,
    }


def _append_host_tool_receipt(
    *,
    source_sha256: str,
    artifact_id: str,
    receipt: NativeStructureHostToolReceiptV1,
) -> NativeStructureHostToolReceiptV1:
    path = _host_tool_receipt_path(source_sha256, artifact_id, create=True)
    encoded = (
        json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    with _FEEDBACK_LOCK:
        existing = _read_jsonl_file(path, root=path.parent, required=False)
        same_command = [
            NativeStructureHostToolReceiptV1.model_validate(item)
            for item in existing
            if item.get("command_id") == receipt.command_id
        ]
        if same_command:
            if same_command[-1].receipt_id != receipt.receipt_id:
                raise _conflict(
                    "native_structure_host_tool_command_conflict",
                    "The Host tool command ID was already used with different content.",
                )
            return same_command[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise _reference_invalid(
                "native structure Host tool receipt could not be saved"
            ) from exc
    return receipt


def _read_host_tool_receipts(
    *,
    source_sha256: str,
    artifact_id: str,
) -> list[dict[str, Any]]:
    path = _host_tool_receipt_path(source_sha256, artifact_id, create=False)
    if not path.is_file():
        return []
    return _read_jsonl_file(path, root=path.parent, required=False)


def _read_handoff_attempts(
    *,
    source_sha256: str,
    artifact_id: str,
) -> list[dict[str, Any]]:
    path = _handoff_attempt_path(source_sha256, artifact_id, create=False)
    if not path.is_file():
        return []
    return _read_jsonl_file(path, root=path.parent, required=False)


def _public_handoff_attempt(
    receipt: NativeStructureHandoffAttemptReceiptV1,
) -> dict[str, Any]:
    selection = receipt.selected_failure_patterns
    scope_progress = receipt.validation_report.get("rendered_scope_progress")
    scope_progress = scope_progress if isinstance(scope_progress, dict) else {}
    working_decision = receipt.validation_report.get("working_decision")
    working_decision = (
        working_decision if isinstance(working_decision, dict) else {}
    )
    return {
        "schema_version": receipt.schema_version,
        "attempt_id": receipt.attempt_id,
        "attempt_ordinal": receipt.attempt_ordinal,
        "status": receipt.status.value,
        "affected_scope_ids": list(receipt.affected_scope_ids),
        "tool_calls": [
            {
                "call_id": item.call_id,
                "intent": item.intent.value,
                "outcome": item.outcome.value,
                "result_role": (
                    item.result_role.value if item.result_role is not None else None
                ),
            }
            for item in receipt.tool_calls
        ],
        "candidate_revision_count": len(receipt.candidate_revisions),
        "rendered_output_sha256": receipt.rendered_output_sha256,
        "rendered_reopened_scope_ids": [
            str(item) for item in scope_progress.get("verified_scope_ids", [])
        ],
        "pending_rendered_scope_ids": [
            str(item) for item in scope_progress.get("pending_scope_ids", [])
        ],
        "validation_status": str(receipt.validation_report.get("status") or ""),
        "selected_pattern_ids": [
            str(item.get("pattern_id") or "")
            for item in selection.get("selected_patterns", [])
            if isinstance(item, dict) and str(item.get("pattern_id") or "")
        ],
        "accepted_work_item_count": len(receipt.accepted_work_item_dispositions),
        "default_applied_work_item_count": sum(
            1
            for item in receipt.accepted_work_item_dispositions
            if item.outcome == "KEPT_UNRESOLVED"
        ),
        "evidence_state": str(
            working_decision.get("evidence_state") or "SUFFICIENT"
        ),
        "decision_policy": str(
            working_decision.get("decision_policy")
            or "FIRST_HOST_VALIDATED_PROPOSAL"
        ),
        "current_action": receipt.current_action,
        "public_summary": receipt.public_summary,
        "kit_version": receipt.kit_version,
        "kit_bundle_sha256": receipt.kit_bundle_sha256,
        "created_at_utc": receipt.created_at_utc.isoformat(),
    }


def _rendered_scope_progress(
    bundle: dict[str, Any],
    *,
    affected_scope_ids: tuple[str, ...],
    confirmed_actions: tuple[Any, ...],
    candidate_revisions: tuple[Any, ...],
) -> dict[str, list[str]]:
    action_scopes = {
        str(item.revision_id): str(item.target_scope_id)
        for item in candidate_revisions
    }
    actions_by_scope: dict[str, list[Any]] = {
        scope_id: [] for scope_id in affected_scope_ids
    }
    for action in confirmed_actions:
        scope_id = action_scopes.get(str(action.action_id))
        if scope_id in actions_by_scope:
            actions_by_scope[scope_id].append(action)
    ordered_scopes = _source_ordered_scope_ids(bundle, affected_scope_ids)
    verified_scope_ids = [
        scope_id
        for scope_id in ordered_scopes
        if actions_by_scope[scope_id]
        and all(
            item.rendered_output_reopened and item.verification_receipt_id
            for item in actions_by_scope[scope_id]
        )
    ]
    return {
        "verified_scope_ids": verified_scope_ids,
        "pending_scope_ids": [
            scope_id
            for scope_id in ordered_scopes
            if scope_id not in set(verified_scope_ids)
        ],
    }


def _source_ordered_scope_ids(
    bundle: dict[str, Any],
    scope_ids: tuple[str, ...],
) -> list[str]:
    node_order = {
        str(item.get("node_id") or ""): index
        for index, item in enumerate(
            _document_tree_nodes(bundle.get("document_tree"))
        )
    }
    return sorted(
        scope_ids,
        key=lambda scope_id: (node_order.get(scope_id, len(node_order)), scope_id),
    )


def _attempt_next_action(
    handoff: dict[str, Any],
    attempts: tuple[NativeStructureHandoffAttemptReceiptV1, ...],
) -> str:
    if not attempts:
        return "START_SCOPED_REVIEW"
    latest = attempts[-1]
    if latest.status is StructureHandoffAttemptStatus.REVISION_REQUIRED:
        return "REVISE_AND_REMATERIALIZE"
    if latest.status is StructureHandoffAttemptStatus.VERIFICATION_REQUIRED:
        return "REOPEN_AND_VERIFY"
    resolved = {
        (item.input_kind, item.input_id)
        for attempt in attempts
        if attempt.status is StructureHandoffAttemptStatus.VERIFIED
        for item in attempt.accepted_work_item_dispositions
    }
    expected = {
        *(("FEEDBACK", str(item)) for item in handoff.get("feedback_ids", [])),
        *(
            ("PROPOSAL_DECISION", str(item))
            for item in handoff.get("proposal_decision_ids", [])
        ),
    }
    return "COMPLETE" if expected.issubset(resolved) else "CONTINUE_REMAINING_ITEMS"


def _feedback_path(
    source_sha256: str,
    artifact_id: str,
    *,
    create: bool,
) -> Path:
    if not _SHA256_PATTERN.fullmatch(source_sha256) or not _SAFE_ID_PATTERN.fullmatch(
        artifact_id
    ):
        raise _reference_invalid("native structure feedback identity is invalid")
    root = FEEDBACK_ROOT
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise _reference_invalid("native structure feedback root is unsafe")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
    source_root = root / source_sha256
    if source_root.exists() and (source_root.is_symlink() or not source_root.is_dir()):
        raise _reference_invalid("native structure feedback source root is unsafe")
    if create:
        source_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        os.chmod(source_root, 0o700)
    return source_root / f"{artifact_id}.jsonl"


def _proposal_decision_path(
    source_sha256: str,
    artifact_id: str,
    *,
    create: bool,
) -> Path:
    feedback_path = _feedback_path(
        source_sha256,
        artifact_id,
        create=create,
    )
    return feedback_path.with_name(f"{artifact_id}.proposal-decisions.jsonl")


def _handoff_path(
    source_sha256: str,
    artifact_id: str,
    *,
    create: bool,
) -> Path:
    feedback_path = _feedback_path(
        source_sha256,
        artifact_id,
        create=create,
    )
    return feedback_path.with_name(f"{artifact_id}.agent-handoffs.jsonl")


def _handoff_event_path(
    source_sha256: str,
    artifact_id: str,
    *,
    create: bool,
) -> Path:
    feedback_path = _feedback_path(
        source_sha256,
        artifact_id,
        create=create,
    )
    return feedback_path.with_name(f"{artifact_id}.agent-handoff-events.jsonl")


def _handoff_attempt_path(
    source_sha256: str,
    artifact_id: str,
    *,
    create: bool,
) -> Path:
    feedback_path = _feedback_path(
        source_sha256,
        artifact_id,
        create=create,
    )
    return feedback_path.with_name(f"{artifact_id}.agent-handoff-attempts.jsonl")


def _host_tool_receipt_path(
    source_sha256: str,
    artifact_id: str,
    *,
    create: bool,
) -> Path:
    feedback_path = _feedback_path(
        source_sha256,
        artifact_id,
        create=create,
    )
    return feedback_path.with_name(f"{artifact_id}.host-tool-receipts.jsonl")


def _verify_manifest_files(run_root: Path, manifest: dict[str, Any]) -> None:
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise _reference_invalid("native structure manifest has no files")
    for item in files:
        if not isinstance(item, dict):
            raise _reference_invalid("native structure manifest file entry is invalid")
        relative = Path(str(item.get("path") or ""))
        expected = str(item.get("sha256") or "")
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or not _SHA256_PATTERN.fullmatch(expected)
        ):
            raise _reference_invalid("native structure manifest path is invalid")
        path = run_root / relative
        resolved = _resolved_safe_file(path, root=run_root)
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != expected:
            raise _reference_invalid("native structure manifest hash drifted")


def _read_json_file(path: Path, *, root: Path) -> dict[str, Any]:
    resolved = _resolved_safe_file(path, root=root)
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _reference_invalid("native structure JSON is invalid") from exc
    if not isinstance(value, dict):
        raise _reference_invalid("native structure JSON root is invalid")
    return value


def _read_jsonl_file(
    path: Path,
    *,
    root: Path,
    required: bool = True,
) -> list[dict[str, Any]]:
    if not required and not path.exists():
        return []
    resolved = _resolved_safe_file(path, root=root)
    result: list[dict[str, Any]] = []
    try:
        for line in resolved.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("JSONL item is not an object")
            result.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _reference_invalid("native structure JSONL is invalid") from exc
    return result


def _resolved_safe_directory(path: Path, *, required: bool) -> Path | None:
    if not path.exists():
        if required:
            raise _reference_invalid("native structure reference root is missing")
        return None
    if path.is_symlink() or not path.is_dir():
        raise _reference_invalid("native structure reference root is unsafe")
    return path.resolve(strict=True)


def _resolved_safe_file(path: Path, *, root: Path) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (FileNotFoundError, ValueError) as exc:
        raise _reference_invalid("native structure reference path is invalid") from exc
    if path.is_symlink() or not resolved.is_file():
        raise _reference_invalid("native structure reference file is unsafe")
    if resolved.stat().st_size > _MAX_REFERENCE_FILE_BYTES:
        raise _reference_invalid("native structure reference file is too large")
    return resolved


def _document_source_sha256(document: dict[str, Any]) -> str:
    value = str(document.get("source_sha256") or "").strip().lower()
    if not _SHA256_PATTERN.fullmatch(value):
        raise _reference_invalid("SOR document source identity is invalid")
    return value


def _require_project_document(project_id: str, document_id: str) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.review_support import _require_project_document

    return _require_project_document(project_id, document_id)


def _canonical_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _no_store_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def _not_found() -> ApplicationError:
    return ApplicationError(
        status_code=404,
        detail={
            "code": "native_structure_reference_not_found",
            "message": "No native Codex App structure candidate is available for this document.",
        },
    )


def _reference_invalid(message: str) -> ApplicationError:
    return ApplicationError(
        status_code=500,
        detail={
            "code": "native_structure_reference_invalid",
            "message": message,
        },
    )


def _conflict(code: str, message: str) -> ApplicationError:
    return ApplicationError(
        status_code=409,
        detail={"code": code, "message": message},
    )


__all__ = [
    "NativeStructureFeedbackRequestV1",
    "get_native_structure_reference",
    "submit_native_structure_feedback",
]
