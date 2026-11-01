"""Durable product orchestration for one SourceItem table scope.

The job owns identity, recovery, publication, and public progress.  The concrete
Codex transport is injected through ``SourceRequirementWindowExecutorPort`` so
the queue contract does not become coupled to the development CLI runner.
"""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.adapters.agents.codex_app_server.native_source_requirement_reference import (
    NativeSourceRequirementRunReceiptV1,
)
from reqpilotx_server.adapters.agents.codex_app_server.native_source_item_text_reference import (
    NativeSourceItemTextRunReceiptV1,
    SourceItemTextBatchCandidateV1,
)
from reqpilotx_server.adapters.agents.codex_app_server.native_source_item_text_windowed import (
    source_item_text_request_set_sha256,
)
from reqpilotx_server.adapters.persistence.native_source_requirement_checkpoints import (
    NativeSourceRequirementCheckpointProgress,
    NativeSourceRequirementDurableCheckpointStore,
)
from reqpilotx_server.adapters.persistence.native_source_item_text_checkpoints import (
    NativeSourceItemTextCheckpointProgress,
    NativeSourceItemTextDurableCheckpointStore,
)
from reqpilotx_server.application.agent_workflow.native_structure_baseline_jobs import (
    _current_reference_context,
)
from reqpilotx_server.application.agent_workflow.native_structure_bridge import (
    _canonical_sha256,
    _no_store_headers,
    _require_project_document,
    _verified_projection_operations,
    materialize_native_structure_table_preview,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1,
    NonRequirementRole,
    RequirementKind,
    Requirementness,
    SourceRequirementBaselineAppendReceiptV1,
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineError,
    SourceRequirementBaselineGrowthReceiptV1,
    SourceRequirementBaselineValidationReceiptV1,
    SourceItemScopeRevisionOperation,
    SourceItemScopeRevisionReceiptV1,
    SourceItemTextScopeRequestV1,
    SourceItemTextSpanV1,
    SourceRecordForm,
    SourceRequirementScopeRequestV1,
    SourceRequirementSliceCandidateV1,
    append_source_requirement_baseline,
    build_source_item_text_scope_request,
    build_source_item_scope_revision,
    build_source_requirement_scope_request,
    canonical_json_sha256,
    compile_document_map_artifact,
    compile_source_item_scope_revision,
    grow_source_requirement_baseline,
    validate_source_requirement_slice_candidate,
    validate_source_item_text_slice_candidate,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.source_runtime import resolve_source_record_path
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.ports.job_queue import (
    DurableJob,
    JobExecutionContext,
    JobStatus,
    NonRetryableJobError,
)
from reqpilotx_server.ports.source_requirement_window_executor import (
    SourceRequirementWindowExecutionV1,
    SourceRequirementWindowTaskV1,
)
from reqpilotx_server.ports.source_item_text_executor import (
    SourceItemTextExecutionV1,
    SourceItemTextTaskV1,
)


SOURCE_REQUIREMENT_BASELINE_JOB_KIND = "agent.source-requirement.baseline"
SOURCE_ITEM_TEXT_BASELINE_JOB_KIND = "agent.source-item-text.baseline"
SOURCE_ITEM_SCOPE_REVISION_JOB_KIND = "agent.source-item.scope-revision"
SOURCE_REQUIREMENT_BASELINE_JOB_SCHEMA_VERSION = (
    "source-requirement-baseline-job/v1"
)
SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION = (
    "source-requirement-baseline-job-result/v1"
)
SOURCE_ITEM_TEXT_BASELINE_JOB_SCHEMA_VERSION = "source-item-text-baseline-job/v1"
SOURCE_ITEM_SCOPE_REVISION_JOB_SCHEMA_VERSION = "source-item-scope-revision-job/v1"
SOURCE_ITEM_SCOPE_UNDO_JOB_SCHEMA_VERSION = "source-item-scope-undo-job/v1"
SOURCE_REQUIREMENT_SCOPE_ARTIFACT_SCHEMA_VERSION = (
    "source-requirement-scope-result-artifact/v1"
)
SOURCE_REQUIREMENT_SCOPE_ARTIFACT_MEDIA_TYPE = (
    "application/vnd.reqpilotx.source-requirement-scope-result+json"
)
SOURCE_ITEM_TEXT_SCOPE_ARTIFACT_SCHEMA_VERSION = (
    "source-item-text-scope-result-artifact/v1"
)
SOURCE_ITEM_TEXT_SCOPE_ARTIFACT_MEDIA_TYPE = (
    "application/vnd.reqpilotx.source-item-text-scope-result+json"
)
SOURCE_REQUIREMENT_PRODUCT_BASELINE_SCHEMA_VERSION = (
    "source-requirement-product-baseline-artifact/v1"
)
SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE = (
    "application/vnd.reqpilotx.source-requirement-product-baseline+json"
)


class SourceRequirementBaselineStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["source-requirement-baseline-start-request/v1"] = (
        "source-requirement-baseline-start-request/v1"
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    logical_table_scope_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )


class SourceItemTextBaselineStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["source-item-text-baseline-start-request/v1"] = (
        "source-item-text-baseline-start-request/v1"
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    node_ids: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_nodes(self) -> SourceItemTextBaselineStartRequestV1:
        if len(self.node_ids) != len(set(self.node_ids)) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", item)
            for item in self.node_ids
        ):
            raise ValueError("source-item text node IDs must be unique and safe")
        return self


class SourceItemScopeRevisionStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["source-item-scope-revision-start-request/v1"] = (
        "source-item-scope-revision-start-request/v1"
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    expected_baseline_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_revision: int = Field(ge=1)
    affected_scope_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    comparison_scope_ids: tuple[str, ...] = Field(default=(), max_length=32)
    feedback_ids: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_revision_scope(self) -> SourceItemScopeRevisionStartRequestV1:
        for values, subject in (
            (self.affected_scope_ids, "affected scopes"),
            (self.comparison_scope_ids, "comparison scopes"),
            (self.feedback_ids, "feedback IDs"),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"scope revision {subject} must be unique and sorted")
        if set(self.affected_scope_ids).intersection(self.comparison_scope_ids):
            raise ValueError("scope revision comparison scopes must remain read-only")
        if any(
            re.fullmatch(r"text:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", item)
            is None
            for item in self.affected_scope_ids + self.comparison_scope_ids
        ) or any(
            re.fullmatch(
                r"source-requirement-feedback-[0-9a-f]{32}",
                item,
            )
            is None
            for item in self.feedback_ids
        ):
            raise ValueError("scope revision IDs are invalid")
        return self


class SourceItemScopeUndoStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["source-item-scope-undo-start-request/v1"] = (
        "source-item-scope-undo-start-request/v1"
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    expected_baseline_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_baseline_revision: int = Field(ge=2)
    expected_scope_revision_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceItemScopeRevisionGuidanceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    guidance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    origin: Literal["USER_FEEDBACK", "HOST_REVIEW_REQUEST"]
    source_record_id: str | None = Field(
        default=None,
        pattern=r"^source-record-[0-9a-f]{32}$",
    )
    scope_id: str = Field(pattern=r"^text:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    message: str = Field(min_length=1, max_length=4000)


class SourceItemScopeRevisionCurrentRecordV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    source_record_id: str = Field(pattern=r"^source-record-[0-9a-f]{32}$")
    source_display_id: str = Field(pattern=r"^SRC-[0-9]{4,}$")
    requirement_display_id: str | None = Field(
        default=None,
        pattern=r"^REQ-[0-9]{4,}$",
    )
    label: str = Field(min_length=1, max_length=1000)
    source_form: SourceRecordForm
    text_spans: tuple[SourceItemTextSpanV1, ...] = Field(
        min_length=1,
        max_length=4096,
    )
    shared_source_content_ids: tuple[str, ...] = Field(default=(), max_length=256)
    requirementness: Requirementness
    requirement_kinds: tuple[RequirementKind, ...] = Field(default=(), max_length=5)
    non_requirement_role: NonRequirementRole | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    decision_summary: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_current_record(self) -> SourceItemScopeRevisionCurrentRecordV1:
        owned_content_ids = {item.content_id for item in self.text_spans}
        if (
            self.source_form
            not in {SourceRecordForm.NUMBERED_CLAUSE, SourceRecordForm.NATIVE_LIST, SourceRecordForm.PARAGRAPH}
            or len(self.shared_source_content_ids)
            != len(set(self.shared_source_content_ids))
            or owned_content_ids.intersection(self.shared_source_content_ids)
        ):
            raise ValueError("current scope record evidence is invalid")
        if self.requirementness is Requirementness.REQUIREMENT:
            if (
                not self.requirement_kinds
                or self.non_requirement_role is not None
                or self.requirement_display_id is None
            ):
                raise ValueError("current requirement violates A/B/F")
        elif (
            self.requirement_kinds
            or self.non_requirement_role is None
            or self.requirement_display_id is not None
        ):
            raise ValueError("current retained content violates A/B/F")
        if self.requirement_display_id is not None and (
            self.requirement_display_id.removeprefix("REQ-")
            != self.source_display_id.removeprefix("SRC-")
        ):
            raise ValueError("current REQ identity must share its SRC suffix")
        return self


class SourceItemScopeRevisionCurrentScopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    scope_id: str = Field(pattern=r"^text:[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    records: tuple[SourceItemScopeRevisionCurrentRecordV1, ...] = Field(
        default=(),
        max_length=8192,
    )
    structure_only_spans: tuple[SourceItemTextSpanV1, ...] = Field(
        default=(),
        max_length=4096,
    )

    @model_validator(mode="after")
    def validate_current_scope(self) -> SourceItemScopeRevisionCurrentScopeV1:
        record_ids = tuple(item.source_record_id for item in self.records)
        if (
            (not self.records and not self.structure_only_spans)
            or len(record_ids) != len(set(record_ids))
        ):
            raise ValueError("current scope boundary is empty or duplicated")
        return self


class SourceItemScopeRevisionAgentContextV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["source-item-scope-revision-agent-context/v2"] = (
        "source-item-scope-revision-agent-context/v2"
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_baseline_artifact_id: str = Field(
        pattern=r"^source-requirement-baseline-[0-9a-f]{32}$"
    )
    current_baseline_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_baseline_revision: int = Field(ge=1)
    affected_scope_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    comparison_scope_ids: tuple[str, ...] = Field(default=(), max_length=32)
    guidance_items: tuple[SourceItemScopeRevisionGuidanceV1, ...] = Field(
        min_length=1, max_length=64
    )
    current_affected_scopes: tuple[SourceItemScopeRevisionCurrentScopeV1, ...] = Field(
        min_length=1,
        max_length=32,
    )
    comparison_scopes: tuple[SourceItemTextScopeRequestV1, ...] = Field(
        default=(), max_length=32
    )
    instruction: Literal[
        "Use source-supported feedback to return complete replacements for affected scopes only; comparison scopes are read-only context."
    ] = (
        "Use source-supported feedback to return complete replacements for affected scopes only; comparison scopes are read-only context."
    )
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_context(self) -> SourceItemScopeRevisionAgentContextV2:
        if (
            self.affected_scope_ids != tuple(sorted(set(self.affected_scope_ids)))
            or self.comparison_scope_ids
            != tuple(sorted(set(self.comparison_scope_ids)))
            or set(self.affected_scope_ids).intersection(self.comparison_scope_ids)
            or tuple(item.guidance_id for item in self.guidance_items)
            != tuple(sorted({item.guidance_id for item in self.guidance_items}))
            or any(
                item.scope_id not in self.affected_scope_ids
                for item in self.guidance_items
            )
            or tuple(item.scope_id for item in self.current_affected_scopes)
            != self.affected_scope_ids
            or len(
                {
                    record.source_record_id
                    for scope in self.current_affected_scopes
                    for record in scope.records
                }
            )
            != sum(len(scope.records) for scope in self.current_affected_scopes)
            or tuple(item.scope_id for item in self.comparison_scopes)
            != self.comparison_scope_ids
        ):
            raise ValueError("scope revision Agent context drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"context_sha256"})
        )
        if self.context_sha256 != expected:
            raise ValueError("scope revision Agent context SHA drifted")
        return self


class SourceRequirementScopeResultArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["source-requirement-scope-result-artifact/v1"] = (
        SOURCE_REQUIREMENT_SCOPE_ARTIFACT_SCHEMA_VERSION
    )
    content_bearing: Literal[True] = True
    private_object_store_only: Literal[True] = True
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: SourceRequirementScopeRequestV1
    candidate: SourceRequirementSliceCandidateV1
    run_receipt: NativeSourceRequirementRunReceiptV1
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> SourceRequirementScopeResultArtifactV1:
        if (
            self.request.document_map_artifact_id != self.document_map_artifact_id
            or self.request.document_map_artifact_sha256
            != self.document_map_artifact_sha256
            or self.candidate.request_sha256 != self.request.request_sha256
            or self.run_receipt.request_sha256 != self.request.request_sha256
            or self.run_receipt.candidate_sha256
            != canonical_json_sha256(self.candidate.model_dump(mode="json"))
        ):
            raise ValueError("source-requirement scope result lineage drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"artifact_sha256"})
        )
        if self.artifact_sha256 != expected:
            raise ValueError("source-requirement scope result artifact SHA drifted")
        return self


class SourceItemTextScopeResultArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["source-item-text-scope-result-artifact/v1"] = (
        SOURCE_ITEM_TEXT_SCOPE_ARTIFACT_SCHEMA_VERSION
    )
    content_bearing: Literal[True] = True
    private_object_store_only: Literal[True] = True
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requests: tuple[SourceItemTextScopeRequestV1, ...] = Field(
        min_length=1, max_length=32
    )
    batch: SourceItemTextBatchCandidateV1
    run_receipt: NativeSourceItemTextRunReceiptV1
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> SourceItemTextScopeResultArtifactV1:
        request_set_sha256 = canonical_json_sha256(
            [item.model_dump(mode="json") for item in self.requests]
        )
        if (
            any(
                item.document_map_artifact_id != self.document_map_artifact_id
                or item.document_map_artifact_sha256
                != self.document_map_artifact_sha256
                for item in self.requests
            )
            or self.batch.document_map_artifact_id
            != self.document_map_artifact_id
            or self.batch.document_map_artifact_sha256
            != self.document_map_artifact_sha256
            or self.run_receipt.request_set_sha256 != request_set_sha256
            or self.run_receipt.candidate_sha256
            != canonical_json_sha256(self.batch.model_dump(mode="json"))
        ):
            raise ValueError("source-item text result lineage drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"artifact_sha256"})
        )
        if self.artifact_sha256 != expected:
            raise ValueError("source-item text result artifact SHA drifted")
        return self


class SourceItemAgentRunReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["source-item-agent-run-receipt/v1"] = "source-item-agent-run-receipt/v1"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str
    runner_version: str = "1.0.0"
    transport_kind: Literal["CODEX_APP_SERVER_JSON_STAGE"] = "CODEX_APP_SERVER_JSON_STAGE"
    operation: Literal["CLASSIFICATION_REVISION", "IMAGE_SCOPE"]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_receipts: tuple[dict[str, Any], ...] = Field(min_length=1)


class SourceItemDirectRunReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["source-item-direct-run-receipt/v1"] = "source-item-direct-run-receipt/v1"
    transport_kind: Literal["CODEX_APP_DIRECT"] = "CODEX_APP_DIRECT"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1)
    recorded_at: str = Field(min_length=1)
    action_record: str = Field(min_length=1)
    authorization_scope: str = Field(min_length=1)
    operation: Literal["CLASSIFICATION_REVISION", "IMAGE_SCOPE"]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceItemNativeLoopRunReceiptV1(BaseModel):
    """Actual retained App Server thread, distinct from disposable JSON stages."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["source-item-native-loop-run-receipt/v1"] = "source-item-native-loop-run-receipt/v1"
    transport_kind: Literal["CODEX_APP_SERVER_NATIVE_LOOP", "PROVIDER_TOOL_LOOP"] = "CODEX_APP_SERVER_NATIVE_LOOP"
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1)
    runtime_thread_id: str = Field(min_length=1)
    runtime_turn_id: str = Field(min_length=1)
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["NATIVE_LOOP_REVISION", "NATIVE_LOOP_REVIEW"] = "NATIVE_LOOP_REVISION"
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_receipt: dict[str, Any]

    @model_validator(mode="after")
    def validate_runtime_evidence(self):
        receipt = self.runtime_receipt
        if (receipt.get("status") != "COMPLETED"
                or receipt.get("thread_id") != self.runtime_thread_id
                or receipt.get("turn_id") != self.runtime_turn_id
                or (receipt.get("identity") or {}).get("model_id") != self.model_id
                or receipt.get("is_human_accepted") is not False
                or not any(tool.get("name") == "reqpilot_publish" and tool.get("success") is True
                           for tool in receipt.get("tools", []))):
            raise ValueError("Native-loop publication requires a completed, identity-bound publish receipt")
        if self.transport_kind == "CODEX_APP_SERVER_NATIVE_LOOP":
            if (receipt.get("adapter_id") != "reqpilotx-codex-native-loop"
                    or receipt.get("terminal_snapshot_verified") is not True
                    or receipt.get("thread_retained") is not True):
                raise ValueError("Codex App Server loop requires a retained terminal snapshot")
        elif (receipt.get("adapter_id") != "reqpilotx-provider-tool-loop"
              or receipt.get("transport_kind") != "PROVIDER_TOOL_LOOP"
              or receipt.get("local_state_persisted") is not True
              or receipt.get("thread_retained") is not True
              or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("run_state_sha256") or ""))
              or not str(receipt.get("run_state_reference") or "").strip()
              or (receipt.get("identity") or {}).get("provider") not in {"azure", "aliyun"}):
            raise ValueError("Provider loop requires an immutable local run-state receipt")
        return self


class SourceItemAgentRevisionReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["source-item-agent-revision-receipt/v1"] = "source-item-agent-revision-receipt/v1"
    artifact_id: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["CLASSIFICATION_REVISION", "IMAGE_SCOPE", "NATIVE_LOOP_REVISION"]
    affected_scope_ids: tuple[str, ...] = Field(min_length=1)
    validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain_proof: dict[str, Any]


class SourceRequirementProductBaselineArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal[
        "source-requirement-product-baseline-artifact/v1"
    ] = SOURCE_REQUIREMENT_PRODUCT_BASELINE_SCHEMA_VERSION
    content_bearing: Literal[True] = True
    private_object_store_only: Literal[True] = True
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_id: str = Field(pattern=r"^document-map-[0-9a-f]{32}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline: SourceRequirementBaselineArtifactV1
    validation_receipt: SourceRequirementBaselineValidationReceiptV1
    append_receipt: SourceRequirementBaselineAppendReceiptV1 | None = None
    growth_receipt: SourceRequirementBaselineGrowthReceiptV1 | None = None
    scope_revision_receipt: SourceItemScopeRevisionReceiptV1 | None = None
    agent_revision_receipt: SourceItemAgentRevisionReceiptV1 | None = None
    product_run_receipts: tuple[
        NativeSourceRequirementRunReceiptV1 | NativeSourceItemTextRunReceiptV1 | SourceItemAgentRunReceiptV1 | SourceItemDirectRunReceiptV1 | SourceItemNativeLoopRunReceiptV1,
        ...,
    ] = Field(
        min_length=1,
        max_length=4096,
    )
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> SourceRequirementProductBaselineArtifactV1:
        revision_receipts = tuple(
            item
            for item in (
                self.append_receipt,
                self.growth_receipt,
                self.scope_revision_receipt,
                self.agent_revision_receipt,
            )
            if item is not None
        )
        if (
            len(revision_receipts) != 1
            or self.baseline.source_sha256 != self.source_sha256
            or self.baseline.document_map_artifact_id
            != self.document_map_artifact_id
            or self.baseline.document_map_artifact_sha256
            != self.document_map_artifact_sha256
            or self.validation_receipt.artifact_id != self.baseline.artifact_id
            or self.validation_receipt.artifact_sha256
            != self.baseline.artifact_sha256
            or revision_receipts[0].artifact_id != self.baseline.artifact_id
            or revision_receipts[0].artifact_sha256
            != self.baseline.artifact_sha256
            or any(
                item.source_sha256 != self.source_sha256
                or item.document_map_artifact_id != self.document_map_artifact_id
                or item.document_map_artifact_sha256
                != self.document_map_artifact_sha256
                for item in self.product_run_receipts
            )
        ):
            raise ValueError("product SourceItem baseline lineage drifted")
        hash_payload = self.model_dump(mode="json", exclude={"artifact_sha256"})
        compatible_payloads = [hash_payload]
        for optional_name, optional_value in (
            ("growth_receipt", self.growth_receipt),
            ("scope_revision_receipt", self.scope_revision_receipt),
            ("agent_revision_receipt", self.agent_revision_receipt),
        ):
            if optional_value is None:
                compatible_payloads.extend(
                    {
                        key: item
                        for key, item in payload.items()
                        if key != optional_name
                    }
                    for payload in tuple(compatible_payloads)
                )
        expected_hashes = {
            canonical_json_sha256(payload) for payload in compatible_payloads
        }
        if self.artifact_sha256 not in expected_hashes:
            raise ValueError("product SourceItem baseline artifact SHA drifted")
        return self


def start_source_requirement_baseline(
    project_id: str,
    document_id: str,
    payload: SourceRequirementBaselineStartRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _durable_jobs

    response.headers.update(_no_store_headers())
    container = RUNTIME_STATE.container
    if (
        container is None
        or container.jobs is None
        or container.source_requirement_window_executor is None
    ):
        raise _runtime_unavailable()
    readiness_reason = container.source_requirement_window_executor.readiness_reason()
    if readiness_reason is not None:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "source_requirement_baseline_runtime_not_ready",
                "message": (
                    "Codex Agent runtime changed and must be requalified before "
                    "this table can be processed."
                ),
                "reason_code": readiness_reason,
            },
        )
    current = _current_scope_context(
        project_id=project_id,
        document_id=document_id,
        logical_table_scope_id=payload.logical_table_scope_id,
    )
    if (
        payload.expected_source_sha256 != current["source_sha256"]
        or payload.expected_artifact_id != current["native_artifact_id"]
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_requirement_baseline_reference_changed",
                "message": "The source or accepted structure changed before start.",
            },
        )
    executor = container.source_requirement_window_executor
    job_payload = {
        "application_entrypoint": "source-item-workbench-v1",
        "schema_version": SOURCE_REQUIREMENT_BASELINE_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": current["source_sha256"],
        "native_artifact_id": current["native_artifact_id"],
        "native_projection_sha256": current["native_projection_sha256"],
        "document_map_artifact_id": current["document_map"].artifact_id,
        "document_map_artifact_sha256": current["document_map"].artifact_sha256,
        "logical_table_scope_id": payload.logical_table_scope_id,
        "request_sha256": current["request"].request_sha256,
        "evidence_recovery_receipt_sha256": str(
            current["evidence_recovery"].get("receipt_sha256") or ""
        ),
        "executor_id": executor.adapter_id,
        "executor_version": executor.adapter_version,
        "transport_kind": executor.transport_kind,
        "model_id": executor.model_id,
        "reasoning_effort": executor.reasoning_effort,
        "timeout_seconds": 1200,
        "max_concurrency": 2,
        "command_id": payload.command_id,
    }
    jobs = _durable_jobs()
    latest = jobs.get_latest_succeeded(
        kind=SOURCE_REQUIREMENT_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if _job_matches(latest, job_payload):
        return _job_projection(latest)
    job = jobs.enqueue(
        kind=SOURCE_REQUIREMENT_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=_canonical_sha256(job_payload),
        payload=job_payload,
        priority=23,
        max_attempts=3,
    )
    return _job_projection(job)


def start_source_item_text_baseline(
    project_id: str,
    document_id: str,
    payload: SourceItemTextBaselineStartRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _durable_jobs

    response.headers.update(_no_store_headers())
    container = RUNTIME_STATE.container
    executor = None if container is None else container.source_item_text_executor
    if container is None or container.jobs is None or executor is None:
        raise _runtime_unavailable()
    readiness_reason = executor.readiness_reason()
    if readiness_reason is not None:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "source_item_text_runtime_not_ready",
                "message": (
                    "Codex Agent runtime changed and must be requalified before "
                    "this direct-text batch can be processed."
                ),
                "reason_code": readiness_reason,
            },
        )
    jobs = _durable_jobs()
    latest = jobs.get_latest_succeeded(
        kind=SOURCE_ITEM_TEXT_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if (
        latest is not None
        and latest.payload.get("source_sha256") == payload.expected_source_sha256
        and latest.payload.get("native_artifact_id") == payload.expected_artifact_id
        and set(latest.payload.get("node_ids") or ()) == set(payload.node_ids)
    ):
        return _job_projection(latest)
    current = _current_text_scope_context(
        project_id=project_id,
        document_id=document_id,
        node_ids=payload.node_ids,
    )
    if (
        payload.expected_source_sha256 != current["source_sha256"]
        or payload.expected_artifact_id != current["native_artifact_id"]
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_text_reference_changed",
                "message": "The source or accepted structure changed before start.",
            },
        )
    job_payload = {
        "application_entrypoint": "source-item-workbench-v1",
        "schema_version": SOURCE_ITEM_TEXT_BASELINE_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": current["source_sha256"],
        "native_artifact_id": current["native_artifact_id"],
        "native_projection_sha256": current["native_projection_sha256"],
        "document_map_artifact_id": current["document_map"].artifact_id,
        "document_map_artifact_sha256": current["document_map"].artifact_sha256,
        "node_ids": list(current["node_ids"]),
        "scope_ids": list(current["scope_ids"]),
        "scope_id": current["batch_scope_id"],
        "request_set_sha256": current["request_set_sha256"],
        "executor_id": executor.adapter_id,
        "executor_version": executor.adapter_version,
        "transport_kind": executor.transport_kind,
        "model_id": executor.model_id,
        "reasoning_effort": executor.reasoning_effort,
        "timeout_seconds": 1200,
        "max_concurrency": 2,
        "command_id": payload.command_id,
    }
    if _text_job_matches(latest, job_payload):
        return _job_projection(latest)
    job = jobs.enqueue(
        kind=SOURCE_ITEM_TEXT_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=_canonical_sha256(job_payload),
        payload=job_payload,
        priority=23,
        max_attempts=3,
    )
    return _job_projection(job)


def start_source_item_scope_revision(
    project_id: str,
    document_id: str,
    payload: SourceItemScopeRevisionStartRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _durable_jobs

    response.headers.update(_no_store_headers())
    container = RUNTIME_STATE.container
    executor = None if container is None else container.source_item_text_executor
    if (
        container is None
        or container.jobs is None
        or container.object_store is None
        or executor is None
    ):
        raise _runtime_unavailable()
    readiness_reason = executor.readiness_reason()
    if readiness_reason is not None:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "source_item_scope_revision_runtime_not_ready",
                "message": (
                    "Codex Agent runtime changed and must be requalified before "
                    "this local SourceItem revision can run."
                ),
                "reason_code": readiness_reason,
            },
        )
    current = _current_scope_revision_context(
        project_id=project_id,
        document_id=document_id,
        affected_scope_ids=payload.affected_scope_ids,
        comparison_scope_ids=payload.comparison_scope_ids,
        feedback_ids=payload.feedback_ids,
        container=container,
    )
    if (
        payload.expected_source_sha256 != current["source_sha256"]
        or payload.expected_document_map_artifact_sha256
        != current["document_map"].artifact_sha256
        or payload.expected_baseline_artifact_id
        != current["baseline"].artifact_id
        or payload.expected_baseline_artifact_sha256
        != current["baseline"].artifact_sha256
        or payload.expected_baseline_revision != current["baseline"].revision
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_scope_revision_baseline_changed",
                "message": "The working SourceItem baseline changed before revision start.",
            },
        )
    job_payload = {
        "application_entrypoint": "source-item-workbench-v1",
        "schema_version": SOURCE_ITEM_SCOPE_REVISION_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": current["source_sha256"],
        "native_artifact_id": current["native_artifact_id"],
        "native_projection_sha256": current["native_projection_sha256"],
        "document_map_artifact_id": current["document_map"].artifact_id,
        "document_map_artifact_sha256": current["document_map"].artifact_sha256,
        "current_baseline_artifact_id": current["baseline"].artifact_id,
        "current_baseline_artifact_sha256": current["baseline"].artifact_sha256,
        "current_baseline_revision": current["baseline"].revision,
        "current_validation_receipt_sha256": current[
            "validation_receipt"
        ].receipt_sha256,
        "affected_scope_ids": list(current["affected_scope_ids"]),
        "scope_ids": list(current["affected_scope_ids"]),
        "scope_id": (
            current["affected_scope_ids"][0]
            if len(current["affected_scope_ids"]) == 1
            else f"scope-revision-{current['agent_context'].context_sha256[:32]}"
        ),
        "comparison_scope_ids": list(current["comparison_scope_ids"]),
        "feedback_ids": list(current["feedback_ids"]),
        "guidance_ids": list(current["guidance_ids"]),
        "node_ids": [item.node_id for item in current["affected_requests"]],
        "request_set_sha256": current["request_set_sha256"],
        "agent_context_sha256": current["agent_context"].context_sha256,
        "executor_id": executor.adapter_id,
        "executor_version": executor.adapter_version,
        "transport_kind": executor.transport_kind,
        "model_id": executor.model_id,
        "reasoning_effort": executor.reasoning_effort,
        "timeout_seconds": 1200,
        "max_concurrency": 2,
        "command_id": payload.command_id,
    }
    jobs = _durable_jobs()
    latest = jobs.get_latest_succeeded(
        kind=SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if _scope_revision_job_matches(latest, job_payload):
        return _job_projection(latest)
    job = jobs.enqueue(
        kind=SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=_canonical_sha256(job_payload),
        payload=job_payload,
        priority=24,
        max_attempts=3,
    )
    return _job_projection(job)


def start_source_item_scope_undo(
    project_id: str,
    document_id: str,
    payload: SourceItemScopeUndoStartRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _durable_jobs

    response.headers.update(_no_store_headers())
    container = RUNTIME_STATE.container
    if (
        container is None
        or container.jobs is None
        or container.object_store is None
    ):
        raise _runtime_unavailable()
    current = _current_scope_undo_context(
        project_id=project_id,
        document_id=document_id,
        container=container,
    )
    if (
        payload.expected_source_sha256 != current["source_sha256"]
        or payload.expected_document_map_artifact_sha256
        != current["document_map"].artifact_sha256
        or payload.expected_baseline_artifact_id
        != current["baseline"].artifact_id
        or payload.expected_baseline_artifact_sha256
        != current["baseline"].artifact_sha256
        or payload.expected_baseline_revision != current["baseline"].revision
        or payload.expected_scope_revision_receipt_sha256
        != current["scope_revision_receipt"].receipt_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_scope_undo_baseline_changed",
                "message": "The working SourceItem revision changed before undo start.",
            },
        )
    job_payload = {
        "application_entrypoint": "source-item-workbench-v1",
        "schema_version": SOURCE_ITEM_SCOPE_UNDO_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": current["source_sha256"],
        "document_map_artifact_id": current["document_map"].artifact_id,
        "document_map_artifact_sha256": current["document_map"].artifact_sha256,
        "current_baseline_artifact_id": current["baseline"].artifact_id,
        "current_baseline_artifact_sha256": current["baseline"].artifact_sha256,
        "current_baseline_revision": current["baseline"].revision,
        "current_validation_receipt_sha256": current[
            "validation_receipt"
        ].receipt_sha256,
        "scope_revision_receipt_sha256": current[
            "scope_revision_receipt"
        ].receipt_sha256,
        "affected_scope_ids": list(current["affected_scope_ids"]),
        "scope_ids": list(current["affected_scope_ids"]),
        "scope_id": (
            current["affected_scope_ids"][0]
            if len(current["affected_scope_ids"]) == 1
            else f"scope-undo-{current['scope_revision_receipt'].receipt_sha256[:32]}"
        ),
        "comparison_scope_ids": list(current["comparison_scope_ids"]),
        "feedback_ids": list(current["feedback_ids"]),
        "guidance_ids": list(current["guidance_ids"]),
        "undo_target_artifact_id": current["undo_target"].baseline.artifact_id,
        "undo_target_artifact_sha256": current[
            "undo_target"
        ].baseline.artifact_sha256,
        "undo_target_artifact_revision": current["undo_target"].baseline.revision,
        "command_id": payload.command_id,
    }
    jobs = _durable_jobs()
    latest = jobs.get_latest_succeeded(
        kind=SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if _scope_undo_job_matches(latest, job_payload):
        return _job_projection(latest)
    job = jobs.enqueue(
        kind=SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=_canonical_sha256(job_payload),
        payload=job_payload,
        priority=24,
        max_attempts=3,
    )
    return _job_projection(job)


def get_source_requirement_baseline_run_status(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _durable_jobs

    response.headers.update(_no_store_headers())
    _require_project_document(project_id, document_id)
    if RUNTIME_STATE.container is None or RUNTIME_STATE.container.jobs is None:
        return {
            "schema_version": "source-requirement-baseline-run-status/v1",
            "status": "UNAVAILABLE",
            "result_ready": False,
            "job": None,
            "message": "Durable SourceItem processing is unavailable.",
        }
    jobs = _durable_jobs()
    job = _latest_job(
        jobs.get_latest(
            kind=SOURCE_REQUIREMENT_BASELINE_JOB_KIND,
            aggregate_type="sor-document",
            aggregate_id=document_id,
        ),
        jobs.get_latest(
            kind=SOURCE_ITEM_TEXT_BASELINE_JOB_KIND,
            aggregate_type="sor-document",
            aggregate_id=document_id,
        ),
        jobs.get_latest(
            kind=SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
            aggregate_type="sor-document",
            aggregate_id=document_id,
        ),
    )
    return {
        "schema_version": "source-requirement-baseline-run-status/v1",
        "status": "IDLE" if job is None else job.status.value,
        "result_ready": bool(job and job.status is JobStatus.SUCCEEDED),
        "job": None if job is None else _job_projection(job),
        "message": _status_message(job),
    }


def run_durable_source_requirement_baseline(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    payload = dict(durable_job.payload or {})
    if payload.get("schema_version") != SOURCE_REQUIREMENT_BASELINE_JOB_SCHEMA_VERSION:
        raise NonRetryableJobError(
            error_code="source_requirement_baseline_job_schema_invalid",
            message="SourceItem baseline job schema is invalid.",
        )
    container = RUNTIME_STATE.container
    executor = (
        None if container is None else container.source_requirement_window_executor
    )
    if container is None or executor is None:
        raise RuntimeError("SourceItem baseline executor is unavailable.")
    if (
        payload.get("executor_id") != executor.adapter_id
        or payload.get("executor_version") != executor.adapter_version
        or payload.get("transport_kind") != executor.transport_kind
        or payload.get("model_id") != executor.model_id
        or payload.get("reasoning_effort") != executor.reasoning_effort
    ):
        raise NonRetryableJobError(
            error_code="source_requirement_baseline_executor_changed",
            message="SourceItem executor identity changed after enqueue.",
        )
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    scope_id = str(payload.get("logical_table_scope_id") or "")
    try:
        current = _current_scope_context(
            project_id=project_id,
            document_id=document_id,
            logical_table_scope_id=scope_id,
        )
    except (ApplicationError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="source_requirement_baseline_scope_unavailable",
            message="The pinned SourceItem scope is no longer available.",
        ) from exc
    if not _current_scope_matches_payload(current, payload):
        raise NonRetryableJobError(
            error_code="source_requirement_baseline_identity_changed",
            message="Source, Document Map, or scope changed after enqueue.",
        )
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="source_requirement_baseline_cancelled",
            message="SourceItem processing was cancelled before execution.",
        )
    progress_state = dict(durable_job.progress or {})
    context.heartbeat(
        progress={
            **progress_state,
            "stage": "PREPARING_SOURCE_REQUIREMENT_SCOPE",
            "message": "正在准备当前逻辑表及其原文证据。",
            "strategy": "BOUNDARY_WINDOWS",
            "logical_table_scope_id": scope_id,
        }
    )
    store = NativeSourceRequirementDurableCheckpointStore(
        object_store=container.object_store,
        project_id=project_id,
        document_id=document_id,
    )
    checkpoint_progress = NativeSourceRequirementCheckpointProgress(
        context=context,
        store=store,
        initial=progress_state,
    )
    try:
        resume_checkpoint = checkpoint_progress.load()
    except ValueError as exc:
        raise NonRetryableJobError(
            error_code="source_requirement_baseline_checkpoint_invalid",
            message="Stored SourceItem checkpoint failed identity validation.",
        ) from exc
    latest_checkpoint = resume_checkpoint

    def save_checkpoint(value: Mapping[str, Any]) -> None:
        nonlocal latest_checkpoint
        latest_checkpoint = _validated_checkpoint(value)
        checkpoint_progress.save(latest_checkpoint)

    source_path = current["source_path"]
    task = SourceRequirementWindowTaskV1(
        project_id=project_id,
        document_id=document_id,
        source_path=source_path,
        document_map=current["document_map"].model_dump(mode="json"),
        scope_request=current["request"].model_dump(mode="json"),
        timeout_seconds=int(payload.get("timeout_seconds") or 1200),
        max_concurrency=int(payload.get("max_concurrency") or 2),
        semantic_context=current.get("semantic_context"),
    )
    accepted_window_count = (
        len(resume_checkpoint.accepted_windows)
        if resume_checkpoint is not None
        else 0
    )
    total_window_count = (
        len(resume_checkpoint.windows) if resume_checkpoint is not None else 0
    )
    context.heartbeat(
        progress={
            **progress_state,
            "stage": (
                "RESUMING_CLASSIFICATION_WINDOWS"
                if resume_checkpoint is not None
                else "RUNNING_BOUNDARY_PLAN"
            ),
            "message": (
                f"正在从检查点继续；已保留 {accepted_window_count}/"
                f"{total_window_count} 个分类窗口。"
                if resume_checkpoint is not None
                else "Agent 正在识别当前原表的完整业务记录边界。"
            ),
            "strategy": "BOUNDARY_WINDOWS",
            "logical_table_scope_id": scope_id,
            "completed_window_count": accepted_window_count,
            "total_window_count": total_window_count,
            **(
                {
                    "source_requirement_window_checkpoint": (
                        checkpoint_progress.receipt.model_dump(mode="json")
                    )
                }
                if checkpoint_progress.receipt is not None
                else {}
            ),
        }
    )
    from reqpilotx_server.application.agent_workflow.source_requirement_joint import (
        load_completed_table, save_completed_table, run_joint_tables,
    )
    completed = load_completed_table(
        current=current, progress=progress_state, container=container,
        project_id=project_id, document_id=document_id, executor=executor,
    )
    if completed is None:
        execution = executor.execute(
            task,
            resume_checkpoint=(
                None if resume_checkpoint is None
                else resume_checkpoint.model_dump(mode="json")
            ),
            checkpoint_sink=save_checkpoint,
            cancellation_requested=context.cancellation_requested,
        )
        candidate, run_receipt = _validate_execution(execution, request=current["request"])
    else:
        candidate, run_receipt = completed.candidate, completed.run_receipt
    if checkpoint_progress.receipt is not None:
        progress_state["source_requirement_window_checkpoint"] = checkpoint_progress.receipt.model_dump(mode="json")
    # Complete all table scopes that compete for this node's direct text before
    # assigning the remaining text or publishing any product revision.
    joint_tables = ()
    if any(record.owning_text_spans for record in candidate.records):
        if not isinstance(current.get("source_requirement_base_bundle"), Mapping):
            raise NonRetryableJobError(
                error_code="source_requirement_mixed_product_baseline_required",
                message="Mixed table/text scopes require a product baseline so both sources can be published atomically.",
            )
        save_completed_table(
            current=current, candidate=candidate, run_receipt=run_receipt,
            progress=progress_state, context=context, container=container,
            project_id=project_id, document_id=document_id,
        )
        joint_tables = run_joint_tables(
            current=current, candidate=candidate, container=container, context=context,
            project_id=project_id, document_id=document_id, progress=progress_state,
            timeout_seconds=task.timeout_seconds, max_concurrency=task.max_concurrency,
        )
    mixed_text = (
        _run_table_owned_text_companion(
            current=current,
            candidate=candidate,
            additional_tables=joint_tables,
            container=container,
            context=context,
            project_id=project_id,
            document_id=document_id,
            progress_state={
                **progress_state,
                **({"source_requirement_window_checkpoint": checkpoint_progress.receipt.model_dump(mode="json")}
                   if checkpoint_progress.receipt is not None else {}),
            },
            timeout_seconds=int(payload.get("timeout_seconds") or 1200),
        )
        if any(record.owning_text_spans for record in candidate.records)
        else None
    )
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="source_requirement_baseline_cancelled_before_publication",
            message="SourceItem processing was cancelled before publication.",
        )
    artifact = _build_result_artifact(
        document_map=current["document_map"],
        request=current["request"],
        candidate=candidate,
        run_receipt=run_receipt,
    )
    encoded = json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    object_sha256 = hashlib.sha256(encoded).hexdigest()
    object_key = _scope_result_object_key(
        project_id=project_id,
        document_id=document_id,
        artifact=artifact,
    )
    final_checkpoint_receipt = checkpoint_progress.receipt
    final_progress = {
        **progress_state,
        "stage": "PERSISTING_SOURCE_REQUIREMENT_SCOPE",
        "message": "正在保存当前范围的可恢复原文条目结果。",
        "strategy": "BOUNDARY_WINDOWS",
        "logical_table_scope_id": scope_id,
        "completed_window_count": len(
            final_checkpoint_receipt.accepted_window_ids
            if final_checkpoint_receipt is not None
            else ()
        ),
        "total_window_count": (
            len(latest_checkpoint.windows) if latest_checkpoint is not None else 0
        ),
    }
    if final_checkpoint_receipt is not None:
        final_progress["source_requirement_window_checkpoint"] = (
            final_checkpoint_receipt.model_dump(mode="json")
        )
    if mixed_text is not None and mixed_text[3] is not None:
        final_progress["source_item_text_window_checkpoint"] = mixed_text[3]
    product_baseline_pointer: dict[str, Any] | None = None
    product_baseline = None
    base_bundle = current.get("source_requirement_base_bundle")
    if isinstance(base_bundle, Mapping):
        predecessor = resolve_effective_source_requirement_bundle(
            project_id=project_id,
            document_id=document_id,
            base_bundle=base_bundle,
            container=container,
        )
        try:
            product_baseline = _append_product_baseline_artifact(
                document_map=current["document_map"],
                previous_artifact=predecessor["baseline"],
                previous_validation_receipt=predecessor["receipt"],
                request=current["request"],
                candidate=candidate,
                run_receipt=run_receipt,
                previous_product_run_receipts=tuple(
                    predecessor.get("product_run_receipts", ())
                ),
                text_requests=mixed_text[0] if mixed_text is not None else (),
                text_batch=mixed_text[1] if mixed_text is not None else None,
                text_run_receipt=mixed_text[2] if mixed_text is not None else None,
                additional_tables=joint_tables,
            )
        except SourceRequirementBaselineError as exc:
            raise NonRetryableJobError(
                error_code="source_requirement_baseline_append_invalid",
                message="The SourceItem scope could not be appended to the current baseline.",
            ) from exc
    context.heartbeat(progress=final_progress)
    context.begin_publication()
    stored = container.object_store.put(
        key=object_key,
        source=BytesIO(encoded),
        expected_sha256=object_sha256,
        media_type=SOURCE_REQUIREMENT_SCOPE_ARTIFACT_MEDIA_TYPE,
    )
    if (
        stored.key != object_key
        or stored.sha256 != object_sha256
        or stored.size != len(encoded)
        or stored.media_type != SOURCE_REQUIREMENT_SCOPE_ARTIFACT_MEDIA_TYPE
    ):
        raise RuntimeError("SourceItem result object-store receipt drifted.")
    if product_baseline is not None:
        product_encoded = json.dumps(
            product_baseline.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        product_object_sha256 = hashlib.sha256(product_encoded).hexdigest()
        product_object_key = _product_baseline_object_key(
            project_id=project_id,
            document_id=document_id,
            artifact=product_baseline,
        )
        product_stored = container.object_store.put(
            key=product_object_key,
            source=BytesIO(product_encoded),
            expected_sha256=product_object_sha256,
            media_type=SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE,
        )
        if (
            product_stored.key != product_object_key
            or product_stored.sha256 != product_object_sha256
            or product_stored.size != len(product_encoded)
            or product_stored.media_type
            != SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE
        ):
            raise RuntimeError("product SourceItem baseline receipt drifted.")
        product_baseline_pointer = {
            "object_key": product_stored.key,
            "object_sha256": product_stored.sha256,
            "artifact_sha256": product_baseline.artifact_sha256,
            "baseline_artifact_id": product_baseline.baseline.artifact_id,
            "baseline_artifact_sha256": (
                product_baseline.baseline.artifact_sha256
            ),
            "baseline_revision": product_baseline.baseline.revision,
            "record_count": len(product_baseline.baseline.records),
            "size": product_stored.size,
            "media_type": product_stored.media_type,
        }
    result = {
        "schema_version": SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION,
        "status": "SCOPE_COMPLETE",
        "source_sha256": current["source_sha256"],
        "native_artifact_id": current["native_artifact_id"],
        "native_projection_sha256": current["native_projection_sha256"],
        "document_map_artifact_id": current["document_map"].artifact_id,
        "document_map_artifact_sha256": current["document_map"].artifact_sha256,
        "logical_table_scope_id": scope_id,
        "request_sha256": current["request"].request_sha256,
        "evidence_recovery": current["evidence_recovery"],
        "candidate_sha256": run_receipt.candidate_sha256,
        "record_count": len(candidate.records),
        "context_row_group_count": len(candidate.context_row_groups),
        **({"joint_table_scope_ids": [scope_id] + [item.request.logical_table_scope_id for item in joint_tables]}
           if joint_tables else {}),
        **({"mixed_text_scope_ids": [item.scope_id for item in mixed_text[0]]}
           if mixed_text is not None else {}),
        "executor_id": executor.adapter_id,
        "executor_version": executor.adapter_version,
        "transport_kind": executor.transport_kind,
        "private_artifact": {
            "object_key": stored.key,
            "object_sha256": stored.sha256,
            "artifact_sha256": artifact.artifact_sha256,
            "size": stored.size,
            "media_type": stored.media_type,
        },
        **(
            {"baseline_revision_artifact": product_baseline_pointer}
            if product_baseline_pointer is not None
            else {}
        ),
        "next_action": (
            "BASELINE_REVISION_PUBLISHED"
            if product_baseline_pointer is not None
            else "ASSEMBLE_BASELINE_REVISION"
        ),
    }
    from reqpilotx_server.application.agent_workflow.source_item_manual_publication import attach_rebound_baseline_manual_contexts
    if product_baseline_pointer is not None:
        attach_rebound_baseline_manual_contexts(result, container=container, project_id=project_id, document_id=document_id,
            previous_bundle=predecessor, document_map=current["document_map"], baseline=product_baseline.baseline)
    return result


def run_durable_source_item_text_baseline(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    payload = dict(durable_job.payload or {})
    if payload.get("schema_version") != SOURCE_ITEM_TEXT_BASELINE_JOB_SCHEMA_VERSION:
        raise NonRetryableJobError(
            error_code="source_item_text_job_schema_invalid",
            message="Direct-text SourceItem job schema is invalid.",
        )
    container = RUNTIME_STATE.container
    executor = None if container is None else container.source_item_text_executor
    if container is None or executor is None:
        raise RuntimeError("Direct-text SourceItem executor is unavailable.")
    if (
        payload.get("executor_id") != executor.adapter_id
        or payload.get("executor_version") != executor.adapter_version
        or payload.get("transport_kind") != executor.transport_kind
        or payload.get("model_id") != executor.model_id
        or payload.get("reasoning_effort") != executor.reasoning_effort
    ):
        raise NonRetryableJobError(
            error_code="source_item_text_executor_changed",
            message="Direct-text SourceItem executor changed after enqueue.",
        )
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    node_ids = tuple(str(item) for item in payload.get("node_ids") or ())
    try:
        current = _current_text_scope_context(
            project_id=project_id,
            document_id=document_id,
            node_ids=node_ids,
        )
    except (ApplicationError, OSError, TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="source_item_text_scope_unavailable",
            message="The pinned direct-text scopes are no longer available.",
        ) from exc
    if not _current_text_scope_matches_payload(current, payload):
        raise NonRetryableJobError(
            error_code="source_item_text_identity_changed",
            message="Source, Document Map, or direct-text scopes changed after enqueue.",
        )
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="source_item_text_cancelled",
            message="Direct-text SourceItem processing was cancelled before execution.",
        )
    progress_state = dict(durable_job.progress or {})
    checkpoint = _load_text_result_checkpoint(
        progress_state,
        object_store=container.object_store,
        project_id=project_id,
        document_id=document_id,
        current=current,
    )
    if checkpoint is None:
        context.heartbeat(
            progress={
                **progress_state,
                "stage": "PREPARING_SOURCE_ITEM_TEXT_SCOPES",
                "message": "正在准备直接正文范围及其不可变原文证据。",
                "strategy": "TEXT_BOUNDARY_WINDOWS",
                "scope_id": current["batch_scope_id"],
                "completed_window_count": 0,
                "total_window_count": 0,
            }
        )
        window_store = NativeSourceItemTextDurableCheckpointStore(
            object_store=container.object_store,
            project_id=project_id,
            document_id=document_id,
        )
        checkpoint_progress = NativeSourceItemTextCheckpointProgress(
            context=context,
            store=window_store,
            initial=progress_state,
        )
        try:
            resume_checkpoint = checkpoint_progress.load()
        except ValueError as exc:
            raise NonRetryableJobError(
                error_code="source_item_text_window_checkpoint_invalid",
                message="Stored direct-text window checkpoint failed validation.",
            ) from exc
        latest_window_checkpoint = resume_checkpoint

        def save_window_checkpoint(value: Mapping[str, Any]) -> None:
            nonlocal latest_window_checkpoint
            latest_window_checkpoint = _validated_text_checkpoint(value)
            checkpoint_progress.save(latest_window_checkpoint)

        accepted_boundary_count = (
            len(resume_checkpoint.accepted_boundaries)
            if resume_checkpoint is not None
            else 0
        )
        accepted_window_count = (
            len(resume_checkpoint.accepted_windows)
            if resume_checkpoint is not None
            else 0
        )
        total_window_count = (
            len(resume_checkpoint.classification_windows)
            if resume_checkpoint is not None
            else 0
        )
        context.heartbeat(
            progress={
                **progress_state,
                "stage": (
                    "RESUMING_SOURCE_ITEM_TEXT_WINDOWS"
                    if resume_checkpoint is not None
                    else "RUNNING_SOURCE_ITEM_TEXT_BOUNDARIES"
                ),
                "message": (
                    f"正在从检查点继续；已保留 {accepted_boundary_count}/"
                    f"{len(current['requests'])} 个正文边界和 "
                    f"{accepted_window_count}/{total_window_count} 个分类窗口。"
                    if resume_checkpoint is not None
                    else f"Agent 正在识别 {len(current['requests'])} 个直接正文范围的完整条目边界。"
                ),
                "strategy": "TEXT_BOUNDARY_WINDOWS",
                "scope_id": current["batch_scope_id"],
                "completed_window_count": accepted_window_count,
                "total_window_count": total_window_count,
                **(
                    {
                        "source_item_text_window_checkpoint": (
                            checkpoint_progress.receipt.model_dump(mode="json")
                        )
                    }
                    if checkpoint_progress.receipt is not None
                    else {}
                ),
            }
        )
        execution = executor.execute(
            SourceItemTextTaskV1(
                project_id=project_id,
                document_id=document_id,
                source_path=current["source_path"],
                document_map=current["document_map"].model_dump(mode="json"),
                scope_requests=tuple(
                    item.model_dump(mode="json") for item in current["requests"]
                ),
                timeout_seconds=int(payload.get("timeout_seconds") or 1200),
                max_concurrency=int(payload.get("max_concurrency") or 2),
                semantic_context=current.get("semantic_context"),
            ),
            cancellation_requested=context.cancellation_requested,
            resume_checkpoint=(
                None
                if resume_checkpoint is None
                else resume_checkpoint.model_dump(mode="json")
            ),
            checkpoint_sink=save_window_checkpoint,
        )
        batch, run_receipt = _validate_text_execution(
            execution,
            document_map=current["document_map"],
            requests=current["requests"],
        )
        artifact = _build_text_result_artifact(
            document_map=current["document_map"],
            requests=current["requests"],
            batch=batch,
            run_receipt=run_receipt,
        )
        stored = _store_text_result_artifact(
            container.object_store,
            project_id=project_id,
            document_id=document_id,
            artifact=artifact,
        )
        checkpoint = artifact
        final_window_receipt = checkpoint_progress.receipt
        progress_state = {
            **progress_state,
            "stage": "SOURCE_ITEM_TEXT_BATCH_CHECKPOINTED",
            "message": (
                f"已保存 {len(current['requests'])} 个直接文本范围的完整候选。"
            ),
            "strategy": "TEXT_BOUNDARY_WINDOWS",
            "scope_id": current["batch_scope_id"],
            "completed_window_count": len(
                final_window_receipt.accepted_window_ids
                if final_window_receipt is not None
                else ()
            ),
            "total_window_count": (
                len(latest_window_checkpoint.classification_windows)
                if latest_window_checkpoint is not None
                else 0
            ),
            "source_item_text_result_checkpoint": stored,
        }
        if final_window_receipt is not None:
            progress_state["source_item_text_window_checkpoint"] = (
                final_window_receipt.model_dump(mode="json")
            )
        context.heartbeat(progress=progress_state)
    else:
        batch = checkpoint.batch
        run_receipt = checkpoint.run_receipt
        context.heartbeat(
            progress={
                **progress_state,
                "stage": "RESUMING_SOURCE_ITEM_TEXT_PUBLICATION",
                "message": "已恢复完整文本候选；不会再次调用 Agent。",
                "strategy": "TEXT_BOUNDARY_WINDOWS",
                "scope_id": current["batch_scope_id"],
                "completed_window_count": int(
                    progress_state.get("completed_window_count") or 0
                ),
                "total_window_count": int(
                    progress_state.get("total_window_count") or 0
                ),
            }
        )
    base_bundle = current.get("source_requirement_base_bundle")
    product_baseline_pointer: dict[str, Any] | None = None
    if isinstance(base_bundle, Mapping):
        predecessor = resolve_effective_source_requirement_bundle(
            project_id=project_id,
            document_id=document_id,
            base_bundle=base_bundle,
            container=container,
        )
        try:
            product_baseline = _append_product_text_baseline_artifact(
                document_map=current["document_map"],
                previous_artifact=predecessor["baseline"],
                previous_validation_receipt=predecessor["receipt"],
                requests=current["requests"],
                batch=batch,
                run_receipt=run_receipt,
                previous_product_run_receipts=tuple(
                    predecessor.get("product_run_receipts", ())
                ),
            )
        except SourceRequirementBaselineError as exc:
            raise NonRetryableJobError(
                error_code="source_item_text_growth_invalid",
                message=(
                    "The direct-text batch could not grow the current SourceItem baseline."
                ),
            ) from exc
        product_encoded = json.dumps(
            product_baseline.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        product_key = _product_baseline_object_key(
            project_id=project_id,
            document_id=document_id,
            artifact=product_baseline,
        )
        context.begin_publication()
        product_stored = container.object_store.put(
            key=product_key,
            source=BytesIO(product_encoded),
            expected_sha256=hashlib.sha256(product_encoded).hexdigest(),
            media_type=SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE,
        )
        if (
            product_stored.key != product_key
            or product_stored.sha256 != hashlib.sha256(product_encoded).hexdigest()
            or product_stored.size != len(product_encoded)
            or product_stored.media_type
            != SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE
        ):
            raise RuntimeError("product SourceItem text baseline receipt drifted")
        product_baseline_pointer = {
            "object_key": product_stored.key,
            "object_sha256": product_stored.sha256,
            "artifact_sha256": product_baseline.artifact_sha256,
            "baseline_artifact_id": product_baseline.baseline.artifact_id,
            "baseline_artifact_sha256": product_baseline.baseline.artifact_sha256,
            "baseline_revision": product_baseline.baseline.revision,
            "record_count": len(product_baseline.baseline.records),
            "size": product_stored.size,
            "media_type": product_stored.media_type,
        }
    result = {
        "schema_version": SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION,
        "status": "SCOPE_COMPLETE",
        "source_sha256": current["source_sha256"],
        "native_artifact_id": current["native_artifact_id"],
        "native_projection_sha256": current["native_projection_sha256"],
        "document_map_artifact_id": current["document_map"].artifact_id,
        "document_map_artifact_sha256": current["document_map"].artifact_sha256,
        "scope_id": current["batch_scope_id"],
        "scope_ids": list(current["scope_ids"]),
        "request_set_sha256": current["request_set_sha256"],
        "candidate_sha256": run_receipt.candidate_sha256,
        "record_count": sum(len(item.items) for item in batch.candidates),
        "context_row_group_count": sum(
            len(item.structure_only_spans) for item in batch.candidates
        ),
        "executor_id": executor.adapter_id,
        "executor_version": executor.adapter_version,
        "transport_kind": executor.transport_kind,
        "private_artifact": dict(
            progress_state.get("source_item_text_result_checkpoint") or {}
        ),
        **(
            {"baseline_revision_artifact": product_baseline_pointer}
            if product_baseline_pointer is not None
            else {}
        ),
        "next_action": (
            "BASELINE_REVISION_PUBLISHED"
            if product_baseline_pointer is not None
            else "ASSEMBLE_BASELINE_REVISION"
        ),
    }
    from reqpilotx_server.application.agent_workflow.source_item_manual_publication import attach_rebound_baseline_manual_contexts
    if product_baseline_pointer is not None:
        attach_rebound_baseline_manual_contexts(result, container=container, project_id=project_id, document_id=document_id,
            previous_bundle=predecessor, document_map=current["document_map"], baseline=product_baseline.baseline)
    return result


def run_durable_source_item_scope_revision(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    payload = dict(durable_job.payload or {})
    if payload.get("schema_version") == SOURCE_ITEM_SCOPE_UNDO_JOB_SCHEMA_VERSION:
        return _run_durable_source_item_scope_undo(durable_job, context)
    if payload.get("schema_version") != SOURCE_ITEM_SCOPE_REVISION_JOB_SCHEMA_VERSION:
        raise NonRetryableJobError(
            error_code="source_item_scope_revision_job_schema_invalid",
            message="SourceItem scope revision job schema is invalid.",
        )
    container = RUNTIME_STATE.container
    executor = None if container is None else container.source_item_text_executor
    if container is None or executor is None or container.object_store is None:
        raise RuntimeError("SourceItem scope revision runtime is unavailable.")
    if (
        payload.get("executor_id") != executor.adapter_id
        or payload.get("executor_version") != executor.adapter_version
        or payload.get("transport_kind") != executor.transport_kind
        or payload.get("model_id") != executor.model_id
        or payload.get("reasoning_effort") != executor.reasoning_effort
    ):
        raise NonRetryableJobError(
            error_code="source_item_scope_revision_executor_changed",
            message="SourceItem scope revision executor changed after enqueue.",
        )
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    affected_scope_ids = tuple(
        str(item) for item in payload.get("affected_scope_ids") or ()
    )
    comparison_scope_ids = tuple(
        str(item) for item in payload.get("comparison_scope_ids") or ()
    )
    feedback_ids = tuple(str(item) for item in payload.get("feedback_ids") or ())
    try:
        current = _current_scope_revision_context(
            project_id=project_id,
            document_id=document_id,
            affected_scope_ids=affected_scope_ids,
            comparison_scope_ids=comparison_scope_ids,
            feedback_ids=feedback_ids,
            container=container,
        )
    except (ApplicationError, OSError, TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="source_item_scope_revision_context_changed",
            message="The pinned SourceItem revision context is no longer available.",
        ) from exc
    if not _current_scope_revision_matches_payload(current, payload):
        raise NonRetryableJobError(
            error_code="source_item_scope_revision_baseline_changed",
            message="Source, feedback, or baseline changed after revision enqueue.",
        )
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="source_item_scope_revision_cancelled",
            message="SourceItem scope revision was cancelled before execution.",
        )
    progress_state = dict(durable_job.progress or {})
    checkpoint_progress = NativeSourceItemTextCheckpointProgress(
        context=context,
        store=NativeSourceItemTextDurableCheckpointStore(
            object_store=container.object_store,
            project_id=project_id,
            document_id=document_id,
        ),
        initial=progress_state,
    )
    try:
        resume_checkpoint = checkpoint_progress.load()
    except ValueError as exc:
        raise NonRetryableJobError(
            error_code="source_item_scope_revision_checkpoint_invalid",
            message="Stored SourceItem revision checkpoint failed validation.",
        ) from exc
    latest_checkpoint = resume_checkpoint

    def save_checkpoint(value: Mapping[str, Any]) -> None:
        nonlocal latest_checkpoint
        latest_checkpoint = _validated_text_checkpoint(value)
        checkpoint_progress.save(latest_checkpoint)

    accepted_window_count = (
        len(resume_checkpoint.accepted_windows)
        if resume_checkpoint is not None
        else 0
    )
    accepted_boundary_count = (
        len(resume_checkpoint.accepted_boundaries)
        if resume_checkpoint is not None
        else 0
    )
    total_window_count = (
        len(resume_checkpoint.classification_windows)
        if resume_checkpoint is not None
        else 0
    )
    context.heartbeat(
        progress={
            **progress_state,
            "stage": (
                "RESUMING_SOURCE_ITEM_SCOPE_REVISION"
                if resume_checkpoint is not None
                else "RUNNING_SOURCE_ITEM_SCOPE_REVISION"
            ),
            "message": (
                f"正在恢复局部修订；已保留 {accepted_boundary_count}/"
                f"{len(current['affected_requests'])} 个正文边界和 "
                f"{accepted_window_count}/{total_window_count} 个分类窗口。"
                if resume_checkpoint is not None
                else "Agent 正在依据已保存指引形成受影响范围的完整替换。"
            ),
            "strategy": "TEXT_SCOPE_REVISION",
            "completed_window_count": accepted_window_count,
            "total_window_count": total_window_count,
            **(
                {
                    "source_item_text_window_checkpoint": (
                        checkpoint_progress.receipt.model_dump(mode="json")
                    )
                }
                if checkpoint_progress.receipt is not None
                else {}
            ),
        }
    )
    execution = executor.execute(
        SourceItemTextTaskV1(
            project_id=project_id,
            document_id=document_id,
            source_path=current["source_path"],
            document_map=current["document_map"].model_dump(mode="json"),
            scope_requests=tuple(
                item.model_dump(mode="json")
                for item in current["affected_requests"]
            ),
            timeout_seconds=int(payload.get("timeout_seconds") or 1200),
            max_concurrency=int(payload.get("max_concurrency") or 2),
            execution_context=current["agent_context"].model_dump(mode="json"),
            semantic_context=current.get("semantic_context"),
        ),
        cancellation_requested=context.cancellation_requested,
        resume_checkpoint=(
            None
            if resume_checkpoint is None
            else resume_checkpoint.model_dump(mode="json")
        ),
        checkpoint_sink=save_checkpoint,
    )
    batch, run_receipt = _validate_scope_revision_execution(
        execution,
        document_map=current["document_map"],
        requests=current["affected_requests"],
        agent_context=current["agent_context"],
    )
    candidate_by_scope = {item.scope_id: item for item in batch.candidates}
    candidates = tuple(
        candidate_by_scope[item.scope_id] for item in current["affected_requests"]
    )
    product_artifact = current.get("product_baseline_artifact")
    previous_scope_revision_receipt = (
        product_artifact.scope_revision_receipt
        if isinstance(product_artifact, SourceRequirementProductBaselineArtifactV1)
        else None
    )
    try:
        scope_revision = build_source_item_scope_revision(
            document_map=current["document_map"],
            current_artifact=current["baseline"],
            current_validation_receipt=current["validation_receipt"],
            operation=SourceItemScopeRevisionOperation.REPLACE,
            affected_scope_ids=current["affected_scope_ids"],
            comparison_scope_ids=current["comparison_scope_ids"],
            feedback_ids=current["guidance_ids"],
            text_requests=current["affected_requests"],
            text_candidates=candidates,
            previous_scope_revision_receipt=previous_scope_revision_receipt,
        )
        baseline, validation_receipt, revision_receipt = (
            compile_source_item_scope_revision(
                document_map=current["document_map"],
                current_artifact=current["baseline"],
                current_validation_receipt=current["validation_receipt"],
                scope_revision=scope_revision,
                text_requests=current["affected_requests"],
                text_candidates=candidates,
                previous_scope_revision_receipt=previous_scope_revision_receipt,
            )
        )
    except SourceRequirementBaselineError as exc:
        raise NonRetryableJobError(
            error_code="source_item_scope_revision_candidate_invalid",
            message="The Agent replacement failed affected-only Host validation.",
        ) from exc
    product = _build_product_scope_revision_artifact(
        document_map=current["document_map"],
        baseline=baseline,
        validation_receipt=validation_receipt,
        scope_revision_receipt=revision_receipt,
        previous_product_run_receipts=current["product_run_receipts"],
        run_receipt=run_receipt,
    )
    encoded = json.dumps(
        product.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    object_sha256 = hashlib.sha256(encoded).hexdigest()
    object_key = _product_baseline_object_key(
        project_id=project_id,
        document_id=document_id,
        artifact=product,
    )
    final_checkpoint_receipt = checkpoint_progress.receipt
    context.heartbeat(
        progress={
            **progress_state,
            "stage": "PERSISTING_SOURCE_ITEM_SCOPE_REVISION",
            "message": "Host 已验证局部替换；正在保存新的工作清单修订。",
            "strategy": "TEXT_SCOPE_REVISION",
            "completed_window_count": len(
                final_checkpoint_receipt.accepted_window_ids
                if final_checkpoint_receipt is not None
                else ()
            ),
            "total_window_count": (
                len(latest_checkpoint.classification_windows)
                if latest_checkpoint is not None
                else 0
            ),
            **(
                {
                    "source_item_text_window_checkpoint": (
                        final_checkpoint_receipt.model_dump(mode="json")
                    )
                }
                if final_checkpoint_receipt is not None
                else {}
            ),
        }
    )
    context.begin_publication()
    stored = container.object_store.put(
        key=object_key,
        source=BytesIO(encoded),
        expected_sha256=object_sha256,
        media_type=SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE,
    )
    if (
        stored.key != object_key
        or stored.sha256 != object_sha256
        or stored.size != len(encoded)
        or stored.media_type != SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE
    ):
        raise RuntimeError("SourceItem scope revision ObjectStore receipt drifted.")
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import (
        _carry_feedbacks_to_baseline,
    )

    if current["feedback_ids"]:
        _carry_feedbacks_to_baseline(
            source_sha256=current["source_sha256"],
            from_baseline_artifact_id=current["baseline"].artifact_id,
            to_baseline_artifact_id=baseline.artifact_id,
            feedback_ids=current["feedback_ids"],
            status="CONSUMED",
        )
    product_pointer = {
        "object_key": stored.key,
        "object_sha256": stored.sha256,
        "artifact_sha256": product.artifact_sha256,
        "baseline_artifact_id": baseline.artifact_id,
        "baseline_artifact_sha256": baseline.artifact_sha256,
        "baseline_revision": baseline.revision,
        "record_count": len(baseline.records),
        "size": stored.size,
        "media_type": stored.media_type,
    }
    result = {
        "schema_version": SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION,
        "status": "REVISION_COMPLETE",
        "source_sha256": current["source_sha256"],
        "native_artifact_id": current["native_artifact_id"],
        "native_projection_sha256": current["native_projection_sha256"],
        "document_map_artifact_id": current["document_map"].artifact_id,
        "document_map_artifact_sha256": current["document_map"].artifact_sha256,
        "scope_id": (
            current["affected_scope_ids"][0]
            if len(current["affected_scope_ids"]) == 1
            else f"scope-revision-{revision_receipt.receipt_sha256[:32]}"
        ),
        "scope_ids": list(current["affected_scope_ids"]),
        "affected_scope_ids": list(current["affected_scope_ids"]),
        "comparison_scope_ids": list(current["comparison_scope_ids"]),
        "feedback_ids": list(current["feedback_ids"]),
        "guidance_ids": list(current["guidance_ids"]),
        "request_set_sha256": current["request_set_sha256"],
        "candidate_sha256": run_receipt.candidate_sha256,
        "record_count": sum(
            1
            for item in baseline.records
            if (item.logical_table_scope_id or f"text:{item.node_id}")
            in current["affected_scope_ids"]
        ),
        "context_row_group_count": sum(
            len(item.structure_only_spans) for item in batch.candidates
        ),
        "executor_id": executor.adapter_id,
        "executor_version": executor.adapter_version,
        "transport_kind": executor.transport_kind,
        "baseline_revision_artifact": product_pointer,
        "predecessor_baseline_revision_artifact": current.get(
            "product_baseline_pointer"
        ),
        "scope_revision": {
            "operation": revision_receipt.operation.value,
            "matched_count": len(revision_receipt.matched_identities),
            "created_count": len(revision_receipt.created_identities),
            "tombstoned_count": len(revision_receipt.tombstoned_identities),
            "receipt_sha256": revision_receipt.receipt_sha256,
        },
        "next_action": "SCOPE_REVISION_PUBLISHED",
    }
    from reqpilotx_server.application.agent_workflow.source_item_manual_publication import attach_rebound_baseline_manual_contexts
    attach_rebound_baseline_manual_contexts(result, container=container, project_id=project_id, document_id=document_id,
        previous_bundle=current, document_map=current["document_map"], baseline=baseline)
    return result


def _run_durable_source_item_scope_undo(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    payload = dict(durable_job.payload or {})
    if payload.get("schema_version") != SOURCE_ITEM_SCOPE_UNDO_JOB_SCHEMA_VERSION:
        raise NonRetryableJobError(
            error_code="source_item_scope_undo_job_schema_invalid",
            message="SourceItem scope undo job schema is invalid.",
        )
    container = RUNTIME_STATE.container
    if (
        container is None
        or container.jobs is None
        or container.object_store is None
    ):
        raise RuntimeError("SourceItem scope undo runtime is unavailable.")
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    try:
        current = _current_scope_undo_context(
            project_id=project_id,
            document_id=document_id,
            container=container,
        )
    except (ApplicationError, OSError, TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="source_item_scope_undo_context_changed",
            message="The reversible SourceItem revision is no longer available.",
        ) from exc
    if not _current_scope_undo_matches_payload(current, payload):
        raise NonRetryableJobError(
            error_code="source_item_scope_undo_baseline_changed",
            message="SourceItem revision or direct predecessor changed after enqueue.",
        )
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="source_item_scope_undo_cancelled",
            message="SourceItem scope undo was cancelled before publication.",
        )
    context.heartbeat(
        progress={
            **dict(durable_job.progress or {}),
            "stage": "VALIDATING_SOURCE_ITEM_SCOPE_UNDO",
            "message": "Host 正在复验直接前驱与局部证据分区。",
            "strategy": "TEXT_SCOPE_UNDO",
            "completed_window_count": 0,
            "total_window_count": 0,
        }
    )
    try:
        scope_revision = build_source_item_scope_revision(
            document_map=current["document_map"],
            current_artifact=current["baseline"],
            current_validation_receipt=current["validation_receipt"],
            operation=SourceItemScopeRevisionOperation.UNDO,
            affected_scope_ids=current["affected_scope_ids"],
            comparison_scope_ids=current["comparison_scope_ids"],
            feedback_ids=current["guidance_ids"],
            undo_target_artifact=current["undo_target"].baseline,
            previous_scope_revision_receipt=current["scope_revision_receipt"],
        )
        baseline, validation_receipt, revision_receipt = (
            compile_source_item_scope_revision(
                document_map=current["document_map"],
                current_artifact=current["baseline"],
                current_validation_receipt=current["validation_receipt"],
                scope_revision=scope_revision,
                undo_target_artifact=current["undo_target"].baseline,
                previous_scope_revision_receipt=current[
                    "scope_revision_receipt"
                ],
            )
        )
    except SourceRequirementBaselineError as exc:
        raise NonRetryableJobError(
            error_code="source_item_scope_undo_validation_failed",
            message="The direct predecessor failed affected-only undo validation.",
        ) from exc
    product = _build_product_scope_revision_artifact(
        document_map=current["document_map"],
        baseline=baseline,
        validation_receipt=validation_receipt,
        scope_revision_receipt=revision_receipt,
        previous_product_run_receipts=current["product_run_receipts"],
        run_receipt=None,
    )
    encoded = json.dumps(
        product.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    object_sha256 = hashlib.sha256(encoded).hexdigest()
    object_key = _product_baseline_object_key(
        project_id=project_id,
        document_id=document_id,
        artifact=product,
    )
    context.begin_publication()
    stored = container.object_store.put(
        key=object_key,
        source=BytesIO(encoded),
        expected_sha256=object_sha256,
        media_type=SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE,
    )
    if (
        stored.key != object_key
        or stored.sha256 != object_sha256
        or stored.size != len(encoded)
        or stored.media_type != SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE
    ):
        raise RuntimeError("SourceItem scope undo ObjectStore receipt drifted.")
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import (
        _carry_feedbacks_to_baseline,
    )

    if current["feedback_ids"]:
        _carry_feedbacks_to_baseline(
            source_sha256=current["source_sha256"],
            from_baseline_artifact_id=current["baseline"].artifact_id,
            to_baseline_artifact_id=baseline.artifact_id,
            feedback_ids=current["feedback_ids"],
            status="ACKNOWLEDGED",
        )
    product_pointer = {
        "object_key": stored.key,
        "object_sha256": stored.sha256,
        "artifact_sha256": product.artifact_sha256,
        "baseline_artifact_id": baseline.artifact_id,
        "baseline_artifact_sha256": baseline.artifact_sha256,
        "baseline_revision": baseline.revision,
        "record_count": len(baseline.records),
        "size": stored.size,
        "media_type": stored.media_type,
    }
    result = {
        "schema_version": SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION,
        "status": "REVISION_COMPLETE",
        "source_sha256": current["source_sha256"],
        "document_map_artifact_id": current["document_map"].artifact_id,
        "document_map_artifact_sha256": current["document_map"].artifact_sha256,
        "scope_id": (
            current["affected_scope_ids"][0]
            if len(current["affected_scope_ids"]) == 1
            else f"scope-undo-{revision_receipt.receipt_sha256[:32]}"
        ),
        "scope_ids": list(current["affected_scope_ids"]),
        "affected_scope_ids": list(current["affected_scope_ids"]),
        "comparison_scope_ids": list(current["comparison_scope_ids"]),
        "feedback_ids": list(current["feedback_ids"]),
        "guidance_ids": list(current["guidance_ids"]),
        "candidate_sha256": current["undo_target"].baseline.artifact_sha256,
        "record_count": sum(
            1
            for item in baseline.records
            if (item.logical_table_scope_id or f"text:{item.node_id}")
            in current["affected_scope_ids"]
        ),
        "context_row_group_count": 0,
        "baseline_revision_artifact": product_pointer,
        "predecessor_baseline_revision_artifact": current[
            "product_baseline_pointer"
        ],
        "scope_revision": {
            "operation": revision_receipt.operation.value,
            "matched_count": len(revision_receipt.matched_identities),
            "created_count": len(revision_receipt.created_identities),
            "tombstoned_count": len(revision_receipt.tombstoned_identities),
            "receipt_sha256": revision_receipt.receipt_sha256,
        },
        "next_action": "SCOPE_UNDO_PUBLISHED",
    }
    from reqpilotx_server.application.agent_workflow.source_item_manual_publication import attach_rebound_baseline_manual_contexts
    attach_rebound_baseline_manual_contexts(result, container=container, project_id=project_id, document_id=document_id,
        previous_bundle=current, document_map=current["document_map"], baseline=baseline)
    return result


def _current_text_scope_context(
    *,
    project_id: str,
    document_id: str,
    node_ids: tuple[str, ...],
) -> dict[str, Any]:
    if not node_ids or len(node_ids) > 32 or len(node_ids) != len(set(node_ids)):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_text_scope_set_invalid",
                "message": "Select one to 32 unique direct-text structure nodes.",
            },
        )
    current = _current_reference_context(project_id, document_id)
    document_map = compile_document_map_artifact(current["public_reference"])
    requested = set(node_ids)
    selected_nodes = tuple(
        item for item in document_map.nodes if item.node_id in requested
    )
    if (
        {item.node_id for item in selected_nodes} != requested
        or any(not item.source_content for item in selected_nodes)
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_text_scope_unavailable",
                "message": "A selected node has no current direct-text evidence.",
            },
        )
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import (
        _load_base_reference,
    )

    base_bundle = _load_base_reference(project_id=project_id, document_id=document_id, source_sha256=current["source_sha256"])
    effective = resolve_effective_source_requirement_bundle(
        project_id=project_id,
        document_id=document_id,
        base_bundle=base_bundle,
    )
    scope_ids = tuple(f"text:{item.node_id}" for item in selected_nodes)
    already_covered = set(scope_ids).intersection(
        effective["baseline"].covered_scope_ids
    )
    if already_covered:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_text_scope_already_covered",
                "message": "The selected direct text is already in the working ledger.",
                "scope_ids": sorted(already_covered),
            },
        )
    requests = tuple(
        build_source_item_text_scope_request(
            document_map,
            node_id=item.node_id,
        )
        for item in selected_nodes
    )
    request_set_sha256 = canonical_json_sha256(
        [item.model_dump(mode="json") for item in requests]
    )
    source_path = resolve_source_record_path(current["document"]).resolve(strict=True)
    return {
        "document": current["document"],
        "source_path": source_path,
        "source_sha256": current["source_sha256"],
        "native_artifact_id": current["artifact_id"],
        "native_projection_sha256": document_map.native_projection_sha256,
        "document_map": document_map,
        "node_ids": tuple(item.node_id for item in selected_nodes),
        "scope_ids": scope_ids,
        "requests": requests,
        "request_set_sha256": request_set_sha256,
        "batch_scope_id": (
            scope_ids[0]
            if len(scope_ids) == 1
            else f"text-batch-{request_set_sha256[:32]}"
        ),
        "source_requirement_base_bundle": base_bundle,
        "semantic_context": _source_semantic_context(current, document_map, tuple(item.node_id for item in selected_nodes), source_path),
    }


def _source_semantic_context(current, document_map, node_ids, source_path, known_previews=None):
    from reqpilotx_server.application.agent_workflow.classification_context import build_classification_context, classification_scope_node_ids
    selected = classification_scope_node_ids(document_map, node_ids)
    scope_ids = {block.logical_table_scope_id for node in document_map.nodes if node.node_id in selected
                 for block in node.content_blocks if block.logical_table_scope_id}
    previews, unavailable = dict(known_previews or {}), []
    for scope_id in scope_ids:
        if scope_id in previews:
            continue
        try:
            previews[scope_id] = materialize_native_structure_table_preview(bundle=current["bundle"], source_sha256=document_map.source_sha256,
                scope_id=scope_id, projection_operations=_verified_projection_operations(current.get("handoff_attempts", ())), source_path=source_path)
        except (OSError, ValueError, KeyError, ApplicationError) as exc:
            unavailable.append({"scope_id": scope_id, "reason_code": type(exc).__name__})
    return build_classification_context(document_map, node_ids=node_ids, table_previews=previews, unavailable_tables=unavailable)


def _current_affected_scope_boundaries(
    *,
    baseline: SourceRequirementBaselineArtifactV1,
    affected_scope_ids: tuple[str, ...],
) -> tuple[SourceItemScopeRevisionCurrentScopeV1, ...]:
    boundaries: list[SourceItemScopeRevisionCurrentScopeV1] = []
    for scope_id in affected_scope_ids:
        records = tuple(
            SourceItemScopeRevisionCurrentRecordV1(
                source_record_id=record.source_record_id,
                source_display_id=record.source_display_id,
                requirement_display_id=record.requirement_display_id,
                label=record.label,
                source_form=record.source_form,
                text_spans=tuple(
                    SourceItemTextSpanV1(
                        content_id=binding.content_id,
                        start_offset=binding.start_offset,
                        end_offset=binding.end_offset,
                    )
                    for binding in record.text_bindings
                ),
                shared_source_content_ids=record.shared_source_content_ids,
                requirementness=record.requirementness,
                requirement_kinds=record.requirement_kinds,
                non_requirement_role=record.non_requirement_role,
                confidence=record.confidence,
                decision_summary=record.decision_summary,
            )
            for record in baseline.records
            if not record.logical_table_scope_id
            and f"text:{record.node_id}" == scope_id
        )
        structure_only_spans = tuple(
            SourceItemTextSpanV1(
                content_id=item.content_id,
                start_offset=item.start_offset,
                end_offset=item.end_offset,
            )
            for item in baseline.text_context_dispositions
            if item.scope_id == scope_id
        )
        boundaries.append(
            SourceItemScopeRevisionCurrentScopeV1(
                scope_id=scope_id,
                records=records,
                structure_only_spans=structure_only_spans,
            )
        )
    return tuple(boundaries)


def _current_scope_revision_context(
    *,
    project_id: str,
    document_id: str,
    affected_scope_ids: tuple[str, ...],
    comparison_scope_ids: tuple[str, ...],
    feedback_ids: tuple[str, ...],
    container: Any,
) -> dict[str, Any]:
    current = _current_reference_context(project_id, document_id)
    document_map = compile_document_map_artifact(current["public_reference"])
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import (
        _load_base_reference,
        _read_feedbacks,
    )

    base_bundle = _load_base_reference(project_id=project_id, document_id=document_id, source_sha256=current["source_sha256"])
    effective = resolve_effective_source_requirement_bundle(
        project_id=project_id,
        document_id=document_id,
        base_bundle=base_bundle,
        container=container,
    )
    baseline = effective["baseline"]
    validation_receipt = effective["receipt"]
    affected_scope_ids = tuple(sorted(set(affected_scope_ids)))
    comparison_scope_ids = tuple(sorted(set(comparison_scope_ids)))
    feedback_ids = tuple(sorted(set(feedback_ids)))
    all_scope_ids = affected_scope_ids + comparison_scope_ids
    if (
        not affected_scope_ids
        or set(affected_scope_ids).intersection(comparison_scope_ids)
        or any(not item.startswith("text:") for item in all_scope_ids)
        or not set(all_scope_ids).issubset(baseline.covered_scope_ids)
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_scope_revision_scope_invalid",
                "message": (
                    "Affected and comparison scopes must be disjoint covered "
                    "direct-text scopes."
                ),
            },
        )
    node_by_id = {item.node_id: item for item in document_map.nodes}

    def request_for_scope(scope_id: str) -> SourceItemTextScopeRequestV1:
        node_id = scope_id.removeprefix("text:")
        node = node_by_id.get(node_id)
        if node is None or not node.source_content:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "source_item_scope_revision_evidence_unavailable",
                    "message": "A revision scope has no current direct-text evidence.",
                },
            )
        return build_source_item_text_scope_request(document_map, node_id=node_id)

    affected_requests = tuple(request_for_scope(item) for item in affected_scope_ids)
    comparison_requests = tuple(
        request_for_scope(item) for item in comparison_scope_ids
    )
    feedback_by_id = {
        str(item.get("feedback_id") or ""): item
        for item in _read_feedbacks(
            source_sha256=current["source_sha256"],
            baseline_artifact_id=baseline.artifact_id,
        )
    }
    if any(item not in feedback_by_id for item in feedback_ids):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_scope_revision_feedback_changed",
                "message": "A selected feedback item is no longer bound to this baseline.",
            },
        )
    record_by_id = {item.source_record_id: item for item in baseline.records}
    guidance_items: list[SourceItemScopeRevisionGuidanceV1] = []
    for feedback_id in feedback_ids:
        value = feedback_by_id[feedback_id]
        source_record_id = str(value.get("source_record_id") or "")
        record = record_by_id.get(source_record_id)
        scope_id = (
            ""
            if record is None
            else record.logical_table_scope_id or f"text:{record.node_id}"
        )
        if record is None or scope_id not in affected_scope_ids:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "source_item_scope_revision_feedback_scope_drifted",
                    "message": "Feedback no longer resolves inside an affected scope.",
                },
            )
        guidance_items.append(
            SourceItemScopeRevisionGuidanceV1(
                guidance_id=feedback_id,
                origin="USER_FEEDBACK",
                source_record_id=source_record_id,
                scope_id=scope_id,
                message=str(value.get("message") or "").strip(),
            )
        )
    guided_scope_ids = {item.scope_id for item in guidance_items}
    for scope_id in affected_scope_ids:
        if scope_id in guided_scope_ids:
            continue
        guidance_sha256 = canonical_json_sha256(
            {
                "schema_version": "source-item-scope-review-request/v1",
                "baseline_artifact_sha256": baseline.artifact_sha256,
                "scope_id": scope_id,
            }
        )
        guidance_items.append(
            SourceItemScopeRevisionGuidanceV1(
                guidance_id=f"source-item-scope-review-{guidance_sha256[:32]}",
                origin="HOST_REVIEW_REQUEST",
                source_record_id=None,
                scope_id=scope_id,
                message=(
                    "Re-evaluate this affected scope against the accepted Document "
                    "Map and source evidence. Return the best complete source-faithful "
                    "replacement even when no user feedback was provided."
                ),
            )
        )
    guidance_items.sort(key=lambda item: item.guidance_id)
    guidance_ids = tuple(item.guidance_id for item in guidance_items)
    current_affected_scopes = _current_affected_scope_boundaries(
        baseline=baseline,
        affected_scope_ids=affected_scope_ids,
    )
    context_values = {
        "source_sha256": current["source_sha256"],
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "current_baseline_artifact_id": baseline.artifact_id,
        "current_baseline_artifact_sha256": baseline.artifact_sha256,
        "current_baseline_revision": baseline.revision,
        "affected_scope_ids": affected_scope_ids,
        "comparison_scope_ids": comparison_scope_ids,
        "guidance_items": tuple(guidance_items),
        "current_affected_scopes": current_affected_scopes,
        "comparison_scopes": comparison_requests,
    }
    context_draft = SourceItemScopeRevisionAgentContextV2.model_construct(
        **context_values,
        context_sha256="0" * 64,
    )
    agent_context = SourceItemScopeRevisionAgentContextV2(
        **context_values,
        context_sha256=canonical_json_sha256(
            context_draft.model_dump(mode="json", exclude={"context_sha256"})
        ),
    )
    request_set_sha256 = source_item_text_request_set_sha256(
        affected_requests,
        execution_context=agent_context.model_dump(mode="json"),
    )
    return {
        "document": current["document"],
        "source_path": resolve_source_record_path(current["document"]).resolve(
            strict=True
        ),
        "source_sha256": current["source_sha256"],
        "native_artifact_id": current["artifact_id"],
        "native_projection_sha256": document_map.native_projection_sha256,
        "document_map": document_map,
        "baseline": baseline,
        "validation_receipt": validation_receipt,
        "affected_scope_ids": affected_scope_ids,
        "comparison_scope_ids": comparison_scope_ids,
        "feedback_ids": feedback_ids,
        "guidance_ids": guidance_ids,
        "affected_requests": affected_requests,
        "comparison_requests": comparison_requests,
        "agent_context": agent_context,
        "semantic_context": _source_semantic_context(current, document_map, tuple(item.node_id for item in affected_requests), resolve_source_record_path(current["document"]).resolve(strict=True)),
        "request_set_sha256": request_set_sha256,
        "product_run_receipts": tuple(effective.get("product_run_receipts", ())),
        "product_baseline_artifact": effective.get("product_baseline_artifact"),
        "product_baseline_pointer": effective.get("product_baseline_pointer"),
        "source_requirement_base_bundle": base_bundle,
    }


def _current_scope_revision_matches_payload(
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> bool:
    document_map = current["document_map"]
    baseline = current["baseline"]
    validation_receipt = current["validation_receipt"]
    return (
        current["source_sha256"] == payload.get("source_sha256")
        and current["native_artifact_id"] == payload.get("native_artifact_id")
        and current["native_projection_sha256"]
        == payload.get("native_projection_sha256")
        and document_map.artifact_id == payload.get("document_map_artifact_id")
        and document_map.artifact_sha256
        == payload.get("document_map_artifact_sha256")
        and baseline.artifact_id == payload.get("current_baseline_artifact_id")
        and baseline.artifact_sha256
        == payload.get("current_baseline_artifact_sha256")
        and baseline.revision == payload.get("current_baseline_revision")
        and validation_receipt.receipt_sha256
        == payload.get("current_validation_receipt_sha256")
        and list(current["affected_scope_ids"])
        == payload.get("affected_scope_ids")
        and list(current["comparison_scope_ids"])
        == payload.get("comparison_scope_ids")
        and list(current["feedback_ids"]) == payload.get("feedback_ids")
        and list(current["guidance_ids"]) == payload.get("guidance_ids")
        and current["request_set_sha256"] == payload.get("request_set_sha256")
        and current["agent_context"].context_sha256
        == payload.get("agent_context_sha256")
    )


def _current_scope_undo_context(
    *,
    project_id: str,
    document_id: str,
    container: Any,
) -> dict[str, Any]:
    current = _current_reference_context(project_id, document_id)
    document_map = compile_document_map_artifact(current["public_reference"])
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import (
        _load_base_reference,
    )

    base_bundle = _load_base_reference(project_id=project_id, document_id=document_id, source_sha256=current["source_sha256"])
    effective = resolve_effective_source_requirement_bundle(
        project_id=project_id,
        document_id=document_id,
        base_bundle=base_bundle,
        container=container,
    )
    product = effective.get("product_baseline_artifact")
    current_pointer = effective.get("product_baseline_pointer")
    if (
        not isinstance(product, SourceRequirementProductBaselineArtifactV1)
        or product.scope_revision_receipt is None
        or not isinstance(current_pointer, Mapping)
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_scope_undo_unavailable",
                "message": "The current working baseline has no reversible scope revision.",
            },
        )
    latest = container.jobs.get_latest_succeeded(
        kind=SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    result = dict(latest.result or {}) if latest is not None else {}
    latest_pointer = result.get("baseline_revision_artifact")
    predecessor_pointer = result.get("predecessor_baseline_revision_artifact")
    if (
        not isinstance(latest_pointer, Mapping)
        or dict(latest_pointer) != dict(current_pointer)
        or not isinstance(predecessor_pointer, Mapping)
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_scope_undo_lineage_unavailable",
                "message": "The direct predecessor product artifact is unavailable.",
            },
        )
    undo_target = _load_product_baseline_artifact(
        container.object_store,
        predecessor_pointer,
        project_id=project_id,
        document_id=document_id,
    )
    receipt = product.scope_revision_receipt
    if (
        undo_target.baseline.artifact_id != receipt.predecessor_artifact_id
        or undo_target.baseline.artifact_sha256
        != receipt.predecessor_artifact_sha256
        or undo_target.baseline.revision != receipt.predecessor_artifact_revision
        or undo_target.document_map_artifact_sha256
        != document_map.artifact_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_item_scope_undo_target_drifted",
                "message": "The direct predecessor no longer matches the revision receipt.",
            },
        )
    return {
        "document": current["document"],
        "source_sha256": current["source_sha256"],
        "document_map": document_map,
        "baseline": product.baseline,
        "validation_receipt": product.validation_receipt,
        "scope_revision_receipt": receipt,
        "affected_scope_ids": receipt.affected_scope_ids,
        "comparison_scope_ids": receipt.comparison_scope_ids,
        "feedback_ids": tuple(
            item
            for item in receipt.feedback_ids
            if item.startswith("source-requirement-feedback-")
        ),
        "guidance_ids": receipt.feedback_ids,
        "undo_target": undo_target,
        "product_run_receipts": product.product_run_receipts,
        "product_baseline_pointer": dict(current_pointer),
    }


def _current_scope_undo_matches_payload(
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> bool:
    document_map = current["document_map"]
    baseline = current["baseline"]
    receipt = current["scope_revision_receipt"]
    target = current["undo_target"].baseline
    return (
        current["source_sha256"] == payload.get("source_sha256")
        and document_map.artifact_id == payload.get("document_map_artifact_id")
        and document_map.artifact_sha256
        == payload.get("document_map_artifact_sha256")
        and baseline.artifact_id == payload.get("current_baseline_artifact_id")
        and baseline.artifact_sha256
        == payload.get("current_baseline_artifact_sha256")
        and baseline.revision == payload.get("current_baseline_revision")
        and current["validation_receipt"].receipt_sha256
        == payload.get("current_validation_receipt_sha256")
        and receipt.receipt_sha256
        == payload.get("scope_revision_receipt_sha256")
        and list(current["affected_scope_ids"])
        == payload.get("affected_scope_ids")
        and list(current["comparison_scope_ids"])
        == payload.get("comparison_scope_ids")
        and list(current["feedback_ids"]) == payload.get("feedback_ids")
        and list(current["guidance_ids"]) == payload.get("guidance_ids")
        and target.artifact_id == payload.get("undo_target_artifact_id")
        and target.artifact_sha256 == payload.get("undo_target_artifact_sha256")
        and target.revision == payload.get("undo_target_artifact_revision")
    )


def _current_text_scope_matches_payload(
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> bool:
    document_map = current["document_map"]
    return (
        current["source_sha256"] == payload.get("source_sha256")
        and current["native_artifact_id"] == payload.get("native_artifact_id")
        and current["native_projection_sha256"]
        == payload.get("native_projection_sha256")
        and document_map.artifact_id == payload.get("document_map_artifact_id")
        and document_map.artifact_sha256
        == payload.get("document_map_artifact_sha256")
        and list(current["node_ids"]) == payload.get("node_ids")
        and list(current["scope_ids"]) == payload.get("scope_ids")
        and current["batch_scope_id"] == payload.get("scope_id")
        and current["request_set_sha256"] == payload.get("request_set_sha256")
    )


def _validate_text_execution(
    execution: SourceItemTextExecutionV1,
    *,
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    execution_context: Mapping[str, object] | None = None,
) -> tuple[SourceItemTextBatchCandidateV1, NativeSourceItemTextRunReceiptV1]:
    try:
        batch = SourceItemTextBatchCandidateV1.model_validate(execution.batch)
        receipt = NativeSourceItemTextRunReceiptV1.model_validate(
            execution.run_receipt
        )
        request_by_scope = {item.scope_id: item for item in requests}
        candidate_by_scope = {item.scope_id: item for item in batch.candidates}
        if set(request_by_scope) != set(candidate_by_scope):
            raise ValueError("source-item text batch scope set drifted")
        for scope_id, request in request_by_scope.items():
            validate_source_item_text_slice_candidate(
                request, candidate_by_scope[scope_id]
            )
    except (TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="source_item_text_executor_result_invalid",
            message="Direct-text SourceItem executor returned an invalid result.",
        ) from exc
    request_set_sha256 = source_item_text_request_set_sha256(
        requests, execution_context=execution_context
    )
    if (
        batch.source_sha256 != document_map.source_sha256
        or batch.document_map_artifact_id != document_map.artifact_id
        or batch.document_map_artifact_sha256 != document_map.artifact_sha256
        or receipt.source_sha256 != document_map.source_sha256
        or receipt.document_map_artifact_id != document_map.artifact_id
        or receipt.document_map_artifact_sha256 != document_map.artifact_sha256
        or receipt.request_set_sha256 != request_set_sha256
        or tuple(receipt.scope_ids)
        != tuple(sorted(item.scope_id for item in requests))
        or receipt.candidate_sha256
        != canonical_json_sha256(batch.model_dump(mode="json"))
    ):
        raise NonRetryableJobError(
            error_code="source_item_text_executor_result_drifted",
            message="Direct-text SourceItem result drifted from its pinned scopes.",
        )
    return batch, receipt


def _validate_scope_revision_execution(
    execution: SourceItemTextExecutionV1,
    *,
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    agent_context: SourceItemScopeRevisionAgentContextV2,
) -> tuple[SourceItemTextBatchCandidateV1, NativeSourceItemTextRunReceiptV1]:
    try:
        batch = SourceItemTextBatchCandidateV1.model_validate(execution.batch)
        receipt = NativeSourceItemTextRunReceiptV1.model_validate(
            execution.run_receipt
        )
        request_by_scope = {item.scope_id: item for item in requests}
        candidate_by_scope = {item.scope_id: item for item in batch.candidates}
        if set(request_by_scope) != set(candidate_by_scope):
            raise ValueError("scope revision candidate scope set drifted")
        for scope_id, request in request_by_scope.items():
            validate_source_item_text_slice_candidate(
                request,
                candidate_by_scope[scope_id],
            )
    except (TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="source_item_scope_revision_executor_result_invalid",
            message="SourceItem revision executor returned an invalid replacement.",
        ) from exc
    request_set_sha256 = source_item_text_request_set_sha256(
        requests,
        execution_context=agent_context.model_dump(mode="json"),
    )
    if (
        batch.source_sha256 != document_map.source_sha256
        or batch.document_map_artifact_id != document_map.artifact_id
        or batch.document_map_artifact_sha256 != document_map.artifact_sha256
        or receipt.source_sha256 != document_map.source_sha256
        or receipt.document_map_artifact_id != document_map.artifact_id
        or receipt.document_map_artifact_sha256 != document_map.artifact_sha256
        or receipt.request_set_sha256 != request_set_sha256
        or tuple(receipt.scope_ids)
        != tuple(sorted(item.scope_id for item in requests))
        or receipt.candidate_sha256
        != canonical_json_sha256(batch.model_dump(mode="json"))
    ):
        raise NonRetryableJobError(
            error_code="source_item_scope_revision_executor_result_drifted",
            message="SourceItem revision result drifted from its private guidance.",
        )
    return batch, receipt


def _build_text_result_artifact(
    *,
    document_map: DocumentMapArtifactV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    batch: SourceItemTextBatchCandidateV1,
    run_receipt: NativeSourceItemTextRunReceiptV1,
) -> SourceItemTextScopeResultArtifactV1:
    values = {
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "requests": requests,
        "batch": batch,
        "run_receipt": run_receipt,
    }
    draft = SourceItemTextScopeResultArtifactV1.model_construct(
        **values,
        artifact_sha256="0" * 64,
    )
    return SourceItemTextScopeResultArtifactV1(
        **values,
        artifact_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"artifact_sha256"})
        ),
    )


def _store_text_result_artifact(
    object_store: Any,
    *,
    project_id: str,
    document_id: str,
    artifact: SourceItemTextScopeResultArtifactV1,
) -> dict[str, Any]:
    encoded = json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    object_sha256 = hashlib.sha256(encoded).hexdigest()
    object_key = _text_result_object_key(
        project_id=project_id,
        document_id=document_id,
        artifact=artifact,
    )
    stored = object_store.put(
        key=object_key,
        source=BytesIO(encoded),
        expected_sha256=object_sha256,
        media_type=SOURCE_ITEM_TEXT_SCOPE_ARTIFACT_MEDIA_TYPE,
    )
    if (
        stored.key != object_key
        or stored.sha256 != object_sha256
        or stored.size != len(encoded)
        or stored.media_type != SOURCE_ITEM_TEXT_SCOPE_ARTIFACT_MEDIA_TYPE
    ):
        raise RuntimeError("direct-text SourceItem result receipt drifted")
    return {
        "object_key": stored.key,
        "object_sha256": stored.sha256,
        "artifact_sha256": artifact.artifact_sha256,
        "request_set_sha256": artifact.run_receipt.request_set_sha256,
        "size": stored.size,
        "media_type": stored.media_type,
    }


def _load_text_result_checkpoint(
    progress: Mapping[str, Any],
    *,
    object_store: Any,
    project_id: str,
    document_id: str,
    current: Mapping[str, Any],
) -> SourceItemTextScopeResultArtifactV1 | None:
    pointer = progress.get("source_item_text_result_checkpoint")
    if not isinstance(pointer, Mapping):
        return None
    artifact = _load_text_scope_result_artifact(
        object_store,
        pointer,
        project_id=project_id,
        document_id=document_id,
    )
    if (
        artifact.run_receipt.request_set_sha256 != current["request_set_sha256"]
        or tuple(item.scope_id for item in artifact.requests)
        != tuple(current["scope_ids"])
        or artifact.document_map_artifact_id != current["document_map"].artifact_id
        or artifact.document_map_artifact_sha256
        != current["document_map"].artifact_sha256
    ):
        raise ValueError("direct-text SourceItem checkpoint identity drifted")
    return artifact


def _current_scope_context(
    *,
    project_id: str,
    document_id: str,
    logical_table_scope_id: str,
) -> dict[str, Any]:
    current = _current_reference_context(project_id, document_id)
    document_map = compile_document_map_artifact(current["public_reference"])
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import (
        _load_base_reference,
    )

    source_requirement_base_bundle = _load_base_reference(
        project_id=project_id, document_id=document_id, source_sha256=current["source_sha256"]
    )
    try:
        source_path = resolve_source_record_path(current["document"]).resolve(
            strict=True
        )
        table_preview = materialize_native_structure_table_preview(
            bundle=current["bundle"],
            source_sha256=current["source_sha256"],
            scope_id=logical_table_scope_id,
            projection_operations=_verified_projection_operations(
                current["handoff_attempts"]
            ),
            source_path=source_path,
        )
        table = next(
            item
            for item in document_map.logical_tables
            if item.scope_id == logical_table_scope_id
        )
        evidence_recovery = dict(table_preview.get("evidence_recovery") or {})
        if table.capture_gap and evidence_recovery.get("status") != "RECOVERED":
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "source_requirement_scope_evidence_recovery_required",
                    "message": (
                        "This table has a source-capture gap. Recover its exact "
                        "cells before asking Agent to decide SourceItem boundaries."
                    ),
                    "logical_table_scope_id": logical_table_scope_id,
                    "capture_gap": table.capture_gap,
                },
            )
        request = build_source_requirement_scope_request(document_map, table_preview)
    except ApplicationError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "source_requirement_scope_evidence_unavailable",
                "message": (
                    "The selected table is visible, but its complete execution "
                    "evidence could not be materialized. Existing SourceItems remain "
                    "available."
                ),
            },
        ) from exc
    return {
        "document": current["document"],
        "source_path": source_path,
        "source_sha256": current["source_sha256"],
        "native_artifact_id": current["artifact_id"],
        "native_projection_sha256": document_map.native_projection_sha256,
        "document_map": document_map,
        "request": request,
        "evidence_recovery": evidence_recovery,
        "source_requirement_base_bundle": source_requirement_base_bundle,
        "semantic_context": _source_semantic_context(current, document_map, (request.node_id,), source_path, {request.logical_table_scope_id: table_preview}),
    }


def _current_scope_matches_payload(
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> bool:
    document_map = current["document_map"]
    request = current["request"]
    return (
        current["source_sha256"] == payload.get("source_sha256")
        and current["native_artifact_id"] == payload.get("native_artifact_id")
        and current["native_projection_sha256"]
        == payload.get("native_projection_sha256")
        and document_map.artifact_id == payload.get("document_map_artifact_id")
        and document_map.artifact_sha256
        == payload.get("document_map_artifact_sha256")
        and request.request_sha256 == payload.get("request_sha256")
        and str(current["evidence_recovery"].get("receipt_sha256") or "")
        == payload.get("evidence_recovery_receipt_sha256")
        and request.logical_table_scope_id
        == payload.get("logical_table_scope_id")
    )


def _validated_checkpoint(value: Mapping[str, Any]):  # type: ignore[no-untyped-def]
    from reqpilotx_server.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
        NativeSourceRequirementWindowedCheckpointV1,
    )

    return NativeSourceRequirementWindowedCheckpointV1.model_validate(value)


def _validated_text_checkpoint(value: Mapping[str, Any]):  # type: ignore[no-untyped-def]
    from reqpilotx_server.adapters.agents.codex_app_server.native_source_item_text_windowed import (
        NativeSourceItemTextWindowedCheckpointV1,
    )

    return NativeSourceItemTextWindowedCheckpointV1.model_validate(value)


def _validate_execution(
    execution: SourceRequirementWindowExecutionV1,
    *,
    request: SourceRequirementScopeRequestV1,
) -> tuple[SourceRequirementSliceCandidateV1, NativeSourceRequirementRunReceiptV1]:
    try:
        candidate = SourceRequirementSliceCandidateV1.model_validate(
            execution.candidate
        )
        run_receipt = NativeSourceRequirementRunReceiptV1.model_validate(
            execution.run_receipt
        )
        validate_source_requirement_slice_candidate(request, candidate)
    except (TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="source_requirement_baseline_executor_result_invalid",
            message="SourceItem executor returned an invalid result.",
        ) from exc
    if (
        run_receipt.source_sha256 != request.source_sha256
        or run_receipt.document_map_artifact_id
        != request.document_map_artifact_id
        or run_receipt.document_map_artifact_sha256
        != request.document_map_artifact_sha256
        or run_receipt.request_sha256 != request.request_sha256
        or run_receipt.logical_table_scope_id
        != request.logical_table_scope_id
    ):
        raise NonRetryableJobError(
            error_code="source_requirement_baseline_executor_result_drifted",
            message="SourceItem executor result drifted from the pinned scope.",
        )
    return candidate, run_receipt


def _build_result_artifact(
    *,
    document_map: DocumentMapArtifactV1,
    request: SourceRequirementScopeRequestV1,
    candidate: SourceRequirementSliceCandidateV1,
    run_receipt: NativeSourceRequirementRunReceiptV1,
) -> SourceRequirementScopeResultArtifactV1:
    values = {
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "request": request,
        "candidate": candidate,
        "run_receipt": run_receipt,
    }
    draft = SourceRequirementScopeResultArtifactV1.model_construct(
        **values,
        artifact_sha256="0" * 64,
    )
    return SourceRequirementScopeResultArtifactV1(
        **values,
        artifact_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"artifact_sha256"})
        ),
    )


def resolve_effective_source_requirement_bundle(
    *,
    project_id: str,
    document_id: str,
    base_bundle: Mapping[str, Any],
    container: Any | None = None,
) -> dict[str, Any]:
    """Overlay the latest durable product revision on the development reference.

    The static reference remains the immutable predecessor. A successful scope job
    either points at a published product baseline or, for the first pre-cutover P24
    run, can be deterministically appended from its private scope artifact without
    another Agent turn.
    """

    if container is None:
        from reqpilotx_server.application.runtime import RUNTIME_STATE

        container = RUNTIME_STATE.container
    resolved = dict(base_bundle)
    resolved["product_run_receipts"] = ()
    if container is None or container.jobs is None:
        return resolved
    latest = _latest_job(
        container.jobs.get_latest_succeeded(
            kind=SOURCE_REQUIREMENT_BASELINE_JOB_KIND,
            aggregate_type="sor-document",
            aggregate_id=document_id,
        ),
        container.jobs.get_latest_succeeded(
            kind=SOURCE_ITEM_TEXT_BASELINE_JOB_KIND,
            aggregate_type="sor-document",
            aggregate_id=document_id,
        ),
        container.jobs.get_latest_succeeded(
            kind=SOURCE_ITEM_SCOPE_REVISION_JOB_KIND,
            aggregate_type="sor-document",
            aggregate_id=document_id,
        ),
        container.jobs.get_latest_succeeded(kind="agent.source-item.classify", aggregate_type="sor-document", aggregate_id=document_id),
        container.jobs.get_latest_succeeded(kind="agent.source-item.images", aggregate_type="sor-document", aggregate_id=document_id),
        container.jobs.get_latest_succeeded(kind="agent.source-item.direct-revision", aggregate_type="sor-document", aggregate_id=document_id),
        container.jobs.get_latest_succeeded(kind="agent.source-item.loop", aggregate_type="sor-document", aggregate_id=document_id),
        container.jobs.get_latest_succeeded(kind="agent.source-item.extract-loop", aggregate_type="sor-document", aggregate_id=document_id,
            require_result_key="baseline_revision_artifact"),
        prefer_baseline_revision=True,
    )
    if latest is None:
        return resolved
    native_publication = base_bundle.get("native_publication")
    if native_publication:
        pointer = (latest.result or {}).get("baseline_revision_artifact") or {}
        candidate_revision = int(pointer.get("baseline_revision") or (
            base_bundle["baseline"].revision if (latest.result or {}).get("baseline_sha256") == base_bundle["baseline"].artifact_sha256 else 0))
        native_order = (base_bundle["baseline"].revision, str(native_publication["updated_at"]))
        if native_order >= (candidate_revision, latest.updated_at.isoformat()):
            return resolved
    result = dict(latest.result or {})
    baseline_pointer = result.get("baseline_revision_artifact")
    if isinstance(baseline_pointer, Mapping):
        product = _load_product_baseline_artifact(
            container.object_store,
            baseline_pointer,
            project_id=project_id,
            document_id=document_id,
        )
    else:
        scope_pointer = result.get("private_artifact")
        if not isinstance(scope_pointer, Mapping):
            if latest.kind in {"agent.source-item.loop", "agent.source-item.extract-loop"}:
                from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import load_native_loop_artifact
                annotations, _ = load_native_loop_artifact(container, project_id, document_id, resolved["baseline"], "annotations_artifact")
                if annotations is not None:
                    from reqpilotx_server.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
                    resolved["image_annotations"] = rebind_source_item_image_annotations(resolved["document_map"], resolved["baseline"], annotations)
            return resolved
        if result.get("scope_ids"):
            scope = _load_text_scope_result_artifact(
                container.object_store,
                scope_pointer,
                project_id=project_id,
                document_id=document_id,
            )
            product = _append_product_text_baseline_artifact(
                document_map=base_bundle["document_map"],
                previous_artifact=base_bundle["baseline"],
                previous_validation_receipt=base_bundle["receipt"],
                requests=scope.requests,
                batch=scope.batch,
                run_receipt=scope.run_receipt,
                previous_product_run_receipts=(),
            )
        else:
            scope = _load_scope_result_artifact(
                container.object_store,
                scope_pointer,
                project_id=project_id,
                document_id=document_id,
            )
            product = _append_product_baseline_artifact(
                document_map=base_bundle["document_map"],
                previous_artifact=base_bundle["baseline"],
                previous_validation_receipt=base_bundle["receipt"],
                request=scope.request,
                candidate=scope.candidate,
                run_receipt=scope.run_receipt,
                previous_product_run_receipts=(),
            )
    if (
        product.source_sha256 != base_bundle["document_map"].source_sha256
        or product.document_map_artifact_id
        != base_bundle["document_map"].artifact_id
        or product.document_map_artifact_sha256
        != base_bundle["document_map"].artifact_sha256
    ):
        raise ValueError("product SourceItem baseline drifted from current source")
    manifest = dict(base_bundle["manifest"])
    manifest.update(
        {
            "baseline_artifact_id": product.baseline.artifact_id,
            "baseline_artifact_sha256": product.baseline.artifact_sha256,
            "authority": "AGENT_PRODUCT_WORKING_BASELINE",
        }
    )
    resolved.update(
        {
            "manifest": manifest,
            "baseline": product.baseline,
            "receipt": product.validation_receipt,
            "run_receipts": list(base_bundle.get("run_receipts", ()))
            + [
                item.model_dump(mode="json")
                for item in product.product_run_receipts
            ],
            "product_run_receipts": product.product_run_receipts,
            "product_baseline_artifact": product,
            "product_baseline_pointer": (
                dict(baseline_pointer)
                if isinstance(baseline_pointer, Mapping)
                else None
            ),
            "product_predecessor_pointer": (
                dict(result["predecessor_baseline_revision_artifact"])
                if isinstance(
                    result.get("predecessor_baseline_revision_artifact"),
                    Mapping,
                )
                else None
            ),
        }
    )
    image_job = _latest_job(
        container.jobs.get_latest_succeeded(kind="agent.source-item.images", aggregate_type="sor-document", aggregate_id=document_id),
        container.jobs.get_latest_succeeded(kind="agent.source-item.direct-revision", aggregate_type="sor-document", aggregate_id=document_id),
        container.jobs.get_latest_succeeded(kind="agent.source-item.loop", aggregate_type="sor-document", aggregate_id=document_id),
        prefer_baseline_revision=True)
    image_pointer = (image_job.result or {}).get("image_annotations_artifact") if image_job else None
    if (isinstance(image_pointer, Mapping) and image_job.payload.get("source_sha256") == product.source_sha256
        and (image_job.result or {}).get("document_map_artifact_sha256") == product.document_map_artifact_sha256):
        from reqpilotx_server.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
        encoded = _read_private_artifact(container.object_store, image_pointer,
            required_prefix=f"runtime/agent-source-requirement-baselines/{_safe_token(project_id)}/{_safe_token(document_id)}/",
            expected_media_type="application/vnd.reqpilotx.source-item-image-annotations+json")
        resolved["image_annotations"] = rebind_source_item_image_annotations(base_bundle["document_map"], product.baseline, json.loads(encoded))
    from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import load_native_loop_artifact
    loop_annotations, _ = load_native_loop_artifact(container, project_id, document_id, product.baseline, "annotations_artifact")
    if loop_annotations is not None:
        from reqpilotx_server.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
        resolved["image_annotations"] = rebind_source_item_image_annotations(base_bundle["document_map"], product.baseline, loop_annotations)
    return resolved


def publish_agent_source_item_revision(*, project_id: str, document_id: str, document_map: DocumentMapArtifactV1,
    previous_bundle: Mapping[str, Any], baseline: SourceRequirementBaselineArtifactV1,
    validation_receipt: SourceRequirementBaselineValidationReceiptV1, operation: str, affected_scope_ids: tuple[str, ...],
    runtime_receipts: tuple[Mapping[str, Any], ...], request_sha256: str, candidate_sha256: str,
    domain_proof: Mapping[str, Any], context: JobExecutionContext, image_annotations: Mapping[str, Any] | None = None,
    container: Any | None = None, direct_provenance: Mapping[str, Any] | None = None,
    native_provenance: Mapping[str, Any] | None = None,
    expected_live_baseline_sha256: str | None = None,
    manual_decomposition: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Publish validated Agent classifications/images through the one baseline path.

    Callers own domain compilation. This boundary verifies actual predecessor,
    receipts and source identity; it never fabricates a native text/table run.
    """
    if container is None:
        from reqpilotx_server.application.runtime import RUNTIME_STATE
        container = RUNTIME_STATE.container
    from reqpilotx_server.application.agent_workflow.source_requirement_reference_bridge import _load_effective_reference
    previous = previous_bundle["baseline"]
    baseline = SourceRequirementBaselineArtifactV1.model_validate(baseline.model_dump(mode="json"))
    validation_receipt = SourceRequirementBaselineValidationReceiptV1.model_validate(validation_receipt.model_dump(mode="json"))
    if expected_live_baseline_sha256 and expected_live_baseline_sha256 != previous.artifact_sha256:
        if native_provenance is None or not previous_bundle.get("product_baseline_pointer"):
            raise ValueError("Private revision chains require a verified native-loop predecessor artifact")
        stored_previous = _load_product_baseline_artifact(container.object_store, previous_bundle["product_baseline_pointer"],
            project_id=project_id, document_id=document_id)
        if stored_previous.baseline.artifact_sha256 != previous.artifact_sha256:
            raise ValueError("Private revision predecessor pointer does not resolve to its actual baseline")
    repository = getattr(container, "agent_structure_loop_repository", None)
    structure_head = (repository.get_working_revision_head(project_id=project_id, document_id=document_id,
        source_sha256=document_map.source_sha256) if repository is not None else None)
    fresh = _load_effective_reference(project_id=project_id, document_id=document_id, source_sha256=document_map.source_sha256)
    if fresh["baseline"].artifact_sha256 != (expected_live_baseline_sha256 or previous.artifact_sha256):
        raise ValueError("Agent revision predecessor changed before publication")
    if (baseline.predecessor_artifact_sha256 != previous.artifact_sha256 or baseline.revision != previous.revision + 1
        or validation_receipt.artifact_sha256 != baseline.artifact_sha256 or baseline.document_map_artifact_sha256 != document_map.artifact_sha256):
        raise ValueError("Agent revision source/domain validation lineage drifted")
    if native_provenance is not None:
        if runtime_receipts or direct_provenance is not None:
            raise ValueError("Native loop, disposable stage and direct provenance must remain separate")
        run_receipt = SourceItemNativeLoopRunReceiptV1.model_validate({**dict(native_provenance),
            "source_sha256": document_map.source_sha256, "document_map_artifact_id": document_map.artifact_id,
            "document_map_artifact_sha256": document_map.artifact_sha256, "predecessor_sha256": previous.artifact_sha256,
            "operation": operation, "request_sha256": request_sha256, "candidate_sha256": candidate_sha256})
    elif direct_provenance is not None:
        if runtime_receipts:
            raise ValueError("Direct and App Server provenance must remain separate")
        run_receipt = SourceItemDirectRunReceiptV1.model_validate({**dict(direct_provenance),
            "source_sha256": document_map.source_sha256, "document_map_artifact_id": document_map.artifact_id,
            "document_map_artifact_sha256": document_map.artifact_sha256, "predecessor_sha256": previous.artifact_sha256,
            "operation": operation, "request_sha256": request_sha256, "candidate_sha256": candidate_sha256})
    elif not runtime_receipts or any(receipt.get("status") != "CANDIDATE_RETURNED" or receipt.get("thread_cleanup_verified") is not True for receipt in runtime_receipts):
        raise ValueError("Agent revision needs actual completed App Server receipts")
    else:
        run_receipt = SourceItemAgentRunReceiptV1(source_sha256=document_map.source_sha256,
            document_map_artifact_id=document_map.artifact_id, document_map_artifact_sha256=document_map.artifact_sha256,
            model_id=str(runtime_receipts[-1]["model_id"]), operation=operation, request_sha256=request_sha256,
            candidate_sha256=candidate_sha256, runtime_receipts=tuple(dict(item) for item in runtime_receipts))
    proof = SourceItemAgentRevisionReceiptV1(artifact_id=baseline.artifact_id, artifact_sha256=baseline.artifact_sha256,
        predecessor_artifact_id=previous.artifact_id, predecessor_artifact_sha256=previous.artifact_sha256,
        operation=operation, affected_scope_ids=affected_scope_ids, validator_receipt_sha256=validation_receipt.receipt_sha256,
        domain_proof=dict(domain_proof))
    values = {"source_sha256": document_map.source_sha256, "document_map_artifact_id": document_map.artifact_id,
              "document_map_artifact_sha256": document_map.artifact_sha256, "baseline": baseline, "validation_receipt": validation_receipt,
              "append_receipt": None, "growth_receipt": None, "scope_revision_receipt": None, "agent_revision_receipt": proof,
              "product_run_receipts": tuple(previous_bundle.get("product_run_receipts", ())) + (run_receipt,)}
    draft = SourceRequirementProductBaselineArtifactV1.model_construct(**values, artifact_sha256="0" * 64)
    product = SourceRequirementProductBaselineArtifactV1(**values, artifact_sha256=canonical_json_sha256(draft.model_dump(mode="json", exclude={"artifact_sha256"})))
    encoded = json.dumps(product.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    context.begin_publication()
    key = _product_baseline_object_key(project_id=project_id, document_id=document_id, artifact=product)
    obj = container.object_store.put(key=key, source=BytesIO(encoded), expected_sha256=hashlib.sha256(encoded).hexdigest(), media_type=SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE)
    pointer = {"object_key": obj.key, "object_sha256": obj.sha256, "size": obj.size, "media_type": obj.media_type,
        "artifact_sha256": product.artifact_sha256, "baseline_revision": baseline.revision,
        "baseline_artifact_id": baseline.artifact_id, "baseline_artifact_sha256": baseline.artifact_sha256, "record_count": len(baseline.records)}
    result = {"schema_version": SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION, "status": "SCOPE_COMPLETE",
        "source_sha256": baseline.source_sha256, "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256, "baseline_revision_artifact": pointer,
        "predecessor_baseline_revision_artifact": previous_bundle.get("product_baseline_pointer"),
        "operation": operation, "scope_ids": list(affected_scope_ids), "record_count": len(baseline.records),
        "transport_kind": run_receipt.transport_kind, "next_action": "BASELINE_REVISION_PUBLISHED", "is_human_accepted": False}
    result["expected_live_baseline_sha256"] = expected_live_baseline_sha256 or previous.artifact_sha256
    native_publication = previous_bundle.get("native_publication") or {}
    if (native_publication.get("graph_pointer") or {}).get("object_sha256"):
        result["expected_native_source_graph_object_sha256"] = native_publication["graph_pointer"]["object_sha256"]
    if repository is not None:
        result["expected_structure_head_verified"] = True
        result["expected_structure_head_revision_sha256"] = structure_head.revision_sha256 if structure_head is not None else None
    if image_annotations is not None:
        from reqpilotx_server.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
        annotations = rebind_source_item_image_annotations(document_map, baseline, image_annotations)
        raw = json.dumps(annotations, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        sha = hashlib.sha256(raw).hexdigest()
        image_obj = container.object_store.put(key=key + ".image-annotations.json", source=BytesIO(raw), expected_sha256=sha,
            media_type="application/vnd.reqpilotx.source-item-image-annotations+json")
        result["image_annotations_artifact"] = {"object_key": image_obj.key, "object_sha256": image_obj.sha256, "size": image_obj.size, "media_type": image_obj.media_type}
    if not expected_live_baseline_sha256 or expected_live_baseline_sha256 == previous.artifact_sha256:
        from reqpilotx_server.application.agent_workflow.source_item_manual_publication import attach_rebound_baseline_manual_contexts
        attach_rebound_baseline_manual_contexts(result, container=container, project_id=project_id, document_id=document_id,
            previous_bundle=previous_bundle, document_map=document_map, baseline=baseline, annotations=image_annotations,
            decomposition=manual_decomposition)
    return result


def _run_table_owned_text_companion(
    *,
    current: Mapping[str, Any],
    candidate: SourceRequirementSliceCandidateV1,
    container: Any,
    context: JobExecutionContext,
    project_id: str,
    document_id: str,
    progress_state: dict[str, Any],
    timeout_seconds: int,
    additional_tables: tuple[SourceRequirementScopeResultArtifactV1, ...] = (),
) -> tuple[tuple[SourceItemTextScopeRequestV1, ...], SourceItemTextBatchCandidateV1, NativeSourceItemTextRunReceiptV1, dict[str, Any] | None]:
    """Resolve one mixed node before any product baseline is published."""
    table_request = current["request"]
    document_map = current["document_map"]
    node = next(item for item in document_map.nodes if item.node_id == table_request.node_id)
    table_scopes = {
        block.logical_table_scope_id for block in node.content_blocks
        if block.logical_table_scope_id
    }
    executor = container.source_item_text_executor
    if executor is None or executor.readiness_reason() is not None:
        raise RuntimeError("Direct-text executor is unavailable for table-owned text.")
    text_request = build_source_item_text_scope_request(document_map, node_id=node.node_id)
    requests = (text_request,)
    base_bundle = current.get("source_requirement_base_bundle")
    covered_table_scopes: set[str] = set()
    if isinstance(base_bundle, Mapping):
        effective = resolve_effective_source_requirement_bundle(
            project_id=project_id,
            document_id=document_id,
            base_bundle=base_bundle,
            container=container,
        )
        covered_table_scopes = set(effective["baseline"].covered_scope_ids)
        if text_request.scope_id in covered_table_scopes:
            raise NonRetryableJobError(
                error_code="source_requirement_mixed_text_already_covered",
                message="The text scope was already published and cannot be silently reassigned.",
            )
    table_pairs = ((table_request, candidate),) + tuple(
        (item.request, item.candidate) for item in additional_tables
    )
    planned = {request.logical_table_scope_id for request, _ in table_pairs}
    if not table_scopes.issubset(planned | covered_table_scopes):
        raise NonRetryableJobError(
            error_code="source_requirement_mixed_joint_plan_incomplete",
            message="All tables in a mixed node must be resolved before publishing its direct text.",
        )
    reserved = [
        {
            "logical_table_scope_id": request.logical_table_scope_id,
            "record_key": record.record_key,
            "text_span": span.model_dump(mode="json"),
        }
        for request, table_candidate in table_pairs
        for record in table_candidate.records for span in record.owning_text_spans
    ]
    # Overlap is an unresolved ownership decision, never a reason to truncate or
    # silently give the words to the first table that happened to finish.
    for index, owner in enumerate(reserved):
        span = owner["text_span"]
        for other in reserved[:index]:
            prior = other["text_span"]
            if (span["content_id"] == prior["content_id"]
                    and max(span["start_offset"], prior["start_offset"])
                    < min(span["end_offset"], prior["end_offset"])):
                raise NonRetryableJobError(
                    error_code="source_requirement_mixed_joint_ownership_overlap",
                    message="Multiple table records claimed the same direct text; revise their boundaries before publication.",
                )
    context_values = {
        "schema_version": "source-item-text-table-ownership/v1",
        "source_sha256": text_request.source_sha256,
        "document_map_artifact_sha256": text_request.document_map_artifact_sha256,
        "scope_id": text_request.scope_id,
        "table_candidate_sha256": canonical_json_sha256(candidate.model_dump(mode="json")),
        **({"additional_table_candidate_sha256s": [
            canonical_json_sha256(item.candidate.model_dump(mode="json")) for item in additional_tables
        ]} if additional_tables else {}),
        "table_owned_spans": reserved,
    }
    execution_context = {
        **context_values,
        "context_sha256": canonical_json_sha256(context_values),
    }
    store = NativeSourceItemTextDurableCheckpointStore(
        object_store=container.object_store,
        project_id=project_id,
        document_id=document_id,
    )
    checkpoint_progress = NativeSourceItemTextCheckpointProgress(
        context=context, store=store, initial=progress_state,
    )
    try:
        resume_checkpoint = checkpoint_progress.load()
    except ValueError as exc:
        raise NonRetryableJobError(
            error_code="source_requirement_mixed_text_checkpoint_invalid",
            message="The paired text checkpoint failed identity validation.",
        ) from exc
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="source_requirement_mixed_text_cancelled",
            message="Paired text processing was cancelled before publication.",
        )
    context.heartbeat(progress={
        **progress_state,
        "stage": "RUNNING_TABLE_OWNED_TEXT",
        "message": "正在核对表格记录与同段正文的成对归属。",
        "scope_id": text_request.scope_id,
    })
    execution = executor.execute(
        SourceItemTextTaskV1(
            project_id=project_id,
            document_id=document_id,
            source_path=current["source_path"],
            document_map=document_map.model_dump(mode="json"),
            scope_requests=(text_request.model_dump(mode="json"),),
            timeout_seconds=timeout_seconds,
            max_concurrency=2,
            execution_context=execution_context,
            semantic_context=current.get("semantic_context"),
        ),
        cancellation_requested=context.cancellation_requested,
        resume_checkpoint=None if resume_checkpoint is None else resume_checkpoint.model_dump(mode="json"),
        checkpoint_sink=checkpoint_progress.save,
    )
    batch, receipt = _validate_text_execution(
        execution,
        document_map=document_map,
        requests=requests,
        execution_context=execution_context,
    )
    actual = [item.model_dump(mode="json") for item in batch.candidates[0].table_owned_spans]
    if sorted(actual, key=canonical_json_sha256) != sorted(reserved, key=canonical_json_sha256):
        raise NonRetryableJobError(
            error_code="source_requirement_mixed_text_ownership_mismatch",
            message="The paired text result did not preserve exact table ownership.",
        )
    return (
        requests, batch, receipt,
        checkpoint_progress.receipt.model_dump(mode="json")
        if checkpoint_progress.receipt is not None else None,
    )


def _append_product_baseline_artifact(
    *,
    document_map: DocumentMapArtifactV1,
    previous_artifact: SourceRequirementBaselineArtifactV1,
    previous_validation_receipt: SourceRequirementBaselineValidationReceiptV1,
    request: SourceRequirementScopeRequestV1,
    candidate: SourceRequirementSliceCandidateV1,
    run_receipt: NativeSourceRequirementRunReceiptV1,
    previous_product_run_receipts: tuple[
        NativeSourceRequirementRunReceiptV1 | NativeSourceItemTextRunReceiptV1,
        ...,
    ],
    text_requests: tuple[SourceItemTextScopeRequestV1, ...] = (),
    text_batch: SourceItemTextBatchCandidateV1 | None = None,
    text_run_receipt: NativeSourceItemTextRunReceiptV1 | None = None,
    additional_tables: tuple[SourceRequirementScopeResultArtifactV1, ...] = (),
) -> SourceRequirementProductBaselineArtifactV1:
    if bool(text_requests) != (text_batch is not None) or bool(text_requests) != (text_run_receipt is not None):
        raise ValueError("paired text requests, candidate and receipt must travel together")
    text_candidates = tuple(
        {item.scope_id: item for item in text_batch.candidates}[request.scope_id]
        for request in text_requests
    ) if text_batch is not None else ()
    append_receipt: SourceRequirementBaselineAppendReceiptV1 | None = None
    growth_receipt: SourceRequirementBaselineGrowthReceiptV1 | None = None
    try:
        baseline, validation_receipt, append_receipt = (
            append_source_requirement_baseline(
                document_map=document_map,
                previous_artifact=previous_artifact,
                previous_validation_receipt=previous_validation_receipt,
                requests=(request,) + tuple(item.request for item in additional_tables),
                candidates=(candidate,) + tuple(item.candidate for item in additional_tables),
                text_requests=text_requests,
                text_candidates=text_candidates,
            )
        )
    except SourceRequirementBaselineError as exc:
        if str(exc) != "SOURCE_REQUIREMENT_APPEND_NOT_SOURCE_ORDERED":
            raise
        baseline, validation_receipt, growth_receipt = (
            grow_source_requirement_baseline(
                document_map=document_map,
                previous_artifact=previous_artifact,
                previous_validation_receipt=previous_validation_receipt,
                requests=(request,) + tuple(item.request for item in additional_tables),
                candidates=(candidate,) + tuple(item.candidate for item in additional_tables),
                text_requests=text_requests,
                text_candidates=text_candidates,
            )
        )
    values = {
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "baseline": baseline,
        "validation_receipt": validation_receipt,
        "append_receipt": append_receipt,
        "growth_receipt": growth_receipt,
        "product_run_receipts": previous_product_run_receipts + (run_receipt,)
        + tuple(item.run_receipt for item in additional_tables)
        + ((text_run_receipt,) if text_run_receipt is not None else ()),
    }
    draft = SourceRequirementProductBaselineArtifactV1.model_construct(
        **values,
        artifact_sha256="0" * 64,
    )
    return SourceRequirementProductBaselineArtifactV1(
        **values,
        artifact_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"artifact_sha256"})
        ),
    )


def _append_product_text_baseline_artifact(
    *,
    document_map: DocumentMapArtifactV1,
    previous_artifact: SourceRequirementBaselineArtifactV1,
    previous_validation_receipt: SourceRequirementBaselineValidationReceiptV1,
    requests: tuple[SourceItemTextScopeRequestV1, ...],
    batch: SourceItemTextBatchCandidateV1,
    run_receipt: NativeSourceItemTextRunReceiptV1 | None,
    previous_product_run_receipts: tuple[
        NativeSourceRequirementRunReceiptV1 | NativeSourceItemTextRunReceiptV1,
        ...,
    ],
) -> SourceRequirementProductBaselineArtifactV1:
    candidate_by_scope = {item.scope_id: item for item in batch.candidates}
    candidates = tuple(candidate_by_scope[item.scope_id] for item in requests)
    append_receipt: SourceRequirementBaselineAppendReceiptV1 | None = None
    growth_receipt: SourceRequirementBaselineGrowthReceiptV1 | None = None
    try:
        baseline, validation_receipt, append_receipt = (
            append_source_requirement_baseline(
                document_map=document_map,
                previous_artifact=previous_artifact,
                previous_validation_receipt=previous_validation_receipt,
                text_requests=requests,
                text_candidates=candidates,
            )
        )
    except SourceRequirementBaselineError as exc:
        if str(exc) != "SOURCE_REQUIREMENT_APPEND_NOT_SOURCE_ORDERED":
            raise
        baseline, validation_receipt, growth_receipt = (
            grow_source_requirement_baseline(
                document_map=document_map,
                previous_artifact=previous_artifact,
                previous_validation_receipt=previous_validation_receipt,
                text_requests=requests,
                text_candidates=candidates,
            )
        )
    values = {
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "baseline": baseline,
        "validation_receipt": validation_receipt,
        "append_receipt": append_receipt,
        "growth_receipt": growth_receipt,
        "product_run_receipts": previous_product_run_receipts
        + ((run_receipt,) if run_receipt is not None else ()),
    }
    draft = SourceRequirementProductBaselineArtifactV1.model_construct(
        **values,
        artifact_sha256="0" * 64,
    )
    return SourceRequirementProductBaselineArtifactV1(
        **values,
        artifact_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"artifact_sha256"})
        ),
    )


def _build_product_scope_revision_artifact(
    *,
    document_map: DocumentMapArtifactV1,
    baseline: SourceRequirementBaselineArtifactV1,
    validation_receipt: SourceRequirementBaselineValidationReceiptV1,
    scope_revision_receipt: SourceItemScopeRevisionReceiptV1,
    previous_product_run_receipts: tuple[
        NativeSourceRequirementRunReceiptV1 | NativeSourceItemTextRunReceiptV1,
        ...,
    ],
    run_receipt: NativeSourceItemTextRunReceiptV1 | None,
) -> SourceRequirementProductBaselineArtifactV1:
    values = {
        "source_sha256": document_map.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "baseline": baseline,
        "validation_receipt": validation_receipt,
        "append_receipt": None,
        "growth_receipt": None,
        "scope_revision_receipt": scope_revision_receipt,
        "product_run_receipts": previous_product_run_receipts
        + ((run_receipt,) if run_receipt is not None else ()),
    }
    draft = SourceRequirementProductBaselineArtifactV1.model_construct(
        **values,
        artifact_sha256="0" * 64,
    )
    return SourceRequirementProductBaselineArtifactV1(
        **values,
        artifact_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"artifact_sha256"})
        ),
    )


def _load_scope_result_artifact(
    object_store: Any,
    pointer: Mapping[str, Any],
    *,
    project_id: str,
    document_id: str,
) -> SourceRequirementScopeResultArtifactV1:
    encoded = _read_private_artifact(
        object_store,
        pointer,
        required_prefix=(
            "runtime/agent-source-requirement-scope-results/"
            f"{_safe_token(project_id)}/{_safe_token(document_id)}/"
        ),
        expected_media_type=SOURCE_REQUIREMENT_SCOPE_ARTIFACT_MEDIA_TYPE,
    )
    return SourceRequirementScopeResultArtifactV1.model_validate_json(encoded)


def _load_text_scope_result_artifact(
    object_store: Any,
    pointer: Mapping[str, Any],
    *,
    project_id: str,
    document_id: str,
) -> SourceItemTextScopeResultArtifactV1:
    encoded = _read_private_artifact(
        object_store,
        pointer,
        required_prefix=(
            "runtime/agent-source-item-text-results/"
            f"{_safe_token(project_id)}/{_safe_token(document_id)}/"
        ),
        expected_media_type=SOURCE_ITEM_TEXT_SCOPE_ARTIFACT_MEDIA_TYPE,
    )
    artifact = SourceItemTextScopeResultArtifactV1.model_validate_json(encoded)
    if artifact.artifact_sha256 != str(pointer.get("artifact_sha256") or ""):
        raise ValueError("direct-text SourceItem pointer drifted")
    return artifact


def _load_product_baseline_artifact(
    object_store: Any,
    pointer: Mapping[str, Any],
    *,
    project_id: str,
    document_id: str,
) -> SourceRequirementProductBaselineArtifactV1:
    encoded = _read_private_artifact(
        object_store,
        pointer,
        required_prefix=(
            "runtime/agent-source-requirement-baselines/"
            f"{_safe_token(project_id)}/{_safe_token(document_id)}/"
        ),
        expected_media_type=SOURCE_REQUIREMENT_PRODUCT_BASELINE_MEDIA_TYPE,
    )
    product = SourceRequirementProductBaselineArtifactV1.model_validate_json(
        encoded
    )
    if product.artifact_sha256 != str(pointer.get("artifact_sha256") or ""):
        raise ValueError("product SourceItem baseline pointer drifted")
    return product


def _read_private_artifact(
    object_store: Any,
    pointer: Mapping[str, Any],
    *,
    required_prefix: str,
    expected_media_type: str,
) -> bytes:
    key = str(pointer.get("object_key") or "")
    expected_sha256 = str(pointer.get("object_sha256") or "")
    expected_size = int(pointer.get("size") or 0)
    media_type = str(pointer.get("media_type") or "")
    if (
        not key.startswith(required_prefix)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or expected_size < 1
        or media_type != expected_media_type
    ):
        raise ValueError("private SourceItem artifact pointer is invalid")
    with object_store.open(key) as stream:
        encoded = stream.read()
    if (
        len(encoded) != expected_size
        or hashlib.sha256(encoded).hexdigest() != expected_sha256
    ):
        raise ValueError("private SourceItem artifact receipt drifted")
    return encoded


def _scope_result_object_key(
    *,
    project_id: str,
    document_id: str,
    artifact: SourceRequirementScopeResultArtifactV1,
) -> str:
    return (
        "runtime/agent-source-requirement-scope-results/"
        f"{_safe_token(project_id)}/{_safe_token(document_id)}/"
        f"{artifact.request.source_sha256}/{artifact.request.request_sha256}/"
        f"{artifact.artifact_sha256}.json"
    )


def _text_result_object_key(
    *,
    project_id: str,
    document_id: str,
    artifact: SourceItemTextScopeResultArtifactV1,
) -> str:
    return (
        "runtime/agent-source-item-text-results/"
        f"{_safe_token(project_id)}/{_safe_token(document_id)}/"
        f"{artifact.run_receipt.source_sha256}/"
        f"{artifact.run_receipt.request_set_sha256}/"
        f"{artifact.artifact_sha256}.json"
    )


def _product_baseline_object_key(
    *,
    project_id: str,
    document_id: str,
    artifact: SourceRequirementProductBaselineArtifactV1,
) -> str:
    return (
        "runtime/agent-source-requirement-baselines/"
        f"{_safe_token(project_id)}/{_safe_token(document_id)}/"
        f"{artifact.source_sha256}/{artifact.baseline.artifact_sha256}/"
        f"{artifact.artifact_sha256}.json"
    )


def _safe_token(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
        for character in normalized
    ):
        raise ValueError("source-requirement job identity is invalid")
    return normalized


def _job_matches(job: DurableJob | None, expected: Mapping[str, Any]) -> bool:
    if job is None or job.status is not JobStatus.SUCCEEDED:
        return False
    result = dict(job.result or {})
    return (
        result.get("schema_version")
        == SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION
        and result.get("status") == "SCOPE_COMPLETE"
        and all(result.get(key) == expected.get(key) for key in (
            "source_sha256",
            "native_artifact_id",
            "native_projection_sha256",
            "document_map_artifact_id",
            "document_map_artifact_sha256",
            "logical_table_scope_id",
            "request_sha256",
            "executor_id",
            "executor_version",
            "transport_kind",
        ))
    )


def _text_job_matches(job: DurableJob | None, expected: Mapping[str, Any]) -> bool:
    if job is None or job.status is not JobStatus.SUCCEEDED:
        return False
    result = dict(job.result or {})
    return (
        result.get("schema_version")
        == SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION
        and result.get("status") == "SCOPE_COMPLETE"
        and all(
            result.get(key) == expected.get(key)
            for key in (
                "source_sha256",
                "native_artifact_id",
                "native_projection_sha256",
                "document_map_artifact_id",
                "document_map_artifact_sha256",
                "scope_id",
                "scope_ids",
                "request_set_sha256",
                "executor_id",
                "executor_version",
                "transport_kind",
            )
        )
    )


def _scope_revision_job_matches(
    job: DurableJob | None,
    expected: Mapping[str, Any],
) -> bool:
    if job is None or job.status is not JobStatus.SUCCEEDED:
        return False
    result = dict(job.result or {})
    return (
        result.get("schema_version")
        == SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION
        and result.get("status") == "REVISION_COMPLETE"
        and (result.get("scope_revision") or {}).get("operation") == "REPLACE"
        and all(
            result.get(key) == expected.get(key)
            for key in (
                "source_sha256",
                "native_artifact_id",
                "native_projection_sha256",
                "document_map_artifact_id",
                "document_map_artifact_sha256",
                "affected_scope_ids",
                "comparison_scope_ids",
                "feedback_ids",
                "guidance_ids",
                "request_set_sha256",
                "executor_id",
                "executor_version",
                "transport_kind",
            )
        )
    )


def _scope_undo_job_matches(
    job: DurableJob | None,
    expected: Mapping[str, Any],
) -> bool:
    if job is None or job.status is not JobStatus.SUCCEEDED:
        return False
    result = dict(job.result or {})
    return (
        result.get("schema_version")
        == SOURCE_REQUIREMENT_BASELINE_RESULT_SCHEMA_VERSION
        and result.get("status") == "REVISION_COMPLETE"
        and (result.get("scope_revision") or {}).get("operation") == "UNDO"
        and all(
            result.get(key) == expected.get(key)
            for key in (
                "source_sha256",
                "document_map_artifact_id",
                "document_map_artifact_sha256",
                "affected_scope_ids",
                "comparison_scope_ids",
                "feedback_ids",
                "guidance_ids",
            )
        )
    )


def _latest_job(*jobs: DurableJob | None, prefer_baseline_revision: bool = False) -> DurableJob | None:
    available = [item for item in jobs if item is not None]
    def key(job: DurableJob):
        pointer = (job.result or {}).get("baseline_revision_artifact") or {}
        revision = pointer.get("baseline_revision", 0) if isinstance(pointer, Mapping) else 0
        return (int(revision) if prefer_baseline_revision else 0, job.updated_at)
    return max(available, key=key) if available else None


def _job_projection(job: DurableJob) -> dict[str, Any]:
    progress = dict(job.progress or {})
    result = dict(job.result or {})
    return {
        "schema_version": "source-requirement-baseline-job-public/v1",
        "job_id": job.job_id,
        "status": job.status.value,
        "generation": job.generation,
        "scope_id": str(
            result.get("scope_id")
            or result.get("logical_table_scope_id")
            or job.payload.get("scope_id")
            or job.payload.get("logical_table_scope_id")
            or ""
        ),
        "scope_ids": list(
            result.get("scope_ids") or job.payload.get("scope_ids") or ()
        ),
        "progress": {
            "stage": str(progress.get("stage") or ""),
            "message": str(progress.get("message") or ""),
            "strategy": str(progress.get("strategy") or ""),
            "completed_window_count": int(
                progress.get("completed_window_count") or 0
            ),
            "total_window_count": int(progress.get("total_window_count") or 0),
        },
        "result": (
            {
                "status": str(result.get("status") or ""),
                "record_count": int(result.get("record_count") or 0),
                "context_row_group_count": int(
                    result.get("context_row_group_count") or 0
                ),
                "candidate_sha256": str(result.get("candidate_sha256") or ""),
                "next_action": str(result.get("next_action") or ""),
                "baseline_revision": int(
                    (result.get("baseline_revision_artifact") or {}).get(
                        "baseline_revision"
                    )
                    or 0
                ),
                "baseline_record_count": int(
                    (result.get("baseline_revision_artifact") or {}).get(
                        "record_count"
                    )
                    or 0
                ),
                "revision_summary": {
                    "operation": str(
                        (result.get("scope_revision") or {}).get("operation") or ""
                    ),
                    "matched_count": int(
                        (result.get("scope_revision") or {}).get("matched_count")
                        or 0
                    ),
                    "created_count": int(
                        (result.get("scope_revision") or {}).get("created_count")
                        or 0
                    ),
                    "tombstoned_count": int(
                        (result.get("scope_revision") or {}).get(
                            "tombstoned_count"
                        )
                        or 0
                    ),
                },
            }
            if job.status is JobStatus.SUCCEEDED
            else None
        ),
        "error_code": job.error_code,
    }


def _status_message(job: DurableJob | None) -> str:
    if job is None:
        return "尚未启动当前原文范围的渐进提取。"
    if job.status is JobStatus.SUCCEEDED:
        result = dict(job.result or {})
        pointer = result.get("baseline_revision_artifact")
        if isinstance(pointer, Mapping):
            if job.kind == SOURCE_ITEM_SCOPE_REVISION_JOB_KIND:
                summary = dict(result.get("scope_revision") or {})
                return (
                    f"局部修订已发布为原文需求清单第 "
                    f"{int(pointer.get('baseline_revision') or 0)} 版；"
                    f"保留 {int(summary.get('matched_count') or 0)} 条，"
                    f"新增 {int(summary.get('created_count') or 0)} 条，"
                    f"替代 {int(summary.get('tombstoned_count') or 0)} 条。"
                )
            return (
                f"当前范围已长入原文需求清单第 "
                f"{int(pointer.get('baseline_revision') or 0)} 版。"
            )
        return "当前范围已形成可恢复、可追踪的原文条目候选。"
    if job.status in {JobStatus.PENDING, JobStatus.RUNNING}:
        return str(job.progress.get("message") or "正在处理当前原文范围。")
    if job.status is JobStatus.FAILED:
        if job.kind == SOURCE_ITEM_SCOPE_REVISION_JOB_KIND:
            return "局部修订未发布；当前清单保持不变，可从已接受窗口继续。"
        if job.kind == SOURCE_ITEM_TEXT_BASELINE_JOB_KIND:
            return "当前正文批次处理失败；未发布半成品，可重新运行。"
        return "当前范围处理失败；已接受窗口仍保留，可从失败窗口继续。"
    if job.status is JobStatus.CANCELLED:
        return "当前范围处理已取消；已发布结果未受影响。"
    return "当前范围正在处理。"


def _runtime_unavailable() -> ApplicationError:
    return ApplicationError(
        status_code=503,
        detail={
            "code": "source_requirement_baseline_runtime_unavailable",
            "message": "Durable SourceItem processing is unavailable.",
        },
    )


__all__ = [
    "SOURCE_ITEM_SCOPE_REVISION_JOB_KIND",
    "SOURCE_ITEM_TEXT_BASELINE_JOB_KIND",
    "SOURCE_REQUIREMENT_BASELINE_JOB_KIND",
    "SourceItemTextBaselineStartRequestV1",
    "SourceItemScopeRevisionStartRequestV1",
    "SourceItemScopeUndoStartRequestV1",
    "SourceItemTextScopeResultArtifactV1",
    "SourceRequirementProductBaselineArtifactV1",
    "SourceRequirementBaselineStartRequestV1",
    "SourceRequirementScopeResultArtifactV1",
    "get_source_requirement_baseline_run_status",
    "resolve_effective_source_requirement_bundle",
    "run_durable_source_requirement_baseline",
    "run_durable_source_item_text_baseline",
    "run_durable_source_item_scope_revision",
    "start_source_item_text_baseline",
    "start_source_item_scope_revision",
    "start_source_item_scope_undo",
    "start_source_requirement_baseline",
]
