"""Fast contract checks for the shared R1 engine selection and staging."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/reqpilot/scripts"))

import build_bundle
import build_engine
from portable_core import NAMESPACE_INITIALIZERS, canonical_digest, select_core


class EngineDistributionTests(unittest.TestCase):
    def _manifest(self):
        content = b"value = 'safe'\n"
        manifest = {
            "schema_version": "reqpilotx-engine-core/v1", "distribution": "reqpilotx-engine", "version": "0.1.0",
            "source_repository": "ReqPilotX", "source_commit": "0" * 40, "source_tree": "0" * 40,
            "python_minimum": "3.11", "module_roots": [], "requirements": [],
            "optional_or_unknown_imports": [], "namespace_initializers": [],
            "runtime_files": [{
                "source_path": "app/example.py", "runtime_path": "app/example.py", "wheel_path": "app/example.py",
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
            "app/example.py": content,
            "reqpilotx_engine/__init__.py": b"def core_manifest(): return {}\n",
            "reqpilotx_engine/core-manifest.json": json.dumps(manifest).encode(),
            "reqpilotx_engine_tools/__init__.py": b'"""tools"""\n',
            f"{dist}/METADATA": b"Metadata-Version: 2.4\nName: reqpilotx-engine\nVersion: 0.1.0\nRequires-Python: >=3.11\n\n",
            f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            f"{dist}/top_level.txt": b"app\nreqpilotx_engine\nreqpilotx_engine_tools\n",
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
        self.assertIn("scripts/fast_source_capture.py", paths)
        self.assertFalse(any("/persistence/" in f"/{path}/" or "/infrastructure/" in f"/{path}/" for path in paths))
        self.assertFalse(any(path.startswith("app/adapters/agents/") for path in paths))
        self.assertEqual(set(NAMESPACE_INITIALIZERS), {member.runtime_relative for member in selection.members if member.namespace_initializer})

    def test_staged_project_contains_manifest_and_no_maintained_source_copy(self):
        with TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            manifest = build_engine.prepare_stage(ROOT, stage)
            self.assertEqual(manifest["core_identity"], canonical_digest({k: v for k, v in manifest.items() if k != "core_identity"}))
            self.assertEqual(len(manifest["runtime_files"]), len(select_core(ROOT).members))
            self.assertTrue((stage / "src/app/application/agent_workflow/source_requirement_baseline.py").is_file())
            self.assertTrue((stage / "src/reqpilotx_engine_tools/fast_source_capture.py").is_file())
            pyproject = (stage / "pyproject.toml").read_text(encoding="utf-8")
            self.assertIn(f'version = "{manifest["version"]}"', pyproject)
            for requirement in manifest["requirements"]:
                self.assertIn(json.dumps(requirement), pyproject)
        self.assertFalse((ROOT / "packages/engine/src").exists(), "Generated engine source must not become a maintained copy")

    def test_wheel_manifest_tampering_is_rejected_before_plugin_copy(self):
        with TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "reqpilotx_engine-0.1.0-py3-none-any.whl"
            entries, record = self._entries()
            entries["app/example.py"] = b"tampered"
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
            "host": ("app/adapters/persistence/review_marker.py", b"marker"),
            "pth": ("review_marker.pth", b"import app"),
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
                if case == "duplicate": duplicate = "app/example.py"
                elif case == "missing": entries.pop("app/example.py")
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
            self.assertTrue((output / "runtime/prototype/app/application/agent_workflow/source_requirement_baseline.py").is_file())


if __name__ == "__main__":
    unittest.main()
