from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import os
from pathlib import Path
import stat
import tempfile
import unittest

from app.adapters.agents.codex_app_server.atomization_mcp import (
    AtomizationMcpError,
    AtomizationToolSession,
    TOOL_GET_TASK,
    TOOL_NAMES,
    TOOL_VALIDATE,
    load_atomization_descriptor,
    tool_definitions,
    write_atomization_descriptor,
)
from tests.test_native_codex_atomization_reference import _reference, _request


class CodexAtomizationMcpTests(unittest.TestCase):
    def test_descriptor_and_validator_preserve_exact_cover(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            path = root / "session.json"
            written = write_atomization_descriptor(path, request=_request())

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = load_atomization_descriptor(path)
            self.assertEqual(loaded.request, written.request)
            session = AtomizationToolSession(loaded)
            task = session.call(TOOL_GET_TASK, {"run_handle": loaded.run_handle})
            self.assertTrue(task["parent_is_preserved_by_host"])
            reread = session.call(TOOL_GET_TASK, {"run_handle": loaded.run_handle})
            self.assertEqual(reread["request"], task["request"])
            with self.assertRaisesRegex(
                AtomizationMcpError, "TOOL_CALL_BUDGET_EXCEEDED"
            ):
                session.call(TOOL_GET_TASK, {"run_handle": loaded.run_handle})
            result = session.call(
                TOOL_VALIDATE,
                {
                    "run_handle": loaded.run_handle,
                    "candidate": _reference().model_dump(mode="json"),
                },
            )

            self.assertEqual(result["validation_receipt"]["obligation_count"], 1)
            self.assertEqual(result["write_audit"]["total_writes"], 0)
            self.assertEqual(result["candidate_sha256"], result["proposal_sha256"])

    def test_sequence_handle_and_grounding_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            os.chmod(root, 0o700)
            path = root / "session.json"
            descriptor = write_atomization_descriptor(path, request=_request())
            session = AtomizationToolSession(descriptor)
            with self.assertRaisesRegex(AtomizationMcpError, "TOOL_SEQUENCE_INVALID"):
                session.call(
                    TOOL_VALIDATE,
                    {
                        "run_handle": descriptor.run_handle,
                        "candidate": _reference().model_dump(mode="json"),
                    },
                )
            session = AtomizationToolSession(descriptor)
            with self.assertRaisesRegex(AtomizationMcpError, "RUN_HANDLE_UNKNOWN"):
                session.call(TOOL_GET_TASK, {"run_handle": "wrong"})
            session = AtomizationToolSession(descriptor)
            session.call(TOOL_GET_TASK, {"run_handle": descriptor.run_handle})
            invalid = _reference().model_copy(update={"context_ranges": ()})
            with self.assertRaisesRegex(
                AtomizationMcpError,
                "PROPOSAL_REJECTED_ATOMIZATION_GROUNDING_INVALID",
            ):
                session.call(
                    TOOL_VALIDATE,
                    {
                        "run_handle": descriptor.run_handle,
                        "candidate": invalid.model_dump(mode="json"),
                    },
                )

    def test_tools_are_closed_world_and_read_only(self) -> None:
        definitions = tool_definitions()
        self.assertEqual(tuple(item["name"] for item in definitions), TOOL_NAMES)
        for item in definitions:
            self.assertFalse(item["inputSchema"]["additionalProperties"])
            self.assertTrue(item["annotations"]["readOnlyHint"])
            self.assertFalse(item["annotations"]["destructiveHint"])
            self.assertFalse(item["annotations"]["openWorldHint"])


if __name__ == "__main__":
    unittest.main()
