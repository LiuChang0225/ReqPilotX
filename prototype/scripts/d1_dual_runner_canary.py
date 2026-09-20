#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.codex_app_server.lifecycle import (  # noqa: E402
    DEFAULT_CODEX_BIN,
)
from app.adapters.agents.d1_app_server_canary import (  # noqa: E402
    run_d1_app_server_canary,
)
from app.adapters.agents.d1_desktop_cold_canary import (  # noqa: E402
    run_d1_desktop_cold_canary,
)
from app.application.agent_workflow.d1_runner_parity import (  # noqa: E402
    compare_d1_runner_receipts,
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
    reference = run_d1_desktop_cold_canary(
        codex_bin=args.codex_bin,
        codex_home=args.codex_home,
        repository_root=args.repository_root,
        timeout_seconds=args.timeout_seconds,
    )
    product = run_d1_app_server_canary(
        codex_bin=args.codex_bin,
        codex_home=args.codex_home,
        repository_root=args.repository_root,
        timeout_seconds=args.timeout_seconds,
    )
    report = compare_d1_runner_receipts(reference, product)
    print(report.model_dump_json())
    return 0 if report.status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
