"""Compose source hierarchy labels without duplicating exact numbering prefixes."""
from __future__ import annotations


_TITLE_PREFIX_SEPARATORS = frozenset("、,，;；:：()（）[]【】-—–")


def compose_source_path_segment(label: object, title: object) -> str:
    """Return one visible hierarchy segment from a structural label and title.

    A numbered-body node intentionally uses its number as both fields.  Ordinary
    headings may also retain that same number at the start of the title.  Only
    collapse an exact token prefix: ``3.1`` must not consume ``3.10`` or
    ``3.1.1``.
    """

    label_text = str(label or "").strip()
    title_text = str(title or "").strip()
    if not label_text:
        return title_text
    if not title_text or title_text == label_text:
        return label_text
    if title_text.startswith(label_text):
        remainder = title_text[len(label_text) :]
        first = remainder[:1]
        if first.isspace() or first in _TITLE_PREFIX_SEPARATORS:
            return title_text
        if first in ".．" and (len(remainder) == 1 or not remainder[1].isdigit()):
            return title_text
    return f"{label_text} {title_text}"
