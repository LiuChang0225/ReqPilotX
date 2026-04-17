from __future__ import annotations

import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import db
from app.services.extraction_preview import build_extraction_preview
from app.services.table_assets import current_source_snapshot_build_token

logger = logging.getLogger(__name__)

_PREVIEW_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview-cache")
_PREVIEW_LOCK = threading.Lock()
_ACTIVE_REFRESHES: set[str] = set()
_CANCELLED_REFRESHES: set[str] = set()
_PREVIEW_CACHE_VERSION = "preview-cache-v29-section-window-visual-override"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview_dir() -> Path:
    path = db.STORAGE_DIR / "previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def preview_cache_path(document_id: str) -> Path:
    return _preview_dir() / f"{document_id}.json"


def document_snapshot_root(document_id: str, *, create: bool = True) -> Path:
    token = str(document_id or "").strip() or "preview"
    path = db.STORAGE_DIR / "table_snapshots" / "preview" / token
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def reset_document_snapshot_root(document_id: str) -> Path:
    path = document_snapshot_root(document_id, create=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def purge_preview_artifacts(document_id: str, *, recreate_snapshot_root: bool = True) -> Path:
    cache_path = preview_cache_path(document_id)
    try:
        cache_path.unlink()
    except FileNotFoundError:
        pass
    snapshot_root = document_snapshot_root(document_id, create=False)
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root, ignore_errors=True)
    if recreate_snapshot_root:
        snapshot_root.mkdir(parents=True, exist_ok=True)
    return snapshot_root


def cancel_preview_refresh(document_id: str) -> None:
    token = str(document_id or "").strip()
    if not token:
        return
    with _PREVIEW_LOCK:
        _CANCELLED_REFRESHES.add(token)


def _refresh_cancelled(document_id: str) -> bool:
    token = str(document_id or "").strip()
    if not token:
        return False
    with _PREVIEW_LOCK:
        return token in _CANCELLED_REFRESHES


def _source_mtime_token(file_path: str) -> str:
    path = Path(str(file_path or "").strip())
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return ""


def _preview_build_token() -> str:
    return "|".join(
        [
            _PREVIEW_CACHE_VERSION,
            current_source_snapshot_build_token(),
        ]
    )


def get_preview_cache_state(document: dict | None) -> dict[str, Any]:
    if not isinstance(document, dict):
        return {
            "preview_status": "NOT_READY",
            "preview_generated_at": "",
            "preview_cache_hit": False,
            "preview_error": "",
            "preview_file_mtime": "",
        }

    document_id = str(document.get("id") or "").strip()
    file_path = str(document.get("file_path") or "").strip()
    stored_status = str(document.get("preview_status") or "NOT_READY").strip().upper() or "NOT_READY"
    stored_generated_at = str(document.get("preview_generated_at") or "")
    stored_error = str(document.get("preview_error") or "")
    stored_token = str(document.get("preview_file_mtime") or "")
    current_token = _source_mtime_token(file_path)
    cache_path = preview_cache_path(document_id) if document_id else None
    cache_exists = bool(cache_path and cache_path.exists())

    status = stored_status
    if current_token and stored_token and current_token != stored_token:
        status = "STALE"
    elif cache_exists and stored_status in {"NOT_READY", "FAILED"}:
        status = stored_status
    elif not cache_exists and stored_status == "READY":
        status = "STALE"

    return {
        "preview_status": status,
        "preview_generated_at": stored_generated_at,
        "preview_cache_hit": cache_exists,
        "preview_error": stored_error,
        "preview_file_mtime": current_token or stored_token,
    }


def load_cached_preview(document: dict | None) -> tuple[dict | None, dict[str, Any]]:
    meta = get_preview_cache_state(document)
    if not isinstance(document, dict):
        return None, meta

    document_id = str(document.get("id") or "").strip()
    if not document_id:
        return None, meta

    cache_path = preview_cache_path(document_id)
    if not cache_path.exists():
        meta["preview_cache_hit"] = False
        return None, meta

    try:
        preview = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - corrupted cache path is rare but must fail safe
        logger.warning("Failed to read cached preview for %s: %s", document_id, exc)
        meta["preview_status"] = "FAILED"
        meta["preview_cache_hit"] = False
        meta["preview_error"] = f"cached preview read failed: {exc}"
        return None, meta

    if (
        not isinstance(preview, dict)
        or str(preview.get("_preview_cache_version") or "") != _PREVIEW_CACHE_VERSION
        or str(preview.get("_preview_build_token") or "") != _preview_build_token()
    ):
        meta["preview_status"] = "STALE"
        meta["preview_cache_hit"] = False
        meta["preview_error"] = "cached preview is outdated"
        return None, meta

    meta["preview_cache_hit"] = True
    return preview, meta


def persist_preview_cache(document: dict | None, preview: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict):
        return {}
    document_id = str(document.get("id") or "").strip()
    file_path = str(document.get("file_path") or "").strip()
    if not document_id or not file_path:
        return {}

    cache_path = preview_cache_path(document_id)
    temp_path = cache_path.with_suffix(".tmp")
    payload = dict(preview)
    payload["_preview_cache_version"] = _PREVIEW_CACHE_VERSION
    payload["_preview_build_token"] = _preview_build_token()
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(cache_path)
    generated_at = _now_iso()
    token = _source_mtime_token(file_path)
    return db.update_sor_document_preview_state(
        document_id,
        preview_status="READY",
        preview_generated_at=generated_at,
        preview_file_mtime=token,
        preview_error="",
    )


def mark_preview_stale(document: dict | None) -> dict[str, Any]:
    if not isinstance(document, dict):
        return {}
    document_id = str(document.get("id") or "").strip()
    if not document_id:
        return {}
    return db.update_sor_document_preview_state(
        document_id,
        preview_status="STALE",
        preview_error="",
    )


def request_preview_refresh(document: dict | None, *, force: bool = False) -> dict[str, Any]:
    if not isinstance(document, dict):
        return {"accepted": False, "preview_status": "NOT_READY"}
    document_id = str(document.get("id") or "").strip()
    file_path = str(document.get("file_path") or "").strip()
    if not document_id or not file_path:
        return {"accepted": False, "preview_status": "FAILED", "preview_error": "Missing preview source file."}

    meta = get_preview_cache_state(document)
    status = str(meta.get("preview_status") or "NOT_READY").upper()
    if not force and status == "BUILDING":
        return {"accepted": False, **meta}

    with _PREVIEW_LOCK:
        _CANCELLED_REFRESHES.discard(document_id)
        if document_id in _ACTIVE_REFRESHES:
            current = db.update_sor_document_preview_state(document_id, preview_status="BUILDING", preview_error="")
            current_meta = get_preview_cache_state(current or document)
            return {"accepted": False, **current_meta}
        _ACTIVE_REFRESHES.add(document_id)

    current = db.update_sor_document_preview_state(document_id, preview_status="BUILDING", preview_error="")
    _PREVIEW_EXECUTOR.submit(_run_preview_refresh, document_id, file_path)
    return {"accepted": True, **get_preview_cache_state(current or document)}


def _run_preview_refresh(document_id: str, file_path: str) -> None:
    try:
        if _refresh_cancelled(document_id):
            return
        snapshot_root = purge_preview_artifacts(document_id)
        if _refresh_cancelled(document_id):
            return
        preview = build_extraction_preview(
            file_path,
            document_id=document_id,
            snapshot_root=snapshot_root,
        )
        if _refresh_cancelled(document_id):
            return
        document = db.get_sor_document(document_id)
        if document:
            persist_preview_cache(document, preview)
    except FileNotFoundError as exc:  # pragma: no cover - delete race is integration-level
        document = db.get_sor_document(document_id)
        if _refresh_cancelled(document_id) or not document:
            logger.info("Preview refresh skipped for deleted/cancelled document %s: %s", document_id, exc)
            return
        logger.exception("Preview refresh failed for %s", document_id)
        db.update_sor_document_preview_state(
            document_id,
            preview_status="FAILED",
            preview_error=str(exc)[:500],
        )
    except Exception as exc:  # pragma: no cover - failure path is integration-level
        logger.exception("Preview refresh failed for %s", document_id)
        db.update_sor_document_preview_state(
            document_id,
            preview_status="FAILED",
            preview_error=str(exc)[:500],
        )
    finally:
        with _PREVIEW_LOCK:
            _ACTIVE_REFRESHES.discard(document_id)
            _CANCELLED_REFRESHES.discard(document_id)
