from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Protocol

from pydantic import ValidationError

from app.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerStageActivation,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (
    CanonicalStructuredStageTransportPort,
    _invoke_transport,
    _load_runner_output_schema,
    build_canonical_stage_invocation,
)
from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.application.agent_workflow.document_map_exploration import (
    DOCUMENT_MAP_SURVEY_CANDIDATE_SCHEMA_VERSION,
    DocumentMapSurveyCandidateV1,
    DocumentMapSurveyCompilationV1,
    DocumentMapVisualCoverageMode,
    DocumentMapVisualCoverageReceiptV1,
    build_document_map_visual_coverage_receipt,
    compile_document_map_survey,
)
from app.application.agent_workflow.document_understanding import (
    DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION,
)
from app.application.agent_workflow.extraction_runtime import AgentExtractionStage


DOCUMENT_MAP_SURVEY_EXECUTION_MODE = "DOCUMENT_MAP_SURVEY"
DOCUMENT_UNDERSTANDING_VALIDATOR_ID = (
    "reqpilotx-document-understanding-validator"
)
DOCUMENT_UNDERSTANDING_VALIDATOR_VERSION = "1.1.0"


@dataclass(frozen=True, slots=True)
class DocumentMapSurveyStageResult:
    candidate: DocumentMapSurveyCandidateV1
    visual_coverage: DocumentMapVisualCoverageReceiptV1
    compilation: DocumentMapSurveyCompilationV1
    runtime_thread_id: str
    runtime_turn_id: str
    tool_activity_summary: Mapping[str, Any]


class DocumentMapSurveyStageSessionPort(Protocol):
    def require_inference_stage(
        self,
        stage: AgentExtractionStage,
        *,
        execution_mode: str | None = None,
    ) -> CanonicalAppServerStageActivation: ...


class DocumentMapSurveyCandidateValidationError(LifecycleProbeError):
    """Private candidate plus content-free field paths for bounded correction."""

    def __init__(
        self,
        *,
        candidate_payload: Mapping[str, Any],
        validation_error: ValidationError,
        tool_activity_summary: Mapping[str, Any],
    ) -> None:
        super().__init__("DOCUMENT_MAP_SURVEY_CANDIDATE_INVALID")
        self.private_candidate_payload = dict(candidate_payload)
        self.safe_issue_codes = _safe_validation_issue_codes(validation_error)
        self.tool_activity_summary = dict(tool_activity_summary)


class CanonicalDocumentMapSurveyStageDriver:
    """Run one goal-driven whole-document survey before detailed Map scopes.

    The result is a private checkpoint. It deliberately does not manufacture a
    D1 final artifact or claim that the Document Map has been recovered.
    """

    def __init__(
        self,
        *,
        transport: CanonicalStructuredStageTransportPort,
        visual_coverage_mode: DocumentMapVisualCoverageMode,
    ) -> None:
        self._transport = transport
        self._visual_coverage_mode = visual_coverage_mode

    def run(
        self,
        session: DocumentMapSurveyStageSessionPort,
        *,
        task_binding_sha256: str,
    ) -> DocumentMapSurveyStageResult:
        activation = session.require_inference_stage(
            AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            execution_mode=DOCUMENT_MAP_SURVEY_EXECUTION_MODE,
        )
        evidence = activation.session_attestation.document_evidence
        if evidence is None:
            raise LifecycleProbeError("DOCUMENT_MAP_SURVEY_EVIDENCE_MISSING")
        invocation = build_canonical_stage_invocation(
            activation,
            task_binding_sha256=task_binding_sha256,
            turn_output_schema_id=DOCUMENT_MAP_SURVEY_CANDIDATE_SCHEMA_VERSION,
            turn_output_schema=_load_runner_output_schema(activation),
            expected_final_artifact_schema_id=(
                DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION
            ),
            expected_validator_id=DOCUMENT_UNDERSTANDING_VALIDATOR_ID,
            expected_validator_version=DOCUMENT_UNDERSTANDING_VALIDATOR_VERSION,
            task_directive=_task_directive(self._visual_coverage_mode),
        )
        transport_result = _invoke_transport(self._transport, invocation)
        try:
            candidate = DocumentMapSurveyCandidateV1.model_validate(
                transport_result.structured_output
            )
        except ValidationError as exc:
            raise DocumentMapSurveyCandidateValidationError(
                candidate_payload=transport_result.structured_output,
                validation_error=exc,
                tool_activity_summary=transport_result.tool_activity_summary,
            ) from None
        except Exception as exc:
            raise LifecycleProbeError(
                "DOCUMENT_MAP_SURVEY_CANDIDATE_INVALID"
            ) from exc
        try:
            visual_coverage = build_document_map_visual_coverage_receipt(
                source_sha256=evidence.source_sha256,
                page_count=evidence.page_count,
                mode=self._visual_coverage_mode,
                event_summary=transport_result.tool_activity_summary,
            )
            compilation = compile_document_map_survey(
                candidate,
                visual_coverage=visual_coverage,
                expected_run_id=evidence.run_id,
                expected_source_sha256=evidence.source_sha256,
                expected_kit_identity=activation.session_attestation.kit_identity,
            )
        except (TypeError, ValueError):
            raise LifecycleProbeError(
                "DOCUMENT_MAP_SURVEY_HOST_COMPILATION_FAILED"
            ) from None
        return DocumentMapSurveyStageResult(
            candidate=candidate,
            visual_coverage=visual_coverage,
            compilation=compilation,
            runtime_thread_id=transport_result.runtime_thread_id,
            runtime_turn_id=transport_result.runtime_turn_id,
            tool_activity_summary=transport_result.tool_activity_summary,
        )


def _task_directive(mode: DocumentMapVisualCoverageMode) -> str:
    visual = (
        "Open the contact sheet and every high-resolution page."
        if mode is DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE
        else (
            "Open the contact sheet and the high-resolution pages needed to test "
            "the current whole-document hypothesis; every authorized page remains "
            "available."
        )
    )
    return (
        "Execution mode DOCUMENT_MAP_SURVEY. Build only the private, revisable "
        "whole-document survey candidate. Partition pages into primary document "
        "units such as front matter, one main document, or separately authoritative "
        "attachments; do not use page ownership as chapter boundaries because a "
        "page may contain multiple chapters. The heading skeleton and parser facts "
        "are advisory. Use only page:N evidence_refs whose pages belong to the "
        "current range or finding. "
        "Use the attached PDF capability to choose native and visual inspection "
        "methods. Write public summaries in the document's dominant language. "
        + visual
    )


def _safe_validation_issue_codes(error: ValidationError) -> tuple[str, ...]:
    values: set[str] = set()
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(str(part) for part in item.get("loc", ())) or "root"
        issue_type = str(item.get("type") or "invalid")
        safe_location = re.sub(r"[^a-zA-Z0-9_.-]+", "_", location)[:240]
        safe_type = re.sub(r"[^a-zA-Z0-9_.-]+", "_", issue_type)[:120]
        values.add(f"{safe_location}:{safe_type}")
    return tuple(sorted(values))


__all__ = [
    "CanonicalDocumentMapSurveyStageDriver",
    "DOCUMENT_MAP_SURVEY_EXECUTION_MODE",
    "DocumentMapSurveyCandidateValidationError",
    "DocumentMapSurveyStageResult",
]
