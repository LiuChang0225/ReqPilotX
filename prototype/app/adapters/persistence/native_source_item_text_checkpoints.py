"""Private ObjectStore persistence for cumulative direct-text checkpoints."""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.adapters.agents.codex_app_server.native_source_item_text_windowed import (
    NativeSourceItemTextWindowedCheckpointV1,
    source_item_text_boundary_set_sha256,
)
from app.application.agent_workflow.source_requirement_baseline import (
    canonical_json_sha256,
)
from app.ports.job_queue import JobExecutionContext
from app.ports.object_store import ObjectStore


CHECKPOINT_RECEIPT_SCHEMA_VERSION = (
    "native-source-item-text-windowed-checkpoint-receipt/v1"
)
CHECKPOINT_MEDIA_TYPE = (
    "application/vnd.reqpilotx.source-item-text-windowed-checkpoint+json"
)


class NativeSourceItemTextWindowedCheckpointReceiptV1(BaseModel):
    """Content-free pointer suitable for durable job progress."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal[
        "native-source-item-text-windowed-checkpoint-receipt/v1"
    ] = CHECKPOINT_RECEIPT_SCHEMA_VERSION
    content_free: Literal[True] = True
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    accepted_boundary_scope_ids: tuple[str, ...] = Field(default=(), max_length=128)
    classification_windows_planned: bool = False
    boundary_set_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    classification_window_ids: tuple[str, ...] = Field(default=(), max_length=128)
    accepted_window_ids: tuple[str, ...] = Field(default=(), max_length=128)
    object_key: str = Field(min_length=1, max_length=2000)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_size: int = Field(ge=1)
    media_type: Literal[
        "application/vnd.reqpilotx.source-item-text-windowed-checkpoint+json"
    ] = CHECKPOINT_MEDIA_TYPE
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(
        self,
    ) -> NativeSourceItemTextWindowedCheckpointReceiptV1:
        for values, subject in (
            (self.scope_ids, "scopes"),
            (self.accepted_boundary_scope_ids, "accepted boundaries"),
            (self.classification_window_ids, "classification windows"),
            (self.accepted_window_ids, "accepted windows"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"text checkpoint receipt {subject} must be unique")
        if not set(self.accepted_boundary_scope_ids).issubset(self.scope_ids):
            raise ValueError("text checkpoint receipt boundary scope drifted")
        if not set(self.accepted_window_ids).issubset(
            self.classification_window_ids
        ):
            raise ValueError("text checkpoint receipt window scope drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("text checkpoint receipt SHA drifted")
        return self


class NativeSourceItemTextDurableCheckpointStore:
    """Persist content-bearing checkpoints and return only a content-free receipt."""

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
        checkpoint: NativeSourceItemTextWindowedCheckpointV1 | Mapping[str, Any],
    ) -> NativeSourceItemTextWindowedCheckpointReceiptV1:
        checkpoint = NativeSourceItemTextWindowedCheckpointV1.model_validate(
            checkpoint
        )
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
        if (
            stored.key != object_key
            or stored.sha256 != object_sha256
            or stored.size != len(encoded)
            or stored.media_type != CHECKPOINT_MEDIA_TYPE
        ):
            raise ValueError("source-item text checkpoint ObjectStore receipt drifted")
        boundary_set_sha256 = (
            source_item_text_boundary_set_sha256(
                tuple(item.plan for item in checkpoint.accepted_boundaries)
            )
            if checkpoint.classification_windows_planned
            else ""
        )
        values = {
            "source_sha256": checkpoint.source_sha256,
            "document_map_artifact_sha256": (
                checkpoint.document_map_artifact_sha256
            ),
            "request_set_sha256": checkpoint.request_set_sha256,
            "scope_ids": checkpoint.scope_ids,
            "accepted_boundary_scope_ids": tuple(
                item.plan.scope_id for item in checkpoint.accepted_boundaries
            ),
            "classification_windows_planned": (
                checkpoint.classification_windows_planned
            ),
            "boundary_set_sha256": boundary_set_sha256,
            "classification_window_ids": tuple(
                item.window_id for item in checkpoint.classification_windows
            ),
            "accepted_window_ids": tuple(
                item.window.window_id for item in checkpoint.accepted_windows
            ),
            "object_key": stored.key,
            "object_sha256": stored.sha256,
            "object_size": stored.size,
            "media_type": stored.media_type,
            "checkpoint_sha256": checkpoint.checkpoint_sha256,
        }
        draft = NativeSourceItemTextWindowedCheckpointReceiptV1.model_construct(
            **values,
            receipt_sha256="0" * 64,
        )
        return NativeSourceItemTextWindowedCheckpointReceiptV1(
            **values,
            receipt_sha256=canonical_json_sha256(
                draft.model_dump(mode="json", exclude={"receipt_sha256"})
            ),
        )

    def load(
        self,
        receipt: NativeSourceItemTextWindowedCheckpointReceiptV1,
    ) -> NativeSourceItemTextWindowedCheckpointV1:
        expected_prefix = (
            "runtime/agent-source-item-text-windowed-checkpoints/"
            f"{self._project_id}/{self._document_id}/{receipt.source_sha256}/"
            f"{receipt.request_set_sha256}/"
        )
        if not receipt.object_key.startswith(expected_prefix):
            raise ValueError("source-item text checkpoint object key drifted")
        try:
            stat = self._object_store.stat(receipt.object_key)
            with self._object_store.open(receipt.object_key) as stream:
                encoded = stream.read()
        except OSError as exc:
            raise ValueError("source-item text checkpoint object missing") from exc
        if (
            stat is None
            or stat.sha256 != receipt.object_sha256
            or stat.size != receipt.object_size
            or len(encoded) != receipt.object_size
            or hashlib.sha256(encoded).hexdigest() != receipt.object_sha256
        ):
            raise ValueError("source-item text checkpoint object SHA drifted")
        checkpoint = NativeSourceItemTextWindowedCheckpointV1.model_validate_json(
            encoded
        )
        boundary_set_sha256 = (
            source_item_text_boundary_set_sha256(
                tuple(item.plan for item in checkpoint.accepted_boundaries)
            )
            if checkpoint.classification_windows_planned
            else ""
        )
        if (
            checkpoint.source_sha256 != receipt.source_sha256
            or checkpoint.document_map_artifact_sha256
            != receipt.document_map_artifact_sha256
            or checkpoint.request_set_sha256 != receipt.request_set_sha256
            or checkpoint.scope_ids != receipt.scope_ids
            or tuple(
                item.plan.scope_id for item in checkpoint.accepted_boundaries
            )
            != receipt.accepted_boundary_scope_ids
            or checkpoint.classification_windows_planned
            != receipt.classification_windows_planned
            or boundary_set_sha256 != receipt.boundary_set_sha256
            or tuple(
                item.window_id for item in checkpoint.classification_windows
            )
            != receipt.classification_window_ids
            or tuple(item.window.window_id for item in checkpoint.accepted_windows)
            != receipt.accepted_window_ids
            or checkpoint.checkpoint_sha256 != receipt.checkpoint_sha256
        ):
            raise ValueError("source-item text checkpoint receipt lineage drifted")
        return checkpoint

    def _object_prefix(
        self,
        checkpoint: NativeSourceItemTextWindowedCheckpointV1,
    ) -> str:
        return (
            "runtime/agent-source-item-text-windowed-checkpoints/"
            f"{self._project_id}/{self._document_id}/{checkpoint.source_sha256}/"
            f"{checkpoint.request_set_sha256}/"
        )


class NativeSourceItemTextCheckpointProgress:
    """Bridge private checkpoints to content-free durable job progress."""

    def __init__(
        self,
        *,
        context: JobExecutionContext,
        store: NativeSourceItemTextDurableCheckpointStore,
        initial: dict[str, Any] | None = None,
    ) -> None:
        self._context = context
        self._store = store
        self._base_progress = {
            key: value
            for key, value in dict(initial or {}).items()
            if key != "source_item_text_window_checkpoint"
        }
        raw_receipt = (initial or {}).get("source_item_text_window_checkpoint")
        self._receipt = (
            NativeSourceItemTextWindowedCheckpointReceiptV1.model_validate(
                raw_receipt
            )
            if isinstance(raw_receipt, dict)
            else None
        )

    @property
    def receipt(self) -> NativeSourceItemTextWindowedCheckpointReceiptV1 | None:
        return self._receipt

    def load(self) -> NativeSourceItemTextWindowedCheckpointV1 | None:
        return None if self._receipt is None else self._store.load(self._receipt)

    def save(
        self,
        checkpoint: NativeSourceItemTextWindowedCheckpointV1 | Mapping[str, Any],
    ) -> None:
        checkpoint = NativeSourceItemTextWindowedCheckpointV1.model_validate(
            checkpoint
        )
        receipt = self._store.save(checkpoint)
        if self._receipt is not None:
            previous = self._receipt
            if (
                previous.source_sha256 != receipt.source_sha256
                or previous.document_map_artifact_sha256
                != receipt.document_map_artifact_sha256
                or previous.request_set_sha256 != receipt.request_set_sha256
                or previous.scope_ids != receipt.scope_ids
                or not set(previous.accepted_boundary_scope_ids).issubset(
                    receipt.accepted_boundary_scope_ids
                )
                or (
                    previous.classification_windows_planned
                    and not receipt.classification_windows_planned
                )
                or (
                    previous.boundary_set_sha256
                    and previous.boundary_set_sha256 != receipt.boundary_set_sha256
                )
                or not set(previous.classification_window_ids).issubset(
                    receipt.classification_window_ids
                )
                or not set(previous.accepted_window_ids).issubset(
                    receipt.accepted_window_ids
                )
            ):
                raise ValueError("source-item text checkpoint progress regressed")
        self._receipt = receipt
        accepted_boundaries = len(receipt.accepted_boundary_scope_ids)
        total_boundaries = len(receipt.scope_ids)
        accepted_windows = len(receipt.accepted_window_ids)
        total_windows = len(receipt.classification_window_ids)
        if accepted_boundaries < total_boundaries:
            stage = "SOURCE_ITEM_TEXT_BOUNDARY_CHECKPOINTED"
            message = (
                f"已保存 {accepted_boundaries}/{total_boundaries} 个文本范围边界。"
            )
        elif not receipt.classification_windows_planned:
            stage = "SOURCE_ITEM_TEXT_BOUNDARIES_FROZEN"
            message = "已保存全部文本范围边界；正在形成分类窗口。"
        elif accepted_windows < total_windows:
            stage = "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW_CHECKPOINTED"
            message = f"已保存 {accepted_windows}/{total_windows} 个分类窗口。"
        else:
            stage = "SOURCE_ITEM_TEXT_WINDOWED_CHECKPOINT_COMPLETE"
            message = "已保存全部文本边界和分类窗口。"
        self._context.heartbeat(
            progress={
                **self._base_progress,
                "stage": stage,
                "message": message,
                "strategy": "TEXT_BOUNDARY_WINDOWS",
                "completed_scope_count": accepted_boundaries,
                "total_scope_count": total_boundaries,
                "completed_window_count": accepted_windows,
                "total_window_count": total_windows,
                "source_item_text_window_checkpoint": receipt.model_dump(
                    mode="json"
                ),
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
        raise ValueError(f"source-item text checkpoint {subject} ID is invalid")
    return normalized


__all__ = [
    "CHECKPOINT_MEDIA_TYPE",
    "CHECKPOINT_RECEIPT_SCHEMA_VERSION",
    "NativeSourceItemTextCheckpointProgress",
    "NativeSourceItemTextDurableCheckpointStore",
    "NativeSourceItemTextWindowedCheckpointReceiptV1",
]
