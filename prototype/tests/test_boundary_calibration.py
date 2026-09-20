from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import UTC, datetime

from app.application.extraction.boundary_calibration import (
    _boundary_presentations,
    build_boundary_calibration_page,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import (
    apply_structure_patch,
    create_structure_patch_proposal,
    get_base_effective_structure,
    validate_structure_patch_proposal,
)
from app.application.extraction.serialize_structure_workbench import (
    _persisted_source_spans_by_node,
)
from app.application.extraction.structure_presentation import (
    build_structure_presentation_index,
)
from app.domain.extraction.page_geometry import (
    PdfPageGeometryLineV1,
    PdfPageGeometrySourceIdentityV1,
    create_pdf_page_geometry_index,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    PdfTextLineGeometry,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
)
from app.domain.extraction.structure_revision import (
    BoundarySplitPartV1,
    BoundarySplitStructureOperationV1,
    StructureSourceSpanV1,
)


_SOURCE_SHA256 = "9" * 64
_ADAPTER_ID = "boundary-calibration-test"
_ADAPTER_VERSION = "1.0.0"
_TEXT = "first line\nsecond line\nthird line"
_SECOND_TEXT = "fourth line\nfifth line"


class BoundaryCalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_ir = _source_ir(SourceIRStatus.COMPLETE)
        self.base_tree = build_structure_tree(self.source_ir)
        self.base_effective = get_base_effective_structure(
            self.source_ir,
            self.base_tree,
        )
        self.source_node = self.source_ir.nodes[0]
        self.target = next(
            node
            for node in self.base_tree.nodes
            if node.identity_key == f"source:{self.source_node.source_node_id}"
        )
        self.geometry_page = _geometry_page(self.source_ir)

    def test_selected_leaf_exposes_only_verified_line_anchors(self) -> None:
        plan = build_boundary_calibration_page(
            page_number=1,
            source_ir=self.source_ir,
            effective=self.base_effective,
            geometry_page=self.geometry_page,
            selected_node_id=self.target.structure_node_id,
            editable=True,
        )

        split = next(
            boundary
            for boundary in plan["boundaries"]
            if boundary["mode"] == "SPLIT"
        )
        self.assertTrue(split["editable"])
        self.assertEqual(
            [anchor["offset"] for anchor in split["anchors"]],
            [10, 22],
        )
        self.assertFalse(
            plan["capabilities"]["supports_free_pixel_boundary"]
        )
        self.assertEqual(
            plan["binding"]["source_sha256"],
            self.source_ir.source_sha256,
        )

    def test_split_projection_exposes_movable_adjacent_boundary(self) -> None:
        source_ref = self.target.source_refs[0]
        operation = BoundarySplitStructureOperationV1(
            structure_node_id=self.target.structure_node_id,
            parts=(
                BoundarySplitPartV1(
                    source_spans=(
                        StructureSourceSpanV1(
                            source_ref=source_ref,
                            start_offset=0,
                            end_offset=10,
                        ),
                    )
                ),
                BoundarySplitPartV1(
                    source_spans=(
                        StructureSourceSpanV1(
                            source_ref=source_ref,
                            start_offset=10,
                            end_offset=len(_TEXT),
                        ),
                    )
                ),
            ),
        )
        proposal = create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            (),
            (operation,),
        )
        patch = validate_structure_patch_proposal(
            proposal,
            self.source_ir,
            self.base_tree,
            (),
        )
        effective = apply_structure_patch(
            patch,
            self.source_ir,
            self.base_tree,
            (),
        ).effective_snapshot

        plan = build_boundary_calibration_page(
            page_number=1,
            source_ir=self.source_ir,
            effective=effective,
            geometry_page=self.geometry_page,
            editable=True,
        )

        boundary = next(
            item
            for item in plan["boundaries"]
            if item["mode"] == "RESEGMENT"
        )
        self.assertEqual(boundary["current_offset"], 10)
        self.assertEqual(
            [anchor["offset"] for anchor in boundary["anchors"]],
            [10, 22],
        )
        self.assertTrue(boundary["editable"])
        self.assertEqual(len(plan["segments"]), 2)
        self.assertEqual(
            [
                (segment["start_offset"], segment["end_offset"])
                for segment in plan["segments"]
            ],
            [(0, 10), (10, len(_TEXT))],
        )

    def test_partial_source_is_visible_but_fail_closed_for_editing(self) -> None:
        partial_source = _source_ir(SourceIRStatus.PARTIAL)
        base_tree = build_structure_tree(partial_source)
        effective = get_base_effective_structure(partial_source, base_tree)
        target = next(
            node
            for node in base_tree.nodes
            if node.identity_key
            == f"source:{partial_source.nodes[0].source_node_id}"
        )

        plan = build_boundary_calibration_page(
            page_number=1,
            source_ir=partial_source,
            effective=effective,
            geometry_page=_geometry_page(partial_source),
            selected_node_id=target.structure_node_id,
            editable=True,
        )

        self.assertEqual(plan["source_status"], "PARTIAL")
        self.assertTrue(plan["boundaries"])
        self.assertTrue(
            all(not boundary["editable"] for boundary in plan["boundaries"])
        )

    def test_cross_source_boundaries_remain_visible_and_locked(self) -> None:
        source_ir = _multi_node_source_ir()
        base_tree = build_structure_tree(source_ir)
        effective = get_base_effective_structure(source_ir, base_tree)

        plan = build_boundary_calibration_page(
            page_number=1,
            source_ir=source_ir,
            effective=effective,
            geometry_page=_geometry_page(source_ir),
            editable=True,
        )

        boundary = next(
            item
            for item in plan["boundaries"]
            if item["state"] == "CURRENT_LOCKED"
        )
        self.assertEqual(boundary["mode"], "RESEGMENT")
        self.assertIn(
            boundary["locked_reason"],
            {"CROSS_SOURCE_BOUNDARY", "NON_PARENT_RELATION_BOUND"},
        )
        self.assertEqual(boundary["current_offset"], len(_TEXT))
        self.assertFalse(boundary["editable"])
        self.assertEqual(len(boundary["anchors"]), 1)

    def test_persisted_effective_spans_are_validated_for_read_projection(
        self,
    ) -> None:
        payload = [
            binding.model_dump(mode="json")
            for binding in self.base_effective.node_source_spans
        ]

        projected = _persisted_source_spans_by_node(payload)

        self.assertEqual(
            set(projected),
            {
                node.structure_node_id
                for node in self.base_effective.nodes
            },
        )
        corrupted = [*payload, dict(payload[0])]
        self.assertEqual(_persisted_source_spans_by_node(corrupted), {})

    def test_boundary_presentations_fail_closed_when_projection_is_stale(
        self,
    ) -> None:
        current_index = build_structure_presentation_index(
            self.source_ir,
            structure=self.base_effective,
        )

        presentations, current = _boundary_presentations(
            source_ir=self.source_ir,
            effective=self.base_effective,
            payload={"structure_presentation_index": current_index},
        )

        self.assertTrue(current)
        target = next(
            item
            for item in presentations
            if item["id"] == self.target.structure_node_id
        )
        self.assertTrue(target["label"])
        self.assertGreaterEqual(len(target["path"]), 2)

        stale_index = {
            **current_index,
            "structure_snapshot_id": "structure-snapshot-stale",
        }
        _, stale = _boundary_presentations(
            source_ir=self.source_ir,
            effective=self.base_effective,
            payload={"structure_presentation_index": stale_index},
        )
        self.assertFalse(stale)


def _source_ir(status: SourceIRStatus) -> SourceIRSnapshot:
    locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
        bbox=(72.0, 80.0, 340.0, 142.0),
    )
    source_node_id = stable_node_id(
        source_sha256=_SOURCE_SHA256,
        adapter_id=_ADAPTER_ID,
        adapter_version=_ADAPTER_VERSION,
        kind=SourceNodeKind.PARAGRAPH,
        native_locator=locator,
    )
    document_id = stable_document_id(_SOURCE_SHA256)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=_SOURCE_SHA256,
            adapter_id=_ADAPTER_ID,
            adapter_version=_ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=_SOURCE_SHA256,
        adapter_id=_ADAPTER_ID,
        adapter_version=_ADAPTER_VERSION,
        status=status,
        nodes=(
            SourceNode(
                source_node_id=source_node_id,
                kind=SourceNodeKind.PARAGRAPH,
                source_order=0,
                raw_text=_TEXT,
                native_locator=locator,
                pdf_text_lines=(
                    PdfTextLineGeometry(
                        start_offset=0,
                        end_offset=10,
                        bbox=(72.0, 80.0, 180.0, 94.0),
                    ),
                    PdfTextLineGeometry(
                        start_offset=11,
                        end_offset=22,
                        bbox=(72.0, 102.0, 190.0, 116.0),
                    ),
                    PdfTextLineGeometry(
                        start_offset=23,
                        end_offset=len(_TEXT),
                        bbox=(72.0, 124.0, 176.0, 138.0),
                    ),
                ),
            ),
        ),
        created_at=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
    )


def _multi_node_source_ir() -> SourceIRSnapshot:
    source_ir = _source_ir(SourceIRStatus.COMPLETE)
    locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=1,
        bbox=(72.0, 160.0, 340.0, 204.0),
    )
    source_node_id = stable_node_id(
        source_sha256=_SOURCE_SHA256,
        adapter_id=_ADAPTER_ID,
        adapter_version=_ADAPTER_VERSION,
        kind=SourceNodeKind.PARAGRAPH,
        native_locator=locator,
    )
    second = SourceNode(
        source_node_id=source_node_id,
        kind=SourceNodeKind.PARAGRAPH,
        source_order=1,
        raw_text=_SECOND_TEXT,
        native_locator=locator,
        pdf_text_lines=(
            PdfTextLineGeometry(
                start_offset=0,
                end_offset=11,
                bbox=(72.0, 160.0, 182.0, 174.0),
            ),
            PdfTextLineGeometry(
                start_offset=12,
                end_offset=len(_SECOND_TEXT),
                bbox=(72.0, 182.0, 176.0, 196.0),
            ),
        ),
    )
    return source_ir.model_copy(
        update={"nodes": (*source_ir.nodes, second)}
    )


def _geometry_page(source_ir: SourceIRSnapshot):
    return create_pdf_page_geometry_index(
        source_identity=PdfPageGeometrySourceIdentityV1(
            source_sha256=source_ir.source_sha256,
            source_ir_id=source_ir.source_ir_id,
            source_ir_schema_version=source_ir.schema_version,
            adapter_id=source_ir.adapter_id,
            adapter_version=source_ir.adapter_version,
        ),
        page_number=1,
        page_width=595.0,
        page_height=842.0,
        rotation_cw_degrees=0,
        text_lines=tuple(
            PdfPageGeometryLineV1(
                source_node_id=source_node.source_node_id,
                source_text_length=len(source_node.raw_text),
                start_offset=line.start_offset,
                end_offset=line.end_offset,
                bbox=line.bbox,
                source_order=source_node.source_order * 1000 + index,
            )
            for source_node in source_ir.nodes
            for index, line in enumerate(source_node.pdf_text_lines)
        ),
    )


if __name__ == "__main__":
    unittest.main()
