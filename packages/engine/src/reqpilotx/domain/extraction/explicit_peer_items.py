from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


EXPLICIT_NUMERIC_PEER_ITEM_PARSER_VERSION = (
    "source-bound-explicit-numeric-peer-items/v1"
)

_PEER_ITEM_PREFIX = re.compile(
    r"^[ \t]*(?P<ordinal>[1-9０-９][0-9０-９]{0,3})[ \t]*、"
    r"[ \t]*(?P<body>\S.*?)[ \t]*$"
)
_FORMULA_PREFIX_CHARACTERS = frozenset("=+-*/×÷^<>≤≥")


@dataclass(frozen=True, slots=True)
class ExplicitNumericPeerItem:
    """One text-free boundary inside an exact native source text view."""

    ordinal: int
    start_offset: int
    end_offset: int


def parse_explicit_numeric_peer_items(
    text: str,
) -> tuple[ExplicitNumericPeerItem, ...]:
    """Return one conservative two-item partition or no result.

    The initial production policy is intentionally narrow: exactly two hard
    lines, the Chinese enumeration delimiter ``、``, consecutive ordinals
    starting after item 1, and two substantive non-formula bodies. Returned
    offsets are Unicode code-point offsets into the unchanged input and form
    an exact cover, including the original line ending.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text or "\x00" in text:
        return ()

    lines = text.splitlines(keepends=True)
    if len(lines) != 2 or "".join(lines) != text:
        return ()

    items: list[ExplicitNumericPeerItem] = []
    normalized_bodies: list[str] = []
    offset = 0
    for line in lines:
        line_text = line.rstrip("\r\n")
        match = _PEER_ITEM_PREFIX.fullmatch(line_text)
        if match is None:
            return ()
        normalized_ordinal = unicodedata.normalize("NFKC", match.group("ordinal"))
        body = unicodedata.normalize("NFKC", match.group("body")).strip()
        if (
            len(body) < 8
            or body[0] in _FORMULA_PREFIX_CHARACTERS
            or not any(character.isalpha() for character in body)
        ):
            return ()
        items.append(
            ExplicitNumericPeerItem(
                ordinal=int(normalized_ordinal),
                start_offset=offset,
                end_offset=offset + len(line),
            )
        )
        normalized_bodies.append(body)
        offset += len(line)

    ordinals = tuple(item.ordinal for item in items)
    if (
        ordinals[0] < 2
        or ordinals[1] != ordinals[0] + 1
        or normalized_bodies[0] == normalized_bodies[1]
        or items[-1].end_offset != len(text)
    ):
        return ()
    return tuple(items)


__all__ = [
    "EXPLICIT_NUMERIC_PEER_ITEM_PARSER_VERSION",
    "ExplicitNumericPeerItem",
    "parse_explicit_numeric_peer_items",
]
