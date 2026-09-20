from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from pydantic import ValidationError

from app.application.extraction.agent_table_boundaries import (
    AgentTableParentBoundaryV1,
    AgentTableRowBindingV1,
    agent_boundaries_to_manual_row_groups,
    build_agent_table_parent_boundary,
)
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.domain.extraction.table_records import (
    TableRecordProjectionDecisionOrigin,
)


SOURCE_SHA = "a" * 64
ARTIFACT_ID = "agent-artifact-" + "b" * 32
ARTIFACT_SHA = "c" * 64
LOGICAL_TABLE_ID = "logical-table-1"


def _row(table_id: str, row_index: int, cell_offset: int) -> AgentTableRowBindingV1:
    return AgentTableRowBindingV1(
        table_id=table_id,
        row_index=row_index,
        cell_ids=(f"cell-{cell_offset:032x}",),
    )


def _boundary(
    parent_id: str,
    source_order: int,
    rows: tuple[AgentTableRowBindingV1, ...],
) -> AgentTableParentBoundaryV1:
    return build_agent_table_parent_boundary(
        source_sha256=SOURCE_SHA,
        boundary_artifact_id=ARTIFACT_ID,
        boundary_artifact_sha256=ARTIFACT_SHA,
        logical_table_id=LOGICAL_TABLE_ID,
        parent_provision_id=parent_id,
        source_order=source_order,
        row_bindings=rows,
        source_refs=tuple(
            StructureSourceRef(
                kind=StructureSourceRefKind.TABLE_CELL,
                source_id=cell_id,
            )
            for row in rows
            for cell_id in row.cell_ids
        ),
    )


class AgentTableBoundaryTests(unittest.TestCase):
    def test_agent_boundaries_become_exact_manual_row_groups(self) -> None:
        boundaries = (
            _boundary(
                "parent-1",
                0,
                (_row("table-a", 0, 1), _row("table-a", 1, 2)),
            ),
            _boundary("parent-2", 1, (_row("table-b", 0, 3),)),
        )

        groups = agent_boundaries_to_manual_row_groups(
            boundaries,
            expected_source_sha256=SOURCE_SHA,
            expected_boundary_artifact_id=ARTIFACT_ID,
            expected_boundary_artifact_sha256=ARTIFACT_SHA,
            expected_logical_table_id=LOGICAL_TABLE_ID,
            eligible_row_coordinates=(
                ("table-a", 0),
                ("table-a", 1),
                ("table-b", 0),
            ),
        )

        self.assertEqual(
            [group.row_coordinates for group in groups],
            [
                (("table-a", 0), ("table-a", 1)),
                (("table-b", 0),),
            ],
        )
        self.assertEqual(TableRecordProjectionDecisionOrigin.AGENT.value, "AGENT")

    def test_agent_boundary_adapter_rejects_gap_overlap_and_lineage_drift(self) -> None:
        first = _boundary("parent-1", 0, (_row("table-a", 0, 1),))
        second = _boundary("parent-2", 1, (_row("table-a", 1, 2),))
        kwargs = {
            "expected_source_sha256": SOURCE_SHA,
            "expected_boundary_artifact_id": ARTIFACT_ID,
            "expected_boundary_artifact_sha256": ARTIFACT_SHA,
            "expected_logical_table_id": LOGICAL_TABLE_ID,
        }

        with self.assertRaisesRegex(ValueError, "exactly partition"):
            agent_boundaries_to_manual_row_groups(
                (first, second),
                eligible_row_coordinates=(
                    ("table-a", 0),
                    ("table-a", 1),
                    ("table-a", 2),
                ),
                **kwargs,
            )
        overlap = _boundary("parent-overlap", 1, (_row("table-a", 0, 1),))
        with self.assertRaisesRegex(ValueError, "exactly partition"):
            agent_boundaries_to_manual_row_groups(
                (first, overlap),
                eligible_row_coordinates=(("table-a", 0),),
                **kwargs,
            )
        with self.assertRaisesRegex(ValueError, "lineage"):
            agent_boundaries_to_manual_row_groups(
                (first, second),
                eligible_row_coordinates=(
                    ("table-a", 0),
                    ("table-a", 1),
                ),
                **{**kwargs, "expected_boundary_artifact_sha256": "d" * 64},
            )

    def test_boundary_hash_rejects_tampering(self) -> None:
        boundary = _boundary("parent-1", 0, (_row("table-a", 0, 1),))
        payload = boundary.model_dump(mode="json")
        payload["row_bindings"][0]["row_index"] = 1

        with self.assertRaisesRegex(ValidationError, "does not bind"):
            AgentTableParentBoundaryV1.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
