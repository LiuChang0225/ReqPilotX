"""Shared SQL persistence for requirement enrichment artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, TypeVar

from pydantic import BaseModel

from app.domain.extraction.requirement_enrichment import (
    REQUIREMENT_ATOMIZATION_RUN_SCHEMA_VERSION,
    RequirementAtomizationRun,
    RequirementPublicationEntryKind,
    RequirementPublicationPreview,
    RequirementPublicationResult,
    RequirementRefinementSuggestion,
)
from app.domain.atomization_scope import (
    ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER_SECONDS,
    AtomizationScopeAttemptStatus,
    AtomizationScopeAttemptV1,
    AtomizationScopeReceiptV1,
    canonical_atomization_scope_attempt_sha256,
)
from app.domain.requirement_enrichment_publication_v2 import (
    RequirementEnrichmentAtomizationStateV2,
    RequirementEnrichmentPublicationBindingPreviewV2,
    RequirementEnrichmentPublicationBindingV2,
    RequirementEnrichmentPublicationProjectionV2,
    RequirementEnrichmentPublicationRowBindingV2,
    RequirementEnrichmentPublicationStartV2,
    build_requirement_enrichment_publication_row_bindings_v2,
    canonical_publication_decision_trace_v2_json,
    stable_publication_binding_v2_id,
)
from app.ports.requirement_enrichment_repository import (
    AtomizationScopeStateSnapshot,
    RequirementEnrichmentConflictError,
    RequirementEnrichmentRepositoryError,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
)


_ATOMIZATION_CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_atomization_checkpoints (
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    review_node_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(lineage_sha256, review_node_id),
    FOREIGN KEY(document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
)
"""

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER = timedelta(
    seconds=ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER_SECONDS
)

_ATOMIZATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_atomization_runs (
    atomization_run_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    atomization_revision INTEGER NOT NULL DEFAULT 0 CHECK(atomization_revision >= 0),
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(
        project_id, document_id, session_id, review_revision,
        atomization_revision, schema_version
    ),
    FOREIGN KEY(document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
)
"""

_ATOMIZATION_SCOPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_atomization_scope_receipts (
    receipt_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    scope_revision INTEGER NOT NULL CHECK(scope_revision >= 1),
    command_id TEXT NOT NULL,
    command_sha256 TEXT NOT NULL CHECK(length(command_sha256) = 64),
    atomization_run_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE(lineage_sha256, scope_revision),
    UNIQUE(lineage_sha256, command_id),
    FOREIGN KEY(atomization_run_id)
        REFERENCES requirement_atomization_runs(atomization_run_id)
        ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
)
"""

_ATOMIZATION_SCOPE_ATTEMPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_atomization_scope_attempts (
    attempt_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    attempt_sequence INTEGER NOT NULL CHECK(attempt_sequence >= 1),
    command_id TEXT NOT NULL,
    command_sha256 TEXT NOT NULL CHECK(length(command_sha256) = 64),
    expected_scope_revision INTEGER NOT NULL CHECK(expected_scope_revision >= 0),
    status TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(lineage_sha256, attempt_sequence),
    FOREIGN KEY(document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
)
"""

_REFINEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_refinement_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    atomization_run_id TEXT NOT NULL,
    atomic_node_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    status TEXT NOT NULL,
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(atomization_run_id, atomic_node_id),
    FOREIGN KEY(atomization_run_id)
        REFERENCES requirement_atomization_runs(atomization_run_id)
        ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
)
"""

_REFINEMENT_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_refinement_events (
    suggestion_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    status TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(suggestion_id, revision),
    FOREIGN KEY(suggestion_id)
        REFERENCES requirement_refinement_suggestions(suggestion_id)
        ON DELETE CASCADE
)
"""

_PUBLICATION_PREVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_enrichment_publication_previews (
    preview_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    atomization_run_id TEXT NOT NULL,
    publication_key TEXT NOT NULL UNIQUE CHECK(length(publication_key) = 64),
    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(atomization_run_id)
        REFERENCES requirement_atomization_runs(atomization_run_id)
        ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
)
"""

_PUBLICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_enrichment_publications (
    publication_key TEXT PRIMARY KEY CHECK(length(publication_key) = 64),
    preview_id TEXT NOT NULL UNIQUE,
    requirement_set_id TEXT NOT NULL UNIQUE,
    result_sha256 TEXT NOT NULL CHECK(length(result_sha256) = 64),
    result_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(preview_id)
        REFERENCES requirement_enrichment_publication_previews(preview_id)
        ON DELETE CASCADE,
    FOREIGN KEY(requirement_set_id)
        REFERENCES requirement_sets(id)
        ON DELETE CASCADE
)
"""

_PUBLICATION_BINDING_PREVIEW_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_enrichment_publication_binding_previews (
    binding_preview_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
    base_active_requirement_set_id TEXT NOT NULL DEFAULT '',
    accepted_classification_snapshot_id TEXT NOT NULL,
    accepted_classification_sha256 TEXT NOT NULL
        CHECK(length(accepted_classification_sha256) = 64),
    atomization_state TEXT NOT NULL CHECK(atomization_state IN ('NOT_RUN', 'APPLIED')),
    projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
)
"""

_PUBLICATION_BINDING_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_enrichment_publication_bindings (
    binding_id TEXT PRIMARY KEY,
    binding_preview_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    requirement_set_id TEXT NOT NULL UNIQUE,
    projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256) = 64),
    publication_state TEXT NOT NULL CHECK(publication_state IN ('PUBLISHING', 'READY')),
    payload_sha256 TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    CHECK(
        (publication_state = 'PUBLISHING' AND payload_sha256 = '' AND payload_json = '')
        OR
        (publication_state = 'READY' AND length(payload_sha256) = 64 AND payload_json <> '')
    ),
    FOREIGN KEY(binding_preview_id)
        REFERENCES requirement_enrichment_publication_binding_previews(binding_preview_id)
        ON DELETE CASCADE,
    FOREIGN KEY(requirement_set_id) REFERENCES requirement_sets(id) ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
)
"""

_PUBLICATION_ROW_BINDING_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_enrichment_publication_row_bindings (
    binding_id TEXT NOT NULL,
    publication_order INTEGER NOT NULL CHECK(publication_order >= 0),
    requirement_tree_node_id TEXT NOT NULL,
    requirement_entry_id TEXT NOT NULL UNIQUE,
    projection_row_sha256 TEXT NOT NULL CHECK(length(projection_row_sha256) = 64),
    decision_trace_sha256 TEXT NOT NULL CHECK(length(decision_trace_sha256) = 64),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    PRIMARY KEY(binding_id, publication_order),
    UNIQUE(binding_id, requirement_tree_node_id),
    FOREIGN KEY(binding_id)
        REFERENCES requirement_enrichment_publication_bindings(binding_id)
        ON DELETE CASCADE,
    FOREIGN KEY(requirement_entry_id) REFERENCES requirements(id) ON DELETE CASCADE
)
"""

_RUN_INDEX = """
CREATE INDEX IF NOT EXISTS idx_requirement_atomization_session_v4
ON requirement_atomization_runs(
    project_id, document_id, session_id, schema_version,
    review_revision DESC, atomization_revision DESC
)
"""

_SCOPE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_requirement_atomization_scope_head
ON requirement_atomization_scope_receipts(
    project_id, document_id, session_id, lineage_sha256, scope_revision DESC
)
"""

_SCOPE_ATTEMPT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_requirement_atomization_scope_attempt_head
ON requirement_atomization_scope_attempts(
    project_id, document_id, session_id, lineage_sha256, attempt_sequence DESC
)
"""

_SCOPE_ATTEMPT_COMMAND_INDEX = """
CREATE INDEX IF NOT EXISTS idx_requirement_atomization_scope_attempt_command
ON requirement_atomization_scope_attempts(
    lineage_sha256, command_id, attempt_sequence
)
"""

_SCOPE_ATTEMPT_RUNNING_INDEX = """
CREATE INDEX IF NOT EXISTS idx_requirement_atomization_scope_attempt_running
ON requirement_atomization_scope_attempts(
    lineage_sha256, expected_scope_revision, status, attempt_sequence
)
"""

_REFINEMENT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_requirement_refinement_run
ON requirement_refinement_suggestions(atomization_run_id, atomic_node_id)
"""

_PREVIEW_INDEX = """
CREATE INDEX IF NOT EXISTS idx_requirement_publication_preview_run
ON requirement_enrichment_publication_previews(atomization_run_id, created_at_utc DESC)
"""

_PUBLICATION_BINDING_PREVIEW_V2_INDEX = """
CREATE INDEX IF NOT EXISTS idx_requirement_enrichment_binding_preview_document
ON requirement_enrichment_publication_binding_previews(
    project_id, document_id, session_id, review_revision,
    projection_sha256, created_at_utc DESC
)
"""

_ModelT = TypeVar("_ModelT", bound=BaseModel)

_SCOPE_RECEIPT_READ_FIELDS = (
    "receipt_id",
    "schema_version",
    "project_id",
    "document_id",
    "session_id",
    "review_revision",
    "lineage_sha256",
    "scope_revision",
    "command_id",
    "command_sha256",
    "atomization_run_id",
    "payload_sha256",
    "payload_json",
)
_SCOPE_ATTEMPT_READ_FIELDS = (
    "attempt_id",
    "schema_version",
    "project_id",
    "document_id",
    "session_id",
    "review_revision",
    "lineage_sha256",
    "attempt_sequence",
    "command_id",
    "command_sha256",
    "expected_scope_revision",
    "status",
    "payload_sha256",
    "payload_json",
    "started_at_utc",
    "updated_at_utc",
)
_SCOPE_CHECKPOINT_READ_FIELDS = (
    "lineage_sha256",
    "review_node_id",
    "project_id",
    "document_id",
    "session_id",
    "review_revision",
    "request_sha256",
    "payload_sha256",
    "payload_json",
)


class SqlRequirementEnrichmentRepository:
    """Persist enrichment artifacts and publish one isolated Requirement Set."""

    def get_atomization_checkpoint(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult | None:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        _validate_atomization_checkpoint_binding(
            review_revision=review_revision,
            lineage_sha256=lineage_sha256,
            request=request,
        )
        _, request_sha256 = _serialize_model(request)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_atomization_checkpoints
                WHERE lineage_sha256 = ? AND review_node_id = ?
                """,
                (lineage_sha256, request.review_node_id),
            ).fetchone()
        if row is None:
            return None
        _validate_atomization_checkpoint_row(
            row,
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
            review_revision=review_revision,
            request_sha256=request_sha256,
        )
        return _deserialize_model(
            str(row["payload_json"]),
            str(row["payload_sha256"]),
            RequirementAtomizationProviderResult,
        )

    def create_or_get_atomization_checkpoint(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
        request: RequirementAtomizationProviderRequest,
        result: RequirementAtomizationProviderResult,
        scope_attempt_id: str | None = None,
    ) -> RequirementAtomizationProviderResult:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        _validate_atomization_checkpoint_binding(
            review_revision=review_revision,
            lineage_sha256=lineage_sha256,
            request=request,
        )
        if not isinstance(result, RequirementAtomizationProviderResult):
            raise TypeError("atomization checkpoint result is invalid")
        _, request_sha256 = _serialize_model(request)
        payload_json, payload_sha256 = _serialize_model(result)
        created_at_utc = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            with self._transaction(connection):
                if scope_attempt_id is not None:
                    _lock_atomization_scope_lineage_in_conn(
                        connection,
                        lineage_sha256=lineage_sha256,
                    )
                    owner = _assert_current_scope_attempt_owner_in_connection(
                        connection,
                        attempt_id=scope_attempt_id,
                        lineage_sha256=lineage_sha256,
                    )
                    _assert_scope_attempt_checkpoint_binding(
                        owner,
                        project_id=project_id,
                        document_id=document_id,
                        session_id=session_id,
                        review_revision=review_revision,
                    )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO requirement_atomization_checkpoints (
                        lineage_sha256, review_node_id, project_id, document_id,
                        session_id, review_revision, request_sha256,
                        payload_sha256, payload_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lineage_sha256,
                        request.review_node_id,
                        project_id,
                        document_id,
                        session_id,
                        review_revision,
                        request_sha256,
                        payload_sha256,
                        payload_json,
                        created_at_utc,
                    ),
                )
                if cursor.rowcount == 1:
                    return result
                row = connection.execute(
                    """
                    SELECT * FROM requirement_atomization_checkpoints
                    WHERE lineage_sha256 = ? AND review_node_id = ?
                    """,
                    (lineage_sha256, request.review_node_id),
                ).fetchone()
        if row is None:
            raise RequirementEnrichmentRepositoryError(
                "atomization checkpoint insert did not produce a readable row"
            )
        _validate_atomization_checkpoint_row(
            row,
            project_id=project_id,
            document_id=document_id,
            session_id=session_id,
            review_revision=review_revision,
            request_sha256=request_sha256,
        )
        stored = _deserialize_model(
            str(row["payload_json"]),
            str(row["payload_sha256"]),
            RequirementAtomizationProviderResult,
        )
        if stored != result:
            raise RequirementEnrichmentConflictError(
                "atomization checkpoint already has different provider output"
            )
        return stored

    def count_atomization_checkpoints(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
    ) -> int:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        if isinstance(review_revision, bool) or review_revision < 0:
            raise ValueError("review_revision must be a non-negative integer")
        if not _SHA256_PATTERN.fullmatch(lineage_sha256):
            raise ValueError("lineage_sha256 is invalid")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS checkpoint_count
                FROM requirement_atomization_checkpoints
                WHERE project_id = ? AND document_id = ? AND session_id = ?
                  AND review_revision = ? AND lineage_sha256 = ?
                """,
                (
                    project_id,
                    document_id,
                    session_id,
                    review_revision,
                    lineage_sha256,
                ),
            ).fetchone()
        return int(row["checkpoint_count"] if row is not None else 0)

    def delete_atomization_checkpoint(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
        request: RequirementAtomizationProviderRequest,
        scope_attempt_id: str | None = None,
    ) -> None:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        _validate_atomization_checkpoint_binding(
            review_revision=review_revision,
            lineage_sha256=lineage_sha256,
            request=request,
        )
        _, request_sha256 = _serialize_model(request)
        with self._connect() as connection:
            with self._transaction(connection):
                if scope_attempt_id is not None:
                    _lock_atomization_scope_lineage_in_conn(
                        connection,
                        lineage_sha256=lineage_sha256,
                    )
                    owner = _assert_current_scope_attempt_owner_in_connection(
                        connection,
                        attempt_id=scope_attempt_id,
                        lineage_sha256=lineage_sha256,
                    )
                    _assert_scope_attempt_checkpoint_binding(
                        owner,
                        project_id=project_id,
                        document_id=document_id,
                        session_id=session_id,
                        review_revision=review_revision,
                    )
                cursor = connection.execute(
                    """
                    DELETE FROM requirement_atomization_checkpoints
                    WHERE lineage_sha256 = ? AND review_node_id = ?
                      AND project_id = ? AND document_id = ? AND session_id = ?
                      AND review_revision = ? AND request_sha256 = ?
                    """,
                    (
                        lineage_sha256,
                        request.review_node_id,
                        project_id,
                        document_id,
                        session_id,
                        review_revision,
                        request_sha256,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RequirementEnrichmentConflictError(
                        "atomization checkpoint changed before invalidation"
                    )

    def create_or_get_atomization(
        self,
        run: RequirementAtomizationRun,
    ) -> RequirementAtomizationRun:
        payload_json, payload_sha256 = _serialize_model(run)
        with self._connect() as connection:
            with self._transaction(connection):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO requirement_atomization_runs (
                        atomization_run_id, schema_version, project_id, document_id,
                        session_id, review_revision, atomization_revision,
                        lineage_sha256, payload_sha256, payload_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.atomization_run_id,
                        run.schema_version,
                        run.lineage.project_id,
                        run.lineage.document_id,
                        run.lineage.session_id,
                        run.lineage.review_revision,
                        run.revision,
                        run.lineage.lineage_sha256,
                        payload_sha256,
                        payload_json,
                        run.created_at_utc.isoformat(),
                    ),
                )
                if cursor.rowcount == 1:
                    return run
                row = connection.execute(
                    """
                    SELECT * FROM requirement_atomization_runs
                    WHERE project_id = ? AND document_id = ? AND session_id = ?
                      AND review_revision = ? AND atomization_revision = ?
                      AND schema_version = ?
                    """,
                    (
                        run.lineage.project_id,
                        run.lineage.document_id,
                        run.lineage.session_id,
                        run.lineage.review_revision,
                        run.revision,
                        REQUIREMENT_ATOMIZATION_RUN_SCHEMA_VERSION,
                    ),
                ).fetchone()
        if row is None:
            raise RequirementEnrichmentRepositoryError(
                "atomization insert did not produce a readable row"
            )
        stored = _deserialize_model(
            str(row["payload_json"]),
            str(row["payload_sha256"]),
            RequirementAtomizationRun,
        )
        if stored != run:
            raise RequirementEnrichmentConflictError(
                "review revision already has different atomization output"
            )
        return stored

    def get_atomization(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        atomization_run_id: str,
    ) -> RequirementAtomizationRun | None:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_atomization_runs
                WHERE atomization_run_id = ? AND project_id = ?
                  AND document_id = ? AND session_id = ? AND schema_version = ?
                """,
                (
                    atomization_run_id,
                    project_id,
                    document_id,
                    session_id,
                    REQUIREMENT_ATOMIZATION_RUN_SCHEMA_VERSION,
                ),
            ).fetchone()
        return _atomization_from_row(row) if row is not None else None

    def get_latest_atomization(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        atomization_revision: int | None = None,
    ) -> RequirementAtomizationRun | None:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        if atomization_revision is not None and (
            isinstance(atomization_revision, bool) or atomization_revision < 0
        ):
            raise ValueError("atomization_revision must be a non-negative integer")
        revision_clause = (
            " AND atomization_revision = ?" if atomization_revision is not None else ""
        )
        parameters: tuple[Any, ...] = (
            project_id,
            document_id,
            session_id,
            REQUIREMENT_ATOMIZATION_RUN_SCHEMA_VERSION,
        )
        if atomization_revision is not None:
            parameters = (*parameters, atomization_revision)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM requirement_atomization_runs
                WHERE project_id = ? AND document_id = ? AND session_id = ?
                  AND schema_version = ?{revision_clause}
                ORDER BY review_revision DESC, atomization_revision DESC,
                         created_at_utc DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return _atomization_from_row(row) if row is not None else None

    def get_latest_atomization_scope_receipt(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        lineage_sha256: str,
    ) -> AtomizationScopeReceiptV1 | None:
        project_id, document_id, session_id = _validated_binding(
            project_id, document_id, session_id
        )
        if not _SHA256_PATTERN.fullmatch(lineage_sha256):
            raise ValueError("lineage_sha256 is invalid")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_atomization_scope_receipts
                WHERE project_id = ? AND document_id = ? AND session_id = ?
                  AND lineage_sha256 = ?
                ORDER BY scope_revision DESC
                LIMIT 1
                """,
                (project_id, document_id, session_id, lineage_sha256),
            ).fetchone()
        return _atomization_scope_receipt_from_row(row) if row is not None else None

    def get_latest_atomization_scope_attempt(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        lineage_sha256: str,
    ) -> AtomizationScopeAttemptV1 | None:
        project_id, document_id, session_id = _validated_binding(
            project_id, document_id, session_id
        )
        if not _SHA256_PATTERN.fullmatch(lineage_sha256):
            raise ValueError("lineage_sha256 is invalid")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_atomization_scope_attempts
                WHERE project_id = ? AND document_id = ? AND session_id = ?
                  AND lineage_sha256 = ?
                ORDER BY attempt_sequence DESC
                LIMIT 1
                """,
                (project_id, document_id, session_id, lineage_sha256),
            ).fetchone()
        return _atomization_scope_attempt_from_row(row) if row is not None else None

    def get_atomization_scope_state_snapshot(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        review_revision: int,
        lineage_sha256: str,
    ) -> AtomizationScopeStateSnapshot:
        project_id, document_id, session_id = _validated_binding(
            project_id, document_id, session_id
        )
        if isinstance(review_revision, bool) or review_revision < 0:
            raise ValueError("review_revision must be non-negative")
        if not _SHA256_PATTERN.fullmatch(lineage_sha256):
            raise ValueError("lineage_sha256 is invalid")
        parameters = (project_id, document_id, session_id, lineage_sha256)
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH latest_receipt AS (
                    SELECT * FROM requirement_atomization_scope_receipts
                    WHERE project_id = ? AND document_id = ? AND session_id = ?
                      AND lineage_sha256 = ?
                    ORDER BY scope_revision DESC
                    LIMIT 1
                ),
                latest_attempt AS (
                    SELECT * FROM requirement_atomization_scope_attempts
                    WHERE project_id = ? AND document_id = ? AND session_id = ?
                      AND lineage_sha256 = ?
                    ORDER BY attempt_sequence DESC
                    LIMIT 1
                ),
                matching_checkpoints AS (
                    SELECT *
                    FROM requirement_atomization_checkpoints
                    WHERE project_id = ? AND document_id = ? AND session_id = ?
                      AND review_revision = ? AND lineage_sha256 = ?
                )
                SELECT
                    receipt.receipt_id AS receipt_receipt_id,
                    receipt.schema_version AS receipt_schema_version,
                    receipt.project_id AS receipt_project_id,
                    receipt.document_id AS receipt_document_id,
                    receipt.session_id AS receipt_session_id,
                    receipt.review_revision AS receipt_review_revision,
                    receipt.lineage_sha256 AS receipt_lineage_sha256,
                    receipt.scope_revision AS receipt_scope_revision,
                    receipt.command_id AS receipt_command_id,
                    receipt.command_sha256 AS receipt_command_sha256,
                    receipt.atomization_run_id AS receipt_atomization_run_id,
                    receipt.payload_sha256 AS receipt_payload_sha256,
                    receipt.payload_json AS receipt_payload_json,
                    attempt.attempt_id AS attempt_attempt_id,
                    attempt.schema_version AS attempt_schema_version,
                    attempt.project_id AS attempt_project_id,
                    attempt.document_id AS attempt_document_id,
                    attempt.session_id AS attempt_session_id,
                    attempt.review_revision AS attempt_review_revision,
                    attempt.lineage_sha256 AS attempt_lineage_sha256,
                    attempt.attempt_sequence AS attempt_attempt_sequence,
                    attempt.command_id AS attempt_command_id,
                    attempt.command_sha256 AS attempt_command_sha256,
                    attempt.expected_scope_revision AS attempt_expected_scope_revision,
                    attempt.status AS attempt_status,
                    attempt.payload_sha256 AS attempt_payload_sha256,
                    attempt.payload_json AS attempt_payload_json,
                    attempt.started_at_utc AS attempt_started_at_utc,
                    attempt.updated_at_utc AS attempt_updated_at_utc,
                    checkpoint.lineage_sha256 AS checkpoint_lineage_sha256,
                    checkpoint.review_node_id AS checkpoint_review_node_id,
                    checkpoint.project_id AS checkpoint_project_id,
                    checkpoint.document_id AS checkpoint_document_id,
                    checkpoint.session_id AS checkpoint_session_id,
                    checkpoint.review_revision AS checkpoint_review_revision,
                    checkpoint.request_sha256 AS checkpoint_request_sha256,
                    checkpoint.payload_sha256 AS checkpoint_payload_sha256,
                    checkpoint.payload_json AS checkpoint_payload_json
                FROM (SELECT 1 AS singleton) AS anchor
                LEFT JOIN latest_receipt AS receipt ON 1 = 1
                LEFT JOIN latest_attempt AS attempt ON 1 = 1
                LEFT JOIN matching_checkpoints AS checkpoint ON 1 = 1
                ORDER BY checkpoint.review_node_id
                """,
                (
                    *parameters,
                    *parameters,
                    project_id,
                    document_id,
                    session_id,
                    review_revision,
                    lineage_sha256,
                ),
            ).fetchall()
        if not rows:
            raise RequirementEnrichmentRepositoryError(
                "atomization scope state snapshot is unavailable"
            )
        row = rows[0]
        receipt = (
            _atomization_scope_receipt_from_row(
                {
                    field: row[f"receipt_{field}"]
                    for field in _SCOPE_RECEIPT_READ_FIELDS
                }
            )
            if row["receipt_receipt_id"] is not None
            else None
        )
        attempt = (
            _atomization_scope_attempt_from_row(
                {
                    field: row[f"attempt_{field}"]
                    for field in _SCOPE_ATTEMPT_READ_FIELDS
                }
            )
            if row["attempt_attempt_id"] is not None
            else None
        )
        valid_checkpoint_review_node_ids: list[str] = []
        for checkpoint_snapshot_row in rows:
            review_node_id = checkpoint_snapshot_row[
                "checkpoint_review_node_id"
            ]
            if review_node_id is None:
                continue
            checkpoint_row = {
                field: checkpoint_snapshot_row[f"checkpoint_{field}"]
                for field in _SCOPE_CHECKPOINT_READ_FIELDS
            }
            try:
                if (
                    not str(review_node_id).strip()
                    or len(str(review_node_id)) > 200
                    or str(checkpoint_row["lineage_sha256"]) != lineage_sha256
                    or not _SHA256_PATTERN.fullmatch(
                        str(checkpoint_row["request_sha256"])
                    )
                ):
                    continue
                _validate_atomization_checkpoint_row(
                    checkpoint_row,
                    project_id=project_id,
                    document_id=document_id,
                    session_id=session_id,
                    review_revision=review_revision,
                    request_sha256=str(checkpoint_row["request_sha256"]),
                )
                _deserialize_model(
                    str(checkpoint_row["payload_json"]),
                    str(checkpoint_row["payload_sha256"]),
                    RequirementAtomizationProviderResult,
                )
            except (RequirementEnrichmentRepositoryError, TypeError, ValueError):
                continue
            valid_checkpoint_review_node_ids.append(str(review_node_id))
        valid_checkpoint_ids = tuple(valid_checkpoint_review_node_ids)
        return AtomizationScopeStateSnapshot(
            receipt=receipt,
            attempt=attempt,
            checkpoint_count=len(valid_checkpoint_ids),
            valid_checkpoint_review_node_ids=valid_checkpoint_ids,
        )

    def begin_atomization_scope_attempt(
        self,
        *,
        attempt: AtomizationScopeAttemptV1,
        expected_scope_revision: int,
    ) -> AtomizationScopeAttemptV1:
        if attempt.status is not AtomizationScopeAttemptStatus.RUNNING:
            raise ValueError("a new scope attempt must be RUNNING")
        if isinstance(expected_scope_revision, bool) or expected_scope_revision < 0:
            raise ValueError("expected_scope_revision must be non-negative")
        if attempt.expected_scope_revision != expected_scope_revision:
            raise ValueError("scope attempt expected revision does not match")
        payload_json, payload_sha256 = _serialize_model(attempt)
        with self._connect() as connection:
            with self._transaction(connection):
                _lock_atomization_scope_lineage_in_conn(
                    connection,
                    lineage_sha256=attempt.lineage_sha256,
                )
                head = connection.execute(
                    """
                    SELECT scope_revision
                    FROM requirement_atomization_scope_receipts
                    WHERE lineage_sha256 = ?
                    ORDER BY scope_revision DESC
                    LIMIT 1
                    """,
                    (attempt.lineage_sha256,),
                ).fetchone()
                current_scope_revision = (
                    int(head["scope_revision"]) if head is not None else 0
                )
                if current_scope_revision != expected_scope_revision:
                    raise RequirementEnrichmentConflictError(
                        "atomization scope revision has changed"
                    )
                active_rows = connection.execute(
                    """
                    SELECT *
                    FROM requirement_atomization_scope_attempts
                    WHERE lineage_sha256 = ?
                      AND expected_scope_revision = ?
                      AND status = ?
                    ORDER BY attempt_sequence
                    """,
                    (
                        attempt.lineage_sha256,
                        expected_scope_revision,
                        AtomizationScopeAttemptStatus.RUNNING.value,
                    ),
                ).fetchall()
                active_attempts = tuple(
                    _atomization_scope_attempt_from_row(row) for row in active_rows
                )
                observed_at = datetime.now(timezone.utc)
                if any(
                    observed_at - active.updated_at_utc
                    < _ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER
                    for active in active_attempts
                ):
                    raise RequirementEnrichmentConflictError(
                        "atomization scope attempt is already running"
                    )
                for stale_attempt in active_attempts:
                    _update_atomization_scope_attempt_in_connection(
                        connection,
                        attempt=_stale_interrupted_atomization_scope_attempt(
                            connection,
                            attempt=stale_attempt,
                            observed_at_utc=observed_at,
                        ),
                        expected_status=AtomizationScopeAttemptStatus.RUNNING,
                    )
                command_bindings = connection.execute(
                    """
                    SELECT DISTINCT command_sha256
                    FROM requirement_atomization_scope_attempts
                    WHERE lineage_sha256 = ? AND command_id = ?
                    LIMIT 2
                    """,
                    (attempt.lineage_sha256, attempt.command_id),
                ).fetchall()
                if any(
                    str(row["command_sha256"]) != attempt.command_sha256
                    for row in command_bindings
                ):
                    raise RequirementEnrichmentConflictError(
                        "scope command_id was already used with different input"
                    )
                latest = connection.execute(
                    """
                    SELECT attempt_sequence
                    FROM requirement_atomization_scope_attempts
                    WHERE lineage_sha256 = ?
                    ORDER BY attempt_sequence DESC
                    LIMIT 1
                    """,
                    (attempt.lineage_sha256,),
                ).fetchone()
                expected_sequence = (
                    int(latest["attempt_sequence"]) + 1 if latest is not None else 1
                )
                if attempt.attempt_sequence != expected_sequence:
                    raise RequirementEnrichmentConflictError(
                        "atomization scope attempt sequence has changed"
                    )
                connection.execute(
                    """
                    INSERT INTO requirement_atomization_scope_attempts (
                        attempt_id, schema_version, project_id, document_id,
                        session_id, review_revision, lineage_sha256,
                        attempt_sequence, command_id, command_sha256,
                        expected_scope_revision, status, payload_sha256,
                        payload_json, started_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.attempt_id,
                        attempt.schema_version,
                        attempt.project_id,
                        attempt.document_id,
                        attempt.session_id,
                        attempt.review_revision,
                        attempt.lineage_sha256,
                        attempt.attempt_sequence,
                        attempt.command_id,
                        attempt.command_sha256,
                        attempt.expected_scope_revision,
                        attempt.status.value,
                        payload_sha256,
                        payload_json,
                        attempt.started_at_utc.isoformat(),
                        attempt.updated_at_utc.isoformat(),
                    ),
                )
        return attempt

    def update_atomization_scope_attempt(
        self,
        *,
        attempt: AtomizationScopeAttemptV1,
        expected_status: AtomizationScopeAttemptStatus,
    ) -> AtomizationScopeAttemptV1:
        with self._connect() as connection:
            with self._transaction(connection):
                if expected_status is AtomizationScopeAttemptStatus.RUNNING:
                    _lock_atomization_scope_lineage_in_conn(
                        connection,
                        lineage_sha256=attempt.lineage_sha256,
                    )
                    _assert_current_scope_attempt_owner_in_connection(
                        connection,
                        attempt_id=attempt.attempt_id,
                        lineage_sha256=attempt.lineage_sha256,
                        expected_scope_revision=attempt.expected_scope_revision,
                    )
                _update_atomization_scope_attempt_in_connection(
                    connection,
                    attempt=attempt,
                    expected_status=expected_status,
                )
        return attempt

    def heartbeat_atomization_scope_attempt(
        self,
        *,
        attempt_id: str,
        lineage_sha256: str,
        expected_scope_revision: int,
    ) -> AtomizationScopeAttemptV1:
        if isinstance(expected_scope_revision, bool) or expected_scope_revision < 0:
            raise ValueError("expected_scope_revision must be non-negative")
        with self._connect() as connection:
            with self._transaction(connection):
                _lock_atomization_scope_lineage_in_conn(
                    connection,
                    lineage_sha256=lineage_sha256,
                )
                current = _assert_current_scope_attempt_owner_in_connection(
                    connection,
                    attempt_id=attempt_id,
                    lineage_sha256=lineage_sha256,
                    expected_scope_revision=expected_scope_revision,
                )
                refreshed = _heartbeat_atomization_scope_attempt(
                    current,
                    observed_at_utc=datetime.now(timezone.utc),
                )
                _update_atomization_scope_attempt_in_connection(
                    connection,
                    attempt=refreshed,
                    expected_status=AtomizationScopeAttemptStatus.RUNNING,
                )
        return refreshed

    def get_atomization_scope_receipt_revision(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        lineage_sha256: str,
        scope_revision: int,
    ) -> AtomizationScopeReceiptV1 | None:
        project_id, document_id, session_id = _validated_binding(
            project_id, document_id, session_id
        )
        if not _SHA256_PATTERN.fullmatch(lineage_sha256):
            raise ValueError("lineage_sha256 is invalid")
        if isinstance(scope_revision, bool) or scope_revision < 1:
            raise ValueError("scope_revision must be a positive integer")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_atomization_scope_receipts
                WHERE project_id = ? AND document_id = ? AND session_id = ?
                  AND lineage_sha256 = ? AND scope_revision = ?
                LIMIT 1
                """,
                (
                    project_id,
                    document_id,
                    session_id,
                    lineage_sha256,
                    scope_revision,
                ),
            ).fetchone()
        return _atomization_scope_receipt_from_row(row) if row is not None else None

    def get_atomization_scope_command_result(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        lineage_sha256: str,
        command_id: str,
        command_sha256: str,
    ) -> tuple[AtomizationScopeReceiptV1, RequirementAtomizationRun] | None:
        project_id, document_id, session_id = _validated_binding(
            project_id, document_id, session_id
        )
        if not _SHA256_PATTERN.fullmatch(
            lineage_sha256
        ) or not _SHA256_PATTERN.fullmatch(command_sha256):
            raise ValueError("scope command binding is invalid")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_atomization_scope_receipts
                WHERE project_id = ? AND document_id = ? AND session_id = ?
                  AND lineage_sha256 = ? AND command_id = ?
                LIMIT 1
                """,
                (project_id, document_id, session_id, lineage_sha256, command_id),
            ).fetchone()
            if row is None:
                return None
            if str(row["command_sha256"]) != command_sha256:
                raise RequirementEnrichmentConflictError(
                    "scope command_id was already used with different input"
                )
            receipt = _atomization_scope_receipt_from_row(row)
            run_row = connection.execute(
                "SELECT * FROM requirement_atomization_runs WHERE atomization_run_id = ?",
                (receipt.atomization_run_id,),
            ).fetchone()
        if run_row is None:
            raise RequirementEnrichmentRepositoryError(
                "scope receipt atomization run is missing"
            )
        return receipt, _atomization_from_row(run_row)

    def create_atomization_scope_revision(
        self,
        *,
        run: RequirementAtomizationRun,
        receipt: AtomizationScopeReceiptV1,
        attempt: AtomizationScopeAttemptV1,
        expected_scope_revision: int,
    ) -> tuple[RequirementAtomizationRun, AtomizationScopeReceiptV1, bool]:
        if run.atomization_run_id != receipt.atomization_run_id:
            raise ValueError("scope receipt must bind the supplied atomization run")
        if run.revision != receipt.scope_revision:
            raise ValueError("scope receipt revision must bind the run revision")
        if run.lineage.lineage_sha256 != receipt.lineage_sha256:
            raise ValueError("scope receipt lineage must bind the run lineage")
        if (
            receipt.project_id != run.lineage.project_id
            or receipt.document_id != run.lineage.document_id
            or receipt.session_id != run.lineage.session_id
            or receipt.review_revision != run.lineage.review_revision
        ):
            raise ValueError("scope receipt source binding must match the run")
        if isinstance(expected_scope_revision, bool) or expected_scope_revision < 0:
            raise ValueError("expected_scope_revision must be non-negative")
        if receipt.scope_revision != expected_scope_revision + 1:
            raise ValueError("scope receipt must append exactly one revision")
        if attempt.status is not AtomizationScopeAttemptStatus.SUCCEEDED:
            raise ValueError("scope revision requires a SUCCEEDED attempt")
        if (
            attempt.project_id != receipt.project_id
            or attempt.document_id != receipt.document_id
            or attempt.session_id != receipt.session_id
            or attempt.review_revision != receipt.review_revision
            or attempt.lineage_sha256 != receipt.lineage_sha256
            or attempt.command_id != receipt.command_id
            or attempt.command_sha256 != receipt.command_sha256
            or attempt.expected_scope_revision != expected_scope_revision
            or attempt.atomization_run_id != run.atomization_run_id
            or attempt.atomization_run_revision != run.revision
            or attempt.receipt_id != receipt.receipt_id
        ):
            raise ValueError("scope attempt must bind the supplied revision")
        run_json, run_sha256 = _serialize_model(run)
        receipt_json, receipt_sha256 = _serialize_model(receipt)
        with self._connect() as connection:
            with self._transaction(connection):
                _lock_and_validate_atomization_classification_head(
                    connection,
                    receipt=receipt,
                )
                _lock_atomization_scope_lineage_in_conn(
                    connection,
                    lineage_sha256=receipt.lineage_sha256,
                )
                _assert_current_scope_attempt_owner_in_connection(
                    connection,
                    attempt_id=attempt.attempt_id,
                    lineage_sha256=attempt.lineage_sha256,
                    expected_scope_revision=expected_scope_revision,
                )
                replay_row = connection.execute(
                    """
                    SELECT * FROM requirement_atomization_scope_receipts
                    WHERE lineage_sha256 = ? AND command_id = ?
                    LIMIT 1
                    """,
                    (receipt.lineage_sha256, receipt.command_id),
                ).fetchone()
                if replay_row is not None:
                    if str(replay_row["command_sha256"]) != receipt.command_sha256:
                        raise RequirementEnrichmentConflictError(
                            "scope command_id was already used with different input"
                        )
                    stored_receipt = _atomization_scope_receipt_from_row(replay_row)
                    stored_run_row = connection.execute(
                        "SELECT * FROM requirement_atomization_runs WHERE atomization_run_id = ?",
                        (stored_receipt.atomization_run_id,),
                    ).fetchone()
                    if stored_run_row is None:
                        raise RequirementEnrichmentRepositoryError(
                            "scope replay atomization run is missing"
                        )
                    stored_run = _atomization_from_row(stored_run_row)
                    if (
                        attempt.atomization_run_id != stored_run.atomization_run_id
                        or attempt.receipt_id != stored_receipt.receipt_id
                    ):
                        raise RequirementEnrichmentConflictError(
                            "scope replay produced different atomization output"
                        )
                    _update_atomization_scope_attempt_in_connection(
                        connection,
                        attempt=attempt,
                        expected_status=AtomizationScopeAttemptStatus.RUNNING,
                    )
                    return stored_run, stored_receipt, False
                head = connection.execute(
                    """
                    SELECT scope_revision
                    FROM requirement_atomization_scope_receipts
                    WHERE lineage_sha256 = ?
                    ORDER BY scope_revision DESC
                    LIMIT 1
                    """,
                    (receipt.lineage_sha256,),
                ).fetchone()
                current_revision = (
                    int(head["scope_revision"]) if head is not None else 0
                )
                if current_revision != expected_scope_revision:
                    raise RequirementEnrichmentConflictError(
                        "atomization scope revision has changed"
                    )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO requirement_atomization_runs (
                        atomization_run_id, schema_version, project_id, document_id,
                        session_id, review_revision, atomization_revision,
                        lineage_sha256, payload_sha256, payload_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.atomization_run_id,
                        run.schema_version,
                        run.lineage.project_id,
                        run.lineage.document_id,
                        run.lineage.session_id,
                        run.lineage.review_revision,
                        run.revision,
                        run.lineage.lineage_sha256,
                        run_sha256,
                        run_json,
                        run.created_at_utc.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    existing = connection.execute(
                        "SELECT * FROM requirement_atomization_runs WHERE atomization_run_id = ?",
                        (run.atomization_run_id,),
                    ).fetchone()
                    if existing is None or _atomization_from_row(existing) != run:
                        raise RequirementEnrichmentConflictError(
                            "atomization scope revision has different output"
                        )
                connection.execute(
                    """
                    INSERT INTO requirement_atomization_scope_receipts (
                        receipt_id, schema_version, project_id, document_id,
                        session_id, review_revision, lineage_sha256, scope_revision,
                        command_id, command_sha256, atomization_run_id,
                        payload_sha256, payload_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receipt_id,
                        receipt.schema_version,
                        receipt.project_id,
                        receipt.document_id,
                        receipt.session_id,
                        receipt.review_revision,
                        receipt.lineage_sha256,
                        receipt.scope_revision,
                        receipt.command_id,
                        receipt.command_sha256,
                        receipt.atomization_run_id,
                        receipt_sha256,
                        receipt_json,
                        receipt.created_at_utc.isoformat(),
                    ),
                )
                _update_atomization_scope_attempt_in_connection(
                    connection,
                    attempt=attempt,
                    expected_status=AtomizationScopeAttemptStatus.RUNNING,
                )
        return run, receipt, True

    def create_or_get_refinement(
        self,
        suggestion: RequirementRefinementSuggestion,
    ) -> RequirementRefinementSuggestion:
        if suggestion.revision != 0:
            raise ValueError("a new refinement suggestion must start at revision zero")
        payload_json, payload_sha256 = _serialize_model(suggestion)
        with self._connect() as connection:
            with self._transaction(connection):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO requirement_refinement_suggestions (
                        suggestion_id, project_id, document_id, session_id,
                        atomization_run_id, atomic_node_id, revision, status,
                        lineage_sha256, payload_sha256, payload_json,
                        created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        suggestion.suggestion_id,
                        suggestion.project_id,
                        suggestion.document_id,
                        suggestion.session_id,
                        suggestion.atomization_run_id,
                        suggestion.atomic_node_id,
                        suggestion.revision,
                        suggestion.status.value,
                        suggestion.lineage_sha256,
                        payload_sha256,
                        payload_json,
                        suggestion.created_at_utc.isoformat(),
                        suggestion.updated_at_utc.isoformat(),
                    ),
                )
                if cursor.rowcount == 1:
                    connection.execute(
                        """
                        INSERT INTO requirement_refinement_events (
                            suggestion_id, revision, status, payload_sha256,
                            payload_json, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            suggestion.suggestion_id,
                            suggestion.revision,
                            suggestion.status.value,
                            payload_sha256,
                            payload_json,
                            suggestion.created_at_utc.isoformat(),
                        ),
                    )
                    return suggestion
                row = connection.execute(
                    """
                    SELECT * FROM requirement_refinement_suggestions
                    WHERE atomization_run_id = ? AND atomic_node_id = ?
                    """,
                    (
                        suggestion.atomization_run_id,
                        suggestion.atomic_node_id,
                    ),
                ).fetchone()
        if row is None:
            raise RequirementEnrichmentRepositoryError(
                "refinement insert did not produce a readable row"
            )
        stored = _refinement_from_row(row)
        if not _same_initial_refinement(stored, suggestion):
            raise RequirementEnrichmentConflictError(
                "atomic obligation already has a different refinement suggestion"
            )
        return stored

    def get_refinement(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        suggestion_id: str,
    ) -> RequirementRefinementSuggestion | None:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_refinement_suggestions
                WHERE suggestion_id = ? AND project_id = ?
                  AND document_id = ? AND session_id = ?
                """,
                (suggestion_id, project_id, document_id, session_id),
            ).fetchone()
        return _refinement_from_row(row) if row is not None else None

    def list_refinements(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        atomization_run_id: str,
    ) -> tuple[RequirementRefinementSuggestion, ...]:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM requirement_refinement_suggestions
                WHERE project_id = ? AND document_id = ? AND session_id = ?
                  AND atomization_run_id = ?
                ORDER BY atomic_node_id
                """,
                (project_id, document_id, session_id, atomization_run_id),
            ).fetchall()
        return tuple(_refinement_from_row(row) for row in rows)

    def update_refinement(
        self,
        *,
        suggestion: RequirementRefinementSuggestion,
        expected_revision: int,
    ) -> RequirementRefinementSuggestion:
        if suggestion.revision != expected_revision + 1:
            raise ValueError("refinement update must advance exactly one revision")
        payload_json, payload_sha256 = _serialize_model(suggestion)
        with self._connect() as connection:
            with self._transaction(connection):
                row = connection.execute(
                    """
                    SELECT * FROM requirement_refinement_suggestions
                    WHERE suggestion_id = ? AND project_id = ?
                      AND document_id = ? AND session_id = ?
                    """,
                    (
                        suggestion.suggestion_id,
                        suggestion.project_id,
                        suggestion.document_id,
                        suggestion.session_id,
                    ),
                ).fetchone()
                if row is None:
                    raise RequirementEnrichmentRepositoryError(
                        "refinement suggestion was not found"
                    )
                current = _refinement_from_row(row)
                if current.revision != expected_revision:
                    raise RequirementEnrichmentConflictError(
                        "refinement suggestion revision has changed"
                    )
                _validate_refinement_lineage(current, suggestion)
                cursor = connection.execute(
                    """
                    UPDATE requirement_refinement_suggestions
                    SET revision = ?, status = ?, payload_sha256 = ?,
                        payload_json = ?, updated_at_utc = ?
                    WHERE suggestion_id = ? AND revision = ?
                    """,
                    (
                        suggestion.revision,
                        suggestion.status.value,
                        payload_sha256,
                        payload_json,
                        suggestion.updated_at_utc.isoformat(),
                        suggestion.suggestion_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RequirementEnrichmentConflictError(
                        "refinement suggestion revision has changed"
                    )
                connection.execute(
                    """
                    INSERT INTO requirement_refinement_events (
                        suggestion_id, revision, status, payload_sha256,
                        payload_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        suggestion.suggestion_id,
                        suggestion.revision,
                        suggestion.status.value,
                        payload_sha256,
                        payload_json,
                        suggestion.updated_at_utc.isoformat(),
                    ),
                )
        return suggestion

    def create_or_get_publication_binding_preview_v2(
        self,
        preview: RequirementEnrichmentPublicationBindingPreviewV2,
    ) -> RequirementEnrichmentPublicationBindingPreviewV2:
        preview = RequirementEnrichmentPublicationBindingPreviewV2.model_validate(
            preview.model_dump(mode="json")
        )
        payload_json = _canonical_model_json(preview)
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            with self._transaction(connection):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO
                        requirement_enrichment_publication_binding_previews (
                            binding_preview_id, schema_version, project_id,
                            document_id, session_id, review_revision,
                            base_active_requirement_set_id,
                            accepted_classification_snapshot_id,
                            accepted_classification_sha256, atomization_state,
                            projection_sha256, payload_sha256, payload_json,
                            created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        preview.binding_preview_id,
                        preview.schema_version,
                        preview.project_id,
                        preview.document_id,
                        preview.session_id,
                        preview.review_revision,
                        preview.base_active_requirement_set_id,
                        preview.projection.entry_classification_snapshot_id,
                        preview.projection.entry_classification_sha256,
                        preview.atomization_state.value,
                        preview.projection.projection_sha256,
                        preview.payload_sha256,
                        payload_json,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT *
                    FROM requirement_enrichment_publication_binding_previews
                    WHERE binding_preview_id = ?
                    """,
                    (preview.binding_preview_id,),
                ).fetchone()
        if row is None:
            raise RequirementEnrichmentRepositoryError(
                "publication binding preview insert was not readable"
            )
        stored = _publication_binding_preview_v2_from_row(row)
        if stored != preview:
            raise RequirementEnrichmentConflictError(
                "publication binding preview identity contains different contents"
            )
        return stored

    def get_publication_binding_preview_v2(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        binding_preview_id: str,
    ) -> RequirementEnrichmentPublicationBindingPreviewV2 | None:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM requirement_enrichment_publication_binding_previews
                WHERE binding_preview_id = ? AND project_id = ?
                  AND document_id = ? AND session_id = ?
                """,
                (binding_preview_id, project_id, document_id, session_id),
            ).fetchone()
        return (
            _publication_binding_preview_v2_from_row(row)
            if row is not None
            else None
        )

    def begin_publication_binding_v2(
        self,
        *,
        preview: RequirementEnrichmentPublicationBindingPreviewV2,
        current_projection: RequirementEnrichmentPublicationProjectionV2,
    ) -> RequirementEnrichmentPublicationStartV2:
        """Atomically materialize an exact inactive PUBLISHING draft."""

        preview = RequirementEnrichmentPublicationBindingPreviewV2.model_validate(
            preview.model_dump(mode="json")
        )
        current_projection = RequirementEnrichmentPublicationProjectionV2.model_validate(
            current_projection.model_dump(mode="json")
        )
        if current_projection != preview.projection:
            raise RequirementEnrichmentConflictError(
                "publication binding preview is stale"
            )
        binding_id = stable_publication_binding_v2_id(preview.binding_preview_id)
        row_bindings = build_requirement_enrichment_publication_row_bindings_v2(
            binding_id=binding_id,
            projection=current_projection,
        )
        timestamp = datetime.now(timezone.utc)
        with self._connect() as connection:
            with self._transaction(connection):
                stored_preview_row = connection.execute(
                    """
                    SELECT *
                    FROM requirement_enrichment_publication_binding_previews
                    WHERE binding_preview_id = ? AND project_id = ?
                      AND document_id = ? AND session_id = ?
                    """,
                    (
                        preview.binding_preview_id,
                        preview.project_id,
                        preview.document_id,
                        preview.session_id,
                    ),
                ).fetchone()
                if stored_preview_row is None:
                    raise RequirementEnrichmentRepositoryError(
                        "publication binding preview must be persisted before publish"
                    )
                if _publication_binding_preview_v2_from_row(stored_preview_row) != preview:
                    raise RequirementEnrichmentConflictError(
                        "persisted publication binding preview has changed"
                    )

                locked = connection.execute(
                    """
                    UPDATE sor_documents SET updated_at = updated_at
                    WHERE id = ? AND project_id = ?
                    """,
                    (preview.document_id, preview.project_id),
                )
                if locked.rowcount != 1:
                    raise RequirementEnrichmentRepositoryError(
                        "publication document was not found"
                    )
                document = connection.execute(
                    """
                    SELECT * FROM sor_documents
                    WHERE id = ? AND project_id = ?
                    """,
                    (preview.document_id, preview.project_id),
                ).fetchone()
                assert document is not None
                if str(document["source_sha256"] or "") != current_projection.source_sha256:
                    raise RequirementEnrichmentConflictError(
                        "document source changed after publication preview"
                    )
                active_requirement_set_id = str(
                    document["active_requirement_set_id"] or ""
                )
                if active_requirement_set_id != preview.base_active_requirement_set_id:
                    raise RequirementEnrichmentConflictError(
                        "active Requirement Set changed after publication preview"
                    )
                _validate_publication_projection_v2_sidecars_in_conn(
                    connection,
                    preview=preview,
                )

                existing = connection.execute(
                    """
                    SELECT b.*, rs.version AS requirement_set_version,
                           rs.status AS requirement_set_status,
                           rs.is_active AS requirement_set_is_active,
                           rs.publication_state AS requirement_set_publication_state
                    FROM requirement_enrichment_publication_bindings b
                    INNER JOIN requirement_sets rs ON rs.id = b.requirement_set_id
                    WHERE b.binding_id = ?
                    """,
                    (binding_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["requirement_set_status"] or "").upper()
                        != "DRAFT"
                        or bool(int(existing["requirement_set_is_active"] or 0))
                        or str(existing["project_id"] or "") != preview.project_id
                        or str(existing["document_id"] or "") != preview.document_id
                        or str(existing["binding_preview_id"] or "")
                        != preview.binding_preview_id
                        or str(existing["projection_sha256"] or "")
                        != current_projection.projection_sha256
                    ):
                        raise RequirementEnrichmentConflictError(
                            "publication binding is no longer a reusable draft"
                        )
                    existing_rows = _publication_row_bindings_v2_in_conn(
                        connection,
                        binding_id=binding_id,
                    )
                    if existing_rows != row_bindings:
                        raise RequirementEnrichmentConflictError(
                            "publication binding rows no longer match the preview"
                        )
                    return RequirementEnrichmentPublicationStartV2(
                        binding_id=binding_id,
                        binding_preview_id=preview.binding_preview_id,
                        binding_preview_sha256=preview.payload_sha256,
                        project_id=preview.project_id,
                        document_id=preview.document_id,
                        session_id=preview.session_id,
                        review_revision=preview.review_revision,
                        base_active_requirement_set_id=(
                            preview.base_active_requirement_set_id
                        ),
                        projection_sha256=current_projection.projection_sha256,
                        requirement_set_id=str(existing["requirement_set_id"]),
                        requirement_set_version=int(
                            existing["requirement_set_version"]
                        ),
                        row_bindings=existing_rows,
                        created=False,
                    )

                version_row = connection.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                    FROM requirement_sets WHERE sor_document_id = ?
                    """,
                    (preview.document_id,),
                ).fetchone()
                version = int(version_row["next_version"] or 1)
                requirement_set_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"reqpilotx:w4:{binding_id}")
                )
                connection.execute(
                    """
                    INSERT INTO requirement_sets (
                        id, project_id, sor_document_id, version, status,
                        is_active, source_hash, preview_generation_id,
                        supersedes_id, created_at, activated_at, locked_at,
                        publication_key, publication_state,
                        enrichment_publication_binding_id,
                        enrichment_publication_binding_sha256
                    ) VALUES (?, ?, ?, ?, 'DRAFT', 0, ?, ?, ?, ?, '', '',
                              '', 'PUBLISHING', '', '')
                    """,
                    (
                        requirement_set_id,
                        preview.project_id,
                        preview.document_id,
                        version,
                        current_projection.source_sha256,
                        str(document["preview_generation_id"] or ""),
                        active_requirement_set_id,
                        timestamp.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO requirement_enrichment_publication_bindings (
                        binding_id, binding_preview_id, project_id, document_id,
                        requirement_set_id, projection_sha256,
                        publication_state, payload_sha256, payload_json,
                        created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PUBLISHING', '', '', ?, ?)
                    """,
                    (
                        binding_id,
                        preview.binding_preview_id,
                        preview.project_id,
                        preview.document_id,
                        requirement_set_id,
                        current_projection.projection_sha256,
                        timestamp.isoformat(),
                        timestamp.isoformat(),
                    ),
                )
                _insert_publication_binding_rows_v2(
                    connection,
                    preview=preview,
                    binding_id=binding_id,
                    requirement_set_id=requirement_set_id,
                    row_bindings=row_bindings,
                    timestamp=timestamp,
                )
        return RequirementEnrichmentPublicationStartV2(
            binding_id=binding_id,
            binding_preview_id=preview.binding_preview_id,
            binding_preview_sha256=preview.payload_sha256,
            project_id=preview.project_id,
            document_id=preview.document_id,
            session_id=preview.session_id,
            review_revision=preview.review_revision,
            base_active_requirement_set_id=preview.base_active_requirement_set_id,
            projection_sha256=current_projection.projection_sha256,
            requirement_set_id=requirement_set_id,
            requirement_set_version=version,
            row_bindings=row_bindings,
            created=True,
        )

    def finalize_publication_binding_v2(
        self,
        binding: RequirementEnrichmentPublicationBindingV2,
    ) -> RequirementEnrichmentPublicationBindingV2:
        binding = RequirementEnrichmentPublicationBindingV2.model_validate(
            binding.model_dump(mode="json")
        )
        payload_json = _canonical_model_json(binding)
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            with self._transaction(connection):
                row = connection.execute(
                    """
                    SELECT b.*, rs.status AS requirement_set_status,
                           rs.is_active AS requirement_set_is_active,
                           rs.publication_state AS requirement_set_publication_state,
                           rs.publication_key AS requirement_set_publication_key,
                           rs.requirement_tree_object_key,
                           rs.requirement_tree_sha256,
                           rs.requirement_tree_schema_version,
                           rs.enrichment_publication_binding_id,
                           rs.enrichment_publication_binding_sha256
                    FROM requirement_enrichment_publication_bindings b
                    INNER JOIN requirement_sets rs ON rs.id = b.requirement_set_id
                    WHERE b.binding_id = ? AND b.requirement_set_id = ?
                    """,
                    (binding.binding_id, binding.requirement_set_id),
                ).fetchone()
                if row is None:
                    raise RequirementEnrichmentRepositoryError(
                        "publication binding start was not found"
                    )
                preview_row = connection.execute(
                    """
                    SELECT *
                    FROM requirement_enrichment_publication_binding_previews
                    WHERE binding_preview_id = ? AND project_id = ?
                      AND document_id = ? AND session_id = ?
                    """,
                    (
                        binding.binding_preview_id,
                        binding.project_id,
                        binding.document_id,
                        binding.session_id,
                    ),
                ).fetchone()
                if preview_row is None:
                    raise RequirementEnrichmentRepositoryError(
                        "publication binding preview disappeared before finalization"
                    )
                preview = _publication_binding_preview_v2_from_row(preview_row)
                if (
                    preview.payload_sha256 != binding.binding_preview_sha256
                    or preview.projection.projection_sha256
                    != binding.projection_sha256
                    or preview.review_revision != binding.review_revision
                    or preview.base_active_requirement_set_id
                    != binding.base_active_requirement_set_id
                ):
                    raise RequirementEnrichmentConflictError(
                        "publication binding preview changed before finalization"
                    )
                locked = connection.execute(
                    """
                    UPDATE sor_documents SET updated_at = updated_at
                    WHERE id = ? AND project_id = ?
                    """,
                    (binding.document_id, binding.project_id),
                )
                if locked.rowcount != 1:
                    raise RequirementEnrichmentRepositoryError(
                        "publication document disappeared before finalization"
                    )
                document = connection.execute(
                    """
                    SELECT source_sha256, active_requirement_set_id
                    FROM sor_documents WHERE id = ? AND project_id = ?
                    """,
                    (binding.document_id, binding.project_id),
                ).fetchone()
                if (
                    document is None
                    or str(document["source_sha256"] or "")
                    != preview.projection.source_sha256
                ):
                    raise RequirementEnrichmentConflictError(
                        "document source changed after publication preview"
                    )
                if str(document["active_requirement_set_id"] or "") != (
                    preview.base_active_requirement_set_id
                ):
                    raise RequirementEnrichmentConflictError(
                        "active Requirement Set changed after publication preview"
                    )
                _validate_publication_projection_v2_sidecars_in_conn(
                    connection,
                    preview=preview,
                )
                if str(row["publication_state"] or "").upper() == "READY":
                    stored = _publication_binding_v2_from_row(row)
                    if stored != binding:
                        raise RequirementEnrichmentConflictError(
                            "READY publication binding contains different contents"
                        )
                    return stored
                if (
                    str(row["publication_state"] or "").upper() != "PUBLISHING"
                    or str(row["requirement_set_publication_state"] or "").upper()
                    != "PUBLISHING"
                    or str(row["requirement_set_status"] or "").upper() != "DRAFT"
                    or bool(int(row["requirement_set_is_active"] or 0))
                    or str(row["requirement_set_publication_key"] or "")
                    or str(row["enrichment_publication_binding_id"] or "")
                    or str(row["enrichment_publication_binding_sha256"] or "")
                ):
                    raise RequirementEnrichmentConflictError(
                        "publication binding is not an exact inactive PUBLISHING draft"
                    )
                if (
                    str(row["binding_preview_id"] or "")
                    != binding.binding_preview_id
                    or str(row["projection_sha256"] or "")
                    != binding.projection_sha256
                    or str(row["requirement_tree_object_key"] or "")
                    != binding.requirement_tree_object_key
                    or str(row["requirement_tree_sha256"] or "")
                    != binding.requirement_tree_sha256
                    or str(row["requirement_tree_schema_version"] or "")
                    != binding.requirement_tree_schema_version
                    or _publication_row_bindings_v2_in_conn(
                        connection,
                        binding_id=binding.binding_id,
                    )
                    != binding.row_bindings
                ):
                    raise RequirementEnrichmentConflictError(
                        "publication tree or rows changed before finalization"
                    )
                _validate_publication_materialized_rows_v2_in_conn(
                    connection,
                    preview=preview,
                    requirement_set_id=binding.requirement_set_id,
                    row_bindings=binding.row_bindings,
                )
                updated_binding = connection.execute(
                    """
                    UPDATE requirement_enrichment_publication_bindings
                    SET publication_state = 'READY', payload_sha256 = ?,
                        payload_json = ?, updated_at_utc = ?
                    WHERE binding_id = ? AND publication_state = 'PUBLISHING'
                      AND payload_sha256 = '' AND payload_json = ''
                    """,
                    (
                        binding.payload_sha256,
                        payload_json,
                        timestamp,
                        binding.binding_id,
                    ),
                )
                updated_set = connection.execute(
                    """
                    UPDATE requirement_sets
                    SET publication_state = 'READY',
                        enrichment_publication_binding_id = ?,
                        enrichment_publication_binding_sha256 = ?
                    WHERE id = ? AND status = 'DRAFT' AND is_active = 0
                      AND publication_state = 'PUBLISHING'
                      AND publication_key = ''
                      AND enrichment_publication_binding_id = ''
                      AND enrichment_publication_binding_sha256 = ''
                    """,
                    (
                        binding.binding_id,
                        binding.payload_sha256,
                        binding.requirement_set_id,
                    ),
                )
                if updated_binding.rowcount != 1 or updated_set.rowcount != 1:
                    raise RequirementEnrichmentConflictError(
                        "publication binding changed during finalization"
                    )
        return binding

    def abort_publication_binding_v2(
        self,
        started: RequirementEnrichmentPublicationStartV2,
    ) -> bool:
        """Delete only the exact PUBLISHING draft owned by this creator."""

        started = RequirementEnrichmentPublicationStartV2.model_validate(
            started.model_dump(mode="json")
        )
        if not started.created:
            return False
        with self._connect() as connection:
            with self._transaction(connection):
                locked = connection.execute(
                    """
                    UPDATE sor_documents SET updated_at = updated_at
                    WHERE id = ? AND project_id = ?
                    """,
                    (started.document_id, started.project_id),
                )
                if locked.rowcount != 1:
                    return False
                target = connection.execute(
                    """
                    SELECT b.binding_id, b.binding_preview_id,
                           b.projection_sha256, b.publication_state,
                           rs.id AS requirement_set_id, rs.status,
                           rs.is_active, rs.activated_at, rs.publication_key,
                           rs.publication_state AS requirement_set_publication_state,
                           rs.enrichment_publication_binding_id,
                           rs.enrichment_publication_binding_sha256
                    FROM requirement_enrichment_publication_bindings b
                    INNER JOIN requirement_sets rs ON rs.id = b.requirement_set_id
                    WHERE b.binding_id = ? AND b.requirement_set_id = ?
                      AND b.project_id = ? AND b.document_id = ?
                    """,
                    (
                        started.binding_id,
                        started.requirement_set_id,
                        started.project_id,
                        started.document_id,
                    ),
                ).fetchone()
                if (
                    target is None
                    or str(target["binding_preview_id"] or "")
                    != started.binding_preview_id
                    or str(target["projection_sha256"] or "")
                    != started.projection_sha256
                    or str(target["publication_state"] or "").upper()
                    != "PUBLISHING"
                    or str(target["requirement_set_publication_state"] or "").upper()
                    != "PUBLISHING"
                    or str(target["status"] or "").upper() != "DRAFT"
                    or bool(int(target["is_active"] or 0))
                    or str(target["activated_at"] or "")
                    or str(target["publication_key"] or "")
                    or str(target["enrichment_publication_binding_id"] or "")
                    or str(target["enrichment_publication_binding_sha256"] or "")
                ):
                    return False
                deleted = connection.execute(
                    """
                    DELETE FROM requirement_sets
                    WHERE id = ? AND project_id = ? AND sor_document_id = ?
                      AND status = 'DRAFT' AND is_active = 0
                      AND COALESCE(activated_at, '') = ''
                      AND publication_key = ''
                      AND publication_state = 'PUBLISHING'
                      AND enrichment_publication_binding_id = ''
                      AND enrichment_publication_binding_sha256 = ''
                    """,
                    (
                        started.requirement_set_id,
                        started.project_id,
                        started.document_id,
                    ),
                )
                return deleted.rowcount == 1

    def get_publication_binding_v2(
        self,
        *,
        requirement_set_id: str,
        binding_id: str,
    ) -> RequirementEnrichmentPublicationBindingV2 | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_enrichment_publication_bindings
                WHERE binding_id = ? AND requirement_set_id = ?
                  AND publication_state = 'READY'
                """,
                (binding_id, requirement_set_id),
            ).fetchone()
            if row is None:
                return None
            binding = _publication_binding_v2_from_row(row)
            if _publication_row_bindings_v2_in_conn(
                connection,
                binding_id=binding_id,
            ) != binding.row_bindings:
                raise RequirementEnrichmentRepositoryError(
                    "publication binding row sidecars do not match final binding"
                )
        return binding

    def get_publication_binding_for_preview_v2(
        self,
        *,
        binding_preview_id: str,
    ) -> RequirementEnrichmentPublicationBindingV2 | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_enrichment_publication_bindings
                WHERE binding_preview_id = ? AND publication_state = 'READY'
                """,
                (binding_preview_id,),
            ).fetchone()
            if row is None:
                return None
            binding = _publication_binding_v2_from_row(row)
            if _publication_row_bindings_v2_in_conn(
                connection,
                binding_id=binding.binding_id,
            ) != binding.row_bindings:
                raise RequirementEnrichmentRepositoryError(
                    "publication binding row sidecars do not match final binding"
                )
        return binding

    def create_or_get_publication_preview(
        self,
        preview: RequirementPublicationPreview,
    ) -> RequirementPublicationPreview:
        payload_json, payload_sha256 = _serialize_model(preview)
        with self._connect() as connection:
            with self._transaction(connection):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO requirement_enrichment_publication_previews (
                        preview_id, project_id, document_id, session_id,
                        atomization_run_id, publication_key, lineage_sha256,
                        payload_sha256, payload_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        preview.preview_id,
                        preview.project_id,
                        preview.document_id,
                        preview.session_id,
                        preview.atomization_run_id,
                        preview.publication_key,
                        preview.lineage_sha256,
                        payload_sha256,
                        payload_json,
                        preview.created_at_utc.isoformat(),
                    ),
                )
                if cursor.rowcount == 1:
                    return preview
                row = connection.execute(
                    """
                    SELECT * FROM requirement_enrichment_publication_previews
                    WHERE publication_key = ?
                    """,
                    (preview.publication_key,),
                ).fetchone()
        if row is None:
            raise RequirementEnrichmentRepositoryError(
                "publication preview insert did not produce a readable row"
            )
        stored = _preview_from_row(row)
        if not _same_publication_preview(stored, preview):
            raise RequirementEnrichmentConflictError(
                "publication key already contains a different preview"
            )
        return stored

    def get_publication_preview(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        preview_id: str,
    ) -> RequirementPublicationPreview | None:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_enrichment_publication_previews
                WHERE preview_id = ? AND project_id = ?
                  AND document_id = ? AND session_id = ?
                """,
                (preview_id, project_id, document_id, session_id),
            ).fetchone()
        return _preview_from_row(row) if row is not None else None

    def get_latest_publication_preview(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        atomization_run_id: str,
    ) -> RequirementPublicationPreview | None:
        project_id, document_id, session_id = _validated_binding(
            project_id,
            document_id,
            session_id,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_enrichment_publication_previews
                WHERE project_id = ? AND document_id = ? AND session_id = ?
                  AND atomization_run_id = ?
                ORDER BY created_at_utc DESC, rowid DESC LIMIT 1
                """,
                (project_id, document_id, session_id, atomization_run_id),
            ).fetchone()
        return _preview_from_row(row) if row is not None else None

    def get_publication_for_preview(
        self,
        *,
        preview_id: str,
    ) -> RequirementPublicationResult | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM requirement_enrichment_publications
                WHERE preview_id = ?
                """,
                (preview_id,),
            ).fetchone()
        if row is None:
            return None
        return _deserialize_model(
            str(row["result_json"]),
            str(row["result_sha256"]),
            RequirementPublicationResult,
        )

    def publish_draft(
        self,
        preview: RequirementPublicationPreview,
    ) -> RequirementPublicationResult:
        timestamp = datetime.now(timezone.utc)
        with self._connect() as connection:
            with self._transaction(connection):
                existing = connection.execute(
                    """
                    SELECT p.*, rs.status AS current_status,
                           rs.is_active AS current_is_active
                    FROM requirement_enrichment_publications p
                    INNER JOIN requirement_sets rs
                      ON rs.id = p.requirement_set_id
                    WHERE p.publication_key = ?
                    """,
                    (preview.publication_key,),
                ).fetchone()
                if existing is not None:
                    if str(existing["current_status"] or "").upper() != "DRAFT" or bool(
                        int(existing["current_is_active"] or 0)
                    ):
                        raise RequirementEnrichmentConflictError(
                            "published draft has already been promoted"
                        )
                    stored = _deserialize_model(
                        str(existing["result_json"]),
                        str(existing["result_sha256"]),
                        RequirementPublicationResult,
                    )
                    return stored.model_copy(update={"created": False})

                stored_preview = connection.execute(
                    """
                    SELECT payload_sha256 FROM
                        requirement_enrichment_publication_previews
                    WHERE preview_id = ? AND publication_key = ?
                    """,
                    (preview.preview_id, preview.publication_key),
                ).fetchone()
                if stored_preview is None:
                    raise RequirementEnrichmentRepositoryError(
                        "publication preview must be persisted before publish"
                    )
                document = connection.execute(
                    """
                    SELECT * FROM sor_documents
                    WHERE id = ? AND project_id = ?
                    """,
                    (preview.document_id, preview.project_id),
                ).fetchone()
                if document is None:
                    raise RequirementEnrichmentRepositoryError(
                        "publication document was not found"
                    )
                active_requirement_set_id = str(
                    document["active_requirement_set_id"] or ""
                )
                if active_requirement_set_id != preview.base_active_requirement_set_id:
                    raise RequirementEnrichmentConflictError(
                        "active Requirement Set changed after publication preview"
                    )
                version_row = connection.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                    FROM requirement_sets WHERE sor_document_id = ?
                    """,
                    (preview.document_id,),
                ).fetchone()
                version = int(version_row["next_version"] or 1)
                requirement_set_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"reqpilotx:{preview.publication_key}",
                    )
                )
                connection.execute(
                    """
                    INSERT INTO requirement_sets (
                        id, project_id, sor_document_id, version, status,
                        is_active, source_hash, preview_generation_id,
                        supersedes_id, created_at, activated_at, locked_at
                    ) VALUES (?, ?, ?, ?, 'DRAFT', 0, ?, ?, ?, ?, '', '')
                    """,
                    (
                        requirement_set_id,
                        preview.project_id,
                        preview.document_id,
                        version,
                        str(document["source_sha256"] or ""),
                        str(document["preview_generation_id"] or ""),
                        active_requirement_set_id,
                        timestamp.isoformat(),
                    ),
                )
                _insert_publication_entries(
                    connection,
                    preview=preview,
                    requirement_set_id=requirement_set_id,
                    timestamp=timestamp,
                )
                result = RequirementPublicationResult(
                    project_id=preview.project_id,
                    document_id=preview.document_id,
                    preview_id=preview.preview_id,
                    publication_key=preview.publication_key,
                    lineage_sha256=preview.lineage_sha256,
                    requirement_set_id=requirement_set_id,
                    requirement_set_version=version,
                    active_requirement_set_id=active_requirement_set_id,
                    created=True,
                    created_at_utc=timestamp,
                )
                result_json, result_sha256 = _serialize_model(result)
                connection.execute(
                    """
                    INSERT INTO requirement_enrichment_publications (
                        publication_key, preview_id, requirement_set_id,
                        result_sha256, result_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        preview.publication_key,
                        preview.preview_id,
                        requirement_set_id,
                        result_sha256,
                        result_json,
                        timestamp.isoformat(),
                    ),
                )
                unchanged = connection.execute(
                    """
                    SELECT active_requirement_set_id FROM sor_documents
                    WHERE id = ?
                    """,
                    (preview.document_id,),
                ).fetchone()
                if (
                    unchanged is None
                    or str(unchanged["active_requirement_set_id"] or "")
                    != active_requirement_set_id
                ):
                    raise RequirementEnrichmentRepositoryError(
                        "draft publication changed the active Requirement Set"
                    )
        return result

    @contextmanager
    def _transaction(self, connection: Any) -> Iterator[None]:
        if connection.in_transaction:
            name = f"requirement_enrichment_{id(self):x}"
            connection.execute(f"SAVEPOINT {name}")
            try:
                yield
            except BaseException:
                connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
                connection.execute(f"RELEASE SAVEPOINT {name}")
                raise
            else:
                connection.execute(f"RELEASE SAVEPOINT {name}")
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def _canonical_model_json(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _publication_binding_preview_v2_from_row(
    row: Any,
) -> RequirementEnrichmentPublicationBindingPreviewV2:
    try:
        preview = RequirementEnrichmentPublicationBindingPreviewV2.model_validate(
            json.loads(str(row["payload_json"]))
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RequirementEnrichmentRepositoryError(
            "stored publication binding preview is invalid"
        ) from exc
    if (
        str(row["payload_sha256"] or "") != preview.payload_sha256
        or str(row["binding_preview_id"] or "") != preview.binding_preview_id
        or str(row["project_id"] or "") != preview.project_id
        or str(row["document_id"] or "") != preview.document_id
        or str(row["session_id"] or "") != preview.session_id
        or int(row["review_revision"] or 0) != preview.review_revision
        or str(row["base_active_requirement_set_id"] or "")
        != preview.base_active_requirement_set_id
        or str(row["accepted_classification_snapshot_id"] or "")
        != preview.projection.entry_classification_snapshot_id
        or str(row["accepted_classification_sha256"] or "")
        != preview.projection.entry_classification_sha256
        or str(row["atomization_state"] or "") != preview.atomization_state.value
        or str(row["projection_sha256"] or "")
        != preview.projection.projection_sha256
    ):
        raise RequirementEnrichmentRepositoryError(
            "stored publication binding preview envelope does not match payload"
        )
    return preview


def _publication_binding_v2_from_row(
    row: Any,
) -> RequirementEnrichmentPublicationBindingV2:
    if str(row["publication_state"] or "").upper() != "READY":
        raise RequirementEnrichmentRepositoryError(
            "publication binding is not READY"
        )
    try:
        binding = RequirementEnrichmentPublicationBindingV2.model_validate(
            json.loads(str(row["payload_json"]))
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RequirementEnrichmentRepositoryError(
            "stored publication binding is invalid"
        ) from exc
    if (
        str(row["payload_sha256"] or "") != binding.payload_sha256
        or str(row["binding_id"] or "") != binding.binding_id
        or str(row["binding_preview_id"] or "") != binding.binding_preview_id
        or str(row["project_id"] or "") != binding.project_id
        or str(row["document_id"] or "") != binding.document_id
        or str(row["requirement_set_id"] or "") != binding.requirement_set_id
        or str(row["projection_sha256"] or "") != binding.projection_sha256
    ):
        raise RequirementEnrichmentRepositoryError(
            "stored publication binding envelope does not match payload"
        )
    return binding


def _publication_row_bindings_v2_in_conn(
    connection: Any,
    *,
    binding_id: str,
) -> tuple[RequirementEnrichmentPublicationRowBindingV2, ...]:
    rows = connection.execute(
        """
        SELECT * FROM requirement_enrichment_publication_row_bindings
        WHERE binding_id = ? ORDER BY publication_order
        """,
        (binding_id,),
    ).fetchall()
    result: list[RequirementEnrichmentPublicationRowBindingV2] = []
    for row in rows:
        try:
            binding = RequirementEnrichmentPublicationRowBindingV2.model_validate(
                json.loads(str(row["payload_json"]))
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RequirementEnrichmentRepositoryError(
                "stored publication row binding is invalid"
            ) from exc
        if (
            str(row["binding_id"] or "") != binding.binding_id
            or int(row["publication_order"] or 0) != binding.publication_order
            or str(row["requirement_tree_node_id"] or "")
            != binding.requirement_tree_node_id
            or str(row["requirement_entry_id"] or "")
            != binding.requirement_entry_id
            or str(row["projection_row_sha256"] or "")
            != binding.projection_row_sha256
            or str(row["decision_trace_sha256"] or "")
            != binding.decision_trace_sha256
            or str(row["payload_sha256"] or "") != binding.payload_sha256
        ):
            raise RequirementEnrichmentRepositoryError(
                "stored publication row binding envelope does not match payload"
            )
        result.append(binding)
    return tuple(result)


def _validate_publication_projection_v2_sidecars_in_conn(
    connection: Any,
    *,
    preview: RequirementEnrichmentPublicationBindingPreviewV2,
) -> None:
    projection = preview.projection
    classification = connection.execute(
        """
        SELECT * FROM entry_classification_snapshots
        WHERE id = ? AND project_id = ? AND sor_document_id = ?
        """,
        (
            projection.entry_classification_snapshot_id,
            projection.project_id,
            projection.document_id,
        ),
    ).fetchone()
    try:
        from app.application.extraction.entry_classification_repository import (
            EntryClassificationSnapshotCorrupted,
            _decode_snapshot,
            entry_classification_lineage_values,
        )
        from app.domain.entry_classification import EntryClassificationStatus

        accepted_classification = (
            None if classification is None else _decode_snapshot(classification)
        )
    except EntryClassificationSnapshotCorrupted as exc:
        raise RequirementEnrichmentConflictError(
            "accepted entry classification is corrupted"
        ) from exc
    if (
        accepted_classification is None
        or accepted_classification.status
        is not EntryClassificationStatus.ACCEPTED
        or accepted_classification.payload_sha256
        != projection.entry_classification_sha256
        or accepted_classification.source_sha256 != projection.source_sha256
        or accepted_classification.working_tree_id
        != projection.accepted_working_tree_id
        or accepted_classification.working_tree_sha256
        != projection.accepted_working_tree_sha256
        or accepted_classification.effective_structure_id
        != projection.effective_structure_id
        or accepted_classification.revision
        != projection.entry_classification_revision
    ):
        raise RequirementEnrichmentConflictError(
            "accepted entry classification changed after publication preview"
        )
    classification_lineage = entry_classification_lineage_values(
        accepted_classification
    )
    if type(connection).__module__.endswith("postgres_compat"):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            (
                "\x1f".join(
                    (
                        "entry-classification-snapshot/v1",
                        *classification_lineage,
                    )
                ),
            ),
        )
    classification_head_row = connection.execute(
        """
        SELECT * FROM entry_classification_snapshots
        WHERE project_id = ? AND sor_document_id = ? AND source_sha256 = ?
          AND working_tree_id = ? AND working_tree_sha256 = ?
          AND effective_structure_id = ? AND semantic_snapshot_id = ?
          AND policy_version = ?
        ORDER BY revision DESC
        LIMIT 1
        """,
        classification_lineage,
    ).fetchone()
    try:
        classification_head = (
            None
            if classification_head_row is None
            else _decode_snapshot(classification_head_row)
        )
    except EntryClassificationSnapshotCorrupted as exc:
        raise RequirementEnrichmentConflictError(
            "current accepted classification head is corrupted"
        ) from exc
    if classification_head != accepted_classification:
        raise RequirementEnrichmentConflictError(
            "current accepted classification head changed after publication preview"
        )
    if (
        projection.atomization_state
        is RequirementEnrichmentAtomizationStateV2.NOT_RUN
    ):
        return
    receipt = connection.execute(
        """
        SELECT * FROM requirement_atomization_scope_receipts
        WHERE receipt_id = ? AND project_id = ? AND document_id = ?
          AND session_id = ? AND scope_revision = ?
        """,
        (
            projection.atomization_scope_receipt_id,
            projection.project_id,
            projection.document_id,
            preview.session_id,
            projection.atomization_scope_revision,
        ),
    ).fetchone()
    run = connection.execute(
        """
        SELECT * FROM requirement_atomization_runs
        WHERE atomization_run_id = ? AND project_id = ? AND document_id = ?
          AND session_id = ? AND atomization_revision = ?
        """,
        (
            projection.atomization_run_id,
            projection.project_id,
            projection.document_id,
            preview.session_id,
            projection.atomization_revision,
        ),
    ).fetchone()
    core_preview = connection.execute(
        """
        SELECT * FROM requirement_enrichment_publication_previews
        WHERE preview_id = ? AND project_id = ? AND document_id = ?
          AND session_id = ? AND atomization_run_id = ?
        """,
        (
            projection.publication_preview_id,
            projection.project_id,
            projection.document_id,
            preview.session_id,
            projection.atomization_run_id,
        ),
    ).fetchone()
    try:
        stored_receipt = (
            None
            if receipt is None
            else _atomization_scope_receipt_from_row(receipt)
        )
        stored_run = None if run is None else _atomization_from_row(run)
        stored_core_preview = (
            None if core_preview is None else _preview_from_row(core_preview)
        )
    except RequirementEnrichmentRepositoryError as exc:
        raise RequirementEnrichmentConflictError(
            "atomization publication sidecars are corrupted"
        ) from exc
    if stored_receipt is not None:
        _lock_atomization_scope_lineage_in_conn(
            connection,
            lineage_sha256=stored_receipt.lineage_sha256,
        )
        latest_receipt_row = connection.execute(
            """
            SELECT * FROM requirement_atomization_scope_receipts
            WHERE project_id = ? AND document_id = ? AND session_id = ?
              AND lineage_sha256 = ?
            ORDER BY scope_revision DESC
            LIMIT 1
            """,
            (
                projection.project_id,
                projection.document_id,
                preview.session_id,
                stored_receipt.lineage_sha256,
            ),
        ).fetchone()
        try:
            latest_receipt = (
                None
                if latest_receipt_row is None
                else _atomization_scope_receipt_from_row(latest_receipt_row)
            )
        except RequirementEnrichmentRepositoryError as exc:
            raise RequirementEnrichmentConflictError(
                "current atomization scope receipt head is corrupted"
            ) from exc
    else:
        latest_receipt = None
    if (
        stored_receipt is None
        or latest_receipt != stored_receipt
        or stored_receipt.payload_sha256
        != projection.atomization_scope_receipt_sha256
        or stored_receipt.active_revision
        != projection.atomization_active_revision
        or stored_receipt.entry_classification_content_sha256
        != projection.atomization_classification_content_sha256
        or projection.atomization_classification_content_sha256
        != projection.entry_classification_content_sha256
        or stored_receipt.atomization_run_id
        != projection.atomization_run_id
        or stored_run is None
        or _serialize_model(stored_run)[1] != projection.atomization_run_sha256
        or stored_core_preview is None
        or _serialize_model(stored_core_preview)[1]
        != projection.publication_preview_sha256
    ):
        raise RequirementEnrichmentConflictError(
            "current atomization scope receipt head or publication sidecars "
            "changed after publication preview"
        )


def _validate_publication_materialized_rows_v2_in_conn(
    connection: Any,
    *,
    preview: RequirementEnrichmentPublicationBindingPreviewV2,
    requirement_set_id: str,
    row_bindings: tuple[RequirementEnrichmentPublicationRowBindingV2, ...],
) -> None:
    count_row = connection.execute(
        "SELECT COUNT(*) AS row_count FROM requirements WHERE requirement_set_id = ?",
        (requirement_set_id,),
    ).fetchone()
    if count_row is None or int(count_row["row_count"] or 0) != len(row_bindings):
        raise RequirementEnrichmentConflictError(
            "materialized requirement rows no longer exactly cover publication"
        )
    req_code_by_tree_id = {
        item.requirement_tree_node_id: f"REQ{item.publication_order + 1:04d}"
        for item in row_bindings
    }
    for item in row_bindings:
        row = item.projection_row
        actual = connection.execute(
            """
            SELECT * FROM requirements
            WHERE id = ? AND requirement_set_id = ?
            """,
            (item.requirement_entry_id, requirement_set_id),
        ).fetchone()
        if actual is None:
            raise RequirementEnrichmentConflictError(
                "materialized requirement rows are missing a publication entry"
            )
        try:
            table_refs = json.loads(str(actual["table_refs"] or "[]"))
            content_blocks = json.loads(str(actual["content_blocks"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RequirementEnrichmentConflictError(
                "materialized requirement rows contain invalid JSON"
            ) from exc
        expected_parent_code = req_code_by_tree_id.get(
            row.parent_requirement_tree_node_id or "",
            "",
        )
        expected_trace_json = canonical_publication_decision_trace_v2_json(
            preview=preview,
            row_binding=item,
        )
        if (
            str(actual["project_id"] or "") != preview.project_id
            or str(actual["sor_document_id"] or "") != preview.document_id
            or str(actual["req_code"] or "")
            != req_code_by_tree_id[item.requirement_tree_node_id]
            or actual["source_page"] != row.page_start
            or str(actual["source_anchor"] or "")
            != f"requirement-tree:{row.source_working_tree_node_id}"
            or str(actual["requirement_text"] or "") != row.published_statement
            or str(actual["requirement_type"] or "") != row.classification
            or str(actual["priority"] or "") != "MEDIUM"
            or str(actual["status"] or "") != "DRAFT"
            or str(actual["entry_kind"] or "") != row.entry_kind.value
            or str(actual["text_relation"] or "")
            != ("atomized" if row.atomization_node_id else "distinct")
            or str(actual["parent_section_code"] or "")
            != expected_parent_code
            or str(actual["parent_section_title"] or "")
            or str(actual["heading_text"] or "")
            != (
                row.published_statement
                if row.entry_kind.value == "HEADING"
                else ""
            )
            or str(actual["raw_text"] or "") != row.original_statement
            or str(actual["display_text"] or "") != row.published_statement
            or str(actual["dedupe_version"] or "") != preview.schema_version
            or table_refs != list(row.table_refs)
            or content_blocks != list(row.content_blocks)
            or bool(int(actual["has_table_asset"] or 0)) != bool(row.table_refs)
            or str(actual["decision_reference_json"] or "")
            != expected_trace_json
        ):
            raise RequirementEnrichmentConflictError(
                "materialized requirement rows changed before finalization"
            )


def _insert_publication_binding_rows_v2(
    connection: Any,
    *,
    preview: RequirementEnrichmentPublicationBindingPreviewV2,
    binding_id: str,
    requirement_set_id: str,
    row_bindings: tuple[RequirementEnrichmentPublicationRowBindingV2, ...],
    timestamp: datetime,
) -> None:
    req_code_by_tree_id = {
        item.requirement_tree_node_id: f"REQ{item.publication_order + 1:04d}"
        for item in row_bindings
    }
    for item in row_bindings:
        row = item.projection_row
        parent_req_code = req_code_by_tree_id.get(
            row.parent_requirement_tree_node_id or "",
            "",
        )
        source_page = row.page_start
        trace_json = canonical_publication_decision_trace_v2_json(
            preview=preview,
            row_binding=item,
        )
        connection.execute(
            """
            INSERT INTO requirements (
                id, project_id, sor_document_id, requirement_set_id,
                req_code, source_page, source_anchor, requirement_text,
                requirement_type, priority, status, entry_kind,
                text_relation, parent_section_code, parent_section_title,
                heading_text, raw_text, display_text, dedupe_version,
                table_refs, content_blocks, has_table_asset,
                decision_reference_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'MEDIUM', 'DRAFT',
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.requirement_entry_id,
                preview.project_id,
                preview.document_id,
                requirement_set_id,
                req_code_by_tree_id[item.requirement_tree_node_id],
                source_page,
                f"requirement-tree:{row.source_working_tree_node_id}",
                row.published_statement,
                row.classification,
                row.entry_kind.value,
                "atomized" if row.atomization_node_id else "distinct",
                parent_req_code,
                "",
                row.published_statement if row.entry_kind.value == "HEADING" else "",
                row.original_statement,
                row.published_statement,
                preview.schema_version,
                json.dumps(list(row.table_refs), ensure_ascii=False, sort_keys=True),
                json.dumps(list(row.content_blocks), ensure_ascii=False, sort_keys=True),
                int(bool(row.table_refs)),
                trace_json,
                timestamp.isoformat(),
                timestamp.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO requirement_enrichment_publication_row_bindings (
                binding_id, publication_order, requirement_tree_node_id,
                requirement_entry_id, projection_row_sha256,
                decision_trace_sha256, payload_sha256, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id,
                item.publication_order,
                item.requirement_tree_node_id,
                item.requirement_entry_id,
                item.projection_row_sha256,
                item.decision_trace_sha256,
                item.payload_sha256,
                _canonical_model_json(item),
            ),
        )


def _insert_publication_entries(
    connection: Any,
    *,
    preview: RequirementPublicationPreview,
    requirement_set_id: str,
    timestamp: datetime,
) -> None:
    entries_by_id = {item.publication_entry_id: item for item in preview.entries}
    for publication_order, entry in enumerate(preview.entries):
        parent = (
            entries_by_id.get(entry.parent_publication_entry_id)
            if entry.parent_publication_entry_id is not None
            else None
        )
        requirement_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"reqpilotx:{preview.publication_key}:{entry.publication_entry_id}",
            )
        )
        trace = {
            "schema_version": "requirement-enrichment-trace/v1",
            "preview_id": preview.preview_id,
            "publication_key": preview.publication_key,
            "lineage_sha256": preview.lineage_sha256,
            "publication_entry_id": entry.publication_entry_id,
            "parent_publication_entry_id": entry.parent_publication_entry_id,
            "publication_order": publication_order,
            "source_order": entry.source_order,
            "entry_kind": entry.entry_kind.value,
            "atomization_node_id": entry.atomization_node_id,
            "atomic_node_id": entry.atomic_node_id,
            "review_node_id": entry.review_node_id,
            "decision_mode": entry.decision_mode.value,
            "source_spans": [
                item.model_dump(mode="json") for item in entry.source_spans
            ],
            "refinement_suggestion_id": entry.refinement_suggestion_id,
            "refinement_revision": entry.refinement_revision,
        }
        connection.execute(
            """
            INSERT INTO requirements (
                id, project_id, sor_document_id, requirement_set_id,
                req_code, source_page, source_anchor, requirement_text,
                requirement_type, priority, status, entry_kind,
                text_relation, parent_section_code, parent_section_title,
                heading_text, raw_text, display_text, dedupe_version,
                decision_reference_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'MEDIUM', 'DRAFT',
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                requirement_id,
                preview.project_id,
                preview.document_id,
                requirement_set_id,
                entry.req_code,
                f"llm-review:{entry.review_node_id}",
                entry.published_statement,
                entry.requirement_type,
                entry.entry_kind.value,
                (
                    "refined"
                    if entry.refinement_suggestion_id is not None
                    else "distinct"
                ),
                parent.req_code if parent is not None else "",
                parent.published_statement if parent is not None else "",
                (
                    entry.published_statement
                    if entry.entry_kind is RequirementPublicationEntryKind.HEADING
                    else ""
                ),
                entry.original_statement,
                entry.published_statement,
                preview.schema_version,
                json.dumps(
                    trace,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                timestamp.isoformat(),
                timestamp.isoformat(),
            ),
        )


def _validated_binding(
    project_id: str,
    document_id: str,
    session_id: str,
) -> tuple[str, str, str]:
    values = []
    for name, value in (
        ("project_id", project_id),
        ("document_id", document_id),
        ("session_id", session_id),
    ):
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > 160:
            raise ValueError(f"{name} must be a bounded non-blank string")
        values.append(normalized)
    return values[0], values[1], values[2]


def _serialize_model(model: BaseModel) -> tuple[str, str]:
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deserialize_model(
    payload_json: str,
    payload_sha256: str,
    model_type: type[_ModelT],
) -> _ModelT:
    actual = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if actual != payload_sha256:
        raise RequirementEnrichmentRepositoryError(
            "stored requirement enrichment payload hash mismatch"
        )
    try:
        return model_type.model_validate(json.loads(payload_json))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RequirementEnrichmentRepositoryError(
            "stored requirement enrichment payload is invalid"
        ) from exc


def _atomization_from_row(row: Any) -> RequirementAtomizationRun:
    run = _deserialize_model(
        str(row["payload_json"]),
        str(row["payload_sha256"]),
        RequirementAtomizationRun,
    )
    keys = set(row.keys())
    bindings = {
        "atomization_run_id": run.atomization_run_id,
        "schema_version": run.schema_version,
        "project_id": run.lineage.project_id,
        "document_id": run.lineage.document_id,
        "session_id": run.lineage.session_id,
        "review_revision": run.lineage.review_revision,
        "lineage_sha256": run.lineage.lineage_sha256,
    }
    if any(
        str(row[field]) != str(value)
        for field, value in bindings.items()
        if field in keys
    ) or (
        "atomization_revision" in keys
        and int(row["atomization_revision"]) != run.revision
    ):
        raise RequirementEnrichmentRepositoryError(
            "stored atomization run envelope binding mismatch"
        )
    return run


def _atomization_scope_receipt_from_row(row: Any) -> AtomizationScopeReceiptV1:
    receipt = _deserialize_model(
        str(row["payload_json"]),
        str(row["payload_sha256"]),
        AtomizationScopeReceiptV1,
    )
    bindings = {
        "receipt_id": receipt.receipt_id,
        "schema_version": receipt.schema_version,
        "project_id": receipt.project_id,
        "document_id": receipt.document_id,
        "session_id": receipt.session_id,
        "review_revision": receipt.review_revision,
        "lineage_sha256": receipt.lineage_sha256,
        "scope_revision": receipt.scope_revision,
        "command_id": receipt.command_id,
        "command_sha256": receipt.command_sha256,
        "atomization_run_id": receipt.atomization_run_id,
    }
    if any(str(row[field]) != str(value) for field, value in bindings.items()):
        raise RequirementEnrichmentRepositoryError(
            "stored atomization scope receipt envelope binding mismatch"
        )
    return receipt


def _atomization_scope_attempt_from_row(row: Any) -> AtomizationScopeAttemptV1:
    attempt = _deserialize_model(
        str(row["payload_json"]),
        str(row["payload_sha256"]),
        AtomizationScopeAttemptV1,
    )
    bindings = {
        "attempt_id": attempt.attempt_id,
        "schema_version": attempt.schema_version,
        "project_id": attempt.project_id,
        "document_id": attempt.document_id,
        "session_id": attempt.session_id,
        "review_revision": attempt.review_revision,
        "lineage_sha256": attempt.lineage_sha256,
        "attempt_sequence": attempt.attempt_sequence,
        "command_id": attempt.command_id,
        "command_sha256": attempt.command_sha256,
        "expected_scope_revision": attempt.expected_scope_revision,
        "status": attempt.status.value,
        "started_at_utc": attempt.started_at_utc.isoformat(),
        "updated_at_utc": attempt.updated_at_utc.isoformat(),
    }
    if any(str(row[field]) != str(value) for field, value in bindings.items()):
        raise RequirementEnrichmentRepositoryError(
            "stored atomization scope attempt envelope binding mismatch"
        )
    return attempt


def _lock_and_validate_atomization_classification_head(
    connection: Any,
    *,
    receipt: AtomizationScopeReceiptV1,
) -> None:
    from app.application.extraction.entry_classification_repository import (
        EntryClassificationSnapshotCorrupted,
        _decode_snapshot,
        entry_classification_lineage_values,
    )

    row = connection.execute(
        """
        SELECT * FROM entry_classification_snapshots
        WHERE id = ? AND project_id = ? AND sor_document_id = ?
        LIMIT 1
        """,
        (
            receipt.entry_classification_snapshot_id,
            receipt.project_id,
            receipt.document_id,
        ),
    ).fetchone()
    if row is None:
        raise RequirementEnrichmentConflictError(
            "entry classification head is unavailable"
        )
    try:
        snapshot = _decode_snapshot(row)
    except EntryClassificationSnapshotCorrupted as exc:
        raise RequirementEnrichmentRepositoryError(
            "entry classification head is corrupted"
        ) from exc
    if (
        snapshot.revision != receipt.entry_classification_revision
        or snapshot.payload_sha256 != receipt.entry_classification_sha256
    ):
        raise RequirementEnrichmentConflictError(
            "entry classification receipt binding has changed"
        )
    lineage_values = entry_classification_lineage_values(snapshot)
    if type(connection).__module__.endswith("postgres_compat"):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            (
                "\x1f".join(
                    ("entry-classification-snapshot/v1", *lineage_values)
                ),
            ),
        )
    head = connection.execute(
        """
        SELECT * FROM entry_classification_snapshots
        WHERE project_id = ? AND sor_document_id = ? AND source_sha256 = ?
          AND working_tree_id = ? AND working_tree_sha256 = ?
          AND effective_structure_id = ? AND semantic_snapshot_id = ?
          AND policy_version = ?
        ORDER BY revision DESC
        LIMIT 1
        """,
        lineage_values,
    ).fetchone()
    if head is None:
        raise RequirementEnrichmentConflictError(
            "entry classification head is unavailable"
        )
    try:
        current = _decode_snapshot(head)
    except EntryClassificationSnapshotCorrupted as exc:
        raise RequirementEnrichmentRepositoryError(
            "entry classification head is corrupted"
        ) from exc
    if (
        current.snapshot_id != receipt.entry_classification_snapshot_id
        or current.revision != receipt.entry_classification_revision
        or current.payload_sha256 != receipt.entry_classification_sha256
    ):
        raise RequirementEnrichmentConflictError(
            "entry classification head has changed"
        )


def _lock_atomization_scope_lineage_in_conn(
    connection: Any,
    *,
    lineage_sha256: str,
) -> None:
    if type(connection).__module__.endswith("postgres_compat"):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            (
                "\x1f".join(
                    (
                        "atomization-scope-attempt/v1",
                        lineage_sha256,
                    )
                ),
            ),
        )


def _stale_interrupted_atomization_scope_attempt(
    connection: Any,
    *,
    attempt: AtomizationScopeAttemptV1,
    observed_at_utc: datetime,
) -> AtomizationScopeAttemptV1:
    valid_checkpoint_ids: set[str] = set()
    checkpoint_rows = connection.execute(
        """
        SELECT * FROM requirement_atomization_checkpoints
        WHERE lineage_sha256 = ?
        """,
        (attempt.lineage_sha256,),
    ).fetchall()
    for row in checkpoint_rows:
        try:
            request_sha256 = str(row["request_sha256"])
            _validate_atomization_checkpoint_row(
                row,
                project_id=attempt.project_id,
                document_id=attempt.document_id,
                session_id=attempt.session_id,
                review_revision=attempt.review_revision,
                request_sha256=request_sha256,
            )
            _deserialize_model(
                str(row["payload_json"]),
                str(row["payload_sha256"]),
                RequirementAtomizationProviderResult,
            )
        except (RequirementEnrichmentRepositoryError, TypeError, ValueError):
            continue
        valid_checkpoint_ids.add(str(row["review_node_id"]))
    completed = tuple(
        review_node_id
        for review_node_id in attempt.executed_review_node_ids
        if review_node_id in valid_checkpoint_ids
    )
    fields = attempt.model_dump(mode="python")
    fields.update(
        {
            "payload_sha256": "0" * 64,
            "status": AtomizationScopeAttemptStatus.INTERRUPTED,
            "completed_review_node_ids": completed,
            "failed_review_node_ids": (),
            "checkpoint_created_review_node_ids": (),
            "checkpoint_reused_review_node_ids": (),
            "checkpoint_invalidated_review_node_ids": (),
            "provider_call_count": 0,
            "retryable": True,
            "checkpoint_resumable": bool(completed),
            "error_code": "PROCESS_INTERRUPTED",
            "updated_at_utc": observed_at_utc,
        }
    )
    draft = AtomizationScopeAttemptV1.model_construct(**fields)
    fields["payload_sha256"] = canonical_atomization_scope_attempt_sha256(draft)
    return AtomizationScopeAttemptV1.model_validate(fields)


def _heartbeat_atomization_scope_attempt(
    attempt: AtomizationScopeAttemptV1,
    *,
    observed_at_utc: datetime,
) -> AtomizationScopeAttemptV1:
    fields = attempt.model_dump(mode="python")
    fields.update(
        {
            "payload_sha256": "0" * 64,
            "updated_at_utc": observed_at_utc,
        }
    )
    draft = AtomizationScopeAttemptV1.model_construct(**fields)
    fields["payload_sha256"] = canonical_atomization_scope_attempt_sha256(draft)
    return AtomizationScopeAttemptV1.model_validate(fields)


def _assert_current_scope_attempt_owner_in_connection(
    connection: Any,
    *,
    attempt_id: str,
    lineage_sha256: str,
    expected_scope_revision: int | None = None,
) -> AtomizationScopeAttemptV1:
    owner_row = connection.execute(
        """
        SELECT * FROM requirement_atomization_scope_attempts
        WHERE attempt_id = ? AND lineage_sha256 = ?
        LIMIT 1
        """,
        (attempt_id, lineage_sha256),
    ).fetchone()
    if owner_row is None:
        raise RequirementEnrichmentConflictError(
            "atomization scope attempt lease was lost"
        )
    owner = _atomization_scope_attempt_from_row(owner_row)
    if owner.status is not AtomizationScopeAttemptStatus.RUNNING:
        raise RequirementEnrichmentConflictError(
            "atomization scope attempt lease was lost"
        )
    expected_revision = (
        owner.expected_scope_revision
        if expected_scope_revision is None
        else expected_scope_revision
    )
    if owner.expected_scope_revision != expected_revision:
        raise RequirementEnrichmentConflictError(
            "atomization scope attempt lease was lost"
        )
    rows = connection.execute(
        """
        SELECT * FROM requirement_atomization_scope_attempts
        WHERE lineage_sha256 = ? AND expected_scope_revision = ? AND status = ?
        ORDER BY attempt_sequence
        """,
        (
            lineage_sha256,
            expected_revision,
            AtomizationScopeAttemptStatus.RUNNING.value,
        ),
    ).fetchall()
    active_attempts = tuple(
        _atomization_scope_attempt_from_row(row) for row in rows
    )
    matching = tuple(
        attempt for attempt in active_attempts if attempt.attempt_id == attempt_id
    )
    if len(active_attempts) != 1 or len(matching) != 1:
        raise RequirementEnrichmentConflictError(
            "atomization scope attempt lease was lost"
        )
    owner = matching[0]
    head = connection.execute(
        """
        SELECT scope_revision FROM requirement_atomization_scope_receipts
        WHERE lineage_sha256 = ?
        ORDER BY scope_revision DESC
        LIMIT 1
        """,
        (lineage_sha256,),
    ).fetchone()
    current_scope_revision = int(head["scope_revision"]) if head is not None else 0
    if current_scope_revision != expected_revision:
        raise RequirementEnrichmentConflictError(
            "atomization scope attempt lease was lost"
        )
    return owner


def _assert_scope_attempt_checkpoint_binding(
    attempt: AtomizationScopeAttemptV1,
    *,
    project_id: str,
    document_id: str,
    session_id: str,
    review_revision: int,
) -> None:
    if (
        attempt.project_id != project_id
        or attempt.document_id != document_id
        or attempt.session_id != session_id
        or attempt.review_revision != review_revision
    ):
        raise RequirementEnrichmentConflictError(
            "atomization scope checkpoint owner binding has changed"
        )


def _update_atomization_scope_attempt_in_connection(
    connection: Any,
    *,
    attempt: AtomizationScopeAttemptV1,
    expected_status: AtomizationScopeAttemptStatus,
) -> None:
    row = connection.execute(
        """
        SELECT * FROM requirement_atomization_scope_attempts
        WHERE attempt_id = ?
        LIMIT 1
        """,
        (attempt.attempt_id,),
    ).fetchone()
    if row is None:
        raise RequirementEnrichmentConflictError(
            "atomization scope attempt is unavailable"
        )
    current = _atomization_scope_attempt_from_row(row)
    if current.status is not expected_status:
        raise RequirementEnrichmentConflictError(
            "atomization scope attempt status has changed"
        )
    immutable_bindings = (
        "attempt_id",
        "project_id",
        "document_id",
        "session_id",
        "review_revision",
        "lineage_sha256",
        "attempt_sequence",
        "command_id",
        "command_sha256",
        "expected_scope_revision",
        "operation",
        "scope",
        "requested_review_node_ids",
        "executed_review_node_ids",
        "skipped_targets",
        "cumulative_review_node_ids",
        "started_at_utc",
    )
    if any(getattr(current, field) != getattr(attempt, field) for field in immutable_bindings):
        raise RequirementEnrichmentConflictError(
            "atomization scope attempt immutable binding has changed"
        )
    payload_json, payload_sha256 = _serialize_model(attempt)
    cursor = connection.execute(
        """
        UPDATE requirement_atomization_scope_attempts
        SET status = ?, payload_sha256 = ?, payload_json = ?, updated_at_utc = ?
        WHERE attempt_id = ? AND status = ?
        """,
        (
            attempt.status.value,
            payload_sha256,
            payload_json,
            attempt.updated_at_utc.isoformat(),
            attempt.attempt_id,
            expected_status.value,
        ),
    )
    if cursor.rowcount != 1:
        raise RequirementEnrichmentConflictError(
            "atomization scope attempt status has changed"
        )


def _validate_atomization_checkpoint_binding(
    *,
    review_revision: int,
    lineage_sha256: str,
    request: RequirementAtomizationProviderRequest,
) -> None:
    if isinstance(review_revision, bool) or review_revision < 0:
        raise ValueError("review_revision must be a non-negative integer")
    if not _SHA256_PATTERN.fullmatch(lineage_sha256):
        raise ValueError("lineage_sha256 is invalid")
    if not isinstance(request, RequirementAtomizationProviderRequest):
        raise TypeError("atomization checkpoint request is invalid")
    if request.lineage_sha256 != lineage_sha256:
        raise ValueError("atomization checkpoint lineage does not match request")


def _validate_atomization_checkpoint_row(
    row: Any,
    *,
    project_id: str,
    document_id: str,
    session_id: str,
    review_revision: int,
    request_sha256: str,
) -> None:
    if (
        str(row["project_id"]) != project_id
        or str(row["document_id"]) != document_id
        or str(row["session_id"]) != session_id
        or int(row["review_revision"]) != review_revision
        or str(row["request_sha256"]) != request_sha256
    ):
        raise RequirementEnrichmentConflictError(
            "atomization checkpoint binding conflicts with the current request"
        )


def _refinement_from_row(
    row: Any,
) -> RequirementRefinementSuggestion:
    suggestion = _deserialize_model(
        str(row["payload_json"]),
        str(row["payload_sha256"]),
        RequirementRefinementSuggestion,
    )
    if (
        int(row["revision"]) != suggestion.revision
        or str(row["status"]) != suggestion.status.value
    ):
        raise RequirementEnrichmentRepositoryError(
            "stored refinement state does not match its payload"
        )
    return suggestion


def _preview_from_row(row: Any) -> RequirementPublicationPreview:
    return _deserialize_model(
        str(row["payload_json"]),
        str(row["payload_sha256"]),
        RequirementPublicationPreview,
    )


def _same_initial_refinement(
    stored: RequirementRefinementSuggestion,
    candidate: RequirementRefinementSuggestion,
) -> bool:
    ignored = {"created_at_utc", "updated_at_utc"}
    return {
        key: value
        for key, value in stored.model_dump(mode="json").items()
        if key not in ignored
    } == {
        key: value
        for key, value in candidate.model_dump(mode="json").items()
        if key not in ignored
    }


def _same_publication_preview(
    stored: RequirementPublicationPreview,
    candidate: RequirementPublicationPreview,
) -> bool:
    return stored.model_dump(
        mode="json",
        exclude={"created_at_utc"},
    ) == candidate.model_dump(
        mode="json",
        exclude={"created_at_utc"},
    )


def _validate_refinement_lineage(
    current: RequirementRefinementSuggestion,
    updated: RequirementRefinementSuggestion,
) -> None:
    immutable_fields = (
        "suggestion_id",
        "project_id",
        "document_id",
        "session_id",
        "atomization_run_id",
        "atomization_revision",
        "atomic_node_id",
        "original_statement",
        "provider_id",
        "provider_version",
        "model_id",
        "prompt_version",
        "provider_run_id",
        "lineage_sha256",
        "created_at_utc",
    )
    if any(
        getattr(current, field) != getattr(updated, field) for field in immutable_fields
    ):
        raise ValueError("refinement update changed immutable source/provider lineage")


__all__ = [
    "_ATOMIZATION_SCOPE_ATTEMPT_SCHEMA",
    "SqlRequirementEnrichmentRepository",
    "_ATOMIZATION_CHECKPOINT_SCHEMA",
    "_ATOMIZATION_SCHEMA",
    "_ATOMIZATION_SCOPE_SCHEMA",
    "_PREVIEW_INDEX",
    "_PUBLICATION_PREVIEW_SCHEMA",
    "_PUBLICATION_SCHEMA",
    "_REFINEMENT_EVENT_SCHEMA",
    "_REFINEMENT_INDEX",
    "_REFINEMENT_SCHEMA",
    "_RUN_INDEX",
    "_SCOPE_ATTEMPT_INDEX",
    "_SCOPE_INDEX",
]
