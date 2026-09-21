#!/usr/bin/env python3
"""Validate and materialize ReqPilotX Agent assets from their canonical sources."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from typing import Any, Iterable


SCHEMA_VERSION = "reqpilotx-agent-asset-catalog/v1"
BUNDLE_SCHEMA_VERSION = "reqpilotx-agent-asset-bundle/v1"
CLASSIFICATIONS = frozenset(
    {
        "CURRENT",
        "COMPATIBILITY",
        "EXPERIMENT",
        "HISTORICAL",
        "GENERATED",
        "PRIVATE",
        "UNRESOLVED",
    }
)
PROJECTION_MODES = frozenset({"mirror", "subset"})


class AssetRegistryError(ValueError):
    """A source, projection, identity, or frozen bundle is invalid."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_sha256(value: object) -> str:
    return sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _safe_relative(value: object, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise AssetRegistryError(f"{field} must be a non-empty relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise AssetRegistryError(f"{field} is unsafe or non-canonical: {value!r}")
    return path


def _path(root: Path, relative: object, *, field: str) -> Path:
    pure = _safe_relative(relative, field=field)
    candidate = root.joinpath(*pure.parts)
    resolved = candidate.resolve(strict=False)
    if resolved != root and not resolved.is_relative_to(root):
        raise AssetRegistryError(f"{field} escapes the repository: {pure}")
    return candidate


def _regular_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        raise AssetRegistryError(f"Asset directory is missing or is a symlink: {root}")
    files: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise AssetRegistryError(f"Asset trees cannot contain symlinks: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            _safe_relative(relative, field="asset file")
            files[relative] = path
    if not files:
        raise AssetRegistryError(f"Asset directory is empty: {root}")
    return files


def tree_receipt(root: Path) -> dict[str, Any]:
    files = _regular_files(root)
    entries = [
        {
            "path": relative,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for relative, path in files.items()
    ]
    return {
        "file_count": len(entries),
        "files": entries,
        "tree_sha256": canonical_sha256(
            [{"path": item["path"], "sha256": item["sha256"]} for item in entries]
        ),
    }


def load_catalog(repo: Path) -> dict[str, Any]:
    repo = repo.expanduser().resolve(strict=True)
    path = repo / "agent-assets/catalog.json"
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssetRegistryError(f"Agent asset catalog is unreadable: {path}") from exc
    if not isinstance(catalog, dict) or set(catalog) != {
        "schema_version",
        "groups",
    }:
        raise AssetRegistryError("Agent asset catalog has unsupported top-level fields")
    if catalog.get("schema_version") != SCHEMA_VERSION:
        raise AssetRegistryError("Agent asset catalog schema is unsupported")
    if not isinstance(catalog.get("groups"), list) or not catalog["groups"]:
        raise AssetRegistryError("Agent asset catalog has no groups")
    return catalog


def _validate_group(repo: Path, group: object) -> dict[str, Any]:
    required = {
        "id",
        "classification",
        "asset_kind",
        "source",
        "tree_sha256",
        "consumers",
        "target",
        "compatibility",
        "verification",
        "retirement_condition",
        "projections",
    }
    optional = {"bundle_root"}
    if not isinstance(group, dict) or set(group) - required - optional or required - set(group):
        raise AssetRegistryError("Agent asset group fields are incomplete or unsupported")
    identifier = group.get("id")
    if not isinstance(identifier, str) or not identifier or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in identifier
    ):
        raise AssetRegistryError(f"Agent asset group has an invalid id: {identifier!r}")
    if group.get("classification") not in CLASSIFICATIONS:
        raise AssetRegistryError(f"Agent asset group has an invalid classification: {identifier}")
    for field in (
        "asset_kind",
        "target",
        "compatibility",
        "verification",
        "retirement_condition",
    ):
        if not isinstance(group.get(field), str) or not str(group[field]).strip():
            raise AssetRegistryError(f"Agent asset group {identifier} has an invalid {field}")
    consumers = group.get("consumers")
    if not isinstance(consumers, list) or not consumers or not all(
        isinstance(value, str) and value.strip() for value in consumers
    ):
        raise AssetRegistryError(f"Agent asset group {identifier} has no consumers")
    source = _path(repo, group["source"], field=f"{identifier}.source")
    receipt = tree_receipt(source)
    if receipt["tree_sha256"] != group.get("tree_sha256"):
        raise AssetRegistryError(
            f"Canonical asset identity changed for {identifier}: "
            f"expected {group.get('tree_sha256')}, got {receipt['tree_sha256']}"
        )
    projections = group.get("projections")
    if not isinstance(projections, list):
        raise AssetRegistryError(f"Agent asset group {identifier} projections must be a list")
    projection_receipts = []
    for number, projection in enumerate(projections):
        if not isinstance(projection, dict) or set(projection) != {"path", "mode"}:
            raise AssetRegistryError(f"Agent asset group {identifier} has an invalid projection")
        mode = projection.get("mode")
        if mode not in PROJECTION_MODES:
            raise AssetRegistryError(f"Agent asset group {identifier} has an invalid projection mode")
        target = _path(
            repo,
            projection["path"],
            field=f"{identifier}.projections[{number}]",
        )
        target_files = _regular_files(target)
        source_files = _regular_files(source)
        source_names = set(source_files)
        target_names = set(target_files)
        if mode == "mirror" and source_names != target_names:
            raise AssetRegistryError(
                f"Projection inventory differs for {identifier}: "
                f"missing={sorted(source_names - target_names)}, "
                f"extra={sorted(target_names - source_names)}"
            )
        if not source_names.issubset(target_names):
            raise AssetRegistryError(
                f"Projection is missing files for {identifier}: "
                f"{sorted(source_names - target_names)}"
            )
        mismatches = [
            relative
            for relative, path in source_files.items()
            if sha256_file(path) != sha256_file(target_files[relative])
        ]
        if mismatches:
            raise AssetRegistryError(
                f"Projection content differs for {identifier}: {mismatches}"
            )
        projection_receipts.append(
            {
                "path": projection["path"],
                "mode": mode,
                "verified_files": len(source_files),
            }
        )
    bundle_root = group.get("bundle_root")
    if bundle_root is not None:
        _safe_relative(bundle_root, field=f"{identifier}.bundle_root")
    return {
        "id": identifier,
        "classification": group["classification"],
        "source": group["source"],
        "tree_sha256": receipt["tree_sha256"],
        "file_count": receipt["file_count"],
        "projections": projection_receipts,
        "bundle_root": bundle_root,
    }


def validate_repository_assets(repo: Path) -> dict[str, Any]:
    repo = repo.expanduser().resolve(strict=True)
    catalog = load_catalog(repo)
    groups = []
    seen: set[str] = set()
    for group in catalog["groups"]:
        receipt = _validate_group(repo, group)
        if receipt["id"] in seen:
            raise AssetRegistryError(f"Duplicate Agent asset group: {receipt['id']}")
        seen.add(receipt["id"])
        groups.append(receipt)
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_sha256": sha256_file(repo / "agent-assets/catalog.json"),
        "groups": groups,
    }


def release_asset_manifest(repo: Path) -> dict[str, Any]:
    repo = repo.expanduser().resolve(strict=True)
    receipt = validate_repository_assets(repo)
    catalog = load_catalog(repo)
    by_id = {group["id"]: group for group in catalog["groups"]}
    groups = []
    for verified in receipt["groups"]:
        group = by_id[verified["id"]]
        bundle_root = group.get("bundle_root")
        if bundle_root is None:
            continue
        source = _path(repo, group["source"], field=f"{group['id']}.source")
        files = []
        for relative, path in _regular_files(source).items():
            bundle_path = PurePosixPath(bundle_root).joinpath(relative).as_posix()
            _safe_relative(bundle_path, field=f"{group['id']}.bundle_file")
            files.append(
                {
                    "path": bundle_path,
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
            )
        groups.append(
            {
                "id": group["id"],
                "source_tree_sha256": verified["tree_sha256"],
                "files": files,
            }
        )
    if not groups:
        raise AssetRegistryError("No Agent assets are declared as release inputs")
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "catalog_sha256": receipt["catalog_sha256"],
        "groups": groups,
    }


def verify_frozen_bundle(bundle: Path) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve(strict=True)
    if not bundle.is_dir() or bundle.is_symlink():
        raise AssetRegistryError("Frozen plugin bundle must be a regular directory")
    receipt_path = bundle / "build-receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssetRegistryError("Frozen plugin build receipt is unreadable") from exc
    manifest = receipt.get("agent_assets") if isinstance(receipt, dict) else None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise AssetRegistryError("Frozen plugin has no supported Agent asset manifest")
    groups = manifest.get("groups")
    if not isinstance(groups, list) or not groups:
        raise AssetRegistryError("Frozen plugin Agent asset manifest is empty")
    seen_paths: set[str] = set()
    verified_files = 0
    for group in groups:
        if not isinstance(group, dict) or set(group) != {
            "id",
            "source_tree_sha256",
            "files",
        }:
            raise AssetRegistryError("Frozen plugin Agent asset group is invalid")
        files = group.get("files")
        if not isinstance(files, list) or not files:
            raise AssetRegistryError("Frozen plugin Agent asset group has no files")
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
                raise AssetRegistryError("Frozen plugin Agent asset file record is invalid")
            pure = _safe_relative(item["path"], field="frozen asset path")
            if pure.as_posix() in seen_paths:
                raise AssetRegistryError(f"Frozen Agent asset path is repeated: {pure}")
            seen_paths.add(pure.as_posix())
            path = bundle.joinpath(*pure.parts)
            if path.is_symlink() or not path.is_file():
                raise AssetRegistryError(f"Frozen Agent asset is missing or is a symlink: {pure}")
            if path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
                raise AssetRegistryError(f"Frozen Agent asset failed identity verification: {pure}")
            verified_files += 1
    return {
        "status": "VERIFIED",
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "group_count": len(groups),
        "file_count": verified_files,
        "catalog_sha256": manifest.get("catalog_sha256"),
    }


def _copy_tree(source: Path, target: Path, *, mode: str) -> None:
    source_files = _regular_files(source)
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise AssetRegistryError(f"Projection target is unsafe: {target}")
        target_files = _regular_files(target) if any(target.iterdir()) else {}
        extra = sorted(set(target_files) - set(source_files))
        if mode == "mirror" and extra:
            raise AssetRegistryError(
                f"Projection has undeclared files; move or review them before sync: {extra}"
            )
    target.mkdir(parents=True, exist_ok=True)
    for relative, path in source_files.items():
        destination = target.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


def materialize_projections(repo: Path, identifiers: Iterable[str] | None = None) -> dict[str, Any]:
    repo = repo.expanduser().resolve(strict=True)
    catalog = load_catalog(repo)
    requested = set(identifiers or ())
    available = {group["id"] for group in catalog["groups"]}
    if requested - available:
        raise AssetRegistryError(
            f"Unknown Agent asset group(s): {sorted(requested - available)}"
        )
    selected = []
    for group in catalog["groups"]:
        if requested and group["id"] not in requested:
            continue
        source = _path(repo, group["source"], field=f"{group['id']}.source")
        receipt = tree_receipt(source)
        if receipt["tree_sha256"] != group.get("tree_sha256"):
            raise AssetRegistryError(
                f"Canonical asset identity changed for {group['id']}; "
                "update and review the catalog before syncing projections"
            )
        for projection in group["projections"]:
            target = _path(repo, projection["path"], field=f"{group['id']}.projection")
            _copy_tree(source, target, mode=projection["mode"])
        selected.append(group["id"])
    validate_repository_assets(repo)
    return {"status": "MATERIALIZED", "groups": selected}


def release_input_paths(repo: Path) -> tuple[str, ...]:
    catalog = load_catalog(repo)
    paths = {
        "agent-assets/catalog.json",
        "agent-assets/scripts/asset_registry.py",
        "plugins/reqpilot/scripts/build_bundle.py",
    }
    for group in catalog["groups"]:
        if group.get("bundle_root") is None:
            continue
        paths.add(group["source"])
        paths.update(projection["path"] for projection in group["projections"])
    return tuple(sorted(paths))


def assert_release_inputs_match_commit(repo: Path, commit: str) -> None:
    """Bind release asset bytes to one clean, committed checkout."""

    repo = repo.expanduser().resolve(strict=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != commit:
        raise AssetRegistryError("Engine and Agent assets must come from the same commit")
    scopes = release_input_paths(repo)
    status = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-C",
            str(repo),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *scopes,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise AssetRegistryError(
            "Release Agent assets must be committed and clean:\n" + status
        )
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", commit, "--", *scopes],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if not tracked:
        raise AssetRegistryError("Release Agent asset inputs are not committed")
    for relative in tracked:
        pure = _safe_relative(relative, field="committed asset input")
        path = repo.joinpath(*pure.parts)
        if path.is_symlink() or not path.is_file():
            raise AssetRegistryError(f"Release Agent asset input is not a regular file: {relative}")
        committed = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{relative}"],
            check=True,
            capture_output=True,
        ).stdout
        if path.read_bytes() != committed:
            raise AssetRegistryError(
                f"Release Agent asset input differs from the commit: {relative}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    sync = subparsers.add_parser("sync")
    sync.add_argument("--group", action="append", default=[])
    frozen = subparsers.add_parser("verify-bundle")
    frozen.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "check":
            result = validate_repository_assets(args.repo)
        elif args.command == "sync":
            result = materialize_projections(args.repo, args.group)
        else:
            result = verify_frozen_bundle(args.bundle)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AssetRegistryError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
