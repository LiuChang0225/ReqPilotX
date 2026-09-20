"""SQL repository for append-only neutral visual capture sidecars."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app import db
from app.domain.source_visual_capture import (
    VisualCaptureGenerationV1,
    VisualPageCaptureV1,
)
from app.ports.source_visual_capture_repository import (
    SourceVisualCaptureRepositoryConflict,
    SourceVisualCaptureRepositoryCorrupt,
    VisualPageFailureRecord,
)


class SqlSourceVisualCaptureRepository:
    def append_generation(
        self, generation: VisualCaptureGenerationV1
    ) -> VisualCaptureGenerationV1:
        payload = _canonical_model_json(generation)
        with db.get_conn() as connection:
            _lock_current_document(
                connection,
                project_id=generation.project_id,
                sor_document_id=generation.sor_document_id,
                source_sha256=generation.source_sha256,
            )
            current = connection.execute(
                """
                SELECT b.capture_id
                FROM source_capture_bindings AS b
                JOIN sor_documents AS d
                  ON d.id = b.sor_document_id
                 AND d.project_id = b.project_id
                 AND d.source_sha256 = b.source_sha256
                WHERE b.project_id = ? AND b.sor_document_id = ?
                  AND b.source_sha256 = ?
                """,
                (
                    generation.project_id,
                    generation.sor_document_id,
                    generation.source_sha256,
                ),
            ).fetchone()
            if current is None or str(current["capture_id"]) != generation.base_capture_id:
                raise SourceVisualCaptureRepositoryConflict(
                    "visual generation does not bind the current base capture"
                )
            candidate_row = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM source_visual_capture_generations
                WHERE generation_id = ?
                """,
                (generation.generation_id,),
            ).fetchone()
            active_row = connection.execute(
                """
                SELECT generation_id
                FROM source_visual_capture_bindings
                WHERE project_id = ? AND sor_document_id = ? AND source_sha256 = ?
                """,
                (
                    generation.project_id,
                    generation.sor_document_id,
                    generation.source_sha256,
                ),
            ).fetchone()
            if candidate_row is not None:
                candidate = _decode_model(
                    candidate_row,
                    VisualCaptureGenerationV1,
                    "visual generation",
                )
                if _generation_identity(candidate) != _generation_identity(generation):
                    raise SourceVisualCaptureRepositoryConflict(
                        "visual generation identity already contains another plan"
                    )
                if (
                    active_row is not None
                    and str(active_row["generation_id"]) != generation.generation_id
                ):
                    raise SourceVisualCaptureRepositoryConflict(
                        "visual generation was superseded by a newer activation"
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_visual_capture_generations (
                    generation_id, project_id, sor_document_id, source_sha256,
                    base_capture_id, provider_id, provider_version,
                    payload_json, payload_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation.generation_id,
                    generation.project_id,
                    generation.sor_document_id,
                    generation.source_sha256,
                    generation.base_capture_id,
                    generation.provider_id,
                    generation.provider_version,
                    payload,
                    _sha256(payload),
                    _utc_iso(generation.created_at_utc),
                ),
            )
            row = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM source_visual_capture_generations
                WHERE generation_id = ?
                """,
                (generation.generation_id,),
            ).fetchone()
            stored = _decode_model(row, VisualCaptureGenerationV1, "visual generation")
            if _generation_identity(stored) != _generation_identity(generation):
                raise SourceVisualCaptureRepositoryConflict(
                    "visual generation identity already contains another plan"
                )
            connection.execute(
                """
                INSERT INTO source_visual_capture_bindings (
                    project_id, sor_document_id, source_sha256,
                    generation_id, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, sor_document_id, source_sha256)
                DO UPDATE SET generation_id = excluded.generation_id,
                              updated_at_utc = excluded.updated_at_utc
                """,
                (
                    generation.project_id,
                    generation.sor_document_id,
                    generation.source_sha256,
                    generation.generation_id,
                    _utc_iso(generation.created_at_utc),
                ),
            )
        return stored

    def get_active_generation(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        source_sha256: str,
    ) -> VisualCaptureGenerationV1 | None:
        _require_scope(project_id, sor_document_id, source_sha256)
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT g.payload_json, g.payload_sha256
                FROM source_visual_capture_bindings AS b
                JOIN source_visual_capture_generations AS g
                  ON g.generation_id = b.generation_id
                JOIN sor_documents AS d
                  ON d.id = b.sor_document_id
                 AND d.project_id = b.project_id
                 AND d.source_sha256 = b.source_sha256
                WHERE b.project_id = ? AND b.sor_document_id = ?
                  AND b.source_sha256 = ?
                """,
                (project_id, sor_document_id, source_sha256),
            ).fetchone()
        if row is None:
            return None
        return _decode_model(row, VisualCaptureGenerationV1, "visual generation")

    def append_page(self, page: VisualPageCaptureV1) -> VisualPageCaptureV1:
        payload = _canonical_model_json(page)
        with db.get_conn() as connection:
            _lock_current_document(
                connection,
                project_id=page.project_id,
                sor_document_id=page.sor_document_id,
                source_sha256=page.source_sha256,
            )
            generation = connection.execute(
                """
                SELECT g.project_id, g.sor_document_id, g.source_sha256,
                       g.base_capture_id, g.payload_json, g.payload_sha256
                FROM source_visual_capture_generations AS g
                JOIN source_visual_capture_bindings AS b
                  ON b.generation_id = g.generation_id
                 AND b.project_id = g.project_id
                 AND b.sor_document_id = g.sor_document_id
                 AND b.source_sha256 = g.source_sha256
                WHERE g.generation_id = ?
                """,
                (page.generation_id,),
            ).fetchone()
            if generation is None or (
                str(generation["project_id"]),
                str(generation["sor_document_id"]),
                str(generation["source_sha256"]),
                str(generation["base_capture_id"]),
            ) != (
                page.project_id,
                page.sor_document_id,
                page.source_sha256,
                page.base_capture_id,
            ):
                raise SourceVisualCaptureRepositoryConflict(
                    "visual page does not bind the active generation"
                )
            generation_model = _decode_model(
                generation,
                VisualCaptureGenerationV1,
                "visual generation",
            )
            if (
                page.page_number not in generation_model.required_page_numbers
                or page.provider_id != generation_model.provider_id
                or page.provider_version != generation_model.provider_version
                or page.renderer_id != generation_model.renderer_id
                or page.renderer_version != generation_model.renderer_version
                or page.render_scale != generation_model.render_scale
            ):
                raise SourceVisualCaptureRepositoryConflict(
                    "visual page does not match the active capture plan"
                )
            current = connection.execute(
                """
                SELECT 1 AS present FROM sor_documents
                WHERE id = ? AND project_id = ? AND source_sha256 = ?
                """,
                (page.sor_document_id, page.project_id, page.source_sha256),
            ).fetchone()
            if current is None:
                raise SourceVisualCaptureRepositoryConflict(
                    "visual page source identity is no longer current"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_visual_page_snapshots (
                    generation_id, page_number, visual_snapshot_id,
                    page_image_sha256, payload_json, payload_sha256, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    page.generation_id,
                    page.page_number,
                    page.visual_snapshot_id,
                    page.page_image_sha256,
                    payload,
                    _sha256(payload),
                    _utc_iso(page.created_at_utc),
                ),
            )
            row = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM source_visual_page_snapshots
                WHERE generation_id = ? AND page_number = ?
                """,
                (page.generation_id, page.page_number),
            ).fetchone()
        stored = _decode_model(row, VisualPageCaptureV1, "visual page")
        if _page_identity(stored) != _page_identity(page):
            raise SourceVisualCaptureRepositoryConflict(
                "visual page already contains another immutable snapshot"
            )
        return stored

    def get_page(
        self,
        *,
        generation_id: str,
        page_number: int,
    ) -> VisualPageCaptureV1 | None:
        _require_non_blank(generation_id, "generation_id")
        _require_page(page_number)
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM source_visual_page_snapshots
                WHERE generation_id = ? AND page_number = ?
                """,
                (generation_id, page_number),
            ).fetchone()
        if row is None:
            return None
        return _decode_model(row, VisualPageCaptureV1, "visual page")

    def list_pages(self, generation_id: str) -> tuple[VisualPageCaptureV1, ...]:
        _require_non_blank(generation_id, "generation_id")
        with db.get_conn() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, payload_sha256
                FROM source_visual_page_snapshots
                WHERE generation_id = ? ORDER BY page_number
                """,
                (generation_id,),
            ).fetchall()
        return tuple(_decode_model(row, VisualPageCaptureV1, "visual page") for row in rows)

    def append_page_failure(
        self, failure: VisualPageFailureRecord
    ) -> VisualPageFailureRecord:
        _validate_failure(failure)
        with db.get_conn() as connection:
            _lock_current_document(
                connection,
                project_id=failure.project_id,
                sor_document_id=failure.sor_document_id,
                source_sha256=failure.source_sha256,
            )
            generation = connection.execute(
                """
                SELECT g.project_id, g.sor_document_id, g.source_sha256,
                       g.payload_json, g.payload_sha256
                FROM source_visual_capture_generations AS g
                JOIN source_visual_capture_bindings AS b
                  ON b.generation_id = g.generation_id
                 AND b.project_id = g.project_id
                 AND b.sor_document_id = g.sor_document_id
                 AND b.source_sha256 = g.source_sha256
                WHERE g.generation_id = ?
                """,
                (failure.generation_id,),
            ).fetchone()
            if generation is None or (
                str(generation["project_id"]),
                str(generation["sor_document_id"]),
                str(generation["source_sha256"]),
            ) != (
                failure.project_id,
                failure.sor_document_id,
                failure.source_sha256,
            ):
                raise SourceVisualCaptureRepositoryConflict(
                    "visual failure does not bind the active generation"
                )
            generation_model = _decode_model(
                generation,
                VisualCaptureGenerationV1,
                "visual generation",
            )
            if failure.page_number not in generation_model.required_page_numbers:
                raise SourceVisualCaptureRepositoryConflict(
                    "visual failure page is outside the active capture plan"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_visual_page_attempts (
                    attempt_id, generation_id, project_id, sor_document_id,
                    source_sha256, page_number, status, failure_code, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 'FAILED', ?, ?)
                """,
                (
                    failure.attempt_id,
                    failure.generation_id,
                    failure.project_id,
                    failure.sor_document_id,
                    failure.source_sha256,
                    failure.page_number,
                    failure.failure_code,
                    _utc_iso(failure.created_at_utc),
                ),
            )
        return failure

    def latest_page_failure(
        self,
        *,
        generation_id: str,
        page_number: int,
    ) -> VisualPageFailureRecord | None:
        _require_non_blank(generation_id, "generation_id")
        _require_page(page_number)
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT * FROM source_visual_page_attempts
                WHERE generation_id = ? AND page_number = ? AND status = 'FAILED'
                ORDER BY created_at_utc DESC, attempt_id DESC LIMIT 1
                """,
                (generation_id, page_number),
            ).fetchone()
        if row is None:
            return None
        return VisualPageFailureRecord(
            attempt_id=str(row["attempt_id"]),
            generation_id=str(row["generation_id"]),
            project_id=str(row["project_id"]),
            sor_document_id=str(row["sor_document_id"]),
            source_sha256=str(row["source_sha256"]),
            page_number=int(row["page_number"]),
            failure_code=str(row["failure_code"]),
            created_at_utc=_parse_datetime(row["created_at_utc"]),
        )

    def purge_document(self, *, project_id: str, sor_document_id: str) -> None:
        _require_non_blank(project_id, "project_id")
        _require_non_blank(sor_document_id, "sor_document_id")
        with db.get_conn() as connection:
            generation_rows = connection.execute(
                """
                SELECT generation_id FROM source_visual_capture_generations
                WHERE project_id = ? AND sor_document_id = ?
                """,
                (project_id, sor_document_id),
            ).fetchall()
            generation_ids = tuple(str(row["generation_id"]) for row in generation_rows)
            for generation_id in generation_ids:
                connection.execute(
                    "DELETE FROM source_visual_page_attempts WHERE generation_id = ?",
                    (generation_id,),
                )
                connection.execute(
                    "DELETE FROM source_visual_page_snapshots WHERE generation_id = ?",
                    (generation_id,),
                )
            connection.execute(
                """
                DELETE FROM source_visual_capture_bindings
                WHERE project_id = ? AND sor_document_id = ?
                """,
                (project_id, sor_document_id),
            )
            connection.execute(
                """
                DELETE FROM source_visual_capture_generations
                WHERE project_id = ? AND sor_document_id = ?
                """,
                (project_id, sor_document_id),
            )


def _generation_identity(value: VisualCaptureGenerationV1) -> tuple[Any, ...]:
    return (
        value.generation_id,
        value.project_id,
        value.sor_document_id,
        value.source_sha256,
        value.base_capture_id,
        value.base_source_ir_id,
        value.capture_profile_id,
        value.risk_policy_version,
        value.renderer_id,
        value.renderer_version,
        value.render_scale,
        value.provider_id,
        value.provider_version,
        value.required_page_numbers,
    )


def _lock_current_document(
    connection: Any,
    *,
    project_id: str,
    sor_document_id: str,
    source_sha256: str,
) -> None:
    _require_scope(project_id, sor_document_id, source_sha256)
    if not db.is_postgresql_runtime():
        connection.execute("BEGIN IMMEDIATE")
    lock_suffix = " FOR UPDATE" if db.is_postgresql_runtime() else ""
    current = connection.execute(
        f"""
        SELECT id FROM sor_documents
        WHERE id = ? AND project_id = ? AND source_sha256 = ?{lock_suffix}
        """,
        (sor_document_id, project_id, source_sha256),
    ).fetchone()
    if current is None:
        raise SourceVisualCaptureRepositoryConflict(
            "visual capture source identity is no longer current"
        )


def _page_identity(value: VisualPageCaptureV1) -> dict[str, Any]:
    """Ignore observation time when replaying the same immutable provider fact."""

    return value.model_dump(mode="json", exclude={"created_at_utc"})


def _decode_model(row: Any, model: Any, label: str):
    if row is None:
        raise SourceVisualCaptureRepositoryCorrupt(f"{label} does not exist")
    payload = str(row["payload_json"])
    if _sha256(payload) != str(row["payload_sha256"]):
        raise SourceVisualCaptureRepositoryCorrupt(f"{label} payload hash mismatch")
    try:
        return model.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise SourceVisualCaptureRepositoryCorrupt(f"{label} payload is invalid") from exc


def _canonical_model_json(value: Any) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise SourceVisualCaptureRepositoryCorrupt("stored timestamp is invalid") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise SourceVisualCaptureRepositoryCorrupt("stored timestamp must be timezone-aware")
    return result


def _require_scope(project_id: str, document_id: str, source_sha256: str) -> None:
    _require_non_blank(project_id, "project_id")
    _require_non_blank(document_id, "sor_document_id")
    if len(source_sha256) != 64 or any(char not in "0123456789abcdef" for char in source_sha256):
        raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")


def _require_non_blank(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


def _require_page(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("page_number must be a positive integer")


def _validate_failure(value: VisualPageFailureRecord) -> None:
    _require_scope(value.project_id, value.sor_document_id, value.source_sha256)
    _require_non_blank(value.attempt_id, "attempt_id")
    _require_non_blank(value.generation_id, "generation_id")
    _require_non_blank(value.failure_code, "failure_code")
    _require_page(value.page_number)
    _utc_iso(value.created_at_utc)


__all__ = ["SqlSourceVisualCaptureRepository"]
