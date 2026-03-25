from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_MODULE_PATH = Path(__file__).resolve().parent / "check_context_sync.py"
_SPEC = importlib.util.spec_from_file_location("check_context_sync", _MODULE_PATH)
assert _SPEC and _SPEC.loader
check_context_sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_context_sync)


class CheckContextSyncTests(unittest.TestCase):
    def test_repo_root_uses_git_common_dir_parent_for_linked_worktree(self) -> None:
        worktree_root = Path("/tmp/repo/worktrees/feature").resolve()
        common_root = Path("/tmp/repo").resolve()

        def _fake_run_git(args: list[str], cwd: Path) -> str:
            self.assertEqual(cwd, worktree_root)
            if args == ["rev-parse", "--git-common-dir"]:
                return str(common_root / ".git")
            raise AssertionError(args)

        with patch.object(check_context_sync, "run_git", side_effect=_fake_run_git):
            resolved = check_context_sync.repo_root(worktree_root)

        self.assertEqual(resolved, common_root)

    def test_context_targets_include_common_root_and_branch_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            worktree_root = repo_root / "worktrees" / "feature"
            (repo_root / "docs").mkdir(parents=True)
            branch_context = worktree_root / "prototype" / "docs" / "feature-active-context.md"
            branch_context.parent.mkdir(parents=True)
            branch_context.write_text("# context", encoding="utf-8")

            targets = check_context_sync._context_targets(repo_root, worktree_root)

        self.assertEqual(targets[0], str(repo_root / "docs" / "active-context.md"))
        self.assertIn(str(branch_context), targets)


if __name__ == "__main__":
    unittest.main()
