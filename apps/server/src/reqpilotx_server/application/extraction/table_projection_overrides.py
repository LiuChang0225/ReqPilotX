from __future__ import annotations

import copy
from typing import Any

from reqpilotx_server.adapters.persistence.table_projection_revisions import (
    decode_projection_revision,
    list_effective_table_projection_revisions,
)
from reqpilotx_server.domain.extraction.table_evidence import TableEvidenceBindingV1
from reqpilotx_server.domain.extraction.table_records import TableRecordProjectionV1


def apply_table_projection_overrides(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    payload: dict[str, Any],
    revision_heads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Overlay exact-lineage manual projections without mutating run artifacts."""

    raw_projections = payload.get("table_record_projections")
    if not isinstance(raw_projections, list) or not raw_projections:
        return payload
    projections = tuple(
        TableRecordProjectionV1.model_validate(item)
        for item in raw_projections
        if isinstance(item, dict)
    )
    if not projections:
        return payload
    structure_snapshot_ids = {
        projection.table_fact.structure_snapshot_id
        for projection in projections
    }
    if len(structure_snapshot_ids) != 1:
        raise ValueError(
            "table projections must share one effective structure snapshot"
        )
    structure_snapshot_id = next(iter(structure_snapshot_ids))
    heads = (
        list_effective_table_projection_revisions(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            structure_snapshot_id=structure_snapshot_id,
        )
        if revision_heads is None
        else list(revision_heads)
    )
    if not heads:
        return payload

    projection_by_logical_id = {
        projection.table_fact.logical_table_id: projection
        for projection in projections
    }
    evidence_by_logical_id: dict[str, list[TableEvidenceBindingV1]] = {}
    for item in payload.get("table_evidence_bindings") or []:
        if not isinstance(item, dict):
            continue
        binding = TableEvidenceBindingV1.model_validate(item)
        evidence_by_logical_id.setdefault(binding.logical_table_id, []).append(
            binding
        )

    revision_heads: list[dict[str, Any]] = []
    changed = False
    for head in heads:
        logical_table_id = str(head.get("logical_table_id") or "")
        if logical_table_id not in projection_by_logical_id:
            continue
        action = str(head.get("action") or "").upper()
        if action not in {
            "APPLY_AGENT_BOUNDARIES",
            "SPLIT_ROWS",
            "MERGE_PREVIOUS",
            "MERGE_NEXT",
            "KEEP_WHOLE",
            "RESTORE_AUTO",
        }:
            raise ValueError("stored table projection revision action is invalid")
        projection_payload, evidence_payload = decode_projection_revision(head)
        revision_heads.append(
            {
                "logical_table_id": logical_table_id,
                "revision": int(head.get("revision") or 0),
                "action": action,
                "created_at": str(head.get("created_at") or ""),
                "active": action != "RESTORE_AUTO",
            }
        )
        if action == "RESTORE_AUTO":
            if projection_payload is not None or evidence_payload:
                raise ValueError(
                    "stored RESTORE_AUTO revision contains replacement data"
                )
            continue
        if projection_payload is None:
            raise ValueError("active table projection revision has no projection")
        projection = TableRecordProjectionV1.model_validate(
            projection_payload
        )
        automatic_projection = projection_by_logical_id[logical_table_id]
        if projection.table_fact.logical_table_id != logical_table_id:
            raise ValueError(
                "stored table projection revision targets another logical table"
            )
        if projection.table_fact != automatic_projection.table_fact:
            raise ValueError(
                "stored table projection revision does not bind the current "
                "source and effective structure"
            )
        bindings = [
            TableEvidenceBindingV1.model_validate(item)
            for item in evidence_payload
        ]
        if any(binding.logical_table_id != logical_table_id for binding in bindings):
            raise ValueError(
                "stored table projection evidence targets another logical table"
            )
        if any(
            (
                binding.projection_id != projection.projection_id
                or binding.projection_revision
                != projection.projection_revision
                or binding.projection_sha256
                != projection.projection_sha256
                or binding.source_table_ids
                != projection.table_fact.table_ids
            )
            for binding in bindings
        ):
            raise ValueError(
                "stored table projection evidence does not bind its projection"
            )
        expected_record_ids: set[str | None] = {
            None,
            *(record.record_id for record in projection.records),
        }
        actual_record_ids = [binding.record_id for binding in bindings]
        if (
            set(actual_record_ids) != expected_record_ids
            or len(actual_record_ids) != len(expected_record_ids)
        ):
            raise ValueError(
                "stored table projection evidence does not cover the whole "
                "table and every derived record exactly once"
            )
        projection_by_logical_id[logical_table_id] = projection
        evidence_by_logical_id[logical_table_id] = bindings
        changed = True

    if not changed and not revision_heads:
        return payload
    output = copy.deepcopy(payload)
    output["table_record_projections"] = [
        projection_by_logical_id[
            projection.table_fact.logical_table_id
        ].model_dump(mode="json")
        for projection in projections
    ]
    output["table_evidence_bindings"] = [
        binding.model_dump(mode="json")
        for projection in projections
        for binding in evidence_by_logical_id.get(
            projection.table_fact.logical_table_id,
            [],
        )
    ]
    counts = (
        dict(output.get("counts") or {})
        if isinstance(output.get("counts"), dict)
        else {}
    )
    counts.update(
        {
            "table_record_projections": len(
                output["table_record_projections"]
            ),
            "table_records": sum(
                len(projection_by_logical_id[
                    projection.table_fact.logical_table_id
                ].records)
                for projection in projections
            ),
            "table_evidence_bindings": len(
                output["table_evidence_bindings"]
            ),
        }
    )
    output["counts"] = counts
    output["table_projection_revision_heads"] = revision_heads
    return output


__all__ = ["apply_table_projection_overrides"]
