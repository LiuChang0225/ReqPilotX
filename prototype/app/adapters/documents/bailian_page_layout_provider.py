from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from app.adapters.documents.bailian_visual_text_provider import (
    BAILIAN_VISUAL_TEXT_PROVIDER_ID,
)
from app.adapters.documents.visual_text_provider_support import (
    inverse_orthogonal_pixel_polygon_rotation,
    pixel_polygon_to_page_bbox,
    validate_visual_text_page_input,
)
from app.domain.source_visual_text import (
    VISUAL_CONTENT_TYPES,
    VisualContentType,
    VisualLayoutTable,
    VisualLayoutTableCell,
    VisualTextObservation,
    VisualTextPageInput,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)
from app.services.visual_text_snapshot_cache import (
    VisualTextProviderTelemetryCallback,
    VisualTextProviderTelemetryV1,
    notify_visual_text_provider_telemetry,
)
from PIL import Image as PILImage
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

BAILIAN_PAGE_LAYOUT_PROVIDER_IMPLEMENTATION_VERSION = "1.1.0"
DEFAULT_BAILIAN_PAGE_LAYOUT_MODEL = "qwen3.7-plus"
DEFAULT_BAILIAN_FLASH_PAGE_LAYOUT_MODEL = "qwen3.6-flash"
DEFAULT_BAILIAN_SCREENING_PAGE_LAYOUT_MODEL = "qwen3.7-flash"
PAGE_LAYOUT_RESPONSE_SCHEMA_VERSION = "page-layout/v1"
_PROMPT_VERSION = "layout-six-types/v3"
_INPUT_ROTATION_TRANSFORM_VERSION = "pb1"
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_MAX_BASE64_CHARACTERS = 24 * 1024 * 1024
_MAX_IMAGE_PIXELS = 16_000_000
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_ELEMENTS = 1_000
_MAX_TEXT_CHARACTERS = 2_000_000
_MAX_TABLE_GRID_POSITIONS = 500_000
_MODEL_MAX_PIXELS = 1_572_864
_MAX_COMPLETION_TOKENS = 32_768
_NORMALIZED_EXTENT = 1_000.0
_ALLOWED_INPUT_ROTATIONS = frozenset({0, 90, 180, 270})
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})
_PIL_TRANSPOSE_BY_CLOCKWISE_ROTATION = {
    90: PILImage.Transpose.ROTATE_270,
    180: PILImage.Transpose.ROTATE_180,
    270: PILImage.Transpose.ROTATE_90,
}

_PAGE_LAYOUT_PROMPT = """\
你是文档页面版面转录器。请只依据图片中可见内容，输出一个 JSON 对象，不要解释，不要使用 Markdown。

目标是高保真恢复信息结构，而不是判断“是否为需求”。请按阅读顺序识别六类内容：
TEXT、TABLE、FIGURE、HEADER、FOOTER、WATERMARK。

规则：
1. 坐标使用图片左上角为原点的 0..1000 归一化坐标 [x0,y0,x1,y1]，必须在范围内且有正面积。
2. TEXT 以自然段、标题、列表项或完整目录块为单位；不要逐物理行拆成多个块。保留块内原始换行、编号、缩进文本和标点。
3. 目录中的 TEXT 设置 role="TOC"；其他块 role=null。页码若只是页眉或页脚，必须标为 HEADER 或 FOOTER，不能并入正文。
4. 每个表格只输出一个 TABLE，不要把表内文字再重复输出为 TEXT。表格必须给出完整矩形网格，单元格按 row、column 排序，合并格只在锚点输出并使用 span。保留单元格内换行、水平/垂直对齐和缩进级别。输出前必须逐表自检：row_count=max(row+row_span)，column_count=max(column+column_span)；网格内每个坐标必须恰好由一个单元格覆盖一次；图片四周或表格边缘的空白边距不算表格行列。
5. 图片、示意图、图标或表格单元格内图片标为 FIGURE。不要编造图片语义；text 只转录图内实际可见文字。若图片唯一属于某个表格单元格，填写 parent_table_index 和 parent_cell。
6. starts_mid_table 表示本页表格从上一页继续；continues_next_page 表示表格在下一页继续；不确定时填 false。
7. 不得遗漏肉眼可见的块，不得输出不可见内容。空白图片允许 text=""；TEXT/HEADER/FOOTER/WATERMARK 的 text 不得为空。

严格输出：
{
  "schema_version": "page-layout/v1",
  "coordinate_space": "NORMALIZED_1000_TOP_LEFT",
  "evaluated_types": ["TEXT","TABLE","FIGURE","HEADER","FOOTER","WATERMARK"],
  "elements": [
    {
      "index": 0,
      "type": "TEXT",
      "role": null,
      "bbox": [0,0,1000,1000],
      "text": "可见原文",
      "table": null,
      "parent_table_index": null,
      "parent_cell": null
    }
  ]
}

TABLE 的 table 对象格式：
{
  "row_count": 2,
  "column_count": 2,
  "header_row_count": 1,
  "starts_mid_table": false,
  "continues_next_page": false,
  "cells": [
    {
      "row": 0,
      "column": 0,
      "row_span": 1,
      "column_span": 1,
      "text": "单元格原文",
      "horizontal_alignment": "LEFT",
      "vertical_alignment": "TOP",
      "indent_level": 0
    }
  ]
}
对不适用的 table、parent_table_index、parent_cell 使用 null。\
"""


class _StrictResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _LayoutCellResponse(_StrictResponseModel):
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(ge=1)
    column_span: StrictInt = Field(ge=1)
    text: StrictStr
    horizontal_alignment: Literal["LEFT", "CENTER", "RIGHT", "JUSTIFY"] | None = None
    vertical_alignment: Literal["TOP", "MIDDLE", "BOTTOM"] | None = None
    indent_level: StrictInt = Field(ge=0, le=20)


class _LayoutTableResponse(_StrictResponseModel):
    row_count: StrictInt = Field(ge=1, le=4_000)
    column_count: StrictInt = Field(ge=1, le=1_000)
    header_row_count: StrictInt = Field(ge=0)
    starts_mid_table: StrictBool
    continues_next_page: StrictBool
    cells: list[_LayoutCellResponse] = Field(min_length=1, max_length=500_000)


class _LayoutElementResponse(_StrictResponseModel):
    index: StrictInt = Field(ge=0)
    type: VisualContentType
    role: Literal["TOC"] | None = None
    bbox: list[StrictInt | float] = Field(min_length=4, max_length=4)
    text: StrictStr = ""
    table: _LayoutTableResponse | None = None
    parent_table_index: StrictInt | None = Field(default=None, ge=0)
    parent_cell: list[StrictInt] | None = Field(
        default=None,
        min_length=2,
        max_length=2,
    )


class _LayoutPageResponse(_StrictResponseModel):
    schema_version: Literal["page-layout/v1"]
    coordinate_space: Literal["NORMALIZED_1000_TOP_LEFT"]
    evaluated_types: list[VisualContentType]
    elements: list[_LayoutElementResponse] = Field(max_length=_MAX_ELEMENTS)


class _PageLayoutOutputTruncated(VisualTextProviderRejected):
    """Internal marker for a bounded completion that exhausted its token budget."""

    def __init__(self) -> None:
        super().__init__(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "Bailian returned a truncated page-layout response.",
        )


class _PageLayoutRequestDeadlineExceeded(Exception):
    """Internal marker for one streaming request exceeding its wall-clock budget."""


@dataclass(frozen=True, slots=True)
class _PageLayoutTableGridDiagnostic:
    """Content-free, zero-based coordinates for one incomplete provider table."""

    element_index: int
    table_ordinal: int
    declared_row_count: int
    declared_column_count: int
    anchor_count: int
    covered_count: int
    expected_count: int
    missing_coordinate_count: int
    missing_coordinate_sha256: str
    diagnostic_code: str = "TABLE_GRID_INCOMPLETE"
    schema_version: str = "page-layout-table-grid-diagnostic/v1"


class _PageLayoutTableGridInvalid(VisualTextProviderRejected):
    """Private provider rejection carrying only bounded, content-free grid facts."""

    def __init__(self, safe_diagnostic: _PageLayoutTableGridDiagnostic) -> None:
        self.safe_diagnostic = safe_diagnostic
        super().__init__(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "Bailian returned an incomplete page-layout table grid.",
        )


class BailianPageLayoutProvider:
    """One-call Bailian page layout capture using an approved Qwen vision model."""

    provider_id = BAILIAN_VISUAL_TEXT_PROVIDER_ID

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = DEFAULT_BAILIAN_PAGE_LAYOUT_MODEL,
        input_rotations_by_page: Mapping[int, int] | None = None,
        timeout_seconds: float = 45.0,
        max_retries: int = 0,
        http_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
    ) -> None:
        normalized_model = str(model or "").strip()
        if _MODEL_PATTERN.fullmatch(normalized_model) is None:
            raise ValueError("Bailian page-layout model has an invalid identifier")
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if isinstance(max_retries, bool) or not 0 <= int(max_retries) <= 1:
            raise ValueError("max_retries must be zero or one")
        self._api_key = str(api_key or "").strip()
        self._endpoint = _chat_completions_endpoint(base_url)
        self._model = normalized_model
        self._input_rotations_by_page = _normalize_input_rotations_by_page(
            input_rotations_by_page
        )
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = int(max_retries)
        self._http_client = http_client
        self._sleep = sleep
        self._monotonic = monotonic
        self._telemetry_callback = telemetry_callback
        self.provider_version = _provider_version(
            model=self._model,
            input_rotations_by_page=self._input_rotations_by_page,
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
        )

    def recognize_page(self, page_input: VisualTextPageInput) -> VisualTextSnapshot:
        started_at = self._monotonic()
        page_deadline = started_at + self._timeout_seconds
        request_count = 0
        outcome: Literal["READY", "FAILED"] = "FAILED"

        def count_request() -> None:
            nonlocal request_count
            request_count += 1

        try:
            validate_visual_text_page_input(page_input)
            if not self._api_key or not self._api_key.isascii():
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.UNAVAILABLE,
                    "Bailian page-layout recognition is not configured.",
                )
            _validate_image_limits(page_input)
            input_rotation = self._input_rotations_by_page.get(
                page_input.page_number,
                0,
            )
            provider_image, provider_width, provider_height = _page_image_for_provider(
                page_input=page_input,
                clockwise_rotation_degrees=input_rotation,
            )
            encoded_image = base64.b64encode(provider_image).decode("ascii")
            if len(encoded_image) > _MAX_BASE64_CHARACTERS:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.INVALID_INPUT,
                    "The rendered page exceeds the Bailian in-memory image limit.",
                )
            request_payload = _page_layout_request_payload(
                model=self._model,
                page_image_media_type=page_input.page_image_media_type,
                encoded_image=encoded_image,
            )
            try:
                response_payload = self._post(
                    request_payload,
                    page_deadline=page_deadline,
                    request_started_callback=count_request,
                )
                layout_payload = _layout_payload_from_completion(response_payload)
                parsed = _LayoutPageResponse.model_validate(layout_payload)
                snapshot = _snapshot_from_layout(
                    parsed,
                    page_input=page_input,
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    input_rotation_degrees=input_rotation,
                    provider_image_width_px=provider_width,
                    provider_image_height_px=provider_height,
                )
                outcome = "READY"
                return snapshot
            except VisualTextProviderRejected:
                raise
            except Exception as exc:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                    "Bailian returned an invalid page-layout response.",
                ) from exc
        finally:
            notify_visual_text_provider_telemetry(
                self._telemetry_callback,
                VisualTextProviderTelemetryV1(
                    page_number=int(getattr(page_input, "page_number", 0) or 0),
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    request_count=request_count,
                    latency_ms=max(
                        0,
                        round((self._monotonic() - started_at) * 1_000),
                    ),
                    cache_hit=False,
                    outcome=outcome,
                ),
            )

    def _post(
        self,
        request_payload: dict[str, object],
        *,
        page_deadline: float,
        request_started_callback: Callable[[], None],
    ) -> object:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        owned_client = self._http_client is None
        client = self._http_client or httpx.Client(follow_redirects=False)
        try:
            for attempt in range(self._max_retries + 1):
                try:
                    remaining_seconds = _remaining_page_layout_seconds(
                        deadline=page_deadline,
                        monotonic=self._monotonic,
                    )
                    request_started_callback()
                    with client.stream(
                        "POST",
                        self._endpoint,
                        headers=headers,
                        json=request_payload,
                        timeout=remaining_seconds,
                    ) as response:
                        status_code = int(getattr(response, "status_code", 0) or 0)
                        if status_code == 200:
                            return _completion_from_sse_response(
                                response,
                                deadline=page_deadline,
                                monotonic=self._monotonic,
                            )
                        if status_code in {401, 403}:
                            raise VisualTextProviderRejected(
                                VisualTextProviderRejectionCode.UNAVAILABLE,
                                "Bailian page-layout recognition is not authorized.",
                            )
                        if status_code == 413:
                            raise VisualTextProviderRejected(
                                VisualTextProviderRejectionCode.INVALID_INPUT,
                                "The rendered page is too large for Bailian.",
                            )
                        if (
                            status_code == 429 or 500 <= status_code <= 599
                        ) and attempt < self._max_retries:
                            _sleep_before_page_layout_retry(
                                attempt=attempt,
                                deadline=page_deadline,
                                sleep=self._sleep,
                                monotonic=self._monotonic,
                            )
                            continue
                        if status_code == 429:
                            raise VisualTextProviderRejected(
                                VisualTextProviderRejectionCode.RATE_LIMITED,
                                "Bailian page-layout recognition is rate limited.",
                            )
                        raise VisualTextProviderRejected(
                            VisualTextProviderRejectionCode.PROVIDER_FAILED,
                            "Bailian page-layout recognition failed.",
                        )
                except VisualTextProviderRejected:
                    raise
                except _PageLayoutRequestDeadlineExceeded as exc:
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.PROVIDER_FAILED,
                        "Bailian page-layout recognition failed.",
                    ) from exc
                except httpx.TransportError as exc:
                    if attempt < self._max_retries:
                        _sleep_before_page_layout_retry(
                            attempt=attempt,
                            deadline=page_deadline,
                            sleep=self._sleep,
                            monotonic=self._monotonic,
                        )
                        continue
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.PROVIDER_FAILED,
                        "Bailian page-layout recognition failed.",
                    ) from exc
                except (UnicodeError, ValueError) as exc:
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                        "Bailian returned an invalid page-layout stream.",
                    ) from exc
                except Exception as exc:
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.PROVIDER_FAILED,
                        "Bailian page-layout recognition failed.",
                    ) from exc
            raise AssertionError("bounded retry loop terminated unexpectedly")
        finally:
            if owned_client:
                client.close()


def _page_layout_request_payload(
    *,
    model: str,
    page_image_media_type: str,
    encoded_image: str,
) -> dict[str, object]:
    content: list[dict[str, object]] = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{page_image_media_type};base64,{encoded_image}",
            },
            "min_pixels": 65_536,
            "max_pixels": _MODEL_MAX_PIXELS,
        },
        {
            "type": "text",
            "text": _PAGE_LAYOUT_PROMPT,
        },
    ]
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": _MAX_COMPLETION_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def _snapshot_from_layout(
    parsed: _LayoutPageResponse,
    *,
    page_input: VisualTextPageInput,
    provider_id: str,
    provider_version: str,
    input_rotation_degrees: int,
    provider_image_width_px: int,
    provider_image_height_px: int,
) -> VisualTextSnapshot:
    if tuple(parsed.evaluated_types) != VISUAL_CONTENT_TYPES:
        raise ValueError("provider must evaluate all six page content types")
    if [element.index for element in parsed.elements] != list(
        range(len(parsed.elements))
    ):
        raise ValueError(
            "page-layout elements must use sequential reading-order indexes"
        )
    total_text_characters = sum(
        len(element.text)
        + (
            sum(len(cell.text) for cell in element.table.cells)
            if element.table is not None
            else 0
        )
        for element in parsed.elements
    )
    if total_text_characters > _MAX_TEXT_CHARACTERS:
        raise ValueError("page-layout response exceeds the text budget")

    observations: list[VisualTextObservation] = []
    table_ordinal = 0
    for element in parsed.elements:
        bbox = _normalized_bbox_to_page_bbox(
            element.bbox,
            page_input=page_input,
            input_rotation_degrees=input_rotation_degrees,
            provider_image_width_px=provider_image_width_px,
            provider_image_height_px=provider_image_height_px,
        )
        table = None
        if element.table is not None:
            table = _layout_table_from_response(
                element.table,
                element_index=element.index,
                table_ordinal=table_ordinal,
            )
            table_ordinal += 1
        parent_cell = (
            tuple(element.parent_cell) if element.parent_cell is not None else None
        )
        observation_id = stable_visual_text_observation_id(
            source_sha256=page_input.source_sha256,
            page_image_sha256=page_input.page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_input.page_number,
            observation_index=element.index,
            bbox=bbox,
        )
        observations.append(
            VisualTextObservation(
                observation_id=observation_id,
                observation_index=element.index,
                source_sha256=page_input.source_sha256,
                page_number=page_input.page_number,
                bbox=bbox,
                raw_text=element.text,
                content_type=element.type,
                role=element.role,
                layout_table=table,
                parent_observation_index=element.parent_table_index,
                parent_table_cell=parent_cell,
                confidence=None,
                provider_id=provider_id,
                provider_version=provider_version,
                page_image_sha256=page_input.page_image_sha256,
            )
        )

    snapshot_id = stable_visual_text_snapshot_id(
        source_sha256=page_input.source_sha256,
        page_image_sha256=page_input.page_image_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_input.page_number,
        page_bbox=page_input.page_bbox,
        page_image_width_px=page_input.page_image_width_px,
        page_image_height_px=page_input.page_image_height_px,
    )
    return VisualTextSnapshot(
        visual_text_snapshot_id=snapshot_id,
        source_sha256=page_input.source_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_input.page_number,
        page_bbox=page_input.page_bbox,
        page_image_sha256=page_input.page_image_sha256,
        page_image_media_type=page_input.page_image_media_type,
        page_image_width_px=page_input.page_image_width_px,
        page_image_height_px=page_input.page_image_height_px,
        evaluated_content_types=VISUAL_CONTENT_TYPES,
        observations=tuple(observations),
    )


def _layout_table_from_response(
    value: _LayoutTableResponse,
    *,
    element_index: int,
    table_ordinal: int,
) -> VisualLayoutTable:
    safe_diagnostic = _incomplete_table_grid_diagnostic(
        value,
        element_index=element_index,
        table_ordinal=table_ordinal,
    )
    if safe_diagnostic is not None:
        raise _PageLayoutTableGridInvalid(safe_diagnostic)
    return VisualLayoutTable(
        row_count=value.row_count,
        column_count=value.column_count,
        header_row_count=value.header_row_count,
        starts_mid_table=value.starts_mid_table,
        continues_next_page=value.continues_next_page,
        cells=tuple(
            VisualLayoutTableCell(
                row=cell.row,
                column=cell.column,
                row_span=cell.row_span,
                column_span=cell.column_span,
                raw_text=cell.text,
                horizontal_alignment=cell.horizontal_alignment,
                vertical_alignment=cell.vertical_alignment,
                indent_level=cell.indent_level,
            )
            for cell in value.cells
        ),
    )


def _incomplete_table_grid_diagnostic(
    value: _LayoutTableResponse,
    *,
    element_index: int,
    table_ordinal: int,
) -> _PageLayoutTableGridDiagnostic | None:
    expected_count = value.row_count * value.column_count
    if (
        value.header_row_count > value.row_count
        or expected_count > _MAX_TABLE_GRID_POSITIONS
    ):
        return None
    anchors = [(cell.row, cell.column) for cell in value.cells]
    if anchors != sorted(anchors) or len(anchors) != len(set(anchors)):
        return None
    occupied: set[tuple[int, int]] = set()
    for cell in value.cells:
        if (
            cell.row + cell.row_span > value.row_count
            or cell.column + cell.column_span > value.column_count
        ):
            return None
        coverage = {
            (row, column)
            for row in range(cell.row, cell.row + cell.row_span)
            for column in range(cell.column, cell.column + cell.column_span)
        }
        if occupied.intersection(coverage):
            return None
        occupied.update(coverage)
    if len(occupied) == expected_count:
        return None
    missing_coordinates = sorted(
        (row, column)
        for row in range(value.row_count)
        for column in range(value.column_count)
        if (row, column) not in occupied
    )
    missing_coordinate_bytes = json.dumps(
        missing_coordinates,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return _PageLayoutTableGridDiagnostic(
        element_index=element_index,
        table_ordinal=table_ordinal,
        declared_row_count=value.row_count,
        declared_column_count=value.column_count,
        anchor_count=len(value.cells),
        covered_count=len(occupied),
        expected_count=expected_count,
        missing_coordinate_count=len(missing_coordinates),
        missing_coordinate_sha256=hashlib.sha256(missing_coordinate_bytes).hexdigest(),
    )


def _layout_payload_from_completion(payload: object) -> object:
    if not isinstance(payload, dict):
        raise ValueError("completion response must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("completion response must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("completion choice must be an object")
    finish_reason = choice.get("finish_reason")
    _reject_truncated_finish_reason(finish_reason)
    if finish_reason != "stop":
        raise ValueError("page-layout completion did not finish normally")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("completion message is missing")
    content = message.get("content")
    if isinstance(content, list):
        text_parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if len(text_parts) != 1:
            raise ValueError("completion content must contain one JSON text part")
        content = text_parts[0]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("completion content is missing")
    if len(content) > _MAX_RESPONSE_BYTES:
        raise ValueError("completion content exceeds the response budget")
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("page-layout JSON must be an object")
    return parsed


def _completion_from_sse_response(
    response: Any,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> dict[str, object]:
    content_parts: list[str] = []
    finish_reason: str | None = None
    response_bytes = 0
    saw_done = False
    _raise_if_page_layout_deadline_elapsed(
        deadline=deadline,
        monotonic=monotonic,
    )
    try:
        lines = response.iter_lines()
    except Exception as exc:
        raise ValueError("streaming response cannot be read") from exc
    for raw_line in lines:
        _raise_if_page_layout_deadline_elapsed(
            deadline=deadline,
            monotonic=monotonic,
        )
        if isinstance(raw_line, bytes):
            response_bytes += len(raw_line)
            line = raw_line.decode("utf-8")
        elif isinstance(raw_line, str):
            response_bytes += len(raw_line.encode("utf-8"))
            line = raw_line
        else:
            raise ValueError("streaming response line must be text")
        if response_bytes > _MAX_RESPONSE_BYTES:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                "Bailian returned an oversized page-layout response.",
            )
        line = line.strip()
        if not line:
            continue
        if not line.startswith("data:"):
            raise ValueError("streaming response contains an unsupported event")
        data = line[5:].strip()
        if data == "[DONE]":
            saw_done = True
            break
        chunk = json.loads(data)
        if not isinstance(chunk, dict):
            raise ValueError("streaming completion chunk must be an object")
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            raise ValueError("streaming completion choices are missing")
        if not choices:
            continue
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("streaming completion must contain one choice")
        choice = choices[0]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ValueError("streaming completion delta is missing")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise ValueError("streaming completion content must be text")
            content_parts.append(content)
            if sum(len(part) for part in content_parts) > _MAX_RESPONSE_BYTES:
                raise ValueError("streaming completion content exceeds its budget")
        candidate_finish_reason = choice.get("finish_reason")
        if candidate_finish_reason is not None:
            _reject_truncated_finish_reason(candidate_finish_reason)
            if candidate_finish_reason != "stop" or finish_reason is not None:
                raise ValueError("streaming completion did not finish normally")
            finish_reason = candidate_finish_reason
    _raise_if_page_layout_deadline_elapsed(
        deadline=deadline,
        monotonic=monotonic,
    )
    if not saw_done or finish_reason != "stop" or not content_parts:
        raise ValueError("streaming completion ended without a complete JSON answer")
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "".join(content_parts)},
            }
        ]
    }


def _raise_if_page_layout_deadline_elapsed(
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> None:
    if monotonic() >= deadline:
        raise _PageLayoutRequestDeadlineExceeded


def _remaining_page_layout_seconds(
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> float:
    remaining_seconds = deadline - monotonic()
    if remaining_seconds <= 0:
        raise _PageLayoutRequestDeadlineExceeded
    return remaining_seconds


def _sleep_before_page_layout_retry(
    *,
    attempt: int,
    deadline: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    remaining_seconds = _remaining_page_layout_seconds(
        deadline=deadline,
        monotonic=monotonic,
    )
    delay_seconds = min(0.5 * (2**attempt), 2.0, remaining_seconds)
    sleep(delay_seconds)
    _remaining_page_layout_seconds(
        deadline=deadline,
        monotonic=monotonic,
    )


def _reject_truncated_finish_reason(value: object) -> None:
    if isinstance(value, str) and value.strip().casefold() in _TRUNCATED_FINISH_REASONS:
        raise _PageLayoutOutputTruncated


def _normalized_bbox_to_page_bbox(
    value: list[int | float],
    *,
    page_input: VisualTextPageInput,
    input_rotation_degrees: int,
    provider_image_width_px: int,
    provider_image_height_px: int,
) -> tuple[float, float, float, float]:
    if len(value) != 4 or any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in value
    ):
        raise ValueError("normalized bbox must contain four finite numbers")
    x0, y0, x1, y1 = (float(item) for item in value)
    if (
        x0 < 0
        or y0 < 0
        or x1 > _NORMALIZED_EXTENT
        or y1 > _NORMALIZED_EXTENT
        or x1 <= x0
        or y1 <= y0
    ):
        raise ValueError("normalized bbox must have positive area inside the page")
    provider_coordinates = (
        x0 / _NORMALIZED_EXTENT * provider_image_width_px,
        y0 / _NORMALIZED_EXTENT * provider_image_height_px,
        x1 / _NORMALIZED_EXTENT * provider_image_width_px,
        y0 / _NORMALIZED_EXTENT * provider_image_height_px,
        x1 / _NORMALIZED_EXTENT * provider_image_width_px,
        y1 / _NORMALIZED_EXTENT * provider_image_height_px,
        x0 / _NORMALIZED_EXTENT * provider_image_width_px,
        y1 / _NORMALIZED_EXTENT * provider_image_height_px,
    )
    original_coordinates = inverse_orthogonal_pixel_polygon_rotation(
        coordinates=provider_coordinates,
        original_width_px=page_input.page_image_width_px,
        original_height_px=page_input.page_image_height_px,
        clockwise_rotation_degrees=input_rotation_degrees,
    )
    return pixel_polygon_to_page_bbox(
        coordinates=original_coordinates,
        image_width_px=page_input.page_image_width_px,
        image_height_px=page_input.page_image_height_px,
        page_bbox=page_input.page_bbox,
    )


def _chat_completions_endpoint(base_url: str) -> str:
    parsed = urlsplit(str(base_url or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Bailian base_url must be an HTTPS service base")
    path = parsed.path.rstrip("/")
    if not path:
        raise ValueError("Bailian base_url must include its compatible API path")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def _validate_image_limits(page_input: VisualTextPageInput) -> None:
    width = page_input.page_image_width_px
    height = page_input.page_image_height_px
    if (
        width <= 10
        or height <= 10
        or width * height > _MAX_IMAGE_PIXELS
        or max(width / height, height / width) > 200.0
    ):
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.INVALID_INPUT,
            "The rendered page dimensions are outside Bailian vision limits.",
        )


def _normalize_input_rotations_by_page(
    value: Mapping[int, int] | None,
) -> dict[int, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("input_rotations_by_page must be a mapping")
    normalized: dict[int, int] = {}
    for page_number, rotation in value.items():
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
        ):
            raise ValueError("input rotation page numbers must be positive integers")
        if (
            isinstance(rotation, bool)
            or not isinstance(rotation, int)
            or rotation not in _ALLOWED_INPUT_ROTATIONS
        ):
            raise ValueError("input rotations must be 0, 90, 180, or 270 degrees")
        normalized[page_number] = rotation
    return dict(sorted(normalized.items()))


def _page_image_for_provider(
    *,
    page_input: VisualTextPageInput,
    clockwise_rotation_degrees: int,
) -> tuple[bytes, int, int]:
    if clockwise_rotation_degrees == 0:
        return (
            page_input.page_image_bytes,
            page_input.page_image_width_px,
            page_input.page_image_height_px,
        )
    try:
        with PILImage.open(BytesIO(page_input.page_image_bytes)) as image:
            if image.format != "PNG" or image.size != (
                page_input.page_image_width_px,
                page_input.page_image_height_px,
            ):
                raise ValueError("page image bytes do not match the declared PNG")
            image.load()
            rotated = image.transpose(
                _PIL_TRANSPOSE_BY_CLOCKWISE_ROTATION[clockwise_rotation_degrees]
            )
            output = BytesIO()
            rotated.save(output, format="PNG", optimize=False, compress_level=9)
            width, height = rotated.size
    except Exception as exc:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.INVALID_INPUT,
            "The rendered page cannot be deterministically rotated for Bailian.",
        ) from exc
    return output.getvalue(), width, height


def _provider_version(
    *,
    model: str,
    input_rotations_by_page: Mapping[int, int],
    timeout_seconds: float,
    max_retries: int,
) -> str:
    prompt_bundle_sha256 = hashlib.sha256(
        _PAGE_LAYOUT_PROMPT.encode("utf-8")
    ).hexdigest()
    rotation_json = json.dumps(
        [[page, rotation] for page, rotation in input_rotations_by_page.items()],
        separators=(",", ":"),
    )
    rotation_sha256 = hashlib.sha256(rotation_json.encode("ascii")).hexdigest()
    deadline_value = format(timeout_seconds, ".12g")
    value = (
        f"{BAILIAN_PAGE_LAYOUT_PROVIDER_IMPLEMENTATION_VERSION};m={model};"
        f"p={_PROMPT_VERSION};h={prompt_bundle_sha256[:16]};"
        f"r={rotation_sha256[:16]};x={_INPUT_ROTATION_TRANSFORM_VERSION};"
        f"c=n1k;px={_MODEL_MAX_PIXELS};o={_MAX_COMPLETION_TOKENS};"
        f"dl={deadline_value};tr={max_retries};cap={max_retries + 1};"
        "or=0;s=1;t=0"
    )
    if len(value) > 160:
        raise ValueError(
            "Bailian page-layout provider version exceeds its schema limit"
        )
    return value


__all__ = [
    "BAILIAN_PAGE_LAYOUT_PROVIDER_IMPLEMENTATION_VERSION",
    "DEFAULT_BAILIAN_FLASH_PAGE_LAYOUT_MODEL",
    "DEFAULT_BAILIAN_PAGE_LAYOUT_MODEL",
    "DEFAULT_BAILIAN_SCREENING_PAGE_LAYOUT_MODEL",
    "PAGE_LAYOUT_RESPONSE_SCHEMA_VERSION",
    "BailianPageLayoutProvider",
]
