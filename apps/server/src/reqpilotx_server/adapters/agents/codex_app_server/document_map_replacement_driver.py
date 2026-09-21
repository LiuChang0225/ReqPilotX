from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any, Mapping, Protocol

from pydantic import ValidationError

from reqpilotx_server.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerStageActivation,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStructuredStageTransportPort,
    CanonicalStageTransportResult,
    _invoke_transport,
    _load_runner_output_schema,
    build_canonical_stage_invocation,
)
from reqpilotx_server.adapters.agents.codex_app_server.document_map_heading_scope_driver import (
    _opened_high_resolution_pages,
)
from reqpilotx_server.adapters.agents.codex_app_server.document_map_survey_driver import (
    DOCUMENT_UNDERSTANDING_VALIDATOR_ID,
    DOCUMENT_UNDERSTANDING_VALIDATOR_VERSION,
    _safe_validation_issue_codes,
)
from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from reqpilotx_server.application.agent_workflow.document_map_replacement import (
    DOCUMENT_MAP_SCOPE_REPLACEMENT_WIRE_SCHEMA_VERSION,
    DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE,
    DocumentMapScopeReplacementAction,
    DocumentMapScopeReplacementCandidateV1,
    DocumentMapScopeReplacementCandidateV2,
    DocumentMapScopeReplacementCompilationV1,
    DocumentMapScopeReplacementRequestV1,
    compile_document_map_scope_replacement,
    load_document_map_scope_evidence,
)
from reqpilotx_server.application.agent_workflow.document_understanding import (
    DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage


@dataclass(frozen=True, slots=True)
class DocumentMapScopeReplacementStageResult:
    candidate: DocumentMapScopeReplacementCandidateV1
    compilation: DocumentMapScopeReplacementCompilationV1 | None
    opened_high_resolution_page_numbers: tuple[int, ...]
    runtime_thread_id: str
    runtime_turn_id: str
    tool_activity_summary: Mapping[str, Any]

    @property
    def coordination_required(self) -> bool:
        return self.compilation is None


class DocumentMapScopeReplacementStageSessionPort(Protocol):
    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
        *,
        execution_mode: str | None = None,
    ) -> CanonicalAppServerStageActivation: ...


class DocumentMapScopeReplacementCandidateValidationError(LifecycleProbeError):
    def __init__(
        self,
        *,
        candidate_payload: Mapping[str, Any],
        validation_error: ValidationError,
        tool_activity_summary: Mapping[str, Any],
    ) -> None:
        super().__init__("DOCUMENT_MAP_SCOPE_REPLACEMENT_CANDIDATE_INVALID")
        self.private_candidate_payload = dict(candidate_payload)
        self.safe_issue_codes = _safe_validation_issue_codes(validation_error)
        self.tool_activity_summary = dict(tool_activity_summary)


class DocumentMapScopeReplacementHostCompilationError(LifecycleProbeError):
    def __init__(
        self,
        *,
        candidate: DocumentMapScopeReplacementCandidateV1,
        cause: TypeError | ValueError,
        tool_activity_summary: Mapping[str, Any],
    ) -> None:
        super().__init__("DOCUMENT_MAP_SCOPE_REPLACEMENT_HOST_COMPILATION_FAILED")
        self.private_candidate_payload = candidate.model_dump(mode="json")
        self.safe_issue_code = _safe_issue_code(cause)
        self.tool_activity_summary = dict(tool_activity_summary)


class CanonicalDocumentMapScopeReplacementStageDriver:
    """Run full content/table/asset ownership for one accepted heading scope."""

    def __init__(
        self,
        *,
        transport: CanonicalStructuredStageTransportPort,
    ) -> None:
        self._transport = transport

    def run(
        self,
        session: DocumentMapScopeReplacementStageSessionPort,
        *,
        request: DocumentMapScopeReplacementRequestV1,
        task_binding_sha256: str,
    ) -> DocumentMapScopeReplacementStageResult:
        request = DocumentMapScopeReplacementRequestV1.model_validate(
            request.model_dump(mode="json")
        )
        activation = session.require_inference_stage(
            AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            execution_mode=DOCUMENT_MAP_SCOPE_REPLACEMENT_EXECUTION_MODE,
        )
        evidence = activation.session_attestation.document_evidence
        stage_input = activation.document_map_replacement_stage_input_attestation
        scope = request.scope_request
        if (
            evidence is None
            or stage_input is None
            or evidence.run_id != scope.run_id
            or evidence.source_sha256 != scope.source_sha256
            or stage_input.replacement_request_sha256 != request.request_sha256
            or stage_input.heading_compilation_sha256
            != request.heading_compilation.compilation_sha256
            or activation.evidence_workspace_path is None
        ):
            raise LifecycleProbeError(
                "DOCUMENT_MAP_SCOPE_REPLACEMENT_INPUT_IDENTITY_MISMATCH"
            )
        started = time.monotonic()
        repair_feedback = ""
        attempts: list[Mapping[str, Any]] = []
        for attempt_index in range(2):
            invocation = build_canonical_stage_invocation(
                activation,
                task_binding_sha256=task_binding_sha256,
                turn_output_schema_id=DOCUMENT_MAP_SCOPE_REPLACEMENT_WIRE_SCHEMA_VERSION,
                turn_output_schema=_load_runner_output_schema(activation),
                expected_final_artifact_schema_id=DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION,
                expected_validator_id=DOCUMENT_UNDERSTANDING_VALIDATOR_ID,
                expected_validator_version=DOCUMENT_UNDERSTANDING_VALIDATOR_VERSION,
                task_directive=_task_directive(request) + repair_feedback,
            )
            transport_result = _invoke_transport(self._transport, invocation)
            attempts.append(transport_result.tool_activity_summary)
            try:
                result = _compile_transport_result(
                    activation=activation,
                    request=request,
                    transport_result=transport_result,
                )
            except (
                DocumentMapScopeReplacementCandidateValidationError,
                DocumentMapScopeReplacementHostCompilationError,
            ) as exc:
                if attempt_index or (
                    isinstance(exc, DocumentMapScopeReplacementHostCompilationError)
                    and exc.safe_issue_code
                    in {
                        "document_map_scope_replacement_invalid",
                        "replacement_lineage_invalid",
                    }
                ):
                    exc.tool_activity_summary = {
                        **exc.tool_activity_summary,
                        "scope_repair_count": attempt_index,
                        "scope_attempt_count": len(attempts),
                        "elapsed_milliseconds": round(
                            (time.monotonic() - started) * 1000
                        ),
                    }
                    raise
                issues = (
                    exc.safe_issue_codes
                    if isinstance(
                        exc, DocumentMapScopeReplacementCandidateValidationError
                    )
                    else (exc.safe_issue_code,)
                )
                repair_feedback = (
                    "\nThe prior candidate failed Host validation in this scope only. "
                    "The accepted headings and source evidence are unchanged. "
                    "Recheck the failing fields against the source and submit a full corrected "
                    "replacement; do not rerun the whole-document survey. "
                    "Table start_row is inclusive and end_row is exclusive: to cover an "
                    "entire physical table use [0, row_count), not row_count - 1.\n"
                    + json.dumps(
                        {
                            "validation_issues": issues[:3],
                            "total_validation_issue_count": len(issues),
                        },
                        ensure_ascii=False,
                    )
                )
                continue
            return DocumentMapScopeReplacementStageResult(
                candidate=result.candidate,
                compilation=result.compilation,
                opened_high_resolution_page_numbers=result.opened_high_resolution_page_numbers,
                runtime_thread_id=result.runtime_thread_id,
                runtime_turn_id=result.runtime_turn_id,
                tool_activity_summary={
                    **result.tool_activity_summary,
                    "scope_repair_count": attempt_index,
                    "scope_attempt_count": len(attempts),
                    "scope_attempts": attempts,
                    "elapsed_milliseconds": round((time.monotonic() - started) * 1000),
                },
            )
        raise AssertionError("bounded scope repair did not terminate")


def _compile_transport_result(
    *,
    activation: CanonicalAppServerStageActivation,
    request: DocumentMapScopeReplacementRequestV1,
    transport_result: CanonicalStageTransportResult,
) -> DocumentMapScopeReplacementStageResult:
    scope = request.scope_request
    try:
        decisions = DocumentMapScopeReplacementCandidateV2.model_validate(
            transport_result.structured_output
        )
    except ValidationError as exc:
        raise DocumentMapScopeReplacementCandidateValidationError(
            candidate_payload=transport_result.structured_output,
            validation_error=exc,
            tool_activity_summary=transport_result.tool_activity_summary,
        ) from None
    except Exception as exc:
        raise LifecycleProbeError(
            "DOCUMENT_MAP_SCOPE_REPLACEMENT_CANDIDATE_INVALID"
        ) from exc
    candidate = DocumentMapScopeReplacementCandidateV1(
        **decisions.model_dump(mode="json", exclude={"schema_version"}),
        request_id=scope.scope_id,
        request_sha256=request.request_sha256,
        heading_compilation_sha256=request.heading_compilation.compilation_sha256,
    )
    opened_pages = tuple(
        sorted(_opened_high_resolution_pages(transport_result.tool_activity_summary))
    )
    if not set(scope.page_numbers).issubset(opened_pages):
        raise LifecycleProbeError(
            "DOCUMENT_MAP_SCOPE_REPLACEMENT_VISUAL_COVERAGE_INCOMPLETE"
        )
    compilation = None
    if candidate.action is DocumentMapScopeReplacementAction.ACCEPT_SCOPE:
        try:
            visual = transport_result.tool_activity_summary.get(
                "visual_inspection",
                {},
            )
            receipt_root = (
                visual.get("receipt_root_sha256")
                if isinstance(visual, Mapping)
                else None
            )
            if not isinstance(receipt_root, str):
                raise ValueError("replacement visual receipt is missing")
            index = load_document_map_scope_evidence(
                root=activation.evidence_workspace_path,
                request=request,
            )
            compilation = compile_document_map_scope_replacement(
                request=request,
                candidate=candidate,
                evidence=index,
                opened_high_resolution_page_numbers=opened_pages,
                visual_receipt_root_sha256=receipt_root,
            )
        except (TypeError, ValueError) as exc:
            raise DocumentMapScopeReplacementHostCompilationError(
                candidate=candidate,
                cause=exc,
                tool_activity_summary=transport_result.tool_activity_summary,
            ) from None
    return DocumentMapScopeReplacementStageResult(
        candidate=candidate,
        compilation=compilation,
        opened_high_resolution_page_numbers=opened_pages,
        runtime_thread_id=transport_result.runtime_thread_id,
        runtime_turn_id=transport_result.runtime_turn_id,
        tool_activity_summary=transport_result.tool_activity_summary,
    )


def _task_directive(request: DocumentMapScopeReplacementRequestV1) -> str:
    scope = request.scope_request
    pages = ",".join(str(item) for item in scope.page_numbers)
    return (
        "Execution mode DOCUMENT_MAP_SCOPE_REPLACEMENT. Follow the attached "
        "document-understanding and PDF Skills. Read the request via stage_input_manifest. "
        "The accepted heading tree is the predecessor; do not re-decide it. Open high-resolution pages "
        + pages
        + ". Review all text, tables and asset placements. Use the ordinary-text policy "
        "with explicit exceptions; confirmed repeated headers/footers are NON_BUSINESS. "
        "Do not discard table-text aliases. Group physical fragments by logical continuity, "
        "not parser order or page breaks. Cover all rows; full-table range is [0, row_count). "
        "The local table key is separate from its visible title. Ground SOURCE_VISIBLE titles; "
        "otherwise use AGENT_LABEL. Return semantic decisions only, not request IDs/hashes. "
        "Keep uncertainty explicit and coordinate wrong boundaries. Do not classify, split "
        "or rewrite requirements. Public summaries use the source language."
    )


def _safe_issue_code(cause: TypeError | ValueError) -> str:
    value = str(cause).casefold()
    known = {
        "replacement table rows require exact one-owner coverage": (
            "table_row_coverage_invalid"
        ),
        "replacement asset placements require exact coverage": (
            "asset_placement_coverage_invalid"
        ),
        "replacement candidate or visual lineage drifted": (
            "replacement_lineage_invalid"
        ),
        "replacement text ownership override is invalid": (
            "text_ownership_override_invalid"
        ),
        "logical table owner heading is invalid": "table_owner_invalid",
        "logical table fragments must preserve source order": "table_fragment_order_invalid",
        "logical table title is not source-visible": "table_title_not_source_visible",
        "replacement table fragment exceeds row count": "table_row_range_invalid",
        "replacement table fragment cuts a merged cell": "table_range_cuts_merged_cell",
        "replacement table fragment leaves its scope": "table_evidence_not_in_scope",
        "replacement scope source coverage drifted": "scope_source_inventory_drifted",
        "replacement text precedes its first accepted heading": "text_precedes_first_heading",
        "replacement finding leaves its scope evidence": "finding_reference_outside_scope",
    }
    return known.get(value, "document_map_scope_replacement_invalid")


__all__ = [
    "CanonicalDocumentMapScopeReplacementStageDriver",
    "DocumentMapScopeReplacementCandidateValidationError",
    "DocumentMapScopeReplacementHostCompilationError",
    "DocumentMapScopeReplacementStageResult",
]
