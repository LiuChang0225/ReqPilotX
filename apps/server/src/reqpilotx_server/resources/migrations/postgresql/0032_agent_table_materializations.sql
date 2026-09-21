-- Immutable Agent-owned table parent/materialization lineage.  The output
-- projection revision and this artifact are committed in one transaction.
CREATE TABLE IF NOT EXISTS agent_table_materializations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    effective_structure_snapshot_id TEXT NOT NULL,
    materializer_logical_table_id TEXT NOT NULL,
    boundary_artifact_id TEXT NOT NULL,
    boundary_artifact_sha256 TEXT NOT NULL
        CHECK(length(boundary_artifact_sha256) = 64),
    agent_logical_table_id TEXT NOT NULL,
    logical_table_scope_sha256 TEXT NOT NULL
        CHECK(length(logical_table_scope_sha256) = 64),
    d1_table_map_artifact_id TEXT NOT NULL,
    d1_table_map_artifact_sha256 TEXT NOT NULL
        CHECK(length(d1_table_map_artifact_sha256) = 64),
    logical_table_scope_payload JSONB NOT NULL,
    materialization_sha256 TEXT NOT NULL
        CHECK(length(materialization_sha256) = 64),
    base_projection_id TEXT NOT NULL,
    base_projection_revision INTEGER NOT NULL
        CHECK(base_projection_revision >= 0),
    base_projection_sha256 TEXT NOT NULL
        CHECK(length(base_projection_sha256) = 64),
    output_projection_id TEXT NOT NULL,
    output_projection_revision INTEGER NOT NULL
        CHECK(output_projection_revision > base_projection_revision),
    output_projection_sha256 TEXT NOT NULL
        CHECK(length(output_projection_sha256) = 64),
    table_projection_revision_id TEXT NOT NULL
        REFERENCES table_projection_revisions(id) ON DELETE CASCADE,
    table_projection_head_revision INTEGER NOT NULL
        CHECK(table_projection_head_revision > 0),
    materialization_payload JSONB NOT NULL,
    evidence_bindings JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (
        sor_document_id,
        source_sha256,
        effective_structure_snapshot_id,
        materializer_logical_table_id,
        materialization_sha256
    ),
    UNIQUE (table_projection_revision_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_table_materializations_scope
    ON agent_table_materializations (
        sor_document_id,
        source_sha256,
        effective_structure_snapshot_id,
        materializer_logical_table_id,
        table_projection_head_revision DESC
    );

CREATE TABLE IF NOT EXISTS agent_table_materialization_commands (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    effective_structure_snapshot_id TEXT NOT NULL,
    materializer_logical_table_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    materialization_id TEXT NOT NULL
        REFERENCES agent_table_materializations(id) ON DELETE CASCADE,
    command_payload_sha256 TEXT NOT NULL
        CHECK(length(command_payload_sha256) = 64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (
        project_id,
        sor_document_id,
        source_sha256,
        effective_structure_snapshot_id,
        materializer_logical_table_id,
        command_id
    )
);
