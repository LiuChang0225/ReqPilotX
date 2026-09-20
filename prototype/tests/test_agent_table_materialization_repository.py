from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.adapters.persistence.memory_agent_table_materializations import (
    InMemoryAgentTableMaterializationRepository,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    build_agent_logical_table_scope,
)
from app.application.extraction.agent_table_boundaries import (
    AgentTableParentBoundaryV1,
    AgentTableRowBindingV1,
    build_agent_table_parent_boundary,
    materialize_agent_table_parent_boundaries,
)
from app.application.extraction.agent_table_materialization_contracts import (
    AgentTableMaterializationCommitRequestV1,
)
from app.application.extraction.agent_table_materializations import (
    commit_document_agent_table_materialization,
    get_document_agent_table_materialization,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import (
    get_base_effective_structure,
)
from app.application.extraction.table_evidence_projection import (
    build_table_evidence_bindings,
)
from app.application.extraction.table_projection_overrides import (
    apply_table_projection_overrides,
)
from app.application.extraction.table_record_projection import (
    build_table_record_projections,
)
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.ports.agent_table_materialization_repository import (
    AgentTableMaterializationConflict,
)
from tests.test_table_record_projection_builder import (
    _source_with_tables,
    _table_spec,
)


class AgentTableMaterializationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "要求"),
                        ("1", "第一条"),
                        ("", "第一条补充"),
                        ("2", "第二条"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        self.base_tree = build_structure_tree(self.source)
        self.structure = get_base_effective_structure(
            self.source,
            self.base_tree,
        )
        self.base_projection = build_table_record_projections(
            self.source,
            self.structure,
        )[0]
        self.d1_identity = AgentD1TableMapArtifactIdentityV1(
            source_sha256=self.source.source_sha256,
            artifact_id="agent-d1-table-map-fixture",
            artifact_sha256="d" * 64,
        )
        self.scope = build_agent_logical_table_scope(
            self.source,
            d1_table_map_artifact=self.d1_identity,
            physical_table_ids=self.base_projection.table_fact.table_ids,
            reason_codes=("same_logical_object",),
        )
        self.boundaries = self._boundaries(
            ((1, 2), (3,)),
            artifact_sha256="e" * 64,
        )
        self.materialization = self._materialization(
            base_projection=self.base_projection,
            boundaries=self.boundaries,
            artifact_sha256="e" * 64,
        )
        self.tables = tuple(
            {
                "id": part.table_id,
                "page_number": 1,
            }
            for part in self.base_projection.table_fact.table_parts
        )
        self.evidence = build_table_evidence_bindings(
            table_record_projections=(self.materialization.projection,),
            tables=self.tables,
        )
        self.repository = InMemoryAgentTableMaterializationRepository()

    def test_commit_is_atomic_idempotent_and_readable(self) -> None:
        record, created = self._commit(command_id="agent-table:1")

        self.assertTrue(created)
        self.assertEqual(record.table_projection_head_revision, 1)
        self.assertEqual(record.logical_table_scope.scope_sha256, self.scope.scope_sha256)
        self.assertEqual(
            record.logical_table_scope.d1_table_map_artifact,
            self.d1_identity,
        )
        self.assertEqual(
            self.repository.get(
                project_id="project-agent-table",
                document_id="document-agent-table",
                record_id=record.record_id,
            ),
            record,
        )

        replay, replay_created = self._commit(command_id="agent-table:1")
        self.assertFalse(replay_created)
        self.assertEqual(replay, record)

    def test_commit_rejects_stale_base_and_cross_scope_replay(self) -> None:
        first, _created = self._commit(command_id="agent-table:first")
        alternative_boundaries = self._boundaries(
            ((1,), (2, 3)),
            artifact_sha256="f" * 64,
        )
        alternative = self._materialization(
            base_projection=self.base_projection,
            boundaries=alternative_boundaries,
            artifact_sha256="f" * 64,
        )
        alternative_evidence = build_table_evidence_bindings(
            table_record_projections=(alternative.projection,),
            tables=self.tables,
        )
        with self.assertRaisesRegex(
            AgentTableMaterializationConflict,
            "base table projection changed",
        ):
            self.repository.commit(
                project_id="project-agent-table",
                document_id="document-agent-table",
                command_id="agent-table:stale",
                base_projection=self.base_projection,
                logical_table_scope=self.scope,
                materialization=alternative,
                evidence_bindings=alternative_evidence,
            )

        alternate_scope = build_agent_logical_table_scope(
            self.source,
            d1_table_map_artifact=self.d1_identity,
            physical_table_ids=self.base_projection.table_fact.table_ids,
            reason_codes=("same_logical_object_reconfirmed",),
        )
        with self.assertRaisesRegex(
            AgentTableMaterializationConflict,
            "lineage",
        ):
            self.repository.commit(
                project_id="project-agent-table",
                document_id="document-agent-table",
                command_id="agent-table:cross-scope",
                base_projection=first.materialization.projection,
                logical_table_scope=alternate_scope,
                materialization=self.materialization,
                evidence_bindings=self.evidence,
            )

    def test_current_agent_projection_can_advance_once_more(self) -> None:
        first, _created = self._commit(command_id="agent-table:first")
        next_materialization = self._materialization(
            base_projection=first.materialization.projection,
            boundaries=self.boundaries,
            artifact_sha256="e" * 64,
        )
        next_evidence = build_table_evidence_bindings(
            table_record_projections=(next_materialization.projection,),
            tables=self.tables,
        )

        second, created = self.repository.commit(
            project_id="project-agent-table",
            document_id="document-agent-table",
            command_id="agent-table:second",
            base_projection=first.materialization.projection,
            logical_table_scope=self.scope,
            materialization=next_materialization,
            evidence_bindings=next_evidence,
        )

        self.assertTrue(created)
        self.assertEqual(second.table_projection_head_revision, 2)
        self.assertEqual(
            second.materialization.projection.projection_revision,
            first.materialization.projection.projection_revision + 1,
        )

    def test_agent_revision_is_visible_through_existing_projection_overlay(self) -> None:
        projection = self.materialization.projection
        overridden = apply_table_projection_overrides(
            project_id="project-agent-table-overlay",
            document_id="document-agent-table-overlay",
            source_sha256=self.source.source_sha256,
            payload={
                "table_record_projections": [
                    self.base_projection.model_dump(mode="json")
                ],
                "table_evidence_bindings": [],
            },
            revision_heads=[
                {
                    "logical_table_id": projection.table_fact.logical_table_id,
                    "revision": 1,
                    "action": "APPLY_AGENT_BOUNDARIES",
                    "projection_json": json.dumps(
                        projection.model_dump(mode="json")
                    ),
                    "evidence_bindings_json": json.dumps(
                        [item.model_dump(mode="json") for item in self.evidence]
                    ),
                    "created_at": "2026-08-24T00:00:00+00:00",
                }
            ],
        )

        self.assertEqual(
            overridden["table_record_projections"][0]["projection_id"],
            projection.projection_id,
        )
        self.assertEqual(
            overridden["table_projection_revision_heads"][0]["action"],
            "APPLY_AGENT_BOUNDARIES",
        )

    def test_request_requires_scope_materialization_lineage(self) -> None:
        request = self._request()
        self.assertEqual(request.logical_table_scope.scope_sha256, self.scope.scope_sha256)
        materialization_tamper = self.materialization.model_dump(mode="json")
        materialization_tamper["agent_logical_table_scope_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValidationError, "hash does not bind"):
            type(self.materialization).model_validate(materialization_tamper)
        drifted = request.model_dump(mode="json")
        drifted["logical_table_scope"] = build_agent_logical_table_scope(
            self.source,
            d1_table_map_artifact=self.d1_identity,
            physical_table_ids=self.base_projection.table_fact.table_ids,
            reason_codes=("scope_drifted",),
        ).model_dump(mode="json")
        drifted["logical_table_scope"]["logical_object_id"] = (
            "agent-logical-table-" + "a" * 32
        )
        with self.assertRaises(ValidationError):
            AgentTableMaterializationCommitRequestV1.model_validate(drifted)

    def test_application_commit_and_read_use_formal_repository(self) -> None:
        result = {
            "table_record_projections": [
                self.base_projection.model_dump(mode="json")
            ],
            "table_evidence_bindings": [],
            "tables": list(self.tables),
        }
        document = {"source_sha256": self.source.source_sha256}
        agent = {
            "effective_structure_identity": {
                "snapshot_id": self.structure.effective_snapshot_id,
            }
        }
        patchers = (
            patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value=document,
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_structure_agent_job_payload",
                return_value=({}, result, agent),
            ),
            patch(
                "app.application.extraction.structure_workspace."
                "_rebuild_structure_revision_context",
                return_value=(self.source, self.base_tree, ()),
            ),
            patch(
                "app.application.extraction.document_jobs."
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
            patch(
                "app.application.runtime._agent_table_materializations",
                return_value=self.repository,
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        committed = commit_document_agent_table_materialization(
            "project-agent-table-api",
            "document-agent-table-api",
            self.base_projection.projection_id,
            self._request(command_id="agent-table:api"),
            SimpleNamespace(headers={}, status_code=200),
        )
        record_id = committed["record"]["record_id"]
        read = get_document_agent_table_materialization(
            "project-agent-table-api",
            "document-agent-table-api",
            record_id,
            SimpleNamespace(headers={}, status_code=200),
        )

        self.assertTrue(committed["created"])
        self.assertEqual(read["record"]["record_id"], record_id)
        self.assertEqual(
            read["record"]["logical_table_scope"]["scope_sha256"],
            self.scope.scope_sha256,
        )

    def _commit(self, *, command_id: str):
        return self.repository.commit(
            project_id="project-agent-table",
            document_id="document-agent-table",
            command_id=command_id,
            base_projection=self.base_projection,
            logical_table_scope=self.scope,
            materialization=self.materialization,
            evidence_bindings=self.evidence,
        )

    def _request(
        self,
        *,
        command_id: str = "agent-table:request",
    ) -> AgentTableMaterializationCommitRequestV1:
        return AgentTableMaterializationCommitRequestV1(
            command_id=command_id,
            expected_source_sha256=self.source.source_sha256,
            expected_effective_structure_snapshot_id=(
                self.structure.effective_snapshot_id
            ),
            expected_base_projection_id=self.base_projection.projection_id,
            expected_base_projection_revision=(
                self.base_projection.projection_revision
            ),
            expected_base_projection_sha256=(
                self.base_projection.projection_sha256
            ),
            logical_table_scope=self.scope,
            materialization=self.materialization,
        )

    def _boundaries(
        self,
        row_groups: tuple[tuple[int, ...], ...],
        *,
        artifact_sha256: str,
    ) -> tuple[AgentTableParentBoundaryV1, ...]:
        table_id = self.base_projection.table_fact.table_ids[0]
        table_node = next(
            node
            for node in self.source.nodes
            if node.table_payload is not None
            and node.table_payload.table_id == table_id
        )
        table = table_node.table_payload
        assert table is not None
        result = []
        for source_order, row_indices in enumerate(row_groups):
            row_bindings = tuple(
                AgentTableRowBindingV1(
                    table_id=table_id,
                    row_index=row_index,
                    cell_ids=tuple(
                        cell.cell_id
                        for cell in sorted(
                            table.cells,
                            key=lambda item: (
                                item.row,
                                item.column,
                                item.cell_id,
                            ),
                        )
                        if cell.row <= row_index < cell.row + cell.row_span
                    ),
                )
                for row_index in row_indices
            )
            source_refs = tuple(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell_id,
                )
                for binding in row_bindings
                for cell_id in binding.cell_ids
            )
            result.append(
                build_agent_table_parent_boundary(
                    source_sha256=self.source.source_sha256,
                    boundary_artifact_id="agent-parent-boundaries-fixture",
                    boundary_artifact_sha256=artifact_sha256,
                    logical_table_id=self.scope.logical_object_id,
                    parent_provision_id=f"parent-{source_order + 1}",
                    source_order=source_order,
                    row_bindings=row_bindings,
                    source_refs=source_refs,
                )
            )
        return tuple(result)

    def _materialization(
        self,
        *,
        base_projection,
        boundaries: tuple[AgentTableParentBoundaryV1, ...],
        artifact_sha256: str,
    ):
        return materialize_agent_table_parent_boundaries(
            self.source,
            self.structure,
            base_projection=base_projection,
            boundaries=boundaries,
            expected_boundary_artifact_id="agent-parent-boundaries-fixture",
            expected_boundary_artifact_sha256=artifact_sha256,
            expected_agent_logical_table_id=self.scope.logical_object_id,
            agent_logical_table_scope_sha256=self.scope.scope_sha256,
            d1_table_map_artifact_id=self.d1_identity.artifact_id,
            d1_table_map_artifact_sha256=self.d1_identity.artifact_sha256,
        )


if __name__ == "__main__":
    unittest.main()
