from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.adapters.documents.bailian_visual_text_provider import (
    BAILIAN_VISUAL_TEXT_PROVIDER_ID,
    DEFAULT_BAILIAN_OCR_MODEL,
    DEFAULT_BAILIAN_OCR_SEED,
    DEFAULT_BAILIAN_OCR_TEMPERATURE,
)
from app.domain.extraction.visual_table import (
    VisualTableCell,
    VisualTableCropInput,
    VisualTableSnapshot,
    stable_visual_table_cell_id,
    stable_visual_table_snapshot_id,
)
from app.ports.visual_table_provider import (
    VisualTableProviderRejected,
    VisualTableProviderRejectionCode,
)


BAILIAN_VISUAL_TABLE_PROVIDER_IMPLEMENTATION_VERSION = "1.0.0"
BAILIAN_TABLE_PARSING_TASK = "table_parsing"
_TABLE_HTML_PARSER_VERSION = "strict-grid/1"
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_MAX_BASE64_CHARACTERS = 10 * 1024 * 1024
_MAX_IMAGE_PIXELS = 15_680_000
_MAX_RESPONSE_TEXT_CHARACTERS = 2_000_000
_MAX_RESPONSE_TEXT_BYTES = 4_000_000
# The JSON carrier can be larger than the decoded HTML because JSON escapes may
# expand one character to six bytes.  Bound the actual decoded HTTP body before
# materializing JSON while still leaving enough room for the text budget above.
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_TABLE_ROWS = 4_000
_MAX_TABLE_COLUMNS = 1_000
_MAX_TABLE_CELLS = 500_000
_MAX_SPAN = 4_000
_ALLOWED_DASHSCOPE_HOSTS = frozenset({"dashscope.aliyuncs.com"})


@dataclass(frozen=True, slots=True)
class _ParsedCell:
    row: int
    column: int
    row_span: int
    column_span: int
    raw_text: str


@dataclass(frozen=True, slots=True)
class _OpenCell:
    row_span: int
    column_span: int
    text_parts: list[str]


class _SingleTableHtmlParser(HTMLParser):
    """Small bounded parser for one model-produced HTML table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_count = 0
        self.in_table = False
        self.in_row = False
        self.current_cell: _OpenCell | None = None
        self.rows: list[list[tuple[int, int, str]]] = []
        self.current_row: list[tuple[int, int, str]] | None = None
        self.invalid = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.casefold()
        if tag == "table":
            self.table_count += 1
            if self.in_table or self.table_count > 1:
                self.invalid = True
            self.in_table = True
            return
        if not self.in_table:
            return
        if tag == "tr":
            if self.in_row or self.current_cell is not None:
                self.invalid = True
                return
            self.in_row = True
            self.current_row = []
            return
        if tag in {"td", "th"}:
            if not self.in_row or self.current_cell is not None:
                self.invalid = True
                return
            attributes: dict[str, str | None] = {}
            for name, value in attrs:
                normalized_name = name.casefold()
                if normalized_name in attributes:
                    self.invalid = True
                    return
                attributes[normalized_name] = value
            try:
                row_span = _parse_span(attributes.get("rowspan"))
                column_span = _parse_span(attributes.get("colspan"))
            except ValueError:
                self.invalid = True
                return
            self.current_cell = _OpenCell(
                row_span=row_span,
                column_span=column_span,
                text_parts=[],
            )
            return
        if tag == "br" and self.current_cell is not None:
            self.current_cell.text_parts.append("\n")
        elif tag in {"p", "div", "li"} and self.current_cell is not None:
            if self.current_cell.text_parts:
                self.current_cell.text_parts.append("\n")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"}:
            if self.current_cell is None or self.current_row is None:
                self.invalid = True
                return
            self.current_row.append(
                (
                    self.current_cell.row_span,
                    self.current_cell.column_span,
                    _normalize_cell_text("".join(self.current_cell.text_parts)),
                )
            )
            self.current_cell = None
            return
        if tag == "tr":
            if (
                not self.in_row
                or self.current_cell is not None
                or self.current_row is None
            ):
                self.invalid = True
                return
            if not self.current_row:
                self.invalid = True
            self.rows.append(self.current_row)
            self.current_row = None
            self.in_row = False
            return
        if tag == "table":
            if not self.in_table or self.in_row or self.current_cell is not None:
                self.invalid = True
            self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.text_parts.append(data)


class BailianVisualTableProvider:
    """Remote Bailian table parser for exact, annotation-free native crops."""

    provider_id = BAILIAN_VISUAL_TEXT_PROVIDER_ID

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = DEFAULT_BAILIAN_OCR_MODEL,
        seed: int = DEFAULT_BAILIAN_OCR_SEED,
        temperature: float = DEFAULT_BAILIAN_OCR_TEMPERATURE,
        timeout_seconds: float = 45.0,
        max_retries: int = 2,
        http_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_model = str(model or "").strip()
        if _MODEL_PATTERN.fullmatch(normalized_model) is None:
            raise ValueError("Bailian OCR model has an invalid identifier")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
            raise ValueError("Bailian OCR seed is outside the supported range")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("Bailian OCR temperature must be numeric")
        normalized_temperature = float(temperature)
        if (
            not math.isfinite(normalized_temperature)
            or not 0 <= normalized_temperature < 2
        ):
            raise ValueError("Bailian OCR temperature is outside the supported range")
        if isinstance(timeout_seconds, bool) or float(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(max_retries, bool) or not 0 <= int(max_retries) <= 5:
            raise ValueError("max_retries must be between zero and five")
        self._api_key = str(api_key or "").strip()
        self._endpoint = _native_multimodal_endpoint(base_url)
        self._model = normalized_model
        self._seed = seed
        self._temperature = normalized_temperature
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = int(max_retries)
        self._http_client = http_client
        self._sleep = sleep
        self.provider_version = (
            f"{BAILIAN_VISUAL_TABLE_PROVIDER_IMPLEMENTATION_VERSION}:"
            f"model={self._model}:task={BAILIAN_TABLE_PARSING_TASK}:"
            f"seed={self._seed}:temperature={self._temperature:g}:"
            f"rotate=false:parser={_TABLE_HTML_PARSER_VERSION}"
        )

    def parse_table(self, crop_input: VisualTableCropInput) -> VisualTableSnapshot:
        _validate_crop_input(crop_input)
        if not self._api_key or not self._api_key.isascii():
            raise VisualTableProviderRejected(
                VisualTableProviderRejectionCode.UNAVAILABLE,
                "Bailian visual table parsing is not configured.",
            )
        encoded_image = base64.b64encode(crop_input.crop_image_bytes).decode("ascii")
        if len(encoded_image) > _MAX_BASE64_CHARACTERS:
            raise VisualTableProviderRejected(
                VisualTableProviderRejectionCode.INVALID_INPUT,
                "The table crop exceeds the Bailian in-memory image limit.",
            )
        request_payload = {
            "model": self._model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "image": (
                                    f"data:{crop_input.crop_image_media_type};base64,"
                                    f"{encoded_image}"
                                ),
                                "min_pixels": 3072,
                                "max_pixels": 8_388_608,
                                "enable_rotate": False,
                            }
                        ],
                    }
                ]
            },
            "parameters": {
                "ocr_options": {"task": BAILIAN_TABLE_PARSING_TASK},
                "seed": self._seed,
                "temperature": self._temperature,
            },
        }
        response_body = self._post(request_payload)
        try:
            payload = _decode_response_json(response_body)
            html = _response_text(payload)
            parsed_cells, row_count, column_count = parse_single_html_table(html)
            snapshot_id = stable_visual_table_snapshot_id(
                source_sha256=crop_input.source_sha256,
                crop_image_sha256=crop_input.crop_image_sha256,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                page_number=crop_input.page_number,
                table_source_node_id=crop_input.table_source_node_id,
                table_id=crop_input.table_id,
                table_index=crop_input.table_index,
                table_bbox=crop_input.table_bbox,
                crop_image_width_px=crop_input.crop_image_width_px,
                crop_image_height_px=crop_input.crop_image_height_px,
            )
            cells = tuple(
                VisualTableCell(
                    visual_table_cell_id=stable_visual_table_cell_id(
                        visual_table_snapshot_id=snapshot_id,
                        row=cell.row,
                        column=cell.column,
                        row_span=cell.row_span,
                        column_span=cell.column_span,
                    ),
                    row=cell.row,
                    column=cell.column,
                    row_span=cell.row_span,
                    column_span=cell.column_span,
                    raw_text=cell.raw_text,
                )
                for cell in parsed_cells
            )
            return VisualTableSnapshot(
                visual_table_snapshot_id=snapshot_id,
                source_sha256=crop_input.source_sha256,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                page_number=crop_input.page_number,
                page_bbox=crop_input.page_bbox,
                table_source_node_id=crop_input.table_source_node_id,
                table_id=crop_input.table_id,
                table_index=crop_input.table_index,
                table_bbox=crop_input.table_bbox,
                crop_image_sha256=crop_input.crop_image_sha256,
                crop_image_width_px=crop_input.crop_image_width_px,
                crop_image_height_px=crop_input.crop_image_height_px,
                row_count=row_count,
                column_count=column_count,
                cells=cells,
            )
        except VisualTableProviderRejected:
            raise
        except Exception as exc:
            raise VisualTableProviderRejected(
                VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                "Bailian returned an invalid visual table response.",
            ) from exc

    def _post(self, request_payload: dict[str, object]) -> bytes:
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
                    stream = getattr(client, "stream", None)
                    if callable(stream):
                        with stream(
                            "POST",
                            self._endpoint,
                            headers=headers,
                            json=request_payload,
                            timeout=self._timeout_seconds,
                        ) as response:
                            status_code = int(getattr(response, "status_code", 0) or 0)
                            if status_code == 200:
                                return _bounded_streaming_response_body(response)
                            if status_code in {401, 403}:
                                raise VisualTableProviderRejected(
                                    VisualTableProviderRejectionCode.UNAVAILABLE,
                                    "Bailian visual table parsing is not authorized.",
                                )
                            retryable = status_code == 429 or 500 <= status_code <= 599
                    else:
                        response = client.post(
                            self._endpoint,
                            headers=headers,
                            json=request_payload,
                            timeout=self._timeout_seconds,
                        )
                        try:
                            status_code = int(getattr(response, "status_code", 0) or 0)
                            if status_code == 200:
                                return _bounded_buffered_response_body(response)
                            if status_code in {401, 403}:
                                raise VisualTableProviderRejected(
                                    VisualTableProviderRejectionCode.UNAVAILABLE,
                                    "Bailian visual table parsing is not authorized.",
                                )
                            retryable = status_code == 429 or 500 <= status_code <= 599
                        finally:
                            close = getattr(response, "close", None)
                            if callable(close):
                                close()
                except VisualTableProviderRejected:
                    raise
                except httpx.TransportError as exc:
                    if attempt < self._max_retries:
                        self._sleep(min(0.5 * (2**attempt), 2.0))
                        continue
                    raise VisualTableProviderRejected(
                        VisualTableProviderRejectionCode.PROVIDER_FAILED,
                        "Bailian visual table parsing failed.",
                    ) from exc
                except Exception as exc:
                    raise VisualTableProviderRejected(
                        VisualTableProviderRejectionCode.PROVIDER_FAILED,
                        "Bailian visual table parsing failed.",
                    ) from exc
                if retryable and attempt < self._max_retries:
                    self._sleep(min(0.5 * (2**attempt), 2.0))
                    continue
                raise VisualTableProviderRejected(
                    VisualTableProviderRejectionCode.PROVIDER_FAILED,
                    "Bailian visual table parsing failed.",
                )
        finally:
            if owned_client:
                client.close()


def _validate_response_content_length(response: Any) -> None:
    headers = getattr(response, "headers", None)
    content_length: object | None = None
    if headers is not None:
        try:
            content_length = headers.get("content-length")
        except Exception as exc:
            raise VisualTableProviderRejected(
                VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                "Bailian returned an invalid visual table response.",
            ) from exc
    if content_length is not None:
        try:
            declared_length = int(str(content_length).strip())
        except (TypeError, ValueError) as exc:
            raise VisualTableProviderRejected(
                VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                "Bailian returned an invalid visual table response.",
            ) from exc
        if declared_length < 0 or declared_length > _MAX_RESPONSE_BYTES:
            raise VisualTableProviderRejected(
                VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                "Bailian returned an oversized visual table response.",
            )


def _bounded_streaming_response_body(response: Any) -> bytes:
    """Read a provider stream up to the decoded-body byte budget."""

    _validate_response_content_length(response)
    body = bytearray()
    try:
        chunks = response.iter_bytes()
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("response chunk is not bytes")
            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise VisualTableProviderRejected(
                    VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                    "Bailian returned an oversized visual table response.",
                )
            body.extend(chunk)
    except VisualTableProviderRejected:
        raise
    except Exception as exc:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "Bailian returned an unreadable visual table response.",
        ) from exc
    return bytes(body)


def _bounded_buffered_response_body(response: Any) -> bytes:
    """Validate a bounded body from a simple injected test client."""

    _validate_response_content_length(response)
    try:
        body = response.content
    except Exception as exc:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "Bailian returned an unreadable visual table response.",
        ) from exc
    if not isinstance(body, bytes) or len(body) > _MAX_RESPONSE_BYTES:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "Bailian returned an oversized visual table response.",
        )
    return body


def _decode_response_json(body: bytes) -> object:
    """Decode one bounded JSON body without exposing provider text."""

    try:
        return json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "Bailian returned an invalid visual table response.",
        ) from exc


def parse_single_html_table(html: str) -> tuple[tuple[_ParsedCell, ...], int, int]:
    """Parse exactly one complete, rectangular HTML table without recovery."""

    if (
        not isinstance(html, str)
        or not html.strip()
        or len(html) > _MAX_RESPONSE_TEXT_CHARACTERS
        or len(html.encode("utf-8")) > _MAX_RESPONSE_TEXT_BYTES
    ):
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The visual table response did not contain bounded HTML text.",
        )
    parser = _SingleTableHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The visual table response contained malformed HTML.",
        ) from exc
    if parser.table_count > 1:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.MULTIPLE_TABLES,
            "The visual table response contained more than one table.",
        )
    if (
        parser.table_count != 1
        or parser.invalid
        or parser.in_table
        or parser.in_row
        or parser.current_cell is not None
        or not parser.rows
    ):
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The visual table response did not contain one closed HTML table.",
        )
    if len(parser.rows) > _MAX_TABLE_ROWS:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
            "The visual table response exceeded the row budget.",
        )

    occupied: dict[tuple[int, int], int] = {}
    parsed: list[_ParsedCell] = []
    max_column = 0
    covered_cell_count = 0
    for row_index, source_row in enumerate(parser.rows):
        column = 0
        for row_span, column_span, raw_text in source_row:
            while (row_index, column) in occupied:
                column += 1
            if column >= _MAX_TABLE_COLUMNS or len(parsed) >= _MAX_TABLE_CELLS:
                raise VisualTableProviderRejected(
                    VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
                    "The visual table response exceeded the cell budget.",
                )
            if row_index + row_span > len(parser.rows):
                raise VisualTableProviderRejected(
                    VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
                    "A visual table rowspan exceeded the returned row count.",
                )
            span_area = row_span * column_span
            if (
                column + column_span > _MAX_TABLE_COLUMNS
                or span_area > _MAX_TABLE_CELLS
                or covered_cell_count + span_area > _MAX_TABLE_CELLS
            ):
                raise VisualTableProviderRejected(
                    VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
                    "The visual table cell coverage exceeded the topology budget.",
                )
            coverage = {
                (row, covered_column)
                for row in range(row_index, row_index + row_span)
                for covered_column in range(column, column + column_span)
            }
            if any(position in occupied for position in coverage):
                raise VisualTableProviderRejected(
                    VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
                    "Visual table cell spans overlapped.",
                )
            cell_number = len(parsed)
            for position in coverage:
                occupied[position] = cell_number
            covered_cell_count += span_area
            parsed.append(
                _ParsedCell(
                    row=row_index,
                    column=column,
                    row_span=row_span,
                    column_span=column_span,
                    raw_text=raw_text,
                )
            )
            column += column_span
            max_column = max(max_column, column)
    if max_column < 1 or max_column > _MAX_TABLE_COLUMNS:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
            "The visual table response had invalid column dimensions.",
        )
    if len(parser.rows) * max_column > _MAX_TABLE_CELLS:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
            "The visual table grid exceeded the topology budget.",
        )
    expected = {
        (row, column) for row in range(len(parser.rows)) for column in range(max_column)
    }
    if set(occupied) != expected:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.TOPOLOGY_INVALID,
            "The visual table response did not form a complete rectangle.",
        )
    return tuple(parsed), len(parser.rows), max_column


def _response_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("response must be an object")
    output = payload.get("output")
    choices = output.get("choices") if isinstance(output, dict) else None
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("response must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise ValueError("response choice must finish with stop")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list) or len(content) != 1:
        raise ValueError("response must contain exactly one content item")
    item = content[0]
    text = item.get("text") if isinstance(item, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("response text is missing")
    return text


def _parse_span(value: str | None) -> int:
    if value is None:
        return 1
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ValueError("span must be a positive decimal integer")
    span = int(value)
    if span > _MAX_SPAN:
        raise ValueError("span exceeds the parser budget")
    return span


def _normalize_cell_text(value: str) -> str:
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _validate_crop_input(crop_input: VisualTableCropInput) -> None:
    if not isinstance(crop_input, VisualTableCropInput):
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.INVALID_INPUT,
            "The visual table input contract is invalid.",
        )
    if hashlib.sha256(crop_input.source_bytes).hexdigest() != crop_input.source_sha256:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.SOURCE_HASH_MISMATCH,
            "The visual table source hash does not match its bytes.",
        )
    if (
        hashlib.sha256(crop_input.crop_image_bytes).hexdigest()
        != crop_input.crop_image_sha256
    ):
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.CROP_IMAGE_HASH_MISMATCH,
            "The visual table crop hash does not match its bytes.",
        )
    width = crop_input.crop_image_width_px
    height = crop_input.crop_image_height_px
    if width <= 10 or height <= 10 or width * height > _MAX_IMAGE_PIXELS:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.INVALID_INPUT,
            "The table crop dimensions are outside Bailian OCR limits.",
        )


def _native_multimodal_endpoint(base_url: str) -> str:
    parsed = urlsplit(str(base_url or "").strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Bailian base_url has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname not in _ALLOWED_DASHSCOPE_HOSTS
        or port not in {None, 443}
    ):
        raise ValueError("Bailian base_url must use an approved DashScope HTTPS origin")
    return urlunsplit(
        (
            "https",
            parsed.netloc,
            "/api/v1/services/aigc/multimodal-generation/generation",
            "",
            "",
        )
    )


__all__ = [
    "BAILIAN_TABLE_PARSING_TASK",
    "BAILIAN_VISUAL_TABLE_PROVIDER_IMPLEMENTATION_VERSION",
    "BailianVisualTableProvider",
    "parse_single_html_table",
]
