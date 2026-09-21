CREATE TABLE IF NOT EXISTS source_capture_asset_publications (
    publication_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    status TEXT NOT NULL CHECK(status IN ('STAGING', 'ABORTED')),
    created_at_utc TIMESTAMPTZ NOT NULL,
    updated_at_utc TIMESTAMPTZ NOT NULL,
    UNIQUE(publication_id, project_id, sor_document_id, source_sha256)
);

CREATE TABLE IF NOT EXISTS source_capture_asset_staging_references (
    publication_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    storage_key TEXT NOT NULL
        REFERENCES stored_objects(storage_key) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(publication_id, storage_key),
    FOREIGN KEY(publication_id, project_id, sor_document_id, source_sha256)
        REFERENCES source_capture_asset_publications(
            publication_id, project_id, sor_document_id, source_sha256
        ) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id, project_id, source_sha256)
        REFERENCES sor_documents(id, project_id, source_sha256)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_source_capture_asset_staging_storage_key
    ON source_capture_asset_staging_references(storage_key);

CREATE TABLE IF NOT EXISTS source_capture_asset_references (
    capture_id TEXT NOT NULL
        REFERENCES source_capture_snapshots(capture_id) ON DELETE CASCADE,
    storage_key TEXT NOT NULL
        REFERENCES stored_objects(storage_key) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(capture_id, storage_key)
);

CREATE INDEX IF NOT EXISTS idx_source_capture_asset_refs_storage_key
    ON source_capture_asset_references(storage_key);

CREATE OR REPLACE VIEW source_asset_reference_counts AS
SELECT
    managed.storage_key,
    managed.legacy_protected,
    (
        SELECT count(*) FROM source_asset_references AS committed
        WHERE committed.storage_key = managed.storage_key
    ) + (
        SELECT count(*) FROM source_capture_asset_references AS committed
        WHERE committed.storage_key = managed.storage_key
    ) AS committed_refcount,
    (
        SELECT count(*) FROM source_asset_staging_references AS staging
        WHERE staging.storage_key = managed.storage_key
    ) + (
        SELECT count(*) FROM source_capture_asset_staging_references AS staging
        WHERE staging.storage_key = managed.storage_key
    ) AS staging_refcount
FROM source_asset_managed_objects AS managed;
