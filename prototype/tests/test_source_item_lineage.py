from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from copy import deepcopy
import unittest
from unittest.mock import patch

from app.application.agent_workflow import source_requirement_baseline as source
from app.application.agent_workflow import source_item_decomposition as decomp
from app.application.agent_workflow import source_requirement_reference_bridge as bridge
from .test_source_requirement_baseline import _reference_with_text_node, _text_candidate, _text_item


def source_fixture(text):
    document_map = source.compile_document_map_artifact(_reference_with_text_node(
        node_id="clause", label="1", title="Requirements", contents=(("body", text, False),)))
    request = source.build_source_item_text_scope_request(document_map, node_id="clause")
    candidate = _text_candidate(request, items=(_text_item(request, key="body", label="Requirements", start=0, end=len(text)),))
    baseline, receipt = source.compile_source_requirement_baseline(document_map=document_map,
        requests=(), candidates=(), text_requests=(request,), text_candidates=(candidate,))
    return document_map, request, candidate, baseline, receipt


class NewSourceLineageTests(unittest.TestCase):
    def test_empty_baseline_can_grow_its_first_real_item(self):
        document_map, request, candidate, _, _ = source_fixture("The controller shall stop.")
        empty, receipt = source.create_empty_source_requirement_baseline(document_map)
        self.assertEqual((empty.status, len(empty.records), receipt.record_count), ("EMPTY", 0, 0))
        for operation in (source.append_source_requirement_baseline, source.grow_source_requirement_baseline):
            with self.subTest(operation=operation.__name__):
                populated, validation, change = operation(document_map=document_map, previous_artifact=empty,
                    previous_validation_receipt=receipt, text_requests=(request,), text_candidates=(candidate,))
                self.assertEqual(populated.records[0].source_display_id, "SRC-0001")
                self.assertEqual(populated.records[0].requirement_display_id, "REQ-0001")
                self.assertEqual(validation.record_count, 1)
                self.assertEqual(len(empty.records), 0)

    def test_split_native_heading_blocks_do_not_create_fake_source_items(self):
        document_map = source.compile_document_map_artifact(_reference_with_text_node(
            node_id="heading", label="3.1", title="General", contents=(("number", "3.1", True), ("title", "General", True))))
        request = source.build_source_item_text_scope_request(document_map, node_id="heading")
        candidate = _text_candidate(request, structure_only=tuple(source.SourceItemStructureOnlySpanV1(
            text_span=source.SourceItemTextSpanV1(content_id=content.content_id, start_offset=0, end_offset=len(content.text)),
            reason_codes=("source_heading",)) for content in request.source_content))
        source.validate_source_item_text_slice_candidate(request, candidate)

    def test_same_pdf_history_cannot_replace_a_different_current_map(self):
        public = _reference_with_text_node(node_id="new-heading", label="9", title="Current", contents=(("new", "Current", True),))
        old_map, _, _, old_baseline, old_receipt = source_fixture("Legacy source requirement")
        with patch("app.application.agent_workflow.native_structure_bridge.get_native_structure_reference", return_value=public), \
             patch.object(bridge, "_load_latest_reference", return_value={"document_map": old_map, "baseline": old_baseline, "receipt": old_receipt}):
            result = bridge._load_base_reference(project_id="project", document_id="document", source_sha256=old_map.source_sha256)
        self.assertEqual(result["baseline"].status, "EMPTY")
        self.assertNotEqual(result["document_map"].artifact_sha256, old_map.artifact_sha256)

    def test_reading_presentation_does_not_invalidate_an_unchanged_source_map(self):
        text = "Legacy source requirement"
        public = _reference_with_text_node(node_id="clause", label="1", title="Requirements", contents=(("body", text, False),))
        old_map, _, _, old_baseline, old_receipt = source_fixture(text)
        target = next(n for n in public["document_tree"]["nodes"] if n["node_id"] == "clause")
        target["source_content"][0]["reading_projection"] = {"display_text": text}
        with patch("app.application.agent_workflow.native_structure_bridge.get_native_structure_reference", return_value=public), \
             patch.object(bridge, "_load_latest_reference", return_value={"document_map": old_map, "baseline": old_baseline, "receipt": old_receipt}):
            result = bridge._load_base_reference(project_id="project", document_id="document", source_sha256=old_map.source_sha256)
        self.assertEqual(result["baseline"].artifact_sha256, old_baseline.artifact_sha256)
        self.assertEqual(result["document_map"].artifact_sha256, old_map.artifact_sha256)
        self.assertNotEqual(result["presentation_projection_sha256"], old_map.native_projection_sha256)


class DecompositionSourceMeaningTests(unittest.TestCase):
    def _decision(self, text, ranges, *, context_ranges=(), logic="ALL_OF"):
        _, _, _, baseline, _ = source_fixture(text)
        children = [{"text": text[start:end], "derivation_type": "VERBATIM_SPAN",
                     "source_ranges": [{"segment_index": 0, "start_offset": start, "end_offset": end}],
                     "requirement_kinds": ["CONSTRAINT"]} for start, end in ranges]
        decision = {"source_item_id": baseline.records[0].source_record_id, "action": "SPLIT", "reason": "Separate response outcomes",
                    "logic": logic, "children": children, "context_ranges": list(context_ranges)}
        return baseline, decision

    def test_inclusive_time_limit_cannot_be_strengthened_after_normalizing(self):
        text = "保持1h以上。记录结果。"
        baseline, decision = self._decision(text, [(0, 7), (7, len(text))])
        decision["children"][0].update(text="保持>1h。", derivation_type="NORMALIZED_DERIVATION")
        with self.assertRaisesRegex(ValueError, "numeric comparator"):
            decomp.compile_source_item_decomposition(baseline, [decision])

    def test_alternative_branches_must_not_become_two_mandatory_requirements(self):
        text = "Status A or Status C"
        contexts = [{"source_range": {"segment_index": 0, "start_offset": 8, "end_offset": 12}, "disposition": "CONTEXT"}]
        baseline, decision = self._decision(text, [(0, 8), (12, len(text))], context_ranges=contexts)
        with self.assertRaisesRegex(ValueError, "alternative"):
            decomp.compile_source_item_decomposition(baseline, [decision])
        decision["logic"] = "ANY_OF"
        artifact = decomp.compile_source_item_decomposition(baseline, [decision])
        decomp.validate_source_item_decomposition(baseline, artifact)

    def test_rehashing_a_modified_derived_quote_does_not_pass_validation(self):
        text = "Open the valve. Close the valve."
        baseline, decision = self._decision(text, [(0, 15), (16, len(text))])
        artifact = decomp.compile_source_item_decomposition(baseline, [decision])
        altered = deepcopy(artifact)
        altered["records"][decision["source_item_id"]][0]["source_excerpts"][0]["text"] = "A different obligation"
        digest = source.canonical_json_sha256({k: v for k, v in altered.items() if k not in {"artifact_sha256", "artifact_id"}})
        altered.update(artifact_sha256=digest, artifact_id="source-item-decomposition-" + digest[:32])
        with self.assertRaisesRegex(ValueError, "altered"):
            decomp.validate_source_item_decomposition(baseline, altered)


if __name__ == "__main__":
    unittest.main()
