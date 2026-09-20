from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any

from app.domain.extraction.requirement_tree import RequirementTreeSnapshotV1


_MAX_RUNTIME_READ_MODELS = 8
_RUNTIME_READ_MODEL_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class DocumentExtractionRuntimeReadModel:
    source_page_snapshot: dict[str, Any]
    requirement_tree: RequirementTreeSnapshotV1 | None


_RUNTIME_READ_MODELS: OrderedDict[
    tuple[str, str, str, str],
    DocumentExtractionRuntimeReadModel,
] = OrderedDict()


def get_document_extraction_runtime_read_model(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_identity: str,
) -> DocumentExtractionRuntimeReadModel | None:
    if not artifact_identity:
        return None
    key = (project_id, document_id, source_sha256, artifact_identity)
    with _RUNTIME_READ_MODEL_LOCK:
        cached = _RUNTIME_READ_MODELS.get(key)
        if cached is None:
            return None
        _RUNTIME_READ_MODELS.move_to_end(key)
        return cached


def cache_document_extraction_runtime_read_model(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_identity: str,
    payload: dict[str, Any],
    workbench: dict[str, Any] | None = None,
) -> DocumentExtractionRuntimeReadModel | None:
    if not artifact_identity:
        return None
    raw_source_pages = payload.get("source_page_snapshot")
    source_page_snapshot = (
        copy.deepcopy(raw_source_pages)
        if isinstance(raw_source_pages, dict)
        else {}
    )
    requirement_tree: RequirementTreeSnapshotV1 | None = None
    if isinstance(workbench, dict):
        raw_tree = workbench.get("requirement_tree")
        if isinstance(raw_tree, dict):
            requirement_tree = RequirementTreeSnapshotV1.model_validate(raw_tree)

    key = (project_id, document_id, source_sha256, artifact_identity)
    with _RUNTIME_READ_MODEL_LOCK:
        current = _RUNTIME_READ_MODELS.get(key)
        if current is not None:
            if not source_page_snapshot:
                source_page_snapshot = current.source_page_snapshot
            if requirement_tree is None:
                requirement_tree = current.requirement_tree
        cached = DocumentExtractionRuntimeReadModel(
            source_page_snapshot=source_page_snapshot,
            requirement_tree=requirement_tree,
        )
        _RUNTIME_READ_MODELS[key] = cached
        _RUNTIME_READ_MODELS.move_to_end(key)
        while len(_RUNTIME_READ_MODELS) > _MAX_RUNTIME_READ_MODELS:
            _RUNTIME_READ_MODELS.popitem(last=False)
        return cached


def clear_document_extraction_runtime_read_models() -> None:
    with _RUNTIME_READ_MODEL_LOCK:
        _RUNTIME_READ_MODELS.clear()


__all__ = [
    "DocumentExtractionRuntimeReadModel",
    "cache_document_extraction_runtime_read_model",
    "clear_document_extraction_runtime_read_models",
    "get_document_extraction_runtime_read_model",
]
