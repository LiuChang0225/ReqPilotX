#!/usr/bin/env python3
"""Evaluate ReqPilot assessment safety against controlled external Gold data.

The runner deliberately keeps customer text out of generated artifacts. Input
rows and knowledge excerpts are read from a controlled external payload, model
traces live only in a temporary directory, and the emitted report contains
aggregate counts and revision hashes only.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Sequence

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.services.ai_copilot import (
    AICopilotConfig,
    ensure_ai_profile_runnable,
    load_ai_copilot_config,
    load_ai_profiles,
    run_ai_copilot_assessment,
)
from app.domain.assessment.execution_policy import evaluate_assessment_execution


SCHEMA_VERSION = "ai_gold_eval.v2"
PAYLOAD_SCHEMA_VERSION = "ai_gold_payload.v2"
MANIFEST_SCHEMA_VERSION = "ai_gold_manifest.v2"
EXPLICIT_LABELS = ("REQUIREMENT", "INFO")
DECISION_LABELS = ("ACCEPT", "REJECT", "DEVIATION_ACCEPT", "NEED_CLARIFICATION")
COMPLIANCE_STATUSES = (
    "COMPLIANT",
    "PARTIALLY_COMPLIANT",
    "NON_COMPLIANT",
    "INSUFFICIENT_INFORMATION",
)
DECISION_TO_COMPLIANCE_STATUS = {
    "ACCEPT": "COMPLIANT",
    "DEVIATION_ACCEPT": "PARTIALLY_COMPLIANT",
    "REJECT": "NON_COMPLIANT",
    "NEED_CLARIFICATION": "INSUFFICIENT_INFORMATION",
}
MIN_EXPLICIT_POSITIVES = 100
MIN_EXPLICIT_NEGATIVES = 50
MIN_DECISION_CASES = 100
MIN_DECISION_CLASS_CASES = 20
MIN_LABELED_DOCUMENTS = 5
MIN_LABELED_PROJECTS = 2
HMAC_ID_PATTERN = re.compile(r"^[a-f0-9]{64}$")

# These are code-enforced safety floors. An approved manifest may tighten them
# but cannot lower them and still produce a passing gate report.
MIN_EXPLICIT_RECALL = 1.0
MIN_INFO_SPECIFICITY = 0.80
MIN_DECISION_MACRO_RECALL = 0.70
MIN_DECISION_CLASS_RECALL = 0.50
MAX_UNSAFE_ACCEPTS = 0
MIN_COMPLIANCE_MACRO_RECALL = 0.70
MIN_CITATION_PRECISION = 0.80
MIN_CITATION_RECALL = 0.80
MIN_INFORMATION_GAP_COMPLETENESS = 0.90
MIN_SUB_JUDGMENT_EXACT_MATCH = 0.75


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalized_contract_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def assessment_contract_sha256(gold: dict[str, Any]) -> str:
    """Hash every structured Gold assertion covered by independent review."""

    projection = {
        "compliance_status": _normalized_label(gold.get("compliance_status")),
        "expected_evidence_ids": sorted(
            str(item or "").strip()
            for item in gold.get("expected_evidence_ids") or []
            if str(item or "").strip()
        ),
        "required_missing_items": sorted(
            (
                str(item.get("missing_item_id") or "").strip().lower(),
                _normalized_contract_text(item.get("item")),
            )
            for item in gold.get("required_missing_items") or []
            if isinstance(item, dict)
        ),
        "expected_sub_judgments": sorted(
            (
                str(item.get("gold_sub_judgment_id") or "").strip().lower(),
                _normalized_contract_text(item.get("statement")),
                _normalized_label(item.get("compliance_status")),
            )
            for item in gold.get("expected_sub_judgments") or []
            if isinstance(item, dict)
        ),
    }
    return sha256_bytes(
        json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def build_config_fingerprint(config: AICopilotConfig) -> tuple[str, dict[str, Any]]:
    """Return a stable non-secret evaluation configuration fingerprint."""

    safe_config = {
        "assessment_mode": config.assessment_mode,
        "retrieval_backend": config.retrieval_backend,
        "top_k": config.top_k,
        "profile_id": config.profile_id,
        "provider": config.provider,
        "chat_model": config.chat_model,
        "screening_model": config.screening_model,
        "embedding_model": config.embedding_model,
        "azure_openai_api_version": config.azure_openai_api_version,
        "azure_openai_embedding_dimensions": config.azure_openai_embedding_dimensions,
        "dashscope_embedding_dimensions": config.dashscope_embedding_dimensions,
        "azure_auth_mode": config.azure_auth_mode,
        "azure_allow_interactive_login": config.azure_allow_interactive_login,
        "llm_timeout_seconds": config.llm_timeout_seconds,
        "openai_max_retries": config.openai_max_retries,
        "embedding_batch_size": config.embedding_batch_size,
    }
    # Endpoint/resource names influence execution and therefore the fingerprint,
    # but are deliberately omitted from the report's safe_config payload.
    fingerprint_config = {
        **safe_config,
        "azure_search_endpoint": config.azure_search_endpoint,
        "azure_search_index_name": config.azure_search_index_name,
        "azure_openai_endpoint": config.azure_openai_endpoint,
        "azure_openai_chat_deployment": config.azure_openai_chat_deployment,
        "azure_openai_embedding_deployment": config.azure_openai_embedding_deployment,
        "dashscope_base_url": config.dashscope_base_url,
        "dashvector_endpoint": config.dashvector_endpoint,
        "dashvector_collection_name": config.dashvector_collection_name,
        "dashvector_partition_name": config.dashvector_partition_name,
        "qdrant_url": config.qdrant_url,
        "qdrant_collection_name": config.qdrant_collection_name,
    }
    encoded = json.dumps(
        fingerprint_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256_bytes(encoded), safe_config


def git_revision(project_dir: Path) -> tuple[str, bool]:
    try:
        code_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        return code_sha, dirty
    except (OSError, subprocess.CalledProcessError):
        return "", True


def _normalized_label(value: Any) -> str:
    return str(value or "").strip().upper()


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unknown_fields(value: dict[str, Any], allowed: set[str]) -> list[str]:
    return sorted(str(key) for key in value if key not in allowed)


def _coverage_from_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    valid_cases = [case for case in cases if isinstance(case, dict)]
    explicit = [
        case for case in valid_cases if _normalized_label((case.get("gold") or {}).get("analysis_label"))
    ]
    decisions = [
        case for case in valid_cases if _normalized_label((case.get("gold") or {}).get("final_decision"))
    ]

    def identities(rows: list[dict[str, Any]], key: str) -> int:
        return len({str(row.get(key) or "") for row in rows if str(row.get(key) or "")})

    explicit_counts = Counter(_normalized_label((case.get("gold") or {}).get("analysis_label")) for case in explicit)
    decision_counts = Counter(_normalized_label((case.get("gold") or {}).get("final_decision")) for case in decisions)
    compliance_counts = Counter(
        _normalized_label((case.get("gold") or {}).get("compliance_status"))
        for case in decisions
    )
    return {
        "case_count": len(cases),
        "explicit_requirement": {
            "labeled": len(explicit),
            "document_count": identities(explicit, "document_id"),
            "project_count": identities(explicit, "project_id"),
            "label_counts": {label: explicit_counts.get(label, 0) for label in EXPLICIT_LABELS},
        },
        "decision": {
            "labeled": len(decisions),
            "document_count": identities(decisions, "document_id"),
            "project_count": identities(decisions, "project_id"),
            "class_counts": {label: decision_counts.get(label, 0) for label in DECISION_LABELS},
        },
        "assessment_contract": {
            "labeled": sum(compliance_counts.values()),
            "class_counts": {
                status: compliance_counts.get(status, 0)
                for status in COMPLIANCE_STATUSES
            },
        },
    }


def _validate_label_provenance(case: dict[str, Any], index: int) -> list[str]:
    blockers: list[str] = []
    gold = case.get("gold") if isinstance(case.get("gold"), dict) else {}
    reviews = case.get("reviews") if isinstance(case.get("reviews"), list) else []
    if len(reviews) < 2:
        return [f"case_{index}:fewer_than_two_reviews"]

    reviewers: set[str] = set()
    review_values: dict[str, set[str]] = {
        "analysis_label": set(),
        "final_decision": set(),
        "compliance_status": set(),
        "assessment_contract_sha256": set(),
    }
    for review_index, review in enumerate(reviews):
        if not isinstance(review, dict):
            blockers.append(f"case_{index}:review_{review_index}:not_object")
            continue
        blockers.extend(
            f"case_{index}:review_{review_index}:unknown_field:{field}"
            for field in _unknown_fields(
                review,
                {
                    "reviewer_id",
                    "analysis_label",
                    "final_decision",
                    "compliance_status",
                    "assessment_contract_sha256",
                },
            )
        )
        reviewer_id = str(review.get("reviewer_id") or "").strip().lower()
        if not HMAC_ID_PATTERN.fullmatch(reviewer_id):
            blockers.append(f"case_{index}:review_{review_index}:reviewer_id_not_hmac")
        if reviewer_id in reviewers:
            blockers.append(f"case_{index}:duplicate_reviewer")
        reviewers.add(reviewer_id)
        for field in review_values:
            expected = (
                str(gold.get(field) or "").strip().lower()
                if field == "assessment_contract_sha256"
                else _normalized_label(gold.get(field))
            )
            actual = (
                str(review.get(field) or "").strip().lower()
                if field == "assessment_contract_sha256"
                else _normalized_label(review.get(field))
            )
            if expected:
                if not actual:
                    blockers.append(f"case_{index}:review_{review_index}:missing_{field}")
                else:
                    allowed = (
                        EXPLICIT_LABELS
                        if field == "analysis_label"
                        else DECISION_LABELS
                        if field == "final_decision"
                        else COMPLIANCE_STATUSES
                        if field == "compliance_status"
                        else ()
                    )
                    if (
                        field == "assessment_contract_sha256"
                        and not HMAC_ID_PATTERN.fullmatch(actual)
                    ) or (
                        field != "assessment_contract_sha256"
                        and actual not in allowed
                    ):
                        blockers.append(f"case_{index}:review_{review_index}:invalid_{field}")
                    review_values[field].add(actual)

    disagreement = any(len(values) > 1 for values in review_values.values())
    adjudication = case.get("adjudication") if isinstance(case.get("adjudication"), dict) else {}
    blockers.extend(
        f"case_{index}:adjudication_unknown_field:{field}"
        for field in _unknown_fields(
            adjudication,
            {
                "status",
                "adjudicator_id",
                "analysis_label",
                "final_decision",
                "compliance_status",
                "assessment_contract_sha256",
            },
        )
    )
    status = _normalized_label(adjudication.get("status"))
    if disagreement:
        if status != "COMPLETED":
            blockers.append(f"case_{index}:disagreement_without_completed_adjudication")
        adjudicator_id = str(adjudication.get("adjudicator_id") or "").strip().lower()
        if not HMAC_ID_PATTERN.fullmatch(adjudicator_id) or adjudicator_id in reviewers:
            blockers.append(f"case_{index}:invalid_independent_adjudicator")
        for field in review_values:
            expected = (
                str(gold.get(field) or "").strip().lower()
                if field == "assessment_contract_sha256"
                else _normalized_label(gold.get(field))
            )
            actual = (
                str(adjudication.get(field) or "").strip().lower()
                if field == "assessment_contract_sha256"
                else _normalized_label(adjudication.get(field))
            )
            if expected and actual != expected:
                blockers.append(f"case_{index}:adjudication_mismatch:{field}")
    elif status not in {"", "NOT_REQUIRED"}:
        blockers.append(f"case_{index}:unexpected_adjudication_status")

    # With no disagreement, both reviewers must independently agree with Gold.
    if not disagreement:
        for field, values in review_values.items():
            expected = (
                str(gold.get(field) or "").strip().lower()
                if field == "assessment_contract_sha256"
                else _normalized_label(gold.get(field))
            )
            if expected and values != {expected}:
                blockers.append(f"case_{index}:review_consensus_mismatch:{field}")
    return blockers


def validate_gold_contract(
    *,
    manifest: dict[str, Any],
    payload: dict[str, Any],
    payload_sha256: str,
    code_sha: str,
    config_sha256: str,
) -> tuple[list[str], dict[str, Any]]:
    """Validate the freeze, coverage, revision and independent-label contract."""

    blockers: list[str] = []
    manifest_required = {
        "schema_version",
        "dataset_id",
        "version",
        "status",
        "payload",
        "label_policy",
        "evaluation_policy",
        "coverage",
        "split_strategy",
        "revisions",
        "privacy",
    }
    missing_manifest_fields = sorted(manifest_required - set(manifest))
    blockers.extend(f"manifest_missing_field:{field}" for field in missing_manifest_fields)
    blockers.extend(
        f"manifest_unknown_field:{field}"
        for field in _unknown_fields(manifest, manifest_required)
    )
    payload_required = {"schema_version", "dataset_id", "version", "cases"}
    blockers.extend(
        f"payload_missing_field:{field}" for field in sorted(payload_required - set(payload))
    )
    blockers.extend(
        f"payload_unknown_field:{field}" for field in _unknown_fields(payload, payload_required)
    )
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        blockers.append("manifest_schema_version_mismatch")
    if payload.get("schema_version") != PAYLOAD_SCHEMA_VERSION:
        blockers.append("payload_schema_version_mismatch")
    if not str(manifest.get("dataset_id") or "").strip():
        blockers.append("manifest_dataset_id_missing")
    if not str(manifest.get("version") or "").strip():
        blockers.append("manifest_dataset_version_missing")
    if str(manifest.get("dataset_id") or "") != str(payload.get("dataset_id") or ""):
        blockers.append("dataset_id_mismatch")
    if str(manifest.get("version") or "") != str(payload.get("version") or ""):
        blockers.append("dataset_version_mismatch")
    if _normalized_label(manifest.get("status")) != "FROZEN":
        blockers.append("manifest_not_frozen")
    if manifest.get("split_strategy") != "PROJECT_DOCUMENT_GROUP":
        blockers.append("split_strategy_mismatch")

    manifest_payload = manifest.get("payload") if isinstance(manifest.get("payload"), dict) else {}
    if manifest_payload.get("location_class") != "CONTROLLED_EXTERNAL":
        blockers.append("payload_not_controlled_external")
    if str(manifest_payload.get("sha256") or "").lower() != payload_sha256:
        blockers.append("payload_sha256_mismatch")

    label_policy = manifest.get("label_policy") if isinstance(manifest.get("label_policy"), dict) else {}
    if not str(label_policy.get("version") or "").strip():
        blockers.append("label_policy_version_missing")
    if _normalized_label(label_policy.get("approval_status")) != "APPROVED":
        blockers.append("label_policy_not_approved")
    required_reviewers = _safe_int(label_policy.get("reviewers_per_case"))
    if required_reviewers is None or required_reviewers < 2:
        blockers.append("label_policy_requires_fewer_than_two_reviewers")
    if label_policy.get("adjudication_required") is not True:
        blockers.append("label_policy_does_not_require_adjudication")

    evaluation_policy = (
        manifest.get("evaluation_policy") if isinstance(manifest.get("evaluation_policy"), dict) else {}
    )
    if _normalized_label(evaluation_policy.get("approval_status")) != "APPROVED":
        blockers.append("evaluation_policy_not_approved")
    if not str(evaluation_policy.get("version") or "").strip():
        blockers.append("evaluation_policy_version_missing")
    explicit_policy = (
        evaluation_policy.get("explicit_requirement")
        if isinstance(evaluation_policy.get("explicit_requirement"), dict)
        else {}
    )
    decision_policy = (
        evaluation_policy.get("decision_safety")
        if isinstance(evaluation_policy.get("decision_safety"), dict)
        else {}
    )
    assessment_contract_policy = (
        evaluation_policy.get("assessment_contract")
        if isinstance(evaluation_policy.get("assessment_contract"), dict)
        else {}
    )
    explicit_recall_threshold = _safe_float(explicit_policy.get("min_recall"))
    info_specificity_threshold = _safe_float(explicit_policy.get("min_info_specificity"))
    macro_recall_threshold = _safe_float(decision_policy.get("min_macro_recall"))
    class_recall_threshold = _safe_float(decision_policy.get("min_per_class_recall"))
    unsafe_accept_threshold = _safe_int(decision_policy.get("max_unsafe_accepts"))
    compliance_macro_recall_threshold = _safe_float(
        assessment_contract_policy.get("min_compliance_macro_recall")
    )
    citation_precision_threshold = _safe_float(
        assessment_contract_policy.get("min_citation_precision")
    )
    citation_recall_threshold = _safe_float(
        assessment_contract_policy.get("min_citation_recall")
    )
    information_gap_threshold = _safe_float(
        assessment_contract_policy.get("min_information_gap_completeness")
    )
    sub_judgment_threshold = _safe_float(
        assessment_contract_policy.get("min_sub_judgment_exact_match")
    )
    if explicit_recall_threshold is None or explicit_recall_threshold < MIN_EXPLICIT_RECALL:
        blockers.append("explicit_recall_threshold_below_safety_floor")
    if info_specificity_threshold is None or info_specificity_threshold < MIN_INFO_SPECIFICITY:
        blockers.append("info_specificity_threshold_below_safety_floor")
    if macro_recall_threshold is None or macro_recall_threshold < MIN_DECISION_MACRO_RECALL:
        blockers.append("decision_macro_recall_threshold_below_safety_floor")
    if class_recall_threshold is None or class_recall_threshold < MIN_DECISION_CLASS_RECALL:
        blockers.append("decision_class_recall_threshold_below_safety_floor")
    if unsafe_accept_threshold is None or unsafe_accept_threshold > MAX_UNSAFE_ACCEPTS:
        blockers.append("unsafe_accept_threshold_above_safety_floor")
    if (
        compliance_macro_recall_threshold is None
        or compliance_macro_recall_threshold < MIN_COMPLIANCE_MACRO_RECALL
    ):
        blockers.append("compliance_macro_recall_threshold_below_safety_floor")
    if (
        citation_precision_threshold is None
        or citation_precision_threshold < MIN_CITATION_PRECISION
    ):
        blockers.append("citation_precision_threshold_below_safety_floor")
    if (
        citation_recall_threshold is None
        or citation_recall_threshold < MIN_CITATION_RECALL
    ):
        blockers.append("citation_recall_threshold_below_safety_floor")
    if (
        information_gap_threshold is None
        or information_gap_threshold < MIN_INFORMATION_GAP_COMPLETENESS
    ):
        blockers.append("information_gap_threshold_below_safety_floor")
    if (
        sub_judgment_threshold is None
        or sub_judgment_threshold < MIN_SUB_JUDGMENT_EXACT_MATCH
    ):
        blockers.append("sub_judgment_threshold_below_safety_floor")

    revisions = manifest.get("revisions") if isinstance(manifest.get("revisions"), dict) else {}
    if not code_sha or str(revisions.get("code_sha") or "") != code_sha:
        blockers.append("code_revision_mismatch")
    if str(revisions.get("config_sha256") or "").lower() != config_sha256:
        blockers.append("config_revision_mismatch")
    if not isinstance(revisions.get("prompt_versions"), list) or not revisions.get("prompt_versions"):
        blockers.append("prompt_revisions_missing")
    if not isinstance(revisions.get("model_versions"), list) or not revisions.get("model_versions"):
        blockers.append("model_revisions_missing")

    privacy = manifest.get("privacy") if isinstance(manifest.get("privacy"), dict) else {}
    if privacy.get("hmac_pseudonymized_ids") is not True:
        blockers.append("privacy_hmac_pseudonyms_not_required")
    if privacy.get("text_committed_to_repo") is not False:
        blockers.append("privacy_allows_text_in_repo")

    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    if not isinstance(payload.get("cases"), list):
        blockers.append("payload_cases_not_array")
    seen_case_ids: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            blockers.append(f"case_{index}:not_object")
            continue
        case_allowed = {"case_id", "project_id", "document_id", "input", "gold", "reviews", "adjudication"}
        blockers.extend(
            f"case_{index}:unknown_field:{field}" for field in _unknown_fields(case, case_allowed)
        )
        for key in ("case_id", "project_id", "document_id"):
            identifier = str(case.get(key) or "").strip().lower()
            if not HMAC_ID_PATTERN.fullmatch(identifier):
                blockers.append(f"case_{index}:{key}_not_hmac")
        case_id = str(case.get("case_id") or "").strip().lower()
        if case_id in seen_case_ids:
            blockers.append(f"case_{index}:duplicate_case_id")
        seen_case_ids.add(case_id)
        case_input = case.get("input") if isinstance(case.get("input"), dict) else {}
        blockers.extend(
            f"case_{index}:input_unknown_field:{field}"
            for field in _unknown_fields(case_input, {"requirement_row", "knowledge_docs"})
        )
        if not isinstance(case_input.get("requirement_row"), dict):
            blockers.append(f"case_{index}:requirement_row_missing")
        if not isinstance(case_input.get("knowledge_docs", []), list):
            blockers.append(f"case_{index}:knowledge_docs_not_array")
        gold = case.get("gold") if isinstance(case.get("gold"), dict) else {}
        blockers.extend(
            f"case_{index}:gold_unknown_field:{field}"
            for field in _unknown_fields(
                gold,
                {
                    "analysis_label",
                    "final_decision",
                    "compliance_status",
                    "expected_evidence_ids",
                    "required_missing_items",
                    "expected_sub_judgments",
                    "assessment_contract_sha256",
                },
            )
        )
        analysis_label = _normalized_label(gold.get("analysis_label"))
        final_decision = _normalized_label(gold.get("final_decision"))
        compliance_status = _normalized_label(gold.get("compliance_status"))
        if not analysis_label and not final_decision:
            blockers.append(f"case_{index}:gold_label_missing")
        if analysis_label and analysis_label not in EXPLICIT_LABELS:
            blockers.append(f"case_{index}:invalid_analysis_label")
        if final_decision and final_decision not in DECISION_LABELS:
            blockers.append(f"case_{index}:invalid_final_decision")
        if final_decision and analysis_label == "INFO":
            blockers.append(f"case_{index}:info_label_cannot_have_final_decision")
        if final_decision:
            if compliance_status not in COMPLIANCE_STATUSES:
                blockers.append(
                    f"case_{index}:compliance_status_missing_or_invalid"
                )
            elif (
                DECISION_TO_COMPLIANCE_STATUS.get(final_decision)
                != compliance_status
            ):
                blockers.append(
                    f"case_{index}:compliance_decision_projection_mismatch"
                )
            expected_evidence_ids = gold.get("expected_evidence_ids")
            if not isinstance(expected_evidence_ids, list) or any(
                not str(item or "").strip() for item in expected_evidence_ids
            ):
                blockers.append(
                    f"case_{index}:expected_evidence_ids_invalid"
                )
            elif len(expected_evidence_ids) != len(
                {str(item).strip() for item in expected_evidence_ids}
            ):
                blockers.append(
                    f"case_{index}:expected_evidence_ids_duplicate"
                )
            required_missing_items = gold.get("required_missing_items")
            if not isinstance(required_missing_items, list) or any(
                not isinstance(item, dict)
                or not HMAC_ID_PATTERN.fullmatch(
                    str(item.get("missing_item_id") or "").strip().lower()
                )
                or not str(item.get("item") or "").strip()
                or set(item) - {"missing_item_id", "item"}
                for item in required_missing_items
            ):
                blockers.append(
                    f"case_{index}:required_missing_items_invalid"
                )
            elif len(required_missing_items) != len(
                {
                    str(item.get("missing_item_id") or "").strip().lower()
                    for item in required_missing_items
                }
            ):
                blockers.append(
                    f"case_{index}:required_missing_items_duplicate"
                )
            elif len(required_missing_items) != len(
                {
                    _normalized_contract_text(item.get("item"))
                    for item in required_missing_items
                }
            ):
                blockers.append(
                    f"case_{index}:required_missing_item_text_duplicate"
                )
            expected_sub_judgments = gold.get(
                "expected_sub_judgments"
            )
            if (
                not isinstance(expected_sub_judgments, list)
                or not expected_sub_judgments
                or any(
                    not isinstance(item, dict)
                    or not HMAC_ID_PATTERN.fullmatch(
                        str(
                            item.get("gold_sub_judgment_id") or ""
                        ).strip().lower()
                    )
                    or not str(item.get("statement") or "").strip()
                    or _normalized_label(item.get("compliance_status"))
                    not in COMPLIANCE_STATUSES
                    or set(item)
                    - {
                        "gold_sub_judgment_id",
                        "statement",
                        "compliance_status",
                    }
                    for item in expected_sub_judgments
                )
            ):
                blockers.append(
                    f"case_{index}:expected_sub_judgments_invalid"
                )
            elif len(expected_sub_judgments) != len(
                {
                    str(
                        item.get("gold_sub_judgment_id") or ""
                    ).strip().lower()
                    for item in expected_sub_judgments
                }
            ):
                blockers.append(
                    f"case_{index}:expected_sub_judgments_duplicate"
                )
            elif len(expected_sub_judgments) != len(
                {
                    _normalized_contract_text(item.get("statement"))
                    for item in expected_sub_judgments
                }
            ):
                blockers.append(
                    f"case_{index}:expected_sub_judgment_statement_duplicate"
                )
            contract_hash = str(
                gold.get("assessment_contract_sha256") or ""
            ).strip().lower()
            if (
                not HMAC_ID_PATTERN.fullmatch(contract_hash)
                or contract_hash != assessment_contract_sha256(gold)
            ):
                blockers.append(
                    f"case_{index}:assessment_contract_hash_invalid"
                )
            if (
                compliance_status == "INSUFFICIENT_INFORMATION"
                and isinstance(required_missing_items, list)
                and not required_missing_items
            ):
                blockers.append(
                    f"case_{index}:insufficient_information_missing_gap_truth"
                )
            if (
                compliance_status != "INSUFFICIENT_INFORMATION"
                and isinstance(expected_evidence_ids, list)
                and not expected_evidence_ids
            ):
                blockers.append(
                    f"case_{index}:decisive_status_missing_evidence_truth"
                )
        blockers.extend(_validate_label_provenance(case, index))
        if required_reviewers is not None and isinstance(case.get("reviews"), list):
            if len(case["reviews"]) < required_reviewers:
                blockers.append(
                    f"case_{index}:insufficient_reviews:{len(case['reviews'])}/{required_reviewers}"
                )

    coverage = _coverage_from_cases(cases)
    if manifest.get("coverage") != coverage:
        blockers.append("manifest_coverage_mismatch")
    explicit_coverage = coverage["explicit_requirement"]
    if explicit_coverage["label_counts"]["REQUIREMENT"] < MIN_EXPLICIT_POSITIVES:
        blockers.append(
            "insufficient_explicit_positive_labels:"
            f"{explicit_coverage['label_counts']['REQUIREMENT']}/{MIN_EXPLICIT_POSITIVES}"
        )
    if explicit_coverage["label_counts"]["INFO"] < MIN_EXPLICIT_NEGATIVES:
        blockers.append(
            "insufficient_explicit_negative_labels:"
            f"{explicit_coverage['label_counts']['INFO']}/{MIN_EXPLICIT_NEGATIVES}"
        )
    if explicit_coverage["document_count"] < MIN_LABELED_DOCUMENTS:
        blockers.append(
            f"insufficient_explicit_documents:{explicit_coverage['document_count']}/{MIN_LABELED_DOCUMENTS}"
        )
    if explicit_coverage["project_count"] < MIN_LABELED_PROJECTS:
        blockers.append(
            f"insufficient_explicit_projects:{explicit_coverage['project_count']}/{MIN_LABELED_PROJECTS}"
        )

    decision_coverage = coverage["decision"]
    if decision_coverage["labeled"] < MIN_DECISION_CASES:
        blockers.append(f"insufficient_decision_cases:{decision_coverage['labeled']}/{MIN_DECISION_CASES}")
    for label in DECISION_LABELS:
        count = decision_coverage["class_counts"][label]
        if count < MIN_DECISION_CLASS_CASES:
            blockers.append(f"insufficient_decision_class:{label}:{count}/{MIN_DECISION_CLASS_CASES}")
    if decision_coverage["document_count"] < MIN_LABELED_DOCUMENTS:
        blockers.append(
            f"insufficient_decision_documents:{decision_coverage['document_count']}/{MIN_LABELED_DOCUMENTS}"
        )
    if decision_coverage["project_count"] < MIN_LABELED_PROJECTS:
        blockers.append(
            f"insufficient_decision_projects:{decision_coverage['project_count']}/{MIN_LABELED_PROJECTS}"
        )
    return sorted(set(blockers)), coverage


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def build_evaluation_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    explicit_records = [record for record in records if record.get("gold_analysis_label")]
    tp = sum(
        record["gold_analysis_label"] == "REQUIREMENT" and record.get("predicted_analysis_label") == "REQUIREMENT"
        for record in explicit_records
    )
    fn = sum(
        record["gold_analysis_label"] == "REQUIREMENT" and record.get("predicted_analysis_label") != "REQUIREMENT"
        for record in explicit_records
    )
    tn = sum(
        record["gold_analysis_label"] == "INFO" and record.get("predicted_analysis_label") == "INFO"
        for record in explicit_records
    )
    fp = sum(
        record["gold_analysis_label"] == "INFO" and record.get("predicted_analysis_label") != "INFO"
        for record in explicit_records
    )

    decision_records = [record for record in records if record.get("gold_final_decision")]
    confusion = {
        gold: {predicted: 0 for predicted in (*DECISION_LABELS, "NO_DECISION", "EXECUTION_ERROR")}
        for gold in DECISION_LABELS
    }
    for record in decision_records:
        gold = record["gold_final_decision"]
        predicted = record.get("predicted_final_decision") or "NO_DECISION"
        if predicted not in confusion[gold]:
            predicted = "EXECUTION_ERROR"
        confusion[gold][predicted] += 1
    class_recall = {
        label: _ratio(confusion[label][label], sum(confusion[label].values())) for label in DECISION_LABELS
    }
    available_recalls = [value for value in class_recall.values() if value is not None]
    unsafe_accepts = sum(
        confusion[gold]["ACCEPT"] for gold in DECISION_LABELS if gold != "ACCEPT"
    )
    execution_errors = sum(record.get("execution_status") == "ERROR" for record in records)
    shadow_degraded = sum(record.get("shadow_degraded") == "true" for record in records)
    compliance_records = [
        record for record in records if record.get("gold_compliance_status")
    ]
    compliance_confusion = {
        gold: {
            predicted: 0
            for predicted in (
                *COMPLIANCE_STATUSES,
                "NO_STATUS",
                "EXECUTION_ERROR",
            )
        }
        for gold in COMPLIANCE_STATUSES
    }
    for record in compliance_records:
        gold_status = str(record["gold_compliance_status"])
        predicted_status = (
            str(record.get("predicted_compliance_status") or "")
            or "NO_STATUS"
        )
        if predicted_status not in compliance_confusion[gold_status]:
            predicted_status = "EXECUTION_ERROR"
        compliance_confusion[gold_status][predicted_status] += 1
    compliance_class_recall = {
        status: _ratio(
            compliance_confusion[status][status],
            sum(compliance_confusion[status].values()),
        )
        for status in COMPLIANCE_STATUSES
    }
    compliance_recalls = [
        value
        for value in compliance_class_recall.values()
        if value is not None
    ]
    citation_tp = sum(int(record.get("citation_tp") or 0) for record in compliance_records)
    citation_fp = sum(int(record.get("citation_fp") or 0) for record in compliance_records)
    citation_fn = sum(int(record.get("citation_fn") or 0) for record in compliance_records)
    gap_records = [
        record
        for record in compliance_records
        if record.get("gold_requires_information_gap") is True
    ]
    sub_judgment_records = [
        record
        for record in compliance_records
        if record.get("sub_judgment_evaluated") is True
    ]
    return {
        "explicit_requirement": {
            "evaluated": len(explicit_records),
            "confusion": {"tp": tp, "fn": fn, "tn": tn, "fp": fp},
            "recall": _ratio(tp, tp + fn),
            "info_specificity": _ratio(tn, tn + fp),
        },
        "decision_safety": {
            "evaluated": len(decision_records),
            "confusion": confusion,
            "exact_accuracy": _ratio(
                sum(confusion[label][label] for label in DECISION_LABELS), len(decision_records)
            ),
            "class_recall": class_recall,
            "macro_recall": round(sum(available_recalls) / len(available_recalls), 6)
            if available_recalls
            else None,
            "unsafe_accepts": unsafe_accepts,
        },
        "assessment_contract": {
            "evaluated": len(compliance_records),
            "compliance_confusion": compliance_confusion,
            "compliance_class_recall": compliance_class_recall,
            "compliance_macro_recall": (
                round(
                    sum(compliance_recalls) / len(compliance_recalls),
                    6,
                )
                if compliance_recalls
                else None
            ),
            "citation": {
                "true_positive": citation_tp,
                "false_positive": citation_fp,
                "false_negative": citation_fn,
                "precision": _ratio(
                    citation_tp,
                    citation_tp + citation_fp,
                ),
                "recall": _ratio(
                    citation_tp,
                    citation_tp + citation_fn,
                ),
            },
            "information_gap": {
                "evaluated": len(gap_records),
                "complete": sum(
                    record.get("information_gap_complete") is True
                    for record in gap_records
                ),
                "completeness": _ratio(
                    sum(
                        record.get("information_gap_complete") is True
                        for record in gap_records
                    ),
                    len(gap_records),
                ),
            },
            "sub_judgments": {
                "evaluated": len(sub_judgment_records),
                "exact_matches": sum(
                    record.get("sub_judgment_exact_match") is True
                    for record in sub_judgment_records
                ),
                "exact_match_rate": _ratio(
                    sum(
                        record.get("sub_judgment_exact_match") is True
                        for record in sub_judgment_records
                    ),
                    len(sub_judgment_records),
                ),
            },
        },
        "execution_errors": execution_errors,
        "shadow_degraded": shadow_degraded,
    }


def evaluate_cases(
    *,
    cases: list[dict[str, Any]],
    config: AICopilotConfig,
    scope: str,
    assessment_runner: Callable[..., dict[str, Any]] = run_ai_copilot_assessment,
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Run product code and retain no raw text or stable case identifier."""

    records: list[dict[str, Any]] = []
    model_versions: set[str] = set()
    prompt_versions: set[str] = set()
    for case in cases:
        gold = case.get("gold") or {}
        has_explicit = bool(_normalized_label(gold.get("analysis_label")))
        has_decision = bool(_normalized_label(gold.get("final_decision")))
        case_input = case.get("input") or {}
        row = dict(case_input.get("requirement_row") or {})
        knowledge_docs = list(case_input.get("knowledge_docs") or [])

        def run_stage(*, explicit_stage: bool) -> None:
            gold_evidence_ids = {
                str(item).strip()
                for item in gold.get("expected_evidence_ids") or []
                if str(item).strip()
            }
            gold_sub_judgments = {
                _normalized_contract_text(item.get("statement")):
                _normalized_label(item.get("compliance_status"))
                for item in gold.get("expected_sub_judgments") or []
                if isinstance(item, dict)
                and _normalized_contract_text(item.get("statement"))
            }
            gold_missing_items = {
                _normalized_contract_text(item.get("item"))
                for item in gold.get("required_missing_items") or []
                if isinstance(item, dict)
                and _normalized_contract_text(item.get("item"))
            }
            record = {
                "gold_analysis_label": _normalized_label(gold.get("analysis_label")) if explicit_stage else "",
                "gold_final_decision": _normalized_label(gold.get("final_decision")) if not explicit_stage else "",
                "gold_compliance_status": (
                    _normalized_label(gold.get("compliance_status"))
                    if not explicit_stage
                    else ""
                ),
                "predicted_analysis_label": "",
                "predicted_final_decision": "",
                "predicted_compliance_status": "",
                "citation_tp": 0,
                "citation_fp": 0,
                "citation_fn": len(gold_evidence_ids) if not explicit_stage else 0,
                "gold_requires_information_gap": bool(
                    not explicit_stage
                    and _normalized_label(gold.get("compliance_status"))
                    == "INSUFFICIENT_INFORMATION"
                ),
                "information_gap_complete": False,
                "sub_judgment_evaluated": bool(
                    not explicit_stage and gold_sub_judgments
                ),
                "sub_judgment_exact_match": False,
                "execution_status": "OK",
                "shadow_degraded": "false",
            }
            try:
                result = assessment_runner(
                    row,
                    [] if explicit_stage else knowledge_docs,
                    config=config,
                    forced_analysis_label=None if explicit_stage else "REQUIREMENT",
                    forced_analysis_basis="" if explicit_stage else "Gold evaluation isolates the decision stage.",
                )
                assessment = result.get("assessment") or {}
                if assessment.get("model_name"):
                    model_versions.add(str(assessment["model_name"]))
                if assessment.get("prompt_version"):
                    prompt_versions.add(str(assessment["prompt_version"]))
                for run in result.get("runs") or []:
                    if run.get("model_name"):
                        model_versions.add(str(run["model_name"]))
                    if run.get("prompt_version"):
                        prompt_versions.add(str(run["prompt_version"]))
                verdict = evaluate_assessment_execution(
                    assessment=assessment,
                    runs=list(result.get("runs") or []),
                    configured_mode=config.assessment_mode,
                )
                if not verdict.serviceable:
                    record["execution_status"] = "ERROR"
                    record["predicted_analysis_label"] = "EXECUTION_ERROR"
                    record["predicted_final_decision"] = "EXECUTION_ERROR"
                    record["predicted_compliance_status"] = "EXECUTION_ERROR"
                else:
                    record["predicted_analysis_label"] = verdict.analysis_label
                    record["predicted_final_decision"] = verdict.ai_decision
                    if not explicit_stage:
                        record["predicted_compliance_status"] = _normalized_label(
                            assessment.get("compliance_status")
                        )
                        predicted_evidence_ids = {
                            str(
                                item.get("retrieval_evidence_id") or ""
                            ).strip()
                            for item in assessment.get(
                                "evidence_findings"
                            )
                            or []
                            if isinstance(item, dict)
                            and str(
                                item.get("finding") or ""
                            ).strip().upper()
                            in {
                                "SUPPORTS",
                                "PARTIALLY_SUPPORTS",
                                "CONTRADICTS",
                            }
                            and str(
                                item.get("retrieval_evidence_id") or ""
                            ).strip()
                        }
                        record["citation_tp"] = len(
                            predicted_evidence_ids & gold_evidence_ids
                        )
                        record["citation_fp"] = len(
                            predicted_evidence_ids - gold_evidence_ids
                        )
                        record["citation_fn"] = len(
                            gold_evidence_ids - predicted_evidence_ids
                        )
                        gap = (
                            assessment.get("structured_information_gap")
                            if isinstance(
                                assessment.get(
                                    "structured_information_gap"
                                ),
                                dict,
                            )
                            else {}
                        )
                        missing_items = (
                            gap.get("missing_items")
                            if isinstance(gap.get("missing_items"), list)
                            else []
                        )
                        questions = (
                            gap.get("clarification_questions")
                            if isinstance(
                                gap.get("clarification_questions"),
                                list,
                            )
                            else []
                        )
                        predicted_missing_items = {
                            _normalized_contract_text(item.get("item")): item
                            for item in missing_items
                            if isinstance(item, dict)
                            and _normalized_contract_text(item.get("item"))
                        }
                        matched_missing_items = [
                            predicted_missing_items[item]
                            for item in gold_missing_items
                            if item in predicted_missing_items
                        ]
                        record["information_gap_complete"] = bool(
                            gold_missing_items
                            and gold_missing_items.issubset(
                                predicted_missing_items
                            )
                            and len(matched_missing_items)
                            == len(gold_missing_items)
                            and all(
                                isinstance(item, dict)
                                and str(item.get("item") or "").strip()
                                and str(
                                    item.get("blocking_reason") or ""
                                ).strip()
                                and str(
                                    item.get("required_input") or ""
                                ).strip()
                                for item in matched_missing_items
                            )
                            and any(str(item or "").strip() for item in questions)
                        )
                        predicted_sub_items = [
                            item
                            for item in assessment.get("sub_judgments") or []
                            if isinstance(item, dict)
                            and _normalized_contract_text(
                                item.get("statement")
                            )
                            and _normalized_label(
                                item.get("compliance_status")
                            )
                        ]
                        predicted_sub_judgments = {
                            _normalized_contract_text(
                                item.get("statement")
                            ): _normalized_label(
                                item.get("compliance_status")
                            )
                            for item in predicted_sub_items
                        }
                        record["sub_judgment_exact_match"] = (
                            predicted_sub_judgments
                            == gold_sub_judgments
                            and len(predicted_sub_items)
                            == len(predicted_sub_judgments)
                        )
                    record["shadow_degraded"] = "true" if verdict.shadow_degraded else "false"
            except Exception:
                # Exception messages can contain provider content or source data.
                record["execution_status"] = "ERROR"
                record["predicted_analysis_label"] = "EXECUTION_ERROR"
                record["predicted_final_decision"] = "EXECUTION_ERROR"
                record["predicted_compliance_status"] = "EXECUTION_ERROR"
            records.append(record)

        if has_explicit and scope in {"all", "explicit"}:
            run_stage(explicit_stage=True)
        if has_decision and scope in {"all", "decision"}:
            run_stage(explicit_stage=False)
    return records, model_versions, prompt_versions


def _evaluate_policy(metrics: dict[str, Any], manifest: dict[str, Any], *, scope: str) -> list[str]:
    blockers: list[str] = []
    policy = manifest.get("evaluation_policy") or {}
    if scope in {"all", "explicit"}:
        explicit = metrics["explicit_requirement"]
        explicit_policy = policy.get("explicit_requirement") or {}
        if explicit["recall"] is None or explicit["recall"] < float(explicit_policy.get("min_recall") or 0):
            blockers.append("explicit_requirement_recall_below_threshold")
        if explicit["info_specificity"] is None or explicit["info_specificity"] < float(
            explicit_policy.get("min_info_specificity") or 0
        ):
            blockers.append("info_specificity_below_threshold")
    if scope in {"all", "decision"}:
        decision = metrics["decision_safety"]
        decision_policy = policy.get("decision_safety") or {}
        if decision["macro_recall"] is None or decision["macro_recall"] < float(
            decision_policy.get("min_macro_recall") or 0
        ):
            blockers.append("decision_macro_recall_below_threshold")
        minimum_class_recall = float(decision_policy.get("min_per_class_recall") or 0)
        if any(value is None or value < minimum_class_recall for value in decision["class_recall"].values()):
            blockers.append("decision_class_recall_below_threshold")
        if decision["unsafe_accepts"] > int(decision_policy.get("max_unsafe_accepts") or 0):
            blockers.append("unsafe_accepts_above_threshold")
        assessment_contract = metrics["assessment_contract"]
        contract_policy = policy.get("assessment_contract") or {}
        if (
            assessment_contract["compliance_macro_recall"] is None
            or assessment_contract["compliance_macro_recall"]
            < float(
                contract_policy.get("min_compliance_macro_recall") or 0
            )
        ):
            blockers.append("compliance_macro_recall_below_threshold")
        citation = assessment_contract["citation"]
        if (
            citation["precision"] is None
            or citation["precision"]
            < float(contract_policy.get("min_citation_precision") or 0)
        ):
            blockers.append("citation_precision_below_threshold")
        if (
            citation["recall"] is None
            or citation["recall"]
            < float(contract_policy.get("min_citation_recall") or 0)
        ):
            blockers.append("citation_recall_below_threshold")
        information_gap = assessment_contract["information_gap"]
        if (
            information_gap["completeness"] is None
            or information_gap["completeness"]
            < float(
                contract_policy.get(
                    "min_information_gap_completeness"
                )
                or 0
            )
        ):
            blockers.append("information_gap_completeness_below_threshold")
        sub_judgments = assessment_contract["sub_judgments"]
        if (
            sub_judgments["exact_match_rate"] is None
            or sub_judgments["exact_match_rate"]
            < float(
                contract_policy.get("min_sub_judgment_exact_match") or 0
            )
        ):
            blockers.append("sub_judgment_exact_match_below_threshold")
    if metrics["execution_errors"]:
        blockers.append(f"evaluation_execution_errors:{metrics['execution_errors']}")
    return blockers


def _revision_blockers(
    manifest: dict[str, Any], *, observed_models: set[str], observed_prompts: set[str], scope: str
) -> list[str]:
    revisions = manifest.get("revisions") or {}
    expected_models = {str(item) for item in revisions.get("model_versions") or []}
    expected_prompts = {str(item) for item in revisions.get("prompt_versions") or []}
    blockers: list[str] = []
    if (scope == "all" and expected_models != observed_models) or (
        scope != "all" and (not observed_models or not observed_models.issubset(expected_models))
    ):
        blockers.append("observed_model_revisions_mismatch")
    if (scope == "all" and expected_prompts != observed_prompts) or (
        scope != "all" and (not observed_prompts or not observed_prompts.issubset(expected_prompts))
    ):
        blockers.append("observed_prompt_revisions_mismatch")
    return blockers


def _load_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value, raw


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run aggregate-only Assessment Gold evaluation.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", default="")
    parser.add_argument("--mode", choices=("heuristic", "llm_shadow", "llm_primary"), default="heuristic")
    parser.add_argument("--scope", choices=("all", "explicit", "decision"), default="all")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help="Exit non-zero unless contract, clean revision, execution and approved thresholds all pass.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_dir = PROJECT_DIR
    code_sha, dirty = git_revision(project_dir)
    try:
        manifest, _ = _load_json(args.manifest.expanduser().resolve())
        payload, payload_raw = _load_json(args.payload.expanduser().resolve())
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "overall_pass": False,
            "blockers": [f"input_error:{type(exc).__name__}"],
        }
        return _emit(report, output=args.output, require_pass=args.require_pass)

    profiles = load_ai_profiles()
    requested_profile = str(args.profile or "").strip().lower()
    profile_missing = bool(requested_profile and requested_profile not in profiles)
    base_config = profiles.get(requested_profile) if requested_profile else load_ai_copilot_config()
    if base_config is None:
        base_config = load_ai_copilot_config()
    with tempfile.TemporaryDirectory(prefix="reqpilot-ai-gold-") as temp_dir:
        config = dataclasses.replace(
            base_config,
            assessment_mode=args.mode,
            retrieval_backend="local",
            trace_dir=Path(temp_dir) / "traces",
        )
        config_sha256, safe_config = build_config_fingerprint(config)
        payload_sha256 = sha256_bytes(payload_raw)
        blockers, coverage = validate_gold_contract(
            manifest=manifest,
            payload=payload,
            payload_sha256=payload_sha256,
            code_sha=code_sha,
            config_sha256=config_sha256,
        )
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "dataset": {
                "dataset_id": str(manifest.get("dataset_id") or ""),
                "version": str(manifest.get("version") or ""),
                "payload_sha256": payload_sha256,
                "coverage": coverage,
            },
            "revision": {
                "code_sha": code_sha,
                "working_tree_dirty": dirty,
                "config_sha256": config_sha256,
                "safe_config": safe_config,
            },
            "scope": args.scope,
            "environment": {
                "python": platform.python_version(),
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "validate_only": args.validate_only,
            "contract_ready": not blockers,
            "synthetic_regression_is_gold": False,
            "blockers": list(blockers),
        }
        if dirty and args.require_pass:
            report["blockers"].append("working_tree_dirty")
        if profile_missing:
            report["blockers"].append("selected_ai_profile_not_found")
        if args.mode == "llm_shadow" and not args.validate_only:
            report["blockers"].append(
                "llm_shadow_does_not_expose_candidate_for_quality_gate"
            )

        # A structurally invalid or unapproved dataset is never executed.
        if not report["blockers"] and not args.validate_only:
            if args.mode == "llm_primary":
                try:
                    ensure_ai_profile_runnable(config)
                except RuntimeError:
                    report["blockers"].append("selected_ai_profile_not_runnable")
            if not report["blockers"]:
                records, models, prompts = evaluate_cases(
                    cases=list(payload.get("cases") or []),
                    config=config,
                    scope=args.scope,
                )
                metrics = build_evaluation_metrics(records)
                report["metrics"] = metrics
                report["observed_revisions"] = {
                    "model_versions": sorted(models),
                    "prompt_versions": sorted(prompts),
                }
                report["blockers"].extend(
                    _revision_blockers(
                        manifest,
                        observed_models=models,
                        observed_prompts=prompts,
                        scope=args.scope,
                    )
                )
                report["blockers"].extend(_evaluate_policy(metrics, manifest, scope=args.scope))

        report["blockers"] = sorted(set(report["blockers"]))
        report["overall_pass"] = not report["blockers"] and not args.validate_only
        return _emit(report, output=args.output, require_pass=args.require_pass)


def _emit(report: dict[str, Any], *, output: Path | None, require_pass: bool) -> int:
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output:
        resolved = output.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 2 if require_pass and not report.get("overall_pass") else 0


if __name__ == "__main__":
    raise SystemExit(main())
