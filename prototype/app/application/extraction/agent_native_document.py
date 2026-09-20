from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.application.extraction.document_decomposition import (
    DocumentDecompositionItemReferenceV1,
    DocumentDecompositionItemRequestV1,
    DocumentDecompositionProviderPort,
    DocumentDecompositionProviderResult,
    DocumentDecompositionRequestV1,
    DocumentDecompositionSourceSegmentV1,
    document_decomposition_lineage_sha256,
)
from app.application.extraction.document_parent_boundaries import (
    DocumentParentBoundaryPlan,
    DocumentParentBoundaryProviderPort,
    DocumentParentBoundaryProviderResult,
    build_document_parent_boundary_plans,
)
from app.application.extraction.llm_provision_batch import (
    LlmRequirementDocumentProgress,
)
from app.application.extraction.native_atomization_reference import (
    NativeAtomizationReferenceStatus,
)
from app.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryStatus,
    NativeParentContextDisposition,
    NativeProvisionDisposition,
)
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.ports.semantic_provider import (
    DocumentSessionSemanticProviderPort,
    SemanticImageInput,
)


AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION = "agent-native-document-extraction/v2"


class AgentNativeWorkflowMode(str, Enum):
    BASELINE = "BASELINE"
    EXPLICIT_ATOMIZATION = "EXPLICIT_ATOMIZATION"


class AgentNativeDocumentStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True, slots=True)
class AgentNativeDocumentResult:
    source_ir_id: str
    source_sha256: str
    provider_binding: tuple[str, str, str]
    page_numbers: tuple[int, ...]
    parent_plans: tuple[DocumentParentBoundaryPlan, ...]
    parent_boundary_results: tuple[DocumentParentBoundaryProviderResult, ...]
    decomposition_requests: tuple[DocumentDecompositionRequestV1, ...]
    decomposition_results: tuple[DocumentDecompositionProviderResult, ...]
    status: AgentNativeDocumentStatus
    workflow_mode: AgentNativeWorkflowMode = AgentNativeWorkflowMode.BASELINE
    runtime_version: str = AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION

    def __post_init__(self) -> None:
        if self.runtime_version != AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION:
            raise ValueError("agent-native document runtime is incompatible")
        if not self.source_ir_id or len(self.source_sha256) != 64:
            raise ValueError("agent-native document source identity is invalid")
        if (
            len(self.provider_binding) != 3
            or any(not item.strip() for item in self.provider_binding)
            or self.page_numbers != tuple(sorted(set(self.page_numbers)))
            or not self.page_numbers
        ):
            raise ValueError("agent-native document binding is invalid")
        if len(self.parent_plans) != len(self.parent_boundary_results):
            raise ValueError("agent-native parent results are incomplete")
        if len(self.decomposition_requests) != len(self.decomposition_results):
            raise ValueError("agent-native decomposition results are incomplete")
        if self.workflow_mode is AgentNativeWorkflowMode.BASELINE and (
            self.decomposition_requests or self.decomposition_results
        ):
            raise ValueError("baseline extraction cannot contain atomization output")
        if (
            self.workflow_mode is AgentNativeWorkflowMode.EXPLICIT_ATOMIZATION
            and not self.decomposition_requests
        ):
            raise ValueError("explicit atomization requires a requested scope")
        run_records = (
            *(item.run for item in self.parent_boundary_results),
            *(item.run for item in self.decomposition_results),
        )
        if any(
            (run.provider_id, run.provider_version, run.model_id)
            != self.provider_binding
            for run in run_records
        ):
            raise ValueError("agent-native provider identity drifted")
        expected_partial = any(
            item.reference.status is NativeParentBoundaryStatus.PARTIAL
            for item in self.parent_boundary_results
        )
        if self.workflow_mode is AgentNativeWorkflowMode.EXPLICIT_ATOMIZATION:
            expected_partial = expected_partial or any(
                item.status is NativeAtomizationReferenceStatus.PARTIAL
                for result in self.decomposition_results
                for item in result.reference.items
            )
        expected_status = (
            AgentNativeDocumentStatus.PARTIAL
            if expected_partial
            else AgentNativeDocumentStatus.COMPLETE
        )
        if self.status is not expected_status:
            raise ValueError("agent-native document status is inconsistent")

    @property
    def parent_count(self) -> int:
        return sum(
            len(item.reference.provisions) for item in self.parent_boundary_results
        )

    @property
    def requirement_parent_count(self) -> int:
        return sum(
            item.disposition is NativeProvisionDisposition.REQUIREMENT
            for result in self.parent_boundary_results
            for item in result.reference.provisions
        )

    @property
    def obligation_count(self) -> int:
        return sum(
            len(item.obligations)
            for result in self.decomposition_results
            for item in result.reference.items
        )

    @property
    def partial_decomposition_parent_count(self) -> int:
        return sum(
            item.status is NativeAtomizationReferenceStatus.PARTIAL
            for result in self.decomposition_results
            for item in result.reference.items
        )


def extract_agent_native_document_with_provider(
    source_ir: SourceIRSnapshot,
    provider: object,
    *,
    images: tuple[SemanticImageInput, ...],
    progress_callback: Callable[[LlmRequirementDocumentProgress], None] | None = None,
    max_parents_per_request: int = 12,
    atomize: bool = False,
) -> AgentNativeDocumentResult:
    """Build a source-faithful parent baseline without legacy node targets.

    Atomization is deliberately opt-in. Product extraction calls this function
    with the default ``atomize=False``; the separate, user-scoped atomization
    workflow owns semantic child generation.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not isinstance(provider, DocumentParentBoundaryProviderPort):
        raise TypeError("provider does not support Agent-native parent recognition")
    if atomize and not isinstance(provider, DocumentDecompositionProviderPort):
        raise TypeError("provider does not support explicit document atomization")
    if (
        isinstance(max_parents_per_request, bool)
        or not 1 <= max_parents_per_request <= 24
    ):
        raise ValueError("agent-native parent batch size is invalid")
    plans = build_document_parent_boundary_plans(source_ir)
    planned_source_pages = tuple(
        sorted(
            {
                segment.page_number
                for plan in plans
                for segment in plan.request.source_segments
            }
        )
    )
    page_numbers = tuple(image.page_number for image in images)
    if (
        page_numbers != tuple(sorted(set(page_numbers)))
        or not set(planned_source_pages).issubset(page_numbers)
        or any(image.source_sha256 != source_ir.source_sha256 for image in images)
    ):
        raise ValueError("agent-native page images do not cover source evidence")
    provider_binding = (
        str(getattr(provider, "provider_id", "")),
        str(getattr(provider, "provider_version", "")),
        str(getattr(provider, "model_id", "")),
    )
    if isinstance(provider, DocumentSessionSemanticProviderPort):
        provider.prepare_document_session(
            source_ir_id=source_ir.source_ir_id,
            structure_snapshot_id=(
                "agent-source-session-"
                + hashlib.sha256(
                    (source_ir.source_ir_id + source_ir.source_sha256).encode("utf-8")
                ).hexdigest()[:32]
            ),
            images=images,
        )
    _emit_progress(
        progress_callback,
        stage="planned",
        total_pages=len(page_numbers),
        completed_pages=0,
        parent_count=0,
    )
    parent_results: list[DocumentParentBoundaryProviderResult] = []
    completed_pages = 0
    for plan in plans:
        parent_results.append(provider.identify_parent_boundaries(plan.request))
        completed_pages += len(plan.request.source_segments)
        _emit_progress(
            progress_callback,
            stage="running",
            total_pages=len(page_numbers),
            completed_pages=completed_pages,
            parent_count=sum(len(item.reference.provisions) for item in parent_results),
        )
    decomposition_requests: list[DocumentDecompositionRequestV1] = []
    decomposition_results: list[DocumentDecompositionProviderResult] = []
    if atomize:
        assert isinstance(provider, DocumentDecompositionProviderPort)
        parent_items = _build_decomposition_items(plans, tuple(parent_results))
        for start in range(0, len(parent_items), max_parents_per_request):
            request = DocumentDecompositionRequestV1(
                source_ir_id=source_ir.source_ir_id,
                source_sha256=source_ir.source_sha256,
                items=parent_items[start : start + max_parents_per_request],
            )
            decomposition_requests.append(request)
            decomposition_results.append(provider.decompose_document_parents(request))
    status = (
        AgentNativeDocumentStatus.PARTIAL
        if any(
            item.reference.status is NativeParentBoundaryStatus.PARTIAL
            for item in parent_results
        )
        or (
            atomize
            and any(
                item.status is NativeAtomizationReferenceStatus.PARTIAL
                for result in decomposition_results
                for item in result.reference.items
            )
        )
        else AgentNativeDocumentStatus.COMPLETE
    )
    result = AgentNativeDocumentResult(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        provider_binding=provider_binding,
        page_numbers=page_numbers,
        parent_plans=plans,
        parent_boundary_results=tuple(parent_results),
        decomposition_requests=tuple(decomposition_requests),
        decomposition_results=tuple(decomposition_results),
        status=status,
        workflow_mode=(
            AgentNativeWorkflowMode.EXPLICIT_ATOMIZATION
            if atomize
            else AgentNativeWorkflowMode.BASELINE
        ),
    )
    _emit_progress(
        progress_callback,
        stage="completed",
        total_pages=len(page_numbers),
        completed_pages=len(page_numbers),
        parent_count=result.parent_count,
        partial=result.status is AgentNativeDocumentStatus.PARTIAL,
    )
    return result


def _build_decomposition_items(
    plans: tuple[DocumentParentBoundaryPlan, ...],
    results: tuple[DocumentParentBoundaryProviderResult, ...],
) -> tuple[DocumentDecompositionItemRequestV1, ...]:
    items: list[DocumentDecompositionItemRequestV1] = []
    document_order = 0
    for plan, result in zip(plans, results, strict=True):
        for provision in result.reference.provisions:
            if provision.disposition is not NativeProvisionDisposition.REQUIREMENT:
                continue
            segments: list[DocumentDecompositionSourceSegmentV1] = []
            for source_range in provision.body_ranges:
                source_segment = plan.request.source_segments[
                    source_range.segment_index
                ]
                text = source_segment.source_text[
                    source_range.start_offset : source_range.end_offset
                ]
                segments.append(
                    DocumentDecompositionSourceSegmentV1(
                        segment_index=len(segments),
                        page_number=source_segment.page_number,
                        source_ref=source_segment.source_ref,
                        source_start_offset=source_range.start_offset,
                        source_end_offset=source_range.end_offset,
                        source_text=text,
                        source_text_sha256=hashlib.sha256(
                            text.encode("utf-8")
                        ).hexdigest(),
                    )
                )
            segment_tuple = tuple(segments)
            lineage = document_decomposition_lineage_sha256(
                parent_provision_id=provision.provision_id,
                explicit_label=provision.explicit_label,
                ancestor_labels=provision.ancestor_labels,
                source_order=document_order,
                source_segments=segment_tuple,
            )
            items.append(
                DocumentDecompositionItemRequestV1(
                    parent_provision_id=provision.provision_id,
                    lineage_sha256=lineage,
                    explicit_label=provision.explicit_label,
                    ancestor_labels=provision.ancestor_labels,
                    source_order=document_order,
                    source_segments=segment_tuple,
                )
            )
            document_order += 1
    return tuple(items)


def serialize_agent_native_document_result(
    result: AgentNativeDocumentResult,
) -> dict[str, object]:
    """Build a complete source-span baseline with no fabricated Structure owners.

    Both provision and context ranges are visible.  The parent-boundary
    validator already exact-covers every non-whitespace source character, so
    dropping context here would turn a complete Agent observation into an
    incomplete product baseline.
    """

    requests_by_id = {
        item.parent_provision_id: item
        for request in result.decomposition_requests
        for item in request.items
    }
    references_by_id: dict[str, DocumentDecompositionItemReferenceV1] = {
        item.parent_provision_id: item
        for provider_result in result.decomposition_results
        for item in provider_result.reference.items
    }
    items: list[dict[str, object]] = []
    parents: list[dict[str, object]] = []
    requirements: list[dict[str, object]] = []
    decomposition: list[dict[str, object]] = []
    source_order = 0
    context_count = 0
    for plan, boundary_result in zip(
        result.parent_plans,
        result.parent_boundary_results,
        strict=True,
    ):
        ordered_source_entries: list[tuple[tuple[int, int, int], str, object]] = []
        for provision in boundary_result.reference.provisions:
            first_range = min(
                provision.body_ranges,
                key=lambda item: (
                    item.segment_index,
                    item.start_offset,
                    item.end_offset,
                ),
            )
            ordered_source_entries.append(
                (
                    (
                        first_range.segment_index,
                        first_range.start_offset,
                        first_range.end_offset,
                    ),
                    "PROVISION",
                    provision,
                )
            )
        for context in boundary_result.reference.context_ranges:
            source_range = context.source_range
            ordered_source_entries.append(
                (
                    (
                        source_range.segment_index,
                        source_range.start_offset,
                        source_range.end_offset,
                    ),
                    "CONTEXT",
                    context,
                )
            )
        ordered_source_entries.sort(key=lambda item: item[0])

        for _, entry_kind, entry in ordered_source_entries:
            if entry_kind == "CONTEXT":
                context = entry
                source_range = context.source_range
                segment = plan.request.source_segments[source_range.segment_index]
                text = segment.source_text[
                    source_range.start_offset : source_range.end_offset
                ]
                context_id = (
                    "agent-context-"
                    + hashlib.sha256(
                        _canonical_json(
                            {
                                "source_sha256": result.source_sha256,
                                "scope_id": plan.request.scope_id,
                                "source_range": source_range.model_dump(mode="json"),
                                "disposition": context.disposition.value,
                            }
                        ).encode("utf-8")
                    ).hexdigest()[:32]
                )
                context_payload = {
                    "id": context_id,
                    "title": _context_entry_title(context.disposition, text),
                    "text": text,
                    "context_text": "",
                    "role": "CONTEXT",
                    "semantic_class": "INFORMATION",
                    "content_role": context.disposition.value,
                    "requirementness": (
                        "UNRESOLVED"
                        if context.disposition
                        is NativeParentContextDisposition.UNRESOLVED
                        else "NON_REQUIREMENT"
                    ),
                    "review_status": (
                        "NEEDS_CLASSIFICATION"
                        if context.disposition
                        is NativeParentContextDisposition.UNRESOLVED
                        else "INFORMATION"
                    ),
                    "source_order": source_order,
                    "page_start": segment.page_number,
                    "page_end": segment.page_number,
                    "parent_id": None,
                    "depth": 0,
                    "source_ranges": [
                        {
                            **source_range.model_dump(mode="json"),
                            "page_number": segment.page_number,
                            "source_ref": segment.source_ref,
                        }
                    ],
                    "reason_codes": list(context.reason_codes),
                    "agent_native": True,
                }
                parents.append(context_payload)
                items.append(context_payload)
                context_count += 1
                source_order += 1
                continue

            provision = entry
            ranges = [
                {
                    **source_range.model_dump(mode="json"),
                    "page_number": plan.request.source_segments[
                        source_range.segment_index
                    ].page_number,
                    "source_ref": plan.request.source_segments[
                        source_range.segment_index
                    ].source_ref,
                }
                for source_range in provision.body_ranges
            ]
            text = "\n".join(
                plan.request.source_segments[item.segment_index].source_text[
                    item.start_offset : item.end_offset
                ]
                for item in provision.body_ranges
            )
            pages = tuple(sorted({int(item["page_number"]) for item in ranges}))
            review_status = (
                "REQUIREMENT"
                if provision.disposition is NativeProvisionDisposition.REQUIREMENT
                else "INFORMATION"
                if provision.disposition is NativeProvisionDisposition.INFORMATION
                else "NEEDS_CLASSIFICATION"
            )
            parent = {
                "id": provision.provision_id,
                "title": provision.explicit_label or f"条款 {source_order + 1}",
                "text": text,
                "context_text": " > ".join(provision.ancestor_labels),
                "role": "PRIMARY",
                "semantic_class": provision.disposition.value,
                "requirementness": (
                    "REQUIREMENT"
                    if provision.disposition is NativeProvisionDisposition.REQUIREMENT
                    else "NON_REQUIREMENT"
                    if provision.disposition is NativeProvisionDisposition.INFORMATION
                    else "UNRESOLVED"
                ),
                "review_status": review_status,
                "source_order": source_order,
                "page_start": min(pages),
                "page_end": max(pages),
                "parent_id": None,
                "depth": 0,
                "source_ranges": ranges,
                "reason_codes": list(provision.reason_codes),
                "agent_native": True,
            }
            parents.append(parent)
            items.append(parent)
            if provision.disposition is NativeProvisionDisposition.REQUIREMENT:
                requirements.append(parent)
            if (
                provision.disposition is NativeProvisionDisposition.REQUIREMENT
                and result.workflow_mode
                is AgentNativeWorkflowMode.EXPLICIT_ATOMIZATION
            ):
                request_item = requests_by_id[provision.provision_id]
                reference = references_by_id[provision.provision_id]
                child_payloads: list[dict[str, object]] = []
                for child_index, obligation in enumerate(reference.obligations, 1):
                    source_text = "\n".join(
                        request_item.source_segments[item.segment_index].source_text[
                            item.start_offset : item.end_offset
                        ]
                        for item in obligation.source_ranges
                    )
                    child_id = (
                        "agent-requirement-"
                        + hashlib.sha256(
                            _canonical_json(
                                {
                                    "parent": provision.provision_id,
                                    "ranges": [
                                        item.model_dump(mode="json")
                                        for item in obligation.source_ranges
                                    ],
                                    "statement": obligation.atomic_statement,
                                }
                            ).encode("utf-8")
                        ).hexdigest()[:32]
                    )
                    child_pages = tuple(
                        sorted(
                            {
                                request_item.source_segments[
                                    item.segment_index
                                ].page_number
                                for item in obligation.source_ranges
                            }
                        )
                    )
                    child = {
                        "id": child_id,
                        "title": f"{provision.explicit_label or '需求'}-{child_index}",
                        "text": obligation.atomic_statement,
                        "role": "CHILD",
                        "semantic_class": "REQUIREMENT",
                        "review_status": "REQUIREMENT",
                        "source_order": source_order,
                        "page_start": min(child_pages),
                        "page_end": max(child_pages),
                        "parent_id": provision.provision_id,
                        "depth": 1,
                        "source_ranges": [
                            {
                                **item.model_dump(mode="json"),
                                "page_number": request_item.source_segments[
                                    item.segment_index
                                ].page_number,
                                "source_ref": request_item.source_segments[
                                    item.segment_index
                                ].source_ref,
                            }
                            for item in obligation.source_ranges
                        ],
                        "derivation_type": (
                            "VERBATIM_SPAN"
                            if obligation.atomic_statement == source_text
                            else "NORMALIZED_DERIVATION"
                        ),
                        "split_basis": obligation.split_basis,
                        "reason_codes": list(obligation.reason_codes),
                        "agent_native": True,
                    }
                    child_payloads.append(child)
                    items.append(child)
                    requirements.append(child)
                decomposition.append(
                    {
                        "parent_provision_id": provision.provision_id,
                        "lineage_sha256": request_item.lineage_sha256,
                        "status": reference.status.value,
                        "obligations": child_payloads,
                        "context_ranges": [
                            item.model_dump(mode="json")
                            for item in reference.context_ranges
                        ],
                        "diagnostic_codes": list(reference.diagnostic_codes),
                    }
                )
            source_order += 1
    return {
        "items": items,
        "review_groups": parents,
        "requirement_groups": requirements,
        "requirement_decomposition": decomposition,
        "baseline": {
            "schema_version": "agent-native-requirement-baseline/v1",
            "source_text_preserved": True,
            "exact_cover_visible": True,
            "entry_count": len(parents),
            "parent_count": result.parent_count,
            "context_entry_count": context_count,
            "requirement_parent_count": result.requirement_parent_count,
            "classification_basis": "AGENT_SOURCE_BOUND_PARENT_DISPOSITION",
            "classification_policy_attested": False,
        },
        "atomization": {
            "schema_version": "agent-native-atomization-state/v1",
            "state": (
                "APPLIED"
                if result.workflow_mode
                is AgentNativeWorkflowMode.EXPLICIT_ATOMIZATION
                else "NOT_REQUESTED"
            ),
            "explicit_trigger_required": True,
            "parent_preserved": True,
            "children_are_derived_projections": True,
            "source_verbatim_guaranteed": False,
        },
    }


def _context_entry_title(
    disposition: NativeParentContextDisposition,
    source_text: str,
) -> str:
    labels = {
        NativeParentContextDisposition.HEADING: "标题",
        NativeParentContextDisposition.METADATA: "文档信息",
        NativeParentContextDisposition.PRESENTATION: "说明内容",
        NativeParentContextDisposition.REFERENCE: "引用信息",
        NativeParentContextDisposition.UNRESOLVED: "待判断内容",
    }
    first_line = next(
        (line.strip() for line in source_text.splitlines() if line.strip()),
        "",
    )
    return first_line[:120] or labels[disposition]


def _emit_progress(
    callback: Callable[[LlmRequirementDocumentProgress], None] | None,
    *,
    stage: str,
    total_pages: int,
    completed_pages: int,
    parent_count: int,
    partial: bool = False,
) -> None:
    if callback is None:
        return
    completed = stage == "completed"
    callback(
        LlmRequirementDocumentProgress(
            stage=stage,
            total_clauses=parent_count,
            total_targets=total_pages,
            total_chunks=1,
            completed_chunks=1 if completed else 0,
            completed_targets=completed_pages,
            successful_chunks=1 if completed and not partial else 0,
            unresolved_chunks=1 if completed and partial else 0,
            blocked_chunks=0,
            current_chunk_index=None if completed else 1,
            current_chunk_target_count=total_pages,
            progress_basis="PAGE_EVIDENCE",
        )
    )


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "AGENT_NATIVE_DOCUMENT_RUNTIME_VERSION",
    "AgentNativeDocumentResult",
    "AgentNativeDocumentStatus",
    "AgentNativeWorkflowMode",
    "extract_agent_native_document_with_provider",
    "serialize_agent_native_document_result",
]
