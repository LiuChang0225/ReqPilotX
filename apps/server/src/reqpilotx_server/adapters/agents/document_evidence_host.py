from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import hashlib
import math
import secrets
from typing import Callable

from PIL import Image

from reqpilotx_server.adapters.documents.pdf_source_crop_renderer import (
    inspect_pdf_page_geometry,
    render_pdf_source_crop,
)
from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import (
    PdfSourceIRAdapter,
    render_pdf_visual_page_input,
)
from reqpilotx_server.adapters.documents.preflight import LocalDocumentPreflight
from reqpilotx_server.application.agent_workflow.document_understanding import (
    DocumentAssetFactV1,
    DocumentAssetLedgerV1,
    DocumentEvidenceAuthority,
    DocumentEvidenceRequirement,
    DocumentPageFactV1,
    DocumentPageFactsChunkV1,
    DocumentPageLedgerEntryV2,
    DocumentPageLedgerV2,
    DocumentPageTextChunkV1,
    DocumentPageTextItemV1,
    DocumentRenderDetail,
    DocumentRenderReceiptV1,
    DocumentRunManifestV2,
    DocumentTableCellFactV1,
    DocumentTableMapChunkV1,
    DocumentUnderstandingProposalV2,
    DocumentUnderstandingStatus,
    DocumentUnderstandingValidationReceiptV2,
    canonical_json_sha256,
    document_page_ledger_sha256,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from reqpilotx_server.application.extraction.build_source_ir import build_source_ir
from reqpilotx_server.domain.extraction.source_ir import (
    SourceAssetRef,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
)


TOOL_GET_RUN_MANIFEST = "document.get_run_manifest"
TOOL_GET_PAGE_LEDGER = "document.get_page_ledger"
TOOL_GET_PAGE_FACTS = "document.get_page_facts"
TOOL_GET_PAGE_TEXT = "document.get_page_text"
TOOL_RENDER_PAGE = "document.render_page"
TOOL_RENDER_REGION = "document.render_region"
TOOL_GET_ASSETS = "document.get_assets"
TOOL_GET_TABLE_MAP = "document.get_table_map"
TOOL_QUERY_PARSER_FACTS = "parser.query_facts"
TOOL_VALIDATE_STAGE = "proposal.validate_stage"

D1_TOOL_NAMES = (
    TOOL_GET_RUN_MANIFEST,
    TOOL_GET_PAGE_LEDGER,
    TOOL_GET_PAGE_FACTS,
    TOOL_GET_PAGE_TEXT,
    TOOL_RENDER_PAGE,
    TOOL_RENDER_REGION,
    TOOL_GET_ASSETS,
    TOOL_GET_TABLE_MAP,
    TOOL_QUERY_PARSER_FACTS,
    TOOL_VALIDATE_STAGE,
)

_VISUAL_RISK_CODES = {
    "pdf_full_page_image_with_invisible_text_layer",
    "pdf_full_page_image_with_low_native_text_coverage",
    "pdf_full_page_image_without_visible_text",
    "pdf_page_without_locatable_content",
    "pdf_vector_text_with_low_native_unicode_coverage",
    "pdf_visual_figure_content_not_evaluated",
    "pdf_visual_page_locator_unavailable",
    "pdf_visual_table_geometry_unavailable",
    "pdf_visual_table_provider_not_configured",
    "pdf_visual_text_provider_not_configured",
}
_RENDER_SCALE = {
    DocumentRenderDetail.THUMBNAIL: 0.75,
    DocumentRenderDetail.HIGH: 2.0,
    DocumentRenderDetail.ORIGINAL: 3.0,
}
_MAX_FULL_PAGE_PIXELS = 16_000_000


class DocumentEvidenceToolError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class RenderedDocumentEvidence:
    receipt: DocumentRenderReceiptV1
    image_bytes: bytes


@dataclass(frozen=True, slots=True)
class PreparedDocumentEvidence:
    source_path: Path
    manifest: DocumentRunManifestV2
    source_ir: SourceIRSnapshot
    ledger: DocumentPageLedgerV2
    page_facts: dict[int, tuple[DocumentPageFactV1, ...]]
    page_text: dict[int, tuple[DocumentPageTextItemV1, ...]]
    page_tables: dict[int, tuple[str, ...]]
    page_assets: dict[int, tuple[DocumentAssetFactV1, ...]]
    tables_by_id: dict[str, SourceNode]


def prepare_document_evidence(
    source_path: Path,
    *,
    expected_source_sha256: str,
    document_role: str,
    kit_identity: AgentKitIdentityV1,
    stage_context_sha256: str,
    run_id: str | None = None,
    opaque_source_handle: str | None = None,
    intentional_blank_pages: tuple[int, ...] = (),
    source_asset_publisher: Callable[[SourceAssetRef, bytes], None] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> PreparedDocumentEvidence:
    source = source_path.expanduser().resolve(strict=True)
    if source.suffix.lower() != ".pdf" or not source.is_file():
        raise DocumentEvidenceToolError("document_evidence_pdf_required")
    preflight = LocalDocumentPreflight().inspect(source)
    if (
        not preflight.processable
        or preflight.identity.source_sha256 != expected_source_sha256
        or preflight.page_count is None
        or preflight.page_count < 1
    ):
        raise DocumentEvidenceToolError("document_evidence_preflight_mismatch")
    source_ir = build_source_ir(
        source,
        preflight_report=preflight,
        adapters=(
            PdfSourceIRAdapter(source_asset_publisher=source_asset_publisher),
        ),
        clock=clock,
    )
    if source_ir.source_sha256 != expected_source_sha256:
        raise DocumentEvidenceToolError("document_evidence_source_ir_mismatch")
    if source_ir.status is SourceIRStatus.FAILED:
        raise DocumentEvidenceToolError("document_evidence_source_ir_failed")
    geometry = inspect_pdf_page_geometry(
        source,
        expected_source_sha256=expected_source_sha256,
    )
    if len(geometry) != preflight.page_count:
        raise DocumentEvidenceToolError("document_evidence_page_geometry_mismatch")

    page_facts: dict[int, tuple[DocumentPageFactV1, ...]] = {}
    page_text: dict[int, tuple[DocumentPageTextItemV1, ...]] = {}
    page_tables: dict[int, tuple[str, ...]] = {}
    page_assets: dict[int, tuple[DocumentAssetFactV1, ...]] = {}
    tables_by_id: dict[str, SourceNode] = {}
    ledger_entries: list[DocumentPageLedgerEntryV2] = []
    for page_number in range(1, preflight.page_count + 1):
        nodes = tuple(
            node
            for node in source_ir.nodes
            if node.native_locator.page_number == page_number
        )
        facts = tuple(_fact_from_node(node) for node in nodes)
        text_items = _text_items_from_nodes(nodes)
        table_ids = tuple(
            sorted(
                node.table_payload.table_id
                for node in nodes
                if node.table_payload is not None
            )
        )
        for node in nodes:
            if node.table_payload is not None:
                tables_by_id[node.table_payload.table_id] = node
        assets = tuple(
            sorted(
                (
                    _asset_fact(asset)
                    for asset in source_ir.assets
                    if asset.native_locator is not None
                    and asset.native_locator.page_number == page_number
                ),
                key=lambda item: item.asset_id,
            )
        )
        diagnostic_codes = _page_diagnostic_codes(
            source_ir,
            page_number=page_number,
            nodes=nodes,
        )
        native_text_char_count = sum(len(item.text) for item in text_items)
        evidence_requirement = _page_evidence_requirement(
            page_number=page_number,
            intentional_blank_pages=intentional_blank_pages,
            native_node_count=len(nodes),
            native_text_char_count=native_text_char_count,
            diagnostic_codes=diagnostic_codes,
        )
        facts_sha = canonical_json_sha256(
            [item.model_dump(mode="json") for item in facts]
        )
        text_sha = canonical_json_sha256(
            [item.model_dump(mode="json") for item in text_items]
        )
        page_facts[page_number] = facts
        page_text[page_number] = text_items
        page_tables[page_number] = table_ids
        page_assets[page_number] = assets
        page_geometry = geometry[page_number - 1]
        ledger_entries.append(
            DocumentPageLedgerEntryV2(
                page_number=page_number,
                bounds=page_geometry.bounds,
                rotation_cw_degrees=page_geometry.rotation_cw_degrees,
                evidence_requirement=evidence_requirement,
                native_node_count=len(nodes),
                native_text_char_count=native_text_char_count,
                table_ids=table_ids,
                asset_ids=tuple(item.asset_id for item in assets),
                diagnostic_codes=diagnostic_codes,
                visual_review_reasons=tuple(
                    sorted(
                        {
                            *(
                                ("physical_table_layout",)
                                if table_ids
                                else ()
                            ),
                            *(("visual_asset_present",) if assets else ()),
                            *(
                                ("hard_visual_authority",)
                                if evidence_requirement
                                in {
                                    DocumentEvidenceRequirement.VISUAL_REQUIRED,
                                    DocumentEvidenceRequirement.NATIVE_AND_VISUAL,
                                }
                                else ()
                            ),
                        }
                    )
                ),
                page_facts_sha256=facts_sha,
                page_text_sha256=text_sha,
            )
        )
    entries = tuple(ledger_entries)
    ledger = DocumentPageLedgerV2(
        source_sha256=expected_source_sha256,
        page_count=preflight.page_count,
        pages=entries,
        ledger_sha256=document_page_ledger_sha256(
            source_sha256=expected_source_sha256,
            page_count=preflight.page_count,
            pages=entries,
        ),
    )
    page_manifest_sha256 = canonical_json_sha256(
        {
            "source_sha256": expected_source_sha256,
            "geometry": [
                {
                    "page_number": item.page_number,
                    "bounds": item.bounds,
                    "rotation_cw_degrees": item.rotation_cw_degrees,
                }
                for item in geometry
            ],
            "intentional_blank_pages": list(intentional_blank_pages),
        }
    )
    manifest = DocumentRunManifestV2(
        run_id=run_id or f"agent-run-{secrets.token_hex(16)}",
        opaque_source_handle=(
            opaque_source_handle or f"source-handle-{secrets.token_hex(16)}"
        ),
        source_sha256=expected_source_sha256,
        page_count=preflight.page_count,
        document_role=document_role,
        source_ir_id=source_ir.source_ir_id,
        source_ir_status=source_ir.status.value,
        source_adapter_id=source_ir.adapter_id,
        source_adapter_version=source_ir.adapter_version,
        page_manifest_sha256=page_manifest_sha256,
        stage_context_sha256=stage_context_sha256,
        kit_identity=kit_identity,
        intentional_blank_pages=intentional_blank_pages,
    )
    return PreparedDocumentEvidence(
        source_path=source,
        manifest=manifest,
        source_ir=source_ir,
        ledger=ledger,
        page_facts=page_facts,
        page_text=page_text,
        page_tables=page_tables,
        page_assets=page_assets,
        tables_by_id=tables_by_id,
    )


class DocumentEvidenceToolHost:
    """Stateful D1 host. It exposes evidence and validation, never business writes."""

    def __init__(self, prepared: PreparedDocumentEvidence) -> None:
        self.prepared = prepared
        self.call_counts: dict[str, int] = {}
        self._manifest_read = False
        self._ledger_read = False
        self._fact_indices_read: dict[int, set[int]] = {}
        self._text_indices_read: dict[int, set[int]] = {}
        self._fact_pages_touched: set[int] = set()
        self._text_pages_touched: set[int] = set()
        self._render_receipts: dict[str, DocumentRenderReceiptV1] = {}

    def get_run_manifest(
        self,
        *,
        source_handle: str,
        stage_context_sha256: str | None = None,
        kit_bundle_sha256: str | None = None,
    ) -> DocumentRunManifestV2:
        self._claim(TOOL_GET_RUN_MANIFEST, ceiling=2)
        self._require_source_handle(source_handle)
        if stage_context_sha256 is not None and (
            stage_context_sha256 != self.prepared.manifest.stage_context_sha256
        ):
            raise DocumentEvidenceToolError("document_stage_context_identity_mismatch")
        if kit_bundle_sha256 is not None and (
            kit_bundle_sha256 != self.prepared.manifest.kit_identity.bundle_sha256
        ):
            raise DocumentEvidenceToolError("document_agent_kit_identity_mismatch")
        self._manifest_read = True
        return self.prepared.manifest

    def get_page_ledger(self, *, source_handle: str) -> DocumentPageLedgerV2:
        self._claim(TOOL_GET_PAGE_LEDGER, ceiling=2)
        self._require_source_handle(source_handle)
        if not self._manifest_read:
            raise DocumentEvidenceToolError("document_manifest_must_be_read_first")
        self._ledger_read = True
        return self.prepared.ledger

    def get_page_facts(
        self,
        *,
        source_handle: str,
        page_number: int,
        offset: int = 0,
        limit: int = 128,
    ) -> DocumentPageFactsChunkV1:
        self._require_read_prelude(source_handle)
        page = self._page(page_number)
        offset, limit = _validated_window(offset, limit, max_limit=1024)
        items = self.prepared.page_facts[page_number]
        if offset > len(items):
            raise DocumentEvidenceToolError("document_page_facts_offset_invalid")
        selected = items[offset : offset + limit]
        self._fact_pages_touched.add(page_number)
        self._fact_indices_read.setdefault(page_number, set()).update(
            range(offset, offset + len(selected))
        )
        next_offset = offset + len(selected)
        if next_offset >= len(items):
            next_offset = None
        self._claim(
            TOOL_GET_PAGE_FACTS,
            ceiling=sum(max(2, len(page_items) + 2) for page_items in self.prepared.page_facts.values()),
        )
        return DocumentPageFactsChunkV1(
            source_sha256=self.prepared.manifest.source_sha256,
            page_number=page_number,
            page_facts_sha256=page.page_facts_sha256,
            offset=offset,
            items=selected,
            next_offset=next_offset,
        )

    def get_page_text(
        self,
        *,
        source_handle: str,
        page_number: int,
        offset: int = 0,
        limit: int = 64,
    ) -> DocumentPageTextChunkV1:
        self._require_read_prelude(source_handle)
        page = self._page(page_number)
        offset, limit = _validated_window(offset, limit, max_limit=512)
        items = self.prepared.page_text[page_number]
        if offset > len(items):
            raise DocumentEvidenceToolError("document_page_text_offset_invalid")
        selected = items[offset : offset + limit]
        self._text_pages_touched.add(page_number)
        self._text_indices_read.setdefault(page_number, set()).update(
            range(offset, offset + len(selected))
        )
        next_offset = offset + len(selected)
        if next_offset >= len(items):
            next_offset = None
        self._claim(
            TOOL_GET_PAGE_TEXT,
            ceiling=sum(max(2, len(page_items) + 2) for page_items in self.prepared.page_text.values()),
        )
        return DocumentPageTextChunkV1(
            source_sha256=self.prepared.manifest.source_sha256,
            page_number=page_number,
            page_text_sha256=page.page_text_sha256,
            offset=offset,
            items=selected,
            next_offset=next_offset,
        )

    def query_parser_facts(
        self,
        *,
        source_handle: str,
        page_number: int,
        offset: int = 0,
        limit: int = 64,
    ) -> DocumentPageFactsChunkV1:
        self._claim(TOOL_QUERY_PARSER_FACTS, ceiling=256)
        return self.get_page_facts(
            source_handle=source_handle,
            page_number=page_number,
            offset=offset,
            limit=limit,
        )

    def get_assets(
        self,
        *,
        source_handle: str,
        page_number: int,
    ) -> DocumentAssetLedgerV1:
        self._require_read_prelude(source_handle)
        self._page(page_number)
        self._claim(TOOL_GET_ASSETS, ceiling=self.prepared.manifest.page_count * 2)
        assets = self.prepared.page_assets[page_number]
        return DocumentAssetLedgerV1(
            source_sha256=self.prepared.manifest.source_sha256,
            page_number=page_number,
            assets=assets,
            assets_sha256=canonical_json_sha256(
                [asset.model_dump(mode="json") for asset in assets]
            ),
        )

    def get_table_map(
        self,
        *,
        source_handle: str,
        table_id: str,
        include_cells: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> DocumentTableMapChunkV1:
        self._require_read_prelude(source_handle)
        offset, limit = _validated_window(offset, limit, max_limit=200)
        node = self.prepared.tables_by_id.get(table_id)
        if node is None or node.table_payload is None:
            raise DocumentEvidenceToolError("document_table_unknown")
        table = node.table_payload
        cells = tuple(_table_cell_fact(cell) for cell in table.cells)
        if offset > len(cells):
            raise DocumentEvidenceToolError("document_table_offset_invalid")
        selected = cells[offset : offset + limit] if include_cells else ()
        next_offset = offset + len(selected)
        if not include_cells or next_offset >= len(cells):
            next_offset = None
        self._claim(TOOL_GET_TABLE_MAP, ceiling=max(2, len(self.prepared.tables_by_id) * 16))
        table_payload = {
            "table_id": table_id,
            "page_number": node.native_locator.page_number,
            "bbox": node.native_locator.bbox,
            "row_count": table.row_count,
            "column_count": table.column_count,
            "header_regions": [
                (item.row, item.column, item.row_span, item.column_span)
                for item in table.header_regions
            ],
            "merged_regions": [
                (item.row, item.column, item.row_span, item.column_span)
                for item in table.merged_regions
            ],
            "notes": list(table.notes),
            "cells": [item.model_dump(mode="json") for item in cells],
        }
        return DocumentTableMapChunkV1(
            source_sha256=self.prepared.manifest.source_sha256,
            table_id=table_id,
            page_number=node.native_locator.page_number or 1,
            bbox=node.native_locator.bbox,
            row_count=table.row_count,
            column_count=table.column_count,
            header_regions=tuple(
                (item.row, item.column, item.row_span, item.column_span)
                for item in table.header_regions
            ),
            merged_regions=tuple(
                (item.row, item.column, item.row_span, item.column_span)
                for item in table.merged_regions
            ),
            notes=table.notes,
            table_map_sha256=canonical_json_sha256(table_payload),
            cells_included=include_cells,
            offset=offset,
            cells=selected,
            next_offset=next_offset,
        )

    def render_page(
        self,
        *,
        source_handle: str,
        page_number: int,
        detail: DocumentRenderDetail,
    ) -> RenderedDocumentEvidence:
        self._require_read_prelude(source_handle)
        page = self._page(page_number)
        if detail is DocumentRenderDetail.REGION:
            raise DocumentEvidenceToolError("document_page_render_detail_invalid")
        scale = _RENDER_SCALE[detail]
        width_points = page.bounds[2] - page.bounds[0]
        height_points = page.bounds[3] - page.bounds[1]
        if width_points * scale * height_points * scale > _MAX_FULL_PAGE_PIXELS:
            raise DocumentEvidenceToolError("document_render_budget_exceeded")
        self._claim(TOOL_RENDER_PAGE, ceiling=self.prepared.manifest.page_count * 4)
        rendered = render_pdf_visual_page_input(
            self.prepared.source_path,
            page_number=page_number,
            scale=scale,
        )
        if rendered.source_sha256 != self.prepared.manifest.source_sha256:
            raise DocumentEvidenceToolError("document_render_source_identity_drift")
        receipt = _render_receipt(
            source_sha256=self.prepared.manifest.source_sha256,
            page_number=page_number,
            detail=detail,
            bbox=page.bounds,
            scale=scale,
            width_px=rendered.page_image_width_px,
            height_px=rendered.page_image_height_px,
            image_sha256=rendered.page_image_sha256,
        )
        self._render_receipts[receipt.render_receipt_id] = receipt
        return RenderedDocumentEvidence(
            receipt=receipt,
            image_bytes=bytes(rendered.page_image_bytes),
        )

    def render_region(
        self,
        *,
        source_handle: str,
        page_number: int,
        bbox: tuple[float, float, float, float],
        scale: float = 3.0,
    ) -> RenderedDocumentEvidence:
        self._require_read_prelude(source_handle)
        self._page(page_number)
        self._claim(TOOL_RENDER_REGION, ceiling=64)
        image_bytes = render_pdf_source_crop(
            self.prepared.source_path,
            page_number=page_number,
            bbox=bbox,
            expected_source_sha256=self.prepared.manifest.source_sha256,
            scale=scale,
        )
        with Image.open(BytesIO(image_bytes)) as image:
            width_px, height_px = image.size
        image_sha256 = hashlib.sha256(image_bytes).hexdigest()
        receipt = _render_receipt(
            source_sha256=self.prepared.manifest.source_sha256,
            page_number=page_number,
            detail=DocumentRenderDetail.REGION,
            bbox=bbox,
            scale=float(scale),
            width_px=width_px,
            height_px=height_px,
            image_sha256=image_sha256,
        )
        self._render_receipts[receipt.render_receipt_id] = receipt
        return RenderedDocumentEvidence(receipt=receipt, image_bytes=image_bytes)

    def validate_understanding(
        self,
        proposal: DocumentUnderstandingProposalV2,
    ) -> DocumentUnderstandingValidationReceiptV2:
        self._claim(TOOL_VALIDATE_STAGE, ceiling=2)
        manifest = self.prepared.manifest
        ledger = self.prepared.ledger
        if not self._manifest_read or not self._ledger_read:
            raise DocumentEvidenceToolError("document_evidence_prelude_incomplete")
        if (
            proposal.run_id != manifest.run_id
            or proposal.source_sha256 != manifest.source_sha256
            or proposal.page_count != manifest.page_count
            or proposal.ledger_sha256 != ledger.ledger_sha256
        ):
            raise DocumentEvidenceToolError("document_understanding_identity_mismatch")
        unresolved_pages: list[int] = []
        for ledger_page, observed in zip(ledger.pages, proposal.pages, strict=True):
            page_number = ledger_page.page_number
            if (
                observed.page_facts_sha256 != ledger_page.page_facts_sha256
                or observed.page_text_sha256 != ledger_page.page_text_sha256
                or observed.table_ids != ledger_page.table_ids
                or observed.asset_ids != ledger_page.asset_ids
            ):
                raise DocumentEvidenceToolError(
                    "document_understanding_page_evidence_mismatch"
                )
            if ledger_page.evidence_requirement is not DocumentEvidenceRequirement.INTENTIONAL_BLANK:
                if page_number not in self._fact_pages_touched or self._fact_indices_read.get(page_number, set()) != set(
                    range(len(self.prepared.page_facts[page_number]))
                ):
                    raise DocumentEvidenceToolError(
                        "document_understanding_page_facts_incomplete"
                    )
                if page_number not in self._text_pages_touched or self._text_indices_read.get(page_number, set()) != set(
                    range(len(self.prepared.page_text[page_number]))
                ):
                    raise DocumentEvidenceToolError(
                        "document_understanding_page_text_incomplete"
                    )
            self._validate_page_authority(ledger_page, observed)
            self._validate_visual_observations(ledger_page, observed)
            if observed.unresolved_codes:
                unresolved_pages.append(page_number)
        expected_status = (
            DocumentUnderstandingStatus.PARTIAL
            if unresolved_pages
            else DocumentUnderstandingStatus.COMPLETE
        )
        if proposal.status is not expected_status:
            raise DocumentEvidenceToolError("document_understanding_status_mismatch")
        proposal_sha256 = canonical_json_sha256(proposal.model_dump(mode="json"))
        evidence_sha256 = canonical_json_sha256(
            {
                "manifest_sha256": canonical_json_sha256(
                    manifest.model_dump(mode="json")
                ),
                "ledger_sha256": ledger.ledger_sha256,
                "render_receipts": [
                    item.model_dump(mode="json")
                    for item in sorted(
                        self._render_receipts.values(),
                        key=lambda value: value.render_receipt_id,
                    )
                ],
            }
        )
        receipt_payload = {
            "run_id": manifest.run_id,
            "source_sha256": manifest.source_sha256,
            "kit_bundle_sha256": manifest.kit_identity.bundle_sha256,
            "status": proposal.status,
            "page_count": manifest.page_count,
            "unresolved_pages": tuple(unresolved_pages),
            "proposal_sha256": proposal_sha256,
            "evidence_sha256": evidence_sha256,
        }
        return DocumentUnderstandingValidationReceiptV2(
            **receipt_payload,
            receipt_sha256=canonical_json_sha256(
                DocumentUnderstandingValidationReceiptV2.model_construct(
                    **receipt_payload,
                    receipt_sha256="0" * 64,
                ).model_dump(mode="json", exclude={"receipt_sha256"})
            ),
        )

    def _validate_page_authority(self, ledger_page, observed) -> None:  # type: ignore[no-untyped-def]
        allowed = {
            DocumentEvidenceRequirement.NATIVE: {
                DocumentEvidenceAuthority.NATIVE,
                DocumentEvidenceAuthority.NATIVE_AND_VISUAL,
            },
            DocumentEvidenceRequirement.VISUAL_REQUIRED: {
                DocumentEvidenceAuthority.VISUAL,
                DocumentEvidenceAuthority.NATIVE_AND_VISUAL,
                DocumentEvidenceAuthority.UNRESOLVED,
            },
            DocumentEvidenceRequirement.NATIVE_AND_VISUAL: {
                DocumentEvidenceAuthority.NATIVE_AND_VISUAL,
                DocumentEvidenceAuthority.UNRESOLVED,
            },
            DocumentEvidenceRequirement.INTENTIONAL_BLANK: {
                DocumentEvidenceAuthority.INTENTIONAL_BLANK,
            },
            DocumentEvidenceRequirement.UNRESOLVED: {
                DocumentEvidenceAuthority.UNRESOLVED,
            },
        }[ledger_page.evidence_requirement]
        if observed.authority not in allowed:
            raise DocumentEvidenceToolError(
                "document_understanding_page_authority_invalid"
            )
        needs_visual = ledger_page.evidence_requirement in {
            DocumentEvidenceRequirement.VISUAL_REQUIRED,
            DocumentEvidenceRequirement.NATIVE_AND_VISUAL,
        }
        if needs_visual and observed.authority is not DocumentEvidenceAuthority.UNRESOLVED:
            if not observed.visual_observations:
                raise DocumentEvidenceToolError(
                    "document_understanding_visual_observation_missing"
                )

    def _validate_visual_observations(self, ledger_page, observed) -> None:  # type: ignore[no-untyped-def]
        receipts = tuple(
            receipt
            for receipt in self._render_receipts.values()
            if receipt.page_number == ledger_page.page_number
        )
        image_hashes = {receipt.image_sha256 for receipt in receipts}
        page_source_handles = {
            item.source_handle
            for item in self.prepared.page_facts[ledger_page.page_number]
        } | {
            item.source_handle
            for item in self.prepared.page_text[ledger_page.page_number]
        }
        for observation in observed.visual_observations:
            if observation.image_sha256 not in image_hashes:
                raise DocumentEvidenceToolError(
                    "document_understanding_visual_image_unread"
                )
            if not set(observation.source_handles).issubset(page_source_handles):
                raise DocumentEvidenceToolError(
                    "document_understanding_visual_source_handle_invalid"
                )
            if observation.bbox is not None and not _bbox_within(
                observation.bbox,
                ledger_page.bounds,
            ):
                raise DocumentEvidenceToolError(
                    "document_understanding_visual_bbox_invalid"
                )

    def _require_read_prelude(self, source_handle: str) -> None:
        self._require_source_handle(source_handle)
        if not self._manifest_read or not self._ledger_read:
            raise DocumentEvidenceToolError("document_evidence_prelude_incomplete")

    def _require_source_handle(self, source_handle: str) -> None:
        if source_handle != self.prepared.manifest.opaque_source_handle:
            raise DocumentEvidenceToolError("document_source_handle_unknown")

    def _page(self, page_number: int) -> DocumentPageLedgerEntryV2:
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or not 1 <= page_number <= self.prepared.manifest.page_count
        ):
            raise DocumentEvidenceToolError("document_page_number_invalid")
        return self.prepared.ledger.pages[page_number - 1]

    def _claim(self, tool_name: str, *, ceiling: int) -> None:
        count = self.call_counts.get(tool_name, 0) + 1
        self.call_counts[tool_name] = count
        if count > ceiling:
            raise DocumentEvidenceToolError("document_tool_budget_exceeded")


def _fact_from_node(node: SourceNode) -> DocumentPageFactV1:
    return DocumentPageFactV1(
        source_handle=node.source_node_id,
        source_order=node.source_order,
        kind=node.kind.value,
        bbox=node.native_locator.bbox,
        parent_source_handle=node.parent_source_node_id,
        table_id=(node.table_payload.table_id if node.table_payload is not None else None),
        asset_id=node.asset_ref,
        raw_text_length=len(node.raw_text),
        raw_text_sha256=hashlib.sha256(node.raw_text.encode("utf-8")).hexdigest(),
        diagnostic_codes=tuple(
            sorted({item.code for item in node.extraction_diagnostics})
        ),
    )


def _text_items_from_nodes(
    nodes: tuple[SourceNode, ...],
) -> tuple[DocumentPageTextItemV1, ...]:
    items: list[DocumentPageTextItemV1] = []
    for node in nodes:
        if node.table_payload is not None:
            # D1 inventories the complete physical table and its bound digest.
            # Cell text/merges/notes are consumed in D2 logical-structure work.
            continue
        if node.raw_text:
            items.append(
                DocumentPageTextItemV1(
                    source_handle=node.source_node_id,
                    kind=node.kind.value,
                    text=node.raw_text,
                    text_sha256=hashlib.sha256(
                        node.raw_text.encode("utf-8")
                    ).hexdigest(),
                    bbox=node.native_locator.bbox,
                )
            )
    return tuple(items)


def _asset_fact(asset: SourceAssetRef) -> DocumentAssetFactV1:
    locator = asset.native_locator
    return DocumentAssetFactV1(
        asset_id=asset.asset_id,
        kind=asset.kind.value,
        content_sha256=asset.content_sha256,
        media_type=asset.media_type,
        page_number=(locator.page_number if locator is not None else None),
        bbox=(locator.bbox if locator is not None else None),
    )


def _table_cell_fact(cell) -> DocumentTableCellFactV1:  # type: ignore[no-untyped-def]
    return DocumentTableCellFactV1(
        cell_id=cell.cell_id,
        row=cell.row,
        column=cell.column,
        row_span=cell.row_span,
        column_span=cell.column_span,
        text=cell.raw_text,
        text_sha256=hashlib.sha256(cell.raw_text.encode("utf-8")).hexdigest(),
        bbox=cell.native_locator.bbox,
    )


def _page_diagnostic_codes(
    source_ir: SourceIRSnapshot,
    *,
    page_number: int,
    nodes: tuple[SourceNode, ...],
) -> tuple[str, ...]:
    codes = {
        diagnostic.code
        for diagnostic in source_ir.diagnostics
        if diagnostic.native_locator is not None
        and diagnostic.native_locator.page_number == page_number
    }
    codes.update(
        diagnostic.code
        for node in nodes
        for diagnostic in node.extraction_diagnostics
    )
    return tuple(sorted(codes))


def _page_evidence_requirement(
    *,
    page_number: int,
    intentional_blank_pages: tuple[int, ...],
    native_node_count: int,
    native_text_char_count: int,
    diagnostic_codes: tuple[str, ...],
) -> DocumentEvidenceRequirement:
    if page_number in intentional_blank_pages:
        return DocumentEvidenceRequirement.INTENTIONAL_BLANK
    visual_required = bool(set(diagnostic_codes) & _VISUAL_RISK_CODES)
    if visual_required and native_text_char_count > 0:
        return DocumentEvidenceRequirement.NATIVE_AND_VISUAL
    if visual_required:
        return DocumentEvidenceRequirement.VISUAL_REQUIRED
    if native_node_count or native_text_char_count:
        return DocumentEvidenceRequirement.NATIVE
    return DocumentEvidenceRequirement.UNRESOLVED


def _validated_window(offset: int, limit: int, *, max_limit: int) -> tuple[int, int]:
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= max_limit
    ):
        raise DocumentEvidenceToolError("document_tool_window_invalid")
    return offset, limit


def _render_receipt(
    *,
    source_sha256: str,
    page_number: int,
    detail: DocumentRenderDetail,
    bbox: tuple[float, float, float, float],
    scale: float,
    width_px: int,
    height_px: int,
    image_sha256: str,
) -> DocumentRenderReceiptV1:
    identity = {
        "source_sha256": source_sha256,
        "page_number": page_number,
        "detail": detail.value,
        "bbox": bbox,
        "scale": scale,
        "width_px": width_px,
        "height_px": height_px,
        "image_sha256": image_sha256,
    }
    return DocumentRenderReceiptV1(
        render_receipt_id=(
            "document-render-" + canonical_json_sha256(identity)[:32]
        ),
        **identity,
    )


def _bbox_within(
    bbox: tuple[float, float, float, float],
    page_bounds: tuple[float, float, float, float],
) -> bool:
    x0, y0, x1, y1 = bbox
    px0, py0, px1, py1 = page_bounds
    return (
        all(math.isfinite(item) for item in bbox)
        and x1 > x0
        and y1 > y0
        and x0 >= px0 - 0.5
        and y0 >= py0 - 0.5
        and x1 <= px1 + 0.5
        and y1 <= py1 + 0.5
    )


__all__ = [
    "D1_TOOL_NAMES",
    "DocumentEvidenceToolError",
    "DocumentEvidenceToolHost",
    "PreparedDocumentEvidence",
    "RenderedDocumentEvidence",
    "TOOL_GET_ASSETS",
    "TOOL_GET_PAGE_FACTS",
    "TOOL_GET_PAGE_LEDGER",
    "TOOL_GET_PAGE_TEXT",
    "TOOL_GET_RUN_MANIFEST",
    "TOOL_GET_TABLE_MAP",
    "TOOL_QUERY_PARSER_FACTS",
    "TOOL_RENDER_PAGE",
    "TOOL_RENDER_REGION",
    "TOOL_VALIDATE_STAGE",
    "prepare_document_evidence",
]
