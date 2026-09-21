#!/usr/bin/env python3
"""Build the existing portable ReqPilotX core as an internal wheel and sdist."""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import portable_core
from portable_core import (
    ENGINE_BASE_VERSION,
    SCRIPT_ROOTS,
    build_core_manifest,
    canonical_digest,
    digest,
    find_repo,
    select_core,
)


ENGINE_PROJECT = Path(__file__).resolve().parents[3] / "packages/engine"
MIN_UV_VERSION = (0, 10, 2)
BUILD_INPUT_FILES = (
    "prototype/requirements.txt",
    "plugins/reqpilot/scripts/build_engine.py",
    "plugins/reqpilot/scripts/portable_core.py",
    "packages/engine/pyproject.toml",
    "packages/engine/README.md",
)
# Check the source tree, not just the current closure: a deleted module or
# initializer can disappear from that closure. Unrelated docs/UI may be dirty.
RELEASE_INPUT_SCOPES = (
    "prototype/app",
    *(f"prototype/scripts/{name}" for name in SCRIPT_ROOTS),
    *BUILD_INPUT_FILES,
)


def git_value(repo: Path, expression: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", expression],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def require_uv_frontend() -> tuple[str, str]:
    uv = shutil.which("uv")
    minimum = ".".join(map(str, MIN_UV_VERSION))
    if uv is None:
        raise ValueError(f"Engine build requires prepared uv >= {minimum}; CI uses uv 0.12.13")
    try:
        result = subprocess.run([uv, "--version"], check=True, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Cannot inspect uv at {uv}; prepare uv >= {minimum}") from exc
    version = result.stdout.strip()
    match = re.match(r"^uv (\d+)\.(\d+)\.(\d+)(?:\s|$)", version)
    if match is None or tuple(map(int, match.groups())) < MIN_UV_VERSION:
        raise ValueError(f"Unsupported uv version {version!r} at {uv}; requires stable uv >= {minimum}; CI uses 0.12.13")
    return uv, version


def assert_release_sources_clean(repo: Path, commit: str) -> None:
    if git_value(repo, "HEAD") != commit:
        raise ValueError("Release HEAD changed while preparing the engine; retry from a fixed checkout")
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), "status", "--porcelain=v1",
         "--untracked-files=all", "--", *RELEASE_INPUT_SCOPES],
        check=True, capture_output=True, text=True,
    )
    if result.stdout:
        raise ValueError("Release inputs must be committed and clean; preserve and commit them in an isolated worktree before building:\n" + result.stdout)


def committed_inputs(repo: Path, commit: str, selection) -> dict[str, bytes]:
    paths = sorted({*BUILD_INPUT_FILES, *(f"prototype/{member.source_relative}" for member in selection.members)})
    if any("\n" in path or "\r" in path for path in paths):
        raise ValueError("Release input paths cannot contain line breaks")
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input="".join(f"{commit}:{path}\n" for path in paths).encode("utf-8"),
        check=True, capture_output=True,
    )
    stream = io.BytesIO(result.stdout)
    contents = {}
    for path in paths:
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError(f"Release input is not a committed file: {path}")
        contents[path] = stream.read(int(header[2]))
        if stream.read(1) != b"\n":
            raise ValueError(f"Cannot read committed release input: {path}")
    for member in selection.members:
        original = contents[f"prototype/{member.source_relative}"]
        if portable_core.digest_bytes(original) != member.upstream_sha256 or (not member.namespace_initializer and original != member.content):
            raise ValueError(f"Selected source differs from release commit: {member.source_relative}")
    # Also reject hidden/assume-unchanged edits and a builder loaded from a
    # different checkout. Stage packaging metadata from the verified Git blobs.
    for path in BUILD_INPUT_FILES:
        source = repo / path
        if source.is_symlink() or source.read_bytes() != contents[path]:
            raise ValueError(f"Build input differs from release commit: {path}")
    for path, loaded in (
        ("plugins/reqpilot/scripts/build_engine.py", Path(__file__)),
        ("plugins/reqpilot/scripts/portable_core.py", Path(portable_core.__file__)),
    ):
        if loaded.read_bytes() != contents[path]:
            raise ValueError(f"Loaded build tool differs from release commit: {path}")
    return contents


def pep440_version(commit: str, identity_seed: str) -> str:
    return f"{ENGINE_BASE_VERSION}+g{commit[:12]}.i{identity_seed[:12]}"


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_pyproject(version: str, requirements: tuple[str, ...], *, template: str | None = None) -> str:
    if template is None:
        template = (ENGINE_PROJECT / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = "[\n" + "".join(f"  {toml_string(item)},\n" for item in requirements) + "]"
    return template.replace('version = "0.0.0"', f"version = {toml_string(version)}").replace("dependencies = []", f"dependencies = {dependencies}")


def prepare_stage(repo: Path, stage: Path) -> dict:
    repo = find_repo(repo)
    commit = git_value(repo, "HEAD")
    tree = git_value(repo, f"{commit}^{{tree}}")
    assert_release_sources_clean(repo, commit)
    selection = select_core(repo)
    inputs = committed_inputs(repo, commit, selection)
    assert_release_sources_clean(repo, commit)
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
    (stage / "pyproject.toml").write_text(render_pyproject(
        version, selection.requirements, template=inputs["packages/engine/pyproject.toml"].decode("utf-8"),
    ), encoding="utf-8")
    (stage / "README.md").write_bytes(inputs["packages/engine/README.md"])
    assert_release_sources_clean(repo, commit)
    return manifest


def build(repo: Path, output: Path, *, offline: bool = False) -> dict:
    repo = find_repo(repo)
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("Engine output must not exist; each release build gets a new immutable directory")
    uv, uv_version = require_uv_frontend()
    assert_release_sources_clean(repo, git_value(repo, "HEAD"))
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
                "frontend": uv_version,
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
