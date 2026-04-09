#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


CODE_PREFIXES = (
    "prototype/app/",
    "prototype/tests/",
    "prototype/scripts/",
    "prototype/README.md",
    "docs/项目.md",
    "docs/codex-git-workflow.md",
    "AGENTS.md",
)


def run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _resolve_git_path(raw_path: str, *, cwd: Path) -> Path:
    path = Path(str(raw_path or "").strip())
    if not path.is_absolute():
        path = (cwd / path).resolve()
    return path


def worktree_root(cwd: Path | None = None) -> Path:
    base = (cwd or Path.cwd()).resolve()
    return _resolve_git_path(run_git(["rev-parse", "--show-toplevel"], base), cwd=base)


def repo_root(cwd: Path | None = None) -> Path:
    base = (cwd or Path.cwd()).resolve()
    common_dir = _resolve_git_path(run_git(["rev-parse", "--git-common-dir"], base), cwd=base)
    return common_dir.parent if common_dir.name == ".git" else common_dir


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check active-context sync before commit.")
    parser.add_argument("--worktree-root", default="", help="Override current worktree root.")
    parser.add_argument("--repo-root", default="", help="Override common repository root.")
    return parser.parse_args()


def _context_targets(repo_root_path: Path, worktree_root_path: Path) -> list[str]:
    targets = [str(worktree_root_path / "docs" / "active-context.md")]
    track_target = _track_context_target(repo_root_path=repo_root_path, worktree_root_path=worktree_root_path)
    if track_target:
        targets.append(str(worktree_root_path / track_target))
        return targets
    branch_context_dir = worktree_root_path / "prototype" / "docs"
    branch_targets = sorted(branch_context_dir.glob("*active-context.md")) if branch_context_dir.exists() else []
    if branch_targets:
        targets.extend(str(path) for path in branch_targets)
    else:
        targets.append(str(branch_context_dir / "*active-context.md"))
    return targets


def _track_context_target(*, repo_root_path: Path, worktree_root_path: Path) -> str:
    registry_path = repo_root_path / "prototype" / "eval" / "progress" / "track_registry.json"
    if not registry_path.exists():
        return ""
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    current_branch = run_git(["branch", "--show-current"], worktree_root_path).strip()
    current_root = worktree_root_path.resolve()
    for track in payload.get("tracks", []):
        if not isinstance(track, dict):
            continue
        branch_name = str(track.get("branch_name") or "").strip()
        worktree_path = Path(str(track.get("worktree_path") or "")).resolve()
        branch_context_doc = str(track.get("branch_context_doc") or "").strip()
        if not branch_context_doc:
            continue
        if branch_name and branch_name == current_branch:
            return branch_context_doc
        if worktree_path == current_root:
            return branch_context_doc
    return ""


def staged_files(root: Path) -> list[str]:
    out = run_git(["diff", "--cached", "--name-only"], root)
    return [line.strip() for line in out.splitlines() if line.strip()]


def is_context_file(path: str) -> bool:
    return path == "docs/active-context.md" or (
        path.startswith("prototype/docs/") and path.endswith("active-context.md")
    )


def needs_context_update(paths: list[str]) -> bool:
    for path in paths:
        if is_context_file(path):
            continue
        if path.startswith(CODE_PREFIXES):
            return True
    return False


def main() -> int:
    args = _parse_args()
    cwd = Path.cwd().resolve()
    current_worktree_root = Path(args.worktree_root).resolve() if args.worktree_root else worktree_root(cwd)
    current_repo_root = Path(args.repo_root).resolve() if args.repo_root else repo_root(cwd)
    paths = staged_files(current_worktree_root)

    if not paths:
        return 0

    if not needs_context_update(paths):
        return 0

    required_context_paths = {"docs/active-context.md"}
    track_target = _track_context_target(repo_root_path=current_repo_root, worktree_root_path=current_worktree_root)
    if track_target:
        required_context_paths.add(track_target)

    staged_context_paths = {path for path in paths if is_context_file(path)}

    if track_target:
        if required_context_paths.issubset(staged_context_paths):
            return 0
    elif any(is_context_file(path) for path in paths):
        return 0

    target_lines = "\n".join(f"- {target}" for target in _context_targets(current_repo_root, current_worktree_root))
    msg = (
        "Context sync check failed.\n\n"
        "You staged code or core project changes, but no active context file was updated.\n"
        "Update at least one of:\n"
        f"{target_lines}\n\n"
        "Use the $context-sync skill to refresh the rolling context before commit.\n"
    )
    sys.stderr.write(msg)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
