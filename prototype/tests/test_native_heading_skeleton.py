from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from datetime import UTC, datetime
import unittest

from pydantic import ValidationError

from app.application.agent_workflow.native_heading_skeleton import (
    NativeHeadingCandidateState,
    NativeHeadingScopeKind,
    NativeHeadingSkeletonArtifactV1,
    build_native_heading_skeleton,
)
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


class NativeHeadingSkeletonTests(unittest.TestCase):
    def test_builds_chapter_windows_and_keeps_uncertain_numbered_text(self) -> None:
        source_ir = _source_ir(
            (
                _node(0, "1\n页眉伪标题", kind=SourceNodeKind.HEADER),
                _node(1, "2026\n02 10", x=220.0),
                _node(2, "1\n目录项", kind=SourceNodeKind.TOC_ENTRY),
                _node(3, "1\n说明", page=9),
                _node(4, "1.1\n文档范围", page=9),
                _node(5, "11\n表格业务行", page=9, x=85.0),
                _node(6, "20\n周的，视为长周期物料", page=9),
                _node(7, "2\n产品信息", page=10),
                _node(8, "2.1\n项目范围", page=10),
                _node(9, "1.2\n跨章伪标题", page=10),
            )
        )

        artifact = build_native_heading_skeleton(source_ir)

        self.assertEqual(artifact.dominant_left_x, 70.0)
        self.assertEqual(len(artifact.candidates), 8)
        self.assertEqual(len(artifact.proposed_candidate_ids), 4)
        self.assertEqual(len(artifact.review_required_candidate_ids), 4)
        self.assertEqual(
            tuple(item.kind for item in artifact.scopes),
            (
                NativeHeadingScopeKind.PREFACE,
                NativeHeadingScopeKind.CHAPTER,
                NativeHeadingScopeKind.CHAPTER,
            ),
        )
        by_number = {item.number: item for item in artifact.candidates}
        self.assertEqual(
            by_number["1.1"].parent_candidate_id,
            by_number["1"].candidate_id,
        )
        self.assertEqual(
            by_number["2.1"].parent_candidate_id,
            by_number["2"].candidate_id,
        )
        self.assertEqual(
            by_number["20"].state,
            NativeHeadingCandidateState.REVIEW_REQUIRED,
        )
        self.assertIn("top_level_sequence_break", by_number["20"].reason_codes)
        self.assertEqual(
            by_number["11"].state,
            NativeHeadingCandidateState.REVIEW_REQUIRED,
        )
        self.assertIn("non_dominant_left_geometry", by_number["11"].reason_codes)
        cross_chapter = next(
            item for item in artifact.candidates if item.title == "跨章伪标题"
        )
        self.assertEqual(
            cross_chapter.state,
            NativeHeadingCandidateState.REVIEW_REQUIRED,
        )
        self.assertIn(
            "parent_heading_outside_current_chapter",
            cross_chapter.reason_codes,
        )
        self.assertEqual(
            build_native_heading_skeleton(source_ir),
            artifact,
        )

    def test_rejects_empty_and_tampered_skeletons(self) -> None:
        with self.assertRaisesRegex(ValueError, "no numbered candidates"):
            build_native_heading_skeleton(
                _source_ir((_node(0, "普通正文，没有编号标题"),))
            )
        artifact = build_native_heading_skeleton(
            _source_ir((_node(0, "1\n说明"),))
        )
        payload = artifact.model_dump(mode="json")
        payload["artifact_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "SHA drifted"):
            NativeHeadingSkeletonArtifactV1.model_validate(payload)

    def test_large_chapter_splits_only_at_declared_section_roots(self) -> None:
        nodes = (_node(0, "1\n技术要求"),) + tuple(
            _node(index, f"1.{index}\n完整子章节 {index}", page=9 + index // 20)
            for index in range(1, 66)
        )

        artifact = build_native_heading_skeleton(_source_ir(nodes))

        chapter_scopes = tuple(
            item
            for item in artifact.scopes
            if item.kind is NativeHeadingScopeKind.CHAPTER
        )
        section_scopes = tuple(
            item
            for item in artifact.scopes
            if item.kind is NativeHeadingScopeKind.SECTION
        )
        self.assertEqual(len(chapter_scopes), 1)
        self.assertEqual(len(chapter_scopes[0].candidate_ids), 1)
        self.assertEqual(len(section_scopes), 65)
        self.assertTrue(all(len(item.candidate_ids) == 1 for item in section_scopes))
        self.assertEqual(
            {
                candidate_id
                for scope in artifact.scopes
                for candidate_id in scope.candidate_ids
            },
            {item.candidate_id for item in artifact.candidates},
        )


def _source_ir(nodes: tuple[SourceNode, ...]) -> SourceIRSnapshot:
    source_sha256 = "a" * 64
    document_id = stable_document_id(source_sha256)
    adapter_id = "native-pdf-test"
    adapter_version = "1.0.0"
    canonical_nodes = tuple(
        node.model_copy(
            update={
                "source_node_id": stable_node_id(
                    source_sha256=source_sha256,
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                    kind=node.kind,
                    native_locator=node.native_locator,
                )
            }
        )
        for node in nodes
    )
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=source_sha256,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=canonical_nodes,
        assets=(),
        diagnostics=(),
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
    )


def _node(
    order: int,
    text: str,
    *,
    page: int = 1,
    x: float = 70.0,
    kind: SourceNodeKind = SourceNodeKind.PARAGRAPH,
) -> SourceNode:
    return SourceNode(
        source_node_id=f"node-heading-{order}",
        kind=kind,
        source_order=order,
        raw_text=text,
        native_locator=NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page,
            block_index=order,
            bbox=(x, 80.0 + order * 20.0, x + 200.0, 95.0 + order * 20.0),
        ),
    )


if __name__ == "__main__":
    unittest.main()
