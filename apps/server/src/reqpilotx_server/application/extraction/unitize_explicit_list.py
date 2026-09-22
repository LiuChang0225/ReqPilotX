from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from reqpilotx_server.domain.extraction.semantic_units import (
    SemanticUnit,
    SemanticUnitKind,
    SemanticUnitMarkerFamily,
    SemanticUnitPartitionCandidate,
    SemanticUnitPartitionDisposition,
    SemanticUnitPartitionReason,
)
from reqpilotx_server.domain.extraction.semantics import (
    SemanticGroundingSpan,
    stable_semantic_grounding_span_id,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot, SourceNodeKind
from reqpilotx_server.domain.extraction.structure import (
    StructureNodeKind,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)

if TYPE_CHECKING:
    from reqpilotx_server.application.extraction.classify_content import SemanticTextResolver


CONTINUOUS_EXPLICIT_LIST_CANDIDATE_UNITIZER_VERSION = (
    "provider-neutral-continuous-explicit-list-candidate/v1"
)

_ALPHA_MARKER_PATTERN = re.compile(
    r"^[ \t]*(?P<marker>(?:[（(]?[A-Za-zＡ-Ｚａ-ｚ][)）]))(?P<rest>.*)$"
)
_NUMERIC_MARKER_PATTERN = re.compile(
    r"^[ \t]*(?P<marker>(?:[（(]?[0-9０-９]+[)）]))(?P<rest>.*)$"
)
_FORMULA_PREFIX_CHARACTERS = frozenset("=+-*/×÷^<>≤≥")
_ELIGIBLE_NATIVE_TEXT_KINDS = frozenset(
    {
        SourceNodeKind.PARAGRAPH,
        SourceNodeKind.LIST_ITEM,
    }
)
_TABLE_SOURCE_KINDS = frozenset(
    {
        SourceNodeKind.TABLE,
        SourceNodeKind.TABLE_ROW,
        SourceNodeKind.CELL,
    }
)


class _ObservedMarkerFamily(str, Enum):
    LATIN_ALPHA = "LATIN_ALPHA"
    ARABIC_NUMERIC = "ARABIC_NUMERIC"


@dataclass(frozen=True, slots=True)
class _MarkerLine:
    line_index: int
    line_start: int
    family: _ObservedMarkerFamily
    ordinal: int
    body: str


@dataclass(frozen=True, slots=True)
class _PartitionAnalysis:
    disposition: SemanticUnitPartitionDisposition
    reason_code: SemanticUnitPartitionReason
    marker_family: SemanticUnitMarkerFamily | None = None
    local_ranges: tuple[tuple[int, int], ...] = ()


def propose_continuous_explicit_list_partition(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    unit: SemanticUnit,
    *,
    text_resolver: SemanticTextResolver | None = None,
) -> SemanticUnitPartitionCandidate:
    """Propose, but do not activate, a conservative explicit-list partition.

    The v1 candidate policy accepts only one native, non-table ``CONTENT``
    owner whose complete text view consists of at least three consecutive
    Latin-letter marker lines. Recognition may use NFKC/case folding, while
    every returned offset remains a Unicode code-point offset into the exact
    original text. Production ``SemanticUnitPlan`` construction does not call
    this function.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not isinstance(tree, StructureTreeSnapshot):
        raise TypeError("tree must be a StructureTreeSnapshot")
    if not isinstance(unit, SemanticUnit):
        raise TypeError("unit must be a SemanticUnit")
    if text_resolver is not None and not callable(text_resolver):
        raise TypeError("text_resolver must be callable")
    if tree.source_ir_id != source_ir.source_ir_id:
        raise ValueError("StructureTree must be bound to the supplied SourceIR")
    if unit.unit_kind is not SemanticUnitKind.OWNER_FULL_SPAN:
        raise ValueError("candidate analysis requires a full-span owner unit")

    owner = next(
        (
            item
            for item in tree.nodes
            if item.structure_node_id == unit.owner_structure_node_id
        ),
        None,
    )
    if owner is None:
        raise ValueError("semantic unit owner is absent from StructureTree")
    if (
        owner.source_order != unit.source_order
        or owner.parent_structure_node_id != unit.parent_owner_structure_node_id
    ):
        raise ValueError(
            "semantic unit owner order or parent does not match StructureTree"
        )

    if owner.kind is not StructureNodeKind.CONTENT:
        return _candidate_result(
            unit,
            disposition=SemanticUnitPartitionDisposition.NO_OP,
            reason_code=SemanticUnitPartitionReason.OWNER_NOT_CONTENT,
        )
    owner_source_refs = owner.effective_semantic_source_refs
    if len(owner_source_refs) != 1 or len(unit.source_spans) != 1:
        return _candidate_result(
            unit,
            disposition=SemanticUnitPartitionDisposition.BLOCKED,
            reason_code=SemanticUnitPartitionReason.CROSS_SOURCE_REF_OWNER,
        )

    owner_span = unit.source_spans[0]
    source_ref = owner_source_refs[0]
    if owner_span.source_ref != source_ref:
        return _candidate_result(
            unit,
            disposition=SemanticUnitPartitionDisposition.BLOCKED,
            reason_code=SemanticUnitPartitionReason.OWNER_TEXT_VIEW_MISMATCH,
        )
    if source_ref.kind is StructureSourceRefKind.TABLE_CELL:
        return _candidate_result(
            unit,
            disposition=SemanticUnitPartitionDisposition.NO_OP,
            reason_code=SemanticUnitPartitionReason.TABLE_BOUND_OWNER,
        )
    if source_ref.kind is not StructureSourceRefKind.SOURCE_NODE:
        return _candidate_result(
            unit,
            disposition=SemanticUnitPartitionDisposition.NO_OP,
            reason_code=SemanticUnitPartitionReason.OWNER_NOT_SINGLE_NATIVE_TEXT,
        )

    source_node = next(
        (
            item
            for item in source_ir.nodes
            if item.source_node_id == source_ref.source_id
        ),
        None,
    )
    if source_node is None:
        return _candidate_result(
            unit,
            disposition=SemanticUnitPartitionDisposition.BLOCKED,
            reason_code=SemanticUnitPartitionReason.OWNER_TEXT_VIEW_MISMATCH,
        )
    if source_node.kind in _TABLE_SOURCE_KINDS:
        return _candidate_result(
            unit,
            disposition=SemanticUnitPartitionDisposition.NO_OP,
            reason_code=SemanticUnitPartitionReason.TABLE_BOUND_OWNER,
        )
    if source_node.kind not in _ELIGIBLE_NATIVE_TEXT_KINDS:
        return _candidate_result(
            unit,
            disposition=SemanticUnitPartitionDisposition.NO_OP,
            reason_code=SemanticUnitPartitionReason.OWNER_NOT_SINGLE_NATIVE_TEXT,
        )

    native_text = source_node.raw_text
    text = native_text
    if text_resolver is not None:
        try:
            override = text_resolver(source_ref, source_ir)
        except Exception:
            return _candidate_result(
                unit,
                disposition=SemanticUnitPartitionDisposition.BLOCKED,
                reason_code=SemanticUnitPartitionReason.TEXT_RESOLUTION_FAILED,
            )
        if override is not None:
            if not isinstance(override, str):
                return _candidate_result(
                    unit,
                    disposition=SemanticUnitPartitionDisposition.BLOCKED,
                    reason_code=SemanticUnitPartitionReason.TEXT_RESOLUTION_FAILED,
                )
            text = override
            if text != native_text:
                return _candidate_result(
                    unit,
                    disposition=SemanticUnitPartitionDisposition.BLOCKED,
                    reason_code=SemanticUnitPartitionReason.OWNER_TEXT_VIEW_MISMATCH,
                )

    if owner_span.start_offset != 0 or owner_span.end_offset != len(text):
        return _candidate_result(
            unit,
            disposition=SemanticUnitPartitionDisposition.BLOCKED,
            reason_code=SemanticUnitPartitionReason.OWNER_TEXT_VIEW_MISMATCH,
        )

    analysis = _analyze_text(text)
    proposed_spans = tuple(
        SemanticGroundingSpan(
            grounding_span_id=stable_semantic_grounding_span_id(
                source_ref=source_ref,
                start_offset=owner_span.start_offset + start,
                end_offset=owner_span.start_offset + end,
            ),
            source_ref=source_ref,
            start_offset=owner_span.start_offset + start,
            end_offset=owner_span.start_offset + end,
        )
        for start, end in analysis.local_ranges
    )
    return _candidate_result(
        unit,
        disposition=analysis.disposition,
        reason_code=analysis.reason_code,
        marker_family=analysis.marker_family,
        proposed_unit_spans=proposed_spans,
    )


def _analyze_text(text: str) -> _PartitionAnalysis:
    lines = text.splitlines(keepends=True)
    if not lines and text:
        lines = [text]

    marker_by_line: dict[int, _MarkerLine] = {}
    line_texts: list[str] = []
    offset = 0
    for line_index, line in enumerate(lines):
        line_text = _without_line_ending(line)
        line_texts.append(line_text)
        marker = _parse_marker_line(
            line_text,
            line_index=line_index,
            line_start=offset,
        )
        if marker is not None:
            marker_by_line[line_index] = marker
        offset += len(line)

    markers = tuple(marker_by_line[index] for index in sorted(marker_by_line))
    if not markers:
        return _PartitionAnalysis(
            disposition=SemanticUnitPartitionDisposition.NO_OP,
            reason_code=SemanticUnitPartitionReason.NO_HARD_LINE_MARKER,
        )

    families = {item.family for item in markers}
    if len(families) != 1:
        return _PartitionAnalysis(
            disposition=SemanticUnitPartitionDisposition.BLOCKED,
            reason_code=SemanticUnitPartitionReason.MIXED_MARKER_FAMILY,
        )
    family = next(iter(families))
    if family is not _ObservedMarkerFamily.LATIN_ALPHA:
        return _PartitionAnalysis(
            disposition=SemanticUnitPartitionDisposition.NO_OP,
            reason_code=SemanticUnitPartitionReason.UNSUPPORTED_MARKER_FAMILY,
        )
    if any(not _has_substantive_body(item.body) for item in markers):
        return _PartitionAnalysis(
            disposition=SemanticUnitPartitionDisposition.BLOCKED,
            reason_code=SemanticUnitPartitionReason.EMPTY_ITEM_BODY,
        )
    if len(markers) < 3:
        return _PartitionAnalysis(
            disposition=SemanticUnitPartitionDisposition.NO_OP,
            reason_code=SemanticUnitPartitionReason.INSUFFICIENT_CONSECUTIVE_ITEMS,
        )
    if any(_looks_formula_like(item.body) for item in markers):
        return _PartitionAnalysis(
            disposition=SemanticUnitPartitionDisposition.BLOCKED,
            reason_code=SemanticUnitPartitionReason.FORMULA_LIKE_ITEM,
        )

    first_line = markers[0].line_index
    last_line = markers[-1].line_index
    if any(
        line_texts[index].strip() and index not in marker_by_line
        for index in range(len(line_texts))
    ) or any(index not in marker_by_line for index in range(first_line, last_line + 1)):
        return _PartitionAnalysis(
            disposition=SemanticUnitPartitionDisposition.BLOCKED,
            reason_code=SemanticUnitPartitionReason.NON_LIST_LINE,
        )

    ordinals = tuple(item.ordinal for item in markers)
    if ordinals != tuple(range(ordinals[0], ordinals[0] + len(ordinals))):
        return _PartitionAnalysis(
            disposition=SemanticUnitPartitionDisposition.BLOCKED,
            reason_code=SemanticUnitPartitionReason.NON_CONSECUTIVE_MARKERS,
        )

    starts = (0, *(item.line_start for item in markers[1:]))
    ends = (*starts[1:], len(text))
    return _PartitionAnalysis(
        disposition=SemanticUnitPartitionDisposition.CANDIDATE,
        reason_code=SemanticUnitPartitionReason.SAFE_CONTINUOUS_EXPLICIT_LIST,
        marker_family=SemanticUnitMarkerFamily.LATIN_ALPHA,
        local_ranges=tuple(zip(starts, ends, strict=True)),
    )


def _parse_marker_line(
    line: str,
    *,
    line_index: int,
    line_start: int,
) -> _MarkerLine | None:
    for pattern, family in (
        (_ALPHA_MARKER_PATTERN, _ObservedMarkerFamily.LATIN_ALPHA),
        (_NUMERIC_MARKER_PATTERN, _ObservedMarkerFamily.ARABIC_NUMERIC),
    ):
        matched = pattern.fullmatch(line)
        if matched is None:
            continue
        rest = matched.group("rest")
        if rest and rest[0] not in {" ", "\t"}:
            return None
        marker = unicodedata.normalize("NFKC", matched.group("marker")).casefold()
        token = marker.removeprefix("(").removesuffix(")")
        ordinal = (
            ord(token) - ord("a") + 1
            if family is _ObservedMarkerFamily.LATIN_ALPHA
            else int(token)
        )
        return _MarkerLine(
            line_index=line_index,
            line_start=line_start,
            family=family,
            ordinal=ordinal,
            body=rest.strip(),
        )
    return None


def _without_line_ending(line: str) -> str:
    return line.rstrip("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _has_substantive_body(body: str) -> bool:
    return any(character.isalnum() for character in body)


def _looks_formula_like(body: str) -> bool:
    stripped = body.lstrip()
    return bool(stripped) and stripped[0] in _FORMULA_PREFIX_CHARACTERS


def _candidate_result(
    unit: SemanticUnit,
    *,
    disposition: SemanticUnitPartitionDisposition,
    reason_code: SemanticUnitPartitionReason,
    marker_family: SemanticUnitMarkerFamily | None = None,
    proposed_unit_spans: tuple[SemanticGroundingSpan, ...] = (),
) -> SemanticUnitPartitionCandidate:
    return SemanticUnitPartitionCandidate(
        candidate_unitizer_version=(
            CONTINUOUS_EXPLICIT_LIST_CANDIDATE_UNITIZER_VERSION
        ),
        owner_semantic_unit_id=unit.semantic_unit_id,
        owner_structure_node_id=unit.owner_structure_node_id,
        parent_owner_structure_node_id=unit.parent_owner_structure_node_id,
        parent_semantic_unit_id=unit.parent_semantic_unit_id,
        disposition=disposition,
        reason_code=reason_code,
        marker_family=marker_family,
        owner_source_spans=unit.source_spans,
        proposed_unit_spans=proposed_unit_spans,
    )


__all__ = [
    "CONTINUOUS_EXPLICIT_LIST_CANDIDATE_UNITIZER_VERSION",
    "propose_continuous_explicit_list_partition",
]
