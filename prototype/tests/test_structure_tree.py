from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from copy import deepcopy
from datetime import datetime, timezone

from pydantic import ValidationError

from app.application.extraction.build_shadow_preview import (
    build_document_view,
    build_page_comparison_view,
)
from app.application.extraction.build_semantic_unit_plan import (
    build_semantic_unit_plan,
)
from app.application.extraction.build_llm_shadow_preview import (
    _scoped_visual_incompleteness_cause_code,
)
from app.application.extraction.classify_content import classify_content
from app.application.extraction.resolve_structure import (
    DEFAULT_STRUCTURE_RESOLVER_VERSION,
    StructureResolutionRejected,
    StructureResolutionRejectionCode,
    _veto_native_continuation_section_labels,
    build_structure_tree,
    validate_structure_tree_against_source_ir,
)
from app.application.extraction.serialize_structure_workbench import (
    _node_label,
    _source_index,
)
from app.application.extraction.structure_boundary_rules import (
    alphabetic_pair_interstitial_evidence,
    alphabetic_pair_interstitial_is_transparent,
)
from app.application.extraction.structure_presentation import (
    STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
    build_structure_presentation_index,
    validated_structure_presentation_index,
)
from app.domain.extraction.source_ir import (
    DocxListMetadata,
    MergedRegion,
    NativeDocumentFormat,
    NativeLocator,
    SourceAlphaFootprint,
    SourceAlphaFootprintRun,
    SourceAssetDerivation,
    SourceAssetDerivationKind,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceAssetKind,
    SourceAssetRef,
    SourceDiagnostic,
    SourceDiagnosticSeverity,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_document_id,
    stable_node_id,
    stable_source_alpha_footprint_evidence_sha256,
    stable_source_ir_id,
    stable_table_id,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)
from app.domain.extraction.visual_table import (
    VisualTableCell,
    VisualTableSnapshot,
    stable_visual_table_cell_id,
    stable_visual_table_snapshot_id,
)
from app.domain.extraction.structure import (
    STRUCTURE_TREE_SCHEMA_VERSION,
    StructureBoundaryBasis,
    StructureFinding,
    StructureFindingSeverity,
    StructureNode,
    StructureNodeKind,
    StructureOptionState,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
    stable_structure_finding_id,
    stable_structure_node_id,
)


SOURCE_HASH = "b" * 64
ADAPTER_ID = "native-pdf-test"
ADAPTER_VERSION = "1.0.0"
CREATED_AT = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


class StructureTreeBaselineTests(unittest.TestCase):
    def test_build_is_deterministic_with_stable_ids_and_unique_order(self) -> None:
        source = _pdf_source_snapshot()

        first = build_structure_tree(source)
        second = build_structure_tree(source)

        self.assertEqual(first, second)
        self.assertEqual(first.schema_version, STRUCTURE_TREE_SCHEMA_VERSION)
        self.assertEqual(DEFAULT_STRUCTURE_RESOLVER_VERSION, "2.48.1")
        self.assertEqual(first.resolver_version, DEFAULT_STRUCTURE_RESOLVER_VERSION)
        self.assertEqual(first.structure_snapshot_id, second.structure_snapshot_id)
        self.assertEqual(
            [node.structure_node_id for node in first.nodes],
            [node.structure_node_id for node in second.nodes],
        )
        orders = [node.source_order for node in first.nodes]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(len(orders), len(set(orders)))

    def test_non_table_nodes_are_candidates_without_sentence_semantics_or_text_copy(
        self,
    ) -> None:
        source = _pdf_source_snapshot()
        tree = build_structure_tree(source)
        heading = source.nodes[0]
        paragraph = source.nodes[1]

        section = _node_for_source(tree, heading.source_node_id)
        content = _node_for_source(tree, paragraph.source_node_id)

        self.assertEqual(section.kind, StructureNodeKind.SECTION)
        self.assertEqual(section.boundary_basis, StructureBoundaryBasis.NATIVE_HEADING)
        self.assertEqual(content.kind, StructureNodeKind.CONTENT)
        self.assertEqual(content.boundary_basis, StructureBoundaryBasis.NATIVE_BLOCK)
        self.assertEqual(
            content.parent_structure_node_id,
            section.structure_node_id,
        )
        # Multiple natural paragraphs and a modal word remain one source-backed
        # content candidate. Classification belongs to the later semantic stage.
        source_content_nodes = [
            node
            for node in tree.nodes
            if node.identity_key == f"source:{paragraph.source_node_id}"
        ]
        self.assertEqual(len(source_content_nodes), 1)
        serialized = tree.model_dump_json()
        self.assertNotIn("必须完成第一步", serialized)
        self.assertNotIn("第二个自然段仍属同一块", serialized)

    def test_numbered_cross_reference_on_a_wrapped_line_stays_with_current_section(
        self,
    ) -> None:
        prior_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=10,
            block_index=0,
            bbox=(72.0, 80.0, 260.0, 94.0),
        )
        section_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=14,
            block_index=0,
            bbox=(72.0, 80.0, 260.0, 94.0),
        )
        body_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=14,
            block_index=1,
            bbox=(90.0, 110.0, 500.0, 124.0),
        )
        continuation_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=14,
            block_index=2,
            bbox=(90.0, 132.0, 180.0, 146.0),
        )
        nodes = (
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    prior_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=0,
                raw_text="2.4.4 已出现的真实标题",
                native_locator=prior_locator,
            ),
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    section_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=1,
                raw_text="2.5.4 随机振动试验",
                native_locator=section_locator,
            ),
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    body_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=2,
                raw_text="测试结果应满足",
                native_locator=body_locator,
            ),
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    continuation_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=3,
                raw_text="2.4.4 的要求。",
                native_locator=continuation_locator,
            ),
        )

        source = _source_snapshot(nodes=nodes)
        tree = build_structure_tree(source)
        prior = _node_for_source(tree, nodes[0].source_node_id)
        section = _node_for_source(tree, nodes[1].source_node_id)
        continuation = _node_for_source(tree, nodes[3].source_node_id)

        self.assertEqual(section.explicit_label, "2.5.4")
        self.assertEqual(continuation.kind, StructureNodeKind.CONTENT)
        self.assertIsNone(continuation.explicit_label)
        self.assertEqual(
            continuation.parent_structure_node_id,
            section.structure_node_id,
        )
        numbered_sections = [
            node for node in tree.nodes if node.explicit_label == "2.4.4"
        ]
        self.assertEqual(
            [node.structure_node_id for node in numbered_sections],
            [prior.structure_node_id],
        )

    def test_caption_like_body_references_preserve_physical_line_continuations(
        self,
    ) -> None:
        cases = (
            (
                "chinese-table-reference",
                "表1所要求文件或实物应由供应商提交并纳入项目开发计",
                "划。",
            ),
            (
                "english-table-reference",
                "Table 1 requirements shall apply to every component and the supplier shall",
                "retain the evidence.",
            ),
            (
                "chinese-figure-reference",
                "图2所示系统架构应由供应商完成并保留原始设",
                "计证据。",
            ),
            (
                "english-figure-reference",
                "Figure 2 shows the system architecture and the supplier shall",
                "retain the interface evidence.",
            ),
            (
                "compact-chinese-table-predicate",
                "表1要求供应商应保留完整开发记",
                "录。",
            ),
            (
                "compact-chinese-figure-predicate",
                "图2给出了系统接口及其验证边",
                "界。",
            ),
        )
        for case, first_text, continuation_text in cases:
            with self.subTest(case=case):
                nodes = tuple(
                    SourceNode(
                        source_node_id=_source_node_id(
                            SourceNodeKind.PARAGRAPH,
                            locator := NativeLocator(
                                format=NativeDocumentFormat.PDF,
                                page_number=17,
                                block_index=source_order,
                                bbox=(
                                    72.0 if source_order != 1 else 92.0,
                                    80.0 + source_order * 24.0,
                                    520.0 if source_order != 2 else 240.0,
                                    94.0 + source_order * 24.0,
                                ),
                            ),
                        ),
                        kind=SourceNodeKind.PARAGRAPH,
                        source_order=source_order,
                        raw_text=raw_text,
                        native_locator=locator,
                    )
                    for source_order, raw_text in enumerate(
                        ("5 Delivery scope", first_text, continuation_text)
                    )
                )

                tree = build_structure_tree(_source_snapshot(nodes=nodes))
                first = _node_for_source(tree, nodes[1].source_node_id)
                continuation = _node_for_source(tree, nodes[2].source_node_id)

                self.assertTrue(
                    any(
                        relation.kind is StructureRelationKind.CONTINUES
                        and relation.source_structure_node_id == first.structure_node_id
                        and relation.target_structure_node_id
                        == continuation.structure_node_id
                        for relation in tree.relations
                    )
                )

    def test_explicit_clause_owned_content_needs_wrapped_line_layout_evidence(
        self,
    ) -> None:
        cases = (
            (
                "physical-wrap",
                "1）供应商应提交完整的验证报",
                (92.0, 104.0, 520.0, 118.0),
                "告。",
                (72.0, 128.0, 92.0, 142.0),
                (),
                True,
            ),
            (
                "full-line-wrap-with-stable-margin",
                "1）供应商应提交完整的元器件统",
                (92.0, 104.0, 520.0, 118.0),
                "计清单以及全部必要说明。",
                (72.0, 128.0, 450.0, 142.0),
                (
                    ("背景说明一。", (72.0, 60.0, 520.0, 74.0)),
                    ("背景说明二。", (72.0, 80.0, 520.0, 94.0)),
                ),
                True,
            ),
            (
                "numbered-title-and-intro",
                "1）执行器环境耐久",
                (72.0, 104.0, 160.0, 118.0),
                "环境耐久试验过后满足如下要求：",
                (112.0, 128.0, 300.0, 142.0),
                (
                    ("背景说明一。", (72.0, 60.0, 520.0, 74.0)),
                    ("背景说明二。", (72.0, 80.0, 520.0, 94.0)),
                ),
                False,
            ),
            (
                "aligned-short-title-and-intro",
                "1）诊断开发相关规范",
                (72.0, 104.0, 300.0, 118.0),
                "通用诊断规范清单如下：",
                (72.0, 128.0, 240.0, 142.0),
                (),
                False,
            ),
        )
        for (
            case,
            clause_text,
            clause_bbox,
            content_text,
            content_bbox,
            peer_specs,
            expected_continuation,
        ) in cases:
            with self.subTest(case=case):
                specs = (
                    ("5 验证要求", (72.0, 40.0, 240.0, 54.0)),
                    *peer_specs,
                    (clause_text, clause_bbox),
                    (content_text, content_bbox),
                )
                nodes = tuple(
                    SourceNode(
                        source_node_id=_source_node_id(
                            SourceNodeKind.PARAGRAPH,
                            locator := NativeLocator(
                                format=NativeDocumentFormat.PDF,
                                page_number=17,
                                block_index=source_order,
                                bbox=bbox,
                            ),
                        ),
                        kind=SourceNodeKind.PARAGRAPH,
                        source_order=source_order,
                        raw_text=raw_text,
                        native_locator=locator,
                    )
                    for source_order, (raw_text, bbox) in enumerate(specs)
                )

                tree = build_structure_tree(_source_snapshot(nodes=nodes))
                clause = _node_for_source(tree, nodes[-2].source_node_id)
                content = _node_for_source(tree, nodes[-1].source_node_id)
                has_continuation = any(
                    relation.kind is StructureRelationKind.CONTINUES
                    and relation.source_structure_node_id
                    == clause.structure_node_id
                    and relation.target_structure_node_id
                    == content.structure_node_id
                    for relation in tree.relations
                )

                self.assertEqual(clause.kind, StructureNodeKind.CLAUSE)
                self.assertEqual(content.kind, StructureNodeKind.CONTENT)
                self.assertEqual(
                    content.parent_structure_node_id,
                    clause.structure_node_id,
                )
                self.assertEqual(has_continuation, expected_continuation)

    def test_high_confidence_table_and_figure_captions_break_physical_line_joining(
        self,
    ) -> None:
        cases = (
            ("table-caption", "表1 设计交付物定义", "交付物名称 交付阶段"),
            ("spaced-short-table-caption", "表1 要求", "项目 阶段"),
            ("spaced-reliability-caption", "表2 可靠性要求", "条件 等级"),
            (
                "figure-caption",
                "Figure 2 System architecture",
                "Controller Sensor Actuator",
            ),
        )
        for case, caption_text, following_text in cases:
            with self.subTest(case=case):
                nodes = tuple(
                    SourceNode(
                        source_node_id=_source_node_id(
                            SourceNodeKind.PARAGRAPH,
                            locator := NativeLocator(
                                format=NativeDocumentFormat.PDF,
                                page_number=17,
                                block_index=source_order,
                                bbox=(
                                    92.0,
                                    80.0 + source_order * 24.0,
                                    520.0,
                                    94.0 + source_order * 24.0,
                                ),
                            ),
                        ),
                        kind=SourceNodeKind.PARAGRAPH,
                        source_order=source_order,
                        raw_text=raw_text,
                        native_locator=locator,
                    )
                    for source_order, raw_text in enumerate(
                        ("5 Delivery scope", caption_text, following_text)
                    )
                )

                tree = build_structure_tree(_source_snapshot(nodes=nodes))
                caption = _node_for_source(tree, nodes[1].source_node_id)
                following = _node_for_source(tree, nodes[2].source_node_id)

                self.assertFalse(
                    any(
                        relation.kind is StructureRelationKind.CONTINUES
                        and relation.source_structure_node_id
                        == caption.structure_node_id
                        and relation.target_structure_node_id
                        == following.structure_node_id
                        for relation in tree.relations
                    )
                )

    def test_wrapped_operating_mode_value_is_not_promoted_to_section(self) -> None:
        specs = (
            (
                "6.3.3 温度试验",
                SourceNodeKind.PARAGRAPH,
                80.0,
                300.0,
            ),
            (
                "Before conducting this test, the DUT shall use operating mode ",
                SourceNodeKind.PARAGRAPH,
                110.0,
                540.0,
            ),
            (
                "2.1 to determine the stability time. If operating mode 2.1 is not applicable,",
                SourceNodeKind.PARAGRAPH,
                126.0,
                540.0,
            ),
        )
        nodes: list[SourceNode] = []
        for source_order, (raw_text, kind, y0, x1) in enumerate(specs):
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=96,
                block_index=source_order,
                bbox=(70.0, y0, x1, y0 + 11.0),
            )
            nodes.append(
                SourceNode(
                    source_node_id=_source_node_id(kind, locator),
                    kind=kind,
                    source_order=source_order,
                    raw_text=raw_text,
                    native_locator=locator,
                )
            )

        tree = build_structure_tree(_source_snapshot(nodes=tuple(nodes)))
        section = _node_for_source(tree, nodes[0].source_node_id)
        wrapped_value = _node_for_source(tree, nodes[2].source_node_id)

        self.assertEqual(section.explicit_label, "6.3.3")
        self.assertEqual(wrapped_value.kind, StructureNodeKind.CONTENT)
        self.assertIsNone(wrapped_value.explicit_label)
        self.assertEqual(
            wrapped_value.parent_structure_node_id,
            section.structure_node_id,
        )
        self.assertFalse(any(node.explicit_label == "2.1" for node in tree.nodes))

    def test_bare_dotted_label_with_bracketed_peer_and_adjacent_title_is_section(
        self,
    ) -> None:
        specs = (
            (8, 0, "3 Terms and definitions", (70.0, 660.0, 320.0, 672.0)),
            (8, 1, "3.6 Prior definition", (70.0, 700.0, 320.0, 712.0)),
            (8, 2, "Prior definition body.", (92.0, 724.0, 420.0, 736.0)),
            (8, 3, "3.7", (70.0, 754.0, 92.0, 766.0)),
            (
                8,
                4,
                "Operating-mode supply voltage",
                (92.0, 770.0, 360.0, 782.0),
            ),
            (
                9,
                0,
                "Definition body continues here\n3.8",
                (70.0, 72.0, 420.0, 100.0),
            ),
            (
                9,
                1,
                "Next definition body\n3.9",
                (70.0, 104.0, 420.0, 132.0),
            ),
            (
                9,
                2,
                "Third definition body\n3.10",
                (70.0, 136.0, 420.0, 164.0),
            ),
            (9, 3, "Fourth definition body.", (70.0, 168.0, 420.0, 180.0)),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=block_index,
                        bbox=bbox,
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (page_number, block_index, raw_text, bbox) in enumerate(
                specs
            )
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }
        title = _node_for_source(tree, nodes[4].source_node_id)
        cross_page_body = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.source_refs
            and node.source_refs[0].source_id == nodes[5].source_node_id
        )

        self.assertEqual(
            set(sections),
            {"3", "3.6", "3.7", "3.8", "3.9", "3.10"},
        )
        self.assertEqual(
            title.parent_structure_node_id,
            sections["3.7"].structure_node_id,
        )
        self.assertEqual(
            cross_page_body.parent_structure_node_id,
            sections["3.7"].structure_node_id,
        )

    def test_unbracketed_bare_decimal_and_distant_title_remain_content(self) -> None:
        specs = (
            (1, 0, "6.3.3 Test scope", (70.0, 80.0, 320.0, 92.0)),
            (1, 1, "3.7", (70.0, 120.0, 92.0, 132.0)),
            (1, 2, "Ordinary measurement explanation.", (220.0, 190.0, 500.0, 202.0)),
            (1, 3, "6.3.4 Next scope", (70.0, 230.0, 320.0, 242.0)),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=block_index,
                        bbox=bbox,
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (page_number, block_index, raw_text, bbox) in enumerate(
                specs
            )
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        bare = _node_for_source(tree, nodes[1].source_node_id)

        self.assertEqual(bare.kind, StructureNodeKind.CONTENT)
        self.assertIsNone(bare.explicit_label)
        self.assertFalse(any(node.explicit_label == "3.7" for node in tree.nodes))

    def test_partial_source_does_not_promote_bracketed_bare_dotted_label(
        self,
    ) -> None:
        values = (
            ("3.6 Prior definition", (70.0, 80.0, 320.0, 92.0)),
            ("3.7", (70.0, 112.0, 92.0, 124.0)),
            ("Plausible adjacent title", (92.0, 126.0, 360.0, 138.0)),
            ("3.8 Next definition", (70.0, 160.0, 320.0, 172.0)),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=bbox,
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (raw_text, bbox) in enumerate(values)
        )
        payload = _source_snapshot(nodes=nodes).model_dump(mode="python")
        payload["status"] = SourceIRStatus.PARTIAL

        tree = build_structure_tree(SourceIRSnapshot.model_validate(payload))
        bare = _node_for_source(tree, nodes[1].source_node_id)

        self.assertEqual(bare.kind, StructureNodeKind.CONTENT)
        self.assertIsNone(bare.explicit_label)
        self.assertFalse(any(node.explicit_label == "3.7" for node in tree.nodes))

    def test_bare_dotted_label_cannot_jump_across_competing_outline_branch(
        self,
    ) -> None:
        values = (
            "3.6 Prior definition",
            "4.1 Competing branch",
            "3.7",
            "Plausible nearby title",
            "3.8 Later repeated branch",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(
                            72.0 if source_order != 3 else 94.0,
                            80.0 + source_order * 24.0,
                            94.0 if source_order == 2 else 360.0,
                            94.0 + source_order * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(values)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        bare = _node_for_source(tree, nodes[2].source_node_id)

        self.assertEqual(bare.kind, StructureNodeKind.CONTENT)
        self.assertFalse(any(node.explicit_label == "3.7" for node in tree.nodes))

    def test_embedded_alphabetic_peer_run_projects_exact_clause_spans(self) -> None:
        raw_text = (
            "Test method:\n"
            "a)\nFirst operation.\n"
            "b)\nSecond operation.\n"
            "c)\nThird operation.\n"
            "d)\nFourth operation."
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=20,
                        block_index=source_order,
                        bbox=(72.0, 80.0 + source_order * 30.0, 520.0, 96.0 + source_order * 30.0),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_value,
                native_locator=locator,
            )
            for source_order, raw_value in enumerate(
                ("5.1.6.3 Test method", raw_text, "5.1.6.4 Next method")
            )
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        owner = _node_for_source(tree, nodes[0].source_node_id)
        split_nodes = tuple(
            sorted(
                (
                    node
                    for node in tree.nodes
                    if node.kind is StructureNodeKind.CLAUSE
                    and node.source_refs
                    == (
                        StructureSourceRef(
                            kind=StructureSourceRefKind.SOURCE_NODE,
                            source_id=nodes[1].source_node_id,
                        ),
                    )
                ),
                key=lambda node: node.source_order,
            )
        )
        prefix = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.semantic_source_spans
            and node.source_refs[0].source_id == nodes[1].source_node_id
        )

        self.assertEqual([node.list_ordinal for node in split_nodes], [1, 2, 3, 4])
        self.assertTrue(
            all(node.parent_structure_node_id == owner.structure_node_id for node in split_nodes)
        )
        exact_nodes = (prefix, *split_nodes)
        recovered = "".join(
            raw_text[span.start_offset : span.end_offset]
            for node in exact_nodes
            for span in node.semantic_source_spans
        )
        self.assertEqual(recovered, raw_text)

    def test_embedded_pair_can_be_corroborated_by_peer_after_table(self) -> None:
        def paragraph(
            source_order: int,
            raw_text: str,
            y0: float,
        ) -> SourceNode:
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=12,
                block_index=source_order,
                bbox=(72.0, y0, 520.0, y0 + 16.0),
            )
            return SourceNode(
                source_node_id=_source_node_id(SourceNodeKind.PARAGRAPH, locator),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )

        heading = paragraph(0, "4.8.2.1 Functional tests", 80.0)
        pair = paragraph(
            1,
            "a)\nFirst functional test.\nb)\nSecond functional test.",
            112.0,
        )
        table = _pdf_table_node(
            page_number=12,
            table_index=0,
            bbox=(72.0, 150.0, 520.0, 250.0),
            source_order=2,
            header=("Condition", "Value"),
            data=("Mode", "Nominal"),
        )
        third = paragraph(3, "c)\nThird functional test.", 270.0)
        closing = paragraph(4, "4.8.2.2 Next definition", 306.0)

        tree = build_structure_tree(
            _source_snapshot(nodes=(heading, pair, table, third, closing))
        )
        owner = _node_for_source(tree, heading.source_node_id)
        third_node = _node_for_source(tree, third.source_node_id)
        pair_nodes = tuple(
            sorted(
                (
                    node
                    for node in tree.nodes
                    if node.kind is StructureNodeKind.CLAUSE
                    and node.source_refs
                    and node.source_refs[0].source_id == pair.source_node_id
                ),
                key=lambda node: node.source_order,
            )
        )

        self.assertEqual([node.list_ordinal for node in pair_nodes], [1, 2])
        self.assertEqual(third_node.kind, StructureNodeKind.CLAUSE)
        self.assertEqual(third_node.list_ordinal, 3)
        self.assertTrue(
            all(
                node.parent_structure_node_id == owner.structure_node_id
                for node in (*pair_nodes, third_node)
            )
        )

    def test_embedded_pair_followup_can_precede_sections_in_same_source_fact(
        self,
    ) -> None:
        def paragraph(
            source_order: int,
            raw_text: str,
            y0: float,
        ) -> SourceNode:
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=12,
                block_index=source_order,
                bbox=(72.0, y0, 520.0, y0 + 16.0),
            )
            return SourceNode(
                source_node_id=_source_node_id(SourceNodeKind.PARAGRAPH, locator),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )

        heading = paragraph(0, "4.8.2.1 Functional tests", 80.0)
        pair = paragraph(
            1,
            "a)\nFirst functional test.\nb)\nSecond functional test.",
            112.0,
        )
        table = _pdf_table_node(
            page_number=12,
            table_index=0,
            bbox=(72.0, 150.0, 520.0, 250.0),
            source_order=2,
            header=("Condition", "Value"),
            data=("Mode", "Nominal"),
        )
        trailing = paragraph(
            3,
            (
                "c)\nThird functional test.\n"
                "4.8.2.2 Appearance check\nBody.\n"
                "4.8.2.3 Next definition"
            ),
            270.0,
        )

        tree = build_structure_tree(
            _source_snapshot(nodes=(heading, pair, table, trailing))
        )
        owner = _node_for_source(tree, heading.source_node_id)
        third = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.CLAUSE
            and node.list_ordinal == 3
            and node.source_refs
            and node.source_refs[0].source_id == trailing.source_node_id
        )

        self.assertEqual(third.parent_structure_node_id, owner.structure_node_id)
        self.assertEqual(len(third.semantic_source_spans), 1)
        span = third.semantic_source_spans[0]
        self.assertEqual(
            trailing.raw_text[span.start_offset : span.end_offset].rstrip(),
            "c)\nThird functional test.",
        )
        source_spans = tuple(
            sorted(
                (
                    source_span
                    for node in tree.nodes
                    for source_span in node.semantic_source_spans
                    if source_span.source_ref.source_id == trailing.source_node_id
                ),
                key=lambda source_span: source_span.start_offset,
            )
        )
        self.assertEqual(source_spans[0].start_offset, 0)
        self.assertEqual(source_spans[-1].end_offset, len(trailing.raw_text))
        self.assertTrue(
            all(
                previous.end_offset == current.start_offset
                for previous, current in zip(
                    source_spans,
                    source_spans[1:],
                    strict=False,
                )
            )
        )
        self.assertEqual(
            "".join(
                trailing.raw_text[source_span.start_offset : source_span.end_offset]
                for source_span in source_spans
            ),
            trailing.raw_text,
        )

    def test_embedded_pair_followup_and_single_trailing_section_exact_cover(
        self,
    ) -> None:
        def paragraph(source_order: int, raw_text: str, y0: float) -> SourceNode:
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=12,
                block_index=source_order,
                bbox=(72.0, y0, 520.0, y0 + 16.0),
            )
            return SourceNode(
                source_node_id=_source_node_id(SourceNodeKind.PARAGRAPH, locator),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )

        parent = paragraph(0, "4.8.2 Parent", 60.0)
        heading = paragraph(1, "4.8.2.1 Functional tests", 80.0)
        pair = paragraph(2, "a)\nA.\nb)\nB.", 112.0)
        table = _pdf_table_node(
            page_number=12,
            table_index=0,
            bbox=(72.0, 150.0, 520.0, 250.0),
            source_order=3,
            header=("Condition", "Value"),
            data=("Mode", "Nominal"),
        )
        trailing = paragraph(4, "c)\nC.\n4.8.2.2 Next\n", 270.0)
        closing = paragraph(5, "4.8.2.3 Following", 306.0)

        tree = build_structure_tree(
            _source_snapshot(
                nodes=(parent, heading, pair, table, trailing, closing)
            )
        )
        grounded = tuple(
            sorted(
                (
                    (node, source_span)
                    for node in tree.nodes
                    for source_span in node.semantic_source_spans
                    if source_span.source_ref.source_id == trailing.source_node_id
                ),
                key=lambda item: item[1].start_offset,
            )
        )

        self.assertEqual(
            [
                (node.kind, node.list_ordinal, node.explicit_label)
                for node, _ in grounded
            ],
            [
                (StructureNodeKind.CLAUSE, 3, None),
                (StructureNodeKind.SECTION, None, "4.8.2.2"),
            ],
        )
        spans = tuple(source_span for _, source_span in grounded)
        self.assertEqual(spans[0].start_offset, 0)
        self.assertEqual(spans[-1].end_offset, len(trailing.raw_text))
        self.assertTrue(
            all(
                previous.end_offset == current.start_offset
                for previous, current in zip(spans, spans[1:], strict=False)
            )
        )
        self.assertEqual(
            "".join(
                trailing.raw_text[span.start_offset : span.end_offset]
                for span in spans
            ),
            trailing.raw_text,
        )
        self.assertTrue(
            trailing.raw_text[
                spans[-1].start_offset : spans[-1].end_offset
            ].endswith("\n")
        )

    def test_unclosed_embedded_alphabetic_pair_remains_one_content_fact(self) -> None:
        raw_text = "a) First example.\nb) Second example."
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(72.0, 80.0 + source_order * 30.0, 520.0, 96.0 + source_order * 30.0),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_value,
                native_locator=locator,
            )
            for source_order, raw_value in enumerate(("5.1 Scope", raw_text, "Ordinary prose follows."))
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        pair = _node_for_source(tree, nodes[1].source_node_id)

        self.assertEqual(pair.kind, StructureNodeKind.CONTENT)
        self.assertFalse(
            any(
                node.kind is StructureNodeKind.CLAUSE
                and node.source_refs
                and node.source_refs[0].source_id == nodes[1].source_node_id
                for node in tree.nodes
            )
        )

    def test_isolated_embedded_markers_without_payload_do_not_split(self) -> None:
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(
                            72.0,
                            80.0 + source_order * 30.0,
                            520.0,
                            96.0 + source_order * 30.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(
                ("5.1 Scope", "a)\nb)\nc)\n", "5.2 Next section")
            )
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        source_id = nodes[1].source_node_id

        self.assertEqual(_node_for_source(tree, source_id).kind, StructureNodeKind.CONTENT)
        self.assertFalse(
            any(
                node.kind is StructureNodeKind.CLAUSE
                and node.source_refs
                and node.source_refs[0].source_id == source_id
                for node in tree.nodes
            )
        )

    def test_embedded_peer_run_with_empty_terminal_marker_does_not_split(
        self,
    ) -> None:
        raw_text = "a)\nFirst example.\nb)\nSecond example.\nc)\n"
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(
                            72.0,
                            80.0 + source_order * 30.0,
                            520.0,
                            96.0 + source_order * 30.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=value,
                native_locator=locator,
            )
            for source_order, value in enumerate(
                ("5.1 Scope", raw_text, "5.2 Next section")
            )
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        source_id = nodes[1].source_node_id

        self.assertEqual(_node_for_source(tree, source_id).kind, StructureNodeKind.CONTENT)
        self.assertFalse(
            any(
                node.kind is StructureNodeKind.CLAUSE
                and node.source_refs
                and node.source_refs[0].source_id == source_id
                for node in tree.nodes
            )
        )

    def test_one_native_block_with_numbered_siblings_becomes_exact_sections(
        self,
    ) -> None:
        raw_text = (
            "4.1.1 供应商应满足第一项要求。\n"
            "The supplier shall satisfy the first requirement.\n"
            "4.1.2 试验至少使用 3 个试件。\n"
            "At least three samples shall be used.\n"
            "4.1.3 如果没有其他要求，试验温度应为 TRT。\n"
            "4.1.4 试验电压应按表 1 执行。"
        )
        specs = (
            ("4 试验要求", 0),
            ("4.1 总体要求", 1),
            (raw_text, 2),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=8,
                        block_index=block_index,
                        bbox=(
                            72.0,
                            80.0 + block_index * 24.0,
                            520.0,
                            98.0 + block_index * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=block_index,
                raw_text=text,
                native_locator=locator,
            )
            for text, block_index in specs
        )

        source = _source_snapshot(nodes=nodes)
        tree = build_structure_tree(source)
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }

        self.assertTrue(
            {"4", "4.1", "4.1.1", "4.1.2", "4.1.3", "4.1.4"}.issubset(sections)
        )
        self.assertEqual(
            sections["4.1.1"].parent_structure_node_id,
            sections["4.1"].structure_node_id,
        )
        self.assertEqual(
            sections["4.1.4"].parent_structure_node_id,
            sections["4.1"].structure_node_id,
        )
        split_nodes = tuple(
            sections[label] for label in ("4.1.1", "4.1.2", "4.1.3", "4.1.4")
        )
        self.assertTrue(
            all(len(node.semantic_source_spans) == 1 for node in split_nodes)
        )
        recovered = tuple(
            raw_text[
                node.semantic_source_spans[0].start_offset : node.semantic_source_spans[
                    0
                ].end_offset
            ]
            for node in split_nodes
        )
        self.assertTrue(recovered[0].startswith("4.1.1 "))
        self.assertTrue(recovered[1].startswith("4.1.2 "))
        self.assertTrue(recovered[2].startswith("4.1.3 "))
        self.assertTrue(recovered[3].startswith("4.1.4 "))
        self.assertEqual("".join(recovered), raw_text)
        presentation = build_structure_presentation_index(source, structure=tree)
        for node, exact_text in zip(split_nodes, recovered, strict=True):
            entry = presentation["nodes"][node.structure_node_id]
            self.assertEqual(
                entry["label"],
                " ".join(exact_text.split()),
            )
            self.assertEqual(entry["owned_text"], exact_text.rstrip())

    def test_structure_presentation_persists_native_media_kind_across_pages(
        self,
    ) -> None:
        heading_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=59,
            block_index=0,
            bbox=(70.0, 540.0, 260.0, 560.0),
        )
        image_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=59,
            block_index=1,
            bbox=(70.0, 570.0, 150.0, 630.0),
        )
        figure_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=60,
            block_index=0,
            bbox=(90.0, 80.0, 180.0, 145.0),
        )
        image_asset = SourceAssetRef(
            asset_id=f"asset-{'a' * 32}",
            kind=SourceAssetKind.IMAGE,
            content_sha256="1" * 64,
            media_type="image/png",
            storage_key=f"source-assets/11/{'1' * 64}.png",
            native_locator=image_locator,
        )
        figure_asset = SourceAssetRef(
            asset_id=f"asset-{'b' * 32}",
            kind=SourceAssetKind.FIGURE,
            content_sha256="2" * 64,
            media_type="image/png",
            storage_key=f"source-assets/22/{'2' * 64}.png",
            native_locator=figure_locator,
        )
        heading = SourceNode(
            source_node_id=_source_node_id(
                SourceNodeKind.PARAGRAPH,
                heading_locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=0,
            raw_text="10.4 附录3：电能管理报价技术资料",
            native_locator=heading_locator,
        )
        image = SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.IMAGE, image_locator),
            kind=SourceNodeKind.IMAGE,
            source_order=1,
            raw_text="",
            native_locator=image_locator,
            asset_ref=image_asset.asset_id,
        )
        figure = SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.FIGURE, figure_locator),
            kind=SourceNodeKind.FIGURE,
            source_order=2,
            raw_text="",
            native_locator=figure_locator,
            asset_ref=figure_asset.asset_id,
        )
        source = _source_snapshot(
            nodes=(heading, image, figure),
            assets=(image_asset, figure_asset),
        )
        tree = build_structure_tree(source)
        heading_structure = _node_for_source(tree, heading.source_node_id)
        image_structure = _node_for_source(tree, image.source_node_id)
        figure_structure = _node_for_source(tree, figure.source_node_id)

        presentation = build_structure_presentation_index(source, structure=tree)

        self.assertEqual(
            presentation["schema_version"],
            STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
        )
        self.assertEqual(
            presentation["nodes"][heading_structure.structure_node_id][
                "source_refs"
            ][0]["source_kind"],
            "paragraph",
        )
        for structure_node, source_kind, page_number in (
            (image_structure, "image", 59),
            (figure_structure, "figure", 60),
        ):
            with self.subTest(source_kind=source_kind):
                self.assertEqual(structure_node.kind, StructureNodeKind.CONTENT)
                entry = presentation["nodes"][structure_node.structure_node_id]
                self.assertEqual(entry["label"], "图片")
                self.assertEqual(entry["owned_text"], "")
                expected_source_order = (
                    image.source_order
                    if source_kind == "image"
                    else figure.source_order
                )
                self.assertEqual(
                    (entry["physical_source_order"], entry["physical_source_offset"]),
                    (expected_source_order, 0),
                )
                self.assertEqual(entry["page_start"], page_number)
                self.assertEqual(entry["page_end"], page_number)
                self.assertEqual(
                    entry["source_refs"],
                    [
                        {
                            "kind": "source_node",
                            "source_id": next(
                                ref.source_id for ref in structure_node.source_refs
                            ),
                            "source_kind": source_kind,
                            "asset_id": (
                                image_asset.asset_id
                                if source_kind == "image"
                                else figure_asset.asset_id
                            ),
                            "asset_kind": source_kind,
                            "content_sha256": (
                                image_asset.content_sha256
                                if source_kind == "image"
                                else figure_asset.content_sha256
                            ),
                            "media_type": "image/png",
                            "storage_key": (
                                image_asset.storage_key
                                if source_kind == "image"
                                else figure_asset.storage_key
                            ),
                            "location": (
                                image_locator.model_dump(mode="json")
                                if source_kind == "image"
                                else figure_locator.model_dump(mode="json")
                            ),
                            "page_number": page_number,
                        }
                    ],
                )

        expected = {
            "expected_source_sha256": source.source_sha256,
            "expected_structure_snapshot_id": tree.structure_snapshot_id,
        }
        self.assertIsNotNone(
            validated_structure_presentation_index(presentation, **expected)
        )
        tampered = deepcopy(presentation)
        image_ref = tampered["nodes"][image_structure.structure_node_id][
            "source_refs"
        ][0]
        image_ref["storage_key"] = f"source-assets/22/{'1' * 64}.png"
        self.assertIsNone(
            validated_structure_presentation_index(tampered, **expected)
        )

        unknown_encoding = deepcopy(presentation)
        unknown_ref = unknown_encoding["nodes"][
            image_structure.structure_node_id
        ]["source_refs"][0]
        unknown_ref["media_type"] = "application/octet-stream"
        unknown_ref["storage_key"] = (
            f"source-assets/11/{image_asset.content_sha256}.bin"
        )
        self.assertIsNotNone(
            validated_structure_presentation_index(
                unknown_encoding,
                **expected,
            )
        )

    def test_structure_presentation_keeps_office_package_assets_logical(
        self,
    ) -> None:
        locator = NativeLocator(
            format=NativeDocumentFormat.DOCX,
            body_element_index=0,
            paragraph_index=0,
            object_index=0,
        )
        asset = SourceAssetRef(
            asset_id=f"asset-{'c' * 32}",
            kind=SourceAssetKind.IMAGE,
            content_sha256="4" * 64,
            media_type="image/png",
            storage_key="ooxml/word/media/image1.png",
            native_locator=locator,
        )
        image = SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.IMAGE, locator),
            kind=SourceNodeKind.IMAGE,
            source_order=0,
            raw_text="",
            native_locator=locator,
            asset_ref=asset.asset_id,
        )
        source = _source_snapshot(nodes=(image,), assets=(asset,))
        tree = build_structure_tree(source)
        presentation = build_structure_presentation_index(
            source,
            structure=tree,
        )
        image_structure = _node_for_source(tree, image.source_node_id)
        ref = presentation["nodes"][image_structure.structure_node_id][
            "source_refs"
        ][0]

        self.assertEqual(ref["source_kind"], "image")
        self.assertNotIn("asset_id", ref)
        self.assertNotIn("storage_key", ref)
        self.assertNotIn("location", ref)
        self.assertIsNotNone(
            validated_structure_presentation_index(
                presentation,
                expected_source_sha256=source.source_sha256,
                expected_structure_snapshot_id=tree.structure_snapshot_id,
            )
        )

    def test_structure_presentation_inserts_detached_pdf_image_by_geometry(
        self,
    ) -> None:
        def paragraph(
            *,
            source_order: int,
            raw_text: str,
            y0: float,
        ) -> SourceNode:
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=7,
                block_index=source_order,
                bbox=(92.0, y0, 520.0, y0 + 14.0),
            )
            return SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )

        before = paragraph(
            source_order=0,
            raw_text="a) Requirement before the figure.",
            y0=80.0,
        )
        caption = paragraph(
            source_order=1,
            raw_text="Figure 3 Generic diagram",
            y0=279.0,
        )
        after = paragraph(
            source_order=2,
            raw_text="b) Requirement after the figure.",
            y0=299.0,
        )
        image_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=7,
            block_index=3,
            bbox=(278.0, 113.0, 360.0, 274.0),
        )
        image_asset = SourceAssetRef(
            asset_id="asset-detached-reading-order",
            kind=SourceAssetKind.IMAGE,
            content_sha256="3" * 64,
            media_type="image/png",
            storage_key="pdf/images/detached-reading-order.png",
            native_locator=image_locator,
        )
        image = SourceNode(
            source_node_id=_source_node_id(
                SourceNodeKind.IMAGE,
                image_locator,
            ),
            kind=SourceNodeKind.IMAGE,
            source_order=3,
            raw_text="",
            native_locator=image_locator,
            asset_ref=image_asset.asset_id,
        )
        source = _source_snapshot(
            nodes=(before, caption, after, image),
            assets=(image_asset,),
        )
        tree = build_structure_tree(source)
        presentation = build_structure_presentation_index(source, structure=tree)

        physical_orders = {
            source_node.source_node_id: presentation["nodes"][
                _node_for_source(tree, source_node.source_node_id).structure_node_id
            ]["physical_source_order"]
            for source_node in (before, image, caption, after)
        }

        self.assertEqual(
            [
                source_id
                for source_id, _ in sorted(
                    physical_orders.items(),
                    key=lambda item: item[1],
                )
            ],
            [
                before.source_node_id,
                image.source_node_id,
                caption.source_node_id,
                after.source_node_id,
            ],
        )

    def test_structure_presentation_inserts_detached_pdf_table_after_anchor(
        self,
    ) -> None:
        paragraphs = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=8,
                        block_index=source_order,
                        bbox=(92.0, y0, 520.0, y0 + 14.0),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text, y0 in (
                (0, "Requirement before the table.", 80.0),
                (1, "Generic table caption", 100.0),
                (2, "Requirement after the table.", 300.0),
            )
        )
        table = _pdf_table_node(
            page_number=8,
            table_index=0,
            bbox=(72.0, 120.0, 530.0, 280.0),
            source_order=3,
            header=("Field", "Value"),
            data=("Generic", "Content"),
        )
        source = _source_snapshot(nodes=(*paragraphs, table))
        tree = build_structure_tree(source)
        presentation = build_structure_presentation_index(source, structure=tree)

        physical_orders = {
            source_node.source_node_id: presentation["nodes"][
                _node_for_source(tree, source_node.source_node_id).structure_node_id
            ]["physical_source_order"]
            for source_node in (*paragraphs, table)
        }

        self.assertLess(
            physical_orders[paragraphs[0].source_node_id],
            physical_orders[paragraphs[1].source_node_id],
        )
        self.assertLess(
            physical_orders[paragraphs[1].source_node_id],
            physical_orders[table.source_node_id],
        )
        self.assertLess(
            physical_orders[table.source_node_id],
            physical_orders[paragraphs[2].source_node_id],
        )

    def test_structure_presentation_distinguishes_table_and_cell_source_kinds(
        self,
    ) -> None:
        source, table_node, image_node, _ = _pdf_table_image_snapshot(
            image_bbox=(185.0, 50.0, 235.0, 80.0)
        )
        tree = build_structure_tree(source)
        presentation = build_structure_presentation_index(source, structure=tree)
        table_structure = _node_for_source(tree, table_node.source_node_id)
        image_structure = _node_for_source(tree, image_node.source_node_id)
        table_entry = presentation["nodes"][table_structure.structure_node_id]
        row_entry = next(
            presentation["nodes"][node.structure_node_id]
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
        )

        self.assertEqual(table_entry["source_refs"][0]["source_kind"], "table")
        self.assertEqual(
            {ref["source_kind"] for ref in row_entry["source_refs"]},
            {"cell"},
        )
        self.assertEqual(
            presentation["nodes"][image_structure.structure_node_id]["source_refs"][
                0
            ]["source_kind"],
            "image",
        )

    def test_structure_presentation_rejects_legacy_or_untyped_refs(self) -> None:
        source = _pdf_source_snapshot()
        tree = build_structure_tree(source)
        presentation = build_structure_presentation_index(source, structure=tree)
        expected = {
            "expected_source_sha256": source.source_sha256,
            "expected_structure_snapshot_id": tree.structure_snapshot_id,
        }

        self.assertIsNotNone(
            validated_structure_presentation_index(presentation, **expected)
        )
        legacy = deepcopy(presentation)
        legacy["schema_version"] = "structure-presentation-index/v2"
        self.assertIsNone(
            validated_structure_presentation_index(legacy, **expected)
        )
        untyped = deepcopy(presentation)
        first_entry = next(
            entry for entry in untyped["nodes"].values() if entry["source_refs"]
        )
        first_entry["source_refs"][0].pop("source_kind")
        self.assertIsNone(
            validated_structure_presentation_index(untyped, **expected)
        )
        incomplete_reading_key = deepcopy(presentation)
        ordered_entry = next(
            entry
            for entry in incomplete_reading_key["nodes"].values()
            if "physical_source_order" in entry
        )
        ordered_entry.pop("physical_source_offset")
        self.assertIsNone(
            validated_structure_presentation_index(
                incomplete_reading_key,
                **expected,
            )
        )

    def test_text_before_embedded_numbered_siblings_stays_with_prior_section(
        self,
    ) -> None:
        raw_text = (
            "前一条款的物理续行仍属于 4.1.1。\n"
            "4.1.2 试验至少使用 3 个试件。\n"
            "4.1.3 试验温度为 TRT。\n"
            "4.1.4 试验电压按表 1 执行。"
        )
        specs = (
            "4 试验要求",
            "4.1 总体要求",
            "4.1.1 供应商应完成准备工作。",
            raw_text,
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=9,
                        block_index=block_index,
                        bbox=(
                            72.0,
                            80.0 + block_index * 24.0,
                            520.0,
                            98.0 + block_index * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=block_index,
                raw_text=text,
                native_locator=locator,
            )
            for block_index, text in enumerate(specs)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }
        split_source_id = nodes[-1].source_node_id
        prefix = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and any(ref.source_id == split_source_id for ref in node.source_refs)
            and node.semantic_source_spans
        )

        self.assertEqual(
            prefix.parent_structure_node_id,
            sections["4.1.1"].structure_node_id,
        )
        prefix_span = prefix.semantic_source_spans[0]
        self.assertEqual(
            raw_text[prefix_span.start_offset : prefix_span.end_offset].rstrip(),
            "前一条款的物理续行仍属于 4.1.1。",
        )
        for label in ("4.1.2", "4.1.3", "4.1.4"):
            self.assertEqual(
                sections[label].parent_structure_node_id,
                sections["4.1"].structure_node_id,
            )

    def test_isolated_numeric_value_does_not_veto_coherent_embedded_outline_runs(
        self,
    ) -> None:
        raw_text = (
            "1.1 环境要求\n完整正文。\n"
            "1.2 机械要求\n"
            "1.2.1 冲击要求\n试验按既定工况执行，\n"
            "3.2模式）。\n"
            "1.2.2 耐久要求\n完整正文。\n"
            "1.2.3 强度要求\n完整正文。\n"
            "1.3 化学要求\n"
            "1.3.1 盐雾要求\n完整正文。\n"
            "1.3.2 材料要求（说明）\n"
            "1.4 其他要求\n完整正文。"
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=13,
                        block_index=source_order,
                        bbox=(55.0, 72.0, 530.0, 96.0 + source_order * 420.0),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=text,
                native_locator=locator,
            )
            for source_order, text in enumerate(("1 可靠性要求", raw_text))
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }

        expected = {
            "1.1",
            "1.2",
            "1.2.1",
            "1.2.2",
            "1.2.3",
            "1.3",
            "1.3.1",
            "1.3.2",
            "1.4",
        }
        self.assertTrue(expected.issubset(sections))
        self.assertNotIn("3.2", sections)
        first_mechanical_child = sections["1.2.1"]
        span = first_mechanical_child.semantic_source_spans[0]
        self.assertIn(
            "3.2模式",
            raw_text[span.start_offset : span.end_offset],
        )

    def test_short_embedded_outline_runs_require_bounding_outline_anchors(
        self,
    ) -> None:
        texts = (
            "4 试验要求",
            "4.6 环境试验",
            "\n".join(f"4.6.{ordinal} 已有要求" for ordinal in range(1, 8)),
            *(f"仍属于 4.6.7 的连续正文 {index}。" for index in range(40)),
            "前一条的物理续行。\n4.6.8 新的完整要求",
            "上一条的补充正文。\n4.6.9 另一项完整要求",
            "4.7 耐久试验",
            "5 设计要求",
            "5.1 总体要求",
            "5.1.1 已有要求",
            "前一条的物理续行。\n5.1.2 中间要求",
            "5.1.3 后续要求",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=20,
                        block_index=source_order,
                        bbox=(
                            72.0,
                            80.0 + source_order * 12.0,
                            520.0,
                            90.0 + source_order * 12.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=text,
                native_locator=locator,
            )
            for source_order, text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }

        self.assertTrue({"4.6.8", "4.6.9", "5.1.2"}.issubset(sections))
        self.assertEqual(
            sections["4.6.8"].parent_structure_node_id,
            sections["4.6"].structure_node_id,
        )
        self.assertEqual(
            sections["5.1.2"].parent_structure_node_id,
            sections["5.1"].structure_node_id,
        )

    def test_bounded_measurements_and_wrapped_references_are_not_sections(
        self,
    ) -> None:
        for case, parent, previous, candidate, following, rejected_label in (
            (
                "wrapped-mode",
                "2 模式要求",
                "2.1 已有要求",
                "设备应使用 operating mode \n2.2 to determine stability.",
                "2.3 后续要求",
                "2.2",
            ),
            (
                "measurement",
                "5.1 总体要求",
                "5.1.1 已有要求",
                "公差取值如下：\n5.1.2 mm tolerance",
                "5.1.3 后续要求",
                "5.1.2",
            ),
            (
                "cross-reference",
                "6.1 总体要求",
                "6.1.1 已有要求",
                "See section\n6.1.2 for details.",
                "6.1.3 后续要求",
                "6.1.2",
            ),
        ):
            with self.subTest(case=case):
                texts = (parent, previous, candidate, following)
                nodes = tuple(
                    SourceNode(
                        source_node_id=_source_node_id(
                            SourceNodeKind.PARAGRAPH,
                            locator := NativeLocator(
                                format=NativeDocumentFormat.PDF,
                                page_number=30,
                                block_index=source_order,
                                bbox=(
                                    72.0,
                                    80.0 + source_order * 28.0,
                                    520.0,
                                    100.0 + source_order * 28.0,
                                ),
                            ),
                        ),
                        kind=SourceNodeKind.PARAGRAPH,
                        source_order=source_order,
                        raw_text=text,
                        native_locator=locator,
                    )
                    for source_order, text in enumerate(texts)
                )

                tree = build_structure_tree(_source_snapshot(nodes=nodes))
                projected = _node_for_source(tree, nodes[2].source_node_id)

                self.assertIsNone(projected.explicit_label)
                self.assertIsNot(projected.kind, StructureNodeKind.SECTION)
                self.assertFalse(
                    any(node.explicit_label == rejected_label for node in tree.nodes)
                )

    def test_trailing_dot_multilevel_headings_preserve_numeric_hierarchy(
        self,
    ) -> None:
        specs = (
            ("6.4 测试要求", 80.0),
            ("6.4.1. 电气性能测试要求", 110.0),
            ("6.4.1.1． 测试要求", 140.0),
            ("6.4.1.2. 测试报告要求", 170.0),
            ("6.4.2． CAN/CANFD 通信测试要求", 200.0),
        )
        nodes: list[SourceNode] = []
        for source_order, (raw_text, y0) in enumerate(specs):
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=41,
                block_index=source_order,
                bbox=(72.0, y0, 500.0, y0 + 14.0),
            )
            nodes.append(
                SourceNode(
                    source_node_id=_source_node_id(
                        SourceNodeKind.PARAGRAPH,
                        locator,
                    ),
                    kind=SourceNodeKind.PARAGRAPH,
                    source_order=source_order,
                    raw_text=raw_text,
                    native_locator=locator,
                )
            )

        tree = build_structure_tree(_source_snapshot(nodes=tuple(nodes)))
        sections = [_node_for_source(tree, node.source_node_id) for node in nodes]

        self.assertEqual(
            [section.explicit_label for section in sections],
            ["6.4", "6.4.1", "6.4.1.1", "6.4.1.2", "6.4.2"],
        )
        self.assertTrue(
            all(section.kind is StructureNodeKind.SECTION for section in sections)
        )
        self.assertEqual(
            sections[1].parent_structure_node_id,
            sections[0].structure_node_id,
        )
        self.assertEqual(
            sections[2].parent_structure_node_id,
            sections[1].structure_node_id,
        )
        self.assertEqual(
            sections[3].parent_structure_node_id,
            sections[1].structure_node_id,
        )
        self.assertEqual(
            sections[4].parent_structure_node_id,
            sections[0].structure_node_id,
        )

    def test_root_number_plausibility_rejects_units_axes_and_long_prose(
        self,
    ) -> None:
        texts = (
            "1.0 工程概要",
            "20 周 交期说明的物理续行",
            "6 6.5 7 8 9 10 12 14 16 18 20 22 24",
            "2.0 " + "这是一段连续正文而不是标题" * 20,
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(
                            72.0,
                            80.0 + source_order * 24.0,
                            520.0,
                            96.0 + source_order * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        projected = tuple(_node_for_source(tree, node.source_node_id) for node in nodes)

        self.assertEqual(projected[0].kind, StructureNodeKind.SECTION)
        self.assertEqual(projected[0].explicit_label, "1.0")
        self.assertTrue(
            all(node.kind is StructureNodeKind.CONTENT for node in projected[1:])
        )
        self.assertTrue(all(node.explicit_label is None for node in projected[1:]))

    def test_coordinate_vectors_are_not_dotted_outline_boundaries(self) -> None:
        texts = (
            "51.24 614.879 147.852 623.879",
            "12.34 56.78",
            "1.0 V",
            "4.1 Credible section title",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        projected = tuple(_node_for_source(tree, node.source_node_id) for node in nodes)

        self.assertTrue(
            all(node.kind is StructureNodeKind.CONTENT for node in projected[:3])
        )
        self.assertTrue(all(node.explicit_label is None for node in projected[:3]))
        self.assertEqual(projected[3].kind, StructureNodeKind.SECTION)
        self.assertEqual(projected[3].explicit_label, "4.1")

    def test_high_ordinal_root_heading_is_not_rejected_by_a_global_cap(self) -> None:
        texts = (
            "21 General verification requirements",
            "22通用验证要求",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        projected = tuple(_node_for_source(tree, node.source_node_id) for node in nodes)

        self.assertEqual(
            tuple(node.kind for node in projected),
            (StructureNodeKind.SECTION, StructureNodeKind.SECTION),
        )
        self.assertEqual(
            tuple(node.explicit_label for node in projected),
            ("21", "22"),
        )

    def test_undelimited_numeric_identifier_sentence_is_not_a_root_heading(
        self,
    ) -> None:
        node = SourceNode(
            source_node_id=_source_node_id(
                SourceNodeKind.PARAGRAPH,
                locator := NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    block_index=0,
                ),
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=0,
            raw_text="30节点按照以下逻辑进行模式转换。",
            native_locator=locator,
        )

        projected = _node_for_source(
            build_structure_tree(_source_snapshot(nodes=(node,))),
            node.source_node_id,
        )

        self.assertEqual(projected.kind, StructureNodeKind.CONTENT)
        self.assertIsNone(projected.explicit_label)

    def test_undelimited_root_requires_independent_structural_corroboration(
        self,
    ) -> None:
        texts = (
            "30节点按照以下逻辑进行模式转换",
            "31次试验",
            "40网络管理",
            "40.1 唤醒要求",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        projected = tuple(_node_for_source(tree, node.source_node_id) for node in nodes)

        self.assertEqual(projected[0].kind, StructureNodeKind.CONTENT)
        self.assertIsNone(projected[0].explicit_label)
        self.assertEqual(projected[1].kind, StructureNodeKind.CONTENT)
        self.assertIsNone(projected[1].explicit_label)
        self.assertEqual(projected[2].kind, StructureNodeKind.SECTION)
        self.assertEqual(projected[2].explicit_label, "40")
        self.assertEqual(projected[3].kind, StructureNodeKind.SECTION)
        self.assertEqual(
            projected[3].parent_structure_node_id,
            projected[2].structure_node_id,
        )

    def test_three_coherent_undelimited_roots_are_accepted_without_ordinal_cap(
        self,
    ) -> None:
        texts = ("31网络管理", "32诊断管理", "33供电管理")
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        projected = tuple(_node_for_source(tree, node.source_node_id) for node in nodes)

        self.assertEqual(
            tuple(node.explicit_label for node in projected),
            ("31", "32", "33"),
        )
        self.assertTrue(
            all(node.kind is StructureNodeKind.SECTION for node in projected)
        )

    def test_ideographic_space_is_an_explicit_root_separator(self) -> None:
        node = SourceNode(
            source_node_id=_source_node_id(
                SourceNodeKind.PARAGRAPH,
                locator := NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    block_index=0,
                ),
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=0,
            raw_text="21\u3000通用验证要求",
            native_locator=locator,
        )

        projected = _node_for_source(
            build_structure_tree(_source_snapshot(nodes=(node,))),
            node.source_node_id,
        )

        self.assertEqual(projected.kind, StructureNodeKind.SECTION)
        self.assertEqual(projected.explicit_label, "21")

    def test_repeated_ideographic_ordinals_form_a_nested_list_not_chapters(
        self,
    ) -> None:
        texts = (
            "10.2.1 设计验证",
            "1、提交验证计划",
            "2、完成样件试验",
            "3、提交完整报告",
            "1）报告应包含原始数据",
            "2）报告应包含结论",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=47,
                        block_index=source_order,
                        bbox=(
                            72.0 if source_order < 4 else 92.0,
                            80.0 + source_order * 24.0,
                            520.0,
                            96.0 + source_order * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        projected = tuple(_node_for_source(tree, node.source_node_id) for node in nodes)

        section = projected[0]
        top_level_items = projected[1:4]
        nested_items = projected[4:]
        self.assertEqual(section.kind, StructureNodeKind.SECTION)
        self.assertTrue(
            all(item.kind is StructureNodeKind.CLAUSE for item in top_level_items)
        )
        self.assertEqual(
            [item.list_ordinal for item in top_level_items],
            [1, 2, 3],
        )
        self.assertTrue(
            all(
                item.parent_structure_node_id == section.structure_node_id
                for item in top_level_items
            )
        )
        self.assertTrue(
            all(item.kind is StructureNodeKind.CLAUSE for item in nested_items)
        )
        self.assertTrue(
            all(
                item.parent_structure_node_id == top_level_items[-1].structure_node_id
                for item in nested_items
            )
        )

    def test_common_cross_family_lists_nest_with_one_glyph_indent_jitter(
        self,
    ) -> None:
        cases = (
            (
                "ideographic-to-numeric",
                (
                    "4.1 Verification scope",
                    "1、Primary verification item",
                    "1）Prepare the plan",
                    "2）Record the result",
                    "2、Next verification item",
                    "4.2 Next scope",
                ),
                1,
                (2, 3),
            ),
            (
                "ideographic-to-alphabetic",
                (
                    "4.1 Verification scope",
                    "1、Primary verification item",
                    "a) Prepare the plan",
                    "b) Record the result",
                    "2、Next verification item",
                    "4.2 Next scope",
                ),
                1,
                (2, 3),
            ),
            (
                "numeric-to-alphabetic",
                (
                    "4.1 Verification scope",
                    "1）Primary verification item",
                    "A) Prepare the plan",
                    "B) Record the result",
                    "4.2 Next scope",
                ),
                1,
                (2, 3),
            ),
            (
                "alphabetic-to-numeric",
                (
                    "4.1 Verification scope",
                    "a) First verification item",
                    "b) Primary verification item",
                    "1）Prepare the plan",
                    "2）Record the result",
                    "4.2 Next scope",
                ),
                2,
                (3, 4),
            ),
        )
        for case, texts, parent_index, child_indices in cases:
            with self.subTest(case=case):
                nodes = tuple(
                    SourceNode(
                        source_node_id=_source_node_id(
                            SourceNodeKind.PARAGRAPH,
                            locator := NativeLocator(
                                format=NativeDocumentFormat.PDF,
                                page_number=1,
                                block_index=source_order,
                                bbox=(
                                    (
                                        72.0
                                        if source_order in {0, len(texts) - 1}
                                        else 98.0
                                        if source_order == parent_index
                                        else 92.0
                                    ),
                                    80.0 + source_order * 24.0,
                                    520.0,
                                    96.0 + source_order * 24.0,
                                ),
                            ),
                        ),
                        kind=SourceNodeKind.PARAGRAPH,
                        source_order=source_order,
                        raw_text=raw_text,
                        native_locator=locator,
                    )
                    for source_order, raw_text in enumerate(texts)
                )

                tree = build_structure_tree(_source_snapshot(nodes=nodes))
                parent = _node_for_source(tree, nodes[parent_index].source_node_id)
                children = tuple(
                    _node_for_source(tree, nodes[index].source_node_id)
                    for index in child_indices
                )

                self.assertEqual(parent.kind, StructureNodeKind.CLAUSE)
                self.assertTrue(
                    all(child.kind is StructureNodeKind.CLAUSE for child in children)
                )
                self.assertTrue(
                    all(
                        child.parent_structure_node_id == parent.structure_node_id
                        for child in children
                    )
                )

    def test_competing_peers_close_stale_nested_scope_before_later_subitem(
        self,
    ) -> None:
        texts = (
            "8.4 Verification scope",
            "5、Fifth peer item",
            "1）Subitem of the fifth peer",
            "6、Sixth peer item",
            "7、Seventh peer item",
            "2）Subitem of the seventh peer",
            "8、Eighth peer item",
            "8.5 Next scope",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(
                            92.0 if source_order in {2, 5} else 98.0,
                            80.0 + source_order * 24.0,
                            520.0,
                            96.0 + source_order * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        section = _node_for_source(tree, nodes[0].source_node_id)
        peers = tuple(
            _node_for_source(tree, nodes[index].source_node_id)
            for index in (1, 3, 4, 6)
        )
        first_subitem = _node_for_source(tree, nodes[2].source_node_id)
        later_subitem = _node_for_source(tree, nodes[5].source_node_id)

        self.assertEqual(
            [peer.list_ordinal for peer in peers],
            [5, 6, 7, 8],
        )
        self.assertTrue(
            all(
                peer.parent_structure_node_id == section.structure_node_id
                for peer in peers
            )
        )
        self.assertEqual(
            first_subitem.parent_structure_node_id,
            peers[0].structure_node_id,
        )
        self.assertEqual(
            later_subitem.parent_structure_node_id,
            peers[2].structure_node_id,
        )

    def test_fully_wrapped_markers_remain_unmaterialized_without_shared_grammar(
        self,
    ) -> None:
        texts = (
            "4.1 Verification scope",
            "(1) Numeric example",
            "（2） Full-width numeric example",
            "(a) Alphabetic example",
            "（b） Full-width alphabetic example",
            "4.2 Next scope",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(
                            92.0,
                            80.0 + source_order * 24.0,
                            520.0,
                            96.0 + source_order * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        wrapped = tuple(
            _node_for_source(tree, node.source_node_id) for node in nodes[1:5]
        )

        self.assertTrue(all(item.kind is StructureNodeKind.CONTENT for item in wrapped))
        self.assertTrue(all(item.list_ordinal is None for item in wrapped))

    def test_ten_item_ideographic_run_keeps_one_parent_across_pages(
        self,
    ) -> None:
        texts = (
            "10.2.1 总线要求",
            *(f"{ordinal}、第 {ordinal} 项要求" for ordinal in range(1, 11)),
            "10.2.2 后续要求",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=47 if source_order <= 5 else 48,
                        block_index=source_order,
                        bbox=(
                            72.0 if source_order <= 5 else 78.0,
                            80.0 + (source_order % 6) * 24.0,
                            520.0,
                            96.0 + (source_order % 6) * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        section = _node_for_source(tree, nodes[0].source_node_id)
        items = tuple(
            _node_for_source(tree, node.source_node_id) for node in nodes[1:11]
        )

        self.assertTrue(all(item.kind is StructureNodeKind.CLAUSE for item in items))
        self.assertEqual([item.list_ordinal for item in items], list(range(1, 11)))
        self.assertTrue(
            all(
                item.parent_structure_node_id == section.structure_node_id
                for item in items
            )
        )
        self.assertFalse(
            any(
                node.kind is StructureNodeKind.SECTION
                and node.explicit_label in {str(value) for value in range(1, 11)}
                for node in tree.nodes
            )
        )

    def test_isolated_ideographic_ordinal_is_not_promoted_without_peer_evidence(
        self,
    ) -> None:
        texts = (
            "4.2 范围",
            "1、正文中孤立的说明",
            "后续正文没有同族编号。",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=2,
                        block_index=source_order,
                        bbox=(
                            72.0,
                            80.0 + source_order * 24.0,
                            520.0,
                            96.0 + source_order * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        isolated = _node_for_source(tree, nodes[1].source_node_id)

        self.assertEqual(isolated.kind, StructureNodeKind.CONTENT)
        self.assertIsNone(isolated.list_ordinal)

    def test_coherent_alphabetic_run_forms_peer_clauses_across_wrapped_content(
        self,
    ) -> None:
        texts = (
            "4.2 Verification scope",
            "a) The supplier shall submit a verification plan.",
            "The plan shall retain the source evidence.",
            "b）The supplier shall execute the approved tests.",
            "The report shall include the measured results.",
            "c) The supplier shall close every failed result.",
            "4.3 Delivery scope",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1 if source_order < 5 else 2,
                        block_index=source_order,
                        bbox=(
                            92.0 if source_order in {1, 3, 5} else 112.0,
                            80.0 + (source_order % 5) * 24.0,
                            520.0,
                            96.0 + (source_order % 5) * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        section = _node_for_source(tree, nodes[0].source_node_id)
        items = tuple(
            _node_for_source(tree, nodes[index].source_node_id) for index in (1, 3, 5)
        )
        wrapped = tuple(
            _node_for_source(tree, nodes[index].source_node_id) for index in (2, 4)
        )

        self.assertTrue(all(item.kind is StructureNodeKind.CLAUSE for item in items))
        self.assertEqual([item.list_ordinal for item in items], [1, 2, 3])
        self.assertTrue(
            all(
                item.parent_structure_node_id == section.structure_node_id
                for item in items
            )
        )
        self.assertEqual(
            wrapped[0].parent_structure_node_id,
            items[0].structure_node_id,
        )
        self.assertEqual(
            wrapped[1].parent_structure_node_id,
            items[1].structure_node_id,
        )

    def test_closed_peer_list_releases_adjacent_table_introduction_to_parent(
        self,
    ) -> None:
        def paragraph(source_order: int, raw_text: str, y0: float) -> SourceNode:
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                block_index=source_order,
                bbox=(92.0, y0, 520.0, y0 + 12.0),
            )
            return SourceNode(
                source_node_id=_source_node_id(SourceNodeKind.PARAGRAPH, locator),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )

        section = paragraph(0, "1.3 Responsibility scope", 80.0)
        peers = (
            paragraph(1, "a) First responsibility.", 110.0),
            paragraph(2, "b) Second responsibility.", 134.0),
            paragraph(3, "c) Final responsibility.", 158.0),
        )
        introduction = paragraph(4, "Responsibility matrix:", 182.0)
        table = _pdf_table_node(
            page_number=1,
            table_index=0,
            bbox=(82.0, 198.0, 526.0, 258.0),
            source_order=5,
            header=("Activity", "Owner"),
            data=("Verification", "Supplier"),
        )
        next_section = paragraph(6, "1.4 Next scope", 282.0)

        tree = build_structure_tree(
            _source_snapshot(
                nodes=(section, *peers, introduction, table, next_section),
            )
        )
        section_node = _node_for_source(tree, section.source_node_id)
        peer_nodes = tuple(
            _node_for_source(tree, peer.source_node_id) for peer in peers
        )
        introduction_node = _node_for_source(tree, introduction.source_node_id)
        table_node = _node_for_source(tree, table.source_node_id)

        self.assertTrue(
            all(
                peer.parent_structure_node_id == section_node.structure_node_id
                for peer in peer_nodes
            )
        )
        self.assertEqual(
            introduction_node.parent_structure_node_id,
            section_node.structure_node_id,
        )
        self.assertEqual(
            table_node.parent_structure_node_id,
            section_node.structure_node_id,
        )

    def test_list_scope_is_not_released_without_every_table_boundary_signal(
        self,
    ) -> None:
        cases = (
            (
                "open-last-item",
                "c) Final responsibility introduces its evidence",
                "Responsibility matrix:",
                198.0,
            ),
            (
                "ordinary-following-prose",
                "c) Final responsibility.",
                "Additional responsibility evidence follows.",
                198.0,
            ),
            (
                "distant-table",
                "c) Final responsibility.",
                "Responsibility matrix:",
                248.0,
            ),
        )

        for case, final_item_text, introduction_text, table_y0 in cases:
            with self.subTest(case=case):

                def paragraph(
                    source_order: int,
                    raw_text: str,
                    y0: float,
                ) -> SourceNode:
                    locator = NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(92.0, y0, 520.0, y0 + 12.0),
                    )
                    return SourceNode(
                        source_node_id=_source_node_id(
                            SourceNodeKind.PARAGRAPH,
                            locator,
                        ),
                        kind=SourceNodeKind.PARAGRAPH,
                        source_order=source_order,
                        raw_text=raw_text,
                        native_locator=locator,
                    )

                section = paragraph(0, "1.3 Responsibility scope", 80.0)
                peers = (
                    paragraph(1, "a) First responsibility.", 110.0),
                    paragraph(2, "b) Second responsibility.", 134.0),
                    paragraph(3, final_item_text, 158.0),
                )
                introduction = paragraph(4, introduction_text, 182.0)
                table = _pdf_table_node(
                    page_number=1,
                    table_index=0,
                    bbox=(82.0, table_y0, 526.0, table_y0 + 60.0),
                    source_order=5,
                    header=("Activity", "Owner"),
                    data=("Verification", "Supplier"),
                )
                next_section = paragraph(
                    6,
                    "1.4 Next scope",
                    table_y0 + 84.0,
                )

                tree = build_structure_tree(
                    _source_snapshot(
                        nodes=(
                            section,
                            *peers,
                            introduction,
                            table,
                            next_section,
                        ),
                    )
                )
                final_peer = _node_for_source(tree, peers[-1].source_node_id)
                introduction_node = _node_for_source(
                    tree,
                    introduction.source_node_id,
                )
                table_node = _node_for_source(tree, table.source_node_id)

                self.assertEqual(
                    introduction_node.parent_structure_node_id,
                    final_peer.structure_node_id,
                )
                self.assertEqual(
                    table_node.parent_structure_node_id,
                    final_peer.structure_node_id,
                )

    def test_short_alphabetic_pair_is_not_promoted_without_strong_run_evidence(
        self,
    ) -> None:
        texts = (
            "5.1 Scope",
            "a) First example.",
            "b) Second example.",
            "Ordinary prose follows.",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(
                            92.0,
                            80.0 + source_order * 24.0,
                            520.0,
                            96.0 + source_order * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        pair = tuple(_node_for_source(tree, node.source_node_id) for node in nodes[1:3])

        self.assertTrue(all(item.kind is StructureNodeKind.CONTENT for item in pair))
        self.assertTrue(all(item.list_ordinal is None for item in pair))

    def test_closed_alphabetic_pair_forms_peer_clauses(self) -> None:
        texts = (
            "5.1 Scope",
            "a) First bounded requirement.",
            "b) Second bounded requirement.",
            "5.2 Next section",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(
                            92.0,
                            80.0 + source_order * 24.0,
                            520.0,
                            96.0 + source_order * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        section = _node_for_source(tree, nodes[0].source_node_id)
        pair = tuple(_node_for_source(tree, node.source_node_id) for node in nodes[1:3])

        self.assertTrue(all(item.kind is StructureNodeKind.CLAUSE for item in pair))
        self.assertEqual([item.list_ordinal for item in pair], [1, 2])
        self.assertTrue(
            all(
                item.parent_structure_node_id == section.structure_node_id
                for item in pair
            )
        )

    def test_closed_alphabetic_pair_allows_only_transparent_caption_gap(
        self,
    ) -> None:
        for interstitial, promoted in (
            ("图3 Synthetic verification figure", True),
            ("Ordinary prose between independent markers.", False),
        ):
            with self.subTest(interstitial=interstitial):
                texts = (
                    "6.1 Scope",
                    "a) First bounded requirement.",
                    interstitial,
                    "b) Second bounded requirement.",
                    "6.2 Next section",
                )
                nodes = tuple(
                    SourceNode(
                        source_node_id=_source_node_id(
                            SourceNodeKind.PARAGRAPH,
                            locator := NativeLocator(
                                format=NativeDocumentFormat.PDF,
                                page_number=1,
                                block_index=source_order,
                                bbox=(
                                    (240.0 if source_order == 2 else 92.0),
                                    80.0 + source_order * 24.0,
                                    520.0,
                                    96.0 + source_order * 24.0,
                                ),
                            ),
                        ),
                        kind=SourceNodeKind.PARAGRAPH,
                        source_order=source_order,
                        raw_text=raw_text,
                        native_locator=locator,
                    )
                    for source_order, raw_text in enumerate(texts)
                )

                tree = build_structure_tree(_source_snapshot(nodes=nodes))
                pair = tuple(
                    _node_for_source(tree, nodes[index].source_node_id)
                    for index in (1, 3)
                )

                self.assertEqual(
                    all(item.kind is StructureNodeKind.CLAUSE for item in pair),
                    promoted,
                )

    def test_closed_alphabetic_pair_allows_bounded_figure_evidence_cluster(
        self,
    ) -> None:
        def source_node(
            *,
            source_order: int,
            kind: SourceNodeKind,
            raw_text: str,
            bbox: tuple[float, float, float, float],
        ) -> SourceNode:
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=2,
                block_index=source_order,
                bbox=bbox,
            )
            return SourceNode(
                source_node_id=_source_node_id(kind, locator),
                kind=kind,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
                asset_ref=(
                    f"asset-{source_order}"
                    if kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
                    else None
                ),
            )

        before = source_node(
            source_order=1,
            kind=SourceNodeKind.PARAGRAPH,
            raw_text="a) First bounded requirement.",
            bbox=(92.0, 80.0, 520.0, 96.0),
        )
        image = source_node(
            source_order=2,
            kind=SourceNodeKind.IMAGE,
            raw_text="",
            bbox=(278.0, 113.0, 360.0, 274.0),
        )
        labels = (
            source_node(
                source_order=3,
                kind=SourceNodeKind.PARAGRAPH,
                raw_text="Piston rod",
                bbox=(218.0, 132.0, 279.0, 143.0),
            ),
            source_node(
                source_order=4,
                kind=SourceNodeKind.PARAGRAPH,
                raw_text="Oil film width",
                bbox=(362.0, 174.0, 433.0, 185.0),
            ),
            source_node(
                source_order=5,
                kind=SourceNodeKind.PARAGRAPH,
                raw_text="Reservoir",
                bbox=(365.0, 233.0, 421.0, 244.0),
            ),
        )
        caption = source_node(
            source_order=6,
            kind=SourceNodeKind.PARAGRAPH,
            raw_text="Figure 3 Synthetic leakage diagram",
            bbox=(254.0, 279.0, 414.0, 291.0),
        )
        after = source_node(
            source_order=7,
            kind=SourceNodeKind.PARAGRAPH,
            raw_text="b) Second bounded requirement.",
            bbox=(92.0, 299.0, 520.0, 315.0),
        )

        self.assertTrue(
            alphabetic_pair_interstitial_is_transparent(
                (image, *labels, caption),
                before=before,
                after=after,
            )
        )
        self.assertFalse(
            alphabetic_pair_interstitial_is_transparent(
                (image, *labels),
                before=before,
                after=after,
            )
        )
        image_only = alphabetic_pair_interstitial_evidence(
            (image,),
            before=before,
            after=after,
        )
        self.assertFalse(image_only.accepted)
        self.assertIn("numbered_caption_missing", image_only.reason_codes)

    def test_incomplete_source_does_not_promote_closed_alphabetic_pair(
        self,
    ) -> None:
        texts = (
            "7.1 Scope",
            "a) First bounded requirement.",
            "b) Second bounded requirement.",
            "7.2 Next section",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )
        source = _source_snapshot(nodes=nodes).model_copy(
            update={"status": SourceIRStatus.PARTIAL}
        )

        tree = build_structure_tree(source)
        pair = tuple(_node_for_source(tree, node.source_node_id) for node in nodes[1:3])

        self.assertTrue(all(item.kind is StructureNodeKind.CONTENT for item in pair))

    def test_distant_alphabetic_markers_do_not_form_a_synthetic_list(self) -> None:
        texts = (
            "6.1 Scope",
            "a) First example.",
            *(f"Unrelated context {index}." for index in range(9)),
            "b) Distant example.",
            "c) Another distant example.",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        block_index=source_order,
                        bbox=(
                            92.0,
                            80.0 + source_order * 18.0,
                            520.0,
                            94.0 + source_order * 18.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        markers = tuple(
            _node_for_source(tree, node.source_node_id)
            for node in (nodes[1], nodes[-2], nodes[-1])
        )

        self.assertTrue(all(item.kind is StructureNodeKind.CONTENT for item in markers))
        self.assertTrue(all(item.list_ordinal is None for item in markers))

    def test_ideographic_chapters_continue_an_existing_numeric_root_sequence(
        self,
    ) -> None:
        texts = (
            "5.4 报价说明",
            "6、投标说明",
            "7、附件目录",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=10,
                        block_index=source_order,
                        bbox=(
                            72.0,
                            80.0 + source_order * 24.0,
                            520.0,
                            96.0 + source_order * 24.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        projected = tuple(_node_for_source(tree, node.source_node_id) for node in nodes)

        self.assertEqual(
            [node.explicit_label for node in projected],
            ["5.4", "6", "7"],
        )
        self.assertTrue(
            all(node.kind is StructureNodeKind.SECTION for node in projected)
        )
        document = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.DOCUMENT
        )
        self.assertEqual(
            [node.parent_structure_node_id for node in projected[1:]],
            [document.structure_node_id, document.structure_node_id],
        )

    def test_trailing_outline_labels_materialize_only_as_a_coherent_sequence(
        self,
    ) -> None:
        texts = (
            "3 术语和定义",
            "本章引言。\n3.1",
            "第一项定义的完整正文。\n3.2",
            "第二项定义的完整正文。\n3.3",
            "第三项定义的完整正文。",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=8 + source_order // 4,
                        block_index=source_order,
                        bbox=(
                            72.0,
                            80.0 + (source_order % 4) * 48.0,
                            520.0,
                            112.0 + (source_order % 4) * 48.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }

        self.assertTrue({"3", "3.1", "3.2", "3.3"}.issubset(sections))
        self.assertTrue(
            all(
                sections[label].parent_structure_node_id
                == sections["3"].structure_node_id
                for label in ("3.1", "3.2", "3.3")
            )
        )
        first_definition_content = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and any(
                ref.source_id == nodes[2].source_node_id for ref in node.source_refs
            )
        )
        self.assertEqual(
            first_definition_content.parent_structure_node_id,
            sections["3.1"].structure_node_id,
        )

    def test_body_appendix_heading_starts_a_region_but_references_do_not(
        self,
    ) -> None:
        texts = (
            "4 最终要求",
            "附录 A（规范性） 试验方法",
            "1.1 试验准备",
            "见附录 B 获取参考信息。",
            "附录 B 中规定的参数仍是正文引用。",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=20 + source_order,
                        block_index=0,
                        bbox=(72.0, 80.0, 520.0, 100.0),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        projected = tuple(_node_for_source(tree, node.source_node_id) for node in nodes)

        appendix = projected[1]
        appendix_clause = projected[2]
        self.assertEqual(appendix.kind, StructureNodeKind.SECTION)
        self.assertEqual(
            appendix_clause.parent_structure_node_id,
            appendix.structure_node_id,
        )
        self.assertTrue(
            all(node.kind is StructureNodeKind.CONTENT for node in projected[3:])
        )
        self.assertTrue(all(node.explicit_label is None for node in projected[3:]))

    def test_attachment_directory_and_page_top_titles_restore_regions_in_order(
        self,
    ) -> None:
        specs = (
            ("7、附件目录", SourceNodeKind.PARAGRAPH, 1, 72.0, 100.0),
            ("1\n软件开发质量要求", SourceNodeKind.LIST_ITEM, 1, 82.0, 130.0),
            ("2\nFMEA", SourceNodeKind.LIST_ITEM, 1, 82.0, 154.0),
            ("软件开发要求", SourceNodeKind.PARAGRAPH, 2, 220.0, 40.0),
            ("供应商应建立软件开发流程。", SourceNodeKind.PARAGRAPH, 2, 72.0, 140.0),
            ("FMEA与特殊特性", SourceNodeKind.PARAGRAPH, 3, 215.0, 40.0),
            ("1 FMEA要求", SourceNodeKind.PARAGRAPH, 3, 72.0, 100.0),
            ("1.1 FMEA编制与更新", SourceNodeKind.PARAGRAPH, 3, 72.0, 130.0),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    kind,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=source_order,
                        bbox=(x0, y0, 550.0 if x0 < 100.0 else x0 + 130.0, y0 + 18.0),
                    ),
                ),
                kind=kind,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (raw_text, kind, page_number, x0, y0) in enumerate(specs)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        software_region = _node_for_source(tree, nodes[3].source_node_id)
        fmea_region = _node_for_source(tree, nodes[5].source_node_id)
        fmea_root = _node_for_source(tree, nodes[6].source_node_id)
        fmea_child = _node_for_source(tree, nodes[7].source_node_id)
        document = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.DOCUMENT
        )

        self.assertEqual(software_region.kind, StructureNodeKind.SECTION)
        self.assertEqual(fmea_region.kind, StructureNodeKind.SECTION)
        self.assertEqual(
            software_region.parent_structure_node_id,
            document.structure_node_id,
        )
        self.assertEqual(
            fmea_region.parent_structure_node_id,
            document.structure_node_id,
        )
        self.assertEqual(
            fmea_root.parent_structure_node_id,
            fmea_region.structure_node_id,
        )
        self.assertEqual(
            fmea_child.parent_structure_node_id,
            fmea_root.structure_node_id,
        )

    def test_attachment_directory_matches_title_prefix_of_merged_page_block(
        self,
    ) -> None:
        specs = (
            ("7、附件目录", 1, 72.0, 100.0, 18.0),
            ("1\n软件开发质量要求", 1, 82.0, 130.0, 18.0),
            ("2\nFMEA", 1, 82.0, 154.0, 18.0),
            ("3\n可靠性设计与验证需求", 1, 82.0, 178.0, 18.0),
            ("软件开发要求", 2, 220.0, 40.0, 18.0),
            ("软件开发正文。", 2, 72.0, 140.0, 18.0),
            ("FMEA与特殊特性", 3, 215.0, 40.0, 18.0),
            ("FMEA正文。", 3, 72.0, 140.0, 18.0),
            (
                "1.可靠性设计目标\n满足整车可靠性目标，并形成后续设计和验证要求。",
                4,
                55.0,
                72.0,
                410.0,
            ),
            ("普通正文。", 4, 72.0, 500.0, 18.0),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=source_order,
                        bbox=(
                            x0,
                            y0,
                            x0 + 130.0 if x0 >= 200.0 else 540.0,
                            y0 + height,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (raw_text, page_number, x0, y0, height) in enumerate(
                specs
            )
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        region = _node_for_source(tree, nodes[8].source_node_id)
        body = _node_for_source(tree, nodes[9].source_node_id)
        document = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.DOCUMENT
        )

        self.assertEqual(region.kind, StructureNodeKind.SECTION)
        self.assertEqual(
            region.parent_structure_node_id,
            document.structure_node_id,
        )
        self.assertEqual(
            body.parent_structure_node_id,
            region.structure_node_id,
        )

    def test_directory_proven_merged_region_keeps_wrapper_when_outline_is_split(
        self,
    ) -> None:
        specs = (
            ("7、附件目录", 1, 72.0, 100.0, 18.0),
            ("1\n软件开发质量要求", 1, 82.0, 130.0, 18.0),
            ("2\nFMEA", 1, 82.0, 154.0, 18.0),
            ("3\n可靠性设计与验证需求", 1, 82.0, 178.0, 18.0),
            ("软件开发要求", 2, 220.0, 40.0, 18.0),
            ("软件开发正文。", 2, 72.0, 140.0, 18.0),
            ("FMEA与特殊特性", 3, 215.0, 40.0, 18.0),
            ("FMEA正文。", 3, 72.0, 140.0, 18.0),
            (
                "1.可靠性设计目标\n区域引言。\n"
                "1.1 环境要求\n完整正文。\n"
                "1.2 机械要求\n完整正文。\n"
                "2.主要设计要求\n完整正文。\n"
                "3.样件数量要求：\n完整正文。\n"
                "4.集成验证要求：\n完整正文。",
                4,
                55.0,
                72.0,
                410.0,
            ),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=source_order,
                        bbox=(
                            x0,
                            y0,
                            x0 + 130.0 if x0 >= 200.0 else 540.0,
                            y0 + height,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (raw_text, page_number, x0, y0, height) in enumerate(
                specs
            )
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        region = _node_for_source(tree, nodes[-1].source_node_id)
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }
        document = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.DOCUMENT
        )

        self.assertEqual(region.kind, StructureNodeKind.SECTION)
        self.assertEqual(region.parent_structure_node_id, document.structure_node_id)
        self.assertEqual(
            sections["1.1"].parent_structure_node_id,
            sections["1"].structure_node_id,
        )
        self.assertEqual(
            sections["1.2"].parent_structure_node_id,
            sections["1"].structure_node_id,
        )
        self.assertTrue(
            all(
                sections[label].parent_structure_node_id == region.structure_node_id
                for label in ("1", "2", "3", "4")
            )
        )
        first_child_span = sections["1.2"].semantic_source_spans[0]
        self.assertNotIn(
            "2.主要设计要求",
            nodes[-1].raw_text[
                first_child_span.start_offset : first_child_span.end_offset
            ],
        )

    def test_sentence_lists_do_not_create_region_roots_even_with_dotted_items(
        self,
    ) -> None:
        specs = (
            ("7、附件目录", 1, 72.0, 100.0),
            ("1\n软件开发要求", 1, 82.0, 130.0),
            ("2\n可靠性验证要求", 1, 82.0, 154.0),
            ("软件开发要求", 2, 220.0, 40.0),
            ("软件开发正文。", 2, 72.0, 140.0),
            (
                "1. 产品应完成准备工作。\n"
                "1.1 参数一\n"
                "1.2 参数二\n"
                "2. 供应商应提交处理结果。",
                3,
                55.0,
                72.0,
            ),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=source_order,
                        bbox=(
                            x0,
                            y0,
                            x0 + 130.0 if x0 >= 200.0 else 540.0,
                            y0 + 120.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (raw_text, page_number, x0, y0) in enumerate(specs)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        candidate_source_id = nodes[-1].source_node_id

        self.assertFalse(
            any(
                node.explicit_label in {"1", "2"}
                and any(
                    ref.source_id == candidate_source_id for ref in node.source_refs
                )
                for node in tree.nodes
            )
        )

    def test_region_roots_may_start_in_the_first_body_fact_after_its_title(
        self,
    ) -> None:
        specs = (
            ("7、附件目录", 1, 72.0, 100.0),
            ("1\n软件开发要求", 1, 82.0, 130.0),
            ("2\n可靠性验证要求", 1, 82.0, 154.0),
            ("软件开发要求", 2, 220.0, 40.0),
            ("软件开发正文。", 2, 72.0, 140.0),
            ("可靠性验证要求", 3, 220.0, 40.0),
            (
                "1.可靠性设计目标\n区域引言。\n"
                "1.1 环境要求\n完整正文。\n"
                "1.2 机械要求\n完整正文。\n"
                "2.主要设计要求\n完整正文。\n"
                "3.样件数量要求：\n完整正文。\n"
                "4.集成验证要求：\n完整正文。",
                3,
                55.0,
                72.0,
            ),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=source_order,
                        bbox=(
                            x0,
                            y0,
                            x0 + 130.0 if x0 >= 200.0 else 540.0,
                            y0 + 220.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (raw_text, page_number, x0, y0) in enumerate(specs)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        region = _node_for_source(tree, nodes[-2].source_node_id)
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
            and any(
                ref.source_id == nodes[-1].source_node_id for ref in node.source_refs
            )
        }

        self.assertEqual(region.kind, StructureNodeKind.SECTION)
        self.assertTrue(
            all(
                sections[label].parent_structure_node_id == region.structure_node_id
                for label in ("1", "2", "3", "4")
            )
        )
        self.assertEqual(
            sections["1.1"].parent_structure_node_id,
            sections["1"].structure_node_id,
        )
        self.assertEqual(
            sections["1.2"].parent_structure_node_id,
            sections["1"].structure_node_id,
        )

    def test_region_roots_do_not_start_after_an_ordinary_body_fact(
        self,
    ) -> None:
        specs = (
            ("7、附件目录", 1, 72.0, 100.0),
            ("1\n软件开发要求", 1, 82.0, 130.0),
            ("2\n可靠性验证要求", 1, 82.0, 154.0),
            ("软件开发要求", 2, 220.0, 40.0),
            ("软件开发正文。", 2, 72.0, 140.0),
            ("可靠性验证要求", 3, 220.0, 40.0),
            ("这是区域的普通引言正文。", 3, 72.0, 72.0),
            (
                "1.可靠性设计目标\n区域引言。\n"
                "1.1 环境要求\n完整正文。\n"
                "1.2 机械要求\n完整正文。\n"
                "2.主要设计要求\n完整正文。",
                3,
                55.0,
                110.0,
            ),
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=source_order,
                        bbox=(
                            x0,
                            y0,
                            x0 + 130.0 if x0 >= 200.0 else 540.0,
                            y0 + 120.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (raw_text, page_number, x0, y0) in enumerate(specs)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        candidate_source_id = nodes[-1].source_node_id

        self.assertFalse(
            any(
                node.explicit_label in {"1", "2"}
                and any(
                    ref.source_id == candidate_source_id for ref in node.source_refs
                )
                for node in tree.nodes
            )
        )

    def test_full_width_table_cannot_establish_a_region_root_sequence(
        self,
    ) -> None:
        leading_specs = (
            ("7、附件目录", 1, 72.0, 100.0),
            ("1\n软件开发要求", 1, 82.0, 130.0),
            ("2\n可靠性验证要求", 1, 82.0, 154.0),
            ("软件开发要求", 2, 220.0, 40.0),
            ("软件开发正文。", 2, 72.0, 140.0),
            ("可靠性验证要求", 3, 220.0, 40.0),
        )
        leading_nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=source_order,
                        bbox=(
                            x0,
                            y0,
                            x0 + 130.0 if x0 >= 200.0 else 540.0,
                            y0 + 18.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (raw_text, page_number, x0, y0) in enumerate(
                leading_specs
            )
        )
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=3,
            table_index=0,
            bbox=(40.0, 80.0, 560.0, 240.0),
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            native_locator=table_locator,
        )
        cell_texts = (
            "1. 普通处理步骤\n1.1 参数一\n1.2 参数二",
            "2. 提交处理结果",
        )
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, 0),
                row=row,
                column=0,
                column_span=2,
                raw_text=raw_text,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=3,
                    table_index=0,
                    row_index=row,
                    column_index=0,
                    bbox=(40.0, 80.0 + row * 80.0, 560.0, 160.0 + row * 80.0),
                ),
            )
            for row, raw_text in enumerate(cell_texts)
        )
        table = SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.TABLE, table_locator),
            kind=SourceNodeKind.TABLE,
            source_order=len(leading_nodes),
            raw_text="",
            native_locator=table_locator,
            table_payload=TableIR(
                table_id=table_id,
                row_count=2,
                column_count=2,
                cells=cells,
                merged_regions=tuple(
                    MergedRegion(
                        row=row,
                        column=0,
                        column_span=2,
                    )
                    for row in range(2)
                ),
            ),
        )
        glyph_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=3,
            block_index=len(leading_nodes),
            bbox=(45.0, 85.0, 550.0, 230.0),
        )
        glyph = SourceNode(
            source_node_id=_source_node_id(
                SourceNodeKind.PARAGRAPH,
                glyph_locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=len(leading_nodes) + 1,
            raw_text="\n".join(cell_texts),
            native_locator=glyph_locator,
        )

        tree = build_structure_tree(
            _source_snapshot(nodes=(*leading_nodes, table, glyph))
        )

        self.assertFalse(
            any(
                node.explicit_label in {"1", "2"}
                and any(
                    ref.source_id == glyph.source_node_id for ref in node.source_refs
                )
                for node in tree.nodes
            )
        )

    def test_repeated_appendix_marker_does_not_create_duplicate_regions(
        self,
    ) -> None:
        texts = (
            "附件4--诊断、OTA相关标准",
            "附件4 --诊断要求",
            "3.1 诊断设计要求",
        )
        nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=15,
                        block_index=source_order,
                        bbox=(
                            80.0,
                            100.0 + source_order * 40.0,
                            400.0,
                            120.0 + source_order * 40.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, raw_text in enumerate(texts)
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        first = _node_for_source(tree, nodes[0].source_node_id)
        repeated = _node_for_source(tree, nodes[1].source_node_id)
        child = _node_for_source(tree, nodes[2].source_node_id)

        self.assertEqual(first.kind, StructureNodeKind.SECTION)
        self.assertEqual(repeated.kind, StructureNodeKind.CONTENT)
        self.assertEqual(
            repeated.parent_structure_node_id,
            first.structure_node_id,
        )
        self.assertEqual(
            child.parent_structure_node_id,
            first.structure_node_id,
        )

    def test_directory_proven_region_folded_in_layout_table_owns_the_table(
        self,
    ) -> None:
        leading_specs = (
            ("7、附件目录", 1, 72.0, 100.0),
            ("1\n软件开发要求", 1, 82.0, 130.0),
            ("2\n网络安全接口协议", 1, 82.0, 154.0),
            ("软件开发要求", 2, 220.0, 40.0),
            ("软件开发正文。", 2, 72.0, 140.0),
        )
        leading_nodes = tuple(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator := NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        block_index=source_order,
                        bbox=(
                            x0,
                            y0,
                            x0 + 130.0 if x0 >= 200.0 else 540.0,
                            y0 + 18.0,
                        ),
                    ),
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
            for source_order, (raw_text, page_number, x0, y0) in enumerate(
                leading_specs
            )
        )
        layout_table = _pdf_table_node(
            page_number=3,
            table_index=0,
            bbox=(40.0, 40.0, 560.0, 220.0),
            source_order=len(leading_nodes),
            header=("网络安全接口协议", "工作产出"),
            data=("活动", "要求"),
        )
        title_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=3,
            block_index=0,
            bbox=(180.0, 60.0, 290.0, 76.0),
        )
        folded_title = SourceNode(
            source_node_id=_source_node_id(
                SourceNodeKind.PARAGRAPH,
                title_locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=len(leading_nodes) + 1,
            raw_text="本网络安全接口协议签署版",
            native_locator=title_locator,
        )

        tree = build_structure_tree(
            _source_snapshot(
                nodes=(*leading_nodes, layout_table, folded_title),
            )
        )
        region = _node_for_source(tree, folded_title.source_node_id)
        table = _node_for_source(tree, layout_table.source_node_id)
        document = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.DOCUMENT
        )

        self.assertEqual(region.kind, StructureNodeKind.SECTION)
        self.assertEqual(
            region.parent_structure_node_id,
            document.structure_node_id,
        )
        self.assertEqual(
            table.parent_structure_node_id,
            region.structure_node_id,
        )

    def test_single_level_trailing_dot_requires_native_heading_evidence(
        self,
    ) -> None:
        paragraph_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=20,
            block_index=0,
            bbox=(72.0, 80.0, 500.0, 94.0),
        )
        list_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=20,
            block_index=1,
            bbox=(72.0, 110.0, 500.0, 124.0),
        )
        heading_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=20,
            block_index=2,
            bbox=(72.0, 140.0, 500.0, 154.0),
        )
        nodes = (
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    paragraph_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=0,
                raw_text="2. 产品按计划完成。",
                native_locator=paragraph_locator,
            ),
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.LIST_ITEM,
                    list_locator,
                ),
                kind=SourceNodeKind.LIST_ITEM,
                source_order=1,
                raw_text="5．电涡流传感器转子总成",
                native_locator=list_locator,
            ),
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.HEADING,
                    heading_locator,
                ),
                kind=SourceNodeKind.HEADING,
                source_order=2,
                raw_text="3. 原生标题",
                native_locator=heading_locator,
            ),
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        paragraph = _node_for_source(tree, nodes[0].source_node_id)
        list_item = _node_for_source(tree, nodes[1].source_node_id)
        heading = _node_for_source(tree, nodes[2].source_node_id)

        self.assertIsNone(paragraph.explicit_label)
        self.assertIsNone(list_item.explicit_label)
        self.assertIsNot(paragraph.kind, StructureNodeKind.SECTION)
        self.assertIsNot(list_item.kind, StructureNodeKind.SECTION)
        self.assertEqual(heading.kind, StructureNodeKind.SECTION)
        self.assertEqual(heading.explicit_label, "3")

    def test_trailing_dot_cross_reference_stays_with_current_section(self) -> None:
        specs = (
            ("6.4.1. 电气性能测试要求", 80.0, 260.0),
            ("6.4.2. 通信测试要求", 110.0, 260.0),
            ("测试结果应满足本节已经定义并经过验证的全部", 140.0, 500.0),
            ("6.4.1. 的要求。", 162.0, 190.0),
        )
        nodes: list[SourceNode] = []
        for source_order, (raw_text, y0, x1) in enumerate(specs):
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=42,
                block_index=source_order,
                bbox=(90.0, y0, x1, y0 + 14.0),
            )
            nodes.append(
                SourceNode(
                    source_node_id=_source_node_id(
                        SourceNodeKind.PARAGRAPH,
                        locator,
                    ),
                    kind=SourceNodeKind.PARAGRAPH,
                    source_order=source_order,
                    raw_text=raw_text,
                    native_locator=locator,
                )
            )

        tree = build_structure_tree(_source_snapshot(nodes=tuple(nodes)))
        current_section = _node_for_source(tree, nodes[1].source_node_id)
        cross_reference = _node_for_source(tree, nodes[3].source_node_id)

        self.assertEqual(current_section.explicit_label, "6.4.2")
        self.assertEqual(cross_reference.kind, StructureNodeKind.CONTENT)
        self.assertIsNone(cross_reference.explicit_label)
        self.assertEqual(
            cross_reference.parent_structure_node_id,
            current_section.structure_node_id,
        )

    def test_numbered_heading_is_not_vetoed_without_prior_cross_reference_evidence(
        self,
    ) -> None:
        for case, body_text, candidate_y, candidate_kind, heading_text in (
            (
                "first-seen-tight",
                "本节前一段很长但版式中没有句末标点并延伸到页面右侧",
                132.0,
                SourceNodeKind.PARAGRAPH,
                "2.4.4 安装要求：",
            ),
            (
                "completed",
                "上一段已经结束。",
                132.0,
                SourceNodeKind.PARAGRAPH,
                "2.4.4 真正标题。",
            ),
            (
                "spaced",
                "上一段尚未结束",
                180.0,
                SourceNodeKind.PARAGRAPH,
                "2.4.4 真正标题。",
            ),
            (
                "open-heading",
                "上一段尚未结束",
                132.0,
                SourceNodeKind.PARAGRAPH,
                "2.4.4 真正标题",
            ),
            (
                "native-heading",
                "上一段尚未结束",
                132.0,
                SourceNodeKind.HEADING,
                "2.4.4 真正标题。",
            ),
        ):
            with self.subTest(case=case):
                body_locator = NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=9,
                    block_index=0,
                    bbox=(90.0, 110.0, 500.0, 124.0),
                )
                heading_locator = NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=9,
                    block_index=1,
                    bbox=(90.0, candidate_y, 260.0, candidate_y + 14.0),
                )
                nodes = (
                    SourceNode(
                        source_node_id=_source_node_id(
                            SourceNodeKind.PARAGRAPH,
                            body_locator,
                        ),
                        kind=SourceNodeKind.PARAGRAPH,
                        source_order=0,
                        raw_text=body_text,
                        native_locator=body_locator,
                    ),
                    SourceNode(
                        source_node_id=_source_node_id(
                            candidate_kind,
                            heading_locator,
                        ),
                        kind=candidate_kind,
                        source_order=1,
                        raw_text=heading_text,
                        native_locator=heading_locator,
                    ),
                )

                tree = build_structure_tree(_source_snapshot(nodes=nodes))
                heading = _node_for_source(tree, nodes[1].source_node_id)

                self.assertEqual(heading.kind, StructureNodeKind.SECTION)
                self.assertEqual(heading.explicit_label, "2.4.4")

    def test_short_lead_in_does_not_veto_a_long_numeric_heading(self) -> None:
        lead_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=9,
            block_index=0,
            bbox=(90.0, 110.0, 170.0, 124.0),
        )
        heading_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=9,
            block_index=1,
            bbox=(90.0, 132.0, 360.0, 146.0),
        )
        nodes = (
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    lead_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=0,
                raw_text="测试项目",
                native_locator=lead_locator,
            ),
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    heading_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=1,
                raw_text="2.4.4 真正的完整编号标题。",
                native_locator=heading_locator,
            ),
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        heading = _node_for_source(tree, nodes[1].source_node_id)

        self.assertEqual(heading.kind, StructureNodeKind.SECTION)
        self.assertEqual(heading.explicit_label, "2.4.4")

    def test_chinese_section_candidate_is_outside_numeric_continuation_veto(
        self,
    ) -> None:
        lead_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=9,
            block_index=0,
            bbox=(90.0, 110.0, 500.0, 124.0),
        )
        heading_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=9,
            block_index=1,
            bbox=(90.0, 132.0, 220.0, 146.0),
        )
        nodes = (
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    lead_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=0,
                raw_text="上一段尚未结束",
                native_locator=lead_locator,
            ),
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    heading_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=1,
                raw_text="二、真正章节。",
                native_locator=heading_locator,
            ),
        )
        explicit_labels = {
            nodes[0].source_node_id: None,
            nodes[1].source_node_id: "2",
        }

        resolved = _veto_native_continuation_section_labels(
            nodes,
            explicit_labels=explicit_labels,
            excluded_source_ids=set(),
        )

        self.assertEqual(resolved[nodes[1].source_node_id], "2")

    def test_table_55_rows_are_locatable_structure_not_promoted_requirements(
        self,
    ) -> None:
        source = _pdf_source_snapshot()
        source_table = source.nodes[2]
        table_payload = source_table.table_payload
        assert table_payload is not None
        tree = build_structure_tree(source)

        table = _node_for_source(tree, source_table.source_node_id)
        rows = _table_rows(tree, table.structure_node_id)

        self.assertEqual(table.kind, StructureNodeKind.TABLE)
        self.assertEqual([row.table_row_index for row in rows], [0, 1, 2])
        self.assertTrue(
            all(row.parent_structure_node_id == table.structure_node_id for row in rows)
        )
        for row in rows:
            referenced_cell_ids = {
                ref.source_id
                for ref in row.source_refs
                if ref.kind is StructureSourceRefKind.TABLE_CELL
            }
            expected_cell_ids = {
                cell.cell_id
                for cell in table_payload.cells
                if cell.row <= (row.table_row_index or 0) < cell.row + cell.row_span
            }
            self.assertEqual(referenced_cell_ids, expected_cell_ids)

        # StructureTree deliberately has no requirement/promotion field. A row
        # can be reviewed independently without becoming a requirement here.
        self.assertNotIn("is_requirement", StructureNode.model_fields)
        self.assertNotIn("semantic_class", StructureNode.model_fields)
        invalid_row = rows[0].model_dump(mode="python")
        invalid_row["is_requirement"] = True
        with self.assertRaisesRegex(ValidationError, "is_requirement"):
            StructureNode.model_validate(invalid_row)

    def test_adjacent_pdf_table_segments_need_strong_continuation_evidence(
        self,
    ) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source()
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)
        table_continuations = [
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.CONTINUES
            and relation.source_structure_node_id == first_table.structure_node_id
            and relation.target_structure_node_id == second_table.structure_node_id
        ]

        self.assertEqual(len(table_continuations), 1)
        self.assertEqual(table_continuations[0].source_refs, second_table.source_refs)

    def test_adjacent_pdf_table_segments_without_repeated_header_merge_on_exact_grid(
        self,
    ) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source(repeat_header=False)
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)
        table_continuations = [
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.CONTINUES
            and relation.source_structure_node_id == first_table.structure_node_id
            and relation.target_structure_node_id == second_table.structure_node_id
        ]

        self.assertEqual(len(table_continuations), 1)

    def test_sparse_next_page_content_extent_does_not_hide_page_top_table(
        self,
    ) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source(
                heading_bbox=(70.0, 30.0, 500.0, 48.0),
                first_table_bbox=(50.0, 72.0, 550.0, 780.0),
                second_table_bbox=(50.0, 72.0, 550.0, 219.0),
            )
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)

        self.assertTrue(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.source_structure_node_id
                == first_table.structure_node_id
                and relation.target_structure_node_id
                == second_table.structure_node_id
                for relation in tree.relations
            )
        )

    def test_sparse_next_page_fallback_rejects_page_middle_table(self) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source(
                first_table_bbox=(50.0, 180.0, 550.0, 780.0),
                second_table_bbox=(50.0, 180.0, 550.0, 400.0),
            )
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.source_structure_node_id
                == first_table.structure_node_id
                and relation.target_structure_node_id
                == second_table.structure_node_id
                for relation in tree.relations
            )
        )

    def test_sparse_next_page_fallback_rejects_misaligned_top_table(self) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source(
                heading_bbox=(70.0, 30.0, 500.0, 48.0),
                first_table_bbox=(50.0, 72.0, 550.0, 780.0),
                second_table_bbox=(50.0, 92.0, 550.0, 219.0),
            )
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.source_structure_node_id
                == first_table.structure_node_id
                and relation.target_structure_node_id
                == second_table.structure_node_id
                for relation in tree.relations
            )
        )

    def test_caption_bounded_last_table_wins_continuation_competition(
        self,
    ) -> None:
        source, earlier_table, header_table, next_page_table = (
            _competing_cross_page_tables_source(with_caption_boundary=True)
        )

        tree = build_structure_tree(source)
        earlier = _node_for_source(tree, earlier_table.source_node_id)
        header = _node_for_source(tree, header_table.source_node_id)
        target = _node_for_source(tree, next_page_table.source_node_id)
        table_continuations = [
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.CONTINUES
            and relation.target_structure_node_id == target.structure_node_id
        ]

        self.assertEqual(len(table_continuations), 1)
        self.assertEqual(
            table_continuations[0].source_structure_node_id,
            header.structure_node_id,
        )
        self.assertNotEqual(
            table_continuations[0].source_structure_node_id,
            earlier.structure_node_id,
        )

    def test_equally_plausible_table_predecessors_remain_fail_closed(
        self,
    ) -> None:
        source, _, _, next_page_table = _competing_cross_page_tables_source(
            with_caption_boundary=False
        )

        tree = build_structure_tree(source)
        target = _node_for_source(tree, next_page_table.source_node_id)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.target_structure_node_id == target.structure_node_id
                for relation in tree.relations
            )
        )

    def test_target_page_new_table_caption_vetoes_single_continuation_candidate(
        self,
    ) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source(target_caption="表 23 新参数")
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.source_structure_node_id
                == first_table.structure_node_id
                and relation.target_structure_node_id
                == second_table.structure_node_id
                for relation in tree.relations
            )
        )

    def test_discontinued_word_is_not_a_table_continuation_marker(self) -> None:
        for caption in (
            "Table 23 Discontinued items",
            "Table 23 Continued items",
        ):
            with self.subTest(caption=caption):
                source, first_source_table, second_source_table, _ = (
                    _continued_pdf_table_source(target_caption=caption)
                )

                tree = build_structure_tree(source)
                first_table = _node_for_source(
                    tree,
                    first_source_table.source_node_id,
                )
                second_table = _node_for_source(
                    tree,
                    second_source_table.source_node_id,
                )

                self.assertFalse(
                    any(
                        relation.kind is StructureRelationKind.CONTINUES
                        and relation.source_structure_node_id
                        == first_table.structure_node_id
                        and relation.target_structure_node_id
                        == second_table.structure_node_id
                        for relation in tree.relations
                    )
                )

    def test_parenthesized_new_table_caption_vetoes_continuation(self) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source(target_caption="表 23（新参数）")
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.source_structure_node_id
                == first_table.structure_node_id
                and relation.target_structure_node_id
                == second_table.structure_node_id
                for relation in tree.relations
            )
        )

    def test_explicit_continued_caption_does_not_veto_compatible_table_edge(
        self,
    ) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source(target_caption="表 22（续）")
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)

        self.assertTrue(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.source_structure_node_id
                == first_table.structure_node_id
                and relation.target_structure_node_id
                == second_table.structure_node_id
                for relation in tree.relations
            )
        )

    def test_matching_continued_caption_overrides_merged_grid_partition_difference(
        self,
    ) -> None:
        source, first_source_table, second_source_table = (
            _continued_pdf_table_partition_source(
                first_column_widths=(120.0, 120.0, 120.0, 120.0),
                second_column_widths=(
                    35.0,
                    45.0,
                    55.0,
                    65.0,
                    50.0,
                    45.0,
                    55.0,
                    65.0,
                    65.0,
                ),
                source_caption="表 4 工作模式",
                target_caption="表 4（续）",
            )
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)
        nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
        outgoing = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.CONTINUES
            and relation.source_structure_node_id == first_table.structure_node_id
        )

        self.assertEqual(len(outgoing), 1)
        self.assertEqual(
            outgoing[0].target_structure_node_id,
            second_table.structure_node_id,
        )
        self.assertEqual(
            nodes_by_id[outgoing[0].target_structure_node_id].kind,
            StructureNodeKind.TABLE,
        )

        mismatched, mismatch_first, mismatch_second = (
            _continued_pdf_table_partition_source(
                first_column_widths=(120.0, 120.0, 120.0, 120.0),
                second_column_widths=(
                    35.0,
                    45.0,
                    55.0,
                    65.0,
                    50.0,
                    45.0,
                    55.0,
                    65.0,
                    65.0,
                ),
                source_caption="表 4 工作模式",
                target_caption="表 5（续）",
            )
        )
        mismatch_tree = build_structure_tree(mismatched)
        mismatch_first_node = _node_for_source(
            mismatch_tree,
            mismatch_first.source_node_id,
        )
        mismatch_second_node = _node_for_source(
            mismatch_tree,
            mismatch_second.source_node_id,
        )
        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.source_structure_node_id
                == mismatch_first_node.structure_node_id
                and relation.target_structure_node_id
                == mismatch_second_node.structure_node_id
                for relation in mismatch_tree.relations
            )
        )

    def test_explicit_continued_caption_must_match_known_source_label(
        self,
    ) -> None:
        for target_caption, expected_continuation in (
            ("表 22（续）", True),
            ("表 23（续）", False),
            ("续表 22", True),
            ("续表 23", False),
            ("Continued Table 22", True),
            ("Continued Table 23", False),
        ):
            with self.subTest(target_caption=target_caption):
                source, first_source_table, second_source_table, _ = (
                    _continued_pdf_table_source(
                        source_caption="表 22 参数要求",
                        target_caption=target_caption,
                    )
                )

                tree = build_structure_tree(source)
                first_table = _node_for_source(
                    tree,
                    first_source_table.source_node_id,
                )
                second_table = _node_for_source(
                    tree,
                    second_source_table.source_node_id,
                )
                has_continuation = any(
                    relation.kind is StructureRelationKind.CONTINUES
                    and relation.source_structure_node_id
                    == first_table.structure_node_id
                    and relation.target_structure_node_id
                    == second_table.structure_node_id
                    for relation in tree.relations
                )

                self.assertEqual(has_continuation, expected_continuation)

    def test_adjacent_pdf_table_segments_merge_when_one_column_partition_refines_the_other(
        self,
    ) -> None:
        coarse = (326.928, 62.88, 54.096)
        refined = (44.647, 282.24, 62.88, 54.015)
        for first_widths, second_widths in (
            (coarse, refined),
            (refined, coarse),
        ):
            with self.subTest(
                first_column_count=len(first_widths),
                second_column_count=len(second_widths),
            ):
                source, first_source_table, second_source_table = (
                    _continued_pdf_table_partition_source(
                        first_column_widths=first_widths,
                        second_column_widths=second_widths,
                    )
                )

                tree = build_structure_tree(source)
                first_table = _node_for_source(
                    tree,
                    first_source_table.source_node_id,
                )
                second_table = _node_for_source(
                    tree,
                    second_source_table.source_node_id,
                )
                table_continuations = [
                    relation
                    for relation in tree.relations
                    if relation.kind is StructureRelationKind.CONTINUES
                    and relation.source_structure_node_id
                    == first_table.structure_node_id
                    and relation.target_structure_node_id
                    == second_table.structure_node_id
                ]

                self.assertEqual(len(table_continuations), 1)

    def test_adjacent_pdf_tables_with_crossing_column_partitions_do_not_merge(
        self,
    ) -> None:
        source, first_source_table, second_source_table = (
            _continued_pdf_table_partition_source(
                first_column_widths=(326.928, 62.88, 54.096),
                second_column_widths=(100.0, 150.0, 100.0, 93.782),
            )
        )

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        second_table = _node_for_source(tree, second_source_table.source_node_id)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.source_structure_node_id == first_table.structure_node_id
                and relation.target_structure_node_id == second_table.structure_node_id
                for relation in tree.relations
            )
        )

    def test_adjacent_pdf_tables_without_strong_structure_evidence_do_not_merge(
        self,
    ) -> None:
        for options in (
            {"same_section": False},
            {"horizontal_offset": 80.0},
            {"repeat_header": False, "second_column_split_ratio": 0.35},
            {"repeat_header": True, "second_column_split_ratio": 0.35},
        ):
            with self.subTest(options=options):
                source, _, _, _ = _continued_pdf_table_source(**options)
                tree = build_structure_tree(source)
                nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
                table_continuations = [
                    relation
                    for relation in tree.relations
                    if relation.kind is StructureRelationKind.CONTINUES
                    and nodes_by_id[relation.source_structure_node_id].kind
                    is StructureNodeKind.TABLE
                    and nodes_by_id[relation.target_structure_node_id].kind
                    is StructureNodeKind.TABLE
                ]
                self.assertEqual(table_continuations, [])

    def test_cross_page_continuation_into_folded_table_uses_complete_target_grounding(
        self,
    ) -> None:
        source, source_table, overlay = _cross_page_folded_table_source()

        tree = build_structure_tree(source)
        table = _node_for_source(tree, source_table.source_node_id)
        continuation = next(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.CONTINUES
            and relation.target_structure_node_id == table.structure_node_id
        )

        self.assertEqual(continuation.source_refs, table.source_refs)
        self.assertIn(
            StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=overlay.source_node_id,
            ),
            continuation.source_refs,
        )

    def test_pdf_raw_text_inside_table_is_row_grounding_not_duplicate_content(
        self,
    ) -> None:
        source, first_source_table, _, overlay = _continued_pdf_table_source()

        tree = build_structure_tree(source)
        first_table = _node_for_source(tree, first_source_table.source_node_id)
        first_row = _table_rows(tree, first_table.structure_node_id)[0]
        first_table_payload = first_source_table.table_payload
        assert first_table_payload is not None
        duplicated_cell = next(
            cell
            for cell in first_table_payload.cells
            if cell.row == 0 and cell.column == 0
        )

        self.assertNotIn(
            f"source:{overlay.source_node_id}",
            {node.identity_key for node in tree.nodes},
        )
        self.assertIn(
            StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=overlay.source_node_id,
            ),
            first_row.source_refs,
        )
        self.assertIn(
            StructureSourceRef(
                kind=StructureSourceRefKind.TABLE_CELL,
                source_id=duplicated_cell.cell_id,
            ),
            first_row.source_refs,
        )
        self.assertNotIn(
            StructureSourceRef(
                kind=StructureSourceRefKind.TABLE_CELL,
                source_id=duplicated_cell.cell_id,
            ),
            first_row.effective_semantic_source_refs,
        )
        self.assertIn(overlay.source_node_id, tree.source_node_ids)

    def test_pdf_raw_text_channel_replaces_conflicting_table_cell_text_once(
        self,
    ) -> None:
        source, source_table, overlay = _pdf_table_text_channel_source()

        tree = build_structure_tree(source)
        table = _node_for_source(tree, source_table.source_node_id)
        data_row = _table_rows(tree, table.structure_node_id)[1]
        table_payload = source_table.table_payload
        assert table_payload is not None
        data_cells = tuple(cell for cell in table_payload.cells if cell.row == 1)

        self.assertEqual(
            data_row.effective_semantic_source_refs,
            (
                StructureSourceRef(
                    kind=StructureSourceRefKind.SOURCE_NODE,
                    source_id=overlay.source_node_id,
                ),
            ),
        )
        self.assertTrue(
            all(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
                not in data_row.effective_semantic_source_refs
                for cell in data_cells
            )
        )
        finding = next(
            item
            for item in tree.findings
            if item.code == "pdf_table_text_channel_conflict"
        )
        self.assertEqual(finding.structure_node_id, data_row.structure_node_id)
        self.assertEqual(finding.severity, StructureFindingSeverity.INFO)
        self.assertFalse(finding.blocking)
        self.assertEqual(
            set(finding.source_refs),
            {
                StructureSourceRef(
                    kind=StructureSourceRefKind.SOURCE_NODE,
                    source_id=overlay.source_node_id,
                ),
                *(
                    StructureSourceRef(
                        kind=StructureSourceRefKind.TABLE_CELL,
                        source_id=cell.cell_id,
                    )
                    for cell in data_cells
                ),
            },
        )
        semantics = classify_content(
            source,
            tree,
            scope_structure_node_ids=(data_row.structure_node_id,),
        )
        plan = build_semantic_unit_plan(source, tree, semantics)
        self.assertEqual(
            tuple(span.source_ref for span in plan.units[0].source_spans),
            (
                StructureSourceRef(
                    kind=StructureSourceRefKind.SOURCE_NODE,
                    source_id=overlay.source_node_id,
                ),
            ),
        )
        self.assertTrue(
            all(
                span.source_ref.kind is not StructureSourceRefKind.TABLE_CELL
                for span in semantics.annotations[0].grounding_spans
            )
        )

    def test_pdf_raw_text_channel_does_not_replace_cells_across_rows(
        self,
    ) -> None:
        source, source_table, overlay = _pdf_table_text_channel_source(
            span_multiple_rows=True,
        )

        tree = build_structure_tree(source)
        table = _node_for_source(tree, source_table.source_node_id)
        rows = _table_rows(tree, table.structure_node_id)
        table_payload = source_table.table_payload
        assert table_payload is not None

        self.assertTrue(
            all(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
                in rows[cell.row].source_refs
                for cell in table_payload.cells
            )
        )
        self.assertTrue(
            all(
                StructureSourceRef(
                    kind=StructureSourceRefKind.SOURCE_NODE,
                    source_id=overlay.source_node_id,
                )
                not in row.source_refs
                for row in rows
            )
        )

    def test_pdf_raw_text_channel_tolerates_bounded_glyph_spill_at_row_edge(
        self,
    ) -> None:
        source, source_table, overlay = _pdf_table_text_channel_source(
            glyph_spill=True,
        )

        tree = build_structure_tree(source)
        table = _node_for_source(tree, source_table.source_node_id)
        header_row = _table_rows(tree, table.structure_node_id)[0]
        data_row = _table_rows(tree, table.structure_node_id)[1]
        overlay_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=overlay.source_node_id,
        )

        self.assertIn(overlay_ref, header_row.source_refs)
        self.assertEqual(
            header_row.effective_semantic_source_refs,
            (overlay_ref,),
        )
        self.assertNotIn(overlay_ref, data_row.source_refs)
        self.assertFalse(
            any(
                item.code == "pdf_table_text_channel_conflict" for item in tree.findings
            )
        )

    def test_nested_pdf_grid_owns_rows_while_outer_full_width_heading_survives(
        self,
    ) -> None:
        broad_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            table_index=0,
            bbox=(40.0, 100.0, 560.0, 400.0),
        )
        broad_table_id = stable_table_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            native_locator=broad_locator,
        )
        broad_cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(broad_table_id, row, 0),
                row=row,
                column=0,
                raw_text=raw_text,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    table_index=0,
                    row_index=row,
                    column_index=0,
                    bbox=(40.0, 100.0 + row * 150.0, 560.0, 250.0 + row * 150.0),
                ),
            )
            for row, raw_text in enumerate(("5.2 交付要求", "1\n设计方案"))
        )
        broad = SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.TABLE, broad_locator),
            kind=SourceNodeKind.TABLE,
            source_order=0,
            raw_text="",
            native_locator=broad_locator,
            table_payload=TableIR(
                table_id=broad_table_id,
                row_count=2,
                column_count=1,
                cells=broad_cells,
            ),
        )
        nested = _pdf_table_node(
            page_number=1,
            table_index=1,
            bbox=(50.0, 250.0, 550.0, 400.0),
            source_order=1,
            header=("交付物", "阶段"),
            data=("方案", "TR"),
        )
        heading_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=0,
            bbox=(52.0, 120.0, 548.0, 140.0),
        )
        heading = SourceNode(
            source_node_id=_source_node_id(
                SourceNodeKind.PARAGRAPH,
                heading_locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=2,
            raw_text="5.2 交付要求",
            native_locator=heading_locator,
        )
        row_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=1,
            bbox=(60.0, 270.0, 520.0, 290.0),
        )
        nested_row = SourceNode(
            source_node_id=_source_node_id(
                SourceNodeKind.PARAGRAPH,
                row_locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=3,
            raw_text="1\n设计方案",
            native_locator=row_locator,
        )

        tree = build_structure_tree(
            _source_snapshot(nodes=(broad, nested, heading, nested_row))
        )
        heading_node = _node_for_source(tree, heading.source_node_id)
        nested_table = _node_for_source(tree, nested.source_node_id)
        nested_row_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=nested_row.source_node_id,
        )

        self.assertEqual(heading_node.kind, StructureNodeKind.SECTION)
        self.assertEqual(heading_node.explicit_label, "5.2")
        self.assertNotIn(
            f"source:{nested_row.source_node_id}",
            {node.identity_key for node in tree.nodes},
        )
        self.assertTrue(
            any(
                nested_row_ref in node.source_refs
                for node in tree.nodes
                if node.structure_node_id == nested_table.structure_node_id
                or node.parent_structure_node_id == nested_table.structure_node_id
            )
        )

    def test_parenthesized_outline_inside_layout_table_keeps_deep_ancestry(
        self,
    ) -> None:
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            table_index=0,
            bbox=(40.0, 100.0, 560.0, 250.0),
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            native_locator=table_locator,
        )
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, 0),
                row=row,
                column=0,
                raw_text=text,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    table_index=0,
                    row_index=row,
                    column_index=0,
                    bbox=(40.0, 100.0 + row * 30.0, 560.0, 130.0 + row * 30.0),
                ),
            )
            for row, text in enumerate(
                (
                    "6 ）控制器总成要求",
                    "6.1）简述",
                    "控制器通过 CAN FD 与整车域控制器交互。",
                    "7）可靠性要求",
                )
            )
        )
        table = SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.TABLE, table_locator),
            kind=SourceNodeKind.TABLE,
            source_order=0,
            raw_text="",
            native_locator=table_locator,
            table_payload=TableIR(
                table_id=table_id,
                row_count=4,
                column_count=1,
                cells=cells,
            ),
        )

        def overlay(
            *,
            block_index: int,
            y0: float,
            text: str,
        ) -> SourceNode:
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                block_index=block_index,
                bbox=(45.0, y0, 555.0, y0 + 18.0),
            )
            return SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=block_index + 1,
                raw_text=text,
                native_locator=locator,
            )

        parent_heading = overlay(
            block_index=0,
            y0=106.0,
            text="6 ）控制器总成要求",
        )
        child_heading = overlay(
            block_index=1,
            y0=136.0,
            text="6.1）简述",
        )
        content = overlay(
            block_index=2,
            y0=166.0,
            text="控制器通过 CAN FD 与整车域控制器交互。",
        )
        sibling_heading = overlay(
            block_index=3,
            y0=196.0,
            text="7）可靠性要求",
        )

        tree = build_structure_tree(
            _source_snapshot(
                nodes=(
                    table,
                    parent_heading,
                    child_heading,
                    content,
                    sibling_heading,
                ),
            )
        )
        parent_node = _node_for_source(tree, parent_heading.source_node_id)
        child_node = _node_for_source(tree, child_heading.source_node_id)
        content_node = _node_for_source(tree, content.source_node_id)
        sibling_node = _node_for_source(tree, sibling_heading.source_node_id)
        table_node = _node_for_source(tree, table.source_node_id)

        self.assertEqual(parent_node.kind, StructureNodeKind.CLAUSE)
        self.assertEqual(parent_node.list_ordinal, 6)
        self.assertEqual(child_node.kind, StructureNodeKind.SECTION)
        self.assertEqual(child_node.explicit_label, "6.1")
        self.assertEqual(
            child_node.parent_structure_node_id,
            parent_node.structure_node_id,
        )
        self.assertEqual(sibling_node.kind, StructureNodeKind.CLAUSE)
        self.assertEqual(sibling_node.list_ordinal, 7)
        self.assertEqual(
            sibling_node.parent_structure_node_id,
            parent_node.parent_structure_node_id,
        )
        self.assertEqual(content_node.kind, StructureNodeKind.CONTENT)
        self.assertEqual(
            content_node.parent_structure_node_id,
            child_node.structure_node_id,
        )
        self.assertEqual(
            table_node.parent_structure_node_id,
            next(
                node.structure_node_id
                for node in tree.nodes
                if node.kind is StructureNodeKind.DOCUMENT
            ),
        )
        content_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=content.source_node_id,
        )
        self.assertFalse(
            any(
                content_ref in row.effective_semantic_source_refs
                for row in _table_rows(tree, table_node.structure_node_id)
            )
        )

    def test_nested_list_ordinal_cannot_replace_an_explicit_section_parent(
        self,
    ) -> None:
        def paragraph(
            *,
            source_order: int,
            page_number: int,
            text: str,
        ) -> SourceNode:
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=page_number,
                block_index=source_order,
                bbox=(
                    72.0,
                    80.0 + source_order * 18.0,
                    520.0,
                    94.0 + source_order * 18.0,
                ),
            )
            return SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=text,
                native_locator=locator,
            )

        nodes = (
            paragraph(
                source_order=0,
                page_number=1,
                text="4 主章节",
            ),
            paragraph(
                source_order=1,
                page_number=1,
                text="4.7 子章节",
            ),
            paragraph(
                source_order=2,
                page_number=1,
                text="4.7.3 深层条款",
            ),
            paragraph(
                source_order=3,
                page_number=1,
                text="4）内部试验项目",
            ),
            paragraph(
                source_order=4,
                page_number=1,
                text="5）第二个试验项目",
            ),
            paragraph(
                source_order=5,
                page_number=2,
                text="4.8 后续同级章节",
            ),
        )

        tree = build_structure_tree(_source_snapshot(nodes=nodes))
        root_section = _node_for_source(tree, nodes[0].source_node_id)
        nested_list = _node_for_source(tree, nodes[3].source_node_id)
        following_section = _node_for_source(tree, nodes[5].source_node_id)

        self.assertEqual(root_section.explicit_label, "4")
        self.assertEqual(nested_list.kind, StructureNodeKind.CLAUSE)
        self.assertEqual(nested_list.list_ordinal, 4)
        self.assertEqual(following_section.explicit_label, "4.8")
        self.assertEqual(
            following_section.parent_structure_node_id,
            root_section.structure_node_id,
        )
        self.assertNotEqual(
            following_section.parent_structure_node_id,
            nested_list.structure_node_id,
        )

    def test_vertical_merged_anchor_applies_to_all_three_rows_without_text_copy(
        self,
    ) -> None:
        source = _pdf_source_snapshot()
        source_table = source.nodes[2]
        table_payload = source_table.table_payload
        assert table_payload is not None
        merged_anchor = next(
            cell for cell in table_payload.cells if cell.row == 0 and cell.column == 1
        )
        tree = build_structure_tree(source)
        table = _node_for_source(tree, source_table.source_node_id)
        rows = _table_rows(tree, table.structure_node_id)
        shared = next(
            node
            for node in tree.nodes
            if node.boundary_basis is StructureBoundaryBasis.MERGED_RANGE
        )

        self.assertEqual(
            shared.source_refs,
            (
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=merged_anchor.cell_id,
                ),
            ),
        )
        applies_to = [
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.APPLIES_TO
            and relation.source_structure_node_id == shared.structure_node_id
        ]
        self.assertEqual(
            {relation.target_structure_node_id for relation in applies_to},
            {row.structure_node_id for row in rows},
        )
        self.assertEqual(len(applies_to), 3)
        self.assertTrue(
            all(relation.source_refs == shared.source_refs for relation in applies_to)
        )
        self.assertNotIn("三行共享验收条件", tree.model_dump_json())

    def test_full_width_trailing_table_note_applies_only_to_parameter_rows(
        self,
    ) -> None:
        source, source_table, _, _ = _note_and_figure_source()

        tree = build_structure_tree(source)
        table = _node_for_source(tree, source_table.source_node_id)
        rows = _table_rows(tree, table.structure_node_id)
        by_id = {node.structure_node_id: node for node in tree.nodes}
        applies_to = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.APPLIES_TO
            and relation.source_structure_node_id == rows[3].structure_node_id
        )

        self.assertEqual(
            [
                by_id[item.target_structure_node_id].table_row_index
                for item in applies_to
            ],
            [1, 2],
        )
        table_payload = source_table.table_payload
        assert table_payload is not None
        note_cell = next(
            cell for cell in table_payload.cells if cell.row == 3 and cell.column == 0
        )
        self.assertTrue(
            all(
                relation.source_refs
                == (
                    StructureSourceRef(
                        kind=StructureSourceRefKind.TABLE_CELL,
                        source_id=note_cell.cell_id,
                    ),
                )
                for relation in applies_to
            )
        )

    def test_reference_table_warns_that_external_content_was_not_loaded(
        self,
    ) -> None:
        source = _pdf_source_snapshot()
        nodes = list(source.nodes)
        nodes[0] = nodes[0].model_copy(update={"raw_text": "1.9 Normative references"})
        source = source.model_copy(update={"nodes": tuple(nodes)})
        before = source.model_dump_json()

        tree = build_structure_tree(source)
        findings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "external_reference_content_not_loaded"
        )

        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0].blocking)
        self.assertEqual(source.model_dump_json(), before)

    def test_numbered_body_reference_and_caption_link_the_same_native_image(
        self,
    ) -> None:
        source, _, image_source, _ = _note_and_figure_source()

        tree = build_structure_tree(source)
        image = _node_for_source(tree, image_source.source_node_id)
        references = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.REFERENCES
            and relation.target_structure_node_id == image.structure_node_id
        )

        self.assertEqual(len(references), 2)
        self.assertEqual(
            {relation.source_structure_node_id for relation in references},
            {
                node.structure_node_id
                for node in tree.nodes
                if node.parent_structure_node_id == image.parent_structure_node_id
                and node.source_order
                in {image.source_order - 1, image.source_order + 1}
            },
        )
        visual_findings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "unstructured_visual_requirement"
        )
        self.assertEqual(len(visual_findings), 1)
        self.assertEqual(
            visual_findings[0].structure_node_id,
            image.structure_node_id,
        )
        self.assertFalse(visual_findings[0].blocking)

    def test_late_materialized_figure_uses_pdf_geometry_for_reference_order(
        self,
    ) -> None:
        source, _, image_source, _ = _note_and_figure_source()
        source_nodes = [
            node
            for node in source.nodes
            if node.source_node_id != image_source.source_node_id
        ]
        source_nodes.append(image_source)
        source = source.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(update={"source_order": source_order})
                    for source_order, node in enumerate(source_nodes)
                )
            }
        )

        tree = build_structure_tree(source)
        image = _node_for_source(tree, image_source.source_node_id)
        references = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.REFERENCES
            and relation.target_structure_node_id == image.structure_node_id
        )

        self.assertEqual(len(references), 2)
        self.assertEqual(
            {relation.source_structure_node_id for relation in references},
            {
                node.structure_node_id
                for node in tree.nodes
                if node.parent_structure_node_id == image.parent_structure_node_id
                and node.source_order
                in {image.source_order - 1, image.source_order + 1}
            },
        )

    def test_duplicate_same_page_table_caption_warns_without_source_mutation(
        self,
    ) -> None:
        source, _, _, captions = _note_and_figure_source()
        before = tuple(node.raw_text for node in source.nodes)

        tree = build_structure_tree(source)
        warnings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "duplicate_table_caption_label"
        )

        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            warnings[0].structure_node_id,
            _node_for_source(tree, captions[1].source_node_id).structure_node_id,
        )
        self.assertEqual(tuple(node.raw_text for node in source.nodes), before)

    def test_different_composite_table_captions_do_not_cluster(self) -> None:
        source, _, _, _ = _note_and_figure_source(
            first_table_caption="表 8.4 合成公差表",
            second_table_caption="Table 8.5 Synthetic second table",
        )

        tree = build_structure_tree(source)
        warnings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "duplicate_table_caption_label"
        )

        self.assertEqual(warnings, ())

    def test_duplicate_composite_table_caption_warns_once(self) -> None:
        source, _, _, captions = _note_and_figure_source(
            first_table_caption="表 8.4 合成公差表",
            second_table_caption="Table 8.4 Synthetic second table",
        )

        tree = build_structure_tree(source)
        warnings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "duplicate_table_caption_label"
        )

        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            warnings[0].structure_node_id,
            _node_for_source(tree, captions[1].source_node_id).structure_node_id,
        )

    def test_spaced_same_table_captions_cluster_after_normalization(self) -> None:
        source, _, _, captions = _note_and_figure_source(
            first_table_caption="表 8 . 4 合成公差表",
            second_table_caption="Table 8.4 Synthetic second table",
        )

        tree = build_structure_tree(source)
        warnings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "duplicate_table_caption_label"
        )

        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            warnings[0].structure_node_id,
            _node_for_source(tree, captions[1].source_node_id).structure_node_id,
        )

    def test_spaced_distinct_table_captions_do_not_cluster(self) -> None:
        source, _, _, _ = _note_and_figure_source(
            first_table_caption="表 8 . 4 合成公差表",
            second_table_caption="Table 8 . 5 Synthetic second table",
        )

        tree = build_structure_tree(source)
        warnings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "duplicate_table_caption_label"
        )

        self.assertEqual(warnings, ())

    def test_fullwidth_dot_and_hyphen_table_labels_are_equivalent(self) -> None:
        source, _, _, captions = _note_and_figure_source(
            first_table_caption="表 8．4 合成公差表",
            second_table_caption="Table 8-4 Synthetic second table",
        )

        tree = build_structure_tree(source)
        warnings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "duplicate_table_caption_label"
        )

        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            warnings[0].structure_node_id,
            _node_for_source(tree, captions[1].source_node_id).structure_node_id,
        )

    def test_spaced_fullwidth_overlong_table_caption_does_not_cluster(
        self,
    ) -> None:
        overlong_label = "1 . 2．3 - 4 . 5．6 - 7 . 8 ． 9"
        source, _, _, _ = _note_and_figure_source(
            first_table_caption=f"表 {overlong_label} 合成公差表",
            second_table_caption=f"Table {overlong_label} Synthetic second table",
        )

        tree = build_structure_tree(source)
        warnings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "duplicate_table_caption_label"
        )

        self.assertEqual(warnings, ())

    def test_figure_reference_accepts_common_sentence_delimiters_after_label(
        self,
    ) -> None:
        cases = (
            ("chinese-comma", "系统方案如下图1，主要部件应按图布置。"),
            ("ascii-comma", "See Figure 1, which defines the system boundary."),
            ("chinese-semicolon", "系统边界参见图1；其余接口另行定义。"),
            ("ascii-semicolon", "See Figure 1; all interfaces shall comply."),
            ("enumeration-comma", "系统方案如下图1、主要部件应按图布置。"),
        )
        for case, body_text in cases:
            with self.subTest(case=case):
                caption_text = "图1 合成系统示意图"
                source, _, image_source, _ = _note_and_figure_source(
                    figure_body_references=(body_text,),
                    figure_caption=caption_text,
                )
                body = next(
                    node for node in source.nodes if node.raw_text == body_text
                )
                caption = next(
                    node for node in source.nodes if node.raw_text == caption_text
                )

                tree = build_structure_tree(source)
                image = _node_for_source(tree, image_source.source_node_id)
                reference_source_ids = {
                    relation.source_structure_node_id
                    for relation in tree.relations
                    if relation.kind is StructureRelationKind.REFERENCES
                    and relation.target_structure_node_id
                    == image.structure_node_id
                }

                self.assertEqual(
                    reference_source_ids,
                    {
                        _node_for_source(
                            tree,
                            body.source_node_id,
                        ).structure_node_id,
                        _node_for_source(
                            tree,
                            caption.source_node_id,
                        ).structure_node_id,
                    },
                )

    def test_composite_figure_reference_requires_the_exact_full_label(self) -> None:
        exact_label = "8.4-3.2-1.5-9.7"
        other_label = "8.4-3.2-1.5-9.8"
        source, _, image_source, _ = _note_and_figure_source(
            figure_body_references=(
                f"参见图 {exact_label} 的计算边界。",
                f"参见图 {other_label} 的其他边界。",
            ),
            figure_caption=f"图 {exact_label} 合成计算示意图",
        )
        exact_body = next(
            node
            for node in source.nodes
            if node.raw_text == f"参见图 {exact_label} 的计算边界。"
        )
        other_body = next(
            node
            for node in source.nodes
            if node.raw_text == f"参见图 {other_label} 的其他边界。"
        )
        caption = next(
            node
            for node in source.nodes
            if node.raw_text == f"图 {exact_label} 合成计算示意图"
        )

        tree = build_structure_tree(source)
        image = _node_for_source(tree, image_source.source_node_id)
        reference_source_ids = {
            relation.source_structure_node_id
            for relation in tree.relations
            if relation.kind is StructureRelationKind.REFERENCES
            and relation.target_structure_node_id == image.structure_node_id
        }

        self.assertEqual(
            reference_source_ids,
            {
                _node_for_source(tree, exact_body.source_node_id).structure_node_id,
                _node_for_source(tree, caption.source_node_id).structure_node_id,
            },
        )
        self.assertNotIn(
            _node_for_source(tree, other_body.source_node_id).structure_node_id,
            reference_source_ids,
        )

    def test_spaced_fullwidth_figure_labels_match_only_the_canonical_label(
        self,
    ) -> None:
        exact_label = "8 . 4 - 3"
        other_label = "8．4-4"
        caption_label = "8-4．3"
        source, _, image_source, _ = _note_and_figure_source(
            figure_body_references=(
                f"参见图 {exact_label} 的计算边界。",
                f"参见图 {other_label} 的其他边界。",
            ),
            figure_caption=f"图 {caption_label} 合成计算示意图",
        )
        exact_body = next(
            node
            for node in source.nodes
            if node.raw_text == f"参见图 {exact_label} 的计算边界。"
        )
        other_body = next(
            node
            for node in source.nodes
            if node.raw_text == f"参见图 {other_label} 的其他边界。"
        )
        caption = next(
            node
            for node in source.nodes
            if node.raw_text == f"图 {caption_label} 合成计算示意图"
        )

        tree = build_structure_tree(source)
        image = _node_for_source(tree, image_source.source_node_id)
        reference_source_ids = {
            relation.source_structure_node_id
            for relation in tree.relations
            if relation.kind is StructureRelationKind.REFERENCES
            and relation.target_structure_node_id == image.structure_node_id
        }

        self.assertEqual(
            reference_source_ids,
            {
                _node_for_source(tree, exact_body.source_node_id).structure_node_id,
                _node_for_source(tree, caption.source_node_id).structure_node_id,
            },
        )
        self.assertNotIn(
            _node_for_source(tree, other_body.source_node_id).structure_node_id,
            reference_source_ids,
        )

    def test_caption_number_does_not_truncate_beyond_eight_segments(self) -> None:
        overlong_label = "1.2.3.4.5.6.7.8.9"
        source, _, image_source, _ = _note_and_figure_source(
            figure_body_references=(f"参见图 {overlong_label}。",),
            figure_caption=f"图 {overlong_label} 合成计算示意图",
        )

        tree = build_structure_tree(source)
        image = _node_for_source(tree, image_source.source_node_id)
        references = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.REFERENCES
            and relation.target_structure_node_id == image.structure_node_id
        )

        self.assertEqual(references, ())

    def test_spaced_fullwidth_caption_number_does_not_truncate_ninth_segment(
        self,
    ) -> None:
        overlong_label = "1 . 2．3 - 4 . 5．6 - 7 . 8 ． 9"
        source, _, image_source, _ = _note_and_figure_source(
            figure_body_references=(f"参见图 {overlong_label}。",),
            figure_caption=f"图 {overlong_label} 合成计算示意图",
        )

        tree = build_structure_tree(source)
        image = _node_for_source(tree, image_source.source_node_id)
        references = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.REFERENCES
            and relation.target_structure_node_id == image.structure_node_id
        )

        self.assertEqual(references, ())

    def test_explicit_sibling_option_marks_preserve_three_states(self) -> None:
        source = _flat_pdf_snapshot_from_texts(
            (
                (1, 0, 100.0, "4.1\n法规与认证"),
                (1, 1, 130.0, "请选择适用区域"),
                (1, 2, 160.0, "[  √  ] 区域甲"),
                (1, 3, 180.0, "[     ] 区域乙"),
                (1, 4, 200.0, "[  ?  ] 区域丙"),
            )
        )

        tree = build_structure_tree(source)
        option_relations = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.OPTION_STATE
        )

        self.assertEqual(
            tuple(relation.option_state for relation in option_relations),
            (
                StructureOptionState.CHECKED,
                StructureOptionState.UNCHECKED,
                StructureOptionState.UNCERTAIN,
            ),
        )
        nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
        self.assertTrue(
            all(
                nodes_by_id[relation.target_structure_node_id].source_refs
                == relation.source_refs
                and nodes_by_id[
                    relation.target_structure_node_id
                ].parent_structure_node_id
                == relation.source_structure_node_id
                for relation in option_relations
            )
        )
        uncertain_findings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "uncertain_option_state"
        )
        self.assertEqual(len(uncertain_findings), 1)
        self.assertTrue(uncertain_findings[0].blocking)

    def test_single_bracketed_fragment_is_not_assumed_to_be_an_option(self) -> None:
        source = _flat_pdf_snapshot_from_texts(
            (
                (1, 0, 100.0, "4.1\n普通章节"),
                (1, 1, 130.0, "[  √  ] 单个编辑标记"),
                (1, 2, 160.0, "普通说明"),
            )
        )

        tree = build_structure_tree(source)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.OPTION_STATE
                for relation in tree.relations
            )
        )

    def test_docx_table_cell_paragraph_is_folded_into_row_without_duplicate_content(
        self,
    ) -> None:
        source, table_node, cell_paragraph, cell = _docx_table_cell_snapshot()
        tree = build_structure_tree(source)
        table = _node_for_source(tree, table_node.source_node_id)
        rows = _table_rows(tree, table.structure_node_id)

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            {ref.source_id for ref in rows[0].source_refs},
            {cell.cell_id, cell_paragraph.source_node_id},
        )
        self.assertFalse(
            any(
                node.identity_key == f"source:{cell_paragraph.source_node_id}"
                for node in tree.nodes
            )
        )
        self.assertEqual(
            [node.kind for node in tree.nodes],
            [
                StructureNodeKind.DOCUMENT,
                StructureNodeKind.TABLE,
                StructureNodeKind.TABLE_ROW,
            ],
        )

    def test_all_docx_table_cell_paragraph_kinds_fold_into_the_same_row(self) -> None:
        source, table_node, paragraph, cell = _docx_table_cell_snapshot()
        heading_locator = paragraph.native_locator.model_copy(
            update={"paragraph_index": 1}
        )
        list_locator = paragraph.native_locator.model_copy(
            update={"paragraph_index": 2}
        )
        heading = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=source.source_sha256,
                adapter_id=source.adapter_id,
                adapter_version=source.adapter_version,
                kind=SourceNodeKind.HEADING,
                native_locator=heading_locator,
            ),
            kind=SourceNodeKind.HEADING,
            source_order=2,
            raw_text="2.1.22 高低温特性",
            native_locator=heading_locator,
            parent_source_node_id=table_node.source_node_id,
        )
        list_item = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=source.source_sha256,
                adapter_id=source.adapter_id,
                adapter_version=source.adapter_version,
                kind=SourceNodeKind.LIST_ITEM,
                native_locator=list_locator,
            ),
            kind=SourceNodeKind.LIST_ITEM,
            source_order=3,
            raw_text="样本数量：6件/OTS",
            native_locator=list_locator,
            parent_source_node_id=table_node.source_node_id,
            docx_list_metadata=DocxListMetadata(num_id=42, ilvl=2),
        )
        payload = source.model_dump(mode="python")
        payload["nodes"] = [
            table_node.model_dump(mode="python"),
            paragraph.model_dump(mode="python"),
            heading.model_dump(mode="python"),
            list_item.model_dump(mode="python"),
        ]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)
        table = _node_for_source(tree, table_node.source_node_id)
        row = _table_rows(tree, table.structure_node_id)[0]

        self.assertEqual(
            {ref.source_id for ref in row.source_refs},
            {
                cell.cell_id,
                paragraph.source_node_id,
                heading.source_node_id,
                list_item.source_node_id,
            },
        )
        self.assertEqual(
            [node.kind for node in tree.nodes],
            [
                StructureNodeKind.DOCUMENT,
                StructureNodeKind.TABLE,
                StructureNodeKind.TABLE_ROW,
            ],
        )

    def test_docx_table_cell_image_is_grounded_on_the_row_not_promoted_to_table_content(
        self,
    ) -> None:
        source, table_node, paragraph, cell = _docx_table_cell_snapshot()
        image_locator = paragraph.native_locator.model_copy(update={"object_index": 0})
        asset = SourceAssetRef(
            asset_id="asset-table-cell-image",
            kind=SourceAssetKind.IMAGE,
            content_sha256="d" * 64,
            media_type="image/png",
            storage_key="ooxml/word/media/image1.png",
            native_locator=image_locator,
        )
        image_node = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=source.source_sha256,
                adapter_id=source.adapter_id,
                adapter_version=source.adapter_version,
                kind=SourceNodeKind.IMAGE,
                native_locator=image_locator,
            ),
            kind=SourceNodeKind.IMAGE,
            source_order=2,
            raw_text="",
            native_locator=image_locator,
            parent_source_node_id=paragraph.source_node_id,
            asset_ref=asset.asset_id,
        )
        payload = source.model_dump(mode="python")
        payload["nodes"] = [
            table_node.model_dump(mode="python"),
            paragraph.model_dump(mode="python"),
            image_node.model_dump(mode="python"),
        ]
        payload["assets"] = [asset.model_dump(mode="python")]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)
        table = _node_for_source(tree, table_node.source_node_id)
        row = _table_rows(tree, table.structure_node_id)[0]

        self.assertEqual(
            {ref.source_id for ref in row.source_refs},
            {cell.cell_id, paragraph.source_node_id, image_node.source_node_id},
        )
        self.assertFalse(
            any(
                node.identity_key == f"source:{image_node.source_node_id}"
                for node in tree.nodes
            )
        )
        self.assertEqual(
            [node.kind for node in tree.nodes],
            [
                StructureNodeKind.DOCUMENT,
                StructureNodeKind.TABLE,
                StructureNodeKind.TABLE_ROW,
            ],
        )

    def test_pdf_image_is_related_to_one_geometrically_unique_table_cell(
        self,
    ) -> None:
        source, table_node, image_node, target_cell = _pdf_table_image_snapshot(
            image_bbox=(185.0, 50.0, 235.0, 80.0)
        )

        tree = build_structure_tree(source)
        image = _node_for_source(tree, image_node.source_node_id)
        table = _node_for_source(tree, table_node.source_node_id)
        placements = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.PLACED_IN_CELL
        )

        self.assertEqual(len(placements), 1)
        self.assertEqual(
            placements[0].source_structure_node_id, image.structure_node_id
        )
        self.assertEqual(
            placements[0].target_structure_node_id, table.structure_node_id
        )
        self.assertEqual(
            tuple((ref.kind, ref.source_id) for ref in placements[0].source_refs),
            (
                (StructureSourceRefKind.SOURCE_NODE, image_node.source_node_id),
                (StructureSourceRefKind.TABLE_CELL, target_cell.cell_id),
            ),
        )
        self.assertEqual(image.kind, StructureNodeKind.CONTENT)
        self.assertEqual(
            {ref.source_id for ref in image.source_refs},
            {image_node.source_node_id},
        )
        self.assertFalse(
            any(
                finding.code == "pdf_image_table_cell_placement_unresolved"
                for finding in tree.findings
            )
        )

    def test_pdf_opaque_image_balanced_across_two_native_cells_has_one_owner_set(
        self,
    ) -> None:
        source, table_node, image_node, _ = _pdf_table_image_snapshot(
            image_bbox=(140.0, 50.0, 180.0, 80.0)
        )

        tree = build_structure_tree(source)
        image = _node_for_source(tree, image_node.source_node_id)
        placements = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.PLACED_OVER_CELLS
        )
        expected_cells = tuple(
            sorted(
                (cell for cell in table_node.table_payload.cells if cell.row == 0),
                key=lambda cell: (cell.row, cell.column),
            )
        )

        self.assertEqual(
            tuple((ref.kind, ref.source_id) for ref in placements[0].source_refs),
            (
                (StructureSourceRefKind.SOURCE_NODE, image_node.source_node_id),
                *tuple(
                    (StructureSourceRefKind.TABLE_CELL, cell.cell_id)
                    for cell in expected_cells
                ),
            ),
        )
        self.assertEqual(len(placements), 1)
        self.assertEqual(
            placements[0].source_structure_node_id, image.structure_node_id
        )
        self.assertFalse(
            any(
                finding.code == "pdf_image_table_cell_placement_unresolved"
                for finding in tree.findings
            )
        )
        view = build_document_view(source, tree)
        table = view["tables"][0]
        image_cells = [cell for cell in table["cells"] if cell["images"]]
        self.assertEqual(len(image_cells), 1)
        self.assertEqual(
            image_cells[0]["images"][0]["cell_placement"],
            {
                "relation_kind": "placed_over_cells",
                "covered_cell_ids": [cell.cell_id for cell in expected_cells],
            },
        )

    def test_pdf_image_inside_overlapping_tables_is_not_assigned_to_either(
        self,
    ) -> None:
        source, _, image_node, _ = _pdf_table_image_snapshot(
            image_bbox=(185.0, 50.0, 235.0, 80.0),
            duplicate_table=True,
        )

        tree = build_structure_tree(source)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.PLACED_IN_CELL
                for relation in tree.relations
            )
        )
        findings = tuple(
            finding
            for finding in tree.findings
            if finding.code == "pdf_image_table_cell_placement_unresolved"
            and any(
                ref.source_id == image_node.source_node_id
                for ref in finding.source_refs
            )
        )
        self.assertEqual(len(findings), 1)

    def test_strict_pdf_raster_and_native_text_can_share_one_proven_cell(
        self,
    ) -> None:
        source, _, _, target_cell = _pdf_table_image_snapshot(
            image_bbox=(185.0, 50.0, 235.0, 80.0),
            target_cell_text="native text already present",
        )

        tree = build_structure_tree(source)
        placements = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.PLACED_IN_CELL
        )

        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].source_refs[1].source_id, target_cell.cell_id)
        self.assertFalse(
            any(
                finding.code == "pdf_image_table_cell_placement_unresolved"
                for finding in tree.findings
            )
        )

    def test_pdf_image_with_bounded_cell_edge_rounding_has_one_proven_owner(
        self,
    ) -> None:
        source, _, _, target_cell = _pdf_table_image_snapshot(
            image_bbox=(157.5, 50.0, 235.0, 80.0),
            target_cell_text="native text remains independently grounded",
        )

        tree = build_structure_tree(source)
        placements = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.PLACED_IN_CELL
        )

        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0].source_refs[1].source_id, target_cell.cell_id)
        self.assertFalse(
            any(
                finding.code == "pdf_image_table_cell_placement_unresolved"
                for finding in tree.findings
            )
        )

    def test_pdf_image_with_material_cross_cell_overlap_remains_blocked(
        self,
    ) -> None:
        source, _, image_node, _ = _pdf_table_image_snapshot(
            image_bbox=(155.0, 50.0, 235.0, 80.0),
            target_cell_text="native text remains independently grounded",
        )

        tree = build_structure_tree(source)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.PLACED_IN_CELL
                for relation in tree.relations
            )
        )
        self.assertTrue(
            any(
                finding.code == "pdf_image_table_cell_placement_unresolved"
                and any(
                    ref.source_id == image_node.source_node_id
                    for ref in finding.source_refs
                )
                for finding in tree.findings
            )
        )

    def test_pdf_alpha_footprint_maps_exactly_to_four_connected_cells(self) -> None:
        runs = (
            SourceAlphaFootprintRun(y=0, x_start=0, x_end=4),
            SourceAlphaFootprintRun(y=1, x_start=0, x_end=4),
        )
        footprint = SourceAlphaFootprint(
            width_px=4,
            height_px=2,
            runs=runs,
            nonzero_sample_count=8,
            evidence_sha256=stable_source_alpha_footprint_evidence_sha256(
                width_px=4,
                height_px=2,
                runs=runs,
                nonzero_sample_count=8,
            ),
        )
        source, table_node, image_node, _ = _pdf_table_image_snapshot(
            image_bbox=(40.0, 40.0, 280.0, 140.0),
            asset_derivation=SourceAssetDerivation(
                kind=SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE,
                base_content_sha256="a" * 64,
                mask_content_sha256="b" * 64,
                alpha_footprint=footprint,
            ),
        )

        tree = build_structure_tree(source)
        placements = tuple(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.PLACED_OVER_CELLS
        )
        expected_cells = tuple(
            sorted(
                table_node.table_payload.cells,
                key=lambda cell: (cell.row, cell.column),
            )
        )

        self.assertEqual(len(placements), 1)
        self.assertEqual(
            tuple(ref.source_id for ref in placements[0].source_refs),
            (
                image_node.source_node_id,
                *(cell.cell_id for cell in expected_cells),
            ),
        )
        self.assertFalse(
            any(
                finding.code == "pdf_image_table_cell_placement_unresolved"
                for finding in tree.findings
            )
        )

    def test_pdf_alpha_composite_without_exact_footprint_is_blocked(self) -> None:
        source, _, image_node, _ = _pdf_table_image_snapshot(
            image_bbox=(40.0, 40.0, 280.0, 140.0),
            asset_derivation=SourceAssetDerivation(
                kind=SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE,
                base_content_sha256="a" * 64,
                mask_content_sha256="b" * 64,
            ),
        )

        tree = build_structure_tree(source)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.PLACED_OVER_CELLS
                for relation in tree.relations
            )
        )
        self.assertTrue(
            any(
                finding.code == "pdf_image_table_cell_alpha_footprint_unavailable"
                and any(
                    ref.source_id == image_node.source_node_id
                    for ref in finding.source_refs
                )
                for finding in tree.findings
            )
        )

    def test_alpha_footprint_contract_rejects_noncanonical_or_tampered_rle(
        self,
    ) -> None:
        first = SourceAlphaFootprintRun(y=0, x_start=0, x_end=2)
        touching = SourceAlphaFootprintRun(y=0, x_start=2, x_end=4)
        evidence = stable_source_alpha_footprint_evidence_sha256(
            width_px=4,
            height_px=1,
            runs=(first, touching),
            nonzero_sample_count=4,
        )

        with self.assertRaisesRegex(ValidationError, "maximally merged"):
            SourceAlphaFootprint(
                width_px=4,
                height_px=1,
                runs=(first, touching),
                nonzero_sample_count=4,
                evidence_sha256=evidence,
            )
        with self.assertRaisesRegex(ValidationError, "canonical exact RLE"):
            SourceAlphaFootprint(
                width_px=4,
                height_px=1,
                runs=(first,),
                nonzero_sample_count=2,
                evidence_sha256="0" * 64,
            )

    def test_pdf_image_requires_unambiguous_asset_type_hash_and_location(
        self,
    ) -> None:
        source, _, image_node, _ = _pdf_table_image_snapshot(
            image_bbox=(185.0, 50.0, 235.0, 80.0)
        )
        base_asset = source.assets[0]
        mismatched_locator = image_node.native_locator.model_copy(
            update={"bbox": (186.0, 50.0, 236.0, 80.0)}
        )
        variants = {
            "type": (base_asset.model_copy(update={"kind": SourceAssetKind.FIGURE}),),
            "location": (
                base_asset.model_copy(update={"native_locator": mismatched_locator}),
            ),
        }

        for evidence, assets in variants.items():
            with self.subTest(evidence=evidence):
                payload = source.model_dump(mode="python")
                payload["assets"] = [
                    asset.model_dump(mode="python") for asset in assets
                ]
                unstable_source = SourceIRSnapshot.model_validate(payload)
                tree = build_structure_tree(unstable_source)

                self.assertFalse(
                    any(
                        relation.kind is StructureRelationKind.PLACED_IN_CELL
                        for relation in tree.relations
                    )
                )
                self.assertTrue(
                    any(
                        finding.code == "pdf_image_table_cell_placement_unresolved"
                        for finding in tree.findings
                    )
                )

        invalid_hash_payload = source.model_dump(mode="python")
        invalid_hash_payload["assets"][0]["content_sha256"] = "0" * 63
        with self.assertRaisesRegex(ValidationError, "content_sha256"):
            SourceIRSnapshot.model_validate(invalid_hash_payload)

    def test_short_pdf_raster_tile_is_not_treated_as_cell_content(self) -> None:
        source, _, _, _ = _pdf_table_image_snapshot(
            image_bbox=(185.0, 50.0, 206.0, 62.75),
            target_cell_text="native text remains independently grounded",
        )

        tree = build_structure_tree(source)

        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.PLACED_IN_CELL
                for relation in tree.relations
            )
        )
        self.assertFalse(
            any(
                finding.code == "pdf_image_table_cell_placement_unresolved"
                for finding in tree.findings
            )
        )

    def test_failed_source_ir_is_rejected_instead_of_looking_successful(self) -> None:
        source = _empty_source_snapshot(status=SourceIRStatus.FAILED)

        with self.assertRaises(StructureResolutionRejected) as caught:
            build_structure_tree(source)

        self.assertEqual(
            caught.exception.code,
            StructureResolutionRejectionCode.SOURCE_IR_FAILED,
        )

    def test_flat_pdf_blocks_restore_outline_lists_and_cross_page_scope(self) -> None:
        source = _flat_pdf_outline_snapshot()

        tree = build_structure_tree(source)
        sections_by_label = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }
        leaf = sections_by_label["4.12.2.1"]

        ancestor_labels: list[str] = []
        current = leaf
        nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
        while current.parent_structure_node_id is not None:
            current = nodes_by_id[current.parent_structure_node_id]
            if current.explicit_label is not None:
                ancestor_labels.append(current.explicit_label)
        self.assertEqual(list(reversed(ancestor_labels)), ["4", "4.12", "4.12.2"])

        target = sections_by_label["4.13.3"]
        numbered_children = sorted(
            (
                node
                for node in tree.nodes
                if node.kind is StructureNodeKind.CLAUSE
                and node.parent_structure_node_id == target.structure_node_id
            ),
            key=lambda node: node.list_ordinal or 0,
        )
        self.assertEqual(
            [node.list_ordinal for node in numbered_children], [1, 2, 3, 4, 5]
        )

        continuation = [
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.CONTINUES
        ]
        self.assertTrue(continuation)
        source_nodes = {node.source_node_id: node for node in source.nodes}
        relevant = next(
            relation
            for relation in continuation
            if source_nodes[
                relation.source_refs[0].source_id
            ].native_locator.page_number
            == 2
        )
        self.assertEqual(
            nodes_by_id[relevant.source_structure_node_id].list_ordinal,
            4,
        )
        self.assertEqual(
            nodes_by_id[relevant.target_structure_node_id].list_ordinal,
            5,
        )

        repeated_headers = [
            node
            for node in tree.nodes
            if node.source_refs
            and source_nodes[node.source_refs[0].source_id].raw_text
            == "STANDARD HEADER"
        ]
        document_root = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.DOCUMENT
        )
        self.assertTrue(repeated_headers)
        self.assertTrue(
            all(
                node.parent_structure_node_id == document_root.structure_node_id
                for node in repeated_headers
            )
        )
        self.assertFalse(any(node.explicit_label == "50" for node in tree.nodes))

    def test_toc_chinese_chapters_and_changing_page_numbers_are_structural(
        self,
    ) -> None:
        source = _fidelity_layout_source_snapshot()

        tree = build_structure_tree(source)
        source_by_id = {node.source_node_id: node for node in source.nodes}
        toc_blocks = [
            node
            for node in tree.nodes
            if node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK
        ]

        self.assertEqual(len(toc_blocks), 1)
        toc_texts = [
            source_by_id[ref.source_id].raw_text for ref in toc_blocks[0].source_refs
        ]
        self.assertEqual(
            toc_texts,
            [
                "目\n录",
                "一、项目总体描述",
                "1.1 项目时间节点",
                "1.2 产品信息",
                "六、测试要求",
                "6.1 样车/样机的测试",
                "七、其它",
            ],
        )
        toc_source_ids = {ref.source_id for ref in toc_blocks[0].source_refs}
        presentation = build_structure_presentation_index(source, structure=tree)
        toc_presentation = presentation["nodes"][
            toc_blocks[0].structure_node_id
        ]
        self.assertEqual(
            toc_presentation["toc_source_text_by_id"],
            {
                ref.source_id: source_by_id[ref.source_id].raw_text
                for ref in toc_blocks[0].source_refs
            },
        )
        self.assertIsNotNone(
            validated_structure_presentation_index(
                presentation,
                expected_source_sha256=source.source_sha256,
                expected_structure_snapshot_id=tree.structure_snapshot_id,
            )
        )
        self.assertFalse(
            any(
                node.kind is StructureNodeKind.SECTION
                and any(ref.source_id in toc_source_ids for ref in node.source_refs)
                for node in tree.nodes
            )
        )

        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.kind is StructureNodeKind.SECTION
            and node.explicit_label is not None
        }
        self.assertEqual(set(sections), {"1", "1.1", "1.2", "6", "6.1", "7"})
        self.assertEqual(
            sections["1.1"].parent_structure_node_id,
            sections["1"].structure_node_id,
        )
        self.assertEqual(
            sections["1.2"].parent_structure_node_id,
            sections["1"].structure_node_id,
        )
        self.assertEqual(
            sections["6.1"].parent_structure_node_id,
            sections["6"].structure_node_id,
        )

        page_number_nodes = [
            node
            for node in tree.nodes
            if node.source_refs
            and source_by_id[node.source_refs[0].source_id].raw_text
            in {"21", "22", "23", "24", "25"}
        ]
        document_root = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.DOCUMENT
        )
        self.assertEqual(len(page_number_nodes), 5)
        self.assertTrue(
            all(
                node.parent_structure_node_id == document_root.structure_node_id
                for node in page_number_nodes
            )
        )
        self.assertEqual(
            [
                node.raw_text
                for node in source.nodes
                if node.raw_text in {"21", "22", "23", "24", "25"}
            ],
            ["21", "22", "23", "24", "25"],
        )

    def test_plain_chinese_enumeration_is_not_promoted_to_document_sections(
        self,
    ) -> None:
        source = _flat_pdf_snapshot_from_texts(
            (
                (1, 0, 100.0, "一、准备材料"),
                (1, 1, 130.0, "将材料放置在工作台。"),
                (1, 2, 170.0, "二、连接设备"),
                (1, 3, 200.0, "按操作说明完成连接。"),
                (1, 4, 240.0, "三、记录结果"),
                (1, 5, 270.0, "将测量值写入记录表。"),
            )
        )

        tree = build_structure_tree(source)

        self.assertFalse(
            any(
                node.kind is StructureNodeKind.SECTION
                and node.explicit_label in {"1", "2", "3"}
                for node in tree.nodes
            )
        )

    def test_single_bottom_number_without_cross_page_evidence_is_retained(self) -> None:
        source = _flat_pdf_snapshot_from_texts(
            (
                (1, 0, 100.0, "1 Test section"),
                (1, 1, 760.0, "42"),
            )
        )

        tree = build_structure_tree(source)
        section = next(node for node in tree.nodes if node.explicit_label == "1")
        number_source = next(node for node in source.nodes if node.raw_text == "42")
        number_node = _node_for_source(tree, number_source.source_node_id)

        self.assertEqual(
            number_node.parent_structure_node_id, section.structure_node_id
        )

    def test_missing_numeric_ancestor_is_not_synthesized_and_emits_finding(
        self,
    ) -> None:
        source = _flat_pdf_snapshot_from_texts(
            (
                (1, 0, 100.0, "7 验证"),
                (1, 1, 130.0, "7.2.4 省略中间层"),
            )
        )

        tree = build_structure_tree(source)

        self.assertEqual(
            [node.explicit_label for node in tree.nodes if node.explicit_label],
            ["7", "7.2.4"],
        )
        self.assertEqual(
            [finding.code for finding in tree.findings],
            ["section_ancestor_missing"],
        )

    def test_bare_decimal_table_values_do_not_break_later_outline_ancestry(
        self,
    ) -> None:
        source = _flat_pdf_snapshot_from_texts(
            (
                (1, 0, 100.0, "4 验证"),
                (1, 1, 130.0, "4.11 前一节"),
                (1, 2, 160.0, "1.2"),
                (1, 3, 180.0, "6.3"),
                (2, 0, 100.0, "4.12 后一节"),
                (2, 1, 130.0, "4.12.1设备"),
            )
        )

        tree = build_structure_tree(source)
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }

        self.assertEqual(set(sections), {"4", "4.11", "4.12", "4.12.1"})
        self.assertEqual(
            sections["4.12"].parent_structure_node_id,
            sections["4"].structure_node_id,
        )
        self.assertEqual(
            sections["4.12.1"].parent_structure_node_id,
            sections["4.12"].structure_node_id,
        )

    def test_visual_text_replaces_untrusted_native_outline_without_overwriting_source(
        self,
    ) -> None:
        native = _flat_pdf_snapshot_from_texts(
            (
                (1, 0, 100.0, "D damaged hidden text"),
                (2, 0, 100.0, "s damaged hidden text"),
            )
        )
        payload = native.model_dump(mode="python")
        payload["status"] = SourceIRStatus.PARTIAL
        payload["diagnostics"] = [
            SourceDiagnostic(
                code="pdf_full_page_image_with_invisible_text_layer",
                message="Visual reconciliation is required.",
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    bbox=(0.0, 0.0, 600.0, 800.0),
                ),
            ).model_dump(mode="python")
            for page_number in (1, 2)
        ]
        payload["visual_text_snapshots"] = [
            _visual_text_snapshot(
                source_sha256=native.source_sha256,
                page_number=1,
                page_image_sha256="1" * 64,
                lines=((100.0, "4 验证"), (130.0, "4.1 设备"), (160.0, "1) 第一项")),
            ).model_dump(mode="python"),
            _visual_text_snapshot(
                source_sha256=native.source_sha256,
                page_number=2,
                page_image_sha256="2" * 64,
                lines=((100.0, "2) 第二项"), (140.0, "4.2 下一节")),
            ).model_dump(mode="python"),
        ]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)

        visual_sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.source_refs
            and node.source_refs[0].kind
            is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
            and node.explicit_label is not None
        }
        self.assertEqual(set(visual_sections), {"4", "4.1", "4.2"})
        self.assertEqual(
            visual_sections["4.1"].parent_structure_node_id,
            visual_sections["4"].structure_node_id,
        )
        native_text_source_ids = {
            node.source_node_id
            for node in source.nodes
            if node.kind
            in {
                SourceNodeKind.HEADING,
                SourceNodeKind.PARAGRAPH,
                SourceNodeKind.LIST,
                SourceNodeKind.LIST_ITEM,
                SourceNodeKind.NOTE,
                SourceNodeKind.HEADER,
                SourceNodeKind.FOOTER,
                SourceNodeKind.WATERMARK,
                SourceNodeKind.TOC_ENTRY,
            }
        }
        self.assertTrue(native_text_source_ids)
        self.assertFalse(
            any(
                ref.kind is StructureSourceRefKind.SOURCE_NODE
                and ref.source_id in native_text_source_ids
                for node in tree.nodes
                for ref in node.source_refs
            )
        )
        self.assertTrue(native_text_source_ids.issubset(tree.source_node_ids))
        source_catalog = build_document_view(source, tree)["source_catalog"]
        self.assertTrue(
            all(
                source_catalog[("source_node", source_id)]["text"] == ""
                and source_catalog[("source_node", source_id)]["text_source"]
                == "superseded_native_audit_only"
                for source_id in native_text_source_ids
            )
        )
        self.assertEqual(
            len(tree.visual_text_observation_ids),
            sum(len(item.observations) for item in source.visual_text_snapshots),
        )
        self.assertTrue(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                for relation in tree.relations
            )
        )

    def test_visual_outline_reports_same_page_gap_and_duplicate_list_ordinal(
        self,
    ) -> None:
        native = _source_snapshot(nodes=())
        payload = native.model_dump(mode="python")
        payload["status"] = SourceIRStatus.PARTIAL
        payload["visual_text_snapshots"] = [
            _visual_text_snapshot(
                source_sha256=native.source_sha256,
                page_number=1,
                page_image_sha256="3" * 64,
                lines=(
                    (80.0, "5 章节"),
                    (110.0, "5.4 子章"),
                    (140.0, "5.4.1 组"),
                    (170.0, "5.4.1.2 条目"),
                    (200.0, "5.4.1.4 条目"),
                    (230.0, "5.4.2 中间节"),
                    (260.0, "5.4.3 列表"),
                    (290.0, "1) 第一项"),
                    (320.0, "2) 第二项"),
                    (350.0, "5) 第一个第五项"),
                    (380.0, "5) 第二个第五项"),
                ),
            ).model_dump(mode="python")
        ]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)
        sections = {
            node.explicit_label: node
            for node in tree.nodes
            if node.explicit_label is not None
        }
        list_parent = sections["5.4.3"]
        ordinals = [
            node.list_ordinal
            for node in tree.nodes
            if node.parent_structure_node_id == list_parent.structure_node_id
            and node.kind is StructureNodeKind.CLAUSE
        ]

        self.assertNotIn("5.4.1.3", sections)
        self.assertEqual(ordinals, [1, 2, 5, 5])
        self.assertEqual(
            [finding.code for finding in tree.findings],
            ["section_numbering_gap", "duplicate_list_ordinal"],
        )

    def test_visual_zero_suffix_root_heading_parents_numbered_subsections(self) -> None:
        native = _source_snapshot(nodes=())
        payload = native.model_dump(mode="python")
        payload["status"] = SourceIRStatus.PARTIAL
        payload["visual_text_snapshots"] = [
            _visual_text_snapshot(
                source_sha256=native.source_sha256,
                page_number=1,
                page_image_sha256="6" * 64,
                lines=(
                    (80.0, "5.0 产品要求"),
                    (110.0, "5.4 技术要求"),
                    (140.0, "5.4.1 基本要求"),
                    (170.0, "5.4.1.4 明示条目"),
                ),
            ).model_dump(mode="python")
        ]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)
        by_id = {node.structure_node_id: node for node in tree.nodes}
        target = next(node for node in tree.nodes if node.explicit_label == "5.4.1.4")
        ancestors: list[str] = []
        parent_id = target.parent_structure_node_id
        while parent_id is not None:
            parent = by_id[parent_id]
            if parent.explicit_label is not None:
                ancestors.append(parent.explicit_label)
            parent_id = parent.parent_structure_node_id

        self.assertEqual(ancestors[::-1], ["5.0", "5.4", "5.4.1"])
        zero_root = next(node for node in tree.nodes if node.explicit_label == "5.0")
        self.assertFalse(
            any(
                finding.code == "section_ancestor_missing"
                and finding.structure_node_id == zero_root.structure_node_id
                for finding in tree.findings
            )
        )

    def test_visual_lines_group_into_paragraphs_instead_of_line_items(self) -> None:
        native = _source_snapshot(nodes=())
        payload = native.model_dump(mode="python")
        payload["status"] = SourceIRStatus.PARTIAL
        payload["visual_text_snapshots"] = [
            _visual_text_snapshot(
                source_sha256=native.source_sha256,
                page_number=1,
                page_image_sha256="4" * 64,
                lines=(
                    (80.0, "1 章节"),
                    (110.0, "1.1 说明"),
                    (150.0, "第一段第一行"),
                    (166.0, "第一段第二行。"),
                    (200.0, "第二段。"),
                    (235.0, "第三段。"),
                ),
            ).model_dump(mode="python")
        ]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)
        section = next(node for node in tree.nodes if node.explicit_label == "1.1")
        paragraphs = [
            node
            for node in tree.nodes
            if node.parent_structure_node_id == section.structure_node_id
            and node.kind is StructureNodeKind.CONTENT
        ]

        self.assertEqual(len(paragraphs), 3)
        self.assertEqual(len(paragraphs[0].source_refs), 2)
        presentation = build_structure_presentation_index(source, structure=tree)
        first_paragraph = presentation["nodes"][paragraphs[0].structure_node_id]
        self.assertEqual(
            first_paragraph["owned_text"],
            "第一段第一行\n第一段第二行。",
        )

    def test_visual_text_authority_keeps_native_table_and_image_under_excluded_parent(
        self,
    ) -> None:
        native = _pdf_source_snapshot()
        heading = native.nodes[0]
        image_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=3,
            bbox=(80.0, 430.0, 220.0, 560.0),
        )
        asset = SourceAssetRef(
            asset_id="asset-authoritative-visual-page-image",
            kind=SourceAssetKind.IMAGE,
            content_sha256="7" * 64,
            media_type="image/png",
            storage_key="source-assets/77/visual-page-image.png",
            native_locator=image_locator,
        )
        image = SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.IMAGE, image_locator),
            kind=SourceNodeKind.IMAGE,
            source_order=3,
            raw_text="",
            native_locator=image_locator,
            parent_source_node_id=heading.source_node_id,
            asset_ref=asset.asset_id,
        )
        payload = _source_snapshot(
            nodes=(*native.nodes, image),
            assets=(asset,),
        ).model_dump(mode="python")
        payload["visual_text_snapshots"] = [
            _visual_text_snapshot(
                source_sha256=native.source_sha256,
                page_number=1,
                page_image_sha256="8" * 64,
                lines=((80.0, "8.3.1 OCR 标题"), (110.0, "OCR 正文。")),
            ).model_dump(mode="python")
        ]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)

        table = next(node for node in tree.nodes if node.kind is StructureNodeKind.TABLE)
        image_structure = _node_for_source(tree, image.source_node_id)
        root = next(node for node in tree.nodes if node.kind is StructureNodeKind.DOCUMENT)
        self.assertIn(
            table.parent_structure_node_id,
            {node.structure_node_id for node in tree.nodes},
        )
        self.assertEqual(image_structure.parent_structure_node_id, root.structure_node_id)
        self.assertTrue(
            any(node.kind is StructureNodeKind.TABLE_ROW for node in tree.nodes)
        )

    def test_empty_evaluated_visual_text_snapshot_hides_native_and_blocks(self) -> None:
        native = _pdf_source_snapshot()
        payload = native.model_dump(mode="python")
        payload["visual_text_snapshots"] = [
            _visual_text_snapshot(
                source_sha256=native.source_sha256,
                page_number=1,
                page_image_sha256="9" * 64,
                lines=(),
            ).model_dump(mode="python")
        ]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)

        for native_text in native.nodes[:2]:
            self.assertFalse(
                any(
                    ref.source_id == native_text.source_node_id
                    for node in tree.nodes
                    for ref in node.source_refs
                )
            )
        finding = next(
            item
            for item in tree.findings
            if item.code == "pdf_ocr_authority_text_unresolved"
        )
        self.assertTrue(finding.blocking)
        self.assertEqual(
            _scoped_visual_incompleteness_cause_code(
                source,
                (1,),
                structure_tree=tree,
            ),
            "pdf_ocr_authority_text_unresolved",
        )
        view = build_page_comparison_view(
            source,
            tree,
            page_bounds=((0.0, 0.0, 600.0, 800.0),),
        )
        self.assertEqual(
            view["pages"][0]["coverage"]["text_authority"]["status"],
            "EMPTY",
        )

    def test_neutral_low_native_text_hint_without_ocr_keeps_native_text(self) -> None:
        native = _pdf_source_snapshot()
        payload = native.model_dump(mode="python")
        payload["diagnostics"] = [
            SourceDiagnostic(
                code="pdf_full_page_image_with_low_native_text_coverage",
                severity=SourceDiagnosticSeverity.INFO,
                message="Neutral opt-in visual candidate.",
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    bbox=(0.0, 0.0, 600.0, 800.0),
                ),
            ).model_dump(mode="python")
        ]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)

        for native_text in native.nodes[:2]:
            self.assertEqual(
                _node_for_source(tree, native_text.source_node_id).source_refs[0].source_id,
                native_text.source_node_id,
            )

    def test_page_comparison_exposes_degraded_visual_text_authority(self) -> None:
        native = _pdf_source_snapshot()
        forced = SourceDiagnostic(
            code="pdf_visual_text_user_forced",
            severity=SourceDiagnosticSeverity.INFO,
            message="Visual text was explicitly requested.",
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                bbox=(0.0, 0.0, 600.0, 800.0),
            ),
        )
        failed = SourceDiagnostic(
            code="pdf_visual_text_provider_failed",
            severity=SourceDiagnosticSeverity.WARNING,
            message="Visual provider failed.",
            native_locator=forced.native_locator,
        )
        authentication_failed = failed.model_copy(
            update={"code": "pdf_visual_text_authentication_failed"}
        )
        rate_limited = failed.model_copy(
            update={"code": "pdf_visual_text_rate_limited"}
        )
        page_out_of_scope = failed.model_copy(
            update={"code": "pdf_visual_text_page_out_of_scope"}
        )
        provider_output_invalid = failed.model_copy(
            update={"code": "pdf_visual_text_provider_output_invalid"}
        )
        capture_hash_mismatch = failed.model_copy(
            update={"code": "pdf_visual_text_capture_page_hash_mismatch"}
        )
        empty_visual = _visual_text_snapshot(
            source_sha256=native.source_sha256,
            page_number=1,
            page_image_sha256="a" * 64,
            lines=(),
        )
        ready_visual = _visual_text_snapshot(
            source_sha256=native.source_sha256,
            page_number=1,
            page_image_sha256="c" * 64,
            lines=((120.0, "OCR adopted body."),),
        )
        for expected_status, diagnostics, visuals, degraded in (
            ("PENDING", (forced,), (), [1]),
            ("FAILED", (forced, failed), (), [1]),
            ("FAILED", (forced, authentication_failed), (), [1]),
            ("FAILED", (forced, rate_limited), (), [1]),
            ("FAILED", (forced, page_out_of_scope), (), [1]),
            ("FAILED", (forced, provider_output_invalid), (), [1]),
            ("FAILED", (forced, capture_hash_mismatch), (), [1]),
            ("EMPTY", (forced,), (empty_visual,), [1]),
            ("READY", (forced,), (ready_visual,), []),
        ):
            with self.subTest(expected_status=expected_status):
                payload = native.model_dump(mode="python")
                payload["status"] = SourceIRStatus.PARTIAL
                payload["diagnostics"] = [
                    diagnostic.model_dump(mode="python")
                    for diagnostic in diagnostics
                ]
                payload["visual_text_snapshots"] = [
                    visual.model_dump(mode="python") for visual in visuals
                ]
                source = SourceIRSnapshot.model_validate(payload)
                tree = build_structure_tree(source)

                view = build_page_comparison_view(
                    source,
                    tree,
                    page_bounds=((0.0, 0.0, 600.0, 800.0),),
                )

                self.assertEqual(
                    view["pages"][0]["coverage"]["text_authority"],
                    {
                        "channel": "REMOTE_VISUAL",
                        "status": expected_status,
                        "reason_codes": sorted(
                            diagnostic.code for diagnostic in diagnostics
                        ),
                    },
                )
                self.assertEqual(
                    view["coverage"]["text_authority"]["degraded_page_numbers"],
                    degraded,
                )
                if expected_status == "FAILED":
                    self.assertEqual(
                        _scoped_visual_incompleteness_cause_code(
                            source,
                            (1,),
                            structure_tree=tree,
                        ),
                        diagnostics[-1].code,
                    )

    def test_visual_page_table_without_snapshot_blocks_and_does_not_guess_cells(
        self,
    ) -> None:
        native, first_source_table, _, _ = _continued_pdf_table_source()
        visual = _visual_text_snapshot_with_boxes(
            source_sha256=native.source_sha256,
            page_number=1,
            page_image_sha256="7" * 64,
            lines=(((70.0, 620.0, 280.0, 636.0), "视觉字段"),),
        )
        payload = native.model_dump(mode="python")
        payload["status"] = SourceIRStatus.PARTIAL
        payload["visual_text_snapshots"] = [visual.model_dump(mode="python")]
        payload["visual_table_page_numbers"] = [1]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)

        self.assertEqual(tree.visual_table_cell_text_bindings, ())
        table_structure = _node_for_source(
            tree,
            first_source_table.source_node_id,
        )
        self.assertEqual(table_structure.semantic_source_refs, ())
        authority_rows = [
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.parent_structure_node_id == table_structure.structure_node_id
        ]
        self.assertTrue(authority_rows)
        self.assertTrue(
            all(row.semantic_source_refs == () for row in authority_rows)
        )
        finding = next(
            item
            for item in tree.findings
            if item.code == "pdf_visual_table_snapshot_missing"
            and first_source_table.source_node_id
            in {ref.source_id for ref in item.source_refs}
        )
        self.assertTrue(finding.blocking)
        authority_finding = next(
            item
            for item in tree.findings
            if item.code == "pdf_ocr_authority_table_text_unresolved"
            and first_source_table.source_node_id
            in {ref.source_id for ref in item.source_refs}
        )
        self.assertTrue(authority_finding.blocking)
        self.assertEqual(
            _scoped_visual_incompleteness_cause_code(
                source,
                (1,),
                structure_tree=tree,
            ),
            "pdf_ocr_authority_table_text_unresolved",
        )
        view = build_document_view(source, tree)
        self.assertEqual(
            view["source_catalog"][("source_node", first_source_table.source_node_id)][
                "text"
            ],
            "",
        )
        table = next(
            item
            for item in view["tables"]
            if item["id"] == first_source_table.table_payload.table_id
        )
        self.assertTrue(
            all(
                cell["text_source"] == "unresolved_ocr_authority"
                and cell["text"] == ""
                and cell["raw_text"] == ""
                and cell["display_value"] is None
                and cell["formula"] is None
                for cell in table["cells"]
            )
        )

    def test_exact_visual_table_snapshot_owns_all_public_cell_text_fields(
        self,
    ) -> None:
        native, first_source_table, _, _ = _continued_pdf_table_source()
        visual_text = _visual_text_snapshot_with_boxes(
            source_sha256=native.source_sha256,
            page_number=1,
            page_image_sha256="8" * 64,
            lines=(((70.0, 620.0, 280.0, 636.0), "页级重复文字"),),
        )
        visual_table = _visual_table_snapshot_for_native_table(
            source=native,
            table_node=first_source_table,
            texts=("视觉字段", "", "视觉第一项", "视觉值一"),
        )
        payload = native.model_dump(mode="python")
        payload["status"] = SourceIRStatus.PARTIAL
        payload["visual_text_snapshots"] = [visual_text.model_dump(mode="python")]
        payload["visual_table_page_numbers"] = [1]
        payload["visual_table_snapshots"] = [visual_table.model_dump(mode="python")]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)

        self.assertEqual(len(tree.visual_table_cell_text_bindings), 4)
        table_structure = _node_for_source(
            tree,
            first_source_table.source_node_id,
        )
        self.assertEqual(table_structure.semantic_source_refs, ())
        authority_rows = [
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.parent_structure_node_id == table_structure.structure_node_id
        ]
        self.assertTrue(authority_rows)
        self.assertTrue(
            all(row.semantic_source_refs == () for row in authority_rows)
        )
        self.assertFalse(
            any(
                finding.code.startswith("pdf_visual_table_")
                for finding in tree.findings
            )
        )
        view = build_document_view(source, tree)
        self.assertEqual(
            view["source_catalog"][("source_node", first_source_table.source_node_id)][
                "text"
            ],
            "",
        )
        table = next(
            item
            for item in view["tables"]
            if item["id"] == first_source_table.table_payload.table_id
        )
        first_cell = next(
            cell for cell in table["cells"] if cell["row"] == 0 and cell["column"] == 0
        )
        blank_cell = next(
            cell for cell in table["cells"] if cell["row"] == 0 and cell["column"] == 1
        )
        self.assertEqual(first_cell["text"], "视觉字段")
        self.assertEqual(first_cell["raw_text"], "视觉字段")
        self.assertIsNone(first_cell["display_value"])
        self.assertIsNone(first_cell["formula"])
        self.assertEqual(first_cell["text_source"], "visual_table_cell_fact")
        self.assertEqual(
            first_cell["visual_table_binding"]["provider"]["version"],
            visual_table.provider_version,
        )
        self.assertEqual(blank_cell["text"], "")
        self.assertEqual(blank_cell["raw_text"], "")
        self.assertIsNone(blank_cell["display_value"])
        self.assertIsNone(blank_cell["formula"])
        self.assertEqual(blank_cell["text_source"], "visual_table_cell_fact")
        native_cell_texts = {
            cell.raw_text
            for cell in first_source_table.table_payload.cells
            if cell.raw_text
        }
        exposed_values = {
            value
            for cell in table["cells"]
            for value in (
                cell["text"],
                cell["raw_text"],
                cell["display_value"],
                cell["formula"],
            )
            if isinstance(value, str) and value
        }
        self.assertTrue(native_cell_texts.isdisjoint(exposed_values))

        presentation = build_structure_presentation_index(source, structure=tree)
        source_index, _ = _source_index(source, structure=tree)
        row_owned_text = {
            row.structure_node_id: presentation["nodes"][row.structure_node_id][
                "owned_text"
            ]
            for row in authority_rows
        }
        self.assertEqual(
            set(row_owned_text.values()),
            {"视觉字段", "视觉第一项\n视觉值一"},
        )
        self.assertTrue(native_cell_texts.isdisjoint(row_owned_text.values()))
        for row in authority_rows:
            serialized = row.model_dump(mode="json")
            warm_label = _node_label(
                serialized,
                document={"file_name": "source.pdf"},
                source_index=source_index,
                presentation=None,
                durable_presentation=presentation["nodes"][row.structure_node_id],
            )
            cold_label = _node_label(
                serialized,
                document={"file_name": "source.pdf"},
                source_index=source_index,
                presentation=None,
                durable_presentation=None,
            )
            self.assertNotIn(warm_label, native_cell_texts)
            self.assertNotIn(cold_label, native_cell_texts)

    def test_visual_table_topology_mismatch_blocks_without_partial_mapping(
        self,
    ) -> None:
        native, first_source_table, _, _ = _continued_pdf_table_source()
        visual_text = _visual_text_snapshot(
            source_sha256=native.source_sha256,
            page_number=1,
            page_image_sha256="9" * 64,
            lines=((620.0, "页级文字"),),
        )
        mismatched = _visual_table_snapshot_for_native_table(
            source=native,
            table_node=first_source_table,
            texts=("一", "二", "三", "四"),
            dimensions=(1, 4),
        )
        payload = native.model_dump(mode="python")
        payload["status"] = SourceIRStatus.PARTIAL
        payload["visual_text_snapshots"] = [visual_text.model_dump(mode="python")]
        payload["visual_table_page_numbers"] = [1]
        payload["visual_table_snapshots"] = [mismatched.model_dump(mode="python")]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)

        self.assertEqual(tree.visual_table_cell_text_bindings, ())
        finding = next(
            item
            for item in tree.findings
            if item.code == "pdf_visual_table_topology_mismatch"
        )
        self.assertTrue(finding.blocking)
        view = build_document_view(source, tree)
        table = next(
            item
            for item in view["tables"]
            if item["id"] == first_source_table.table_payload.table_id
        )
        self.assertTrue(
            all(
                cell["text_source"] == "unresolved_ocr_authority"
                and cell["text"] == ""
                and cell["raw_text"] == ""
                and cell["display_value"] is None
                and cell["formula"] is None
                for cell in table["cells"]
            )
        )

    def test_exact_merged_visual_topology_maps_only_the_native_anchor(self) -> None:
        native, table_node, target_cell = _merged_pdf_table_source()
        visual_text = _visual_text_snapshot(
            source_sha256=native.source_sha256,
            page_number=1,
            page_image_sha256="d" * 64,
            lines=((80.0, "OCR page body"),),
        )
        visual_table = _visual_table_snapshot_for_native_table(
            source=native,
            table_node=table_node,
            texts=("视觉合并文字",),
        )
        payload = native.model_dump(mode="python")
        payload["status"] = SourceIRStatus.PARTIAL
        payload["visual_text_snapshots"] = [visual_text.model_dump(mode="python")]
        payload["visual_table_page_numbers"] = [1]
        payload["visual_table_snapshots"] = [visual_table.model_dump(mode="python")]
        source = SourceIRSnapshot.model_validate(payload)

        tree = build_structure_tree(source)

        self.assertEqual(len(tree.visual_table_cell_text_bindings), 1)
        binding = tree.visual_table_cell_text_bindings[0]
        self.assertEqual(binding.table_cell_id, target_cell.cell_id)
        self.assertEqual(
            binding.visual_table_cell_id,
            visual_table.cells[0].visual_table_cell_id,
        )
        authority_table = _node_for_source(tree, table_node.source_node_id)
        self.assertEqual(authority_table.semantic_source_refs, ())
        self.assertTrue(
            all(
                node.semantic_source_refs == ()
                for node in tree.nodes
                if node.parent_structure_node_id
                == authority_table.structure_node_id
                and node.boundary_basis
                in {
                    StructureBoundaryBasis.TABLE_ROW,
                    StructureBoundaryBasis.MERGED_RANGE,
                }
            )
        )


class StructureTreeContractTests(unittest.TestCase):
    def test_contract_is_strict_frozen_and_nested_collections_are_immutable(
        self,
    ) -> None:
        tree = build_structure_tree(_pdf_source_snapshot())
        row = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.TABLE_ROW
        )

        with self.assertRaises(ValidationError):
            tree.resolver_version = "changed"  # type: ignore[misc]
        with self.assertRaises(ValidationError):
            row.source_order = 999  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            tree.nodes.append(row)  # type: ignore[attr-defined]
        unknown_field = row.model_dump(mode="python")
        unknown_field["raw_text"] = "不得复制到 StructureTree"
        with self.assertRaisesRegex(ValidationError, "raw_text"):
            StructureNode.model_validate(unknown_field)

    def test_dangling_parent_relation_and_source_refs_fail_closed(self) -> None:
        tree = build_structure_tree(_pdf_source_snapshot())

        dangling_parent = tree.model_dump(mode="python")
        dangling_parent["nodes"][1]["parent_structure_node_id"] = "missing-parent"
        with self.assertRaisesRegex(ValidationError, "parent_structure_node_id"):
            StructureTreeSnapshot.model_validate(dangling_parent)

        dangling_relation = tree.model_dump(mode="python")
        dangling_relation["relations"][0]["target_structure_node_id"] = "missing-target"
        with self.assertRaisesRegex(ValidationError, "relation target"):
            StructureTreeSnapshot.model_validate(dangling_relation)

        dangling_source = tree.model_dump(mode="python")
        row_index = next(
            index
            for index, node in enumerate(tree.nodes)
            if node.kind is StructureNodeKind.TABLE_ROW
        )
        dangling_source["nodes"][row_index]["source_refs"] = [
            {
                "kind": StructureSourceRefKind.TABLE_CELL,
                "source_id": f"cell-{'e' * 32}",
            }
        ]
        with self.assertRaisesRegex(ValidationError, "table cell reference"):
            StructureTreeSnapshot.model_validate(dangling_source)

    def test_parent_cycle_and_duplicate_source_order_are_rejected(self) -> None:
        tree = build_structure_tree(_pdf_source_snapshot())
        content = next(
            node
            for node in tree.nodes
            if node.boundary_basis is StructureBoundaryBasis.NATIVE_BLOCK
        )
        table = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.TABLE
        )
        content_index = tree.nodes.index(content)
        table_index = tree.nodes.index(table)
        heading_index = next(
            index
            for index, node in enumerate(tree.nodes)
            if node.structure_node_id == content.parent_structure_node_id
        )

        cyclic = tree.model_dump(mode="python")
        cyclic["nodes"][content_index]["parent_structure_node_id"] = (
            table.structure_node_id
        )
        cyclic["nodes"][table_index]["parent_structure_node_id"] = (
            content.structure_node_id
        )
        with self.assertRaisesRegex(ValidationError, "acyclic"):
            StructureTreeSnapshot.model_validate(cyclic)

        duplicate_order = tree.model_dump(mode="python")
        duplicate_order["nodes"][2]["source_order"] = duplicate_order["nodes"][1][
            "source_order"
        ]
        with self.assertRaisesRegex(ValidationError, "source_order"):
            StructureTreeSnapshot.model_validate(duplicate_order)

        child_before_parent = tree.model_dump(mode="python")
        child_before_parent["nodes"] = list(child_before_parent["nodes"])
        (
            child_before_parent["nodes"][content_index],
            child_before_parent["nodes"][heading_index],
        ) = (
            child_before_parent["nodes"][heading_index],
            child_before_parent["nodes"][content_index],
        )
        for source_order, node in enumerate(child_before_parent["nodes"]):
            node["source_order"] = source_order
        with self.assertRaisesRegex(
            ValidationError, "parent structure node must precede"
        ):
            StructureTreeSnapshot.model_validate(child_before_parent)

    def test_single_parent_tree_requires_exactly_one_matching_contains_relation(
        self,
    ) -> None:
        tree = build_structure_tree(_pdf_source_snapshot())
        payload = tree.model_dump(mode="python")
        payload["relations"] = [
            relation
            for relation in payload["relations"]
            if relation["target_structure_node_id"] != tree.nodes[1].structure_node_id
        ]

        with self.assertRaisesRegex(ValidationError, "exactly one contains"):
            StructureTreeSnapshot.model_validate(payload)

    def test_finding_is_source_grounded_stable_and_strict(self) -> None:
        tree = build_structure_tree(_pdf_source_snapshot())
        target = tree.nodes[1]
        source_ref = target.source_refs[0]
        finding = StructureFinding(
            finding_id=stable_structure_finding_id(
                source_ir_id=tree.source_ir_id,
                resolver_id=tree.resolver_id,
                resolver_version=tree.resolver_version,
                code="structure_number_gap",
                identity_key="gap:5.4.1.2:5.4.1.4",
            ),
            identity_key="gap:5.4.1.2:5.4.1.4",
            code="structure_number_gap",
            severity=StructureFindingSeverity.WARNING,
            blocking=False,
            message_key="structure.number_gap",
            structure_node_id=target.structure_node_id,
            source_refs=(source_ref,),
        )
        payload = tree.model_dump(mode="python")
        payload["findings"] = [finding.model_dump(mode="python")]

        with_finding = StructureTreeSnapshot.model_validate(payload)

        self.assertEqual(with_finding.findings, (finding,))
        invalid = finding.model_dump(mode="python")
        invalid["message"] = "free-form copied text is not part of the contract"
        with self.assertRaisesRegex(ValidationError, "message"):
            StructureFinding.model_validate(invalid)

    def test_all_first_stage_relation_kinds_are_in_the_contract(self) -> None:
        self.assertEqual(
            {kind.value for kind in StructureRelationKind},
            {
                "contains",
                "continues",
                "applies_to",
                "inherits",
                "references",
                "translation_of",
                "option_state",
                "placed_in_cell",
                "placed_over_cells",
            },
        )

    def test_option_state_is_required_and_reserved_for_option_relations(self) -> None:
        tree = build_structure_tree(
            _flat_pdf_snapshot_from_texts(
                (
                    (1, 0, 100.0, "4.1\n法规与认证"),
                    (1, 1, 130.0, "[ √ ] 区域甲"),
                    (1, 2, 150.0, "[   ] 区域乙"),
                )
            )
        )
        option_index = next(
            index
            for index, relation in enumerate(tree.relations)
            if relation.kind is StructureRelationKind.OPTION_STATE
        )

        missing_state = tree.model_dump(mode="python")
        missing_state["relations"][option_index]["option_state"] = None
        with self.assertRaisesRegex(ValidationError, "explicit state"):
            StructureTreeSnapshot.model_validate(missing_state)

        non_option_index = next(
            index
            for index, relation in enumerate(tree.relations)
            if relation.kind is StructureRelationKind.CONTAINS
        )
        misplaced_state = tree.model_dump(mode="python")
        misplaced_state["relations"][non_option_index]["option_state"] = (
            StructureOptionState.CHECKED
        )
        with self.assertRaisesRegex(ValidationError, "only for option_state"):
            StructureTreeSnapshot.model_validate(misplaced_state)

    def test_identity_key_cannot_be_replaced_with_customer_content(self) -> None:
        tree = build_structure_tree(_pdf_source_snapshot())
        row = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.TABLE_ROW
        )
        payload = row.model_dump(mode="python")
        payload["identity_key"] = "customer private acceptance requirement"
        payload["structure_node_id"] = stable_structure_node_id(
            source_ir_id=tree.source_ir_id,
            resolver_id=tree.resolver_id,
            resolver_version=tree.resolver_version,
            kind=row.kind,
            identity_key=payload["identity_key"],
        )

        with self.assertRaisesRegex(ValidationError, "non-content structural identity"):
            StructureNode.model_validate(payload)

    def test_self_reported_source_catalog_must_match_actual_source_ir(self) -> None:
        source = _pdf_source_snapshot()
        tree = build_structure_tree(source)
        payload = tree.model_dump(mode="python")
        payload["source_cell_ids"] = sorted(
            [*payload["source_cell_ids"], f"cell-{'f' * 32}"]
        )
        fabricated = StructureTreeSnapshot.model_validate(payload)

        with self.assertRaises(StructureResolutionRejected) as caught:
            validate_structure_tree_against_source_ir(fabricated, source)

        self.assertEqual(
            caught.exception.code,
            StructureResolutionRejectionCode.SOURCE_CATALOG_MISMATCH,
        )

    def test_cross_validation_rejects_row_and_source_grounding_tamper(self) -> None:
        source = _pdf_source_snapshot()
        tree = build_structure_tree(source)
        table_structure = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.TABLE
        )
        rows = _table_rows(tree, table_structure.structure_node_id)

        wrong_row_index = tree.model_dump(mode="python")
        first_row_index = tree.nodes.index(rows[0])
        wrong_row_index["nodes"][first_row_index]["table_row_index"] = 1
        self._assert_cross_validation_rejected(wrong_row_index, source)

        out_of_range = tree.model_dump(mode="python")
        out_of_range["nodes"][first_row_index]["table_row_index"] = 999
        out_of_range["nodes"][first_row_index]["source_refs"] = [
            {
                "kind": StructureSourceRefKind.SOURCE_NODE,
                "source_id": table_structure.source_refs[0].source_id,
            }
        ]
        self._assert_cross_validation_rejected(out_of_range, source)

        missing_row = tree.model_dump(mode="python")
        removed_row_id = rows[-1].structure_node_id
        missing_row["nodes"] = [
            node
            for node in missing_row["nodes"]
            if node["structure_node_id"] != removed_row_id
        ]
        missing_row["relations"] = [
            relation
            for relation in missing_row["relations"]
            if relation["source_structure_node_id"] != removed_row_id
            and relation["target_structure_node_id"] != removed_row_id
        ]
        self._assert_cross_validation_rejected(missing_row, source)

        source_ref_swap = tree.model_dump(mode="python")
        content = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.boundary_basis is StructureBoundaryBasis.NATIVE_BLOCK
        )
        heading = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.SECTION
        )
        content_index = tree.nodes.index(content)
        source_ref_swap["nodes"][content_index]["source_refs"] = [
            heading.source_refs[0].model_dump(mode="python")
        ]
        self._assert_cross_validation_rejected(source_ref_swap, source)

    def test_cross_validation_rejects_reordered_source_siblings(self) -> None:
        source_payload = _pdf_source_snapshot().model_dump(mode="python")
        source_payload["nodes"][1]["parent_source_node_id"] = None
        source = SourceIRSnapshot.model_validate(source_payload)
        tree = build_structure_tree(source)
        heading_index = next(
            index
            for index, node in enumerate(tree.nodes)
            if node.kind is StructureNodeKind.SECTION
        )
        content_index = next(
            index
            for index, node in enumerate(tree.nodes)
            if node.kind is StructureNodeKind.CONTENT
            and node.boundary_basis is StructureBoundaryBasis.NATIVE_BLOCK
        )

        reordered = tree.model_dump(mode="python")
        reordered["nodes"] = list(reordered["nodes"])
        reordered["nodes"][heading_index], reordered["nodes"][content_index] = (
            reordered["nodes"][content_index],
            reordered["nodes"][heading_index],
        )
        for source_order, node in enumerate(reordered["nodes"]):
            node["source_order"] = source_order

        self._assert_cross_validation_rejected(reordered, source)

    def _assert_cross_validation_rejected(
        self,
        payload: dict,
        source: SourceIRSnapshot,
    ) -> None:
        try:
            tampered = StructureTreeSnapshot.model_validate(payload)
        except ValidationError:
            # Contract-level rejection is already fail closed; only payloads
            # that remain internally well-formed reach SourceIR cross-checking.
            return
        with self.assertRaises(StructureResolutionRejected):
            validate_structure_tree_against_source_ir(tampered, source)


def _visual_text_snapshot(
    *,
    source_sha256: str,
    page_number: int,
    page_image_sha256: str,
    lines: tuple[tuple[float, str], ...],
) -> VisualTextSnapshot:
    provider_id = "fixture-visual-text"
    provider_version = "1.0.0"
    page_bbox = (0.0, 0.0, 600.0, 800.0)
    observations: list[VisualTextObservation] = []
    for index, (y0, raw_text) in enumerate(lines):
        bbox = (80.0, y0, 500.0, y0 + 14.0)
        observations.append(
            VisualTextObservation(
                observation_id=stable_visual_text_observation_id(
                    source_sha256=source_sha256,
                    page_image_sha256=page_image_sha256,
                    provider_id=provider_id,
                    provider_version=provider_version,
                    page_number=page_number,
                    observation_index=index,
                    bbox=bbox,
                ),
                observation_index=index,
                source_sha256=source_sha256,
                page_number=page_number,
                bbox=bbox,
                raw_text=raw_text,
                confidence=0.95,
                provider_id=provider_id,
                provider_version=provider_version,
                page_image_sha256=page_image_sha256,
            )
        )
    return VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            page_bbox=page_bbox,
            page_image_width_px=1200,
            page_image_height_px=1600,
        ),
        source_sha256=source_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_number,
        page_bbox=page_bbox,
        page_image_sha256=page_image_sha256,
        page_image_width_px=1200,
        page_image_height_px=1600,
        observations=tuple(observations),
    )


def _visual_text_snapshot_with_boxes(
    *,
    source_sha256: str,
    page_number: int,
    page_image_sha256: str,
    lines: tuple[tuple[tuple[float, float, float, float], str], ...],
) -> VisualTextSnapshot:
    provider_id = "fixture-visual-text"
    provider_version = "1.0.0"
    page_bbox = (0.0, 0.0, 600.0, 800.0)
    observations = tuple(
        VisualTextObservation(
            observation_id=stable_visual_text_observation_id(
                source_sha256=source_sha256,
                page_image_sha256=page_image_sha256,
                provider_id=provider_id,
                provider_version=provider_version,
                page_number=page_number,
                observation_index=index,
                bbox=bbox,
            ),
            observation_index=index,
            source_sha256=source_sha256,
            page_number=page_number,
            bbox=bbox,
            raw_text=raw_text,
            confidence=0.95,
            provider_id=provider_id,
            provider_version=provider_version,
            page_image_sha256=page_image_sha256,
        )
        for index, (bbox, raw_text) in enumerate(lines)
    )
    return VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            page_bbox=page_bbox,
            page_image_width_px=1200,
            page_image_height_px=1600,
        ),
        source_sha256=source_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_number,
        page_bbox=page_bbox,
        page_image_sha256=page_image_sha256,
        page_image_width_px=1200,
        page_image_height_px=1600,
        observations=observations,
    )


def _visual_table_snapshot_for_native_table(
    *,
    source: SourceIRSnapshot,
    table_node: SourceNode,
    texts: tuple[str, ...],
    dimensions: tuple[int, int] | None = None,
) -> VisualTableSnapshot:
    table = table_node.table_payload
    assert table is not None
    assert table_node.native_locator.page_number is not None
    assert table_node.native_locator.table_index is not None
    assert table_node.native_locator.bbox is not None
    provider_id = "fixture-visual-table"
    provider_version = (
        "1.0.0:model=fixture:task=table_parsing:seed=1234:"
        "temperature=0.01:rotate=false:parser=strict-grid/1"
    )
    crop_hash = "d" * 64
    bbox = table_node.native_locator.bbox
    width = max(1, int(round((bbox[2] - bbox[0]) * 2)))
    height = max(1, int(round((bbox[3] - bbox[1]) * 2)))
    snapshot_id = stable_visual_table_snapshot_id(
        source_sha256=source.source_sha256,
        crop_image_sha256=crop_hash,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=table_node.native_locator.page_number,
        table_source_node_id=table_node.source_node_id,
        table_id=table.table_id,
        table_index=table_node.native_locator.table_index,
        table_bbox=bbox,
        crop_image_width_px=width,
        crop_image_height_px=height,
    )
    if dimensions is None:
        row_count = table.row_count
        column_count = table.column_count
        topology = tuple(
            (cell.row, cell.column, cell.row_span, cell.column_span)
            for cell in sorted(
                table.cells,
                key=lambda item: (item.row, item.column),
            )
        )
    else:
        row_count, column_count = dimensions
        topology = tuple(
            (row, column, 1, 1)
            for row in range(row_count)
            for column in range(column_count)
        )
    assert len(texts) == len(topology)
    cells = tuple(
        VisualTableCell(
            visual_table_cell_id=stable_visual_table_cell_id(
                visual_table_snapshot_id=snapshot_id,
                row=row,
                column=column,
                row_span=row_span,
                column_span=column_span,
            ),
            row=row,
            column=column,
            row_span=row_span,
            column_span=column_span,
            raw_text=text,
        )
        for (row, column, row_span, column_span), text in zip(topology, texts)
    )
    return VisualTableSnapshot(
        visual_table_snapshot_id=snapshot_id,
        source_sha256=source.source_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=table_node.native_locator.page_number,
        page_bbox=(0.0, 0.0, 600.0, 800.0),
        table_source_node_id=table_node.source_node_id,
        table_id=table.table_id,
        table_index=table_node.native_locator.table_index,
        table_bbox=bbox,
        crop_image_sha256=crop_hash,
        crop_image_width_px=width,
        crop_image_height_px=height,
        row_count=row_count,
        column_count=column_count,
        cells=cells,
    )


def _flat_pdf_outline_snapshot() -> SourceIRSnapshot:
    return _flat_pdf_snapshot_from_texts(
        (
            (1, 0, 20.0, "STANDARD HEADER"),
            (1, 1, 100.0, "4\n验证总章"),
            (1, 2, 130.0, "4. 12\n连接测试"),
            (1, 3, 160.0, "4.12.2\n执行方法"),
            (1, 4, 190.0, "4.12.2.1\n主流程"),
            (1, 5, 220.0, "1) 准备样本"),
            (1, 6, 240.0, "2）执行动作"),
            (1, 7, 260.0, "3) 记录数据"),
            (1, 8, 280.0, "4) 检查状态"),
            (1, 9, 300.0, "50 mm/min 匀速运行"),
            (2, 0, 20.0, "STANDARD HEADER"),
            (2, 1, 80.0, "5) 保持载荷"),
            (2, 2, 100.0, "6) 完成记录"),
            (2, 3, 130.0, "4.12.2.2\n后续流程"),
            (2, 4, 170.0, "4.13\n另一类测试"),
            (3, 0, 20.0, "STANDARD HEADER"),
            (3, 1, 100.0, "4.13.3\n验收要求"),
            (3, 2, 130.0, "内容如下"),
            (3, 3, 160.0, "1) 第一项"),
            (3, 4, 180.0, "2) 第二项"),
            (3, 5, 200.0, "3) 第三项"),
            (3, 6, 220.0, "第三项续行"),
            (3, 7, 240.0, "4) 第四项"),
            (3, 8, 260.0, "5) 第五项"),
            (3, 9, 300.0, "4.14\n下一章节"),
        )
    )


def _flat_pdf_snapshot_from_texts(
    specs: tuple[tuple[int, int, float, str], ...],
) -> SourceIRSnapshot:
    nodes: list[SourceNode] = []
    for source_order, (page_number, block_index, y0, raw_text) in enumerate(specs):
        kind = (
            SourceNodeKind.HEADER
            if raw_text == "STANDARD HEADER"
            else SourceNodeKind.PARAGRAPH
        )
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            block_index=block_index,
            bbox=(80.0, y0, 500.0, y0 + 14.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=_source_node_id(kind, locator),
                kind=kind,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
        )
    return _source_snapshot(nodes=tuple(nodes))


def _fidelity_layout_source_snapshot() -> SourceIRSnapshot:
    specs = (
        (1, 0, 80.0, 90.0, 500.0, "封面"),
        (1, 1, 293.0, 772.0, 303.0, "21"),
        (2, 0, 259.0, 76.0, 336.0, "目\n录"),
        (2, 1, 90.0, 110.0, 230.0, "一、项目总体描述"),
        (2, 2, 101.0, 140.0, 230.0, "1.1 项目时间节点"),
        (2, 3, 101.0, 162.0, 230.0, "1.2 产品信息"),
        (2, 4, 90.0, 190.0, 230.0, "六、测试要求"),
        (2, 5, 293.0, 772.0, 303.0, "22"),
        (3, 0, 101.0, 110.0, 260.0, "6.1 样车/样机的测试"),
        (3, 1, 90.0, 140.0, 230.0, "七、其它"),
        (3, 2, 293.0, 772.0, 303.0, "23"),
        (4, 0, 90.0, 110.0, 230.0, "一、项目总体描述"),
        (4, 1, 90.0, 145.0, 230.0, "1.1 项目时间节点"),
        (4, 2, 110.0, 175.0, 500.0, "项目节点按计划执行。"),
        (4, 3, 90.0, 210.0, 230.0, "1.2 产品信息"),
        (4, 4, 110.0, 240.0, 500.0, "产品信息保持完整。"),
        (4, 5, 293.0, 772.0, 303.0, "24"),
        (5, 0, 90.0, 110.0, 230.0, "六、测试要求"),
        (5, 1, 90.0, 145.0, 260.0, "6.1 样车/样机的测试"),
        (5, 2, 110.0, 175.0, 500.0, "测试记录应完整保存。"),
        (5, 3, 90.0, 220.0, 230.0, "七、其它"),
        (5, 4, 110.0, 250.0, 500.0, "其它说明。"),
        (5, 5, 293.0, 772.0, 303.0, "25"),
    )
    nodes: list[SourceNode] = []
    for source_order, (page_number, block_index, x0, y0, x1, raw_text) in enumerate(
        specs
    ):
        kind = (
            SourceNodeKind.FOOTER
            if raw_text in {"21", "22", "23", "24", "25"}
            else SourceNodeKind.PARAGRAPH
        )
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            block_index=block_index,
            bbox=(x0, y0, x1, y0 + 14.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=_source_node_id(kind, locator),
                kind=kind,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
        )
    return _source_snapshot(nodes=tuple(nodes))


def _pdf_source_snapshot() -> SourceIRSnapshot:
    heading_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
    )
    paragraph_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=1,
    )
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
        bbox=(10.0, 100.0, 500.0, 400.0),
    )
    heading_id = _source_node_id(SourceNodeKind.HEADING, heading_locator)
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    cell_specs = (
        (0, 0, 1, "场景 A"),
        (0, 1, 3, "三行共享验收条件"),
        (0, 2, 1, "结果 A"),
        (1, 0, 1, "场景 B"),
        (1, 2, 1, "结果 B"),
        (2, 0, 1, "场景 C"),
        (2, 2, 1, "结果 C"),
    )
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row, column),
            row=row,
            column=column,
            row_span=row_span,
            raw_text=raw_text,
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                table_index=0,
                row_index=row,
                column_index=column,
            ),
        )
        for row, column, row_span, raw_text in cell_specs
    )
    table = TableIR(
        table_id=table_id,
        row_count=3,
        column_count=3,
        cells=cells,
        merged_regions=(MergedRegion(row=0, column=1, row_span=3),),
    )
    nodes = (
        SourceNode(
            source_node_id=heading_id,
            kind=SourceNodeKind.HEADING,
            source_order=0,
            raw_text="8.3.1 表 55 测试场景",
            native_locator=heading_locator,
        ),
        SourceNode(
            source_node_id=_source_node_id(
                SourceNodeKind.PARAGRAPH,
                paragraph_locator,
            ),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=1,
            raw_text="必须完成第一步。\n\n第二个自然段仍属同一块。",
            native_locator=paragraph_locator,
            parent_source_node_id=heading_id,
        ),
        SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.TABLE, table_locator),
            kind=SourceNodeKind.TABLE,
            source_order=2,
            raw_text="",
            native_locator=table_locator,
            parent_source_node_id=heading_id,
            table_payload=table,
        ),
    )
    return _source_snapshot(nodes=nodes)


def _note_and_figure_source(
    *,
    first_table_caption: str = "Table 5 Synthetic tolerance table",
    second_table_caption: str = "Table 5 Synthetic second table",
    figure_body_references: tuple[str, ...] = (
        "See Figure 2 for the calculation boundary.",
    ),
    figure_caption: str = "Figure 2 Synthetic calculation diagram",
) -> tuple[SourceIRSnapshot, SourceNode, SourceNode, tuple[SourceNode, SourceNode]]:
    nodes: list[SourceNode] = []

    def paragraph(raw_text: str, *, y0: float) -> SourceNode:
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=len(nodes),
            bbox=(70.0, y0, 530.0, y0 + 16.0),
        )
        node = SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.PARAGRAPH, locator),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=len(nodes),
            raw_text=raw_text,
            native_locator=locator,
        )
        nodes.append(node)
        return node

    paragraph("9.16 Synthetic requirements", y0=60.0)
    paragraph("9.16.2 Performance requirements", y0=90.0)
    first_caption = paragraph(first_table_caption, y0=120.0)
    table_bbox = (70.0, 145.0, 530.0, 265.0)
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
        bbox=table_bbox,
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    values = (
        ("Metric", "Low", "High"),
        ("Start", "±10%", "±8%"),
        ("Stop", "±10%", "±8%"),
        ("Note: use the shared tolerance.", "", ""),
    )
    column_width = (table_bbox[2] - table_bbox[0]) / 3.0
    row_height = (table_bbox[3] - table_bbox[1]) / 4.0
    cells: list[TableCellIR] = []
    for row in range(4):
        for column in range(3):
            bbox = (
                (
                    table_bbox[0],
                    table_bbox[1] + row * row_height,
                    table_bbox[2],
                    table_bbox[1] + (row + 1) * row_height,
                )
                if row == 3 and column == 0
                else None
                if row == 3
                else (
                    table_bbox[0] + column * column_width,
                    table_bbox[1] + row * row_height,
                    table_bbox[0] + (column + 1) * column_width,
                    table_bbox[1] + (row + 1) * row_height,
                )
            )
            cells.append(
                TableCellIR(
                    cell_id=stable_cell_id(table_id, row, column),
                    row=row,
                    column=column,
                    raw_text=values[row][column],
                    native_locator=NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        table_index=0,
                        row_index=row,
                        column_index=column,
                        bbox=bbox,
                    ),
                )
            )
    source_table = SourceNode(
        source_node_id=_source_node_id(SourceNodeKind.TABLE, table_locator),
        kind=SourceNodeKind.TABLE,
        source_order=len(nodes),
        raw_text="",
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=4,
            column_count=3,
            cells=tuple(cells),
        ),
    )
    nodes.append(source_table)

    paragraph("9.16.3 Energy requirement", y0=290.0)
    for reference_index, raw_text in enumerate(figure_body_references):
        paragraph(raw_text, y0=320.0 + reference_index * 18.0)
    image_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=len(nodes),
        bbox=(
            180.0,
            350.0 + max(0, len(figure_body_references) - 1) * 18.0,
            420.0,
            500.0 + max(0, len(figure_body_references) - 1) * 18.0,
        ),
    )
    asset = SourceAssetRef(
        asset_id="asset-synthetic-figure",
        kind=SourceAssetKind.IMAGE,
        content_sha256="d" * 64,
        media_type="image/png",
        storage_key="synthetic/figure.png",
        native_locator=image_locator,
    )
    image = SourceNode(
        source_node_id=_source_node_id(SourceNodeKind.IMAGE, image_locator),
        kind=SourceNodeKind.IMAGE,
        source_order=len(nodes),
        native_locator=image_locator,
        asset_ref=asset.asset_id,
    )
    nodes.append(image)
    y_offset = max(0, len(figure_body_references) - 1) * 18.0
    paragraph(figure_caption, y0=515.0 + y_offset)
    paragraph("9.16.4 Another requirement", y0=550.0 + y_offset)
    second_caption = paragraph(second_table_caption, y0=580.0 + y_offset)
    return (
        _source_snapshot(nodes=tuple(nodes), assets=(asset,)),
        source_table,
        image,
        (first_caption, second_caption),
    )


def _cross_page_folded_table_source() -> tuple[
    SourceIRSnapshot, SourceNode, SourceNode
]:
    heading_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
        bbox=(70.0, 100.0, 520.0, 118.0),
    )
    paragraph_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=1,
        bbox=(70.0, 700.0, 520.0, 718.0),
    )
    overlay_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=2,
        block_index=0,
        bbox=(60.0, 105.0, 540.0, 125.0),
    )
    heading = SourceNode(
        source_node_id=_source_node_id(SourceNodeKind.PARAGRAPH, heading_locator),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=0,
        raw_text="8.3.1 Synthetic interface requirements",
        native_locator=heading_locator,
    )
    paragraph = SourceNode(
        source_node_id=_source_node_id(
            SourceNodeKind.PARAGRAPH,
            paragraph_locator,
        ),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=1,
        raw_text="The requirement continues on the next page.",
        native_locator=paragraph_locator,
    )
    overlay = SourceNode(
        source_node_id=_source_node_id(SourceNodeKind.PARAGRAPH, overlay_locator),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=2,
        raw_text="Field",
        native_locator=overlay_locator,
    )
    table = _pdf_table_node(
        page_number=2,
        table_index=0,
        bbox=(50.0, 90.0, 550.0, 350.0),
        source_order=3,
        header=("Field", "Requirement"),
        data=("Item", "Value"),
    )
    return _source_snapshot(nodes=(heading, paragraph, overlay, table)), table, overlay


def _continued_pdf_table_source(
    *,
    repeat_header: bool = True,
    same_section: bool = True,
    horizontal_offset: float = 0.0,
    second_column_split_ratio: float = 0.5,
    source_caption: str | None = None,
    target_caption: str | None = None,
    heading_bbox: tuple[float, float, float, float] = (
        70.0,
        100.0,
        500.0,
        118.0,
    ),
    first_table_bbox: tuple[float, float, float, float] | None = None,
    second_table_bbox: tuple[float, float, float, float] | None = None,
) -> tuple[SourceIRSnapshot, SourceNode, SourceNode, SourceNode]:
    nodes: list[SourceNode] = []
    heading_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
        bbox=heading_bbox,
    )
    nodes.append(
        SourceNode(
            source_node_id=_source_node_id(SourceNodeKind.PARAGRAPH, heading_locator),
            kind=SourceNodeKind.PARAGRAPH,
            source_order=len(nodes),
            raw_text="8.3.1 接口要求",
            native_locator=heading_locator,
        )
    )

    if source_caption is not None:
        source_caption_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=len(nodes),
            bbox=(70.0, 570.0, 500.0, 590.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    source_caption_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=len(nodes),
                raw_text=source_caption,
                native_locator=source_caption_locator,
            )
        )

    first_bbox = first_table_bbox or (50.0, 600.0, 550.0, 780.0)
    first_table = _pdf_table_node(
        page_number=1,
        table_index=0,
        bbox=first_bbox,
        source_order=len(nodes) + 1,
        header=("字段", "要求"),
        data=("第一项", "值一"),
    )
    overlay_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=len(nodes),
        bbox=(60.0, 625.0, 285.0, 650.0),
    )
    overlay = SourceNode(
        source_node_id=_source_node_id(SourceNodeKind.PARAGRAPH, overlay_locator),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=len(nodes),
        raw_text="字段",
        native_locator=overlay_locator,
    )
    nodes.extend((overlay, first_table))

    if not same_section:
        second_heading_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            block_index=0,
            bbox=(70.0, 60.0, 500.0, 78.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    second_heading_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=len(nodes),
                raw_text="8.3.2 其他要求",
                native_locator=second_heading_locator,
            )
        )

    if target_caption is not None:
        caption_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            block_index=1 if not same_section else 0,
            bbox=(70.0, 80.0 if not same_section else 60.0, 500.0, 88.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    caption_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=len(nodes),
                raw_text=target_caption,
                native_locator=caption_locator,
            )
        )

    second_bbox = second_table_bbox or (
        50.0 + horizontal_offset,
        90.0,
        550.0 + horizontal_offset,
        350.0,
    )
    second_table = _pdf_table_node(
        page_number=2,
        table_index=0,
        bbox=second_bbox,
        source_order=len(nodes),
        header=("字段", "要求") if repeat_header else ("其他", "列名"),
        data=("第二项", "值二"),
        column_split_ratio=second_column_split_ratio,
    )
    nodes.append(second_table)
    return _source_snapshot(nodes=tuple(nodes)), first_table, second_table, overlay


def _continued_pdf_table_partition_source(
    *,
    first_column_widths: tuple[float, ...],
    second_column_widths: tuple[float, ...],
    source_caption: str | None = None,
    target_caption: str | None = None,
) -> tuple[SourceIRSnapshot, SourceNode, SourceNode]:
    heading_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
        bbox=(70.0, 100.0, 500.0, 118.0),
    )
    heading = SourceNode(
        source_node_id=_source_node_id(
            SourceNodeKind.PARAGRAPH,
            heading_locator,
        ),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=0,
        raw_text="8.3.1 Synthetic partitioned table",
        native_locator=heading_locator,
    )
    nodes: list[SourceNode] = [heading]
    if source_caption is not None:
        source_caption_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=1,
            bbox=(180.0, 570.0, 420.0, 590.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    source_caption_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=len(nodes),
                raw_text=source_caption,
                native_locator=source_caption_locator,
            )
        )
    first_table = _pdf_table_node_with_column_widths(
        page_number=1,
        table_index=0,
        bbox=(50.0, 600.0, 50.0 + sum(first_column_widths), 780.0),
        source_order=len(nodes),
        column_widths=first_column_widths,
        marker="first",
    )
    nodes.append(first_table)
    if target_caption is not None:
        target_caption_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            block_index=0,
            bbox=(180.0, 60.0, 420.0, 80.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    target_caption_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=len(nodes),
                raw_text=target_caption,
                native_locator=target_caption_locator,
            )
        )
    second_table = _pdf_table_node_with_column_widths(
        page_number=2,
        table_index=0,
        bbox=(50.0, 90.0, 50.0 + sum(second_column_widths), 350.0),
        source_order=len(nodes),
        column_widths=second_column_widths,
        marker="second",
    )
    nodes.append(second_table)
    return (
        _source_snapshot(nodes=tuple(nodes)),
        first_table,
        second_table,
    )


def _competing_cross_page_tables_source(
    *,
    with_caption_boundary: bool,
) -> tuple[SourceIRSnapshot, SourceNode, SourceNode, SourceNode]:
    heading_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
        bbox=(70.0, 100.0, 500.0, 118.0),
    )
    heading = SourceNode(
        source_node_id=_source_node_id(
            SourceNodeKind.PARAGRAPH,
            heading_locator,
        ),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=0,
        raw_text="8.3.1 参数要求",
        native_locator=heading_locator,
    )
    earlier_table = _pdf_table_node(
        page_number=1,
        table_index=0,
        bbox=(50.0, 500.0, 550.0, 690.0),
        source_order=1,
        header=("字段", "要求"),
        data=("旧项目", "旧值"),
    )
    nodes: list[SourceNode] = [heading, earlier_table]
    if with_caption_boundary:
        caption_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=1,
            bbox=(220.0, 700.0, 380.0, 718.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=_source_node_id(
                    SourceNodeKind.PARAGRAPH,
                    caption_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=2,
                raw_text="表 22 参数要求",
                native_locator=caption_locator,
            )
        )
    header_table = _pdf_table_node(
        page_number=1,
        table_index=1,
        bbox=(50.0, 725.0, 550.0, 780.0),
        source_order=len(nodes),
        header=("参数", "参考值"),
        data=("", ""),
    )
    nodes.append(header_table)
    next_page_table = _pdf_table_node(
        page_number=2,
        table_index=0,
        bbox=(50.0, 60.0, 550.0, 350.0),
        source_order=len(nodes),
        header=("超调量", "小于 3%"),
        data=("通讯周期", "1ms"),
    )
    nodes.append(next_page_table)
    return (
        _source_snapshot(nodes=tuple(nodes)),
        earlier_table,
        header_table,
        next_page_table,
    )


def _pdf_table_text_channel_source(
    *,
    span_multiple_rows: bool = False,
    glyph_spill: bool = False,
) -> tuple[SourceIRSnapshot, SourceNode, SourceNode]:
    table = _pdf_table_node(
        page_number=1,
        table_index=0,
        bbox=(50.0, 100.0, 550.0, 300.0),
        source_order=1,
        header=("参数", "参考值"),
        data=("间隙", "05 28\n. ~ ."),
    )
    overlay_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
        bbox=(
            (80.0, 150.0, 500.0, 250.0)
            if span_multiple_rows
            else (80.0, 185.0, 500.0, 205.0)
            if glyph_spill
            else (80.0, 215.0, 500.0, 235.0)
        ),
    )
    overlay = SourceNode(
        source_node_id=_source_node_id(
            SourceNodeKind.PARAGRAPH,
            overlay_locator,
        ),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=0,
        raw_text=(
            "参数\n参考值\n间隙\n0.5~2.8"
            if span_multiple_rows
            else "参数\n参考值"
            if glyph_spill
            else "间隙\n0.5~2.8"
        ),
        native_locator=overlay_locator,
    )
    return _source_snapshot(nodes=(overlay, table)), table, overlay


def _merged_pdf_table_source() -> tuple[SourceIRSnapshot, SourceNode, TableCellIR]:
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
        bbox=(50.0, 100.0, 550.0, 180.0),
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    cell = TableCellIR(
        cell_id=stable_cell_id(table_id, 0, 0),
        row=0,
        column=0,
        row_span=1,
        column_span=2,
        raw_text="原生合并文字",
        native_locator=NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            table_index=0,
            row_index=0,
            column_index=0,
            bbox=table_locator.bbox,
        ),
    )
    table_node = SourceNode(
        source_node_id=_source_node_id(SourceNodeKind.TABLE, table_locator),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        raw_text="",
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=1,
            column_count=2,
            cells=(cell,),
            merged_regions=(
                MergedRegion(
                    row=0,
                    column=0,
                    row_span=1,
                    column_span=2,
                ),
            ),
        ),
    )
    return _source_snapshot(nodes=(table_node,)), table_node, cell


def _pdf_table_node(
    *,
    page_number: int,
    table_index: int,
    bbox: tuple[float, float, float, float],
    source_order: int,
    header: tuple[str, str],
    data: tuple[str, str],
    column_split_ratio: float = 0.5,
) -> SourceNode:
    locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=page_number,
        table_index=table_index,
        bbox=bbox,
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=locator,
    )
    row_height = (bbox[3] - bbox[1]) / 2.0
    column_boundary = bbox[0] + (bbox[2] - bbox[0]) * column_split_ratio
    column_boundaries = (bbox[0], column_boundary, bbox[2])
    values = (header, data)
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row, column),
            row=row,
            column=column,
            raw_text=values[row][column],
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=page_number,
                table_index=table_index,
                row_index=row,
                column_index=column,
                bbox=(
                    column_boundaries[column],
                    bbox[1] + row * row_height,
                    column_boundaries[column + 1],
                    bbox[1] + (row + 1) * row_height,
                ),
            ),
        )
        for row in range(2)
        for column in range(2)
    )
    return SourceNode(
        source_node_id=_source_node_id(SourceNodeKind.TABLE, locator),
        kind=SourceNodeKind.TABLE,
        source_order=source_order,
        raw_text="",
        native_locator=locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=2,
            column_count=2,
            cells=cells,
        ),
    )


def _pdf_table_node_with_column_widths(
    *,
    page_number: int,
    table_index: int,
    bbox: tuple[float, float, float, float],
    source_order: int,
    column_widths: tuple[float, ...],
    marker: str,
) -> SourceNode:
    if (
        len(column_widths) < 2
        or any(width <= 0.0 for width in column_widths)
        or abs(sum(column_widths) - (bbox[2] - bbox[0])) > 0.001
    ):
        raise ValueError("column widths must form the candidate bbox")
    locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=page_number,
        table_index=table_index,
        bbox=bbox,
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=locator,
    )
    column_boundaries = [bbox[0]]
    for width in column_widths:
        column_boundaries.append(column_boundaries[-1] + width)
    row_count = 2
    row_height = (bbox[3] - bbox[1]) / row_count
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row, column),
            row=row,
            column=column,
            raw_text=f"{marker}-{row}-{column}",
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=page_number,
                table_index=table_index,
                row_index=row,
                column_index=column,
                bbox=(
                    column_boundaries[column],
                    bbox[1] + row * row_height,
                    column_boundaries[column + 1],
                    bbox[1] + (row + 1) * row_height,
                ),
            ),
        )
        for row in range(row_count)
        for column in range(len(column_widths))
    )
    return SourceNode(
        source_node_id=_source_node_id(SourceNodeKind.TABLE, locator),
        kind=SourceNodeKind.TABLE,
        source_order=source_order,
        raw_text="",
        native_locator=locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=row_count,
            column_count=len(column_widths),
            cells=cells,
        ),
    )


def _pdf_table_image_snapshot(
    *,
    image_bbox: tuple[float, float, float, float],
    duplicate_table: bool = False,
    target_cell_text: str = "",
    asset_derivation: SourceAssetDerivation | None = None,
) -> tuple[SourceIRSnapshot, SourceNode, SourceNode, TableCellIR]:
    table_bbox = (40.0, 40.0, 280.0, 140.0)
    table_node = _pdf_table_node(
        page_number=1,
        table_index=0,
        bbox=table_bbox,
        source_order=0,
        header=("字段", target_cell_text),
        data=("接口", "说明"),
    )
    nodes: list[SourceNode] = [table_node]
    if duplicate_table:
        nodes.append(
            _pdf_table_node(
                page_number=1,
                table_index=1,
                bbox=table_bbox,
                source_order=1,
                header=("字段", target_cell_text),
                data=("接口", "说明"),
            )
        )
    image_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=7,
        bbox=image_bbox,
    )
    asset = SourceAssetRef(
        asset_id="asset-pdf-table-image",
        kind=SourceAssetKind.IMAGE,
        content_sha256="e" * 64,
        media_type="image/png",
        storage_key="pdf/images/table-cell.png",
        native_locator=image_locator,
    )
    image_node = SourceNode(
        source_node_id=_source_node_id(SourceNodeKind.IMAGE, image_locator),
        kind=SourceNodeKind.IMAGE,
        source_order=len(nodes),
        raw_text="",
        native_locator=image_locator,
        visual_locator=image_locator,
        asset_ref=asset.asset_id,
        asset_derivation=asset_derivation,
    )
    nodes.append(image_node)
    target_cell = next(
        cell
        for cell in table_node.table_payload.cells
        if cell.row == 0 and cell.column == 1
    )
    return (
        _source_snapshot(nodes=tuple(nodes), assets=(asset,)),
        table_node,
        image_node,
        target_cell,
    )


def _docx_table_cell_snapshot() -> tuple[
    SourceIRSnapshot, SourceNode, SourceNode, TableCellIR
]:
    source_hash = "c" * 64
    adapter_id = "native-docx-test"
    adapter_version = "1.0.0"
    table_locator = NativeLocator(
        format=NativeDocumentFormat.DOCX,
        body_element_index=0,
        table_index=0,
    )
    cell_locator = NativeLocator(
        format=NativeDocumentFormat.DOCX,
        body_element_index=0,
        table_index=0,
        row_index=0,
        column_index=0,
    )
    paragraph_locator = NativeLocator(
        format=NativeDocumentFormat.DOCX,
        body_element_index=0,
        table_index=0,
        row_index=0,
        column_index=0,
        paragraph_index=0,
    )
    table_id = stable_table_id(
        source_sha256=source_hash,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        native_locator=table_locator,
    )
    cell = TableCellIR(
        cell_id=stable_cell_id(table_id, 0, 0),
        row=0,
        column=0,
        raw_text="单元格忠实文本",
        native_locator=cell_locator,
    )
    table_node_id = stable_node_id(
        source_sha256=source_hash,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        kind=SourceNodeKind.TABLE,
        native_locator=table_locator,
    )
    table_node = SourceNode(
        source_node_id=table_node_id,
        kind=SourceNodeKind.TABLE,
        source_order=0,
        raw_text="",
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=1,
            column_count=1,
            cells=(cell,),
        ),
    )
    cell_paragraph = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=source_hash,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            kind=SourceNodeKind.PARAGRAPH,
            native_locator=paragraph_locator,
        ),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=1,
        raw_text="单元格忠实文本",
        native_locator=paragraph_locator,
        parent_source_node_id=table_node_id,
    )
    document_id = stable_document_id(source_hash)
    snapshot = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=source_hash,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=source_hash,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=(table_node, cell_paragraph),
        created_at=CREATED_AT,
    )
    return snapshot, table_node, cell_paragraph, cell


def _source_snapshot(
    *,
    nodes: tuple[SourceNode, ...],
    assets: tuple[SourceAssetRef, ...] = (),
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
        created_at=CREATED_AT,
    )


def _empty_source_snapshot(*, status: SourceIRStatus) -> SourceIRSnapshot:
    payload = _source_snapshot(nodes=()).model_dump(mode="python")
    payload["status"] = status
    return SourceIRSnapshot.model_validate(payload)


def _source_node_id(kind: SourceNodeKind, locator: NativeLocator) -> str:
    return stable_node_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        kind=kind,
        native_locator=locator,
    )


def _node_for_source(
    tree: StructureTreeSnapshot,
    source_node_id: str,
) -> StructureNode:
    return next(
        node for node in tree.nodes if node.identity_key == f"source:{source_node_id}"
    )


def _table_rows(
    tree: StructureTreeSnapshot,
    table_structure_node_id: str,
) -> list[StructureNode]:
    return sorted(
        (
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.parent_structure_node_id == table_structure_node_id
        ),
        key=lambda node: node.table_row_index or 0,
    )


if __name__ == "__main__":
    unittest.main()
