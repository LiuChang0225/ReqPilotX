from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from app.domain.source_visual_text import (
    VisualTextGeometryReceipt,
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


@dataclass(frozen=True, slots=True)
class GroundedVisualTextCandidate:
    raw_text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None
    geometry_receipt: VisualTextGeometryReceipt | None = None


def validate_visual_text_page_input(page_input: VisualTextPageInput) -> None:
    """Recompute both hashes before any page bytes cross a provider boundary."""

    if not isinstance(page_input, VisualTextPageInput):
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.INVALID_INPUT,
            "The visual text provider requires a validated page input.",
        )
    if hashlib.sha256(page_input.source_bytes).hexdigest() != page_input.source_sha256:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.SOURCE_HASH_MISMATCH,
            "The approved source bytes no longer match their recorded digest.",
        )
    if (
        hashlib.sha256(page_input.page_image_bytes).hexdigest()
        != page_input.page_image_sha256
    ):
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.PAGE_IMAGE_HASH_MISMATCH,
            "The rendered page image no longer matches its recorded digest.",
        )


def build_visual_text_snapshot(
    *,
    page_input: VisualTextPageInput,
    provider_id: str,
    provider_version: str,
    candidates: tuple[GroundedVisualTextCandidate, ...],
) -> VisualTextSnapshot:
    """Build immutable provider-neutral observations from page-grounded lines."""

    observations: list[VisualTextObservation] = []
    for index, candidate in enumerate(candidates):
        _validate_candidate(candidate, page_input.page_bbox)
        observation_id = stable_visual_text_observation_id(
            source_sha256=page_input.source_sha256,
            page_image_sha256=page_input.page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_input.page_number,
            observation_index=index,
            bbox=candidate.bbox,
        )
        observations.append(
            VisualTextObservation(
                observation_id=observation_id,
                observation_index=index,
                source_sha256=page_input.source_sha256,
                page_number=page_input.page_number,
                bbox=candidate.bbox,
                raw_text=candidate.raw_text,
                confidence=candidate.confidence,
                provider_id=provider_id,
                provider_version=provider_version,
                page_image_sha256=page_input.page_image_sha256,
                geometry_receipt=candidate.geometry_receipt,
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
        observations=tuple(observations),
    )


def pixel_polygon_to_page_bbox(
    *,
    coordinates: tuple[float, ...],
    image_width_px: int,
    image_height_px: int,
    page_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Map one validated clockwise pixel quadrilateral to a PDF page bbox."""

    points = validate_clockwise_pixel_quadrilateral(
        coordinates=coordinates,
        image_width_px=image_width_px,
        image_height_px=image_height_px,
    )
    xs = tuple(point[0] for point in points)
    ys = tuple(point[1] for point in points)
    page_x0, page_y0, page_x1, page_y1 = page_bbox
    page_width = page_x1 - page_x0
    page_height = page_y1 - page_y0
    return (
        page_x0 + min(xs) / image_width_px * page_width,
        page_y0 + min(ys) / image_height_px * page_height,
        page_x0 + max(xs) / image_width_px * page_width,
        page_y0 + max(ys) / image_height_px * page_height,
    )


def inverse_orthogonal_pixel_polygon_rotation(
    *,
    coordinates: tuple[float, ...],
    original_width_px: int,
    original_height_px: int,
    clockwise_rotation_degrees: int,
) -> tuple[float, ...]:
    """Return provider polygon points in the original image coordinate space.

    The formulas use continuous pixel-boundary coordinates rather than pixel
    indexes.  This keeps page edges at ``0`` and ``width``/``height`` stable
    across Pillow's lossless orthogonal rotations.
    """

    if (
        isinstance(original_width_px, bool)
        or not isinstance(original_width_px, int)
        or original_width_px < 1
        or isinstance(original_height_px, bool)
        or not isinstance(original_height_px, int)
        or original_height_px < 1
    ):
        raise ValueError("original image dimensions must be positive integers")
    if (
        isinstance(clockwise_rotation_degrees, bool)
        or not isinstance(clockwise_rotation_degrees, int)
        or clockwise_rotation_degrees not in {0, 90, 180, 270}
    ):
        raise ValueError("clockwise rotation must be 0, 90, 180, or 270")
    if clockwise_rotation_degrees in {0, 180}:
        rotated_width_px = original_width_px
        rotated_height_px = original_height_px
    else:
        rotated_width_px = original_height_px
        rotated_height_px = original_width_px
    points = validate_clockwise_pixel_quadrilateral(
        coordinates=coordinates,
        image_width_px=rotated_width_px,
        image_height_px=rotated_height_px,
    )

    transformed: list[float] = []
    for rotated_x, rotated_y in points:
        if clockwise_rotation_degrees == 0:
            original_x, original_y = rotated_x, rotated_y
        elif clockwise_rotation_degrees == 90:
            original_x = rotated_y
            original_y = float(original_height_px) - rotated_x
        elif clockwise_rotation_degrees == 180:
            original_x = float(original_width_px) - rotated_x
            original_y = float(original_height_px) - rotated_y
        else:
            original_x = float(original_width_px) - rotated_y
            original_y = rotated_x
        transformed.extend((original_x, original_y))
    result = tuple(transformed)
    validate_clockwise_pixel_quadrilateral(
        coordinates=result,
        image_width_px=original_width_px,
        image_height_px=original_height_px,
    )
    return result


def validate_clockwise_pixel_quadrilateral(
    *,
    coordinates: tuple[float, ...],
    image_width_px: int,
    image_height_px: int,
) -> tuple[tuple[float, float], ...]:
    """Validate the provider's top-left-first, clockwise four-point contract.

    The provider declares the first vertex as the text-relative top-left point.
    Geometry can independently prove the clockwise traversal, uniqueness,
    simplicity, strict convexity, positive area, and page containment.  It
    cannot infer text reading direction, so the first-vertex semantic remains
    provider-declared rather than a recognition-accuracy claim.
    """

    if (
        isinstance(image_width_px, bool)
        or not isinstance(image_width_px, int)
        or image_width_px < 1
        or isinstance(image_height_px, bool)
        or not isinstance(image_height_px, int)
        or image_height_px < 1
    ):
        raise ValueError("image dimensions must be positive integers")
    if len(coordinates) != 8 or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in coordinates
    ):
        raise ValueError("polygon must contain exactly four finite numeric points")
    points = tuple(
        (float(coordinates[index]), float(coordinates[index + 1]))
        for index in range(0, 8, 2)
    )
    if len(set(points)) != 4:
        raise ValueError("polygon must contain four unique points")
    if any(
        x < 0.0
        or y < 0.0
        or x > float(image_width_px)
        or y > float(image_height_px)
        for x, y in points
    ):
        raise ValueError("polygon must stay inside the rendered page image")

    tolerance = max(1e-9, float(image_width_px * image_height_px) * 1e-12)
    if _segments_intersect(
        points[0], points[1], points[2], points[3], tolerance=tolerance
    ) or _segments_intersect(
        points[1], points[2], points[3], points[0], tolerance=tolerance
    ):
        raise ValueError("polygon must be simple")
    signed_double_area = sum(
        point[0] * points[(index + 1) % 4][1]
        - points[(index + 1) % 4][0] * point[1]
        for index, point in enumerate(points)
    )
    if signed_double_area <= tolerance:
        raise ValueError("polygon must have positive clockwise area")
    corner_cross_products = tuple(
        _cross_product(
            points[index],
            points[(index + 1) % 4],
            points[(index + 2) % 4],
        )
        for index in range(4)
    )
    if any(value <= tolerance for value in corner_cross_products):
        raise ValueError("polygon must be strictly convex and clockwise")
    return points


def _cross_product(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (
        second[1] - first[1]
    ) * (third[0] - first[0])


def _segments_intersect(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
    *,
    tolerance: float,
) -> bool:
    orientations = (
        _cross_product(first_start, first_end, second_start),
        _cross_product(first_start, first_end, second_end),
        _cross_product(second_start, second_end, first_start),
        _cross_product(second_start, second_end, first_end),
    )
    if (
        orientations[0] * orientations[1] < -(tolerance**2)
        and orientations[2] * orientations[3] < -(tolerance**2)
    ):
        return True
    endpoints = (
        (orientations[0], second_start, first_start, first_end),
        (orientations[1], second_end, first_start, first_end),
        (orientations[2], first_start, second_start, second_end),
        (orientations[3], first_end, second_start, second_end),
    )
    return any(
        abs(orientation) <= tolerance
        and min(start[0], end[0]) - tolerance
        <= point[0]
        <= max(start[0], end[0]) + tolerance
        and min(start[1], end[1]) - tolerance
        <= point[1]
        <= max(start[1], end[1]) + tolerance
        for orientation, point, start, end in endpoints
    )


def _validate_candidate(
    candidate: GroundedVisualTextCandidate,
    page_bbox: tuple[float, float, float, float],
) -> None:
    if not isinstance(candidate, GroundedVisualTextCandidate):
        raise TypeError("candidate must be a GroundedVisualTextCandidate")
    if not isinstance(candidate.raw_text, str) or not candidate.raw_text.strip():
        raise ValueError("candidate text must not be blank")
    if candidate.confidence is not None and (
        isinstance(candidate.confidence, bool)
        or not isinstance(candidate.confidence, (int, float))
        or not math.isfinite(float(candidate.confidence))
        or not 0.0 <= float(candidate.confidence) <= 1.0
    ):
        raise ValueError("candidate confidence must be null or in [0, 1]")
    x0, y0, x1, y1 = candidate.bbox
    px0, py0, px1, py1 = page_bbox
    if x1 <= x0 or y1 <= y0 or x0 < px0 or y0 < py0 or x1 > px1 or y1 > py1:
        raise ValueError("candidate bbox must have positive area inside the page")


__all__ = [
    "GroundedVisualTextCandidate",
    "build_visual_text_snapshot",
    "inverse_orthogonal_pixel_polygon_rotation",
    "pixel_polygon_to_page_bbox",
    "validate_clockwise_pixel_quadrilateral",
    "validate_visual_text_page_input",
]
