"""Durable Requirement Set impact receipts and carried assessment results.

The comparator proves equivalence; this adapter makes that proof durable and
applies it without ever mutating the historical Requirement Set.  SQLite owns
an intentionally lazy table bootstrap for isolated tests.  PostgreSQL schema
creation remains migration-only.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from reqpilotx_server import db
from reqpilotx_server.adapters.persistence.requirement_matrix_states import (
    REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION,
    lock_requirement_matrix_sets_in_connection,
    requirement_matrix_state_sha256_in_connection,
)
from reqpilotx_server.domain.requirement_set_impact import (
    RequirementSetImpactNodeV1,
    RequirementSetImpactOutcome,
    RequirementSetImpactReceiptV1,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_set_impact_receipts (
    receipt_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    old_requirement_set_id TEXT NOT NULL,
    new_requirement_set_id TEXT NOT NULL,
    old_tree_sha256 TEXT NOT NULL CHECK (length(old_tree_sha256) = 64),
    new_tree_sha256 TEXT NOT NULL CHECK (length(new_tree_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (new_requirement_set_id, payload_sha256),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE,
    FOREIGN KEY(old_requirement_set_id) REFERENCES requirement_sets(id) ON DELETE CASCADE,
    FOREIGN KEY(new_requirement_set_id) REFERENCES requirement_sets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_requirement_set_impact_receipts_pair
ON requirement_set_impact_receipts (
    old_requirement_set_id,
    new_requirement_set_id,
    created_at
);

CREATE INDEX IF NOT EXISTS idx_assessments_impact_receipt
ON assessments(impact_receipt_id)
WHERE impact_receipt_id <> '';
"""

_ASSESSMENT_RESULT_COLUMNS = (
    "profile_id",
    "provider",
    "retrieval_backend",
    "ai_decision",
    "compliance_status",
    "ai_reason",
    "ai_confidence",
    "assessment_mode",
    "analysis_label",
    "analysis_basis",
    "normalized_requirement",
    "risk_level",
    "clarification_question",
    "model_name",
    "prompt_version",
    "evidence_findings_json",
    "structured_information_gap_json",
    "sub_judgments_json",
)

_EVIDENCE_COLUMNS = (
    "source_doc_id",
    "source_doc_name",
    "source_doc_version",
    "retrieval_evidence_id",
    "content_hash",
    "evidence_contract_version",
    "content_kind",
    "evidence_metadata_json",
    "source_location",
    "excerpt",
    "relevance",
)


class RequirementSetImpactPersistenceError(RuntimeError):
    """The receipt cannot be proven against durable Requirement Set state."""


class RequirementSetImpactConflictError(RequirementSetImpactPersistenceError):
    """An immutable identity or a target-native result conflicts with the receipt."""


class RequirementSetImpactStateChangedError(RequirementSetImpactConflictError):
    """The receipt was calculated from an older mutable Matrix revision."""


@dataclass(frozen=True, slots=True)
class RequirementSetImpactApplyResult:
    receipt: RequirementSetImpactReceiptV1
    created: bool
    applied: bool
    retained_count: int
    stale_count: int
    cleared_count: int


def save_requirement_set_impact_receipt(
    receipt: RequirementSetImpactReceiptV1,
) -> tuple[RequirementSetImpactReceiptV1, bool]:
    """Persist one canonical receipt after validating its complete lineage."""

    normalized = _validated_receipt(receipt)
    with db.get_conn() as connection:
        _ensure_schema(connection)
        _begin_write(connection)
        _lock_receipt_matrix_sets(connection, normalized)
        _validate_lineage(connection, normalized, lock_target=True)
        _validate_matrix_state_binding(connection, normalized)
        _validate_node_bindings(connection, normalized)
        created = _insert_receipt(connection, normalized)
        stored = _get_receipt_in_connection(connection, normalized.receipt_id)
        if stored is None:
            raise RequirementSetImpactPersistenceError(
                "Requirement Set impact receipt disappeared during persistence."
            )
        return stored, created


def get_requirement_set_impact_receipt(
    receipt_id: str,
) -> RequirementSetImpactReceiptV1 | None:
    normalized_id = str(receipt_id or "").strip()
    if not normalized_id:
        raise ValueError("receipt_id must not be blank")
    with db.get_conn() as connection:
        _ensure_schema(connection)
        return _get_receipt_in_connection(connection, normalized_id)


def list_requirement_set_impact_receipts(
    *,
    project_id: str,
    document_id: str,
    new_requirement_set_id: str,
) -> tuple[RequirementSetImpactReceiptV1, ...]:
    scope = tuple(
        str(value or "").strip()
        for value in (project_id, document_id, new_requirement_set_id)
    )
    if any(not value for value in scope):
        raise ValueError("impact receipt scope must not contain blank identities")
    with db.get_conn() as connection:
        _ensure_schema(connection)
        rows = connection.execute(
            """
            SELECT *
            FROM requirement_set_impact_receipts
            WHERE project_id = ?
              AND sor_document_id = ?
              AND new_requirement_set_id = ?
            ORDER BY created_at ASC, receipt_id ASC
            """,
            scope,
        ).fetchall()
    return tuple(_deserialize_receipt(row) for row in rows)


def apply_requirement_set_impact_receipt(
    receipt: RequirementSetImpactReceiptV1,
    *,
    analysis_input_schema_version: str,
) -> RequirementSetImpactApplyResult:
    """Atomically persist a receipt and project its result into the new set.

    ``RETAINED`` and ``STALE`` both preserve the historical AI payload and its
    evidences.  Only retained results remain current, and only retained results
    preserve a human final decision.  Provider execution runs are deliberately
    outside the copy surface.
    """

    normalized = _validated_receipt(receipt)
    input_schema = str(analysis_input_schema_version or "").strip()
    if not input_schema:
        raise ValueError("analysis_input_schema_version must not be blank")
    if len(input_schema) > 160:
        raise ValueError("analysis_input_schema_version is too long")

    with db.get_conn() as connection:
        _ensure_schema(connection)
        _begin_write(connection)
        _lock_receipt_matrix_sets(connection, normalized)
        _validate_lineage(connection, normalized, lock_target=True, require_draft=True)
        _validate_matrix_state_binding(connection, normalized)
        bindings = _validate_node_bindings(connection, normalized)
        created = _insert_receipt(connection, normalized)
        if _receipt_is_fully_applied(
            connection,
            normalized,
            bindings=bindings,
            analysis_input_schema_version=input_schema,
        ):
            return RequirementSetImpactApplyResult(
                receipt=normalized,
                created=created,
                applied=False,
                retained_count=len(normalized.retained_result_node_ids),
                stale_count=len(normalized.stale_result_node_ids),
                cleared_count=0,
            )

        cleared = 0
        for node in normalized.node_results:
            if node.outcome in {
                RequirementSetImpactOutcome.RETAINED,
                RequirementSetImpactOutcome.STALE,
            }:
                source_assessment = bindings[node.node_id]["source_assessment"]
                assert source_assessment is not None
                _copy_assessment(
                    connection,
                    node=node,
                    source_assessment=source_assessment,
                    receipt=normalized,
                    analysis_input_schema_version=input_schema,
                )
                _copy_evidences(
                    connection,
                    source_requirement_id=str(node.old_requirement_id),
                    target_requirement_id=str(node.new_requirement_id),
                )
            elif node.outcome in {
                RequirementSetImpactOutcome.NEW,
                RequirementSetImpactOutcome.NO_PRIOR_RESULT,
            }:
                cleared += _clear_non_current_target_result(
                    connection,
                    target_requirement_id=str(node.new_requirement_id),
                )

        return RequirementSetImpactApplyResult(
            receipt=normalized,
            created=created,
            applied=True,
            retained_count=len(normalized.retained_result_node_ids),
            stale_count=len(normalized.stale_result_node_ids),
            cleared_count=cleared,
        )


def finalize_staged_requirement_set_activation(
    receipt: RequirementSetImpactReceiptV1,
    *,
    analysis_input_schema_version: str,
    workflow_stage: str = "REVIEWING",
    expected_active_requirement_set_id: str = "",
    analysis_target_node_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Verify one fully projected inactive DRAFT and atomically activate it.

    Applying an impact receipt and switching the document pointer are separate
    durability steps because the frozen tree lives in object storage.  This
    finalizer is the linearization point between them: the document lifecycle
    lock prevents another activation or cleanup, while the shared source/target
    Matrix locks prevent a late human decision from invalidating the receipt
    between its final verification and the active-set CAS.
    """

    normalized = _validated_receipt(receipt)
    input_schema = str(analysis_input_schema_version or "").strip()
    if not input_schema:
        raise ValueError("analysis_input_schema_version must not be blank")
    if len(input_schema) > 160:
        raise ValueError("analysis_input_schema_version is too long")
    requested_source_id = str(expected_active_requirement_set_id or "").strip()
    if requested_source_id and (
        requested_source_id != normalized.old_requirement_set_id
    ):
        raise RequirementSetImpactConflictError(
            "Staged activation expected a different active source Requirement Set."
        )
    if analysis_target_node_ids is None:
        # Compatibility callers fail closed: without an actual Matrix target
        # projection, every target-side candidate may still require analysis.
        normalized_analysis_target_ids = {
            node.node_id
            for node in normalized.node_results
            if node.new_requirement_id is not None
        }
    else:
        normalized_analysis_target_ids = {
            str(node_id or "").strip()
            for node_id in analysis_target_node_ids
        }
        if "" in normalized_analysis_target_ids:
            raise ValueError("analysis_target_node_ids must not contain blanks")
        target_node_ids = {
            node.node_id
            for node in normalized.node_results
            if node.new_requirement_id is not None
        }
        if not normalized_analysis_target_ids.issubset(target_node_ids):
            raise RequirementSetImpactConflictError(
                "Staged activation analysis targets are not bound by its impact receipt."
            )

    with db.get_conn() as connection:
        _ensure_schema(connection)
        _begin_write(connection)
        if not db._lock_requirement_set_document_in_conn(
            connection,
            project_id=normalized.project_id,
            sor_document_id=normalized.document_id,
        ):
            raise RequirementSetImpactPersistenceError(
                "Staged activation document scope does not exist."
            )
        _lock_receipt_matrix_sets(connection, normalized)
        _validate_staged_activation_state(connection, normalized)
        _validate_lineage(
            connection,
            normalized,
            lock_target=True,
            require_draft=True,
        )
        stored = _get_receipt_in_connection(
            connection,
            normalized.receipt_id,
        )
        if stored is None or stored != normalized:
            raise RequirementSetImpactConflictError(
                "Staged activation impact receipt is not durably persisted."
            )
        _validate_matrix_state_binding(connection, normalized)
        _validate_staged_matrix_clone(connection, normalized)
        bindings = _validate_node_bindings(connection, normalized)
        if not _receipt_is_fully_applied(
            connection,
            normalized,
            bindings=bindings,
            analysis_input_schema_version=input_schema,
        ):
            raise RequirementSetImpactConflictError(
                "Staged activation target does not fully match its impact receipt."
            )
        activated = db._activate_requirement_set_in_conn(
            connection,
            project_id=normalized.project_id,
            sor_document_id=normalized.document_id,
            requirement_set_id=normalized.new_requirement_set_id,
            workflow_stage=workflow_stage,
            expected_active_requirement_set_id=(
                normalized.old_requirement_set_id
            ),
            carried_ai_projection_source_requirement_set_id=(
                normalized.old_requirement_set_id
            ),
            carried_ai_projection_requires_analysis=any(
                node.node_id in normalized_analysis_target_ids
                and node.outcome
                in {
                    RequirementSetImpactOutcome.STALE,
                    RequirementSetImpactOutcome.NEW,
                    RequirementSetImpactOutcome.NO_PRIOR_RESULT,
                }
                for node in normalized.node_results
            ),
        )
        if str(activated.get("id") or "") != (
            normalized.new_requirement_set_id
        ):
            raise RequirementSetImpactConflictError(
                "Staged Requirement Set activation CAS did not select its target."
            )
        return activated


def _ensure_schema(connection: Any) -> None:
    if db.is_postgresql_runtime():
        return
    connection.executescript(_SQLITE_SCHEMA)


def _begin_write(connection: Any) -> None:
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")


def _lock_receipt_matrix_sets(
    connection: Any,
    receipt: RequirementSetImpactReceiptV1,
) -> None:
    lock_requirement_matrix_sets_in_connection(
        connection,
        project_id=receipt.project_id,
        document_id=receipt.document_id,
        requirement_set_ids=(
            receipt.old_requirement_set_id,
            receipt.new_requirement_set_id,
        ),
    )


def _validate_matrix_state_binding(
    connection: Any,
    receipt: RequirementSetImpactReceiptV1,
) -> None:
    if receipt.matrix_state_schema_version != (
        REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION
    ):
        raise RequirementSetImpactPersistenceError(
            "Impact receipt Matrix state schema is unsupported."
        )
    actual_old = requirement_matrix_state_sha256_in_connection(
        connection,
        project_id=receipt.project_id,
        document_id=receipt.document_id,
        requirement_set_id=receipt.old_requirement_set_id,
    )
    actual_new = requirement_matrix_state_sha256_in_connection(
        connection,
        project_id=receipt.project_id,
        document_id=receipt.document_id,
        requirement_set_id=receipt.new_requirement_set_id,
    )
    if (
        actual_old != receipt.old_matrix_state_sha256
        or actual_new != receipt.new_matrix_state_sha256
    ):
        raise RequirementSetImpactStateChangedError(
            "Impact receipt Matrix state changed before it could be applied."
        )


def _validate_staged_activation_state(
    connection: Any,
    receipt: RequirementSetImpactReceiptV1,
) -> None:
    row = connection.execute(
        """
        SELECT d.active_requirement_set_id,
               source.status AS source_status,
               source.is_active AS source_is_active,
               target.status AS target_status,
               target.is_active AS target_is_active,
               target.activated_at AS target_activated_at,
               target.supersedes_id AS target_supersedes_id
        FROM sor_documents d
        INNER JOIN requirement_sets source
          ON source.id = ?
         AND source.project_id = d.project_id
         AND source.sor_document_id = d.id
        INNER JOIN requirement_sets target
          ON target.id = ?
         AND target.project_id = d.project_id
         AND target.sor_document_id = d.id
        WHERE d.id = ? AND d.project_id = ?
        """,
        (
            receipt.old_requirement_set_id,
            receipt.new_requirement_set_id,
            receipt.document_id,
            receipt.project_id,
        ),
    ).fetchone()
    if row is None:
        raise RequirementSetImpactPersistenceError(
            "Staged activation Requirement Set scope does not exist."
        )
    if (
        str(row["active_requirement_set_id"] or "")
        != receipt.old_requirement_set_id
        or not bool(int(row["source_is_active"] or 0))
        or str(row["source_status"] or "").upper() != "LOCKED"
    ):
        raise RequirementSetImpactConflictError(
            "Staged activation source is no longer the active LOCKED Requirement Set."
        )
    if (
        str(row["target_status"] or "").upper() != "DRAFT"
        or bool(int(row["target_is_active"] or 0))
        or str(row["target_activated_at"] or "").strip()
        or str(row["target_supersedes_id"] or "")
        != receipt.old_requirement_set_id
    ):
        raise RequirementSetImpactConflictError(
            "Staged activation target must be an inactive, never-activated direct DRAFT."
        )


def _validate_staged_matrix_clone(
    connection: Any,
    receipt: RequirementSetImpactReceiptV1,
) -> None:
    """Reject activation while the staging Matrix copy is still incomplete."""

    def state_projection(requirement_set_id: str) -> tuple[tuple[str, str, str], ...]:
        rows = connection.execute(
            """
            SELECT node_id, disposition, reason
            FROM requirement_matrix_node_states
            WHERE project_id = ?
              AND sor_document_id = ?
              AND requirement_set_id = ?
            ORDER BY node_id ASC
            """,
            (
                receipt.project_id,
                receipt.document_id,
                requirement_set_id,
            ),
        ).fetchall()
        return tuple(
            (
                str(row["node_id"] or ""),
                str(row["disposition"] or ""),
                str(row["reason"] or ""),
            )
            for row in rows
        )

    if state_projection(receipt.old_requirement_set_id) != state_projection(
        receipt.new_requirement_set_id
    ):
        raise RequirementSetImpactConflictError(
            "Staged activation target Matrix has not finished cloning its source."
        )


def _validated_receipt(
    receipt: RequirementSetImpactReceiptV1,
) -> RequirementSetImpactReceiptV1:
    if not isinstance(receipt, RequirementSetImpactReceiptV1):
        raise TypeError("receipt must be a RequirementSetImpactReceiptV1")
    # Re-validation prevents model_construct or copied mutable internals from
    # bypassing the canonical identity and payload checks.
    return RequirementSetImpactReceiptV1.model_validate(
        receipt.model_dump(mode="json")
    )


def _validate_lineage(
    connection: Any,
    receipt: RequirementSetImpactReceiptV1,
    *,
    lock_target: bool,
    require_draft: bool = False,
) -> None:
    document = connection.execute(
        """
        SELECT d.id, d.project_id
        FROM sor_documents d
        INNER JOIN projects p ON p.id = d.project_id
        WHERE d.id = ? AND d.project_id = ?
        """,
        (receipt.document_id, receipt.project_id),
    ).fetchone()
    if document is None:
        raise RequirementSetImpactPersistenceError(
            "Impact receipt project/document scope does not exist."
        )

    suffix = " FOR UPDATE" if lock_target and db.is_postgresql_runtime() else ""
    old_set = connection.execute(
        """
        SELECT * FROM requirement_sets
        WHERE id = ? AND project_id = ? AND sor_document_id = ?
        """
        + suffix,
        (
            receipt.old_requirement_set_id,
            receipt.project_id,
            receipt.document_id,
        ),
    ).fetchone()
    new_set = connection.execute(
        """
        SELECT * FROM requirement_sets
        WHERE id = ? AND project_id = ? AND sor_document_id = ?
        """
        + suffix,
        (
            receipt.new_requirement_set_id,
            receipt.project_id,
            receipt.document_id,
        ),
    ).fetchone()
    if old_set is None or new_set is None:
        raise RequirementSetImpactPersistenceError(
            "Impact receipt Requirement Set scope does not exist."
        )
    if str(new_set["supersedes_id"] or "") != receipt.old_requirement_set_id:
        raise RequirementSetImpactPersistenceError(
            "Impact receipt target must directly supersede its source Requirement Set."
        )
    if int(new_set["version"] or 0) <= int(old_set["version"] or 0):
        raise RequirementSetImpactPersistenceError(
            "Impact receipt target Requirement Set version is not newer than its source."
        )
    if require_draft and str(new_set["status"] or "").upper() != "DRAFT":
        raise RequirementSetImpactConflictError(
            "Impact results may only be applied to a DRAFT Requirement Set."
        )

    old_tree = str(old_set["requirement_tree_sha256"] or "").lower()
    new_tree = str(new_set["requirement_tree_sha256"] or "").lower()
    if (
        _SHA256_RE.fullmatch(old_tree) is None
        or old_tree != receipt.old_tree_sha256
    ):
        raise RequirementSetImpactConflictError(
            "Impact receipt source tree hash no longer matches durable state."
        )
    if (
        _SHA256_RE.fullmatch(new_tree) is None
        or new_tree != receipt.new_tree_sha256
    ):
        raise RequirementSetImpactConflictError(
            "Impact receipt target tree hash no longer matches durable state."
        )


def _validate_node_bindings(
    connection: Any,
    receipt: RequirementSetImpactReceiptV1,
) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    seen_old: set[str] = set()
    seen_new: set[str] = set()
    for node in receipt.node_results:
        old_requirement = _load_requirement_binding(
            connection,
            requirement_id=node.old_requirement_id,
            expected_set_id=receipt.old_requirement_set_id,
            receipt=receipt,
            side="source",
        )
        new_requirement = _load_requirement_binding(
            connection,
            requirement_id=node.new_requirement_id,
            expected_set_id=receipt.new_requirement_set_id,
            receipt=receipt,
            side="target",
        )
        if node.old_requirement_id is not None:
            if node.old_requirement_id in seen_old:
                raise RequirementSetImpactPersistenceError(
                    "Impact receipt maps one source requirement more than once."
                )
            seen_old.add(node.old_requirement_id)
        if node.new_requirement_id is not None:
            if node.new_requirement_id in seen_new:
                raise RequirementSetImpactPersistenceError(
                    "Impact receipt maps one target requirement more than once."
                )
            seen_new.add(node.new_requirement_id)

        source_assessment = None
        if old_requirement is not None:
            source_assessment = _select_assessment_for_update(
                connection,
                str(node.old_requirement_id),
            )
        source_is_current = _assessment_has_current_result(source_assessment)
        expects_prior_result = node.outcome in {
            RequirementSetImpactOutcome.RETAINED,
            RequirementSetImpactOutcome.STALE,
        } or (
            node.outcome is RequirementSetImpactOutcome.REMOVED
            and bool(node.source_result_metadata)
        )
        if expects_prior_result and not source_is_current:
            raise RequirementSetImpactConflictError(
                f"Impact node {node.node_id} no longer has its claimed current source result."
            )
        if (
            node.outcome is RequirementSetImpactOutcome.NO_PRIOR_RESULT
            and source_is_current
        ):
            raise RequirementSetImpactConflictError(
                f"Impact node {node.node_id} claims no prior result, but one is current."
            )
        metadata_assessment_id = str(
            node.source_result_metadata.get("assessment_id") or ""
        ).strip()
        if expects_prior_result and not metadata_assessment_id:
            raise RequirementSetImpactConflictError(
                f"Impact node {node.node_id} does not bind its source assessment identity."
            )
        if metadata_assessment_id and (
            source_assessment is None
            or str(source_assessment["id"]) != metadata_assessment_id
        ):
            raise RequirementSetImpactConflictError(
                f"Impact node {node.node_id} source assessment identity changed."
            )
        if source_assessment is not None and node.source_result_metadata:
            _validate_source_result_metadata(
                node=node,
                source_assessment=source_assessment,
            )
        if new_requirement is not None:
            target_assessment = _select_assessment_for_update(
                connection,
                str(node.new_requirement_id),
            )
            if _is_protected_native_result(target_assessment):
                raise RequirementSetImpactConflictError(
                    f"Impact node {node.node_id} would overwrite a current native target result."
                )
        bindings[node.node_id] = {
            "source_requirement": old_requirement,
            "target_requirement": new_requirement,
            "source_assessment": source_assessment,
        }
    return bindings


def _select_assessment_for_update(
    connection: Any,
    requirement_id: str,
) -> Any | None:
    suffix = " FOR UPDATE" if db.is_postgresql_runtime() else ""
    return connection.execute(
        "SELECT * FROM assessments WHERE requirement_id = ?" + suffix,
        (requirement_id,),
    ).fetchone()


def _validate_source_result_metadata(
    *,
    node: RequirementSetImpactNodeV1,
    source_assessment: Any,
) -> None:
    metadata = node.source_result_metadata
    expected_text = {
        "assessment_updated_at": str(source_assessment["updated_at"] or ""),
        "result_origin": str(
            source_assessment["result_origin"] or "NATIVE"
        ).upper(),
        "source_assessment_id": str(
            source_assessment["source_assessment_id"] or ""
        ),
    }
    for field, expected in expected_text.items():
        if field in metadata and str(metadata[field] or "") != expected:
            raise RequirementSetImpactConflictError(
                f"Impact node {node.node_id} source assessment metadata changed."
            )
    if "has_manual_decision" in metadata:
        actual_manual = bool(
            str(source_assessment["final_decision"] or "").strip()
        )
        if metadata["has_manual_decision"] is not actual_manual:
            raise RequirementSetImpactConflictError(
                f"Impact node {node.node_id} source manual decision metadata changed."
            )


def _load_requirement_binding(
    connection: Any,
    *,
    requirement_id: str | None,
    expected_set_id: str,
    receipt: RequirementSetImpactReceiptV1,
    side: str,
) -> Any | None:
    if requirement_id is None:
        return None
    row = connection.execute(
        """
        SELECT * FROM requirements
        WHERE id = ?
          AND requirement_set_id = ?
          AND project_id = ?
          AND sor_document_id = ?
        """,
        (
            requirement_id,
            expected_set_id,
            receipt.project_id,
            receipt.document_id,
        ),
    ).fetchone()
    if row is None:
        raise RequirementSetImpactPersistenceError(
            f"Impact receipt {side} requirement binding does not exist."
        )
    return row


def _insert_receipt(
    connection: Any,
    receipt: RequirementSetImpactReceiptV1,
) -> bool:
    payload_json = _canonical_receipt_json(receipt)
    cursor = connection.execute(
        """
        INSERT INTO requirement_set_impact_receipts (
            receipt_id, schema_version, algorithm_version, project_id,
            sor_document_id, old_requirement_set_id, new_requirement_set_id,
            old_tree_sha256, new_tree_sha256, payload_sha256, payload_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            receipt.receipt_id,
            receipt.schema_version,
            receipt.algorithm_version,
            receipt.project_id,
            receipt.document_id,
            receipt.old_requirement_set_id,
            receipt.new_requirement_set_id,
            receipt.old_tree_sha256,
            receipt.new_tree_sha256,
            receipt.payload_sha256,
            payload_json,
            _now_iso(),
        ),
    )
    created = cursor.rowcount == 1
    stored_row = connection.execute(
        "SELECT * FROM requirement_set_impact_receipts WHERE receipt_id = ?",
        (receipt.receipt_id,),
    ).fetchone()
    if stored_row is None:
        collision = connection.execute(
            """
            SELECT receipt_id FROM requirement_set_impact_receipts
            WHERE new_requirement_set_id = ? AND payload_sha256 = ?
            """,
            (receipt.new_requirement_set_id, receipt.payload_sha256),
        ).fetchone()
        if collision is not None:
            raise RequirementSetImpactConflictError(
                "Canonical impact payload is bound to a different receipt identity."
            )
        raise RequirementSetImpactPersistenceError(
            "Requirement Set impact receipt could not be persisted."
        )
    stored = _deserialize_receipt(stored_row)
    if stored != receipt or str(stored_row["payload_json"]) != payload_json:
        raise RequirementSetImpactConflictError(
            "Impact receipt identity already contains a different canonical payload."
        )
    return created


def _get_receipt_in_connection(
    connection: Any,
    receipt_id: str,
) -> RequirementSetImpactReceiptV1 | None:
    row = connection.execute(
        "SELECT * FROM requirement_set_impact_receipts WHERE receipt_id = ?",
        (receipt_id,),
    ).fetchone()
    return _deserialize_receipt(row) if row is not None else None


def _deserialize_receipt(row: Any) -> RequirementSetImpactReceiptV1:
    payload_json = str(row["payload_json"] or "")
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RequirementSetImpactPersistenceError(
            "Stored Requirement Set impact receipt JSON is invalid."
        ) from exc
    if not isinstance(payload, dict):
        raise RequirementSetImpactPersistenceError(
            "Stored Requirement Set impact receipt payload is not an object."
        )
    try:
        receipt = RequirementSetImpactReceiptV1.model_validate(payload)
    except Exception as exc:
        raise RequirementSetImpactPersistenceError(
            "Stored Requirement Set impact receipt failed canonical validation."
        ) from exc
    if payload_json != _canonical_receipt_json(receipt):
        raise RequirementSetImpactPersistenceError(
            "Stored Requirement Set impact receipt JSON is not canonical."
        )
    expected_columns = {
        "receipt_id": receipt.receipt_id,
        "schema_version": receipt.schema_version,
        "algorithm_version": receipt.algorithm_version,
        "project_id": receipt.project_id,
        "sor_document_id": receipt.document_id,
        "old_requirement_set_id": receipt.old_requirement_set_id,
        "new_requirement_set_id": receipt.new_requirement_set_id,
        "old_tree_sha256": receipt.old_tree_sha256,
        "new_tree_sha256": receipt.new_tree_sha256,
        "payload_sha256": receipt.payload_sha256,
    }
    if any(str(row[column] or "") != value for column, value in expected_columns.items()):
        raise RequirementSetImpactPersistenceError(
            "Stored Requirement Set impact receipt columns disagree with its payload."
        )
    return receipt


def _copy_assessment(
    connection: Any,
    *,
    node: RequirementSetImpactNodeV1,
    source_assessment: Any,
    receipt: RequirementSetImpactReceiptV1,
    analysis_input_schema_version: str,
) -> None:
    target_requirement_id = str(node.new_requirement_id)
    existing = connection.execute(
        "SELECT * FROM assessments WHERE requirement_id = ?",
        (target_requirement_id,),
    ).fetchone()
    if _is_protected_native_result(existing):
        raise RequirementSetImpactConflictError(
            f"Impact node {node.node_id} would overwrite a current native target result."
        )
    assessment_id = str(existing["id"]) if existing is not None else str(uuid.uuid4())
    reference_status = (
        "CURRENT"
        if node.outcome is RequirementSetImpactOutcome.RETAINED
        else "STALE_REFERENCE"
    )
    final_decision = (
        source_assessment["final_decision"]
        if reference_status == "CURRENT"
        else None
    )
    engineer_reason = (
        source_assessment["engineer_reason"]
        if reference_status == "CURRENT"
        else None
    )
    values = [source_assessment[column] for column in _ASSESSMENT_RESULT_COLUMNS]
    values.extend(
        (
            final_decision,
            engineer_reason,
            reference_status,
            str(node.old_requirement_id),
            "CARRIED",
            str(source_assessment["id"]),
            receipt.receipt_id,
            analysis_input_schema_version,
            str(node.new_analysis_input_sha256 or ""),
            _now_iso(),
        )
    )
    mutable_columns = (
        *_ASSESSMENT_RESULT_COLUMNS,
        "final_decision",
        "engineer_reason",
        "reference_status",
        "source_requirement_id",
        "result_origin",
        "source_assessment_id",
        "impact_receipt_id",
        "analysis_input_schema_version",
        "analysis_input_sha256",
        "updated_at",
    )
    if existing is None:
        columns = ("id", "requirement_id", *mutable_columns)
        connection.execute(
            f"INSERT INTO assessments ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            (assessment_id, target_requirement_id, *values),
        )
    else:
        connection.execute(
            "UPDATE assessments SET "
            + ", ".join(f"{column} = ?" for column in mutable_columns)
            + " WHERE requirement_id = ?",
            (*values, target_requirement_id),
        )


def _copy_evidences(
    connection: Any,
    *,
    source_requirement_id: str,
    target_requirement_id: str,
) -> None:
    suffix = " FOR UPDATE" if db.is_postgresql_runtime() else ""
    source_rows = connection.execute(
        "SELECT * FROM evidences WHERE requirement_id = ? ORDER BY id ASC"
        + suffix,
        (source_requirement_id,),
    ).fetchall()
    connection.execute(
        "DELETE FROM evidences WHERE requirement_id = ?",
        (target_requirement_id,),
    )
    columns = ("id", "requirement_id", *_EVIDENCE_COLUMNS)
    for row in source_rows:
        connection.execute(
            f"INSERT INTO evidences ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            (
                str(uuid.uuid4()),
                target_requirement_id,
                *(row[column] for column in _EVIDENCE_COLUMNS),
            ),
        )


def _clear_non_current_target_result(
    connection: Any,
    *,
    target_requirement_id: str,
) -> int:
    existing = connection.execute(
        "SELECT * FROM assessments WHERE requirement_id = ?",
        (target_requirement_id,),
    ).fetchone()
    if existing is None:
        return 0
    if _is_protected_native_result(existing):
        raise RequirementSetImpactConflictError(
            "Impact receipt would clear a current native target result."
        )
    connection.execute(
        "DELETE FROM evidences WHERE requirement_id = ?",
        (target_requirement_id,),
    )
    connection.execute(
        "DELETE FROM assessments WHERE requirement_id = ?",
        (target_requirement_id,),
    )
    return 1


def _is_protected_native_result(assessment: Any | None) -> bool:
    if assessment is None:
        return False
    return (
        _assessment_has_current_result(assessment)
        and str(assessment["result_origin"] or "NATIVE").upper() != "CARRIED"
    )


def _assessment_has_current_result(assessment: Any | None) -> bool:
    if assessment is None:
        return False
    if str(assessment["reference_status"] or "CURRENT").upper() != "CURRENT":
        return False
    return any(
        str(assessment[field] or "").strip()
        for field in (
            "ai_decision",
            "compliance_status",
            "ai_reason",
            "final_decision",
        )
    )


def _receipt_is_fully_applied(
    connection: Any,
    receipt: RequirementSetImpactReceiptV1,
    *,
    bindings: dict[str, dict[str, Any]],
    analysis_input_schema_version: str,
) -> bool:
    for node in receipt.node_results:
        if node.outcome in {
            RequirementSetImpactOutcome.RETAINED,
            RequirementSetImpactOutcome.STALE,
        }:
            target = connection.execute(
                "SELECT * FROM assessments WHERE requirement_id = ?",
                (node.new_requirement_id,),
            ).fetchone()
            source = bindings[node.node_id]["source_assessment"]
            if target is None or source is None:
                return False
            expected_status = (
                "CURRENT"
                if node.outcome is RequirementSetImpactOutcome.RETAINED
                else "STALE_REFERENCE"
            )
            expected_final = source["final_decision"] if expected_status == "CURRENT" else None
            expected_reason = source["engineer_reason"] if expected_status == "CURRENT" else None
            if (
                any(target[column] != source[column] for column in _ASSESSMENT_RESULT_COLUMNS)
                or target["final_decision"] != expected_final
                or target["engineer_reason"] != expected_reason
                or str(target["reference_status"] or "").upper() != expected_status
                or str(target["source_requirement_id"] or "") != str(node.old_requirement_id)
                or str(target["result_origin"] or "").upper() != "CARRIED"
                or str(target["source_assessment_id"] or "") != str(source["id"])
                or str(target["impact_receipt_id"] or "") != receipt.receipt_id
                or str(target["analysis_input_schema_version"] or "")
                != analysis_input_schema_version
                or str(target["analysis_input_sha256"] or "")
                != str(node.new_analysis_input_sha256 or "")
                or _evidence_signature(connection, str(node.old_requirement_id))
                != _evidence_signature(connection, str(node.new_requirement_id))
            ):
                return False
        elif node.outcome in {
            RequirementSetImpactOutcome.NEW,
            RequirementSetImpactOutcome.NO_PRIOR_RESULT,
        }:
            target = connection.execute(
                "SELECT * FROM assessments WHERE requirement_id = ?",
                (node.new_requirement_id,),
            ).fetchone()
            if target is not None:
                return False
    return True


def _evidence_signature(connection: Any, requirement_id: str) -> tuple[tuple[Any, ...], ...]:
    rows = connection.execute(
        "SELECT * FROM evidences WHERE requirement_id = ?",
        (requirement_id,),
    ).fetchall()
    values = [tuple(row[column] for column in _EVIDENCE_COLUMNS) for row in rows]
    return tuple(sorted(values, key=lambda value: tuple(str(item) for item in value)))


def _canonical_receipt_json(receipt: RequirementSetImpactReceiptV1) -> str:
    return json.dumps(
        receipt.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "RequirementSetImpactApplyResult",
    "RequirementSetImpactConflictError",
    "RequirementSetImpactPersistenceError",
    "RequirementSetImpactStateChangedError",
    "apply_requirement_set_impact_receipt",
    "finalize_staged_requirement_set_activation",
    "get_requirement_set_impact_receipt",
    "list_requirement_set_impact_receipts",
    "save_requirement_set_impact_receipt",
]
