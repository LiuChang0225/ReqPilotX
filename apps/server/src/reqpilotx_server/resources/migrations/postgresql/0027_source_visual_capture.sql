CREATE TABLE IF NOT EXISTS source_visual_capture_generations (
    generation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    base_capture_id TEXT NOT NULL
        REFERENCES source_capture_snapshots(capture_id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TIMESTAMPTZ NOT NULL,
    UNIQUE(generation_id, project_id, sor_document_id, source_sha256)
);

CREATE INDEX IF NOT EXISTS idx_source_visual_generations_document
    ON source_visual_capture_generations(
        project_id,
        sor_document_id,
        source_sha256,
        created_at_utc DESC
    );

CREATE TABLE IF NOT EXISTS source_visual_capture_bindings (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    generation_id TEXT NOT NULL,
    updated_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(project_id, sor_document_id, source_sha256),
    FOREIGN KEY(generation_id, project_id, sor_document_id, source_sha256)
        REFERENCES source_visual_capture_generations(
            generation_id,
            project_id,
            sor_document_id,
            source_sha256
        ) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_source_visual_bindings_generation
    ON source_visual_capture_bindings(generation_id);

CREATE TABLE IF NOT EXISTS source_visual_page_snapshots (
    generation_id TEXT NOT NULL
        REFERENCES source_visual_capture_generations(generation_id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK(page_number >= 1),
    visual_snapshot_id TEXT NOT NULL,
    page_image_sha256 TEXT NOT NULL CHECK(length(page_image_sha256) = 64),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(generation_id, page_number)
);

CREATE INDEX IF NOT EXISTS idx_source_visual_pages_snapshot
    ON source_visual_page_snapshots(visual_snapshot_id);

CREATE TABLE IF NOT EXISTS source_visual_page_attempts (
    attempt_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    page_number INTEGER NOT NULL CHECK(page_number >= 1),
    status TEXT NOT NULL CHECK(status IN ('FAILED')),
    failure_code TEXT NOT NULL,
    created_at_utc TIMESTAMPTZ NOT NULL,
    FOREIGN KEY(generation_id, project_id, sor_document_id, source_sha256)
        REFERENCES source_visual_capture_generations(
            generation_id,
            project_id,
            sor_document_id,
            source_sha256
        ) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_source_visual_attempts_page
    ON source_visual_page_attempts(
        generation_id,
        page_number,
        created_at_utc DESC
    );

ALTER TABLE reading_sessions
    ADD COLUMN IF NOT EXISTS effective_bundle_id TEXT NOT NULL DEFAULT '';

ALTER TABLE reading_sessions
    ADD COLUMN IF NOT EXISTS visual_generation_id TEXT NOT NULL DEFAULT '';
