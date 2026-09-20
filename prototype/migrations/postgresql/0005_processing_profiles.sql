ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS processing_profile_json TEXT NOT NULL DEFAULT '';

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS processing_profile_revision INTEGER NOT NULL DEFAULT 0;

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS processing_profile_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE projects
    DROP CONSTRAINT IF EXISTS projects_processing_profile_revision_nonnegative;

ALTER TABLE projects
    ADD CONSTRAINT projects_processing_profile_revision_nonnegative
    CHECK (processing_profile_revision >= 0);

ALTER TABLE projects
    DROP CONSTRAINT IF EXISTS projects_processing_profile_sha256_shape;

ALTER TABLE projects
    ADD CONSTRAINT projects_processing_profile_sha256_shape
    CHECK (
        processing_profile_sha256 = ''
        OR length(processing_profile_sha256) = 64
    );
