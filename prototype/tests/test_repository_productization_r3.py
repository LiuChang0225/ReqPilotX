from __future__ import annotations

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

import ast
from pathlib import Path
import sys
import tempfile
import unittest

from reqpilotx.ports.agent_structure_handoff_executor import (
    CanonicalStructureHandoffBindingV1,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalStructureHandoffBindingV1 as HostBinding,
)
from reqpilotx_server.infrastructure.runtime_settings import RuntimeSettings
from reqpilotx_server.resource_paths import postgres_migration_dir


ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "packages/engine/src/reqpilotx"
SERVER = ROOT / "apps/server/src/reqpilotx_server"
sys.path.insert(0, str(ROOT / "plugins/reqpilot/scripts"))

from portable_core import select_core


class RepositoryProductizationR3Tests(unittest.TestCase):
    def test_engine_has_no_legacy_or_server_imports_and_all_sources_are_selected(self):
        selected = {
            member.source_path.resolve() for member in select_core(ROOT).members
        }
        actual = {
            path.resolve()
            for path in ENGINE.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(selected, actual)
        for path in ENGINE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] if not node.level else []
                else:
                    continue
                self.assertFalse(
                    any(
                        name == "app"
                        or name.startswith("app.")
                        or name == "reqpilotx_server"
                        or name.startswith("reqpilotx_server.")
                        for name in names
                    ),
                    path,
                )

    def test_server_engine_compatibility_modules_are_aliases_not_copies(self):
        checked = 0
        for source in ENGINE.rglob("*.py"):
            relative = source.relative_to(ENGINE)
            alias = SERVER / relative
            self.assertTrue(alias.is_file(), relative)
            if relative.name != "__init__.py":
                self.assertIn("Engine compatibility alias", alias.read_text())
            checked += 1
        self.assertGreater(checked, 90)

    def test_handoff_binding_keeps_one_class_identity(self):
        self.assertIs(CanonicalStructureHandoffBindingV1, HostBinding)

    def test_installed_migration_resource_and_legacy_path_are_same_files(self):
        installed = postgres_migration_dir()
        legacy = ROOT / "prototype/migrations/postgresql"
        self.assertTrue(legacy.is_symlink())
        self.assertEqual(legacy.resolve(), installed.resolve())
        self.assertEqual(
            [path.name for path in sorted(installed.glob("*.sql"))],
            [path.name for path in sorted(legacy.glob("*.sql"))],
        )
        self.assertEqual(len(tuple(installed.glob("*.sql"))), 38)

    def test_runtime_paths_are_host_configured_not_package_writable_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = RuntimeSettings.from_environment(
                environment={
                    "REQPILOT_TEST_MODE": "1",
                    "REQPILOT_STORAGE_DIR": str(root / "storage"),
                    "REQPILOT_OBJECT_ROOT": str(root / "objects"),
                    "REQPILOT_FRONTEND_DIR": str(root / "frontend"),
                    "REQPILOT_DEVELOPMENT_ROOT": str(ROOT),
                }
            )
        self.assertEqual(settings.development_root, ROOT)
        self.assertEqual(settings.frontend_root, (root / "frontend").resolve())
        self.assertTrue(settings.object_root.is_relative_to(root.resolve()))


if __name__ == "__main__":
    unittest.main()
