from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import re
import hashlib
import unittest
from datetime import datetime, timezone

from app.application.extraction.inspect_structure import (
    STRUCTURE_INSPECTION_RUNTIME_VERSION,
    StructureInspectionCandidateCategory,
    StructureInspectionCandidateV1,
    StructureInspectionConfidence,
    StructureInspectionDisposition,
    StructureInspectionNodeMetricV1,
    StructureInspectionSnapshotV1,
    inspect_structure,
)
from app.application.extraction.structure_boundary_rules import (
    STRUCTURE_BOUNDARY_RULESET_VERSION,
)
from app.application.extraction.processing_pipeline import (
    _serialize_structure_inspection,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.source_ir import (
    MergedRegion,
    NativeDocumentFormat,
    NativeLocator,
    PdfTextLineGeometry,
    SourceDiagnostic,
    SourceDiagnosticSeverity,
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
from app.domain.extraction.structure import (
    StructureBoundaryBasis,
    StructureNode,
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
    stable_structure_node_id,
    stable_structure_relation_id,
    stable_structure_snapshot_id,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)


SOURCE_HASH = "d" * 64
ADAPTER_ID = "structure-inspection-test"
ADAPTER_VERSION = "1.0.0"
CREATED_AT = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)


class StructureInspectionTests(unittest.TestCase):
    def test_inspection_identity_binds_runtime_and_boundary_ruleset(self) -> None:
        source_node = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="1 Synthetic section",
            kind=SourceNodeKind.HEADING,
        )
        source = _source_snapshot(nodes=(source_node,))
        inspection = inspect_structure(source, _simple_tree(source))

        self.assertEqual(
            inspection.inspection_runtime_version,
            STRUCTURE_INSPECTION_RUNTIME_VERSION,
        )
        self.assertEqual(
            inspection.boundary_ruleset_version,
            STRUCTURE_BOUNDARY_RULESET_VERSION,
        )
        tampered = inspection.model_dump(mode="json")
        tampered["boundary_ruleset_version"] = "999.0.0"
        with self.assertRaises(ValueError):
            StructureInspectionSnapshotV1.model_validate(tampered)

    def test_internal_boundary_and_oversized_scope_are_source_bound(self) -> None:
        heading = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="4.1 Synthetic section",
        )
        short_one = _pdf_text_node(
            source_order=1,
            page_number=1,
            block_index=1,
            raw_text="Short synthetic context.",
        )
        prefix = "SENSITIVE-SYNTHETIC-PAYLOAD " * 45
        boundary_line = "4.1.4 Detailed verification"
        suffix = " synthetic continuation" * 35
        long_text = f"{prefix}\n{boundary_line}\n{suffix}"
        marker_start = long_text.index("4.1.4")
        long_node = _pdf_text_node(
            source_order=2,
            page_number=2,
            block_index=0,
            raw_text=long_text,
            bbox=(60.0, 80.0, 530.0, 680.0),
            pdf_text_lines=(
                PdfTextLineGeometry(
                    start_offset=marker_start,
                    end_offset=marker_start + len(boundary_line),
                    bbox=(72.0, 320.0, 310.0, 336.0),
                ),
            ),
        )
        short_two = _pdf_text_node(
            source_order=3,
            page_number=2,
            block_index=1,
            raw_text="Another short synthetic context.",
        )
        source = _source_snapshot(nodes=(heading, short_one, long_node, short_two))
        tree = _simple_tree(source)

        inspection = inspect_structure(source, tree)

        internal = _candidates(
            inspection,
            StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
        )
        oversized = _candidates(
            inspection,
            StructureInspectionCandidateCategory.OVERSIZED_UNSPLIT_SCOPE,
        )
        self.assertEqual(len(internal), 1)
        self.assertEqual(len(oversized), 1)
        self.assertEqual(internal[0].safe_marker, "4.1.4")
        self.assertEqual(internal[0].source_ref.source_id, long_node.source_node_id)
        self.assertEqual(internal[0].page_number, 2)
        self.assertEqual(internal[0].bbox, (72.0, 320.0, 310.0, 336.0))
        self.assertEqual(
            internal[0].disposition,
            StructureInspectionDisposition.ACTIONABLE,
        )
        unknown_reason = internal[0].model_dump(mode="json")
        unknown_reason["reason_codes"] = ["unregistered_reason_code"]
        with self.assertRaises(ValueError):
            StructureInspectionCandidateV1.model_validate(unknown_reason)
        self.assertEqual(
            oversized[0].structure_node_id,
            internal[0].structure_node_id,
        )
        self.assertEqual(oversized[0].start_offset, marker_start)
        metric = _metric_for_source(inspection, long_node.source_node_id)
        self.assertTrue(metric.length_outlier)
        self.assertEqual(metric.unconsumed_strong_boundary_count, 1)
        self.assertNotIn(
            "SENSITIVE-SYNTHETIC-PAYLOAD",
            inspection.model_dump_json(),
        )

    def test_long_scope_alone_does_not_become_an_oversized_error(self) -> None:
        heading = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="5.1 Synthetic section",
        )
        short_nodes = tuple(
            _pdf_text_node(
                source_order=index,
                page_number=1,
                block_index=index,
                raw_text=f"Short context {index}.",
            )
            for index in range(1, 4)
        )
        long_node = _pdf_text_node(
            source_order=4,
            page_number=2,
            block_index=0,
            raw_text="Long but structurally continuous synthetic prose. " * 35,
        )
        source = _source_snapshot(nodes=(heading, *short_nodes, long_node))

        inspection = inspect_structure(source, _simple_tree(source))

        metric = _metric_for_source(inspection, long_node.source_node_id)
        self.assertTrue(metric.length_outlier)
        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.OVERSIZED_UNSPLIT_SCOPE,
            )
        )

    def test_unconfirmed_implicit_cjk_root_is_detect_only_for_model_review(
        self,
    ) -> None:
        unresolved = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="22通用验证要求",
        )
        completed_sentence = _pdf_text_node(
            source_order=1,
            page_number=1,
            block_index=1,
            raw_text="30节点按照以下逻辑进行模式转换。",
        )
        source = _source_snapshot(nodes=(unresolved, completed_sentence))

        inspection = inspect_structure(source, _simple_tree(source))

        implicit = tuple(
            candidate
            for candidate in _candidates(
                inspection,
                StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
            )
            if candidate.safe_marker == "22"
        )
        self.assertEqual(len(implicit), 1)
        self.assertEqual(
            implicit[0].disposition,
            StructureInspectionDisposition.ACTIONABLE,
        )
        self.assertEqual(
            implicit[0].reason_codes,
            ("implicit_cjk_root_requires_corroboration",),
        )
        self.assertFalse(
            any(
                candidate.safe_marker == "30"
                for candidate in _candidates(
                    inspection,
                    StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
                )
            )
        )

    def test_standalone_appendices_are_detected_but_references_are_not(self) -> None:
        texts = (
            "附录 A",
            "Appendix B",
            "Annex C",
            "附件三",
            "见附录 A 获取补充信息。",
            "本附录仅用于说明。",
            "Appendix B describes a referenced method.",
            "Annex C contains a referenced method.",
        )
        nodes = tuple(
            _pdf_text_node(
                source_order=index,
                page_number=index + 1,
                block_index=0,
                raw_text=text,
            )
            for index, text in enumerate(texts)
        )
        source = _source_snapshot(nodes=nodes)

        inspection = inspect_structure(source, _simple_tree(source))

        appendix_candidates = _candidates(
            inspection,
            StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
        )
        self.assertEqual(
            {candidate.safe_marker for candidate in appendix_candidates},
            {"附录 A", "Appendix B", "Annex C", "附件三"},
        )
        self.assertTrue(
            all(
                candidate.disposition is StructureInspectionDisposition.ACTIONABLE
                for candidate in appendix_candidates
            )
        )
        referenced_source_ids = {node.source_node_id for node in nodes[4:]}
        self.assertFalse(
            any(
                candidate.source_ref.source_id in referenced_source_ids
                for candidate in appendix_candidates
            )
        )

    def test_visual_region_candidate_requires_grounded_independent_evidence(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        visual = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("A", (285.0, 82.0, 315.0, 102.0), None),
                ("（规范性）", (260.0, 112.0, 340.0, 130.0), None),
                (
                    "本附录规定一种通用试验方法。",
                    (90.0, 155.0, 510.0, 180.0),
                    None,
                ),
                ("表 A.1 试验参数", (220.0, 280.0, 380.0, 300.0), None),
            ),
        )
        source = _source_snapshot(
            nodes=(native,),
            visual_text_snapshots=(visual,),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        regions = _candidates(
            inspection,
            StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
        )
        self.assertEqual(len(regions), 1)
        candidate = regions[0]
        self.assertEqual(candidate.safe_marker, "A")
        self.assertEqual(candidate.page_number, 2)
        self.assertEqual(
            candidate.source_ref.kind,
            StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
        )
        self.assertEqual(
            candidate.confidence,
            StructureInspectionConfidence.MEDIUM,
        )
        self.assertEqual(
            candidate.disposition,
            StructureInspectionDisposition.ACTIONABLE,
        )
        self.assertIn(
            "visual_page_top_centered_region_marker",
            candidate.reason_codes,
        )
        self.assertIn(
            "nearby_document_region_qualifier",
            candidate.reason_codes,
        )
        self.assertIn(
            "nearby_document_region_self_reference",
            candidate.reason_codes,
        )
        self.assertIn(
            "downstream_lettered_region_designator",
            candidate.reason_codes,
        )
        self.assertEqual(
            inspection.visual_text_snapshot_ids,
            (visual.visual_text_snapshot_id,),
        )
        self.assertEqual(inspection.inspected_visual_observation_count, 4)

    def test_native_and_visual_region_evidence_form_one_logical_candidate(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=2,
            block_index=0,
            raw_text="附录 A",
            bbox=(270.0, 75.0, 330.0, 110.0),
        )
        visual = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("附录 A", (280.0, 82.0, 320.0, 102.0), None),
            ),
        )
        source = _source_snapshot(
            nodes=(native,),
            visual_text_snapshots=(visual,),
        )

        regions = _candidates(
            inspect_structure(source, _simple_tree(source)),
            StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
        )

        self.assertEqual(len(regions), 1)
        self.assertEqual(
            regions[0].source_ref.kind,
            StructureSourceRefKind.SOURCE_NODE,
        )
        self.assertEqual(
            regions[0].confidence,
            StructureInspectionConfidence.HIGH,
        )

    def test_low_confidence_visual_region_is_retained_but_suppressed(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        visual = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("附录 A", (270.0, 82.0, 330.0, 104.0), None),
            ),
            confidence=0.40,
        )
        source = _source_snapshot(
            nodes=(native,),
            visual_text_snapshots=(visual,),
        )

        regions = _candidates(
            inspect_structure(source, _simple_tree(source)),
            StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
        )

        self.assertEqual(len(regions), 1)
        self.assertEqual(
            regions[0].confidence,
            StructureInspectionConfidence.LOW,
        )
        self.assertEqual(
            regions[0].disposition,
            StructureInspectionDisposition.SUPPRESSED,
        )
        self.assertIn(
            "visual_recognition_confidence_below_policy",
            regions[0].reason_codes,
        )

    def test_ambiguous_duplicate_visual_renders_do_not_corroborate_each_other(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        first = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("A", (285.0, 82.0, 315.0, 102.0), None),
                ("本附录给出补充信息。", (90.0, 150.0, 510.0, 175.0), None),
            ),
            page_image_variant="first-render",
        )
        second = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("A", (285.0, 82.0, 315.0, 102.0), None),
                ("图 A.1 试验参数", (220.0, 280.0, 380.0, 300.0), None),
            ),
            page_image_variant="second-render",
        )
        source = _source_snapshot(
            nodes=(native,),
            visual_text_snapshots=tuple(
                sorted(
                    (first, second),
                    key=lambda snapshot: (
                        snapshot.page_number,
                        snapshot.provider_id,
                        snapshot.provider_version,
                        snapshot.visual_text_snapshot_id,
                    ),
                )
            ),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
            )
        )
        self.assertEqual(len(inspection.visual_text_snapshot_ids), 2)
        self.assertEqual(inspection.inspected_visual_observation_count, 4)

    def test_visual_letter_or_qualifier_alone_does_not_claim_a_region(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        visual = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("A", (285.0, 82.0, 315.0, 102.0), None),
                ("Unrelated title", (240.0, 145.0, 360.0, 165.0), None),
                ("（资料性）", (250.0, 420.0, 350.0, 440.0), None),
            ),
        )
        source = _source_snapshot(
            nodes=(native,),
            visual_text_snapshots=(visual,),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
            )
        )

    def test_visual_embedded_letter_and_inline_version_do_not_claim_a_region(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        visual = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("版本A说明", (270.0, 82.0, 330.0, 102.0), None),
                (
                    "本附录用于说明版本历史。",
                    (90.0, 155.0, 510.0, 180.0),
                    None,
                ),
                ("版本 A.1 已发布", (220.0, 280.0, 380.0, 300.0), None),
            ),
        )
        source = _source_snapshot(
            nodes=(native,),
            visual_text_snapshots=(visual,),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
            )
        )

    def test_visual_inline_version_is_not_a_lettered_region_designator(self) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        visual = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("A", (285.0, 82.0, 315.0, 102.0), None),
                ("版本 A.1 已发布", (220.0, 280.0, 380.0, 300.0), None),
            ),
        )
        source = _source_snapshot(
            nodes=(native,),
            visual_text_snapshots=(visual,),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
            )
        )

    def test_visual_standalone_letter_and_figure_designator_claim_a_region(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        visual = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("A", (285.0, 82.0, 315.0, 102.0), None),
                ("图A.1 试验布置", (220.0, 280.0, 380.0, 300.0), None),
            ),
        )
        source = _source_snapshot(
            nodes=(native,),
            visual_text_snapshots=(visual,),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        candidates = _candidates(
            inspection,
            StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].safe_marker, "A")
        self.assertIn(
            "downstream_lettered_region_designator",
            candidates[0].reason_codes,
        )

    def test_native_invisible_text_shape_cannot_infer_an_appendix_identity(
        self,
    ) -> None:
        corrupt_marker = _pdf_text_node(
            source_order=0,
            page_number=52,
            block_index=0,
            raw_text="CorruptGlyphs\nA",
            bbox=(280.0, 100.0, 330.0, 120.0),
        )
        self_reference = _pdf_text_node(
            source_order=1,
            page_number=52,
            block_index=1,
            raw_text="本附录规定一种试验方法。",
            bbox=(100.0, 160.0, 500.0, 180.0),
        )
        designator = _pdf_text_node(
            source_order=2,
            page_number=52,
            block_index=2,
            raw_text="表 A.1 试验参数",
            bbox=(220.0, 300.0, 380.0, 320.0),
        )
        source = _source_snapshot(
            nodes=(corrupt_marker, self_reference, designator),
            status=SourceIRStatus.PARTIAL,
        )

        inspection = inspect_structure(source, _simple_tree(source))

        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
            )
        )
        capture_gaps = _candidates(
            inspection,
            StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
        )
        self.assertTrue(
            any(candidate.source_ref is None for candidate in capture_gaps)
        )

    def test_partial_visual_region_evidence_remains_suppressed(self) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        visual = _visual_text_snapshot(
            page_number=2,
            observations=(
                ("B", (285.0, 82.0, 315.0, 102.0), None),
                ("本附录给出补充信息。", (90.0, 150.0, 510.0, 175.0), None),
            ),
        )
        source = _source_snapshot(
            nodes=(native,),
            status=SourceIRStatus.PARTIAL,
            visual_text_snapshots=(visual,),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        regions = _candidates(
            inspection,
            StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
        )
        self.assertEqual(len(regions), 1)
        self.assertEqual(
            regions[0].disposition,
            StructureInspectionDisposition.SUPPRESSED,
        )
        self.assertIn("source_ir_partial", regions[0].reason_codes)
        self.assertIn("source_ir_not_complete", regions[0].reason_codes)

    def test_partial_pdf_visual_capture_risk_is_reconciled_by_exact_text_snapshot(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        visual = _visual_text_snapshot(
            page_number=1,
            observations=(("视觉恢复正文", (90.0, 150.0, 510.0, 175.0), None),),
        )
        source = _source_snapshot(
            nodes=(native,),
            status=SourceIRStatus.PARTIAL,
            visual_text_snapshots=(visual,),
            diagnostics=(
                _pdf_diagnostic(
                    code="pdf_vector_text_with_low_native_unicode_coverage",
                    page_number=1,
                ),
                _pdf_diagnostic(
                    code="pdf_visual_text_observations_captured",
                    page_number=1,
                    severity=SourceDiagnosticSeverity.INFO,
                ),
                _pdf_diagnostic(
                    code="pdf_uniform_opaque_white_image_excluded",
                    page_number=1,
                    severity=SourceDiagnosticSeverity.INFO,
                ),
            ),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        self.assertEqual(source.status, SourceIRStatus.PARTIAL)
        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
            )
        )

    def test_user_forced_visual_page_reconciles_only_with_nonempty_text(self) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native audit text.",
        )
        exact_visual = _visual_text_snapshot(
            page_number=1,
            observations=(("OCR authoritative text", (90.0, 150.0, 510.0, 175.0), None),),
        )
        forced = _pdf_diagnostic(
            code="pdf_visual_text_user_forced",
            page_number=1,
            severity=SourceDiagnosticSeverity.INFO,
        )

        ready = _source_snapshot(
            nodes=(native,),
            status=SourceIRStatus.PARTIAL,
            visual_text_snapshots=(exact_visual,),
            diagnostics=(forced,),
        )
        missing = _source_snapshot(
            nodes=(native,),
            status=SourceIRStatus.PARTIAL,
            diagnostics=(forced,),
        )

        self.assertFalse(
            tuple(
                candidate
                for candidate in _candidates(
                    inspect_structure(ready, build_structure_tree(ready)),
                    StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
                )
                if candidate.disposition
                is StructureInspectionDisposition.ACTIONABLE
            )
        )
        missing_receipt = _serialize_structure_inspection(
            inspect_structure(missing, build_structure_tree(missing)),
            effective_revision=0,
        )
        self.assertEqual(missing_receipt["status"], "REQUIRES_REEXTRACTION")
        self.assertEqual(missing_receipt["counts"]["capture_gap_count"], 1)

    def test_evaluated_empty_visual_page_is_authoritative_capture_gap(self) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Native text must remain audit-only.",
        )
        empty_visual = _visual_text_snapshot(
            page_number=1,
            observations=(),
        )
        source = _source_snapshot(
            nodes=(native,),
            status=SourceIRStatus.COMPLETE,
            visual_text_snapshots=(empty_visual,),
        )

        tree = build_structure_tree(source)
        inspection = inspect_structure(source, tree)
        receipt = _serialize_structure_inspection(inspection, effective_revision=0)

        self.assertTrue(
            any(
                finding.code == "pdf_ocr_authority_text_unresolved"
                and finding.blocking
                for finding in tree.findings
            )
        )
        self.assertEqual(receipt["status"], "REQUIRES_REEXTRACTION")
        self.assertEqual(receipt["counts"]["capture_gap_count"], 1)
        self.assertNotIn("Native text must remain audit-only.", inspection.model_dump_json())

    def test_ocr_authority_native_table_without_exact_visual_cells_is_capture_gap(
        self,
    ) -> None:
        table = _pdf_full_width_row_table(
            source_order=0,
            page_number=1,
            table_index=0,
            bbox=(60.0, 120.0, 530.0, 320.0),
            rows=(("NATIVE-SECRET-CELL", (60.0, 120.0, 530.0, 180.0)),),
            column_count=2,
        )
        visual = _visual_text_snapshot(
            page_number=1,
            observations=(("OCR page text", (90.0, 80.0, 510.0, 105.0), None),),
        )
        source = _source_snapshot(
            nodes=(table,),
            status=SourceIRStatus.PARTIAL,
            visual_text_snapshots=(visual,),
            diagnostics=(
                _pdf_diagnostic(
                    code="pdf_visual_text_user_forced",
                    page_number=1,
                    severity=SourceDiagnosticSeverity.INFO,
                ),
            ),
        )

        tree = build_structure_tree(source)
        unresolved = next(
            finding
            for finding in tree.findings
            if finding.code == "pdf_ocr_authority_table_text_unresolved"
        )
        self.assertTrue(unresolved.blocking)
        inspection = inspect_structure(source, tree)
        receipt = _serialize_structure_inspection(inspection, effective_revision=0)

        self.assertEqual(receipt["status"], "REQUIRES_REEXTRACTION")
        self.assertEqual(receipt["counts"]["capture_gap_count"], 1)
        capture_gap = next(
            candidate
            for candidate in inspection.candidates
            if candidate.category
            is StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP
            and candidate.source_ref is None
        )
        self.assertIn(
            "ocr_authority_table_text_unresolved",
            capture_gap.reason_codes,
        )
        self.assertNotIn("NATIVE-SECRET-CELL", inspection.model_dump_json())

    def test_partial_pdf_visual_capture_reconciliation_fails_closed(self) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Synthetic native body.",
        )
        exact_visual = _visual_text_snapshot(
            page_number=1,
            observations=(("视觉恢复正文", (90.0, 150.0, 510.0, 175.0), None),),
        )
        wrong_page_visual = _visual_text_snapshot(
            page_number=2,
            observations=(("错误页面正文", (90.0, 150.0, 510.0, 175.0), None),),
        )
        empty_visual = _visual_text_snapshot(
            page_number=1,
            observations=(),
        )
        text_not_evaluated = _visual_text_snapshot(
            page_number=1,
            observations=(),
            evaluated_content_types=("FIGURE",),
        )
        risk_page_one = _pdf_diagnostic(
            code="pdf_vector_text_with_low_native_unicode_coverage",
            page_number=1,
        )
        cases = {
            "wrong_page": {
                "status": SourceIRStatus.PARTIAL,
                "visuals": (wrong_page_visual,),
                "diagnostics": (risk_page_one,),
            },
            "empty_snapshot": {
                "status": SourceIRStatus.PARTIAL,
                "visuals": (empty_visual,),
                "diagnostics": (risk_page_one,),
            },
            "text_not_evaluated": {
                "status": SourceIRStatus.PARTIAL,
                "visuals": (text_not_evaluated,),
                "diagnostics": (risk_page_one,),
            },
            "second_risk_page_failed": {
                "status": SourceIRStatus.PARTIAL,
                "visuals": (exact_visual,),
                "diagnostics": (
                    risk_page_one,
                    _pdf_diagnostic(
                        code="pdf_vector_text_with_low_native_unicode_coverage",
                        page_number=2,
                    ),
                    _pdf_diagnostic(
                        code="pdf_visual_text_provider_failed",
                        page_number=2,
                    ),
                ),
            },
            "unrelated_partial_warning": {
                "status": SourceIRStatus.PARTIAL,
                "visuals": (exact_visual,),
                "diagnostics": (
                    risk_page_one,
                    _pdf_diagnostic(
                        code="pdf_table_merge_geometry_unresolved",
                        page_number=1,
                    ),
                ),
            },
            "visual_capture_info_blocker": {
                "status": SourceIRStatus.PARTIAL,
                "visuals": (exact_visual,),
                "diagnostics": (
                    risk_page_one,
                    _pdf_diagnostic(
                        code="pdf_visual_text_page_out_of_scope",
                        page_number=1,
                        severity=SourceDiagnosticSeverity.INFO,
                    ),
                ),
            },
            "companion_diagnostic_wrong_page": {
                "status": SourceIRStatus.PARTIAL,
                "visuals": (exact_visual,),
                "diagnostics": (
                    risk_page_one,
                    _pdf_diagnostic(
                        code="pdf_invisible_text_span_detected",
                        page_number=2,
                    ),
                ),
            },
            "failed_source": {
                "status": SourceIRStatus.FAILED,
                "visuals": (exact_visual,),
                "diagnostics": (risk_page_one,),
            },
        }

        for name, case in cases.items():
            with self.subTest(name=name):
                source = _source_snapshot(
                    nodes=(native,),
                    status=case["status"],
                    visual_text_snapshots=case["visuals"],
                    diagnostics=case["diagnostics"],
                )

                inspection = inspect_structure(source, _simple_tree(source))

                self.assertTrue(
                    _candidates(
                        inspection,
                        StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
                    )
                )

    def test_full_page_hidden_text_risk_reconciles_its_exact_page_companion(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Hidden native layer.",
        )
        visual = _visual_text_snapshot(
            page_number=1,
            observations=(("可见页面正文", (90.0, 150.0, 510.0, 175.0), None),),
            evaluated_content_types=(
                "TEXT",
                "TABLE",
                "FIGURE",
                "HEADER",
                "FOOTER",
                "WATERMARK",
            ),
        )
        source = _source_snapshot(
            nodes=(native,),
            status=SourceIRStatus.PARTIAL,
            visual_text_snapshots=(visual,),
            diagnostics=(
                _pdf_diagnostic(
                    code="pdf_invisible_text_span_detected",
                    page_number=1,
                ),
                _pdf_diagnostic(
                    code="pdf_full_page_image_with_invisible_text_layer",
                    page_number=1,
                ),
                _pdf_diagnostic(
                    code="pdf_visual_text_observations_captured",
                    page_number=1,
                    severity=SourceDiagnosticSeverity.INFO,
                ),
            ),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
            )
        )

    def test_full_page_scan_text_only_ocr_does_not_claim_figure_completion(
        self,
    ) -> None:
        native = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Hidden native layer.",
        )
        visual = _visual_text_snapshot(
            page_number=1,
            observations=(("OCR recovered text", (90.0, 150.0, 510.0, 175.0), None),),
        )
        source = _source_snapshot(
            nodes=(native,),
            status=SourceIRStatus.PARTIAL,
            visual_text_snapshots=(visual,),
            diagnostics=(
                _pdf_diagnostic(
                    code="pdf_full_page_image_with_invisible_text_layer",
                    page_number=1,
                ),
                _pdf_diagnostic(
                    code="pdf_visual_text_observations_captured",
                    page_number=1,
                    severity=SourceDiagnosticSeverity.INFO,
                ),
                _pdf_diagnostic(
                    code="pdf_visual_figure_content_not_evaluated",
                    page_number=1,
                ),
            ),
        )

        inspection = inspect_structure(source, _simple_tree(source))

        self.assertTrue(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
            )
        )

    def test_document_region_alias_owned_by_existing_section_is_suppressed(
        self,
    ) -> None:
        represented = _pdf_text_node(
            source_order=0,
            page_number=10,
            block_index=0,
            raw_text="10.1\n附录 1",
        )
        missing = _pdf_text_node(
            source_order=1,
            page_number=11,
            block_index=0,
            raw_text="附录 2",
        )
        source = _source_snapshot(nodes=(represented, missing))
        tree = _simple_tree(
            source,
            aliased_region_labels_by_source_node_id={
                represented.source_node_id: "10.1",
            },
        )

        inspection = inspect_structure(source, tree)

        by_marker = {
            candidate.safe_marker: candidate
            for candidate in _candidates(
                inspection,
                StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
            )
        }
        self.assertEqual(
            by_marker["附录 1"].disposition,
            StructureInspectionDisposition.SUPPRESSED,
        )
        self.assertIn(
            "boundary_already_materialized_by_structure_owner",
            by_marker["附录 1"].reason_codes,
        )
        self.assertEqual(
            by_marker["附录 2"].disposition,
            StructureInspectionDisposition.ACTIONABLE,
        )

    def test_repeated_marker_inside_first_materialized_region_is_suppressed(
        self,
    ) -> None:
        anchor = _pdf_text_node(
            source_order=0,
            page_number=15,
            block_index=0,
            raw_text="附件 4",
        )
        repeat_same_page = _pdf_text_node(
            source_order=1,
            page_number=15,
            block_index=1,
            raw_text="附件4",
        )
        repeat_next_page = _pdf_text_node(
            source_order=2,
            page_number=16,
            block_index=0,
            raw_text="附件 4",
        )
        different_marker = _pdf_text_node(
            source_order=3,
            page_number=16,
            block_index=1,
            raw_text="附件 5",
        )
        source = _source_snapshot(
            nodes=(
                anchor,
                repeat_same_page,
                repeat_next_page,
                different_marker,
            )
        )
        tree = _simple_tree(
            source,
            materialized_region_source_node_ids=frozenset({anchor.source_node_id}),
            structure_parent_source_node_ids={
                repeat_same_page.source_node_id: anchor.source_node_id,
                repeat_next_page.source_node_id: anchor.source_node_id,
                different_marker.source_node_id: anchor.source_node_id,
            },
        )

        inspection = inspect_structure(source, tree)

        regions = _candidates(
            inspection,
            StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
        )
        anchor_candidate = next(
            candidate
            for candidate in regions
            if candidate.source_ref is not None
            and candidate.source_ref.source_id == anchor.source_node_id
        )
        repeated_candidates = tuple(
            candidate
            for candidate in regions
            if candidate.safe_marker == "附件 4" and candidate is not anchor_candidate
        )
        different_candidate = next(
            candidate for candidate in regions if candidate.safe_marker == "附件 5"
        )
        self.assertEqual(
            anchor_candidate.disposition,
            StructureInspectionDisposition.SUPPRESSED,
        )
        self.assertTrue(
            all(
                candidate.disposition is StructureInspectionDisposition.SUPPRESSED
                for candidate in repeated_candidates
            )
        )
        self.assertTrue(
            all(
                "repeated_document_region_marker_within_materialized_region"
                in candidate.reason_codes
                for candidate in repeated_candidates
            )
        )
        self.assertEqual(
            different_candidate.disposition,
            StructureInspectionDisposition.ACTIONABLE,
        )

    def test_table_toc_and_furniture_markers_are_retained_as_suppressed(self) -> None:
        header = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="4.1.4 Running header",
            kind=SourceNodeKind.HEADER,
        )
        toc = _pdf_text_node(
            source_order=1,
            page_number=1,
            block_index=1,
            raw_text="Appendix B",
            kind=SourceNodeKind.TOC_ENTRY,
        )
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            table_index=0,
            bbox=(60.0, 120.0, 530.0, 320.0),
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
            raw_text="附录 A\n4.2.1 Tabular value",
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=2,
                table_index=0,
                row_index=0,
                column_index=0,
            ),
        )
        table = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_HASH,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                kind=SourceNodeKind.TABLE,
                native_locator=table_locator,
            ),
            kind=SourceNodeKind.TABLE,
            source_order=2,
            raw_text="",
            native_locator=table_locator,
            table_payload=TableIR(
                table_id=table_id,
                row_count=1,
                column_count=1,
                cells=(cell,),
            ),
        )
        source = _source_snapshot(nodes=(header, toc, table))

        inspection = inspect_structure(source, _simple_tree(source))

        boundary_candidates = tuple(
            candidate
            for candidate in inspection.candidates
            if candidate.category
            in {
                StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
                StructureInspectionCandidateCategory.DOCUMENT_REGION_BOUNDARY,
            }
        )
        self.assertEqual(len(boundary_candidates), 4)
        self.assertTrue(
            all(
                candidate.disposition is StructureInspectionDisposition.SUPPRESSED
                for candidate in boundary_candidates
            )
        )
        table_candidates = tuple(
            candidate
            for candidate in boundary_candidates
            if candidate.source_ref.kind is StructureSourceRefKind.TABLE_CELL
        )
        self.assertEqual(len(table_candidates), 2)
        self.assertTrue(
            all(
                "inside_table_content" in candidate.reason_codes
                for candidate in table_candidates
            )
        )
        toc_candidate = next(
            candidate
            for candidate in boundary_candidates
            if candidate.source_ref.source_id == toc.source_node_id
        )
        self.assertIn("inside_table_of_contents", toc_candidate.reason_codes)
        header_candidate = next(
            candidate
            for candidate in boundary_candidates
            if candidate.source_ref.source_id == header.source_node_id
        )
        self.assertIn("inside_page_furniture", header_candidate.reason_codes)

    def test_folded_pdf_table_text_reuses_resolver_negative_evidence(self) -> None:
        folded_text = _pdf_text_node(
            source_order=0,
            page_number=2,
            block_index=0,
            raw_text="Synthetic scale\n6.5",
            bbox=(80.0, 140.0, 180.0, 170.0),
        )
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            table_index=0,
            bbox=(60.0, 120.0, 530.0, 320.0),
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            native_locator=table_locator,
        )
        table = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_HASH,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                kind=SourceNodeKind.TABLE,
                native_locator=table_locator,
            ),
            kind=SourceNodeKind.TABLE,
            source_order=1,
            native_locator=table_locator,
            table_payload=TableIR(
                table_id=table_id,
                row_count=1,
                column_count=1,
                cells=(
                    TableCellIR(
                        cell_id=stable_cell_id(table_id, 0, 0),
                        row=0,
                        column=0,
                        raw_text="Synthetic scale\n6.5",
                        native_locator=NativeLocator(
                            format=NativeDocumentFormat.PDF,
                            page_number=2,
                            table_index=0,
                            row_index=0,
                            column_index=0,
                            bbox=(60.0, 120.0, 530.0, 320.0),
                        ),
                    ),
                ),
            ),
        )
        source = _source_snapshot(nodes=(folded_text, table))

        inspection = inspect_structure(source, _simple_tree(source))

        folded_candidate = next(
            candidate
            for candidate in _candidates(
                inspection,
                StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
            )
            if candidate.source_ref is not None
            and candidate.source_ref.source_id == folded_text.source_node_id
        )
        self.assertEqual(folded_candidate.safe_marker, "6.5")
        self.assertEqual(
            folded_candidate.disposition,
            StructureInspectionDisposition.SUPPRESSED,
        )
        self.assertIn(
            "folded_pdf_table_text",
            folded_candidate.reason_codes,
        )

    def test_bare_numeric_axis_values_are_not_actionable_boundaries(self) -> None:
        axis = _pdf_text_node(
            source_order=0,
            page_number=3,
            block_index=0,
            raw_text="Synthetic axis\n6.5\n8.5\n16.5\n1.0 V",
            bbox=(70.0, 100.0, 500.0, 180.0),
        )
        source = _source_snapshot(nodes=(axis,))

        inspection = inspect_structure(source, _simple_tree(source))

        candidates = _candidates(
            inspection,
            StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
        )
        self.assertEqual(
            {candidate.safe_marker for candidate in candidates},
            {"1.0", "6.5", "8.5", "16.5"},
        )
        self.assertTrue(
            all(
                candidate.disposition is StructureInspectionDisposition.SUPPRESSED
                for candidate in candidates
            )
        )
        self.assertTrue(
            all(
                "numeric_scale_or_measurement" in candidate.reason_codes
                for candidate in candidates
            )
        )

    def test_wrapped_references_and_inline_numeric_peers_are_suppressed(self) -> None:
        wrapped = _pdf_text_node(
            source_order=0,
            page_number=4,
            block_index=0,
            raw_text=(
                "Synthetic operating mode\n"
                "1.2 continues the same sentence.\n"
                "Completed context.\n"
                "4.1.4 Genuine internal heading"
            ),
        )
        english = _pdf_text_node(
            source_order=1,
            page_number=5,
            block_index=0,
            raw_text=(
                "Completed English context.\n"
                "2.1 to determine a synthetic stability time."
            ),
        )
        inline_peers = _pdf_text_node(
            source_order=2,
            page_number=6,
            block_index=0,
            raw_text=(
                "Synthetic options.\n"
                "1.0 first operating range. 2.0 second operating range."
            ),
        )
        source = _source_snapshot(nodes=(wrapped, english, inline_peers))

        inspection = inspect_structure(source, _simple_tree(source))

        by_marker = {
            candidate.safe_marker: candidate
            for candidate in _candidates(
                inspection,
                StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
            )
        }
        self.assertEqual(
            by_marker["4.1.4"].disposition,
            StructureInspectionDisposition.ACTIONABLE,
        )
        self.assertIn(
            "wrapped_paragraph_continuation",
            by_marker["1.2"].reason_codes,
        )
        self.assertIn(
            "english_sentence_continuation",
            by_marker["2.1"].reason_codes,
        )
        self.assertIn(
            "inline_numeric_peer_sequence",
            by_marker["1.0"].reason_codes,
        )
        self.assertTrue(
            all(
                by_marker[marker].disposition
                is StructureInspectionDisposition.SUPPRESSED
                for marker in {"1.0", "1.2", "2.1"}
            )
        )

    def test_coherent_single_dot_roots_extend_into_full_width_table_headings(
        self,
    ) -> None:
        anchor = _pdf_text_node(
            source_order=0,
            page_number=7,
            block_index=0,
            raw_text="1. Reliability goals.",
            bbox=(54.0, 80.0, 530.0, 100.0),
        )
        first_child = _pdf_text_node(
            source_order=1,
            page_number=7,
            block_index=1,
            raw_text="1.1 Climate evidence",
            bbox=(54.0, 105.0, 530.0, 125.0),
        )
        second_child = _pdf_text_node(
            source_order=2,
            page_number=7,
            block_index=2,
            raw_text="1.2 Mechanical evidence",
            bbox=(54.0, 130.0, 530.0, 150.0),
        )
        second_root = _pdf_text_node(
            source_order=3,
            page_number=7,
            block_index=3,
            raw_text="2. Validation goals",
            bbox=(54.0, 155.0, 530.0, 175.0),
        )
        table_text_value = "3. Sample requirements\n4. Integration validation"
        table_text = _pdf_text_node(
            source_order=4,
            page_number=7,
            block_index=4,
            raw_text=table_text_value,
            bbox=(54.0, 200.0, 530.0, 260.0),
            pdf_text_lines=(
                PdfTextLineGeometry(
                    start_offset=0,
                    end_offset=len("3. Sample requirements"),
                    bbox=(55.0, 205.0, 190.0, 218.0),
                ),
                PdfTextLineGeometry(
                    start_offset=table_text_value.index("4."),
                    end_offset=len(table_text_value),
                    bbox=(55.0, 235.0, 200.0, 248.0),
                ),
            ),
        )
        table = _pdf_full_width_row_table(
            source_order=5,
            page_number=7,
            table_index=0,
            bbox=(54.0, 200.0, 530.0, 260.0),
            rows=(
                ("3. Sample requirements", (54.0, 200.0, 530.0, 230.0)),
                ("4. Integration validation", (54.0, 230.0, 530.0, 260.0)),
            ),
        )
        source = _source_snapshot(
            nodes=(
                anchor,
                first_child,
                second_child,
                second_root,
                table_text,
                table,
            )
        )
        tree = _simple_tree(
            source,
            aliased_region_labels_by_source_node_id={
                anchor.source_node_id: "9",
            },
            structure_parent_source_node_ids={
                first_child.source_node_id: anchor.source_node_id,
                second_child.source_node_id: anchor.source_node_id,
                second_root.source_node_id: anchor.source_node_id,
                table_text.source_node_id: anchor.source_node_id,
                table.source_node_id: anchor.source_node_id,
            },
        )

        inspection = inspect_structure(source, tree)

        sequence_candidates = tuple(
            candidate
            for candidate in _candidates(
                inspection,
                StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
            )
            if "coherent_single_dot_root_sequence" in candidate.reason_codes
        )
        self.assertEqual(
            [candidate.safe_marker for candidate in sequence_candidates],
            ["1", "2", "3", "4"],
        )
        self.assertTrue(
            all(
                candidate.disposition is StructureInspectionDisposition.ACTIONABLE
                for candidate in sequence_candidates
            )
        )
        self.assertTrue(
            all(
                "full_width_table_boundary_heading" in candidate.reason_codes
                for candidate in sequence_candidates[2:]
            )
        )
        self.assertTrue(
            all(
                candidate.structure_node_id == sequence_candidates[0].structure_node_id
                for candidate in sequence_candidates
            )
        )
        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.OVERSIZED_UNSPLIT_SCOPE,
            )
        )

    def test_competitive_single_dot_negatives_do_not_prove_a_root_sequence(
        self,
    ) -> None:
        ordinary_list = _pdf_text_node(
            source_order=0,
            page_number=8,
            block_index=0,
            raw_text=("1. Collect evidence.\n2. Review evidence.\n3. Close the task."),
        )
        measurement_and_reference = _pdf_text_node(
            source_order=0,
            page_number=9,
            block_index=0,
            raw_text=(
                "1. 5 mm\n"
                "1.1 Climate evidence\n"
                "Complete.\n"
                "1.2 Mechanical evidence\n"
                "Complete.\n"
                "2. See section 4\n"
                "3.2 operating mode"
            ),
        )
        toc = _pdf_text_node(
            source_order=0,
            page_number=10,
            block_index=0,
            raw_text=(
                "1. Overview.\n"
                "1.1 Scope\n"
                "Complete.\n"
                "1.2 Terms\n"
                "Complete.\n"
                "2. Requirements"
            ),
            kind=SourceNodeKind.TOC_ENTRY,
        )
        furniture = _pdf_text_node(
            source_order=0,
            page_number=11,
            block_index=0,
            raw_text=(
                "1. Document family.\n"
                "1.1 Product\n"
                "Complete.\n"
                "1.2 Variant\n"
                "Complete.\n"
                "2. Confidential"
            ),
            kind=SourceNodeKind.HEADER,
        )
        table_text = _pdf_text_node(
            source_order=0,
            page_number=12,
            block_index=0,
            raw_text=(
                "1. Table list.\n"
                "1.1 First row\n"
                "Complete.\n"
                "1.2 Second row\n"
                "Complete.\n"
                "2. Table list continuation"
            ),
            bbox=(54.0, 200.0, 530.0, 260.0),
            pdf_text_lines=(
                PdfTextLineGeometry(
                    start_offset=0,
                    end_offset=len("1. Table list."),
                    bbox=(55.0, 205.0, 160.0, 215.0),
                ),
                PdfTextLineGeometry(
                    start_offset=(
                        len(
                            "1. Table list.\n"
                            "1.1 First row\n"
                            "Complete.\n"
                            "1.2 Second row\n"
                            "Complete.\n"
                        )
                    ),
                    end_offset=len(
                        "1. Table list.\n"
                        "1.1 First row\n"
                        "Complete.\n"
                        "1.2 Second row\n"
                        "Complete.\n"
                        "2. Table list continuation"
                    ),
                    bbox=(55.0, 245.0, 220.0, 255.0),
                ),
            ),
        )
        table = _pdf_full_width_row_table(
            source_order=1,
            page_number=12,
            table_index=0,
            bbox=(54.0, 200.0, 530.0, 260.0),
            rows=(
                (
                    (
                        "1. Table list.\n"
                        "1.1 First row\n"
                        "Complete.\n"
                        "1.2 Second row\n"
                        "Complete."
                    ),
                    (54.0, 200.0, 530.0, 240.0),
                ),
                (
                    "2. Table list continuation",
                    (54.0, 240.0, 530.0, 260.0),
                ),
            ),
        )
        cases = (
            _source_snapshot(nodes=(ordinary_list,)),
            _source_snapshot(nodes=(measurement_and_reference,)),
            _source_snapshot(nodes=(toc,)),
            _source_snapshot(nodes=(furniture,)),
            _source_snapshot(nodes=(table_text, table)),
        )

        for source in cases:
            with self.subTest(source_ir_id=source.source_ir_id):
                inspection = inspect_structure(source, _simple_tree(source))
                self.assertFalse(
                    tuple(
                        candidate
                        for candidate in _candidates(
                            inspection,
                            (
                                StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                            ),
                        )
                        if "coherent_single_dot_root_sequence" in candidate.reason_codes
                    )
                )

    def test_coherent_alphabetic_parenthesized_sequence_is_source_bound(
        self,
    ) -> None:
        root = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="4 Synthetic root",
        )
        section = _pdf_text_node(
            source_order=1,
            page_number=1,
            block_index=1,
            raw_text="4.2 Synthetic scope",
        )
        first = _pdf_text_node(
            source_order=2,
            page_number=1,
            block_index=2,
            raw_text="a) First verification item",
            bbox=(90.0, 120.0, 510.0, 138.0),
        )
        second = _pdf_text_node(
            source_order=3,
            page_number=1,
            block_index=3,
            raw_text="b） Second verification item",
            bbox=(92.0, 145.0, 510.0, 163.0),
        )
        third = _pdf_text_node(
            source_order=4,
            page_number=2,
            block_index=0,
            raw_text="c) Third verification item",
            bbox=(91.0, 80.0, 510.0, 98.0),
        )
        source = _source_snapshot(nodes=(root, section, first, second, third))
        tree = _simple_tree(
            source,
            aliased_region_labels_by_source_node_id={
                root.source_node_id: "4",
            },
            structure_parent_source_node_ids={
                section.source_node_id: root.source_node_id,
                first.source_node_id: section.source_node_id,
                second.source_node_id: section.source_node_id,
                third.source_node_id: section.source_node_id,
            },
        )

        inspection = inspect_structure(source, tree)

        candidates = tuple(
            candidate
            for candidate in _candidates(
                inspection,
                StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
            )
            if "unconsumed_alphabetic_parenthesized_boundary" in candidate.reason_codes
        )
        self.assertEqual(
            [candidate.safe_marker for candidate in candidates],
            ["a)", "b)", "c)"],
        )
        self.assertTrue(
            all(
                candidate.disposition is StructureInspectionDisposition.ACTIONABLE
                for candidate in candidates
            )
        )
        self.assertTrue(
            all(
                candidate.structure_node_id == candidates[0].structure_node_id
                for candidate in candidates
            )
        )
        self.assertEqual(candidates[-1].page_number, 2)

    def test_embedded_parenthesized_peer_sequences_are_not_silent(self) -> None:
        for family, raw_text, expected_markers, reason_code in (
            (
                "numeric",
                "Scope context\n1) First item\n2) Second item\n3) Third item",
                ["1)", "2)", "3)"],
                "embedded_numeric_parenthesized_peer_sequence",
            ),
            (
                "alphabetic",
                "Scope context\na) First item\nb) Second item\nc) Third item",
                ["a)", "b)", "c)"],
                "embedded_alphabetic_parenthesized_peer_sequence",
            ),
        ):
            with self.subTest(family=family):
                node = _pdf_text_node(
                    source_order=0,
                    page_number=1,
                    block_index=0,
                    raw_text=raw_text,
                )
                source = _source_snapshot(nodes=(node,))

                inspection = inspect_structure(source, _simple_tree(source))

                candidates = tuple(
                    candidate
                    for candidate in _candidates(
                        inspection,
                        (
                            StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                        ),
                    )
                    if reason_code in candidate.reason_codes
                )
                self.assertEqual(
                    [candidate.safe_marker for candidate in candidates],
                    expected_markers,
                )
                self.assertTrue(
                    all(
                        candidate.disposition
                        is StructureInspectionDisposition.ACTIONABLE
                        for candidate in candidates
                    )
                )
                self.assertEqual(
                    [
                        raw_text[candidate.start_offset : candidate.end_offset]
                        for candidate in candidates
                    ],
                    expected_markers,
                )

    def test_embedded_continuation_runs_remain_detectable_without_write_authority(
        self,
    ) -> None:
        for raw_text, expected_markers in (
            ("Context\n4) Fourth\n5) Fifth\n6) Sixth", ["4)", "5)", "6)"]),
            ("Context\nb) Second\nc) Third\nd) Fourth", ["b)", "c)", "d)"]),
            ("Context\n7) Seventh\n8) Eighth", ["7)", "8)"]),
        ):
            with self.subTest(raw_text=raw_text):
                node = _pdf_text_node(
                    source_order=0,
                    page_number=1,
                    block_index=0,
                    raw_text=raw_text,
                )
                source = _source_snapshot(nodes=(node,))

                candidates = tuple(
                    candidate
                    for candidate in _candidates(
                        inspect_structure(source, _simple_tree(source)),
                        (
                            StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                        ),
                    )
                    if "embedded_peer_sequence_not_root_anchored"
                    in candidate.reason_codes
                )

                self.assertEqual(
                    [candidate.safe_marker for candidate in candidates],
                    expected_markers,
                )
                self.assertTrue(
                    all(
                        candidate.confidence
                        is StructureInspectionConfidence.MEDIUM
                        for candidate in candidates
                    )
                )
                self.assertTrue(
                    all(
                        candidate.disposition
                        is StructureInspectionDisposition.ACTIONABLE
                        for candidate in candidates
                    )
                )
                self.assertEqual(
                    [
                        raw_text[candidate.start_offset : candidate.end_offset]
                        for candidate in candidates
                    ],
                    expected_markers,
                )

    def test_alphabetic_list_requires_a_coherent_local_sequence(self) -> None:
        isolated = _source_snapshot(
            nodes=(
                _pdf_text_node(
                    source_order=0,
                    page_number=1,
                    block_index=0,
                    raw_text="a) Isolated letter item",
                ),
            )
        )
        nonconsecutive = _source_snapshot(
            nodes=(
                _pdf_text_node(
                    source_order=0,
                    page_number=1,
                    block_index=0,
                    raw_text="a) First item",
                ),
                _pdf_text_node(
                    source_order=1,
                    page_number=1,
                    block_index=1,
                    raw_text="c) Third item",
                ),
            )
        )
        mixed_case = _source_snapshot(
            nodes=(
                _pdf_text_node(
                    source_order=0,
                    page_number=1,
                    block_index=0,
                    raw_text="a) Lowercase item",
                ),
                _pdf_text_node(
                    source_order=1,
                    page_number=1,
                    block_index=1,
                    raw_text="B) Uppercase item",
                ),
            )
        )
        distant_pages = _source_snapshot(
            nodes=(
                _pdf_text_node(
                    source_order=0,
                    page_number=1,
                    block_index=0,
                    raw_text="a) First item",
                ),
                _pdf_text_node(
                    source_order=1,
                    page_number=3,
                    block_index=0,
                    raw_text="b) Distant item",
                ),
            )
        )
        distant_source_order = _source_snapshot(
            nodes=(
                _pdf_text_node(
                    source_order=0,
                    page_number=1,
                    block_index=0,
                    raw_text="a) First item",
                ),
                _pdf_text_node(
                    source_order=1,
                    page_number=1,
                    block_index=1,
                    raw_text="Intervening context one.",
                ),
                _pdf_text_node(
                    source_order=2,
                    page_number=1,
                    block_index=2,
                    raw_text="Intervening context two.",
                ),
                _pdf_text_node(
                    source_order=3,
                    page_number=1,
                    block_index=3,
                    raw_text="b) Distant source item",
                ),
            )
        )
        misaligned = _source_snapshot(
            nodes=(
                _pdf_text_node(
                    source_order=0,
                    page_number=1,
                    block_index=0,
                    raw_text="a) First item",
                    bbox=(70.0, 80.0, 510.0, 98.0),
                ),
                _pdf_text_node(
                    source_order=1,
                    page_number=1,
                    block_index=1,
                    raw_text="b) Shifted item",
                    bbox=(110.0, 105.0, 510.0, 123.0),
                ),
            )
        )

        for source in (
            isolated,
            nonconsecutive,
            mixed_case,
            distant_pages,
            distant_source_order,
            misaligned,
        ):
            with self.subTest(source_ir_id=source.source_ir_id, nodes=source.nodes):
                inspection = inspect_structure(source, _simple_tree(source))
                self.assertFalse(
                    tuple(
                        candidate
                        for candidate in _candidates(
                            inspection,
                            (
                                StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                            ),
                        )
                        if "unconsumed_alphabetic_parenthesized_boundary"
                        in candidate.reason_codes
                    )
                )

    def test_materialized_alphabetic_clauses_are_not_actionable(self) -> None:
        items = tuple(
            _pdf_text_node(
                source_order=index,
                page_number=1,
                block_index=index,
                raw_text=f"{letter}) Materialized item",
            )
            for index, letter in enumerate(("a", "b", "c"))
        )
        source = _source_snapshot(nodes=items)
        tree = _simple_tree(
            source,
            list_ordinals_by_source_node_id={
                node.source_node_id: index for index, node in enumerate(items, start=1)
            },
        )

        inspection = inspect_structure(source, tree)

        self.assertFalse(
            tuple(
                candidate
                for candidate in _candidates(
                    inspection,
                    StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
                )
                if "alphabetic_parenthesized" in "_".join(candidate.reason_codes)
            )
        )

    def test_incomplete_source_suppresses_alphabetic_list_candidates(self) -> None:
        for status, status_reason in (
            (SourceIRStatus.PARTIAL, "source_ir_partial"),
            (SourceIRStatus.FAILED, "source_ir_failed"),
        ):
            with self.subTest(status=status):
                items = tuple(
                    _pdf_text_node(
                        source_order=index,
                        page_number=1,
                        block_index=index,
                        raw_text=f"{letter}) Incomplete item",
                    )
                    for index, letter in enumerate(("A", "B"))
                )
                source = _source_snapshot(nodes=items, status=status)

                inspection = inspect_structure(source, _simple_tree(source))

                candidates = tuple(
                    candidate
                    for candidate in _candidates(
                        inspection,
                        (
                            StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY
                        ),
                    )
                    if "alphabetic_parenthesized_sequence_detected"
                    in candidate.reason_codes
                )
                self.assertEqual(
                    [candidate.safe_marker for candidate in candidates],
                    ["A)", "B)"],
                )
                self.assertTrue(
                    all(
                        candidate.disposition
                        is StructureInspectionDisposition.SUPPRESSED
                        and status_reason in candidate.reason_codes
                        and "source_ir_not_complete" in candidate.reason_codes
                        for candidate in candidates
                    )
                )
                self.assertFalse(
                    _candidates(
                        inspection,
                        StructureInspectionCandidateCategory.OVERSIZED_UNSPLIT_SCOPE,
                    )
                )

    def test_explicit_outline_parent_mismatch_is_source_bound(self) -> None:
        source, tree, misplaced = _outline_parent_mismatch_fixture()

        inspection = inspect_structure(source, tree)

        mismatches = _candidates(
            inspection,
            StructureInspectionCandidateCategory.OUTLINE_PARENT_MISMATCH,
        )
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].safe_marker, "4.8")
        self.assertEqual(
            mismatches[0].source_ref.source_id,
            misplaced.source_node_id,
        )
        self.assertEqual(
            mismatches[0].disposition,
            StructureInspectionDisposition.ACTIONABLE,
        )
        self.assertEqual(
            mismatches[0].reason_codes,
            ("outline_parent_label_mismatch",),
        )
        self.assertEqual(mismatches[0].page_number, 5)
        self.assertEqual(mismatches[0].start_offset, 0)
        self.assertEqual(mismatches[0].end_offset, len("4.8"))

    def test_parenthesized_outline_child_under_exact_clause_is_valid(self) -> None:
        root = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="6 Synthetic root",
        )
        clause = _pdf_text_node(
            source_order=1,
            page_number=1,
            block_index=1,
            raw_text="6） Parenthesized outline",
        )
        child = _pdf_text_node(
            source_order=2,
            page_number=1,
            block_index=2,
            raw_text="6.1） Direct outline child",
        )
        grandchild = _pdf_text_node(
            source_order=3,
            page_number=1,
            block_index=3,
            raw_text="6.1.1） Nested outline child",
        )
        source = _source_snapshot(nodes=(root, clause, child, grandchild))
        tree = _simple_tree(
            source,
            aliased_region_labels_by_source_node_id={
                root.source_node_id: "6",
            },
            list_ordinals_by_source_node_id={
                clause.source_node_id: 6,
            },
            structure_parent_source_node_ids={
                clause.source_node_id: root.source_node_id,
                child.source_node_id: clause.source_node_id,
                grandchild.source_node_id: child.source_node_id,
            },
        )

        inspection = inspect_structure(source, tree)

        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.OUTLINE_PARENT_MISMATCH,
            )
        )

    def test_missing_outline_parent_is_reported_but_logical_root_is_not(self) -> None:
        logical_root = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="8.0 Logical root",
        )
        orphan = _pdf_text_node(
            source_order=1,
            page_number=2,
            block_index=0,
            raw_text="8.2 Orphan section",
        )
        source = _source_snapshot(nodes=(logical_root, orphan))

        inspection = inspect_structure(source, _simple_tree(source))

        mismatches = _candidates(
            inspection,
            StructureInspectionCandidateCategory.OUTLINE_PARENT_MISMATCH,
        )
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].safe_marker, "8.2")
        self.assertEqual(
            mismatches[0].reason_codes,
            ("outline_parent_missing",),
        )

    def test_incomplete_source_suppresses_outline_parent_mismatch(self) -> None:
        for status, status_reason in (
            (SourceIRStatus.PARTIAL, "source_ir_partial"),
            (SourceIRStatus.FAILED, "source_ir_failed"),
        ):
            with self.subTest(status=status):
                source, tree, _misplaced = _outline_parent_mismatch_fixture(
                    status=status
                )

                inspection = inspect_structure(source, tree)

                mismatches = _candidates(
                    inspection,
                    StructureInspectionCandidateCategory.OUTLINE_PARENT_MISMATCH,
                )
                self.assertEqual(len(mismatches), 1)
                self.assertEqual(
                    mismatches[0].disposition,
                    StructureInspectionDisposition.SUPPRESSED,
                )
                self.assertIn("source_ir_not_complete", mismatches[0].reason_codes)
                self.assertIn(status_reason, mismatches[0].reason_codes)
                self.assertTrue(
                    _candidates(
                        inspection,
                        StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
                    )
                )

    def test_partial_source_without_visual_facts_blocks_structure_candidates(
        self,
    ) -> None:
        prefix = "Synthetic long scope " * 70
        long_node = _pdf_text_node(
            source_order=0,
            page_number=4,
            block_index=0,
            raw_text=f"{prefix}\n4.1.4 Hidden subsection\n{'tail ' * 180}",
            bbox=(60.0, 80.0, 530.0, 680.0),
        )
        peer = _pdf_text_node(
            source_order=1,
            page_number=5,
            block_index=0,
            raw_text="Short peer content.",
        )
        source = _source_snapshot(
            nodes=(long_node, peer),
            status=SourceIRStatus.PARTIAL,
        )

        inspection = inspect_structure(source, _simple_tree(source))

        capture_gaps = _candidates(
            inspection,
            StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
        )
        self.assertEqual(len(capture_gaps), 1)
        self.assertIsNone(capture_gaps[0].source_ref)
        self.assertEqual(capture_gaps[0].start_offset, 0)
        self.assertEqual(capture_gaps[0].end_offset, 0)
        self.assertEqual(
            set(capture_gaps[0].reason_codes),
            {
                "source_ir_not_complete",
                "source_ir_partial",
                "visual_text_facts_missing",
            },
        )
        internal = _candidates(
            inspection,
            StructureInspectionCandidateCategory.INTERNAL_OUTLINE_BOUNDARY,
        )
        self.assertEqual(len(internal), 1)
        self.assertEqual(
            internal[0].disposition,
            StructureInspectionDisposition.SUPPRESSED,
        )
        self.assertIn("source_ir_partial", internal[0].reason_codes)
        self.assertFalse(
            _candidates(
                inspection,
                StructureInspectionCandidateCategory.OVERSIZED_UNSPLIT_SCOPE,
            )
        )
        receipt = _serialize_structure_inspection(
            inspection,
            effective_revision=1,
        )
        self.assertEqual(receipt["status"], "REQUIRES_REEXTRACTION")
        self.assertTrue(receipt["stale"])
        self.assertEqual(receipt["counts"]["capture_gap_count"], 1)
        self.assertEqual(
            receipt["counts"]["observed_document_capture_gap_count"],
            1,
        )
        self.assertEqual(receipt["actionable_candidates"], [])

    def test_ungrounded_source_node_is_reported_without_copying_text(self) -> None:
        first = _pdf_text_node(
            source_order=0,
            page_number=1,
            block_index=0,
            raw_text="Grounded synthetic text.",
        )
        missing = _pdf_text_node(
            source_order=1,
            page_number=2,
            block_index=0,
            raw_text="SECRET-GAP-SYNTHETIC-TEXT",
            bbox=(70.0, 100.0, 350.0, 118.0),
        )
        source = _source_snapshot(nodes=(first, missing))
        tree = _simple_tree(
            source,
            omitted_source_node_ids=frozenset({missing.source_node_id}),
        )

        first_inspection = inspect_structure(source, tree)
        second_inspection = inspect_structure(source, tree)

        gaps = _candidates(
            first_inspection,
            StructureInspectionCandidateCategory.SOURCE_CAPTURE_GAP,
        )
        self.assertEqual(first_inspection, second_inspection)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].source_ref.source_id, missing.source_node_id)
        self.assertIsNone(gaps[0].safe_marker)
        self.assertEqual(gaps[0].start_offset, 0)
        self.assertEqual(gaps[0].end_offset, len(missing.raw_text))
        self.assertEqual(gaps[0].page_number, 2)
        self.assertEqual(gaps[0].bbox, (70.0, 100.0, 350.0, 118.0))
        self.assertNotIn(
            "SECRET-GAP-SYNTHETIC-TEXT",
            first_inspection.model_dump_json(),
        )
        receipt = _serialize_structure_inspection(
            first_inspection,
            effective_revision=0,
        )
        self.assertEqual(receipt["status"], "REVIEW_REQUIRED")
        self.assertEqual(receipt["counts"]["capture_gap_count"], 0)
        self.assertEqual(
            receipt["counts"]["observed_ungrounded_source_fact_count"],
            1,
        )
        self.assertEqual(
            receipt["counts"]["structural_actionable_candidate_count"],
            1,
        )
        self.assertEqual(len(receipt["actionable_candidates"]), 1)

        stale_receipt = _serialize_structure_inspection(
            first_inspection,
            effective_revision=1,
        )
        self.assertEqual(
            stale_receipt["status"],
            "STALE_AFTER_STRUCTURE_REVISION",
        )
        self.assertEqual(
            stale_receipt["counts"]["observed_ungrounded_source_fact_count"],
            1,
        )
        self.assertEqual(
            stale_receipt["counts"]["structural_actionable_candidate_count"],
            0,
        )


def _pdf_text_node(
    *,
    source_order: int,
    page_number: int,
    block_index: int,
    raw_text: str,
    kind: SourceNodeKind = SourceNodeKind.PARAGRAPH,
    bbox: tuple[float, float, float, float] | None = None,
    pdf_text_lines: tuple[PdfTextLineGeometry, ...] = (),
) -> SourceNode:
    locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=page_number,
        block_index=block_index,
        bbox=bbox,
    )
    return SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=kind,
            native_locator=locator,
        ),
        kind=kind,
        source_order=source_order,
        raw_text=raw_text,
        native_locator=locator,
        pdf_text_lines=pdf_text_lines,
    )


def _pdf_full_width_row_table(
    *,
    source_order: int,
    page_number: int,
    table_index: int,
    bbox: tuple[float, float, float, float],
    rows: tuple[tuple[str, tuple[float, float, float, float]], ...],
    column_count: int = 4,
) -> SourceNode:
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=page_number,
        table_index=table_index,
        bbox=bbox,
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row_index, 0),
            row=row_index,
            column=0,
            row_span=1,
            column_span=column_count,
            raw_text=raw_text,
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=page_number,
                table_index=table_index,
                row_index=row_index,
                column_index=0,
                bbox=row_bbox,
            ),
        )
        for row_index, (raw_text, row_bbox) in enumerate(rows)
    )
    return SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.TABLE,
            native_locator=table_locator,
        ),
        kind=SourceNodeKind.TABLE,
        source_order=source_order,
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=len(rows),
            column_count=column_count,
            cells=cells,
            merged_regions=tuple(
                MergedRegion(
                    row=row_index,
                    column=0,
                    column_span=column_count,
                )
                for row_index in range(len(rows))
            ),
        ),
    )


def _source_snapshot(
    *,
    nodes: tuple[SourceNode, ...],
    status: SourceIRStatus = SourceIRStatus.COMPLETE,
    visual_text_snapshots: tuple[VisualTextSnapshot, ...] = (),
    diagnostics: tuple[SourceDiagnostic, ...] = (),
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
        status=status,
        nodes=nodes,
        visual_text_snapshots=visual_text_snapshots,
        diagnostics=diagnostics,
        created_at=CREATED_AT,
    )


def _pdf_diagnostic(
    *,
    code: str,
    page_number: int,
    severity: SourceDiagnosticSeverity = SourceDiagnosticSeverity.WARNING,
) -> SourceDiagnostic:
    return SourceDiagnostic(
        code=code,
        severity=severity,
        message=f"Synthetic diagnostic {code} on page {page_number}.",
        native_locator=NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            bbox=(0.0, 0.0, 600.0, 800.0),
        ),
    )


def _visual_text_snapshot(
    *,
    page_number: int,
    observations: tuple[
        tuple[str, tuple[float, float, float, float], str | None],
        ...,
    ],
    provider_id: str = "synthetic-visual-provider",
    provider_version: str = "1.0.0",
    confidence: float | None = 0.95,
    page_image_variant: str = "",
    evaluated_content_types: tuple[str, ...] = ("TEXT",),
) -> VisualTextSnapshot:
    page_bbox = (0.0, 0.0, 600.0, 800.0)
    page_image_sha256 = hashlib.sha256(
        f"synthetic-page-{page_number}-{page_image_variant}".encode()
    ).hexdigest()
    visual_observations = tuple(
        VisualTextObservation(
            observation_id=stable_visual_text_observation_id(
                source_sha256=SOURCE_HASH,
                page_image_sha256=page_image_sha256,
                provider_id=provider_id,
                provider_version=provider_version,
                page_number=page_number,
                observation_index=index,
                bbox=bbox,
            ),
            observation_index=index,
            source_sha256=SOURCE_HASH,
            page_number=page_number,
            bbox=bbox,
            raw_text=raw_text,
            content_type="TEXT",
            role=role,
            confidence=confidence,
            provider_id=provider_id,
            provider_version=provider_version,
            page_image_sha256=page_image_sha256,
        )
        for index, (raw_text, bbox, role) in enumerate(observations)
    )
    return VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=SOURCE_HASH,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            page_bbox=page_bbox,
            page_image_width_px=1200,
            page_image_height_px=1600,
        ),
        source_sha256=SOURCE_HASH,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_number,
        page_bbox=page_bbox,
        page_image_sha256=page_image_sha256,
        page_image_width_px=1200,
        page_image_height_px=1600,
        evaluated_content_types=evaluated_content_types,
        observations=visual_observations,
    )


def _outline_parent_mismatch_fixture(
    *,
    status: SourceIRStatus = SourceIRStatus.COMPLETE,
) -> tuple[SourceIRSnapshot, StructureTreeSnapshot, SourceNode]:
    root = _pdf_text_node(
        source_order=0,
        page_number=1,
        block_index=0,
        raw_text="4 Synthetic root",
    )
    section = _pdf_text_node(
        source_order=1,
        page_number=2,
        block_index=0,
        raw_text="4.7 Intermediate section",
    )
    nested = _pdf_text_node(
        source_order=2,
        page_number=3,
        block_index=0,
        raw_text="4.7.3 Nested section",
    )
    clause = _pdf_text_node(
        source_order=3,
        page_number=4,
        block_index=0,
        raw_text="4） Local list item",
    )
    misplaced = _pdf_text_node(
        source_order=4,
        page_number=5,
        block_index=0,
        raw_text="4.8 Later sibling section",
    )
    source = _source_snapshot(
        nodes=(root, section, nested, clause, misplaced),
        status=status,
    )
    tree = _simple_tree(
        source,
        aliased_region_labels_by_source_node_id={
            root.source_node_id: "4",
        },
        list_ordinals_by_source_node_id={
            clause.source_node_id: 4,
        },
        structure_parent_source_node_ids={
            section.source_node_id: root.source_node_id,
            nested.source_node_id: section.source_node_id,
            clause.source_node_id: nested.source_node_id,
            misplaced.source_node_id: clause.source_node_id,
        },
    )
    return source, tree, misplaced


def _simple_tree(
    source: SourceIRSnapshot,
    *,
    omitted_source_node_ids: frozenset[str] = frozenset(),
    aliased_region_labels_by_source_node_id: dict[str, str] | None = None,
    materialized_region_source_node_ids: frozenset[str] = frozenset(),
    list_ordinals_by_source_node_id: dict[str, int] | None = None,
    structure_parent_source_node_ids: dict[str, str] | None = None,
) -> StructureTreeSnapshot:
    aliased_region_labels_by_source_node_id = (
        aliased_region_labels_by_source_node_id or {}
    )
    list_ordinals_by_source_node_id = list_ordinals_by_source_node_id or {}
    structure_parent_source_node_ids = structure_parent_source_node_ids or {}
    resolver_id = "structure-inspection-test-resolver"
    resolver_version = "1.0.0"
    root_id = stable_structure_node_id(
        source_ir_id=source.source_ir_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        kind=StructureNodeKind.DOCUMENT,
        identity_key="document",
    )
    nodes: list[StructureNode] = [
        StructureNode(
            structure_node_id=root_id,
            identity_key="document",
            kind=StructureNodeKind.DOCUMENT,
            source_order=0,
            boundary_basis=StructureBoundaryBasis.DOCUMENT,
        )
    ]
    relations: list[StructureRelation] = []
    structure_ids_by_source_node_id: dict[str, str] = {}
    next_order = 1

    def append_node(node: StructureNode) -> None:
        nonlocal next_order
        nodes.append(node)
        relations.append(
            StructureRelation(
                relation_id=stable_structure_relation_id(
                    source_ir_id=source.source_ir_id,
                    resolver_id=resolver_id,
                    resolver_version=resolver_version,
                    kind=StructureRelationKind.CONTAINS,
                    source_structure_node_id=node.parent_structure_node_id or root_id,
                    target_structure_node_id=node.structure_node_id,
                    source_refs=node.source_refs,
                ),
                kind=StructureRelationKind.CONTAINS,
                source_structure_node_id=node.parent_structure_node_id or root_id,
                target_structure_node_id=node.structure_node_id,
                source_refs=node.source_refs,
            )
        )
        next_order += 1

    for source_node in source.nodes:
        if source_node.source_node_id in omitted_source_node_ids:
            continue
        source_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=source_node.source_node_id,
        )
        explicit_label: str | None = None
        list_ordinal: int | None = None
        if (
            aliased_label := aliased_region_labels_by_source_node_id.get(
                source_node.source_node_id
            )
        ) is not None:
            kind = StructureNodeKind.SECTION
            boundary_basis = StructureBoundaryBasis.EXPLICIT_NUMBER
            explicit_label = aliased_label
        elif (
            ordinal := list_ordinals_by_source_node_id.get(source_node.source_node_id)
        ) is not None:
            kind = StructureNodeKind.CLAUSE
            boundary_basis = StructureBoundaryBasis.EXPLICIT_NUMBER
            list_ordinal = ordinal
        elif source_node.source_node_id in materialized_region_source_node_ids:
            kind = StructureNodeKind.SECTION
            boundary_basis = StructureBoundaryBasis.NATIVE_BLOCK
        elif source_node.kind is SourceNodeKind.TABLE:
            kind = StructureNodeKind.TABLE
            boundary_basis = StructureBoundaryBasis.TABLE_SPAN
        elif source_node.kind is SourceNodeKind.TOC_ENTRY:
            kind = StructureNodeKind.GROUP
            boundary_basis = StructureBoundaryBasis.TOC_BLOCK
        elif source_node.kind in {
            SourceNodeKind.HEADER,
            SourceNodeKind.FOOTER,
            SourceNodeKind.WATERMARK,
        }:
            kind = StructureNodeKind.CONTENT
            boundary_basis = StructureBoundaryBasis.PAGE_FURNITURE
        elif (label := _leading_numeric_label(source_node.raw_text)) is not None:
            kind = StructureNodeKind.SECTION
            boundary_basis = StructureBoundaryBasis.EXPLICIT_NUMBER
            explicit_label = label
        elif source_node.kind is SourceNodeKind.HEADING:
            kind = StructureNodeKind.SECTION
            boundary_basis = StructureBoundaryBasis.NATIVE_HEADING
        else:
            kind = StructureNodeKind.CONTENT
            boundary_basis = StructureBoundaryBasis.NATIVE_BLOCK
        identity_key = f"source:{source_node.source_node_id}"
        structure_node_id = stable_structure_node_id(
            source_ir_id=source.source_ir_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            kind=kind,
            identity_key=identity_key,
        )
        parent_structure_node_id = root_id
        if (
            parent_source_node_id := structure_parent_source_node_ids.get(
                source_node.source_node_id
            )
        ) is not None:
            parent_structure_node_id = structure_ids_by_source_node_id[
                parent_source_node_id
            ]
        append_node(
            StructureNode(
                structure_node_id=structure_node_id,
                identity_key=identity_key,
                kind=kind,
                parent_structure_node_id=parent_structure_node_id,
                source_order=next_order,
                source_refs=(source_ref,),
                boundary_basis=boundary_basis,
                explicit_label=explicit_label,
                list_ordinal=list_ordinal,
            )
        )
        structure_ids_by_source_node_id[source_node.source_node_id] = structure_node_id
        if source_node.table_payload is None:
            continue
        rows = sorted({cell.row for cell in source_node.table_payload.cells})
        for row in rows:
            cell_refs = tuple(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
                for cell in sorted(
                    (
                        item
                        for item in source_node.table_payload.cells
                        if item.row == row
                    ),
                    key=lambda item: (item.column, item.cell_id),
                )
            )
            row_identity = f"table:{source_node.source_node_id}:row:{row}"
            row_node_id = stable_structure_node_id(
                source_ir_id=source.source_ir_id,
                resolver_id=resolver_id,
                resolver_version=resolver_version,
                kind=StructureNodeKind.TABLE_ROW,
                identity_key=row_identity,
            )
            append_node(
                StructureNode(
                    structure_node_id=row_node_id,
                    identity_key=row_identity,
                    kind=StructureNodeKind.TABLE_ROW,
                    parent_structure_node_id=structure_node_id,
                    source_order=next_order,
                    source_refs=cell_refs,
                    boundary_basis=StructureBoundaryBasis.TABLE_ROW,
                    table_row_index=row,
                )
            )

    source_cell_ids = tuple(
        sorted(
            cell.cell_id
            for node in source.nodes
            if node.table_payload is not None
            for cell in node.table_payload.cells
        )
    )
    return StructureTreeSnapshot(
        structure_snapshot_id=stable_structure_snapshot_id(
            source_ir_id=source.source_ir_id,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
        ),
        source_ir_id=source.source_ir_id,
        resolver_id=resolver_id,
        resolver_version=resolver_version,
        source_node_ids=tuple(sorted(node.source_node_id for node in source.nodes)),
        source_cell_ids=source_cell_ids,
        nodes=tuple(nodes),
        relations=tuple(relations),
    )


def _leading_numeric_label(value: str) -> str | None:
    match = re.match(
        r"^\s*(?P<label>[1-9][0-9]{0,2}"
        r"(?:\.(?:0|[1-9][0-9]{0,2})){1,7})"
        r"(?=$|[ \t\u3000、.)）:：])",
        value,
    )
    return match.group("label") if match is not None else None


def _candidates(
    inspection: StructureInspectionSnapshotV1,
    category: StructureInspectionCandidateCategory,
) -> tuple[StructureInspectionCandidateV1, ...]:
    return tuple(
        candidate
        for candidate in inspection.candidates
        if candidate.category is category
    )


def _metric_for_source(
    inspection: StructureInspectionSnapshotV1,
    source_node_id: str,
) -> StructureInspectionNodeMetricV1:
    source_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=source_node_id,
    )
    return next(
        metric for metric in inspection.node_metrics if source_ref in metric.source_refs
    )


if __name__ == "__main__":
    unittest.main()
