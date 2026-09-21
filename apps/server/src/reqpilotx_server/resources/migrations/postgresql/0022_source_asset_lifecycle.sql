ALTER TABLE sor_documents
    DROP CONSTRAINT IF EXISTS sor_documents_asset_ownership_identity;

ALTER TABLE sor_documents
    ADD CONSTRAINT sor_documents_asset_ownership_identity
    UNIQUE (id, project_id, source_sha256);

CREATE TABLE IF NOT EXISTS source_asset_managed_objects (
    storage_key TEXT PRIMARY KEY
        REFERENCES stored_objects(storage_key) ON DELETE CASCADE,
    legacy_protected BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Assets published before this lifecycle existed have no trustworthy owner
-- inventory.  Keep them readable, but never infer that zero new references
-- makes them safe to delete.
INSERT INTO source_asset_managed_objects (storage_key, legacy_protected)
SELECT storage_key, TRUE
FROM stored_objects
WHERE object_kind = 'source-ir-asset'
ON CONFLICT (storage_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS source_asset_references (
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    storage_key TEXT NOT NULL
        REFERENCES stored_objects(storage_key) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, source_sha256, storage_key),
    FOREIGN KEY (document_id, project_id, source_sha256)
        REFERENCES sor_documents(id, project_id, source_sha256)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_source_asset_references_storage_key
    ON source_asset_references(storage_key);

CREATE TABLE IF NOT EXISTS source_asset_staging_references (
    job_id TEXT NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    attempt_generation BIGINT NOT NULL CHECK(attempt_generation > 0),
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    storage_key TEXT NOT NULL
        REFERENCES stored_objects(storage_key) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, attempt_generation, storage_key),
    FOREIGN KEY (document_id, project_id, source_sha256)
        REFERENCES sor_documents(id, project_id, source_sha256)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_source_asset_staging_storage_key
    ON source_asset_staging_references(storage_key);

CREATE INDEX IF NOT EXISTS idx_source_asset_staging_document
    ON source_asset_staging_references(document_id, source_sha256);

-- The count is derived from durable reference rows instead of a mutable
-- counter column, so retries and cascades cannot make it drift.
CREATE OR REPLACE VIEW source_asset_reference_counts AS
SELECT
    managed.storage_key,
    managed.legacy_protected,
    (
        SELECT count(*)
        FROM source_asset_references AS committed
        WHERE committed.storage_key = managed.storage_key
    ) AS committed_refcount,
    (
        SELECT count(*)
        FROM source_asset_staging_references AS staging
        WHERE staging.storage_key = managed.storage_key
    ) AS staging_refcount
FROM source_asset_managed_objects AS managed;
