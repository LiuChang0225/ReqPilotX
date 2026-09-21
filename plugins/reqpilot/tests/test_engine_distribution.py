"""Fast contract checks for the shared R1 engine selection and staging."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/reqpilot/scripts"))

import build_bundle
import build_engine
from portable_core import NAMESPACE_INITIALIZERS, canonical_digest, select_core


def fixture_git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=ReqPilotX test",
         "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
         "-c", f"core.hooksPath={repo / '.git/no-hooks'}", *arguments],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@contextmanager
def committed_release_fixture():
    # Real Git provenance tests never dirty the caller's checkout or require
    # developers to commit their current edits merely to run unit tests.
    with TemporaryDirectory(prefix="reqpilotx-release-input-test-") as temporary:
        repo = Path(temporary)
        selection = select_core(ROOT)
        paths = {*build_engine.BUILD_INPUT_FILES, *(m.source_relative for m in selection.members)}
        for relative in paths:
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        fixture_git(repo, "init", "--quiet")
        fixture_git(repo, "add", ".")
        fixture_git(repo, "commit", "--quiet", "-m", "committed engine test inputs")
        yield repo


class EngineDistributionTests(unittest.TestCase):
    def _manifest(self):
        content = b"value = 'safe'\n"
        manifest = {
            "schema_version": "reqpilotx-engine-core/v2", "distribution": "reqpilotx-engine", "version": "0.1.0",
            "source_repository": "ReqPilotX", "source_commit": "0" * 40, "source_tree": "0" * 40,
            "python_minimum": "3.11", "module_roots": [], "requirements": [],
            "optional_or_unknown_imports": [], "namespace_initializers": [],
            "runtime_layout": "reqpilotx-package/v1",
            "runtime_files": [{
                "source_path": "reqpilotx/example.py", "runtime_path": "reqpilotx/example.py", "wheel_path": "reqpilotx/example.py",
                "sha256": hashlib.sha256(content).hexdigest(), "upstream_sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content), "namespace_initializer": False,
            }],
            "banned_host_members_found": 0, "customer_data_included": False,
        }
        manifest["core_identity"] = canonical_digest(manifest)
        return manifest, content

    def _entries(self):
        manifest, content = self._manifest()
        dist = "reqpilotx_engine-0.1.0.dist-info"
        entries = {
            "reqpilotx/example.py": content,
            "reqpilotx_engine/__init__.py": b"def core_manifest(): return {}\n",
            "reqpilotx_engine/core-manifest.json": json.dumps(manifest).encode(),
            f"{dist}/METADATA": b"Metadata-Version: 2.4\nName: reqpilotx-engine\nVersion: 0.1.0\nRequires-Python: >=3.11\n\n",
            f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            f"{dist}/entry_points.txt": b"[console_scripts]\nreqpilotx-fast-source-capture = reqpilotx.tools.fast_source_capture:main\n",
            f"{dist}/top_level.txt": b"reqpilotx\nreqpilotx_engine\n",
        }
        return entries, f"{dist}/RECORD"

    def _write_wheel(self, path: Path, entries: dict[str, bytes], record_path: str, *, duplicate: str | None = None, valid_record: bool = True):
        rows = []
        for name, data in sorted(entries.items()):
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rows.append((name, f"sha256={digest}", str(len(data))))
        rows.append((record_path, "", ""))
        stream = io.StringIO(); csv.writer(stream, lineterminator="\n").writerows(rows)
        record = stream.getvalue().encode()
        if not valid_record:
            record = record.replace(b"sha256=", b"sha256=broken", 1)
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
            archive.writestr(record_path, record)
            if duplicate:
                archive.writestr(duplicate, entries[duplicate])

    def test_shared_selection_keeps_one_source_boundary_and_host_layers_out(self):
        selection = select_core(ROOT)
        paths = [member.runtime_relative for member in selection.members]
        self.assertGreater(len(paths), 1)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("reqpilotx/tools/fast_source_capture.py", paths)
        self.assertFalse(any("/persistence/" in f"/{path}/" or "/infrastructure/" in f"/{path}/" for path in paths))
        self.assertFalse(any(path.startswith("reqpilotx_server/") for path in paths))
        self.assertEqual(set(NAMESPACE_INITIALIZERS), {member.runtime_relative for member in selection.members if member.namespace_initializer})

    def test_staged_project_contains_manifest_and_no_maintained_source_copy(self):
        with committed_release_fixture() as repo, TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            manifest = build_engine.prepare_stage(repo, stage)
            self.assertEqual(manifest["core_identity"], canonical_digest({k: v for k, v in manifest.items() if k != "core_identity"}))
            self.assertEqual(len(manifest["runtime_files"]), len(select_core(ROOT).members))
            self.assertTrue((stage / "src/reqpilotx/application/agent_workflow/source_requirement_baseline.py").is_file())
            self.assertTrue((stage / "src/reqpilotx/tools/fast_source_capture.py").is_file())
            pyproject = (stage / "pyproject.toml").read_text(encoding="utf-8")
            self.assertIn(f'version = "{manifest["version"]}"', pyproject)
            for requirement in manifest["requirements"]:
                self.assertIn(json.dumps(requirement), pyproject)
        self.assertTrue(
            (ROOT / "packages/engine/src/reqpilotx").is_dir(),
            "The maintained reqpilotx package is the authoritative engine source",
        )

    def test_uv_version_contract_accepts_verified_versions_and_rejects_old_or_unknown(self):
        for version, accepted in (
            ("uv 0.10.2 (Homebrew)", True), ("uv 0.12.13 (build)", True),
            ("uv 0.7.22", False), ("uv 0.10.1", False),
            ("uv 0.12.13.dev1", False), ("unknown", False),
        ):
            with self.subTest(version=version), patch.object(build_engine.shutil, "which", return_value="/test/uv"), patch.object(
                build_engine.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=version, stderr=""),
            ):
                if accepted:
                    self.assertEqual(build_engine.require_uv_frontend(), ("/test/uv", version))
                else:
                    with self.assertRaisesRegex(ValueError, "requires stable uv >= 0.10.2"):
                        build_engine.require_uv_frontend()

    def test_old_uv_is_rejected_before_creating_build_output(self):
        with TemporaryDirectory() as temporary, patch.object(build_engine.shutil, "which", return_value="/test/uv"), patch.object(
            build_engine.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="uv 0.7.22", stderr=""),
        ) as process:
            output = Path(temporary) / "release"
            with self.assertRaisesRegex(ValueError, "Unsupported uv version"):
                build_engine.build(ROOT, output)
            self.assertFalse(output.exists())
            self.assertEqual(process.call_count, 1)
            self.assertEqual(process.call_args.args[0], ["/test/uv", "--version"])

    def test_release_rejects_dirty_source_dependency_and_packaging_inputs_before_staging(self):
        with committed_release_fixture() as repo:
            for relative in (
                "packages/engine/src/reqpilotx/application/fast_source_capture.py",
                "packages/engine/src/reqpilotx/__init__.py", "packages/engine/pyproject.toml",
                "plugins/reqpilot/scripts/build_engine.py", "plugins/reqpilot/scripts/portable_core.py",
                "packages/engine/pyproject.toml", "packages/engine/README.md",
            ):
                with self.subTest(path=relative):
                    path = repo / relative
                    before = path.read_bytes()
                    path.write_bytes(before + b"\n# uncommitted test change\n")
                    try:
                        with self.assertRaisesRegex(ValueError, "Release inputs must be committed and clean"):
                            build_engine.prepare_stage(repo, repo / "stage")
                        self.assertFalse((repo / "stage").exists())
                    finally:
                        path.write_bytes(before)

    def test_release_rejects_staged_new_and_deleted_source_inputs(self):
        with committed_release_fixture() as repo:
            source = repo / "packages/engine/src/reqpilotx/application/fast_source_capture.py"
            source.write_bytes(source.read_bytes() + b"\n# staged change\n")
            fixture_git(repo, "add", str(source.relative_to(repo)))
            with self.assertRaisesRegex(ValueError, "committed and clean"):
                build_engine.prepare_stage(repo, repo / "stage")
        with committed_release_fixture() as repo:
            (repo / "packages/engine/src/reqpilotx/untracked_source.py").write_text("# untracked source\n")
            with self.assertRaisesRegex(ValueError, "committed and clean"):
                build_engine.prepare_stage(repo, repo / "stage")
        with committed_release_fixture() as repo:
            (repo / "packages/engine/src/reqpilotx/__init__.py").unlink()
            with self.assertRaisesRegex(ValueError, "committed and clean"):
                build_engine.prepare_stage(repo, repo / "stage")

    def test_commit_byte_comparison_detects_assume_unchanged_source_edit(self):
        with committed_release_fixture() as repo:
            relative = "packages/engine/src/reqpilotx/application/fast_source_capture.py"
            fixture_git(repo, "update-index", "--assume-unchanged", relative)
            source = repo / relative
            source.write_bytes(source.read_bytes() + b"\n# hidden worktree edit\n")
            with self.assertRaisesRegex(ValueError, "Selected source differs from release commit"):
                build_engine.prepare_stage(repo, repo / "stage")
            self.assertFalse((repo / "stage").exists())

    def test_release_allows_unrelated_dirty_documentation(self):
        with committed_release_fixture() as repo:
            (repo / "notes.md").write_text("Unrelated developer note; not an engine input.\n")
            manifest = build_engine.prepare_stage(repo, repo / "stage")
            self.assertEqual(manifest["source_commit"], fixture_git(repo, "rev-parse", "HEAD"))
            self.assertEqual(manifest["source_tree"], fixture_git(repo, "rev-parse", "HEAD^{tree}"))

    def test_release_detects_input_change_during_selection(self):
        real_selection = build_engine.select_core
        with committed_release_fixture() as repo:
            def change_after_reading(path):
                selection = real_selection(path)
                target = repo / "packages/engine/README.md"
                target.write_bytes(target.read_bytes() + b"\nchanged during selection\n")
                return selection
            with patch.object(build_engine, "select_core", side_effect=change_after_reading):
                with self.assertRaisesRegex(ValueError, "Build input differs from release commit"):
                    build_engine.prepare_stage(repo, repo / "stage")
            self.assertFalse((repo / "stage").exists())

    def test_wheel_manifest_tampering_is_rejected_before_plugin_copy(self):
        with TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "reqpilotx_engine-0.1.0-py3-none-any.whl"
            entries, record = self._entries()
            entries["reqpilotx/example.py"] = b"tampered"
            self._write_wheel(wheel, entries, record)
            with self.assertRaisesRegex(ValueError, "failed its manifest hash"):
                build_bundle._validated_wheel_manifest(wheel)

    def test_complete_valid_wheel_inventory_and_record_are_accepted(self):
        with TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "reqpilotx_engine-0.1.0-py3-none-any.whl"
            entries, record = self._entries(); self._write_wheel(wheel, entries, record)
            manifest = build_bundle._validated_wheel_manifest(wheel)
            self.assertEqual(manifest["distribution"], "reqpilotx-engine")

    def test_extra_host_pth_unregistered_and_traversal_members_are_rejected_even_with_updated_record(self):
        extras = {
            "host": ("reqpilotx_server/adapters/persistence/review_marker.py", b"marker"),
            "pth": ("review_marker.pth", b"import reqpilotx_server"),
            "unregistered": ("reqpilotx_engine/extra.py", b"marker"),
            "traversal": ("../escape.py", b"marker"),
        }
        for name, (member, data) in extras.items():
            with self.subTest(name=name), TemporaryDirectory() as temporary:
                wheel = Path(temporary) / "reqpilotx_engine-0.1.0-py3-none-any.whl"
                entries, record = self._entries(); entries[member] = data
                self._write_wheel(wheel, entries, record)
                with self.assertRaises(ValueError):
                    build_bundle._validated_wheel_manifest(wheel)

    def test_duplicate_missing_record_and_metadata_mismatches_are_rejected(self):
        cases = ("duplicate", "missing", "record", "metadata")
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as temporary:
                wheel = Path(temporary) / "reqpilotx_engine-0.1.0-py3-none-any.whl"
                entries, record = self._entries()
                duplicate = None; valid_record = True
                if case == "duplicate": duplicate = "reqpilotx/example.py"
                elif case == "missing": entries.pop("reqpilotx/example.py")
                elif case == "record": valid_record = False
                else:
                    metadata = next(name for name in entries if name.endswith("/METADATA"))
                    entries[metadata] = entries[metadata].replace(b"Version: 0.1.0", b"Version: 0.2.0")
                self._write_wheel(wheel, entries, record, duplicate=duplicate, valid_record=valid_record)
                with self.assertRaises(ValueError):
                    build_bundle._validated_wheel_manifest(wheel)

    def test_existing_no_wheel_build_remains_a_checkout_development_path(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "plugin"
            result = build_bundle.build(ROOT, output)
            receipt = json.loads((output / "build-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "BUILT")
            self.assertEqual(receipt["release_mode"], "DEVELOPMENT_SOURCE_COPY")
            self.assertNotIn("engine_release", receipt)
            self.assertTrue((output / "runtime/prototype/reqpilotx/application/agent_workflow/source_requirement_baseline.py").is_file())


if __name__ == "__main__":
    unittest.main()
