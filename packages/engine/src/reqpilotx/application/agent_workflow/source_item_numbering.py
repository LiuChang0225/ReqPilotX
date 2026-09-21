"""Conservative original-number evidence from already-owned numbered text.

This is a read-only projection. It never changes source boundaries, hierarchy,
classification, IDs or annotations, and never promotes table row numbers.
"""
from __future__ import annotations

import re

from reqpilotx.application.extraction.structure_boundary_rules import (
    EXPLICIT_SECTION_PREFIX, ROOT_MEASUREMENT_PREFIX, IMPLICIT_CJK_QUANTIFIER_PREFIX,
)
from reqpilotx.application.agent_workflow.source_requirement_baseline import SourceRecordForm

# The shared grammar already rejects most units. These common engineering units
# are not covered by its root-heading guard (e.g. a wrapped "20 ms" line).
_ADDITIONAL_MEASUREMENT_PREFIX = re.compile(
    r'^(?:ms|us|µs|μs|ns|ps|MHz|GHz|rpm|°[CF]|℃|Ω|ohms?|W|kW|MW|J|kJ|MJ|'
    r'Ah|mAh|Vdc|Vac|Vrms|Arms|C|F|H|T|dB|dBA|bar|mbar|psi|s|secs?|seconds?|mins?|minutes?|'
    r'h|hrs?|hours?|percent|cycles?)(?=$|\b|[\s/²³])', re.IGNORECASE,
)
_POSSIBLE_START = re.compile(r'\n|(?<=[.!?。；;])[ \t]+')
_NUMBER_RANGE_TAIL = re.compile(r'^(?:[-–—~～/、,，;&]|to\b|through\b|and\b|or\b|至|到|及|或)\s*\d',re.IGNORECASE)


def _prefix(text):
    match = EXPLICIT_SECTION_PREFIX.match(text)
    if match is None:
        return None
    remainder = text[match.end():].lstrip()
    if remainder[:1] in {')', '）'}:
        remainder = remainder[1:].lstrip()
    if not remainder or _NUMBER_RANGE_TAIL.match(remainder) or any(pattern.match(remainder) for pattern in (
            ROOT_MEASUREMENT_PREFIX, IMPLICIT_CJK_QUANTIFIER_PREFIX, _ADDITIONAL_MEASUREMENT_PREFIX)):
        return None
    return match


def observed_source_item_number(document_map, baseline, record, *, table_previews=None):
    """Return one exact {value,evidence}, or None when the source is ambiguous.

Explicit annotation precedence is the caller's responsibility. Multi-number
parents keep the existing explicit source_numbers/source_extent contract.
"""
    if (record.source_form is not SourceRecordForm.NUMBERED_CLAUSE or record.table_fragments
            or record.image_bindings or not record.text_bindings):
        return None
    bindings = [binding for binding in record.text_bindings if binding.text.strip()]
    if not bindings:
        return None
    first = bindings[0]
    prefix = _prefix(first.text)
    if prefix is None:
        return None
    start = first.start_offset + prefix.start('label')
    end = first.start_offset + prefix.end('label')
    node = next(node for node in document_map.nodes if node.node_id == record.node_id)
    content = next(content for content in node.source_content if content.content_id == first.content_id)
    # A range beginning midway through a numeric/token prefix is not an
    # independent original number merely because the range starts there.
    if start and (content.text[start-1].isalnum() or content.text[start-1] in '.．'):
        return None
    occurrences = set()
    for binding in bindings:
        for offset in [0, *(match.end() for match in _POSSIBLE_START.finditer(binding.text))]:
            match = _prefix(binding.text[offset:])
            if match is not None:
                lo = binding.start_offset + offset + match.start('label')
                hi = binding.start_offset + offset + match.end('label')
                if binding.content_id == first.content_id and start <= lo < hi <= end and (lo, hi) != (start, end):
                    continue
                occurrences.add((binding.content_id, lo, hi))
    if occurrences != {(first.content_id, start, end)}:
        return None
    from reqpilotx.application.agent_workflow.source_item_visual import _source_number_evidence
    number = prefix.group('label')
    evidence = _source_number_evidence(document_map, baseline, record.source_record_id, number,
        {'kind': 'TEXT', 'content_id': first.content_id, 'start_offset': start, 'end_offset': end}, table_previews or {})
    return {'value': number, 'evidence': evidence}
