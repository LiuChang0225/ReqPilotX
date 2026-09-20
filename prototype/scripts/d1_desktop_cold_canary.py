#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.codex_app_server.lifecycle import (  # noqa: E402
    DEFAULT_CODEX_BIN,
)
from app.adapters.agents.d1_desktop_cold_canary import (  # noqa: E402
    run_d1_desktop_cold_canary,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", type=Path, default=DEFAULT_CODEX_BIN)
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex-reqpilotx",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=PROTOTYPE_ROOT.parent,
    )
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args()
    result = run_d1_desktop_cold_canary(
        codex_bin=args.codex_bin,
        codex_home=args.codex_home,
        repository_root=args.repository_root,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
