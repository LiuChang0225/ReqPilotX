CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    knowledge_base_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sor_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    extracted_text TEXT NOT NULL,
    workflow_stage TEXT NOT NULL DEFAULT 'UPLOADED',
    requirement_set_version INTEGER NOT NULL DEFAULT 0,
    extraction_locked_at TEXT NOT NULL DEFAULT '',
    ai_status TEXT NOT NULL DEFAULT 'NOT_RUN',
    ai_assessed_at TEXT NOT NULL DEFAULT '',
    ai_requirement_set_version INTEGER NOT NULL DEFAULT 0,
    ai_knowledge_base_version INTEGER NOT NULL DEFAULT 0,
    preview_status TEXT NOT NULL DEFAULT 'NOT_READY',
    preview_generated_at TEXT NOT NULL DEFAULT '',
    preview_file_mtime TEXT NOT NULL DEFAULT '',
    preview_error TEXT NOT NULL DEFAULT '',
    preview_generation_id TEXT NOT NULL DEFAULT '',
    active_requirement_set_id TEXT NOT NULL DEFAULT '',
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    source_size BIGINT NOT NULL CHECK(source_size >= 0),
    declared_extension TEXT NOT NULL DEFAULT '',
    detected_format TEXT NOT NULL DEFAULT '',
    preflight_schema_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sor_documents_project_id
    ON sor_documents(project_id);

CREATE TABLE IF NOT EXISTS requirement_sets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK(version > 0),
    status TEXT NOT NULL DEFAULT 'DRAFT',
    is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0, 1)),
    source_hash TEXT NOT NULL CHECK(length(source_hash) = 64),
    preview_generation_id TEXT NOT NULL DEFAULT '',
    supersedes_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    activated_at TEXT NOT NULL DEFAULT '',
    locked_at TEXT NOT NULL DEFAULT '',
    lock_waiver_reason TEXT NOT NULL DEFAULT '',
    preview_status_at_lock TEXT NOT NULL DEFAULT '',
    UNIQUE(sor_document_id, version)
);

CREATE INDEX IF NOT EXISTS idx_requirement_sets_document
    ON requirement_sets(sor_document_id, version DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_requirement_sets_active_document
    ON requirement_sets(sor_document_id)
    WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    extracted_text TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    source_size BIGINT NOT NULL CHECK(source_size >= 0),
    declared_extension TEXT NOT NULL DEFAULT '',
    detected_format TEXT NOT NULL DEFAULT '',
    preflight_schema_version TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL DEFAULT 'REFERENCE',
    doc_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    effective_from TEXT NOT NULL DEFAULT '',
    effective_to TEXT NOT NULL DEFAULT '',
    retrieval_sync_status TEXT NOT NULL DEFAULT 'PENDING',
    retrieval_sync_error TEXT NOT NULL DEFAULT '',
    retrieval_synced_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_documents_project_id
    ON knowledge_documents(project_id);

CREATE TABLE IF NOT EXISTS knowledge_document_syncs (
    id TEXT PRIMARY KEY,
    knowledge_document_id TEXT NOT NULL
        REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    retrieval_backend TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    error_message TEXT NOT NULL DEFAULT '',
    synced_at TEXT NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK(chunk_count >= 0),
    payload_storage_key TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(knowledge_document_id, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_document_syncs_document_id
    ON knowledge_document_syncs(knowledge_document_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS requirements (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    requirement_set_id TEXT NOT NULL
        REFERENCES requirement_sets(id) ON DELETE CASCADE,
    req_code TEXT NOT NULL,
    source_page INTEGER,
    source_anchor TEXT,
    requirement_text TEXT NOT NULL,
    requirement_type TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_kind TEXT NOT NULL DEFAULT 'REQUIREMENT',
    text_relation TEXT NOT NULL DEFAULT 'distinct',
    is_deduped INTEGER NOT NULL DEFAULT 0 CHECK(is_deduped IN (0, 1)),
    heading_text TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    display_text TEXT NOT NULL DEFAULT '',
    merged_from_req_codes TEXT NOT NULL DEFAULT '[]',
    merged_from_entry_ids TEXT NOT NULL DEFAULT '[]',
    dedupe_version TEXT NOT NULL DEFAULT '',
    parent_section_code TEXT NOT NULL DEFAULT '',
    parent_section_title TEXT NOT NULL DEFAULT '',
    parent_section_label TEXT NOT NULL DEFAULT '',
    table_refs TEXT NOT NULL DEFAULT '[]',
    content_blocks TEXT NOT NULL DEFAULT '[]',
    has_table_asset INTEGER NOT NULL DEFAULT 0 CHECK(has_table_asset IN (0, 1)),
    table_render_mode TEXT NOT NULL DEFAULT '',
    preview_generation_id TEXT NOT NULL DEFAULT '',
    prior_requirement_id TEXT NOT NULL DEFAULT '',
    decision_reference_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requirement_set_id, req_code)
);

CREATE INDEX IF NOT EXISTS idx_requirements_project_id
    ON requirements(project_id);
CREATE INDEX IF NOT EXISTS idx_requirements_document_id
    ON requirements(sor_document_id);
CREATE INDEX IF NOT EXISTS idx_requirements_set_id
    ON requirements(requirement_set_id);

CREATE TABLE IF NOT EXISTS assessments (
    id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL UNIQUE
        REFERENCES requirements(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    retrieval_backend TEXT NOT NULL DEFAULT '',
    ai_decision TEXT NOT NULL,
    ai_reason TEXT NOT NULL,
    ai_confidence DOUBLE PRECISION NOT NULL,
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
    reference_status TEXT NOT NULL DEFAULT 'CURRENT',
    source_requirement_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidences (
    id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    source_doc_id TEXT NOT NULL,
    source_doc_name TEXT NOT NULL,
    source_doc_version TEXT NOT NULL DEFAULT '',
    source_location TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    relevance DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidences_requirement_id
    ON evidences(requirement_id);

CREATE TABLE IF NOT EXISTS assessment_runs (
    id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
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
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assessment_runs_requirement_id
    ON assessment_runs(requirement_id);

CREATE TABLE IF NOT EXISTS ai_analysis_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    requirement_set_id TEXT NOT NULL DEFAULT '',
    knowledge_base_version INTEGER NOT NULL DEFAULT 0,
    profile_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    assessment_mode TEXT NOT NULL DEFAULT '',
    retrieval_backend TEXT NOT NULL DEFAULT '',
    chat_model TEXT NOT NULL DEFAULT '',
    embedding_model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    total_requirements INTEGER NOT NULL DEFAULT 0,
    completed_requirements INTEGER NOT NULL DEFAULT 0,
    analyzed_requirements INTEGER NOT NULL DEFAULT 0,
    info_requirements INTEGER NOT NULL DEFAULT 0,
    screened_requirements INTEGER NOT NULL DEFAULT 0,
    current_requirement_id TEXT NOT NULL DEFAULT '',
    current_requirement_code TEXT NOT NULL DEFAULT '',
    current_requirement_index INTEGER NOT NULL DEFAULT 0,
    current_stage TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    runtime_job_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    _row_order BIGINT GENERATED ALWAYS AS IDENTITY
);

CREATE INDEX IF NOT EXISTS idx_ai_analysis_jobs_document_id
    ON ai_analysis_jobs(sor_document_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_jobs_status
    ON ai_analysis_jobs(status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_analysis_jobs_one_active_document
    ON ai_analysis_jobs(sor_document_id)
    WHERE status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED');
CREATE INDEX IF NOT EXISTS idx_ai_analysis_jobs_set_completion
    ON ai_analysis_jobs(requirement_set_id, status, finished_at DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_analysis_events (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES ai_analysis_jobs(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    requirement_id TEXT NOT NULL DEFAULT '',
    requirement_code TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_analysis_events_job_id
    ON ai_analysis_events(job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS review_logs (
    id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    comment TEXT,
    operator TEXT NOT NULL,
    operated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_logs_requirement
    ON review_logs(requirement_id);

CREATE TABLE IF NOT EXISTS export_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL DEFAULT '',
    requirement_set_id TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    version_label TEXT,
    compare_export_id TEXT,
    snapshot_json TEXT,
    export_mode TEXT NOT NULL DEFAULT 'DRAFT',
    source_hash TEXT NOT NULL DEFAULT '',
    knowledge_base_version INTEGER NOT NULL DEFAULT 0,
    ai_run_id TEXT NOT NULL DEFAULT '',
    human_coverage DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_export_jobs_project
    ON export_jobs(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS batch_decision_operations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL DEFAULT '',
    requirement_set_id TEXT NOT NULL DEFAULT '',
    forked_from_requirement_set_id TEXT NOT NULL DEFAULT '',
    scope_hash TEXT NOT NULL,
    final_decision TEXT NOT NULL,
    reason_template TEXT NOT NULL DEFAULT '',
    changes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'APPLIED',
    created_at TEXT NOT NULL,
    undone_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_batch_decision_operations_project
    ON batch_decision_operations(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS document_table_assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    table_id TEXT NOT NULL,
    asset_json TEXT NOT NULL,
    preview_generation_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(sor_document_id, table_id)
);

CREATE INDEX IF NOT EXISTS idx_document_table_assets_document_id
    ON document_table_assets(sor_document_id);

CREATE TABLE IF NOT EXISTS requirement_set_table_assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sor_document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    requirement_set_id TEXT NOT NULL
        REFERENCES requirement_sets(id) ON DELETE CASCADE,
    table_id TEXT NOT NULL,
    asset_json TEXT NOT NULL,
    preview_generation_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requirement_set_id, table_id)
);

CREATE INDEX IF NOT EXISTS idx_requirement_set_table_assets_set
    ON requirement_set_table_assets(requirement_set_id, table_id);

CREATE TABLE IF NOT EXISTS source_ir_snapshots (
    source_ir_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    schema_version TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('COMPLETE', 'PARTIAL')),
    created_at_utc TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_ir_snapshots_document
    ON source_ir_snapshots(document_id, created_at_utc, source_ir_id);

CREATE TABLE IF NOT EXISTS semantic_provider_runs (
    provider_run_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    response_sha256 TEXT,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_requirement_review_sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    prediction_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    status TEXT NOT NULL,
    model_prediction_sha256 TEXT NOT NULL CHECK(length(model_prediction_sha256) = 64),
    model_prediction_json TEXT NOT NULL,
    reviewed_result_sha256 TEXT NOT NULL CHECK(length(reviewed_result_sha256) = 64),
    reviewed_result_json TEXT NOT NULL,
    delta_sha256 TEXT NOT NULL CHECK(length(delta_sha256) = 64),
    delta_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(project_id, document_id, prediction_id)
);

CREATE TABLE IF NOT EXISTS llm_requirement_review_events (
    session_id TEXT NOT NULL
        REFERENCES llm_requirement_review_sessions(session_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    status TEXT NOT NULL,
    reviewer_label TEXT NOT NULL,
    reviewed_result_sha256 TEXT NOT NULL CHECK(length(reviewed_result_sha256) = 64),
    reviewed_result_json TEXT NOT NULL,
    delta_sha256 TEXT NOT NULL CHECK(length(delta_sha256) = 64),
    delta_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(session_id, revision)
);

CREATE TABLE IF NOT EXISTS llm_requirement_review_confirmation_events (
    session_id TEXT NOT NULL
        REFERENCES llm_requirement_review_sessions(session_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision > 0),
    review_node_id TEXT NOT NULL,
    confirmed INTEGER NOT NULL CHECK(confirmed IN (0, 1)),
    status TEXT NOT NULL,
    previous_reviewed_result_sha256 TEXT NOT NULL CHECK(length(previous_reviewed_result_sha256) = 64),
    reviewed_result_sha256 TEXT NOT NULL CHECK(length(reviewed_result_sha256) = 64),
    previous_event_sha256 TEXT NOT NULL CHECK(length(previous_event_sha256) = 64),
    event_sha256 TEXT NOT NULL CHECK(length(event_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(session_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_llm_requirement_review_document
    ON llm_requirement_review_sessions(project_id, document_id, updated_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_llm_requirement_confirmation_event_session
    ON llm_requirement_review_confirmation_events(session_id, revision DESC);

CREATE TABLE IF NOT EXISTS requirement_atomization_checkpoints (
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    review_node_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(lineage_sha256, review_node_id)
);

CREATE TABLE IF NOT EXISTS requirement_atomization_runs (
    atomization_run_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(project_id, document_id, session_id, review_revision, schema_version)
);

CREATE TABLE IF NOT EXISTS requirement_refinement_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    atomization_run_id TEXT NOT NULL
        REFERENCES requirement_atomization_runs(atomization_run_id) ON DELETE CASCADE,
    atomic_node_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    status TEXT NOT NULL,
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(atomization_run_id, atomic_node_id)
);

CREATE TABLE IF NOT EXISTS requirement_refinement_events (
    suggestion_id TEXT NOT NULL
        REFERENCES requirement_refinement_suggestions(suggestion_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    status TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(suggestion_id, revision)
);

CREATE TABLE IF NOT EXISTS requirement_enrichment_publication_previews (
    preview_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES sor_documents(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    atomization_run_id TEXT NOT NULL
        REFERENCES requirement_atomization_runs(atomization_run_id) ON DELETE CASCADE,
    publication_key TEXT NOT NULL UNIQUE CHECK(length(publication_key) = 64),
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    _row_order BIGINT GENERATED ALWAYS AS IDENTITY
);

CREATE TABLE IF NOT EXISTS requirement_enrichment_publications (
    publication_key TEXT PRIMARY KEY CHECK(length(publication_key) = 64),
    preview_id TEXT NOT NULL UNIQUE
        REFERENCES requirement_enrichment_publication_previews(preview_id) ON DELETE CASCADE,
    requirement_set_id TEXT NOT NULL UNIQUE
        REFERENCES requirement_sets(id) ON DELETE CASCADE,
    result_sha256 TEXT NOT NULL CHECK(length(result_sha256) = 64),
    result_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requirement_atomization_session
    ON requirement_atomization_runs(
        project_id, document_id, session_id, schema_version, review_revision DESC
    );
CREATE INDEX IF NOT EXISTS idx_requirement_refinement_run
    ON requirement_refinement_suggestions(atomization_run_id, atomic_node_id);
CREATE INDEX IF NOT EXISTS idx_requirement_publication_preview_run
    ON requirement_enrichment_publication_previews(atomization_run_id, created_at_utc DESC);
