from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from datetime import UTC, datetime
import unittest

from app.application.agent_workflow.document_map_exploration import (
    DocumentMapSurveyCandidateV1,
    DocumentMapSurveyRangeCandidateV1,
    DocumentMapSurveyRangeKind,
    DocumentMapSurveyStatus,
    DocumentMapVisualCoverageMode,
    build_document_map_visual_coverage_receipt,
    compile_document_map_survey,
)
from app.application.agent_workflow.document_map_scope import (
    DocumentMapHeadingDecisionV1,
    DocumentMapHeadingDisposition,
    DocumentMapHeadingExpansionProposalV2,
    DocumentMapHeadingScopeAction,
    DocumentMapHeadingScopeActionV2,
    DocumentMapHeadingScopeCandidateV1,
    DocumentMapHeadingScopeCandidateV2,
    build_document_map_scope_request,
    build_document_map_scope_request_from_evidence_index,
    compile_document_map_heading_scope,
    expand_document_map_heading_candidates,
)
from app.application.extraction.agent_logical_table_scope import (
    source_ir_canonical_sha256,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from app.application.agent_workflow.native_heading_skeleton import (
    NativeHeadingCandidateState,
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


class DocumentMapScopeTests(unittest.TestCase):
    def test_late_emitted_table_is_bound_by_page_geometry_not_parser_order(
        self,
    ) -> None:
        source, survey, visual, skeleton = _fixtures()
        roots = tuple(
            item
            for item in skeleton.candidates
            if item.level == 1 and item.number != "20"
        )
        ledger = [
            {
                "source_ordinal": ordinal,
                "source_node_id": node.source_node_id,
                "source_order": node.source_order,
                "page_number": node.native_locator.page_number,
                "table_id": None,
                "asset_ref": None,
                "bbox": node.native_locator.bbox,
            }
            for ordinal, node in enumerate(source.nodes)
        ]
        late_node_id, late_table_id = "node-" + "f" * 32, "table-" + "f" * 32
        ledger.append(
            {
                "source_ordinal": len(ledger),
                "source_node_id": late_node_id,
                "source_order": max(node.source_order for node in source.nodes) + 1,
                "page_number": roots[2].page_number,
                "table_id": late_table_id,
                "asset_ref": None,
                "bbox": [70.0, roots[2].bbox[1] - 25, 310.0, roots[2].bbox[1] - 5],
            }
        )
        request = build_document_map_scope_request_from_evidence_index(
            run_id=survey.run_id,
            source_sha256=source.source_sha256,
            source_ir_id=source.source_ir_id,
            source_ir_canonical_sha256="7" * 64,
            evidence_workspace_bundle_sha256="9" * 64,
            source_ordinal_records=tuple(ledger),
            survey=survey,
            visual_coverage=visual,
            heading_skeleton=skeleton,
            start_candidate_id=roots[1].candidate_id,
            next_candidate_id=roots[2].candidate_id,
        )
        self.assertIn(late_node_id, request.source_node_ids)
        self.assertIn(late_table_id, request.physical_table_ids)
        self.assertNotIn(late_node_id, request.following_context_source_node_ids)
        ledger[-1]["bbox"] = [70.0, roots[2].bbox[3] + 5, 310.0, roots[2].bbox[3] + 25]
        outside = build_document_map_scope_request_from_evidence_index(
            run_id=survey.run_id,
            source_sha256=source.source_sha256,
            source_ir_id=source.source_ir_id,
            source_ir_canonical_sha256="7" * 64,
            evidence_workspace_bundle_sha256="9" * 64,
            source_ordinal_records=tuple(ledger),
            survey=survey,
            visual_coverage=visual,
            heading_skeleton=skeleton,
            start_candidate_id=roots[1].candidate_id,
            next_candidate_id=roots[2].candidate_id,
        )
        self.assertNotIn(late_table_id, outside.physical_table_ids)

    def test_same_pdf_page_can_belong_to_two_disjoint_source_scopes(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        chapter_one, chapter_two = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )

        self.assertEqual(chapter_one.page_numbers, (1,))
        self.assertEqual(chapter_two.page_numbers, (1, 2))
        self.assertTrue(
            set(chapter_one.page_numbers).intersection(chapter_two.page_numbers)
        )
        self.assertFalse(
            set(chapter_one.source_node_ids).intersection(chapter_two.source_node_ids)
        )
        self.assertEqual(
            chapter_one.suggested_source_span.end,
            chapter_two.suggested_source_span.start,
        )
        self.assertEqual(chapter_one.suggested_source_span.end.offset, 0)

    def test_agent_can_reject_false_numeric_row_and_compile_stable_tree(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        chapter_one, _ = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )
        candidate = _accepted_candidate(chapter_one)

        compilation = compile_document_map_heading_scope(
            request=chapter_one,
            candidate=candidate,
        )
        replay = compile_document_map_heading_scope(
            request=chapter_one,
            candidate=candidate,
        )

        by_number = {item.number: item for item in compilation.heading_nodes}
        self.assertEqual(set(by_number), {"1", "1.1"})
        self.assertEqual(
            by_number["1.1"].parent_stable_node_id,
            by_number["1"].stable_node_id,
        )
        false_row_id = next(
            item.candidate_id
            for item in chapter_one.heading_candidates
            if item.number == "20"
        )
        self.assertIn(false_row_id, compilation.non_heading_candidate_ids)
        self.assertEqual(replay.compilation_sha256, compilation.compilation_sha256)
        self.assertFalse(compilation.document_map_complete)
        self.assertTrue(compilation.private_checkpoint_only)

    def test_every_candidate_must_receive_an_agent_decision(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        chapter_one, _ = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )
        candidate = _accepted_candidate(chapter_one)
        payload = candidate.model_dump(mode="json")
        payload["heading_decisions"] = payload["heading_decisions"][:-1]

        with self.assertRaisesRegex(ValueError, "cover every advisory candidate"):
            compile_document_map_heading_scope(
                request=chapter_one,
                candidate=DocumentMapHeadingScopeCandidateV1.model_validate(payload),
            )

    def test_false_top_level_candidate_cannot_become_a_second_chapter_root(
        self,
    ) -> None:
        source, survey, visual, skeleton = _fixtures()
        chapter_one, _ = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )
        candidate = _accepted_candidate(chapter_one)
        payload = candidate.model_dump(mode="json")
        for decision in payload["heading_decisions"]:
            ref = next(
                item
                for item in chapter_one.heading_candidates
                if item.candidate_id == decision["candidate_id"]
            )
            if ref.number == "20":
                decision["disposition"] = "HEADING"

        with self.assertRaisesRegex(ValueError, "exactly its first candidate"):
            compile_document_map_heading_scope(
                request=chapter_one,
                candidate=DocumentMapHeadingScopeCandidateV1.model_validate(payload),
            )

    def test_finding_cannot_claim_a_page_outside_the_exact_scope(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        chapter_one, _ = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )
        candidate = _accepted_candidate(chapter_one)
        payload = candidate.model_dump(mode="json")
        payload["findings"] = [
            {
                "finding_code": "UNRELATED_PAGE",
                "evidence_refs": ["page:2"],
                "public_summary": "该证据不属于当前章节。",
            }
        ]

        with self.assertRaisesRegex(ValueError, "leaves its owned page context"):
            compile_document_map_heading_scope(
                request=chapter_one,
                candidate=DocumentMapHeadingScopeCandidateV1.model_validate(payload),
            )

    def test_finding_can_bind_more_precise_owned_node_evidence(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        chapter_one, _ = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )
        candidate = _accepted_candidate(chapter_one)
        payload = candidate.model_dump(mode="json")
        payload["findings"] = [
            {
                "finding_code": "BOUNDARY_CONFIRMED",
                "evidence_refs": [
                    chapter_one.source_node_ids[0],
                    chapter_one.following_context_source_node_ids[0],
                    "page:1",
                ],
                "public_summary": "来源节点与页面共同证明章节边界。",
            }
        ]

        compilation = compile_document_map_heading_scope(
            request=chapter_one,
            candidate=DocumentMapHeadingScopeCandidateV1.model_validate(payload),
        )

        self.assertEqual(
            compilation.findings[0].evidence_refs,
            (
                chapter_one.source_node_ids[0],
                chapter_one.following_context_source_node_ids[0],
                "page:1",
            ),
        )

    def test_finding_cannot_claim_an_unrelated_source_node(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        chapter_one, _ = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )
        candidate = _accepted_candidate(chapter_one)
        payload = candidate.model_dump(mode="json")
        payload["findings"] = [
            {
                "finding_code": "UNRELATED_NODE",
                "evidence_refs": [source.nodes[-1].source_node_id],
                "public_summary": "该节点不属于当前范围。",
            }
        ]

        with self.assertRaisesRegex(ValueError, "leaves its source context"):
            compile_document_map_heading_scope(
                request=chapter_one,
                candidate=DocumentMapHeadingScopeCandidateV1.model_validate(payload),
            )

    def test_context_window_is_bounded_host_resource_not_agent_gate(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        chapter_roots = tuple(
            item
            for item in skeleton.candidates
            if item.level == 1 and item.number != "20"
        )

        with self.assertRaisesRegex(ValueError, "context size"):
            build_document_map_scope_request(
                source_ir=source,
                survey=survey,
                visual_coverage=visual,
                heading_skeleton=skeleton,
                evidence_workspace_bundle_sha256="9" * 64,
                start_candidate_id=chapter_roots[0].candidate_id,
                next_candidate_id=chapter_roots[1].candidate_id,
                context_nodes_per_side=9,
            )

    def test_locked_evidence_index_builds_same_scope_without_reparsing_pdf(
        self,
    ) -> None:
        source, survey, visual, skeleton = _fixtures()
        direct, _ = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )
        records = tuple(
            {
                "source_ordinal": ordinal,
                "source_node_id": item.source_node_id,
                "source_order": item.source_order,
                "source_kind": item.kind.value,
                "page_number": item.native_locator.page_number,
                "source_text_length": len(item.raw_text),
                "source_text_sha256": "0" * 64,
                "table_id": (
                    item.table_payload.table_id
                    if item.table_payload is not None
                    else None
                ),
                "asset_ref": item.asset_ref,
            }
            for ordinal, item in enumerate(source.nodes)
        )
        roots = tuple(
            item
            for item in skeleton.candidates
            if item.level == 1 and item.number != "20"
        )

        indexed = build_document_map_scope_request_from_evidence_index(
            run_id=survey.run_id,
            source_sha256=source.source_sha256,
            source_ir_id=source.source_ir_id,
            source_ir_canonical_sha256=source_ir_canonical_sha256(source),
            evidence_workspace_bundle_sha256="9" * 64,
            source_ordinal_records=records,
            survey=survey,
            visual_coverage=visual,
            heading_skeleton=skeleton,
            start_candidate_id=roots[0].candidate_id,
            next_candidate_id=roots[1].candidate_id,
        )

        self.assertEqual(indexed, direct)

    def test_evidence_index_rejects_reordered_source_ledger(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        records = tuple(
            {
                "source_ordinal": ordinal,
                "source_node_id": item.source_node_id,
                "source_order": item.source_order,
                "page_number": item.native_locator.page_number,
                "table_id": None,
                "asset_ref": None,
            }
            for ordinal, item in enumerate(reversed(source.nodes))
        )
        roots = tuple(
            item
            for item in skeleton.candidates
            if item.level == 1 and item.number != "20"
        )

        with self.assertRaisesRegex(ValueError, "source index is invalid"):
            build_document_map_scope_request_from_evidence_index(
                run_id=survey.run_id,
                source_sha256=source.source_sha256,
                source_ir_id=source.source_ir_id,
                source_ir_canonical_sha256=source_ir_canonical_sha256(source),
                evidence_workspace_bundle_sha256="9" * 64,
                source_ordinal_records=records,
                survey=survey,
                visual_coverage=visual,
                heading_skeleton=skeleton,
                start_candidate_id=roots[0].candidate_id,
                next_candidate_id=roots[1].candidate_id,
            )

    def test_agent_can_expand_advisory_universe_with_source_bound_heading(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        request, _ = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )
        missing_node = source.nodes[3]
        root = request.heading_candidates[0]
        expansion = DocumentMapHeadingScopeCandidateV2(
            request_id=request.scope_id,
            request_sha256=request.request_sha256,
            action=DocumentMapHeadingScopeActionV2.EXPAND_HEADING_CANDIDATES,
            accepted_source_span=request.suggested_source_span,
            heading_decisions=(),
            heading_expansion_proposals=(
                DocumentMapHeadingExpansionProposalV2(
                    source_node_id=missing_node.source_node_id,
                    number=None,
                    title="本章正文",
                    level=2,
                    parent_source_node_id=root.source_node_id,
                    confidence=0.95,
                    public_summary="视觉层级表明这是骨架漏掉的无编号标题。",
                ),
            ),
            findings=(),
            public_summary="发现一个骨架外标题，请扩展候选后继续。",
        )

        expanded = expand_document_map_heading_candidates(
            request=request,
            candidate=expansion,
            source_ir=source,
        )

        self.assertEqual(expanded.scope_id, request.scope_id)
        self.assertNotEqual(expanded.request_sha256, request.request_sha256)
        self.assertEqual(
            len(expanded.heading_candidates),
            len(request.heading_candidates) + 1,
        )
        added = next(
            item
            for item in expanded.heading_candidates
            if item.source_node_id == missing_node.source_node_id
        )
        self.assertIsNone(added.number)
        self.assertEqual(added.title, "本章正文")
        self.assertEqual(
            added.advisory_state,
            NativeHeadingCandidateState.REVIEW_REQUIRED,
        )
        self.assertEqual(added.advisory_parent_candidate_id, root.candidate_id)

        decisions = tuple(
            DocumentMapHeadingDecisionV1(
                candidate_id=ref.candidate_id,
                disposition=(
                    DocumentMapHeadingDisposition.NOT_HEADING
                    if ref.number == "20"
                    else DocumentMapHeadingDisposition.HEADING
                ),
                parent_candidate_id=(
                    None
                    if ref.candidate_id == root.candidate_id or ref.number == "20"
                    else root.candidate_id
                ),
                confidence=0.98,
                public_summary="已依据原文与视觉证据核对。",
            )
            for ref in expanded.heading_candidates
        )
        accepted = DocumentMapHeadingScopeCandidateV2(
            request_id=expanded.scope_id,
            request_sha256=expanded.request_sha256,
            action=DocumentMapHeadingScopeActionV2.ACCEPT_SCOPE,
            accepted_source_span=expanded.suggested_source_span,
            heading_decisions=decisions,
            heading_expansion_proposals=(),
            findings=(),
            public_summary="扩展后的完整标题树已核对。",
        )
        compilation = compile_document_map_heading_scope(
            request=expanded,
            candidate=accepted,
        )

        self.assertIn(
            missing_node.source_node_id,
            {item.source_node_id for item in compilation.heading_nodes},
        )

    def test_heading_expansion_rejects_unowned_or_invented_source_text(self) -> None:
        source, survey, visual, skeleton = _fixtures()
        request, _ = _chapter_requests(
            source=source,
            survey=survey,
            visual=visual,
            skeleton=skeleton,
        )
        root = request.heading_candidates[0]

        for node_id, title, reason in (
            (source.nodes[-1].source_node_id, "术语和定义", "owned non-candidate"),
            (source.nodes[3].source_node_id, "不存在的标题", "not source-bound"),
        ):
            with self.subTest(reason=reason):
                proposal = DocumentMapHeadingScopeCandidateV2(
                    request_id=request.scope_id,
                    request_sha256=request.request_sha256,
                    action=(DocumentMapHeadingScopeActionV2.EXPAND_HEADING_CANDIDATES),
                    accepted_source_span=request.suggested_source_span,
                    heading_decisions=(),
                    heading_expansion_proposals=(
                        DocumentMapHeadingExpansionProposalV2(
                            source_node_id=node_id,
                            title=title,
                            level=2,
                            parent_source_node_id=root.source_node_id,
                            confidence=0.9,
                            public_summary="请求补充标题候选。",
                        ),
                    ),
                    findings=(),
                    public_summary="请求扩展标题候选。",
                )
                with self.assertRaises(ValueError):
                    expand_document_map_heading_candidates(
                        request=request,
                        candidate=proposal,
                        source_ir=source,
                    )


def _fixtures():
    source = _source_ir(
        (
            _node(0, "封面说明", page=1),
            _node(1, "1\n范围", page=1),
            _node(2, "1.1\n目的", page=1),
            _node(3, "本章正文。", page=1),
            _node(4, "20\n周的，视为长周期物料", page=1, x=86.0),
            _node(5, "2\n规范性引用文件", page=1),
            _node(6, "第二章跨页正文。", page=2),
            _node(7, "2.1\n引用原则", page=2),
            _node(8, "第二章其余正文。", page=2),
            _node(9, "3\n术语和定义", page=2),
        )
    )
    visual = build_document_map_visual_coverage_receipt(
        source_sha256=source.source_sha256,
        page_count=2,
        mode=DocumentMapVisualCoverageMode.REFERENCE_FULL_PAGE,
        event_summary={
            "schema_version": "reqpilotx-canonical-stage-event-ledger/v1",
            "visual_inspection": {
                "contact_sheet_opened": True,
                "high_resolution_page_numbers": [1, 2],
                "other_image_view_count": 0,
            },
        },
    )
    survey_candidate = DocumentMapSurveyCandidateV1(
        run_id="agent-run-" + "1" * 32,
        source_sha256=source.source_sha256,
        kit_identity=_kit_identity(),
        page_count=2,
        status=DocumentMapSurveyStatus.COMPLETE,
        document_summary="一份两页且章节边界共享物理页的技术文档。",
        ranges=(
            DocumentMapSurveyRangeCandidateV1(
                proposal_key="main-document",
                kind=DocumentMapSurveyRangeKind.MAIN_DOCUMENT,
                label="正文",
                page_numbers=(1, 2),
                evidence_refs=("page:1", "page:2"),
                public_summary="正文跨两页连续排版。",
            ),
        ),
    )
    survey = compile_document_map_survey(
        survey_candidate,
        visual_coverage=visual,
        expected_run_id=survey_candidate.run_id,
        expected_source_sha256=source.source_sha256,
        expected_kit_identity=survey_candidate.kit_identity,
    )
    return source, survey, visual, build_native_heading_skeleton(source)


def _chapter_requests(*, source, survey, visual, skeleton):
    roots = tuple(
        item for item in skeleton.candidates if item.level == 1 and item.number != "20"
    )
    chapter_one = build_document_map_scope_request(
        source_ir=source,
        survey=survey,
        visual_coverage=visual,
        heading_skeleton=skeleton,
        evidence_workspace_bundle_sha256="9" * 64,
        start_candidate_id=roots[0].candidate_id,
        next_candidate_id=roots[1].candidate_id,
    )
    chapter_two = build_document_map_scope_request(
        source_ir=source,
        survey=survey,
        visual_coverage=visual,
        heading_skeleton=skeleton,
        evidence_workspace_bundle_sha256="9" * 64,
        start_candidate_id=roots[1].candidate_id,
        next_candidate_id=roots[2].candidate_id,
    )
    return chapter_one, chapter_two


def _accepted_candidate(request):
    root_id = request.heading_candidates[0].candidate_id
    decisions = []
    for ref in request.heading_candidates:
        is_heading = ref.number != "20"
        decisions.append(
            DocumentMapHeadingDecisionV1(
                candidate_id=ref.candidate_id,
                disposition=(
                    DocumentMapHeadingDisposition.HEADING
                    if is_heading
                    else DocumentMapHeadingDisposition.NOT_HEADING
                ),
                parent_candidate_id=(
                    root_id if is_heading and ref.level == 2 else None
                ),
                confidence=0.98,
                public_summary=(
                    "编号、排版和上下文共同支持该标题层级。"
                    if is_heading
                    else "这是正文中的数值行，不是标题。"
                ),
            )
        )
    return DocumentMapHeadingScopeCandidateV1(
        request_id=request.scope_id,
        request_sha256=request.request_sha256,
        action=DocumentMapHeadingScopeAction.ACCEPT_SCOPE,
        accepted_source_span=request.suggested_source_span,
        heading_decisions=tuple(decisions),
        findings=(),
        public_summary="本章节标题层级已经依据原文上下文完成核对。",
    )


def _kit_identity() -> AgentKitIdentityV1:
    return AgentKitIdentityV1(
        kit_id="reqpilotx-document-extraction",
        kit_version="0.15.0",
        manifest_sha256="4" * 64,
        bundle_sha256="3" * 64,
        policy_id="reqpilotx-extraction-policy",
        policy_version="1.0.0",
        tool_registry_id="reqpilotx-document-evidence-tools",
        tool_registry_version="1.3.0",
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
        created_at=datetime(2026, 9, 3, tzinfo=UTC),
    )


def _node(order: int, text: str, *, page: int, x: float = 70.0) -> SourceNode:
    return SourceNode(
        source_node_id=f"node-placeholder-{order}",
        kind=SourceNodeKind.PARAGRAPH,
        source_order=order,
        raw_text=text,
        native_locator=NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page,
            block_index=order,
            bbox=(x, 70.0 + order * 10.0, x + 240.0, 79.0 + order * 10.0),
        ),
    )


if __name__ == "__main__":
    unittest.main()
