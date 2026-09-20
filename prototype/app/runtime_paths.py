from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


STORAGE_ROOT_ENV_VAR = "REQPILOT_STORAGE_DIR"


def resolve_storage_dir(
    prototype_dir: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the writable storage root from an injectable environment.

    Application modules still bind this value during import, so launchers and
    tests must provide the environment before importing those modules.  The
    optional mapping keeps the resolver deterministic and directly testable
    without mutating process state.
    """

    source = os.environ if environment is None else environment
    raw_value = str(source.get(STORAGE_ROOT_ENV_VAR) or "").strip()
    if not raw_value:
        return prototype_dir / "storage"

    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        candidate = prototype_dir / candidate
    return candidate
