-- Ephemeral durable-worker fleet inventory.
--
-- Worker identity remains an internal lease key. Public projections must omit
-- worker_id and never persist or expose an operating-system PID.
CREATE TABLE IF NOT EXISTS runtime_worker_fleet (
    worker_id TEXT PRIMARY KEY,
    slot TEXT NOT NULL CHECK(length(slot) BETWEEN 1 AND 64),
    claim_profile TEXT NOT NULL CHECK(length(claim_profile) BETWEEN 1 AND 64),
    accepted_kinds TEXT[] NOT NULL,
    connection_revision BIGINT NOT NULL CHECK(connection_revision >= 0),
    source_revision TEXT NOT NULL CHECK(length(source_revision) BETWEEN 1 AND 128),
    runtime_instance_id TEXT NOT NULL
        CHECK(length(runtime_instance_id) BETWEEN 1 AND 128),
    state TEXT NOT NULL
        CHECK(state IN ('IDLE', 'ACTIVE', 'DRAINING')),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runtime_worker_fleet_heartbeat
    ON runtime_worker_fleet(last_heartbeat_at DESC);

CREATE INDEX IF NOT EXISTS idx_runtime_worker_fleet_revision_state
    ON runtime_worker_fleet(connection_revision, state, last_heartbeat_at DESC);

CREATE INDEX IF NOT EXISTS idx_runtime_worker_fleet_accepted_kinds
    ON runtime_worker_fleet USING GIN(accepted_kinds);
