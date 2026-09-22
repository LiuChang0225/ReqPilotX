"""Compatibility package for the canonical :mod:`evals.agent_evals` suite."""

from pathlib import Path as _Path


_CANONICAL = _Path(__file__).resolve().parents[3] / "evals" / "agent_evals"
if not _CANONICAL.is_dir():
    raise ImportError(f"Canonical ReqPilotX Agent Eval package is missing: {_CANONICAL}")
__path__ = [str(_CANONICAL)]
