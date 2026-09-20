"""Resolve optional customer-derived evaluation metadata outside source control."""

from __future__ import annotations

import os
from pathlib import Path


PRIVATE_EVAL_ENV = "REQPILOTX_PRIVATE_EVAL_ROOT"


def private_eval_path(relative_path: str) -> Path:
    """Use an explicitly configured private eval root, with an ignored local default.

    The root has the same layout as prototype/eval. No source repository or
    customer corpus is discovered automatically. Schemas remain in the public
    eval directory and must not be resolved through this function.
    """
    configured = os.environ.get(PRIVATE_EVAL_ENV)
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).resolve().parents[1] / "eval" / "private"
    )
    result = (root / relative_path).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("Private evaluation path must stay within its root.")
    return result
