CREATE TABLE IF NOT EXISTS table_projection_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL,
    structure_snapshot_id TEXT NOT NULL,
    logical_table_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    action TEXT NOT NULL,
    projection_id TEXT NOT NULL DEFAULT '',
    projection_sha256 TEXT NOT NULL DEFAULT '',
    projection_json TEXT NOT NULL DEFAULT '',
    evidence_bindings_json TEXT NOT NULL DEFAULT '[]',
    supersedes_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (
        sor_document_id,
        source_sha256,
        structure_snapshot_id,
        logical_table_id,
        revision
    )
);

CREATE INDEX IF NOT EXISTS idx_table_projection_revisions_scope
    ON table_projection_revisions (
        sor_document_id,
        source_sha256,
        structure_snapshot_id,
        logical_table_id,
        revision
    );
