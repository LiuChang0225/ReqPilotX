from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Protocol

from pydantic import ValidationError

from reqpilotx_server.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerStageActivation,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStructuredStageTransportPort,
    _invoke_transport,
    _load_runner_output_schema,
    build_canonical_stage_invocation,
)
from reqpilotx_server.adapters.agents.codex_app_server.document_map_scope_stage_input import (
    DOCUMENT_MAP_SCOPE_EXECUTION_MODE,
)
from reqpilotx_server.adapters.agents.codex_app_server.document_map_survey_driver import (
    DOCUMENT_UNDERSTANDING_VALIDATOR_ID,
    DOCUMENT_UNDERSTANDING_VALIDATOR_VERSION,
    _safe_validation_issue_codes,
)
from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from reqpilotx_server.application.agent_workflow.document_map_scope import (
    DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_V2_SCHEMA_VERSION,
    DocumentMapHeadingScopeActionV2,
    DocumentMapHeadingScopeCandidateV2,
    DocumentMapHeadingScopeCompilationV1,
    DocumentMapScopeRequestV1,
    compile_document_map_heading_scope,
)
from reqpilotx_server.application.agent_workflow.document_understanding import (
    DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage


@dataclass(frozen=True, slots=True)
class DocumentMapHeadingScopeStageResult:
    candidate: DocumentMapHeadingScopeCandidateV2
    compilation: DocumentMapHeadingScopeCompilationV1 | None
    opened_high_resolution_page_numbers: tuple[int, ...]
    runtime_thread_id: str
    runtime_turn_id: str
    tool_activity_summary: Mapping[str, Any]

    @property
    def coordination_required(self) -> bool:
        return self.compilation is None


class DocumentMapHeadingScopeStageSessionPort(Protocol):
    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
        *,
        execution_mode: str | None = None,
    ) -> CanonicalAppServerStageActivation: ...


class DocumentMapHeadingScopeCandidateValidationError(LifecycleProbeError):
    """Retain content privately while exposing only safe field-path feedback."""

    def __init__(
        self,
        *,
        candidate_payload: Mapping[str, Any],
        validation_error: ValidationError,
        tool_activity_summary: Mapping[str, Any],
    ) -> None:
        super().__init__("DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_INVALID")
        self.private_candidate_payload = dict(candidate_payload)
        self.safe_issue_codes = _safe_validation_issue_codes(validation_error)
        self.tool_activity_summary = dict(tool_activity_summary)


class DocumentMapHeadingScopeHostCompilationError(LifecycleProbeError):
    """Preserve a schema-valid candidate when deterministic Host review rejects it."""

    def __init__(
        self,
        *,
        candidate: DocumentMapHeadingScopeCandidateV2,
        cause: TypeError | ValueError,
        tool_activity_summary: Mapping[str, Any],
    ) -> None:
        super().__init__("DOCUMENT_MAP_HEADING_SCOPE_HOST_COMPILATION_FAILED")
        self.private_candidate_payload = candidate.model_dump(mode="json")
        self.safe_issue_code = _safe_host_issue_code(cause)
        self.tool_activity_summary = dict(tool_activity_summary)


class CanonicalDocumentMapHeadingScopeStageDriver:
    """Run one exact-source detailed scope without repeating document survey."""

    def __init__(
        self,
        *,
        transport: CanonicalStructuredStageTransportPort,
    ) -> None:
        self._transport = transport

    def run(
        self,
        session: DocumentMapHeadingScopeStageSessionPort,
        *,
        request: DocumentMapScopeRequestV1,
        task_binding_sha256: str,
    ) -> DocumentMapHeadingScopeStageResult:
        request = DocumentMapScopeRequestV1.model_validate(
            request.model_dump(mode="json")
        )
        activation = session.require_inference_stage(
            AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            execution_mode=DOCUMENT_MAP_SCOPE_EXECUTION_MODE,
        )
        evidence = activation.session_attestation.document_evidence
        stage_input = activation.document_map_scope_stage_input_attestation
        if (
            evidence is None
            or stage_input is None
            or evidence.run_id != request.run_id
            or evidence.source_sha256 != request.source_sha256
            or stage_input.request_sha256 != request.request_sha256
            or stage_input.scope_id != request.scope_id
        ):
            raise LifecycleProbeError(
                "DOCUMENT_MAP_HEADING_SCOPE_INPUT_IDENTITY_MISMATCH"
            )
        invocation = build_canonical_stage_invocation(
            activation,
            task_binding_sha256=task_binding_sha256,
            turn_output_schema_id=(
                DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_V2_SCHEMA_VERSION
            ),
            turn_output_schema=_load_runner_output_schema(activation),
            expected_final_artifact_schema_id=(
                DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION
            ),
            expected_validator_id=DOCUMENT_UNDERSTANDING_VALIDATOR_ID,
            expected_validator_version=DOCUMENT_UNDERSTANDING_VALIDATOR_VERSION,
            task_directive=_task_directive(request),
        )
        transport_result = _invoke_transport(self._transport, invocation)
        try:
            candidate = DocumentMapHeadingScopeCandidateV2.model_validate(
                transport_result.structured_output
            )
        except ValidationError as exc:
            raise DocumentMapHeadingScopeCandidateValidationError(
                candidate_payload=transport_result.structured_output,
                validation_error=exc,
                tool_activity_summary=transport_result.tool_activity_summary,
            ) from None
        except Exception as exc:
            raise LifecycleProbeError(
                "DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_INVALID"
            ) from exc
        if (
            candidate.request_id != request.scope_id
            or candidate.request_sha256 != request.request_sha256
            or candidate.accepted_source_span != request.suggested_source_span
            or any(
                item.source_node_id not in request.source_node_ids
                for item in candidate.heading_expansion_proposals
            )
        ):
            raise LifecycleProbeError(
                "DOCUMENT_MAP_HEADING_SCOPE_CANDIDATE_IDENTITY_MISMATCH"
            )
        opened_pages = _opened_high_resolution_pages(
            transport_result.tool_activity_summary
        )
        if not set(request.page_numbers).issubset(opened_pages):
            raise LifecycleProbeError(
                "DOCUMENT_MAP_HEADING_SCOPE_VISUAL_COVERAGE_INCOMPLETE"
            )
        compilation: DocumentMapHeadingScopeCompilationV1 | None = None
        if candidate.action is DocumentMapHeadingScopeActionV2.ACCEPT_SCOPE:
            try:
                compilation = compile_document_map_heading_scope(
                    request=request,
                    candidate=candidate,
                    authorized_external_evidence_refs=(
                        _authorized_evidence_refs(
                            activation,
                            opened_page_numbers=opened_pages,
                        )
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise DocumentMapHeadingScopeHostCompilationError(
                    candidate=candidate,
                    cause=exc,
                    tool_activity_summary=transport_result.tool_activity_summary,
                ) from None
        return DocumentMapHeadingScopeStageResult(
            candidate=candidate,
            compilation=compilation,
            opened_high_resolution_page_numbers=tuple(sorted(opened_pages)),
            runtime_thread_id=transport_result.runtime_thread_id,
            runtime_turn_id=transport_result.runtime_turn_id,
            tool_activity_summary=transport_result.tool_activity_summary,
        )


def _task_directive(request: DocumentMapScopeRequestV1) -> str:
    pages = ",".join(str(item) for item in request.page_numbers)
    return (
        "Execution mode DOCUMENT_MAP_HEADING_SCOPE. Read the attested scope request "
        "When kind=SECTION_GROUP the scope is a whole surveyed document unit and may "
        "contain multiple genuine level-1 chapters, each with no heading parent. "
        "Do not manufacture a common chapter or promote table captions into chapters. "
        "from stage_input_manifest and do not repeat the completed whole-document "
        "survey. Open every requested high-resolution page ("
        + pages
        + ") and inspect the exact owned source nodes plus adjacent context, tables, "
        "assets, and pixels as needed using the attached PDF capability. Decide every "
        "advisory numbered candidate as "
        "HEADING or NOT_HEADING. Preserve the exact source span when accepted; if the "
        "boundary is wrong, request adjacent coordination without taking neighboring "
        "content. If a real heading is absent from the advisory candidates, return "
        "EXPAND_HEADING_CANDIDATES with its exact owned source node and no heading "
        "decisions; the Host will materialize a revised request instead of suppressing "
        "it. Write public summaries in the document's dominant language. Do not "
        "identify or classify requirements."
    )


def _opened_high_resolution_pages(summary: Mapping[str, Any]) -> set[int]:
    visual = summary.get("visual_inspection")
    if not isinstance(visual, Mapping):
        return set()
    values = visual.get("high_resolution_page_numbers")
    if not isinstance(values, (list, tuple)) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in values
    ):
        return set()
    return {item for item in values if item > 0}


def _authorized_evidence_refs(
    activation: CanonicalAppServerStageActivation,
    *,
    opened_page_numbers: set[int],
) -> frozenset[str]:
    root = getattr(activation, "evidence_workspace_path", None)
    if root is None:
        return frozenset(f"page:{page}" for page in opened_page_numbers)
    try:
        node_values = tuple(
            json.loads(line)
            for line in (root / "pages/source-ordinal-ledger.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
        table_payload = json.loads(
            (root / "tables/index.json").read_text(encoding="utf-8")
        )
        asset_payload = json.loads(
            (root / "assets/index.json").read_text(encoding="utf-8")
        )
        node_ids = {
            item["source_node_id"]
            for item in node_values
            if isinstance(item, Mapping)
        }
        table_ids = {
            item["table_id"]
            for item in table_payload["tables"]
            if isinstance(item, Mapping)
        }
        asset_ids = {
            item["asset_id"]
            for item in asset_payload["unique_assets"]
            if isinstance(item, Mapping)
        }
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("heading evidence inventory is invalid") from exc
    refs = {
        *(f"page:{page}" for page in opened_page_numbers),
        *node_ids,
        *table_ids,
        *asset_ids,
    }
    pattern = re.compile(
        r"^(?:page:[1-9][0-9]{0,2}|node-[0-9a-f]{32}|"
        r"table-[0-9a-f]{32}|asset-[0-9a-f]{32})$"
    )
    if not refs or any(
        not isinstance(item, str) or pattern.fullmatch(item) is None
        for item in refs
    ):
        raise ValueError("heading evidence inventory is invalid")
    return frozenset(refs)


def _safe_host_issue_code(cause: TypeError | ValueError) -> str:
    return {
        "non-root heading requires a decided parent": (
            "non_root_heading_requires_a_decided_parent"
        ),
        "heading scope parent decision is invalid": (
            "heading_scope_parent_decision_invalid"
        ),
        "heading scope decisions must cover every advisory candidate": (
            "heading_scope_candidate_coverage_invalid"
        ),
        "heading scope finding leaves its owned page context": (
            "heading_scope_finding_page_context_invalid"
        ),
        "heading scope finding leaves its source context": (
            "heading_scope_finding_source_context_invalid"
        ),
        "heading scope finding leaves its table context": (
            "heading_scope_finding_table_context_invalid"
        ),
        "heading scope finding leaves its asset context": (
            "heading_scope_finding_asset_context_invalid"
        ),
    }.get(str(cause), "heading_scope_host_compilation_failed")


__all__ = [
    "CanonicalDocumentMapHeadingScopeStageDriver",
    "DocumentMapHeadingScopeCandidateValidationError",
    "DocumentMapHeadingScopeHostCompilationError",
    "DocumentMapHeadingScopeStageResult",
]
