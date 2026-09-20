from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.application.extraction.assemble_requirements import (
    RequirementAssemblyRejected,
    validate_requirement_assembly_against_inputs,
)
from app.application.extraction.classify_content import (
    classify_content,
    validate_semantic_classification_against_inputs,
)
from app.application.extraction.build_semantic_unit_plan import (
    build_semantic_unit_plan,
)
from app.application.extraction.locate_llm_scope import (
    LlmExtractionScopeLocator,
    locate_llm_extraction_scope,
)
from app.application.extraction.document_decomposition import (
    DocumentDecompositionProviderPort,
)
from app.application.extraction.project_requirements import (
    _continuation_components,
    _projection_units,
    project_requirements_from_assembly,
)
from app.application.extraction.structure_snapshot import structure_snapshot_id
from app.domain.extraction.requirement_assembly import (
    RequirementAssemblyItem,
    RequirementAssemblySnapshot,
    RequirementAssemblyStatus,
    stable_requirement_assembly_id,
)
from app.domain.extraction.requirement_view import (
    RequirementProjectionRole,
    RequirementViewSnapshot,
)
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.domain.extraction.semantic_terminal_receipts import (
    SemanticUnitTerminalReceipt,
    SemanticUnitTerminalStatus,
    semantic_unit_source_coverage_sha256,
    stable_semantic_unit_terminal_receipt_id,
)
from app.domain.extraction.semantic_units import SemanticUnit, SemanticUnitPlan
from app.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticAnnotation,
    SemanticAnnotationStatus,
    SemanticCandidateSource,
    SemanticClass,
    SemanticClassificationSnapshot,
)
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.structure import (
    StructureNode,
    StructureNodeKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)
from app.ports.semantic_provider import (
    PreflightSemanticProviderPort,
    RequirementAssemblyDecision,
    RequirementAssemblyNodeInput,
    RequirementAssemblyRequest,
    SemanticClassificationRequest,
    SemanticDecision,
    SemanticEvidenceInput,
    SemanticImageInput,
    SemanticNodeInput,
    SemanticProviderPort,
    SemanticProviderRequestRejected,
    SemanticProviderRequestRejectionCode,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderPort,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
    RequirementAtomizationSourceSegment,
)


SEMANTIC_PLANNING_RUNTIME_VERSION = "source-bound-semantic-planning/v4"
MODEL_SEMANTIC_CLASSIFIER_VERSION = "1.15.2"
MODEL_REQUIREMENT_ASSEMBLER_VERSION = "1.7.1"
DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST = 2
_RETRYABLE_MODEL_UNRESOLVED_REASON_CODES = frozenset({"provider_model_unresolved"})
_RETRYABLE_ASSEMBLY_UNRESOLVED_REASON_CODES = frozenset(
    {
        "provider_assembly_scope_missing",
        "provider_assembly_scope_duplicated",
        "provider_assembly_unresolved",
        "provider_assembly_invalid",
        "provider_assembly_shape_invalid",
        "provider_assembly_constraint_invalid",
    }
)
_SPREADSHEET_ERROR_PLACEHOLDER_PATTERN = re.compile(
    r"(?:(?:#REF!|#N/A|#VALUE!|#DIV/0!|#NAME\?|#NUM!|#NULL!|#SPILL!|#CALC!))+",
    re.IGNORECASE,
)


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ProviderSemanticClassification:
    semantics: SemanticClassificationSnapshot
    semantic_unit_plan: SemanticUnitPlan
    runs: tuple[SemanticProviderRunRecord, ...]
    terminal_receipts: tuple[SemanticUnitTerminalReceipt, ...]
    request_windows: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if (
            self.semantics.source_ir_id != self.semantic_unit_plan.source_ir_id
            or self.semantics.structure_snapshot_id
            != self.semantic_unit_plan.structure_snapshot_id
            or self.semantics.scope_structure_node_ids
            != self.semantic_unit_plan.scope_owner_structure_node_ids
        ):
            raise ValueError(
                "provider semantics and semantic unit plan bindings must match"
            )
        if not self.runs or len({item.provider_run_id for item in self.runs}) != len(
            self.runs
        ):
            raise ValueError(
                "provider classification runs must be non-empty and unique"
            )
        expected_windows = tuple(
            target_id for window in self.request_windows for target_id in window
        )
        if (
            not self.request_windows
            or expected_windows
            != self.semantic_unit_plan.scope_owner_structure_node_ids
        ):
            raise ValueError(
                "classification request windows must exactly partition owner scope"
            )
        annotations_by_unit = {
            item.semantic_unit_id: item for item in self.semantics.annotations
        }
        runs_by_id = {item.provider_run_id: item for item in self.runs}
        if len(self.terminal_receipts) != len(self.semantic_unit_plan.units):
            raise ValueError("classification requires one terminal receipt per unit")
        for unit, receipt in zip(
            self.semantic_unit_plan.units,
            self.terminal_receipts,
            strict=True,
        ):
            annotation = annotations_by_unit.get(unit.semantic_unit_id)
            if (
                receipt.semantic_unit_plan_id
                != self.semantic_unit_plan.semantic_unit_plan_id
                or receipt.semantic_unit_id != unit.semantic_unit_id
                or receipt.owner_structure_node_id != unit.owner_structure_node_id
                or receipt.unit_order != unit.unit_order
                or receipt.source_coverage_sha256
                != semantic_unit_source_coverage_sha256(unit)
                or receipt.planning_runtime_version != SEMANTIC_PLANNING_RUNTIME_VERSION
                or receipt.provider_run_id not in runs_by_id
                or annotation is None
                or annotation.provider_run_id != receipt.provider_run_id
                or (
                    receipt.status is SemanticUnitTerminalStatus.UNRESOLVED
                    and annotation.semantic_class is not SemanticClass.UNRESOLVED
                )
                or (
                    receipt.status is SemanticUnitTerminalStatus.RESOLVED
                    and annotation.semantic_class is SemanticClass.UNRESOLVED
                )
            ):
                raise ValueError(
                    "classification terminal receipt does not match its source unit"
                )

    @property
    def run(self) -> SemanticProviderRunRecord:
        """Return the run for the common single-window call."""

        return self.runs[0]


@dataclass(frozen=True)
class ProviderRequirementAssembly:
    assembly: RequirementAssemblySnapshot
    runs: tuple[SemanticProviderRunRecord, ...]

    def __post_init__(self) -> None:
        if not self.runs or len({item.provider_run_id for item in self.runs}) != len(
            self.runs
        ):
            raise ValueError("provider assembly runs must be non-empty and unique")
        if self.assembly.provider_run_id != self.runs[0].provider_run_id:
            raise ValueError("assembly primary run must be the first retained run")

    @property
    def run(self) -> SemanticProviderRunRecord:
        return self.runs[0]


@dataclass(frozen=True)
class ProviderSemanticClassificationPlanItem:
    semantic_unit_ids: tuple[str, ...]
    scaffold: SemanticClassificationSnapshot
    request: SemanticClassificationRequest
    local_rejection_code: SemanticProviderRequestRejectionCode | None = None

    def __post_init__(self) -> None:
        if (
            not self.semantic_unit_ids
            or len(self.semantic_unit_ids) != len(set(self.semantic_unit_ids))
            or len(self.semantic_unit_ids) != len(self.request.scope_structure_node_ids)
        ):
            raise ValueError(
                "classification plan item semantic units must exactly cover its scope"
            )
        if (
            self.scaffold.source_ir_id != self.request.source_ir_id
            or self.scaffold.structure_snapshot_id != self.request.structure_snapshot_id
            or self.scaffold.scope_structure_node_ids
            != self.request.scope_structure_node_ids
        ):
            raise ValueError(
                "classification plan item scaffold and request bindings must match"
            )
        if self.local_rejection_code is not None and not isinstance(
            self.local_rejection_code,
            SemanticProviderRequestRejectionCode,
        ):
            raise TypeError("classification local rejection code is invalid")


@dataclass(frozen=True)
class ProviderSemanticClassificationPlan:
    grounding_scaffold: SemanticClassificationSnapshot
    semantic_unit_plan: SemanticUnitPlan
    items: tuple[ProviderSemanticClassificationPlanItem, ...]

    def __post_init__(self) -> None:
        if (
            self.grounding_scaffold.source_ir_id != self.semantic_unit_plan.source_ir_id
            or self.grounding_scaffold.structure_snapshot_id
            != self.semantic_unit_plan.structure_snapshot_id
            or self.grounding_scaffold.scope_structure_node_ids
            != self.semantic_unit_plan.scope_owner_structure_node_ids
        ):
            raise ValueError(
                "classification grounding and semantic unit plan bindings must match"
            )
        if not self.items:
            raise ValueError("classification plans require request items")
        unit_ids = tuple(
            unit_id for item in self.items for unit_id in item.semantic_unit_ids
        )
        owner_ids = tuple(
            owner_id
            for item in self.items
            for owner_id in item.request.scope_structure_node_ids
        )
        if (
            unit_ids != self.semantic_unit_plan.semantic_unit_ids
            or owner_ids != self.semantic_unit_plan.scope_owner_structure_node_ids
        ):
            raise ValueError(
                "classification request windows must exactly partition semantic units"
            )


@dataclass(frozen=True)
class ProviderRequirementAssemblyPlanItem:
    target_structure_node_ids: tuple[str, ...]
    context_structure_node_ids: tuple[str, ...]
    request: RequirementAssemblyRequest
    local_rejection_code: SemanticProviderRequestRejectionCode | None = None

    def __post_init__(self) -> None:
        if (
            not self.target_structure_node_ids
            or len(self.target_structure_node_ids)
            != len(set(self.target_structure_node_ids))
            or len(self.context_structure_node_ids)
            != len(set(self.context_structure_node_ids))
            or set(self.target_structure_node_ids).intersection(
                self.context_structure_node_ids
            )
        ):
            raise ValueError("assembly plan targets and context are invalid")
        expected_scope = tuple(
            item.target_structure_node_id for item in self.request.nodes
        )
        if expected_scope != self.request.scope_structure_node_ids or set(
            expected_scope
        ) != set((*self.target_structure_node_ids, *self.context_structure_node_ids)):
            raise ValueError(
                "assembly request must exactly contain target and context nodes"
            )
        if self.local_rejection_code is not None and not isinstance(
            self.local_rejection_code,
            SemanticProviderRequestRejectionCode,
        ):
            raise TypeError("assembly local rejection code is invalid")


@dataclass(frozen=True)
class RequirementViewAtomization:
    requirement_view_node_id: str
    request_sha256: str
    result: RequirementAtomizationProviderResult


@dataclass(frozen=True)
class LlmRequirementExtractionResult:
    semantics: SemanticClassificationSnapshot
    assembly: RequirementAssemblySnapshot
    requirement_view: RequirementViewSnapshot
    provider_runs: tuple[SemanticProviderRunRecord, ...]
    semantic_terminal_receipts: tuple[SemanticUnitTerminalReceipt, ...]
    atomization_results: tuple[RequirementViewAtomization, ...] = ()
    planning_runtime_version: str = SEMANTIC_PLANNING_RUNTIME_VERSION

    def __post_init__(self) -> None:
        if self.planning_runtime_version != SEMANTIC_PLANNING_RUNTIME_VERSION:
            raise ValueError("extraction planning runtime identity is incompatible")
        if len(self.semantic_terminal_receipts) != len(
            self.semantics.scope_semantic_unit_ids
        ):
            raise ValueError(
                "extraction requires one terminal receipt per semantic unit"
            )
        if (
            tuple(item.semantic_unit_id for item in self.semantic_terminal_receipts)
            != self.semantics.scope_semantic_unit_ids
        ):
            raise ValueError("extraction terminal receipts do not match semantic scope")
        if len({item.provider_run_id for item in self.provider_runs}) != len(
            self.provider_runs
        ):
            raise ValueError("extraction provider run receipts must be unique")
        atomized_ids = tuple(
            item.requirement_view_node_id for item in self.atomization_results
        )
        atomization_run_ids = tuple(
            item.result.run.provider_run_id for item in self.atomization_results
        )
        primary_view_ids = {
            item.requirement_view_node_id
            for item in self.requirement_view.nodes
            if item.role is RequirementProjectionRole.PRIMARY
            and item.semantic_class is SemanticClass.REQUIREMENT
        }
        if (
            len(atomized_ids) != len(set(atomized_ids))
            or not set(atomized_ids).issubset(primary_view_ids)
            or set(atomization_run_ids).intersection(
                item.provider_run_id for item in self.provider_runs
            )
            or any(
                not isinstance(item.result, RequirementAtomizationProviderResult)
                or not re.fullmatch(r"[0-9a-f]{64}", item.request_sha256)
                for item in self.atomization_results
            )
        ):
            raise ValueError("extraction atomization results are invalid")


def extract_requirements_with_provider(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: SemanticProviderPort,
    *,
    scope_structure_node_ids: tuple[str, ...] | None = None,
    scope_locator: LlmExtractionScopeLocator | None = None,
    images: tuple[SemanticImageInput, ...] = (),
    max_images_per_classification_request: int = (
        DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST
    ),
    max_nodes_per_classification_request: int = 80,
    pack_adjacent_provisions: bool = False,
    classification_target_windows: tuple[tuple[str, ...], ...] | None = None,
    atomization_provider: (
        RequirementAtomizationProviderPort | DocumentDecompositionProviderPort | None
    ) = None,
) -> LlmRequirementExtractionResult:
    """Execute the review-only semantic, assembly and projection stages."""

    if scope_structure_node_ids is not None and scope_locator is not None:
        raise ValueError("exact scope IDs and a scope locator are mutually exclusive")
    if scope_locator is not None:
        scope_structure_node_ids = locate_llm_extraction_scope(
            source_ir,
            tree,
            scope_locator,
        ).scope_structure_node_ids

    classification = classify_content_with_provider(
        source_ir,
        tree,
        provider,
        scope_structure_node_ids=scope_structure_node_ids,
        images=images,
        max_nodes_per_request=max_nodes_per_classification_request,
        max_images_per_request=max_images_per_classification_request,
        pack_adjacent_provisions=pack_adjacent_provisions,
        target_windows=classification_target_windows,
    )
    assembly = assemble_requirements_with_provider(
        source_ir,
        tree,
        classification.semantics,
        provider,
        target_windows=classification.request_windows,
        max_nodes_per_request=max_nodes_per_classification_request,
    )
    view = project_requirements_from_assembly(
        source_ir,
        tree,
        classification.semantics,
        assembly.assembly,
    )
    atomization_results = (
        _atomize_primary_requirement_view_nodes(
            source_ir,
            view,
            atomization_provider,
        )
        if atomization_provider is not None
        else ()
    )
    return LlmRequirementExtractionResult(
        semantics=classification.semantics,
        assembly=assembly.assembly,
        requirement_view=view,
        provider_runs=(*classification.runs, *assembly.runs),
        semantic_terminal_receipts=classification.terminal_receipts,
        atomization_results=atomization_results,
    )


def _atomize_primary_requirement_view_nodes(
    source_ir: SourceIRSnapshot,
    view: RequirementViewSnapshot,
    provider: RequirementAtomizationProviderPort | DocumentDecompositionProviderPort,
) -> tuple[RequirementViewAtomization, ...]:
    planned: list[tuple[str, RequirementAtomizationProviderRequest, str]] = []
    for node in view.nodes:
        if (
            node.role is not RequirementProjectionRole.PRIMARY
            or node.semantic_class is not SemanticClass.REQUIREMENT
        ):
            continue
        source_refs = tuple(
            dict.fromkeys((*node.source_refs, *node.full_context_source_refs))
        )
        segments: list[RequirementAtomizationSourceSegment] = []
        source_fingerprints: list[dict[str, object]] = []
        for source_ref in source_refs:
            text = _resolve_source_text(source_ref, source_ir)
            if not text.strip():
                continue
            segments.append(
                RequirementAtomizationSourceSegment(
                    segment_index=len(segments),
                    semantic_unit_id=node.semantic_unit_id,
                    owner_structure_node_id=node.owner_structure_node_id,
                    source_start_offset=0,
                    source_end_offset=len(text),
                    source_text=text,
                )
            )
            source_fingerprints.append(
                {
                    "source_ref": source_ref.model_dump(mode="json"),
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
        if not segments:
            continue
        lineage_sha256 = _canonical_json_sha256(
            {
                "source_ir_id": source_ir.source_ir_id,
                "requirement_view_snapshot_id": view.requirement_view_snapshot_id,
                "requirement_view_node_id": node.requirement_view_node_id,
                "semantic_unit_id": node.semantic_unit_id,
                "owner_structure_node_id": node.owner_structure_node_id,
                "source_fingerprints": source_fingerprints,
            }
        )
        request = RequirementAtomizationProviderRequest(
            lineage_sha256=lineage_sha256,
            review_node_id=node.requirement_view_node_id,
            requirement_subtype=(
                node.requirement_subtype.value
                if node.requirement_subtype is not None
                else "UNSPECIFIED_REQUIREMENT"
            ),
            source_segments=tuple(segments),
        )
        planned.append(
            (
                node.requirement_view_node_id,
                request,
                _canonical_json_sha256(request.model_dump(mode="json")),
            )
        )
    # The document-batch port is Agent-native and consumes parent source spans,
    # not RequirementView/Structure owners.  The legacy node lane therefore
    # keeps its existing single-parent atomization port instead of adapting
    # Agent-native results back into the old node universe.
    if not isinstance(provider, RequirementAtomizationProviderPort):
        raise TypeError("atomization provider has no supported document operation")
    output = []
    for parent_id, request, request_sha256 in planned:
        result = provider.atomize(request)
        output.append(
            RequirementViewAtomization(
                requirement_view_node_id=parent_id,
                request_sha256=request_sha256,
                result=result,
            )
        )
    return tuple(output)


def classify_content_with_provider(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: SemanticProviderPort,
    *,
    scope_structure_node_ids: tuple[str, ...] | None = None,
    images: tuple[SemanticImageInput, ...] = (),
    max_nodes_per_request: int = 80,
    max_images_per_request: int = DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST,
    pack_adjacent_provisions: bool = False,
    target_windows: tuple[tuple[str, ...], ...] | None = None,
) -> ProviderSemanticClassification:
    """Create MODEL annotations while retaining deterministic source grounding."""

    if not isinstance(provider, SemanticProviderPort):
        raise TypeError("provider must implement SemanticProviderPort")
    plan = plan_content_classification_provider_requests(
        source_ir,
        tree,
        scope_structure_node_ids=scope_structure_node_ids,
        images=images,
        max_nodes_per_request=max_nodes_per_request,
        max_images_per_request=max_images_per_request,
        pack_adjacent_provisions=pack_adjacent_provisions,
        target_windows=target_windows,
    )
    grounding_scaffold = plan.grounding_scaffold
    window_results: list[tuple[SemanticDecision, SemanticProviderRunRecord]] = []
    runs: list[SemanticProviderRunRecord] = []
    for item in plan.items:
        item_results, item_runs = _classify_plan_item_adaptively(
            source_ir,
            tree,
            provider,
            plan=plan,
            item=item,
            images=images,
        )
        runs.extend(item_runs)
        window_results.extend(item_results)
    if len({item.provider_run_id for item in runs}) != len(runs):
        raise ValueError("semantic provider retries produced duplicate run identities")
    provision_normalized = _normalize_provision_subtypes(
        tuple(decision for decision, _run in window_results),
        tree=tree,
    )
    window_results = [
        (decision, run)
        for decision, (_previous, run) in zip(
            provision_normalized,
            window_results,
            strict=True,
        )
    ]
    window_results = _resolve_cross_window_continuation_conflicts(
        window_results,
        tree=tree,
    )
    window_results = _normalize_model_continuation_parent_semantics(
        window_results,
        tree=tree,
    )
    results_by_target: dict[
        str, tuple[SemanticDecision, SemanticProviderRunRecord]
    ] = {}
    for decision, run in window_results:
        if decision.target_structure_node_id in results_by_target:
            raise ValueError("semantic provision results contain duplicate targets")
        results_by_target[decision.target_structure_node_id] = (decision, run)
    if set(results_by_target) != set(grounding_scaffold.scope_structure_node_ids):
        raise ValueError("semantic provision results do not cover the exact scope")
    window_results = [
        results_by_target[target_id]
        for target_id in grounding_scaffold.scope_structure_node_ids
    ]
    # Snapshot identity follows the normalized semantic decisions, not the
    # provider run timestamps/response-envelope IDs.  Repeating the same model
    # input and receiving the same decisions must therefore produce the same
    # assembly input, while every annotation still retains its exact run ID for
    # audit traceability.
    result_digest = hashlib.sha256(
        json.dumps(
            {
                "planning_runtime_version": SEMANTIC_PLANNING_RUNTIME_VERSION,
                "request_windows": [
                    list(item.request.scope_structure_node_ids) for item in plan.items
                ],
                "decisions": [
                    decision.model_dump(mode="json")
                    for decision, _run in window_results
                ],
                "prompt_versions": [item.prompt_version for item in runs],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    classifier_version = _version_with_result(
        MODEL_SEMANTIC_CLASSIFIER_VERSION,
        provider.model_id,
        runs[-1].prompt_version,
        result_digest,
    )
    scaffold = classify_content(
        source_ir,
        tree,
        scope_structure_node_ids=scope_structure_node_ids,
        classifier_id=provider.provider_id,
        classifier_version=classifier_version,
    )
    annotations = tuple(
        annotation.model_copy(
            update={
                "semantic_class": decision.semantic_class,
                "requirement_subtype": decision.requirement_subtype,
                "candidate_source": SemanticCandidateSource.MODEL,
                "provider_run_id": run.provider_run_id,
                "selected_evidence_source_refs": decision.evidence_source_refs,
                "is_continuation_fragment": decision.is_continuation_fragment,
                "status": (
                    SemanticAnnotationStatus.UNRESOLVED
                    if decision.semantic_class is SemanticClass.UNRESOLVED
                    else SemanticAnnotationStatus.CANDIDATE
                ),
                "reason_codes": decision.reason_codes,
            }
        )
        for annotation, (decision, run) in zip(
            scaffold.annotations,
            window_results,
            strict=True,
        )
    )
    semantics = scaffold.model_copy(update={"annotations": annotations})
    validate_semantic_classification_against_inputs(semantics, source_ir, tree)
    terminal_receipts = tuple(
        _semantic_terminal_receipt(
            plan.semantic_unit_plan,
            unit=unit,
            decision=decision,
            run=run,
        )
        for unit, (decision, run) in zip(
            plan.semantic_unit_plan.units,
            window_results,
            strict=True,
        )
    )
    return ProviderSemanticClassification(
        semantics=semantics,
        semantic_unit_plan=plan.semantic_unit_plan,
        runs=tuple(runs),
        terminal_receipts=terminal_receipts,
        request_windows=tuple(
            item.request.scope_structure_node_ids for item in plan.items
        ),
    )


def plan_content_classification_grounding_request(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
) -> SemanticClassificationRequest:
    """Build one full-scope evidence request without semantic window constraints.

    Read-only consumers such as structure audit need the common source grounding
    but do not execute semantic classification.  In particular, an existing
    non-contiguous ``CONTINUES`` relation is evidence for audit rather than a
    reason to reject the document before it can be inspected.
    """

    grounding_scaffold = classify_content(source_ir, tree)
    if not grounding_scaffold.scope_structure_node_ids:
        raise ValueError("semantic grounding requires a non-empty scope")
    return _classification_request(
        source_ir,
        tree,
        grounding_scaffold,
        images=(),
    )


def plan_content_classification_provider_requests(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    *,
    scope_structure_node_ids: tuple[str, ...] | None = None,
    images: tuple[SemanticImageInput, ...] = (),
    max_nodes_per_request: int = 80,
    max_images_per_request: int = DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST,
    pack_adjacent_provisions: bool = False,
    target_windows: tuple[tuple[str, ...], ...] | None = None,
) -> ProviderSemanticClassificationPlan:
    """Build every classification request without invoking a provider."""

    if isinstance(max_nodes_per_request, bool) or max_nodes_per_request < 1:
        raise ValueError("max_nodes_per_request must be a positive integer")
    if isinstance(max_images_per_request, bool) or max_images_per_request < 1:
        raise ValueError("max_images_per_request must be a positive integer")
    if not isinstance(pack_adjacent_provisions, bool):
        raise TypeError("pack_adjacent_provisions must be a boolean")
    grounding_scaffold = classify_content(
        source_ir,
        tree,
        scope_structure_node_ids=scope_structure_node_ids,
    )
    if not grounding_scaffold.scope_structure_node_ids:
        raise ValueError("model semantic classification requires a non-empty scope")
    _validate_complete_section_provisions(
        tree,
        grounding_scaffold.scope_structure_node_ids,
    )
    semantic_unit_plan = build_semantic_unit_plan(
        source_ir,
        tree,
        grounding_scaffold,
    )
    units_by_owner = {
        item.owner_structure_node_id: item for item in semantic_unit_plan.units
    }
    context_scaffold = (
        grounding_scaffold
        if scope_structure_node_ids is None
        else classify_content(source_ir, tree)
    )
    if target_windows is None:
        windows = _semantic_windows(
            tree,
            semantic_unit_plan.scope_owner_structure_node_ids,
            max_nodes_per_request=max_nodes_per_request,
            pack_adjacent_provisions=pack_adjacent_provisions,
        )
    else:
        if (
            not target_windows
            or any(not window for window in target_windows)
            or tuple(item for window in target_windows for item in window)
            != semantic_unit_plan.scope_owner_structure_node_ids
        ):
            raise ValueError(
                "classification target windows must exact-partition semantic scope"
            )
        bounded_windows = tuple(
            bounded
            for window in target_windows
            for bounded in _semantic_windows(
                tree,
                window,
                max_nodes_per_request=max_nodes_per_request,
                pack_adjacent_provisions=True,
            )
        )
        packed: list[tuple[str, ...]] = []
        for window in bounded_windows:
            if packed and len((*packed[-1], *window)) <= max_nodes_per_request:
                packed[-1] = (*packed[-1], *window)
            else:
                packed.append(window)
        windows = tuple(packed)
    windows = _split_semantic_windows_by_image_budget(
        windows,
        image_pages_by_target=_semantic_image_pages_by_target(
            grounding_scaffold,
            source_ir,
            tree,
            images,
        ),
        max_images_per_request=max_images_per_request,
        indivisible_group_by_target=_semantic_indivisible_group_by_target(
            tree,
            semantic_unit_plan.scope_owner_structure_node_ids,
        ),
    )
    items: list[ProviderSemanticClassificationPlanItem] = []
    for window in windows:
        window_scaffold = classify_content(
            source_ir,
            tree,
            scope_structure_node_ids=window,
        )
        request = _classification_request(
            source_ir,
            tree,
            window_scaffold,
            context_scaffold=context_scaffold,
            images=_images_for_scaffold(images, window_scaffold, source_ir, tree),
        )
        items.append(
            ProviderSemanticClassificationPlanItem(
                semantic_unit_ids=tuple(
                    units_by_owner[owner_id].semantic_unit_id for owner_id in window
                ),
                scaffold=window_scaffold,
                request=request,
                local_rejection_code=(
                    SemanticProviderRequestRejectionCode.NODE_BUDGET_EXCEEDED
                    if len(window) > max_nodes_per_request
                    else None
                ),
            )
        )
    return ProviderSemanticClassificationPlan(
        grounding_scaffold=grounding_scaffold,
        semantic_unit_plan=semantic_unit_plan,
        items=tuple(items),
    )


def _classify_plan_item_adaptively(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: SemanticProviderPort,
    *,
    plan: ProviderSemanticClassificationPlan,
    item: ProviderSemanticClassificationPlanItem,
    images: tuple[SemanticImageInput, ...],
    allow_minimal_context_retry: bool = True,
) -> tuple[
    list[tuple[SemanticDecision, SemanticProviderRunRecord]],
    list[SemanticProviderRunRecord],
]:
    if item.local_rejection_code is not None:
        return _terminally_reject_classification_item(
            provider,
            item,
            error_code=item.local_rejection_code.value,
        )
    try:
        if isinstance(provider, PreflightSemanticProviderPort):
            provider.preflight_classification_request(item.request)
        result = provider.classify_nodes(item.request)
    except SemanticProviderRequestRejected as exc:
        split = _bisect_source_bound_scope(
            tree,
            item.request.scope_structure_node_ids,
        )
        if split is None:
            return _terminally_reject_classification_item(
                provider,
                item,
                error_code=exc.code.value,
            )
        return _classify_child_scopes(
            source_ir,
            tree,
            provider,
            plan=plan,
            child_scopes=split,
            images=images,
        )
    runs = [result.run]
    if _is_output_schema_invalid(result.run):
        split = _bisect_source_bound_scope(
            tree,
            item.request.scope_structure_node_ids,
        )
        if split is not None:
            resolved, child_runs = _classify_child_scopes(
                source_ir,
                tree,
                provider,
                plan=plan,
                child_scopes=split,
                images=images,
            )
            return resolved, [*runs, *child_runs]
        if allow_minimal_context_retry:
            minimal_item = _classification_plan_item_for_scope(
                source_ir,
                tree,
                plan=plan,
                scope_structure_node_ids=item.request.scope_structure_node_ids,
                images=images,
                include_adjacent_siblings=False,
            )
            if minimal_item.request != item.request:
                resolved, retry_runs = _classify_plan_item_adaptively(
                    source_ir,
                    tree,
                    provider,
                    plan=plan,
                    item=minimal_item,
                    images=images,
                    allow_minimal_context_retry=False,
                )
                return resolved, [*runs, *retry_runs]

    normalized = _normalize_semantic_decisions(
        result.decisions,
        scaffold=item.scaffold,
        request=item.request,
        tree=tree,
        run=result.run,
    )
    retried, child_runs = _retry_model_unresolved_child_scopes(
        source_ir,
        tree,
        provider,
        plan=plan,
        item=item,
        normalized=normalized,
        run=result.run,
        images=images,
        allow_minimal_context_retry=allow_minimal_context_retry,
    )
    return retried, [*runs, *child_runs]


def _retry_model_unresolved_child_scopes(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: SemanticProviderPort,
    *,
    plan: ProviderSemanticClassificationPlan,
    item: ProviderSemanticClassificationPlanItem,
    normalized: tuple[SemanticDecision, ...],
    run: SemanticProviderRunRecord,
    images: tuple[SemanticImageInput, ...],
    allow_minimal_context_retry: bool,
) -> tuple[
    list[tuple[SemanticDecision, SemanticProviderRunRecord]],
    list[SemanticProviderRunRecord],
]:
    """Retry only model-declared unresolved targets behind smaller scopes.

    A valid provider envelope can still explicitly decline individual
    classifications.  Those targets may become judgeable once unrelated peers
    are removed, but already resolved peers must keep their original decision
    and run identity.  Source-bound bisection strictly reduces the atomic-group
    count on every recursive path; a singleton or indivisible continuation
    group therefore remains terminal instead of retrying forever.
    """

    retryable_target_ids = {
        decision.target_structure_node_id
        for decision in normalized
        if decision.semantic_class is SemanticClass.UNRESOLVED
        and decision.reason_codes
        and set(decision.reason_codes).issubset(
            _RETRYABLE_MODEL_UNRESOLVED_REASON_CODES
        )
    }
    current_results = {
        decision.target_structure_node_id: (decision, run) for decision in normalized
    }
    if not retryable_target_ids:
        return (
            [
                current_results[target_id]
                for target_id in item.request.scope_structure_node_ids
            ],
            [],
        )

    split = _bisect_source_bound_scope(
        tree,
        item.request.scope_structure_node_ids,
    )
    if split is None:
        if allow_minimal_context_retry:
            minimal_item = _classification_plan_item_for_scope(
                source_ir,
                tree,
                plan=plan,
                scope_structure_node_ids=item.request.scope_structure_node_ids,
                images=images,
                include_adjacent_siblings=False,
            )
            if minimal_item.request != item.request:
                return _classify_plan_item_adaptively(
                    source_ir,
                    tree,
                    provider,
                    plan=plan,
                    item=minimal_item,
                    images=images,
                    allow_minimal_context_retry=False,
                )
        return (
            [
                current_results[target_id]
                for target_id in item.request.scope_structure_node_ids
            ],
            [],
        )

    child_runs: list[SemanticProviderRunRecord] = []
    for child_scope in split:
        child_retryable_target_ids = retryable_target_ids.intersection(child_scope)
        if not child_retryable_target_ids:
            continue
        child_item = _classification_plan_item_for_scope(
            source_ir,
            tree,
            plan=plan,
            scope_structure_node_ids=child_scope,
            images=images,
        )
        child_results, executed_child_runs = _classify_plan_item_adaptively(
            source_ir,
            tree,
            provider,
            plan=plan,
            item=child_item,
            images=images,
        )
        child_results_by_target = {
            decision.target_structure_node_id: (decision, child_run)
            for decision, child_run in child_results
        }
        for target_id in child_retryable_target_ids:
            current_results[target_id] = child_results_by_target[target_id]
        child_runs.extend(executed_child_runs)

    return (
        [
            current_results[target_id]
            for target_id in item.request.scope_structure_node_ids
        ],
        child_runs,
    )


def _classify_child_scopes(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: SemanticProviderPort,
    *,
    plan: ProviderSemanticClassificationPlan,
    child_scopes: tuple[tuple[str, ...], tuple[str, ...]],
    images: tuple[SemanticImageInput, ...],
) -> tuple[
    list[tuple[SemanticDecision, SemanticProviderRunRecord]],
    list[SemanticProviderRunRecord],
]:
    resolved: list[tuple[SemanticDecision, SemanticProviderRunRecord]] = []
    runs: list[SemanticProviderRunRecord] = []
    for child_scope in child_scopes:
        child_item = _classification_plan_item_for_scope(
            source_ir,
            tree,
            plan=plan,
            scope_structure_node_ids=child_scope,
            images=images,
        )
        child_results, child_runs = _classify_plan_item_adaptively(
            source_ir,
            tree,
            provider,
            plan=plan,
            item=child_item,
            images=images,
        )
        resolved.extend(child_results)
        runs.extend(child_runs)
    return resolved, runs


def _terminally_reject_classification_item(
    provider: SemanticProviderPort,
    item: ProviderSemanticClassificationPlanItem,
    *,
    error_code: str,
) -> tuple[
    list[tuple[SemanticDecision, SemanticProviderRunRecord]],
    list[SemanticProviderRunRecord],
]:
    run = _skipped_classification_provider_run(
        provider,
        item.request,
        error_code=error_code,
    )
    decisions = tuple(
        _unresolved_semantic(node.target_structure_node_id, error_code)
        for node in item.request.nodes
    )
    return [(decision, run) for decision in decisions], [run]


def _classification_plan_item_for_scope(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    *,
    plan: ProviderSemanticClassificationPlan,
    scope_structure_node_ids: tuple[str, ...],
    images: tuple[SemanticImageInput, ...],
    include_adjacent_siblings: bool = True,
) -> ProviderSemanticClassificationPlanItem:
    if not scope_structure_node_ids:
        raise ValueError("adaptive classification scope must not be empty")
    units_by_owner = {
        item.owner_structure_node_id: item for item in plan.semantic_unit_plan.units
    }
    if any(owner_id not in units_by_owner for owner_id in scope_structure_node_ids):
        raise ValueError("adaptive classification scope escaped its semantic unit plan")
    scaffold = classify_content(
        source_ir,
        tree,
        scope_structure_node_ids=scope_structure_node_ids,
    )
    context_scaffold = classify_content(source_ir, tree)
    request_images = _images_for_scaffold(images, scaffold, source_ir, tree)
    return ProviderSemanticClassificationPlanItem(
        semantic_unit_ids=tuple(
            units_by_owner[owner_id].semantic_unit_id
            for owner_id in scope_structure_node_ids
        ),
        scaffold=scaffold,
        request=_classification_request(
            source_ir,
            tree,
            scaffold,
            context_scaffold=context_scaffold,
            images=request_images,
            include_adjacent_siblings=include_adjacent_siblings,
        ),
    )


def _semantic_terminal_receipt(
    plan: SemanticUnitPlan,
    *,
    unit: SemanticUnit,
    decision: SemanticDecision,
    run: SemanticProviderRunRecord,
) -> SemanticUnitTerminalReceipt:
    semantic_unit = next(
        item for item in plan.units if item.semantic_unit_id == unit.semantic_unit_id
    )
    status = (
        SemanticUnitTerminalStatus.UNRESOLVED
        if decision.semantic_class is SemanticClass.UNRESOLVED
        else SemanticUnitTerminalStatus.RESOLVED
    )
    reason_codes = decision.reason_codes or ("model_classification",)
    source_coverage_sha256 = semantic_unit_source_coverage_sha256(semantic_unit)
    receipt_id = stable_semantic_unit_terminal_receipt_id(
        semantic_unit_plan_id=plan.semantic_unit_plan_id,
        semantic_unit_id=semantic_unit.semantic_unit_id,
        owner_structure_node_id=semantic_unit.owner_structure_node_id,
        unit_order=semantic_unit.unit_order,
        source_coverage_sha256=source_coverage_sha256,
        planning_runtime_version=SEMANTIC_PLANNING_RUNTIME_VERSION,
        status=status,
        provider_run_id=run.provider_run_id,
        reason_codes=reason_codes,
    )
    return SemanticUnitTerminalReceipt(
        terminal_receipt_id=receipt_id,
        semantic_unit_plan_id=plan.semantic_unit_plan_id,
        semantic_unit_id=semantic_unit.semantic_unit_id,
        owner_structure_node_id=semantic_unit.owner_structure_node_id,
        unit_order=semantic_unit.unit_order,
        source_coverage_sha256=source_coverage_sha256,
        planning_runtime_version=SEMANTIC_PLANNING_RUNTIME_VERSION,
        status=status,
        provider_run_id=run.provider_run_id,
        reason_codes=reason_codes,
    )


def _is_output_schema_invalid(run: SemanticProviderRunRecord) -> bool:
    return (
        run.status is SemanticProviderRunStatus.INVALID_OUTPUT
        and run.error_code == "provider_output_schema_invalid"
    )


def plan_requirement_assembly_provider_request(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantic_scaffold: SemanticClassificationSnapshot,
) -> RequirementAssemblyRequest:
    """Build a conservative maximum-payload assembly request without transport.

    The provider can return any semantic class per target.  REQUIREMENT with
    the longest subtype maximizes the semantic fields; grounding evidence and
    structural ancestry are invariant across model decisions.  The provider's
    mandatory preflight expands this into a structural constraint superset.
    Structural continuation components remain exactly as resolved by the tree,
    while model-only continuation is false (the longer JSON literal and the
    non-absorbed request shape).
    """

    validate_semantic_classification_against_inputs(
        semantic_scaffold,
        source_ir,
        tree,
    )
    largest_subtype = max(RequirementSubtype, key=lambda item: len(item.value))
    envelope = semantic_scaffold.model_copy(
        update={
            "annotations": tuple(
                annotation.model_copy(
                    update={
                        "semantic_class": SemanticClass.REQUIREMENT,
                        "requirement_subtype": largest_subtype,
                        "is_continuation_fragment": False,
                    }
                )
                for annotation in semantic_scaffold.annotations
            )
        }
    )
    return _assembly_request(source_ir, tree, envelope)


def plan_requirement_assembly_provider_requests(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantic_scaffold: SemanticClassificationSnapshot,
    *,
    target_windows: tuple[tuple[str, ...], ...],
    max_nodes_per_request: int,
) -> tuple[ProviderRequirementAssemblyPlanItem, ...]:
    """Build the conservative assembly windows used by document preflight."""

    request = plan_requirement_assembly_provider_request(
        source_ir,
        tree,
        semantic_scaffold,
    )
    return _plan_requirement_assembly_windows(
        tree,
        semantic_scaffold,
        request,
        target_windows=target_windows,
        max_nodes_per_request=max_nodes_per_request,
        excluded_target_ids=set(),
    )


def assemble_requirements_with_provider(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    provider: SemanticProviderPort,
    *,
    target_windows: tuple[tuple[str, ...], ...] | None = None,
    max_nodes_per_request: int = 80,
) -> ProviderRequirementAssembly:
    """Assemble source-bound windows and validate their merged graph locally."""

    if not isinstance(provider, SemanticProviderPort):
        raise TypeError("provider must implement SemanticProviderPort")
    if isinstance(max_nodes_per_request, bool) or max_nodes_per_request < 1:
        raise ValueError("max_nodes_per_request must be a positive integer")
    request = _assembly_request(source_ir, tree, semantics)
    unresolved_target_ids = {
        node.target_structure_node_id
        for node in request.nodes
        if node.semantic_class is SemanticClass.UNRESOLVED
    }
    if len(unresolved_target_ids) == len(request.nodes):
        return _skipped_requirement_assembly(
            source_ir,
            tree,
            semantics,
            provider,
            request,
        )

    plan_items = _plan_requirement_assembly_windows(
        tree,
        semantics,
        request,
        target_windows=target_windows,
        max_nodes_per_request=max_nodes_per_request,
        excluded_target_ids=unresolved_target_ids,
    )
    runs: list[SemanticProviderRunRecord] = []
    items_by_target: dict[str, RequirementAssemblyItem] = {
        node.target_structure_node_id: _unresolved_assembly(
            node,
            "upstream_semantic_unresolved",
            semantics=semantics,
        )
        for node in request.nodes
        if node.target_structure_node_id in unresolved_target_ids
    }
    for plan_item in plan_items:
        if not any(
            node.semantic_class is SemanticClass.REQUIREMENT
            for node in plan_item.request.nodes
        ):
            target_ids = set(plan_item.target_structure_node_ids)
            for node in plan_item.request.nodes:
                if node.target_structure_node_id not in target_ids:
                    continue
                if node.target_structure_node_id in items_by_target:
                    raise ValueError(
                        "assembly windows produced duplicate terminal targets"
                    )
                items_by_target[node.target_structure_node_id] = (
                    _excluded_non_requirement_assembly(
                        node,
                        semantics=semantics,
                    )
                )
            continue
        window_items, window_runs = _assemble_plan_item_adaptively(
            provider,
            tree,
            semantics,
            full_request=request,
            item=plan_item,
            max_nodes_per_request=max_nodes_per_request,
        )
        runs.extend(window_runs)
        for item in window_items:
            if item.owner_structure_node_id in items_by_target:
                raise ValueError("assembly windows produced duplicate terminal targets")
            items_by_target[item.owner_structure_node_id] = item
    if len({item.provider_run_id for item in runs}) != len(runs):
        raise ValueError("assembly provider retries produced duplicate run identities")
    if set(items_by_target) != set(request.scope_structure_node_ids):
        raise ValueError("assembly terminal results do not exactly cover request scope")
    items = tuple(
        items_by_target[target_id] for target_id in request.scope_structure_node_ids
    )
    if not runs:
        return _locally_resolved_requirement_assembly(
            source_ir,
            tree,
            semantics,
            provider,
            request,
            items=items,
        )
    result_digest = hashlib.sha256(
        json.dumps(
            {
                "planning_runtime_version": SEMANTIC_PLANNING_RUNTIME_VERSION,
                "runs": [
                    {
                        "provider_run_id": item.provider_run_id,
                        "request_sha256": item.request_sha256,
                        "response_sha256": item.response_sha256,
                        "status": item.status.value,
                        "error_code": item.error_code,
                    }
                    for item in runs
                ],
                "items": [item.model_dump(mode="json") for item in items],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assembler_version = _version_with_result(
        MODEL_REQUIREMENT_ASSEMBLER_VERSION,
        provider.model_id,
        runs[-1].prompt_version,
        result_digest,
    )
    assembly = _assembly_snapshot(
        source_ir=source_ir,
        tree=tree,
        semantics=semantics,
        provider=provider,
        assembler_version=assembler_version,
        run=runs[0],
        items=items,
    )
    try:
        validate_requirement_assembly_against_inputs(
            assembly,
            source_ir,
            tree,
            semantics,
        )
    except RequirementAssemblyRejected as exc:
        reason_code = f"provider_assembly_{exc.code.value}"
        fail_closed_items = tuple(
            _unresolved_assembly(
                node,
                reason_code,
                semantics=semantics,
            )
            for node in request.nodes
        )
        assembly = _assembly_snapshot(
            source_ir=source_ir,
            tree=tree,
            semantics=semantics,
            provider=provider,
            assembler_version=assembler_version,
            run=runs[0],
            items=fail_closed_items,
        )
        validate_requirement_assembly_against_inputs(
            assembly,
            source_ir,
            tree,
            semantics,
        )
    return ProviderRequirementAssembly(assembly=assembly, runs=tuple(runs))


def _plan_requirement_assembly_windows(
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    full_request: RequirementAssemblyRequest,
    *,
    target_windows: tuple[tuple[str, ...], ...] | None,
    max_nodes_per_request: int,
    excluded_target_ids: set[str],
) -> tuple[ProviderRequirementAssemblyPlanItem, ...]:
    request_target_ids = full_request.scope_structure_node_ids
    resolved_target_ids = tuple(
        target_id
        for target_id in request_target_ids
        if target_id not in excluded_target_ids
    )
    if not resolved_target_ids:
        return ()
    resolved_target_set = set(resolved_target_ids)
    if target_windows is None:
        mapped_windows = (resolved_target_ids,)
    else:
        mapped_windows = tuple(
            tuple(target_id for target_id in window if target_id in resolved_target_set)
            for window in target_windows
        )
        mapped_windows = tuple(window for window in mapped_windows if window)
    flattened = tuple(target_id for window in mapped_windows for target_id in window)
    if flattened != resolved_target_ids:
        raise ValueError(
            "assembly target windows must exactly partition resolved projection units"
        )

    plan_items: list[ProviderRequirementAssemblyPlanItem] = []
    for window in mapped_windows:
        plan_items.extend(
            _assembly_plan_items_for_targets(
                tree,
                semantics,
                full_request,
                target_structure_node_ids=window,
                max_nodes_per_request=max_nodes_per_request,
                excluded_target_ids=excluded_target_ids,
            )
        )
    return tuple(plan_items)


def _assembly_plan_items_for_targets(
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    full_request: RequirementAssemblyRequest,
    *,
    target_structure_node_ids: tuple[str, ...],
    max_nodes_per_request: int,
    excluded_target_ids: set[str],
) -> tuple[ProviderRequirementAssemblyPlanItem, ...]:
    request_nodes_by_id = {
        item.target_structure_node_id: item for item in full_request.nodes
    }
    target_set = set(target_structure_node_ids)
    if (
        not target_structure_node_ids
        or len(target_set) != len(target_structure_node_ids)
        or any(target_id not in request_nodes_by_id for target_id in target_set)
    ):
        raise ValueError("assembly target scope is invalid")
    context_ids = {
        context_id
        for target_id in target_structure_node_ids
        for context_id in request_nodes_by_id[target_id].ancestor_structure_node_ids
        if context_id in request_nodes_by_id
        and context_id not in target_set
        and context_id not in excluded_target_ids
        and request_nodes_by_id[context_id].semantic_class is SemanticClass.REQUIREMENT
    }
    request_nodes = tuple(
        node
        for node in full_request.nodes
        if node.target_structure_node_id in target_set
        or node.target_structure_node_id in context_ids
    )
    ordered_target_ids = tuple(
        node.target_structure_node_id
        for node in full_request.nodes
        if node.target_structure_node_id in target_set
    )
    ordered_context_ids = tuple(
        node.target_structure_node_id
        for node in full_request.nodes
        if node.target_structure_node_id in context_ids
    )
    request = RequirementAssemblyRequest(
        source_ir_id=full_request.source_ir_id,
        structure_snapshot_id=full_request.structure_snapshot_id,
        semantic_snapshot_id=full_request.semantic_snapshot_id,
        scope_structure_node_ids=tuple(
            node.target_structure_node_id for node in request_nodes
        ),
        nodes=request_nodes,
    )
    if len(request_nodes) > max_nodes_per_request:
        split = _bisect_source_bound_scope(tree, target_structure_node_ids)
        if split is None:
            return (
                ProviderRequirementAssemblyPlanItem(
                    target_structure_node_ids=ordered_target_ids,
                    context_structure_node_ids=ordered_context_ids,
                    request=request,
                    local_rejection_code=(
                        SemanticProviderRequestRejectionCode.NODE_BUDGET_EXCEEDED
                    ),
                ),
            )
        return tuple(
            item
            for child_targets in split
            for item in _assembly_plan_items_for_targets(
                tree,
                semantics,
                full_request,
                target_structure_node_ids=child_targets,
                max_nodes_per_request=max_nodes_per_request,
                excluded_target_ids=excluded_target_ids,
            )
        )
    return (
        ProviderRequirementAssemblyPlanItem(
            target_structure_node_ids=ordered_target_ids,
            context_structure_node_ids=ordered_context_ids,
            request=request,
        ),
    )


def _assemble_plan_item_adaptively(
    provider: SemanticProviderPort,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    *,
    full_request: RequirementAssemblyRequest,
    item: ProviderRequirementAssemblyPlanItem,
    max_nodes_per_request: int,
) -> tuple[
    tuple[RequirementAssemblyItem, ...],
    list[SemanticProviderRunRecord],
]:
    if item.local_rejection_code is not None:
        return _terminally_reject_assembly_item(
            provider,
            semantics,
            item,
            error_code=item.local_rejection_code.value,
        )
    try:
        if isinstance(provider, PreflightSemanticProviderPort):
            provider.preflight_assembly_request(item.request)
        result = provider.assemble_requirements(item.request)
    except SemanticProviderRequestRejected as exc:
        split = _bisect_source_bound_scope(tree, item.target_structure_node_ids)
        if split is None:
            return _terminally_reject_assembly_item(
                provider,
                semantics,
                item,
                error_code=exc.code.value,
            )
        return _assemble_child_targets(
            provider,
            tree,
            semantics,
            full_request=full_request,
            child_target_scopes=split,
            max_nodes_per_request=max_nodes_per_request,
        )
    runs = [result.run]
    if _is_output_schema_invalid(result.run):
        split = _bisect_source_bound_scope(tree, item.target_structure_node_ids)
        if split is not None:
            resolved, child_runs = _assemble_child_targets(
                provider,
                tree,
                semantics,
                full_request=full_request,
                child_target_scopes=split,
                max_nodes_per_request=max_nodes_per_request,
            )
            return resolved, [*runs, *child_runs]

    normalized = _normalize_assembly_decisions(
        result.decisions,
        request=item.request,
        run=result.run,
        semantics=semantics,
    )
    retried, child_runs = _retry_unresolved_assembly_child_scopes(
        provider,
        tree,
        semantics,
        full_request=full_request,
        item=item,
        normalized=normalized,
        max_nodes_per_request=max_nodes_per_request,
    )
    return retried, [*runs, *child_runs]


def _retry_unresolved_assembly_child_scopes(
    provider: SemanticProviderPort,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    *,
    full_request: RequirementAssemblyRequest,
    item: ProviderRequirementAssemblyPlanItem,
    normalized: tuple[RequirementAssemblyItem, ...],
    max_nodes_per_request: int,
) -> tuple[
    tuple[RequirementAssemblyItem, ...],
    list[SemanticProviderRunRecord],
]:
    """Retry only unresolved projection targets in strictly smaller scopes."""

    target_ids = set(item.target_structure_node_ids)
    current_results = {
        result.owner_structure_node_id: result
        for result in normalized
        if result.owner_structure_node_id in target_ids
    }
    retryable_target_ids = {
        result.owner_structure_node_id
        for result in current_results.values()
        if result.status is RequirementAssemblyStatus.UNRESOLVED
        and result.reason_codes
        and set(result.reason_codes).issubset(
            _RETRYABLE_ASSEMBLY_UNRESOLVED_REASON_CODES
        )
    }
    if not retryable_target_ids:
        return (
            tuple(
                current_results[target_id]
                for target_id in item.target_structure_node_ids
            ),
            [],
        )
    split = _bisect_source_bound_scope(tree, item.target_structure_node_ids)
    if split is None:
        return (
            tuple(
                current_results[target_id]
                for target_id in item.target_structure_node_ids
            ),
            [],
        )

    excluded_target_ids = {
        node.target_structure_node_id
        for node in full_request.nodes
        if node.semantic_class is SemanticClass.UNRESOLVED
    }
    child_runs: list[SemanticProviderRunRecord] = []
    for child_targets in split:
        child_retryable_target_ids = retryable_target_ids.intersection(child_targets)
        if not child_retryable_target_ids:
            continue
        child_plan_items = _assembly_plan_items_for_targets(
            tree,
            semantics,
            full_request,
            target_structure_node_ids=child_targets,
            max_nodes_per_request=max_nodes_per_request,
            excluded_target_ids=excluded_target_ids,
        )
        child_results: dict[str, RequirementAssemblyItem] = {}
        for child_item in child_plan_items:
            resolved, executed_runs = _assemble_plan_item_adaptively(
                provider,
                tree,
                semantics,
                full_request=full_request,
                item=child_item,
                max_nodes_per_request=max_nodes_per_request,
            )
            child_results.update(
                (result.owner_structure_node_id, result) for result in resolved
            )
            child_runs.extend(executed_runs)
        for target_id in child_retryable_target_ids:
            current_results[target_id] = child_results[target_id]

    return (
        tuple(
            current_results[target_id] for target_id in item.target_structure_node_ids
        ),
        child_runs,
    )


def _assemble_child_targets(
    provider: SemanticProviderPort,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    *,
    full_request: RequirementAssemblyRequest,
    child_target_scopes: tuple[tuple[str, ...], tuple[str, ...]],
    max_nodes_per_request: int,
) -> tuple[
    tuple[RequirementAssemblyItem, ...],
    list[SemanticProviderRunRecord],
]:
    resolved: list[RequirementAssemblyItem] = []
    runs: list[SemanticProviderRunRecord] = []
    excluded_target_ids = {
        node.target_structure_node_id
        for node in full_request.nodes
        if node.semantic_class is SemanticClass.UNRESOLVED
    }
    for child_targets in child_target_scopes:
        child_plan_items = _assembly_plan_items_for_targets(
            tree,
            semantics,
            full_request,
            target_structure_node_ids=child_targets,
            max_nodes_per_request=max_nodes_per_request,
            excluded_target_ids=excluded_target_ids,
        )
        for child_item in child_plan_items:
            child_results, child_runs = _assemble_plan_item_adaptively(
                provider,
                tree,
                semantics,
                full_request=full_request,
                item=child_item,
                max_nodes_per_request=max_nodes_per_request,
            )
            resolved.extend(child_results)
            runs.extend(child_runs)
    return tuple(resolved), runs


def _terminally_reject_assembly_item(
    provider: SemanticProviderPort,
    semantics: SemanticClassificationSnapshot,
    item: ProviderRequirementAssemblyPlanItem,
    *,
    error_code: str,
) -> tuple[
    tuple[RequirementAssemblyItem, ...],
    list[SemanticProviderRunRecord],
]:
    run, _prompt_version, _request_sha256 = _skipped_assembly_provider_run(
        provider,
        item.request,
        error_code=error_code,
    )
    target_ids = set(item.target_structure_node_ids)
    items = tuple(
        _unresolved_assembly(
            node,
            error_code,
            semantics=semantics,
        )
        for node in item.request.nodes
        if node.target_structure_node_id in target_ids
    )
    return items, [run]


def _skipped_requirement_assembly(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    provider: SemanticProviderPort,
    request: RequirementAssemblyRequest,
) -> ProviderRequirementAssembly:
    """Represent an upstream-gated assembly without claiming a remote request."""

    run, prompt_version, request_sha256 = _skipped_assembly_provider_run(
        provider,
        request,
        error_code="upstream_semantic_unresolved",
    )
    assembler_version = _version_with_result(
        MODEL_REQUIREMENT_ASSEMBLER_VERSION,
        provider.model_id,
        prompt_version,
        request_sha256,
    )
    items = tuple(
        _unresolved_assembly(
            node,
            "upstream_semantic_unresolved",
            semantics=semantics,
        )
        for node in request.nodes
    )
    assembly = _assembly_snapshot(
        source_ir=source_ir,
        tree=tree,
        semantics=semantics,
        provider=provider,
        assembler_version=assembler_version,
        run=run,
        items=items,
    )
    validate_requirement_assembly_against_inputs(
        assembly,
        source_ir,
        tree,
        semantics,
    )
    return ProviderRequirementAssembly(assembly=assembly, runs=(run,))


def _locally_resolved_requirement_assembly(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    provider: SemanticProviderPort,
    request: RequirementAssemblyRequest,
    *,
    items: tuple[RequirementAssemblyItem, ...],
) -> ProviderRequirementAssembly:
    """Close a non-requirement scope without inventing a remote model run."""

    run, prompt_version, request_sha256 = _skipped_assembly_provider_run(
        provider,
        request,
        error_code="no_requirement_candidates",
    )
    assembler_version = _version_with_result(
        MODEL_REQUIREMENT_ASSEMBLER_VERSION,
        provider.model_id,
        prompt_version,
        request_sha256,
    )
    assembly = _assembly_snapshot(
        source_ir=source_ir,
        tree=tree,
        semantics=semantics,
        provider=provider,
        assembler_version=assembler_version,
        run=run,
        items=items,
    )
    validate_requirement_assembly_against_inputs(
        assembly,
        source_ir,
        tree,
        semantics,
    )
    return ProviderRequirementAssembly(assembly=assembly, runs=(run,))


def _skipped_assembly_provider_run(
    provider: SemanticProviderPort,
    request: RequirementAssemblyRequest,
    *,
    error_code: str,
) -> tuple[SemanticProviderRunRecord, str, str]:
    request_payload = request.model_dump(mode="json")
    request_sha256 = hashlib.sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    now = datetime.now(timezone.utc)
    prompt_version = str(
        getattr(provider, "assembly_prompt_version", "assembly-not-invoked/v1")
    )
    response_schema_version = str(
        getattr(
            provider,
            "assembly_response_schema_version",
            "assembly-not-invoked/v1",
        )
    )
    run_id = stable_semantic_provider_run_id(
        operation="assemble_requirements",
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        request_sha256=request_sha256,
        response_sha256=None,
        status=SemanticProviderRunStatus.SKIPPED,
        started_at=now,
    )
    run = SemanticProviderRunRecord(
        provider_run_id=run_id,
        operation="assemble_requirements",
        provider_id=provider.provider_id,
        provider_version=provider.provider_version,
        model_id=provider.model_id,
        prompt_version=prompt_version,
        response_schema_version=response_schema_version,
        request_sha256=request_sha256,
        response_sha256=None,
        status=SemanticProviderRunStatus.SKIPPED,
        started_at=now,
        finished_at=now,
        latency_ms=0,
        request_count=0,
        retry_count=0,
        error_code=error_code,
    )
    return run, prompt_version, request_sha256


def _skipped_classification_provider_run(
    provider: SemanticProviderPort,
    request: SemanticClassificationRequest,
    *,
    error_code: str,
) -> SemanticProviderRunRecord:
    request_sha256 = hashlib.sha256(
        json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    now = datetime.now(timezone.utc)
    prompt_version = str(
        getattr(
            provider,
            "classification_prompt_version",
            "classification-not-invoked/v1",
        )
    )
    response_schema_version = str(
        getattr(
            provider,
            "classification_response_schema_version",
            "classification-not-invoked/v1",
        )
    )
    run_id = stable_semantic_provider_run_id(
        operation="classify_nodes",
        provider_id=provider.provider_id,
        model_id=provider.model_id,
        request_sha256=request_sha256,
        response_sha256=None,
        status=SemanticProviderRunStatus.SKIPPED,
        started_at=now,
    )
    return SemanticProviderRunRecord(
        provider_run_id=run_id,
        operation="classify_nodes",
        provider_id=provider.provider_id,
        provider_version=provider.provider_version,
        model_id=provider.model_id,
        prompt_version=prompt_version,
        response_schema_version=response_schema_version,
        request_sha256=request_sha256,
        response_sha256=None,
        status=SemanticProviderRunStatus.SKIPPED,
        started_at=now,
        finished_at=now,
        latency_ms=0,
        request_count=0,
        retry_count=0,
        error_code=error_code,
    )


def _semantic_windows(
    tree: StructureTreeSnapshot,
    scope: tuple[str, ...],
    *,
    max_nodes_per_request: int,
    pack_adjacent_provisions: bool = False,
) -> tuple[tuple[str, ...], ...]:
    """Split only on source-bound semantic-unit boundaries.

    Structural ``CONTINUES`` intervals stay together while they fit the model
    budget.  A continuation interval that is itself larger than the budget is
    windowed in source order at complete Structure-owned semantic units.  The
    planner never slices source text inside one owner, and every later window
    still receives its structural ancestors as context.
    """

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}

    component_for, _ = _continuation_components(tree)

    def provision_owner(target_id: str) -> tuple[str, str]:
        current_id: str | None = target_id
        while current_id is not None:
            current = nodes_by_id[current_id]
            if current.kind is StructureNodeKind.SECTION:
                return ("section", component_for[current_id])
            parent_id = current.parent_structure_node_id
            if (
                parent_id is None
                or nodes_by_id[parent_id].kind is StructureNodeKind.DOCUMENT
            ):
                return ("root_branch", component_for[current_id])
            current_id = parent_id
        raise ValueError("semantic target has no provision owner")

    provision_members: dict[tuple[str, str], list[str]] = {}
    owners_by_component: dict[str, set[tuple[str, str]]] = {}
    for target_id in scope:
        owner = provision_owner(target_id)
        provision_members.setdefault(owner, []).append(target_id)
        owners_by_component.setdefault(component_for[target_id], set()).add(owner)

    owner_order = tuple(provision_members)
    owner_parent = {owner: owner for owner in owner_order}

    def find_owner(owner: tuple[str, str]) -> tuple[str, str]:
        while owner_parent[owner] != owner:
            owner_parent[owner] = owner_parent[owner_parent[owner]]
            owner = owner_parent[owner]
        return owner

    def union_owners(left: tuple[str, str], right: tuple[str, str]) -> None:
        left_root = find_owner(left)
        right_root = find_owner(right)
        if left_root == right_root:
            return
        left_index = owner_order.index(left_root)
        right_index = owner_order.index(right_root)
        if left_index <= right_index:
            owner_parent[right_root] = left_root
        else:
            owner_parent[left_root] = right_root

    for owners in owners_by_component.values():
        ordered = tuple(owner for owner in owner_order if owner in owners)
        for left, right in zip(ordered, ordered[1:], strict=False):
            union_owners(left, right)

    provision_roots: list[tuple[str, str]] = []
    for owner in owner_order:
        root = find_owner(owner)
        if root not in provision_roots:
            provision_roots.append(root)
    provisions = tuple(
        tuple(
            target_id
            for target_id in scope
            if find_owner(provision_owner(target_id)) == root
        )
        for root in provision_roots
    )

    provision_windows: list[tuple[str, ...]] = []
    for provision in provisions:
        atomic_groups = _source_bound_atomic_groups(tree, provision)
        current: list[str] = []
        for group in atomic_groups:
            if len(group) > max_nodes_per_request:
                if current:
                    provision_windows.append(tuple(current))
                    current = []
                provision_windows.extend(
                    tuple(group[offset : offset + max_nodes_per_request])
                    for offset in range(0, len(group), max_nodes_per_request)
                )
                continue
            if current and len(current) + len(group) > max_nodes_per_request:
                provision_windows.append(tuple(current))
                current = []
            current.extend(group)
        if current:
            provision_windows.append(tuple(current))
    if not pack_adjacent_provisions:
        return tuple(provision_windows)

    packed: list[tuple[str, ...]] = []
    current: list[str] = []
    for window in provision_windows:
        if current and len(current) + len(window) > max_nodes_per_request:
            packed.append(tuple(current))
            current = []
        current.extend(window)
    if current:
        packed.append(tuple(current))
    return tuple(packed)


def _semantic_indivisible_group_by_target(
    tree: StructureTreeSnapshot,
    scope: tuple[str, ...],
) -> dict[str, str]:
    return {
        target_id: group[0]
        for group in _source_bound_atomic_groups(tree, scope)
        for target_id in group
    }


def _source_bound_atomic_groups(
    tree: StructureTreeSnapshot,
    scope: tuple[str, ...],
) -> tuple[tuple[str, ...], ...]:
    if not scope:
        return ()
    component_for, _ = _continuation_components(tree)
    positions_by_component: dict[str, list[int]] = {}
    for position, target_id in enumerate(scope):
        positions_by_component.setdefault(component_for[target_id], []).append(position)

    # A CONTINUES component may connect two parent nodes while their owned
    # children appear between them in source order (for example, two cross-page
    # TABLE nodes with the first table's ROW nodes in between).  Its complete
    # source interval is therefore the indivisible unit.  Merging overlapping
    # intervals keeps every target covered exactly once and gives recursive
    # preflight bisection a strictly source-bound cut.
    intervals = sorted(
        (positions[0], positions[-1]) for positions in positions_by_component.values()
    )
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(scope[start : end + 1] for start, end in merged)


def _bisect_source_bound_scope(
    tree: StructureTreeSnapshot,
    scope: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    groups = _source_bound_atomic_groups(tree, scope)
    if len(groups) < 2:
        return None
    target_midpoint = len(scope) / 2
    running = 0
    best_cut = 1
    best_distance = float("inf")
    for index, group in enumerate(groups[:-1], start=1):
        running += len(group)
        distance = abs(running - target_midpoint)
        if distance < best_distance:
            best_cut = index
            best_distance = distance
    return (
        tuple(item for group in groups[:best_cut] for item in group),
        tuple(item for group in groups[best_cut:] for item in group),
    )


def _images_for_scaffold(
    images: tuple[SemanticImageInput, ...],
    scaffold: SemanticClassificationSnapshot,
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
) -> tuple[SemanticImageInput, ...]:
    if not images:
        return ()
    pages = set().union(
        *_semantic_image_pages_by_target(scaffold, source_ir, tree, images).values()
    )
    return tuple(item for item in images if item.page_number in pages)


def _semantic_image_pages_by_target(
    scaffold: SemanticClassificationSnapshot,
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    images: tuple[SemanticImageInput, ...],
) -> dict[str, frozenset[int]]:
    """Map each semantic target to the supplied page images that ground it."""

    image_pages = {item.page_number for item in images}
    if not image_pages:
        return {}
    source_node_pages = {
        item.source_node_id: item.native_locator.page_number
        for item in source_ir.nodes
        if item.native_locator.page_number is not None
    }
    cell_pages = {
        cell.cell_id: cell.native_locator.page_number
        for node in source_ir.nodes
        if node.table_payload is not None
        for cell in node.table_payload.cells
    }
    visual_pages = {
        observation.observation_id: observation.page_number
        for snapshot in source_ir.visual_text_snapshots
        for observation in snapshot.observations
    }
    target_refs = {
        node.structure_node_id: set(node.effective_semantic_source_refs)
        for node in tree.nodes
    }
    result: dict[str, frozenset[int]] = {}
    for annotation in scaffold.annotations:
        pages: set[int] = set()
        for span in annotation.grounding_spans:
            if span.source_ref not in target_refs[annotation.target_structure_node_id]:
                continue
            if span.source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
                page = source_node_pages.get(span.source_ref.source_id)
            elif span.source_ref.kind is StructureSourceRefKind.TABLE_CELL:
                page = cell_pages.get(span.source_ref.source_id)
            else:
                page = visual_pages.get(span.source_ref.source_id)
            if page in image_pages:
                pages.add(page)
        result[annotation.target_structure_node_id] = frozenset(pages)
    return result


def _split_semantic_windows_by_image_budget(
    windows: tuple[tuple[str, ...], ...],
    *,
    image_pages_by_target: dict[str, frozenset[int]],
    max_images_per_request: int,
    indivisible_group_by_target: dict[str, str] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Split at source-bound units while keeping continuation groups atomic."""

    if isinstance(max_images_per_request, bool) or max_images_per_request < 1:
        raise ValueError("max_images_per_request must be a positive integer")
    if not image_pages_by_target:
        return windows
    window_targets = {target_id for window in windows for target_id in window}
    if set(image_pages_by_target) != window_targets:
        raise ValueError("semantic image-page map must exactly cover the scope")
    group_by_target = indivisible_group_by_target or {
        target_id: target_id for target_id in window_targets
    }
    if set(group_by_target) != window_targets:
        raise ValueError("semantic indivisible-group map must exactly cover the scope")
    result: list[tuple[str, ...]] = []
    for window in windows:
        groups: list[list[str]] = []
        group_indexes: dict[str, int] = {}
        for target_id in window:
            group_id = group_by_target[target_id]
            index = group_indexes.get(group_id)
            if index is None:
                index = len(groups)
                group_indexes[group_id] = index
                groups.append([])
            groups[index].append(target_id)
        if tuple(item for group in groups for item in group) != window:
            raise ValueError("semantic image groups must remain source contiguous")
        current: list[str] = []
        current_pages: set[int] = set()
        for group in groups:
            group_pages = set().union(
                *(set(image_pages_by_target.get(target_id, ())) for target_id in group)
            )
            if len(group_pages) > max_images_per_request:
                raise SemanticProviderRequestRejected(
                    SemanticProviderRequestRejectionCode.IMAGE_COUNT_BUDGET_EXCEEDED,
                    "one source-bound semantic unit group exceeds the page-image budget",
                )
            if current and len(current_pages | group_pages) > max_images_per_request:
                result.append(tuple(current))
                current = []
                current_pages = set()
            current.extend(group)
            current_pages.update(group_pages)
        if current:
            result.append(tuple(current))
    return tuple(result)


def _resolve_cross_window_continuation_conflicts(
    values: list[tuple[SemanticDecision, SemanticProviderRunRecord]],
    *,
    tree: StructureTreeSnapshot,
) -> list[tuple[SemanticDecision, SemanticProviderRunRecord]]:
    """Reconcile one validated continuation object after all window retries.

    This is intentionally the only point that resolves semantic disagreement
    across structural CONTINUES members.  It therefore handles members from
    both the same provider window and different adaptive windows consistently.
    """

    component_for, _ = _continuation_components(tree)
    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    values_by_component: dict[
        str,
        list[tuple[SemanticDecision, SemanticProviderRunRecord]],
    ] = {}
    for item in values:
        values_by_component.setdefault(
            component_for[item[0].target_structure_node_id],
            [],
        ).append(item)

    normalized: dict[
        str,
        tuple[SemanticDecision, SemanticProviderRunRecord],
    ] = {}
    for component_values in values_by_component.values():
        ordered = sorted(
            component_values,
            key=lambda item: nodes_by_id[item[0].target_structure_node_id].source_order,
        )
        classifications = {
            (decision.semantic_class, decision.requirement_subtype)
            for decision, _run in ordered
        }
        if len(classifications) <= 1:
            normalized.update(
                (decision.target_structure_node_id, (decision, run))
                for decision, run in ordered
            )
            continue

        # A validated structural CONTINUES component is one logical source
        # object.  When all of its fragments produced resolved but conflicting
        # labels in different windows, inherit the earliest source fragment's
        # semantic identity instead of turning the complete object into an
        # artificial unresolved gap.  If any fragment is already unresolved,
        # keep the component fail-closed because there is no fully grounded
        # canonical identity to inherit.
        if any(
            decision.semantic_class is SemanticClass.UNRESOLVED
            for decision, _run in ordered
        ):
            normalized.update(
                (
                    decision.target_structure_node_id,
                    (
                        _unresolved_semantic(
                            decision.target_structure_node_id,
                            "continuation_model_conflict",
                        ),
                        run,
                    ),
                )
                for decision, run in ordered
            )
            continue

        canonical = ordered[0][0]
        for decision, run in ordered:
            if (
                decision.semantic_class is canonical.semantic_class
                and decision.requirement_subtype is canonical.requirement_subtype
            ):
                inherited = decision
            else:
                inherited = decision.model_copy(
                    update={
                        "semantic_class": canonical.semantic_class,
                        "requirement_subtype": canonical.requirement_subtype,
                        "reason_codes": tuple(
                            sorted(
                                {
                                    *decision.reason_codes,
                                    "structural_continuation_semantics_inherited",
                                }
                            )
                        ),
                    }
                )
            normalized[decision.target_structure_node_id] = (inherited, run)

    return [normalized[decision.target_structure_node_id] for decision, _run in values]


def _normalize_model_continuation_parent_semantics(
    window_results: list[tuple[SemanticDecision, SemanticProviderRunRecord]],
    *,
    tree: StructureTreeSnapshot,
) -> list[tuple[SemanticDecision, SemanticProviderRunRecord]]:
    """Enforce one semantic identity for a model-declared continuation.

    A model-only continuation is absorbed into its structural owner and cannot
    carry a different semantic class or requirement subtype. The model's
    explicit continuation decision authorizes this generic normalization.  A
    missing or non-requirement parent means the model's continuation flag is
    not structurally applicable; the flag is discarded while the independently
    grounded semantic decision remains reviewable.
    """

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    decisions_by_target = {
        item.target_structure_node_id: item for item, _run in window_results
    }
    normalized: list[tuple[SemanticDecision, SemanticProviderRunRecord]] = []
    for decision, run in window_results:
        if not decision.is_continuation_fragment:
            normalized.append((decision, run))
            continue
        parent_id = nodes_by_id[
            decision.target_structure_node_id
        ].parent_structure_node_id
        parent = decisions_by_target.get(parent_id or "")
        if parent is None or parent.semantic_class is not SemanticClass.REQUIREMENT:
            decision = decision.model_copy(
                update={
                    "is_continuation_fragment": False,
                    "reason_codes": tuple(
                        sorted(
                            {
                                *decision.reason_codes,
                                "model_continuation_flag_not_applicable",
                            }
                        )
                    ),
                }
            )
        elif (
            decision.semantic_class is not parent.semantic_class
            or decision.requirement_subtype is not parent.requirement_subtype
        ):
            decision = decision.model_copy(
                update={
                    "semantic_class": parent.semantic_class,
                    "requirement_subtype": parent.requirement_subtype,
                    "reason_codes": tuple(
                        sorted(
                            {
                                *decision.reason_codes,
                                "continuation_parent_semantics_normalized",
                            }
                        )
                    ),
                }
            )
        normalized.append((decision, run))
    return normalized


def _classification_request(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    scaffold: SemanticClassificationSnapshot,
    *,
    context_scaffold: SemanticClassificationSnapshot | None = None,
    images: tuple[SemanticImageInput, ...],
    include_adjacent_siblings: bool = False,
) -> SemanticClassificationRequest:
    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    image_pages_by_target = _semantic_image_pages_by_target(
        scaffold,
        source_ir,
        tree,
        images,
    )
    base_nodes = tuple(
        _semantic_node_input(
            annotation,
            source_ir=source_ir,
            nodes_by_id=nodes_by_id,
            image_page_numbers=tuple(
                sorted(
                    image_pages_by_target.get(annotation.target_structure_node_id, ())
                )
            ),
        )
        for annotation in scaffold.annotations
    )
    full_scaffold = context_scaffold or scaffold
    full_nodes = tuple(
        _semantic_node_input(
            annotation,
            source_ir=source_ir,
            nodes_by_id=nodes_by_id,
            image_page_numbers=(),
        )
        for annotation in full_scaffold.annotations
    )
    request_nodes: list[SemanticNodeInput] = []
    for node in base_nodes:
        if node.kind is not StructureNodeKind.SECTION:
            request_nodes.append(node)
            continue
        scoped_provision_nodes = tuple(
            candidate
            for candidate in base_nodes
            if _belongs_to_section_provision(
                node.target_structure_node_id,
                candidate.target_structure_node_id,
                nodes_by_id=nodes_by_id,
            )
        )
        full_provision_nodes = tuple(
            candidate
            for candidate in full_nodes
            if _belongs_to_section_provision(
                node.target_structure_node_id,
                candidate.target_structure_node_id,
                nodes_by_id=nodes_by_id,
            )
        )
        request_nodes.append(
            node.model_copy(
                update={
                    "provision_structure_node_ids": tuple(
                        item.target_structure_node_id for item in scoped_provision_nodes
                    ),
                    "evidence": _dedupe_evidence(
                        (
                            *node.evidence,
                            *(
                                evidence
                                for provision in full_provision_nodes
                                for evidence in provision.evidence
                            ),
                        )
                    ),
                }
            )
        )
    nodes = tuple(request_nodes)
    context_nodes = tuple(
        _semantic_node_input(
            annotation,
            source_ir=source_ir,
            nodes_by_id=nodes_by_id,
            image_page_numbers=(),
        )
        for annotation in _classification_context_annotations(
            scaffold,
            context_scaffold or scaffold,
            nodes_by_id=nodes_by_id,
            include_adjacent_siblings=include_adjacent_siblings,
        )
    )
    return SemanticClassificationRequest(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        structure_snapshot_id=structure_snapshot_id(tree),
        scope_structure_node_ids=scaffold.scope_structure_node_ids,
        nodes=nodes,
        context_nodes=context_nodes,
        images=images,
    )


def _belongs_to_section_provision(
    section_id: str,
    candidate_id: str,
    *,
    nodes_by_id: dict[str, StructureNode],
) -> bool:
    """Keep a provision inside its owning section, stopping at nested sections."""

    candidate = nodes_by_id[candidate_id]
    if candidate.kind is StructureNodeKind.SECTION:
        return False
    ancestor_structure_node_ids = _ancestor_ids(candidate_id, nodes_by_id)
    try:
        owner_index = ancestor_structure_node_ids.index(section_id)
    except ValueError:
        return False
    return not any(
        nodes_by_id[ancestor_id].kind is StructureNodeKind.SECTION
        for ancestor_id in ancestor_structure_node_ids[owner_index + 1 :]
    )


def _validate_complete_section_provisions(
    tree: StructureTreeSnapshot,
    scope: tuple[str, ...],
) -> None:
    """Reject a selected Section whose owned provision is only partially scoped."""

    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    scope_set = set(scope)
    for section_id in scope:
        if nodes_by_id[section_id].kind is not StructureNodeKind.SECTION:
            continue
        owned_ids = {
            candidate_id
            for candidate_id in nodes_by_id
            if _belongs_to_section_provision(
                section_id,
                candidate_id,
                nodes_by_id=nodes_by_id,
            )
        }
        if not owned_ids.issubset(scope_set):
            raise ValueError("semantic section scope omits provision descendants")


def _classification_context_annotations(
    target_scaffold: SemanticClassificationSnapshot,
    full_scaffold: SemanticClassificationSnapshot,
    *,
    nodes_by_id: dict[str, StructureNode],
    include_adjacent_siblings: bool = False,
) -> tuple[SemanticAnnotation, ...]:
    """Carry read-only structural context without requesting duplicate decisions.

    Initial windows use ancestors only.  Adaptive retry windows additionally
    retain one immediate sibling on either side under the same structural
    parent.  This gives an isolated content node or table row the local label,
    row, and continuation context it may need without re-introducing the large
    request that caused the retry.
    """

    target_ids = set(target_scaffold.scope_structure_node_ids)
    annotations_by_id = {
        item.target_structure_node_id: item for item in full_scaffold.annotations
    }
    sibling_ids_by_parent: dict[str, tuple[str, ...]] = {}
    if include_adjacent_siblings:
        mutable_siblings: dict[str, list[str]] = {}
        for annotation in sorted(
            full_scaffold.annotations,
            key=lambda item: nodes_by_id[item.target_structure_node_id].source_order,
        ):
            node = nodes_by_id[annotation.target_structure_node_id]
            if node.parent_structure_node_id is not None:
                mutable_siblings.setdefault(
                    node.parent_structure_node_id,
                    [],
                ).append(node.structure_node_id)
        sibling_ids_by_parent = {
            parent_id: tuple(sibling_ids)
            for parent_id, sibling_ids in mutable_siblings.items()
        }
    context_ids: set[str] = set()
    for target_id in target_ids:
        context_ids.update(_ancestor_ids(target_id, nodes_by_id))
        if not include_adjacent_siblings:
            continue
        parent_id = nodes_by_id[target_id].parent_structure_node_id
        if parent_id is None:
            continue
        sibling_ids = sibling_ids_by_parent.get(parent_id, ())
        target_index = sibling_ids.index(target_id)
        if target_index > 0:
            context_ids.add(sibling_ids[target_index - 1])
        if target_index + 1 < len(sibling_ids):
            context_ids.add(sibling_ids[target_index + 1])

    context_ids.difference_update(target_ids)
    return tuple(
        annotations_by_id[target_id]
        for target_id in sorted(
            context_ids.intersection(annotations_by_id),
            key=lambda item: nodes_by_id[item].source_order,
        )
    )


def _semantic_node_input(
    annotation: SemanticAnnotation,
    *,
    source_ir: SourceIRSnapshot,
    nodes_by_id: dict[str, StructureNode],
    image_page_numbers: tuple[int, ...],
) -> SemanticNodeInput:
    target = nodes_by_id[annotation.target_structure_node_id]
    semantic_source_refs = target.effective_semantic_source_refs
    target_source_refs = set(semantic_source_refs)
    evidence = tuple(
        item
        for item in _annotation_evidence(annotation, source_ir)
        if item.source_ref in target_source_refs
    )
    return SemanticNodeInput(
        target_structure_node_id=annotation.target_structure_node_id,
        source_order=target.source_order,
        kind=target.kind,
        parent_structure_node_id=target.parent_structure_node_id,
        ancestor_structure_node_ids=_ancestor_ids(
            annotation.target_structure_node_id,
            nodes_by_id,
        ),
        explicit_label=target.explicit_label,
        list_ordinal=target.list_ordinal,
        image_page_numbers=image_page_numbers,
        target_source_refs=semantic_source_refs,
        evidence=evidence,
    )


def _assembly_request(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
) -> RequirementAssemblyRequest:
    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    component_for, component_members = _continuation_components(tree)
    units = _projection_units(
        semantics.annotations,
        nodes_by_id=nodes_by_id,
        component_for=component_for,
        component_members=component_members,
    )
    request_nodes: list[RequirementAssemblyNodeInput] = []
    for unit in units:
        target = nodes_by_id[unit.target_structure_node_id]
        parent_id = target.parent_structure_node_id
        canonical_parent = component_for[parent_id] if parent_id is not None else None
        unit_source_refs = set(unit.source_refs)
        evidence = _dedupe_evidence(
            item
            for annotation in unit.annotations
            for item in _annotation_evidence(annotation, source_ir)
            if item.source_ref in unit_source_refs
        )
        request_nodes.append(
            RequirementAssemblyNodeInput(
                target_structure_node_id=unit.target_structure_node_id,
                source_order=unit.source_order,
                kind=target.kind,
                parent_structure_node_id=canonical_parent,
                ancestor_structure_node_ids=tuple(
                    dict.fromkeys(
                        component_for[item]
                        for item in _ancestor_ids(
                            unit.target_structure_node_id,
                            nodes_by_id,
                        )
                        if component_for[item] != unit.target_structure_node_id
                    )
                ),
                explicit_label=target.explicit_label,
                list_ordinal=target.list_ordinal,
                semantic_annotation_ids=tuple(
                    item.semantic_annotation_id for item in unit.annotations
                ),
                semantic_class=unit.semantic_class,
                requirement_subtype=unit.requirement_subtype,
                is_continuation_fragment=all(
                    item.is_continuation_fragment for item in unit.annotations
                ),
                evidence=evidence,
            )
        )
    return RequirementAssemblyRequest(
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=structure_snapshot_id(tree),
        semantic_snapshot_id=semantics.semantic_snapshot_id,
        scope_structure_node_ids=tuple(
            item.target_structure_node_id for item in request_nodes
        ),
        nodes=tuple(request_nodes),
    )


def _normalize_semantic_decisions(
    decisions: tuple[SemanticDecision, ...],
    *,
    scaffold: SemanticClassificationSnapshot,
    request: SemanticClassificationRequest,
    tree: StructureTreeSnapshot,
    run: SemanticProviderRunRecord,
) -> tuple[SemanticDecision, ...]:
    expected_targets = scaffold.scope_structure_node_ids
    if run.status not in {
        SemanticProviderRunStatus.SUCCESS,
        SemanticProviderRunStatus.PARTIAL,
    }:
        return tuple(
            _unresolved_semantic(target, run.error_code or "provider_unavailable")
            for target in expected_targets
        )

    allowed_refs = {
        node.target_structure_node_id: {item.source_ref for item in node.evidence}
        for node in request.nodes
    }
    request_nodes_by_target = {
        node.target_structure_node_id: node for node in request.nodes
    }
    decisions_by_target: dict[str, SemanticDecision] = {}
    duplicated_targets: set[str] = set()
    expected_target_set = set(expected_targets)
    for decision in decisions:
        target_id = decision.target_structure_node_id
        if target_id not in expected_target_set:
            continue
        if target_id in decisions_by_target:
            duplicated_targets.add(target_id)
            continue
        decisions_by_target[target_id] = decision

    normalized: list[SemanticDecision] = []
    for target_id in expected_targets:
        decision = decisions_by_target.get(target_id)
        if decision is None:
            normalized.append(
                _unresolved_semantic(
                    target_id,
                    run.error_code or "provider_scope_missing",
                )
            )
            continue
        if target_id in duplicated_targets:
            normalized.append(
                _unresolved_semantic(target_id, "provider_scope_duplicated")
            )
            continue
        target_node = next(
            node
            for node in tree.nodes
            if node.structure_node_id == decision.target_structure_node_id
        )
        request_node = request_nodes_by_target[target_id]
        if (
            decision.semantic_class is SemanticClass.UNRESOLVED
            and target_node.kind is StructureNodeKind.TABLE_ROW
            and request_node.evidence
            and all(not item.text.strip() for item in request_node.evidence)
        ):
            # Empty authored table rows are source placeholders, not missing
            # model output and not latent technical obligations.  Keep the row
            # in the Structure tree, but close its semantic receipt as NOISE so
            # it does not block the document or enter downstream requirement
            # analysis.
            decision = SemanticDecision(
                target_structure_node_id=target_id,
                semantic_class=SemanticClass.NOISE,
                evidence_source_refs=tuple(
                    item.source_ref for item in request_node.evidence
                ),
                reason_codes=("empty_table_row_placeholder",),
            )
        elif (
            decision.semantic_class is SemanticClass.UNRESOLVED
            and target_node.kind is StructureNodeKind.TABLE_ROW
            and request_node.evidence
            and _SPREADSHEET_ERROR_PLACEHOLDER_PATTERN.fullmatch(
                "".join(
                    character
                    for item in request_node.evidence
                    for character in item.text
                    if not character.isspace()
                )
            )
            is not None
        ):
            decision = SemanticDecision(
                target_structure_node_id=target_id,
                semantic_class=SemanticClass.PROJECT_RISK,
                evidence_source_refs=tuple(
                    item.source_ref for item in request_node.evidence
                ),
                reason_codes=("broken_reference_placeholder",),
            )
        if decision.is_continuation_fragment and (
            target_node.kind
            not in {StructureNodeKind.CONTENT, StructureNodeKind.CONTINUATION}
            or target_node.parent_structure_node_id is None
            or target_node.explicit_label is not None
            or target_node.list_ordinal is not None
        ):
            # A provider-only continuation flag is advisory metadata.  When
            # Structure proves that the authored node is independently
            # labelled, ordinal, root-level, or otherwise not a continuation,
            # preserve the source-grounded semantic classification and discard
            # only the inapplicable flag.  Turning the whole unit UNRESOLVED
            # would let one bad boolean erase an otherwise valid terminal
            # decision and unnecessarily block the document.
            decision = decision.model_copy(
                update={
                    "is_continuation_fragment": False,
                    "reason_codes": tuple(
                        sorted(
                            {
                                *decision.reason_codes,
                                "model_continuation_flag_not_applicable",
                            }
                        )
                    ),
                }
            )
        if (
            decision.semantic_class is not SemanticClass.UNRESOLVED
            and allowed_refs[decision.target_structure_node_id]
            and not decision.evidence_source_refs
        ):
            normalized.append(
                _unresolved_semantic(
                    decision.target_structure_node_id,
                    "provider_evidence_missing",
                )
            )
            continue
        if not set(decision.evidence_source_refs).issubset(
            allowed_refs[decision.target_structure_node_id]
        ):
            normalized.append(
                _unresolved_semantic(
                    decision.target_structure_node_id,
                    "provider_evidence_invalid",
                )
            )
            continue
        reason_codes = decision.reason_codes
        if not reason_codes:
            reason_codes = ("model_classification",)
        normalized.append(decision.model_copy(update={"reason_codes": reason_codes}))

    return tuple(normalized)


def _normalize_provision_subtypes(
    decisions: tuple[SemanticDecision, ...],
    *,
    tree: StructureTreeSnapshot,
) -> tuple[SemanticDecision, ...]:
    """Stabilize a specific Section subtype across its numbered provision."""

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    normalized_by_target: dict[str, SemanticDecision] = {}
    normalized: list[SemanticDecision] = []
    for decision in decisions:
        node = nodes_by_id[decision.target_structure_node_id]
        inherited_subtype = None
        if decision.semantic_class is SemanticClass.REQUIREMENT:
            ancestor_id = node.parent_structure_node_id
            while ancestor_id is not None:
                ancestor = nodes_by_id[ancestor_id]
                ancestor_decision = normalized_by_target.get(ancestor_id)
                is_container = (
                    node.list_ordinal is not None
                    and ancestor.kind is StructureNodeKind.SECTION
                ) or (node.list_ordinal is None and ancestor.list_ordinal is not None)
                if is_container and ancestor_decision is not None:
                    subtype = ancestor_decision.requirement_subtype
                    if (
                        ancestor_decision.semantic_class is SemanticClass.REQUIREMENT
                        and subtype is not None
                        and subtype is not RequirementSubtype.PRODUCT_REQUIREMENT
                    ):
                        inherited_subtype = subtype
                    break
                if ancestor.kind is StructureNodeKind.SECTION:
                    break
                ancestor_id = ancestor.parent_structure_node_id
        if (
            inherited_subtype is not None
            and decision.requirement_subtype is RequirementSubtype.PRODUCT_REQUIREMENT
        ):
            decision = decision.model_copy(
                update={
                    "requirement_subtype": inherited_subtype,
                    "reason_codes": tuple(
                        sorted(
                            {
                                *decision.reason_codes,
                                "structural_subtype_inheritance",
                            }
                        )
                    ),
                }
            )
        normalized.append(decision)
        normalized_by_target[decision.target_structure_node_id] = decision
    return tuple(normalized)


def _normalize_assembly_decisions(
    decisions: tuple[RequirementAssemblyDecision, ...],
    *,
    request: RequirementAssemblyRequest,
    run: SemanticProviderRunRecord,
    semantics: SemanticClassificationSnapshot,
) -> tuple[RequirementAssemblyItem, ...]:
    def unresolved(
        node: RequirementAssemblyNodeInput,
        reason_code: str,
    ) -> RequirementAssemblyItem:
        return _unresolved_assembly(
            node,
            reason_code,
            semantics=semantics,
        )

    expected_targets = request.scope_structure_node_ids
    if run.status not in {
        SemanticProviderRunStatus.SUCCESS,
        SemanticProviderRunStatus.PARTIAL,
    }:
        return tuple(
            unresolved(node, run.error_code or "provider_unavailable")
            for node in request.nodes
        )
    expected_target_set = set(expected_targets)
    decisions_by_target: dict[str, RequirementAssemblyDecision] = {}
    duplicated_targets: set[str] = set()
    for decision in decisions:
        target_id = decision.target_structure_node_id
        if target_id not in expected_target_set:
            continue
        if target_id in decisions_by_target:
            duplicated_targets.add(target_id)
            continue
        decisions_by_target[target_id] = decision
    result: list[RequirementAssemblyItem] = []
    for node in request.nodes:
        target_id = node.target_structure_node_id
        decision = decisions_by_target.get(target_id)
        if decision is None:
            result.append(
                unresolved(
                    node,
                    run.error_code or "provider_assembly_scope_missing",
                )
            )
            continue
        if target_id in duplicated_targets:
            result.append(unresolved(node, "provider_assembly_scope_duplicated"))
            continue
        if decision.status is RequirementAssemblyStatus.UNRESOLVED:
            result.append(
                unresolved(
                    node,
                    decision.reason_codes[0]
                    if decision.reason_codes
                    else "provider_assembly_unresolved",
                )
            )
            continue
        if not decision.include_in_view:
            (
                semantic_unit_id,
                unit_order,
                source_semantic_unit_ids,
                source_owner_structure_node_ids,
            ) = _assembly_lineage(node, semantics)
            result.append(
                RequirementAssemblyItem(
                    semantic_unit_id=semantic_unit_id,
                    source_semantic_unit_ids=source_semantic_unit_ids,
                    owner_structure_node_id=node.target_structure_node_id,
                    source_owner_structure_node_ids=(source_owner_structure_node_ids),
                    unit_order=unit_order,
                    source_order=node.source_order,
                    include_in_view=False,
                    role=None,
                    parent_semantic_unit_id=None,
                    parent_owner_structure_node_id=None,
                    status=RequirementAssemblyStatus.CANDIDATE,
                    reason_codes=decision.reason_codes,
                )
            )
            continue
        invalid = decision.role is None
        invalid = invalid or (
            decision.role is RequirementProjectionRole.PRIMARY
            and decision.parent_structure_node_id is not None
        )
        invalid = invalid or (
            decision.role is not RequirementProjectionRole.PRIMARY
            and decision.parent_structure_node_id is None
        )
        if invalid:
            result.append(unresolved(node, "provider_assembly_invalid"))
            continue
        (
            semantic_unit_id,
            unit_order,
            source_semantic_unit_ids,
            source_owner_structure_node_ids,
        ) = _assembly_lineage(node, semantics)
        annotations_by_owner = {
            annotation.owner_structure_node_id: annotation
            for annotation in semantics.annotations
        }
        parent_annotation = annotations_by_owner.get(
            decision.parent_structure_node_id or "",
        )
        result.append(
            RequirementAssemblyItem(
                semantic_unit_id=semantic_unit_id,
                source_semantic_unit_ids=source_semantic_unit_ids,
                owner_structure_node_id=node.target_structure_node_id,
                source_owner_structure_node_ids=(source_owner_structure_node_ids),
                unit_order=unit_order,
                source_order=node.source_order,
                include_in_view=True,
                role=decision.role,
                parent_semantic_unit_id=(
                    parent_annotation.semantic_unit_id
                    if parent_annotation is not None
                    else None
                ),
                parent_owner_structure_node_id=(decision.parent_structure_node_id),
                status=RequirementAssemblyStatus.CANDIDATE,
                reason_codes=decision.reason_codes or ("model_assembly",),
            )
        )
    return tuple(result)


def _assembly_snapshot(
    *,
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    provider: SemanticProviderPort,
    assembler_version: str,
    run: SemanticProviderRunRecord,
    items: tuple[RequirementAssemblyItem, ...],
) -> RequirementAssemblySnapshot:
    assembly_id = stable_requirement_assembly_id(
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=structure_snapshot_id(tree),
        semantic_snapshot_id=semantics.semantic_snapshot_id,
        semantic_unit_plan_id=semantics.semantic_unit_plan_id,
        assembler_id=provider.provider_id,
        assembler_version=assembler_version,
        provider_run_id=run.provider_run_id,
        items=items,
    )
    return RequirementAssemblySnapshot(
        requirement_assembly_id=assembly_id,
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=structure_snapshot_id(tree),
        semantic_snapshot_id=semantics.semantic_snapshot_id,
        semantic_unit_plan_id=semantics.semantic_unit_plan_id,
        assembler_id=provider.provider_id,
        assembler_version=assembler_version,
        provider_run_id=run.provider_run_id,
        scope_semantic_unit_ids=semantics.scope_semantic_unit_ids,
        scope_owner_structure_node_ids=(semantics.scope_owner_structure_node_ids),
        items=items,
    )


def _annotation_evidence(
    annotation: object, source_ir: SourceIRSnapshot
) -> tuple[SemanticEvidenceInput, ...]:
    spans = getattr(annotation, "grounding_spans")
    seen: set[StructureSourceRef] = set()
    result: list[SemanticEvidenceInput] = []
    for span in spans:
        source_ref = span.source_ref
        if source_ref in seen:
            continue
        seen.add(source_ref)
        text = _resolve_source_text(source_ref, source_ir)
        result.append(
            SemanticEvidenceInput(
                source_ref=source_ref,
                text=text,
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(result)


def _resolve_source_text(
    source_ref: StructureSourceRef, source_ir: SourceIRSnapshot
) -> str:
    if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
        values = {item.source_node_id: item.raw_text for item in source_ir.nodes}
    elif source_ref.kind is StructureSourceRefKind.TABLE_CELL:
        values = {
            cell.cell_id: cell.raw_text
            for node in source_ir.nodes
            if node.table_payload is not None
            for cell in node.table_payload.cells
        }
    else:
        values = {
            observation.observation_id: observation.raw_text
            for snapshot in source_ir.visual_text_snapshots
            for observation in snapshot.observations
        }
    try:
        return values[source_ref.source_id]
    except KeyError as exc:
        raise ValueError("semantic source ref is absent from SourceIR") from exc


def _ancestor_ids(
    target_id: str,
    nodes_by_id: dict[str, StructureNode],
) -> tuple[str, ...]:
    values: list[str] = []
    parent_id = nodes_by_id[target_id].parent_structure_node_id
    while parent_id is not None:
        values.append(parent_id)
        parent_id = nodes_by_id[parent_id].parent_structure_node_id
    values.reverse()
    return tuple(values)


def _unresolved_semantic(target: str, reason_code: str) -> SemanticDecision:
    return SemanticDecision(
        target_structure_node_id=target,
        semantic_class=SemanticClass.UNRESOLVED,
        requirement_subtype=None,
        evidence_source_refs=(),
        reason_codes=(reason_code,),
    )


def _unresolved_assembly(
    node: RequirementAssemblyNodeInput,
    reason_code: str,
    *,
    semantics: SemanticClassificationSnapshot,
) -> RequirementAssemblyItem:
    (
        semantic_unit_id,
        unit_order,
        source_semantic_unit_ids,
        source_owner_structure_node_ids,
    ) = _assembly_lineage(node, semantics)
    return RequirementAssemblyItem(
        semantic_unit_id=semantic_unit_id,
        source_semantic_unit_ids=source_semantic_unit_ids,
        owner_structure_node_id=node.target_structure_node_id,
        source_owner_structure_node_ids=source_owner_structure_node_ids,
        unit_order=unit_order,
        source_order=node.source_order,
        include_in_view=False,
        role=None,
        parent_semantic_unit_id=None,
        parent_owner_structure_node_id=None,
        status=RequirementAssemblyStatus.UNRESOLVED,
        reason_codes=(reason_code,),
    )


def _excluded_non_requirement_assembly(
    node: RequirementAssemblyNodeInput,
    *,
    semantics: SemanticClassificationSnapshot,
) -> RequirementAssemblyItem:
    (
        semantic_unit_id,
        unit_order,
        source_semantic_unit_ids,
        source_owner_structure_node_ids,
    ) = _assembly_lineage(node, semantics)
    return RequirementAssemblyItem(
        semantic_unit_id=semantic_unit_id,
        source_semantic_unit_ids=source_semantic_unit_ids,
        owner_structure_node_id=node.target_structure_node_id,
        source_owner_structure_node_ids=source_owner_structure_node_ids,
        unit_order=unit_order,
        source_order=node.source_order,
        include_in_view=False,
        role=None,
        parent_semantic_unit_id=None,
        parent_owner_structure_node_id=None,
        status=RequirementAssemblyStatus.CANDIDATE,
        reason_codes=("semantic_non_requirement_excluded",),
    )


def _assembly_lineage(
    node: RequirementAssemblyNodeInput,
    semantics: SemanticClassificationSnapshot,
) -> tuple[str, int, tuple[str, ...], tuple[str, ...]]:
    annotations_by_id = {
        annotation.semantic_annotation_id: annotation
        for annotation in semantics.annotations
    }
    try:
        annotations = tuple(
            annotations_by_id[annotation_id]
            for annotation_id in node.semantic_annotation_ids
        )
    except KeyError as exc:
        raise ValueError(
            "assembly request annotation is absent from semantics"
        ) from exc
    primary = next(
        (
            annotation
            for annotation in annotations
            if annotation.owner_structure_node_id == node.target_structure_node_id
        ),
        None,
    )
    if primary is None:
        raise ValueError("assembly request has no primary semantic unit")
    ordered = (
        primary,
        *(annotation for annotation in annotations if annotation is not primary),
    )
    unit_order_by_id = {
        unit_id: unit_order
        for unit_order, unit_id in enumerate(semantics.scope_semantic_unit_ids)
    }
    return (
        primary.semantic_unit_id,
        unit_order_by_id[primary.semantic_unit_id],
        tuple(annotation.semantic_unit_id for annotation in ordered),
        tuple(annotation.owner_structure_node_id for annotation in ordered),
    )


def _dedupe_evidence(
    values: Iterable[SemanticEvidenceInput],
) -> tuple[SemanticEvidenceInput, ...]:
    result: list[SemanticEvidenceInput] = []
    seen: set[StructureSourceRef] = set()
    for value in values:
        if value.source_ref in seen:
            continue
        seen.add(value.source_ref)
        result.append(value)
    return tuple(result)


def _version_with_result(
    implementation_version: str,
    model_id: str,
    prompt_version: str,
    result_digest: str,
) -> str:
    prompt_tail = prompt_version.rsplit("/", 1)[-1]
    return (
        f"{implementation_version};model={model_id};"
        f"prompt={prompt_tail};result={result_digest[:12]}"
    )


__all__ = [
    "DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST",
    "LlmRequirementExtractionResult",
    "MODEL_REQUIREMENT_ASSEMBLER_VERSION",
    "MODEL_SEMANTIC_CLASSIFIER_VERSION",
    "SEMANTIC_PLANNING_RUNTIME_VERSION",
    "ProviderRequirementAssembly",
    "ProviderRequirementAssemblyPlanItem",
    "ProviderSemanticClassification",
    "ProviderSemanticClassificationPlan",
    "ProviderSemanticClassificationPlanItem",
    "assemble_requirements_with_provider",
    "classify_content_with_provider",
    "extract_requirements_with_provider",
    "plan_content_classification_provider_requests",
    "plan_requirement_assembly_provider_request",
    "plan_requirement_assembly_provider_requests",
]
