from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
import uuid

from app.adapters.persistence.postgres_agent_table_materializations import (
    PostgresAgentTableMaterializationRepository,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    build_agent_logical_table_scope,
)
from app.application.extraction.agent_table_boundaries import (
    AgentTableRowBindingV1,
    build_agent_table_parent_boundary,
    materialize_agent_table_parent_boundaries,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import get_base_effective_structure
from app.application.extraction.table_evidence_projection import (
    build_table_evidence_bindings,
)
from app.application.extraction.table_record_projection import (
    build_table_record_projections,
)
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.infrastructure.postgres import PostgresDatabase
from tests.test_table_record_projection_builder import (
    _source_with_tables,
    _table_spec,
)


_POSTGRES_E2E_ENABLED = os.environ.get("REQPILOT_RUN_POSTGRES_INTEGRATION") == "1"


@unittest.skipUnless(
    _POSTGRES_E2E_ENABLED,
    "set REQPILOT_RUN_POSTGRES_INTEGRATION=1 for the local PostgreSQL E2E",
)
class AgentTableMaterializationPostgresE2ETests(unittest.TestCase):
    def test_atomic_commit_replay_read_and_document_cascade(self) -> None:
        database = PostgresDatabase(
            os.environ.get(
                "REQPILOT_POSTGRES_INTEGRATION_URL",
                "postgresql://localhost/reqpilotx",
            )
        )
        database.apply_migrations(
            Path(__file__).resolve().parents[1] / "migrations" / "postgresql"
        )
        repository = PostgresAgentTableMaterializationRepository(database)
        suffix = uuid.uuid4().hex
        project_id = f"agent-table-e2e-project-{suffix}"
        document_id = f"agent-table-e2e-document-{suffix}"
        storage_key = f"synthetic/{document_id}.pdf"
        source = _source_with_tables(
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
        base_tree = build_structure_tree(source)
        structure = get_base_effective_structure(source, base_tree)
        base_projection = build_table_record_projections(source, structure)[0]
        d1_identity = AgentD1TableMapArtifactIdentityV1(
            source_sha256=source.source_sha256,
            artifact_id="agent-d1-table-map-postgres-e2e",
            artifact_sha256="d" * 64,
        )
        scope = build_agent_logical_table_scope(
            source,
            d1_table_map_artifact=d1_identity,
            physical_table_ids=base_projection.table_fact.table_ids,
            reason_codes=("same_logical_object",),
        )
        table_id = base_projection.table_fact.table_ids[0]
        table = next(
            node.table_payload
            for node in source.nodes
            if node.table_payload is not None
            and node.table_payload.table_id == table_id
        )
        boundaries = []
        for source_order, row_indices in enumerate(((1, 2), (3,))):
            row_bindings = tuple(
                AgentTableRowBindingV1(
                    table_id=table_id,
                    row_index=row_index,
                    cell_ids=tuple(
                        cell.cell_id
                        for cell in sorted(
                            table.cells,
                            key=lambda item: (item.row, item.column, item.cell_id),
                        )
                        if cell.row <= row_index < cell.row + cell.row_span
                    ),
                )
                for row_index in row_indices
            )
            boundaries.append(
                build_agent_table_parent_boundary(
                    source_sha256=source.source_sha256,
                    boundary_artifact_id="agent-parent-boundaries-postgres-e2e",
                    boundary_artifact_sha256="e" * 64,
                    logical_table_id=scope.logical_object_id,
                    parent_provision_id=f"parent-{source_order + 1}",
                    source_order=source_order,
                    row_bindings=row_bindings,
                    source_refs=tuple(
                        StructureSourceRef(
                            kind=StructureSourceRefKind.TABLE_CELL,
                            source_id=cell_id,
                        )
                        for binding in row_bindings
                        for cell_id in binding.cell_ids
                    ),
                )
            )
        materialization = materialize_agent_table_parent_boundaries(
            source,
            structure,
            base_projection=base_projection,
            boundaries=tuple(boundaries),
            expected_boundary_artifact_id="agent-parent-boundaries-postgres-e2e",
            expected_boundary_artifact_sha256="e" * 64,
            expected_agent_logical_table_id=scope.logical_object_id,
            agent_logical_table_scope_sha256=scope.scope_sha256,
            d1_table_map_artifact_id=d1_identity.artifact_id,
            d1_table_map_artifact_sha256=d1_identity.artifact_sha256,
        )
        evidence = build_table_evidence_bindings(
            table_record_projections=(materialization.projection,),
            tables=tuple(
                {"id": part.table_id, "page_number": 1}
                for part in base_projection.table_fact.table_parts
            ),
        )
        now = datetime.now(timezone.utc).isoformat()

        try:
            with database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO projects (id, name, customer_name, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (project_id, "Agent table E2E", "Synthetic", now),
                )
                connection.execute(
                    """
                    INSERT INTO stored_objects (
                        storage_key, sha256, size_bytes, media_type, object_kind
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        storage_key,
                        source.source_sha256,
                        1,
                        "application/pdf",
                        "sor-source",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sor_documents (
                        id, project_id, file_name, file_type, storage_key,
                        extracted_text, source_sha256, source_size, created_at,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        document_id,
                        project_id,
                        "synthetic.pdf",
                        "pdf",
                        storage_key,
                        "",
                        source.source_sha256,
                        1,
                        now,
                        now,
                    ),
                )

            committed, created = repository.commit(
                project_id=project_id,
                document_id=document_id,
                command_id="agent-table-postgres-e2e",
                base_projection=base_projection,
                logical_table_scope=scope,
                materialization=materialization,
                evidence_bindings=evidence,
            )
            replay, replay_created = repository.commit(
                project_id=project_id,
                document_id=document_id,
                command_id="agent-table-postgres-e2e",
                base_projection=base_projection,
                logical_table_scope=scope,
                materialization=materialization,
                evidence_bindings=evidence,
            )

            self.assertTrue(created)
            self.assertFalse(replay_created)
            self.assertEqual(replay, committed)
            self.assertEqual(
                repository.get(
                    project_id=project_id,
                    document_id=document_id,
                    record_id=committed.record_id,
                ),
                committed,
            )
            with database.connection() as connection:
                revision = connection.execute(
                    """
                    SELECT action, projection_id
                    FROM table_projection_revisions
                    WHERE id = %s
                    """,
                    (committed.table_projection_revision_id,),
                ).fetchone()
            self.assertEqual(revision["action"], "APPLY_AGENT_BOUNDARIES")
            self.assertEqual(
                revision["projection_id"],
                materialization.projection.projection_id,
            )

            with database.connection() as connection:
                connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))
                remaining = connection.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM agent_table_materializations
                       WHERE sor_document_id = %s) AS materializations,
                      (SELECT COUNT(*) FROM agent_table_materialization_commands
                       WHERE sor_document_id = %s) AS commands,
                      (SELECT COUNT(*) FROM table_projection_revisions
                       WHERE sor_document_id = %s) AS revisions
                    """,
                    (document_id, document_id, document_id),
                ).fetchone()
            self.assertEqual(dict(remaining), {
                "materializations": 0,
                "commands": 0,
                "revisions": 0,
            })
        finally:
            with database.connection() as connection:
                connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))
                connection.execute(
                    "DELETE FROM stored_objects WHERE storage_key = %s",
                    (storage_key,),
                )
            database.close()


if __name__ == "__main__":
    unittest.main()
