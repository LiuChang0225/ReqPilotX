from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.extraction.assemble_requirements import (
    validate_requirement_assembly_against_inputs,
)
from app.application.extraction.classify_content import (
    validate_semantic_classification_against_inputs,
)
from app.application.extraction.llm_provision_batch import (
    LlmProvisionExtractionStatus,
    LlmRequirementBatchResult,
    LlmRequirementBatchStatus,
    extract_requirement_provisions_with_provider,
)
from app.application.extraction.llm_semantics import (
    DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST,
)
from app.application.extraction.locate_llm_scope import LlmExtractionScopeLocator
from app.application.extraction.project_requirements import (
    validate_assembled_requirement_view_against_inputs,
)
from app.application.extraction.resolve_structure import (
    validate_structure_tree_against_source_ir,
)
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.structure import StructureTreeSnapshot
from app.ports.semantic_provider import (
    PreflightSemanticProviderPort,
    SemanticImageInput,
)


LLM_REQUIREMENT_REPEATABILITY_SCHEMA_VERSION = "llm-requirement-repeatability/v3"
MIN_LLM_REPEATABILITY_RUNS = 3
MAX_LLM_REPEATABILITY_RUNS = 10
_MAX_BAILIAN_SEMANTIC_SEED = 2**31 - 1


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class LlmRequirementRepeatabilityStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class LlmRequirementRepeatabilityMismatchCode(str, Enum):
    SOURCE_BINDING_MISMATCH = "source_binding_mismatch"
    LOCATOR_BINDING_MISMATCH = "locator_binding_mismatch"
    PROVIDER_BINDING_MISMATCH = "provider_binding_mismatch"
    PROVISION_SCOPE_MISMATCH = "provision_scope_mismatch"
    RUN_NOT_COMPLETE = "run_not_complete"
    PROVIDER_REQUEST_MISMATCH = "provider_request_mismatch"
    SEMANTIC_RESULT_MISMATCH = "semantic_result_mismatch"
    ASSEMBLY_RESULT_MISMATCH = "assembly_result_mismatch"
    REQUIREMENT_VIEW_MISMATCH = "requirement_view_mismatch"


class LlmRequirementRepeatabilityRequestIdentity(_StrictFrozenModel):
    operation_index: int = Field(ge=0)
    operation: str = Field(min_length=1, max_length=80)
    prompt_version: str = Field(min_length=1, max_length=160)
    response_schema_version: str = Field(min_length=1, max_length=160)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LlmRequirementRepeatabilityTargetSummary(_StrictFrozenModel):
    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    semantic_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_grounding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_diagnostic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assembly_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assembly_diagnostic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirement_view_node_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    decision_skeleton_node_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    normalized_target_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LlmRequirementRepeatabilityProvisionSummary(_StrictFrozenModel):
    root_section_structure_node_id: str = Field(min_length=1, max_length=160)
    status: LlmProvisionExtractionStatus
    error_code: str | None = Field(default=None, max_length=120)
    request_identities: tuple[LlmRequirementRepeatabilityRequestIdentity, ...]
    semantic_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    assembly_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    requirement_view_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    normalized_result_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    targets: tuple[LlmRequirementRepeatabilityTargetSummary, ...] = ()


class LlmRequirementRepeatabilityRunSummary(_StrictFrozenModel):
    run_index: int = Field(ge=1)
    batch_status: LlmRequirementBatchStatus
    successful_provision_count: int = Field(ge=0)
    unresolved_provision_count: int = Field(ge=0)
    blocked_provision_count: int = Field(ge=0)
    provider_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assembly_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assembly_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirement_view_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provisions: tuple[LlmRequirementRepeatabilityProvisionSummary, ...]


class LlmRequirementRepeatabilityReport(_StrictFrozenModel):
    schema_version: str = LLM_REQUIREMENT_REPEATABILITY_SCHEMA_VERSION
    status: LlmRequirementRepeatabilityStatus
    sampling_seed: int = Field(ge=0, le=_MAX_BAILIAN_SEMANTIC_SEED)
    repetition_count: int = Field(
        ge=MIN_LLM_REPEATABILITY_RUNS,
        le=MAX_LLM_REPEATABILITY_RUNS,
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_adapter_id: str = Field(min_length=1, max_length=160)
    source_adapter_version: str = Field(min_length=1, max_length=160)
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    structure_resolver_id: str = Field(min_length=1, max_length=160)
    structure_resolver_version: str = Field(min_length=1, max_length=160)
    locator_version: str = Field(min_length=1, max_length=160)
    locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=160)
    all_runs_complete: bool
    provider_request_exact_match: bool
    classification_request_exact_match: bool
    assembly_request_exact_match: bool
    semantic_exact_match: bool
    assembly_exact_match: bool
    requirement_view_exact_match: bool
    decision_skeleton_exact_match: bool
    unstable_semantic_decision_semantic_unit_ids: tuple[str, ...]
    unstable_semantic_grounding_semantic_unit_ids: tuple[str, ...]
    unstable_semantic_diagnostic_semantic_unit_ids: tuple[str, ...]
    unstable_assembly_decision_semantic_unit_ids: tuple[str, ...]
    unstable_assembly_diagnostic_semantic_unit_ids: tuple[str, ...]
    unstable_requirement_view_semantic_unit_ids: tuple[str, ...]
    unstable_decision_semantic_unit_ids: tuple[str, ...]
    mismatch_codes: tuple[LlmRequirementRepeatabilityMismatchCode, ...]
    runs: tuple[LlmRequirementRepeatabilityRunSummary, ...]
    human_review_required: bool
    decision_review_required: bool
    non_decision_variance_only: bool
    is_human_gold: bool = False
    is_serving_activation_gate: bool = False

    @model_validator(mode="after")
    def validate_report_contract(self) -> LlmRequirementRepeatabilityReport:
        if len(self.runs) != self.repetition_count:
            raise ValueError("repeatability run summaries must match repetition_count")
        if tuple(item.run_index for item in self.runs) != tuple(
            range(1, self.repetition_count + 1)
        ):
            raise ValueError("repeatability run indexes must be contiguous")
        if len(self.mismatch_codes) != len(set(self.mismatch_codes)):
            raise ValueError("repeatability mismatch codes must be unique")
        expected_status = (
            LlmRequirementRepeatabilityStatus.PASS
            if not self.mismatch_codes
            else LlmRequirementRepeatabilityStatus.FAIL
        )
        if self.status is not expected_status:
            raise ValueError("repeatability status does not match mismatch codes")
        if self.status is LlmRequirementRepeatabilityStatus.PASS and not all(
            (
                self.all_runs_complete,
                self.provider_request_exact_match,
                self.classification_request_exact_match,
                self.assembly_request_exact_match,
                self.semantic_exact_match,
                self.assembly_exact_match,
                self.requirement_view_exact_match,
            )
        ):
            raise ValueError("passing repeatability reports require exact results")
        expected_human_review = self.status is LlmRequirementRepeatabilityStatus.FAIL
        if self.human_review_required is not expected_human_review:
            raise ValueError("human_review_required must mirror a failed gate")
        expected_decision_exact = not self.unstable_decision_semantic_unit_ids
        if self.decision_skeleton_exact_match is not expected_decision_exact:
            raise ValueError(
                "decision_skeleton_exact_match must mirror unstable decision targets"
            )
        binding_or_execution_mismatches = {
            LlmRequirementRepeatabilityMismatchCode.SOURCE_BINDING_MISMATCH,
            LlmRequirementRepeatabilityMismatchCode.LOCATOR_BINDING_MISMATCH,
            LlmRequirementRepeatabilityMismatchCode.PROVIDER_BINDING_MISMATCH,
            LlmRequirementRepeatabilityMismatchCode.PROVISION_SCOPE_MISMATCH,
            LlmRequirementRepeatabilityMismatchCode.RUN_NOT_COMPLETE,
        }
        expected_decision_review = (
            not self.decision_skeleton_exact_match
            or not self.classification_request_exact_match
            or any(
                item in binding_or_execution_mismatches for item in self.mismatch_codes
            )
        )
        if self.decision_review_required is not expected_decision_review:
            raise ValueError(
                "decision_review_required does not match decision or gate variance"
            )
        expected_non_decision_only = (
            self.status is LlmRequirementRepeatabilityStatus.FAIL
            and not expected_decision_review
        )
        if self.non_decision_variance_only is not expected_non_decision_only:
            raise ValueError(
                "non_decision_variance_only must identify strict non-decision drift"
            )
        if self.is_human_gold or self.is_serving_activation_gate:
            raise ValueError(
                "repeatability evidence cannot claim Gold or serving status"
            )
        return self


@dataclass(frozen=True)
class LlmRequirementRepeatabilityExecution:
    """In-memory evidence plus its text-free serializable report."""

    report: LlmRequirementRepeatabilityReport
    runs: tuple[LlmRequirementBatchResult, ...]


def run_llm_requirement_batch_repeatability(
    *,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
    provider_factory: Callable[[], PreflightSemanticProviderPort],
    scope_locator: LlmExtractionScopeLocator,
    sampling_seed: int,
    repetition_count: int = MIN_LLM_REPEATABILITY_RUNS,
    images: tuple[SemanticImageInput, ...] = (),
    max_images_per_classification_request: int = (
        DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST
    ),
) -> LlmRequirementRepeatabilityExecution:
    """Execute independent providers against one frozen extraction input."""

    _validate_repetition_inputs(
        repetition_count=repetition_count,
        sampling_seed=sampling_seed,
    )
    if not callable(provider_factory):
        raise TypeError("provider_factory must be callable")
    if not isinstance(scope_locator, LlmExtractionScopeLocator):
        raise TypeError("scope_locator must be an LlmExtractionScopeLocator")
    if not isinstance(images, tuple) or any(
        not isinstance(item, SemanticImageInput) for item in images
    ):
        raise TypeError("images must be a tuple of SemanticImageInput values")

    batches: list[LlmRequirementBatchResult] = []
    for _run_index in range(repetition_count):
        provider = provider_factory()
        if not isinstance(provider, PreflightSemanticProviderPort):
            raise TypeError(
                "provider_factory must return PreflightSemanticProviderPort"
            )
        if getattr(provider, "sampling_seed", None) != sampling_seed:
            raise ValueError("provider sampling seed does not match the gate seed")
        batches.append(
            extract_requirement_provisions_with_provider(
                source_ir,
                structure_tree,
                provider,
                scope_locator,
                images=images,
                max_images_per_classification_request=(
                    max_images_per_classification_request
                ),
            )
        )
    run_tuple = tuple(batches)
    return LlmRequirementRepeatabilityExecution(
        report=evaluate_llm_requirement_batch_repeatability(
            source_ir=source_ir,
            structure_tree=structure_tree,
            runs=run_tuple,
            sampling_seed=sampling_seed,
        ),
        runs=run_tuple,
    )


def evaluate_llm_requirement_batch_repeatability(
    *,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
    runs: tuple[LlmRequirementBatchResult, ...],
    sampling_seed: int,
) -> LlmRequirementRepeatabilityReport:
    """Compare N independently executed LLM batches without retaining source text.

    Volatile provider run IDs, timestamps, response-envelope IDs and derived
    snapshot IDs are deliberately excluded.  The strict gate compares provider
    request identities and the normalized semantic decisions, evidence refs,
    assembly relationships and RequirementView projection for every provision.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not isinstance(structure_tree, StructureTreeSnapshot):
        raise TypeError("structure_tree must be a StructureTreeSnapshot")
    if not isinstance(runs, tuple) or any(
        not isinstance(item, LlmRequirementBatchResult) for item in runs
    ):
        raise TypeError("runs must be a tuple of LlmRequirementBatchResult values")
    _validate_repetition_inputs(
        repetition_count=len(runs),
        sampling_seed=sampling_seed,
    )

    validate_structure_tree_against_source_ir(structure_tree, source_ir)
    summaries = tuple(
        _summarize_run(
            source_ir=source_ir,
            structure_tree=structure_tree,
            batch=batch,
            run_index=index,
        )
        for index, batch in enumerate(runs, start=1)
    )
    baseline = runs[0]
    baseline_summary = summaries[0]
    mismatch_codes: set[LlmRequirementRepeatabilityMismatchCode] = set()

    if any(
        batch.located_scope.source_sha256 != source_ir.source_sha256 for batch in runs
    ):
        mismatch_codes.add(
            LlmRequirementRepeatabilityMismatchCode.SOURCE_BINDING_MISMATCH
        )
    if any(
        (
            batch.located_scope.locator_version,
            batch.located_scope.locator_sha256,
        )
        != (
            baseline.located_scope.locator_version,
            baseline.located_scope.locator_sha256,
        )
        for batch in runs[1:]
    ):
        mismatch_codes.add(
            LlmRequirementRepeatabilityMismatchCode.LOCATOR_BINDING_MISMATCH
        )
    if any(batch.provider_binding != baseline.provider_binding for batch in runs[1:]):
        mismatch_codes.add(
            LlmRequirementRepeatabilityMismatchCode.PROVIDER_BINDING_MISMATCH
        )
    if any(
        _batch_scope_identity(batch) != _batch_scope_identity(baseline)
        for batch in runs[1:]
    ):
        mismatch_codes.add(
            LlmRequirementRepeatabilityMismatchCode.PROVISION_SCOPE_MISMATCH
        )

    all_runs_complete = all(_batch_is_complete(batch) for batch in runs)
    if not all_runs_complete:
        mismatch_codes.add(LlmRequirementRepeatabilityMismatchCode.RUN_NOT_COMPLETE)

    provider_request_exact_match = all(
        summary.provider_request_sha256 == baseline_summary.provider_request_sha256
        for summary in summaries[1:]
    )
    classification_request_exact_match = all(
        summary.classification_request_sha256
        == baseline_summary.classification_request_sha256
        for summary in summaries[1:]
    )
    assembly_request_exact_match = all(
        summary.assembly_request_sha256 == baseline_summary.assembly_request_sha256
        for summary in summaries[1:]
    )
    semantic_exact_match = all(
        summary.semantic_sha256 == baseline_summary.semantic_sha256
        for summary in summaries[1:]
    )
    assembly_exact_match = all(
        summary.assembly_sha256 == baseline_summary.assembly_sha256
        for summary in summaries[1:]
    )
    requirement_view_exact_match = all(
        summary.requirement_view_sha256 == baseline_summary.requirement_view_sha256
        for summary in summaries[1:]
    )
    if not provider_request_exact_match:
        mismatch_codes.add(
            LlmRequirementRepeatabilityMismatchCode.PROVIDER_REQUEST_MISMATCH
        )
    if not semantic_exact_match:
        mismatch_codes.add(
            LlmRequirementRepeatabilityMismatchCode.SEMANTIC_RESULT_MISMATCH
        )
    if not assembly_exact_match:
        mismatch_codes.add(
            LlmRequirementRepeatabilityMismatchCode.ASSEMBLY_RESULT_MISMATCH
        )
    if not requirement_view_exact_match:
        mismatch_codes.add(
            LlmRequirementRepeatabilityMismatchCode.REQUIREMENT_VIEW_MISMATCH
        )

    unstable_semantic_decisions = _unstable_target_ids(
        summaries,
        "semantic_decision_sha256",
    )
    unstable_semantic_grounding = _unstable_target_ids(
        summaries,
        "semantic_grounding_sha256",
    )
    unstable_semantic_diagnostics = _unstable_target_ids(
        summaries,
        "semantic_diagnostic_sha256",
    )
    unstable_assembly_decisions = _unstable_target_ids(
        summaries,
        "assembly_decision_sha256",
    )
    unstable_assembly_diagnostics = _unstable_target_ids(
        summaries,
        "assembly_diagnostic_sha256",
    )
    unstable_requirement_view = _unstable_target_ids(
        summaries,
        "requirement_view_node_sha256",
    )
    unstable_decision_skeleton = _unstable_target_ids(
        summaries,
        "decision_skeleton_node_sha256",
    )

    ordered_mismatches = tuple(
        item
        for item in LlmRequirementRepeatabilityMismatchCode
        if item in mismatch_codes
    )
    provider_id, provider_version, model_id = baseline.provider_binding
    decision_skeleton_exact_match = not unstable_decision_skeleton
    binding_or_execution_mismatches = {
        LlmRequirementRepeatabilityMismatchCode.SOURCE_BINDING_MISMATCH,
        LlmRequirementRepeatabilityMismatchCode.LOCATOR_BINDING_MISMATCH,
        LlmRequirementRepeatabilityMismatchCode.PROVIDER_BINDING_MISMATCH,
        LlmRequirementRepeatabilityMismatchCode.PROVISION_SCOPE_MISMATCH,
        LlmRequirementRepeatabilityMismatchCode.RUN_NOT_COMPLETE,
    }
    decision_review_required = (
        not decision_skeleton_exact_match
        or not classification_request_exact_match
        or any(item in binding_or_execution_mismatches for item in ordered_mismatches)
    )
    return LlmRequirementRepeatabilityReport(
        status=(
            LlmRequirementRepeatabilityStatus.PASS
            if not ordered_mismatches
            else LlmRequirementRepeatabilityStatus.FAIL
        ),
        sampling_seed=sampling_seed,
        repetition_count=len(runs),
        source_sha256=source_ir.source_sha256,
        source_ir_id=source_ir.source_ir_id,
        source_adapter_id=source_ir.adapter_id,
        source_adapter_version=source_ir.adapter_version,
        structure_snapshot_id=structure_tree.structure_snapshot_id,
        structure_resolver_id=structure_tree.resolver_id,
        structure_resolver_version=structure_tree.resolver_version,
        locator_version=baseline.located_scope.locator_version,
        locator_sha256=baseline.located_scope.locator_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        model_id=model_id,
        all_runs_complete=all_runs_complete,
        provider_request_exact_match=provider_request_exact_match,
        classification_request_exact_match=classification_request_exact_match,
        assembly_request_exact_match=assembly_request_exact_match,
        semantic_exact_match=semantic_exact_match,
        assembly_exact_match=assembly_exact_match,
        requirement_view_exact_match=requirement_view_exact_match,
        decision_skeleton_exact_match=decision_skeleton_exact_match,
        unstable_semantic_decision_semantic_unit_ids=(unstable_semantic_decisions),
        unstable_semantic_grounding_semantic_unit_ids=(unstable_semantic_grounding),
        unstable_semantic_diagnostic_semantic_unit_ids=(unstable_semantic_diagnostics),
        unstable_assembly_decision_semantic_unit_ids=(unstable_assembly_decisions),
        unstable_assembly_diagnostic_semantic_unit_ids=(unstable_assembly_diagnostics),
        unstable_requirement_view_semantic_unit_ids=(unstable_requirement_view),
        unstable_decision_semantic_unit_ids=(unstable_decision_skeleton),
        mismatch_codes=ordered_mismatches,
        runs=summaries,
        human_review_required=bool(ordered_mismatches),
        decision_review_required=decision_review_required,
        non_decision_variance_only=(
            bool(ordered_mismatches) and not decision_review_required
        ),
    )


def _summarize_run(
    *,
    source_ir: SourceIRSnapshot,
    structure_tree: StructureTreeSnapshot,
    batch: LlmRequirementBatchResult,
    run_index: int,
) -> LlmRequirementRepeatabilityRunSummary:
    provision_summaries: list[LlmRequirementRepeatabilityProvisionSummary] = []
    semantic_payloads: list[dict[str, object]] = []
    assembly_payloads: list[dict[str, object]] = []
    view_payloads: list[dict[str, object]] = []
    request_payloads: list[dict[str, object]] = []
    classification_request_payloads: list[dict[str, object]] = []
    assembly_request_payloads: list[dict[str, object]] = []

    for outcome in batch.provisions:
        extraction = outcome.extraction
        provider_runs = (
            extraction.provider_runs
            if extraction is not None
            else outcome.completed_provider_runs
        )
        request_identities = tuple(
            LlmRequirementRepeatabilityRequestIdentity(
                operation_index=index,
                operation=run.operation,
                prompt_version=run.prompt_version,
                response_schema_version=run.response_schema_version,
                request_sha256=run.request_sha256,
            )
            for index, run in enumerate(provider_runs)
        )
        request_payloads.append(
            {
                "root_section_structure_node_id": (
                    outcome.root_section_structure_node_id
                ),
                "request_identities": [
                    item.model_dump(mode="json") for item in request_identities
                ],
            }
        )
        for identity in request_identities:
            target = (
                classification_request_payloads
                if identity.operation == "classify_nodes"
                else assembly_request_payloads
            )
            target.append(
                {
                    "root_section_structure_node_id": (
                        outcome.root_section_structure_node_id
                    ),
                    "request_identity": identity.model_dump(mode="json"),
                }
            )

        semantic_sha256 = None
        assembly_sha256 = None
        view_sha256 = None
        result_sha256 = None
        target_summaries: tuple[LlmRequirementRepeatabilityTargetSummary, ...] = ()
        if extraction is not None:
            validate_semantic_classification_against_inputs(
                extraction.semantics,
                source_ir,
                structure_tree,
            )
            validate_requirement_assembly_against_inputs(
                extraction.assembly,
                source_ir,
                structure_tree,
                extraction.semantics,
            )
            validate_assembled_requirement_view_against_inputs(
                extraction.requirement_view,
                source_ir,
                structure_tree,
                extraction.semantics,
                extraction.assembly,
            )
            semantic_payload = _normalized_semantics(extraction.semantics)
            assembly_payload = _normalized_assembly(extraction.assembly)
            view_payload = _normalized_requirement_view(
                extraction.semantics,
                extraction.requirement_view,
            )
            target_summaries = _target_summaries(
                extraction.semantics,
                extraction.assembly,
                view_payload,
            )
            semantic_sha256 = _canonical_sha256(semantic_payload)
            assembly_sha256 = _canonical_sha256(assembly_payload)
            view_sha256 = _canonical_sha256(view_payload)
            result_sha256 = _canonical_sha256(
                {
                    "semantic_sha256": semantic_sha256,
                    "assembly_sha256": assembly_sha256,
                    "requirement_view_sha256": view_sha256,
                }
            )
            semantic_payloads.append(
                {
                    "root_section_structure_node_id": (
                        outcome.root_section_structure_node_id
                    ),
                    "payload": semantic_payload,
                }
            )
            assembly_payloads.append(
                {
                    "root_section_structure_node_id": (
                        outcome.root_section_structure_node_id
                    ),
                    "payload": assembly_payload,
                }
            )
            view_payloads.append(
                {
                    "root_section_structure_node_id": (
                        outcome.root_section_structure_node_id
                    ),
                    "payload": view_payload,
                }
            )

        provision_summaries.append(
            LlmRequirementRepeatabilityProvisionSummary(
                root_section_structure_node_id=(outcome.root_section_structure_node_id),
                status=outcome.status,
                error_code=outcome.error_code,
                request_identities=request_identities,
                semantic_sha256=semantic_sha256,
                assembly_sha256=assembly_sha256,
                requirement_view_sha256=view_sha256,
                normalized_result_sha256=result_sha256,
                targets=target_summaries,
            )
        )

    semantic_sha256 = _canonical_sha256(semantic_payloads)
    assembly_sha256 = _canonical_sha256(assembly_payloads)
    view_sha256 = _canonical_sha256(view_payloads)
    return LlmRequirementRepeatabilityRunSummary(
        run_index=run_index,
        batch_status=batch.status,
        successful_provision_count=batch.successful_provision_count,
        unresolved_provision_count=batch.unresolved_provision_count,
        blocked_provision_count=batch.blocked_provision_count,
        provider_request_sha256=_canonical_sha256(request_payloads),
        classification_request_sha256=_canonical_sha256(
            classification_request_payloads
        ),
        assembly_request_sha256=_canonical_sha256(assembly_request_payloads),
        semantic_sha256=semantic_sha256,
        assembly_sha256=assembly_sha256,
        requirement_view_sha256=view_sha256,
        normalized_result_sha256=_canonical_sha256(
            {
                "semantic_sha256": semantic_sha256,
                "assembly_sha256": assembly_sha256,
                "requirement_view_sha256": view_sha256,
            }
        ),
        provisions=tuple(provision_summaries),
    )


def _target_summaries(
    semantics: object,
    assembly: object,
    view_payload: dict[str, object],
) -> tuple[LlmRequirementRepeatabilityTargetSummary, ...]:
    assembly_by_unit = {
        unit_id: item
        for item in assembly.items
        for unit_id in item.source_semantic_unit_ids
    }
    normalized_view_by_unit = {
        unit_id: item
        for item in view_payload["nodes"]
        for unit_id in item["source_semantic_unit_ids"]
    }
    result: list[LlmRequirementRepeatabilityTargetSummary] = []
    for annotation in semantics.annotations:
        unit_id = annotation.semantic_unit_id
        owner_id = annotation.owner_structure_node_id
        assembly_item = assembly_by_unit[unit_id]
        semantic_decision = {
            "semantic_unit_id": unit_id,
            "owner_structure_node_id": owner_id,
            "semantic_class": annotation.semantic_class.value,
            "requirement_subtype": (
                annotation.requirement_subtype.value
                if annotation.requirement_subtype is not None
                else None
            ),
            "is_continuation_fragment": annotation.is_continuation_fragment,
            "status": annotation.status.value,
        }
        semantic_grounding = {
            "grounding_spans": [
                {
                    "source_ref": span.source_ref.model_dump(mode="json"),
                    "start_offset": span.start_offset,
                    "end_offset": span.end_offset,
                }
                for span in annotation.grounding_spans
            ],
            "selected_evidence_source_refs": [
                item.model_dump(mode="json")
                for item in annotation.selected_evidence_source_refs
            ],
        }
        semantic_diagnostic = {
            "candidate_source": annotation.candidate_source.value,
            "reason_codes": list(annotation.reason_codes),
        }
        assembly_decision = {
            "semantic_unit_id": assembly_item.semantic_unit_id,
            "source_semantic_unit_ids": list(assembly_item.source_semantic_unit_ids),
            "owner_structure_node_id": assembly_item.owner_structure_node_id,
            "source_order": assembly_item.source_order,
            "include_in_view": assembly_item.include_in_view,
            "role": (
                assembly_item.role.value if assembly_item.role is not None else None
            ),
            "parent_semantic_unit_id": (assembly_item.parent_semantic_unit_id),
            "status": assembly_item.status.value,
        }
        assembly_diagnostic = {
            "reason_codes": list(assembly_item.reason_codes),
        }
        normalized_view = normalized_view_by_unit.get(unit_id)
        semantic_decision_sha256 = _canonical_sha256(semantic_decision)
        semantic_grounding_sha256 = _canonical_sha256(semantic_grounding)
        semantic_diagnostic_sha256 = _canonical_sha256(semantic_diagnostic)
        assembly_decision_sha256 = _canonical_sha256(assembly_decision)
        assembly_diagnostic_sha256 = _canonical_sha256(assembly_diagnostic)
        view_sha256 = (
            _canonical_sha256(normalized_view) if normalized_view is not None else None
        )
        decision_skeleton = _decision_skeleton_node(normalized_view)
        decision_skeleton_sha256 = (
            _canonical_sha256(decision_skeleton)
            if decision_skeleton is not None
            else None
        )
        result.append(
            LlmRequirementRepeatabilityTargetSummary(
                semantic_unit_id=unit_id,
                owner_structure_node_id=owner_id,
                semantic_decision_sha256=semantic_decision_sha256,
                semantic_grounding_sha256=semantic_grounding_sha256,
                semantic_diagnostic_sha256=semantic_diagnostic_sha256,
                assembly_decision_sha256=assembly_decision_sha256,
                assembly_diagnostic_sha256=assembly_diagnostic_sha256,
                requirement_view_node_sha256=view_sha256,
                decision_skeleton_node_sha256=decision_skeleton_sha256,
                normalized_target_result_sha256=_canonical_sha256(
                    {
                        "semantic_decision_sha256": semantic_decision_sha256,
                        "semantic_grounding_sha256": semantic_grounding_sha256,
                        "semantic_diagnostic_sha256": semantic_diagnostic_sha256,
                        "assembly_decision_sha256": assembly_decision_sha256,
                        "assembly_diagnostic_sha256": assembly_diagnostic_sha256,
                        "requirement_view_node_sha256": view_sha256,
                        "decision_skeleton_node_sha256": decision_skeleton_sha256,
                    }
                ),
            )
        )
    return tuple(result)


def _decision_skeleton_node(
    normalized_view: object,
) -> dict[str, object] | None:
    """Keep only the hierarchy and semantic fields that carry a decision.

    Exact RequirementView comparison remains the strict gate.  This narrower
    projection makes it possible to tell a changed requirement judgment from
    variation in supporting CONTENT/REFERENCE presentation without weakening
    that gate.
    """

    if not isinstance(normalized_view, dict):
        return None
    if normalized_view.get("decision_mode") == "NONE":
        return None
    return {
        "semantic_unit_id": normalized_view["semantic_unit_id"],
        "source_semantic_unit_ids": normalized_view["source_semantic_unit_ids"],
        "role": normalized_view["role"],
        "parent_semantic_unit_id": normalized_view["parent_semantic_unit_id"],
        "decision_mode": normalized_view["decision_mode"],
        "semantic_class": normalized_view["semantic_class"],
        "requirement_subtype": normalized_view["requirement_subtype"],
        "source_order": normalized_view["source_order"],
    }


def _normalized_semantics(semantics: object) -> dict[str, object]:
    return {
        "classifier_id": semantics.classifier_id,
        "classifier_version": _stable_implementation_version(
            semantics.classifier_version
        ),
        "semantic_unit_plan_id": semantics.semantic_unit_plan_id,
        "unitizer_version": semantics.unitizer_version,
        "scope_semantic_unit_ids": list(semantics.scope_semantic_unit_ids),
        "scope_owner_structure_node_ids": list(
            semantics.scope_owner_structure_node_ids
        ),
        "annotations": [
            {
                "semantic_unit_id": item.semantic_unit_id,
                "owner_structure_node_id": item.owner_structure_node_id,
                "semantic_class": item.semantic_class.value,
                "requirement_subtype": (
                    item.requirement_subtype.value
                    if item.requirement_subtype is not None
                    else None
                ),
                "candidate_source": item.candidate_source.value,
                "grounding_spans": [
                    {
                        "source_ref": span.source_ref.model_dump(mode="json"),
                        "start_offset": span.start_offset,
                        "end_offset": span.end_offset,
                    }
                    for span in item.grounding_spans
                ],
                "selected_evidence_source_refs": [
                    source_ref.model_dump(mode="json")
                    for source_ref in item.selected_evidence_source_refs
                ],
                "is_continuation_fragment": item.is_continuation_fragment,
                "status": item.status.value,
                "reason_codes": list(item.reason_codes),
            }
            for item in semantics.annotations
        ],
    }


def _normalized_assembly(assembly: object) -> dict[str, object]:
    return {
        "assembler_id": assembly.assembler_id,
        "assembler_version": _stable_implementation_version(assembly.assembler_version),
        "semantic_unit_plan_id": assembly.semantic_unit_plan_id,
        "scope_semantic_unit_ids": list(assembly.scope_semantic_unit_ids),
        "scope_owner_structure_node_ids": list(assembly.scope_owner_structure_node_ids),
        "items": [
            {
                "semantic_unit_id": item.semantic_unit_id,
                "source_semantic_unit_ids": list(item.source_semantic_unit_ids),
                "owner_structure_node_id": item.owner_structure_node_id,
                "source_owner_structure_node_ids": list(
                    item.source_owner_structure_node_ids
                ),
                "unit_order": item.unit_order,
                "source_order": item.source_order,
                "include_in_view": item.include_in_view,
                "role": item.role.value if item.role is not None else None,
                "parent_semantic_unit_id": item.parent_semantic_unit_id,
                "parent_owner_structure_node_id": (item.parent_owner_structure_node_id),
                "status": item.status.value,
                "reason_codes": list(item.reason_codes),
            }
            for item in assembly.items
        ],
    }


def _normalized_requirement_view(semantics: object, view: object) -> dict[str, object]:
    view_unit_by_id = {
        item.requirement_view_node_id: item.semantic_unit_id for item in view.nodes
    }
    return {
        "projector_id": view.projector_id,
        "projector_version": view.projector_version,
        "projection_policy": view.projection_policy.value,
        "semantic_unit_plan_id": view.semantic_unit_plan_id,
        "semantic_unit_ids": list(view.semantic_unit_ids),
        "structure_node_ids": list(view.structure_node_ids),
        "structure_relation_ids": list(view.structure_relation_ids),
        "source_node_ids": list(view.source_node_ids),
        "source_cell_ids": list(view.source_cell_ids),
        "visual_text_observation_ids": list(view.visual_text_observation_ids),
        "nodes": [
            {
                "role": item.role.value,
                "parent_semantic_unit_id": (
                    view_unit_by_id[item.parent_requirement_view_node_id]
                    if item.parent_requirement_view_node_id is not None
                    else None
                ),
                "decision_mode": item.decision_mode.value,
                "semantic_class": item.semantic_class.value,
                "requirement_subtype": (
                    item.requirement_subtype.value
                    if item.requirement_subtype is not None
                    else None
                ),
                "semantic_unit_id": item.semantic_unit_id,
                "source_semantic_unit_ids": list(item.source_semantic_unit_ids),
                "owner_structure_node_id": item.owner_structure_node_id,
                "ancestor_structure_node_ids": list(item.ancestor_structure_node_ids),
                "source_span_ids": list(item.source_span_ids),
                "source_refs": [
                    source_ref.model_dump(mode="json")
                    for source_ref in item.source_refs
                ],
                "full_context_source_refs": [
                    source_ref.model_dump(mode="json")
                    for source_ref in item.full_context_source_refs
                ],
                "relation_ids": list(item.relation_ids),
                "source_order": item.source_order,
            }
            for item in view.nodes
        ],
    }


def _stable_implementation_version(value: str) -> str:
    return value.partition(";result=")[0]


def _batch_scope_identity(batch: LlmRequirementBatchResult) -> object:
    return {
        "root_section_structure_node_ids": list(
            batch.located_scope.root_section_structure_node_ids
        ),
        "scope_structure_node_ids": list(batch.located_scope.scope_structure_node_ids),
        "provisions": [
            {
                "root_section_structure_node_id": (item.root_section_structure_node_id),
                "scope_structure_node_ids": list(item.scope_structure_node_ids),
                "source_order": item.source_order,
            }
            for item in batch.provisions
        ],
    }


def _batch_is_complete(batch: LlmRequirementBatchResult) -> bool:
    return batch.status is LlmRequirementBatchStatus.COMPLETE and all(
        outcome.status is LlmProvisionExtractionStatus.SUCCESS
        and outcome.extraction is not None
        and all(
            run.status is SemanticProviderRunStatus.SUCCESS
            for run in outcome.extraction.provider_runs
        )
        for outcome in batch.provisions
    )


def _unstable_target_ids(
    summaries: tuple[LlmRequirementRepeatabilityRunSummary, ...],
    field_name: str,
) -> tuple[str, ...]:
    values_by_run = tuple(
        {
            target.semantic_unit_id: getattr(target, field_name)
            for provision in summary.provisions
            for target in provision.targets
        }
        for summary in summaries
    )
    target_ids = sorted({target_id for values in values_by_run for target_id in values})
    missing = object()
    return tuple(
        target_id
        for target_id in target_ids
        if len({values.get(target_id, missing) for values in values_by_run}) > 1
    )


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_repetition_inputs(*, repetition_count: int, sampling_seed: int) -> None:
    if (
        isinstance(repetition_count, bool)
        or not isinstance(repetition_count, int)
        or not MIN_LLM_REPEATABILITY_RUNS
        <= repetition_count
        <= MAX_LLM_REPEATABILITY_RUNS
    ):
        raise ValueError("repeatability evaluation requires between three and ten runs")
    if (
        isinstance(sampling_seed, bool)
        or not isinstance(sampling_seed, int)
        or not 0 <= sampling_seed <= _MAX_BAILIAN_SEMANTIC_SEED
    ):
        raise ValueError("sampling_seed must be between zero and 2^31 - 1")


__all__ = [
    "LLM_REQUIREMENT_REPEATABILITY_SCHEMA_VERSION",
    "MAX_LLM_REPEATABILITY_RUNS",
    "MIN_LLM_REPEATABILITY_RUNS",
    "LlmRequirementRepeatabilityMismatchCode",
    "LlmRequirementRepeatabilityExecution",
    "LlmRequirementRepeatabilityProvisionSummary",
    "LlmRequirementRepeatabilityReport",
    "LlmRequirementRepeatabilityRequestIdentity",
    "LlmRequirementRepeatabilityRunSummary",
    "LlmRequirementRepeatabilityStatus",
    "LlmRequirementRepeatabilityTargetSummary",
    "evaluate_llm_requirement_batch_repeatability",
    "run_llm_requirement_batch_repeatability",
]
