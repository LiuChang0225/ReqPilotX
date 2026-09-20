ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS publication_owner_token TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS publication_lease_expires_at TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS publication_heartbeat_at TEXT NOT NULL DEFAULT '';

-- Existing PUBLISHING rows intentionally keep empty lease fields. Their age
-- cannot be proven, so the application treats them as non-takeoverable rather
-- than assigning a synthetic expiry during migration.
ALTER TABLE requirement_sets
    DROP CONSTRAINT IF EXISTS requirement_sets_publication_lease_pair;

ALTER TABLE requirement_sets
    ADD CONSTRAINT requirement_sets_publication_lease_pair
    CHECK (
        (
            publication_owner_token = ''
            AND publication_lease_expires_at = ''
            AND publication_heartbeat_at = ''
        )
        OR
        (
            publication_owner_token <> ''
            AND publication_lease_expires_at <> ''
            AND publication_heartbeat_at <> ''
        )
    );
