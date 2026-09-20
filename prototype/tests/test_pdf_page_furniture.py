from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import unittest
from datetime import datetime, timezone

from app.application.extraction.build_shadow_preview import build_document_view
from app.application.extraction.classify_content import classify_content
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.pdf_page_furniture import (
    PdfPageFurnitureCandidate,
    PdfPageFurnitureImageCandidate,
    PdfPageFurnitureKind,
    classify_pdf_page_furniture,
    classify_pdf_repeated_image_furniture,
)
from app.domain.extraction.semantics import SemanticClass
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
)
from app.domain.extraction.structure import (
    StructureBoundaryBasis,
    StructureNodeKind,
    StructureSourceRefKind,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)


_SOURCE_SHA256 = "7" * 64
_PAGE_BBOX = (0.0, 0.0, 600.0, 800.0)


class PdfPageFurnitureProofTest(unittest.TestCase):
    def test_repeated_image_requires_same_digest_and_near_geometry(self) -> None:
        candidates = tuple(
            PdfPageFurnitureImageCandidate(
                candidate_id=f"logo-{page_number}",
                page_number=page_number,
                page_order=0,
                content_sha256="a" * 64,
                bbox=(50.0 + shift, 20.0, 150.0 + shift, 50.0),
                page_bbox=_PAGE_BBOX,
            )
            for page_number, shift in ((1, 0.0), (2, 2.0), (3, -2.0))
        )
        self.assertEqual(
            classify_pdf_repeated_image_furniture(candidates),
            {
                "logo-1": PdfPageFurnitureKind.HEADER,
                "logo-2": PdfPageFurnitureKind.HEADER,
                "logo-3": PdfPageFurnitureKind.HEADER,
            },
        )

        shifted = tuple(
            PdfPageFurnitureImageCandidate(
                candidate_id=f"shifted-{page_number}",
                page_number=page_number,
                page_order=0,
                content_sha256="a" * 64,
                bbox=(x0, 20.0, x0 + 100.0, 50.0),
                page_bbox=_PAGE_BBOX,
            )
            for page_number, x0 in ((1, 50.0), (2, 50.0), (3, 150.0))
        )
        self.assertEqual(classify_pdf_repeated_image_furniture(shifted), {})

        changed_digest = tuple(
            PdfPageFurnitureImageCandidate(
                candidate_id=f"digest-{page_number}",
                page_number=page_number,
                page_order=0,
                content_sha256=("b" if page_number == 3 else "a") * 64,
                bbox=(50.0, 20.0, 150.0, 50.0),
                page_bbox=_PAGE_BBOX,
            )
            for page_number in (1, 2, 3)
        )
        self.assertEqual(classify_pdf_repeated_image_furniture(changed_digest), {})

    def test_fixed_text_requires_three_adjacent_pages_and_outer_ten_percent(
        self,
    ) -> None:
        candidates = tuple(
            _candidate(
                candidate_id=f"header-{page_number}",
                page_number=page_number,
                raw_text="STANDARD HEADER",
                bbox=(50.0, 20.0, 300.0, 34.0),
            )
            for page_number in (1, 2, 3)
        )
        result = classify_pdf_page_furniture(candidates)

        self.assertEqual(
            result,
            {
                "header-1": PdfPageFurnitureKind.HEADER,
                "header-2": PdfPageFurnitureKind.HEADER,
                "header-3": PdfPageFurnitureKind.HEADER,
            },
        )

        nonadjacent = tuple(
            _candidate(
                candidate_id=f"gap-{page_number}",
                page_number=page_number,
                raw_text="STANDARD HEADER",
                bbox=(50.0, 20.0, 300.0, 34.0),
            )
            for page_number in (1, 3, 5)
        )
        self.assertEqual(classify_pdf_page_furniture(nonadjacent), {})

        # The old visual rule accepted any box starting within 90pt.  The
        # shared proof requires the whole box to fit in the actual 10% band.
        outside_relative_margin = tuple(
            _candidate(
                candidate_id=f"middle-{page_number}",
                page_number=page_number,
                raw_text="STANDARD HEADER",
                bbox=(50.0, 75.0, 300.0, 89.0),
            )
            for page_number in (1, 2, 3)
        )
        self.assertEqual(classify_pdf_page_furniture(outside_relative_margin), {})

    def test_dynamic_page_syntax_and_separate_total_use_fixed_offset(self) -> None:
        explicit = tuple(
            _candidate(
                candidate_id=f"page-of-{page_number}",
                page_number=page_number,
                raw_text=f"Page {page_number} of 4",
                bbox=(250.0, 760.0, 350.0, 774.0),
            )
            for page_number in (1, 2, 3, 4)
        )
        explicit_result = classify_pdf_page_furniture(explicit)
        self.assertEqual(set(explicit_result), {item.candidate_id for item in explicit})
        self.assertTrue(
            all(
                kind is PdfPageFurnitureKind.FOOTER for kind in explicit_result.values()
            )
        )

        separate = tuple(
            candidate
            for page_number in (1, 2, 3, 4)
            for candidate in (
                _candidate(
                    candidate_id=f"current-{page_number}",
                    page_number=page_number,
                    page_order=0,
                    raw_text=str(page_number),
                    bbox=(250.0, 748.0, 280.0, 760.0),
                ),
                _candidate(
                    candidate_id=f"total-{page_number}",
                    page_number=page_number,
                    page_order=1,
                    raw_text="4",
                    bbox=(250.0, 764.0, 280.0, 776.0),
                ),
            )
        )
        self.assertEqual(
            set(classify_pdf_page_furniture(separate)),
            {item.candidate_id for item in separate},
        )

    def test_advancing_bare_number_with_fixed_header_prose_stays_body(self) -> None:
        candidates = tuple(
            _candidate(
                candidate_id=f"title-{page_number}",
                page_number=page_number,
                raw_text=f"{page_number}\nTest stage",
                bbox=(50.0, 20.0, 300.0, 50.0),
            )
            for page_number in (1, 2, 3, 4)
        )
        self.assertEqual(classify_pdf_page_furniture(candidates), {})

    def test_different_marker_forms_or_totals_cannot_combine_proof(self) -> None:
        mixed_forms = tuple(
            _candidate(
                candidate_id=f"mixed-{page_number}",
                page_number=page_number,
                raw_text=raw_text,
                bbox=(250.0, 760.0, 350.0, 774.0),
            )
            for page_number, raw_text in (
                (1, "Page 1 of 10"),
                (2, "第 2 页"),
                (3, "Page 3 of 999"),
            )
        )
        self.assertEqual(classify_pdf_page_furniture(mixed_forms), {})

        changing_total = tuple(
            _candidate(
                candidate_id=f"total-change-{page_number}",
                page_number=page_number,
                raw_text=f"Page {page_number} of {total}",
                bbox=(250.0, 760.0, 350.0, 774.0),
            )
            for page_number, total in ((1, 10), (2, 10), (3, 11))
        )
        self.assertEqual(classify_pdf_page_furniture(changing_total), {})

    def test_series_duplicates_and_unstable_offsets_cannot_combine_proof(
        self,
    ) -> None:
        mixed_series = tuple(
            PdfPageFurnitureCandidate(
                candidate_id=f"series-{page_number}",
                page_number=page_number,
                page_order=0,
                raw_text="FIXED FOOTER",
                bbox=(50.0, 760.0, 300.0, 774.0),
                page_bbox=_PAGE_BBOX,
                series_key="provider-a" if page_number < 3 else "provider-b",
            )
            for page_number in (1, 2, 3)
        )
        self.assertEqual(classify_pdf_page_furniture(mixed_series), {})

        duplicate_on_middle_page = tuple(
            _candidate(
                candidate_id=f"duplicate-{page_number}-{page_order}",
                page_number=page_number,
                page_order=page_order,
                raw_text="FIXED FOOTER",
                bbox=(50.0, 760.0, 300.0, 774.0),
            )
            for page_number, page_orders in ((1, (0,)), (2, (0, 1)), (3, (0,)))
            for page_order in page_orders
        )
        self.assertEqual(
            classify_pdf_page_furniture(duplicate_on_middle_page),
            {},
        )

        unstable_offset = tuple(
            _candidate(
                candidate_id=f"offset-{page_number}",
                page_number=page_number,
                raw_text=f"Page {printed_number}",
                bbox=(250.0, 760.0, 350.0, 774.0),
            )
            for page_number, printed_number in ((1, 1), (2, 2), (3, 9), (4, 4))
        )
        self.assertEqual(classify_pdf_page_furniture(unstable_offset), {})

    def test_nfkc_roman_and_mirrored_positions_share_generic_proof(self) -> None:
        fixed = tuple(
            _candidate(
                candidate_id=f"nfkc-{page_number}",
                page_number=page_number,
                raw_text=("ＳＴＡＮＤＡＲＤ" if page_number == 1 else "STANDARD"),
                bbox=(
                    30.0 if page_number % 2 else 400.0,
                    20.0,
                    200.0 if page_number % 2 else 570.0,
                    34.0,
                ),
            )
            for page_number in (1, 2, 3)
        )
        self.assertEqual(
            set(classify_pdf_page_furniture(fixed)),
            {item.candidate_id for item in fixed},
        )

        roman = tuple(
            _candidate(
                candidate_id=f"roman-{page_number}",
                page_number=page_number,
                raw_text=marker,
                bbox=(250.0, 760.0, 350.0, 774.0),
            )
            for page_number, marker in ((1, "I"), (2, "II"), (3, "III"))
        )
        self.assertEqual(
            set(classify_pdf_page_furniture(roman)),
            {item.candidate_id for item in roman},
        )

    def test_native_furniture_fact_is_consumed_without_visual_reproof(self) -> None:
        source = _native_header_source()
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        document_view = build_document_view(source, tree)
        header_node = next(node for node in tree.nodes if node.source_refs)
        annotation = next(
            item
            for item in semantics.annotations
            if item.target_structure_node_id == header_node.structure_node_id
        )

        self.assertIs(header_node.kind, StructureNodeKind.CONTENT)
        self.assertIs(
            header_node.boundary_basis,
            StructureBoundaryBasis.PAGE_FURNITURE,
        )
        self.assertIs(annotation.semantic_class, SemanticClass.NOISE)
        self.assertNotIn(
            "4.1 ADAPTER-PROVEN HEADER",
            json.dumps(document_view["document_outline"], ensure_ascii=False),
        )

    def test_structure_and_semantics_consume_the_same_visual_proof(self) -> None:
        source = _visual_source()
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        document_view = build_document_view(source, tree)
        header_ids = {
            observation.observation_id
            for snapshot in source.visual_text_snapshots
            for observation in snapshot.observations
            if observation.raw_text == "4.1 STANDARD HEADER"
        }
        header_nodes = tuple(
            node
            for node in tree.nodes
            if any(
                ref.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
                and ref.source_id in header_ids
                for ref in node.source_refs
            )
        )
        header_structure_ids = {node.structure_node_id for node in header_nodes}
        header_annotations = tuple(
            annotation
            for annotation in semantics.annotations
            if annotation.target_structure_node_id in header_structure_ids
        )

        self.assertEqual(len(header_structure_ids), 3)
        self.assertTrue(
            all(
                node.kind is StructureNodeKind.CONTENT
                and node.boundary_basis is StructureBoundaryBasis.PAGE_FURNITURE
                and node.explicit_label is None
                and node.list_ordinal is None
                for node in header_nodes
            )
        )
        self.assertEqual(len(header_annotations), 3)
        self.assertTrue(
            all(
                annotation.semantic_class is SemanticClass.NOISE
                for annotation in header_annotations
            )
        )
        self.assertNotIn(
            "4.1 STANDARD HEADER",
            json.dumps(document_view["document_outline"], ensure_ascii=False),
        )


def _candidate(
    *,
    candidate_id: str,
    page_number: int,
    raw_text: str,
    bbox: tuple[float, float, float, float],
    page_order: int = 0,
) -> PdfPageFurnitureCandidate[str]:
    return PdfPageFurnitureCandidate(
        candidate_id=candidate_id,
        page_number=page_number,
        page_order=page_order,
        raw_text=raw_text,
        bbox=bbox,
        page_bbox=_PAGE_BBOX,
    )


def _visual_source() -> SourceIRSnapshot:
    provider_id = "fixture-visual-provider"
    provider_version = "1.0.0"
    snapshots: list[VisualTextSnapshot] = []
    for page_number in (1, 2, 3):
        page_image_sha256 = f"{page_number}" * 64
        lines = (
            ("4.1 STANDARD HEADER", (50.0, 20.0, 300.0, 34.0)),
            (f"{page_number}.1 Body", (50.0, 120.0, 300.0, 134.0)),
        )
        observations: list[VisualTextObservation] = []
        for observation_index, (raw_text, bbox) in enumerate(lines):
            observations.append(
                VisualTextObservation(
                    observation_id=stable_visual_text_observation_id(
                        source_sha256=_SOURCE_SHA256,
                        page_image_sha256=page_image_sha256,
                        provider_id=provider_id,
                        provider_version=provider_version,
                        page_number=page_number,
                        observation_index=observation_index,
                        bbox=bbox,
                    ),
                    observation_index=observation_index,
                    source_sha256=_SOURCE_SHA256,
                    page_number=page_number,
                    bbox=bbox,
                    raw_text=raw_text,
                    confidence=0.95,
                    provider_id=provider_id,
                    provider_version=provider_version,
                    page_image_sha256=page_image_sha256,
                )
            )
        snapshots.append(
            VisualTextSnapshot(
                visual_text_snapshot_id=stable_visual_text_snapshot_id(
                    source_sha256=_SOURCE_SHA256,
                    page_image_sha256=page_image_sha256,
                    provider_id=provider_id,
                    provider_version=provider_version,
                    page_number=page_number,
                    page_bbox=_PAGE_BBOX,
                    page_image_width_px=1200,
                    page_image_height_px=1600,
                ),
                source_sha256=_SOURCE_SHA256,
                provider_id=provider_id,
                provider_version=provider_version,
                page_number=page_number,
                page_bbox=_PAGE_BBOX,
                page_image_sha256=page_image_sha256,
                page_image_width_px=1200,
                page_image_height_px=1600,
                observations=tuple(observations),
            )
        )
    document_id = stable_document_id(_SOURCE_SHA256)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=_SOURCE_SHA256,
            adapter_id="fixture-pdf",
            adapter_version="1.0.0",
        ),
        document_id=document_id,
        source_sha256=_SOURCE_SHA256,
        adapter_id="fixture-pdf",
        adapter_version="1.0.0",
        status=SourceIRStatus.PARTIAL,
        visual_text_snapshots=tuple(snapshots),
        created_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )


def _native_header_source() -> SourceIRSnapshot:
    adapter_id = "fixture-pdf"
    adapter_version = "1.0.0"
    locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
        bbox=(50.0, 20.0, 300.0, 34.0),
    )
    node = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=_SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            kind=SourceNodeKind.HEADER,
            native_locator=locator,
        ),
        kind=SourceNodeKind.HEADER,
        source_order=0,
        raw_text="4.1 ADAPTER-PROVEN HEADER",
        native_locator=locator,
    )
    document_id = stable_document_id(_SOURCE_SHA256)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=_SOURCE_SHA256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=_SOURCE_SHA256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=(node,),
        created_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )


if __name__ == "__main__":
    unittest.main()
