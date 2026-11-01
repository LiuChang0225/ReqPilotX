"""Build a packaged offline runtime release, with explicit provenance and hashes."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def verify_capability_bundle(bundle: Path, engine: dict, wheel: Path) -> dict:
    receipt = json.loads((bundle / "build-receipt.json").read_text())
    if (receipt.get("source_repository") != "ReqPilotX"
            or receipt.get("release_mode") != "ENGINE_WHEEL"
            or receipt.get("customer_data_included") is not False
            or receipt.get("source_checkout_required_at_runtime") is not False
            or receipt.get("delivery_included") is not True
            or receipt.get("decomposition_included") is not True
            or receipt.get("bundle_sha256") != canonical_digest({k: v for k, v in receipt.items() if k != "bundle_sha256"})):
        raise ValueError("Invalid registered capability bundle receipt")
    release = receipt.get("engine_release", {})
    if (release.get("core_identity") != engine["core_identity"]
            or release.get("source_commit") != engine["source_commit"]
            or release.get("wheel_sha256") != sha256(wheel)):
        raise ValueError("Capability bundle does not match this exact engine wheel")
    declared = set()
    for category, prefix in (("plugin_files", ""), ("runtime_files", "runtime/prototype/")):
        for record in receipt[category]:
            relative = Path(prefix + record["path"])
            if relative.is_absolute() or ".." in relative.parts or str(relative) in declared:
                raise ValueError("Unsafe or duplicate capability bundle path")
            declared.add(str(relative))
            target = bundle / relative
            if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(bundle.resolve()):
                raise ValueError("Unsafe capability bundle member")
            if target.stat().st_size != record["bytes"] or sha256(target) != record["sha256"]:
                raise ValueError("Capability bundle member hash/size mismatch")
    actual = set()
    for member in bundle.rglob("*"):
        if member.is_symlink():
            raise ValueError("Capability bundle symlinks are forbidden")
        if member.is_file() and member != bundle / "build-receipt.json" and "__pycache__" not in member.parts and member.suffix != ".pyc":
            actual.add(str(member.relative_to(bundle)))
    if actual != declared:
        raise ValueError("Capability bundle payload does not match its immutable receipt")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("revision", "root", "wheels", "wheelhouse", "output"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--capability-bundle", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", args.revision):
        raise ValueError("Invalid revision label")
    root, output = Path(args.root).resolve(), Path(args.output).resolve()
    if output.exists():
        raise ValueError("Release output already exists")
    frontend = root / "apps/web/dist"
    if not (frontend / "index.html").is_file():
        raise ValueError("Prebuilt frontend is missing")
    source_commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--",
        "apps/server", "packages/engine", "apps/web", "packages/workbench-ui", "deploy/saas", "docs/deployment/saas-pilot.md"], text=True)
    if status:
        raise ValueError("Candidate runtime inputs must be committed and frozen before packaging")
    wheels = tuple(Path(args.wheels).glob("*.whl"))
    if len(wheels) != 2 or {p.name.split("-")[0] for p in wheels} != {"reqpilotx_engine", "reqpilotx_server"}:
        raise ValueError("Expected exactly engine and server wheels")
    engine_wheel = next(path for path in wheels if path.name.startswith("reqpilotx_engine-"))
    with zipfile.ZipFile(engine_wheel) as archive:
        engine = json.loads(archive.read("reqpilotx_engine/core-manifest.json"))
    if (engine.get("schema_version") != "reqpilotx-engine-core/v2"
            or engine.get("source_commit") != source_commit
            or engine.get("customer_data_included") is not False
            or engine.get("core_identity") != canonical_digest({k: v for k, v in engine.items() if k != "core_identity"})):
        raise ValueError("Final release needs the exact frozen formal engine wheel")
    bundle = Path(args.capability_bundle).resolve(strict=True)
    capability_receipt = verify_capability_bundle(bundle, engine, engine_wheel)
    server_wheel = next(path for path in wheels if path.name.startswith("reqpilotx_server-"))
    with zipfile.ZipFile(server_wheel) as archive:
        required = {
            "reqpilotx_server/saas/auth.py", "reqpilotx_server/saas/config.py",
            "reqpilotx_server/saas/store.py", "reqpilotx_server/saas/authorization.py",
            "reqpilotx_server/saas/invite.py", "reqpilotx_server/saas/routes.py",
            "reqpilotx_server/resources/migrations/postgresql/0039_saas_identity.sql",
        }
        if not required.issubset(set(archive.namelist())):
            raise ValueError("SaaS auth modules/migration missing from server wheel; refusing baseline-only release")
    # The wheel payload must match this candidate's source. Concurrent edits after
    # wheel construction must force a rebuild, never relabel an old wheel.
    for wheel_path, source_folder in (
        (server_wheel, root / "apps/server/src"),
        (next(path for path in wheels if path.name.startswith("reqpilotx_engine-")), root / "packages/engine/src"),
    ):
        with zipfile.ZipFile(wheel_path) as archive:
            for source_path in source_folder.rglob("*"):
                if not source_path.is_file() or source_path.suffix not in {".py", ".sql"} or "__pycache__" in source_path.parts:
                    continue
                member = source_path.relative_to(source_folder).as_posix()
                if member not in archive.namelist() or hashlib.sha256(archive.read(member)).hexdigest() != sha256(source_path):
                    raise ValueError("Wheel/source drift detected; rebuild candidate before packaging")
    dependencies = tuple(Path(args.wheelhouse).glob("*.whl"))
    if not dependencies:
        raise ValueError("Offline dependency wheels are missing")
    output.mkdir(parents=True)
    for name, source in (("wheels", Path(args.wheels)), ("wheelhouse", Path(args.wheelhouse)), ("frontend", frontend)):
        shutil.copytree(source, output / name)
    deploy = output / "deploy"
    deploy.mkdir()
    # Allowlist release operations only; never include .env, user data or local secrets.
    for name in ("entrypoint.py", "preflight.py", "backup-native.sh", "verify-release.py", "requirements-runtime.lock"):
        shutil.copy2(root / "deploy/saas" / name, deploy / name)
    for name in ("application.env.example", "pg_hba.conf.example", "postgresql.conf.fragment"):
        shutil.copy2(root / "deploy/saas" / name, deploy / name)
    shutil.copy2(root / "docs/deployment/saas-pilot.md", deploy / "saas-pilot.md")
    shutil.copytree(bundle, output / "capability/reqpilot", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    source = root / "deploy/saas/systemd"
    shutil.copytree(source, deploy / "systemd")
    source_identity = {
        str(path.relative_to(root)): sha256(path)
        for folder in ("apps/server/src", "packages/engine/src", "apps/web/src", "packages/workbench-ui/src")
        for path in sorted((root / folder).rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    source_digest = hashlib.sha256(json.dumps(source_identity, sort_keys=True).encode()).hexdigest()
    files = {
        str(path.relative_to(output)): {"sha256": sha256(path), "bytes": path.stat().st_size}
        for path in sorted(output.rglob("*")) if path.is_file()
    }
    manifest = {
        "contract": "reqpilotx-private-pilot-release-v1",
        "revision_label": args.revision,
        "source_snapshot_sha256": source_digest,
        "source_commit": source_commit,
        "engine_core_identity": engine["core_identity"],
        "native_capability_bundle_sha256": capability_receipt["bundle_sha256"],
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "target": "linux-x86_64-python3.11-glibc2.28",
        "files": files,
        "release_gates": {"loopback_only": True, "auth_required": True, "postgres_required": True, "model_live_acceptance": False},
    }
    (output / "release.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"release": str(output), "source_snapshot_sha256": source_digest, "artifact_count": len(files)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
