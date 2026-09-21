"""Source-bound Qwen3.8-Max PDF parsing for one derived PDF page.

This adapter intentionally does not reuse the image-layout prompt.  It sends a
deterministic, one-page PDF through Bailian's Chat Completions ``file`` input
and accepts only a strict, page-grounded JSON contract.  Model-reported
geometry is validated; coordinates are never synthesized when they are absent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from io import BytesIO
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import fitz
import httpx
from PIL import Image as PILImage
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from reqpilotx_server.adapters.documents.visual_text_provider_support import (
    validate_visual_text_page_input,
)
from reqpilotx_server.domain.source_visual_text import (
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
from reqpilotx_server.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)


BAILIAN_QWEN38_PDF_PAGE_PROVIDER_ID = (
    "aliyun-bailian-qwen3.8-max-pdf-page-parsing"
)
BAILIAN_QWEN38_PDF_PAGE_PROVIDER_IMPLEMENTATION_VERSION = "1.1.0"
DEFAULT_BAILIAN_QWEN38_PDF_MODEL = "qwen3.8-max"
PDF_PAGE_PARSING_TASK = "pdf_understanding.single_page_parse"
PDF_PAGE_PARSING_RESPONSE_SCHEMA_VERSION = "pdf-page-parsing/v1"
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_DASHSCOPE_KEY_PATTERN = re.compile(r"^[!-~]{20,256}$")
_NORMALIZED_EXTENT = 1_000.0
_MAX_SOURCE_PDF_BYTES = 150 * 1024 * 1024
_MAX_DERIVED_PDF_BYTES = 32 * 1024 * 1024
_MAX_ENCODED_PDF_CHARACTERS = 48 * 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_BLOCKS = 2_000
_MAX_TEXT_CHARACTERS = 2_000_000
_MAX_COMPLETION_TOKENS = 32_768

_PDF_PAGE_PARSING_PROMPT = """\
你是单页 PDF 内容解析器。只依据随请求提供的这一页 PDF，返回符合给定 JSON Schema 的对象；不要解释，不要使用 Markdown。

目标是高保真恢复该页可读文字与版面结构，不进行需求判断。按阅读顺序识别 TEXT、TABLE、FIGURE、HEADER、FOOTER、WATERMARK 六类内容。

坐标规则：bbox 必须来自你实际解析到的该 PDF 页面位置，使用页面左上角为原点的 0..1000 归一化坐标 [x0,y0,x1,y1]。bbox 缺失或无法确定时不要猜测该块；不得用整页框代替未知坐标。每个 bbox 必须在页面内且有正面积。

内容规则：
1. TEXT 按自然段、标题、列表项或完整目录块输出；目录块 role=TOC，其他 role=null。
2. 页眉、页脚、水印分别标为 HEADER、FOOTER、WATERMARK，不并入正文。
3. 每个表格只输出一个 TABLE；表内文字不得再重复为 TEXT。table 必须是完整矩形网格，合并格只在锚点输出并用 span 表示，所有网格位置必须恰好覆盖一次。
4. FIGURE 的 text 只转录图中实际可见文字，不得编造图片语义。
5. 不得输出当前单页 PDF 之外的内容。空白页返回 blocks=[]；此时仍须声明已经评估全部六类。
"""


class _StrictResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _PdfCellResponse(_StrictResponseModel):
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(ge=1)
    column_span: StrictInt = Field(ge=1)
    text: StrictStr
    horizontal_alignment: Literal["LEFT", "CENTER", "RIGHT", "JUSTIFY"] | None
    vertical_alignment: Literal["TOP", "MIDDLE", "BOTTOM"] | None
    indent_level: StrictInt = Field(ge=0, le=20)


class _PdfTableResponse(_StrictResponseModel):
    row_count: StrictInt = Field(ge=1, le=4_000)
    column_count: StrictInt = Field(ge=1, le=1_000)
    header_row_count: StrictInt = Field(ge=0)
    starts_mid_table: StrictBool
    continues_next_page: StrictBool
    cells: list[_PdfCellResponse] = Field(min_length=1, max_length=500_000)


class _PdfBlockResponse(_StrictResponseModel):
    index: StrictInt = Field(ge=0)
    type: VisualContentType
    role: Literal["TOC"] | None
    bbox: list[StrictInt | float] = Field(min_length=4, max_length=4)
    text: StrictStr
    table: _PdfTableResponse | None


class _PdfPageResponse(_StrictResponseModel):
    schema_version: Literal["pdf-page-parsing/v1"]
    coordinate_space: Literal["NORMALIZED_1000_TOP_LEFT"]
    evaluated_types: list[VisualContentType]
    blocks: list[_PdfBlockResponse] = Field(max_length=_MAX_BLOCKS)


class _RequestDeadlineExceeded(Exception):
    pass


class BailianQwen38PdfPageParsingProvider:
    """Parse exactly one derived PDF page with Bailian qwen3.8-max."""

    provider_id = BAILIAN_QWEN38_PDF_PAGE_PROVIDER_ID

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = DEFAULT_BAILIAN_QWEN38_PDF_MODEL,
        timeout_seconds: float = 300.0,
        max_retries: int = 0,
        http_client: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized_model = str(model or "").strip()
        if _MODEL_PATTERN.fullmatch(normalized_model) is None:
            raise ValueError("Bailian PDF parsing model has an invalid identifier")
        if normalized_model != DEFAULT_BAILIAN_QWEN38_PDF_MODEL:
            raise ValueError("Bailian PDF parsing supports only qwen3.8-max")
        if isinstance(timeout_seconds, bool) or float(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(max_retries, bool) or int(max_retries) != 0:
            raise ValueError("Bailian PDF parsing retries must be zero")
        self._api_key = str(api_key or "").strip()
        self._endpoint = _chat_completions_endpoint(base_url)
        self._model = normalized_model
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = 0
        self._http_client = http_client
        self._monotonic = monotonic
        self.provider_version = _provider_version(model=self._model)

    def recognize_page(self, page_input: VisualTextPageInput) -> VisualTextSnapshot:
        validate_visual_text_page_input(page_input)
        if _DASHSCOPE_KEY_PATTERN.fullmatch(self._api_key) is None:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.UNAVAILABLE,
                "Bailian PDF page parsing is not configured.",
            )
        derived_pdf = derive_single_page_pdf(page_input)
        encoded_pdf = base64.b64encode(derived_pdf).decode("ascii")
        if len(encoded_pdf) > _MAX_ENCODED_PDF_CHARACTERS:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.INVALID_INPUT,
                "The encoded PDF page exceeds the in-memory request limit.",
            )
        filename = f"source-page-{page_input.page_number:04d}.pdf"
        payload = _request_payload(
            model=self._model,
            encoded_pdf=encoded_pdf,
            filename=filename,
        )
        response_payload = self._post(payload)
        try:
            parsed_json = _json_payload_from_completion(response_payload)
            parsed = _PdfPageResponse.model_validate(parsed_json)
            if not parsed.blocks and not _page_image_is_strictly_blank(page_input):
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                    "Bailian returned no blocks for a non-blank PDF page.",
                )
            return _snapshot_from_response(
                parsed,
                page_input=page_input,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
            )
        except VisualTextProviderRejected:
            raise
        except Exception as exc:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                "Bailian returned invalid PDF page parsing output.",
            ) from exc

    def _post(self, request_payload: dict[str, object]) -> object:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        owned_client = self._http_client is None
        client = self._http_client or httpx.Client(follow_redirects=False)
        try:
            deadline = self._monotonic() + self._timeout_seconds
            try:
                with client.stream(
                    "POST",
                    self._endpoint,
                    headers=headers,
                    json=request_payload,
                    timeout=self._timeout_seconds,
                ) as response:
                    status_code = int(getattr(response, "status_code", 0) or 0)
                    if status_code == 200:
                        return _completion_from_sse(
                            response,
                            deadline=deadline,
                            monotonic=self._monotonic,
                        )
                    raise _http_rejection(response, status_code)
            except VisualTextProviderRejected:
                raise
            except (_RequestDeadlineExceeded, httpx.TransportError) as exc:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_FAILED,
                    "Bailian PDF page parsing failed.",
                ) from exc
            except (UnicodeError, ValueError) as exc:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                    "Bailian returned an invalid PDF page parsing stream.",
                ) from exc
            except Exception as exc:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_FAILED,
                    "Bailian PDF page parsing failed.",
                ) from exc
        finally:
            if owned_client:
                client.close()


def derive_single_page_pdf(page_input: VisualTextPageInput) -> bytes:
    """Wrap the exact rendered PNG in a deterministic, raster-only PDF page."""

    validate_visual_text_page_input(page_input)
    if len(page_input.source_bytes) > _MAX_SOURCE_PDF_BYTES:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.INVALID_INPUT,
            "The source PDF exceeds the Bailian PDF parsing input limit.",
        )
    try:
        source = fitz.open(stream=page_input.source_bytes, filetype="pdf")
    except Exception as exc:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.INVALID_INPUT,
            "The approved source is not a readable PDF.",
        ) from exc
    try:
        if page_input.page_number > source.page_count:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.INVALID_INPUT,
                "The requested page is outside the source PDF.",
            )
        source_page = source.load_page(page_input.page_number - 1)
        source_bbox = tuple(float(value) for value in source_page.rect)
        if not _bbox_matches(source_bbox, page_input.page_bbox):
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.INVALID_INPUT,
                "The requested page geometry does not match the source PDF.",
            )
    finally:
        source.close()
    image_pixels = _validated_page_image_pixels(page_input)
    derived = fitz.open()
    try:
        width = page_input.page_bbox[2] - page_input.page_bbox[0]
        height = page_input.page_bbox[3] - page_input.page_bbox[1]
        page = derived.new_page(width=width, height=height)
        page.set_mediabox(fitz.Rect(page_input.page_bbox))
        page.insert_image(
            page.rect,
            stream=page_input.page_image_bytes,
            keep_proportion=False,
            overlay=True,
        )
        derived.set_metadata({})
        output = derived.tobytes(
            garbage=4,
            deflate=True,
            clean=True,
            no_new_id=True,
        )
    finally:
        derived.close()
    try:
        validation = fitz.open(stream=output, filetype="pdf")
        try:
            _validate_raster_only_pdf(
                validation,
                page_input=page_input,
                expected_pixels=image_pixels,
            )
        finally:
            validation.close()
    except Exception as exc:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.INVALID_INPUT,
            "The source page could not be isolated for PDF parsing.",
        ) from exc
    if len(output) > _MAX_DERIVED_PDF_BYTES:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.INVALID_INPUT,
            "The derived PDF page exceeds the bounded in-memory request limit.",
        )
    return bytes(output)


def _request_payload(*, model: str, encoded_pdf: str, filename: str) -> dict[str, object]:
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": _PDF_PAGE_PARSING_PROMPT,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "file",
                        "file": {
                            "file_data": (
                                "data:application/pdf;base64," + encoded_pdf
                            ),
                            "filename": filename,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "解析这一个 PDF 页面，并严格返回 "
                            "pdf-page-parsing/v1 JSON。"
                        ),
                    },
                ],
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "reqpilotx_pdf_page_parse",
                "strict": True,
                "schema": _PdfPageResponse.model_json_schema(),
            },
        },
        "max_completion_tokens": _MAX_COMPLETION_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def _snapshot_from_response(
    parsed: _PdfPageResponse,
    *,
    page_input: VisualTextPageInput,
    provider_id: str,
    provider_version: str,
) -> VisualTextSnapshot:
    if tuple(parsed.evaluated_types) != VISUAL_CONTENT_TYPES:
        raise ValueError("provider must evaluate all six page content types")
    if [block.index for block in parsed.blocks] != list(range(len(parsed.blocks))):
        raise ValueError("PDF parsing blocks must use sequential reading-order indexes")
    total_text = sum(
        len(block.text)
        + (sum(len(cell.text) for cell in block.table.cells) if block.table else 0)
        for block in parsed.blocks
    )
    if total_text > _MAX_TEXT_CHARACTERS:
        raise ValueError("PDF page parsing output exceeds the text budget")
    observations: list[VisualTextObservation] = []
    for block in parsed.blocks:
        if block.type == "TABLE" and block.table is None:
            raise ValueError("TABLE blocks require a complete table")
        if block.type != "TABLE" and block.table is not None:
            raise ValueError("only TABLE blocks may carry a table")
        if block.role is not None and block.type != "TEXT":
            raise ValueError("TOC role is valid only for TEXT")
        bbox = _normalized_bbox_to_page_bbox(block.bbox, page_input.page_bbox)
        layout_table = _table_from_response(block.table) if block.table else None
        observation_id = stable_visual_text_observation_id(
            source_sha256=page_input.source_sha256,
            page_image_sha256=page_input.page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_input.page_number,
            observation_index=block.index,
            bbox=bbox,
        )
        observations.append(
            VisualTextObservation(
                observation_id=observation_id,
                observation_index=block.index,
                source_sha256=page_input.source_sha256,
                page_number=page_input.page_number,
                bbox=bbox,
                raw_text=block.text,
                content_type=block.type,
                role=block.role,
                layout_table=layout_table,
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


def _table_from_response(value: _PdfTableResponse) -> VisualLayoutTable:
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


def _normalized_bbox_to_page_bbox(
    value: list[int | float],
    page_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if len(value) != 4 or any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in value
    ):
        raise ValueError("normalized bbox must contain four finite numbers")
    x0, y0, x1, y1 = (float(item) for item in value)
    if x0 < 0 or y0 < 0 or x1 > 1_000 or y1 > 1_000 or x1 <= x0 or y1 <= y0:
        raise ValueError("normalized bbox must have positive area inside the page")
    px0, py0, px1, py1 = page_bbox
    width = px1 - px0
    height = py1 - py0
    return (
        px0 + x0 / _NORMALIZED_EXTENT * width,
        py0 + y0 / _NORMALIZED_EXTENT * height,
        px0 + x1 / _NORMALIZED_EXTENT * width,
        py0 + y1 / _NORMALIZED_EXTENT * height,
    )


def _json_payload_from_completion(payload: object) -> object:
    if not isinstance(payload, dict):
        raise ValueError("completion response must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("completion must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise ValueError("completion did not finish normally")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("completion message is missing")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("completion content is missing")
    if len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise ValueError("completion content exceeds the response budget")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("completion JSON must be an object")
    return result


def _completion_from_sse(
    response: Any,
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> dict[str, object]:
    parts: list[str] = []
    response_bytes = 0
    finish_reason: str | None = None
    saw_done = False
    try:
        lines = response.iter_lines()
    except Exception as exc:
        raise ValueError("stream cannot be read") from exc
    for raw_line in lines:
        if monotonic() >= deadline:
            raise _RequestDeadlineExceeded
        if isinstance(raw_line, bytes):
            response_bytes += len(raw_line)
            line = raw_line.decode("utf-8")
        elif isinstance(raw_line, str):
            response_bytes += len(raw_line.encode("utf-8"))
            line = raw_line
        else:
            raise ValueError("stream line must be text")
        if response_bytes > _MAX_RESPONSE_BYTES:
            raise ValueError("stream exceeds the response budget")
        line = line.strip()
        if not line:
            continue
        if not line.startswith("data:"):
            raise ValueError("unsupported stream event")
        data = line[5:].strip()
        if data == "[DONE]":
            saw_done = True
            break
        chunk = json.loads(data)
        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        if not isinstance(choices, list):
            raise ValueError("stream choices are missing")
        if not choices:
            continue
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("stream must contain one choice")
        choice = choices[0]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ValueError("stream delta is missing")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise ValueError("stream content must be text")
            parts.append(content)
        candidate_finish = choice.get("finish_reason")
        if candidate_finish is not None:
            if candidate_finish in {"length", "max_tokens"}:
                raise ValueError("completion was truncated")
            if candidate_finish != "stop" or finish_reason is not None:
                raise ValueError("completion did not finish normally")
            finish_reason = candidate_finish
    if monotonic() >= deadline:
        raise _RequestDeadlineExceeded
    if not saw_done or finish_reason != "stop" or not parts:
        raise ValueError("stream ended without a complete answer")
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "".join(parts)},
            }
        ]
    }


def _http_rejection(response: Any, status_code: int) -> VisualTextProviderRejected:
    safe_error = _bounded_error_text(response)
    if status_code == 401:
        code = VisualTextProviderRejectionCode.AUTHENTICATION_FAILED
        message = "Bailian PDF page parsing authentication failed."
    elif status_code == 403 and any(
        token in safe_error
        for token in (
            "quota",
            "credit",
            "balance",
            "insufficient",
            "freetieronly",
            "free tier",
        )
    ):
        code = VisualTextProviderRejectionCode.QUOTA_EXHAUSTED
        message = "Bailian PDF page parsing quota is unavailable."
    elif status_code == 403:
        code = VisualTextProviderRejectionCode.ACCESS_DENIED
        message = "Bailian PDF page parsing access was denied."
    elif status_code == 429:
        code = VisualTextProviderRejectionCode.RATE_LIMITED
        message = "Bailian PDF page parsing was rate limited."
    elif status_code == 413:
        code = VisualTextProviderRejectionCode.INVALID_INPUT
        message = "The derived PDF page is too large for Bailian."
    else:
        code = VisualTextProviderRejectionCode.PROVIDER_FAILED
        message = "Bailian PDF page parsing failed."
    return VisualTextProviderRejected(code, message)


def _bounded_error_text(response: Any) -> str:
    """Read at most 8 KiB from a streamed error response for classification."""

    limit = 8_192
    try:
        iter_bytes = getattr(response, "iter_bytes", None)
        if callable(iter_bytes):
            parts: list[bytes] = []
            size = 0
            for raw_part in iter_bytes():
                if not isinstance(raw_part, (bytes, bytearray)):
                    break
                remaining = limit - size
                if remaining <= 0:
                    break
                part = bytes(raw_part[:remaining])
                parts.append(part)
                size += len(part)
            return b"".join(parts).decode("utf-8", errors="replace").casefold()
    except Exception:
        pass
    try:
        return json.dumps(response.json(), ensure_ascii=True).casefold()[:limit]
    except Exception:
        return ""


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


def _bbox_matches(
    actual: tuple[float, float, float, float],
    expected: tuple[float, float, float, float],
) -> bool:
    return all(abs(left - right) <= 1e-3 for left, right in zip(actual, expected))


def _page_image_is_strictly_blank(page_input: VisualTextPageInput) -> bool:
    """Permit VERIFIED_EMPTY only when the locally rendered pixels are blank."""

    try:
        with PILImage.open(BytesIO(page_input.page_image_bytes)) as image:
            if image.format != "PNG" or image.size != (
                page_input.page_image_width_px,
                page_input.page_image_height_px,
            ):
                return False
            grayscale = image.convert("L")
            minimum, maximum = grayscale.getextrema()
            return int(minimum) >= 250 and int(maximum) <= 255
    except Exception:
        return False


def _validated_page_image_pixels(page_input: VisualTextPageInput) -> bytes:
    try:
        with PILImage.open(BytesIO(page_input.page_image_bytes)) as image:
            if image.format != "PNG" or image.size != (
                page_input.page_image_width_px,
                page_input.page_image_height_px,
            ):
                raise ValueError("rendered PNG dimensions do not match its contract")
            image.load()
            return image.convert("RGBA").tobytes()
    except Exception as exc:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.INVALID_INPUT,
            "The rendered page PNG is invalid.",
        ) from exc


def _validate_raster_only_pdf(
    document: fitz.Document,
    *,
    page_input: VisualTextPageInput,
    expected_pixels: bytes,
) -> None:
    if document.page_count != 1:
        raise ValueError("derived PDF is not one page")
    page = document.load_page(0)
    if not _bbox_matches(tuple(float(value) for value in page.mediabox), page_input.page_bbox):
        raise ValueError("derived PDF MediaBox does not match the source page")
    expected_width = page_input.page_bbox[2] - page_input.page_bbox[0]
    expected_height = page_input.page_bbox[3] - page_input.page_bbox[1]
    if abs(page.rect.width - expected_width) > 1e-3 or abs(page.rect.height - expected_height) > 1e-3:
        raise ValueError("derived PDF page dimensions changed")
    if page.get_text("text").strip():
        raise ValueError("derived PDF must not contain a text layer")
    if page.get_links() or list(page.annots() or ()) or list(page.widgets() or ()):
        raise ValueError("derived PDF must not contain interactive page objects")
    if document.embfile_names():
        raise ValueError("derived PDF must not contain embedded files")
    images = page.get_images(full=True)
    if len(images) != 1:
        raise ValueError("derived PDF must contain exactly one page image")
    image_xref = int(images[0][0])
    placements = page.get_image_rects(image_xref)
    if len(placements) != 1 or not _bbox_matches(
        tuple(float(value) for value in placements[0]),
        tuple(float(value) for value in page.rect),
    ):
        raise ValueError("derived PDF image must cover the complete page exactly once")
    extracted = document.extract_image(image_xref)
    extracted_bytes = extracted.get("image") if isinstance(extracted, dict) else None
    if not isinstance(extracted_bytes, bytes):
        raise ValueError("derived PDF page image could not be verified")
    with PILImage.open(BytesIO(extracted_bytes)) as image:
        if image.size != (
            page_input.page_image_width_px,
            page_input.page_image_height_px,
        ) or image.convert("RGBA").tobytes() != expected_pixels:
            raise ValueError("derived PDF page image pixels changed")
    forbidden_catalog_tokens = (
        "/JavaScript",
        "/JS",
        "/EmbeddedFiles",
        "/Filespec",
        "/OpenAction",
        "/AA",
    )
    for xref in range(1, document.xref_length()):
        value = document.xref_object(xref, compressed=False)
        if any(token in value for token in forbidden_catalog_tokens):
            raise ValueError("derived PDF contains a prohibited active object")


def _provider_version(*, model: str) -> str:
    prompt_sha = hashlib.sha256(_PDF_PAGE_PARSING_PROMPT.encode("utf-8")).hexdigest()
    value = (
        f"{BAILIAN_QWEN38_PDF_PAGE_PROVIDER_IMPLEMENTATION_VERSION};"
        f"model={model};task={PDF_PAGE_PARSING_TASK};"
        f"schema=pdf1;derive=rp1;bbox=n1000;"
        f"prompt={prompt_sha[:16]};retry=0;stream=1"
    )
    if len(value) > 160:
        raise ValueError("Bailian PDF page provider version exceeds its schema limit")
    return value


__all__ = [
    "BAILIAN_QWEN38_PDF_PAGE_PROVIDER_ID",
    "BAILIAN_QWEN38_PDF_PAGE_PROVIDER_IMPLEMENTATION_VERSION",
    "BailianQwen38PdfPageParsingProvider",
    "DEFAULT_BAILIAN_QWEN38_PDF_MODEL",
    "PDF_PAGE_PARSING_RESPONSE_SCHEMA_VERSION",
    "PDF_PAGE_PARSING_TASK",
    "derive_single_page_pdf",
]
