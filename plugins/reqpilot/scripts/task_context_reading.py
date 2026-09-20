"""Expose declared source context for Agent review without inferring applicability."""

from app.application.agent_workflow import source_requirement_baseline as core
from task_reading import _next


def context_inventory(document_map, baseline, record, annotation):
    records = {item.source_record_id: item for item in baseline.records}
    linked_ids = list(annotation.get("context_source_ids", []))
    if set(linked_ids) - set(records) or record.source_record_id in linked_ids:
        raise ValueError("Invalid same-baseline source context link")
    candidates = []
    for item in sorted(baseline.records, key=lambda item: item.source_order):
        if item.source_record_id == record.source_record_id:
            continue
        linked = item.source_record_id in linked_ids
        if not linked and item.non_requirement_role not in {
            core.NonRequirementRole.DEFINITION,
            core.NonRequirementRole.CONTEXT,
        }:
            continue
        candidates.append(
            {
                "source_item_id": item.source_record_id,
                "source_display_id": item.source_display_id,
                "source_role": item.non_requirement_role.value
                if item.non_requirement_role
                else None,
                "label_preview": item.label[:240],
                "pages": list(item.pages),
                "ancestor_path": list(item.ancestor_path),
                "relation": "EXPLICIT_CONTEXT_LINK"
                if linked
                else "SOURCE_ROLE_CANDIDATE",
                "applicability": "AGENT_SELECTED" if linked else "NOT_INFERRED",
                "read": _next(
                    "source_item_read",
                    {"source_item_id": item.source_display_id, "part": "source"},
                ),
            }
        )
    basis = core.canonical_json_sha256(
        {
            "map_sha256": document_map.artifact_sha256,
            "baseline_sha256": baseline.artifact_sha256,
            "source_item_id": record.source_record_id,
            "context_source_ids": linked_ids,
        }
    )

    def pinned(name):
        return _next(
            "source_item_read",
            {"source_item_id": record.source_display_id, "part": name + "@" + basis},
        )

    summary = {
        "context_bindings_sha256": basis,
        "linked_source_ids": linked_ids,
        "candidate_count": len(candidates),
        "candidate_previews": [
            {
                key: item[key]
                for key in (
                    "source_display_id",
                    "source_role",
                    "label_preview",
                    "relation",
                )
            }
            for item in candidates[:3]
        ],
        "read_candidates": pinned("context_candidates"),
        "read_linked_context": pinned("linked_context"),
        "selection": {
            "action": "annotations_apply",
            "field": "changes.annotation_updates[].context_source_ids",
            "source_item_id": record.source_display_id,
            "existing_context_source_ids": [
                records[sid].source_display_id for sid in linked_ids
            ],
            "semantics": "An explicit list replaces the existing set; retain still-valid links and explain the source applicability.",
        },
        "instruction": "Review definitions, conditions and scope across the source before independent analysis. These candidates use already-declared source roles, not keyword matches or automatic applicability. Read the original and link only governing context through the existing annotation action. An empty list does not prove context completeness; unrecognized definitions remain discoverable in the original.",
    }
    return candidates, summary, [records[sid] for sid in linked_ids]


def context_part(part, basis):
    name, marker, expected = (part or "").partition("@")
    if name not in {"context_candidates", "linked_context", "decomposition_packet"}:
        return part
    if marker and expected != basis:
        raise ValueError(
            "CONTEXT_VERSION_CONFLICT: source context links changed; reload this item"
        )
    return name
