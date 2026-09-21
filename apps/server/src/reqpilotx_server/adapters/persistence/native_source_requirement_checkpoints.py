"""Private object-store checkpoints for resumable SourceItem table windows."""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    NativeSourceRequirementWindowedCheckpointV1,
)
from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    canonical_json_sha256,
)
from reqpilotx_server.ports.job_queue import JobExecutionContext
from reqpilotx_server.ports.object_store import ObjectStore


CHECKPOINT_RECEIPT_SCHEMA_VERSION = (
    "native-source-requirement-windowed-checkpoint-receipt/v1"
)
CHECKPOINT_MEDIA_TYPE = (
    "application/vnd.reqpilotx.source-requirement-windowed-checkpoint+json"
)


class NativeSourceRequirementWindowedCheckpointReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal[
        "native-source-requirement-windowed-checkpoint-receipt/v1"
    ] = CHECKPOINT_RECEIPT_SCHEMA_VERSION
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_table_scope_id: str = Field(min_length=1, max_length=200)
    boundary_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_window_ids: tuple[str, ...] = Field(default=(), max_length=4)
    object_key: str = Field(min_length=1, max_length=2000)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(
        self,
    ) -> NativeSourceRequirementWindowedCheckpointReceiptV1:
        if len(self.accepted_window_ids) != len(set(self.accepted_window_ids)):
            raise ValueError("checkpoint receipt window IDs must be unique")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("checkpoint receipt SHA drifted")
        return self


class NativeSourceRequirementDurableCheckpointStore:
    """Persist cumulative runner checkpoints and bind a small durable receipt."""

    def __init__(
        self,
        *,
        object_store: ObjectStore,
        project_id: str,
        document_id: str,
    ) -> None:
        self._object_store = object_store
        self._project_id = _safe_path_token(project_id, subject="project")
        self._document_id = _safe_path_token(document_id, subject="document")

    def save(
        self,
        checkpoint: NativeSourceRequirementWindowedCheckpointV1,
    ) -> NativeSourceRequirementWindowedCheckpointReceiptV1:
        encoded = json.dumps(
            checkpoint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object_sha256 = hashlib.sha256(encoded).hexdigest()
        object_key = self._object_prefix(checkpoint) + (
            f"{checkpoint.checkpoint_sha256}.json"
        )
        stored = self._object_store.put(
            key=object_key,
            source=BytesIO(encoded),
            expected_sha256=object_sha256,
            media_type=CHECKPOINT_MEDIA_TYPE,
        )
        values = {
            "source_sha256": checkpoint.source_sha256,
            "document_map_artifact_sha256": (checkpoint.document_map_artifact_sha256),
            "request_sha256": checkpoint.request_sha256,
            "logical_table_scope_id": checkpoint.logical_table_scope_id,
            "boundary_identity_sha256": (
                checkpoint.boundary_plan.boundary_identity_sha256
            ),
            "accepted_window_ids": tuple(
                item.window.window_id for item in checkpoint.accepted_windows
            ),
            "object_key": stored.key,
            "object_sha256": stored.sha256,
            "checkpoint_sha256": checkpoint.checkpoint_sha256,
        }
        draft = NativeSourceRequirementWindowedCheckpointReceiptV1.model_construct(
            **values,
            receipt_sha256="0" * 64,
        )
        return NativeSourceRequirementWindowedCheckpointReceiptV1(
            **values,
            receipt_sha256=canonical_json_sha256(
                draft.model_dump(mode="json", exclude={"receipt_sha256"})
            ),
        )

    def load(
        self,
        receipt: NativeSourceRequirementWindowedCheckpointReceiptV1,
    ) -> NativeSourceRequirementWindowedCheckpointV1:
        expected_prefix = (
            "runtime/agent-source-requirement-windowed-checkpoints/"
            f"{self._project_id}/{self._document_id}/{receipt.source_sha256}/"
            f"{receipt.request_sha256}/"
        )
        if not receipt.object_key.startswith(expected_prefix):
            raise ValueError("source-requirement checkpoint object key drifted")
        try:
            with self._object_store.open(receipt.object_key) as stream:
                encoded = stream.read()
        except OSError as exc:
            raise ValueError("source-requirement checkpoint object missing") from exc
        if hashlib.sha256(encoded).hexdigest() != receipt.object_sha256:
            raise ValueError("source-requirement checkpoint object SHA drifted")
        checkpoint = NativeSourceRequirementWindowedCheckpointV1.model_validate_json(
            encoded
        )
        if (
            checkpoint.source_sha256 != receipt.source_sha256
            or checkpoint.document_map_artifact_sha256
            != receipt.document_map_artifact_sha256
            or checkpoint.request_sha256 != receipt.request_sha256
            or checkpoint.logical_table_scope_id != receipt.logical_table_scope_id
            or checkpoint.boundary_plan.boundary_identity_sha256
            != receipt.boundary_identity_sha256
            or tuple(item.window.window_id for item in checkpoint.accepted_windows)
            != receipt.accepted_window_ids
            or checkpoint.checkpoint_sha256 != receipt.checkpoint_sha256
        ):
            raise ValueError("source-requirement checkpoint receipt lineage drifted")
        return checkpoint

    def _object_prefix(
        self,
        checkpoint: NativeSourceRequirementWindowedCheckpointV1,
    ) -> str:
        return (
            "runtime/agent-source-requirement-windowed-checkpoints/"
            f"{self._project_id}/{self._document_id}/{checkpoint.source_sha256}/"
            f"{checkpoint.request_sha256}/"
        )


class NativeSourceRequirementCheckpointProgress:
    """Bridge checkpoint objects to PostgreSQL-backed durable job progress."""

    def __init__(
        self,
        *,
        context: JobExecutionContext,
        store: NativeSourceRequirementDurableCheckpointStore,
        initial: dict[str, Any] | None = None,
    ) -> None:
        self._context = context
        self._store = store
        self._base_progress = {
            key: value
            for key, value in dict(initial or {}).items()
            if key != "source_requirement_window_checkpoint"
        }
        raw_receipt = (initial or {}).get("source_requirement_window_checkpoint")
        self._receipt = (
            NativeSourceRequirementWindowedCheckpointReceiptV1.model_validate(
                raw_receipt
            )
            if isinstance(raw_receipt, dict)
            else None
        )

    @property
    def receipt(self) -> NativeSourceRequirementWindowedCheckpointReceiptV1 | None:
        return self._receipt

    def load(self) -> NativeSourceRequirementWindowedCheckpointV1 | None:
        return None if self._receipt is None else self._store.load(self._receipt)

    def save(self, checkpoint: NativeSourceRequirementWindowedCheckpointV1) -> None:
        receipt = self._store.save(checkpoint)
        if self._receipt is not None:
            previous = self._receipt
            if (
                previous.source_sha256 != receipt.source_sha256
                or previous.document_map_artifact_sha256
                != receipt.document_map_artifact_sha256
                or previous.request_sha256 != receipt.request_sha256
                or previous.logical_table_scope_id != receipt.logical_table_scope_id
                or previous.boundary_identity_sha256 != receipt.boundary_identity_sha256
                or not set(previous.accepted_window_ids).issubset(
                    receipt.accepted_window_ids
                )
            ):
                raise ValueError("source-requirement checkpoint progress regressed")
        self._receipt = receipt
        accepted = len(receipt.accepted_window_ids)
        total = len(checkpoint.windows)
        self._context.heartbeat(
            progress={
                **self._base_progress,
                "stage": (
                    "BOUNDARY_PLAN_CHECKPOINTED"
                    if accepted == 0
                    else "CLASSIFICATION_WINDOW_CHECKPOINTED"
                ),
                "message": (
                    "已保存业务边界计划。"
                    if accepted == 0
                    else f"已保存 {accepted}/{total} 个分类窗口。"
                ),
                "strategy": "BOUNDARY_WINDOWS",
                "completed_window_count": accepted,
                "total_window_count": total,
                "source_requirement_window_checkpoint": receipt.model_dump(mode="json"),
            }
        )


def _safe_path_token(value: str, *, subject: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
            for character in normalized
        )
    ):
        raise ValueError(f"source-requirement checkpoint {subject} ID is invalid")
    return normalized


__all__ = [
    "CHECKPOINT_MEDIA_TYPE",
    "CHECKPOINT_RECEIPT_SCHEMA_VERSION",
    "NativeSourceRequirementCheckpointProgress",
    "NativeSourceRequirementDurableCheckpointStore",
    "NativeSourceRequirementWindowedCheckpointReceiptV1",
]
