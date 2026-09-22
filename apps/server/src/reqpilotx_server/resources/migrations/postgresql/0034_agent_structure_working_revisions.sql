-- Immutable Host-verified D2 navigation-tree revisions.
-- Full customer-bearing projections live in the private object store; PostgreSQL
-- keeps only immutable identities/object receipts and the current CAS head.
CREATE TABLE IF NOT EXISTS agent_structure_working_revisions (
    revision_id TEXT PRIMARY KEY
        CHECK(revision_id ~ '^native-structure-working-revision-[0-9a-f]{32}$'),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL CHECK(run_id ~ '^agent-run-[0-9a-f]{32}$'),
    handoff_id TEXT CHECK(handoff_id IS NULL OR length(handoff_id) BETWEEN 1 AND 200),
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    base_artifact_id TEXT NOT NULL CHECK(length(base_artifact_id) BETWEEN 1 AND 200),
    base_projection_sha256 TEXT NOT NULL CHECK(length(base_projection_sha256) = 64),
    evidence_artifact_id TEXT NOT NULL CHECK(length(evidence_artifact_id) BETWEEN 1 AND 200),
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 100000),
    previous_revision_sha256 TEXT
        CHECK(previous_revision_sha256 IS NULL OR length(previous_revision_sha256) = 64),
    revision_sha256 TEXT NOT NULL CHECK(length(revision_sha256) = 64),
    validator_kind TEXT NOT NULL
        CHECK(validator_kind IN ('ACCEPTED_REFERENCE_REPLAY', 'D2_HOST_COMPILER')),
    validator_receipt_sha256 TEXT NOT NULL CHECK(length(validator_receipt_sha256) = 64),
    validation_status TEXT NOT NULL CHECK(validation_status = 'PASS'),
    state TEXT NOT NULL CHECK(state IN ('WORKING', 'ACCEPTED')),
    quality_claim TEXT NOT NULL
        CHECK(quality_claim IN ('TRANSPORT_REPLAY_ONLY', 'D2_HOST_VERIFIED')),
    object_key TEXT NOT NULL CHECK(length(object_key) BETWEEN 1 AND 1000),
    object_sha256 TEXT NOT NULL CHECK(length(object_sha256) = 64),
    object_size BIGINT NOT NULL CHECK(object_size > 0),
    media_type TEXT NOT NULL
        CHECK(media_type = 'application/vnd.reqpilotx.native-structure-working-revision+json'),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(project_id, sor_document_id, run_id, revision),
    UNIQUE(project_id, sor_document_id, revision_sha256),
    UNIQUE(object_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_structure_working_revisions_lineage
    ON agent_structure_working_revisions (
        project_id,
        sor_document_id,
        source_sha256,
        base_artifact_id,
        base_projection_sha256,
        run_id,
        revision
    );

CREATE TABLE IF NOT EXISTS agent_structure_working_revision_heads (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    base_artifact_id TEXT NOT NULL CHECK(length(base_artifact_id) BETWEEN 1 AND 200),
    base_projection_sha256 TEXT NOT NULL CHECK(length(base_projection_sha256) = 64),
    revision_id TEXT NOT NULL
        REFERENCES agent_structure_working_revisions(revision_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL CHECK(run_id ~ '^agent-run-[0-9a-f]{32}$'),
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 100000),
    revision_sha256 TEXT NOT NULL CHECK(length(revision_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('WORKING', 'ACCEPTED')),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (
        project_id,
        sor_document_id,
        source_sha256,
        base_artifact_id,
        base_projection_sha256
    )
);

CREATE INDEX IF NOT EXISTS idx_agent_structure_working_revision_heads_current
    ON agent_structure_working_revision_heads (
        project_id,
        sor_document_id,
        source_sha256,
        updated_at DESC
    );
