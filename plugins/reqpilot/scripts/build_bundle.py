#!/usr/bin/env python3
"""Build a portable plugin from the shared selection or an engine wheel.

The R1 release path consumes an explicit engine wheel generated from the same
centrally selected source closure. The no-wheel path preserves the existing
checkout development build used by native tasks. No runtime/customer artifacts,
databases, environment files or old answers enter either form.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
import json
import shutil
import sys
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from portable_core import (
    ENGINE_DISTRIBUTION,
    MODULE_ROOTS,  # noqa: F401 - compatibility export for bundle-boundary tests
    NAMESPACE_INITIALIZERS,
    SCRIPT_ROOTS,  # noqa: F401 - compatibility export for bundle-boundary tests
    canonical_digest,
    digest,
    digest_bytes,
    find_repo,
    module_file,  # noqa: F401 - compatibility export for bundle-boundary tests
    python_closure,  # noqa: F401 - compatibility export for bundle-boundary tests
    select_core,
)


ENGINE_WRAPPER_MEMBERS = frozenset({
    "reqpilotx_engine/__init__.py",
    "reqpilotx_engine/core-manifest.json",
})
DIST_INFO_MEMBERS = frozenset(
    {"METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD"}
)


def _load_agent_asset_registry(repo: Path):
    path = repo / "agent-assets/scripts/asset_registry.py"
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Agent asset registry is missing or unsafe: {path}")
    spec = importlib.util.spec_from_file_location("reqpilotx_agent_asset_registry", path)
    if spec is None or spec.loader is None:
        raise ValueError("Agent asset registry cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _asset_source(repo: Path, catalog: dict, identifier: str) -> Path:
    matches = [group for group in catalog["groups"] if group.get("id") == identifier]
    if len(matches) != 1:
        raise ValueError(f"Agent asset catalog must define exactly one {identifier!r} group")
    path = repo.joinpath(*PurePosixPath(matches[0]["source"]).parts)
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"Canonical Agent asset source is missing or unsafe: {identifier}")
    return path


def _safe_wheel_member(name: str) -> PurePosixPath:
    pure = PurePosixPath(name)
    if (
        not name or name.endswith("/") or "\\" in name or "\x00" in name
        or pure.is_absolute() or ".." in pure.parts or pure.as_posix() != name
    ):
        raise ValueError(f"Engine wheel contains an unsafe or non-canonical member: {name!r}")
    return pure


def _record_hash(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}"


def _validate_record(archive: ZipFile, names: list[str], record_path: str) -> None:
    rows = list(csv.reader(io.StringIO(archive.read(record_path).decode("utf-8"))))
    if any(len(row) != 3 for row in rows):
        raise ValueError("Engine wheel RECORD must contain path, hash and size columns")
    by_path = {}
    for path, digest_value, size in rows:
        if path in by_path:
            raise ValueError(f"Engine wheel RECORD repeats a member: {path}")
        by_path[path] = (digest_value, size)
    if set(by_path) != set(names):
        missing = sorted(set(names) - set(by_path))
        extra = sorted(set(by_path) - set(names))
        raise ValueError(f"Engine wheel RECORD inventory mismatch: missing={missing}, extra={extra}")
    for name in names:
        digest_value, size = by_path[name]
        if name == record_path:
            if digest_value or size:
                raise ValueError("Engine wheel RECORD must leave its own hash and size empty")
            continue
        data = archive.read(name)
        if digest_value != _record_hash(data) or size != str(len(data)):
            raise ValueError(f"Engine wheel RECORD hash or size mismatch: {name}")


def _validate_distribution_metadata(archive: ZipFile, dist_info: str, manifest: dict) -> None:
    metadata = BytesParser(policy=email_policy).parsebytes(archive.read(f"{dist_info}/METADATA"))
    if metadata.get("Name") != ENGINE_DISTRIBUTION or metadata.get("Version") != manifest.get("version"):
        raise ValueError("Engine wheel METADATA distribution or version differs from the core manifest")
    if metadata.get("Requires-Python") != f">={manifest.get('python_minimum')}":
        raise ValueError("Engine wheel METADATA Requires-Python differs from the core manifest")
    actual_requirements = sorted(metadata.get_all("Requires-Dist") or [], key=str.casefold)
    expected_requirements = sorted(manifest.get("requirements") or [], key=str.casefold)
    if actual_requirements != expected_requirements:
        raise ValueError("Engine wheel METADATA dependencies differ from the core manifest")
    wheel_metadata = BytesParser(policy=email_policy).parsebytes(archive.read(f"{dist_info}/WHEEL"))
    if wheel_metadata.get("Root-Is-Purelib", "").lower() != "true" or "py3-none-any" not in (wheel_metadata.get_all("Tag") or []):
        raise ValueError("Engine wheel must be a pure Python py3-none-any distribution")
    top_level = {line.strip() for line in archive.read(f"{dist_info}/top_level.txt").decode("utf-8").splitlines() if line.strip()}
    if top_level != {"reqpilotx", "reqpilotx_engine"}:
        raise ValueError("Engine wheel top-level packages differ from the release contract")
    entry_points = archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")
    if entry_points.strip().splitlines() != [
        "[console_scripts]",
        "reqpilotx-fast-source-capture = reqpilotx.tools.fast_source_capture:main",
    ]:
        raise ValueError("Engine wheel console entry point differs from the release contract")


def _wheel_content_inventory(wheel: Path) -> dict[str, str]:
    with ZipFile(wheel) as archive:
        names = [item.filename for item in archive.infolist()]
        if len(names) != len(set(names)):
            raise ValueError("Engine wheel contains duplicate ZIP members")
        for name in names:
            _safe_wheel_member(name)
        return {name: digest_bytes(archive.read(name)) for name in sorted(names)}


def _validated_wheel_manifest(wheel: Path) -> dict:
    wheel = wheel.expanduser().resolve(strict=True)
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError("--engine-wheel must be an existing wheel file")
    try:
        with ZipFile(wheel) as archive:
            names_list = [item.filename for item in archive.infolist()]
            if len(names_list) != len(set(names_list)):
                raise ValueError("Engine wheel contains duplicate ZIP members")
            for name in names_list:
                _safe_wheel_member(name)
            names = set(names_list)
            manifest_path = "reqpilotx_engine/core-manifest.json"
            if manifest_path not in names:
                raise ValueError("Engine wheel is missing reqpilotx_engine/core-manifest.json")
            manifest = json.loads(archive.read(manifest_path))
            runtime_files = manifest.get("runtime_files") if isinstance(manifest, dict) else None
            if not isinstance(runtime_files, list) or not runtime_files:
                raise ValueError("Engine wheel identity manifest has no runtime file list")
            seen_runtime = set()
            for entry in runtime_files:
                if not isinstance(entry, dict):
                    raise ValueError("Engine wheel identity manifest has an invalid runtime entry")
                wheel_path = str(entry.get("wheel_path", ""))
                runtime_path = str(entry.get("runtime_path", ""))
                pure = PurePosixPath(wheel_path)
                if runtime_path in seen_runtime:
                    raise ValueError(f"Engine manifest repeats a runtime path: {runtime_path}")
                seen_runtime.add(runtime_path)
                if not wheel_path or pure.is_absolute() or ".." in pure.parts or wheel_path not in names:
                    raise ValueError(f"Engine manifest has an invalid or missing wheel member: {wheel_path}")
                if digest_bytes(archive.read(wheel_path)) != entry.get("sha256"):
                    raise ValueError(f"Engine wheel member failed its manifest hash: {wheel_path}")
    except (BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Engine wheel or identity manifest is unreadable") from exc
    expected_identity = canonical_digest({key: value for key, value in manifest.items() if key != "core_identity"})
    if (
        manifest.get("schema_version") != "reqpilotx-engine-core/v2"
        or manifest.get("distribution") != ENGINE_DISTRIBUTION
        or manifest.get("source_repository") != "ReqPilotX"
        or manifest.get("core_identity") != expected_identity
    ):
        raise ValueError("Engine wheel identity manifest is invalid")
    if manifest.get("customer_data_included") is not False or manifest.get("banned_host_members_found") != 0:
        raise ValueError("Engine wheel does not satisfy the portable release boundary")
    declared = {str(entry["wheel_path"]) for entry in manifest["runtime_files"]}
    expected_dist_info = (
        f"{ENGINE_DISTRIBUTION.replace('-', '_')}-{str(manifest['version']).replace('-', '_')}.dist-info"
    )
    dist_info_members = {f"{expected_dist_info}/{name}" for name in DIST_INFO_MEMBERS}
    expected_members = declared | set(ENGINE_WRAPPER_MEMBERS) | dist_info_members
    if names != expected_members:
        missing = sorted(expected_members - names)
        extra = sorted(names - expected_members)
        raise ValueError(f"Engine wheel contains missing or undeclared members: missing={missing}, extra={extra}")
    for name in names:
        parts = PurePosixPath(name).parts
        if (
            name.endswith(".pth")
            or parts[:1] == ("app",)
            or parts[:1] == ("reqpilotx_server",)
        ):
            raise ValueError(f"Engine wheel contains a forbidden install member: {name}")
    with ZipFile(wheel) as archive:
        _validate_record(archive, names_list, f"{expected_dist_info}/RECORD")
        _validate_distribution_metadata(archive, expected_dist_info, manifest)
    return manifest


def _assert_same_selection(selection, manifest: dict) -> None:
    expected = [(member.runtime_relative, member.packaged_sha256) for member in selection.members]
    actual = [(str(entry.get("runtime_path")), str(entry.get("sha256"))) for entry in manifest.get("runtime_files", [])]
    if actual != expected:
        raise ValueError("Engine wheel does not match the current shared portable selection")
    if manifest.get("module_roots") != list(selection.module_roots):
        raise ValueError("Engine wheel module roots do not match the current shared selection")
    if manifest.get("requirements") != list(selection.requirements):
        raise ValueError("Engine wheel dependency pins do not match the current shared selection")


def _install_wheel_runtime(wheel: Path, manifest: dict, runtime: Path) -> list[dict]:
    entries = []
    with ZipFile(wheel) as archive:
        for item in manifest["runtime_files"]:
            relative = PurePosixPath(str(item["runtime_path"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Engine runtime path is unsafe: {relative}")
            allowed = relative.parts[:1] == ("reqpilotx",)
            if not allowed:
                raise ValueError(f"Engine wheel attempted to install an unexpected runtime path: {relative}")
            data = archive.read(item["wheel_path"])
            destination = runtime.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            entries.append({
                "path": str(relative),
                "sha256": digest_bytes(data),
                "upstream_sha256": item["upstream_sha256"],
                "bytes": len(data),
            })
    return entries


def _install_direct_selection(selection, runtime: Path) -> list[dict]:
    entries = []
    for member in selection.members:
        destination = runtime / member.runtime_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(member.content)
        entries.append({
            "path": member.runtime_relative,
            "sha256": member.packaged_sha256,
            "upstream_sha256": member.upstream_sha256,
            "bytes": len(member.content),
        })
    return entries


def build(repo: Path, output: Path, *, extraction_only: bool = False, engine_wheel: Path | None = None) -> dict:
    repo = find_repo(repo)
    plugin_root = repo / "plugins/reqpilot"
    if not plugin_root.is_dir() or plugin_root.is_symlink():
        raise ValueError("ReqPilotX plugin source is missing or unsafe")
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("Bundle output must not exist; each build gets a new immutable directory")
    if output == plugin_root or output in plugin_root.parents or plugin_root in output.parents:
        raise ValueError("Build output must be outside plugin source")
    asset_registry = _load_agent_asset_registry(repo)
    agent_assets = asset_registry.release_asset_manifest(repo)
    asset_catalog = asset_registry.load_catalog(repo)
    skill_source = _asset_source(repo, asset_catalog, "current-portable-skills")
    pdf_source = _asset_source(repo, asset_catalog, "vendor-pdf-26-905-11957")
    selection = select_core(repo, extraction_only=extraction_only)
    engine_manifest = None
    if engine_wheel is not None and extraction_only:
        raise ValueError("Extraction-only bundles are not full engine release consumers")
    try:
        if not extraction_only and engine_wheel is not None:
            engine_wheel = engine_wheel.expanduser().resolve(strict=True)
            engine_manifest = _validated_wheel_manifest(engine_wheel)
            _assert_same_selection(selection, engine_manifest)
            asset_registry.assert_release_inputs_match_commit(
                repo,
                str(engine_manifest["source_commit"]),
            )
            loaded_builder = Path(__file__).resolve()
            committed_builder = plugin_root / "scripts/build_bundle.py"
            if loaded_builder.read_bytes() != committed_builder.read_bytes():
                raise ValueError("Loaded plugin builder differs from the release checkout")
        shutil.copytree(
            plugin_root,
            output,
            ignore=shutil.ignore_patterns(
                "runtime",
                "runtime-data",
                "tests",
                "skills",
                "__pycache__",
                "*.pyc",
                "*.zip",
                ".pytest_cache",
            ),
        )
        shutil.copytree(skill_source, output / "skills")
        shutil.rmtree(output / "capabilities/pdf")
        shutil.copytree(pdf_source, output / "capabilities/pdf")
        runtime = output / "runtime/prototype"
        if extraction_only or engine_manifest is None:
            runtime_files = _install_direct_selection(selection, runtime)
        else:
            runtime_files = _install_wheel_runtime(engine_wheel, engine_manifest, runtime)
        (output / "requirements.txt").write_text("\n".join(selection.requirements) + "\n", encoding="utf-8")
        receipt = {
            "schema_version": "reqpilot-portable-build/v1",
            "plugin_name": "reqpilot",
            "source_repository": "ReqPilotX",
            "domain_implementation": "UNCHANGED_SHARED_ENGINE_RELEASE" if engine_manifest is not None else "UNCHANGED_ALLOWLIST_COPY",
            "release_mode": "ENGINE_WHEEL" if engine_manifest is not None else "DEVELOPMENT_SOURCE_COPY",
            "namespace_initializers": [
                {"path": path, "reason": "Suppress eager host/provider exports; every concrete implementation remains unchanged"}
                for path in sorted(NAMESPACE_INITIALIZERS)
            ],
            "source_checkout_required_at_runtime": False,
            "delivery_included": not extraction_only,
            "decomposition_included": not extraction_only,
            "customer_data_included": False,
            "module_roots": list(selection.module_roots),
            "python_minimum": "3.11",
            "requirements": list(selection.requirements),
            "optional_local_ocr_requirements": (plugin_root / "requirements-ocr.txt").read_text(encoding="utf-8").splitlines(),
            "optional_or_unknown_imports": list(selection.optional_or_unknown_imports),
            "runtime_files": runtime_files,
            "agent_assets": agent_assets,
        }
        if engine_manifest is not None:
            receipt["engine_release"] = {
                "distribution": engine_manifest["distribution"],
                "version": engine_manifest["version"],
                "core_identity": engine_manifest["core_identity"],
                "source_commit": engine_manifest["source_commit"],
                "source_tree": engine_manifest["source_tree"],
                "wheel_filename": engine_wheel.name,
                "wheel_sha256": digest(engine_wheel),
                "runtime_file_count": len(runtime_files),
            }
        receipt["plugin_files"] = [
            {"path": str(path.relative_to(output)), "sha256": digest(path), "bytes": path.stat().st_size}
            for path in sorted(output.rglob("*"))
            if path.is_file() and "runtime" not in path.relative_to(output).parts
        ]
        receipt["bundle_sha256"] = canonical_digest(receipt)
        (output / "build-receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        asset_registry.verify_frozen_bundle(output)
        return {
            "status": "EXTRACTION_ONLY_BUILT" if extraction_only else "BUILT",
            "output": str(output),
            "runtime_file_count": len(runtime_files),
            "runtime_bytes": sum(item["bytes"] for item in runtime_files),
            "optional_or_unknown_imports": list(selection.optional_or_unknown_imports),
            "engine_core_identity": engine_manifest["core_identity"] if engine_manifest else None,
        }
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, help="ReqPilotX checkout; used only at build time")
    parser.add_argument("--output", type=Path, required=True, help="New bundle directory outside plugin source")
    parser.add_argument("--engine-wheel", type=Path, help="Previously built and verified reqpilotx-engine wheel")
    parser.add_argument("--extraction-only", action="store_true", help="Explicit intermediate bundle without the delivery bridge; never a full release")
    args = parser.parse_args()
    try:
        print(json.dumps(build(find_repo(args.repo), args.output, extraction_only=args.extraction_only, engine_wheel=args.engine_wheel), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "BUILD_FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
