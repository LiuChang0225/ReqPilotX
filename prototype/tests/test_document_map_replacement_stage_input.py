from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app.adapters.agents.codex_app_server.document_map_replacement_stage_input import (
    materialize_document_map_replacement_stage_input,
)
from tests.document_map_replacement_support import build_replacement_fixture


class DocumentMapReplacementStageInputTests(unittest.TestCase):
    def test_materializes_heading_predecessor_and_scope_as_one_read_only_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = build_replacement_fixture(root / "evidence")
            session = root / "session"
            session.mkdir()
            result = materialize_document_map_replacement_stage_input(
                session_root=session,
                request=fixture.request,
            )
            self.assertEqual(result.attestation.execution_mode, "DOCUMENT_MAP_SCOPE_REPLACEMENT")
            self.assertEqual(result.attestation.replacement_request_sha256, fixture.request.request_sha256)
            self.assertEqual(result.attestation.heading_compilation_sha256, fixture.request.heading_compilation.compilation_sha256)
            self.assertEqual(result.request_path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(result.manifest_path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(hashlib.sha256(result.request_path.read_bytes()).hexdigest(), result.attestation.file.sha256)
            payload = json.loads(result.request_path.read_text())
            self.assertEqual(payload["heading_compilation"]["heading_nodes"][0]["title"], "Scope")
            self.assertNotIn("Scope", result.attestation.model_dump_json())


if __name__ == "__main__":
    unittest.main()
