from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
from typing import Mapping

from app.adapters.agents.agent_kit_materializer import materialize_agent_kit
from app.adapters.agents.d1_app_server_canary import (
    _build_execution_result,
    _d1_turn_input,
    _load_validated_result,
    _validate_final_receipt,
    _validate_synthetic_quality,
)
from app.adapters.agents.d1_evidence_mcp import (
    D1_MCP_SERVER_NAME,
    D1_MCP_SERVER_VERSION,
    D1McpSessionDescriptorV1,
    write_d1_mcp_descriptor,
)
from app.adapters.agents.d1_runtime_preflight import _build_three_page_fixture
from app.adapters.agents.document_evidence_host import D1_TOOL_NAMES
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.adapters.agents.stage_context import compile_agent_stage_context
from app.adapters.agents.codex_app_server.lifecycle import (
    EventLedger,
    LifecycleProbeError,
    _assert_persistent_markers_absent,
    _canary_runtime_state,
    _open_runtime_session,
)
from app.adapters.agents.codex_app_server.pdf_skill_canary import (
    _isolate_runtime_skills,
    _load_schema,
    _parse_agent_json,
    _verify_skill_allowlist,
    _workspace_manifest,
)
from app.adapters.agents.codex_app_server.read_only_mcp_canary import (
    _preflight_mcp_session,
    configure_read_only_mcp_runtime,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    PROMPT_INJECTION_MARKER,
    VISUAL_CANARY_CODE,
)
from app.adapters.agents.codex_app_server.native_reference import (
    _safe_process_error,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentStageFinalReceiptV1,
)


D1_DESKTOP_COLD_CANARY_SCHEMA_VERSION = "reqpilotx-d1-desktop-cold-canary/v1"
_D1_SKILL_ID = "reqpilotx-document-understanding"
_D1_SERVER_MODULE = "app.adapters.agents.d1_evidence_mcp"


def run_d1_desktop_cold_canary(
    *,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    timeout_seconds: float = 300.0,
) -> dict[str, object]:
    repository_root = repository_root.resolve(strict=True)
    canonical_kit = load_portable_agent_kit(repository_root / "agent-kit")
    profile = canonical_kit.runner_profile("desktop-reference")
    receipt: dict[str, object]
    with _canary_runtime_state(
        codex_bin=codex_bin,
        codex_home=codex_home,
        repository_root=repository_root,
    ) as base_runtime:
        materialized = materialize_agent_kit(
            canonical_kit,
            base_runtime.state_path / "portable-agent-kit",
            runner_profile_id="desktop-reference",
        )
        kit = load_portable_agent_kit(materialized.root)
        stage_context = compile_agent_stage_context(
            kit,
            AgentExtractionStage.DOCUMENT_UNDERSTANDING,
        )
        runtime = replace(base_runtime, sterile_cwd=materialized.root)
        runtime, ambient_disabled_count = _isolate_runtime_skills(
            runtime,
            skill_paths={_D1_SKILL_ID: stage_context.skill_path},
            timeout_seconds=timeout_seconds,
        )
        source_path, source_sha256 = _build_three_page_fixture(base_runtime)
        run_id = "agent-run-" + "e" * 32
        source_handle = "source-handle-" + "f" * 32
        descriptor_path = base_runtime.tmp_dir / "d1-mcp-session.json"
        descriptor_sha256 = write_d1_mcp_descriptor(
            descriptor_path,
            source_path=source_path,
            source_sha256=source_sha256,
            document_role="SYNTHETIC_TEST",
            run_id=run_id,
            opaque_source_handle=source_handle,
            intentional_blank_pages=(3,),
            kit=kit,
            stage_context_sha256=stage_context.context_sha256,
        )
        descriptor_payload = json.loads(descriptor_path.read_text(encoding="utf-8"))[
            "descriptor"
        ]
        descriptor = D1McpSessionDescriptorV1.model_validate(descriptor_payload)
        result_path = Path(descriptor.validated_result_path)
        tool_names = tuple(sorted(D1_TOOL_NAMES))
        runtime = configure_read_only_mcp_runtime(
            runtime,
            descriptor_path=descriptor_path,
            repository_root=repository_root,
            enabled_tool_names=tool_names,
            server_module=_D1_SERVER_MODULE,
            server_name=D1_MCP_SERVER_NAME,
            known_tool_names=tool_names,
        )
        output_schema_path = kit.root / "schemas/agent-stage-final-receipt.v1.schema.json"
        _output_schema, output_schema_sha256 = _load_schema(output_schema_path)
        workspace_before = _workspace_manifest(runtime.sterile_cwd)

        preflight_ledger = EventLedger()
        preflight_session = _open_runtime_session(
            runtime,
            ledger=preflight_ledger,
            timeout_seconds=timeout_seconds,
        )
        try:
            platform, account, mcp_summary = _preflight_mcp_session(
                preflight_session,
                runtime=runtime,
                model=profile.model_id,
                descriptor_path=descriptor_path,
                repository_root=repository_root,
                expected_tool_names=tool_names,
                server_module=_D1_SERVER_MODULE,
                server_name=D1_MCP_SERVER_NAME,
                server_version=D1_MCP_SERVER_VERSION,
            )
            _verify_skill_allowlist(
                preflight_session,
                runtime=runtime,
                expected_paths={_D1_SKILL_ID: stage_context.skill_path},
            )
            preflight_session.stop_gracefully()
        finally:
            preflight_session.close()

        final_message_path = base_runtime.tmp_dir / "desktop-final.json"
        prompt = _d1_turn_input(
            source_handle=source_handle,
            stage_context=stage_context.context_text,
            stage_context_sha256=stage_context.context_sha256,
            skill_path=stage_context.skill_path,
        )[0]["text"]
        command = _desktop_exec_command(
            runtime=runtime,
            model_id=profile.model_id,
            reasoning_effort=profile.reasoning_effort,
            output_schema_path=output_schema_path,
            final_message_path=final_message_path,
        )
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=runtime.sterile_cwd,
            env=dict(runtime.environment),
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise LifecycleProbeError(
                "D1_DESKTOP_EXEC_"
                + _safe_process_error(completed.stderr, completed.stdout)
            )
        if not final_message_path.is_file():
            raise LifecycleProbeError("D1_DESKTOP_FINAL_MESSAGE_MISSING")
        final_receipt = AgentStageFinalReceiptV1.model_validate(
            _parse_agent_json(final_message_path.read_text(encoding="utf-8"))
        )
        (
            run_manifest,
            proposal,
            validation_receipt,
            validated_result_sha256,
            tool_sequence,
        ) = _load_validated_result(result_path)
        _validate_final_receipt(
            final_receipt,
            proposal=proposal,
            validation_receipt=validation_receipt,
            validation_receipt_sha256=validation_receipt.receipt_sha256,
            run_id=run_id,
            source_sha256=source_sha256,
            kit_bundle_sha256=kit.identity.bundle_sha256,
        )
        quality = _validate_synthetic_quality(proposal)
        event_summary = _desktop_event_summary(completed.stdout)
        execution_result = _build_execution_result(
            kit=kit,
            profile=profile,
            runner_profile_sha256=materialized.runner_profile_sha256,
            runtime_version=str(runtime.protocol["codex_version"]),
            run_id=run_id,
            source_sha256=source_sha256,
            manifest_payload=run_manifest.model_dump(mode="json"),
            proposal=proposal,
            validation_receipt=validation_receipt,
            validation_receipt_sha256=validation_receipt.receipt_sha256,
            tool_sequence=tool_sequence,
        )
        workspace_after = _workspace_manifest(runtime.sterile_cwd)
        if workspace_after != workspace_before:
            raise LifecycleProbeError("MODEL_WORKSPACE_MUTATED")
        session_inventory = _assert_persistent_markers_absent(
            runtime.codex_home,
            extra_markers=(VISUAL_CANARY_CODE, PROMPT_INJECTION_MARKER),
        )
        receipt = {
            "schema_version": D1_DESKTOP_COLD_CANARY_SCHEMA_VERSION,
            "status": "PASS",
            "quality_gate_eligible": True,
            "model_id": profile.model_id,
            "reasoning_effort": profile.reasoning_effort,
            "account_type": account["account_type"],
            "platform": platform,
            "protocol": dict(runtime.protocol),
            "agent_kit": {
                "kit_id": kit.identity.kit_id,
                "kit_version": kit.identity.kit_version,
                "bundle_sha256": kit.identity.bundle_sha256,
                "runner_profile_sha256": materialized.runner_profile_sha256,
                "stage_context_sha256": stage_context.context_sha256,
            },
            "skills": {
                "enabled": [_D1_SKILL_ID],
                "ambient_disabled_count": ambient_disabled_count,
            },
            "mcp": {
                **mcp_summary,
                "descriptor_sha256": descriptor_sha256,
                "tool_sequence": list(tool_sequence),
            },
            "desktop_events": event_summary,
            "schema_sha256": output_schema_sha256,
            "validated_result_sha256": validated_result_sha256,
            "proposal_sha256": validation_receipt.proposal_sha256,
            "evidence_sha256": validation_receipt.evidence_sha256,
            "quality": quality,
            "execution_result": execution_result.model_dump(mode="json"),
            "workspace_manifest_unchanged": True,
            "codex_home_session_marker_absent": True,
            "codex_home_session_inventory": session_inventory,
            "ephemeral": True,
            "run_temp_cleanup": False,
        }
    receipt["run_temp_cleanup"] = True
    return receipt


def _desktop_exec_command(
    *,
    runtime,
    model_id: str,
    reasoning_effort: str,
    output_schema_path: Path,
    final_message_path: Path,
) -> list[str]:  # type: ignore[no-untyped-def]
    overrides = _config_overrides(runtime.command)
    command = [
        runtime.command[0],
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--skip-git-repo-check",
        "--json",
        "--color",
        "never",
        "--model",
        model_id,
        "--sandbox",
        "read-only",
        "--cd",
        str(runtime.sterile_cwd),
        "--output-schema",
        str(output_schema_path),
        "--output-last-message",
        str(final_message_path),
    ]
    for override in (
        *overrides,
        f'model_reasoning_effort="{reasoning_effort}"',
    ):
        command.extend(("--config", override))
    command.append("-")
    return command


def _config_overrides(command: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    index = 0
    while index < len(command):
        if command[index] == "--config":
            if index + 1 >= len(command):
                raise LifecycleProbeError("D1_DESKTOP_CONFIG_INVALID")
            values.append(command[index + 1])
            index += 2
            continue
        index += 1
    if not values:
        raise LifecycleProbeError("D1_DESKTOP_CONFIG_INVALID")
    return tuple(values)


def _desktop_event_summary(payload: str) -> dict[str, object]:
    event_counts: dict[str, int] = {}
    item_types: dict[str, int] = {}
    tool_names: set[str] = set()
    forbidden = False
    for line in payload.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("type") or "unknown")[:120]
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        item = event.get("item")
        if isinstance(item, Mapping):
            item_type = str(item.get("type") or "unknown")[:120]
            item_types[item_type] = item_types.get(item_type, 0) + 1
            tool = item.get("name") or item.get("tool_name") or item.get("tool")
            if isinstance(tool, str) and tool:
                tool_names.add(tool[:160])
            normalized = f"{item_type} {tool or ''}".casefold()
            if any(
                marker in normalized
                for marker in (
                    "shell",
                    "command_execution",
                    "file_change",
                    "web_search",
                    "browser",
                    "subagent",
                )
            ):
                forbidden = True
    if forbidden:
        raise LifecycleProbeError("D1_DESKTOP_FORBIDDEN_TOOL_EVENT")
    return {
        "event_counts": dict(sorted(event_counts.items())),
        "item_type_counts": dict(sorted(item_types.items())),
        "reported_tool_names": sorted(tool_names),
        "forbidden_tool_event": False,
    }


__all__ = [
    "D1_DESKTOP_COLD_CANARY_SCHEMA_VERSION",
    "run_d1_desktop_cold_canary",
]
