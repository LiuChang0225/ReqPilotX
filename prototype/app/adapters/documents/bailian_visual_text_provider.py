from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from io import BytesIO
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from app.adapters.documents.visual_text_provider_support import (
    GroundedVisualTextCandidate,
    build_visual_text_snapshot,
    inverse_orthogonal_pixel_polygon_rotation,
    pixel_polygon_to_page_bbox,
    validate_clockwise_pixel_quadrilateral,
    validate_visual_text_page_input,
)
from app.domain.source_visual_text import (
    VisualTextGeometryReceipt,
    VisualTextPageInput,
    VisualTextSnapshot,
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

_ALLOWED_DASHSCOPE_HOSTS = frozenset({"dashscope.aliyuncs.com"})


BAILIAN_VISUAL_TEXT_PROVIDER_ID = "aliyun-bailian-qwen-ocr"
BAILIAN_VISUAL_TEXT_PROVIDER_IMPLEMENTATION_VERSION = "1.5.0"
DEFAULT_BAILIAN_OCR_MODEL = "qwen3.5-ocr"
DEFAULT_BAILIAN_OCR_SEED = 1234
DEFAULT_BAILIAN_OCR_TEMPERATURE = 0.01
BAILIAN_OCR_TASK = "advanced_recognition"
# Bailian defines ``location`` as absolute coordinates in the original input
# image. Explicit local rotations are inverted point-by-point before grounding.
_BAILIAN_OCR_COORDINATE_SPACE = "original-page-image"
_INPUT_ROTATION_TRANSFORM_VERSION = "pb2"
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_MAX_BASE64_CHARACTERS = 10 * 1024 * 1024
_MAX_IMAGE_PIXELS = 15_680_000
_MAX_BAILIAN_OCR_SEED = 2**31 - 1
_ALLOWED_INPUT_ROTATIONS = frozenset({0, 90, 180, 270})
_PIL_TRANSPOSE_BY_CLOCKWISE_ROTATION = {
    90: PILImage.Transpose.ROTATE_270,
    180: PILImage.Transpose.ROTATE_180,
    270: PILImage.Transpose.ROTATE_90,
}


class _BailianOcrPageDeadlineExceeded(Exception):
    """Internal marker for the absolute recognize-page wall-clock boundary."""


class BailianVisualTextProvider:
    """Remote-only Bailian OCR with native, provider-grounded line polygons."""

    provider_id = BAILIAN_VISUAL_TEXT_PROVIDER_ID

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = DEFAULT_BAILIAN_OCR_MODEL,
        enable_rotate: bool = True,
        input_rotations_by_page: Mapping[int, int] | None = None,
        seed: int = DEFAULT_BAILIAN_OCR_SEED,
        temperature: float = DEFAULT_BAILIAN_OCR_TEMPERATURE,
        timeout_seconds: float = 45.0,
        max_retries: int = 0,
        http_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        telemetry_callback: VisualTextProviderTelemetryCallback | None = None,
    ) -> None:
        normalized_model = str(model or "").strip()
        if not _MODEL_PATTERN.fullmatch(normalized_model):
            raise ValueError("Bailian OCR model has an invalid identifier")
        if not isinstance(enable_rotate, bool):
            raise TypeError("enable_rotate must be a boolean")
        normalized_rotations = _normalize_input_rotations_by_page(
            input_rotations_by_page
        )
        if enable_rotate and any(normalized_rotations.values()):
            raise ValueError(
                "Bailian auto-rotation and non-zero input rotation cannot both be enabled"
            )
        if (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not 0 <= seed <= _MAX_BAILIAN_OCR_SEED
        ):
            raise ValueError(
                "Bailian OCR seed must be an integer between zero and 2^31 - 1"
            )
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError("Bailian OCR temperature must be numeric")
        normalized_temperature = float(temperature)
        if (
            not math.isfinite(normalized_temperature)
            or not 0.0 <= normalized_temperature < 2.0
        ):
            raise ValueError("Bailian OCR temperature must be between zero and two")
        if normalized_temperature == 0.0:
            normalized_temperature = 0.0
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0.0
        ):
            raise ValueError("timeout_seconds must be positive")
        if isinstance(max_retries, bool) or not 0 <= int(max_retries) <= 1:
            raise ValueError("max_retries must be zero or one")
        self._api_key = str(api_key or "").strip()
        self._endpoint = _native_multimodal_endpoint(base_url)
        self._model = normalized_model
        self._enable_rotate = enable_rotate
        self._input_rotations_by_page = normalized_rotations
        self._seed = seed
        self._temperature = normalized_temperature
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = int(max_retries)
        self._http_client = http_client
        self._sleep = sleep
        self._monotonic = monotonic
        self._telemetry_callback = telemetry_callback
        self.provider_version = _provider_version(
            model=self._model,
            enable_rotate=self._enable_rotate,
            input_rotations_by_page=self._input_rotations_by_page,
            seed=self._seed,
            temperature=self._temperature,
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
                    "Bailian visual text recognition is not configured.",
                )
            _validate_image_limits(page_input)
            input_rotation = self._input_rotations_by_page.get(
                page_input.page_number,
                0,
            )
            provider_image_bytes, provider_image_width_px, provider_image_height_px = (
                _page_image_for_provider(
                    page_input=page_input,
                    clockwise_rotation_degrees=input_rotation,
                )
            )
            encoded_image = base64.b64encode(provider_image_bytes).decode("ascii")
            if len(encoded_image) > _MAX_BASE64_CHARACTERS:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.INVALID_INPUT,
                    "The rendered page exceeds the Bailian in-memory image limit.",
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
                    "ocr_options": {"task": BAILIAN_OCR_TASK},
                    "seed": self._seed,
                    "temperature": self._temperature,
                },
            }
            response = self._post(
                request_payload,
                page_deadline=page_deadline,
                request_started_callback=count_request,
            )
            try:
                response_payload = response.json()
                candidates = _candidates_from_response(
                    response_payload,
                    page_input,
                    input_rotation_degrees=input_rotation,
                    provider_image_width_px=provider_image_width_px,
                    provider_image_height_px=provider_image_height_px,
                )
                snapshot = build_visual_text_snapshot(
                    page_input=page_input,
                    provider_id=self.provider_id,
                    provider_version=self.provider_version,
                    candidates=candidates,
                )
                outcome = "READY"
                return snapshot
            except VisualTextProviderRejected:
                raise
            except Exception as exc:
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_OUTPUT_INVALID,
                    "Bailian returned an invalid visual text response.",
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
    ) -> Any:
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
                    remaining_seconds = _remaining_ocr_page_seconds(
                        deadline=page_deadline,
                        monotonic=self._monotonic,
                    )
                    request_started_callback()
                    response = client.post(
                        self._endpoint,
                        headers=headers,
                        json=request_payload,
                        timeout=remaining_seconds,
                    )
                    _remaining_ocr_page_seconds(
                        deadline=page_deadline,
                        monotonic=self._monotonic,
                    )
                except _BailianOcrPageDeadlineExceeded as exc:
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.PROVIDER_FAILED,
                        "Bailian visual text recognition failed.",
                    ) from exc
                except httpx.TransportError as exc:
                    if attempt < self._max_retries:
                        try:
                            _sleep_before_ocr_page_retry(
                                attempt=attempt,
                                deadline=page_deadline,
                                sleep=self._sleep,
                                monotonic=self._monotonic,
                            )
                        except _BailianOcrPageDeadlineExceeded as deadline_exc:
                            raise VisualTextProviderRejected(
                                VisualTextProviderRejectionCode.PROVIDER_FAILED,
                                "Bailian visual text recognition failed.",
                            ) from deadline_exc
                        continue
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.PROVIDER_FAILED,
                        "Bailian visual text recognition failed.",
                    ) from exc
                except Exception as exc:
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.PROVIDER_FAILED,
                        "Bailian visual text recognition failed.",
                    ) from exc
                status_code = int(getattr(response, "status_code", 0) or 0)
                if status_code == 200:
                    return response
                if status_code in {401, 403}:
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.UNAVAILABLE,
                        "Bailian visual text recognition is not authorized.",
                    )
                if (
                    status_code == 429 or 500 <= status_code <= 599
                ) and attempt < self._max_retries:
                    try:
                        _sleep_before_ocr_page_retry(
                            attempt=attempt,
                            deadline=page_deadline,
                            sleep=self._sleep,
                            monotonic=self._monotonic,
                        )
                    except _BailianOcrPageDeadlineExceeded as exc:
                        raise VisualTextProviderRejected(
                            VisualTextProviderRejectionCode.PROVIDER_FAILED,
                            "Bailian visual text recognition failed.",
                        ) from exc
                    continue
                if status_code == 429:
                    raise VisualTextProviderRejected(
                        VisualTextProviderRejectionCode.RATE_LIMITED,
                        "Bailian visual text recognition is rate limited.",
                    )
                raise VisualTextProviderRejected(
                    VisualTextProviderRejectionCode.PROVIDER_FAILED,
                    "Bailian visual text recognition failed.",
                )
        finally:
            if owned_client:
                client.close()


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


def _rotation_map_sha256(input_rotations_by_page: Mapping[int, int]) -> str:
    canonical_map = [
        [page, rotation] for page, rotation in input_rotations_by_page.items()
    ]
    canonical_json = json.dumps(
        canonical_map,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("ascii")).hexdigest()


def _remaining_ocr_page_seconds(
    *,
    deadline: float,
    monotonic: Callable[[], float],
) -> float:
    remaining_seconds = deadline - monotonic()
    if remaining_seconds <= 0:
        raise _BailianOcrPageDeadlineExceeded
    return remaining_seconds


def _sleep_before_ocr_page_retry(
    *,
    attempt: int,
    deadline: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    remaining_seconds = _remaining_ocr_page_seconds(
        deadline=deadline,
        monotonic=monotonic,
    )
    delay_seconds = min(0.5 * (2**attempt), 2.0, remaining_seconds)
    sleep(delay_seconds)
    _remaining_ocr_page_seconds(
        deadline=deadline,
        monotonic=monotonic,
    )


def _provider_version(
    *,
    model: str,
    enable_rotate: bool,
    input_rotations_by_page: Mapping[int, int],
    seed: int,
    temperature: float,
    timeout_seconds: float,
    max_retries: int,
) -> str:
    rotation_map_sha256 = _rotation_map_sha256(input_rotations_by_page)
    temperature_identity = format(temperature, ".17g")
    deadline_identity = format(timeout_seconds, ".12g")
    readable = (
        f"{BAILIAN_VISUAL_TEXT_PROVIDER_IMPLEMENTATION_VERSION};"
        f"model={model};task={BAILIAN_OCR_TASK};"
        f"auto_rotate={int(enable_rotate)};s={seed};t={temperature_identity};"
        f"rot={rotation_map_sha256[:16]};"
        f"x={_INPUT_ROTATION_TRANSFORM_VERSION};c=orig;"
        f"dl={deadline_identity};tr={max_retries};cap={max_retries + 1}"
    )
    if not any(input_rotations_by_page.values()) and len(readable) <= 160:
        return readable

    canonical_config = json.dumps(
        {
            "auto_rotate": enable_rotate,
            "coordinate_space": _BAILIAN_OCR_COORDINATE_SPACE,
            "input_rotation_transform": _INPUT_ROTATION_TRANSFORM_VERSION,
            "model": model,
            "rotation_map_sha256": rotation_map_sha256,
            "seed": seed,
            "temperature": temperature,
            "task": BAILIAN_OCR_TASK,
            "timeout_seconds": timeout_seconds,
            "transport_retries": max_retries,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    config_sha256 = hashlib.sha256(canonical_config.encode("ascii")).hexdigest()
    compact = (
        f"{BAILIAN_VISUAL_TEXT_PROVIDER_IMPLEMENTATION_VERSION};"
        f"cfg={config_sha256[:16]};rot={rotation_map_sha256};"
        f"x={_INPUT_ROTATION_TRANSFORM_VERSION};c=orig;"
        f"dl={deadline_identity};tr={max_retries};cap={max_retries + 1}"
    )
    if len(compact) > 160:  # pragma: no cover - fixed-size defensive invariant
        raise RuntimeError("Bailian provider version exceeds its schema limit")
    return compact


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
            if image.width * image.height > _MAX_IMAGE_PIXELS:
                raise ValueError("page image exceeds the pixel limit")
            image.load()
            rotated = image.transpose(
                _PIL_TRANSPOSE_BY_CLOCKWISE_ROTATION[clockwise_rotation_degrees]
            )
            output = BytesIO()
            rotated.save(
                output,
                format="PNG",
                optimize=False,
                compress_level=9,
            )
            rotated_width_px, rotated_height_px = rotated.size
    except Exception as exc:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.INVALID_INPUT,
            "The rendered page cannot be deterministically rotated for Bailian.",
        ) from exc
    return output.getvalue(), rotated_width_px, rotated_height_px


def _candidates_from_response(
    payload: object,
    page_input: VisualTextPageInput,
    *,
    input_rotation_degrees: int = 0,
    provider_image_width_px: int | None = None,
    provider_image_height_px: int | None = None,
) -> tuple[GroundedVisualTextCandidate, ...]:
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
        raise ValueError("response choice must finish exactly with stop")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        raise ValueError("response content is missing")
    words_info: list[object] | None = None
    for part in content:
        ocr_result = part.get("ocr_result") if isinstance(part, dict) else None
        candidate_words = (
            ocr_result.get("words_info") if isinstance(ocr_result, dict) else None
        )
        if isinstance(candidate_words, list):
            if words_info is not None:
                raise ValueError("response contains duplicate OCR result sets")
            words_info = candidate_words
    if words_info is None:
        raise ValueError("response OCR result is missing")

    expected_provider_width_px = (
        page_input.page_image_width_px
        if provider_image_width_px is None
        else provider_image_width_px
    )
    expected_provider_height_px = (
        page_input.page_image_height_px
        if provider_image_height_px is None
        else provider_image_height_px
    )
    if input_rotation_degrees in {90, 270}:
        actual_provider_dimensions = (
            page_input.page_image_height_px,
            page_input.page_image_width_px,
        )
    else:
        actual_provider_dimensions = (
            page_input.page_image_width_px,
            page_input.page_image_height_px,
        )
    if (expected_provider_width_px, expected_provider_height_px) != (
        actual_provider_dimensions
    ):
        raise ValueError("provider image dimensions do not match input rotation")

    candidates: list[GroundedVisualTextCandidate] = []
    for item in words_info:
        if not isinstance(item, dict):
            raise ValueError("OCR line must be an object")
        raw_text = item.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError("OCR line text must not be blank")
        location = item.get("location")
        if not isinstance(location, list):
            raise ValueError("OCR line location is missing")
        provider_coordinates = tuple(location)
        provider_points = validate_clockwise_pixel_quadrilateral(
            coordinates=provider_coordinates,
            image_width_px=expected_provider_width_px,
            image_height_px=expected_provider_height_px,
        )
        original_coordinates = inverse_orthogonal_pixel_polygon_rotation(
            coordinates=provider_coordinates,
            original_width_px=page_input.page_image_width_px,
            original_height_px=page_input.page_image_height_px,
            clockwise_rotation_degrees=input_rotation_degrees,
        )
        original_points = tuple(
            (float(original_coordinates[index]), float(original_coordinates[index + 1]))
            for index in range(0, 8, 2)
        )
        bbox = pixel_polygon_to_page_bbox(
            coordinates=original_coordinates,
            image_width_px=page_input.page_image_width_px,
            image_height_px=page_input.page_image_height_px,
            page_bbox=page_input.page_bbox,
        )
        confidence_value = item.get("confidence")
        if confidence_value is not None and (
            isinstance(confidence_value, bool)
            or not isinstance(confidence_value, (int, float))
        ):
            raise ValueError("OCR confidence must be numeric when present")
        confidence = None if confidence_value is None else float(confidence_value)
        candidates.append(
            GroundedVisualTextCandidate(
                raw_text=raw_text,
                bbox=bbox,
                confidence=confidence,
                geometry_receipt=VisualTextGeometryReceipt(
                    provider_quadrilateral_px=provider_points,
                    original_page_quadrilateral_px=original_points,
                    provider_image_size_px=(
                        expected_provider_width_px,
                        expected_provider_height_px,
                    ),
                    original_page_image_size_px=(
                        page_input.page_image_width_px,
                        page_input.page_image_height_px,
                    ),
                    input_rotation_degrees=input_rotation_degrees,
                    target_bbox=bbox,
                ),
            )
        )
    return tuple(candidates)


__all__ = [
    "BAILIAN_OCR_TASK",
    "BAILIAN_VISUAL_TEXT_PROVIDER_ID",
    "DEFAULT_BAILIAN_OCR_MODEL",
    "DEFAULT_BAILIAN_OCR_SEED",
    "DEFAULT_BAILIAN_OCR_TEMPERATURE",
    "BailianVisualTextProvider",
]
