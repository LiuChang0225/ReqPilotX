from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.release_preflight import (
    PreflightError,
    QdrantArtifact,
    run_preflight,
    validate_bundled_qdrant,
)


def resolve_venv_pyinstaller(
    project_dir: Path,
    *,
    platform_name: str | None = None,
) -> Path:
    """Return the project-pinned PyInstaller executable or fail closed."""
    platform_name = platform_name or sys.platform
    if platform_name == "win32":
        executable = project_dir / ".venv" / "Scripts" / "pyinstaller.exe"
    else:
        executable = project_dir / ".venv" / "bin" / "pyinstaller"

    if not executable.is_file():
        raise PreflightError(
            f"project PyInstaller is missing: {executable}. "
            "Install build dependencies into apps/desktop/.venv; global fallback is disabled."
        )
    if platform_name != "win32" and not os.access(executable, os.X_OK):
        raise PreflightError(f"project PyInstaller is not executable: {executable}")
    return executable.resolve()


def build_pyinstaller_command(
    pyinstaller: Path,
    spec_path: Path,
    *,
    with_qdrant: bool = False,
    qdrant_manifest: Path | None = None,
) -> list[str]:
    """Build the minimal command; all packaging policy lives in the spec file."""
    command = [
        str(pyinstaller),
        "--noconfirm",
        "--clean",
        str(spec_path),
    ]
    if with_qdrant:
        if qdrant_manifest is None:
            raise PreflightError("--with-qdrant requires a validated manifest")
        command.extend(
            [
                "--",
                "--with-qdrant",
                "--qdrant-manifest",
                str(qdrant_manifest),
            ]
        )
    return command


def _qdrant_from_preflight(report: dict[str, object]) -> QdrantArtifact | None:
    if not report.get("qdrant_embedded"):
        return None
    return QdrantArtifact(
        path=Path(str(report["qdrant_artifact"])),
        platform=str(report["qdrant_platform"]),
        arch=str(report["qdrant_arch"]),
        version=str(report["qdrant_version"]),
        size_bytes=int(report["qdrant_size_bytes"]),
        sha256=str(report["qdrant_sha256"]),
        bundle_destination=str(report["qdrant_bundle_destination"]),
    )


def require_clean_source_for_build(
    *,
    preflight_only: bool,
    explicitly_required: bool,
) -> bool:
    return bool(explicitly_required or not preflight_only)


def finalize_pyinstaller_output(
    dist_dir: Path,
    *,
    platform_name: str | None = None,
) -> Path | None:
    """Remove PyInstaller's transient macOS COLLECT after a complete BUNDLE.

    A macOS spec that wraps ``COLLECT`` in ``BUNDLE`` leaves both
    ``dist/ReqPilotX`` and ``dist/ReqPilotX.app``.  The former is a build-stage
    directory, not a second supported artifact, and keeping it would make the
    canonical artifact contract ambiguous.  Cleanup is deliberately bounded:
    it runs only on macOS, only after both expected layouts exist, and refuses
    symlinks or an incomplete stage rather than deleting an unknown path.
    """

    target = str(platform_name or sys.platform).strip().lower()
    if target not in {"darwin", "mac", "macos"}:
        return None

    dist_root = Path(dist_dir).resolve()
    bundle = dist_root / "ReqPilotX.app"
    transient = dist_root / "ReqPilotX"
    bundle_entrypoint = bundle / "Contents" / "MacOS" / "ReqPilotX"
    transient_entrypoint = transient / "ReqPilotX"
    transient_runtime = transient / "_internal"

    if bundle.is_symlink() or not bundle.is_dir() or not bundle_entrypoint.is_file():
        raise PreflightError(
            "macOS bundle is incomplete; transient COLLECT output was preserved"
        )
    if transient.is_symlink() or not transient.is_dir():
        raise PreflightError(
            "macOS transient COLLECT output is missing or unsafe"
        )
    if (
        transient_entrypoint.is_symlink()
        or not transient_entrypoint.is_file()
        or transient_runtime.is_symlink()
        or not transient_runtime.is_dir()
    ):
        raise PreflightError(
            "macOS transient COLLECT output is incomplete; refusing cleanup"
        )

    shutil.rmtree(transient)
    if transient.exists() or transient.is_symlink():
        raise PreflightError("macOS transient COLLECT output could not be removed")
    return transient


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight and package ReqPilotX from the canonical PyInstaller spec."
    )
    parser.add_argument(
        "--with-qdrant",
        action="store_true",
        help="Embed the platform-specific Qdrant binary after manifest verification.",
    )
    parser.add_argument(
        "--qdrant-manifest",
        type=Path,
        help="Committed SHA-256 manifest (default: prototype/release/qdrant-artifacts.json).",
    )
    parser.add_argument(
        "--require-python",
        metavar="MAJOR.MINOR",
        help="Fail unless the current interpreter matches this exact major/minor version.",
    )
    parser.add_argument(
        "--require-clean-tree",
        action="store_true",
        help="Fail when the containing Git worktree has tracked or untracked changes.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run validation without invoking PyInstaller.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    project_dir = Path(__file__).resolve().parent
    manifest = (
        args.qdrant_manifest.expanduser().resolve()
        if args.qdrant_manifest
        else (project_dir / "release" / "qdrant-artifacts.json").resolve()
    )

    print("[ReqPilotX Build] Running release source preflight...")
    if not args.with_qdrant:
        print("[ReqPilotX Build] Qdrant is NOT embedded in the default artifact.")

    try:
        report = run_preflight(
            project_dir,
            with_qdrant=args.with_qdrant,
            qdrant_manifest=manifest if args.with_qdrant else None,
            require_python=args.require_python,
            require_clean_tree=require_clean_source_for_build(
                preflight_only=args.preflight_only,
                explicitly_required=args.require_clean_tree,
            ),
        )
        qdrant = _qdrant_from_preflight(report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))

        if args.preflight_only:
            print("[ReqPilotX Build] Preflight passed; build was not started.")
            return 0

        pyinstaller = resolve_venv_pyinstaller(project_dir)
        command = build_pyinstaller_command(
            pyinstaller,
            project_dir / "ReqPilotX.spec",
            with_qdrant=args.with_qdrant,
            qdrant_manifest=manifest if args.with_qdrant else None,
        )
        print(f"[ReqPilotX Build] Executing: {' '.join(command)}")
        subprocess.run(command, cwd=project_dir, check=True)
        removed_transient = finalize_pyinstaller_output(
            project_dir / "dist",
            platform_name=sys.platform,
        )
        if removed_transient is not None:
            print(
                "[ReqPilotX Build] Removed verified transient macOS COLLECT: "
                f"{removed_transient}"
            )
        if qdrant is not None:
            bundled = validate_bundled_qdrant(project_dir / "dist", qdrant)
            print(f"[ReqPilotX Build] Bundled Qdrant verified: {bundled}")
        from scripts.artifact_self_test import (
            run_artifact_self_test,
            write_artifact_manifest,
        )

        artifact_manifest = write_artifact_manifest(
            project_dir / "dist",
            project_dir=project_dir,
            platform_name=sys.platform,
            arch=platform.machine(),
            qdrant=qdrant,
        )
        artifact_report = run_artifact_self_test(
            project_dir / "dist",
            manifest_path=artifact_manifest,
        )
        print(json.dumps(artifact_report, ensure_ascii=False, indent=2, sort_keys=True))
    except PreflightError as exc:
        print(f"[ReqPilotX Build] PREFLIGHT FAILED: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(
            f"[ReqPilotX Build] PyInstaller failed with exit code {exc.returncode}",
            file=sys.stderr,
        )
        return exc.returncode

    print("[ReqPilotX Build] Packaging completed successfully.")
    print(f"[ReqPilotX Build] Output root: {project_dir / 'dist'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
