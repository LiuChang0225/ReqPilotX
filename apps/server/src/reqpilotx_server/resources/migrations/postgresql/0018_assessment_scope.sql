ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS requirement_set_version INTEGER NOT NULL DEFAULT 0;

ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS requirement_tree_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS command_id TEXT NOT NULL DEFAULT '';

ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS command_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS scope_schema_version TEXT NOT NULL DEFAULT '';

ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS scope_kind TEXT NOT NULL DEFAULT '';

ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS scope_payload_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS scope_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS publication_mode TEXT NOT NULL DEFAULT 'ATOMIC_BATCH';

ALTER TABLE ai_analysis_jobs
    ADD COLUMN IF NOT EXISTS published_requirements INTEGER NOT NULL DEFAULT 0;

-- Requirement Sets are immutable, so their version/tree identity is safe to
-- recover for legacy jobs.  The exact ordered target list is not recoverable;
-- legacy command/scope identity therefore remains explicitly blank.
UPDATE ai_analysis_jobs AS jobs
SET requirement_set_version = sets.version,
    requirement_tree_sha256 = sets.requirement_tree_sha256
FROM requirement_sets AS sets
WHERE sets.id = jobs.requirement_set_id;

-- Before W6-A, successful serving publication was one all-or-none transaction.
-- Only completed legacy jobs can therefore be backfilled as fully published.
UPDATE ai_analysis_jobs
SET published_requirements = GREATEST(total_requirements, 0)
WHERE status = 'COMPLETED';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_ai_analysis_jobs_set_version_nonnegative'
    ) THEN
        ALTER TABLE ai_analysis_jobs
            ADD CONSTRAINT ck_ai_analysis_jobs_set_version_nonnegative
            CHECK (requirement_set_version >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_ai_analysis_jobs_tree_sha256'
    ) THEN
        ALTER TABLE ai_analysis_jobs
            ADD CONSTRAINT ck_ai_analysis_jobs_tree_sha256
            CHECK (
                requirement_tree_sha256 = ''
                OR requirement_tree_sha256 ~ '^[0-9a-f]{64}$'
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_ai_analysis_jobs_scope_kind'
    ) THEN
        ALTER TABLE ai_analysis_jobs
            ADD CONSTRAINT ck_ai_analysis_jobs_scope_kind
            CHECK (scope_kind IN ('', 'CURRENT', 'SELECTED', 'ALL'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_ai_analysis_jobs_scope_payload_object'
    ) THEN
        ALTER TABLE ai_analysis_jobs
            ADD CONSTRAINT ck_ai_analysis_jobs_scope_payload_object
            CHECK (jsonb_typeof(scope_payload_json::jsonb) = 'object');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_ai_analysis_jobs_scope_identity'
    ) THEN
        ALTER TABLE ai_analysis_jobs
            ADD CONSTRAINT ck_ai_analysis_jobs_scope_identity
            CHECK (
                (
                    scope_sha256 = ''
                    AND scope_schema_version = ''
                    AND scope_kind = ''
                    AND command_id = ''
                    AND command_sha256 = ''
                )
                OR (
                    scope_sha256 ~ '^[0-9a-f]{64}$'
                    AND scope_schema_version = 'requirement-analysis-scope/v1'
                    AND scope_kind IN ('CURRENT', 'SELECTED', 'ALL')
                    AND command_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
                    AND command_sha256 ~ '^[0-9a-f]{64}$'
                )
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_ai_analysis_jobs_publication_mode'
    ) THEN
        ALTER TABLE ai_analysis_jobs
            ADD CONSTRAINT ck_ai_analysis_jobs_publication_mode
            CHECK (publication_mode = 'ATOMIC_BATCH');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_ai_analysis_jobs_published_nonnegative'
    ) THEN
        ALTER TABLE ai_analysis_jobs
            ADD CONSTRAINT ck_ai_analysis_jobs_published_nonnegative
            CHECK (published_requirements >= 0);
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_analysis_jobs_document_command
    ON ai_analysis_jobs(sor_document_id, command_id)
    WHERE command_id <> '';
