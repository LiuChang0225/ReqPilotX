"""B3 immutable shadow Structure Proposal contracts and artifact host."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from reqpilotx_server.adapters.agents.codex_app_server.pdf_tool_host import (
    PdfCanaryContext,
    canonical_json,
    canonical_sha256,
    structure_task_payload,
    validate_structure_proposal,
)
from reqpilotx_server.adapters.agents.codex_app_server.read_only_mcp import BusinessWriteSpy


SHADOW_STRUCTURE_ENVELOPE_SCHEMA = "shadow-structure-proposal/v1"
SHADOW_STRUCTURE_RECEIPT_SCHEMA = "shadow-structure-proposal-receipt/v1"
SHADOW_STRUCTURE_MCP_STAGE_SCHEMA = "shadow-structure-mcp-stage/v1"
BASE_STRUCTURE_IDENTITY_SCHEMA = "structure-base-identity/v1"
DEFAULT_SHADOW_TTL_SECONDS = 900
MAX_SHADOW_TTL_SECONDS = 1800
_PROPOSAL_ID_PATTERN = re.compile(r"^shadow-structure-proposal-[0-9a-f]{32}$")


class ShadowStructureError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class ShadowStructureContext:
    pdf_context: PdfCanaryContext
    understanding_proposal: Mapping[str, Any]
    understanding_proposal_sha256: str
    coverage_receipt: Mapping[str, Any]
    coverage_receipt_sha256: str
    base_structure_identity: Mapping[str, Any]
    issued_at_utc: str
    expires_at_utc: str
    task_payload: Mapping[str, Any]


def prepare_shadow_structure_context(
    *,
    pdf_context: PdfCanaryContext,
    understanding_proposal: Mapping[str, Any],
    understanding_proposal_sha256: str,
    coverage_receipt: Mapping[str, Any],
    clock: Callable[[], datetime] | None = None,
    ttl_seconds: int = DEFAULT_SHADOW_TTL_SECONDS,
) -> ShadowStructureContext:
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_SHADOW_TTL_SECONDS:
        raise ShadowStructureError("SHADOW_TTL_INVALID")
    if canonical_sha256(understanding_proposal) != understanding_proposal_sha256:
        raise ShadowStructureError("UNDERSTANDING_PROPOSAL_SHA_MISMATCH")
    coverage_sha256 = canonical_sha256(coverage_receipt)
    if (
        coverage_receipt.get("proposal_sha256") != understanding_proposal_sha256
        or coverage_receipt.get("source_sha256") != pdf_context.fixture.source_sha256
        or coverage_receipt.get("facts_sha256") != pdf_context.facts_sha256
        or coverage_receipt.get("apply_eligible") is not False
        or coverage_receipt.get("status") != "PARTIAL"
    ):
        raise ShadowStructureError("COVERAGE_RECEIPT_BINDING_INVALID")
    now = _utc(clock() if clock is not None else datetime.now(timezone.utc))
    expires = now + timedelta(seconds=ttl_seconds)
    base_payload = pdf_context.deterministic_tree.model_dump(
        mode="json",
        exclude_none=True,
    )
    base_identity = {
        "schema_version": BASE_STRUCTURE_IDENTITY_SCHEMA,
        "structure_snapshot_id": pdf_context.deterministic_tree.structure_snapshot_id,
        "structure_sha256": canonical_sha256(base_payload),
        "revision": 0,
    }
    issued_at = _format_utc(now)
    expires_at = _format_utc(expires)
    task = structure_task_payload(
        context=pdf_context,
        understanding_proposal=understanding_proposal,
        understanding_proposal_sha256=understanding_proposal_sha256,
        coverage_receipt=coverage_receipt,
    )
    task = {
        **task,
        "shadow_binding": {
            "base_structure_identity": base_identity,
            "issued_at_utc": issued_at,
            "expires_at_utc": expires_at,
            "apply_eligible": False,
            "serving_impact": "NONE",
        },
    }
    return ShadowStructureContext(
        pdf_context=pdf_context,
        understanding_proposal=dict(understanding_proposal),
        understanding_proposal_sha256=understanding_proposal_sha256,
        coverage_receipt=dict(coverage_receipt),
        coverage_receipt_sha256=coverage_sha256,
        base_structure_identity=base_identity,
        issued_at_utc=issued_at,
        expires_at_utc=expires_at,
        task_payload=task,
    )


def validate_shadow_structure_proposal(
    proposal: Mapping[str, Any],
    *,
    context: ShadowStructureContext,
    expected_base_structure_sha256: str,
    expected_base_revision: int,
    expected_expires_at_utc: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        expected_base_structure_sha256
        != context.base_structure_identity["structure_sha256"]
        or expected_base_revision != context.base_structure_identity["revision"]
    ):
        raise ShadowStructureError("BASE_STRUCTURE_STALE")
    if expected_expires_at_utc != context.expires_at_utc:
        raise ShadowStructureError("SHADOW_EXPIRY_STALE")
    issued = _parse_utc(context.issued_at_utc)
    expires = _parse_utc(context.expires_at_utc)
    observed_now = _utc(now if now is not None else datetime.now(timezone.utc))
    if expires <= issued or (expires - issued).total_seconds() > MAX_SHADOW_TTL_SECONDS:
        raise ShadowStructureError("SHADOW_EXPIRY_INVALID")
    if observed_now >= expires:
        raise ShadowStructureError("SHADOW_PROPOSAL_EXPIRED")
    try:
        proposal_sha256 = validate_structure_proposal(
            proposal,
            context=context.pdf_context,
            understanding_proposal_sha256=context.understanding_proposal_sha256,
            coverage_receipt=context.coverage_receipt,
        )
    except ValueError as exc:
        raise ShadowStructureError(f"PROPOSAL_REJECTED_{_safe_code(str(exc))}") from exc
    proposal_id = f"shadow-structure-proposal-{proposal_sha256[:32]}"
    envelope = {
        "schema_version": SHADOW_STRUCTURE_ENVELOPE_SCHEMA,
        "proposal_id": proposal_id,
        "source_sha256": context.pdf_context.fixture.source_sha256,
        "facts_sha256": context.pdf_context.facts_sha256,
        "page_ledger_sha256": context.pdf_context.page_ledger_sha256,
        "understanding_proposal_sha256": context.understanding_proposal_sha256,
        "coverage_receipt_sha256": context.coverage_receipt_sha256,
        "base_structure_identity": context.base_structure_identity,
        "issued_at_utc": context.issued_at_utc,
        "expires_at_utc": context.expires_at_utc,
        "structure_proposal": dict(proposal),
        "agent_claimed_status": proposal["agent_claimed_status"],
        "apply_eligible": False,
        "serving_impact": "NONE",
    }
    _validate_shadow_envelope_shape(envelope)
    validation_receipt = {
        "schema_version": "shadow-structure-validation-receipt/v1",
        "proposal_id": proposal_id,
        "proposal_sha256": proposal_sha256,
        "envelope_sha256": canonical_sha256(envelope),
        "base_structure_sha256": expected_base_structure_sha256,
        "base_revision": expected_base_revision,
        "expires_at_utc": expected_expires_at_utc,
        "status": "VALIDATED_SHADOW",
        "apply_eligible": False,
        "serving_impact": "NONE",
    }
    return envelope, validation_receipt


def shadow_structure_stage_payload(
    context: ShadowStructureContext,
) -> dict[str, Any]:
    payload = {
        "schema_version": SHADOW_STRUCTURE_MCP_STAGE_SCHEMA,
        "understanding_proposal": context.understanding_proposal,
        "understanding_proposal_sha256": context.understanding_proposal_sha256,
        "coverage_receipt": context.coverage_receipt,
        "coverage_receipt_sha256": context.coverage_receipt_sha256,
        "base_structure_identity": context.base_structure_identity,
        "issued_at_utc": context.issued_at_utc,
        "expires_at_utc": context.expires_at_utc,
        "task_payload": context.task_payload,
    }
    return payload


def shadow_structure_context_from_stage(
    *,
    pdf_context: PdfCanaryContext,
    stage: Mapping[str, Any],
) -> ShadowStructureContext:
    expected = {
        "schema_version",
        "understanding_proposal",
        "understanding_proposal_sha256",
        "coverage_receipt",
        "coverage_receipt_sha256",
        "base_structure_identity",
        "issued_at_utc",
        "expires_at_utc",
        "task_payload",
    }
    if (
        not isinstance(stage, Mapping)
        or set(stage) != expected
        or stage.get("schema_version") != SHADOW_STRUCTURE_MCP_STAGE_SCHEMA
        or not isinstance(stage.get("understanding_proposal"), Mapping)
        or not isinstance(stage.get("coverage_receipt"), Mapping)
        or not isinstance(stage.get("base_structure_identity"), Mapping)
        or not isinstance(stage.get("task_payload"), Mapping)
        or canonical_sha256(stage["understanding_proposal"])
        != stage.get("understanding_proposal_sha256")
        or canonical_sha256(stage["coverage_receipt"])
        != stage.get("coverage_receipt_sha256")
    ):
        raise ShadowStructureError("SHADOW_STAGE_INVALID")
    base = stage["base_structure_identity"]
    task = stage["task_payload"]
    if (
        base.get("revision") != 0
        or re.fullmatch(r"[0-9a-f]{64}", str(base.get("structure_sha256"))) is None
        or task.get("source_sha256") != pdf_context.fixture.source_sha256
        or task.get("facts_sha256") != pdf_context.facts_sha256
        or task.get("understanding_proposal_sha256")
        != stage["understanding_proposal_sha256"]
        or task.get("coverage_receipt_sha256") != stage["coverage_receipt_sha256"]
        or task.get("shadow_binding")
        != {
            "base_structure_identity": base,
            "issued_at_utc": stage["issued_at_utc"],
            "expires_at_utc": stage["expires_at_utc"],
            "apply_eligible": False,
            "serving_impact": "NONE",
        }
    ):
        raise ShadowStructureError("SHADOW_STAGE_BASE_MISMATCH")
    _parse_utc(str(stage["issued_at_utc"]))
    _parse_utc(str(stage["expires_at_utc"]))
    if (
        pdf_context.deterministic_tree is not None
        and base.get("structure_snapshot_id")
        != pdf_context.deterministic_tree.structure_snapshot_id
    ):
        raise ShadowStructureError("SHADOW_STAGE_BASE_MISMATCH")
    return ShadowStructureContext(
        pdf_context=pdf_context,
        understanding_proposal=dict(stage["understanding_proposal"]),
        understanding_proposal_sha256=str(stage["understanding_proposal_sha256"]),
        coverage_receipt=dict(stage["coverage_receipt"]),
        coverage_receipt_sha256=str(stage["coverage_receipt_sha256"]),
        base_structure_identity=dict(base),
        issued_at_utc=str(stage["issued_at_utc"]),
        expires_at_utc=str(stage["expires_at_utc"]),
        task_payload=dict(stage["task_payload"]),
    )


def write_immutable_shadow_artifact(
    envelope: Mapping[str, Any],
    *,
    destination: Path,
    write_spy: BusinessWriteSpy | None = None,
) -> dict[str, Any]:
    _validate_shadow_envelope_shape(envelope)
    if (
        destination.exists()
        or destination.is_symlink()
        or not destination.is_absolute()
    ):
        raise ShadowStructureError("SHADOW_ARTIFACT_ALREADY_EXISTS")
    if (
        not destination.parent.is_dir()
        or destination.parent.is_symlink()
        or destination.parent.stat().st_uid != os.getuid()
        or destination.parent.stat().st_mode & 0o077
    ):
        raise ShadowStructureError("SHADOW_ARTIFACT_PARENT_INVALID")
    encoded = canonical_json(envelope).encode("utf-8")
    try:
        file_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ShadowStructureError("SHADOW_ARTIFACT_ALREADY_EXISTS") from exc
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if destination.read_bytes() != encoded or destination.stat().st_mode & 0o077:
        raise ShadowStructureError("SHADOW_ARTIFACT_VERIFICATION_FAILED")
    audit = (write_spy or BusinessWriteSpy()).receipt()
    if audit["total_writes"] != 0:
        raise ShadowStructureError("BUSINESS_WRITE_AUDIT_FAILED")
    return {
        "schema_version": SHADOW_STRUCTURE_RECEIPT_SCHEMA,
        "proposal_id": envelope["proposal_id"],
        "artifact_sha256": canonical_sha256(envelope),
        "artifact_size_bytes": len(encoded),
        "source_sha256": envelope["source_sha256"],
        "facts_sha256": envelope["facts_sha256"],
        "base_structure_sha256": envelope["base_structure_identity"][
            "structure_sha256"
        ],
        "base_revision": envelope["base_structure_identity"]["revision"],
        "issued_at_utc": envelope["issued_at_utc"],
        "expires_at_utc": envelope["expires_at_utc"],
        "immutable": True,
        "apply_eligible": False,
        "serving_impact": "NONE",
        "write_audit": audit,
    }


def _validate_shadow_envelope_shape(envelope: Mapping[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "proposal_id",
        "source_sha256",
        "facts_sha256",
        "page_ledger_sha256",
        "understanding_proposal_sha256",
        "coverage_receipt_sha256",
        "base_structure_identity",
        "issued_at_utc",
        "expires_at_utc",
        "structure_proposal",
        "agent_claimed_status",
        "apply_eligible",
        "serving_impact",
    }
    base = envelope.get("base_structure_identity")
    if (
        not isinstance(envelope, Mapping)
        or set(envelope) != expected_keys
        or envelope.get("schema_version") != SHADOW_STRUCTURE_ENVELOPE_SCHEMA
        or not isinstance(envelope.get("proposal_id"), str)
        or _PROPOSAL_ID_PATTERN.fullmatch(str(envelope.get("proposal_id"))) is None
        or not isinstance(base, Mapping)
        or set(base)
        != {
            "schema_version",
            "structure_snapshot_id",
            "structure_sha256",
            "revision",
        }
        or base.get("schema_version") != BASE_STRUCTURE_IDENTITY_SCHEMA
        or base.get("revision") != 0
        or envelope.get("apply_eligible") is not False
        or envelope.get("serving_impact") != "NONE"
        or envelope.get("agent_claimed_status") != "PARTIAL"
        or not isinstance(envelope.get("structure_proposal"), Mapping)
    ):
        raise ShadowStructureError("SHADOW_ENVELOPE_INVALID")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ShadowStructureError("SHADOW_CLOCK_INVALID")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ShadowStructureError("SHADOW_EXPIRY_INVALID") from exc
    return _utc(parsed)


def _safe_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()[:96] or "UNKNOWN"


__all__ = [
    "BASE_STRUCTURE_IDENTITY_SCHEMA",
    "DEFAULT_SHADOW_TTL_SECONDS",
    "SHADOW_STRUCTURE_ENVELOPE_SCHEMA",
    "SHADOW_STRUCTURE_MCP_STAGE_SCHEMA",
    "SHADOW_STRUCTURE_RECEIPT_SCHEMA",
    "ShadowStructureContext",
    "ShadowStructureError",
    "prepare_shadow_structure_context",
    "shadow_structure_context_from_stage",
    "shadow_structure_stage_payload",
    "validate_shadow_structure_proposal",
    "write_immutable_shadow_artifact",
]
