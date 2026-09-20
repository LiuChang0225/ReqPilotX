from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.agents.codex_app_server.native_reference import (
    _canonical_json_sha256,
    _safe_process_error,
    _write_private_json,
)
from app.application.extraction.codex_extraction_parity import (
    strict_structured_output_schema,
)
from app.application.extraction.native_semantic_consensus import (
    build_native_semantic_consensus,
)
from app.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.ports.semantic_provider import (
    RequirementAssemblyDecision,
    RequirementAssemblyProviderResult,
    RequirementAssemblyRequest,
    SemanticClassificationProviderResult,
    SemanticClassificationRequest,
    SemanticDecision,
)


CODEX_CONSENSUS_SEMANTIC_PROVIDER_ID = "codex-native-consensus-semantic"
CODEX_CONSENSUS_SEMANTIC_PROVIDER_VERSION = "1.0.0"


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ClassificationPayload(_StrictPayload):
    decisions: tuple[SemanticDecision, ...] = Field(min_length=1, max_length=512)


class _AssemblyPayload(_StrictPayload):
    decisions: tuple[RequirementAssemblyDecision, ...] = Field(
        min_length=1,
        max_length=512,
    )


class CodexConsensusSemanticProvider:
    """Developer-only text scope adapter implementing the existing semantic port."""

    provider_id = CODEX_CONSENSUS_SEMANTIC_PROVIDER_ID
    provider_version = CODEX_CONSENSUS_SEMANTIC_PROVIDER_VERSION

    def __init__(
        self,
        *,
        repetition_count: int = 3,
        codex_binary: str = "codex",
        model_id: str = "gpt-5.6-sol",
        timeout_seconds: int = 900,
        command_runner: Callable[
            ..., subprocess.CompletedProcess[str]
        ] = subprocess.run,
    ) -> None:
        if repetition_count < 2 or repetition_count > 5:
            raise ValueError("Codex semantic repetition_count must be between 2 and 5")
        self._repetition_count = repetition_count
        self._codex_binary = codex_binary
        self.model_id = model_id
        self._timeout_seconds = timeout_seconds
        self._command_runner = command_runner

    def preflight_classification_request(
        self,
        request: SemanticClassificationRequest,
    ) -> None:
        if request.images:
            raise ValueError("codex_semantic_images_require_mcp_v2")

    def preflight_assembly_request(self, request: RequirementAssemblyRequest) -> None:
        if not request.nodes:
            raise ValueError("codex assembly requires at least one node")

    def classify_nodes(
        self,
        request: SemanticClassificationRequest,
    ) -> SemanticClassificationProviderResult:
        self.preflight_classification_request(request)
        started = datetime.now(timezone.utc)
        runs = tuple(
            self._invoke(
                request.model_dump(mode="json"),
                _ClassificationPayload,
                _classification_prompt(),
            ).decisions
            for _index in range(self._repetition_count)
        )
        decisions, report = build_native_semantic_consensus(request, runs)
        return SemanticClassificationProviderResult(
            decisions=decisions,
            run=self._run_record(
                operation="classify_semantic_nodes",
                prompt_version="native-codex-semantic-classification/v1",
                schema_version="native-codex-semantic-classification/v1",
                request=request.model_dump(mode="json"),
                response={
                    "decisions": [item.model_dump(mode="json") for item in decisions],
                    "consensus": report.model_dump(mode="json"),
                },
                started=started,
                partial=report.unresolved_target_count > 0,
            ),
        )

    def assemble_requirements(
        self,
        request: RequirementAssemblyRequest,
    ) -> RequirementAssemblyProviderResult:
        self.preflight_assembly_request(request)
        started = datetime.now(timezone.utc)
        runs = tuple(
            self._invoke(
                request.model_dump(mode="json"),
                _AssemblyPayload,
                _assembly_prompt(),
            ).decisions
            for _index in range(self._repetition_count)
        )
        decisions = _assembly_consensus(request, runs)
        partial = any(
            item.status is RequirementAssemblyStatus.UNRESOLVED for item in decisions
        )
        return RequirementAssemblyProviderResult(
            decisions=decisions,
            run=self._run_record(
                operation="assemble_requirement_view",
                prompt_version="native-codex-requirement-assembly/v1",
                schema_version="native-codex-requirement-assembly/v1",
                request=request.model_dump(mode="json"),
                response=[item.model_dump(mode="json") for item in decisions],
                started=started,
                partial=partial,
            ),
        )

    def _invoke(
        self,
        request_payload: object,
        payload_model: type[_StrictPayload],
        prompt: str,
    ) -> _StrictPayload:
        with tempfile.TemporaryDirectory(
            prefix="reqpilotx-native-codex-semantic-"
        ) as raw:
            workspace = Path(raw).resolve()
            os.chmod(workspace, 0o700)
            _write_private_json(
                workspace / "scope.json",
                request_payload,
                exclusive=True,
            )
            schema_path = workspace / "output.schema.json"
            _write_private_json(
                schema_path,
                strict_structured_output_schema(payload_model),
                exclusive=True,
            )
            output_path = workspace / "output.json"
            command = [
                self._codex_binary,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--json",
                "--color",
                "never",
                "--model",
                self.model_id,
                "--sandbox",
                "workspace-write",
                "--cd",
                str(workspace),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            completed = self._command_runner(
                command,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "native Codex semantic call failed: "
                    + _safe_process_error(completed.stderr, completed.stdout)
                )
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            _normalize_reason_codes(payload)
            return payload_model.model_validate(payload)

    def _run_record(
        self,
        *,
        operation: str,
        prompt_version: str,
        schema_version: str,
        request: object,
        response: object,
        started: datetime,
        partial: bool,
    ) -> SemanticProviderRunRecord:
        finished = datetime.now(timezone.utc)
        request_sha = _canonical_json_sha256(request)
        response_sha = _canonical_json_sha256(response)
        status = (
            SemanticProviderRunStatus.PARTIAL
            if partial
            else SemanticProviderRunStatus.SUCCESS
        )
        error_code = "semantic_consensus_unresolved" if partial else None
        return SemanticProviderRunRecord(
            provider_run_id=stable_semantic_provider_run_id(
                operation=operation,
                provider_id=self.provider_id,
                model_id=self.model_id,
                request_sha256=request_sha,
                response_sha256=response_sha,
                status=status,
                started_at=started,
            ),
            operation=operation,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            model_id=self.model_id,
            prompt_version=prompt_version,
            response_schema_version=schema_version,
            request_sha256=request_sha,
            response_sha256=response_sha,
            status=status,
            started_at=started,
            finished_at=finished,
            latency_ms=round((finished - started).total_seconds() * 1000),
            request_count=self._repetition_count,
            retry_count=0,
            error_code=error_code,
        )


def _assembly_consensus(
    request: RequirementAssemblyRequest,
    runs: tuple[tuple[RequirementAssemblyDecision, ...], ...],
) -> tuple[RequirementAssemblyDecision, ...]:
    if not runs:
        raise ValueError("assembly decision aggregation requires at least one run")
    targets = request.scope_structure_node_ids
    if any(
        tuple(item.target_structure_node_id for item in run) != targets for run in runs
    ):
        raise ValueError("assembly decisions must exactly cover request targets")
    threshold = len(runs) // 2 + 1
    output: list[RequirementAssemblyDecision] = []
    for index, target in enumerate(targets):
        decisions = [run[index] for run in runs]
        groups: dict[str, list[RequirementAssemblyDecision]] = {}
        for decision in decisions:
            fingerprint = _canonical_json_sha256(
                {
                    "include_in_view": decision.include_in_view,
                    "role": decision.role.value if decision.role else None,
                    "parent_structure_node_id": decision.parent_structure_node_id,
                    "status": decision.status.value,
                }
            )
            groups.setdefault(fingerprint, []).append(decision)
        selected = max(groups.values(), key=len)
        if len(selected) >= threshold:
            output.append(selected[0])
        else:
            output.append(
                RequirementAssemblyDecision(
                    target_structure_node_id=target,
                    include_in_view=False,
                    status=RequirementAssemblyStatus.UNRESOLVED,
                    reason_codes=("assembly_consensus_unresolved",),
                )
            )
    return tuple(output)


def _normalize_reason_codes(payload: object) -> None:
    if isinstance(payload, list):
        for item in payload:
            _normalize_reason_codes(item)
        return
    if not isinstance(payload, dict):
        return
    reasons = payload.get("reason_codes")
    if isinstance(reasons, list):
        payload["reason_codes"] = sorted(
            {item for item in reasons if isinstance(item, str)}
        )
    for item in payload.values():
        _normalize_reason_codes(item)


def _classification_prompt() -> str:
    return (
        "Read scope.json and classify every target node exactly once in order. Use only "
        "the supplied evidence refs. Preserve complete parent semantics, tables, conditions, "
        "negation, exceptions, units, references, and continuation state. Do not infer authority "
        "from names. REQUIREMENT requires a subtype and source-bound evidence. Codes must be "
        "lowercase snake_case. Return only schema-valid JSON."
    )


def _assembly_prompt() -> str:
    return (
        "Read scope.json and return one assembly decision per target in exact order. Include only "
        "source-bound requirement decisions. Preserve parent/child roles and continuation; do not "
        "promote headings, context, methods, references, tables, or figures merely because they "
        "are locatable. Uncertain nodes must be UNRESOLVED and excluded. Codes must be lowercase "
        "snake_case. Return only schema-valid JSON."
    )


__all__ = [
    "CODEX_CONSENSUS_SEMANTIC_PROVIDER_ID",
    "CODEX_CONSENSUS_SEMANTIC_PROVIDER_VERSION",
    "CodexConsensusSemanticProvider",
]
