"""Render a source-bound TIF frame inventory into pinned PNG artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import PIL
from PIL import Image, ImageOps

from app.adapters.documents.preflight import LocalDocumentPreflight
from app.domain.extraction.preflight import DocumentFormat
from scripts.document_visual_review_bundle import (
    PROFILE_HASH_CONTRACT,
    RENDER_PROFILE_SCHEMA_VERSION,
    _validate_render_profile,
    canonical_json_bytes,
    canonical_profile_sha256,
)
from scripts.storage_backup import BackupError, _rename_no_replace


TIF_RENDERER_ID = "pillow-tif-frame-renderer"
TIF_RENDERER_VERSION = PIL.__version__
_RENDER_CONFIG = {
    "frame_order": "native-zero-based",
    "orientation": "pillow-exif-transpose",
    "png_metadata": "stripped",
    "supported_mode_policy": "preserve-png-mode-else-rgba",
    "png_compress_level": 6,
}
TIF_RENDERER_CONFIG_SHA256 = hashlib.sha256(
    canonical_json_bytes(_RENDER_CONFIG)
).hexdigest()
_PNG_NATIVE_MODES = frozenset(
    {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16", "I;16B", "I;16L"}
)


class DocumentVisualTifRenderRejected(RuntimeError):
    """Stable, customer-text-free TIF render rejection."""

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


def export_tif_render_profile(
    source: Path,
    *,
    expected_source_sha256: str,
    output_dir: Path,
    corpus_root: Path,
) -> dict[str, Any]:
    """Atomically render every validated TIF frame and publish its v1 profile."""

    source_path = Path(source)
    if source_path.suffix.lower() not in {".tif", ".tiff"}:
        _fail("tif_render_source_format_mismatch")
    source_pin = _pin_file(
        source_path,
        expected_sha256=_sha256(
            expected_source_sha256,
            "tif_render_expected_source_sha256_invalid",
        ),
        code="tif_render_source",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        report = LocalDocumentPreflight().inspect(source_path)
    if (
        report.source_sha256 != source_pin.sha256
        or report.source_size != source_pin.size
    ):
        _fail("tif_render_preflight_source_mismatch")
    if (
        report.identity.detected_format is not DocumentFormat.TIF
        or not report.attachment_acceptable
        or not report.page_count
    ):
        _fail("tif_render_preflight_rejected")

    destination = Path(output_dir).absolute()
    parent = _controlled_destination_parent(
        destination,
        corpus_root=Path(corpus_root),
    )
    if destination.exists() or destination.is_symlink():
        _fail("tif_render_output_exists")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=parent,
        )
    )
    staging.chmod(0o700)
    try:
        pages = _render_frames(
            source_path,
            staging=staging,
            expected_frame_count=report.page_count,
        )
        profile: dict[str, Any] = {
            "schema_version": RENDER_PROFILE_SCHEMA_VERSION,
            "profile_hash_contract": PROFILE_HASH_CONTRACT,
            "source": {
                "source_sha256": source_pin.sha256,
                "format": "tif",
            },
            "renderer": {
                "id": TIF_RENDERER_ID,
                "version": TIF_RENDERER_VERSION,
                "config_sha256": TIF_RENDERER_CONFIG_SHA256,
            },
            "page_count": len(pages),
            "pages": pages,
        }
        profile["profile_sha256"] = canonical_profile_sha256(profile)
        _validate_profile(profile, source_sha256=source_pin.sha256)
        profile_payload = (
            json.dumps(
                profile,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        _write_bytes(staging / "render-profile.json", profile_payload)
        _verify_source_unchanged(source_pin)
        profile_file_sha256 = hashlib.sha256(profile_payload).hexdigest()
        _verify_render_tree(
            staging,
            profile=profile,
            expected_profile_file_sha256=profile_file_sha256,
        )
        _fsync_directory(staging)
        if destination.exists() or destination.is_symlink():
            _fail("tif_render_output_exists")
        try:
            _rename_no_replace(staging, destination)
        except BackupError as exc:
            if destination.exists() or destination.is_symlink():
                raise DocumentVisualTifRenderRejected(
                    "tif_render_output_exists"
                ) from exc
            raise DocumentVisualTifRenderRejected(
                "tif_render_atomic_publish_failed"
            ) from exc
        _fsync_directory(parent)
        return {
            "profile": profile,
            "profile_file_sha256": profile_file_sha256,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def verify_tif_render_profile(
    profile_dir: Path,
    *,
    source: Path,
    expected_source_sha256: str,
    expected_profile_file_sha256: str,
    corpus_root: Path,
) -> dict[str, Any]:
    """Recompute source, profile, complete frame inventory, PNG hashes and sizes."""

    source_pin = _pin_file(
        Path(source),
        expected_sha256=_sha256(
            expected_source_sha256,
            "tif_render_expected_source_sha256_invalid",
        ),
        code="tif_render_source",
    )
    root = _controlled_existing_directory(
        Path(profile_dir),
        corpus_root=Path(corpus_root),
    )
    profile_path = root / "render-profile.json"
    profile_pin = _pin_file(
        profile_path,
        expected_sha256=_sha256(
            expected_profile_file_sha256,
            "tif_render_expected_profile_sha256_invalid",
        ),
        code="tif_render_profile",
        containment_root=root,
    )
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DocumentVisualTifRenderRejected(
            "tif_render_profile_json_invalid"
        ) from exc
    _validate_profile(profile, source_sha256=source_pin.sha256)
    _verify_render_tree(
        root,
        profile=profile,
        expected_profile_file_sha256=profile_pin.sha256,
    )
    _verify_source_unchanged(source_pin)
    return profile


def _render_frames(
    source: Path,
    *,
    staging: Path,
    expected_frame_count: int,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                frame_count = int(getattr(image, "n_frames", 1))
                if frame_count != expected_frame_count:
                    _fail("tif_render_frame_inventory_mismatch")
                for frame_index in range(frame_count):
                    image.seek(frame_index)
                    frame = ImageOps.exif_transpose(image.copy())
                    if frame.mode not in _PNG_NATIVE_MODES:
                        frame = frame.convert("RGBA")
                    stream = BytesIO()
                    frame.save(
                        stream,
                        format="PNG",
                        compress_level=_RENDER_CONFIG["png_compress_level"],
                    )
                    payload = stream.getvalue()
                    page_number = frame_index + 1
                    relative_path = f"page-{page_number:04d}.png"
                    _write_bytes(staging / relative_path, payload)
                    pages.append(
                        {
                            "page_number": page_number,
                            "locator": {
                                "kind": "TIF_FRAME",
                                "frame_index": frame_index,
                            },
                            "artifact": {
                                "path": relative_path,
                                "sha256": hashlib.sha256(payload).hexdigest(),
                                "media_type": "image/png",
                                "width_px": frame.width,
                                "height_px": frame.height,
                            },
                        }
                    )
    except DocumentVisualTifRenderRejected:
        raise
    except Exception as exc:
        raise DocumentVisualTifRenderRejected(
            "tif_render_decode_failed"
        ) from exc
    if not pages:
        _fail("tif_render_frame_inventory_empty")
    return pages


def _validate_profile(profile: Any, *, source_sha256: str) -> None:
    try:
        _validate_render_profile(
            profile,
            source_sha256=source_sha256,
            document_format="tif",
        )
    except Exception as exc:
        code = getattr(exc, "code", "tif_render_profile_contract_invalid")
        raise DocumentVisualTifRenderRejected(str(code)) from exc
    renderer = profile["renderer"]
    if renderer != {
        "id": TIF_RENDERER_ID,
        "version": TIF_RENDERER_VERSION,
        "config_sha256": TIF_RENDERER_CONFIG_SHA256,
    }:
        _fail("tif_render_renderer_identity_mismatch")


def _verify_render_tree(
    root: Path,
    *,
    profile: Mapping[str, Any],
    expected_profile_file_sha256: str,
) -> None:
    profile_path = root / "render-profile.json"
    _pin_file(
        profile_path,
        expected_sha256=expected_profile_file_sha256,
        code="tif_render_profile",
        containment_root=root,
    )
    expected_files = {"render-profile.json"}
    for index, page in enumerate(profile["pages"]):
        artifact = page["artifact"]
        path = root.joinpath(*PurePosixPath(artifact["path"]).parts)
        pin = _pin_file(
            path,
            expected_sha256=artifact["sha256"],
            code=f"tif_render_page_{index}",
            containment_root=root,
        )
        expected_files.add(artifact["path"])
        try:
            with Image.open(path) as image:
                image.load()
                if image.format != "PNG" or image.size != (
                    artifact["width_px"],
                    artifact["height_px"],
                ):
                    _fail(f"tif_render_page_{index}_dimensions_mismatch")
        except DocumentVisualTifRenderRejected:
            raise
        except Exception as exc:
            raise DocumentVisualTifRenderRejected(
                f"tif_render_page_{index}_png_invalid"
            ) from exc
        del pin
    actual_files: set[str] = set()
    for candidate in root.rglob("*"):
        metadata = candidate.stat(follow_symlinks=False)
        relative = candidate.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            _fail("tif_render_output_symlink")
        if stat.S_ISREG(metadata.st_mode):
            actual_files.add(relative)
        elif not stat.S_ISDIR(metadata.st_mode):
            _fail("tif_render_output_non_regular")
    if actual_files != expected_files:
        _fail("tif_render_output_inventory_mismatch")


def _pin_file(
    path: Path,
    *,
    expected_sha256: str,
    code: str,
    containment_root: Path | None = None,
) -> _Pin:
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
        payload = candidate.read_bytes()
        after = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualTifRenderRejected(
            f"{code}_unavailable"
        ) from exc
    if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(after.st_mode):
        _fail(f"{code}_not_regular")
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
    return _Pin(
        path=candidate,
        sha256=digest,
        size=after.st_size,
        device=after.st_dev,
        inode=after.st_ino,
        mtime_ns=after.st_mtime_ns,
    )


def _verify_source_unchanged(pin: _Pin) -> None:
    current = _pin_file(
        pin.path,
        expected_sha256=pin.sha256,
        code="tif_render_source_drift",
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
        _fail("tif_render_source_toctou_detected")


def _controlled_destination_parent(
    destination: Path,
    *,
    corpus_root: Path,
) -> Path:
    if not destination.name or destination.name in {".", ".."}:
        _fail("tif_render_output_name_invalid")
    parent = destination.parent
    _validate_external_directory(parent, corpus_root=corpus_root)
    return parent


def _controlled_existing_directory(
    root: Path,
    *,
    corpus_root: Path,
) -> Path:
    candidate = Path(root).absolute()
    _validate_external_directory(candidate, corpus_root=corpus_root)
    return candidate


def _validate_external_directory(path: Path, *, corpus_root: Path) -> None:
    candidate = Path(path).absolute()
    corpus = Path(corpus_root)
    _reject_symlink_chain(candidate, "tif_render_output_parent_symlink")
    _reject_symlink_chain(corpus, "tif_render_corpus_root_symlink")
    try:
        candidate_metadata = candidate.stat(follow_symlinks=False)
        corpus_metadata = corpus.stat(follow_symlinks=False)
    except OSError as exc:
        raise DocumentVisualTifRenderRejected(
            "tif_render_controlled_root_unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(candidate_metadata.st_mode)
        or not stat.S_ISDIR(corpus_metadata.st_mode)
    ):
        _fail("tif_render_controlled_root_invalid")
    resolved_candidate = candidate.resolve(strict=True)
    resolved_corpus = corpus.resolve(strict=True)
    if (
        resolved_candidate == resolved_corpus
        or resolved_corpus in resolved_candidate.parents
    ):
        _fail("tif_render_output_inside_corpus")


def _write_bytes(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    except OSError as exc:
        raise DocumentVisualTifRenderRejected(
            "tif_render_write_failed"
        ) from exc


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
            raise DocumentVisualTifRenderRejected(code) from exc
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
        raise DocumentVisualTifRenderRejected(
            "tif_render_directory_fsync_failed"
        ) from exc


def _sha256(value: Any, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(code)
    return value


def _fail(code: str) -> None:
    raise DocumentVisualTifRenderRejected(code)


__all__ = [
    "DocumentVisualTifRenderRejected",
    "TIF_RENDERER_CONFIG_SHA256",
    "TIF_RENDERER_ID",
    "TIF_RENDERER_VERSION",
    "export_tif_render_profile",
    "verify_tif_render_profile",
]
