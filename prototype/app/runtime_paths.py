from __future__ import annotations

import os
from pathlib import Path


STORAGE_ROOT_ENV_VAR = "REQPILOT_STORAGE_DIR"


def resolve_storage_dir(prototype_dir: Path) -> Path:
    raw_value = str(os.getenv(STORAGE_ROOT_ENV_VAR) or "").strip()
    if not raw_value:
        return prototype_dir / "storage"

    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        candidate = prototype_dir / candidate
    return candidate