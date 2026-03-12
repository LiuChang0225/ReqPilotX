#!/usr/bin/env python3
from __future__ import annotations

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


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(out.stdout.strip())


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
    root = repo_root()
    paths = staged_files(root)

    if not paths:
        return 0

    if not needs_context_update(paths):
        return 0

    if any(is_context_file(path) for path in paths):
        return 0

    msg = (
        "Context sync check failed.\n\n"
        "You staged code or core project changes, but no active context file was updated.\n"
        "Update at least one of:\n"
        f"- {root / 'docs' / 'active-context.md'}\n"
        f"- {root / 'prototype' / 'docs'}/*active-context.md\n\n"
        "Use the $context-sync skill to refresh the rolling context before commit.\n"
    )
    sys.stderr.write(msg)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
