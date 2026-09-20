from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pymupdf

from app.adapters.agents.agent_kit_materializer import materialize_agent_kit
from app.adapters.agents.d1_evidence_mcp import (
    D1_MCP_SERVER_NAME,
    D1_MCP_SERVER_VERSION,
    write_d1_mcp_descriptor,
)
from app.adapters.agents.document_evidence_host import D1_TOOL_NAMES
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.adapters.agents.stage_context import compile_agent_stage_context
from app.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    EventLedger,
    LifecycleProbeError,
    _canary_runtime_state,
    _open_runtime_session,
)
from app.adapters.agents.codex_app_server.pdf_skill_canary import (
    _isolate_runtime_skills,
    _verify_skill_allowlist,
    _workspace_manifest,
)
from app.adapters.agents.codex_app_server.read_only_mcp_canary import (
    _preflight_mcp_session,
    configure_read_only_mcp_runtime,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.application.agent_workflow.document_understanding import (
    canonical_json_sha256,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage


D1_RUNTIME_PREFLIGHT_SCHEMA_VERSION = "reqpilotx-d1-runtime-preflight/v1"
_D1_SKILL_ID = "reqpilotx-document-understanding"
_D1_SERVER_MODULE = "app.adapters.agents.d1_evidence_mcp"


def run_d1_runtime_preflight(
    *,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    timeout_seconds: float = 120.0,
) -> dict[str, object]:
    repository_root = repository_root.resolve(strict=True)
    canonical_kit = load_portable_agent_kit(repository_root / "agent-kit")
    profile = canonical_kit.runner_profile("app-server-product")
    receipt: dict[str, object]
    with _canary_runtime_state(
        codex_bin=codex_bin,
        codex_home=codex_home,
        repository_root=repository_root,
    ) as base_runtime:
        materialized = materialize_agent_kit(
            canonical_kit,
            base_runtime.state_path / "portable-agent-kit",
            runner_profile_id="app-server-product",
        )
        kit = load_portable_agent_kit(materialized.root)
        stage_context = compile_agent_stage_context(
            kit,
            AgentExtractionStage.DOCUMENT_UNDERSTANDING,
        )
        skill_paths = {_D1_SKILL_ID: stage_context.skill_path}
        runtime = replace(base_runtime, sterile_cwd=materialized.root)
        runtime, ambient_disabled_count = _isolate_runtime_skills(
            runtime,
            skill_paths=skill_paths,
            timeout_seconds=timeout_seconds,
        )
        source_path, source_sha256 = _build_three_page_fixture(base_runtime)
        descriptor_path = base_runtime.tmp_dir / "d1-mcp-session.json"
        descriptor_sha256 = write_d1_mcp_descriptor(
            descriptor_path,
            source_path=source_path,
            source_sha256=source_sha256,
            document_role="SYNTHETIC_TEST",
            run_id="agent-run-" + "a" * 32,
            opaque_source_handle="source-handle-" + "b" * 32,
            intentional_blank_pages=(3,),
            kit=kit,
            stage_context_sha256=stage_context.context_sha256,
        )
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
        workspace_before = _workspace_manifest(runtime.sterile_cwd)
        ledger = EventLedger()
        session = _open_runtime_session(
            runtime,
            ledger=ledger,
            timeout_seconds=timeout_seconds,
        )
        try:
            platform, account, mcp_summary = _preflight_mcp_session(
                session,
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
                session,
                runtime=runtime,
                expected_paths=skill_paths,
            )
            schema_attestation = _validate_status_schema_attestation(
                session.request(
                    "mcpServerStatus/list",
                    {"limit": 100, "detail": "toolsAndAuthOnly"},
                ),
                kit=kit,
            )
            session.stop_gracefully()
        finally:
            session.close()
        workspace_after = _workspace_manifest(runtime.sterile_cwd)
        if workspace_after != workspace_before:
            raise LifecycleProbeError("MODEL_WORKSPACE_MUTATED")
        receipt = {
            "schema_version": D1_RUNTIME_PREFLIGHT_SCHEMA_VERSION,
            "status": "PASS",
            "quality_gate_eligible": False,
            "quality_gate_reason": "NO_INFERENCE_IDENTITY_AND_CAPABILITY_PREFLIGHT",
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
                "skill_sha256": stage_context.skill_sha256,
                "ambient_disabled_count": ambient_disabled_count,
            },
            "mcp": {
                **mcp_summary,
                "descriptor_sha256": descriptor_sha256,
                "schema_attestation": schema_attestation,
            },
            "fixture": {
                "source_sha256": source_sha256,
                "page_count": 3,
                "intentional_blank_pages": [3],
            },
            "model_inference_started": False,
            "workspace_manifest_unchanged": True,
            "run_temp_cleanup": False,
        }
    receipt["run_temp_cleanup"] = True
    return receipt


def _build_three_page_fixture(
    runtime: CanaryRuntimeState,
) -> tuple[Path, str]:
    two_page = runtime.tmp_dir / "d1-two-page.pdf"
    build_synthetic_pdf(two_page, include_prompt_injection=True)
    source_path = runtime.tmp_dir / "d1-three-page.pdf"
    original = pymupdf.open(two_page)
    combined = pymupdf.open()
    try:
        combined.insert_pdf(original)
        combined.new_page()
        combined.set_metadata(
            {
                "author": "ReqPilotX synthetic canary",
                "creationDate": "D:20260824000000+08'00'",
                "creator": "ReqPilotX Portable D1 fixture",
                "modDate": "D:20260824000000+08'00'",
                "producer": "PyMuPDF",
                "subject": "Non-sensitive D1 runner parity fixture",
                "title": "Portable D1 Synthetic Specification",
            }
        )
        combined.save(
            source_path,
            clean=True,
            deflate=True,
            garbage=4,
            no_new_id=True,
        )
    finally:
        combined.close()
        original.close()
    os.chmod(source_path, 0o600)
    return source_path, hashlib.sha256(source_path.read_bytes()).hexdigest()


def _validate_status_schema_attestation(
    result: object,
    *,
    kit,
) -> dict[str, object]:  # type: ignore[no-untyped-def]
    if not isinstance(result, dict):
        raise LifecycleProbeError("D1_MCP_SCHEMA_STATUS_INVALID")
    data = result.get("data")
    if not isinstance(data, list) or len(data) != 1:
        raise LifecycleProbeError("D1_MCP_SCHEMA_STATUS_INVALID")
    server = data[0]
    if not isinstance(server, dict):
        raise LifecycleProbeError("D1_MCP_SCHEMA_STATUS_INVALID")
    tools = server.get("tools")
    if not isinstance(tools, dict) or set(tools) != set(D1_TOOL_NAMES):
        raise LifecycleProbeError("D1_MCP_SCHEMA_STATUS_INVALID")
    input_hashes: dict[str, str] = {}
    output_schema_visible = True
    for name, tool in tools.items():
        if not isinstance(tool, dict) or not isinstance(tool.get("inputSchema"), dict):
            raise LifecycleProbeError("D1_MCP_SCHEMA_STATUS_INVALID")
        input_schema = tool["inputSchema"]
        expected_spec = next(item for item in kit.tool_registry.tools if item.name == name)
        assert expected_spec.input_schema_path is not None
        expected_input = json.loads(
            (kit.root / expected_spec.input_schema_path).read_text(encoding="utf-8")
        )
        if canonical_json_sha256(input_schema) != canonical_json_sha256(expected_input):
            raise LifecycleProbeError("D1_MCP_INPUT_SCHEMA_DRIFT")
        input_hashes[name] = canonical_json_sha256(input_schema)
        output_schema = tool.get("outputSchema")
        if output_schema is None:
            output_schema_visible = False
        elif not isinstance(output_schema, dict):
            raise LifecycleProbeError("D1_MCP_OUTPUT_SCHEMA_DRIFT")
        else:
            assert expected_spec.output_schema_path is not None
            expected_output = json.loads(
                (kit.root / expected_spec.output_schema_path).read_text(
                    encoding="utf-8"
                )
            )
            if canonical_json_sha256(output_schema) != canonical_json_sha256(
                expected_output
            ):
                raise LifecycleProbeError("D1_MCP_OUTPUT_SCHEMA_DRIFT")
    return {
        "input_schema_count": len(input_hashes),
        "input_schema_manifest_sha256": canonical_json_sha256(input_hashes),
        "output_schema_visible_in_app_server_status": output_schema_visible,
        "direct_server_output_schema_contract": True,
    }


__all__ = ["D1_RUNTIME_PREFLIGHT_SCHEMA_VERSION", "run_d1_runtime_preflight"]
