"""Source-bound TEXT/TABLE scope revision adapters; no semantic decisions."""
from copy import deepcopy

from reqpilotx.application.agent_workflow import source_requirement_baseline as core


def scope_template(document_map, baseline, tables, scope_id):
    records = [record for record in baseline.records if core._record_scope_id(record) == scope_id]
    if scope_id.startswith("text:"):
        mixed = [record for record in baseline.records if record.table_fragments and record.text_bindings
                 and f"text:{record.node_id}" == scope_id]
        request = core.build_source_item_text_scope_request(document_map, node_id=scope_id.removeprefix("text:"))
        items = []
        for record in records:
            items.append({"item_key": "existing-" + record.source_display_id.lower(), "label": record.label,
                "source_form": record.source_form.value,
                "text_spans": [{"content_id": binding.content_id, "start_offset": binding.start_offset, "end_offset": binding.end_offset} for binding in record.text_bindings],
                "shared_source_content_ids": list(record.shared_source_content_ids), "requirementness": record.requirementness.value,
                "requirement_kinds": [kind.value for kind in record.requirement_kinds], "non_requirement_role": record.non_requirement_role.value if record.non_requirement_role else None,
                "confidence": record.confidence, "decision_summary": record.decision_summary, "reason_codes": list(record.reason_codes)})
        candidate = {"scope_id": scope_id, "items": items, "structure_only_spans": [
            {"text_span": {"content_id": item.content_id, "start_offset": item.start_offset, "end_offset": item.end_offset}, "reason_codes": list(item.reason_codes)}
            for item in baseline.text_context_dispositions if item.scope_id == scope_id],
            "table_owned_spans": [{"logical_table_scope_id": record.logical_table_scope_id,
                "record_key": "existing-" + record.source_display_id.lower(),
                "text_span": {"content_id": binding.content_id, "start_offset": binding.start_offset,
                    "end_offset": binding.end_offset}}
                for record in mixed for binding in record.text_bindings]}
        return {"scope_kind": "TEXT", "request": request.model_dump(mode="json"), "candidate_template": candidate,
            "existing_source_item_ids": [record.source_record_id for record in (*records, *mixed)],
            "coupled_scope_ids": sorted({record.logical_table_scope_id for record in mixed})}
    if scope_id not in tables:
        raise ValueError("The selected table scope has no original preview")
    request = core.build_source_requirement_scope_request(document_map, tables[scope_id])
    items = []
    for record in records:
        items.append({"record_key": "existing-" + record.source_display_id.lower(), "label": record.label, "source_form": record.source_form.value,
            "row_groups": [{"fragment_id": binding.fragment_id, "row_start": binding.row_start, "row_end": binding.row_end} for binding in record.table_fragments],
            "owning_text_spans": [{"content_id": binding.content_id, "start_offset": binding.start_offset,
                "end_offset": binding.end_offset} for binding in record.text_bindings],
            "shared_source_content_ids": list(record.shared_source_content_ids),
            "requirementness": record.requirementness.value, "requirement_kinds": [kind.value for kind in record.requirement_kinds],
            "non_requirement_role": record.non_requirement_role.value if record.non_requirement_role else None,
            "confidence": record.confidence, "decision_summary": record.decision_summary, "reason_codes": list(record.reason_codes)})
    contexts = [{"role": item.role.value, "row_group": {"fragment_id": item.fragment_id, "row_start": item.row_start, "row_end": item.row_end}, "reason_codes": list(item.reason_codes),
        **({"exact_placeholder_values": list(item.exact_placeholder_values), "empty_template_reason": item.empty_template_reason} if item.exact_placeholder_values else {})}
        for item in baseline.context_dispositions if item.logical_table_scope_id == scope_id]
    return {"scope_kind": "TABLE", "request": request.model_dump(mode="json"), "candidate_template": {"logical_table_scope_id": scope_id, "records": items, "context_row_groups": contexts},
        "existing_source_item_ids": [record.source_record_id for record in records],
        "coupled_scope_ids": sorted({f"text:{record.node_id}" for record in records if record.text_bindings})}


def compile_scope(document_map, previous, receipt, tables, value, *, feedback_ids, previous_scope_receipt=None, undo_target=None,
        source_display_high_watermark_floor=None):
    """Rebuild canonical requests from actual source facts and replay the core."""
    if not feedback_ids:
        raise ValueError("A scope revision must cite actual recorded feedback or self-review findings")
    operation = value.get("operation", "REPLACE")
    affected = value.get("affected_scope_ids", [])
    if not affected or len(affected) != len(set(affected)) or set(affected) - set(previous.covered_scope_ids):
        raise ValueError("Scope replacement needs explicit unique already-covered source scopes")
    text_requests, text_candidates, requests, candidates = [], [], [], []
    seen = set()
    for kind, selections in (("TEXT", value.get("text_decisions", [])), ("TABLE", value.get("table_decisions", []))):
        for selection in selections:
            key = "scope_id" if kind == "TEXT" else "logical_table_scope_id"
            scope_id = selection[key]
            if scope_id in seen or scope_id not in affected:
                raise ValueError("Scope candidates must match the explicit affected scope exactly once")
            seen.add(scope_id)
            template = scope_template(document_map, previous, tables, scope_id)
            if template["scope_kind"] != kind:
                raise ValueError("Text/table candidate kind differs from the original scope")
            request_type = core.SourceItemTextScopeRequestV1 if kind == "TEXT" else core.SourceRequirementScopeRequestV1
            candidate_type = core.SourceItemTextSliceCandidateV1 if kind == "TEXT" else core.SourceRequirementSliceCandidateV1
            request = request_type.model_validate(template["request"])
            payload = deepcopy(selection)
            payload.update(source_sha256=document_map.source_sha256, document_map_artifact_id=document_map.artifact_id,
                document_map_artifact_sha256=document_map.artifact_sha256, request_sha256=request.request_sha256)
            candidate = candidate_type.model_validate(payload)
            if kind == "TEXT":
                text_requests.append(request); text_candidates.append(candidate)
            else:
                requests.append(request); candidates.append(candidate)
    arguments = {"document_map": document_map, "current_artifact": previous, "current_validation_receipt": receipt,
        "requests": tuple(requests), "candidates": tuple(candidates), "text_requests": tuple(text_requests), "text_candidates": tuple(text_candidates),
        "undo_target_artifact": undo_target, "previous_scope_revision_receipt": previous_scope_receipt,
        "source_display_high_watermark_floor": source_display_high_watermark_floor}
    revision = core.build_source_item_scope_revision(**arguments, operation=operation, affected_scope_ids=tuple(affected),
        comparison_scope_ids=tuple(value.get("comparison_scope_ids", [])), feedback_ids=tuple(feedback_ids))
    baseline, validation, proof = core.compile_source_item_scope_revision(**arguments, scope_revision=revision)
    return baseline, validation, revision, proof


def scope_diff(previous, current, proof):
    before = {record.source_record_id: record.model_dump(mode="json") for record in previous.records}
    after = {record.source_record_id: record.model_dump(mode="json") for record in current.records}
    common = sorted(set(before) & set(after)); changes = []
    for sid in common:
        fields = {key: {"before": before[sid].get(key), "after": after[sid].get(key)} for key in set(before[sid]) | set(after[sid])
            if key != "source_order" and before[sid].get(key) != after[sid].get(key)}
        if fields:
            changes.append({"source_item_id": sid, "source_display_id": before[sid]["source_display_id"], "fields": fields})
    created = {item.source_record_id for item in proof.created_identities}
    restored = (set(after) - set(before)) - created
    retired = set(before) - set(after)
    return {"schema_version": "reqpilot-revision-diff/v1", "transition_kind": "SOURCE_SCOPE_REVISION", "source_sha256": current.source_sha256,
        "predecessor_baseline_sha256": previous.artifact_sha256, "baseline_sha256": current.artifact_sha256,
        "preserved_source_item_count": len(common), "changed_source_item_count": len(changes) + len(created | restored | retired), "changes": changes,
        "created_source_item_ids": sorted(created), "restored_source_item_ids": sorted(restored), "retired_source_item_ids": sorted(retired),
        "affected_scope_ids": list(proof.affected_scope_ids), "successor_relations": [item.model_dump(mode="json") for item in proof.successor_relations],
        "source_display_high_watermark_before": proof.source_display_id_high_watermark_before,
        "source_display_high_watermark_after": proof.source_display_id_high_watermark_after,
        "coverage_before_sha256": proof.coverage_before_sha256, "coverage_after_sha256": proof.coverage_after_sha256,
        "source_coverage": "CONSERVED_BY_SHARED_SCOPE_COMPILER", "unaffected_scope_records": "UNCHANGED_EXCEPT_SOURCE_ORDER",
        "semantic_verification": "REQUIRES_SOURCE_REVIEW", "human_acceptance": "NOT_CLAIMED"}
