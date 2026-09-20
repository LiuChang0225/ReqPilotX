from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from spikes.codex_app_server.lifecycle import CanaryRuntimeState
from spikes.codex_app_server.read_only_mcp import (
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    STRUCTURE_TOOL_GET_CURRENT,
    STRUCTURE_TOOL_NAMES,
    STRUCTURE_TOOL_VALIDATE_PROPOSAL,
    tool_definitions,
)
from spikes.codex_app_server.read_only_mcp_canary import (
    McpEventLedger,
    configure_read_only_mcp_runtime,
    validate_required_mcp_status,
)
from spikes.codex_app_server.shadow_structure_canary import _structure_turn_input
from spikes.codex_app_server.synthetic_pdf_fixture import (
    PROMPT_INJECTION_MARKER,
    VISUAL_CANARY_CODE,
)
from tests.spikes.test_codex_read_only_mcp_runner_contract import (
    _observe_basic_finish,
    _observe_basic_start,
    _tool_event,
)


class CodexShadowStructureRunnerContractTests(unittest.TestCase):
    def test_structure_turn_input_is_handle_and_explicit_skill_only(self) -> None:
        turn_input = _structure_turn_input(
            run_handle="rpxh_" + ("A" * 32),
            skill_path=Path("/private/structure/SKILL.md"),
        )

        self.assertEqual([item["type"] for item in turn_input], ["text", "skill"])
        self.assertIn("RUN_HANDLE=rpxh_", turn_input[0]["text"])
        for forbidden in (
            VISUAL_CANARY_CODE,
            PROMPT_INJECTION_MARKER,
            "admin.export_secrets",
            "localImage",
        ):
            self.assertNotIn(forbidden.casefold(), turn_input[0]["text"].casefold())

    def test_structure_runtime_enables_only_two_required_tools(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-b3-runtime-") as raw:
            root = Path(raw).resolve()
            descriptor = root / "descriptor.json"
            descriptor.write_text("{}", encoding="utf-8")
            os.chmod(descriptor, 0o600)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = CanaryRuntimeState(
                codex_home=root / "home",
                command=("codex", "app-server"),
                environment={},
                log_dir=root / "logs",
                protocol={},
                sqlite_home=root / "sqlite",
                state_path=root,
                sterile_cwd=workspace,
                tmp_dir=root / "tmp",
            )
            configured = configure_read_only_mcp_runtime(
                runtime,
                descriptor_path=descriptor,
                repository_root=Path(__file__).resolve().parents[3],
                enabled_tool_names=STRUCTURE_TOOL_NAMES,
            )

        joined = " ".join(configured.command)
        self.assertIn(
            json.dumps(list(STRUCTURE_TOOL_NAMES), separators=(",", ":")),
            joined,
        )
        self.assertNotIn("document.render_scoped_page", joined)

    def test_structure_mcp_status_and_event_sequence_are_exact(self) -> None:
        tools = {
            item["name"]: item
            for item in tool_definitions()
            if item["name"] in STRUCTURE_TOOL_NAMES
        }
        status = {
            "data": [
                {
                    "name": MCP_SERVER_NAME,
                    "authStatus": "unsupported",
                    "pluginId": None,
                    "resourceTemplates": [],
                    "resources": [],
                    "serverInfo": {
                        "name": MCP_SERVER_NAME,
                        "version": MCP_SERVER_VERSION,
                    },
                    "tools": tools,
                }
            ],
            "nextCursor": None,
        }
        summary = validate_required_mcp_status(
            status,
            expected_tool_names=STRUCTURE_TOOL_NAMES,
        )
        self.assertEqual(summary["tool_count"], 2)

        ledger = McpEventLedger(
            allowed_tool_names=STRUCTURE_TOOL_NAMES,
            expected_tool_prefix=(STRUCTURE_TOOL_GET_CURRENT,),
            validation_tool_name=STRUCTURE_TOOL_VALIDATE_PROPOSAL,
        )
        ledger.bind_thread("thread-1")
        ledger.bind_turn("turn-1")
        _observe_basic_start(ledger)
        first_arguments = {"run_handle": "rpxh_" + ("A" * 32)}
        ledger.observe(
            _tool_event(
                "item/started",
                "tool-1",
                STRUCTURE_TOOL_GET_CURRENT,
                first_arguments,
                status="inProgress",
            )
        )
        ledger.observe(
            _tool_event(
                "item/completed",
                "tool-1",
                STRUCTURE_TOOL_GET_CURRENT,
                first_arguments,
                status="completed",
                structured={},
            )
        )
        validate_arguments = {
            "document_handle": "rpxh_" + ("B" * 32),
            "expected_base_structure_sha256": "a" * 64,
            "expected_base_revision": 0,
            "expected_expires_at_utc": "2026-08-21T08:15:00Z",
            "proposal": {},
        }
        ledger.observe(
            _tool_event(
                "item/started",
                "tool-2",
                STRUCTURE_TOOL_VALIDATE_PROPOSAL,
                validate_arguments,
                status="inProgress",
            )
        )
        ledger.observe(
            _tool_event(
                "item/completed",
                "tool-2",
                STRUCTURE_TOOL_VALIDATE_PROPOSAL,
                validate_arguments,
                status="completed",
                structured={
                    "proposal_sha256": "c" * 64,
                    "write_audit": {
                        "schema_version": "reqpilotx-read-only-write-audit/v1",
                        "counts": {
                            "business_state": 0,
                            "job_or_event": 0,
                            "runtime_cache": 0,
                        },
                        "total_writes": 0,
                    },
                },
            )
        )
        _observe_basic_finish(ledger)
        ledger.validate_tool_sequence(thread_id="thread-1", turn_id="turn-1")
        self.assertEqual(ledger.tool_sequence, list(STRUCTURE_TOOL_NAMES))


if __name__ == "__main__":
    unittest.main()
