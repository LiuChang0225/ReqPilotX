ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS compliance_status TEXT NOT NULL DEFAULT '';

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS evidence_findings_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS structured_information_gap_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS sub_judgments_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE assessments
    DROP CONSTRAINT IF EXISTS assessments_compliance_status_valid;

ALTER TABLE assessments
    ADD CONSTRAINT assessments_compliance_status_valid
    CHECK (
        compliance_status IN (
            '',
            'COMPLIANT',
            'PARTIALLY_COMPLIANT',
            'NON_COMPLIANT',
            'INSUFFICIENT_INFORMATION'
        )
    );

ALTER TABLE evidences
    ADD COLUMN IF NOT EXISTS retrieval_evidence_id TEXT NOT NULL DEFAULT '';

ALTER TABLE evidences
    ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE evidences
    ADD COLUMN IF NOT EXISTS evidence_contract_version TEXT NOT NULL DEFAULT '';

ALTER TABLE evidences
    ADD COLUMN IF NOT EXISTS content_kind TEXT NOT NULL DEFAULT '';

ALTER TABLE evidences
    ADD COLUMN IF NOT EXISTS evidence_metadata_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE evidences
    DROP CONSTRAINT IF EXISTS evidences_content_hash_valid;

ALTER TABLE evidences
    ADD CONSTRAINT evidences_content_hash_valid
    CHECK (content_hash = '' OR length(content_hash) = 64);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evidences_requirement_retrieval_id
    ON evidences(requirement_id, retrieval_evidence_id)
    WHERE retrieval_evidence_id <> '';
