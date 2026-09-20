#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.application.extraction.codex_extraction_parity import (  # noqa: E402
    CodexExtractionObservationV1,
    compare_codex_extraction_observations,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare one native/Codex draft extraction observation with the "
            "same-source ReqPilotX observation."
        )
    )
    parser.add_argument("reference", type=Path)
    parser.add_argument("reqpilotx", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reference = _load_observation(args.reference)
    reqpilotx = _load_observation(args.reqpilotx)
    report = compare_codex_extraction_observations(reference, reqpilotx)
    _write_private_json(args.output, report.model_dump(mode="json"))
    print(
        json.dumps(
            {
                "status": report.status.value,
                "source_sha256": report.source_sha256,
                "first_gap_stage": (
                    report.first_gap_stage.value if report.first_gap_stage else None
                ),
                "output": str(args.output.expanduser().resolve()),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _load_observation(path: Path) -> CodexExtractionObservationV1:
    resolved = path.expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    return CodexExtractionObservationV1.model_validate(payload)


def _write_private_json(path: Path, payload: object) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(resolved.parent, 0o700)
    descriptor = os.open(
        resolved,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
