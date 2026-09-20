from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from dataclasses import dataclass, replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from app.adapters.agents.codex_app_server.agent_native_read_only_runtime import (
    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_DESCRIPTION,
    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID,
    AgentNativeReadOnlyRuntimePolicyV1,
)
from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAgentNativeReadCapabilityV1,
    CanonicalAppServerDocumentSessionAttestationV1,
    CanonicalAppServerStageAttestationV1,
    CanonicalDocumentEvidenceAttestationV1,
    CanonicalStageCapabilitySkillAttestationV1,
    CanonicalStageFileDigestV1,
    CanonicalStageRunnerOutputSchemaVariantV1,
)
from app.adapters.agents.codex_app_server.canonical_dynamic_tools import (
    CanonicalDynamicToolExecution,
    CanonicalDynamicToolSpec,
    canonical_dynamic_tool_specs_sha256,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStageInvocation,
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.canonical_stage_transport import (
    CanonicalStageEventLedger,
    LocalCodexAppServerStructuredStageTransport,
    _validate_invocation,
)
from app.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    JsonlRpcSession,
    LifecycleProbeError,
    MAX_INBOUND_JSONL_BYTES,
    MAX_OUTBOUND_JSONL_BYTES,
)
from app.adapters.agents.codex_app_server.pdf_skill_canary import _workspace_manifest
from app.adapters.agents.codex_app_server.primary_runtime_pdf import (
    OpenAIPrimaryRuntimePackageV1,
    OpenAIPrimaryRuntimePdfAttestationV1,
    OpenAIPrimaryRuntimeToolV1,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentKitIdentityV1,
    AgentRunnerKind,
)


class CanonicalStageTransportTests(unittest.TestCase):
    def test_image_views_publish_content_free_visual_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            evidence = root / "evidence-workspace"
            high = evidence / "renders" / "high"
            high.mkdir(parents=True)
            contact_sheet = evidence / "contact-sheet.png"
            first_page = high / "P0001.png"
            second_page = high / "P0002.png"
            for path, payload in (
                (contact_sheet, b"contact"),
                (first_page, b"page-one"),
                (second_page, b"page-two"),
            ):
                path.write_bytes(payload)
            ledger = CanonicalStageEventLedger(readable_root=root)
            ledger.bind_thread("thread-stage-1")
            ledger.bind_turn("turn-stage-1")
            for ordinal, path in enumerate(
                (contact_sheet, first_page, second_page),
                start=1,
            ):
                item = {
                    "id": f"image-{ordinal}",
                    "type": "imageView",
                    "path": str(path),
                }
                ledger.observe(
                    {"method": "item/started", "params": _item_params(item)}
                )
                ledger.observe(
                    {"method": "item/completed", "params": _item_params(item)}
                )

            visual = ledger.content_free_summary()["visual_inspection"]
            self.assertTrue(visual["contact_sheet_opened"])
            self.assertEqual(visual["high_resolution_page_numbers"], [1, 2])
            self.assertEqual(visual["high_resolution_page_count"], 2)
            self.assertEqual(visual["image_view_count"], 3)
            self.assertNotIn(str(root), json.dumps(visual))

    def test_explicit_dynamic_tools_are_client_executed_and_content_free(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            opened: list[_FakeJsonlStageSession] = []
            host = _FakeDynamicToolHost(
                supported_stages=(invocation.stage,),
            )
            invocation = replace(
                invocation,
                dynamic_tool_names=tuple(sorted(item.name for item in host.specs)),
                dynamic_tool_specs_sha256=canonical_dynamic_tool_specs_sha256(
                    host.specs
                ),
            )

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                    emit_dynamic_tool=True,
                )
                opened.append(session)
                return session

            result = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                session_opener=open_session,
                dynamic_tool_host=host,
            ).invoke(invocation)

            session = opened[0]
            self.assertTrue(session.dynamic_server_requests_configured)
            self.assertEqual(
                session.params_for("thread/start")["dynamicTools"],
                [item.app_server_payload() for item in host.specs],
            )
            self.assertEqual(host.calls, [("reqpilotx_test_tool", {"scope": "one"})])
            summary = session.ledger.content_free_summary()["dynamic_tools"]
            self.assertEqual(summary["call_count"], 1)
            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(
                summary["tool_call_counts"],
                {"reqpilotx_test_tool": 1},
            )
            self.assertNotIn("SECRET_DYNAMIC_RESULT", json.dumps(summary))
            self.assertEqual(result.structured_output, {"marker": "OK"})

    def test_transport_accepts_only_the_attested_runner_schema_variant(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _variant_invocation(Path(raw))

            _validate_invocation(invocation)
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_STAGE_RUNNER_SCHEMA_MISMATCH",
            ):
                _validate_invocation(
                    replace(
                        invocation,
                        runner_output_execution_mode="UNKNOWN_MODE",
                    )
                )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_STAGE_RUNNER_SCHEMA_MISMATCH",
            ):
                _validate_invocation(
                    replace(
                        invocation,
                        runner_output_schema_variants_sha256="0" * 64,
                    )
                )

    def test_jsonl_allows_large_structured_inbound_but_caps_outbound(self) -> None:
        payload = {"candidate": "x" * (MAX_OUTBOUND_JSONL_BYTES + 1024)}
        encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        self.assertLess(len(encoded), MAX_INBOUND_JSONL_BYTES)

        session = JsonlRpcSession.__new__(JsonlRpcSession)
        session.process = SimpleNamespace(stdout=object(), stdin=BytesIO())
        session._read_buffer = bytearray(encoded)

        self.assertEqual(session._read(time.monotonic() + 1), payload)
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "OUTBOUND_MESSAGE_TOO_LARGE",
        ):
            session._write(payload)

    def test_jsonl_dynamic_server_request_requires_explicit_experimental_handler(
        self,
    ) -> None:
        message = {
            "method": "item/tool/call",
            "id": 77,
            "params": {"tool": "reqpilotx_test_tool"},
        }
        session = JsonlRpcSession.__new__(JsonlRpcSession)
        session.process = SimpleNamespace(stdout=object(), stdin=BytesIO())
        session._experimental_api = False
        session._server_request_handler = None
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "SERVER_REQUEST_FORBIDDEN",
        ):
            session._handle_server_request(message)

        session._experimental_api = True
        session._server_request_handler = lambda _message: {
            "success": True,
            "contentItems": [{"type": "inputText", "text": "{}"}],
        }
        session._handle_server_request(message)
        response = json.loads(session.process.stdin.getvalue().decode("utf-8"))
        self.assertEqual(response["id"], 77)
        self.assertTrue(response["result"]["success"])

    def test_runs_exact_native_read_turn_and_returns_content_free_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            opened: list[_FakeJsonlStageSession] = []
            verified: list[str] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                )
                opened.append(session)
                return session

            transport = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                session_opener=open_session,
                thread_absence_verifier=(
                    lambda _runtime, thread_id, _timeout: verified.append(thread_id)
                ),
            )
            result = transport.invoke(invocation)

            session = opened[0]
            turn_params = session.params_for("turn/start")
            thread_params = session.params_for("thread/start")
            self.assertEqual(turn_params["model"], "gpt-5.6-sol")
            self.assertEqual(turn_params["effort"], "high")
            self.assertEqual(turn_params["outputSchema"], invocation.turn_output_schema)
            self.assertNotIn("sandboxPolicy", turn_params)
            self.assertEqual(turn_params["approvalPolicy"], "never")
            self.assertEqual(turn_params["input"], list(invocation.input_items))
            self.assertEqual(
                thread_params["developerInstructions"],
                invocation.developer_context,
            )
            self.assertNotIn("sandbox", thread_params)
            self.assertNotIn("permissions", thread_params)
            self.assertNotIn("runtimeWorkspaceRoots", thread_params)
            self.assertIn("thread/delete", session.methods)
            self.assertTrue(session.gracefully_stopped)
            self.assertEqual(verified, ["thread-stage-1"])
            self.assertEqual(result.structured_output, {"marker": "OK"})
            self.assertEqual(
                result.response_sha256,
                canonical_sha256({"marker": "OK"}),
            )
            self.assertEqual(result.runtime_thread_id, "thread-stage-1")
            self.assertEqual(result.runtime_turn_id, "turn-stage-1")
            self.assertEqual(
                result.tool_activity_summary["skill_inputs"],
                {
                    "primary_skill_id": "reqpilotx-document-understanding",
                    "capability_skill_ids": ["pdf"],
                    "skill_count": 2,
                    "closure_sha256": canonical_sha256(
                        {
                            item["name"]: hashlib.sha256(
                                Path(item["path"]).read_bytes()
                            ).hexdigest()
                            for item in invocation.input_items
                            if item["type"] == "skill"
                        }
                    ),
                },
            )
            summary = json.dumps(
                session.ledger.content_free_summary(),
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn("SECRET_REASONING", summary)
            self.assertNotIn("SECRET_PLAN", summary)
            self.assertNotIn("SECRET_COMMAND", summary)
            self.assertNotIn("SECRET_OUTPUT", summary)
            self.assertNotIn(str(_image_path(invocation)), summary)
            self.assertEqual(
                session.ledger.content_free_summary()["unique_item_counts"],
                {
                    "agentMessage": 1,
                    "commandExecution": 1,
                    "contextCompaction": 1,
                    "imageView": 1,
                    "plan": 1,
                    "reasoning": 1,
                    "userMessage": 1,
                },
            )

    def test_completed_thread_snapshot_reconciles_missing_reasoning_notification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            opened: list[_FakeJsonlStageSession] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                    snapshot_reasoning_completion=True,
                )
                opened.append(session)
                return session

            result = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                session_opener=open_session,
            ).invoke(invocation)

            methods = opened[0].methods
            self.assertEqual(result.structured_output, {"marker": "OK"})
            self.assertLess(
                methods.index("thread/read"), methods.index("thread/delete")
            )
            self.assertEqual(
                opened[0].ledger.content_free_summary()[
                    "snapshot_reconciled_item_type_counts"
                ],
                {"reasoning": 1},
            )

    def test_terminal_orphaned_reasoning_is_recorded_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            opened: list[_FakeJsonlStageSession] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                    drop_reasoning_completion=True,
                )
                opened.append(session)
                return session

            result = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                session_opener=open_session,
            ).invoke(invocation)

            self.assertEqual(result.structured_output, {"marker": "OK"})
            self.assertIn("thread/read", opened[0].methods)
            self.assertIn("thread/delete", opened[0].methods)
            self.assertEqual(
                opened[0].ledger.content_free_summary()[
                    "terminal_orphaned_item_type_counts"
                ],
                {"reasoning": 1},
            )

    def test_terminal_open_command_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            opened: list[_FakeJsonlStageSession] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                    drop_command_completion=True,
                )
                opened.append(session)
                return session

            with self.assertRaisesRegex(
                LifecycleProbeError,
                "ITEM_LIFECYCLE_INCOMPLETE_COMMAND_EXECUTION",
            ):
                LocalCodexAppServerStructuredStageTransport(
                    timeout_seconds=5,
                    session_opener=open_session,
                ).invoke(invocation)

            self.assertIn("thread/read", opened[0].methods)
            self.assertIn("thread/delete", opened[0].methods)

    def test_restricted_root_fallback_keeps_legacy_sandbox_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw), permission_profile=False)
            opened: list[_FakeJsonlStageSession] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                )
                opened.append(session)
                return session

            result = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                session_opener=open_session,
            ).invoke(invocation)

            session = opened[0]
            self.assertEqual(
                session.params_for("thread/start")["sandbox"],
                "read-only",
            )
            self.assertEqual(
                session.params_for("turn/start")["sandboxPolicy"],
                invocation.turn_sandbox_policy,
            )
            self.assertEqual(result.structured_output, {"marker": "OK"})

    def test_effective_config_drift_fails_before_thread_or_inference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            opened: list[_FakeJsonlStageSession] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                    invalid_config=True,
                )
                opened.append(session)
                return session

            transport = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                session_opener=open_session,
            )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "AGENT_NATIVE_AMBIENT_CAPABILITY_ENABLED",
            ):
                transport.invoke(invocation)

            self.assertNotIn("thread/start", opened[0].methods)
            self.assertNotIn("turn/start", opened[0].methods)
            self.assertTrue(opened[0].force_closed)

    def test_protocol_without_any_read_confinement_fails_before_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            unsupported_runtime = replace(
                invocation.runtime,
                protocol={
                    **invocation.runtime.protocol,
                    "permission_profiles": "false",
                    "restricted_read_access": "false",
                },
            )
            unsupported = replace(invocation, runtime=unsupported_runtime)
            opened: list[object] = []
            transport = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                session_opener=lambda *_args: opened.append(object()),  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_NATIVE_READ_RESTRICTED_ROOT_UNSUPPORTED",
            ):
                transport.invoke(unsupported)
            self.assertEqual(opened, [])

    def test_active_permission_profile_mismatch_fails_and_deletes_thread(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            opened: list[_FakeJsonlStageSession] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                    invalid_permission_profile=True,
                )
                opened.append(session)
                return session

            transport = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                session_opener=open_session,
            )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_STAGE_PERMISSION_PROFILE_MISMATCH",
            ):
                transport.invoke(invocation)

            session = opened[0]
            self.assertNotIn("turn/start", session.methods)
            self.assertIn("thread/delete", session.methods)
            self.assertTrue(session.force_closed)

    def test_rejects_file_web_mcp_and_approval_events(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            forbidden = (
                (
                    {
                        "method": "item/started",
                        "params": _item_params(
                            {
                                "id": "forbidden-1",
                                "type": "fileChange",
                                "status": "inProgress",
                            }
                        ),
                    },
                    "CANONICAL_STAGE_FILE_CHANGE_FORBIDDEN",
                ),
                (
                    {
                        "method": "item/started",
                        "params": _item_params(
                            {"id": "forbidden-2", "type": "webSearch"}
                        ),
                    },
                    "CANONICAL_STAGE_WEB_EVENT_FORBIDDEN",
                ),
                (
                    {
                        "method": "item/started",
                        "params": _item_params(
                            {"id": "forbidden-3", "type": "mcpToolCall"}
                        ),
                    },
                    "CANONICAL_STAGE_MCP_EVENT_FORBIDDEN",
                ),
                (
                    {
                        "method": "item/started",
                        "params": _item_params(
                            {
                                "id": "forbidden-dynamic",
                                "type": "dynamicToolCall",
                                "tool": "reqpilotx_test_tool",
                                "namespace": None,
                                "arguments": {},
                                "status": "inProgress",
                            }
                        ),
                    },
                    "CANONICAL_STAGE_DYNAMIC_TOOL_FORBIDDEN",
                ),
                (
                    {
                        "method": "item/commandExecution/requestApproval",
                        "id": 99,
                        "params": {
                            "threadId": "thread-stage-1",
                            "turnId": "turn-stage-1",
                        },
                    },
                    "CANONICAL_STAGE_APPROVAL_REQUEST_FORBIDDEN",
                ),
            )
            for event, reason in forbidden:
                with self.subTest(reason=reason):
                    ledger = CanonicalStageEventLedger(
                        readable_root=invocation.runtime.sterile_cwd
                    )
                    with self.assertRaisesRegex(LifecycleProbeError, reason):
                        ledger.observe(event)

    def test_timeout_interrupts_and_deletes_the_exact_thread(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            opened: list[_FakeJsonlStageSession] = []
            verified: list[str] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                    timeout_on_wait=True,
                )
                opened.append(session)
                return session

            transport = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=0.1,
                session_opener=open_session,
                thread_absence_verifier=(
                    lambda _runtime, thread_id, _timeout: verified.append(thread_id)
                ),
            )
            with self.assertRaisesRegex(LifecycleProbeError, "APP_SERVER_TIMEOUT"):
                transport.invoke(invocation)

            session = opened[0]
            self.assertIn("turn/interrupt", session.methods)
            self.assertIn("thread/delete", session.methods)
            self.assertIn("thread-stage-1", session.ledger.deleted_thread_ids)
            self.assertEqual(verified, ["thread-stage-1"])

    def test_cancellation_interrupts_and_deletes_the_exact_thread(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            cancel = {"requested": False}
            opened: list[_FakeJsonlStageSession] = []
            verified: list[str] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                    cancel_state=cancel,
                )
                opened.append(session)
                return session

            transport = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                cancellation_requested=lambda: cancel["requested"],
                session_opener=open_session,
                thread_absence_verifier=(
                    lambda _runtime, thread_id, _timeout: verified.append(thread_id)
                ),
            )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_STAGE_CANCELLED",
            ):
                transport.invoke(invocation)

            session = opened[0]
            self.assertIn("thread/delete", session.methods)
            self.assertIn("thread-stage-1", session.ledger.deleted_thread_ids)
            self.assertEqual(verified, ["thread-stage-1"])

    def test_workspace_mutation_is_rejected_after_thread_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            opened: list[_FakeJsonlStageSession] = []

            def open_session(runtime, ledger, timeout):  # type: ignore[no-untyped-def]
                session = _FakeJsonlStageSession(
                    invocation=invocation,
                    ledger=ledger,
                    mutate_workspace=True,
                )
                opened.append(session)
                return session

            transport = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=5,
                session_opener=open_session,
            )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "MODEL_WORKSPACE_MUTATED",
            ):
                transport.invoke(invocation)

            session = opened[0]
            self.assertIn("thread/delete", session.methods)
            self.assertIn("thread-stage-1", session.ledger.deleted_thread_ids)

    def test_local_image_and_shell_paths_cannot_escape_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            invocation = _invocation(Path(raw))
            ledger = CanonicalStageEventLedger(
                readable_root=invocation.runtime.sterile_cwd
            )
            outside = invocation.runtime.state_path / "outside.png"
            outside.write_bytes(b"outside")
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_STAGE_IMAGE_PATH_OUTSIDE_ROOT",
            ):
                ledger.observe(
                    {
                        "method": "item/started",
                        "params": _item_params(
                            {
                                "id": "image-out",
                                "type": "imageView",
                                "path": str(outside),
                            }
                        ),
                    }
                )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "CANONICAL_STAGE_COMMAND_CWD_OUTSIDE_ROOT",
            ):
                ledger.observe(
                    {
                        "method": "item/started",
                        "params": _item_params(
                            {
                                "id": "command-out",
                                "type": "commandExecution",
                                "command": "pwd",
                                "cwd": str(invocation.runtime.state_path),
                                "status": "inProgress",
                            }
                        ),
                    }
                )


@dataclass
class _FakeDynamicToolHost:
    supported_stages: tuple[AgentExtractionStage, ...]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._specs = (
            CanonicalDynamicToolSpec(
                name="reqpilotx_test_tool",
                description="Read one synthetic scope.",
                input_schema={
                    "type": "object",
                    "properties": {"scope": {"type": "string"}},
                    "required": ["scope"],
                    "additionalProperties": False,
                },
            ),
        )

    @property
    def specs(self) -> tuple[CanonicalDynamicToolSpec, ...]:
        return self._specs

    def invoke(self, *, tool_name, arguments):  # type: ignore[no-untyped-def]
        self.calls.append((tool_name, dict(arguments)))
        return CanonicalDynamicToolExecution(
            success=True,
            result={"marker": "SECRET_DYNAMIC_RESULT"},
        )


@dataclass
class _FakeJsonlStageSession:
    invocation: CanonicalStageInvocation
    ledger: CanonicalStageEventLedger
    invalid_config: bool = False
    invalid_permission_profile: bool = False
    timeout_on_wait: bool = False
    cancel_state: dict[str, bool] | None = None
    mutate_workspace: bool = False
    late_reasoning_completion: bool = False
    snapshot_reasoning_completion: bool = False
    drop_reasoning_completion: bool = False
    drop_command_completion: bool = False
    emit_dynamic_tool: bool = False

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.force_closed = False
        self.gracefully_stopped = False
        self._interrupt_requested = False
        self._timed_out = False
        self._late_reasoning_item: dict[str, object] | None = None
        self._snapshot_reasoning_item: dict[str, object] | None = None
        self._dynamic_server_request_handler = None
        self.dynamic_server_requests_configured = False

    def configure_dynamic_server_requests(self, handler):  # type: ignore[no-untyped-def]
        self._dynamic_server_request_handler = handler
        self.dynamic_server_requests_configured = True

    @property
    def methods(self) -> list[str]:
        return [method for method, _params in self.calls]

    def params_for(self, method: str) -> dict[str, object]:
        return next(params for name, params in self.calls if name == method)

    def initialize(self) -> dict[str, object]:
        return {
            "codexHome": str(self.invocation.runtime.codex_home),
            "platformFamily": "unix",
            "platformOs": "macos",
            "userAgent": "fake-jsonl-stage-session",
        }

    def request(
        self,
        method: str,
        params: dict[str, object] | object,
    ) -> dict[str, object]:
        values = dict(params)  # type: ignore[arg-type]
        self.calls.append((method, values))
        if method == "account/read":
            return {
                "account": {"type": "chatgpt", "planType": "test"},
                "requiresOpenaiAuth": False,
            }
        if method == "model/list":
            return {
                "data": [
                    {
                        "id": self.invocation.session_attestation.model_id,
                        "inputModalities": ["text", "image"],
                    }
                ],
                "nextCursor": None,
            }
        if method == "config/read":
            readable_roots = tuple(
                Path(item)
                for item in self.invocation.turn_sandbox_policy["access"][
                    "readableRoots"
                ]
            )
            config = _effective_config(
                permission_profile=(
                    self.invocation.session_attestation.native_runtime_policy
                    is not None
                    and self.invocation.session_attestation.native_runtime_policy.confinement_mode
                    == "PERMISSION_PROFILE"
                ),
                readable_roots=readable_roots,
            )
            if self.invalid_config:
                config["config"]["features"]["apps"] = True
            return config
        if method == "mcpServerStatus/list":
            return {"data": [], "nextCursor": None}
        if method == "skills/list":
            skills = tuple(
                item
                for item in self.invocation.input_items
                if item["type"] == "skill"
            )
            return {
                "data": [
                    {
                        "cwd": str(self.invocation.runtime.sterile_cwd),
                        "errors": [],
                        "skills": [
                            {
                                "name": skill["name"],
                                "path": skill["path"],
                                "enabled": True,
                            }
                            for skill in skills
                        ],
                    }
                ]
            }
        if method == "thread/start":
            self.ledger.observe(
                {
                    "method": "thread/started",
                    "params": {"thread": {"id": "thread-stage-1"}},
                }
            )
            response = {
                "model": self.invocation.session_attestation.model_id,
                "cwd": str(self.invocation.runtime.sterile_cwd),
                "approvalPolicy": "never",
                "instructionSources": [],
                "thread": {
                    "id": "thread-stage-1",
                    "cwd": str(self.invocation.runtime.sterile_cwd),
                    "ephemeral": False,
                },
            }
            native_policy = self.invocation.session_attestation.native_runtime_policy
            if (
                native_policy is not None
                and native_policy.confinement_mode == "PERMISSION_PROFILE"
            ):
                response.update(
                    {
                        "activePermissionProfile": {
                            "id": (
                                "wrong-profile"
                                if self.invalid_permission_profile
                                else AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
                            ),
                            "extends": None,
                        },
                        "sandbox": {
                            "type": "dangerFullAccess",
                            "networkAccess": True,
                        },
                    }
                )
            else:
                response["sandbox"] = {
                    "type": "readOnly",
                    "networkAccess": False,
                }
            return response
        if method == "turn/start":
            self.ledger.observe(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "thread-stage-1",
                        "turn": {
                            "id": "turn-stage-1",
                            "status": "inProgress",
                            "items": [],
                            "error": None,
                        },
                    },
                }
            )
            return {
                "turn": {
                    "id": "turn-stage-1",
                    "status": "inProgress",
                    "items": [],
                    "error": None,
                }
            }
        if method == "turn/interrupt":
            self._interrupt_requested = True
            return {}
        if method == "thread/read":
            if self._late_reasoning_item is not None:
                self.ledger.observe(
                    {
                        "method": "item/completed",
                        "params": _item_params(self._late_reasoning_item),
                    }
                )
                self._late_reasoning_item = None
            return {
                "thread": {
                    "id": "thread-stage-1",
                    "turns": [
                        {
                            "id": "turn-stage-1",
                            "status": "completed",
                            "itemsView": "full",
                            "items": (
                                []
                                if self._snapshot_reasoning_item is None
                                else [self._snapshot_reasoning_item]
                            ),
                        }
                    ],
                }
            }
        if method == "thread/delete":
            self.ledger.observe(
                {
                    "method": "thread/deleted",
                    "params": {"threadId": "thread-stage-1"},
                }
            )
            return {}
        raise AssertionError(f"unexpected fake request: {method}")

    def wait_for_turn(
        self,
        turn_id: str,
        *,
        cancellation_requested=None,  # type: ignore[no-untyped-def]
    ) -> str:
        if (
            self.timeout_on_wait
            and not self._timed_out
            and not self._interrupt_requested
        ):
            self._timed_out = True
            raise LifecycleProbeError("APP_SERVER_TIMEOUT")
        if self.cancel_state is not None and not self._interrupt_requested:
            self.cancel_state["requested"] = True
        if self._interrupt_requested or (
            cancellation_requested is not None and cancellation_requested()
        ):
            self._emit_turn_completed(turn_id, "interrupted")
            return "interrupted"
        self._emit_success_items()
        if self.mutate_workspace:
            (self.invocation.runtime.sterile_cwd / "forbidden-write.txt").write_text(
                "mutation",
                encoding="utf-8",
            )
        self._emit_turn_completed(turn_id, "completed")
        return "completed"

    def wait_for_thread_deleted(self, thread_id: str) -> None:
        if thread_id not in self.ledger.deleted_thread_ids:
            raise AssertionError("fake thread was not deleted")

    def drain_notifications_until(
        self,
        predicate,
        *,
        timeout_seconds: float,
    ) -> None:  # type: ignore[no-untyped-def]
        if not predicate():
            raise LifecycleProbeError("APP_SERVER_TIMEOUT")

    def stop_gracefully(self, *, timeout_seconds: float = 3.0) -> None:
        self.gracefully_stopped = True

    def close(self, *, force: bool = False) -> None:
        self.force_closed = self.force_closed or force

    def _emit_success_items(self) -> None:
        image = str(_image_path(self.invocation))
        if self.emit_dynamic_tool:
            dynamic_started = {
                "id": "dynamic-1",
                "type": "dynamicToolCall",
                "tool": "reqpilotx_test_tool",
                "namespace": None,
                "arguments": {"scope": "one"},
                "status": "inProgress",
            }
            self.ledger.observe(
                {
                    "method": "item/started",
                    "params": _item_params(dynamic_started),
                }
            )
            if self._dynamic_server_request_handler is None:
                raise AssertionError("dynamic server request handler is missing")
            response = self._dynamic_server_request_handler(
                {
                    "method": "item/tool/call",
                    "id": 91,
                    "params": {
                        "threadId": "thread-stage-1",
                        "turnId": "turn-stage-1",
                        "callId": "dynamic-1",
                        "tool": "reqpilotx_test_tool",
                        "namespace": None,
                        "arguments": {"scope": "one"},
                    },
                }
            )
            self.ledger.observe(
                {
                    "method": "item/completed",
                    "params": _item_params(
                        {
                            **dynamic_started,
                            "status": (
                                "completed" if response["success"] else "failed"
                            ),
                            "success": response["success"],
                            "contentItems": response["contentItems"],
                            "durationMs": 1,
                        }
                    ),
                }
            )
        items = (
            {
                "id": "user-1",
                "type": "userMessage",
                "content": [{"type": "text", "text": "SECRET_USER_INPUT"}],
            },
            {
                "id": "reason-1",
                "type": "reasoning",
                "summary": ["SECRET_REASONING"],
                "content": ["SECRET_RAW_REASONING"],
            },
            {
                "id": "plan-1",
                "type": "plan",
                "text": "SECRET_PLAN",
            },
            {
                "id": "compaction-1",
                "type": "contextCompaction",
            },
            {
                "id": "command-1",
                "type": "commandExecution",
                "command": "SECRET_COMMAND",
                "cwd": str(self.invocation.runtime.sterile_cwd),
                "status": "inProgress",
            },
            {"id": "image-1", "type": "imageView", "path": image},
            {
                "id": "agent-1",
                "type": "agentMessage",
                "text": '{"marker":"OK"}',
                "phase": "final_answer",
            },
        )
        for item in items:
            self.ledger.observe(
                {"method": "item/started", "params": _item_params(item)}
            )
        self.ledger.observe(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "threadId": "thread-stage-1",
                    "turnId": "turn-stage-1",
                    "itemId": "command-1",
                    "delta": "SECRET_OUTPUT",
                },
            }
        )
        for item in items:
            completed = dict(item)
            if item["type"] == "commandExecution":
                completed["status"] = "completed"
                completed["aggregatedOutput"] = "SECRET_OUTPUT"
            if item["type"] == "reasoning" and (
                self.late_reasoning_completion
                or self.snapshot_reasoning_completion
                or self.drop_reasoning_completion
            ):
                if self.late_reasoning_completion:
                    self._late_reasoning_item = completed
                if self.snapshot_reasoning_completion:
                    self._snapshot_reasoning_item = completed
                continue
            if item["type"] == "commandExecution" and self.drop_command_completion:
                continue
            self.ledger.observe(
                {"method": "item/completed", "params": _item_params(completed)}
            )

    def _emit_turn_completed(self, turn_id: str, status: str) -> None:
        if turn_id in self.ledger.terminal_turns:
            return
        self.ledger.observe(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-stage-1",
                    "turn": {"id": turn_id, "status": status},
                },
            }
        )


def _invocation(
    state: Path,
    *,
    permission_profile: bool = True,
) -> CanonicalStageInvocation:
    root = state / "session-root"
    root.mkdir(mode=0o700)
    kit_root = root / "agent-kit"
    kit_root.mkdir(mode=0o700)
    codex_home = state / "codex-home"
    codex_home.mkdir(mode=0o700)
    sqlite = state / "sqlite"
    sqlite.mkdir(mode=0o700)
    logs = state / "logs"
    logs.mkdir(mode=0o700)
    tmp = state / "tmp"
    tmp.mkdir(mode=0o700)
    dependencies_root = state / "primary-runtime-dependencies"
    for path in (
        dependencies_root / "bin/override",
        dependencies_root / "python/bin",
        dependencies_root / "bin/fallback",
    ):
        path.mkdir(parents=True, exist_ok=True)
    dependencies_root = dependencies_root.resolve()
    skill_id = "reqpilotx-document-understanding"
    skill_path = kit_root / f".agents/skills/{skill_id}/SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        f"---\nname: {skill_id}\n---\nSynthetic stage Skill.\n",
        encoding="utf-8",
    )
    capability_id = "pdf"
    capability_path = kit_root / f".agents/skills/{capability_id}/SKILL.md"
    capability_path.parent.mkdir(parents=True)
    capability_path.write_text(
        f"---\nname: {capability_id}\n---\nSynthetic PDF capability Skill.\n",
        encoding="utf-8",
    )
    image_path = root / "evidence-workspace/contact-sheet.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"synthetic-image")
    runtime = CanaryRuntimeState(
        codex_home=codex_home.resolve(),
        command=("fake-codex", "app-server"),
        environment={
            "PATH": ":".join(
                (
                    str(dependencies_root / "bin/override"),
                    str(dependencies_root / "python/bin"),
                    str(dependencies_root / "bin/fallback"),
                    "/usr/bin:/bin",
                )
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        },
        log_dir=logs.resolve(),
        protocol={
            "codex_version": "fake",
            "permission_profiles": str(permission_profile).lower(),
            "restricted_read_access": "true",
            "dynamic_tools": "true",
        },
        sqlite_home=sqlite.resolve(),
        state_path=state.resolve(),
        sterile_cwd=kit_root.resolve(),
        tmp_dir=tmp.resolve(),
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"marker": {"const": "OK"}},
        "required": ["marker"],
        "additionalProperties": False,
    }
    policy_path = kit_root / "policy/extraction-policy.v1.json"
    policy_path.parent.mkdir()
    policy_path.write_text('{"policy":"synthetic"}\n', encoding="utf-8")
    schema_path = kit_root / "schemas/document-understanding-proposal.v2.schema.json"
    schema_path.parent.mkdir()
    schema_path.write_text(
        json.dumps(schema, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    skill_sha = hashlib.sha256(skill_path.read_bytes()).hexdigest()
    capability_sha = hashlib.sha256(capability_path.read_bytes()).hexdigest()
    policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    schema_sha = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    stage = AgentExtractionStage.DOCUMENT_UNDERSTANDING
    capability_values = {
        "skill_id": capability_id,
        "skill_file": CanonicalStageFileDigestV1(
            path=f".agents/skills/{capability_id}/SKILL.md",
            sha256=capability_sha,
        ),
    }
    capability_draft = CanonicalStageCapabilitySkillAttestationV1.model_construct(
        **capability_values,
        attestation_sha256="0" * 64,
    )
    capability_attestation = CanonicalStageCapabilitySkillAttestationV1(
        **capability_values,
        attestation_sha256=canonical_sha256(
            capability_draft.model_dump(
                mode="json",
                exclude={"attestation_sha256"},
            )
        ),
    )
    stage_attestation = CanonicalAppServerStageAttestationV1(
        stage=stage,
        implementation_status="AVAILABLE",
        context_kind="EXECUTABLE_STAGE_CONTEXT",
        skill_id=skill_id,
        skill=CanonicalStageFileDigestV1(
            path=f".agents/skills/{skill_id}/SKILL.md",
            sha256=skill_sha,
        ),
        capability_skills=(capability_attestation,),
        policy=CanonicalStageFileDigestV1(
            path="policy/extraction-policy.v1.json",
            sha256=policy_sha,
        ),
        references=(),
        context_sha256="2" * 64,
        artifact_schema_id="document-understanding-proposal/v2",
        artifact_schema=CanonicalStageFileDigestV1(
            path="schemas/document-understanding-proposal.v2.schema.json",
            sha256=schema_sha,
        ),
        runner_output_schema_id="document-understanding-proposal/v2",
        runner_output_schema=CanonicalStageFileDigestV1(
            path="schemas/document-understanding-proposal.v2.schema.json",
            sha256=schema_sha,
        ),
        validator_id="reqpilotx-document-understanding-validator",
        validator_version="1.0.0",
        required_tools=(),
        inference_available=True,
        diagnostic_codes=(),
    )
    kit_identity = AgentKitIdentityV1(
        kit_id="reqpilotx-document-extraction",
        kit_version="9.9.9",
        manifest_sha256="4" * 64,
        bundle_sha256="5" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-tools",
        tool_registry_version="1.0.0",
    )
    pdf_runtime = _pdf_runtime_attestation(dependencies_root)
    dependency_root_sha256 = hashlib.sha256(
        str(dependencies_root.resolve()).encode("utf-8")
    ).hexdigest()
    attestation_values = {
        "session_id": "canonical-document-session-" + "6" * 32,
        "content_free": True,
        "kit_identity": kit_identity,
        "runner_profile_id": "app-server-product",
        "runner_profile_sha256": "7" * 64,
        "runner_kind": AgentRunnerKind.APP_SERVER_PRODUCT,
        "adapter_id": "codex-app-server",
        "adapter_version": "1.0.0",
        "model_id": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "domain_logic_source": "PORTABLE_AGENT_KIT",
        "exact_bundle_materialized": True,
        "private_runtime_state": True,
        "runtime_assets_copied": False,
        "agent_native_read_capability": CanonicalAgentNativeReadCapabilityV1(
            status="ATTESTED",
            shell_tool_enabled=True,
            unified_exec_enabled=True,
            view_image_enabled=True,
            local_image_input_enabled=True,
            session_root_read_confinement_attested=True,
            network_access_enabled=False,
            filesystem_write_enabled=False,
            real_transport_eligible=True,
            diagnostic_codes=(),
        ),
        "native_runtime_policy": AgentNativeReadOnlyRuntimePolicyV1(
            confinement_mode=(
                "PERMISSION_PROFILE"
                if permission_profile
                else "READ_ONLY_RESTRICTED_ROOT"
            ),
            permission_profile_id=(
                AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
                if permission_profile
                else None
            ),
            readable_root_path_sha256=hashlib.sha256(
                str(root.resolve()).encode("utf-8")
            ).hexdigest(),
            additional_readable_root_path_sha256s=(dependency_root_sha256,),
            filesystem_access=(
                "SESSION_ROOT_READ_ONLY_WITH_ATTESTED_DEPENDENCIES"
            ),
        ),
        "pdf_primary_runtime": pdf_runtime,
        "document_evidence": CanonicalDocumentEvidenceAttestationV1(
            run_id="agent-run-" + "8" * 32,
            opaque_source_handle="source-handle-" + "9" * 32,
            source_sha256="a" * 64,
            document_role="SYNTHETIC_TEST",
            page_count=1,
            source_ir_id="source-ir-synthetic",
            source_ir_canonical_sha256="b" * 64,
            workspace_bundle_sha256="c" * 64,
            evidence_inventory_sha256="d" * 64,
        ),
        "stage_predecessors": (),
        "workspace_manifest_sha256": canonical_sha256(_workspace_manifest(root)),
        "workspace_manifest_unchanged": True,
        "stages": (stage_attestation,),
        "d3_policy_closure_sha256": None,
    }
    draft = CanonicalAppServerDocumentSessionAttestationV1.model_construct(
        attestation_id="canonical-session-attestation-" + "0" * 32,
        **attestation_values,
    )
    payload = draft.model_dump(mode="json", exclude={"attestation_id"})
    attestation = CanonicalAppServerDocumentSessionAttestationV1(
        attestation_id=(
            "canonical-session-attestation-" + canonical_sha256(payload)[:32]
        ),
        **attestation_values,
    )
    sandbox_policy = {
        "type": "readOnly",
        "access": {
            "type": "restricted",
            "includePlatformDefaults": True,
            "readableRoots": [
                str(root.resolve()),
                str(dependencies_root.resolve()),
            ],
        },
    }
    return CanonicalStageInvocation(
        stage=stage,
        runtime=runtime,
        developer_context="REQPILOTX_SYNTHETIC_STAGE_POLICY\n",
        input_items=(
            {
                "type": "text",
                "text": (
                    f"${skill_id} ${capability_id}\n"
                    "REQPILOTX_CANONICAL_STAGE_TURN_V1\n"
                    "Return the structured result."
                ),
            },
            {"type": "skill", "name": skill_id, "path": str(skill_path.resolve())},
            {
                "type": "skill",
                "name": capability_id,
                "path": str(capability_path.resolve()),
            },
            {"type": "localImage", "path": str(image_path.resolve())},
        ),
        turn_output_schema_id="document-understanding-proposal/v2",
        turn_output_schema=schema,
        turn_output_schema_sha256=canonical_sha256(schema),
        final_artifact_schema_id="document-understanding-proposal/v2",
        final_artifact_schema_sha256=schema_sha,
        stage_context_sha256="2" * 64,
        task_binding_sha256="f" * 64,
        runner_output_execution_mode=None,
        runner_output_schema_variants_sha256=(
            stage_attestation.runner_output_schema_variants_sha256
        ),
        session_attestation=attestation,
        turn_sandbox_policy=sandbox_policy,
    )


def _image_path(invocation: CanonicalStageInvocation) -> Path:
    return Path(
        next(
            item["path"]
            for item in invocation.input_items
            if item["type"] == "localImage"
        )
    )


def _variant_invocation(root: Path) -> CanonicalStageInvocation:
    invocation = _invocation(root)
    stage = invocation.session_attestation.stages[0]
    primary_schema = stage.runner_output_schema
    assert primary_schema is not None
    variant_path = "schemas/document-understanding-object.v1.schema.json"
    variant_file = invocation.runtime.sterile_cwd / variant_path
    variant_file.write_bytes(
        (invocation.runtime.sterile_cwd / primary_schema.path).read_bytes()
    )
    variant_values = {
        "content_free": True,
        "execution_mode": "OBJECT_BATCH",
        "schema_id": "document-understanding-object/v1",
        "schema_file": CanonicalStageFileDigestV1(
            path=variant_path,
            sha256=hashlib.sha256(variant_file.read_bytes()).hexdigest(),
        ),
    }
    variant_draft = CanonicalStageRunnerOutputSchemaVariantV1.model_construct(
        **variant_values,
        variant_sha256="0" * 64,
    )
    variant = CanonicalStageRunnerOutputSchemaVariantV1(
        **variant_values,
        variant_sha256=canonical_sha256(
            variant_draft.model_dump(mode="json", exclude={"variant_sha256"})
        ),
    )
    variants_root = canonical_sha256([variant.model_dump(mode="json")])
    stage_payload = stage.model_dump(mode="json")
    stage_payload.update(
        {
            "runner_output_schema_variants": [variant.model_dump(mode="json")],
            "runner_output_schema_variants_sha256": variants_root,
        }
    )
    variant_stage = CanonicalAppServerStageAttestationV1.model_validate(stage_payload)
    session_payload = invocation.session_attestation.model_dump(
        mode="json",
        exclude={"attestation_id"},
    )
    session_payload["stages"] = [variant_stage.model_dump(mode="json")]
    readable_root = Path(invocation.turn_sandbox_policy["access"]["readableRoots"][0])
    session_payload["workspace_manifest_sha256"] = canonical_sha256(
        _workspace_manifest(readable_root)
    )
    variant_session = CanonicalAppServerDocumentSessionAttestationV1(
        attestation_id=(
            "canonical-session-attestation-" + canonical_sha256(session_payload)[:32]
        ),
        **session_payload,
    )
    return replace(
        invocation,
        turn_output_schema_id=variant.schema_id,
        runner_output_execution_mode=variant.execution_mode,
        runner_output_schema_variants_sha256=variants_root,
        session_attestation=variant_session,
    )


def _item_params(item: dict[str, object]) -> dict[str, object]:
    return {
        "threadId": "thread-stage-1",
        "turnId": "turn-stage-1",
        "item": item,
    }


def _effective_config(
    *,
    permission_profile: bool,
    readable_roots: tuple[Path, ...],
) -> dict[str, object]:
    disabled = {
        "apps": False,
        "browser_use": False,
        "computer_use": False,
        "hooks": False,
        "image_generation": False,
        "in_app_browser": False,
        "multi_agent": False,
        "plugins": False,
        "remote_plugin": False,
        "skill_search": False,
        "tool_suggest": False,
        "workspace_dependencies": False,
    }
    config: dict[str, object] = {
        "features": {
            **disabled,
            "shell_tool": True,
            "unified_exec": True,
            "view_image": True,
        },
        "mcp_servers": {},
        "approval_policy": "never",
        "sandbox_mode": ("danger-full-access" if permission_profile else "read-only"),
        "allow_login_shell": False,
        "web_search": "disabled",
    }
    if permission_profile:
        config.update(
            {
                "default_permissions": AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID,
                "permissions": {
                    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID: {
                        "description": (
                            AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_DESCRIPTION
                        ),
                        "extends": None,
                        "workspace_roots": {
                            str(item): True for item in readable_roots
                        },
                        "filesystem": {
                            ":minimal": "read",
                            ":workspace_roots": {".": "read"},
                            "glob_scan_max_depth": None,
                        },
                        "network": {
                            "allow_local_binding": None,
                            "allow_upstream_proxy": None,
                            "dangerously_allow_all_unix_sockets": None,
                            "dangerously_allow_non_loopback_proxy": None,
                            "domains": None,
                            "enable_socks5": None,
                            "enable_socks5_udp": None,
                            "enabled": False,
                            "mitm": None,
                            "mode": None,
                            "proxy_url": None,
                            "socks_url": None,
                            "unix_sockets": None,
                        },
                    }
                },
            }
        )
    return {"config": config}


def _pdf_runtime_attestation(
    dependencies_root: Path,
) -> OpenAIPrimaryRuntimePdfAttestationV1:
    values = {
        "distribution_id": "pdf@openai-primary-runtime",
        "distribution_version": "26.826.12353",
        "runtime_bundle_version": "26.826.12353",
        "target_platform": "test",
        "target_arch": "test",
        "runtime_manifest_sha256": "1" * 64,
        "provenance_sha256": "2" * 64,
        "vendored_skill_sha256": "3" * 64,
        "runtime_skill_sha256": "3" * 64,
        "runtime_plugin_manifest_sha256": "4" * 64,
        "dependency_root_path_sha256": hashlib.sha256(
            str(dependencies_root.resolve()).encode("utf-8")
        ).hexdigest(),
        "python_version": "3.12.13",
        "python_executable_sha256": "5" * 64,
        "python_packages": (
            OpenAIPrimaryRuntimePackageV1(name="pdfplumber", version="0.11.9"),
            OpenAIPrimaryRuntimePackageV1(name="pypdf", version="6.10.0"),
            OpenAIPrimaryRuntimePackageV1(name="reportlab", version="4.4.9"),
        ),
        "render_tools": (
            OpenAIPrimaryRuntimeToolV1(
                name="pdfinfo",
                version="26.05.0",
                executable_sha256="6" * 64,
            ),
            OpenAIPrimaryRuntimeToolV1(
                name="pdftoppm",
                version="26.05.0",
                executable_sha256="7" * 64,
            ),
        ),
        "read_only_session": True,
        "dependency_installation_allowed": False,
        "network_access": False,
    }
    draft = OpenAIPrimaryRuntimePdfAttestationV1.model_construct(
        attestation_id="primary-runtime-pdf-" + "0" * 32,
        **values,
    )
    payload = draft.model_dump(mode="json", exclude={"attestation_id"})
    return OpenAIPrimaryRuntimePdfAttestationV1(
        attestation_id="primary-runtime-pdf-" + canonical_sha256(payload)[:32],
        **values,
    )


if __name__ == "__main__":
    unittest.main()
