"""Real public compiler regressions for non-ancestor customer context."""
from copy import deepcopy
from argparse import Namespace
import json

import pytest

from reqpilotx.application.agent_workflow import source_requirement_baseline as core
from reqpilotx.application.agent_workflow.source_item_visual import compile_source_item_annotation_updates
from reqpilotx.application.agent_workflow.source_item_analysis import build_source_item_analysis_packet, compile_source_item_assessments, rebind_source_item_assessments
from reqpilotx.application.agent_workflow.source_item_context import SourceItemContextError
from test_integration import service
from portable_fixtures import item_decisions, write
import artifact_tools


def inputs(service):
    directory = service.baseline_dir
    mapping = core.DocumentMapArtifactV1.model_validate_json((directory / "document-map.json").read_text())
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate_json((directory / "source-requirement-baseline.json").read_text())
    tables = json.loads((directory / "table-previews.json").read_text())
    _, catalog = service.store.snapshot()
    return mapping, baseline, tables, catalog


def annotate(mapping, baseline, tables, links, previous=None):
    return compile_source_item_annotation_updates(mapping, baseline,
        [{"source_item_id": owner, "context_source_ids": targets, "reason": "Explicit synthetic customer source relationship"} for owner, targets in links],
        base_annotations=previous, table_previews=tables)


def test_nonancestor_context_contains_original_text_and_native_locator(service):
    mapping, baseline, tables, catalog = inputs(service)
    a = next(r for r in baseline.records if r.node_id == "s1")
    b = next(r for r in baseline.records if r.node_id == "s2")
    annotations = annotate(mapping, baseline, tables, [(a.source_record_id, [b.source_record_id])])
    packet = build_source_item_analysis_packet(mapping, baseline, tables, catalog,
        selected_unit_ids=[a.source_record_id], image_annotations=annotations)
    unit = packet["units"][0]
    context = unit["referenced_customer_context"]
    assert [item["node_id"] for item in unit["document_context"]] == ["main", "s1"]
    referenced = context["source_items"][0]
    assert referenced["source_item_id"] == b.source_record_id
    assert referenced["source_segments"][0]["text"] == b.text_bindings[0].text
    assert referenced["source_segments"][0]["source_ref"]["content_id"] == b.text_bindings[0].content_id
    assert referenced["source_segments"][0]["bbox"] == list(b.text_bindings[0].bbox)
    assert referenced["role"] == "REFERENCED_CUSTOMER_CONTEXT"
    assert referenced["interpretation"] == "DECLARED_CONTEXT_RELATION_NOT_AN_ADDITIONAL_OBLIGATION"
    assert len(packet["units"]) == 1 and packet["unit_registry"][a.source_record_id]["unit_sha256"] == core.canonical_json_sha256(unit)
    assert len(context["dependencies_sha256"]) == 64


def test_changed_indirect_condition_invalidates_only_dependent_units(service):
    mapping, baseline, tables, catalog = inputs(service)
    a, b, c = [next(r for r in baseline.records if r.node_id == node) for node in ("s1", "s2", "s3")]
    initial = annotate(mapping, baseline, tables, [(a.source_record_id, [b.source_record_id])])
    old = build_source_item_analysis_packet(mapping, baseline, tables, catalog, image_annotations=initial)
    artifact = compile_source_item_assessments(old, {"decisions": [{"unit_id": unit["unit_id"], "decision": "PENDING_EVIDENCE",
        "reason": "No current controlled evidence.", "evidence": [], "clarification_question": "Provide controlled current evidence."} for unit in old["units"]]})
    changed = annotate(mapping, baseline, tables, [(b.source_record_id, [c.source_record_id])], previous=initial)
    new = build_source_item_analysis_packet(mapping, baseline, tables, catalog, image_annotations=changed)
    expanded = next(unit for unit in new["units"] if unit["unit_id"] == a.source_record_id)["referenced_customer_context"]
    assert {item["source_item_id"] for item in expanded["source_items"]} == {b.source_record_id, c.source_record_id}
    carried = rebind_source_item_assessments(old, artifact, new)
    assert set(carried["stale_unit_ids"]) == {a.source_record_id, b.source_record_id}
    assert c.source_record_id in carried["retained_unit_ids"]
    assert len(carried["retained_unit_ids"]) == len(old["units"]) - 2


def test_cycles_expand_finitely_and_do_not_duplicate_obligations(service):
    mapping, baseline, tables, catalog = inputs(service)
    a, b = [next(r for r in baseline.records if r.node_id == node) for node in ("s1", "s2")]
    annotations = annotate(mapping, baseline, tables, [(a.source_record_id, [b.source_record_id]), (b.source_record_id, [a.source_record_id])])
    packet = build_source_item_analysis_packet(mapping, baseline, tables, catalog, selected_unit_ids=[a.source_record_id], image_annotations=annotations)
    context = packet["units"][0]["referenced_customer_context"]
    assert len(context["source_items"]) == 1
    assert context["cycle_paths"] == [[a.source_record_id, b.source_record_id, a.source_record_id]]
    assert len(packet["units"]) == 1


def test_unknown_retired_or_foreign_source_references_fail_explicitly(service):
    mapping, baseline, tables, catalog = inputs(service)
    a = next(r for r in baseline.records if r.node_id == "s1")
    annotations = annotate(mapping, baseline, tables, [(a.source_record_id, [])])
    # A re-sealed malformed object is not proof of an existing original relation.
    for foreign in ("foreign-document-SRC-001", "retired-source-id"):
        bad = deepcopy(annotations); bad["records"][a.source_record_id]["context_source_ids"] = [foreign]
        from reqpilotx.application.agent_workflow.source_item_visual import _seal_annotations
        bad = _seal_annotations(bad)
        with pytest.raises(SourceItemContextError, match="same-document"):
            build_source_item_analysis_packet(mapping, baseline, tables, catalog, image_annotations=bad)
    changed = deepcopy(annotations); changed["source_sha256"] = "f" * 64
    from reqpilotx.application.agent_workflow.source_item_visual import _seal_annotations
    with pytest.raises(ValueError, match="another original Map"):
        build_source_item_analysis_packet(mapping, baseline, tables, catalog, image_annotations=_seal_annotations(changed))


@pytest.mark.parametrize("service", [{"table_image": True}], indirect=True)
def test_referenced_table_and_supporting_image_remain_complete_context(service):
    mapping, baseline, tables, catalog = inputs(service)
    a = next(r for r in baseline.records if r.node_id == "s1")
    table = next(r for r in baseline.records if r.table_fragments)
    annotations = annotate(mapping, baseline, tables, [(a.source_record_id, [table.source_record_id])])
    packet = build_source_item_analysis_packet(mapping, baseline, tables, catalog, selected_unit_ids=[a.source_record_id], image_annotations=annotations)
    context = packet["units"][0]["referenced_customer_context"]["source_items"][0]
    assert context["source_segments"] and context["shared_table_context"]
    assert any(item["text"] == "Response" for item in context["source_segments"])
    assert any(item["text"] == "Criterion" for item in context["shared_table_context"])
    assert all(item["source_ref"]["kind"] == "TABLE_CELL" for item in context["source_segments"])
    assert context["image_refs"]
    assert all(item["source_sha256"] == baseline.source_sha256 for item in context["image_refs"])


def test_shared_content_ids_resolve_from_current_map_without_new_ownership(service):
    mapping, baseline, tables, catalog = inputs(service)
    a = next(r for r in baseline.records if r.node_id == "s1")
    heading = next(c for n in mapping.nodes if n.node_id == "s1" for c in n.source_content if c.is_title)
    # Use the public compiler, preserving exact source coverage and ownership.
    map_dir = service.baseline_dir.parent / "map"
    decisions = item_decisions(map_dir)
    target = next(item for item in decisions["text_decisions"] if item["scope_id"] == "text:s1")["items"][0]
    target["shared_source_content_ids"] = [heading.content_id]
    path = service.baseline_dir.parent / "shared-items.json"; write(path, decisions)
    output = service.baseline_dir.parent / "shared-baseline"
    artifact_tools.compile_items(Namespace(map_dir=map_dir, input=path, baseline_dir=None, output_dir=output))
    modified = core.SourceRequirementBaselineArtifactV1.model_validate_json((output / "source-requirement-baseline.json").read_text())
    packet = build_source_item_analysis_packet(mapping, modified, tables, catalog, selected_unit_ids=[a.source_record_id])
    context = packet["units"][0]["referenced_customer_context"]
    assert context["shared_source_content"][0]["text"] == heading.text
    assert context["shared_source_content"][0]["role"] == "SHARED_CUSTOMER_SOURCE_CONTENT"
    assert packet["units"][0]["source_segments"][0]["text"] == a.text_bindings[0].text


def test_unrelated_missing_table_preview_preserves_existing_visual_fallback(service):
    mapping, baseline, tables, catalog = inputs(service)
    a = next(r for r in baseline.records if r.node_id == "s1")
    b = next(r for r in baseline.records if r.node_id == "s2")
    annotations = annotate(mapping, baseline, tables, [(a.source_record_id, [b.source_record_id])])
    # The table is not part of A's declared context; previous analysis permits
    # missing readable previews to retain visual/page evidence for that table.
    packet = build_source_item_analysis_packet(mapping, baseline, {}, catalog,
        selected_unit_ids=[a.source_record_id], image_annotations=annotations)
    assert packet["units"][0]["referenced_customer_context"]["source_items"][0]["source_item_id"] == b.source_record_id
    table = next(r for r in baseline.records if r.table_fragments)
    with pytest.raises(SourceItemContextError, match="same-original previews"):
        explicit = annotate(mapping, baseline, tables, [(a.source_record_id, [table.source_record_id])])
        build_source_item_analysis_packet(mapping, baseline, {}, catalog,
            selected_unit_ids=[a.source_record_id], image_annotations=explicit)
