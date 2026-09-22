from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import ast
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.infrastructure.local_runtime import prepare_artifact_smoke_storage
from app.runtime_paths import resolve_storage_dir


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "apps/desktop/app_desktop.py"
LEGACY = ROOT / "prototype/app_desktop.py"


class DesktopStoragePathsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="reqpilot-desktop-paths-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout"
        self.home = self.root / "user"

    def storage(self, *, legacy=False, environment=None, frozen=False,
                smoke=False, platform="darwin"):
        # Execute the launcher's actual storage decision only. Never import the
        # desktop program or reach its mkdir/logger/config/DB/webview startup.
        tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_default_user_storage_dir")
        index = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "configured_storage_dir"
                             for t in node.targets))
        selection = ast.Module(body=[function, tree.body[index], tree.body[index + 1]], type_ignores=[])
        env = dict(environment or {})

        def evaluate(path, run_name=None):
            self.assertEqual(Path(path), self.checkout / "apps/desktop/app_desktop.py")
            namespace = {
                "__file__": str(path), "Path": Path,
                "os": SimpleNamespace(environ=env, getenv=env.get),
                "sys": SimpleNamespace(frozen=frozen, platform=platform),
                "_artifact_smoke_server": smoke,
                "prepare_artifact_smoke_storage": prepare_artifact_smoke_storage,
                "resolve_storage_dir": lambda base: resolve_storage_dir(base, environment=env),
            }
            with patch.object(Path, "home", return_value=self.home):
                exec(compile(selection, str(LAUNCHER), "exec"), namespace)
            return namespace["storage_dir"]

        if not legacy:
            return evaluate(self.checkout / "apps/desktop/app_desktop.py")
        results = []
        def forward(path, **kwargs):
            results.append(evaluate(path, **kwargs))
        with patch("runpy.run_path", side_effect=forward):
            exec(compile(LEGACY.read_text(encoding="utf-8"), str(LEGACY), "exec"),
                 {"__name__": "__main__", "__file__": str(self.checkout / "prototype/app_desktop.py")})
        self.assertEqual(len(results), 1)
        return results[0]

    def test_both_development_entries_keep_checkout_prototype_storage_without_writes(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.assertEqual(self.storage(legacy=legacy), self.checkout / "prototype/storage")
                self.assertEqual(self.storage(legacy=legacy, environment={"REQPILOT_PORTABLE_MODE": "0"}),
                                 self.checkout / "prototype/storage")
        self.assertFalse(self.checkout.exists())

    def test_explicit_absolute_storage_wins_in_development_frozen_and_portable(self):
        explicit = self.root / "explicit"
        for legacy in (False, True):
            for frozen, portable in ((False, "0"), (True, "0"), (False, "1")):
                with self.subTest(legacy=legacy, frozen=frozen, portable=portable):
                    self.assertEqual(self.storage(legacy=legacy, frozen=frozen, environment={
                        "REQPILOT_STORAGE_DIR": str(explicit), "REQPILOT_PORTABLE_MODE": portable}), explicit)
        self.assertFalse(explicit.exists())

    def test_frozen_and_portable_preserve_platform_user_storage(self):
        for platform, suffix in (("darwin", "Library/Application Support/ReqPilotX/storage"),
                                 ("win32", "AppData/Local/ReqPilotX/storage"),
                                 ("linux", ".local/share/ReqPilotX/storage")):
            for legacy in (False, True):
                for frozen, environment in ((True, {}), (False, {"REQPILOT_PORTABLE_MODE": "1"})):
                    with self.subTest(platform=platform, legacy=legacy, frozen=frozen):
                        self.assertEqual(self.storage(legacy=legacy, platform=platform, frozen=frozen,
                                                      environment=environment), self.home / suffix)
        self.assertFalse(self.home.exists())

    def test_artifact_smoke_uses_only_its_private_root_and_rejects_existing_data(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                smoke_root = self.root / f"smoke-{legacy}"
                env = {"REQPILOT_STORAGE_DIR": str(smoke_root)}
                self.assertEqual(self.storage(legacy=legacy, frozen=True, smoke=True, environment=env), smoke_root)
                marker = smoke_root / "config.json"
                marker.write_text("synthetic-canary", encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "existing data"):
                    self.storage(legacy=legacy, frozen=True, smoke=True, environment=env)
                self.assertEqual(marker.read_text(encoding="utf-8"), "synthetic-canary")
        self.assertFalse(self.checkout.exists())

    def test_artifact_smoke_never_falls_back_to_development_or_user_storage(self):
        for legacy in (False, True):
            for frozen, env in ((False, {"REQPILOT_STORAGE_DIR": str(self.root / "unused")}),
                                (True, {}), (True, {"REQPILOT_STORAGE_DIR": "relative"})):
                with self.subTest(legacy=legacy, frozen=frozen, environment=env):
                    with self.assertRaises(SystemExit):
                        self.storage(legacy=legacy, frozen=frozen, smoke=True, environment=env)
        self.assertFalse((self.root / "unused").exists())
        self.assertFalse(self.checkout.exists())
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
