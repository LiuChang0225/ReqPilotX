"""Neutral, read-only construction of immutable SourceIR source facts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from enum import Enum
from os import PathLike

from reqpilotx.adapters.documents.docx_source_ir_adapter import DocxSourceIRAdapter
from reqpilotx.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from reqpilotx.adapters.documents.preflight import LocalDocumentPreflight
from reqpilotx.adapters.documents.xlsb_source_ir_adapter import XlsbSourceIRAdapter
from reqpilotx.adapters.documents.xlsx_source_ir_adapter import XlsxSourceIRAdapter
from reqpilotx.domain.extraction.preflight import DocumentPreflightReport
from reqpilotx.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx.ports.document_preflight import DocumentPreflightPort
from reqpilotx.ports.source_ir_adapter import (
    SourceIRAdapterPort,
    SourceIRAdapterRejected,
    SourceIRAdapterRejectionCode,
)


class SourceIRBuildRejectionCode(str, Enum):
    PREFLIGHT_REJECTED = "preflight_rejected"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    ADAPTER_CONFIGURATION_ERROR = "adapter_configuration_error"
    ADAPTER_REJECTED = "adapter_rejected"
    INVALID_TIMESTAMP = "invalid_timestamp"


class SourceIRBuildRejected(RuntimeError):
    """Safe application-boundary rejection with machine-readable evidence."""

    def __init__(
        self,
        code: SourceIRBuildRejectionCode,
        message: str,
        *,
        preflight_diagnostic_codes: tuple[str, ...] = (),
        adapter_rejection_code: SourceIRAdapterRejectionCode | None = None,
    ) -> None:
        self.code = code
        self.preflight_diagnostic_codes = preflight_diagnostic_codes
        self.adapter_rejection_code = adapter_rejection_code
        super().__init__(message)


def build_source_ir(
    source_path: str | PathLike[str],
    *,
    preflight_port: DocumentPreflightPort | None = None,
    preflight_report: DocumentPreflightReport | None = None,
    adapters: Iterable[SourceIRAdapterPort] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SourceIRSnapshot:
    """Preflight a source, select one native adapter, and build SourceIR.

    The use case is intentionally read-only. Persistence, reading projections,
    structures, semantic classification, and requirements remain separate.
    """

    if preflight_port is not None and preflight_report is not None:
        raise ValueError("preflight_port and preflight_report are mutually exclusive")
    if preflight_report is not None and not isinstance(
        preflight_report,
        DocumentPreflightReport,
    ):
        raise TypeError("preflight_report must be a DocumentPreflightReport")
    inspector = preflight_port or LocalDocumentPreflight()
    report = preflight_report or inspector.inspect(source_path)
    configured_adapters = tuple(adapters) if adapters is not None else _default_adapters()
    matching_adapters = tuple(
        adapter
        for adapter in configured_adapters
        if adapter.document_format is report.identity.detected_format
    )
    controlled_conversion_authorized = bool(
        report.controlled_conversion_eligible
        and len(matching_adapters) == 1
        and getattr(
            matching_adapters[0],
            "supports_controlled_conversion",
            False,
        )
        is True
    )
    if not report.processable and not controlled_conversion_authorized:
        raise SourceIRBuildRejected(
            SourceIRBuildRejectionCode.PREFLIGHT_REJECTED,
            "Document preflight did not authorize native or controlled-conversion SourceIR extraction.",
            preflight_diagnostic_codes=_preflight_diagnostic_codes(report),
        )

    if not matching_adapters:
        raise SourceIRBuildRejected(
            SourceIRBuildRejectionCode.ADAPTER_UNAVAILABLE,
            f"No SourceIR adapter is configured for {report.identity.detected_format.value}.",
        )
    if len(matching_adapters) != 1:
        raise SourceIRBuildRejected(
            SourceIRBuildRejectionCode.ADAPTER_CONFIGURATION_ERROR,
            f"More than one SourceIR adapter is configured for {report.identity.detected_format.value}.",
        )

    created_at = (clock or _utc_now)()
    if (
        not isinstance(created_at, datetime)
        or created_at.tzinfo is None
        or created_at.utcoffset() is None
    ):
        raise SourceIRBuildRejected(
            SourceIRBuildRejectionCode.INVALID_TIMESTAMP,
            "The SourceIR clock must return a timezone-aware datetime.",
        )

    try:
        return matching_adapters[0].extract(
            source_path,
            preflight=report,
            created_at=created_at,
        )
    except SourceIRAdapterRejected as exc:
        raise SourceIRBuildRejected(
            SourceIRBuildRejectionCode.ADAPTER_REJECTED,
            "The selected SourceIR adapter rejected its input boundary.",
            adapter_rejection_code=exc.code,
        ) from exc


def _default_adapters() -> tuple[SourceIRAdapterPort, ...]:
    return (
        PdfSourceIRAdapter(),
        DocxSourceIRAdapter(),
        XlsxSourceIRAdapter(),
        XlsbSourceIRAdapter(),
    )


def _preflight_diagnostic_codes(
    report: DocumentPreflightReport,
) -> tuple[str, ...]:
    return tuple(item.code.value for item in report.diagnostics)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "SourceIRBuildRejected",
    "SourceIRBuildRejectionCode",
    "build_source_ir",
]
