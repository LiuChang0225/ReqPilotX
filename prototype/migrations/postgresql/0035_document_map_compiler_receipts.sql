ALTER TABLE agent_structure_working_revisions
    ADD COLUMN IF NOT EXISTS d2_compiler_receipt_sha256 TEXT;

ALTER TABLE agent_structure_working_revisions
    ADD CONSTRAINT agent_structure_working_revisions_d2_receipt_sha
    CHECK (
        d2_compiler_receipt_sha256 IS NULL
        OR d2_compiler_receipt_sha256 ~ '^[0-9a-f]{64}$'
    );
