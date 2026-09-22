-- Durable structure-loop attempts and public Agent Activity.
-- Source content remains in canonical evidence/artifacts; these tables store
-- immutable request/receipt envelopes and content-safe public events only.
CREATE TABLE IF NOT EXISTS agent_structure_handoff_attempts (
    attempt_id TEXT PRIMARY KEY
        CHECK(attempt_id ~ '^native-structure-handoff-attempt-[0-9a-f]{32}$'),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    artifact_id TEXT NOT NULL CHECK(length(artifact_id) BETWEEN 1 AND 200),
    handoff_id TEXT NOT NULL CHECK(length(handoff_id) BETWEEN 1 AND 200),
    command_id TEXT NOT NULL CHECK(length(command_id) BETWEEN 1 AND 200),
    attempt_ordinal INTEGER NOT NULL CHECK(attempt_ordinal BETWEEN 1 AND 64),
    previous_attempt_receipt_sha256 TEXT
        CHECK(previous_attempt_receipt_sha256 IS NULL OR length(previous_attempt_receipt_sha256) = 64),
    status TEXT NOT NULL
        CHECK(status IN ('VERIFIED', 'VERIFICATION_REQUIRED', 'REVISION_REQUIRED')),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256) = 64),
    request_payload JSONB NOT NULL CHECK(jsonb_typeof(request_payload) = 'object'),
    receipt_payload JSONB NOT NULL CHECK(jsonb_typeof(receipt_payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(project_id, sor_document_id, source_sha256, artifact_id, handoff_id, command_id),
    UNIQUE(project_id, sor_document_id, source_sha256, artifact_id, handoff_id, attempt_ordinal),
    UNIQUE(receipt_sha256)
);

CREATE INDEX IF NOT EXISTS idx_agent_structure_handoff_attempts_stream
    ON agent_structure_handoff_attempts (
        project_id,
        sor_document_id,
        source_sha256,
        artifact_id,
        handoff_id,
        attempt_ordinal
    );

CREATE TABLE IF NOT EXISTS agent_structure_activity_events (
    event_id TEXT PRIMARY KEY
        CHECK(event_id ~ '^agent-activity-[0-9a-f]{32}$'),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    artifact_id TEXT NOT NULL CHECK(length(artifact_id) BETWEEN 1 AND 200),
    handoff_id TEXT NOT NULL CHECK(length(handoff_id) BETWEEN 1 AND 200),
    run_id TEXT NOT NULL CHECK(run_id ~ '^agent-run-[0-9a-f]{32}$'),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    command_id TEXT NOT NULL CHECK(length(command_id) BETWEEN 1 AND 200),
    command_payload_sha256 TEXT NOT NULL CHECK(length(command_payload_sha256) = 64),
    event_payload_sha256 TEXT NOT NULL CHECK(length(event_payload_sha256) = 64),
    event_payload JSONB NOT NULL CHECK(jsonb_typeof(event_payload) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id, sor_document_id, source_sha256, artifact_id, handoff_id, sequence),
    UNIQUE(project_id, sor_document_id, source_sha256, artifact_id, handoff_id, command_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_structure_activity_events_stream
    ON agent_structure_activity_events (
        project_id,
        sor_document_id,
        source_sha256,
        artifact_id,
        handoff_id,
        sequence
    );
