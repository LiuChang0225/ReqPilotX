from __future__ import annotations

from base64 import b64decode
import hashlib
import json
import re
import sys
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable

import xlsxwriter
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from reqpilotx.adapters.documents.pdf_source_crop_renderer import (
    PdfSourceCropRejected,
    render_pdf_source_crop,
)
from reqpilotx.domain.requirement_matrix import (
    MatrixConclusionSource,
    RequirementMatrixSnapshotV1,
    has_incomplete_terminal_conclusion,
    is_analysis_target,
    matrix_conclusion_contract,
)
from reqpilotx.services.table_assets import load_snapshot_bytes, load_table_font

_TABLE_FONT_SIZE = 13
_TABLE_CELL_PADDING_X = 12
_TABLE_CELL_PADDING_Y = 8
_TABLE_LINE_SPACING = 4
_TABLE_UNIT_PX = 7
_TABLE_MIN_COLUMN_UNITS = 10
_WORKBOOK_FONT_SIZE = 11
_WORKBOOK_TEXT_PADDING_X = 10
_WORKBOOK_TEXT_PADDING_Y = 6
_DEFAULT_ROW_HEIGHT_POINTS = 22.0
_MAX_ROW_HEIGHT_POINTS = 409.5
_MAX_ROW_HEIGHT_PX = int(_MAX_ROW_HEIGHT_POINTS / 0.75)
_MAX_IMAGE_ROW_RENDER_HEIGHT_PX = max(120, _MAX_ROW_HEIGHT_PX - 12)
_MIN_SINGLE_IMAGE_SCALE = 0.55
_MAX_SOURCE_PACKAGE_ASSET_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_PACKAGE_SCAN_BYTES = 128 * 1024 * 1024
_WORKBOOK_TEXT_LINE_HEIGHT_POINTS = 18.0
_WORKBOOK_TEXT_ROW_EXTRA_POINTS = 8.0
_MATRIX_MAX_VISIBLE_ROW_HEIGHT_POINTS = 96.0
_MATRIX_COMPACT_PREVIEW_HEIGHT_POINTS = 96.0
_EXCEL_MAX_CELL_CHARACTERS = 32_767
_CUSTOMER_REQUIREMENT_COLUMN_WIDTH = 130.0
_REQUIREMENT_FIXED_COLUMN_WIDTHS = {
    "A": 14.0,
    "B": 24.0,
    "D": 22.0,
    "E": 18.0,
    "F": 40.0,
}
_BRAND_PRIMARY = "#00893D"
_BRAND_SECONDARY = "#005E46"
_BRAND_TEXT = "#343434"
_BRAND_TEXT_SECONDARY = "#646464"
_BRAND_BORDER = "#D0D0D0"
_BRAND_BACKGROUND = "#F6F6F6"
_BRAND_HEADER_BACKGROUND = "#EBEBEB"
_BRAND_ROW_ALT = "#FCFBFB"
_BRAND_HEADER_TEXT = "#FFFFFF"
_MATRIX_OVERVIEW_SHEET_NAME = "00_清单概览"
_MATRIX_SHEET_NAME = "ComplianceMatrix"
_MATRIX_COLUMN_WIDTHS = (
    11.0,  # Req ID within this Requirement Set
    10.0,  # direct source number
    22.0,  # chapter path and title
    100.0,  # requirement body
    14.0,  # source/table/image entry
    9.0,  # analysis scope
    11.0,  # progress
    12.0,  # current conclusion
    23.0,  # basis and conclusion provenance
    12.0,  # customer confirmation
)
_MATRIX_CONTENT_COLUMN_INDEX = 3
_MATRIX_CONTENT_COLUMN_WIDTH = _MATRIX_COLUMN_WIDTHS[_MATRIX_CONTENT_COLUMN_INDEX]
_MATRIX_OVERVIEW_COLUMN_WIDTHS = (
    18.0,
    12.0,
    25.0,
    52.0,
    9.0,
    13.0,
    14.0,
    20.0,
    20.0,
    11.0,
    13.0,
)
_MATRIX_FULL_TEXT_SHEET_NAME = "全文详情"
_MATRIX_FIGURE_GAP_PX = 6
_MATRIX_FIGURE_PADDING_PX = 4
_MATRIX_FIGURE_MIN_SLOT_WIDTH_PX = 96
_MATRIX_FIGURE_MAX_SLOT_HEIGHT_PX = 120
_BRAND_LOGO_PATH_CANDIDATES = (
    Path(str(files("reqpilotx.resources").joinpath("branding/schaeffler-logo.png"))),
    Path(
        str(
            files("reqpilotx.resources").joinpath(
                "branding/schaeffler-logo.png.base64"
            )
        )
    ),
    Path.home()
    / ".codex"
    / "skills"
    / "schaeffler-frontend-generator"
    / "assets"
    / "logos"
    / "schaeffler-logo.png",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ASSET_KEY_PATTERN = re.compile(
    r"^source-assets/(?P<prefix>[0-9a-f]{2})/"
    r"(?P<sha256>[0-9a-f]{64})\.(?P<extension>[a-z0-9]{1,12})$"
)
_SOURCE_PAGE_RENDER_KEY_PATTERN = re.compile(
    r"^source-page-renders/(?P<prefix>[0-9a-f]{2})/"
    r"(?P<source_sha256>[0-9a-f]{64})/page-(?P<page>[0-9]{6})-"
    r"(?P<sha256>[0-9a-f]{64})\.png$"
)


@dataclass
class _RequirementBlockSplit:
    blocks: list[tuple[str, Any]]
    table_fallback_text_count: int = 0


@dataclass
class _PreparedRequirementRow:
    kind: str
    text: str = ""
    row_height: float = _DEFAULT_ROW_HEIGHT_POINTS
    image_buffer: BytesIO | None = None
    x_scale: float = 1.0
    y_scale: float = 1.0


@dataclass
class _MatrixNodeRowPlan:
    node: dict[str, Any]
    start_row: int
    row_count: int
    title_row: _PreparedRequirementRow
    content_row: _PreparedRequirementRow
    basis_row: _PreparedRequirementRow
    trace_row: _PreparedRequirementRow
    detail_texts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _MatrixOverviewRow:
    node: dict[str, Any]
    roles: tuple[str, ...]
    logical_table_ids: tuple[str, ...]
    figure_refs: tuple[dict[str, Any], ...]
    requirement_nodes: tuple[dict[str, Any], ...]
    object_id: str
    title: str
    source_number: str
    depth: int
    is_figure_evidence: bool = False


@dataclass
class _TableExportPart:
    logical_table_id: str
    physical_table_id: str
    title: str
    source_order: int
    page_start: int | None
    page_end: int | None
    headers: list[str]
    rows: list[list[str]]
    row_start: int | None
    cells: list[dict[str, Any]]
    merged_regions: list[dict[str, int]]
    notes: list[str]
    cell_images: list[dict[str, Any]]
    snapshot_paths: list[str]
    raw_ref: dict[str, Any]


@dataclass
class _TableExportRelation:
    logical_table_id: str
    physical_table_ids: tuple[str, ...]
    projection_id: str
    projection_mode: str
    record_id: str
    requirement_code: str
    atomic_requirement_id: str
    page_start: int | None
    page_end: int | None


@dataclass
class _TableExportCatalog:
    parts_by_logical_id: dict[str, list[_TableExportPart]]
    relations: list[_TableExportRelation]
    first_seen_order: list[str]


@dataclass
class _ExportSourceImageContext:
    """Ephemeral source binding used only while rendering one workbook.

    Immutable table evidence stores portable facts (source hash, page and
    geometry), never a customer-machine path.  The application supplies the
    materialized source path at export time, and the PDF crop renderer verifies
    the exact hash again before returning bytes.
    """

    source_file_path: Path | None
    source_sha256: str
    crop_cache: dict[tuple[int, tuple[float, float, float, float]], bytes]
    asset_cache: dict[str, bytes | None]
    object_asset_cache: dict[tuple[str, str], bytes | None]
    source_asset_loader: Callable[[str, str, int], bytes | None] | None
    source_verified: bool | None
    office_source_cache: dict | None = None


@dataclass
class _WorkbookFormats:
    header: Any
    meta_fill: Any
    brand_bar: Any
    row_even: Any
    row_odd: Any
    paragraph_even: Any
    paragraph_odd: Any
    diff_header: Any
    diff_even: Any
    diff_odd: Any
    meta_label: Any
    meta_value: Any
    decision_formats: dict[str, Any]


def export_compliance_matrix(
    project_name: str,
    customer_name: str,
    export_dir: Path,
    source_file_name: str = "",
    *,
    matrix_snapshot: dict[str, Any] | Any,
    version_label: str = "",
    compare_matrix_snapshot: dict[str, Any] | Any | None = None,
    compare_version_label: str = "",
    export_mode: str = "FORMAL",
    source_file_path: str | Path = "",
    source_sha256: str = "",
    source_asset_loader: Callable[[str, str, int], bytes | None] | None = None,
    image_export_receipt: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Render the hierarchy-preserving workbook from one frozen Matrix view."""

    export_dir.mkdir(parents=True, exist_ok=True)
    # Include sub-second time and a short nonce so rapid consecutive exports
    # cannot overwrite one another on disk.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    unique_suffix = uuid.uuid4().hex[:8]
    safe_version = _slug(version_label) if version_label else ""
    normalized_mode = str(export_mode or "DRAFT").strip().upper()
    mode_suffix = "_DRAFT" if normalized_mode == "DRAFT" else ""
    suffix = f"_{safe_version}" if safe_version else ""
    file_name = (
        f"{_slug(project_name)}_compliance_matrix{suffix}{mode_suffix}_"
        f"{timestamp}_{unique_suffix}.xlsx"
    )
    file_path = export_dir / file_name

    snapshot = _normalize_matrix_snapshot(
        matrix_snapshot=matrix_snapshot,
    )
    nodes = _visible_matrix_nodes(snapshot)
    table_details = _matrix_table_details(snapshot, nodes)
    overview_rows = [] if snapshot.get("source_item_baseline") else _build_matrix_overview_rows(
        nodes,
        table_details=table_details,
    )
    table_catalog = _build_matrix_table_catalog(snapshot, nodes)
    row_plans = [] if snapshot.get("source_item_baseline") else _prepare_matrix_row_plans(
        nodes,
        table_details=table_details,
        table_parts_by_logical_id=table_catalog.parts_by_logical_id,
    )
    matrix_row_by_node = {
        str(plan.node.get("node_id") or ""): plan.start_row + 1
        for plan in row_plans
        if str(plan.node.get("node_id") or "")
    }
    matrix_row_span_by_node = {
        str(plan.node.get("node_id") or ""): (
            plan.start_row + 1,
            plan.start_row + plan.row_count,
        )
        for plan in row_plans
        if str(plan.node.get("node_id") or "")
    }
    unresolved_figure_reference_count = sum(
        1
        for node in nodes
        for figure in _matrix_node_figure_refs(node)
        if not _matrix_figure_has_resolvable_source(figure)
    )
    _validate_matrix_export_text_limits(
        row_plans=row_plans,
        table_catalog=table_catalog,
        table_details=table_details,
    )

    source_image_context = _build_export_source_image_context(
        source_file_path=source_file_path,
        source_sha256=source_sha256,
        source_asset_loader=source_asset_loader,
    )
    expected_source_images = _preflight_source_item_images(nodes, table_catalog, source_image_context) if snapshot.get('source_item_baseline') else []
    workbook = xlsxwriter.Workbook(
        str(file_path),
        {
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )
    formats = _build_formats(workbook)
    if snapshot.get("source_item_baseline"):
        from reqpilotx.services.response_matrix import write_response_workbook
        try:
            rendered = write_response_workbook(workbook=workbook, formats=formats, snapshot=snapshot, nodes=nodes,
                table_details=table_details, table_catalog=table_catalog, source_image_context=source_image_context,
                project_name=project_name, customer_name=customer_name, source_file_name=source_file_name,
                version_label=version_label, export_mode=normalized_mode, compare_version_label=compare_version_label,
                compare_matrix_snapshot=compare_matrix_snapshot)
            workbook.close()
            checked_images = _verify_source_item_image_package(file_path, expected_source_images, rendered)
            if image_export_receipt is not None:
                image_export_receipt.update(checked_images)
        except Exception:
            if not workbook.fileclosed:
                workbook.close()
            file_path.unlink(missing_ok=True)
            raise
        return file_name, str(file_path)
    overview_sheet = workbook.add_worksheet(_MATRIX_OVERVIEW_SHEET_NAME)
    overview_sheet.hide_gridlines(2)
    matrix_sheet = workbook.add_worksheet(_MATRIX_SHEET_NAME)
    matrix_sheet.hide_gridlines(2)

    used_sheet_names: set[str] = {
        _MATRIX_OVERVIEW_SHEET_NAME,
        _MATRIX_SHEET_NAME,
        _MATRIX_FULL_TEXT_SHEET_NAME,
        "_ReqPilotXMeta",
    }
    table_sheet_names: dict[str, str] = {}
    record_rows_by_table: dict[str, dict[str, int]] = {}
    table_stats = {
        "cell_images": 0,
        "cell_images_inserted": 0,
        "cell_images_failed": 0,
        "fallbacks": 0,
        "warnings": [],
    }
    for index, logical_table_id in enumerate(table_catalog.first_seen_order, start=1):
        sheet_name = _matrix_table_sheet_name(
            index=index,
            detail=table_details.get(logical_table_id, {}),
            used_names=used_sheet_names,
        )
        used_sheet_names.add(sheet_name)
        table_sheet_names[logical_table_id] = sheet_name

    table_order = list(table_catalog.first_seen_order)
    for index, logical_table_id in enumerate(table_order):
        sheet_name = table_sheet_names[logical_table_id]
        record_rows, stats = _append_matrix_logical_table_sheet(
            workbook=workbook,
            formats=formats,
            snapshot=snapshot,
            logical_table_id=logical_table_id,
            sheet_name=sheet_name,
            parts=table_catalog.parts_by_logical_id.get(logical_table_id, []),
            detail=table_details.get(logical_table_id, {}),
            nodes=nodes,
            matrix_row_by_node=matrix_row_by_node,
            source_image_context=source_image_context,
            previous_sheet_name=(
                table_sheet_names[table_order[index - 1]] if index > 0 else ""
            ),
            next_sheet_name=(
                table_sheet_names[table_order[index + 1]]
                if index + 1 < len(table_order)
                else ""
            ),
        )
        record_rows_by_table[logical_table_id] = record_rows
        table_stats["cell_images"] += int(stats.get("cell_images") or 0)
        table_stats["cell_images_inserted"] += int(
            stats.get("cell_images_inserted") or 0
        )
        table_stats["cell_images_failed"] += int(stats.get("cell_images_failed") or 0)
        table_stats["fallbacks"] += int(stats.get("fallbacks") or 0)
        table_stats["warnings"].extend(stats.get("warnings") or [])

    full_text_rows_by_node = _append_matrix_full_text_sheet(
        workbook=workbook,
        formats=formats,
        row_plans=row_plans,
        matrix_row_by_node=matrix_row_by_node,
        table_sheet_names=table_sheet_names,
        source_image_context=source_image_context,
    )

    _write_matrix_sheet(
        sheet=matrix_sheet,
        workbook=workbook,
        formats=formats,
        project_name=project_name,
        customer_name=customer_name,
        source_file_name=source_file_name,
        export_mode=normalized_mode,
        row_plans=row_plans,
        table_sheet_names=table_sheet_names,
        record_rows_by_table=record_rows_by_table,
        full_text_rows_by_node=full_text_rows_by_node,
        source_image_context=source_image_context,
    )
    _write_matrix_overview_sheet(
        sheet=overview_sheet,
        workbook=workbook,
        formats=formats,
        project_name=project_name,
        customer_name=customer_name,
        source_file_name=source_file_name,
        export_mode=normalized_mode,
        rows=overview_rows,
        matrix_row_by_node=matrix_row_by_node,
        table_sheet_names=table_sheet_names,
        full_text_rows_by_node=full_text_rows_by_node,
        source_image_context=source_image_context,
    )
    _append_hidden_matrix_meta_sheet(
        workbook=workbook,
        formats=formats,
        snapshot=snapshot,
        nodes=nodes,
        matrix_row_by_node=matrix_row_by_node,
        matrix_row_span_by_node=matrix_row_span_by_node,
        table_sheet_names=table_sheet_names,
        project_name=project_name,
        customer_name=customer_name,
        version_label=version_label,
        export_mode=normalized_mode,
        compare_version_label=compare_version_label,
        compare_matrix_snapshot=compare_matrix_snapshot,
        table_stats=table_stats,
        oversized_text_detail_count=sum(1 for plan in row_plans if plan.detail_texts),
        figure_detail_reference_count=sum(
            len(_matrix_node_figure_refs(plan.node)) for plan in row_plans
        ),
        unresolved_figure_reference_count=unresolved_figure_reference_count,
    )

    workbook.close()
    return file_name, str(file_path)


def _normalize_matrix_snapshot(
    *,
    matrix_snapshot: dict[str, Any] | Any,
) -> dict[str, Any]:
    payload = _plain_export_payload(matrix_snapshot)
    if not isinstance(payload, dict):
        raise ValueError("matrix_snapshot must serialize to an object")
    payload = RequirementMatrixSnapshotV1.model_validate(payload).model_dump(
        mode="json"
    )
    if str(payload.get("schema_version") or "") != "requirement-matrix-snapshot/v1":
        raise ValueError("matrix_snapshot must use requirement-matrix-snapshot/v1")
    if str(payload.get("state") or "").strip().upper() != "READY":
        raise ValueError("matrix_snapshot must be READY before export")
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ValueError("matrix_snapshot.nodes must be a list")
    nodes: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise ValueError(f"matrix_snapshot.nodes[{index}] must be an object")
        node = dict(raw)
        node_id = str(node.get("node_id") or "").strip()
        if not node_id:
            raise ValueError(f"matrix_snapshot.nodes[{index}].node_id is required")
        node["node_id"] = node_id
        node["parent_id"] = str(node.get("parent_id") or "").strip() or None
        node["depth"] = max(0, _coerce_int_or_default(node.get("depth"), 0))
        node["source_order"] = max(
            0,
            _coerce_int_or_default(node.get("source_order"), index),
        )
        display_role = str(node.get("display_role") or "").strip().upper()
        matrix_visibility = str(node.get("matrix_visibility") or "").strip().upper()
        disposition = str(node.get("disposition") or "").strip().upper()
        eligibility = str(node.get("analysis_eligibility") or "").strip().upper()
        analysis_status = str(node.get("analysis_status") or "").strip().upper()
        if display_role not in {
            "STRUCTURE",
            "CLAUSE",
            "TABLE",
            "TABLE_RECORD",
            "ATOMIC_REQUIREMENT",
            "CONTEXT",
        }:
            raise ValueError("matrix node display_role is invalid")
        if matrix_visibility not in {
            "VISIBLE",
            "SYSTEM_NON_BUSINESS",
            "HUMAN_EXCLUDED",
        }:
            raise ValueError("matrix node matrix_visibility is invalid")
        if disposition not in {"ANALYZE", "RETAIN", "EXCLUDE"}:
            raise ValueError("matrix node disposition is invalid")
        if eligibility not in {"ELIGIBLE", "NOT_ELIGIBLE"}:
            raise ValueError("matrix node analysis_eligibility is invalid")
        if analysis_status not in {
            "NOT_APPLICABLE",
            "NOT_STARTED",
            "ASSESSED",
            "DECIDED",
        }:
            raise ValueError("matrix node analysis_status is invalid")
        effective_target = (
            matrix_visibility == "VISIBLE"
            and disposition == "ANALYZE"
            and eligibility == "ELIGIBLE"
        )
        if disposition == "ANALYZE" and not effective_target:
            raise ValueError(
                "matrix ANALYZE nodes must have ELIGIBLE analysis eligibility"
            )
        if effective_target and analysis_status == "NOT_APPLICABLE":
            raise ValueError("matrix analysis target cannot be NOT_APPLICABLE")
        if not effective_target and analysis_status != "NOT_APPLICABLE":
            raise ValueError(
                "matrix nodes outside the analysis target must be NOT_APPLICABLE"
            )
        nodes.append(node)
    node_by_id = {node["node_id"]: node for node in nodes}
    if len(node_by_id) != len(nodes):
        raise ValueError("matrix_snapshot node IDs must be unique")
    root_node_ids = payload.get("root_node_ids")
    if not isinstance(root_node_ids, list) or not root_node_ids:
        raise ValueError("matrix_snapshot.root_node_ids must be a non-empty list")
    normalized_roots = [str(value or "").strip() for value in root_node_ids]
    if any(not value for value in normalized_roots):
        raise ValueError("matrix_snapshot root node IDs cannot be blank")
    if len(normalized_roots) != len(set(normalized_roots)):
        raise ValueError("matrix_snapshot root node IDs must be unique")
    if any(value not in node_by_id for value in normalized_roots):
        raise ValueError("matrix_snapshot roots must reference existing nodes")
    for node in nodes:
        parent_id = node.get("parent_id")
        if parent_id is not None and parent_id not in node_by_id:
            raise ValueError("matrix_snapshot node parent does not exist")
        child_ids = node.get("child_ids")
        if not isinstance(child_ids, list):
            raise ValueError("matrix_snapshot node child_ids must be a list")
        normalized_children = [str(value or "").strip() for value in child_ids]
        if any(not value for value in normalized_children):
            raise ValueError("matrix_snapshot child IDs cannot be blank")
        if len(normalized_children) != len(set(normalized_children)):
            raise ValueError("matrix_snapshot child IDs must be unique")
        for child_id in normalized_children:
            child = node_by_id.get(child_id)
            if child is None or child.get("parent_id") != node["node_id"]:
                raise ValueError(
                    "matrix_snapshot parent/child relation is inconsistent"
                )
        node["child_ids"] = normalized_children
    visited: set[str] = set()
    for root_id in normalized_roots:
        if node_by_id[root_id].get("parent_id") is not None:
            raise ValueError("matrix_snapshot roots cannot have parents")
        active: set[str] = set()
        pending: list[tuple[str, bool]] = [(root_id, True)]
        while pending:
            node_id, entering = pending.pop()
            if not entering:
                active.remove(node_id)
                continue
            if node_id in active:
                raise ValueError("matrix_snapshot hierarchy contains a cycle")
            if node_id in visited:
                raise ValueError("matrix_snapshot node is reachable more than once")
            active.add(node_id)
            visited.add(node_id)
            pending.append((node_id, False))
            pending.extend(
                (child_id, True)
                for child_id in reversed(node_by_id[node_id]["child_ids"])
            )
    if visited != set(node_by_id):
        raise ValueError("matrix_snapshot contains nodes outside the root hierarchy")
    logical_tables = payload.get("logical_tables")
    if not isinstance(logical_tables, list):
        raise ValueError("matrix_snapshot.logical_tables must be a list")
    logical_table_ids: set[str] = set()
    for index, table in enumerate(logical_tables):
        if not isinstance(table, dict):
            raise ValueError(
                f"matrix_snapshot.logical_tables[{index}] must be an object"
            )
        logical_id = str(table.get("logical_table_id") or "").strip()
        if not logical_id:
            raise ValueError("matrix logical_table_id is required")
        if logical_id in logical_table_ids:
            raise ValueError("matrix logical table IDs must be unique")
        logical_table_ids.add(logical_id)
        if not isinstance(table.get("projection"), dict):
            raise ValueError("matrix logical table projection is required")
        table_node_ids = table.get("node_ids")
        if not isinstance(table_node_ids, list):
            raise ValueError("matrix logical table node_ids must be a list")
        if any(str(node_id or "") not in node_by_id for node_id in table_node_ids):
            raise ValueError("matrix logical table references an unknown node")
    for node in nodes:
        linked_table_ids = node.get("logical_table_ids")
        if not isinstance(linked_table_ids, list):
            raise ValueError("matrix node logical_table_ids must be a list")
        if any(str(value or "") not in logical_table_ids for value in linked_table_ids):
            raise ValueError(
                "matrix node references a logical table outside logical_tables"
            )
    payload = dict(payload)
    payload["nodes"] = nodes
    payload["root_node_ids"] = normalized_roots
    return payload


def _plain_export_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain_export_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain_export_payload(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _plain_export_payload(model_dump(mode="json"))
    enum_value = getattr(value, "value", None)
    if enum_value is not None and not isinstance(value, (str, bytes)):
        return _plain_export_payload(enum_value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _ensure_excel_cell_text(value: Any, *, context: str) -> str:
    text = str(value or "")
    if len(text) > _EXCEL_MAX_CELL_CHARACTERS:
        raise ValueError(
            f"{context} exceeds Excel's {_EXCEL_MAX_CELL_CHARACTERS}-character cell limit; "
            "export stopped to prevent silent truncation"
        )
    return text


def _validate_matrix_export_text_limits(
    *,
    row_plans: list[_MatrixNodeRowPlan],
    table_catalog: _TableExportCatalog,
    table_details: dict[str, dict[str, Any]],
) -> None:
    """Fail before workbook creation if native table topology cannot be lossless.

    Matrix prose stays in one logical row.  Text that cannot be shown within
    Excel's row-height limit is projected to the visible full-text detail sheet.
    Native source cells cannot be split without changing their row/merge identity,
    so an oversized source cell fails closed instead of being silently truncated by
    XlsxWriter.
    """

    for plan in row_plans:
        for prepared in (plan.content_row, plan.basis_row, plan.trace_row):
            _ensure_excel_cell_text(prepared.text, context="Matrix cell")
    for logical_table_id, parts in table_catalog.parts_by_logical_id.items():
        for part in parts:
            for cell in part.cells:
                _ensure_excel_cell_text(
                    _table_cell_display_text(cell),
                    context=(
                        f"logical table {logical_table_id} source cell "
                        f"{cell.get('id') or cell.get('cell_id') or '<unknown>'}"
                    ),
                )
            for header in part.headers:
                _ensure_excel_cell_text(
                    header,
                    context=f"logical table {logical_table_id} header",
                )
            for row in part.rows:
                for value in row:
                    _ensure_excel_cell_text(
                        value,
                        context=f"logical table {logical_table_id} source row",
                    )
    for logical_table_id, detail in table_details.items():
        _ensure_excel_cell_text(
            str(detail.get("projection_id") or ""),
            context=f"logical table {logical_table_id} projection identity",
        )


def _source_fact_identity(value: dict[str, Any]) -> str:
    kind = str(value.get("kind") or value.get("type") or "").strip().lower()
    primary = ""
    for key in (
        "source_id",
        "table_id",
        "cell_id",
        "asset_id",
        "binding_id",
        "content_sha256",
    ):
        normalized = str(value.get(key) or "").strip()
        if normalized:
            primary = f"{kind}:{key}:{normalized}"
            break
    location = value.get("location") or value.get("source_location")
    span: dict[str, Any] = {}
    for key in (
        "start_offset",
        "end_offset",
        "char_start",
        "char_end",
        "text_start",
        "text_end",
        "page_number",
        "row_index",
        "column_index",
        "bbox",
    ):
        candidate = value.get(key)
        if candidate in (None, "", [], {}):
            candidate = location.get(key) if isinstance(location, dict) else None
        if candidate not in (None, "", [], {}):
            span[key] = candidate
    if primary:
        return (
            f"{primary}:span:{json.dumps(span, sort_keys=True, default=str)}"
            if span
            else primary
        )
    if isinstance(location, dict) and location:
        return f"{kind}:location:{json.dumps(location, sort_keys=True, default=str)}"
    return ""


def _table_cell_display_text(cell: dict[str, Any]) -> str:
    return str(
        cell.get("display_value")
        if cell.get("display_value") is not None
        else cell.get("raw_text")
        if cell.get("raw_text") is not None
        else cell.get("text") or ""
    )


def _table_cell_export_value(cell: dict[str, Any]) -> Any:
    """Keep declared primitive source types without guessing from display text."""

    value_type = (
        str(cell.get("value_type") or cell.get("data_type") or cell.get("type") or "")
        .strip()
        .upper()
    )
    raw_value = cell.get("raw_value")
    if raw_value is None:
        raw_value = cell.get("value")
    if value_type in {"NUMBER", "NUMERIC", "INTEGER", "FLOAT", "DECIMAL"}:
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            return raw_value
    if value_type in {"BOOLEAN", "BOOL"} and isinstance(raw_value, bool):
        return raw_value
    # Formula-like source values intentionally remain inert text even when the
    # source schema labels them as formulas.
    return _table_cell_display_text(cell)


def _visible_matrix_nodes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    node_by_id = {
        str(node.get("node_id") or ""): node
        for node in snapshot.get("nodes") or []
        if isinstance(node, dict)
    }
    ordered: list[dict[str, Any]] = []

    pending = [
        (str(root_id), 0, None)
        for root_id in reversed(snapshot.get("root_node_ids") or [])
    ]
    while pending:
        node_id, visible_depth, visible_parent_id = pending.pop()
        node = node_by_id[node_id]
        next_depth = visible_depth
        next_parent_id = visible_parent_id
        if _matrix_node_is_visible(node):
            visible_node = dict(node)
            visible_node["depth"] = visible_depth
            visible_node["_visible_parent_id"] = visible_parent_id
            visible_node["_visible_has_children"] = False
            ordered.append(visible_node)
            next_depth = visible_depth + 1
            next_parent_id = str(node.get("node_id") or "") or visible_parent_id
        # The frozen tree already records the reviewed sibling order.  Do not
        # sort equal-source-order table rows by opaque node identity here.
        pending.extend(
            (str(child_id), next_depth, next_parent_id)
            for child_id in reversed(node.get("child_ids") or [])
        )
    visible_by_id = {
        str(node.get("node_id") or ""): node
        for node in ordered
        if str(node.get("node_id") or "")
    }
    for node in ordered:
        parent = visible_by_id.get(str(node.get("_visible_parent_id") or ""))
        if parent is not None:
            parent["_visible_has_children"] = True
    for node in ordered:
        path_nodes: list[dict[str, Any]] = []
        current: dict[str, Any] | None = node
        visited: set[str] = set()
        while current is not None:
            current_id = str(current.get("node_id") or "")
            if current_id in visited:
                break
            if current_id:
                visited.add(current_id)
            if _matrix_node_is_heading(current):
                path_nodes.append(current)
            current = visible_by_id.get(str(current.get("_visible_parent_id") or ""))
        path_nodes.reverse()
        node["_chapter_path"] = " › ".join(
            _matrix_heading_path_label(item) for item in path_nodes
        )
        node["_chapter_compact_path"] = " › ".join(
            _matrix_node_number(item)
            or _matrix_title_without_direct_number(item, _matrix_node_label(item))
            for item in path_nodes
        )
    for node in ordered:
        parent = visible_by_id.get(str(node.get("_visible_parent_id") or ""))
        if parent is not None and _matrix_parent_is_transparent_heading(
            parent,
            child=node,
        ):
            parent["_suppress_summary_body"] = True
    return ordered


def _matrix_node_is_heading(node: dict[str, Any]) -> bool:
    role = str(node.get("display_role") or "").strip().upper()
    source_kind = str(node.get("source_kind") or "").strip().upper()
    if source_kind in {
        "SECTION",
        "SUBSECTION",
        "GROUP",
        "APPENDIX",
        "ANNEX",
    }:
        return True
    return role == "STRUCTURE" or (
        role == "CLAUSE" and bool(node.get("_visible_has_children"))
    )


def _matrix_heading_path_label(node: dict[str, Any]) -> str:
    number = _matrix_node_number(node)
    title = _matrix_node_label(node)
    normalized_title = re.sub(r"\s+", "", title)
    normalized_number = re.sub(r"\s+", "", number)
    if number and normalized_title.startswith(normalized_number):
        return title
    return " ".join(value for value in (number, title) if value)


def _matrix_parent_is_transparent_heading(
    parent: dict[str, Any],
    *,
    child: dict[str, Any],
) -> bool:
    """Identify a source container whose summary mirrors its direct child.

    This is deliberately stricter than text deduplication: the parent must be a
    non-requirement structure container with no direct evidence, and the child
    must be the actual DIRECT_CONTENT requirement owned by the same structure
    node.  Only the parent's fallback body is suppressed; both business nodes
    and all identities remain intact.
    """

    parent_role = str(parent.get("display_role") or "").strip().upper()
    child_kind = str(child.get("source_kind") or "").strip().upper()
    if parent_role not in {"STRUCTURE", "CLAUSE"} or child_kind != "DIRECT_CONTENT":
        return False
    if (
        str(parent.get("requirement_id") or "").strip()
        or not str(child.get("requirement_id") or "").strip()
    ):
        return False
    direct_parent_fields = (
        parent.get("display_text"),
        parent.get("requirement_text"),
        parent.get("content"),
        parent.get("atomic_statement"),
    )
    if any(str(value or "").strip() for value in direct_parent_fields):
        return False
    if any(
        parent.get(key)
        for key in ("source_refs", "content_blocks", "table_refs", "figure_refs")
    ):
        return False
    parent_structure_id = str(parent.get("structure_node_id") or "").strip()
    child_structure_id = str(child.get("structure_node_id") or "").strip()
    if not parent_structure_id or parent_structure_id != child_structure_id:
        return False
    child_owned_by_structure = any(
        isinstance(ref, dict)
        and str(ref.get("kind") or ref.get("type") or "").strip().upper() == "TEXT"
        and str(ref.get("owner_structure_node_id") or "").strip() == parent_structure_id
        for ref in child.get("source_refs") or []
    )
    if not child_owned_by_structure:
        return False
    parent_summary = _normalize_explicit_matrix_text(parent.get("summary") or "")
    child_body = _normalize_explicit_matrix_text(
        child.get("requirement_text")
        or child.get("display_text")
        or child.get("content")
        or child.get("atomic_statement")
        or ""
    )
    return bool(parent_summary and parent_summary == child_body)


def _matrix_node_is_visible(node: dict[str, Any]) -> bool:
    visibility = str(node.get("matrix_visibility") or "").strip().upper()
    # Human exclusion changes the analysis disposition, not the evidence
    # ledger.  Keep those business rows visible and omit only proven system
    # wrappers such as covers, contents pages, headers and footers.
    return visibility in {"VISIBLE", "HUMAN_EXCLUDED"}


def _matrix_table_details(
    snapshot: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    visible_node_ids = {str(node.get("node_id") or "") for node in nodes}
    linked_visible_table_ids = {
        logical_id
        for node in nodes
        for logical_id in _matrix_node_logical_table_ids(node)
    }
    for index, raw in enumerate(snapshot.get("logical_tables") or []):
        if not isinstance(raw, dict):
            raise ValueError(
                f"matrix_snapshot.logical_tables[{index}] must be an object"
            )
        logical_id = str(raw.get("logical_table_id") or "").strip()
        if not logical_id:
            raise ValueError("matrix logical_table_id is required")
        if logical_id in details:
            raise ValueError("matrix logical table IDs must be unique")
        linked_node_ids = {str(value or "") for value in raw.get("node_ids") or []}
        if (
            logical_id not in linked_visible_table_ids
            and not linked_node_ids.intersection(visible_node_ids)
        ):
            continue
        details[logical_id] = _plain_export_payload(raw)
    physical_logical_ids: dict[str, set[str]] = {}
    for logical_id, detail in details.items():
        for physical_id in _matrix_detail_physical_table_ids(detail):
            physical_logical_ids.setdefault(physical_id, set()).add(logical_id)
    for node in nodes:
        logical_ids = _matrix_node_logical_table_ids(node)
        for logical_id in logical_ids:
            if logical_id not in details:
                raise ValueError(
                    "matrix node references a logical table outside logical_tables"
                )
            detail = details[logical_id]
            detail.setdefault("source_order", node.get("source_order"))
            detail.setdefault("title", _matrix_node_label(node))
            detail.setdefault("owner_node_id", node.get("node_id"))
            refs = detail.setdefault("table_refs", [])
            evidence_blocks = [
                block
                for block in (
                    *(node.get("content_blocks") or []),
                    *(node.get("table_refs") or []),
                )
                if isinstance(block, dict)
                and (
                    str(block.get("kind") or block.get("type") or "").strip().lower()
                    == "table"
                    or isinstance(block.get("cells"), list)
                    or isinstance(block.get("table_payload"), dict)
                    or isinstance(block.get("table"), dict)
                )
            ]
            for ref in evidence_blocks:
                if not isinstance(ref, dict):
                    continue
                normalized = dict(ref)
                explicit_logical_id = str(
                    normalized.get("logical_table_id") or ""
                ).strip()
                if explicit_logical_id and explicit_logical_id != logical_id:
                    continue
                if not explicit_logical_id:
                    physical_id = _table_ref_physical_id(normalized)
                    proven_logical_ids = physical_logical_ids.get(physical_id, set())
                    if proven_logical_ids:
                        if proven_logical_ids != {logical_id}:
                            continue
                    elif len(logical_ids) != 1:
                        continue
                normalized["logical_table_id"] = logical_id
                signature = json.dumps(
                    normalized, ensure_ascii=False, sort_keys=True, default=str
                )
                if all(
                    json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                    != signature
                    for item in refs
                    if isinstance(item, dict)
                ):
                    refs.append(normalized)
    return details


def _matrix_detail_physical_table_ids(detail: dict[str, Any]) -> set[str]:
    values = {
        str(value or "").strip()
        for value in detail.get("physical_table_ids") or []
        if str(value or "").strip()
    }
    projection = detail.get("projection")
    if isinstance(projection, dict):
        table_fact = projection.get("table_fact")
        if isinstance(table_fact, dict):
            for part in table_fact.get("table_parts") or []:
                if not isinstance(part, dict):
                    continue
                value = str(
                    part.get("physical_table_id")
                    or part.get("table_id")
                    or part.get("id")
                    or ""
                ).strip()
                if value:
                    values.add(value)
    return values


def _build_matrix_overview_rows(
    nodes: list[dict[str, Any]],
    *,
    table_details: dict[str, dict[str, Any]],
) -> list[_MatrixOverviewRow]:
    """Build a compact source-order index without creating a second tree.

    A visible Matrix node appears at most once even when it is simultaneously a
    heading, stored requirement and logical-table owner.  Ordinary figures are
    evidence subrows because Matrix V1 intentionally keeps them on their owning
    business node instead of promoting them into analysis targets.
    """

    node_by_id = {
        str(node.get("node_id") or ""): node
        for node in nodes
        if str(node.get("node_id") or "")
    }
    node_position = {
        str(node.get("node_id") or ""): index for index, node in enumerate(nodes)
    }

    table_representative: dict[str, str] = {}
    for logical_id, detail in table_details.items():
        table_nodes = [
            node
            for node in nodes
            if logical_id in _matrix_node_logical_table_ids(node)
            and str(node.get("display_role") or "").strip().upper() == "TABLE"
        ]
        if table_nodes:
            representative = table_nodes[0]
        else:
            owner = node_by_id.get(str(detail.get("owner_node_id") or ""))
            if owner is not None:
                representative = owner
            else:
                representative = next(
                    (
                        node
                        for node in nodes
                        if logical_id in _matrix_node_logical_table_ids(node)
                    ),
                    None,
                )
        if representative is not None:
            table_representative[logical_id] = str(representative.get("node_id") or "")

    descendant_requirements: dict[str, list[dict[str, Any]]] = {
        node_id: [] for node_id in node_by_id
    }
    table_requirements: dict[str, list[dict[str, Any]]] = {}
    for requirement in nodes:
        requirement_id = str(requirement.get("requirement_id") or "").strip()
        if not requirement_id:
            continue
        current: dict[str, Any] | None = requirement
        visited: set[str] = set()
        while current is not None:
            current_id = str(current.get("node_id") or "")
            if not current_id or current_id in visited:
                break
            visited.add(current_id)
            descendant_requirements.setdefault(current_id, []).append(requirement)
            current = node_by_id.get(str(current.get("_visible_parent_id") or ""))
        for logical_id in _matrix_node_logical_table_ids(requirement):
            table_requirements.setdefault(logical_id, []).append(requirement)

    figure_groups: dict[str, dict[str, Any]] = {}
    figures_after_node: dict[str, list[str]] = {}
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if str(node.get("source_kind") or "").strip().upper() in {
            "FIGURE",
            "IMAGE",
        }:
            continue
        for local_index, figure in enumerate(_matrix_node_figure_refs(node)):
            identity = _source_fact_identity(figure)
            if not identity:
                # Missing immutable placement evidence must not cause two
                # unrelated figures to be collapsed merely because their text
                # or caption happens to match.
                identity = f"unbound:{node_id}:{local_index}"
            group = figure_groups.get(identity)
            if group is None:
                group = {
                    "figure": figure,
                    "owner_ids": [],
                    "first_owner_id": node_id,
                }
                figure_groups[identity] = group
                figures_after_node.setdefault(node_id, []).append(identity)
            if node_id not in group["owner_ids"]:
                group["owner_ids"].append(node_id)

    def unique_requirements(
        values: Iterable[dict[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in values:
            identity = str(value.get("requirement_id") or "").strip()
            if not identity or identity in seen:
                continue
            seen.add(identity)
            output.append(value)
        return tuple(
            sorted(
                output,
                key=lambda value: node_position.get(
                    str(value.get("node_id") or ""),
                    2**31 - 1,
                ),
            )
        )

    output: list[_MatrixOverviewRow] = []
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        roles: list[str] = []
        if _matrix_node_is_heading(node):
            roles.append("章节")
        if str(node.get("requirement_id") or "").strip():
            roles.append("需求")
        logical_ids = tuple(
            logical_id
            for logical_id, representative_id in table_representative.items()
            if representative_id == node_id
        )
        if logical_ids:
            roles.append("表格")
        is_explicit_figure = str(node.get("source_kind") or "").strip().upper() in {
            "FIGURE",
            "IMAGE",
        }
        if is_explicit_figure:
            roles.append("图片")
        if roles:
            linked_requirements: list[dict[str, Any]] = []
            if "章节" in roles:
                linked_requirements.extend(descendant_requirements.get(node_id, []))
            if "需求" in roles:
                linked_requirements.append(node)
            for logical_id in logical_ids:
                linked_requirements.extend(table_requirements.get(logical_id, []))
            output.append(
                _MatrixOverviewRow(
                    node=node,
                    roles=tuple(roles),
                    logical_table_ids=logical_ids,
                    figure_refs=tuple(_matrix_node_figure_refs(node))
                    if is_explicit_figure
                    else (),
                    requirement_nodes=unique_requirements(linked_requirements),
                    object_id=str(node.get("requirement_code") or "").strip(),
                    title=_matrix_overview_node_title(
                        node,
                        logical_table_ids=logical_ids,
                        table_details=table_details,
                    ),
                    source_number=_matrix_node_number(node),
                    depth=max(0, _coerce_int_or_default(node.get("depth"), 0)),
                    is_figure_evidence=is_explicit_figure,
                )
            )

        for identity in figures_after_node.get(node_id, []):
            group = figure_groups[identity]
            figure = group["figure"]
            output.append(
                _MatrixOverviewRow(
                    node=node,
                    roles=("图片",),
                    logical_table_ids=(),
                    figure_refs=(figure,),
                    # An attachment may be owned by a requirement, but it is
                    # evidence rather than a second requirement/status row.
                    requirement_nodes=(),
                    object_id="",
                    title=_matrix_figure_overview_title(figure),
                    source_number=_matrix_figure_source_number(figure),
                    depth=min(
                        7, max(0, _coerce_int_or_default(node.get("depth"), 0)) + 1
                    ),
                    is_figure_evidence=True,
                )
            )
    return output


def _matrix_overview_node_title(
    node: dict[str, Any],
    *,
    logical_table_ids: tuple[str, ...],
    table_details: dict[str, dict[str, Any]],
) -> str:
    generic_table_titles = {"整表候选", "整表", "表格", "未命名内容"}
    title = str(node.get("display_title") or "").strip()
    if (
        title
        and title not in generic_table_titles
        and not _matrix_title_matches_owned_body(node, title)
    ):
        return _matrix_title_without_direct_number(node, title)
    label = str(node.get("label") or "").strip()
    requirement_text = str(node.get("requirement_text") or "").strip()
    display_text = str(node.get("display_text") or "").strip()
    if (
        label
        and label not in {requirement_text, display_text, *generic_table_titles}
        and not _matrix_title_matches_owned_body(node, label)
    ):
        return _matrix_title_without_direct_number(node, label)
    if (
        str(node.get("requirement_id") or "").strip()
        and str(node.get("display_role") or "").strip().upper() != "TABLE"
    ):
        return "未提供标题"
    for logical_id in logical_table_ids:
        detail = table_details.get(logical_id) or {}
        table_title = _matrix_trusted_table_detail_title(detail)
        if table_title:
            return _matrix_title_without_direct_number(node, table_title)
    if str(node.get("requirement_id") or "").strip():
        return "未提供标题"
    if logical_table_ids:
        return _matrix_unlabelled_table_title(node)
    return _matrix_title_without_direct_number(node, label) or "未提供标题"


def _matrix_unlabelled_table_title(node: dict[str, Any]) -> str:
    source = _matrix_source_label(node)
    return f"无独立表题 · {source}" if source else "无独立表题"


def _matrix_trusted_table_detail_title(detail: dict[str, Any]) -> str:
    generic = {"整表候选", "整表", "表格", "未命名内容"}
    for key in ("section_code", "owner_label", "title"):
        value = str(detail.get(key) or "").strip()
        if value and value not in generic:
            return value
    return ""


def _matrix_figure_source_number(figure: dict[str, Any]) -> str:
    return str(
        figure.get("source_number")
        or figure.get("figure_number")
        or figure.get("number")
        or ""
    ).strip()


def _matrix_figure_overview_title(figure: dict[str, Any]) -> str:
    title = str(
        figure.get("caption") or figure.get("title") or figure.get("text") or ""
    ).strip()
    if title:
        return title
    location = figure.get("location") or figure.get("source_location") or {}
    page = (
        _coerce_optional_int(location.get("page_number"))
        if isinstance(location, dict)
        else None
    )
    return f"无图题图片 · 第 {page} 页" if page is not None else "无图题图片"


def _table_ref_physical_id(ref: dict[str, Any]) -> str:
    nested = ref.get("table_payload") or ref.get("table")
    table = nested if isinstance(nested, dict) else {}
    return str(
        ref.get("physical_table_id")
        or ref.get("source_table_id")
        or ref.get("table_id")
        or ref.get("id")
        or table.get("physical_table_id")
        or table.get("table_id")
        or table.get("id")
        or ""
    ).strip()


def _merge_rich_mapping(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if value in (None, "", [], {}):
            continue
        current = target.get(key)
        if current in (None, "", [], {}):
            target[key] = _plain_export_payload(value)
            continue
        if isinstance(current, list) and isinstance(value, list):
            signatures = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                for item in current
            }
            for item in value:
                signature = json.dumps(
                    item, ensure_ascii=False, sort_keys=True, default=str
                )
                if signature not in signatures:
                    current.append(_plain_export_payload(item))
                    signatures.add(signature)


def _build_matrix_table_catalog(
    snapshot: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> _TableExportCatalog:
    details = _matrix_table_details(snapshot, nodes)
    physical_logical_ids: dict[str, set[str]] = {}
    for logical_id, detail in details.items():
        for physical_id in _matrix_detail_physical_table_ids(detail):
            physical_logical_ids.setdefault(physical_id, set()).add(logical_id)
    items: list[dict[str, Any]] = []
    for logical_id, detail in details.items():
        refs = [
            dict(ref) for ref in detail.get("table_refs") or [] if isinstance(ref, dict)
        ]
        projection = detail.get("projection")
        if isinstance(projection, dict):
            refs.extend(_table_refs_from_lineage([projection]))
        if not refs:
            refs = [
                {
                    "logical_table_id": logical_id,
                    "table_id": logical_id,
                    "projection_id": detail.get("projection_id"),
                    "projection_mode": detail.get("projection_mode"),
                    "parts": [],
                }
            ]
        refs = [
            {**ref, "logical_table_id": logical_id}
            for ref in refs
            if str(ref.get("logical_table_id") or logical_id).strip() == logical_id
        ]
        items.append(
            {
                "node_id": detail.get("owner_node_id"),
                "table_refs": refs,
                "table_record_projection": projection
                if isinstance(projection, dict)
                else {},
                "table_record_id": "",
            }
        )
    for node in nodes:
        refs = [
            dict(ref) for ref in node.get("table_refs") or [] if isinstance(ref, dict)
        ]
        logical_ids = _matrix_node_logical_table_ids(node)
        normalized_refs: list[dict[str, Any]] = []
        for ref in refs:
            explicit_logical_id = str(ref.get("logical_table_id") or "").strip()
            if explicit_logical_id:
                if explicit_logical_id in details:
                    normalized_refs.append(ref)
                continue
            physical_id = _table_ref_physical_id(ref)
            proven_logical_ids = physical_logical_ids.get(physical_id, set())
            if len(proven_logical_ids) == 1:
                resolved_logical_id = next(iter(proven_logical_ids))
                if resolved_logical_id in logical_ids:
                    normalized_refs.append(
                        {**ref, "logical_table_id": resolved_logical_id}
                    )
            elif len(logical_ids) == 1 and logical_ids[0] in details:
                normalized_refs.append({**ref, "logical_table_id": logical_ids[0]})
        refs = normalized_refs
        if refs:
            items.append(
                {
                    **node,
                    "table_refs": refs,
                    "atomic_requirement_id": node.get("requirement_id")
                    or node.get("requirement_entry_id"),
                    "display_req_code": _matrix_node_label(node),
                }
            )
    catalog = _build_table_export_catalog(items)
    detail_order = list(details)
    ordered = list(dict.fromkeys([*detail_order, *catalog.first_seen_order]))
    for logical_id in ordered:
        catalog.parts_by_logical_id.setdefault(logical_id, [])
    catalog.first_seen_order = ordered
    return catalog


def _matrix_table_sheet_name(
    *,
    index: int,
    detail: dict[str, Any],
    used_names: set[str],
) -> str:
    title = str(
        detail.get("section_code")
        or detail.get("owner_label")
        or detail.get("title")
        or ""
    ).strip()
    match = re.match(r"^\s*([A-Za-z]?\d+(?:\.\d+)*(?:\s*[A-Za-z]\))?)", title)
    code = match.group(1).replace(" ", "") if match else ""
    readable_title = title[match.end() :].strip(" -—·_") if match else title
    suffix = "_".join(
        value
        for value in (
            code,
            readable_title[:18],
        )
        if value
    )
    suffix = suffix or title[:24]
    suffix = re.sub(r"[\[\]:*?/\\]", "_", suffix).strip(" _")
    base = f"T{index:03d}" + (f"_{suffix}" if suffix else "")
    candidate = base[:31]
    counter = 2
    existing = {name.casefold() for name in used_names}
    while candidate.casefold() in existing:
        ending = f"_{counter}"
        candidate = f"{base[: 31 - len(ending)]}{ending}"
        counter += 1
    return candidate


def _write_matrix_overview_sheet(
    *,
    sheet,
    workbook: xlsxwriter.Workbook,
    formats: _WorkbookFormats,
    project_name: str,
    customer_name: str,
    source_file_name: str,
    export_mode: str,
    rows: list[_MatrixOverviewRow],
    matrix_row_by_node: dict[str, int],
    table_sheet_names: dict[str, str],
    full_text_rows_by_node: dict[str, int],
    source_image_context: _ExportSourceImageContext | None,
) -> None:
    """Write the compact structural/status index shown first in the workbook."""

    for column, width in enumerate(_MATRIX_OVERVIEW_COLUMN_WIDTHS):
        sheet.set_column(column, column, width)
    sheet.set_zoom(100)
    sheet.set_tab_color(_BRAND_PRIMARY)
    _write_brand_header(
        sheet,
        formats=formats,
        project_name=project_name,
        customer_name=customer_name,
        source_file_name=source_file_name,
        export_mode=export_mode,
        last_col=10,
    )
    guide_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": 10,
            "font_color": "#FFFFFF",
            "bg_color": _BRAND_SECONDARY,
            "bold": True,
            "valign": "vcenter",
        }
    )
    sheet.merge_range(
        1,
        0,
        1,
        10,
        "快速总览：按原文顺序列出章节、需求、逻辑表和图片证据；这里只显示标题与状态，完整正文在 ComplianceMatrix。Req ID 仅在当前 Requirement Set 内唯一。",
        guide_format,
    )
    sheet.set_row(1, 28)

    requirements: dict[str, dict[str, Any]] = {}
    logical_tables: set[str] = set()
    figure_rows = [value for value in rows if value.is_figure_evidence]
    for overview_row in rows:
        logical_tables.update(overview_row.logical_table_ids)
        for requirement in overview_row.requirement_nodes:
            requirement_id = str(requirement.get("requirement_id") or "").strip()
            if requirement_id:
                requirements.setdefault(requirement_id, requirement)
    targets = [
        value for value in requirements.values() if _matrix_analysis_eligible(value)
    ]
    effective = [value for value in targets if _matrix_effective_decision_token(value)]
    decision_counts = Counter(
        _matrix_effective_decision_token(value) for value in effective
    )
    figure_renderability = [
        (
            any(
                _load_matrix_figure_payload(
                    ref,
                    source_image_context=source_image_context,
                )
                is not None
                for ref in value.figure_refs
            ),
            any(_matrix_figure_has_resolvable_source(ref) for ref in value.figure_refs),
        )
        for value in figure_rows
    ]
    renderable_figure_count = sum(loadable for loadable, _ in figure_renderability)
    unresolved_figure_count = sum(
        not loadable and not resolvable for loadable, resolvable in figure_renderability
    )
    unavailable_figure_count = sum(
        not loadable and resolvable for loadable, resolvable in figure_renderability
    )
    summary = "  |  ".join(
        (
            f"需求 {len(requirements)}",
            f"待分析/待结论 {len(targets) - len(effective)}",
            *(f"{label} {decision_counts[token]}" for token, label in (
                ("REJECT", "拒绝"), ("DEVIATION_ACCEPT", "偏差"), ("NEED_CLARIFICATION", "需确认"),
                ("COMPLIANT", "符合"), ("PARTIALLY_COMPLIANT", "部分符合"), ("NON_COMPLIANT", "不符合"),
            ) if decision_counts.get(token)),
            f"逻辑表 {len(logical_tables)}",
            f"图片证据 {len(figure_rows)}",
            f"可渲染 {renderable_figure_count}",
            f"资源不可用/校验失败 {unavailable_figure_count}",
            f"源信息不完整 {unresolved_figure_count}",
            "客户确认：上游未提供",
        )
    )
    summary_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": 10,
            "font_color": _BRAND_TEXT,
            "bg_color": "#EAF3ED",
            "bold": True,
            "bottom": 1,
            "bottom_color": "#B7D7C3",
            "valign": "vcenter",
            "text_wrap": True,
        }
    )
    sheet.merge_range(2, 0, 2, 10, summary, summary_format)
    sheet.set_row(2, 44)
    sheet.write_row(
        3,
        0,
        [
            "对象 / Req ID",
            "原文编号",
            "所属章节",
            "标题",
            "需求数",
            "是否分析",
            "分析进度",
            "当前结论",
            "客户确认",
            "原文 / 附件",
            "打开 Matrix",
        ],
        formats.header,
    )
    sheet.set_row(3, 28)
    sheet.freeze_panes(4, 4)

    role_colors = {
        "章节": "#E2F0E7",
        "需求": "#FFFFFF",
        "表格": "#E8F1F5",
        "图片": "#F3EDF8",
    }
    format_cache: dict[tuple[str, int, str], Any] = {}

    def row_format(
        overview_row: _MatrixOverviewRow,
        *,
        alignment: str = "left",
    ) -> Any:
        primary_role = overview_row.roles[0] if overview_row.roles else "需求"
        key = (primary_role, min(overview_row.depth, 7), alignment)
        if key not in format_cache:
            format_cache[key] = workbook.add_format(
                {
                    "font_name": _workbook_font_name(),
                    "font_size": 10,
                    "font_color": _BRAND_TEXT,
                    "bg_color": role_colors.get(primary_role, "#FFFFFF"),
                    "bottom": 1,
                    "bottom_color": "#E2E2E2",
                    "bold": "章节" in overview_row.roles,
                    "text_wrap": True,
                    "valign": "vcenter",
                    "align": alignment,
                    "indent": min(overview_row.depth, 7) if alignment == "left" else 0,
                }
            )
        return format_cache[key]

    link_formats = {
        role: workbook.add_format(
            {
                "font_name": _workbook_font_name(),
                "font_size": 10,
                "font_color": _BRAND_SECONDARY,
                "bg_color": fill,
                "underline": True,
                "bold": True,
                "bottom": 1,
                "bottom_color": "#E2E2E2",
                "text_wrap": True,
                "align": "center",
                "valign": "vcenter",
            }
        )
        for role, fill in role_colors.items()
    }
    decision_formats = {
        token: workbook.add_format(
            {
                "font_name": _workbook_font_name(),
                "font_size": 10,
                "font_color": _BRAND_TEXT,
                "bg_color": color,
                "bottom": 1,
                "bottom_color": "#E2E2E2",
                "bold": True,
                "text_wrap": True,
                "align": "center",
                "valign": "vcenter",
            }
        )
        for token, color in {
            "ACCEPT": "#E8F5E9",
            "REJECT": "#FFEBEE",
            "DEVIATION_ACCEPT": "#FFF3E0",
            "NEED_CLARIFICATION": "#E2EBF8",
            "PENDING": "#FFF8E1",
            "INFO": "#F5F5F5",
        }.items()
    }

    if not rows:
        sheet.merge_range(
            4,
            0,
            4,
            10,
            "没有可展示的业务章节、需求、表格或图片。",
            formats.meta_value,
        )
        last_row = 4
    else:
        for offset, overview_row in enumerate(rows):
            row = 4 + offset
            node = overview_row.node
            node_id = str(node.get("node_id") or "")
            primary_role = overview_row.roles[0] if overview_row.roles else "需求"
            linked_tables = [
                table_sheet_names[logical_id]
                for logical_id in overview_row.logical_table_ids
                if logical_id in table_sheet_names
            ]
            object_parts = [overview_row.object_id] if overview_row.object_id else []
            if linked_tables:
                object_parts.append(
                    linked_tables[0]
                    if len(linked_tables) == 1
                    else f"{linked_tables[0]} 等 {len(linked_tables)} 张表"
                )
            if not object_parts:
                object_parts.append(" · ".join(overview_row.roles))

            requirement_values = list(overview_row.requirement_nodes)
            target_values = [
                value
                for value in requirement_values
                if _matrix_analysis_eligible(value)
            ]
            retained_count = len(requirement_values) - len(target_values)
            effective_values = [
                value
                for value in target_values
                if _matrix_effective_decision_token(value)
            ]
            pending_count = len(target_values) - len(effective_values)
            if target_values and retained_count:
                scope = f"分析 {len(target_values)} · 保留 {retained_count}"
            elif target_values:
                scope = f"分析 {len(target_values)}"
            elif retained_count > 1:
                scope = f"不分析 · {retained_count}"
            elif retained_count == 1:
                scope = "不分析"
            else:
                scope = "不适用"
            progress = (
                f"{len(effective_values)}/{len(target_values)} 已结论"
                if target_values
                else "不适用"
            )
            row_decisions = Counter(
                _matrix_effective_decision_token(value)
                for value in effective_values
                if _matrix_effective_decision_token(value)
            )
            if len(requirement_values) == 1:
                direct = requirement_values[0]
                conclusion = _matrix_current_decision_label(direct)
                if not conclusion and _matrix_analysis_eligible(direct):
                    conclusion = _matrix_analysis_status_label(direct)
            else:
                conclusion_parts = [
                    value
                    for value in (
                        f"接受 {row_decisions.get('ACCEPT', 0)}"
                        if row_decisions.get("ACCEPT")
                        else "",
                        f"拒绝 {row_decisions.get('REJECT', 0)}"
                        if row_decisions.get("REJECT")
                        else "",
                        f"偏差 {row_decisions.get('DEVIATION_ACCEPT', 0)}"
                        if row_decisions.get("DEVIATION_ACCEPT")
                        else "",
                        f"确认 {row_decisions.get('NEED_CLARIFICATION', 0)}"
                        if row_decisions.get("NEED_CLARIFICATION")
                        else "",
                        f"符合 {row_decisions['COMPLIANT']}" if row_decisions.get("COMPLIANT") else "",
                        f"部分符合 {row_decisions['PARTIALLY_COMPLIANT']}" if row_decisions.get("PARTIALLY_COMPLIANT") else "",
                        f"不符合 {row_decisions['NON_COMPLIANT']}" if row_decisions.get("NON_COMPLIANT") else "",
                        f"待处理 {pending_count}" if pending_count else "",
                    )
                    if value
                ]
                conclusion = "\n".join(
                    " · ".join(conclusion_parts[index : index + 2])
                    for index in range(0, len(conclusion_parts), 2)
                )
            values = [
                "\n".join(object_parts),
                overview_row.source_number,
                str(
                    node.get("_chapter_compact_path") or node.get("_chapter_path") or ""
                ).strip(),
                overview_row.title,
                len(requirement_values) if requirement_values else 0,
                scope,
                progress,
                conclusion,
                "上游未提供" if requirement_values else "不适用",
                _matrix_source_label(node),
                "",
            ]
            for column, value in enumerate(values):
                alignment = "center" if column in {4, 5, 6, 7, 8, 9, 10} else "left"
                target_format = row_format(overview_row, alignment=alignment)
                if column == 7:
                    token = (
                        "REJECT"
                        if row_decisions.get("REJECT") or row_decisions.get("NON_COMPLIANT")
                        else "DEVIATION_ACCEPT"
                        if row_decisions.get("DEVIATION_ACCEPT") or row_decisions.get("PARTIALLY_COMPLIANT")
                        else "NEED_CLARIFICATION"
                        if row_decisions.get("NEED_CLARIFICATION")
                        else "PENDING"
                        if pending_count
                        else "ACCEPT"
                        if row_decisions.get("ACCEPT")
                        else "INFO"
                    )
                    target_format = decision_formats[token]
                sheet.write(row, column, value, target_format)

            link_format = link_formats.get(primary_role, link_formats["需求"])
            if linked_tables:
                sheet.write_url(
                    row,
                    9,
                    f"internal:'{linked_tables[0]}'!A1",
                    link_format,
                    string=(
                        "打开原表"
                        if len(linked_tables) == 1
                        else f"打开 {len(linked_tables)} 张表"
                    ),
                )
                if len(linked_tables) > 1:
                    sheet.write_comment(
                        row,
                        9,
                        "关联原表：" + "、".join(linked_tables),
                        {"author": "ReqPilotX"},
                    )
            elif overview_row.figure_refs:
                available = any(
                    _load_matrix_figure_payload(
                        figure,
                        source_image_context=source_image_context,
                    )
                    is not None
                    for figure in overview_row.figure_refs
                )
                detail_row = full_text_rows_by_node.get(node_id)
                label = "查看图片 · 资源可用" if available else "图片引用 · 资源缺失"
                if detail_row:
                    sheet.write_url(
                        row,
                        9,
                        f"internal:'{_MATRIX_FULL_TEXT_SHEET_NAME}'!A{detail_row}",
                        link_format,
                        string=label,
                    )
                else:
                    sheet.write(
                        row,
                        9,
                        label,
                        row_format(overview_row, alignment="center"),
                    )
            matrix_row = matrix_row_by_node.get(node_id)
            if matrix_row:
                sheet.write_url(
                    row,
                    10,
                    f"internal:'{_MATRIX_SHEET_NAME}'!A{matrix_row}",
                    link_format,
                    string="打开条目",
                )
            base_row_height = 30.0 if "章节" in overview_row.roles else 24.0
            indent_padding_px = min(overview_row.depth, 7) * 12
            adaptive_row_height = max(
                _estimate_total_text_height(
                    values[column],
                    max(
                        40,
                        _excel_column_width_to_pixels(
                            _MATRIX_OVERVIEW_COLUMN_WIDTHS[column]
                        )
                        - (indent_padding_px if column in {0, 2, 3} else 0),
                    ),
                    cap_to_excel_row=True,
                )
                for column in (0, 2, 3, 5, 6, 7, 8)
            )
            sheet.set_row(
                row,
                min(72.0, max(base_row_height, adaptive_row_height)),
            )
        last_row = 3 + len(rows)

    sheet.autofilter(3, 0, max(3, last_row), 10)
    sheet.set_landscape()
    sheet.set_paper(8)
    sheet.set_print_scale(85)
    sheet.repeat_rows(0, 3)
    sheet.repeat_columns(0, 3)
    sheet.print_area(0, 0, max(3, last_row), 10)
    sheet.set_margins(0.3, 0.3, 0.45, 0.45)
    sheet.set_header("&LReqPilotX&C清单概览&R第 &P / &N 页", {"margin": 0.2})
    sheet.set_footer("&L来源顺序 · 标题与状态索引&R&P / &N", {"margin": 0.2})
    if rows:
        sheet.set_selection(4, 3, 4, 3)


def _write_matrix_sheet(
    *,
    sheet,
    workbook: xlsxwriter.Workbook,
    formats: _WorkbookFormats,
    project_name: str,
    customer_name: str,
    source_file_name: str,
    export_mode: str,
    row_plans: list[_MatrixNodeRowPlan],
    table_sheet_names: dict[str, str],
    record_rows_by_table: dict[str, dict[str, int]],
    full_text_rows_by_node: dict[str, int],
    source_image_context: _ExportSourceImageContext | None,
) -> None:
    sheet.freeze_panes(3, 3)
    has_current_analysis_output = any(
        _matrix_current_decision_label(plan.node) or plan.basis_row.text.strip()
        for plan in row_plans
    )
    for column, width in enumerate(_MATRIX_COLUMN_WIDTHS):
        sheet.set_column(column, column, width)
    if not has_current_analysis_output:
        sheet.set_column(
            7,
            7,
            _MATRIX_COLUMN_WIDTHS[7],
            None,
            {"hidden": True, "level": 1},
        )
        sheet.set_column(
            8,
            8,
            _MATRIX_COLUMN_WIDTHS[8],
            None,
            {"hidden": True, "level": 1},
        )
        sheet.set_column(
            9,
            9,
            _MATRIX_COLUMN_WIDTHS[9],
            None,
            {"collapsed": True},
        )
    sheet.set_zoom(100)
    sheet.set_tab_color(_BRAND_PRIMARY)
    _write_brand_header(
        sheet,
        formats=formats,
        project_name=project_name,
        customer_name=customer_name,
        source_file_name=source_file_name,
        export_mode=export_mode,
        last_col=9,
    )
    headers = [
        "Req ID",
        "原文编号",
        "章节 / 标题",
        "需求正文",
        "原文 / 附件",
        "是否分析",
        "分析进度",
        "当前结论",
        "分析依据 / 结论来源",
        "客户确认",
    ]
    sheet.write_row(2, 0, headers, formats.header)
    sheet.set_row(2, 28)
    sheet.outline_settings(True, False, True, False)
    guide_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": 10,
            "font_color": "#FFFFFF",
            "bg_color": _BRAND_SECONDARY,
            "bold": True,
            "valign": "vcenter",
        }
    )
    sheet.set_row(1, 28)
    sheet.merge_range(
        1,
        0,
        1,
        9,
        "分析口径：人工最终结论优先，否则采用当前终态 AI 结论。Req ID 仅在当前 Requirement Set 内唯一；原文编号只显示直接编号。客户确认仅接受独立回执，当前上游未提供。"
        + (
            ""
            if has_current_analysis_output
            else " 当前快照无有效结论/依据，H:I 已默认折叠，可手动展开。"
        ),
        guide_format,
    )
    sheet.write_comment(
        2,
        9,
        "当前 Requirement Matrix 上游没有独立、可追溯的客户确认字段。本列明确显示“上游未提供”，绝不使用工程师人工结论或备注冒充客户确认。",
        {"author": "ReqPilotX"},
    )
    role_fills = {
        "STRUCTURE": "#E2F0E7",
        "CLAUSE": "#F0F6F2",
        "TABLE": "#E8F1F5",
        "TABLE_RECORD": "#F3F7F9",
        "CONTEXT": "#FFF8E8",
    }
    format_cache: dict[tuple[str, int, bool, bool, str, bool], Any] = {}

    def row_cell_format(
        *,
        node: dict[str, Any],
        depth: int,
        even: bool,
        continuation: bool,
        alignment: str,
        emphasis: bool = False,
    ) -> Any:
        role = str(node.get("display_role") or "").strip().upper()
        key = (role, min(depth, 7), even, continuation, alignment, emphasis)
        cached = format_cache.get(key)
        if cached is not None:
            return cached
        fill = role_fills.get(role) or (_BRAND_ROW_ALT if even else "#FFFFFF")
        properties: dict[str, Any] = {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_TEXT_SECONDARY if continuation else _BRAND_TEXT,
            "bg_color": fill,
            "bottom": 1,
            "bottom_color": "#E4E4E4",
            "text_wrap": True,
            "valign": "top" if alignment == "left" else "vcenter",
            "align": alignment,
            "bold": (
                emphasis
                or (
                    not continuation
                    and (
                        role in {"STRUCTURE", "TABLE"}
                        or bool(node.get("_visible_has_children"))
                    )
                )
            ),
        }
        if alignment == "left" and role == "CONTEXT":
            properties["italic"] = True
        if alignment == "left":
            properties["indent"] = min(depth, 7)
        cached = workbook.add_format(properties)
        format_cache[key] = cached
        return cached

    link_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_SECONDARY,
            "underline": True,
            "bold": True,
            "bg_color": "#E5F3E9",
            "bottom": 1,
            "bottom_color": "#B7D7C3",
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
        }
    )
    image_link_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_SECONDARY,
            "underline": True,
            "bold": True,
            "bg_color": "#E5F3E9",
            "bottom": 1,
            "bottom_color": "#B7D7C3",
            "align": "center",
            "valign": "top",
            "text_wrap": True,
        }
    )
    decision_formats: dict[str, Any] = {}
    for plan_index, plan in enumerate(row_plans):
        node = plan.node
        depth = max(0, _coerce_int_or_default(node.get("depth"), 0))
        even = plan_index % 2 == 1
        row = plan.start_row
        decision_token = _matrix_effective_decision_token(node)
        values = [
            str(node.get("requirement_code") or "").strip(),
            _matrix_node_number(node),
            _matrix_chapter_title_label(node, plan.title_row.text),
            plan.content_row.text,
            _matrix_hierarchy_source_label(node),
            _matrix_analysis_scope_label(node),
            _matrix_analysis_status_label(node),
            _matrix_current_decision_label(node),
            plan.basis_row.text,
            "上游未提供" if str(node.get("requirement_id") or "").strip() else "",
        ]
        for column, value in enumerate(values):
            alignment = "left" if column in {0, 1, 2, 3, 8} else "center"
            cell_format = row_cell_format(
                node=node,
                depth=depth if column == 2 else 0,
                even=even,
                continuation=False,
                alignment=alignment,
                emphasis=column == 2,
            )
            if column == 7 and value:
                token = decision_token or "INFO"
                if token not in decision_formats:
                    color = {
                        "ACCEPT": "#E8F5E9",
                        "REJECT": "#FFEBEE",
                        "DEVIATION_ACCEPT": "#FFF3E0",
                        "NEED_CLARIFICATION": "#E2EBF8",
                        "INFO": "#F3E5F5",
                    }.get(token, "#FFFFFF")
                    decision_formats[token] = workbook.add_format(
                        {
                            "font_name": _workbook_font_name(),
                            "font_size": _WORKBOOK_FONT_SIZE,
                            "font_color": _BRAND_TEXT,
                            "bg_color": color,
                            "bottom": 1,
                            "bottom_color": "#E4E4E4",
                            "bold": True,
                            "align": "center",
                            "valign": "vcenter",
                            "text_wrap": True,
                        }
                    )
                cell_format = decision_formats[token]
            sheet.write(row, column, value, cell_format)

        logical_ids = [
            logical_id
            for logical_id in _matrix_node_logical_table_ids(node)
            if logical_id in table_sheet_names
        ]
        has_figure_refs = bool(_matrix_node_figure_refs(node))
        source_entry_label = str(values[4] or "")
        node_id = str(node.get("node_id") or "").strip()
        full_text_row = full_text_rows_by_node.get(node_id)
        if full_text_row:
            has_text_detail = bool(plan.detail_texts)
            has_figure_detail = bool(_matrix_node_figure_refs(node))
            detail_label = (
                "全文 + 图片"
                if has_text_detail and has_figure_detail
                else "查看全文"
                if has_text_detail
                else "查看图片"
            )
            source_entry_label = (
                f"{detail_label} + 原表" if logical_ids else detail_label
            )
            sheet.write_url(
                row,
                4,
                f"internal:'{_MATRIX_FULL_TEXT_SHEET_NAME}'!A{full_text_row}",
                image_link_format if has_figure_refs else link_format,
                string=source_entry_label,
            )
        elif logical_ids:
            logical_id = logical_ids[0]
            target_sheet = table_sheet_names[logical_id]
            record_id = str(node.get("table_record_id") or "").strip()
            target_rows = record_rows_by_table.get(logical_id, {})
            target_row = target_rows.get(node_id) or target_rows.get(record_id) or 1
            source_entry_label = (
                f"打开 {len(logical_ids)} 张表"
                if len(logical_ids) > 1
                else "查看表格"
                if record_id
                else "打开原表"
            )
            sheet.write_url(
                row,
                4,
                f"internal:'{target_sheet}'!A{target_row}",
                link_format,
                string=source_entry_label,
            )
        if len(logical_ids) > 1:
            sheet.write_comment(
                row,
                4,
                "关联表页："
                + "、".join(table_sheet_names[value] for value in logical_ids)
                + "。进入后可用顶部“上一张 / 下一张”切换。",
                {"author": "ReqPilotX"},
            )

        content_height = plan.content_row.row_height
        row_height = max(
            _DEFAULT_ROW_HEIGHT_POINTS,
            plan.title_row.row_height,
            content_height,
            plan.basis_row.row_height,
        )
        row_height = _insert_matrix_node_figures(
            sheet=sheet,
            row=row,
            node=node,
            content_height=content_height,
            current_row_height=row_height,
            source_image_context=source_image_context,
            source_entry_label=source_entry_label,
        )
        row_options = {"level": min(depth, 7)}
        sheet.set_row(row, min(_MAX_ROW_HEIGHT_POINTS, row_height), None, row_options)

    last_row = max(
        (plan.start_row + plan.row_count - 1 for plan in row_plans), default=2
    )
    sheet.autofilter(2, 0, max(2, last_row), 9)
    sheet.set_landscape()
    sheet.set_paper(8)
    sheet.set_print_scale(85)
    sheet.repeat_rows(0, 2)
    sheet.repeat_columns(0, 2)
    sheet.print_area(0, 0, max(2, last_row), 9)
    sheet.set_margins(0.3, 0.3, 0.45, 0.45)
    sheet.set_header("&LReqPilotX&C需求清单&R第 &P / &N 页", {"margin": 0.2})
    sheet.set_footer("&L来源可追溯导出&R&P / &N", {"margin": 0.2})
    if row_plans:
        sheet.set_selection(row_plans[0].start_row, 3, row_plans[0].start_row, 3)


def _append_matrix_full_text_sheet(
    *,
    workbook: xlsxwriter.Workbook,
    formats: _WorkbookFormats,
    row_plans: list[_MatrixNodeRowPlan],
    matrix_row_by_node: dict[str, int],
    table_sheet_names: dict[str, str],
    source_image_context: _ExportSourceImageContext | None,
) -> dict[str, int]:
    detailed = [
        plan
        for plan in row_plans
        if plan.detail_texts or _matrix_node_figure_refs(plan.node)
    ]
    if not detailed:
        return {}

    sheet = workbook.add_worksheet(_MATRIX_FULL_TEXT_SHEET_NAME)
    sheet.hide_gridlines(2)
    sheet.set_tab_color(_BRAND_SECONDARY)
    sheet.set_zoom(100)
    widths = (16.0, 18.0, 90.0, 14.0, 14.0, 16.0)
    for column, width in enumerate(widths):
        sheet.set_column(column, column, width)
        sheet.write_blank(0, column, None, formats.meta_fill)
    sheet.merge_range(
        0,
        1,
        0,
        5,
        "内容详情：承接超限全文和原文图片；主 Matrix 保持一个节点一行并聚焦分析。",
        formats.meta_fill,
    )
    sheet.set_row(0, 34)
    _insert_brand_logo(sheet)
    detail_guide_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": 10,
            "font_color": "#FFFFFF",
            "bg_color": _BRAND_SECONDARY,
            "bold": True,
            "align": "left",
            "valign": "vcenter",
        }
    )
    sheet.merge_range(
        1,
        0,
        1,
        5,
        "完整内容不截断，仅清理 PDF 物理硬换行；原文图片按可读尺寸展示。D 列返回 Matrix，E 列进入原表。",
        detail_guide_format,
    )
    sheet.set_row(1, 24)
    sheet.write_row(
        2,
        0,
        ["原文编号", "字段", "完整内容 / 原文图片", "返回清单", "原表", "层级 / 来源"],
        formats.header,
    )
    sheet.set_row(2, 28)
    sheet.freeze_panes(3, 2)

    link_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_SECONDARY,
            "underline": True,
            "bold": True,
            "bg_color": "#E5F3E9",
            "bottom": 1,
            "bottom_color": "#B7D7C3",
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
        }
    )
    body_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_TEXT,
            "bg_color": "#FFFFFF",
            "bottom": 1,
            "bottom_color": "#E4E4E4",
            "text_wrap": True,
            "valign": "top",
        }
    )
    center_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_TEXT,
            "bg_color": "#FFFFFF",
            "bottom": 1,
            "bottom_color": "#E4E4E4",
            "text_wrap": True,
            "align": "center",
            "valign": "vcenter",
        }
    )

    cursor = 3
    rows_by_node: dict[str, int] = {}
    detail_width_px = _excel_column_width_to_pixels(widths[2])
    max_figure_height_px = 280

    def write_navigation(
        *,
        row: int,
        node: dict[str, Any],
        node_id: str,
        logical_ids: list[str],
        first: bool,
    ) -> None:
        if not first:
            for column in (3, 4, 5):
                sheet.write(row, column, "", center_format)
            return
        sheet.write_url(
            row,
            3,
            f"internal:'ComplianceMatrix'!A{matrix_row_by_node.get(node_id, 1)}",
            link_format,
            string="返回条目",
        )
        if logical_ids:
            sheet.write_url(
                row,
                4,
                f"internal:'{table_sheet_names[logical_ids[0]]}'!A1",
                link_format,
                string=(
                    f"打开 {len(logical_ids)} 张表"
                    if len(logical_ids) > 1
                    else "打开原表"
                ),
            )
        else:
            sheet.write(row, 4, "", center_format)
        sheet.write(
            row,
            5,
            _matrix_hierarchy_source_label(node),
            center_format,
        )

    for plan in detailed:
        node = plan.node
        node_id = str(node.get("node_id") or "").strip()
        rows_by_node[node_id] = cursor + 1
        first_node_row = True
        logical_ids = [
            value
            for value in _matrix_node_logical_table_ids(node)
            if value in table_sheet_names
        ]
        for detail_label, detail_text in plan.detail_texts:
            prepared_rows = _prepare_text_rows_for_height(
                detail_text,
                detail_width_px,
                max_height_points=_MAX_ROW_HEIGHT_POINTS,
            )
            for offset, prepared in enumerate(prepared_rows):
                if cursor >= 1_048_576:
                    raise ValueError(
                        "full-text detail exceeds the Excel worksheet row limit"
                    )
                is_first = first_node_row and offset == 0
                sheet.write(
                    cursor,
                    0,
                    _matrix_node_number(node) if is_first else "",
                    center_format,
                )
                sheet.write(
                    cursor, 1, detail_label if offset == 0 else "续", center_format
                )
                sheet.write(cursor, 2, prepared.text, body_format)
                write_navigation(
                    row=cursor,
                    node=node,
                    node_id=node_id,
                    logical_ids=logical_ids,
                    first=is_first,
                )
                sheet.set_row(
                    cursor,
                    min(_MAX_ROW_HEIGHT_POINTS, prepared.row_height),
                )
                cursor += 1
                first_node_row = False

        for figure_index, figure in enumerate(
            _matrix_node_figure_refs(node),
            start=1,
        ):
            if cursor >= 1_048_576:
                raise ValueError("figure detail exceeds the Excel worksheet row limit")
            is_first = first_node_row
            sheet.write(
                cursor,
                0,
                _matrix_node_number(node) if is_first else "",
                center_format,
            )
            sheet.write(cursor, 1, f"原文图片 {figure_index}", center_format)
            payload = _load_matrix_figure_payload(
                figure,
                source_image_context=source_image_context,
            )
            if payload is None:
                sheet.write(
                    cursor,
                    2,
                    "图片未能通过来源校验；来源引用仍保留在冻结快照中。",
                    body_format,
                )
                row_height = 44.0
            else:
                buffer, width, height = payload
                scale = min(
                    1.0,
                    max(1, detail_width_px - 12) / max(width, 1),
                    max_figure_height_px / max(height, 1),
                )
                rendered_height_px = max(1, int(round(height * scale)))
                sheet.write_blank(cursor, 2, None, body_format)
                description = str(
                    figure.get("caption")
                    or figure.get("text")
                    or f"原文图片 {figure_index}"
                ).strip()
                sheet.insert_image(
                    cursor,
                    2,
                    f"matrix-figure-detail-{figure_index}.png",
                    {
                        "image_data": buffer,
                        "object_position": 2,
                        "description": description or f"原文图片 {figure_index}",
                        "x_scale": scale,
                        "y_scale": scale,
                        "x_offset": 4,
                        "y_offset": 4,
                    },
                )
                row_height = min(
                    _MAX_ROW_HEIGHT_POINTS,
                    _pixels_to_row_height_points(rendered_height_px + 8),
                )
            write_navigation(
                row=cursor,
                node=node,
                node_id=node_id,
                logical_ids=logical_ids,
                first=is_first,
            )
            sheet.set_row(cursor, row_height)
            cursor += 1
            first_node_row = False

    last_row = max(2, cursor - 1)
    sheet.autofilter(2, 0, last_row, 5)
    sheet.set_landscape()
    sheet.set_paper(9)
    sheet.fit_to_pages(1, 0)
    sheet.repeat_rows(0, 2)
    sheet.repeat_columns(0, 1)
    sheet.print_area(0, 0, last_row, 5)
    sheet.set_margins(0.3, 0.3, 0.45, 0.45)
    sheet.set_header("&LReqPilotX&C内容详情&R第 &P / &N 页", {"margin": 0.2})
    sheet.set_footer("&L来源可追溯导出&R&P / &N", {"margin": 0.2})
    return rows_by_node


def _prepare_matrix_row_plans(
    nodes: list[dict[str, Any]],
    *,
    table_details: dict[str, dict[str, Any]] | None = None,
    table_parts_by_logical_id: dict[str, list[_TableExportPart]] | None = None,
) -> list[_MatrixNodeRowPlan]:
    plans: list[_MatrixNodeRowPlan] = []
    cursor = 3
    for node in nodes:
        title_row, title_detail = _prepare_matrix_cell_text(
            _matrix_node_display_title(node, table_details=table_details),
            _excel_column_width_to_pixels(_MATRIX_COLUMN_WIDTHS[2]),
            detail_label="标题",
            max_display_height_points=_MATRIX_MAX_VISIBLE_ROW_HEIGHT_POINTS,
        )
        content_row, content_detail = _prepare_matrix_cell_text(
            _matrix_node_body(
                node,
                table_details=table_details,
                table_parts_by_logical_id=table_parts_by_logical_id,
            ),
            _excel_column_width_to_pixels(
                _MATRIX_COLUMN_WIDTHS[_MATRIX_CONTENT_COLUMN_INDEX]
            ),
            detail_label="需求正文",
            # Labelled table fields are intentional line boundaries, not PDF
            # physical wraps.  Ordinary prose still receives wrap cleanup.
            normalize_hard_wraps=(
                str(node.get("display_role") or "").strip().upper() != "TABLE_RECORD"
            ),
            max_display_height_points=_MATRIX_MAX_VISIBLE_ROW_HEIGHT_POINTS,
        )
        basis_text = "\n".join(
            value
            for value in (
                _matrix_conclusion_source_summary(node),
                _matrix_analysis_basis_text(node),
            )
            if value
        )
        basis_row, basis_detail = _prepare_matrix_cell_text(
            basis_text,
            _excel_column_width_to_pixels(_MATRIX_COLUMN_WIDTHS[8]),
            detail_label="分析依据 / 结论来源",
            max_display_height_points=80.0,
        )
        trace_row = _PreparedRequirementRow(kind="paragraph", text="")
        detail_items = [
            (label, text)
            for label, text in (
                ("标题", title_detail),
                ("需求正文", content_detail),
                ("分析依据 / 结论来源", basis_detail),
            )
            if text
        ]
        decision_trace = _matrix_decision_trace_text(node)
        if decision_trace and decision_trace not in basis_text:
            detail_items.append(("结论完整轨迹", decision_trace))
        detail_texts = tuple(detail_items)
        if cursor + 1 > 1_048_576:
            raise ValueError("Matrix export exceeds the Excel worksheet row limit")
        plans.append(
            _MatrixNodeRowPlan(
                node=node,
                start_row=cursor,
                row_count=1,
                title_row=title_row,
                content_row=content_row,
                basis_row=basis_row,
                trace_row=trace_row,
                detail_texts=detail_texts,
            )
        )
        cursor += 1
    return plans


def _prepare_matrix_cell_text(
    text: Any,
    max_width_px: int,
    *,
    detail_label: str,
    normalize_hard_wraps: bool = False,
    protected_heading: str = "",
    max_display_height_points: float = _MAX_ROW_HEIGHT_POINTS,
) -> tuple[_PreparedRequirementRow, str]:
    """Keep one logical Matrix node in one row, with a lossless overflow path."""

    content = (
        _normalize_matrix_cell_text(text, protected_heading=protected_heading)
        if normalize_hard_wraps
        else _normalize_explicit_matrix_text(text)
    )
    required_height = _estimate_total_text_height(
        content,
        max_width_px,
        cap_to_excel_row=False,
    )
    if (
        len(content) <= _EXCEL_MAX_CELL_CHARACTERS
        and required_height <= max_display_height_points
    ):
        return (
            _PreparedRequirementRow(
                kind="paragraph",
                text=content,
                row_height=required_height,
            ),
            "",
        )

    preview_rows = _prepare_text_rows_for_height(
        content,
        max_width_px,
        # Reserve two visible lines for the lossless-detail affordance.  This
        # prevents the suffix itself from pushing an otherwise compact preview
        # beyond the Matrix row-height contract.
        max_height_points=max(
            _WORKBOOK_TEXT_LINE_HEIGHT_POINTS,
            _MATRIX_COMPACT_PREVIEW_HEIGHT_POINTS
            - (2 * _WORKBOOK_TEXT_LINE_HEIGHT_POINTS),
        ),
    )
    preview = str(preview_rows[0].text or "").rstrip()
    suffix = f"…\n（完整{detail_label}见 E 列详情）"
    if preview.endswith("\n"):
        preview = preview.rstrip()
    if preview == content:
        preview = preview[: max(1, len(preview) - len(suffix))].rstrip()
    display_text = f"{preview}{suffix}" if preview else suffix.lstrip("…\n")
    _ensure_excel_cell_text(display_text, context=f"Matrix {detail_label} preview")
    return (
        _PreparedRequirementRow(
            kind="paragraph",
            text=display_text,
            row_height=min(
                max_display_height_points,
                _estimate_total_text_height(
                    display_text,
                    max_width_px,
                    cap_to_excel_row=True,
                ),
            ),
        ),
        content,
    )


def _normalize_matrix_cell_text(
    value: Any,
    *,
    protected_heading: str = "",
) -> str:
    """Remove PDF hard-wrap noise while retaining explicit paragraphs and lists."""

    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.split("\n")]
    list_marker = re.compile(
        r"^(?:"
        r"[-•▪◦✓✔☐□]"
        r"|(?:\d+(?:\.\d+)*|[A-Za-z])[.)、]"
        r"|[（(](?:\d+(?:\.\d+)*|[A-Za-z]|[一二三四五六七八九十]+)[）)]"
        r")\s*"
    )
    visible: list[tuple[str, bool]] = []
    blank_before = False
    for line in lines:
        if not line:
            blank_before = bool(visible)
            continue
        visible.append((line, blank_before))
        blank_before = False
    if not visible:
        return ""

    long_line_lengths = [len(line) for line, _ in visible if len(line) >= 28]
    dominant_wrap_width = 0
    dominant_wrap_support = 0
    for candidate in long_line_lengths:
        support = sum(1 for length in long_line_lengths if abs(length - candidate) <= 4)
        if support > dominant_wrap_support:
            dominant_wrap_width = candidate
            dominant_wrap_support = support
    has_pdf_wrap_pattern = dominant_wrap_support >= 3

    heading = str(protected_heading or "").strip()
    prefix = ""
    if heading and re.sub(r"\s+", "", visible[0][0]) == re.sub(r"\s+", "", heading):
        prefix = visible.pop(0)[0]
    if not visible:
        return prefix

    body = visible[0][0]
    previous_line = visible[0][0]
    for line, had_blank_before in visible[1:]:
        starts_list_item = bool(list_marker.match(line))
        previous_is_complete = bool(re.search(r"[。！？；.!?;]$", previous_line))
        previous_looks_physically_wrapped = (
            has_pdf_wrap_pattern
            and len(previous_line) >= max(28, dominant_wrap_width - 6)
            and not previous_is_complete
        )
        if had_blank_before:
            # Some PDF extractors place a blank line after every physical line.
            # Collapse that noise only when the document contains a repeated,
            # near-fixed line-width pattern; otherwise a blank line remains an
            # explicit paragraph boundary even without terminal punctuation.
            if previous_looks_physically_wrapped:
                separator = (
                    ""
                    if _is_cjk_character(previous_line[-1])
                    and _is_cjk_character(line[0])
                    else " "
                )
            else:
                separator = "\n\n"
        elif starts_list_item:
            separator = "\n"
        elif _is_cjk_character(previous_line[-1]) and _is_cjk_character(line[0]):
            separator = ""
        else:
            separator = " "
        body = f"{body}{separator}{line}"
        previous_line = line
    return f"{prefix}\n\n{body}".strip() if prefix else body.strip()


def _normalize_explicit_matrix_text(value: Any) -> str:
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.split("\n")]
    output: list[str] = []
    blank_pending = False
    for line in lines:
        if not line:
            blank_pending = bool(output)
            continue
        if blank_pending:
            output.append("")
        output.append(line)
        blank_pending = False
    return "\n".join(output).strip()


def _is_cjk_character(character: str) -> bool:
    if not character:
        return False
    codepoint = ord(character[0])
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _prepare_text_rows_for_height(
    text: Any,
    max_width_px: int,
    *,
    max_height_points: float,
) -> list[_PreparedRequirementRow]:
    content = str(text or "")
    usable_height = max(
        _WORKBOOK_TEXT_LINE_HEIGHT_POINTS,
        max_height_points - _WORKBOOK_TEXT_ROW_EXTRA_POINTS,
    )
    max_lines = max(1, int(usable_height // _WORKBOOK_TEXT_LINE_HEIGHT_POINTS))
    max_units = max(
        8,
        (max_width_px - (_WORKBOOK_TEXT_PADDING_X * 2)) // _TABLE_UNIT_PX,
    )
    chunks: list[str] = []
    buffer: list[str] = []
    line_count = 1
    line_units = 0
    for character in content:
        is_newline = character == "\n"
        character_units = 0 if is_newline else (2 if ord(character) > 127 else 1)
        wraps = (
            not is_newline
            and bool(buffer)
            and line_units > 0
            and line_units + character_units > max_units
        )
        if is_newline and line_count >= max_lines and buffer:
            buffer.append(character)
            chunks.append("".join(buffer))
            buffer = []
            line_count = 1
            line_units = 0
            continue
        if wraps and line_count >= max_lines and buffer:
            chunks.append("".join(buffer))
            buffer = []
            line_count = 1
            line_units = 0
        if is_newline:
            buffer.append(character)
            line_count += 1
            line_units = 0
            continue
        if wraps:
            line_count += 1
            line_units = 0
        buffer.append(character)
        line_units += character_units
    if buffer or not chunks:
        chunks.append("".join(buffer))

    rows: list[_PreparedRequirementRow] = []
    for chunk_text in chunks:
        _ensure_excel_cell_text(chunk_text, context="wrapped Matrix text")
        rows.append(
            _PreparedRequirementRow(
                kind="paragraph",
                text=chunk_text,
                row_height=min(
                    max_height_points,
                    _estimate_total_text_height(
                        chunk_text,
                        max_width_px,
                        cap_to_excel_row=True,
                    ),
                ),
            )
        )
    return rows or [_PreparedRequirementRow(kind="paragraph", text="")]


def _insert_matrix_node_figures(
    *,
    sheet,
    row: int,
    node: dict[str, Any],
    content_height: float,
    current_row_height: float,
    source_image_context: _ExportSourceImageContext | None,
    source_entry_label: str,
) -> float:
    """Place one compact, verified figure preview in the source-entry column.

    Matrix remains a scanning/analysis surface: it never expands into an image
    gallery.  The first loadable ordinary figure is shown as a thumbnail while
    every figure remains discoverable through the overview and the lossless
    full-text sheet.  Table-cell images stay authoritative on their T worksheet.
    """

    role = str(node.get("display_role") or "").strip().upper()
    if role in {"TABLE", "TABLE_RECORD"}:
        return current_row_height
    figures = _matrix_node_figure_refs(node)
    if not figures:
        return current_row_height

    loaded: tuple[dict[str, Any], BytesIO, int, int] | None = None
    for figure in figures:
        payload = _load_matrix_figure_payload(
            figure,
            source_image_context=source_image_context,
        )
        if payload is None:
            continue
        buffer, width, height = payload
        loaded = (figure, buffer, width, height)
        break
    if loaded is None:
        return current_row_height

    del content_height
    figure, buffer, width, height = loaded
    source_column = 4
    column_width_px = _excel_column_width_to_pixels(
        _MATRIX_COLUMN_WIDTHS[source_column]
    )
    usable_width_px = max(1, column_width_px - (2 * _MATRIX_FIGURE_PADDING_PX))
    label_height_points = _estimate_total_text_height(
        source_entry_label,
        column_width_px,
        cap_to_excel_row=True,
    )
    label_offset_px = int(round(label_height_points / 0.75)) + 4
    maximum_row_height_px = int(round(_MATRIX_MAX_VISIBLE_ROW_HEIGHT_POINTS / 0.75))
    slot_height_px = max(20, maximum_row_height_px - label_offset_px - 4)
    scale = min(
        1.0,
        usable_width_px / max(width, 1),
        slot_height_px / max(height, 1),
    )
    location = figure.get("location") or figure.get("source_location") or {}
    if not isinstance(location, dict):
        location = {}
    page = _coerce_optional_int(location.get("page_number"))
    description = str(figure.get("caption") or figure.get("text") or "原文图片").strip()
    if page is not None:
        description = f"{description or '原文图片'} · 第 {page} 页"
    sheet.insert_image(
        row,
        source_column,
        "matrix-figure-preview.png",
        {
            "image_data": buffer,
            "object_position": 2,
            "description": description or "原文图片",
            "x_scale": scale,
            "y_scale": scale,
            "x_offset": _MATRIX_FIGURE_PADDING_PX,
            "y_offset": label_offset_px,
        },
    )
    rendered_height_px = label_offset_px + int(round(height * scale)) + 4
    return min(
        _MATRIX_MAX_VISIBLE_ROW_HEIGHT_POINTS,
        max(
            current_row_height,
            _pixels_to_row_height_points(rendered_height_px),
        ),
    )


def _matrix_node_figure_refs(node: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in node.get("figure_refs") or []:
        if not isinstance(value, dict):
            continue
        kind = str(value.get("kind") or value.get("type") or value.get("anchor_kind") or "").strip().lower()
        legacy_source_image = (not kind and value.get('precision') == 'SOURCE_IR_BLOCK'
            and value.get('relation') == 'AGENT_CONFIRMED_IMAGE_CONTEXT' and value.get('anchor_id') and value.get('asset_id')
            and _SHA256_PATTERN.fullmatch(str(value.get('source_sha256') or '')))
        if not kind and value.get("precision") == "EXACT_IMAGE":
            kind = "image"
        if legacy_source_image:
            kind = 'image'
        if kind not in {"figure", "image"}:
            continue
        value = dict(value, kind=kind)
        if legacy_source_image:
            value['_requires_original_image_placement'] = True
        location = value.get("location") or value.get("source_location") or {}
        if not location and (value.get("precision") == "EXACT_IMAGE" or legacy_source_image) and value.get("page") and value.get("bbox"):
            location = {"format": "pdf", "page_number": value["page"], "bbox": value["bbox"]}
            value["location"] = location
            value["page_number"] = value["page"]
        identity = json.dumps(
            {
                "source_id": value.get("source_id") or value.get("source_node_id"),
                "asset_id": value.get("asset_id") or value.get("reference_id"),
                "content_sha256": value.get("content_sha256"),
                "location": location if isinstance(location, dict) else {},
                "path": value.get("image_path")
                or value.get("snapshot_path")
                or value.get("source_snapshot_path")
                or value.get("path"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if identity in seen:
            continue
        seen.add(identity)
        output.append(value)
    return output


def _matrix_figure_identity(figure):
    location = figure.get('location') or figure.get('source_location') or {}
    return json.dumps({'page': location.get('page_number') or figure.get('page') or figure.get('page_number'),
        'bbox': location.get('bbox') or figure.get('bbox') or figure.get('source_bbox'),
        'asset': figure.get('asset_id'), 'content_sha256': figure.get('content_sha256'),
        **({'native_location': location} if location.get('format') in {'docx', 'xlsx'} else {}),
        'path': figure.get('image_path') or figure.get('snapshot_path') or figure.get('path')}, sort_keys=True)


def _preflight_source_item_images(nodes, table_catalog, context):
    expected, gaps = [], []
    def check(figure, owner, *, cell_image=False):
        name = str(figure.get('anchor_id') or figure.get('asset_id') or figure.get('cell_id') or 'unknown')
        normalized = [figure] if cell_image else _matrix_node_figure_refs({'figure_refs': [figure]})
        if not normalized:
            gaps.append(f'{owner}/{name}: UNRECOGNIZED_IMAGE_REFERENCE'); return
        value = normalized[0]
        if value.get('source_sha256') and context and context.source_sha256 and value['source_sha256'] != context.source_sha256:
            gaps.append(f'{owner}/{name}: IMAGE_SOURCE_SHA_MISMATCH'); return
        payload = _load_image_payload(value, source_image_context=context) if cell_image else _load_matrix_figure_payload(value, source_image_context=context)
        if payload is None:
            gaps.append(f'{owner}/{name}: IMAGE_PAYLOAD_UNAVAILABLE'); return
        buffer, width, height = payload
        location = value.get('location') or value.get('source_location') or {}
        expected.append({'owner': owner, 'anchor_id': value.get('anchor_id'), 'source_key': _matrix_figure_identity(value),
            'source_sha256': value.get('source_sha256') or (context.source_sha256 if context else ''),
            'page': location.get('page_number') or value.get('page') or value.get('page_number'),
            'bbox': location.get('bbox') or value.get('bbox') or value.get('source_bbox'),
            **({'native_locator': location} if location.get('format') in {'docx', 'xlsx'} else {}),
            'payload_sha256': hashlib.sha256(buffer.getvalue()).hexdigest(), 'width': width, 'height': height,
            'role': 'TABLE_CELL_IMAGE' if cell_image else 'SOURCE_FIGURE'})
    for node in nodes:
        raw_figures = node.get('figure_refs') or []
        original_bindings = {value['anchor_id']: value for value in (node.get('source_item') or {}).get('image_bindings', [])
            if isinstance(value, dict) and value.get('anchor_id') and (value.get('page') and value.get('bbox') or value.get('native_locator'))
            and _SHA256_PATTERN.fullmatch(str(value.get('source_sha256') or ''))}
        projected = {value.get('anchor_id') for value in raw_figures if isinstance(value, dict)}
        for anchor_id in set(original_bindings) - projected:
            gaps.append(f"{node.get('node_id')}/{anchor_id}: MISSING_IMAGE_PROJECTION")
        for figure in raw_figures:
            if not isinstance(figure, dict):
                gaps.append(f"{node.get('node_id')}/unknown: INVALID_IMAGE_REFERENCE")
            else:
                original = original_bindings.get(figure.get('anchor_id'))
                conflicts = [key for key in ('source_sha256', 'source_handle', 'asset_id', 'page', 'bbox', 'source_bounds', 'precision', 'native_locator', 'source_sequence', 'content_sha256')
                    if original is not None and key in original and key in figure and figure[key] != original[key]]
                if original is not None and figure.get('location') and figure['location'] != ({key: value for key, value in original['native_locator'].items() if value is not None} if original.get('native_locator') else {'format': 'pdf', 'page_number': original['page'], 'bbox': original['bbox']}):
                    conflicts.append('location')
                if conflicts:
                    gaps.append(f"{node.get('node_id')}/{figure.get('anchor_id')}: IMAGE_BINDING_CONFLICT {','.join(conflicts)}")
                    continue
                check(figure, node.get('node_id'))
    for scope, parts in table_catalog.parts_by_logical_id.items():
        for part in parts:
            for figure in part.cell_images:
                check(figure, f'{scope}/{part.physical_table_id}', cell_image=True)
    if gaps:
        raise ValueError('SOURCE_IMAGE_EXPORT_INCOMPLETE: ' + '; '.join(gaps))
    return expected


def _verify_source_item_image_package(path, expected, rendered):
    with zipfile.ZipFile(path) as package:
        media = {name: hashlib.sha256(package.read(name)).hexdigest() for name in package.namelist() if name.startswith('xl/media/')}
    missing = [value for value in expected if value['payload_sha256'] not in set(media.values())
               or value['source_key'] not in rendered['rendered_source_keys']]
    if missing:
        raise ValueError('SOURCE_IMAGE_EXPORT_INCOMPLETE: ' + '; '.join(f"{value['owner']}/{value.get('anchor_id')}: IMAGE_NOT_EMBEDDED" for value in missing))
    return {'schema_version': 'source-image-export-receipt/v1', 'status': 'VERIFIED',
        'expected_reference_count': len(expected), 'unique_source_image_count': len({value['source_key'] for value in expected}),
        'embedded_media_count': len(media), 'inserted_placement_count': rendered['inserted_placement_count'], 'gaps': [],
        'references': [{**value, 'media_parts': [name for name, digest in media.items() if digest == value['payload_sha256']],
            'figure_placements': rendered['figure_placements'].get(value['source_key'], [])} for value in expected]}


def _matrix_figure_has_resolvable_source(figure: dict[str, Any]) -> bool:
    """Report whether the frozen reference contains enough data to fetch/crop."""

    if any(
        str(figure.get(key) or "").strip()
        for key in (
            "storage_key",
            "object_key",
            "asset_path",
            "image_path",
            "snapshot_path",
            "content_sha256",
        )
    ):
        return True
    location = figure.get("location") or figure.get("source_location") or {}
    if not isinstance(location, dict):
        return False
    page = _coerce_optional_int(
        location.get("page_number") or figure.get("page_number")
    )
    bbox = location.get("bbox") or figure.get("bbox")
    return page is not None and isinstance(bbox, (list, tuple)) and len(bbox) == 4


def _load_matrix_figure_payload(
    figure: dict[str, Any],
    *,
    source_image_context: _ExportSourceImageContext | None,
) -> tuple[BytesIO, int, int] | None:
    if figure.get('_requires_original_image_placement') and not _original_pdf_image_placement(figure, source_image_context):
        return None
    payload = _load_image_payload(
        figure,
        source_image_context=source_image_context,
    )
    if payload is not None:
        return payload
    return _source_pdf_crop_payload(
        figure,
        source_image_context=source_image_context,
        require_declared_rendering=False,
    )


def _original_pdf_image_placement(figure, context):
    """Frozen sparse image refs must match an actual PDF image, not a text crop."""
    if context is None or context.source_file_path is None or figure.get('source_sha256') != context.source_sha256:
        return False
    try:
        import pymupdf
        if context.source_verified is None:
            context.source_verified = hashlib.sha256(context.source_file_path.read_bytes()).hexdigest() == context.source_sha256
        if not context.source_verified:
            return False
        location = figure.get('location') or {}
        page_number = int(location['page_number']); bbox = tuple(float(v) for v in location['bbox'])
        if len(bbox) != 4:
            return False
        with pymupdf.open(context.source_file_path) as document:
            if not 1 <= page_number <= len(document):
                return False
            page = document[page_number - 1]
            return any(item.get('xref', 0) > 0 and max(abs(a-b) for a, b in zip(item['bbox'], bbox)) < .01
                       for item in page.get_image_info(xrefs=True))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def _matrix_node_label(node: dict[str, Any]) -> str:
    return str(
        node.get("display_title")
        or node.get("label")
        or node.get("summary")
        or "未命名内容"
    ).strip()


def _matrix_node_number(node: dict[str, Any]) -> str:
    return str(node.get("source_number") or "").strip()


def _matrix_node_display_title(
    node: dict[str, Any],
    *,
    table_details: dict[str, dict[str, Any]] | None = None,
) -> str:
    generic_table_titles = {"整表候选", "整表", "表格", "未命名内容"}
    title = str(node.get("display_title") or "").strip()
    if (
        title
        and title not in generic_table_titles
        and not _matrix_title_matches_owned_body(node, title)
    ):
        return _matrix_title_without_direct_number(node, title)
    label = str(node.get("label") or "").strip()
    body_candidates = {
        str(node.get("display_text") or "").strip(),
        str(node.get("requirement_text") or "").strip(),
    }
    if (
        label
        and label not in body_candidates
        and label not in generic_table_titles
        and not _matrix_title_matches_owned_body(node, label)
    ):
        return _matrix_title_without_direct_number(node, label)
    if (
        str(node.get("requirement_id") or "").strip()
        and str(node.get("display_role") or "").strip().upper() != "TABLE"
    ):
        return "未提供标题"
    if table_details:
        for logical_id in _matrix_node_logical_table_ids(node):
            detail = table_details.get(logical_id) or {}
            trusted = _matrix_trusted_table_detail_title(detail)
            if trusted:
                return _matrix_title_without_direct_number(node, trusted)
    if str(node.get("requirement_id") or "").strip():
        return "未提供标题"
    fallback = str(node.get("summary") or "").strip()
    if fallback in generic_table_titles:
        fallback = ""
    return fallback or "未提供标题"


def _matrix_title_without_direct_number(node: dict[str, Any], title: str) -> str:
    """Keep the title cell distinct from the dedicated source-number field."""

    text = str(title or "").strip()
    number = _matrix_node_number(node)
    if not text or not number:
        return text
    if number[-1:] in {"、", ".", ")", "）", ":", "：", "-", "—"} and text.startswith(
        number
    ):
        remainder = text[len(number) :].lstrip(" 	-—:：、.)）")
        return remainder or text
    match = re.match(
        rf"^\s*{re.escape(number)}(?=\s|[-—:：、.)）。])\s*[-—:：、.)）。]?\s*(.+)$",
        text,
        flags=re.DOTALL,
    )
    remainder = str(match.group(1) if match else "").strip()
    return remainder or text


def _matrix_title_matches_owned_body(node: dict[str, Any], title: str) -> bool:
    """Reject a body copy masquerading as a title without inventing a new one."""

    if not str(node.get("requirement_id") or "").strip():
        return False
    title_identity = re.sub(r"\s+", "", str(title or ""))
    if not title_identity:
        return False
    number = re.sub(r"\s+", "", _matrix_node_number(node))

    def without_direct_number(value: str) -> set[str]:
        candidates = {value}
        if number and value.startswith(number):
            remainder = value[len(number) :].lstrip("、.)）。:：-—")
            if remainder:
                candidates.add(remainder)
        return candidates

    title_candidates = without_direct_number(title_identity)
    for value in (node.get("requirement_text"), node.get("display_text")):
        body_identity = re.sub(r"\s+", "", str(value or ""))
        if not body_identity:
            continue
        if title_candidates.intersection(without_direct_number(body_identity)):
            return True
    return False


def _matrix_chapter_title_label(node: dict[str, Any], title: str) -> str:
    path = str(
        node.get("_chapter_compact_path") or node.get("_chapter_path") or ""
    ).strip()
    if _matrix_node_is_heading(node) and title:
        if path and _matrix_node_number(node):
            return f"§{path}\n{title}"
        return f"§{path}" if path else title
    if path and title:
        return f"§{path}\n{title}"
    if path:
        return f"§{path}"
    return title or "未提供标题"


def _strip_exact_leading_title(text: str, title: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or not title:
        return normalized.strip()
    if re.sub(r"\s+", "", lines[0]) != re.sub(r"\s+", "", title):
        return normalized.strip()
    return "\n".join(lines[1:]).lstrip("\n").strip()


def _matrix_node_body(
    node: dict[str, Any],
    *,
    table_details: dict[str, dict[str, Any]] | None = None,
    table_parts_by_logical_id: dict[str, list[_TableExportPart]] | None = None,
) -> str:
    """Return only directly owned body text; title and ancestor path stay separate."""

    title = _matrix_node_display_title(node, table_details=table_details)
    role = str(node.get("display_role") or "").strip().upper()
    if role == "TABLE_RECORD":
        structured_record = _matrix_table_record_readable_body(
            node,
            table_details=table_details or {},
            table_parts_by_logical_id=table_parts_by_logical_id or {},
        )
        if structured_record:
            return structured_record
    direct = str(
        node.get("requirement_text")
        if str(node.get("requirement_text") or "").strip()
        else node.get("display_text") or ""
    ).strip()
    if role == "TABLE" and direct in {"整表候选", "整表", "表格", "未命名内容"}:
        direct = ""
    if direct:
        return _strip_exact_leading_title(direct, title)

    paragraphs: list[str] = []
    seen_source_identities: set[str] = set()
    for block in node.get("content_blocks") or []:
        if not isinstance(block, dict):
            continue
        if str(block.get("kind") or block.get("type") or "").strip().lower() not in {
            "text",
            "paragraph",
        }:
            continue
        text = str(block.get("text") or block.get("raw_text") or "").strip()
        if not text:
            continue
        identity = _source_fact_identity(block)
        if identity and identity in seen_source_identities:
            continue
        if identity:
            seen_source_identities.add(identity)
        paragraphs.append(text)
    if paragraphs:
        return _strip_exact_leading_title("\n\n".join(paragraphs), title)

    if role == "TABLE":
        summary = str(node.get("summary") or "").strip()
        return (
            ""
            if summary in {title, "整表候选", "整表", "表格", "未命名内容"}
            else summary
        )
    if node.get("_suppress_summary_body"):
        return ""
    fallback = str(
        node.get("content") or node.get("atomic_statement") or node.get("summary") or ""
    ).strip()
    return "" if fallback == title else fallback


def _matrix_table_record_readable_body(
    node: dict[str, Any],
    *,
    table_details: dict[str, dict[str, Any]],
    table_parts_by_logical_id: dict[str, list[_TableExportPart]],
) -> str:
    """Render a table record as labelled fields instead of an opaque text pile."""

    record_id = str(node.get("table_record_id") or "").strip()
    if not record_id:
        return ""
    for logical_id in _matrix_node_logical_table_ids(node):
        detail = table_details.get(logical_id) or {}
        projection = detail.get("projection")
        if not isinstance(projection, dict):
            continue
        records = projection.get("records") or []
        if isinstance(records, dict):
            record_values = list(records.values())
        elif isinstance(records, list):
            record_values = records
        else:
            record_values = []
        record = next(
            (
                value
                for value in record_values
                if isinstance(value, dict)
                and str(value.get("record_id") or "").strip() == record_id
            ),
            None,
        )
        if record is None:
            continue
        rendered = _matrix_record_source_text(
            {
                "record_id": record_id,
                "record_detail": record,
                "record_node": node,
                "label": _matrix_node_display_title(
                    node,
                    table_details=table_details,
                ),
            },
            parts=table_parts_by_logical_id.get(logical_id, ()),
        )
        if rendered:
            direct = str(
                node.get("requirement_text") or node.get("display_text") or ""
            ).strip()
            rendered_identity = re.sub(r"\s+", "", rendered)
            equal_native_record_count = sum(
                1
                for candidate in record_values
                if isinstance(candidate, dict)
                and re.sub(
                    r"\s+",
                    "",
                    _matrix_record_source_text(
                        {
                            "record_id": candidate.get("record_id") or "",
                            "record_detail": candidate,
                            "record_node": {},
                            "label": "",
                        },
                        parts=table_parts_by_logical_id.get(logical_id, ()),
                    ),
                )
                == rendered_identity
            )
            if (
                equal_native_record_count > 1
                and direct
                and re.sub(r"\s+", "", direct) not in rendered_identity
            ):
                rendered = f"{rendered}\n记录摘要：{direct}"
            return rendered
    return ""


def _matrix_node_content(
    node: dict[str, Any],
    *,
    table_details: dict[str, dict[str, Any]] | None = None,
) -> str:
    title = _matrix_node_label(node)
    source_number = _matrix_node_number(node)
    role = str(node.get("display_role") or "").strip().upper()
    direct = str(node.get("display_text") or "").strip()
    if role == "TABLE" and title in {
        "整表候选",
        "整表",
        "表格",
        "未命名内容",
    }:
        logical_ids = _matrix_node_logical_table_ids(node)
        if len(logical_ids) == 1 and table_details:
            detail = table_details.get(logical_ids[0]) or {}
            detail_title = str(
                detail.get("section_code")
                or detail.get("owner_label")
                or detail.get("title")
                or ""
            ).strip()
            if detail_title and detail_title not in {
                title,
                "整表候选",
                "整表",
                "表格",
                "未命名内容",
            }:
                same_title_ids = [
                    candidate_id
                    for candidate_id, candidate_detail in table_details.items()
                    if re.sub(
                        r"\s+",
                        "",
                        str(
                            candidate_detail.get("section_code")
                            or candidate_detail.get("owner_label")
                            or candidate_detail.get("title")
                            or ""
                        ),
                    )
                    == re.sub(r"\s+", "", detail_title)
                ]
                if len(same_title_ids) > 1:
                    table_position = same_title_ids.index(logical_ids[0]) + 1
                    detail_title = f"{detail_title}（表 {table_position}）"
                title = f"整表：{detail_title}"
    if role == "TABLE_RECORD":
        # The category/title may repeat across many distinct native rows.  Show
        # the row's own projected values so users do not see a page of apparently
        # duplicated entries; the exact native row and inherited headers remain
        # available through C-column provenance.
        record_text = direct or str(node.get("requirement_text") or "").strip()
        if not record_text:
            return title
        if re.sub(r"\s+", "", record_text).startswith(re.sub(r"\s+", "", title)):
            return record_text
        return f"{title}\n\n{record_text}"
    heading_only = {
        title,
        str(node.get("label") or "").strip(),
        f"{source_number} {title}".strip(),
        f"{source_number}{title}".strip(),
    }
    if direct:
        normalized_title = re.sub(r"\s+", "", title)
        normalized_direct = re.sub(r"\s+", "", direct)
        if normalized_direct == normalized_title or normalized_direct.startswith(
            normalized_title
        ):
            return direct
        return f"{title}\n\n{direct}"
    paragraphs: list[str] = []
    seen_source_identities: set[str] = set()
    for block in node.get("content_blocks") or []:
        if not isinstance(block, dict):
            continue
        if str(block.get("kind") or block.get("type") or "").strip().lower() not in {
            "text",
            "paragraph",
        }:
            continue
        text = str(block.get("text") or block.get("raw_text") or "").strip()
        if not text:
            continue
        identity = _source_fact_identity(block)
        if identity and identity in seen_source_identities:
            continue
        if identity:
            seen_source_identities.add(identity)
        paragraphs.append(text)
    if paragraphs:
        body = "\n\n".join(paragraphs)
        return title if body in heading_only else f"{title}\n\n{body}"
    fallback = str(
        node.get("content")
        or node.get("atomic_statement")
        or node.get("requirement_text")
        or node.get("summary")
        or ""
    ).strip()
    return title if fallback in heading_only else (fallback or title)


def _matrix_role_label(node: dict[str, Any]) -> str:
    source_kind = str(node.get("source_kind") or "").strip().upper()
    depth = int(node.get("depth") or 0)
    if source_kind == "SECTION":
        return "章节" if depth <= 1 else "子章节"
    if source_kind == "CLAUSE":
        return "条款" if depth <= 2 else "子条款"
    source_labels = {
        "DOCUMENT": "文档",
        "GROUP": "章节组",
        "SUBSECTION": "子章节",
        "APPENDIX": "附录",
        "ANNEX": "附录",
        "SUBCLAUSE": "子条款",
        "LIST_GROUP": "条目组",
        "LIST_ITEM": "条目",
        "DIRECT_CONTENT": "正文",
        "CONTENT": "正文",
        "TABLE": "表格",
        "TABLE_RECORD": "表格条目",
        "REQUIREMENT": "原子需求",
        "FIGURE": "图片",
        "IMAGE": "图片",
    }
    if source_kind in source_labels:
        return source_labels[source_kind]
    role = str(node.get("display_role") or "").strip().upper()
    labels = {
        "STRUCTURE": "章节",
        "CLAUSE": "条款",
        "TABLE": "表格",
        "TABLE_RECORD": "表格条目",
        "ATOMIC_REQUIREMENT": "原子需求",
        "CONTEXT": "上下文",
    }
    label = labels.get(role, role.replace("_", " ").title() if role else "内容")
    disposition = str(node.get("disposition") or "").strip().upper()
    if disposition in {"CONTEXT", "REFERENCE", "EVIDENCE", "INFORMATION"}:
        return f"{label} · 依据/上下文"
    return label


def _matrix_analysis_eligible(node: dict[str, Any]) -> bool:
    return is_analysis_target(node)


def _matrix_analysis_scope_label(node: dict[str, Any]) -> str:
    disposition = str(node.get("disposition") or "").strip().upper()
    visibility = str(node.get("matrix_visibility") or "").strip().upper()
    if visibility == "HUMAN_EXCLUDED" or disposition == "EXCLUDE":
        return "不分析\n已排除"
    if disposition == "RETAIN":
        return "不分析\n仅保留"
    if _matrix_analysis_eligible(node):
        return "需要分析"
    return "不分析"


def _matrix_analysis_status_label(node: dict[str, Any]) -> str:
    if not _matrix_analysis_eligible(node):
        if str(node.get("disposition") or "").strip().upper() == "EXCLUDE":
            return "已排除"
        return "无需分析"
    contract = matrix_conclusion_contract(node)
    if contract.conclusion_source is MatrixConclusionSource.STALE_REFERENCE:
        return "需重新分析"
    assessment = node.get("ai_assessment") or {}
    if (str(node.get("analysis_result_state") or "") == "CURRENT"
            and assessment.get("assessment_mode") == "agent-evidence-bound"
            and assessment.get("decision") in {"PENDING_EVIDENCE", "NEEDS_CLARIFICATION"}):
        return _matrix_decision_label(assessment["decision"])
    if has_incomplete_terminal_conclusion(node):
        return "结论不完整"
    return {
        MatrixConclusionSource.AI_DEFAULT: "AI 结论已采用",
        MatrixConclusionSource.HUMAN_OVERRIDE: "人工已修改",
        MatrixConclusionSource.HUMAN_NATIVE: "人工已录入",
    }.get(contract.conclusion_source, "待分析")


def _matrix_effective_decision_token(node: dict[str, Any]) -> str:
    return _matrix_effective_conclusion(node).strip().upper()


def _matrix_decision_label(value: Any) -> str:
    token = str(value or "").strip().upper()
    return {
        "ACCEPT": "接受",
        "REJECT": "拒绝",
        "DEVIATION_ACCEPT": "偏差",
        "NEED_CLARIFICATION": "需要确认",
        "NEEDS_CLARIFICATION": "待澄清",
        "PENDING_EVIDENCE": "待内部证据",
        "NOT_APPLICABLE": "不适用",
        "COMPLIANT": "符合",
        "PARTIALLY_COMPLIANT": "部分符合",
        "NON_COMPLIANT": "不符合",
        "INSUFFICIENT_INFORMATION": "信息不足",
    }.get(token, str(value or "").strip())


def _matrix_current_decision_label(node: dict[str, Any]) -> str:
    token = _matrix_effective_decision_token(node)
    return _matrix_decision_label(token) if token else ""


def _matrix_analysis_basis_text(node: dict[str, Any]) -> str:
    if not _matrix_analysis_eligible(node):
        disposition_reason = str(node.get("disposition_reason") or "").strip()
        return f"处置依据：{disposition_reason}" if disposition_reason else ""
    contract = matrix_conclusion_contract(node)
    if contract.conclusion_source in {
        MatrixConclusionSource.NONE,
        MatrixConclusionSource.STALE_REFERENCE,
    }:
        assessment = node.get("ai_assessment") or {}
        if (str(node.get("analysis_result_state") or "") == "CURRENT"
                and assessment.get("assessment_mode") == "agent-evidence-bound"
                and assessment.get("decision") in {"PENDING_EVIDENCE", "NEEDS_CLARIFICATION"}):
            return "\n".join(value for value in (
                f"{_matrix_decision_label(assessment['decision'])}（尚无符合性结论）",
                str(assessment.get("reason") or ""),
                "需补充：" + str(assessment["clarification_question"]) if assessment.get("clarification_question") else "",
            ) if value)
        # A pending, stale, or reason-only payload is not current decision
        # evidence.  It may remain in the frozen snapshot, but the customer
        # workbook must not project it as a valid analysis basis.
        return ""
    assessment = node.get("ai_assessment")
    manual = node.get("manual_decision")
    lines: list[str] = []
    if contract.conclusion_source in {
        MatrixConclusionSource.HUMAN_OVERRIDE,
        MatrixConclusionSource.HUMAN_NATIVE,
    } and isinstance(manual, dict):
        reason = str(manual.get("reason") or "").strip()
        if reason:
            lines.append(f"工程师依据：{reason}")
    if contract.conclusion_source in {
        MatrixConclusionSource.AI_DEFAULT,
        MatrixConclusionSource.HUMAN_OVERRIDE,
    } and isinstance(assessment, dict):
        reason = str(assessment.get("reason") or "").strip()
        if reason:
            lines.append(f"AI 理由：{reason}")
        analysis_basis = str(assessment.get("analysis_basis") or "").strip()
        if analysis_basis and analysis_basis != reason:
            lines.append(f"运行依据：{analysis_basis}")
        clarification = str(assessment.get("clarification_question") or "").strip()
        if clarification:
            lines.append(f"待确认问题：{clarification}")
    return "\n".join(dict.fromkeys(lines))


def _matrix_conclusion_source_summary(node: dict[str, Any]) -> str:
    """Return the compact, user-facing provenance needed in the Matrix row."""

    if not _matrix_analysis_eligible(node):
        return ""
    source = matrix_conclusion_contract(node).conclusion_source
    return {
        MatrixConclusionSource.AI_DEFAULT: "来源：AI 默认采用",
        MatrixConclusionSource.HUMAN_OVERRIDE: "来源：工程师修改 AI",
        MatrixConclusionSource.HUMAN_NATIVE: "来源：工程师录入",
        MatrixConclusionSource.STALE_REFERENCE: "来源：历史结果已失效",
    }.get(source, "")


def _matrix_decision_trace_text(node: dict[str, Any]) -> str:
    conclusion_note = _matrix_conclusion_note(node)
    if not _matrix_analysis_eligible(node):
        return conclusion_note
    contract = matrix_conclusion_contract(node)
    if contract.conclusion_source in {
        MatrixConclusionSource.NONE,
        MatrixConclusionSource.STALE_REFERENCE,
    }:
        return conclusion_note
    assessment = node.get("ai_assessment")
    lines = [conclusion_note] if conclusion_note else []
    if contract.conclusion_source in {
        MatrixConclusionSource.AI_DEFAULT,
        MatrixConclusionSource.HUMAN_OVERRIDE,
    } and isinstance(assessment, dict):
        ai_token = str(
            assessment.get("decision") or assessment.get("compliance_status") or ""
        ).strip()
        if ai_token:
            lines.append(f"AI 原结论：{_matrix_decision_label(ai_token)}")
        compliance = str(assessment.get("compliance_status") or "").strip()
        compliance_label = {
            "COMPLIANT": "符合",
            "NON_COMPLIANT": "不符合",
            "PARTIALLY_COMPLIANT": "部分符合",
            "INSUFFICIENT_INFORMATION": "信息不足",
            "UNKNOWN": "信息不足",
        }.get(compliance.upper(), compliance)
        if compliance_label:
            lines.append(f"符合性：{compliance_label}")
        confidence = assessment.get("confidence")
        if confidence not in (None, ""):
            try:
                score = float(confidence)
                confidence_label = (
                    f"{score:.0%}" if 0 <= score <= 1 else str(confidence)
                )
            except (TypeError, ValueError):
                confidence_label = str(confidence)
            lines.append(f"置信度：{confidence_label}")
    return "\n".join(lines)


def _matrix_hierarchy_source_label(node: dict[str, Any]) -> str:
    return "\n".join(
        value
        for value in (
            _matrix_role_label(node),
            _matrix_source_label(node),
        )
        if value
    )


def _matrix_ai_text(node: dict[str, Any]) -> str:
    if not _matrix_analysis_eligible(node):
        return ""
    contract = matrix_conclusion_contract(node)
    if contract.conclusion_source is MatrixConclusionSource.STALE_REFERENCE:
        return "历史分析结果已失效，需重新分析"
    if contract.conclusion_source not in {
        MatrixConclusionSource.AI_DEFAULT,
        MatrixConclusionSource.HUMAN_OVERRIDE,
    }:
        return ""
    assessment = node.get("ai_assessment")
    if not isinstance(assessment, dict):
        return ""
    lines: list[str] = []
    decision = str(assessment.get("decision") or "").strip()
    compliance_status = str(assessment.get("compliance_status") or "").strip()
    if not decision and not compliance_status:
        return ""
    reason = str(assessment.get("reason") or "").strip()
    analysis_basis = str(assessment.get("analysis_basis") or "").strip()
    clarification = str(assessment.get("clarification_question") or "").strip()
    confidence = assessment.get("confidence")
    if decision:
        lines.append(f"建议：{_matrix_decision_label(decision)}")
    if compliance_status and compliance_status != decision:
        lines.append(f"符合性：{compliance_status}")
    if reason:
        lines.append(f"理由：{reason}")
    if analysis_basis and analysis_basis != reason:
        lines.append(f"运行依据：{analysis_basis}")
    if clarification:
        lines.append(f"待确认问题：{clarification}")
    if confidence not in (None, ""):
        try:
            score = float(confidence)
            confidence_label = f"{score:.0%}" if 0 <= score <= 1 else str(confidence)
        except (TypeError, ValueError):
            confidence_label = str(confidence)
        lines.append(f"置信度：{confidence_label}")
    return "\n".join(lines)


def _matrix_effective_conclusion(node: dict[str, Any]) -> str:
    return matrix_conclusion_contract(node).effective_conclusion


def _matrix_manual_reason(node: dict[str, Any]) -> str:
    if not _matrix_analysis_eligible(node):
        return ""
    decision = node.get("manual_decision")
    if not isinstance(decision, dict):
        return ""
    return str(decision.get("reason") or "").strip()


def _matrix_conclusion_note(node: dict[str, Any]) -> str:
    contract = matrix_conclusion_contract(node)
    reason = _matrix_manual_reason(node)
    if contract.conclusion_source is MatrixConclusionSource.AI_DEFAULT:
        return "AI 结论（默认采用）\n未人工修改"
    if contract.conclusion_source is MatrixConclusionSource.HUMAN_OVERRIDE:
        return "\n".join(
            value
            for value in (
                "人工覆盖 AI 结论",
                f"原因：{reason}" if reason else "",
            )
            if value
        )
    if contract.conclusion_source is MatrixConclusionSource.HUMAN_NATIVE:
        return "\n".join(
            value
            for value in (
                "人工录入",
                f"原因：{reason}" if reason else "",
            )
            if value
        )
    if contract.conclusion_source is MatrixConclusionSource.STALE_REFERENCE:
        return "历史结论不可作为当前有效结论"
    if str(node.get("disposition") or "").strip().upper() == "EXCLUDE":
        disposition_reason = str(node.get("disposition_reason") or "").strip()
        return f"排除原因：{disposition_reason}" if disposition_reason else "人工排除"
    return ""


def _matrix_effective_conclusion_details(node: dict[str, Any]) -> str:
    effective_conclusion = _matrix_effective_conclusion(node)
    conclusion_note = _matrix_conclusion_note(node)
    lines = [
        f"有效结论：{effective_conclusion}" if effective_conclusion else "",
        f"来源：{conclusion_note}" if conclusion_note else "",
    ]
    if not any(lines) and _matrix_analysis_eligible(node):
        lines.append(f"状态：{_matrix_analysis_status_label(node)}")
    return "\n".join(value for value in lines if value)


def _display_export_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        preferred = [
            value.get(key)
            for key in (
                "label",
                "summary",
                "assessment",
                "status",
                "decision",
                "reason",
                "rationale",
            )
            if value.get(key) not in (None, "", [], {})
        ]
        return "\n".join(
            dict.fromkeys(_display_export_value(item) for item in preferred)
        )
    if isinstance(value, (list, tuple, set)):
        return "\n".join(
            item for item in (_display_export_value(item) for item in value) if item
        )
    return str(value).strip()


def _matrix_source_label(node: dict[str, Any]) -> str:
    native = []
    from reqpilotx.application.agent_workflow.source_locations import source_location_label
    for ref in node.get('source_refs') or []:
        locator = ref.get('native_locator') or ref.get('location') or (ref.get('source_ref') or {}).get('native_locator')
        if locator and locator.get('format') in {'docx', 'xlsx'}:
            label = source_location_label(locator)
            if label not in native:
                native.append(label)
    if native:
        return ' · '.join(native)
    start = _coerce_optional_int(node.get("page_start") or node.get("source_page"))
    end = (
        _coerce_optional_int(node.get("page_end") or node.get("source_page_end"))
        or start
    )
    if start is None:
        pages = []
        for ref in node.get("source_refs") or []:
            if not isinstance(ref, dict):
                continue
            page = _coerce_optional_int(
                ref.get("page_number") or ref.get("page_start") or ref.get("page")
            )
            if page is not None:
                pages.append(page)
        if pages:
            start, end = min(pages), max(pages)
    if start is None:
        return ""
    return f"第 {start} 页" if end in {None, start} else f"第 {start}–{end} 页"


def _matrix_node_logical_table_ids(node: dict[str, Any]) -> list[str]:
    # Matrix V1 binds logical identity explicitly.  Raw table_refs preserve
    # physical TableIR parts and must never be promoted into logical IDs.
    values = node.get("logical_table_ids") or []
    if isinstance(values, str):
        values = [values]
    return list(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def _append_hidden_matrix_meta_sheet(
    *,
    workbook: xlsxwriter.Workbook,
    formats: _WorkbookFormats,
    snapshot: dict[str, Any],
    nodes: list[dict[str, Any]],
    matrix_row_by_node: dict[str, int],
    matrix_row_span_by_node: dict[str, tuple[int, int]],
    table_sheet_names: dict[str, str],
    project_name: str,
    customer_name: str,
    version_label: str,
    export_mode: str,
    compare_version_label: str,
    compare_matrix_snapshot: dict[str, Any] | Any | None,
    table_stats: dict[str, Any],
    oversized_text_detail_count: int,
    figure_detail_reference_count: int,
    unresolved_figure_reference_count: int,
) -> None:
    sheet = workbook.add_worksheet("_ReqPilotXMeta")
    sheet.hide_gridlines(2)
    sheet.set_column("A:A", 28)
    sheet.set_column("B:B", 72)
    sheet.set_column("C:U", 22)
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    baseline = (
        _plain_export_payload(compare_matrix_snapshot)
        if compare_matrix_snapshot is not None
        else None
    )
    baseline_nodes = (
        baseline.get("nodes")
        if isinstance(baseline, dict) and isinstance(baseline.get("nodes"), list)
        else []
    )
    metadata = [
        ("SchemaVersion", snapshot.get("schema_version") or ""),
        ("SnapshotSHA256", hashlib.sha256(canonical).hexdigest()),
        ("TreeSHA256", snapshot.get("tree_sha256") or ""),
        ("State", snapshot.get("state") or ""),
        ("Project", project_name),
        ("Customer", customer_name),
        ("RequirementSetId", snapshot.get("requirement_set_id") or ""),
        ("RequirementSetStatus", snapshot.get("requirement_set_status") or ""),
        ("VersionLabel", version_label),
        ("ExportMode", export_mode),
        ("ComparedVersion", compare_version_label),
        (
            "ComparedSnapshotTreeSHA256",
            (baseline or {}).get("tree_sha256") if isinstance(baseline, dict) else "",
        ),
        ("ComparedSnapshotNodeCount", len(baseline_nodes)),
        (
            "VisibleMatrixNodeCount",
            sum(
                1
                for node in nodes
                if str(node.get("matrix_visibility") or "").strip().upper() == "VISIBLE"
            ),
        ),
        (
            "HumanExcludedMatrixNodeCount",
            sum(
                1
                for node in nodes
                if str(node.get("matrix_visibility") or "").strip().upper()
                == "HUMAN_EXCLUDED"
            ),
        ),
        ("MatrixLogicalRowCount", len(nodes)),
        ("OversizedTextDetailCount", oversized_text_detail_count),
        ("FigureDetailReferenceCount", figure_detail_reference_count),
        ("UnresolvedFigureReferenceCount", unresolved_figure_reference_count),
        (
            "FigureSourceCapability",
            "AVAILABLE"
            if figure_detail_reference_count and unresolved_figure_reference_count == 0
            else "PARTIAL"
            if figure_detail_reference_count
            else "NOT_REFERENCED",
        ),
        ("CustomerConfirmationCapability", "UNAVAILABLE_IN_SNAPSHOT_V1"),
        (
            "AnalysisBasisCapability",
            "AI_REASON_AND_RUNTIME_BASIS_WHEN_UPSTREAM_PROVIDES_DATA",
        ),
        (
            "AnalysisOutputCapability",
            "AVAILABLE"
            if any(
                _matrix_current_decision_label(node)
                or _matrix_conclusion_source_summary(node)
                or _matrix_analysis_basis_text(node)
                for node in nodes
            )
            else "NO_CURRENT_RESULTS",
        ),
        ("LogicalTableCount", len(table_sheet_names)),
        ("CellImageReferenceCount", int(table_stats.get("cell_images") or 0)),
        (
            "CellImageInsertedCount",
            int(table_stats.get("cell_images_inserted") or 0),
        ),
        ("CellImageFailedCount", int(table_stats.get("cell_images_failed") or 0)),
        ("TableFallbackCount", int(table_stats.get("fallbacks") or 0)),
        ("TableExportWarnings", "\n".join(table_stats.get("warnings") or [])),
        ("ExportedAt", datetime.now().isoformat(timespec="seconds")),
    ]
    for row, (label, value) in enumerate(metadata):
        sheet.write(row, 0, label, formats.meta_label)
        sheet.write(row, 1, value, formats.meta_value)
    cursor = len(metadata) + 2
    sheet.write_row(
        cursor,
        0,
        [
            "NodeId",
            "ParentId",
            "MatrixRowStart",
            "MatrixRowEnd",
            "RequirementEntryId",
            "TableRecordId",
            "SaveRevision",
            "UpdatedAt",
            "VisibleParentId",
            "VisibleDepth",
            "SourceOrder",
            "MatrixVisibility",
            "Disposition",
            "DispositionReason",
            "AnalysisEligibility",
            "AnalysisStatus",
            "AnalysisResultState",
            "EffectiveConclusion",
            "ConclusionSource",
            "OverridesAi",
            "FrozenParentId",
        ],
        formats.header,
    )
    cursor += 1
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        row_span = matrix_row_span_by_node.get(node_id, (0, 0))
        conclusion = matrix_conclusion_contract(node)
        sheet.write_row(
            cursor,
            0,
            [
                node_id,
                node.get("parent_id") or "",
                row_span[0] or matrix_row_by_node.get(node_id, ""),
                row_span[1] or matrix_row_by_node.get(node_id, ""),
                node.get("requirement_entry_id") or "",
                node.get("table_record_id") or "",
                node.get("save_revision") or 0,
                node.get("updated_at") or "",
                node.get("_visible_parent_id") or "",
                node.get("depth") or 0,
                node.get("source_order") or 0,
                node.get("matrix_visibility") or "",
                node.get("disposition") or "",
                node.get("disposition_reason") or "",
                node.get("analysis_eligibility") or "",
                node.get("analysis_status") or "",
                node.get("analysis_result_state") or "",
                conclusion.effective_conclusion,
                conclusion.conclusion_source.value,
                conclusion.overrides_ai,
                node.get("parent_id") or "",
            ],
            formats.meta_value,
        )
        cursor += 1
    cursor += 1
    sheet.write_row(
        cursor,
        0,
        ["LogicalTableId", "Worksheet", "ProjectionId", "ProjectionMode"],
        formats.header,
    )
    cursor += 1
    for table in snapshot.get("logical_tables") or []:
        if not isinstance(table, dict):
            continue
        logical_id = str(table.get("logical_table_id") or "")
        sheet.write_row(
            cursor,
            0,
            [
                logical_id,
                table_sheet_names.get(logical_id, ""),
                table.get("projection_id") or "",
                table.get("projection_mode") or "",
            ],
            formats.meta_value,
        )
        cursor += 1
    sheet.hide()


def _workbook_font_name() -> str:
    """Return the best CJK-capable font name for the current platform."""
    if sys.platform == "win32":
        return "Microsoft YaHei"
    if sys.platform == "darwin":
        return "PingFang SC"
    return "Noto Sans CJK SC"


def _build_export_source_image_context(
    *,
    source_file_path: str | Path,
    source_sha256: str,
    source_asset_loader: Callable[[str, str, int], bytes | None] | None = None,
) -> _ExportSourceImageContext | None:
    source_path_text = str(source_file_path or "").strip()
    normalized_sha256 = str(source_sha256 or "").strip().lower()
    crop_binding_available = bool(
        source_path_text and _SHA256_PATTERN.fullmatch(normalized_sha256)
    )
    if source_asset_loader is None and not crop_binding_available:
        return None
    return _ExportSourceImageContext(
        source_file_path=Path(source_path_text) if crop_binding_available else None,
        source_sha256=normalized_sha256 if crop_binding_available else "",
        crop_cache={},
        asset_cache={},
        object_asset_cache={},
        source_asset_loader=source_asset_loader,
        source_verified=None,
    )


def _build_formats(workbook: xlsxwriter.Workbook) -> _WorkbookFormats:
    base = {
        "border": 1,
        "border_color": _BRAND_BORDER,
        "text_wrap": True,
        "valign": "top",
        "font_name": _workbook_font_name(),
        "font_size": _WORKBOOK_FONT_SIZE,
        "font_color": _BRAND_TEXT,
    }

    row_even = workbook.add_format({**base, "bg_color": _BRAND_ROW_ALT})
    row_odd = workbook.add_format({**base, "bg_color": "#FFFFFF"})
    paragraph_even = workbook.add_format(
        {**base, "bg_color": _BRAND_ROW_ALT, "align": "left"}
    )
    paragraph_odd = workbook.add_format(
        {**base, "bg_color": "#FFFFFF", "align": "left"}
    )
    header = workbook.add_format(
        {
            **base,
            "bg_color": _BRAND_PRIMARY,
            "bold": True,
            "font_color": _BRAND_HEADER_TEXT,
            "align": "center",
            "valign": "vcenter",
        }
    )
    meta_fill = workbook.add_format(
        {
            **base,
            "bg_color": _BRAND_BACKGROUND,
            "bold": True,
            "font_color": _BRAND_TEXT,
            "valign": "vcenter",
        }
    )
    brand_bar = workbook.add_format({"bg_color": _BRAND_SECONDARY, "border": 0})
    meta_label = workbook.add_format(
        {
            **base,
            "bg_color": _BRAND_HEADER_BACKGROUND,
            "bold": True,
            "font_color": _BRAND_TEXT,
        }
    )
    meta_value = workbook.add_format(base)
    diff_header = workbook.add_format(
        {
            **base,
            "bg_color": _BRAND_PRIMARY,
            "bold": True,
            "font_color": _BRAND_HEADER_TEXT,
            "align": "center",
            "valign": "vcenter",
        }
    )
    diff_even = workbook.add_format({**base, "bg_color": _BRAND_ROW_ALT})
    diff_odd = workbook.add_format({**base, "bg_color": "#FFFFFF"})
    decision_formats = {
        token: workbook.add_format(
            {
                **base,
                "bg_color": color,
            }
        )
        for token, color in {
            "ACCEPT": "#E8F5E9",
            "REJECT": "#FFEBEE",
            "DEVIATION_ACCEPT": "#FFF3E0",
            "NEED_CLARIFICATION": "#E2EBF8",
            "INFO": "#F3E5F5",
            "DEFAULT": "#FFFFFF",
        }.items()
    }
    return _WorkbookFormats(
        header=header,
        meta_fill=meta_fill,
        brand_bar=brand_bar,
        row_even=row_even,
        row_odd=row_odd,
        paragraph_even=paragraph_even,
        paragraph_odd=paragraph_odd,
        diff_header=diff_header,
        diff_even=diff_even,
        diff_odd=diff_odd,
        meta_label=meta_label,
        meta_value=meta_value,
        decision_formats=decision_formats,
    )


def _block_row_formats(formats: _WorkbookFormats, block_index: int) -> dict[str, Any]:
    even = block_index % 2 == 0
    return {
        "meta": formats.row_even if even else formats.row_odd,
        "paragraph": formats.paragraph_even if even else formats.paragraph_odd,
    }


def _decision_format(formats: _WorkbookFormats, value: Any) -> Any:
    token = str(value or "").strip().upper() or "DEFAULT"
    return formats.decision_formats.get(token, formats.decision_formats["DEFAULT"])


def _append_requirement_block(
    *,
    ws,
    row_index: int,
    item: dict[str, Any],
    requirement_col: int,
    ai_assessment_col: int,
    final_decision_col: int,
    comments_col: int,
    requirement_area_width_px: int,
    column_widths_px: dict[int, int],
    row_formats: dict[str, Any],
    ai_assessment_format: Any,
    decision_format: Any,
    source_image_context: _ExportSourceImageContext | None,
) -> tuple[int, int, int, list[str]]:
    split = _split_requirement_blocks(_display_requirement_text(item))
    blocks = _build_export_blocks(item, split) or [("paragraph", "")]
    prepared_rows, table_image_count, table_fallback_text_count, warnings = (
        _prepare_requirement_rows(
            blocks=blocks,
            item=item,
            requirement_area_width_px=requirement_area_width_px,
            fallback_text_count=split.table_fallback_text_count,
            source_image_context=source_image_context,
        )
    )

    block_rows = len(prepared_rows)
    start_row = row_index
    end_row = row_index + block_rows - 1

    ai_decision_value = _ai_decision_label(item)
    final_decision_value = item.get("final_decision") or ""

    _ensure_required_block_height(
        prepared_rows,
        required_total_height=max(
            _total_prepared_row_height(prepared_rows),
            _estimate_total_text_height(
                item.get("display_req_code") or item.get("req_code") or "",
                column_widths_px[0],
            ),
            _estimate_total_text_height(
                _display_parent_section_label(item), column_widths_px[1]
            ),
            _estimate_total_text_height(
                ai_decision_value, column_widths_px[ai_assessment_col]
            ),
            _estimate_total_text_height(
                final_decision_value, column_widths_px[final_decision_col]
            ),
            _estimate_total_text_height(
                item.get("engineer_reason") or "", column_widths_px[comments_col]
            ),
        ),
    )

    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=0,
        value=item.get("display_req_code") or item.get("req_code") or "",
        cell_format=row_formats["meta"],
    )
    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=1,
        value=_display_parent_section_label(item),
        cell_format=row_formats["meta"],
    )
    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=ai_assessment_col,
        value=ai_decision_value,
        cell_format=ai_assessment_format,
    )
    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=final_decision_col,
        value=final_decision_value,
        cell_format=decision_format,
    )
    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=comments_col,
        value=item.get("engineer_reason") or "",
        cell_format=row_formats["meta"],
    )

    cursor = start_row
    for prepared in prepared_rows:
        ws.set_row(cursor, prepared.row_height)
        if prepared.kind == "paragraph":
            ws.write(cursor, requirement_col, prepared.text, row_formats["paragraph"])
        elif prepared.kind == "image" and prepared.image_buffer is not None:
            ws.write_blank(cursor, requirement_col, None, row_formats["paragraph"])
            ws.insert_image(
                cursor,
                requirement_col,
                "table.png",
                {
                    "image_data": prepared.image_buffer,
                    "object_position": 2,
                    "x_scale": prepared.x_scale,
                    "y_scale": prepared.y_scale,
                    "x_offset": 2,
                    "y_offset": 2,
                },
            )
        else:
            ws.write_blank(cursor, requirement_col, None, row_formats["paragraph"])
        cursor += 1

    return end_row + 1, table_image_count, table_fallback_text_count, warnings


def _write_brand_header(
    ws,
    *,
    formats: _WorkbookFormats,
    project_name: str,
    customer_name: str,
    source_file_name: str,
    export_mode: str = "FORMAL",
    last_col: int = 5,
) -> None:
    last_col = max(5, int(last_col))
    for col in range(last_col + 1):
        ws.write_blank(0, col, None, formats.meta_fill)
        ws.write_blank(1, col, None, formats.brand_bar)
    source_label = f"来源文件：{source_file_name}" if source_file_name else ""
    mode_label = "草稿" if str(export_mode or "").upper() == "DRAFT" else "正式"
    if last_col >= 9:
        ws.merge_range(0, 1, 0, 2, f"项目：{project_name}", formats.meta_fill)
        ws.merge_range(0, 3, 0, 4, f"客户：{customer_name}", formats.meta_fill)
        ws.merge_range(0, 5, 0, 8, source_label, formats.meta_fill)
        ws.write(0, 9, mode_label, formats.meta_fill)
    else:
        ws.write(0, 1, f"项目：{project_name}", formats.meta_fill)
        ws.write(0, 2, f"客户：{customer_name}", formats.meta_fill)
        ws.merge_range(0, 3, 0, max(3, last_col - 1), source_label, formats.meta_fill)
        ws.write(0, last_col, mode_label, formats.meta_fill)
    ws.set_row(0, 38)
    ws.set_row(1, 7)
    _insert_brand_logo(ws)


@lru_cache(maxsize=1)
def _load_brand_logo_bytes() -> tuple[bytes, int, int] | None:
    for path in _BRAND_LOGO_PATH_CANDIDATES:
        try:
            if not path.exists():
                continue
            raw = (
                b64decode(
                    "".join(path.read_text(encoding="ascii").split()), validate=True
                )
                if path.suffix == ".base64"
                else path.read_bytes()
            )
            with PILImage.open(BytesIO(raw)) as image:
                return raw, int(image.width), int(image.height)
        except Exception:
            continue
    return None


def _insert_brand_logo(ws) -> None:
    logo_payload = _load_brand_logo_bytes()
    if logo_payload is None:
        return
    raw, width, _ = logo_payload
    target_width_px = 118
    scale = min(1.0, target_width_px / max(width, 1))
    ws.insert_image(
        0,
        0,
        "schaeffler-logo.png",
        {
            "image_data": BytesIO(raw),
            "object_position": 2,
            "x_scale": scale,
            "y_scale": scale,
            "x_offset": 5,
            "y_offset": 8,
        },
    )


def _build_export_blocks(
    item: dict[str, Any], split: _RequirementBlockSplit
) -> list[tuple[str, Any]]:
    table_refs = [
        dict(ref)
        for ref in item.get("table_refs") or []
        if isinstance(ref, dict) and str(ref.get("table_id") or "").strip()
    ]
    table_ref_map = {str(ref.get("table_id") or "").strip(): ref for ref in table_refs}

    content_blocks = item.get("content_blocks")
    if isinstance(content_blocks, list):
        blocks: list[tuple[str, Any]] = []
        used_table_ids: set[str] = set()
        for block_index, block in enumerate(content_blocks):
            if not isinstance(block, dict):
                continue
            kind = str(block.get("kind") or "").strip().lower()
            if kind == "paragraph":
                text = str(block.get("text") or "")
                if text.strip():
                    blocks.append(("paragraph", text))
                continue
            if kind == "table":
                table_id = str(block.get("table_id") or "").strip()
                ref = table_ref_map.get(table_id)
                if ref:
                    used_table_ids.add(table_id)
                    blocks.append(("table_ref", ref))
                continue
            if kind in {"image", "snapshot", "visual"}:
                ref = _snapshot_block_to_table_ref(block, block_index=block_index)
                if ref:
                    used_table_ids.add(str(ref.get("table_id") or ""))
                    blocks.append(("table_ref", ref))
        if blocks:
            for ref in table_refs:
                table_id = str(ref.get("table_id") or "").strip()
                if table_id and table_id not in used_table_ids:
                    blocks.append(("table_ref", ref))
            return blocks

    if table_refs:
        if split.blocks:
            return _merge_split_blocks_with_table_refs(split.blocks, table_refs)
        text = _display_requirement_text(item)
        blocks = [("paragraph", text)] if text else []
        blocks.extend(("table_ref", ref) for ref in table_refs)
        return blocks

    return split.blocks


def _snapshot_block_to_table_ref(
    block: dict[str, Any], *, block_index: int
) -> dict[str, Any] | None:
    slice_snapshot_path = str(
        block.get("slice_snapshot_path")
        or block.get("snapshot_path")
        or block.get("image_path")
        or ""
    ).strip()
    source_snapshot_path = str(
        block.get("source_snapshot_path") or slice_snapshot_path
    ).strip()
    if not slice_snapshot_path and not source_snapshot_path:
        return None
    table_id = str(
        block.get("table_id")
        or block.get("snapshot_id")
        or f"snapshot_block_{block_index + 1}"
    ).strip()
    title = str(
        block.get("title") or block.get("text") or block.get("caption") or table_id
    ).strip()
    export_variant = str(block.get("export_variant") or "").strip().lower()
    return {
        "table_id": table_id,
        "table_title": title,
        "row_range": [],
        "row_ids": [],
        "inherited_headers": [],
        "slice_rows": [],
        "slice_ai_projection": title,
        "slice_render_mode": "snapshot_only",
        "slice_snapshot_path": slice_snapshot_path or source_snapshot_path,
        "source_snapshot_path": source_snapshot_path or slice_snapshot_path,
        "export_variant": export_variant
        or ("source" if source_snapshot_path else "slice"),
    }


def _merge_split_blocks_with_table_refs(
    split_blocks: list[tuple[str, Any]],
    table_refs: list[dict[str, Any]],
) -> list[tuple[str, Any]]:
    merged: list[tuple[str, Any]] = []
    ref_iter = iter(table_refs)
    for kind, payload in split_blocks:
        if kind == "paragraph":
            merged.append((kind, payload))
            continue
        if kind == "table":
            ref = next(ref_iter, None)
            if ref is not None:
                merged.append(("table_ref", ref))
            else:
                merged.append((kind, payload))
    merged.extend(("table_ref", ref) for ref in ref_iter)
    return merged


def _prepare_requirement_rows(
    *,
    blocks: list[tuple[str, Any]],
    item: dict[str, Any],
    requirement_area_width_px: int,
    fallback_text_count: int,
    source_image_context: _ExportSourceImageContext | None,
) -> tuple[list[_PreparedRequirementRow], int, int, list[str]]:
    prepared_rows: list[_PreparedRequirementRow] = []
    table_image_count = 0
    table_fallback_text_count = fallback_text_count
    warnings: list[str] = []

    for kind, payload in blocks:
        if kind == "paragraph":
            prepared_rows.extend(
                _prepare_paragraph_rows(str(payload or ""), requirement_area_width_px)
            )
            continue

        if kind == "table_ref":
            image_buffer, image_width_px, image_height_px, warning_text = (
                _table_ref_snapshot(
                    payload,
                    requirement_area_width_px,
                    source_image_context=source_image_context,
                )
            )
            if warning_text:
                warnings.append(
                    f"{str(item.get('display_req_code') or item.get('req_code') or 'UNKNOWN')} / {str(payload.get('table_id') or '')}: {warning_text}"
                )
            if image_buffer is None:
                fallback_text = _table_ref_to_text(payload)
                prepared_rows.extend(
                    _prepare_paragraph_rows(fallback_text, requirement_area_width_px)
                )
                table_fallback_text_count += 1
            else:
                prepared_rows.extend(
                    _prepare_image_rows(
                        image_buffer=image_buffer,
                        image_width_px=image_width_px,
                        image_height_px=image_height_px,
                        max_width_px=requirement_area_width_px,
                    )
                )
                table_image_count += 1
            continue

        try:
            image_buffer, image_width_px, image_height_px = _render_table_image(
                payload, max_width_px=requirement_area_width_px
            )
        except Exception:
            fallback_text = _table_rows_to_text(payload)
            prepared_rows.extend(
                _prepare_paragraph_rows(fallback_text, requirement_area_width_px)
            )
            table_fallback_text_count += 1
        else:
            prepared_rows.extend(
                _prepare_image_rows(
                    image_buffer=image_buffer,
                    image_width_px=image_width_px,
                    image_height_px=image_height_px,
                    max_width_px=requirement_area_width_px,
                )
            )
            table_image_count += 1

    if not prepared_rows:
        prepared_rows.append(_PreparedRequirementRow(kind="blank"))
    return prepared_rows, table_image_count, table_fallback_text_count, warnings


def _table_ref_snapshot(
    payload: dict[str, Any],
    max_width_px: int,
    *,
    source_image_context: _ExportSourceImageContext | None = None,
) -> tuple[BytesIO | None, int, int, str]:
    export_variant = str(payload.get("export_variant") or "").strip().lower()
    if export_variant not in {"slice", "source"}:
        export_variant = (
            "source"
            if str(payload.get("source_snapshot_path") or "").strip()
            else "slice"
        )
    if export_variant == "source":
        source_snapshot_path = str(payload.get("source_snapshot_path") or "").strip()
        if source_snapshot_path:
            try:
                raw = load_snapshot_bytes(source_snapshot_path)
                buffer = BytesIO(raw)
                image = PILImage.open(BytesIO(raw))
                return buffer, int(image.width), int(image.height), ""
            except Exception:
                pass
        export_variant = "slice"
        warning_text = "source snapshot missing, fell back to slice"
    else:
        warning_text = ""

    snapshot_path = str(payload.get("slice_snapshot_path") or "").strip()
    if (
        snapshot_path
        and str(payload.get("slice_render_mode") or "").strip().lower() != "structured"
    ):
        try:
            raw = load_snapshot_bytes(snapshot_path)
            buffer = BytesIO(raw)
            image = PILImage.open(BytesIO(raw))
            return buffer, int(image.width), int(image.height), warning_text
        except Exception:
            pass

    rich_rows, rich_header_row_count = _rich_table_ref_rows(payload)
    if rich_rows:
        try:
            buffer, image_width, image_height = _render_table_image(
                rich_rows,
                max_width_px=max_width_px,
                header_row_count=rich_header_row_count,
            )
            return buffer, image_width, image_height, warning_text
        except Exception:
            pass

    headers = [str(cell or "") for cell in payload.get("inherited_headers") or []]
    rows = [
        [str(cell or "") for cell in row]
        for row in payload.get("slice_rows") or []
        if isinstance(row, list)
    ]
    if headers or rows:
        try:
            buffer, image_width, image_height = _render_table_image(
                [headers, *rows] if headers else rows,
                max_width_px=max_width_px,
                header_row_count=1 if headers else 0,
            )
            return buffer, image_width, image_height, warning_text
        except Exception:
            return None, 0, 0, warning_text
    return None, 0, 0, warning_text


def _exact_pdf_image_bytes(context, page_number, bbox):
    """Keep native pixels for one complete, unrotated PDF image placement.

    Composite overlays, cropped ranges and transformed images stay on the page
    renderer, which preserves their actual visible arrangement.
    """
    try:
        import pymupdf
        source = context.source_file_path
        if context.source_verified is None:
            context.source_verified = hashlib.sha256(source.read_bytes()).hexdigest() == context.source_sha256
        if not context.source_verified:
            return None
        with pymupdf.open(source) as document:
            if not 1 <= page_number <= len(document):
                return None
            page = document[page_number - 1]
            rect = pymupdf.Rect(bbox)
            if page.rotation or not page.rect.contains(rect) or rect.is_empty:
                return None
            images = [item for item in page.get_image_info(xrefs=True)
                      if item.get('xref', 0) > 0 and max(abs(a-b) for a,b in zip(item['bbox'], bbox)) < 0.01]
            if len(images) != 1:
                return None
            image = images[0]
            a,b,c,d,_,_ = image['transform']
            if abs(b) > 0.001 or abs(c) > 0.001 or a <= 0 or d <= 0 or image['width'] * image['height'] > 20_000_000:
                return None
            log = page.get_bboxlog()
            placements = [i for i, value in enumerate(log) if value[0] == 'fill-image'
                          and max(abs(x-y) for x,y in zip(value[1], bbox)) < 0.01]
            if len(placements) != 1:
                return None
            # Later paint over this image belongs to the visible source too.
            if any(kind != 'ignore-text' and (pymupdf.Rect(bounds) & rect).get_area() > 0.01
                   for kind, bounds, *_ in log[placements[0] + 1:]):
                return None
            xref = image['xref']
            smask = next((item[1] for item in page.get_images(full=True) if item[0] == xref), 0)
            if smask:
                raw = pymupdf.Pixmap(pymupdf.Pixmap(document, xref), pymupdf.Pixmap(document, smask)).tobytes('png')
            else:
                raw = document.extract_image(xref)['image']
            return raw if 0 < len(raw) <= _MAX_SOURCE_PACKAGE_ASSET_BYTES else None
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return None


def _source_pdf_crop_payload(
    evidence: dict[str, Any],
    *,
    source_image_context: _ExportSourceImageContext | None,
    require_declared_rendering: bool,
) -> tuple[BytesIO, int, int] | None:
    if (
        source_image_context is None
        or source_image_context.source_file_path is None
        or source_image_context.source_file_path.suffix.lower() != ".pdf"
    ):
        return None
    if require_declared_rendering:
        rendering = evidence.get("rendering")
        if not isinstance(rendering, dict):
            return None
        if str(rendering.get("mode") or "").strip().upper() != "SOURCE_PDF_CROP":
            return None
        if rendering.get("available") is False:
            return None

    location = evidence.get("location") or evidence.get("source_location")
    if not isinstance(location, dict):
        location = next(
            (
                item.get("location") or item.get("source_location")
                for item in evidence.get("source_refs") or []
                if isinstance(item, dict)
                and isinstance(
                    item.get("location") or item.get("source_location"),
                    dict,
                )
            ),
            None,
        )
    if not isinstance(location, dict):
        return None
    if str(location.get("format") or "").strip().lower() != "pdf":
        return None
    page_number = _coerce_optional_int(location.get("page_number"))
    raw_bbox = location.get("bbox")
    if (
        page_number is None
        or page_number < 1
        or not isinstance(raw_bbox, (list, tuple))
        or len(raw_bbox) != 4
    ):
        return None
    try:
        bbox = tuple(float(value) for value in raw_bbox)
    except (TypeError, ValueError):
        return None
    cache_key = (page_number, bbox)
    raw = source_image_context.crop_cache.get(cache_key)
    if raw is None:
        try:
            raw = _exact_pdf_image_bytes(source_image_context, page_number, bbox)
            if raw is None:
                raw = render_pdf_source_crop(
                    source_image_context.source_file_path,
                    page_number=page_number,
                    bbox=bbox,
                    expected_source_sha256=source_image_context.source_sha256,
                )
        except (PdfSourceCropRejected, OSError, ValueError):
            return None
        source_image_context.crop_cache[cache_key] = raw
    try:
        with PILImage.open(BytesIO(raw)) as image:
            width, height = int(image.width), int(image.height)
    except Exception:
        return None
    return BytesIO(raw), width, height


def _source_package_asset_payload(
    evidence: dict[str, Any],
    *,
    source_image_context: _ExportSourceImageContext | None,
) -> tuple[BytesIO, int, int] | None:
    if source_image_context is None:
        return None
    source_path = source_image_context.source_file_path
    if source_path is None:
        return None
    suffix = source_path.suffix.lower()
    media_prefix = (
        "word/media/"
        if suffix == ".docx"
        else "xl/media/"
        if suffix in {".xlsx", ".xlsm"}
        else ""
    )
    expected_asset_sha256 = str(evidence.get("content_sha256") or "").strip().lower()
    if not media_prefix or not _SHA256_PATTERN.fullmatch(expected_asset_sha256):
        return None

    cached = source_image_context.asset_cache.get(expected_asset_sha256, ...)
    if cached is None:
        return None
    if cached is ...:
        if source_image_context.source_verified is None:
            try:
                source_image_context.source_verified = (
                    hashlib.sha256(source_path.read_bytes()).hexdigest()
                    == source_image_context.source_sha256
                )
            except OSError:
                source_image_context.source_verified = False
        if not source_image_context.source_verified:
            source_image_context.asset_cache[expected_asset_sha256] = None
            return None

        matched: bytes | None = None
        try:
            with zipfile.ZipFile(source_path) as package:
                scanned_bytes = 0
                for member in package.infolist():
                    normalized_name = member.filename.replace("\\", "/")
                    if member.is_dir() or not normalized_name.startswith(media_prefix):
                        continue
                    scanned_bytes += max(int(member.file_size), 0)
                    if scanned_bytes > _MAX_SOURCE_PACKAGE_SCAN_BYTES:
                        break
                    if (
                        member.file_size < 1
                        or member.file_size > _MAX_SOURCE_PACKAGE_ASSET_BYTES
                    ):
                        continue
                    raw = package.read(member)
                    if len(raw) != member.file_size:
                        continue
                    if hashlib.sha256(raw).hexdigest() != expected_asset_sha256:
                        continue
                    matched = raw
                    break
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
            matched = None
        source_image_context.asset_cache[expected_asset_sha256] = matched
        cached = matched
    if cached is None:
        return None
    try:
        with PILImage.open(BytesIO(cached)) as image:
            width, height = int(image.width), int(image.height)
    except Exception:
        return None
    return BytesIO(cached), width, height


def _source_object_asset_payload(
    evidence: dict[str, Any],
    *,
    source_image_context: _ExportSourceImageContext | None,
) -> tuple[BytesIO, int, int] | None:
    """Load one content-addressed SourceIR asset with full identity checks."""

    if source_image_context is None or source_image_context.source_asset_loader is None:
        return None
    candidates = [evidence]
    candidates.extend(
        item for item in evidence.get("source_refs") or [] if isinstance(item, dict)
    )
    asset_evidence = next(
        (
            item
            for item in candidates
            if _SHA256_PATTERN.fullmatch(
                str(item.get("content_sha256") or "").strip().lower()
            )
            and str(item.get("media_type") or "").startswith("image/")
        ),
        None,
    )
    if asset_evidence is None:
        return None
    expected_sha256 = str(asset_evidence.get("content_sha256") or "").strip().lower()
    storage_key = str(asset_evidence.get("storage_key") or "").strip()
    if not _source_asset_storage_key_matches_sha256(
        storage_key,
        expected_sha256=expected_sha256,
    ):
        return None
    cache_key = (storage_key, expected_sha256)
    cached = source_image_context.object_asset_cache.get(cache_key, ...)
    if cached is ...:
        try:
            raw = source_image_context.source_asset_loader(
                storage_key,
                expected_sha256,
                _MAX_SOURCE_PACKAGE_ASSET_BYTES,
            )
            if raw is not None and (
                not isinstance(raw, bytes)
                or len(raw) < 1
                or len(raw) > _MAX_SOURCE_PACKAGE_ASSET_BYTES
                or hashlib.sha256(raw).hexdigest() != expected_sha256
            ):
                raw = None
        except Exception:
            raw = None
        source_image_context.object_asset_cache[cache_key] = raw
        cached = raw
    if cached is None:
        return None
    try:
        with PILImage.open(BytesIO(cached)) as image:
            width, height = int(image.width), int(image.height)
    except Exception:
        return None
    return BytesIO(cached), width, height


def _source_asset_storage_key_matches_sha256(
    storage_key: str,
    *,
    expected_sha256: str,
) -> bool:
    image_match = _SOURCE_ASSET_KEY_PATTERN.fullmatch(storage_key)
    if image_match is not None:
        return bool(
            image_match.group("prefix") == expected_sha256[:2]
            and image_match.group("sha256") == expected_sha256
        )
    page_match = _SOURCE_PAGE_RENDER_KEY_PATTERN.fullmatch(storage_key)
    if page_match is None:
        return False
    source_sha256 = page_match.group("source_sha256")
    return bool(
        page_match.group("prefix") == source_sha256[:2]
        and page_match.group("sha256") == expected_sha256
    )


def _table_ref_to_text(payload: dict[str, Any]) -> str:
    rich_rows, rich_header_row_count = _rich_table_ref_rows(payload)
    if rich_rows:
        return _table_rows_to_text(
            rich_rows,
            include_header=rich_header_row_count > 0,
        )
    headers = [str(cell or "") for cell in payload.get("inherited_headers") or []]
    rows = [
        [str(cell or "") for cell in row]
        for row in payload.get("slice_rows") or []
        if isinstance(row, list)
    ]
    if headers:
        return _table_rows_to_text([headers, *rows], include_header=True)
    if rows:
        return _table_rows_to_text(rows, include_header=False)
    return str(payload.get("slice_ai_projection") or "").strip()


def _rich_table_ref_rows(
    payload: dict[str, Any],
) -> tuple[list[list[str]], int]:
    table_payload = payload.get("table_payload") or payload.get("table")
    rich = table_payload if isinstance(table_payload, dict) else payload
    cells = [cell for cell in rich.get("cells") or [] if isinstance(cell, dict)]
    if not cells:
        return [], 0
    row_count = max(
        _coerce_optional_int(rich.get("row_count")) or 0,
        max(
            (
                (_coerce_optional_int(cell.get("row")) or 0)
                + max(
                    1,
                    _coerce_optional_int(cell.get("row_span")) or 1,
                )
                for cell in cells
            ),
            default=0,
        ),
    )
    column_count = max(
        _coerce_optional_int(rich.get("column_count")) or 0,
        max(
            (
                (_coerce_optional_int(cell.get("column")) or 0)
                + max(
                    1,
                    _coerce_optional_int(cell.get("column_span")) or 1,
                )
                for cell in cells
            ),
            default=0,
        ),
    )
    if row_count <= 0 or column_count <= 0:
        return [], 0
    rows = [["" for _ in range(column_count)] for _ in range(row_count)]
    for cell in cells:
        row = _coerce_optional_int(cell.get("row")) or 0
        column = _coerce_optional_int(cell.get("column")) or 0
        if row >= row_count or column >= column_count:
            continue
        rows[row][column] = str(
            cell.get("display_value")
            if cell.get("display_value") is not None
            else cell.get("raw_text")
            if cell.get("raw_text") is not None
            else cell.get("text") or ""
        )
    header_row_count = max(
        (
            (_coerce_optional_int(region.get("row")) or 0)
            + max(
                1,
                _coerce_optional_int(region.get("row_span")) or 1,
            )
            for region in rich.get("header_regions") or []
            if isinstance(region, dict)
        ),
        default=0,
    )
    return rows, min(header_row_count, row_count)


def _write_vertical_block(
    ws, *, start_row: int, end_row: int, col: int, value: Any, cell_format: Any
) -> None:
    if end_row > start_row:
        ws.merge_range(start_row, col, end_row, col, value, cell_format)
    else:
        ws.write(start_row, col, value, cell_format)


def _prepare_paragraph_rows(
    text: str, max_width_px: int
) -> list[_PreparedRequirementRow]:
    wrapped_lines = _wrap_text_to_export_lines(text, max_width_px)
    chunk_size = _max_wrapped_lines_per_row()
    rows: list[_PreparedRequirementRow] = []
    for start in range(0, max(1, len(wrapped_lines)), chunk_size):
        chunk = wrapped_lines[start : start + chunk_size] or [""]
        chunk_text = "\n".join(chunk)
        rows.append(
            _PreparedRequirementRow(
                kind="paragraph",
                text=chunk_text,
                row_height=_estimate_total_text_height(
                    chunk_text, max_width_px, cap_to_excel_row=True
                ),
            )
        )
    return rows or [
        _PreparedRequirementRow(
            kind="paragraph", text="", row_height=_DEFAULT_ROW_HEIGHT_POINTS
        )
    ]


def _prepare_image_rows(
    *,
    image_buffer: BytesIO,
    image_width_px: int,
    image_height_px: int,
    max_width_px: int,
) -> list[_PreparedRequirementRow]:
    width_scale = min(1.0, max_width_px / max(image_width_px, 1))
    fit_scale = min(
        width_scale, _MAX_IMAGE_ROW_RENDER_HEIGHT_PX / max(image_height_px, 1)
    )
    if fit_scale >= _MIN_SINGLE_IMAGE_SCALE:
        rendered_height_px = max(1, int(round(image_height_px * fit_scale)))
        return [
            _PreparedRequirementRow(
                kind="image",
                row_height=_pixels_to_row_height_points(rendered_height_px + 8),
                image_buffer=image_buffer,
                x_scale=fit_scale,
                y_scale=fit_scale,
            )
        ]

    x_scale = width_scale
    y_scale = width_scale
    rendered_height_px = max(1, int(round(image_height_px * width_scale)))
    if rendered_height_px <= _MAX_IMAGE_ROW_RENDER_HEIGHT_PX:
        return [
            _PreparedRequirementRow(
                kind="image",
                row_height=_pixels_to_row_height_points(rendered_height_px + 8),
                image_buffer=image_buffer,
                x_scale=x_scale,
                y_scale=y_scale,
            )
        ]

    raw = image_buffer.getvalue()
    with PILImage.open(BytesIO(raw)) as image:
        segment_source_height = max(
            1, int(_MAX_IMAGE_ROW_RENDER_HEIGHT_PX / max(y_scale, 0.01))
        )
        rows: list[_PreparedRequirementRow] = []
        for top in range(0, image.height, segment_source_height):
            bottom = min(image.height, top + segment_source_height)
            segment = image.crop((0, top, image.width, bottom))
            segment_buffer = BytesIO()
            segment.save(segment_buffer, format="PNG")
            segment_buffer.seek(0)
            segment_render_height_px = max(1, int(round(segment.height * y_scale)))
            rows.append(
                _PreparedRequirementRow(
                    kind="image",
                    row_height=_pixels_to_row_height_points(
                        segment_render_height_px + 8
                    ),
                    image_buffer=segment_buffer,
                    x_scale=x_scale,
                    y_scale=y_scale,
                )
            )
    return rows or [
        _PreparedRequirementRow(
            kind="image",
            row_height=_DEFAULT_ROW_HEIGHT_POINTS,
            image_buffer=image_buffer,
            x_scale=x_scale,
            y_scale=y_scale,
        )
    ]


def _ensure_required_block_height(
    rows: list[_PreparedRequirementRow],
    *,
    required_total_height: float,
) -> None:
    total_height = _total_prepared_row_height(rows)
    if total_height >= required_total_height:
        return

    while total_height + 0.1 < required_total_height:
        adjustable = [
            row
            for row in rows
            if row.kind in {"paragraph", "blank"}
            and row.row_height < _MAX_ROW_HEIGHT_POINTS - 0.1
        ]
        if not adjustable:
            adjustable = [
                row for row in rows if row.row_height < _MAX_ROW_HEIGHT_POINTS - 0.1
            ]
        if not adjustable:
            extra_row_height = min(
                _MAX_ROW_HEIGHT_POINTS,
                max(_DEFAULT_ROW_HEIGHT_POINTS, required_total_height - total_height),
            )
            rows.append(
                _PreparedRequirementRow(kind="blank", row_height=extra_row_height)
            )
            total_height += extra_row_height
            continue

        remaining = required_total_height - total_height
        per_row = remaining / len(adjustable)
        added = 0.0
        for row in adjustable:
            capacity = _MAX_ROW_HEIGHT_POINTS - row.row_height
            delta = min(capacity, per_row)
            if delta <= 0:
                continue
            row.row_height += delta
            added += delta
        total_height += added
        if added <= 0.1:
            rows.append(
                _PreparedRequirementRow(
                    kind="blank", row_height=min(_MAX_ROW_HEIGHT_POINTS, remaining)
                )
            )
            total_height += min(_MAX_ROW_HEIGHT_POINTS, remaining)


def _total_prepared_row_height(rows: list[_PreparedRequirementRow]) -> float:
    return sum(row.row_height for row in rows)


def _wrap_text_to_export_lines(text: str, max_width_px: int) -> list[str]:
    content = str(text or "")
    if not content:
        return [""]
    max_units = max(
        8, (max_width_px - (_WORKBOOK_TEXT_PADDING_X * 2)) // _TABLE_UNIT_PX
    )
    wrapped = _wrap_text_to_units(content, max_units)
    return wrapped.split("\n") or [wrapped]


@lru_cache(maxsize=1)
def _max_wrapped_lines_per_row() -> int:
    usable_height = max(36.0, _MAX_ROW_HEIGHT_POINTS - _WORKBOOK_TEXT_ROW_EXTRA_POINTS)
    return max(1, int(usable_height // _WORKBOOK_TEXT_LINE_HEIGHT_POINTS))


def _estimate_total_text_height(
    text: Any,
    max_width_px: int,
    *,
    cap_to_excel_row: bool = False,
) -> float:
    content = str(text or "")
    if not content.strip():
        return _DEFAULT_ROW_HEIGHT_POINTS
    wrapped_lines = _wrap_text_to_export_lines(content, max_width_px)
    line_count = max(1, len(wrapped_lines))
    height_points = round(
        _WORKBOOK_TEXT_ROW_EXTRA_POINTS
        + (line_count * _WORKBOOK_TEXT_LINE_HEIGHT_POINTS),
        2,
    )
    if cap_to_excel_row:
        return max(
            _DEFAULT_ROW_HEIGHT_POINTS, min(_MAX_ROW_HEIGHT_POINTS, height_points)
        )
    return max(_DEFAULT_ROW_HEIGHT_POINTS, height_points)


def _split_requirement_blocks(text: str) -> _RequirementBlockSplit:
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    blocks: list[tuple[str, Any]] = []
    paragraph: list[str] = []
    table_fallback_text_count = 0

    def flush_paragraph() -> None:
        if not paragraph:
            return
        merged = "\n".join(part for part in paragraph if str(part).strip()).strip()
        paragraph.clear()
        if merged:
            blocks.append(("paragraph", merged))

    index = 0
    while index < len(lines):
        if _starts_markdown_table(lines, index):
            flush_paragraph()
            markdown_mode = _is_markdown_separator_row(
                _parse_markdown_cells(lines[index + 1])
            )
            table_lines = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines) and _looks_like_markdown_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            parsed = _parse_markdown_table(table_lines)
            if not parsed and not markdown_mode:
                parsed = _parse_pipe_table(table_lines)
            if parsed:
                blocks.append(("table", parsed))
                continue
            paragraph.extend(table_lines)
            table_fallback_text_count += 1
            continue

        paragraph.append(lines[index])
        index += 1

    flush_paragraph()
    return _RequirementBlockSplit(
        blocks=blocks, table_fallback_text_count=table_fallback_text_count
    )


def _starts_markdown_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    return _looks_like_markdown_table_row(
        lines[index]
    ) and _looks_like_markdown_table_row(lines[index + 1])


def _parse_markdown_table(lines: list[str]) -> list[list[str]]:
    rows = [
        _parse_markdown_cells(line) for line in lines if _parse_markdown_cells(line)
    ]
    if len(rows) < 3:
        return []
    header = rows[0]
    separator = rows[1]
    if (
        len(header) < 2
        or len(separator) != len(header)
        or not _is_markdown_separator_row(separator)
    ):
        return []
    if any(len(row) != len(header) for row in rows[2:]):
        return []
    return [header, *rows[2:]]


def _parse_pipe_table(lines: list[str]) -> list[list[str]]:
    rows = [
        _parse_markdown_cells(line)
        for line in lines
        if _looks_like_markdown_table_row(line)
    ]
    if len(rows) < 2:
        return []

    column_counts = Counter(len(row) for row in rows if len(row) >= 2)
    if not column_counts:
        return []
    expected_columns, expected_frequency = max(
        column_counts.items(), key=lambda item: (item[1], item[0])
    )
    if expected_frequency < 2:
        return []

    normalized_rows: list[list[str]] = []
    mismatched_rows = 0
    for row in rows:
        if len(row) == expected_columns:
            normalized_rows.append(row)
            continue
        if abs(len(row) - expected_columns) <= 1 and expected_columns >= 3:
            normalized_rows.append((row + [""] * expected_columns)[:expected_columns])
            mismatched_rows += 1
            continue
        mismatched_rows += 1

    if len(normalized_rows) < 2:
        return []
    if mismatched_rows > max(1, len(rows) // 2):
        return []
    return normalized_rows


def _looks_like_markdown_table_row(line: str) -> bool:
    text = str(line or "").strip()
    return text.startswith("|") and len(_parse_markdown_cells(text)) >= 2


def _parse_markdown_cells(line: str) -> list[str]:
    text = str(line or "").strip()
    if not text:
        return []
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    placeholder = "__ESCAPED_PIPE__"
    return [
        cell.replace(placeholder, "|").strip()
        for cell in text.replace("\\|", placeholder).split("|")
    ]


def _is_markdown_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(
        re.fullmatch(r":?-{2,}:?", str(cell or "").strip()) for cell in cells
    )


def _display_requirement_text(item: dict[str, Any]) -> str:
    return str(
        item.get("raw_text")
        or item.get("display_text")
        or item.get("requirement_text")
        or ""
    ).strip()


def _ai_decision_label(item: dict[str, Any]) -> str:
    """Resolve the AI assessment label for a requirement row."""
    reference_status = (
        str(
            item.get("assessment_reference_status")
            or item.get("reference_status")
            or "CURRENT"
        )
        .strip()
        .upper()
    )
    if reference_status == "STALE_REFERENCE":
        return "STALE REFERENCE — RE-RUN REQUIRED"
    analysis_label = str(item.get("analysis_label") or "").strip().upper()
    if analysis_label == "INFO":
        return "INFO"
    assessment_mode = str(item.get("assessment_mode") or "").strip().lower()
    if assessment_mode in {"screening_failed", "analysis_failed"}:
        return assessment_mode.upper()
    ai_decision = str(item.get("ai_decision") or "").strip().upper()
    if ai_decision and ai_decision != "REQUIREMENT":
        return ai_decision
    if ai_decision == "REQUIREMENT":
        return "REQUIREMENT" if str(item.get("analysis_basis") or "").strip() else ""
    return ""


def _excel_column_width_to_pixels(width: float) -> int:
    return int(width * 7 + 5)


def _pixels_to_excel_column_width(pixels: int) -> float:
    return round(max(1, pixels - 5) / 7.0, 2)


def _default_requirement_area_width() -> float:
    return _CUSTOMER_REQUIREMENT_COLUMN_WIDTH


def _pixels_to_row_height_points(pixels: int) -> float:
    return round(min(_MAX_ROW_HEIGHT_PX, max(20, pixels)) * 0.75, 2)


def _table_rows_to_text(rows: list[list[str]], *, include_header: bool = True) -> str:
    if not rows:
        return ""
    if include_header:
        header = "| " + " | ".join(str(cell or "") for cell in rows[0]) + " |"
        separator = "| " + " | ".join("---" for _ in rows[0]) + " |"
        body = [
            "| " + " | ".join(str(cell or "") for cell in row) + " |"
            for row in rows[1:]
        ]
        return "\n".join([header, separator, *body]).strip()
    return "\n".join(
        "| " + " | ".join(str(cell or "") for cell in row) + " |" for row in rows
    ).strip()


def _render_table_image(
    rows: list[list[str]],
    *,
    max_width_px: int,
    header_row_count: int = 1,
) -> tuple[BytesIO, int, int]:
    normalized_rows = _normalize_table_rows(rows)
    font = load_table_font(_TABLE_FONT_SIZE)
    probe = ImageDraw.Draw(PILImage.new("RGB", (1, 1), "white"))
    column_units = _fit_table_column_units(normalized_rows, max_width_px=max_width_px)
    column_widths = [
        units * _TABLE_UNIT_PX + (_TABLE_CELL_PADDING_X * 2) for units in column_units
    ]

    wrapped_rows: list[list[str]] = []
    row_heights: list[int] = []
    for row in normalized_rows:
        wrapped_row: list[str] = []
        row_height = 0
        for col_index, value in enumerate(row):
            content = _wrap_text_to_units(
                str(value or ""), max(4, column_units[col_index] - 2)
            )
            wrapped_row.append(content)
            _, text_height = _measure_multiline_text(probe, content or " ", font)
            row_height = max(row_height, text_height + (_TABLE_CELL_PADDING_Y * 2))
        wrapped_rows.append(wrapped_row)
        row_heights.append(max(28, row_height))

    image_width = sum(column_widths) + 1
    image_height = sum(row_heights) + 1
    image = PILImage.new("RGB", (image_width, image_height), "white")
    draw = ImageDraw.Draw(image)

    y = 0
    for row_index, row in enumerate(wrapped_rows):
        x = 0
        fill_color = (
            _BRAND_HEADER_BACKGROUND
            if row_index < max(0, int(header_row_count or 0))
            else "#FFFFFF"
        )
        for col_index, content in enumerate(row):
            width = column_widths[col_index]
            height = row_heights[row_index]
            draw.rectangle(
                [x, y, x + width, y + height],
                fill=fill_color,
                outline=_BRAND_BORDER,
                width=1,
            )
            draw.multiline_text(
                (x + _TABLE_CELL_PADDING_X, y + _TABLE_CELL_PADDING_Y),
                content,
                fill=_BRAND_TEXT,
                font=font,
                spacing=_TABLE_LINE_SPACING,
            )
            x += width
        y += row_heights[row_index]

    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output, image_width, image_height


def _normalize_table_rows(rows: list[list[str]]) -> list[list[str]]:
    max_cols = max(len(row) for row in rows)
    normalized = [list(row) + [""] * (max_cols - len(row)) for row in rows]
    return [[str(cell or "") for cell in row] for row in normalized]


def _fit_table_column_units(rows: list[list[str]], *, max_width_px: int) -> list[int]:
    col_count = len(rows[0])
    desired_units = []
    for col_index in range(col_count):
        max_units = _TABLE_MIN_COLUMN_UNITS
        for row in rows:
            max_units = max(max_units, _display_width_units(str(row[col_index] or "")))
        desired_units.append(max_units + 2)

    total_budget_units = max(
        col_count * _TABLE_MIN_COLUMN_UNITS,
        max(
            1,
            (max_width_px - (col_count * _TABLE_CELL_PADDING_X * 2)) // _TABLE_UNIT_PX,
        ),
    )
    if sum(desired_units) <= total_budget_units:
        return desired_units

    scale = total_budget_units / sum(desired_units)
    widths = [
        max(_TABLE_MIN_COLUMN_UNITS, int(units * scale)) for units in desired_units
    ]
    while sum(widths) > total_budget_units:
        widest = max(range(len(widths)), key=widths.__getitem__)
        if widths[widest] <= _TABLE_MIN_COLUMN_UNITS:
            break
        widths[widest] -= 1
    while sum(widths) < total_budget_units:
        widest = max(
            range(len(widths)), key=lambda idx: desired_units[idx] - widths[idx]
        )
        widths[widest] += 1
    return widths


def _display_width_units(text: str) -> int:
    units = 0
    for ch in str(text or ""):
        units += 2 if ord(ch) > 127 else 1
    return max(units, 1)


def _wrap_text_to_units(text: str, max_units: int) -> str:
    lines = []
    for raw_line in str(text or "").splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        current: list[str] = []
        current_units = 0
        for ch in raw_line:
            ch_units = 2 if ord(ch) > 127 else 1
            if current and current_units + ch_units > max_units:
                lines.append("".join(current).rstrip())
                current = [ch]
                current_units = ch_units
                continue
            current.append(ch)
            current_units += ch_units
        lines.append("".join(current).rstrip())
    return "\n".join(lines)


def _measure_multiline_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont
) -> tuple[int, int]:
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=_TABLE_LINE_SPACING)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _display_parent_section_label(item: dict[str, Any]) -> str:
    return (
        _build_section_label(
            label=item.get("parent_section_label"),
            code=item.get("parent_section_code"),
            title=item.get("parent_section_title"),
        )
        or _build_section_label(
            label=item.get("section_label"),
            code=item.get("section_code"),
            title=item.get("section_title"),
        )
        or _section_label_from_anchor(item.get("source_anchor"))
        or str(item.get("heading_text") or "").strip()
    )


def _build_section_label(*, label: Any, code: Any, title: Any) -> str:
    direct_label = str(label or "").strip()
    if direct_label:
        return direct_label
    code_text = str(code or "").strip()
    title_text = str(title or "").strip()
    return " ".join(part for part in (code_text, title_text) if part)


def _section_label_from_anchor(anchor: Any) -> str:
    text = str(anchor or "")
    marker = "Section "
    index = text.lower().find(marker.lower())
    if index < 0:
        return ""
    fragment = text[index + len(marker) :]
    return fragment.split("|", 1)[0].strip()


def _build_table_export_catalog(
    requirements: list[dict[str, Any]],
) -> _TableExportCatalog:
    parts_by_logical_id: dict[str, list[_TableExportPart]] = {}
    relations: list[_TableExportRelation] = []
    first_seen_order: list[str] = []
    seen_relations: set[tuple[Any, ...]] = set()

    for item_index, item in enumerate(requirements):
        refs = [
            dict(ref) for ref in item.get("table_refs") or [] if isinstance(ref, dict)
        ]
        projection_payloads = _table_projection_payloads(item)
        if not refs:
            refs = _table_refs_from_lineage(projection_payloads)

        for ref_index, ref in enumerate(refs):
            logical_table_id = _logical_table_id(ref)
            if not logical_table_id:
                continue
            if logical_table_id not in parts_by_logical_id:
                parts_by_logical_id[logical_table_id] = []
                first_seen_order.append(logical_table_id)

            normalized_parts = _table_parts_from_ref(
                ref,
                logical_table_id=logical_table_id,
                default_source_order=(item_index * 1000) + ref_index,
            )
            parts_by_logical_id[logical_table_id].extend(normalized_parts)
            physical_table_ids = tuple(
                dict.fromkeys(
                    part.physical_table_id
                    for part in normalized_parts
                    if part.physical_table_id
                )
            ) or tuple(
                value
                for value in (
                    str(
                        ref.get("physical_table_id") or ref.get("table_id") or ""
                    ).strip(),
                )
                if value
            )
            projection_id, projection_mode = _projection_identity(
                item,
                ref,
                logical_table_id=logical_table_id,
                projection_payloads=projection_payloads,
            )
            record_id = _record_identity(item, ref)
            page_start = _coerce_optional_int(
                ref.get("source_page") or ref.get("page_start") or ref.get("page")
            )
            page_end = (
                _coerce_optional_int(ref.get("source_page_end") or ref.get("page_end"))
                or page_start
            )
            relation = _TableExportRelation(
                logical_table_id=logical_table_id,
                physical_table_ids=physical_table_ids,
                projection_id=projection_id,
                projection_mode=projection_mode,
                record_id=record_id,
                requirement_code=str(
                    item.get("display_req_code")
                    or item.get("req_code")
                    or item.get("requirement_entry_id")
                    or ""
                ).strip(),
                atomic_requirement_id=str(
                    item.get("atomic_requirement_id") or item.get("atom_id") or ""
                ).strip(),
                page_start=page_start,
                page_end=page_end,
            )
            relation_key = (
                relation.logical_table_id,
                relation.physical_table_ids,
                relation.projection_id,
                relation.projection_mode,
                relation.record_id,
                relation.requirement_code,
                relation.atomic_requirement_id,
                relation.page_start,
                relation.page_end,
            )
            if relation_key not in seen_relations:
                seen_relations.add(relation_key)
                relations.append(relation)

    for logical_table_id, parts in tuple(parts_by_logical_id.items()):
        parts_by_logical_id[logical_table_id] = _coalesce_table_parts(parts)
    return _TableExportCatalog(
        parts_by_logical_id=parts_by_logical_id,
        relations=relations,
        first_seen_order=first_seen_order,
    )


def _table_projection_payloads(item: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for key in ("table_record_projection", "table_projection"):
        value = item.get(key)
        if isinstance(value, dict):
            payloads.append(value)
    for key in ("table_record_projections", "table_projections"):
        value = item.get(key)
        if isinstance(value, list):
            payloads.extend(dict(entry) for entry in value if isinstance(entry, dict))
    evidence = item.get("table_evidence") or item.get("table_evidence_binding")
    if isinstance(evidence, dict):
        payloads.append(evidence)
    return payloads


def _table_refs_from_lineage(
    projection_payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for payload in projection_payloads:
        table_fact = payload.get("table_fact")
        fact = table_fact if isinstance(table_fact, dict) else payload
        logical_table_id = str(
            fact.get("logical_table_id") or payload.get("logical_table_id") or ""
        ).strip()
        if not logical_table_id:
            continue
        parts = fact.get("table_parts") or payload.get("table_parts") or []
        normalized_parts: list[dict[str, Any]] = []
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            source_refs = part.get("source_refs") or []
            page_numbers = [
                _coerce_optional_int(ref.get("page_number") or ref.get("page"))
                for ref in source_refs
                if isinstance(ref, dict)
            ]
            pages = [page for page in page_numbers if page is not None]
            normalized_parts.append(
                {
                    **part,
                    "physical_table_id": str(part.get("table_id") or "").strip(),
                    "source_page": min(pages) if pages else None,
                    "source_page_end": max(pages) if pages else None,
                }
            )
        if not normalized_parts:
            source_table_ids = (
                payload.get("source_table_ids") or fact.get("source_table_ids") or []
            )
            normalized_parts = [
                {
                    "table_id": str(table_id).strip(),
                    "physical_table_id": str(table_id).strip(),
                    "source_order": index,
                }
                for index, table_id in enumerate(source_table_ids)
                if str(table_id).strip()
            ]
        refs.append(
            {
                "logical_table_id": logical_table_id,
                "table_id": logical_table_id,
                "projection_id": payload.get("projection_id"),
                "projection_mode": payload.get("mode"),
                "record_id": payload.get("record_id"),
                "parts": normalized_parts,
            }
        )
    return refs


def _logical_table_id(ref: dict[str, Any]) -> str:
    return str(
        ref.get("logical_table_id")
        or ref.get("continuation_logical_table_id")
        or ref.get("complex_zone_id")
        or ref.get("table_id")
        or ref.get("id")
        or ""
    ).strip()


def _projection_identity(
    item: dict[str, Any],
    ref: dict[str, Any],
    *,
    logical_table_id: str,
    projection_payloads: list[dict[str, Any]],
) -> tuple[str, str]:
    projection_id = str(
        ref.get("projection_id")
        or item.get("table_projection_id")
        or item.get("projection_id")
        or ""
    ).strip()
    projection_mode = str(
        ref.get("projection_mode") or item.get("table_projection_mode") or ""
    ).strip()
    if projection_id and projection_mode:
        return projection_id, projection_mode
    for payload in projection_payloads:
        table_fact = payload.get("table_fact")
        fact = table_fact if isinstance(table_fact, dict) else payload
        candidate_logical_id = str(
            fact.get("logical_table_id") or payload.get("logical_table_id") or ""
        ).strip()
        if candidate_logical_id != logical_table_id:
            continue
        projection_id = projection_id or str(payload.get("projection_id") or "").strip()
        projection_mode = projection_mode or str(payload.get("mode") or "").strip()
        break
    return projection_id, projection_mode


def _record_identity(item: dict[str, Any], ref: dict[str, Any]) -> str:
    direct = str(
        ref.get("record_id")
        or ref.get("table_record_id")
        or item.get("table_record_id")
        or item.get("record_id")
        or ""
    ).strip()
    if direct:
        return direct
    evidence = item.get("table_evidence") or item.get("table_evidence_binding")
    if isinstance(evidence, dict):
        evidence_record_id = str(evidence.get("record_id") or "").strip()
        evidence_logical_id = str(evidence.get("logical_table_id") or "").strip()
        if evidence_record_id and (
            not evidence_logical_id or evidence_logical_id == _logical_table_id(ref)
        ):
            return evidence_record_id
    row_ids = [
        str(value).strip() for value in ref.get("row_ids") or [] if str(value).strip()
    ]
    if row_ids:
        return f"{_logical_table_id(ref)}:{','.join(row_ids)}"
    row_range = ref.get("row_range")
    if isinstance(row_range, (list, tuple)) and row_range:
        return f"{_logical_table_id(ref)}:rows:{'-'.join(str(value) for value in row_range)}"
    return ""


def _table_parts_from_ref(
    ref: dict[str, Any],
    *,
    logical_table_id: str,
    default_source_order: int,
) -> list[_TableExportPart]:
    explicit_parts = (
        ref.get("parts") or ref.get("table_parts") or ref.get("source_tables")
    )
    if isinstance(explicit_parts, list) and explicit_parts:
        parts: list[_TableExportPart] = []
        for part_index, part in enumerate(explicit_parts):
            if not isinstance(part, dict):
                continue
            inherited = {
                key: value
                for key, value in ref.items()
                if key
                in {
                    "table_title",
                    "logical_table_id",
                    "projection_id",
                    "projection_mode",
                }
            }
            normalized = _normalize_table_part(
                {**inherited, **part},
                logical_table_id=logical_table_id,
                default_source_order=default_source_order + part_index,
            )
            if _table_part_has_renderable_evidence(normalized):
                parts.append(normalized)
        if parts:
            return parts
    normalized = _normalize_table_part(
        ref,
        logical_table_id=logical_table_id,
        default_source_order=default_source_order,
    )
    return [normalized] if _table_part_has_renderable_evidence(normalized) else []


def _canonical_table_presentation(payload: dict[str, Any]) -> dict[str, Any]:
    """Use the same published column mapping as the native workbench.

    The frozen source cells remain in the Matrix artifact; this is only the
    workbook view, with every merged cell retaining its original source IDs.
    """
    projection = payload.get("canonical_column_projection") or {}
    if projection.get("status") != "EXACT":
        return payload
    cells = payload.get("cells") or []
    mappings = {item["cell_id"]: item for item in projection["cell_mappings"]}
    if set(mappings) != {cell.get("cell_id") or cell.get("id") for cell in cells}:
        raise ValueError("Canonical table presentation is missing original cells")
    groups: dict[tuple, dict] = {}
    for cell in sorted(cells, key=lambda item: (item["row"], item["column"])):
        cell_id = cell.get("cell_id") or cell.get("id")
        mapping = mappings[cell_id]
        key = (cell["row"], mapping["column"], cell.get("row_span", 1), mapping["column_span"])
        if key not in groups:
            groups[key] = {**cell, "column": mapping["column"], "column_span": mapping["column_span"],
                           "source_cell_ids": [cell_id]}
        else:
            target = groups[key]
            target["source_cell_ids"].append(cell_id)
            if str(cell.get("text") or "").strip():
                target["text"] = "\n".join(value for value in (target.get("text"), cell["text"]) if value)
            if cell.get("bbox") and target.get("bbox"):
                a, b = target["bbox"], cell["bbox"]
                target["bbox"] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
    normalized = list(groups.values())
    images = [{**image, "column": mappings[image["cell_id"]]["column"]}
              for image in payload.get("cell_images", [])]
    return {**payload, "cells": normalized, "column_count": projection["column_count"],
            "cell_images": images, "merged_regions": [
                {"row": c["row"], "column": c["column"], "row_span": c.get("row_span", 1), "column_span": c.get("column_span", 1)}
                for c in normalized if c.get("row_span", 1) > 1 or c.get("column_span", 1) > 1]}


def _normalize_table_part(
    payload: dict[str, Any],
    *,
    logical_table_id: str,
    default_source_order: int,
) -> _TableExportPart:
    payload = _canonical_table_presentation(payload)
    table_payload = payload.get("table_payload") or payload.get("table")
    rich = table_payload if isinstance(table_payload, dict) else payload
    headers = _string_list(
        payload.get("inherited_headers")
        or payload.get("headers")
        or rich.get("inherited_headers")
        or rich.get("headers")
        or []
    )
    rows = _string_rows(
        payload.get("slice_rows")
        or payload.get("rows")
        or rich.get("slice_rows")
        or rich.get("rows")
        or []
    )
    cells = [
        dict(cell)
        for cell in rich.get("cells") or payload.get("cells") or []
        if isinstance(cell, dict)
    ]
    merged_regions = [
        _normalize_merged_region(region)
        for region in (
            rich.get("merged_regions") or payload.get("merged_regions") or []
        )
        if isinstance(region, dict)
    ]
    merged_regions = [region for region in merged_regions if region is not None]
    notes = _string_list(
        payload.get("inherited_notes")
        or payload.get("notes")
        or rich.get("notes")
        or []
    )
    cell_images = [
        dict(image)
        for image in (
            payload.get("cell_images")
            or rich.get("cell_images")
            or payload.get("images")
            or rich.get("images")
            or []
        )
        if isinstance(image, dict)
    ]
    for cell in cells:
        for image_value in cell.get("images") or []:
            if isinstance(image_value, dict):
                cell_images.append(
                    {
                        **image_value,
                        "row": cell.get("row"),
                        "column": cell.get("column"),
                        "cell_id": cell.get("cell_id") or cell.get("id"),
                    }
                )
        image_value = (
            cell.get("image") or cell.get("visual") or cell.get("visual_asset")
        )
        if isinstance(image_value, dict):
            cell_images.append(
                {
                    **image_value,
                    "row": cell.get("row"),
                    "column": cell.get("column"),
                    "cell_id": cell.get("cell_id") or cell.get("id"),
                }
            )
    source_snapshot_path = str(payload.get("source_snapshot_path") or "").strip()
    fallback_snapshot_path = str(
        payload.get("slice_snapshot_path") or payload.get("snapshot_path") or ""
    ).strip()
    snapshot_paths = (
        [source_snapshot_path or fallback_snapshot_path]
        if source_snapshot_path or fallback_snapshot_path
        else []
    )
    row_range = payload.get("row_range")
    row_start = None
    if isinstance(row_range, (list, tuple)) and row_range:
        row_start = _coerce_optional_int(row_range[0])
    page_start = _coerce_optional_int(
        payload.get("source_page") or payload.get("page_start") or payload.get("page")
    )
    page_end = (
        _coerce_optional_int(payload.get("source_page_end") or payload.get("page_end"))
        or page_start
    )
    return _TableExportPart(
        logical_table_id=logical_table_id,
        physical_table_id=str(
            payload.get("physical_table_id")
            or payload.get("source_table_id")
            or payload.get("table_id")
            or payload.get("id")
            or rich.get("table_id")
            or rich.get("id")
            or logical_table_id
        ).strip(),
        title=str(
            payload.get("table_title")
            or payload.get("title")
            or rich.get("table_title")
            or rich.get("title")
            or ""
        ).strip(),
        source_order=_coerce_int_or_default(
            payload.get("source_order"),
            default_source_order,
        ),
        page_start=page_start,
        page_end=page_end,
        headers=headers,
        rows=rows,
        row_start=row_start,
        cells=cells,
        merged_regions=merged_regions,
        notes=notes,
        cell_images=cell_images,
        snapshot_paths=snapshot_paths,
        raw_ref=dict(payload),
    )


def _table_part_has_renderable_evidence(part: _TableExportPart) -> bool:
    if (
        part.cells
        or part.headers
        or part.rows
        or part.cell_images
        or part.snapshot_paths
    ):
        return True
    location = part.raw_ref.get("location") or part.raw_ref.get("source_location")
    if not isinstance(location, dict):
        return False
    bbox = location.get("bbox")
    return (
        str(location.get("format") or "").strip().lower() == "pdf"
        and (_coerce_optional_int(location.get("page_number")) or 0) >= 1
        and isinstance(bbox, (list, tuple))
        and len(bbox) == 4
    )


def _normalize_merged_region(region: dict[str, Any]) -> dict[str, int] | None:
    row = _coerce_optional_int(region.get("row"))
    column = _coerce_optional_int(region.get("column"))
    if row is None or column is None:
        return None
    return {
        "row": max(0, row),
        "column": max(0, column),
        "row_span": max(1, _coerce_optional_int(region.get("row_span")) or 1),
        "column_span": max(
            1,
            _coerce_optional_int(region.get("column_span")) or 1,
        ),
    }


def _coalesce_table_parts(parts: list[_TableExportPart]) -> list[_TableExportPart]:
    by_part: dict[str, list[_TableExportPart]] = {}
    order: list[str] = []
    for part in parts:
        key = part.physical_table_id
        if key not in by_part:
            by_part[key] = []
            order.append(key)
        by_part[key].append(part)

    combined: list[_TableExportPart] = []
    for key in order:
        fragments = by_part[key]
        first = min(
            fragments,
            key=lambda item: (item.source_order, item.physical_table_id),
        )
        richest = max(
            fragments,
            key=lambda item: (
                len(item.cells),
                sum(len(row) for row in item.rows),
                len(item.rows),
                len(item.headers),
                len(item.cell_images),
                -item.source_order,
            ),
        )
        header_source = max(
            fragments,
            key=lambda item: (len(item.headers), -item.source_order),
        )
        headers = list(header_source.headers)
        cells: list[dict[str, Any]] = []
        seen_cells: set[tuple[Any, ...]] = set()
        for fragment in [
            richest,
            *sorted(fragments, key=lambda item: item.source_order),
        ]:
            for cell in fragment.cells:
                cell_id = str(cell.get("id") or cell.get("cell_id") or "").strip()
                identity: tuple[Any, ...] = (
                    ("id", cell_id)
                    if cell_id
                    else (
                        "position",
                        _coerce_optional_int(cell.get("row")),
                        _coerce_optional_int(cell.get("column")),
                        _coerce_optional_int(cell.get("row_span")) or 1,
                        _coerce_optional_int(cell.get("column_span")) or 1,
                    )
                )
                if identity in seen_cells:
                    continue
                seen_cells.add(identity)
                cells.append(dict(cell))
        merged_regions = _dedupe_dicts(
            region for item in fragments for region in item.merged_regions
        )
        notes = list(
            dict.fromkeys(note for item in fragments for note in item.notes if note)
        )
        cell_images = _dedupe_dicts(
            image for item in fragments for image in item.cell_images
        )
        snapshot_paths = list(
            dict.fromkeys(
                path for item in fragments for path in item.snapshot_paths if path
            )
        )
        indexed_rows: dict[int, list[str]] = {}
        for fragment in sorted(fragments, key=lambda item: item.source_order):
            if fragment.row_start is not None:
                for offset, row in enumerate(fragment.rows):
                    row_index = fragment.row_start + offset
                    current = indexed_rows.get(row_index)
                    if current is None or len([value for value in row if value]) > len(
                        [value for value in current if value]
                    ):
                        indexed_rows[row_index] = list(row)
        row_source = max(
            fragments,
            key=lambda item: (
                len(item.rows),
                sum(len(row) for row in item.rows),
                -item.source_order,
            ),
        )
        # Equal text at different source positions is legitimate source data.
        # Use one authoritative unindexed segment intact instead of text-deduping
        # rows collected through several wrapper references.
        rows = (
            [indexed_rows[index] for index in sorted(indexed_rows)]
            if indexed_rows
            else [list(row) for row in row_source.rows]
        )
        pages = [
            page
            for fragment in fragments
            for page in (fragment.page_start, fragment.page_end)
            if page is not None
        ]
        combined.append(
            _TableExportPart(
                logical_table_id=first.logical_table_id,
                physical_table_id=first.physical_table_id,
                title=richest.title
                or next((item.title for item in fragments if item.title), ""),
                source_order=min(item.source_order for item in fragments),
                page_start=min(pages) if pages else first.page_start,
                page_end=max(pages) if pages else first.page_end,
                headers=list(headers),
                rows=rows,
                row_start=min(indexed_rows) if indexed_rows else None,
                cells=list(cells),
                merged_regions=merged_regions,
                notes=notes,
                cell_images=cell_images,
                snapshot_paths=snapshot_paths,
                raw_ref=richest.raw_ref,
            )
        )
    return sorted(
        combined,
        key=lambda item: (
            item.source_order,
            item.page_start if item.page_start is not None else 10**9,
            item.physical_table_id,
        ),
    )


def _dedupe_dicts(values) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        signature = repr(sorted(value.items(), key=lambda item: item[0]))
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(dict(value))
    return deduped


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    output: list[str] = []
    for item in value:
        if isinstance(item, dict):
            rendered = str(
                item.get("text")
                or item.get("label")
                or item.get("value")
                or item.get("note")
                or ""
            ).strip()
        else:
            rendered = str(item or "").strip()
        if rendered:
            output.append(rendered)
    return output


def _string_rows(value: Any) -> list[list[str]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        [str(cell or "") for cell in row]
        for row in value
        if isinstance(row, (list, tuple))
    ]


def _coerce_optional_int(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_int_or_default(value: Any, default: int) -> int:
    parsed = _coerce_optional_int(value)
    return parsed if parsed is not None else default


def _append_matrix_logical_table_sheet(
    *,
    workbook: xlsxwriter.Workbook,
    formats: _WorkbookFormats,
    snapshot: dict[str, Any],
    logical_table_id: str,
    sheet_name: str,
    parts: list[_TableExportPart],
    detail: dict[str, Any],
    nodes: list[dict[str, Any]],
    matrix_row_by_node: dict[str, int],
    source_image_context: _ExportSourceImageContext | None,
    previous_sheet_name: str,
    next_sheet_name: str,
) -> tuple[dict[str, int], dict[str, Any]]:
    """Render one logical table as a source-faithful, analysis-free worksheet.

    Excel applies one width to a whole column, so appending a six-column
    analysis grid below an irregular source table inevitably damages the source
    geometry.  Analysis lives in ``ComplianceMatrix``; this sheet keeps only
    original cells, merges, notes and source-bound images.
    """

    del snapshot

    sheet = workbook.add_worksheet(sheet_name)
    sheet.hide_gridlines(2)
    sheet.freeze_panes(3, 0)
    sheet.set_zoom(90)
    sheet.set_tab_color("#6A9C7A")
    last_col = _matrix_table_last_column(parts)
    title_format = workbook.add_format(
        {
            "bold": True,
            "font_name": _workbook_font_name(),
            "font_size": 14,
            "font_color": _BRAND_HEADER_TEXT,
            "bg_color": _BRAND_SECONDARY,
            "text_wrap": True,
            "valign": "vcenter",
        }
    )
    section_format = workbook.add_format(
        {
            "bold": True,
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_TEXT,
            "bg_color": _BRAND_HEADER_BACKGROUND,
            "bottom": 1,
            "bottom_color": _BRAND_BORDER,
            "text_wrap": True,
            "valign": "vcenter",
        }
    )
    cell_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_TEXT,
            "border": 1,
            "border_color": _BRAND_BORDER,
            "text_wrap": True,
            "valign": "top",
        }
    )
    header_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_TEXT,
            "bold": True,
            "bg_color": _BRAND_HEADER_BACKGROUND,
            "border": 1,
            "border_color": _BRAND_BORDER,
            "text_wrap": True,
            "valign": "top",
        }
    )
    note_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_TEXT_SECONDARY,
            "italic": True,
            "text_wrap": True,
            "valign": "top",
        }
    )
    return_link_format = workbook.add_format(
        {
            "font_name": _workbook_font_name(),
            "font_size": _WORKBOOK_FONT_SIZE,
            "font_color": _BRAND_SECONDARY,
            "underline": True,
            "bold": True,
            "bg_color": "#E5F3E9",
            "bottom": 1,
            "bottom_color": _BRAND_BORDER,
            "align": "center",
            "valign": "vcenter",
        }
    )
    native_column_widths = _apply_matrix_table_column_widths(sheet, parts)
    owner_row = matrix_row_by_node.get(str(detail.get("owner_node_id") or ""), 1)
    sheet.write_url(
        0,
        0,
        f"internal:'ComplianceMatrix'!A{owner_row}",
        return_link_format,
        string="← 返回对应条目",
    )
    if previous_sheet_name:
        sheet.write_url(
            0,
            1,
            f"internal:'{previous_sheet_name}'!A1",
            return_link_format,
            string="← 上一张表",
        )
    else:
        sheet.write(0, 1, "第一张表", return_link_format)
    if next_sheet_name:
        sheet.write_url(
            0,
            2,
            f"internal:'{next_sheet_name}'!A1",
            return_link_format,
            string="下一张表 →",
        )
    else:
        sheet.write(0, 2, "最后一张表", return_link_format)
    title = str(
        detail.get("title")
        or detail.get("owner_label")
        or detail.get("section_code")
        or "原表"
    ).strip()
    title_presentation = (detail.get("projection") or {}).get("title_presentation") or {}
    agent_table_label = title_presentation.get("authority") == "AGENT_DISPLAY_LABEL"
    title = title if agent_table_label else title[:240]
    sheet.merge_range(1, 0, 1, last_col, title, title_format)
    title_width_px = sum(
        _excel_column_width_to_pixels(width) for width in native_column_widths
    )
    sheet.set_row(
        1,
        max(
            30.0,
            _estimate_total_text_height(
                title,
                title_width_px,
                cap_to_excel_row=True,
            ),
        ),
    )
    sheet.set_row(0, 24.0)
    sheet.merge_range(
        2,
        0,
        2,
        last_col,
        ("显示名（AI） · " if agent_table_label else "") + "原表保真视图 · 分析状态与结论请返回 ComplianceMatrix",
        section_format,
    )
    if agent_table_label:
        sheet.set_row(2, max(30.0, _estimate_total_text_height(
            "显示名（AI） · 原表保真视图 · 分析状态与结论请返回 ComplianceMatrix", title_width_px, cap_to_excel_row=True)))

    cursor = 3
    fallbacks = 0
    warnings: list[str] = []
    part_start_rows: dict[str, int] = {}
    image_stats = {
        "referenced": sum(len(part.cell_images) for part in parts),
        "inserted": 0,
        "failed": 0,
    }
    if not parts:
        sheet.merge_range(
            cursor,
            0,
            cursor,
            last_col,
            "冻结版本中没有可还原的结构化表格证据。",
            formats.meta_value,
        )
        warnings.append(f"{logical_table_id}: structured table evidence is unavailable")
        cursor += 1
    for part_index, part in enumerate(
        sorted(parts, key=lambda value: (value.source_order, value.physical_table_id)),
        start=1,
    ):
        page_label = (
            ""
            if part.page_start is None
            else f"第 {part.page_start} 页"
            if part.page_end in {None, part.page_start}
            else f"第 {part.page_start}–{part.page_end} 页"
        )
        if not page_label and part.raw_ref.get('native_locator'):
            from reqpilotx.application.agent_workflow.source_locations import source_location_label
            page_label = source_location_label(part.raw_ref['native_locator'])
        part_label = " · ".join(
            value
            for value in (
                f"原表片段 {part_index}" if len(parts) > 1 else "原表",
                page_label,
                part.title,
            )
            if value
        )
        sheet.merge_range(cursor, 0, cursor, last_col, part_label, section_format)
        sheet.set_row(
            cursor,
            max(
                24.0,
                _estimate_total_text_height(
                    part_label,
                    title_width_px,
                    cap_to_excel_row=True,
                ),
            ),
        )
        cursor += 1
        part_start_rows[part.physical_table_id] = cursor
        if part.cells:
            cursor = _write_rich_table_part(
                sheet=sheet,
                start_row=cursor,
                part=part,
                cell_format=cell_format,
                header_format=header_format,
                source_image_context=source_image_context,
                image_stats=image_stats,
                warnings=warnings,
                native_column_widths=native_column_widths,
            )
        elif part.headers or part.rows:
            cursor = _write_simple_table_part(
                sheet=sheet,
                start_row=cursor,
                part=part,
                cell_format=cell_format,
                header_format=header_format,
                native_column_widths=native_column_widths,
            )
        else:
            before = cursor
            cursor = _write_original_snapshot_fallbacks(
                sheet=sheet,
                cursor=cursor,
                snapshot_paths=part.snapshot_paths,
                label_format=section_format,
                source_ref=part.raw_ref,
                source_image_context=source_image_context,
                last_col=last_col,
                max_width_px=title_width_px,
            )
            if cursor > before:
                fallbacks += 1
            else:
                sheet.merge_range(
                    cursor,
                    0,
                    cursor,
                    last_col,
                    "结构化单元格与可验证的原表图像均不可用。",
                    formats.meta_value,
                )
                warnings.append(
                    f"{logical_table_id}: neither structured cells nor a verified source image is available"
                )
                cursor += 1
        note_width_px = title_width_px
        for note in part.notes:
            for prepared in _prepare_text_rows_for_height(
                f"表注：{note}",
                note_width_px,
                max_height_points=_MATRIX_MAX_VISIBLE_ROW_HEIGHT_POINTS,
            ):
                sheet.merge_range(
                    cursor,
                    0,
                    cursor,
                    last_col,
                    prepared.text,
                    note_format,
                )
                sheet.set_row(cursor, prepared.row_height)
                cursor += 1
        # A structured table is already the authoritative visual restoration.
        # Never append a second whole-table screenshot underneath it.
        if part_index < len(parts):
            cursor += 1

    record_rows = _matrix_table_record_source_rows(
        logical_table_id=logical_table_id,
        detail=detail,
        nodes=nodes,
        parts=parts,
        part_start_rows=part_start_rows,
    )
    native_width = sum(native_column_widths)
    if native_width <= 75:
        sheet.set_portrait()
        sheet.set_paper(9)
        sheet.set_print_scale(100)
    elif native_width <= 150:
        sheet.set_landscape()
        sheet.set_paper(9)
        sheet.set_print_scale(95)
    else:
        sheet.set_landscape()
        sheet.set_paper(8)
        sheet.set_print_scale(85)
    sheet.repeat_rows(0, 2)
    sheet.print_area(0, 0, max(2, cursor - 1), last_col)
    sheet.set_margins(0.3, 0.3, 0.45, 0.45)
    sheet.set_header(
        f"&LReqPilotX&C{sheet_name} 原表&R第 &P / &N 页",
        {"margin": 0.2},
    )
    sheet.set_footer(
        "&L原表保真视图 · 分析见 ComplianceMatrix&R&P / &N", {"margin": 0.2}
    )
    sheet.set_selection(3, 0, 3, 0)
    return record_rows, {
        "cell_images": image_stats["referenced"],
        "cell_images_inserted": image_stats["inserted"],
        "cell_images_failed": image_stats["failed"],
        "fallbacks": fallbacks,
        "warnings": warnings,
    }


def _matrix_table_record_source_rows(
    *,
    logical_table_id: str,
    detail: dict[str, Any],
    nodes: list[dict[str, Any]],
    parts: list[_TableExportPart],
    part_start_rows: dict[str, int],
) -> dict[str, int]:
    """Map record/atom links to their native source row on the T worksheet."""

    part_by_id = {part.physical_table_id: part for part in parts}
    fallback_row = min(part_start_rows.values(), default=0) + 1
    output: dict[str, int] = {}
    for group in _matrix_table_analysis_groups(
        logical_table_id=logical_table_id,
        detail=detail,
        nodes=nodes,
    ):
        target_row = fallback_row
        candidates: list[dict[str, Any]] = []
        record_detail = group.get("record_detail") or {}
        record_node = group.get("record_node") or {}
        if isinstance(record_detail, dict):
            candidates.append(record_detail)
            nested = record_detail.get("record")
            if isinstance(nested, dict):
                candidates.append(nested)
        if isinstance(record_node, dict):
            candidates.append(record_node)
        bindings = next(
            (
                candidate.get("row_bindings")
                for candidate in candidates
                if isinstance(candidate.get("row_bindings"), list)
                and candidate.get("row_bindings")
            ),
            [],
        )
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            table_id = str(
                binding.get("physical_table_id") or binding.get("table_id") or ""
            ).strip()
            start_row = part_start_rows.get(table_id)
            row_index = _coerce_optional_int(
                binding.get("row_index")
                if binding.get("row_index") is not None
                else binding.get("row")
            )
            if start_row is None or row_index is None:
                continue
            part = part_by_id.get(table_id)
            if part is not None and not part.cells and part.row_start is not None:
                row_index = max(0, row_index - part.row_start)
            target_row = start_row + max(0, row_index) + 1
            break

        identities = {
            str(group.get("record_id") or "").strip(),
            str(record_node.get("node_id") or "").strip(),
        }
        identities.update(
            str(atom.get("node_id") or "").strip()
            for atom in group.get("atoms") or []
            if isinstance(atom, dict)
        )
        for identity in identities:
            if identity:
                output[identity] = target_row
    return output


def _write_merged_analysis_value(
    *,
    sheet,
    start_row: int,
    end_row: int,
    column: int,
    value: str,
    cell_format: Any,
) -> None:
    if end_row > start_row:
        sheet.merge_range(start_row, column, end_row, column, value, cell_format)
    else:
        sheet.write(start_row, column, value, cell_format)


def _apply_matrix_table_column_widths(
    sheet,
    parts: Iterable[_TableExportPart],
) -> tuple[float, ...]:
    table_parts = list(parts)
    last_col = _matrix_table_last_column(table_parts)
    widths = {
        column: width
        for column, width in enumerate(_matrix_table_base_widths(last_col))
    }
    if any(_table_part_uses_image_fallback(part) for part in table_parts):
        for column in range(last_col + 1):
            widths[column] = max(widths.get(column, 0), 20.0)
    for part in table_parts:
        for cell in part.cells:
            column = _coerce_optional_int(cell.get("column"))
            if column is None or column < 0 or column >= 16_384:
                continue
            text = _table_cell_display_text(cell)
            span = max(1, _coerce_optional_int(cell.get("column_span")) or 1)
            proposed = max(
                10.0,
                min(34.0, (_display_width_units(text) + 4) / span),
            )
            for offset in range(span):
                target = column + offset
                if target <= last_col:
                    widths[target] = max(widths.get(target, 0), proposed)
        for row in ([part.headers] if part.headers else []) + part.rows:
            for column, text in enumerate(row):
                proposed = max(
                    10.0,
                    min(34.0, _display_width_units(str(text)) + 4),
                )
                widths[column] = max(widths.get(column, 0), proposed)
        for image in part.cell_images:
            column = _coerce_optional_int(image.get("column"))
            if column is not None and 0 <= column <= last_col:
                widths[column] = max(widths.get(column, 0), 22.0)
    for column, width in widths.items():
        sheet.set_column(column, column, width)
    return tuple(widths[column] for column in range(last_col + 1))


def _matrix_table_last_column(parts: Iterable[_TableExportPart]) -> int:
    # Three columns are the minimum needed for return/previous/next navigation.
    # No analysis-grid width is imposed on the native source table.
    last_column = 2
    for part in parts:
        if _table_part_uses_image_fallback(part):
            last_column = max(last_column, 7)
        for cell in part.cells:
            column = _coerce_optional_int(cell.get("column"))
            if column is None or column < 0:
                continue
            span = max(1, _coerce_optional_int(cell.get("column_span")) or 1)
            last_column = max(last_column, column + span - 1)
        for row in ([part.headers] if part.headers else []) + part.rows:
            last_column = max(last_column, len(row) - 1)
        for image in part.cell_images:
            column = _coerce_optional_int(image.get("column"))
            if column is not None and column >= 0:
                last_column = max(last_column, column)
    return min(last_column, 16_383)


def _table_part_uses_image_fallback(part: _TableExportPart) -> bool:
    """Reserve a real printable canvas when a source table is image-only."""

    if part.cells or part.headers or part.rows:
        return False
    if part.snapshot_paths:
        return True
    location = part.raw_ref.get("location") or part.raw_ref.get("source_location")
    if not isinstance(location, dict):
        return False
    bbox = location.get("bbox") or part.raw_ref.get("bbox")
    page = _coerce_optional_int(
        location.get("page_number")
        or part.raw_ref.get("page_number")
        or part.raw_ref.get("page_start")
    )
    return page is not None and isinstance(bbox, (list, tuple)) and len(bbox) == 4


def _matrix_table_base_widths(last_col: int) -> tuple[float, ...]:
    return tuple(14.0 for _ in range(max(0, last_col) + 1))


def _matrix_table_analysis_groups(
    *,
    logical_table_id: str,
    detail: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    projection = detail.get("projection")
    explicit_records = (
        projection.get("records") if isinstance(projection, dict) else []
    ) or []
    if isinstance(explicit_records, dict):
        explicit_records = list(explicit_records.values())
    for index, record in enumerate(
        explicit_records if isinstance(explicit_records, list) else []
    ):
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("record_id") or f"record-{index + 1}").strip()
        groups[record_id] = {
            "record_id": record_id,
            "record_detail": dict(record),
            "record_node": {},
            "atoms": [
                dict(atom)
                for atom in record.get("atomic_requirements")
                or record.get("atoms")
                or []
                if isinstance(atom, dict)
            ],
            "source_order": _coerce_int_or_default(record.get("source_order"), index),
            "label": record.get("label") or record.get("title") or "",
        }
        order.append(record_id)
    node_by_id = {
        str(node.get("node_id") or ""): node
        for node in nodes
        if str(node.get("node_id") or "")
    }
    table_nodes = [
        node
        for node in nodes
        if logical_table_id in _matrix_node_logical_table_ids(node)
    ]
    for node in table_nodes:
        record_id = str(node.get("table_record_id") or "").strip()
        if not record_id:
            if _matrix_analysis_eligible(node) and (
                node.get("requirement_id") or node.get("requirement_entry_id")
            ):
                whole_group = groups.get("__whole_table__")
                if whole_group is None:
                    whole_group = {
                        "record_id": "",
                        "record_detail": {},
                        "record_node": {},
                        "atoms": [],
                        "source_order": _coerce_int_or_default(
                            node.get("source_order"), len(order)
                        ),
                        "label": "整表分析",
                    }
                    groups["__whole_table__"] = whole_group
                    order.append("__whole_table__")
                whole_group["atoms"].append(node)
            continue
        group = groups.get(record_id)
        if group is None:
            group = {
                "record_id": record_id,
                "record_detail": {},
                "record_node": {},
                "atoms": [],
                "source_order": _coerce_int_or_default(
                    node.get("source_order"), len(order)
                ),
                "label": "",
            }
            groups[record_id] = group
            order.append(record_id)
        kind = str(node.get("display_role") or "").strip().upper()
        if kind == "TABLE_RECORD":
            group["record_node"] = node
            group["label"] = group.get("label") or _matrix_node_label(node)
        elif kind in {"REQUIREMENT", "ATOMIC_REQUIREMENT"} or _matrix_analysis_eligible(
            node
        ):
            group["atoms"].append(node)
    for record_id in order:
        group = groups[record_id]
        record_node = group.get("record_node") or {}
        child_ids = set(record_node.get("child_ids") or [])
        for child_id in child_ids:
            child = node_by_id.get(str(child_id))
            if child is None:
                continue
            kind = str(child.get("display_role") or "").strip().upper()
            if kind in {
                "REQUIREMENT",
                "ATOMIC_REQUIREMENT",
            } or _matrix_analysis_eligible(child):
                group["atoms"].append(child)
        if (
            record_node
            and _matrix_analysis_eligible(record_node)
            and not group["atoms"]
            and (
                record_node.get("requirement_id")
                or record_node.get("requirement_entry_id")
            )
        ):
            group["atoms"].append(record_node)
        seen_atoms: set[str] = set()
        unique_atoms: list[dict[str, Any]] = []
        for atom in group["atoms"]:
            identity = str(
                atom.get("node_id")
                or atom.get("requirement_id")
                or atom.get("requirement_entry_id")
                or id(atom)
            )
            if identity in seen_atoms:
                continue
            seen_atoms.add(identity)
            unique_atoms.append(atom)
        group["atoms"] = sorted(
            unique_atoms,
            key=lambda atom: _coerce_int_or_default(
                atom.get("source_order"), 2**31 - 1
            ),
        )
    return sorted(
        (groups[record_id] for record_id in order),
        key=lambda group: (
            _coerce_int_or_default(group.get("source_order"), 2**31 - 1),
            str(group.get("record_id") or ""),
        ),
    )


def _matrix_record_header_path(
    group: dict[str, Any],
    *,
    parts: Iterable[_TableExportPart] = (),
) -> str:
    table_parts = list(parts)
    candidates = [group.get("record_detail") or {}, group.get("record_node") or {}]
    for candidate in candidates:
        for key in ("header_path", "headers", "inherited_headers"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                texts = [_display_export_value(item) for item in value]
                texts = [text for text in texts if text]
                if texts:
                    return " › ".join(texts)
        nested = candidate.get("record")
        if isinstance(nested, dict):
            bindings = nested.get("header_bindings") or []
        else:
            bindings = candidate.get("header_bindings") or []
        if isinstance(bindings, list):
            ordered = _ordered_matrix_record_headers(bindings, table_parts)
            texts = [str(binding.get("text") or "").strip() for binding in ordered]
            texts = [text for text in texts if text]
            if texts:
                return " › ".join(texts)
        for ref in candidate.get("table_refs") or []:
            if not isinstance(ref, dict):
                continue
            headers = ref.get("inherited_headers") or ref.get("headers") or []
            texts = (
                [_display_export_value(item) for item in headers]
                if isinstance(headers, list)
                else []
            )
            texts = [text for text in texts if text]
            if texts:
                return " › ".join(texts)
    return ""


def _ordered_matrix_record_headers(
    bindings: list[Any],
    parts: list[_TableExportPart],
) -> list[dict[str, Any]]:
    """Project frozen header identities back into their native cell order."""

    positions: dict[str, tuple[int, int, int, int]] = {}
    for part_index, part in enumerate(
        sorted(parts, key=lambda value: (value.source_order, value.physical_table_id))
    ):
        for cell_index, cell in enumerate(part.cells):
            cell_id = str(cell.get("id") or cell.get("cell_id") or "").strip()
            if not cell_id:
                continue
            row = _coerce_optional_int(cell.get("row"))
            column = _coerce_optional_int(cell.get("column"))
            positions[cell_id] = (
                part_index,
                row if row is not None else 2**31 - 1,
                column if column is not None else 2**31 - 1,
                cell_index,
            )

    indexed = [
        (index, binding)
        for index, binding in enumerate(bindings)
        if isinstance(binding, dict)
    ]

    def order_key(
        item: tuple[int, dict[str, Any]],
    ) -> tuple[int, int, int, int, int]:
        original_index, binding = item
        cell_positions = [
            positions[str(cell_id)]
            for cell_id in binding.get("cell_ids") or []
            if str(cell_id) in positions
        ]
        if not cell_positions:
            return (2**31 - 1, 2**31 - 1, 2**31 - 1, original_index, original_index)
        return (*min(cell_positions), original_index)

    return [binding for _, binding in sorted(indexed, key=order_key)]


def _matrix_record_source_text(
    group: dict[str, Any],
    *,
    parts: Iterable[_TableExportPart] = (),
) -> str:
    table_parts = list(parts)
    candidates = [group.get("record_detail") or {}, group.get("record_node") or {}]
    shared_values: list[str] = []
    notes: list[str] = []
    seen_shared_identities: set[str] = set()
    seen_note_identities: set[str] = set()

    def append_values(
        target: list[str],
        values: Iterable[Any],
        *,
        seen_identities: set[str],
    ) -> None:
        for value in values:
            rendered = _display_record_value(value)
            if not rendered:
                continue
            identity = _source_fact_identity(value) if isinstance(value, dict) else ""
            if identity and identity in seen_identities:
                continue
            if identity:
                seen_identities.add(identity)
            target.append(rendered)

    for candidate in candidates:
        nested = candidate.get("record")
        record = nested if isinstance(nested, dict) else candidate
        base = _matrix_record_structured_text(record, parts=table_parts)
        for key in (
            "display_text",
            "original_record",
            "source_record",
            "record_text",
            "raw_text",
        ):
            if base:
                break
            value = candidate.get(key)
            rendered = _display_record_value(value)
            if rendered:
                base = rendered
                break
        if isinstance(nested, dict):
            if not base:
                base = _display_record_value(
                    nested.get("original_record")
                    or nested.get("values")
                    or nested.get("row_values")
                )
        append_values(
            shared_values,
            record.get("shared_merged_values") or [],
            seen_identities=seen_shared_identities,
        )
        append_values(
            notes,
            record.get("notes") or [],
            seen_identities=seen_note_identities,
        )
        if record is not candidate:
            append_values(
                shared_values,
                candidate.get("shared_merged_values") or [],
                seen_identities=seen_shared_identities,
            )
            append_values(
                notes,
                candidate.get("notes") or [],
                seen_identities=seen_note_identities,
            )
        for ref in candidate.get("table_refs") or []:
            if not isinstance(ref, dict):
                continue
            if not base:
                rows = ref.get("slice_rows") or ref.get("rows") or []
                if isinstance(rows, list) and rows:
                    base = "\n".join(_display_record_value(row) for row in rows if row)
        if base:
            additions: list[str] = []
            if shared_values:
                additions.append("共享值：" + "；".join(shared_values))
            if notes:
                additions.extend(f"表注：{note}" for note in notes)
            return "\n".join([base, *additions])
    if not (group.get("record_detail") or group.get("record_node")):
        return ""
    return str(group.get("label") or "").strip()


def _matrix_record_structured_text(
    record: dict[str, Any],
    *,
    parts: Iterable[_TableExportPart],
) -> str:
    """Render one native table record as readable ``header：value`` lines.

    The frozen record summary is intentionally excluded: it is a compact
    machine serialization and may contain pipe separators.  Human exports
    instead resolve the record's source cell identities back into the native
    table, preserving column meaning without flattening the original table.
    """

    table_parts = list(parts)
    cell_by_id: dict[str, dict[str, Any]] = {}
    cell_part_by_id: dict[str, int] = {}
    cells_by_part: dict[int, list[dict[str, Any]]] = {}
    fallback_headers: dict[int, dict[int, str]] = {}
    image_cell_ids: set[str] = set()
    for part_index, part in enumerate(table_parts):
        cells_by_part[part_index] = part.cells
        for column, header in enumerate(part.headers):
            if str(header or "").strip():
                fallback_headers.setdefault(part_index, {}).setdefault(
                    column,
                    str(header).strip(),
                )
        for cell in part.cells:
            cell_id = str(cell.get("id") or cell.get("cell_id") or "").strip()
            if cell_id:
                cell_by_id[cell_id] = cell
                cell_part_by_id[cell_id] = part_index
        for image in part.cell_images:
            cell_id = str(image.get("cell_id") or image.get("id") or "").strip()
            if cell_id:
                image_cell_ids.add(cell_id)

    row_cell_ids: list[str] = []
    for binding in record.get("row_bindings") or []:
        if not isinstance(binding, dict):
            continue
        row_cell_ids.extend(str(value) for value in binding.get("cell_ids") or [])
    if not row_cell_ids:
        column_bindings = [
            binding
            for binding in record.get("column_bindings") or []
            if isinstance(binding, dict)
        ]
        for binding in sorted(
            column_bindings,
            key=lambda value: _coerce_int_or_default(
                value.get("column_index"), 2**31 - 1
            ),
        ):
            row_cell_ids.extend(str(value) for value in binding.get("cell_ids") or [])
    row_cell_ids = list(dict.fromkeys(value for value in row_cell_ids if value))
    if not row_cell_ids:
        return ""

    header_bindings = [
        binding
        for binding in record.get("header_bindings") or []
        if isinstance(binding, dict) and str(binding.get("text") or "").strip()
    ]
    bound_header_rows: dict[int, set[int]] = {}
    for binding in header_bindings:
        for cell_id in binding.get("cell_ids") or []:
            normalized_id = str(cell_id)
            cell = cell_by_id.get(normalized_id)
            part_index = cell_part_by_id.get(normalized_id)
            if cell is None or part_index is None:
                continue
            row = _coerce_int_or_default(cell.get("row"), -1)
            if row >= 0:
                bound_header_rows.setdefault(part_index, set()).add(row)

    def cell_column(cell_id: str, fallback: int) -> int:
        cell = cell_by_id.get(cell_id) or {}
        return _coerce_int_or_default(cell.get("column"), fallback)

    def leaf_header_for_cell(cell_id: str, column: int) -> str:
        part_index = cell_part_by_id.get(cell_id)
        if part_index is None:
            return ""
        matched: list[tuple[int, int, int, str]] = []
        for original_index, binding in enumerate(header_bindings):
            for header_cell_id in binding.get("cell_ids") or []:
                normalized_header_id = str(header_cell_id)
                header_cell = cell_by_id.get(normalized_header_id)
                if (
                    header_cell is None
                    or cell_part_by_id.get(normalized_header_id) != part_index
                ):
                    continue
                header_column = _coerce_int_or_default(
                    header_cell.get("column"), 2**31 - 1
                )
                column_span = max(
                    1,
                    _coerce_optional_int(header_cell.get("column_span")) or 1,
                )
                if not (header_column <= column < header_column + column_span):
                    continue
                value = str(
                    header_cell.get("display_value")
                    if header_cell.get("display_value") is not None
                    else header_cell.get("raw_text")
                    if header_cell.get("raw_text") is not None
                    else header_cell.get("text") or binding.get("text") or ""
                ).strip()
                if value:
                    matched.append(
                        (
                            _coerce_int_or_default(header_cell.get("row"), -1),
                            -column_span,
                            original_index,
                            value,
                        )
                    )
        # Some parsers bind only one representative cell for a complete
        # header row.  Recover sibling header cells from those proven header
        # rows, but only inside the current physical table part.  This avoids
        # leaking a later cross-page part's row-zero headers into this record.
        for sibling_index, cell in enumerate(cells_by_part.get(part_index, [])):
            row = _coerce_int_or_default(cell.get("row"), -1)
            start_column = _coerce_int_or_default(cell.get("column"), -1)
            column_span = max(
                1,
                _coerce_optional_int(cell.get("column_span")) or 1,
            )
            if (
                row not in bound_header_rows.get(part_index, set())
                or start_column < 0
                or not (start_column <= column < start_column + column_span)
            ):
                continue
            value = str(
                cell.get("display_value")
                if cell.get("display_value") is not None
                else cell.get("raw_text")
                if cell.get("raw_text") is not None
                else cell.get("text") or ""
            ).strip()
            if value:
                matched.append((row, -column_span, sibling_index, value))
        if matched:
            return max(matched)[3]
        return fallback_headers.get(part_index, {}).get(column, "")

    lines: list[str] = []
    for fallback_index, cell_id in sorted(
        enumerate(row_cell_ids),
        key=lambda item: cell_column(item[1], item[0]),
    ):
        cell = cell_by_id.get(cell_id) or {}
        column = cell_column(cell_id, fallback_index)
        value = str(
            cell.get("display_value")
            if cell.get("display_value") is not None
            else cell.get("raw_text")
            if cell.get("raw_text") is not None
            else cell.get("text") or ""
        ).strip()
        if not value and cell_id in image_cell_ids:
            value = "[图片见上方原表]"
        if not value:
            continue
        label = leaf_header_for_cell(cell_id, column)
        lines.append(f"{label}：{value}" if label else value)
    return "\n".join(lines)


def _display_record_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, dict):
        if "text" in value and value.get("text") not in (None, ""):
            return str(value.get("text")).strip()
        pairs = [
            f"{str(key).strip()}：{_display_record_value(item)}"
            for key, item in value.items()
            if key not in {"source_refs", "cell_ids", "id", "record_id"}
            and item not in (None, "", [], {})
        ]
        return "；".join(pair for pair in pairs if not pair.endswith("："))
    if isinstance(value, (list, tuple)):
        return "；".join(
            rendered
            for rendered in (_display_record_value(item) for item in value)
            if rendered
        )
    return str(value).strip()


def _write_simple_table_part(
    *,
    sheet,
    start_row: int,
    part: _TableExportPart,
    cell_format: Any,
    header_format: Any,
    native_column_widths: tuple[float, ...],
) -> int:
    rows = ([part.headers] if part.headers else []) + part.rows
    cursor = start_row
    max_columns = max((len(row) for row in rows), default=1)
    for row_index, row in enumerate(rows):
        if cursor >= 1_048_575:
            sheet.write(
                cursor,
                0,
                "Excel row limit reached; source evidence remains in the snapshot.",
            )
            return cursor + 1
        normalized = [
            _ensure_excel_cell_text(value, context="source table row cell")
            for value in (row + [""] * (max_columns - len(row)))
        ]
        row_format = header_format if part.headers and row_index == 0 else cell_format
        sheet.write_row(cursor, 0, normalized[:16_384], row_format)
        row_height = max(
            (
                _estimate_total_text_height(
                    value,
                    _excel_column_width_to_pixels(
                        native_column_widths[column]
                        if column < len(native_column_widths)
                        else 14.0
                    ),
                    cap_to_excel_row=True,
                )
                for column, value in enumerate(normalized)
            ),
            default=_DEFAULT_ROW_HEIGHT_POINTS,
        )
        sheet.set_row(cursor, row_height)
        cursor += 1
    return cursor


def _write_rich_table_part(
    *,
    sheet,
    start_row: int,
    part: _TableExportPart,
    cell_format: Any,
    header_format: Any,
    source_image_context: _ExportSourceImageContext | None,
    image_stats: dict[str, int],
    warnings: list[str],
    native_column_widths: tuple[float, ...],
) -> int:
    header_positions = _header_positions(part)
    region_by_anchor = {
        (region["row"], region["column"]): region for region in part.merged_regions
    }
    cell_layout_by_position: dict[tuple[int, int], tuple[float, int]] = {}
    max_source_row = 0
    occupied: set[tuple[int, int]] = set()
    for cell in sorted(
        part.cells,
        key=lambda value: (
            _coerce_optional_int(value.get("row")) or 0,
            _coerce_optional_int(value.get("column")) or 0,
        ),
    ):
        source_row = _coerce_optional_int(cell.get("row")) or 0
        source_column = _coerce_optional_int(cell.get("column")) or 0
        if source_row >= 1_048_575 - start_row or source_column >= 16_384:
            continue
        region = region_by_anchor.get((source_row, source_column), {})
        row_span = max(
            1,
            _coerce_optional_int(cell.get("row_span"))
            or _coerce_optional_int(region.get("row_span"))
            or 1,
        )
        column_span = max(
            1,
            _coerce_optional_int(cell.get("column_span"))
            or _coerce_optional_int(region.get("column_span"))
            or 1,
        )
        row_span = min(row_span, 1_048_576 - (start_row + source_row))
        column_span = min(column_span, 16_384 - source_column)
        coverage = {
            (row, column)
            for row in range(source_row, source_row + row_span)
            for column in range(source_column, source_column + column_span)
        }
        if occupied.intersection(coverage):
            continue
        occupied.update(coverage)
        cell_value = _table_cell_export_value(cell)
        text = _ensure_excel_cell_text(
            _table_cell_display_text(cell),
            context=f"source table cell {cell.get('id') or cell.get('cell_id') or '<unknown>'}",
        )
        target_row = start_row + source_row
        target_column = source_column
        target_format = (
            header_format
            if (source_row, source_column) in header_positions
            else cell_format
        )
        cell_width_px = sum(
            _excel_column_width_to_pixels(width)
            for width in native_column_widths[
                target_column : target_column + column_span
            ]
        )
        text_height_points = _estimate_total_text_height(
            text,
            max(1, cell_width_px),
            cap_to_excel_row=True,
        )
        cell_layout_by_position[(source_row, source_column)] = (
            text_height_points,
            cell_width_px,
        )
        if row_span > 1 or column_span > 1:
            sheet.merge_range(
                target_row,
                target_column,
                target_row + row_span - 1,
                target_column + column_span - 1,
                cell_value,
                target_format,
            )
        else:
            sheet.write(target_row, target_column, cell_value, target_format)
        max_source_row = max(max_source_row, source_row + row_span - 1)
        if text:
            sheet.set_row(
                target_row,
                max(
                    sheet.row_sizes.get(target_row, [_DEFAULT_ROW_HEIGHT_POINTS])[0]
                    if hasattr(sheet, "row_sizes")
                    else _DEFAULT_ROW_HEIGHT_POINTS,
                    text_height_points,
                ),
            )

    _insert_cell_images(
        sheet=sheet,
        start_row=start_row,
        images=part.cell_images,
        source_image_context=source_image_context,
        image_stats=image_stats,
        warnings=warnings,
        cell_layout_by_position=cell_layout_by_position,
    )
    return start_row + max_source_row + 1


def _header_positions(part: _TableExportPart) -> set[tuple[int, int]]:
    positions: set[tuple[int, int]] = set()
    rich = part.raw_ref.get("table_payload") or part.raw_ref.get("table")
    payload = rich if isinstance(rich, dict) else part.raw_ref
    for region in payload.get("header_regions") or []:
        if not isinstance(region, dict):
            continue
        row = _coerce_optional_int(region.get("row"))
        column = _coerce_optional_int(region.get("column"))
        row_span = _coerce_optional_int(region.get("row_span")) or 1
        column_span = _coerce_optional_int(region.get("column_span")) or 1
        if row is None or column is None:
            continue
        positions.update(
            (row_index, column_index)
            for row_index in range(row, row + row_span)
            for column_index in range(column, column + column_span)
        )
    return positions


def _insert_cell_images(
    *,
    sheet,
    start_row: int,
    images: list[dict[str, Any]],
    source_image_context: _ExportSourceImageContext | None,
    image_stats: dict[str, int],
    warnings: list[str],
    cell_layout_by_position: dict[tuple[int, int], tuple[float, int]],
) -> None:
    stacked_height_px: dict[tuple[int, int], int] = {}
    for image in images:
        row = _coerce_optional_int(image.get("row"))
        column = _coerce_optional_int(image.get("column"))
        if row is None or column is None or row < 0 or column < 0:
            image_stats["failed"] = image_stats.get("failed", 0) + 1
            warnings.append("table cell image has an invalid row/column binding")
            continue
        payload = _load_image_payload(
            image,
            source_image_context=source_image_context,
        )
        if payload is None:
            image_stats["failed"] = image_stats.get("failed", 0) + 1
            warning = "table cell image could not be resolved: " + str(
                image.get("asset_id") or image.get("cell_id") or "unknown"
            )
            warnings.append(warning)
            sheet.write_comment(
                start_row + row,
                column,
                f"⚠ {warning}",
                {"author": "ReqPilotX"},
            )
            continue
        buffer, width, height = payload
        text_height_points, cell_width_px = cell_layout_by_position.get(
            (row, column),
            (
                _DEFAULT_ROW_HEIGHT_POINTS,
                _excel_column_width_to_pixels(22.0),
            ),
        )
        target_width = max(24, min(180, cell_width_px - 8))
        target_height = 96
        position = (row, column)
        prior_image_height_px = stacked_height_px.get(position, 0)
        text_height_px = int(round(text_height_points / 0.75))
        y_offset_px = text_height_px + prior_image_height_px + 4
        remaining_height_px = max(16, _MAX_ROW_HEIGHT_PX - y_offset_px - 4)
        scale = min(
            1.0,
            target_width / max(width, 1),
            target_height / max(height, 1),
            remaining_height_px / max(height, 1),
        )
        rendered_height_px = max(1, int(round(height * scale)))
        target_row = start_row + row
        existing_height = (
            sheet.row_sizes.get(target_row, [_DEFAULT_ROW_HEIGHT_POINTS])[0]
            if hasattr(sheet, "row_sizes")
            else _DEFAULT_ROW_HEIGHT_POINTS
        )
        sheet.set_row(
            target_row,
            min(
                _MAX_ROW_HEIGHT_POINTS,
                max(
                    existing_height,
                    _pixels_to_row_height_points(y_offset_px + rendered_height_px + 4),
                ),
            ),
        )
        sheet.insert_image(
            target_row,
            column,
            "cell-image.png",
            {
                "image_data": buffer,
                "object_position": 2,
                "x_scale": scale,
                "y_scale": scale,
                "x_offset": 3,
                "y_offset": y_offset_px,
            },
        )
        stacked_height_px[position] = prior_image_height_px + rendered_height_px + 4
        image_stats["inserted"] = image_stats.get("inserted", 0) + 1


def _load_image_payload(
    image: dict[str, Any],
    *,
    source_image_context: _ExportSourceImageContext | None = None,
) -> tuple[BytesIO, int, int] | None:
    location = image.get('location') or image.get('native_locator') or {}
    if location.get('format') in {'docx', 'xlsx'}:
        if source_image_context is None or source_image_context.source_file_path is None:
            return None
        from reqpilotx.application.agent_workflow.office_source_evidence import validate_office_image_reference
        if source_image_context.office_source_cache is None:
            source_image_context.office_source_cache = {}
        validate_office_image_reference(source_image_context.source_file_path, source_image_context.source_sha256,
            image, cache=source_image_context.office_source_cache)
    source_object = _source_object_asset_payload(
        image,
        source_image_context=source_image_context,
    )
    if source_object is not None:
        return source_object
    path = str(
        image.get("image_path")
        or image.get("snapshot_path")
        or image.get("source_snapshot_path")
        or image.get("path")
        or ""
    ).strip()
    if path:
        try:
            raw = load_snapshot_bytes(path)
            expected_sha256 = str(image.get("content_sha256") or "").strip().lower()
            if (
                _SHA256_PATTERN.fullmatch(expected_sha256)
                and hashlib.sha256(raw).hexdigest() != expected_sha256
            ):
                raw = b""
            if raw:
                with PILImage.open(BytesIO(raw)) as parsed:
                    width, height = int(parsed.width), int(parsed.height)
                return BytesIO(raw), width, height
        except Exception:
            pass
    source_crop = _source_pdf_crop_payload(
        image,
        source_image_context=source_image_context,
        require_declared_rendering=True,
    )
    if source_crop is not None:
        return source_crop
    return _source_package_asset_payload(
        image,
        source_image_context=source_image_context,
    )


def _write_original_snapshot_fallbacks(
    *,
    sheet,
    cursor: int,
    snapshot_paths: list[str],
    label_format: Any,
    source_ref: dict[str, Any],
    source_image_context: _ExportSourceImageContext | None,
    last_col: int,
    max_width_px: int,
) -> int:
    resolved_snapshots: list[tuple[BytesIO, int, int]] = []
    for path in snapshot_paths:
        try:
            raw = load_snapshot_bytes(path)
            with PILImage.open(BytesIO(raw)) as image:
                width, height = int(image.width), int(image.height)
        except Exception:
            continue
        resolved_snapshots.append((BytesIO(raw), width, height))
    if not resolved_snapshots:
        source_crop = _source_pdf_crop_payload(
            source_ref,
            source_image_context=source_image_context,
            require_declared_rendering=False,
        )
        if source_crop is not None:
            resolved_snapshots.append(source_crop)

    for image_buffer, width, height in resolved_snapshots:
        sheet.merge_range(
            cursor,
            0,
            cursor,
            last_col,
            "Original table snapshot",
            label_format,
        )
        cursor += 1
        prepared = _prepare_image_rows(
            image_buffer=image_buffer,
            image_width_px=width,
            image_height_px=height,
            max_width_px=max_width_px,
        )
        for segment in prepared:
            if segment.image_buffer is None:
                continue
            sheet.set_row(cursor, segment.row_height)
            sheet.insert_image(
                cursor,
                0,
                "table-snapshot.png",
                {
                    "image_data": segment.image_buffer,
                    "object_position": 2,
                    "x_scale": segment.x_scale,
                    "y_scale": segment.y_scale,
                    "x_offset": 3,
                    "y_offset": 3,
                },
            )
            cursor += 1
    return cursor


def _slug(value: str) -> str:
    return (
        "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")
        or "project"
    )
