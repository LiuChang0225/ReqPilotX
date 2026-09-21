"""Shared SQL persistence for source-grounded LLM review sessions."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, TypeVar

from pydantic import BaseModel

from reqpilotx_server.domain.extraction.llm_requirement_review import (
    LLM_REQUIREMENT_PREDICTION_SCHEMA_VERSION,
    LLM_REQUIREMENT_REVIEW_SCHEMA_VERSION,
    LlmRequirementModelPrediction,
    LlmRequirementReviewDelta,
    LlmRequirementReviewStatus,
    LlmRequirementReviewedResult,
    LlmRequirementReviewSession,
)
from reqpilotx_server.ports.llm_requirement_review_repository import (
    LlmRequirementReviewConflictError,
    LlmRequirementReviewIncompatibleError,
    LlmRequirementReviewRepositoryError,
)


_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_requirement_review_sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    prediction_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    revision INTEGER NOT NULL CHECK(revision >= 0),
    status TEXT NOT NULL,
    model_prediction_sha256 TEXT NOT NULL CHECK(length(model_prediction_sha256) = 64),
    model_prediction_json TEXT NOT NULL,
    reviewed_result_sha256 TEXT NOT NULL CHECK(length(reviewed_result_sha256) = 64),
    reviewed_result_json TEXT NOT NULL,
    delta_sha256 TEXT NOT NULL CHECK(length(delta_sha256) = 64),
    delta_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(project_id, document_id, prediction_id)
)
"""

_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_requirement_review_events (
    session_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    status TEXT NOT NULL,
    reviewer_label TEXT NOT NULL,
    reviewed_result_sha256 TEXT NOT NULL CHECK(length(reviewed_result_sha256) = 64),
    reviewed_result_json TEXT NOT NULL,
    delta_sha256 TEXT NOT NULL CHECK(length(delta_sha256) = 64),
    delta_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(session_id, revision),
    FOREIGN KEY(session_id) REFERENCES llm_requirement_review_sessions(session_id)
        ON DELETE CASCADE
)
"""

_CONFIRMATION_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_requirement_review_confirmation_events (
    session_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    review_node_id TEXT NOT NULL,
    confirmed INTEGER NOT NULL CHECK(confirmed IN (0, 1)),
    status TEXT NOT NULL,
    previous_reviewed_result_sha256 TEXT NOT NULL
        CHECK(length(previous_reviewed_result_sha256) = 64),
    reviewed_result_sha256 TEXT NOT NULL
        CHECK(length(reviewed_result_sha256) = 64),
    previous_event_sha256 TEXT NOT NULL
        CHECK(length(previous_event_sha256) = 64),
    event_sha256 TEXT NOT NULL CHECK(length(event_sha256) = 64),
    created_at_utc TEXT NOT NULL,
    PRIMARY KEY(session_id, revision),
    FOREIGN KEY(session_id) REFERENCES llm_requirement_review_sessions(session_id)
        ON DELETE CASCADE
)
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_llm_requirement_review_document
ON llm_requirement_review_sessions(project_id, document_id, updated_at_utc DESC)
"""

_CONFIRMATION_EVENT_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_llm_requirement_confirmation_event_session
ON llm_requirement_review_confirmation_events(session_id, revision DESC)
"""

_EMPTY_EVENT_SHA256 = "0" * 64
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class SqlLlmRequirementReviewRepository:
    """Store immutable model output plus append-only reviewer revisions.

    The source-grounded contracts include reviewer-visible customer text and
    therefore belong to the same deletion boundary as their SOR document.
    """

    def create_or_get(
        self,
        *,
        project_id: str,
        document_id: str,
        prediction: LlmRequirementModelPrediction,
        reviewed_result: LlmRequirementReviewedResult,
        delta: LlmRequirementReviewDelta,
    ) -> LlmRequirementReviewSession:
        project_id, document_id = _validated_binding(project_id, document_id)
        _validate_payload_binding(prediction, reviewed_result, delta)
        if reviewed_result.revision != 0:
            raise ValueError("a new review session must start at revision zero")
        session_id = f"llm-review-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        prediction_json, prediction_hash = _serialize_model(prediction)
        reviewed_json, reviewed_hash = _serialize_model(reviewed_result)
        delta_json, delta_hash = _serialize_model(delta)

        with self._connect() as connection:
            with self._transaction(connection):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO llm_requirement_review_sessions (
                        session_id, project_id, document_id, prediction_id,
                        source_sha256, revision, status,
                        model_prediction_sha256, model_prediction_json,
                        reviewed_result_sha256, reviewed_result_json,
                        delta_sha256, delta_json, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        project_id,
                        document_id,
                        prediction.prediction_id,
                        prediction.source_sha256,
                        reviewed_result.revision,
                        reviewed_result.status.value,
                        prediction_hash,
                        prediction_json,
                        reviewed_hash,
                        reviewed_json,
                        delta_hash,
                        delta_json,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                if cursor.rowcount == 1:
                    connection.execute(
                        """
                        INSERT INTO llm_requirement_review_events (
                            session_id, revision, status, reviewer_label,
                            reviewed_result_sha256, reviewed_result_json,
                            delta_sha256, delta_json, created_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            reviewed_result.revision,
                            reviewed_result.status.value,
                            reviewed_result.reviewer_label,
                            reviewed_hash,
                            reviewed_json,
                            delta_hash,
                            delta_json,
                            now.isoformat(),
                        ),
                    )
                else:
                    row = connection.execute(
                        """
                        SELECT * FROM llm_requirement_review_sessions
                        WHERE project_id = ? AND document_id = ? AND prediction_id = ?
                        """,
                        (project_id, document_id, prediction.prediction_id),
                    ).fetchone()
                    if row is None:
                        raise LlmRequirementReviewRepositoryError(
                            "review session insert did not produce a readable row"
                        )
                    session = _session_from_row(row)
                    if session.prediction != prediction:
                        raise LlmRequirementReviewConflictError(
                            "prediction_id already contains different model output"
                        )
                    return session

        return LlmRequirementReviewSession(
            session_id=session_id,
            project_id=project_id,
            document_id=document_id,
            prediction=prediction,
            reviewed_result=reviewed_result,
            delta=delta,
            created_at_utc=now,
            updated_at_utc=now,
        )

    def get(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
    ) -> LlmRequirementReviewSession | None:
        project_id, document_id = _validated_binding(project_id, document_id)
        session_id = _validated_session_id(session_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM llm_requirement_review_sessions
                WHERE session_id = ? AND project_id = ? AND document_id = ?
                """,
                (session_id, project_id, document_id),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def get_latest(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
    ) -> LlmRequirementReviewSession | None:
        project_id, document_id = _validated_binding(project_id, document_id)
        source_sha256 = _validated_source_sha256(source_sha256)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM llm_requirement_review_sessions
                WHERE project_id = ? AND document_id = ? AND source_sha256 = ?
                ORDER BY updated_at_utc DESC, created_at_utc DESC
                LIMIT 1
                """,
                (project_id, document_id, source_sha256),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def get_for_prediction(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        prediction_id: str,
    ) -> LlmRequirementReviewSession | None:
        project_id, document_id = _validated_binding(project_id, document_id)
        source_sha256 = _validated_source_sha256(source_sha256)
        prediction_id = _validated_prediction_id(prediction_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM llm_requirement_review_sessions
                WHERE project_id = ? AND document_id = ?
                    AND source_sha256 = ? AND prediction_id = ?
                """,
                (project_id, document_id, source_sha256, prediction_id),
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def update(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        expected_revision: int,
        reviewed_result: LlmRequirementReviewedResult,
        delta: LlmRequirementReviewDelta,
    ) -> LlmRequirementReviewSession:
        project_id, document_id = _validated_binding(project_id, document_id)
        session_id = _validated_session_id(session_id)
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if reviewed_result.revision != expected_revision + 1:
            raise ValueError("reviewed result must advance exactly one revision")
        if delta.revision != reviewed_result.revision:
            raise ValueError("review delta revision must match the reviewed result")
        reviewed_json, reviewed_hash = _serialize_model(reviewed_result)
        delta_json, delta_hash = _serialize_model(delta)
        now = datetime.now(timezone.utc)

        with self._connect() as connection:
            with self._transaction(connection):
                row = connection.execute(
                    """
                    SELECT * FROM llm_requirement_review_sessions
                    WHERE session_id = ? AND project_id = ? AND document_id = ?
                    """,
                    (session_id, project_id, document_id),
                ).fetchone()
                if row is None:
                    raise LlmRequirementReviewRepositoryError(
                        "review session was not found"
                    )
                current = _session_from_row(row)
                if current.reviewed_result.revision != expected_revision:
                    raise LlmRequirementReviewConflictError(
                        "review session revision has changed"
                    )
                _validate_payload_binding(current.prediction, reviewed_result, delta)
                cursor = connection.execute(
                    """
                    UPDATE llm_requirement_review_sessions
                    SET revision = ?, status = ?,
                        reviewed_result_sha256 = ?, reviewed_result_json = ?,
                        delta_sha256 = ?, delta_json = ?, updated_at_utc = ?
                    WHERE session_id = ? AND project_id = ? AND document_id = ?
                        AND revision = ?
                    """,
                    (
                        reviewed_result.revision,
                        reviewed_result.status.value,
                        reviewed_hash,
                        reviewed_json,
                        delta_hash,
                        delta_json,
                        now.isoformat(),
                        session_id,
                        project_id,
                        document_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LlmRequirementReviewConflictError(
                        "review session revision has changed"
                    )
                connection.execute(
                    """
                    INSERT INTO llm_requirement_review_events (
                        session_id, revision, status, reviewer_label,
                        reviewed_result_sha256, reviewed_result_json,
                        delta_sha256, delta_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        reviewed_result.revision,
                        reviewed_result.status.value,
                        reviewed_result.reviewer_label,
                        reviewed_hash,
                        reviewed_json,
                        delta_hash,
                        delta_json,
                        now.isoformat(),
                    ),
                )

        return LlmRequirementReviewSession(
            session_id=session_id,
            project_id=project_id,
            document_id=document_id,
            prediction=current.prediction,
            reviewed_result=reviewed_result,
            delta=delta,
            created_at_utc=current.created_at_utc,
            updated_at_utc=now,
        )

    def update_confirmation(
        self,
        *,
        project_id: str,
        document_id: str,
        session_id: str,
        expected_revision: int,
        review_node_id: str,
        confirmed: bool,
        reviewed_result: LlmRequirementReviewedResult,
        delta: LlmRequirementReviewDelta,
    ) -> LlmRequirementReviewSession:
        """Advance one confirmation without duplicating the full event snapshot."""

        project_id, document_id = _validated_binding(project_id, document_id)
        session_id = _validated_session_id(session_id)
        review_node_id = _validated_review_node_id(review_node_id)
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if not isinstance(confirmed, bool):
            raise ValueError("confirmed must be a boolean")
        if reviewed_result.revision != expected_revision + 1:
            raise ValueError("reviewed result must advance exactly one revision")
        if delta.revision != reviewed_result.revision:
            raise ValueError("review delta revision must match the reviewed result")
        reviewed_json, reviewed_hash = _serialize_model(reviewed_result)
        delta_json, delta_hash = _serialize_model(delta)
        now = datetime.now(timezone.utc)

        with self._connect() as connection:
            with self._transaction(connection):
                row = connection.execute(
                    """
                    SELECT * FROM llm_requirement_review_sessions
                    WHERE session_id = ? AND project_id = ? AND document_id = ?
                    """,
                    (session_id, project_id, document_id),
                ).fetchone()
                if row is None:
                    raise LlmRequirementReviewRepositoryError(
                        "review session was not found"
                    )
                current = _session_from_row(row)
                if current.reviewed_result.revision != expected_revision:
                    raise LlmRequirementReviewConflictError(
                        "review session revision has changed"
                    )
                _validate_confirmation_transition(
                    current=current,
                    review_node_id=review_node_id,
                    confirmed=confirmed,
                    reviewed_result=reviewed_result,
                    delta=delta,
                )
                previous_event_sha256 = _latest_confirmation_event_sha256(
                    connection,
                    session_id=session_id,
                )
                previous_reviewed_hash = str(row["reviewed_result_sha256"])
                event_sha256 = _confirmation_event_sha256(
                    session_id=session_id,
                    revision=reviewed_result.revision,
                    review_node_id=review_node_id,
                    confirmed=confirmed,
                    status=reviewed_result.status.value,
                    previous_reviewed_result_sha256=previous_reviewed_hash,
                    reviewed_result_sha256=reviewed_hash,
                    previous_event_sha256=previous_event_sha256,
                )
                cursor = connection.execute(
                    """
                    UPDATE llm_requirement_review_sessions
                    SET revision = ?, status = ?,
                        reviewed_result_sha256 = ?, reviewed_result_json = ?,
                        delta_sha256 = ?, delta_json = ?, updated_at_utc = ?
                    WHERE session_id = ? AND project_id = ? AND document_id = ?
                        AND revision = ?
                    """,
                    (
                        reviewed_result.revision,
                        reviewed_result.status.value,
                        reviewed_hash,
                        reviewed_json,
                        delta_hash,
                        delta_json,
                        now.isoformat(),
                        session_id,
                        project_id,
                        document_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LlmRequirementReviewConflictError(
                        "review session revision has changed"
                    )
                connection.execute(
                    """
                    INSERT INTO llm_requirement_review_confirmation_events (
                        session_id, revision, review_node_id, confirmed, status,
                        previous_reviewed_result_sha256, reviewed_result_sha256,
                        previous_event_sha256, event_sha256, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        reviewed_result.revision,
                        review_node_id,
                        int(confirmed),
                        reviewed_result.status.value,
                        previous_reviewed_hash,
                        reviewed_hash,
                        previous_event_sha256,
                        event_sha256,
                        now.isoformat(),
                    ),
                )

        return LlmRequirementReviewSession(
            session_id=session_id,
            project_id=project_id,
            document_id=document_id,
            prediction=current.prediction,
            reviewed_result=reviewed_result,
            delta=delta,
            created_at_utc=current.created_at_utc,
            updated_at_utc=now,
        )

    def purge_document(
        self,
        *,
        project_id: str,
        document_id: str,
    ) -> int:
        project_id, document_id = _validated_binding(project_id, document_id)
        with self._connect() as connection:
            with self._transaction(connection):
                rows = connection.execute(
                    """
                    SELECT session_id FROM llm_requirement_review_sessions
                    WHERE project_id = ? AND document_id = ?
                    """,
                    (project_id, document_id),
                ).fetchall()
                session_ids = tuple(str(row["session_id"]) for row in rows)
                if session_ids:
                    connection.execute(
                        """
                        DELETE FROM llm_requirement_review_confirmation_events
                        WHERE session_id IN (
                            SELECT session_id
                            FROM llm_requirement_review_sessions
                            WHERE project_id = ? AND document_id = ?
                        )
                        """,
                        (project_id, document_id),
                    )
                    connection.execute(
                        """
                        DELETE FROM llm_requirement_review_events
                        WHERE session_id IN (
                            SELECT session_id
                            FROM llm_requirement_review_sessions
                            WHERE project_id = ? AND document_id = ?
                        )
                        """,
                        (project_id, document_id),
                    )
                connection.execute(
                    """
                    DELETE FROM llm_requirement_review_sessions
                    WHERE project_id = ? AND document_id = ?
                    """,
                    (project_id, document_id),
                )
        return len(session_ids)

    @contextmanager
    def _transaction(self, connection: Any) -> Iterator[None]:
        if connection.in_transaction:
            name = f"llm_review_repo_{id(self):x}"
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


def _validate_confirmation_transition(
    *,
    current: LlmRequirementReviewSession,
    review_node_id: str,
    confirmed: bool,
    reviewed_result: LlmRequirementReviewedResult,
    delta: LlmRequirementReviewDelta,
) -> None:
    _validate_payload_binding(current.prediction, reviewed_result, delta)
    if reviewed_result.nodes != current.reviewed_result.nodes:
        raise ValueError("a confirmation command cannot change reviewed nodes")
    if reviewed_result.reviewer_label != current.reviewed_result.reviewer_label:
        raise ValueError("a confirmation command cannot change the reviewer label")
    if reviewed_result.status is not LlmRequirementReviewStatus.DRAFT:
        raise ValueError("a confirmation command must leave the review in DRAFT")

    node_ids = tuple(node.review_node_id for node in current.reviewed_result.nodes)
    if review_node_id not in set(node_ids):
        raise ValueError("confirmation review_node_id does not belong to the session")
    expected_confirmed = set(current.reviewed_result.confirmed_review_node_ids)
    if confirmed:
        if review_node_id in expected_confirmed:
            raise ValueError("confirmation command must change exactly one node")
        expected_confirmed.add(review_node_id)
    else:
        if review_node_id not in expected_confirmed:
            raise ValueError("confirmation command must change exactly one node")
        expected_confirmed.remove(review_node_id)
    ordered_expected = tuple(
        node_id for node_id in node_ids if node_id in expected_confirmed
    )
    if reviewed_result.confirmed_review_node_ids != ordered_expected:
        raise ValueError("confirmation command must change only the requested node")


def _latest_confirmation_event_sha256(
    connection: Any,
    *,
    session_id: str,
) -> str:
    row = connection.execute(
        """
        SELECT * FROM llm_requirement_review_confirmation_events
        WHERE session_id = ?
        ORDER BY revision DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return _EMPTY_EVENT_SHA256
    expected = _confirmation_event_sha256(
        session_id=str(row["session_id"]),
        revision=int(row["revision"]),
        review_node_id=str(row["review_node_id"]),
        confirmed=bool(row["confirmed"]),
        status=str(row["status"]),
        previous_reviewed_result_sha256=str(row["previous_reviewed_result_sha256"]),
        reviewed_result_sha256=str(row["reviewed_result_sha256"]),
        previous_event_sha256=str(row["previous_event_sha256"]),
    )
    if str(row["event_sha256"]) != expected:
        raise LlmRequirementReviewRepositoryError(
            "stored confirmation event hash mismatch"
        )
    return expected


def _confirmation_event_sha256(
    *,
    session_id: str,
    revision: int,
    review_node_id: str,
    confirmed: bool,
    status: str,
    previous_reviewed_result_sha256: str,
    reviewed_result_sha256: str,
    previous_event_sha256: str,
) -> str:
    payload = json.dumps(
        {
            "confirmed": confirmed,
            "previous_event_sha256": previous_event_sha256,
            "previous_reviewed_result_sha256": previous_reviewed_result_sha256,
            "review_node_id": review_node_id,
            "reviewed_result_sha256": reviewed_result_sha256,
            "revision": revision,
            "session_id": session_id,
            "status": status,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validated_binding(project_id: str, document_id: str) -> tuple[str, str]:
    values = []
    for name, value in (("project_id", project_id), ("document_id", document_id)):
        if not isinstance(value, str) or not value.strip() or len(value) > 160:
            raise ValueError(f"{name} must be a non-blank bounded string")
        values.append(value.strip())
    return values[0], values[1]


def _validated_review_node_id(review_node_id: str) -> str:
    if (
        not isinstance(review_node_id, str)
        or not review_node_id.strip()
        or len(review_node_id) > 200
    ):
        raise ValueError("review_node_id must be a non-blank bounded string")
    return review_node_id.strip()


def _validated_session_id(session_id: str) -> str:
    if (
        not isinstance(session_id, str)
        or not session_id.startswith("llm-review-")
        or len(session_id) != len("llm-review-") + 32
        or any(char not in "0123456789abcdef" for char in session_id[11:])
    ):
        raise ValueError("session_id is invalid")
    return session_id


def _validated_source_sha256(source_sha256: str) -> str:
    value = str(source_sha256 or "").strip()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("source_sha256 is invalid")
    return value


def _validated_prediction_id(prediction_id: str) -> str:
    value = str(prediction_id or "").strip()
    prefix = "llm-prediction-"
    suffix = value[len(prefix) :] if value.startswith(prefix) else ""
    if len(suffix) != 32 or any(char not in "0123456789abcdef" for char in suffix):
        raise ValueError("prediction_id is invalid")
    return value


def _validate_payload_binding(
    prediction: LlmRequirementModelPrediction,
    reviewed_result: LlmRequirementReviewedResult,
    delta: LlmRequirementReviewDelta,
) -> None:
    if reviewed_result.prediction_id != prediction.prediction_id:
        raise ValueError("reviewed result belongs to a different prediction")
    if delta.prediction_id != prediction.prediction_id:
        raise ValueError("review delta belongs to a different prediction")
    if delta.revision != reviewed_result.revision:
        raise ValueError("review delta revision must match the reviewed result")


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
        raise LlmRequirementReviewRepositoryError(
            "stored LLM review payload hash mismatch"
        )
    try:
        payload = json.loads(payload_json)
        if isinstance(payload, dict):
            stored_schema_version = str(payload.get("schema_version") or "")
            current_schema_version = (
                LLM_REQUIREMENT_PREDICTION_SCHEMA_VERSION
                if model_type is LlmRequirementModelPrediction
                else LLM_REQUIREMENT_REVIEW_SCHEMA_VERSION
                if model_type is LlmRequirementReviewedResult
                else ""
            )
            if (
                current_schema_version
                and stored_schema_version
                and stored_schema_version != current_schema_version
            ):
                raise LlmRequirementReviewIncompatibleError(
                    stored_schema_version=stored_schema_version,
                    current_schema_version=current_schema_version,
                )
        return model_type.model_validate(payload)
    except LlmRequirementReviewIncompatibleError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise LlmRequirementReviewRepositoryError(
            "stored LLM review payload is invalid"
        ) from exc


def _session_from_row(row: Any) -> LlmRequirementReviewSession:
    prediction = _deserialize_model(
        str(row["model_prediction_json"]),
        str(row["model_prediction_sha256"]),
        LlmRequirementModelPrediction,
    )
    reviewed_result = _deserialize_model(
        str(row["reviewed_result_json"]),
        str(row["reviewed_result_sha256"]),
        LlmRequirementReviewedResult,
    )
    delta = _deserialize_model(
        str(row["delta_json"]),
        str(row["delta_sha256"]),
        LlmRequirementReviewDelta,
    )
    if str(row["prediction_id"]) != prediction.prediction_id:
        raise LlmRequirementReviewRepositoryError(
            "stored review prediction identity mismatch"
        )
    if int(row["revision"]) != reviewed_result.revision:
        raise LlmRequirementReviewRepositoryError("stored review revision mismatch")
    if str(row["status"]) != reviewed_result.status.value:
        raise LlmRequirementReviewRepositoryError("stored review status mismatch")
    return LlmRequirementReviewSession(
        session_id=str(row["session_id"]),
        project_id=str(row["project_id"]),
        document_id=str(row["document_id"]),
        prediction=prediction,
        reviewed_result=reviewed_result,
        delta=delta,
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
    )


__all__ = [
    "SqlLlmRequirementReviewRepository",
    "_CONFIRMATION_EVENT_INDEX_SCHEMA",
    "_CONFIRMATION_EVENT_SCHEMA",
    "_EVENT_SCHEMA",
    "_INDEX_SCHEMA",
    "_SESSION_SCHEMA",
]
