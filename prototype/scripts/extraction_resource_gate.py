from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.extraction_case_runner import (
    CorpusPathError,
    require_report_destination_outside_corpus,
)
from scripts.extraction_resource_probe import (
    RESOURCE_PROBE_SCHEMA_VERSION,
    canonical_resource_report_sha256,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
RESOURCE_POLICY_SCHEMA_VERSION = "extraction-resource-policy/v1"
RESOURCE_GATE_SCHEMA_VERSION = "extraction-resource-gate/v1"

RESOURCE_GATE_PASS = "PASS"
RESOURCE_GATE_FAIL = "FAIL"
RESOURCE_GATE_NOT_EVALUATED = "NOT_EVALUATED"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,127}$")
_DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PYTHON_VERSION_PREFIX_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}$")

_RESOURCE_REPORT_TOP_LEVEL_FIELDS = {
    "schema_version",
    "generated_at",
    "measurement_scope",
    "runtime",
    "document",
    "pipeline",
    "measurements",
    "privacy",
    "report_sha256",
}
_STAGE_METRIC_FIELDS = {
    "wall_ms",
    "user_cpu_ms",
    "system_cpu_ms",
    "cpu_ms",
    "process_peak_rss_bytes_after_stage",
    "process_peak_rss_growth_bytes_during_stage",
}


class ExtractionResourceGateRejected(RuntimeError):
    """Fail-closed policy, report, or output rejection."""

    def __init__(self, code: str) -> None:
        token = str(code or "").strip()
        if not token:
            raise ValueError("resource gate rejection code must not be blank")
        self.code = token
        super().__init__(token)


def canonical_resource_policy_sha256(policy: dict[str, Any]) -> str:
    return _canonical_json_sha256(policy)


def canonical_resource_gate_sha256(report: dict[str, Any]) -> str:
    payload = dict(report)
    payload.pop("gate_report_sha256", None)
    return _canonical_json_sha256(payload)


def validate_resource_policy(policy: dict[str, Any]) -> None:
    _require_object_fields(
        policy,
        {
            "schema_version",
            "policy_id",
            "revision",
            "approval",
            "deployment_profile",
            "pipeline",
            "cohort",
            "limits",
        },
        "resource_policy_shape_invalid",
    )
    if policy["schema_version"] != RESOURCE_POLICY_SCHEMA_VERSION:
        _reject("resource_policy_schema_version_mismatch")
    _require_token(policy["policy_id"], "resource_policy_id_invalid")
    _require_positive_int(policy["revision"], "resource_policy_revision_invalid")

    approval = policy["approval"]
    _require_object_fields(
        approval,
        {"status", "authority", "approved_by", "approved_at"},
        "resource_policy_approval_shape_invalid",
    )
    status = approval["status"]
    if status not in {"DRAFT", "APPROVED"}:
        _reject("resource_policy_approval_status_invalid")
    if approval["authority"] != "DEPLOYMENT_OWNER":
        _reject("resource_policy_approval_authority_invalid")
    if status == "APPROVED":
        _require_nonempty_string(
            approval["approved_by"],
            "resource_policy_approver_missing",
            maximum_length=256,
        )
        _require_timestamp(
            approval["approved_at"],
            "resource_policy_approval_timestamp_invalid",
        )
    elif approval["approved_by"] is not None or approval["approved_at"] is not None:
        _reject("resource_policy_draft_must_not_claim_approval")

    deployment = policy["deployment_profile"]
    _require_object_fields(
        deployment,
        {
            "profile_id",
            "platform_system",
            "machine",
            "python_version_prefix",
        },
        "resource_policy_deployment_shape_invalid",
    )
    _require_token(
        deployment["profile_id"],
        "resource_policy_deployment_profile_invalid",
    )
    _require_token(
        deployment["platform_system"],
        "resource_policy_platform_invalid",
    )
    _require_token(deployment["machine"], "resource_policy_machine_invalid")
    python_prefix = deployment["python_version_prefix"]
    if not isinstance(python_prefix, str) or python_prefix != python_prefix.strip():
        _reject("resource_policy_python_version_prefix_invalid")
    if _PYTHON_VERSION_PREFIX_PATTERN.fullmatch(python_prefix) is None:
        _reject("resource_policy_python_version_prefix_invalid")

    pipeline = policy["pipeline"]
    _require_object_fields(
        pipeline,
        {"source_ir", "structure"},
        "resource_policy_pipeline_shape_invalid",
    )
    _validate_policy_component(
        pipeline["source_ir"],
        id_field="adapter_id",
        rejection_prefix="resource_policy_source_ir",
    )
    _validate_policy_component(
        pipeline["structure"],
        id_field="resolver_id",
        rejection_prefix="resource_policy_structure",
    )

    cohort = policy["cohort"]
    _require_object_fields(
        cohort,
        {"documents"},
        "resource_policy_cohort_shape_invalid",
    )
    documents = cohort["documents"]
    if not isinstance(documents, list) or not documents:
        _reject("resource_policy_documents_missing")
    seen_document_ids: set[str] = set()
    seen_source_hashes: set[str] = set()
    for document in documents:
        _require_object_fields(
            document,
            {"document_id", "source_sha256", "minimum_runs"},
            "resource_policy_document_shape_invalid",
        )
        document_id = document["document_id"]
        if not isinstance(document_id, str) or document_id != document_id.strip():
            _reject("resource_policy_document_id_invalid")
        if _DOCUMENT_ID_PATTERN.fullmatch(document_id) is None:
            _reject("resource_policy_document_id_invalid")
        source_sha256 = document["source_sha256"]
        if (
            not isinstance(source_sha256, str)
            or source_sha256 != source_sha256.strip().lower()
        ):
            _reject("resource_policy_document_hash_invalid")
        if _SHA256_PATTERN.fullmatch(source_sha256) is None:
            _reject("resource_policy_document_hash_invalid")
        minimum_runs = _require_positive_int(
            document["minimum_runs"],
            "resource_policy_minimum_runs_invalid",
        )
        if minimum_runs < 3 or minimum_runs > 100:
            _reject("resource_policy_minimum_runs_invalid")
        if document_id in seen_document_ids:
            _reject("resource_policy_document_id_duplicate")
        if source_sha256 in seen_source_hashes:
            _reject("resource_policy_source_hash_duplicate")
        seen_document_ids.add(document_id)
        seen_source_hashes.add(source_sha256)

    limits = policy["limits"]
    _require_object_fields(
        limits,
        {"total_wall_ms", "process_peak_rss_bytes"},
        "resource_policy_limits_shape_invalid",
    )
    _validate_limit(
        limits["total_wall_ms"],
        integer_capacity=False,
        rejection_prefix="resource_policy_wall",
    )
    _validate_limit(
        limits["process_peak_rss_bytes"],
        integer_capacity=True,
        rejection_prefix="resource_policy_rss",
    )


def validate_resource_probe_report(report: dict[str, Any]) -> None:
    _require_object_fields(
        report,
        _RESOURCE_REPORT_TOP_LEVEL_FIELDS,
        "resource_gate_probe_shape_invalid",
    )
    if report["schema_version"] != RESOURCE_PROBE_SCHEMA_VERSION:
        _reject("resource_gate_probe_schema_version_mismatch")
    _require_timestamp(
        report["generated_at"],
        "resource_gate_probe_timestamp_invalid",
    )
    report_sha256 = report["report_sha256"]
    if (
        not isinstance(report_sha256, str)
        or report_sha256 != report_sha256.strip().lower()
    ):
        _reject("resource_gate_probe_hash_invalid")
    if _SHA256_PATTERN.fullmatch(report_sha256) is None:
        _reject("resource_gate_probe_hash_invalid")
    if canonical_resource_report_sha256(report) != report_sha256:
        _reject("resource_gate_probe_hash_mismatch")

    scope = report["measurement_scope"]
    _require_object_fields(
        scope,
        {"pipeline", "visual_provider", "process_mode", "rss_semantics"},
        "resource_gate_probe_scope_shape_invalid",
    )
    if scope != {
        "pipeline": "PREFLIGHT_SOURCE_IR_STRUCTURE",
        "visual_provider": "off",
        "process_mode": "DEDICATED_CLI_PROCESS",
        "rss_semantics": "PROCESS_CUMULATIVE_HIGH_WATER_MARK",
    }:
        _reject("resource_gate_probe_scope_invalid")

    runtime = report["runtime"]
    _require_object_fields(
        runtime,
        {"python_version", "platform_system", "machine"},
        "resource_gate_probe_runtime_shape_invalid",
    )
    _require_runtime_value(
        runtime["python_version"],
        "resource_gate_probe_python_version_invalid",
    )
    _require_token(
        runtime["platform_system"],
        "resource_gate_probe_platform_invalid",
    )
    _require_token(runtime["machine"], "resource_gate_probe_machine_invalid")

    document = report["document"]
    _require_object_fields(
        document,
        {
            "document_id",
            "source_sha256",
            "source_size_bytes",
            "page_count",
            "format",
        },
        "resource_gate_probe_document_shape_invalid",
    )
    document_id = document["document_id"]
    if (
        not isinstance(document_id, str)
        or document_id != document_id.strip()
        or _DOCUMENT_ID_PATTERN.fullmatch(document_id) is None
    ):
        _reject("resource_gate_probe_document_id_invalid")
    source_sha256 = document["source_sha256"]
    if (
        not isinstance(source_sha256, str)
        or source_sha256 != source_sha256.strip().lower()
        or _SHA256_PATTERN.fullmatch(source_sha256) is None
    ):
        _reject("resource_gate_probe_source_hash_invalid")
    _require_positive_int(
        document["source_size_bytes"],
        "resource_gate_probe_source_size_invalid",
    )
    _require_positive_int(
        document["page_count"],
        "resource_gate_probe_page_count_invalid",
    )
    if document["format"] != "pdf":
        _reject("resource_gate_probe_format_invalid")

    pipeline = report["pipeline"]
    _require_object_fields(
        pipeline,
        {"source_ir", "structure"},
        "resource_gate_probe_pipeline_shape_invalid",
    )
    _validate_report_component(
        pipeline["source_ir"],
        id_field="adapter_id",
        allowed_statuses={"COMPLETE", "PARTIAL"},
        count_fields={
            "nodes",
            "assets",
            "diagnostics",
            "visual_text_snapshots",
        },
        rejection_prefix="resource_gate_probe_source_ir",
    )
    _validate_report_component(
        pipeline["structure"],
        id_field="resolver_id",
        allowed_statuses={"COMPLETE", "REVIEW_REQUIRED"},
        count_fields={
            "nodes",
            "relations",
            "findings",
            "blocking_findings",
        },
        rejection_prefix="resource_gate_probe_structure",
    )

    measurements = report["measurements"]
    _require_object_fields(
        measurements,
        {"baseline_process_peak_rss_bytes", "stages", "total"},
        "resource_gate_probe_measurements_shape_invalid",
    )
    _require_nonnegative_int(
        measurements["baseline_process_peak_rss_bytes"],
        "resource_gate_probe_baseline_rss_invalid",
    )
    stages = measurements["stages"]
    _require_object_fields(
        stages,
        {"preflight", "source_ir", "structure"},
        "resource_gate_probe_stages_shape_invalid",
    )
    for stage in ("preflight", "source_ir", "structure"):
        _validate_stage_metrics(stages[stage])
    _validate_stage_metrics(measurements["total"])
    _validate_measurement_consistency(measurements)

    privacy = report["privacy"]
    expected_privacy = {
        "contains_customer_text": False,
        "contains_source_path": False,
        "contains_source_filename": False,
        "contains_locators": False,
        "contains_asset_bytes": False,
        "is_fidelity_or_quality_evidence": False,
        "is_human_gold": False,
        "is_serving_activation_gate": False,
    }
    _require_object_fields(
        privacy,
        set(expected_privacy),
        "resource_gate_probe_privacy_shape_invalid",
    )
    if privacy != expected_privacy:
        _reject("resource_gate_probe_privacy_invalid")


def evaluate_resource_gate(
    policy: dict[str, Any],
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    validate_resource_policy(policy)
    if not isinstance(reports, list) or not reports:
        _reject("resource_gate_reports_missing")
    for report in reports:
        validate_resource_probe_report(report)

    report_hashes = [str(report["report_sha256"]) for report in reports]
    if len(report_hashes) != len(set(report_hashes)):
        _reject("resource_gate_probe_duplicate")

    policy_documents = {
        str(item["document_id"]): item for item in policy["cohort"]["documents"]
    }
    reports_by_document: dict[str, list[dict[str, Any]]] = {
        document_id: [] for document_id in policy_documents
    }
    for report in reports:
        document_id = str(report["document"]["document_id"])
        expected = policy_documents.get(document_id)
        if expected is None:
            _reject("resource_gate_probe_document_not_in_policy")
        if report["document"]["source_sha256"] != expected["source_sha256"]:
            _reject("resource_gate_probe_source_hash_not_in_policy")
        reports_by_document[document_id].append(report)

    eligibility_blockers: set[str] = set()
    if policy["approval"]["status"] != "APPROVED":
        eligibility_blockers.add("resource_policy_not_approved")

    deployment = policy["deployment_profile"]
    policy_pipeline = policy["pipeline"]
    document_summaries: list[dict[str, Any]] = []
    for document_id in sorted(policy_documents):
        expected = policy_documents[document_id]
        document_reports = reports_by_document[document_id]
        if len(document_reports) < expected["minimum_runs"]:
            eligibility_blockers.add(
                f"minimum_runs_not_met:{document_id}"
            )

        result_fingerprints: set[str] = set()
        wall_values: list[float] = []
        cpu_values: list[float] = []
        peak_rss_values: list[int] = []
        for report in document_reports:
            runtime = report["runtime"]
            if runtime["platform_system"] != deployment["platform_system"]:
                eligibility_blockers.add(
                    f"platform_mismatch:{document_id}"
                )
            if runtime["machine"] != deployment["machine"]:
                eligibility_blockers.add(f"machine_mismatch:{document_id}")
            if not str(runtime["python_version"]).startswith(
                str(deployment["python_version_prefix"])
            ):
                eligibility_blockers.add(
                    f"python_version_mismatch:{document_id}"
                )
            if not _pipeline_matches_policy(
                report["pipeline"],
                policy_pipeline,
            ):
                eligibility_blockers.add(f"pipeline_mismatch:{document_id}")

            result_fingerprints.add(
                _canonical_json_sha256(
                    {
                        "source_ir": {
                            "status": report["pipeline"]["source_ir"]["status"],
                            "counts": report["pipeline"]["source_ir"]["counts"],
                        },
                        "structure": {
                            "status": report["pipeline"]["structure"]["status"],
                            "counts": report["pipeline"]["structure"]["counts"],
                        },
                    }
                )
            )
            total = report["measurements"]["total"]
            wall_values.append(float(total["wall_ms"]))
            cpu_values.append(float(total["cpu_ms"]))
            peak_rss_values.append(
                int(total["process_peak_rss_bytes_after_stage"])
            )
        if len(result_fingerprints) > 1:
            eligibility_blockers.add(f"result_counts_unstable:{document_id}")

        document_summaries.append(
            {
                "document_id": document_id,
                "source_sha256": expected["source_sha256"],
                "run_count": len(document_reports),
                "minimum_runs": expected["minimum_runs"],
                "result_counts_stable": len(result_fingerprints) <= 1,
                "total_wall_ms": _numeric_summary(wall_values),
                "total_cpu_ms": _numeric_summary(cpu_values),
                "process_peak_rss_bytes": _numeric_summary(
                    peak_rss_values,
                    integer=True,
                ),
            }
        )

    observed_wall_ms = max(
        (
            float(summary["total_wall_ms"]["maximum"])
            for summary in document_summaries
            if summary["run_count"]
        ),
        default=0.0,
    )
    observed_peak_rss_bytes = max(
        (
            int(summary["process_peak_rss_bytes"]["maximum"])
            for summary in document_summaries
            if summary["run_count"]
        ),
        default=0,
    )
    applicable = not eligibility_blockers
    limit_results = {
        "total_wall_ms": _evaluate_limit(
            policy["limits"]["total_wall_ms"],
            observed_wall_ms,
            applicable=applicable,
            integer=False,
        ),
        "process_peak_rss_bytes": _evaluate_limit(
            policy["limits"]["process_peak_rss_bytes"],
            observed_peak_rss_bytes,
            applicable=applicable,
            integer=True,
        ),
    }
    if not applicable:
        status = RESOURCE_GATE_NOT_EVALUATED
        blockers = sorted(eligibility_blockers)
    else:
        failed_limits = [
            name
            for name, result in limit_results.items()
            if result["status"] == RESOURCE_GATE_FAIL
        ]
        status = RESOURCE_GATE_FAIL if failed_limits else RESOURCE_GATE_PASS
        blockers = [
            f"{name}_limit_exceeded" for name in sorted(failed_limits)
        ]

    gate_report: dict[str, Any] = {
        "schema_version": RESOURCE_GATE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "policy": {
            "policy_id": policy["policy_id"],
            "revision": policy["revision"],
            "policy_sha256": canonical_resource_policy_sha256(policy),
            "approval_status": policy["approval"]["status"],
            "approval_authority": policy["approval"]["authority"],
            "approval_identity_verified_by_gate": False,
            "deployment_profile_id": deployment["profile_id"],
        },
        "scope": {
            "pipeline": "PREFLIGHT_SOURCE_IR_STRUCTURE",
            "visual_provider": "off",
            "process_mode": "DEDICATED_CLI_PROCESS",
            "uses_worst_observed_run": True,
            "mutates_serving": False,
            "sufficient_for_overall_serving_activation": False,
        },
        "inputs": {
            "report_count": len(reports),
            "report_sha256s": sorted(report_hashes),
        },
        "eligibility": {
            "applicable": applicable,
            "blockers": sorted(eligibility_blockers),
        },
        "documents": document_summaries,
        "limits": limit_results,
        "blockers": blockers,
        "privacy": {
            "contains_customer_text": False,
            "contains_source_path": False,
            "contains_source_filename": False,
            "contains_locators": False,
            "contains_asset_bytes": False,
            "is_fidelity_or_quality_evidence": False,
            "is_human_gold": False,
            "automatically_activates_serving": False,
        },
    }
    gate_report["gate_report_sha256"] = canonical_resource_gate_sha256(
        gate_report
    )
    return gate_report


def write_resource_gate_report(
    report: dict[str, Any],
    destination: Path,
    *,
    corpus_root: Path,
) -> Path:
    output_path = require_report_destination_outside_corpus(
        Path(destination),
        corpus_root,
    )
    if output_path.suffix.lower() != ".json":
        _reject("resource_gate_output_must_be_json")
    if output_path.exists():
        _reject("resource_gate_output_exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, output_path)
        except FileExistsError as exc:
            raise ExtractionResourceGateRejected(
                "resource_gate_output_exists"
            ) from exc
        temporary_path.unlink()
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path


def _validate_policy_component(
    component: Any,
    *,
    id_field: str,
    rejection_prefix: str,
) -> None:
    _require_object_fields(
        component,
        {"schema_version", id_field, f"{id_field.removesuffix('_id')}_version"},
        f"{rejection_prefix}_shape_invalid",
    )
    _require_token(
        component["schema_version"],
        f"{rejection_prefix}_schema_version_invalid",
    )
    _require_token(component[id_field], f"{rejection_prefix}_id_invalid")
    _require_token(
        component[f"{id_field.removesuffix('_id')}_version"],
        f"{rejection_prefix}_version_invalid",
    )


def _validate_limit(
    value: Any,
    *,
    integer_capacity: bool,
    rejection_prefix: str,
) -> None:
    _require_object_fields(
        value,
        {"capacity", "minimum_headroom_ratio"},
        f"{rejection_prefix}_shape_invalid",
    )
    if integer_capacity:
        capacity = _require_positive_int(
            value["capacity"],
            f"{rejection_prefix}_capacity_invalid",
        )
    else:
        capacity = _require_positive_number(
            value["capacity"],
            f"{rejection_prefix}_capacity_invalid",
        )
    margin = _require_nonnegative_number(
        value["minimum_headroom_ratio"],
        f"{rejection_prefix}_headroom_invalid",
    )
    if margin >= 1:
        _reject(f"{rejection_prefix}_headroom_invalid")
    effective = float(capacity) * (1.0 - margin)
    if effective <= 0 or (integer_capacity and math.floor(effective) < 1):
        _reject(f"{rejection_prefix}_effective_limit_invalid")


def _validate_report_component(
    component: Any,
    *,
    id_field: str,
    allowed_statuses: set[str],
    count_fields: set[str],
    rejection_prefix: str,
) -> None:
    version_field = f"{id_field.removesuffix('_id')}_version"
    _require_object_fields(
        component,
        {"schema_version", id_field, version_field, "status", "counts"},
        f"{rejection_prefix}_shape_invalid",
    )
    _require_token(
        component["schema_version"],
        f"{rejection_prefix}_schema_version_invalid",
    )
    _require_token(component[id_field], f"{rejection_prefix}_id_invalid")
    _require_token(
        component[version_field],
        f"{rejection_prefix}_version_invalid",
    )
    if component["status"] not in allowed_statuses:
        _reject(f"{rejection_prefix}_status_invalid")
    counts = component["counts"]
    _require_object_fields(
        counts,
        count_fields,
        f"{rejection_prefix}_counts_shape_invalid",
    )
    for count in counts.values():
        _require_nonnegative_int(
            count,
            f"{rejection_prefix}_count_invalid",
        )


def _validate_stage_metrics(metrics: Any) -> None:
    _require_object_fields(
        metrics,
        _STAGE_METRIC_FIELDS,
        "resource_gate_probe_stage_metrics_shape_invalid",
    )
    for field in ("wall_ms", "user_cpu_ms", "system_cpu_ms", "cpu_ms"):
        _require_nonnegative_number(
            metrics[field],
            "resource_gate_probe_stage_metric_invalid",
        )
    _require_positive_int(
        metrics["process_peak_rss_bytes_after_stage"],
        "resource_gate_probe_stage_peak_rss_invalid",
    )
    _require_nonnegative_int(
        metrics["process_peak_rss_growth_bytes_during_stage"],
        "resource_gate_probe_stage_rss_growth_invalid",
    )
    if not _numbers_close(
        float(metrics["cpu_ms"]),
        float(metrics["user_cpu_ms"]) + float(metrics["system_cpu_ms"]),
    ):
        _reject("resource_gate_probe_stage_cpu_inconsistent")


def _validate_measurement_consistency(measurements: dict[str, Any]) -> None:
    stages = measurements["stages"]
    total = measurements["total"]
    ordered_stages = [
        stages["preflight"],
        stages["source_ir"],
        stages["structure"],
    ]
    previous_peak = int(measurements["baseline_process_peak_rss_bytes"])
    for stage in ordered_stages:
        stage_peak = int(stage["process_peak_rss_bytes_after_stage"])
        if stage_peak < previous_peak:
            _reject("resource_gate_probe_peak_rss_not_monotonic")
        if (
            int(stage["process_peak_rss_growth_bytes_during_stage"])
            != stage_peak - previous_peak
        ):
            _reject("resource_gate_probe_stage_rss_growth_inconsistent")
        previous_peak = stage_peak

    if int(total["process_peak_rss_bytes_after_stage"]) != previous_peak:
        _reject("resource_gate_probe_total_peak_rss_inconsistent")
    if (
        int(total["process_peak_rss_growth_bytes_during_stage"])
        != previous_peak
        - int(measurements["baseline_process_peak_rss_bytes"])
    ):
        _reject("resource_gate_probe_total_rss_growth_inconsistent")
    for field in ("wall_ms", "user_cpu_ms", "system_cpu_ms", "cpu_ms"):
        if not _numbers_close(
            float(total[field]),
            sum(float(stage[field]) for stage in ordered_stages),
        ):
            _reject(f"resource_gate_probe_total_{field}_inconsistent")


def _numbers_close(first: float, second: float) -> bool:
    return abs(first - second) <= 0.01


def _pipeline_matches_policy(
    report_pipeline: dict[str, Any],
    policy_pipeline: dict[str, Any],
) -> bool:
    for component_name, id_field in (
        ("source_ir", "adapter_id"),
        ("structure", "resolver_id"),
    ):
        report_component = report_pipeline[component_name]
        policy_component = policy_pipeline[component_name]
        version_field = f"{id_field.removesuffix('_id')}_version"
        if any(
            report_component[field] != policy_component[field]
            for field in ("schema_version", id_field, version_field)
        ):
            return False
    return True


def _numeric_summary(
    values: list[float] | list[int],
    *,
    integer: bool = False,
) -> dict[str, int | float | None]:
    if not values:
        return {"minimum": None, "median": None, "maximum": None}
    minimum = min(values)
    median = statistics.median(values)
    maximum = max(values)
    if integer:
        return {
            "minimum": int(minimum),
            "median": int(median) if float(median).is_integer() else median,
            "maximum": int(maximum),
        }
    return {
        "minimum": round(float(minimum), 3),
        "median": round(float(median), 3),
        "maximum": round(float(maximum), 3),
    }


def _evaluate_limit(
    limit: dict[str, Any],
    observed: float | int,
    *,
    applicable: bool,
    integer: bool,
) -> dict[str, Any]:
    capacity = limit["capacity"]
    margin = float(limit["minimum_headroom_ratio"])
    raw_effective = float(capacity) * (1.0 - margin)
    effective: float | int
    if integer:
        effective = math.floor(raw_effective)
        observed_value: float | int = int(observed)
    else:
        effective = round(raw_effective, 3)
        observed_value = round(float(observed), 3)
    within = observed_value <= effective
    status = (
        RESOURCE_GATE_NOT_EVALUATED
        if not applicable
        else RESOURCE_GATE_PASS
        if within
        else RESOURCE_GATE_FAIL
    )
    return {
        "capacity": capacity,
        "minimum_headroom_ratio": margin,
        "effective_observed_limit": effective,
        "observed_worst": observed_value,
        "observed_headroom_ratio": round(
            (float(capacity) - float(observed_value)) / float(capacity),
            6,
        ),
        "observed_within_effective_limit": within,
        "status": status,
    }


def _require_object_fields(
    value: Any,
    fields: set[str],
    rejection_code: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _reject(rejection_code)
    return value


def _require_token(value: Any, rejection_code: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        _reject(rejection_code)
    token = value
    if _TOKEN_PATTERN.fullmatch(token) is None:
        _reject(rejection_code)
    return token


def _require_runtime_value(value: Any, rejection_code: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        _reject(rejection_code)
    token = value
    if (
        not token
        or len(token) > 64
        or re.fullmatch(r"^[A-Za-z0-9._+-]+$", token) is None
    ):
        _reject(rejection_code)
    return token


def _require_nonempty_string(
    value: Any,
    rejection_code: str,
    *,
    maximum_length: int,
) -> str:
    if not isinstance(value, str):
        _reject(rejection_code)
    token = value.strip()
    if not token or len(token) > maximum_length:
        _reject(rejection_code)
    return token


def _require_timestamp(value: Any, rejection_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _reject(rejection_code)
    token = value.strip()
    try:
        parsed = datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        _reject(rejection_code)
    if parsed.tzinfo is None:
        _reject(rejection_code)
    return token


def _require_positive_int(value: Any, rejection_code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _reject(rejection_code)
    return value


def _require_nonnegative_int(value: Any, rejection_code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _reject(rejection_code)
    return value


def _require_positive_number(value: Any, rejection_code: str) -> float:
    number = _require_nonnegative_number(value, rejection_code)
    if number <= 0:
        _reject(rejection_code)
    return number


def _require_nonnegative_number(value: Any, rejection_code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(rejection_code)
    number = float(value)
    if not math.isfinite(number) or number < 0:
        _reject(rejection_code)
    return number


def _canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _reject(code: str) -> None:
    raise ExtractionResourceGateRejected(code)


def _resolve_cli_path(value: Path) -> Path:
    return value if value.is_absolute() else PROJECT_DIR / value


def _load_json_object(path: Path, rejection_code: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".json":
        _reject(rejection_code)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _reject(rejection_code)
    if not isinstance(payload, dict):
        _reject(rejection_code)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate text-free extraction resource probe reports against an "
            "explicit deployment-owner policy without mutating serving."
        )
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument(
        "--report",
        type=Path,
        action="append",
        required=True,
        dest="reports",
    )
    parser.add_argument("--corpus-root", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        corpus_value = str(
            args.corpus_root
            or os.getenv("REQPILOTX_TRAINING_CORPUS_ROOT")
            or ""
        ).strip()
        if not corpus_value:
            _reject("resource_gate_corpus_root_required")
        corpus_root = Path(corpus_value).expanduser().resolve()
        policy_path = require_report_destination_outside_corpus(
            _resolve_cli_path(args.policy),
            corpus_root,
        )
        report_paths = [
            require_report_destination_outside_corpus(
                _resolve_cli_path(path),
                corpus_root,
            )
            for path in args.reports
        ]
        policy = _load_json_object(
            policy_path,
            "resource_gate_policy_file_invalid",
        )
        reports = [
            _load_json_object(path, "resource_gate_probe_file_invalid")
            for path in report_paths
        ]
        gate_report = evaluate_resource_gate(policy, reports)
        if args.output is None:
            print(
                json.dumps(
                    gate_report,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            output_path = write_resource_gate_report(
                gate_report,
                _resolve_cli_path(args.output),
                corpus_root=corpus_root,
            )
            print(f"JSON report: {output_path}")
    except (CorpusPathError, ExtractionResourceGateRejected) as exc:
        code = getattr(exc, "code", "resource_gate_path_rejected")
        print(f"[ERROR] {code}", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"[ERROR] resource_gate_failed:{type(exc).__name__}",
            file=sys.stderr,
        )
        return 2

    if gate_report["status"] == RESOURCE_GATE_PASS:
        return 0
    if gate_report["status"] == RESOURCE_GATE_FAIL:
        return 3
    return 4


__all__ = [
    "RESOURCE_GATE_FAIL",
    "RESOURCE_GATE_NOT_EVALUATED",
    "RESOURCE_GATE_PASS",
    "RESOURCE_GATE_SCHEMA_VERSION",
    "RESOURCE_POLICY_SCHEMA_VERSION",
    "ExtractionResourceGateRejected",
    "canonical_resource_gate_sha256",
    "canonical_resource_policy_sha256",
    "evaluate_resource_gate",
    "validate_resource_policy",
    "validate_resource_probe_report",
    "write_resource_gate_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
