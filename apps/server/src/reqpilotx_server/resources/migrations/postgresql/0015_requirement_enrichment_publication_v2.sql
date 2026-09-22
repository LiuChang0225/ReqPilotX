ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS enrichment_publication_binding_id TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    ADD COLUMN IF NOT EXISTS enrichment_publication_binding_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE requirement_sets
    DROP CONSTRAINT IF EXISTS requirement_sets_enrichment_publication_binding_pair;

ALTER TABLE requirement_sets
    ADD CONSTRAINT requirement_sets_enrichment_publication_binding_pair
    CHECK (
        (
            enrichment_publication_binding_id = ''
            AND enrichment_publication_binding_sha256 = ''
        )
        OR
        (
            enrichment_publication_binding_id <> ''
            AND length(enrichment_publication_binding_sha256) = 64
        )
    );

CREATE UNIQUE INDEX IF NOT EXISTS
    idx_requirement_sets_enrichment_publication_binding
ON requirement_sets(enrichment_publication_binding_id)
WHERE enrichment_publication_binding_id <> '';

CREATE TABLE IF NOT EXISTS requirement_enrichment_publication_binding_previews (
    binding_preview_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    base_active_requirement_set_id TEXT NOT NULL DEFAULT '',
    accepted_classification_snapshot_id TEXT NOT NULL
        REFERENCES entry_classification_snapshots(id),
    accepted_classification_sha256 TEXT NOT NULL
        CHECK(length(accepted_classification_sha256) = 64),
    atomization_state TEXT NOT NULL
        CHECK(atomization_state IN ('NOT_RUN', 'APPLIED')),
    projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requirement_enrichment_binding_preview_document
ON requirement_enrichment_publication_binding_previews(
    project_id, document_id, session_id, review_revision,
    projection_sha256, created_at_utc DESC
);

CREATE TABLE IF NOT EXISTS requirement_enrichment_publication_bindings (
    binding_id TEXT PRIMARY KEY,
    binding_preview_id TEXT NOT NULL UNIQUE
        REFERENCES requirement_enrichment_publication_binding_previews(binding_preview_id)
        ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    requirement_set_id TEXT NOT NULL UNIQUE
        REFERENCES requirement_sets(id) ON DELETE CASCADE,
    projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256) = 64),
    publication_state TEXT NOT NULL
        CHECK(publication_state IN ('PUBLISHING', 'READY')),
    payload_sha256 TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK (
        (
            publication_state = 'PUBLISHING'
            AND payload_sha256 = ''
            AND payload_json = ''
        )
        OR
        (
            publication_state = 'READY'
            AND length(payload_sha256) = 64
            AND payload_json <> ''
        )
    )
);

CREATE TABLE IF NOT EXISTS requirement_enrichment_publication_row_bindings (
    binding_id TEXT NOT NULL
        REFERENCES requirement_enrichment_publication_bindings(binding_id)
        ON DELETE CASCADE,
    publication_order INTEGER NOT NULL CHECK(publication_order >= 0),
    requirement_tree_node_id TEXT NOT NULL,
    requirement_entry_id TEXT NOT NULL UNIQUE
        REFERENCES requirements(id) ON DELETE CASCADE,
    projection_row_sha256 TEXT NOT NULL CHECK(length(projection_row_sha256) = 64),
    decision_trace_sha256 TEXT NOT NULL CHECK(length(decision_trace_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    PRIMARY KEY(binding_id, publication_order),
    UNIQUE(binding_id, requirement_tree_node_id)
);
