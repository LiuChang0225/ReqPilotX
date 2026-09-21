-- Durable, immutable Agent Structure proposals.
--
-- These rows are orchestration artifacts only. They never grant Apply authority;
-- Structure mutation still goes through the existing validator/CAS use case.
CREATE TABLE IF NOT EXISTS agent_structure_proposals (
    id TEXT PRIMARY KEY
        CHECK(id ~ '^agent-structure-proposal-[0-9a-f]{32}$'),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    facts_sha256 TEXT NOT NULL CHECK(length(facts_sha256) = 64),
    proposal_revision INTEGER NOT NULL CHECK(proposal_revision > 0),
    supersedes_proposal_id TEXT REFERENCES agent_structure_proposals(id),
    structure_patch_proposal_id TEXT NOT NULL
        CHECK(structure_patch_proposal_id ~ '^structure-proposal-[0-9a-f]{32}$'),
    proposal_payload_sha256 TEXT NOT NULL CHECK(length(proposal_payload_sha256) = 64),
    proposal_payload JSONB NOT NULL
        CHECK(jsonb_typeof(proposal_payload) = 'object'),
    runtime_receipt JSONB NOT NULL
        CHECK(jsonb_typeof(runtime_receipt) = 'object'),
    runtime_thread_id TEXT NOT NULL DEFAULT ''
        CHECK(length(runtime_thread_id) <= 200),
    status TEXT NOT NULL
        CHECK(status IN ('READY', 'REJECTED', 'APPLIED', 'STALE', 'EXPIRED')),
    status_reason_code TEXT NOT NULL DEFAULT ''
        CHECK(length(status_reason_code) <= 120),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_structure_revision_id TEXT NOT NULL DEFAULT ''
        CHECK(length(applied_structure_revision_id) <= 160),
    applied_structure_revision_number INTEGER NOT NULL DEFAULT 0
        CHECK(applied_structure_revision_number >= 0),
    applied_effective_snapshot_id TEXT NOT NULL DEFAULT ''
        CHECK(length(applied_effective_snapshot_id) <= 160),
    applied_command_id TEXT NOT NULL DEFAULT ''
        CHECK(length(applied_command_id) <= 200),
    applied_at TIMESTAMPTZ,
    CHECK(expires_at > created_at),
    CHECK(
        (status = 'APPLIED'
            AND applied_structure_revision_id <> ''
            AND applied_structure_revision_number > 0
            AND applied_effective_snapshot_id <> ''
            AND applied_command_id <> ''
            AND applied_at IS NOT NULL)
        OR
        (status <> 'APPLIED'
            AND applied_structure_revision_id = ''
            AND applied_structure_revision_number = 0
            AND applied_effective_snapshot_id = ''
            AND applied_command_id = ''
            AND applied_at IS NULL)
    ),
    UNIQUE(sor_document_id, proposal_revision),
    UNIQUE(sor_document_id, structure_patch_proposal_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_structure_proposals_document_latest
    ON agent_structure_proposals(sor_document_id, proposal_revision DESC);

CREATE INDEX IF NOT EXISTS idx_agent_structure_proposals_status_expiry
    ON agent_structure_proposals(status, expires_at);

CREATE TABLE IF NOT EXISTS agent_structure_proposal_events (
    id BIGSERIAL PRIMARY KEY,
    proposal_id TEXT NOT NULL
        REFERENCES agent_structure_proposals(id) ON DELETE CASCADE,
    command_id TEXT NOT NULL CHECK(length(command_id) BETWEEN 1 AND 200),
    event_type TEXT NOT NULL
        CHECK(event_type IN ('CREATED', 'REJECTED', 'APPLIED', 'STALE', 'EXPIRED')),
    from_status TEXT,
    to_status TEXT NOT NULL
        CHECK(to_status IN ('READY', 'REJECTED', 'APPLIED', 'STALE', 'EXPIRED')),
    event_payload_sha256 TEXT NOT NULL CHECK(length(event_payload_sha256) = 64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(proposal_id, command_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_structure_proposal_events_proposal
    ON agent_structure_proposal_events(proposal_id, id);
