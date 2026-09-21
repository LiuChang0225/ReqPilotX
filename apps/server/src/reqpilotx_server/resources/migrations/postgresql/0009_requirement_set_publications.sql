ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS publication_key TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS publication_state TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    DROP CONSTRAINT IF EXISTS requirement_sets_publication_key_valid;

ALTER TABLE requirement_sets
    ADD CONSTRAINT requirement_sets_publication_key_valid
    CHECK (
        publication_key = ''
        OR length(publication_key) = 64
    );

ALTER TABLE requirement_sets
    DROP CONSTRAINT IF EXISTS requirement_sets_publication_state_valid;

ALTER TABLE requirement_sets
    ADD CONSTRAINT requirement_sets_publication_state_valid
    CHECK (
        publication_state IN ('', 'PUBLISHING', 'READY', 'FAILED')
    );

CREATE UNIQUE INDEX IF NOT EXISTS
    idx_requirement_sets_document_publication_key
ON requirement_sets(sor_document_id, publication_key)
WHERE publication_key <> '';
