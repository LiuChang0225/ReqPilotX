CREATE TABLE IF NOT EXISTS stored_objects (
    storage_key TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    size_bytes BIGINT NOT NULL CHECK(size_bytes >= 0),
    media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    object_kind TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_sor_documents_storage_key'
    ) THEN
        ALTER TABLE sor_documents
            ADD CONSTRAINT fk_sor_documents_storage_key
            FOREIGN KEY (storage_key)
            REFERENCES stored_objects(storage_key)
            ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_knowledge_documents_storage_key'
    ) THEN
        ALTER TABLE knowledge_documents
            ADD CONSTRAINT fk_knowledge_documents_storage_key
            FOREIGN KEY (storage_key)
            REFERENCES stored_objects(storage_key)
            ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_export_jobs_storage_key'
    ) THEN
        ALTER TABLE export_jobs
            ADD CONSTRAINT fk_export_jobs_storage_key
            FOREIGN KEY (storage_key)
            REFERENCES stored_objects(storage_key)
            ON DELETE RESTRICT;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS cutover_migration_runs (
    id TEXT PRIMARY KEY,
    source_database_sha256 TEXT NOT NULL CHECK(length(source_database_sha256) = 64),
    source_inventory_sha256 TEXT NOT NULL CHECK(length(source_inventory_sha256) = 64),
    target_environment TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('RUNNING', 'VERIFIED', 'FAILED')),
    table_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    object_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    verification_sha256 TEXT,
    error_code TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE(source_database_sha256, target_environment)
);
