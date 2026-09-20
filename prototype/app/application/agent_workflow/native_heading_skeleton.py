"""Source-bound numbered-heading candidates for checkpointed D2 growth.

The Host may reduce a native-text document to exact heading candidates before
an Agent decides the final hierarchy.  This module never promotes a candidate
to an accepted Document Map node: uncertain geometry, numbering, or ancestry
remains explicit for the owning chapter window to review.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceIRSnapshot,
    SourceNode,
    SourceNodeKind,
)


NATIVE_HEADING_SKELETON_SCHEMA_VERSION = "native-heading-skeleton/v1"
_NUMBERED_HEADING_PATTERN = re.compile(
    r"^\s*(?P<number>[0-9]+(?:\.[0-9]+){0,5})\s+(?P<title>.{1,240}?)\s*$",
    re.DOTALL,
)
_LEADER_PATTERN = re.compile(r"(?:\.{4,}|…{2,})")
_HEADING_KINDS = frozenset({SourceNodeKind.HEADING, SourceNodeKind.PARAGRAPH})
_LEFT_ANCHOR_TOLERANCE_POINTS = 4.0
_MAX_HEADING_CANDIDATES_PER_SCOPE = 64


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeHeadingCandidateState(StrEnum):
    PROPOSED = "PROPOSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class NativeHeadingScopeKind(StrEnum):
    PREFACE = "PREFACE"
    CHAPTER = "CHAPTER"
    SECTION = "SECTION"


class NativeHeadingCandidateV1(_StrictFrozenModel):
    candidate_id: str = Field(pattern=r"^heading-candidate-[0-9a-f]{32}$")
    source_node_id: str = Field(min_length=1, max_length=160)
    source_order: StrictInt = Field(ge=0)
    page_number: StrictInt = Field(ge=1)
    bbox: tuple[float, float, float, float]
    source_text: str = Field(min_length=1, max_length=1000)
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    number: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){0,5}$")
    title: str = Field(min_length=1, max_length=240)
    level: StrictInt = Field(ge=1, le=6)
    parent_candidate_id: str | None = Field(
        default=None,
        pattern=r"^heading-candidate-[0-9a-f]{32}$",
    )
    chapter_candidate_id: str | None = Field(
        default=None,
        pattern=r"^heading-candidate-[0-9a-f]{32}$",
    )
    state: NativeHeadingCandidateState
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_candidate(self) -> NativeHeadingCandidateV1:
        x0, y0, x1, y1 = self.bbox
        if (
            any(not math.isfinite(item) for item in self.bbox)
            or x1 <= x0
            or y1 <= y0
            or hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
            != self.source_text_sha256
            or self.level != len(self.number.split("."))
            or self.reason_codes != tuple(sorted(set(self.reason_codes)))
        ):
            raise ValueError("native heading candidate drifted")
        return self


class NativeHeadingScopeV1(_StrictFrozenModel):
    scope_id: str = Field(pattern=r"^heading-scope-[0-9a-f]{32}$")
    kind: NativeHeadingScopeKind
    root_candidate_id: str | None = Field(
        default=None,
        pattern=r"^heading-candidate-[0-9a-f]{32}$",
    )
    candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=4096)
    page_numbers: tuple[StrictInt, ...] = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_scope(self) -> NativeHeadingScopeV1:
        if (
            len(self.candidate_ids) != len(set(self.candidate_ids))
            or self.page_numbers != tuple(sorted(set(self.page_numbers)))
            or (self.kind is NativeHeadingScopeKind.PREFACE)
            == (self.root_candidate_id is not None)
        ):
            raise ValueError("native heading scope drifted")
        return self


class NativeHeadingSkeletonArtifactV1(_StrictFrozenModel):
    schema_version: Literal["native-heading-skeleton/v1"] = (
        NATIVE_HEADING_SKELETON_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_adapter_id: str = Field(min_length=1, max_length=160)
    source_ir_adapter_version: str = Field(min_length=1, max_length=160)
    dominant_left_x: float
    candidates: tuple[NativeHeadingCandidateV1, ...] = Field(
        min_length=1,
        max_length=8192,
    )
    scopes: tuple[NativeHeadingScopeV1, ...] = Field(
        min_length=1,
        max_length=1024,
    )
    proposed_candidate_ids: tuple[str, ...] = Field(default=(), max_length=8192)
    review_required_candidate_ids: tuple[str, ...] = Field(
        default=(),
        max_length=8192,
    )
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> NativeHeadingSkeletonArtifactV1:
        candidate_ids = tuple(item.candidate_id for item in self.candidates)
        candidate_set = set(candidate_ids)
        scoped_ids = tuple(
            candidate_id
            for scope in self.scopes
            for candidate_id in scope.candidate_ids
        )
        if (
            not math.isfinite(self.dominant_left_x)
            or len(candidate_ids) != len(candidate_set)
            or tuple(item.source_order for item in self.candidates)
            != tuple(sorted(item.source_order for item in self.candidates))
            or len(scoped_ids) != len(set(scoped_ids))
            or set(scoped_ids) != candidate_set
            or set(self.proposed_candidate_ids).intersection(
                self.review_required_candidate_ids
            )
            or set(self.proposed_candidate_ids).union(
                self.review_required_candidate_ids
            )
            != candidate_set
        ):
            raise ValueError("native heading skeleton topology drifted")
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"artifact_sha256"})
        )
        if self.artifact_sha256 != expected:
            raise ValueError("native heading skeleton SHA drifted")
        return self


def build_native_heading_skeleton(
    source_ir: SourceIRSnapshot,
) -> NativeHeadingSkeletonArtifactV1:
    """Build exact numbered-heading candidates without deciding final structure."""

    raw_candidates = tuple(
        candidate
        for node in sorted(source_ir.nodes, key=lambda item: item.source_order)
        if (candidate := _raw_candidate(node)) is not None
    )
    if not raw_candidates:
        raise ValueError("native heading skeleton has no numbered candidates")
    dominant_left_x = _dominant_left_x(raw_candidates)
    latest_proposed_by_number: dict[str, tuple[str, str | None]] = {}
    current_chapter_id: str | None = None
    previous_top_level_number: int | None = None
    candidates: list[NativeHeadingCandidateV1] = []

    for raw in raw_candidates:
        number = raw["number"]
        level = len(number.split("."))
        reason_codes = {"native_numbered_heading_pattern"}
        geometry_supported = (
            abs(float(raw["bbox"][0]) - dominant_left_x)
            <= _LEFT_ANCHOR_TOLERANCE_POINTS
        )
        if not geometry_supported:
            reason_codes.add("non_dominant_left_geometry")
        parent_candidate_id: str | None = None
        state = NativeHeadingCandidateState.PROPOSED
        if level == 1:
            numeric = int(number)
            sequence_supported = (
                numeric == 1
                if previous_top_level_number is None
                else numeric == previous_top_level_number + 1
            )
            if not sequence_supported:
                reason_codes.add("top_level_sequence_break")
            if not geometry_supported or not sequence_supported:
                state = NativeHeadingCandidateState.REVIEW_REQUIRED
        else:
            parent_number = number.rsplit(".", 1)[0]
            parent = latest_proposed_by_number.get(parent_number)
            parent_candidate_id = None if parent is None else parent[0]
            if parent is None:
                reason_codes.add("parent_heading_not_observed")
                state = NativeHeadingCandidateState.REVIEW_REQUIRED
            elif parent[1] != current_chapter_id:
                reason_codes.add("parent_heading_outside_current_chapter")
                state = NativeHeadingCandidateState.REVIEW_REQUIRED
            if not geometry_supported:
                state = NativeHeadingCandidateState.REVIEW_REQUIRED

        candidate_id = _candidate_id(
            source_sha256=source_ir.source_sha256,
            source_node_id=str(raw["source_node_id"]),
            source_order=int(raw["source_order"]),
            source_text_sha256=str(raw["source_text_sha256"]),
        )
        if level == 1 and state is NativeHeadingCandidateState.PROPOSED:
            current_chapter_id = candidate_id
            previous_top_level_number = int(number)
        chapter_candidate_id = (
            candidate_id
            if level == 1 and state is NativeHeadingCandidateState.PROPOSED
            else current_chapter_id
        )
        candidate = NativeHeadingCandidateV1(
            candidate_id=candidate_id,
            source_node_id=str(raw["source_node_id"]),
            source_order=int(raw["source_order"]),
            page_number=int(raw["page_number"]),
            bbox=tuple(float(item) for item in raw["bbox"]),
            source_text=str(raw["source_text"]),
            source_text_sha256=str(raw["source_text_sha256"]),
            number=number,
            title=str(raw["title"]),
            level=level,
            parent_candidate_id=parent_candidate_id,
            chapter_candidate_id=chapter_candidate_id,
            state=state,
            reason_codes=tuple(sorted(reason_codes)),
        )
        candidates.append(candidate)
        if state is NativeHeadingCandidateState.PROPOSED:
            latest_proposed_by_number[number] = (
                candidate_id,
                chapter_candidate_id,
            )

    scopes = _build_scopes(tuple(candidates), source_sha256=source_ir.source_sha256)
    proposed_ids = tuple(
        item.candidate_id
        for item in candidates
        if item.state is NativeHeadingCandidateState.PROPOSED
    )
    review_ids = tuple(
        item.candidate_id
        for item in candidates
        if item.state is NativeHeadingCandidateState.REVIEW_REQUIRED
    )
    values = {
        "source_sha256": source_ir.source_sha256,
        "source_ir_id": source_ir.source_ir_id,
        "source_ir_adapter_id": source_ir.adapter_id,
        "source_ir_adapter_version": source_ir.adapter_version,
        "dominant_left_x": dominant_left_x,
        "candidates": tuple(candidates),
        "scopes": scopes,
        "proposed_candidate_ids": proposed_ids,
        "review_required_candidate_ids": review_ids,
    }
    draft = NativeHeadingSkeletonArtifactV1.model_construct(
        **values,
        artifact_sha256="0" * 64,
    )
    return NativeHeadingSkeletonArtifactV1(
        **values,
        artifact_sha256=_canonical_sha256(
            draft.model_dump(mode="json", exclude={"artifact_sha256"})
        ),
    )


def _raw_candidate(node: SourceNode) -> dict[str, object] | None:
    if (
        node.kind not in _HEADING_KINDS
        or node.native_locator.format is not NativeDocumentFormat.PDF
        or node.native_locator.page_number is None
        or node.native_locator.bbox is None
    ):
        return None
    source_text = node.raw_text.strip()
    match = _NUMBERED_HEADING_PATTERN.fullmatch(source_text)
    if match is None:
        return None
    title = re.sub(r"\s+", " ", match.group("title")).strip()
    if not title or len(title) > 240 or _LEADER_PATTERN.search(title):
        return None
    return {
        "source_node_id": node.source_node_id,
        "source_order": node.source_order,
        "page_number": node.native_locator.page_number,
        "bbox": node.native_locator.bbox,
        "source_text": source_text,
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "number": match.group("number"),
        "title": title,
    }


def _dominant_left_x(candidates: tuple[dict[str, object], ...]) -> float:
    strong = tuple(
        item
        for item in candidates
        if "." in str(item["number"])
    ) or candidates
    bins = Counter(round(float(item["bbox"][0]) * 2) / 2 for item in strong)
    highest = max(bins.values())
    return min(value for value, count in bins.items() if count == highest)


def _build_scopes(
    candidates: tuple[NativeHeadingCandidateV1, ...],
    *,
    source_sha256: str,
) -> tuple[NativeHeadingScopeV1, ...]:
    grouped: dict[str | None, list[NativeHeadingCandidateV1]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.chapter_candidate_id, []).append(candidate)
    ordered_keys = sorted(
        grouped,
        key=lambda key: min(item.source_order for item in grouped[key]),
    )
    scope_groups: list[
        tuple[NativeHeadingScopeKind, str | None, list[NativeHeadingCandidateV1]]
    ] = []
    for chapter_id in ordered_keys:
        items = grouped[chapter_id]
        if chapter_id is None:
            scope_groups.append((NativeHeadingScopeKind.PREFACE, None, items))
            continue
        if len(items) <= _MAX_HEADING_CANDIDATES_PER_SCOPE:
            scope_groups.append(
                (NativeHeadingScopeKind.CHAPTER, chapter_id, items)
            )
            continue
        chapter_root = next(
            item for item in items if item.candidate_id == chapter_id
        )
        current_kind = NativeHeadingScopeKind.CHAPTER
        current_root = chapter_id
        current_items: list[NativeHeadingCandidateV1] = []
        for item in items:
            if (
                item.level == 2
                and item.state is NativeHeadingCandidateState.PROPOSED
                and item.candidate_id != chapter_root.candidate_id
            ):
                if current_items:
                    scope_groups.append((current_kind, current_root, current_items))
                current_kind = NativeHeadingScopeKind.SECTION
                current_root = item.candidate_id
                current_items = []
            current_items.append(item)
        if current_items:
            scope_groups.append((current_kind, current_root, current_items))

    scopes: list[NativeHeadingScopeV1] = []
    for kind, root_candidate_id, items in scope_groups:
        payload = {
            "source_sha256": source_sha256,
            "kind": kind.value,
            "root_candidate_id": root_candidate_id,
            "candidate_ids": [item.candidate_id for item in items],
        }
        scopes.append(
            NativeHeadingScopeV1(
                scope_id="heading-scope-" + _canonical_sha256(payload)[:32],
                kind=kind,
                root_candidate_id=root_candidate_id,
                candidate_ids=tuple(item.candidate_id for item in items),
                page_numbers=tuple(sorted({item.page_number for item in items})),
            )
        )
    return tuple(scopes)


def _candidate_id(
    *,
    source_sha256: str,
    source_node_id: str,
    source_order: int,
    source_text_sha256: str,
) -> str:
    return "heading-candidate-" + _canonical_sha256(
        {
            "source_sha256": source_sha256,
            "source_node_id": source_node_id,
            "source_order": source_order,
            "source_text_sha256": source_text_sha256,
        }
    )[:32]


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "NATIVE_HEADING_SKELETON_SCHEMA_VERSION",
    "NativeHeadingCandidateState",
    "NativeHeadingCandidateV1",
    "NativeHeadingScopeKind",
    "NativeHeadingScopeV1",
    "NativeHeadingSkeletonArtifactV1",
    "build_native_heading_skeleton",
]
