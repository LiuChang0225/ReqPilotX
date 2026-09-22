"""Honest effective-source context assembly for the single-document reader."""

from __future__ import annotations

import os
from dataclasses import dataclass

from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot, SourceNode, SourceNodeKind
from reqpilotx_server.domain.reading import ReadingRetrievalV1, ReadingSourceRefV1
from reqpilotx_server.domain.source_visual_capture import (
    CaptureQualityManifestV1,
    CaptureQualityStatus,
    PageReadableMode,
    VisualPageCaptureV1,
    VisualTextRunV1,
)
from reqpilotx_server.ports.reading_provider import (
    MAX_READING_CHAT_CONTEXT_CHARACTERS,
    MAX_READING_CHAT_CONTEXT_CHUNKS,
    ReadingChatContextChunk,
)


_CHUNK_CHARACTER_LIMIT = 1_600
_MAX_SELECTED_SOURCE_CHARACTERS = 12_000
_DEFAULT_DIRECT_INPUT_TOKEN_BUDGET = 850_000
_DIRECT_INPUT_BUDGET_ENV = "REQPILOT_READING_DIRECT_INPUT_TOKEN_BUDGET"
_CONTEXT_METADATA_TOKEN_RESERVE_PER_CHUNK = 96
_REQUEST_TOKEN_RESERVE = 64_000
_READABLE_TEXT_KINDS = frozenset(
    {
        SourceNodeKind.HEADING,
        SourceNodeKind.PARAGRAPH,
        SourceNodeKind.LIST,
        SourceNodeKind.LIST_ITEM,
        SourceNodeKind.NOTE,
        SourceNodeKind.TOC_ENTRY,
        SourceNodeKind.ATTACHMENT,
    }
)


class FullDocumentTooLarge(ValueError):
    """The current effective document cannot fit one honest full request."""


class EffectiveSourceIncomplete(ValueError):
    """Full-document reading is forbidden while effective pages are unreadable."""


@dataclass(frozen=True, slots=True)
class RetrievedReadingContext:
    chunks: tuple[ReadingChatContextChunk, ...]
    retrieval: ReadingRetrievalV1


@dataclass(frozen=True, slots=True)
class _CandidateChunk:
    source_node_id: str | None
    source_order: int
    page_number: int | None
    start_offset: int
    end_offset: int
    text: str
    locator_kind: str = "SOURCE_TEXT"
    cell_id: str | None = None
    cell_order: int = -1
    table_row: int | None = None
    table_column: int | None = None
    table_row_span: int | None = None
    table_column_span: int | None = None
    visual_snapshot_id: str | None = None
    observation_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    page_bbox: tuple[float, float, float, float] | None = None
    page_image_sha256: str | None = None
    visual_provider_id: str | None = None
    visual_provider_version: str | None = None

    @property
    def fact_id(self) -> str:
        return self.source_node_id or self.observation_id or ""


def assemble_reading_context(
    source_ir: SourceIRSnapshot,
    *,
    question: str,
    recent_user_text: str = "",
    source_ref: ReadingSourceRefV1 | None = None,
    source_refs: tuple[ReadingSourceRefV1, ...] = (),
    coverage_intent: str = "AUTO",
    direct_input_token_budget: int | None = None,
    capture_quality: CaptureQualityManifestV1 | None = None,
    visual_pages: tuple[VisualPageCaptureV1, ...] = (),
) -> RetrievedReadingContext:
    """Assemble full effective facts or one explicit selected-context receipt."""

    del recent_user_text
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not str(question or "").strip():
        raise ValueError("question must not be blank")
    if coverage_intent not in {"AUTO", "DOCUMENT_OVERVIEW"}:
        raise ValueError("unsupported reading coverage intent")
    refs = source_refs or ((source_ref,) if source_ref is not None else ())
    if source_ref is not None and refs and refs[0] != source_ref:
        raise ValueError("source_ref must match the first source_refs item")
    if len(refs) > 16:
        raise ValueError("too many selected source ranges")
    _validate_effective_scope(source_ir, capture_quality, visual_pages)
    selected_exact = _chunks_for_source_refs(
        source_ir,
        refs,
        visual_pages,
        capture_quality=capture_quality,
    )
    incomplete = (
        capture_quality is not None
        and capture_quality.status is not CaptureQualityStatus.READY
    )
    if incomplete and not refs:
        raise EffectiveSourceIncomplete(
            "effective source has unreadable pages and no verified selection"
        )
    if incomplete:
        chosen = _deduplicate(selected_exact)
        if not chosen:
            raise ValueError("selected source does not contain readable text")
        coverage_mode = "SELECTED_CONTEXT"
        strategy = "effective-source-selected-context/v1"
        document_candidates = _full_document_candidates(
            source_ir,
            capture_quality=capture_quality,
            visual_pages=visual_pages,
        )
    else:
        full = _full_document_candidates(
            source_ir,
            capture_quality=capture_quality,
            visual_pages=visual_pages,
        )
        chosen = _deduplicate((*selected_exact, *full))
        has_visual_pages = bool(
            capture_quality is not None
            and any(
                item.readable_mode is PageReadableMode.VISUAL
                for item in capture_quality.pages
            )
        )
        if has_visual_pages:
            coverage_mode = "FULL_EFFECTIVE_TEXT"
            strategy = "effective-source-full-effective-text/v1"
        else:
            coverage_mode = "FULL_DOCUMENT"
            strategy = (
                "effective-source-full-document/v1"
                if capture_quality is not None
                else "source-ir-full-document/v2"
            )
        document_candidates = full
    _enforce_context_budget(chosen, direct_input_token_budget)
    provider_chunks = tuple(
        ReadingChatContextChunk(
            citation_id=f"S{index}",
            source_node_id=item.source_node_id,
            page_number=item.page_number,
            start_offset=item.start_offset,
            end_offset=item.end_offset,
            text=item.text,
            locator_kind=item.locator_kind,
            cell_id=item.cell_id,
            table_row=item.table_row,
            table_column=item.table_column,
            table_row_span=item.table_row_span,
            table_column_span=item.table_column_span,
            visual_snapshot_id=item.visual_snapshot_id,
            observation_id=item.observation_id,
            bbox=item.bbox,
            page_bbox=item.page_bbox,
            page_image_sha256=item.page_image_sha256,
            visual_provider_id=item.visual_provider_id,
            visual_provider_version=item.visual_provider_version,
        )
        for index, item in enumerate(chosen, start=1)
    )
    unreadable_pages = (
        tuple(
            item.page_number
            for item in capture_quality.pages
            if item.readable_mode is PageReadableMode.NONE
        )
        if capture_quality is not None
        else ()
    )
    return RetrievedReadingContext(
        chunks=provider_chunks,
        retrieval=ReadingRetrievalV1(
            strategy=strategy,
            coverage_mode=coverage_mode,
            selected_node_count=len({item.fact_id for item in chosen}),
            selected_character_count=sum(len(item.text) for item in chosen),
            document_node_count=len(
                {item.fact_id for item in document_candidates if item.fact_id}
            ),
            covered_page_numbers=(
                tuple(range(1, capture_quality.page_count + 1))
                if capture_quality is not None and not incomplete
                else tuple(
                    sorted(
                        {
                            item.page_number
                            for item in chosen
                            if item.page_number is not None
                        }
                    )
                )
            ),
            effective_bundle_id=(
                capture_quality.effective_bundle_id
                if capture_quality is not None
                else None
            ),
            visual_generation_id=(
                capture_quality.active_visual_generation_id
                if capture_quality is not None
                else None
            ),
            capture_quality_status=(
                capture_quality.status.value if capture_quality is not None else None
            ),
            unreadable_page_numbers=unreadable_pages,
        ),
    )


def _full_document_candidates(
    source_ir: SourceIRSnapshot,
    *,
    capture_quality: CaptureQualityManifestV1 | None,
    visual_pages: tuple[VisualPageCaptureV1, ...],
) -> tuple[_CandidateChunk, ...]:
    page_modes = (
        {item.page_number: item.readable_mode for item in capture_quality.pages}
        if capture_quality is not None
        else {}
    )
    candidates: list[_CandidateChunk] = []
    for node in source_ir.nodes:
        page_number = node.native_locator.page_number
        if page_number is not None and page_modes.get(
            page_number, PageReadableMode.NATIVE
        ) is not PageReadableMode.NATIVE:
            continue
        if node.kind in _READABLE_TEXT_KINDS and node.raw_text.strip():
            candidates.extend(_split_source_text(node, 0, len(node.raw_text)))
        if node.kind is SourceNodeKind.TABLE and node.table_payload is not None:
            for cell_order, cell in enumerate(
                sorted(node.table_payload.cells, key=lambda item: (item.row, item.column))
            ):
                if cell.raw_text.strip():
                    candidates.extend(
                        _split_table_cell(
                            node,
                            cell.cell_id,
                            cell.raw_text,
                            cell_order,
                            table_row=cell.row,
                            table_column=cell.column,
                            table_row_span=cell.row_span,
                            table_column_span=cell.column_span,
                        )
                    )
    if capture_quality is not None:
        visible_pages = {
            item.page_number
            for item in capture_quality.pages
            if item.readable_mode is PageReadableMode.VISUAL
        }
        first_order_by_page = _first_source_order_by_page(source_ir)
        for page in sorted(visual_pages, key=lambda item: item.page_number):
            if page.page_number not in visible_pages:
                continue
            base_order = first_order_by_page.get(page.page_number, page.page_number * 1_000_000)
            for run_index, run in enumerate(page.runs):
                candidates.extend(
                    _split_visual_text(
                        page,
                        run,
                        0,
                        len(run.text),
                        source_order=base_order + run_index,
                    )
                )
    return tuple(sorted(candidates, key=_candidate_sort_key))


def _chunks_for_source_refs(
    source_ir: SourceIRSnapshot,
    refs: tuple[ReadingSourceRefV1, ...],
    visual_pages: tuple[VisualPageCaptureV1, ...],
    *,
    capture_quality: CaptureQualityManifestV1 | None,
) -> tuple[_CandidateChunk, ...]:
    nodes = {item.source_node_id: item for item in source_ir.nodes}
    visual_runs = {
        (page.visual_snapshot_id, run.observation_id): (page, run)
        for page in visual_pages
        for run in page.runs
    }
    chunks: list[_CandidateChunk] = []
    page_modes = (
        {item.page_number: item.readable_mode for item in capture_quality.pages}
        if capture_quality is not None
        else {}
    )
    total = 0
    for ref in refs:
        if ref.kind == "SOURCE_TEXT":
            node = nodes.get(ref.source_node_id or "")
            if node is None or node.kind not in _READABLE_TEXT_KINDS or not node.raw_text:
                raise ValueError("source reference does not identify current readable text")
            page_number = node.native_locator.page_number
            if page_number is not None and page_modes.get(
                page_number, PageReadableMode.NATIVE
            ) is not PageReadableMode.NATIVE:
                raise ValueError("native source reference is not effective on this page")
            text_length = len(node.raw_text)
            selected_length = ref.end_offset - ref.start_offset
            _validate_ref_offsets(ref.start_offset, ref.end_offset, text_length)
            chunks.extend(_split_source_text(node, ref.start_offset, ref.end_offset))
        else:
            found = visual_runs.get(
                (ref.visual_snapshot_id or "", ref.observation_id or "")
            )
            if found is None:
                raise ValueError("visual source reference is not in the active capture")
            page, run = found
            if page_modes.get(page.page_number) is not PageReadableMode.VISUAL:
                raise ValueError("visual source reference is not effective on this page")
            if (
                ref.page_number != page.page_number
                or ref.bbox != run.bbox
                or ref.page_bbox != page.page_bbox
                or ref.page_image_sha256 != page.page_image_sha256
                or ref.provider_id != page.provider_id
                or ref.provider_version != page.provider_version
            ):
                raise ValueError("visual source provenance does not match the active capture")
            text_length = len(run.text)
            selected_length = ref.end_offset - ref.start_offset
            _validate_ref_offsets(ref.start_offset, ref.end_offset, text_length)
            chunks.extend(
                _split_visual_text(
                    page,
                    run,
                    ref.start_offset,
                    ref.end_offset,
                    source_order=page.page_number * 1_000_000 + run.reading_order,
                )
            )
        total += selected_length
        if total > _MAX_SELECTED_SOURCE_CHARACTERS:
            raise ValueError("selected source exceeds the conversational reading limit")
    return tuple(chunks)


def _split_source_text(node: SourceNode, start: int, end: int) -> list[_CandidateChunk]:
    return _split_native_text(
        node=node,
        text=node.raw_text,
        start=start,
        end=end,
        locator_kind="SOURCE_TEXT",
        cell_id=None,
        cell_order=-1,
    )


def _split_table_cell(
    node: SourceNode,
    cell_id: str,
    text: str,
    cell_order: int,
    *,
    table_row: int,
    table_column: int,
    table_row_span: int,
    table_column_span: int,
) -> list[_CandidateChunk]:
    return _split_native_text(
        node=node,
        text=text,
        start=0,
        end=len(text),
        locator_kind="TABLE_CELL",
        cell_id=cell_id,
        cell_order=cell_order,
        table_row=table_row,
        table_column=table_column,
        table_row_span=table_row_span,
        table_column_span=table_column_span,
    )


def _split_native_text(
    *,
    node: SourceNode,
    text: str,
    start: int,
    end: int,
    locator_kind: str,
    cell_id: str | None,
    cell_order: int,
    table_row: int | None = None,
    table_column: int | None = None,
    table_row_span: int | None = None,
    table_column_span: int | None = None,
) -> list[_CandidateChunk]:
    return [
        _CandidateChunk(
            source_node_id=node.source_node_id,
            source_order=node.source_order,
            page_number=node.native_locator.page_number,
            start_offset=left,
            end_offset=right,
            text=text[left:right],
            locator_kind=locator_kind,
            cell_id=cell_id,
            cell_order=cell_order,
            table_row=table_row,
            table_column=table_column,
            table_row_span=table_row_span,
            table_column_span=table_column_span,
        )
        for left, right in _text_ranges(text, start, end)
    ]


def _split_visual_text(
    page: VisualPageCaptureV1,
    run: VisualTextRunV1,
    start: int,
    end: int,
    *,
    source_order: int,
) -> list[_CandidateChunk]:
    return [
        _CandidateChunk(
            source_node_id=None,
            source_order=source_order,
            page_number=page.page_number,
            start_offset=left,
            end_offset=right,
            text=run.text[left:right],
            locator_kind="VISUAL_TEXT",
            visual_snapshot_id=page.visual_snapshot_id,
            observation_id=run.observation_id,
            bbox=run.bbox,
            page_bbox=page.page_bbox,
            page_image_sha256=page.page_image_sha256,
            visual_provider_id=page.provider_id,
            visual_provider_version=page.provider_version,
        )
        for left, right in _text_ranges(run.text, start, end)
    ]


def _text_ranges(text: str, start: int, end: int) -> list[tuple[int, int]]:
    chunks: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        raw_end = min(end, cursor + _CHUNK_CHARACTER_LIMIT)
        split_end = raw_end
        if raw_end < end:
            window = text[cursor:raw_end]
            split_at = max(window.rfind("\n"), window.rfind("。"), window.rfind(" "))
            if split_at >= _CHUNK_CHARACTER_LIMIT // 2:
                split_end = cursor + split_at + 1
        left, right = cursor, split_end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if right > left:
            chunks.append((left, right))
        cursor = max(split_end, cursor + 1)
    return chunks


def _deduplicate(items: tuple[_CandidateChunk, ...]) -> tuple[_CandidateChunk, ...]:
    result: list[_CandidateChunk] = []
    seen: set[tuple[object, ...]] = set()
    for item in items:
        identity = (
            item.locator_kind,
            item.source_node_id,
            item.cell_id,
            item.visual_snapshot_id,
            item.observation_id,
            item.start_offset,
            item.end_offset,
        )
        if identity not in seen:
            result.append(item)
            seen.add(identity)
    return tuple(result)


def _candidate_sort_key(item: _CandidateChunk) -> tuple[object, ...]:
    return (
        item.page_number if item.page_number is not None else 0,
        item.source_order,
        0 if item.locator_kind == "SOURCE_TEXT" else 1,
        item.cell_order,
        item.start_offset,
    )


def _first_source_order_by_page(source_ir: SourceIRSnapshot) -> dict[int, int]:
    result: dict[int, int] = {}
    for node in source_ir.nodes:
        page = node.native_locator.page_number
        if page is not None:
            result[page] = min(result.get(page, node.source_order), node.source_order)
    return result


def _validate_effective_scope(
    source_ir: SourceIRSnapshot,
    manifest: CaptureQualityManifestV1 | None,
    visual_pages: tuple[VisualPageCaptureV1, ...],
) -> None:
    if manifest is None:
        if visual_pages:
            raise ValueError("visual pages require a capture quality manifest")
        return
    if (
        manifest.source_sha256 != source_ir.source_sha256
        or manifest.base_source_ir_id != source_ir.source_ir_id
    ):
        raise ValueError("capture quality manifest does not bind this SourceIR")
    if visual_pages and manifest.active_visual_generation_id is None:
        raise ValueError("visual pages require an active generation")
    if any(
        page.generation_id != manifest.active_visual_generation_id
        or page.source_sha256 != source_ir.source_sha256
        for page in visual_pages
    ):
        raise ValueError("visual pages do not bind the active effective source")
    pages_by_number = {page.page_number: page for page in visual_pages}
    if len(pages_by_number) != len(visual_pages):
        raise ValueError("active visual pages must be unique by page number")
    visual_quality_pages = {
        item.page_number: item
        for item in manifest.pages
        if item.readable_mode is PageReadableMode.VISUAL
    }
    if set(pages_by_number) != set(visual_quality_pages):
        raise ValueError("active visual pages do not match manifest arbitration")
    for page_number, page in pages_by_number.items():
        quality = visual_quality_pages[page_number]
        if (
            quality.visual_snapshot_id != page.visual_snapshot_id
            or quality.page_bbox != page.page_bbox
            or quality.page_image_sha256 != page.page_image_sha256
            or quality.provider_id != page.provider_id
            or quality.provider_version != page.provider_version
        ):
            raise ValueError("visual page provenance does not match quality manifest")


def _validate_ref_offsets(start: int, end: int, text_length: int) -> None:
    if start < 0 or end <= start or end > text_length:
        raise ValueError("source reference offsets exceed current readable text")


def _enforce_context_budget(
    chosen: tuple[_CandidateChunk, ...],
    direct_input_token_budget: int | None,
) -> None:
    if len(chosen) > MAX_READING_CHAT_CONTEXT_CHUNKS:
        raise FullDocumentTooLarge("context chunk count exceeds the provider contract")
    if sum(len(item.text) for item in chosen) > MAX_READING_CHAT_CONTEXT_CHARACTERS:
        raise FullDocumentTooLarge("context text exceeds the provider contract")
    estimated_tokens = (
        sum(_estimate_tokens(item.text) for item in chosen)
        + len(chosen) * _CONTEXT_METADATA_TOKEN_RESERVE_PER_CHUNK
        + _REQUEST_TOKEN_RESERVE
    )
    budget = direct_input_token_budget or _direct_input_token_budget()
    if estimated_tokens > budget:
        raise FullDocumentTooLarge("hierarchical full-document reading is required")


def _estimate_tokens(text: str) -> int:
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    return max(1, int(cjk * 1.5 + (len(text) - cjk) * 0.35))


def _direct_input_token_budget() -> int:
    raw = str(os.environ.get(_DIRECT_INPUT_BUDGET_ENV, "") or "").strip()
    if not raw:
        return _DEFAULT_DIRECT_INPUT_TOKEN_BUDGET
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("reading direct input token budget must be an integer") from exc
    if not 16_000 <= value <= 950_000:
        raise ValueError("reading direct input token budget is outside safe bounds")
    return value


__all__ = [
    "EffectiveSourceIncomplete",
    "FullDocumentTooLarge",
    "RetrievedReadingContext",
    "assemble_reading_context",
]
