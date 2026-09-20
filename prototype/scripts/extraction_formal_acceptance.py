from __future__ import annotations

# The repository-local app path must be installed before application imports.
# ruff: noqa: E402

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.private_eval import private_eval_path

from scripts.extraction_case_runner import (
    SCHEMA_VERSION as EXTRACTION_CASE_RUN_SCHEMA_VERSION,
)
from scripts.extraction_case_runner import (
    load_manifest,
    load_oracle_registry,
    require_report_destination_outside_corpus,
)
from scripts.storage_backup import BackupError, _rename_no_replace


FORMAL_ACCEPTANCE_SCHEMA_VERSION = "extraction-formal-acceptance/v2"
FORMAL_ACCEPTANCE_GATE_KIND = "FULL_MANIFEST_RUNNER_RESULT"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}$")
_ALLOWED_ASSERTION_STATUSES = frozenset({"PASS", "FAIL", "GAP", "BLOCKED_INPUT"})
_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "gate_kind",
        "status",
        "formal_acceptance_pass",
        "artifact_sha256",
        "binding",
        "coverage",
        "case_status_counts",
        "assertion_status_counts",
        "non_pass_assertions",
        "privacy",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "runner_schema_version",
        "report_sha256",
        "expected_report_sha256",
        "manifest_schema_version",
        "manifest_sha256",
        "oracle_registry_sha256",
        "source_ir_mode",
        "pdf_visual_provider",
        "acceptance_scope",
    }
)
_COVERAGE_FIELDS = frozenset(
    {
        "document_count",
        "case_count",
        "assertion_count",
        "all_scope_documents_present",
        "all_scope_cases_present",
        "all_scope_assertions_present",
    }
)
_NON_PASS_FIELDS = frozenset({"case_id", "assertion_id", "status", "reason_code"})
_PRIVACY_FIELDS = frozenset(
    {
        "contains_customer_text",
        "contains_source_path",
        "contains_source_filename",
        "is_human_gold",
        "is_visual_review_evidence",
    }
)


class ExtractionFormalAcceptanceRejected(ValueError):
    """Fail-closed validation error with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "").strip()
        if not self.code:
            raise ValueError("formal acceptance rejection code must not be blank")
        super().__init__(self.code)


def evaluate_extraction_formal_acceptance(
    report: dict[str, Any],
    *,
    manifest: dict[str, Any],
    oracle_registry: dict[str, Any],
    expected_report_sha256: str,
) -> dict[str, Any]:
    """Evaluate one independently pinned, full-manifest runner-v3 result.

    The development runner intentionally returns success for unresolved GAPs.
    This gate has the opposite contract: every assertion in the full manifest
    scope must be present exactly once and PASS. Phase cohorts are deliberately
    unsupported because they must not produce a formal PASS. The gate recomputes
    coverage and status counts from the case rows instead of trusting the report
    summary. It is a runner-result gate, not visual review or Human Gold evidence.
    """

    report_object = _object(report, "formal_report_not_object")
    manifest_object = _object(manifest, "formal_manifest_not_object")
    oracle_registry_object = _object(
        oracle_registry,
        "formal_oracle_registry_not_object",
    )
    pinned_report_sha256 = _require_sha256(
        expected_report_sha256,
        "formal_expected_report_sha256_invalid",
    )
    computed_report_sha256 = _canonical_payload_sha256(report_object)
    if computed_report_sha256 != pinned_report_sha256:
        _fail("formal_expected_report_sha256_mismatch")
    expected_oracle_registry_sha256 = _canonical_payload_sha256(oracle_registry_object)
    if report_object.get("schema_version") != EXTRACTION_CASE_RUN_SCHEMA_VERSION:
        _fail("formal_report_schema_version_mismatch")
    if report_object.get("manifest_schema_version") != manifest_object.get(
        "schema_version"
    ):
        _fail("formal_manifest_schema_version_mismatch")
    expected_manifest_sha256 = _canonical_manifest_sha256(manifest_object)
    if report_object.get("manifest_sha256") != expected_manifest_sha256:
        _fail("formal_manifest_sha256_mismatch")
    if report_object.get("oracle_registry_sha256") != expected_oracle_registry_sha256:
        _fail("formal_oracle_registry_sha256_mismatch")
    if report_object.get("source_ir_mode") != "native":
        _fail("formal_native_source_ir_required")

    _object(
        manifest_object.get("documents"),
        "formal_manifest_documents_invalid",
    )
    manifest_cases = _object(
        manifest_object.get("cases"),
        "formal_manifest_cases_invalid",
    )
    selected_cases = list(manifest_cases)
    if not selected_cases:
        _fail("formal_manifest_cases_empty")
    selected_documents = {
        str(document_id)
        for case_id in selected_cases
        for document_id in _array(
            _object(
                manifest_cases[case_id],
                "formal_manifest_case_invalid",
            ).get("document_ids"),
            "formal_manifest_case_documents_invalid",
        )
    }
    report_documents = _object(
        report_object.get("documents"),
        "formal_report_documents_invalid",
    )
    if set(report_documents) != selected_documents:
        _fail("formal_document_coverage_mismatch")

    report_cases = _object(
        report_object.get("cases"),
        "formal_report_cases_invalid",
    )
    if set(report_cases) != set(selected_cases):
        _fail("formal_case_coverage_mismatch")

    expected_assertion_ids: set[str] = set()
    observed_assertion_ids: set[str] = set()
    assertion_status_counts = {
        status: 0 for status in sorted(_ALLOWED_ASSERTION_STATUSES)
    }
    case_status_counts: dict[str, int] = {}
    non_pass_assertions: list[dict[str, str]] = []
    for case_id in selected_cases:
        manifest_case = _object(
            manifest_cases[case_id],
            "formal_manifest_case_invalid",
        )
        report_case = _object(
            report_cases.get(case_id),
            "formal_report_case_invalid",
        )
        if report_case.get("case_id") != case_id:
            _fail("formal_case_identity_mismatch")
        expected_case_assertions = [
            str(_object(item, "formal_manifest_assertion_invalid").get("assertion_id"))
            for item in _array(
                manifest_case.get("assertions"),
                "formal_manifest_assertions_invalid",
            )
        ]
        if len(expected_case_assertions) != len(set(expected_case_assertions)) or any(
            not assertion_id for assertion_id in expected_case_assertions
        ):
            _fail("formal_manifest_assertion_identity_invalid")
        expected_assertion_ids.update(expected_case_assertions)

        report_assertions = _array(
            report_case.get("assertions"),
            "formal_report_assertions_invalid",
        )
        observed_case_assertion_ids: list[str] = []
        observed_case_statuses: list[str] = []
        for assertion in report_assertions:
            assertion_object = _object(
                assertion,
                "formal_report_assertion_invalid",
            )
            assertion_id = str(assertion_object.get("assertion_id") or "")
            status = str(assertion_object.get("status") or "")
            if status not in _ALLOWED_ASSERTION_STATUSES:
                _fail("formal_assertion_status_invalid")
            observed_case_assertion_ids.append(assertion_id)
            observed_case_statuses.append(status)
            if assertion_id in observed_assertion_ids:
                _fail("formal_duplicate_assertion")
            observed_assertion_ids.add(assertion_id)
            assertion_status_counts[status] += 1
            if status != "PASS":
                non_pass_assertions.append(
                    {
                        "case_id": case_id,
                        "assertion_id": assertion_id,
                        "status": status,
                        "reason_code": str(
                            assertion_object.get("reason_code") or "unspecified"
                        ),
                    }
                )
        if observed_case_assertion_ids != expected_case_assertions:
            _fail("formal_assertion_coverage_or_order_mismatch")

        case_status = str(report_case.get("status") or "")
        expected_case_status = _aggregate_case_status(observed_case_statuses)
        if case_status != expected_case_status:
            _fail("formal_case_status_mismatch")
        case_status_counts[case_status] = case_status_counts.get(case_status, 0) + 1

    if observed_assertion_ids != expected_assertion_ids:
        _fail("formal_assertion_coverage_mismatch")

    summary = _object(
        report_object.get("summary"),
        "formal_report_summary_invalid",
    )
    if summary.get("identity_gate_pass") is not True:
        _fail("formal_identity_gate_failed")
    if summary.get("source_ir_observation_ok") is not True:
        _fail("formal_source_ir_observation_failed")
    if int(summary.get("hard_blocker_count", -1)) != 0:
        _fail("formal_hard_blocker_present")
    hard_blockers = _array(
        report_object.get("hard_blockers"),
        "formal_hard_blockers_invalid",
    )
    if hard_blockers:
        _fail("formal_hard_blocker_present")
    if _normalized_counts(summary.get("assertion_status_counts")) != {
        key: value for key, value in assertion_status_counts.items() if value
    }:
        _fail("formal_assertion_summary_mismatch")
    if _normalized_counts(summary.get("case_status_counts")) != dict(
        sorted(case_status_counts.items())
    ):
        _fail("formal_case_summary_mismatch")
    if int(summary.get("documents", -1)) != len(selected_documents):
        _fail("formal_document_summary_mismatch")
    if int(summary.get("cases", -1)) != len(selected_cases):
        _fail("formal_case_summary_count_mismatch")
    if int(summary.get("assertions", -1)) != len(expected_assertion_ids):
        _fail("formal_assertion_summary_count_mismatch")

    gate_pass = not non_pass_assertions
    result: dict[str, Any] = {
        "schema_version": FORMAL_ACCEPTANCE_SCHEMA_VERSION,
        "gate_kind": FORMAL_ACCEPTANCE_GATE_KIND,
        "status": "PASS" if gate_pass else "BLOCKED",
        "formal_acceptance_pass": gate_pass,
        "artifact_sha256": "",
        "binding": {
            "runner_schema_version": report_object["schema_version"],
            "report_sha256": computed_report_sha256,
            "expected_report_sha256": pinned_report_sha256,
            "manifest_schema_version": manifest_object["schema_version"],
            "manifest_sha256": expected_manifest_sha256,
            "oracle_registry_sha256": expected_oracle_registry_sha256,
            "source_ir_mode": "native",
            "pdf_visual_provider": str(
                report_object.get("pdf_visual_provider") or "off"
            ),
            "acceptance_scope": "FULL_MANIFEST",
        },
        "coverage": {
            "document_count": len(selected_documents),
            "case_count": len(selected_cases),
            "assertion_count": len(expected_assertion_ids),
            "all_scope_documents_present": True,
            "all_scope_cases_present": True,
            "all_scope_assertions_present": True,
        },
        "case_status_counts": dict(sorted(case_status_counts.items())),
        "assertion_status_counts": {
            key: value for key, value in assertion_status_counts.items() if value
        },
        "non_pass_assertions": non_pass_assertions,
        "privacy": {
            "contains_customer_text": False,
            "contains_source_path": False,
            "contains_source_filename": False,
            "is_human_gold": False,
            "is_visual_review_evidence": False,
        },
    }
    result["artifact_sha256"] = canonical_formal_acceptance_sha256(result)
    return validate_formal_acceptance_result(result)


def canonical_formal_acceptance_sha256(result: dict[str, Any]) -> str:
    """Hash an acceptance result after excluding its self-referential digest."""

    result_object = _object(result, "formal_result_not_object")
    payload = {
        key: value for key, value in result_object.items() if key != "artifact_sha256"
    }
    return _canonical_payload_sha256(payload)


def validate_formal_acceptance_result(
    result: dict[str, Any],
    *,
    expected_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate exact result shape, invariants, and canonical artifact hash."""

    result_object = _object(result, "formal_result_not_object")
    _require_exact_fields(result_object, _RESULT_FIELDS, "formal_result")
    if result_object["schema_version"] != FORMAL_ACCEPTANCE_SCHEMA_VERSION:
        _fail("formal_result_schema_version_mismatch")
    if result_object["gate_kind"] != FORMAL_ACCEPTANCE_GATE_KIND:
        _fail("formal_result_gate_kind_mismatch")

    formal_pass = result_object["formal_acceptance_pass"]
    if type(formal_pass) is not bool:
        _fail("formal_result_pass_invalid")
    status = result_object["status"]
    if status not in {"PASS", "BLOCKED"}:
        _fail("formal_result_status_invalid")
    artifact_sha256 = _require_sha256(
        result_object["artifact_sha256"],
        "formal_result_artifact_sha256_invalid",
    )

    binding = _object(result_object["binding"], "formal_result_binding_invalid")
    _require_exact_fields(binding, _BINDING_FIELDS, "formal_result_binding")
    _require_safe_token(
        binding["runner_schema_version"],
        "formal_result_runner_schema_version_invalid",
    )
    report_sha256 = _require_sha256(
        binding["report_sha256"],
        "formal_result_report_sha256_invalid",
    )
    pinned_report_sha256 = _require_sha256(
        binding["expected_report_sha256"],
        "formal_result_expected_report_sha256_invalid",
    )
    if report_sha256 != pinned_report_sha256:
        _fail("formal_result_report_pin_mismatch")
    _require_safe_token(
        binding["manifest_schema_version"],
        "formal_result_manifest_schema_version_invalid",
    )
    _require_sha256(
        binding["manifest_sha256"],
        "formal_result_manifest_sha256_invalid",
    )
    _require_sha256(
        binding["oracle_registry_sha256"],
        "formal_result_oracle_registry_sha256_invalid",
    )
    if binding["source_ir_mode"] != "native":
        _fail("formal_result_source_ir_mode_invalid")
    _require_safe_token(
        binding["pdf_visual_provider"],
        "formal_result_pdf_visual_provider_invalid",
    )
    if binding["acceptance_scope"] != "FULL_MANIFEST":
        _fail("formal_result_scope_invalid")

    coverage = _object(result_object["coverage"], "formal_result_coverage_invalid")
    _require_exact_fields(coverage, _COVERAGE_FIELDS, "formal_result_coverage")
    _require_positive_count(
        coverage["document_count"],
        "formal_result_document_count_invalid",
    )
    case_count = _require_positive_count(
        coverage["case_count"],
        "formal_result_case_count_invalid",
    )
    assertion_count = _require_positive_count(
        coverage["assertion_count"],
        "formal_result_assertion_count_invalid",
    )
    for field in (
        "all_scope_documents_present",
        "all_scope_cases_present",
        "all_scope_assertions_present",
    ):
        if coverage[field] is not True:
            _fail(f"formal_result_{field}_required")

    case_status_counts = _validate_status_counts(
        result_object["case_status_counts"],
        code="formal_result_case_status_counts_invalid",
    )
    assertion_status_counts = _validate_status_counts(
        result_object["assertion_status_counts"],
        code="formal_result_assertion_status_counts_invalid",
    )
    if sum(case_status_counts.values()) != case_count:
        _fail("formal_result_case_status_count_mismatch")
    if sum(assertion_status_counts.values()) != assertion_count:
        _fail("formal_result_assertion_status_count_mismatch")

    non_pass_source = _array(
        result_object["non_pass_assertions"],
        "formal_result_non_pass_assertions_invalid",
    )
    non_pass_assertions: list[dict[str, str]] = []
    seen_assertions: set[tuple[str, str]] = set()
    for index, item in enumerate(non_pass_source):
        item_object = _object(
            item,
            f"formal_result_non_pass_assertion_{index}_invalid",
        )
        _require_exact_fields(
            item_object,
            _NON_PASS_FIELDS,
            f"formal_result_non_pass_assertion_{index}",
        )
        case_id = _require_safe_token(
            item_object["case_id"],
            f"formal_result_non_pass_assertion_{index}_case_id_invalid",
        )
        assertion_id = _require_safe_token(
            item_object["assertion_id"],
            f"formal_result_non_pass_assertion_{index}_assertion_id_invalid",
        )
        item_status = item_object["status"]
        if item_status not in _ALLOWED_ASSERTION_STATUSES or item_status == "PASS":
            _fail(f"formal_result_non_pass_assertion_{index}_status_invalid")
        reason_code = _require_safe_token(
            item_object["reason_code"],
            f"formal_result_non_pass_assertion_{index}_reason_code_invalid",
        )
        identity = (case_id, assertion_id)
        if identity in seen_assertions:
            _fail("formal_result_non_pass_assertion_duplicate")
        seen_assertions.add(identity)
        non_pass_assertions.append(
            {
                "case_id": case_id,
                "assertion_id": assertion_id,
                "status": item_status,
                "reason_code": reason_code,
            }
        )
    expected_non_pass_count = sum(
        count
        for item_status, count in assertion_status_counts.items()
        if item_status != "PASS"
    )
    if len(non_pass_assertions) != expected_non_pass_count:
        _fail("formal_result_non_pass_assertion_count_mismatch")

    expected_pass = expected_non_pass_count == 0
    if formal_pass is not expected_pass:
        _fail("formal_result_pass_invariant_mismatch")
    if status != ("PASS" if expected_pass else "BLOCKED"):
        _fail("formal_result_status_invariant_mismatch")

    privacy = _object(result_object["privacy"], "formal_result_privacy_invalid")
    _require_exact_fields(privacy, _PRIVACY_FIELDS, "formal_result_privacy")
    for field in _PRIVACY_FIELDS:
        if privacy[field] is not False:
            _fail(f"formal_result_privacy_{field}_must_be_false")

    computed_artifact_sha256 = canonical_formal_acceptance_sha256(result_object)
    if artifact_sha256 != computed_artifact_sha256:
        _fail("formal_result_artifact_sha256_mismatch")
    if expected_artifact_sha256 is not None:
        expected_hash = _require_sha256(
            expected_artifact_sha256,
            "formal_expected_artifact_sha256_invalid",
        )
        if artifact_sha256 != expected_hash:
            _fail("formal_expected_artifact_sha256_mismatch")
    return json.loads(
        json.dumps(
            result_object,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def write_formal_acceptance_result(
    result: dict[str, Any],
    destination: Path,
    *,
    corpus_root: Path,
) -> Path:
    """Atomically write one non-overwriting, corpus-external gate result."""

    validated_result = validate_formal_acceptance_result(result)
    target = require_report_destination_outside_corpus(
        Path(destination),
        Path(corpus_root),
    )
    if target.exists():
        _fail("formal_output_exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        validated_result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        if target.exists():
            _fail("formal_output_exists")
        try:
            _rename_no_replace(temporary_path, target)
        except BackupError as exc:
            if target.exists():
                raise ExtractionFormalAcceptanceRejected(
                    "formal_output_exists"
                ) from exc
            raise ExtractionFormalAcceptanceRejected(
                "formal_atomic_publish_failed"
            ) from exc
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return target


def _canonical_manifest_sha256(manifest: dict[str, Any]) -> str:
    return _canonical_payload_sha256(manifest)


def _canonical_payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _aggregate_case_status(statuses: list[str]) -> str:
    if "BLOCKED_INPUT" in statuses:
        return "BLOCKED_INPUT"
    if "FAIL" in statuses:
        return "FAIL"
    if all(status == "PASS" for status in statuses):
        return "PASS"
    return "GAP"


def _normalized_counts(value: Any) -> dict[str, int]:
    source = _object(value, "formal_summary_counts_invalid")
    normalized: dict[str, int] = {}
    for key, count in source.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _fail("formal_summary_counts_invalid")
        if count:
            normalized[str(key)] = count
    return dict(sorted(normalized.items()))


def _validate_status_counts(value: Any, *, code: str) -> dict[str, int]:
    source = _object(value, code)
    if not source or any(key not in _ALLOWED_ASSERTION_STATUSES for key in source):
        _fail(code)
    normalized: dict[str, int] = {}
    for key, count in source.items():
        if type(count) is not int or count < 1:
            _fail(code)
        normalized[str(key)] = count
    return dict(sorted(normalized.items()))


def _require_exact_fields(
    value: dict[str, Any],
    expected_fields: frozenset[str],
    prefix: str,
) -> None:
    missing = sorted(expected_fields.difference(value))
    if missing:
        _fail(f"{prefix}_missing_field:{missing[0]}")
    unknown = sorted(set(value).difference(expected_fields))
    if unknown:
        _fail(f"{prefix}_unknown_field:{unknown[0]}")


def _require_sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        _fail(code)
    return value


def _require_safe_token(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SAFE_TOKEN_PATTERN.fullmatch(value) is None:
        _fail(code)
    return value


def _require_positive_count(value: Any, code: str) -> int:
    if type(value) is not int or value < 1:
        _fail(code)
    return value


def _object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code)
    return value


def _array(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(code)
    return value


def _fail(code: str) -> None:
    raise ExtractionFormalAcceptanceRejected(code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate an independently pinned FULL_MANIFEST_RUNNER_RESULT with "
            "every assertion PASS. This is not visual or Human Gold evidence."
        )
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=private_eval_path("extraction_cases/manifest.json"),
    )
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--expected-report-sha256",
        required=True,
        help="Independently pinned canonical SHA-256 of the complete runner report.",
    )
    args = parser.parse_args(argv)

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        manifest_path = args.manifest.resolve()
        oracle_path = manifest_path.with_name("oracles.json")
        oracle_registry = load_oracle_registry(oracle_path)
        manifest = load_manifest(
            manifest_path,
            oracle_path=oracle_path,
        )
        result = evaluate_extraction_formal_acceptance(
            report,
            manifest=manifest,
            oracle_registry=oracle_registry,
            expected_report_sha256=args.expected_report_sha256,
        )
        output = write_formal_acceptance_result(
            result,
            args.output,
            corpus_root=args.corpus_root,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(f"[ERROR] {code}", file=os.sys.stderr)
        return 2
    print(f"Formal acceptance: {result['status']}")
    print(f"Result: {output}")
    return 0 if result["formal_acceptance_pass"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
