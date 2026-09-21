from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping


_MAX_CACHED_STRUCTURE_RESPONSES = 4
_MAX_CACHED_STRUCTURE_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_SINGLE_STRUCTURE_RESPONSE_BYTES = 32 * 1024 * 1024
_STRUCTURE_RESPONSE_CACHE_LOCK = RLock()


def canonical_json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def strong_etag(content: bytes) -> str:
    return f'"{hashlib.sha256(content).hexdigest()}"'


def if_none_match_matches(value: str, etag: str) -> bool:
    """Apply the weak comparison required for If-None-Match on GET.

    The response validator itself remains strong.  Accepting a weak request
    validator for the same opaque tag is RFC-compatible and cannot cross a
    response identity because the opaque tag is the response byte digest.
    """

    expected = str(etag or "").strip()
    if not expected:
        return False
    expected_opaque = expected[2:] if expected.startswith("W/") else expected
    for candidate in str(value or "").split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        candidate_opaque = candidate[2:] if candidate.startswith("W/") else candidate
        if candidate_opaque == expected_opaque:
            return True
    return False


@dataclass(frozen=True, slots=True)
class StructureResponseCacheEntry:
    static_identity_sha256: str
    complete_identity_sha256: str
    content: bytes
    etag: str
    effective_structure_snapshot_id: str
    review_session_id: str
    table_projection_revision_sha256: str
    enrichment_state_sha256: str


_STRUCTURE_RESPONSE_CACHE: OrderedDict[
    str,
    StructureResponseCacheEntry,
] = OrderedDict()
_STRUCTURE_RESPONSE_STATIC_INDEX: dict[str, str] = {}
_STRUCTURE_RESPONSE_CACHE_BYTES = 0


def build_static_structure_response_identity(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_receipt: Mapping[str, Any],
    document_identity: Mapping[str, Any],
    source_ir_adapter_version: str,
    structure_resolver_version: str,
    structure_presentation_schema_version: str,
    workbench_schema_version: str,
    requirement_tree_projection_runtime_version: str,
    requirement_mode: str = "WORKING",
    requirement_set_id: str = "",
) -> str:
    return canonical_json_sha256(
        {
            "schema_version": "structure-response-cache-static-identity/v1",
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": source_sha256,
            "artifact_receipt": dict(artifact_receipt),
            "document_identity": dict(document_identity),
            "source_ir_adapter_version": source_ir_adapter_version,
            "structure_resolver_version": structure_resolver_version,
            "structure_presentation_schema_version": (
                structure_presentation_schema_version
            ),
            "workbench_schema_version": workbench_schema_version,
            "requirement_tree_projection_runtime_version": (
                requirement_tree_projection_runtime_version
            ),
            "requirement_mode": requirement_mode,
            "requirement_set_id": requirement_set_id,
        }
    )


def build_complete_structure_response_identity(
    *,
    static_identity_sha256: str,
    base_structure_identity: Mapping[str, Any],
    effective_structure_identity: Mapping[str, Any],
    revision_chain_sha256: str,
    job_identity: Mapping[str, Any],
    payload_runtime_identity: Mapping[str, Any],
    table_projection_revision_sha256: str,
    enrichment_state_sha256: str,
) -> str:
    return canonical_json_sha256(
        {
            "schema_version": "structure-response-cache-complete-identity/v1",
            "static_identity_sha256": static_identity_sha256,
            "base_structure_identity": dict(base_structure_identity),
            "effective_structure_identity": dict(effective_structure_identity),
            "revision_chain_sha256": revision_chain_sha256,
            "job_identity": dict(job_identity),
            "payload_runtime_identity": dict(payload_runtime_identity),
            "table_projection_revision_sha256": (table_projection_revision_sha256),
            "enrichment_state_sha256": enrichment_state_sha256,
        }
    )


def get_structure_response_cache_candidate(
    static_identity_sha256: str,
) -> StructureResponseCacheEntry | None:
    with _STRUCTURE_RESPONSE_CACHE_LOCK:
        complete_identity = _STRUCTURE_RESPONSE_STATIC_INDEX.get(static_identity_sha256)
        cached = (
            _STRUCTURE_RESPONSE_CACHE.get(complete_identity)
            if complete_identity is not None
            else None
        )
        if cached is None:
            _STRUCTURE_RESPONSE_STATIC_INDEX.pop(
                static_identity_sha256,
                None,
            )
            return None
        _STRUCTURE_RESPONSE_CACHE.move_to_end(complete_identity)
        return cached


def cache_structure_response(
    entry: StructureResponseCacheEntry,
) -> StructureResponseCacheEntry | None:
    global _STRUCTURE_RESPONSE_CACHE_BYTES

    content_size = len(entry.content)
    if (
        not entry.static_identity_sha256
        or not entry.complete_identity_sha256
        or content_size < 1
        or content_size > _MAX_SINGLE_STRUCTURE_RESPONSE_BYTES
    ):
        return None
    with _STRUCTURE_RESPONSE_CACHE_LOCK:
        previous_complete_identity = _STRUCTURE_RESPONSE_STATIC_INDEX.pop(
            entry.static_identity_sha256,
            None,
        )
        previous = (
            _STRUCTURE_RESPONSE_CACHE.pop(previous_complete_identity, None)
            if previous_complete_identity is not None
            else None
        )
        if previous is not None:
            _STRUCTURE_RESPONSE_CACHE_BYTES -= len(previous.content)
        _STRUCTURE_RESPONSE_CACHE[entry.complete_identity_sha256] = entry
        _STRUCTURE_RESPONSE_STATIC_INDEX[entry.static_identity_sha256] = (
            entry.complete_identity_sha256
        )
        _STRUCTURE_RESPONSE_CACHE_BYTES += content_size
        _STRUCTURE_RESPONSE_CACHE.move_to_end(entry.complete_identity_sha256)
        while (
            len(_STRUCTURE_RESPONSE_CACHE) > _MAX_CACHED_STRUCTURE_RESPONSES
            or _STRUCTURE_RESPONSE_CACHE_BYTES > _MAX_CACHED_STRUCTURE_RESPONSE_BYTES
        ):
            complete_identity, evicted = _STRUCTURE_RESPONSE_CACHE.popitem(last=False)
            _STRUCTURE_RESPONSE_CACHE_BYTES -= len(evicted.content)
            if (
                _STRUCTURE_RESPONSE_STATIC_INDEX.get(evicted.static_identity_sha256)
                == complete_identity
            ):
                _STRUCTURE_RESPONSE_STATIC_INDEX.pop(
                    evicted.static_identity_sha256,
                    None,
                )
        return (
            entry
            if _STRUCTURE_RESPONSE_CACHE.get(entry.complete_identity_sha256) is entry
            else None
        )


def clear_structure_response_cache() -> None:
    global _STRUCTURE_RESPONSE_CACHE_BYTES

    with _STRUCTURE_RESPONSE_CACHE_LOCK:
        _STRUCTURE_RESPONSE_CACHE.clear()
        _STRUCTURE_RESPONSE_STATIC_INDEX.clear()
        _STRUCTURE_RESPONSE_CACHE_BYTES = 0


__all__ = [
    "StructureResponseCacheEntry",
    "build_complete_structure_response_identity",
    "build_static_structure_response_identity",
    "cache_structure_response",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "clear_structure_response_cache",
    "get_structure_response_cache_candidate",
    "if_none_match_matches",
    "strong_etag",
]
