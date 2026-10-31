CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    processing_profile_json TEXT NOT NULL DEFAULT '',
    processing_profile_revision INTEGER NOT NULL DEFAULT 0,
    processing_profile_sha256 TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entry_command_receipts (
    kind TEXT NOT NULL CHECK(kind IN ('project.create', 'document.import')),
    scope_id TEXT NOT NULL,
    command_id TEXT NOT NULL CHECK(length(command_id) BETWEEN 1 AND 200),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    resource_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(kind, scope_id, command_id),
    CHECK((kind = 'project.create' AND scope_id = '') OR
          (kind = 'document.import' AND scope_id <> ''))
);

CREATE TABLE IF NOT EXISTS sor_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    extracted_text TEXT NOT NULL,
    source_sha256 TEXT NOT NULL DEFAULT '',
    source_size INTEGER NOT NULL DEFAULT 0,
    declared_extension TEXT NOT NULL DEFAULT '',
    detected_format TEXT NOT NULL DEFAULT '',
    preflight_schema_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sor_documents_project_id ON sor_documents(project_id);

CREATE TABLE IF NOT EXISTS stored_objects (
    storage_key TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    object_kind TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_asset_managed_objects (
    storage_key TEXT PRIMARY KEY,
    legacy_protected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(storage_key) REFERENCES stored_objects(storage_key)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS source_capture_snapshots (
    capture_id TEXT PRIMARY KEY,
    source_ir_id TEXT NOT NULL UNIQUE,
    source_ir_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    schema_version TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('COMPLETE', 'PARTIAL')),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    facts_sha256 TEXT NOT NULL CHECK(length(facts_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_capture_snapshots_source
    ON source_capture_snapshots(source_sha256, source_ir_id);

CREATE TABLE IF NOT EXISTS source_capture_asset_publications (
    publication_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    status TEXT NOT NULL CHECK(status IN ('STAGING', 'ABORTED')),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(publication_id, project_id, sor_document_id, source_sha256),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS source_capture_asset_staging_references (
    publication_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    storage_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(publication_id, storage_key),
    FOREIGN KEY(publication_id, project_id, sor_document_id, source_sha256)
        REFERENCES source_capture_asset_publications(
            publication_id, project_id, sor_document_id, source_sha256
        ) ON DELETE CASCADE,
    FOREIGN KEY(storage_key) REFERENCES stored_objects(storage_key)
        ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_source_capture_asset_staging_storage_key
    ON source_capture_asset_staging_references(storage_key);

CREATE TABLE IF NOT EXISTS source_capture_bindings (
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    capture_id TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(project_id, sor_document_id, source_sha256),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(capture_id) REFERENCES source_capture_snapshots(capture_id)
);
CREATE INDEX IF NOT EXISTS idx_source_capture_bindings_capture
    ON source_capture_bindings(capture_id);

CREATE TABLE IF NOT EXISTS source_capture_asset_references (
    capture_id TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(capture_id, storage_key),
    FOREIGN KEY(capture_id) REFERENCES source_capture_snapshots(capture_id)
        ON DELETE CASCADE,
    FOREIGN KEY(storage_key) REFERENCES stored_objects(storage_key)
        ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_source_capture_asset_refs_storage_key
    ON source_capture_asset_references(storage_key);

CREATE TABLE IF NOT EXISTS source_visual_capture_generations (
    generation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    base_capture_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    UNIQUE(generation_id, project_id, sor_document_id, source_sha256),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(base_capture_id) REFERENCES source_capture_snapshots(capture_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_visual_generations_document
    ON source_visual_capture_generations(
        project_id, sor_document_id, source_sha256, created_at_utc DESC
    );

CREATE TABLE IF NOT EXISTS source_visual_capture_bindings (
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    generation_id TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(project_id, sor_document_id, source_sha256),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(generation_id, project_id, sor_document_id, source_sha256)
        REFERENCES source_visual_capture_generations(
            generation_id, project_id, sor_document_id, source_sha256
        ) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_visual_bindings_generation
    ON source_visual_capture_bindings(generation_id);

CREATE TABLE IF NOT EXISTS source_visual_page_snapshots (
    generation_id TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK(page_number >= 1),
    visual_snapshot_id TEXT NOT NULL,
    page_image_sha256 TEXT NOT NULL CHECK(length(page_image_sha256) = 64),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(generation_id, page_number),
    FOREIGN KEY(generation_id)
        REFERENCES source_visual_capture_generations(generation_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_visual_pages_snapshot
    ON source_visual_page_snapshots(visual_snapshot_id);

CREATE TABLE IF NOT EXISTS source_visual_page_attempts (
    attempt_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    page_number INTEGER NOT NULL CHECK(page_number >= 1),
    status TEXT NOT NULL CHECK(status IN ('FAILED')),
    failure_code TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(generation_id, project_id, sor_document_id, source_sha256)
        REFERENCES source_visual_capture_generations(
            generation_id, project_id, sor_document_id, source_sha256
        ) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_visual_attempts_page
    ON source_visual_page_attempts(
        generation_id, page_number, created_at_utc DESC
    );

CREATE TABLE IF NOT EXISTS reading_sessions (
    reading_session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    capture_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'READY', 'PARTIAL', 'STALE', 'FAILED')),
    overview_json TEXT NOT NULL,
    overview_sha256 TEXT NOT NULL CHECK(length(overview_sha256) = 64),
    structure_map_json TEXT NOT NULL,
    structure_map_sha256 TEXT NOT NULL CHECK(length(structure_map_sha256) = 64),
    selectable_nodes_json TEXT NOT NULL,
    selectable_nodes_sha256 TEXT NOT NULL CHECK(length(selectable_nodes_sha256) = 64),
    effective_bundle_id TEXT NOT NULL DEFAULT '',
    visual_generation_id TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(project_id, sor_document_id, source_sha256),
    UNIQUE(reading_session_id, project_id, sor_document_id, source_sha256),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(capture_id) REFERENCES source_capture_snapshots(capture_id)
);
CREATE INDEX IF NOT EXISTS idx_reading_sessions_document
    ON reading_sessions(project_id, sor_document_id, updated_at_utc DESC);

CREATE TABLE IF NOT EXISTS reading_artifacts (
    artifact_id TEXT PRIMARY KEY,
    reading_session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    action TEXT NOT NULL CHECK(action IN ('EXPLANATION', 'TRANSLATION', 'ENGINEERING_IMPACT', 'CLARIFICATION_QUESTION')),
    source_ref_json TEXT NOT NULL,
    source_ref_sha256 TEXT NOT NULL CHECK(length(source_ref_sha256) = 64),
    output_text TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STALE', 'FAILED')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(reading_session_id, project_id, sor_document_id, source_sha256)
        REFERENCES reading_sessions(reading_session_id, project_id, sor_document_id, source_sha256)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reading_artifacts_session
    ON reading_artifacts(reading_session_id, created_at_utc DESC);

CREATE TABLE IF NOT EXISTS reading_notes (
    note_id TEXT PRIMARY KEY,
    reading_session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    source_ref_json TEXT NOT NULL,
    source_ref_sha256 TEXT NOT NULL CHECK(length(source_ref_sha256) = 64),
    content TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STALE', 'DELETED')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY(reading_session_id, project_id, sor_document_id, source_sha256)
        REFERENCES reading_sessions(reading_session_id, project_id, sor_document_id, source_sha256)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reading_notes_session
    ON reading_notes(reading_session_id, updated_at_utc DESC);

CREATE TABLE IF NOT EXISTS requirement_sets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    is_active INTEGER NOT NULL DEFAULT 0,
    source_hash TEXT NOT NULL DEFAULT '',
    preview_generation_id TEXT NOT NULL DEFAULT '',
    supersedes_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    activated_at TEXT NOT NULL DEFAULT '',
    locked_at TEXT NOT NULL DEFAULT '',
    lock_waiver_reason TEXT NOT NULL DEFAULT '',
    preview_status_at_lock TEXT NOT NULL DEFAULT '',
    requirement_tree_object_key TEXT NOT NULL DEFAULT '',
    requirement_tree_sha256 TEXT NOT NULL DEFAULT '',
    requirement_tree_schema_version TEXT NOT NULL DEFAULT '',
    structure_snapshot_id TEXT NOT NULL DEFAULT '',
    structure_tree_sha256 TEXT NOT NULL DEFAULT '',
    structure_revision INTEGER NOT NULL DEFAULT 0,
    publication_key TEXT NOT NULL DEFAULT '',
    publication_state TEXT NOT NULL DEFAULT '',
    publication_owner_token TEXT NOT NULL DEFAULT '',
    publication_lease_expires_at TEXT NOT NULL DEFAULT '',
    publication_heartbeat_at TEXT NOT NULL DEFAULT '',
    entry_classification_snapshot_id TEXT NOT NULL DEFAULT '',
    entry_classification_snapshot_sha256 TEXT NOT NULL DEFAULT '',
    enrichment_publication_binding_id TEXT NOT NULL DEFAULT '',
    enrichment_publication_binding_sha256 TEXT NOT NULL DEFAULT '',
    CHECK(
        (enrichment_publication_binding_id = '' AND
         enrichment_publication_binding_sha256 = '')
        OR
        (enrichment_publication_binding_id <> '' AND
         length(enrichment_publication_binding_sha256) = 64)
    ),
    UNIQUE(sor_document_id, version),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_requirement_sets_document
    ON requirement_sets(sor_document_id, version DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_requirement_sets_active_document
    ON requirement_sets(sor_document_id) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS entry_classification_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    working_tree_id TEXT NOT NULL,
    working_tree_sha256 TEXT NOT NULL CHECK(length(working_tree_sha256) = 64),
    effective_structure_id TEXT NOT NULL,
    semantic_snapshot_id TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    status TEXT NOT NULL CHECK(status IN ('WORKING', 'ACCEPTED')),
    supersedes_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    command_sha256 TEXT NOT NULL CHECK(length(command_sha256) = 64),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256) = 64),
    created_at TEXT NOT NULL,
    accepted_at TEXT NOT NULL DEFAULT '',
    accepted_by TEXT NOT NULL DEFAULT '',
    UNIQUE (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, revision
    ),
    UNIQUE (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, command_id
    ),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entry_classification_snapshots_head
    ON entry_classification_snapshots (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, revision DESC
    );

CREATE TABLE IF NOT EXISTS entry_classification_command_receipts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    working_tree_id TEXT NOT NULL,
    working_tree_sha256 TEXT NOT NULL CHECK(length(working_tree_sha256) = 64),
    effective_structure_id TEXT NOT NULL,
    semantic_snapshot_id TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL,
    command_id TEXT NOT NULL,
    command_sha256 TEXT NOT NULL CHECK(length(command_sha256) = 64),
    result_snapshot_id TEXT NOT NULL,
    result_snapshot_sha256 TEXT NOT NULL
        CHECK(length(result_snapshot_sha256) = 64),
    created_at TEXT NOT NULL,
    envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256) = 64),
    UNIQUE (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, command_id
    ),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entry_classification_command_receipts_scope
    ON entry_classification_command_receipts (
        project_id, sor_document_id, source_sha256, working_tree_id,
        working_tree_sha256, effective_structure_id,
        semantic_snapshot_id, policy_version, command_id
    );

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    extracted_text TEXT NOT NULL,
    source_sha256 TEXT NOT NULL DEFAULT '',
    source_size INTEGER NOT NULL DEFAULT 0,
    declared_extension TEXT NOT NULL DEFAULT '',
    detected_format TEXT NOT NULL DEFAULT '',
    preflight_schema_version TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL DEFAULT 'REFERENCE',
    doc_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    effective_from TEXT NOT NULL DEFAULT '',
    effective_to TEXT NOT NULL DEFAULT '',
    authority TEXT NOT NULL DEFAULT 'UNKNOWN',
    claim_role TEXT NOT NULL DEFAULT 'UNKNOWN',
    applicability TEXT NOT NULL DEFAULT 'UNKNOWN',
    retrieval_sync_status TEXT NOT NULL DEFAULT 'PENDING',
    retrieval_sync_error TEXT NOT NULL DEFAULT '',
    retrieval_synced_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_project_id ON knowledge_documents(project_id);

CREATE TABLE IF NOT EXISTS requirements (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    requirement_set_id TEXT NOT NULL DEFAULT '',
    req_code TEXT NOT NULL,
    source_page INTEGER,
    source_anchor TEXT,
    requirement_text TEXT NOT NULL,
    requirement_type TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requirement_set_id, req_code),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(requirement_set_id) REFERENCES requirement_sets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_requirements_project_id ON requirements(project_id);
CREATE INDEX IF NOT EXISTS idx_requirements_document_id ON requirements(sor_document_id);

CREATE TABLE IF NOT EXISTS assessments (
    id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL UNIQUE,
    profile_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    retrieval_backend TEXT NOT NULL DEFAULT '',
    ai_decision TEXT NOT NULL,
    compliance_status TEXT NOT NULL DEFAULT '',
    ai_reason TEXT NOT NULL,
    ai_confidence REAL NOT NULL,
    assessment_mode TEXT NOT NULL DEFAULT 'heuristic',
    analysis_label TEXT NOT NULL DEFAULT 'REQUIREMENT',
    analysis_basis TEXT NOT NULL DEFAULT '',
    normalized_requirement TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT 'HIGH',
    clarification_question TEXT NOT NULL DEFAULT '',
    final_decision TEXT,
    engineer_reason TEXT,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    evidence_findings_json TEXT NOT NULL DEFAULT '[]',
    structured_information_gap_json TEXT NOT NULL DEFAULT '{}',
    sub_judgments_json TEXT NOT NULL DEFAULT '[]',
    reference_status TEXT NOT NULL DEFAULT 'CURRENT',
    source_requirement_id TEXT NOT NULL DEFAULT '',
    result_origin TEXT NOT NULL DEFAULT 'NATIVE',
    source_assessment_id TEXT NOT NULL DEFAULT '',
    impact_receipt_id TEXT NOT NULL DEFAULT '',
    analysis_input_schema_version TEXT NOT NULL DEFAULT '',
    analysis_input_sha256 TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evidences (
    id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL,
    source_doc_id TEXT NOT NULL,
    source_doc_name TEXT NOT NULL,
    source_doc_version TEXT NOT NULL DEFAULT '',
    retrieval_evidence_id TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    evidence_contract_version TEXT NOT NULL DEFAULT '',
    content_kind TEXT NOT NULL DEFAULT '',
    evidence_metadata_json TEXT NOT NULL DEFAULT '{}',
    source_location TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    relevance REAL NOT NULL,
    FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_evidences_requirement_id ON evidences(requirement_id);

CREATE TABLE IF NOT EXISTS assessment_runs (
    id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL,
    analysis_node_attempt_id TEXT NOT NULL DEFAULT '',
    run_index INTEGER NOT NULL DEFAULT -1,
    profile_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    retrieval_backend TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_payload_json TEXT NOT NULL,
    output_payload_json TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    fallback_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_assessment_runs_requirement_id ON assessment_runs(requirement_id);

CREATE TABLE IF NOT EXISTS ai_analysis_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    requirement_set_id TEXT NOT NULL DEFAULT '',
    requirement_set_version INTEGER NOT NULL DEFAULT 0,
    requirement_tree_sha256 TEXT NOT NULL DEFAULT '',
    knowledge_base_version INTEGER NOT NULL DEFAULT 0,
    command_id TEXT NOT NULL DEFAULT '',
    command_sha256 TEXT NOT NULL DEFAULT '',
    scope_schema_version TEXT NOT NULL DEFAULT '',
    scope_kind TEXT NOT NULL DEFAULT '',
    scope_payload_json TEXT NOT NULL DEFAULT '{}',
    scope_sha256 TEXT NOT NULL DEFAULT '',
    publication_mode TEXT NOT NULL DEFAULT 'ATOMIC_BATCH',
    profile_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    assessment_mode TEXT NOT NULL DEFAULT '',
    retrieval_backend TEXT NOT NULL DEFAULT '',
    chat_model TEXT NOT NULL DEFAULT '',
    embedding_model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    total_requirements INTEGER NOT NULL DEFAULT 0,
    completed_requirements INTEGER NOT NULL DEFAULT 0,
    published_requirements INTEGER NOT NULL DEFAULT 0,
    analyzed_requirements INTEGER NOT NULL DEFAULT 0,
    info_requirements INTEGER NOT NULL DEFAULT 0,
    screened_requirements INTEGER NOT NULL DEFAULT 0,
    current_requirement_id TEXT NOT NULL DEFAULT '',
    current_requirement_code TEXT NOT NULL DEFAULT '',
    current_requirement_index INTEGER NOT NULL DEFAULT 0,
    current_stage TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_jobs_document_id ON ai_analysis_jobs(sor_document_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_jobs_status ON ai_analysis_jobs(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS ai_analysis_job_targets (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
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
    CHECK(
        status NOT IN ('RUNNING', 'PUBLISHING', 'SUCCEEDED')
        OR latest_attempt_id <> ''
    ),
    CHECK(status <> 'SUCCEEDED' OR published_attempt_id <> ''),
    FOREIGN KEY(job_id) REFERENCES ai_analysis_jobs(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_job_targets_job_order
    ON ai_analysis_job_targets(job_id, source_order);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_job_targets_job_status
    ON ai_analysis_job_targets(job_id, status, source_order);

CREATE TABLE IF NOT EXISTS ai_analysis_node_attempts (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL,
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
        status <> 'SUCCEEDED'
        OR (
            result_schema_version <> ''
            AND result_payload_json <> ''
            AND length(result_sha256) = 64
        )
    ),
    CHECK(
        (analysis_input_schema_version = '' AND analysis_input_sha256 = '')
        OR (
            analysis_input_schema_version <> ''
            AND length(analysis_input_sha256) = 64
        )
    ),
    FOREIGN KEY(target_id) REFERENCES ai_analysis_job_targets(id) ON DELETE CASCADE,
    FOREIGN KEY(job_id) REFERENCES ai_analysis_jobs(id) ON DELETE CASCADE,
    FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_node_attempts_job_target
    ON ai_analysis_node_attempts(job_id, target_id, attempt_no DESC);

CREATE TABLE IF NOT EXISTS ai_analysis_events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    profile_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    requirement_id TEXT NOT NULL DEFAULT '',
    requirement_code TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES ai_analysis_jobs(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_events_job_id ON ai_analysis_events(job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_document_syncs (
    id TEXT PRIMARY KEY,
    knowledge_document_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    retrieval_backend TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    error_message TEXT NOT NULL DEFAULT '',
    synced_at TEXT NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    payload_path TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(knowledge_document_id, profile_id),
    FOREIGN KEY(knowledge_document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_knowledge_document_syncs_document_id ON knowledge_document_syncs(knowledge_document_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS review_logs (
    id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL,
    action TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    comment TEXT,
    operator TEXT NOT NULL,
    operated_at TEXT NOT NULL,
    FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_review_logs_requirement ON review_logs(requirement_id);

CREATE TABLE IF NOT EXISTS export_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    version_label TEXT,
    compare_export_id TEXT,
    snapshot_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS batch_decision_operations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL DEFAULT '',
    requirement_set_id TEXT NOT NULL DEFAULT '',
    forked_from_requirement_set_id TEXT NOT NULL DEFAULT '',
    scope_hash TEXT NOT NULL,
    final_decision TEXT NOT NULL,
    reason_template TEXT NOT NULL DEFAULT '',
    changes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'APPLIED',
    created_at TEXT NOT NULL,
    undone_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_batch_decision_operations_project
    ON batch_decision_operations(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS parsing_risk_acknowledgements (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    parse_artifact_id TEXT NOT NULL,
    parse_identity_sha256 TEXT NOT NULL,
    severe_warning_count INTEGER NOT NULL,
    acknowledged_at TEXT NOT NULL,
    actor_type TEXT NOT NULL DEFAULT 'USER',
    UNIQUE (
        sor_document_id,
        source_sha256,
        parse_artifact_id,
        parse_identity_sha256
    ),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_parsing_risk_acknowledgements_scope
    ON parsing_risk_acknowledgements (
        project_id,
        sor_document_id,
        source_sha256,
        parse_artifact_id
    );

CREATE TABLE IF NOT EXISTS document_table_assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    asset_json TEXT NOT NULL,
    preview_generation_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(sor_document_id, table_id),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_document_table_assets_document_id ON document_table_assets(sor_document_id);

CREATE TABLE IF NOT EXISTS requirement_set_table_assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    requirement_set_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    asset_json TEXT NOT NULL,
    preview_generation_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requirement_set_id, table_id),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(requirement_set_id) REFERENCES requirement_sets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_requirement_set_table_assets_set
    ON requirement_set_table_assets(requirement_set_id, table_id);
