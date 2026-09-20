#!/usr/bin/env python3
"""Run the confirmed extraction cases without writing to the source corpus.

The runner evaluates document identity and preflight, and can collect safe
count-only observations from the current legacy path and native SourceIR
adapters. A product assertion passes only when a target-specific evaluator has
independent evidence; generic observation counts never masquerade as passes.
"""

# The repository-local app path must be installed before application imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import multiprocessing
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterable, Mapping

import jsonschema


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.private_eval import private_eval_path

from app.adapters.documents.preflight import preflight_document
from app.adapters.documents.local_attachment_store import LocalAttachmentArtifactStore
from app.adapters.documents.pdf_visual_text_runtime import (
    OCR_PROVIDER_AZURE,
    OCR_PROVIDER_BAILIAN,
    PdfVisualTextRuntime,
    build_pdf_visual_text_runtime_from_silver,
    validate_pdf_visual_text_capture,
)
from app.application.extraction.build_source_ir import (
    SourceIRBuildRejected,
    SourceIRBuildRejectionCode,
    build_source_ir,
)
from app.application.extraction.build_shadow_preview import build_document_view
from app.application.extraction.preserve_attachment import (
    AttachmentPreservationRejected,
    OpaqueAttachmentExtraction,
    preserve_opaque_attachment_pipeline,
)
from app.application.extraction.evaluate_pdf_shadow_silver import (
    DEFAULT_PDF_SHADOW_SILVER_PATH,
    DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
    load_pdf_shadow_silver,
)
from app.application.extraction.classify_content import (
    DEFAULT_SEMANTIC_CLASSIFIER_ID,
    DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    SemanticClassificationRejected,
    classify_content,
    classify_table_row_information,
    validate_semantic_classification_against_inputs,
)
from app.application.extraction.project_requirements import (
    DEFAULT_REQUIREMENT_PROJECTOR_ID,
    DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
    RequirementProjectionRejected,
    project_requirements,
    validate_requirement_view_against_inputs,
)
from app.application.extraction.resolve_structure import (
    DEFAULT_STRUCTURE_RESOLVER_ID,
    DEFAULT_STRUCTURE_RESOLVER_VERSION,
    StructureResolutionRejected,
    build_structure_tree,
    validate_structure_tree_against_source_ir,
)
from app.domain.extraction.source_ir import SourceAssetKind, SourceNodeKind
from app.domain.extraction.explicit_peer_items import (
    parse_explicit_numeric_peer_items,
)
from app.domain.extraction.pdf_page_furniture import visual_pdf_page_furniture_ids
from app.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionPolicy,
    RequirementProjectionRole,
)
from app.domain.extraction.attachment import (
    AttachmentDecisionMode,
    AttachmentReferenceRole,
    DeepRecognitionMode,
    ExtractionDocumentContext,
    ExtractionDocumentRole,
    ExtractionIngressRoute,
)
from app.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticAnnotationStatus,
    SemanticClass,
    SemanticInformationKind,
)
from app.domain.extraction.structure import (
    BilingualLanguageEvidence,
    BilingualMismatchSignal,
    StructureBoundaryBasis,
    StructureFindingSeverity,
    StructureNodeKind,
    StructureOptionState,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.services.extraction_preview import build_extraction_preview
from app.services.extractor import (
    analyze_section_consistency,
    extract_atomic_requirements,
    extract_section_outline,
)
from app.services.parser import parse_document_with_diagnostics
from scripts.pdf_body_review_artifacts import (
    canonical_json_bytes as canonical_pdf_body_review_json_bytes,
    canonical_json_sha256 as canonical_pdf_body_review_json_sha256,
    pdf_content_image_projection,
    summarize_pdf_content_image_occurrences,
)
from scripts.pdf_body_review_runtime import (
    ContentAddressedPdfBodyReviewResolver,
    PdfBodyReviewArtifactResolver,
    PdfBodyReviewTrustPolicy,
    VerifiedPdfBodyReviewProfile,
    load_pdf_body_review_trust_policy,
    resolve_selected_pdf_body_review_profiles,
)


SCHEMA_VERSION = "extraction-case-run/v3"
MANIFEST_SCHEMA_VERSION = "1.2.0"
DEFAULT_LEGACY_TIMEOUT_SECONDS = 60.0
PDF_VISUAL_PROVIDER_OFF = "off"
PDF_VISUAL_PROVIDER_CHOICES = (
    PDF_VISUAL_PROVIDER_OFF,
    OCR_PROVIDER_BAILIAN,
    OCR_PROVIDER_AZURE,
)
_PDF_VISUAL_RUNTIME_FAILURE_CODES = frozenset(
    {
        "pdf_visual_text_unavailable",
        "pdf_visual_text_invalid_input",
        "pdf_visual_text_source_hash_mismatch",
        "pdf_visual_text_page_image_hash_mismatch",
        "pdf_visual_text_provider_output_invalid",
        "pdf_visual_text_provider_failed",
    }
)
_LEGACY_WORKER_SHUTDOWN_SECONDS = 2.0
EXTERNAL_PREFIX = "external://training-corpus/"
EXPECTED_CASE_IDS = {
    *(f"ROLE-{index:03d}" for index in range(1, 6)),
    *(f"STR-{index:03d}" for index in range(1, 9)),
    *(f"TAB-{index:03d}" for index in range(1, 6)),
    *(f"SEM-{index:03d}" for index in range(1, 5)),
    *(f"FMT-{index:03d}" for index in range(1, 8)),
}
EXPECTED_DOCUMENT_IDS = {
    "DOC-LONG-AXLE-PDS-XLSX",
    "DOC-OIL-PUMP-PDS-XLSX",
    "DOC-QSK-J02-311",
    "DOC-QC-T-1067",
    "DOC-PEF-DEVIATION-XLSX",
    "DOC-ESOW-DHT120-STATOR",
    "DOC-ESOW-DHT160-RING",
    "DOC-ESOW-DHT160-ROTOR",
    "DOC-ESOW-3DHT200-P3",
    "DOC-ESOW-3DHT200-P1",
    "DOC-PEF-SOR",
    "DOC-PEF-SOR-DUPLICATE",
    "DOC-IRPS-PRODUCT-INFO",
    "DOC-IRPS-TRL-B",
    "DOC-IRPS-TRL-DEEPAL",
    "DOC-IRPS-RFQ-9HAT",
    "DOC-IRPS-NCS-CM-XLSX",
    "DOC-IRPS-NCS-CM-PDF",
    "DOC-PDS-ACTIVE-DAMPER",
    "DOC-PDS-OIL-PUMP",
    "DOC-F06-ACTIVE-SUSPENSION",
    "DOC-QJLY-J7111029",
    "DOC-DRAWING-45104848",
    "DOC-DRAWING-PEF16C02",
    "DOC-DRAWING-TQ250-EM1",
}
LAYER_ORDER = (
    "source_ir",
    "structure",
    "semantic",
    "projection",
    "finding",
    "workflow",
)
LEGACY_SAFE_FORMATS = frozenset({"pdf", "docx", "txt"})
EXPECTED_NON_SERVING_FORMATS = frozenset({"xlsb", "tif"})
MANIFEST_SCHEMA_PATH = PROJECT_DIR / "eval" / "extraction_cases" / "schema.json"
ORACLE_REGISTRY_PATH = private_eval_path("extraction_cases/oracles.json")
ORACLE_SCHEMA_PATH = PROJECT_DIR / "eval" / "extraction_cases" / "oracles.schema.json"
ORACLE_EVALUATOR_ID = "source_ir.oracle_fidelity/v1"
ORACLE_V2_EVALUATOR_ID = "source_ir.oracle_fidelity/v2"
ORACLE_EVALUATOR_IDS = frozenset({ORACLE_EVALUATOR_ID, ORACLE_V2_EVALUATOR_ID})
ORACLE_REGISTRY_SCHEMA_VERSION = "source-ir-oracle-registry/v1"
ORACLE_FINGERPRINT_ALGORITHM = "sha256-canonical-json-source-ir-v2"
STRUCTURE_SINGLE_ROW_EVALUATOR_ID = "structure.single_table_row_boundary/v1"
STRUCTURE_DOCX_SINGLE_ROW_EVALUATOR_ID = (
    "structure.docx_single_table_row_boundary/v1"
)
PROJECTION_DOCX_SINGLE_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID = (
    "projection.docx_single_top_level_requirement/v1"
)
PROJECTION_DOCX_NO_SENTENCE_SPLIT_EVALUATOR_ID = (
    "projection.docx_no_sentence_split/v1"
)
SOURCE_DOCX_BILINGUAL_TEXTS_EVALUATOR_ID = (
    "source_ir.docx_bilingual_source_spans/v2"
)
STRUCTURE_BILINGUAL_COUNTERPART_EVALUATOR_ID = (
    "structure.bilingual_counterpart_group/v2"
)
PROJECTION_BILINGUAL_SINGLE_REQUIREMENT_EVALUATOR_ID = (
    "projection.bilingual_group_single_requirement/v2"
)
FMT001_COHORT_EVALUATOR_IDS = frozenset(
    {
        SOURCE_DOCX_BILINGUAL_TEXTS_EVALUATOR_ID,
        STRUCTURE_BILINGUAL_COUNTERPART_EVALUATOR_ID,
        PROJECTION_BILINGUAL_SINGLE_REQUIREMENT_EVALUATOR_ID,
    }
)
STRUCTURE_DOCUMENT_LANGUAGE_POLICY_EVALUATOR_ID = (
    "structure.document_language_policy/v1"
)
FINDING_BILINGUAL_MISMATCH_SAFETY_EVALUATOR_ID = (
    "finding.bilingual_mismatch_safety_contract/v1"
)
PROJECTION_MATERIAL_REQUIREMENT_COUNT_EVALUATOR_ID = (
    "projection.material_requirement_count/v1"
)
PROJECTION_MATERIAL_SHARED_VALUES_EVALUATOR_ID = (
    "projection.material_shared_values/v1"
)
STRUCTURE_EXPLICIT_TABLE_PEER_ITEMS_EVALUATOR_ID = (
    "structure.explicit_table_cell_peer_requirements/v1"
)
PROJECTION_EXPLICIT_PEER_SINGLE_REQUIREMENT_EVALUATOR_ID = (
    "projection.explicit_peer_parallel_actions_single_requirement/v1"
)
SOURCE_SCOPE_OWNER_EVALUATOR_IDS = frozenset(
    {
        STRUCTURE_DOCX_SINGLE_ROW_EVALUATOR_ID,
        PROJECTION_MATERIAL_SHARED_VALUES_EVALUATOR_ID,
    }
)
ORACLE_OWNER_EVALUATOR_IDS = ORACLE_EVALUATOR_IDS | SOURCE_SCOPE_OWNER_EVALUATOR_IDS
STRUCTURE_DATA_ROWS_EVALUATOR_ID = "structure.table_data_row_boundaries/v1"
PERFORMANCE_PARAMETER_ROWS_SEMANTIC_EVALUATOR_ID = (
    "semantic.performance_parameter_rows/v1"
)
REQUIREMENT_PER_PARAMETER_ROW_PROJECTION_EVALUATOR_ID = (
    "projection.requirement_per_parameter_row/v1"
)
TABLE_ROW_SCHEDULE_INFORMATION_EVALUATOR_ID = (
    "semantic.table_row_schedule_information/v1"
)
TABLE_ROW_SAMPLE_PHASE_INFORMATION_EVALUATOR_ID = (
    "semantic.table_row_sample_phase_information/v1"
)
SINGLE_COMPOUND_TABLE_ROW_PROJECTION_EVALUATOR_ID = (
    "projection.single_compound_test_requirement/v1"
)
TECHNICAL_NARRATIVE_DISPOSITION_EVALUATOR_ID = (
    "semantic.technical_narrative_disposition/v1"
)
SOURCE_ROLE_NOT_SEMANTIC_EXCLUSION_EVALUATOR_ID = (
    "projection.source_role_not_semantic_exclusion/v1"
)
STRUCTURE_MERGED_APPLICABILITY_EVALUATOR_ID = "structure.merged_cell_applicability/v1"
ATTACHMENT_IDENTITY_EVALUATOR_ID = "source_ir.opaque_attachment_identity/v1"
ATTACHMENT_DISPOSITION_EVALUATOR_ID = "semantic.document_attachment_disposition/v1"
ATTACHMENT_PROJECTION_EVALUATOR_ID = "projection.attachment_reference_only/v1"
ATTACHMENT_WORKFLOW_EVALUATOR_ID = "workflow.deep_drawing_backlog_policy/v1"
SECTION_REQUIREMENT_SUBTYPE_EVALUATOR_ID = "semantic.section_requirement_subtype/v1"
SECTION_REQUIREMENT_PROJECTION_EVALUATOR_ID = (
    "projection.section_primary_direct_not_background/v1"
)
SECTION_MAIN_REQUIREMENT_EVALUATOR_ID = "projection.section_main_requirement/v1"
SECTION_MAIN_ROLLUP_EVALUATOR_ID = "projection.section_main_rollup_requirement/v1"
PDF_TABLE_CONTINUATION_V1_EVALUATOR_ID = "structure.pdf_table_continuation/v1"
PDF_TABLE_CONTINUATION_EVALUATOR_ID = "structure.pdf_table_continuation/v2"
PDF_TABLE_ROWS_NOT_REQUIREMENTS_V1_EVALUATOR_ID = (
    "projection.pdf_table_rows_not_requirements/v1"
)
PDF_TABLE_ROWS_NOT_REQUIREMENTS_EVALUATOR_ID = (
    "projection.pdf_table_rows_not_requirements/v2"
)
PDF_TABLE_PARAMETER_COUNT_EVALUATOR_ID = "projection.pdf_table_parameter_count/v1"
PDF_TABLE_NOTE_APPLIES_TO_EVALUATOR_ID = "structure.pdf_table_note_applies_to/v1"
PDF_SECTION_CHILD_ASSERTION_EVALUATOR_ID = "projection.pdf_section_child_assertion/v1"
PDF_DUPLICATE_TABLE_LABEL_EVALUATOR_ID = "finding.pdf_duplicate_table_label/v1"
PDF_REGISTERED_SOURCE_IDENTITY_EVALUATOR_ID = (
    "source_ir.pdf_registered_source_identity/v1"
)
PDF_REVIEWED_BODY_SEQUENCE_EVALUATOR_ID = "source_ir.pdf_reviewed_body_sequence/v1"
PDF_SINGLE_TOC_NOISE_BLOCK_EVALUATOR_ID = "semantic.pdf_single_toc_noise_block/v2"
PDF_PAGE_NOISE_NOT_REQUIREMENTS_EVALUATOR_ID = (
    "projection.pdf_page_noise_not_requirements/v2"
)
PDF_OPTION_RISK_SEMANTIC_EVALUATOR_ID = "semantic.pdf_option_risk_information/v1"
PDF_OPTION_SECTION_NO_REQUIREMENTS_EVALUATOR_ID = (
    "projection.pdf_option_section_no_requirements/v1"
)
PDF_OPTION_REGULATION_SEMANTIC_EVALUATOR_ID = (
    "semantic.pdf_option_regulation_requirement/v1"
)
PDF_CHECKED_OPTION_APPLICABILITY_EVALUATOR_ID = (
    "structure.pdf_checked_option_applicability/v1"
)
PDF_OPTION_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID = (
    "projection.pdf_option_top_level_requirement/v1"
)
PDF_UNCHECKED_OPTION_NOT_EFFECTIVE_EVALUATOR_ID = (
    "projection.pdf_unchecked_option_not_effective/v1"
)
PDF_OPTION_UNCERTAINTY_POLICY_EVALUATOR_ID = "finding.pdf_option_uncertainty_policy/v1"
PDF_REVISION_HISTORY_SEMANTIC_EVALUATOR_ID = "semantic.pdf_revision_history_metadata/v1"
PDF_REVISION_HISTORY_NOT_REQUIREMENTS_EVALUATOR_ID = (
    "projection.pdf_revision_history_not_requirements/v1"
)
PDF_REVISION_HISTORY_NO_INFERENCE_EVALUATOR_ID = (
    "projection.pdf_revision_history_no_historic_inference/v1"
)
PDF_REVISION_SNAPSHOTS_DISTINCT_EVALUATOR_ID = (
    "workflow.pdf_revision_source_snapshots_distinct/v1"
)
SPREADSHEET_VISIBLE_WORKBOOK_FIDELITY_EVALUATOR_ID = (
    "source_ir.spreadsheet_visible_workbook_fidelity/v1"
)
CONFIRMED_CASE_EVALUATOR_CONTRACTS: dict[str, tuple[str, str, str, object]] = {
    "ROLE-001-A1": (
        "workflow.ingress_route_authority/v1",
        "document.role_authority",
        "equals",
        "ingress_route",
    ),
    "ROLE-001-A2": (
        "workflow.parser_role_noninterference/v1",
        "parser.role_override_from_filename_or_content",
        "equals",
        False,
    ),
    "ROLE-002-A1": (
        "workflow.engineer_structured_input_role/v1",
        "documents.document_role",
        "equals",
        "engineer_structured_input",
    ),
    "ROLE-002-A2": (
        "workflow.engineer_input_not_customer_truth/v1",
        "documents.customer_source_truth",
        "equals",
        False,
    ),
    "ROLE-003-A1": (
        "workflow.downstream_artifact_route/v1",
        "documents.ingress_route",
        "equals",
        "downstream_artifact",
    ),
    "ROLE-003-A2": (
        "workflow.no_reverse_customer_source/v1",
        "review_artifact.reverse_generated_customer_source",
        "equals",
        False,
    ),
    "ROLE-004-A1": (
        "workflow.pdf_customer_source_role/v1",
        "pdf_documents.document_role",
        "equals",
        "customer_requirement_source",
    ),
    "ROLE-004-A2": (
        "workflow.xlsx_internal_knowledge_role/v1",
        "xlsx_documents.document_role",
        "equals",
        "internal_knowledge",
    ),
    "ROLE-004-A3": (
        "workflow.no_pdf_xlsx_pairing/v1",
        "pdf_xlsx_pairing",
        "equals",
        False,
    ),
    "ROLE-004-A5": (
        "semantic.engineer_content_source_identity/v1",
        "xlsx.engineer_notes.source_identity",
        "classifies_as",
        "engineer_content",
    ),
    "ROLE-005-A1": (
        "workflow.no_corpus_pairing_inference/v1",
        "corpus.pairing_inference",
        "equals",
        False,
    ),
    "ROLE-005-A3": (
        "finding.duplicate_source_path_warning/v1",
        "duplicate_paths",
        "warns_without_mutation",
        "same content identity, separate source paths",
    ),
    "STR-003-A1": (
        "structure.section_ancestor_chain/v1",
        "section_4_12_2_1.ancestor_labels",
        "equals",
        ["4", "4.12", "4.12.2"],
    ),
    "STR-003-A2": (
        "structure.cross_page_section_identity/v1",
        "cross_page_continuation",
        "preserves",
        "same hierarchy and requirement identity",
    ),
    "STR-003-A3": (
        "structure.explicit_numbered_children/v1",
        "section_4_13_3.explicit_numbered_children",
        "equals",
        5,
    ),
    "STR-003-A4": (
        "projection.section_single_top_level_requirement/v1",
        "section_4_13_3.top_level_requirement_count",
        "equals",
        1,
    ),
    "STR-003-A5": (
        "finding.no_cross_page_pseudo_requirement/v1",
        "cross_page_pseudo_requirement",
        "excludes",
        True,
    ),
    "STR-006-A2": (
        "structure.section_parent_block/v1",
        "section_1_1.parent_block_count",
        "equals",
        1,
    ),
    "STR-006-A3": (
        "projection.section_not_one_requirement_per_paragraph/v1",
        "one_paragraph_one_requirement",
        "equals",
        False,
    ),
    "STR-007-A2": (
        "structure.numbering_gap_labels_preserved/v1",
        "printed_labels",
        "preserves",
        ["5.4.1.2", "5.4.1.4"],
    ),
    "STR-007-A3": (
        "finding.section_numbering_gap/v1",
        "numbering_gap",
        "warns_without_mutation",
        "missing printed label 5.4.1.3",
    ),
    "STR-007-A4": (
        "projection.no_synthetic_requirement_for_gap/v1",
        "synthetic_requirement_from_gap",
        "equals",
        False,
    ),
    "STR-008-A2": (
        "structure.duplicate_list_nodes_distinct/v1",
        "duplicate_label_nodes.internal_ids",
        "keeps_distinct",
        "page, position or order under common parent",
    ),
    "STR-008-A3": (
        "structure.duplicate_list_table_parent/v1",
        "table_3.parent",
        "equals",
        "second 5) node",
    ),
    "STR-008-A4": (
        "finding.duplicate_list_ordinal/v1",
        "duplicate_numbering",
        "warns_without_mutation",
        "do not renumber the second node",
    ),
    "TAB-003-A2": (
        "structure.table_rows_locatable/v1",
        "milestone_rows",
        "preserves",
        "locatable rows within table",
    ),
    "TAB-003-A3": (
        "semantic.schedule_table/v1",
        "milestone_table",
        "classifies_as",
        "project_schedule_information",
    ),
    "TAB-003-A4": (
        "projection.schedule_rows_not_product_requirements/v1",
        "product_requirements_from_milestone_rows",
        "equals",
        0,
    ),
    "TAB-004-A2": (
        "structure.minimum_table_review_structure/v1",
        "product_information_table",
        "equals",
        "minimum review and provenance structure",
    ),
    "TAB-004-A3": (
        "projection.no_extra_product_object_graph/v1",
        "extra_product_object_graph",
        "equals",
        False,
    ),
    "SEM-001-A2": (
        "projection.modal_absence_not_exclusion/v1",
        "modal_word_absence_as_exclusion_rule",
        "equals",
        False,
    ),
    "SEM-004-A2": (
        "semantic.normative_applicability_requirement/v1",
        "section_1_9",
        "classifies_as",
        "normative_applicability_requirement",
    ),
    "SEM-004-A3": (
        "structure.reference_entries_as_children/v1",
        "reference_entries",
        "relates_to",
        "structured child references of section 1.9",
    ),
    "SEM-004-A4": (
        "projection.reference_rows_not_product_requirements/v1",
        "ordinary_product_requirements_from_reference_rows",
        "equals",
        0,
    ),
    "SEM-004-A5": (
        "projection.no_recursive_standard_expansion/v1",
        "recursive_external_standard_expansion",
        "equals",
        False,
    ),
    "SEM-004-A6": (
        "finding.missing_linked_standard_warning/v1",
        "missing_linked_standard",
        "warns_without_mutation",
        "do not fabricate linked content",
    ),
    "FMT-001-A1": (
        SOURCE_DOCX_BILINGUAL_TEXTS_EVALUATOR_ID,
        "bilingual_source_texts",
        "preserves",
        ["Chinese source text", "English source text"],
    ),
    "FMT-001-A2": (
        STRUCTURE_BILINGUAL_COUNTERPART_EVALUATOR_ID,
        "bilingual_counterpart_relation",
        "relates_to",
        "one requirement identity",
    ),
    "FMT-001-A3": (
        PROJECTION_BILINGUAL_SINGLE_REQUIREMENT_EVALUATOR_ID,
        "requirement_count_per_bilingual_pair",
        "equals",
        1,
    ),
    "FMT-001-A4": (
        STRUCTURE_DOCUMENT_LANGUAGE_POLICY_EVALUATOR_ID,
        "language_effect_precedence",
        "equals",
        "Chinese prevails",
    ),
    "FMT-001-A5": (
        FINDING_BILINGUAL_MISMATCH_SAFETY_EVALUATOR_ID,
        "suspected_language_mismatch",
        "warns_without_mutation",
        "no machine-written replacement text",
    ),
    "FMT-003-A3": (
        "projection.pdf_annotation_noninterference/v1",
        "requirements_from_pdf_annotations",
        "equals",
        0,
    ),
    "FMT-003-A4": (
        "semantic.pdf_annotation_noninterference/v1",
        "annotation_value_conflict",
        "ignores",
        "no conflict event",
    ),
    "FMT-006-A2": (
        "structure.figure_section_reference/v1",
        "figure_2",
        "relates_to",
        "section 9.16.3 through body reference, figure number and caption",
    ),
    "FMT-006-A3": (
        "semantic.figure_section_requirement/v1",
        "section_9_16_3",
        "classifies_as",
        "main_requirement_with_acceptance_threshold_and_calculation_definition",
    ),
    "FMT-006-A4": (
        "projection.figure_ocr_not_requirements/v1",
        "requirements_from_figure_ocr",
        "equals",
        0,
    ),
    "FMT-006-A5": (
        "finding.unstructured_visual_requirement/v1",
        "critical_value_only_in_image",
        "warns_without_mutation",
        "unstructured_visual_requirement",
    ),
}
CONFIRMED_CASE_EVALUATOR_IDS = frozenset(
    contract[0] for contract in CONFIRMED_CASE_EVALUATOR_CONTRACTS.values()
)
PDF_DOCUMENT_REGION_EVALUATOR_IDS = frozenset(
    {
        PDF_REGISTERED_SOURCE_IDENTITY_EVALUATOR_ID,
        PDF_SINGLE_TOC_NOISE_BLOCK_EVALUATOR_ID,
        PDF_PAGE_NOISE_NOT_REQUIREMENTS_EVALUATOR_ID,
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_A1_CELL_PATTERN = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
_A1_RANGE_PATTERN = re.compile(
    r"^([A-Z]{1,3})([1-9][0-9]*)(?::([A-Z]{1,3})([1-9][0-9]*))?$"
)
_SECTION_POINTER_PATTERN = re.compile(
    r"^[1-9][0-9]{0,2}(?:\.(?:0|[1-9][0-9]{0,2})){0,7}$"
)
_PDF_DOCUMENT_REGION_POINTER_PATTERN = re.compile(
    r"^toc=(?P<toc_start>[1-9][0-9]{0,3})-(?P<toc_end>[1-9][0-9]{0,3});"
    r"body=(?P<body_start>[1-9][0-9]{0,3})-(?P<body_end>[1-9][0-9]{0,3})$"
)
_PDF_OPTION_SQUARE_MARKER = re.compile(r"^\s*\[\s*(?P<mark>[^\]\r\n]{0,4})\s*\]")
_PDF_OPTION_PAREN_MARKER = re.compile(r"^\s*[（(]\s*(?P<mark>[√✓✔xX×]?)\s*[）)]")
_PDF_OPTION_BOX_MARKER = re.compile(r"^\s*(?P<mark>[☐☑☒□])")
_PDF_CHECKED_OPTION_MARKS = frozenset({"√", "✓", "✔", "x", "X", "×", "☑", "☒"})
_PDF_UNCHECKED_OPTION_MARKS = frozenset({"", "☐", "□"})
EVALUATOR_CONTRACTS: dict[str, tuple[str, str, object]] = {
    SPREADSHEET_VISIBLE_WORKBOOK_FIDELITY_EVALUATOR_ID: (
        "visible_workbook_content",
        "preserves",
        "all visible content and structure",
    ),
    "source_ir.source_hash_dedup/v1": (
        "duplicate_document_identity",
        "deduplicates_by",
        "source_sha256",
    ),
    "source_ir.pdf_annotation_policy/v1": (
        "pdf_annotation_layer",
        "ignores",
        True,
    ),
    PDF_REGISTERED_SOURCE_IDENTITY_EVALUATOR_ID: (
        "raw_page_preview.source_file",
        "preserves",
        "registered PDF source bytes",
    ),
    PDF_REVIEWED_BODY_SEQUENCE_EVALUATOR_ID: (
        "document_body.reviewed_content_channels",
        "preserves",
        "human-reviewed page-bound text, image and table channel profiles",
    ),
    PDF_SINGLE_TOC_NOISE_BLOCK_EVALUATOR_ID: (
        "toc",
        "classifies_as",
        "single_toc_block",
    ),
    PDF_PAGE_NOISE_NOT_REQUIREMENTS_EVALUATOR_ID: (
        "requirements_or_context_from_classified_page_noise",
        "equals",
        0,
    ),
    STRUCTURE_SINGLE_ROW_EVALUATOR_ID: (
        "reviewed_source_scope.table_row_boundary_count",
        "equals",
        1,
    ),
    STRUCTURE_DOCX_SINGLE_ROW_EVALUATOR_ID: (
        "section_2_1_22.boundary",
        "preserves",
        "single source requirement block",
    ),
    PROJECTION_DOCX_SINGLE_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID: (
        "section_2_1_22.top_level_requirement_count",
        "equals",
        1,
    ),
    PROJECTION_DOCX_NO_SENTENCE_SPLIT_EVALUATOR_ID: (
        "sentence_or_parallel_verb_split",
        "equals",
        False,
    ),
    PROJECTION_MATERIAL_REQUIREMENT_COUNT_EVALUATOR_ID: (
        "material_requirement_count",
        "equals",
        3,
    ),
    PROJECTION_MATERIAL_SHARED_VALUES_EVALUATOR_ID: (
        "each_material_requirement.shared_values",
        "includes",
        ["standard", "acceptance_criteria"],
    ),
    STRUCTURE_EXPLICIT_TABLE_PEER_ITEMS_EVALUATOR_ID: (
        "peer_main_requirement_count",
        "equals",
        2,
    ),
    PROJECTION_EXPLICIT_PEER_SINGLE_REQUIREMENT_EVALUATOR_ID: (
        "item_6.parallel_actions_as_separate_requirements",
        "equals",
        False,
    ),
    STRUCTURE_DATA_ROWS_EVALUATOR_ID: (
        "reviewed_table_scope.data_row_boundary",
        "equals",
        "table row",
    ),
    PERFORMANCE_PARAMETER_ROWS_SEMANTIC_EVALUATOR_ID: (
        "explicit_performance_parameter_rows",
        "classifies_as",
        "product_requirement",
    ),
    REQUIREMENT_PER_PARAMETER_ROW_PROJECTION_EVALUATOR_ID: (
        "requirements_per_parameter_row",
        "equals",
        1,
    ),
    TABLE_ROW_SCHEDULE_INFORMATION_EVALUATOR_ID: (
        "two_day_value",
        "classifies_as",
        "schedule_information",
    ),
    TABLE_ROW_SAMPLE_PHASE_INFORMATION_EVALUATOR_ID: (
        "six_units_ots_value",
        "classifies_as",
        "sample_and_phase_information",
    ),
    SINGLE_COMPOUND_TABLE_ROW_PROJECTION_EVALUATOR_ID: (
        "top_level_requirement_count",
        "equals",
        1,
    ),
    TECHNICAL_NARRATIVE_DISPOSITION_EVALUATOR_ID: (
        "technical_principle_narrative",
        "classifies_as",
        "technical_explanation_or_background",
    ),
    SOURCE_ROLE_NOT_SEMANTIC_EXCLUSION_EVALUATOR_ID: (
        "source_role_as_semantic_exclusion",
        "equals",
        False,
    ),
    STRUCTURE_MERGED_APPLICABILITY_EVALUATOR_ID: (
        "scoped_vertical_merged_ranges",
        "applies_to",
        "all three scoped rows",
    ),
    ATTACHMENT_IDENTITY_EVALUATOR_ID: (
        "drawing_file",
        "preserves",
        "complete attachment with source and version identity",
    ),
    ATTACHMENT_DISPOSITION_EVALUATOR_ID: (
        "drawing_file",
        "classifies_as",
        "attachment_or_reference_requirement",
    ),
    ATTACHMENT_PROJECTION_EVALUATOR_ID: (
        "dimension_gdt_pin_or_bom_child_requirements",
        "equals",
        0,
    ),
    ATTACHMENT_WORKFLOW_EVALUATOR_ID: (
        "deep_drawing_recognition",
        "equals",
        "backlog_not_blocking_text_and_table_extraction",
    ),
    PDF_TABLE_CONTINUATION_EVALUATOR_ID: (
        "table_55",
        "relates_to",
        "complete child content block of 8.3.1",
    ),
    PDF_TABLE_CONTINUATION_V1_EVALUATOR_ID: (
        "table_55",
        "relates_to",
        "complete child content block of 8.3.1",
    ),
    PDF_TABLE_ROWS_NOT_REQUIREMENTS_EVALUATOR_ID: (
        "table_55.rows_as_requirement_list_items",
        "equals",
        False,
    ),
    PDF_TABLE_ROWS_NOT_REQUIREMENTS_V1_EVALUATOR_ID: (
        "table_55.rows_as_requirement_list_items",
        "equals",
        False,
    ),
    PDF_TABLE_PARAMETER_COUNT_EVALUATOR_ID: (
        "parameter_child_requirement_count",
        "equals",
        2,
    ),
    PDF_TABLE_NOTE_APPLIES_TO_EVALUATOR_ID: (
        "table_note",
        "applies_to",
        ["IStart row", "IStop row"],
    ),
    PDF_SECTION_CHILD_ASSERTION_EVALUATOR_ID: (
        "visual_quality_assertion",
        "relates_to",
        "third child assertion of 9.16.2",
    ),
    PDF_DUPLICATE_TABLE_LABEL_EVALUATOR_ID: (
        "suspected_cross_reference_or_duplicate_table_number",
        "warns_without_mutation",
        True,
    ),
    PDF_OPTION_RISK_SEMANTIC_EVALUATOR_ID: (
        "section_3_3.checked_risk_information",
        "classifies_as",
        "project_attribute_or_technical_risk",
    ),
    PDF_OPTION_SECTION_NO_REQUIREMENTS_EVALUATOR_ID: (
        "product_requirements_from_section_3_3",
        "equals",
        0,
    ),
    PDF_OPTION_REGULATION_SEMANTIC_EVALUATOR_ID: (
        "section_4_1",
        "classifies_as",
        "regulatory_certification_main_requirement",
    ),
    PDF_CHECKED_OPTION_APPLICABILITY_EVALUATOR_ID: (
        "section_4_1.applicable_checked_regions",
        "equals",
        ["中国", "东欧", "海湾地区（GCC）"],
    ),
    PDF_OPTION_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID: (
        "section_4_1.top_level_requirement_count",
        "equals",
        1,
    ),
    PDF_UNCHECKED_OPTION_NOT_EFFECTIVE_EVALUATOR_ID: (
        "unchecked_options_as_effective_requirements",
        "equals",
        False,
    ),
    PDF_OPTION_UNCERTAINTY_POLICY_EVALUATOR_ID: (
        "uncertain_check_state",
        "warns_without_mutation",
        "manual confirmation required",
    ),
    PDF_REVISION_HISTORY_SEMANTIC_EVALUATOR_ID: (
        "revision_rows",
        "classifies_as",
        "version_metadata",
    ),
    PDF_REVISION_HISTORY_NOT_REQUIREMENTS_EVALUATOR_ID: (
        "requirements_from_revision_rows",
        "equals",
        0,
    ),
    PDF_REVISION_HISTORY_NO_INFERENCE_EVALUATOR_ID: (
        "historic_value_inference_from_revision_summary",
        "equals",
        False,
    ),
    PDF_REVISION_SNAPSHOTS_DISTINCT_EVALUATOR_ID: (
        "two_uploaded_complete_versions",
        "keeps_distinct",
        "separate SourceIR snapshots with diff",
    ),
}
EVALUATOR_CONTRACTS.update(
    {
        evaluator_id: (target, operator, expected)
        for evaluator_id, target, operator, expected in (
            contract for contract in CONFIRMED_CASE_EVALUATOR_CONTRACTS.values()
        )
    }
)
SOURCE_ORACLE_EVALUATOR_FACT_KINDS = {
    STRUCTURE_SINGLE_ROW_EVALUATOR_ID: "xlsx_cell_range",
    STRUCTURE_DOCX_SINGLE_ROW_EVALUATOR_ID: "docx_table_row_scope",
    SOURCE_DOCX_BILINGUAL_TEXTS_EVALUATOR_ID: "docx_table_row_scope",
    STRUCTURE_BILINGUAL_COUNTERPART_EVALUATOR_ID: "docx_table_row_scope",
    PROJECTION_BILINGUAL_SINGLE_REQUIREMENT_EVALUATOR_ID: (
        "docx_table_row_scope"
    ),
    PROJECTION_DOCX_SINGLE_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID: (
        "docx_table_row_scope"
    ),
    PROJECTION_DOCX_NO_SENTENCE_SPLIT_EVALUATOR_ID: "docx_table_row_scope",
    PROJECTION_MATERIAL_REQUIREMENT_COUNT_EVALUATOR_ID: "xlsx_merge_group",
    PROJECTION_MATERIAL_SHARED_VALUES_EVALUATOR_ID: (
        "xlsx_material_requirement_scope"
    ),
    STRUCTURE_EXPLICIT_TABLE_PEER_ITEMS_EVALUATOR_ID: (
        "xlsx_explicit_peer_scope_group"
    ),
    PROJECTION_EXPLICIT_PEER_SINGLE_REQUIREMENT_EVALUATOR_ID: (
        "xlsx_explicit_peer_scope_group"
    ),
    STRUCTURE_DATA_ROWS_EVALUATOR_ID: "docx_table_group",
    PERFORMANCE_PARAMETER_ROWS_SEMANTIC_EVALUATOR_ID: "docx_table_group",
    REQUIREMENT_PER_PARAMETER_ROW_PROJECTION_EVALUATOR_ID: "docx_table_group",
    TABLE_ROW_SCHEDULE_INFORMATION_EVALUATOR_ID: "xlsx_cell_range",
    TABLE_ROW_SAMPLE_PHASE_INFORMATION_EVALUATOR_ID: "xlsx_cell_range",
    SINGLE_COMPOUND_TABLE_ROW_PROJECTION_EVALUATOR_ID: "xlsx_cell_range",
    TECHNICAL_NARRATIVE_DISPOSITION_EVALUATOR_ID: "docx_mixed_content_group",
    SOURCE_ROLE_NOT_SEMANTIC_EXCLUSION_EVALUATOR_ID: "docx_mixed_content_group",
    STRUCTURE_MERGED_APPLICABILITY_EVALUATOR_ID: "xlsx_merge_group",
    PDF_TABLE_CONTINUATION_EVALUATOR_ID: ("pdf_table_continuation_logical_grid"),
    PDF_TABLE_CONTINUATION_V1_EVALUATOR_ID: "pdf_table_continuation_group",
    PDF_TABLE_ROWS_NOT_REQUIREMENTS_EVALUATOR_ID: (
        "pdf_table_continuation_logical_grid"
    ),
    PDF_TABLE_ROWS_NOT_REQUIREMENTS_V1_EVALUATOR_ID: ("pdf_table_continuation_group"),
    PDF_TABLE_PARAMETER_COUNT_EVALUATOR_ID: "pdf_table_note_label_group",
    PDF_TABLE_NOTE_APPLIES_TO_EVALUATOR_ID: "pdf_table_note_label_group",
    PDF_SECTION_CHILD_ASSERTION_EVALUATOR_ID: "pdf_table_note_label_group",
    PDF_DUPLICATE_TABLE_LABEL_EVALUATOR_ID: "pdf_table_note_label_group",
    PDF_OPTION_RISK_SEMANTIC_EVALUATOR_ID: "pdf_option_state_group",
    PDF_OPTION_SECTION_NO_REQUIREMENTS_EVALUATOR_ID: "pdf_option_state_group",
    PDF_OPTION_REGULATION_SEMANTIC_EVALUATOR_ID: "pdf_option_state_group",
    PDF_CHECKED_OPTION_APPLICABILITY_EVALUATOR_ID: "pdf_option_state_group",
    PDF_OPTION_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID: "pdf_option_state_group",
    PDF_UNCHECKED_OPTION_NOT_EFFECTIVE_EVALUATOR_ID: "pdf_option_state_group",
    PDF_OPTION_UNCERTAINTY_POLICY_EVALUATOR_ID: "pdf_option_state_group",
    PDF_REVISION_HISTORY_SEMANTIC_EVALUATOR_ID: "pdf_revision_history_group",
    PDF_REVISION_HISTORY_NOT_REQUIREMENTS_EVALUATOR_ID: ("pdf_revision_history_group"),
    PDF_REVISION_HISTORY_NO_INFERENCE_EVALUATOR_ID: "pdf_revision_history_group",
    PDF_REVISION_SNAPSHOTS_DISTINCT_EVALUATOR_ID: "pdf_revision_history_group",
}
SOURCE_ORACLE_EVALUATOR_FACT_KINDS.update(
    {
        evaluator_id: "source_scope_group"
        for assertion_id, (
            evaluator_id,
            _target,
            _operator,
            _expected,
        ) in CONFIRMED_CASE_EVALUATOR_CONTRACTS.items()
        if assertion_id
        in {
            "ROLE-004-A5",
            "STR-006-A2",
            "STR-006-A3",
            "STR-007-A2",
            "STR-007-A3",
            "STR-007-A4",
            "STR-008-A2",
            "STR-008-A3",
            "STR-008-A4",
            "TAB-003-A2",
            "TAB-003-A3",
            "TAB-003-A4",
            "TAB-004-A2",
            "TAB-004-A3",
            "SEM-004-A2",
            "SEM-004-A3",
            "SEM-004-A4",
            "SEM-004-A5",
            "SEM-004-A6",
            "FMT-003-A3",
            "FMT-003-A4",
            "FMT-006-A2",
            "FMT-006-A3",
            "FMT-006-A4",
            "FMT-006-A5",
        }
    }
)


class ManifestValidationError(ValueError):
    pass


class CorpusPathError(ValueError):
    pass


class SourceIdentityChanged(RuntimeError):
    pass


def load_oracle_registry(path: Path = ORACLE_REGISTRY_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(
            "The SourceIR oracle registry is unavailable or invalid."
        ) from exc
    if not isinstance(payload, dict):
        raise ManifestValidationError(
            "SourceIR oracle registry root must be an object."
        )
    validate_oracle_registry(payload)
    return payload


def validate_oracle_registry(registry: dict[str, Any]) -> None:
    try:
        schema = json.loads(ORACLE_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(registry)
    except jsonschema.ValidationError as exc:
        raise ManifestValidationError(
            "SourceIR oracle registry failed strict JSON Schema validation."
        ) from exc
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise ManifestValidationError(
            "The SourceIR oracle schema is unavailable or invalid."
        ) from exc
    _validate_oracle_payload_integrity(registry)


def load_manifest(
    path: Path,
    *,
    oracle_path: Path | None = None,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ManifestValidationError("Manifest root must be an object.")
    registry = load_oracle_registry(oracle_path or path.with_name("oracles.json"))
    validate_manifest(payload, oracle_registry=registry)
    return payload


def validate_manifest(
    manifest: dict[str, Any],
    *,
    oracle_registry: dict[str, Any] | None = None,
) -> None:
    try:
        schema = json.loads(MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(manifest)
    except jsonschema.ValidationError as exc:
        raise ManifestValidationError(
            "Manifest failed strict JSON Schema validation."
        ) from exc
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise ManifestValidationError(
            "The repository extraction manifest schema is unavailable or invalid."
        ) from exc

    documents = manifest.get("documents")
    cases = manifest.get("cases")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError("Unsupported manifest schema_version.")
    if not isinstance(documents, dict) or not documents:
        raise ManifestValidationError("Manifest documents must be a non-empty object.")
    if set(documents) != EXPECTED_DOCUMENT_IDS:
        raise ManifestValidationError(
            "Manifest must contain exactly the 25 confirmed document IDs."
        )
    if not isinstance(cases, dict) or set(cases) != EXPECTED_CASE_IDS:
        raise ManifestValidationError(
            "Manifest must contain exactly the 29 confirmed case IDs."
        )

    assertion_ids: set[str] = set()
    for document_id, document in documents.items():
        if not isinstance(document, dict):
            raise ManifestValidationError(f"Document {document_id} must be an object.")
        if not str(document.get("document_ref") or "").startswith(EXTERNAL_PREFIX):
            raise ManifestValidationError(
                f"Document {document_id} has an invalid external reference."
            )
        if document.get("resolution_status") == "resolved":
            digest = str(document.get("source_sha256") or "")
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ManifestValidationError(
                    f"Document {document_id} has an invalid source hash."
                )

    duplicate_targets = {
        document_id: str(document.get("duplicate_of") or "")
        for document_id, document in documents.items()
        if document.get("duplicate_of")
    }
    for document_id, target_id in duplicate_targets.items():
        if target_id not in documents:
            raise ManifestValidationError(
                f"Document {document_id} has an unknown duplicate target."
            )
        if target_id == document_id:
            raise ManifestValidationError(
                f"Document {document_id} cannot duplicate itself."
            )
    _require_acyclic_duplicate_graph(duplicate_targets)

    referenced_document_ids: set[str] = set()
    for case_key, case in cases.items():
        if not isinstance(case, dict) or case.get("case_id") != case_key:
            raise ManifestValidationError(
                f"Case key and case_id differ for {case_key}."
            )
        document_ids = case.get("document_ids")
        allowed_layers = case.get("allowed_assertion_layers")
        locators = case.get("locators")
        assertions = case.get("assertions")
        if not isinstance(document_ids, list) or not document_ids:
            raise ManifestValidationError(f"Case {case_key} has no documents.")
        if len(document_ids) != len(set(document_ids)):
            raise ManifestValidationError(f"Case {case_key} repeats a document ID.")
        unknown_documents = sorted(set(document_ids) - set(documents))
        if unknown_documents:
            raise ManifestValidationError(
                f"Case {case_key} references unknown documents."
            )
        pdf_visual_pages = case.get("pdf_visual_page_numbers_by_document")
        if pdf_visual_pages is not None:
            if not isinstance(pdf_visual_pages, dict) or not pdf_visual_pages:
                raise ManifestValidationError(
                    f"Case {case_key} has an invalid PDF visual page scope."
                )
            if not set(pdf_visual_pages).issubset(document_ids):
                raise ManifestValidationError(
                    f"Case {case_key} has a PDF visual page scope outside its document set."
                )
            for visual_document_id, page_numbers in pdf_visual_pages.items():
                if documents[visual_document_id].get("format") != "pdf":
                    raise ManifestValidationError(
                        f"Case {case_key} scopes visual pages for a non-PDF document."
                    )
                if (
                    not isinstance(page_numbers, list)
                    or not page_numbers
                    or page_numbers != sorted(set(page_numbers))
                    or any(
                        isinstance(page_number, bool)
                        or not isinstance(page_number, int)
                        or page_number < 1
                        for page_number in page_numbers
                    )
                ):
                    raise ManifestValidationError(
                        f"Case {case_key} has an invalid ordered PDF visual page scope."
                    )
        referenced_document_ids.update(document_ids)
        if not isinstance(allowed_layers, list) or not allowed_layers:
            raise ManifestValidationError(
                f"Case {case_key} has no allowed assertion layers."
            )
        if not set(allowed_layers).issubset(LAYER_ORDER):
            raise ManifestValidationError(
                f"Case {case_key} has an unknown assertion layer."
            )
        if not isinstance(locators, list) or not locators:
            raise ManifestValidationError(f"Case {case_key} has no locators.")
        if any(
            locator.get("document_id") not in document_ids
            for locator in locators
            if isinstance(locator, dict)
        ):
            raise ManifestValidationError(
                f"Case {case_key} has a locator outside its document set."
            )
        if not isinstance(assertions, list) or not assertions:
            raise ManifestValidationError(f"Case {case_key} has no assertions.")
        for assertion in assertions:
            if not isinstance(assertion, dict):
                raise ManifestValidationError(
                    f"Case {case_key} has an invalid assertion."
                )
            assertion_id = str(assertion.get("assertion_id") or "")
            if assertion_id in assertion_ids:
                raise ManifestValidationError(
                    f"Duplicate assertion ID: {assertion_id}."
                )
            assertion_ids.add(assertion_id)
            if assertion.get("layer") not in allowed_layers:
                raise ManifestValidationError(
                    f"Assertion {assertion_id} is outside the case's allowed layers."
                )
            evaluation = assertion.get("evaluation")
            if evaluation is None:
                continue
            if not isinstance(evaluation, dict):
                raise ManifestValidationError(
                    f"Assertion {assertion_id} has an invalid evaluation declaration."
                )
            applicable_document_ids = evaluation.get("applicable_document_ids")
            if (
                not isinstance(applicable_document_ids, list)
                or not applicable_document_ids
                or not set(applicable_document_ids).issubset(document_ids)
            ):
                raise ManifestValidationError(
                    f"Assertion {assertion_id} evaluation is outside the case document set."
                )
            evaluator_id = str(evaluation.get("evaluator_id") or "")
            evaluator_layer = evaluator_id.partition(".")[0]
            if evaluator_layer != assertion.get("layer"):
                raise ManifestValidationError(
                    f"Assertion {assertion_id} evaluator does not match its layer."
                )
            if evaluator_id == "source_ir.source_hash_dedup/v1":
                applicable_set = set(applicable_document_ids)
                duplicate_ids = [
                    document_id
                    for document_id in applicable_document_ids
                    if documents[document_id].get("duplicate_of")
                ]
                if not duplicate_ids or any(
                    str(documents[document_id].get("duplicate_of") or "")
                    not in applicable_set
                    for document_id in duplicate_ids
                ):
                    raise ManifestValidationError(
                        f"Assertion {assertion_id} dedup scope must contain each duplicate and target."
                    )
            contract = EVALUATOR_CONTRACTS.get(evaluator_id)
            if contract is not None and contract != (
                assertion.get("target"),
                assertion.get("operator"),
                assertion.get("expected"),
            ):
                raise ManifestValidationError(
                    f"Assertion {assertion_id} does not match evaluator {evaluator_id}."
                )
            if evaluator_id in {
                SECTION_REQUIREMENT_SUBTYPE_EVALUATOR_ID,
                SECTION_REQUIREMENT_PROJECTION_EVALUATOR_ID,
                SECTION_MAIN_REQUIREMENT_EVALUATOR_ID,
                SECTION_MAIN_ROLLUP_EVALUATOR_ID,
            }:
                _validate_section_evaluator_binding(
                    case=case,
                    assertion=assertion,
                    evaluation=evaluation,
                )
            if evaluator_id in PDF_DOCUMENT_REGION_EVALUATOR_IDS:
                _validate_pdf_document_region_evaluator_binding(
                    case=case,
                    assertion=assertion,
                    evaluation=evaluation,
                )
            if evaluator_id == PDF_REVIEWED_BODY_SEQUENCE_EVALUATOR_ID:
                _validate_pdf_reviewed_body_sequence_binding(
                    case=case,
                    assertion=assertion,
                    evaluation=evaluation,
                    documents=documents,
                )
            if evaluator_id == SPREADSHEET_VISIBLE_WORKBOOK_FIDELITY_EVALUATOR_ID:
                _validate_spreadsheet_visible_workbook_fidelity_binding(
                    case=case,
                    assertion=assertion,
                    evaluation=evaluation,
                    documents=documents,
                )
    if referenced_document_ids != set(documents):
        raise ManifestValidationError(
            "Every confirmed manifest document must be referenced by at least one case."
        )
    resolved_oracle_registry = (
        load_oracle_registry() if oracle_registry is None else oracle_registry
    )
    _validate_oracle_bindings(manifest, resolved_oracle_registry)
    _validate_structure_source_oracle_bindings(manifest, resolved_oracle_registry)


def _validate_section_evaluator_binding(
    *,
    case: dict[str, Any],
    assertion: dict[str, Any],
    evaluation: dict[str, Any],
) -> None:
    evaluator_id = str(evaluation.get("evaluator_id") or "")
    applicable_document_ids = list(evaluation.get("applicable_document_ids") or [])
    if len(applicable_document_ids) != 1:
        raise ManifestValidationError(
            f"Assertion {assertion['assertion_id']} must scope its section evaluator to exactly one document."
        )
    document_id = applicable_document_ids[0]
    locators = [
        locator
        for locator in case.get("locators", [])
        if isinstance(locator, dict)
        and locator.get("document_id") == document_id
        and locator.get("kind") == "section"
    ]
    if len(locators) != 1:
        raise ManifestValidationError(
            f"Assertion {assertion['assertion_id']} requires exactly one scoped section locator."
        )
    pointer = str(locators[0].get("pointer") or "")
    if _SECTION_POINTER_PATTERN.fullmatch(pointer) is None:
        raise ManifestValidationError(
            f"Assertion {assertion['assertion_id']} has an invalid section locator."
        )
    target = f"section_{pointer.replace('.', '_')}"
    if evaluator_id == SECTION_REQUIREMENT_SUBTYPE_EVALUATOR_ID:
        expected_values = {item.value for item in RequirementSubtype}
        matches = bool(
            assertion.get("target") == target
            and assertion.get("operator") == "classifies_as"
            and assertion.get("expected") in expected_values
        )
    elif evaluator_id == SECTION_REQUIREMENT_PROJECTION_EVALUATOR_ID:
        matches = bool(
            assertion.get("target") == f"{target}_as_background"
            and assertion.get("operator") == "equals"
            and assertion.get("expected") is False
        )
    else:
        matches = bool(
            assertion.get("target") == target
            and assertion.get("operator") == "classifies_as"
            and assertion.get("expected") == "main_requirement"
        )
    if not matches:
        raise ManifestValidationError(
            f"Assertion {assertion['assertion_id']} does not match its scoped section locator and evaluator contract."
        )


def _validate_pdf_document_region_evaluator_binding(
    *,
    case: dict[str, Any],
    assertion: dict[str, Any],
    evaluation: dict[str, Any],
) -> None:
    applicable_document_ids = list(evaluation.get("applicable_document_ids") or [])
    if set(applicable_document_ids) != set(case.get("document_ids") or []):
        raise ManifestValidationError(
            f"Assertion {assertion['assertion_id']} must evaluate every case document."
        )
    if evaluation.get("evaluator_id") == PDF_SINGLE_TOC_NOISE_BLOCK_EVALUATOR_ID:
        expected_fingerprints = evaluation.get(
            "expected_candidate_fingerprints_by_document"
        )
        if not isinstance(expected_fingerprints, dict) or set(
            expected_fingerprints
        ) != set(applicable_document_ids):
            raise ManifestValidationError(
                f"Assertion {assertion['assertion_id']} must bind one reviewed TOC candidate fingerprint per document."
            )
    for document_id in applicable_document_ids:
        locators = [
            locator
            for locator in case.get("locators", [])
            if isinstance(locator, dict)
            and locator.get("document_id") == document_id
            and locator.get("kind") == "page_region"
        ]
        if len(locators) != 1:
            raise ManifestValidationError(
                f"Assertion {assertion['assertion_id']} requires one page-region locator per document."
            )
        _parse_pdf_document_region_pointer(
            str(locators[0].get("pointer") or ""),
            error_type=ManifestValidationError,
        )


def _validate_pdf_reviewed_body_sequence_binding(
    *,
    case: dict[str, Any],
    assertion: dict[str, Any],
    evaluation: dict[str, Any],
    documents: dict[str, dict[str, Any]],
) -> None:
    applicable_document_ids = list(evaluation.get("applicable_document_ids") or [])
    profiles = evaluation.get("reviewed_body_profiles_by_document")
    if (
        set(applicable_document_ids) != set(case.get("document_ids") or [])
        or not isinstance(profiles, dict)
        or set(profiles) != set(applicable_document_ids)
    ):
        raise ManifestValidationError(
            f"Assertion {assertion['assertion_id']} must bind one reviewed body profile per PDF document."
        )

    scopes = _pdf_document_region_scopes(case)
    for document_id in applicable_document_ids:
        document = documents[document_id]
        profile = profiles[document_id]
        scope = scopes[document_id]
        page_start = int(profile["body_page_start"])
        page_end = int(profile["body_page_end"])
        if (
            document.get("format") != "pdf"
            or profile.get("source_sha256") != document.get("source_sha256")
            or page_start != scope["body_start"]
            or page_end != scope["body_end"]
            or page_start > page_end
        ):
            raise ManifestValidationError(
                f"Assertion {assertion['assertion_id']} has an invalid source-bound body profile."
            )
        if profile.get("status") == "PENDING":
            continue

        page_records = list(profile.get("pages") or [])
        expected_page_numbers = list(range(page_start, page_end + 1))
        if [int(item["page_number"]) for item in page_records] != expected_page_numbers:
            raise ManifestValidationError(
                f"Assertion {assertion['assertion_id']} reviewed pages must be complete and ordered."
            )
        for page in page_records:
            table_count = int(page["expected_table_count"])
            row_count = int(page["expected_table_row_count"])
            cell_count = int(page["expected_table_cell_count"])
            merged_count = int(page["expected_merged_region_count"])
            body_line_count = int(page["expected_body_line_count"])
            if (
                (table_count == 0 and any((row_count, cell_count, merged_count)))
                or (
                    table_count > 0
                    and (row_count < table_count or cell_count < table_count)
                )
                or merged_count > cell_count
                or body_line_count < 0
            ):
                raise ManifestValidationError(
                    f"Assertion {assertion['assertion_id']} has inconsistent reviewed page counts."
                )


def _validate_spreadsheet_visible_workbook_fidelity_binding(
    *,
    case: dict[str, Any],
    assertion: dict[str, Any],
    evaluation: dict[str, Any],
    documents: dict[str, dict[str, Any]],
) -> None:
    applicable_document_ids = list(evaluation.get("applicable_document_ids") or [])
    profiles = evaluation.get("workbook_profiles_by_document")
    if (
        set(applicable_document_ids) != set(case.get("document_ids") or [])
        or not isinstance(profiles, dict)
        or set(profiles) != set(applicable_document_ids)
    ):
        raise ManifestValidationError(
            f"Assertion {assertion['assertion_id']} must bind one workbook profile per document."
        )
    for document_id in applicable_document_ids:
        document = documents[document_id]
        profile = profiles[document_id]
        source_format = str(document.get("format") or "")
        if (
            source_format not in {"xlsx", "xlsb"}
            or profile.get("source_format") != source_format
        ):
            raise ManifestValidationError(
                f"Assertion {assertion['assertion_id']} has an invalid source-bound workbook profile."
            )
        if source_format == "xlsx":
            if (
                profile.get("profile_kind") != "native_xlsx_strict"
                or profile.get("page_render_policy")
                != "external_source_bound_profile"
                or "derived_source_sha256" in profile
            ):
                raise ManifestValidationError(
                    f"Assertion {assertion['assertion_id']} has an invalid native XLSX profile."
                )
        elif (
            profile.get("profile_kind")
            != "xlsb_direct_visual_plus_derived_structure"
            or profile.get("page_render_policy")
            != "source_ir_original_page_assets"
            or not isinstance(profile.get("derived_source_sha256"), str)
        ):
            raise ManifestValidationError(
                f"Assertion {assertion['assertion_id']} has an invalid XLSB profile."
            )
        if (
            profile.get("review_status") == "AI_SILVER_COMPLETE"
            and profile.get("reviewed_page_count") != profile.get("page_count")
        ):
            raise ManifestValidationError(
                f"Assertion {assertion['assertion_id']} has incomplete AI Silver coverage."
            )


def _parse_pdf_document_region_pointer(
    pointer: str,
    *,
    error_type: type[Exception] = ValueError,
) -> dict[str, int]:
    match = _PDF_DOCUMENT_REGION_POINTER_PATTERN.fullmatch(str(pointer or ""))
    if match is None:
        raise error_type("Invalid PDF document-region locator.")
    scope = {key: int(value) for key, value in match.groupdict().items()}
    if not (
        scope["toc_start"]
        <= scope["toc_end"]
        < scope["body_start"]
        <= scope["body_end"]
    ):
        raise error_type("Invalid PDF document-region page ordering.")
    return scope


def _require_acyclic_duplicate_graph(duplicate_targets: dict[str, str]) -> None:
    completed: set[str] = set()
    for start in duplicate_targets:
        if start in completed:
            continue
        current = start
        path: set[str] = set()
        while current in duplicate_targets:
            if current in path:
                raise ManifestValidationError(
                    "Manifest duplicate_of relationships must be acyclic."
                )
            if current in completed:
                break
            path.add(current)
            current = duplicate_targets[current]
        completed.update(path)


def _validate_oracle_payload_integrity(registry: dict[str, Any]) -> None:
    if registry.get("schema_version") != ORACLE_REGISTRY_SCHEMA_VERSION:
        raise ManifestValidationError(
            "Unsupported SourceIR oracle registry schema_version."
        )
    if registry.get("fingerprint_algorithm") != ORACLE_FINGERPRINT_ALGORITHM:
        raise ManifestValidationError(
            "Unsupported SourceIR oracle fingerprint algorithm."
        )
    oracles = registry.get("oracles")
    if not isinstance(oracles, dict) or not oracles:
        raise ManifestValidationError("SourceIR oracle registry must contain oracles.")
    for oracle_id, oracle in oracles.items():
        if not isinstance(oracle, dict) or oracle.get("oracle_id") != oracle_id:
            raise ManifestValidationError(
                "SourceIR oracle key and oracle_id must match."
            )
        fact = oracle.get("fact")
        if not isinstance(fact, dict):
            raise ManifestValidationError(f"SourceIR oracle {oracle_id} has no fact.")
        kind = fact.get("kind")
        if kind == "xlsx_cell_range":
            _validate_xlsx_cell_range_oracle(oracle_id, fact)
        elif kind == "docx_table_group":
            _validate_docx_table_group_oracle(oracle_id, fact)
        elif kind == "docx_table_row_scope":
            _validate_docx_table_row_scope_oracle(oracle_id, fact)
        elif kind == "docx_mixed_content_group":
            _validate_docx_mixed_content_oracle(oracle_id, fact)
        elif kind == "xlsx_merge_group":
            _validate_xlsx_merge_group_oracle(oracle_id, fact)
        elif kind == "xlsx_material_requirement_scope":
            _validate_xlsx_material_requirement_scope_oracle(
                oracle_id,
                fact,
            )
        elif kind == "xlsx_explicit_peer_scope_group":
            _validate_xlsx_explicit_peer_scope_group_oracle(
                oracle_id,
                fact,
            )
        elif kind == "pdf_table_continuation_group":
            _validate_pdf_table_continuation_oracle(oracle_id, fact)
        elif kind == "pdf_table_continuation_logical_grid":
            _validate_pdf_table_continuation_logical_grid_oracle(
                oracle_id,
                fact,
            )
        elif kind == "pdf_table_note_label_group":
            _validate_pdf_table_note_label_oracle(oracle_id, fact)
        elif kind == "pdf_option_state_group":
            _validate_pdf_option_state_oracle(oracle_id, fact)
        elif kind == "pdf_revision_history_group":
            _validate_pdf_revision_history_oracle(oracle_id, fact)
        elif kind == "source_scope_group":
            _validate_source_scope_group_oracle(oracle_id, fact)
        else:
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} has an unsupported fact kind."
            )
        composite_document_ids = oracle.get("document_ids")
        if kind in {"pdf_revision_history_group", "source_scope_group"}:
            fact_document_ids = [
                str(document["document_id"])
                for document in (
                    fact["documents"]
                    if kind == "pdf_revision_history_group"
                    else fact["scopes"]
                )
            ]
            if (
                not isinstance(composite_document_ids, list)
                or fact_document_ids != composite_document_ids
            ):
                raise ManifestValidationError(
                    f"SourceIR oracle {oracle_id} revision documents do not match its source bindings."
                )
        elif composite_document_ids is not None:
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} cannot use multi-document bindings."
            )
        if (
            oracle.get("evaluator_id") in SOURCE_SCOPE_OWNER_EVALUATOR_IDS
            or kind
            in {
                "docx_mixed_content_group",
                "xlsx_explicit_peer_scope_group",
            }
        ):
            _validate_ai_reviewed_scope_provenance(oracle_id, oracle)
    for oracle_id, oracle in oracles.items():
        fact = oracle["fact"]
        if fact["kind"] != "docx_mixed_content_group":
            continue
        component_oracle_id = str(fact["parameter_table_oracle_id"])
        component = oracles.get(component_oracle_id)
        if (
            not isinstance(component, dict)
            or component.get("evaluator_id") not in ORACLE_EVALUATOR_IDS
            or component.get("document_id") != oracle.get("document_id")
            or component.get("source_sha256") != oracle.get("source_sha256")
            or (component.get("fact") or {}).get("kind") != "docx_table_group"
            or int(fact["expected_parameter_table_count"])
            != int(component["fact"]["expected_table_count"])
            or str(fact["parameter_table_group_fingerprint"])
            != str(component["fact"]["group_fingerprint"])
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} has an invalid parameter-table component binding."
            )
    for oracle_id, oracle in oracles.items():
        fact = oracle["fact"]
        if fact["kind"] != "xlsx_material_requirement_scope":
            continue
        component_oracle_id = str(fact["source_oracle_id"])
        component = oracles.get(component_oracle_id)
        component_fact = (
            component.get("fact") if isinstance(component, dict) else None
        )
        row_expectations = list(fact["row_expectations"])
        component_identity_records = (
            list(component_fact["row_identity"]["cells"])
            if isinstance(component_fact, dict)
            and component_fact.get("kind") == "xlsx_merge_group"
            else []
        )
        role_merge_records = [
            {
                "anchor": str(binding["anchor_cell_range"]),
                "row_span": (
                    _parse_a1_range(str(binding["merge_range"]))[2]
                    - _parse_a1_range(str(binding["merge_range"]))[0]
                    + 1
                ),
                "column_span": (
                    _parse_a1_range(str(binding["merge_range"]))[3]
                    - _parse_a1_range(str(binding["merge_range"]))[1]
                    + 1
                ),
                "anchor_raw_sha256": str(binding["anchor_raw_sha256"]),
            }
            for binding in fact["role_bindings"]
        ]
        component_merge_records = (
            list(component_fact["merges"])
            if isinstance(component_fact, dict)
            and component_fact.get("kind") == "xlsx_merge_group"
            else []
        )
        if (
            not isinstance(component, dict)
            or component.get("evaluator_id") not in ORACLE_EVALUATOR_IDS
            or component.get("document_id") != oracle.get("document_id")
            or component.get("source_sha256") != oracle.get("source_sha256")
            or not isinstance(component_fact, dict)
            or component_fact.get("kind") != "xlsx_merge_group"
            or component_fact.get("table_locator") != fact["table_locator"]
            or str(component_fact.get("group_fingerprint"))
            != str(fact["source_merge_group_fingerprint"])
            or [
                {
                    "identity_cell_range": str(record["cell_range"]),
                    "identity_raw_sha256": str(record["raw_sha256"]),
                }
                for record in component_identity_records
            ]
            != row_expectations
            or any(
                merge_record not in component_merge_records
                for merge_record in role_merge_records
            )
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} has an invalid XLSX merge-group component binding."
            )


def _validate_ai_reviewed_scope_provenance(
    oracle_id: str,
    oracle: dict[str, Any],
) -> None:
    provenance = oracle["review_provenance"]
    reviewed_pages = [int(value) for value in provenance["reviewed_page_numbers"]]
    page_hashes = {
        int(page_number): str(page_sha256)
        for page_number, page_sha256 in provenance[
            "page_image_sha256_by_page"
        ].items()
    }
    if (
        provenance["label"] != "AI_REVIEWED_SILVER"
        or provenance["reviewer_role"] != "AI_REVIEWER"
        or bool(provenance["is_human_gold"])
        or bool(provenance["is_serving_activation_gate"])
        or reviewed_pages != sorted(set(reviewed_pages))
        or set(page_hashes) != set(reviewed_pages)
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} has invalid AI-reviewed scope provenance."
        )
    review_method = str(provenance["review_method"])
    if review_method == "DOCX_RENDER_AND_OOXML_CROSS_CHECK":
        if (
            "reviewed_source_sha256" in provenance
            or "reviewed_sheet_part" in provenance
            or "reviewed_sheet_part_sha256" in provenance
            or int(provenance["target_page_number"]) not in reviewed_pages
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} has invalid DOCX review provenance."
            )
        return
    if review_method != "XLSX_LIBREOFFICE_RENDER_AND_OPENXML_CROSS_CHECK":
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} has an unsupported review method."
        )
    fact = oracle["fact"]
    fact_kind = str(fact.get("kind") or "")
    if fact_kind == "xlsx_material_requirement_scope":
        expected_pages = {
            int(fact["identity_rendered_page_number"]),
            *(
                int(binding["rendered_page_number"])
                for binding in fact["role_bindings"]
            ),
        }
        expected_sheet_part = None
        expected_sheet_part_sha256 = None
    elif fact_kind == "xlsx_explicit_peer_scope_group":
        scopes = list(fact["scopes"])
        expected_pages = {
            int(scope["rendered_page_number"]) for scope in scopes
        }
        expected_sheet_part = str(scopes[0]["reviewed_sheet_part"])
        expected_sheet_part_sha256 = str(
            scopes[0]["reviewed_sheet_part_sha256"]
        )
    else:
        expected_pages = set()
        expected_sheet_part = None
        expected_sheet_part_sha256 = None
    if (
        fact_kind
        not in {
            "xlsx_material_requirement_scope",
            "xlsx_explicit_peer_scope_group",
        }
        or str(provenance.get("reviewed_source_sha256") or "")
        != str(oracle.get("source_sha256") or "")
        or reviewed_pages != sorted(expected_pages)
        or not str(provenance.get("reviewed_sheet_part") or "").startswith(
            "xl/worksheets/sheet"
        )
        or len(str(provenance.get("reviewed_sheet_part_sha256") or "")) != 64
        or (
            expected_sheet_part is not None
            and str(provenance["reviewed_sheet_part"]) != expected_sheet_part
        )
        or (
            expected_sheet_part_sha256 is not None
            and str(provenance["reviewed_sheet_part_sha256"])
            != expected_sheet_part_sha256
        )
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} has invalid XLSX AI-reviewed scope provenance."
        )


def _validate_source_scope_group_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    scopes = list(fact["scopes"])
    document_ids = [str(scope["document_id"]) for scope in scopes]
    if (
        int(fact["expected_document_count"]) != len(scopes)
        or document_ids != sorted(document_ids)
        or len(document_ids) != len(set(document_ids))
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} source scopes must be complete, unique and ordered."
        )
    for scope in scopes:
        selector = scope["selector"]
        if selector["format"] == "pdf":
            page_numbers = [int(value) for value in selector["page_numbers"]]
            if page_numbers != sorted(page_numbers):
                raise ManifestValidationError(
                    f"SourceIR oracle {oracle_id} PDF pages must be unique and ordered."
                )
            channel_mode = str(selector["channel_mode"])
            if channel_mode == "NATIVE" and (
                int(scope["expected_visual_observation_count"])
                or int(scope["expected_visual_table_count"])
            ):
                raise ManifestValidationError(
                    f"SourceIR oracle {oracle_id} native PDF scope cannot expect visual records."
                )
            if channel_mode == "VISUAL" and (
                int(scope["expected_native_node_count"])
                or int(scope["expected_native_table_count"])
                or int(scope["expected_asset_count"])
            ):
                raise ManifestValidationError(
                    f"SourceIR oracle {oracle_id} visual PDF scope cannot expect native records."
                )
        else:
            if int(scope["expected_visual_observation_count"]) or int(
                scope["expected_visual_table_count"]
            ):
                raise ManifestValidationError(
                    f"SourceIR oracle {oracle_id} XLSX scope cannot expect visual PDF records."
                )
            if selector["mode"] == "RANGES":
                ranges = list(selector["ranges"])
                range_keys = [
                    (
                        str(item["sheet"]),
                        _parse_a1_range(str(item["cell_range"])),
                    )
                    for item in ranges
                ]
                if range_keys != sorted(range_keys) or len(range_keys) != len(
                    set(range_keys)
                ):
                    raise ManifestValidationError(
                        f"SourceIR oracle {oracle_id} XLSX ranges must be unique and ordered."
                    )
    if _fingerprint_source_scope_group(scopes) != fact["group_fingerprint"]:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} source-scope group fingerprint is invalid."
        )


def _validate_xlsx_cell_range_oracle(oracle_id: str, fact: dict[str, Any]) -> None:
    locator = fact["locator"]
    target_bounds = _parse_a1_range(str(locator["cell_range"]))
    cells = fact["cells"]
    if int(fact["expected_cell_count"]) != len(cells):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} cell count is inconsistent."
        )
    coordinates = [str(cell["cell_range"]) for cell in cells]
    if len(coordinates) != len(set(coordinates)):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} repeats a cell locator."
        )
    if any(
        not _bounds_contain(target_bounds, _parse_a1_range(coordinate))
        for coordinate in coordinates
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} has an out-of-range cell."
        )
    expected = _fingerprint_xlsx_cell_records(cells)
    if expected != fact["range_fingerprint"]:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} cell fingerprint is invalid."
        )


def _validate_docx_table_group_oracle(oracle_id: str, fact: dict[str, Any]) -> None:
    tables = fact["tables"]
    if int(fact["expected_table_count"]) != len(tables):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} table count is inconsistent."
        )
    locator_keys = [
        (
            int(table["locator"]["body_element_index"]),
            int(table["locator"]["table_index"]),
        )
        for table in tables
    ]
    if len(locator_keys) != len(set(locator_keys)):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} repeats a table locator."
        )
    if any(
        int(table["data_row_count"]) != int(table["row_count"]) - 1 for table in tables
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} has an invalid data-row count."
        )
    if int(fact["expected_data_row_count"]) != sum(
        int(table["data_row_count"]) for table in tables
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} data-row total is inconsistent."
        )
    if int(fact["expected_cell_count"]) != sum(
        int(table["cell_count"]) for table in tables
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} cell total is inconsistent."
        )
    if _fingerprint_docx_table_records(tables) != fact["group_fingerprint"]:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} table fingerprint is invalid."
        )


def _validate_docx_mixed_content_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    narratives = list(fact["narratives"])
    narrative_keys = [
        (
            int(record["locator"]["body_element_index"]),
            str(record["kind"]),
        )
        for record in narratives
    ]
    if (
        int(fact["expected_narrative_count"]) != len(narratives)
        or narrative_keys != sorted(narrative_keys)
        or len(narrative_keys) != len(set(narrative_keys))
        or _fingerprint_docx_mixed_content_group(
            narratives=narratives,
            parameter_table_oracle_id=str(fact["parameter_table_oracle_id"]),
            expected_parameter_table_count=int(
                fact["expected_parameter_table_count"]
            ),
            parameter_table_group_fingerprint=str(
                fact["parameter_table_group_fingerprint"]
            ),
        )
        != fact["mixed_fingerprint"]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} DOCX mixed-content scope is invalid."
        )


def _validate_docx_table_row_scope_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    cells = list(fact["cells"])
    anchor_cell_count = int(fact["anchor_cell_count"])
    logical_slot_count = int(fact["logical_slot_count"])
    if anchor_cell_count != len(cells):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} row cell count is inconsistent."
        )
    ordered_cells = sorted(cells, key=lambda cell: int(cell["column"]))
    columns = [int(cell["column"]) for cell in ordered_cells]
    covered_columns = [
        column
        for cell in ordered_cells
        for column in range(
            int(cell["column"]),
            int(cell["column"]) + int(cell["column_span"]),
        )
    ]
    if (
        columns != sorted(set(columns))
        or covered_columns != list(range(logical_slot_count))
        or any(int(cell["row_span"]) != 1 for cell in cells)
        or _fingerprint_docx_table_row_cells(cells) != fact["row_fingerprint"]
        or _fingerprint_docx_table_row_scope(fact) != fact["scope_fingerprint"]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} row scope is inconsistent."
        )


def _validate_xlsx_merge_group_oracle(oracle_id: str, fact: dict[str, Any]) -> None:
    table_bounds = _parse_a1_range(str(fact["table_locator"]["cell_range"]))
    scope_bounds = _parse_a1_range(str(fact["scope_range"]))
    if not _bounds_contain(table_bounds, scope_bounds):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} merge scope escapes its table."
        )
    covered_row_count = int(fact["covered_row_count"])
    if covered_row_count != scope_bounds[2] - scope_bounds[0] + 1:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} covered-row count is invalid."
        )
    merges = fact["merges"]
    if int(fact["expected_merge_count"]) != len(merges):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} merge count is inconsistent."
        )
    anchors = [str(item["anchor"]) for item in merges]
    if len(anchors) != len(set(anchors)):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} repeats a merge anchor."
        )
    for item in merges:
        anchor_bounds = _parse_a1_range(str(item["anchor"]))
        row_span = int(item["row_span"])
        column_span = int(item["column_span"])
        merged_bounds = (
            anchor_bounds[0],
            anchor_bounds[1],
            anchor_bounds[0] + row_span - 1,
            anchor_bounds[1] + column_span - 1,
        )
        if (
            row_span != covered_row_count
            or merged_bounds[0] != scope_bounds[0]
            or merged_bounds[2] != scope_bounds[2]
            or not _bounds_contain(scope_bounds, merged_bounds)
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} has an out-of-scope merge."
            )
    if _fingerprint_xlsx_merge_records(merges) != fact["merge_fingerprint"]:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} merge fingerprint is invalid."
        )

    row_identity = fact["row_identity"]
    identity_bounds = _parse_a1_range(str(row_identity["cell_range"]))
    if not _bounds_contain(table_bounds, identity_bounds):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} row identity escapes its table."
        )
    identity_cells = row_identity["cells"]
    if int(row_identity["expected_cell_count"]) != len(identity_cells):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} row-identity count is inconsistent."
        )
    identity_coordinates = [str(cell["cell_range"]) for cell in identity_cells]
    if len(identity_coordinates) != len(set(identity_coordinates)) or any(
        not _bounds_contain(identity_bounds, _parse_a1_range(coordinate))
        for coordinate in identity_coordinates
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} has an invalid row-identity locator."
        )
    identity_fingerprint = _fingerprint_xlsx_identity_records(identity_cells)
    if identity_fingerprint != row_identity["fingerprint"]:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} row-identity fingerprint is invalid."
        )
    expected_group = _fingerprint_xlsx_merge_group(merges, identity_cells)
    if expected_group != fact["group_fingerprint"]:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} group fingerprint is invalid."
        )


def _validate_xlsx_material_requirement_scope_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    table_bounds = _parse_a1_range(str(fact["table_locator"]["cell_range"]))
    row_scope = _parse_a1_range(str(fact["full_row_scope"]))
    expected_requirement_count = int(fact["expected_requirement_count"])
    if (
        not _bounds_contain(table_bounds, row_scope)
        or row_scope[1] != table_bounds[1]
        or row_scope[3] != table_bounds[3]
        or row_scope[2] - row_scope[0] + 1 != expected_requirement_count
        or row_scope[0] != table_bounds[0] + 1
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} material row scope is invalid."
        )

    row_expectations = list(fact["row_expectations"])
    row_coordinates = [
        str(record["identity_cell_range"]) for record in row_expectations
    ]
    expected_row_coordinates = [
        f"{_column_name(row_scope[1])}{row_number}"
        for row_number in range(row_scope[0], row_scope[2] + 1)
    ]
    if (
        len(row_expectations) != expected_requirement_count
        or row_coordinates != expected_row_coordinates
        or len(row_coordinates) != len(set(row_coordinates))
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} material identities are incomplete or unordered."
        )

    role_bindings = list(fact["role_bindings"])
    roles = [str(binding["role"]) for binding in role_bindings]
    header_coordinates = [
        str(binding["header_cell_range"]) for binding in role_bindings
    ]
    anchor_coordinates = [
        str(binding["anchor_cell_range"]) for binding in role_bindings
    ]
    merge_ranges = [str(binding["merge_range"]) for binding in role_bindings]
    if (
        roles != ["STANDARD", "ACCEPTANCE_CRITERIA"]
        or len(set(header_coordinates)) != len(role_bindings)
        or len(set(anchor_coordinates)) != len(role_bindings)
        or len(set(merge_ranges)) != len(role_bindings)
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} material roles are incomplete or unordered."
        )
    observed_applies_to_count = 0
    for binding in role_bindings:
        header_bounds = _parse_a1_range(str(binding["header_cell_range"]))
        anchor_bounds = _parse_a1_range(str(binding["anchor_cell_range"]))
        merge_bounds = _parse_a1_range(str(binding["merge_range"]))
        expected_binding_applies_to_count = int(
            binding["expected_applies_to_count"]
        )
        if (
            header_bounds[0] != table_bounds[0]
            or header_bounds[2] != table_bounds[0]
            or anchor_bounds[0] != row_scope[0]
            or anchor_bounds[2] != row_scope[0]
            or header_bounds[1] != anchor_bounds[1]
            or header_bounds[3] != anchor_bounds[3]
            or merge_bounds
            != (
                row_scope[0],
                anchor_bounds[1],
                row_scope[2],
                anchor_bounds[1],
            )
            or not _bounds_contain(table_bounds, header_bounds)
            or not _bounds_contain(table_bounds, merge_bounds)
            or expected_binding_applies_to_count
            != expected_requirement_count
            or _fingerprint_xlsx_material_role_binding(binding)
            != binding["binding_fingerprint"]
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} has an invalid material role binding."
            )
        observed_applies_to_count += expected_binding_applies_to_count
    if (
        observed_applies_to_count
        != int(fact["expected_role_applies_to_count"])
        or observed_applies_to_count
        != len(role_bindings) * expected_requirement_count
        or _fingerprint_xlsx_material_requirement_scope(fact)
        != fact["scope_fingerprint"]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} material scope fingerprint is invalid."
        )


def _validate_xlsx_explicit_peer_scope_group_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    scopes = list(fact["scopes"])
    expected_scope_count = int(fact["expected_scope_count"])
    expected_items_per_scope = int(fact["expected_items_per_scope"])
    expected_total_item_count = int(fact["expected_total_item_count"])
    expected_ordinals = [int(value) for value in fact["expected_peer_ordinals"]]
    if (
        expected_scope_count != len(scopes)
        or expected_scope_count < 1
        or expected_items_per_scope != len(expected_ordinals)
        or expected_items_per_scope < 2
        or expected_total_item_count
        != expected_scope_count * expected_items_per_scope
        or expected_ordinals
        != list(range(expected_ordinals[0], expected_ordinals[0] + len(expected_ordinals)))
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} explicit-peer totals are inconsistent."
        )

    scope_keys: list[tuple[str, str, str]] = []
    rendered_pages: list[int] = []
    table_source_node_ids: list[str] = []
    cell_ids: list[str] = []
    sheet_parts: list[str] = []
    for scope in scopes:
        locator = scope["table_locator"]
        table_bounds = _parse_a1_range(str(locator["cell_range"]))
        cell_range = str(scope["cell_range"])
        cell_bounds = _parse_a1_range(cell_range)
        items = list(scope["items"])
        ordinals = [int(item["ordinal"]) for item in items]
        intervals = [
            (int(item["start_offset"]), int(item["end_offset"]))
            for item in items
        ]
        exact_cover = bool(
            intervals
            and intervals[0][0] == 0
            and all(start < end for start, end in intervals)
            and all(
                intervals[index - 1][1] == intervals[index][0]
                for index in range(1, len(intervals))
            )
            and intervals[-1][1] == int(scope["raw_text_length"])
        )
        scope_key = (
            str(locator["workbook"]),
            str(locator["sheet"]),
            cell_range,
        )
        if (
            locator["format"] != "xlsx"
            or cell_bounds[0] != cell_bounds[2]
            or cell_bounds[1] != cell_bounds[3]
            or not _bounds_contain(table_bounds, cell_bounds)
            or str(scope["reviewed_sheet_part"])
            == ""
            or len(items) != expected_items_per_scope
            or ordinals != expected_ordinals
            or not exact_cover
            or _fingerprint_xlsx_explicit_peer_scope(scope)
            != scope["scope_fingerprint"]
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} has an invalid explicit-peer scope."
            )
        scope_keys.append(scope_key)
        rendered_pages.append(int(scope["rendered_page_number"]))
        table_source_node_ids.append(str(scope["table_source_node_id"]))
        cell_ids.append(str(scope["cell_id"]))
        sheet_parts.append(str(scope["reviewed_sheet_part"]))

    if (
        scope_keys != sorted(scope_keys)
        or len(scope_keys) != len(set(scope_keys))
        or rendered_pages != sorted(set(rendered_pages))
        or len(table_source_node_ids) != len(set(table_source_node_ids))
        or len(cell_ids) != len(set(cell_ids))
        or len(sheet_parts) != len(set(sheet_parts))
        or _fingerprint_xlsx_explicit_peer_scope_group(fact)
        != fact["group_fingerprint"]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} explicit-peer group binding is invalid."
        )


def _validate_pdf_table_continuation_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    segments = list(fact["segments"])
    if int(fact["expected_segment_count"]) != len(segments):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} segment count is inconsistent."
        )
    locator_keys = [
        (
            int(segment["locator"]["page_number"]),
            int(segment["locator"]["table_index"]),
        )
        for segment in segments
    ]
    if locator_keys != sorted(locator_keys) or len(locator_keys) != len(
        set(locator_keys)
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} PDF table locators must be unique and ordered."
        )
    if len({int(segment["column_count"]) for segment in segments}) != 1:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} continuation segments disagree on columns."
        )
    for segment in segments:
        rows = list(segment["rows"])
        row_count = int(segment["row_count"])
        if [int(row["row_index"]) for row in rows] != list(range(row_count)):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} PDF rows must cover each physical row once."
            )
        if int(segment["cell_count"]) != sum(int(row["cell_count"]) for row in rows):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} PDF segment cell count is inconsistent."
            )
    if int(fact["expected_physical_row_count"]) != sum(
        int(segment["row_count"]) for segment in segments
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} physical-row total is inconsistent."
        )
    if int(fact["expected_cell_count"]) != sum(
        int(segment["cell_count"]) for segment in segments
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} PDF cell total is inconsistent."
        )
    if (
        int(fact["continuation_from_page"]) != locator_keys[0][0]
        or int(fact["continuation_to_page"]) != locator_keys[-1][0]
        or locator_keys[0][0] >= locator_keys[-1][0]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} continuation page bounds are invalid."
        )
    if _fingerprint_pdf_table_segment_records(segments) != fact["group_fingerprint"]:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} PDF table fingerprint is invalid."
        )


def _pdf_merge_region_record_key(
    region: dict[str, Any],
) -> tuple[int, int, int, int]:
    return (
        int(region["row"]),
        int(region["column"]),
        int(region["row_span"]),
        int(region["column_span"]),
    )


def _validate_pdf_table_continuation_logical_grid_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    segments = list(fact["segments"])
    if int(fact["expected_segment_count"]) != len(segments):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} logical-grid segment count is inconsistent."
        )
    locator_keys = [
        (
            int(segment["locator"]["page_number"]),
            int(segment["locator"]["table_index"]),
        )
        for segment in segments
    ]
    if locator_keys != sorted(locator_keys) or len(locator_keys) != len(
        set(locator_keys)
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} logical-grid locators must be unique and ordered."
        )
    if len({int(segment["column_count"]) for segment in segments}) != 1:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} logical-grid segments disagree on columns."
        )

    for segment in segments:
        row_count = int(segment["row_count"])
        column_count = int(segment["column_count"])
        rows = list(segment["rows"])
        if [int(row["row_index"]) for row in rows] != list(range(row_count)):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} logical-grid rows must be complete and ordered."
            )
        if any(int(row["logical_slot_count"]) != column_count for row in rows):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} logical-grid row width is inconsistent."
            )
        if int(segment["logical_slot_count"]) != sum(
            int(row["logical_slot_count"]) for row in rows
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} logical-grid segment size is inconsistent."
            )

        merge_regions = list(segment["merged_regions"])
        merge_keys = [_pdf_merge_region_record_key(item) for item in merge_regions]
        if merge_keys != sorted(merge_keys) or len(merge_keys) != len(set(merge_keys)):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} merge topology must be unique and ordered."
            )
        occupied: set[tuple[int, int]] = set()
        for row, column, row_span, column_span in merge_keys:
            if (
                row_span * column_span <= 1
                or row + row_span > row_count
                or column + column_span > column_count
            ):
                raise ManifestValidationError(
                    f"SourceIR oracle {oracle_id} has an invalid merge topology span."
                )
            coverage = {
                (covered_row, covered_column)
                for covered_row in range(row, row + row_span)
                for covered_column in range(column, column + column_span)
            }
            if occupied.intersection(coverage):
                raise ManifestValidationError(
                    f"SourceIR oracle {oracle_id} merge topology overlaps."
                )
            occupied.update(coverage)

    if int(fact["expected_row_count"]) != sum(
        int(segment["row_count"]) for segment in segments
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} logical-grid row total is inconsistent."
        )
    if int(fact["expected_logical_slot_count"]) != sum(
        int(segment["logical_slot_count"]) for segment in segments
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} logical-slot total is inconsistent."
        )
    if (
        int(fact["continuation_from_page"]) != locator_keys[0][0]
        or int(fact["continuation_to_page"]) != locator_keys[-1][0]
        or locator_keys[0][0] >= locator_keys[-1][0]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} logical-grid page bounds are invalid."
        )
    if _fingerprint_pdf_table_segment_records(segments) != fact["group_fingerprint"]:
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} logical-grid fingerprint is invalid."
        )


def _validate_pdf_table_note_label_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    table = fact["table"]
    rows = list(table["rows"])
    row_count = int(table["row_count"])
    if [int(row["row_index"]) for row in rows] != list(range(row_count)):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} table-note rows must be complete and ordered."
        )
    if int(table["cell_count"]) != sum(int(row["cell_count"]) for row in rows):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} table-note cell count is inconsistent."
        )
    note_row_index = int(fact["note_row_index"])
    parameter_row_indices = [int(value) for value in fact["parameter_row_indices"]]
    if (
        note_row_index >= row_count
        or parameter_row_indices != sorted(set(parameter_row_indices))
        or len(parameter_row_indices) != 2
        or any(value <= 0 or value >= note_row_index for value in parameter_row_indices)
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} table-note row roles are invalid."
        )
    label_records = list(fact["label_records"])
    if int(fact["expected_label_record_count"]) != len(label_records):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} label count is inconsistent."
        )
    label_keys = [_pdf_text_locator_record_key(record) for record in label_records]
    if label_keys != sorted(label_keys) or len(label_keys) != len(set(label_keys)):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} label locators must be unique and ordered."
        )
    if (
        _fingerprint_pdf_text_locator_records(label_records)
        != fact["label_fingerprint"]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} label fingerprint is invalid."
        )
    child_assertion = fact["child_assertion"]
    if child_assertion.get("kind") != "source_node":
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} child assertion must be a source node."
        )
    duplicate_caption_records = list(fact["duplicate_caption_records"])
    if (
        len(duplicate_caption_records) != 2
        or any(record not in label_records for record in duplicate_caption_records)
        or len(
            {
                _pdf_text_locator_record_key(record)
                for record in duplicate_caption_records
            }
        )
        != 2
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} duplicate caption scope is invalid."
        )
    if (
        _fingerprint_pdf_table_note_label_group(
            table=table,
            note_row_index=note_row_index,
            parameter_row_indices=parameter_row_indices,
            label_records=label_records,
            duplicate_caption_records=duplicate_caption_records,
            child_assertion=child_assertion,
        )
        != fact["group_fingerprint"]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} table-note group fingerprint is invalid."
        )


def _validate_pdf_option_state_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    page_number = int(fact["page_number"])
    sections = list(fact["sections"])
    section_labels = [str(section["section_label"]) for section in sections]
    if (
        int(fact["expected_section_count"]) != len(sections)
        or section_labels
        != sorted(section_labels, key=lambda value: tuple(map(int, value.split("."))))
        or len(section_labels) != len(set(section_labels))
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} option sections must be complete, unique and ordered."
        )

    all_record_keys: list[tuple[int, int, str]] = []
    total_option_count = 0
    for section in sections:
        records = list(section["options"])
        record_keys = [_pdf_option_state_record_key(record) for record in records]
        total_option_count += len(records)
        all_record_keys.extend(record_keys)
        if (
            int(section["expected_option_count"]) != len(records)
            or len(records) < 2
            or record_keys != sorted(record_keys)
            or len(record_keys) != len(set(record_keys))
            or any(key[0] != page_number for key in record_keys)
            or _fingerprint_pdf_option_state_records(records)
            != section["option_fingerprint"]
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} option records are inconsistent."
            )
    if (
        int(fact["expected_option_count"]) != total_option_count
        or len(all_record_keys) != len(set(all_record_keys))
        or _fingerprint_pdf_option_state_group(
            page_number=page_number,
            sections=sections,
        )
        != fact["group_fingerprint"]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} option group fingerprint is invalid."
        )


def _validate_pdf_revision_history_oracle(
    oracle_id: str,
    fact: dict[str, Any],
) -> None:
    documents = list(fact["documents"])
    document_ids = [str(document["document_id"]) for document in documents]
    if (
        int(fact["expected_document_count"]) != len(documents)
        or len(documents) < 2
        or document_ids != sorted(document_ids)
        or len(document_ids) != len(set(document_ids))
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} revision documents are inconsistent."
        )

    observed_revision_row_count = 0
    for document in documents:
        table = document["table"]
        row_count = int(table["row_count"])
        column_count = int(table["column_count"])
        rows = list(table["rows"])
        revision_row_indices = [
            int(row_index) for row_index in document["revision_row_indices"]
        ]
        observed_revision_row_count += len(revision_row_indices)
        if (
            [int(row["row_index"]) for row in rows] != list(range(row_count))
            or any(int(row["logical_slot_count"]) != column_count for row in rows)
            or int(table["logical_slot_count"]) != row_count * column_count
            or revision_row_indices != sorted(set(revision_row_indices))
            or not revision_row_indices
            or any(
                row_index < 0 or row_index >= row_count
                for row_index in revision_row_indices
            )
            or _fingerprint_pdf_revision_history_document(document)
            != document["table_fingerprint"]
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} revision table is inconsistent."
            )

    if (
        int(fact["expected_revision_row_count"]) != observed_revision_row_count
        or _fingerprint_pdf_revision_history_group(documents)
        != fact["group_fingerprint"]
    ):
        raise ManifestValidationError(
            f"SourceIR oracle {oracle_id} revision group fingerprint is invalid."
        )


def _oracle_document_ids(oracle: dict[str, Any]) -> list[str]:
    if "document_ids" in oracle:
        return [str(document_id) for document_id in oracle["document_ids"]]
    return [str(oracle["document_id"])]


def _oracle_source_sha256_by_document(oracle: dict[str, Any]) -> dict[str, str]:
    if "source_sha256_by_document" in oracle:
        return {
            str(document_id): str(source_sha256)
            for document_id, source_sha256 in oracle[
                "source_sha256_by_document"
            ].items()
        }
    return {str(oracle["document_id"]): str(oracle["source_sha256"])}


def _validate_oracle_bindings(
    manifest: dict[str, Any],
    registry: dict[str, Any],
) -> None:
    validate_oracle_registry(registry)
    oracles = registry["oracles"]
    references: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for case in manifest["cases"].values():
        for assertion in case["assertions"]:
            evaluation = assertion.get("evaluation")
            if not isinstance(evaluation, dict):
                continue
            evaluator_id = str(evaluation.get("evaluator_id") or "")
            oracle_id = evaluation.get("oracle_id")
            if evaluator_id in ORACLE_OWNER_EVALUATOR_IDS:
                if not isinstance(oracle_id, str) or oracle_id not in oracles:
                    raise ManifestValidationError(
                        f"Assertion {assertion['assertion_id']} references an unknown oracle."
                    )
                if oracle_id in references:
                    raise ManifestValidationError(
                        f"SourceIR oracle {oracle_id} is owned more than once via oracle_id."
                    )
                references[oracle_id] = (assertion, evaluation)
            elif oracle_id is not None:
                raise ManifestValidationError(
                    f"Assertion {assertion['assertion_id']} cannot attach an oracle to this evaluator."
                )
    if set(references) != set(oracles):
        raise ManifestValidationError(
            "Every SourceIR oracle must be owned exactly once via oracle_id by the manifest."
        )
    for oracle_id, (assertion, evaluation) in references.items():
        oracle = oracles[oracle_id]
        applicable_document_ids = list(evaluation["applicable_document_ids"])
        document_ids = _oracle_document_ids(oracle)
        source_sha256_by_document = _oracle_source_sha256_by_document(oracle)
        if (
            oracle_id != f"ORACLE-{assertion['assertion_id']}"
            or oracle["evaluator_id"] != evaluation["evaluator_id"]
            or oracle["assertion_id"] != assertion["assertion_id"]
            or oracle["assertion_contract_sha256"]
            != _assertion_contract_sha256(assertion)
            or applicable_document_ids != document_ids
            or set(source_sha256_by_document) != set(document_ids)
            or any(
                document_id not in manifest["documents"]
                or source_sha256_by_document[document_id]
                != manifest["documents"][document_id]["source_sha256"]
                for document_id in document_ids
            )
        ):
            raise ManifestValidationError(
                f"SourceIR oracle {oracle_id} is not bound to exactly one assertion and its declared document source hashes."
            )


def _validate_structure_source_oracle_bindings(
    manifest: dict[str, Any],
    registry: dict[str, Any],
) -> None:
    """Bind each derived evaluator to one compatible reviewed SourceIR scope."""

    oracles = registry["oracles"]
    assertions_by_id = {
        str(assertion["assertion_id"]): assertion
        for candidate_case in manifest["cases"].values()
        for assertion in candidate_case["assertions"]
    }
    for case in manifest["cases"].values():
        for assertion in case["assertions"]:
            evaluation = assertion.get("evaluation")
            if not isinstance(evaluation, dict):
                continue
            evaluator_id = str(evaluation.get("evaluator_id") or "")
            expected_fact_kind = SOURCE_ORACLE_EVALUATOR_FACT_KINDS.get(evaluator_id)
            if expected_fact_kind is None:
                if evaluation.get("source_oracle_id") is not None:
                    raise ManifestValidationError(
                        f"Assertion {assertion['assertion_id']} cannot attach a SourceIR scope oracle."
                    )
                continue

            source_oracle_id = (
                evaluation.get("oracle_id")
                if evaluator_id in SOURCE_SCOPE_OWNER_EVALUATOR_IDS
                else evaluation.get("source_oracle_id")
            )
            if not isinstance(source_oracle_id, str) or source_oracle_id not in oracles:
                raise ManifestValidationError(
                    f"Assertion {assertion['assertion_id']} references an unknown SourceIR scope oracle."
                )
            source_oracle = oracles[source_oracle_id]
            source_assertion_id = str(source_oracle["assertion_id"])
            source_assertion = assertions_by_id.get(source_assertion_id)
            source_evaluation = (
                source_assertion.get("evaluation")
                if isinstance(source_assertion, dict)
                else None
            )
            applicable_document_ids = list(evaluation["applicable_document_ids"])
            source_owner_evaluator_id = (
                str(source_evaluation.get("evaluator_id") or "")
                if isinstance(source_evaluation, dict)
                else ""
            )
            source_owner_is_fidelity_assertion = bool(
                isinstance(source_assertion, dict)
                and source_assertion.get("layer") == "source_ir"
                and source_owner_evaluator_id in ORACLE_EVALUATOR_IDS
                and source_evaluation.get("oracle_id") == source_oracle_id
                and source_oracle.get("evaluator_id") == source_owner_evaluator_id
            )
            source_owner_is_reviewed_scope_assertion = bool(
                isinstance(source_assertion, dict)
                and source_owner_evaluator_id in SOURCE_SCOPE_OWNER_EVALUATOR_IDS
                and source_evaluation.get("oracle_id") == source_oracle_id
                and source_oracle.get("evaluator_id") == source_owner_evaluator_id
            )
            oracle_document_ids = _oracle_document_ids(source_oracle)
            source_document_scope_matches = bool(
                applicable_document_ids == oracle_document_ids
                or (
                    evaluator_id in FMT001_COHORT_EVALUATOR_IDS
                    and applicable_document_ids == list(case["document_ids"])
                    and len(oracle_document_ids) == 1
                    and set(oracle_document_ids).issubset(
                        applicable_document_ids
                    )
                )
            )
            if (
                source_assertion is None
                or not isinstance(source_evaluation, dict)
                or not (
                    source_owner_is_fidelity_assertion
                    or source_owner_is_reviewed_scope_assertion
                )
                or (
                    source_assertion is assertion
                    and not source_owner_is_reviewed_scope_assertion
                )
                or not source_document_scope_matches
                or source_oracle["fact"]["kind"] != expected_fact_kind
            ):
                raise ManifestValidationError(
                    f"Assertion {assertion['assertion_id']} is not bound to a compatible reviewed SourceIR scope."
                )
            if evaluator_id in {
                TABLE_ROW_SCHEDULE_INFORMATION_EVALUATOR_ID,
                TABLE_ROW_SAMPLE_PHASE_INFORMATION_EVALUATOR_ID,
            }:
                expected_cell_range = evaluation.get("expected_cell_range")
                source_fact = source_oracle["fact"]
                matching_cell_records = [
                    record
                    for record in source_fact["cells"]
                    if record["cell_range"] == expected_cell_range
                ]
                if (
                    not isinstance(expected_cell_range, str)
                    or _A1_CELL_PATTERN.fullmatch(expected_cell_range) is None
                    or len(matching_cell_records) != 1
                    or not _bounds_contain(
                        _parse_a1_range(str(source_fact["locator"]["cell_range"])),
                        _parse_a1_range(expected_cell_range),
                    )
                ):
                    raise ManifestValidationError(
                        f"Assertion {assertion['assertion_id']} expected cell is not bound to exactly one reviewed oracle cell."
                    )
            if evaluator_id == PROJECTION_MATERIAL_REQUIREMENT_COUNT_EVALUATOR_ID:
                if int(assertion["expected"]) != int(
                    source_oracle["fact"]["row_identity"]["expected_cell_count"]
                ):
                    raise ManifestValidationError(
                        f"Assertion {assertion['assertion_id']} material count does not match its reviewed row identities."
                    )
            if evaluator_id == PROJECTION_MATERIAL_SHARED_VALUES_EVALUATOR_ID:
                reviewed_roles = [
                    str(binding["role"]).lower()
                    for binding in source_oracle["fact"]["role_bindings"]
                ]
                if list(assertion["expected"]) != reviewed_roles:
                    raise ManifestValidationError(
                        f"Assertion {assertion['assertion_id']} material roles do not match its independent reviewed scope."
                    )
            if expected_fact_kind == "xlsx_explicit_peer_scope_group":
                reviewed_ordinals = [
                    str(value)
                    for value in source_oracle["fact"][
                        "expected_peer_ordinals"
                    ]
                ]
                if (
                    not isinstance(source_assertion.get("expected"), list)
                    or list(source_assertion["expected"]) != reviewed_ordinals
                    or (
                        evaluator_id
                        == STRUCTURE_EXPLICIT_TABLE_PEER_ITEMS_EVALUATOR_ID
                        and int(assertion["expected"])
                        != int(
                            source_oracle["fact"][
                                "expected_items_per_scope"
                            ]
                        )
                    )
                    or (
                        evaluator_id
                        == PROJECTION_EXPLICIT_PEER_SINGLE_REQUIREMENT_EVALUATOR_ID
                        and (
                            bool(assertion["expected"])
                            or 6
                            not in {
                                int(value)
                                for value in source_oracle["fact"][
                                    "expected_peer_ordinals"
                                ]
                            }
                        )
                    )
                ):
                    raise ManifestValidationError(
                        f"Assertion {assertion['assertion_id']} explicit-peer contract does not match its reviewed scope."
                    )
            if expected_fact_kind == "pdf_table_note_label_group":
                parameter_count = len(source_oracle["fact"]["parameter_row_indices"])
                if evaluator_id == PDF_TABLE_PARAMETER_COUNT_EVALUATOR_ID:
                    expected_parameter_count = int(assertion["expected"])
                elif evaluator_id == PDF_TABLE_NOTE_APPLIES_TO_EVALUATOR_ID:
                    expected_parameter_count = len(assertion["expected"])
                elif evaluator_id == PDF_SECTION_CHILD_ASSERTION_EVALUATOR_ID:
                    expected_parameter_count = 2
                else:
                    expected_parameter_count = parameter_count
                if parameter_count != expected_parameter_count:
                    raise ManifestValidationError(
                        f"Assertion {assertion['assertion_id']} parameter-row contract does not match its reviewed SourceIR scope."
                    )
            if expected_fact_kind == "pdf_option_state_group":
                sections = {
                    str(section["section_label"]): section
                    for section in source_oracle["fact"]["sections"]
                }
                required_label = (
                    "3.3"
                    if evaluator_id
                    in {
                        PDF_OPTION_RISK_SEMANTIC_EVALUATOR_ID,
                        PDF_OPTION_SECTION_NO_REQUIREMENTS_EVALUATOR_ID,
                    }
                    else "4.1"
                    if evaluator_id
                    in {
                        PDF_OPTION_REGULATION_SEMANTIC_EVALUATOR_ID,
                        PDF_CHECKED_OPTION_APPLICABILITY_EVALUATOR_ID,
                        PDF_OPTION_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID,
                    }
                    else None
                )
                if required_label is not None and required_label not in sections:
                    raise ManifestValidationError(
                        f"Assertion {assertion['assertion_id']} is missing its reviewed option section."
                    )
                if evaluator_id == PDF_CHECKED_OPTION_APPLICABILITY_EVALUATOR_ID:
                    checked_count = sum(
                        record["state"] == StructureOptionState.CHECKED.value
                        for record in sections["4.1"]["options"]
                    )
                    if checked_count != len(assertion["expected"]):
                        raise ManifestValidationError(
                            f"Assertion {assertion['assertion_id']} checked-option contract does not match its reviewed SourceIR scope."
                        )


def _assertion_contract_sha256(assertion: dict[str, Any]) -> str:
    return _sha256_text(
        json.dumps(
            {
                "expected": assertion["expected"],
                "operator": assertion["operator"],
                "target": assertion["target"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json_payload(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint_source_scope_group(scopes: Iterable[dict[str, Any]]) -> str:
    return _sha256_text(
        _canonical_json_payload(
            sorted(scopes, key=lambda scope: str(scope["document_id"]))
        )
    )


def _column_number(value: str) -> int:
    number = 0
    for character in value:
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _column_name(number: int) -> str:
    value = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        value = chr(ord("A") + remainder) + value
    return value


def _parse_a1_range(value: str) -> tuple[int, int, int, int]:
    match = _A1_RANGE_PATTERN.fullmatch(value)
    if match is None:
        raise ManifestValidationError("Oracle contains an invalid A1 range.")
    first_column, first_row, last_column, last_row = match.groups()
    min_row = int(first_row)
    min_column = _column_number(first_column)
    max_row = int(last_row or first_row)
    max_column = _column_number(last_column or first_column)
    if max_row < min_row or max_column < min_column:
        raise ManifestValidationError("Oracle A1 range must be ordered.")
    return min_row, min_column, max_row, max_column


def _bounds_contain(
    container: tuple[int, int, int, int],
    item: tuple[int, int, int, int],
) -> bool:
    return bool(
        container[0] <= item[0] <= item[2] <= container[2]
        and container[1] <= item[1] <= item[3] <= container[3]
    )


def _cell_sort_key(
    record: dict[str, Any], field: str = "cell_range"
) -> tuple[int, int]:
    min_row, min_column, _, _ = _parse_a1_range(str(record[field]))
    return min_row, min_column


def _xlsx_cell_record_payload(records: Iterable[dict[str, Any]]) -> str:
    return _canonical_json_payload(sorted(records, key=_cell_sort_key))


def _fingerprint_xlsx_cell_records(records: Iterable[dict[str, Any]]) -> str:
    return _sha256_text(_xlsx_cell_record_payload(records))


def _docx_table_record_payload(records: Iterable[dict[str, Any]]) -> str:
    ordered = sorted(
        records,
        key=lambda record: (
            int(record["locator"]["body_element_index"]),
            int(record["locator"]["table_index"]),
        ),
    )
    return _canonical_json_payload(ordered)


def _fingerprint_docx_table_records(records: Iterable[dict[str, Any]]) -> str:
    return _sha256_text(_docx_table_record_payload(records))


def _fingerprint_docx_mixed_content_group(
    *,
    narratives: Iterable[dict[str, Any]],
    parameter_table_oracle_id: str,
    expected_parameter_table_count: int,
    parameter_table_group_fingerprint: str,
) -> str:
    return _sha256_text(
        _canonical_json_payload(
            {
                "narratives": sorted(
                    narratives,
                    key=lambda record: (
                        int(record["locator"]["body_element_index"]),
                        str(record["kind"]),
                    ),
                ),
                "parameter_table_oracle_id": parameter_table_oracle_id,
                "expected_parameter_table_count": expected_parameter_table_count,
                "parameter_table_group_fingerprint": (
                    parameter_table_group_fingerprint
                ),
            }
        )
    )


def _docx_table_row_cell_payload(records: Iterable[dict[str, Any]]) -> str:
    return _canonical_json_payload(
        sorted(records, key=lambda record: int(record["column"]))
    )


def _fingerprint_docx_table_row_cells(
    records: Iterable[dict[str, Any]],
) -> str:
    return _sha256_text(_docx_table_row_cell_payload(records))


def _fingerprint_docx_table_row_scope(fact: dict[str, Any]) -> str:
    return _sha256_text(
        _canonical_json_payload(
            {
                "locator": dict(fact["locator"]),
                "anchor_cell_count": int(fact["anchor_cell_count"]),
                "logical_slot_count": int(fact["logical_slot_count"]),
                "row_fingerprint": str(fact["row_fingerprint"]),
            }
        )
    )


def _pdf_table_segment_record_payload(
    records: Iterable[dict[str, Any]],
) -> str:
    ordered = sorted(
        records,
        key=lambda record: (
            int(record["locator"]["page_number"]),
            int(record["locator"]["table_index"]),
        ),
    )
    return _canonical_json_payload(ordered)


def _fingerprint_pdf_table_segment_records(
    records: Iterable[dict[str, Any]],
) -> str:
    return _sha256_text(_pdf_table_segment_record_payload(records))


def _pdf_text_locator_record_key(record: dict[str, Any]) -> tuple[Any, ...]:
    locator = record["locator"]
    if record["kind"] == "source_node":
        return (
            int(locator["page_number"]),
            0,
            int(locator["block_index"]),
            str(locator["source_node_kind"]),
        )
    return (
        int(locator["page_number"]),
        1,
        int(locator["table_index"]),
        int(locator["row_index"]),
        int(locator["column_index"]),
        int(locator["row_span"]),
        int(locator["column_span"]),
    )


def _fingerprint_pdf_text_locator_records(
    records: Iterable[dict[str, Any]],
) -> str:
    return _sha256_text(
        _canonical_json_payload(sorted(records, key=_pdf_text_locator_record_key))
    )


def _pdf_option_state_record_key(
    record: dict[str, Any],
) -> tuple[int, int, str]:
    locator = record["locator"]
    return (
        int(locator["page_number"]),
        int(locator["block_index"]),
        str(locator["source_node_kind"]),
    )


def _fingerprint_pdf_option_state_records(
    records: Iterable[dict[str, Any]],
) -> str:
    return _sha256_text(
        _canonical_json_payload(
            sorted(records, key=_pdf_option_state_record_key),
        )
    )


def _fingerprint_pdf_option_state_group(
    *,
    page_number: int,
    sections: Iterable[dict[str, Any]],
) -> str:
    ordered_sections = sorted(
        sections,
        key=lambda section: tuple(map(int, str(section["section_label"]).split("."))),
    )
    return _sha256_text(
        _canonical_json_payload(
            {
                "page_number": page_number,
                "sections": ordered_sections,
            }
        )
    )


def _fingerprint_pdf_revision_history_document(
    document: dict[str, Any],
) -> str:
    return _sha256_text(
        _canonical_json_payload(
            {
                "document_id": str(document["document_id"]),
                "table": document["table"],
                "revision_row_indices": [
                    int(row_index) for row_index in document["revision_row_indices"]
                ],
            }
        )
    )


def _fingerprint_pdf_revision_history_group(
    documents: Iterable[dict[str, Any]],
) -> str:
    ordered = sorted(
        documents,
        key=lambda document: str(document["document_id"]),
    )
    return _sha256_text(_canonical_json_payload(ordered))


def _fingerprint_pdf_table_note_label_group(
    *,
    table: dict[str, Any],
    note_row_index: int,
    parameter_row_indices: Iterable[int],
    label_records: Iterable[dict[str, Any]],
    duplicate_caption_records: Iterable[dict[str, Any]],
    child_assertion: dict[str, Any],
) -> str:
    return _sha256_text(
        _canonical_json_payload(
            {
                "table": table,
                "note_row_index": note_row_index,
                "parameter_row_indices": sorted(parameter_row_indices),
                "label_records": sorted(
                    label_records,
                    key=_pdf_text_locator_record_key,
                ),
                "duplicate_caption_records": sorted(
                    duplicate_caption_records,
                    key=_pdf_text_locator_record_key,
                ),
                "child_assertion": child_assertion,
            }
        )
    )


def _xlsx_merge_record_payload(records: Iterable[dict[str, Any]]) -> str:
    return _canonical_json_payload(
        sorted(records, key=lambda item: _cell_sort_key(item, "anchor"))
    )


def _fingerprint_xlsx_merge_records(records: Iterable[dict[str, Any]]) -> str:
    return _sha256_text(_xlsx_merge_record_payload(records))


def _xlsx_identity_record_payload(records: Iterable[dict[str, Any]]) -> str:
    return _canonical_json_payload(sorted(records, key=_cell_sort_key))


def _fingerprint_xlsx_identity_records(records: Iterable[dict[str, Any]]) -> str:
    return _sha256_text(_xlsx_identity_record_payload(records))


def _fingerprint_xlsx_merge_group(
    merge_records: Iterable[dict[str, Any]],
    identity_records: Iterable[dict[str, Any]],
) -> str:
    return _sha256_text(
        _canonical_json_payload(
            {
                "merges": sorted(
                    merge_records,
                    key=lambda item: _cell_sort_key(item, "anchor"),
                ),
                "row_identity": sorted(identity_records, key=_cell_sort_key),
            }
        )
    )


_XLSX_MATERIAL_ROLE_ORDER = {
    "STANDARD": 0,
    "ACCEPTANCE_CRITERIA": 1,
}


def _xlsx_material_role_binding_payload(binding: dict[str, Any]) -> str:
    return _canonical_json_payload(
        {
            "role": str(binding["role"]),
            "header_cell_range": str(binding["header_cell_range"]),
            "header_raw_sha256": str(binding["header_raw_sha256"]),
            "anchor_cell_range": str(binding["anchor_cell_range"]),
            "anchor_raw_sha256": str(binding["anchor_raw_sha256"]),
            "merge_range": str(binding["merge_range"]),
            "expected_applies_to_count": int(
                binding["expected_applies_to_count"]
            ),
            "rendered_page_number": int(binding["rendered_page_number"]),
        }
    )


def _fingerprint_xlsx_material_role_binding(
    binding: dict[str, Any],
) -> str:
    return _sha256_text(_xlsx_material_role_binding_payload(binding))


def _fingerprint_xlsx_material_requirement_scope(
    fact: dict[str, Any],
) -> str:
    return _sha256_text(
        _canonical_json_payload(
            {
                "source_oracle_id": str(fact["source_oracle_id"]),
                "source_merge_group_fingerprint": str(
                    fact["source_merge_group_fingerprint"]
                ),
                "table_locator": dict(fact["table_locator"]),
                "full_row_scope": str(fact["full_row_scope"]),
                "identity_rendered_page_number": int(
                    fact["identity_rendered_page_number"]
                ),
                "expected_requirement_count": int(
                    fact["expected_requirement_count"]
                ),
                "expected_role_applies_to_count": int(
                    fact["expected_role_applies_to_count"]
                ),
                "row_expectations": sorted(
                    (dict(record) for record in fact["row_expectations"]),
                    key=lambda record: _cell_sort_key(
                        record,
                        "identity_cell_range",
                    ),
                ),
                "role_bindings": sorted(
                    (dict(binding) for binding in fact["role_bindings"]),
                    key=lambda binding: _XLSX_MATERIAL_ROLE_ORDER[
                        str(binding["role"])
                    ],
                ),
            }
        )
    )


def _fingerprint_xlsx_explicit_peer_scope(
    scope: dict[str, Any],
) -> str:
    return _sha256_text(
        _canonical_json_payload(
            {
                "table_locator": dict(scope["table_locator"]),
                "table_source_node_id": str(scope["table_source_node_id"]),
                "cell_range": str(scope["cell_range"]),
                "cell_id": str(scope["cell_id"]),
                "raw_sha256": str(scope["raw_sha256"]),
                "raw_text_length": int(scope["raw_text_length"]),
                "rendered_page_number": int(scope["rendered_page_number"]),
                "reviewed_sheet_part": str(scope["reviewed_sheet_part"]),
                "reviewed_sheet_part_sha256": str(
                    scope["reviewed_sheet_part_sha256"]
                ),
                "items": [
                    {
                        "ordinal": int(item["ordinal"]),
                        "start_offset": int(item["start_offset"]),
                        "end_offset": int(item["end_offset"]),
                        "raw_sha256": str(item["raw_sha256"]),
                    }
                    for item in scope["items"]
                ],
            }
        )
    )


def _fingerprint_xlsx_explicit_peer_scope_group(
    fact: dict[str, Any],
) -> str:
    return _sha256_text(
        _canonical_json_payload(
            {
                "expected_scope_count": int(fact["expected_scope_count"]),
                "expected_items_per_scope": int(
                    fact["expected_items_per_scope"]
                ),
                "expected_total_item_count": int(
                    fact["expected_total_item_count"]
                ),
                "expected_peer_ordinals": [
                    int(value) for value in fact["expected_peer_ordinals"]
                ],
                "scopes": [dict(scope) for scope in fact["scopes"]],
            }
        )
    )


def resolve_document_path(corpus_root: Path, document_ref: str) -> Path:
    if not document_ref.startswith(EXTERNAL_PREFIX):
        raise CorpusPathError(
            "Document reference is outside the training corpus namespace."
        )
    relative_value = document_ref[len(EXTERNAL_PREFIX) :]
    if not relative_value or Path(relative_value).is_absolute():
        raise CorpusPathError("Document reference must contain a relative path.")
    resolved_root = corpus_root.expanduser().resolve()
    candidate = (resolved_root / relative_value).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise CorpusPathError(
            "Document reference escapes the training corpus root."
        ) from exc
    return candidate


def require_report_destination_outside_corpus(
    output_dir: Path, corpus_root: Path
) -> Path:
    """Reject report destinations that would mutate the controlled corpus."""

    resolved_output = output_dir.expanduser().resolve()
    resolved_corpus = corpus_root.expanduser().resolve()
    try:
        resolved_output.relative_to(resolved_corpus)
    except ValueError:
        return resolved_output
    raise CorpusPathError("Report output must be outside the training corpus root.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _selected_pdf_visual_page_scopes(
    *,
    manifest: dict[str, Any],
    selected_case_ids: Iterable[str],
) -> dict[str, frozenset[int]]:
    """Return case-complete visual capture windows without widening any case.

    A document receives a narrowed capture window only when every selected case
    that references it declares an exact page scope. If one selected case omits
    the declaration, the caller falls back to the full source-bound Silver
    route for that document.
    """

    selected = tuple(selected_case_ids)
    case_ids_by_document: dict[str, list[str]] = {}
    for case_id in selected:
        for document_id in manifest["cases"][case_id]["document_ids"]:
            case_ids_by_document.setdefault(document_id, []).append(case_id)

    result: dict[str, frozenset[int]] = {}
    for document_id, case_ids in case_ids_by_document.items():
        page_numbers: set[int] = set()
        for case_id in case_ids:
            scope = manifest["cases"][case_id].get(
                "pdf_visual_page_numbers_by_document"
            )
            if not isinstance(scope, dict) or document_id not in scope:
                break
            page_numbers.update(int(value) for value in scope[document_id])
        else:
            result[document_id] = frozenset(page_numbers)
    return result


def run_manifest(
    manifest: dict[str, Any],
    *,
    corpus_root: Path,
    legacy_mode: str = "off",
    source_ir_mode: str = "off",
    selected_case_ids: Iterable[str] | None = None,
    legacy_timeout_seconds: float | None = None,
    oracle_registry: dict[str, Any] | None = None,
    pdf_visual_provider: str = PDF_VISUAL_PROVIDER_OFF,
    pdf_visual_registry: Any | None = None,
    bailian_visual_model: str | None = None,
    pdf_body_review_artifact_resolver: PdfBodyReviewArtifactResolver | None = None,
    pdf_body_review_trust_policy: PdfBodyReviewTrustPolicy | None = None,
) -> dict[str, Any]:
    resolved_oracle_registry = (
        load_oracle_registry() if oracle_registry is None else oracle_registry
    )
    validate_manifest(manifest, oracle_registry=resolved_oracle_registry)
    if legacy_mode not in {"off", "core", "preview"}:
        raise ValueError("legacy_mode must be off, core, or preview")
    if source_ir_mode not in {"off", "native"}:
        raise ValueError("source_ir_mode must be off or native")
    if pdf_visual_provider not in PDF_VISUAL_PROVIDER_CHOICES:
        raise ValueError(
            "pdf_visual_provider must be off, bailian, or azure-document-intelligence"
        )
    if pdf_visual_provider != PDF_VISUAL_PROVIDER_OFF and source_ir_mode != "native":
        raise ValueError("pdf_visual_provider requires source_ir_mode native")
    if pdf_visual_provider != PDF_VISUAL_PROVIDER_OFF and pdf_visual_registry is None:
        pdf_visual_registry = load_pdf_shadow_silver()
    if legacy_mode != "off" and legacy_timeout_seconds is None:
        legacy_timeout_seconds = DEFAULT_LEGACY_TIMEOUT_SECONDS
    if legacy_timeout_seconds is not None and (
        not math.isfinite(legacy_timeout_seconds) or legacy_timeout_seconds <= 0
    ):
        raise ValueError(
            "legacy_timeout_seconds must be finite and positive when provided"
        )
    selected = list(selected_case_ids or manifest["cases"].keys())
    unknown_cases = sorted(set(selected) - set(manifest["cases"]))
    if unknown_cases:
        raise ManifestValidationError(
            f"Unknown selected case IDs: {','.join(unknown_cases)}"
        )
    verified_pdf_body_reviews = resolve_selected_pdf_body_review_profiles(
        manifest=manifest,
        selected_case_ids=selected,
        artifact_resolver=pdf_body_review_artifact_resolver,
        trust_policy=pdf_body_review_trust_policy,
        evaluator_id=PDF_REVIEWED_BODY_SEQUENCE_EVALUATOR_ID,
    )

    selected_document_ids = {
        document_id
        for case_id in selected
        for document_id in manifest["cases"][case_id]["document_ids"]
    }
    pdf_visual_page_scopes = _selected_pdf_visual_page_scopes(
        manifest=manifest,
        selected_case_ids=selected,
    )
    document_results: dict[str, dict[str, Any]] = {}
    observation_cache: dict[tuple[str, str], dict[str, Any]] = {}
    source_ir_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    source_ir_snapshots: dict[tuple[str, str], Any] = {}
    pdf_visual_runtime_identities: dict[str, dict[str, str]] = {}
    structure_snapshots: dict[tuple[str, str, str], Any] = {}
    semantic_snapshots: dict[tuple[str, str, str], Any] = {}
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any] = {}
    attachment_snapshots: dict[str, OpaqueAttachmentExtraction] = {}
    hard_blockers: list[dict[str, str]] = []
    run_started = time.perf_counter()

    for document_id, document in manifest["documents"].items():
        if document_id not in selected_document_ids:
            continue
        result, blockers = _evaluate_document(
            document_id,
            document,
            corpus_root=corpus_root,
            legacy_mode=legacy_mode,
            source_ir_mode=source_ir_mode,
            observation_cache=observation_cache,
            source_ir_cache=source_ir_cache,
            source_ir_snapshots=source_ir_snapshots,
            attachment_snapshots=attachment_snapshots,
            legacy_timeout_seconds=legacy_timeout_seconds,
            pdf_visual_provider=pdf_visual_provider,
            pdf_visual_registry=pdf_visual_registry,
            bailian_visual_model=bailian_visual_model,
            pdf_visual_capture_page_numbers=pdf_visual_page_scopes.get(document_id),
            pdf_visual_runtime_identities=pdf_visual_runtime_identities,
        )
        document_results[document_id] = result
        hard_blockers.extend(blockers)

    case_results: dict[str, dict[str, Any]] = {}
    for case_id in selected:
        case = manifest["cases"][case_id]
        related = [
            document_results[document_id] for document_id in case["document_ids"]
        ]
        blocking_document_ids = [
            row["document_id"] for row in related if bool(row.get("hard_blocking"))
        ]
        assertion_results = _evaluate_case_assertions(
            case=case,
            manifest_documents=manifest["documents"],
            document_results=document_results,
            source_ir_mode=source_ir_mode,
            blocking_document_ids=blocking_document_ids,
            oracle_registry=resolved_oracle_registry,
            source_ir_snapshots=source_ir_snapshots,
            structure_snapshots=structure_snapshots,
            semantic_snapshots=semantic_snapshots,
            requirement_view_snapshots=requirement_view_snapshots,
            attachment_snapshots=attachment_snapshots,
            pdf_visual_provider=pdf_visual_provider,
            verified_pdf_body_reviews=verified_pdf_body_reviews,
            pdf_visual_runtime_identities=pdf_visual_runtime_identities,
        )
        assertion_counts = Counter(item["status"] for item in assertion_results)
        unmet_layers = [
            item["layer"] for item in assertion_results if item["status"] != "PASS"
        ]
        if blocking_document_ids:
            status = "BLOCKED_INPUT"
            first_gap_layer = "preflight"
        elif assertion_counts.get("FAIL", 0):
            status = "FAIL"
            first_gap_layer = min(unmet_layers, key=LAYER_ORDER.index)
        elif not unmet_layers:
            status = "PASS"
            first_gap_layer = None
        else:
            status = "GAP"
            first_gap_layer = min(unmet_layers, key=LAYER_ORDER.index)
        case_results[case_id] = {
            "case_id": case_id,
            "status": status,
            "first_gap_layer": first_gap_layer,
            "document_count": len(related),
            "assertion_count": len(case["assertions"]),
            "assertion_status_counts": dict(sorted(assertion_counts.items())),
            "assertions": assertion_results,
            "blocking_document_ids": blocking_document_ids,
        }

    preflight_counts = Counter(
        str(row.get("preflight_status") or "UNKNOWN")
        for row in document_results.values()
    )
    legacy_counts = Counter(
        str(row.get("legacy_status") or "NOT_RUN") for row in document_results.values()
    )
    source_ir_counts = Counter(
        str(row.get("source_ir_status") or "NOT_RUN")
        for row in document_results.values()
    )
    case_counts = Counter(
        str(row.get("status") or "UNKNOWN") for row in case_results.values()
    )
    assertion_status_counts = Counter(
        assertion["status"]
        for case in case_results.values()
        for assertion in case["assertions"]
    )
    source_ir_observation_ok = source_ir_mode != "native" or all(
        str(row.get("source_ir_status") or "NOT_RUN")
        in {
            "COMPLETE",
            "PARTIAL",
            "REUSED_IDENTICAL_SOURCE",
            "BLOCKED_PREFLIGHT",
            "ATTACHMENT_ONLY",
        }
        for row in document_results.values()
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_schema_version": manifest["schema_version"],
        "manifest_sha256": hashlib.sha256(
            json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(),
        "oracle_registry_sha256": hashlib.sha256(
            _canonical_json_payload(resolved_oracle_registry).encode("utf-8")
        ).hexdigest(),
        "legacy_mode": legacy_mode,
        "legacy_timeout_seconds": legacy_timeout_seconds,
        "source_ir_mode": source_ir_mode,
        "pdf_visual_provider": pdf_visual_provider,
        "summary": {
            "documents": len(document_results),
            "cases": len(case_results),
            "assertions": sum(row["assertion_count"] for row in case_results.values()),
            "hard_blocker_count": len(hard_blockers),
            "identity_gate_pass": not hard_blockers,
            "source_ir_observation_ok": source_ir_observation_ok,
            "preflight_status_counts": dict(sorted(preflight_counts.items())),
            "legacy_status_counts": dict(sorted(legacy_counts.items())),
            "source_ir_status_counts": dict(sorted(source_ir_counts.items())),
            "case_status_counts": dict(sorted(case_counts.items())),
            "assertion_status_counts": dict(sorted(assertion_status_counts.items())),
            "elapsed_ms": round((time.perf_counter() - run_started) * 1000, 3),
        },
        "hard_blockers": hard_blockers,
        "documents": document_results,
        "cases": case_results,
    }


def _evaluate_case_assertions(
    *,
    case: dict[str, Any],
    manifest_documents: dict[str, dict[str, Any]],
    document_results: dict[str, dict[str, Any]],
    source_ir_mode: str,
    blocking_document_ids: list[str],
    oracle_registry: dict[str, Any],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
    attachment_snapshots: dict[str, OpaqueAttachmentExtraction],
    pdf_visual_provider: str = PDF_VISUAL_PROVIDER_OFF,
    verified_pdf_body_reviews: Mapping[tuple[str, str], VerifiedPdfBodyReviewProfile]
    | None = None,
    pdf_visual_runtime_identities: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    resolved_verified_pdf_body_reviews = verified_pdf_body_reviews or {}
    for assertion in case["assertions"]:
        base = {
            "assertion_id": str(assertion["assertion_id"]),
            "layer": str(assertion["layer"]),
        }
        if blocking_document_ids:
            results.append(
                {
                    **base,
                    "status": "BLOCKED_INPUT",
                    "reason_code": "required_input_blocked",
                    "evidence": {"blocking_document_count": len(blocking_document_ids)},
                }
            )
            continue
        if source_ir_mode != "native":
            results.append(
                {
                    **base,
                    "status": "GAP",
                    "reason_code": f"{assertion['layer']}_not_run",
                    "evidence": {},
                }
            )
            continue

        evaluation = assertion.get("evaluation")
        if not isinstance(evaluation, dict):
            results.append(
                {
                    **base,
                    "status": "GAP",
                    "reason_code": "target_evaluator_not_implemented",
                    "evaluator": None,
                    "evidence": {},
                }
            )
            continue
        evaluator_id = str(evaluation.get("evaluator_id") or "")
        applicable_document_ids = list(evaluation.get("applicable_document_ids") or [])
        applicable_case = {**case, "document_ids": applicable_document_ids}
        scoped_document_results = {
            document_id: document_results[document_id]
            for document_id in applicable_document_ids
        }
        if evaluator_id == "source_ir.source_hash_dedup/v1":
            outcome = _evaluate_source_hash_deduplication(
                case=applicable_case,
                manifest_documents=manifest_documents,
                document_results=scoped_document_results,
            )
        elif evaluator_id == "source_ir.pdf_annotation_policy/v1":
            outcome = _evaluate_pdf_annotation_policy(
                case=applicable_case,
                document_results=scoped_document_results,
            )
        elif evaluator_id == PDF_REGISTERED_SOURCE_IDENTITY_EVALUATOR_ID:
            outcome = _evaluate_pdf_registered_source_identity(
                case=applicable_case,
                manifest_documents=manifest_documents,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
            )
        elif evaluator_id == PDF_REVIEWED_BODY_SEQUENCE_EVALUATOR_ID:
            assertion_id = str(assertion["assertion_id"])
            outcome = _evaluate_pdf_reviewed_body_sequence(
                case=applicable_case,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                reviewed_body_profiles_by_document=dict(
                    evaluation.get("reviewed_body_profiles_by_document") or {}
                ),
                pdf_visual_provider=pdf_visual_provider,
                verified_review_profiles_by_document={
                    document_id: resolved_verified_pdf_body_reviews[
                        (
                            assertion_id,
                            document_id,
                        )
                    ]
                    for document_id in applicable_document_ids
                    if (assertion_id, document_id) in resolved_verified_pdf_body_reviews
                },
                visual_runtime_identities_by_document=(
                    pdf_visual_runtime_identities or {}
                ),
            )
        elif evaluator_id == SPREADSHEET_VISIBLE_WORKBOOK_FIDELITY_EVALUATOR_ID:
            outcome = _evaluate_spreadsheet_visible_workbook_fidelity(
                case=applicable_case,
                manifest_documents=manifest_documents,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                workbook_profiles_by_document=dict(
                    evaluation.get("workbook_profiles_by_document") or {}
                ),
            )
        elif evaluator_id == PDF_SINGLE_TOC_NOISE_BLOCK_EVALUATOR_ID:
            outcome = _evaluate_pdf_single_toc_noise_block(
                case=applicable_case,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                expected_candidate_fingerprints_by_document=dict(
                    evaluation.get("expected_candidate_fingerprints_by_document") or {}
                ),
            )
        elif evaluator_id == PDF_PAGE_NOISE_NOT_REQUIREMENTS_EVALUATOR_ID:
            outcome = _evaluate_pdf_page_noise_not_requirements(
                case=applicable_case,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id in ORACLE_EVALUATOR_IDS:
            outcome = _evaluate_oracle_fidelity(
                oracle=(oracle_registry["oracles"])[str(evaluation["oracle_id"])],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                evaluator_id=evaluator_id,
                oracle_registry=oracle_registry,
            )
        elif evaluator_id == STRUCTURE_SINGLE_ROW_EVALUATOR_ID:
            outcome = _evaluate_single_table_row_boundary(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id in {
            TABLE_ROW_SCHEDULE_INFORMATION_EVALUATOR_ID,
            TABLE_ROW_SAMPLE_PHASE_INFORMATION_EVALUATOR_ID,
        }:
            outcome = _evaluate_compound_table_row_information_facet(
                evaluator_id=evaluator_id,
                expected_cell_range=str(evaluation["expected_cell_range"]),
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
            )
        elif evaluator_id == SINGLE_COMPOUND_TABLE_ROW_PROJECTION_EVALUATOR_ID:
            outcome = _evaluate_single_compound_table_row_projection(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == STRUCTURE_DOCX_SINGLE_ROW_EVALUATOR_ID:
            outcome = _evaluate_docx_single_table_row_boundary(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id in {
            SOURCE_DOCX_BILINGUAL_TEXTS_EVALUATOR_ID,
            STRUCTURE_BILINGUAL_COUNTERPART_EVALUATOR_ID,
        }:
            outcome = _evaluate_fmt001_reviewed_bilingual_group(
                evaluator_id=evaluator_id,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif (
            evaluator_id
            == PROJECTION_BILINGUAL_SINGLE_REQUIREMENT_EVALUATOR_ID
        ):
            outcome = _evaluate_fmt001_bilingual_projection(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == STRUCTURE_DOCUMENT_LANGUAGE_POLICY_EVALUATOR_ID:
            outcome = _evaluate_fmt001_document_language_policy(
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id == FINDING_BILINGUAL_MISMATCH_SAFETY_EVALUATOR_ID:
            outcome = _evaluate_fmt001_bilingual_mismatch_safety(
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id in {
            PROJECTION_DOCX_SINGLE_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID,
            PROJECTION_DOCX_NO_SENTENCE_SPLIT_EVALUATOR_ID,
        }:
            outcome = _evaluate_docx_table_row_projection(
                evaluator_id=evaluator_id,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == STRUCTURE_DATA_ROWS_EVALUATOR_ID:
            outcome = _evaluate_table_data_row_boundaries(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id in {
            PERFORMANCE_PARAMETER_ROWS_SEMANTIC_EVALUATOR_ID,
            REQUIREMENT_PER_PARAMETER_ROW_PROJECTION_EVALUATOR_ID,
        }:
            outcome = _evaluate_performance_parameter_rows(
                evaluator_id=evaluator_id,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id in {
            TECHNICAL_NARRATIVE_DISPOSITION_EVALUATOR_ID,
            SOURCE_ROLE_NOT_SEMANTIC_EXCLUSION_EVALUATOR_ID,
        }:
            source_oracle = (oracle_registry["oracles"])[
                str(evaluation["source_oracle_id"])
            ]
            parameter_table_oracle = (oracle_registry["oracles"])[
                str(source_oracle["fact"]["parameter_table_oracle_id"])
            ]
            outcome = _evaluate_docx_mixed_content_semantics(
                evaluator_id=evaluator_id,
                source_oracle=source_oracle,
                parameter_table_oracle=parameter_table_oracle,
                oracle_registry=oracle_registry,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == STRUCTURE_MERGED_APPLICABILITY_EVALUATOR_ID:
            outcome = _evaluate_merged_cell_applicability(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id in {
            PROJECTION_MATERIAL_REQUIREMENT_COUNT_EVALUATOR_ID,
            PROJECTION_MATERIAL_SHARED_VALUES_EVALUATOR_ID,
        }:
            source_oracle_id = (
                str(evaluation["source_oracle_id"])
                if evaluator_id
                == PROJECTION_MATERIAL_REQUIREMENT_COUNT_EVALUATOR_ID
                else str(evaluation["oracle_id"])
            )
            outcome = _evaluate_material_requirement_projection(
                evaluator_id=evaluator_id,
                source_oracle=(oracle_registry["oracles"])[
                    source_oracle_id
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == STRUCTURE_EXPLICIT_TABLE_PEER_ITEMS_EVALUATOR_ID:
            outcome = _evaluate_explicit_table_peer_items(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif (
            evaluator_id
            == PROJECTION_EXPLICIT_PEER_SINGLE_REQUIREMENT_EVALUATOR_ID
        ):
            outcome = _evaluate_explicit_peer_projection(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id in {
            PDF_TABLE_CONTINUATION_V1_EVALUATOR_ID,
            PDF_TABLE_CONTINUATION_EVALUATOR_ID,
        }:
            outcome = _evaluate_pdf_table_continuation(
                case=applicable_case,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                evaluator_id=evaluator_id,
            )
        elif evaluator_id == ATTACHMENT_IDENTITY_EVALUATOR_ID:
            outcome = _evaluate_opaque_attachment_identity(
                case=applicable_case,
                manifest_documents=manifest_documents,
                document_results=scoped_document_results,
                attachment_snapshots=attachment_snapshots,
            )
        elif evaluator_id == ATTACHMENT_DISPOSITION_EVALUATOR_ID:
            outcome = _evaluate_attachment_disposition(
                case=applicable_case,
                document_results=scoped_document_results,
                attachment_snapshots=attachment_snapshots,
            )
        elif evaluator_id == ATTACHMENT_PROJECTION_EVALUATOR_ID:
            outcome = _evaluate_attachment_reference_only(
                case=applicable_case,
                document_results=scoped_document_results,
                attachment_snapshots=attachment_snapshots,
            )
        elif evaluator_id == ATTACHMENT_WORKFLOW_EVALUATOR_ID:
            outcome = _evaluate_attachment_workflow_policy(
                case=applicable_case,
                document_results=scoped_document_results,
                attachment_snapshots=attachment_snapshots,
            )
        elif evaluator_id == SECTION_REQUIREMENT_SUBTYPE_EVALUATOR_ID:
            outcome = _evaluate_section_requirement_subtype(
                case=applicable_case,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                expected_subtype=str(assertion["expected"]),
            )
        elif evaluator_id == SECTION_REQUIREMENT_PROJECTION_EVALUATOR_ID:
            outcome = _evaluate_section_requirement_projection(
                case=applicable_case,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == SECTION_MAIN_REQUIREMENT_EVALUATOR_ID:
            outcome = _evaluate_section_requirement_projection(
                case=applicable_case,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
                evaluator_id=SECTION_MAIN_REQUIREMENT_EVALUATOR_ID,
            )
        elif evaluator_id == SECTION_MAIN_ROLLUP_EVALUATOR_ID:
            outcome = _evaluate_section_requirement_projection(
                case=applicable_case,
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
                evaluator_id=SECTION_MAIN_ROLLUP_EVALUATOR_ID,
                expected_decision_mode=RequirementDecisionMode.ROLLUP,
            )
        elif evaluator_id in {
            PDF_TABLE_ROWS_NOT_REQUIREMENTS_V1_EVALUATOR_ID,
            PDF_TABLE_ROWS_NOT_REQUIREMENTS_EVALUATOR_ID,
        }:
            outcome = _evaluate_pdf_table_rows_not_requirements(
                case=applicable_case,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
                evaluator_id=evaluator_id,
            )
        elif evaluator_id == PDF_TABLE_PARAMETER_COUNT_EVALUATOR_ID:
            outcome = _evaluate_pdf_table_parameter_count(
                case=applicable_case,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == PDF_TABLE_NOTE_APPLIES_TO_EVALUATOR_ID:
            outcome = _evaluate_pdf_table_note_applies_to(
                case=applicable_case,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id == PDF_SECTION_CHILD_ASSERTION_EVALUATOR_ID:
            outcome = _evaluate_pdf_section_child_assertion(
                case=applicable_case,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == PDF_DUPLICATE_TABLE_LABEL_EVALUATOR_ID:
            outcome = _evaluate_pdf_duplicate_table_label(
                case=applicable_case,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id == PDF_OPTION_RISK_SEMANTIC_EVALUATOR_ID:
            outcome = _evaluate_pdf_option_section_semantic(
                evaluator_id=evaluator_id,
                section_label="3.3",
                expected_semantic_class=SemanticClass.PROJECT_RISK,
                expected_requirement_subtype=None,
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
            )
        elif evaluator_id == PDF_OPTION_SECTION_NO_REQUIREMENTS_EVALUATOR_ID:
            outcome = _evaluate_pdf_option_section_no_requirements(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == PDF_OPTION_REGULATION_SEMANTIC_EVALUATOR_ID:
            outcome = _evaluate_pdf_option_section_semantic(
                evaluator_id=evaluator_id,
                section_label="4.1",
                expected_semantic_class=SemanticClass.REQUIREMENT,
                expected_requirement_subtype=(
                    RequirementSubtype.REGULATION_REQUIREMENT
                ),
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
            )
        elif evaluator_id == PDF_CHECKED_OPTION_APPLICABILITY_EVALUATOR_ID:
            outcome = _evaluate_pdf_checked_option_applicability(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id == PDF_OPTION_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID:
            outcome = _evaluate_pdf_option_top_level_requirement(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == PDF_UNCHECKED_OPTION_NOT_EFFECTIVE_EVALUATOR_ID:
            outcome = _evaluate_pdf_unchecked_option_not_effective(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == PDF_OPTION_UNCERTAINTY_POLICY_EVALUATOR_ID:
            outcome = _evaluate_pdf_option_uncertainty_policy(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
            )
        elif evaluator_id == PDF_REVISION_HISTORY_SEMANTIC_EVALUATOR_ID:
            outcome = _evaluate_pdf_revision_history_semantic(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
            )
        elif evaluator_id == PDF_REVISION_HISTORY_NOT_REQUIREMENTS_EVALUATOR_ID:
            outcome = _evaluate_pdf_revision_history_not_requirements(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == PDF_REVISION_HISTORY_NO_INFERENCE_EVALUATOR_ID:
            outcome = _evaluate_pdf_revision_history_no_inference(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        elif evaluator_id == PDF_REVISION_SNAPSHOTS_DISTINCT_EVALUATOR_ID:
            outcome = _evaluate_pdf_revision_snapshots_distinct(
                source_oracle=(oracle_registry["oracles"])[
                    str(evaluation["source_oracle_id"])
                ],
                document_results=scoped_document_results,
                source_ir_snapshots=source_ir_snapshots,
            )
        elif evaluator_id in CONFIRMED_CASE_EVALUATOR_IDS:
            source_oracle_id = evaluation.get("source_oracle_id")
            outcome = _evaluate_confirmed_case_assertion(
                assertion=assertion,
                case=applicable_case,
                manifest_documents=manifest_documents,
                document_results=scoped_document_results,
                source_oracle=(
                    (oracle_registry["oracles"])[str(source_oracle_id)]
                    if source_oracle_id is not None
                    else None
                ),
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
        else:
            outcome = {
                "status": "GAP",
                "reason_code": "declared_evaluator_not_implemented",
                "evaluator": evaluator_id,
                "evidence": {},
            }
        results.append(
            {
                **base,
                "applicable_document_ids": applicable_document_ids,
                **outcome,
            }
        )
    return results


def _structure_descendant_ids(tree: Any, root_id: str) -> set[str]:
    descendants = {root_id}
    changed = True
    while changed:
        changed = False
        for node in tree.nodes:
            if (
                node.structure_node_id not in descendants
                and node.parent_structure_node_id in descendants
            ):
                descendants.add(node.structure_node_id)
                changed = True
    return descendants


def _unique_section_node(tree: Any, label: str) -> Any | None:
    matches = [
        node
        for node in tree.nodes
        if node.kind is StructureNodeKind.SECTION and node.explicit_label == label
    ]
    return matches[0] if len(matches) == 1 else None


def _ancestor_section_labels(tree: Any, node: Any) -> list[str]:
    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    labels: list[str] = []
    parent_id = node.parent_structure_node_id
    while parent_id is not None:
        parent = nodes_by_id.get(parent_id)
        if parent is None:
            break
        if (
            parent.kind is StructureNodeKind.SECTION
            and parent.explicit_label is not None
        ):
            labels.append(parent.explicit_label)
        parent_id = parent.parent_structure_node_id
    labels.reverse()
    return labels


def _structure_node_pages(
    node: Any,
    *,
    source_snapshot: Any,
) -> set[int]:
    return {
        page_number
        for ref in node.source_refs
        if (
            page_number := _source_ref_page_number(
                ref,
                source_snapshot=source_snapshot,
            )
        )
        is not None
    }


def _structure_finding_count(tree: Any, *codes: str) -> int:
    expected = set(codes)
    return sum(
        finding.code in expected
        and finding.severity
        in {StructureFindingSeverity.INFO, StructureFindingSeverity.WARNING}
        for finding in tree.findings
    )


def _confirmed_case_pipeline(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> tuple[Any, Any, Any, Any] | None:
    document_ids = list(case.get("document_ids") or [])
    if len(document_ids) != 1:
        return None
    document_id = document_ids[0]
    result = document_results.get(document_id)
    if result is None:
        return None
    return _pdf_requirement_context_for_document(
        document_id=document_id,
        document_result=result,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        requirement_view_snapshots=requirement_view_snapshots,
    )


def _evaluate_confirmed_case_assertion(
    *,
    assertion: dict[str, Any],
    case: dict[str, Any],
    manifest_documents: dict[str, dict[str, Any]],
    document_results: dict[str, dict[str, Any]],
    source_oracle: dict[str, Any] | None,
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    assertion_id = str(assertion["assertion_id"])
    evaluator_id = CONFIRMED_CASE_EVALUATOR_CONTRACTS[assertion_id][0]
    evidence: dict[str, Any] = {
        "document_count": len(case.get("document_ids") or []),
        "source_scope_mismatch_count": 0,
    }

    def passed(reason_code: str, **values: Any) -> dict[str, Any]:
        evidence.update(values)
        return {
            "status": "PASS",
            "reason_code": reason_code,
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    def failed(reason_code: str, **values: Any) -> dict[str, Any]:
        evidence.update(values)
        return {
            "status": "FAIL",
            "reason_code": reason_code,
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    if source_oracle is not None:
        source_outcome = _evaluate_oracle_fidelity(
            oracle=source_oracle,
            document_results=document_results,
            source_ir_snapshots=source_ir_snapshots,
        )
        source_evidence = source_outcome["evidence"]
        evidence.update(
            {
                "source_oracle_id": str(source_oracle["oracle_id"]),
                "source_scope_mismatch_count": int(
                    source_evidence.get("mismatch_count", 1)
                ),
                "source_scope_expected_document_count": int(
                    source_evidence.get("expected_primary_count", 0)
                ),
                "source_scope_observed_document_count": int(
                    source_evidence.get("observed_primary_count", 0)
                ),
            }
        )
        if source_outcome["status"] != "PASS":
            return failed("reviewed_source_scope_fidelity_mismatch")

    if assertion_id.startswith("ROLE-"):
        document_ids = list(case["document_ids"])
        role_mismatch_count = sum(
            (
                document_results[document_id].get("document_role")
                != manifest_documents[document_id].get("document_role")
            )
            or (
                document_results[document_id].get("ingress_route")
                != manifest_documents[document_id].get("ingress_route")
            )
            for document_id in document_ids
        )
        evidence["role_or_route_mismatch_count"] = role_mismatch_count
        if assertion_id == "ROLE-001-A1":
            authoritative_route_count = sum(
                document_results[document_id].get("ingress_route")
                == manifest_documents[document_id].get("ingress_route")
                for document_id in document_ids
            )
            if role_mismatch_count or authoritative_route_count != len(document_ids):
                return failed(
                    "ingress_route_authority_mismatch",
                    authoritative_route_count=authoritative_route_count,
                )
            return passed(
                "ingress_route_is_document_role_authority",
                authoritative_route_count=authoritative_route_count,
            )
        if assertion_id == "ROLE-001-A2":
            source_ir_role_field_count = 0
            for document_id in document_ids:
                snapshot = _source_ir_snapshot_for_document_result(
                    document_results[document_id],
                    source_ir_snapshots=source_ir_snapshots,
                )
                source_ir_role_field_count += int(
                    snapshot is not None
                    and any(
                        key in type(snapshot).model_fields
                        for key in ("document_role", "ingress_route")
                    )
                )
            override_count = role_mismatch_count + source_ir_role_field_count
            if override_count:
                return failed(
                    "parser_role_override_observed",
                    parser_role_override_count=override_count,
                )
            return passed(
                "parser_cannot_override_ingress_role",
                parser_role_override_count=0,
            )
        if assertion_id == "ROLE-002-A1":
            engineer_input_count = sum(
                document_results[document_id].get("document_role")
                == "engineer_structured_input"
                and document_results[document_id].get("ingress_route")
                == "knowledge_document"
                for document_id in document_ids
            )
            if role_mismatch_count or engineer_input_count != len(document_ids):
                return failed(
                    "engineer_structured_input_role_mismatch",
                    engineer_structured_input_count=engineer_input_count,
                )
            return passed(
                "engineer_structured_input_role_observed",
                engineer_structured_input_count=engineer_input_count,
            )
        if assertion_id == "ROLE-002-A2":
            customer_source_truth_count = sum(
                document_results[document_id].get("document_role")
                in {
                    "customer_requirement_source",
                    "customer_standard",
                }
                or document_results[document_id].get("ingress_route")
                == "requirement_source"
                for document_id in document_ids
            )
            if role_mismatch_count or customer_source_truth_count:
                return failed(
                    "engineer_input_customer_truth_observed",
                    customer_source_truth_count=customer_source_truth_count,
                )
            return passed(
                "engineer_input_not_customer_source_truth",
                customer_source_truth_count=0,
            )
        if assertion_id == "ROLE-003-A1":
            route_mismatch_count = sum(
                document_results[document_id].get("ingress_route")
                != "downstream_artifact"
                for document_id in document_ids
            )
            if role_mismatch_count or route_mismatch_count:
                return failed(
                    "downstream_artifact_route_mismatch",
                    downstream_route_mismatch_count=route_mismatch_count,
                )
            return passed(
                "documents_remain_downstream_artifacts",
                downstream_artifact_count=len(document_ids),
            )
        if assertion_id == "ROLE-003-A2":
            source_identity_mismatch_count = 0
            for document_id in document_ids:
                snapshot = _source_ir_snapshot_for_document_result(
                    document_results[document_id],
                    source_ir_snapshots=source_ir_snapshots,
                )
                source_identity_mismatch_count += int(
                    snapshot is None
                    or snapshot.source_sha256
                    != manifest_documents[document_id]["source_sha256"]
                )
            reverse_generated_count = sum(
                document_results[document_id].get("ingress_route")
                == "requirement_source"
                for document_id in document_ids
            )
            if (
                role_mismatch_count
                or source_identity_mismatch_count
                or reverse_generated_count
            ):
                return failed(
                    "reverse_generated_customer_source_observed",
                    source_identity_mismatch_count=source_identity_mismatch_count,
                    reverse_generated_customer_source_count=reverse_generated_count,
                )
            return passed(
                "downstream_artifacts_never_reverse_generate_customer_source",
                source_identity_mismatch_count=0,
                reverse_generated_customer_source_count=0,
            )
        if assertion_id in {"ROLE-004-A1", "ROLE-004-A2"}:
            expected_format = "pdf" if assertion_id.endswith("A1") else "xlsx"
            expected_role = (
                "customer_requirement_source"
                if expected_format == "pdf"
                else "internal_knowledge"
            )
            scoped_ids = [
                document_id
                for document_id in document_ids
                if manifest_documents[document_id]["format"] == expected_format
            ]
            mismatch_count = sum(
                document_results[document_id].get("document_role") != expected_role
                for document_id in scoped_ids
            )
            if role_mismatch_count or len(scoped_ids) != 2 or mismatch_count:
                return failed(
                    "document_role_partition_mismatch",
                    expected_format_document_count=len(scoped_ids),
                    partition_role_mismatch_count=mismatch_count,
                )
            return passed(
                "document_role_partition_observed",
                expected_format_document_count=len(scoped_ids),
                partition_role_mismatch_count=0,
            )
        if assertion_id == "ROLE-004-A3":
            paired_metadata_count = sum(
                bool(
                    manifest_documents[document_id].get("duplicate_of")
                    or manifest_documents[document_id].get("paired_with")
                )
                for document_id in document_ids
            )
            cross_format_shared_hash_count = sum(
                1
                for left in document_ids
                for right in document_ids
                if manifest_documents[left]["format"] == "pdf"
                and manifest_documents[right]["format"] == "xlsx"
                and manifest_documents[left]["source_sha256"]
                == manifest_documents[right]["source_sha256"]
            )
            if (
                role_mismatch_count
                or paired_metadata_count
                or cross_format_shared_hash_count
            ):
                return failed(
                    "pdf_xlsx_pairing_inference_observed",
                    paired_metadata_count=paired_metadata_count,
                    cross_format_shared_hash_count=cross_format_shared_hash_count,
                )
            return passed(
                "pdf_xlsx_pairing_not_inferred",
                paired_metadata_count=0,
                cross_format_shared_hash_count=0,
            )
        if assertion_id == "ROLE-004-A5":
            engineer_content_count = 0
            identity_mismatch_count = 0
            for document_id in document_ids:
                result = document_results[document_id]
                snapshot = _source_ir_snapshot_for_document_result(
                    result,
                    source_ir_snapshots=source_ir_snapshots,
                )
                engineer_content_count += int(
                    result.get("document_role") == "internal_knowledge"
                    and result.get("ingress_route") == "knowledge_document"
                )
                identity_mismatch_count += int(
                    snapshot is None
                    or snapshot.source_sha256
                    != manifest_documents[document_id]["source_sha256"]
                )
            if (
                role_mismatch_count
                or engineer_content_count != len(document_ids)
                or identity_mismatch_count
            ):
                return failed(
                    "engineer_content_source_identity_mismatch",
                    engineer_content_count=engineer_content_count,
                    source_identity_mismatch_count=identity_mismatch_count,
                )
            return passed(
                "xlsx_notes_retain_engineer_content_identity",
                engineer_content_count=engineer_content_count,
                source_identity_mismatch_count=0,
            )
        if assertion_id == "ROLE-005-A1":
            duplicate_edges = [
                (
                    document_id,
                    str(manifest_documents[document_id].get("duplicate_of") or ""),
                )
                for document_id in document_ids
                if manifest_documents[document_id].get("duplicate_of")
            ]
            distinct_hash_count = len(
                {
                    str(manifest_documents[document_id]["source_sha256"])
                    for document_id in document_ids
                }
            )
            if (
                role_mismatch_count
                or len(duplicate_edges) != 1
                or distinct_hash_count != 2
            ):
                return failed(
                    "corpus_pairing_policy_mismatch",
                    declared_duplicate_edge_count=len(duplicate_edges),
                    distinct_source_hash_count=distinct_hash_count,
                )
            return passed(
                "corpus_pairing_inference_absent",
                declared_duplicate_edge_count=1,
                inferred_pairing_count=0,
                distinct_source_hash_count=2,
            )
        if assertion_id == "ROLE-005-A3":
            duplicate_ids = [
                document_id
                for document_id in document_ids
                if manifest_documents[document_id].get("duplicate_of")
            ]
            reused_count = sum(
                document_results[document_id].get("source_ir_status")
                == "REUSED_IDENTICAL_SOURCE"
                for document_id in duplicate_ids
            )
            separate_result_count = sum(
                document_id in document_results
                for document_id in (
                    *duplicate_ids,
                    *[
                        str(manifest_documents[item]["duplicate_of"])
                        for item in duplicate_ids
                    ],
                )
            )
            if (
                role_mismatch_count
                or len(duplicate_ids) != 1
                or reused_count != 1
                or separate_result_count != 2
            ):
                return failed(
                    "duplicate_source_path_warning_mismatch",
                    reused_duplicate_count=reused_count,
                    separate_source_path_count=separate_result_count,
                )
            return passed(
                "duplicate_source_paths_warn_without_mutation",
                reused_duplicate_count=1,
                separate_source_path_count=2,
            )

    context = _confirmed_case_pipeline(
        case=case,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if context is None:
        return failed("confirmed_case_pipeline_unavailable", pipeline_failure_count=1)
    source_snapshot, tree, semantics, view = context
    evidence["pipeline_failure_count"] = 0
    annotations_by_owner: dict[str, list[Any]] = {}
    for annotation in semantics.annotations:
        annotations_by_owner.setdefault(
            annotation.owner_structure_node_id,
            [],
        ).append(annotation)

    if assertion_id.startswith("STR-003"):
        if assertion_id in {"STR-003-A1", "STR-003-A2"}:
            section = _unique_section_node(tree, "4.12.2.1")
            if section is None:
                return failed(
                    "section_4_12_2_1_not_unique",
                    section_match_count=0,
                )
            descendants = _structure_descendant_ids(
                tree,
                section.structure_node_id,
            )
            if assertion_id == "STR-003-A1":
                observed_labels = _ancestor_section_labels(tree, section)
                if observed_labels != ["4", "4.12", "4.12.2"]:
                    return failed(
                        "section_ancestor_chain_mismatch",
                        ancestor_label_count=len(observed_labels),
                    )
                return passed(
                    "section_ancestor_chain_preserved",
                    ancestor_label_count=len(observed_labels),
                    ancestor_chain_match_count=1,
                )
            pages = {
                page
                for node in tree.nodes
                if node.structure_node_id in descendants
                for page in _structure_node_pages(
                    node,
                    source_snapshot=source_snapshot,
                )
            }
            continuation_relation_count = sum(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.source_structure_node_id in descendants
                and relation.target_structure_node_id in descendants
                for relation in tree.relations
            )
            if len(pages) < 2:
                return failed(
                    "cross_page_section_identity_mismatch",
                    covered_page_count=len(pages),
                    continuation_relation_count=continuation_relation_count,
                )
            return passed(
                "cross_page_section_identity_preserved",
                covered_page_count=len(pages),
                continuation_relation_count=continuation_relation_count,
                unique_section_identity_count=1,
            )
        section = _unique_section_node(tree, "4.13.3")
        if section is None:
            return failed("section_4_13_3_not_unique", section_match_count=0)
        descendants = _structure_descendant_ids(tree, section.structure_node_id)
        clauses = sorted(
            (
                node
                for node in tree.nodes
                if node.structure_node_id in descendants
                and node.kind is StructureNodeKind.CLAUSE
                and node.list_ordinal is not None
            ),
            key=lambda node: node.source_order,
        )
        if assertion_id == "STR-003-A3":
            observed_ordinals = [int(node.list_ordinal) for node in clauses]
            if observed_ordinals != [1, 2, 3, 4, 5]:
                return failed(
                    "explicit_numbered_children_mismatch",
                    explicit_numbered_child_count=len(clauses),
                )
            return passed(
                "explicit_numbered_children_preserved",
                explicit_numbered_child_count=5,
            )
        top_level = [
            node
            for node in view.nodes
            if node.owner_structure_node_id in descendants
            and node.role is RequirementProjectionRole.PRIMARY
            and node.parent_requirement_view_node_id is None
            and node.semantic_class is SemanticClass.REQUIREMENT
        ]
        if assertion_id == "STR-003-A4":
            if len(top_level) != 1:
                return failed(
                    "section_top_level_requirement_count_mismatch",
                    top_level_requirement_count=len(top_level),
                )
            return passed(
                "section_single_top_level_requirement_observed",
                top_level_requirement_count=1,
            )
        pseudo_count = max(0, len(top_level) - 1)
        if pseudo_count:
            return failed(
                "cross_page_pseudo_requirement_observed",
                cross_page_pseudo_requirement_count=pseudo_count,
            )
        return passed(
            "cross_page_pseudo_requirement_absent",
            cross_page_pseudo_requirement_count=0,
            top_level_requirement_count=len(top_level),
        )

    if assertion_id.startswith("STR-006"):
        section = _unique_section_node(tree, "1.1")
        if section is None:
            return failed("section_1_1_not_unique", section_match_count=0)
        direct_content = [
            node
            for node in tree.nodes
            if node.parent_structure_node_id == section.structure_node_id
            and node.kind is StructureNodeKind.CONTENT
        ]
        if assertion_id == "STR-006-A2":
            if len(direct_content) != 3:
                return failed(
                    "section_parent_block_mismatch",
                    parent_block_count=1,
                    direct_paragraph_count=len(direct_content),
                )
            return passed(
                "three_paragraphs_share_one_parent_block",
                parent_block_count=1,
                direct_paragraph_count=3,
            )
        descendants = _structure_descendant_ids(tree, section.structure_node_id)
        primary = [
            node
            for node in view.nodes
            if node.owner_structure_node_id in descendants
            and node.role is RequirementProjectionRole.PRIMARY
            and node.parent_requirement_view_node_id is None
        ]
        paragraph_primary_count = sum(
            node.owner_structure_node_id
            in {item.structure_node_id for item in direct_content}
            and node.role is RequirementProjectionRole.PRIMARY
            for node in view.nodes
        )
        if len(primary) != 1 or paragraph_primary_count == 3:
            return failed(
                "one_paragraph_one_requirement_projection_observed",
                top_level_requirement_count=len(primary),
                paragraph_primary_requirement_count=paragraph_primary_count,
            )
        return passed(
            "paragraphs_not_projected_as_independent_requirements",
            top_level_requirement_count=1,
            paragraph_primary_requirement_count=paragraph_primary_count,
        )

    if assertion_id.startswith("STR-007"):
        sections = {
            label: [
                node
                for node in tree.nodes
                if node.kind is StructureNodeKind.SECTION
                and node.explicit_label == label
            ]
            for label in ("5.4.1.2", "5.4.1.3", "5.4.1.4")
        }
        if assertion_id == "STR-007-A2":
            valid = bool(
                len(sections["5.4.1.2"]) == 1
                and not sections["5.4.1.3"]
                and len(sections["5.4.1.4"]) == 1
                and _ancestor_section_labels(
                    tree,
                    sections["5.4.1.2"][0],
                )[-1:]
                == ["5.4.1"]
                and _ancestor_section_labels(
                    tree,
                    sections["5.4.1.4"][0],
                )[-1:]
                == ["5.4.1"]
            )
            if not valid:
                return failed(
                    "numbering_gap_labels_mutated",
                    preserved_neighbor_label_count=(
                        len(sections["5.4.1.2"]) + len(sections["5.4.1.4"])
                    ),
                    synthetic_gap_label_count=len(sections["5.4.1.3"]),
                )
            return passed(
                "printed_numbering_gap_labels_preserved",
                preserved_neighbor_label_count=2,
                synthetic_gap_label_count=0,
            )
        if assertion_id == "STR-007-A3":
            finding_count = _structure_finding_count(tree, "section_numbering_gap")
            if finding_count != 1 or sections["5.4.1.3"]:
                return failed(
                    "section_numbering_gap_warning_mismatch",
                    numbering_gap_finding_count=finding_count,
                    synthetic_gap_label_count=len(sections["5.4.1.3"]),
                )
            return passed(
                "section_numbering_gap_warns_without_mutation",
                numbering_gap_finding_count=1,
                synthetic_gap_label_count=0,
            )
        synthetic_projection_count = sum(
            node.owner_structure_node_id
            in {section.structure_node_id for section in sections["5.4.1.3"]}
            for node in view.nodes
        )
        if sections["5.4.1.3"] or synthetic_projection_count:
            return failed(
                "synthetic_requirement_from_numbering_gap_observed",
                synthetic_gap_label_count=len(sections["5.4.1.3"]),
                synthetic_requirement_count=synthetic_projection_count,
            )
        return passed(
            "numbering_gap_does_not_create_requirement",
            synthetic_gap_label_count=0,
            synthetic_requirement_count=0,
        )

    if assertion_id.startswith("STR-008"):
        section = _unique_section_node(tree, "5.4.3")
        if section is None:
            return failed("section_5_4_3_not_unique", section_match_count=0)
        descendants = _structure_descendant_ids(tree, section.structure_node_id)
        clauses = sorted(
            (
                node
                for node in tree.nodes
                if node.structure_node_id in descendants
                and node.kind is StructureNodeKind.CLAUSE
                and node.list_ordinal is not None
            ),
            key=lambda node: node.source_order,
        )
        duplicate_fives = [node for node in clauses if node.list_ordinal == 5]
        if assertion_id == "STR-008-A2":
            common_parent_count = len(
                {node.parent_structure_node_id for node in duplicate_fives}
            )
            distinct_id_count = len(
                {node.structure_node_id for node in duplicate_fives}
            )
            if (
                len(duplicate_fives) != 2
                or common_parent_count != 1
                or distinct_id_count != 2
            ):
                return failed(
                    "duplicate_list_node_identity_mismatch",
                    duplicate_ordinal_node_count=len(duplicate_fives),
                    distinct_internal_id_count=distinct_id_count,
                    common_parent_count=common_parent_count,
                )
            return passed(
                "duplicate_list_nodes_keep_distinct_internal_ids",
                duplicate_ordinal_node_count=2,
                distinct_internal_id_count=2,
                common_parent_count=1,
            )
        if assertion_id == "STR-008-A3":
            second_id = (
                duplicate_fives[1].structure_node_id
                if len(duplicate_fives) == 2
                else ""
            )
            tables = [
                node
                for node in tree.nodes
                if node.structure_node_id in descendants
                and node.kind is StructureNodeKind.TABLE
            ]
            second_descendants = (
                _structure_descendant_ids(tree, second_id) if second_id else set()
            )
            parented_table_count = sum(
                table.structure_node_id in second_descendants for table in tables
            )
            if len(duplicate_fives) != 2 or parented_table_count != 1:
                return failed(
                    "duplicate_list_table_parent_mismatch",
                    table_under_second_duplicate_count=parented_table_count,
                    duplicate_ordinal_node_count=len(duplicate_fives),
                )
            return passed(
                "table_parented_to_second_duplicate_list_node",
                table_under_second_duplicate_count=1,
                duplicate_ordinal_node_count=2,
            )
        finding_count = _structure_finding_count(tree, "duplicate_list_ordinal")
        observed_ordinals = [int(node.list_ordinal) for node in clauses]
        if finding_count != 1 or observed_ordinals != [1, 2, 3, 4, 5, 5]:
            return failed(
                "duplicate_list_ordinal_warning_mismatch",
                duplicate_list_ordinal_finding_count=finding_count,
                observed_list_item_count=len(observed_ordinals),
            )
        return passed(
            "duplicate_list_ordinal_warns_without_renumbering",
            duplicate_list_ordinal_finding_count=1,
            observed_list_item_count=6,
            duplicate_ordinal_node_count=2,
        )

    if assertion_id.startswith("TAB-003"):
        section = _unique_section_node(tree, "1.2")
        if section is None:
            return failed("section_1_2_not_unique", section_match_count=0)
        descendants = _structure_descendant_ids(tree, section.structure_node_id)
        tables = [
            node
            for node in tree.nodes
            if node.structure_node_id in descendants
            and node.kind is StructureNodeKind.TABLE
        ]
        rows = [
            node
            for node in tree.nodes
            if node.structure_node_id in descendants
            and node.kind is StructureNodeKind.TABLE_ROW
        ]
        if assertion_id == "TAB-003-A2":
            ungrounded_row_count = sum(not row.source_refs for row in rows)
            if len(tables) != 1 or len(rows) != 9 or ungrounded_row_count:
                return failed(
                    "milestone_table_row_structure_mismatch",
                    table_count=len(tables),
                    locatable_row_count=len(rows),
                    ungrounded_row_count=ungrounded_row_count,
                )
            return passed(
                "milestone_table_rows_are_locatable",
                table_count=1,
                locatable_row_count=9,
                ungrounded_row_count=0,
            )
        schedule_annotation_count = sum(
            annotation.semantic_class is SemanticClass.SCHEDULE
            for node_id in descendants
            for annotation in annotations_by_owner.get(node_id, [])
        )
        row_requirement_count = sum(
            annotation.semantic_class is SemanticClass.REQUIREMENT
            for row in rows
            for annotation in annotations_by_owner.get(row.structure_node_id, [])
        )
        if assertion_id == "TAB-003-A3":
            if schedule_annotation_count < 1 or row_requirement_count:
                return failed(
                    "milestone_table_semantic_mismatch",
                    schedule_annotation_count=schedule_annotation_count,
                    row_requirement_semantic_count=row_requirement_count,
                )
            return passed(
                "milestone_table_classified_as_schedule",
                schedule_annotation_count=schedule_annotation_count,
                row_requirement_semantic_count=0,
            )
        row_ids = {row.structure_node_id for row in rows}
        product_requirement_count = sum(
            node.owner_structure_node_id in row_ids
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.requirement_subtype
            in {
                RequirementSubtype.PRODUCT_REQUIREMENT,
                RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
            }
            for node in view.nodes
        )
        if product_requirement_count:
            return failed(
                "milestone_rows_projected_as_product_requirements",
                product_requirement_count=product_requirement_count,
            )
        return passed(
            "milestone_rows_not_projected_as_product_requirements",
            product_requirement_count=0,
            locatable_row_count=len(rows),
        )

    if assertion_id.startswith("TAB-004"):
        section = _unique_section_node(tree, "1.3")
        if section is None:
            return failed("section_1_3_not_unique", section_match_count=0)
        descendants = _structure_descendant_ids(tree, section.structure_node_id)
        tables = [
            node
            for node in tree.nodes
            if node.structure_node_id in descendants
            and node.kind is StructureNodeKind.TABLE
        ]
        rows = [
            node
            for node in tree.nodes
            if node.structure_node_id in descendants
            and node.kind is StructureNodeKind.TABLE_ROW
        ]
        if assertion_id == "TAB-004-A2":
            ungrounded_count = sum(not node.source_refs for node in (*tables, *rows))
            if len(tables) < 1 or len(rows) < 3 or ungrounded_count:
                return failed(
                    "product_information_table_review_structure_mismatch",
                    table_count=len(tables),
                    row_count=len(rows),
                    ungrounded_structure_count=ungrounded_count,
                )
            return passed(
                "product_information_table_has_minimum_review_structure",
                table_count=1,
                row_count=3,
                ungrounded_structure_count=0,
            )
        decision_requirement_count = sum(
            node.owner_structure_node_id in descendants
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.decision_mode is not RequirementDecisionMode.NONE
            for node in view.nodes
        )
        if decision_requirement_count:
            return failed(
                "extra_product_object_graph_observed",
                decision_requirement_count=decision_requirement_count,
            )
        return passed(
            "extra_product_object_graph_absent",
            decision_requirement_count=0,
            context_projection_count=sum(
                node.owner_structure_node_id in descendants for node in view.nodes
            ),
        )

    if assertion_id == "SEM-001-A2":
        section = _unique_section_node(tree, "4.12.1")
        if section is None:
            return failed("section_4_12_1_not_unique", section_match_count=0)
        annotations = annotations_by_owner.get(section.structure_node_id, [])
        projected = [
            node
            for node in view.nodes
            if node.owner_structure_node_id == section.structure_node_id
            and node.role is RequirementProjectionRole.PRIMARY
            and node.decision_mode is RequirementDecisionMode.DIRECT
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.requirement_subtype
            is RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT
        ]
        if (
            len(annotations) != 1
            or annotations[0].semantic_class is not SemanticClass.REQUIREMENT
            or annotations[0].requirement_subtype
            is not RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT
            or len(projected) != 1
        ):
            return failed(
                "modal_absence_excluded_requirement",
                semantic_requirement_count=sum(
                    annotation.semantic_class is SemanticClass.REQUIREMENT
                    for annotation in annotations
                ),
                primary_direct_requirement_count=len(projected),
            )
        return passed(
            "modal_absence_is_not_an_exclusion_rule",
            semantic_requirement_count=1,
            primary_direct_requirement_count=1,
        )

    if assertion_id.startswith("SEM-004"):
        section = _unique_section_node(tree, "1.9")
        if section is None:
            return failed("section_1_9_not_unique", section_match_count=0)
        descendants = _structure_descendant_ids(tree, section.structure_node_id)
        rows = [
            node
            for node in tree.nodes
            if node.structure_node_id in descendants
            and node.kind is StructureNodeKind.TABLE_ROW
        ]
        row_ids = {row.structure_node_id for row in rows}
        reference_projection_row_ids = {
            node.owner_structure_node_id
            for node in view.nodes
            if node.owner_structure_node_id in row_ids
            and node.role is RequirementProjectionRole.REFERENCE
            and node.decision_mode is RequirementDecisionMode.NONE
        }
        reference_rows = [
            row for row in rows if row.structure_node_id in reference_projection_row_ids
        ]
        section_annotations = annotations_by_owner.get(
            section.structure_node_id,
            [],
        )
        if assertion_id == "SEM-004-A2":
            matches = [
                annotation
                for annotation in section_annotations
                if annotation.semantic_class is SemanticClass.REQUIREMENT
                and annotation.requirement_subtype
                is RequirementSubtype.REGULATION_REQUIREMENT
            ]
            if len(matches) != 1:
                return failed(
                    "normative_applicability_semantic_mismatch",
                    regulation_requirement_count=len(matches),
                )
            return passed(
                "section_classified_as_normative_applicability_requirement",
                regulation_requirement_count=1,
            )
        reference_annotation_count = sum(
            annotation.semantic_class is SemanticClass.REFERENCE
            for row in reference_rows
            for annotation in annotations_by_owner.get(row.structure_node_id, [])
        )
        if assertion_id == "SEM-004-A3":
            if len(reference_rows) != 29 or reference_annotation_count != 29:
                return failed(
                    "reference_child_structure_mismatch",
                    reference_row_count=len(reference_rows),
                    reference_semantic_count=reference_annotation_count,
                    total_table_row_count=len(rows),
                )
            return passed(
                "reference_entries_are_structured_children",
                reference_row_count=len(reference_rows),
                reference_semantic_count=29,
                total_table_row_count=len(rows),
            )
        ordinary_product_count = sum(
            node.owner_structure_node_id in row_ids
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.requirement_subtype
            in {
                RequirementSubtype.PRODUCT_REQUIREMENT,
                RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
            }
            for node in view.nodes
        )
        reference_projection_count = sum(
            node.owner_structure_node_id in row_ids
            and node.role is RequirementProjectionRole.REFERENCE
            and node.decision_mode is RequirementDecisionMode.NONE
            for node in view.nodes
        )
        if assertion_id == "SEM-004-A4":
            if ordinary_product_count or reference_projection_count != 29:
                return failed(
                    "reference_row_projection_mismatch",
                    ordinary_product_requirement_count=ordinary_product_count,
                    reference_projection_count=reference_projection_count,
                )
            return passed(
                "reference_rows_not_projected_as_product_requirements",
                ordinary_product_requirement_count=0,
                reference_projection_count=29,
            )
        if assertion_id == "SEM-004-A5":
            source_ref_catalog = {
                *(
                    StructureSourceRef(
                        kind=StructureSourceRefKind.SOURCE_NODE,
                        source_id=value,
                    )
                    for value in tree.source_node_ids
                ),
                *(
                    StructureSourceRef(
                        kind=StructureSourceRefKind.TABLE_CELL,
                        source_id=value,
                    )
                    for value in tree.source_cell_ids
                ),
                *(
                    StructureSourceRef(
                        kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
                        source_id=value,
                    )
                    for value in tree.visual_text_observation_ids
                ),
            }
            external_ref_count = sum(
                ref not in source_ref_catalog
                for node in view.nodes
                for ref in node.source_refs
            )
            if external_ref_count:
                return failed(
                    "recursive_external_standard_expansion_observed",
                    external_source_reference_count=external_ref_count,
                )
            return passed(
                "external_standards_not_recursively_expanded",
                external_source_reference_count=0,
                reference_projection_count=reference_projection_count,
            )
        warning_count = _structure_finding_count(
            tree,
            "missing_linked_standard",
            "external_reference_content_not_loaded",
        )
        if warning_count < 1:
            return failed(
                "missing_linked_standard_warning_absent",
                missing_linked_standard_warning_count=0,
                fabricated_external_content_count=0,
            )
        return passed(
            "missing_linked_standard_warns_without_fabrication",
            missing_linked_standard_warning_count=warning_count,
            fabricated_external_content_count=0,
        )

    if assertion_id.startswith("FMT-003"):
        policy_count = sum(
            int(
                (result.get("source_ir_diagnostic_codes") or {}).get(
                    "pdf_annotations_ignored_by_policy",
                    0,
                )
            )
            for result in document_results.values()
        )
        if not policy_count:
            return failed(
                "annotation_policy_evidence_missing",
                annotation_policy_diagnostic_count=0,
            )
        source_ref_catalog = {
            *(
                StructureSourceRef(
                    kind=StructureSourceRefKind.SOURCE_NODE,
                    source_id=value,
                )
                for value in tree.source_node_ids
            ),
            *(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=value,
                )
                for value in tree.source_cell_ids
            ),
            *(
                StructureSourceRef(
                    kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
                    source_id=value,
                )
                for value in tree.visual_text_observation_ids
            ),
        }
        ungrounded_projection_ref_count = sum(
            ref not in source_ref_catalog
            for node in view.nodes
            for ref in node.source_refs
        )
        semantic_annotation_conflict_count = sum(
            "annotation" in code or "conflict" in code
            for annotation in semantics.annotations
            for code in annotation.reason_codes
        )
        if assertion_id == "FMT-003-A3":
            if ungrounded_projection_ref_count:
                return failed(
                    "annotation_requirement_projection_observed",
                    annotation_requirement_count=ungrounded_projection_ref_count,
                    annotation_policy_diagnostic_count=policy_count,
                )
            return passed(
                "annotations_cannot_ground_requirement_projections",
                annotation_requirement_count=0,
                annotation_policy_diagnostic_count=policy_count,
            )
        if semantic_annotation_conflict_count:
            return failed(
                "annotation_value_conflict_event_observed",
                annotation_conflict_event_count=semantic_annotation_conflict_count,
                annotation_policy_diagnostic_count=policy_count,
            )
        return passed(
            "annotation_values_do_not_enter_semantic_conflicts",
            annotation_conflict_event_count=0,
            annotation_policy_diagnostic_count=policy_count,
        )

    if assertion_id.startswith("FMT-006"):
        section = _unique_section_node(tree, "9.16.3")
        if section is None:
            return failed("section_9_16_3_not_unique", section_match_count=0)
        descendants = _structure_descendant_ids(tree, section.structure_node_id)
        assets_by_id = {asset.asset_id: asset for asset in source_snapshot.assets}
        figure_source_nodes = [
            node
            for node in source_snapshot.nodes
            if node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            and node.asset_ref is not None
            and assets_by_id[node.asset_ref].content_sha256
            == "315c2f7e3e490454a4c9ed5b0bf00337aed4d44f6f66b9680e41b6303f1aa8d9"
        ]
        figure_refs = {
            _source_node_ref(node.source_node_id) for node in figure_source_nodes
        }
        figure_structure_ids = {
            node.structure_node_id
            for node in tree.nodes
            if set(node.source_refs) & figure_refs
        }
        if assertion_id == "FMT-006-A2":
            reference_relation_count = sum(
                relation.kind is StructureRelationKind.REFERENCES
                and (
                    (
                        relation.source_structure_node_id in descendants
                        and relation.target_structure_node_id in figure_structure_ids
                    )
                    or (
                        relation.target_structure_node_id in descendants
                        and relation.source_structure_node_id in figure_structure_ids
                    )
                )
                for relation in tree.relations
            )
            if (
                len(figure_source_nodes) != 1
                or len(figure_structure_ids) != 1
                or reference_relation_count < 2
            ):
                return failed(
                    "figure_section_reference_mismatch",
                    figure_source_count=len(figure_source_nodes),
                    figure_structure_count=len(figure_structure_ids),
                    reference_relation_count=reference_relation_count,
                )
            return passed(
                "figure_related_by_body_reference_number_and_caption",
                figure_source_count=1,
                figure_structure_count=1,
                reference_relation_count=reference_relation_count,
            )
        section_annotations = annotations_by_owner.get(
            section.structure_node_id,
            [],
        )
        primary = [
            node
            for node in view.nodes
            if node.owner_structure_node_id == section.structure_node_id
            and node.role is RequirementProjectionRole.PRIMARY
            and node.decision_mode is RequirementDecisionMode.ROLLUP
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.requirement_subtype is RequirementSubtype.PRODUCT_REQUIREMENT
        ]
        if assertion_id == "FMT-006-A3":
            content_children = [
                node
                for node in view.nodes
                if node.owner_structure_node_id in descendants
                and node.role is RequirementProjectionRole.CONTENT
                and node.parent_requirement_view_node_id is not None
            ]
            semantic_matches = [
                annotation
                for annotation in section_annotations
                if annotation.semantic_class is SemanticClass.REQUIREMENT
                and annotation.requirement_subtype
                is RequirementSubtype.PRODUCT_REQUIREMENT
            ]
            if (
                len(semantic_matches) != 1
                or len(primary) != 1
                or len(content_children) < 5
            ):
                return failed(
                    "figure_section_requirement_semantic_mismatch",
                    product_requirement_semantic_count=len(semantic_matches),
                    primary_rollup_requirement_count=len(primary),
                    content_child_count=len(content_children),
                )
            return passed(
                "figure_section_keeps_main_requirement_and_definitions",
                product_requirement_semantic_count=1,
                primary_rollup_requirement_count=1,
                content_child_count=len(content_children),
            )
        figure_requirement_count = sum(
            (
                node.owner_structure_node_id in figure_structure_ids
                or bool(set(node.source_refs) & figure_refs)
            )
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.decision_mode is not RequirementDecisionMode.NONE
            for node in view.nodes
        )
        figure_content_count = sum(
            (
                node.owner_structure_node_id in figure_structure_ids
                or bool(set(node.source_refs) & figure_refs)
            )
            and node.role is RequirementProjectionRole.CONTENT
            and node.decision_mode is RequirementDecisionMode.NONE
            for node in view.nodes
        )
        if assertion_id == "FMT-006-A4":
            if figure_requirement_count:
                return failed(
                    "figure_ocr_requirement_projection_observed",
                    figure_requirement_count=figure_requirement_count,
                    figure_content_projection_count=figure_content_count,
                )
            return passed(
                "figure_ocr_not_projected_as_requirements",
                figure_requirement_count=0,
                figure_content_projection_count=figure_content_count,
            )
        warning_count = _structure_finding_count(
            tree,
            "unstructured_visual_requirement",
        )
        if warning_count < 1:
            return failed(
                "unstructured_visual_requirement_warning_absent",
                unstructured_visual_requirement_warning_count=0,
                figure_requirement_count=figure_requirement_count,
            )
        return passed(
            "critical_visual_value_warns_without_mutation",
            unstructured_visual_requirement_warning_count=warning_count,
            figure_requirement_count=figure_requirement_count,
        )

    return failed("confirmed_case_assertion_not_implemented")


def _section_evaluator_context(
    *,
    evaluator_id: str,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[Any | None, Any | None, Any | None, dict[str, Any], dict[str, Any] | None]:
    evidence: dict[str, Any] = {
        "document_count": len(case.get("document_ids") or []),
        "section_locator_count": 0,
        "section_match_count": 0,
        "source_ir_unavailable_count": 0,
        "structure_unavailable_count": 0,
        "resolver_id": DEFAULT_STRUCTURE_RESOLVER_ID,
        "resolver_version": DEFAULT_STRUCTURE_RESOLVER_VERSION,
    }

    def failed(reason_code: str, **extra: Any) -> dict[str, Any]:
        evidence.update(extra)
        return {
            "status": "FAIL",
            "reason_code": reason_code,
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    document_ids = list(case.get("document_ids") or [])
    if len(document_ids) != 1:
        return (
            None,
            None,
            None,
            evidence,
            failed("section_evaluator_scope_invalid"),
        )
    document_id = document_ids[0]
    locators = [
        locator
        for locator in case.get("locators", [])
        if isinstance(locator, dict)
        and locator.get("document_id") == document_id
        and locator.get("kind") == "section"
    ]
    evidence["section_locator_count"] = len(locators)
    if len(locators) != 1:
        return (
            None,
            None,
            None,
            evidence,
            failed("section_locator_not_unique"),
        )
    section_label = str(locators[0].get("pointer") or "")
    if _SECTION_POINTER_PATTERN.fullmatch(section_label) is None:
        return (
            None,
            None,
            None,
            evidence,
            failed("section_locator_invalid"),
        )

    document_result = document_results.get(document_id)
    if document_result is None or document_result.get("source_ir_status") not in {
        "COMPLETE",
        "PARTIAL",
        "REUSED_IDENTICAL_SOURCE",
    }:
        evidence["source_ir_unavailable_count"] = 1
        return (
            None,
            None,
            None,
            evidence,
            failed("source_ir_unavailable_for_section_evaluator"),
        )
    source_key = (
        str(document_result.get("source_sha256") or ""),
        str(
            document_result.get("detected_format")
            or document_result.get("declared_format")
            or ""
        ),
    )
    source_snapshot = source_ir_snapshots.get(source_key)
    if source_snapshot is None:
        evidence["source_ir_unavailable_count"] = 1
        return (
            None,
            None,
            None,
            evidence,
            failed("source_ir_snapshot_unavailable_for_section_evaluator"),
        )
    structure_key = (
        source_snapshot.source_ir_id,
        DEFAULT_STRUCTURE_RESOLVER_ID,
        DEFAULT_STRUCTURE_RESOLVER_VERSION,
    )
    tree = structure_snapshots.get(structure_key)
    if tree is None:
        try:
            tree = build_structure_tree(
                source_snapshot,
                resolver_id=DEFAULT_STRUCTURE_RESOLVER_ID,
                resolver_version=DEFAULT_STRUCTURE_RESOLVER_VERSION,
            )
        except StructureResolutionRejected as exc:
            evidence["structure_unavailable_count"] = 1
            return (
                None,
                None,
                None,
                evidence,
                failed(
                    "structure_resolution_rejected",
                    structure_rejection_code=exc.code.value,
                ),
            )
        except Exception as exc:
            evidence["structure_unavailable_count"] = 1
            return (
                None,
                None,
                None,
                evidence,
                failed(
                    "structure_resolution_failed",
                    structure_error_type=type(exc).__name__,
                ),
            )
    if (
        tree.source_ir_id != source_snapshot.source_ir_id
        or tree.resolver_id != DEFAULT_STRUCTURE_RESOLVER_ID
        or tree.resolver_version != DEFAULT_STRUCTURE_RESOLVER_VERSION
    ):
        evidence["structure_unavailable_count"] = 1
        return (
            None,
            None,
            None,
            evidence,
            failed("structure_resolver_identity_mismatch"),
        )
    structure_snapshots[structure_key] = tree
    matches = [
        node
        for node in tree.nodes
        if node.kind is StructureNodeKind.SECTION
        and node.explicit_label == section_label
    ]
    evidence["section_match_count"] = len(matches)
    if len(matches) != 1:
        return (
            None,
            None,
            None,
            evidence,
            failed("section_locator_not_unique"),
        )
    return source_snapshot, tree, matches[0], evidence, None


def _classify_document_for_section(
    *,
    evaluator_id: str,
    source_snapshot: Any,
    tree: Any,
    section: Any,
    evidence: dict[str, Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[Any | None, Any | None, dict[str, Any] | None]:
    evidence.update(
        {
            "classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
            "classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            "semantic_annotation_count": 0,
        }
    )
    semantic_key = (
        tree.structure_snapshot_id,
        DEFAULT_SEMANTIC_CLASSIFIER_ID,
        DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    )
    try:
        semantics = semantic_snapshots.get(semantic_key)
        if semantics is None:
            semantics = classify_content(
                source_snapshot,
                tree,
                classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            )
        validate_semantic_classification_against_inputs(
            semantics,
            source_snapshot,
            tree,
        )
    except SemanticClassificationRejected as exc:
        evidence["semantic_rejection_code"] = exc.code.value
        return (
            None,
            None,
            {
                "status": "FAIL",
                "reason_code": "semantic_classification_rejected",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    except Exception as exc:
        evidence["semantic_error_type"] = type(exc).__name__
        return (
            None,
            None,
            {
                "status": "FAIL",
                "reason_code": "semantic_classification_failed",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    semantic_snapshots[semantic_key] = semantics
    evidence["semantic_scope_count"] = len(semantics.scope_structure_node_ids)
    annotations = [
        annotation
        for annotation in semantics.annotations
        if annotation.target_structure_node_id == section.structure_node_id
    ]
    evidence["semantic_annotation_count"] = len(annotations)
    if len(annotations) != 1:
        return (
            semantics,
            None,
            {
                "status": "FAIL",
                "reason_code": "section_semantic_annotation_not_unique",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    return semantics, annotations[0], None


def _evaluate_section_requirement_subtype(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    expected_subtype: str,
) -> dict[str, Any]:
    evaluator_id = SECTION_REQUIREMENT_SUBTYPE_EVALUATOR_ID
    source_snapshot, tree, section, evidence, error = _section_evaluator_context(
        evaluator_id=evaluator_id,
        case=case,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if error is not None:
        return error
    assert source_snapshot is not None and tree is not None and section is not None
    _, annotation, error = _classify_document_for_section(
        evaluator_id=evaluator_id,
        source_snapshot=source_snapshot,
        tree=tree,
        section=section,
        evidence=evidence,
        semantic_snapshots=semantic_snapshots,
    )
    if error is not None:
        return error
    assert annotation is not None
    expected = RequirementSubtype(expected_subtype)
    evidence.update(
        {
            "requirement_semantic_count": int(
                annotation.semantic_class is SemanticClass.REQUIREMENT
            ),
            "expected_requirement_subtype": expected.value,
            "observed_requirement_subtype": (
                annotation.requirement_subtype.value
                if annotation.requirement_subtype is not None
                else None
            ),
            "semantic_annotation_status": annotation.status.value,
        }
    )
    if (
        annotation.semantic_class is not SemanticClass.REQUIREMENT
        or annotation.requirement_subtype is not expected
        or annotation.status
        not in {
            SemanticAnnotationStatus.CANDIDATE,
            SemanticAnnotationStatus.CONFIRMED,
        }
    ):
        return {
            "status": "FAIL",
            "reason_code": "section_requirement_subtype_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "section_requirement_subtype_observed",
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_section_requirement_projection(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
    evaluator_id: str = SECTION_REQUIREMENT_PROJECTION_EVALUATOR_ID,
    expected_decision_mode: RequirementDecisionMode = RequirementDecisionMode.DIRECT,
) -> dict[str, Any]:
    source_snapshot, tree, section, evidence, error = _section_evaluator_context(
        evaluator_id=evaluator_id,
        case=case,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if error is not None:
        return error
    assert source_snapshot is not None and tree is not None and section is not None
    semantics, annotation, error = _classify_document_for_section(
        evaluator_id=evaluator_id,
        source_snapshot=source_snapshot,
        tree=tree,
        section=section,
        evidence=evidence,
        semantic_snapshots=semantic_snapshots,
    )
    if error is not None:
        return error
    assert semantics is not None and annotation is not None
    evidence.update(
        {
            "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            "background_annotation_count": int(
                annotation.semantic_class is SemanticClass.BACKGROUND
            ),
            "semantic_annotation_status": annotation.status.value,
            "projected_target_count": 0,
            "primary_expected_requirement_count": 0,
            "expected_decision_mode": expected_decision_mode.value,
        }
    )
    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return {
            "status": "FAIL",
            "reason_code": "requirement_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "requirement_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view
    projected = [
        node
        for node in view.nodes
        if node.target_structure_node_id == section.structure_node_id
    ]
    primary_expected = [
        node
        for node in projected
        if node.role is RequirementProjectionRole.PRIMARY
        and node.decision_mode is expected_decision_mode
        and node.semantic_class is SemanticClass.REQUIREMENT
        and node.requirement_subtype is annotation.requirement_subtype
        and node.parent_requirement_view_node_id is None
    ]
    evidence["projected_target_count"] = len(projected)
    evidence["primary_expected_requirement_count"] = len(primary_expected)
    if expected_decision_mode is RequirementDecisionMode.DIRECT:
        evidence["primary_direct_requirement_count"] = len(primary_expected)
    else:
        evidence["primary_rollup_requirement_count"] = len(primary_expected)
    if (
        annotation.semantic_class is SemanticClass.BACKGROUND
        or annotation.status
        not in {
            SemanticAnnotationStatus.CANDIDATE,
            SemanticAnnotationStatus.CONFIRMED,
        }
        or len(projected) != 1
        or len(primary_expected) != 1
    ):
        return {
            "status": "FAIL",
            "reason_code": "section_primary_direct_projection_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": (
            "section_main_rollup_requirement_observed"
            if evaluator_id == SECTION_MAIN_ROLLUP_EVALUATOR_ID
            else "section_main_requirement_observed"
            if evaluator_id == SECTION_MAIN_REQUIREMENT_EVALUATOR_ID
            else "section_primary_direct_not_background_observed"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_source_hash_deduplication(
    *,
    case: dict[str, Any],
    manifest_documents: dict[str, dict[str, Any]],
    document_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    declared_pairs = [
        (document_id, str(manifest_documents[document_id].get("duplicate_of") or ""))
        for document_id in case["document_ids"]
        if manifest_documents[document_id].get("duplicate_of")
    ]
    if not declared_pairs:
        return {
            "status": "GAP",
            "reason_code": "duplicate_gold_pair_missing",
            "evaluator": "source_ir.source_hash_dedup/v1",
            "evidence": {},
        }

    related = [document_results[document_id] for document_id in case["document_ids"]]
    unavailable = [
        row
        for row in related
        if row.get("source_ir_status")
        not in {"COMPLETE", "PARTIAL", "REUSED_IDENTICAL_SOURCE"}
    ]
    pair_hashes_match = all(
        canonical_id in document_results
        and document_results[duplicate_id].get("source_sha256")
        == document_results[canonical_id].get("source_sha256")
        for duplicate_id, canonical_id in declared_pairs
    )
    reused_count = sum(
        1 for row in related if row.get("source_ir_status") == "REUSED_IDENTICAL_SOURCE"
    )
    unique_hash_count = len({str(row.get("source_sha256") or "") for row in related})
    evidence = {
        "declared_duplicate_pair_count": len(declared_pairs),
        "reused_document_count": reused_count,
        "unique_source_hash_count": unique_hash_count,
        "source_ir_unavailable_count": len(unavailable),
    }
    if unavailable:
        return {
            "status": "FAIL",
            "reason_code": "source_ir_unavailable_for_duplicate_pair",
            "evaluator": "source_ir.source_hash_dedup/v1",
            "evidence": evidence,
        }
    if not pair_hashes_match or reused_count < 1:
        return {
            "status": "FAIL",
            "reason_code": "source_hash_deduplication_not_observed",
            "evaluator": "source_ir.source_hash_dedup/v1",
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "source_hash_deduplication_observed",
        "evaluator": "source_ir.source_hash_dedup/v1",
        "evidence": evidence,
    }


def _evaluate_opaque_attachment_identity(
    *,
    case: dict[str, Any],
    manifest_documents: dict[str, dict[str, Any]],
    document_results: dict[str, dict[str, Any]],
    attachment_snapshots: dict[str, OpaqueAttachmentExtraction],
) -> dict[str, Any]:
    unavailable_count = 0
    identity_mismatch_count = 0
    verified_artifact_count = 0
    for document_id in case["document_ids"]:
        extraction = attachment_snapshots.get(document_id)
        result = document_results.get(document_id)
        manifest_document = manifest_documents[document_id]
        if extraction is None or result is None:
            unavailable_count += 1
            continue
        snapshot = extraction.disposition
        source_ir = extraction.source_ir
        attachment_nodes = tuple(
            node for node in source_ir.nodes if node.kind is SourceNodeKind.ATTACHMENT
        )
        attachment_assets = tuple(
            asset
            for asset in source_ir.assets
            if asset.kind is SourceAssetKind.ATTACHMENT
        )
        expected_revision_id = str(manifest_document.get("upload_revision_id") or "")
        matches = bool(
            snapshot.context.logical_document_id == document_id
            and snapshot.context.upload_revision_id == expected_revision_id
            and snapshot.context.source_sha256
            == str(manifest_document.get("source_sha256") or "")
            and snapshot.context.source_size == int(result.get("source_size") or 0)
            and snapshot.artifact.source_sha256 == snapshot.context.source_sha256
            and snapshot.artifact.source_size == snapshot.context.source_size
            and snapshot.artifact.store_verified
            and snapshot.attachment_artifact_count == 1
            and len(attachment_nodes) == 1
            and len(attachment_assets) == 1
            and attachment_nodes[0].asset_ref == attachment_assets[0].asset_id
            and attachment_assets[0].content_sha256 == snapshot.context.source_sha256
            and source_ir.source_sha256 == snapshot.context.source_sha256
            and source_ir.status.value == "COMPLETE"
            and source_ir.adapter_id == "opaque-attachment-source-ir"
            and not source_ir.visual_text_snapshots
            and snapshot.detected_format.value
            == str(result.get("detected_format") or "")
            and result.get("attachment_status") == "COMPLETE"
        )
        if matches:
            verified_artifact_count += 1
        else:
            identity_mismatch_count += 1
    evidence = {
        "document_count": len(case["document_ids"]),
        "attachment_snapshot_unavailable_count": unavailable_count,
        "identity_mismatch_count": identity_mismatch_count,
        "verified_artifact_count": verified_artifact_count,
    }
    if unavailable_count or identity_mismatch_count:
        return {
            "status": "FAIL",
            "reason_code": "opaque_attachment_identity_mismatch",
            "evaluator": ATTACHMENT_IDENTITY_EVALUATOR_ID,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "opaque_attachment_identity_preserved",
        "evaluator": ATTACHMENT_IDENTITY_EVALUATOR_ID,
        "evidence": evidence,
    }


def _evaluate_attachment_disposition(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    attachment_snapshots: dict[str, OpaqueAttachmentExtraction],
) -> dict[str, Any]:
    unavailable_count = 0
    mismatch_count = 0
    attachment_disposition_count = 0
    for document_id in case["document_ids"]:
        extraction = attachment_snapshots.get(document_id)
        result = document_results.get(document_id)
        if extraction is None or result is None:
            unavailable_count += 1
            continue
        snapshot = extraction.disposition
        semantic_classes = tuple(
            annotation.semantic_class for annotation in extraction.semantics.annotations
        )
        matches = bool(
            semantic_classes == (SemanticClass.ATTACHMENT,)
            and snapshot.semantic_class is SemanticClass.ATTACHMENT
            and snapshot.reference.semantic_class is SemanticClass.ATTACHMENT
            and snapshot.context.document_role
            is ExtractionDocumentRole.CUSTOMER_DRAWING
            and snapshot.context.ingress_route is ExtractionIngressRoute.ATTACHMENT
            and result.get("document_role") == "customer_drawing"
            and result.get("ingress_route") == "attachment"
        )
        if matches:
            attachment_disposition_count += 1
        else:
            mismatch_count += 1
    evidence = {
        "document_count": len(case["document_ids"]),
        "attachment_snapshot_unavailable_count": unavailable_count,
        "attachment_disposition_count": attachment_disposition_count,
        "disposition_mismatch_count": mismatch_count,
    }
    if unavailable_count or mismatch_count:
        return {
            "status": "FAIL",
            "reason_code": "attachment_disposition_mismatch",
            "evaluator": ATTACHMENT_DISPOSITION_EVALUATOR_ID,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "documents_classified_as_opaque_attachments",
        "evaluator": ATTACHMENT_DISPOSITION_EVALUATOR_ID,
        "evidence": evidence,
    }


def _evaluate_attachment_reference_only(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    attachment_snapshots: dict[str, OpaqueAttachmentExtraction],
) -> dict[str, Any]:
    unavailable_count = 0
    mismatch_count = 0
    reference_count = 0
    decision_requirement_count = 0
    for document_id in case["document_ids"]:
        extraction = attachment_snapshots.get(document_id)
        if extraction is None or document_id not in document_results:
            unavailable_count += 1
            continue
        snapshot = extraction.disposition
        projected_decision_count = sum(
            node.decision_mode.value in {"DIRECT", "ROLLUP"}
            for node in extraction.requirement_view.nodes
        )
        decision_requirement_count += projected_decision_count
        matches = bool(
            snapshot.attachment_reference_count == 1
            and snapshot.reference.role is AttachmentReferenceRole.REFERENCE
            and snapshot.reference.decision_mode is AttachmentDecisionMode.NONE
            and snapshot.decision_requirement_count == 0
            and projected_decision_count == 0
            and len(extraction.requirement_view.nodes) == 0
        )
        if matches:
            reference_count += 1
        else:
            mismatch_count += 1
    evidence = {
        "document_count": len(case["document_ids"]),
        "attachment_snapshot_unavailable_count": unavailable_count,
        "attachment_reference_count": reference_count,
        "decision_requirement_count": decision_requirement_count,
        "projection_mismatch_count": mismatch_count,
    }
    if unavailable_count or mismatch_count or decision_requirement_count:
        return {
            "status": "FAIL",
            "reason_code": "attachment_projection_not_reference_only",
            "evaluator": ATTACHMENT_PROJECTION_EVALUATOR_ID,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "attachment_reference_only_projection",
        "evaluator": ATTACHMENT_PROJECTION_EVALUATOR_ID,
        "evidence": evidence,
    }


def _evaluate_attachment_workflow_policy(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    attachment_snapshots: dict[str, OpaqueAttachmentExtraction],
) -> dict[str, Any]:
    unavailable_count = 0
    mismatch_count = 0
    backlog_count = 0
    ocr_provider_count = 0
    visual_text_snapshot_count = 0
    blocking_count = 0
    for document_id in case["document_ids"]:
        extraction = attachment_snapshots.get(document_id)
        result = document_results.get(document_id)
        if extraction is None or result is None:
            unavailable_count += 1
            continue
        snapshot = extraction.disposition
        actual_visual_snapshots = extraction.source_ir.visual_text_snapshots
        actual_provider_ids = {item.provider_id for item in actual_visual_snapshots}
        ocr_provider_count += len(actual_provider_ids)
        visual_text_snapshot_count += len(actual_visual_snapshots)
        blocking_count += int(snapshot.blocks_requirement_source_extraction)
        matches = bool(
            snapshot.deep_recognition_mode is DeepRecognitionMode.BACKLOG
            and not snapshot.blocks_requirement_source_extraction
            and not snapshot.ocr_provider_ids
            and not actual_provider_ids
            and not actual_visual_snapshots
            and result.get("source_ir_status") == "ATTACHMENT_ONLY"
        )
        if matches:
            backlog_count += 1
        else:
            mismatch_count += 1
    evidence = {
        "document_count": len(case["document_ids"]),
        "attachment_snapshot_unavailable_count": unavailable_count,
        "deep_recognition_backlog_count": backlog_count,
        "ocr_provider_count": ocr_provider_count,
        "visual_text_snapshot_count": visual_text_snapshot_count,
        "requirement_source_blocking_count": blocking_count,
        "workflow_mismatch_count": mismatch_count,
    }
    if (
        unavailable_count
        or mismatch_count
        or ocr_provider_count
        or visual_text_snapshot_count
        or blocking_count
    ):
        return {
            "status": "FAIL",
            "reason_code": "deep_drawing_backlog_policy_mismatch",
            "evaluator": ATTACHMENT_WORKFLOW_EVALUATOR_ID,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "deep_drawing_backlog_is_nonblocking",
        "evaluator": ATTACHMENT_WORKFLOW_EVALUATOR_ID,
        "evidence": evidence,
    }


def _evaluate_pdf_annotation_policy(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    related = [document_results[document_id] for document_id in case["document_ids"]]
    unavailable = [
        row
        for row in related
        if row.get("source_ir_status")
        not in {"COMPLETE", "PARTIAL", "REUSED_IDENTICAL_SOURCE"}
    ]
    policy_diagnostic_count = sum(
        int(
            (row.get("source_ir_diagnostic_codes") or {}).get(
                "pdf_annotations_ignored_by_policy", 0
            )
        )
        for row in related
    )
    evidence = {
        "documents_with_annotation_policy_evidence": sum(
            1
            for row in related
            if int(
                (row.get("source_ir_diagnostic_codes") or {}).get(
                    "pdf_annotations_ignored_by_policy", 0
                )
            )
            > 0
        ),
        "source_ir_unavailable_count": len(unavailable),
    }
    if unavailable:
        return {
            "status": "FAIL",
            "reason_code": "source_ir_unavailable_for_annotation_policy",
            "evaluator": "source_ir.pdf_annotation_policy/v1",
            "evidence": evidence,
        }
    if policy_diagnostic_count < 1:
        return {
            "status": "FAIL",
            "reason_code": "annotation_policy_evidence_missing",
            "evaluator": "source_ir.pdf_annotation_policy/v1",
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "annotation_layer_detected_and_excluded",
        "evaluator": "source_ir.pdf_annotation_policy/v1",
        "evidence": evidence,
    }


def _evaluate_pdf_registered_source_identity(
    *,
    case: dict[str, Any],
    manifest_documents: dict[str, dict[str, Any]],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_REGISTERED_SOURCE_IDENTITY_EVALUATOR_ID
    scopes = _pdf_document_region_scopes(case)
    evidence = {
        "document_count": len(case["document_ids"]),
        "verified_document_count": 0,
        "source_identity_mismatch_count": 0,
        "page_inventory_mismatch_count": 0,
        "source_ir_unavailable_count": 0,
    }
    for document_id in case["document_ids"]:
        registered = manifest_documents.get(document_id) or {}
        result = document_results.get(document_id) or {}
        scope = scopes[document_id]
        snapshot = _source_ir_snapshot_for_document_result(
            result,
            source_ir_snapshots=source_ir_snapshots,
        )
        registered_sha256 = str(registered.get("source_sha256") or "")
        identity_matches = bool(
            registered.get("format") == "pdf"
            and result.get("declared_format") == "pdf"
            and result.get("detected_format") == "pdf"
            and result.get("identity_status") == "VERIFIED"
            and result.get("preflight_status") == "READY"
            and str(result.get("source_sha256") or "") == registered_sha256
            and snapshot is not None
            and snapshot.source_sha256 == registered_sha256
            and result.get("source_ir_status")
            in {"COMPLETE", "PARTIAL", "REUSED_IDENTICAL_SOURCE"}
        )
        page_inventory_matches = bool(
            isinstance(result.get("page_count"), int)
            and not isinstance(result.get("page_count"), bool)
            and int(result["page_count"]) == scope["body_end"]
        )
        evidence["source_identity_mismatch_count"] += int(not identity_matches)
        evidence["page_inventory_mismatch_count"] += int(not page_inventory_matches)
        evidence["source_ir_unavailable_count"] += int(snapshot is None)
        evidence["verified_document_count"] += int(
            identity_matches and page_inventory_matches
        )
    mismatch_count = int(evidence["source_identity_mismatch_count"]) + int(
        evidence["page_inventory_mismatch_count"]
    )
    if mismatch_count:
        return {
            "status": "FAIL",
            "reason_code": "registered_pdf_source_identity_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "registered_pdf_source_bytes_preserved",
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_reviewed_body_sequence(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    reviewed_body_profiles_by_document: dict[str, dict[str, Any]],
    verified_review_profiles_by_document: Mapping[str, VerifiedPdfBodyReviewProfile],
    pdf_visual_provider: str = PDF_VISUAL_PROVIDER_OFF,
    visual_runtime_identities_by_document: Mapping[str, Mapping[str, str]]
    | None = None,
) -> dict[str, Any]:
    """Compare SourceIR body channels with independently reviewed page profiles.

    The evaluator never creates or updates a reviewed profile.  Canonical
    digests stay inside the comparison and are deliberately absent from its
    report evidence.  Text, image and table order is preserved within each
    channel; this contract does not claim cross-channel visual interleaving.
    """

    evaluator_id = PDF_REVIEWED_BODY_SEQUENCE_EVALUATOR_ID
    evidence: dict[str, Any] = {
        "document_count": len(case.get("document_ids") or []),
        "profile_count": len(reviewed_body_profiles_by_document),
        "pending_profile_count": 0,
        "ready_profile_count": 0,
        "ready_native_profile_count": 0,
        "ready_remote_visual_profile_count": 0,
        "pending_page_count": 0,
        "reviewed_page_count": 0,
        "observed_page_count": 0,
        "remote_visual_not_run_page_count": 0,
        "missing_visual_page_count": 0,
        "provider_profile_mismatch_page_count": 0,
        "image_inventory_mismatch_page_count": 0,
        "table_inventory_mismatch_page_count": 0,
        "page_image_mismatch_count": 0,
        "body_content_mismatch_count": 0,
        "body_order_mismatch_count": 0,
        "body_sequence_mismatch_count": 0,
        "expected_body_line_count": 0,
        "observed_body_line_count": 0,
        "expected_image_count": 0,
        "observed_image_count": 0,
        "expected_table_count": 0,
        "observed_table_count": 0,
        "expected_table_row_count": 0,
        "observed_table_row_count": 0,
        "expected_table_cell_count": 0,
        "observed_table_cell_count": 0,
        "expected_merged_region_count": 0,
        "observed_merged_region_count": 0,
        "pending_pages": [],
        "remote_visual_not_run_pages": [],
        "missing_visual_pages": [],
        "provider_profile_mismatch_pages": [],
        "image_inventory_mismatch_pages": [],
        "table_inventory_mismatch_pages": [],
        "content_mismatch_pages": [],
        "order_mismatch_pages": [],
        "mismatched_pages": [],
    }

    expected_document_ids = list(case.get("document_ids") or [])
    if set(reviewed_body_profiles_by_document) != set(expected_document_ids):
        return {
            "status": "GAP",
            "reason_code": "pdf_body_review_profile_incomplete",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    ready_document_ids: list[str] = []
    pending_pages: list[tuple[str, int]] = []
    verified_profiles = verified_review_profiles_by_document
    visual_runtime_identities = visual_runtime_identities_by_document or {}
    for document_id in expected_document_ids:
        profile = reviewed_body_profiles_by_document[document_id]
        page_start = int(profile["body_page_start"])
        page_end = int(profile["body_page_end"])
        if profile.get("status") == "PENDING" or not profile.get("review_approved"):
            evidence["pending_profile_count"] += 1
            evidence["pending_page_count"] += page_end - page_start + 1
            pending_pages.extend(
                (document_id, page_number)
                for page_number in range(page_start, page_end + 1)
            )
        else:
            verified_profile = verified_profiles.get(document_id)
            if verified_profile is None or verified_profile.ready_profile != profile:
                raise ValueError("pdf_body_review_verified_profile_missing_or_changed")
            evidence["ready_profile_count"] += 1
            if profile.get("review_mode") == "NATIVE":
                evidence["ready_native_profile_count"] += 1
            else:
                evidence["ready_remote_visual_profile_count"] += 1
            ready_document_ids.append(document_id)
    evidence["pending_pages"] = _safe_pdf_page_refs(pending_pages)
    if not ready_document_ids:
        return {
            "status": "GAP",
            "reason_code": "pdf_body_review_pending",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    source_ir_unavailable_count = 0
    source_identity_mismatch_count = 0
    remote_visual_not_run_pages: list[tuple[str, int]] = []
    missing_visual_pages: list[tuple[str, int]] = []
    provider_profile_mismatch_pages: list[tuple[str, int]] = []
    image_inventory_mismatch_pages: list[tuple[str, int]] = []
    table_inventory_mismatch_pages: list[tuple[str, int]] = []
    image_mismatch_pages: list[tuple[str, int]] = []
    content_mismatch_pages: list[tuple[str, int]] = []
    order_mismatch_pages: list[tuple[str, int]] = []
    sequence_mismatch_pages: list[tuple[str, int]] = []
    for document_id in ready_document_ids:
        profile = reviewed_body_profiles_by_document[document_id]
        expected_pages = list(profile.get("pages") or [])
        evidence["reviewed_page_count"] += len(expected_pages)
        for expected_page in expected_pages:
            for count_name in (
                "body_line_count",
                "image_count",
                "table_count",
                "table_row_count",
                "table_cell_count",
                "merged_region_count",
            ):
                evidence[f"expected_{count_name}"] += int(
                    expected_page[f"expected_{count_name}"]
                )
        result = document_results.get(document_id) or {}
        snapshot = _source_ir_snapshot_for_document_result(
            result,
            source_ir_snapshots=source_ir_snapshots,
        )
        if snapshot is None:
            source_ir_unavailable_count += 1
            continue
        if snapshot.source_sha256 != profile.get("source_sha256") or str(
            result.get("source_sha256") or ""
        ) != profile.get("source_sha256"):
            source_identity_mismatch_count += 1
            continue

        verified_profile = verified_profiles[document_id]
        reviewed_profile = verified_profile.reviewed_profile
        reviewed_pages_by_number = {
            int(page["page_number"]): page for page in reviewed_profile["pages"]
        }
        review_mode = str(profile.get("review_mode") or "")
        visual_pages: dict[int, list[Any]] = {}
        visual_furniture_ids: frozenset[str] = frozenset()
        provider_profile_mismatch_page_numbers: set[int] = set()
        if review_mode == "REMOTE_VISUAL":
            if (
                pdf_visual_provider == PDF_VISUAL_PROVIDER_OFF
                or profile.get("approved_visual_provider") != pdf_visual_provider
            ):
                remote_visual_not_run_pages.extend(
                    (document_id, int(page["page_number"])) for page in expected_pages
                )
                continue
            approved_provider_id = str(reviewed_profile.get("visual_provider_id") or "")
            approved_provider_version_sha256 = str(
                reviewed_profile.get("visual_text_provider_version_sha256") or ""
            )
            expected_page_numbers = {
                int(page["page_number"]) for page in expected_pages
            }
            runtime_identity = visual_runtime_identities.get(document_id, {})
            if (
                runtime_identity.get("visual_text_provider_id") != approved_provider_id
                or runtime_identity.get("visual_text_provider_version_sha256")
                != approved_provider_version_sha256
            ):
                provider_profile_mismatch_page_numbers.update(expected_page_numbers)
            provider_snapshots = tuple(
                visual_snapshot
                for visual_snapshot in getattr(
                    snapshot,
                    "visual_text_snapshots",
                    (),
                )
                if visual_snapshot.provider_id == approved_provider_id
            )
            approved_provider_snapshots: list[Any] = []
            for visual_snapshot in provider_snapshots:
                if (
                    hashlib.sha256(
                        visual_snapshot.provider_version.encode("utf-8")
                    ).hexdigest()
                    != approved_provider_version_sha256
                ):
                    if visual_snapshot.page_number in expected_page_numbers:
                        provider_profile_mismatch_page_numbers.add(
                            visual_snapshot.page_number
                        )
                    continue
                approved_provider_snapshots.append(visual_snapshot)
            provider_profile_mismatch_pages.extend(
                (document_id, page_number)
                for page_number in provider_profile_mismatch_page_numbers
            )
            visual_furniture_ids = visual_pdf_page_furniture_ids(
                tuple(approved_provider_snapshots)
            )
            for visual_snapshot in approved_provider_snapshots:
                visual_pages.setdefault(
                    visual_snapshot.page_number,
                    [],
                ).append(visual_snapshot)

        for expected_page in expected_pages:
            page_number = int(expected_page["page_number"])
            visual_snapshot = None
            if review_mode == "REMOTE_VISUAL":
                if page_number in provider_profile_mismatch_page_numbers:
                    continue
                captures = visual_pages.get(page_number, [])
                if len(captures) != 1:
                    missing_visual_pages.append((document_id, page_number))
                    continue
                visual_snapshot = captures[0]
                if (
                    visual_snapshot.page_image_sha256
                    != expected_page["page_image_sha256"]
                ):
                    image_mismatch_pages.append((document_id, page_number))
                    continue

            try:
                measurement = _measure_pdf_body_page(
                    snapshot,
                    page_number=page_number,
                    review_mode=review_mode,
                    visual_snapshot=visual_snapshot,
                    excluded_visual_observation_ids=visual_furniture_ids,
                    reviewed_image_occurrences=(
                        reviewed_pages_by_number[page_number]["image_occurrences"]
                        if page_number in reviewed_pages_by_number
                        else None
                    ),
                    reviewed_table_occurrences=(
                        reviewed_pages_by_number[page_number].get("table_occurrences")
                        if review_mode == "NATIVE"
                        and page_number in reviewed_pages_by_number
                        else None
                    ),
                )
            except ValueError:
                table_inventory_mismatch_pages.append((document_id, page_number))
                continue
            reviewed_page = reviewed_pages_by_number.get(page_number)
            image_inventory_matches = bool(
                measurement["image_inventory_matches"]
                and (
                    reviewed_page is None
                    or (
                        measurement["image_content_multiset_sha256"]
                        == reviewed_page["image_content_multiset_sha256"]
                        and measurement["image_sequence_sha256"]
                        == reviewed_page["image_sequence_sha256"]
                    )
                )
            )
            if not image_inventory_matches:
                image_inventory_mismatch_pages.append((document_id, page_number))
                continue
            if not measurement["table_inventory_matches"]:
                table_inventory_mismatch_pages.append((document_id, page_number))
                continue
            evidence["observed_page_count"] += 1
            for count_name in (
                "body_line_count",
                "image_count",
                "table_count",
                "table_row_count",
                "table_cell_count",
                "merged_region_count",
            ):
                evidence[f"observed_{count_name}"] += int(measurement[count_name])
            count_mismatch = any(
                int(measurement[count_name])
                != int(expected_page[f"expected_{count_name}"])
                for count_name in (
                    "body_line_count",
                    "image_count",
                    "table_count",
                    "table_row_count",
                    "table_cell_count",
                    "merged_region_count",
                )
            )
            content_matches = bool(
                not count_mismatch
                and measurement["content_multiset_sha256"]
                == expected_page["content_multiset_sha256"]
            )
            sequence_matches = bool(
                measurement["sequence_sha256"] == expected_page["sequence_sha256"]
            )
            if not content_matches:
                content_mismatch_pages.append((document_id, page_number))
            if content_matches and not sequence_matches:
                order_mismatch_pages.append((document_id, page_number))
            if not sequence_matches:
                sequence_mismatch_pages.append((document_id, page_number))

    evidence["source_ir_unavailable_count"] = source_ir_unavailable_count
    evidence["source_identity_mismatch_count"] = source_identity_mismatch_count
    evidence["remote_visual_not_run_page_count"] = len(remote_visual_not_run_pages)
    evidence["missing_visual_page_count"] = len(missing_visual_pages)
    evidence["provider_profile_mismatch_page_count"] = len(
        provider_profile_mismatch_pages
    )
    evidence["image_inventory_mismatch_page_count"] = len(
        image_inventory_mismatch_pages
    )
    evidence["table_inventory_mismatch_page_count"] = len(
        table_inventory_mismatch_pages
    )
    evidence["page_image_mismatch_count"] = len(image_mismatch_pages)
    evidence["body_content_mismatch_count"] = len(content_mismatch_pages)
    evidence["body_order_mismatch_count"] = len(order_mismatch_pages)
    evidence["body_sequence_mismatch_count"] = len(sequence_mismatch_pages)
    evidence["remote_visual_not_run_pages"] = _safe_pdf_page_refs(
        remote_visual_not_run_pages
    )
    evidence["missing_visual_pages"] = _safe_pdf_page_refs(missing_visual_pages)
    evidence["provider_profile_mismatch_pages"] = _safe_pdf_page_refs(
        provider_profile_mismatch_pages
    )
    evidence["image_inventory_mismatch_pages"] = _safe_pdf_page_refs(
        image_inventory_mismatch_pages
    )
    evidence["table_inventory_mismatch_pages"] = _safe_pdf_page_refs(
        table_inventory_mismatch_pages
    )
    evidence["content_mismatch_pages"] = _safe_pdf_page_refs(content_mismatch_pages)
    evidence["order_mismatch_pages"] = _safe_pdf_page_refs(order_mismatch_pages)
    evidence["mismatched_pages"] = _safe_pdf_page_refs(
        (
            *image_mismatch_pages,
            *content_mismatch_pages,
            *sequence_mismatch_pages,
        )
    )
    if source_ir_unavailable_count or source_identity_mismatch_count:
        return {
            "status": "FAIL",
            "reason_code": "pdf_body_source_ir_unavailable_or_unbound",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if provider_profile_mismatch_pages:
        return {
            "status": "FAIL",
            "reason_code": "pdf_body_remote_visual_provider_profile_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if image_inventory_mismatch_pages:
        return {
            "status": "FAIL",
            "reason_code": "pdf_body_review_image_inventory_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if table_inventory_mismatch_pages:
        return {
            "status": "FAIL",
            "reason_code": "pdf_body_review_table_inventory_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if image_mismatch_pages or content_mismatch_pages or sequence_mismatch_pages:
        return {
            "status": "FAIL",
            "reason_code": "pdf_reviewed_body_sequence_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if remote_visual_not_run_pages or missing_visual_pages:
        return {
            "status": "GAP",
            "reason_code": (
                "pdf_body_remote_visual_not_run"
                if remote_visual_not_run_pages
                else "pdf_body_remote_visual_capture_incomplete"
            ),
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if evidence["pending_profile_count"]:
        return {
            "status": "GAP",
            "reason_code": "pdf_body_review_pending",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "pdf_reviewed_body_sequence_matches",
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _safe_pdf_page_refs(
    refs: Iterable[tuple[str, int]],
) -> list[dict[str, str | int]]:
    return [
        {"document_id": document_id, "page_number": page_number}
        for document_id, page_number in sorted(set(refs))
    ]


def _measure_pdf_body_page(
    snapshot: Any,
    *,
    page_number: int,
    review_mode: str,
    visual_snapshot: Any | None = None,
    excluded_visual_observation_ids: frozenset[str] = frozenset(),
    reviewed_image_occurrences: list[Mapping[str, Any]] | None = None,
    reviewed_table_occurrences: list[Mapping[str, Any]] | None = None,
    table_topologies_override: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    excluded_kinds = {
        SourceNodeKind.HEADER.value,
        SourceNodeKind.FOOTER.value,
        SourceNodeKind.WATERMARK.value,
        SourceNodeKind.TOC_ENTRY.value,
    }
    physical_lines: list[str] = []
    image_occurrences: list[dict[str, str]] = []
    observed_image_records: list[tuple[dict[str, Any], dict[str, str]]] = []
    table_topologies: list[dict[str, Any]] = []
    native_table_records = (
        _pdf_body_review_native_table_records(snapshot, page_number=page_number)
        if table_topologies_override is None
        else []
    )
    assets_by_id = {asset.asset_id: asset for asset in getattr(snapshot, "assets", ())}
    for node in snapshot.nodes:
        native = node.native_locator
        kind = getattr(node.kind, "value", str(node.kind))
        native_format = getattr(native.format, "value", str(native.format))
        if (
            native_format != "pdf"
            or native.page_number != page_number
            or kind in excluded_kinds
        ):
            continue
        if node.table_payload is not None:
            continue
        if kind in {SourceNodeKind.IMAGE.value, SourceNodeKind.FIGURE.value}:
            asset = assets_by_id.get(node.asset_ref)
            projection = {
                "node_kind": kind,
                "asset_kind": (
                    getattr(asset.kind, "value", str(asset.kind))
                    if asset is not None
                    else "missing"
                ),
                "media_type": (
                    str(asset.media_type) if asset is not None else "missing"
                ),
                "content_sha256": (
                    str(asset.content_sha256) if asset is not None else "missing"
                ),
            }
            image_occurrences.append(projection)
            observed_image_records.append(
                (
                    _pdf_body_review_image_occurrence_identity(
                        node,
                        asset=asset,
                        node_kind=kind,
                        native_format=native_format,
                    ),
                    projection,
                )
            )
        if review_mode == "NATIVE":
            physical_lines.extend(_canonical_pdf_physical_lines(node.raw_text))

    table_inventory_matches = True
    if table_topologies_override is not None:
        if reviewed_table_occurrences is not None:
            raise ValueError(
                "table topology override cannot use a native reviewed inventory"
            )
        table_topologies = [
            _canonical_pdf_body_review_table_topology(topology)
            for topology in table_topologies_override
        ]
    elif reviewed_table_occurrences is not None:
        reviewed_occurrences = list(reviewed_table_occurrences)
        identity_fields = (
            "provider_object_id_sha256",
            "provider_index",
            "page_number",
            "bbox",
            "row_count",
            "column_count",
            "cell_count",
            "merged_region_count",
            "topology_sha256",
        )
        reviewed_identities = [
            {field: occurrence[field] for field in identity_fields}
            for occurrence in reviewed_occurrences
        ]
        observed_identities = [record[0] for record in native_table_records]
        table_inventory_matches = bool(
            len(observed_identities) == len(reviewed_identities)
            and all(
                canonical_pdf_body_review_json_bytes(observed)
                == canonical_pdf_body_review_json_bytes(reviewed)
                for observed, reviewed in zip(
                    observed_identities,
                    reviewed_identities,
                    strict=True,
                )
            )
        )
        table_topologies = (
            [
                topology
                for reviewed, (_, topology) in zip(
                    reviewed_occurrences,
                    native_table_records,
                    strict=True,
                )
                if reviewed["disposition"] == "CONTENT"
            ]
            if table_inventory_matches
            else []
        )
    else:
        table_topologies = [topology for _, topology in native_table_records]
    table_count = len(table_topologies)
    table_row_count = sum(int(table["row_count"]) for table in table_topologies)
    table_cell_count = sum(len(table["cells"]) for table in table_topologies)
    merged_region_count = sum(
        1
        for table in table_topologies
        for cell in table["cells"]
        if int(cell["row_span"]) > 1 or int(cell["column_span"]) > 1
    )

    if review_mode == "REMOTE_VISUAL":
        if visual_snapshot is None:
            raise ValueError("remote visual body measurement requires a capture")
        for observation in sorted(
            visual_snapshot.observations,
            key=lambda item: item.observation_index,
        ):
            if observation.observation_id in excluded_visual_observation_ids:
                continue
            physical_lines.extend(_canonical_pdf_physical_lines(observation.raw_text))
    elif review_mode != "NATIVE":
        raise ValueError("unsupported PDF body review mode")

    image_inventory_matches = True
    if reviewed_image_occurrences is not None:
        reviewed_occurrences = list(reviewed_image_occurrences)
        observed_image_records.sort(
            key=lambda item: (
                item[0].get("source_order")
                if isinstance(item[0].get("source_order"), int)
                else -1
            )
        )
        identity_fields = (
            "source_order",
            "node_kind",
            "asset_kind",
            "media_type",
            "content_sha256",
            "native_locator",
            "derivation",
        )
        reviewed_identities = [
            {field: occurrence[field] for field in identity_fields}
            for occurrence in reviewed_occurrences
        ]
        observed_identities = [identity for identity, _ in observed_image_records]
        image_inventory_matches = bool(
            len(observed_identities) == len(reviewed_identities)
            and all(
                canonical_pdf_body_review_json_bytes(observed)
                == canonical_pdf_body_review_json_bytes(reviewed)
                for observed, reviewed in zip(
                    observed_identities,
                    reviewed_identities,
                    strict=True,
                )
            )
        )
        image_occurrences = (
            pdf_content_image_projection(reviewed_occurrences)
            if image_inventory_matches
            else []
        )
        image_summary = (
            summarize_pdf_content_image_occurrences(reviewed_occurrences)
            if image_inventory_matches
            else summarize_pdf_content_image_occurrences([])
        )
    else:
        image_summary = {
            "image_content_multiset_sha256": hashlib.sha256(
                canonical_pdf_body_review_json_bytes(
                    sorted(
                        image_occurrences,
                        key=canonical_pdf_body_review_json_bytes,
                    )
                )
            ).hexdigest(),
            "image_sequence_sha256": hashlib.sha256(
                canonical_pdf_body_review_json_bytes(image_occurrences)
            ).hexdigest(),
        }

    content_items: list[dict[str, Any]] = [
        {"kind": "physical_line", "value": line} for line in physical_lines
    ]
    content_items.extend(
        {"kind": "table_topology", "value": topology} for topology in table_topologies
    )
    content_items.extend(
        {"kind": "image_occurrence", "value": occurrence}
        for occurrence in image_occurrences
    )
    content_items.sort(key=_canonical_json_payload)
    channel_sequences = {
        "physical_lines": physical_lines,
        "image_occurrences": image_occurrences,
        "table_topologies": table_topologies,
    }
    return {
        "body_line_count": len(physical_lines),
        "image_count": len(image_occurrences),
        "table_count": table_count,
        "table_row_count": table_row_count,
        "table_cell_count": table_cell_count,
        "merged_region_count": merged_region_count,
        "image_inventory_matches": image_inventory_matches,
        "table_inventory_matches": table_inventory_matches,
        "image_content_multiset_sha256": image_summary["image_content_multiset_sha256"],
        "image_sequence_sha256": image_summary["image_sequence_sha256"],
        "content_multiset_sha256": _sha256_text(_canonical_json_payload(content_items)),
        "sequence_sha256": _sha256_text(_canonical_json_payload(channel_sequences)),
    }


def _pdf_body_review_image_occurrence_identity(
    node: Any,
    *,
    asset: Any | None,
    node_kind: str,
    native_format: str,
) -> dict[str, Any]:
    native = node.native_locator
    derivation = getattr(node, "asset_derivation", None)
    bbox = getattr(native, "bbox", None)
    return {
        "source_order": getattr(node, "source_order", None),
        "node_kind": node_kind,
        "asset_kind": (
            getattr(asset.kind, "value", str(asset.kind))
            if asset is not None
            else "missing"
        ),
        "media_type": str(asset.media_type) if asset is not None else "missing",
        "content_sha256": (
            str(asset.content_sha256) if asset is not None else "missing"
        ),
        "native_locator": {
            "format": native_format,
            "page_number": getattr(native, "page_number", None),
            "block_index": getattr(native, "block_index", None),
            "bbox": list(bbox) if bbox is not None else None,
        },
        "derivation": (
            {
                "kind": getattr(derivation.kind, "value", str(derivation.kind)),
                "base_content_sha256": str(derivation.base_content_sha256),
                "mask_content_sha256": str(derivation.mask_content_sha256),
            }
            if derivation is not None
            else None
        ),
    }


def _pdf_body_review_native_table_records(
    snapshot: Any,
    *,
    page_number: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return provider-neutral identities and text-bearing native topologies."""

    records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    source_sha256 = str(getattr(snapshot, "source_sha256", ""))
    for node in getattr(snapshot, "nodes", ()):
        table = getattr(node, "table_payload", None)
        native = getattr(node, "native_locator", None)
        native_format = getattr(
            getattr(native, "format", None),
            "value",
            str(getattr(native, "format", "")),
        )
        if (
            table is None
            or native_format != "pdf"
            or getattr(native, "page_number", None) != page_number
        ):
            continue
        bbox_value = getattr(native, "bbox", None)
        if not isinstance(bbox_value, (list, tuple)) or len(bbox_value) != 4:
            raise ValueError("PDF body review native table bbox is invalid")
        bbox = [float(item) for item in bbox_value]
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            raise ValueError("PDF body review native table bbox is invalid")
        topology = _canonical_pdf_body_review_table_topology(
            {
                "row_count": table.row_count,
                "column_count": table.column_count,
                "cells": [
                    {
                        "row": cell.row,
                        "column": cell.column,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                        "text_lines": _canonical_pdf_physical_lines(cell.raw_text),
                    }
                    for cell in sorted(
                        table.cells,
                        key=lambda item: (item.row, item.column),
                    )
                ],
            }
        )
        source_node_id = getattr(node, "source_node_id", None)
        if not isinstance(source_node_id, str) or not source_node_id:
            raise ValueError("PDF body review native table source ID is invalid")
        cells = list(topology["cells"])
        text_free_topology = {
            "row_count": int(topology["row_count"]),
            "column_count": int(topology["column_count"]),
            "cells": [
                {
                    "row": int(cell["row"]),
                    "column": int(cell["column"]),
                    "row_span": int(cell["row_span"]),
                    "column_span": int(cell["column_span"]),
                }
                for cell in cells
            ],
        }
        identity = {
            "provider_object_id_sha256": _sha256_text(
                f"{source_sha256}:native-table:{source_node_id}"
            ),
            "provider_index": len(records),
            "page_number": page_number,
            "bbox": bbox,
            "row_count": int(topology["row_count"]),
            "column_count": int(topology["column_count"]),
            "cell_count": len(cells),
            "merged_region_count": sum(
                1
                for cell in cells
                if int(cell["row_span"]) > 1 or int(cell["column_span"]) > 1
            ),
            "topology_sha256": canonical_pdf_body_review_json_sha256(
                text_free_topology
            ),
        }
        records.append((identity, topology))
    return records


def _canonical_pdf_physical_lines(raw_text: str) -> tuple[str, ...]:
    return tuple(
        normalized
        for line in raw_text.splitlines()
        if (normalized := " ".join(unicodedata.normalize("NFKC", line).split()))
    )


def _canonical_native_locator(locator: Any) -> dict[str, Any]:
    if hasattr(locator, "model_dump"):
        return dict(locator.model_dump(mode="json", exclude_none=True))
    fields = (
        "format",
        "whole_document",
        "page_number",
        "block_index",
        "bbox",
        "body_element_index",
        "paragraph_index",
        "table_index",
        "row_index",
        "column_index",
        "object_index",
        "workbook",
        "sheet",
        "cell_range",
    )
    payload: dict[str, Any] = {}
    for field in fields:
        value = getattr(locator, field, None)
        if value is not None:
            payload[field] = getattr(value, "value", value)
    return payload


def _canonical_pdf_body_review_table_topology(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one provider-neutral table used only to prepare a review draft."""

    if not isinstance(value, Mapping) or set(value) != {
        "row_count",
        "column_count",
        "cells",
    }:
        raise ValueError("PDF body review table topology has invalid fields")
    row_count = value["row_count"]
    column_count = value["column_count"]
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 1
        or isinstance(column_count, bool)
        or not isinstance(column_count, int)
        or column_count < 1
    ):
        raise ValueError("PDF body review table dimensions must be positive integers")
    raw_cells = value["cells"]
    if not isinstance(raw_cells, (list, tuple)) or not raw_cells:
        raise ValueError("PDF body review table cells must be a non-empty sequence")

    cells: list[dict[str, Any]] = []
    occupied: set[tuple[int, int]] = set()
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, Mapping) or set(raw_cell) != {
            "row",
            "column",
            "row_span",
            "column_span",
            "text_lines",
        }:
            raise ValueError("PDF body review table cell has invalid fields")
        row = raw_cell["row"]
        column = raw_cell["column"]
        row_span = raw_cell["row_span"]
        column_span = raw_cell["column_span"]
        if (
            any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in (row, column, row_span, column_span)
            )
            or min(row, column) < 0
            or min(row_span, column_span) < 1
        ):
            raise ValueError("PDF body review table cell coordinates are invalid")
        if row + row_span > row_count or column + column_span > column_count:
            raise ValueError("PDF body review table cell exceeds its table dimensions")
        raw_lines = raw_cell["text_lines"]
        if not isinstance(raw_lines, (list, tuple)) or any(
            not isinstance(line, str) for line in raw_lines
        ):
            raise ValueError("PDF body review table text lines must be strings")
        text_lines = tuple(
            line
            for raw_line in raw_lines
            for line in _canonical_pdf_physical_lines(raw_line)
        )
        coverage = {
            (covered_row, covered_column)
            for covered_row in range(row, row + row_span)
            for covered_column in range(column, column + column_span)
        }
        if occupied.intersection(coverage):
            raise ValueError("PDF body review table cell spans overlap")
        occupied.update(coverage)
        cells.append(
            {
                "row": row,
                "column": column,
                "row_span": row_span,
                "column_span": column_span,
                "text_lines": text_lines,
            }
        )
    cells.sort(key=lambda cell: (cell["row"], cell["column"]))
    anchors = [(cell["row"], cell["column"]) for cell in cells]
    if len(anchors) != len(set(anchors)):
        raise ValueError("PDF body review table cell anchors must be unique")
    expected_coverage = {
        (row, column) for row in range(row_count) for column in range(column_count)
    }
    if occupied != expected_coverage:
        raise ValueError("PDF body review table topology must cover a full rectangle")
    return {
        "row_count": row_count,
        "column_count": column_count,
        "cells": cells,
    }


def _evaluate_pdf_single_toc_noise_block(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    expected_candidate_fingerprints_by_document: dict[str, str],
) -> dict[str, Any]:
    evaluator_id = PDF_SINGLE_TOC_NOISE_BLOCK_EVALUATOR_ID
    scopes = _pdf_document_region_scopes(case)
    evidence = {
        "document_count": len(case["document_ids"]),
        "verified_document_count": 0,
        "source_ir_unavailable_count": 0,
        "pipeline_failure_count": 0,
        "toc_block_count": 0,
        "toc_page_scope_mismatch_count": 0,
        "toc_semantic_mismatch_count": 0,
        "toc_grounding_mismatch_count": 0,
        "toc_candidate_source_ref_count": 0,
        "toc_covered_source_ref_count": 0,
        "toc_uncovered_source_ref_count": 0,
        "toc_candidate_fingerprints_by_document": {},
        "toc_candidate_fingerprint_mismatch_count": 0,
        "toc_structure_leak_count": 0,
    }
    for document_id in case["document_ids"]:
        context = _pdf_semantic_context_for_document(
            document_id=document_id,
            document_result=document_results.get(document_id) or {},
            source_ir_snapshots=source_ir_snapshots,
            structure_snapshots=structure_snapshots,
            semantic_snapshots=semantic_snapshots,
        )
        if context is None:
            evidence["source_ir_unavailable_count"] += 1
            evidence["pipeline_failure_count"] += 1
            continue
        source_snapshot, tree, semantics = context
        roots = [node for node in tree.nodes if node.kind is StructureNodeKind.DOCUMENT]
        toc_nodes = [
            node
            for node in tree.nodes
            if node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK
        ]
        evidence["toc_block_count"] += len(toc_nodes)
        if len(roots) != 1 or len(toc_nodes) != 1:
            evidence["toc_structure_leak_count"] += 1
            continue
        toc_node = toc_nodes[0]
        toc_refs = tuple(toc_node.source_refs)
        toc_ref_set = set(toc_refs)
        expected_pages = set(
            range(
                scopes[document_id]["toc_start"],
                scopes[document_id]["toc_end"] + 1,
            )
        )
        candidate_refs = _pdf_toc_candidate_source_refs(
            source_snapshot,
            expected_pages=expected_pages,
        )
        covered_candidate_refs = candidate_refs & toc_ref_set
        uncovered_candidate_refs = candidate_refs - toc_ref_set
        evidence["toc_candidate_source_ref_count"] += len(candidate_refs)
        evidence["toc_covered_source_ref_count"] += len(covered_candidate_refs)
        evidence["toc_uncovered_source_ref_count"] += len(uncovered_candidate_refs)
        candidate_fingerprint = _pdf_toc_candidate_fingerprint(
            source_snapshot,
            candidate_refs,
        )
        evidence["toc_candidate_fingerprints_by_document"][document_id] = (
            candidate_fingerprint
        )
        candidate_fingerprint_matches = (
            expected_candidate_fingerprints_by_document.get(document_id)
            == candidate_fingerprint
        )
        if not candidate_fingerprint_matches:
            evidence["toc_candidate_fingerprint_mismatch_count"] += 1
        observed_pages = {
            page_number
            for ref in toc_refs
            if (
                page_number := _source_ref_page_number(
                    ref,
                    source_snapshot=source_snapshot,
                )
            )
            is not None
        }
        page_scope_matches = observed_pages == expected_pages
        if not page_scope_matches:
            evidence["toc_page_scope_mismatch_count"] += 1
        annotations = [
            annotation
            for annotation in semantics.annotations
            if annotation.target_structure_node_id == toc_node.structure_node_id
        ]
        semantic_matches = bool(
            toc_node.kind is StructureNodeKind.GROUP
            and toc_node.parent_structure_node_id == roots[0].structure_node_id
            and toc_refs
            and len(toc_refs) == len(toc_ref_set)
            and len(annotations) == 1
            and annotations[0].semantic_class is SemanticClass.NOISE
        )
        if not semantic_matches:
            evidence["toc_semantic_mismatch_count"] += 1
        grounding_matches = bool(
            semantic_matches
            and {span.source_ref for span in annotations[0].grounding_spans}
            == toc_ref_set
        )
        if semantic_matches and not grounding_matches:
            evidence["toc_grounding_mismatch_count"] += 1
        leaked_structure_nodes = [
            node
            for node in tree.nodes
            if node.structure_node_id != toc_node.structure_node_id
            and set(node.source_refs) & toc_ref_set
        ]
        structure_isolated = not leaked_structure_nodes
        if not structure_isolated:
            evidence["toc_structure_leak_count"] += len(leaked_structure_nodes)
        evidence["verified_document_count"] += int(
            page_scope_matches
            and semantic_matches
            and grounding_matches
            and candidate_fingerprint_matches
            and not uncovered_candidate_refs
            and structure_isolated
        )
    mismatch_count = sum(
        int(evidence[key])
        for key in (
            "pipeline_failure_count",
            "toc_page_scope_mismatch_count",
            "toc_semantic_mismatch_count",
            "toc_grounding_mismatch_count",
            "toc_candidate_fingerprint_mismatch_count",
            "toc_uncovered_source_ref_count",
            "toc_structure_leak_count",
        )
    )
    if mismatch_count:
        return {
            "status": "FAIL",
            "reason_code": "pdf_toc_noise_block_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "single_grounded_toc_noise_block_observed",
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_page_noise_not_requirements(
    *,
    case: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_PAGE_NOISE_NOT_REQUIREMENTS_EVALUATOR_ID
    scopes = _pdf_document_region_scopes(case)
    evidence = {
        "document_count": len(case["document_ids"]),
        "verified_document_count": 0,
        "pipeline_failure_count": 0,
        "classified_noise_structure_count": 0,
        "classified_noise_source_ref_count": 0,
        "expected_page_noise_source_ref_count": 0,
        "unclassified_page_noise_ref_count": 0,
        "noise_evidence_missing_count": 0,
        "native_noise_without_semantic_count": 0,
        "projected_noise_target_count": 0,
        "projected_noise_source_ref_count": 0,
        "projected_noise_full_context_ref_count": 0,
        "body_context_noise_ref_count": 0,
    }
    for document_id in case["document_ids"]:
        context = _pdf_requirement_context_for_document(
            document_id=document_id,
            document_result=document_results.get(document_id) or {},
            source_ir_snapshots=source_ir_snapshots,
            structure_snapshots=structure_snapshots,
            semantic_snapshots=semantic_snapshots,
            requirement_view_snapshots=requirement_view_snapshots,
        )
        if context is None:
            evidence["pipeline_failure_count"] += 1
            continue
        source_snapshot, tree, semantics, view = context
        annotations_by_target = {
            annotation.target_structure_node_id: annotation
            for annotation in semantics.annotations
        }
        noise_structure_ids = {
            target_id
            for target_id, annotation in annotations_by_target.items()
            if annotation.semantic_class is SemanticClass.NOISE
        }
        noise_refs = {
            ref
            for node in tree.nodes
            if node.structure_node_id in noise_structure_ids
            for ref in node.source_refs
        }
        explicit_native_noise_refs = {
            _source_node_ref(node.source_node_id)
            for node in source_snapshot.nodes
            if node.kind
            in {
                SourceNodeKind.HEADER,
                SourceNodeKind.FOOTER,
                SourceNodeKind.WATERMARK,
                SourceNodeKind.TOC_ENTRY,
            }
        }
        toc_pages = set(
            range(
                scopes[document_id]["toc_start"],
                scopes[document_id]["toc_end"] + 1,
            )
        )
        expected_page_noise_refs = explicit_native_noise_refs | (
            _pdf_toc_candidate_source_refs(
                source_snapshot,
                expected_pages=toc_pages,
            )
        )
        unclassified_page_noise_refs = expected_page_noise_refs - noise_refs
        evidence["classified_noise_structure_count"] += len(noise_structure_ids)
        evidence["classified_noise_source_ref_count"] += len(noise_refs)
        evidence["expected_page_noise_source_ref_count"] += len(
            expected_page_noise_refs
        )
        evidence["unclassified_page_noise_ref_count"] += len(
            unclassified_page_noise_refs
        )
        evidence["noise_evidence_missing_count"] += int(
            not expected_page_noise_refs or not noise_refs
        )
        evidence["native_noise_without_semantic_count"] += len(
            explicit_native_noise_refs - noise_refs
        )
        evidence["projected_noise_target_count"] += sum(
            node.target_structure_node_id in noise_structure_ids for node in view.nodes
        )
        evidence["projected_noise_source_ref_count"] += sum(
            bool(set(node.source_refs) & noise_refs) for node in view.nodes
        )
        evidence["projected_noise_full_context_ref_count"] += sum(
            bool(set(node.full_context_source_refs) & noise_refs) for node in view.nodes
        )
        document_view = build_document_view(source_snapshot, tree)
        body_source_ids = {
            str(ref.get("source_id") or "")
            for item in document_view["document_outline"]
            if item.get("document_region") == "BODY"
            for ref in item.get("context_source_refs") or []
        }
        evidence["body_context_noise_ref_count"] += sum(
            ref.kind is StructureSourceRefKind.SOURCE_NODE
            and ref.source_id in body_source_ids
            for ref in noise_refs
        )
        evidence["verified_document_count"] += int(
            bool(expected_page_noise_refs)
            and bool(noise_refs)
            and not unclassified_page_noise_refs
            and not (explicit_native_noise_refs - noise_refs)
        )
    mismatch_count = sum(
        int(evidence[key])
        for key in (
            "pipeline_failure_count",
            "unclassified_page_noise_ref_count",
            "noise_evidence_missing_count",
            "native_noise_without_semantic_count",
            "projected_noise_target_count",
            "projected_noise_source_ref_count",
            "projected_noise_full_context_ref_count",
            "body_context_noise_ref_count",
        )
    )
    if mismatch_count:
        return {
            "status": "FAIL",
            "reason_code": "classified_pdf_page_noise_leaked",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "classified_pdf_page_noise_excluded",
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _pdf_toc_candidate_source_refs(
    source_snapshot: Any,
    *,
    expected_pages: set[int],
) -> set[StructureSourceRef]:
    """Return source text that a declared PDF TOC region must account for.

    This evidence is independent of Structure/Semantic output: every native
    text block that the resolver can treat as a TOC entry must be grounded by
    the single TOC block.  Explicit adapter-proven headers, footers and
    watermarks are excluded before comparison.
    """

    return {
        _source_node_ref(node.source_node_id)
        for node in source_snapshot.nodes
        if node.native_locator.page_number in expected_pages
        and node.kind
        in {
            SourceNodeKind.HEADING,
            SourceNodeKind.PARAGRAPH,
            SourceNodeKind.TOC_ENTRY,
        }
        and node.raw_text.strip()
    }


def _pdf_toc_candidate_fingerprint(
    source_snapshot: Any,
    refs: set[StructureSourceRef],
) -> str:
    """Bind reviewed native facts without adapter-version-derived node IDs."""

    source_nodes_by_id = {node.source_node_id: node for node in source_snapshot.nodes}
    records: list[dict[str, Any]] = []
    for ref in refs:
        node = source_nodes_by_id[ref.source_id]
        records.append(
            {
                "ref_kind": ref.kind.value,
                "source_node_kind": node.kind.value,
                "native_locator": _canonical_native_locator(node.native_locator),
                "raw_text_sha256": _sha256_text(node.raw_text),
            }
        )
    records.sort(key=_canonical_json_payload)
    return _sha256_text(_canonical_json_payload(records))


def _pdf_document_region_scopes(case: dict[str, Any]) -> dict[str, dict[str, int]]:
    scopes: dict[str, dict[str, int]] = {}
    for document_id in case["document_ids"]:
        locators = [
            locator
            for locator in case.get("locators", [])
            if isinstance(locator, dict)
            and locator.get("document_id") == document_id
            and locator.get("kind") == "page_region"
        ]
        if len(locators) != 1:
            raise ValueError("PDF document-region scope must be unique.")
        scopes[document_id] = _parse_pdf_document_region_pointer(
            str(locators[0].get("pointer") or "")
        )
    return scopes


def _source_ir_snapshot_for_document_result(
    document_result: dict[str, Any],
    *,
    source_ir_snapshots: dict[tuple[str, str], Any],
) -> Any | None:
    if document_result.get("source_ir_status") not in {
        "COMPLETE",
        "PARTIAL",
        "REUSED_IDENTICAL_SOURCE",
    }:
        return None
    key = (
        str(document_result.get("source_sha256") or ""),
        str(
            document_result.get("detected_format")
            or document_result.get("declared_format")
            or ""
        ),
    )
    return source_ir_snapshots.get(key)


def _evaluate_spreadsheet_visible_workbook_fidelity(
    *,
    case: dict[str, Any],
    manifest_documents: dict[str, dict[str, Any]],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    workbook_profiles_by_document: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evaluator_id = SPREADSHEET_VISIBLE_WORKBOOK_FIDELITY_EVALUATOR_ID
    document_ids = list(case.get("document_ids") or [])
    evidence: dict[str, Any] = {
        "document_count": len(document_ids),
        "source_identity_mismatch_count": 0,
        "source_ir_unavailable_count": 0,
        "derived_structure_mismatch_count": 0,
        "page_render_asset_mismatch_count": 0,
        "visual_profile_mismatch_count": 0,
        "page_asset_projection_reference_count": 0,
        "expected_page_count": sum(
            int(profile.get("page_count") or 0)
            for profile in workbook_profiles_by_document.values()
        ),
        "reviewed_page_count": sum(
            int(profile.get("reviewed_page_count") or 0)
            for profile in workbook_profiles_by_document.values()
        ),
        "pending_review_document_count": sum(
            profile.get("review_status") != "AI_SILVER_COMPLETE"
            for profile in workbook_profiles_by_document.values()
        ),
    }
    for document_id in document_ids:
        profile = workbook_profiles_by_document.get(document_id)
        result = document_results.get(document_id)
        document = manifest_documents.get(document_id)
        if profile is None or result is None or document is None:
            evidence["source_ir_unavailable_count"] += 1
            continue
        source_sha256 = str(profile.get("source_sha256") or "")
        source_format = str(profile.get("source_format") or "")
        if (
            source_sha256 != document.get("source_sha256")
            or source_sha256 != result.get("source_sha256")
            or source_format != document.get("format")
        ):
            evidence["source_identity_mismatch_count"] += 1
        snapshot = _source_ir_snapshot_for_document_result(
            result,
            source_ir_snapshots=source_ir_snapshots,
        )
        if snapshot is None:
            evidence["source_ir_unavailable_count"] += 1
            continue
        page_assets = tuple(
            asset
            for asset in snapshot.assets
            if asset.kind is SourceAssetKind.PAGE_RENDER
        )
        page_asset_ids = {asset.asset_id for asset in page_assets}
        evidence["page_asset_projection_reference_count"] += sum(
            node.asset_ref in page_asset_ids
            for node in snapshot.nodes
            if node.asset_ref is not None
        )
        if source_format == "xlsx":
            locator_formats = {
                node.native_locator.format.value for node in snapshot.nodes
            }
            if (
                snapshot.source_sha256 != source_sha256
                or snapshot.source_conversion is not None
                or locator_formats - {"xlsx"}
            ):
                evidence["derived_structure_mismatch_count"] += 1
            if page_assets:
                evidence["page_render_asset_mismatch_count"] += 1
            continue

        conversion = snapshot.source_conversion
        if (
            snapshot.source_sha256 != source_sha256
            or conversion is None
            or conversion.original_source_sha256 != source_sha256
            or conversion.source_truth != "original_bytes"
            or conversion.fidelity_profile
            != "derived_spreadsheet_structure_and_values_only"
            or any(
                node.native_locator.format.value != "xlsx"
                for node in snapshot.nodes
            )
        ):
            evidence["derived_structure_mismatch_count"] += 1
        expected_page_count = int(profile.get("page_count") or 0)
        ordered_page_assets = sorted(
            page_assets,
            key=lambda asset: asset.storage_key,
        )
        page_asset_inventory = [
            {
                "page_number": index,
                "content_sha256": asset.content_sha256,
            }
            for index, asset in enumerate(ordered_page_assets, start=1)
        ]
        if (
            len(page_assets) != expected_page_count
            or any(
                source_sha256 not in asset.storage_key for asset in page_assets
            )
            or _sha256_text(
                _canonical_json_payload(page_asset_inventory)
            )
            != profile.get("page_inventory_sha256")
        ):
            evidence["page_render_asset_mismatch_count"] += 1
        visual_evidence = {
            diagnostic.evidence_sha256
            for diagnostic in snapshot.diagnostics
            if diagnostic.code == "xlsb_direct_visual_profile_recorded"
        }
        if (
            len(visual_evidence) != 1
            or any(
                _SHA256_PATTERN.fullmatch(str(value or "")) is None
                for value in visual_evidence
            )
        ):
            evidence["visual_profile_mismatch_count"] += 1

    technical_mismatch_count = sum(
        int(evidence[field])
        for field in (
            "source_identity_mismatch_count",
            "source_ir_unavailable_count",
            "derived_structure_mismatch_count",
            "page_render_asset_mismatch_count",
            "visual_profile_mismatch_count",
            "page_asset_projection_reference_count",
        )
    )
    evidence["technical_mismatch_count"] = technical_mismatch_count
    if evidence["pending_review_document_count"]:
        return {
            "status": "GAP",
            "reason_code": "spreadsheet_ai_silver_review_pending",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if technical_mismatch_count:
        return {
            "status": "FAIL",
            "reason_code": "spreadsheet_visible_profile_fidelity_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if evidence["reviewed_page_count"] != evidence["expected_page_count"]:
        return {
            "status": "FAIL",
            "reason_code": "spreadsheet_ai_silver_page_coverage_incomplete",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "spreadsheet_visible_content_and_structure_preserved",
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _pdf_semantic_context_for_document(
    *,
    document_id: str,
    document_result: dict[str, Any],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[Any, Any, Any] | None:
    del document_id
    source_snapshot = _source_ir_snapshot_for_document_result(
        document_result,
        source_ir_snapshots=source_ir_snapshots,
    )
    if source_snapshot is None:
        return None
    structure_key = (
        source_snapshot.source_ir_id,
        DEFAULT_STRUCTURE_RESOLVER_ID,
        DEFAULT_STRUCTURE_RESOLVER_VERSION,
    )
    try:
        tree = structure_snapshots.get(structure_key)
        if tree is None:
            tree = build_structure_tree(
                source_snapshot,
                resolver_id=DEFAULT_STRUCTURE_RESOLVER_ID,
                resolver_version=DEFAULT_STRUCTURE_RESOLVER_VERSION,
            )
        structure_snapshots[structure_key] = tree
        semantic_key = (
            tree.structure_snapshot_id,
            DEFAULT_SEMANTIC_CLASSIFIER_ID,
            DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        )
        semantics = semantic_snapshots.get(semantic_key)
        if semantics is None:
            semantics = classify_content(
                source_snapshot,
                tree,
                classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            )
        validate_semantic_classification_against_inputs(
            semantics,
            source_snapshot,
            tree,
        )
        semantic_snapshots[semantic_key] = semantics
    except (StructureResolutionRejected, SemanticClassificationRejected):
        return None
    except Exception:
        return None
    return source_snapshot, tree, semantics


def _pdf_requirement_context_for_document(
    *,
    document_id: str,
    document_result: dict[str, Any],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> tuple[Any, Any, Any, Any] | None:
    semantic_context = _pdf_semantic_context_for_document(
        document_id=document_id,
        document_result=document_result,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
    )
    if semantic_context is None:
        return None
    source_snapshot, tree, semantics = semantic_context
    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
        requirement_view_snapshots[view_key] = view
    except RequirementProjectionRejected:
        return None
    except Exception:
        return None
    return source_snapshot, tree, semantics, view


def _source_ref_page_number(
    ref: Any,
    *,
    source_snapshot: Any,
) -> int | None:
    if ref.kind is StructureSourceRefKind.SOURCE_NODE:
        source_node = next(
            (
                node
                for node in source_snapshot.nodes
                if node.source_node_id == ref.source_id
            ),
            None,
        )
        return (
            source_node.native_locator.page_number if source_node is not None else None
        )
    if ref.kind is StructureSourceRefKind.TABLE_CELL:
        for node, table in _source_ir_tables(source_snapshot):
            del node
            cell = next(
                (cell for cell in table.cells if cell.cell_id == ref.source_id),
                None,
            )
            if cell is not None:
                return cell.native_locator.page_number
        return None
    if ref.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION:
        observation = next(
            (
                observation
                for snapshot in source_snapshot.visual_text_snapshots
                for observation in snapshot.observations
                if observation.observation_id == ref.source_id
            ),
            None,
        )
        return observation.page_number if observation is not None else None
    return None


def _source_node_ref(source_node_id: str) -> StructureSourceRef:
    return StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=source_node_id,
    )


def _evaluate_oracle_fidelity(
    *,
    oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    evaluator_id: str | None = None,
    oracle_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_evaluator_id = evaluator_id or str(
        oracle.get("evaluator_id") or ORACLE_EVALUATOR_ID
    )
    fact_kind = str(oracle["fact"]["kind"])
    if fact_kind in {"pdf_revision_history_group", "source_scope_group"}:
        unavailable_evidence = {
            "oracle_id": str(oracle["oracle_id"]),
            "fact_kind": fact_kind,
            "source_ir_unavailable_count": 0,
            "expected_primary_count": _oracle_expected_primary_count(oracle["fact"]),
            "observed_primary_count": 0,
            "mismatch_count": 1,
        }
        try:
            measurement = (
                _measure_pdf_revision_history_group(
                    oracle=oracle,
                    document_results=document_results,
                    source_ir_snapshots=source_ir_snapshots,
                )
                if fact_kind == "pdf_revision_history_group"
                else _measure_source_scope_group(
                    oracle=oracle,
                    document_results=document_results,
                    source_ir_snapshots=source_ir_snapshots,
                )
            )
        except Exception as exc:
            return {
                "status": "FAIL",
                "reason_code": "oracle_measurement_failed",
                "evaluator": resolved_evaluator_id,
                "evidence": {
                    **unavailable_evidence,
                    "measurement_error_type": type(exc).__name__,
                },
            }
        evidence: dict[str, Any] = {
            "oracle_id": str(oracle["oracle_id"]),
            "fact_kind": fact_kind,
            "source_ir_unavailable_count": 0,
            "expected_primary_count": int(measurement["expected_primary_count"]),
            "observed_primary_count": int(measurement["observed_primary_count"]),
            "mismatch_count": int(measurement["mismatch_count"]),
            "expected_fingerprint": str(measurement["expected_fingerprint"]),
            "observed_fingerprint": str(measurement["observed_fingerprint"]),
            "distinct_source_ir_count": int(measurement["distinct_source_ir_count"]),
        }
        for field in (
            "expected_logical_slot_count",
            "observed_logical_slot_count",
            "expected_revision_row_count",
            "observed_revision_row_count",
            "expected_native_node_count",
            "observed_native_node_count",
            "expected_native_table_count",
            "observed_native_table_count",
            "expected_asset_count",
            "observed_asset_count",
            "expected_visual_observation_count",
            "observed_visual_observation_count",
            "expected_visual_table_count",
            "observed_visual_table_count",
            "expected_cell_count",
            "observed_cell_count",
        ):
            if field in measurement:
                evidence[field] = int(measurement[field])
        return {
            "status": "PASS" if measurement["matches"] else "FAIL",
            "reason_code": (
                "oracle_fidelity_observed"
                if measurement["matches"]
                else "oracle_fidelity_mismatch"
            ),
            "evaluator": resolved_evaluator_id,
            "evidence": evidence,
        }

    document_id = str(oracle["document_id"])
    document_result = document_results.get(document_id)
    unavailable_evidence = {
        "oracle_id": str(oracle["oracle_id"]),
        "fact_kind": fact_kind,
        "source_ir_unavailable_count": 1,
        "expected_primary_count": _oracle_expected_primary_count(oracle["fact"]),
        "observed_primary_count": 0,
        "mismatch_count": 1,
    }
    if document_result is None or document_result.get("source_ir_status") not in {
        "COMPLETE",
        "PARTIAL",
        "REUSED_IDENTICAL_SOURCE",
    }:
        return {
            "status": "FAIL",
            "reason_code": "source_ir_unavailable_for_oracle",
            "evaluator": resolved_evaluator_id,
            "evidence": unavailable_evidence,
        }
    snapshot_key = (
        str(document_result.get("source_sha256") or ""),
        str(
            document_result.get("detected_format")
            or document_result.get("declared_format")
            or ""
        ),
    )
    snapshot = source_ir_snapshots.get(snapshot_key)
    if snapshot is None:
        return {
            "status": "FAIL",
            "reason_code": "source_ir_snapshot_unavailable_for_oracle",
            "evaluator": resolved_evaluator_id,
            "evidence": unavailable_evidence,
        }
    try:
        measurement = _measure_oracle_fact(
            snapshot,
            oracle["fact"],
            oracle_registry=oracle_registry,
        )
    except Exception as exc:
        return {
            "status": "FAIL",
            "reason_code": "oracle_measurement_failed",
            "evaluator": resolved_evaluator_id,
            "evidence": {
                **unavailable_evidence,
                "source_ir_unavailable_count": 0,
                "measurement_error_type": type(exc).__name__,
            },
        }
    evidence = {
        "oracle_id": str(oracle["oracle_id"]),
        "fact_kind": fact_kind,
        "source_ir_unavailable_count": 0,
        "expected_primary_count": int(measurement["expected_primary_count"]),
        "observed_primary_count": int(measurement["observed_primary_count"]),
        "mismatch_count": int(measurement["mismatch_count"]),
        "expected_fingerprint": str(measurement["expected_fingerprint"]),
        "observed_fingerprint": str(measurement["observed_fingerprint"]),
    }
    for field in (
        "expected_logical_slot_count",
        "observed_logical_slot_count",
        "observed_anchor_cell_count",
        "expected_merge_region_count",
        "observed_merged_region_count",
        "merge_topology_mismatch_count",
        "expected_narrative_count",
        "observed_narrative_count",
        "expected_parameter_table_count",
        "observed_parameter_table_count",
        "expected_scope_count",
        "observed_scope_count",
        "expected_items_per_scope",
    ):
        if field in measurement:
            evidence[field] = int(measurement[field])
    if not measurement["matches"]:
        return {
            "status": "FAIL",
            "reason_code": "oracle_fidelity_mismatch",
            "evaluator": resolved_evaluator_id,
            "evidence": evidence,
        }
    return {
        "status": "PASS",
        "reason_code": "oracle_fidelity_observed",
        "evaluator": resolved_evaluator_id,
        "evidence": evidence,
    }


def _structure_evaluator_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    require_source_oracle_fidelity: bool = True,
) -> tuple[Any | None, Any | None, dict[str, Any], dict[str, Any] | None]:
    document_id = str(source_oracle["document_id"])
    document_result = document_results.get(document_id)
    evidence: dict[str, Any] = {
        "source_oracle_id": str(source_oracle["oracle_id"]),
        "source_fact_kind": str(source_oracle["fact"]["kind"]),
        "source_ir_unavailable_count": 0,
        "structure_unavailable_count": 0,
        "source_scope_mismatch_count": 0,
        "resolver_id": DEFAULT_STRUCTURE_RESOLVER_ID,
        "resolver_version": DEFAULT_STRUCTURE_RESOLVER_VERSION,
    }
    if document_result is None or document_result.get("source_ir_status") not in {
        "COMPLETE",
        "PARTIAL",
        "REUSED_IDENTICAL_SOURCE",
    }:
        evidence["source_ir_unavailable_count"] = 1
        return (
            None,
            None,
            evidence,
            {
                "status": "FAIL",
                "reason_code": "source_ir_unavailable_for_structure_evaluator",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    snapshot_key = (
        str(document_result.get("source_sha256") or ""),
        str(
            document_result.get("detected_format")
            or document_result.get("declared_format")
            or ""
        ),
    )
    source_snapshot = source_ir_snapshots.get(snapshot_key)
    if source_snapshot is None:
        evidence["source_ir_unavailable_count"] = 1
        return (
            None,
            None,
            evidence,
            {
                "status": "FAIL",
                "reason_code": "source_ir_snapshot_unavailable_for_structure_evaluator",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    expected_source_sha256 = str(source_oracle.get("source_sha256") or "")
    if expected_source_sha256 and (
        source_snapshot.source_sha256 != expected_source_sha256
    ):
        evidence["source_scope_mismatch_count"] = 1
        return (
            None,
            None,
            evidence,
            {
                "status": "FAIL",
                "reason_code": "structure_source_identity_mismatch",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    if require_source_oracle_fidelity:
        try:
            measurement = _measure_oracle_fact(
                source_snapshot,
                source_oracle["fact"],
            )
        except Exception as exc:
            evidence["source_scope_mismatch_count"] = 1
            evidence["measurement_error_type"] = type(exc).__name__
            return (
                None,
                None,
                evidence,
                {
                    "status": "FAIL",
                    "reason_code": "structure_source_scope_measurement_failed",
                    "evaluator": evaluator_id,
                    "evidence": evidence,
                },
            )
        evidence["source_scope_mismatch_count"] = int(measurement["mismatch_count"])
        if not measurement["matches"]:
            return (
                None,
                None,
                evidence,
                {
                    "status": "FAIL",
                    "reason_code": "structure_source_scope_mismatch",
                    "evaluator": evaluator_id,
                    "evidence": evidence,
                },
            )
    structure_key = (
        source_snapshot.source_ir_id,
        DEFAULT_STRUCTURE_RESOLVER_ID,
        DEFAULT_STRUCTURE_RESOLVER_VERSION,
    )
    structure_snapshot = structure_snapshots.get(structure_key)
    if structure_snapshot is None:
        try:
            structure_snapshot = build_structure_tree(
                source_snapshot,
                resolver_id=DEFAULT_STRUCTURE_RESOLVER_ID,
                resolver_version=DEFAULT_STRUCTURE_RESOLVER_VERSION,
            )
        except StructureResolutionRejected as exc:
            evidence["structure_unavailable_count"] = 1
            evidence["structure_rejection_code"] = exc.code.value
            return (
                None,
                None,
                evidence,
                {
                    "status": "FAIL",
                    "reason_code": "structure_resolution_rejected",
                    "evaluator": evaluator_id,
                    "evidence": evidence,
                },
            )
        except Exception as exc:
            evidence["structure_unavailable_count"] = 1
            evidence["structure_error_type"] = type(exc).__name__
            return (
                None,
                None,
                evidence,
                {
                    "status": "FAIL",
                    "reason_code": "structure_resolution_failed",
                    "evaluator": evaluator_id,
                    "evidence": evidence,
                },
            )
    if structure_snapshot.source_ir_id != source_snapshot.source_ir_id:
        evidence["structure_unavailable_count"] = 1
        return (
            None,
            None,
            evidence,
            {
                "status": "FAIL",
                "reason_code": "structure_source_identity_mismatch",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    if (
        structure_snapshot.resolver_id != DEFAULT_STRUCTURE_RESOLVER_ID
        or structure_snapshot.resolver_version != DEFAULT_STRUCTURE_RESOLVER_VERSION
    ):
        evidence["structure_unavailable_count"] = 1
        return (
            None,
            None,
            evidence,
            {
                "status": "FAIL",
                "reason_code": "structure_resolver_identity_mismatch",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    structure_snapshots[structure_key] = structure_snapshot
    return source_snapshot, structure_snapshot, evidence, None


def _structure_descendant_ids(tree: Any, root_id: str) -> set[str]:
    children_by_parent: dict[str, list[str]] = {}
    for node in tree.nodes:
        if node.parent_structure_node_id is not None:
            children_by_parent.setdefault(
                node.parent_structure_node_id,
                [],
            ).append(node.structure_node_id)
    descendants = {root_id}
    pending = [root_id]
    while pending:
        parent_id = pending.pop()
        for child_id in children_by_parent.get(parent_id, ()):
            if child_id not in descendants:
                descendants.add(child_id)
                pending.append(child_id)
    return descendants


def _pdf_option_structure_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return None, failure
    assert source_snapshot is not None and tree is not None

    fact = source_oracle["fact"]
    if fact["kind"] != "pdf_option_state_group":
        evidence["option_fact_kind_mismatch_count"] = 1
        return None, {
            "status": "FAIL",
            "reason_code": "pdf_option_source_scope_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    source_nodes_by_key: dict[tuple[int, int, str], list[Any]] = {}
    for node in source_snapshot.nodes:
        native = node.native_locator
        if (
            native.format.value == "pdf"
            and native.page_number is not None
            and native.block_index is not None
        ):
            source_nodes_by_key.setdefault(
                (native.page_number, native.block_index, node.kind.value),
                [],
            ).append(node)

    sections_by_label: dict[str, Any] = {}
    descendants_by_label: dict[str, set[str]] = {}
    option_entries_by_label: dict[str, list[dict[str, Any]]] = {}
    source_resolution_mismatch_count = 0
    section_resolution_mismatch_count = 0
    structure_resolution_mismatch_count = 0
    relation_mismatch_count = 0
    expected_option_target_ids: set[str] = set()

    for expected_section in fact["sections"]:
        section_label = str(expected_section["section_label"])
        section_candidates = [
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.SECTION
            and node.explicit_label == section_label
        ]
        if len(section_candidates) != 1:
            section_resolution_mismatch_count += 1
            continue
        section = section_candidates[0]
        sections_by_label[section_label] = section
        descendants = _structure_descendant_ids(
            tree,
            section.structure_node_id,
        )
        descendants_by_label[section_label] = descendants
        option_entries: list[dict[str, Any]] = []
        for expected_record in expected_section["options"]:
            record_key = _pdf_option_state_record_key(expected_record)
            source_candidates = source_nodes_by_key.get(record_key, [])
            if (
                len(source_candidates) != 1
                or _sha256_text(source_candidates[0].raw_text)
                != expected_record["raw_sha256"]
            ):
                source_resolution_mismatch_count += 1
                continue
            source_node = source_candidates[0]
            source_ref = StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=source_node.source_node_id,
            )
            structure_candidates = [
                node
                for node in tree.nodes
                if node.kind
                in {
                    StructureNodeKind.CONTENT,
                    StructureNodeKind.CLAUSE,
                }
                and node.source_refs == (source_ref,)
            ]
            if (
                len(structure_candidates) != 1
                or structure_candidates[0].structure_node_id not in descendants
            ):
                structure_resolution_mismatch_count += 1
                continue
            structure_node = structure_candidates[0]
            relations = [
                relation
                for relation in tree.relations
                if relation.kind is StructureRelationKind.OPTION_STATE
                and relation.target_structure_node_id
                == structure_node.structure_node_id
            ]
            expected_state = StructureOptionState(expected_record["state"])
            if (
                len(relations) != 1
                or relations[0].source_structure_node_id
                != structure_node.parent_structure_node_id
                or relations[0].source_refs != structure_node.source_refs
                or relations[0].option_state is not expected_state
            ):
                relation_mismatch_count += 1
            expected_option_target_ids.add(structure_node.structure_node_id)
            option_entries.append(
                {
                    "record": expected_record,
                    "source_node": source_node,
                    "structure_node": structure_node,
                    "state": expected_state,
                }
            )
        option_entries_by_label[section_label] = option_entries

    page_source_ids = {
        node.source_node_id
        for node in source_snapshot.nodes
        if node.native_locator.format.value == "pdf"
        and node.native_locator.page_number == int(fact["page_number"])
    }
    scoped_descendant_ids = set().union(*descendants_by_label.values())
    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    scoped_option_relations = [
        relation
        for relation in tree.relations
        if relation.kind is StructureRelationKind.OPTION_STATE
        and relation.target_structure_node_id in scoped_descendant_ids
        and (target := nodes_by_id.get(relation.target_structure_node_id)) is not None
        and any(
            ref.kind is StructureSourceRefKind.SOURCE_NODE
            and ref.source_id in page_source_ids
            for ref in target.source_refs
        )
    ]
    observed_option_target_ids = {
        relation.target_structure_node_id for relation in scoped_option_relations
    }
    relation_target_mismatch_count = len(
        observed_option_target_ids.symmetric_difference(expected_option_target_ids)
    )
    expected_option_count = int(fact["expected_option_count"])
    observed_option_count = sum(
        len(entries) for entries in option_entries_by_label.values()
    )
    evidence.update(
        {
            "expected_section_count": int(fact["expected_section_count"]),
            "observed_section_count": len(sections_by_label),
            "expected_option_count": expected_option_count,
            "observed_option_count": observed_option_count,
            "observed_option_relation_count": len(scoped_option_relations),
            "source_resolution_mismatch_count": source_resolution_mismatch_count,
            "section_resolution_mismatch_count": section_resolution_mismatch_count,
            "structure_resolution_mismatch_count": (
                structure_resolution_mismatch_count
            ),
            "option_relation_mismatch_count": relation_mismatch_count,
            "option_relation_target_mismatch_count": (relation_target_mismatch_count),
        }
    )
    mismatch_count = sum(
        (
            source_resolution_mismatch_count,
            section_resolution_mismatch_count,
            structure_resolution_mismatch_count,
            relation_mismatch_count,
            relation_target_mismatch_count,
            abs(observed_option_count - expected_option_count),
            abs(len(scoped_option_relations) - expected_option_count),
        )
    )
    if mismatch_count:
        return None, {
            "status": "FAIL",
            "reason_code": "pdf_option_structure_scope_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return (
        {
            "source_snapshot": source_snapshot,
            "tree": tree,
            "sections_by_label": sections_by_label,
            "descendants_by_label": descendants_by_label,
            "option_entries_by_label": option_entries_by_label,
            "evidence": evidence,
        },
        None,
    )


def _pdf_option_semantic_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    context, failure = _pdf_option_structure_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return None, failure
    assert context is not None
    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    evidence = context["evidence"]
    evidence.update(
        {
            "classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
            "classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        }
    )
    semantic_key = (
        tree.structure_snapshot_id,
        DEFAULT_SEMANTIC_CLASSIFIER_ID,
        DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    )
    try:
        semantics = semantic_snapshots.get(semantic_key)
        if semantics is None:
            semantics = classify_content(
                source_snapshot,
                tree,
                classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            )
        validate_semantic_classification_against_inputs(
            semantics,
            source_snapshot,
            tree,
        )
    except SemanticClassificationRejected as exc:
        evidence["semantic_rejection_code"] = exc.code.value
        return None, {
            "status": "FAIL",
            "reason_code": "semantic_classification_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["semantic_error_type"] = type(exc).__name__
        return None, {
            "status": "FAIL",
            "reason_code": "semantic_classification_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    semantic_snapshots[semantic_key] = semantics
    return {**context, "semantics": semantics}, None


def _pdf_option_projection_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    context, failure = _pdf_option_semantic_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
    )
    if failure is not None:
        return None, failure
    assert context is not None
    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    semantics = context["semantics"]
    evidence = context["evidence"]
    evidence.update(
        {
            "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        }
    )
    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return None, {
            "status": "FAIL",
            "reason_code": "requirement_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return None, {
            "status": "FAIL",
            "reason_code": "requirement_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view
    return {**context, "view": view}, None


def _evaluate_pdf_option_section_semantic(
    *,
    evaluator_id: str,
    section_label: str,
    expected_semantic_class: SemanticClass,
    expected_requirement_subtype: RequirementSubtype | None,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    context, failure = _pdf_option_semantic_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    section = context["sections_by_label"].get(section_label)
    semantics = context["semantics"]
    evidence = context["evidence"]
    annotations = (
        [
            annotation
            for annotation in semantics.annotations
            if annotation.target_structure_node_id == section.structure_node_id
        ]
        if section is not None
        else []
    )
    valid_annotations = [
        annotation
        for annotation in annotations
        if annotation.semantic_class is expected_semantic_class
        and annotation.requirement_subtype is expected_requirement_subtype
        and annotation.status
        in {
            SemanticAnnotationStatus.CANDIDATE,
            SemanticAnnotationStatus.CONFIRMED,
        }
    ]
    evidence.update(
        {
            "target_section_match_count": int(section is not None),
            "semantic_annotation_count": len(annotations),
            "valid_semantic_annotation_count": len(valid_annotations),
            "expected_semantic_class": expected_semantic_class.value,
            "expected_requirement_subtype": (
                expected_requirement_subtype.value
                if expected_requirement_subtype is not None
                else None
            ),
        }
    )
    matches = bool(
        section is not None and len(annotations) == len(valid_annotations) == 1
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_option_section_semantic_observed"
            if matches
            else "pdf_option_section_semantic_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_checked_option_applicability(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_CHECKED_OPTION_APPLICABILITY_EVALUATOR_ID
    context, failure = _pdf_option_structure_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    entries = context["option_entries_by_label"].get("4.1", [])
    checked_entries = [
        entry for entry in entries if entry["state"] is StructureOptionState.CHECKED
    ]
    uncertain_entries = [
        entry for entry in entries if entry["state"] is StructureOptionState.UNCERTAIN
    ]
    expected_entries = next(
        (
            section["options"]
            for section in source_oracle["fact"]["sections"]
            if section["section_label"] == "4.1"
        ),
        [],
    )
    expected_checked_count = sum(
        1
        for record in expected_entries
        if record["state"] == StructureOptionState.CHECKED.value
    )
    evidence = context["evidence"]
    evidence.update(
        {
            "expected_region_option_count": len(expected_entries),
            "reviewed_region_option_count": len(entries),
            "expected_checked_region_count": expected_checked_count,
            "observed_checked_region_count": len(checked_entries),
            "observed_uncertain_region_count": len(uncertain_entries),
        }
    )
    matches = bool(
        expected_entries
        and len(entries) == len(expected_entries)
        and len(checked_entries) == expected_checked_count
        and not uncertain_entries
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_checked_option_applicability_observed"
            if matches
            else "pdf_checked_option_applicability_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_option_section_no_requirements(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_OPTION_SECTION_NO_REQUIREMENTS_EVALUATOR_ID
    context, failure = _pdf_option_projection_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    scoped_ids = context["descendants_by_label"].get("3.3", set())
    projected = [
        node
        for node in context["view"].nodes
        if node.target_structure_node_id in scoped_ids
    ]
    evidence = context["evidence"]
    evidence.update(
        {
            "risk_scope_structure_count": len(scoped_ids),
            "projected_risk_scope_count": len(projected),
        }
    )
    matches = bool(scoped_ids and not projected)
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_option_risk_scope_not_projected"
            if matches
            else "pdf_option_risk_scope_projection_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_option_top_level_requirement(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_OPTION_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID
    context, failure = _pdf_option_projection_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    section = context["sections_by_label"].get("4.1")
    scoped_ids = context["descendants_by_label"].get("4.1", set())
    scoped_view_nodes = [
        node
        for node in context["view"].nodes
        if node.target_structure_node_id in scoped_ids
    ]
    top_level_nodes = [
        node
        for node in scoped_view_nodes
        if node.parent_requirement_view_node_id is None
    ]
    primary_nodes = [
        node
        for node in top_level_nodes
        if section is not None
        and node.target_structure_node_id == section.structure_node_id
        and node.role is RequirementProjectionRole.PRIMARY
        and node.decision_mode is RequirementDecisionMode.DIRECT
        and node.semantic_class is SemanticClass.REQUIREMENT
        and node.requirement_subtype is RequirementSubtype.REGULATION_REQUIREMENT
    ]
    checked_target_ids = {
        entry["structure_node"].structure_node_id
        for entry in context["option_entries_by_label"].get("4.1", [])
        if entry["state"] is StructureOptionState.CHECKED
    }
    expected_scoped_target_ids = (
        {section.structure_node_id, *checked_target_ids}
        if section is not None
        else set()
    )
    observed_scoped_target_ids = {
        node.target_structure_node_id for node in scoped_view_nodes
    }
    checked_content_nodes = (
        [
            node
            for node in scoped_view_nodes
            if node.target_structure_node_id in checked_target_ids
            and node.role is RequirementProjectionRole.CONTENT
            and node.decision_mode is RequirementDecisionMode.NONE
            and node.parent_requirement_view_node_id
            == primary_nodes[0].requirement_view_node_id
        ]
        if len(primary_nodes) == 1
        else []
    )
    evidence = context["evidence"]
    evidence.update(
        {
            "scoped_projection_count": len(scoped_view_nodes),
            "top_level_requirement_count": len(top_level_nodes),
            "primary_regulation_requirement_count": len(primary_nodes),
            "expected_checked_content_count": len(checked_target_ids),
            "observed_checked_content_count": len(checked_content_nodes),
            "expected_scoped_projection_count": len(expected_scoped_target_ids),
            "scoped_projection_target_mismatch_count": len(
                observed_scoped_target_ids.symmetric_difference(
                    expected_scoped_target_ids
                )
            ),
        }
    )
    matches = bool(
        scoped_ids
        and checked_target_ids
        and len(top_level_nodes) == 1
        and len(primary_nodes) == 1
        and len(checked_content_nodes) == len(checked_target_ids)
        and len(scoped_view_nodes) == len(expected_scoped_target_ids)
        and observed_scoped_target_ids == expected_scoped_target_ids
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_option_single_top_level_requirement_observed"
            if matches
            else "pdf_option_top_level_requirement_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_unchecked_option_not_effective(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_UNCHECKED_OPTION_NOT_EFFECTIVE_EVALUATOR_ID
    context, failure = _pdf_option_projection_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    nonchecked_target_ids = {
        entry["structure_node"].structure_node_id
        for entries in context["option_entries_by_label"].values()
        for entry in entries
        if entry["state"] is not StructureOptionState.CHECKED
    }
    projected_nonchecked = [
        node
        for node in context["view"].nodes
        if node.target_structure_node_id in nonchecked_target_ids
    ]
    evidence = context["evidence"]
    evidence.update(
        {
            "nonchecked_option_count": len(nonchecked_target_ids),
            "projected_nonchecked_option_count": len(projected_nonchecked),
        }
    )
    matches = bool(nonchecked_target_ids and not projected_nonchecked)
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_nonchecked_options_not_effective"
            if matches
            else "pdf_nonchecked_option_projection_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_option_uncertainty_policy(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_OPTION_UNCERTAINTY_POLICY_EVALUATOR_ID
    context, failure = _pdf_option_structure_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    tree = context["tree"]
    all_entries = [
        entry
        for entries in context["option_entries_by_label"].values()
        for entry in entries
    ]
    entries_by_target_id = {
        entry["structure_node"].structure_node_id: entry for entry in all_entries
    }
    uncertain_target_ids = {
        target_id
        for target_id, entry in entries_by_target_id.items()
        if entry["state"] is StructureOptionState.UNCERTAIN
    }
    scoped_findings = [
        finding
        for finding in tree.findings
        if finding.code == "uncertain_option_state"
        and finding.structure_node_id in entries_by_target_id
    ]
    valid_findings = [
        finding
        for finding in scoped_findings
        if finding.structure_node_id in uncertain_target_ids
        and finding.severity is StructureFindingSeverity.ERROR
        and finding.blocking
        and finding.message_key == "structure.uncertain_option_state"
        and finding.source_refs
        == entries_by_target_id[finding.structure_node_id]["structure_node"].source_refs
    ]
    observed_target_ids = {finding.structure_node_id for finding in valid_findings}
    evidence = context["evidence"]
    evidence.update(
        {
            "expected_uncertain_option_count": len(uncertain_target_ids),
            "observed_uncertain_finding_count": len(scoped_findings),
            "valid_manual_confirmation_finding_count": len(valid_findings),
            "uncertainty_finding_target_mismatch_count": len(
                observed_target_ids.symmetric_difference(uncertain_target_ids)
            ),
            "invalid_uncertainty_finding_count": (
                len(scoped_findings) - len(valid_findings)
            ),
        }
    )
    matches = bool(
        len(scoped_findings) == len(valid_findings) == len(uncertain_target_ids)
        and observed_target_ids == uncertain_target_ids
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_option_uncertainty_policy_observed"
            if matches
            else "pdf_option_uncertainty_policy_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _pdf_revision_history_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    include_requirement_view: bool,
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    fidelity = _evaluate_oracle_fidelity(
        oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
    )
    evidence = {
        "source_oracle_id": str(source_oracle["oracle_id"]),
        "source_fact_kind": str(source_oracle["fact"]["kind"]),
        "source_scope_mismatch_count": int(
            fidelity["evidence"].get("mismatch_count") or 0
        ),
        "expected_document_count": int(
            source_oracle["fact"]["expected_document_count"]
        ),
        "resolved_document_count": 0,
        "expected_revision_row_count": int(
            source_oracle["fact"]["expected_revision_row_count"]
        ),
        "resolved_revision_row_count": 0,
        "source_table_resolution_mismatch_count": 0,
        "structure_table_resolution_mismatch_count": 0,
        "structure_row_resolution_mismatch_count": 0,
        "classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
        "classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    }
    if include_requirement_view:
        evidence.update(
            {
                "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
                "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            }
        )
    if fidelity["status"] != "PASS":
        return None, {
            "status": "FAIL",
            "reason_code": "pdf_revision_history_source_scope_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    document_contexts: list[dict[str, Any]] = []
    for expected_document in source_oracle["fact"]["documents"]:
        document_id = str(expected_document["document_id"])
        document_result = document_results.get(document_id)
        semantic_context = (
            _pdf_semantic_context_for_document(
                document_id=document_id,
                document_result=document_result,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
            )
            if document_result is not None
            else None
        )
        if semantic_context is None:
            continue
        source_snapshot, tree, semantics = semantic_context
        view = None
        if include_requirement_view:
            if requirement_view_snapshots is None:
                continue
            requirement_context = _pdf_requirement_context_for_document(
                document_id=document_id,
                document_result=document_result,
                source_ir_snapshots=source_ir_snapshots,
                structure_snapshots=structure_snapshots,
                semantic_snapshots=semantic_snapshots,
                requirement_view_snapshots=requirement_view_snapshots,
            )
            if requirement_context is None:
                continue
            source_snapshot, tree, semantics, view = requirement_context
        locator = expected_document["table"]["locator"]
        source_tables = [
            node
            for node in source_snapshot.nodes
            if node.kind is SourceNodeKind.TABLE
            and node.native_locator.page_number == int(locator["page_number"])
            and node.native_locator.table_index == int(locator["table_index"])
            and node.table_payload is not None
        ]
        if len(source_tables) != 1:
            evidence["source_table_resolution_mismatch_count"] += 1
            continue
        source_table = source_tables[0]
        structure_tables = [
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE
            and _source_node_ref(source_table.source_node_id) in node.source_refs
        ]
        if len(structure_tables) != 1:
            evidence["structure_table_resolution_mismatch_count"] += 1
            continue
        structure_table = structure_tables[0]
        row_nodes: dict[int, Any] = {}
        for row_index in expected_document["revision_row_indices"]:
            candidates = [
                node
                for node in tree.nodes
                if node.kind is StructureNodeKind.TABLE_ROW
                and node.parent_structure_node_id == structure_table.structure_node_id
                and node.table_row_index == int(row_index)
            ]
            if len(candidates) != 1:
                evidence["structure_row_resolution_mismatch_count"] += 1
                continue
            row_nodes[int(row_index)] = candidates[0]
        document_contexts.append(
            {
                "document_id": document_id,
                "source_snapshot": source_snapshot,
                "tree": tree,
                "semantics": semantics,
                "view": view,
                "source_table": source_table,
                "structure_table": structure_table,
                "row_nodes": row_nodes,
                "expected_revision_row_indices": [
                    int(row_index)
                    for row_index in expected_document["revision_row_indices"]
                ],
            }
        )

    evidence["resolved_document_count"] = len(document_contexts)
    evidence["resolved_revision_row_count"] = sum(
        len(context["row_nodes"]) for context in document_contexts
    )
    if (
        len(document_contexts) != evidence["expected_document_count"]
        or evidence["resolved_revision_row_count"]
        != evidence["expected_revision_row_count"]
        or evidence["source_table_resolution_mismatch_count"]
        or evidence["structure_table_resolution_mismatch_count"]
        or evidence["structure_row_resolution_mismatch_count"]
    ):
        return None, {
            "status": "FAIL",
            "reason_code": "pdf_revision_history_context_resolution_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return {
        "documents": document_contexts,
        "evidence": evidence,
    }, None


def _evaluate_pdf_revision_history_semantic(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_REVISION_HISTORY_SEMANTIC_EVALUATOR_ID
    context, failure = _pdf_revision_history_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        include_requirement_view=False,
    )
    if failure is not None:
        return failure
    assert context is not None
    expected_count = context["evidence"]["expected_revision_row_count"]
    annotation_count = 0
    valid_annotation_count = 0
    for document in context["documents"]:
        for row_node in document["row_nodes"].values():
            annotations = [
                annotation
                for annotation in document["semantics"].annotations
                if annotation.target_structure_node_id == row_node.structure_node_id
            ]
            annotation_count += len(annotations)
            valid_annotation_count += sum(
                annotation.semantic_class is SemanticClass.VERSION_METADATA
                and annotation.requirement_subtype is None
                and annotation.status
                in {
                    SemanticAnnotationStatus.CANDIDATE,
                    SemanticAnnotationStatus.CONFIRMED,
                }
                for annotation in annotations
            )
    evidence = context["evidence"]
    evidence.update(
        {
            "revision_row_annotation_count": annotation_count,
            "valid_version_metadata_annotation_count": valid_annotation_count,
        }
    )
    matches = bool(annotation_count == valid_annotation_count == expected_count)
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_revision_history_metadata_observed"
            if matches
            else "pdf_revision_history_semantic_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_revision_history_not_requirements(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_REVISION_HISTORY_NOT_REQUIREMENTS_EVALUATOR_ID
    context, failure = _pdf_revision_history_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        include_requirement_view=True,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    revision_row_ids = {
        row_node.structure_node_id
        for document in context["documents"]
        for row_node in document["row_nodes"].values()
    }
    projected_count = sum(
        node.target_structure_node_id in revision_row_ids
        for document in context["documents"]
        for node in document["view"].nodes
    )
    evidence = context["evidence"]
    evidence["projected_revision_row_count"] = projected_count
    return {
        "status": "PASS" if projected_count == 0 else "FAIL",
        "reason_code": (
            "pdf_revision_rows_not_projected"
            if projected_count == 0
            else "pdf_revision_row_projection_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_revision_history_no_inference(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_REVISION_HISTORY_NO_INFERENCE_EVALUATOR_ID
    context, failure = _pdf_revision_history_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        include_requirement_view=True,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    direct_leak_count = 0
    context_leak_count = 0
    for document in context["documents"]:
        revision_refs = {
            source_ref
            for row_node in document["row_nodes"].values()
            for source_ref in row_node.source_refs
        }
        direct_leak_count += sum(
            bool(set(node.source_refs).intersection(revision_refs))
            for node in document["view"].nodes
        )
        context_leak_count += sum(
            bool(set(node.full_context_source_refs).intersection(revision_refs))
            for node in document["view"].nodes
        )
    evidence = context["evidence"]
    evidence.update(
        {
            "direct_revision_source_leak_count": direct_leak_count,
            "full_context_revision_source_leak_count": context_leak_count,
        }
    )
    matches = direct_leak_count == context_leak_count == 0
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_revision_summary_not_inferred"
            if matches
            else "pdf_revision_summary_source_leak"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_revision_snapshots_distinct(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_REVISION_SNAPSHOTS_DISTINCT_EVALUATOR_ID
    try:
        measurement = _measure_pdf_revision_history_group(
            oracle=source_oracle,
            document_results=document_results,
            source_ir_snapshots=source_ir_snapshots,
        )
    except Exception as exc:
        return {
            "status": "FAIL",
            "reason_code": "pdf_revision_snapshot_measurement_failed",
            "evaluator": evaluator_id,
            "evidence": {
                "source_oracle_id": str(source_oracle["oracle_id"]),
                "measurement_error_type": type(exc).__name__,
            },
        }
    source_ir_ids: list[str] = []
    source_sha256s: list[str] = []
    table_fingerprints: list[str] = []
    for expected_document in source_oracle["fact"]["documents"]:
        document_id = str(expected_document["document_id"])
        document_result = document_results.get(document_id)
        snapshot = (
            _source_ir_snapshot_for_document_result(
                document_result,
                source_ir_snapshots=source_ir_snapshots,
            )
            if document_result is not None
            else None
        )
        if snapshot is None:
            continue
        source_ir_ids.append(snapshot.source_ir_id)
        source_sha256s.append(snapshot.source_sha256)
        measured_document, _, _ = _measure_pdf_revision_history_document(
            snapshot,
            expected_document,
        )
        if measured_document is not None:
            table_fingerprints.append(measured_document["table_fingerprint"])
    expected_count = int(source_oracle["fact"]["expected_document_count"])
    evidence = {
        "source_oracle_id": str(source_oracle["oracle_id"]),
        "expected_document_count": expected_count,
        "observed_document_count": len(source_ir_ids),
        "distinct_source_ir_count": len(set(source_ir_ids)),
        "distinct_source_sha256_count": len(set(source_sha256s)),
        "distinct_revision_table_fingerprint_count": len(set(table_fingerprints)),
        "source_scope_mismatch_count": int(measurement["mismatch_count"]),
    }
    matches = bool(
        measurement["matches"]
        and len(source_ir_ids)
        == len(set(source_ir_ids))
        == len(set(source_sha256s))
        == len(set(table_fingerprints))
        == expected_count
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_revision_snapshots_distinct_with_diff"
            if matches
            else "pdf_revision_snapshot_distinction_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _scoped_table_row_candidates(
    tree: Any,
    *,
    table_structure_node: Any | None,
    row_index: int,
) -> list[Any]:
    if table_structure_node is None:
        return []
    return [
        node
        for node in tree.nodes
        if node.kind is StructureNodeKind.TABLE_ROW
        and node.parent_structure_node_id == table_structure_node.structure_node_id
        and node.table_row_index == row_index
    ]


def _docx_table_row_structure_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return None, failure
    assert source_snapshot is not None and tree is not None

    fact = source_oracle["fact"]
    if fact["kind"] != "docx_table_row_scope":
        evidence["row_fact_kind_mismatch_count"] = 1
        return None, {
            "status": "FAIL",
            "reason_code": "docx_table_row_source_scope_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    locator = fact["locator"]
    table_key = (
        int(locator["body_element_index"]),
        int(locator["table_index"]),
    )
    row_index = int(locator["row_index"])
    table_candidates = [
        (node, table)
        for node, table in _source_ir_tables(source_snapshot)
        if node.native_locator.format.value == "docx"
        and node.native_locator.body_element_index == table_key[0]
        and node.native_locator.table_index == table_key[1]
    ]
    row_cells = (
        sorted(
            (
                cell
                for cell in table_candidates[0][1].cells
                if cell.row == row_index
            ),
            key=lambda cell: (cell.column, cell.row_span, cell.column_span),
        )
        if len(table_candidates) == 1
        else []
    )
    table_structure_candidates: list[Any] = []
    row_candidates: list[Any] = []
    if len(table_candidates) == 1:
        table_source_node, _ = table_candidates[0]
        table_structure_candidates = [
            node
            for node in tree.nodes
            if node.identity_key == f"source:{table_source_node.source_node_id}"
        ]
        if len(table_structure_candidates) == 1:
            row_candidates = _scoped_table_row_candidates(
                tree,
                table_structure_node=table_structure_candidates[0],
                row_index=row_index,
            )

    scoped_cell_ids = {cell.cell_id for cell in row_cells}
    missing_scoped_refs = 0
    extra_nonempty_refs = 0
    invalid_boundary_count = 0
    nested_boundary_count = 0
    row_node = row_candidates[0] if len(row_candidates) == 1 else None
    if row_node is not None:
        row_cell_refs = {
            ref.source_id
            for ref in row_node.source_refs
            if ref.kind is StructureSourceRefKind.TABLE_CELL
        }
        missing_scoped_refs = len(scoped_cell_ids - row_cell_refs)
        table = table_candidates[0][1]
        extra_nonempty_refs = sum(
            1
            for cell in table.cells
            if cell.cell_id in row_cell_refs
            and cell.cell_id not in scoped_cell_ids
            and bool(cell.raw_text.strip())
        )
        expected_identity_key = (
            f"table:{table_candidates[0][0].source_node_id}:row:{row_index}"
        )
        invalid_boundary_count = int(
            row_node.identity_key != expected_identity_key
            or row_node.boundary_basis is not StructureBoundaryBasis.TABLE_ROW
        )
        descendant_ids = _structure_descendant_ids(
            tree,
            row_node.structure_node_id,
        ) - {row_node.structure_node_id}
        nested_boundary_count = sum(
            node.structure_node_id in descendant_ids
            and node.kind
            in {
                StructureNodeKind.CLAUSE,
                StructureNodeKind.LIST_ITEM,
                StructureNodeKind.TABLE_ROW,
            }
            for node in tree.nodes
        )

    evidence.update(
        {
            "expected_row_boundary_count": 1,
            "observed_row_boundary_count": len(row_candidates),
            "table_source_candidate_count": len(table_candidates),
            "table_structure_candidate_count": len(table_structure_candidates),
            "expected_scoped_cell_count": int(fact["anchor_cell_count"]),
            "observed_scoped_cell_count": len(row_cells),
            "missing_scoped_cell_ref_count": missing_scoped_refs,
            "extra_nonempty_cell_ref_count": extra_nonempty_refs,
            "invalid_boundary_count": invalid_boundary_count,
            "nested_boundary_count": nested_boundary_count,
        }
    )
    mismatch_count = sum(
        (
            abs(len(table_candidates) - 1),
            abs(len(table_structure_candidates) - 1),
            abs(len(row_candidates) - 1),
            abs(len(row_cells) - int(fact["anchor_cell_count"])),
            missing_scoped_refs,
            extra_nonempty_refs,
            invalid_boundary_count,
            nested_boundary_count,
        )
    )
    if mismatch_count:
        return None, {
            "status": "FAIL",
            "reason_code": "docx_single_table_row_boundary_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    assert row_node is not None
    return (
        {
            "source_snapshot": source_snapshot,
            "tree": tree,
            "row_node": row_node,
            "row_cells": row_cells,
            "scoped_cell_ids": scoped_cell_ids,
            "evidence": evidence,
        },
        None,
    )


def _evaluate_docx_single_table_row_boundary(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = STRUCTURE_DOCX_SINGLE_ROW_EVALUATOR_ID
    context, failure = _docx_table_row_structure_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    return {
        "status": "PASS",
        "reason_code": "docx_single_table_row_boundary_observed",
        "evaluator": evaluator_id,
        "evidence": context["evidence"],
    }


def _docx_table_row_projection_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    context, failure = _docx_table_row_structure_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return None, failure
    assert context is not None
    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    evidence = context["evidence"]
    evidence.update(
        {
            "classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
            "classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        }
    )

    semantic_key = (
        tree.structure_snapshot_id,
        DEFAULT_SEMANTIC_CLASSIFIER_ID,
        DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    )
    try:
        semantics = semantic_snapshots.get(semantic_key)
        if semantics is None:
            semantics = classify_content(
                source_snapshot,
                tree,
                classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            )
        validate_semantic_classification_against_inputs(
            semantics,
            source_snapshot,
            tree,
        )
    except SemanticClassificationRejected as exc:
        evidence["semantic_rejection_code"] = exc.code.value
        return None, {
            "status": "FAIL",
            "reason_code": "semantic_classification_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["semantic_error_type"] = type(exc).__name__
        return None, {
            "status": "FAIL",
            "reason_code": "semantic_classification_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    semantic_snapshots[semantic_key] = semantics

    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return None, {
            "status": "FAIL",
            "reason_code": "requirement_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return None, {
            "status": "FAIL",
            "reason_code": "requirement_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view

    row_node = context["row_node"]
    scoped_structure_ids = _structure_descendant_ids(
        tree,
        row_node.structure_node_id,
    )
    scoped_cell_ids = context["scoped_cell_ids"]
    scoped_annotations = [
        annotation
        for annotation in semantics.annotations
        if annotation.owner_structure_node_id in scoped_structure_ids
        or any(
            span.source_ref.kind is StructureSourceRefKind.TABLE_CELL
            and span.source_ref.source_id in scoped_cell_ids
            for span in annotation.grounding_spans
        )
    ]
    scoped_view_nodes = [
        node
        for node in view.nodes
        if node.owner_structure_node_id in scoped_structure_ids
        or any(
            ref.kind is StructureSourceRefKind.TABLE_CELL
            and ref.source_id in scoped_cell_ids
            for ref in node.source_refs
        )
    ]
    context.update(
        {
            "semantics": semantics,
            "view": view,
            "scoped_annotations": scoped_annotations,
            "scoped_view_nodes": scoped_view_nodes,
        }
    )
    return context, None


def _evaluate_docx_table_row_projection(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    context, failure = _docx_table_row_projection_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    row_node = context["row_node"]
    annotations = context["scoped_annotations"]
    view_nodes = context["scoped_view_nodes"]
    evidence = context["evidence"]
    active_requirement_annotations = [
        annotation
        for annotation in annotations
        if annotation.owner_structure_node_id == row_node.structure_node_id
        and annotation.semantic_class is SemanticClass.REQUIREMENT
        and annotation.requirement_subtype
        is RequirementSubtype.PRODUCT_REQUIREMENT
        and annotation.status
        in {
            SemanticAnnotationStatus.CANDIDATE,
            SemanticAnnotationStatus.CONFIRMED,
        }
    ]
    top_level_nodes = [
        node
        for node in view_nodes
        if node.parent_requirement_view_node_id is None
    ]
    primary_nodes = [
        node
        for node in top_level_nodes
        if node.owner_structure_node_id == row_node.structure_node_id
        and node.role is RequirementProjectionRole.PRIMARY
        and node.decision_mode is RequirementDecisionMode.DIRECT
        and node.semantic_class is SemanticClass.REQUIREMENT
        and node.requirement_subtype is RequirementSubtype.PRODUCT_REQUIREMENT
    ]
    evidence.update(
        {
            "scoped_semantic_annotation_count": len(annotations),
            "active_product_requirement_annotation_count": len(
                active_requirement_annotations
            ),
            "scoped_projection_count": len(view_nodes),
            "top_level_requirement_count": len(top_level_nodes),
            "primary_direct_product_requirement_count": len(primary_nodes),
            "distinct_semantic_unit_count": len(
                {
                    annotation.semantic_unit_id
                    for annotation in annotations
                }
            ),
        }
    )

    if (
        evaluator_id
        == PROJECTION_DOCX_SINGLE_TOP_LEVEL_REQUIREMENT_EVALUATOR_ID
    ):
        matches = bool(
            len(active_requirement_annotations) == 1
            and len(top_level_nodes) == 1
            and len(primary_nodes) == 1
        )
        return {
            "status": "PASS" if matches else "FAIL",
            "reason_code": (
                "docx_single_top_level_requirement_observed"
                if matches
                else "docx_single_top_level_requirement_mismatch"
            ),
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    if evaluator_id != PROJECTION_DOCX_NO_SENTENCE_SPLIT_EVALUATOR_ID:
        return {
            "status": "FAIL",
            "reason_code": "docx_table_row_projection_evaluator_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    split_count = sum(
        (
            max(0, len(annotations) - 1),
            max(0, len(view_nodes) - 1),
            max(0, len(top_level_nodes) - 1),
            max(0, evidence["distinct_semantic_unit_count"] - 1),
        )
    )
    evidence["sentence_or_parallel_verb_split_count"] = split_count
    matches = bool(
        len(active_requirement_annotations) == 1
        and len(annotations) == 1
        and len(view_nodes) == 1
        and len(top_level_nodes) == 1
        and len(primary_nodes) == 1
        and evidence["distinct_semantic_unit_count"] == 1
        and split_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "docx_sentence_split_not_observed"
            if matches
            else "docx_sentence_split_observed"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _fmt001_reviewed_bilingual_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    context, failure = _docx_table_row_structure_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return None, failure
    assert context is not None
    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    row_node = context["row_node"]
    evidence = context["evidence"]
    groups = tuple(
        group
        for group in tree.bilingual_counterpart_groups
        if row_node.structure_node_id in group.member_structure_node_ids
    )
    evidence["reviewed_bilingual_group_count"] = len(groups)
    if len(groups) != 1:
        return None, {
            "status": "FAIL",
            "reason_code": "fmt001_reviewed_bilingual_group_missing",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    group = groups[0]
    source_nodes_by_id = {
        node.source_node_id: node for node in source_snapshot.nodes
    }
    row_source_refs = set(row_node.source_refs)
    all_spans = (*group.english_spans, *group.chinese_spans)
    span_source_ids = [span.source_ref.source_id for span in all_spans]
    invalid_span_count = 0
    spans_by_source_id: dict[str, list[Any]] = {}
    for span in all_spans:
        source_node = source_nodes_by_id.get(span.source_ref.source_id)
        if (
            span.source_ref.kind is not StructureSourceRefKind.SOURCE_NODE
            or span.source_ref not in row_source_refs
            or source_node is None
            or not source_node.raw_text
            or span.start_offset < 0
            or span.end_offset <= span.start_offset
            or span.end_offset > len(source_node.raw_text)
        ):
            invalid_span_count += 1
            continue
        spans_by_source_id.setdefault(span.source_ref.source_id, []).append(span)
    incomplete_source_interval_count = 0
    raw_sha256s: list[str] = []
    for source_id, spans in spans_by_source_id.items():
        source_node = source_nodes_by_id[source_id]
        ordered = sorted(
            spans,
            key=lambda span: (
                span.start_offset,
                span.end_offset,
                span.bilingual_span_id,
            ),
        )
        if (
            ordered[0].start_offset != 0
            or ordered[-1].end_offset != len(source_node.raw_text)
            or any(
                current.end_offset != following.start_offset
                for current, following in zip(ordered, ordered[1:])
            )
        ):
            incomplete_source_interval_count += 1
        raw_sha256s.append(_sha256_text(source_node.raw_text))
    duplicate_span_source_count = len(span_source_ids) - len(set(span_source_ids))
    evidence.update(
        {
            "english_span_count": len(group.english_spans),
            "chinese_span_count": len(group.chinese_spans),
            "distinct_span_source_count": len(set(span_source_ids)),
            "invalid_span_count": invalid_span_count,
            "incomplete_source_interval_count": incomplete_source_interval_count,
            "duplicate_span_source_count": duplicate_span_source_count,
            "span_raw_sha256s": sorted(raw_sha256s),
            "group_member_count": len(group.member_structure_node_ids),
        }
    )
    context["bilingual_group"] = group
    context["fmt001_span_contract_matches"] = bool(
        len(group.english_spans) == 2
        and len(group.chinese_spans) == 2
        and len(set(span_source_ids)) == 4
        and invalid_span_count == 0
        and incomplete_source_interval_count == 0
        and duplicate_span_source_count == 0
    )
    return context, None


def _evaluate_fmt001_reviewed_bilingual_group(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    context, failure = _fmt001_reviewed_bilingual_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    group = context["bilingual_group"]
    tree = context["tree"]
    row_node = context["row_node"]
    evidence = context["evidence"]
    cohort_contexts, cohort_evidence, cohort_failure = (
        _fmt001_bilingual_group_cohort_measurement(
            evaluator_id=evaluator_id,
            document_results=document_results,
            source_ir_snapshots=source_ir_snapshots,
            structure_snapshots=structure_snapshots,
        )
    )
    if cohort_failure is not None:
        return cohort_failure
    assert cohort_contexts is not None
    evidence.update(cohort_evidence)
    evidence["reviewed_anchor_document_id"] = str(source_oracle["document_id"])
    cohort_span_contract_matches = bool(
        cohort_evidence["cohort_observed_document_count"]
        == cohort_evidence["cohort_expected_document_count"]
        and cohort_evidence["cohort_document_without_group_count"] == 0
        and cohort_evidence["cohort_group_without_both_language_sides_count"] == 0
        and cohort_evidence["cohort_invalid_span_count"] == 0
        and cohort_evidence["cohort_duplicate_span_id_count"] == 0
        and cohort_evidence["cohort_non_exact_cover_group_count"] == 0
        and cohort_evidence["cohort_group_owner_scope_mismatch_count"] == 0
        and cohort_evidence["cohort_policy_binding_mismatch_group_count"] == 0
    )
    if evaluator_id == SOURCE_DOCX_BILINGUAL_TEXTS_EVALUATOR_ID:
        matches = bool(
            context["fmt001_span_contract_matches"]
            and cohort_span_contract_matches
        )
        return {
            "status": "PASS" if matches else "FAIL",
            "reason_code": (
                "fmt001_bilingual_source_spans_preserved"
                if matches
                else "fmt001_bilingual_source_spans_mismatch"
            ),
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if evaluator_id != STRUCTURE_BILINGUAL_COUNTERPART_EVALUATOR_ID:
        return {
            "status": "FAIL",
            "reason_code": "fmt001_bilingual_group_evaluator_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    self_translation_relation_count = sum(
        relation.kind is StructureRelationKind.TRANSLATION_OF
        and relation.source_structure_node_id
        == relation.target_structure_node_id
        for relation in tree.relations
    )
    evidence.update(
        {
            "typed_translation_relation": (
                group.relation_kind.value
                == StructureRelationKind.TRANSLATION_OF.value
            ),
            "translation_relation_materialization_count": int(
                group.translation_relation_id is not None
            ),
            "self_translation_relation_count": self_translation_relation_count,
            "canonical_owner_matches_reviewed_row": (
                group.canonical_structure_node_id
                == row_node.structure_node_id
            ),
        }
    )
    cohort_relation_contract_matches = bool(
        cohort_evidence["cohort_invalid_relation_kind_group_count"] == 0
        and cohort_evidence[
            "cohort_relation_materialization_mismatch_group_count"
        ]
        == 0
        and cohort_evidence["cohort_self_translation_relation_count"] == 0
    )
    matches = bool(
        context["fmt001_span_contract_matches"]
        and cohort_span_contract_matches
        and cohort_relation_contract_matches
        and group.member_structure_node_ids
        == (row_node.structure_node_id,)
        and group.canonical_structure_node_id == row_node.structure_node_id
        and group.relation_kind is StructureRelationKind.TRANSLATION_OF
        and group.translation_relation_id is None
        and self_translation_relation_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "fmt001_bilingual_counterpart_group_observed"
            if matches
            else "fmt001_bilingual_counterpart_group_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_fmt001_bilingual_projection(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PROJECTION_BILINGUAL_SINGLE_REQUIREMENT_EVALUATOR_ID
    context, failure = _docx_table_row_projection_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    tree = context["tree"]
    row_node = context["row_node"]
    evidence = context["evidence"]
    cohort_evidence, cohort_failure = (
        _fmt001_bilingual_projection_cohort_measurement(
            evaluator_id=evaluator_id,
            document_results=document_results,
            source_ir_snapshots=source_ir_snapshots,
            structure_snapshots=structure_snapshots,
            semantic_snapshots=semantic_snapshots,
            requirement_view_snapshots=requirement_view_snapshots,
        )
    )
    if cohort_failure is not None:
        return cohort_failure
    evidence.update(cohort_evidence)
    evidence["reviewed_anchor_document_id"] = str(source_oracle["document_id"])
    groups = tuple(
        group
        for group in tree.bilingual_counterpart_groups
        if row_node.structure_node_id in group.member_structure_node_ids
    )
    if len(groups) != 1:
        evidence["reviewed_bilingual_group_count"] = len(groups)
        return {
            "status": "FAIL",
            "reason_code": "fmt001_reviewed_bilingual_group_missing",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    group = groups[0]
    group_source_refs = {
        span.source_ref for span in (*group.english_spans, *group.chinese_spans)
    }
    group_view_nodes = tuple(
        node
        for node in context["view"].nodes
        if (
            node.owner_structure_node_id in group.member_structure_node_ids
            or group_source_refs.intersection(node.source_refs)
        )
    )
    exact_grounding_view_count = sum(
        group_source_refs.issubset(node.source_refs) for node in group_view_nodes
    )
    requirement_view_count = sum(
        node.semantic_class is SemanticClass.REQUIREMENT
        for node in group_view_nodes
    )
    evidence.update(
        {
            "reviewed_bilingual_group_count": 1,
            "bilingual_group_projection_count": len(group_view_nodes),
            "bilingual_group_requirement_count": requirement_view_count,
            "union_grounding_view_count": exact_grounding_view_count,
            "source_language_side_count": 2,
        }
    )
    matches = bool(
        len(group_view_nodes) == 1
        and requirement_view_count == 1
        and exact_grounding_view_count == 1
        and cohort_evidence["cohort_observed_document_count"]
        == cohort_evidence["cohort_expected_document_count"]
        and cohort_evidence["cohort_document_without_group_count"] == 0
        and cohort_evidence["cohort_group_without_both_language_sides_count"]
        == 0
        and cohort_evidence["cohort_invalid_span_count"] == 0
        and cohort_evidence["cohort_duplicate_span_id_count"] == 0
        and cohort_evidence["cohort_non_exact_cover_group_count"] == 0
        and cohort_evidence["cohort_group_owner_scope_mismatch_count"] == 0
        and cohort_evidence["cohort_policy_binding_mismatch_group_count"] == 0
        and cohort_evidence["cohort_invalid_relation_kind_group_count"] == 0
        and cohort_evidence[
            "cohort_relation_materialization_mismatch_group_count"
        ]
        == 0
        and cohort_evidence["cohort_self_translation_relation_count"] == 0
        and cohort_evidence[
            "cohort_requirement_projection_overflow_group_count"
        ]
        == 0
        and cohort_evidence[
            "cohort_requirement_projection_grounding_mismatch_group_count"
        ]
        == 0
        and cohort_evidence[
            "cohort_document_without_exactly_one_requirement_group_count"
        ]
        == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "fmt001_bilingual_single_requirement_observed"
            if matches
            else "fmt001_bilingual_requirement_projection_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _fmt001_document_tree_contexts(
    *,
    evaluator_id: str,
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[list[tuple[str, Any, Any]] | None, dict[str, Any] | None]:
    contexts: list[tuple[str, Any, Any]] = []
    evidence: dict[str, Any] = {
        "expected_document_count": len(document_results),
        "resolver_id": DEFAULT_STRUCTURE_RESOLVER_ID,
        "resolver_version": DEFAULT_STRUCTURE_RESOLVER_VERSION,
    }
    for document_id in sorted(document_results):
        result = document_results[document_id]
        source_snapshot = _source_ir_snapshot_for_document_result(
            result,
            source_ir_snapshots=source_ir_snapshots,
        )
        if source_snapshot is None:
            evidence["missing_source_document_id"] = document_id
            return None, {
                "status": "FAIL",
                "reason_code": "fmt001_source_snapshot_missing",
                "evaluator": evaluator_id,
                "evidence": evidence,
            }
        structure_key = (
            source_snapshot.source_ir_id,
            DEFAULT_STRUCTURE_RESOLVER_ID,
            DEFAULT_STRUCTURE_RESOLVER_VERSION,
        )
        try:
            tree = structure_snapshots.get(structure_key)
            if tree is None:
                tree = build_structure_tree(
                    source_snapshot,
                    resolver_id=DEFAULT_STRUCTURE_RESOLVER_ID,
                    resolver_version=DEFAULT_STRUCTURE_RESOLVER_VERSION,
                )
            validate_structure_tree_against_source_ir(tree, source_snapshot)
        except StructureResolutionRejected as exc:
            evidence["structure_rejection_code"] = exc.code.value
            return None, {
                "status": "FAIL",
                "reason_code": "fmt001_structure_resolution_rejected",
                "evaluator": evaluator_id,
                "evidence": evidence,
            }
        except Exception as exc:
            evidence["structure_error_type"] = type(exc).__name__
            return None, {
                "status": "FAIL",
                "reason_code": "fmt001_structure_resolution_failed",
                "evaluator": evaluator_id,
                "evidence": evidence,
            }
        structure_snapshots[structure_key] = tree
        contexts.append((document_id, source_snapshot, tree))
    evidence["observed_document_count"] = len(contexts)
    return contexts, None


def _fmt001_bilingual_group_cohort_measurement(
    *,
    evaluator_id: str,
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[
    list[tuple[str, Any, Any]] | None,
    dict[str, Any],
    dict[str, Any] | None,
]:
    contexts, failure = _fmt001_document_tree_contexts(
        evaluator_id=evaluator_id,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    evidence: dict[str, Any] = {
        "cohort_expected_document_count": len(document_results),
    }
    if failure is not None:
        failure_evidence = dict(failure.get("evidence") or {})
        failure_evidence.update(evidence)
        failure["evidence"] = failure_evidence
        return None, evidence, failure
    assert contexts is not None

    group_counts_by_document: dict[str, int] = {}
    total_group_count = 0
    document_without_group_count = 0
    group_without_both_language_sides_count = 0
    invalid_span_count = 0
    duplicate_span_id_count = 0
    non_exact_cover_group_count = 0
    group_owner_scope_mismatch_count = 0
    policy_binding_mismatch_group_count = 0
    invalid_relation_kind_group_count = 0
    relation_materialization_mismatch_group_count = 0
    self_translation_relation_count = 0

    for document_id, source_snapshot, tree in contexts:
        source_nodes_by_id = {
            node.source_node_id: node for node in source_snapshot.nodes
        }
        structure_nodes_by_id = {
            node.structure_node_id: node for node in tree.nodes
        }
        relations_by_id = {
            relation.relation_id: relation for relation in tree.relations
        }
        groups = tuple(tree.bilingual_counterpart_groups)
        group_counts_by_document[document_id] = len(groups)
        total_group_count += len(groups)
        document_without_group_count += int(not groups)
        self_translation_relation_count += sum(
            relation.kind is StructureRelationKind.TRANSLATION_OF
            and relation.source_structure_node_id
            == relation.target_structure_node_id
            for relation in tree.relations
        )

        for group in groups:
            all_spans = (*group.english_spans, *group.chinese_spans)
            group_without_both_language_sides_count += int(
                not group.english_spans or not group.chinese_spans
            )
            duplicate_span_id_count += len(all_spans) - len(
                {span.bilingual_span_id for span in all_spans}
            )
            spans_by_source_id: dict[str, list[Any]] = {}
            group_invalid_span_count = 0
            for span in all_spans:
                source_node = source_nodes_by_id.get(span.source_ref.source_id)
                expected_language = (
                    BilingualLanguageEvidence.EN
                    if span in group.english_spans
                    else BilingualLanguageEvidence.ZH_CN
                )
                if (
                    span.source_ref.kind
                    is not StructureSourceRefKind.SOURCE_NODE
                    or source_node is None
                    or span.language_evidence is not expected_language
                    or span.start_offset < 0
                    or span.end_offset <= span.start_offset
                    or (
                        source_node is not None
                        and span.end_offset > len(source_node.raw_text)
                    )
                ):
                    group_invalid_span_count += 1
                    continue
                spans_by_source_id.setdefault(
                    span.source_ref.source_id,
                    [],
                ).append(span)
            invalid_span_count += group_invalid_span_count

            exact_cover = bool(spans_by_source_id) and (
                len(spans_by_source_id)
                == len({span.source_ref.source_id for span in all_spans})
            )
            for source_id, spans in spans_by_source_id.items():
                source_node = source_nodes_by_id[source_id]
                ordered = sorted(
                    spans,
                    key=lambda span: (
                        span.start_offset,
                        span.end_offset,
                        span.bilingual_span_id,
                    ),
                )
                exact_cover = bool(
                    exact_cover
                    and ordered[0].start_offset == 0
                    and ordered[-1].end_offset == len(source_node.raw_text)
                    and all(
                        current.end_offset == following.start_offset
                        for current, following in zip(ordered, ordered[1:])
                    )
                )
            non_exact_cover_group_count += int(
                group_invalid_span_count > 0 or not exact_cover
            )

            member_source_ids = {
                source_ref.source_id
                for member_id in group.member_structure_node_ids
                for source_ref in (
                    structure_nodes_by_id[member_id].source_refs
                    if member_id in structure_nodes_by_id
                    else ()
                )
                if source_ref.kind is StructureSourceRefKind.SOURCE_NODE
            }
            group_span_source_ids = {
                span.source_ref.source_id for span in all_spans
            }
            group_owner_scope_mismatch_count += int(
                any(
                    member_id not in structure_nodes_by_id
                    for member_id in group.member_structure_node_ids
                )
                or group.canonical_structure_node_id
                not in group.member_structure_node_ids
                or not group_span_source_ids.issubset(member_source_ids)
            )
            policy_binding_mismatch_group_count += int(
                tree.document_language_policy is None
                or group.document_language_policy_id
                != tree.document_language_policy.document_language_policy_id
            )

            invalid_relation_kind_group_count += int(
                group.relation_kind is not StructureRelationKind.TRANSLATION_OF
            )
            member_ids = tuple(group.member_structure_node_ids)
            relation_shape_matches = False
            if len(member_ids) == 1:
                relation_shape_matches = group.translation_relation_id is None
            elif len(member_ids) == 2 and group.translation_relation_id is not None:
                relation = relations_by_id.get(group.translation_relation_id)
                other_member_id = next(
                    member_id
                    for member_id in member_ids
                    if member_id != group.canonical_structure_node_id
                )
                relation_shape_matches = bool(
                    relation is not None
                    and relation.kind is StructureRelationKind.TRANSLATION_OF
                    and relation.source_structure_node_id
                    == group.canonical_structure_node_id
                    and relation.target_structure_node_id == other_member_id
                    and relation.source_structure_node_id
                    != relation.target_structure_node_id
                    and set(relation.source_refs)
                    == {span.source_ref for span in all_spans}
                )
            relation_materialization_mismatch_group_count += int(
                not relation_shape_matches
            )

    evidence.update(
        {
            "cohort_observed_document_count": len(contexts),
            "cohort_bilingual_group_count": total_group_count,
            "cohort_bilingual_group_counts_by_document": (
                group_counts_by_document
            ),
            "cohort_document_without_group_count": (
                document_without_group_count
            ),
            "cohort_group_without_both_language_sides_count": (
                group_without_both_language_sides_count
            ),
            "cohort_invalid_span_count": invalid_span_count,
            "cohort_duplicate_span_id_count": duplicate_span_id_count,
            "cohort_non_exact_cover_group_count": (
                non_exact_cover_group_count
            ),
            "cohort_group_owner_scope_mismatch_count": (
                group_owner_scope_mismatch_count
            ),
            "cohort_policy_binding_mismatch_group_count": (
                policy_binding_mismatch_group_count
            ),
            "cohort_invalid_relation_kind_group_count": (
                invalid_relation_kind_group_count
            ),
            "cohort_relation_materialization_mismatch_group_count": (
                relation_materialization_mismatch_group_count
            ),
            "cohort_self_translation_relation_count": (
                self_translation_relation_count
            ),
            "semantic_equivalence_evaluated": False,
        }
    )
    return contexts, evidence, None


def _fmt001_bilingual_projection_cohort_measurement(
    *,
    evaluator_id: str,
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    contexts, evidence, failure = _fmt001_bilingual_group_cohort_measurement(
        evaluator_id=evaluator_id,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return evidence, failure
    assert contexts is not None

    requirement_projection_count = 0
    projection_overflow_group_count = 0
    projection_grounding_mismatch_group_count = 0
    exactly_one_group_count = 0
    exactly_one_group_counts_by_document: dict[str, int] = {}
    document_without_exactly_one_group_count = 0

    for document_id, source_snapshot, tree in contexts:
        semantic_key = (
            tree.structure_snapshot_id,
            DEFAULT_SEMANTIC_CLASSIFIER_ID,
            DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        )
        try:
            semantics = semantic_snapshots.get(semantic_key)
            if semantics is None:
                semantics = classify_content(
                    source_snapshot,
                    tree,
                    classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                    classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
                )
            validate_semantic_classification_against_inputs(
                semantics,
                source_snapshot,
                tree,
            )
        except SemanticClassificationRejected as exc:
            evidence["cohort_semantic_rejection_code"] = exc.code.value
            evidence["cohort_semantic_failure_document_id"] = document_id
            return evidence, {
                "status": "FAIL",
                "reason_code": "fmt001_cohort_semantic_classification_rejected",
                "evaluator": evaluator_id,
                "evidence": evidence,
            }
        except Exception as exc:
            evidence["cohort_semantic_error_type"] = type(exc).__name__
            evidence["cohort_semantic_failure_document_id"] = document_id
            return evidence, {
                "status": "FAIL",
                "reason_code": "fmt001_cohort_semantic_classification_failed",
                "evaluator": evaluator_id,
                "evidence": evidence,
            }
        semantic_snapshots[semantic_key] = semantics

        view_key = (
            semantics.semantic_snapshot_id,
            DEFAULT_REQUIREMENT_PROJECTOR_ID,
            DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
        )
        try:
            view = requirement_view_snapshots.get(view_key)
            if view is None:
                view = project_requirements(
                    source_snapshot,
                    tree,
                    semantics,
                    projection_policy=(
                        RequirementProjectionPolicy.SHADOW_CANDIDATES
                    ),
                    projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                    projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
                )
            validate_requirement_view_against_inputs(
                view,
                source_snapshot,
                tree,
                semantics,
            )
        except RequirementProjectionRejected as exc:
            evidence["cohort_projection_rejection_code"] = exc.code.value
            evidence["cohort_projection_failure_document_id"] = document_id
            return evidence, {
                "status": "FAIL",
                "reason_code": "fmt001_cohort_requirement_projection_rejected",
                "evaluator": evaluator_id,
                "evidence": evidence,
            }
        except Exception as exc:
            evidence["cohort_projection_error_type"] = type(exc).__name__
            evidence["cohort_projection_failure_document_id"] = document_id
            return evidence, {
                "status": "FAIL",
                "reason_code": "fmt001_cohort_requirement_projection_failed",
                "evaluator": evaluator_id,
                "evidence": evidence,
            }
        requirement_view_snapshots[view_key] = view

        document_exactly_one_count = 0
        for group in tree.bilingual_counterpart_groups:
            group_source_refs = {
                span.source_ref
                for span in (*group.english_spans, *group.chinese_spans)
            }
            requirement_nodes = tuple(
                node
                for node in view.nodes
                if node.semantic_class is SemanticClass.REQUIREMENT
                and (
                    node.owner_structure_node_id
                    in group.member_structure_node_ids
                    or bool(group_source_refs.intersection(node.source_refs))
                )
            )
            requirement_projection_count += len(requirement_nodes)
            projection_overflow_group_count += int(
                len(requirement_nodes) > 1
            )
            if len(requirement_nodes) == 1:
                exact_grounding = group_source_refs.issubset(
                    requirement_nodes[0].source_refs
                )
                projection_grounding_mismatch_group_count += int(
                    not exact_grounding
                )
                if exact_grounding:
                    exactly_one_group_count += 1
                    document_exactly_one_count += 1
        exactly_one_group_counts_by_document[document_id] = (
            document_exactly_one_count
        )
        document_without_exactly_one_group_count += int(
            document_exactly_one_count == 0
        )

    evidence.update(
        {
            "cohort_classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
            "cohort_classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            "cohort_projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "cohort_projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            "cohort_requirement_projection_count": (
                requirement_projection_count
            ),
            "cohort_requirement_projection_overflow_group_count": (
                projection_overflow_group_count
            ),
            "cohort_requirement_projection_grounding_mismatch_group_count": (
                projection_grounding_mismatch_group_count
            ),
            "cohort_exactly_one_requirement_group_count": (
                exactly_one_group_count
            ),
            "cohort_exactly_one_requirement_group_counts_by_document": (
                exactly_one_group_counts_by_document
            ),
            "cohort_document_without_exactly_one_requirement_group_count": (
                document_without_exactly_one_group_count
            ),
        }
    )
    return evidence, None


def _evaluate_fmt001_document_language_policy(
    *,
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = STRUCTURE_DOCUMENT_LANGUAGE_POLICY_EVALUATOR_ID
    contexts, failure = _fmt001_document_tree_contexts(
        evaluator_id=evaluator_id,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert contexts is not None
    missing_policy_count = 0
    invalid_policy_count = 0
    blocking_policy_finding_count = 0
    for _, source_snapshot, tree in contexts:
        policy = tree.document_language_policy
        if policy is None:
            missing_policy_count += 1
            continue
        source_nodes_by_id = {
            node.source_node_id: node for node in source_snapshot.nodes
        }
        spans = (
            *policy.english_declaration_spans,
            *policy.chinese_declaration_spans,
        )
        valid_spans = all(
            span.source_ref.kind is StructureSourceRefKind.SOURCE_NODE
            and span.source_ref.source_id in source_nodes_by_id
            and span.start_offset == 0
            and span.end_offset
            == len(source_nodes_by_id[span.source_ref.source_id].raw_text)
            for span in spans
        )
        invalid_policy_count += int(
            not (
                policy.equal_validity is True
                and policy.conflict_precedence == "zh-CN"
                and len(policy.english_declaration_spans) == 1
                and len(policy.chinese_declaration_spans) == 1
                and valid_spans
            )
        )
        blocking_policy_finding_count += sum(
            finding.blocking
            and finding.code.startswith("document_language_policy_")
            for finding in tree.findings
        )
    evidence = {
        "expected_document_count": len(document_results),
        "observed_document_count": len(contexts),
        "typed_policy_count": sum(
            tree.document_language_policy is not None for _, _, tree in contexts
        ),
        "missing_policy_count": missing_policy_count,
        "invalid_policy_count": invalid_policy_count,
        "blocking_policy_finding_count": blocking_policy_finding_count,
        "equal_validity": True,
        "conflict_precedence": "zh-CN",
    }
    matches = bool(
        len(contexts) == len(document_results)
        and missing_policy_count == 0
        and invalid_policy_count == 0
        and blocking_policy_finding_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "fmt001_document_language_policy_observed"
            if matches
            else "fmt001_document_language_policy_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_fmt001_bilingual_mismatch_safety(
    *,
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = FINDING_BILINGUAL_MISMATCH_SAFETY_EVALUATOR_ID
    contexts, failure = _fmt001_document_tree_contexts(
        evaluator_id=evaluator_id,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert contexts is not None
    model_field_names = set(BilingualMismatchSignal.model_fields)
    forbidden_field_count = sum(
        "replacement" in field_name or field_name.endswith("_text")
        for field_name in model_field_names
    )
    invalid_signal_count = 0
    signal_count = 0
    for _, _, tree in contexts:
        groups_by_id = {
            group.counterpart_group_id: group
            for group in tree.bilingual_counterpart_groups
        }
        for signal in tree.bilingual_mismatch_signals:
            signal_count += 1
            group = groups_by_id.get(signal.counterpart_group_id)
            invalid_signal_count += int(
                group is None
                or signal.severity is not StructureFindingSeverity.WARNING
                or signal.blocking is not False
                or not signal.english_span_ids
                or not signal.chinese_span_ids
                or (
                    group is not None
                    and (
                        not set(signal.english_span_ids).issubset(
                            span.bilingual_span_id for span in group.english_spans
                        )
                        or not set(signal.chinese_span_ids).issubset(
                            span.bilingual_span_id for span in group.chinese_spans
                        )
                    )
                )
            )
    evidence = {
        "expected_document_count": len(document_results),
        "observed_document_count": len(contexts),
        "typed_mismatch_signal_count": signal_count,
        "invalid_mismatch_signal_count": invalid_signal_count,
        "replacement_field_count": forbidden_field_count,
        "machine_written_replacement_count": 0,
        "semantic_equivalence_evaluated": False,
        "real_mismatch_assertion_count": 0,
        "trusted_reviewer_gold_required": True,
        "trusted_reviewer_gold_available": False,
        "external_truth_gate": "HUMAN_OR_TRUSTED_REVIEWER_GOLD_REQUIRED",
    }
    # The current corpus has no trusted semantic-equivalence Gold.  PASS here
    # covers only the typed non-mutating safety boundary.  Any real mismatch
    # claim must remain absent until that external gate is supplied.
    matches = bool(
        len(contexts) == len(document_results)
        and forbidden_field_count == 0
        and invalid_signal_count == 0
        and signal_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "fmt001_bilingual_mismatch_safety_contract_observed"
            if matches
            else "fmt001_bilingual_mismatch_safety_contract_violated"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_single_table_row_boundary(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = STRUCTURE_SINGLE_ROW_EVALUATOR_ID
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert source_snapshot is not None and tree is not None

    fact = source_oracle["fact"]
    locator = fact["locator"]
    target_bounds = _parse_a1_range(str(locator["cell_range"]))
    table_candidates = _xlsx_table_nodes_containing_range(
        source_snapshot,
        locator=locator,
        target_bounds=target_bounds,
    )
    scoped_cells: list[Any] = []
    row_indexes: set[int] = set()
    if len(table_candidates) == 1:
        _, table = table_candidates[0]
        for cell in table.cells:
            native = cell.native_locator
            if native.cell_range is not None and _bounds_contain(
                target_bounds,
                _parse_a1_range(native.cell_range),
            ):
                scoped_cells.append(cell)
                row_indexes.add(cell.row)

    nodes_by_identity = {node.identity_key: node for node in tree.nodes}
    observed_rows: list[Any] = []
    missing_scoped_refs = 0
    extra_nonempty_refs = 0
    invalid_boundary_count = 0
    if len(table_candidates) == 1:
        table_node, _ = table_candidates[0]
        table_structure = nodes_by_identity.get(f"source:{table_node.source_node_id}")
        for row_index in sorted(row_indexes):
            expected_identity_key = f"table:{table_node.source_node_id}:row:{row_index}"
            row_candidates = _scoped_table_row_candidates(
                tree,
                table_structure_node=table_structure,
                row_index=row_index,
            )
            observed_rows.extend(row_candidates)
            invalid_boundary_count += abs(len(row_candidates) - 1)
            for row_node in row_candidates:
                row_cell_refs = {
                    ref.source_id
                    for ref in row_node.source_refs
                    if ref.kind is StructureSourceRefKind.TABLE_CELL
                }
                missing_scoped_refs += len(
                    {cell.cell_id for cell in scoped_cells} - row_cell_refs
                )
                extra_nonempty_refs += sum(
                    1
                    for cell in table.cells
                    if cell.cell_id in row_cell_refs
                    and cell not in scoped_cells
                    and bool(cell.raw_text.strip())
                )
                if (
                    row_node.identity_key != expected_identity_key
                    or row_node.boundary_basis is not StructureBoundaryBasis.TABLE_ROW
                ):
                    invalid_boundary_count += 1

    evidence.update(
        {
            "expected_row_boundary_count": 1,
            "observed_row_boundary_count": len(observed_rows),
            "scoped_cell_count": len(scoped_cells),
            "missing_scoped_cell_ref_count": missing_scoped_refs,
            "extra_nonempty_cell_ref_count": extra_nonempty_refs,
            "invalid_boundary_count": invalid_boundary_count,
        }
    )
    matches = bool(
        len(table_candidates) == 1
        and len(row_indexes) == 1
        and len(observed_rows) == 1
        and len(scoped_cells) == int(fact["expected_cell_count"])
        and missing_scoped_refs == 0
        and extra_nonempty_refs == 0
        and invalid_boundary_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "single_table_row_boundary_observed"
            if matches
            else "single_table_row_boundary_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _compound_table_row_semantic_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return None, failure
    assert source_snapshot is not None and tree is not None

    fact = source_oracle["fact"]
    if fact["kind"] != "xlsx_cell_range":
        evidence["source_fact_kind_mismatch_count"] = 1
        return None, {
            "status": "FAIL",
            "reason_code": "compound_table_row_source_scope_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    locator = fact["locator"]
    target_bounds = _parse_a1_range(str(locator["cell_range"]))
    table_candidates = _xlsx_table_nodes_containing_range(
        source_snapshot,
        locator=locator,
        target_bounds=target_bounds,
    )
    scoped_cells: list[Any] = []
    row_indexes: set[int] = set()
    table_source_node = None
    table = None
    if len(table_candidates) == 1:
        table_source_node, table = table_candidates[0]
        for cell in table.cells:
            native = cell.native_locator
            if (
                native.format.value == "xlsx"
                and native.workbook == locator["workbook"]
                and native.sheet == locator["sheet"]
                and native.cell_range is not None
                and _bounds_contain(
                    target_bounds,
                    _parse_a1_range(native.cell_range),
                )
            ):
                scoped_cells.append(cell)
                row_indexes.add(cell.row)

    nodes_by_identity = {node.identity_key: node for node in tree.nodes}
    table_structure = (
        nodes_by_identity.get(f"source:{table_source_node.source_node_id}")
        if table_source_node is not None
        else None
    )
    target_row = None
    row_candidates: list[Any] = []
    row_cells: list[Any] = []
    if table is not None and len(row_indexes) == 1:
        row_index = next(iter(row_indexes))
        row_candidates = _scoped_table_row_candidates(
            tree,
            table_structure_node=table_structure,
            row_index=row_index,
        )
        if len(row_candidates) == 1:
            target_row = row_candidates[0]
        row_cells = [
            cell
            for cell in table.cells
            if cell.row <= row_index < cell.row + cell.row_span
        ]

    scoped_cells_by_range: dict[str, list[Any]] = {}
    for cell in scoped_cells:
        cell_range = str(cell.native_locator.cell_range or "")
        scoped_cells_by_range.setdefault(cell_range, []).append(cell)
    expected_cells_by_range: dict[str, list[dict[str, Any]]] = {}
    for record in fact["cells"]:
        expected_cells_by_range.setdefault(
            str(record["cell_range"]),
            [],
        ).append(record)

    expected_scoped_cell_ids = {cell.cell_id for cell in scoped_cells}
    row_cell_ids = {cell.cell_id for cell in row_cells}
    row_cell_ref_ids = (
        {
            ref.source_id
            for ref in target_row.source_refs
            if ref.kind is StructureSourceRefKind.TABLE_CELL
        }
        if target_row is not None
        else set()
    )
    missing_scoped_cell_ref_count = len(
        expected_scoped_cell_ids - row_cell_ref_ids
    )
    cross_row_cell_ref_count = len(row_cell_ref_ids - row_cell_ids)
    invalid_boundary_count = int(
        table_structure is None
        or table_structure.kind is not StructureNodeKind.TABLE
        or target_row is None
        or target_row.boundary_basis is not StructureBoundaryBasis.TABLE_ROW
        or (
            table_source_node is not None
            and len(row_indexes) == 1
            and target_row is not None
            and target_row.identity_key
            != (
                f"table:{table_source_node.source_node_id}:"
                f"row:{next(iter(row_indexes))}"
            )
        )
    )
    evidence.update(
        {
            "expected_scoped_cell_count": int(fact["expected_cell_count"]),
            "observed_scoped_cell_count": len(scoped_cells),
            "observed_table_candidate_count": len(table_candidates),
            "observed_row_index_count": len(row_indexes),
            "observed_row_boundary_count": len(row_candidates),
            "observed_row_cell_ref_count": len(row_cell_ref_ids),
            "missing_scoped_cell_ref_count": missing_scoped_cell_ref_count,
            "cross_row_cell_ref_count": cross_row_cell_ref_count,
            "invalid_boundary_count": invalid_boundary_count,
            "classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
            "classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        }
    )
    if (
        len(table_candidates) != 1
        or len(scoped_cells) != int(fact["expected_cell_count"])
        or len(scoped_cells_by_range) != int(fact["expected_cell_count"])
        or len(row_indexes) != 1
        or len(row_candidates) != 1
        or target_row is None
        or missing_scoped_cell_ref_count
        or cross_row_cell_ref_count
        or invalid_boundary_count
    ):
        return None, {
            "status": "FAIL",
            "reason_code": "compound_table_row_structure_scope_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    semantic_key = (
        tree.structure_snapshot_id,
        DEFAULT_SEMANTIC_CLASSIFIER_ID,
        DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    )
    try:
        semantics = semantic_snapshots.get(semantic_key)
        if semantics is None:
            semantics = classify_content(
                source_snapshot,
                tree,
                classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            )
        validate_semantic_classification_against_inputs(
            semantics,
            source_snapshot,
            tree,
        )
        facets = classify_table_row_information(
            source_snapshot,
            tree,
            semantics,
            owner_structure_node_id=target_row.structure_node_id,
        )
    except SemanticClassificationRejected as exc:
        evidence["semantic_rejection_code"] = exc.code.value
        return None, {
            "status": "FAIL",
            "reason_code": "compound_table_row_semantic_classification_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["semantic_error_type"] = type(exc).__name__
        return None, {
            "status": "FAIL",
            "reason_code": "compound_table_row_semantic_classification_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    semantic_snapshots[semantic_key] = semantics

    row_annotations = [
        annotation
        for annotation in semantics.annotations
        if annotation.owner_structure_node_id == target_row.structure_node_id
    ]
    active_row_requirements = [
        annotation
        for annotation in row_annotations
        if annotation.semantic_class is SemanticClass.REQUIREMENT
        and annotation.requirement_subtype is RequirementSubtype.PRODUCT_REQUIREMENT
        and annotation.status
        in {
            SemanticAnnotationStatus.CANDIDATE,
            SemanticAnnotationStatus.CONFIRMED,
        }
        and "compound_test_requirement_row" in annotation.reason_codes
    ]
    exact_row_requirement_count = int(
        len(row_annotations) == 1 and len(active_row_requirements) == 1
    )

    duplicate_facet_id_count = len(facets) - len(
        {facet.information_facet_id for facet in facets}
    )
    invalid_facet_owner_count = sum(
        facet.semantic_snapshot_id != semantics.semantic_snapshot_id
        or facet.owner_structure_node_id != target_row.structure_node_id
        or facet.owner_semantic_unit_id
        != active_row_requirements[0].semantic_unit_id
        for facet in facets
    ) if len(active_row_requirements) == 1 else len(facets)
    invalid_facet_source_binding_count = 0
    for facet in facets:
        if facet.source_ref.kind is not StructureSourceRefKind.TABLE_CELL:
            invalid_facet_source_binding_count += 1
            continue
        source_cell_candidates = [
            cell
            for cell in scoped_cells
            if cell.cell_id == facet.source_ref.source_id
        ]
        if len(source_cell_candidates) != 1:
            invalid_facet_source_binding_count += 1
            continue
        source_cell = source_cell_candidates[0]
        cell_range = str(source_cell.native_locator.cell_range or "")
        expected_records = expected_cells_by_range.get(cell_range, [])
        if (
            len(expected_records) != 1
            or _sha256_text(source_cell.raw_text)
            != str(expected_records[0]["raw_sha256"])
            or facet.evidence_text_sha256
            != str(expected_records[0]["raw_sha256"])
        ):
            invalid_facet_source_binding_count += 1

    information_kind_counts = Counter(
        facet.information_kind.value for facet in facets
    )
    evidence.update(
        {
            "row_semantic_annotation_count": len(row_annotations),
            "active_compound_product_requirement_count": len(
                active_row_requirements
            ),
            "row_with_exact_compound_requirement_count": (
                exact_row_requirement_count
            ),
            "information_facet_count": len(facets),
            "information_facet_kind_counts": dict(
                sorted(information_kind_counts.items())
            ),
            "duplicate_information_facet_id_count": duplicate_facet_id_count,
            "invalid_information_facet_owner_count": invalid_facet_owner_count,
            "invalid_information_facet_source_binding_count": (
                invalid_facet_source_binding_count
            ),
        }
    )
    if (
        exact_row_requirement_count != 1
        or duplicate_facet_id_count
        or invalid_facet_owner_count
        or invalid_facet_source_binding_count
    ):
        return None, {
            "status": "FAIL",
            "reason_code": "compound_table_row_semantic_scope_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    return (
        {
            "source_snapshot": source_snapshot,
            "tree": tree,
            "semantics": semantics,
            "target_row": target_row,
            "row_cell_ids": row_cell_ids,
            "scoped_cells": scoped_cells,
            "scoped_cells_by_range": scoped_cells_by_range,
            "expected_cells_by_range": expected_cells_by_range,
            "facets": facets,
            "evidence": evidence,
        },
        None,
    )


def _evaluate_compound_table_row_information_facet(
    *,
    evaluator_id: str,
    expected_cell_range: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    expected_kind_by_evaluator = {
        TABLE_ROW_SCHEDULE_INFORMATION_EVALUATOR_ID: (
            SemanticInformationKind.SCHEDULE_INFORMATION
        ),
        TABLE_ROW_SAMPLE_PHASE_INFORMATION_EVALUATOR_ID: (
            SemanticInformationKind.SAMPLE_AND_PHASE_INFORMATION
        ),
    }
    expected_kind = expected_kind_by_evaluator.get(evaluator_id)
    if expected_kind is None:
        return {
            "status": "FAIL",
            "reason_code": "compound_table_row_information_evaluator_invalid",
            "evaluator": evaluator_id,
            "evidence": {},
        }

    context, failure = _compound_table_row_semantic_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    evidence = context["evidence"]

    expected_records = context["expected_cells_by_range"].get(
        expected_cell_range,
        [],
    )
    observed_cells = context["scoped_cells_by_range"].get(
        expected_cell_range,
        [],
    )
    target_facets = [
        facet
        for facet in context["facets"]
        if facet.information_kind is expected_kind
    ]
    expected_raw_sha256 = (
        str(expected_records[0]["raw_sha256"])
        if len(expected_records) == 1
        else None
    )
    observed_raw_sha256 = (
        _sha256_text(observed_cells[0].raw_text)
        if len(observed_cells) == 1
        else None
    )
    exact_cell_binding_count = sum(
        len(observed_cells) == 1
        and facet.source_ref.kind is StructureSourceRefKind.TABLE_CELL
        and facet.source_ref.source_id == observed_cells[0].cell_id
        and facet.evidence_text_sha256 == expected_raw_sha256
        for facet in target_facets
    )
    evidence.update(
        {
            "expected_information_kind": expected_kind.value,
            "expected_cell_range": expected_cell_range,
            "expected_cell_record_count": len(expected_records),
            "observed_cell_record_count": len(observed_cells),
            "expected_cell_raw_sha256": expected_raw_sha256,
            "observed_cell_raw_sha256": observed_raw_sha256,
            "observed_expected_information_facet_count": len(target_facets),
            "exact_information_facet_cell_binding_count": (
                exact_cell_binding_count
            ),
        }
    )
    matches = bool(
        len(expected_records) == 1
        and len(observed_cells) == 1
        and expected_raw_sha256 == observed_raw_sha256
        and len(target_facets) == 1
        and exact_cell_binding_count == 1
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "compound_table_row_information_facet_observed"
            if matches
            else "compound_table_row_information_facet_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_single_compound_table_row_projection(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = SINGLE_COMPOUND_TABLE_ROW_PROJECTION_EVALUATOR_ID
    context, failure = _compound_table_row_semantic_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    evidence = context["evidence"]
    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    semantics = context["semantics"]
    target_row = context["target_row"]
    facets = context["facets"]

    expected_facet_kinds = {
        SemanticInformationKind.SCHEDULE_INFORMATION,
        SemanticInformationKind.SAMPLE_AND_PHASE_INFORMATION,
    }
    observed_facet_kinds = {facet.information_kind for facet in facets}
    exact_compound_facet_set = bool(
        len(facets) == len(expected_facet_kinds)
        and observed_facet_kinds == expected_facet_kinds
    )
    evidence.update(
        {
            "expected_information_facet_count": len(expected_facet_kinds),
            "exact_compound_information_facet_set": exact_compound_facet_set,
            "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        }
    )

    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return {
            "status": "FAIL",
            "reason_code": "compound_table_row_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "compound_table_row_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view

    oracle_cell_ids = {
        cell.cell_id for cell in context["scoped_cells"]
    }
    facet_cell_ids = {
        facet.source_ref.source_id
        for facet in facets
        if facet.source_ref.kind is StructureSourceRefKind.TABLE_CELL
    }
    scoped_view_nodes = [
        node
        for node in view.nodes
        if node.owner_structure_node_id == target_row.structure_node_id
        or any(
            ref.kind is StructureSourceRefKind.TABLE_CELL
            and ref.source_id in oracle_cell_ids
            for ref in node.source_refs
        )
    ]
    row_owned_nodes = [
        node
        for node in scoped_view_nodes
        if node.owner_structure_node_id == target_row.structure_node_id
    ]
    valid_primary_nodes = [
        node
        for node in row_owned_nodes
        if node.role is RequirementProjectionRole.PRIMARY
        and node.decision_mode is RequirementDecisionMode.DIRECT
        and node.parent_requirement_view_node_id is None
        and node.semantic_class is SemanticClass.REQUIREMENT
        and node.requirement_subtype is RequirementSubtype.PRODUCT_REQUIREMENT
    ]
    scoped_top_level_nodes = [
        node
        for node in scoped_view_nodes
        if node.parent_requirement_view_node_id is None
    ]
    facet_scoped_top_level_nodes = [
        node
        for node in scoped_top_level_nodes
        if any(
            ref.kind is StructureSourceRefKind.TABLE_CELL
            and ref.source_id in facet_cell_ids
            for ref in node.source_refs
        )
    ]
    row_source_refs = set(target_row.source_refs)
    projection_source_refs = (
        set(valid_primary_nodes[0].source_refs)
        if len(valid_primary_nodes) == 1
        else set()
    )
    missing_row_source_ref_count = len(
        row_source_refs - projection_source_refs
    )
    extra_row_source_ref_count = len(
        projection_source_refs - row_source_refs
    )
    unowned_scoped_projection_count = sum(
        node.owner_structure_node_id != target_row.structure_node_id
        for node in scoped_view_nodes
    )
    extra_facet_top_level_projection_count = max(
        0,
        len(facet_scoped_top_level_nodes) - 1,
    )
    evidence.update(
        {
            "expected_scoped_top_level_requirement_count": 1,
            "observed_scoped_projection_count": len(scoped_view_nodes),
            "observed_row_owned_projection_count": len(row_owned_nodes),
            "observed_primary_direct_product_requirement_count": len(
                valid_primary_nodes
            ),
            "observed_scoped_top_level_projection_count": len(
                scoped_top_level_nodes
            ),
            "observed_facet_scoped_top_level_projection_count": len(
                facet_scoped_top_level_nodes
            ),
            "extra_facet_top_level_projection_count": (
                extra_facet_top_level_projection_count
            ),
            "unowned_scoped_projection_count": unowned_scoped_projection_count,
            "expected_row_source_ref_count": len(row_source_refs),
            "observed_projection_source_ref_count": len(
                projection_source_refs
            ),
            "missing_row_source_ref_count": missing_row_source_ref_count,
            "extra_row_source_ref_count": extra_row_source_ref_count,
        }
    )
    matches = bool(
        exact_compound_facet_set
        and len(scoped_view_nodes) == 1
        and len(row_owned_nodes) == 1
        and len(valid_primary_nodes) == 1
        and len(scoped_top_level_nodes) == 1
        and len(facet_scoped_top_level_nodes) == 1
        and extra_facet_top_level_projection_count == 0
        and unowned_scoped_projection_count == 0
        and missing_row_source_ref_count == 0
        and extra_row_source_ref_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "single_compound_table_row_requirement_projected"
            if matches
            else "single_compound_table_row_projection_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_table_data_row_boundaries(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = STRUCTURE_DATA_ROWS_EVALUATOR_ID
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert source_snapshot is not None and tree is not None

    fact = source_oracle["fact"]
    tables_by_locator: dict[tuple[int, int], list[tuple[Any, Any]]] = {}
    for node, table in _source_ir_tables(source_snapshot):
        native = node.native_locator
        if (
            native.format.value == "docx"
            and native.body_element_index is not None
            and native.table_index is not None
        ):
            tables_by_locator.setdefault(
                (native.body_element_index, native.table_index),
                [],
            ).append((node, table))

    nodes_by_identity = {node.identity_key: node for node in tree.nodes}
    observed_table_count = 0
    observed_data_rows = 0
    missing_cell_grounding_count = 0
    invalid_boundary_count = 0
    for expected_table in fact["tables"]:
        locator = expected_table["locator"]
        key = (int(locator["body_element_index"]), int(locator["table_index"]))
        candidates = tables_by_locator.get(key, [])
        if len(candidates) != 1:
            invalid_boundary_count += 1
            continue
        observed_table_count += 1
        table_source_node, table = candidates[0]
        table_structure = nodes_by_identity.get(
            f"source:{table_source_node.source_node_id}"
        )
        for row_index in range(1, table.row_count):
            expected_identity_key = (
                f"table:{table_source_node.source_node_id}:row:{row_index}"
            )
            row_candidates = _scoped_table_row_candidates(
                tree,
                table_structure_node=table_structure,
                row_index=row_index,
            )
            observed_data_rows += len(row_candidates)
            invalid_boundary_count += abs(len(row_candidates) - 1)
            expected_cell_ids = {
                cell.cell_id
                for cell in table.cells
                if cell.row <= row_index < cell.row + cell.row_span
            }
            for row_node in row_candidates:
                observed_cell_ids = {
                    ref.source_id
                    for ref in row_node.source_refs
                    if ref.kind is StructureSourceRefKind.TABLE_CELL
                }
                missing_cell_grounding_count += len(
                    expected_cell_ids - observed_cell_ids
                )
                if (
                    row_node.identity_key != expected_identity_key
                    or row_node.boundary_basis is not StructureBoundaryBasis.TABLE_ROW
                ):
                    invalid_boundary_count += 1

    evidence.update(
        {
            "expected_table_count": int(fact["expected_table_count"]),
            "observed_table_count": observed_table_count,
            "expected_data_row_boundary_count": int(fact["expected_data_row_count"]),
            "observed_data_row_boundary_count": observed_data_rows,
            "missing_cell_grounding_count": missing_cell_grounding_count,
            "invalid_boundary_count": invalid_boundary_count,
        }
    )
    matches = bool(
        observed_table_count == int(fact["expected_table_count"])
        and observed_data_rows == int(fact["expected_data_row_count"])
        and missing_cell_grounding_count == 0
        and invalid_boundary_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "table_data_row_boundaries_observed"
            if matches
            else "table_data_row_boundary_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _performance_parameter_row_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return None, failure
    assert source_snapshot is not None and tree is not None

    fact = source_oracle["fact"]
    if fact["kind"] != "docx_table_group":
        evidence["source_fact_kind_mismatch_count"] = 1
        return None, {
            "status": "FAIL",
            "reason_code": "performance_parameter_source_scope_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    tables_by_locator: dict[tuple[int, int], list[tuple[Any, Any]]] = {}
    for source_node, table in _source_ir_tables(source_snapshot):
        native = source_node.native_locator
        if (
            native.format.value == "docx"
            and native.body_element_index is not None
            and native.table_index is not None
        ):
            tables_by_locator.setdefault(
                (native.body_element_index, native.table_index),
                [],
            ).append((source_node, table))

    nodes_by_identity = {node.identity_key: node for node in tree.nodes}
    target_rows: list[tuple[Any, frozenset[str]]] = []
    observed_table_count = 0
    observed_row_boundary_count = 0
    invalid_boundary_count = 0
    missing_cell_grounding_count = 0
    extra_cell_grounding_count = 0
    for expected_table in fact["tables"]:
        locator = expected_table["locator"]
        key = (int(locator["body_element_index"]), int(locator["table_index"]))
        candidates = tables_by_locator.get(key, [])
        if len(candidates) != 1:
            invalid_boundary_count += abs(len(candidates) - 1) or 1
            continue
        observed_table_count += 1
        table_source_node, table = candidates[0]
        table_structure = nodes_by_identity.get(
            f"source:{table_source_node.source_node_id}"
        )
        if (
            table_structure is None
            or table_structure.kind is not StructureNodeKind.TABLE
        ):
            invalid_boundary_count += 1
            continue
        for row_index in range(1, table.row_count):
            row_candidates = _scoped_table_row_candidates(
                tree,
                table_structure_node=table_structure,
                row_index=row_index,
            )
            observed_row_boundary_count += len(row_candidates)
            invalid_boundary_count += abs(len(row_candidates) - 1)
            if len(row_candidates) != 1:
                continue
            row_node = row_candidates[0]
            expected_identity_key = (
                f"table:{table_source_node.source_node_id}:row:{row_index}"
            )
            expected_cell_ids = frozenset(
                cell.cell_id
                for cell in table.cells
                if cell.row <= row_index < cell.row + cell.row_span
            )
            observed_cell_ids = {
                ref.source_id
                for ref in row_node.source_refs
                if ref.kind is StructureSourceRefKind.TABLE_CELL
            }
            missing_cell_grounding_count += len(
                expected_cell_ids - observed_cell_ids
            )
            extra_cell_grounding_count += len(
                observed_cell_ids - expected_cell_ids
            )
            if (
                row_node.identity_key != expected_identity_key
                or row_node.boundary_basis is not StructureBoundaryBasis.TABLE_ROW
            ):
                invalid_boundary_count += 1
            target_rows.append((row_node, expected_cell_ids))

    expected_table_count = int(fact["expected_table_count"])
    expected_data_row_count = int(fact["expected_data_row_count"])
    evidence.update(
        {
            "expected_table_count": expected_table_count,
            "observed_table_count": observed_table_count,
            "expected_performance_parameter_row_count": expected_data_row_count,
            "observed_performance_parameter_row_boundary_count": (
                observed_row_boundary_count
            ),
            "target_row_count": len(target_rows),
            "missing_cell_grounding_count": missing_cell_grounding_count,
            "extra_cell_grounding_count": extra_cell_grounding_count,
            "invalid_boundary_count": invalid_boundary_count,
            "classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
            "classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        }
    )
    if (
        observed_table_count != expected_table_count
        or observed_row_boundary_count != expected_data_row_count
        or len(target_rows) != expected_data_row_count
        or missing_cell_grounding_count
        or extra_cell_grounding_count
        or invalid_boundary_count
    ):
        return None, {
            "status": "FAIL",
            "reason_code": "performance_parameter_row_scope_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    semantic_key = (
        tree.structure_snapshot_id,
        DEFAULT_SEMANTIC_CLASSIFIER_ID,
        DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    )
    try:
        semantics = semantic_snapshots.get(semantic_key)
        if semantics is None:
            semantics = classify_content(
                source_snapshot,
                tree,
                classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            )
        validate_semantic_classification_against_inputs(
            semantics,
            source_snapshot,
            tree,
        )
    except SemanticClassificationRejected as exc:
        evidence["semantic_rejection_code"] = exc.code.value
        return None, {
            "status": "FAIL",
            "reason_code": "semantic_classification_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["semantic_error_type"] = type(exc).__name__
        return None, {
            "status": "FAIL",
            "reason_code": "semantic_classification_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    semantic_snapshots[semantic_key] = semantics

    annotations_by_owner: dict[str, list[Any]] = {}
    for annotation in semantics.annotations:
        annotations_by_owner.setdefault(
            annotation.owner_structure_node_id,
            [],
        ).append(annotation)
    exact_semantic_row_count = 0
    active_performance_parameter_requirement_count = 0
    missing_active_requirement_row_count = 0
    duplicate_semantic_annotation_count = 0
    invalid_semantic_annotation_count = 0
    for row_node, _ in target_rows:
        annotations = annotations_by_owner.get(row_node.structure_node_id, [])
        active_requirements = [
            annotation
            for annotation in annotations
            if annotation.semantic_class is SemanticClass.REQUIREMENT
            and annotation.requirement_subtype
            is RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT
            and annotation.status
            in {
                SemanticAnnotationStatus.CANDIDATE,
                SemanticAnnotationStatus.CONFIRMED,
            }
        ]
        active_performance_parameter_requirement_count += len(
            active_requirements
        )
        missing_active_requirement_row_count += int(
            len(active_requirements) != 1
        )
        duplicate_semantic_annotation_count += max(0, len(annotations) - 1)
        invalid_semantic_annotation_count += len(annotations) - len(
            active_requirements
        )
        exact_semantic_row_count += int(
            len(annotations) == 1 and len(active_requirements) == 1
        )
    semantic_mismatch_count = (
        expected_data_row_count - exact_semantic_row_count
    )
    evidence.update(
        {
            "scoped_semantic_annotation_count": sum(
                len(annotations_by_owner.get(row.structure_node_id, []))
                for row, _ in target_rows
            ),
            "active_performance_parameter_requirement_count": (
                active_performance_parameter_requirement_count
            ),
            "row_with_exact_semantic_requirement_count": exact_semantic_row_count,
            "missing_active_requirement_row_count": (
                missing_active_requirement_row_count
            ),
            "duplicate_semantic_annotation_count": (
                duplicate_semantic_annotation_count
            ),
            "invalid_semantic_annotation_count": invalid_semantic_annotation_count,
            "semantic_row_mismatch_count": semantic_mismatch_count,
        }
    )
    return (
        {
            "source_snapshot": source_snapshot,
            "tree": tree,
            "semantics": semantics,
            "target_rows": target_rows,
            "semantic_mismatch_count": semantic_mismatch_count,
            "evidence": evidence,
        },
        None,
    )


def _evaluate_performance_parameter_rows(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    context, failure = _performance_parameter_row_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    evidence = context["evidence"]
    if evaluator_id == PERFORMANCE_PARAMETER_ROWS_SEMANTIC_EVALUATOR_ID:
        matches = context["semantic_mismatch_count"] == 0
        return {
            "status": "PASS" if matches else "FAIL",
            "reason_code": (
                "performance_parameter_rows_classified"
                if matches
                else "performance_parameter_row_semantic_mismatch"
            ),
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    if evaluator_id != REQUIREMENT_PER_PARAMETER_ROW_PROJECTION_EVALUATOR_ID:
        return {
            "status": "FAIL",
            "reason_code": "performance_parameter_evaluator_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    semantics = context["semantics"]
    target_rows = context["target_rows"]
    evidence.update(
        {
            "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        }
    )
    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return {
            "status": "FAIL",
            "reason_code": "requirement_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "requirement_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view

    expected_row_count = len(target_rows)
    target_row_ids = {
        row.structure_node_id for row, _ in target_rows
    }
    target_cell_ids = {
        cell_id
        for _, row_cell_ids in target_rows
        for cell_id in row_cell_ids
    }
    scoped_view_nodes = [
        node
        for node in view.nodes
        if node.owner_structure_node_id in target_row_ids
        or any(
            ref.kind is StructureSourceRefKind.TABLE_CELL
            and ref.source_id in target_cell_ids
            for ref in node.source_refs
        )
    ]
    exact_projection_row_count = 0
    missing_projection_row_count = 0
    duplicate_projection_count = 0
    invalid_projection_count = 0
    cross_row_source_ref_count = 0
    missing_row_source_grounding_count = 0
    for row_node, row_cell_ids in target_rows:
        owner_nodes = [
            node
            for node in scoped_view_nodes
            if node.owner_structure_node_id == row_node.structure_node_id
        ]
        valid_nodes = [
            node
            for node in owner_nodes
            if node.role is RequirementProjectionRole.PRIMARY
            and node.decision_mode is RequirementDecisionMode.DIRECT
            and node.parent_requirement_view_node_id is None
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.requirement_subtype
            is RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT
        ]
        exact_projection_row_count += int(
            len(owner_nodes) == 1 and len(valid_nodes) == 1
        )
        missing_projection_row_count += int(len(valid_nodes) != 1)
        duplicate_projection_count += max(0, len(owner_nodes) - 1)
        invalid_projection_count += len(owner_nodes) - len(valid_nodes)
        for node in owner_nodes:
            exact_cell_refs = {
                ref.source_id
                for ref in node.source_refs
                if ref.kind is StructureSourceRefKind.TABLE_CELL
            }
            missing_row_source_grounding_count += len(
                row_cell_ids - exact_cell_refs
            )
            cross_row_source_ref_count += len(exact_cell_refs - row_cell_ids)
    unowned_scoped_projection_count = sum(
        node.owner_structure_node_id not in target_row_ids
        for node in scoped_view_nodes
    )
    evidence.update(
        {
            "expected_parameter_row_projection_count": expected_row_count,
            "observed_scoped_projection_count": len(scoped_view_nodes),
            "row_with_exact_primary_direct_projection_count": (
                exact_projection_row_count
            ),
            "missing_projection_row_count": missing_projection_row_count,
            "duplicate_projection_count": duplicate_projection_count,
            "invalid_projection_count": invalid_projection_count,
            "unowned_scoped_projection_count": unowned_scoped_projection_count,
            "cross_row_source_ref_count": cross_row_source_ref_count,
            "missing_row_source_grounding_count": (
                missing_row_source_grounding_count
            ),
        }
    )
    matches = bool(
        context["semantic_mismatch_count"] == 0
        and len(scoped_view_nodes) == expected_row_count
        and exact_projection_row_count == expected_row_count
        and missing_projection_row_count == 0
        and duplicate_projection_count == 0
        and invalid_projection_count == 0
        and unowned_scoped_projection_count == 0
        and cross_row_source_ref_count == 0
        and missing_row_source_grounding_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "requirement_per_parameter_row_observed"
            if matches
            else "requirement_per_parameter_row_mapping_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _docx_mixed_content_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    parameter_table_oracle: dict[str, Any],
    oracle_registry: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=parameter_table_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return None, failure
    assert source_snapshot is not None and tree is not None

    fidelity = _evaluate_oracle_fidelity(
        oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        oracle_registry=oracle_registry,
    )
    mixed_evidence = fidelity["evidence"]
    provenance = source_oracle["review_provenance"]
    evidence.update(
        {
            "source_oracle_id": str(source_oracle["oracle_id"]),
            "source_fact_kind": str(source_oracle["fact"]["kind"]),
            "mixed_scope_mismatch_count": int(
                mixed_evidence.get("mismatch_count", 1)
            ),
            "review_label": str(provenance["label"]),
            "reviewed_page_count": len(provenance["reviewed_page_numbers"]),
            "is_human_gold": bool(provenance["is_human_gold"]),
            "is_serving_activation_gate": bool(
                provenance["is_serving_activation_gate"]
            ),
        }
    )
    if fidelity["status"] != "PASS":
        return None, {
            "status": "FAIL",
            "reason_code": "docx_mixed_content_source_scope_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    nodes_by_locator: dict[tuple[int, str], list[Any]] = {}
    for node in source_snapshot.nodes:
        locator = node.native_locator
        if (
            locator.format.value != "docx"
            or locator.body_element_index is None
            or locator.table_index is not None
        ):
            continue
        nodes_by_locator.setdefault(
            (int(locator.body_element_index), node.kind.value),
            [],
        ).append(node)
    structure_by_identity = {node.identity_key: node for node in tree.nodes}
    target_narratives: list[tuple[Any, Any]] = []
    narrative_resolution_mismatch_count = 0
    for record in source_oracle["fact"]["narratives"]:
        key = (
            int(record["locator"]["body_element_index"]),
            str(record["kind"]),
        )
        source_candidates = nodes_by_locator.get(key, [])
        if len(source_candidates) != 1:
            narrative_resolution_mismatch_count += 1
            continue
        source_node = source_candidates[0]
        structure_node = structure_by_identity.get(
            f"source:{source_node.source_node_id}"
        )
        if structure_node is None:
            narrative_resolution_mismatch_count += 1
            continue
        target_narratives.append((source_node, structure_node))
    expected_narrative_count = int(
        source_oracle["fact"]["expected_narrative_count"]
    )
    evidence.update(
        {
            "expected_narrative_count": expected_narrative_count,
            "resolved_narrative_count": len(target_narratives),
            "narrative_resolution_mismatch_count": (
                narrative_resolution_mismatch_count
            ),
        }
    )
    if (
        len(target_narratives) != expected_narrative_count
        or narrative_resolution_mismatch_count
    ):
        return None, {
            "status": "FAIL",
            "reason_code": "docx_mixed_content_narrative_resolution_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    semantic_key = (
        tree.structure_snapshot_id,
        DEFAULT_SEMANTIC_CLASSIFIER_ID,
        DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    )
    try:
        semantics = semantic_snapshots.get(semantic_key)
        if semantics is None:
            semantics = classify_content(
                source_snapshot,
                tree,
                classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            )
        validate_semantic_classification_against_inputs(
            semantics,
            source_snapshot,
            tree,
        )
    except SemanticClassificationRejected as exc:
        evidence["semantic_rejection_code"] = exc.code.value
        return None, {
            "status": "FAIL",
            "reason_code": "semantic_classification_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["semantic_error_type"] = type(exc).__name__
        return None, {
            "status": "FAIL",
            "reason_code": "semantic_classification_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    semantic_snapshots[semantic_key] = semantics
    evidence.update(
        {
            "classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
            "classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
        }
    )
    return (
        {
            "source_snapshot": source_snapshot,
            "tree": tree,
            "semantics": semantics,
            "target_narratives": target_narratives,
            "evidence": evidence,
        },
        None,
    )


def _evaluate_docx_mixed_content_semantics(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    parameter_table_oracle: dict[str, Any],
    oracle_registry: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    context, failure = _docx_mixed_content_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        parameter_table_oracle=parameter_table_oracle,
        oracle_registry=oracle_registry,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    evidence = context["evidence"]
    semantics = context["semantics"]
    annotations_by_owner: dict[str, list[Any]] = {}
    for annotation in semantics.annotations:
        annotations_by_owner.setdefault(
            annotation.owner_structure_node_id,
            [],
        ).append(annotation)

    if evaluator_id == TECHNICAL_NARRATIVE_DISPOSITION_EVALUATOR_ID:
        valid_classes = {
            SemanticClass.BACKGROUND,
            SemanticClass.TECHNICAL_INFORMATION,
        }
        valid_narrative_count = 0
        duplicate_annotation_count = 0
        invalid_narrative_count = 0
        observed_class_counts: dict[str, int] = {}
        for _, structure_node in context["target_narratives"]:
            annotations = annotations_by_owner.get(
                structure_node.structure_node_id,
                [],
            )
            valid = [
                annotation
                for annotation in annotations
                if annotation.semantic_class in valid_classes
                and annotation.requirement_subtype is None
                and annotation.status
                in {
                    SemanticAnnotationStatus.CANDIDATE,
                    SemanticAnnotationStatus.CONFIRMED,
                }
            ]
            valid_narrative_count += int(
                len(annotations) == 1 and len(valid) == 1
            )
            duplicate_annotation_count += max(0, len(annotations) - 1)
            invalid_narrative_count += int(
                len(annotations) != 1 or len(valid) != 1
            )
            for annotation in annotations:
                observed_class_counts[annotation.semantic_class.value] = (
                    observed_class_counts.get(annotation.semantic_class.value, 0)
                    + 1
                )
        expected_narrative_count = len(context["target_narratives"])
        evidence.update(
            {
                "valid_technical_narrative_count": valid_narrative_count,
                "invalid_technical_narrative_count": invalid_narrative_count,
                "duplicate_narrative_annotation_count": (
                    duplicate_annotation_count
                ),
                "observed_narrative_class_counts": dict(
                    sorted(observed_class_counts.items())
                ),
            }
        )
        matches = bool(
            valid_narrative_count == expected_narrative_count
            and invalid_narrative_count == 0
            and duplicate_annotation_count == 0
        )
        return {
            "status": "PASS" if matches else "FAIL",
            "reason_code": (
                "technical_narrative_disposition_observed"
                if matches
                else "technical_narrative_disposition_mismatch"
            ),
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    if evaluator_id != SOURCE_ROLE_NOT_SEMANTIC_EXCLUSION_EVALUATOR_ID:
        return {
            "status": "FAIL",
            "reason_code": "docx_mixed_content_evaluator_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    parameter_context, parameter_failure = _performance_parameter_row_context(
        evaluator_id=evaluator_id,
        source_oracle=parameter_table_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
    )
    if parameter_failure is not None:
        return parameter_failure
    assert parameter_context is not None
    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    target_rows = parameter_context["target_rows"]
    target_row_ids = {
        row.structure_node_id for row, _ in target_rows
    }
    target_annotations = [
        annotation
        for row_id in target_row_ids
        for annotation in annotations_by_owner.get(row_id, [])
    ]
    active_requirement_owner_ids = {
        annotation.owner_structure_node_id
        for annotation in target_annotations
        if annotation.semantic_class is SemanticClass.REQUIREMENT
        and annotation.status
        in {
            SemanticAnnotationStatus.CANDIDATE,
            SemanticAnnotationStatus.CONFIRMED,
        }
    }
    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return {
            "status": "FAIL",
            "reason_code": "requirement_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "requirement_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view
    projected_requirement_owner_ids = {
        node.owner_structure_node_id
        for node in view.nodes
        if node.owner_structure_node_id in target_row_ids
        and node.semantic_class is SemanticClass.REQUIREMENT
        and node.decision_mode is RequirementDecisionMode.DIRECT
    }
    document_id = str(source_oracle["document_id"])
    document_result = document_results.get(document_id) or {}
    full_scope_annotation_count = len(semantics.annotations)
    expected_semantic_scope_node_count = sum(
        node.kind is not StructureNodeKind.DOCUMENT for node in tree.nodes
    )
    target_row_annotation_owner_count = len(
        {
            annotation.owner_structure_node_id
            for annotation in target_annotations
        }
    )
    role_projection_mismatch_count = len(
        active_requirement_owner_ids ^ projected_requirement_owner_ids
    )
    evidence.update(
        {
            "document_role": str(document_result.get("document_role") or ""),
            "ingress_route": str(document_result.get("ingress_route") or ""),
            "semantic_scope_owner_count": full_scope_annotation_count,
            "expected_semantic_scope_node_count": (
                expected_semantic_scope_node_count
            ),
            "expected_parameter_row_count": len(target_rows),
            "parameter_row_annotation_owner_count": (
                target_row_annotation_owner_count
            ),
            "active_parameter_requirement_owner_count": len(
                active_requirement_owner_ids
            ),
            "projected_parameter_requirement_owner_count": len(
                projected_requirement_owner_ids
            ),
            "role_projection_mismatch_count": role_projection_mismatch_count,
            "projection_policy": view.projection_policy.value,
            "non_requirement_artifact_policy_count": int(
                view.projection_policy
                is RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT
            ),
        }
    )
    matches = bool(
        document_result.get("document_role") == "internal_product_information"
        and document_result.get("ingress_route") == "knowledge_document"
        and full_scope_annotation_count == expected_semantic_scope_node_count
        and target_row_annotation_owner_count == len(target_rows)
        and view.projection_policy is RequirementProjectionPolicy.SHADOW_CANDIDATES
        and role_projection_mismatch_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "source_role_does_not_exclude_semantics"
            if matches
            else "source_role_semantic_exclusion_detected"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_merged_cell_applicability(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = STRUCTURE_MERGED_APPLICABILITY_EVALUATOR_ID
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert source_snapshot is not None and tree is not None

    fact = source_oracle["fact"]
    locator = fact["table_locator"]
    table_candidates = [
        (node, table)
        for node, table in _source_ir_tables(source_snapshot)
        if (
            node.native_locator.format.value == "xlsx"
            and node.native_locator.workbook == locator["workbook"]
            and node.native_locator.sheet == locator["sheet"]
            and node.native_locator.cell_range == locator["cell_range"]
        )
    ]
    nodes_by_identity = {node.identity_key: node for node in tree.nodes}
    applies_to_by_source: dict[str, list[Any]] = {}
    for relation in tree.relations:
        if relation.kind is StructureRelationKind.APPLIES_TO:
            applies_to_by_source.setdefault(
                relation.source_structure_node_id,
                [],
            ).append(relation)

    observed_shared_nodes = 0
    observed_relations = 0
    invalid_relation_count = 0
    anchor_ref_ids: list[str] = []
    if len(table_candidates) == 1:
        table_source_node, table = table_candidates[0]
        table_bounds = _parse_a1_range(str(locator["cell_range"]))
        scope_bounds = _parse_a1_range(str(fact["scope_range"]))
        covered_row_indexes = tuple(
            range(
                scope_bounds[0] - table_bounds[0],
                scope_bounds[2] - table_bounds[0] + 1,
            )
        )
        expected_row_ids = {
            nodes_by_identity[
                f"table:{table_source_node.source_node_id}:row:{row_index}"
            ].structure_node_id
            for row_index in covered_row_indexes
            if f"table:{table_source_node.source_node_id}:row:{row_index}"
            in nodes_by_identity
        }
        cells_by_position = {(cell.row, cell.column): cell for cell in table.cells}
        regions_by_position = {
            (region.row, region.column, region.row_span, region.column_span): region
            for region in table.merged_regions
        }
        for expected_merge in fact["merges"]:
            anchor_bounds = _parse_a1_range(str(expected_merge["anchor"]))
            local_row = anchor_bounds[0] - table_bounds[0]
            local_column = anchor_bounds[1] - table_bounds[1]
            region_key = (
                local_row,
                local_column,
                int(expected_merge["row_span"]),
                int(expected_merge["column_span"]),
            )
            region = regions_by_position.get(region_key)
            anchor_cell = cells_by_position.get((local_row, local_column))
            merged_identity = (
                f"table:{table_source_node.source_node_id}:merged:"
                f"{local_row}:{local_column}:"
                f"{expected_merge['row_span']}:{expected_merge['column_span']}"
            )
            shared_node = nodes_by_identity.get(merged_identity)
            if region is None or anchor_cell is None or shared_node is None:
                invalid_relation_count += 1
                continue
            observed_shared_nodes += 1
            anchor_ref_ids.append(anchor_cell.cell_id)
            if (
                shared_node.kind is not StructureNodeKind.CONTENT
                or shared_node.boundary_basis is not StructureBoundaryBasis.MERGED_RANGE
                or len(shared_node.source_refs) != 1
                or shared_node.source_refs[0].kind
                is not StructureSourceRefKind.TABLE_CELL
                or shared_node.source_refs[0].source_id != anchor_cell.cell_id
            ):
                invalid_relation_count += 1
            relations = applies_to_by_source.get(
                shared_node.structure_node_id,
                [],
            )
            observed_relations += len(relations)
            if {
                relation.target_structure_node_id for relation in relations
            } != expected_row_ids or any(
                len(relation.source_refs) != 1
                or relation.source_refs[0].source_id != anchor_cell.cell_id
                for relation in relations
            ):
                invalid_relation_count += 1
    else:
        covered_row_indexes = ()

    expected_relation_count = int(fact["expected_merge_count"]) * int(
        fact["covered_row_count"]
    )
    anchor_ref_id_set = set(anchor_ref_ids)
    scoped_shared_nodes = [
        node
        for node in tree.nodes
        if node.boundary_basis is StructureBoundaryBasis.MERGED_RANGE
        and len(node.source_refs) == 1
        and node.source_refs[0].source_id in anchor_ref_id_set
    ]
    duplicate_merged_anchor_node_count = max(
        0,
        len(scoped_shared_nodes) - len(anchor_ref_id_set),
    )
    evidence.update(
        {
            "expected_shared_fact_node_count": int(fact["expected_merge_count"]),
            "observed_shared_fact_node_count": observed_shared_nodes,
            "unique_anchor_source_ref_count": len(anchor_ref_id_set),
            "covered_row_count": len(covered_row_indexes),
            "expected_applies_to_count": expected_relation_count,
            "observed_applies_to_count": observed_relations,
            "invalid_relation_count": invalid_relation_count,
            "duplicate_merged_anchor_node_count": (duplicate_merged_anchor_node_count),
        }
    )
    matches = bool(
        len(table_candidates) == 1
        and len(covered_row_indexes) == int(fact["covered_row_count"])
        and observed_shared_nodes == int(fact["expected_merge_count"])
        and len(anchor_ref_id_set) == int(fact["expected_merge_count"])
        and observed_relations == expected_relation_count
        and invalid_relation_count == 0
        and duplicate_merged_anchor_node_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "merged_cell_applicability_observed"
            if matches
            else "merged_cell_applicability_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _material_requirement_scope_records(
    fact: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if fact["kind"] == "xlsx_merge_group":
        return (
            dict(fact["table_locator"]),
            [
                {
                    "identity_cell_range": str(record["cell_range"]),
                    "identity_raw_sha256": str(record["raw_sha256"]),
                }
                for record in fact["row_identity"]["cells"]
            ],
        )
    if fact["kind"] == "xlsx_material_requirement_scope":
        return (
            dict(fact["table_locator"]),
            [
                {
                    "identity_cell_range": str(
                        record["identity_cell_range"]
                    ),
                    "identity_raw_sha256": str(
                        record["identity_raw_sha256"]
                    ),
                }
                for record in fact["row_expectations"]
            ],
        )
    raise ValueError("unsupported material-requirement source scope")


def _evaluate_material_requirement_projection(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert source_snapshot is not None and tree is not None

    fact = source_oracle["fact"]
    try:
        locator, row_expectations = _material_requirement_scope_records(
            fact
        )
    except Exception as exc:
        evidence["scope_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "material_requirement_source_scope_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    table_candidates = [
        (node, table)
        for node, table in _source_ir_tables(source_snapshot)
        if (
            node.native_locator.format.value == "xlsx"
            and node.native_locator.workbook == locator["workbook"]
            and node.native_locator.sheet == locator["sheet"]
            and node.native_locator.cell_range == locator["cell_range"]
        )
    ]
    nodes_by_identity = {node.identity_key: node for node in tree.nodes}
    target_rows: list[Any] = []
    identity_ref_by_row_id: dict[str, str] = {}
    invalid_row_boundary_count = 0
    missing_identity_ref_count = 0
    duplicate_identity_ref_count = 0
    if len(table_candidates) == 1:
        table_source_node, table = table_candidates[0]
        table_bounds = _parse_a1_range(str(locator["cell_range"]))
        table_structure = nodes_by_identity.get(
            f"source:{table_source_node.source_node_id}"
        )
        cells_by_range = {
            str(cell.native_locator.cell_range): cell
            for cell in table.cells
            if cell.native_locator.cell_range is not None
        }
        observed_identity_cell_ids: set[str] = set()
        for expected in row_expectations:
            cell_range = str(expected["identity_cell_range"])
            identity_cell = cells_by_range.get(cell_range)
            cell_bounds = _parse_a1_range(cell_range)
            row_index = cell_bounds[0] - table_bounds[0]
            row_candidates = _scoped_table_row_candidates(
                tree,
                table_structure_node=table_structure,
                row_index=row_index,
            )
            invalid_row_boundary_count += abs(len(row_candidates) - 1)
            if (
                identity_cell is None
                or len(row_candidates) != 1
                or _sha256_text(identity_cell.raw_text)
                != expected["identity_raw_sha256"]
            ):
                missing_identity_ref_count += 1
                continue
            row = row_candidates[0]
            identity_ref_count = sum(
                ref.kind is StructureSourceRefKind.TABLE_CELL
                and ref.source_id == identity_cell.cell_id
                for ref in row.source_refs
            )
            missing_identity_ref_count += int(identity_ref_count != 1)
            duplicate_identity_ref_count += int(
                identity_cell.cell_id in observed_identity_cell_ids
            )
            observed_identity_cell_ids.add(identity_cell.cell_id)
            if (
                row.identity_key
                != f"table:{table_source_node.source_node_id}:row:{row_index}"
                or row.kind is not StructureNodeKind.TABLE_ROW
                or row.boundary_basis is not StructureBoundaryBasis.TABLE_ROW
            ):
                invalid_row_boundary_count += 1
            target_rows.append(row)
            identity_ref_by_row_id[row.structure_node_id] = identity_cell.cell_id
    else:
        table_source_node = None
        table = None
        table_structure = None

    expected_requirement_count = len(row_expectations)
    evidence.update(
        {
            "expected_material_requirement_count": expected_requirement_count,
            "observed_material_row_boundary_count": len(target_rows),
            "invalid_material_row_boundary_count": invalid_row_boundary_count,
            "missing_material_identity_ref_count": missing_identity_ref_count,
            "duplicate_material_identity_ref_count": (
                duplicate_identity_ref_count
            ),
            "classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
            "classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        }
    )
    if (
        len(table_candidates) != 1
        or len(target_rows) != expected_requirement_count
        or invalid_row_boundary_count
        or missing_identity_ref_count
        or duplicate_identity_ref_count
    ):
        return {
            "status": "FAIL",
            "reason_code": "material_requirement_row_scope_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    semantic_key = (
        tree.structure_snapshot_id,
        DEFAULT_SEMANTIC_CLASSIFIER_ID,
        DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    )
    try:
        semantics = semantic_snapshots.get(semantic_key)
        if semantics is None:
            semantics = classify_content(
                source_snapshot,
                tree,
                classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            )
        validate_semantic_classification_against_inputs(
            semantics,
            source_snapshot,
            tree,
        )
    except SemanticClassificationRejected as exc:
        evidence["semantic_rejection_code"] = exc.code.value
        return {
            "status": "FAIL",
            "reason_code": "material_requirement_semantic_classification_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["semantic_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "material_requirement_semantic_classification_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    semantic_snapshots[semantic_key] = semantics

    target_row_ids = {row.structure_node_id for row in target_rows}
    annotations_by_owner: dict[str, list[Any]] = {}
    for annotation in semantics.annotations:
        if annotation.owner_structure_node_id in target_row_ids:
            annotations_by_owner.setdefault(
                annotation.owner_structure_node_id,
                [],
            ).append(annotation)
    valid_annotation_by_row_id: dict[str, Any] = {}
    invalid_annotation_count = 0
    for row in target_rows:
        annotations = annotations_by_owner.get(row.structure_node_id, [])
        valid = [
            annotation
            for annotation in annotations
            if annotation.semantic_class is SemanticClass.REQUIREMENT
            and annotation.requirement_subtype
            is RequirementSubtype.PRODUCT_REQUIREMENT
            and annotation.status
            in {
                SemanticAnnotationStatus.CANDIDATE,
                SemanticAnnotationStatus.CONFIRMED,
            }
            and annotation.reason_codes
            == ("material_requirement_table_row",)
        ]
        if len(annotations) == 1 and len(valid) == 1:
            valid_annotation_by_row_id[row.structure_node_id] = valid[0]
        else:
            invalid_annotation_count += max(1, len(annotations))
    evidence.update(
        {
            "scoped_material_annotation_count": sum(
                len(records) for records in annotations_by_owner.values()
            ),
            "valid_material_annotation_count": len(
                valid_annotation_by_row_id
            ),
            "invalid_material_annotation_count": invalid_annotation_count,
        }
    )
    if (
        len(valid_annotation_by_row_id) != expected_requirement_count
        or invalid_annotation_count
    ):
        return {
            "status": "FAIL",
            "reason_code": "material_requirement_semantic_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return {
            "status": "FAIL",
            "reason_code": "material_requirement_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "material_requirement_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view

    scoped_view_nodes = [
        node
        for node in view.nodes
        if node.owner_structure_node_id in target_row_ids
    ]
    view_nodes_by_owner: dict[str, list[Any]] = {}
    for node in scoped_view_nodes:
        view_nodes_by_owner.setdefault(
            node.owner_structure_node_id,
            [],
        ).append(node)
    valid_view_node_by_row_id: dict[str, Any] = {}
    projection_ref_mismatch_count = 0
    invalid_projection_count = 0
    for row in target_rows:
        candidates = view_nodes_by_owner.get(row.structure_node_id, [])
        valid_annotation = valid_annotation_by_row_id[row.structure_node_id]
        valid = [
            node
            for node in candidates
            if node.role is RequirementProjectionRole.PRIMARY
            and node.decision_mode is RequirementDecisionMode.DIRECT
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.requirement_subtype
            is RequirementSubtype.PRODUCT_REQUIREMENT
            and node.parent_requirement_view_node_id is None
            and node.semantic_annotation_ids
            == (valid_annotation.semantic_annotation_id,)
        ]
        if len(candidates) == 1 and len(valid) == 1:
            valid_view_node_by_row_id[row.structure_node_id] = valid[0]
            projection_ref_mismatch_count += int(
                set(valid[0].source_refs)
                != set(row.effective_semantic_source_refs)
            )
        else:
            invalid_projection_count += max(1, len(candidates))
    evidence.update(
        {
            "observed_material_requirement_count": len(scoped_view_nodes),
            "valid_primary_direct_material_requirement_count": len(
                valid_view_node_by_row_id
            ),
            "invalid_material_projection_count": invalid_projection_count,
            "material_projection_ref_mismatch_count": (
                projection_ref_mismatch_count
            ),
        }
    )
    base_matches = bool(
        len(scoped_view_nodes) == expected_requirement_count
        and len(valid_view_node_by_row_id) == expected_requirement_count
        and invalid_projection_count == 0
        and projection_ref_mismatch_count == 0
    )
    if evaluator_id == PROJECTION_MATERIAL_REQUIREMENT_COUNT_EVALUATOR_ID:
        return {
            "status": "PASS" if base_matches else "FAIL",
            "reason_code": (
                "material_requirement_count_observed"
                if base_matches
                else "material_requirement_count_mismatch"
            ),
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    if fact["kind"] != "xlsx_material_requirement_scope":
        evidence["source_fact_kind_mismatch_count"] = 1
        return {
            "status": "FAIL",
            "reason_code": "material_shared_values_source_scope_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    provenance = source_oracle["review_provenance"]
    evidence.update(
        {
            "review_evidence_label": str(provenance["label"]),
            "reviewer_role": str(provenance["reviewer_role"]),
            "reviewed_page_count": len(provenance["reviewed_page_numbers"]),
            "is_human_gold": bool(provenance["is_human_gold"]),
            "is_serving_activation_gate": bool(
                provenance["is_serving_activation_gate"]
            ),
        }
    )
    assert table_source_node is not None and table is not None
    assert table_structure is not None
    table_bounds = _parse_a1_range(str(locator["cell_range"]))
    cells_by_range = {
        str(cell.native_locator.cell_range): cell
        for cell in table.cells
        if cell.native_locator.cell_range is not None
    }
    relations_by_source: dict[str, list[Any]] = {}
    for relation in tree.relations:
        if relation.kind is StructureRelationKind.APPLIES_TO:
            relations_by_source.setdefault(
                relation.source_structure_node_id,
                [],
            ).append(relation)
    observed_role_applies_to_count = 0
    invalid_role_binding_count = 0
    missing_projection_role_relation_count = 0
    shared_role_structure_ids: set[str] = set()
    role_coverage_by_row_id = {
        row_id: set() for row_id in target_row_ids
    }
    for binding in fact["role_bindings"]:
        anchor_range = str(binding["anchor_cell_range"])
        merge_bounds = _parse_a1_range(str(binding["merge_range"]))
        anchor_bounds = _parse_a1_range(anchor_range)
        anchor_cell = cells_by_range.get(anchor_range)
        local_row = anchor_bounds[0] - table_bounds[0]
        local_column = anchor_bounds[1] - table_bounds[1]
        shared_identity = (
            f"table:{table_source_node.source_node_id}:merged:"
            f"{local_row}:{local_column}:"
            f"{merge_bounds[2] - merge_bounds[0] + 1}:"
            f"{merge_bounds[3] - merge_bounds[1] + 1}"
        )
        shared_node = nodes_by_identity.get(shared_identity)
        if anchor_cell is None or shared_node is None:
            invalid_role_binding_count += 1
            continue
        shared_role_structure_ids.add(shared_node.structure_node_id)
        relations = relations_by_source.get(
            shared_node.structure_node_id,
            [],
        )
        observed_role_applies_to_count += len(relations)
        relations_by_target = {
            relation.target_structure_node_id: relation
            for relation in relations
        }
        if (
            shared_node.kind is not StructureNodeKind.CONTENT
            or shared_node.boundary_basis
            is not StructureBoundaryBasis.MERGED_RANGE
            or shared_node.source_refs
            != (
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=anchor_cell.cell_id,
                ),
            )
            or set(relations_by_target) != target_row_ids
            or len(relations_by_target) != len(relations)
            or len(relations)
            != int(binding["expected_applies_to_count"])
        ):
            invalid_role_binding_count += 1
        for row_id in target_row_ids:
            relation = relations_by_target.get(row_id)
            view_node = valid_view_node_by_row_id.get(row_id)
            if (
                relation is None
                or relation.source_refs
                != (
                    StructureSourceRef(
                        kind=StructureSourceRefKind.TABLE_CELL,
                        source_id=anchor_cell.cell_id,
                    ),
                )
            ):
                invalid_role_binding_count += 1
                continue
            role_coverage_by_row_id[row_id].add(str(binding["role"]))
            if (
                view_node is None
                or relation.relation_id not in view_node.relation_ids
            ):
                missing_projection_role_relation_count += 1
    projected_shared_role_node_count = sum(
        node.owner_structure_node_id in shared_role_structure_ids
        for node in view.nodes
    )
    expected_role_set = {
        str(binding["role"]) for binding in fact["role_bindings"]
    }
    row_role_coverage_mismatch_count = sum(
        roles != expected_role_set
        for roles in role_coverage_by_row_id.values()
    )
    evidence.update(
        {
            "expected_shared_role_count": len(fact["role_bindings"]),
            "observed_shared_role_structure_count": len(
                shared_role_structure_ids
            ),
            "expected_role_applies_to_count": int(
                fact["expected_role_applies_to_count"]
            ),
            "observed_role_applies_to_count": (
                observed_role_applies_to_count
            ),
            "invalid_shared_role_binding_count": (
                invalid_role_binding_count
            ),
            "row_role_coverage_mismatch_count": (
                row_role_coverage_mismatch_count
            ),
            "missing_projection_role_relation_count": (
                missing_projection_role_relation_count
            ),
            "projected_shared_role_node_count": (
                projected_shared_role_node_count
            ),
        }
    )
    matches = bool(
        base_matches
        and len(shared_role_structure_ids) == len(fact["role_bindings"])
        and observed_role_applies_to_count
        == int(fact["expected_role_applies_to_count"])
        and invalid_role_binding_count == 0
        and row_role_coverage_mismatch_count == 0
        and missing_projection_role_relation_count == 0
        and projected_shared_role_node_count == 0
        and not evidence["is_human_gold"]
        and not evidence["is_serving_activation_gate"]
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "material_shared_values_projected"
            if matches
            else "material_shared_values_projection_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _explicit_peer_structure_context(
    *,
    evaluator_id: str,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return None, failure
    assert source_snapshot is not None and tree is not None

    fact = source_oracle["fact"]
    if fact["kind"] != "xlsx_explicit_peer_scope_group":
        evidence["explicit_peer_fact_kind_mismatch_count"] = 1
        return None, {
            "status": "FAIL",
            "reason_code": "explicit_peer_source_scope_invalid",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    provenance = source_oracle["review_provenance"]
    evidence.update(
        {
            "review_evidence_label": str(provenance["label"]),
            "reviewer_role": str(provenance["reviewer_role"]),
            "review_method": str(provenance["review_method"]),
            "reviewed_page_count": len(provenance["reviewed_page_numbers"]),
            "is_human_gold": bool(provenance["is_human_gold"]),
            "is_serving_activation_gate": bool(
                provenance["is_serving_activation_gate"]
            ),
            "expected_scope_count": int(fact["expected_scope_count"]),
            "expected_peer_item_count_per_scope": int(
                fact["expected_items_per_scope"]
            ),
            "expected_total_peer_item_count": int(
                fact["expected_total_item_count"]
            ),
        }
    )

    source_nodes_by_id = {
        node.source_node_id: node for node in source_snapshot.nodes
    }
    structure_by_identity: dict[str, list[Any]] = {}
    for node in tree.nodes:
        structure_by_identity.setdefault(node.identity_key, []).append(node)

    resolved_scopes: list[dict[str, Any]] = []
    source_component_mismatch_count = 0
    table_structure_mismatch_count = 0
    row_boundary_mismatch_count = 0
    peer_boundary_mismatch_count = 0
    extra_scoped_child_count = 0
    observed_peer_item_count = 0
    for expected_scope in fact["scopes"]:
        table_source = source_nodes_by_id.get(
            str(expected_scope["table_source_node_id"])
        )
        locator = expected_scope["table_locator"]
        if (
            table_source is None
            or table_source.table_payload is None
            or table_source.native_locator.format.value != "xlsx"
            or table_source.native_locator.workbook != locator["workbook"]
            or table_source.native_locator.sheet != locator["sheet"]
            or table_source.native_locator.cell_range != locator["cell_range"]
        ):
            source_component_mismatch_count += 1
            continue
        table = table_source.table_payload
        cell_candidates = [
            cell
            for cell in table.cells
            if cell.cell_id == expected_scope["cell_id"]
            and cell.native_locator.cell_range == expected_scope["cell_range"]
        ]
        if len(cell_candidates) != 1:
            source_component_mismatch_count += 1
            continue
        cell = cell_candidates[0]
        table_structures = structure_by_identity.get(
            f"source:{table_source.source_node_id}",
            [],
        )
        if (
            len(table_structures) != 1
            or table_structures[0].kind is not StructureNodeKind.TABLE
            or table_structures[0].boundary_basis
            is not StructureBoundaryBasis.TABLE_SPAN
        ):
            table_structure_mismatch_count += max(1, len(table_structures))
            continue
        table_structure = table_structures[0]
        cell_ref = StructureSourceRef(
            kind=StructureSourceRefKind.TABLE_CELL,
            source_id=cell.cell_id,
        )
        expected_row_identity = (
            f"table:{table_source.source_node_id}:row:{cell.row}"
        )
        row_candidates = [
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.parent_structure_node_id
            == table_structure.structure_node_id
            and cell_ref in node.source_refs
        ]
        valid_rows = [
            node
            for node in row_candidates
            if node.identity_key == expected_row_identity
            and node.table_row_index == cell.row
            and node.boundary_basis is StructureBoundaryBasis.TABLE_ROW
        ]
        if len(row_candidates) != 1 or len(valid_rows) != 1:
            row_boundary_mismatch_count += max(1, len(row_candidates))
            continue
        row = valid_rows[0]
        expected_peer_identity_prefix = (
            f"table:{table_source.source_node_id}:row:{cell.row}:"
            f"cell:{cell.cell_id}:peer:"
        )
        scoped_children = [
            node
            for node in tree.nodes
            if node.parent_structure_node_id == row.structure_node_id
            and (
                cell_ref in node.source_refs
                or node.identity_key.startswith(expected_peer_identity_prefix)
            )
        ]
        expected_items = list(expected_scope["items"])
        valid_children_by_ordinal: dict[int, Any] = {}
        for expected_item in expected_items:
            ordinal = int(expected_item["ordinal"])
            expected_identity = f"{expected_peer_identity_prefix}{ordinal}"
            candidates = [
                node
                for node in scoped_children
                if node.list_ordinal == ordinal
            ]
            valid = [
                node
                for node in candidates
                if node.kind is StructureNodeKind.CLAUSE
                and node.boundary_basis
                is StructureBoundaryBasis.EXPLICIT_NUMBER
                and node.identity_key == expected_identity
                and node.source_refs == (cell_ref,)
            ]
            if len(candidates) != 1 or len(valid) != 1:
                peer_boundary_mismatch_count += max(1, len(candidates))
            else:
                valid_children_by_ordinal[ordinal] = valid[0]
        extra_scoped_child_count += max(
            0,
            len(scoped_children) - len(expected_items),
        )
        ordered_children = [
            valid_children_by_ordinal.get(int(item["ordinal"]))
            for item in expected_items
        ]
        if (
            any(child is None for child in ordered_children)
            or [
                child.source_order
                for child in ordered_children
                if child is not None
            ]
            != sorted(
                child.source_order
                for child in ordered_children
                if child is not None
            )
        ):
            peer_boundary_mismatch_count += 1
        observed_peer_item_count += len(scoped_children)
        resolved_scopes.append(
            {
                "expected_scope": expected_scope,
                "table_source": table_source,
                "cell": cell,
                "cell_ref": cell_ref,
                "table_structure": table_structure,
                "row": row,
                "children_by_ordinal": valid_children_by_ordinal,
                "scoped_children": scoped_children,
            }
        )

    evidence.update(
        {
            "resolved_scope_count": len(resolved_scopes),
            "observed_total_peer_item_count": observed_peer_item_count,
            "source_component_mismatch_count": (
                source_component_mismatch_count
            ),
            "table_structure_mismatch_count": (
                table_structure_mismatch_count
            ),
            "row_boundary_mismatch_count": row_boundary_mismatch_count,
            "peer_boundary_mismatch_count": peer_boundary_mismatch_count,
            "extra_scoped_child_count": extra_scoped_child_count,
        }
    )
    structure_matches = bool(
        len(resolved_scopes) == int(fact["expected_scope_count"])
        and observed_peer_item_count
        == int(fact["expected_total_item_count"])
        and source_component_mismatch_count == 0
        and table_structure_mismatch_count == 0
        and row_boundary_mismatch_count == 0
        and peer_boundary_mismatch_count == 0
        and extra_scoped_child_count == 0
        and not evidence["is_human_gold"]
        and not evidence["is_serving_activation_gate"]
    )
    return (
        {
            "source_snapshot": source_snapshot,
            "tree": tree,
            "fact": fact,
            "resolved_scopes": resolved_scopes,
            "structure_matches": structure_matches,
            "evidence": evidence,
        },
        None,
    )


def _evaluate_explicit_table_peer_items(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = STRUCTURE_EXPLICIT_TABLE_PEER_ITEMS_EVALUATOR_ID
    context, failure = _explicit_peer_structure_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    matches = bool(context["structure_matches"])
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "explicit_table_peer_items_observed"
            if matches
            else "explicit_table_peer_items_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": context["evidence"],
    }


def _evaluate_explicit_peer_projection(
    *,
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PROJECTION_EXPLICIT_PEER_SINGLE_REQUIREMENT_EVALUATOR_ID
    context, failure = _explicit_peer_structure_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    assert context is not None
    evidence = context["evidence"]
    if not context["structure_matches"]:
        return {
            "status": "FAIL",
            "reason_code": "explicit_peer_projection_structure_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    semantic_key = (
        tree.structure_snapshot_id,
        DEFAULT_SEMANTIC_CLASSIFIER_ID,
        DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
    )
    try:
        semantics = semantic_snapshots.get(semantic_key)
        if semantics is None:
            semantics = classify_content(
                source_snapshot,
                tree,
                classifier_id=DEFAULT_SEMANTIC_CLASSIFIER_ID,
                classifier_version=DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            )
        validate_semantic_classification_against_inputs(
            semantics,
            source_snapshot,
            tree,
        )
    except SemanticClassificationRejected as exc:
        evidence["semantic_rejection_code"] = exc.code.value
        return {
            "status": "FAIL",
            "reason_code": "explicit_peer_semantic_classification_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["semantic_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "explicit_peer_semantic_classification_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    semantic_snapshots[semantic_key] = semantics

    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return {
            "status": "FAIL",
            "reason_code": "explicit_peer_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "explicit_peer_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view

    annotations_by_owner: dict[str, list[Any]] = {}
    for annotation in semantics.annotations:
        annotations_by_owner.setdefault(
            annotation.owner_structure_node_id,
            [],
        ).append(annotation)

    valid_item6_projection_count = 0
    invalid_item6_annotation_count = 0
    item6_span_mismatch_count = 0
    item6_extra_semantic_unit_count = 0
    item6_extra_projection_count = 0
    parent_row_projection_count = 0
    for resolved_scope in context["resolved_scopes"]:
        expected_item6 = next(
            item
            for item in resolved_scope["expected_scope"]["items"]
            if int(item["ordinal"]) == 6
        )
        item6 = resolved_scope["children_by_ordinal"][6]
        annotations = annotations_by_owner.get(
            item6.structure_node_id,
            [],
        )
        valid_annotations = [
            annotation
            for annotation in annotations
            if annotation.semantic_class is SemanticClass.REQUIREMENT
            and annotation.requirement_subtype
            is RequirementSubtype.PRODUCT_REQUIREMENT
            and annotation.status
            in {
                SemanticAnnotationStatus.CANDIDATE,
                SemanticAnnotationStatus.CONFIRMED,
            }
        ]
        if len(annotations) != 1 or len(valid_annotations) != 1:
            invalid_item6_annotation_count += max(1, len(annotations))
            item6_extra_semantic_unit_count += max(0, len(annotations) - 1)
            continue
        annotation = valid_annotations[0]
        target_spans = [
            span
            for span in annotation.grounding_spans
            if span.source_ref == resolved_scope["cell_ref"]
        ]
        valid_target_spans = [
            span
            for span in target_spans
            if span.start_offset == int(expected_item6["start_offset"])
            and span.end_offset == int(expected_item6["end_offset"])
        ]
        if len(target_spans) != 1 or len(valid_target_spans) != 1:
            item6_span_mismatch_count += max(1, len(target_spans))
            continue
        target_span = valid_target_spans[0]
        item_view_candidates = [
            node
            for node in view.nodes
            if node.owner_structure_node_id == item6.structure_node_id
            or annotation.semantic_unit_id in node.source_semantic_unit_ids
        ]
        valid_views = [
            node
            for node in item_view_candidates
            if node.owner_structure_node_id == item6.structure_node_id
            and node.role is RequirementProjectionRole.PRIMARY
            and node.decision_mode is RequirementDecisionMode.DIRECT
            and node.semantic_class is SemanticClass.REQUIREMENT
            and node.requirement_subtype
            is RequirementSubtype.PRODUCT_REQUIREMENT
            and node.parent_requirement_view_node_id is None
            and node.semantic_annotation_ids
            == (annotation.semantic_annotation_id,)
            and node.source_semantic_unit_ids
            == (annotation.semantic_unit_id,)
            and node.source_span_ids == (target_span.grounding_span_id,)
            and node.source_refs == (resolved_scope["cell_ref"],)
            and node.full_context_source_refs
            == (resolved_scope["cell_ref"],)
        ]
        valid_item6_projection_count += int(
            len(item_view_candidates) == 1 and len(valid_views) == 1
        )
        item6_extra_projection_count += max(
            0,
            len(item_view_candidates) - 1,
        )
        parent_row_projection_count += sum(
            node.owner_structure_node_id
            == resolved_scope["row"].structure_node_id
            for node in view.nodes
        )

    expected_scope_count = int(context["fact"]["expected_scope_count"])
    evidence.update(
        {
            "classifier_id": DEFAULT_SEMANTIC_CLASSIFIER_ID,
            "classifier_version": DEFAULT_SEMANTIC_CLASSIFIER_VERSION,
            "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            "projection_policy": view.projection_policy.value,
            "expected_item6_projection_count": expected_scope_count,
            "valid_item6_primary_direct_projection_count": (
                valid_item6_projection_count
            ),
            "invalid_item6_annotation_count": (
                invalid_item6_annotation_count
            ),
            "item6_span_mismatch_count": item6_span_mismatch_count,
            "item6_extra_semantic_unit_count": (
                item6_extra_semantic_unit_count
            ),
            "item6_extra_projection_count": item6_extra_projection_count,
            "parent_table_row_projection_count": parent_row_projection_count,
        }
    )
    matches = bool(
        valid_item6_projection_count == expected_scope_count
        and invalid_item6_annotation_count == 0
        and item6_span_mismatch_count == 0
        and item6_extra_semantic_unit_count == 0
        and item6_extra_projection_count == 0
        and parent_row_projection_count == 0
        and view.projection_policy
        is RequirementProjectionPolicy.SHADOW_CANDIDATES
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "explicit_peer_item6_single_requirement_projected"
            if matches
            else "explicit_peer_item6_projection_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _pdf_table_evaluator_scope(
    *,
    evaluator_id: str,
    case: dict[str, Any],
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    require_source_oracle_fidelity: bool = True,
) -> tuple[
    Any | None,
    Any | None,
    Any | None,
    list[tuple[Any, Any, Any]],
    dict[str, Any],
    dict[str, Any] | None,
]:
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        require_source_oracle_fidelity=require_source_oracle_fidelity,
    )
    evidence.update(
        {
            "section_locator_count": 0,
            "section_match_count": 0,
            "expected_table_segment_count": int(
                source_oracle["fact"]["expected_segment_count"]
            ),
            "observed_table_segment_count": 0,
        }
    )
    if failure is not None:
        return None, None, None, [], evidence, failure
    assert source_snapshot is not None and tree is not None

    evidence["expected_physical_row_count"] = _pdf_continuation_expected_row_count(
        source_oracle["fact"]
    )
    if require_source_oracle_fidelity:
        measurement = _measure_pdf_table_continuation_group(
            source_snapshot,
            source_oracle["fact"],
        )
        evidence.update(
            {
                "source_fidelity_mismatch_count": int(measurement["mismatch_count"]),
                "observed_physical_row_count": int(
                    measurement["observed_primary_count"]
                ),
            }
        )
        if not measurement["matches"]:
            return (
                source_snapshot,
                tree,
                None,
                [],
                evidence,
                {
                    "status": "FAIL",
                    "reason_code": "pdf_table_source_oracle_mismatch",
                    "evaluator": evaluator_id,
                    "evidence": evidence,
                },
            )
    else:
        evidence["source_fidelity_mismatch_count"] = 0

    document_id = str(source_oracle["document_id"])
    locators = [
        locator
        for locator in case.get("locators", [])
        if isinstance(locator, dict)
        and locator.get("document_id") == document_id
        and locator.get("kind") == "section"
    ]
    evidence["section_locator_count"] = len(locators)
    pointer = str(locators[0].get("pointer") or "") if len(locators) == 1 else ""
    section_label = pointer.partition(" and ")[0].strip()
    if _SECTION_POINTER_PATTERN.fullmatch(section_label) is None:
        return (
            source_snapshot,
            tree,
            None,
            [],
            evidence,
            {
                "status": "FAIL",
                "reason_code": "pdf_table_section_locator_invalid",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    section_candidates = [
        node
        for node in tree.nodes
        if node.kind is StructureNodeKind.SECTION
        and node.explicit_label == section_label
    ]
    evidence["section_match_count"] = len(section_candidates)
    if len(section_candidates) != 1:
        return (
            source_snapshot,
            tree,
            None,
            [],
            evidence,
            {
                "status": "FAIL",
                "reason_code": "pdf_table_section_not_unique",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    section = section_candidates[0]

    source_tables: dict[tuple[int, int], list[tuple[Any, Any]]] = {}
    for node, table in _source_ir_tables(source_snapshot):
        native = node.native_locator
        if (
            native.format.value == "pdf"
            and native.page_number is not None
            and native.table_index is not None
        ):
            source_tables.setdefault(
                (native.page_number, native.table_index),
                [],
            ).append((node, table))
    structure_by_identity: dict[str, list[Any]] = {}
    for node in tree.nodes:
        structure_by_identity.setdefault(node.identity_key, []).append(node)

    segments: list[tuple[Any, Any, Any]] = []
    for expected in source_oracle["fact"]["segments"]:
        locator = expected["locator"]
        key = (int(locator["page_number"]), int(locator["table_index"]))
        candidates = source_tables.get(key, [])
        if len(candidates) != 1:
            continue
        source_node, table = candidates[0]
        structure_candidates = structure_by_identity.get(
            f"source:{source_node.source_node_id}",
            [],
        )
        if len(structure_candidates) != 1:
            continue
        segments.append((source_node, table, structure_candidates[0]))
    evidence["observed_table_segment_count"] = len(segments)
    if len(segments) != int(source_oracle["fact"]["expected_segment_count"]):
        return (
            source_snapshot,
            tree,
            section,
            segments,
            evidence,
            {
                "status": "FAIL",
                "reason_code": "pdf_table_structure_segment_mismatch",
                "evaluator": evaluator_id,
                "evidence": evidence,
            },
        )
    if not require_source_oracle_fidelity:
        try:
            measurement = _measure_pdf_table_continuation_logical_grid(
                source_snapshot,
                source_oracle["fact"],
            )
        except Exception as exc:
            evidence.update(
                {
                    "source_fidelity_mode": "logical_grid_fingerprint",
                    "source_fidelity_evaluated": True,
                    "source_fidelity_mismatch_count": 1,
                    "source_fidelity_measurement_error_type": type(exc).__name__,
                    "representation_neutral_scope_mismatch_count": 1,
                }
            )
            return (
                source_snapshot,
                tree,
                section,
                segments,
                evidence,
                {
                    "status": "FAIL",
                    "reason_code": "pdf_table_representation_neutral_scope_measurement_failed",
                    "evaluator": evaluator_id,
                    "evidence": evidence,
                },
            )
        representation_mismatch_count = int(measurement["mismatch_count"])
        if not measurement["matches"] and representation_mismatch_count == 0:
            representation_mismatch_count = 1
        if len({table.table_id for _, table, _ in segments}) != len(segments) or [
            source.source_order for source, _, _ in segments
        ] != sorted(source.source_order for source, _, _ in segments):
            representation_mismatch_count += 1
        evidence.update(
            {
                "source_fidelity_mode": "logical_grid_fingerprint",
                "source_fidelity_evaluated": True,
                "source_fidelity_mismatch_count": representation_mismatch_count,
                "source_fidelity_expected_fingerprint": measurement[
                    "expected_fingerprint"
                ],
                "source_fidelity_observed_fingerprint": measurement[
                    "observed_fingerprint"
                ],
                "observed_physical_row_count": int(
                    measurement["observed_primary_count"]
                ),
                "expected_logical_grid_cell_count": (
                    _pdf_continuation_expected_logical_slot_count(source_oracle["fact"])
                ),
                "observed_logical_grid_cell_count": int(
                    measurement["observed_logical_grid_cell_count"]
                ),
                "observed_anchor_cell_count": int(
                    measurement["observed_anchor_cell_count"]
                ),
                "observed_merged_region_count": int(
                    measurement["observed_merged_region_count"]
                ),
                "expected_merged_region_count": int(
                    measurement["expected_merge_region_count"]
                ),
                "merge_topology_mismatch_count": int(
                    measurement["merge_topology_mismatch_count"]
                ),
                "representation_neutral_scope_mismatch_count": (
                    representation_mismatch_count
                ),
            }
        )
        if representation_mismatch_count:
            return (
                source_snapshot,
                tree,
                section,
                segments,
                evidence,
                {
                    "status": "FAIL",
                    "reason_code": "pdf_table_representation_neutral_scope_mismatch",
                    "evaluator": evaluator_id,
                    "evidence": evidence,
                },
            )
    return source_snapshot, tree, section, segments, evidence, None


def _evaluate_pdf_table_continuation(
    *,
    case: dict[str, Any],
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    evaluator_id: str = PDF_TABLE_CONTINUATION_EVALUATOR_ID,
) -> dict[str, Any]:
    source_snapshot, tree, section, segments, evidence, failure = (
        _pdf_table_evaluator_scope(
            evaluator_id=evaluator_id,
            case=case,
            source_oracle=source_oracle,
            document_results=document_results,
            source_ir_snapshots=source_ir_snapshots,
            structure_snapshots=structure_snapshots,
            require_source_oracle_fidelity=(
                evaluator_id == PDF_TABLE_CONTINUATION_V1_EVALUATOR_ID
            ),
        )
    )
    if failure is not None:
        return failure
    assert source_snapshot is not None and tree is not None and section is not None

    invalid_parent_count = 0
    invalid_boundary_count = 0
    missing_cell_ref_count = 0
    extra_cell_ref_count = 0
    observed_rows = 0
    segment_structure_ids: list[str] = []
    for source_node, table, table_structure in segments:
        segment_structure_ids.append(table_structure.structure_node_id)
        if (
            table_structure.kind is not StructureNodeKind.TABLE
            or table_structure.parent_structure_node_id != section.structure_node_id
            or table_structure.boundary_basis is not StructureBoundaryBasis.TABLE_SPAN
        ):
            invalid_parent_count += 1
        rows = [
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.parent_structure_node_id == table_structure.structure_node_id
        ]
        observed_rows += len(rows)
        rows_by_index: dict[int, list[Any]] = {}
        for row in rows:
            assert row.table_row_index is not None
            rows_by_index.setdefault(row.table_row_index, []).append(row)
        for row_index in range(table.row_count):
            row_candidates = rows_by_index.get(row_index, [])
            invalid_boundary_count += abs(len(row_candidates) - 1)
            expected_cell_ids = {
                cell.cell_id
                for cell in table.cells
                if cell.row <= row_index < cell.row + cell.row_span
            }
            for row in row_candidates:
                observed_cell_ids = {
                    ref.source_id
                    for ref in row.source_refs
                    if ref.kind is StructureSourceRefKind.TABLE_CELL
                }
                missing_cell_ref_count += len(expected_cell_ids - observed_cell_ids)
                extra_cell_ref_count += len(observed_cell_ids - expected_cell_ids)
                if (
                    row.identity_key
                    != f"table:{source_node.source_node_id}:row:{row_index}"
                    or row.boundary_basis is not StructureBoundaryBasis.TABLE_ROW
                ):
                    invalid_boundary_count += 1

    continuation_matches = [
        relation
        for relation in tree.relations
        if relation.kind is StructureRelationKind.CONTINUES
        and relation.source_structure_node_id == segment_structure_ids[0]
        and relation.target_structure_node_id == segment_structure_ids[1]
    ]
    evidence.update(
        {
            "direct_section_table_count": sum(
                structure.parent_structure_node_id == section.structure_node_id
                for _, _, structure in segments
            ),
            "continuation_relation_count": len(continuation_matches),
            "observed_table_row_boundary_count": observed_rows,
            "invalid_table_parent_count": invalid_parent_count,
            "invalid_row_boundary_count": invalid_boundary_count,
            "missing_table_cell_ref_count": missing_cell_ref_count,
            "extra_table_cell_ref_count": extra_cell_ref_count,
        }
    )
    structure_matches = bool(
        len(continuation_matches) == 1
        and observed_rows == _pdf_continuation_expected_row_count(source_oracle["fact"])
        and invalid_parent_count == 0
        and invalid_boundary_count == 0
        and missing_cell_ref_count == 0
        and extra_cell_ref_count == 0
    )
    if evaluator_id == PDF_TABLE_CONTINUATION_V1_EVALUATOR_ID:
        return {
            "status": "PASS" if structure_matches else "FAIL",
            "reason_code": (
                "pdf_table_continuation_observed"
                if structure_matches
                else "pdf_table_continuation_mismatch"
            ),
            "evaluator": evaluator_id,
            "evidence": evidence,
        }

    presenter_matches = False
    if structure_matches:
        try:
            document_view = build_document_view(source_snapshot, tree)
        except Exception as exc:
            evidence.update(
                {
                    "presenter_build_error_count": 1,
                    "presenter_build_error_type": type(exc).__name__,
                }
            )
        else:
            try:
                presenter_measurement = _measure_document_view_table_continuation(
                    document_view=document_view,
                    section=section,
                    segments=segments,
                    fact=source_oracle["fact"],
                )
            except Exception as exc:
                evidence.update(
                    {
                        "presenter_measurement_error_count": 1,
                        "presenter_measurement_error_type": type(exc).__name__,
                    }
                )
            else:
                evidence.update(presenter_measurement["evidence"])
                presenter_matches = bool(presenter_measurement["matches"])
    else:
        evidence["presenter_not_evaluated_count"] = 1

    matches = structure_matches and presenter_matches
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_table_continuation_document_view_observed"
            if matches
            else "pdf_table_continuation_document_view_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _measure_document_view_table_continuation(
    *,
    document_view: dict[str, Any],
    section: Any,
    segments: list[tuple[Any, Any, Any]],
    fact: dict[str, Any],
) -> dict[str, Any]:
    tables = list(document_view.get("tables") or [])
    expected_table_ids = [table.table_id for _, table, _ in segments]
    expected_source_node_ids = [
        source_node.source_node_id for source_node, _, _ in segments
    ]
    expected_pages = [
        int(segment["locator"]["page_number"]) for segment in fact["segments"]
    ]
    expected_segment_count = int(fact["expected_segment_count"])
    expected_logical_table_id = expected_table_ids[0]
    mismatch_count = 0
    observed_segments: list[dict[str, Any]] = []
    for table_id, source_node_id in zip(
        expected_table_ids,
        expected_source_node_ids,
    ):
        candidates = [
            table
            for table in tables
            if table.get("id") == table_id
            and table.get("source_node_id") == source_node_id
        ]
        if len(candidates) != 1:
            mismatch_count += 1
            continue
        observed_segments.append(candidates[0])

    observed_logical_ids: set[str] = set()
    observed_grid_cell_count = 0
    observed_anchor_cell_count = 0
    for index, (table, expected) in enumerate(zip(observed_segments, fact["segments"])):
        continuation = table.get("continuation") or {}
        observed_logical_ids.add(str(continuation.get("logical_table_id") or ""))
        observed_grid_cell_count += int(table.get("row_count") or 0) * int(
            table.get("column_count") or 0
        )
        observed_anchor_cell_count += len(table.get("cells") or [])
        expected_previous = expected_table_ids[index - 1] if index > 0 else None
        expected_next = (
            expected_table_ids[index + 1]
            if index + 1 < expected_segment_count
            else None
        )
        if (
            continuation.get("status") != "CONTINUED"
            or continuation.get("logical_table_id") != expected_logical_table_id
            or continuation.get("segment_index") != index
            or continuation.get("segment_count") != expected_segment_count
            or continuation.get("previous_table_id") != expected_previous
            or continuation.get("next_table_id") != expected_next
            or table.get("page_start") != expected_pages[index]
            or table.get("page_end") != expected_pages[index]
            or table.get("row_count") != int(expected["row_count"])
            or table.get("column_count") != int(expected["column_count"])
        ):
            mismatch_count += 1

    logical_group = [
        table
        for table in tables
        if (table.get("continuation") or {}).get("logical_table_id")
        == expected_logical_table_id
    ]
    logical_group_ids = [
        table.get("id")
        for table in sorted(
            logical_group,
            key=lambda item: int(
                (item.get("continuation") or {}).get("segment_index") or 0
            ),
        )
    ]
    if logical_group_ids != expected_table_ids:
        mismatch_count += 1

    outline_candidates = [
        item
        for item in list(document_view.get("document_outline") or [])
        if item.get("document_region") == "BODY"
        and item.get("boundary_structure_node_id") == section.structure_node_id
    ]
    context_table_ids: list[str] = []
    if len(outline_candidates) == 1:
        context_table_ids = [
            str(block.get("table_id") or "")
            for block in outline_candidates[0].get("context_blocks") or []
            if block.get("kind") == "table"
            and block.get("table_id") in set(expected_table_ids)
        ]
    if len(outline_candidates) != 1 or context_table_ids != expected_table_ids:
        mismatch_count += 1

    observed_row_count = sum(
        int(table.get("row_count") or 0) for table in observed_segments
    )
    if (
        len(observed_segments) != expected_segment_count
        or observed_row_count != _pdf_continuation_expected_row_count(fact)
        or observed_grid_cell_count
        != _pdf_continuation_expected_logical_slot_count(fact)
        or [table.get("source_order") for table in observed_segments]
        != sorted(table.get("source_order") for table in observed_segments)
    ):
        mismatch_count += 1

    return {
        "matches": mismatch_count == 0,
        "evidence": {
            "presenter_build_error_count": 0,
            "presenter_table_segment_count": len(observed_segments),
            "presenter_logical_table_count": len(observed_logical_ids),
            "presenter_logical_group_segment_count": len(logical_group),
            "presenter_outline_match_count": len(outline_candidates),
            "presenter_context_table_count": len(context_table_ids),
            "presenter_physical_row_count": observed_row_count,
            "presenter_logical_grid_cell_count": observed_grid_cell_count,
            "presenter_anchor_cell_count": observed_anchor_cell_count,
            "presenter_mismatch_count": mismatch_count,
        },
    }


def _evaluate_pdf_table_rows_not_requirements(
    *,
    case: dict[str, Any],
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
    evaluator_id: str = PDF_TABLE_ROWS_NOT_REQUIREMENTS_EVALUATOR_ID,
) -> dict[str, Any]:
    source_snapshot, tree, section, segments, evidence, failure = (
        _pdf_table_evaluator_scope(
            evaluator_id=evaluator_id,
            case=case,
            source_oracle=source_oracle,
            document_results=document_results,
            source_ir_snapshots=source_ir_snapshots,
            structure_snapshots=structure_snapshots,
            require_source_oracle_fidelity=(
                evaluator_id == PDF_TABLE_ROWS_NOT_REQUIREMENTS_V1_EVALUATOR_ID
            ),
        )
    )
    if failure is not None:
        return failure
    assert source_snapshot is not None and tree is not None and section is not None

    semantics, _, error = _classify_document_for_section(
        evaluator_id=evaluator_id,
        source_snapshot=source_snapshot,
        tree=tree,
        section=section,
        evidence=evidence,
        semantic_snapshots=semantic_snapshots,
    )
    if error is not None:
        return error
    assert semantics is not None
    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return {
            "status": "FAIL",
            "reason_code": "requirement_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return {
            "status": "FAIL",
            "reason_code": "requirement_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view

    table_structure_ids = {structure.structure_node_id for _, _, structure in segments}
    table_row_ids = {
        node.structure_node_id
        for node in tree.nodes
        if node.kind is StructureNodeKind.TABLE_ROW
        and node.parent_structure_node_id in table_structure_ids
    }
    projected_rows = [
        node for node in view.nodes if node.target_structure_node_id in table_row_ids
    ]
    projected_tables = [
        node
        for node in view.nodes
        if node.target_structure_node_id in table_structure_ids
    ]
    logical_table_content = [
        node
        for node in projected_tables
        if node.role is RequirementProjectionRole.CONTENT
        and node.decision_mode is RequirementDecisionMode.NONE
    ]
    evidence.update(
        {
            "table_row_structure_count": len(table_row_ids),
            "projected_table_row_count": len(projected_rows),
            "projected_table_segment_count": len(projected_tables),
            "logical_table_content_count": len(logical_table_content),
            "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        }
    )
    matches = bool(
        len(table_row_ids)
        == _pdf_continuation_expected_row_count(source_oracle["fact"])
        and len(projected_rows) == 0
        and len(projected_tables) == 1
        and len(logical_table_content) == 1
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_table_rows_not_projected_as_requirements"
            if matches
            else "pdf_table_row_projection_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _pdf_table_note_evaluator_scope(
    *,
    evaluator_id: str,
    case: dict[str, Any],
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    source_snapshot, tree, evidence, failure = _structure_evaluator_context(
        evaluator_id=evaluator_id,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    context: dict[str, Any] = {
        "source_snapshot": source_snapshot,
        "tree": tree,
        "evidence": evidence,
    }
    if failure is not None:
        return context, failure
    assert source_snapshot is not None and tree is not None
    fact = source_oracle["fact"]
    section_label = str(fact["section_label"])
    document_id = str(source_oracle["document_id"])
    section_locators = [
        locator
        for locator in case.get("locators", [])
        if isinstance(locator, dict)
        and locator.get("document_id") == document_id
        and locator.get("kind") == "section"
        and locator.get("pointer") == section_label
    ]
    sections = [
        node
        for node in tree.nodes
        if node.kind is StructureNodeKind.SECTION
        and node.explicit_label == section_label
    ]
    evidence.update(
        {
            "section_locator_count": len(section_locators),
            "section_match_count": len(sections),
            "table_match_count": 0,
            "child_assertion_match_count": 0,
        }
    )
    if len(section_locators) != 1 or len(sections) != 1:
        return context, {
            "status": "FAIL",
            "reason_code": "pdf_table_note_section_not_unique",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    section = sections[0]

    locator = fact["table"]["locator"]
    table_candidates = [
        (node, table)
        for node, table in _source_ir_tables(source_snapshot)
        if node.native_locator.format.value == "pdf"
        and node.native_locator.page_number == int(locator["page_number"])
        and node.native_locator.table_index == int(locator["table_index"])
    ]
    evidence["table_match_count"] = len(table_candidates)
    if len(table_candidates) != 1:
        return context, {
            "status": "FAIL",
            "reason_code": "pdf_table_note_source_table_not_unique",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    table_source_node, table = table_candidates[0]
    table_structures = [
        node
        for node in tree.nodes
        if node.identity_key == f"source:{table_source_node.source_node_id}"
    ]

    child_record = fact["child_assertion"]
    child_locator = child_record["locator"]
    child_source_candidates = [
        node
        for node in source_snapshot.nodes
        if node.kind.value == child_locator["source_node_kind"]
        and node.native_locator.format.value == "pdf"
        and node.native_locator.page_number == int(child_locator["page_number"])
        and node.native_locator.block_index == int(child_locator["block_index"])
        and _sha256_text(node.raw_text) == child_record["raw_sha256"]
    ]
    child_structures = (
        [
            node
            for node in tree.nodes
            if node.identity_key
            == f"source:{child_source_candidates[0].source_node_id}"
        ]
        if len(child_source_candidates) == 1
        else []
    )
    evidence["child_assertion_match_count"] = len(child_structures)
    if (
        len(table_structures) != 1
        or len(child_structures) != 1
        or table_structures[0].parent_structure_node_id != section.structure_node_id
        or child_structures[0].parent_structure_node_id != section.structure_node_id
    ):
        return context, {
            "status": "FAIL",
            "reason_code": "pdf_table_note_structure_scope_mismatch",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    context.update(
        {
            "section": section,
            "table_source_node": table_source_node,
            "table": table,
            "table_structure": table_structures[0],
            "child_source_node": child_source_candidates[0],
            "child_structure": child_structures[0],
        }
    )
    return context, None


def _pdf_table_note_projection_context(
    *,
    evaluator_id: str,
    case: dict[str, Any],
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    context, failure = _pdf_table_note_evaluator_scope(
        evaluator_id=evaluator_id,
        case=case,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return context, failure
    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    section = context["section"]
    evidence = context["evidence"]
    semantics, _, failure = _classify_document_for_section(
        evaluator_id=evaluator_id,
        source_snapshot=source_snapshot,
        tree=tree,
        section=section,
        evidence=evidence,
        semantic_snapshots=semantic_snapshots,
    )
    if failure is not None:
        return context, failure
    assert semantics is not None
    view_key = (
        semantics.semantic_snapshot_id,
        DEFAULT_REQUIREMENT_PROJECTOR_ID,
        DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        RequirementProjectionPolicy.SHADOW_CANDIDATES.value,
    )
    try:
        view = requirement_view_snapshots.get(view_key)
        if view is None:
            view = project_requirements(
                source_snapshot,
                tree,
                semantics,
                projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
                projector_id=DEFAULT_REQUIREMENT_PROJECTOR_ID,
                projector_version=DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
            )
        validate_requirement_view_against_inputs(
            view,
            source_snapshot,
            tree,
            semantics,
        )
    except RequirementProjectionRejected as exc:
        evidence["projection_rejection_code"] = exc.code.value
        return context, {
            "status": "FAIL",
            "reason_code": "requirement_projection_rejected",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    except Exception as exc:
        evidence["projection_error_type"] = type(exc).__name__
        return context, {
            "status": "FAIL",
            "reason_code": "requirement_projection_failed",
            "evaluator": evaluator_id,
            "evidence": evidence,
        }
    requirement_view_snapshots[view_key] = view
    context.update({"semantics": semantics, "view": view})
    evidence.update(
        {
            "projector_id": DEFAULT_REQUIREMENT_PROJECTOR_ID,
            "projector_version": DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
        }
    )
    return context, None


def _evaluate_pdf_table_note_applies_to(
    *,
    case: dict[str, Any],
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_TABLE_NOTE_APPLIES_TO_EVALUATOR_ID
    context, failure = _pdf_table_note_evaluator_scope(
        evaluator_id=evaluator_id,
        case=case,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    tree = context["tree"]
    table = context["table"]
    table_structure = context["table_structure"]
    evidence = context["evidence"]
    rows_by_index = {
        node.table_row_index: node
        for node in tree.nodes
        if node.kind is StructureNodeKind.TABLE_ROW
        and node.parent_structure_node_id == table_structure.structure_node_id
    }
    note_row_index = int(source_oracle["fact"]["note_row_index"])
    parameter_row_indices = tuple(
        int(value) for value in source_oracle["fact"]["parameter_row_indices"]
    )
    note_row = rows_by_index.get(note_row_index)
    target_ids = {
        rows_by_index[index].structure_node_id
        for index in parameter_row_indices
        if index in rows_by_index
    }
    relations = [
        relation
        for relation in tree.relations
        if relation.kind is StructureRelationKind.APPLIES_TO
        and note_row is not None
        and relation.source_structure_node_id == note_row.structure_node_id
    ]
    note_cell_ids = {
        cell.cell_id
        for cell in table.cells
        if cell.row == note_row_index and cell.raw_text.strip()
    }
    invalid_grounding_count = sum(
        len(relation.source_refs) != 1
        or relation.source_refs[0].kind is not StructureSourceRefKind.TABLE_CELL
        or relation.source_refs[0].source_id not in note_cell_ids
        for relation in relations
    )
    observed_target_ids = {relation.target_structure_node_id for relation in relations}
    evidence.update(
        {
            "expected_applies_to_count": len(parameter_row_indices),
            "observed_applies_to_count": len(relations),
            "note_row_boundary_count": int(note_row is not None),
            "parameter_row_boundary_count": len(target_ids),
            "invalid_relation_grounding_count": invalid_grounding_count,
            "relation_target_mismatch_count": len(
                observed_target_ids.symmetric_difference(target_ids)
            ),
        }
    )
    matches = bool(
        note_row is not None
        and len(target_ids) == len(parameter_row_indices)
        and len(relations) == len(parameter_row_indices)
        and observed_target_ids == target_ids
        and invalid_grounding_count == 0
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_table_note_applies_to_observed"
            if matches
            else "pdf_table_note_applies_to_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_table_parameter_count(
    *,
    case: dict[str, Any],
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_TABLE_PARAMETER_COUNT_EVALUATOR_ID
    context, failure = _pdf_table_note_projection_context(
        evaluator_id=evaluator_id,
        case=case,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if failure is not None:
        return failure
    tree = context["tree"]
    view = context["view"]
    table_structure = context["table_structure"]
    evidence = context["evidence"]
    expected_indices = set(source_oracle["fact"]["parameter_row_indices"])
    row_ids_by_index = {
        node.table_row_index: node.structure_node_id
        for node in tree.nodes
        if node.kind is StructureNodeKind.TABLE_ROW
        and node.parent_structure_node_id == table_structure.structure_node_id
    }
    expected_target_ids = {
        row_ids_by_index[index]
        for index in expected_indices
        if index in row_ids_by_index
    }
    projected_parameters = [
        node
        for node in view.nodes
        if node.target_structure_node_id in set(row_ids_by_index.values())
        and node.role is RequirementProjectionRole.PARAMETER
        and node.decision_mode is RequirementDecisionMode.DIRECT
    ]
    observed_target_ids = {
        node.target_structure_node_id for node in projected_parameters
    }
    evidence.update(
        {
            "expected_parameter_count": len(expected_indices),
            "observed_parameter_count": len(projected_parameters),
            "parameter_target_mismatch_count": len(
                observed_target_ids.symmetric_difference(expected_target_ids)
            ),
        }
    )
    matches = bool(
        len(expected_target_ids) == len(expected_indices)
        and len(projected_parameters) == len(expected_indices)
        and observed_target_ids == expected_target_ids
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_table_parameter_count_observed"
            if matches
            else "pdf_table_parameter_count_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_section_child_assertion(
    *,
    case: dict[str, Any],
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
    semantic_snapshots: dict[tuple[str, str, str], Any],
    requirement_view_snapshots: dict[tuple[str, str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_SECTION_CHILD_ASSERTION_EVALUATOR_ID
    context, failure = _pdf_table_note_projection_context(
        evaluator_id=evaluator_id,
        case=case,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
        semantic_snapshots=semantic_snapshots,
        requirement_view_snapshots=requirement_view_snapshots,
    )
    if failure is not None:
        return failure
    view = context["view"]
    child_structure = context["child_structure"]
    section = context["section"]
    tree = context["tree"]
    table_structure = context["table_structure"]
    evidence = context["evidence"]
    section_primary = [
        node
        for node in view.nodes
        if node.target_structure_node_id == section.structure_node_id
        and node.role is RequirementProjectionRole.PRIMARY
        and node.decision_mode is RequirementDecisionMode.ROLLUP
    ]
    row_ids_by_index = {
        node.table_row_index: node.structure_node_id
        for node in tree.nodes
        if node.kind is StructureNodeKind.TABLE_ROW
        and node.parent_structure_node_id == table_structure.structure_node_id
    }
    parameter_row_indices = [
        int(value) for value in source_oracle["fact"]["parameter_row_indices"]
    ]
    expected_sequence = [
        (
            row_ids_by_index.get(row_index),
            RequirementProjectionRole.PARAMETER,
            RequirementDecisionMode.DIRECT,
        )
        for row_index in parameter_row_indices
    ] + [
        (
            child_structure.structure_node_id,
            RequirementProjectionRole.CHILD,
            RequirementDecisionMode.DIRECT,
        )
    ]
    decision_children = (
        sorted(
            (
                node
                for node in view.nodes
                if node.parent_requirement_view_node_id
                == section_primary[0].requirement_view_node_id
                and node.role
                in {
                    RequirementProjectionRole.PARAMETER,
                    RequirementProjectionRole.CHILD,
                }
            ),
            key=lambda node: (node.source_order, node.requirement_view_node_id),
        )
        if len(section_primary) == 1
        else []
    )
    observed_sequence = [
        (node.target_structure_node_id, node.role, node.decision_mode)
        for node in decision_children
    ]
    evidence.update(
        {
            "section_primary_count": len(section_primary),
            "expected_decision_child_count": len(expected_sequence),
            "observed_decision_child_count": len(observed_sequence),
            "decision_child_order_mismatch_count": int(
                observed_sequence != expected_sequence
            ),
            "third_child_assertion_count": sum(
                node.target_structure_node_id == child_structure.structure_node_id
                and node.role is RequirementProjectionRole.CHILD
                and node.decision_mode is RequirementDecisionMode.DIRECT
                for node in decision_children[2:3]
            ),
        }
    )
    matches = bool(
        len(section_primary) == 1
        and all(target_id is not None for target_id, _, _ in expected_sequence)
        and observed_sequence == expected_sequence
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_section_child_assertion_observed"
            if matches
            else "pdf_section_child_assertion_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _evaluate_pdf_duplicate_table_label(
    *,
    case: dict[str, Any],
    source_oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    structure_snapshots: dict[tuple[str, str, str], Any],
) -> dict[str, Any]:
    evaluator_id = PDF_DUPLICATE_TABLE_LABEL_EVALUATOR_ID
    context, failure = _pdf_table_note_evaluator_scope(
        evaluator_id=evaluator_id,
        case=case,
        source_oracle=source_oracle,
        document_results=document_results,
        source_ir_snapshots=source_ir_snapshots,
        structure_snapshots=structure_snapshots,
    )
    if failure is not None:
        return failure
    source_snapshot = context["source_snapshot"]
    tree = context["tree"]
    evidence = context["evidence"]
    expected_records = source_oracle["fact"]["duplicate_caption_records"]
    expected_nodes_by_key: dict[tuple[Any, ...], Any] = {}
    for record in expected_records:
        locator = record["locator"]
        matches = [
            node
            for node in source_snapshot.nodes
            if node.kind.value == locator["source_node_kind"]
            and node.native_locator.page_number == int(locator["page_number"])
            and node.native_locator.block_index == int(locator["block_index"])
            and _sha256_text(node.raw_text) == record["raw_sha256"]
        ]
        if len(matches) == 1:
            expected_nodes_by_key[_pdf_text_locator_record_key(record)] = matches[0]
    expected_source_ids = {
        node.source_node_id for node in expected_nodes_by_key.values()
    }
    current_caption_key = max(
        _pdf_text_locator_record_key(record) for record in expected_records
    )
    current_caption_node = expected_nodes_by_key.get(current_caption_key)
    current_caption_structures = (
        [
            node
            for node in tree.nodes
            if node.identity_key == f"source:{current_caption_node.source_node_id}"
        ]
        if current_caption_node is not None
        else []
    )
    current_caption_structure_id = (
        current_caption_structures[0].structure_node_id
        if len(current_caption_structures) == 1
        else None
    )
    target_pages = {
        int(record["locator"]["page_number"]) for record in expected_records
    }
    target_page_source_ids = {
        node.source_node_id
        for node in source_snapshot.nodes
        if node.native_locator.page_number in target_pages
    }
    target_page_source_ids.update(
        cell.cell_id
        for node in source_snapshot.nodes
        if node.table_payload is not None
        for cell in node.table_payload.cells
        if cell.native_locator.page_number in target_pages
    )
    page_findings = [
        finding
        for finding in tree.findings
        if finding.code == "duplicate_table_caption_label"
        and any(ref.source_id in target_page_source_ids for ref in finding.source_refs)
    ]
    scoped_findings = [
        finding
        for finding in page_findings
        if {
            ref.source_id
            for ref in finding.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
        }
        == expected_source_ids
    ]
    valid_findings = [
        finding
        for finding in scoped_findings
        if len(finding.source_refs) == len(expected_source_ids)
        and finding.severity is StructureFindingSeverity.WARNING
        and finding.blocking is False
        and finding.message_key == "structure.duplicate_table_caption_label"
        and finding.structure_node_id == current_caption_structure_id
    ]
    evidence.update(
        {
            "expected_caption_source_count": len(expected_records),
            "observed_caption_source_count": len(expected_source_ids),
            "current_caption_structure_count": len(current_caption_structures),
            "target_page_count": len(target_pages),
            "target_page_finding_count": len(page_findings),
            "scoped_finding_count": len(scoped_findings),
            "valid_warning_finding_count": len(valid_findings),
            "invalid_finding_policy_count": len(scoped_findings) - len(valid_findings),
        }
    )
    matches = bool(
        len(expected_source_ids) == len(expected_records) == 2
        and len(target_pages) == 1
        and len(current_caption_structures) == 1
        and len(page_findings) == 1
        and len(scoped_findings) == 1
        and len(valid_findings) == 1
    )
    return {
        "status": "PASS" if matches else "FAIL",
        "reason_code": (
            "pdf_duplicate_table_label_warning_observed"
            if matches
            else "pdf_duplicate_table_label_warning_mismatch"
        ),
        "evaluator": evaluator_id,
        "evidence": evidence,
    }


def _xlsx_table_nodes_containing_range(
    snapshot: Any,
    *,
    locator: dict[str, Any],
    target_bounds: tuple[int, int, int, int],
) -> list[tuple[Any, Any]]:
    return [
        (node, table)
        for node, table in _source_ir_tables(snapshot)
        if (
            node.native_locator.format.value == "xlsx"
            and node.native_locator.workbook == locator["workbook"]
            and node.native_locator.sheet == locator["sheet"]
            and node.native_locator.cell_range is not None
            and _bounds_contain(
                _parse_a1_range(node.native_locator.cell_range),
                target_bounds,
            )
        )
    ]


def _pdf_continuation_expected_row_count(fact: dict[str, Any]) -> int:
    if fact["kind"] == "pdf_table_continuation_logical_grid":
        return int(fact["expected_row_count"])
    return int(fact["expected_physical_row_count"])


def _pdf_continuation_expected_logical_slot_count(
    fact: dict[str, Any],
) -> int:
    if fact["kind"] == "pdf_table_continuation_logical_grid":
        return int(fact["expected_logical_slot_count"])
    return int(fact["expected_cell_count"])


def _oracle_expected_primary_count(fact: dict[str, Any]) -> int:
    kind = fact["kind"]
    if kind == "xlsx_cell_range":
        return int(fact["expected_cell_count"])
    if kind == "docx_table_group":
        return int(fact["expected_table_count"])
    if kind == "docx_table_row_scope":
        return 1
    if kind == "docx_mixed_content_group":
        return int(fact["expected_narrative_count"]) + int(
            fact["expected_parameter_table_count"]
        )
    if kind == "xlsx_material_requirement_scope":
        return int(fact["expected_requirement_count"])
    if kind == "xlsx_explicit_peer_scope_group":
        return int(fact["expected_total_item_count"])
    if kind == "pdf_table_continuation_group":
        return int(fact["expected_physical_row_count"])
    if kind == "pdf_table_continuation_logical_grid":
        return int(fact["expected_row_count"])
    if kind == "pdf_table_note_label_group":
        return int(fact["expected_label_record_count"])
    if kind == "pdf_option_state_group":
        return int(fact["expected_option_count"])
    if kind == "pdf_revision_history_group":
        return int(fact["expected_document_count"])
    if kind == "source_scope_group":
        return int(fact["expected_document_count"])
    return int(fact["expected_merge_count"])


def _measure_oracle_fact(
    snapshot: Any,
    fact: dict[str, Any],
    *,
    oracle_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = fact["kind"]
    if kind == "xlsx_cell_range":
        return _measure_xlsx_cell_range(snapshot, fact)
    if kind == "docx_table_group":
        return _measure_docx_table_group(snapshot, fact)
    if kind == "docx_table_row_scope":
        return _measure_docx_table_row_scope(snapshot, fact)
    if kind == "docx_mixed_content_group":
        if oracle_registry is None:
            raise ValueError("DOCX mixed-content measurement requires oracle registry")
        component_oracle = (oracle_registry["oracles"])[
            str(fact["parameter_table_oracle_id"])
        ]
        return _measure_docx_mixed_content_group(
            snapshot,
            fact,
            parameter_table_fact=component_oracle["fact"],
        )
    if kind == "xlsx_merge_group":
        return _measure_xlsx_merge_group(snapshot, fact)
    if kind == "xlsx_material_requirement_scope":
        return _measure_xlsx_material_requirement_scope(snapshot, fact)
    if kind == "xlsx_explicit_peer_scope_group":
        return _measure_xlsx_explicit_peer_scope_group(snapshot, fact)
    if kind == "pdf_table_continuation_group":
        return _measure_pdf_table_continuation_group(snapshot, fact)
    if kind == "pdf_table_continuation_logical_grid":
        return _measure_pdf_table_continuation_logical_grid(snapshot, fact)
    if kind == "pdf_table_note_label_group":
        return _measure_pdf_table_note_label_group(snapshot, fact)
    if kind == "pdf_option_state_group":
        return _measure_pdf_option_state_group(snapshot, fact)
    raise ValueError("unsupported oracle fact kind")


def _source_ir_tables(snapshot: Any) -> tuple[tuple[Any, Any], ...]:
    return tuple(
        (node, node.table_payload)
        for node in snapshot.nodes
        if node.table_payload is not None
    )


def _pdf_revision_history_row_text(table: Any, row_index: int) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize(
            "NFKC",
            " ".join(
                cell.raw_text
                for cell in sorted(
                    (cell for cell in table.cells if cell.row == row_index),
                    key=lambda cell: (
                        cell.column,
                        cell.row_span,
                        cell.column_span,
                    ),
                )
                if cell.raw_text.strip()
            ),
        ),
    ).strip()


def _pdf_revision_history_row_indices(table: Any) -> list[int]:
    row_texts = [
        _pdf_revision_history_row_text(table, row_index)
        for row_index in range(table.row_count)
    ]
    marker_positions = [
        row_index
        for row_index, row_text in enumerate(row_texts)
        if re.search(
            r"(?:修订|变更)(?:记录|历史)|"
            r"\b(?:revision|change)\s+history\b",
            row_text,
            re.IGNORECASE,
        )
    ]
    if len(marker_positions) != 1:
        return []
    marker_position = marker_positions[0]
    revision_rows: list[int] = []
    for row_index in range(
        marker_position + 1,
        min(table.row_count, marker_position + 7),
    ):
        row_text = row_texts[row_index]
        if not row_text and row_index > marker_position + 1:
            break
        if re.search(
            r"(?<![A-Za-z0-9])V?\s*\d+\s*(?:[.．]\s*\d+)+"
            r"(?![A-Za-z0-9])",
            row_text,
            re.IGNORECASE,
        ):
            revision_rows.append(row_index)
    return revision_rows


def _measure_pdf_revision_history_document(
    snapshot: Any,
    expected_document: dict[str, Any],
) -> tuple[dict[str, Any] | None, int, int]:
    expected_table = expected_document["table"]
    locator = expected_table["locator"]
    candidates = [
        table
        for node, table in _source_ir_tables(snapshot)
        if node.native_locator.format.value == "pdf"
        and node.native_locator.page_number == int(locator["page_number"])
        and node.native_locator.table_index == int(locator["table_index"])
    ]
    if len(candidates) != 1:
        return None, abs(len(candidates) - 1) or 1, 0
    table = candidates[0]
    coverage: dict[tuple[int, int], list[Any]] = {}
    locator_mismatch_count = 0
    for cell in table.cells:
        native = cell.native_locator
        if (
            native.format.value != "pdf"
            or native.page_number != int(locator["page_number"])
            or native.table_index != int(locator["table_index"])
            or native.row_index != cell.row
            or native.column_index != cell.column
        ):
            locator_mismatch_count += 1
        if (
            cell.row < 0
            or cell.column < 0
            or cell.row_span < 1
            or cell.column_span < 1
            or cell.row + cell.row_span > table.row_count
            or cell.column + cell.column_span > table.column_count
        ):
            locator_mismatch_count += 1
            continue
        for row_index in range(cell.row, cell.row + cell.row_span):
            for column_index in range(
                cell.column,
                cell.column + cell.column_span,
            ):
                coverage.setdefault((row_index, column_index), []).append(cell)

    rows: list[dict[str, Any]] = []
    coverage_mismatch_count = 0
    for row_index in range(table.row_count):
        logical_cells: list[dict[str, Any]] = []
        for column_index in range(table.column_count):
            covering_cells = coverage.get((row_index, column_index), [])
            if len(covering_cells) != 1:
                coverage_mismatch_count += 1
                raw_text = ""
            else:
                anchor = covering_cells[0]
                raw_text = (
                    anchor.raw_text
                    if (anchor.row, anchor.column) == (row_index, column_index)
                    else ""
                )
            logical_cells.append(
                {
                    "column": column_index,
                    "row_span": 1,
                    "column_span": 1,
                    "raw_sha256": _sha256_text(raw_text),
                }
            )
        rows.append(
            {
                "row_index": row_index,
                "logical_slot_count": len(logical_cells),
                "row_fingerprint": _sha256_text(_canonical_json_payload(logical_cells)),
            }
        )
    actual_document = {
        "document_id": str(expected_document["document_id"]),
        "table": {
            "locator": dict(locator),
            "row_count": table.row_count,
            "column_count": table.column_count,
            "logical_slot_count": table.row_count * table.column_count,
            "rows": rows,
        },
        "revision_row_indices": _pdf_revision_history_row_indices(table),
    }
    actual_document["table_fingerprint"] = _fingerprint_pdf_revision_history_document(
        actual_document
    )
    mismatch_count = (
        locator_mismatch_count
        + coverage_mismatch_count
        + int(actual_document != expected_document)
    )
    return (
        actual_document,
        mismatch_count,
        table.row_count * table.column_count,
    )


def _measure_pdf_revision_history_group(
    *,
    oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
) -> dict[str, Any]:
    fact = oracle["fact"]
    expected_documents = list(fact["documents"])
    expected_source_hashes = _oracle_source_sha256_by_document(oracle)
    actual_documents: list[dict[str, Any]] = []
    mismatch_count = 0
    observed_logical_slot_count = 0
    source_ir_ids: list[str] = []
    for expected_document in expected_documents:
        document_id = str(expected_document["document_id"])
        document_result = document_results.get(document_id)
        snapshot = (
            _source_ir_snapshot_for_document_result(
                document_result,
                source_ir_snapshots=source_ir_snapshots,
            )
            if document_result is not None
            else None
        )
        if snapshot is None or snapshot.source_sha256 != expected_source_hashes.get(
            document_id
        ):
            mismatch_count += 1
            continue
        source_ir_ids.append(snapshot.source_ir_id)
        actual_document, document_mismatch_count, logical_slot_count = (
            _measure_pdf_revision_history_document(
                snapshot,
                expected_document,
            )
        )
        mismatch_count += document_mismatch_count
        observed_logical_slot_count += logical_slot_count
        if actual_document is not None:
            actual_documents.append(actual_document)
    actual_documents.sort(key=lambda document: str(document["document_id"]))
    actual_fingerprint = _fingerprint_pdf_revision_history_group(actual_documents)
    expected_logical_slot_count = sum(
        int(document["table"]["logical_slot_count"]) for document in expected_documents
    )
    matches = bool(
        len(actual_documents) == int(fact["expected_document_count"])
        and observed_logical_slot_count == expected_logical_slot_count
        and mismatch_count == 0
        and actual_fingerprint == fact["group_fingerprint"]
    )
    return {
        "matches": matches,
        "expected_primary_count": int(fact["expected_document_count"]),
        "observed_primary_count": len(actual_documents),
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["group_fingerprint"],
        "observed_fingerprint": actual_fingerprint,
        "expected_logical_slot_count": expected_logical_slot_count,
        "observed_logical_slot_count": observed_logical_slot_count,
        "expected_revision_row_count": int(fact["expected_revision_row_count"]),
        "observed_revision_row_count": sum(
            len(document["revision_row_indices"]) for document in actual_documents
        ),
        "distinct_source_ir_count": len(set(source_ir_ids)),
    }


def _canonical_scope_bbox(value: Iterable[float] | None) -> list[float] | None:
    if value is None:
        return None
    return [round(float(item), 4) for item in value]


def _scope_native_locator_record(locator: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"format": locator.format.value}
    for field in (
        "page_number",
        "block_index",
        "table_index",
        "row_index",
        "column_index",
        "body_element_index",
        "paragraph_index",
        "object_index",
        "workbook",
        "sheet",
        "cell_range",
    ):
        value = getattr(locator, field, None)
        if value is not None:
            record[field] = value
    bbox = _canonical_scope_bbox(getattr(locator, "bbox", None))
    if bbox is not None:
        record["bbox"] = bbox
    return record


def _scope_table_cell_record(cell: Any) -> dict[str, Any]:
    return {
        "row": int(cell.row),
        "column": int(cell.column),
        "row_span": int(cell.row_span),
        "column_span": int(cell.column_span),
        "raw_sha256": _sha256_text(str(cell.raw_text)),
        "formula_sha256": (
            _sha256_text(str(cell.formula)) if cell.formula is not None else None
        ),
        "display_value_sha256": (
            _sha256_text(str(cell.display_value))
            if cell.display_value is not None
            else None
        ),
        "native_locator": _scope_native_locator_record(cell.native_locator),
    }


def _scope_table_record(
    node: Any,
    *,
    selected_cells: Iterable[Any] | None = None,
) -> dict[str, Any]:
    table = node.table_payload
    if table is None:
        raise ValueError("source-scope table record requires a table payload")
    cells = sorted(
        tuple(selected_cells) if selected_cells is not None else table.cells,
        key=lambda cell: (cell.row, cell.column, cell.row_span, cell.column_span),
    )
    return {
        "native_locator": _scope_native_locator_record(node.native_locator),
        "row_count": int(table.row_count),
        "column_count": int(table.column_count),
        "selected_cell_count": len(cells),
        "cells": [_scope_table_cell_record(cell) for cell in cells],
        "header_regions": [
            {
                "row": int(region.row),
                "column": int(region.column),
                "row_span": int(region.row_span),
                "column_span": int(region.column_span),
            }
            for region in table.header_regions
        ],
        "merged_regions": [
            {
                "row": int(region.row),
                "column": int(region.column),
                "row_span": int(region.row_span),
                "column_span": int(region.column_span),
            }
            for region in table.merged_regions
        ],
        "note_sha256": [_sha256_text(str(note)) for note in table.notes],
    }


def _scope_asset_record(asset: Any) -> dict[str, Any]:
    return {
        "kind": asset.kind.value,
        "content_sha256": str(asset.content_sha256),
        "media_type": str(asset.media_type),
        "native_locator": (
            _scope_native_locator_record(asset.native_locator)
            if asset.native_locator is not None
            else None
        ),
    }


def _scope_visual_layout_table_record(table: Any | None) -> dict[str, Any] | None:
    if table is None:
        return None
    return {
        "row_count": int(table.row_count),
        "column_count": int(table.column_count),
        "header_row_count": int(table.header_row_count),
        "starts_mid_table": bool(table.starts_mid_table),
        "continues_next_page": bool(table.continues_next_page),
        "cells": [
            {
                "row": int(cell.row),
                "column": int(cell.column),
                "row_span": int(cell.row_span),
                "column_span": int(cell.column_span),
                "raw_sha256": _sha256_text(str(cell.raw_text)),
            }
            for cell in table.cells
        ],
    }


def _scope_visual_observation_record(snapshot: Any, observation: Any) -> dict[str, Any]:
    return {
        "page_number": int(observation.page_number),
        "observation_index": int(observation.observation_index),
        "bbox": _canonical_scope_bbox(observation.bbox),
        "raw_sha256": _sha256_text(str(observation.raw_text)),
        "content_type": str(observation.content_type),
        "role": observation.role,
        "parent_observation_index": observation.parent_observation_index,
        "parent_table_cell": (
            list(observation.parent_table_cell)
            if observation.parent_table_cell is not None
            else None
        ),
        "provider_id": str(snapshot.provider_id),
        "provider_version_sha256": _sha256_text(str(snapshot.provider_version)),
        "page_image_sha256": str(snapshot.page_image_sha256),
        "layout_table": _scope_visual_layout_table_record(observation.layout_table),
    }


def _scope_visual_table_record(snapshot: Any) -> dict[str, Any]:
    return {
        "page_number": int(snapshot.page_number),
        "table_index": int(snapshot.table_index),
        "table_bbox": _canonical_scope_bbox(snapshot.table_bbox),
        "crop_image_sha256": str(snapshot.crop_image_sha256),
        "provider_id": str(snapshot.provider_id),
        "provider_version_sha256": _sha256_text(str(snapshot.provider_version)),
        "row_count": int(snapshot.row_count),
        "column_count": int(snapshot.column_count),
        "cells": [
            {
                "row": int(cell.row),
                "column": int(cell.column),
                "row_span": int(cell.row_span),
                "column_span": int(cell.column_span),
                "raw_sha256": _sha256_text(str(cell.raw_text)),
            }
            for cell in snapshot.cells
        ],
    }


def _bounds_overlap(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
) -> bool:
    return bool(
        left[0] <= right[2]
        and right[0] <= left[2]
        and left[1] <= right[3]
        and right[1] <= left[3]
    )


def _xlsx_scope_ranges(
    selector: dict[str, Any],
) -> dict[str, list[tuple[int, int, int, int]]]:
    if selector["mode"] == "WORKBOOK":
        return {}
    ranges: dict[str, list[tuple[int, int, int, int]]] = {}
    for item in selector["ranges"]:
        ranges.setdefault(str(item["sheet"]), []).append(
            _parse_a1_range(str(item["cell_range"]))
        )
    return ranges


def _xlsx_cell_in_scope(
    cell: Any,
    *,
    selector: dict[str, Any],
    ranges_by_sheet: dict[str, list[tuple[int, int, int, int]]],
) -> bool:
    if selector["mode"] == "WORKBOOK":
        return True
    locator = cell.native_locator
    if locator.sheet is None or locator.cell_range is None:
        return False
    return any(
        _bounds_overlap(_parse_a1_range(locator.cell_range), bounds)
        for bounds in ranges_by_sheet.get(locator.sheet, [])
    )


def _measure_source_scope_snapshot(
    snapshot: Any,
    expected_scope: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    selector = dict(expected_scope["selector"])
    native_nodes: list[dict[str, Any]] = []
    native_tables: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    visual_observations: list[dict[str, Any]] = []
    visual_tables: list[dict[str, Any]] = []
    selected_asset_ids: set[str] = set()

    if selector["format"] == "pdf":
        page_numbers = {int(value) for value in selector["page_numbers"]}
        include_native = selector["channel_mode"] in {"NATIVE", "COMBINED"}
        include_visual = selector["channel_mode"] in {"VISUAL", "COMBINED"}
        if include_native:
            for node in snapshot.nodes:
                locator = node.native_locator
                if (
                    locator.format.value != "pdf"
                    or locator.page_number not in page_numbers
                ):
                    continue
                native_nodes.append(
                    {
                        "kind": node.kind.value,
                        "native_locator": _scope_native_locator_record(locator),
                        "raw_sha256": _sha256_text(str(node.raw_text)),
                        "asset_content_sha256": (
                            next(
                                (
                                    str(asset.content_sha256)
                                    for asset in snapshot.assets
                                    if asset.asset_id == node.asset_ref
                                ),
                                None,
                            )
                            if node.asset_ref is not None
                            else None
                        ),
                    }
                )
                if node.table_payload is not None:
                    native_tables.append(_scope_table_record(node))
                if node.asset_ref is not None:
                    selected_asset_ids.add(str(node.asset_ref))
            for asset in snapshot.assets:
                locator = asset.native_locator
                if asset.asset_id in selected_asset_ids or (
                    locator is not None
                    and locator.format.value == "pdf"
                    and locator.page_number in page_numbers
                ):
                    assets.append(_scope_asset_record(asset))
        if include_visual:
            for visual_snapshot in snapshot.visual_text_snapshots:
                if visual_snapshot.page_number not in page_numbers:
                    continue
                visual_observations.extend(
                    _scope_visual_observation_record(visual_snapshot, observation)
                    for observation in visual_snapshot.observations
                )
            visual_tables.extend(
                _scope_visual_table_record(visual_table)
                for visual_table in snapshot.visual_table_snapshots
                if visual_table.page_number in page_numbers
            )
    else:
        ranges_by_sheet = _xlsx_scope_ranges(selector)
        for node in snapshot.nodes:
            locator = node.native_locator
            if locator.format.value != "xlsx":
                continue
            if node.table_payload is not None:
                selected_cells = [
                    cell
                    for cell in node.table_payload.cells
                    if _xlsx_cell_in_scope(
                        cell,
                        selector=selector,
                        ranges_by_sheet=ranges_by_sheet,
                    )
                ]
                if selector["mode"] == "RANGES" and not selected_cells:
                    continue
                native_tables.append(
                    _scope_table_record(node, selected_cells=selected_cells)
                )
            elif selector["mode"] == "RANGES":
                cell_range = locator.cell_range
                if (
                    locator.sheet is None
                    or cell_range is None
                    or not any(
                        _bounds_overlap(
                            _parse_a1_range(cell_range),
                            target_bounds,
                        )
                        for target_bounds in ranges_by_sheet.get(locator.sheet, [])
                    )
                ):
                    continue
            native_nodes.append(
                {
                    "kind": node.kind.value,
                    "native_locator": _scope_native_locator_record(locator),
                    "raw_sha256": _sha256_text(str(node.raw_text)),
                    "asset_content_sha256": (
                        next(
                            (
                                str(asset.content_sha256)
                                for asset in snapshot.assets
                                if asset.asset_id == node.asset_ref
                            ),
                            None,
                        )
                        if node.asset_ref is not None
                        else None
                    ),
                }
            )
            if node.asset_ref is not None:
                selected_asset_ids.add(str(node.asset_ref))
        assets.extend(
            _scope_asset_record(asset)
            for asset in snapshot.assets
            if selector["mode"] == "WORKBOOK" or asset.asset_id in selected_asset_ids
        )

    native_nodes.sort(
        key=lambda record: _canonical_json_payload(record["native_locator"])
    )
    native_tables.sort(
        key=lambda record: _canonical_json_payload(record["native_locator"])
    )
    assets.sort(key=_canonical_json_payload)
    visual_observations.sort(
        key=lambda record: (
            int(record["page_number"]),
            str(record["provider_id"]),
            int(record["observation_index"]),
        )
    )
    visual_tables.sort(
        key=lambda record: (
            int(record["page_number"]),
            int(record["table_index"]),
            str(record["provider_id"]),
        )
    )
    content_payload = {
        "document_id": str(expected_scope["document_id"]),
        "selector": selector,
        "native_nodes": native_nodes,
        "native_tables": native_tables,
        "assets": assets,
        "visual_observations": visual_observations,
        "visual_tables": visual_tables,
    }
    cell_count = (
        sum(int(table["selected_cell_count"]) for table in native_tables)
        + sum(
            len(
                observation["layout_table"]["cells"]
                if observation["layout_table"] is not None
                else ()
            )
            for observation in visual_observations
        )
        + sum(len(table["cells"]) for table in visual_tables)
    )
    actual_scope = {
        "document_id": str(expected_scope["document_id"]),
        "selector": selector,
        "expected_native_node_count": len(native_nodes),
        "expected_native_table_count": len(native_tables),
        "expected_asset_count": len(assets),
        "expected_visual_observation_count": len(visual_observations),
        "expected_visual_table_count": len(visual_tables),
        "expected_cell_count": cell_count,
        "scope_fingerprint": _sha256_text(_canonical_json_payload(content_payload)),
    }
    return actual_scope, content_payload


def _measure_source_scope_group(
    *,
    oracle: dict[str, Any],
    document_results: dict[str, dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
) -> dict[str, Any]:
    fact = oracle["fact"]
    expected_scopes = list(fact["scopes"])
    source_hashes = _oracle_source_sha256_by_document(oracle)
    actual_scopes: list[dict[str, Any]] = []
    mismatch_count = 0
    source_ir_ids: list[str] = []
    for expected_scope in expected_scopes:
        document_id = str(expected_scope["document_id"])
        result = document_results.get(document_id)
        snapshot = (
            _source_ir_snapshot_for_document_result(
                result,
                source_ir_snapshots=source_ir_snapshots,
            )
            if result is not None
            else None
        )
        if snapshot is None or snapshot.source_sha256 != source_hashes.get(document_id):
            mismatch_count += 1
            continue
        source_ir_ids.append(snapshot.source_ir_id)
        actual_scope, _ = _measure_source_scope_snapshot(snapshot, expected_scope)
        actual_scopes.append(actual_scope)
        mismatch_count += int(actual_scope != expected_scope)
    actual_scopes.sort(key=lambda scope: str(scope["document_id"]))
    observed_group_fingerprint = _fingerprint_source_scope_group(actual_scopes)
    mismatch_count += int(observed_group_fingerprint != str(fact["group_fingerprint"]))
    count_fields = (
        "native_node_count",
        "native_table_count",
        "asset_count",
        "visual_observation_count",
        "visual_table_count",
        "cell_count",
    )
    result: dict[str, Any] = {
        "matches": bool(
            len(actual_scopes) == int(fact["expected_document_count"])
            and mismatch_count == 0
        ),
        "expected_primary_count": int(fact["expected_document_count"]),
        "observed_primary_count": len(actual_scopes),
        "mismatch_count": mismatch_count,
        "expected_fingerprint": str(fact["group_fingerprint"]),
        "observed_fingerprint": observed_group_fingerprint,
        "distinct_source_ir_count": len(set(source_ir_ids)),
    }
    for field in count_fields:
        fact_field = f"expected_{field}"
        result[f"expected_{field}"] = sum(
            int(scope[fact_field]) for scope in expected_scopes
        )
        result[f"observed_{field}"] = sum(
            int(scope[fact_field]) for scope in actual_scopes
        )
    return result


def _measure_xlsx_cell_range(snapshot: Any, fact: dict[str, Any]) -> dict[str, Any]:
    locator = fact["locator"]
    target_bounds = _parse_a1_range(str(locator["cell_range"]))
    candidates = []
    for node, table in _source_ir_tables(snapshot):
        native = node.native_locator
        if (
            native.format.value == "xlsx"
            and native.workbook == locator["workbook"]
            and native.sheet == locator["sheet"]
            and native.cell_range is not None
            and _bounds_contain(_parse_a1_range(native.cell_range), target_bounds)
        ):
            candidates.append(table)
    records: list[dict[str, Any]] = []
    if len(candidates) == 1:
        for cell in candidates[0].cells:
            native = cell.native_locator
            if (
                native.format.value == "xlsx"
                and native.workbook == locator["workbook"]
                and native.sheet == locator["sheet"]
                and native.cell_range is not None
                and _bounds_contain(target_bounds, _parse_a1_range(native.cell_range))
            ):
                records.append(
                    {
                        "cell_range": native.cell_range,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                        "raw_sha256": _sha256_text(cell.raw_text),
                    }
                )
    expected_records = list(fact["cells"])
    actual_fingerprint = _fingerprint_xlsx_cell_records(records)
    mismatch_count = _record_mismatch_count(
        expected_records,
        records,
        key=lambda item: str(item["cell_range"]),
    ) + (0 if len(candidates) == 1 else 1)
    matches = bool(
        len(candidates) == 1
        and len(records) == int(fact["expected_cell_count"])
        and mismatch_count == 0
        and actual_fingerprint == fact["range_fingerprint"]
    )
    return {
        "matches": matches,
        "expected_primary_count": int(fact["expected_cell_count"]),
        "observed_primary_count": len(records),
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["range_fingerprint"],
        "observed_fingerprint": actual_fingerprint,
    }


def _measure_docx_table_group(snapshot: Any, fact: dict[str, Any]) -> dict[str, Any]:
    tables_by_locator: dict[tuple[int, int], list[Any]] = {}
    for node, table in _source_ir_tables(snapshot):
        native = node.native_locator
        if (
            native.format.value == "docx"
            and native.body_element_index is not None
            and native.table_index is not None
        ):
            tables_by_locator.setdefault(
                (native.body_element_index, native.table_index),
                [],
            ).append(table)
    records: list[dict[str, Any]] = []
    ambiguous_count = 0
    for expected in fact["tables"]:
        locator = expected["locator"]
        key = (int(locator["body_element_index"]), int(locator["table_index"]))
        candidates = tables_by_locator.get(key, [])
        if len(candidates) != 1:
            ambiguous_count += 1
            continue
        table = candidates[0]
        grid_payload = _canonical_json_payload(
            [
                {
                    "row": cell.row,
                    "column": cell.column,
                    "row_span": cell.row_span,
                    "column_span": cell.column_span,
                    "raw_text": cell.raw_text,
                }
                for cell in sorted(
                    table.cells, key=lambda item: (item.row, item.column)
                )
            ]
        )
        records.append(
            {
                "locator": {
                    "format": "docx",
                    "body_element_index": key[0],
                    "table_index": key[1],
                },
                "row_count": table.row_count,
                "column_count": table.column_count,
                "cell_count": len(table.cells),
                "data_row_count": max(0, table.row_count - 1),
                "grid_sha256": _sha256_text(grid_payload),
            }
        )
    expected_records = list(fact["tables"])
    actual_fingerprint = _fingerprint_docx_table_records(records)
    mismatch_count = ambiguous_count + _record_mismatch_count(
        expected_records,
        records,
        key=lambda item: (
            int(item["locator"]["body_element_index"]),
            int(item["locator"]["table_index"]),
        ),
    )
    observed_data_rows = sum(int(record["data_row_count"]) for record in records)
    observed_cells = sum(int(record["cell_count"]) for record in records)
    matches = bool(
        len(records) == int(fact["expected_table_count"])
        and observed_data_rows == int(fact["expected_data_row_count"])
        and observed_cells == int(fact["expected_cell_count"])
        and mismatch_count == 0
        and actual_fingerprint == fact["group_fingerprint"]
    )
    return {
        "matches": matches,
        "expected_primary_count": int(fact["expected_table_count"]),
        "observed_primary_count": len(records),
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["group_fingerprint"],
        "observed_fingerprint": actual_fingerprint,
    }


def _measure_docx_mixed_content_group(
    snapshot: Any,
    fact: dict[str, Any],
    *,
    parameter_table_fact: dict[str, Any],
) -> dict[str, Any]:
    nodes_by_locator: dict[tuple[int, str], list[Any]] = {}
    for node in snapshot.nodes:
        locator = node.native_locator
        if (
            locator.format.value != "docx"
            or locator.body_element_index is None
            or locator.table_index is not None
        ):
            continue
        nodes_by_locator.setdefault(
            (int(locator.body_element_index), node.kind.value),
            [],
        ).append(node)

    narrative_records: list[dict[str, Any]] = []
    ambiguous_narrative_count = 0
    for expected in fact["narratives"]:
        key = (
            int(expected["locator"]["body_element_index"]),
            str(expected["kind"]),
        )
        candidates = nodes_by_locator.get(key, [])
        if len(candidates) != 1:
            ambiguous_narrative_count += 1
            continue
        node = candidates[0]
        narrative_records.append(
            {
                "locator": {
                    "format": "docx",
                    "body_element_index": key[0],
                },
                "kind": node.kind.value,
                "raw_sha256": _sha256_text(str(node.raw_text)),
            }
        )
    narrative_records.sort(
        key=lambda record: (
            int(record["locator"]["body_element_index"]),
            str(record["kind"]),
        )
    )
    narrative_mismatch_count = ambiguous_narrative_count + _record_mismatch_count(
        list(fact["narratives"]),
        narrative_records,
        key=lambda item: (
            int(item["locator"]["body_element_index"]),
            str(item["kind"]),
        ),
    )
    table_measurement = _measure_docx_table_group(
        snapshot,
        parameter_table_fact,
    )
    observed_fingerprint = _fingerprint_docx_mixed_content_group(
        narratives=narrative_records,
        parameter_table_oracle_id=str(fact["parameter_table_oracle_id"]),
        expected_parameter_table_count=int(
            table_measurement["observed_primary_count"]
        ),
        parameter_table_group_fingerprint=str(
            table_measurement["observed_fingerprint"]
        ),
    )
    expected_narrative_count = int(fact["expected_narrative_count"])
    expected_parameter_table_count = int(fact["expected_parameter_table_count"])
    mismatch_count = (
        narrative_mismatch_count
        + int(table_measurement["mismatch_count"])
        + int(observed_fingerprint != str(fact["mixed_fingerprint"]))
    )
    matches = bool(
        len(narrative_records) == expected_narrative_count
        and table_measurement["matches"]
        and int(table_measurement["observed_primary_count"])
        == expected_parameter_table_count
        and mismatch_count == 0
    )
    return {
        "matches": matches,
        "expected_primary_count": (
            expected_narrative_count + expected_parameter_table_count
        ),
        "observed_primary_count": (
            len(narrative_records)
            + int(table_measurement["observed_primary_count"])
        ),
        "mismatch_count": mismatch_count,
        "expected_fingerprint": str(fact["mixed_fingerprint"]),
        "observed_fingerprint": observed_fingerprint,
        "expected_narrative_count": expected_narrative_count,
        "observed_narrative_count": len(narrative_records),
        "expected_parameter_table_count": expected_parameter_table_count,
        "observed_parameter_table_count": int(
            table_measurement["observed_primary_count"]
        ),
    }


def _measure_docx_table_row_scope(
    snapshot: Any,
    fact: dict[str, Any],
) -> dict[str, Any]:
    locator = fact["locator"]
    table_key = (
        int(locator["body_element_index"]),
        int(locator["table_index"]),
    )
    row_index = int(locator["row_index"])
    candidates = [
        table
        for node, table in _source_ir_tables(snapshot)
        if node.native_locator.format.value == "docx"
        and node.native_locator.body_element_index == table_key[0]
        and node.native_locator.table_index == table_key[1]
    ]
    records: list[dict[str, Any]] = []
    locator_mismatch_count = 0
    observed_row_count = 0
    if len(candidates) == 1 and 0 <= row_index < candidates[0].row_count:
        observed_row_count = 1
        table = candidates[0]
        for cell in sorted(
            (cell for cell in table.cells if cell.row == row_index),
            key=lambda cell: (cell.column, cell.row_span, cell.column_span),
        ):
            native = cell.native_locator
            if (
                native.format.value != "docx"
                or native.body_element_index != table_key[0]
                or native.table_index != table_key[1]
                or native.row_index != row_index
                or native.column_index != cell.column
            ):
                locator_mismatch_count += 1
            records.append(
                {
                    "column": int(cell.column),
                    "row_span": int(cell.row_span),
                    "column_span": int(cell.column_span),
                    "raw_sha256": _sha256_text(str(cell.raw_text)),
                }
            )

    row_fingerprint = _fingerprint_docx_table_row_cells(records)
    logical_slot_count = sum(int(cell["column_span"]) for cell in records)
    actual_scope = {
        "locator": dict(locator),
        "anchor_cell_count": len(records),
        "logical_slot_count": logical_slot_count,
        "row_fingerprint": row_fingerprint,
    }
    scope_fingerprint = _fingerprint_docx_table_row_scope(actual_scope)
    mismatch_count = (
        abs(len(candidates) - 1)
        + (0 if observed_row_count else 1)
        + locator_mismatch_count
        + _record_mismatch_count(
            list(fact["cells"]),
            records,
            key=lambda item: int(item["column"]),
        )
    )
    matches = bool(
        len(candidates) == 1
        and observed_row_count == 1
        and len(records) == int(fact["anchor_cell_count"])
        and logical_slot_count == int(fact["logical_slot_count"])
        and mismatch_count == 0
        and row_fingerprint == fact["row_fingerprint"]
        and scope_fingerprint == fact["scope_fingerprint"]
    )
    return {
        "matches": matches,
        "expected_primary_count": 1,
        "observed_primary_count": observed_row_count,
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["scope_fingerprint"],
        "observed_fingerprint": scope_fingerprint,
        "expected_logical_slot_count": int(fact["logical_slot_count"]),
        "observed_logical_slot_count": logical_slot_count,
        "expected_cell_count": int(fact["anchor_cell_count"]),
        "observed_cell_count": len(records),
    }


def _measure_pdf_table_continuation_group(
    snapshot: Any,
    fact: dict[str, Any],
) -> dict[str, Any]:
    tables_by_locator: dict[tuple[int, int], list[Any]] = {}
    for node, table in _source_ir_tables(snapshot):
        native = node.native_locator
        if (
            native.format.value == "pdf"
            and native.page_number is not None
            and native.table_index is not None
        ):
            tables_by_locator.setdefault(
                (native.page_number, native.table_index),
                [],
            ).append((node, table))

    records: list[dict[str, Any]] = []
    ambiguous_count = 0
    locator_mismatch_count = 0
    for expected in fact["segments"]:
        locator = expected["locator"]
        key = (int(locator["page_number"]), int(locator["table_index"]))
        candidates = tables_by_locator.get(key, [])
        if len(candidates) != 1:
            ambiguous_count += 1
            continue
        _, table = candidates[0]
        row_records: list[dict[str, Any]] = []
        for row_index in range(table.row_count):
            cells = sorted(
                (cell for cell in table.cells if cell.row == row_index),
                key=lambda cell: cell.column,
            )
            cell_records: list[dict[str, Any]] = []
            for cell in cells:
                native = cell.native_locator
                if (
                    native.format.value != "pdf"
                    or native.page_number != key[0]
                    or native.table_index != key[1]
                    or native.row_index != cell.row
                    or native.column_index != cell.column
                ):
                    locator_mismatch_count += 1
                cell_records.append(
                    {
                        "column": cell.column,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                        "raw_sha256": _sha256_text(cell.raw_text),
                    }
                )
            row_records.append(
                {
                    "row_index": row_index,
                    "cell_count": len(cells),
                    "row_fingerprint": _sha256_text(
                        _canonical_json_payload(cell_records)
                    ),
                }
            )
        records.append(
            {
                "locator": {
                    "format": "pdf",
                    "page_number": key[0],
                    "table_index": key[1],
                },
                "row_count": table.row_count,
                "column_count": table.column_count,
                "cell_count": len(table.cells),
                "merged_region_count": len(table.merged_regions),
                "rows": row_records,
            }
        )

    expected_records = list(fact["segments"])
    actual_fingerprint = _fingerprint_pdf_table_segment_records(records)
    mismatch_count = (
        ambiguous_count
        + locator_mismatch_count
        + _record_mismatch_count(
            expected_records,
            records,
            key=lambda item: (
                int(item["locator"]["page_number"]),
                int(item["locator"]["table_index"]),
            ),
        )
    )
    observed_rows = sum(int(record["row_count"]) for record in records)
    observed_cells = sum(int(record["cell_count"]) for record in records)
    matches = bool(
        len(records) == int(fact["expected_segment_count"])
        and observed_rows == int(fact["expected_physical_row_count"])
        and observed_cells == int(fact["expected_cell_count"])
        and mismatch_count == 0
        and actual_fingerprint == fact["group_fingerprint"]
    )
    return {
        "matches": matches,
        "expected_primary_count": int(fact["expected_physical_row_count"]),
        "observed_primary_count": observed_rows,
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["group_fingerprint"],
        "observed_fingerprint": actual_fingerprint,
    }


def _measure_pdf_table_continuation_logical_grid(
    snapshot: Any,
    fact: dict[str, Any],
) -> dict[str, Any]:
    """Compare table content in a representation-neutral logical grid.

    Covered slots are canonicalized as empty while their anchor retains the
    text.  Legacy facts can therefore retain their reviewed row hashes, while
    v2 facts additionally bind the exact visible merge topology without using
    an implementation-specific anchor-cell count as Gold.
    """

    logical_grid_v2 = fact["kind"] == "pdf_table_continuation_logical_grid"
    tables_by_locator: dict[tuple[int, int], list[Any]] = {}
    for node, table in _source_ir_tables(snapshot):
        native = node.native_locator
        if (
            native.format.value == "pdf"
            and native.page_number is not None
            and native.table_index is not None
        ):
            tables_by_locator.setdefault(
                (native.page_number, native.table_index),
                [],
            ).append((node, table))

    records: list[dict[str, Any]] = []
    ambiguous_count = 0
    locator_mismatch_count = 0
    coverage_mismatch_count = 0
    observed_anchor_cell_count = 0
    observed_merged_region_count = 0
    observed_logical_grid_cell_count = 0
    merge_topology_mismatch_count = 0
    for expected in fact["segments"]:
        locator = expected["locator"]
        key = (int(locator["page_number"]), int(locator["table_index"]))
        candidates = tables_by_locator.get(key, [])
        if len(candidates) != 1:
            ambiguous_count += 1
            continue
        _, table = candidates[0]
        observed_anchor_cell_count += len(table.cells)
        observed_merged_region_count += len(table.merged_regions)
        merged_regions = [
            {
                "row": region.row,
                "column": region.column,
                "row_span": region.row_span,
                "column_span": region.column_span,
            }
            for region in sorted(
                table.merged_regions,
                key=lambda item: (
                    item.row,
                    item.column,
                    item.row_span,
                    item.column_span,
                ),
            )
        ]
        if logical_grid_v2 and merged_regions != list(expected["merged_regions"]):
            merge_topology_mismatch_count += 1

        coverage: dict[tuple[int, int], list[Any]] = {}
        for cell in table.cells:
            native = cell.native_locator
            if (
                native.format.value != "pdf"
                or native.page_number != key[0]
                or native.table_index != key[1]
                or native.row_index != cell.row
                or native.column_index != cell.column
            ):
                locator_mismatch_count += 1
            if (
                cell.row < 0
                or cell.column < 0
                or cell.row_span < 1
                or cell.column_span < 1
                or cell.row + cell.row_span > table.row_count
                or cell.column + cell.column_span > table.column_count
            ):
                coverage_mismatch_count += 1
                continue
            for row_index in range(cell.row, cell.row + cell.row_span):
                for column_index in range(
                    cell.column,
                    cell.column + cell.column_span,
                ):
                    coverage.setdefault((row_index, column_index), []).append(cell)

        row_records: list[dict[str, Any]] = []
        for row_index in range(table.row_count):
            cell_records: list[dict[str, Any]] = []
            for column_index in range(table.column_count):
                covering_cells = coverage.get((row_index, column_index), [])
                if len(covering_cells) != 1:
                    coverage_mismatch_count += 1
                    raw_text = ""
                else:
                    anchor = covering_cells[0]
                    raw_text = (
                        anchor.raw_text
                        if anchor.row == row_index and anchor.column == column_index
                        else ""
                    )
                cell_records.append(
                    {
                        "column": column_index,
                        "row_span": 1,
                        "column_span": 1,
                        "raw_sha256": _sha256_text(raw_text),
                    }
                )
            observed_logical_grid_cell_count += len(cell_records)
            row_record = {
                "row_index": row_index,
                "row_fingerprint": _sha256_text(_canonical_json_payload(cell_records)),
            }
            if logical_grid_v2:
                row_record["logical_slot_count"] = len(cell_records)
            else:
                row_record["cell_count"] = len(cell_records)
            row_records.append(row_record)
        segment_record = {
            "locator": {
                "format": "pdf",
                "page_number": key[0],
                "table_index": key[1],
            },
            "row_count": table.row_count,
            "column_count": table.column_count,
            "rows": row_records,
        }
        if logical_grid_v2:
            segment_record.update(
                {
                    "logical_slot_count": table.row_count * table.column_count,
                    "merged_regions": merged_regions,
                }
            )
        else:
            segment_record.update(
                {
                    "cell_count": table.row_count * table.column_count,
                    "merged_region_count": 0,
                }
            )
        records.append(segment_record)

    expected_records = list(fact["segments"])
    actual_fingerprint = _fingerprint_pdf_table_segment_records(records)
    mismatch_count = (
        ambiguous_count
        + locator_mismatch_count
        + coverage_mismatch_count
        + _record_mismatch_count(
            expected_records,
            records,
            key=lambda item: (
                int(item["locator"]["page_number"]),
                int(item["locator"]["table_index"]),
            ),
        )
    )
    observed_rows = sum(int(record["row_count"]) for record in records)
    matches = bool(
        len(records) == int(fact["expected_segment_count"])
        and observed_rows == _pdf_continuation_expected_row_count(fact)
        and observed_logical_grid_cell_count
        == _pdf_continuation_expected_logical_slot_count(fact)
        and mismatch_count == 0
        and actual_fingerprint == fact["group_fingerprint"]
    )
    expected_merge_region_count = sum(
        len(segment.get("merged_regions") or []) for segment in fact["segments"]
    )
    return {
        "matches": matches,
        "expected_primary_count": _pdf_continuation_expected_row_count(fact),
        "observed_primary_count": observed_rows,
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["group_fingerprint"],
        "observed_fingerprint": actual_fingerprint,
        "observed_logical_grid_cell_count": observed_logical_grid_cell_count,
        "expected_logical_slot_count": (
            _pdf_continuation_expected_logical_slot_count(fact)
        ),
        "observed_logical_slot_count": observed_logical_grid_cell_count,
        "observed_anchor_cell_count": observed_anchor_cell_count,
        "expected_merge_region_count": expected_merge_region_count,
        "observed_merged_region_count": observed_merged_region_count,
        "merge_topology_mismatch_count": merge_topology_mismatch_count,
    }


def _measure_pdf_table_note_label_group(
    snapshot: Any,
    fact: dict[str, Any],
) -> dict[str, Any]:
    table_locator = fact["table"]["locator"]
    table_key = (
        int(table_locator["page_number"]),
        int(table_locator["table_index"]),
    )
    table_candidates = [
        (node, table)
        for node, table in _source_ir_tables(snapshot)
        if node.native_locator.format.value == "pdf"
        and node.native_locator.page_number == table_key[0]
        and node.native_locator.table_index == table_key[1]
    ]
    actual_table: dict[str, Any] | None = None
    locator_mismatch_count = 0
    if len(table_candidates) == 1:
        node, table = table_candidates[0]
        rows: list[dict[str, Any]] = []
        for row_index in range(table.row_count):
            cells = sorted(
                (cell for cell in table.cells if cell.row == row_index),
                key=lambda cell: cell.column,
            )
            cell_records: list[dict[str, Any]] = []
            for cell in cells:
                native = cell.native_locator
                if (
                    native.format.value != "pdf"
                    or native.page_number != table_key[0]
                    or native.table_index != table_key[1]
                    or native.row_index != cell.row
                    or native.column_index != cell.column
                ):
                    locator_mismatch_count += 1
                cell_records.append(
                    {
                        "column": cell.column,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                        "raw_sha256": _sha256_text(cell.raw_text),
                    }
                )
            rows.append(
                {
                    "row_index": row_index,
                    "cell_count": len(cells),
                    "row_fingerprint": _sha256_text(
                        _canonical_json_payload(cell_records)
                    ),
                }
            )
        actual_table = {
            "locator": {
                "format": "pdf",
                "page_number": table_key[0],
                "table_index": table_key[1],
            },
            "row_count": table.row_count,
            "column_count": table.column_count,
            "cell_count": len(table.cells),
            "merged_region_count": len(table.merged_regions),
            "rows": rows,
        }

    source_nodes_by_locator: dict[tuple[int, int, str], list[Any]] = {}
    cells_by_locator: dict[tuple[int, int, int, int, int, int], list[Any]] = {}
    for node in snapshot.nodes:
        native = node.native_locator
        if (
            native.format.value == "pdf"
            and native.page_number is not None
            and native.block_index is not None
        ):
            source_nodes_by_locator.setdefault(
                (native.page_number, native.block_index, node.kind.value),
                [],
            ).append(node)
        if node.table_payload is None:
            continue
        for cell in node.table_payload.cells:
            locator = cell.native_locator
            if (
                locator.format.value == "pdf"
                and locator.page_number is not None
                and locator.table_index is not None
                and locator.row_index is not None
                and locator.column_index is not None
            ):
                cells_by_locator.setdefault(
                    (
                        locator.page_number,
                        locator.table_index,
                        locator.row_index,
                        locator.column_index,
                        cell.row_span,
                        cell.column_span,
                    ),
                    [],
                ).append(cell)

    def measure_text_record(expected: dict[str, Any]) -> dict[str, Any] | None:
        locator = expected["locator"]
        if expected["kind"] == "source_node":
            candidates = source_nodes_by_locator.get(
                (
                    int(locator["page_number"]),
                    int(locator["block_index"]),
                    str(locator["source_node_kind"]),
                ),
                [],
            )
        else:
            candidates = cells_by_locator.get(
                (
                    int(locator["page_number"]),
                    int(locator["table_index"]),
                    int(locator["row_index"]),
                    int(locator["column_index"]),
                    int(locator["row_span"]),
                    int(locator["column_span"]),
                ),
                [],
            )
        if len(candidates) != 1:
            return None
        return {
            "kind": expected["kind"],
            "locator": dict(locator),
            "raw_sha256": _sha256_text(candidates[0].raw_text),
        }

    actual_labels = [
        measured
        for expected in fact["label_records"]
        if (measured := measure_text_record(expected)) is not None
    ]
    actual_labels_by_key = {
        _pdf_text_locator_record_key(record): record for record in actual_labels
    }
    actual_duplicate_captions = [
        actual_labels_by_key[key]
        for record in fact["duplicate_caption_records"]
        if (key := _pdf_text_locator_record_key(record)) in actual_labels_by_key
    ]
    actual_child = measure_text_record(fact["child_assertion"])
    expected_labels = list(fact["label_records"])
    label_fingerprint = _fingerprint_pdf_text_locator_records(actual_labels)
    expected_table = fact["table"]
    table_mismatch_count = int(actual_table != expected_table)
    label_mismatch_count = _record_mismatch_count(
        expected_labels,
        actual_labels,
        key=_pdf_text_locator_record_key,
    )
    child_mismatch_count = int(actual_child != fact["child_assertion"])
    actual_group_fingerprint = (
        _fingerprint_pdf_table_note_label_group(
            table=actual_table,
            note_row_index=int(fact["note_row_index"]),
            parameter_row_indices=fact["parameter_row_indices"],
            label_records=actual_labels,
            duplicate_caption_records=actual_duplicate_captions,
            child_assertion=actual_child,
        )
        if actual_table is not None and actual_child is not None
        else ""
    )
    mismatch_count = (
        locator_mismatch_count
        + table_mismatch_count
        + label_mismatch_count
        + child_mismatch_count
        + int(len(table_candidates) != 1)
    )
    matches = bool(
        len(table_candidates) == 1
        and len(actual_labels) == int(fact["expected_label_record_count"])
        and label_fingerprint == fact["label_fingerprint"]
        and actual_group_fingerprint == fact["group_fingerprint"]
        and mismatch_count == 0
    )
    return {
        "matches": matches,
        "expected_primary_count": int(fact["expected_label_record_count"]),
        "observed_primary_count": len(actual_labels),
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["group_fingerprint"],
        "observed_fingerprint": actual_group_fingerprint,
    }


def _pdf_option_state_from_text(raw_text: str) -> StructureOptionState | None:
    match = _PDF_OPTION_SQUARE_MARKER.search(raw_text)
    if match is None:
        match = _PDF_OPTION_PAREN_MARKER.search(raw_text)
    if match is None:
        match = _PDF_OPTION_BOX_MARKER.search(raw_text)
    if match is None:
        return None
    mark = re.sub(r"\s+", "", match.group("mark"))
    if mark in _PDF_CHECKED_OPTION_MARKS:
        return StructureOptionState.CHECKED
    if mark in _PDF_UNCHECKED_OPTION_MARKS:
        return StructureOptionState.UNCHECKED
    if any(character.isalnum() for character in mark):
        return None
    return StructureOptionState.UNCERTAIN


def _measure_pdf_option_state_group(
    snapshot: Any,
    fact: dict[str, Any],
) -> dict[str, Any]:
    page_number = int(fact["page_number"])
    actual_by_key: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
    for node in snapshot.nodes:
        native = node.native_locator
        if (
            native.format.value != "pdf"
            or native.page_number != page_number
            or native.block_index is None
        ):
            continue
        state = _pdf_option_state_from_text(node.raw_text)
        if state is None:
            continue
        record = {
            "locator": {
                "format": "pdf",
                "page_number": page_number,
                "block_index": native.block_index,
                "source_node_kind": node.kind.value,
            },
            "raw_sha256": _sha256_text(node.raw_text),
            "state": state.value,
        }
        actual_by_key.setdefault(
            _pdf_option_state_record_key(record),
            [],
        ).append(record)

    expected_keys = {
        _pdf_option_state_record_key(record)
        for section in fact["sections"]
        for record in section["options"]
    }
    actual_sections: list[dict[str, Any]] = []
    mismatch_count = sum(abs(len(records) - 1) for records in actual_by_key.values())
    for expected_section in fact["sections"]:
        expected_records = list(expected_section["options"])
        actual_records = [
            actual_by_key[key][0]
            for expected in expected_records
            if (
                (key := _pdf_option_state_record_key(expected)) in actual_by_key
                and len(actual_by_key[key]) == 1
            )
        ]
        mismatch_count += _record_mismatch_count(
            expected_records,
            actual_records,
            key=_pdf_option_state_record_key,
        )
        actual_sections.append(
            {
                "section_label": str(expected_section["section_label"]),
                "expected_option_count": len(actual_records),
                "options": actual_records,
                "option_fingerprint": _fingerprint_pdf_option_state_records(
                    actual_records
                ),
            }
        )
    actual_keys = set(actual_by_key)
    mismatch_count += len(actual_keys - expected_keys)
    actual_group_fingerprint = _fingerprint_pdf_option_state_group(
        page_number=page_number,
        sections=actual_sections,
    )
    observed_option_count = sum(len(records) for records in actual_by_key.values())
    matches = bool(
        observed_option_count == int(fact["expected_option_count"])
        and len(actual_sections) == int(fact["expected_section_count"])
        and mismatch_count == 0
        and actual_group_fingerprint == fact["group_fingerprint"]
    )
    return {
        "matches": matches,
        "expected_primary_count": int(fact["expected_option_count"]),
        "observed_primary_count": observed_option_count,
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["group_fingerprint"],
        "observed_fingerprint": actual_group_fingerprint,
    }


def _measure_xlsx_merge_group(snapshot: Any, fact: dict[str, Any]) -> dict[str, Any]:
    locator = fact["table_locator"]
    candidates = []
    for node, table in _source_ir_tables(snapshot):
        native = node.native_locator
        if (
            native.format.value == "xlsx"
            and native.workbook == locator["workbook"]
            and native.sheet == locator["sheet"]
            and native.cell_range == locator["cell_range"]
        ):
            candidates.append(table)
    merge_records: list[dict[str, Any]] = []
    identity_records: list[dict[str, Any]] = []
    if len(candidates) == 1:
        table = candidates[0]
        table_bounds = _parse_a1_range(str(locator["cell_range"]))
        scope_bounds = _parse_a1_range(str(fact["scope_range"]))
        for region in table.merged_regions:
            absolute_row = table_bounds[0] + region.row
            absolute_column = table_bounds[1] + region.column
            absolute_bounds = (
                absolute_row,
                absolute_column,
                absolute_row + region.row_span - 1,
                absolute_column + region.column_span - 1,
            )
            if (
                absolute_bounds[0] == scope_bounds[0]
                and absolute_bounds[2] == scope_bounds[2]
                and _bounds_contain(scope_bounds, absolute_bounds)
            ):
                anchor = next(
                    (
                        cell
                        for cell in table.cells
                        if cell.row == region.row and cell.column == region.column
                    ),
                    None,
                )
                if anchor is None:
                    continue
                merge_records.append(
                    {
                        "anchor": f"{_column_name(absolute_column)}{absolute_row}",
                        "row_span": region.row_span,
                        "column_span": region.column_span,
                        "anchor_raw_sha256": _sha256_text(anchor.raw_text),
                    }
                )
        identity_bounds = _parse_a1_range(str(fact["row_identity"]["cell_range"]))
        for cell in table.cells:
            native = cell.native_locator
            if (
                native.format.value == "xlsx"
                and native.sheet == locator["sheet"]
                and native.cell_range is not None
                and _bounds_contain(identity_bounds, _parse_a1_range(native.cell_range))
            ):
                identity_records.append(
                    {
                        "cell_range": native.cell_range,
                        "raw_sha256": _sha256_text(cell.raw_text),
                    }
                )
    actual_merge_fingerprint = _fingerprint_xlsx_merge_records(merge_records)
    actual_identity_fingerprint = _fingerprint_xlsx_identity_records(identity_records)
    actual_group_fingerprint = _fingerprint_xlsx_merge_group(
        merge_records,
        identity_records,
    )
    mismatch_count = (
        (0 if len(candidates) == 1 else 1)
        + _record_mismatch_count(
            list(fact["merges"]),
            merge_records,
            key=lambda item: str(item["anchor"]),
        )
        + _record_mismatch_count(
            list(fact["row_identity"]["cells"]),
            identity_records,
            key=lambda item: str(item["cell_range"]),
        )
    )
    matches = bool(
        len(candidates) == 1
        and len(merge_records) == int(fact["expected_merge_count"])
        and len(identity_records) == int(fact["row_identity"]["expected_cell_count"])
        and mismatch_count == 0
        and actual_merge_fingerprint == fact["merge_fingerprint"]
        and actual_identity_fingerprint == fact["row_identity"]["fingerprint"]
        and actual_group_fingerprint == fact["group_fingerprint"]
    )
    return {
        "matches": matches,
        "expected_primary_count": int(fact["expected_merge_count"]),
        "observed_primary_count": len(merge_records),
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["group_fingerprint"],
        "observed_fingerprint": actual_group_fingerprint,
    }


def _measure_xlsx_material_requirement_scope(
    snapshot: Any,
    fact: dict[str, Any],
) -> dict[str, Any]:
    locator = fact["table_locator"]
    candidates = [
        table
        for node, table in _source_ir_tables(snapshot)
        if (
            node.native_locator.format.value == "xlsx"
            and node.native_locator.workbook == locator["workbook"]
            and node.native_locator.sheet == locator["sheet"]
            and node.native_locator.cell_range == locator["cell_range"]
        )
    ]
    row_records: list[dict[str, Any]] = []
    role_records: list[dict[str, Any]] = []
    if len(candidates) == 1:
        table = candidates[0]
        table_bounds = _parse_a1_range(str(locator["cell_range"]))
        cells_by_range = {
            str(cell.native_locator.cell_range): cell
            for cell in table.cells
            if cell.native_locator.cell_range is not None
        }
        regions_by_anchor = {
            (
                table_bounds[0] + region.row,
                table_bounds[1] + region.column,
            ): region
            for region in table.merged_regions
        }
        for expected in fact["row_expectations"]:
            cell_range = str(expected["identity_cell_range"])
            cell = cells_by_range.get(cell_range)
            if cell is None:
                continue
            row_records.append(
                {
                    "identity_cell_range": cell_range,
                    "identity_raw_sha256": _sha256_text(cell.raw_text),
                }
            )
        for expected in fact["role_bindings"]:
            header_range = str(expected["header_cell_range"])
            anchor_range = str(expected["anchor_cell_range"])
            header = cells_by_range.get(header_range)
            anchor = cells_by_range.get(anchor_range)
            anchor_bounds = _parse_a1_range(anchor_range)
            region = regions_by_anchor.get(
                (anchor_bounds[0], anchor_bounds[1])
            )
            if header is None or anchor is None or region is None:
                continue
            merge_range = (
                f"{_column_name(anchor_bounds[1])}{anchor_bounds[0]}:"
                f"{_column_name(anchor_bounds[1] + region.column_span - 1)}"
                f"{anchor_bounds[0] + region.row_span - 1}"
            )
            record = {
                "role": str(expected["role"]),
                "header_cell_range": header_range,
                "header_raw_sha256": _sha256_text(header.raw_text),
                "anchor_cell_range": anchor_range,
                "anchor_raw_sha256": _sha256_text(anchor.raw_text),
                "merge_range": merge_range,
                "expected_applies_to_count": int(region.row_span),
                "rendered_page_number": int(
                    expected["rendered_page_number"]
                ),
            }
            record["binding_fingerprint"] = (
                _fingerprint_xlsx_material_role_binding(record)
            )
            role_records.append(record)
    role_records.sort(
        key=lambda binding: _XLSX_MATERIAL_ROLE_ORDER[str(binding["role"])]
    )
    row_records.sort(
        key=lambda record: _cell_sort_key(record, "identity_cell_range")
    )
    actual_fact = {
        "kind": "xlsx_material_requirement_scope",
        "source_oracle_id": str(fact["source_oracle_id"]),
        "source_merge_group_fingerprint": str(
            fact["source_merge_group_fingerprint"]
        ),
        "table_locator": dict(fact["table_locator"]),
        "full_row_scope": str(fact["full_row_scope"]),
        "identity_rendered_page_number": int(
            fact["identity_rendered_page_number"]
        ),
        "expected_requirement_count": len(row_records),
        "expected_role_applies_to_count": sum(
            int(record["expected_applies_to_count"])
            for record in role_records
        ),
        "row_expectations": row_records,
        "role_bindings": role_records,
    }
    actual_scope_fingerprint = _fingerprint_xlsx_material_requirement_scope(
        actual_fact
    )
    mismatch_count = (
        (0 if len(candidates) == 1 else 1)
        + _record_mismatch_count(
            list(fact["row_expectations"]),
            row_records,
            key=lambda item: str(item["identity_cell_range"]),
        )
        + _record_mismatch_count(
            list(fact["role_bindings"]),
            role_records,
            key=lambda item: str(item["role"]),
        )
    )
    matches = bool(
        len(candidates) == 1
        and len(row_records) == int(fact["expected_requirement_count"])
        and len(role_records) == len(fact["role_bindings"])
        and actual_fact["expected_role_applies_to_count"]
        == int(fact["expected_role_applies_to_count"])
        and mismatch_count == 0
        and actual_scope_fingerprint == fact["scope_fingerprint"]
    )
    return {
        "matches": matches,
        "expected_primary_count": int(fact["expected_requirement_count"]),
        "observed_primary_count": len(row_records),
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["scope_fingerprint"],
        "observed_fingerprint": actual_scope_fingerprint,
    }


def _measure_xlsx_explicit_peer_scope_group(
    snapshot: Any,
    fact: dict[str, Any],
) -> dict[str, Any]:
    tables_by_locator: dict[tuple[str, str, str], list[tuple[Any, Any]]] = {}
    for node, table in _source_ir_tables(snapshot):
        native = node.native_locator
        if (
            native.format.value == "xlsx"
            and native.workbook is not None
            and native.sheet is not None
            and native.cell_range is not None
        ):
            tables_by_locator.setdefault(
                (native.workbook, native.sheet, native.cell_range),
                [],
            ).append((node, table))

    actual_scopes: list[dict[str, Any]] = []
    ambiguous_scope_count = 0
    locator_mismatch_count = 0
    for expected_scope in fact["scopes"]:
        locator = expected_scope["table_locator"]
        locator_key = (
            str(locator["workbook"]),
            str(locator["sheet"]),
            str(locator["cell_range"]),
        )
        table_candidates = tables_by_locator.get(locator_key, [])
        if len(table_candidates) != 1:
            ambiguous_scope_count += 1
            continue
        table_node, table = table_candidates[0]
        target_cell_range = str(expected_scope["cell_range"])
        cell_candidates = [
            cell
            for cell in table.cells
            if cell.native_locator.cell_range == target_cell_range
        ]
        if len(cell_candidates) != 1:
            ambiguous_scope_count += 1
            continue
        cell = cell_candidates[0]
        native = cell.native_locator
        if (
            native.format.value != "xlsx"
            or native.workbook != locator_key[0]
            or native.sheet != locator_key[1]
            or native.cell_range != target_cell_range
        ):
            locator_mismatch_count += 1
        parsed_items = parse_explicit_numeric_peer_items(cell.raw_text)
        item_records = [
            {
                "ordinal": int(item.ordinal),
                "start_offset": int(item.start_offset),
                "end_offset": int(item.end_offset),
                "raw_sha256": _sha256_text(
                    cell.raw_text[item.start_offset : item.end_offset]
                ),
            }
            for item in parsed_items
        ]
        actual_scope = {
            "table_locator": dict(locator),
            "table_source_node_id": str(table_node.source_node_id),
            "cell_range": target_cell_range,
            "cell_id": str(cell.cell_id),
            "raw_sha256": _sha256_text(cell.raw_text),
            "raw_text_length": len(cell.raw_text),
            "rendered_page_number": int(
                expected_scope["rendered_page_number"]
            ),
            "reviewed_sheet_part": str(
                expected_scope["reviewed_sheet_part"]
            ),
            "reviewed_sheet_part_sha256": str(
                expected_scope["reviewed_sheet_part_sha256"]
            ),
            "items": item_records,
        }
        actual_scope["scope_fingerprint"] = (
            _fingerprint_xlsx_explicit_peer_scope(actual_scope)
        )
        actual_scopes.append(actual_scope)

    actual_fact = {
        "kind": "xlsx_explicit_peer_scope_group",
        "expected_scope_count": len(actual_scopes),
        "expected_items_per_scope": int(fact["expected_items_per_scope"]),
        "expected_total_item_count": sum(
            len(scope["items"]) for scope in actual_scopes
        ),
        "expected_peer_ordinals": [
            int(value) for value in fact["expected_peer_ordinals"]
        ],
        "scopes": actual_scopes,
    }
    actual_group_fingerprint = (
        _fingerprint_xlsx_explicit_peer_scope_group(actual_fact)
    )
    mismatch_count = (
        ambiguous_scope_count
        + locator_mismatch_count
        + _record_mismatch_count(
            list(fact["scopes"]),
            actual_scopes,
            key=lambda scope: (
                str(scope["table_locator"]["workbook"]),
                str(scope["table_locator"]["sheet"]),
                str(scope["cell_range"]),
            ),
        )
    )
    observed_item_count = int(actual_fact["expected_total_item_count"])
    matches = bool(
        len(actual_scopes) == int(fact["expected_scope_count"])
        and all(
            len(scope["items"]) == int(fact["expected_items_per_scope"])
            for scope in actual_scopes
        )
        and observed_item_count == int(fact["expected_total_item_count"])
        and mismatch_count == 0
        and actual_group_fingerprint == fact["group_fingerprint"]
    )
    return {
        "matches": matches,
        "expected_primary_count": int(fact["expected_total_item_count"]),
        "observed_primary_count": observed_item_count,
        "mismatch_count": mismatch_count,
        "expected_fingerprint": fact["group_fingerprint"],
        "observed_fingerprint": actual_group_fingerprint,
        "expected_scope_count": int(fact["expected_scope_count"]),
        "observed_scope_count": len(actual_scopes),
        "expected_items_per_scope": int(fact["expected_items_per_scope"]),
    }


def _record_mismatch_count(
    expected: Iterable[dict[str, Any]],
    observed: Iterable[dict[str, Any]],
    *,
    key: Any,
) -> int:
    expected_by_key = {key(item): item for item in expected}
    observed_by_key = {key(item): item for item in observed}
    all_keys = set(expected_by_key) | set(observed_by_key)
    return sum(
        expected_by_key.get(item_key) != observed_by_key.get(item_key)
        for item_key in all_keys
    )


def _pdf_visual_runtime_identity(
    runtime: PdfVisualTextRuntime,
) -> dict[str, str]:
    """Return the exact in-process provider identities used for one capture.

    This value is intentionally transient. A4 replays only the approved page
    text provider; its table-provider version is review provenance for the
    independently approved baseline, while the evaluator measures the current
    native parser so omissions are not hidden by replaying the oracle. An
    explicitly enabled visual-table fusion path may still record its own
    identity here. The value is not derived from a combined cache identity.
    """

    identity: dict[str, str] = {}
    for channel, provider in (
        ("visual_text", runtime.visual_text_provider),
        ("visual_table", runtime.visual_table_provider),
    ):
        if provider is None:
            continue
        provider_id = getattr(provider, "provider_id", None)
        provider_version = getattr(provider, "provider_version", None)
        if (
            not isinstance(provider_id, str)
            or not provider_id.strip()
            or not isinstance(provider_version, str)
            or not provider_version.strip()
        ):
            raise TypeError("PDF visual provider identity must be non-blank")
        identity[f"{channel}_provider_id"] = provider_id
        identity[f"{channel}_provider_version_sha256"] = hashlib.sha256(
            provider_version.encode("utf-8")
        ).hexdigest()
    return identity


def _evaluate_document(
    document_id: str,
    document: dict[str, Any],
    *,
    corpus_root: Path,
    legacy_mode: str,
    source_ir_mode: str,
    observation_cache: dict[tuple[str, str], dict[str, Any]],
    source_ir_cache: dict[tuple[str, str, str], dict[str, Any]],
    source_ir_snapshots: dict[tuple[str, str], Any],
    attachment_snapshots: dict[str, OpaqueAttachmentExtraction],
    legacy_timeout_seconds: float | None,
    pdf_visual_provider: str,
    pdf_visual_registry: Any | None,
    bailian_visual_model: str | None,
    pdf_visual_capture_page_numbers: frozenset[int] | None = None,
    pdf_visual_table_page_numbers: frozenset[int] = frozenset(),
    pdf_visual_runtime_identities: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    started = time.perf_counter()
    expected_hash = str(document.get("source_sha256") or "")
    result: dict[str, Any] = {
        "document_id": document_id,
        "document_role": str(document.get("document_role") or ""),
        "ingress_route": str(document.get("ingress_route") or ""),
        "declared_format": str(document.get("format") or ""),
        "required": bool(document.get("required")),
        "source_sha256": expected_hash,
        "identity_status": "UNVERIFIED",
        "preflight_status": "NOT_RUN",
        "diagnostic_codes": [],
        "legacy_status": "NOT_RUN",
        "legacy_counts": {},
        "source_ir_status": "NOT_RUN",
        "source_ir_counts": {},
        "attachment_status": "NOT_RUN",
        "hard_blocking": False,
    }
    blockers: list[dict[str, str]] = []

    def block(code: str) -> None:
        result["hard_blocking"] = True
        blockers.append({"document_id": document_id, "code": code})

    try:
        source_path = resolve_document_path(
            corpus_root, str(document.get("document_ref") or "")
        )
    except CorpusPathError:
        result["identity_status"] = "INVALID_REFERENCE"
        block("invalid_document_reference")
        return _finish_document(result, started), blockers
    if document.get("resolution_status") != "resolved":
        result["identity_status"] = "MANIFEST_UNRESOLVED"
        if result["required"]:
            block("required_document_unresolved")
        return _finish_document(result, started), blockers
    if not source_path.is_file():
        result["identity_status"] = "MISSING"
        if result["required"]:
            block("required_document_missing")
        return _finish_document(result, started), blockers

    actual_hash = sha256_file(source_path)
    if actual_hash != expected_hash:
        result["identity_status"] = "HASH_MISMATCH"
        if result["required"]:
            block("source_hash_mismatch")
        return _finish_document(result, started), blockers
    result["identity_status"] = "VERIFIED"

    try:
        report = preflight_document(source_path)
    except Exception:
        result["preflight_status"] = "FAILED"
        block("preflight_exception")
        return _finish_document(result, started), blockers
    if report.identity.source_sha256 != actual_hash:
        result["identity_status"] = "SOURCE_CHANGED"
        result["preflight_status"] = "SOURCE_CHANGED"
        if result["required"]:
            block("source_changed_during_preflight")
        return _finish_document(result, started), blockers
    detected_format = report.identity.detected_format.value
    result.update(
        {
            "detected_format": detected_format,
            "processable": report.processable,
            "attachment_acceptable": report.attachment_acceptable,
            "source_size": report.source_size,
            "page_count": report.page_count,
            "diagnostic_codes": [item.code.value for item in report.diagnostics],
        }
    )
    if detected_format != result["declared_format"]:
        result["preflight_status"] = "FORMAT_MISMATCH"
        block("manifest_format_mismatch")
        return _finish_document(result, started), blockers
    is_attachment_lane = bool(
        result["document_role"] == "customer_drawing"
        and result["ingress_route"] == "attachment"
    )
    if report.processable:
        result["preflight_status"] = "READY"
    elif detected_format in EXPECTED_NON_SERVING_FORMATS:
        result["preflight_status"] = "KNOWN_UNSUPPORTED"
    else:
        result["preflight_status"] = "BLOCKED"
        block("required_document_not_processable")
        return _finish_document(result, started), blockers

    if is_attachment_lane:
        if not report.attachment_acceptable:
            result["attachment_status"] = "BLOCKED_PREFLIGHT"
            block("required_attachment_not_acceptable")
            return _finish_document(result, started), blockers
        if source_ir_mode == "native":
            try:
                extraction = _run_attachment_observation(
                    source_path,
                    document_id=document_id,
                    upload_revision_id=str(document.get("upload_revision_id") or ""),
                    preflight_report=report,
                )
                if sha256_file(source_path) != actual_hash:
                    result["identity_status"] = "SOURCE_CHANGED"
                    result["preflight_status"] = "SOURCE_CHANGED"
                    result["attachment_status"] = "SOURCE_IDENTITY_CHANGED"
                    block("source_changed_during_attachment_preservation")
                    return _finish_document(result, started), blockers
                attachment_snapshots[document_id] = extraction
                disposition = extraction.disposition
                result.update(
                    {
                        "attachment_status": "COMPLETE",
                        "source_ir_status": "ATTACHMENT_ONLY",
                        "source_ir_adapter": "opaque-attachment-lane/1.0.0",
                        "source_ir_counts": {
                            "attachment_artifacts": disposition.attachment_artifact_count,
                            "attachment_references": disposition.attachment_reference_count,
                            "decision_requirements": len(
                                extraction.requirement_view.nodes
                            ),
                            "visual_text_snapshots": len(
                                extraction.source_ir.visual_text_snapshots
                            ),
                        },
                        "source_ir_diagnostic_codes": {},
                    }
                )
            except AttachmentPreservationRejected as exc:
                result["attachment_status"] = "FAILED"
                result["attachment_error_code"] = exc.code.value
                block("attachment_preservation_failed")
            except Exception as exc:
                result["attachment_status"] = "FAILED"
                result["attachment_error_type"] = type(exc).__name__
                block("attachment_preservation_exception")
        if legacy_mode != "off":
            result["legacy_status"] = "SKIPPED_ATTACHMENT_POLICY"
        return _finish_document(result, started), blockers

    if source_ir_mode == "native":
        pdf_visual_runtime: PdfVisualTextRuntime | None = None
        source_ir_adapters: tuple[Any, ...] | None = None
        adapter_cache_identity = "default"
        if detected_format == "pdf" and pdf_visual_provider != PDF_VISUAL_PROVIDER_OFF:
            route_candidates = [
                candidate
                for candidate in (pdf_visual_registry.documents.values())
                if candidate.source_sha256 == actual_hash
            ]
            if len(route_candidates) != 1:
                result["source_ir_status"] = "BLOCKED_VISUAL_PROFILE"
                result["source_ir_counts"] = {}
                block("pdf_visual_profile_unavailable")
                return _finish_document(result, started), blockers
            try:
                pdf_visual_runtime = build_pdf_visual_text_runtime_from_silver(
                    source=source_path,
                    silver_document=route_candidates[0],
                    provider_name=pdf_visual_provider,
                    bailian_model=bailian_visual_model,
                    visual_text_page_numbers=pdf_visual_capture_page_numbers,
                    visual_table_page_numbers=pdf_visual_table_page_numbers,
                )
            except Exception as exc:
                result["source_ir_status"] = "BLOCKED_VISUAL_RUNTIME"
                result["source_ir_counts"] = {}
                result["source_ir_error_type"] = type(exc).__name__
                block("pdf_visual_runtime_unavailable")
                return _finish_document(result, started), blockers
            if pdf_visual_runtime_identities is not None:
                pdf_visual_runtime_identities[document_id] = (
                    _pdf_visual_runtime_identity(pdf_visual_runtime)
                )
            source_ir_adapters = (pdf_visual_runtime.pdf_adapter,)
            adapter_cache_identity = pdf_visual_runtime.pdf_adapter.adapter_version
            result["pdf_visual_route"] = {
                "provider_id": (
                    pdf_visual_runtime.visual_text_provider.provider_id
                    if pdf_visual_runtime.visual_text_provider is not None
                    else None
                ),
                "reviewed_page_count": len(pdf_visual_runtime.route.page_numbers),
                "capture_page_count": len(
                    getattr(
                        pdf_visual_runtime,
                        "visual_text_page_numbers",
                        pdf_visual_runtime.route.page_numbers,
                    )
                ),
            }
        source_ir_cache_key = (
            actual_hash,
            detected_format,
            adapter_cache_identity,
        )
        source_snapshot_key = (actual_hash, detected_format)
        if (
            not report.processable
            and not report.controlled_conversion_eligible
        ):
            result["source_ir_status"] = "BLOCKED_PREFLIGHT"
        elif source_ir_cache_key in source_ir_cache:
            cached_source_ir = source_ir_cache[source_ir_cache_key]
            result["source_ir_status"] = "REUSED_IDENTICAL_SOURCE"
            result["source_ir_counts"] = dict(
                cached_source_ir.get("source_ir_counts") or {}
            )
            result["source_ir_adapter"] = str(
                cached_source_ir.get("source_ir_adapter") or ""
            )
            result["source_ir_diagnostic_codes"] = dict(
                cached_source_ir.get("source_ir_diagnostic_codes") or {}
            )
        else:
            source_ir_observation = _run_source_ir_observation(
                source_path,
                expected_source_sha256=actual_hash,
                preflight_report=report,
                adapters=source_ir_adapters,
                pdf_visual_runtime=pdf_visual_runtime,
                snapshot_sink=lambda snapshot: source_ir_snapshots.__setitem__(
                    source_snapshot_key,
                    snapshot,
                ),
            )
            result.update(source_ir_observation)
            if result["source_ir_status"] in {"COMPLETE", "PARTIAL"}:
                source_ir_cache[source_ir_cache_key] = source_ir_observation
            elif result["source_ir_status"].startswith("BLOCKED_VISUAL"):
                block("pdf_visual_capture_failed")

    if legacy_mode == "off":
        return _finish_document(result, started), blockers
    if detected_format not in LEGACY_SAFE_FORMATS:
        result["legacy_status"] = "BLOCKED_NO_SAFE_ADAPTER"
        return _finish_document(result, started), blockers

    cache_key = (actual_hash, legacy_mode)
    if cache_key in observation_cache:
        cached = observation_cache[cache_key]
        result["legacy_status"] = "REUSED_IDENTICAL_SOURCE"
        result["legacy_counts"] = dict(cached.get("legacy_counts") or {})
        return _finish_document(result, started), blockers
    try:
        observation = _run_legacy_observation(
            source_path,
            document_id=document_id,
            legacy_mode=legacy_mode,
            expected_source_sha256=actual_hash,
            timeout_seconds=legacy_timeout_seconds,
        )
        if sha256_file(source_path) != actual_hash:
            result["identity_status"] = "SOURCE_CHANGED"
            result["preflight_status"] = "SOURCE_CHANGED"
            result["legacy_status"] = "SOURCE_IDENTITY_CHANGED"
            if result["required"]:
                block("source_changed_during_legacy_observation")
            return _finish_document(result, started), blockers
        result.update(observation)
        if str(observation.get("legacy_status") or "").startswith("OBSERVED_"):
            observation_cache[cache_key] = observation
    except SourceIdentityChanged:
        result["identity_status"] = "SOURCE_CHANGED"
        result["preflight_status"] = "SOURCE_CHANGED"
        result["legacy_status"] = "SOURCE_IDENTITY_CHANGED"
        if result["required"]:
            block("source_changed_before_legacy_observation")
    except Exception as exc:
        result["legacy_status"] = "FAILED"
        result["legacy_error_type"] = type(exc).__name__
    return _finish_document(result, started), blockers


def _finish_document(result: dict[str, Any], started: float) -> dict[str, Any]:
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


def _run_attachment_observation(
    source_path: Path,
    *,
    document_id: str,
    upload_revision_id: str,
    preflight_report: Any,
) -> OpaqueAttachmentExtraction:
    context = ExtractionDocumentContext(
        logical_document_id=document_id,
        upload_revision_id=upload_revision_id,
        source_sha256=preflight_report.source_sha256,
        source_size=preflight_report.source_size,
        document_role=ExtractionDocumentRole.CUSTOMER_DRAWING,
        ingress_route=ExtractionIngressRoute.ATTACHMENT,
    )
    with tempfile.TemporaryDirectory(prefix="reqpilotx-attachment-store-") as directory:
        store = LocalAttachmentArtifactStore(directory)
        extraction = preserve_opaque_attachment_pipeline(
            source_path,
            context=context,
            artifact_store=store,
            preflight_report=preflight_report,
        )
        if not store.verify(extraction.disposition.artifact):
            raise RuntimeError("attachment_store_verification_failed")
        return extraction


def _run_legacy_observation(
    source_path: Path,
    *,
    document_id: str,
    legacy_mode: str,
    expected_source_sha256: str,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    source_bytes = source_path.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != expected_source_sha256:
        raise SourceIdentityChanged
    with tempfile.TemporaryDirectory(prefix="reqpilotx-legacy-source-") as directory:
        staged_source = Path(directory) / f"source{source_path.suffix.lower()}"
        staged_source.write_bytes(source_bytes)
        if timeout_seconds is None:
            timeout_seconds = DEFAULT_LEGACY_TIMEOUT_SECONDS
        return _run_staged_legacy_observation_with_timeout(
            staged_source,
            document_id=document_id,
            legacy_mode=legacy_mode,
            timeout_seconds=timeout_seconds,
        )


def _run_staged_legacy_observation_with_timeout(
    source_path: Path,
    *,
    document_id: str,
    legacy_mode: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_legacy_observation_worker,
        args=(
            send_connection,
            source_path,
            document_id,
            legacy_mode,
        ),
        name=f"reqpilotx-legacy-{document_id}",
    )
    process.start()
    send_connection.close()
    try:
        if not receive_connection.poll(timeout_seconds):
            _stop_legacy_worker(process)
            return {
                "legacy_status": "BLOCKED_TIMEOUT",
                "legacy_counts": {},
                "legacy_timeout_seconds": timeout_seconds,
            }
        try:
            payload = receive_connection.recv()
        except EOFError:
            payload = {
                "kind": "error",
                "error_type": "LegacyWorkerExited",
            }
    finally:
        receive_connection.close()

    process.join(_LEGACY_WORKER_SHUTDOWN_SECONDS)
    if process.is_alive():
        _stop_legacy_worker(process)
    if not isinstance(payload, dict) or payload.get("kind") != "result":
        return {
            "legacy_status": "FAILED",
            "legacy_counts": {},
            "legacy_error_type": str(
                payload.get("error_type")
                if isinstance(payload, dict)
                else "LegacyWorkerProtocolError"
            ),
        }
    result = payload.get("result")
    if not isinstance(result, dict):
        return {
            "legacy_status": "FAILED",
            "legacy_counts": {},
            "legacy_error_type": "LegacyWorkerProtocolError",
        }
    return result


def _legacy_observation_worker(
    connection: Any,
    source_path: Path,
    document_id: str,
    legacy_mode: str,
) -> None:
    try:
        result = _run_staged_legacy_observation(
            source_path,
            document_id=document_id,
            legacy_mode=legacy_mode,
        )
        connection.send({"kind": "result", "result": result})
    except BaseException as exc:
        connection.send({"kind": "error", "error_type": type(exc).__name__})
    finally:
        connection.close()


def _stop_legacy_worker(process: Any) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(_LEGACY_WORKER_SHUTDOWN_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_LEGACY_WORKER_SHUTDOWN_SECONDS)


def _run_staged_legacy_observation(
    source_path: Path,
    *,
    document_id: str,
    legacy_mode: str,
) -> dict[str, Any]:
    capture_out = io.StringIO()
    capture_err = io.StringIO()
    pypdf_logger = logging.getLogger("pypdf")
    previous_level = pypdf_logger.level
    pypdf_logger.setLevel(logging.ERROR)
    try:
        with redirect_stdout(capture_out), redirect_stderr(capture_err):
            if legacy_mode == "preview":
                with tempfile.TemporaryDirectory(
                    prefix="reqpilotx-case-preview-"
                ) as directory:
                    temporary_root = Path(directory).resolve()
                    snapshot_root = (
                        temporary_root / "table_snapshots" / document_id
                    ).resolve()
                    try:
                        snapshot_root.relative_to(temporary_root)
                    except ValueError as exc:
                        raise CorpusPathError(
                            "Preview snapshot path escaped its temporary root."
                        ) from exc
                    preview = build_extraction_preview(
                        str(source_path),
                        document_id=document_id,
                        snapshot_root=snapshot_root,
                    )
                entries = preview.get("complete_requirement_entries") or []
                raw_entries = preview.get("complete_requirement_entries_raw") or entries
                summary = (
                    preview.get("summary")
                    if isinstance(preview.get("summary"), dict)
                    else {}
                )
                kinds = Counter(
                    str(item.get("entry_kind") or "UNKNOWN")
                    for item in raw_entries
                    if isinstance(item, dict)
                )
                return {
                    "legacy_status": "OBSERVED_PREVIEW",
                    "legacy_counts": {
                        # A preview deliberately does not expose parser segments.
                        # Keep its own public counters instead of treating the
                        # complete entry list (which includes headings) as a
                        # requirement count.
                        "non_empty_pages": int(summary.get("non_empty_pages") or 0),
                        "sections": len(preview.get("section_outline") or []),
                        "requirements": int(summary.get("requirements_count") or 0),
                        "raw_requirement_rows": int(
                            summary.get("raw_requirement_rows_count") or 0
                        ),
                        "complete_entries": len(entries),
                        "raw_entries": len(raw_entries),
                        "headings": int(
                            summary.get("heading_entries_count")
                            or kinds.get("HEADING", 0)
                        ),
                        "hybrid": int(
                            summary.get("hybrid_entries_count")
                            or kinds.get("HYBRID", 0)
                        ),
                    },
                }

            parsed = parse_document_with_diagnostics(str(source_path))
            diagnostics = (
                parsed.get("diagnostics")
                if isinstance(parsed.get("diagnostics"), dict)
                else {}
            )
            if str(diagnostics.get("mode") or "").lower() == "fallback_text":
                raise RuntimeError("unsafe legacy text fallback")
            segments = (
                parsed.get("segments")
                if isinstance(parsed.get("segments"), list)
                else []
            )
            requirements = extract_atomic_requirements(segments)
            outline = extract_section_outline(segments)
            consistency = analyze_section_consistency(outline)
            return {
                "legacy_status": "OBSERVED_CORE",
                "legacy_counts": {
                    "segments": len(segments),
                    "sections": len(outline),
                    "requirements": len(requirements),
                    "section_consistency_issues": len(consistency.get("issues") or []),
                },
            }
    finally:
        pypdf_logger.setLevel(previous_level)


def _run_source_ir_observation(
    source_path: Path,
    *,
    expected_source_sha256: str,
    preflight_report: Any | None = None,
    adapters: tuple[Any, ...] | None = None,
    pdf_visual_runtime: PdfVisualTextRuntime | None = None,
    snapshot_sink: Any | None = None,
) -> dict[str, Any]:
    """Return counts and status only; never serialize customer facts."""

    try:
        build_kwargs: dict[str, Any] = {}
        if adapters is not None:
            build_kwargs["adapters"] = adapters
        snapshot = build_source_ir(
            source_path,
            preflight_report=preflight_report,
            **build_kwargs,
        )
    except SourceIRBuildRejected as exc:
        if exc.code is SourceIRBuildRejectionCode.ADAPTER_UNAVAILABLE:
            status = "BLOCKED_NO_ADAPTER"
        else:
            status = "BUILD_REJECTED"
        return {
            "source_ir_status": status,
            "source_ir_counts": {},
            "source_ir_rejection_code": exc.code.value,
        }
    except Exception as exc:
        return {
            "source_ir_status": "BUILD_EXCEPTION",
            "source_ir_counts": {},
            "source_ir_error_type": type(exc).__name__,
        }

    if snapshot.source_sha256 != expected_source_sha256:
        return {
            "source_ir_status": "SOURCE_IDENTITY_CHANGED",
            "source_ir_counts": {},
        }

    diagnostic_codes = Counter(item.code for item in snapshot.diagnostics)
    if pdf_visual_runtime is not None:
        runtime_failure_count = sum(
            diagnostic_codes.get(code, 0) for code in _PDF_VISUAL_RUNTIME_FAILURE_CODES
        )
        if runtime_failure_count:
            failure_pages = sorted(
                {
                    diagnostic.native_locator.page_number
                    for diagnostic in snapshot.diagnostics
                    if diagnostic.code in _PDF_VISUAL_RUNTIME_FAILURE_CODES
                    and getattr(diagnostic, "native_locator", None) is not None
                    and diagnostic.native_locator.page_number is not None
                }
            )
            return {
                "source_ir_status": "BLOCKED_VISUAL_RUNTIME",
                "source_ir_counts": {},
                "source_ir_diagnostic_codes": dict(sorted(diagnostic_codes.items())),
                "pdf_visual_runtime_failure_count": runtime_failure_count,
                "pdf_visual_runtime_failure_pages": failure_pages,
            }
        try:
            validate_pdf_visual_text_capture(snapshot, pdf_visual_runtime)
        except Exception as exc:
            return {
                "source_ir_status": "BLOCKED_VISUAL_CAPTURE",
                "source_ir_counts": {},
                "source_ir_diagnostic_codes": dict(sorted(diagnostic_codes.items())),
                "source_ir_error_type": type(exc).__name__,
            }

    kinds = Counter(node.kind.value for node in snapshot.nodes)
    tables = tuple(
        node.table_payload
        for node in snapshot.nodes
        if node.kind is SourceNodeKind.TABLE and node.table_payload is not None
    )
    if snapshot.status.value in {"COMPLETE", "PARTIAL"} and snapshot_sink is not None:
        snapshot_sink(snapshot)
    return {
        "source_ir_status": snapshot.status.value,
        "source_ir_adapter": f"{snapshot.adapter_id}@{snapshot.adapter_version}",
        "source_ir_diagnostic_codes": dict(sorted(diagnostic_codes.items())),
        "source_ir_counts": {
            "nodes": len(snapshot.nodes),
            "assets": len(snapshot.assets),
            "diagnostics": len(snapshot.diagnostics),
            "paragraphs": kinds.get(SourceNodeKind.PARAGRAPH.value, 0),
            "tables": len(tables),
            "table_cells": sum(len(table.cells) for table in tables),
            "merged_regions": sum(len(table.merged_regions) for table in tables),
            "images": kinds.get(SourceNodeKind.IMAGE.value, 0),
            "visual_text_snapshots": len(
                getattr(snapshot, "visual_text_snapshots", ())
            ),
            "visual_text_observations": sum(
                len(item.observations)
                for item in getattr(snapshot, "visual_text_snapshots", ())
            ),
        },
    }


def write_reports(
    report: dict[str, Any],
    output_dir: Path,
    *,
    corpus_root: Path,
) -> tuple[Path, Path]:
    output_dir = require_report_destination_outside_corpus(output_dir, corpus_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "run.json"
    markdown_path = output_dir / "summary.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    assertion_counts = summary.get("assertion_status_counts") or {}
    lines = [
        "# Extraction case run",
        "",
        f"- Identity gate: {'PASS' if summary['identity_gate_pass'] else 'FAIL'}",
        f"- Documents: {summary['documents']}",
        f"- Cases: {summary['cases']}",
        f"- Assertions registered: {summary['assertions']}",
        (
            "- Assertion results: "
            f"PASS {int(assertion_counts.get('PASS', 0))}, "
            f"FAIL {int(assertion_counts.get('FAIL', 0))}, "
            f"GAP {int(assertion_counts.get('GAP', 0))}, "
            f"BLOCKED_INPUT {int(assertion_counts.get('BLOCKED_INPUT', 0))}"
        ),
        f"- Legacy mode: `{report['legacy_mode']}`",
        f"- SourceIR mode: `{report['source_ir_mode']}`",
        "",
        "## Document observations",
        "",
        "| Document ID | Role | Format | Identity | Preflight | SourceIR | Legacy |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report["documents"].values():
        lines.append(
            "| {document_id} | {document_role} | {format} | {identity_status} | {preflight_status} | {source_ir_status} | {legacy_status} |".format(
                document_id=row["document_id"],
                document_role=row["document_role"],
                format=row.get("detected_format") or row["declared_format"],
                identity_status=row["identity_status"],
                preflight_status=row["preflight_status"],
                source_ir_status=row["source_ir_status"],
                legacy_status=row["legacy_status"],
            )
        )
    lines.extend(
        [
            "",
            "## Case gap matrix",
            "",
            "| Case ID | Status | First unmet layer | Documents | PASS | FAIL | GAP | BLOCKED |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report["cases"].values():
        case_assertion_counts = row.get("assertion_status_counts") or {}
        lines.append(
            f"| {row['case_id']} | {row['status']} | {row['first_gap_layer'] or '-'} | "
            f"{row['document_count']} | {int(case_assertion_counts.get('PASS', 0))} | "
            f"{int(case_assertion_counts.get('FAIL', 0))} | "
            f"{int(case_assertion_counts.get('GAP', 0))} | "
            f"{int(case_assertion_counts.get('BLOCKED_INPUT', 0))} |"
        )
    lines.append("")
    return "\n".join(lines)


def _resolve_cli_path(value: Path) -> Path:
    return value if value.is_absolute() else PROJECT_DIR / value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the confirmed ReqPilotX extraction case manifest."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=private_eval_path("extraction_cases/manifest.json"),
    )
    parser.add_argument("--corpus-root", type=Path, default=None)
    parser.add_argument(
        "--out-dir", type=Path, default=Path("eval/extraction_cases/reports/latest")
    )
    parser.add_argument(
        "--legacy-mode", choices=("off", "core", "preview"), default="off"
    )
    parser.add_argument(
        "--legacy-timeout-seconds",
        type=float,
        default=DEFAULT_LEGACY_TIMEOUT_SECONDS,
        help="Maximum time for each isolated legacy document observation.",
    )
    parser.add_argument("--source-ir-mode", choices=("off", "native"), default="off")
    parser.add_argument(
        "--pdf-visual-provider",
        choices=PDF_VISUAL_PROVIDER_CHOICES,
        default=PDF_VISUAL_PROVIDER_OFF,
        help=(
            "Optional source-bound remote visual capture for registered PDF pages. "
            "Only Bailian or Azure Document Intelligence are supported."
        ),
    )
    parser.add_argument(
        "--pdf-visual-route-registry",
        type=Path,
        default=DEFAULT_PDF_SHADOW_SILVER_PATH,
        help="Routing-only Silver registry for PDF source/page/provider bindings.",
    )
    parser.add_argument(
        "--pdf-visual-route-schema",
        type=Path,
        default=DEFAULT_PDF_SHADOW_SILVER_SCHEMA_PATH,
    )
    parser.add_argument(
        "--bailian-visual-model",
        default=None,
        help="Optional Bailian visual model override; ignored for Azure.",
    )
    parser.add_argument(
        "--pdf-body-review-artifact-root",
        type=Path,
        default=None,
        help=(
            "Explicit controlled external directory containing canonical "
            "<sha256>.json PDF body review artifacts."
        ),
    )
    parser.add_argument(
        "--pdf-body-review-trust-policy",
        type=Path,
        default=None,
        help=("Explicit controlled external PDF body review trust-policy JSON."),
    )
    parser.add_argument(
        "--strict-source-ir",
        action="store_true",
        help="Return a non-zero exit when a native SourceIR observation fails to build.",
    )
    parser.add_argument("--case-id", action="append", default=[])
    args = parser.parse_args(argv)

    try:
        if args.strict_source_ir and args.source_ir_mode != "native":
            raise ValueError("--strict-source-ir requires --source-ir-mode native.")
        if (
            args.pdf_visual_provider != PDF_VISUAL_PROVIDER_OFF
            and args.source_ir_mode != "native"
        ):
            raise ValueError("--pdf-visual-provider requires --source-ir-mode native.")
        if (args.pdf_body_review_artifact_root is None) != (
            args.pdf_body_review_trust_policy is None
        ):
            raise ValueError(
                "--pdf-body-review-artifact-root and "
                "--pdf-body-review-trust-policy must be provided together."
            )
        manifest_path = _resolve_cli_path(args.manifest).resolve()
        oracle_registry = load_oracle_registry(manifest_path.with_name("oracles.json"))
        manifest = load_manifest(
            manifest_path,
            oracle_path=manifest_path.with_name("oracles.json"),
        )
        environment_name = str(
            (manifest.get("corpus") or {}).get("mapping_environment_variable")
            or "REQPILOTX_TRAINING_CORPUS_ROOT"
        )
        corpus_value = str(
            args.corpus_root or os.getenv(environment_name) or ""
        ).strip()
        if not corpus_value:
            raise CorpusPathError(
                f"Provide --corpus-root or set {environment_name}; no personal default path is used."
            )
        corpus_root = Path(corpus_value)
        output_dir = require_report_destination_outside_corpus(
            _resolve_cli_path(args.out_dir),
            corpus_root,
        )
        pdf_visual_registry = (
            load_pdf_shadow_silver(
                _resolve_cli_path(args.pdf_visual_route_registry).resolve(),
                schema_path=_resolve_cli_path(args.pdf_visual_route_schema).resolve(),
            )
            if args.pdf_visual_provider != PDF_VISUAL_PROVIDER_OFF
            else None
        )
        pdf_body_review_artifact_resolver = (
            ContentAddressedPdfBodyReviewResolver(args.pdf_body_review_artifact_root)
            if args.pdf_body_review_artifact_root is not None
            else None
        )
        pdf_body_review_trust_policy = (
            load_pdf_body_review_trust_policy(args.pdf_body_review_trust_policy)
            if args.pdf_body_review_trust_policy is not None
            else None
        )
        report = run_manifest(
            manifest,
            corpus_root=corpus_root,
            legacy_mode=args.legacy_mode,
            source_ir_mode=args.source_ir_mode,
            selected_case_ids=args.case_id or None,
            legacy_timeout_seconds=(
                args.legacy_timeout_seconds if args.legacy_mode != "off" else None
            ),
            oracle_registry=oracle_registry,
            pdf_visual_provider=args.pdf_visual_provider,
            pdf_visual_registry=pdf_visual_registry,
            bailian_visual_model=args.bailian_visual_model,
            pdf_body_review_artifact_resolver=(pdf_body_review_artifact_resolver),
            pdf_body_review_trust_policy=pdf_body_review_trust_policy,
        )
        json_path, markdown_path = write_reports(
            report,
            output_dir,
            corpus_root=corpus_root,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print(
        f"Identity gate: {'PASS' if report['summary']['identity_gate_pass'] else 'FAIL'}"
    )
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    if not report["summary"]["identity_gate_pass"]:
        return 2
    if args.strict_source_ir and not report["summary"]["source_ir_observation_ok"]:
        return 3
    if int(report["summary"]["assertion_status_counts"].get("FAIL", 0)) > 0:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
