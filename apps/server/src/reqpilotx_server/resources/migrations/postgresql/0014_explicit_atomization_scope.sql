ALTER TABLE requirement_atomization_runs
    ADD COLUMN IF NOT EXISTS atomization_revision INTEGER NOT NULL DEFAULT 0
        CHECK(atomization_revision >= 0);

DO $$
DECLARE
    old_constraint_name TEXT;
BEGIN
    SELECT constraint_row.conname
      INTO old_constraint_name
      FROM (
          SELECT constraint_def.conname,
                 ARRAY_AGG(attribute.attname ORDER BY key_column.ordinality) AS columns
            FROM pg_constraint AS constraint_def
            JOIN LATERAL UNNEST(constraint_def.conkey)
                 WITH ORDINALITY AS key_column(attnum, ordinality) ON TRUE
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = constraint_def.conrelid
             AND attribute.attnum = key_column.attnum
           WHERE constraint_def.conrelid = 'requirement_atomization_runs'::regclass
             AND constraint_def.contype = 'u'
           GROUP BY constraint_def.conname
      ) AS constraint_row
     WHERE constraint_row.columns = ARRAY[
         'project_id', 'document_id', 'session_id',
         'review_revision', 'schema_version'
     ]::name[]
     LIMIT 1;

    IF old_constraint_name IS NOT NULL THEN
        EXECUTE FORMAT(
            'ALTER TABLE requirement_atomization_runs DROP CONSTRAINT %I',
            old_constraint_name
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM (
              SELECT ARRAY_AGG(attribute.attname ORDER BY key_column.ordinality) AS columns
                FROM pg_constraint AS constraint_def
                JOIN LATERAL UNNEST(constraint_def.conkey)
                     WITH ORDINALITY AS key_column(attnum, ordinality) ON TRUE
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = constraint_def.conrelid
                 AND attribute.attnum = key_column.attnum
               WHERE constraint_def.conrelid = 'requirement_atomization_runs'::regclass
                 AND constraint_def.contype = 'u'
               GROUP BY constraint_def.conname
          ) AS constraint_row
         WHERE constraint_row.columns = ARRAY[
             'project_id', 'document_id', 'session_id', 'review_revision',
             'atomization_revision', 'schema_version'
         ]::name[]
    ) THEN
        ALTER TABLE requirement_atomization_runs
            ADD CONSTRAINT uq_requirement_atomization_run_revision
            UNIQUE (
                project_id, document_id, session_id, review_revision,
                atomization_revision, schema_version
            );
    END IF;
END
$$;

DROP INDEX IF EXISTS idx_requirement_atomization_session;
DROP INDEX IF EXISTS idx_requirement_atomization_session_v3;
CREATE INDEX IF NOT EXISTS idx_requirement_atomization_session_v4
    ON requirement_atomization_runs(
        project_id, document_id, session_id, schema_version,
        review_revision DESC, atomization_revision DESC
    );

CREATE TABLE IF NOT EXISTS requirement_atomization_scope_receipts (
    receipt_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    scope_revision INTEGER NOT NULL CHECK(scope_revision >= 1),
    command_id TEXT NOT NULL,
    command_sha256 TEXT NOT NULL CHECK(length(command_sha256) = 64),
    atomization_run_id TEXT NOT NULL
        REFERENCES requirement_atomization_runs(atomization_run_id) ON DELETE CASCADE,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(lineage_sha256, scope_revision),
    UNIQUE(lineage_sha256, command_id)
);

CREATE INDEX IF NOT EXISTS idx_requirement_atomization_scope_head
    ON requirement_atomization_scope_receipts(
        project_id, document_id, session_id, lineage_sha256,
        scope_revision DESC
    );

CREATE TABLE IF NOT EXISTS requirement_atomization_scope_attempts (
    attempt_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    attempt_sequence INTEGER NOT NULL CHECK(attempt_sequence >= 1),
    command_id TEXT NOT NULL,
    command_sha256 TEXT NOT NULL CHECK(length(command_sha256) = 64),
    expected_scope_revision INTEGER NOT NULL CHECK(expected_scope_revision >= 0),
    status TEXT NOT NULL CHECK(status IN ('RUNNING', 'INTERRUPTED', 'SUCCEEDED')),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(lineage_sha256, attempt_sequence)
);

CREATE INDEX IF NOT EXISTS idx_requirement_atomization_scope_attempt_head
    ON requirement_atomization_scope_attempts(
        project_id, document_id, session_id, lineage_sha256,
        attempt_sequence DESC
    );

CREATE INDEX IF NOT EXISTS idx_requirement_atomization_scope_attempt_command
    ON requirement_atomization_scope_attempts(
        lineage_sha256, command_id, attempt_sequence
    );
