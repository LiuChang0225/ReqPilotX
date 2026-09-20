-- User-entered working opinions, separate from AI/classification and source heads.
CREATE TABLE IF NOT EXISTS source_item_manual_decision_versions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision > 0),
    artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256) = 64),
    artifact_json TEXT NOT NULL,
    command_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    operation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, sor_document_id, revision),
    UNIQUE(project_id, sor_document_id, command_id)
);
CREATE INDEX IF NOT EXISTS idx_source_item_manual_decisions_current
    ON source_item_manual_decision_versions(project_id, sor_document_id, revision DESC);
