from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server import d3_region_stage_input_workspace
from app.adapters.agents.codex_app_server.d3_region_stage_input_workspace import (
    D3_REGION_STAGE_INPUT_RELATIVE_DIRECTORY,
    D3RegionStageInputWorkspaceError,
    materialize_d3_region_stage_input_workspace,
    verify_d3_region_stage_input_workspace,
)

if __package__:
    from .test_d3_region_stage_input import _Fixture
else:
    from test_d3_region_stage_input import _Fixture


class D3RegionStageInputWorkspaceTests(unittest.TestCase):
    def test_materialize_reopen_permissions_and_content_free_manifest(self) -> None:
        prepared = _prepared()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "private-session"
            root.mkdir(mode=0o700)

            materialized = _materialize(root, prepared)
            reopened = verify_d3_region_stage_input_workspace(
                session_root=root,
                expected_attestation=materialized.attestation,
            )

            self.assertEqual(reopened.attestation, materialized.attestation)
            self.assertEqual(reopened.workspace_path.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                tuple(sorted(item.name for item in reopened.workspace_path.iterdir())),
                (
                    "d2-logical-structure-proposal-v2.json",
                    "d2-logical-structure-validation-receipt-v2.json",
                    "d2-table-region-parent-input-receipt.json",
                    "d2-table-region-parent-input.json",
                    "d3-unified-exact-source-segments-v3.json",
                    "manifest.json",
                ),
            )
            self.assertTrue(
                all(
                    item.stat().st_mode & 0o777 == 0o600
                    for item in reopened.workspace_path.iterdir()
                )
            )
            serialized = json.dumps(
                reopened.attestation.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertTrue(reopened.attestation.content_free)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("A source-faithful provision", serialized)
            self.assertNotIn("table-1-cell", serialized)
            self.assertNotIn("note-1", serialized)

    def test_payload_and_manifest_tamper_fail_closed(self) -> None:
        prepared = _prepared()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "payload-session"
            root.mkdir(mode=0o700)
            materialized = _materialize(root, prepared)
            payload = (
                materialized.workspace_path / "d3-unified-exact-source-segments-v3.json"
            )
            payload.write_bytes(payload.read_bytes() + b" ")
            with self.assertRaisesRegex(
                D3RegionStageInputWorkspaceError,
                "FILE_TAMPERED",
            ):
                verify_d3_region_stage_input_workspace(
                    session_root=root,
                    expected_attestation=materialized.attestation,
                )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "manifest-session"
            root.mkdir(mode=0o700)
            materialized = _materialize(root, prepared)
            manifest = json.loads(
                materialized.manifest_path.read_text(encoding="utf-8")
            )
            manifest["source_sha256"] = "f" * 64
            materialized.manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            materialized.manifest_path.chmod(0o600)
            with self.assertRaises((D3RegionStageInputWorkspaceError, ValueError)):
                verify_d3_region_stage_input_workspace(
                    session_root=root,
                    expected_attestation=materialized.attestation,
                )

    def test_partial_write_is_removed_before_publication(self) -> None:
        prepared = _prepared()
        real_write = d3_region_stage_input_workspace._atomic_write_new_file
        calls = 0

        def fail_second_write(path: Path, payload: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected write failure")
            real_write(path, payload)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "private-session"
            root.mkdir(mode=0o700)
            with patch.object(
                d3_region_stage_input_workspace,
                "_atomic_write_new_file",
                side_effect=fail_second_write,
            ):
                with self.assertRaisesRegex(OSError, "injected write failure"):
                    _materialize(root, prepared)

            self.assertFalse((root / D3_REGION_STAGE_INPUT_RELATIVE_DIRECTORY).exists())
            self.assertFalse((root / "stage-input").exists())
            self.assertFalse(tuple(root.rglob("*.tmp")))

    def test_symlink_overwrite_and_permission_drift_are_rejected(self) -> None:
        prepared = _prepared()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "private-session"
            root.mkdir(mode=0o700)
            materialized = _materialize(root, prepared)
            with self.assertRaisesRegex(
                D3RegionStageInputWorkspaceError,
                "OVERWRITE_FORBIDDEN",
            ):
                _materialize(root, prepared)

            payload = materialized.workspace_path / "d2-table-region-parent-input.json"
            payload.chmod(0o644)
            with self.assertRaisesRegex(
                D3RegionStageInputWorkspaceError,
                "FILE_MODE_DRIFT",
            ):
                verify_d3_region_stage_input_workspace(
                    session_root=root,
                    expected_attestation=materialized.attestation,
                )

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            private = base / "private"
            private.mkdir(mode=0o700)
            link = base / "session-link"
            link.symlink_to(private, target_is_directory=True)
            with self.assertRaisesRegex(
                D3RegionStageInputWorkspaceError,
                "SYMLINK_FORBIDDEN",
            ):
                _materialize(link, prepared)


def _prepared():
    fixture = _Fixture()
    return fixture.prepare(fixture.complete_scope_artifact())


def _materialize(root: Path, prepared):
    return materialize_d3_region_stage_input_workspace(
        session_root=root,
        source_ir=prepared.source,
        d2_proposal=prepared.proposal,
        d2_validation_receipt=prepared.receipt,
        parent_input=prepared.parent_input,
        parent_input_receipt=prepared.parent_receipt,
        expected_lineage=prepared.lineage,
    )


if __name__ == "__main__":
    unittest.main()
