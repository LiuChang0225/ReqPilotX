"""Geometry-proven reading views over immutable native PDF text.

No characters in SourceIR are changed. A whole paint line may be omitted from a
reading view only if every character has an equal, same-font occurrence in a
retained line at the same position. Nearby but distinct lines, incomplete
fragments, and ambiguous matches stay unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class _Character:
    text: str
    offset: int
    bbox: tuple[float, ...]
    origin: tuple[float, ...]
    font: str
    size: float
    alpha: int
    color: int


@dataclass(frozen=True)
class _Line:
    index: int
    start: int
    end: int
    text: str
    direction: tuple[float, ...]
    characters: tuple[_Character, ...]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _geometry(value: Any, count: int) -> tuple[float, ...]:
    try:
        result = tuple(float(x) for x in value)
    except (TypeError, ValueError):
        return ()
    return result if len(result) == count and all(math.isfinite(x) for x in result) else ()


def _lines(block: Mapping[str, Any]) -> tuple[str, list[_Line]]:
    lines, offset = [], 0
    for index, line in enumerate(block.get("lines", ())):
        characters, text_parts = [], []
        line_length = 0
        supported = True
        for span in line.get("spans", ()):
            if not isinstance(span.get("chars"), (tuple, list)):
                value = str(span.get("text", ""))
                text_parts.append(value)
                line_length += len(value)
                supported = False
                continue
            for char in span["chars"]:
                text = char.get("c", "")
                text = text if isinstance(text, str) else ""
                char_offset = offset + line_length
                text_parts.append(text)
                line_length += len(text)
                bbox, origin = _geometry(char.get("bbox"), 4), _geometry(char.get("origin"), 2)
                size = float(span.get("size", 0))
                if (len(text) != 1 or not bbox or not origin or not math.isfinite(size)
                        or size <= 0 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]):
                    supported = False
                characters.append(_Character(text, char_offset, bbox, origin,
                                              str(span.get("font", "")), size,
                                              int(span.get("alpha", 255)), int(span.get("color", 0))))
        text = "".join(text_parts)
        lines.append(_Line(index, offset, offset + len(text), text,
                           _geometry(line.get("dir"), 2), tuple(characters) if supported else ()))
        offset += len(text) + 1
    return "\n".join(line.text for line in lines), lines


def _same_paint(left: _Character, right: _Character) -> bool:
    # At most 6% of the font em and 0.35 PDF points: the tolerance scales down
    # for tiny glyphs and never grows enough to merge separate small labels.
    tolerance = min(0.35, 0.06 * min(left.size, right.size))
    return (left.text == right.text and left.font == right.font and bool(left.font)
            and left.alpha == right.alpha and left.alpha > 0 and left.color == right.color
            and abs(left.size - right.size) <= 0.001
            and all(abs(a - b) <= tolerance for a, b in zip(left.bbox, right.bbox))
            and all(abs(a - b) <= tolerance for a, b in zip(left.origin, right.origin)))


def _contained_match(line: _Line, retained: _Line) -> int | None:
    if (not line.text.strip() or not line.characters or not retained.characters
            or not line.direction or line.direction != retained.direction):
        return None
    matches = []
    start = retained.text.find(line.text)
    while start >= 0:
        candidates = retained.characters[start:start + len(line.characters)]
        if len(candidates) == len(line.characters) and all(
                _same_paint(a, b) for a, b in zip(line.characters, candidates)):
            matches.append(start)
        start = retained.text.find(line.text, start + 1)
    return matches[0] if len(matches) == 1 else None


def project_pdf_text_block(block: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a proved reading projection, or None; never modify ``block``.

    Projection offsets are in the unchanged ``raw_text``. Only fully duplicated
    lines are folded; partially overlapping lines are deliberately not repaired.
    Retained lines keep native order, including real repeated physical lines.
    """
    raw_text, lines = _lines(block)
    retained: list[_Line] = []
    duplicates = []
    # Prefer a complete native line over a contained paint fragment. This lets
    # an original name+part-number line survive without synthesizing that line.
    for line in sorted(lines, key=lambda item: (-len(item.text), item.index)):
        proofs = [(other, start) for other in retained
                  if (start := _contained_match(line, other)) is not None]
        if len(proofs) != 1:
            retained.append(line)
            continue
        other, start = proofs[0]
        pairs = [[char.offset, target.offset, list(char.bbox), list(target.bbox)]
                 for char, target in zip(line.characters, other.characters[start:])]
        duplicates.append({
            "start_offset": line.start, "end_offset": line.end,
            "retained_start_offset": other.start + start,
            "retained_end_offset": other.start + start + len(line.text),
            "character_count": len(line.characters),
            "max_coordinate_delta_pt": max(abs(a - b) for c, t in zip(line.characters, other.characters[start:])
                                           for a, b in zip(c.bbox, t.bbox)),
            "character_geometry_sha256": _digest(pairs),
        })
    if not duplicates:
        return None
    retained.sort(key=lambda item: item.index)
    partial_overlaps = []
    for index, left in enumerate(retained):
        for right in retained[index + 1:]:
            if not left.direction or left.direction != right.direction:
                continue
            count = sum(any(_same_paint(a, b) for b in right.characters) for a in left.characters)
            if count >= 2:
                partial_overlaps.append({"left_start_offset": left.start,
                                         "right_start_offset": right.start,
                                         "matching_character_count": count})
    display_text = "\n".join(line.text for line in retained)
    receipt = {
        "schema_version": "reqpilotx-pdf-reading-projection/v1",
        "method": "same_font_character_geometry_whole_line",
        "raw_text_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
        "display_text": display_text,
        "display_text_sha256": hashlib.sha256(display_text.encode()).hexdigest(),
        "source_ranges": [{"start_offset": line.start, "end_offset": line.end} for line in retained],
        "duplicate_ranges": sorted(duplicates, key=lambda item: item["start_offset"]),
        "raw_line_count": len(lines), "display_line_count": len(retained),
        "raw_text_preserved": True,
        "remaining_partial_overlaps": partial_overlaps,
    }
    receipt["projection_sha256"] = _digest(receipt)
    return receipt


def project_pdf_source_contents(
    source_path: str | Path, *, expected_source_sha256: str,
    contents: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Bind content reading views to exact native blocks in a verified PDF.

    Existing content ids, original text and source ranges remain authoritative.
    Unsupported partial/multi-block contents get no projection. Matching text at
    another page or another position is never enough to establish this binding.
    """
    import pymupdf

    source_bytes = Path(source_path).read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != expected_source_sha256:
        raise ValueError("pdf_reading_projection_source_hash_mismatch")
    groups: dict[int, list[Mapping[str, Any]]] = {}
    for content in contents:
        groups.setdefault(int(content.get("page", 0)), []).append(content)
    result = {}
    with pymupdf.open(stream=source_bytes, filetype="pdf") as pdf:
        for page_number, requested in groups.items():
            if not 1 <= page_number <= len(pdf):
                continue
            page = pdf[page_number - 1]
            blocks = page.get_text("rawdict", sort=False)["blocks"]
            candidates = []
            for block_index, block in enumerate(blocks):
                if block.get("type") != 0:
                    continue
                projection = project_pdf_text_block(block)
                if projection is not None:
                    visual_bbox = tuple(pymupdf.Rect(block["bbox"]) * page.rotation_matrix)
                    candidates.append((block_index, visual_bbox, projection))
            for content in requested:
                text = str(content.get("text", ""))
                text_sha = hashlib.sha256(text.encode()).hexdigest()
                if content.get("text_sha256") and content["text_sha256"] != text_sha:
                    raise ValueError("pdf_reading_projection_content_hash_mismatch")
                bbox = _geometry(content.get("bbox"), 4)
                matches = [(index, projection) for index, visual_bbox, projection in candidates
                           if bbox and projection["raw_text_sha256"] == text_sha
                           and all(abs(a - b) <= 0.5 for a, b in zip(bbox, visual_bbox))]
                if len(matches) != 1 or not content.get("content_id"):
                    continue
                block_index, projection = matches[0]
                bound = {**projection, "source_sha256": expected_source_sha256,
                         "page": page_number, "block_index": block_index,
                         "content_id": content["content_id"], "source_bbox": list(bbox)}
                bound.pop("projection_sha256")
                bound["projection_sha256"] = _digest(bound)
                result[str(content["content_id"])] = bound
    return result
