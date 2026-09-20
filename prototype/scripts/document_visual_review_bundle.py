"""Build strict whole-document visual-review inputs for non-PDF formats.

The exporter never renders a source document and never runs the extraction
pipeline.  It consumes two independently pinned, already generated profiles:
one for ordered page PNGs and one for pipeline identities/snapshots.  Customer
artifacts are copied into a new corpus-external directory and published by one
no-replace atomic rename.

PDF deliberately remains on ``pdf_visual_review_bundle.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from PIL import Image

from scripts.storage_backup import BackupError, _rename_no_replace


RENDER_PROFILE_SCHEMA_VERSION = "document-visual-render-profile/v1"
PIPELINE_PROFILE_SCHEMA_VERSION = "document-visual-pipeline-profile/v1"
BUNDLE_SCHEMA_VERSION = "document-visual-review-bundle/v1"
BUNDLE_HASH_CONTRACT = "canonical-json-without-bundle-sha256/v1"
PROFILE_HASH_CONTRACT = "canonical-json-without-profile-sha256/v1"
SUPPORTED_FORMATS = frozenset({"docx", "xlsx", "xlsb", "tif"})
PIPELINE_COMPONENTS = ("adapter", "resolver", "classifier", "projector")
PIPELINE_STATUSES = frozenset(
    {"COMPLETE", "PARTIAL", "BLOCKED", "FAILED", "NOT_RUN"}
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-/]{0,255}$")
_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+\-]{0,159}$")
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+\-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+\-]{0,127}$"
)
_EXTERNAL_URL = re.compile(
    r"""(?ix)\b(?:https?|ftp|file|data):|\b(?:src|href)\s*=\s*["']\s*//"""
)

_PRIVACY = {
    "classification": "CUSTOMER_SENSITIVE",
    "handling": "CONTROLLED_LOCAL_REVIEW_ONLY",
    "contains_customer_content": True,
    "contains_source_path": False,
    "contains_source_filename": False,
    "is_anonymized": False,
    "source_hash_is_anonymization": False,
}
_PROVENANCE = {
    "is_technical_draft": False,
    "is_human_review": False,
    "is_human_gold": False,
    "is_gold": False,
    "is_formal_approval": False,
    "is_serving_activation_gate": False,
}


class DocumentVisualReviewBundleRejected(RuntimeError):
    """Stable, text-free rejection from the cross-format bundle contract."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _PinnedFile:
    path: Path
    sha256: str
    size: int
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _LoadedArtifact:
    pin: _PinnedFile
    payload: bytes
    media_type: str
    width_px: int | None = None
    height_px: int | None = None


@dataclass(frozen=True, slots=True)
class _InputSet:
    source: _PinnedFile
    document_format: str
    render_profile_path: _PinnedFile
    render_profile: dict[str, Any]
    render_artifacts: tuple[_LoadedArtifact, ...]
    pipeline_profile_path: _PinnedFile
    pipeline_profile: dict[str, Any]
    pipeline_artifacts: tuple[_LoadedArtifact, ...]


def canonical_json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DocumentVisualReviewBundleRejected(
            "bundle_payload_not_canonical_json"
        ) from exc
    return rendered.encode("utf-8")


def canonical_profile_sha256(profile: Mapping[str, Any]) -> str:
    payload = dict(profile)
    payload.pop("profile_sha256", None)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def canonical_pipeline_fingerprint(profile: Mapping[str, Any]) -> str:
    pipeline = _object(profile.get("pipeline"), "pipeline_profile_pipeline_invalid")
    artifacts = profile.get("artifacts")
    if not isinstance(artifacts, list):
        _reject("pipeline_profile_artifacts_invalid")
    payload = {
        "status": pipeline.get("status"),
        "components": {
            key: pipeline.get(key) for key in PIPELINE_COMPONENTS
        },
        "artifacts": artifacts,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def canonical_bundle_sha256(manifest: Mapping[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("bundle_sha256", None)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def canonical_artifact_inventory_sha256(
    inventory: Iterable[Mapping[str, Any]],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes([dict(record) for record in inventory])
    ).hexdigest()


def export_document_visual_review_bundle(
    source: Path,
    *,
    document_id: str,
    document_format: str,
    expected_source_sha256: str,
    render_profile_path: Path,
    expected_render_profile_sha256: str,
    pipeline_profile_path: Path,
    expected_pipeline_profile_sha256: str,
    output_dir: Path,
    corpus_root: Path,
) -> dict[str, Any]:
    """Atomically publish one verified full-document review input bundle."""

    document_token = _document_id(document_id, source=Path(source))
    inputs = _load_input_set(
        source=Path(source),
        document_format=document_format,
        expected_source_sha256=expected_source_sha256,
        render_profile_path=Path(render_profile_path),
        expected_render_profile_sha256=expected_render_profile_sha256,
        pipeline_profile_path=Path(pipeline_profile_path),
        expected_pipeline_profile_sha256=expected_pipeline_profile_sha256,
    )
    destination = _controlled_destination(
        Path(output_dir),
        corpus_root=Path(corpus_root),
    )
    if destination.exists():
        _reject("bundle_output_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_chain(destination.parent, "bundle_output_parent_symlink")

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
    )
    staging.chmod(0o700)
    try:
        page_records: list[dict[str, Any]] = []
        inventory: list[dict[str, Any]] = []
        for index, (page, artifact) in enumerate(
            zip(
                inputs.render_profile["pages"],
                inputs.render_artifacts,
                strict=True,
            ),
            start=1,
        ):
            relative_path = f"renders/page-{index:04d}.png"
            _write_bytes(staging / relative_path, artifact.payload)
            record = {
                "path": relative_path,
                "sha256": artifact.pin.sha256,
                "media_type": "image/png",
                "width_px": artifact.width_px,
                "height_px": artifact.height_px,
            }
            page_records.append(
                {
                    "page_number": index,
                    "locator": dict(page["locator"]),
                    "render": record,
                }
            )
            inventory.append(
                {
                    "kind": "PAGE_RENDER",
                    "page_number": index,
                    **record,
                }
            )

        pipeline_records: list[dict[str, Any]] = []
        for index, (declared, artifact) in enumerate(
            zip(
                inputs.pipeline_profile["artifacts"],
                inputs.pipeline_artifacts,
                strict=True,
            ),
            start=1,
        ):
            suffix = _artifact_suffix(artifact.media_type)
            relative_path = f"pipeline/artifact-{index:04d}{suffix}"
            _write_bytes(staging / relative_path, artifact.payload)
            record = {
                "artifact_number": index,
                "kind": str(declared["kind"]),
                "path": relative_path,
                "sha256": artifact.pin.sha256,
                "media_type": artifact.media_type,
                "customer_sensitive": True,
            }
            pipeline_records.append(record)
            inventory.append(dict(record))

        index_bytes = _offline_index(
            document_id=document_token,
            source_sha256=inputs.source.sha256,
            document_format=inputs.document_format,
            pages=page_records,
            pipeline_artifacts=pipeline_records,
        )
        _write_bytes(staging / "index.html", index_bytes)
        entrypoint = {
            "path": "index.html",
            "sha256": hashlib.sha256(index_bytes).hexdigest(),
            "media_type": "text/html",
            "offline": True,
            "external_urls": False,
        }
        inventory.append(
            {
                "kind": "OFFLINE_INDEX",
                "path": "index.html",
                "sha256": entrypoint["sha256"],
                "media_type": "text/html",
            }
        )

        render_profile = inputs.render_profile
        pipeline_profile = inputs.pipeline_profile
        manifest: dict[str, Any] = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bundle_kind": "WHOLE_DOCUMENT_VISUAL_REVIEW_INPUT",
            "bundle_hash_contract": BUNDLE_HASH_CONTRACT,
            "document": {
                "document_id": document_token,
                "source_sha256": inputs.source.sha256,
                "format": inputs.document_format,
                "page_count": len(page_records),
            },
            "profile_pins": {
                "render": {
                    "file_sha256": inputs.render_profile_path.sha256,
                    "profile_sha256": render_profile["profile_sha256"],
                    "schema_version": render_profile["schema_version"],
                },
                "pipeline": {
                    "file_sha256": inputs.pipeline_profile_path.sha256,
                    "profile_sha256": pipeline_profile["profile_sha256"],
                    "schema_version": pipeline_profile["schema_version"],
                },
            },
            "renderer": dict(render_profile["renderer"]),
            "pipeline": dict(pipeline_profile["pipeline"]),
            "coverage": {
                "full_document": True,
                "page_numbers": list(range(1, len(page_records) + 1)),
                "page_count": len(page_records),
            },
            "pages": page_records,
            "pipeline_artifacts": pipeline_records,
            "entrypoint": entrypoint,
            "artifact_inventory": inventory,
            "artifact_inventory_sha256": (
                canonical_artifact_inventory_sha256(inventory)
            ),
            "privacy": dict(_PRIVACY),
            "review_provenance": dict(_PROVENANCE),
        }
        manifest["bundle_sha256"] = canonical_bundle_sha256(manifest)
        _write_json(staging / "manifest.json", manifest)
        _fsync_directory(staging)

        _verify_input_set_unchanged(inputs)
        _verify_bundle_tree(staging, expected=manifest)
        if destination.exists():
            _reject("bundle_output_exists")
        try:
            _rename_no_replace(staging, destination)
        except BackupError as exc:
            if destination.exists():
                raise DocumentVisualReviewBundleRejected(
                    "bundle_output_exists"
                ) from exc
            raise DocumentVisualReviewBundleRejected(
                "bundle_atomic_publish_failed"
            ) from exc
        _fsync_directory(destination.parent)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def verify_document_visual_review_bundle(
    bundle_dir: Path,
    *,
    source: Path,
    document_format: str,
    expected_source_sha256: str,
    render_profile_path: Path,
    expected_render_profile_sha256: str,
    pipeline_profile_path: Path,
    expected_pipeline_profile_sha256: str,
    expected_bundle_sha256: str,
    expected_artifact_inventory_sha256: str,
) -> dict[str, Any]:
    """Recompute all external pins and verify every published artifact."""

    expected_bundle_hash = _sha256(
        expected_bundle_sha256,
        "bundle_expected_sha256_invalid",
    )
    expected_inventory_hash = _sha256(
        expected_artifact_inventory_sha256,
        "bundle_expected_inventory_sha256_invalid",
    )
    inputs = _load_input_set(
        source=Path(source),
        document_format=document_format,
        expected_source_sha256=expected_source_sha256,
        render_profile_path=Path(render_profile_path),
        expected_render_profile_sha256=expected_render_profile_sha256,
        pipeline_profile_path=Path(pipeline_profile_path),
        expected_pipeline_profile_sha256=expected_pipeline_profile_sha256,
    )
    root = _regular_directory(Path(bundle_dir), "bundle_verify_unavailable")
    manifest = _load_manifest(root)
    if manifest.get("bundle_sha256") != expected_bundle_hash:
        _reject("bundle_expected_sha256_mismatch")
    if (
        manifest.get("artifact_inventory_sha256")
        != expected_inventory_hash
    ):
        _reject("bundle_expected_inventory_sha256_mismatch")
    expected = _expected_manifest_contract(manifest, inputs)
    _verify_bundle_tree(root, expected=expected)
    return manifest


def _expected_manifest_contract(
    manifest: dict[str, Any],
    inputs: _InputSet,
) -> dict[str, Any]:
    """Return a copy whose source/profile-derived fields are authoritative."""

    expected = json.loads(json.dumps(manifest))
    expected["document"].update(
        {
            "source_sha256": inputs.source.sha256,
            "format": inputs.document_format,
            "page_count": len(inputs.render_artifacts),
        }
    )
    expected["profile_pins"] = {
        "render": {
            "file_sha256": inputs.render_profile_path.sha256,
            "profile_sha256": inputs.render_profile["profile_sha256"],
            "schema_version": inputs.render_profile["schema_version"],
        },
        "pipeline": {
            "file_sha256": inputs.pipeline_profile_path.sha256,
            "profile_sha256": inputs.pipeline_profile["profile_sha256"],
            "schema_version": inputs.pipeline_profile["schema_version"],
        },
    }
    expected["renderer"] = dict(inputs.render_profile["renderer"])
    expected["pipeline"] = dict(inputs.pipeline_profile["pipeline"])
    expected["coverage"] = {
        "full_document": True,
        "page_numbers": list(range(1, len(inputs.render_artifacts) + 1)),
        "page_count": len(inputs.render_artifacts),
    }
    expected_pages = []
    for index, (declared, artifact) in enumerate(
        zip(
            inputs.render_profile["pages"],
            inputs.render_artifacts,
            strict=True,
        ),
        start=1,
    ):
        expected_pages.append(
            {
                "page_number": index,
                "locator": dict(declared["locator"]),
                "render": {
                    "path": f"renders/page-{index:04d}.png",
                    "sha256": artifact.pin.sha256,
                    "media_type": "image/png",
                    "width_px": artifact.width_px,
                    "height_px": artifact.height_px,
                },
            }
        )
    if expected.get("pages") != expected_pages:
        _reject("bundle_page_profile_binding_mismatch")
    expected_pipeline = []
    for index, (declared, artifact) in enumerate(
        zip(
            inputs.pipeline_profile["artifacts"],
            inputs.pipeline_artifacts,
            strict=True,
        ),
        start=1,
    ):
        expected_pipeline.append(
            {
                "artifact_number": index,
                "kind": str(declared["kind"]),
                "path": (
                    f"pipeline/artifact-{index:04d}"
                    f"{_artifact_suffix(artifact.media_type)}"
                ),
                "sha256": artifact.pin.sha256,
                "media_type": artifact.media_type,
                "customer_sensitive": True,
            }
        )
    if expected.get("pipeline_artifacts") != expected_pipeline:
        _reject("bundle_pipeline_profile_binding_mismatch")
    return expected


def _load_input_set(
    *,
    source: Path,
    document_format: str,
    expected_source_sha256: str,
    render_profile_path: Path,
    expected_render_profile_sha256: str,
    pipeline_profile_path: Path,
    expected_pipeline_profile_sha256: str,
) -> _InputSet:
    format_token = str(document_format or "").lower()
    if format_token not in SUPPORTED_FORMATS:
        _reject(
            "bundle_pdf_uses_existing_tool"
            if format_token == "pdf"
            else "bundle_document_format_unsupported"
        )
    source_pin, _ = _read_pinned_file(
        source,
        expected_sha256=_sha256(
            expected_source_sha256,
            "bundle_expected_source_sha256_invalid",
        ),
        code="bundle_source",
        retain_payload=False,
    )
    render_pin, render_bytes = _read_pinned_file(
        render_profile_path,
        expected_sha256=_sha256(
            expected_render_profile_sha256,
            "render_profile_expected_sha256_invalid",
        ),
        code="render_profile",
        retain_payload=True,
    )
    pipeline_pin, pipeline_bytes = _read_pinned_file(
        pipeline_profile_path,
        expected_sha256=_sha256(
            expected_pipeline_profile_sha256,
            "pipeline_profile_expected_sha256_invalid",
        ),
        code="pipeline_profile",
        retain_payload=True,
    )
    render_profile = _parse_json(render_bytes, "render_profile_json_invalid")
    pipeline_profile = _parse_json(
        pipeline_bytes,
        "pipeline_profile_json_invalid",
    )
    _validate_render_profile(
        render_profile,
        source_sha256=source_pin.sha256,
        document_format=format_token,
    )
    _validate_pipeline_profile(
        pipeline_profile,
        source_sha256=source_pin.sha256,
        document_format=format_token,
    )
    render_artifacts = tuple(
        _load_declared_artifact(
            render_profile_path.parent,
            page["artifact"],
            code=f"render_page_{index}",
            require_png=True,
        )
        for index, page in enumerate(render_profile["pages"])
    )
    pipeline_artifacts = tuple(
        _load_declared_artifact(
            pipeline_profile_path.parent,
            artifact,
            code=f"pipeline_artifact_{index}",
            require_png=False,
        )
        for index, artifact in enumerate(pipeline_profile["artifacts"])
    )
    physical_paths = [
        artifact.pin.path.resolve(strict=True)
        for artifact in (*render_artifacts, *pipeline_artifacts)
    ]
    if len(physical_paths) != len(set(physical_paths)):
        _reject("bundle_input_artifact_duplicate")
    return _InputSet(
        source=source_pin,
        document_format=format_token,
        render_profile_path=render_pin,
        render_profile=render_profile,
        render_artifacts=render_artifacts,
        pipeline_profile_path=pipeline_pin,
        pipeline_profile=pipeline_profile,
        pipeline_artifacts=pipeline_artifacts,
    )


def _validate_render_profile(
    profile: Any,
    *,
    source_sha256: str,
    document_format: str,
) -> None:
    root = _object(profile, "render_profile_not_object")
    _exact(
        root,
        {
            "schema_version",
            "profile_hash_contract",
            "profile_sha256",
            "source",
            "renderer",
            "page_count",
            "pages",
        },
        "render_profile",
    )
    if root["schema_version"] != RENDER_PROFILE_SCHEMA_VERSION:
        _reject("render_profile_schema_version_mismatch")
    if root["profile_hash_contract"] != PROFILE_HASH_CONTRACT:
        _reject("render_profile_hash_contract_mismatch")
    if _sha256(root["profile_sha256"], "render_profile_sha256_invalid") != (
        canonical_profile_sha256(root)
    ):
        _reject("render_profile_sha256_mismatch")
    source = _object(root["source"], "render_profile_source_invalid")
    _exact(source, {"source_sha256", "format"}, "render_profile_source")
    if (
        source["source_sha256"] != source_sha256
        or source["format"] != document_format
    ):
        _reject("render_profile_source_mismatch")
    renderer = _object(root["renderer"], "render_profile_renderer_invalid")
    _exact(
        renderer,
        {"id", "version", "config_sha256"},
        "render_profile_renderer",
    )
    _safe_id(renderer["id"], "render_profile_renderer_id_invalid")
    _safe_id(renderer["version"], "render_profile_renderer_version_invalid")
    _sha256(
        renderer["config_sha256"],
        "render_profile_renderer_config_sha256_invalid",
    )
    pages = root["pages"]
    page_count = _positive_int(
        root["page_count"],
        "render_profile_page_count_invalid",
    )
    if not isinstance(pages, list) or len(pages) != page_count:
        _reject("render_profile_page_inventory_invalid")
    page_numbers: list[int] = []
    for index, page_value in enumerate(pages):
        page = _object(page_value, f"render_profile_page_{index}_invalid")
        _exact(
            page,
            {"page_number", "locator", "artifact"},
            f"render_profile_page_{index}",
        )
        page_number = _positive_int(
            page["page_number"],
            f"render_profile_page_{index}_number_invalid",
        )
        page_numbers.append(page_number)
        _validate_page_locator(
            page["locator"],
            document_format=document_format,
            page_number=page_number,
            index=index,
        )
        _validate_artifact_declaration(
            page["artifact"],
            code=f"render_profile_page_{index}_artifact",
            require_png=True,
        )
    expected_pages = list(range(1, page_count + 1))
    if page_numbers != expected_pages:
        if len(page_numbers) != len(set(page_numbers)):
            _reject("render_profile_page_duplicate")
        if sorted(page_numbers) == expected_pages:
            _reject("render_profile_page_out_of_order")
        _reject("render_profile_page_coverage_incomplete")


def _validate_pipeline_profile(
    profile: Any,
    *,
    source_sha256: str,
    document_format: str,
) -> None:
    root = _object(profile, "pipeline_profile_not_object")
    _exact(
        root,
        {
            "schema_version",
            "profile_hash_contract",
            "profile_sha256",
            "source",
            "pipeline",
            "artifacts",
        },
        "pipeline_profile",
    )
    if root["schema_version"] != PIPELINE_PROFILE_SCHEMA_VERSION:
        _reject("pipeline_profile_schema_version_mismatch")
    if root["profile_hash_contract"] != PROFILE_HASH_CONTRACT:
        _reject("pipeline_profile_hash_contract_mismatch")
    if _sha256(root["profile_sha256"], "pipeline_profile_sha256_invalid") != (
        canonical_profile_sha256(root)
    ):
        _reject("pipeline_profile_sha256_mismatch")
    source = _object(root["source"], "pipeline_profile_source_invalid")
    _exact(source, {"source_sha256", "format"}, "pipeline_profile_source")
    if (
        source["source_sha256"] != source_sha256
        or source["format"] != document_format
    ):
        _reject("pipeline_profile_source_mismatch")
    pipeline = _object(root["pipeline"], "pipeline_profile_pipeline_invalid")
    _exact(
        pipeline,
        {
            "status",
            *PIPELINE_COMPONENTS,
            "pipeline_fingerprint_sha256",
        },
        "pipeline_profile_pipeline",
    )
    if pipeline["status"] not in PIPELINE_STATUSES:
        _reject("pipeline_profile_status_invalid")
    for component_name in PIPELINE_COMPONENTS:
        component = _object(
            pipeline[component_name],
            f"pipeline_profile_{component_name}_invalid",
        )
        _exact(
            component,
            {
                "id",
                "version",
                "status",
                "snapshot_id",
                "fingerprint_sha256",
            },
            f"pipeline_profile_{component_name}",
        )
        _safe_id(
            component["id"],
            f"pipeline_profile_{component_name}_id_invalid",
        )
        _safe_id(
            component["version"],
            f"pipeline_profile_{component_name}_version_invalid",
        )
        if component["status"] not in PIPELINE_STATUSES:
            _reject(f"pipeline_profile_{component_name}_status_invalid")
        _safe_id(
            component["snapshot_id"],
            f"pipeline_profile_{component_name}_snapshot_id_invalid",
        )
        _sha256(
            component["fingerprint_sha256"],
            f"pipeline_profile_{component_name}_fingerprint_invalid",
        )
    artifacts = root["artifacts"]
    if not isinstance(artifacts, list):
        _reject("pipeline_profile_artifacts_invalid")
    seen_paths: set[str] = set()
    for index, artifact in enumerate(artifacts):
        declaration = _validate_artifact_declaration(
            artifact,
            code=f"pipeline_profile_artifact_{index}",
            require_png=False,
            require_kind=True,
        )
        if declaration["path"] in seen_paths:
            _reject("pipeline_profile_artifact_path_duplicate")
        seen_paths.add(declaration["path"])
        if declaration["customer_sensitive"] is not True:
            _reject("pipeline_profile_artifact_must_be_customer_sensitive")
    provided_fingerprint = _sha256(
        pipeline["pipeline_fingerprint_sha256"],
        "pipeline_profile_fingerprint_invalid",
    )
    if provided_fingerprint != canonical_pipeline_fingerprint(root):
        _reject("pipeline_profile_fingerprint_mismatch")


def _validate_page_locator(
    value: Any,
    *,
    document_format: str,
    page_number: int,
    index: int,
) -> None:
    locator = _object(value, f"render_profile_page_{index}_locator_invalid")
    if document_format == "tif":
        _exact(
            locator,
            {"kind", "frame_index"},
            f"render_profile_page_{index}_locator",
        )
        if (
            locator["kind"] != "TIF_FRAME"
            or isinstance(locator["frame_index"], bool)
            or locator["frame_index"] != page_number - 1
        ):
            _reject(f"render_profile_page_{index}_tif_frame_invalid")
        return
    allowed = {
        "kind",
        "sheet_locator",
        "sheet_index",
        "sheet_name_sha256",
        "sheet_page_number",
    }
    if not set(locator).issubset(allowed) or "kind" not in locator:
        _reject(f"render_profile_page_{index}_locator_fields_invalid")
    if locator["kind"] != "OFFICE_PAGE":
        _reject(f"render_profile_page_{index}_office_locator_invalid")
    has_sheet = "sheet_locator" in locator
    has_sheet_page = "sheet_page_number" in locator
    if has_sheet != has_sheet_page:
        _reject(f"render_profile_page_{index}_sheet_locator_incomplete")
    if has_sheet:
        _safe_id(
            locator["sheet_locator"],
            f"render_profile_page_{index}_sheet_locator_invalid",
        )
        _positive_int(
            locator["sheet_page_number"],
            f"render_profile_page_{index}_sheet_page_invalid",
        )
    has_sheet_index = "sheet_index" in locator
    has_sheet_name_sha256 = "sheet_name_sha256" in locator
    if has_sheet_index != has_sheet_name_sha256:
        _reject(f"render_profile_page_{index}_sheet_identity_incomplete")
    if has_sheet_index:
        if not has_sheet or document_format not in {"xlsx", "xlsb"}:
            _reject(f"render_profile_page_{index}_sheet_identity_invalid")
        sheet_index = locator["sheet_index"]
        if (
            isinstance(sheet_index, bool)
            or not isinstance(sheet_index, int)
            or sheet_index < 0
        ):
            _reject(f"render_profile_page_{index}_sheet_index_invalid")
        sheet_name_sha256 = _sha256(
            locator["sheet_name_sha256"],
            f"render_profile_page_{index}_sheet_name_sha256_invalid",
        )
        expected_sheet_locator = (
            f"sheet-{sheet_index:04d}-{sheet_name_sha256}"
        )
        if locator["sheet_locator"] != expected_sheet_locator:
            _reject(f"render_profile_page_{index}_sheet_identity_mismatch")


def _validate_artifact_declaration(
    value: Any,
    *,
    code: str,
    require_png: bool,
    require_kind: bool = False,
) -> dict[str, Any]:
    artifact = _object(value, f"{code}_invalid")
    expected_fields = {
        "path",
        "sha256",
        "media_type",
        *(
            {"width_px", "height_px"}
            if require_png
            else {"kind", "customer_sensitive"}
            if require_kind
            else set()
        ),
    }
    _exact(artifact, expected_fields, code)
    relative_path = _safe_relative_path(
        artifact["path"],
        f"{code}_path_invalid",
    )
    digest = _sha256(artifact["sha256"], f"{code}_sha256_invalid")
    media_type = _media_type(
        artifact["media_type"],
        f"{code}_media_type_invalid",
    )
    if require_png and media_type != "image/png":
        _reject(f"{code}_must_be_png")
    validated = dict(artifact)
    validated.update(
        {"path": relative_path, "sha256": digest, "media_type": media_type}
    )
    if require_png:
        validated["width_px"] = _positive_int(
            artifact["width_px"],
            f"{code}_width_invalid",
        )
        validated["height_px"] = _positive_int(
            artifact["height_px"],
            f"{code}_height_invalid",
        )
    elif require_kind:
        validated["kind"] = _safe_id(
            artifact["kind"],
            f"{code}_kind_invalid",
        )
    return validated


def _load_declared_artifact(
    profile_root: Path,
    declaration_value: Any,
    *,
    code: str,
    require_png: bool,
) -> _LoadedArtifact:
    declaration = _validate_artifact_declaration(
        declaration_value,
        code=code,
        require_png=require_png,
        require_kind=not require_png,
    )
    path = profile_root.joinpath(
        *PurePosixPath(declaration["path"]).parts
    )
    pin, payload = _read_pinned_file(
        path,
        expected_sha256=declaration["sha256"],
        code=code,
        retain_payload=True,
        containment_root=profile_root,
    )
    width: int | None = None
    height: int | None = None
    if require_png:
        try:
            with Image.open(BytesIO(payload)) as image:
                image.load()
                if image.format != "PNG":
                    _reject(f"{code}_must_be_png")
                width, height = image.size
        except DocumentVisualReviewBundleRejected:
            raise
        except Exception as exc:
            raise DocumentVisualReviewBundleRejected(
                f"{code}_png_invalid"
            ) from exc
        if (
            width != declaration["width_px"]
            or height != declaration["height_px"]
        ):
            _reject(f"{code}_dimensions_mismatch")
    return _LoadedArtifact(
        pin=pin,
        payload=payload,
        media_type=declaration["media_type"],
        width_px=width,
        height_px=height,
    )


def _verify_input_set_unchanged(inputs: _InputSet) -> None:
    pins = (
        inputs.source,
        inputs.render_profile_path,
        inputs.pipeline_profile_path,
        *(artifact.pin for artifact in inputs.render_artifacts),
        *(artifact.pin for artifact in inputs.pipeline_artifacts),
    )
    for pin in pins:
        current, _ = _read_pinned_file(
            pin.path,
            expected_sha256=pin.sha256,
            code="bundle_input_drift",
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
            _reject("bundle_input_toctou_detected")


def _verify_bundle_tree(
    root: Path,
    *,
    expected: Mapping[str, Any],
) -> None:
    manifest = _load_manifest(root)
    required_fields = {
        "schema_version",
        "generated_at",
        "bundle_kind",
        "bundle_hash_contract",
        "document",
        "profile_pins",
        "renderer",
        "pipeline",
        "coverage",
        "pages",
        "pipeline_artifacts",
        "entrypoint",
        "artifact_inventory",
        "artifact_inventory_sha256",
        "privacy",
        "review_provenance",
        "bundle_sha256",
    }
    if set(manifest) != required_fields:
        _reject("bundle_manifest_fields_invalid")
    if (
        manifest["schema_version"] != BUNDLE_SCHEMA_VERSION
        or manifest["bundle_kind"] != "WHOLE_DOCUMENT_VISUAL_REVIEW_INPUT"
        or manifest["bundle_hash_contract"] != BUNDLE_HASH_CONTRACT
        or not isinstance(manifest["generated_at"], str)
        or not manifest["generated_at"].strip()
    ):
        _reject("bundle_manifest_contract_invalid")
    if manifest["privacy"] != _PRIVACY:
        _reject("bundle_privacy_invalid")
    if manifest["review_provenance"] != _PROVENANCE:
        _reject("bundle_review_provenance_invalid")
    if _sha256(manifest["bundle_sha256"], "bundle_sha256_invalid") != (
        canonical_bundle_sha256(manifest)
    ):
        _reject("bundle_sha256_mismatch")
    if manifest["artifact_inventory_sha256"] != (
        canonical_artifact_inventory_sha256(manifest["artifact_inventory"])
    ):
        _reject("bundle_artifact_inventory_sha256_mismatch")

    for field in (
        "document",
        "profile_pins",
        "renderer",
        "pipeline",
        "coverage",
        "pages",
        "pipeline_artifacts",
        "entrypoint",
        "artifact_inventory",
        "artifact_inventory_sha256",
        "privacy",
        "review_provenance",
        "bundle_sha256",
    ):
        if manifest[field] != expected[field]:
            _reject(f"bundle_{field}_binding_mismatch")

    inventory = manifest["artifact_inventory"]
    if not isinstance(inventory, list) or not inventory:
        _reject("bundle_artifact_inventory_invalid")
    paths: list[str] = []
    for index, record_value in enumerate(inventory):
        record = _object(
            record_value,
            f"bundle_inventory_{index}_invalid",
        )
        path_token = _safe_relative_path(
            record.get("path"),
            f"bundle_inventory_{index}_path_invalid",
        )
        paths.append(path_token)
        digest = _sha256(
            record.get("sha256"),
            f"bundle_inventory_{index}_sha256_invalid",
        )
        artifact_path = root.joinpath(*PurePosixPath(path_token).parts)
        pin, payload = _read_pinned_file(
            artifact_path,
            expected_sha256=digest,
            code=f"bundle_inventory_{index}",
            retain_payload=True,
            containment_root=root,
        )
        del pin
        if record.get("media_type") == "image/png":
            try:
                with Image.open(BytesIO(payload)) as image:
                    image.load()
                    observed_size = image.size
            except Exception as exc:
                raise DocumentVisualReviewBundleRejected(
                    f"bundle_inventory_{index}_png_invalid"
                ) from exc
            if "width_px" in record and observed_size != (
                record["width_px"],
                record["height_px"],
            ):
                _reject(f"bundle_inventory_{index}_dimensions_mismatch")
    if len(paths) != len(set(paths)):
        _reject("bundle_artifact_path_duplicate")

    expected_files = {"manifest.json", *paths}
    actual_files: set[str] = set()
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root).as_posix()
        metadata = candidate.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            _reject("bundle_symlink_forbidden")
        if stat.S_ISREG(metadata.st_mode):
            actual_files.add(relative)
        elif not stat.S_ISDIR(metadata.st_mode):
            _reject("bundle_non_regular_artifact")
    if actual_files != expected_files:
        _reject("bundle_file_inventory_mismatch")
    index_path = root / "index.html"
    try:
        index_text = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DocumentVisualReviewBundleRejected(
            "bundle_index_invalid"
        ) from exc
    if _EXTERNAL_URL.search(index_text):
        _reject("bundle_index_external_url")


def _load_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    _, payload = _read_pinned_file(
        manifest_path,
        expected_sha256=None,
        code="bundle_manifest",
        retain_payload=True,
        containment_root=root,
    )
    return _parse_json(payload, "bundle_manifest_json_invalid")


def _offline_index(
    *,
    document_id: str,
    source_sha256: str,
    document_format: str,
    pages: list[dict[str, Any]],
    pipeline_artifacts: list[dict[str, Any]],
) -> bytes:
    page_sections = "\n".join(
        (
            f'<section id="page-{page["page_number"]}">'
            f'<h2>Page {page["page_number"]}</h2>'
            f'<img src="{html.escape(page["render"]["path"], quote=True)}" '
            f'alt="Full page {page["page_number"]} render"></section>'
        )
        for page in pages
    )
    pipeline_links = "\n".join(
        (
            f'<li><a href="{html.escape(record["path"], quote=True)}">'
            f'Pipeline artifact {record["artifact_number"]}</a></li>'
        )
        for record in pipeline_artifacts
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Whole-document visual review input</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;background:#eef2f7;color:#172033}}
header,main{{max-width:1400px;margin:auto;padding:20px}}
header{{background:#172033;color:white}}section{{background:white;padding:16px;margin:18px 0}}
img{{display:block;max-width:100%;height:auto;border:1px solid #94a3b8}}
</style></head><body><header>
<h1>Whole-document visual review input</h1>
<p>CUSTOMER SENSITIVE — CONTROLLED LOCAL REVIEW ONLY.</p>
<p>Document ID: {html.escape(document_id)} | Format: {html.escape(document_format)}
| Source SHA-256: {html.escape(source_sha256)}</p></header>
<main>{page_sections}<h2>Pipeline payloads</h2><ul>{pipeline_links}</ul></main>
</body></html>"""
    if _EXTERNAL_URL.search(document):
        _reject("bundle_index_external_url")
    return document.encode("utf-8")


def _read_pinned_file(
    path: Path,
    *,
    expected_sha256: str | None,
    code: str,
    retain_payload: bool,
    containment_root: Path | None = None,
) -> tuple[_PinnedFile, bytes]:
    candidate = Path(path)
    if containment_root is not None:
        root = _regular_directory(
            containment_root,
            f"{code}_root_unavailable",
        )
        try:
            candidate.absolute().relative_to(root.absolute())
        except ValueError:
            _reject(f"{code}_path_escape")
        _reject_symlink_chain_between(root, candidate, f"{code}_symlink")
    else:
        _reject_symlink_chain(candidate, f"{code}_symlink")
    try:
        metadata_before = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualReviewBundleRejected(
            f"{code}_unavailable"
        ) from exc
    if not stat.S_ISREG(metadata_before.st_mode):
        _reject(f"{code}_not_regular")
    try:
        payload = candidate.read_bytes()
        metadata_after = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualReviewBundleRejected(
            f"{code}_unavailable"
        ) from exc
    identity_before = (
        metadata_before.st_dev,
        metadata_before.st_ino,
        metadata_before.st_size,
        metadata_before.st_mtime_ns,
    )
    identity_after = (
        metadata_after.st_dev,
        metadata_after.st_ino,
        metadata_after.st_size,
        metadata_after.st_mtime_ns,
    )
    if identity_before != identity_after:
        _reject(f"{code}_toctou_detected")
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        _reject(f"{code}_hash_mismatch")
    pin = _PinnedFile(
        path=candidate,
        sha256=digest,
        size=metadata_after.st_size,
        device=metadata_after.st_dev,
        inode=metadata_after.st_ino,
        mtime_ns=metadata_after.st_mtime_ns,
    )
    return pin, payload if retain_payload else b""


def _controlled_destination(path: Path, *, corpus_root: Path) -> Path:
    corpus = _regular_directory(corpus_root, "bundle_corpus_root_invalid")
    destination = path.absolute()
    existing_parent = destination.parent
    suffix: list[str] = [destination.name]
    while not existing_parent.exists():
        suffix.append(existing_parent.name)
        existing_parent = existing_parent.parent
    _reject_symlink_chain(existing_parent, "bundle_output_parent_symlink")
    resolved_parent = existing_parent.resolve(strict=True)
    resolved_destination = resolved_parent.joinpath(*reversed(suffix))
    resolved_corpus = corpus.resolve(strict=True)
    if (
        resolved_destination == resolved_corpus
        or resolved_corpus in resolved_destination.parents
    ):
        _reject("bundle_output_inside_corpus")
    return destination


def _regular_directory(path: Path, code: str) -> Path:
    _reject_symlink_chain(path, f"{code}_symlink")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualReviewBundleRejected(code) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        _reject(code)
    return path


def _reject_symlink_chain(path: Path, code: str) -> None:
    candidate = path.absolute()
    parts = candidate.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            continue
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise DocumentVisualReviewBundleRejected(code) from exc
        if stat.S_ISLNK(metadata.st_mode):
            _reject(code)


def _reject_symlink_chain_between(root: Path, path: Path, code: str) -> None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        _reject(f"{code}_escape")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            _reject(code)


def _parse_json(payload: bytes, code: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _reject(code)
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentVisualReviewBundleRejected(code) from exc
    return _object(value, code)


def _object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _reject(code)
    return value


def _exact(value: Mapping[str, Any], fields: set[str], prefix: str) -> None:
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        _reject(f"{prefix}_unknown_field:{unknown[0]}")
    if missing:
        _reject(f"{prefix}_missing_field:{missing[0]}")


def _sha256(value: Any, code: str) -> str:
    token = str(value or "")
    if _SHA256.fullmatch(token) is None:
        _reject(code)
    return token


def _safe_id(value: Any, code: str) -> str:
    token = str(value or "")
    if _SAFE_ID.fullmatch(token) is None or ".." in token:
        _reject(code)
    return token


def _document_id(value: Any, *, source: Path) -> str:
    token = str(value or "")
    if _DOCUMENT_ID.fullmatch(token) is None or ".." in token:
        _reject("bundle_document_id_invalid")
    source_name = source.name
    if source_name and source_name.casefold() in token.casefold():
        _reject("bundle_document_id_leaks_source_filename")
    return token


def _media_type(value: Any, code: str) -> str:
    token = str(value or "").lower()
    if _MEDIA_TYPE.fullmatch(token) is None:
        _reject(code)
    return token


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _reject(code)
    return value


def _safe_relative_path(value: Any, code: str) -> str:
    token = str(value or "")
    path = PurePosixPath(token)
    if (
        not token
        or path.is_absolute()
        or "\\" in token
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _reject(code)
    return token


def _artifact_suffix(media_type: str) -> str:
    return {
        "application/json": ".json",
        "image/png": ".png",
        "text/plain": ".txt",
    }.get(media_type, ".bin")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    except OSError as exc:
        raise DocumentVisualReviewBundleRejected(
            "bundle_staging_write_failed"
        ) from exc


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n",
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise DocumentVisualReviewBundleRejected(
            "bundle_directory_fsync_failed"
        ) from exc


def _reject(code: str) -> None:
    raise DocumentVisualReviewBundleRejected(code)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a pinned non-PDF whole-document visual-review bundle."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--format", required=True, choices=sorted(SUPPORTED_FORMATS))
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--render-profile", required=True, type=Path)
    parser.add_argument("--expected-render-profile-sha256", required=True)
    parser.add_argument("--pipeline-profile", required=True, type=Path)
    parser.add_argument("--expected-pipeline-profile-sha256", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--corpus-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = export_document_visual_review_bundle(
            args.source,
            document_id=args.document_id,
            document_format=args.format,
            expected_source_sha256=args.expected_source_sha256,
            render_profile_path=args.render_profile,
            expected_render_profile_sha256=(
                args.expected_render_profile_sha256
            ),
            pipeline_profile_path=args.pipeline_profile,
            expected_pipeline_profile_sha256=(
                args.expected_pipeline_profile_sha256
            ),
            output_dir=args.output_dir,
            corpus_root=args.corpus_root,
        )
    except DocumentVisualReviewBundleRejected as exc:
        print(exc.code)
        return 2
    print(manifest["bundle_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
