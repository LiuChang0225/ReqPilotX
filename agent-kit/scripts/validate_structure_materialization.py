#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any
import unicodedata


OBSERVATION_SCHEMA_VERSION = "structure-materialization-observation/v1"
REPORT_SCHEMA_VERSION = "structure-materialization-validation-report/v1"
_DISPOSITIONS = {"PLACED_IN_CELL", "FLOATING_IN_TABLE", "UNRESOLVED"}


class StructureMaterializationValidationError(RuntimeError):
    pass


def _normalized_text(value: object) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip()


def _bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result  # type: ignore[return-value]


def _contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
) -> bool:
    return (
        inner[0] >= outer[0]
        and inner[1] >= outer[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def validate_materialization(observation: dict[str, Any]) -> dict[str, Any]:
    if observation.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        raise StructureMaterializationValidationError(
            "structure_materialization_observation_schema_invalid"
        )
    findings: list[dict[str, Any]] = []

    confirmed_actions = observation.get("confirmed_actions", [])
    if not isinstance(confirmed_actions, list):
        raise StructureMaterializationValidationError(
            "structure_materialization_confirmed_actions_invalid"
        )
    incomplete_actions = []
    incomplete_action_signals: set[str] = set()
    for item in confirmed_actions:
        if not isinstance(item, dict):
            incomplete_actions.append("")
            incomplete_action_signals.add("required_host_check_not_executed")
            continue
        action_id = str(item.get("action_id") or "")
        action_incomplete = False
        if not str(item.get("materialization_receipt_id") or ""):
            action_incomplete = True
            incomplete_action_signals.add(
                "accepted_action_has_no_materialization_receipt"
            )
        if not str(item.get("verification_receipt_id") or ""):
            action_incomplete = True
            incomplete_action_signals.add(
                "confirmed_status_without_postcondition_receipt"
            )
        if item.get("rendered_output_reopened") is not True:
            action_incomplete = True
            incomplete_action_signals.add("rendered_output_not_reopened")
        if action_incomplete:
            incomplete_actions.append(action_id)
    if incomplete_actions:
        findings.append(
            {
                "pattern_id": "confirmed_proposal_not_verified",
                "signals": sorted(incomplete_action_signals),
                "evidence": {"incomplete_action_ids": incomplete_actions},
            }
        )

    parent_refs = set(map(str, observation.get("parent_direct_source_refs", [])))
    child_refs = set(map(str, observation.get("child_owned_source_refs", [])))
    overlap = sorted(parent_refs.intersection(child_refs))
    if overlap:
        findings.append(
            {
                "pattern_id": "parent_child_evidence_duplication",
                "signals": ["duplicate_source_handle_across_visible_owners"],
                "evidence": {"duplicate_source_refs": overlap},
            }
        )

    title = _normalized_text(observation.get("node_title", ""))
    title_lines = [
        _normalized_text(item)
        for item in observation.get("visible_title_lines", [])
        if _normalized_text(item)
    ]
    duplicate_title_count = sum(line == title for line in title_lines)
    if title and duplicate_title_count > 1:
        findings.append(
            {
                "pattern_id": "repeated_title_extraction",
                "signals": ["visible_title_duplicate_lines"],
                "evidence": {"duplicate_title_count": duplicate_title_count},
            }
        )

    visual_scope = _bbox(observation.get("visual_scope_bbox"))
    rendered_cells = observation.get("rendered_cells", [])
    following_headings = {
        _normalized_text(item)
        for item in observation.get("following_heading_texts", [])
        if _normalized_text(item)
    }
    leaking_cells: list[str] = []
    heading_cells: list[str] = []
    if not isinstance(rendered_cells, list):
        raise StructureMaterializationValidationError(
            "structure_materialization_cells_invalid"
        )
    for cell in rendered_cells:
        if not isinstance(cell, dict):
            raise StructureMaterializationValidationError(
                "structure_materialization_cell_invalid"
            )
        cell_id = str(cell.get("cell_id") or "")
        cell_bbox = _bbox(cell.get("bbox"))
        if visual_scope is not None and (
            cell_bbox is None or not _contains(visual_scope, cell_bbox)
        ):
            leaking_cells.append(cell_id)
        cell_text = _normalized_text(cell.get("text", ""))
        if any(heading and heading in cell_text for heading in following_headings):
            heading_cells.append(cell_id)
    if leaking_cells or heading_cells:
        signals = []
        if leaking_cells:
            signals.append("cell_bbox_exceeds_visual_scope")
        if heading_cells:
            signals.append("cell_text_contains_following_heading")
        findings.append(
            {
                "pattern_id": "visual_subcell_scope_leakage",
                "signals": signals,
                "evidence": {
                    "cells_outside_visual_scope": sorted(leaking_cells),
                    "cells_containing_following_headings": sorted(heading_cells),
                },
            }
        )

    segments = observation.get("continuation_segments", [])
    if not isinstance(segments, list):
        raise StructureMaterializationValidationError(
            "structure_materialization_segments_invalid"
        )
    contracts = [
        tuple(str(column) for column in item.get("canonical_columns", []))
        for item in segments
        if isinstance(item, dict)
    ]
    if len(contracts) > 1 and any(contract != contracts[0] for contract in contracts[1:]):
        contract_signals = []
        if any(len(contract) != len(contracts[0]) for contract in contracts[1:]):
            contract_signals.append("continuation_column_count_changes")
        if any(
            len(contract) == len(contracts[0]) and contract != contracts[0]
            for contract in contracts[1:]
        ):
            contract_signals.append("continuation_header_contract_changes")
        findings.append(
            {
                "pattern_id": "continuation_column_topology_drift",
                "signals": contract_signals,
                "evidence": {
                    "segment_column_counts": [len(contract) for contract in contracts]
                },
            }
        )

    expected_images = tuple(map(str, observation.get("expected_image_ids", [])))
    dispositions = observation.get("image_dispositions", [])
    if not isinstance(dispositions, list):
        raise StructureMaterializationValidationError(
            "structure_materialization_image_dispositions_invalid"
        )
    disposition_counts: dict[str, int] = {}
    invalid_dispositions: list[str] = []
    for item in dispositions:
        if not isinstance(item, dict):
            raise StructureMaterializationValidationError(
                "structure_materialization_image_disposition_invalid"
            )
        image_id = str(item.get("image_id") or "")
        disposition = str(item.get("disposition") or "")
        disposition_counts[image_id] = disposition_counts.get(image_id, 0) + 1
        if disposition not in _DISPOSITIONS:
            invalid_dispositions.append(image_id)
    missing_images = sorted(
        image_id for image_id in expected_images if disposition_counts.get(image_id) != 1
    )
    unexpected_images = sorted(
        image_id for image_id in disposition_counts if image_id not in set(expected_images)
    )
    if missing_images or unexpected_images or invalid_dispositions:
        findings.append(
            {
                "pattern_id": "cross_cell_visual_placement_drop",
                "signals": ["image_not_dispositioned"],
                "evidence": {
                    "missing_or_duplicate_image_ids": missing_images,
                    "unexpected_image_ids": unexpected_images,
                    "invalid_disposition_image_ids": sorted(invalid_dispositions),
                },
            }
        )

    navigation_children = observation.get("navigation_children", [])
    if not isinstance(navigation_children, list):
        raise StructureMaterializationValidationError(
            "structure_materialization_navigation_children_invalid"
        )
    oversegmented = sorted(
        str(item.get("child_id") or "")
        for item in navigation_children
        if isinstance(item, dict)
        and item.get("source_kind") == "INLINE_LIST_ITEM"
        and item.get("independently_navigable") is not True
    )
    if oversegmented:
        findings.append(
            {
                "pattern_id": "minor_list_navigation_oversegmentation",
                "signals": ["inline_list_items_promoted_to_tree_nodes"],
                "evidence": {"oversegmented_child_ids": oversegmented},
            }
        )

    findings.sort(key=lambda item: item["pattern_id"])
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "PASS" if not findings else "FAIL",
        "finding_count": len(findings),
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        observation = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(observation, dict):
            raise StructureMaterializationValidationError(
                "structure_materialization_observation_invalid"
            )
        result = validate_materialization(observation)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        StructureMaterializationValidationError,
    ) as exc:
        print(json.dumps({"reason_code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
