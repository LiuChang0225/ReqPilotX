"""Optional private regression fixtures; absent fixtures are explicit skips."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.private_eval import PRIVATE_EVAL_ENV, private_eval_path


def require_private_eval(relative_path: str) -> Path:
    path = private_eval_path(relative_path)
    if not path.is_file():
        raise unittest.SkipTest(
            f"Private fixture {relative_path} unavailable; configure {PRIVATE_EVAL_ENV}. "
            "Public synthetic contracts run independently."
        )
    return path
