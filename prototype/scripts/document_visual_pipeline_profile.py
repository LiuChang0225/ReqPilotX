"""Build a pinned pipeline profile from already generated JSON artifacts.

This module is a provenance assembler, not a pipeline runner.  Callers must
provide real SourceIR, structure, semantics, and RequirementView JSON outputs
plus explicit component identities.  The builder validates and pins those
inputs, but never invents a successful pipeline execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from scripts.document_visual_review_bundle import (
    PIPELINE_COMPONENTS,
    PIPELINE_PROFILE_SCHEMA_VERSION,
    PIPELINE_STATUSES,
    PROFILE_HASH_CONTRACT,
    SUPPORTED_FORMATS,
    _validate_pipeline_profile,
    canonical_pipeline_fingerprint,
    canonical_profile_sha256,
)
from scripts.storage_backup import BackupError, _rename_no_replace


COMPLETE_PIPELINE_ARTIFACT_KINDS = (
    "SOURCE_IR",
    "STRUCTURE_TREE",
    "SEMANTICS",
    "REQUIREMENT_VIEW",
)
PIPELINE_ARTIFACT_KINDS = (
    *COMPLETE_PIPELINE_ARTIFACT_KINDS,
    "PREFLIGHT_REPORT",
)
_SHA256_LENGTH = 64


class DocumentVisualPipelineProfileRejected(RuntimeError):
    """Stable, text-free profile-builder rejection."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _Pin:
    path: Path
    sha256: str
    size: int
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _Artifact:
    pin: _Pin
    kind: str
    relative_path: str


def build_document_visual_pipeline_profile(
    source: Path,
    *,
    document_format: str,
    expected_source_sha256: str,
    pipeline_status: str,
    components: Mapping[str, Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    output_path: Path,
    corpus_root: Path,
) -> dict[str, Any]:
    """Atomically create one v1 pipeline profile without running the pipeline.

    Artifact paths are relative to ``output_path.parent``.  Each descriptor is
    exactly ``kind``, ``path``, ``expected_sha256``, ``media_type`` and
    ``customer_sensitive``.  The four complete-pipeline kinds must be supplied
    in canonical order.
    """

    format_token = _document_format(document_format)
    source_path = Path(source)
    _validate_source_suffix(source_path, format_token)
    source_pin, _ = _read_pinned_file(
        source_path,
        expected_sha256=_sha256(
            expected_source_sha256,
            "pipeline_profile_expected_source_sha256_invalid",
        ),
        code="pipeline_profile_source",
        retain_payload=False,
    )
    destination = Path(output_path).absolute()
    parent = _controlled_external_parent(
        destination.parent,
        corpus_root=Path(corpus_root),
    )
    if destination.parent != parent:
        _fail("pipeline_profile_output_parent_invalid")
    if destination.exists() or destination.is_symlink():
        _fail("pipeline_profile_output_exists")
    if not destination.name or destination.name in {".", ".."}:
        _fail("pipeline_profile_output_name_invalid")

    loaded_artifacts = _load_artifacts(
        parent,
        artifacts,
        pipeline_status=pipeline_status,
    )
    profile = _profile_payload(
        source_sha256=source_pin.sha256,
        document_format=format_token,
        pipeline_status=pipeline_status,
        components=components,
        artifacts=loaded_artifacts,
    )
    try:
        _validate_pipeline_profile(
            profile,
            source_sha256=source_pin.sha256,
            document_format=format_token,
        )
    except Exception as exc:
        if isinstance(exc, DocumentVisualPipelineProfileRejected):
            raise
        code = getattr(exc, "code", "pipeline_profile_contract_invalid")
        raise DocumentVisualPipelineProfileRejected(str(code)) from exc

    payload = (
        json.dumps(
            profile,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    file_descriptor, staging_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=parent,
    )
    staging = Path(staging_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        staging.chmod(0o600)
        _verify_pins_unchanged(
            (source_pin, *(artifact.pin for artifact in loaded_artifacts))
        )
        staged_pin, staged_payload = _read_pinned_file(
            staging,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            code="pipeline_profile_staging",
            retain_payload=True,
            containment_root=parent,
        )
        del staged_pin
        staged_profile = _parse_json(
            staged_payload,
            "pipeline_profile_staging_json_invalid",
        )
        if staged_profile != profile:
            _fail("pipeline_profile_staging_payload_mismatch")
        if destination.exists() or destination.is_symlink():
            _fail("pipeline_profile_output_exists")
        try:
            _rename_no_replace(staging, destination)
        except BackupError as exc:
            if destination.exists() or destination.is_symlink():
                raise DocumentVisualPipelineProfileRejected(
                    "pipeline_profile_output_exists"
                ) from exc
            raise DocumentVisualPipelineProfileRejected(
                "pipeline_profile_atomic_publish_failed"
            ) from exc
        _fsync_directory(parent)
        return profile
    finally:
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass


def verify_document_visual_pipeline_profile(
    profile_path: Path,
    *,
    source: Path,
    document_format: str,
    expected_source_sha256: str,
    expected_profile_file_sha256: str,
    corpus_root: Path,
) -> dict[str, Any]:
    """Recompute source, profile, artifact, JSON, privacy, and TOCTOU pins."""

    format_token = _document_format(document_format)
    source_path = Path(source)
    _validate_source_suffix(source_path, format_token)
    source_pin, _ = _read_pinned_file(
        source_path,
        expected_sha256=_sha256(
            expected_source_sha256,
            "pipeline_profile_expected_source_sha256_invalid",
        ),
        code="pipeline_profile_source",
        retain_payload=False,
    )
    profile_file = Path(profile_path).absolute()
    parent = _controlled_external_parent(
        profile_file.parent,
        corpus_root=Path(corpus_root),
    )
    profile_pin, payload = _read_pinned_file(
        profile_file,
        expected_sha256=_sha256(
            expected_profile_file_sha256,
            "pipeline_profile_expected_file_sha256_invalid",
        ),
        code="pipeline_profile_file",
        retain_payload=True,
        containment_root=parent,
    )
    profile = _parse_json(payload, "pipeline_profile_json_invalid")
    try:
        _validate_pipeline_profile(
            profile,
            source_sha256=source_pin.sha256,
            document_format=format_token,
        )
    except Exception as exc:
        code = getattr(exc, "code", "pipeline_profile_contract_invalid")
        raise DocumentVisualPipelineProfileRejected(str(code)) from exc
    descriptors = [
        {
            "kind": artifact["kind"],
            "path": artifact["path"],
            "expected_sha256": artifact["sha256"],
            "media_type": artifact["media_type"],
            "customer_sensitive": artifact["customer_sensitive"],
        }
        for artifact in profile["artifacts"]
    ]
    loaded_artifacts = _load_artifacts(
        parent,
        descriptors,
        pipeline_status=profile["pipeline"]["status"],
    )
    _verify_pins_unchanged(
        (
            source_pin,
            profile_pin,
            *(artifact.pin for artifact in loaded_artifacts),
        )
    )
    return profile


def _profile_payload(
    *,
    source_sha256: str,
    document_format: str,
    pipeline_status: str,
    components: Mapping[str, Mapping[str, Any]],
    artifacts: Sequence[_Artifact],
) -> dict[str, Any]:
    status = str(pipeline_status or "")
    if status not in PIPELINE_STATUSES:
        _fail("pipeline_profile_status_invalid")
    if not isinstance(components, Mapping):
        _fail("pipeline_profile_components_invalid")
    if set(components) != set(PIPELINE_COMPONENTS):
        _fail("pipeline_profile_components_incomplete")
    component_payload = {
        name: _component(name, components[name])
        for name in PIPELINE_COMPONENTS
    }
    if status == "COMPLETE" and any(
        component["status"] != "COMPLETE"
        for component in component_payload.values()
    ):
        _fail("pipeline_profile_complete_component_not_complete")
    profile: dict[str, Any] = {
        "schema_version": PIPELINE_PROFILE_SCHEMA_VERSION,
        "profile_hash_contract": PROFILE_HASH_CONTRACT,
        "source": {
            "source_sha256": source_sha256,
            "format": document_format,
        },
        "pipeline": {
            "status": status,
            **component_payload,
            "pipeline_fingerprint_sha256": "0" * _SHA256_LENGTH,
        },
        "artifacts": [
            {
                "kind": artifact.kind,
                "path": artifact.relative_path,
                "sha256": artifact.pin.sha256,
                "media_type": "application/json",
                "customer_sensitive": True,
            }
            for artifact in artifacts
        ],
    }
    profile["pipeline"]["pipeline_fingerprint_sha256"] = (
        canonical_pipeline_fingerprint(profile)
    )
    profile["profile_sha256"] = canonical_profile_sha256(profile)
    return profile


def _component(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"pipeline_profile_{name}_invalid")
    required = {
        "id",
        "version",
        "status",
        "snapshot_id",
        "fingerprint_sha256",
    }
    if set(value) != required:
        _fail(f"pipeline_profile_{name}_fields_invalid")
    status = str(value["status"] or "")
    if status not in PIPELINE_STATUSES:
        _fail(f"pipeline_profile_{name}_status_invalid")
    return {
        "id": _safe_id(value["id"], f"pipeline_profile_{name}_id_invalid"),
        "version": _safe_id(
            value["version"],
            f"pipeline_profile_{name}_version_invalid",
        ),
        "status": status,
        "snapshot_id": _safe_id(
            value["snapshot_id"],
            f"pipeline_profile_{name}_snapshot_id_invalid",
        ),
        "fingerprint_sha256": _sha256(
            value["fingerprint_sha256"],
            f"pipeline_profile_{name}_fingerprint_invalid",
        ),
    }


def _load_artifacts(
    root: Path,
    values: Sequence[Mapping[str, Any]],
    *,
    pipeline_status: str,
) -> tuple[_Artifact, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail("pipeline_profile_artifacts_invalid")
    loaded: list[_Artifact] = []
    seen_paths: set[Path] = set()
    kinds: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            _fail(f"pipeline_profile_artifact_{index}_invalid")
        required = {
            "kind",
            "path",
            "expected_sha256",
            "media_type",
            "customer_sensitive",
        }
        if set(value) != required:
            _fail(f"pipeline_profile_artifact_{index}_fields_invalid")
        kind = str(value["kind"] or "")
        if kind not in PIPELINE_ARTIFACT_KINDS:
            _fail(f"pipeline_profile_artifact_{index}_kind_invalid")
        relative_path = _safe_relative_path(
            value["path"],
            f"pipeline_profile_artifact_{index}_path_invalid",
        )
        if value["media_type"] != "application/json":
            _fail(f"pipeline_profile_artifact_{index}_must_be_json")
        if value["customer_sensitive"] is not True:
            _fail(
                f"pipeline_profile_artifact_{index}_privacy_not_confirmed"
            )
        path = root.joinpath(*PurePosixPath(relative_path).parts)
        pin, payload = _read_pinned_file(
            path,
            expected_sha256=_sha256(
                value["expected_sha256"],
                f"pipeline_profile_artifact_{index}_expected_sha256_invalid",
            ),
            code=f"pipeline_profile_artifact_{index}",
            retain_payload=True,
            containment_root=root,
        )
        _parse_json(
            payload,
            f"pipeline_profile_artifact_{index}_json_invalid",
            allow_array=True,
        )
        resolved = path.resolve(strict=True)
        if resolved in seen_paths:
            _fail("pipeline_profile_artifact_path_duplicate")
        seen_paths.add(resolved)
        kinds.append(kind)
        loaded.append(
            _Artifact(
                pin=pin,
                kind=kind,
                relative_path=relative_path,
            )
        )
    expected_subset = [
        kind for kind in PIPELINE_ARTIFACT_KINDS if kind in set(kinds)
    ]
    if kinds != expected_subset or len(kinds) != len(set(kinds)):
        _fail("pipeline_profile_artifact_kind_order_invalid")
    if (
        pipeline_status == "COMPLETE"
        and tuple(kinds) != COMPLETE_PIPELINE_ARTIFACT_KINDS
    ):
        _fail("pipeline_profile_complete_artifacts_incomplete")
    return tuple(loaded)


def _read_pinned_file(
    path: Path,
    *,
    expected_sha256: str,
    code: str,
    retain_payload: bool,
    containment_root: Path | None = None,
) -> tuple[_Pin, bytes]:
    candidate = Path(path)
    if containment_root is not None:
        root = Path(containment_root)
        try:
            candidate.absolute().relative_to(root.absolute())
        except ValueError:
            _fail(f"{code}_path_escape")
        _reject_symlink_between(root, candidate, f"{code}_symlink")
    else:
        _reject_symlink_chain(candidate, f"{code}_symlink")
    try:
        before = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualPipelineProfileRejected(
            f"{code}_unavailable"
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        _fail(f"{code}_not_regular")
    try:
        payload = candidate.read_bytes()
        after = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualPipelineProfileRejected(
            f"{code}_unavailable"
        ) from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity:
        _fail(f"{code}_toctou_detected")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        _fail(f"{code}_hash_mismatch")
    return (
        _Pin(
            path=candidate,
            sha256=digest,
            size=after.st_size,
            device=after.st_dev,
            inode=after.st_ino,
            mtime_ns=after.st_mtime_ns,
        ),
        payload if retain_payload else b"",
    )


def _verify_pins_unchanged(pins: Sequence[_Pin]) -> None:
    for pin in pins:
        current, _ = _read_pinned_file(
            pin.path,
            expected_sha256=pin.sha256,
            code="pipeline_profile_input_drift",
            retain_payload=False,
        )
        if (
            current.size,
            current.device,
            current.inode,
            current.mtime_ns,
        ) != (
            pin.size,
            pin.device,
            pin.inode,
            pin.mtime_ns,
        ):
            _fail("pipeline_profile_input_toctou_detected")


def _controlled_external_parent(path: Path, *, corpus_root: Path) -> Path:
    parent = Path(path).absolute()
    corpus = Path(corpus_root)
    _reject_symlink_chain(parent, "pipeline_profile_output_parent_symlink")
    _reject_symlink_chain(corpus, "pipeline_profile_corpus_root_symlink")
    try:
        parent_metadata = parent.stat(follow_symlinks=False)
        corpus_metadata = corpus.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualPipelineProfileRejected(
            "pipeline_profile_controlled_root_unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or not stat.S_ISDIR(corpus_metadata.st_mode)
    ):
        _fail("pipeline_profile_controlled_root_invalid")
    resolved_parent = parent.resolve(strict=True)
    resolved_corpus = corpus.resolve(strict=True)
    if (
        resolved_parent == resolved_corpus
        or resolved_corpus in resolved_parent.parents
    ):
        _fail("pipeline_profile_output_inside_corpus")
    return parent


def _parse_json(
    payload: bytes,
    code: str,
    *,
    allow_array: bool = False,
) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail(code)
            result[key] = value
        return result

    def reject_constant(_: str) -> None:
        _fail(code)

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentVisualPipelineProfileRejected(code) from exc
    if not isinstance(value, (dict, list) if allow_array else dict):
        _fail(code)
    return value


def _document_format(value: Any) -> str:
    token = str(value or "").lower()
    if token not in SUPPORTED_FORMATS:
        _fail(
            "pipeline_profile_pdf_uses_existing_tool"
            if token == "pdf"
            else "pipeline_profile_document_format_unsupported"
        )
    return token


def _validate_source_suffix(path: Path, document_format: str) -> None:
    suffix = path.suffix.lower()
    expected = (
        {".tif", ".tiff"}
        if document_format == "tif"
        else {f".{document_format}"}
    )
    if suffix not in expected:
        _fail("pipeline_profile_source_format_mismatch")


def _safe_relative_path(value: Any, code: str) -> str:
    if not isinstance(value, str):
        _fail(code)
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _fail(code)
    return value


def _sha256(value: Any, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(code)
    return value


def _safe_id(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        _fail(code)
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        "0123456789._:@+-/"
    )
    if value[0] not in allowed or any(char not in allowed for char in value):
        _fail(code)
    if ".." in value:
        _fail(code)
    return value


def _reject_symlink_chain(path: Path, code: str) -> None:
    candidate = Path(path).absolute()
    current = Path(candidate.parts[0])
    for part in candidate.parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            continue
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise DocumentVisualPipelineProfileRejected(code) from exc
        if stat.S_ISLNK(metadata.st_mode):
            _fail(code)


def _reject_symlink_between(root: Path, path: Path, code: str) -> None:
    try:
        relative = Path(path).absolute().relative_to(Path(root).absolute())
    except ValueError:
        _fail(f"{code}_escape")
    current = Path(root)
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            _fail(code)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise DocumentVisualPipelineProfileRejected(
            "pipeline_profile_directory_fsync_failed"
        ) from exc


def _fail(code: str) -> None:
    raise DocumentVisualPipelineProfileRejected(code)


__all__ = [
    "DocumentVisualPipelineProfileRejected",
    "COMPLETE_PIPELINE_ARTIFACT_KINDS",
    "PIPELINE_ARTIFACT_KINDS",
    "build_document_visual_pipeline_profile",
    "verify_document_visual_pipeline_profile",
]
