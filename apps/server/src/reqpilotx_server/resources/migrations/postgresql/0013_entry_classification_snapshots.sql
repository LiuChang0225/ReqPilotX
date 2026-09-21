CREATE TABLE IF NOT EXISTS entry_classification_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    working_tree_id TEXT NOT NULL,
    working_tree_sha256 TEXT NOT NULL CHECK(length(working_tree_sha256) = 64),
    effective_structure_id TEXT NOT NULL,
    semantic_snapshot_id TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    status TEXT NOT NULL CHECK(status IN ('WORKING', 'ACCEPTED')),
    supersedes_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    command_sha256 TEXT NOT NULL CHECK(length(command_sha256) = 64),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256) = 64),
    created_at TEXT NOT NULL,
    accepted_at TEXT NOT NULL DEFAULT '',
    accepted_by TEXT NOT NULL DEFAULT '',
    UNIQUE (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, revision
    ),
    UNIQUE (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, command_id
    )
);

CREATE INDEX IF NOT EXISTS idx_entry_classification_snapshots_head
    ON entry_classification_snapshots (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, revision DESC
    );

CREATE TABLE IF NOT EXISTS entry_classification_command_receipts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    working_tree_id TEXT NOT NULL,
    working_tree_sha256 TEXT NOT NULL CHECK(length(working_tree_sha256) = 64),
    effective_structure_id TEXT NOT NULL,
    semantic_snapshot_id TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL,
    command_id TEXT NOT NULL,
    command_sha256 TEXT NOT NULL CHECK(length(command_sha256) = 64),
    result_snapshot_id TEXT NOT NULL,
    result_snapshot_sha256 TEXT NOT NULL
        CHECK(length(result_snapshot_sha256) = 64),
    created_at TEXT NOT NULL,
    envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256) = 64),
    UNIQUE (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, command_id
    )
);

CREATE INDEX IF NOT EXISTS idx_entry_classification_command_receipts_scope
    ON entry_classification_command_receipts (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, command_id
    );

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS entry_classification_snapshot_id TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS entry_classification_snapshot_sha256 TEXT NOT NULL DEFAULT '';
