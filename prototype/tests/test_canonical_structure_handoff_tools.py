from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.adapters.agents.codex_app_server.canonical_structure_handoff_tools import (
    CanonicalNativeStructureHandoffToolHost,
    TOOL_STRUCTURE_GET_HANDOFF_CONTEXT,
    TOOL_STRUCTURE_INSPECT_SCOPE,
)


class CanonicalStructureHandoffToolTests(unittest.TestCase):
    def test_catalog_is_high_level_and_host_injects_all_identities(self) -> None:
        publisher = _Publisher()
        host = CanonicalNativeStructureHandoffToolHost(
            project_id="project-1",
            document_id="document-1",
            handoff_id="handoff-1",
            revision_publisher=publisher,
        )
        context = {
            "source_sha256": "a" * 64,
            "artifact_id": "artifact-1",
            "handoff_id": "handoff-1",
            "affected_scope_ids": ["scope-1"],
        }
        with patch(
            "app.adapters.agents.codex_app_server.canonical_structure_handoff_tools.native_structure_bridge.get_native_structure_handoff_context",
            return_value=context,
        ) as get_context:
            result = host.invoke(
                tool_name=TOOL_STRUCTURE_GET_HANDOFF_CONTEXT,
                arguments={},
            )

        self.assertTrue(result.success)
        self.assertEqual(host.context_call_count, 1)
        get_context.assert_called_once_with("project-1", "document-1", "handoff-1")
        names = tuple(item.name for item in host.specs)
        self.assertEqual(len(names), 4)
        self.assertFalse(any("page" in name or "limit" in name for name in names))

    def test_inspection_builds_host_bound_request(self) -> None:
        host = CanonicalNativeStructureHandoffToolHost(
            project_id="project-1",
            document_id="document-1",
            handoff_id="handoff-1",
            revision_publisher=_Publisher(),
        )
        context = {
            "source_sha256": "a" * 64,
            "artifact_id": "artifact-1",
            "handoff_id": "handoff-1",
        }
        with (
            patch(
                "app.adapters.agents.codex_app_server.canonical_structure_handoff_tools.native_structure_bridge.get_native_structure_handoff_context",
                return_value=context,
            ),
            patch(
                "app.adapters.agents.codex_app_server.canonical_structure_handoff_tools.native_structure_bridge.invoke_native_structure_host_tool",
                return_value={"receipt": {"tool_call": {"call_id": "call-1"}}},
            ) as invoke,
        ):
            result = host.invoke(
                tool_name=TOOL_STRUCTURE_INSPECT_SCOPE,
                arguments={
                    "scope_id": "scope-1",
                    "intent": "INSPECT_LOGICAL_TABLE",
                    "logical_table_scope_id": "table-1",
                },
            )

        self.assertTrue(result.success)
        request = invoke.call_args.args[2]
        self.assertEqual(request.expected_source_sha256, "a" * 64)
        self.assertEqual(request.expected_artifact_id, "artifact-1")
        self.assertEqual(request.handoff_id, "handoff-1")
        self.assertTrue(request.command_id.startswith("canonical-handoff-tool:"))
        self.assertIsInstance(invoke.call_args.args[3], SimpleNamespace)


class _Publisher:
    last_result = None

    def publish(self, submission):  # type: ignore[no-untyped-def]
        del submission
        return {"status": "VERIFICATION_REQUIRED"}


if __name__ == "__main__":
    unittest.main()
