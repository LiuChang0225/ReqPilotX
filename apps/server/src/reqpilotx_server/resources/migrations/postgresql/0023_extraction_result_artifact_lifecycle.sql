-- Durable ownership for document-extraction result artifacts.
--
-- Managed objects remain in this inventory after their last reference is
-- released.  Object-store deletion and removal of this row are one fenced
-- application operation, so a transient object-store failure remains
-- discoverable and retryable.
CREATE TABLE IF NOT EXISTS extraction_result_artifact_managed_objects (
    storage_key TEXT PRIMARY KEY,
    artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256) = 64),
    size_bytes BIGINT NOT NULL CHECK(size_bytes > 0),
    media_type TEXT NOT NULL DEFAULT 'application/json',
    legacy_protected BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Successful receipts written before this migration have no complete owner
-- inventory.  Record them as readable legacy objects, but never guess that it
-- is safe to delete them.
INSERT INTO extraction_result_artifact_managed_objects (
    storage_key, artifact_sha256, size_bytes, media_type, legacy_protected
)
SELECT DISTINCT ON (result->>'storage_key')
       result->>'storage_key',
       lower(result->>'sha256'),
       (result->>'size')::bigint,
       COALESCE(NULLIF(result->>'media_type', ''), 'application/json'),
       TRUE
FROM runtime_jobs
WHERE kind = 'document.extract'
  AND status = 'SUCCEEDED'
  AND jsonb_typeof(result) = 'object'
  AND COALESCE(result->>'schema_version', '') =
        'document-extraction-result-artifact/v1'
  AND COALESCE(result->>'storage_key', '') <> ''
  AND COALESCE(result->>'sha256', '') ~ '^[0-9A-Fa-f]{64}$'
  AND COALESCE(result->>'size', '') ~ '^[1-9][0-9]*$'
ORDER BY result->>'storage_key', updated_at DESC
ON CONFLICT (storage_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS extraction_result_artifact_references (
    job_id TEXT PRIMARY KEY REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    storage_key TEXT NOT NULL REFERENCES
        extraction_result_artifact_managed_objects(storage_key) ON DELETE RESTRICT,
    artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256) = 64),
    size_bytes BIGINT NOT NULL CHECK(size_bytes > 0),
    media_type TEXT NOT NULL DEFAULT 'application/json',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (document_id, project_id, source_sha256)
        REFERENCES sor_documents(id, project_id, source_sha256)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_extraction_result_artifact_storage_key
    ON extraction_result_artifact_references(storage_key);

CREATE TABLE IF NOT EXISTS extraction_result_artifact_staging_references (
    job_id TEXT NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    attempt_generation BIGINT NOT NULL CHECK(attempt_generation > 0),
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    storage_key TEXT NOT NULL REFERENCES
        extraction_result_artifact_managed_objects(storage_key) ON DELETE RESTRICT,
    artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256) = 64),
    size_bytes BIGINT NOT NULL CHECK(size_bytes > 0),
    media_type TEXT NOT NULL DEFAULT 'application/json',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, attempt_generation),
    FOREIGN KEY (document_id, project_id, source_sha256)
        REFERENCES sor_documents(id, project_id, source_sha256)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_extraction_result_artifact_staging_key
    ON extraction_result_artifact_staging_references(storage_key);

-- A Structure-workspace CAS writes bytes before it can atomically replace the
-- job result.  This operation row is the durable owner for that interval.  A
-- recovery scan can determine success from runtime_jobs.result and either
-- finish the reference swap or release the new object.
CREATE TABLE IF NOT EXISTS extraction_result_artifact_cas_operations (
    operation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    expected_storage_key TEXT NOT NULL,
    new_storage_key TEXT NOT NULL REFERENCES
        extraction_result_artifact_managed_objects(storage_key) ON DELETE RESTRICT,
    new_artifact_sha256 TEXT NOT NULL CHECK(length(new_artifact_sha256) = 64),
    new_size_bytes BIGINT NOT NULL CHECK(new_size_bytes > 0),
    media_type TEXT NOT NULL DEFAULT 'application/json',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '15 minutes'),
    FOREIGN KEY (document_id, project_id, source_sha256)
        REFERENCES sor_documents(id, project_id, source_sha256)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_extraction_result_artifact_cas_new_key
    ON extraction_result_artifact_cas_operations(new_storage_key);

CREATE INDEX IF NOT EXISTS idx_extraction_result_artifact_cas_expiry
    ON extraction_result_artifact_cas_operations(expires_at);
