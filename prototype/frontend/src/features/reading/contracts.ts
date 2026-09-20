export type ReadingJsonRecord = Record<string, unknown>;

export type ReadingStatus =
  | "PENDING"
  | "READY"
  | "PARTIAL"
  | "STALE"
  | "FAILED";

export interface ReadingProject {
  id: string;
  name: string;
  customer_name: string;
}

export interface ReadingDocument {
  id: string;
  project_id: string;
  file_name: string;
  file_type?: string;
  detected_format?: string;
  source_sha256?: string;
  source_size?: number;
  page_count?: number;
  created_at?: string;
}

export interface ReadingProjectCatalogV1 {
  schema_version: "reading-project-catalog/v1";
  items: ReadingProject[];
}

export interface ReadingDocumentCatalogV1 {
  schema_version: "reading-document-catalog/v1";
  project_id: string;
  items: ReadingDocument[];
}

export interface ReadingSourceRef extends ReadingJsonRecord {
  kind?: "SOURCE_TEXT" | "VISUAL_TEXT" | (string & {});
  source_node_id?: string;
  visual_snapshot_id?: string;
  observation_id?: string;
  start_offset: number;
  end_offset: number;
  page_number?: number;
  bbox?: number[];
  page_bbox?: number[];
  page_image_sha256?: string;
  provider_id?: string;
  provider_version?: string;
}

export interface ReadingRiskRegion extends ReadingJsonRecord {
  kind: string;
  page_number?: number;
  source_node_id?: string;
  confidence: number;
  reason_code: string;
}

export interface ReadingRangeV1 extends ReadingJsonRecord {
  page_start?: number;
  page_end?: number;
  source_node_ids?: string[];
  reason_codes?: string[];
}

export interface ReadingCoverageV1 extends ReadingJsonRecord {
  status: "NOT_EVALUATED" | "PARTIAL" | "COMPLETE" | (string & {});
  evaluated_ranges: ReadingRangeV1[];
  unevaluated_ranges: ReadingRangeV1[];
  blocked_ranges: ReadingRangeV1[];
  reason_codes: string[];
}

export interface ReadingOverviewCandidateV1 extends ReadingJsonRecord {
  title?: string;
  label?: string;
  page_start?: number;
  page_end?: number;
  source_node_id?: string;
  confidence: number;
  reason_codes?: string[];
}

export interface DocumentReadingOverviewV1 extends ReadingJsonRecord {
  schema_version: "document-reading-overview/v1" | (string & {});
  document_type_candidate?: ReadingOverviewCandidateV1 | null;
  document_format: string;
  page_count: number;
  node_count: number;
  table_count: number;
  image_count: number;
  heading_count: number;
  toc_candidates: ReadingOverviewCandidateV1[];
  section_candidates: ReadingOverviewCandidateV1[];
  body_candidate_ranges: ReadingRangeV1[];
  appendix_candidate_ranges: ReadingRangeV1[];
  attachment_suspect_ranges: ReadingRangeV1[];
  risk_regions: ReadingRiskRegion[];
  coverage: ReadingCoverageV1;
}

export interface ReadingStructureMapNodeV1 extends ReadingJsonRecord {
  map_node_id: string;
  source_node_id: string;
  parent_map_node_id?: string | null;
  kind: "SECTION" | "TOC_ENTRY" | "APPENDIX" | "ATTACHMENT" | (string & {});
  title: string;
  level_hint: number;
  source_order: number;
  page_start: number;
  page_end: number;
  confidence: number;
  reason_codes: string[];
  evidence_status:
    | "SOURCE_BOUND"
    | "PARTIAL_EVIDENCE"
    | "STALE"
    | (string & {});
}

export interface ReadingStructureMapV1 extends ReadingJsonRecord {
  schema_version: "reading-structure-map/v1" | (string & {});
  map_id: string;
  status: ReadingStatus | (string & {});
  generation_status: ReadingStatus | (string & {});
  coverage_status: "NOT_EVALUATED" | "PARTIAL" | "COMPLETE" | (string & {});
  page_count: number;
  reason_codes: string[];
  nodes: ReadingStructureMapNodeV1[];
}

export interface ReadingSelectableNodeV1 extends ReadingJsonRecord {
  source_node_id: string;
  kind: string;
  source_order: number;
  page_number?: number;
  text_length: number;
  preview: string;
}

export interface ReadingSourcePageNodeV1 extends ReadingSelectableNodeV1 {
  text: string;
}

export interface ReadingSourcePageV1 extends ReadingJsonRecord {
  schema_version: "reading-source-page/v1" | (string & {});
  project_id: string;
  sor_document_id: string;
  reading_session_id: string;
  source_ir_id: string;
  source_sha256: string;
  effective_bundle_id: string;
  visual_generation_id: string | null;
  page_number: number;
  page_count: number;
  readable_mode: "NATIVE" | "VISUAL" | "NONE" | (string & {});
  nodes: ReadingSourcePageNodeV1[];
  visual_runs: ReadingJsonRecord[];
}

export type ReadingAction =
  | "EXPLANATION"
  | "TRANSLATION"
  | "ENGINEERING_IMPACT"
  | "CLARIFICATION_QUESTION";

export interface ReadingArtifactV1 extends ReadingJsonRecord {
  schema_version: "reading-artifact/v1" | (string & {});
  artifact_id: string;
  reading_session_id: string;
  project_id: string;
  sor_document_id: string;
  source_sha256: string;
  action: ReadingAction | (string & {});
  source_ref: ReadingSourceRef;
  output_text: string;
  status: "ACTIVE" | "STALE" | "FAILED" | (string & {});
  provider_id: string;
  model_name: string;
  prompt_version: string;
  created_at_utc: string;
}

export interface ReadingNoteV1 extends ReadingJsonRecord {
  schema_version: "reading-note/v1" | (string & {});
  note_id: string;
  reading_session_id: string;
  project_id: string;
  sor_document_id: string;
  source_sha256: string;
  source_ref: ReadingSourceRef;
  content: string;
  revision: number;
  status: "ACTIVE" | "STALE" | "DELETED" | (string & {});
  created_at_utc: string;
  updated_at_utc: string;
}

export interface ReadingContextV1 extends ReadingJsonRecord {
  schema_version: "reading-context/v1" | (string & {});
  reading_session_id: string;
  project_id: string;
  sor_document_id: string;
  source_sha256: string;
  source_ir_id: string;
  source_ir_document_id: string;
  effective_bundle_id: string;
  visual_generation_id: string | null;
  status: ReadingStatus | (string & {});
  overview: DocumentReadingOverviewV1;
  structure_map: ReadingStructureMapV1;
  selectable_nodes: ReadingSelectableNodeV1[];
  artifacts: ReadingArtifactV1[];
  notes: ReadingNoteV1[];
  created_at_utc: string;
  updated_at_utc: string;
}

export interface ReadingPrepareRequestV1 {
  schema_version: "reading-prepare-request/v1";
  expected_source_sha256?: string;
}

export interface ReadingActionRequestV1 {
  schema_version: "reading-action-request/v1";
  action: ReadingAction;
  source_sha256: string;
  source_ref: ReadingSourceRef;
  target_language?: string;
}

export interface ReadingNoteCreateRequestV1 {
  schema_version: "reading-note-create-request/v1";
  source_sha256: string;
  source_ref: ReadingSourceRef;
  content: string;
}

export interface ReadingNoteUpdateRequestV1 {
  schema_version: "reading-note-update-request/v1";
  expected_revision: number;
  content: string;
}

export interface ReadingNoteListV1 extends ReadingJsonRecord {
  schema_version: "reading-note-list/v1" | (string & {});
  items: ReadingNoteV1[];
}

export interface ReadingNoteDeleteV1 extends ReadingJsonRecord {
  schema_version: "reading-note-delete-result/v1" | (string & {});
  deleted: true;
  note_id: string;
  revision: number;
}

export interface ReadingSelection {
  selection_id: string;
  kind: string;
  label: string;
  excerpt: string;
  page_number?: number;
  source_ref: ReadingSourceRef;
}

export type ReadingChatRole = "USER" | "ASSISTANT";

export type ReadingTaskType =
  | "FREE_QUESTION"
  | "QUICK_READ"
  | "TECHNICAL_CONDITIONS"
  | "SECTION_GUIDE"
  | "MISSED_CONDITIONS";

export interface ReadingCitationV1 extends ReadingJsonRecord {
  citation_id: string;
  locator_kind?:
    | "SOURCE_TEXT"
    | "TABLE_CELL"
    | "OCR_TEXT"
    | "VISUAL_TEXT"
    | "VISUAL_REGION"
    | (string & {});
  source_node_id?: string;
  visual_snapshot_id?: string;
  observation_id?: string;
  cell_id?: string;
  start_offset: number;
  end_offset: number;
  page_number?: number;
  bbox?: number[];
  page_bbox?: number[];
  page_image_sha256?: string;
  provider_id?: string;
  provider_version?: string;
  provenance_channel?: string;
  label?: string;
  quote?: string;
}

export interface ReadingChatMessageV1 extends ReadingJsonRecord {
  schema_version: "reading-chat-message/v1" | (string & {});
  message_id: string;
  conversation_id: string;
  reading_session_id: string;
  project_id: string;
  sor_document_id: string;
  source_sha256: string;
  effective_bundle_id?: string;
  visual_generation_id?: string | null;
  sequence_number: number;
  role: ReadingChatRole | (string & {});
  content: string;
  task_type?: ReadingTaskType | (string & {});
  source_ref?: ReadingSourceRef | null;
  source_refs?: ReadingSourceRef[];
  citations: ReadingCitationV1[];
  suggested_questions?: string[];
  provider_id?: string;
  model_name?: string;
  prompt_version?: string;
  retrieval?: ReadingRetrievalSummaryV1 | null;
  status: "ACTIVE" | "FAILED" | "STALE" | (string & {});
  created_at_utc: string;
}

export interface ReadingConversationSummaryV1 extends ReadingJsonRecord {
  schema_version: "reading-conversation/v1" | (string & {});
  conversation_id: string;
  reading_session_id: string;
  project_id: string;
  sor_document_id: string;
  source_sha256: string;
  effective_bundle_id?: string;
  visual_generation_id?: string | null;
  title: string;
  revision: number;
  status: "ACTIVE" | "STALE" | "DELETED" | (string & {});
  created_at_utc: string;
  updated_at_utc: string;
  last_message_at_utc?: string;
}

export type ReadingConversationV1 = ReadingConversationSummaryV1;

export interface ReadingConversationListV1 extends ReadingJsonRecord {
  schema_version: "reading-conversation-list/v1" | (string & {});
  items: ReadingConversationSummaryV1[];
}

export interface ReadingConversationCreateRequestV1 {
  schema_version: "reading-conversation-create-request/v1";
  title?: string;
}

export interface ReadingChatMessageCreateRequestV1 {
  schema_version: "reading-message-create-request/v1";
  source_sha256: string;
  content: string;
  source_ref?: ReadingSourceRef;
  source_refs?: ReadingSourceRef[];
  task_type?: ReadingTaskType;
  coverage_intent?: "AUTO" | "DOCUMENT_OVERVIEW";
}

export interface ReadingMessageListV1 extends ReadingJsonRecord {
  schema_version: "reading-message-list/v1" | (string & {});
  conversation_id: string;
  items: ReadingChatMessageV1[];
}

export interface ReadingRetrievalSummaryV1 extends ReadingJsonRecord {
  schema_version: "reading-retrieval/v1" | (string & {});
  strategy:
    | "source-ir-full-document/v2"
    | "effective-source-full-document/v1"
    | "effective-source-full-effective-text/v1"
    | "effective-source-selected-context/v1"
    | (string & {});
  selected_node_count: number;
  selected_character_count: number;
  document_node_count: number;
  covered_page_numbers: number[];
  coverage_mode:
    | "FULL_DOCUMENT"
    | "FULL_EFFECTIVE_TEXT"
    | "HIERARCHICAL_FULL_DOCUMENT"
    | "SELECTED_CONTEXT"
    | (string & {});
  effective_bundle_id?: string | null;
  visual_generation_id?: string | null;
  capture_quality_status?: "READY" | "PARTIAL" | "BLOCKED" | null;
  unreadable_page_numbers?: number[];
}

export interface ReadingChatTurnV1 extends ReadingJsonRecord {
  schema_version: "reading-chat-turn/v1" | (string & {});
  conversation: ReadingConversationSummaryV1;
  user_message: ReadingChatMessageV1;
  assistant_message: ReadingChatMessageV1;
  retrieval: ReadingRetrievalSummaryV1;
}

export interface ReadingConversationDeleteV1 extends ReadingJsonRecord {
  schema_version: "reading-conversation-delete-result/v1" | (string & {});
  deleted: true;
  conversation_id: string;
}

export interface ReadingConversationClearV1 extends ReadingJsonRecord {
  schema_version: "reading-conversation-clear-result/v1" | (string & {});
  conversation_id: string;
  cleared: true;
  deleted_message_count: number;
}

export interface ReadingAIReadinessV1 extends ReadingJsonRecord {
  schema_version: "reading-ai-readiness/v1" | (string & {});
  ready: boolean;
  provider_id?: string | null;
  model_name?: string | null;
  configuration_source:
    | "CODEX_RUNTIME"
    | "READING_OVERRIDE"
    | "SHARED_RUNTIME"
    | "NONE"
    | (string & {});
  reason_code?: string | null;
  capture_ready?: boolean | null;
  capture_quality_status?: "READY" | "PARTIAL" | "BLOCKED" | null;
  effective_bundle_id?: string | null;
  visual_generation_id?: string | null;
  visual_required_page_count?: number | null;
  visual_ready_page_count?: number | null;
}
