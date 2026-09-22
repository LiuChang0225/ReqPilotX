export type JsonRecord = Record<string, unknown>;

export interface Project {
  id: string;
  name: string;
  customer_name: string;
  knowledge_base_version?: number;
  created_at?: string;
}

export interface SorDocument {
  id: string;
  project_id: string;
  file_name: string;
  file_type?: string;
  status?: string;
  source_size?: number;
  created_at?: string;
  extraction_locked?: boolean;
  requirement_count?: number;
  [key: string]: unknown;
}

export interface KnowledgeDocument {
  id: string;
  project_id?: string;
  file_name: string;
  file_type?: string;
  status?: string;
  retrieval_sync_status?: string;
  retrieval_sync_error?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface RuntimeCheck extends JsonRecord {
  status: string;
  detail?: string;
  provider?: string;
  profile_id?: string;
  required_for?: string[];
}

export interface RuntimeReadiness {
  ready: boolean;
  runtime_ready: boolean;
  status: string;
  checked_at?: string;
  deployment_mode?: string;
  database_backend?: string;
  object_store_backend?: string;
  checks: RuntimeCheck[] | Record<string, RuntimeCheck>;
  ai?: {
    default_profile_id?: string;
    default_profile_ready?: boolean;
    runnable_profiles?: string[];
    profiles?: JsonRecord[];
  };
}

export interface AiProfile extends JsonRecord {
  id: string;
  label?: string;
  provider?: string;
  default?: boolean;
  ready?: boolean;
  llm_ready?: boolean;
  retrieval_ready?: boolean;
  retrieval_backend?: string;
  runtime_status?: string;
  runtime_detail?: string;
  missing?: string[];
}

export type ReadingVisualSetupReasonCode =
  | "reading_visual_not_configured"
  | "reading_visual_configuration_invalid"
  | "reading_visual_credentials_unavailable";

export interface ReadingVisualProviderSetupV1 extends JsonRecord {
  schema_version: "reading-visual-provider-setup/v1";
  provider: "bailian-qwen3.8-max-pdf-parsing";
  model: "qwen3.8-max";
  timeout_seconds: 300;
  max_retries: 0;
  billing_basis: ["pdf_pages", "input_tokens", "output_tokens"];
  configured: boolean;
  ready: boolean;
  reason_code: ReadingVisualSetupReasonCode | null;
  message: string | null;
  connection_revision: number;
}

export interface ReadingVisualProviderSetupWriteV1 {
  schema_version: "reading-visual-provider-setup/v1";
  provider: "bailian-qwen3.8-max-pdf-parsing";
  model: "qwen3.8-max";
  timeout_seconds: 300;
  max_retries: 0;
}

export interface AiProviderSetup extends JsonRecord {
  default_profile_id?: string;
  provider_selection_required_before_knowledge_upload?: boolean;
  profiles?: AiProfile[] | Record<string, AiProfile>;
  aliyun?: JsonRecord & {
    reading_visual?: ReadingVisualProviderSetupV1;
  };
  azure?: JsonRecord;
  capabilities?: RuntimeCapability[];
  connection_revision?: number;
  aliyun_probe?: AliyunConnectionProbe | null;
  worker_fleet?: RuntimeWorkerFleetStatusV1 | null;
}

export interface AliyunConnectionProbe extends JsonRecord {
  schema_version:
    | "runtime-aliyun-connection-probe/v1"
    | "runtime-aliyun-connection-probe/v2";
  provider?: "aliyun";
  connection_revision: number;
  checked_at?: string;
  ready?: boolean;
  checks: Record<string, JsonRecord>;
}

export type CapabilityState =
  | "READY"
  | "CHECKING"
  | "NOT_CONFIGURED"
  | "AUTH_FAILED"
  | "MODEL_NOT_DEPLOYED"
  | "DEPENDENCY_UNAVAILABLE"
  | "UNREACHABLE";

export interface RuntimeCapability extends JsonRecord {
  schema_version?: "runtime-capability/v1";
  capability_id: string;
  category:
    | "document_parser"
    | "document_enhancement"
    | "complex_vision"
    | "llm"
    | "embedding"
    | "vector_store";
  provider: string;
  label: string;
  state: CapabilityState;
  ready: boolean;
  reason_code?: string;
  message?: string;
  blocked_by?: string[];
  settings_path?: string;
  connection_revision?: number;
}

export interface RuntimeCapabilityCatalog extends JsonRecord {
  schema_version: "runtime-capability-catalog/v1";
  connection_revision: number;
  capabilities: RuntimeCapability[];
}

export type RuntimeWorkerFleetState = "READY" | "SYNCING" | "UNAVAILABLE";

export interface RuntimeWorkerStatusV1 extends JsonRecord {
  slot: string | number;
  claim_profile: string;
  accepted_kinds: string[];
  connection_revision: number;
  state: string;
  fresh: boolean;
  revision_match: boolean;
  claim_profile_match: boolean;
  eligible: boolean;
  last_seen_age_seconds: number;
  source_revision_match?: boolean;
  runtime_instance_match?: boolean;
}

export interface RuntimeWorkerFleetStatusV1 extends JsonRecord {
  schema_version: "runtime-worker-fleet-status/v1";
  state: RuntimeWorkerFleetState;
  desired_connection_revision: number;
  required_kind: string;
  revision_required: boolean;
  registered_worker_count: number;
  live_worker_count: number;
  eligible_worker_count: number;
  idle_worker_count: number;
  active_worker_count: number;
  draining_worker_count: number;
  stale_revision_worker_count: number;
  workers: RuntimeWorkerStatusV1[];
}

export type ExtractionExecutionReadinessState =
  | "READY"
  | "EXECUTING"
  | "WAITING_FOR_WORKER_SYNC"
  | "BLOCKED_NO_LIVE_WORKER"
  | "CONFIGURATION_SUPERSEDED"
  | "NOT_APPLICABLE";

export interface ExtractionExecutionReadinessV1 extends JsonRecord {
  schema_version: "document-extraction-execution-readiness/v1";
  state: ExtractionExecutionReadinessState;
  revision_required: boolean;
  required_connection_revision: number | null;
  api_connection_revision: number;
  worker_fleet_state: RuntimeWorkerFleetState;
  retryable: boolean;
  recommended_action: string;
}

export interface ExtractionErrorContextV1 extends JsonRecord {
  failure_phase: string;
  pages_started: number;
  submitted_connection_revision: number | null;
  api_connection_revision: number;
  worker_connection_revisions: number[];
  recommended_action: string;
}

export type DocumentPrimaryParser =
  | "pymupdf"
  | "azure_document_intelligence";
export type DocumentEnhancer =
  | "azure_document_intelligence"
  | "bailian_layout"
  | "bailian_ocr";
export type ComplexVisionProvider =
  | "azure_gpt56_sol"
  | "bailian_vision";
export type LlmProvider = "none" | "codex" | "aliyun" | "azure_openai";
export type KnowledgeBackend =
  | "none"
  | "aliyun_qdrant"
  | "azure_ai_search";

export interface DocumentProcessingProfile extends JsonRecord {
  primary_parser: DocumentPrimaryParser;
  enhancement_chain: DocumentEnhancer[];
  complex_vision: ComplexVisionProvider | null;
  auto_escalation: boolean;
}

export interface ProjectProcessingProfile extends JsonRecord {
  schema_version: "project-processing-profile/v1";
  project_id: string;
  document: DocumentProcessingProfile;
  llm_provider: LlmProvider;
  knowledge_backend: KnowledgeBackend;
  revision: number;
  canonical_sha256: string;
}

export interface ProcessingProfilePayload extends JsonRecord {
  profile: ProjectProcessingProfile;
  capabilities?: RuntimeCapability[];
  reindex_job?: JsonRecord | null;
}

export interface RunProfileOverride extends JsonRecord {
  schema_version: "run-profile-override/v1";
  document?: DocumentProcessingProfile;
  llm_provider?: LlmProvider;
  force_visual_pages?: number[];
}

export interface WorkspaceSummary extends JsonRecord {
  requirement_count?: number;
  requirements_total?: number;
  reviewed_count?: number;
  decided_count?: number;
  evidence_count?: number;
  [key: string]: unknown;
}

export interface Requirement extends JsonRecord {
  id: string;
  requirement_id?: string;
  section_code?: string;
  clause_id?: string;
  title?: string;
  text?: string;
  requirement_text?: string;
  source_text?: string;
  page_start?: number;
  page_end?: number;
  category?: string;
  semantic_class?: string;
  decision?: string;
  manual_decision?: string;
  final_decision?: string;
  decision_reason?: string;
  ai_decision?: string;
  compliance_status?: string;
  ai_recommendation?: string;
  ai_risk?: string;
  evidence_findings?: JsonRecord[];
  structured_information_gap?: JsonRecord;
  sub_judgments?: JsonRecord[];
  analysis_status?: string;
  evidence_count?: number;
  status?: string;
}

export interface WorkspacePayload extends JsonRecord {
  project?: Project;
  document?: SorDocument;
  summary?: WorkspaceSummary;
  actions?: JsonRecord;
  readiness?: JsonRecord;
  ai_profiles?: AiProfile[];
  requirements?: Requirement[];
  review_entries?: JsonRecord[];
  preview?: JsonRecord;
  selected_requirement_set?: RequirementSet;
  read_only_reason?: string | null;
}

export interface RequirementSet extends JsonRecord {
  id?: string;
  requirement_set_id?: string;
  version?: number;
  version_label?: string;
  status?: string;
  source_kind?: string;
  created_at?: string;
  activated_at?: string;
  locked_at?: string;
  requirement_count?: number;
  decision_eligible_count?: number;
  included_requirement_count?: number;
  reference_count?: number;
  excluded_count?: number;
  human_decision_count?: number;
  requirement_tree_state?: RequirementTreeState;
  preview_generation_id?: string;
  preview_status_at_lock?: string;
}

export interface RequirementSetsPayload extends JsonRecord {
  active_requirement_set_id?: string;
  requirement_sets?: RequirementSet[];
}

export interface RequirementSetSemanticActivationRequestV1 {
  schema_version: "requirement-set-semantic-activation-request/v1";
  expected_active_requirement_set_id: string;
  expected_enrichment_publication_binding_id: string;
  expected_enrichment_publication_binding_sha256: string;
}

export interface RequirementSetSemanticActivationResponseV1 extends JsonRecord {
  project_id: string;
  document_id: string;
  active_requirement_set: RequirementSet & {
    id: string;
    enrichment_publication_binding_id: string;
    enrichment_publication_binding_sha256: string;
  };
  document: SorDocument & {
    active_requirement_set_id: string;
  };
  impact_receipt?: RequirementSetImpactReceipt | null;
}

export interface RequirementSetImpactReceipt extends JsonRecord {
  schema_version?: "requirement-set-impact-receipt/v1";
  receipt_id?: string;
  old_requirement_set_id?: string;
  new_requirement_set_id?: string;
  retained_result_node_ids?: string[];
  stale_result_node_ids?: string[];
  changed_node_ids?: string[];
  retained_result_count?: number;
  stale_result_count?: number;
}

export interface RequirementSetDeriveResponse extends JsonRecord {
  project_id: string;
  document_id: string;
  requirement_set: RequirementSet;
  created: boolean;
  impact_receipt: RequirementSetImpactReceipt | null;
  affected_result_count?: number;
}

export interface VisualTextProviderTelemetryV1 extends JsonRecord {
  page_number: number;
  provider_id: string;
  provider_version: string;
  request_count: number;
  latency_ms: number;
  cache_hit: boolean;
  outcome: "READY" | "FAILED" | "CACHE_HIT";
  updated_at: string;
}

export interface ExtractionJob extends JsonRecord {
  job_id?: string;
  status?: string;
  stage?: string;
  progress?: JsonRecord;
  partial_revision?: number;
  last_progress?: string;
  last_progress_en?: string;
  error_code?: string;
  error_stage?: string;
  error_cause_code?: string;
  execution_readiness?: ExtractionExecutionReadinessV1 | null;
  error_context?: ExtractionErrorContextV1 | null;
  processing_plan?: {
    project_profile_revision?: number;
    connection_revision?: number;
    llm_provider?: LlmProvider;
    document?: {
      primary_parser?: DocumentPrimaryParser;
      enhancement_chain?: DocumentEnhancer[];
      complex_vision?: ComplexVisionProvider | null;
    };
    provider_bindings?: {
      structure_overview_deployment?: string | null;
      structure_repair_deployment?: string | null;
      semantic_deployment?: string | null;
    };
    force_visual_pages?: number[];
  } | null;
  queue?: {
    state?:
      | "NOT_ACTIVE"
      | "WAITING_FOR_WORKER"
      | "WAITING_FOR_RUNNING_JOB"
      | "WAITING_BEHIND_JOBS"
      | "WAITING_FOR_WORKER_SYNC"
      | "WAITING_FOR_CAPACITY"
      | "BLOCKED_NO_LIVE_WORKER"
      | "BLOCKED_NO_COMPATIBLE_LANE"
      | "WORKER_NOT_CLAIMING"
      | "RETRY_SCHEDULED"
      | "CLAIMED"
      | "CANCELLING";
    position?: number | null;
    ahead_count?: number;
    running_count?: number;
    configured_capacity?: number;
    available_slots?: number;
    wait_seconds?: number;
    runnable_wait_seconds?: number;
    can_cancel?: boolean;
    recoverable?: boolean;
    retry_available_at_utc?: string;
  };
  page_staging?: ExtractionPageStaging | null;
  latest_visual_page_event?: VisualTextProviderTelemetryV1 | null;
}

export interface ExtractionPageStagingRange extends JsonRecord {
  start_page: number;
  end_page: number;
}

export interface ExtractionPageStaging extends JsonRecord {
  schema_version?: "document-extraction-page-staging-summary/v1";
  state?:
    | "UNAVAILABLE"
    | "WAITING_FOR_FIRST_PAGE"
    | "STAGED_READ_ONLY"
    | "DEGRADED"
    | "RETIRED_AFTER_FORMAL_PUBLICATION";
  job_id: string;
  attempt_generation: number;
  source_sha256?: string;
  processing_plan_sha256?: string;
  result_runtime_sha256?: string;
  total_pages: number;
  ready_page_count: number;
  ready_page_ranges: ExtractionPageStagingRange[];
  page_url_template: string;
  completed_page_count?: number;
  browsable_lag_page_count?: number;
  formal_snapshot_id?: string;
  error_code?: string;
}

export interface PreviousExtractionArtifact extends JsonRecord {
  available: boolean;
  job_id?: string;
  source_sha256?: string;
  runtime_compatible?: boolean;
  read_mode?: "READ_ONLY" | "HISTORICAL_ONLY";
  can_browse_structure?: boolean;
  can_continue_structure?: boolean;
  published_at_utc?: string;
}

export interface ExtractionItem extends JsonRecord {
  id: string;
  title?: string;
  text?: string;
  context_text?: string;
  role?: string;
  semantic_class?: string;
  requirement_subtype?: string;
  review_status?: string;
  source_order?: number;
  page_start?: number;
  page_end?: number;
  parent_id?: string;
  depth?: number;
  table_ids?: string[];
  finding_codes?: string[];
}

export interface PageProcessingReceipt extends JsonRecord {
  schema_version?: "page-processing-receipt/v1";
  page_number: number;
  page_image_sha256?: string;
  attempted_provider_ids?: string[];
  adopted_provider_id?: string;
  state: "READY" | "DEGRADED";
  degraded_reason_codes?: string[];
  table_parsing?: PageTableParsingReceiptV1;
}

export interface PageTableParsingReceiptV1 extends JsonRecord {
  mode: "OFF" | "NATIVE_TABLE_CROPS";
  candidate_count: number;
  attempt_count: number;
  ready_count: number;
  failed_count: number;
  budget_skipped_count: number;
  request_ceiling_per_page: 8;
  request_ceiling_per_document: 64;
}

export interface DocumentExtractionVisualRepairRequestV1 extends JsonRecord {
  schema_version: "document-extraction-visual-repair-request/v1";
  expected_base_job_id: string;
  expected_base_job_generation: number;
  expected_base_artifact_sha256: string;
}

export interface DocumentExtractionVisualRepairEligibilityV1
  extends JsonRecord {
  schema_version: "document-extraction-visual-repair-eligibility/v1";
  state: "AVAILABLE" | "ACTIVE" | "NOT_REQUIRED" | "UNSUPPORTED";
  reason_code: string;
  mode: "FAILED_OCR_PAGES_ONLY";
  base_job_id?: string;
  base_job_generation?: number;
  base_artifact_sha256?: string;
  required_page_count: number;
  ready_page_count: number;
  failed_page_count: number;
  failed_page_numbers: number[];
  remote_request_ceiling: number;
  deep_layout_request_ceiling: number;
}

export interface ExtractionResult extends JsonRecord {
  editable?: boolean;
  edit_reason_code?: string;
  revision?: number;
  status?: string;
  counts?: JsonRecord;
  items?: ExtractionItem[];
  review_groups?: ExtractionItem[];
  requirement_groups?: ExtractionItem[];
  findings?: JsonRecord[];
  tables?: JsonRecord[];
  page_processing_receipts?: PageProcessingReceipt[];
  degraded_page_numbers?: number[];
  structure_agent?: StructureAgent;
  requirement_set_id?: string;
  review_session_id?: string;
  session_id?: string;
}

export interface ExtractionStatus extends JsonRecord {
  project_id?: string;
  document_id?: string;
  job?: ExtractionJob | null;
  result?: ExtractionResult | null;
  partial_result?: ExtractionResult | null;
  previous_artifact?: PreviousExtractionArtifact | null;
  visual_repair?: DocumentExtractionVisualRepairEligibilityV1 | null;
  execution_readiness?: ExtractionExecutionReadinessV1 | null;
  error_context?: ExtractionErrorContextV1 | null;
}

export interface StructureNode extends JsonRecord {
  structure_node_id: string;
  parent_structure_node_id?: string | null;
  kind?: string;
  explicit_label?: string;
  title?: string;
  text?: string;
  source_order?: number;
  source_refs?: JsonRecord[];
  semantic_source_refs?: JsonRecord[];
}

export interface StructureStage extends JsonRecord {
  stage: string;
  status: string;
}

export interface StructureAgent extends JsonRecord {
  editable?: boolean;
  read_only_reason?: string;
  effective_nodes?: StructureNode[];
  effective_relations?: JsonRecord[];
  workflow?: string[];
  stages?: StructureStage[];
  current_revision?: JsonRecord | null;
}

export interface StructurePayload extends JsonRecord {
  job?: ExtractionJob;
  structure_agent?: StructureAgent;
  structure_workbench?: {
    actions?: {
      commit_url?: string;
      undo_url?: string;
      [key: string]: unknown;
    };
    readiness?: JsonRecord;
    capabilities?: JsonRecord;
    workflow?: unknown;
    requirements?: JsonRecord;
    result?: JsonRecord;
    issues?: JsonRecord[];
    recovery?: {
      reason_code?:
        | "SOURCE_CAPTURE_GAP"
        | "RESULT_REGENERATION_REQUIRED"
        | (string & {});
      detail_code?: string;
      recommended_action?:
        | "VISUAL_RESTORE_AND_REEXTRACT"
        | "REGENERATE_EXTRACTION_RESULT"
        | (string & {});
      [key: string]: unknown;
    };
    requirement_tree?: RequirementTreeSnapshot;
    source_pages?: {
      schema_version?: string;
      state?: "READY" | "UNAVAILABLE";
      snapshot_id?: string;
      source_sha256?: string;
      source_ir_id?: string;
      page_schema_version?: string;
      page_count?: number;
      geometry_index?: {
        schema_version?: string;
        state?: "READY" | "UNAVAILABLE";
        geometry_index_id?: string;
        geometry_index_sha256?: string;
        coordinate_space?: string;
        page_count?: number;
      };
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
}

export type AgentStructureIntent =
  | "GENERAL"
  | "REVIEW_PARENTAGE"
  | "REVIEW_ORDER"
  | "REVIEW_BOUNDARY"
  | "REVIEW_RELATIONS";

export interface AgentStructureOperation extends JsonRecord {
  operation: string;
  structure_node_id?: string;
  new_parent_structure_node_id?: string | null;
  new_sibling_index?: number;
  relation_id?: string;
}

export interface AgentStructureProposalPublicV1 extends JsonRecord {
  schema_version: "agent-structure-proposal-public/v1";
  proposal_record_id: string;
  proposal_revision: number;
  status: "READY" | "REJECTED" | "APPLIED" | "STALE" | "EXPIRED";
  source_sha256: string;
  facts_sha256: string;
  proposal_payload_sha256: string;
  created_at_utc: string;
  expires_at_utc: string;
  operation_count: number;
  operations: AgentStructureOperation[];
  base: {
    source_ir_id: string;
    base_structure_snapshot_id: string;
    expected_revision_number: number;
    expected_effective_snapshot_id: string;
  };
}

export interface AgentStructureProposalJobPublicV1 extends JsonRecord {
  schema_version: "agent-structure-proposal-job-public/v1";
  job_id: string;
  status:
    | "PENDING"
    | "RUNNING"
    | "CANCEL_REQUESTED"
    | "SUCCEEDED"
    | "FAILED"
    | "CANCELLED";
  generation: number;
  progress: JsonRecord;
  result: JsonRecord | null;
  error_code: string;
}

export interface AgentStructureProposalStatusV1 extends JsonRecord {
  schema_version: "agent-structure-proposal-status/v1";
  project_id: string;
  document_id: string;
  job: AgentStructureProposalJobPublicV1 | null;
  proposal: AgentStructureProposalPublicV1 | null;
  steer: AgentStructureSteerPublicV1 | null;
}

export interface AgentStructureSteerPublicV1 extends JsonRecord {
  schema_version: "agent-structure-steer-public/v1";
  steer_command_id: string;
  job_id: string;
  command_id: string;
  expected_job_generation: number;
  status: "PENDING" | "DELIVERING" | "DELIVERED" | "REJECTED";
  status_reason_code: string;
  intent: Exclude<AgentStructureIntent, "GENERAL">;
  focus_structure_node_ids: string[];
  operation_count: number;
  payload_sha256: string;
  created_at_utc: string;
  updated_at_utc: string;
}

export interface AgentStructureProposalCommandBaseV1 extends JsonRecord {
  command_id: string;
  expected_source_sha256: string;
  expected_revision_number: number;
  expected_effective_snapshot_id: string;
  intent: AgentStructureIntent;
  focus_structure_node_ids: string[];
  suggested_operations: AgentStructureOperation[];
  suggested_operations_authority:
    | "NONE"
    | "USER_REQUESTED"
    | "DETERMINISTIC_FINDING";
}

export interface AgentStructureProposalStartRequestV1
  extends AgentStructureProposalCommandBaseV1 {
  schema_version: "agent-structure-proposal-start-request/v1";
}

export interface AgentStructureProposalReviseRequestV1
  extends AgentStructureProposalCommandBaseV1 {
  schema_version: "agent-structure-proposal-revise-request/v1";
  proposal_record_id: string;
  expected_proposal_payload_sha256: string;
}

export interface AgentStructureProposalSteerRequestV1 extends JsonRecord {
  schema_version: "agent-structure-proposal-steer-request/v1";
  job_id: string;
  command_id: string;
  expected_job_generation: number;
  expected_source_sha256: string;
  expected_revision_number: number;
  expected_effective_snapshot_id: string;
  intent: Exclude<AgentStructureIntent, "GENERAL">;
  focus_structure_node_ids: string[];
  suggested_operations: AgentStructureOperation[];
  suggested_operations_authority:
    | "NONE"
    | "USER_REQUESTED"
    | "DETERMINISTIC_FINDING";
}

export interface AgentStructureProposalApplyResultV1 extends JsonRecord {
  schema_version: "agent-structure-proposal-apply-result/v1";
  proposal: AgentStructureProposalPublicV1;
  apply_receipt: {
    schema_version: "agent-structure-apply-receipt/v1";
    structure_revision_id: string;
    structure_revision_number: number;
    effective_structure_snapshot_id: string;
    appended: boolean;
  };
}

export type NativeStructureFeedbackScopeKind =
  | "DOCUMENT"
  | "DOCUMENT_UNIT"
  | "LOGICAL_TABLE"
  | "UNRESOLVED"
  | "STRUCTURE_NODE";

export interface NativeStructurePageUnitV1 extends JsonRecord {
  unit_id: string;
  title: string;
  pages: number[];
  title_authority: string;
  confidence: string;
}

export interface NativeStructureTableFragmentV1 extends JsonRecord {
  table_token: string;
  page: number;
  row_range: [number, number];
  representation?: string;
}

export interface NativeStructureLogicalTableV1 extends JsonRecord {
  scope_id: string;
  title: string;
  agent_display_label?: string;
  owner_unit_id: string;
  role: string;
  pages: number[];
  fragments: NativeStructureTableFragmentV1[];
  source_numbering_anomaly: string;
  capture_gap: string;
}

export interface NativeStructureUnresolvedV1 extends JsonRecord {
  code: string;
  scope: string;
  detail: string;
  pages: number[];
}

export interface NativeStructureTreeContentBlockV1 extends JsonRecord {
  kind: "LOGICAL_TABLE";
  logical_table_scope_id: string;
  source_order?: number;
}

export interface NativeStructureSourceAnchorV1 extends JsonRecord {
  anchor_id: string;
  page: number;
  bbox: [number, number, number, number];
  source_bounds: [number, number, number, number];
  anchor_kind: "TITLE" | "SOURCE_BLOCK" | "TABLE_REGION" | "IMAGE";
  precision:
    | "EXACT_NATIVE_SPAN"
    | "EXACT_SOURCE_BLOCK"
    | "EXACT_TABLE_REGION"
    | "EXACT_IMAGE"
    | "SHARED_BLOCK_APPROXIMATE"
    | "PAGE_ONLY";
  source_handle?: string;
  source_text_sha256?: string;
  logical_table_scope_id?: string;
  table_token?: string;
  row_range?: [number, number];
  placement_id?: string;
  asset_id?: string;
}

export interface NativeStructureSourceContentV1 extends JsonRecord {
  content_id: string;
  source_order?: number;
  page: number;
  bbox: [number, number, number, number];
  source_bounds: [number, number, number, number];
  source_handles: string[];
  text: string;
  text_sha256: string;
  display_text?: string;
  display_text_sha256?: string;
  projection_reason_codes?: string[];
  precision: "EXACT_SOURCE_RANGE" | "EXACT_NATIVE_SPAN_RANGE";
  is_title: boolean;
}

export interface NativeStructureTreeNodeV1 extends JsonRecord {
  node_id: string;
  parent_id: string | null;
  order: number;
  kind:
    | "DOCUMENT_ROOT"
    | "DOCUMENT_UNIT"
    | "FRONT_MATTER"
    | "CHAPTER"
    | "SECTION"
    | "CLAUSE";
  label: string;
  title: string;
  title_authority: string;
  pages: number[];
  content_blocks: NativeStructureTreeContentBlockV1[];
  finding_codes: string[];
  source_anchors: NativeStructureSourceAnchorV1[];
  source_content: NativeStructureSourceContentV1[];
}

export interface NativeStructureDocumentTreeV1 extends JsonRecord {
  schema_version: "native-document-structure-tree/v1";
  root_ids: string[];
  nodes: NativeStructureTreeNodeV1[];
}

export interface NativeStructureProjectionStateV1 extends JsonRecord {
  schema_version: "native-structure-projection-state/v1";
  state: "DRAFT" | "ACCEPTED" | "WORKING_VERIFICATION_REQUIRED";
  base_artifact_id: string;
  attempt_id: string | null;
  attempt_ordinal: number | null;
  affected_scope_ids: string[];
  pending_rendered_scope_ids: string[];
  validation_status: "PASS" | "FAIL";
  projection_sha256: string;
}

export interface NativeStructureWorkingRevisionHeadV1 extends JsonRecord {
  schema_version: "native-structure-working-revision-head/v1";
  run_id: string;
  handoff_id: string | null;
  source_sha256: string;
  base_artifact_id: string;
  base_projection_sha256: string;
  revision_id: string;
  revision: number;
  previous_revision_sha256: string | null;
  revision_sha256: string;
  evidence_artifact_id: string;
  d2_compiler_receipt_sha256?: string | null;
  validator_kind: "ACCEPTED_REFERENCE_REPLAY" | "D2_HOST_COMPILER";
  quality_claim: "TRANSPORT_REPLAY_ONLY" | "D2_HOST_VERIFIED";
  created_at_utc: string;
  state: "WORKING" | "ACCEPTED";
}

export interface NativeStructureWorkingRevisionHeadResponseV1
  extends JsonRecord {
  schema_version: "native-structure-working-revision-head-response/v1";
  source_sha256: string;
  head: NativeStructureWorkingRevisionHeadV1 | null;
}

export interface NativeStructureTablePreviewBindingV1 extends JsonRecord {
  schema_version: "native-structure-table-preview-binding/v1";
  scope_id: string;
  preview_sha256: string;
  object_key: string;
  object_sha256: string;
  object_size: number;
  media_type: "application/vnd.reqpilotx.native-structure-table-preview+json";
  source_sha256: string;
  base_artifact_id: string;
  base_projection_sha256: string;
}

export interface NativeStructureWorkingRevisionV1 extends JsonRecord {
  schema_version: "native-structure-working-revision/v1";
  run_id: string;
  handoff_id: string | null;
  source_sha256: string;
  base_artifact_id: string;
  base_projection_sha256: string;
  evidence_artifact_id: string;
  revision_id: string;
  revision: number;
  previous_revision_sha256: string | null;
  revision_sha256: string;
  validator_receipt_sha256: string;
  d2_compiler_receipt_sha256?: string | null;
  validator_kind: "ACCEPTED_REFERENCE_REPLAY" | "D2_HOST_COMPILER";
  validation_status: "PASS";
  quality_claim: "TRANSPORT_REPLAY_ONLY" | "D2_HOST_VERIFIED";
  created_at_utc: string;
  state: "WORKING" | "ACCEPTED";
  document_tree: NativeStructureDocumentTreeV1;
  node_states: Record<string, "WORKING_VERIFIED" | "ACCEPTED">;
  inserted_node_ids: string[];
  changed_node_ids: string[];
  retired_node_ids: string[];
  successor_node_ids: Record<string, string[]>;
  page_ownership: NativeStructurePageUnitV1[];
  logical_tables: NativeStructureLogicalTableV1[];
  table_preview_bindings: NativeStructureTablePreviewBindingV1[];
  structure_proposals: NativeStructureProposalV1[];
  unresolved: NativeStructureUnresolvedV1[];
  visual_summary: {
    opened_page_count: number;
    page_count: number;
    receipts: JsonRecord[];
  };
}

export interface NativeStructureFeedbackV1 extends JsonRecord {
  schema_version: "native-structure-feedback/v1";
  feedback_id: string;
  project_id: string;
  document_id: string;
  source_sha256: string;
  artifact_id: string;
  command_id: string;
  scope_kind: NativeStructureFeedbackScopeKind;
  scope_id: string;
  message: string;
  status: "PENDING_CODEX_APP" | "ACKNOWLEDGED" | "CONSUMED" | "REJECTED";
  created_at_utc: string;
}

export interface NativeStructureProposalDecisionV1 extends JsonRecord {
  schema_version: "native-structure-proposal-decision/v1";
  decision_id: string;
  project_id: string;
  document_id: string;
  source_sha256: string;
  artifact_id: string;
  command_id: string;
  proposal_id: string;
  object_id: string;
  node_id: string;
  action: "CONFIRM" | "UNDO" | "KEEP_UNRESOLVED";
  created_at_utc: string;
}

export interface NativeStructureProposalV1 extends JsonRecord {
  proposal_id: string;
  finding_code: string;
  node_id: string;
  object_kind: string;
  object_id: string;
  page: number;
  bbox: [number, number, number, number];
  source_bounds: [number, number, number, number];
  observation: string;
  proposal_title: string;
  recommended_action: string;
  recommendation: string;
  recommended_target_node_id: string;
  impact: string;
  confidence: string;
  default_policy: string;
  status:
    | "AUTO_APPLIED_PENDING_REVIEW"
    | "PROPOSED_CONTINUABLE"
    | "UNRESOLVED_CONTINUABLE"
    | "CONFIRMED"
    | "UNDONE"
    | "KEPT_UNRESOLVED";
  overlap_ratio: number;
  decision: NativeStructureProposalDecisionV1 | null;
}

export interface NativeStructureReferencePublicV1 extends JsonRecord {
  source_findings?: SourceFindingsV1;
  scope_summaries?: Record<string, SourceScopeSummaryV1>;
  schema_version: "native-structure-reference-public/v1";
  availability: "READY";
  artifact_id: string;
  source_sha256: string;
  reading_projections?: Record<string, SourceTextReadingProjectionV1>;
  artifact_status: string;
  coverage_status: string;
  boundary_status: string;
  workflow_step: "DOCUMENT_STRUCTURE_RECOVERY";
  summary: {
    primary_document_units: number;
    main_document_top_level_sections: number;
    top_level_logical_tables: number;
    nested_physical_tables: number;
    nested_visual_tables: number;
    opened_high_resolution_pages: number;
    count_is_quality_gate: boolean;
  };
  page_ownership: NativeStructurePageUnitV1[];
  main_document_sections: Array<{
    label: string;
    title: string;
    pages: [number, number];
  }>;
  document_tree: NativeStructureDocumentTreeV1;
  structure_projection: NativeStructureProjectionStateV1;
  working_revision_head?: NativeStructureWorkingRevisionHeadV1 | null;
  structure_proposals: NativeStructureProposalV1[];
  logical_tables: NativeStructureLogicalTableV1[];
  physical_table_dispositions: JsonRecord[];
  unresolved: NativeStructureUnresolvedV1[];
  implementation_findings: JsonRecord[];
  visual_summary: {
    opened_page_count: number;
    page_count: number;
    receipts: JsonRecord[];
  };
  repeatability: {
    status: string;
    stable_semantics: JsonRecord;
    refinements_applied: JsonRecord[];
    unresolved_not_settled_by_replay: string[];
    quality_claim: string;
  };
  validation: {
    status: string;
    checks: JsonRecord;
    quality_claim: string;
  };
  oracle_diagnostic: {
    authority: string;
    oracle_expected_region_count: number;
    native_region_disposition_count: number;
    count_is_correctness_signal: boolean;
    native_boundary_corrections: JsonRecord[];
  };
  activity: JsonRecord[];
  feedbacks: NativeStructureFeedbackV1[];
  continuation: NativeStructureContinuationV1;
  feedback_capability: {
    status: "QUEUE_READY";
    agent_delivery: NativeStructureContinuationV1["status"];
    message: string;
  };
}

export interface NativeStructureFeedbackRequestV1 extends JsonRecord {
  schema_version: "native-structure-feedback-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_artifact_id: string;
  scope_kind: NativeStructureFeedbackScopeKind;
  scope_id: string;
  message: string;
}

export interface NativeStructureFeedbackResultV1 extends JsonRecord {
  schema_version: "native-structure-feedback-result/v1";
  feedback: NativeStructureFeedbackV1;
  delivery_message: string;
}

export interface NativeStructureProposalDecisionRequestV1 extends JsonRecord {
  schema_version: "native-structure-proposal-decision-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_artifact_id: string;
  proposal_id: string;
  action: "CONFIRM" | "UNDO" | "KEEP_UNRESOLVED";
}

export interface NativeStructureProposalDecisionResultV1 extends JsonRecord {
  schema_version: "native-structure-proposal-decision-result/v1";
  decision: NativeStructureProposalDecisionV1;
  proposal_status: NativeStructureProposalV1["status"];
}

export interface NativeStructureAgentHandoffV1 extends JsonRecord {
  schema_version: "native-structure-agent-handoff/v1";
  handoff_id: string;
  project_id: string;
  document_id: string;
  source_sha256: string;
  artifact_id: string;
  command_id: string;
  input_fingerprint: string;
  status:
    | "QUEUED_FOR_CODEX_APP"
    | "ACKNOWLEDGED"
    | "IN_PROGRESS"
    | "VERIFYING"
    | "REVISION_REQUIRED"
    | "NO_CHANGE"
    | "NEEDS_GUIDANCE"
    | "UNRESOLVED_CONTINUED"
    | "CONSUMED"
    | "FAILED";
  feedback_ids: string[];
  proposal_decision_ids: string[];
  affected_node_ids: string[];
  decision_counts: {
    CONFIRM: number;
    UNDO: number;
    KEEP_UNRESOLVED: number;
  };
  requires_scoped_structure_revision: boolean;
  requires_scoped_evidence_recovery: boolean;
  preserve_unaffected_nodes: boolean;
  current_action?: string;
  outcome_reason_code?: string;
  evidence_state?: "SUFFICIENT" | "CONFLICTING" | "INSUFFICIENT";
  decision_policy?:
    | "FIRST_HOST_VALIDATED_PROPOSAL"
    | "KEEP_CURRENT_STRUCTURE";
  default_resolution_policy?:
    | "NONE"
    | "FIRST_HOST_VALIDATED_PROPOSAL"
    | "KEEP_CURRENT_STRUCTURE";
  nonblocking_unresolved_count?: number;
  acknowledged_at_utc?: string;
  latest_attempt?: NativeStructureHandoffAttemptPublicV1;
  attempt_summary?: {
    attempt_count: number;
    tool_call_count: number;
    candidate_revision_count: number;
    tool_intents: string[];
    validation_status: "PASS" | "FAIL";
  };
  created_at_utc: string;
}

export interface NativeStructureHandoffAttemptPublicV1 extends JsonRecord {
  schema_version: "native-structure-handoff-attempt-receipt/v1";
  attempt_id: string;
  attempt_ordinal: number;
  status: "VERIFIED" | "VERIFICATION_REQUIRED" | "REVISION_REQUIRED";
  affected_scope_ids: string[];
  tool_calls: Array<{
    call_id: string;
    intent: string;
    outcome: "SUCCEEDED" | "FAILED" | "UNAVAILABLE";
    result_role:
      | "SOURCE_EVIDENCE"
      | "ADVISORY_CANDIDATE"
      | "MATERIALIZED_OUTPUT"
      | "VALIDATION_REPORT"
      | null;
  }>;
  candidate_revision_count: number;
  rendered_output_sha256: string;
  validation_status: "PASS" | "FAIL";
  selected_pattern_ids: string[];
  accepted_work_item_count: number;
  default_applied_work_item_count?: number;
  evidence_state?: "SUFFICIENT" | "CONFLICTING" | "INSUFFICIENT";
  decision_policy?:
    | "FIRST_HOST_VALIDATED_PROPOSAL"
    | "KEEP_CURRENT_STRUCTURE";
  rendered_reopened_scope_ids?: string[];
  pending_rendered_scope_ids?: string[];
  current_action: string;
  public_summary: string;
  kit_version: string;
  kit_bundle_sha256: string;
  created_at_utc: string;
}

export interface NativeStructureRenderedReopenRequestV1 extends JsonRecord {
  schema_version: "native-structure-rendered-reopen-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_artifact_id: string;
  handoff_id: string;
  attempt_id: string;
  scope_id: string;
  expected_projection_sha256: string;
  expected_rendered_output_sha256: string;
}

export interface NativeStructureRenderedReopenResultV1 extends JsonRecord {
  schema_version: "native-structure-rendered-reopen-result/v1";
  reopen_receipt_sha256: string;
  attempt: NativeStructureHandoffAttemptPublicV1;
  next_action: string;
}

export interface NativeStructureContinuationV1 extends JsonRecord {
  status:
    | "IDLE"
    | "READY_TO_CONTINUE"
    | "QUEUED_FOR_CODEX_APP"
    | "ACKNOWLEDGED"
    | "IN_PROGRESS"
    | "VERIFYING"
    | "REVISION_REQUIRED"
    | "NO_CHANGE"
    | "NEEDS_GUIDANCE"
    | "UNRESOLVED_CONTINUED"
    | "CONSUMED";
  can_continue: boolean;
  pending_feedback_count: number;
  pending_proposal_decision_count: number;
  confirmed_proposal_count: number;
  undone_proposal_count: number;
  kept_unresolved_proposal_count: number;
  review_pending_proposal_count: number;
  completed_work_item_count?: number;
  total_work_item_count?: number;
  advancement_allowed?: boolean;
  default_resolution_policy?:
    | "NONE"
    | "FIRST_HOST_VALIDATED_PROPOSAL"
    | "KEEP_CURRENT_STRUCTURE";
  nonblocking_unresolved_count?: number;
  evidence_state?: "SUFFICIENT" | "CONFLICTING" | "INSUFFICIENT";
  latest_handoff: NativeStructureAgentHandoffV1 | null;
  message: string;
}

export interface NativeStructureContinueRequestV1 extends JsonRecord {
  schema_version: "native-structure-continue-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_artifact_id: string;
}

export interface NativeStructureContinueResultV1 extends JsonRecord {
  schema_version: "native-structure-continue-result/v1";
  handoff: NativeStructureAgentHandoffV1;
  job: {
    job_id: string;
    status: string;
    generation: number;
    progress: JsonRecord;
    error_code: string;
  } | null;
  delivery_message: string;
}

export interface NativeStructureBaselineStartRequestV1 extends JsonRecord {
  schema_version: "native-structure-baseline-start-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_artifact_id: string;
}

export interface NativeStructureAcceptedBaselineV1 extends JsonRecord {
  schema_version: "native-structure-accepted-baseline/v1";
  baseline_id: string;
  status: "ACCEPTED";
  source_sha256: string;
  native_artifact_id: string;
  native_projection_sha256: string;
  accepted_structure_artifact_id: string;
  accepted_structure_artifact_sha256: string;
  d3_stage_input_manifest_id: string;
  d3_stage_input_manifest_sha256: string;
  d3_source_scope_sha256: string;
  evidence_state: "SUFFICIENT" | "CONFLICTING" | "INSUFFICIENT";
  decision_policy:
    | "NONE"
    | "FIRST_HOST_VALIDATED_PROPOSAL"
    | "KEEP_CURRENT_STRUCTURE";
  logical_object_count: number;
  exact_source_segment_count: number;
  physical_table_count: number;
  table_region_count: number;
  baseline_sha256: string;
}

export interface NativeStructureBaselineJobPublicV1 extends JsonRecord {
  schema_version: "native-structure-baseline-job-public/v1";
  job_id: string;
  status: string;
  generation: number;
  progress: JsonRecord;
  error_code: string;
  baseline: NativeStructureAcceptedBaselineV1 | null;
  next_action: string;
}

export interface NativeStructureBaselineStatusV1 extends JsonRecord {
  schema_version: "native-structure-baseline-status/v1";
  status: string;
  baseline_ready: boolean;
  job: NativeStructureBaselineJobPublicV1 | null;
  message: string;
}

export interface NativeParentProvisionJobPublicV1 extends JsonRecord {
  schema_version: "native-parent-provision-job-public/v1";
  job_id: string;
  status: string;
  generation: number;
  progress: JsonRecord;
  error_code: string;
  execution_strategy: string;
  source_window_count: number;
  artifact_id: string;
  artifact_sha256: string;
  parent_provision_count: number;
  declared_scope_count: number;
  requirementness_counts: Record<string, number>;
  professional_policy_attested: boolean;
  next_action: string;
}

export interface NativeParentProvisionStatusV1 extends JsonRecord {
  schema_version: "native-parent-provision-status/v1";
  status: string;
  result_ready: boolean;
  job: NativeParentProvisionJobPublicV1 | null;
  message: string;
}

export interface NativeParentProvisionSourceRangeV1 extends JsonRecord {
  segment_id: string;
  start_offset: number;
  end_offset: number;
  verbatim_text: string;
  verbatim_text_sha256: string;
  page: number;
  bbox: [number, number, number, number] | null;
  source_bounds: [number, number, number, number] | null;
  precision: "EXACT_TABLE_CELL" | "EXACT_SOURCE_BLOCK" | "SOURCE_NODE_MISSING";
}

export interface NativeParentProvisionEntryV1 extends JsonRecord {
  parent_provision_id: string;
  source_order: number;
  logical_object_ids: string[];
  explicit_label: string | null;
  ancestor_labels: string[];
  requirementness: "REQUIREMENT" | "NON_REQUIREMENT";
  requirement_kind:
    | "FUNCTIONAL"
    | "QUALITY"
    | "CONSTRAINT"
    | "OTHER_REQUIREMENT"
    | null;
  technicality: "TECHNICAL" | "NON_TECHNICAL" | "MIXED" | null;
  content_role: string;
  confidence: number;
  reason_codes: string[];
  source_ranges: NativeParentProvisionSourceRangeV1[];
  verbatim_text: string;
}

export interface NativeParentProvisionBaselinePublicV1 extends JsonRecord {
  schema_version: "native-parent-provision-baseline-public/v1";
  source_sha256: string;
  baseline_id: string;
  baseline_sha256: string;
  artifact_id: string;
  artifact_sha256: string;
  status: "COMPLETE";
  professional_policy_attested: boolean;
  summary: {
    parent_provision_count: number;
    declared_scope_count: number;
    requirementness_counts: Record<string, number>;
  };
  entries: NativeParentProvisionEntryV1[];
}

export type SourceRequirementnessV1 = "REQUIREMENT" | "NON_REQUIREMENT";
export type SourceRequirementKindV1 = "FUNCTIONAL" | "QUALITY" | "CONSTRAINT" | "PROJECT" | "PROCESS";
export type SourceNonRequirementRoleV1 =
  | "HEADING"
  | "METADATA"
  | "REVISION_HISTORY"
  | "CONTEXT"
  | "DEFINITION"
  | "RATIONALE"
  | "EXAMPLE"
  | "NOTE"
  | "REFERENCE";

export interface SourceRequirementBaselineStatusV1 extends JsonRecord {
  schema_version: "source-requirement-baseline-status/v1";
  status: "NOT_READY" | "SLICE_COMPLETE" | "COMPLETE";
  baseline_ready: boolean;
  document_map_ready: boolean;
  source_sha256: string;
  document_map_artifact_id?: string;
  document_map_artifact_sha256?: string;
  baseline_artifact_id?: string;
  baseline_artifact_sha256?: string;
  covered_scope_ids?: string[];
  record_count?: number;
  requirement_count?: number;
  non_requirement_count?: number;
  authority?: string;
  legacy_parent_provision_result_role?: "DIAGNOSTIC_ONLY";
  scope_revision?: SourceItemScopeRevisionProjectionV1;
  message: string;
}

export type SourceRequirementBaselineRunJobStatusV1 =
  | "PENDING"
  | "RUNNING"
  | "CANCEL_REQUESTED"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export interface SourceRequirementBaselineRunJobPublicV1 extends JsonRecord {
  schema_version: "source-requirement-baseline-job-public/v1";
  job_id: string;
  status: SourceRequirementBaselineRunJobStatusV1;
  generation: number;
  scope_id: string;
  scope_ids?: string[];
  progress: {
    stage: string;
    message: string;
    strategy: string;
    completed_window_count: number;
    total_window_count: number;
  };
  result: {
    status: string;
    record_count: number;
    context_row_group_count: number;
    candidate_sha256: string;
    next_action: string;
    baseline_revision: number;
    baseline_record_count: number;
    revision_summary: {
      operation: string;
      matched_count: number;
      created_count: number;
      tombstoned_count: number;
    };
  } | null;
  error_code: string;
}

export interface SourceRequirementBaselineRunStatusV1 extends JsonRecord {
  schema_version: "source-requirement-baseline-run-status/v1";
  status:
    | "IDLE"
    | "UNAVAILABLE"
    | SourceRequirementBaselineRunJobStatusV1;
  result_ready: boolean;
  job: SourceRequirementBaselineRunJobPublicV1 | null;
  message: string;
}

export interface SourceRequirementBaselineRunStartRequestV1 extends JsonRecord {
  schema_version: "source-requirement-baseline-start-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_artifact_id: string;
  logical_table_scope_id: string;
}

export interface SourceItemTextBaselineRunStartRequestV1 extends JsonRecord {
  schema_version: "source-item-text-baseline-start-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_artifact_id: string;
  node_ids: string[];
}

export interface SourceItemScopeRevisionRunStartRequestV1 extends JsonRecord {
  schema_version: "source-item-scope-revision-start-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_document_map_artifact_sha256: string;
  expected_baseline_artifact_id: string;
  expected_baseline_artifact_sha256: string;
  expected_baseline_revision: number;
  affected_scope_ids: string[];
  comparison_scope_ids: string[];
  feedback_ids: string[];
}

export interface SourceItemScopeUndoRunStartRequestV1 extends JsonRecord {
  schema_version: "source-item-scope-undo-start-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_document_map_artifact_sha256: string;
  expected_baseline_artifact_id: string;
  expected_baseline_artifact_sha256: string;
  expected_baseline_revision: number;
  expected_scope_revision_receipt_sha256: string;
}

export interface SourceItemScopeRevisionProjectionV1 extends JsonRecord {
  available: boolean;
  can_undo: boolean;
  operation: "" | "REPLACE" | "UNDO";
  receipt_sha256: string;
  affected_scope_ids: string[];
  comparison_scope_ids: string[];
  feedback_ids: string[];
  guidance_count: number;
}

export interface SourceRequirementOutlineNodeV1 extends JsonRecord {
  node_id: string;
  parent_id: string | null;
  order: number;
  kind: string;
  label: string;
  title: string;
  pages: number[];
  record_count: number;
  requirement_count: number;
  non_requirement_count: number;
  scope_status: "READY" | "NOT_IN_CURRENT_SLICE";
}

export interface SourceNumberFieldsV1 {
  source_number?: string;
  source_numbers?: Array<string | ({ value: string } & JsonRecord)>;
  source_number_role?: string;
  source_number_projection?: JsonRecord | null;
}
export interface SourceScopeSummaryV1 extends JsonRecord {
  direct_source_item_count: number;
  subtree_source_item_count: number;
  subtree_requirement_count: number;
  subtree_non_requirement_count: number;
  derived_count: number;
  analysis_unit_count: number;
  coverage?: unknown;
}
export interface SourceFindingV1 extends JsonRecord {
  finding_id?: string;
  kind: string;
  status?: string;
  summary?: string;
  observed?: unknown;
  endpoints?: unknown;
  source_refs?: unknown;
}
export interface SourceFindingsV1 extends JsonRecord {
  scan_state: string;
  checked_scope?: unknown;
  findings: SourceFindingV1[];
}

export interface SourceItemOutlineV1 extends JsonRecord, SourceNumberFieldsV1 {
  source_item_id: string;
  source_record_id: string;
  source_display_id: string;
  requirement_display_id: string | null;
  source_order: number;
  node_id: string;
  ancestor_path: string[];
  logical_table_scope_id: string;
  label: string;
  display_label: string;
  source_form:
    | "NUMBERED_CLAUSE"
    | "NATIVE_LIST"
    | "PARAGRAPH"
    | "TABLE_RECORD"
    | "TABLE_BRANCH"
    | "IMAGE";
  pages: number[];
  requirementness: SourceRequirementnessV1;
  requirement_kinds: SourceRequirementKindV1[];
  non_requirement_role: SourceNonRequirementRoleV1 | null;
  confidence: number;
  evidence_state: "SUFFICIENT" | "CONFLICTING" | "INSUFFICIENT" | "BLOCKED_INPUT";
  decision_summary: string;
  decision_origin: "AGENT_DEFAULT" | "USER_OVERRIDE";
  review_state: "UNREVIEWED" | "REVIEWED";
  feedback_count: number;
}

/** Compatibility name while the v1 endpoint retains its historical URL. */
export type SourceRequirementOutlineRecordV1 = SourceItemOutlineV1;

export interface SourceRequirementActivityV1 extends JsonRecord {
  sequence: number;
  scope_id: string;
  state: "COMPLETED";
  title: string;
  summary: string;
  tool_names: string[];
  elapsed_milliseconds: number;
}

export interface SourceItemLedgerRowV1 extends JsonRecord {
  row_id: string;
  parent_row_id: string | null;
  child_row_ids: string[];
  row_role: "DOCUMENT_STRUCTURE" | "SOURCE_ITEM";
  structure_node_id: string;
  source_item_id: string | null;
  logical_table_scope_ids: string[];
  is_table_structure: boolean;
  is_one_to_one_item: boolean;
  processing_state: "STRUCTURE_ONLY" | "ITEMS_READY" | "CLASSIFIED";
  direct_source_item_count: number;
  requirement_count: number;
  non_requirement_count: number;
}

export interface SourceItemLedgerV1 extends JsonRecord {
  schema_version: "source-item-ledger/v1";
  document_map_artifact_id: string;
  document_map_artifact_sha256: string;
  baseline_artifact_id: string;
  baseline_artifact_sha256: string;
  root_row_ids: string[];
  rows: SourceItemLedgerRowV1[];
  ledger_sha256: string;
  interchange: {
    target: "REQIF_1_2";
    mapping_profile: string;
    mapping_status: "MODEL_READY_EXPORT_NOT_IMPLEMENTED";
    hierarchy_mapping: "SPEC_HIERARCHY";
    item_mapping: "SPEC_OBJECT";
  };
}

export interface SourceRequirementBaselineOutlineV1 extends JsonRecord {
  scope_summaries?: Record<string, SourceScopeSummaryV1>;
  source_findings?: SourceFindingsV1;
  schema_version: "source-requirement-baseline-outline/v1";
  source_sha256: string;
  document_map: {
    artifact_id: string;
    artifact_sha256: string;
    revision: number;
    status: "ACCEPTED";
    summary: string;
    root_node_ids: string[];
    nodes: SourceRequirementOutlineNodeV1[];
  };
  baseline: {
    artifact_id: string;
    artifact_sha256: string;
    revision: number;
    status: "SLICE_COMPLETE" | "COMPLETE";
    covered_scope_ids: string[];
    authority: string;
  };
  summary: {
    record_count: number;
    source_item_count: number;
    requirement_count: number;
    non_requirement_count: number;
    feedback_count: number;
  };
  records: SourceRequirementOutlineRecordV1[];
  ledger: SourceItemLedgerV1;
  activity: SourceRequirementActivityV1[];
  scope_revision?: SourceItemScopeRevisionProjectionV1;
}

export interface SourceRequirementTableFragmentBindingV1 extends JsonRecord {
  fragment_id: string;
  table_token: string;
  page: number;
  row_start: number;
  row_end: number;
  cell_ids: string[];
  bbox: [number, number, number, number];
  source_bounds: [number, number, number, number];
}

export interface SourceTextReadingProjectionV1 extends JsonRecord {
  display_text: string;
  display_text_sha256: string;
  raw_text_sha256: string;
  source_sha256: string;
  projection_sha256: string;
  duplicate_ranges: unknown[];
  remaining_partial_overlaps?: unknown[];
}

export interface SourceItemTextBindingV1 extends JsonRecord {
  content_id: string;
  page: number;
  start_offset: number;
  end_offset: number;
  text: string;
  text_sha256: string;
  bbox: [number, number, number, number];
  source_bounds: [number, number, number, number];
  precision: string;
  reading_projection?: SourceTextReadingProjectionV1;
}

export interface SourceItemImageBindingV1 extends JsonRecord {
  anchor_id: string;
  source_sha256: string;
  page: number;
  bbox: [number, number, number, number];
  source_bounds: [number, number, number, number];
  asset_id: string;
  precision: string;
}

export interface SourceRequirementRecordDetailV1 extends JsonRecord {
  schema_version: "source-requirement-record-detail/v1";
  source_sha256: string;
  document_map_artifact_id: string;
  document_map_artifact_sha256: string;
  baseline_artifact_id: string;
  baseline_artifact_sha256: string;
  record: SourceRequirementOutlineRecordV1 & {
    document_map_artifact_id: string;
    document_map_artifact_sha256: string;
    table_fragments: SourceRequirementTableFragmentBindingV1[];
    text_bindings: SourceItemTextBindingV1[];
    image_bindings?: SourceItemImageBindingV1[];
    related_images?: (SourceItemImageBindingV1 & {
      relation: "IMAGE_WITHIN_OWNED_TABLE_CELLS";
      source_cell_ids: string[];
      table_fragment_id: string;
    })[];
    visual_interpretation?: {
      text: string;
      authority: "AGENT_VISUAL_INTERPRETATION";
      source_sha256: string;
      image_anchor_ids: string[];
      review_state: "UNREVIEWED";
    };
    shared_header_cell_ids: string[];
    shared_table_cell_ids: string[];
    shared_source_content_ids: string[];
    shared_source_content: NativeStructureSourceContentV1[];
    context_source_items?: Array<{ source_item_id: string; source_display_id: string;
      label: string; pages: number[]; text_bindings: SourceItemTextBindingV1[] }>;
    internal_diagnostics_available: boolean;
    feedbacks: Array<{
      feedback_id: string;
      message: string;
      status: "PENDING_AGENT" | "ACKNOWLEDGED" | "CONSUMED";
      created_at_utc: string;
      origin_revision_id?: string;
      resolution?: { revision_id: string; reason: string } | null;
    }>;
  };
}

export interface SourceRequirementFeedbackRequestV1 extends JsonRecord {
  schema_version: "source-requirement-feedback-request/v1";
  command_id: string;
  expected_source_sha256: string;
  expected_document_map_artifact_sha256: string;
  expected_baseline_artifact_id: string;
  source_record_id: string;
  message: string;
}

export interface SourceRequirementFeedbackResultV1 extends JsonRecord {
  schema_version: "source-requirement-feedback-result/v1";
  feedback: JsonRecord;
  delivery_message: string;
}

export interface NativeStructureActivityEventV2 extends JsonRecord {
  schema_version: "reqpilotx-agent-activity-event/v2";
  event_id: string;
  run_id: string;
  source_sha256: string;
  sequence: number;
  occurred_at_utc: string;
  actor: "AGENT" | "HOST" | "TOOL" | "VALIDATOR" | "USER";
  family: string;
  kind: string;
  stage: "D2_LOGICAL_STRUCTURE";
  scope: {
    kind: string;
    ref_id: string;
    page_numbers: number[];
  } | null;
  visibility: "PRIMARY" | "DETAIL" | "AUDIT";
  display: {
    title: string;
    summary: string;
  };
  payload: {
    status: string;
    outcome_code: string;
    authorized_parameters: JsonRecord;
  };
}

export interface NativeStructureActivityStreamV1 extends JsonRecord {
  schema_version: "native-structure-activity-stream/v1";
  source_sha256: string;
  artifact_id: string;
  handoff_id: string | null;
  events: NativeStructureActivityEventV2[];
  cursor: number;
  continuation: NativeStructureContinuationV1;
  working_revision_head?: NativeStructureWorkingRevisionHeadV1 | null;
}

export interface NativeStructureTablePreviewCellV1 extends JsonRecord {
  cell_id: string;
  row: number;
  column: number;
  row_span: number;
  column_span: number;
  text: string;
  bbox: [number, number, number, number];
}

export interface NativeStructureTablePreviewFragmentV1 extends JsonRecord {
  fragment_id: string;
  table_token: string;
  page: number;
  row_range: [number, number];
  row_count: number;
  column_count: number;
  bbox: [number, number, number, number];
  source_bounds: [number, number, number, number];
  cells: NativeStructureTablePreviewCellV1[];
  source_header_regions?: Array<{
    row: number;
    column: number;
    row_span: number;
    column_span: number;
  }>;
  source_notes?: string[];
}

export interface NativeStructureCanonicalColumnProjectionV1 extends JsonRecord {
  schema_version: "native-structure-canonical-column-projection/v1";
  status: "EXACT";
  canonical_column_count: number;
  boundary_ratios: number[];
  fragments: Array<{
    fragment_id: string;
    cell_mappings: Array<{
      cell_id: string;
      column: number;
      column_span: number;
    }>;
  }>;
  projection_sha256: string;
}

export interface NativeStructureTablePreviewV1 extends JsonRecord {
  schema_version: "native-structure-table-preview/v1";
  artifact_id: string;
  source_sha256: string;
  logical_table_scope_id: string;
  title: string;
  fragments: NativeStructureTablePreviewFragmentV1[];
  evidence_recovery?: {
    schema_version: "pdf-visual-table-recovery-receipt/v1";
    status: "RECOVERED";
    method: "NATIVE_WORDS_WITH_VECTOR_GRID";
    source_sha256: string;
    logical_table_scope_id: string;
    pages: number[];
    fragment_sha256s: string[];
    receipt_sha256: string;
  };
  presentation?: {
    display_title?: string;
    authority?: "AGENT_DISPLAY_LABEL";
    map_title?: string;
    schema_version: "native-structure-table-presentation/v1";
    mode: "STRUCTURED_CELLS" | "VISUAL_REGION_ONLY";
    canonical_column_count: number | null;
    canonical_projection?: NativeStructureCanonicalColumnProjectionV1 | null;
    floating_images: NativeStructureSourceAnchorV1[];
  };
  summary: {
    fragment_count: number;
    row_count: number;
    cell_count: number;
  };
}

export type RequirementTreeMode = "WORKING" | "REQUIREMENT_SET";
export type RequirementTreeState =
  | "NOT_STARTED"
  | "RUNNING_PARTIAL"
  | "READY"
  | "FAILED"
  | "REQUIRES_REEXTRACTION";
export type RequirementTreeProjectionRole =
  | "STRUCTURE"
  | "STRUCTURE_PROXY"
  | "DIRECT_CONTENT"
  | "WHOLE_TABLE_REVIEW"
  | "TABLE_RECORD"
  | "REQUIREMENT";
export type RequirementTreeNodeKind =
  | "DOCUMENT"
  | "SECTION"
  | "CLAUSE"
  | "GROUP"
  | "LIST_GROUP"
  | "TABLE"
  | "TABLE_RECORD"
  | "REQUIREMENT";

export interface RequirementTreeReadingBlock extends JsonRecord {
  /** Stable identity for one source-bound reading event. */
  reading_event_id?: string;
  /** Stable physical reading order. Optional only for pre-contract snapshots. */
  source_order?: number;
  /** Exact offset inside one SourceIR fact for split-span ordering. */
  source_offset?: number;
  /** Figure/image caption bound by the structure read model. */
  caption?: string;
  /** Exact source facts that supplied the bound caption. */
  caption_source_refs?: JsonRecord[];
  /** Caption's own physical position; it may differ from the media position. */
  caption_source_order?: number;
  caption_source_offset?: number;
}

export interface RequirementTreeTocEntry extends JsonRecord {
  entry_id?: string;
  number: string;
  title: string;
  printed_page_label?: string;
  target_page: number | null;
  source_order: number;
  source_refs: JsonRecord[];
  target_structure_node_id?: string | null;
}

export interface RequirementTreeBilingualSourceSpan extends JsonRecord {
  bilingual_span_id: string;
  language: "en" | "zh-CN";
  source_ref: JsonRecord;
  owner_structure_node_id: string;
  start_offset: number;
  end_offset: number;
  boundary_basis: "DOCX_RUN_BOUNDARY" | "SOURCE_BLOCK_BOUNDARY";
}

export interface RequirementTreeBilingualContext extends JsonRecord {
  counterpart_group_id: string;
  document_language_policy_id: string | null;
  canonical_structure_node_id: string;
  member_structure_node_ids: string[];
  authority_language: "en" | "zh-CN" | "UNKNOWN";
  authority_basis: "DECLARED_DOCUMENT_POLICY" | "UNKNOWN";
  equal_validity: boolean | null;
  source_spans: RequirementTreeBilingualSourceSpan[];
  mismatch_signal_ids: string[];
  mismatch_review_required: boolean;
}

export interface RequirementTreeNode extends JsonRecord {
  id: string;
  parent_id: string | null;
  kind: RequirementTreeNodeKind | (string & {});
  depth: number;
  source_order: number;
  source_offset?: number;
  label: string;
  summary: string;
  child_ids: string[];
  structure_node_id: string | null;
  requirement_entry_id: string | null;
  workflow_node_id: string | null;
  /**
   * Concrete Effective Structure nodes represented by this projected row.
   * Optional only while immutable pre-contract snapshots remain readable.
   */
  editable_structure_node_ids?: string[];
  page_start: number | null;
  page_end: number | null;
  source_refs: JsonRecord[];
  requirement_count: number;
  attention_count: number;
  reviewed_count: number;
  status: string;
  classification: string;
  /** Optional only while immutable pre-contract Requirement Set snapshots remain readable. */
  projection_role?: RequirementTreeProjectionRole;
  semantic_state?: string;
  deterministic_rule_evidence?: JsonRecord[];
  table_record_id?: string | null;
  table_evidence_binding_id?: string | null;
  requirement_evidence_binding_id?: string | null;
  review_item_count?: number;
  include_in_view: boolean;
  content_blocks: RequirementTreeReadingBlock[];
  table_refs: JsonRecord[];
  figure_refs: RequirementTreeReadingBlock[];
  /** Exact two-language source proof for one logical review candidate. */
  bilingual_context?: RequirementTreeBilingualContext;
  /** Dedicated navigation projection for a table-of-contents node. */
  toc_entries?: RequirementTreeTocEntry[];
}

export interface RequirementTreeSnapshot extends JsonRecord {
  schema_version: "requirement-tree-snapshot/v1";
  source_identity: {
    project_id: string;
    document_id: string;
    source_ir_id: string;
    source_sha256: string;
    preview_generation_id: string;
    requirement_set_id: string;
    geometry_index_id?: string;
    geometry_index_sha256?: string;
  };
  structure_identity: {
    base_snapshot_id: string;
    effective_snapshot_id: string;
    effective_tree_sha256: string;
    revision: number;
  };
  mode: RequirementTreeMode;
  state: RequirementTreeState;
  root_node_ids: string[];
  nodes: RequirementTreeNode[];
  table_record_projections?: JsonRecord[];
  table_evidence_bindings?: JsonRecord[];
  requirement_evidence_bundle?: JsonRecord | null;
}

export interface TableRecordProjectionRowCoordinate extends JsonRecord {
  table_id: string;
  row_index: number;
}

export interface TableRecordProjectionRowDescriptor
  extends TableRecordProjectionRowCoordinate {
  page_number: number | null;
  display_text: string;
}

export interface TableRecordProjectionRowGroup extends JsonRecord {
  rows: TableRecordProjectionRowCoordinate[];
}

export interface TableRecordProjectionRowGroupReview extends JsonRecord {
  schema_version: "table-record-projection-row-group-review/v1";
  project_id: string;
  document_id: string;
  projection_id: string;
  logical_table_id: string;
  expected_projection_revision: number;
  expected_source_sha256: string;
  expected_structure_snapshot_id: string;
  editable: boolean;
  read_only_reason: string | null;
  eligible_rows: TableRecordProjectionRowDescriptor[];
  current_row_groups: TableRecordProjectionRowGroup[];
  suggested_row_groups: TableRecordProjectionRowGroup[];
}

export type TableRecordProjectionRevisionAction =
  | "SPLIT_ROWS"
  | "MERGE_PREVIOUS"
  | "MERGE_NEXT"
  | "KEEP_WHOLE"
  | "RESTORE_AUTO";

export interface TableRecordProjectionRevisionRequestV1 extends JsonRecord {
  schema_version: "table-record-projection-revision-request/v1";
  action: TableRecordProjectionRevisionAction;
  expected_projection_revision: number;
  record_id?: string;
}

export interface TableRecordProjectionRevisionRequestV2 extends JsonRecord {
  schema_version: "table-record-projection-revision-request/v2";
  action: "SPLIT_ROWS";
  expected_projection_revision: number;
  expected_source_sha256: string;
  expected_structure_snapshot_id: string;
  row_groups: TableRecordProjectionRowGroup[];
}

export type TableRecordProjectionRevisionRequest =
  | TableRecordProjectionRevisionRequestV1
  | TableRecordProjectionRevisionRequestV2;

export interface TableRecordProjectionRevisionResponse extends JsonRecord {
  schema_version: "table-record-projection-revision-response/v1";
  project_id: string;
  document_id: string;
  created: boolean;
  revision: JsonRecord;
  projection: JsonRecord | null;
}

export type EntryClassificationType =
  | "TECHNICAL"
  | "NON_TECHNICAL"
  | "INFORMATION";
export type EntryClassificationStatus = "WORKING" | "ACCEPTED";
export type EntryClassificationSuggestionSource =
  | "MODEL"
  | "RULE_EVIDENCE"
  | "NONE";

export interface EntryClassificationEntry extends JsonRecord {
  tree_node_id: string;
  workflow_node_id: string;
  projection_role: string;
  suggested_type: EntryClassificationType | null;
  suggestion_source: EntryClassificationSuggestionSource;
  effective_type: EntryClassificationType | null;
  human_override: EntryClassificationType | null;
  reason_codes: string[];
  confidence: number | null;
}

export interface EntryClassificationSnapshot extends JsonRecord {
  schema_version: "entry-classification-snapshot/v1";
  snapshot_id: string;
  payload_sha256: string;
  content_sha256?: string;
  project_id: string;
  sor_document_id: string;
  source_sha256: string;
  working_tree_id: string;
  working_tree_sha256: string;
  effective_structure_id: string;
  semantic_snapshot_id: string;
  policy_version: string;
  revision: number;
  status: EntryClassificationStatus;
  entries: EntryClassificationEntry[];
  supersedes_id: string;
  created_at: string;
  accepted_at: string;
  accepted_by: string;
}

export interface EntryClassificationCounts extends JsonRecord {
  total: number;
  technical: number;
  non_technical: number;
  information: number;
  unclassified: number;
  human_override: number;
}

export interface EntryClassificationResponse extends JsonRecord {
  schema_version: "entry-classification-response/v1";
  snapshot: EntryClassificationSnapshot;
  counts: EntryClassificationCounts;
  can_accept: boolean;
  can_publish: boolean;
  actions: {
    can_override: boolean;
    can_accept: boolean;
    can_publish: boolean;
  };
  changed: boolean;
  serving_impact: "NONE";
}

export interface EntryClassificationCommand extends JsonRecord {
  command_id: string;
  expected_revision: number;
  expected_snapshot_sha256: string;
  expected_source_sha256: string;
  expected_working_tree_sha256: string;
  expected_effective_structure_id: string;
  expected_policy_version: string;
}

export interface EntryClassificationPatchCommand
  extends EntryClassificationCommand {
  overrides: Record<string, EntryClassificationType | null>;
}

export type RequirementAtomizationScopeOperation = "APPLY" | "UNDO";
export type RequirementAtomizationScopeTarget =
  | "CURRENT"
  | "SELECTED"
  | "BRANCH"
  | "ALL";

export interface RequirementAtomizationScopeCommand {
  schema_version: "requirement-atomization-run-request/v2";
  command_id: string;
  operation: RequirementAtomizationScopeOperation;
  scope: RequirementAtomizationScopeTarget;
  expected_review_revision: number;
  expected_scope_revision: number;
  expected_entry_classification_snapshot_id: string;
  expected_entry_classification_revision: number;
  expected_entry_classification_sha256: string;
  expected_working_tree_id: string;
  expected_working_tree_sha256: string;
  expected_effective_structure_id: string;
  anchor_tree_node_id: string | null;
  target_review_node_ids: string[];
  target_scope_sha256: string;
  unselected_policy: "SOURCE_BOUND_PASSTHROUGH";
}

export interface RequirementAtomizationScopeCapability extends JsonRecord {
  schema_version: "requirement-atomization-scope-capability/v1";
  available: boolean;
  explicit_trigger_required: true;
  read_has_side_effects: false;
  operations: RequirementAtomizationScopeOperation[];
  scopes: RequirementAtomizationScopeTarget[];
  unselected_policy: "SOURCE_BOUND_PASSTHROUGH";
}

export type RequirementAtomizationScopeAttemptStatus =
  | "RUNNING"
  | "INTERRUPTED"
  | "SUCCEEDED";

export interface RequirementAtomizationScopeSkippedTarget extends JsonRecord {
  review_node_id: string;
  reason:
    | "UNCLASSIFIED"
    | "NON_TECHNICAL"
    | "INFORMATION"
    | "NOT_INCLUDED"
    | "NOT_REQUIREMENT"
    | "NOT_DIRECT";
}

export interface RequirementAtomizationScopeAttempt extends JsonRecord {
  schema_version: "atomization-scope-attempt/v1";
  operation: RequirementAtomizationScopeOperation;
  scope: RequirementAtomizationScopeTarget;
  status: RequirementAtomizationScopeAttemptStatus;
  requested_review_node_ids: string[];
  executed_review_node_ids: string[];
  skipped_targets: RequirementAtomizationScopeSkippedTarget[];
  cumulative_review_node_ids: string[];
  completed_review_node_ids: string[];
  failed_review_node_ids: string[];
  checkpoint_created_review_node_ids: string[];
  checkpoint_reused_review_node_ids: string[];
  checkpoint_invalidated_review_node_ids: string[];
  provider_call_count: number;
  retryable: boolean;
  checkpoint_resumable: boolean;
  error_code: string;
}

export interface RequirementAtomizationScopeReceipt extends JsonRecord {
  schema_version: "atomization-scope-receipt/v1";
  receipt_id: string;
  payload_sha256: string;
  scope_revision: number;
  operation: RequirementAtomizationScopeOperation;
  scope: RequirementAtomizationScopeTarget;
  requested_review_node_ids: string[];
  executed_review_node_ids: string[];
  new_review_node_ids: string[];
  reused_review_node_ids: string[];
  skipped_targets: RequirementAtomizationScopeSkippedTarget[];
  succeeded_review_node_ids: string[];
  failed_review_node_ids: string[];
  pass_through_review_node_ids: string[];
  cumulative_review_node_ids: string[];
  provider_call_count: number;
  checkpoint_created_count: number;
  checkpoint_reuse_count: number;
  resumable: boolean;
}

export interface RequirementAtomizationScopeState extends JsonRecord {
  schema_version: "requirement-atomization-scope-state/v1";
  capability: RequirementAtomizationScopeCapability;
  current_scope_revision: number;
  eligible_review_node_ids: string[];
  eligible_review_node_count: number;
  classification_binding_state:
    | "CLASSIFICATION_UNAVAILABLE"
    | "NOT_STARTED"
    | "CURRENT"
    | "CONTENT_EQUIVALENT"
    | "STALE";
  classification_snapshot_id: string;
  classification_revision: number;
  classification_sha256: string;
  classification_content_sha256: string;
  can_undo: boolean;
  current_atomization_run_id: string;
  current_atomization_run_revision: number;
  current_receipt: RequirementAtomizationScopeReceipt | null;
  last_attempt: RequirementAtomizationScopeAttempt | null;
}

export interface RequirementEnrichmentState extends JsonRecord {
  schema_version: "requirement-enrichment-state/v1";
  project_id: string;
  document_id: string;
  session_id: string;
  review_revision: number;
  atomization_scope: RequirementAtomizationScopeState;
  semantic_publication_capability?: RequirementSemanticPublicationCapabilityV2 | null;
}

export interface RequirementSemanticPublicationCapabilityV2 extends JsonRecord {
  schema_version: "requirement-enrichment-semantic-publication-capability/v2";
  preview_request_schema_version: "requirement-enrichment-publication-preview-request/v2";
  preview_response_schema_version: "requirement-enrichment-publication-preview-response/v2";
  publish_request_schema_version: "requirement-enrichment-publish-draft-request/v2";
  publish_response_schema_version: "requirement-enrichment-publish-draft-response/v2";
  atomization_states: ["NOT_RUN", "APPLIED"];
  not_run_preview_invokes_atomization: false;
  publish_starts_analysis: false;
  default_dispositions: {
    TECHNICAL: "ANALYZE";
    NON_TECHNICAL: "RETAIN";
    INFORMATION: "REFERENCE";
  };
}

export interface RequirementSemanticPublicationCountsV2 extends JsonRecord {
  total: number;
  default_analyze: number;
  default_retain: number;
  reference: number;
  atomized: number;
}

interface RequirementSemanticPublicationPreviewRequestBaseV2 {
  schema_version: "requirement-enrichment-publication-preview-request/v2";
  expected_review_revision: number;
  accepted_entry_classification_snapshot_id: string;
  accepted_entry_classification_revision: number;
  accepted_entry_classification_sha256: string;
}

export type RequirementSemanticPublicationPreviewRequestV2 =
  | (RequirementSemanticPublicationPreviewRequestBaseV2 & {
      atomization_state: "NOT_RUN";
    })
  | (RequirementSemanticPublicationPreviewRequestBaseV2 & {
      atomization_state: "APPLIED";
      atomization_scope_receipt_id: string;
      atomization_scope_receipt_sha256: string;
      atomization_scope_revision: number;
    });

export interface RequirementSemanticPublicationProjectionRowV2
  extends JsonRecord {
  requirement_tree_node_id: string;
  source_working_tree_node_id: string;
  parent_requirement_tree_node_id: string | null;
  workflow_node_id: string;
  atomization_node_id: string | null;
  atomic_requirement_id: string | null;
  classification: "TECHNICAL" | "NON_TECHNICAL" | "INFORMATION";
  entry_kind: "HEADING" | "REQUIREMENT" | "REFERENCE";
  builder_kind: "REQUIREMENT" | "REQUIREMENT_CONTAINER";
  decision_mode: "DIRECT" | "ROLLUP" | "NONE";
  builder_binding_role: string;
  final_projection_role: string;
  intrinsic_analysis_eligible: boolean;
  default_disposition: "ANALYZE" | "RETAIN";
  source_order: number;
  source_offset: number;
  label: string;
  original_statement: string;
  published_statement: string;
  structure_node_id: string | null;
  page_start: number | null;
  page_end: number | null;
  source_spans: JsonRecord[];
  source_refs: JsonRecord[];
  status: string;
  table_record_id: string | null;
  table_evidence_binding_id: string | null;
  deterministic_rule_evidence: JsonRecord[];
  include_in_view: boolean;
  content_blocks: JsonRecord[];
  editable_structure_node_ids: string[];
  table_refs: JsonRecord[];
  figure_refs: JsonRecord[];
}

export interface RequirementSemanticPublicationProjectionV2 extends JsonRecord {
  schema_version: "requirement-enrichment-publication-projection/v2";
  project_id: string;
  document_id: string;
  source_sha256: string;
  effective_structure_id: string;
  accepted_working_tree_id: string;
  accepted_working_tree_sha256: string;
  entry_classification_snapshot_id: string;
  entry_classification_revision: number;
  entry_classification_sha256: string;
  entry_classification_content_sha256: string;
  atomization_state: "NOT_RUN" | "APPLIED";
  atomization_scope_receipt_id: string;
  atomization_scope_receipt_sha256: string;
  atomization_scope_revision: number;
  atomization_active_revision: number;
  atomization_classification_content_sha256: string;
  atomization_run_id: string;
  atomization_revision: number;
  atomization_run_sha256: string;
  publication_preview_id: string;
  publication_preview_sha256: string;
  publication_key: string;
  publication_lineage_sha256: string;
  applied_review_node_ids: string[];
  source_business_tree_node_ids: string[];
  rows: RequirementSemanticPublicationProjectionRowV2[];
  projection_sha256: string;
}

export interface RequirementSemanticPublicationBindingPreviewV2
  extends JsonRecord {
  schema_version: "requirement-enrichment-publication-binding-preview/v2";
  binding_preview_id: string;
  project_id: string;
  document_id: string;
  session_id: string;
  review_revision: number;
  base_active_requirement_set_id: string;
  projection: RequirementSemanticPublicationProjectionV2;
  payload_sha256: string;
}

export interface RequirementSemanticPublicationPreviewResponseV2
  extends JsonRecord {
  schema_version: "requirement-enrichment-publication-preview-response/v2";
  project_id: string;
  document_id: string;
  serving_impact: "NONE";
  analysis_started: false;
  atomization_invoked: false;
  classification: {
    snapshot_id: string;
    revision: number;
    sha256: string;
  };
  binding_preview: RequirementSemanticPublicationBindingPreviewV2;
  counts: RequirementSemanticPublicationCountsV2;
}

export interface RequirementSemanticPublishDraftRequestV2 {
  schema_version: "requirement-enrichment-publish-draft-request/v2";
  expected_review_revision: number;
  binding_preview_id: string;
  binding_preview_sha256: string;
}

export interface RequirementSemanticPublicationBindingSummaryV2
  extends JsonRecord {
  binding_id: string;
  sha256: string;
  preview_id: string;
  preview_sha256: string;
  state: "READY";
}

export interface RequirementSemanticPublishDraftResponseV2 extends JsonRecord {
  schema_version: "requirement-enrichment-publish-draft-response/v2";
  project_id: string;
  document_id: string;
  serving_impact: "DRAFT_ONLY";
  analysis_started: false;
  requirement_set_created: boolean;
  requirement_set_activated: false;
  classification: {
    snapshot_id: string;
    revision: number;
    sha256: string;
  };
  binding: RequirementSemanticPublicationBindingSummaryV2;
  counts: RequirementSemanticPublicationCountsV2;
  publication: JsonRecord;
  requirement_tree: RequirementTreeSnapshot;
}

export interface RequirementAtomizationScopeRunResponse extends JsonRecord {
  schema_version: "requirement-atomization-run-response/v2";
  serving_impact: "NONE";
  replayed: boolean;
  atomization_run: JsonRecord;
  scope_receipt: RequirementAtomizationScopeReceipt;
}

export type RequirementMatrixState = "READY" | "REQUIRES_REEXTRACTION";
export type RequirementMatrixNodeDisposition =
  | "ANALYZE"
  | "RETAIN"
  | "EXCLUDE";
export type RequirementMatrixDisplayRole =
  | "STRUCTURE"
  | "CLAUSE"
  | "TABLE"
  | "TABLE_RECORD"
  | "ATOMIC_REQUIREMENT"
  | "CONTEXT";
export type RequirementMatrixVisibility =
  | "VISIBLE"
  | "SYSTEM_NON_BUSINESS"
  | "HUMAN_EXCLUDED";
export type RequirementMatrixAnalysisEligibility =
  | "ELIGIBLE"
  | "NOT_ELIGIBLE";
export type RequirementMatrixAnalysisStatus =
  | "NOT_APPLICABLE"
  | "NOT_STARTED"
  | "ASSESSED"
  | "DECIDED";
export type RequirementMatrixAnalysisResultState =
  | "NONE"
  | "CURRENT"
  | "STALE_REFERENCE";

export interface RequirementMatrixAiAssessment extends JsonRecord {
  decision: string;
  compliance_status: string;
  reason: string;
  confidence: number | null;
  assessment_mode: string;
}

export interface RequirementMatrixManualDecision extends JsonRecord {
  decision: string;
  reason: string;
  decided_at: string;
}

export interface RequirementMatrixNode extends JsonRecord {
  source_title_role?: string;
  source_number_role?: string;
  source_excerpts?: { id: string; role: string; text: string; pages: number[] }[];
  classification_paths?: string[];
  topic_tags?: string[];
  candidate_review?: JsonRecord;
  node_id: string;
  parent_id: string | null;
  child_ids: string[];
  depth: number;
  source_order: number;
  display_role: RequirementMatrixDisplayRole;
  source_kind?: string;
  source_number?: string;
  display_title?: string;
  display_text?: string;
  label: string;
  summary: string;
  content_blocks: JsonRecord[];
  table_refs: JsonRecord[];
  figure_refs: JsonRecord[];
  source_refs: JsonRecord[];
  page_start: number | null;
  page_end: number | null;
  structure_node_id: string | null;
  requirement_entry_id: string | null;
  requirement_id: string | null;
  requirement_code: string;
  requirement_text: string;
  table_record_id: string | null;
  logical_table_ids: string[];
  matrix_visibility: RequirementMatrixVisibility;
  disposition: RequirementMatrixNodeDisposition;
  disposition_reason: string;
  analysis_eligibility: RequirementMatrixAnalysisEligibility;
  analysis_status: RequirementMatrixAnalysisStatus;
  analysis_result_state?: RequirementMatrixAnalysisResultState;
  ai_assessment: RequirementMatrixAiAssessment | null;
  manual_decision: RequirementMatrixManualDecision | null;
  stale_ai_assessment?: RequirementMatrixAiAssessment | null;
  stale_manual_decision?: RequirementMatrixManualDecision | null;
  compliance_status: string;
  save_revision: number;
  updated_at: string;
}

export interface RequirementMatrixLogicalTable extends JsonRecord {
  logical_table_id: string;
  projection_id: string;
  projection_mode: string;
  record_count: number;
  physical_table_ids: string[];
  node_ids: string[];
  evidence_binding_ids: string[];
  projection?: JsonRecord;
  evidence_bindings?: JsonRecord[];
}

export interface RequirementMatrixCounts extends JsonRecord {
  total_nodes: number;
  visible_nodes: number;
  system_non_business_nodes: number;
  human_excluded_nodes: number;
  analyze_nodes: number;
  retained_nodes: number;
  excluded_nodes: number;
  analysis_eligible_nodes: number;
  assessed_nodes: number;
  decided_nodes: number;
  stale_result_nodes?: number;
  logical_tables: number;
}

export interface RequirementMatrixSnapshot extends JsonRecord {
  schema_version: "requirement-matrix-snapshot/v1";
  state: RequirementMatrixState;
  project_id: string;
  document_id: string;
  requirement_set_id: string;
  requirement_set_status: string;
  tree_sha256: string;
  root_node_ids: string[];
  nodes: RequirementMatrixNode[];
  logical_tables: RequirementMatrixLogicalTable[];
  counts: RequirementMatrixCounts;
}

export type RequirementAnalysisInteractiveScope = "CURRENT" | "SELECTED" | "ALL";
export type RequirementAnalysisScope =
  | RequirementAnalysisInteractiveScope
  | "FAILED_ONLY";

export interface RequirementAnalysisScopeExample extends JsonRecord {
  node_id: string;
  requirement_id?: string;
  requirement_code?: string;
  label: string;
  reason?: string;
  reason_code?: string;
}

interface RequirementAnalysisScopePreviewBase extends JsonRecord {
  command_id: string;
  requirement_set_id: string;
  requirement_set_version: number;
  tree_sha256: string;
  scope_sha256: string;
  requested_node_ids: string[];
  target_node_ids: string[];
  target_requirement_ids: string[];
  requested_count: number;
  target_count: number;
  skipped_count: number;
  skipped_reason_counts: Record<string, number>;
  target_examples: RequirementAnalysisScopeExample[];
  skipped_examples: RequirementAnalysisScopeExample[];
}

export interface RequirementAnalysisScopePreviewV1
  extends RequirementAnalysisScopePreviewBase {
  schema_version: "requirement-analysis-scope-preview/v1";
  scope: RequirementAnalysisInteractiveScope;
  publication_mode: "ATOMIC_BATCH";
}

export interface RequirementAnalysisScopePreviewV2
  extends RequirementAnalysisScopePreviewBase {
  schema_version: "requirement-analysis-scope-preview/v2";
  scope: RequirementAnalysisScope;
  publication_mode: "PER_NODE_RECOVERABLE";
  source_job_id?: string | null;
  failed_snapshot_sha256: string;
  source_failed_count: number;
  already_current_count: number;
  retry_target_count: number;
  retry_already_current_count: number;
  retry_skipped_count: number;
}

export type RequirementAnalysisScopePreview =
  | RequirementAnalysisScopePreviewV1
  | RequirementAnalysisScopePreviewV2;

interface RequirementAnalysisScopeRequestBase extends JsonRecord {
  command_id: string;
  requirement_set_id: string;
  expected_tree_sha256: string;
  node_ids: string[];
}

export interface RequirementAnalysisScopeRequestV1
  extends RequirementAnalysisScopeRequestBase {
  schema_version: "requirement-analysis-scope-request/v1";
  scope: RequirementAnalysisInteractiveScope;
}

export interface RequirementAnalysisScopeRequestV2
  extends RequirementAnalysisScopeRequestBase {
  schema_version: "requirement-analysis-scope-request/v2";
  scope: RequirementAnalysisInteractiveScope;
  source_job_id?: never;
}

export interface RequirementAnalysisFailedOnlyScopeRequestV2
  extends RequirementAnalysisScopeRequestBase {
  schema_version: "requirement-analysis-scope-request/v2";
  scope: "FAILED_ONLY";
  node_ids: [];
  source_job_id: string;
}

export type RequirementAnalysisScopeRequest =
  | RequirementAnalysisScopeRequestV1
  | RequirementAnalysisScopeRequestV2
  | RequirementAnalysisFailedOnlyScopeRequestV2;

export type RequirementAnalysisNodeExecutionStatus =
  | "WAITING"
  | "RUNNING"
  | "PUBLISHING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export interface RequirementAnalysisNodeExecution extends JsonRecord {
  execution_id: string;
  target_id: string;
  job_id: string;
  node_id: string;
  requirement_id: string;
  requirement_code: string;
  source_order: number;
  attempt_no: number;
  status: RequirementAnalysisNodeExecutionStatus;
  stage: string;
  published: boolean;
  retryable: boolean;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RequirementAnalysisExecutionCounts extends JsonRecord {
  total: number;
  waiting: number;
  running: number;
  publishing: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  retryable_failed: number;
  settled: number;
}

export interface RequirementSetLockReadinessCounts extends JsonRecord {
  visible_content: number;
  analysis_targets: number;
  retained_visible: number;
  excluded_visible: number;
  skipped_visible: number;
  technical_source_entries: number;
  non_technical_source_entries: number;
  information_source_entries: number;
  source_entries: number;
  atomic_requirements: number;
  stale_analysis_results?: number;
}

export interface RequirementSetLockReadinessNotice extends JsonRecord {
  code?: string;
  message?: string;
  detail?: string;
}

export interface RequirementSetLockReadinessCountMetadata extends JsonRecord {
  content_classification: "AVAILABLE" | "LEGACY_UNAVAILABLE";
  entry_composition: "AVAILABLE" | "LEGACY_UNAVAILABLE";
  source: "FROZEN_TREE" | "LEGACY_MATRIX";
}

export type RequirementSetLockReadinessItem =
  | string
  | RequirementSetLockReadinessNotice;

export interface RequirementSetLockReadiness extends JsonRecord {
  schema_version: string;
  readiness_sha256: string;
  can_lock: boolean;
  next_destination: "ANALYSIS" | "MATRIX";
  counts: RequirementSetLockReadinessCounts;
  count_metadata?: RequirementSetLockReadinessCountMetadata;
  hard_blockers: RequirementSetLockReadinessItem[];
  warnings: RequirementSetLockReadinessItem[];
}

export interface RequirementSetLockReceipt extends JsonRecord {
  schema_version?: string;
  requirement_set_id?: string;
  status?: string;
  next_destination?: "ANALYSIS" | "MATRIX";
  counts?: RequirementSetLockReadinessCounts;
  locked_requirement_count?: number;
}

export interface RequirementMatrixNodeDetail extends JsonRecord {
  schema_version: "requirement-matrix-node-detail/v1";
  project_id: string;
  document_id: string;
  requirement_set_id: string;
  node: RequirementMatrixNode;
  path: RequirementMatrixNode[];
  parent_clause?: RequirementMatrixNode | null;
  tree_node: JsonRecord;
  requirement: Requirement | null;
  assessment: JsonRecord | null;
  logical_tables: RequirementMatrixLogicalTable[];
}

export interface RequirementMatrixTableDetail extends JsonRecord {
  schema_version: "requirement-matrix-logical-table-detail/v1";
  project_id: string;
  document_id: string;
  requirement_set_id: string;
  logical_table: RequirementMatrixLogicalTable;
  nodes: RequirementMatrixNode[];
}

export interface StructureOptimizationJob extends JsonRecord {
  job_id?: string | null;
  status?: string;
  stage?: string;
  optimization_status?: string | null;
  result?: JsonRecord | null;
  error_code?: string;
}

export interface StructureOptimizationPayload extends JsonRecord {
  project_id?: string;
  document_id?: string;
  job?: StructureOptimizationJob | null;
}

export interface RestorationRun extends JsonRecord {
  run_id?: string;
  id?: string;
  status?: string;
  page_count?: number;
  total_pages?: number;
  current_page?: number;
  progress?: JsonRecord;
}

export interface ApiErrorPayload {
  detail?: unknown;
  error?: unknown;
  message?: unknown;
  code?: unknown;
}
