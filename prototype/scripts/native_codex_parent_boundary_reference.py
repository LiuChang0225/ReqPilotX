#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.adapters.agents.codex_app_server.native_parent_boundary_reference import (  # noqa: E402
    run_native_codex_parent_boundary_reference,
)
from app.application.extraction.native_parent_boundary_reference import (  # noqa: E402
    NativeParentBoundaryRequestV1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one exact-cover native Codex provision-boundary reference."
    )
    parser.add_argument("request", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    request = NativeParentBoundaryRequestV1.model_validate_json(
        args.request.expanduser().resolve(strict=True).read_text(encoding="utf-8")
    )
    result = run_native_codex_parent_boundary_reference(
        request,
        args.output_dir,
        model_id=args.model,
        timeout_seconds=args.timeout_seconds,
    )
    print(
        json.dumps(
            {
                "status": result.reference.status.value,
                "scope_id": result.run_receipt.scope_id,
                "provision_count": result.run_receipt.provision_count,
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
