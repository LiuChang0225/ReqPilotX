from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.preview_snapshot_scope import build_preview_in_isolated_workspace


class PreviewSnapshotScopeTests(unittest.TestCase):
    def test_build_uses_controlled_output_root_and_never_touches_source_dir(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_dir = root / "source"
            output_dir = root / "reports"
            source_dir.mkdir()
            source_path = source_dir / "sample.pdf"
            source_path.write_bytes(b"%PDF-1.7\n")
            observed_snapshot_roots: list[Path] = []

            def fake_preview(
                file_path: str,
                *,
                document_id: str = "",
                snapshot_root: Path | None = None,
            ) -> dict[str, object]:
                self.assertEqual(Path(file_path), source_path)
                self.assertEqual(document_id, "fixture-document")
                self.assertIsNotNone(snapshot_root)
                assert snapshot_root is not None
                observed_snapshot_roots.append(snapshot_root)
                snapshot_root.mkdir(parents=True, exist_ok=True)
                (snapshot_root / "fixture.png").write_bytes(b"snapshot")
                return {"snapshot_root_was_explicit": True}

            with patch(
                "scripts.preview_snapshot_scope.build_extraction_preview",
                side_effect=fake_preview,
            ):
                preview = build_preview_in_isolated_workspace(
                    source_path,
                    working_dir=output_dir,
                    document_id="fixture-document",
                )

            self.assertEqual(preview, {"snapshot_root_was_explicit": True})
            self.assertEqual(len(observed_snapshot_roots), 1)
            snapshot_root = observed_snapshot_roots[0]
            self.assertEqual(snapshot_root, output_dir.resolve() / "table_snapshots")
            self.assertTrue((snapshot_root / "fixture.png").is_file())
            self.assertFalse((source_dir / "table_snapshots").exists())
            self.assertEqual(list(source_dir.glob(".write_probe_*")), [])
            self.assertEqual(list(output_dir.glob(".reqpilotx-preview-*")), [])

    def test_without_output_dir_uses_and_cleans_system_temporary_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_dir = Path(temporary_directory)
            source_path = source_dir / "sample.pdf"
            source_path.write_bytes(b"%PDF-1.7\n")
            observed_snapshot_roots: list[Path] = []

            def fake_preview(
                _file_path: str,
                *,
                document_id: str = "",
                snapshot_root: Path | None = None,
            ) -> dict[str, object]:
                self.assertEqual(document_id, "")
                self.assertIsNotNone(snapshot_root)
                assert snapshot_root is not None
                observed_snapshot_roots.append(snapshot_root)
                snapshot_root.mkdir(parents=True, exist_ok=True)
                return {}

            with patch(
                "scripts.preview_snapshot_scope.build_extraction_preview",
                side_effect=fake_preview,
            ):
                build_preview_in_isolated_workspace(source_path)

            self.assertEqual(len(observed_snapshot_roots), 1)
            self.assertFalse(observed_snapshot_roots[0].exists())
            self.assertFalse((source_dir / "table_snapshots").exists())
            self.assertEqual(list(source_dir.glob(".write_probe_*")), [])


if __name__ == "__main__":
    unittest.main()
