ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS requirement_tree_object_key TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS requirement_tree_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS requirement_tree_schema_version TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS structure_snapshot_id TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS structure_tree_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS structure_revision INTEGER NOT NULL DEFAULT 0;

ALTER TABLE requirement_sets
    DROP CONSTRAINT IF EXISTS requirement_sets_tree_sha256_valid;

ALTER TABLE requirement_sets
    ADD CONSTRAINT requirement_sets_tree_sha256_valid
    CHECK (
        requirement_tree_sha256 = ''
        OR length(requirement_tree_sha256) = 64
    );

ALTER TABLE requirement_sets
    DROP CONSTRAINT IF EXISTS requirement_sets_structure_tree_sha256_valid;

ALTER TABLE requirement_sets
    ADD CONSTRAINT requirement_sets_structure_tree_sha256_valid
    CHECK (
        structure_tree_sha256 = ''
        OR length(structure_tree_sha256) = 64
    );

ALTER TABLE requirement_sets
    DROP CONSTRAINT IF EXISTS requirement_sets_structure_revision_valid;

ALTER TABLE requirement_sets
    ADD CONSTRAINT requirement_sets_structure_revision_valid
    CHECK (structure_revision >= 0);
