"""Export SpreadsheetVisualProfile as a generic visual-review render profile."""

from __future__ import annotations

# The repository-local app path must be installed before application imports.
# ruff: noqa: E402

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.document_visual_review_bundle import (
    PROFILE_HASH_CONTRACT,
    RENDER_PROFILE_SCHEMA_VERSION,
    _validate_render_profile,
    canonical_profile_sha256,
)
from scripts.storage_backup import BackupError, _rename_no_replace

from app.adapters.documents.spreadsheet_visual_profile import (
    SpreadsheetVisualProfile,
)


class SpreadsheetVisualRenderExportRejected(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SpreadsheetVisualRenderExport:
    root: Path
    profile_path: Path
    profile: dict[str, Any]
    profile_file_sha256: str


def export_spreadsheet_visual_render_profile(
    profile: SpreadsheetVisualProfile,
    *,
    output_dir: Path,
) -> SpreadsheetVisualRenderExport:
    if not isinstance(profile, SpreadsheetVisualProfile):
        raise TypeError("profile must be a SpreadsheetVisualProfile")
    if profile.visual_fidelity_status != "complete":
        raise SpreadsheetVisualRenderExportRejected(
            "source-bound spreadsheet render is incomplete"
        )
    destination = Path(output_dir).expanduser()
    destination_parent = destination.parent.resolve(strict=True)
    destination = destination_parent / destination.name
    if destination.exists() or destination.is_symlink():
        raise SpreadsheetVisualRenderExportRejected(
            "render profile output already exists"
        )

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination_parent,
        )
    )
    staging.chmod(0o700)
    try:
        page_directory = staging / "pages"
        page_directory.mkdir(mode=0o700)
        page_records: list[dict[str, Any]] = []
        for page in profile.pages:
            sheet = profile.sheets[page.sheet_index]
            if sheet.page_start is None:
                raise SpreadsheetVisualRenderExportRejected(
                    "rendered page has no sheet-page binding"
                )
            sheet_page_number = page.page_number - sheet.page_start + 1
            relative_path = f"pages/page-{page.page_number:04d}.png"
            artifact_path = staging / relative_path
            _write_new_file(artifact_path, page.png_bytes)
            sheet_locator = (
                f"sheet-{page.sheet_index:04d}-{page.sheet_name_sha256}"
            )
            page_records.append(
                {
                    "page_number": page.page_number,
                    "locator": {
                        "kind": "OFFICE_PAGE",
                        "sheet_locator": sheet_locator,
                        "sheet_index": page.sheet_index,
                        "sheet_name_sha256": page.sheet_name_sha256,
                        "sheet_page_number": sheet_page_number,
                    },
                    "artifact": {
                        "path": relative_path,
                        "sha256": page.png_sha256,
                        "media_type": "image/png",
                        "width_px": page.width_pixels,
                        "height_px": page.height_pixels,
                    },
                }
            )
        generic_profile: dict[str, Any] = {
            "schema_version": RENDER_PROFILE_SCHEMA_VERSION,
            "profile_hash_contract": PROFILE_HASH_CONTRACT,
            "source": {
                "source_sha256": profile.source_sha256,
                "format": profile.source_format.value,
            },
            "renderer": {
                "id": profile.renderer_id,
                "version": profile.renderer_version,
                "config_sha256": profile.config.sha256,
            },
            "page_count": len(page_records),
            "pages": page_records,
        }
        generic_profile["profile_sha256"] = canonical_profile_sha256(
            generic_profile
        )
        _validate_render_profile(
            generic_profile,
            source_sha256=profile.source_sha256,
            document_format=profile.source_format.value,
        )
        profile_path = staging / "render-profile.json"
        profile_payload = (
            json.dumps(
                generic_profile,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        _write_new_file(profile_path, profile_payload)
        _fsync_directory(page_directory)
        _fsync_directory(staging)
        try:
            _rename_no_replace(staging, destination)
        except BackupError as exc:
            raise SpreadsheetVisualRenderExportRejected(
                "render profile atomic publish failed"
            ) from exc
        _fsync_directory(destination_parent)
        published_profile_path = destination / "render-profile.json"
        return SpreadsheetVisualRenderExport(
            root=destination,
            profile_path=published_profile_path,
            profile=generic_profile,
            profile_file_sha256=_sha256_file(published_profile_path),
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _write_new_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "SpreadsheetVisualRenderExport",
    "SpreadsheetVisualRenderExportRejected",
    "export_spreadsheet_visual_render_profile",
]
