from __future__ import annotations

from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from copy import deepcopy
import hashlib
import unittest

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.document_map_revision import (
    DocumentMapRevisionError, compile_document_map_revision,
    rebind_baseline_for_document_map_revision,
)
from app.application.agent_workflow.source_item_visual import (
    build_source_item_image_scope_packet, compile_source_item_images,
)


def fixture(*, mislabeled=False, table_image=False, misclassified_clause=False):
    """Synthetic captured blocks: stable handles, exact text hashes and cell boxes."""
    source_sha = hashlib.sha256(b"Synthetic Map revision source fixture v1").hexdigest()
    bounds = [0.0, 0.0, 300.0, 400.0]

    def content(cid, text, y, title=False):
        return {"content_id": cid, "page": 1, "bbox": [10.0, float(y), 290.0, float(y + 12)],
            "source_bounds": bounds, "source_handles": [cid], "text": text,
            "text_sha256": core.canonical_text_sha256(text), "precision": "SOURCE_IR_BLOCK", "is_title": title}

    def node(nid, parent, order, label, title, contents):
        return {"node_id": nid, "parent_id": parent, "order": order, "kind": "SECTION",
            "label": label, "title": title, "title_authority": "SOURCE_VISIBLE", "pages": [1],
            "source_content": contents, "source_anchors": [{"anchor_id": c["content_id"], "source_handle": c["content_id"],
                "anchor_kind": "SOURCE", "page": 1, "bbox": c["bbox"], "source_bounds": bounds,
                "source_text_sha256": c["text_sha256"], "precision": "SOURCE_IR_BLOCK"} for c in contents], "content_blocks": []}

    nodes = [{"node_id": "root", "parent_id": None, "order": 0, "kind": "DOCUMENT", "label": "",
              "title": "synthetic.pdf", "title_authority": "AGENT_DISPLAY_LABEL", "pages": [1]},
             node("other", "root", 1, "1", "Other", [content("heading-a", "1 Other", 10, True), content("context-a", "Context for the other subsystem.", 24)]),
             node("correct", "root", 2, "2", "Correct", [content("heading-b", "2 Correct", 50, True), content("context-b", "Context for the intended subsystem.", 64)]),
             node("group", "other", 3, "9" if mislabeled else "3", "Wrong" if mislabeled else "The housing shall be grounded." if misclassified_clause else "Torque", [
                 content("heading-torque", "3 The housing shall be grounded." if misclassified_clause else "3 Torque", 100, True),
                 content("body-one", "The actuator shall provide torque.", 120),
                 content("body-two", "Torque shall be at least 10 Nm.", 138)])]
    table_box = [10.0, 180.0, 290.0, 220.0]
    nodes[-1]["content_blocks"] = [{"kind": "LOGICAL_TABLE", "logical_table_scope_id": "lt-torque"}]
    nodes[-1]["source_anchors"] += [
        {"anchor_id": "table-physical:0:2", "source_handle": "table-source", "anchor_kind": "TABLE", "page": 1,
         "bbox": table_box, "source_bounds": bounds, "precision": "SOURCE_IR_TABLE", "table_token": "table-physical",
         "row_range": [0, 2], "logical_table_scope_id": "lt-torque"},
        {"anchor_id": "drawing-placement", "source_handle": "drawing-source", "asset_id": "drawing-asset",
         "anchor_kind": "IMAGE", "page": 1, "bbox": [160.0, 202.0, 200.0, 218.0] if table_image else [10.0, 280.0, 100.0, 320.0], "source_bounds": bounds,
         "precision": "EXACT_IMAGE"}]
    reference = {"schema_version": "native-structure-reference-public/v1", "artifact_id": "synthetic-capture-v1",
        "source_sha256": source_sha, "document_tree": {"root_ids": ["root"], "nodes": nodes},
        "logical_tables": [{"scope_id": "lt-torque", "title": "Torque", "owner_unit_id": "root", "role": "SOURCE_TABLE",
                            "pages": [1], "fragments": [{"table_token": "table-physical", "page": 1, "row_range": [0, 2]}]}],
        "structure_proposals": []}
    dm = core.compile_document_map_artifact(reference)
    cells = [{"cell_id": f"cell-{row}-{col}", "row": row, "column": col, "row_span": 1, "column_span": 1,
              "text": text, "bbox": [10.0 + col * 140, 180.0 + row * 20, 150.0 + col * 140, 200.0 + row * 20]}
             for row, values in enumerate([["Parameter", "Requirement"], ["Torque", "at least 10 Nm"]])
             for col, text in enumerate(values)]
    previews = {"lt-torque": {"source_sha256": source_sha, "logical_table_scope_id": "lt-torque", "fragments": [
        {"fragment_id": "table-physical:0:2", "table_token": "table-physical", "page": 1, "row_range": [0, 2],
         "row_count": 2, "column_count": 2, "bbox": table_box, "source_bounds": bounds, "cells": cells}]}}
    request = core.build_source_requirement_scope_request(dm, previews["lt-torque"])
    judgment = {"requirementness": "REQUIREMENT", "requirement_kinds": ("QUALITY",), "confidence": 0.95,
                "decision_summary": "Synthetic source performance target.", "reason_codes": ("source_review",)}
    candidate = core.SourceRequirementSliceCandidateV1(source_sha256=source_sha, document_map_artifact_id=dm.artifact_id,
        document_map_artifact_sha256=dm.artifact_sha256, request_sha256=request.request_sha256, logical_table_scope_id="lt-torque",
        records=(core.SourceRequirementBoundaryCandidateV1(record_key="torque-row", label="Torque", source_form="TABLE_RECORD",
            row_groups=(core.SourceRequirementRowGroupV1(fragment_id="table-physical:0:2", row_start=1, row_end=2),), **judgment),),
        context_row_groups=(core.SourceRequirementContextRowGroupV1(role="HEADER", reason_codes=("shared_table_header",),
            row_group=core.SourceRequirementRowGroupV1(fragment_id="table-physical:0:2", row_start=0, row_end=1)),))
    text_request = core.build_source_item_text_scope_request(dm, node_id="group",
        content_ids=("body-one", "body-two") if mislabeled else None)
    text_candidate = core.SourceItemTextSliceCandidateV1(source_sha256=source_sha, document_map_artifact_id=dm.artifact_id,
        document_map_artifact_sha256=dm.artifact_sha256, request_sha256=text_request.request_sha256, scope_id="text:group",
        items=(core.SourceItemTextBoundaryCandidateV1(item_key="torque-body", label="Actuator torque", source_form="PARAGRAPH",
            text_spans=tuple(core.SourceItemTextSpanV1(content_id=c.content_id, start_offset=0, end_offset=len(c.text))
                             for c in text_request.source_content if not c.is_title), **judgment),),
        structure_only_spans=tuple(core.SourceItemStructureOnlySpanV1(
            text_span=core.SourceItemTextSpanV1(content_id=c.content_id, start_offset=0, end_offset=len(c.text)),
            reason_codes=("source_title",)) for c in text_request.source_content if c.is_title))
    baseline, receipt = core.compile_source_requirement_baseline(document_map=dm, requests=(request,), candidates=(candidate,),
        text_requests=(text_request,), text_candidates=(text_candidate,), starting_display_ordinal=40)
    packet = build_source_item_image_scope_packet(dm, node_id="group", baseline=baseline, table_previews=previews)
    baseline, receipt, _ = compile_source_item_images(dm, [{"node_id": "group", "packet_sha256": packet["packet_sha256"],
        "items": [{"item_key": "drawing", "display_label": "Torque drawing", "image_anchor_ids": ["drawing-placement"],
                   "requirementness": "NON_REQUIREMENT", "requirement_kinds": [], "non_requirement_role": "REFERENCE",
                   "confidence": 0.95, "decision_summary": "Retain the actual drawing.", "reason_codes": ["source_review"]}] if packet["images"] else []}],
        previous_baseline=baseline, table_previews=previews)
    return reference, dm, baseline, receipt, previews


class DocumentMapRevisionTests(unittest.TestCase):
    def setUp(self):
        self.reference, self.dm, self.base, self.validation, self.previews = fixture()

    def candidate(self, update):
        reference = deepcopy(self.reference)
        update(reference)
        return core.compile_document_map_artifact(reference)

    def rebind(self, candidate, *, previews=None, baseline=None, validation=None):
        revised = compile_document_map_revision(self.dm, candidate)
        result = rebind_baseline_for_document_map_revision(self.dm, revised["document_map"], baseline or self.base,
            validation or self.validation, self.previews if previews is None else previews)
        return revised, result

    def test_reparent_preserves_source_table_image_and_stable_retired_number_floor(self):
        before = (self.dm.model_dump(), self.base.model_dump(), self.validation.model_dump(), deepcopy(self.previews))
        candidate = self.candidate(lambda ref: ref["document_tree"]["nodes"][-1].update(parent_id="correct"))
        revised, result = self.rebind(candidate)
        new_map = core.DocumentMapArtifactV1.model_validate(revised["document_map"])
        self.assertEqual(core.compile_document_map_artifact(revised["native_reference"], revision=new_map.revision), new_map)
        self.assertEqual(new_map.revision, self.dm.revision + 1)
        new = core.SourceRequirementBaselineArtifactV1.model_validate(result["baseline"])
        self.assertEqual(new.predecessor_artifact_sha256, self.base.artifact_sha256)
        self.assertEqual(new.source_display_high_watermark, self.base.source_display_high_watermark)
        self.assertEqual([r.source_display_id for r in new.records], [r.source_display_id for r in self.base.records])
        for old, record in zip(self.base.records, new.records):
            self.assertEqual(record.ancestor_path[-2:], ("2 Correct", "3 Torque"))
            for key in ("source_record_id", "source_display_id", "requirement_display_id", "source_order", "label",
                        "text_bindings", "table_fragments", "image_bindings", "requirement_kinds", "requirementness", "decision_summary"):
                self.assertEqual(getattr(record, key), getattr(old, key))
        self.assertEqual(set(result["receipt"]["context_changed_source_item_ids"]), {r.source_record_id for r in self.base.records})
        self.assertEqual(len(result["receipt"]["rebound_source_item_ids"]), 3)
        self.assertEqual(before, (self.dm.model_dump(), self.base.model_dump(), self.validation.model_dump(), self.previews))

    def test_correct_source_title_and_number_without_reclassifying_or_recreating_items(self):
        reference, previous, baseline, validation, previews = fixture(mislabeled=True)
        reference["document_tree"]["nodes"][-1].update(label="3", title="Torque")
        candidate = core.compile_document_map_artifact(reference)
        revised = compile_document_map_revision(previous, candidate)
        result = rebind_baseline_for_document_map_revision(previous, revised["document_map"], baseline, validation, previews)
        self.assertEqual([r["source_record_id"] for r in result["baseline"]["records"]], [r.source_record_id for r in baseline.records])
        self.assertTrue(all(r["ancestor_path"][-1] == "3 Torque" for r in result["baseline"]["records"]))
        fields = next(c["fields"] for c in revised["receipt"]["node_changes"] if c["node_id"] == "group")
        self.assertEqual(fields["label"], {"before": "9", "after": "3"})

    def test_new_node_and_scope_names_rebind_only_by_conserved_original_evidence(self):
        def rename(ref):
            node = ref["document_tree"]["nodes"][-1]
            node["node_id"] = "torque-corrected"
            node["content_blocks"][0]["logical_table_scope_id"] = "lt-renamed"
            next(a for a in node["source_anchors"] if a.get("table_token"))["logical_table_scope_id"] = "lt-renamed"
            ref["logical_tables"][0]["scope_id"] = "lt-renamed"
        revised, result = self.rebind(self.candidate(rename))
        self.assertTrue(all(r["node_id"] == "torque-corrected" for r in result["baseline"]["records"]))
        self.assertEqual(result["baseline"]["covered_scope_ids"], ["image:torque-corrected", "lt-renamed", "text:torque-corrected"])
        self.assertTrue(all(c["node_id"] == "torque-corrected" for c in result["baseline"]["text_context_dispositions"]))
        self.assertEqual(result["table_previews"]["lt-renamed"]["fragments"], self.previews["lt-torque"]["fragments"])
        self.assertEqual(result["receipt"]["context_changed_source_item_ids"], [])

    def test_whole_business_sources_move_to_the_actual_existing_owner(self):
        def move(ref):
            group, correct = ref["document_tree"]["nodes"][-1], ref["document_tree"]["nodes"][2]
            moved = [c for c in group["source_content"] if not c["is_title"]]
            group["source_content"] = [c for c in group["source_content"] if c["is_title"]]
            correct["source_content"].extend(moved)
            anchors = [a for a in group["source_anchors"] if a["anchor_id"] != "heading-torque"]
            group["source_anchors"] = [a for a in group["source_anchors"] if a["anchor_id"] == "heading-torque"]
            correct["source_anchors"].extend(anchors)
            correct["content_blocks"], group["content_blocks"] = group["content_blocks"], []
        revised, result = self.rebind(self.candidate(move))
        self.assertTrue(all(r["node_id"] == "correct" for r in result["baseline"]["records"]))
        self.assertEqual(result["baseline"]["covered_scope_ids"], ["image:correct", "lt-torque", "text:correct", "text:group"])
        self.assertEqual(result["baseline"]["text_context_dispositions"][0]["node_id"], "group")

    def test_table_owned_image_relation_moves_without_a_duplicate_image_source_item(self):
        reference, previous, baseline, validation, previews = fixture(table_image=True)
        self.assertEqual(len(baseline.table_image_relations), 1)
        self.assertEqual(len(baseline.records), 2)
        reference["document_tree"]["nodes"][-1]["node_id"] = "corrected-section"
        candidate = core.compile_document_map_artifact(reference)
        revised = compile_document_map_revision(previous, candidate)
        result = rebind_baseline_for_document_map_revision(previous, revised["document_map"], baseline, validation, previews)
        self.assertEqual(len(result["baseline"]["records"]), 2)
        relation = result["baseline"]["table_image_relations"][0]
        self.assertEqual(relation["node_id"], "corrected-section")
        self.assertEqual(relation["image_binding"], baseline.table_image_relations[0].image_binding.model_dump(mode="json"))

    def test_same_whole_source_item_cannot_silently_split_between_new_owners(self):
        def split(ref):
            group, parent = ref["document_tree"]["nodes"][-1], ref["document_tree"]["nodes"][2]
            content = next(c for c in group["source_content"] if c["content_id"] == "body-two")
            anchor = next(a for a in group["source_anchors"] if a["anchor_id"] == "body-two")
            group["source_content"].remove(content); parent["source_content"].append(content)
            group["source_anchors"].remove(anchor); parent["source_anchors"].append(anchor)
        revised = compile_document_map_revision(self.dm, self.candidate(split))
        with self.assertRaisesRegex(DocumentMapRevisionError, "explicit SourceItem boundary revision"):
            rebind_baseline_for_document_map_revision(self.dm, revised["document_map"], self.base, self.validation, self.previews)

    def test_missing_changed_or_duplicated_source_cannot_be_resigned_into_a_map(self):
        mutations = [lambda ref: ref["document_tree"]["nodes"][-1]["source_content"].pop(),
            lambda ref: ref["document_tree"]["nodes"][-1]["source_anchors"][-1]["bbox"].__setitem__(0, 11.0),
            lambda ref: ref["document_tree"]["nodes"][-1]["source_content"].append(deepcopy(ref["document_tree"]["nodes"][-1]["source_content"][0])),
            lambda ref: ref["document_tree"]["nodes"][-1]["source_content"][0]["source_handles"].__setitem__(0, "unproven-new-capture-id")]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaisesRegex(DocumentMapRevisionError, "SOURCE_FACTS_CHANGED"):
                compile_document_map_revision(self.dm, self.candidate(mutation))

    def test_arbitrary_title_or_number_cannot_be_promoted_to_customer_heading(self):
        for updates in ({"title": "Unseen source title"}, {"label": "700", "title": "3 Torque"}, {"label": "3.9"}):
            with self.subTest(updates=updates), self.assertRaisesRegex(DocumentMapRevisionError, "TITLE_NOT_SOURCE_GROUNDED"):
                compile_document_map_revision(self.dm, self.candidate(lambda ref: ref["document_tree"]["nodes"][-1].update(updates)))

    def test_title_role_correction_preserves_source_but_cannot_hide_unrelated_body(self):
        ref = deepcopy(self.reference)
        ref["document_tree"]["nodes"][-1]["source_content"][0]["is_title"] = False
        previous = core.compile_document_map_artifact(ref)
        result = compile_document_map_revision(previous, self.dm)
        self.assertTrue(result["document_map"]["nodes"][-1]["source_content"][0]["is_title"])
        with self.assertRaisesRegex(DocumentMapRevisionError, "TITLE_ROLE_NOT_SOURCE_GROUNDED"):
            compile_document_map_revision(self.dm, self.candidate(
                lambda ref: ref["document_tree"]["nodes"][-1]["source_content"][1].update(is_title=True)))

    def test_changed_table_regions_and_missing_previews_are_rejected(self):
        with self.assertRaisesRegex(DocumentMapRevisionError, "LOGICAL_TABLE_CHANGED"):
            compile_document_map_revision(self.dm, self.candidate(lambda ref: ref["logical_tables"][0]["fragments"][0].update(row_range=[0, 1])))
        candidate = self.candidate(lambda ref: ref["document_tree"]["nodes"][-1].update(parent_id="correct"))
        with self.assertRaisesRegex(DocumentMapRevisionError, "PREVIEW_MISSING"):
            self.rebind(candidate, previews={})
        altered = deepcopy(self.previews)
        altered["lt-torque"]["fragments"][0]["cells"][-1]["bbox"][2] = 285.0
        with self.assertRaisesRegex(DocumentMapRevisionError, "BINDING_REPLAY_DRIFT"):
            self.rebind(candidate, previews=altered)

    def test_another_source_revision_is_not_a_map_feedback_revision(self):
        with self.assertRaisesRegex(DocumentMapRevisionError, "SOURCE_CHANGED"):
            compile_document_map_revision(self.dm, self.candidate(lambda ref: ref.update(source_sha256="a" * 64)))

    def test_self_signed_forged_binding_fails_existing_compiler_replay(self):
        records = list(self.base.records)
        index = next(i for i, r in enumerate(records) if r.text_bindings)
        record = records[index]
        binding = record.text_bindings[0].model_copy(update={"bbox": (11.0, 120.0, 290.0, 132.0)})
        records[index] = record.model_copy(update={"text_bindings": (binding, *record.text_bindings[1:])})
        draft = self.base.model_copy(update={"records": tuple(records)})
        sha = core.canonical_json_sha256(core._baseline_hash_payload(draft))
        forged = draft.model_copy(update={"artifact_sha256": sha, "artifact_id": "source-requirement-baseline-" + sha[:32]})
        receipt = core._compile_validation_receipt_from_artifact(document_map=self.dm, artifact=forged)
        with self.assertRaisesRegex(DocumentMapRevisionError, "BINDING_REPLAY_DRIFT"):
            self.rebind(self.dm, baseline=forged, validation=receipt)

    def test_raw_candidate_without_revision_binding_cannot_bypass_revision_compiler(self):
        candidate = self.candidate(lambda ref: ref["document_tree"]["nodes"][-1].update(parent_id="correct"))
        with self.assertRaisesRegex(DocumentMapRevisionError, "PREDECESSOR_OR_COMPILER"):
            rebind_baseline_for_document_map_revision(self.dm, candidate, self.base, self.validation, self.previews)

    def test_sibling_order_is_scoped_to_its_parent_not_globally_unique(self):
        reference = deepcopy(self.reference)
        reference["document_tree"]["nodes"][-1]["order"] = 0
        previous = core.compile_document_map_artifact(reference)
        result = compile_document_map_revision(previous, previous)
        self.assertEqual(result["receipt"]["changed_node_ids"], [])

    def test_legacy_native_unit_id_is_preserved_without_guessing_a_new_unit(self):
        reference = deepcopy(self.reference)
        reference["logical_tables"][0]["owner_unit_id"] = "native-document-unit"
        previous = core.compile_document_map_artifact(reference)
        self.assertTrue(compile_document_map_revision(previous, previous)["receipt"]["source_conserved"])
        reference["logical_tables"][0]["owner_unit_id"] = "unproven-document-unit"
        with self.assertRaisesRegex(DocumentMapRevisionError, "UNIT_DRIFT"):
            compile_document_map_revision(previous, core.compile_document_map_artifact(reference))


if __name__ == "__main__":
    unittest.main()
