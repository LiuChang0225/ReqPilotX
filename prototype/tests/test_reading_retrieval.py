from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import datetime, timezone

from app.application.reading_retrieval import assemble_reading_context
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
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
)
from app.domain.reading import ReadingSourceRefV1


SOURCE_SHA256 = "d" * 64
ADAPTER_ID = "reading-retrieval-test"
ADAPTER_VERSION = "1.0"


def _snapshot(count: int = 30) -> SourceIRSnapshot:
    document_id = stable_document_id(SOURCE_SHA256)
    nodes = []
    for order in range(count):
        page = order + 1
        kind = SourceNodeKind.HEADING if order % 5 == 0 else SourceNodeKind.PARAGRAPH
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page,
            block_index=order,
            bbox=(20.0, 20.0, 400.0, 40.0),
        )
        text = (
            "第十章 额定工况和连续运行要求 pump rated operating point"
            if order == 17
            else f"第 {page} 页一般说明 section {page} hydraulic system"
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_SHA256,
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    kind=kind,
                    native_locator=locator,
                ),
                kind=kind,
                source_order=order,
                raw_text=text,
                native_locator=locator,
            )
        )
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_SHA256,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=SOURCE_SHA256,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )


class ReadingRetrievalTests(unittest.TestCase):
    def test_mixed_chinese_and_latin_query_selects_exact_current_source(self) -> None:
        source_ir = _snapshot()
        result = assemble_reading_context(
            source_ir,
            question="额定工况 rated operating point 是什么？",
        )
        self.assertEqual(result.retrieval.coverage_mode, "FULL_DOCUMENT")
        self.assertIn(18, result.retrieval.covered_page_numbers)
        target = next(item for item in result.chunks if "额定工况" in item.text)
        source = next(
            item for item in source_ir.nodes if item.source_node_id == target.source_node_id
        )
        self.assertEqual(
            target.text,
            source.raw_text[target.start_offset : target.end_offset],
        )

    def test_document_summary_uses_the_complete_ordered_document(self) -> None:
        result = assemble_reading_context(
            _snapshot(60),
            question="请总结整份文档的主要内容和章节结构",
        )
        self.assertEqual(result.retrieval.coverage_mode, "FULL_DOCUMENT")
        pages = result.retrieval.covered_page_numbers
        self.assertEqual(len(pages), 60)
        self.assertIn(1, pages)
        self.assertIn(60, pages)
        self.assertEqual(result.retrieval.selected_node_count, 60)
        self.assertEqual(result.retrieval.document_node_count, 60)
        self.assertEqual(
            tuple(item.citation_id for item in result.chunks),
            tuple(f"S{index}" for index in range(1, len(result.chunks) + 1)),
        )

    def test_explicit_overview_intent_covers_all_four_reader_shortcuts(self) -> None:
        prompts = (
            "请生成这份文档的阅读概览。",
            "请提炼这份文档最关键的内容。",
            "请像带我第一次阅读一样解释它。",
            "请将这份文档的核心内容翻译为中文。",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                result = assemble_reading_context(
                    _snapshot(40),
                    question=prompt,
                    coverage_intent="DOCUMENT_OVERVIEW",
                )
                self.assertEqual(
                    result.retrieval.coverage_mode,
                    "FULL_DOCUMENT",
                )
                self.assertIn(1, result.retrieval.covered_page_numbers)
                self.assertIn(40, result.retrieval.covered_page_numbers)

    def test_pronoun_followup_uses_three_recent_user_topics(self) -> None:
        result = assemble_reading_context(
            _snapshot(),
            question="它具体意味着什么？",
            recent_user_text=(
                "第一轮：额定工况是什么？\n"
                "第二轮：连续运行有什么条件？\n"
                "第三轮：rated operating point 在哪里定义？"
            ),
        )
        self.assertIn(18, result.retrieval.covered_page_numbers)
        self.assertTrue(any("额定工况" in item.text for item in result.chunks))

    def test_explicit_selection_is_preserved_even_without_lexical_overlap(self) -> None:
        source_ir = _snapshot()
        selected_node = source_ir.nodes[3]
        result = assemble_reading_context(
            source_ir,
            question="解释这个选区",
            source_ref=ReadingSourceRefV1(
                source_node_id=selected_node.source_node_id,
                start_offset=0,
                end_offset=5,
            ),
        )
        selected = next(
            item
            for item in result.chunks
            if item.source_node_id == selected_node.source_node_id
            and item.start_offset == 0
            and item.end_offset == 5
        )
        self.assertEqual(selected.text, selected_node.raw_text[:5])

    def test_all_table_cells_are_included_with_exact_cell_locators(self) -> None:
        source_ir = _snapshot(2)
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=3,
            table_index=0,
            bbox=(20.0, 50.0, 400.0, 140.0),
        )
        table_id = "table-reading-full-context"
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, column),
                row=row,
                column=column,
                raw_text=text,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=3,
                    table_index=0,
                    row_index=row,
                    column_index=column,
                ),
            )
            for row, column, text in ((0, 0, "参数"), (0, 1, "限值"), (1, 0, "电压"), (1, 1, "14 V"))
        )
        table = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_SHA256,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                kind=SourceNodeKind.TABLE,
                native_locator=locator,
            ),
            kind=SourceNodeKind.TABLE,
            source_order=2,
            raw_text="",
            native_locator=locator,
            table_payload=TableIR(
                table_id=table_id,
                row_count=2,
                column_count=2,
                cells=cells,
            ),
        )
        source_ir = source_ir.model_copy(update={"nodes": (*source_ir.nodes, table)})
        result = assemble_reading_context(source_ir, question="表格说了什么？")
        table_chunks = [item for item in result.chunks if item.locator_kind == "TABLE_CELL"]
        self.assertEqual(len(table_chunks), 4)
        self.assertEqual([item.text for item in table_chunks], ["参数", "限值", "电压", "14 V"])
        self.assertTrue(all(item.source_node_id == table.source_node_id for item in table_chunks))
        self.assertEqual(
            [(item.table_row, item.table_column) for item in table_chunks],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )
        self.assertTrue(
            all(
                item.table_row_span == 1 and item.table_column_span == 1
                for item in table_chunks
            )
        )
        self.assertEqual(result.retrieval.selected_node_count, 3)

    def test_oversize_document_fails_closed_instead_of_sampling(self) -> None:
        from app.application.reading_retrieval import FullDocumentTooLarge

        with self.assertRaises(FullDocumentTooLarge):
            assemble_reading_context(
                _snapshot(4),
                question="总结全文",
                direct_input_token_budget=16,
            )

    def test_more_than_six_thousand_context_chunks_fails_closed(self) -> None:
        from app.application.reading_retrieval import FullDocumentTooLarge

        with self.assertRaisesRegex(FullDocumentTooLarge, "chunk count"):
            assemble_reading_context(
                _snapshot(6_001),
                question="总结全文",
            )

    def test_selected_source_ranges_are_rebuilt_and_capped_at_twelve_thousand(self) -> None:
        source_ir = _snapshot(1)
        node = source_ir.nodes[0].model_copy(update={"raw_text": "选" * 12_001})
        source_ir = source_ir.model_copy(update={"nodes": (node,)})
        with self.assertRaisesRegex(ValueError, "selected source exceeds"):
            assemble_reading_context(
                source_ir,
                question="解释选区",
                source_refs=(
                    ReadingSourceRefV1(
                        source_node_id=node.source_node_id,
                        start_offset=0,
                        end_offset=12_001,
                    ),
                ),
            )

    def test_direct_budget_includes_request_and_per_chunk_metadata_reserves(self) -> None:
        from app.application.reading_retrieval import FullDocumentTooLarge

        with self.assertRaises(FullDocumentTooLarge):
            assemble_reading_context(
                _snapshot(1),
                question="总结全文",
                direct_input_token_budget=64_000,
            )


if __name__ == "__main__":
    unittest.main()
