from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


_SAFE_REVISION = re.compile(r"^[0-9a-f]{64}$")
_SAFE_INSTANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ROLE_MARKER = re.compile(r"(?:^|\s)REQPILOT_PROCESS_ROLE=(api|worker)(?:\s|$)")
_PORT_MARKER = re.compile(
    r"(?:^|\s)REQPILOT_LOCAL_RUNTIME_PORT=(\d+)(?:\s|$)"
)
_INSTANCE_MARKER = re.compile(
    r"(?:^|\s)REQPILOT_LOCAL_RUNTIME_INSTANCE_ID=([A-Za-z0-9_.:-]+)(?:\s|$)"
)
_REVISION_MARKER = re.compile(
    r"(?:^|\s)REQPILOT_RUNTIME_SOURCE_REVISION=([0-9a-f]{64})(?:\s|$)"
)
_WORKER_COUNT_MARKER = re.compile(
    r"(?:^|\s)REQPILOT_LOCAL_WORKER_COUNT=([12])(?:\s|$)"
)


def _source_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for directory, suffixes in (
        (root / "app", {".py"}),
        (root.parent / "packages" / "engine" / "src", {".py"}),
        (root.parent / "apps" / "server" / "src", {".py", ".json", ".sql"}),
        (root / "migrations" / "postgresql", {".sql"}),
        (
            root.parent / "packages/workbench-ui/src",
            {".css", ".js", ".jsx", ".json", ".scss", ".svg", ".ts", ".tsx"},
        ),
        (root.parent / "apps/web/src", {".ts", ".tsx"}),
    ):
        if directory.exists():
            candidates.extend(
                path
                for path in directory.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix.casefold() in suffixes
            )
    for relative in (
        "requirements.txt",
        "frontend/index.html",
        "frontend/package.json",
        "frontend/package-lock.json",
        "frontend/tsconfig.json",
        "frontend/vite.config.ts",
        "scripts/local_runtime_identity.py",
        "scripts/local_worker_guard.py",
        "start.sh",
        "stop.sh",
        "status.sh",
    ):
        path = root / relative
        if path.is_file():
            candidates.append(path)
    for relative in ("package.json", "package-lock.json", "tsconfig.json",
                     "apps/web/index.html", "apps/web/package.json", "apps/web/vite.config.ts",
                     "packages/workbench-ui/package.json"):
        path = root.parent / relative
        if path.is_file():
            candidates.append(path)
    return sorted(set(candidates), key=lambda path: path.resolve().as_posix())


def calculate_source_revision(root: Path) -> str:
    resolved = root.resolve(strict=True)
    repository_root = (
        resolved.parent
        if (resolved.parent / "packages/engine/src").is_dir()
        else resolved
    )
    digest = hashlib.sha256()
    for path in _source_files(resolved):
        relative = path.relative_to(repository_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _process_command(pid: int) -> str:
    if pid <= 0:
        return ""
    result = subprocess.run(
        ("ps", "eww", "-p", str(pid), "-o", "command="),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def process_matches_runtime(
    pid: int,
    *,
    role: str,
    port: int,
    instance_id: str,
    source_revision: str,
    worker_count: int,
) -> bool:
    command = _process_command(pid)
    role_match = _ROLE_MARKER.search(command)
    port_match = _PORT_MARKER.search(command)
    instance_match = _INSTANCE_MARKER.search(command)
    revision_match = _REVISION_MARKER.search(command)
    worker_count_match = _WORKER_COUNT_MARKER.search(command)
    return bool(
        role_match
        and role_match.group(1) == role
        and port_match
        and int(port_match.group(1)) == port
        and instance_match
        and instance_match.group(1) == instance_id
        and revision_match
        and revision_match.group(1) == source_revision
        and worker_count_match
        and int(worker_count_match.group(1)) == worker_count
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and verify a secret-free ReqPilotX local runtime identity."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    revision = subparsers.add_parser("revision")
    revision.add_argument("--root", type=Path, required=True)
    matches = subparsers.add_parser("matches")
    matches.add_argument("--pid", type=int, required=True)
    matches.add_argument("--role", choices=("api", "worker"), required=True)
    matches.add_argument("--port", type=int, required=True)
    matches.add_argument("--instance-id", required=True)
    matches.add_argument("--source-revision", required=True)
    matches.add_argument("--worker-count", type=int, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "revision":
        print(calculate_source_revision(args.root))
        return 0
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    if not _SAFE_INSTANCE.fullmatch(args.instance_id):
        raise SystemExit("instance-id is not a safe runtime token")
    if not _SAFE_REVISION.fullmatch(args.source_revision):
        raise SystemExit("source-revision must be a lowercase SHA-256")
    if args.worker_count not in {1, 2}:
        raise SystemExit("worker-count must be 1 or 2")
    return (
        0
        if process_matches_runtime(
            args.pid,
            role=args.role,
            port=args.port,
            instance_id=args.instance_id,
            source_revision=args.source_revision,
            worker_count=args.worker_count,
        )
        else 1
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
