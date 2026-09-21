-- W6-B: durable, independently recoverable analysis-node execution.
--
-- W6-A jobs remain valid ATOMIC_BATCH/v1 records.  W6-B jobs use the v2
-- scope envelope and PER_NODE_RECOVERABLE publication.  No historical job is
-- assigned targets because its exact frozen node order cannot be recovered.

ALTER TABLE assessment_runs
    ADD COLUMN IF NOT EXISTS analysis_node_attempt_id TEXT NOT NULL DEFAULT '';

ALTER TABLE assessment_runs
    ADD COLUMN IF NOT EXISTS run_index INTEGER NOT NULL DEFAULT -1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_assessment_runs_node_attempt_index
    ON assessment_runs(analysis_node_attempt_id, run_index)
    WHERE analysis_node_attempt_id <> '';

ALTER TABLE ai_analysis_jobs
    DROP CONSTRAINT IF EXISTS ck_ai_analysis_jobs_scope_kind;

ALTER TABLE ai_analysis_jobs
    DROP CONSTRAINT IF EXISTS ck_ai_analysis_jobs_scope_identity;

ALTER TABLE ai_analysis_jobs
    DROP CONSTRAINT IF EXISTS ck_ai_analysis_jobs_publication_mode;

ALTER TABLE ai_analysis_jobs
    DROP CONSTRAINT IF EXISTS ck_ai_analysis_jobs_scope_publication_pair;

ALTER TABLE ai_analysis_jobs
    ADD CONSTRAINT ck_ai_analysis_jobs_scope_kind
    CHECK (scope_kind IN ('', 'CURRENT', 'SELECTED', 'ALL', 'FAILED_ONLY'));

ALTER TABLE ai_analysis_jobs
    ADD CONSTRAINT ck_ai_analysis_jobs_publication_mode
    CHECK (publication_mode IN ('ATOMIC_BATCH', 'PER_NODE_RECOVERABLE'));

ALTER TABLE ai_analysis_jobs
    ADD CONSTRAINT ck_ai_analysis_jobs_scope_identity
    CHECK (
        (
            scope_sha256 = ''
            AND scope_schema_version = ''
            AND scope_kind = ''
            AND command_id = ''
            AND command_sha256 = ''
        )
        OR (
            scope_sha256 ~ '^[0-9a-f]{64}$'
            AND command_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'
            AND command_sha256 ~ '^[0-9a-f]{64}$'
            AND (
                (
                    scope_schema_version = 'requirement-analysis-scope/v1'
                    AND scope_kind IN ('CURRENT', 'SELECTED', 'ALL')
                )
                OR (
                    scope_schema_version = 'requirement-analysis-scope/v2'
                    AND scope_kind IN (
                        'CURRENT', 'SELECTED', 'ALL', 'FAILED_ONLY'
                    )
                )
            )
        )
    );

ALTER TABLE ai_analysis_jobs
    ADD CONSTRAINT ck_ai_analysis_jobs_scope_publication_pair
    CHECK (
        (
            scope_schema_version IN ('', 'requirement-analysis-scope/v1')
            AND publication_mode = 'ATOMIC_BATCH'
        )
        OR (
            scope_schema_version = 'requirement-analysis-scope/v2'
            AND publication_mode = 'PER_NODE_RECOVERABLE'
        )
    );

CREATE TABLE IF NOT EXISTS ai_analysis_job_targets (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES ai_analysis_jobs(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL
        REFERENCES sor_documents(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL
        REFERENCES requirements(id) ON DELETE CASCADE,
    requirement_code TEXT NOT NULL DEFAULT '',
    source_order INTEGER NOT NULL CHECK(source_order >= 0),
    status TEXT NOT NULL DEFAULT 'WAITING'
        CHECK(status IN (
            'WAITING', 'RUNNING', 'PUBLISHING',
            'SUCCEEDED', 'FAILED', 'CANCELLED'
        )),
    latest_attempt_no INTEGER NOT NULL DEFAULT 0
        CHECK(latest_attempt_no >= 0),
    latest_attempt_id TEXT NOT NULL DEFAULT '',
    published_attempt_id TEXT NOT NULL DEFAULT '',
    retryable INTEGER NOT NULL DEFAULT 1 CHECK(retryable IN (0, 1)),
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, node_id),
    UNIQUE(job_id, requirement_id),
    UNIQUE(job_id, source_order),
    CHECK(latest_attempt_id = '' OR latest_attempt_no >= 1),
    CHECK(
        status NOT IN ('RUNNING', 'PUBLISHING', 'SUCCEEDED')
        OR latest_attempt_id <> ''
    ),
    CHECK(status <> 'SUCCEEDED' OR published_attempt_id <> '')
);

CREATE INDEX IF NOT EXISTS idx_ai_analysis_job_targets_job_order
    ON ai_analysis_job_targets(job_id, source_order);

CREATE INDEX IF NOT EXISTS idx_ai_analysis_job_targets_job_status
    ON ai_analysis_job_targets(job_id, status, source_order);

CREATE TABLE IF NOT EXISTS ai_analysis_node_attempts (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL
        REFERENCES ai_analysis_job_targets(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES ai_analysis_jobs(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL
        REFERENCES requirements(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
    fence_token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL
        CHECK(status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    result_schema_version TEXT NOT NULL DEFAULT '',
    result_payload_json TEXT NOT NULL DEFAULT '',
    result_sha256 TEXT NOT NULL DEFAULT '',
    analysis_input_schema_version TEXT NOT NULL DEFAULT '',
    analysis_input_sha256 TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(target_id, attempt_no),
    CHECK(
        CASE
            WHEN result_payload_json = '' THEN TRUE
            ELSE jsonb_typeof(result_payload_json::jsonb) = 'object'
        END
    ),
    CHECK(
        (result_schema_version = '' AND result_sha256 = '')
        OR (
            result_schema_version <> ''
            AND result_payload_json <> ''
            AND result_sha256 ~ '^[0-9a-f]{64}$'
        )
    ),
    CHECK(
        status <> 'SUCCEEDED'
        OR (
            result_schema_version <> ''
            AND result_payload_json <> ''
            AND result_sha256 ~ '^[0-9a-f]{64}$'
        )
    ),
    CHECK(
        (
            analysis_input_schema_version = ''
            AND analysis_input_sha256 = ''
        )
        OR (
            analysis_input_schema_version <> ''
            AND analysis_input_sha256 ~ '^[0-9a-f]{64}$'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_ai_analysis_node_attempts_job_target
    ON ai_analysis_node_attempts(job_id, target_id, attempt_no DESC);
