from __future__ import annotations

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from .test_agent_assets import (
    APPROVED_ASSET_COUNT, APPROVED_RELEASE_GROUPS, ROOT, asset_registry, build_bundle,
)
from evals.agent_evals.contracts import bundle_integrity, digest
from evals.agent_evals.host_install import install


class FrozenAssetBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="reqpilotx-frozen-asset-guard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        build_bundle.build(ROOT, self.bundle)

    def assert_both_reject(self):
        for verify in (asset_registry.verify_frozen_bundle, bundle_integrity):
            with self.subTest(verifier=verify.__name__):
                with self.assertRaises(ValueError):
                    verify(self.bundle)

    def test_approved_assets_and_host_install_markers_remain_valid(self):
        receipt = asset_registry.verify_frozen_bundle(self.bundle)
        declared = json.loads((self.bundle / "build-receipt.json").read_text())["agent_assets"]["groups"]
        self.assertEqual(declared, APPROVED_RELEASE_GROUPS)
        self.assertEqual(receipt["file_count"], APPROVED_ASSET_COUNT)
        result = install(self.bundle, self.root / "installed", "antigravity", self.root / "receipts")
        self.assertEqual(bundle_integrity(self.root / "installed")["bundle_sha256"], result["bundle_sha256"])
        self.assertEqual(asset_registry.verify_frozen_bundle(self.root / "installed")["file_count"], APPROVED_ASSET_COUNT)

    def test_extra_skill_and_vendor_file_are_rejected_by_both_consumers(self):
        for relative in ("skills/review-extra/SKILL.md", "capabilities/pdf/review-extra.txt"):
            with self.subTest(relative=relative):
                path = self.bundle / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("inert synthetic marker\n", encoding="utf-8")
                self.assert_both_reject()
                path.unlink()
        self.assertEqual(asset_registry.verify_frozen_bundle(self.bundle)["file_count"], APPROVED_ASSET_COUNT)

    def test_host_install_rejects_extra_skill_before_creating_target(self):
        path = self.bundle / "skills/review-extra/SKILL.md"
        path.parent.mkdir()
        path.write_text("inert test; never execute\n")
        target = self.root / "install-must-not-exist"
        with self.assertRaises(ValueError):
            install(self.bundle, target, "codex", self.root / "receipts")
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob("*.staging-*")))

    def test_extra_symlink_is_rejected_even_with_unchanged_declared_files(self):
        (self.bundle / "skills/review-link").symlink_to(self.bundle / "skills/reqpilot")
        self.assert_both_reject()

    def test_asset_root_and_parent_symlinks_are_rejected(self):
        for relative in ("skills", "capabilities"):
            with self.subTest(relative=relative):
                path = self.bundle / relative
                moved = self.root / (relative + "-outside")
                path.rename(moved)
                path.symlink_to(moved, target_is_directory=True)
                try:
                    self.assert_both_reject()
                finally:
                    path.unlink()
                    moved.rename(path)

    def test_linked_bundle_root_is_rejected(self):
        link = self.root / "bundle-link"
        link.symlink_to(self.bundle, target_is_directory=True)
        for verify in (asset_registry.verify_frozen_bundle, bundle_integrity):
            with self.assertRaises(ValueError):
                verify(link)

    def test_special_asset_member_is_rejected_without_opening_it(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation is unavailable on this platform")
        os.mkfifo(self.bundle / "skills/review-fifo")
        self.assert_both_reject()

    def test_missing_or_duplicate_declarations_fail_closed(self):
        path = self.bundle / "build-receipt.json"
        original = json.loads(path.read_text())
        for duplicate in (False, True):
            value = json.loads(json.dumps(original))
            files = value["agent_assets"]["groups"][0]["files"]
            if duplicate:
                files.append(dict(files[0]))
            else:
                files.pop()
            value["bundle_sha256"] = digest({k: v for k, v in value.items() if k != "bundle_sha256"})
            path.write_text(json.dumps(value))
            self.assert_both_reject()
        path.write_text(json.dumps(original))

    def test_pre_r5_receipt_stays_supported_but_extra_skills_do_not(self):
        path = self.bundle / "build-receipt.json"
        receipt = json.loads(path.read_text())
        del receipt["agent_assets"]
        receipt["bundle_sha256"] = digest({k: v for k, v in receipt.items() if k != "bundle_sha256"})
        path.write_text(json.dumps(receipt))
        self.assertEqual(bundle_integrity(self.bundle)["bundle_sha256"], receipt["bundle_sha256"])
        extra = self.bundle / "skills/review-extra.md"
        extra.write_text("inert legacy fixture marker\n")
        with self.assertRaisesRegex(ValueError, "inventory"):
            bundle_integrity(self.bundle)


class CommittedAssetInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="reqpilotx-asset-input-git-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.source = self.repo / "agent-assets/source"
        self.projection = self.repo / "projection"
        for directory in (self.source, self.projection, self.repo / "agent-assets/scripts", self.repo / "plugins/reqpilot/scripts"):
            directory.mkdir(parents=True, exist_ok=True)
        (self.source / "SKILL.md").write_text("canonical fixture\n")
        shutil.copyfile(self.source / "SKILL.md", self.projection / "SKILL.md")
        shutil.copyfile(ROOT / "agent-assets/scripts/asset_registry.py", self.repo / "agent-assets/scripts/asset_registry.py")
        (self.repo / "plugins/reqpilot/scripts/build_bundle.py").write_text("# inert fixture\n")
        self.group = {
            "id": "fixture", "classification": "CURRENT", "asset_kind": "TEST",
            "source": "agent-assets/source", "tree_sha256": "", "consumers": ["test"],
            "target": "test", "compatibility": "test", "verification": "test",
            "retirement_condition": "test", "projections": [{"path": "projection", "mode": "mirror"}],
            "bundle_root": "skills",
        }
        self.write_catalog()
        self.git("init", "-q")
        self.git("config", "user.email", "review@example.invalid")
        self.git("config", "user.name", "R5 synthetic regression")
        self.git("config", "core.filemode", "false")

    def git(self, *args):
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(self.repo), *args],
                                check=True, capture_output=True, text=True)
        return result.stdout

    def write_catalog(self):
        self.group["tree_sha256"] = asset_registry.tree_receipt(self.source)["tree_sha256"]
        (self.repo / "agent-assets/catalog.json").write_text(json.dumps({
            "schema_version": asset_registry.SCHEMA_VERSION, "groups": [self.group],
        }))

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic asset inputs")
        return self.git("rev-parse", "HEAD").strip()

    def test_committed_unicode_and_space_names_are_valid(self):
        for directory in (self.source, self.projection):
            (directory / "说明 test.md").write_text("ordinary committed fixture\n")
        self.write_catalog()
        commit = self.commit()
        asset_registry.assert_release_inputs_match_commit(self.repo, commit)

    def test_ignored_canonical_input_is_rejected_despite_clean_status_and_matching_catalog(self):
        (self.repo / ".gitignore").write_text("*.review-ignored\n")
        for directory in (self.source, self.projection):
            (directory / "marker.review-ignored").write_text("inert not-a-secret\n")
        self.write_catalog()
        commit = self.commit()
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")
        asset_registry.validate_repository_assets(self.repo)
        with self.assertRaisesRegex(ValueError, "uncommitted_or_ignored"):
            asset_registry.assert_release_inputs_match_commit(self.repo, commit)
        self.assertTrue((self.source / "marker.review-ignored").is_file())

    def test_ignored_projection_extra_is_rejected_even_for_subset_projection(self):
        (self.repo / ".gitignore").write_text("*.review-ignored\n")
        self.group["projections"][0]["mode"] = "subset"
        (self.projection / "marker.review-ignored").write_text("inert projection file\n")
        self.write_catalog()
        commit = self.commit()
        asset_registry.validate_repository_assets(self.repo)
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(ValueError, "inventory"):
            asset_registry.assert_release_inputs_match_commit(self.repo, commit)

    def test_assume_unchanged_content_is_compared_with_the_commit(self):
        commit = self.commit()
        relative = "agent-assets/source/SKILL.md"
        self.git("update-index", "--assume-unchanged", relative)
        (self.repo / relative).write_text("changed and hidden from status\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(ValueError, "differs from the commit"):
            asset_registry.assert_release_inputs_match_commit(self.repo, commit)

    def test_executable_mode_is_checked_when_git_filemode_is_disabled(self):
        commit = self.commit()
        source = self.source / "SKILL.md"
        source.chmod(source.stat().st_mode | 0o100)
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(ValueError, "mode differs"):
            asset_registry.assert_release_inputs_match_commit(self.repo, commit)

    def test_committed_symlink_mode_is_rejected_even_if_checkout_contains_regular_bytes(self):
        (self.source / "ref").symlink_to("SKILL.md")
        commit = self.commit()
        relative = "agent-assets/source/ref"
        self.git("update-index", "--assume-unchanged", relative)
        (self.repo / relative).unlink()
        (self.repo / relative).write_text("SKILL.md")
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(ValueError, "Committed asset input is not a regular file"):
            asset_registry.assert_release_inputs_match_commit(self.repo, commit)


if __name__ == "__main__":
    unittest.main()
