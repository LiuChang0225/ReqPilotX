from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from spikes.codex_app_server.pdf_tool_host import prepare_pdf_canary
from spikes.codex_app_server.pdf_tool_host import canonical_sha256
from spikes.codex_app_server.read_only_mcp import (
    ALL_TOOL_NAMES,
    DOCUMENT_AGENT_FACTS_SCHEMA,
    MCP_PROTOCOL_VERSION,
    ReadOnlyMcpError,
    ReadOnlyToolSession,
    StdioMcpServer,
    TOOL_GET_DOCUMENT_FACTS,
    TOOL_GET_PAGE_LEDGER,
    TOOL_GET_SOURCE_CHUNKS,
    TOOL_PREFLIGHT,
    TOOL_RENDER_SCOPED_PAGE,
    TOOL_VALIDATE_COVERAGE,
    document_agent_facts_payload,
    load_session_descriptor,
    tool_definitions,
    write_session_descriptor,
)
from spikes.codex_app_server.synthetic_pdf_fixture import PROMPT_INJECTION_MARKER
from tests.spikes.test_codex_pdf_skill_canary import (
    _valid_understanding_proposal,
)


class CodexReadOnlyMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="reqpilotx-readonly-mcp-")
        self.root = Path(self._tmp.name).resolve()
        os.chmod(self.root, 0o700)
        self.server_dir = self.root / "server"
        self.model_dir = self.root / "model"
        for directory in (self.server_dir, self.model_dir):
            directory.mkdir(mode=0o700)
        self.context = prepare_pdf_canary(
            server_private_dir=self.server_dir,
            model_workspace=self.model_dir,
            include_prompt_injection=True,
        )
        self.descriptor_path = self.server_dir / "mcp-session.json"
        tokens = iter(("A" * 32, "B" * 32))
        self.descriptor, self.descriptor_sha256 = write_session_descriptor(
            self.context,
            destination=self.descriptor_path,
            token_factory=lambda _length: next(tokens),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_document_facts_are_actor_neutral_path_free_and_hash_stable(self) -> None:
        facts = document_agent_facts_payload(self.descriptor)

        self.assertEqual(facts["schema_version"], DOCUMENT_AGENT_FACTS_SCHEMA)
        self.assertFalse(facts["capabilities"]["business_writes"])
        self.assertEqual(facts, document_agent_facts_payload(self.context))
        serialized = json.dumps(facts, sort_keys=True).casefold()
        for forbidden in (
            "actor",
            "principal",
            "email",
            "run_handle",
            "document_handle",
            str(self.context.source_path).casefold(),
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertNotEqual(self.descriptor.run_handle, self.descriptor.document_handle)
        self.assertNotIn(self.context.fixture.source_sha256, self.descriptor.run_handle)
        self.assertEqual(oct(self.descriptor_path.stat().st_mode & 0o777), "0o600")
        self.assertRegex(self.descriptor_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(load_session_descriptor(self.descriptor_path), self.descriptor)
        resolved = ReadOnlyToolSession(self.descriptor).call(
            TOOL_GET_DOCUMENT_FACTS,
            {"run_handle": self.descriptor.run_handle},
        )
        self.assertEqual(
            resolved["document_agent_facts_sha256"],
            canonical_sha256(facts),
        )

    def test_tool_schemas_are_exact_read_only_and_expose_no_write_surface(self) -> None:
        definitions = tool_definitions()

        self.assertEqual([item["name"] for item in definitions], list(ALL_TOOL_NAMES))
        for item in definitions:
            self.assertFalse(item["inputSchema"]["additionalProperties"])
            self.assertTrue(item["annotations"]["readOnlyHint"])
            self.assertFalse(item["annotations"]["destructiveHint"])
            serialized = json.dumps(item, sort_keys=True).casefold()
            for forbidden in ("source_path", "sql", "apply", "publish", "export write"):
                self.assertNotIn(forbidden, serialized)

    def test_unknown_handles_paths_pages_stale_source_and_budgets_fail_closed(
        self,
    ) -> None:
        session = ReadOnlyToolSession(self.descriptor)

        with self.assertRaisesRegex(ReadOnlyMcpError, "TOOL_NOT_ALLOWED"):
            session.call("admin.export_secrets", {})
        with self.assertRaisesRegex(ReadOnlyMcpError, "TOOL_ARGUMENTS_INVALID"):
            session.call(
                TOOL_GET_DOCUMENT_FACTS,
                {"run_handle": self.descriptor.run_handle, "path": "/etc/passwd"},
            )
        with self.assertRaisesRegex(ReadOnlyMcpError, "RUN_HANDLE_UNKNOWN"):
            ReadOnlyToolSession(self.descriptor).call(
                TOOL_GET_DOCUMENT_FACTS,
                {"run_handle": "rpxh_" + ("Z" * 32)},
            )

        session = ReadOnlyToolSession(self.descriptor)
        session.call(
            TOOL_GET_DOCUMENT_FACTS,
            {"run_handle": self.descriptor.run_handle},
        )
        with self.assertRaisesRegex(ReadOnlyMcpError, "DOCUMENT_HANDLE_UNKNOWN"):
            session.call(
                TOOL_PREFLIGHT,
                {"document_handle": "rpxh_" + ("Z" * 32)},
            )
        session = _through_ledger(self.descriptor)
        with self.assertRaisesRegex(ReadOnlyMcpError, "PAGE_OUT_OF_SCOPE"):
            session.call(
                TOOL_GET_SOURCE_CHUNKS,
                {
                    "document_handle": self.descriptor.document_handle,
                    "physical_page": 3,
                },
            )
        session.call(
            TOOL_GET_SOURCE_CHUNKS,
            {
                "document_handle": self.descriptor.document_handle,
                "physical_page": 1,
            },
        )
        with self.assertRaisesRegex(ReadOnlyMcpError, "TOOL_CALL_BUDGET_EXCEEDED"):
            session.call(
                TOOL_GET_SOURCE_CHUNKS,
                {
                    "document_handle": self.descriptor.document_handle,
                    "physical_page": 2,
                },
            )

        stale_session = ReadOnlyToolSession(self.descriptor)
        self.context.source_path.write_bytes(b"%PDF-stale")
        with self.assertRaisesRegex(ReadOnlyMcpError, "SOURCE_STALE"):
            stale_session.call(
                TOOL_GET_DOCUMENT_FACTS,
                {"run_handle": self.descriptor.run_handle},
            )

    def test_complete_tool_loop_validates_pixels_ignores_injection_and_writes_zero(
        self,
    ) -> None:
        session = _through_ledger(self.descriptor)
        for page in (1, 2):
            chunks = session.call(
                TOOL_GET_SOURCE_CHUNKS,
                {
                    "document_handle": self.descriptor.document_handle,
                    "physical_page": page,
                },
            )
            self.assertEqual(chunks["physical_page"], page)
            if page == 1:
                self.assertTrue(chunks["native_observation_targets"])
                self.assertTrue(
                    all(
                        target["exact_label"] is None
                        and target["evidence_channel"] == "NATIVE"
                        for target in chunks["native_observation_targets"]
                    )
                )
            else:
                self.assertEqual(chunks["native_observation_targets"], [])
        rendered = session.call(
            TOOL_RENDER_SCOPED_PAGE,
            {
                "document_handle": self.descriptor.document_handle,
                "physical_page": 2,
                "expected_page_ledger_sha256": self.descriptor.page_ledger_sha256,
            },
        )
        self.assertEqual(rendered["page_image_sha256"], self.context.page_image_sha256)
        self.assertGreater(len(rendered["image_base64"]), 100)

        proposal = _valid_understanding_proposal(self.context)
        validated = session.call(
            TOOL_VALIDATE_COVERAGE,
            {
                "document_handle": self.descriptor.document_handle,
                "proposal": proposal,
            },
        )
        self.assertEqual(validated["coverage_receipt"]["status"], "PARTIAL")
        self.assertFalse(validated["coverage_receipt"]["apply_eligible"])
        self.assertEqual(validated["write_audit"]["total_writes"], 0)
        self.assertEqual(
            validated["write_audit"]["counts"],
            {"business_state": 0, "job_or_event": 0, "runtime_cache": 0},
        )
        self.assertNotIn(PROMPT_INJECTION_MARKER, json.dumps(proposal))

        observed_injection = copy.deepcopy(proposal)
        observed_injection["pages"][1]["observations"].append(
            {
                "kind": "VISUAL_LABEL",
                "evidence_channel": "VISUAL",
                "evidence_refs": [self.context.page_image_sha256],
                "exact_label": PROMPT_INJECTION_MARKER,
            }
        )
        observed = _ready_for_validation(self.descriptor).call(
            TOOL_VALIDATE_COVERAGE,
            {
                "document_handle": self.descriptor.document_handle,
                "proposal": observed_injection,
            },
        )
        self.assertNotEqual(observed["proposal_sha256"], validated["proposal_sha256"])

    def test_stdio_server_negotiates_tools_and_returns_content_free_errors(
        self,
    ) -> None:
        server = _CapturingServer(ReadOnlyToolSession(self.descriptor))

        server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
            }
        )
        server._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        server._dispatch(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "admin.export_secrets", "arguments": {}},
            }
        )

        self.assertEqual(
            server.messages[0]["result"]["protocolVersion"], MCP_PROTOCOL_VERSION
        )
        self.assertEqual(
            [item["name"] for item in server.messages[1]["result"]["tools"]],
            list(ALL_TOOL_NAMES),
        )
        failure = server.messages[2]["result"]
        self.assertTrue(failure["isError"])
        self.assertEqual(
            failure["structuredContent"], {"reason_code": "TOOL_NOT_ALLOWED"}
        )

    def test_stdio_transport_runs_as_isolated_process(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": TOOL_GET_DOCUMENT_FACTS,
                    "arguments": {"run_handle": self.descriptor.run_handle},
                },
            },
        ]
        payload = "".join(json.dumps(item) + "\n" for item in requests)
        prototype_root = Path(__file__).resolve().parents[2]
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(prototype_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "spikes.codex_app_server.read_only_mcp",
                "--session-file",
                str(self.descriptor_path),
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=prototype_root,
            env=environment,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        messages = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([message["id"] for message in messages], [1, 2, 3])
        facts = messages[2]["result"]["structuredContent"]
        self.assertEqual(facts["document_handle"], self.descriptor.document_handle)
        self.assertRegex(facts["document_agent_facts_sha256"], r"^[0-9a-f]{64}$")


class _CapturingServer(StdioMcpServer):
    def __init__(self, session: ReadOnlyToolSession) -> None:
        super().__init__(session)
        self.messages: list[dict[str, object]] = []

    def _write(self, payload):
        self.messages.append(dict(payload))


def _through_ledger(descriptor) -> ReadOnlyToolSession:
    session = ReadOnlyToolSession(descriptor)
    session.call(
        TOOL_GET_DOCUMENT_FACTS,
        {"run_handle": descriptor.run_handle},
    )
    session.call(
        TOOL_PREFLIGHT,
        {"document_handle": descriptor.document_handle},
    )
    ledger = session.call(
        TOOL_GET_PAGE_LEDGER,
        {"document_handle": descriptor.document_handle},
    )
    if ledger["proposal_contract"]["agent_claimed_status"] != "PARTIAL":
        raise AssertionError("proposal contract drifted")
    return session


def _ready_for_validation(descriptor) -> ReadOnlyToolSession:
    session = _through_ledger(descriptor)
    for page in (1, 2):
        session.call(
            TOOL_GET_SOURCE_CHUNKS,
            {"document_handle": descriptor.document_handle, "physical_page": page},
        )
    session.call(
        TOOL_RENDER_SCOPED_PAGE,
        {
            "document_handle": descriptor.document_handle,
            "physical_page": 2,
            "expected_page_ledger_sha256": descriptor.page_ledger_sha256,
        },
    )
    return session


if __name__ == "__main__":
    unittest.main()
