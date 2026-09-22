"""Validated payload contract for active LLM extraction checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from reqpilotx_server.domain.extraction.processing_profile import ResolvedProcessingPlanV1
from reqpilotx_server.services.extraction_visual_routes import (
    extraction_visual_strategy_identity,
    normalize_extraction_visual_strategy_identity,
)

LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION = "llm-extraction-job-checkpoint/v4"
_V3_LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION = (
    "llm-extraction-job-checkpoint/v3"
)
_V2_LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION = (
    "llm-extraction-job-checkpoint/v2"
)
LLM_EXTRACTION_PARTIAL_SCHEMA_VERSION = "extraction-llm-document-partial/v1"

_ACTIVE_STATUSES = {"PENDING", "RUNNING"}
_CHECKPOINT_STATUSES = {*_ACTIVE_STATUSES, "FAILED"}
_JOB_ID_PATTERN = re.compile(r"^llm-extraction-[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_PROGRESS_KEYS = (
    "total_clauses",
    "total_targets",
    "total_chunks",
    "completed_chunks",
    "completed_targets",
    "successful_chunks",
    "unresolved_chunks",
    "blocked_chunks",
    "current_chunk_index",
    "current_chunk_target_count",
)
_JOB_KEYS = {
    "job_id",
    "project_id",
    "document_id",
    "source_sha256",
    "visual_strategy",
    "processing_plan",
    "status",
    "stage",
    "progress",
    "error_code",
    "error_stage",
    "error_cause_code",
    "partial_result",
    "partial_revision",
    "partial_completed_chunk_indexes",
    "final_result_sha256",
    "created_at_utc",
    "started_at_utc",
    "finished_at_utc",
    "updated_at_utc",
}
_V3_JOB_KEYS = _JOB_KEYS - {"processing_plan"}
_V2_JOB_KEYS = _V3_JOB_KEYS - {"error_stage", "error_cause_code"}
_RUNTIME_IDENTITY_KEYS = {
    "preview_schema_version",
    "partial_schema_version",
    "pdf_source_ir_adapter_version",
    "structure_resolver_version",
    "visual_strategy",
}


def canonical_llm_extraction_payload_sha256(payload: Mapping[str, Any]) -> str:
    """Hash one JSON payload using the same canonical form as the file cache."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a JSON object")
    serialized = json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def build_llm_extraction_job_checkpoint(
    job: Mapping[str, Any],
    *,
    preview_schema_version: str,
    pdf_source_ir_adapter_version: str,
    structure_resolver_version: str,
    visual_strategy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and self-validate a JSON-safe checkpoint from one live job."""

    if not isinstance(job, Mapping):
        raise TypeError("job must be an object")
    progress = {key: job.get(key) for key in _PROGRESS_KEYS}
    expected_visual_strategy = _checkpoint_visual_strategy(
        visual_strategy or job.get("visual_strategy"),
        pdf_source_ir_adapter_version=pdf_source_ir_adapter_version,
    )
    partial_result = job.get("partial_result")
    if isinstance(partial_result, Mapping):
        partial_result = dict(partial_result)
        stored_partial_strategy = partial_result.get("visual_strategy")
        if stored_partial_strategy is None:
            partial_result["visual_strategy"] = expected_visual_strategy
        elif (
            normalize_extraction_visual_strategy_identity(
                stored_partial_strategy,
                expected_source_ir_adapter_version=pdf_source_ir_adapter_version,
            )
            != expected_visual_strategy
        ):
            raise ValueError("partial result visual strategy is invalid")
    processing_plan = job.get("processing_plan")
    checkpoint_schema_version = (
        LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION
        if processing_plan is not None
        else _V3_LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION
    )
    checkpoint_job = {
        "job_id": job.get("job_id"),
        "project_id": job.get("project_id"),
        "document_id": job.get("document_id"),
        "source_sha256": job.get("source_sha256"),
        "visual_strategy": expected_visual_strategy,
        "status": job.get("status"),
        "stage": job.get("stage"),
        "progress": progress,
        "error_code": job.get("error_code"),
        "error_stage": job.get("error_stage") or "",
        "error_cause_code": job.get("error_cause_code") or "",
        "partial_result": partial_result,
        "partial_revision": job.get("partial_revision"),
        "partial_completed_chunk_indexes": list(
            job.get("partial_completed_chunk_indexes") or ()
        ),
        "final_result_sha256": job.get("final_result_sha256") or "",
        "created_at_utc": job.get("created_at_utc"),
        "started_at_utc": job.get("started_at_utc"),
        "finished_at_utc": job.get("finished_at_utc"),
        "updated_at_utc": job.get("updated_at_utc"),
    }
    if processing_plan is not None:
        checkpoint_job["processing_plan"] = processing_plan
    payload = {
        "schema_version": checkpoint_schema_version,
        "runtime_identity": _runtime_identity(
            preview_schema_version=preview_schema_version,
            pdf_source_ir_adapter_version=pdf_source_ir_adapter_version,
            structure_resolver_version=structure_resolver_version,
            visual_strategy=expected_visual_strategy,
        ),
        "job": checkpoint_job,
    }
    normalized = json.loads(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    restored = restore_llm_extraction_job_checkpoint(
        normalized,
        project_id=str(job.get("project_id") or ""),
        document_id=str(job.get("document_id") or ""),
        source_sha256=str(job.get("source_sha256") or ""),
        preview_schema_version=preview_schema_version,
        pdf_source_ir_adapter_version=pdf_source_ir_adapter_version,
        structure_resolver_version=structure_resolver_version,
        visual_strategy=expected_visual_strategy,
    )
    if restored is None:
        raise ValueError("job checkpoint is invalid")
    return normalized


def restore_llm_extraction_job_checkpoint(
    payload: Mapping[str, Any],
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    preview_schema_version: str,
    pdf_source_ir_adapter_version: str | None,
    structure_resolver_version: str,
    visual_strategy: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a normalized job only when every binding remains valid."""

    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"schema_version", "runtime_identity", "job"}
        or payload.get("schema_version")
        not in {
            LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION,
            _V3_LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION,
            _V2_LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION,
        }
    ):
        return None
    runtime_identity = payload.get("runtime_identity")
    if (
        not isinstance(runtime_identity, Mapping)
        or set(runtime_identity) != _RUNTIME_IDENTITY_KEYS
    ):
        return None
    stored_adapter_version = runtime_identity.get("pdf_source_ir_adapter_version")
    if (
        not isinstance(stored_adapter_version, str)
        or not stored_adapter_version.strip()
        or (
            pdf_source_ir_adapter_version is not None
            and stored_adapter_version != pdf_source_ir_adapter_version
        )
    ):
        return None
    try:
        expected_visual_strategy = _checkpoint_visual_strategy(
            visual_strategy or runtime_identity.get("visual_strategy"),
            pdf_source_ir_adapter_version=stored_adapter_version,
        )
        expected_runtime = _runtime_identity(
            preview_schema_version=preview_schema_version,
            pdf_source_ir_adapter_version=stored_adapter_version,
            structure_resolver_version=structure_resolver_version,
            visual_strategy=expected_visual_strategy,
        )
    except (TypeError, ValueError):
        return None
    if dict(runtime_identity) != expected_runtime:
        return None
    checkpoint_schema_version = str(payload.get("schema_version") or "")
    job = payload.get("job")
    if checkpoint_schema_version == _V2_LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION:
        expected_job_keys = _V2_JOB_KEYS
    elif checkpoint_schema_version == _V3_LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION:
        expected_job_keys = _V3_JOB_KEYS
    else:
        expected_job_keys = _JOB_KEYS
    if not isinstance(job, Mapping) or set(job) != expected_job_keys:
        return None
    try:
        if (
            normalize_extraction_visual_strategy_identity(
                job.get("visual_strategy"),
                expected_source_ir_adapter_version=stored_adapter_version,
            )
            != expected_visual_strategy
        ):
            return None
    except (TypeError, ValueError):
        return None
    if (
        job.get("project_id") != project_id
        or job.get("document_id") != document_id
        or job.get("source_sha256") != source_sha256
        or not _valid_identifier(project_id)
        or not _valid_identifier(document_id)
        or not _SHA256_PATTERN.fullmatch(source_sha256)
    ):
        return None
    processing_plan = job.get("processing_plan")
    if checkpoint_schema_version == LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION:
        try:
            normalized_plan = ResolvedProcessingPlanV1.model_validate(
                processing_plan
            )
        except (ValidationError, TypeError, ValueError):
            return None
        if (
            normalized_plan.project_id != project_id
            or normalized_plan.document_id != document_id
            or normalized_plan.source_sha256 != source_sha256
        ):
            return None
        processing_plan = normalized_plan.model_dump(mode="json")
    else:
        processing_plan = None
    job_id = job.get("job_id")
    status = str(job.get("status") or "").upper()
    stage = job.get("stage")
    error_code = job.get("error_code")
    error_stage = job.get("error_stage", "")
    error_cause_code = job.get("error_cause_code", "")
    final_result_sha256 = job.get("final_result_sha256")
    if (
        not isinstance(job_id, str)
        or not _JOB_ID_PATTERN.fullmatch(job_id)
        or status not in _CHECKPOINT_STATUSES
        or not isinstance(stage, str)
        or not _TOKEN_PATTERN.fullmatch(stage)
        or not isinstance(error_code, str)
        or (error_code and not _TOKEN_PATTERN.fullmatch(error_code))
        or not isinstance(error_stage, str)
        or (error_stage and not _TOKEN_PATTERN.fullmatch(error_stage))
        or not isinstance(error_cause_code, str)
        or (error_cause_code and not _TOKEN_PATTERN.fullmatch(error_cause_code))
        or not isinstance(final_result_sha256, str)
        or (final_result_sha256 and not _SHA256_PATTERN.fullmatch(final_result_sha256))
    ):
        return None
    if final_result_sha256 and (status != "RUNNING" or stage != "finalizing"):
        return None
    if status == "FAILED" and (not error_code or final_result_sha256):
        return None

    progress = job.get("progress")
    if not isinstance(progress, Mapping) or set(progress) != set(_PROGRESS_KEYS):
        return None
    normalized_progress: dict[str, int | None] = {}
    for key in _PROGRESS_KEYS:
        value = progress.get(key)
        if key == "current_chunk_index" and value is None:
            normalized_progress[key] = None
            continue
        if not _is_nonnegative_int(value):
            return None
        normalized_progress[key] = int(value)
    total_chunks = int(normalized_progress["total_chunks"] or 0)
    completed_chunks = int(normalized_progress["completed_chunks"] or 0)
    if total_chunks and completed_chunks > total_chunks:
        return None
    terminal_chunks = sum(
        int(normalized_progress[key] or 0)
        for key in (
            "successful_chunks",
            "unresolved_chunks",
            "blocked_chunks",
        )
    )
    if terminal_chunks > completed_chunks:
        return None

    partial_revision = job.get("partial_revision")
    completed_indexes = job.get("partial_completed_chunk_indexes")
    partial_result = job.get("partial_result")
    if (
        not _is_nonnegative_int(partial_revision)
        or not isinstance(completed_indexes, list)
        or any(
            not _is_nonnegative_int(index)
            or int(index) < 1
            or (total_chunks > 0 and int(index) > total_chunks)
            for index in completed_indexes
        )
        or len(set(completed_indexes)) != len(completed_indexes)
        or len(completed_indexes) != int(partial_revision)
    ):
        return None
    if int(partial_revision) == 0:
        if partial_result is not None or completed_indexes:
            return None
    elif not _valid_partial_result(
        partial_result,
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        job_id=job_id,
        revision=int(partial_revision),
        completed_indexes=completed_indexes,
        visual_strategy=expected_visual_strategy,
    ):
        return None
    if status == "FAILED" and (
        partial_result is not None or int(partial_revision) != 0 or completed_indexes
    ):
        return None

    timestamp_keys = (
        "created_at_utc",
        "started_at_utc",
        "finished_at_utc",
        "updated_at_utc",
    )
    if any(not isinstance(job.get(key), str) for key in timestamp_keys):
        return None

    return {
        "job_id": job_id,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "visual_strategy": expected_visual_strategy,
        "processing_plan": processing_plan,
        "status": status,
        "stage": stage,
        **normalized_progress,
        "error_code": error_code,
        "error_stage": error_stage,
        "error_cause_code": error_cause_code,
        "result": None,
        "partial_result": partial_result,
        "partial_revision": int(partial_revision),
        "partial_completed_chunk_indexes": tuple(completed_indexes),
        "final_result_sha256": final_result_sha256,
        **{key: str(job.get(key) or "") for key in timestamp_keys},
    }


def _runtime_identity(
    *,
    preview_schema_version: str,
    pdf_source_ir_adapter_version: str,
    structure_resolver_version: str,
    visual_strategy: Mapping[str, Any],
) -> dict[str, Any]:
    values = {
        "preview_schema_version": preview_schema_version,
        "partial_schema_version": LLM_EXTRACTION_PARTIAL_SCHEMA_VERSION,
        "pdf_source_ir_adapter_version": pdf_source_ir_adapter_version,
        "structure_resolver_version": structure_resolver_version,
    }
    if any(
        not isinstance(value, str) or not value.strip() for value in values.values()
    ):
        raise ValueError("runtime identity values must be non-empty strings")
    return {
        **values,
        "visual_strategy": normalize_extraction_visual_strategy_identity(
            visual_strategy,
            expected_source_ir_adapter_version=pdf_source_ir_adapter_version,
        ),
    }


def _checkpoint_visual_strategy(
    value: Any,
    *,
    pdf_source_ir_adapter_version: str,
) -> dict[str, Any]:
    if value is None:
        return extraction_visual_strategy_identity(
            None,
            source_ir_adapter_version=pdf_source_ir_adapter_version,
        )
    return normalize_extraction_visual_strategy_identity(
        value,
        expected_source_ir_adapter_version=pdf_source_ir_adapter_version,
    )


def _valid_partial_result(
    value: Any,
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    revision: int,
    completed_indexes: list[Any],
    visual_strategy: Mapping[str, Any],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    source_identity = value.get("source_identity")
    coverage = value.get("coverage")
    if (
        value.get("schema_version") != LLM_EXTRACTION_PARTIAL_SCHEMA_VERSION
        or value.get("mode") != "READ_ONLY_LLM_PARTIAL"
        or value.get("editable") is not False
        or value.get("project_id") != project_id
        or value.get("document_id") != document_id
        or value.get("job_id") != job_id
        or value.get("source_sha256") != source_sha256
        or value.get("revision") != revision
        or value.get("visual_strategy") != dict(visual_strategy)
        or not isinstance(source_identity, Mapping)
        or source_identity.get("source_sha256") != source_sha256
        or not isinstance(coverage, Mapping)
        or coverage.get("completed_chunks") != revision
        or coverage.get("completed_chunk_indexes") != completed_indexes
    ):
        return False
    return True


def _valid_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "\x00" not in value
    )


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = [
    "LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION",
    "LLM_EXTRACTION_PARTIAL_SCHEMA_VERSION",
    "build_llm_extraction_job_checkpoint",
    "canonical_llm_extraction_payload_sha256",
    "restore_llm_extraction_job_checkpoint",
]
