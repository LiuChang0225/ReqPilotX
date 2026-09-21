from __future__ import annotations

import tempfile
from pathlib import Path

from reqpilotx.domain.extraction.preflight import DocumentFormat
from reqpilotx.ports.document_adapter import ParsedDocumentPayload
from reqpilotx.services.parser import parse_document_with_diagnostics


class IsolatedTestDocumentAdapterError(RuntimeError):
    """The isolated test parser could not produce an auditable result."""


class IsolatedTestDocumentAdapter:
    """Test-only adapter around the pre-cutover parser implementation."""

    _SUPPORTED_FORMATS = frozenset(
        {DocumentFormat.PDF, DocumentFormat.DOCX, DocumentFormat.TXT}
    )

    def supports(self, document_format: DocumentFormat) -> bool:
        return document_format in self._SUPPORTED_FORMATS

    def parse_bytes(
        self,
        source_bytes: bytes,
        *,
        source_name: str,
    ) -> ParsedDocumentPayload:
        suffix = Path(source_name).suffix.lower()
        with tempfile.TemporaryDirectory(prefix="reqpilotx-serving-source-") as directory:
            staged_path = Path(directory) / f"source{suffix}"
            staged_path.write_bytes(source_bytes)
            parsed = parse_document_with_diagnostics(str(staged_path))
        diagnostics = parsed.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        if str(diagnostics.get("mode") or "").strip().lower() == "fallback_text":
            raise IsolatedTestDocumentAdapterError(
                "The isolated parser attempted an unsafe text fallback."
            )
        segments = parsed.get("segments")
        if not isinstance(segments, list):
            raise IsolatedTestDocumentAdapterError(
                "The isolated parser returned invalid segments."
            )
        return ParsedDocumentPayload(
            text=str(parsed.get("text") or ""),
            segments=tuple(dict(item) for item in segments if isinstance(item, dict)),
            file_type=str(parsed.get("file_type") or ""),
            diagnostics=dict(diagnostics),
        )
