"""Export real pipeline snapshots for cross-format whole-document review.

DOCX, XLSX, and XLSB execute the current production extraction functions in
order and persist their actual Pydantic snapshots.  TIF executes formal local
preflight only: without an explicitly supplied attachment store it publishes
an honest BLOCKED/NOT_RUN profile and never claims opaque preservation, OCR,
or four-stage extraction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction.build_source_ir import build_source_ir
from app.application.extraction.classify_content import (
    DEFAULT_SEMANTIC_CLASSIFIER_ID,
    DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    classify_content,
)
from app.application.extraction.project_requirements import (
    DEFAULT_REQUIREMENT_PROJECTOR_ID,
    DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
    project_requirements,
)
from app.application.extraction.resolve_structure import (
    DEFAULT_STRUCTURE_RESOLVER_ID,
    DEFAULT_STRUCTURE_RESOLVER_VERSION,
    build_structure_tree,
)
from app.domain.extraction.source_ir import SourceIRStatus
from scripts.document_visual_pipeline_profile import (
    DocumentVisualPipelineProfileRejected,
    build_document_visual_pipeline_profile,
    verify_document_visual_pipeline_profile,
)
from scripts.storage_backup import BackupError, _rename_no_replace


SUPPORTED_EXPORT_FORMATS = frozenset({"docx", "xlsx", "xlsb", "tif"})
_OFFICE_FORMATS = frozenset({"docx", "xlsx", "xlsb"})
_SHA256_LENGTH = 64


class DocumentVisualPipelineExportRejected(RuntimeError):
    """Stable, customer-text-free exporter rejection."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _SourcePin:
    path: Path
    sha256: str
    size: int
    device: int
    inode: int
    mtime_ns: int


def export_document_visual_pipeline(
    source: Path,
    *,
    document_format: str,
    expected_source_sha256: str,
    output_dir: Path,
    corpus_root: Path,
) -> dict[str, Any]:
    """Run the real supported pipeline and atomically publish review artifacts."""

    format_token = _document_format(document_format)
    source_path = Path(source)
    _validate_source_suffix(source_path, format_token)
    source_pin = _pin_source(
        source_path,
        expected_sha256=_sha256(
            expected_source_sha256,
            "pipeline_export_expected_source_sha256_invalid",
        ),
        code="pipeline_export_source",
    )
    destination = Path(output_dir).absolute()
    parent = _controlled_destination_parent(
        destination,
        corpus_root=Path(corpus_root),
    )
    if destination.exists() or destination.is_symlink():
        _fail("pipeline_export_output_exists")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=parent,
        )
    )
    staging.chmod(0o700)
    try:
        report = _inspect_preflight(
            source_path,
            source_pin=source_pin,
            document_format=format_token,
        )
        if format_token in _OFFICE_FORMATS:
            result = _export_office_pipeline(
                source_path,
                source_pin=source_pin,
                document_format=format_token,
                preflight_report=report,
                staging=staging,
                corpus_root=Path(corpus_root),
            )
        else:
            result = _export_tif_preflight(
                source_path,
                source_pin=source_pin,
                preflight_report=report,
                staging=staging,
                corpus_root=Path(corpus_root),
            )
        _verify_source_unchanged(source_pin)
        _verify_staging_inventory(
            staging,
            expected_paths=result["artifact_paths"],
        )
        _fsync_tree(staging)
        if destination.exists() or destination.is_symlink():
            _fail("pipeline_export_output_exists")
        try:
            _rename_no_replace(staging, destination)
        except BackupError as exc:
            if destination.exists() or destination.is_symlink():
                raise DocumentVisualPipelineExportRejected(
                    "pipeline_export_output_exists"
                ) from exc
            raise DocumentVisualPipelineExportRejected(
                "pipeline_export_atomic_publish_failed"
            ) from exc
        _fsync_directory(parent)
        return {
            "document_format": format_token,
            "pipeline_status": result["profile"]["pipeline"]["status"],
            "profile": result["profile"],
            "profile_file_sha256": result["profile_file_sha256"],
            "artifact_sha256": result["artifact_sha256"],
        }
    except DocumentVisualPipelineExportRejected:
        raise
    except DocumentVisualPipelineProfileRejected as exc:
        raise DocumentVisualPipelineExportRejected(
            f"pipeline_export_profile_rejected:{exc.code}"
        ) from exc
    except Exception as exc:
        stage = getattr(exc, "_document_visual_stage", "unknown")
        raise DocumentVisualPipelineExportRejected(
            f"pipeline_export_stage_failed:{stage}"
        ) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _export_office_pipeline(
    source: Path,
    *,
    source_pin: _SourcePin,
    document_format: str,
    preflight_report: Any,
    staging: Path,
    corpus_root: Path,
) -> dict[str, Any]:
    source_ir = _run_stage(
        "build_source_ir",
        build_source_ir,
        source,
        preflight_report=preflight_report,
    )
    source_status = getattr(source_ir, "status", None)
    source_status_value = getattr(source_status, "value", source_status)
    if source_status_value == SourceIRStatus.FAILED.value:
        _fail("pipeline_export_source_ir_failed")
    if getattr(source_ir, "source_sha256", None) != source_pin.sha256:
        _fail("pipeline_export_source_ir_binding_mismatch")

    structure = _run_stage(
        "build_structure_tree",
        build_structure_tree,
        source_ir,
    )
    semantics = _run_stage(
        "classify_content",
        classify_content,
        source_ir,
        structure,
    )
    requirement_view = _run_stage(
        "project_requirements",
        project_requirements,
        source_ir,
        structure,
        semantics,
    )
    snapshots = (
        ("SOURCE_IR", source_ir),
        ("STRUCTURE_TREE", structure),
        ("SEMANTICS", semantics),
        ("REQUIREMENT_VIEW", requirement_view),
    )
    artifact_descriptors: list[dict[str, Any]] = []
    artifact_hashes: dict[str, str] = {}
    for index, (kind, snapshot) in enumerate(snapshots, start=1):
        payload = _model_dump_json_bytes(
            snapshot,
            code=f"pipeline_export_{kind.lower()}_serialization_failed",
        )
        relative_path = f"artifact-{index:04d}.json"
        _write_bytes(staging / relative_path, payload)
        digest = hashlib.sha256(payload).hexdigest()
        artifact_hashes[kind] = digest
        artifact_descriptors.append(
            {
                "kind": kind,
                "path": relative_path,
                "expected_sha256": digest,
                "media_type": "application/json",
                "customer_sensitive": True,
            }
        )

    adapter_status = (
        "COMPLETE"
        if source_status_value == SourceIRStatus.COMPLETE.value
        else "PARTIAL"
    )
    pipeline_status = (
        "COMPLETE" if adapter_status == "COMPLETE" else "PARTIAL"
    )
    components = {
        "adapter": _completed_component(
            component_id=source_ir.adapter_id,
            version=source_ir.adapter_version,
            status=adapter_status,
            snapshot_id=source_ir.source_ir_id,
            artifact_sha256=artifact_hashes["SOURCE_IR"],
        ),
        "resolver": _completed_component(
            component_id=structure.resolver_id,
            version=structure.resolver_version,
            status="COMPLETE",
            snapshot_id=structure.structure_snapshot_id,
            artifact_sha256=artifact_hashes["STRUCTURE_TREE"],
        ),
        "classifier": _completed_component(
            component_id=semantics.classifier_id,
            version=semantics.classifier_version,
            status="COMPLETE",
            snapshot_id=semantics.semantic_snapshot_id,
            artifact_sha256=artifact_hashes["SEMANTICS"],
        ),
        "projector": _completed_component(
            component_id=requirement_view.projector_id,
            version=requirement_view.projector_version,
            status="COMPLETE",
            snapshot_id=requirement_view.requirement_view_snapshot_id,
            artifact_sha256=artifact_hashes["REQUIREMENT_VIEW"],
        ),
    }
    profile = build_document_visual_pipeline_profile(
        source,
        document_format=document_format,
        expected_source_sha256=source_pin.sha256,
        pipeline_status=pipeline_status,
        components=components,
        artifacts=artifact_descriptors,
        output_path=staging / "pipeline-profile.json",
        corpus_root=corpus_root,
    )
    return _verified_export_result(
        source=source,
        source_pin=source_pin,
        document_format=document_format,
        staging=staging,
        profile=profile,
        artifact_hashes=artifact_hashes,
        corpus_root=corpus_root,
    )


def _export_tif_preflight(
    source: Path,
    *,
    source_pin: _SourcePin,
    preflight_report: Any,
    staging: Path,
    corpus_root: Path,
) -> dict[str, Any]:
    if not preflight_report.attachment_acceptable:
        _fail("pipeline_export_tif_attachment_preflight_rejected")
    payload = _tif_preflight_evidence(preflight_report)
    relative_path = "artifact-0001.json"
    _write_bytes(staging / relative_path, payload)
    digest = hashlib.sha256(payload).hexdigest()
    adapter_snapshot = f"preflight-{digest[:32]}"
    not_run_suffix = source_pin.sha256[:32]
    components = {
        "adapter": _blocked_component(
            component_id="opaque-attachment-preflight-only",
            version=preflight_report.schema_version,
            status="BLOCKED",
            snapshot_id=adapter_snapshot,
            evidence_sha256=digest,
        ),
        "resolver": _blocked_component(
            component_id=DEFAULT_STRUCTURE_RESOLVER_ID,
            version=DEFAULT_STRUCTURE_RESOLVER_VERSION,
            status="NOT_RUN",
            snapshot_id=f"not-run-resolver-{not_run_suffix}",
        ),
        "classifier": _blocked_component(
            component_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
            version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            status="NOT_RUN",
            snapshot_id=f"not-run-classifier-{not_run_suffix}",
        ),
        "projector": _blocked_component(
            component_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
            version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            status="NOT_RUN",
            snapshot_id=f"not-run-projector-{not_run_suffix}",
        ),
    }
    profile = build_document_visual_pipeline_profile(
        source,
        document_format="tif",
        expected_source_sha256=source_pin.sha256,
        pipeline_status="BLOCKED",
        components=components,
        artifacts=(
            {
                "kind": "PREFLIGHT_REPORT",
                "path": relative_path,
                "expected_sha256": digest,
                "media_type": "application/json",
                "customer_sensitive": True,
            },
        ),
        output_path=staging / "pipeline-profile.json",
        corpus_root=corpus_root,
    )
    return _verified_export_result(
        source=source,
        source_pin=source_pin,
        document_format="tif",
        staging=staging,
        profile=profile,
        artifact_hashes={"PREFLIGHT_REPORT": digest},
        corpus_root=corpus_root,
    )


def _verified_export_result(
    *,
    source: Path,
    source_pin: _SourcePin,
    document_format: str,
    staging: Path,
    profile: Mapping[str, Any],
    artifact_hashes: Mapping[str, str],
    corpus_root: Path,
) -> dict[str, Any]:
    profile_path = staging / "pipeline-profile.json"
    profile_file_sha256 = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    verified = verify_document_visual_pipeline_profile(
        profile_path,
        source=source,
        document_format=document_format,
        expected_source_sha256=source_pin.sha256,
        expected_profile_file_sha256=profile_file_sha256,
        corpus_root=corpus_root,
    )
    if verified != profile:
        _fail("pipeline_export_profile_verification_mismatch")
    artifact_paths = {
        "pipeline-profile.json",
        *(artifact["path"] for artifact in profile["artifacts"]),
    }
    return {
        "profile": dict(profile),
        "profile_file_sha256": profile_file_sha256,
        "artifact_sha256": dict(artifact_hashes),
        "artifact_paths": artifact_paths,
    }
def _completed_component(
    *,
    component_id: str,
    version: str,
    status: str,
    snapshot_id: str,
    artifact_sha256: str,
) -> dict[str, Any]:
    return _component(
        component_id=component_id,
        version=version,
        status=status,
        snapshot_id=snapshot_id,
        evidence_sha256=artifact_sha256,
    )


def _blocked_component(
    *,
    component_id: str,
    version: str,
    status: str,
    snapshot_id: str,
    evidence_sha256: str | None = None,
) -> dict[str, Any]:
    return _component(
        component_id=component_id,
        version=version,
        status=status,
        snapshot_id=snapshot_id,
        evidence_sha256=evidence_sha256,
    )


def _component(
    *,
    component_id: str,
    version: str,
    status: str,
    snapshot_id: str,
    evidence_sha256: str | None,
) -> dict[str, Any]:
    fingerprint = hashlib.sha256(
        _canonical_json_bytes(
            {
                "id": component_id,
                "version": version,
                "status": status,
                "snapshot_id": snapshot_id,
                "evidence_sha256": evidence_sha256,
            }
        )
    ).hexdigest()
    return {
        "id": component_id,
        "version": version,
        "status": status,
        "snapshot_id": snapshot_id,
        "fingerprint_sha256": fingerprint,
    }


def _tif_preflight_evidence(report: Any) -> bytes:
    payload = {
        "schema_version": report.schema_version,
        "evidence_kind": "OPAQUE_ATTACHMENT_PREFLIGHT_ONLY",
        "source": {
            "source_sha256": report.source_sha256,
            "source_size": report.source_size,
            "detected_format": report.identity.detected_format.value,
            "mime_type": report.identity.mime_type,
        },
        "status": {
            "authenticity": report.authenticity_status.value,
            "support": report.support_status.value,
            "integrity": report.integrity_status.value,
            "access": report.access_status.value,
            "attachment_acceptable": report.attachment_acceptable,
            "pipeline_status": "BLOCKED",
        },
        "page_count": report.page_count,
        "diagnostics": [
            {
                "code": diagnostic.code.value,
                "severity": diagnostic.severity.value,
            }
            for diagnostic in report.diagnostics
        ],
        "opaque_attachment": {
            "preservation_executed": False,
            "ocr_executed": False,
            "deep_recognition_executed": False,
            "downstream_pipeline_executed": False,
            "reason_code": "ATTACHMENT_STORE_NOT_PROVIDED",
        },
    }
    return _canonical_json_bytes(payload) + b"\n"


def _inspect_preflight(
    source: Path,
    *,
    source_pin: _SourcePin,
    document_format: str,
) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        report = _run_stage(
            "preflight",
            LocalDocumentPreflight().inspect,
            source,
        )
    if (
        report.source_sha256 != source_pin.sha256
        or report.source_size != source_pin.size
    ):
        _fail("pipeline_export_preflight_source_mismatch")
    if report.identity.detected_format.value != document_format:
        _fail("pipeline_export_preflight_format_mismatch")
    if document_format in {"docx", "xlsx"} and not report.processable:
        _fail("pipeline_export_preflight_not_processable")
    if document_format == "xlsb" and not report.controlled_conversion_eligible:
        _fail("pipeline_export_xlsb_conversion_not_eligible")
    if document_format == "tif" and not report.attachment_acceptable:
        _fail("pipeline_export_tif_attachment_preflight_rejected")
    return report


def _run_stage(stage: str, function: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        try:
            setattr(exc, "_document_visual_stage", stage)
        except Exception:
            pass
        raise


def _model_dump_json_bytes(value: Any, *, code: str) -> bytes:
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        _fail(code)
    try:
        payload = model_dump(mode="json")
        return _canonical_json_bytes(payload) + b"\n"
    except Exception as exc:
        raise DocumentVisualPipelineExportRejected(code) from exc


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DocumentVisualPipelineExportRejected(
            "pipeline_export_json_not_canonical"
        ) from exc


def _write_bytes(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    except OSError as exc:
        raise DocumentVisualPipelineExportRejected(
            "pipeline_export_artifact_write_failed"
        ) from exc


def _pin_source(path: Path, *, expected_sha256: str, code: str) -> _SourcePin:
    _reject_symlink_chain(path, f"{code}_symlink")
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualPipelineExportRejected(
            f"{code}_unavailable"
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        _fail(f"{code}_not_regular")
    try:
        payload = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualPipelineExportRejected(
            f"{code}_unavailable"
        ) from exc
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        _fail(f"{code}_toctou_detected")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        _fail(f"{code}_hash_mismatch")
    return _SourcePin(
        path=path,
        sha256=digest,
        size=after.st_size,
        device=after.st_dev,
        inode=after.st_ino,
        mtime_ns=after.st_mtime_ns,
    )


def _verify_source_unchanged(pin: _SourcePin) -> None:
    current = _pin_source(
        pin.path,
        expected_sha256=pin.sha256,
        code="pipeline_export_source_drift",
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
        _fail("pipeline_export_source_toctou_detected")


def _controlled_destination_parent(
    destination: Path,
    *,
    corpus_root: Path,
) -> Path:
    if not destination.name or destination.name in {".", ".."}:
        _fail("pipeline_export_output_name_invalid")
    parent = destination.parent
    corpus = Path(corpus_root)
    _reject_symlink_chain(parent, "pipeline_export_output_parent_symlink")
    _reject_symlink_chain(corpus, "pipeline_export_corpus_root_symlink")
    try:
        parent_metadata = parent.stat(follow_symlinks=False)
        corpus_metadata = corpus.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualPipelineExportRejected(
            "pipeline_export_controlled_root_unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or not stat.S_ISDIR(corpus_metadata.st_mode)
    ):
        _fail("pipeline_export_controlled_root_invalid")
    resolved_destination = destination.absolute()
    resolved_corpus = corpus.resolve(strict=True)
    if (
        resolved_destination == resolved_corpus
        or resolved_corpus in resolved_destination.parents
    ):
        _fail("pipeline_export_output_inside_corpus")
    return parent


def _verify_staging_inventory(
    staging: Path,
    *,
    expected_paths: set[str],
) -> None:
    actual_paths: set[str] = set()
    for candidate in staging.rglob("*"):
        metadata = candidate.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            _fail("pipeline_export_staging_symlink")
        if stat.S_ISREG(metadata.st_mode):
            actual_paths.add(candidate.relative_to(staging).as_posix())
        elif not stat.S_ISDIR(metadata.st_mode):
            _fail("pipeline_export_staging_non_regular")
    if actual_paths != expected_paths:
        _fail("pipeline_export_staging_inventory_mismatch")


def _fsync_tree(root: Path) -> None:
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        _fsync_directory(directory)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise DocumentVisualPipelineExportRejected(
            "pipeline_export_directory_fsync_failed"
        ) from exc


def _document_format(value: Any) -> str:
    token = str(value or "").lower()
    if token not in SUPPORTED_EXPORT_FORMATS:
        _fail(
            "pipeline_export_pdf_uses_existing_tool"
            if token == "pdf"
            else "pipeline_export_document_format_unsupported"
        )
    return token


def _validate_source_suffix(path: Path, document_format: str) -> None:
    expected = (
        {".tif", ".tiff"}
        if document_format == "tif"
        else {f".{document_format}"}
    )
    if path.suffix.lower() not in expected:
        _fail("pipeline_export_source_format_mismatch")


def _sha256(value: Any, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
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
            raise DocumentVisualPipelineExportRejected(code) from exc
        if stat.S_ISLNK(metadata.st_mode):
            _fail(code)


def _fail(code: str) -> None:
    raise DocumentVisualPipelineExportRejected(code)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export real ReqPilotX pipeline artifacts for non-PDF visual review."
        )
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--format", required=True, choices=sorted(SUPPORTED_EXPORT_FORMATS))
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = export_document_visual_pipeline(
            args.source,
            document_format=args.format,
            expected_source_sha256=args.expected_source_sha256,
            output_dir=args.output_dir,
            corpus_root=args.corpus_root,
        )
    except DocumentVisualPipelineExportRejected as exc:
        print(exc.code)
        return 2
    print(
        json.dumps(
            {
                "pipeline_status": result["pipeline_status"],
                "profile_file_sha256": result["profile_file_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
