from __future__ import annotations

import codecs


def declared_plain_text_encoding(prefix: bytes) -> str:
    """Return the one strict encoding allowed by the TXT compatibility path."""

    if prefix.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if prefix.startswith(codecs.BOM_UTF16_LE) or prefix.startswith(codecs.BOM_UTF16_BE):
        # The BOM is required for UTF-16 and lets Python select endianness while
        # removing the marker from the decoded customer text.
        return "utf-16"
    return "utf-8"


def decode_plain_text_strict(payload: bytes) -> str:
    """Decode TXT bytes without replacement or ignored data."""

    return payload.decode(declared_plain_text_encoding(payload[:4]), errors="strict")


__all__ = ["decode_plain_text_strict", "declared_plain_text_encoding"]
