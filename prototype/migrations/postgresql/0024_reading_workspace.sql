CREATE TABLE IF NOT EXISTS source_capture_snapshots (
    capture_id TEXT PRIMARY KEY,
    source_ir_id TEXT NOT NULL UNIQUE,
    source_ir_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    schema_version TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('COMPLETE', 'PARTIAL')),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    facts_sha256 TEXT NOT NULL CHECK(length(facts_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_capture_snapshots_source
    ON source_capture_snapshots(source_sha256, source_ir_id);

CREATE TABLE IF NOT EXISTS source_capture_bindings (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    capture_id TEXT NOT NULL REFERENCES source_capture_snapshots(capture_id),
    created_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(project_id, sor_document_id, source_sha256)
);

CREATE INDEX IF NOT EXISTS idx_source_capture_bindings_capture
    ON source_capture_bindings(capture_id);

CREATE TABLE IF NOT EXISTS reading_sessions (
    reading_session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    capture_id TEXT NOT NULL REFERENCES source_capture_snapshots(capture_id),
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'READY', 'PARTIAL', 'STALE', 'FAILED')),
    overview_json TEXT NOT NULL,
    overview_sha256 TEXT NOT NULL CHECK(length(overview_sha256) = 64),
    structure_map_json TEXT NOT NULL,
    structure_map_sha256 TEXT NOT NULL CHECK(length(structure_map_sha256) = 64),
    selectable_nodes_json TEXT NOT NULL,
    selectable_nodes_sha256 TEXT NOT NULL CHECK(length(selectable_nodes_sha256) = 64),
    created_at_utc TIMESTAMPTZ NOT NULL,
    updated_at_utc TIMESTAMPTZ NOT NULL,
    UNIQUE(project_id, sor_document_id, source_sha256),
    UNIQUE(reading_session_id, project_id, sor_document_id, source_sha256)
);

CREATE INDEX IF NOT EXISTS idx_reading_sessions_document
    ON reading_sessions(project_id, sor_document_id, updated_at_utc DESC);

CREATE TABLE IF NOT EXISTS reading_artifacts (
    artifact_id TEXT PRIMARY KEY,
    reading_session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    action TEXT NOT NULL CHECK(action IN (
        'EXPLANATION',
        'TRANSLATION',
        'ENGINEERING_IMPACT',
        'CLARIFICATION_QUESTION'
    )),
    source_ref_json TEXT NOT NULL,
    source_ref_sha256 TEXT NOT NULL CHECK(length(source_ref_sha256) = 64),
    output_text TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STALE', 'FAILED')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TIMESTAMPTZ NOT NULL,
    FOREIGN KEY(reading_session_id, project_id, sor_document_id, source_sha256)
        REFERENCES reading_sessions(
            reading_session_id, project_id, sor_document_id, source_sha256
        )
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reading_artifacts_session
    ON reading_artifacts(reading_session_id, created_at_utc DESC);

CREATE TABLE IF NOT EXISTS reading_notes (
    note_id TEXT PRIMARY KEY,
    reading_session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    source_ref_json TEXT NOT NULL,
    source_ref_sha256 TEXT NOT NULL CHECK(length(source_ref_sha256) = 64),
    content TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STALE', 'DELETED')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TIMESTAMPTZ NOT NULL,
    updated_at_utc TIMESTAMPTZ NOT NULL,
    FOREIGN KEY(reading_session_id, project_id, sor_document_id, source_sha256)
        REFERENCES reading_sessions(
            reading_session_id, project_id, sor_document_id, source_sha256
        )
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reading_notes_session
    ON reading_notes(reading_session_id, updated_at_utc DESC);
