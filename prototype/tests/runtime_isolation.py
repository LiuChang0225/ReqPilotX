"""Process-wide writable-path isolation for the unittest suite.

This module must be activated before importing the server package or any legacy
``app`` alias because application composition binds storage paths at import time.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from threading import Lock


STORAGE_ROOT_ENV_VAR = "REQPILOT_STORAGE_DIR"
TEST_STORAGE_ROOT_ENV_VAR = "REQPILOT_TEST_STORAGE_DIR"
TEST_MODE_ENV_VAR = "REQPILOT_TEST_MODE"
DEPLOYMENT_MODE_ENV_VAR = "REQPILOT_ENV"
OBJECT_STORE_ENV_VAR = "REQPILOT_OBJECT_STORE"
OBJECT_ROOT_ENV_VAR = "REQPILOT_OBJECT_ROOT"
SCRATCH_ROOT_ENV_VAR = "REQPILOT_SCRATCH_ROOT"
AGENT_RUNTIME_ENV_VAR = "REQPILOT_AGENT_RUNTIME"
DEVELOPMENT_ROOT_ENV_VAR = "REQPILOT_DEVELOPMENT_ROOT"
FRONTEND_ROOT_ENV_VAR = "REQPILOT_FRONTEND_DIR"

_LOCK = Lock()
_ACTIVE_STORAGE_DIR: Path | None = None
_TEMPORARY_STORAGE: tempfile.TemporaryDirectory[str] | None = None

_PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _PROTOTYPE_DIR.parent
for _source_root in (
    _REPO_ROOT / "packages" / "engine" / "src",
    _REPO_ROOT / "apps" / "server" / "src",
    _PROTOTYPE_DIR,
):
    if str(_source_root) not in sys.path:
        sys.path.insert(0, str(_source_root))


def _apply_isolated_environment(storage_dir: Path) -> None:
    """Override inherited runtime settings that could reach local services."""

    scratch_dir = storage_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            STORAGE_ROOT_ENV_VAR: str(storage_dir),
            TEST_MODE_ENV_VAR: "1",
            DEPLOYMENT_MODE_ENV_VAR: "test",
            OBJECT_STORE_ENV_VAR: "local",
            OBJECT_ROOT_ENV_VAR: str(storage_dir),
            SCRATCH_ROOT_ENV_VAR: str(scratch_dir),
            AGENT_RUNTIME_ENV_VAR: "none",
            DEVELOPMENT_ROOT_ENV_VAR: str(_PROTOTYPE_DIR.parent),
            FRONTEND_ROOT_ENV_VAR: str(_PROTOTYPE_DIR.parent / "apps/web/dist"),
        }
    )


def activate_test_runtime() -> Path:
    """Route every legacy storage consumer to one isolated process directory."""

    global _ACTIVE_STORAGE_DIR, _TEMPORARY_STORAGE

    with _LOCK:
        if _ACTIVE_STORAGE_DIR is not None:
            _apply_isolated_environment(_ACTIVE_STORAGE_DIR)
            return _ACTIVE_STORAGE_DIR

        requested_root = str(os.getenv(TEST_STORAGE_ROOT_ENV_VAR) or "").strip()
        if requested_root:
            candidate = Path(requested_root).expanduser().resolve()
        else:
            _TEMPORARY_STORAGE = tempfile.TemporaryDirectory(prefix="reqpilotx-tests-")
            candidate = Path(_TEMPORARY_STORAGE.name).resolve()

        production_storage = (_PROTOTYPE_DIR / "storage").resolve()
        if candidate == production_storage or production_storage in candidate.parents:
            raise RuntimeError(
                f"Test storage must not use the application storage tree: {candidate}"
            )

        candidate.mkdir(parents=True, exist_ok=True)
        _apply_isolated_environment(candidate)
        _ACTIVE_STORAGE_DIR = candidate
        return candidate


def active_test_storage_dir() -> Path:
    """Return the active isolated root, activating it when necessary."""

    return activate_test_runtime()
