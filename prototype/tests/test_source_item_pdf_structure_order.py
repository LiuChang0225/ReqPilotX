"""Synthetic PDF evidence stays in source order through revision and Excel.

The fixtures exercise the shared workbench/plugin projection. They make no claim
about real customer semantics, browser acceptance, or live model execution.
"""
from __future__ import annotations

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_delivery import (
    build_delivery_snapshot,
    export_delivery_xlsx,
)
from app.application.agent_workflow.delivery_snapshot_compatibility import (
    verify_published_delivery_snapshot,
)
from app.application.agent_workflow.source_item_ledger import (
    document_node_position,
    project_source_item_ledger,
)
from .test_source_requirement_baseline import (
    _reference_with_text_node,
    _text_candidate,
    _text_item,
)


def fixture(*, direct_y=100.0, child_y=200.0, child_page=1, anchor_y=None,
            direct_text="The controller shall report readiness."):
    child_text = "The controller shall retain the last fault code."
    reference = _reference_with_text_node(
        node_id="controls", label="1", title="Controls",
        contents=(("direct-body", direct_text, False),),
    )
    reference["logical_tables"] = []
    reference["document_tree"]["nodes"] = [
        node for node in reference["document_tree"]["nodes"]
        if node["node_id"] in {"root", "controls"}
    ]
    parent = reference["document_tree"]["nodes"][-1]
    parent["order"] = 0
    parent["source_content"][0]["bbox"] = [40.0, direct_y, 500.0, direct_y + 20.0]
    child = deepcopy(parent)
    child.update(node_id="later-clause", parent_id="controls", order=0,
                 label="1.1", title="Retained fault code", pages=[child_page])
    content = child["source_content"][0]
    content.update(content_id="child-body", page=child_page,
                   bbox=[40.0, child_y, 500.0, child_y + 20.0],
                   source_handles=["source-child-body"], text=child_text,
                   text_sha256=core.canonical_text_sha256(child_text))
    if anchor_y is not None:
        child["source_anchors"] = [{
            "anchor_id": "child-region", "anchor_kind": "TEXT",
            "page": child_page, "bbox": [40.0, anchor_y, 500.0, anchor_y + 10.0],
            "source_bounds": [0.0, 0.0, 595.0, 842.0],
            "precision": "EXACT_NATIVE_SPAN_RANGE",
        }]
    reference["document_tree"]["nodes"].append(child)
    mapping = core.compile_document_map_artifact(reference)
    requests, candidates = [], []
    for node_id in ("controls", "later-clause"):
        request = core.build_source_item_text_scope_request(mapping, node_id=node_id)
        candidate = _text_candidate(request, items=(
            _text_item(request, key=node_id, label=node_id, start=0,
                       end=len(request.source_content[0].text)),
        ))
        requests.append(request)
        candidates.append(candidate)
    baseline, receipt = core.compile_source_requirement_baseline(
        document_map=mapping, requests=(), candidates=(),
        text_requests=tuple(requests), text_candidates=tuple(candidates), status="COMPLETE",
    )
    return mapping, baseline, receipt, tuple(requests), tuple(candidates)


class PdfStructureOrderTests(unittest.TestCase):
    def test_text_geometry_interleaves_structure_and_direct_items(self):
        for kwargs, first in (
            ({}, "direct"),
            ({"direct_y": 250.0, "anchor_y": 300.0}, "child"),
            ({"anchor_y": 80.0}, "child"),
            ({"direct_y": 500.0, "child_y": 10.0, "child_page": 2}, "direct"),
        ):
            with self.subTest(kwargs=kwargs):
                mapping, baseline, _, _, _ = fixture(**kwargs)
                original = (mapping.model_dump(mode="json"), baseline.model_dump(mode="json"))
                direct = next(record for record in baseline.records if record.node_id == "controls")
                expected = [direct.source_record_id, "later-clause"]
                if first == "child":
                    expected.reverse()
                ledger = project_source_item_ledger(document_map=mapping, artifact=baseline)
                parent = next(row for row in ledger["rows"] if row["row_id"] == "controls")
                self.assertEqual(parent["child_row_ids"], expected)
                snapshot = build_delivery_snapshot(mapping, baseline, {})
                actual = [node["node_id"] for node in snapshot["nodes"]
                          if node["parent_id"] == "controls"]
                self.assertEqual(actual, expected)
                self.assertEqual(original, (mapping.model_dump(mode="json"), baseline.model_dump(mode="json")))

    def test_feedback_revision_keeps_source_ids_and_same_version_excel_order(self):
        mapping, baseline, receipt, requests, candidates = fixture()
        original = baseline.model_dump(mode="json")
        request, candidate = requests[0], candidates[0]
        item = candidate.items[0]
        revised_item = core.SourceItemTextBoundaryCandidateV1.model_validate({
            **item.model_dump(mode="json"), "decision_summary": "Synthetic feedback corrected the category.",
            "requirement_kinds": ["QUALITY"],
        })
        revised_candidate = _text_candidate(request, items=(revised_item,))
        revision = core.build_source_item_scope_revision(
            document_map=mapping, current_artifact=baseline,
            current_validation_receipt=receipt, operation="REPLACE",
            affected_scope_ids=("text:controls",), comparison_scope_ids=("text:later-clause",),
            feedback_ids=("feedback-synthetic-order",), text_requests=(request,),
            text_candidates=(revised_candidate,),
        )
        successor, _, proof = core.compile_source_item_scope_revision(
            document_map=mapping, current_artifact=baseline,
            current_validation_receipt=receipt, scope_revision=revision,
            text_requests=(request,), text_candidates=(revised_candidate,),
        )
        self.assertEqual(successor.revision, baseline.revision + 1)
        self.assertEqual(baseline.model_dump(mode="json"), original)
        self.assertEqual([r.source_record_id for r in successor.records],
                         [r.source_record_id for r in baseline.records])
        self.assertEqual(proof.feedback_ids, ("feedback-synthetic-order",))
        snapshot = build_delivery_snapshot(mapping, successor, {})
        restored = core.SourceRequirementBaselineArtifactV1.model_validate_json(successor.model_dump_json())
        self.assertEqual(build_delivery_snapshot(mapping, restored, {}), snapshot)
        sources = [node for node in snapshot["nodes"] if node.get("source_item")]
        self.assertEqual([node["structure_node_id"] for node in sources],
                         ["controls", "later-clause"])
        self.assertEqual(snapshot["source_item_baseline"]["artifact_sha256"], successor.artifact_sha256)
        workbook = load_workbook(BytesIO(export_delivery_xlsx(snapshot)))
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["_ReqPilotXMeta"]["B3"].value, successor.artifact_sha256)
        matrix = workbook["ComplianceMatrix"]
        source_rows = {record.source_record_id: next(
            row[0].row for row in matrix if any(
                record.requirement_display_id in str(cell.value or "") for cell in row
            )) for record in successor.records}
        self.assertLess(source_rows[successor.records[0].source_record_id],
                        source_rows[successor.records[1].source_record_id])
        values = "\n".join(str(cell.value) for row in matrix for cell in row if cell.value)
        for record in successor.records:
            self.assertIn(record.text_bindings[0].text, values)

    def test_nodes_without_measured_evidence_keep_the_legacy_fallback(self):
        node = SimpleNamespace(source_content=(), source_anchors=(), pages=(3,), order=7)
        self.assertEqual(document_node_position(node), (3, 7.0, 0.0, 0, 7))

    def test_exact_legacy_layout_remains_readable_without_rewriting_its_snapshot(self):
        from app.application.agent_workflow import source_item_ledger as ledger_module
        from app.application.agent_workflow.source_item_decomposition import compile_source_item_decomposition

        mapping, baseline, _, _, _ = fixture(
            direct_text="Report readiness. Preserve diagnostic history."
        )
        direct = next(record for record in baseline.records if record.node_id == "controls")
        text = direct.text_bindings[0].text
        cut = text.index(" Preserve")
        decomposition = compile_source_item_decomposition(baseline, [{
            "source_item_id": direct.source_record_id, "action": "SPLIT",
            "reason": "Two explicitly separate synthetic operating duties.",
            "children": [{"text": text[lo:hi].strip(), "source_ranges": [
                {"segment_index": 0, "start_offset": lo, "end_offset": hi}
            ]} for lo, hi in ((0, cut), (cut, len(text)))],
        }])
        current = build_delivery_snapshot(mapping, baseline, {}, decomposition=decomposition)

        def old_position(node):
            if node.source_anchors:
                anchor = min(node.source_anchors,
                             key=lambda item: (item.page, item.bbox[1], item.bbox[0]))
                return (anchor.page, float(anchor.bbox[1]), float(anchor.bbox[0]), 0, node.order)
            return (node.pages[0] if node.pages else 1_000_000,
                    float(node.order), 0.0, 0, node.order)

        with patch.object(ledger_module, "document_node_position", old_position):
            published = build_delivery_snapshot(mapping, baseline, {}, decomposition=decomposition)
        self.assertNotEqual(published, current)
        original = deepcopy(published)

        def verify(value, **overrides):
            return verify_published_delivery_snapshot(
                document_map=overrides.get("document_map", mapping),
                baseline=baseline, published_snapshot=value, current_snapshot=current,
                expected_snapshot_sha256=core.canonical_json_sha256(value),
            )

        proof = verify(published)
        self.assertTrue(proof["verified_legacy_pdf_structure_order"])
        self.assertFalse(proof["published_snapshot_modified"])
        self.assertEqual(published, original)
        for field, value in (("display_text", "Invented customer text"),
                             ("classification_paths", ["PROJECT"]),
                             ("source_order", 123),
                             ("parent_id", "root")):
            changed = deepcopy(published)
            target = next(node for node in changed["nodes"] if node["node_id"] == "later-clause")
            target[field] = value
            changed["delivery_sha256"] = core.canonical_json_sha256(
                {key: part for key, part in changed.items() if key != "delivery_sha256"}
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify(changed)
        changed_map = mapping.model_copy(update={"artifact_sha256": "b" * 64})
        with self.assertRaises(ValueError):
            verify(published, document_map=changed_map)
        with self.assertRaises(ValueError):
            verify(published, document_map=None)
        office = mapping.model_dump(mode="json")
        office["source_format"] = None
        content = office["nodes"][-1]["source_content"][0]
        content.update(page=None, bbox=None, source_bounds=None, source_sequence=0,
                       native_locator={"format": "docx", "body_element_index": 0,
                                       "paragraph_index": 0})
        digest = core.canonical_json_sha256({
            key: part for key, part in office.items() if key not in {"artifact_id", "artifact_sha256"}
        })
        office.update(artifact_id="document-map-" + digest[:32], artifact_sha256=digest)
        with self.assertRaisesRegex(ValueError, "locator format differs"):
            verify(published, document_map=office)


if __name__ == "__main__":
    unittest.main()
