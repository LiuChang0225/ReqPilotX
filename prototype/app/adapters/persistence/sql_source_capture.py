"""Persistence owned by neutral source capture, plus a read-only facts view."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app import db
from app.domain.extraction.source_ir import SourceIRSnapshot, SourceIRStatus
from app.ports.source_capture_repository import (
    SourceCaptureRecord,
    SourceCaptureRepositoryConflict,
    SourceCaptureRepositoryCorrupt,
)


class SqlSourceFactsReader:
    """Read-only capability exposed to the reading application."""

    def get_capture(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        source_sha256: str,
    ) -> SourceCaptureRecord | None:
        _require_scope(project_id, sor_document_id, source_sha256)
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT
                    b.project_id,
                    b.sor_document_id,
                    b.source_sha256,
                    s.capture_id,
                    s.source_ir_id,
                    s.source_ir_document_id,
                    s.payload_sha256,
                    s.facts_sha256,
                    s.payload_json,
                    s.created_at_utc
                FROM source_capture_bindings AS b
                JOIN source_capture_snapshots AS s
                  ON s.capture_id = b.capture_id
                WHERE b.project_id = ?
                  AND b.sor_document_id = ?
                  AND b.source_sha256 = ?
                """,
                (project_id, sor_document_id, source_sha256),
            ).fetchone()
            if row is None:
                return None
            capture = _decode_capture(row)
            _validate_stored_asset_manifest(connection, capture)
            return capture


class SqlSourceCaptureRepository(SqlSourceFactsReader):
    """Append/GC capability used only by source capture and deletion owners."""

    def append_capture(
        self,
        record: SourceCaptureRecord,
        *,
        asset_publication_id: str | None = None,
    ) -> SourceCaptureRecord:
        _validate_capture_record(record)
        source_ir = record.source_ir
        asset_manifest = _asset_manifest(source_ir)
        if asset_manifest and not str(asset_publication_id or "").strip():
            raise SourceCaptureRepositoryConflict(
                "SourceIR assets require an open source-capture publication"
            )
        payload_json = _canonical_model_json(source_ir)
        payload_sha256 = _sha256(payload_json)
        facts_json = _canonical_source_facts_json(source_ir)
        facts_sha256 = _sha256(facts_json)
        created_at = _utc_iso(record.created_at_utc)
        with db.get_conn() as connection:
            if asset_publication_id:
                _require_asset_publication(
                    connection,
                    record=record,
                    publication_id=asset_publication_id,
                    expected_manifest=asset_manifest,
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_capture_snapshots (
                    capture_id, source_ir_id, source_ir_document_id,
                    source_sha256, schema_version, adapter_id, adapter_version,
                    status, payload_sha256, facts_sha256, payload_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.capture_id,
                    source_ir.source_ir_id,
                    source_ir.document_id,
                    source_ir.source_sha256,
                    source_ir.schema_version,
                    source_ir.adapter_id,
                    source_ir.adapter_version,
                    source_ir.status.value,
                    payload_sha256,
                    facts_sha256,
                    payload_json,
                    created_at,
                ),
            )
            existing_snapshot = connection.execute(
                """
                SELECT capture_id, source_ir_id, source_ir_document_id,
                       source_sha256, payload_sha256, facts_sha256,
                       schema_version, adapter_id, adapter_version, status
                FROM source_capture_snapshots
                WHERE capture_id = ? OR source_ir_id = ?
                ORDER BY capture_id
                LIMIT 1
                """,
                (record.capture_id, source_ir.source_ir_id),
            ).fetchone()
            if existing_snapshot is None or (
                str(existing_snapshot["capture_id"]) != record.capture_id
                or str(existing_snapshot["source_ir_id"]) != source_ir.source_ir_id
                or str(existing_snapshot["source_ir_document_id"])
                != source_ir.document_id
                or str(existing_snapshot["source_sha256"])
                != source_ir.source_sha256
                or str(existing_snapshot["payload_sha256"]) != payload_sha256
                or str(existing_snapshot["facts_sha256"]) != facts_sha256
                or str(existing_snapshot["schema_version"])
                != source_ir.schema_version
                or str(existing_snapshot["adapter_id"]) != source_ir.adapter_id
                or str(existing_snapshot["adapter_version"])
                != source_ir.adapter_version
                or str(existing_snapshot["status"]) != source_ir.status.value
            ):
                raise SourceCaptureRepositoryConflict(
                    "source capture identity already contains different facts"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_capture_bindings (
                    project_id, sor_document_id, source_sha256,
                    capture_id, created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.project_id,
                    record.sor_document_id,
                    record.source_sha256,
                    record.capture_id,
                    created_at,
                ),
            )
            binding = connection.execute(
                """
                SELECT capture_id
                FROM source_capture_bindings
                WHERE project_id = ? AND sor_document_id = ? AND source_sha256 = ?
                """,
                (record.project_id, record.sor_document_id, record.source_sha256),
            ).fetchone()
            if binding is None or str(binding["capture_id"]) != record.capture_id:
                raise SourceCaptureRepositoryConflict(
                    "document source version is already bound to another capture"
                )
            if asset_publication_id:
                connection.execute(
                    """
                    INSERT INTO source_capture_asset_references (
                        capture_id, storage_key
                    )
                    SELECT ?, storage_key
                    FROM source_capture_asset_staging_references
                    WHERE publication_id = ?
                    ON CONFLICT(capture_id, storage_key) DO NOTHING
                    """,
                    (record.capture_id, asset_publication_id),
                )
                connection.execute(
                    """
                    DELETE FROM source_capture_asset_staging_references
                    WHERE publication_id = ?
                    """,
                    (asset_publication_id,),
                )
                connection.execute(
                    """
                    DELETE FROM source_capture_asset_publications
                    WHERE publication_id = ? AND status = 'STAGING'
                    """,
                    (asset_publication_id,),
                )
        stored = self.get_capture(
            project_id=record.project_id,
            sor_document_id=record.sor_document_id,
            source_sha256=record.source_sha256,
        )
        if stored is None:
            raise SourceCaptureRepositoryCorrupt(
                "appended source capture could not be read"
            )
        return stored

    def purge_document(
        self,
        *,
        project_id: str,
        sor_document_id: str,
    ) -> tuple[str, ...]:
        _require_non_blank(project_id, "project_id")
        _require_non_blank(sor_document_id, "sor_document_id")
        released_keys: set[str] = set()
        with db.get_conn() as connection:
            capture_rows = connection.execute(
                """
                SELECT capture_id
                FROM source_capture_bindings
                WHERE project_id = ? AND sor_document_id = ?
                """,
                (project_id, sor_document_id),
            ).fetchall()
            capture_ids = tuple(str(row["capture_id"]) for row in capture_rows)
            if capture_ids:
                placeholders = ", ".join("?" for _ in capture_ids)
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT storage_key
                    FROM source_capture_asset_references
                    WHERE capture_id IN ({placeholders})
                    """,
                    capture_ids,
                ).fetchall()
                released_keys.update(str(row["storage_key"]) for row in rows)
            connection.execute(
                "DELETE FROM source_capture_bindings WHERE project_id = ? AND sor_document_id = ?",
                (project_id, sor_document_id),
            )
            for capture_id in capture_ids:
                connection.execute(
                    """
                    DELETE FROM source_capture_snapshots
                    WHERE capture_id = ?
                      AND NOT EXISTS (
                        SELECT 1 FROM source_capture_bindings
                        WHERE source_capture_bindings.capture_id = ?
                      )
                    """,
                    (capture_id, capture_id),
                )
        return tuple(sorted(released_keys))


def _validate_capture_record(record: SourceCaptureRecord) -> None:
    _require_scope(record.project_id, record.sor_document_id, record.source_sha256)
    _require_non_blank(record.capture_id, "capture_id")
    if record.source_ir.source_sha256 != record.source_sha256:
        raise ValueError("capture source_sha256 must match SourceIR")
    if record.source_ir.status is SourceIRStatus.FAILED:
        raise ValueError("failed SourceIR attempts cannot be captured as immutable facts")
    _utc_iso(record.created_at_utc)


def _asset_manifest(source_ir: SourceIRSnapshot) -> dict[str, tuple[str, str]]:
    manifest: dict[str, tuple[str, str]] = {}
    for asset in source_ir.assets:
        identity = (asset.content_sha256, asset.media_type)
        previous = manifest.setdefault(asset.storage_key, identity)
        if previous != identity:
            raise SourceCaptureRepositoryConflict(
                "one SourceIR storage key has conflicting asset identities"
            )
    return manifest


def _require_asset_publication(
    connection: Any,
    *,
    record: SourceCaptureRecord,
    publication_id: str,
    expected_manifest: dict[str, tuple[str, str]],
) -> None:
    publication = connection.execute(
        """
        SELECT 1 AS present
        FROM source_capture_asset_publications AS publication
        JOIN sor_documents AS document
          ON document.id = publication.sor_document_id
         AND document.project_id = publication.project_id
         AND document.source_sha256 = publication.source_sha256
        WHERE publication.publication_id = ?
          AND publication.project_id = ?
          AND publication.sor_document_id = ?
          AND publication.source_sha256 = ?
          AND publication.status = 'STAGING'
        """,
        (
            publication_id,
            record.project_id,
            record.sor_document_id,
            record.source_sha256,
        ),
    ).fetchone()
    if publication is None:
        raise SourceCaptureRepositoryConflict(
            "source asset publication lost its current-document fence"
        )
    rows = connection.execute(
        """
        SELECT staging.storage_key, object.sha256, object.media_type
        FROM source_capture_asset_staging_references AS staging
        JOIN stored_objects AS object ON object.storage_key = staging.storage_key
        WHERE staging.publication_id = ?
        ORDER BY staging.storage_key
        """,
        (publication_id,),
    ).fetchall()
    actual_manifest = {
        str(row["storage_key"]): (str(row["sha256"]), str(row["media_type"]))
        for row in rows
    }
    if actual_manifest != expected_manifest:
        raise SourceCaptureRepositoryConflict(
            "source asset publication does not exact-match SourceIR assets"
        )


def _validate_stored_asset_manifest(
    connection: Any,
    capture: SourceCaptureRecord,
) -> None:
    rows = connection.execute(
        """
        SELECT ref.storage_key, object.sha256, object.media_type
        FROM source_capture_asset_references AS ref
        JOIN stored_objects AS object ON object.storage_key = ref.storage_key
        WHERE ref.capture_id = ?
        ORDER BY ref.storage_key
        """,
        (capture.capture_id,),
    ).fetchall()
    actual = {
        str(row["storage_key"]): (str(row["sha256"]), str(row["media_type"]))
        for row in rows
    }
    if actual != _asset_manifest(capture.source_ir):
        raise SourceCaptureRepositoryCorrupt(
            "source capture asset references do not exact-match SourceIR"
        )


def _decode_capture(row: Any) -> SourceCaptureRecord:
    payload_json = str(row["payload_json"])
    if _sha256(payload_json) != str(row["payload_sha256"]):
        raise SourceCaptureRepositoryCorrupt("source capture payload hash mismatch")
    try:
        source_ir = SourceIRSnapshot.model_validate_json(payload_json)
    except (TypeError, ValueError) as exc:
        raise SourceCaptureRepositoryCorrupt(
            "source capture payload is invalid"
        ) from exc
    facts_sha256 = _sha256(_canonical_source_facts_json(source_ir))
    if (
        source_ir.source_ir_id != str(row["source_ir_id"])
        or source_ir.document_id != str(row["source_ir_document_id"])
        or source_ir.source_sha256 != str(row["source_sha256"])
        or facts_sha256 != str(row["facts_sha256"])
    ):
        raise SourceCaptureRepositoryCorrupt(
            "source capture identity metadata mismatch"
        )
    return SourceCaptureRecord(
        capture_id=str(row["capture_id"]),
        project_id=str(row["project_id"]),
        sor_document_id=str(row["sor_document_id"]),
        source_sha256=str(row["source_sha256"]),
        source_ir=source_ir,
        created_at_utc=_parse_datetime(row["created_at_utc"]),
    )


def _canonical_model_json(model: Any) -> str:
    return _canonical_json(model.model_dump(mode="json"))


def _canonical_source_facts_json(source_ir: SourceIRSnapshot) -> str:
    return _canonical_json(source_ir.model_dump(mode="json", exclude={"created_at"}))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_scope(project_id: str, document_id: str, source_sha256: str) -> None:
    _require_non_blank(project_id, "project_id")
    _require_non_blank(document_id, "sor_document_id")
    if len(source_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in source_sha256
    ):
        raise ValueError(
            "source_sha256 must be 64 lowercase hexadecimal characters"
        )


def _require_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise SourceCaptureRepositoryCorrupt("stored timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceCaptureRepositoryCorrupt(
            "stored timestamp must be timezone-aware"
        )
    return parsed


__all__ = ["SqlSourceCaptureRepository", "SqlSourceFactsReader"]
