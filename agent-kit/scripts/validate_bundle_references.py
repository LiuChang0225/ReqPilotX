#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PATTERNS = {
    "SOURCE_HANDLE": re.compile(r"\bnode-[0-9a-f]{32}\b"),
    "CELL_ID": re.compile(r"\bcell-[0-9a-f]{32}\b"),
    "TABLE_ID": re.compile(r"\btable-[0-9a-f]{32}\b"),
    "TABLE_TOKEN": re.compile(r"\bT[0-9]{4}\b"),
    "ASSET_ID": re.compile(r"\basset-[0-9a-f]{32}\b"),
    "PAGE_TOKEN": re.compile(r"\bP([0-9]{4})\b"),
}
_MAX_JSON_BYTES = 32 * 1024 * 1024


class ReferenceValidationError(RuntimeError):
    pass


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_artifact_references(
    workspace: Path,
    artifact: Path,
    *,
    expected_source_sha256: str,
    expected_bundle_sha256: str,
) -> dict[str, Any]:
    root = _real_directory(workspace)
    if not _SHA256.fullmatch(expected_source_sha256) or not _SHA256.fullmatch(
        expected_bundle_sha256
    ):
        raise ReferenceValidationError("expected_identity_invalid")
    lock = _read_json(_real_file(root, root / "workspace.lock.json"))
    if (
        lock.get("schema_version") != "reqpilotx-reference-workspace-lock/v2"
        or lock.get("source_sha256") != expected_source_sha256
        or lock.get("bundle_sha256") != expected_bundle_sha256
    ):
        raise ReferenceValidationError("workspace_identity_mismatch")
    files = lock.get("files")
    if not isinstance(files, list) or canonical_sha256(files) != expected_bundle_sha256:
        raise ReferenceValidationError("workspace_lock_invalid")
    locked_paths = _verify_locked_files(root, files)

    artifact_path = _real_file(root, artifact.expanduser())
    artifact_payload = _read_json(artifact_path)
    index = _build_index(root, locked_paths)
    occurrences = _collect_references(artifact_payload)
    if not occurrences:
        raise ReferenceValidationError("artifact_has_no_bundle_references")
    missing = [
        occurrence
        for occurrence in occurrences
        if occurrence["ref_id"] not in index[occurrence["kind"]]
    ]
    receipt = {
        "schema_version": "reqpilotx-bundle-reference-validation/v1",
        "status": "PASS" if not missing else "FAIL",
        "source_sha256": expected_source_sha256,
        "bundle_sha256": expected_bundle_sha256,
        "artifact_path": artifact_path.relative_to(root).as_posix(),
        "artifact_sha256": _sha256(artifact_path.read_bytes()),
        "reference_count": len(occurrences),
        "resolved_reference_count": len(occurrences) - len(missing),
        "missing_reference_count": len(missing),
        "missing_references": missing,
    }
    return receipt


def _build_index(root: Path, locked_paths: set[str]) -> dict[str, set[str]]:
    values = {kind: set() for kind in _PATTERNS}
    manifest = _read_json(_locked(root, locked_paths, "manifest.json"))
    document = manifest.get("document")
    if not isinstance(document, dict) or not isinstance(
        document.get("page_count"), int
    ):
        raise ReferenceValidationError("workspace_manifest_invalid")
    values["PAGE_TOKEN"].update(
        f"P{page:04d}" for page in range(1, int(document["page_count"]) + 1)
    )
    for relative in ("pages/source-map.jsonl", "pages/text-index.jsonl"):
        for item in _read_jsonl(_locked(root, locked_paths, relative)):
            handle = item.get("source_handle")
            if isinstance(handle, str):
                values["SOURCE_HANDLE"].add(handle)
    table_index = _read_json(_locked(root, locked_paths, "tables/index.json"))
    tables = table_index.get("tables")
    if not isinstance(tables, list):
        raise ReferenceValidationError("workspace_table_index_invalid")
    for table in tables:
        if not isinstance(table, dict):
            raise ReferenceValidationError("workspace_table_index_invalid")
        token = table.get("table_token")
        table_id = table.get("table_id")
        relative = table.get("json_path")
        if not all(isinstance(item, str) for item in (token, table_id, relative)):
            raise ReferenceValidationError("workspace_table_index_invalid")
        values["TABLE_TOKEN"].add(token)
        values["TABLE_ID"].add(table_id)
        payload = _read_json(_locked(root, locked_paths, relative))
        cells = payload.get("cells")
        if not isinstance(cells, list):
            raise ReferenceValidationError("workspace_table_invalid")
        for cell in cells:
            if isinstance(cell, dict) and isinstance(cell.get("cell_id"), str):
                values["CELL_ID"].add(cell["cell_id"])
    asset_index = _read_json(_locked(root, locked_paths, "assets/index.json"))
    assets = asset_index.get("unique_assets")
    if not isinstance(assets, list):
        raise ReferenceValidationError("workspace_asset_index_invalid")
    for asset in assets:
        if isinstance(asset, dict) and isinstance(asset.get("asset_id"), str):
            values["ASSET_ID"].add(asset["asset_id"])
    for placement in _read_jsonl(
        _locked(root, locked_paths, "assets/placements.jsonl")
    ):
        placement_id = placement.get("placement_id")
        if isinstance(placement_id, str):
            values["SOURCE_HANDLE"].add(placement_id)
    return values


def _collect_references(value: object) -> list[dict[str, str]]:
    occurrences: list[dict[str, str]] = []

    def visit(item: object, path: str) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                visit(nested, f"{path}/{key}")
            return
        if isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{path}/{index}")
            return
        if not isinstance(item, str):
            return
        for kind, pattern in _PATTERNS.items():
            for match in pattern.finditer(item):
                ref_id = (
                    f"P{int(match.group(1)):04d}"
                    if kind == "PAGE_TOKEN"
                    else match.group(0)
                )
                occurrences.append({"kind": kind, "ref_id": ref_id, "path": path})

    visit(value, "$")
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for occurrence in occurrences:
        key = (occurrence["kind"], occurrence["ref_id"], occurrence["path"])
        unique[key] = occurrence
    return list(unique.values())


def _verify_locked_files(root: Path, files: list[object]) -> set[str]:
    paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes"}:
            raise ReferenceValidationError("workspace_lock_entry_invalid")
        relative = _relative(str(item["path"]))
        path = _real_file(root, root / relative)
        payload = path.read_bytes()
        if _sha256(payload) != item["sha256"] or len(payload) != item["size_bytes"]:
            raise ReferenceValidationError("workspace_locked_file_drift")
        paths.add(relative)
    return paths


def _locked(root: Path, locked_paths: set[str], relative: str) -> Path:
    relative = _relative(relative)
    if relative not in locked_paths:
        raise ReferenceValidationError("workspace_required_index_unlocked")
    return _real_file(root, root / relative)


def _real_directory(path: Path) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ReferenceValidationError("workspace_symlink_forbidden")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise ReferenceValidationError("workspace_directory_required")
    return resolved


def _real_file(root: Path, path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ReferenceValidationError("workspace_file_invalid") from exc
    if path.is_symlink() or not resolved.is_file():
        raise ReferenceValidationError("workspace_file_invalid")
    return resolved


def _relative(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise ReferenceValidationError("workspace_relative_path_invalid")
    return path.as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    if len(payload) > _MAX_JSON_BYTES:
        raise ReferenceValidationError("workspace_json_budget_exceeded")
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceValidationError("workspace_json_invalid") from exc
    if not isinstance(value, dict):
        raise ReferenceValidationError("workspace_json_invalid")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    payload = path.read_bytes()
    if len(payload) > _MAX_JSON_BYTES:
        raise ReferenceValidationError("workspace_json_budget_exceeded")
    values: list[dict[str, Any]] = []
    try:
        for line in payload.decode("utf-8").splitlines():
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReferenceValidationError("workspace_json_invalid")
            values.append(value)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceValidationError("workspace_json_invalid") from exc
    return values


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-bundle-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = validate_artifact_references(
            args.workspace,
            args.artifact,
            expected_source_sha256=args.expected_source_sha256,
            expected_bundle_sha256=args.expected_bundle_sha256,
        )
    except (OSError, ReferenceValidationError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "reqpilotx-bundle-reference-validation/v1",
                    "status": "FAIL",
                    "reason_code": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
