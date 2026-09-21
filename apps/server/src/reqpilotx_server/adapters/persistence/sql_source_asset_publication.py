"""SQL ownership metadata for neutral source-capture asset publication."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from reqpilotx_server import db
from reqpilotx_server.domain.extraction.source_ir import SourceAssetRef
from reqpilotx_server.ports.source_asset_publication import (
    SourceAssetPublicationConflict,
    SourceAssetPublicationIdentity,
)


class SqlSourceAssetPublicationRepository:
    def begin(self, identity: SourceAssetPublicationIdentity) -> None:
        _validate_identity(identity)
        created_at = _utc_iso(identity.created_at_utc)
        with db.get_conn() as connection:
            current = connection.execute(
                """
                SELECT 1 AS present
                FROM sor_documents
                WHERE id = ? AND project_id = ? AND source_sha256 = ?
                """,
                (
                    identity.sor_document_id,
                    identity.project_id,
                    identity.source_sha256,
                ),
            ).fetchone()
            if current is None:
                raise SourceAssetPublicationConflict(
                    "source asset publication does not bind the current document"
                )
            connection.execute(
                """
                INSERT INTO source_capture_asset_publications (
                    publication_id, project_id, sor_document_id, source_sha256,
                    status, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, 'STAGING', ?, ?)
                ON CONFLICT(publication_id) DO NOTHING
                """,
                (
                    identity.publication_id,
                    identity.project_id,
                    identity.sor_document_id,
                    identity.source_sha256,
                    created_at,
                    created_at,
                ),
            )
            row = connection.execute(
                """
                SELECT project_id, sor_document_id, source_sha256, status
                FROM source_capture_asset_publications
                WHERE publication_id = ?
                """,
                (identity.publication_id,),
            ).fetchone()
            if row is None or (
                str(row["project_id"]) != identity.project_id
                or str(row["sor_document_id"]) != identity.sor_document_id
                or str(row["source_sha256"]) != identity.source_sha256
                or str(row["status"]) != "STAGING"
            ):
                raise SourceAssetPublicationConflict(
                    "source asset publication identity is already fenced"
                )

    def stage_asset(
        self,
        identity: SourceAssetPublicationIdentity,
        *,
        asset: SourceAssetRef,
        size_bytes: int,
        touched_at_utc: datetime,
    ) -> None:
        _validate_identity(identity)
        if not isinstance(asset, SourceAssetRef):
            raise TypeError("asset must be a SourceAssetRef")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 1:
            raise ValueError("source asset size must be positive")
        touched_at = _utc_iso(touched_at_utc)
        with db.get_conn() as connection:
            if db.is_postgresql_runtime():
                db._source_asset_object_advisory_lock(connection, asset.storage_key)
            self._require_open_identity(connection, identity)
            metadata_existed = (
                connection.execute(
                    "SELECT 1 AS present FROM stored_objects WHERE storage_key = ?",
                    (asset.storage_key,),
                ).fetchone()
                is not None
            )
            managed_existed = (
                connection.execute(
                    """
                    SELECT 1 AS present
                    FROM source_asset_managed_objects
                    WHERE storage_key = ?
                    """,
                    (asset.storage_key,),
                ).fetchone()
                is not None
            )
            try:
                db._register_stored_object_in_conn(
                    connection,
                    storage_key=asset.storage_key,
                    sha256=asset.content_sha256,
                    size_bytes=size_bytes,
                    media_type=asset.media_type,
                    object_kind="source-ir-asset",
                )
            except ValueError as exc:
                raise SourceAssetPublicationConflict(
                    "source asset storage key is already bound to different bytes"
                ) from exc
            if not managed_existed:
                connection.execute(
                    """
                    INSERT INTO source_asset_managed_objects (
                        storage_key, legacy_protected
                    ) VALUES (?, ?)
                    ON CONFLICT(storage_key) DO NOTHING
                    """,
                    (asset.storage_key, bool(metadata_existed)),
                )
            connection.execute(
                """
                INSERT INTO source_capture_asset_staging_references (
                    publication_id, project_id, sor_document_id,
                    source_sha256, storage_key
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(publication_id, storage_key) DO NOTHING
                """,
                (
                    identity.publication_id,
                    identity.project_id,
                    identity.sor_document_id,
                    identity.source_sha256,
                    asset.storage_key,
                ),
            )
            staged = connection.execute(
                """
                SELECT object.sha256, object.size_bytes, object.media_type
                FROM source_capture_asset_staging_references AS staging
                JOIN stored_objects AS object
                  ON object.storage_key = staging.storage_key
                WHERE staging.publication_id = ? AND staging.storage_key = ?
                """,
                (identity.publication_id, asset.storage_key),
            ).fetchone()
            if staged is None or (
                str(staged["sha256"]) != asset.content_sha256
                or int(staged["size_bytes"]) != size_bytes
                or str(staged["media_type"]) != asset.media_type
            ):
                raise SourceAssetPublicationConflict(
                    "source asset staging key contains different bytes"
                )
            connection.execute(
                """
                UPDATE source_capture_asset_publications
                SET updated_at_utc = ?
                WHERE publication_id = ? AND status = 'STAGING'
                """,
                (touched_at, identity.publication_id),
            )

    def validate_manifest(
        self,
        identity: SourceAssetPublicationIdentity,
        assets: tuple[SourceAssetRef, ...],
    ) -> None:
        _validate_identity(identity)
        expected = _asset_manifest(assets)
        with db.get_conn() as connection:
            self._require_open_identity(connection, identity)
            actual = _staged_manifest(connection, identity.publication_id)
        if actual != expected:
            raise SourceAssetPublicationConflict(
                "source asset staging manifest does not exact-match SourceIR"
            )

    def abort(self, publication_id: str) -> tuple[str, ...]:
        _require_non_blank(publication_id, "publication_id")
        with db.get_conn() as connection:
            rows = connection.execute(
                """
                SELECT storage_key
                FROM source_capture_asset_staging_references
                WHERE publication_id = ?
                """,
                (publication_id,),
            ).fetchall()
            connection.execute(
                """
                DELETE FROM source_capture_asset_staging_references
                WHERE publication_id = ?
                """,
                (publication_id,),
            )
            connection.execute(
                "DELETE FROM source_capture_asset_publications WHERE publication_id = ?",
                (publication_id,),
            )
        return tuple(sorted({str(row["storage_key"]) for row in rows}))

    def reap_stale(self, *, stale_before_utc: datetime) -> tuple[str, ...]:
        cutoff = _utc_iso(stale_before_utc)
        with db.get_conn() as connection:
            publications = connection.execute(
                """
                SELECT publication_id
                FROM source_capture_asset_publications
                WHERE status = 'STAGING' AND updated_at_utc < ?
                ORDER BY updated_at_utc, publication_id
                LIMIT 256
                """,
                (cutoff,),
            ).fetchall()
            publication_ids = tuple(str(row["publication_id"]) for row in publications)
            keys: set[str] = set()
            for publication_id in publication_ids:
                rows = connection.execute(
                    """
                    SELECT storage_key
                    FROM source_capture_asset_staging_references
                    WHERE publication_id = ?
                    """,
                    (publication_id,),
                ).fetchall()
                keys.update(str(row["storage_key"]) for row in rows)
                connection.execute(
                    """
                    DELETE FROM source_capture_asset_staging_references
                    WHERE publication_id = ?
                    """,
                    (publication_id,),
                )
                connection.execute(
                    "DELETE FROM source_capture_asset_publications WHERE publication_id = ?",
                    (publication_id,),
                )
        return tuple(sorted(keys))

    def list_unreferenced(self, *, limit: int = 256) -> tuple[str, ...]:
        bounded_limit = int(limit)
        if not 1 <= bounded_limit <= 10_000:
            raise ValueError("source asset GC limit must be between 1 and 10000")
        with db.get_conn() as connection:
            clauses = [
                "NOT EXISTS (SELECT 1 FROM source_capture_asset_references AS ref WHERE ref.storage_key = managed.storage_key)",
                "NOT EXISTS (SELECT 1 FROM source_capture_asset_staging_references AS staging WHERE staging.storage_key = managed.storage_key)",
            ]
            if db.is_postgresql_runtime():
                clauses.extend(
                    (
                        "NOT EXISTS (SELECT 1 FROM source_asset_references AS ref WHERE ref.storage_key = managed.storage_key)",
                        "NOT EXISTS (SELECT 1 FROM source_asset_staging_references AS staging WHERE staging.storage_key = managed.storage_key)",
                    )
                )
            rows = connection.execute(
                f"""
                SELECT managed.storage_key
                FROM source_asset_managed_objects AS managed
                WHERE managed.legacy_protected = FALSE
                  AND {' AND '.join(clauses)}
                ORDER BY managed.created_at, managed.storage_key
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return tuple(str(row["storage_key"]) for row in rows)

    def forget_if_unreferenced(self, storage_key: str) -> bool:
        normalized_key = _require_non_blank(storage_key, "storage_key")
        if db.is_postgresql_runtime():
            raise RuntimeError(
                "PostgreSQL source asset deletion requires the global advisory claim"
            )
        with db.get_conn() as connection:
            managed = connection.execute(
                """
                SELECT legacy_protected
                FROM source_asset_managed_objects
                WHERE storage_key = ?
                """,
                (normalized_key,),
            ).fetchone()
            if managed is None or bool(managed["legacy_protected"]):
                return False
            referenced = connection.execute(
                """
                SELECT 1 AS present
                FROM (
                    SELECT storage_key FROM source_capture_asset_references
                    UNION ALL
                    SELECT storage_key FROM source_capture_asset_staging_references
                ) AS refs
                WHERE refs.storage_key = ?
                LIMIT 1
                """,
                (normalized_key,),
            ).fetchone()
            if referenced is not None:
                return False
            deleted = connection.execute(
                "DELETE FROM stored_objects WHERE storage_key = ?",
                (normalized_key,),
            )
        return deleted.rowcount == 1

    @staticmethod
    def _require_open_identity(
        connection: Any,
        identity: SourceAssetPublicationIdentity,
    ) -> None:
        row = connection.execute(
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
                identity.publication_id,
                identity.project_id,
                identity.sor_document_id,
                identity.source_sha256,
            ),
        ).fetchone()
        if row is None:
            raise SourceAssetPublicationConflict(
                "source asset publication lost its current-source fence"
            )


def _asset_manifest(
    assets: tuple[SourceAssetRef, ...],
) -> dict[str, tuple[str, str]]:
    manifest: dict[str, tuple[str, str]] = {}
    for asset in assets:
        identity = (asset.content_sha256, asset.media_type)
        previous = manifest.setdefault(asset.storage_key, identity)
        if previous != identity:
            raise SourceAssetPublicationConflict(
                "one source asset storage key has conflicting identities"
            )
    return manifest


def _staged_manifest(
    connection: Any,
    publication_id: str,
) -> dict[str, tuple[str, str]]:
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
    return {
        str(row["storage_key"]): (str(row["sha256"]), str(row["media_type"]))
        for row in rows
    }


def _validate_identity(identity: SourceAssetPublicationIdentity) -> None:
    _require_non_blank(identity.publication_id, "publication_id")
    _require_non_blank(identity.project_id, "project_id")
    _require_non_blank(identity.sor_document_id, "sor_document_id")
    if len(identity.source_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in identity.source_sha256
    ):
        raise ValueError("source_sha256 must be lowercase SHA-256")
    _utc_iso(identity.created_at_utc)


def _require_non_blank(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


__all__ = ["SqlSourceAssetPublicationRepository"]
