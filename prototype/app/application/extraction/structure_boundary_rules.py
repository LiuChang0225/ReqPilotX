from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from app.domain.extraction.source_ir import SourceNode, SourceNodeKind


STRUCTURE_BOUNDARY_RULESET_VERSION = "1.8.0"

# This module owns source-text grammars and bounded evidence predicates shared by
# the deterministic resolver and the detect-only inspector.  Callers decide
# whether accepted evidence mutates a tree, creates an inspection candidate, or
# is sent to an optional model.  Keeping those decisions outside this module
# prevents one parser heuristic from silently becoming product authorization.

EXPLICIT_SECTION_PREFIX = re.compile(
    r"^\s*(?P<label>[1-9][0-9]{0,2}(?:[ \t\u3000]*\.[ \t\u3000]*(?:0|[1-9][0-9]{0,2})){0,7})"
    r"(?P<trailing_dot>[.．])?"
    r"(?=$|[ \t\u3000\r\n]|[)）]|[\u3400-\u9fff])"
)
EXPLICIT_LIST_PREFIX = re.compile(
    r"^\s*(?P<ordinal>[1-9][0-9]{0,3})\s*[)）]"
)
ALPHABETIC_LIST_PREFIX = re.compile(r"^\s*(?P<marker>[A-Za-z])\s*[)）]")
ALPHABETIC_LIST_PREFIX_WITH_PAYLOAD = re.compile(
    r"^[ \t\u3000]{0,12}"
    r"(?P<marker>[A-Za-z])[ \t]*(?P<suffix>[)）])"
    r"(?=[ \t\u3000]*\S)"
)
_EMBEDDED_NUMERIC_PARENTHESIZED_PREFIX = re.compile(
    r"(?m)^[ \t\u3000]{0,12}"
    r"(?P<marker>[1-9][0-9]{0,3})[ \t]*(?P<suffix>[)）])"
)
_EMBEDDED_ALPHABETIC_PARENTHESIZED_PREFIX = re.compile(
    r"(?m)^[ \t\u3000]{0,12}"
    r"(?P<marker>[A-Za-z])[ \t]*(?P<suffix>[)）])"
)
IDEOGRAPHIC_NUMBERED_PREFIX = re.compile(
    r"^\s*(?P<ordinal>[1-9][0-9]{0,3})\s*、\s*(?P<title>\S.*)$"
)
BODY_APPENDIX_HEADING = re.compile(
    r"^\s*(?P<label>"
    r"(?:附录|附件)\s*(?:[A-Z0-9一二三四五六七八九十]{1,3})"
    r"|appendix\s+[A-Z0-9]{1,3}"
    r"|annex\s+[A-Z0-9]{1,3}"
    r")"
    r"(?P<tail>.*)$",
    re.IGNORECASE,
)
DOCUMENT_REGION_BOUNDARY_PATTERN = re.compile(
    r"(?im)^[ \t\u3000]{0,8}"
    r"(?P<marker>"
    r"(?:附录|附件)[ \t\u3000]*(?:[A-Z0-9]|[一二三四五六七八九十百]+)"
    r"|(?:Appendix|Annex)[ \t]+[A-Z0-9]+"
    r")"
    r"(?P<tail>[^\r\n]*)$"
)
AMBIGUOUS_LETTERED_REGION_TOKEN = re.compile(
    r"^(?P<marker>[A-Z])$"
)
DOCUMENT_REGION_SELF_REFERENCE = re.compile(
    r"(?:本附录|本附件|this\s+(?:appendix|annex))",
    re.IGNORECASE,
)
APPENDIX_REFERENCE_TAIL = re.compile(
    r"^(?:"
    r"中|内|的|所述|所示|规定|要求|见|详见|参见|请见|"
    r"用于|包含|为|是|"
    r"of\b|is\b|are\b|contains?\b|shows?\b|describes?\b|"
    r"provides?\b|applies?\b|see\b|refer\b"
    r")",
    re.IGNORECASE,
)
APPENDIX_QUALIFIER_PREFIX = re.compile(
    r"^[（(]?(?:规范性|资料性|normative|informative)[）)]?",
    re.IGNORECASE,
)
ROOT_MEASUREMENT_PREFIX = re.compile(
    r"^(?:"
    r"(?:mm|cm|m|km|mg|g|kg|n|kn|pa|kpa|mpa|v|mv|a|ma|hz|khz|%)"
    r"(?:\b|\s)|"
    r"(?:周|天|日|小时|时|分钟|分|秒|月|年|次|个|件|台|套|辆)"
    r"(?=$|\s|[，,。.;；:：、）)的内以上下后前])"
    r")",
    re.IGNORECASE,
)
ROOT_HEADING_MAX_NORMALIZED_CHARACTERS = 160
ROOT_SENTENCE_TERMINATOR = re.compile(
    r"[。！？!?；;]\s*[）)\]】」』”’\"']*\s*$"
)
IMPLICIT_CJK_QUANTIFIER_PREFIX = re.compile(
    r"^(?:"
    r"℃|°[CFcf]|"
    r"毫米|厘米|米|千米|毫克|克|千克|牛|千牛|帕|千帕|兆帕|"
    r"伏|毫伏|安|毫安|赫兹|千赫兹|"
    r"周|天|日|小时|时|分钟|分|秒|月|年|次|个|件|台|套|辆"
    r")"
)
NUMERIC_VALUE_LINE_PATTERN = re.compile(
    r"^[\s\u3000]*"
    r"[+\-−–—±~～<>≤≥]?"
    r"(?:\d+(?:[.,]\d+)?|\.\d+)"
    r"(?:"
    r"[\s\u3000,，;；/\\|~～+\-−–—±<>≤≥]"
    r"|(?:mm|cm|m|km|mg|g|kg|n|kn|pa|kpa|mpa|v|mv|a|ma|hz|khz|%)"
    r"|(?:℃|°c|°f|度|周|天|日|小时|时|分钟|分|秒|月|年|次|个|件|台|套|辆)"
    r"|(?:\d+(?:[.,]\d+)?|\.\d+)"
    r")*$",
    re.IGNORECASE,
)

_CAPTION_HORIZONTAL_SPACE = r"[ \t\u00a0\u3000]*"
_CAPTION_NUMBER_SEPARATOR = (
    rf"{_CAPTION_HORIZONTAL_SPACE}[.\-．]{_CAPTION_HORIZONTAL_SPACE}"
)
_CAPTION_NUMBER_LABEL = (
    rf"[0-9]{{1,4}}(?:{_CAPTION_NUMBER_SEPARATOR}[0-9]{{1,4}}){{0,7}}"
)
_CAPTION_NUMBER_BOUNDARY = (
    rf"(?!{_CAPTION_NUMBER_SEPARATOR}[0-9])"
    r"(?=$|[\s:：.．。()（）-]|[,，;；、!?！？](?!\s*[0-9])|[\u3400-\u9fff])"
)
TABLE_CAPTION_PREFIX = re.compile(
    rf"^\s*(?:表|table)\s*(?P<label>{_CAPTION_NUMBER_LABEL})"
    rf"{_CAPTION_NUMBER_BOUNDARY}",
    re.IGNORECASE,
)
FIGURE_CAPTION_PREFIX = re.compile(
    rf"^\s*(?:图|figure|fig\.?)\s*(?P<label>{_CAPTION_NUMBER_LABEL})"
    rf"{_CAPTION_NUMBER_BOUNDARY}",
    re.IGNORECASE,
)
FIGURE_BODY_REFERENCE = re.compile(
    r"(?:见下|参见|如下|见|see(?:\s+below)?)\s*(?:图|figure|fig\.?)\s*"
    rf"(?P<label>{_CAPTION_NUMBER_LABEL}){_CAPTION_NUMBER_BOUNDARY}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BoundaryEvidenceVerdict:
    """Content-free result from one reusable boundary evidence rule."""

    rule_id: str
    accepted: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AlphabeticMarkerEvidence:
    marker: str
    ordinal: int
    uppercase: bool
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class AlphabeticPeerPosition:
    source_order: int
    page_number: int | None
    indent_kind: Literal["bbox", "text", "unknown"]
    indent_value: float | None


@dataclass(frozen=True)
class AlphabeticPeerPolicy:
    max_source_order_gap: int
    max_page_gap: int
    bbox_indent_tolerance_points: float
    text_indent_tolerance: float
    require_same_indent_kind: bool
    allow_unknown_indent: bool


RESOLVER_ALPHABETIC_PEER_POLICY = AlphabeticPeerPolicy(
    max_source_order_gap=8,
    max_page_gap=1,
    bbox_indent_tolerance_points=8.0,
    text_indent_tolerance=2.0,
    require_same_indent_kind=False,
    allow_unknown_indent=True,
)
INSPECTOR_ALPHABETIC_PEER_POLICY = AlphabeticPeerPolicy(
    # Inspection only links close source facts.  The resolver may span bounded
    # interstitial content because it separately validates that content.
    max_source_order_gap=2,
    max_page_gap=1,
    bbox_indent_tolerance_points=12.0,
    text_indent_tolerance=2.0,
    require_same_indent_kind=True,
    allow_unknown_indent=False,
)

_FIGURE_CLUSTER_BOUNDARY_TOLERANCE_POINTS = 4.0
_SHORT_VISUAL_LABEL_MAX_CHARACTERS = 48
_SHORT_VISUAL_LABEL_MAX_VERTICAL_GAP_POINTS = 28.0


@dataclass(frozen=True)
class DocumentRegionMarkerEvidence:
    raw_marker: str
    safe_marker: str
    tail: str
    start_offset: int
    end_offset: int
    reference_like: bool


@dataclass(frozen=True)
class AmbiguousLetteredRegionMarkerEvidence:
    """One weak, content-bound letter token from a short region-heading shape.

    The token is intentionally insufficient to authorize a tree boundary.
    Callers still need independent page geometry, a coherent peer sequence,
    and downstream self-reference or lettered designator evidence.
    """

    marker: str
    ordinal: int
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class EmbeddedPeerMarkerEvidence:
    family: Literal["numeric_parenthesized", "alphabetic_parenthesized"]
    raw_marker: str
    safe_marker: str
    ordinal: int
    uppercase: bool | None
    start_offset: int
    end_offset: int
    text_indent: float
    has_payload: bool


@dataclass(frozen=True)
class ImplicitCjkRootEvidence:
    """Evidence for the ambiguous compact form ``N标题``.

    This object is lexical and negative-evidence only.  It never authorizes a
    Structure boundary; the decision layer must still require an independent
    peer, dotted child, native heading, or TOC corroborator.
    """

    label: str
    ordinal: int
    payload: str
    start_offset: int
    end_offset: int
    negative_reason_codes: tuple[str, ...]


def explicit_section_label_from_text(
    raw_text: str,
    *,
    native_heading: bool,
) -> str | None:
    """Parse one credible explicit numeric outline label.

    Root-like numbers need stronger native/title evidence than dotted
    descendants because measurements and ordinary numbered prose share the
    same lexical prefix.
    """

    if not raw_text:
        return None
    match = EXPLICIT_SECTION_PREFIX.match(raw_text)
    if match is None:
        return None
    label = re.sub(r"[ \t\u3000]", "", match.group("label"))
    parts = label.split(".")
    parenthesized_suffix = match_has_parenthesized_suffix(raw_text, match)
    if parenthesized_suffix and len(parts) == 1:
        return None
    remainder = raw_text[match.end() :].strip()
    if (
        len(parts) == 1
        and match.group("trailing_dot") is not None
        and not native_heading
    ):
        return None
    if not native_heading and not remainder:
        return None
    if not native_heading and NUMERIC_VALUE_LINE_PATTERN.fullmatch(raw_text):
        return None
    logical_root = len(parts) == 1 or (len(parts) == 2 and parts[1] == "0")
    if logical_root and not native_heading:
        normalized_remainder = " ".join(remainder.split())
        if (
            not normalized_remainder
            or len(normalized_remainder) > ROOT_HEADING_MAX_NORMALIZED_CHARACTERS
            or normalized_remainder[0].isdigit()
            or normalized_remainder[0] in ".．+-±≤≥<>"
        ):
            return None
        if ROOT_MEASUREMENT_PREFIX.match(normalized_remainder):
            return None
        if (
            root_prefix_uses_implicit_cjk_adjacency(raw_text, match=match)
            and (
                ROOT_SENTENCE_TERMINATOR.search(normalized_remainder)
                or IMPLICIT_CJK_QUANTIFIER_PREFIX.match(normalized_remainder)
            )
        ):
            return None
    return label


def bare_dotted_section_label_from_text(raw_text: str) -> str | None:
    """Return one payload-free dotted outline token as lexical evidence only.

    A bare decimal is intentionally not a Structure boundary by itself.  The
    resolver must additionally prove an adjacent title block and exact sibling
    sequence closure before it may promote this token.
    """

    if not raw_text:
        return None
    match = EXPLICIT_SECTION_PREFIX.match(raw_text)
    if match is None or raw_text[match.end() :].strip():
        return None
    label = re.sub(r"[ \t\u3000]", "", match.group("label"))
    return label if "." in label else None


def implicit_cjk_root_evidence_from_text(
    raw_text: str,
) -> ImplicitCjkRootEvidence | None:
    """Parse weak ``N中文`` root evidence without deciding tree membership."""

    match = EXPLICIT_SECTION_PREFIX.match(raw_text)
    if match is None or not root_prefix_uses_implicit_cjk_adjacency(
        raw_text,
        match=match,
    ):
        return None
    label = re.sub(r"[ \t\u3000]", "", match.group("label"))
    if "." in label:
        return None
    payload = " ".join(raw_text[match.end() :].split())
    negative_reason_codes: list[str] = []
    if not payload:
        negative_reason_codes.append("implicit_root_missing_payload")
    if len(payload) > ROOT_HEADING_MAX_NORMALIZED_CHARACTERS:
        negative_reason_codes.append("implicit_root_payload_too_long")
    if ROOT_SENTENCE_TERMINATOR.search(payload):
        negative_reason_codes.append("implicit_root_completed_sentence")
    if IMPLICIT_CJK_QUANTIFIER_PREFIX.match(payload):
        negative_reason_codes.append("implicit_root_quantifier_prefix")
    if NUMERIC_VALUE_LINE_PATTERN.fullmatch(raw_text):
        negative_reason_codes.append("implicit_root_numeric_value")
    return ImplicitCjkRootEvidence(
        label=label,
        ordinal=int(label),
        payload=payload,
        start_offset=match.start("label"),
        end_offset=match.end("label"),
        negative_reason_codes=tuple(negative_reason_codes),
    )


def root_prefix_uses_implicit_cjk_adjacency(
    raw_text: str,
    *,
    match: re.Match[str],
) -> bool:
    """Return whether a root number touches Chinese payload without a delimiter.

    ``1范围`` is a common compact heading form, so adjacency alone is not a
    rejection.  It is weaker evidence than ``1 范围`` or ``1、范围`` though:
    when the payload is a completed sentence the prefix is more plausibly a
    technical identifier such as a numbered node/channel than an outline root.
    The predicate deliberately contains no ordinal ceiling or vocabulary list.
    """

    if "." in re.sub(r"[ \t\u3000]", "", match.group("label")):
        return False
    if match.group("trailing_dot") is not None:
        return False
    label_end = match.end("label")
    if label_end >= len(raw_text):
        return False
    return bool(re.match(r"[\u3400-\u9fff]", raw_text[label_end]))


def match_has_parenthesized_suffix(
    raw_text: str,
    match: re.Match[str],
) -> bool:
    suffix = raw_text[match.end() :].lstrip(" \t")
    return suffix.startswith((")", "）"))


def parse_alphabetic_parenthesized_marker(
    raw_text: str,
    *,
    require_payload: bool,
) -> AlphabeticMarkerEvidence | None:
    pattern = (
        ALPHABETIC_LIST_PREFIX_WITH_PAYLOAD
        if require_payload
        else ALPHABETIC_LIST_PREFIX
    )
    match = pattern.match(raw_text)
    if match is None:
        return None
    marker = match.group("marker")
    end_group = "suffix" if require_payload else "marker"
    end_offset = (
        match.end(end_group)
        if require_payload
        else _parenthesized_marker_end_offset(raw_text, match.end(end_group))
    )
    return AlphabeticMarkerEvidence(
        marker=marker,
        ordinal=ord(marker.lower()) - ord("a") + 1,
        uppercase=marker.isupper(),
        start_offset=match.start("marker"),
        end_offset=end_offset,
    )


def embedded_parenthesized_peer_sequences(
    raw_text: str,
) -> tuple[tuple[EmbeddedPeerMarkerEvidence, ...], ...]:
    """Return coherent peer runs printed inside one immutable fact.

    This is intentionally detect-only evidence.  A resolver or model may not
    split the source fact merely because the grammar matched; exact-cover
    projection and parent selection remain separate decisions.  Runs that do
    not start at ``1``/``a`` or contain only two peers remain visible so the
    inspector can assign lower confidence instead of silently discarding them.
    """

    markers = tuple(
        sorted(
            (
                *_embedded_peer_markers(
                    raw_text,
                    family="numeric_parenthesized",
                    pattern=_EMBEDDED_NUMERIC_PARENTHESIZED_PREFIX,
                ),
                *_embedded_peer_markers(
                    raw_text,
                    family="alphabetic_parenthesized",
                    pattern=_EMBEDDED_ALPHABETIC_PARENTHESIZED_PREFIX,
                ),
            ),
            key=lambda item: (item.start_offset, item.family, item.safe_marker),
        )
    )
    runs: list[tuple[EmbeddedPeerMarkerEvidence, ...]] = []
    current: list[EmbeddedPeerMarkerEvidence] = []

    def flush() -> None:
        if (
            len(current) >= 2
            and all(item.has_payload for item in current)
            and all(
                item.ordinal == previous.ordinal + 1
                for previous, item in zip(current, current[1:], strict=False)
            )
        ):
            runs.append(tuple(current))
        current.clear()

    for marker in markers:
        if marker.ordinal == 1:
            flush()
            current.append(marker)
            continue
        if not current:
            current.append(marker)
            continue
        anchor = current[0]
        previous = current[-1]
        if (
            marker.family == previous.family
            and marker.uppercase == previous.uppercase
            and marker.ordinal == previous.ordinal + 1
            and marker.start_offset > previous.start_offset
            and abs(marker.text_indent - anchor.text_indent) <= 2.0
        ):
            current.append(marker)
            continue
        flush()
        current.append(marker)
    flush()
    return tuple(runs)


def _embedded_peer_markers(
    raw_text: str,
    *,
    family: Literal["numeric_parenthesized", "alphabetic_parenthesized"],
    pattern: re.Pattern[str],
) -> tuple[EmbeddedPeerMarkerEvidence, ...]:
    result: list[EmbeddedPeerMarkerEvidence] = []
    matches = tuple(pattern.finditer(raw_text))
    for index, match in enumerate(matches):
        next_marker_start = (
            matches[index + 1].start() if index + 1 < len(matches) else len(raw_text)
        )
        # PDF text blocks can preserve a list marker as its own native line
        # while keeping the payload in the same immutable SourceNode.  Require
        # actual text before the next same-family marker instead of requiring
        # it to share the marker's physical line.  Empty marker outlines still
        # fail closed and therefore cannot authorize a synthetic split.
        has_payload = bool(
            raw_text[match.end("suffix") : next_marker_start].strip()
        )
        marker = match.group("marker")
        line_start = raw_text.rfind("\n", 0, match.start("marker")) + 1
        prefix = raw_text[line_start : match.start("marker")]
        text_indent = float(
            sum(
                4 if character == "\t" else 2 if character == "\u3000" else 1
                for character in prefix
            )
        )
        if family == "alphabetic_parenthesized":
            ordinal = ord(marker.lower()) - ord("a") + 1
            uppercase: bool | None = marker.isupper()
        else:
            ordinal = int(marker)
            uppercase = None
        result.append(
            EmbeddedPeerMarkerEvidence(
                family=family,
                raw_marker=marker,
                safe_marker=f"{marker}{match.group('suffix')}",
                ordinal=ordinal,
                uppercase=uppercase,
                start_offset=match.start("marker"),
                end_offset=match.end("suffix"),
                text_indent=text_indent,
                has_payload=has_payload,
            )
        )
    return tuple(result)


def _parenthesized_marker_end_offset(raw_text: str, marker_end: int) -> int:
    suffix = re.match(r"[ \t]*[)）]", raw_text[marker_end:])
    return marker_end + (suffix.end() if suffix is not None else 0)


def alphabetic_peer_position(
    source_node: SourceNode,
    *,
    marker_start_offset: int,
    include_text_indent: bool,
) -> AlphabeticPeerPosition:
    bbox = source_node.native_locator.bbox
    if bbox is not None:
        indent_kind: Literal["bbox", "text", "unknown"] = "bbox"
        indent_value: float | None = float(bbox[0])
    elif include_text_indent:
        indent_kind = "text"
        indent_value = float(
            sum(
                4 if character == "\t" else 2 if character == "\u3000" else 1
                for character in source_node.raw_text[:marker_start_offset]
            )
        )
    else:
        indent_kind = "unknown"
        indent_value = None
    return AlphabeticPeerPosition(
        source_order=source_node.source_order,
        page_number=source_node.native_locator.page_number,
        indent_kind=indent_kind,
        indent_value=indent_value,
    )


def evaluate_alphabetic_peer_transition(
    current_marker: AlphabeticMarkerEvidence,
    *,
    previous_marker: AlphabeticMarkerEvidence,
    current_position: AlphabeticPeerPosition,
    previous_position: AlphabeticPeerPosition,
    anchor_position: AlphabeticPeerPosition,
    policy: AlphabeticPeerPolicy,
) -> BoundaryEvidenceVerdict:
    reasons: set[str] = set()
    if current_marker.uppercase != previous_marker.uppercase:
        reasons.add("alphabetic_case_family_changed")
    if current_marker.ordinal != previous_marker.ordinal + 1:
        reasons.add("alphabetic_ordinal_not_consecutive")
    source_gap = current_position.source_order - previous_position.source_order
    if source_gap <= 0:
        reasons.add("source_order_not_forward")
    elif source_gap > policy.max_source_order_gap:
        reasons.add("source_gap_exceeded")
    if (
        current_position.page_number is not None
        and previous_position.page_number is not None
    ):
        page_gap = current_position.page_number - previous_position.page_number
        if page_gap < 0:
            reasons.add("page_order_reversed")
        elif page_gap > policy.max_page_gap:
            reasons.add("page_gap_exceeded")
    if (
        policy.require_same_indent_kind
        and current_position.indent_kind != anchor_position.indent_kind
    ):
        reasons.add("indent_basis_changed")
    if (
        not policy.allow_unknown_indent
        and (
            current_position.indent_value is None
            or anchor_position.indent_value is None
        )
    ):
        reasons.add("indent_evidence_missing")
    if (
        current_position.indent_value is not None
        and anchor_position.indent_value is not None
        and current_position.indent_kind == anchor_position.indent_kind
    ):
        tolerance = (
            policy.bbox_indent_tolerance_points
            if anchor_position.indent_kind == "bbox"
            else policy.text_indent_tolerance
        )
        if abs(current_position.indent_value - anchor_position.indent_value) > tolerance:
            reasons.add("indent_shift_exceeded")
    accepted = not reasons
    return BoundaryEvidenceVerdict(
        rule_id="alphabetic_peer_transition/v1",
        accepted=accepted,
        reason_codes=(
            ("alphabetic_peer_transition_accepted",)
            if accepted
            else tuple(sorted(reasons))
        ),
    )


def alphabetic_pair_interstitial_is_transparent(
    source_nodes: tuple[SourceNode, ...],
    *,
    before: SourceNode,
    after: SourceNode,
) -> bool:
    return alphabetic_pair_interstitial_evidence(
        source_nodes,
        before=before,
        after=after,
    ).accepted


def alphabetic_pair_interstitial_evidence(
    source_nodes: tuple[SourceNode, ...],
    *,
    before: SourceNode,
    after: SourceNode,
) -> BoundaryEvidenceVerdict:
    content_nodes = tuple(
        node
        for node in source_nodes
        if node.kind
        not in {
            SourceNodeKind.HEADER,
            SourceNodeKind.FOOTER,
            SourceNodeKind.WATERMARK,
        }
    )
    if not content_nodes:
        return BoundaryEvidenceVerdict(
            rule_id="alphabetic_pair_interstitial/v1",
            accepted=True,
            reason_codes=("page_furniture_only",),
        )
    if all(_caption_node(node) for node in content_nodes):
        return BoundaryEvidenceVerdict(
            rule_id="alphabetic_pair_interstitial/v1",
            accepted=True,
            reason_codes=("numbered_caption_only",),
        )
    reasons = _figure_evidence_cluster_rejection_reasons(
        content_nodes,
        before=before,
        after=after,
    )
    return BoundaryEvidenceVerdict(
        rule_id="alphabetic_pair_interstitial/v1",
        accepted=not reasons,
        reason_codes=(
            ("bounded_figure_evidence_cluster",)
            if not reasons
            else tuple(sorted(reasons))
        ),
    )


def _direct_visual_evidence_node(source_node: SourceNode) -> bool:
    if source_node.kind in {
        SourceNodeKind.IMAGE,
        SourceNodeKind.FIGURE,
        SourceNodeKind.TABLE,
    }:
        return True
    return _caption_node(source_node)


def _figure_evidence_cluster_rejection_reasons(
    source_nodes: tuple[SourceNode, ...],
    *,
    before: SourceNode,
    after: SourceNode,
) -> set[str]:
    reasons: set[str] = set()
    before_page = before.native_locator.page_number
    after_page = after.native_locator.page_number
    if before_page is None or before_page != after_page:
        reasons.add("anchor_pages_do_not_match")
    if before_page is not None and any(
        node.native_locator.page_number != before_page for node in source_nodes
    ):
        reasons.add("interstitial_crosses_page")
    before_bbox = before.native_locator.bbox
    after_bbox = after.native_locator.bbox
    if before_bbox is None or after_bbox is None:
        reasons.add("anchor_geometry_missing")
    visual_nodes = tuple(
        node
        for node in source_nodes
        if node.kind
        in {
            SourceNodeKind.IMAGE,
            SourceNodeKind.FIGURE,
            SourceNodeKind.TABLE,
        }
        and node.native_locator.bbox is not None
    )
    if not visual_nodes:
        reasons.add("visual_asset_missing")
    if not any(_caption_node(node) for node in source_nodes):
        reasons.add("numbered_caption_missing")
    if before_bbox is not None and after_bbox is not None and any(
        node.native_locator.bbox is None
        or node.native_locator.bbox[1]
        < before_bbox[3] - _FIGURE_CLUSTER_BOUNDARY_TOLERANCE_POINTS
        or node.native_locator.bbox[3]
        > after_bbox[1] + _FIGURE_CLUSTER_BOUNDARY_TOLERANCE_POINTS
        for node in source_nodes
    ):
        reasons.add("interstitial_outside_anchor_band")
    if visual_nodes and any(
        not (
            _direct_visual_evidence_node(node)
            or _short_visual_label(node, visual_nodes=visual_nodes)
        )
        for node in source_nodes
    ):
        reasons.add("unexplained_interstitial_content")
    return reasons


def _caption_node(source_node: SourceNode) -> bool:
    return bool(
        source_node.kind
        in {
            SourceNodeKind.PARAGRAPH,
            SourceNodeKind.NOTE,
        }
        and (
            FIGURE_CAPTION_PREFIX.match(source_node.raw_text) is not None
            or TABLE_CAPTION_PREFIX.match(source_node.raw_text) is not None
        )
    )


def _short_visual_label(
    source_node: SourceNode,
    *,
    visual_nodes: tuple[SourceNode, ...],
) -> bool:
    if _caption_node(source_node):
        return True
    compact = " ".join(source_node.raw_text.split())
    bbox = source_node.native_locator.bbox
    if (
        source_node.kind
        not in {
            SourceNodeKind.PARAGRAPH,
            SourceNodeKind.NOTE,
        }
        or bbox is None
        or not compact
        or len(compact) > _SHORT_VISUAL_LABEL_MAX_CHARACTERS
        or re.search(r"[。；;!?！？]$", compact) is not None
        or ALPHABETIC_LIST_PREFIX.match(compact) is not None
        or EXPLICIT_LIST_PREFIX.match(compact) is not None
        or IDEOGRAPHIC_NUMBERED_PREFIX.match(compact) is not None
        or EXPLICIT_SECTION_PREFIX.match(compact) is not None
        or BODY_APPENDIX_HEADING.match(compact) is not None
    ):
        return False
    return any(
        _bbox_is_near_visual_label(
            bbox,
            visual_bbox=node.native_locator.bbox,
        )
        for node in visual_nodes
        if node.native_locator.bbox is not None
    )


def _bbox_is_near_visual_label(
    bbox: tuple[float, float, float, float],
    *,
    visual_bbox: tuple[float, float, float, float],
) -> bool:
    x0, y0, x1, y1 = bbox
    vx0, vy0, vx1, vy1 = visual_bbox
    visual_width = vx1 - vx0
    label_width = x1 - x0
    if label_width > max(visual_width * 1.5, 180.0):
        return False
    horizontal_gap = max(vx0 - x1, x0 - vx1, 0.0)
    vertical_gap = max(vy0 - y1, y0 - vy1, 0.0)
    return (
        horizontal_gap <= max(visual_width, 96.0)
        and vertical_gap <= _SHORT_VISUAL_LABEL_MAX_VERTICAL_GAP_POINTS
    )


def body_appendix_heading(raw_text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", raw_text).strip()
    if (
        not normalized
        or len(" ".join(normalized.split())) > ROOT_HEADING_MAX_NORMALIZED_CHARACTERS
        or normalized.startswith(("本附录", "本附件"))
    ):
        return False
    match = BODY_APPENDIX_HEADING.fullmatch(normalized)
    if match is None:
        return False
    tail = match.group("tail").strip().lstrip("-—:：").strip()
    if not tail:
        return True
    if APPENDIX_REFERENCE_TAIL.match(tail):
        return False
    qualifier = APPENDIX_QUALIFIER_PREFIX.match(tail)
    if qualifier is not None:
        tail = tail[qualifier.end() :].strip().lstrip("-—:：").strip()
        if not tail:
            return True
        if APPENDIX_REFERENCE_TAIL.match(tail):
            return False
    return (
        len(tail) <= 100
        and "\n" not in tail
        and re.search(r"[。！？!?;；]$", tail) is None
    )


def body_appendix_identity(raw_text: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", raw_text).strip()
    match = BODY_APPENDIX_HEADING.fullmatch(normalized)
    if match is None or not body_appendix_heading(raw_text):
        return None
    return normalize_boundary_title(match.group("label"))


def iter_document_region_markers(
    raw_text: str,
) -> tuple[DocumentRegionMarkerEvidence, ...]:
    result: list[DocumentRegionMarkerEvidence] = []
    for match in DOCUMENT_REGION_BOUNDARY_PATTERN.finditer(raw_text):
        tail = match.group("tail")
        result.append(
            DocumentRegionMarkerEvidence(
                raw_marker=match.group("marker"),
                safe_marker=safe_document_region_marker(match.group("marker")),
                tail=tail,
                start_offset=match.start("marker"),
                end_offset=match.end("marker"),
                reference_like=_document_region_tail_is_reference_like(
                    match.group(0),
                    tail,
                ),
            )
        )
    return tuple(result)


def ambiguous_lettered_region_marker(
    raw_text: str,
) -> AmbiguousLetteredRegionMarkerEvidence | None:
    """Return a weak standalone ``A``/``B`` region token.

    Surrounding whitespace and Unicode punctuation are presentation, but every
    remaining source character is semantic.  In particular, text such as
    ``版本A说明`` or ``附A录`` must not be treated as an OCR-corrupted appendix
    marker.  Geometry and cross-page corroboration remain mandatory in the
    inspection layer.
    """

    start_offset = 0
    end_offset = len(raw_text)
    while start_offset < end_offset and (
        raw_text[start_offset].isspace()
        or unicodedata.category(raw_text[start_offset]).startswith("P")
    ):
        start_offset += 1
    while end_offset > start_offset and (
        raw_text[end_offset - 1].isspace()
        or unicodedata.category(raw_text[end_offset - 1]).startswith("P")
    ):
        end_offset -= 1
    token = unicodedata.normalize("NFKC", raw_text[start_offset:end_offset])
    match = AMBIGUOUS_LETTERED_REGION_TOKEN.fullmatch(token)
    if match is None:
        return None
    marker = match.group("marker").upper()
    return AmbiguousLetteredRegionMarkerEvidence(
        marker=marker,
        ordinal=ord(marker) - ord("A") + 1,
        start_offset=start_offset,
        end_offset=end_offset,
    )


def contains_document_region_self_reference(raw_text: str) -> bool:
    return DOCUMENT_REGION_SELF_REFERENCE.search(
        unicodedata.normalize("NFKC", raw_text)
    ) is not None


def contains_lettered_region_designator(raw_text: str, *, marker: str) -> bool:
    """Return whether text contains a credible ``B.1``-style region label.

    A table/figure prefix is explicit wherever it occurs.  Without that prefix,
    the designator must start its own physical line; inline version, variable,
    or channel identifiers are not document-region evidence.
    """

    if len(marker) != 1 or marker not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        return False
    normalized = unicodedata.normalize("NFKC", raw_text)
    numbered_label = (
        rf"{re.escape(marker)}[ \t\u3000]*[.．][ \t\u3000]*"
        rf"[0-9]{{1,3}}(?![0-9])"
    )
    explicit_table_or_figure = re.search(
        rf"(?<![A-Za-z0-9])"
        rf"(?:表|图|table|figure|fig\.?)[ \t\u3000]*"
        rf"{numbered_label}",
        normalized,
        re.IGNORECASE,
    )
    if explicit_table_or_figure is not None:
        return True
    return (
        re.search(
            rf"(?m)^[ \t\u3000]*{numbered_label}"
            rf"(?=$|[ \t\u3000:：—\-、.)）]|[\u3400-\u9fff])",
            normalized,
            re.IGNORECASE,
        )
        is not None
    )


def _document_region_tail_is_reference_like(full_match: str, tail: str) -> bool:
    normalized_tail = tail.strip().lstrip(" \t\u3000:：—-、.)）")
    if not normalized_tail:
        return False
    if len(full_match.strip()) > 160:
        return True
    return APPENDIX_REFERENCE_TAIL.match(normalized_tail) is not None


def safe_document_region_marker(value: str) -> str:
    normalized = re.sub(r"[ \t\u3000]+", " ", value.strip())
    lowered = normalized.casefold()
    if lowered.startswith("appendix "):
        _, suffix = normalized.split(" ", 1)
        return f"Appendix {suffix.upper()}"
    if lowered.startswith("annex "):
        _, suffix = normalized.split(" ", 1)
        return f"Annex {suffix.upper()}"
    if normalized.startswith("附录"):
        suffix = normalized.removeprefix("附录").strip()
        return f"附录 {suffix.upper()}" if suffix.isascii() else f"附录{suffix}"
    suffix = normalized.removeprefix("附件").strip()
    return f"附件 {suffix.upper()}" if suffix.isascii() else f"附件{suffix}"


def normalize_boundary_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


__all__ = [
    "ALPHABETIC_LIST_PREFIX",
    "ALPHABETIC_LIST_PREFIX_WITH_PAYLOAD",
    "APPENDIX_QUALIFIER_PREFIX",
    "APPENDIX_REFERENCE_TAIL",
    "AlphabeticMarkerEvidence",
    "AlphabeticPeerPolicy",
    "AlphabeticPeerPosition",
    "AmbiguousLetteredRegionMarkerEvidence",
    "AMBIGUOUS_LETTERED_REGION_TOKEN",
    "BODY_APPENDIX_HEADING",
    "BoundaryEvidenceVerdict",
    "DocumentRegionMarkerEvidence",
    "DOCUMENT_REGION_SELF_REFERENCE",
    "EmbeddedPeerMarkerEvidence",
    "EXPLICIT_LIST_PREFIX",
    "EXPLICIT_SECTION_PREFIX",
    "FIGURE_BODY_REFERENCE",
    "FIGURE_CAPTION_PREFIX",
    "IDEOGRAPHIC_NUMBERED_PREFIX",
    "IMPLICIT_CJK_QUANTIFIER_PREFIX",
    "INSPECTOR_ALPHABETIC_PEER_POLICY",
    "ImplicitCjkRootEvidence",
    "NUMERIC_VALUE_LINE_PATTERN",
    "RESOLVER_ALPHABETIC_PEER_POLICY",
    "ROOT_HEADING_MAX_NORMALIZED_CHARACTERS",
    "ROOT_MEASUREMENT_PREFIX",
    "ROOT_SENTENCE_TERMINATOR",
    "STRUCTURE_BOUNDARY_RULESET_VERSION",
    "TABLE_CAPTION_PREFIX",
    "alphabetic_pair_interstitial_evidence",
    "alphabetic_pair_interstitial_is_transparent",
    "alphabetic_peer_position",
    "ambiguous_lettered_region_marker",
    "body_appendix_heading",
    "body_appendix_identity",
    "bare_dotted_section_label_from_text",
    "contains_document_region_self_reference",
    "contains_lettered_region_designator",
    "evaluate_alphabetic_peer_transition",
    "embedded_parenthesized_peer_sequences",
    "explicit_section_label_from_text",
    "iter_document_region_markers",
    "implicit_cjk_root_evidence_from_text",
    "match_has_parenthesized_suffix",
    "normalize_boundary_title",
    "parse_alphabetic_parenthesized_marker",
    "root_prefix_uses_implicit_cjk_adjacency",
    "safe_document_region_marker",
]
