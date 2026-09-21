-- Cross-process, structured steering inbox for one active Agent Structure turn.
-- No free-form customer text is persisted; delivery has no business Apply authority.
CREATE TABLE IF NOT EXISTS agent_structure_steer_commands (
    id TEXT PRIMARY KEY
        CHECK(id ~ '^agent-structure-steer-[0-9a-f]{32}$'),
    job_id TEXT NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    command_id TEXT NOT NULL CHECK(length(command_id) BETWEEN 1 AND 200),
    expected_job_generation INTEGER NOT NULL CHECK(expected_job_generation > 0),
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    expected_revision_number INTEGER NOT NULL CHECK(expected_revision_number >= 0),
    expected_effective_snapshot_id TEXT NOT NULL,
    intent TEXT NOT NULL CHECK(intent IN (
        'REVIEW_PARENTAGE', 'REVIEW_ORDER', 'REVIEW_BOUNDARY', 'REVIEW_RELATIONS'
    )),
    focus_structure_node_ids JSONB NOT NULL
        CHECK(jsonb_typeof(focus_structure_node_ids) = 'array'),
    suggested_operations JSONB NOT NULL
        CHECK(jsonb_typeof(suggested_operations) = 'array'),
    suggested_operations_authority TEXT NOT NULL
        CHECK(suggested_operations_authority IN (
            'NONE', 'USER_REQUESTED', 'DETERMINISTIC_FINDING'
        )),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    status TEXT NOT NULL
        CHECK(status IN ('PENDING', 'DELIVERING', 'DELIVERED', 'REJECTED')),
    status_reason_code TEXT NOT NULL DEFAULT ''
        CHECK(length(status_reason_code) <= 120),
    runtime_thread_id TEXT NOT NULL DEFAULT '' CHECK(length(runtime_thread_id) <= 200),
    runtime_turn_id TEXT NOT NULL DEFAULT '' CHECK(length(runtime_turn_id) <= 200),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(job_id, command_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_structure_steers_job_pending
    ON agent_structure_steer_commands(job_id, expected_job_generation, status, created_at);
