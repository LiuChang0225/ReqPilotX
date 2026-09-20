from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.adapters.agents.codex_app_server.atomization_app_server import (
    LocalCodexAppServerAtomizationDriver,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest,
    RequirementAtomizationSourceSegment,
)


TEXT = "The unit shall operate at 24 V; test method per Standard X."


def _request() -> RequirementAtomizationProviderRequest:
    return RequirementAtomizationProviderRequest(
        lineage_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
        review_node_id="review-node-app-server-canary",
        requirement_subtype="TECHNICAL_REQUIREMENT",
        source_segments=(
            RequirementAtomizationSourceSegment(
                segment_index=0,
                semantic_unit_id="semantic-unit-" + "a" * 32,
                owner_structure_node_id="structure-node-" + "b" * 32,
                source_start_offset=0,
                source_end_offset=len(TEXT),
                source_text=TEXT,
            ),
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex-reqpilotx",
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--timeout-seconds", type=float, default=360.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    driver = LocalCodexAppServerAtomizationDriver(
        codex_bin=args.codex_bin.resolve(strict=True),
        codex_home=args.codex_home.resolve(strict=True),
        repository_root=Path(__file__).resolve().parents[2],
        model_id=args.model,
        timeout_seconds=args.timeout_seconds,
    )
    result = driver.atomize_once(_request())
    print(
        json.dumps(
            {
                "schema_version": "codex-atomization-app-server-canary/v1",
                "status": "PASS",
                "candidate_sha256": result.candidate_sha256,
                "request_sha256": result.request_sha256,
                "tool_receipt_sha256": result.tool_receipt_sha256,
                "reference_status": result.reference.status.value,
                "obligation_count": len(result.reference.obligations),
                "context_range_count": len(result.reference.context_ranges),
                "grounding_sha256": result.validation_receipt.grounding_sha256,
                "decision_sha256": result.validation_receipt.decision_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
