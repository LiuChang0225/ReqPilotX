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

from app.adapters.agents.codex_app_server.lifecycle import CanaryRuntimeState
from app.adapters.agents.d1_desktop_cold_canary import (
    _desktop_event_summary,
    _desktop_exec_command,
)


class D1DesktopColdCanaryTests(unittest.TestCase):
    def test_exec_command_is_ephemeral_hermetic_and_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            runtime = CanaryRuntimeState(
                codex_home=root,
                command=(
                    "/opt/codex",
                    "app-server",
                    "--stdio",
                    "--strict-config",
                    "--config",
                    "features.shell_tool=false",
                    "--config",
                    "mcp_servers.test.enabled=true",
                ),
                environment={},
                log_dir=root,
                protocol={},
                sqlite_home=root,
                state_path=root,
                sterile_cwd=root,
                tmp_dir=root,
            )
            command = _desktop_exec_command(
                runtime=runtime,
                model_id="gpt-5.6-sol",
                reasoning_effort="high",
                output_schema_path=schema,
                final_message_path=root / "final.json",
            )

        self.assertEqual(command[:2], ["/opt/codex", "exec"])
        for flag in (
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--sandbox",
            "--output-schema",
            "--output-last-message",
        ):
            self.assertIn(flag, command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn("features.shell_tool=false", command)
        self.assertEqual(command[-1], "-")

    def test_event_summary_rejects_shell_and_accepts_mcp(self) -> None:
        allowed = _desktop_event_summary(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "tool": "document.get_run_manifest",
                    },
                }
            )
        )
        self.assertFalse(allowed["forbidden_tool_event"])

        with self.assertRaisesRegex(Exception, "D1_DESKTOP_FORBIDDEN_TOOL_EVENT"):
            _desktop_event_summary(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "command_execution", "name": "shell"},
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
