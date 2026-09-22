CREATE TABLE IF NOT EXISTS parsing_risk_acknowledgements (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL,
    parse_artifact_id TEXT NOT NULL,
    parse_identity_sha256 TEXT NOT NULL,
    severe_warning_count INTEGER NOT NULL CHECK (severe_warning_count >= 0),
    acknowledged_at TEXT NOT NULL,
    actor_type TEXT NOT NULL DEFAULT 'USER',
    UNIQUE (
        sor_document_id,
        source_sha256,
        parse_artifact_id,
        parse_identity_sha256
    )
);

CREATE INDEX IF NOT EXISTS idx_parsing_risk_acknowledgements_scope
    ON parsing_risk_acknowledgements (
        project_id,
        sor_document_id,
        source_sha256,
        parse_artifact_id
    );
