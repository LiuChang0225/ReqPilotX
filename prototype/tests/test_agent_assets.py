from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from .runtime_isolation import activate_test_runtime


activate_test_runtime()

from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import (  # noqa: E402
    LifecycleProbeError,
)
from reqpilotx_server.adapters.agents.codex_app_server.pdf_skill_canary import (  # noqa: E402
    _copy_runtime_skills,
)
from reqpilotx_server.adapters.agents.portable_kit import (  # noqa: E402
    load_portable_agent_kit,
)


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SCRIPTS = ROOT / "plugins/reqpilot/scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

import build_bundle  # noqa: E402


def _load_registry():
    path = ROOT / "agent-assets/scripts/asset_registry.py"
    spec = importlib.util.spec_from_file_location("test_agent_asset_registry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


asset_registry = _load_registry()
APPROVED_RELEASE_GROUPS = json.loads(
    (ROOT / "prototype/tests/fixtures/agent-assets/approved-release.json").read_text(encoding="utf-8")
)["groups"]
APPROVED_ASSET_COUNT = sum(len(group["files"]) for group in APPROVED_RELEASE_GROUPS)


class AgentAssetRegistryTests(unittest.TestCase):
    def test_current_sources_projections_vendor_and_compatibility_kit_validate(self) -> None:
        receipt = asset_registry.validate_repository_assets(ROOT)
        by_id = {group["id"]: group for group in receipt["groups"]}

        self.assertEqual(
            asset_registry.release_asset_manifest(ROOT)["groups"],
            APPROVED_RELEASE_GROUPS,
        )
        for group in APPROVED_RELEASE_GROUPS:
            self.assertEqual(by_id[group["id"]]["file_count"], len(group["files"]))
        self.assertEqual(by_id["compatibility-runtime-skills"]["file_count"], 16)
        kit = load_portable_agent_kit(ROOT / "agent-kit")
        self.assertEqual(kit.lock.kit_version, "0.16.0")
        self.assertEqual(kit.lock.bundle_sha256,
            "b07f2285b26400fce3e119f48b25b804b94c66feabe349441a6a3bb72506ffb9")

    def test_projection_tamper_missing_file_and_path_escape_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-agent-assets-") as raw:
            repo = Path(raw)
            source = repo / "agent-assets/source"
            projection = repo / "projection"
            source.mkdir(parents=True)
            projection.mkdir()
            (source / "SKILL.md").write_text("canonical\n", encoding="utf-8")
            shutil.copyfile(source / "SKILL.md", projection / "SKILL.md")
            group = {
                "id": "fixture",
                "classification": "CURRENT",
                "asset_kind": "TEST",
                "source": "agent-assets/source",
                "tree_sha256": asset_registry.tree_receipt(source)["tree_sha256"],
                "consumers": ["test"],
                "target": "test",
                "compatibility": "test",
                "verification": "test",
                "retirement_condition": "test",
                "projections": [{"path": "projection", "mode": "mirror"}],
            }
            (repo / "agent-assets/catalog.json").write_text(
                json.dumps(
                    {
                        "schema_version": asset_registry.SCHEMA_VERSION,
                        "groups": [group],
                    }
                ),
                encoding="utf-8",
            )
            asset_registry.validate_repository_assets(repo)

            (projection / "SKILL.md").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(
                asset_registry.AssetRegistryError, "Projection content differs"
            ):
                asset_registry.validate_repository_assets(repo)

            (projection / "SKILL.md").unlink()
            with self.assertRaisesRegex(
                asset_registry.AssetRegistryError,
                "Projection inventory differs|Asset directory is empty",
            ):
                asset_registry.validate_repository_assets(repo)

            (projection / "SKILL.md").write_text("canonical\n", encoding="utf-8")
            (source / "SKILL.md").write_text("unreviewed source change\n", encoding="utf-8")
            with self.assertRaisesRegex(
                asset_registry.AssetRegistryError,
                "identity changed",
            ):
                asset_registry.materialize_projections(repo)
            self.assertEqual(
                (projection / "SKILL.md").read_text(encoding="utf-8"),
                "canonical\n",
            )

            group["source"] = "../escape"
            (repo / "agent-assets/catalog.json").write_text(
                json.dumps(
                    {
                        "schema_version": asset_registry.SCHEMA_VERSION,
                        "groups": [group],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                asset_registry.AssetRegistryError, "unsafe or non-canonical"
            ):
                asset_registry.validate_repository_assets(repo)

    def test_plugin_build_uses_canonical_regular_files_and_is_checkout_independent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-agent-bundle-") as raw:
            output = Path(raw) / "reqpilot"
            build_bundle.build(ROOT, output)
            receipt = asset_registry.verify_frozen_bundle(output)
            self.assertEqual(receipt["status"], "VERIFIED")
            self.assertEqual(receipt["group_count"], 2)
            self.assertFalse(any(path.is_symlink() for path in output.rglob("*")))

            skill = output / "skills/reqpilot/SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8") + "\ntamper\n", encoding="utf-8")
            with self.assertRaisesRegex(
                asset_registry.AssetRegistryError,
                "failed identity verification",
            ):
                asset_registry.verify_frozen_bundle(output)

    def test_release_assets_must_match_the_engine_commit_and_clean_git_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-agent-assets-git-") as raw:
            repo = Path(raw)
            source = repo / "agent-assets/source"
            projection = repo / "projection"
            scripts = repo / "agent-assets/scripts"
            plugin_scripts = repo / "plugins/reqpilot/scripts"
            for directory in (source, projection, scripts, plugin_scripts):
                directory.mkdir(parents=True, exist_ok=True)
            (source / "SKILL.md").write_text("canonical\n", encoding="utf-8")
            shutil.copyfile(source / "SKILL.md", projection / "SKILL.md")
            shutil.copyfile(
                ROOT / "agent-assets/scripts/asset_registry.py",
                scripts / "asset_registry.py",
            )
            (plugin_scripts / "build_bundle.py").write_text("# fixture\n", encoding="utf-8")
            group = {
                "id": "fixture",
                "classification": "CURRENT",
                "asset_kind": "TEST",
                "source": "agent-assets/source",
                "tree_sha256": asset_registry.tree_receipt(source)["tree_sha256"],
                "consumers": ["test"],
                "target": "test",
                "compatibility": "test",
                "verification": "test",
                "retirement_condition": "test",
                "projections": [{"path": "projection", "mode": "mirror"}],
                "bundle_root": "skills",
            }
            (repo / "agent-assets/catalog.json").write_text(
                json.dumps(
                    {
                        "schema_version": asset_registry.SCHEMA_VERSION,
                        "groups": [group],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "r5@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "R5 test"],
                check=True,
            )
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "fixture"],
                check=True,
            )
            commit = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            asset_registry.assert_release_inputs_match_commit(repo, commit)

            with self.assertRaisesRegex(
                asset_registry.AssetRegistryError, "same commit"
            ):
                asset_registry.assert_release_inputs_match_commit(repo, "0" * 40)

            (source / "SKILL.md").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(
                asset_registry.AssetRegistryError, "committed and clean"
            ):
                asset_registry.assert_release_inputs_match_commit(repo, commit)


class CompatibilitySkillConsumerTests(unittest.TestCase):
    def test_runtime_skill_consumer_uses_registered_asset_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-runtime-skills-") as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            paths, manifest = _copy_runtime_skills(
                SimpleNamespace(sterile_cwd=workspace),
                repository_root=ROOT,
            )
            self.assertEqual(
                set(paths),
                {
                    "reqpilotx-pdf-understanding",
                    "reqpilotx-structure-tree-generation",
                },
            )
            self.assertEqual(set(manifest), set(paths))
            for path in paths.values():
                self.assertTrue(path.is_file())
                self.assertTrue(path.is_relative_to(workspace.resolve()))

    def test_runtime_skill_consumer_rejects_missing_and_unsafe_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-runtime-skills-") as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            with self.assertRaisesRegex(
                LifecycleProbeError, "PRODUCT_SKILL_SOURCE_MISSING"
            ):
                _copy_runtime_skills(
                    SimpleNamespace(sterile_cwd=workspace),
                    repository_root=root,
                )

            with self.assertRaisesRegex(
                LifecycleProbeError, "PRODUCT_SKILL_NAME_INVALID"
            ):
                _copy_runtime_skills(
                    SimpleNamespace(sterile_cwd=workspace),
                    repository_root=ROOT,
                    skill_names=("../escape",),
                )


if __name__ == "__main__":
    unittest.main()
