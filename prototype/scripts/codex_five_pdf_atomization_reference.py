from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import unicodedata

from app.adapters.agents.codex_app_server.atomization_app_server import (
    CodexAppServerConsensusRequirementAtomizationProvider,
    LocalCodexAppServerAtomizationDriver,
)
from app.adapters.agents.codex_app_server.native_reference import (
    _write_private_json,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest,
    RequirementAtomizationRange,
)


SUMMARY_SCHEMA = "five-pdf-codex-app-server-atomization-reference/v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
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
    parser.add_argument("--repetition-count", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--slug", action="append", default=[])
    return parser


def main() -> int:
    args = _parser().parse_args()
    observation_root = args.observation_root.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(output_dir, 0o700)
    provider = CodexAppServerConsensusRequirementAtomizationProvider(
        LocalCodexAppServerAtomizationDriver(
            codex_bin=args.codex_bin.resolve(strict=True),
            codex_home=args.codex_home.resolve(strict=True),
            repository_root=Path(__file__).resolve().parents[2],
            model_id=args.model,
            timeout_seconds=args.timeout_seconds,
        ),
        repetition_count=args.repetition_count,
    )
    selected = set(args.slug)
    summaries: list[dict[str, Any]] = []
    for document_dir in sorted(
        item for item in observation_root.iterdir() if item.is_dir()
    ):
        if selected and document_dir.name not in selected:
            continue
        summaries.append(
            _run_document(
                document_dir,
                output_dir=output_dir,
                provider=provider,
            )
        )
        print(json.dumps(summaries[-1], sort_keys=True), flush=True)
    if selected - {item["slug"] for item in summaries}:
        raise ValueError("one or more selected slugs were not found")
    aggregate = {
        "schema_version": SUMMARY_SCHEMA,
        "status": (
            "PASS"
            if summaries and all(item["status"] == "SUCCESS" for item in summaries)
            else "PARTIAL"
        ),
        "document_count": len(summaries),
        "documents": summaries,
        "model_id": args.model,
        "repetition_count": args.repetition_count,
        "is_human_gold": False,
        "is_serving_activation_gate": False,
    }
    _write_private_json(output_dir / "summary.json", aggregate, exclusive=True)
    return 0 if aggregate["status"] == "PASS" else 2


def _run_document(
    document_dir: Path,
    *,
    output_dir: Path,
    provider: CodexAppServerConsensusRequirementAtomizationProvider,
) -> dict[str, Any]:
    target_dirs = tuple(
        sorted(document_dir.glob("atomization_references/*-native-parent"))
    )
    if len(target_dirs) != 1:
        raise ValueError("document atomization scope is not unique")
    target_dir = target_dirs[0]
    request = RequirementAtomizationProviderRequest.model_validate(
        _load_object(target_dir / "request.json")
    )
    previous = _load_object(target_dir / "consensus_reference.json")
    result = provider.atomize(request)
    app_hashes = tuple(
        sorted(
            _obligation_source_hash(request, item.source_ranges)
            for item in result.obligations
        )
    )
    previous_hashes = tuple(
        sorted(
            _obligation_source_hash(
                request,
                tuple(
                    RequirementAtomizationRange.model_validate(source_range)
                    for source_range in item["source_ranges"]
                ),
            )
            for item in previous["obligations"]
        )
    )
    target_output = output_dir / document_dir.name
    target_output.mkdir(mode=0o700)
    os.chmod(target_output, 0o700)
    payload = {
        "schema_version": "codex-app-server-atomization-document-reference/v1",
        "slug": document_dir.name,
        "review_node_id": request.review_node_id,
        "lineage_sha256": request.lineage_sha256,
        "provider_run": result.run.model_dump(mode="json"),
        "obligations": [item.model_dump(mode="json") for item in result.obligations],
        "context_ranges": [
            item.model_dump(mode="json") for item in result.context_ranges
        ],
        "obligation_source_sha256s": app_hashes,
        "previous_consensus_obligation_source_sha256s": previous_hashes,
        "exact_previous_consensus_match": app_hashes == previous_hashes,
        "is_human_gold": False,
        "is_serving_activation_gate": False,
    }
    _write_private_json(
        target_output / "atomization_reference.json",
        payload,
        exclusive=True,
    )
    return {
        "slug": document_dir.name,
        "status": result.run.status.value,
        "review_node_id_sha256": hashlib.sha256(
            request.review_node_id.encode()
        ).hexdigest(),
        "obligation_count": len(result.obligations),
        "context_range_count": len(result.context_ranges),
        "previous_consensus_obligation_count": len(previous_hashes),
        "exact_previous_consensus_match": app_hashes == previous_hashes,
        "request_sha256": result.run.request_sha256,
        "response_sha256": result.run.response_sha256,
    }


def _obligation_source_hash(
    request: RequirementAtomizationProviderRequest,
    ranges: tuple[RequirementAtomizationRange, ...],
) -> str:
    parts = []
    for source_range in ranges:
        segment = request.source_segments[source_range.segment_index]
        parts.append(
            segment.source_text[source_range.start_offset : source_range.end_offset]
        )
    normalized = " ".join(
        unicodedata.normalize("NFKC", " ".join(parts)).split()
    ).casefold()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
