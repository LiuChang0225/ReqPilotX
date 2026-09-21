-- Immutable native delivery snapshots. Never use native baseline IDs as legacy set IDs.
CREATE TABLE IF NOT EXISTS source_item_delivery_versions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision > 0),
    command_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    artifact_json TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, sor_document_id, revision),
    UNIQUE(project_id, sor_document_id, command_id)
);
