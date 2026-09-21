"""Qwen3.5 OCR ``document_parsing`` as geometry-free auxiliary facts.

The built-in task emits LaTeX, not a grounded layout contract.  This adapter
therefore records only ordered HEADING/TEXT/TABLE/FORMULA hints.  Image or
figure placeholders are discarded and produce a content-free diagnostic that
requires a grounded complex-vision pass.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from reqpilotx_server.adapters.documents.visual_text_provider_support import (
    validate_visual_text_page_input,
)
from reqpilotx_server.domain.ocr_document_structure import (
    OCR_DOCUMENT_ADOPTABLE_CONTENT_TYPES,
    OCR_DOCUMENT_CONTENT_TYPES,
    OCR_DOCUMENT_STRUCTURE_TASK,
    OcrDocumentAdoptableContentType,
    OcrDocumentDiagnosticCode,
    OcrDocumentStructureBlock,
    OcrDocumentStructureCoverage,
    OcrDocumentStructureSnapshot,
    stable_ocr_document_structure_block_id,
    stable_ocr_document_structure_snapshot_id,
)
from reqpilotx_server.domain.source_visual_text import VisualTextPageInput
from reqpilotx_server.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)


BAILIAN_OCR_DOCUMENT_STRUCTURE_PROVIDER_ID = "aliyun-bailian-qwen-ocr-document-parsing"
BAILIAN_OCR_DOCUMENT_STRUCTURE_IMPLEMENTATION_VERSION = "1.0.0"
DEFAULT_BAILIAN_OCR_DOCUMENT_STRUCTURE_MODEL = "qwen3.5-ocr"
DEFAULT_BAILIAN_OCR_DOCUMENT_STRUCTURE_SEED = 1234
DEFAULT_BAILIAN_OCR_DOCUMENT_STRUCTURE_TEMPERATURE = 0.01

_ALLOWED_DASHSCOPE_HOSTS = frozenset({"dashscope.aliyuncs.com"})
_MAX_BASE64_CHARACTERS = 10 * 1024 * 1024
_MAX_IMAGE_PIXELS = 15_680_000
_MAX_LATEX_CHARACTERS = 2_000_000
_MAX_BLOCKS = 20_000
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_FENCED_LATEX_PATTERN = re.compile(
    r"\A\s*```(?:latex|tex)\s*\n(?P<body>[\s\S]*?)\n```\s*\Z",
    flags=re.IGNORECASE,
)
_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]\n]{0,2000}\]\([^\)\n]{0,4000}\)")
_HOUSEKEEPING_COMMAND_PATTERN = re.compile(
    r"\\(?:maketitle|newpage|clearpage|pagebreak|centering)\b(?:\s*\{\})?"
)
_HEADING_LEVELS = {
    "part": 1,
    "chapter": 1,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
    "subparagraph": 5,
}
_TABLE_ENVIRONMENTS = frozenset(
    {"table", "table*", "tabular", "tabular*", "tabularx", "longtable", "array"}
)
_FORMULA_ENVIRONMENTS = frozenset(
    {
        "equation",
        "equation*",
        "align",
        "align*",
        "gather",
        "gather*",
        "multline",
        "multline*",
        "displaymath",
        "math",
    }
)
_IMAGE_ENVIRONMENTS = ("figure", "figure*", "picture")


@dataclass(frozen=True, slots=True)
class _ParsedBlock:
    content_type: OcrDocumentAdoptableContentType
    latex: str
    text: str
    heading_level: int | None = None


class BailianOcrDocumentStructureProvider:
    """Parse one exact rendered page with the Qwen OCR document task."""

    provider_id = BAILIAN_OCR_DOCUMENT_STRUCTURE_PROVIDER_ID

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = DEFAULT_BAILIAN_OCR_DOCUMENT_STRUCTURE_MODEL,
        enable_rotate: bool = True,
        seed: int = DEFAULT_BAILIAN_OCR_DOCUMENT_STRUCTURE_SEED,
        temperature: float = DEFAULT_BAILIAN_OCR_DOCUMENT_STRUCTURE_TEMPERATURE,
        timeout_seconds: float = 90.0,
        execution_identity: str,
        http_client: Any | None = None,
    ) -> None:
        normalized_model = str(model or "").strip()
        if _MODEL_PATTERN.fullmatch(normalized_model) is None:
            raise ValueError("Bailian OCR document model has an invalid identifier")
        if normalized_model != DEFAULT_BAILIAN_OCR_DOCUMENT_STRUCTURE_MODEL:
            raise ValueError("Bailian OCR document parsing supports only qwen3.5-ocr")
        if not isinstance(enable_rotate, bool):
            raise TypeError("enable_rotate must be a boolean")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
            raise ValueError("seed must be an integer between zero and 2^31 - 1")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("temperature must be numeric")
        normalized_temperature = float(temperature)
        if not math.isfinite(normalized_temperature) or not (
            0.0 <= normalized_temperature < 2.0
        ):
            raise ValueError("temperature must be between zero and two")
        if normalized_temperature == 0.0:
            normalized_temperature = 0.0
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0.0
        ):
            raise ValueError("timeout_seconds must be positive")
        normalized_execution_identity = str(execution_identity or "").strip()
        if (
            not normalized_execution_identity
            or len(normalized_execution_identity) > 512
        ):
            raise ValueError("execution_identity must be non-blank and bounded")

        self._api_key = str(api_key or "").strip()
        self._endpoint = _native_multimodal_endpoint(base_url)
        self._model = normalized_model
        self._enable_rotate = enable_rotate
        self._seed = seed
        self._temperature = normalized_temperature
        self._timeout_seconds = float(timeout_seconds)
        self._http_client = http_client
        self.execution_identity_sha256 = hashlib.sha256(
            normalized_execution_identity.encode("utf-8")
        ).hexdigest()
        self.provider_version = _provider_version(
            model=self._model,
            enable_rotate=self._enable_rotate,
            seed=self._seed,
            temperature=self._temperature,
            execution_identity_sha256=self.execution_identity_sha256,
        )

    def parse_page(
        self,
        page_input: VisualTextPageInput,
    ) -> OcrDocumentStructureSnapshot:
        validate_visual_text_page_input(page_input)
        if not self._api_key or not self._api_key.isascii():
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.UNAVAILABLE,
                "Bailian OCR document parsing is not configured.",
            )
        _validate_image_limits(page_input)
        encoded_image = base64.b64encode(page_input.page_image_bytes).decode("ascii")
        if len(encoded_image) > _MAX_BASE64_CHARACTERS:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.INVALID_INPUT,
                "The rendered page exceeds the Bailian in-memory image limit.",
            )
        payload = {
            "model": self._model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "image": (
                                    f"data:{page_input.page_image_media_type};base64,"
                                    f"{encoded_image}"
                                ),
                                "min_pixels": 3072,
                                "max_pixels": 8_388_608,
                                "enable_rotate": self._enable_rotate,
                            }
                        ],
                    }
                ]
            },
            "parameters": {
                "ocr_options": {"task": OCR_DOCUMENT_STRUCTURE_TASK},
                "seed": self._seed,
                "temperature": self._temperature,
            },
        }
        response = self._post(payload)
        try:
            response_payload = response.json()
            latex = _document_latex_from_response(response_payload)
            return parse_qwen_document_latex(
                latex,
                page_input=page_input,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
            )
        except VisualTextProviderRejected:
            raise
        except Exception as exc:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                "Bailian returned invalid OCR document parsing output.",
            ) from exc

    def _post(self, payload: dict[str, object]) -> Any:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        owned_client = self._http_client is None
        client = self._http_client or httpx.Client(follow_redirects=False)
        try:
            try:
                response = client.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self._timeout_seconds,
                )
            except httpx.TransportError as exc:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_FAILED,
                    "Bailian OCR document parsing failed.",
                ) from exc
            except Exception as exc:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_FAILED,
                    "Bailian OCR document parsing failed.",
                ) from exc
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code == 200:
                return response
            code = _http_rejection_code(status_code)
            raise VisualTextProviderRejected(
                code,
                "Bailian OCR document parsing request was rejected.",
            )
        finally:
            if owned_client:
                client.close()


def parse_qwen_document_latex(
    latex: str,
    *,
    page_input: VisualTextPageInput,
    provider_id: str,
    provider_version: str,
) -> OcrDocumentStructureSnapshot:
    """Convert one official LaTeX result without inventing any geometry."""

    validate_visual_text_page_input(page_input)
    normalized = _normalize_latex(latex)
    document_latex_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    parse_input = _strip_latex_comments(normalized)
    parse_input, image_placeholder_seen = _strip_image_placeholders(parse_input)
    preamble, body = _document_parts(parse_input)
    parsed_blocks: list[_ParsedBlock] = []
    title = _command_argument(preamble, "title")
    if title is not None and title.strip():
        parsed_blocks.append(
            _ParsedBlock(
                content_type="HEADING",
                heading_level=1,
                latex=title.strip(),
                text=_compact_text(title),
            )
        )
    parsed_blocks.extend(_parse_body_blocks(body))
    if len(parsed_blocks) > _MAX_BLOCKS:
        raise ValueError("document parsing block limit exceeded")

    blocks: list[OcrDocumentStructureBlock] = []
    for source_order, parsed in enumerate(parsed_blocks):
        block_id = stable_ocr_document_structure_block_id(
            source_sha256=page_input.source_sha256,
            page_image_sha256=page_input.page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_input.page_number,
            source_order=source_order,
            content_type=parsed.content_type,
            latex=parsed.latex,
        )
        blocks.append(
            OcrDocumentStructureBlock(
                block_id=block_id,
                source_order=source_order,
                content_type=parsed.content_type,
                heading_level=parsed.heading_level,
                latex=parsed.latex,
                text=parsed.text,
                source_sha256=page_input.source_sha256,
                page_number=page_input.page_number,
                page_image_sha256=page_input.page_image_sha256,
                provider_id=provider_id,
                provider_version=provider_version,
            )
        )
    counts = {
        content_type: sum(block.content_type == content_type for block in blocks)
        for content_type in OCR_DOCUMENT_ADOPTABLE_CONTENT_TYPES
    }
    coverage = tuple(
        _coverage(content_type=content_type, block_count=counts.get(content_type, 0))
        for content_type in OCR_DOCUMENT_CONTENT_TYPES
    )
    diagnostics: tuple[OcrDocumentDiagnosticCode, ...] = (
        ("IMAGE_PLACEHOLDER_IGNORED_REQUIRES_COMPLEX_VISION",)
        if image_placeholder_seen
        else ()
    )
    snapshot_id = stable_ocr_document_structure_snapshot_id(
        source_sha256=page_input.source_sha256,
        page_image_sha256=page_input.page_image_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_input.page_number,
        page_bbox=page_input.page_bbox,
        document_latex_sha256=document_latex_sha256,
    )
    return OcrDocumentStructureSnapshot(
        snapshot_id=snapshot_id,
        source_sha256=page_input.source_sha256,
        page_number=page_input.page_number,
        page_bbox=page_input.page_bbox,
        page_image_sha256=page_input.page_image_sha256,
        page_image_media_type=page_input.page_image_media_type,
        page_image_width_px=page_input.page_image_width_px,
        page_image_height_px=page_input.page_image_height_px,
        provider_id=provider_id,
        provider_version=provider_version,
        document_latex_sha256=document_latex_sha256,
        coverage=coverage,
        blocks=tuple(blocks),
        diagnostics=diagnostics,
    )


def _coverage(
    *,
    content_type: str,
    block_count: int,
) -> OcrDocumentStructureCoverage:
    if content_type in OCR_DOCUMENT_ADOPTABLE_CONTENT_TYPES:
        return OcrDocumentStructureCoverage(
            content_type=content_type,
            status="CAPTURED" if block_count else "NOT_DETECTED",
            block_count=block_count,
            next_action="NONE",
        )
    return OcrDocumentStructureCoverage(
        content_type=content_type,
        status="NOT_EVALUATED",
        block_count=0,
        next_action=(
            "COMPLEX_VISION"
            if content_type in {"FIGURE", "IMAGE"}
            else "GROUNDED_LAYOUT"
        ),
    )


def _normalize_latex(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("document parsing output must be text")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in normalized or len(normalized) > _MAX_LATEX_CHARACTERS:
        raise ValueError("document parsing output is outside bounded text limits")
    fenced = _FENCED_LATEX_PATTERN.fullmatch(normalized)
    if fenced is not None:
        normalized = fenced.group("body")
    elif "```" in normalized:
        raise ValueError("document parsing output has an invalid code fence")
    normalized = normalized.strip()
    if not normalized:
        raise ValueError("document parsing output must not be blank")
    return normalized


def _document_latex_from_response(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("response must be an object")
    output = payload.get("output")
    if not isinstance(output, dict):
        raise ValueError("response output is missing")
    choices = output.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("response must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise ValueError("document parsing response did not finish completely")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        raise ValueError("response content is missing")
    candidates: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            candidates.append(text)
        ocr_result = part.get("ocr_result")
        if isinstance(ocr_result, str) and ocr_result.strip():
            candidates.append(ocr_result)
        elif isinstance(ocr_result, dict):
            result_text = ocr_result.get("text")
            if isinstance(result_text, str) and result_text.strip():
                candidates.append(result_text)
    if len(candidates) != 1:
        raise ValueError("response must contain exactly one document parsing result")
    return candidates[0]


def _document_parts(value: str) -> tuple[str, str]:
    begin_token = r"\begin{document}"
    end_token = r"\end{document}"
    begin = value.find(begin_token)
    end = value.find(end_token)
    if begin < 0 and end < 0:
        return "", value
    if begin < 0 or end < begin or value.find(begin_token, begin + 1) >= 0:
        raise ValueError("document parsing output has invalid document boundaries")
    if value.find(end_token, end + 1) >= 0:
        raise ValueError("document parsing output has duplicate document boundaries")
    if value[end + len(end_token) :].strip():
        raise ValueError("document parsing output has trailing content")
    return value[:begin], value[begin + len(begin_token) : end]


def _strip_latex_comments(value: str) -> str:
    lines: list[str] = []
    for line in value.split("\n"):
        cut = len(line)
        for index, character in enumerate(line):
            if character == "%" and not _is_escaped(line, index):
                cut = index
                break
        lines.append(line[:cut])
    return "\n".join(lines)


def _strip_image_placeholders(value: str) -> tuple[str, bool]:
    result = value
    seen = False
    for environment in _IMAGE_ENVIRONMENTS:
        while True:
            start = result.find(rf"\begin{{{environment}}}")
            if start < 0:
                break
            end = _matching_environment_end(result, start, environment)
            result = result[:start] + "\n" + result[end:]
            seen = True

    while True:
        start = result.find(r"\includegraphics")
        if start < 0:
            break
        end = _command_with_optional_and_required_argument_end(
            result,
            start,
            "includegraphics",
        )
        result = result[:start] + result[end:]
        seen = True
    result, markdown_count = _MARKDOWN_IMAGE_PATTERN.subn("", result)
    return result, seen or bool(markdown_count)


def _parse_body_blocks(body: str) -> list[_ParsedBlock]:
    blocks: list[_ParsedBlock] = []
    cursor = 0
    text_start = 0
    while cursor < len(body):
        special = _special_at(body, cursor)
        if special is None:
            cursor += 1
            continue
        _append_text_fragments(blocks, body[text_start:cursor])
        kind, end, content, level = special
        fragment = body[cursor:end].strip()
        if kind == "HEADING":
            heading_text = _compact_text(content)
            if heading_text:
                blocks.append(
                    _ParsedBlock(
                        content_type="HEADING",
                        heading_level=level,
                        latex=content.strip(),
                        text=heading_text,
                    )
                )
        elif kind == "TABLE":
            blocks.append(
                _ParsedBlock(
                    content_type="TABLE",
                    latex=fragment,
                    text="",
                )
            )
        else:
            blocks.append(
                _ParsedBlock(
                    content_type="FORMULA",
                    latex=fragment,
                    text=content.strip(),
                )
            )
        cursor = end
        text_start = end
    _append_text_fragments(blocks, body[text_start:])
    return blocks


def _special_at(
    value: str,
    start: int,
) -> tuple[Literal["HEADING", "TABLE", "FORMULA"], int, str, int | None] | None:
    if value[start] == "\\":
        for command, level in _HEADING_LEVELS.items():
            token = rf"\{command}"
            if value.startswith(token, start):
                parsed = _command_argument_span(value, start, command)
                if parsed is not None:
                    end, content = parsed
                    return "HEADING", end, content, level
        if value.startswith(r"\begin{", start):
            close = value.find("}", start + len(r"\begin{"))
            if close >= 0:
                environment = value[start + len(r"\begin{") : close]
                if environment in _TABLE_ENVIRONMENTS | _FORMULA_ENVIRONMENTS:
                    end = _matching_environment_end(value, start, environment)
                    kind: Literal["TABLE", "FORMULA"] = (
                        "TABLE" if environment in _TABLE_ENVIRONMENTS else "FORMULA"
                    )
                    content_start = close + 1
                    content_end = end - len(rf"\end{{{environment}}}")
                    return kind, end, value[content_start:content_end], None
        if value.startswith(r"\[", start):
            end_marker = value.find(r"\]", start + 2)
            if end_marker < 0:
                raise ValueError("document parsing formula is unterminated")
            return "FORMULA", end_marker + 2, value[start + 2 : end_marker], None
    if value.startswith("$$", start) and not _is_escaped(value, start):
        end_marker = value.find("$$", start + 2)
        if end_marker < 0:
            raise ValueError("document parsing formula is unterminated")
        return "FORMULA", end_marker + 2, value[start + 2 : end_marker], None
    return None


def _append_text_fragments(blocks: list[_ParsedBlock], fragment: str) -> None:
    cleaned = _HOUSEKEEPING_COMMAND_PATTERN.sub("", fragment)
    for paragraph in re.split(r"\n\s*\n+", cleaned):
        latex = paragraph.strip()
        if not latex:
            continue
        text = _compact_text(latex)
        if not text or re.fullmatch(r"(?:\\[A-Za-z@]+\s*)+", text):
            continue
        blocks.append(
            _ParsedBlock(
                content_type="TEXT",
                latex=latex,
                text=text,
            )
        )


def _command_argument(value: str, command: str) -> str | None:
    start = value.find(rf"\{command}")
    if start < 0:
        return None
    parsed = _command_argument_span(value, start, command)
    if parsed is None:
        raise ValueError(f"document parsing {command} command is invalid")
    _end, content = parsed
    return content


def _command_argument_span(
    value: str,
    start: int,
    command: str,
) -> tuple[int, str] | None:
    token = rf"\{command}"
    if not value.startswith(token, start):
        return None
    cursor = start + len(token)
    if cursor < len(value) and (value[cursor].isalpha() or value[cursor] == "@"):
        return None
    if cursor < len(value) and value[cursor] == "*":
        cursor += 1
    cursor = _skip_space(value, cursor)
    if cursor < len(value) and value[cursor] == "[":
        cursor, _unused = _balanced_content(value, cursor, "[", "]")
        cursor = _skip_space(value, cursor)
    if cursor >= len(value) or value[cursor] != "{":
        return None
    return _balanced_content(value, cursor, "{", "}")


def _command_with_optional_and_required_argument_end(
    value: str,
    start: int,
    command: str,
) -> int:
    token = rf"\{command}"
    cursor = start + len(token)
    if not value.startswith(token, start):
        raise ValueError("invalid document parsing command")
    if cursor < len(value) and value[cursor] == "*":
        cursor += 1
    cursor = _skip_space(value, cursor)
    if cursor < len(value) and value[cursor] == "[":
        cursor, _unused = _balanced_content(value, cursor, "[", "]")
        cursor = _skip_space(value, cursor)
    if cursor >= len(value) or value[cursor] != "{":
        raise ValueError("image placeholder has no bounded source argument")
    cursor, _unused = _balanced_content(value, cursor, "{", "}")
    return cursor


def _balanced_content(
    value: str,
    start: int,
    opening: str,
    closing: str,
) -> tuple[int, str]:
    if start >= len(value) or value[start] != opening:
        raise ValueError("balanced document parsing argument is missing")
    depth = 1
    cursor = start + 1
    while cursor < len(value):
        character = value[cursor]
        if character == opening and not _is_escaped(value, cursor):
            depth += 1
        elif character == closing and not _is_escaped(value, cursor):
            depth -= 1
            if depth == 0:
                return cursor + 1, value[start + 1 : cursor]
        cursor += 1
    raise ValueError("document parsing argument is unterminated")


def _matching_environment_end(value: str, start: int, environment: str) -> int:
    begin_token = rf"\begin{{{environment}}}"
    end_token = rf"\end{{{environment}}}"
    if not value.startswith(begin_token, start):
        raise ValueError("document parsing environment start is invalid")
    depth = 1
    cursor = start + len(begin_token)
    while cursor < len(value):
        next_begin = value.find(begin_token, cursor)
        next_end = value.find(end_token, cursor)
        if next_end < 0:
            raise ValueError("document parsing environment is unterminated")
        if 0 <= next_begin < next_end:
            depth += 1
            cursor = next_begin + len(begin_token)
            continue
        depth -= 1
        cursor = next_end + len(end_token)
        if depth == 0:
            return cursor
    raise ValueError("document parsing environment is unterminated")


def _compact_text(value: str) -> str:
    return " ".join(value.split())


def _skip_space(value: str, cursor: int) -> int:
    while cursor < len(value) and value[cursor].isspace():
        cursor += 1
    return cursor


def _is_escaped(value: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _provider_version(
    *,
    model: str,
    enable_rotate: bool,
    seed: int,
    temperature: float,
    execution_identity_sha256: str,
) -> str:
    return (
        f"{BAILIAN_OCR_DOCUMENT_STRUCTURE_IMPLEMENTATION_VERSION};"
        f"model={model};task={OCR_DOCUMENT_STRUCTURE_TASK};"
        f"rotate={int(enable_rotate)};s={seed};t={format(temperature, '.17g')};"
        f"exec={execution_identity_sha256[:24]};"
        "geometry=none;parser=latex-blocks-v1"
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
            "The rendered page dimensions are outside Bailian OCR limits.",
        )


def _http_rejection_code(status_code: int) -> VisualTextProviderRejectionCode:
    if status_code == 401:
        return VisualTextProviderRejectionCode.AUTHENTICATION_FAILED
    if status_code == 403:
        return VisualTextProviderRejectionCode.ACCESS_DENIED
    if status_code == 413:
        return VisualTextProviderRejectionCode.INVALID_INPUT
    if status_code == 429:
        return VisualTextProviderRejectionCode.RATE_LIMITED
    return VisualTextProviderRejectionCode.PROVIDER_FAILED


__all__ = [
    "BAILIAN_OCR_DOCUMENT_STRUCTURE_PROVIDER_ID",
    "DEFAULT_BAILIAN_OCR_DOCUMENT_STRUCTURE_MODEL",
    "BailianOcrDocumentStructureProvider",
    "parse_qwen_document_latex",
]
