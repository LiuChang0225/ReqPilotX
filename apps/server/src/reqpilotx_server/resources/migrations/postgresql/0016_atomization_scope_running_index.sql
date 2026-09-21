CREATE INDEX IF NOT EXISTS idx_requirement_atomization_scope_attempt_running
    ON requirement_atomization_scope_attempts(
        lineage_sha256, expected_scope_revision, status, attempt_sequence
    );
