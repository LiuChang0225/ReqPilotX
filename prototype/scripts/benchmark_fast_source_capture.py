#!/usr/bin/env python3
"""Repeatable local-native capture benchmark over a private input manifest.

An input document has ``key``, ``source_path`` (or ``directory/source.pdf``),
and an optional ``source.sha256``. Customer text and results stay in the new
output directory. Artifact equality is a regression check, not semantic QA.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.application.fast_source_capture import capture_fast_source


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compare_capture(reference: Path, candidate: Path) -> dict[str, Any]:
    before, after = _load(reference), _load(candidate)
    before.pop("created_at", None)
    after.pop("created_at", None)
    return {
        "same_source": before["source_sha256"] == after["source_sha256"],
        "exact_source_ir_except_timestamp": before == after,
        "changed_fields": [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)],
        "meaning": "Regression conservation only; this does not establish original-document correctness.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--compare-dir", type=Path, help="Previous capture root containing <key>/source_ir.json")
    parser.add_argument("--keys", nargs="*", help="Optional document keys; input order is retained")
    parser.add_argument("--capture-only", action="store_true")
    args = parser.parse_args(argv)
    manifest = _load(args.manifest)
    documents = manifest["documents"]
    selected = [d for d in documents if not args.keys or d["key"] in args.keys]
    if not selected or len({d["key"] for d in selected}) != len(selected):
        parser.error("Select at least one document with unique keys")
    if any(not str(d["key"]).replace("-", "").replace("_", "").isalnum() for d in selected):
        parser.error("Document keys must be letters, digits, underscores or hyphens")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    results = []
    failed = False
    for document in selected:
        key = document["key"]
        source = document.get("source_path") or str(Path(document["directory"]) / "source.pdf")
        try:
            result = capture_fast_source(
                source,
                output_dir=args.output_dir / key,
                expected_sha256=document.get("source", {}).get("sha256"),
                include_structure=not args.capture_only,
            )
            receipt = {"key": key, "source_sha256": result["source"]["sha256"], "status": result["source_ir_status"], "timings_seconds": result["timings_seconds"], "gap_codes": [gap["code"] for gap in result["gaps"]]}
            if args.compare_dir:
                receipt["comparison"] = compare_capture(args.compare_dir / key / "source_ir.json", args.output_dir / key / "source_ir.json")
                failed = failed or not receipt["comparison"]["exact_source_ir_except_timestamp"]
        except (ValueError, OSError, RuntimeError) as exc:
            receipt = {"key": key, "error_type": type(exc).__name__, "message": str(exc)}
            failed = True
        results.append(receipt)
        print(json.dumps(receipt, ensure_ascii=False), flush=True)
    (args.output_dir / "benchmark.json").write_text(json.dumps({"schema_version": "reqpilotx-fast-source-benchmark/v1", "input_manifest": str(args.manifest.resolve()), "results": results, "qa_scope": "Measured runtime and exact SourceIR regression only. Original-page witnesses must be reviewed separately."}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
