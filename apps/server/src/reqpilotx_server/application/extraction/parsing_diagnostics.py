from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.adapters.persistence.parsing_risk_acknowledgements import (
    get_parsing_risk_acknowledgement,
    record_parsing_risk_acknowledgement,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction.source_page_facts import (
    _current_document_source_page_snapshot,
    _read_verified_json_artifact,
)
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.services.extraction_shadow_runs import SHADOW_DETAILS_SCHEMA_VERSION


# v2 changes the document-level counting unit from one risk per source
# reference to one risk per diagnostic/finding.  Keeping this version in the
# parse identity intentionally invalidates acknowledgements issued against the
# inflated v1 count while the underlying parse artifact remains unchanged.
PARSING_DIAGNOSTICS_SCHEMA_VERSION = "parsing-diagnostics/v2"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ParsingRiskAcknowledgementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_parse_artifact_id: str = Field(pattern=_SHA256_PATTERN)
    expected_parse_identity_sha256: str = Field(pattern=_SHA256_PATTERN)


def get_document_parsing_diagnostics(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    """Return one lightweight summary from the current persisted details artifact."""

    _document, snapshot = _current_document_source_page_snapshot(
        project_id=project_id,
        document_id=document_id,
    )
    summary = _build_current_summary(
        project_id=project_id,
        document_id=document_id,
        snapshot=snapshot,
    )
    acknowledgement = get_parsing_risk_acknowledgement(
        project_id=project_id,
        document_id=document_id,
        source_sha256=str(summary["source_sha256"]),
        parse_artifact_id=str(summary["active_parse_artifact_id"]),
        parse_identity_sha256=str(summary["parse_identity_sha256"]),
    )
    _set_response_headers(response)
    return _with_acknowledgement(summary, acknowledgement)


def acknowledge_document_parsing_risks(
    project_id: str,
    document_id: str,
    payload: ParsingRiskAcknowledgementRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    """Persist an idempotent acknowledgement bound to the current parse artifact."""

    _document, snapshot = _current_document_source_page_snapshot(
        project_id=project_id,
        document_id=document_id,
    )
    summary = _build_current_summary(
        project_id=project_id,
        document_id=document_id,
        snapshot=snapshot,
    )
    expected = (
        payload.expected_source_sha256,
        payload.expected_parse_artifact_id,
        payload.expected_parse_identity_sha256,
    )
    current = (
        str(summary["source_sha256"]),
        str(summary["active_parse_artifact_id"]),
        str(summary["parse_identity_sha256"]),
    )
    if expected != current:
        raise ApplicationError(
            status_code=409,
            code="parsing_risk_identity_changed",
            detail={
                "code": "parsing_risk_identity_changed",
                "message": "解析结果已更新，请先查看当前风险摘要。",
            },
        )
    if int(summary["hard_blocker_count"]) > 0:
        raise ApplicationError(
            status_code=409,
            code="parsing_risk_acknowledgement_blocked",
            detail={
                "code": "parsing_risk_acknowledgement_blocked",
                "message": "当前解析结果存在硬阻断，不能以风险知晓代替恢复处理。",
            },
        )
    severe_warning_count = int(summary["severe_warning_count"])
    if severe_warning_count < 1:
        raise ApplicationError(
            status_code=409,
            code="parsing_risk_acknowledgement_not_required",
            detail={
                "code": "parsing_risk_acknowledgement_not_required",
                "message": "当前解析结果没有需要知晓后继续的严重风险。",
            },
        )
    acknowledgement = record_parsing_risk_acknowledgement(
        project_id=project_id,
        document_id=document_id,
        source_sha256=current[0],
        parse_artifact_id=current[1],
        parse_identity_sha256=current[2],
        severe_warning_count=severe_warning_count,
    )
    _latest_document, latest_snapshot = _current_document_source_page_snapshot(
        project_id=project_id,
        document_id=document_id,
    )
    latest_summary = _build_current_summary(
        project_id=project_id,
        document_id=document_id,
        snapshot=latest_snapshot,
    )
    if _summary_identity(latest_summary) != current:
        raise ApplicationError(
            status_code=409,
            code="parsing_risk_identity_changed",
            detail={
                "code": "parsing_risk_identity_changed",
                "message": "解析结果已更新，请先查看当前风险摘要。",
            },
        )
    _set_response_headers(response)
    return _with_acknowledgement(latest_summary, acknowledgement)


def _build_current_summary(
    *,
    project_id: str,
    document_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    details_descriptor = _record(snapshot.get("details"))
    payload_bytes = _read_verified_json_artifact(
        details_descriptor,
        not_found_message="The parsing diagnostics artifact is missing.",
    )
    try:
        details = json.loads(payload_bytes)
    except (TypeError, json.JSONDecodeError) as cause:
        raise _invalid_details() from cause
    if not isinstance(details, dict):
        raise _invalid_details()
    snapshot_id = str(snapshot.get("snapshot_id") or "")
    if (
        str(details.get("schema_version") or "")
        != SHADOW_DETAILS_SCHEMA_VERSION
        or str(details.get("run_id") or "") != snapshot_id
        or str(details.get("project_id") or "") != project_id
        or str(details.get("document_id") or "") != document_id
    ):
        raise _invalid_details()
    return _build_parsing_risk_summary(
        snapshot=snapshot,
        details=details,
        details_sha256=str(details_descriptor.get("sha256") or ""),
    )


def _build_parsing_risk_summary(
    *,
    snapshot: dict[str, Any],
    details: dict[str, Any],
    details_sha256: str,
) -> dict[str, Any]:
    risks = _parsing_risks(details)
    hard_blockers = [risk for risk in risks if risk["blocking"]]
    severe_warnings = [risk for risk in risks if not risk["blocking"]]
    pages: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "hard_blocker_count": 0,
            "severe_warning_count": 0,
            "risk_ids": [],
            "issue_codes": [],
        }
    )
    for risk in risks:
        # A Structure finding is one risk even when it is grounded on several
        # source references.  Page summaries intentionally repeat that same
        # risk identity on every locatable page so navigation remains complete
        # without inflating the document-level count.
        for page_number in _risk_page_numbers(risk):
            page = pages[page_number]
            key = (
                "hard_blocker_count"
                if risk["blocking"]
                else "severe_warning_count"
            )
            page[key] += 1
            page["risk_ids"].append(risk["id"])
            page["issue_codes"].append(risk["code"])
    risk_pages = [
        {
            "page_number": page_number,
            "hard_blocker_count": page["hard_blocker_count"],
            "severe_warning_count": page["severe_warning_count"],
            "risk_ids": sorted(set(page["risk_ids"])),
            "issue_codes": sorted(set(page["issue_codes"])),
        }
        for page_number, page in sorted(pages.items())
    ]
    source_sha256 = str(snapshot.get("source_sha256") or "")
    parse_artifact_id = str(snapshot.get("snapshot_id") or "")
    identity_payload = {
        "schema_version": PARSING_DIAGNOSTICS_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "active_parse_artifact_id": parse_artifact_id,
        "details_sha256": details_sha256,
    }
    parse_identity_sha256 = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    status = (
        "BLOCKED"
        if hard_blockers
        else "READY_WITH_RISKS"
        if severe_warnings
        else "READY"
    )
    return {
        "schema_version": PARSING_DIAGNOSTICS_SCHEMA_VERSION,
        "status": status,
        "source_sha256": source_sha256,
        "active_parse_artifact_id": parse_artifact_id,
        "parse_identity_sha256": parse_identity_sha256,
        "page_count": max(0, int(snapshot.get("page_count") or 0)),
        "hard_blocker_count": len(hard_blockers),
        "severe_warning_count": len(severe_warnings),
        "risk_count": len(risks),
        "severe_issue_ids": sorted(risk["id"] for risk in severe_warnings),
        "blocking_issue_ids": sorted(risk["id"] for risk in hard_blockers),
        "unassigned_risk_count": sum(not _risk_page_numbers(risk) for risk in risks),
        "risk_pages": risk_pages,
        "risks": risks,
        "risk_acknowledged_at": None,
        "risk_acknowledged_artifact_id": None,
    }


def _summary_identity(summary: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(summary.get("source_sha256") or ""),
        str(summary.get("active_parse_artifact_id") or ""),
        str(summary.get("parse_identity_sha256") or ""),
    )


def _parsing_risks(details: dict[str, Any]) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    for index, raw in enumerate(_list(details.get("diagnostics"))):
        diagnostic = _record(raw)
        severity = str(diagnostic.get("severity") or "").strip().lower()
        if severity not in {"warning", "error"}:
            continue
        location = _record(diagnostic.get("location"))
        locations = [dict(location)] if location else []
        page_numbers = _location_page_numbers(locations)
        code = str(diagnostic.get("code") or f"SOURCE_DIAGNOSTIC_{index + 1}")
        risks.append(
            {
                "id": f"source-diagnostic:{index}:{code}",
                "code": code,
                "severity": severity,
                "blocking": severity == "error",
                "stage": "SOURCE_IR",
                "message": str(diagnostic.get("message") or code),
                # page_number remains the backward-compatible primary locator;
                # locations/page_numbers retain the complete location set.
                "page_number": page_numbers[0] if page_numbers else None,
                "location": dict(location) if location else None,
                "page_numbers": page_numbers,
                "locations": locations,
            }
        )
    for finding_index, raw in enumerate(_list(details.get("findings"))):
        finding = _record(raw)
        severity = str(finding.get("severity") or "").strip().lower()
        blocking = bool(finding.get("blocking"))
        if not blocking and severity not in {"warning", "error"}:
            continue
        code = str(finding.get("code") or f"STRUCTURE_FINDING_{finding_index + 1}")
        finding_id = str(finding.get("id") or f"structure-finding-{finding_index + 1}")
        locations = [
            dict(location)
            for raw_ref in _list(finding.get("source_refs"))
            if (location := _record(_record(raw_ref).get("location")))
        ]
        page_numbers = _location_page_numbers(locations)
        risks.append(
            {
                # Keep the former first-reference identity for compatibility,
                # but never manufacture :1, :2, ... risks for other refs.
                "id": f"{finding_id}:0",
                "finding_id": finding_id,
                "code": code,
                "severity": severity or ("error" if blocking else "warning"),
                "blocking": blocking,
                "stage": "STRUCTURE",
                "message": str(finding.get("message_key") or code),
                "page_number": page_numbers[0] if page_numbers else None,
                "location": locations[0] if locations else None,
                "page_numbers": page_numbers,
                "locations": locations,
            }
        )
    return sorted(
        risks,
        key=lambda risk: (
            risk["page_number"] if isinstance(risk["page_number"], int) else 10**9,
            str(risk["id"]),
        ),
    )


def _risk_page_numbers(risk: dict[str, Any]) -> list[int]:
    """Return unique valid pages while accepting legacy primary-only risks."""

    page_numbers = _list(risk.get("page_numbers"))
    if not page_numbers:
        page_numbers = [risk.get("page_number")]
    result: list[int] = []
    seen: set[int] = set()
    for value in page_numbers:
        page_number = _positive_int(value)
        if page_number is None or page_number in seen:
            continue
        result.append(page_number)
        seen.add(page_number)
    return result


def _location_page_numbers(locations: list[dict[str, Any]]) -> list[int]:
    return _risk_page_numbers(
        {
            "page_numbers": [
                location.get("page_number") for location in locations
            ]
        }
    )


def _with_acknowledgement(
    summary: dict[str, Any],
    acknowledgement: dict[str, Any] | None,
) -> dict[str, Any]:
    if (
        acknowledgement is None
        or int(acknowledgement.get("severe_warning_count") or -1)
        != int(summary.get("severe_warning_count") or 0)
    ):
        return dict(summary)
    return {
        **summary,
        "risk_acknowledged_at": str(acknowledgement.get("acknowledged_at") or "")
        or None,
        "risk_acknowledged_artifact_id": str(
            acknowledgement.get("parse_artifact_id") or ""
        )
        or None,
    }


def _invalid_details() -> ApplicationError:
    return ApplicationError(
        status_code=409,
        code="parsing_diagnostics_identity_mismatch",
        detail={
            "code": "parsing_diagnostics_identity_mismatch",
            "message": "解析诊断工件与当前来源身份不一致。",
        },
    )


def _set_response_headers(response: ResponseSink) -> None:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    response.headers.update(
        {
            **_ASSET_RESPONSE_HEADERS,
            "Cache-Control": "private, no-store",
        }
    )


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


__all__ = [
    "PARSING_DIAGNOSTICS_SCHEMA_VERSION",
    "ParsingRiskAcknowledgementRequest",
    "acknowledge_document_parsing_risks",
    "get_document_parsing_diagnostics",
]
