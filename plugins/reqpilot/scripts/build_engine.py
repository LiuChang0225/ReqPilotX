#!/usr/bin/env python3
"""Build the existing portable ReqPilotX core as an internal wheel and sdist."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from portable_core import (
    ENGINE_BASE_VERSION,
    build_core_manifest,
    canonical_digest,
    digest,
    find_repo,
    select_core,
)


ENGINE_PROJECT = Path(__file__).resolve().parents[3] / "packages/engine"


def git_value(repo: Path, expression: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", expression],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def pep440_version(commit: str, identity_seed: str) -> str:
    return f"{ENGINE_BASE_VERSION}+g{commit[:12]}.i{identity_seed[:12]}"


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_pyproject(version: str, requirements: tuple[str, ...]) -> str:
    template = (ENGINE_PROJECT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = "[\n" + "".join(f"  {toml_string(item)},\n" for item in requirements) + "]"
    return template.replace('version = "0.0.0"', f"version = {toml_string(version)}").replace("dependencies = []", f"dependencies = {dependencies}")


def prepare_stage(repo: Path, stage: Path) -> dict:
    selection = select_core(repo)
    commit = git_value(repo, "HEAD")
    tree = git_value(repo, "HEAD^{tree}")
    seed = {
        "source_commit": commit,
        "source_tree": tree,
        "members": [(member.source_relative, member.packaged_sha256) for member in selection.members],
        "requirements": selection.requirements,
    }
    version = pep440_version(commit, canonical_digest(seed))
    manifest = build_core_manifest(selection, version=version, source_commit=commit, source_tree=tree)
    source_root = stage / "src"
    for member in selection.members:
        destination = source_root / member.wheel_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(member.content)
    metadata_package = source_root / "reqpilotx_engine"
    metadata_package.mkdir(parents=True, exist_ok=True)
    (metadata_package / "__init__.py").write_text(
        '"""Installed identity metadata for the transitional ReqPilotX engine package."""\n'
        "from importlib.resources import files\n"
        "import json\n\n"
        "def core_manifest():\n"
        "    return json.loads(files(__package__).joinpath('core-manifest.json').read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    (metadata_package / "core-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tools_package = source_root / "reqpilotx_engine_tools"
    tools_package.mkdir(parents=True, exist_ok=True)
    (tools_package / "__init__.py").write_text('"""Packaged transitional command modules for ReqPilotX engine."""\n', encoding="utf-8")
    (stage / "pyproject.toml").write_text(render_pyproject(version, selection.requirements), encoding="utf-8")
    shutil.copyfile(ENGINE_PROJECT / "README.md", stage / "README.md")
    return manifest


def build(repo: Path, output: Path, *, offline: bool = False) -> dict:
    repo = find_repo(repo)
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("Engine output must not exist; each release build gets a new immutable directory")
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("Engine build requires the prepared uv build frontend; no dependency is installed at runtime")
    output.mkdir(parents=True)
    try:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-engine-stage-") as temporary:
            stage = Path(temporary) / "reqpilotx-engine"
            stage.mkdir()
            manifest = prepare_stage(repo, stage)
            command = [
                uv, "build", "--no-sources", "--no-python-downloads", "--no-create-gitignore", "--no-build-logs",
                "--python", sys.executable, "--out-dir", str(output), str(stage),
            ]
            if offline:
                command.insert(2, "--offline")
            subprocess.run(command, check=True)
        wheels = sorted(output.glob("*.whl"))
        sdists = sorted(output.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("Engine build must create exactly one wheel and one sdist")
        receipt = {
            "schema_version": "reqpilotx-engine-build/v1",
            "distribution": manifest["distribution"],
            "version": manifest["version"],
            "core_identity": manifest["core_identity"],
            "source_commit": manifest["source_commit"],
            "source_tree": manifest["source_tree"],
            "runtime_file_count": len(manifest["runtime_files"]),
            "requirements": manifest["requirements"],
            "build_environment": {
                "python": sys.version.split()[0],
                "frontend": subprocess.run([uv, "--version"], check=True, capture_output=True, text=True).stdout.strip(),
            },
            "artifacts": [
                {"filename": path.name, "sha256": digest(path), "bytes": path.stat().st_size, "kind": "wheel" if path.suffix == ".whl" else "sdist"}
                for path in (wheels[0], sdists[0])
            ],
            "source_checkout_required_at_runtime": False,
            "customer_data_included": False,
        }
        receipt["receipt_sha256"] = canonical_digest(receipt)
        (output / "engine-build-receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return receipt
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, help="ReqPilotX checkout; used only at build time")
    parser.add_argument("--output", type=Path, required=True, help="New output directory for wheel, sdist and receipt")
    parser.add_argument("--offline", action="store_true", help="Require all prepared build dependencies to be available in the uv cache")
    args = parser.parse_args()
    try:
        print(json.dumps(build(find_repo(args.repo), args.output, offline=args.offline), ensure_ascii=False))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "BUILD_FAILED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
