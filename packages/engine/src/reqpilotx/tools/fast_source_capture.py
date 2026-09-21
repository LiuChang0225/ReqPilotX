#!/usr/bin/env python3
"""Capture native evidence with the application's algorithms and no models."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from reqpilotx.application.fast_source_capture import capture_fast_source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path, help="New private output directory; never overwritten")
    parser.add_argument("--expected-sha256", help="Reject a changed or wrongly bound source before extraction")
    parser.add_argument("--capture-only", action="store_true", help="Omit deterministic structure proposal and inspection")
    parser.add_argument("--progress", action="store_true", help="Write source-page progress as JSON lines to stderr")
    args = parser.parse_args(argv)

    def progress(completed: int, total: int, current: int) -> None:
        print(json.dumps({"event": "capture_progress", "completed_pages": completed, "total_pages": total, "current_page": current}), file=sys.stderr, flush=True)

    try:
        # Some native libraries print informational banners. stdout is reserved
        # for the final machine-readable receipt, stderr for native diagnostics.
        with contextlib.redirect_stdout(sys.stderr):
            result = capture_fast_source(
                args.source,
                output_dir=args.output_dir,
                expected_sha256=args.expected_sha256,
                include_structure=not args.capture_only,
                progress=progress if args.progress else None,
            )
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"event": "capture_rejected", "error_type": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"schema_version": result["schema_version"], "manifest": str(args.output_dir.resolve() / "manifest.json"), "source_sha256": result["source"]["sha256"], "source_ir_status": result["source_ir_status"], "semantic_status": result["semantic_status"], "timings_seconds": result["timings_seconds"], "gap_codes": [gap["code"] for gap in result["gaps"]]}, ensure_ascii=False))
    return 2 if result["source_ir_status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
