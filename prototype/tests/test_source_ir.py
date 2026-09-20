from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
import hashlib
from datetime import datetime, timezone

from pydantic import ValidationError

from app.domain.extraction.source_ir import (
    DocxListMetadata,
    MergedRegion,
    NativeDocumentFormat,
    NativeLocator,
    SourceAssetDerivation,
    SourceAssetDerivationKind,
    SourceAssetKind,
    SourceAssetRef,
    SourceDiagnostic,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
    stable_table_id,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextPageInput,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)


SOURCE_HASH = "a" * 64
ADAPTER_ID = "native-pdf"
ADAPTER_VERSION = "1.0.0"
CREATED_AT = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)


class NativeLocatorTests(unittest.TestCase):
    def test_pdf_docx_and_xlsx_paths_are_expressible(self) -> None:
        pdf_block = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            block_index=4,
        )
        pdf_region = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            bbox=(10.0, 20.0, 100.0, 120.0),
        )
        pdf_table = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            table_index=1,
            bbox=(10.0, 20.0, 100.0, 120.0),
        )
        pdf_cell = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            table_index=1,
            row_index=3,
            column_index=4,
            bbox=(20.0, 30.0, 40.0, 50.0),
        )
        docx_paragraph = NativeLocator(
            format=NativeDocumentFormat.DOCX,
            body_element_index=8,
            paragraph_index=6,
        )
        docx_cell_paragraph = NativeLocator(
            format=NativeDocumentFormat.DOCX,
            body_element_index=9,
            table_index=1,
            row_index=2,
            column_index=3,
            paragraph_index=0,
            object_index=1,
        )
        xlsx_range = NativeLocator(
            format=NativeDocumentFormat.XLSX,
            workbook="xl/workbook.xml",
            sheet="Requirements",
            cell_range="$B$4:D8",
        )

        self.assertEqual(pdf_block.block_index, 4)
        self.assertEqual(pdf_region.bbox, (10.0, 20.0, 100.0, 120.0))
        self.assertEqual(pdf_table.table_index, 1)
        self.assertEqual((pdf_cell.row_index, pdf_cell.column_index), (3, 4))
        self.assertEqual(docx_paragraph.paragraph_index, 6)
        self.assertEqual(docx_cell_paragraph.column_index, 3)
        self.assertEqual(docx_cell_paragraph.object_index, 1)
        self.assertEqual(xlsx_range.cell_range, "$B$4:D8")

    def test_format_specific_paths_fail_closed(self) -> None:
        invalid_payloads = (
            {"format": NativeDocumentFormat.PDF, "page_number": 1},
            {
                "format": NativeDocumentFormat.PDF,
                "page_number": 1,
                "block_index": 0,
                "sheet": "Requirements",
            },
            {
                "format": NativeDocumentFormat.PDF,
                "page_number": 1,
                "block_index": 0,
                "table_index": 0,
            },
            {
                "format": NativeDocumentFormat.PDF,
                "page_number": 1,
                "row_index": 0,
                "column_index": 0,
            },
            {
                "format": NativeDocumentFormat.PDF,
                "page_number": 1,
                "table_index": 0,
                "row_index": 0,
            },
            {
                "format": NativeDocumentFormat.PDF,
                "page_number": 1,
                "table_index": 0,
                "body_element_index": 0,
            },
            {
                "format": NativeDocumentFormat.DOCX,
                "body_element_index": 0,
            },
            {
                "format": NativeDocumentFormat.DOCX,
                "body_element_index": 0,
                "table_index": 0,
                "row_index": 1,
            },
            {
                "format": NativeDocumentFormat.DOCX,
                "body_element_index": 0,
                "table_index": 0,
                "object_index": 0,
            },
            {
                "format": NativeDocumentFormat.XLSX,
                "workbook": "xl/workbook.xml",
                "sheet": "Requirements",
            },
            {
                "format": NativeDocumentFormat.XLSX,
                "workbook": "xl/workbook.xml",
                "sheet": "Requirements",
                "cell_range": "not-a-range",
            },
            {
                "format": NativeDocumentFormat.XLSX,
                "workbook": "xl/workbook.xml",
                "sheet": "Requirements",
                "cell_range": "A1",
                "table_index": 0,
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                NativeLocator(**payload)  # type: ignore[arg-type]

    def test_unknown_fields_and_invalid_geometry_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "provider_ref"):
            NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                block_index=0,
                provider_ref="vendor-123",  # type: ignore[call-arg]
            )
        with self.assertRaisesRegex(ValidationError, "bbox"):
            NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                bbox=(100.0, 20.0, 10.0, 30.0),
            )


class StableIdentityTests(unittest.TestCase):
    def test_document_and_snapshot_ids_are_stable_and_strict(self) -> None:
        document_id = stable_document_id(SOURCE_HASH)
        first = stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        )
        second = stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        )

        self.assertEqual(document_id, f"doc-{'a' * 32}")
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            stable_document_id("A" * 64)
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            stable_document_id("a" * 63)

    def test_asset_storage_key_is_portable_and_never_an_absolute_customer_path(self) -> None:
        for invalid_key in (
            "/Users/example/customer/image.png",
            "../outside/image.png",
            "source-assets/../outside.png",
            "C:/customer/image.png",
            "source-assets\\image.png",
        ):
            with self.subTest(storage_key=invalid_key), self.assertRaisesRegex(
                ValidationError,
                "storage_key",
            ):
                payload = _asset().model_dump()
                payload["storage_key"] = invalid_key
                SourceAssetRef.model_validate(payload)

    def test_alpha_mask_derivation_is_strict_and_serialized_only_when_present(
        self,
    ) -> None:
        asset = _asset()
        ordinary = SourceNode(
            source_node_id=_node_id(SourceNodeKind.IMAGE, _pdf_locator()),
            kind=SourceNodeKind.IMAGE,
            source_order=0,
            native_locator=_pdf_locator(),
            asset_ref=asset.asset_id,
        )
        self.assertNotIn("asset_derivation", ordinary.model_dump(mode="json"))

        derived = ordinary.model_copy(
            update={
                "asset_derivation": SourceAssetDerivation(
                    kind=SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE,
                    base_content_sha256="c" * 64,
                    mask_content_sha256="d" * 64,
                )
            }
        )
        self.assertEqual(
            derived.model_dump(mode="json")["asset_derivation"]["kind"],
            "alpha_mask_composite",
        )

        invalid = ordinary.model_dump(mode="json")
        invalid["kind"] = SourceNodeKind.PARAGRAPH.value
        invalid["asset_derivation"] = derived.model_dump(mode="json")[
            "asset_derivation"
        ]
        with self.assertRaisesRegex(ValidationError, "asset_derivation"):
            SourceNode.model_validate(invalid)

        jpeg_asset = asset.model_copy(update={"media_type": "image/jpeg"})
        with self.assertRaisesRegex(ValidationError, "PNG image asset"):
            _snapshot((derived,), assets=(jpeg_asset,))

    def test_node_identity_uses_native_location_and_never_text(self) -> None:
        first_locator = _pdf_locator(block_index=1)
        second_locator = _pdf_locator(block_index=2)
        first_id = _node_id(SourceNodeKind.PARAGRAPH, first_locator)
        repeated = SourceNode(
            source_node_id=first_id,
            kind=SourceNodeKind.PARAGRAPH,
            source_order=0,
            raw_text="重复文字",
            native_locator=first_locator,
        )
        cleaned = SourceNode(
            source_node_id=first_id,
            kind=SourceNodeKind.PARAGRAPH,
            source_order=0,
            raw_text="重复 文字",
            native_locator=first_locator,
        )

        self.assertEqual(repeated.source_node_id, cleaned.source_node_id)
        self.assertNotEqual(first_id, _node_id(SourceNodeKind.PARAGRAPH, second_locator))
        self.assertNotEqual(first_id, _node_id(SourceNodeKind.HEADING, first_locator))

    def test_table_and_cell_ids_are_stable(self) -> None:
        locator = _pdf_locator(block_index=7)
        table_id = _table_id(locator)

        self.assertEqual(table_id, _table_id(locator))
        self.assertEqual(stable_cell_id(table_id, 0, 0), stable_cell_id(table_id, 0, 0))
        self.assertNotEqual(stable_cell_id(table_id, 0, 0), stable_cell_id(table_id, 0, 1))
        with self.assertRaisesRegex(ValueError, "must not be negative"):
            stable_cell_id(table_id, -1, 0)


class DocxListMetadataTests(unittest.TestCase):
    def test_direct_num_id_and_ilvl_are_strict_frozen_facts(self) -> None:
        metadata = DocxListMetadata(num_id=42, ilvl=2)

        self.assertEqual(metadata.num_id, 42)
        self.assertEqual(metadata.ilvl, 2)
        self.assertNotIn("lvl_text", DocxListMetadata.model_fields)
        with self.assertRaises(ValidationError):
            metadata.ilvl = 3  # type: ignore[misc]

    def test_missing_inferred_and_invalid_values_are_rejected(self) -> None:
        invalid_payloads = (
            {},
            {"num_id": "42"},
            {"ilvl": -1},
            {"num_id": True},
            {"num_id": 42, "lvl_text": "%1."},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                DocxListMetadata.model_validate(payload)

    def test_metadata_is_allowed_only_on_docx_list_item_nodes(self) -> None:
        locator = NativeLocator(
            format=NativeDocumentFormat.DOCX,
            body_element_index=2,
            table_index=0,
            row_index=1,
            column_index=0,
            paragraph_index=3,
        )
        metadata = DocxListMetadata(num_id=7, ilvl=1)
        valid = SourceNode(
            source_node_id=_node_id(SourceNodeKind.LIST_ITEM, locator),
            kind=SourceNodeKind.LIST_ITEM,
            source_order=0,
            raw_text="直接编号段落",
            native_locator=locator,
            docx_list_metadata=metadata,
        )
        self.assertEqual(valid.docx_list_metadata, metadata)

        with self.assertRaisesRegex(ValidationError, "only for DOCX list-item"):
            SourceNode(
                source_node_id=_node_id(SourceNodeKind.PARAGRAPH, locator),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=0,
                raw_text="普通段落",
                native_locator=locator,
                docx_list_metadata=metadata,
            )
        pdf_locator = _pdf_locator()
        with self.assertRaisesRegex(ValidationError, "only for DOCX list-item"):
            SourceNode(
                source_node_id=_node_id(SourceNodeKind.LIST_ITEM, pdf_locator),
                kind=SourceNodeKind.LIST_ITEM,
                source_order=0,
                raw_text="PDF 列表段落",
                native_locator=pdf_locator,
                docx_list_metadata=metadata,
            )


class TableIRTests(unittest.TestCase):
    def test_merged_text_is_stored_only_in_the_spanning_anchor(self) -> None:
        table = _valid_table()

        self.assertEqual(len(table.cells), 3)
        self.assertEqual(table.cells[0].raw_text, "共享验收条件")
        self.assertEqual(table.cells[0].column_span, 2)
        self.assertEqual(table.merged_regions[0], MergedRegion(row=0, column=0, column_span=2))

    def test_covered_merged_cell_cannot_duplicate_anchor_text(self) -> None:
        table_id = _table_id(_pdf_locator(block_index=7))
        anchor = _cell(
            table_id,
            row=0,
            column=0,
            raw_text="共享条件",
            column_span=2,
        )
        duplicated_covered_cell = _cell(
            table_id,
            row=0,
            column=1,
            raw_text="共享条件",
        )

        with self.assertRaisesRegex(ValidationError, "must not overlap"):
            TableIR(
                table_id=table_id,
                row_count=1,
                column_count=2,
                cells=(anchor, duplicated_covered_cell),
                merged_regions=(MergedRegion(row=0, column=0, column_span=2),),
            )

    def test_spans_must_be_in_bounds_and_match_merged_regions(self) -> None:
        table_id = _table_id(_pdf_locator(block_index=7))
        spanning = _cell(table_id, row=0, column=0, column_span=2)

        with self.assertRaisesRegex(ValidationError, "matching merged region"):
            TableIR(
                table_id=table_id,
                row_count=1,
                column_count=2,
                cells=(spanning,),
            )
        with self.assertRaisesRegex(ValidationError, "exceeds column_count"):
            TableIR(
                table_id=table_id,
                row_count=1,
                column_count=1,
                cells=(spanning,),
                merged_regions=(MergedRegion(row=0, column=0, column_span=2),),
            )

    def test_cell_id_must_match_its_table_and_anchor(self) -> None:
        table_id = _table_id(_pdf_locator(block_index=7))
        invalid_cell = TableCellIR(
            cell_id=stable_cell_id(table_id, 0, 1),
            row=0,
            column=0,
            raw_text="材料",
            native_locator=_pdf_locator(block_index=8),
        )

        with self.assertRaisesRegex(ValidationError, "cell_id"):
            TableIR(
                table_id=table_id,
                row_count=1,
                column_count=1,
                cells=(invalid_cell,),
            )


class SourceIRSnapshotTests(unittest.TestCase):
    def test_diagnostic_evidence_hash_is_optional_but_serialized_when_present(
        self,
    ) -> None:
        ordinary = SourceDiagnostic(code="ordinary", message="ordinary diagnostic")
        evidenced = SourceDiagnostic(
            code="evidenced",
            message="content-bound diagnostic",
            evidence_sha256="b" * 64,
        )

        self.assertNotIn("evidence_sha256", ordinary.model_dump(mode="json"))
        self.assertEqual(
            evidenced.model_dump(mode="json")["evidence_sha256"],
            "b" * 64,
        )
        with self.assertRaisesRegex(ValidationError, "evidence_sha256"):
            SourceDiagnostic(
                code="invalid-evidence",
                message="invalid content evidence",
                evidence_sha256="not-a-sha256",
            )

    def test_snapshot_and_nested_collections_are_immutable(self) -> None:
        snapshot = _snapshot((_paragraph_node(),))

        with self.assertRaises(ValidationError):
            snapshot.status = SourceIRStatus.PARTIAL  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            snapshot.nodes.append(_paragraph_node())  # type: ignore[attr-defined]
        with self.assertRaises(ValidationError):
            snapshot.nodes[0].raw_text = "changed"  # type: ignore[misc]

    def test_same_source_and_adapter_inputs_create_the_same_contract(self) -> None:
        first = _snapshot((_paragraph_node(raw_text="原文"),))
        second = _snapshot((_paragraph_node(raw_text="原文"),))

        self.assertEqual(first, second)
        self.assertEqual(first.source_ir_id, second.source_ir_id)
        self.assertEqual(first.nodes[0].source_node_id, second.nodes[0].source_node_id)

    def test_source_hash_and_unknown_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "source_sha256"):
            SourceIRSnapshot(
                source_ir_id="sir-invalid",
                document_id="doc-invalid",
                source_sha256="A" * 64,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                status=SourceIRStatus.FAILED,
                created_at=CREATED_AT,
            )
        with self.assertRaisesRegex(ValidationError, "source_sha256"):
            SourceIRSnapshot(
                source_ir_id="sir-invalid",
                document_id="doc-invalid",
                source_sha256=b"a" * 64,  # type: ignore[arg-type]
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                status=SourceIRStatus.FAILED,
                created_at=CREATED_AT,
            )
        valid = _snapshot((_paragraph_node(),))
        payload = valid.model_dump(mode="python")
        payload["provider_payload"] = {"raw": True}
        with self.assertRaisesRegex(ValidationError, "provider_payload"):
            SourceIRSnapshot.model_validate(payload)

    def test_source_order_is_unique_nonnegative_and_canonical(self) -> None:
        first = _paragraph_node(source_order=0, block_index=1)
        duplicate_order = _paragraph_node(source_order=0, block_index=2)
        earlier = _paragraph_node(source_order=1, block_index=1)
        later = _paragraph_node(source_order=0, block_index=2)

        with self.assertRaisesRegex(ValidationError, "source_order must be unique"):
            _snapshot((first, duplicate_order))
        with self.assertRaisesRegex(ValidationError, "ascending source_order"):
            _snapshot((earlier, later))
        with self.assertRaisesRegex(ValidationError, "greater than or equal to 0"):
            _paragraph_node(source_order=-1)

    def test_node_and_table_ids_are_verified_against_native_identity(self) -> None:
        canonical = _snapshot((_paragraph_node(),))
        invalid_document_id = "doc-arbitrary-business-id"
        with self.assertRaisesRegex(ValidationError, "document_id.*source_sha256"):
            SourceIRSnapshot(
                **{
                    **canonical.model_dump(),
                    "document_id": invalid_document_id,
                    "source_ir_id": stable_source_ir_id(
                        document_id=invalid_document_id,
                        source_sha256=SOURCE_HASH,
                        adapter_id=ADAPTER_ID,
                        adapter_version=ADAPTER_VERSION,
                    ),
                }
            )

        node = _paragraph_node()
        invalid_node = node.model_copy(update={"source_node_id": "node-from-text"})
        with self.assertRaisesRegex(ValidationError, "native source identity"):
            _snapshot((invalid_node,))

        table_node = _table_node()
        invalid_table = table_node.table_payload.model_copy(update={"table_id": "table-from-text"})
        invalid_table_node = table_node.model_copy(update={"table_payload": invalid_table})
        with self.assertRaisesRegex(ValidationError, "table_id"):
            _snapshot((invalid_table_node,))

    def test_parent_and_diagnostic_references_must_stay_in_snapshot(self) -> None:
        orphan = _paragraph_node().model_copy(update={"parent_source_node_id": "node-missing"})
        with self.assertRaisesRegex(ValidationError, "parent_source_node_id"):
            _snapshot((orphan,))

        diagnostic = SourceDiagnostic(
            code="low_confidence",
            message="Block needs review.",
            source_node_id="node-missing",
        )
        with self.assertRaisesRegex(ValidationError, "diagnostic source_node_id"):
            _snapshot((_paragraph_node(),), diagnostics=(diagnostic,))

    def test_parent_graph_rejects_self_loops_and_multi_node_cycles(self) -> None:
        first = _paragraph_node(source_order=0, block_index=1)
        second = _paragraph_node(source_order=1, block_index=2)

        self_loop = first.model_copy(
            update={"parent_source_node_id": first.source_node_id}
        )
        with self.assertRaisesRegex(ValidationError, "cannot be its own parent"):
            _snapshot((self_loop,))

        cyclic_first = first.model_copy(
            update={"parent_source_node_id": second.source_node_id}
        )
        cyclic_second = second.model_copy(
            update={"parent_source_node_id": first.source_node_id}
        )
        with self.assertRaisesRegex(ValidationError, "acyclic"):
            _snapshot((cyclic_first, cyclic_second))

    def test_visual_and_linked_diagnostic_locators_stay_in_node_scope(self) -> None:
        locator = _pdf_locator(block_index=1)
        with self.assertRaisesRegex(ValidationError, "visual_locator.*format"):
            SourceNode(
                source_node_id=_node_id(SourceNodeKind.PARAGRAPH, locator),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=0,
                raw_text="原文",
                native_locator=locator,
                visual_locator=NativeLocator(
                    format=NativeDocumentFormat.DOCX,
                    body_element_index=0,
                    paragraph_index=0,
                ),
            )

        node = _paragraph_node(block_index=1)
        wrong_page = SourceDiagnostic(
            code="wrong_page",
            message="This diagnostic is incorrectly linked across pages.",
            source_node_id=node.source_node_id,
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=2,
                block_index=1,
            ),
        )
        with self.assertRaisesRegex(ValidationError, "source node PDF page"):
            _snapshot((node,), diagnostics=(wrong_page,))

        wrong_format = SourceDiagnostic(
            code="wrong_format",
            message="This nested diagnostic has the wrong native format.",
            source_node_id=node.source_node_id,
            native_locator=NativeLocator(
                format=NativeDocumentFormat.XLSX,
                workbook="xl/workbook.xml",
                sheet="Requirements",
                cell_range="A1",
            ),
        )
        node_with_diagnostic = node.model_copy(
            update={"extraction_diagnostics": (wrong_format,)}
        )
        with self.assertRaisesRegex(ValidationError, "native_locator format"):
            _snapshot((node_with_diagnostic,))

    def test_snapshot_rejects_mixed_node_asset_and_top_diagnostic_formats(self) -> None:
        pdf_node = _paragraph_node()
        xlsx_locator = NativeLocator(
            format=NativeDocumentFormat.XLSX,
            workbook="xl/workbook.xml",
            sheet="Requirements",
            cell_range="A1",
        )
        xlsx_node = SourceNode(
            source_node_id=_node_id(SourceNodeKind.PARAGRAPH, xlsx_locator),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=1,
            raw_text="跨格式节点",
            native_locator=xlsx_locator,
        )
        with self.assertRaisesRegex(ValidationError, "one document format"):
            _snapshot((pdf_node, xlsx_node))

        pdf_asset = _asset()
        mixed_asset = pdf_asset.model_copy(
            update={"native_locator": xlsx_locator}
        )
        image_locator = _pdf_locator(block_index=10)
        image_node = SourceNode(
            source_node_id=_node_id(SourceNodeKind.IMAGE, image_locator),
            kind=SourceNodeKind.IMAGE,
            source_order=0,
            native_locator=image_locator,
            asset_ref=mixed_asset.asset_id,
        )
        with self.assertRaisesRegex(ValidationError, "one document format"):
            _snapshot((image_node,), assets=(mixed_asset,))

        mixed_diagnostic = SourceDiagnostic(
            code="mixed_format",
            message="A top-level diagnostic cannot point into another container format.",
            native_locator=xlsx_locator,
        )
        with self.assertRaisesRegex(ValidationError, "one document format"):
            _snapshot((pdf_node,), diagnostics=(mixed_diagnostic,))

    def test_table_cell_locators_match_table_scope_and_anchor(self) -> None:
        valid_pairs = (
            (
                NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=2,
                    table_index=3,
                ),
                NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=2,
                    table_index=3,
                    row_index=0,
                    column_index=0,
                ),
            ),
            (
                NativeLocator(
                    format=NativeDocumentFormat.DOCX,
                    body_element_index=5,
                    table_index=2,
                ),
                NativeLocator(
                    format=NativeDocumentFormat.DOCX,
                    body_element_index=5,
                    table_index=2,
                    row_index=0,
                    column_index=0,
                ),
            ),
            (
                NativeLocator(
                    format=NativeDocumentFormat.XLSX,
                    workbook="xl/workbook.xml",
                    sheet="Requirements",
                    cell_range="$B$4:D8",
                ),
                NativeLocator(
                    format=NativeDocumentFormat.XLSX,
                    workbook="xl/workbook.xml",
                    sheet="Requirements",
                    cell_range="B4",
                ),
            ),
        )
        for table_locator, cell_locator in valid_pairs:
            with self.subTest(format=table_locator.format):
                snapshot = _snapshot(
                    (_single_cell_table_node(table_locator, cell_locator),)
                )
                self.assertEqual(snapshot.nodes[0].table_payload.cells[0].row, 0)

        invalid_pairs = (
            (
                NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=2,
                    table_index=3,
                ),
                NativeLocator(
                    format=NativeDocumentFormat.DOCX,
                    body_element_index=5,
                    table_index=2,
                    row_index=0,
                    column_index=0,
                ),
                "format",
            ),
            (
                NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=2,
                    table_index=3,
                ),
                NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=3,
                    table_index=3,
                    row_index=0,
                    column_index=0,
                ),
                "page and table_index",
            ),
            (
                NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=2,
                    table_index=3,
                ),
                NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=2,
                    table_index=4,
                    row_index=0,
                    column_index=0,
                ),
                "page and table_index",
            ),
            (
                NativeLocator(
                    format=NativeDocumentFormat.DOCX,
                    body_element_index=5,
                    table_index=2,
                ),
                NativeLocator(
                    format=NativeDocumentFormat.DOCX,
                    body_element_index=5,
                    table_index=2,
                    row_index=1,
                    column_index=0,
                ),
                "row/column",
            ),
            (
                NativeLocator(
                    format=NativeDocumentFormat.DOCX,
                    body_element_index=5,
                    table_index=2,
                ),
                NativeLocator(
                    format=NativeDocumentFormat.DOCX,
                    body_element_index=6,
                    table_index=2,
                    row_index=0,
                    column_index=0,
                ),
                "body element and table_index",
            ),
            (
                NativeLocator(
                    format=NativeDocumentFormat.XLSX,
                    workbook="xl/workbook.xml",
                    sheet="Requirements",
                    cell_range="$B$4:D8",
                ),
                NativeLocator(
                    format=NativeDocumentFormat.XLSX,
                    workbook="xl/workbook.xml",
                    sheet="Other",
                    cell_range="B4",
                ),
                "workbook and sheet",
            ),
            (
                NativeLocator(
                    format=NativeDocumentFormat.XLSX,
                    workbook="xl/workbook.xml",
                    sheet="Requirements",
                    cell_range="$B$4:D8",
                ),
                NativeLocator(
                    format=NativeDocumentFormat.XLSX,
                    workbook="xl/workbook.xml",
                    sheet="Requirements",
                    cell_range="C4",
                ),
                "coordinate",
            ),
        )
        for table_locator, cell_locator, error in invalid_pairs:
            with self.subTest(error=error), self.assertRaisesRegex(ValidationError, error):
                _snapshot((_single_cell_table_node(table_locator, cell_locator),))

    def test_textless_table_and_image_are_preserved_with_payloads(self) -> None:
        table_node = _table_node()
        asset = _asset()
        image_locator = _pdf_locator(block_index=10)
        image_node = SourceNode(
            source_node_id=_node_id(SourceNodeKind.IMAGE, image_locator),
            kind=SourceNodeKind.IMAGE,
            source_order=1,
            raw_text="",
            native_locator=image_locator,
            asset_ref=asset.asset_id,
        )
        snapshot = _snapshot((table_node, image_node), assets=(asset,))

        self.assertEqual(snapshot.nodes[0].raw_text, "")
        self.assertEqual(snapshot.nodes[1].raw_text, "")
        with self.assertRaisesRegex(ValidationError, "textless nodes"):
            SourceNode(
                source_node_id=_node_id(SourceNodeKind.PARAGRAPH, _pdf_locator()),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=0,
                raw_text="",
                native_locator=_pdf_locator(),
            )
        with self.assertRaisesRegex(ValidationError, "require asset_ref"):
            SourceNode(
                source_node_id=_node_id(SourceNodeKind.IMAGE, _pdf_locator()),
                kind=SourceNodeKind.IMAGE,
                source_order=0,
                raw_text="caption",
                native_locator=_pdf_locator(),
            )

    def test_assets_and_table_renders_must_reference_snapshot_assets(self) -> None:
        missing_image = _asset().asset_id
        image_locator = _pdf_locator(block_index=10)
        image_node = SourceNode(
            source_node_id=_node_id(SourceNodeKind.IMAGE, image_locator),
            kind=SourceNodeKind.IMAGE,
            source_order=0,
            native_locator=image_locator,
            asset_ref=missing_image,
        )
        with self.assertRaisesRegex(ValidationError, "asset_ref"):
            _snapshot((image_node,))

        table_node = _table_node(visual_asset_ref="asset-missing")
        with self.assertRaisesRegex(ValidationError, "visual_asset_ref"):
            _snapshot((table_node,))

    def test_created_at_must_be_timezone_aware(self) -> None:
        with self.assertRaisesRegex(ValidationError, "timezone-aware"):
            _snapshot(
                (_paragraph_node(),),
                created_at=datetime(2026, 7, 20, 12, 0),
            )


class VisualTextFactTests(unittest.TestCase):
    def test_page_input_and_visual_facts_are_immutable_and_stably_grounded(self) -> None:
        source_bytes = b"approved source bytes"
        page_image_bytes = b"in-memory PNG bytes"
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        page_image_sha256 = hashlib.sha256(page_image_bytes).hexdigest()
        page_input = VisualTextPageInput(
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            page_number=2,
            page_bbox=(0.0, 0.0, 200.0, 300.0),
            page_image_bytes=page_image_bytes,
            page_image_sha256=page_image_sha256,
            page_image_width_px=400,
            page_image_height_px=600,
        )
        first = _visual_snapshot(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            page_number=2,
        )
        second = _visual_snapshot(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            page_number=2,
        )

        self.assertEqual(first, second)
        self.assertEqual(first.observations[0].confidence, 0.875)
        self.assertEqual(first.observations[0].bbox, (10.0, 20.0, 80.0, 40.0))
        self.assertNotIn(source_bytes, repr(page_input).encode("utf-8"))
        self.assertNotIn(page_image_bytes, repr(page_input).encode("utf-8"))
        self.assertNotIn("source_bytes", page_input.model_dump())
        self.assertNotIn("page_image_bytes", page_input.model_dump())
        with self.assertRaises(ValidationError):
            first.observations[0].raw_text = "changed"  # type: ignore[misc]

    def test_visual_fact_ids_confidence_and_page_grounding_fail_closed(self) -> None:
        visual = _visual_snapshot(
            source_sha256=SOURCE_HASH,
            page_image_sha256="c" * 64,
        )
        observation_payload = visual.observations[0].model_dump()
        observation_payload["observation_id"] = "vto-arbitrary"
        with self.assertRaisesRegex(ValidationError, "observation_id"):
            VisualTextObservation.model_validate(observation_payload)

        observation_payload = visual.observations[0].model_dump()
        observation_payload["confidence"] = 1.01
        with self.assertRaisesRegex(ValidationError, "less than or equal to 1"):
            VisualTextObservation.model_validate(observation_payload)

        snapshot_payload = visual.model_dump()
        snapshot_payload["page_bbox"] = (0.0, 0.0, 20.0, 20.0)
        snapshot_payload["visual_text_snapshot_id"] = stable_visual_text_snapshot_id(
            source_sha256=SOURCE_HASH,
            page_image_sha256="c" * 64,
            provider_id="test-vision",
            provider_version="1.0.0",
            page_number=1,
            page_bbox=(0.0, 0.0, 20.0, 20.0),
            page_image_width_px=400,
            page_image_height_px=600,
        )
        with self.assertRaisesRegex(ValidationError, "bbox must stay"):
            VisualTextSnapshot.model_validate(snapshot_payload)

    def test_source_ir_validates_visual_snapshot_source_and_canonical_order(self) -> None:
        first = _visual_snapshot(
            source_sha256=SOURCE_HASH,
            page_image_sha256="c" * 64,
            page_number=1,
        )
        second = _visual_snapshot(
            source_sha256=SOURCE_HASH,
            page_image_sha256="d" * 64,
            page_number=2,
        )
        snapshot = _snapshot(
            (_paragraph_node(),),
            visual_text_snapshots=(first, second),
        )
        self.assertEqual(snapshot.visual_text_snapshots, (first, second))
        self.assertIn(
            "visual_text_snapshots",
            snapshot.model_dump(mode="json"),
        )
        self.assertNotIn(
            "visual_text_snapshots",
            _snapshot((_paragraph_node(),)).model_dump(mode="json"),
        )

        with self.assertRaisesRegex(ValidationError, "canonical page order"):
            _snapshot(
                (_paragraph_node(),),
                visual_text_snapshots=(second, first),
            )

        wrong_source = _visual_snapshot(
            source_sha256="f" * 64,
            page_image_sha256="e" * 64,
        )
        with self.assertRaisesRegex(ValidationError, "must match SourceIR"):
            _snapshot(
                (_paragraph_node(),),
                visual_text_snapshots=(wrong_source,),
            )


def _pdf_locator(*, block_index: int = 0) -> NativeLocator:
    return NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=block_index,
    )


def _node_id(kind: SourceNodeKind, locator: NativeLocator) -> str:
    return stable_node_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        kind=kind,
        native_locator=locator,
    )


def _table_id(locator: NativeLocator) -> str:
    return stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=locator,
    )


def _paragraph_node(
    *,
    source_order: int = 0,
    block_index: int = 0,
    raw_text: str = "系统应满足验收要求。",
) -> SourceNode:
    locator = _pdf_locator(block_index=block_index)
    return SourceNode(
        source_node_id=_node_id(SourceNodeKind.PARAGRAPH, locator),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=source_order,
        raw_text=raw_text,
        native_locator=locator,
    )


def _cell(
    table_id: str,
    *,
    row: int,
    column: int,
    raw_text: str = "",
    row_span: int = 1,
    column_span: int = 1,
    native_locator: NativeLocator | None = None,
) -> TableCellIR:
    return TableCellIR(
        cell_id=stable_cell_id(table_id, row, column),
        row=row,
        column=column,
        row_span=row_span,
        column_span=column_span,
        raw_text=raw_text,
        native_locator=(
            native_locator
            if native_locator is not None
            else _pdf_locator(block_index=20 + row * 10 + column)
        ),
    )


def _valid_table(*, visual_asset_ref: str | None = None) -> TableIR:
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
    )
    table_id = _table_id(table_locator)
    merged = MergedRegion(row=0, column=0, column_span=2)
    return TableIR(
        table_id=table_id,
        row_count=2,
        column_count=2,
        header_regions=(merged,),
        cells=(
            _cell(
                table_id,
                row=0,
                column=0,
                raw_text="共享验收条件",
                column_span=2,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    table_index=0,
                    row_index=0,
                    column_index=0,
                ),
            ),
            _cell(
                table_id,
                row=1,
                column=0,
                raw_text="材料 A",
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    table_index=0,
                    row_index=1,
                    column_index=0,
                ),
            ),
            _cell(
                table_id,
                row=1,
                column=1,
                raw_text="≥ 100 MPa",
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    table_index=0,
                    row_index=1,
                    column_index=1,
                ),
            ),
        ),
        merged_regions=(merged,),
        visual_asset_ref=visual_asset_ref,
    )


def _table_node(*, visual_asset_ref: str | None = None) -> SourceNode:
    locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
    )
    return SourceNode(
        source_node_id=_node_id(SourceNodeKind.TABLE, locator),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        raw_text="",
        native_locator=locator,
        table_payload=_valid_table(visual_asset_ref=visual_asset_ref),
    )


def _single_cell_table_node(
    table_locator: NativeLocator,
    cell_locator: NativeLocator,
) -> SourceNode:
    table_id = _table_id(table_locator)
    table = TableIR(
        table_id=table_id,
        row_count=1,
        column_count=1,
        cells=(
            TableCellIR(
                cell_id=stable_cell_id(table_id, 0, 0),
                row=0,
                column=0,
                raw_text="要求",
                native_locator=cell_locator,
            ),
        ),
    )
    return SourceNode(
        source_node_id=_node_id(SourceNodeKind.TABLE, table_locator),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        native_locator=table_locator,
        table_payload=table,
    )


def _asset() -> SourceAssetRef:
    return SourceAssetRef(
        asset_id="asset-page-1-image-1",
        kind=SourceAssetKind.IMAGE,
        content_sha256="b" * 64,
        media_type="image/png",
        storage_key="source-assets/a/page-1-image-1.png",
        native_locator=_pdf_locator(block_index=10),
    )


def _visual_snapshot(
    *,
    source_sha256: str,
    page_image_sha256: str,
    page_number: int = 1,
) -> VisualTextSnapshot:
    provider_id = "test-vision"
    provider_version = "1.0.0"
    page_bbox = (0.0, 0.0, 200.0, 300.0)
    observation_bbox = (10.0, 20.0, 80.0, 40.0)
    observation = VisualTextObservation(
        observation_id=stable_visual_text_observation_id(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            observation_index=0,
            bbox=observation_bbox,
        ),
        observation_index=0,
        source_sha256=source_sha256,
        page_number=page_number,
        bbox=observation_bbox,
        raw_text="visual fact",
        confidence=0.875,
        provider_id=provider_id,
        provider_version=provider_version,
        page_image_sha256=page_image_sha256,
    )
    return VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            page_bbox=page_bbox,
            page_image_width_px=400,
            page_image_height_px=600,
        ),
        source_sha256=source_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_number,
        page_bbox=page_bbox,
        page_image_sha256=page_image_sha256,
        page_image_width_px=400,
        page_image_height_px=600,
        observations=(observation,),
    )


def _snapshot(
    nodes: tuple[SourceNode, ...],
    *,
    assets: tuple[SourceAssetRef, ...] = (),
    visual_text_snapshots: tuple[VisualTextSnapshot, ...] = (),
    diagnostics: tuple[SourceDiagnostic, ...] = (),
    created_at: datetime = CREATED_AT,
) -> SourceIRSnapshot:
    document_id = stable_document_id(SOURCE_HASH)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=nodes,
        assets=assets,
        visual_text_snapshots=visual_text_snapshots,
        diagnostics=diagnostics,
        created_at=created_at,
    )


if __name__ == "__main__":
    unittest.main()
