from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from os import PathLike
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.domain.extraction.preflight import DocumentFormat, DocumentPreflightReport
from app.domain.extraction.source_ir import (
    MergedRegion,
    NativeDocumentFormat,
    NativeLocator,
    SourceDiagnostic,
    SourceDiagnosticSeverity,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
    stable_table_id,
)
from app.ports.source_ir_adapter import (
    SourceIRAdapterRejected,
    SourceIRAdapterRejectionCode,
)
from app.services.ai_copilot import get_runtime_ai_config_value


AZURE_DI_SOURCE_IR_ADAPTER_ID = "azure-document-intelligence-prebuilt-layout"
AZURE_DI_SOURCE_IR_ADAPTER_VERSION = "1.0.0+api-2024-11-30"
AZURE_DI_API_VERSION = "2024-11-30"
AZURE_DI_LAYOUT_MODEL_ID = "prebuilt-layout"


class _HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any:
        ...


class _HttpClient(Protocol):
    def post(self, url: str, **kwargs: Any) -> _HttpResponse:
        ...

    def get(self, url: str, **kwargs: Any) -> _HttpResponse:
        ...


class AzureDocumentIntelligenceSourceIRAdapter:
    """Parse the original PDF directly with Azure DI; SourceIR stays source-bound."""

    adapter_id = AZURE_DI_SOURCE_IR_ADAPTER_ID
    adapter_version = AZURE_DI_SOURCE_IR_ADAPTER_VERSION
    document_format = DocumentFormat.PDF

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        plan_identity_sha256: str | None = None,
        http_client: _HttpClient | None = None,
        request_timeout_seconds: float = 60.0,
        poll_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 0.5,
        max_poll_attempts: int = 1_200,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if plan_identity_sha256 is not None and (
            len(plan_identity_sha256) != 64
            or any(character not in "0123456789abcdef" for character in plan_identity_sha256)
        ):
            raise ValueError("plan_identity_sha256 must be a lowercase SHA-256")
        self.adapter_version = (
            f"{AZURE_DI_SOURCE_IR_ADAPTER_VERSION}+plan."
            f"{plan_identity_sha256[:12]}"
            if plan_identity_sha256
            else AZURE_DI_SOURCE_IR_ADAPTER_VERSION
        )
        self._endpoint = _normalize_endpoint(endpoint)
        self._api_key = str(api_key or "").strip()
        if self._endpoint is None or not self._api_key:
            raise ValueError("Azure Document Intelligence is not configured")
        if request_timeout_seconds <= 0 or poll_timeout_seconds <= 0:
            raise ValueError("Azure DI timeouts must be positive")
        if not 1 <= int(max_poll_attempts) <= 10_000:
            raise ValueError("max_poll_attempts is outside the safe bound")
        self._http_client = http_client
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._poll_timeout_seconds = float(poll_timeout_seconds)
        self._poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._max_poll_attempts = int(max_poll_attempts)
        self._monotonic = monotonic
        self._sleep = sleep

    def extract(
        self,
        source_path: str | PathLike[str],
        *,
        preflight: DocumentPreflightReport,
        created_at: datetime,
    ) -> SourceIRSnapshot:
        path = Path(source_path)
        _validate_preflight(path, preflight)
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.INVALID_TIMESTAMP,
                "SourceIR creation time must be timezone-aware.",
            )
        source_bytes = path.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        if (
            len(source_bytes) != preflight.identity.source_size
            or source_sha256 != preflight.identity.source_sha256
        ):
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.SOURCE_CHANGED,
                "The PDF bytes changed after preflight.",
            )
        if self._http_client is not None:
            payload = self._analyze(self._http_client, source_bytes)
        else:
            with httpx.Client() as client:
                payload = self._analyze(client, source_bytes)
        return _source_ir_from_analyze_result(
            payload,
            source_sha256=source_sha256,
            created_at=created_at,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
        )

    def _analyze(
        self,
        client: _HttpClient,
        source_bytes: bytes,
    ) -> Mapping[str, Any]:
        assert self._endpoint is not None
        analyze_url = (
            f"{self._endpoint}/documentintelligence/documentModels/"
            f"{AZURE_DI_LAYOUT_MODEL_ID}:analyze"
        )
        headers = {
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": self._api_key,
        }
        try:
            response = client.post(
                analyze_url,
                params={
                    "_overload": "analyzeDocument",
                    "api-version": AZURE_DI_API_VERSION,
                },
                headers=headers,
                json={
                    "base64Source": base64.b64encode(source_bytes).decode("ascii")
                },
                timeout=self._request_timeout_seconds,
            )
        except Exception as exc:
            raise RuntimeError("azure_di_primary_request_failed") from exc
        if response.status_code in {401, 403}:
            raise RuntimeError("azure_di_primary_auth_failed")
        if response.status_code != 202:
            raise RuntimeError("azure_di_primary_request_rejected")
        operation_location = str(
            response.headers.get("Operation-Location") or ""
        ).strip()
        _validate_operation_location(
            operation_location,
            endpoint=self._endpoint,
        )
        deadline = self._monotonic() + self._poll_timeout_seconds
        for attempt in range(self._max_poll_attempts):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            try:
                polled = client.get(
                    operation_location,
                    headers={"Ocp-Apim-Subscription-Key": self._api_key},
                    timeout=min(self._request_timeout_seconds, remaining),
                )
            except Exception as exc:
                raise RuntimeError("azure_di_primary_poll_failed") from exc
            if polled.status_code != 200:
                raise RuntimeError("azure_di_primary_poll_rejected")
            payload = polled.json()
            if not isinstance(payload, Mapping):
                raise RuntimeError("azure_di_primary_output_invalid")
            status = str(payload.get("status") or "").strip().lower()
            if status == "succeeded":
                return payload
            if status in {"failed", "canceled", "cancelled"}:
                raise RuntimeError("azure_di_primary_analysis_failed")
            if status not in {"notstarted", "running"}:
                raise RuntimeError("azure_di_primary_status_invalid")
            if attempt + 1 < self._max_poll_attempts:
                self._sleep(min(self._poll_interval_seconds, max(remaining, 0.0)))
        raise RuntimeError("azure_di_primary_timeout")


def build_azure_document_intelligence_source_ir_adapter(
    *,
    plan_identity_sha256: str | None = None,
) -> AzureDocumentIntelligenceSourceIRAdapter:
    return AzureDocumentIntelligenceSourceIRAdapter(
        endpoint=get_runtime_ai_config_value(
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
        ),
        api_key=get_runtime_ai_config_value(
            "AZURE_DOCUMENT_INTELLIGENCE_API_KEY"
        ),
        plan_identity_sha256=plan_identity_sha256,
    )


def _source_ir_from_analyze_result(
    payload: Mapping[str, Any],
    *,
    source_sha256: str,
    created_at: datetime,
    adapter_id: str,
    adapter_version: str,
) -> SourceIRSnapshot:
    result = payload.get("analyzeResult")
    if not isinstance(result, Mapping):
        raise RuntimeError("azure_di_primary_output_invalid")
    if result.get("modelId") not in {None, AZURE_DI_LAYOUT_MODEL_ID}:
        raise RuntimeError("azure_di_primary_model_mismatch")
    raw_pages = result.get("pages")
    pages = raw_pages if isinstance(raw_pages, list) else []
    page_dimensions = {
        int(page.get("pageNumber")): (
            float(page.get("width") or 0.0),
            float(page.get("height") or 0.0),
        )
        for page in pages
        if isinstance(page, Mapping)
        and isinstance(page.get("pageNumber"), int)
        and int(page.get("pageNumber")) >= 1
    }
    candidates: list[tuple[int, int, SourceNode]] = []
    diagnostics: list[SourceDiagnostic] = []
    block_index_by_page: dict[int, int] = {}
    paragraphs = result.get("paragraphs")
    for ordinal, paragraph in enumerate(
        paragraphs if isinstance(paragraphs, list) else []
    ):
        if not isinstance(paragraph, Mapping):
            diagnostics.append(_diagnostic("azure_di_paragraph_invalid"))
            continue
        text = paragraph.get("content")
        region = _first_bounding_region(paragraph)
        if not isinstance(text, str) or not text.strip() or region is None:
            diagnostics.append(_diagnostic("azure_di_paragraph_unbound"))
            continue
        page_number, polygon = region
        bbox = _polygon_bbox(polygon, page_dimensions.get(page_number))
        block_index = block_index_by_page.get(page_number, 0)
        block_index_by_page[page_number] = block_index + 1
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            block_index=block_index,
            bbox=bbox,
        )
        kind = _paragraph_kind(str(paragraph.get("role") or ""))
        node = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=source_sha256,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                kind=kind,
                native_locator=locator,
            ),
            kind=kind,
            source_order=0,
            raw_text=text,
            native_locator=locator,
            visual_locator=locator,
        )
        candidates.append((_span_offset(paragraph, ordinal), 0, node))

    tables = result.get("tables")
    for table_index, table in enumerate(
        tables if isinstance(tables, list) else []
    ):
        if not isinstance(table, Mapping):
            diagnostics.append(_diagnostic("azure_di_table_invalid"))
            continue
        region = _first_bounding_region(table)
        if region is None:
            diagnostics.append(_diagnostic("azure_di_table_unbound"))
            continue
        page_number, polygon = region
        bbox = _polygon_bbox(polygon, page_dimensions.get(page_number))
        row_count = table.get("rowCount")
        column_count = table.get("columnCount")
        if (
            not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count < 1
            or not isinstance(column_count, int)
            or isinstance(column_count, bool)
            or column_count < 1
        ):
            diagnostics.append(_diagnostic("azure_di_table_shape_invalid"))
            continue
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=table_index,
            bbox=bbox,
        )
        table_id = stable_table_id(
            source_sha256=source_sha256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            native_locator=table_locator,
        )
        cells: list[TableCellIR] = []
        merged: list[MergedRegion] = []
        headers: list[MergedRegion] = []
        occupied_headers: set[tuple[int, int]] = set()
        for raw_cell in (
            table.get("cells") if isinstance(table.get("cells"), list) else []
        ):
            if not isinstance(raw_cell, Mapping):
                continue
            row = raw_cell.get("rowIndex")
            column = raw_cell.get("columnIndex")
            row_span = raw_cell.get("rowSpan", 1)
            column_span = raw_cell.get("columnSpan", 1)
            if not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (row, column, row_span, column_span)
            ):
                continue
            if (
                row < 0
                or column < 0
                or row_span < 1
                or column_span < 1
                or row + row_span > row_count
                or column + column_span > column_count
            ):
                continue
            cell_locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=page_number,
                table_index=table_index,
                row_index=row,
                column_index=column,
                bbox=_cell_bbox(raw_cell, page_dimensions.get(page_number), bbox),
            )
            cells.append(
                TableCellIR(
                    cell_id=stable_cell_id(table_id, row, column),
                    row=row,
                    column=column,
                    row_span=row_span,
                    column_span=column_span,
                    raw_text=str(raw_cell.get("content") or ""),
                    native_locator=cell_locator,
                )
            )
            region_value = MergedRegion(
                row=row,
                column=column,
                row_span=row_span,
                column_span=column_span,
            )
            if row_span > 1 or column_span > 1:
                merged.append(region_value)
            if str(raw_cell.get("kind") or "") in {
                "columnHeader",
                "rowHeader",
                "stubHead",
            }:
                coverage = {
                    (rr, cc)
                    for rr in range(row, row + row_span)
                    for cc in range(column, column + column_span)
                }
                if not occupied_headers.intersection(coverage):
                    headers.append(region_value)
                    occupied_headers.update(coverage)
        try:
            table_payload = TableIR(
                table_id=table_id,
                row_count=row_count,
                column_count=column_count,
                header_regions=tuple(headers),
                cells=tuple(cells),
                merged_regions=tuple(merged),
            )
        except ValueError:
            diagnostics.append(_diagnostic("azure_di_table_topology_invalid"))
            continue
        node = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=source_sha256,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                kind=SourceNodeKind.TABLE,
                native_locator=table_locator,
            ),
            kind=SourceNodeKind.TABLE,
            source_order=0,
            native_locator=table_locator,
            visual_locator=table_locator,
            table_payload=table_payload,
        )
        candidates.append((_span_offset(table, table_index), 1, node))

    candidates.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2].native_locator.page_number or 0,
            item[2].source_node_id,
        )
    )
    nodes = tuple(
        item[2].model_copy(update={"source_order": index})
        for index, item in enumerate(candidates)
    )
    if not nodes:
        raise RuntimeError("azure_di_primary_no_source_facts")
    document_id = stable_document_id(source_sha256)
    source_ir_id = stable_source_ir_id(
        document_id=document_id,
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
    )
    return SourceIRSnapshot(
        source_ir_id=source_ir_id,
        document_id=document_id,
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.PARTIAL if diagnostics else SourceIRStatus.COMPLETE,
        nodes=nodes,
        diagnostics=tuple(diagnostics),
        created_at=created_at,
    )


def _validate_preflight(
    path: Path,
    preflight: DocumentPreflightReport,
) -> None:
    if (
        not isinstance(preflight, DocumentPreflightReport)
        or not preflight.processable
        or preflight.identity.detected_format is not DocumentFormat.PDF
        or preflight.identity.source_name != path.name
        or preflight.identity.declared_extension != path.suffix.lower()
    ):
        raise SourceIRAdapterRejected(
            SourceIRAdapterRejectionCode.INVALID_PREFLIGHT,
            "Preflight does not authorize this PDF for Azure DI parsing.",
        )


def _normalize_endpoint(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().rstrip("/")
    parsed = urlparse(candidate)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return candidate


def _validate_operation_location(value: str, *, endpoint: str) -> None:
    operation = urlparse(value)
    base = urlparse(endpoint)
    if (
        operation.scheme.lower() != "https"
        or operation.hostname != base.hostname
        or (operation.port or 443) != (base.port or 443)
        or operation.username
        or operation.password
        or operation.fragment
        or "/analyzeResults/" not in operation.path
    ):
        raise RuntimeError("azure_di_primary_operation_location_invalid")


def _first_bounding_region(
    value: Mapping[str, Any],
) -> tuple[int, Any] | None:
    regions = value.get("boundingRegions")
    if not isinstance(regions, list) or not regions:
        return None
    region = regions[0]
    if (
        not isinstance(region, Mapping)
        or not isinstance(region.get("pageNumber"), int)
        or int(region["pageNumber"]) < 1
    ):
        return None
    return int(region["pageNumber"]), region.get("polygon")


def _polygon_bbox(
    value: Any,
    page_size: tuple[float, float] | None,
) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) < 8 or len(value) % 2:
        if page_size and page_size[0] > 0 and page_size[1] > 0:
            return (0.0, 0.0, page_size[0], page_size[1])
        return (0.0, 0.0, 0.0, 0.0)
    try:
        coordinates = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(coordinates[0::2]),
        min(coordinates[1::2]),
        max(coordinates[0::2]),
        max(coordinates[1::2]),
    )


def _cell_bbox(
    cell: Mapping[str, Any],
    page_size: tuple[float, float] | None,
    fallback: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    region = _first_bounding_region(cell)
    return _polygon_bbox(region[1], page_size) if region is not None else fallback


def _span_offset(value: Mapping[str, Any], fallback: int) -> int:
    spans = value.get("spans")
    if isinstance(spans, list) and spans and isinstance(spans[0], Mapping):
        offset = spans[0].get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
            return offset
    return 10**12 + fallback


def _paragraph_kind(role: str) -> SourceNodeKind:
    return {
        "title": SourceNodeKind.HEADING,
        "sectionHeading": SourceNodeKind.HEADING,
        "pageHeader": SourceNodeKind.HEADER,
        "pageFooter": SourceNodeKind.FOOTER,
        "pageNumber": SourceNodeKind.FOOTER,
        "footnote": SourceNodeKind.NOTE,
        "formulaBlock": SourceNodeKind.NOTE,
    }.get(role, SourceNodeKind.PARAGRAPH)


def _diagnostic(code: str) -> SourceDiagnostic:
    return SourceDiagnostic(
        code=code,
        severity=SourceDiagnosticSeverity.WARNING,
        message="Azure Document Intelligence returned an incomplete source fact.",
        retryable=False,
    )


__all__ = [
    "AZURE_DI_API_VERSION",
    "AZURE_DI_LAYOUT_MODEL_ID",
    "AZURE_DI_SOURCE_IR_ADAPTER_ID",
    "AZURE_DI_SOURCE_IR_ADAPTER_VERSION",
    "AzureDocumentIntelligenceSourceIRAdapter",
    "build_azure_document_intelligence_source_ir_adapter",
]
