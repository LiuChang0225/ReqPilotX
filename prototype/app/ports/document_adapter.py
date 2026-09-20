from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from app.domain.extraction.preflight import DocumentFormat


@dataclass(frozen=True, slots=True)
class ParsedDocumentPayload:
    """Compatibility payload produced by a document parser adapter.

    This is deliberately not SourceIR.  It keeps the legacy parser behind a
    port while the immutable SourceIR adapter is introduced in the next stage.
    """

    text: str
    segments: tuple[Mapping[str, Any], ...]
    file_type: str
    diagnostics: Mapping[str, Any]


@runtime_checkable
class DocumentAdapterPort(Protocol):
    def supports(self, document_format: DocumentFormat) -> bool:
        ...

    def parse_bytes(
        self,
        source_bytes: bytes,
        *,
        source_name: str,
    ) -> ParsedDocumentPayload:
        ...
