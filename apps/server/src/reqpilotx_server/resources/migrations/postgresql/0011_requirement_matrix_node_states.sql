CREATE TABLE IF NOT EXISTS requirement_matrix_node_states (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    requirement_set_id TEXT NOT NULL REFERENCES requirement_sets(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    disposition TEXT NOT NULL
        CHECK (disposition IN ('ANALYZE', 'RETAIN', 'EXCLUDE')),
    reason TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (requirement_set_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_requirement_matrix_node_states_scope
    ON requirement_matrix_node_states (
        project_id,
        sor_document_id,
        requirement_set_id,
        node_id
    );
