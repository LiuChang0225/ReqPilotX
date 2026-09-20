from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import unittest
from datetime import datetime, timezone

from app.application.reading_projection import (
    build_reading_projection,
    build_reading_source_page,
    source_slice_for_ref,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceDiagnostic,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
)


SOURCE_SHA256 = "a" * 64
ADAPTER_ID = "reading-test-adapter"
ADAPTER_VERSION = "1.0"


def _locator(*, order: int, page: int) -> NativeLocator:
    return NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=page,
        block_index=order,
        bbox=(20.0, 20.0 + order, 400.0, 40.0 + order),
    )


def _node_id(*, kind: SourceNodeKind, order: int, page: int) -> str:
    return stable_node_id(
        source_sha256=SOURCE_SHA256,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        kind=kind,
        native_locator=_locator(order=order, page=page),
    )


def _node(
    *,
    kind: SourceNodeKind,
    order: int,
    page: int,
    text: str,
) -> SourceNode:
    return SourceNode(
        source_node_id=_node_id(kind=kind, order=order, page=page),
        kind=kind,
        source_order=order,
        raw_text=text,
        native_locator=_locator(order=order, page=page),
    )


def _source_ir() -> SourceIRSnapshot:
    document_id = stable_document_id(SOURCE_SHA256)
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
        nodes=(
            _node(
                kind=SourceNodeKind.HEADING,
                order=0,
                page=1,
                text="1 系统概述",
            ),
            _node(
                kind=SourceNodeKind.PARAGRAPH,
                order=1,
                page=1,
                text="泵组在额定工况下连续运行。",
            ),
            _node(
                kind=SourceNodeKind.HEADING,
                order=2,
                page=2,
                text="附录 A 术语",
            ),
        ),
        diagnostics=(
            SourceDiagnostic(
                code="LOW_TEXT_COVERAGE",
                message="Page coverage should be reviewed.",
                source_node_id=_node_id(
                    kind=SourceNodeKind.PARAGRAPH,
                    order=1,
                    page=1,
                ),
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    block_index=1,
                ),
            ),
        ),
        created_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )


class ReadingProjectionTests(unittest.TestCase):
    def test_projection_is_source_bound_and_requirement_free(self) -> None:
        projection = build_reading_projection(_source_ir())

        self.assertEqual(projection["overview"]["document_format"], "pdf")
        self.assertEqual(
            projection["overview"]["document_type_candidate"]["label"],
            "类型待判定",
        )
        self.assertEqual(projection["overview"]["page_count"], 2)
        self.assertEqual(
            [item["title"] for item in projection["structure_map"]["nodes"]],
            ["1 系统概述", "附录 A 术语"],
        )
        self.assertEqual(
            projection["overview"]["appendix_candidate_ranges"][0]["page_start"],
            2,
        )
        self.assertEqual(
            projection["structure_map"]["nodes"][0]["page_end"],
            1,
        )
        self.assertEqual(
            projection["overview"]["risk_regions"][0]["reason_code"],
            "LOW_TEXT_COVERAGE",
        )
        serialized = json.dumps(projection, ensure_ascii=False).lower()
        for forbidden in (
            "requirement_id",
            "is_requirement",
            "priority",
            "compliance",
            "acceptance_criteria",
            "handoff",
            "eligible_for",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_source_page_returns_exact_text_only_for_requested_page(self) -> None:
        page = build_reading_source_page(_source_ir(), page_number=1)

        self.assertEqual(page["source_sha256"], SOURCE_SHA256)
        self.assertEqual(
            [item["source_node_id"] for item in page["nodes"]],
            [
                _node_id(kind=SourceNodeKind.HEADING, order=0, page=1),
                _node_id(kind=SourceNodeKind.PARAGRAPH, order=1, page=1),
            ],
        )
        self.assertEqual(page["nodes"][1]["text"], "泵组在额定工况下连续运行。")

    def test_source_slice_rebuilds_offsets_and_fails_closed(self) -> None:
        source_ir = _source_ir()
        text = source_slice_for_ref(
            source_ir,
            source_node_id=_node_id(
                kind=SourceNodeKind.PARAGRAPH,
                order=1,
                page=1,
            ),
            start_offset=0,
            end_offset=2,
        )
        self.assertEqual(text, "泵组")

        with self.assertRaisesRegex(ValueError, "outside"):
            source_slice_for_ref(
                source_ir,
                source_node_id=_node_id(
                    kind=SourceNodeKind.PARAGRAPH,
                    order=1,
                    page=1,
                ),
                start_offset=0,
                end_offset=10_000,
            )
        with self.assertRaisesRegex(ValueError, "does not identify"):
            source_slice_for_ref(
                source_ir,
                source_node_id="node-from-another-document",
                start_offset=0,
                end_offset=2,
            )


if __name__ == "__main__":
    unittest.main()
