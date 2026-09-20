#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.adapters.agents.codex_app_server.native_reference import (  # noqa: E402
    run_native_codex_reference,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one developer-only native Codex PDF reference observation."
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_native_codex_reference(
        args.pdf,
        args.output_dir,
        codex_binary=args.codex_binary,
        model_id=args.model,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "status": "OBSERVED",
                "source_sha256": result.receipt.source_sha256,
                "page_count": result.receipt.page_count,
                "observation_id": result.observation.observation_id,
                "elapsed_milliseconds": result.receipt.elapsed_milliseconds,
                "output_directory": str(args.output_dir.expanduser().resolve()),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
