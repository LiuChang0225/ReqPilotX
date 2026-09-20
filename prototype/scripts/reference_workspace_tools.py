#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

import pymupdf


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WORKSPACE_LOCK_SCHEMAS = {
    "reqpilotx-reference-workspace-lock/v2",
    "reqpilotx-product-evidence-workspace-lock/v2",
}
_DETAIL_SCALE = {"thumbnail": 0.5, "readable": 2.0, "original": 3.0}
_MAX_PAGE_PIXELS = 16_000_000
_MAX_REGION_PIXELS = 4_000_000


class WorkspaceToolError(RuntimeError):
    pass


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_workspace(
    root: Path,
    *,
    expected_source_sha256: str,
    expected_bundle_sha256: str,
) -> dict[str, object]:
    root = _workspace_root(root)
    if not _SHA256.fullmatch(expected_source_sha256) or not _SHA256.fullmatch(
        expected_bundle_sha256
    ):
        raise WorkspaceToolError("workspace_expected_identity_invalid")
    lock = _read_json(root / "workspace.lock.json")
    if lock.get("schema_version") not in _WORKSPACE_LOCK_SCHEMAS:
        raise WorkspaceToolError("workspace_lock_schema_invalid")
    if lock.get("source_sha256") != expected_source_sha256:
        raise WorkspaceToolError("workspace_expected_source_mismatch")
    if lock.get("bundle_sha256") != expected_bundle_sha256:
        raise WorkspaceToolError("workspace_expected_bundle_mismatch")
    files = lock.get("files")
    if not isinstance(files, list) or not files:
        raise WorkspaceToolError("workspace_lock_files_invalid")
    expected_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes"}:
            raise WorkspaceToolError("workspace_lock_entry_invalid")
        relative = _relative_path(str(item["path"]))
        path = _safe_workspace_file(root, relative)
        if not path.is_file():
            raise WorkspaceToolError("workspace_locked_file_missing")
        payload = path.read_bytes()
        if (
            hashlib.sha256(payload).hexdigest() != item["sha256"]
            or len(payload) != item["size_bytes"]
        ):
            raise WorkspaceToolError("workspace_locked_file_drift")
        expected_paths.add(relative)
    for mutable_root in ("artifacts", "scratch", "trace"):
        _safe_workspace_directory(root, mutable_root)
    observed: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise WorkspaceToolError("workspace_symlink_forbidden")
        if (
            path.is_file()
            and path.name != "workspace.lock.json"
            and path.parts[len(root.parts)] not in {"artifacts", "scratch", "trace"}
        ):
            observed.add(path.relative_to(root).as_posix())
    if observed != expected_paths:
        raise WorkspaceToolError("workspace_locked_inventory_drift")
    expected_bundle = canonical_sha256(files)
    if lock.get("bundle_sha256") != expected_bundle:
        raise WorkspaceToolError("workspace_bundle_sha_mismatch")
    manifest = _read_json(_safe_workspace_file(root, "manifest.json"))
    document = manifest.get("document")
    if (
        not isinstance(document, dict)
        or document.get("source_sha256") != expected_source_sha256
    ):
        raise WorkspaceToolError("workspace_manifest_source_mismatch")
    return {
        "schema_version": "reqpilotx-reference-workspace-validation/v1",
        "status": "PASS",
        "bundle_sha256": expected_bundle,
        "file_count": len(files),
    }


def render_page(
    root: Path,
    *,
    expected_source_sha256: str,
    expected_bundle_sha256: str,
    page_number: int,
    detail: str,
) -> dict[str, object]:
    validate_workspace(
        root,
        expected_source_sha256=expected_source_sha256,
        expected_bundle_sha256=expected_bundle_sha256,
    )
    root, source_payload, source_sha256 = _source(
        root,
        expected_source_sha256=expected_source_sha256,
    )
    if detail not in _DETAIL_SCALE:
        raise WorkspaceToolError("render_detail_invalid")
    scale = _DETAIL_SCALE[detail]
    document = pymupdf.open(stream=source_payload, filetype="pdf")
    try:
        page = _page(document, page_number)
        rect = page.rect
        if rect.width * scale * rect.height * scale > _MAX_PAGE_PIXELS:
            raise WorkspaceToolError("render_page_budget_exceeded")
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            alpha=False,
            annots=False,
        )
        payload = bytes(pixmap.tobytes("png"))
        bbox = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
    finally:
        document.close()
    return _write_render(
        root,
        source_sha256=source_sha256,
        page_number=page_number,
        kind="page",
        detail=detail,
        bbox=bbox,
        scale=scale,
        payload=payload,
    )


def render_region(
    root: Path,
    *,
    expected_source_sha256: str,
    expected_bundle_sha256: str,
    page_number: int,
    bbox: tuple[float, float, float, float],
    scale: float,
) -> dict[str, object]:
    validate_workspace(
        root,
        expected_source_sha256=expected_source_sha256,
        expected_bundle_sha256=expected_bundle_sha256,
    )
    root, source_payload, source_sha256 = _source(
        root,
        expected_source_sha256=expected_source_sha256,
    )
    if not 0 < scale <= 4:
        raise WorkspaceToolError("render_region_scale_invalid")
    x0, y0, x1, y1 = bbox
    if (
        x1 <= x0
        or y1 <= y0
        or (x1 - x0) * scale * (y1 - y0) * scale > _MAX_REGION_PIXELS
    ):
        raise WorkspaceToolError("render_region_bbox_invalid")
    document = pymupdf.open(stream=source_payload, filetype="pdf")
    try:
        page = _page(document, page_number)
        rect = page.rect
        if (
            x0 < rect.x0 - 0.5
            or y0 < rect.y0 - 0.5
            or x1 > rect.x1 + 0.5
            or y1 > rect.y1 + 0.5
        ):
            raise WorkspaceToolError("render_region_bbox_invalid")
        clip = pymupdf.Rect(x0, y0, x1, y1)
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            clip=clip,
            alpha=False,
            annots=False,
        )
        payload = bytes(pixmap.tobytes("png"))
    finally:
        document.close()
    return _write_render(
        root,
        source_sha256=source_sha256,
        page_number=page_number,
        kind="region",
        detail="region",
        bbox=bbox,
        scale=scale,
        payload=payload,
    )


def _write_render(
    root: Path,
    *,
    source_sha256: str,
    page_number: int,
    kind: str,
    detail: str,
    bbox: tuple[float, float, float, float],
    scale: float,
    payload: bytes,
) -> dict[str, object]:
    image_sha256 = hashlib.sha256(payload).hexdigest()
    identity = {
        "source_sha256": source_sha256,
        "page_number": page_number,
        "kind": kind,
        "detail": detail,
        "bbox": bbox,
        "scale": scale,
        "image_sha256": image_sha256,
    }
    receipt_id = canonical_sha256(identity)[:32]
    relative = f"scratch/renders/{receipt_id}.png"
    scratch = _safe_workspace_directory(root, "scratch")
    renders = scratch / "renders"
    try:
        renders.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _require_real_directory(renders)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_descriptor = os.open(renders, directory_flags)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                f"{receipt_id}.png",
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError as exc:
            raise WorkspaceToolError("render_output_exists") from exc
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return {
        "schema_version": "reqpilotx-reference-render-receipt/v1",
        "receipt_id": receipt_id,
        "path": relative,
        **identity,
    }


def _source(
    root: Path,
    *,
    expected_source_sha256: str,
) -> tuple[Path, bytes, str]:
    root = _workspace_root(root)
    manifest = _read_json(root / "manifest.json")
    document = manifest.get("document")
    if not isinstance(document, dict):
        raise WorkspaceToolError("workspace_manifest_invalid")
    source_sha256 = str(document.get("source_sha256") or "")
    if not _SHA256.fullmatch(source_sha256):
        raise WorkspaceToolError("workspace_manifest_invalid")
    if source_sha256 != expected_source_sha256:
        raise WorkspaceToolError("workspace_expected_source_mismatch")
    source = _safe_workspace_file(root, "source/document.pdf")
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != source_sha256:
        raise WorkspaceToolError("workspace_source_drift")
    return root, payload, source_sha256


def _workspace_root(root: Path) -> Path:
    candidate = root.expanduser()
    if candidate.is_symlink():
        raise WorkspaceToolError("workspace_root_invalid")
    root = candidate.resolve(strict=True)
    if not root.is_dir():
        raise WorkspaceToolError("workspace_root_invalid")
    return root


def _safe_workspace_file(root: Path, relative: str) -> Path:
    path = root
    parts = Path(_relative_path(relative)).parts
    for index, part in enumerate(parts):
        path = path / part
        if path.is_symlink():
            raise WorkspaceToolError("workspace_symlink_forbidden")
        if index < len(parts) - 1 and not path.is_dir():
            raise WorkspaceToolError("workspace_locked_file_missing")
    if not path.is_file():
        raise WorkspaceToolError("workspace_locked_file_missing")
    return path


def _safe_workspace_directory(root: Path, relative: str) -> Path:
    path = root
    for part in Path(_relative_path(relative)).parts:
        path = path / part
        _require_real_directory(path)
    return path


def _require_real_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise WorkspaceToolError("workspace_mutable_root_missing") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise WorkspaceToolError("workspace_symlink_forbidden")


def _page(document: pymupdf.Document, page_number: int) -> pymupdf.Page:
    if type(page_number) is not int or not 1 <= page_number <= document.page_count:
        raise WorkspaceToolError("render_page_invalid")
    return document.load_page(page_number - 1)


def _relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise WorkspaceToolError("workspace_relative_path_invalid")
    return path.as_posix()


def _read_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceToolError("workspace_json_missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceToolError("workspace_json_invalid") from exc
    if not isinstance(value, dict):
        raise WorkspaceToolError("workspace_json_invalid")
    return value


def _bbox(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox must be x0,y0,x1,y1") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be x0,y0,x1,y1")
    return parts  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("workspace", type=Path)
    page = subparsers.add_parser("render-page")
    page.add_argument("workspace", type=Path)
    page.add_argument("--page", required=True, type=int)
    page.add_argument("--detail", choices=tuple(_DETAIL_SCALE), default="readable")
    region = subparsers.add_parser("render-region")
    region.add_argument("workspace", type=Path)
    region.add_argument("--page", required=True, type=int)
    region.add_argument("--bbox", required=True, type=_bbox)
    region.add_argument("--scale", type=float, default=3.0)
    for command in (validate, page, region):
        command.add_argument("--expected-source-sha256", required=True)
        command.add_argument("--expected-bundle-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate_workspace(
                args.workspace,
                expected_source_sha256=args.expected_source_sha256,
                expected_bundle_sha256=args.expected_bundle_sha256,
            )
        elif args.command == "render-page":
            result = render_page(
                args.workspace,
                expected_source_sha256=args.expected_source_sha256,
                expected_bundle_sha256=args.expected_bundle_sha256,
                page_number=args.page,
                detail=args.detail,
            )
        else:
            result = render_region(
                args.workspace,
                expected_source_sha256=args.expected_source_sha256,
                expected_bundle_sha256=args.expected_bundle_sha256,
                page_number=args.page,
                bbox=args.bbox,
                scale=args.scale,
            )
    except (OSError, WorkspaceToolError) as exc:
        print(json.dumps({"status": "FAIL", "reason_code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
