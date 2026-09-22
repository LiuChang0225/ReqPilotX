-- Job-generation scoped, read-only completed-page artifacts.
--
-- These rows are intentionally staging references, not formal SourceIR or
-- Structure publication. Readers must join the exact live runtime_jobs lease;
-- terminal, cancelled, expired, and retried generations are therefore hidden.
CREATE TABLE IF NOT EXISTS document_extraction_page_staging_references (
    job_id TEXT NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    attempt_generation BIGINT NOT NULL CHECK(attempt_generation > 0),
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    processing_plan_sha256 TEXT NOT NULL
        CHECK(length(processing_plan_sha256) = 64),
    result_runtime_sha256 TEXT NOT NULL
        CHECK(length(result_runtime_sha256) = 64),
    adapter_id TEXT NOT NULL CHECK(length(adapter_id) > 0),
    adapter_version TEXT NOT NULL CHECK(length(adapter_version) > 0),
    page_number INTEGER NOT NULL CHECK(page_number > 0),
    total_pages INTEGER NOT NULL CHECK(total_pages >= page_number),
    completed_at_utc TIMESTAMPTZ NOT NULL,
    storage_key TEXT NOT NULL
        REFERENCES source_asset_managed_objects(storage_key) ON DELETE RESTRICT,
    artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256) = 64),
    size_bytes BIGINT NOT NULL CHECK(size_bytes > 0),
    media_type TEXT NOT NULL DEFAULT 'application/json',
    confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, attempt_generation, page_number),
    FOREIGN KEY (document_id, project_id, source_sha256)
        REFERENCES sor_documents(id, project_id, source_sha256)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_extraction_page_staging_document
    ON document_extraction_page_staging_references(
        project_id, document_id, source_sha256, job_id,
        attempt_generation, page_number
    );

CREATE INDEX IF NOT EXISTS idx_document_extraction_page_staging_storage_key
    ON document_extraction_page_staging_references(storage_key);
