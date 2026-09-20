from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol, runtime_checkable

from app.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryReceiptV1,
    NativeParentBoundaryReferenceV1,
    NativeParentBoundaryRequestV1,
    NativeParentBoundarySourceSegmentV1,
)
from app.application.extraction.structure_snapshot import StructureSnapshot
from app.domain.extraction.semantic_provider import SemanticProviderRunRecord
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)


@dataclass(frozen=True, slots=True)
class DocumentParentBoundarySourceRefSpan:
    segment_index: int
    source_ref: StructureSourceRef
    start_offset: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class DocumentParentBoundaryPlan:
    request: NativeParentBoundaryRequestV1
    source_ref_spans: tuple[DocumentParentBoundarySourceRefSpan, ...]


@dataclass(frozen=True, slots=True)
class DocumentParentBoundaryProviderResult:
    reference: NativeParentBoundaryReferenceV1
    validation_receipt: NativeParentBoundaryReceiptV1
    run: SemanticProviderRunRecord


@runtime_checkable
class DocumentParentBoundaryProviderPort(Protocol):
    def identify_parent_boundaries(
        self,
        request: NativeParentBoundaryRequestV1,
    ) -> DocumentParentBoundaryProviderResult: ...


@dataclass(frozen=True, slots=True)
class _SourceFact:
    text: str
    page_number: int
    channel: str


def build_document_parent_boundary_plans(
    source_ir: SourceIRSnapshot,
    *,
    max_pages_per_request: int = 32,
) -> tuple[DocumentParentBoundaryPlan, ...]:
    """Build page-ordered source segments without a Structure-node universe.

    The immutable SourceIR/page channel is the grounding boundary.  A
    deterministic StructureTree may still be exposed to the Agent as an
    optional tool, but it must not decide which source facts the Agent is
    allowed to understand or how many work items it must return.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if type(max_pages_per_request) is not int or not 1 <= max_pages_per_request <= 32:
        raise ValueError("parent-boundary planning scope is invalid")
    facts = _agent_source_facts(source_ir)
    refs_by_page: dict[int, list[StructureSourceRef]] = {}
    for source_ref, fact in facts:
        refs_by_page.setdefault(fact.page_number, []).append(source_ref)
    pages = tuple(sorted(refs_by_page))
    if not pages:
        raise ValueError("parent-boundary planning has no page-bound source text")
    facts_by_ref = dict(facts)
    plans: list[DocumentParentBoundaryPlan] = []
    for page_start in range(0, len(pages), max_pages_per_request):
        selected_pages = pages[page_start : page_start + max_pages_per_request]
        segments: list[NativeParentBoundarySourceSegmentV1] = []
        bindings: list[DocumentParentBoundarySourceRefSpan] = []
        for segment_index, page_number in enumerate(selected_pages):
            buffer: list[str] = []
            offset = 0
            channels: set[str] = set()
            for source_ref in refs_by_page[page_number]:
                fact = facts_by_ref[source_ref]
                if buffer:
                    buffer.append("\n")
                    offset += 1
                start_offset = offset
                buffer.append(fact.text)
                offset += len(fact.text)
                bindings.append(
                    DocumentParentBoundarySourceRefSpan(
                        segment_index=segment_index,
                        source_ref=source_ref,
                        start_offset=start_offset,
                        end_offset=offset,
                    )
                )
                channels.add(fact.channel)
            text = "".join(buffer)
            channel = (
                "VISUAL_TEXT"
                if "VISUAL_TEXT" in channels
                else "TABLE_TEXT"
                if "TABLE_TEXT" in channels
                else "NATIVE_TEXT"
            )
            segments.append(
                NativeParentBoundarySourceSegmentV1(
                    segment_index=segment_index,
                    page_number=page_number,
                    evidence_channel=channel,
                    source_ref=(
                        "document-page-"
                        + str(page_number)
                        + "-"
                        + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                    ),
                    source_text=text,
                    source_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
            )
        scope_id = (
            "document-parent-scope-"
            + _canonical_hash(
                {
                    "source_sha256": source_ir.source_sha256,
                    "pages": list(selected_pages),
                    "segment_hashes": [item.source_text_sha256 for item in segments],
                }
            )[:32]
        )
        plans.append(
            DocumentParentBoundaryPlan(
                request=NativeParentBoundaryRequestV1(
                    source_sha256=source_ir.source_sha256,
                    scope_id=scope_id,
                    source_segments=tuple(segments),
                ),
                source_ref_spans=tuple(bindings),
            )
        )
    return tuple(plans)


def build_parent_boundary_target_windows(
    plans: tuple[DocumentParentBoundaryPlan, ...],
    results: tuple[DocumentParentBoundaryProviderResult, ...],
    tree: StructureSnapshot,
    scope_structure_node_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], ...]:
    """Project Agent provision/context ranges back to an exact node partition."""

    if not plans or len(plans) != len(results):
        raise ValueError("parent-boundary plan results are incomplete")
    coverage_by_ref: dict[StructureSourceRef, Counter[str]] = {}
    for plan, result in zip(plans, results, strict=True):
        if (
            result.reference.source_sha256 != plan.request.source_sha256
            or result.reference.scope_id != plan.request.scope_id
            or result.validation_receipt.source_sha256 != plan.request.source_sha256
            or result.validation_receipt.scope_id != plan.request.scope_id
        ):
            raise ValueError("parent-boundary result identity drifted")
        for provision in result.reference.provisions:
            key = f"P:{plan.request.scope_id}:{provision.provision_id}"
            for source_range in provision.body_ranges:
                _add_range_coverage(
                    coverage_by_ref,
                    plan.source_ref_spans,
                    key,
                    source_range.segment_index,
                    source_range.start_offset,
                    source_range.end_offset,
                )
        for context_index, context in enumerate(result.reference.context_ranges):
            key = (
                f"C:{plan.request.scope_id}:{context_index}:{context.disposition.value}"
            )
            source_range = context.source_range
            _add_range_coverage(
                coverage_by_ref,
                plan.source_ref_spans,
                key,
                source_range.segment_index,
                source_range.start_offset,
                source_range.end_offset,
            )
    scope_set = set(scope_structure_node_ids)
    ordered_nodes = tuple(
        node for node in tree.nodes if node.structure_node_id in scope_set
    )
    if tuple(node.structure_node_id for node in ordered_nodes) != (
        scope_structure_node_ids
    ):
        raise ValueError("parent-boundary target scope is not source ordered")
    windows: list[list[str]] = []
    previous_key = ""
    for node in ordered_nodes:
        scores: Counter[str] = Counter()
        for source_ref in node.effective_semantic_source_refs:
            scores.update(coverage_by_ref.get(source_ref, {}))
        key = (
            max(
                scores,
                key=lambda item: (scores[item], item.startswith("P:"), item),
            )
            if scores
            else f"U:{node.parent_structure_node_id or 'root'}"
        )
        if not windows or key != previous_key:
            windows.append([])
        windows[-1].append(node.structure_node_id)
        previous_key = key
    output = tuple(tuple(window) for window in windows if window)
    if tuple(item for window in output for item in window) != (
        scope_structure_node_ids
    ):
        raise ValueError("parent-boundary windows do not exact-partition scope")
    return output


def _add_range_coverage(
    coverage_by_ref: dict[StructureSourceRef, Counter[str]],
    spans: tuple[DocumentParentBoundarySourceRefSpan, ...],
    key: str,
    segment_index: int,
    start_offset: int,
    end_offset: int,
) -> None:
    for span in spans:
        if span.segment_index != segment_index:
            continue
        overlap = max(
            0,
            min(span.end_offset, end_offset) - max(span.start_offset, start_offset),
        )
        if overlap:
            coverage_by_ref.setdefault(span.source_ref, Counter())[key] += overlap


def _agent_source_facts(
    source_ir: SourceIRSnapshot,
) -> tuple[tuple[StructureSourceRef, _SourceFact], ...]:
    """Select one source-ordered text authority per page without Structure.

    Native PDF text blocks are the canonical born-digital text channel.
    Parallel table candidates remain available through parser tools and page
    pixels, but are not copied into the canonical text stream because that
    would duplicate the same visible characters.  When a visual TEXT capture
    exists, its ordered observations replace native text for that page.
    """

    visual_snapshots = {
        snapshot.page_number: snapshot
        for snapshot in source_ir.visual_text_snapshots
        if "TEXT" in snapshot.evaluated_content_types
    }
    output: list[tuple[StructureSourceRef, _SourceFact]] = []
    seen_refs: set[StructureSourceRef] = set()
    for node in sorted(source_ir.nodes, key=lambda item: item.source_order):
        page = node.native_locator.page_number
        if (
            page is None
            or page in visual_snapshots
            or node.table_payload is not None
            or not node.raw_text.strip()
        ):
            continue
        source_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=node.source_node_id,
        )
        if source_ref in seen_refs:
            continue
        seen_refs.add(source_ref)
        output.append((source_ref, _SourceFact(node.raw_text, page, "NATIVE_TEXT")))
    for page_number in sorted(visual_snapshots):
        snapshot = visual_snapshots[page_number]
        for observation in sorted(
            snapshot.observations,
            key=lambda item: item.observation_index,
        ):
            text = _visual_observation_text(observation)
            if not text.strip():
                continue
            source_ref = StructureSourceRef(
                kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
                source_id=observation.observation_id,
            )
            if source_ref in seen_refs:
                continue
            seen_refs.add(source_ref)
            output.append((source_ref, _SourceFact(text, page_number, "VISUAL_TEXT")))
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item[1].page_number,
                _source_ref_order(source_ir, item[0]),
            ),
        )
    )


def _visual_observation_text(observation: object) -> str:
    raw_text = str(getattr(observation, "raw_text", ""))
    layout_table = getattr(observation, "layout_table", None)
    if raw_text.strip() or layout_table is None:
        return raw_text
    return "\n".join(
        " | ".join(
            str(cell.raw_text)
            for cell in layout_table.cells
            if cell.row == row and str(cell.raw_text).strip()
        )
        for row in range(layout_table.row_count)
    )


def _source_ref_order(
    source_ir: SourceIRSnapshot,
    source_ref: StructureSourceRef,
) -> tuple[int, int]:
    if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
        for node in source_ir.nodes:
            if node.source_node_id == source_ref.source_id:
                return (node.source_order, 0)
    for snapshot in source_ir.visual_text_snapshots:
        for observation in snapshot.observations:
            if observation.observation_id == source_ref.source_id:
                return (observation.observation_index, 1)
    return (2**31 - 1, 2)


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "DocumentParentBoundaryPlan",
    "DocumentParentBoundaryProviderPort",
    "DocumentParentBoundaryProviderResult",
    "DocumentParentBoundarySourceRefSpan",
    "build_document_parent_boundary_plans",
    "build_parent_boundary_target_windows",
]
