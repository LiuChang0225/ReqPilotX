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
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pymupdf

from app.adapters.agents.agent_kit_materializer import materialize_agent_kit
from app.adapters.agents.d1_evidence_mcp import (
    D1_MCP_SERVER_NAME,
    d1_tool_definitions,
    load_d1_mcp_session,
    write_d1_mcp_descriptor,
)
from app.adapters.agents.document_evidence_host import D1_TOOL_NAMES
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.adapters.agents.stage_context import compile_agent_stage_context
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE_ROOT = REPOSITORY_ROOT / "prototype"


class D1EvidenceMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        two_page = self.root / "two-page.pdf"
        build_synthetic_pdf(two_page, include_prompt_injection=True)
        self.source = self.root / "portable-d1-three-page.pdf"
        original = pymupdf.open(two_page)
        combined = pymupdf.open()
        combined.insert_pdf(original)
        combined.new_page()
        combined.save(self.source)
        combined.close()
        original.close()
        self.source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        canonical = load_portable_agent_kit(REPOSITORY_ROOT / "agent-kit")
        self.materialized = materialize_agent_kit(
            canonical,
            self.root / "materialized-kit",
            runner_profile_id="app-server-product",
        )
        self.kit = load_portable_agent_kit(self.materialized.root)
        self.stage_context = compile_agent_stage_context(
            self.kit,
            AgentExtractionStage.DOCUMENT_UNDERSTANDING,
        )
        self.run_id = "agent-run-" + "a" * 32
        self.source_handle = "source-handle-" + "b" * 32
        self.descriptor_path = self.root / "d1-session.json"
        self.descriptor_sha256 = write_d1_mcp_descriptor(
            self.descriptor_path,
            source_path=self.source,
            source_sha256=self.source_sha256,
            document_role="CUSTOMER_SOR",
            run_id=self.run_id,
            opaque_source_handle=self.source_handle,
            intentional_blank_pages=(3,),
            kit=self.kit,
            stage_context_sha256=self.stage_context.context_sha256,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_descriptor_loads_exact_kit_source_and_host(self) -> None:
        descriptor, kit, host = load_d1_mcp_session(self.descriptor_path)

        self.assertEqual(descriptor.source_sha256, self.source_sha256)
        self.assertEqual(kit.identity, self.kit.identity)
        self.assertEqual(host.prepared.manifest.run_id, self.run_id)
        self.assertEqual(
            host.prepared.manifest.opaque_source_handle,
            self.source_handle,
        )
        self.assertEqual(len(self.descriptor_sha256), 64)

    def test_tool_list_is_exact_and_schema_bound(self) -> None:
        definitions = d1_tool_definitions(self.kit)

        self.assertEqual(
            tuple(item["name"] for item in definitions),
            tuple(sorted(D1_TOOL_NAMES)),
        )
        for item in definitions:
            input_payload = json.dumps(
                item["inputSchema"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
            output_payload = json.dumps(
                item["outputSchema"],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
            self.assertEqual(
                hashlib.sha256(input_payload.encode("utf-8")).hexdigest(),
                self.kit.tool_schema_sha256[item["name"]][0],
            )
            self.assertEqual(
                hashlib.sha256(output_payload.encode("utf-8")).hexdigest(),
                self.kit.tool_schema_sha256[item["name"]][1],
            )
            self.assertTrue(item["annotations"]["readOnlyHint"])
            self.assertFalse(item["annotations"]["destructiveHint"])

    def test_stdio_server_lists_tools_and_returns_content_free_manifest(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(PROTOTYPE_ROOT)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.adapters.agents.d1_evidence_mcp",
                "--session-file",
                str(self.descriptor_path),
            ],
            cwd=PROTOTYPE_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        try:
            initialize = self._request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                },
            )
            self.assertEqual(
                initialize["result"]["serverInfo"]["name"],
                D1_MCP_SERVER_NAME,
            )
            process.stdin.write(
                json.dumps(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
                + "\n"
            )
            process.stdin.flush()
            listed = self._request(
                process,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            self.assertEqual(
                tuple(item["name"] for item in listed["result"]["tools"]),
                tuple(sorted(D1_TOOL_NAMES)),
            )
            manifest = self._request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "document.get_run_manifest",
                        "arguments": {
                            "source_handle": self.source_handle,
                            "stage_context_sha256": self.stage_context.context_sha256,
                        },
                    },
                },
            )
            structured = manifest["result"]["structuredContent"]
            self.assertEqual(structured["source_sha256"], self.source_sha256)
            serialized = json.dumps(structured, sort_keys=True)
            self.assertNotIn(str(self.source), serialized)
            self.assertNotIn(self.source.name, serialized)
            denied = self._request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "shell", "arguments": {}},
                },
            )
            self.assertTrue(denied["result"]["isError"])
            self.assertEqual(
                denied["result"]["structuredContent"]["reason_code"],
                "document_tool_not_allowed",
            )
        finally:
            process.stdin.close()
            process.wait(timeout=15)
            process.stdout.close()
            assert process.stderr is not None
            process.stderr.close()
        self.assertEqual(process.returncode, 0)

    def _request(self, process, payload):  # type: ignore[no-untyped-def]
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        self.assertTrue(line)
        return json.loads(line)


if __name__ == "__main__":
    unittest.main()
