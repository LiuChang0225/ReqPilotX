"""Narrow runtime binding for immutable source access.

The reading/source-capture slice must not import ``reqpilotx_server.application.runtime``:
that module owns formal extraction and AI-copilot setup. Application startup
binds only the object-store and materialization capabilities needed to read the
current document and publish content-addressed SourceIR assets.
"""

from __future__ import annotations

from reqpilotx_server.resource_paths import development_path

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reqpilotx_server.adapters.storage.local_object_store import LocalObjectStore
from reqpilotx_server.application.storage.runtime_files import RuntimeObjectFiles
from reqpilotx_server.infrastructure.runtime_settings import (
    DatabaseBackend,
    ObjectStoreBackend,
    RuntimeSettings,
)
from reqpilotx_server.ports.object_store import ObjectStore


_PROTOTYPE_DIR = development_path('prototype')


@dataclass(slots=True)
class _SourceRuntimeBinding:
    settings: RuntimeSettings | None = None
    object_store: ObjectStore | None = None
    files: RuntimeObjectFiles | None = None


_BINDING = _SourceRuntimeBinding()


def bind_source_runtime(
    *,
    settings: RuntimeSettings,
    object_store: ObjectStore,
    files: RuntimeObjectFiles,
) -> None:
    """Bind source-only runtime capabilities after application startup."""

    _BINDING.settings = settings
    _BINDING.object_store = object_store
    _BINDING.files = files


def clear_source_runtime() -> None:
    _BINDING.settings = None
    _BINDING.object_store = None
    _BINDING.files = None


def resolve_source_record_path(record: dict[str, Any] | None) -> Path:
    """Resolve one persisted source identity without loading the formal runtime."""

    if not record:
        raise ValueError("A persisted source record is required.")
    storage_key = str(record.get("storage_key") or "").strip()
    if storage_key:
        if _BINDING.files is None:
            raise RuntimeError("Source object materialization is not initialized.")
        return _BINDING.files.resolve_record(record)

    settings = _BINDING.settings or _settings_from_environment()
    if settings.database_backend is not DatabaseBackend.SQLITE_TEST:
        raise RuntimeError("Production source records must use a storage_key.")
    file_path = str(record.get("file_path") or "").strip()
    if not file_path:
        raise ValueError("Test source record has no file path.")
    return Path(file_path)


def get_source_object_store() -> ObjectStore:
    """Return the bound source store, with a local-only test fallback."""

    if _BINDING.object_store is not None:
        return _BINDING.object_store
    settings = _BINDING.settings or _settings_from_environment()
    if (
        settings.database_backend is DatabaseBackend.SQLITE_TEST
        and settings.object_store_backend is ObjectStoreBackend.LOCAL
    ):
        return LocalObjectStore(settings.object_root)
    raise RuntimeError("Source object storage is not initialized.")


def _settings_from_environment() -> RuntimeSettings:
    return RuntimeSettings.from_environment(prototype_dir=_PROTOTYPE_DIR)


__all__ = [
    "bind_source_runtime",
    "clear_source_runtime",
    "get_source_object_store",
    "resolve_source_record_path",
]
