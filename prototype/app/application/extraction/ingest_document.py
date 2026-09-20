from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from os import PathLike
from pathlib import Path

from app.adapters.documents.preflight import LocalDocumentPreflight
from app.domain.extraction.preflight import DocumentPreflightReport
from app.ports.document_adapter import DocumentAdapterPort, ParsedDocumentPayload
from app.ports.document_preflight import DocumentPreflightPort


class IngestionRejectionCode(str, Enum):
    PREFLIGHT_FAILED = "preflight_failed"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    PARSE_FAILED = "parse_failed"
    SOURCE_CHANGED = "source_changed"


class DocumentIngestionRejected(ValueError):
    def __init__(
        self,
        code: IngestionRejectionCode,
        report: DocumentPreflightReport,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.report = report

    def safe_detail(self) -> dict[str, object]:
        return {
            "message": "The document cannot enter the extraction pipeline.",
            "code": self.code.value,
            "detected_format": self.report.identity.detected_format.value,
            "authenticity_status": self.report.authenticity_status.value,
            "support_status": self.report.support_status.value,
            "integrity_status": self.report.integrity_status.value,
            "access_status": self.report.access_status.value,
            "diagnostic_codes": [item.code.value for item in self.report.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    preflight: DocumentPreflightReport
    parsed: ParsedDocumentPayload


def prepare_document_for_ingestion(
    source_path: str | PathLike[str],
    *,
    preflight: DocumentPreflightPort | None = None,
    adapter: DocumentAdapterPort | None = None,
) -> PreparedDocument:
    """Prepare a document for isolated tests or an explicitly injected adapter.

    Production uploads call ``inspect_document_for_ingestion`` and enqueue the
    durable SourceIR pipeline. This parser helper is intentionally unavailable
    unless the isolated test runtime calls it or a caller injects an adapter.
    """

    report = inspect_document_for_ingestion(
        source_path,
        preflight=preflight,
    )
    if adapter is None:
        from app.adapters.documents.isolated_test_parser import (
            IsolatedTestDocumentAdapter,
        )

        document_adapter = IsolatedTestDocumentAdapter()
    else:
        document_adapter = adapter
    if not document_adapter.supports(report.identity.detected_format):
        raise DocumentIngestionRejected(
            IngestionRejectionCode.ADAPTER_UNAVAILABLE,
            report,
            f"No serving adapter is available for {report.identity.detected_format.value}.",
        )
    try:
        source_bytes = Path(source_path).read_bytes()
    except OSError as exc:
        raise DocumentIngestionRejected(
            IngestionRejectionCode.SOURCE_CHANGED,
            report,
            "The source document became unavailable after preflight.",
        ) from exc
    if (
        len(source_bytes) != report.identity.source_size
        or hashlib.sha256(source_bytes).hexdigest() != report.identity.source_sha256
    ):
        raise DocumentIngestionRejected(
            IngestionRejectionCode.SOURCE_CHANGED,
            report,
            "The source document changed after preflight.",
        )
    try:
        parsed = document_adapter.parse_bytes(
            source_bytes,
            source_name=report.identity.source_name,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise DocumentIngestionRejected(
            IngestionRejectionCode.PARSE_FAILED,
            report,
            "The document parser did not produce a safe result.",
        ) from exc
    try:
        current_size, current_sha256 = _source_identity(source_path)
    except OSError as exc:
        raise DocumentIngestionRejected(
            IngestionRejectionCode.SOURCE_CHANGED,
            report,
            "The source document became unavailable after parsing.",
        ) from exc
    if (
        current_size != report.identity.source_size
        or current_sha256 != report.identity.source_sha256
    ):
        raise DocumentIngestionRejected(
            IngestionRejectionCode.SOURCE_CHANGED,
            report,
            "The source document changed between preflight and parsing.",
        )
    return PreparedDocument(preflight=report, parsed=parsed)


def inspect_document_for_ingestion(
    source_path: str | PathLike[str],
    *,
    preflight: DocumentPreflightPort | None = None,
) -> DocumentPreflightReport:
    """Validate immutable source identity without running a serving parser."""

    report = (preflight or LocalDocumentPreflight()).inspect(source_path)
    if not report.processable:
        raise DocumentIngestionRejected(
            IngestionRejectionCode.PREFLIGHT_FAILED,
            report,
            "Document preflight did not pass.",
        )
    return report


def _source_identity(source_path: str | PathLike[str]) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with Path(source_path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
