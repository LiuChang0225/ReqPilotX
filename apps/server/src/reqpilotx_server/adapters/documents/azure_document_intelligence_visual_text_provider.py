from __future__ import annotations

import base64
import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

import httpx

from reqpilotx_server.domain.source_visual_text import (
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


AZURE_DOCUMENT_INTELLIGENCE_API_VERSION = "2024-11-30"
AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID = "prebuilt-read"
AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_ID = (
    "azure-document-intelligence"
)
AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_VERSION = (
    "1.0.0+api-2024-11-30"
)

_RUNNING_STATUSES = frozenset({"notstarted", "running"})
_FAILED_STATUSES = frozenset({"failed", "canceled", "cancelled"})
_MAX_REQUEST_TIMEOUT_SECONDS = 120.0
_MAX_POLL_TIMEOUT_SECONDS = 600.0
_MAX_POLL_ATTEMPTS = 1_200


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


@dataclass(frozen=True, slots=True)
class _AzureLine:
    raw_text: str
    bbox: tuple[float, float, float, float]
    confidence: float
    span_start: int


@dataclass(frozen=True, slots=True)
class _AzureWordEvidence:
    span_start: int
    span_end: int
    confidence: float


class AzureDocumentIntelligenceVisualTextProvider:
    """Remote-only visual text provider backed by Azure prebuilt-read.

    The provider submits only the in-memory PNG rendering.  It never writes the
    page image, changes native SourceIR text, or falls back to another OCR
    implementation.  Remote error details and response text are intentionally
    excluded from rejection messages.
    """

    provider_id = AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_ID
    provider_version = AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_VERSION

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        http_client: _HttpClient | None = None,
        request_timeout_seconds: float = 30.0,
        poll_timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.5,
        max_poll_attempts: int = 240,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._endpoint = _normalize_endpoint(endpoint)
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self._http_client = http_client
        self._request_timeout_seconds = _bounded_positive_float(
            request_timeout_seconds,
            "request_timeout_seconds",
            maximum=_MAX_REQUEST_TIMEOUT_SECONDS,
        )
        self._poll_timeout_seconds = _bounded_positive_float(
            poll_timeout_seconds,
            "poll_timeout_seconds",
            maximum=_MAX_POLL_TIMEOUT_SECONDS,
        )
        self._poll_interval_seconds = _bounded_non_negative_float(
            poll_interval_seconds,
            "poll_interval_seconds",
            maximum=_MAX_POLL_TIMEOUT_SECONDS,
        )
        if (
            isinstance(max_poll_attempts, bool)
            or not isinstance(max_poll_attempts, int)
            or not 1 <= max_poll_attempts <= _MAX_POLL_ATTEMPTS
        ):
            raise ValueError(
                f"max_poll_attempts must be an integer in [1, {_MAX_POLL_ATTEMPTS}]"
            )
        if not callable(monotonic) or not callable(sleep):
            raise TypeError("monotonic and sleep must be callable")
        self._max_poll_attempts = max_poll_attempts
        self._monotonic = monotonic
        self._sleep = sleep

    def recognize_page(self, page_input: VisualTextPageInput) -> VisualTextSnapshot:
        if not isinstance(page_input, VisualTextPageInput):
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.INVALID_INPUT,
                "Azure visual text recognition requires a validated page input.",
            )
        _validate_input_hashes(page_input)
        if self._endpoint is None or not self._api_key:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.UNAVAILABLE,
                "Azure Document Intelligence configuration is unavailable.",
            )

        if self._http_client is not None:
            return self._recognize_with_client(self._http_client, page_input)
        try:
            with httpx.Client() as client:
                return self._recognize_with_client(client, page_input)
        except VisualTextProviderRejected:
            raise
        except Exception as exc:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.PROVIDER_FAILED,
                "Azure Document Intelligence failed without producing trusted facts.",
            ) from exc

    def _recognize_with_client(
        self,
        client: _HttpClient,
        page_input: VisualTextPageInput,
    ) -> VisualTextSnapshot:
        assert self._endpoint is not None
        analyze_url = (
            f"{self._endpoint}/documentintelligence/documentModels/"
            f"{AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID}:analyze"
        )
        headers = {
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": self._api_key,
        }
        request_body = {
            "base64Source": base64.b64encode(page_input.page_image_bytes).decode(
                "ascii"
            )
        }
        try:
            response = client.post(
                analyze_url,
                params={
                    "_overload": "analyzeDocument",
                    "api-version": AZURE_DOCUMENT_INTELLIGENCE_API_VERSION,
                },
                headers=headers,
                json=request_body,
                timeout=self._request_timeout_seconds,
            )
        except Exception as exc:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.PROVIDER_FAILED,
                "The Azure analyze request failed without producing trusted facts.",
            ) from exc

        status_code = _response_status_code(response)
        if status_code in {401, 403}:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.UNAVAILABLE,
                "Azure Document Intelligence authorization is unavailable.",
            )
        if status_code != 202:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.PROVIDER_FAILED,
                "The Azure analyze request was not accepted.",
            )
        operation_location = _operation_location(response)
        _validate_operation_location(operation_location, self._endpoint)

        payload = self._poll_operation(client, operation_location)
        try:
            lines = _parse_succeeded_result(payload, page_input)
            observations = tuple(
                _observation_from_line(
                    line=line,
                    observation_index=index,
                    page_input=page_input,
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                )
                for index, line in enumerate(lines)
            )
            snapshot_id = stable_visual_text_snapshot_id(
                source_sha256=page_input.source_sha256,
                page_image_sha256=page_input.page_image_sha256,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                page_number=page_input.page_number,
                page_bbox=page_input.page_bbox,
                page_image_width_px=page_input.page_image_width_px,
                page_image_height_px=page_input.page_image_height_px,
            )
            return VisualTextSnapshot(
                visual_text_snapshot_id=snapshot_id,
                source_sha256=page_input.source_sha256,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                page_number=page_input.page_number,
                page_bbox=page_input.page_bbox,
                page_image_sha256=page_input.page_image_sha256,
                page_image_media_type=page_input.page_image_media_type,
                page_image_width_px=page_input.page_image_width_px,
                page_image_height_px=page_input.page_image_height_px,
                observations=observations,
            )
        except VisualTextProviderRejected:
            raise
        except Exception as exc:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                "The Azure result could not be grounded to the source page.",
            ) from exc

    def _poll_operation(
        self,
        client: _HttpClient,
        operation_location: str,
    ) -> Mapping[str, Any]:
        deadline = self._monotonic() + self._poll_timeout_seconds
        headers = {"Ocp-Apim-Subscription-Key": self._api_key}
        for attempt in range(self._max_poll_attempts):
            remaining = deadline - self._monotonic()
            if remaining <= 0.0:
                break
            try:
                response = client.get(
                    operation_location,
                    headers=headers,
                    timeout=min(self._request_timeout_seconds, remaining),
                )
            except Exception as exc:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_FAILED,
                    "The Azure result request failed without producing trusted facts.",
                ) from exc
            if _response_status_code(response) != 200:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_FAILED,
                    "The Azure result request did not complete successfully.",
                )
            payload = _response_json_object(response)
            status = payload.get("status")
            if not isinstance(status, str) or not status.strip():
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                    "The Azure result status is invalid.",
                )
            normalized_status = status.strip().lower()
            if normalized_status == "succeeded":
                return payload
            if normalized_status in _FAILED_STATUSES:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_FAILED,
                    "Azure Document Intelligence did not produce a successful result.",
                )
            if normalized_status not in _RUNNING_STATUSES:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                    "The Azure result status is unsupported.",
                )
            if attempt + 1 >= self._max_poll_attempts:
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0.0:
                break
            self._sleep(min(self._poll_interval_seconds, remaining))

        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_FAILED,
            "Azure Document Intelligence did not finish within the polling bound.",
        )


def _validate_input_hashes(page_input: VisualTextPageInput) -> None:
    if hashlib.sha256(page_input.source_bytes).hexdigest() != page_input.source_sha256:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.SOURCE_HASH_MISMATCH,
            "Azure visual text recognition rejected a stale source identity.",
        )
    if (
        hashlib.sha256(page_input.page_image_bytes).hexdigest()
        != page_input.page_image_sha256
    ):
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PAGE_IMAGE_HASH_MISMATCH,
            "Azure visual text recognition rejected a stale page-image identity.",
        )


def _normalize_endpoint(endpoint: object) -> str | None:
    if not isinstance(endpoint, str) or not endpoint.strip():
        return None
    candidate = endpoint.strip().rstrip("/")
    parsed = urlparse(candidate)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.params
        or port is not None and not 1 <= port <= 65535
    ):
        return None
    return candidate


def _validate_operation_location(operation_location: str, endpoint: str) -> None:
    operation = urlparse(operation_location)
    base = urlparse(endpoint)
    try:
        operation_port = operation.port or 443
        base_port = base.port or 443
    except ValueError as exc:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The Azure operation location is invalid.",
        ) from exc
    expected_prefix = (
        f"{base.path.rstrip('/')}/documentintelligence/documentModels/"
    )
    if (
        operation.scheme.lower() != "https"
        or operation.hostname is None
        or operation.hostname.lower() != (base.hostname or "").lower()
        or operation_port != base_port
        or operation.username is not None
        or operation.password is not None
        or operation.fragment
        or not operation.path.startswith(expected_prefix)
        or "/analyzeResults/" not in operation.path
    ):
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The Azure operation location is invalid.",
        )


def _operation_location(response: _HttpResponse) -> str:
    try:
        value = response.headers.get("Operation-Location")
    except Exception as exc:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The Azure operation location is missing.",
        ) from exc
    if not isinstance(value, str) or not value.strip():
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The Azure operation location is missing.",
        )
    return value.strip()


def _response_status_code(response: object) -> int:
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The Azure HTTP response is invalid.",
        )
    return status_code


def _response_json_object(response: _HttpResponse) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The Azure result payload is invalid.",
        ) from exc
    if not isinstance(payload, Mapping):
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
            "The Azure result payload is invalid.",
        )
    return payload


def _parse_succeeded_result(
    payload: Mapping[str, Any],
    page_input: VisualTextPageInput,
) -> tuple[_AzureLine, ...]:
    analyze_result = payload.get("analyzeResult")
    if not isinstance(analyze_result, Mapping):
        raise ValueError("analyzeResult must be an object")
    if analyze_result.get("modelId") != AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID:
        raise ValueError("modelId does not match prebuilt-read")
    api_version = analyze_result.get("apiVersion")
    if (
        api_version is not None
        and api_version != AZURE_DOCUMENT_INTELLIGENCE_API_VERSION
    ):
        raise ValueError("apiVersion does not match the requested contract")
    pages = analyze_result.get("pages")
    if not isinstance(pages, list) or len(pages) != 1:
        raise ValueError("a single PNG must produce exactly one page")
    page = pages[0]
    if not isinstance(page, Mapping) or page.get("pageNumber") != 1:
        raise ValueError("the returned image page is invalid")
    if page.get("unit") != "pixel":
        raise ValueError("image coordinates must use pixels")
    page_width = _finite_positive_number(page.get("width"), "page width")
    page_height = _finite_positive_number(page.get("height"), "page height")
    if not _dimension_matches(page_width, page_input.page_image_width_px) or not _dimension_matches(
        page_height,
        page_input.page_image_height_px,
    ):
        raise ValueError("returned page dimensions do not match the rendered image")

    words = page.get("words")
    lines = page.get("lines")
    if not isinstance(words, list) or not isinstance(lines, list):
        raise ValueError("words and lines must be arrays")
    word_evidence = tuple(_parse_word_evidence(item) for item in words)
    candidates = tuple(
        _parse_line(
            item,
            word_evidence=word_evidence,
            service_page_width=page_width,
            service_page_height=page_height,
            page_bbox=page_input.page_bbox,
        )
        for item in lines
    )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.span_start,
                item.bbox[1],
                item.bbox[0],
                item.bbox[3],
                item.bbox[2],
                item.raw_text,
            ),
        )
    )


def _parse_word_evidence(value: object) -> _AzureWordEvidence:
    if not isinstance(value, Mapping):
        raise ValueError("word evidence must be an object")
    span = value.get("span")
    if not isinstance(span, Mapping):
        raise ValueError("word span must be an object")
    offset = span.get("offset")
    length = span.get("length")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(length, bool)
        or not isinstance(length, int)
        or length <= 0
    ):
        raise ValueError("word span must contain a non-empty source range")
    confidence = _confidence(value.get("confidence"))
    return _AzureWordEvidence(
        span_start=offset,
        span_end=offset + length,
        confidence=confidence,
    )


def _parse_line(
    value: object,
    *,
    word_evidence: tuple[_AzureWordEvidence, ...],
    service_page_width: float,
    service_page_height: float,
    page_bbox: tuple[float, float, float, float],
) -> _AzureLine:
    if not isinstance(value, Mapping):
        raise ValueError("line must be an object")
    raw_text = value.get("content")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("line content must not be blank")
    line_spans = _parse_line_spans(value.get("spans"))
    matched_words = tuple(
        word
        for word in word_evidence
        if any(
            word.span_start >= start and word.span_end <= end
            for start, end in line_spans
        )
    )
    if not matched_words:
        raise ValueError("line confidence must be grounded in returned words")
    confidence = sum(word.confidence for word in matched_words) / len(matched_words)
    bbox = _ground_polygon(
        value.get("polygon"),
        service_page_width=service_page_width,
        service_page_height=service_page_height,
        page_bbox=page_bbox,
    )
    return _AzureLine(
        raw_text=raw_text,
        bbox=bbox,
        confidence=confidence,
        span_start=min(start for start, _ in line_spans),
    )


def _parse_line_spans(value: object) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("line spans must be a non-empty array")
    spans: list[tuple[int, int]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("line span must be an object")
        offset = item.get("offset")
        length = item.get("length")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length <= 0
        ):
            raise ValueError("line span must contain a non-empty source range")
        spans.append((offset, offset + length))
    return tuple(spans)


def _ground_polygon(
    value: object,
    *,
    service_page_width: float,
    service_page_height: float,
    page_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) < 8 or len(value) % 2:
        raise ValueError("line polygon must contain at least four points")
    coordinates = tuple(_finite_number(item, "polygon coordinate") for item in value)
    x_values = coordinates[0::2]
    y_values = coordinates[1::2]
    tolerance = 1e-6
    if (
        min(x_values) < -tolerance
        or min(y_values) < -tolerance
        or max(x_values) > service_page_width + tolerance
        or max(y_values) > service_page_height + tolerance
    ):
        raise ValueError("line polygon must remain inside the returned image page")
    source_x0 = min(max(min(x_values), 0.0), service_page_width)
    source_y0 = min(max(min(y_values), 0.0), service_page_height)
    source_x1 = min(max(max(x_values), 0.0), service_page_width)
    source_y1 = min(max(max(y_values), 0.0), service_page_height)
    if source_x1 <= source_x0 or source_y1 <= source_y0:
        raise ValueError("line polygon must have positive area")
    page_x0, page_y0, page_x1, page_y1 = page_bbox
    page_width = page_x1 - page_x0
    page_height = page_y1 - page_y0
    return (
        round(page_x0 + source_x0 / service_page_width * page_width, 9),
        round(page_y0 + source_y0 / service_page_height * page_height, 9),
        round(page_x0 + source_x1 / service_page_width * page_width, 9),
        round(page_y0 + source_y1 / service_page_height * page_height, 9),
    )


def _observation_from_line(
    *,
    line: _AzureLine,
    observation_index: int,
    page_input: VisualTextPageInput,
    provider_id: str,
    provider_version: str,
) -> VisualTextObservation:
    observation_id = stable_visual_text_observation_id(
        source_sha256=page_input.source_sha256,
        page_image_sha256=page_input.page_image_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_input.page_number,
        observation_index=observation_index,
        bbox=line.bbox,
    )
    return VisualTextObservation(
        observation_id=observation_id,
        observation_index=observation_index,
        source_sha256=page_input.source_sha256,
        page_number=page_input.page_number,
        bbox=line.bbox,
        raw_text=line.raw_text,
        confidence=line.confidence,
        provider_id=provider_id,
        provider_version=provider_version,
        page_image_sha256=page_input.page_image_sha256,
    )


def _confidence(value: object) -> float:
    confidence = _finite_number(value, "word confidence")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("word confidence must be in [0, 1]")
    return confidence


def _finite_positive_number(value: object, field_name: str) -> float:
    number = _finite_number(value, field_name)
    if number <= 0.0:
        raise ValueError(f"{field_name} must be positive")
    return number


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _dimension_matches(observed: float, expected: int) -> bool:
    return abs(observed - float(expected)) <= max(1e-6, abs(float(expected)) * 1e-9)


def _bounded_positive_float(value: object, field_name: str, *, maximum: float) -> float:
    number = _bounded_non_negative_float(value, field_name, maximum=maximum)
    if number <= 0.0:
        raise ValueError(f"{field_name} must be positive")
    return number


def _bounded_non_negative_float(
    value: object,
    field_name: str,
    *,
    maximum: float,
) -> float:
    number = _finite_number(value, field_name)
    if not 0.0 <= number <= maximum:
        raise ValueError(f"{field_name} must be in [0, {maximum}]")
    return number


__all__ = [
    "AZURE_DOCUMENT_INTELLIGENCE_API_VERSION",
    "AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID",
    "AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_ID",
    "AZURE_DOCUMENT_INTELLIGENCE_VISUAL_TEXT_PROVIDER_VERSION",
    "AzureDocumentIntelligenceVisualTextProvider",
]
