#!/usr/bin/env python3
"""Compatibility entry for reqpilotx-capability."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "packages/engine/src"), str(ROOT / "apps/server/src")]

from reqpilotx_server.tools.prepare_native_loop_capability import main


if __name__ == "__main__":
    raise SystemExit(main())
