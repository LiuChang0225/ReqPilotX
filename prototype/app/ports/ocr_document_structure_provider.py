from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.ocr_document_structure import OcrDocumentStructureSnapshot
from app.domain.source_visual_text import VisualTextPageInput


@runtime_checkable
class OcrDocumentStructureProviderPort(Protocol):
    """Capture geometry-free document structure for one exact rendered page."""

    provider_id: str
    provider_version: str

    def parse_page(
        self,
        page_input: VisualTextPageInput,
    ) -> OcrDocumentStructureSnapshot: ...


__all__ = ["OcrDocumentStructureProviderPort"]
