from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from spikes.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    LifecycleProbeError,
)
from spikes.codex_app_server.read_only_mcp import (
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    TOOL_NAMES,
    tool_definitions,
)
from spikes.codex_app_server.read_only_mcp_canary import (
    McpEventLedger,
    _EXPECTED_TOOL_SEQUENCE,
    _mcp_turn_input,
    configure_read_only_mcp_runtime,
    validate_required_mcp_status,
)
from spikes.codex_app_server.synthetic_pdf_fixture import (
    PROMPT_INJECTION_MARKER,
    VISUAL_CANARY_CODE,
)


class CodexReadOnlyMcpRunnerContractTests(unittest.TestCase):
    def test_runtime_config_is_required_exact_and_private(self) -> None:
        with tempfile.TemporaryDirectory(prefix="reqpilotx-mcp-runtime-") as root:
            root_path = Path(root).resolve()
            descriptor = root_path / "session.json"
            descriptor.write_text("{}", encoding="utf-8")
            os.chmod(descriptor, 0o600)
            runtime = _runtime(root_path)
            configured = configure_read_only_mcp_runtime(
                runtime,
                descriptor_path=descriptor,
                repository_root=Path(__file__).resolve().parents[3],
            )

        joined = " ".join(configured.command)
        self.assertIn(f"mcp_servers.{MCP_SERVER_NAME}.required=true", joined)
        self.assertIn(f"mcp_servers.{MCP_SERVER_NAME}.enabled_tools=", joined)
        self.assertIn("PYTHONDONTWRITEBYTECODE", joined)
        self.assertIn("read_only_mcp", joined)
        for forbidden in ("OPENAI_API_KEY", "DASHSCOPE_API_KEY", "auth.json"):
            self.assertNotIn(forbidden, joined)

    def test_required_mcp_status_rejects_missing_failed_or_extra_tools(self) -> None:
        valid = _mcp_status()
        summary = validate_required_mcp_status(valid)
        self.assertEqual(summary["tool_count"], len(TOOL_NAMES))

        for invalid in (
            {"data": [], "nextCursor": None},
            {"data": [{**valid["data"][0], "name": "ambient"}], "nextCursor": None},
        ):
            with self.assertRaisesRegex(
                LifecycleProbeError, "REQUIRED_MCP_UNAVAILABLE"
            ):
                validate_required_mcp_status(invalid)
        extra = copy.deepcopy(valid)
        extra["data"][0]["tools"]["admin.write"] = {}
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "REQUIRED_MCP_TOOLSET_MISMATCH",
        ):
            validate_required_mcp_status(extra)
        writable = copy.deepcopy(valid)
        writable["data"][0]["tools"][TOOL_NAMES[0]]["annotations"]["readOnlyHint"] = (
            False
        )
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "REQUIRED_MCP_TOOLSET_MISMATCH",
        ):
            validate_required_mcp_status(writable)

    def test_turn_input_contains_only_handle_and_explicit_skill(self) -> None:
        skill_path = Path("/private/skill/SKILL.md")
        turn_input = _mcp_turn_input(
            run_handle="rpxh_" + ("A" * 32),
            skill_path=skill_path,
        )

        self.assertEqual([item["type"] for item in turn_input], ["text", "skill"])
        self.assertIn("RUN_HANDLE=rpxh_", turn_input[0]["text"])
        self.assertNotIn("localImage", {item["type"] for item in turn_input})
        for forbidden in (
            VISUAL_CANARY_CODE,
            PROMPT_INJECTION_MARKER,
            "admin.export_secrets",
        ):
            self.assertNotIn(forbidden.casefold(), turn_input[0]["text"].casefold())

    def test_event_ledger_accepts_only_exact_read_only_tool_sequence(self) -> None:
        ledger = McpEventLedger()
        ledger.bind_thread("thread-1")
        ledger.bind_turn("turn-1")
        _observe_basic_start(ledger)
        for index, tool in enumerate(_EXPECTED_TOOL_SEQUENCE, start=1):
            item_id = f"tool-{index}"
            arguments = {"document_handle": "rpxh_" + ("B" * 32)}
            if tool == TOOL_NAMES[0]:
                arguments = {"run_handle": "rpxh_" + ("A" * 32)}
            ledger.observe(
                _tool_event(
                    "item/started", item_id, tool, arguments, status="inProgress"
                )
            )
            structured = {}
            if tool == TOOL_NAMES[-1]:
                structured = {
                    "proposal_sha256": "a" * 64,
                    "write_audit": {
                        "schema_version": "reqpilotx-read-only-write-audit/v1",
                        "counts": {
                            "business_state": 0,
                            "job_or_event": 0,
                            "runtime_cache": 0,
                        },
                        "total_writes": 0,
                    },
                }
            ledger.observe(
                _tool_event(
                    "item/completed",
                    item_id,
                    tool,
                    arguments,
                    status="completed",
                    structured=structured,
                )
            )
        _observe_basic_finish(ledger)
        ledger.validate_tool_sequence(thread_id="thread-1", turn_id="turn-1")
        self.assertEqual(ledger.tool_sequence, list(_EXPECTED_TOOL_SEQUENCE))
        self.assertNotIn("rpxh_", json.dumps(ledger.content_free_summary()))

        unsafe = McpEventLedger()
        unsafe.bind_thread("thread-1")
        unsafe.bind_turn("turn-1")
        with self.assertRaisesRegex(LifecycleProbeError, "MCP_TOOL_ITEM_INVALID"):
            unsafe.observe(
                _tool_event(
                    "item/started",
                    "tool-unsafe",
                    TOOL_NAMES[0],
                    {"path": "/etc/passwd"},
                    status="inProgress",
                )
            )


def _runtime(root: Path) -> CanaryRuntimeState:
    workspace = root / "workspace"
    workspace.mkdir()
    return CanaryRuntimeState(
        codex_home=root / "codex-home",
        command=("codex", "app-server"),
        environment={},
        log_dir=root / "logs",
        protocol={},
        sqlite_home=root / "sqlite",
        state_path=root,
        sterile_cwd=workspace,
        tmp_dir=root / "tmp",
    )


def _mcp_status() -> dict[str, object]:
    tools = {
        item["name"]: item for item in tool_definitions() if item["name"] in TOOL_NAMES
    }
    return {
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


def _observe_basic_start(ledger: McpEventLedger) -> None:
    ledger.observe(
        {"method": "thread/started", "params": {"thread": {"id": "thread-1"}}}
    )
    ledger.observe(
        {
            "method": "turn/started",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "inProgress"},
            },
        }
    )
    for item_id, item_type in (("user-1", "userMessage"),):
        ledger.observe(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": item_id, "type": item_type},
                },
            }
        )
        ledger.observe(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": item_id, "type": item_type},
                },
            }
        )


def _observe_basic_finish(ledger: McpEventLedger) -> None:
    ledger.observe(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "agent-1", "type": "agentMessage"},
            },
        }
    )
    ledger.observe(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "agent-1", "type": "agentMessage", "text": "{}"},
            },
        }
    )
    ledger.observe(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
    )
    ledger.observe({"method": "thread/deleted", "params": {"threadId": "thread-1"}})


def _tool_event(
    method: str,
    item_id: str,
    tool: str,
    arguments: dict[str, object],
    *,
    status: str,
    structured: dict[str, object] | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": item_id,
        "type": "mcpToolCall",
        "server": MCP_SERVER_NAME,
        "tool": tool,
        "status": status,
        "arguments": arguments,
    }
    if method == "item/completed":
        item.update(
            {
                "error": None,
                "result": {
                    "content": [],
                    "structuredContent": structured or {},
                },
            }
        )
    return {
        "method": method,
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": item,
        },
    }


if __name__ == "__main__":
    unittest.main()
