#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.adapters.agents.codex_app_server.native_atomization_reference import (  # noqa: E402
    run_native_codex_atomization_reference,
)
from app.ports.requirement_atomization_provider import (  # noqa: E402
    RequirementAtomizationProviderRequest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one exact-range native Codex atomization reference."
    )
    parser.add_argument("request", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request = RequirementAtomizationProviderRequest.model_validate_json(
        args.request.expanduser().resolve(strict=True).read_text(encoding="utf-8")
    )
    result = run_native_codex_atomization_reference(
        request,
        args.output_dir,
        codex_binary=args.codex_binary,
        model_id=args.model,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "status": result.reference.status.value,
                "lineage_sha256": result.run_receipt.lineage_sha256,
                "review_node_id": result.run_receipt.review_node_id,
                "obligation_count": result.run_receipt.obligation_count,
                "context_range_count": result.run_receipt.context_range_count,
                "elapsed_milliseconds": result.run_receipt.elapsed_milliseconds,
                "output_directory": str(args.output_dir.expanduser().resolve()),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
