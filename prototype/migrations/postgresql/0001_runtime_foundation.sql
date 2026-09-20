CREATE TABLE IF NOT EXISTS runtime_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN (
            'PENDING',
            'RUNNING',
            'CANCEL_REQUESTED',
            'SUCCEEDED',
            'FAILED',
            'CANCELLED'
        )),
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 20),
    generation BIGINT NOT NULL DEFAULT 0 CHECK(generation >= 0),
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TIMESTAMPTZ,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(kind, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_runtime_jobs_claim
    ON runtime_jobs(status, available_at, priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_runtime_jobs_aggregate
    ON runtime_jobs(aggregate_type, aggregate_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_runtime_jobs_lease
    ON runtime_jobs(lease_expires_at)
    WHERE status IN ('RUNNING', 'CANCEL_REQUESTED');

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_jobs_one_active_aggregate
    ON runtime_jobs(kind, aggregate_type, aggregate_id)
    WHERE status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED');
