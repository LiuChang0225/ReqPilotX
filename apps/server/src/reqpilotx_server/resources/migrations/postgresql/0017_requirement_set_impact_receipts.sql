ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS result_origin TEXT NOT NULL DEFAULT 'NATIVE';

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS source_assessment_id TEXT NOT NULL DEFAULT '';

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS impact_receipt_id TEXT NOT NULL DEFAULT '';

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS analysis_input_schema_version TEXT NOT NULL DEFAULT '';

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS analysis_input_sha256 TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS requirement_set_impact_receipts (
    receipt_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    old_requirement_set_id TEXT NOT NULL
        REFERENCES requirement_sets(id) ON DELETE CASCADE,
    new_requirement_set_id TEXT NOT NULL
        REFERENCES requirement_sets(id) ON DELETE CASCADE,
    old_tree_sha256 TEXT NOT NULL CHECK (length(old_tree_sha256) = 64),
    new_tree_sha256 TEXT NOT NULL CHECK (length(new_tree_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (new_requirement_set_id, payload_sha256)
);

CREATE INDEX IF NOT EXISTS idx_requirement_set_impact_receipts_pair
    ON requirement_set_impact_receipts (
        old_requirement_set_id,
        new_requirement_set_id,
        created_at
    );

CREATE INDEX IF NOT EXISTS idx_assessments_impact_receipt
    ON assessments(impact_receipt_id)
    WHERE impact_receipt_id <> '';
