#!/usr/bin/env python3
"""Fail-closed HTTP acceptance harness for the Document Requirement Agent.

The harness deliberately consumes an already-running *local* ReqPilotX service.
It never starts/stops a service, never activates a Requirement Set, and never
persists source text, OCR text, requirement statements, prompts, or responses.

The canonical flow is:

    source-bound extraction -> Structure -> semantic coverage
    -> atomization -> accepted refinement -> publication preview
    -> idempotent non-active DRAFT -> explicit read-only review/workspace

Only text-free operational identities, counts, statuses, runtimes, and lineage
digests are written to the receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler, Request, build_opener


SCRIPT_PATH = Path(__file__).resolve()
PROTOTYPE_ROOT = SCRIPT_PATH.parents[1]
REPOSITORY_ROOT = PROTOTYPE_ROOT.parent
DEFAULT_MANIFEST = Path(os.environ.get("REQPILOTX_PRIVATE_EVAL_ROOT", str(PROTOTYPE_ROOT / "eval" / "private"))) / "extraction_cases" / "manifest.json"

RECEIPT_SCHEMA_VERSION = "document-requirement-agent-e2e-receipt/v1"
FINAL_RESULT_SCHEMA_VERSION = "extraction-llm-shadow-preview/v5"
REVIEW_SESSION_SCHEMA_VERSION = "llm-requirement-review-session/v4"
PREDICTION_SCHEMA_VERSION = "llm-requirement-prediction/v5"
REVIEW_RESULT_SCHEMA_VERSION = "llm-requirement-review/v4"
ATOMIZATION_RUN_SCHEMA_VERSION = "requirement-atomization-run/v3"
ATOMIZATION_ALGORITHM_VERSION = "source-bound-provider-atomization/v3"
REFINEMENT_SCHEMA_VERSION = "requirement-refinement-suggestion/v1"
PUBLICATION_PREVIEW_SCHEMA_VERSION = "requirement-enrichment-publication-preview/v1"
PUBLICATION_RESULT_SCHEMA_VERSION = "requirement-enrichment-publication-result/v1"
READ_ONLY_REASON = "NON_ACTIVE_REQUIREMENT_SET_MUST_BE_ACTIVATED"

CANONICAL_DOCUMENTS: tuple[str, ...] = (
    "DOC-PEF-SOR",
    "DOC-PDS-OIL-PUMP",
    "DOC-F06-ACTIVE-SUSPENSION",
    "DOC-QC-T-1067",
    "DOC-QJLY-J7111029",
)

CANONICAL_VISUAL_PROVIDERS: Mapping[str, str] = {
    "DOC-PEF-SOR": "bailian-qwen3.7-plus-layout",
    "DOC-PDS-OIL-PUMP": "off",
    "DOC-F06-ACTIVE-SUSPENSION": "off",
    "DOC-QC-T-1067": "bailian",
    "DOC-QJLY-J7111029": "off",
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{0,239}$")
_SAFE_RECEIPT_STRING_RE = re.compile(
    r"^[A-Za-z0-9_.:/?&=%+@;,\-\[\]]{0,2048}$"
)
_TERMINAL_JOB_STATUSES = {"COMPLETED", "FAILED", "INTERRUPTED"}
_FORBIDDEN_RECEIPT_KEYS = {
    "text",
    "raw_text",
    "source_text",
    "review_text",
    "requirement_text",
    "original_statement",
    "suggested_statement",
    "atomic_statement",
    "published_statement",
    "statement",
    "ocr_text",
    "prompt",
    "response",
    "request_body",
    "response_body",
    "file_name",
    "display_name",
    "document_ref",
}


class GateFailure(RuntimeError):
    """One machine-readable, source-text-free acceptance failure."""

    def __init__(
        self,
        code: str,
        *,
        stage: str = "",
        logical_document_id: str = "",
        http_status: int | None = None,
    ) -> None:
        safe_code = _safe_code(code, fallback="acceptance_gate_failed")
        self.code = safe_code
        self.stage = _safe_code(stage, fallback="") if stage else ""
        self.logical_document_id = (
            _safe_code(logical_document_id, fallback="")
            if logical_document_id
            else ""
        )
        self.http_status = http_status
        self.partial_rehearsals: list[dict[str, Any]] = []
        super().__init__(safe_code)


@contextmanager
def _gate_stage(stage: str) -> Iterable[None]:
    """Attach a stable stage to lower-level fail-closed assertions."""

    try:
        yield
    except GateFailure as exc:
        if not exc.stage:
            exc.stage = _safe_code(stage, fallback="document")
        raise


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Any: ...


class JsonHttpClient:
    """Small no-proxy JSON client that exposes only safe error metadata."""

    def __init__(self, base_url: str, *, timeout_seconds: float) -> None:
        self.base_url = _validated_local_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(ProxyHandler({}), HTTPHandler(), HTTPSHandler())

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        normalized_path = "/" + str(path or "").lstrip("/")
        url = f"{self.base_url}{normalized_path}"
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        encoded = (
            json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        request = Request(
            url,
            data=encoded,
            method=method.upper(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "ReqPilotX-document-agent-e2e/v1",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(64 * 1024 * 1024 + 1)
        except HTTPError as exc:
            raw = exc.read(2 * 1024 * 1024 + 1)
            code = _safe_http_error_code(raw, fallback=f"http_{exc.code}")
            raise GateFailure(
                code,
                stage="http",
                http_status=exc.code,
            ) from None
        except (URLError, TimeoutError, OSError) as exc:
            reason = type(getattr(exc, "reason", exc)).__name__.lower()
            raise GateFailure(
                _safe_code(f"http_transport_{reason}", fallback="http_transport_error"),
                stage="http",
            ) from None
        if len(raw) > 64 * 1024 * 1024:
            raise GateFailure("http_response_too_large", stage="http")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GateFailure("http_response_json_invalid", stage="http") from None


@dataclass(frozen=True)
class DocumentTarget:
    logical_id: str
    application_document_id: str
    source_sha256: str
    visual_provider: str
    active_requirement_set_id: str


@dataclass(frozen=True)
class RunOptions:
    project_id: str
    poll_interval_seconds: float = 1.0
    extraction_timeout_seconds: float = 7200.0
    atomization_timeout_seconds: float = 7200.0
    atomization_attempts: int = 8


def _safe_code(value: Any, *, fallback: str) -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_CODE_RE.fullmatch(candidate) else fallback


def _safe_http_error_code(raw: bytes, *, fallback: str) -> str:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _safe_code(fallback, fallback="http_error")
    detail = decoded.get("detail") if isinstance(decoded, dict) else None
    candidate = detail.get("code") if isinstance(detail, dict) else None
    if candidate is None and isinstance(decoded, dict):
        candidate = decoded.get("code")
    return _safe_code(candidate, fallback=_safe_code(fallback, fallback="http_error"))


def _validated_local_base_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise GateFailure("base_url_invalid", stage="configuration")
    hostname = parsed.hostname
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.casefold() == "localhost"
    if not loopback:
        raise GateFailure("base_url_must_be_loopback", stage="configuration")
    return value.rstrip("/")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: Any) -> str:
    return _sha256_bytes(str(value or "").encode("utf-8"))


def _as_dict(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateFailure(code)
    return value


def _as_list(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise GateFailure(code)
    return value


def _semantic_decision_content_sha256(
    reviewed_result: Mapping[str, Any],
) -> str:
    nodes = [
        _as_dict(item, "semantic_decision_node_invalid")
        for item in _as_list(
            reviewed_result.get("nodes"),
            "semantic_decision_nodes_missing",
        )
    ]
    content = [
        {
            "order": index,
            "include_in_view": node.get("include_in_view"),
            "semantic_class": node.get("semantic_class"),
            "decision_mode": node.get("decision_mode"),
            "requirement_type": node.get("requirement_type"),
            "requirement_subtype": node.get("requirement_subtype"),
            "source_spans": node.get("source_spans"),
        }
        for index, node in enumerate(nodes)
    ]
    return _sha256_bytes(_canonical_json_bytes(content))


def _int(value: Any, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GateFailure(code)
    return value


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise GateFailure(code)


def _quoted(value: str) -> str:
    return quote(value, safe="")


def _api_root(project_id: str, document_id: str) -> str:
    return (
        f"/api/projects/{_quoted(project_id)}/sor-documents/"
        f"{_quoted(document_id)}"
    )


def _review_root(project_id: str, document_id: str, session_id: str) -> str:
    return (
        f"{_api_root(project_id, document_id)}/extraction/"
        f"llm-reviews/{_quoted(session_id)}"
    )


def _manifest_documents(
    manifest_path: Path,
    logical_ids: Sequence[str],
) -> tuple[dict[str, str], str]:
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GateFailure("manifest_json_invalid", stage="configuration") from None
    documents = _as_dict(manifest.get("documents"), "manifest_documents_missing")
    result: dict[str, str] = {}
    for logical_id in logical_ids:
        entry = _as_dict(
            documents.get(logical_id),
            "manifest_canonical_document_missing",
        )
        source_sha256 = str(entry.get("source_sha256") or "").strip().lower()
        _require(bool(_SHA256_RE.fullmatch(source_sha256)), "manifest_source_sha_invalid")
        _require(entry.get("resolution_status") == "resolved", "manifest_source_unresolved")
        result[logical_id] = source_sha256
    return result, _sha256_bytes(raw)


def _parse_document_overrides(values: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        logical_id, separator, document_id = raw.partition("=")
        if not separator or logical_id not in CANONICAL_DOCUMENTS or not document_id:
            raise GateFailure("application_document_override_invalid", stage="configuration")
        if logical_id in result:
            raise GateFailure("application_document_override_duplicate", stage="configuration")
        result[logical_id] = document_id
    return result


def resolve_targets(
    transport: JsonTransport,
    *,
    project_id: str,
    logical_ids: Sequence[str],
    source_hashes: Mapping[str, str],
    application_document_overrides: Mapping[str, str] | None = None,
) -> list[DocumentTarget]:
    payload = transport.request(
        "GET",
        f"/api/projects/{_quoted(project_id)}/sor-documents",
    )
    rows = _as_list(payload, "document_list_schema_invalid")
    overrides = application_document_overrides or {}
    targets: list[DocumentTarget] = []
    for logical_id in logical_ids:
        expected_sha = source_hashes[logical_id]
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and (
                str(row.get("id") or "") == overrides.get(logical_id)
                if logical_id in overrides
                else str(row.get("source_sha256") or "").lower() == expected_sha
            )
        ]
        _require(bool(matches), "canonical_document_not_registered")
        _require(len(matches) == 1, "canonical_document_registration_ambiguous")
        row = matches[0]
        _require(
            str(row.get("source_sha256") or "").lower() == expected_sha,
            "canonical_document_source_mismatch",
        )
        targets.append(
            DocumentTarget(
                logical_id=logical_id,
                application_document_id=str(row.get("id") or ""),
                source_sha256=expected_sha,
                visual_provider=CANONICAL_VISUAL_PROVIDERS[logical_id],
                active_requirement_set_id=str(
                    row.get("active_requirement_set_id") or ""
                ),
            )
        )
    return targets


def _validate_service(transport: JsonTransport) -> None:
    readiness = _as_dict(
        transport.request("GET", "/api/runtime-readiness"),
        "runtime_readiness_schema_invalid",
    )
    _require(
        readiness.get("runtime_ready") is True,
        "runtime_readiness_gate_failed",
    )
    checks = _as_dict(
        readiness.get("checks"),
        "runtime_readiness_checks_missing",
    )
    for name in ("startup", "storage", "sqlite"):
        status = str(
            _as_dict(
                checks.get(name),
                f"runtime_{name}_check_missing",
            ).get("status")
            or ""
        ).upper()
        _require(
            status == "OK",
            f"runtime_{name}_gate_failed",
        )
    provider_setup = _as_dict(
        transport.request("GET", "/api/runtime/ai-provider-setup"),
        "provider_setup_schema_invalid",
    )
    profiles = [
        _as_dict(item, "provider_profile_invalid")
        for item in _as_list(
            provider_setup.get("profiles"),
            "provider_profiles_missing",
        )
    ]
    _require(
        any(
            item.get("assessment_mode") == "llm_shadow"
            and item.get("llm_ready") is True
            for item in profiles
        ),
        "semantic_llm_not_ready",
    )


def _validate_progress(previous: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    for key in ("completed_chunks", "completed_targets", "successful_chunks"):
        old = previous.get(key, 0)
        new = current.get(key, 0)
        if isinstance(old, int) and isinstance(new, int):
            _require(new >= old, "extraction_progress_regressed")
    for completed_key, total_key in (
        ("completed_chunks", "total_chunks"),
        ("completed_targets", "total_targets"),
    ):
        completed = current.get(completed_key, 0)
        total = current.get(total_key, 0)
        if isinstance(completed, int) and isinstance(total, int) and total:
            _require(completed <= total, "extraction_progress_exceeds_total")


def _wait_for_extraction(
    transport: JsonTransport,
    *,
    target: DocumentTarget,
    options: RunOptions,
) -> dict[str, Any]:
    root = _api_root(options.project_id, target.application_document_id)
    start = _as_dict(
        transport.request(
            "POST",
            f"{root}/extraction/llm-document/start",
            body={"visual_provider": target.visual_provider},
        ),
        "extraction_start_schema_invalid",
    )
    deadline = time.monotonic() + options.extraction_timeout_seconds
    current = start
    previous_progress: Mapping[str, Any] = {}
    while True:
        job = _as_dict(current.get("job"), "extraction_job_missing")
        status = str(job.get("status") or "").upper()
        progress = _as_dict(job.get("progress"), "extraction_progress_missing")
        _validate_progress(previous_progress, progress)
        previous_progress = progress
        _require(
            str(job.get("source_sha256") or "") == target.source_sha256,
            "extraction_job_source_mismatch",
        )
        if status in _TERMINAL_JOB_STATUSES:
            _require(status == "COMPLETED", "extraction_job_not_completed")
            _require(not str(job.get("error_code") or ""), "extraction_job_has_error")
            _require(not str(job.get("error_stage") or ""), "extraction_job_has_error")
            _require(
                not str(job.get("error_cause_code") or ""),
                "extraction_job_has_error",
            )
            return current
        _require(status in {"PENDING", "RUNNING"}, "extraction_job_status_invalid")
        if time.monotonic() >= deadline:
            raise GateFailure("extraction_timeout", stage="extraction")
        time.sleep(options.poll_interval_seconds)
        current = _as_dict(
            transport.request(
                "GET",
                f"{root}/extraction/llm-document/status",
            ),
            "extraction_status_schema_invalid",
        )


def _validate_final_result(
    response: Mapping[str, Any],
    *,
    target: DocumentTarget,
    project_id: str,
) -> tuple[dict[str, Any], dict[str, Any], str, int]:
    result = _as_dict(response.get("result"), "extraction_final_result_missing")
    _require(
        result.get("schema_version") == FINAL_RESULT_SCHEMA_VERSION,
        "extraction_final_schema_incompatible",
    )
    _require(
        result.get("status") in {"READY", "REVIEW_REQUIRED"},
        "extraction_final_not_usable",
    )
    _require(result.get("serving_impact") == "NONE", "extraction_serving_impact_invalid")
    _require(result.get("requirement_set_id") is None, "extraction_formal_set_mutated")
    _require(result.get("requirement_set_created") is False, "extraction_formal_set_mutated")
    _require(
        result.get("requirement_set_activated") is False,
        "extraction_formal_set_mutated",
    )
    identity = _as_dict(result.get("source_identity"), "source_identity_missing")
    _require(result.get("source_identity_match") is True, "source_identity_mismatch")
    _require(identity.get("status") == "MATCHED", "source_identity_mismatch")
    _require(identity.get("match") is True, "source_identity_mismatch")
    _require(
        identity.get("source_sha256") == target.source_sha256,
        "source_identity_mismatch",
    )
    visual = _as_dict(result.get("visual_strategy"), "visual_strategy_missing")
    _require(
        visual.get("provider_name") == target.visual_provider,
        "visual_strategy_provider_mismatch",
    )
    expected_mode = "OFF" if target.visual_provider == "off" else "REMOTE_VISUAL"
    _require(visual.get("mode") == expected_mode, "visual_strategy_mode_mismatch")
    if expected_mode == "OFF":
        _require(
            _int(
                visual.get("authorized_page_count"),
                "visual_strategy_page_count_invalid",
            )
            == 0,
            "visual_strategy_page_count_invalid",
        )
    else:
        _require(
            _int(
                visual.get("authorized_page_count"),
                "visual_strategy_page_count_invalid",
                minimum=1,
            )
            >= 1,
            "visual_strategy_page_count_invalid",
        )

    pipeline = _as_dict(result.get("pipeline"), "pipeline_missing")
    semantic = _as_dict(pipeline.get("semantic"), "semantic_pipeline_missing")
    _require(semantic.get("status") == "COMPLETE", "semantic_pipeline_incomplete")
    coverage = _as_dict(result.get("semantic_coverage"), "semantic_coverage_missing")
    receipts = _as_list(
        result.get("semantic_terminal_receipts"),
        "semantic_terminal_receipts_missing",
    )
    total_units = _int(coverage.get("total_units"), "semantic_total_units_invalid", minimum=1)
    blocked = _int(
        coverage.get("blocked_unit_count"),
        "semantic_blocked_count_invalid",
    )
    unresolved = _int(
        coverage.get("unresolved_unit_count"),
        "semantic_unresolved_count_invalid",
    )
    receipt_count = _int(
        coverage.get("terminal_receipt_count"),
        "semantic_terminal_count_invalid",
    )
    _require(blocked == 0, "semantic_coverage_blocked")
    _require(unresolved == 0, "semantic_coverage_unresolved")
    _require(receipt_count == total_units == len(receipts), "semantic_exact_cover_failed")
    unit_ids: list[str] = []
    for receipt in receipts:
        item = _as_dict(receipt, "semantic_terminal_receipt_invalid")
        _require(item.get("status") == "RESOLVED", "semantic_terminal_unresolved")
        unit_id = str(item.get("semantic_unit_id") or "")
        _require(bool(unit_id), "semantic_terminal_unit_id_missing")
        unit_ids.append(unit_id)
    _require(len(unit_ids) == len(set(unit_ids)), "semantic_terminal_unit_duplicate")
    runtime_fields = (
        "planning_runtime_version",
        "classifier_runtime_version",
        "assembler_runtime_version",
        "document_runtime_version",
    )
    for key in runtime_fields:
        _require(
            str(coverage.get(key) or "") == str(semantic.get(key) or ""),
            "semantic_coverage_runtime_mismatch",
        )

    llm_review = _as_dict(result.get("llm_review"), "llm_review_missing")
    _require(
        llm_review.get("schema_version") == REVIEW_SESSION_SCHEMA_VERSION,
        "llm_review_schema_incompatible",
    )
    persistence = _as_dict(llm_review.get("persistence"), "llm_review_persistence_missing")
    _require(persistence.get("status") == "PERSISTED", "llm_review_not_persisted")
    session_id = str(persistence.get("session_id") or "")
    _require(bool(session_id), "llm_review_session_id_missing")
    prediction = _as_dict(llm_review.get("model_prediction"), "llm_prediction_missing")
    reviewed = _as_dict(llm_review.get("reviewed_result"), "llm_reviewed_result_missing")
    _require(
        prediction.get("schema_version") == PREDICTION_SCHEMA_VERSION,
        "llm_prediction_schema_incompatible",
    )
    _require(
        reviewed.get("schema_version") == REVIEW_RESULT_SCHEMA_VERSION,
        "llm_reviewed_schema_incompatible",
    )
    _require(
        prediction.get("source_sha256") == target.source_sha256,
        "llm_prediction_source_mismatch",
    )
    review_revision = _int(
        reviewed.get("revision"),
        "llm_review_revision_invalid",
    )
    _require(
        str(prediction.get("structure_snapshot_id") or ""),
        "llm_prediction_structure_snapshot_missing",
    )
    _require(
        response.get("project_id") == project_id
        and response.get("document_id") == target.application_document_id,
        "extraction_response_binding_mismatch",
    )
    return result, llm_review, session_id, review_revision


def _validate_structure(
    transport: JsonTransport,
    *,
    target: DocumentTarget,
    project_id: str,
    prediction: Mapping[str, Any],
    session_id: str,
    review_revision: int,
) -> dict[str, Any]:
    root = _api_root(project_id, target.application_document_id)
    response = _as_dict(
        transport.request("GET", f"{root}/extraction/structure"),
        "structure_response_schema_invalid",
    )
    _require(response.get("serving_impact") == "NONE", "structure_serving_impact_invalid")
    workbench = _as_dict(
        response.get("structure_workbench"),
        "structure_workbench_missing",
    )
    _require(
        workbench.get("schema_version") == "structure-workbench/v0",
        "structure_workbench_schema_incompatible",
    )
    source = _as_dict(workbench.get("source"), "structure_source_missing")
    structure = _as_dict(workbench.get("structure"), "structure_identity_missing")
    agent = _as_dict(workbench.get("agent"), "structure_agent_missing")
    workflow = _as_dict(workbench.get("workflow"), "structure_workflow_missing")
    readiness = _as_dict(workbench.get("readiness"), "structure_readiness_missing")
    _require(source.get("source_sha256") == target.source_sha256, "structure_source_mismatch")
    _require(agent.get("status") == "COMPLETED", "structure_agent_not_completed")
    _require(agent.get("editable") is True, "structure_not_editable")
    _require(structure.get("state") == "STABLE", "structure_not_stable")
    _require(structure.get("editable") is True, "structure_not_editable")
    _require(
        structure.get("effective_snapshot_id")
        == prediction.get("structure_snapshot_id"),
        "structure_prediction_snapshot_mismatch",
    )
    review = _as_dict(workflow.get("review"), "structure_review_workflow_missing")
    coverage = _as_dict(workflow.get("coverage"), "structure_coverage_workflow_missing")
    _require(review.get("state") == "READY", "structure_review_not_ready")
    _require(review.get("session_id") == session_id, "structure_review_session_mismatch")
    _require(review.get("revision") == review_revision, "structure_review_revision_mismatch")
    _require(coverage.get("state") == "READY", "structure_coverage_not_ready")
    _require(coverage.get("blocked_unit_count") == 0, "structure_coverage_blocked")
    _require(coverage.get("unresolved_unit_count") == 0, "structure_coverage_unresolved")
    _require(
        readiness.get("blocking_issue_codes") == [],
        "structure_readiness_blocked",
    )
    _require(readiness.get("can_atomize") is True, "structure_atomization_not_ready")
    return {
        "schema_version": workbench.get("schema_version"),
        "source_ir_id": source.get("source_ir_id"),
        "source_ir_adapter_version": source.get("source_ir_adapter_version"),
        "page_count": source.get("page_count"),
        "base_snapshot_id": structure.get("base_snapshot_id"),
        "effective_snapshot_id": structure.get("effective_snapshot_id"),
        "effective_tree_sha256": structure.get("effective_tree_sha256"),
        "revision_chain_sha256": structure.get("revision_chain_sha256"),
        "revision": structure.get("revision"),
        "node_count": len(_as_list(structure.get("nodes"), "structure_nodes_missing")),
        "proposal_count": len(_as_list(workbench.get("issues"), "structure_issues_invalid")),
    }


def _validate_persisted_review(
    transport: JsonTransport,
    *,
    target: DocumentTarget,
    project_id: str,
    session_id: str,
    review_revision: int,
) -> dict[str, Any]:
    response = _as_dict(
        transport.request(
            "GET",
            _review_root(project_id, target.application_document_id, session_id),
        ),
        "persisted_review_response_invalid",
    )
    _require(response.get("serving_impact") == "NONE", "persisted_review_serving_impact")
    review = _as_dict(response.get("llm_review"), "persisted_review_missing")
    persistence = _as_dict(review.get("persistence"), "persisted_review_metadata_missing")
    _require(persistence.get("status") == "PERSISTED", "persisted_review_not_persisted")
    _require(persistence.get("session_id") == session_id, "persisted_review_id_mismatch")
    _require(persistence.get("revision") == review_revision, "persisted_review_revision_mismatch")
    return review


def _validate_enrichment_coverage(state: Mapping[str, Any]) -> dict[str, Any]:
    _require(
        state.get("schema_version") == "requirement-enrichment-state/v1",
        "enrichment_state_schema_incompatible",
    )
    coverage = _as_dict(state.get("coverage"), "enrichment_coverage_missing")
    total = _int(coverage.get("total_units"), "enrichment_coverage_total_invalid", minimum=1)
    blocked = _int(
        coverage.get("blocked_unit_count"),
        "enrichment_coverage_blocked_invalid",
    )
    unresolved = _int(
        coverage.get("unresolved_unit_count"),
        "enrichment_coverage_unresolved_invalid",
    )
    receipts = _as_list(
        coverage.get("terminal_receipts"),
        "enrichment_coverage_receipts_missing",
    )
    _require(blocked == 0, "enrichment_coverage_blocked")
    _require(unresolved == 0, "enrichment_coverage_unresolved")
    _require(coverage.get("eligible") is True, "enrichment_coverage_ineligible")
    _require(len(receipts) == total, "enrichment_coverage_denominator_mismatch")
    _require(
        len({str(item.get("semantic_unit_id") or "") for item in receipts if isinstance(item, dict)})
        == total,
        "enrichment_coverage_unit_duplicate",
    )
    return coverage


def _coalesced_spans(spans: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, int, int]]:
    result: list[tuple[str, str, int, int]] = []
    for raw in spans:
        semantic_unit_id = str(raw.get("semantic_unit_id") or "")
        owner_id = str(raw.get("owner_structure_node_id") or "")
        start = _int(raw.get("start_offset"), "atomization_span_invalid")
        end = _int(raw.get("end_offset"), "atomization_span_invalid")
        _require(end >= start, "atomization_span_invalid")
        current = (semantic_unit_id, owner_id, start, end)
        if (
            result
            and result[-1][0] == semantic_unit_id
            and result[-1][1] == owner_id
            and result[-1][3] == start
        ):
            previous = result[-1]
            result[-1] = (previous[0], previous[1], previous[2], end)
        else:
            result.append(current)
    return result


def _source_text_for_spans(
    spans: Iterable[Mapping[str, Any]],
    *,
    prediction_text_by_unit: Mapping[str, str],
) -> str:
    parts: list[str] = []
    for span in spans:
        unit_id = str(span.get("semantic_unit_id") or "")
        source = prediction_text_by_unit.get(unit_id)
        _require(source is not None, "atomization_span_source_missing")
        start = _int(span.get("start_offset"), "atomization_span_invalid")
        end = _int(span.get("end_offset"), "atomization_span_invalid")
        _require(end >= start and end <= len(source), "atomization_span_out_of_range")
        parts.append(source[start:end])
    return "".join(parts)


def _validate_atomization_run(
    run: Mapping[str, Any],
    *,
    persisted_review: Mapping[str, Any],
    target: DocumentTarget,
    session_id: str,
    review_revision: int,
    coverage_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _require(
        run.get("schema_version") == ATOMIZATION_RUN_SCHEMA_VERSION,
        "atomization_run_schema_incompatible",
    )
    _require(
        run.get("algorithm_version") == ATOMIZATION_ALGORITHM_VERSION,
        "atomization_algorithm_incompatible",
    )
    lineage = _as_dict(run.get("lineage"), "atomization_lineage_missing")
    _require(lineage.get("session_id") == session_id, "atomization_session_mismatch")
    _require(lineage.get("source_sha256") == target.source_sha256, "atomization_source_mismatch")
    _require(lineage.get("review_revision") == review_revision, "atomization_revision_mismatch")
    _require(lineage.get("coverage_sha256") == coverage_sha256, "atomization_coverage_mismatch")
    nodes = [
        _as_dict(item, "atomization_node_invalid")
        for item in _as_list(run.get("nodes"), "atomization_nodes_missing")
    ]
    provider_runs = [
        _as_dict(item, "atomization_provider_run_invalid")
        for item in _as_list(
            run.get("provider_runs"),
            "atomization_provider_runs_missing",
        )
    ]
    _require(bool(provider_runs), "atomization_provider_runs_empty")
    _require(
        all(item.get("status") == "SUCCESS" for item in provider_runs),
        "atomization_provider_run_failed",
    )
    atomic_nodes = [item for item in nodes if item.get("kind") == "ATOMIC_OBLIGATION"]
    _require(bool(atomic_nodes), "atomization_atomic_nodes_empty")
    _require(
        len({str(item.get("atomic_node_id") or "") for item in atomic_nodes})
        == len(atomic_nodes),
        "atomization_atomic_node_duplicate",
    )
    _require(
        [item.get("source_order") for item in nodes]
        == sorted(item.get("source_order") for item in nodes),
        "atomization_source_order_invalid",
    )

    prediction = _as_dict(
        persisted_review.get("model_prediction"),
        "atomization_prediction_missing",
    )
    prediction_nodes = [
        _as_dict(item, "atomization_prediction_node_invalid")
        for item in _as_list(prediction.get("nodes"), "atomization_prediction_nodes_missing")
    ]
    prediction_text_by_unit = {
        str(item.get("semantic_unit_id") or ""): str(item.get("review_text") or "")
        for item in prediction_nodes
    }
    reviewed = _as_dict(
        persisted_review.get("reviewed_result"),
        "atomization_reviewed_result_missing",
    )
    reviewed_nodes = [
        _as_dict(item, "atomization_reviewed_node_invalid")
        for item in _as_list(reviewed.get("nodes"), "atomization_reviewed_nodes_missing")
    ]
    expected_direct = {
        str(item.get("review_node_id") or ""): item
        for item in reviewed_nodes
        if item.get("include_in_view") is True
        and item.get("semantic_class") == "REQUIREMENT"
        and item.get("decision_mode") == "DIRECT"
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for node in atomic_nodes:
        grouped.setdefault(str(node.get("review_node_id") or ""), []).append(node)
        spans = [
            _as_dict(item, "atomization_span_invalid")
            for item in _as_list(node.get("source_spans"), "atomization_spans_missing")
        ]
        reconstructed = _source_text_for_spans(
            spans,
            prediction_text_by_unit=prediction_text_by_unit,
        )
        _require(reconstructed == node.get("source_text"), "atomization_source_text_mismatch")
        statement = str(node.get("atomic_statement") or "")
        _require(bool(statement.strip()), "atomization_statement_missing")
    _require(set(grouped) == set(expected_direct), "atomization_direct_cover_mismatch")
    for review_node_id, expected in expected_direct.items():
        actual_spans: list[Mapping[str, Any]] = []
        for atom in grouped[review_node_id]:
            actual_spans.extend(
                _as_dict(item, "atomization_span_invalid")
                for item in _as_list(atom.get("source_spans"), "atomization_spans_missing")
            )
        expected_spans = [
            _as_dict(item, "atomization_span_invalid")
            for item in _as_list(expected.get("source_spans"), "atomization_review_spans_missing")
        ]
        _require(
            _coalesced_spans(actual_spans) == _coalesced_spans(expected_spans),
            "atomization_exact_cover_failed",
        )
    group_sizes = [len(value) for value in grouped.values()]
    node_order_by_id = {
        str(node.get("node_id") or ""): index for index, node in enumerate(nodes)
    }
    content_nodes = [
        {
            "order": index,
            "kind": node.get("kind"),
            "parent_order": node_order_by_id.get(
                str(node.get("parent_node_id") or "")
            ),
            "source_spans": node.get("source_spans"),
            "source_order": node.get("source_order"),
            "decision_mode": node.get("decision_mode"),
            "requirement_subtype": node.get("requirement_subtype"),
            "source_text_sha256": _sha256_text(node.get("source_text")),
            "atomic_statement_sha256": _sha256_text(
                node.get("atomic_statement")
            ),
        }
        for index, node in enumerate(nodes)
    ]
    receipt = {
        "schema_version": run.get("schema_version"),
        "atomization_run_id": run.get("atomization_run_id"),
        "revision": run.get("revision"),
        "algorithm_version": run.get("algorithm_version"),
        "lineage_sha256": lineage.get("lineage_sha256"),
        "node_count": len(nodes),
        "atomic_obligation_count": len(atomic_nodes),
        "rollup_count": len(nodes) - len(atomic_nodes),
        "direct_review_node_count": len(grouped),
        "single_atom_review_node_count": sum(size == 1 for size in group_sizes),
        "multi_atom_review_node_count": sum(size > 1 for size in group_sizes),
        "provider_run_count": len(provider_runs),
        "provider_ids": sorted({str(item.get("provider_id") or "") for item in provider_runs}),
        "model_ids": sorted({str(item.get("model_id") or "") for item in provider_runs}),
        "provider_statuses": sorted({str(item.get("status") or "") for item in provider_runs}),
        "content_sha256": _sha256_bytes(_canonical_json_bytes(content_nodes)),
        "exact_source_cover": True,
    }
    atomic_nodes.sort(
        key=lambda item: (
            _int(item.get("source_order"), "atomization_source_order_invalid"),
            str(item.get("atomic_node_id") or ""),
        )
    )
    return receipt, atomic_nodes


def _run_atomization(
    transport: JsonTransport,
    *,
    target: DocumentTarget,
    options: RunOptions,
    session_id: str,
    review_revision: int,
    persisted_review: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    root = _review_root(
        options.project_id,
        target.application_document_id,
        session_id,
    )
    deadline = time.monotonic() + options.atomization_timeout_seconds
    previous_completed = -1
    last_state: dict[str, Any] | None = None
    attempts = 0
    while time.monotonic() < deadline and attempts < options.atomization_attempts:
        attempts += 1
        state = _as_dict(
            transport.request("GET", f"{root}/enrichment"),
            "enrichment_state_schema_invalid",
        )
        coverage = _validate_enrichment_coverage(state)
        existing = state.get("atomization_run")
        if isinstance(existing, dict):
            receipt, nodes = _validate_atomization_run(
                existing,
                persisted_review=persisted_review,
                target=target,
                session_id=session_id,
                review_revision=review_revision,
                coverage_sha256=str(coverage.get("coverage_sha256") or ""),
            )
            receipt["resume_attempt_count"] = attempts - 1
            return existing, receipt, nodes
        progress = _as_dict(
            state.get("atomization_progress"),
            "atomization_progress_missing",
        )
        progress_state = str(progress.get("state") or "")
        completed = _int(
            progress.get("completed_direct_node_count"),
            "atomization_progress_invalid",
        )
        total = _int(
            progress.get("total_direct_node_count"),
            "atomization_progress_invalid",
            minimum=1,
        )
        _require(completed <= total, "atomization_progress_invalid")
        _require(completed >= previous_completed, "atomization_progress_regressed")
        previous_completed = completed
        if progress_state == "RUNNING":
            time.sleep(options.poll_interval_seconds)
            continue
        _require(
            progress_state in {"NOT_STARTED", "PARTIAL"},
            "atomization_progress_state_invalid",
        )
        if progress_state == "PARTIAL":
            _require(progress.get("resumable") is True, "atomization_partial_not_resumable")
        try:
            response = _as_dict(
                transport.request(
                    "POST",
                    f"{root}/atomization-runs",
                    body={
                        "schema_version": "requirement-atomization-run-request/v1",
                        "expected_review_revision": review_revision,
                    },
                ),
                "atomization_response_schema_invalid",
            )
        except GateFailure as exc:
            retryable_transport_error = bool(
                exc.http_status is None
                and exc.code.startswith("http_transport_")
            )
            if (
                exc.http_status not in {408, 429, 500, 502, 503, 504}
                and not retryable_transport_error
            ):
                raise
            last_state = state
            time.sleep(options.poll_interval_seconds)
            continue
        run = _as_dict(response.get("atomization_run"), "atomization_run_missing")
        receipt, nodes = _validate_atomization_run(
            run,
            persisted_review=persisted_review,
            target=target,
            session_id=session_id,
            review_revision=review_revision,
            coverage_sha256=str(coverage.get("coverage_sha256") or ""),
        )
        receipt["resume_attempt_count"] = attempts - 1
        return run, receipt, nodes
    if last_state is not None:
        raise GateFailure("atomization_resume_exhausted", stage="atomization")
    raise GateFailure("atomization_timeout", stage="atomization")


def _create_or_accept_refinement(
    transport: JsonTransport,
    *,
    target: DocumentTarget,
    options: RunOptions,
    session_id: str,
    review_revision: int,
    atomization_run: Mapping[str, Any],
    atomic_nodes: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _review_root(
        options.project_id,
        target.application_document_id,
        session_id,
    )
    run_id = str(atomization_run.get("atomization_run_id") or "")
    for node in atomic_nodes:
        atomic_node_id = str(node.get("atomic_node_id") or "")
        response = _as_dict(
            transport.request(
                "POST",
                f"{root}/refinement-suggestions",
                body={
                    "schema_version": "requirement-refinement-suggestion-request/v1",
                    "expected_review_revision": review_revision,
                    "atomization_run_id": run_id,
                    "atomic_node_id": atomic_node_id,
                },
            ),
            "refinement_response_schema_invalid",
        )
        suggestion = _as_dict(
            response.get("refinement_suggestion"),
            "refinement_suggestion_missing",
        )
        _require(
            suggestion.get("schema_version") == REFINEMENT_SCHEMA_VERSION,
            "refinement_schema_incompatible",
        )
        _require(suggestion.get("atomization_run_id") == run_id, "refinement_run_mismatch")
        _require(suggestion.get("atomic_node_id") == atomic_node_id, "refinement_node_mismatch")
        _require(
            suggestion.get("original_statement") == node.get("atomic_statement"),
            "refinement_original_mismatch",
        )
        _require(
            str(suggestion.get("suggested_statement") or "").strip()
            != str(suggestion.get("original_statement") or "").strip(),
            "refinement_not_independent",
        )
        status = str(suggestion.get("status") or "")
        if status == "REJECTED":
            continue
        if status == "DRAFT":
            suggestion_id = str(suggestion.get("suggestion_id") or "")
            decided = _as_dict(
                transport.request(
                    "PATCH",
                    f"{root}/refinement-suggestions/{_quoted(suggestion_id)}",
                    body={
                        "schema_version": "requirement-refinement-decision-request/v1",
                        "expected_refinement_revision": _int(
                            suggestion.get("revision"),
                            "refinement_revision_invalid",
                        ),
                        "operation": "ACCEPT",
                        "decision_note": "Accepted by canonical end-to-end acceptance.",
                    },
                ),
                "refinement_decision_response_invalid",
            )
            suggestion = _as_dict(
                decided.get("refinement_suggestion"),
                "refinement_decision_missing",
            )
        _require(suggestion.get("status") == "ACCEPTED", "refinement_not_accepted")
        _require(
            suggestion.get("original_statement") == node.get("atomic_statement"),
            "refinement_acceptance_overwrote_atom",
        )
        return suggestion, {
            "schema_version": suggestion.get("schema_version"),
            "suggestion_id": suggestion.get("suggestion_id"),
            "revision": suggestion.get("revision"),
            "status": suggestion.get("status"),
            "atomization_run_id": suggestion.get("atomization_run_id"),
            "atomic_node_id": suggestion.get("atomic_node_id"),
            "provider_id": suggestion.get("provider_id"),
            "provider_version": suggestion.get("provider_version"),
            "model_id": suggestion.get("model_id"),
            "prompt_version": suggestion.get("prompt_version"),
            "provider_run_id": suggestion.get("provider_run_id"),
            "reason_codes": suggestion.get("reason_codes"),
            "lineage_sha256": suggestion.get("lineage_sha256"),
            "independent_suggestion": True,
            "accepted_without_atom_mutation": True,
        }
    raise GateFailure("no_acceptable_refinement_candidate", stage="refinement")


def _create_publication_preview(
    transport: JsonTransport,
    *,
    target: DocumentTarget,
    options: RunOptions,
    session_id: str,
    review_revision: int,
    atomization_run: Mapping[str, Any],
    accepted_refinement: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _review_root(
        options.project_id,
        target.application_document_id,
        session_id,
    )
    run_id = str(atomization_run.get("atomization_run_id") or "")
    response = _as_dict(
        transport.request(
            "POST",
            f"{root}/publication-preview",
            body={
                "schema_version": (
                    "requirement-enrichment-publication-preview-request/v1"
                ),
                "expected_review_revision": review_revision,
                "atomization_run_id": run_id,
            },
        ),
        "publication_preview_response_invalid",
    )
    preview = _as_dict(
        response.get("publication_preview"),
        "publication_preview_missing",
    )
    _require(
        preview.get("schema_version") == PUBLICATION_PREVIEW_SCHEMA_VERSION,
        "publication_preview_schema_incompatible",
    )
    _require(preview.get("atomization_run_id") == run_id, "publication_preview_run_mismatch")
    _require(
        preview.get("base_active_requirement_set_id")
        == target.active_requirement_set_id,
        "publication_preview_active_base_mismatch",
    )
    entries = [
        _as_dict(item, "publication_entry_invalid")
        for item in _as_list(preview.get("entries"), "publication_entries_missing")
    ]
    _require(bool(entries), "publication_entries_empty")
    atomization_nodes = [
        _as_dict(item, "atomization_node_invalid")
        for item in _as_list(
            atomization_run.get("nodes"),
            "atomization_nodes_missing",
        )
    ]
    _require(
        [item.get("atomization_node_id") for item in entries]
        == [item.get("node_id") for item in atomization_nodes],
        "publication_atomization_cover_mismatch",
    )
    entry_ids = [str(item.get("publication_entry_id") or "") for item in entries]
    _require(len(entry_ids) == len(set(entry_ids)), "publication_entry_duplicate")
    order_by_id = {entry_id: index for index, entry_id in enumerate(entry_ids)}
    entry_id_by_atomization_node_id = {
        str(entry.get("atomization_node_id") or ""): entry_id
        for entry, entry_id in zip(entries, entry_ids, strict=True)
    }
    _require(
        [item.get("source_order") for item in entries]
        == sorted(item.get("source_order") for item in entries),
        "publication_source_order_invalid",
    )
    for index, (entry, atomization_node) in enumerate(
        zip(entries, atomization_nodes, strict=True)
    ):
        parent_id = str(entry.get("parent_publication_entry_id") or "")
        _require(
            not parent_id or (parent_id in order_by_id and order_by_id[parent_id] < index),
            "publication_parent_order_invalid",
        )
        expected_parent_id = entry_id_by_atomization_node_id.get(
            str(atomization_node.get("parent_node_id") or "")
        )
        _require(
            (entry.get("parent_publication_entry_id") or None)
            == expected_parent_id,
            "publication_parent_binding_mismatch",
        )
        expected_kind = (
            "HEADING"
            if atomization_node.get("kind") == "ROLLUP"
            else "REQUIREMENT"
        )
        expected_mode = (
            "ROLLUP"
            if atomization_node.get("kind") == "ROLLUP"
            else "DIRECT"
        )
        for entry_key, node_key, error_code in (
            ("atomic_node_id", "atomic_node_id", "publication_atomic_binding_mismatch"),
            ("review_node_id", "review_node_id", "publication_review_binding_mismatch"),
            ("source_order", "source_order", "publication_source_order_mismatch"),
            (
                "decision_mode",
                "decision_mode",
                "publication_decision_mode_mismatch",
            ),
            (
                "requirement_type",
                "requirement_subtype",
                "publication_requirement_type_mismatch",
            ),
        ):
            expected = atomization_node.get(node_key)
            if entry_key == "decision_mode" and expected is None:
                expected = expected_mode
            _require(entry.get(entry_key) == expected, error_code)
        _require(entry.get("entry_kind") == expected_kind, "publication_kind_mismatch")
        entry_spans = _as_list(
            entry.get("source_spans"),
            "publication_source_spans_missing",
        )
        atomization_spans = _as_list(
            atomization_node.get("source_spans"),
            "atomization_spans_missing",
        )
        _require(
            _canonical_json_bytes(entry_spans)
            == _canonical_json_bytes(atomization_spans),
            "publication_source_spans_mismatch",
        )
    matching = [
        entry
        for entry in entries
        if entry.get("atomic_node_id") == accepted_refinement.get("atomic_node_id")
    ]
    _require(len(matching) == 1, "publication_refinement_entry_missing")
    refined_entry = matching[0]
    _require(
        refined_entry.get("refinement_suggestion_id")
        == accepted_refinement.get("suggestion_id"),
        "publication_refinement_binding_mismatch",
    )
    _require(
        refined_entry.get("refinement_revision")
        == accepted_refinement.get("revision"),
        "publication_refinement_binding_mismatch",
    )
    _require(
        refined_entry.get("published_statement")
        == accepted_refinement.get("suggested_statement"),
        "publication_refinement_text_mismatch",
    )
    content_entries = [
        {
            "order": index,
            "parent_order": order_by_id.get(
                str(entry.get("parent_publication_entry_id") or "")
            ),
            "entry_kind": entry.get("entry_kind"),
            "requirement_type": entry.get("requirement_type"),
            "decision_mode": entry.get("decision_mode"),
            "req_code": entry.get("req_code"),
            "source_order": entry.get("source_order"),
            "source_spans": entry.get("source_spans"),
            "published_statement_sha256": _sha256_text(
                entry.get("published_statement")
            ),
            "refinement_applied": bool(
                entry.get("refinement_suggestion_id")
            ),
        }
        for index, entry in enumerate(entries)
    ]
    return preview, {
        "schema_version": preview.get("schema_version"),
        "preview_id": preview.get("preview_id"),
        "publication_key": preview.get("publication_key"),
        "lineage_sha256": preview.get("lineage_sha256"),
        "base_active_requirement_set_id": preview.get(
            "base_active_requirement_set_id"
        ),
        "entry_count": len(entries),
        "heading_count": sum(item.get("entry_kind") == "HEADING" for item in entries),
        "requirement_count": sum(
            item.get("entry_kind") == "REQUIREMENT" for item in entries
        ),
        "refinement_binding_count": len(
            _as_list(
                preview.get("refinement_bindings"),
                "publication_refinement_bindings_invalid",
            )
        ),
        "content_sha256": _sha256_bytes(
            _canonical_json_bytes(content_entries)
        ),
        "parent_before_child": True,
        "source_order_preserved": True,
        "exact_source_trace_preserved": True,
        "accepted_refinement_bound": True,
    }


def _publish_idempotent_draft(
    transport: JsonTransport,
    *,
    target: DocumentTarget,
    options: RunOptions,
    session_id: str,
    review_revision: int,
    atomization_run: Mapping[str, Any],
    preview: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _review_root(
        options.project_id,
        target.application_document_id,
        session_id,
    )
    body = {
        "schema_version": "requirement-enrichment-publish-draft-request/v1",
        "expected_review_revision": review_revision,
        "atomization_run_id": atomization_run.get("atomization_run_id"),
        "preview_id": preview.get("preview_id"),
        "publication_key": preview.get("publication_key"),
    }
    responses = [
        _as_dict(
            transport.request("POST", f"{root}/publish-draft", body=body),
            "publish_draft_response_invalid",
        )
        for _ in range(2)
    ]
    publications = [
        _as_dict(item.get("publication"), "publication_result_missing")
        for item in responses
    ]
    for response, publication in zip(responses, publications, strict=True):
        _require(
            response.get("schema_version")
            == "requirement-enrichment-publish-draft-response/v1",
            "publish_response_schema_incompatible",
        )
        _require(response.get("serving_impact") == "DRAFT_ONLY", "publish_serving_impact_invalid")
        _require(response.get("requirement_set_activated") is False, "draft_was_activated")
        _require(
            publication.get("schema_version") == PUBLICATION_RESULT_SCHEMA_VERSION,
            "publication_result_schema_incompatible",
        )
        _require(publication.get("requirement_set_status") == "DRAFT", "publication_not_draft")
        _require(publication.get("is_active") is False, "publication_is_active")
        _require(
            publication.get("active_requirement_set_id")
            == target.active_requirement_set_id,
            "publication_active_set_changed",
        )
    _require(
        publications[0].get("requirement_set_id")
        == publications[1].get("requirement_set_id"),
        "publication_not_idempotent",
    )
    _require(
        responses[1].get("requirement_set_created") is False,
        "publication_replay_created_duplicate",
    )
    return publications[0], {
        "schema_version": publications[0].get("schema_version"),
        "requirement_set_id": publications[0].get("requirement_set_id"),
        "requirement_set_version": publications[0].get("requirement_set_version"),
        "requirement_set_status": publications[0].get("requirement_set_status"),
        "first_call_created": responses[0].get("requirement_set_created"),
        "second_call_created": responses[1].get("requirement_set_created"),
        "idempotent": True,
        "activated": False,
        "active_requirement_set_id": publications[0].get(
            "active_requirement_set_id"
        ),
    }


def _document_after(
    transport: JsonTransport,
    *,
    project_id: str,
    document_id: str,
) -> dict[str, Any]:
    rows = _as_list(
        transport.request(
            "GET",
            f"/api/projects/{_quoted(project_id)}/sor-documents",
        ),
        "document_list_schema_invalid",
    )
    matches = [
        item
        for item in rows
        if isinstance(item, dict) and str(item.get("id") or "") == document_id
    ]
    _require(len(matches) == 1, "published_document_not_found")
    return matches[0]


def _validate_selected_draft(
    transport: JsonTransport,
    *,
    target: DocumentTarget,
    options: RunOptions,
    preview: Mapping[str, Any],
    publication: Mapping[str, Any],
) -> dict[str, Any]:
    root = _api_root(options.project_id, target.application_document_id)
    set_id = str(publication.get("requirement_set_id") or "")
    listed = _as_dict(
        transport.request("GET", f"{root}/requirement-sets"),
        "requirement_set_list_invalid",
    )
    _require(
        listed.get("active_requirement_set_id") == target.active_requirement_set_id,
        "active_requirement_set_drifted",
    )
    set_rows = [
        _as_dict(item, "requirement_set_row_invalid")
        for item in _as_list(
            listed.get("requirement_sets"),
            "requirement_sets_missing",
        )
    ]
    matching_sets = [item for item in set_rows if str(item.get("id") or "") == set_id]
    _require(len(matching_sets) == 1, "published_requirement_set_missing")
    _require(matching_sets[0].get("status") == "DRAFT", "published_requirement_set_not_draft")

    workspace = _as_dict(
        transport.request(
            "GET",
            f"{root}/workspace",
            query={"requirement_set_id": set_id},
        ),
        "selected_workspace_invalid",
    )
    document = _as_dict(
        workspace.get("document"),
        "selected_workspace_document_missing",
    )
    selected_set = _as_dict(
        workspace.get("selected_requirement_set"),
        "selected_workspace_requirement_set_missing",
    )
    _require(selected_set.get("id") == set_id, "selected_workspace_set_mismatch")
    _require(
        selected_set.get("status") == "DRAFT",
        "selected_requirement_set_not_draft",
    )
    _require(
        selected_set.get("is_active") is False,
        "selected_workspace_is_active",
    )
    _require(
        document.get("active_requirement_set_id") == target.active_requirement_set_id,
        "selected_workspace_active_drift",
    )
    _require(
        workspace.get("read_only_reason") == READ_ONLY_REASON,
        "selected_workspace_not_read_only",
    )
    requirements = [
        _as_dict(item, "selected_requirement_invalid")
        for item in _as_list(workspace.get("requirements"), "selected_requirements_missing")
    ]
    entries = [
        _as_dict(item, "publication_entry_invalid")
        for item in _as_list(preview.get("entries"), "publication_entries_missing")
    ]
    _require(len(requirements) == len(entries), "selected_requirement_count_mismatch")
    for index, (requirement, entry) in enumerate(zip(requirements, entries, strict=True)):
        trace = _as_dict(
            requirement.get("decision_reference"),
            "selected_requirement_trace_missing",
        )
        _require(
            trace.get("schema_version") == "requirement-enrichment-trace/v1",
            "selected_requirement_trace_invalid",
        )
        _require(trace.get("publication_order") == index, "selected_requirement_order_invalid")
        _require(
            trace.get("publication_entry_id") == entry.get("publication_entry_id"),
            "selected_requirement_entry_mismatch",
        )
        for trace_key, entry_key, error_code in (
            (
                "parent_publication_entry_id",
                "parent_publication_entry_id",
                "selected_requirement_parent_mismatch",
            ),
            ("publication_key", None, "selected_requirement_publication_mismatch"),
            ("preview_id", None, "selected_requirement_preview_mismatch"),
            ("lineage_sha256", None, "selected_requirement_lineage_mismatch"),
            ("source_order", "source_order", "selected_requirement_source_order_mismatch"),
            ("entry_kind", "entry_kind", "selected_requirement_kind_mismatch"),
            (
                "atomization_node_id",
                "atomization_node_id",
                "selected_requirement_atomization_mismatch",
            ),
            ("atomic_node_id", "atomic_node_id", "selected_requirement_atomic_mismatch"),
            ("review_node_id", "review_node_id", "selected_requirement_review_mismatch"),
            ("decision_mode", "decision_mode", "selected_requirement_mode_mismatch"),
            (
                "refinement_suggestion_id",
                "refinement_suggestion_id",
                "selected_requirement_refinement_mismatch",
            ),
            (
                "refinement_revision",
                "refinement_revision",
                "selected_requirement_refinement_mismatch",
            ),
        ):
            if trace_key == "publication_key":
                expected = preview.get("publication_key")
            elif trace_key == "preview_id":
                expected = preview.get("preview_id")
            elif trace_key == "lineage_sha256":
                expected = preview.get("lineage_sha256")
            else:
                expected = entry.get(str(entry_key))
            _require(trace.get(trace_key) == expected, error_code)
        _require(
            _canonical_json_bytes(
                _as_list(
                    trace.get("source_spans"),
                    "selected_requirement_source_spans_missing",
                )
            )
            == _canonical_json_bytes(
                _as_list(
                    entry.get("source_spans"),
                    "publication_source_spans_missing",
                )
            ),
            "selected_requirement_source_spans_mismatch",
        )

    _require(document.get("requirement_set_id") == set_id, "selected_workspace_set_mismatch")
    _require(document.get("requirement_set_is_active") is False, "selected_workspace_is_active")
    _require(document.get("active_requirement_set_id") == target.active_requirement_set_id, "selected_workspace_active_drift")
    _require(workspace.get("read_only_reason") == READ_ONLY_REASON, "selected_workspace_not_read_only")
    summary = _as_dict(workspace.get("summary"), "selected_workspace_summary_missing")
    _require(summary.get("order_source") == "enrichment_publication", "selected_workspace_order_invalid")
    workspace_requirements = _as_list(
        workspace.get("requirements"),
        "selected_workspace_requirements_missing",
    )
    _require(
        len(workspace_requirements) == len(entries),
        "selected_workspace_requirement_count_mismatch",
    )
    actions = _as_dict(workspace.get("actions"), "selected_workspace_actions_missing")
    for key in (
        "can_lock_extraction",
        "can_export",
        "can_export_draft",
        "can_export_formal",
        "can_run_ai_analysis",
    ):
        _require(actions.get(key) is False, "selected_workspace_mutation_enabled")

    after = _document_after(
        transport,
        project_id=options.project_id,
        document_id=target.application_document_id,
    )
    _require(
        str(after.get("active_requirement_set_id") or "")
        == target.active_requirement_set_id,
        "active_requirement_set_drifted",
    )
    return {
        "schema_version": "document-workspace/v1",
        "requirement_set_id": set_id,
        "active_requirement_set_id_before": target.active_requirement_set_id,
        "active_requirement_set_id_after": str(
            after.get("active_requirement_set_id") or ""
        ),
        "is_active": False,
        "read_only_reason": workspace.get("read_only_reason"),
        "requirement_count": len(requirements),
        "publication_order_preserved": True,
        "parent_relationships_preserved": True,
        "workspace_order_source": summary.get("order_source"),
        "mutating_actions_disabled": True,
        "formal_review_path": (
            f"/workspace?view=workspace&stage=review&projectId="
            f"{_quoted(options.project_id)}&documentId="
            f"{_quoted(target.application_document_id)}&requirementSetId="
            f"{_quoted(set_id)}"
        ),
    }


def run_document(
    transport: JsonTransport,
    *,
    target: DocumentTarget,
    options: RunOptions,
    rehearsal_index: int,
) -> dict[str, Any]:
    started_monotonic = time.monotonic()
    started_at = _now()
    with _gate_stage("extraction"):
        response = _wait_for_extraction(transport, target=target, options=options)
        result, llm_review, session_id, review_revision = _validate_final_result(
            response,
            target=target,
            project_id=options.project_id,
        )
        prediction = _as_dict(
            llm_review.get("model_prediction"),
            "llm_prediction_missing",
        )
    with _gate_stage("structure"):
        structure_receipt = _validate_structure(
            transport,
            target=target,
            project_id=options.project_id,
            prediction=prediction,
            session_id=session_id,
            review_revision=review_revision,
        )
    with _gate_stage("review"):
        persisted_review = _validate_persisted_review(
            transport,
            target=target,
            project_id=options.project_id,
            session_id=session_id,
            review_revision=review_revision,
        )
    with _gate_stage("atomization"):
        atomization_run, atomization_receipt, atomic_nodes = _run_atomization(
            transport,
            target=target,
            options=options,
            session_id=session_id,
            review_revision=review_revision,
            persisted_review=persisted_review,
        )
    with _gate_stage("refinement"):
        accepted_refinement, refinement_receipt = _create_or_accept_refinement(
            transport,
            target=target,
            options=options,
            session_id=session_id,
            review_revision=review_revision,
            atomization_run=atomization_run,
            atomic_nodes=atomic_nodes,
        )
    with _gate_stage("publication_preview"):
        preview, preview_receipt = _create_publication_preview(
            transport,
            target=target,
            options=options,
            session_id=session_id,
            review_revision=review_revision,
            atomization_run=atomization_run,
            accepted_refinement=accepted_refinement,
        )
    with _gate_stage("publication"):
        publication, publication_receipt = _publish_idempotent_draft(
            transport,
            target=target,
            options=options,
            session_id=session_id,
            review_revision=review_revision,
            atomization_run=atomization_run,
            preview=preview,
        )
    with _gate_stage("selected_review"):
        selected_receipt = _validate_selected_draft(
            transport,
            target=target,
            options=options,
            preview=preview,
            publication=publication,
        )
    job = _as_dict(response.get("job"), "extraction_job_missing")
    coverage = _as_dict(result.get("semantic_coverage"), "semantic_coverage_missing")
    semantic = _as_dict(
        _as_dict(result.get("pipeline"), "pipeline_missing").get("semantic"),
        "semantic_pipeline_missing",
    )
    reviewed_result = _as_dict(
        llm_review.get("reviewed_result"),
        "llm_reviewed_result_missing",
    )
    llm_batch = _as_dict(result.get("llm_batch"), "llm_batch_missing")
    counts = _as_dict(result.get("counts"), "result_counts_missing")
    finished_at = _now()
    return {
        "logical_document_id": target.logical_id,
        "application_document_id": target.application_document_id,
        "source_sha256": target.source_sha256,
        "rehearsal_index": rehearsal_index,
        "status": "PASS",
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
        "extraction": {
            "schema_version": result.get("schema_version"),
            "result_status": result.get("status"),
            "job_id": job.get("job_id"),
            "job_status": job.get("status"),
            "job_stage": job.get("stage"),
            "created_at_utc": job.get("created_at_utc"),
            "started_at_utc": job.get("started_at_utc"),
            "finished_at_utc": job.get("finished_at_utc"),
            "visual_strategy": {
                key: _as_dict(
                    result.get("visual_strategy"),
                    "visual_strategy_missing",
                ).get(key)
                for key in (
                    "schema_version",
                    "mode",
                    "provider_name",
                    "provider_id",
                    "provider_version",
                    "route_id",
                    "route_sha256",
                    "routing_policy_id",
                    "authorized_page_count",
                    "source_ir_adapter_version",
                )
            },
            "semantic_provider": {
                key: llm_batch.get("provider", {}).get(key)
                if isinstance(llm_batch.get("provider"), dict)
                else None
                for key in ("id", "version", "model_id")
            },
            "semantic_status": semantic.get("status"),
            "planning_runtime_version": semantic.get("planning_runtime_version"),
            "classifier_runtime_version": semantic.get("classifier_runtime_version"),
            "assembler_runtime_version": semantic.get("assembler_runtime_version"),
            "document_runtime_version": semantic.get("document_runtime_version"),
            "classification_counts": {
                key: counts.get(key)
                for key in (
                    "requirement",
                    "needs_classification",
                    "information",
                    "requirement_groups",
                    "review_groups",
                    "semantic_annotations",
                )
            },
            "semantic_coverage": {
                "total_units": coverage.get("total_units"),
                "blocked_unit_count": coverage.get("blocked_unit_count"),
                "unresolved_unit_count": coverage.get("unresolved_unit_count"),
                "terminal_receipt_count": coverage.get("terminal_receipt_count"),
                "planning_runtime_version": coverage.get(
                    "planning_runtime_version"
                ),
                "classifier_runtime_version": coverage.get(
                    "classifier_runtime_version"
                ),
                "assembler_runtime_version": coverage.get(
                    "assembler_runtime_version"
                ),
                "document_runtime_version": coverage.get(
                    "document_runtime_version"
                ),
            },
            "review_session": {
                "schema_version": llm_review.get("schema_version"),
                "session_id": session_id,
                "prediction_id": prediction.get("prediction_id"),
                "prediction_schema_version": prediction.get("schema_version"),
                "review_schema_version": _as_dict(
                    llm_review.get("reviewed_result"),
                    "llm_reviewed_result_missing",
                ).get("schema_version"),
                "review_revision": review_revision,
                "structure_snapshot_id": prediction.get("structure_snapshot_id"),
                "semantic_decision_content_sha256": (
                    _semantic_decision_content_sha256(reviewed_result)
                ),
                "persistence_status": _as_dict(
                    llm_review.get("persistence"),
                    "llm_review_persistence_missing",
                ).get("status"),
            },
        },
        "structure": structure_receipt,
        "atomization": atomization_receipt,
        "refinement": refinement_receipt,
        "publication_preview": preview_receipt,
        "publication": publication_receipt,
        "selected_draft": selected_receipt,
    }


def _repeatability(rehearsals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_document: dict[str, list[Mapping[str, Any]]] = {}
    for rehearsal in rehearsals:
        for item in rehearsal.get("documents", []):
            if isinstance(item, dict):
                by_document.setdefault(
                    str(item.get("logical_document_id") or ""),
                    [],
                ).append(item)
    documents: list[dict[str, Any]] = []
    for logical_id, rows in sorted(by_document.items()):
        extraction = [
            _as_dict(item.get("extraction"), "repeatability_extraction_missing")
            for item in rows
        ]
        structures = [
            _as_dict(item.get("structure"), "repeatability_structure_missing")
            for item in rows
        ]
        stable_runtime = len(
            {
                (
                    item.get("planning_runtime_version"),
                    item.get("classifier_runtime_version"),
                    item.get("assembler_runtime_version"),
                    item.get("document_runtime_version"),
                    _canonical_json_bytes(item.get("visual_strategy")).decode("ascii"),
                )
                for item in extraction
            }
        ) == 1
        stable_structure_runtime = len(
            {
                (
                    item.get("schema_version"),
                    item.get("source_ir_adapter_version"),
                )
                for item in structures
            }
        ) == 1
        input_bindings = [
            {
                "source_sha256": row.get("source_sha256"),
                "visual_strategy": row.get("extraction", {}).get(
                    "visual_strategy"
                ),
                "semantic_provider": row.get("extraction", {}).get(
                    "semantic_provider"
                ),
                "structure_schema_version": row.get("structure", {}).get(
                    "schema_version"
                ),
                "source_ir_adapter_version": row.get("structure", {}).get(
                    "source_ir_adapter_version"
                ),
                "page_count": row.get("structure", {}).get("page_count"),
                "effective_tree_sha256": row.get("structure", {}).get(
                    "effective_tree_sha256"
                ),
                "revision_chain_sha256": row.get("structure", {}).get(
                    "revision_chain_sha256"
                ),
                "structure_revision": row.get("structure", {}).get("revision"),
                "review_revision": row.get("extraction", {})
                .get("review_session", {})
                .get("review_revision"),
            }
            for row in rows
        ]
        content_digests = [
            {
                "semantic_decision_content_sha256": row.get("extraction", {})
                .get("review_session", {})
                .get("semantic_decision_content_sha256"),
                "effective_tree_sha256": row.get("structure", {}).get(
                    "effective_tree_sha256"
                ),
                "atomization_content_sha256": row.get("atomization", {}).get(
                    "content_sha256"
                ),
                "publication_content_sha256": row.get(
                    "publication_preview", {}
                ).get("content_sha256"),
            }
            for row in rows
        ]
        audit_instance_identities = [
            {
                "job_id": row.get("extraction", {}).get("job_id"),
                "session_id": row.get("extraction", {})
                .get("review_session", {})
                .get("session_id"),
                "prediction_id": row.get("extraction", {})
                .get("review_session", {})
                .get("prediction_id"),
                "structure_snapshot_id": row.get("extraction", {})
                .get("review_session", {})
                .get("structure_snapshot_id"),
                "atomization_run_id": row.get("atomization", {}).get(
                    "atomization_run_id"
                ),
                "publication_preview_id": row.get(
                    "publication_preview", {}
                ).get("preview_id"),
                "publication_key": row.get("publication_preview", {}).get(
                    "publication_key"
                ),
                "requirement_set_id": row.get("publication", {}).get(
                    "requirement_set_id"
                ),
                "selected_requirement_set_id": row.get("selected_draft", {}).get(
                    "requirement_set_id"
                ),
            }
            for row in rows
        ]
        input_binding_stable = len(
            {
                _sha256_bytes(_canonical_json_bytes(item))
                for item in input_bindings
            }
        ) == 1
        content_digest_stable = len(
            {
                _sha256_bytes(_canonical_json_bytes(item))
                for item in content_digests
            }
        ) == 1
        audit_instance_identity_count = len(
            {
                _sha256_bytes(_canonical_json_bytes(item))
                for item in audit_instance_identities
            }
        )
        shape_signatures = [
            {
                "classification_counts": item.get("classification_counts"),
                "semantic_coverage": item.get("semantic_coverage"),
                "atomic_obligation_count": rows[index].get("atomization", {}).get(
                    "atomic_obligation_count"
                ),
                "publication_entry_count": rows[index]
                .get("publication_preview", {})
                .get("entry_count"),
            }
            for index, item in enumerate(extraction)
        ]
        documents.append(
            {
                "logical_document_id": logical_id,
                "run_count": len(rows),
                "runtime_binding_stable": stable_runtime,
                "structure_runtime_stable": stable_structure_runtime,
                "input_binding_stable": input_binding_stable,
                "content_digest_stable": content_digest_stable,
                "result_shape_stable": len(
                    {
                        _sha256_bytes(_canonical_json_bytes(item))
                        for item in shape_signatures
                    }
                )
                == 1,
                "publication_idempotency_verified": all(
                    item.get("publication", {}).get("idempotent") is True
                    and item.get("publication", {}).get("requirement_set_id")
                    == item.get("selected_draft", {}).get(
                        "requirement_set_id"
                    )
                    for item in rows
                ),
                "audit_instance_identity_count": (
                    audit_instance_identity_count
                ),
                "audit_instance_ids_varied": (
                    audit_instance_identity_count > 1
                ),
                "all_runs_passed": all(item.get("status") == "PASS" for item in rows),
            }
        )
    return {
        "document_count": len(documents),
        "documents": documents,
        "all_runtime_bindings_stable": all(
            item["runtime_binding_stable"] and item["structure_runtime_stable"]
            for item in documents
        ),
        "all_input_bindings_stable": all(
            item["input_binding_stable"] for item in documents
        ),
        "all_content_digests_stable": all(
            item["content_digest_stable"] for item in documents
        ),
        "all_result_shapes_stable": all(
            item["result_shape_stable"] for item in documents
        ),
        "all_publications_idempotent": all(
            item["publication_idempotency_verified"] for item in documents
        ),
        "audit_instance_ids_varied": any(
            item["audit_instance_ids_varied"] for item in documents
        ),
        "all_runs_passed": (
            all(item["all_runs_passed"] for item in documents)
            and all(
                rehearsal.get("status") == "PASS"
                for rehearsal in rehearsals
            )
        ),
    }


def _assert_receipt_text_free(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_RECEIPT_KEYS or normalized.endswith("_text"):
                raise GateFailure("receipt_forbidden_field", stage="receipt")
            _assert_receipt_text_free(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_receipt_text_free(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        if any(ord(character) < 9 for character in value):
            raise GateFailure("receipt_control_character", stage="receipt")
        if not _SAFE_RECEIPT_STRING_RE.fullmatch(value):
            raise GateFailure("receipt_string_not_text_free", stage="receipt")


def build_receipt(
    *,
    base_url: str,
    project_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    rehearsals: list[dict[str, Any]],
    started_at_utc: str,
    finished_at_utc: str,
    failure: GateFailure | None = None,
) -> dict[str, Any]:
    parsed = urlsplit(base_url)
    with _gate_stage("repeatability"):
        repeatability = _repeatability(rehearsals)
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "FAIL" if failure is not None else "PASS",
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "service": {
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": parsed.port,
        },
        "project_id": project_id,
        "manifest": {
            "repository_path": str(manifest_path.resolve().relative_to(REPOSITORY_ROOT)),
            "sha256": manifest_sha256,
        },
        "rehearsal_count": len(rehearsals),
        "rehearsals": rehearsals,
        "repeatability": repeatability,
        "safety": {
            "service_lifecycle_managed": False,
            "requirement_set_activation_called": False,
            "customer_text_persisted": False,
            "raw_provider_payload_persisted": False,
            "serial_execution": True,
        },
    }
    if failure is not None:
        receipt["failure"] = {
            "code": failure.code,
            "stage": failure.stage or None,
            "logical_document_id": failure.logical_document_id or None,
            "http_status": failure.http_status,
        }
    _assert_receipt_text_free(receipt)
    receipt["receipt_sha256"] = _sha256_bytes(_canonical_json_bytes(receipt))
    return receipt


def _minimal_failure_rehearsals(
    rehearsals: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for fallback_index, rehearsal in enumerate(rehearsals, start=1):
        raw_documents = rehearsal.get("documents")
        documents = raw_documents if isinstance(raw_documents, list) else []
        safe_documents: list[dict[str, Any]] = []
        for document in documents:
            if not isinstance(document, dict):
                continue
            source_sha256 = str(document.get("source_sha256") or "")
            safe_documents.append(
                {
                    "logical_document_id": _safe_code(
                        document.get("logical_document_id"),
                        fallback="redacted-document",
                    ),
                    "application_document_id": _safe_code(
                        document.get("application_document_id"),
                        fallback="redacted-document",
                    ),
                    "source_sha256": (
                        source_sha256
                        if _SHA256_RE.fullmatch(source_sha256)
                        else None
                    ),
                    "status": _safe_code(
                        document.get("status"),
                        fallback="UNKNOWN",
                    ),
                }
            )
        raw_index = rehearsal.get("rehearsal_index")
        rehearsal_index = (
            raw_index
            if isinstance(raw_index, int)
            and not isinstance(raw_index, bool)
            and raw_index >= 1
            else fallback_index
        )
        summaries.append(
            {
                "rehearsal_index": rehearsal_index,
                "status": _safe_code(
                    rehearsal.get("status"),
                    fallback="UNKNOWN",
                ),
                "completed_document_count": len(safe_documents),
                "documents": safe_documents,
            }
        )
    return summaries


def build_minimal_failure_receipt(
    *,
    base_url: str,
    project_id: str,
    manifest_sha256: str,
    rehearsals: Sequence[Mapping[str, Any]],
    started_at_utc: str,
    finished_at_utc: str,
    failure: GateFailure,
) -> dict[str, Any]:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        parsed = urlsplit("")
        port = None
    safe_manifest_sha256 = (
        manifest_sha256
        if _SHA256_RE.fullmatch(str(manifest_sha256 or ""))
        else None
    )
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "FAIL",
        "minimal_failure_receipt": True,
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "service": {
            "scheme": _safe_code(parsed.scheme, fallback="unknown"),
            "host": _safe_code(parsed.hostname, fallback="unknown"),
            "port": port,
        },
        "project_id": _safe_code(project_id, fallback="redacted-project"),
        "manifest": {"sha256": safe_manifest_sha256},
        "rehearsal_count": len(rehearsals),
        "rehearsals": _minimal_failure_rehearsals(rehearsals),
        "repeatability": {"status": "NOT_EVALUATED"},
        "failure": {
            "code": failure.code,
            "stage": failure.stage or None,
            "logical_document_id": failure.logical_document_id or None,
            "http_status": failure.http_status,
        },
        "safety": {
            "service_lifecycle_managed": False,
            "requirement_set_activation_called": False,
            "customer_text_persisted": False,
            "raw_provider_payload_persisted": False,
            "serial_execution": True,
        },
    }
    _assert_receipt_text_free(receipt)
    receipt["receipt_sha256"] = _sha256_bytes(_canonical_json_bytes(receipt))
    return receipt


def build_failure_receipt(
    *,
    base_url: str,
    project_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    rehearsals: list[dict[str, Any]],
    started_at_utc: str,
    finished_at_utc: str,
    failure: GateFailure,
) -> dict[str, Any]:
    try:
        return build_receipt(
            base_url=base_url,
            project_id=project_id,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            rehearsals=rehearsals,
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc,
            failure=failure,
        )
    except (GateFailure, OSError, ValueError):
        return build_minimal_failure_receipt(
            base_url=base_url,
            project_id=project_id,
            manifest_sha256=manifest_sha256,
            rehearsals=rehearsals,
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc,
            failure=failure,
        )


def _write_receipt(path: Path, receipt: Mapping[str, Any], *, overwrite: bool) -> None:
    resolved = path.expanduser().resolve()
    if resolved.exists() and not overwrite:
        raise GateFailure("receipt_output_exists", stage="configuration")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    temporary.write_bytes(
        json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    temporary.replace(resolved)


def run_acceptance(
    transport: JsonTransport,
    *,
    base_url: str,
    project_id: str,
    manifest_path: Path = DEFAULT_MANIFEST,
    logical_ids: Sequence[str] = CANONICAL_DOCUMENTS,
    application_document_overrides: Mapping[str, str] | None = None,
    rehearsals: int = 1,
    poll_interval_seconds: float = 1.0,
    extraction_timeout_seconds: float = 7200.0,
    atomization_timeout_seconds: float = 7200.0,
    atomization_attempts: int = 8,
) -> dict[str, Any]:
    _validated_local_base_url(base_url)
    _require(bool(project_id.strip()), "project_id_missing")
    _require(1 <= rehearsals <= 10, "rehearsal_count_invalid")
    _require(bool(logical_ids), "document_selection_empty")
    _require(
        len(logical_ids) == len(set(logical_ids))
        and all(item in CANONICAL_DOCUMENTS for item in logical_ids),
        "document_selection_invalid",
    )
    source_hashes, manifest_sha256 = _manifest_documents(manifest_path, logical_ids)
    options = RunOptions(
        project_id=project_id,
        poll_interval_seconds=poll_interval_seconds,
        extraction_timeout_seconds=extraction_timeout_seconds,
        atomization_timeout_seconds=atomization_timeout_seconds,
        atomization_attempts=atomization_attempts,
    )
    started = _now()
    _validate_service(transport)
    targets = resolve_targets(
        transport,
        project_id=project_id,
        logical_ids=logical_ids,
        source_hashes=source_hashes,
        application_document_overrides=application_document_overrides,
    )
    results: list[dict[str, Any]] = []
    for rehearsal_index in range(1, rehearsals + 1):
        documents: list[dict[str, Any]] = []
        for target in targets:
            try:
                documents.append(
                    run_document(
                        transport,
                        target=target,
                        options=options,
                        rehearsal_index=rehearsal_index,
                    )
                )
            except GateFailure as exc:
                if not exc.logical_document_id:
                    exc.logical_document_id = target.logical_id
                exc.partial_rehearsals = [
                    *results,
                    {
                        "rehearsal_index": rehearsal_index,
                        "status": "FAIL",
                        "documents": documents,
                    },
                ]
                raise
        results.append(
            {
                "rehearsal_index": rehearsal_index,
                "status": "PASS",
                "documents": documents,
            }
        )
    try:
        with _gate_stage("receipt"):
            receipt = build_receipt(
                base_url=base_url,
                project_id=project_id,
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                rehearsals=results,
                started_at_utc=started,
                finished_at_utc=_now(),
            )
        with _gate_stage("repeatability"):
            repeatability = _as_dict(
                receipt.get("repeatability"),
                "repeatability_receipt_missing",
            )
            for field, code in (
                (
                    "all_runtime_bindings_stable",
                    "repeatability_runtime_binding_drift",
                ),
                (
                    "all_input_bindings_stable",
                    "repeatability_input_binding_drift",
                ),
                (
                    "all_content_digests_stable",
                    "repeatability_content_digest_drift",
                ),
                (
                    "all_result_shapes_stable",
                    "repeatability_result_shape_drift",
                ),
                (
                    "all_publications_idempotent",
                    "repeatability_publication_idempotency_failed",
                ),
                ("all_runs_passed", "repeatability_run_failed"),
            ):
                _require(repeatability.get(field) is True, code)
    except GateFailure as exc:
        exc.partial_rehearsals = results
        raise
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the canonical ReqPilotX Document Requirement Agent HTTP "
            "acceptance flow without managing the target service."
        )
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--document-id",
        action="append",
        choices=CANONICAL_DOCUMENTS,
        dest="document_ids",
        help="Run a canonical subset; repeat for multiple documents.",
    )
    parser.add_argument(
        "--app-document",
        action="append",
        default=[],
        metavar="LOGICAL_ID=APPLICATION_ID",
        help="Disambiguate one application document registration.",
    )
    parser.add_argument("--rehearsals", type=int, default=1)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--http-timeout", type=float, default=900.0)
    parser.add_argument("--extraction-timeout", type=float, default=7200.0)
    parser.add_argument("--atomization-timeout", type=float, default=7200.0)
    parser.add_argument("--atomization-attempts", type=int, default=8)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = _now()
    manifest_sha = ""
    base_url = str(args.base_url)
    try:
        base_url = _validated_local_base_url(base_url)
        logical_ids = tuple(args.document_ids or CANONICAL_DOCUMENTS)
        overrides = _parse_document_overrides(args.app_document)
        client = JsonHttpClient(base_url, timeout_seconds=args.http_timeout)
        receipt = run_acceptance(
            client,
            base_url=base_url,
            project_id=args.project_id,
            manifest_path=args.manifest,
            logical_ids=logical_ids,
            application_document_overrides=overrides,
            rehearsals=args.rehearsals,
            poll_interval_seconds=args.poll_interval,
            extraction_timeout_seconds=args.extraction_timeout,
            atomization_timeout_seconds=args.atomization_timeout,
            atomization_attempts=args.atomization_attempts,
        )
        _write_receipt(args.out, receipt, overwrite=args.overwrite)
        print(
            f"PASS receipt={args.out.resolve()} "
            f"sha256={receipt['receipt_sha256']}"
        )
        return 0
    except GateFailure as exc:
        try:
            if args.manifest.exists():
                manifest_sha = _sha256_bytes(args.manifest.read_bytes())
            failure_receipt = build_failure_receipt(
                base_url=base_url,
                project_id=str(args.project_id),
                manifest_path=args.manifest,
                manifest_sha256=manifest_sha,
                rehearsals=exc.partial_rehearsals,
                started_at_utc=started,
                finished_at_utc=_now(),
                failure=exc,
            )
            _write_receipt(args.out, failure_receipt, overwrite=args.overwrite)
        except (GateFailure, OSError, ValueError):
            pass
        print(
            f"FAIL code={exc.code} stage={exc.stage or 'unknown'} "
            f"document={exc.logical_document_id or 'unknown'}",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as exc:
        code = _safe_code(type(exc).__name__.lower(), fallback="acceptance_io_error")
        print(f"FAIL code={code} stage=configuration", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
