from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.application.extraction.structure_boundary_rules import (
    APPENDIX_QUALIFIER_PREFIX,
    AmbiguousLetteredRegionMarkerEvidence,
    ambiguous_lettered_region_marker,
    contains_document_region_self_reference,
    contains_lettered_region_designator,
    iter_document_region_markers,
)
from app.domain.extraction.pdf_page_furniture import visual_pdf_page_furniture_ids
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextSnapshot,
)


@dataclass(frozen=True, slots=True)
class VisualDocumentRegionEvidencePolicyV1:
    """Versioned geometry and recognition bounds for visual region evidence."""

    policy_id: str = "visual-document-region-evidence-policy/v1"
    page_top_ratio: float = 0.30
    center_tolerance_ratio: float = 0.18
    maximum_marker_width_ratio: float = 0.45
    maximum_marker_height_ratio: float = 0.12
    qualifier_distance_ratio: float = 0.10
    self_reference_page_limit_ratio: float = 0.50
    minimum_calibrated_confidence: float = 0.65


DEFAULT_VISUAL_DOCUMENT_REGION_EVIDENCE_POLICY = (
    VisualDocumentRegionEvidencePolicyV1()
)


@dataclass(frozen=True, slots=True)
class VisualDocumentRegionEvidence:
    """One page-image-bound region suspicion; never a tree-write command."""

    source_ref: StructureSourceRef
    start_offset: int
    end_offset: int
    page_number: int
    bbox: tuple[float, float, float, float]
    safe_marker: str
    recognition_confidence: float | None
    reason_codes: tuple[str, ...]
    explicit_marker_shape: bool
    order_key: tuple[int, int, str]


@dataclass(frozen=True, slots=True)
class _WeakRegionObservation:
    snapshot: VisualTextSnapshot
    observation: VisualTextObservation
    marker: AmbiguousLetteredRegionMarkerEvidence
    order_rank: int

    @property
    def source_ref(self) -> StructureSourceRef:
        return StructureSourceRef(
            kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
            source_id=self.observation.observation_id,
        )

    @property
    def order_key(self) -> tuple[int, int, str]:
        return (
            self.snapshot.page_number,
            self.order_rank,
            self.observation.observation_id,
        )


_ObservationEntry = tuple[VisualTextSnapshot, VisualTextObservation]
_SnapshotSeriesKey = tuple[str, str, str, str, int, int]


def collect_visual_document_region_evidence(
    source_ir: SourceIRSnapshot,
    *,
    policy: VisualDocumentRegionEvidencePolicyV1 = (
        DEFAULT_VISUAL_DOCUMENT_REGION_EVIDENCE_POLICY
    ),
) -> tuple[VisualDocumentRegionEvidence, ...]:
    """Compose visual marker facts with bounded same-page corroboration.

    Native invisible PDF text is intentionally outside this function. Every
    retained source reference binds provider/version, page-image SHA, page, and
    bbox through ``VisualTextObservation``. The collector does not authorize a
    repair or assign inspection confidence; that decision belongs to the
    inspector.
    """

    snapshots = _unique_active_snapshots(tuple(source_ir.visual_text_snapshots))
    if not snapshots:
        return ()
    furniture_ids = set(visual_pdf_page_furniture_ids(snapshots))
    eligible_by_series: dict[_SnapshotSeriesKey, list[_ObservationEntry]] = {}
    all_eligible: list[_ObservationEntry] = []
    for snapshot in snapshots:
        series_key = _snapshot_series_key(snapshot)
        for observation in snapshot.observations:
            if not _observation_is_eligible(
                observation,
                furniture_ids=furniture_ids,
            ):
                continue
            entry = (snapshot, observation)
            all_eligible.append(entry)
            eligible_by_series.setdefault(series_key, []).append(entry)

    ordered_eligible = tuple(sorted(all_eligible, key=_reading_order_key))
    order_rank_by_observation_id = {
        observation.observation_id: index
        for index, (_snapshot, observation) in enumerate(ordered_eligible)
    }
    weak_by_series: dict[_SnapshotSeriesKey, list[_WeakRegionObservation]] = {}
    result: list[VisualDocumentRegionEvidence] = []

    for snapshot, observation in ordered_eligible:
        order_rank = order_rank_by_observation_id[observation.observation_id]
        explicit_markers = tuple(
            marker
            for marker in iter_document_region_markers(observation.raw_text)
            if not marker.reference_like
        )
        for marker in explicit_markers:
            result.append(
                VisualDocumentRegionEvidence(
                    source_ref=StructureSourceRef(
                        kind=(
                            StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
                        ),
                        source_id=observation.observation_id,
                    ),
                    start_offset=marker.start_offset,
                    end_offset=marker.end_offset,
                    page_number=snapshot.page_number,
                    bbox=observation.bbox,
                    safe_marker=marker.safe_marker,
                    recognition_confidence=observation.confidence,
                    reason_codes=(
                        "visual_document_region_marker_shape_detected",
                    ),
                    explicit_marker_shape=True,
                    order_key=(
                        snapshot.page_number,
                        order_rank,
                        observation.observation_id,
                    ),
                )
            )

        if explicit_markers:
            continue
        weak_marker = ambiguous_lettered_region_marker(observation.raw_text)
        if (
            weak_marker is not None
            and _observation_is_top_centered(
                snapshot,
                observation=observation,
                policy=policy,
            )
        ):
            weak_by_series.setdefault(
                _snapshot_series_key(snapshot),
                [],
            ).append(
                _WeakRegionObservation(
                    snapshot=snapshot,
                    observation=observation,
                    marker=weak_marker,
                    order_rank=order_rank,
                )
            )

    for series_key, weak_observations in weak_by_series.items():
        series_observations = tuple(
            sorted(
                eligible_by_series.get(series_key, ()),
                key=_reading_order_key,
            )
        )
        for weak in sorted(weak_observations, key=lambda item: item.order_key):
            corroboration = _corroboration_reason_codes(
                weak,
                series_observations=series_observations,
                policy=policy,
            )
            if not corroboration:
                continue
            result.append(
                VisualDocumentRegionEvidence(
                    source_ref=weak.source_ref,
                    start_offset=weak.marker.start_offset,
                    end_offset=weak.marker.end_offset,
                    page_number=weak.snapshot.page_number,
                    bbox=weak.observation.bbox,
                    safe_marker=weak.marker.marker,
                    recognition_confidence=weak.observation.confidence,
                    reason_codes=tuple(
                        sorted(
                            {
                                *corroboration,
                                (
                                    "ambiguous_lettered_region_marker_"
                                    "requires_confirmation"
                                ),
                                "visual_document_region_marker_shape_detected",
                                "visual_page_top_centered_region_marker",
                            }
                        )
                    ),
                    explicit_marker_shape=False,
                    order_key=weak.order_key,
                )
            )

    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.order_key,
                item.start_offset,
                item.end_offset,
                item.safe_marker,
            ),
        )
    )


def _unique_active_snapshots(
    snapshots: tuple[VisualTextSnapshot, ...],
) -> tuple[VisualTextSnapshot, ...]:
    """Fail closed when one provider claims multiple renders for one page."""

    grouped: dict[
        tuple[str, int, str, str],
        list[VisualTextSnapshot],
    ] = {}
    for snapshot in snapshots:
        grouped.setdefault(
            (
                snapshot.source_sha256,
                snapshot.page_number,
                snapshot.provider_id,
                snapshot.provider_version,
            ),
            [],
        ).append(snapshot)
    active = [
        group[0]
        for group in grouped.values()
        if len(group) == 1
    ]
    return tuple(
        sorted(
            active,
            key=lambda snapshot: (
                snapshot.page_number,
                snapshot.provider_id,
                snapshot.provider_version,
                snapshot.visual_text_snapshot_id,
            ),
        )
    )


def _snapshot_series_key(
    snapshot: VisualTextSnapshot,
) -> _SnapshotSeriesKey:
    return (
        snapshot.source_sha256,
        snapshot.provider_id,
        snapshot.provider_version,
        snapshot.page_image_media_type,
        snapshot.page_image_width_px,
        snapshot.page_image_height_px,
    )


def _reading_order_key(
    entry: _ObservationEntry,
) -> tuple[int, float, float, int, str]:
    snapshot, observation = entry
    return (
        snapshot.page_number,
        observation.bbox[1],
        observation.bbox[0],
        observation.observation_index,
        observation.observation_id,
    )


def _observation_is_eligible(
    observation: VisualTextObservation,
    *,
    furniture_ids: set[str],
) -> bool:
    return (
        observation.content_type == "TEXT"
        and observation.role is None
        and observation.parent_observation_index is None
        and observation.observation_id not in furniture_ids
    )


def _observation_is_top_centered(
    snapshot: VisualTextSnapshot,
    *,
    observation: VisualTextObservation,
    policy: VisualDocumentRegionEvidencePolicyV1,
) -> bool:
    page_x0, page_y0, page_x1, page_y1 = snapshot.page_bbox
    x0, y0, x1, y1 = observation.bbox
    page_width = page_x1 - page_x0
    page_height = page_y1 - page_y0
    observation_width = x1 - x0
    observation_height = y1 - y0
    if page_width <= 0 or page_height <= 0:
        return False
    page_center_x = (page_x0 + page_x1) / 2.0
    observation_center_x = (x0 + x1) / 2.0
    return (
        y0 <= page_y0 + page_height * policy.page_top_ratio
        and abs(observation_center_x - page_center_x)
        <= page_width * policy.center_tolerance_ratio
        and observation_width
        <= page_width * policy.maximum_marker_width_ratio
        and observation_height
        <= page_height * policy.maximum_marker_height_ratio
    )


def _corroboration_reason_codes(
    weak: _WeakRegionObservation,
    *,
    series_observations: tuple[_ObservationEntry, ...],
    policy: VisualDocumentRegionEvidencePolicyV1,
) -> tuple[str, ...]:
    """Return only same-page, downstream structural relations."""

    reasons: set[str] = set()
    page_height = weak.snapshot.page_bbox[3] - weak.snapshot.page_bbox[1]
    marker_bottom = weak.observation.bbox[3]
    qualifier_limit = marker_bottom + page_height * policy.qualifier_distance_ratio
    self_reference_limit = (
        weak.snapshot.page_bbox[1]
        + page_height * policy.self_reference_page_limit_ratio
    )
    weak_reading_order = _reading_order_key(
        (weak.snapshot, weak.observation)
    )

    for snapshot, observation in series_observations:
        if (
            snapshot.page_number != weak.snapshot.page_number
            or observation.observation_id == weak.observation.observation_id
            or _reading_order_key((snapshot, observation))
            <= weak_reading_order
        ):
            continue
        normalized = unicodedata.normalize(
            "NFKC",
            observation.raw_text,
        ).strip()
        if (
            marker_bottom <= observation.bbox[1] <= qualifier_limit
            and APPENDIX_QUALIFIER_PREFIX.match(normalized) is not None
        ):
            reasons.add("nearby_document_region_qualifier")
        if (
            marker_bottom <= observation.bbox[1] <= self_reference_limit
            and contains_document_region_self_reference(
                observation.raw_text
            )
        ):
            reasons.add("nearby_document_region_self_reference")
        if contains_lettered_region_designator(
            observation.raw_text,
            marker=weak.marker.marker,
        ):
            reasons.add("downstream_lettered_region_designator")

    return tuple(sorted(reasons))


__all__ = [
    "DEFAULT_VISUAL_DOCUMENT_REGION_EVIDENCE_POLICY",
    "VisualDocumentRegionEvidence",
    "VisualDocumentRegionEvidencePolicyV1",
    "collect_visual_document_region_evidence",
]
