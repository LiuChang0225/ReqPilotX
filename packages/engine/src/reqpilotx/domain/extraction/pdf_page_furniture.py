from __future__ import annotations

import re
import unicodedata
from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from reqpilotx.domain.source_visual_text import VisualTextSnapshot


PDF_PAGE_FURNITURE_MARGIN_RATIO = 0.10
PDF_PAGE_FURNITURE_MIN_ADJACENT_PAGES = 3
PDF_PAGE_FURNITURE_IMAGE_GEOMETRY_TOLERANCE_RATIO = 0.01
PDF_REPEATED_HEADER_IMAGE_FURNITURE_DIAGNOSTIC = (
    "pdf_repeated_header_image_furniture"
)
PDF_REPEATED_FOOTER_IMAGE_FURNITURE_DIAGNOSTIC = (
    "pdf_repeated_footer_image_furniture"
)

_PAGE_MARKER = re.compile(
    r"^\s*(?:[-\u2014\u2013]\s*)?(?:"
    r"(?:(?P<page_prefix>page|p\.)\s*)?"
    r"(?P<cjk_prefix>第\s*)?(?P<arabic>[1-9][0-9]{0,3})"
    r"(?:\s*(?P<page_suffix>页|頁)"
    r"|\s*(?P<total_separator>/|of)\s*(?P<total>[1-9][0-9]{0,3}))?"
    r"|(?P<roman>[IVXLCDM]{1,10})"
    r")(?:\s*[-\u2014\u2013])?\s*$",
    re.IGNORECASE,
)

_CandidateId = TypeVar("_CandidateId", bound=Hashable)


class PdfPageFurnitureKind(str, Enum):
    HEADER = "header"
    FOOTER = "footer"


@dataclass(frozen=True, slots=True)
class PdfPageFurnitureCandidate(Generic[_CandidateId]):
    """One text occurrence in a PDF page coordinate system.

    ``page_order`` is the stable native paint or visual observation order on
    that page.  ``series_key`` prevents evidence from different visual
    providers from being combined into one repeated-page proof.
    """

    candidate_id: _CandidateId
    page_number: int
    page_order: int
    raw_text: str
    bbox: tuple[float, float, float, float]
    page_bbox: tuple[float, float, float, float]
    series_key: str = "native"


@dataclass(frozen=True, slots=True)
class PdfPageFurnitureImageCandidate(Generic[_CandidateId]):
    """One immutable image placement eligible for repeated-furniture proof."""

    candidate_id: _CandidateId
    page_number: int
    page_order: int
    content_sha256: str
    bbox: tuple[float, float, float, float]
    page_bbox: tuple[float, float, float, float]
    series_key: str = "native-image"


def classify_pdf_page_furniture(
    candidates: Iterable[PdfPageFurnitureCandidate[_CandidateId]],
) -> dict[_CandidateId, PdfPageFurnitureKind]:
    """Prove repeated PDF headers, footers, and dynamic page markers.

    A candidate must lie fully in the outer ten percent of the real page and
    recur on at least three adjacent pages.  Fixed text must be unique on each
    page.  Dynamic markers additionally need one stable printed-page to PDF-
    page offset.  Facing-page horizontal mirroring is intentionally ignored.
    """

    fixed_occurrences: dict[
        tuple[str, PdfPageFurnitureKind, tuple[str, ...]],
        dict[int, list[PdfPageFurnitureCandidate[_CandidateId]]],
    ] = {}
    variable_occurrences: dict[
        tuple[
            str,
            PdfPageFurnitureKind,
            int,
            tuple[str, ...],
            tuple[str, ...],
            tuple[object, ...],
        ],
        dict[int, list[tuple[PdfPageFurnitureCandidate[_CandidateId], int]]],
    ] = {}

    for candidate in candidates:
        kind = pdf_page_furniture_margin_kind(candidate.page_bbox, candidate.bbox)
        if kind is None or candidate.page_number < 1:
            continue
        normalized_lines = _normalized_lines(candidate.raw_text)
        if not normalized_lines:
            continue

        marker_lines = tuple(
            (line_index, match, printed_number)
            for line_index, line in enumerate(normalized_lines)
            if (match := _PAGE_MARKER.fullmatch(line)) is not None
            and (printed_number := _page_marker_number(match)) is not None
        )
        if not marker_lines:
            fixed_occurrences.setdefault(
                (candidate.series_key, kind, normalized_lines), {}
            ).setdefault(candidate.page_number, []).append(candidate)
            continue

        marker_only_candidate = len(marker_lines) == len(normalized_lines)
        for marker_line_index, match, printed_number in marker_lines:
            marker_is_explicit = _page_marker_is_explicit(match)
            # A changing bare number beside fixed prose at the page top is
            # equally consistent with a legitimate numbered running title.
            if (
                not marker_only_candidate
                and not marker_is_explicit
                and kind is PdfPageFurnitureKind.HEADER
            ):
                continue
            key = (
                candidate.series_key,
                kind,
                marker_line_index,
                normalized_lines[:marker_line_index],
                normalized_lines[marker_line_index + 1 :],
                _page_marker_series_signature(match),
            )
            variable_occurrences.setdefault(key, {}).setdefault(
                candidate.page_number, []
            ).append((candidate, printed_number))

    result: dict[_CandidateId, PdfPageFurnitureKind] = {}
    for (_, kind, _), candidates_by_page in fixed_occurrences.items():
        unique_candidates = {
            page_number: page_candidates[0]
            for page_number, page_candidates in candidates_by_page.items()
            if len(page_candidates) == 1
        }
        for run in _adjacent_page_runs(unique_candidates):
            if len(run) < PDF_PAGE_FURNITURE_MIN_ADJACENT_PAGES:
                continue
            result.update(
                {
                    unique_candidates[page_number].candidate_id: kind
                    for page_number in run
                }
            )

    for (
        _,
        kind,
        _,
        fixed_prefix,
        fixed_suffix,
        _,
    ), candidates_by_page in variable_occurrences.items():
        ordered_candidates = {
            page_number: tuple(
                sorted(
                    page_candidates,
                    key=lambda item: item[0].page_order,
                )
            )
            for page_number, page_candidates in candidates_by_page.items()
        }
        slot_count = max(
            (len(page_candidates) for page_candidates in ordered_candidates.values()),
            default=0,
        )
        for slot_index in range(slot_count):
            slot_candidates = {
                page_number: page_candidates[slot_index]
                for page_number, page_candidates in ordered_candidates.items()
                if len(page_candidates) > slot_index
            }
            for adjacent_run in _adjacent_page_runs(slot_candidates):
                offset_run: list[int] = []
                offset: int | None = None
                for page_number in adjacent_run:
                    _, printed_number = slot_candidates[page_number]
                    candidate_offset = printed_number - page_number
                    if offset_run and candidate_offset != offset:
                        recorded = _record_variable_run(
                            result=result,
                            run=offset_run,
                            candidates=slot_candidates,
                            kind=kind,
                        )
                        if recorded and not fixed_prefix and not fixed_suffix:
                            _record_fixed_total(
                                result=result,
                                run=offset_run,
                                ordered_candidates=ordered_candidates,
                                current_slot_index=slot_index,
                                kind=kind,
                            )
                        offset_run = []
                    if not offset_run:
                        offset = candidate_offset
                    offset_run.append(page_number)
                recorded = _record_variable_run(
                    result=result,
                    run=offset_run,
                    candidates=slot_candidates,
                    kind=kind,
                )
                if recorded and not fixed_prefix and not fixed_suffix:
                    _record_fixed_total(
                        result=result,
                        run=offset_run,
                        ordered_candidates=ordered_candidates,
                        current_slot_index=slot_index,
                        kind=kind,
                    )
    return result


def classify_pdf_repeated_image_furniture(
    candidates: Iterable[PdfPageFurnitureImageCandidate[_CandidateId]],
) -> dict[_CandidateId, PdfPageFurnitureKind]:
    """Prove repeated header/footer image placements without erasing assets.

    Image proof is intentionally stricter than repeated text proof: the exact
    content digest, margin kind, and provider/native series must match; every
    normalized bbox edge must remain within one percent of the page; and the
    placement must be unique on each of at least three adjacent pages.
    """

    occurrences: dict[
        tuple[str, PdfPageFurnitureKind, str],
        dict[
            int,
            list[
                tuple[
                    PdfPageFurnitureImageCandidate[_CandidateId],
                    tuple[float, float, float, float],
                ]
            ],
        ],
    ] = {}
    for candidate in candidates:
        kind = pdf_page_furniture_margin_kind(candidate.page_bbox, candidate.bbox)
        normalized_geometry = _normalized_image_geometry(
            candidate.page_bbox,
            candidate.bbox,
        )
        if (
            kind is None
            or candidate.page_number < 1
            or re.fullmatch(r"[0-9a-f]{64}", candidate.content_sha256) is None
            or normalized_geometry is None
        ):
            continue
        occurrences.setdefault(
            (candidate.series_key, kind, candidate.content_sha256),
            {},
        ).setdefault(candidate.page_number, []).append(
            (candidate, normalized_geometry)
        )

    result: dict[_CandidateId, PdfPageFurnitureKind] = {}
    for (_, kind, _), candidates_by_page in occurrences.items():
        unique_candidates = {
            page_number: page_candidates[0]
            for page_number, page_candidates in candidates_by_page.items()
            if len(page_candidates) == 1
        }
        for adjacent_run in _adjacent_page_runs(unique_candidates):
            geometry_run: list[int] = []
            anchor_geometry: tuple[float, float, float, float] | None = None
            for page_number in adjacent_run:
                _, geometry = unique_candidates[page_number]
                if (
                    geometry_run
                    and anchor_geometry is not None
                    and not _image_geometries_are_near(anchor_geometry, geometry)
                ):
                    _record_image_run(
                        result=result,
                        run=geometry_run,
                        candidates=unique_candidates,
                        kind=kind,
                    )
                    geometry_run = []
                    anchor_geometry = None
                if not geometry_run:
                    anchor_geometry = geometry
                geometry_run.append(page_number)
            _record_image_run(
                result=result,
                run=geometry_run,
                candidates=unique_candidates,
                kind=kind,
            )
    return result


def pdf_image_furniture_diagnostic_code(kind: PdfPageFurnitureKind) -> str:
    return (
        PDF_REPEATED_HEADER_IMAGE_FURNITURE_DIAGNOSTIC
        if kind is PdfPageFurnitureKind.HEADER
        else PDF_REPEATED_FOOTER_IMAGE_FURNITURE_DIAGNOSTIC
    )


def pdf_image_furniture_kind_from_diagnostic_codes(
    codes: Iterable[str],
) -> PdfPageFurnitureKind | None:
    observed = set(codes)
    if PDF_REPEATED_HEADER_IMAGE_FURNITURE_DIAGNOSTIC in observed:
        return PdfPageFurnitureKind.HEADER
    if PDF_REPEATED_FOOTER_IMAGE_FURNITURE_DIAGNOSTIC in observed:
        return PdfPageFurnitureKind.FOOTER
    return None


def _normalized_image_geometry(
    page_bbox: tuple[float, float, float, float],
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    page_x0, page_y0, page_x1, page_y1 = page_bbox
    width = page_x1 - page_x0
    height = page_y1 - page_y0
    if width <= 0.0 or height <= 0.0:
        return None
    x0, y0, x1, y1 = bbox
    return (
        (x0 - page_x0) / width,
        (y0 - page_y0) / height,
        (x1 - page_x0) / width,
        (y1 - page_y0) / height,
    )


def _image_geometries_are_near(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return all(
        abs(left_value - right_value)
        <= PDF_PAGE_FURNITURE_IMAGE_GEOMETRY_TOLERANCE_RATIO
        for left_value, right_value in zip(left, right, strict=True)
    )


def _record_image_run(
    *,
    result: dict[_CandidateId, PdfPageFurnitureKind],
    run: list[int],
    candidates: dict[
        int,
        tuple[
            PdfPageFurnitureImageCandidate[_CandidateId],
            tuple[float, float, float, float],
        ],
    ],
    kind: PdfPageFurnitureKind,
) -> None:
    if len(run) < PDF_PAGE_FURNITURE_MIN_ADJACENT_PAGES:
        return
    result.update(
        {candidates[page_number][0].candidate_id: kind for page_number in run}
    )


def visual_pdf_page_furniture_ids(
    snapshots: Iterable[VisualTextSnapshot],
) -> frozenset[str]:
    candidates = (
        PdfPageFurnitureCandidate(
            candidate_id=observation.observation_id,
            page_number=observation.page_number,
            page_order=observation.observation_index,
            raw_text=observation.raw_text,
            bbox=observation.bbox,
            page_bbox=snapshot.page_bbox,
            series_key=(f"{snapshot.provider_id}\x00{snapshot.provider_version}"),
        )
        for snapshot in snapshots
        for observation in snapshot.observations
    )
    return frozenset(classify_pdf_page_furniture(candidates))


def _normalized_lines(raw_text: str) -> tuple[str, ...]:
    return tuple(
        normalized
        for line in raw_text.splitlines()
        if (normalized := " ".join(unicodedata.normalize("NFKC", line).split()))
    )


def pdf_page_furniture_margin_kind(
    page_bbox: tuple[float, float, float, float],
    text_bbox: tuple[float, float, float, float],
) -> PdfPageFurnitureKind | None:
    page_x0, page_y0, page_x1, page_y1 = page_bbox
    text_x0, text_y0, text_x1, text_y1 = text_bbox
    page_height = page_y1 - page_y0
    if page_x1 <= page_x0 or page_height <= 0.0:
        return None
    if (
        text_x1 < text_x0
        or text_y1 < text_y0
        or text_x0 < page_x0
        or text_x1 > page_x1
        or text_y0 < page_y0
        or text_y1 > page_y1
    ):
        return None
    if text_y1 <= page_y0 + page_height * PDF_PAGE_FURNITURE_MARGIN_RATIO:
        return PdfPageFurnitureKind.HEADER
    if text_y0 >= page_y1 - page_height * PDF_PAGE_FURNITURE_MARGIN_RATIO:
        return PdfPageFurnitureKind.FOOTER
    return None


def _adjacent_page_runs(candidates: dict[int, object]) -> tuple[tuple[int, ...], ...]:
    runs: list[tuple[int, ...]] = []
    run: list[int] = []
    for page_number in sorted(candidates):
        if run and page_number != run[-1] + 1:
            runs.append(tuple(run))
            run = []
        run.append(page_number)
    if run:
        runs.append(tuple(run))
    return tuple(runs)


def _record_variable_run(
    *,
    result: dict[_CandidateId, PdfPageFurnitureKind],
    run: list[int],
    candidates: dict[
        int,
        tuple[PdfPageFurnitureCandidate[_CandidateId], int],
    ],
    kind: PdfPageFurnitureKind,
) -> bool:
    if len(run) < PDF_PAGE_FURNITURE_MIN_ADJACENT_PAGES:
        return False
    printed_numbers = tuple(candidates[page_number][1] for page_number in run)
    if len(set(printed_numbers)) != len(printed_numbers):
        return False
    result.update(
        {candidates[page_number][0].candidate_id: kind for page_number in run}
    )
    return True


def _record_fixed_total(
    *,
    result: dict[_CandidateId, PdfPageFurnitureKind],
    run: list[int],
    ordered_candidates: dict[
        int,
        tuple[tuple[PdfPageFurnitureCandidate[_CandidateId], int], ...],
    ],
    current_slot_index: int,
    kind: PdfPageFurnitureKind,
) -> None:
    if len(run) < PDF_PAGE_FURNITURE_MIN_ADJACENT_PAGES:
        return
    candidate_counts = {len(ordered_candidates[page_number]) for page_number in run}
    if len(candidate_counts) != 1:
        return
    current_numbers = tuple(
        ordered_candidates[page_number][current_slot_index][1] for page_number in run
    )
    paired_slots: list[int] = []
    for slot_index in range(next(iter(candidate_counts))):
        if slot_index == current_slot_index:
            continue
        printed_numbers = {
            ordered_candidates[page_number][slot_index][1] for page_number in run
        }
        if len(printed_numbers) == 1 and next(iter(printed_numbers)) >= max(
            current_numbers
        ):
            paired_slots.append(slot_index)
    if len(paired_slots) != 1:
        return
    total_slot_index = paired_slots[0]
    result.update(
        {
            ordered_candidates[page_number][total_slot_index][0].candidate_id: kind
            for page_number in run
        }
    )


def _page_marker_number(match: re.Match[str]) -> int | None:
    arabic = match.group("arabic")
    if arabic is not None:
        page_number = int(arabic)
        page_prefix = match.group("page_prefix")
        cjk_prefix = match.group("cjk_prefix")
        page_suffix = match.group("page_suffix")
        total = match.group("total")
        if cjk_prefix is not None and page_suffix is None:
            return None
        if page_prefix is not None and cjk_prefix is not None:
            return None
        if total is not None and page_number > int(total):
            return None
        return page_number
    roman = str(match.group("roman") or "").upper()
    value = _roman_value(roman)
    return value if value is not None and _roman_marker(value) == roman else None


def _page_marker_is_explicit(match: re.Match[str]) -> bool:
    return any(
        match.group(group_name) is not None
        for group_name in (
            "page_prefix",
            "cjk_prefix",
            "page_suffix",
            "total_separator",
        )
    )


def _page_marker_series_signature(match: re.Match[str]) -> tuple[object, ...]:
    """Keep changing page numbers inside one stable printed marker form."""

    if match.group("roman") is not None:
        return ("roman",)
    total = match.group("total")
    return (
        "arabic",
        str(match.group("page_prefix") or "").casefold(),
        bool(match.group("cjk_prefix")),
        str(match.group("page_suffix") or ""),
        str(match.group("total_separator") or "").casefold(),
        int(total) if total is not None else None,
    )


def _roman_value(value: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for character in reversed(value):
        number = values.get(character)
        if number is None:
            return None
        total += -number if number < previous else number
        previous = max(previous, number)
    return total if 0 < total <= 3999 else None


def _roman_marker(value: int) -> str:
    parts: list[str] = []
    for number, numeral in (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    ):
        count, value = divmod(value, number)
        parts.append(numeral * count)
    return "".join(parts)


__all__ = [
    "PDF_PAGE_FURNITURE_IMAGE_GEOMETRY_TOLERANCE_RATIO",
    "PDF_PAGE_FURNITURE_MARGIN_RATIO",
    "PDF_PAGE_FURNITURE_MIN_ADJACENT_PAGES",
    "PDF_REPEATED_FOOTER_IMAGE_FURNITURE_DIAGNOSTIC",
    "PDF_REPEATED_HEADER_IMAGE_FURNITURE_DIAGNOSTIC",
    "PdfPageFurnitureCandidate",
    "PdfPageFurnitureImageCandidate",
    "PdfPageFurnitureKind",
    "classify_pdf_page_furniture",
    "classify_pdf_repeated_image_furniture",
    "pdf_image_furniture_diagnostic_code",
    "pdf_image_furniture_kind_from_diagnostic_codes",
    "pdf_page_furniture_margin_kind",
    "visual_pdf_page_furniture_ids",
]
