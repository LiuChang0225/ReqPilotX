#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import urlopen

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.adapters.agents.codex_app_server.native_reference import (
    _write_private_json,
)
from app.adapters.agents.codex_app_server.native_source_item_text_reference import (
    NativeSourceItemTextRunReceiptV1,
    SourceItemTextBatchCandidateV1,
    run_native_source_item_text_reference,
)
from app.adapters.agents.codex_app_server.native_source_requirement_reference import (
    NativeSourceRequirementRunReceiptV1,
    run_native_source_requirement_slice_reference,
)
from app.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    run_native_source_requirement_windowed_reference,
)
from app.application.agent_workflow.source_requirement_baseline import (
    SourceItemTextScopeRequestV1,
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    SourceRequirementScopeRequestV1,
    SourceRequirementSliceCandidateV1,
    append_source_requirement_baseline,
    build_source_item_text_scope_request,
    build_source_requirement_scope_request,
    compile_document_map_artifact,
    compile_source_requirement_baseline,
    validate_source_item_text_slice_candidate,
    validate_source_requirement_slice_candidate,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one or more native Codex Source Requirement Record slices against "
            "an accepted ReqPilotX Document Map."
        )
    )
    parser.add_argument("--api-root", required=True)
    parser.add_argument("--scope-id", action="append", default=[])
    parser.add_argument("--text-node-id", action="append", default=[])
    parser.add_argument("--reuse-scope-dir", action="append", type=Path, default=[])
    parser.add_argument("--reuse-text-dir", action="append", type=Path, default=[])
    parser.add_argument("--previous-baseline", type=Path)
    parser.add_argument("--previous-validation-receipt", type=Path)
    parser.add_argument("--append-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--agent-kit-dir",
        type=Path,
        default=PROJECT_DIR.parent / "agent-kit",
    )
    parser.add_argument("--source-pdf", type=Path)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument(
        "--execution-profile",
        choices=("legacy-full-context", "compact-adaptive"),
        default="compact-adaptive",
    )
    parser.add_argument(
        "--execution-strategy",
        choices=("single-turn", "boundary-windows"),
        default="single-turn",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        choices=(1, 2, 3, 4),
        default=2,
    )
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()

    api_root = args.api_root.rstrip("/")
    _require_local_api(api_root)
    output = args.output_dir.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("source-requirement output directory must be empty")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)

    previous_artifact: SourceRequirementBaselineArtifactV1 | None = None
    previous_validation_receipt: SourceRequirementBaselineValidationReceiptV1 | None = (
        None
    )
    if args.previous_baseline is not None:
        previous_artifact = SourceRequirementBaselineArtifactV1.model_validate_json(
            args.previous_baseline.expanduser()
            .resolve(strict=True)
            .read_text(encoding="utf-8")
        )
    if args.previous_validation_receipt is not None:
        previous_validation_receipt = (
            SourceRequirementBaselineValidationReceiptV1.model_validate_json(
                args.previous_validation_receipt.expanduser()
                .resolve(strict=True)
                .read_text(encoding="utf-8")
            )
        )
    if args.append_only and (
        previous_artifact is None or previous_validation_receipt is None
    ):
        raise ValueError(
            "append-only requires previous baseline and validation receipt"
        )

    public_reference = _read_json_url(api_root)
    document_map = compile_document_map_artifact(public_reference)
    if previous_artifact is not None and (
        previous_artifact.source_sha256 != document_map.source_sha256
        or previous_artifact.document_map_artifact_id != document_map.artifact_id
        or previous_artifact.document_map_artifact_sha256
        != document_map.artifact_sha256
    ):
        raise ValueError("previous source-requirement baseline lineage drifted")
    requests = []
    candidates = []
    text_requests: list[SourceItemTextScopeRequestV1] = []
    text_candidates = []
    run_receipts = []
    reused_scope_ids: set[str] = set()
    for raw_directory in args.reuse_scope_dir:
        directory = raw_directory.expanduser().resolve(strict=True)
        request = SourceRequirementScopeRequestV1.model_validate_json(
            (directory / "scope-request.json").read_text(encoding="utf-8")
        )
        candidate = SourceRequirementSliceCandidateV1.model_validate_json(
            (directory / "candidate.json").read_text(encoding="utf-8")
        )
        receipt = NativeSourceRequirementRunReceiptV1.model_validate_json(
            (directory / "run-receipt.json").read_text(encoding="utf-8")
        )
        if (
            request.document_map_artifact_id != document_map.artifact_id
            or request.document_map_artifact_sha256 != document_map.artifact_sha256
            or receipt.request_sha256 != request.request_sha256
            or receipt.candidate_sha256
            != hashlib.sha256(
                json.dumps(
                    candidate.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        ):
            raise ValueError("reused source-requirement scope lineage drifted")
        validate_source_requirement_slice_candidate(request, candidate)
        scope_output = (
            output / "scopes" / _safe_scope_directory(request.logical_table_scope_id)
        )
        scope_output.mkdir(parents=True, exist_ok=True, mode=0o700)
        for filename, payload in (
            ("scope-request.json", request.model_dump(mode="json")),
            ("candidate.json", candidate.model_dump(mode="json")),
            ("run-receipt.json", receipt.model_dump(mode="json")),
        ):
            _write_private_json(scope_output / filename, payload, exclusive=True)
        requests.append(request)
        candidates.append(candidate)
        run_receipts.append(receipt)
        reused_scope_ids.add(request.logical_table_scope_id)
    requested_scope_ids = tuple(dict.fromkeys(args.scope_id))
    requested_text_node_ids = tuple(dict.fromkeys(args.text_node_id))
    if (
        not requested_scope_ids
        and not reused_scope_ids
        and not requested_text_node_ids
        and not args.reuse_text_dir
    ):
        raise ValueError(
            "at least one table/text scope or reusable directory is required"
        )
    for scope_id in requested_scope_ids:
        if scope_id in reused_scope_ids:
            continue
        table_preview = _read_json_url(f"{api_root}/tables/{quote(scope_id, safe='')}")
        request = build_source_requirement_scope_request(document_map, table_preview)
        scope_output = output / "scopes" / _safe_scope_directory(scope_id)
        if args.execution_strategy == "boundary-windows":
            result = run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=scope_output,
                model_id=args.model,
                reasoning_effort=args.reasoning_effort,
                timeout_seconds=args.timeout_seconds,
                max_concurrency=args.max_concurrency,
                agent_kit_directory=args.agent_kit_dir,
                source_pdf_path=args.source_pdf,
            )
        else:
            result = run_native_source_requirement_slice_reference(
                document_map=document_map,
                request=request,
                output_directory=scope_output,
                model_id=args.model,
                reasoning_effort=args.reasoning_effort,
                timeout_seconds=args.timeout_seconds,
                agent_kit_directory=args.agent_kit_dir,
                source_pdf_path=args.source_pdf,
                execution_profile=args.execution_profile,
            )
        requests.append(request)
        candidates.append(result.candidate)
        run_receipts.append(result.run_receipt)

    reused_text_scope_ids: set[str] = set()
    for batch_index, raw_text_directory in enumerate(args.reuse_text_dir, start=1):
        text_directory = raw_text_directory.expanduser().resolve(strict=True)
        raw_requests = json.loads(
            (text_directory / "text-scope-requests.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw_requests, list):
            raise TypeError("reused source-item text requests must be an array")
        batch_requests = [
            SourceItemTextScopeRequestV1.model_validate(item) for item in raw_requests
        ]
        batch = SourceItemTextBatchCandidateV1.model_validate_json(
            (text_directory / "candidate-batch.json").read_text(encoding="utf-8")
        )
        text_receipt = NativeSourceItemTextRunReceiptV1.model_validate_json(
            (text_directory / "run-receipt.json").read_text(encoding="utf-8")
        )
        if (
            batch.document_map_artifact_id != document_map.artifact_id
            or batch.document_map_artifact_sha256 != document_map.artifact_sha256
            or text_receipt.document_map_artifact_id != document_map.artifact_id
            or text_receipt.candidate_sha256
            != hashlib.sha256(
                json.dumps(
                    batch.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        ):
            raise ValueError("reused source-item text lineage drifted")
        batch_candidates = list(batch.candidates)
        batch_scope_ids = {request.scope_id for request in batch_requests}
        if reused_text_scope_ids.intersection(batch_scope_ids):
            raise ValueError("reused source-item text scopes overlap")
        reused_text_scope_ids.update(batch_scope_ids)
        for request, candidate in zip(batch_requests, batch_candidates, strict=True):
            validate_source_item_text_slice_candidate(request, candidate)
        text_requests.extend(batch_requests)
        text_candidates.extend(batch_candidates)
        text_output = output / "text-scopes" / f"batch-{batch_index:02d}"
        text_output.mkdir(parents=True, exist_ok=True, mode=0o700)
        for filename, payload in (
            (
                "text-scope-requests.json",
                [item.model_dump(mode="json") for item in batch_requests],
            ),
            ("candidate-batch.json", batch.model_dump(mode="json")),
            ("run-receipt.json", text_receipt.model_dump(mode="json")),
        ):
            _write_private_json(text_output / filename, payload, exclusive=True)
        run_receipts.append(text_receipt)

    if requested_text_node_ids:
        new_text_requests = [
            build_source_item_text_scope_request(document_map, node_id=node_id)
            for node_id in requested_text_node_ids
        ]
        new_scope_ids = {request.scope_id for request in new_text_requests}
        if reused_text_scope_ids.intersection(new_scope_ids):
            raise ValueError("requested source-item text scopes overlap reused scopes")
        text_output = (
            output / "text-scopes" / f"batch-{len(args.reuse_text_dir) + 1:02d}"
        )
        text_result = run_native_source_item_text_reference(
            document_map=document_map,
            requests=tuple(new_text_requests),
            output_directory=text_output,
            model_id=args.model,
            reasoning_effort=args.reasoning_effort,
            timeout_seconds=args.timeout_seconds,
            agent_kit_directory=args.agent_kit_dir,
            source_pdf_path=args.source_pdf,
        )
        text_requests.extend(new_text_requests)
        text_candidates.extend(text_result.batch.candidates)
        run_receipts.append(text_result.run_receipt)

    append_receipt = None
    if args.append_only:
        assert previous_artifact is not None
        assert previous_validation_receipt is not None
        artifact, validation_receipt, append_receipt = (
            append_source_requirement_baseline(
                document_map=document_map,
                previous_artifact=previous_artifact,
                previous_validation_receipt=previous_validation_receipt,
                requests=tuple(requests),
                candidates=tuple(candidates),
                text_requests=tuple(text_requests),
                text_candidates=tuple(text_candidates),
            )
        )
    else:
        artifact, validation_receipt = compile_source_requirement_baseline(
            document_map=document_map,
            requests=tuple(requests),
            candidates=tuple(candidates),
            text_requests=tuple(text_requests),
            text_candidates=tuple(text_candidates),
            previous_artifact=previous_artifact,
            status="SLICE_COMPLETE",
        )
    payloads = {
        "document-map.json": document_map.model_dump(mode="json"),
        "source-requirement-baseline.json": artifact.model_dump(mode="json"),
        "validation-receipt.json": validation_receipt.model_dump(mode="json"),
        "run-receipts.json": [item.model_dump(mode="json") for item in run_receipts],
    }
    if append_receipt is not None:
        payloads["append-receipt.json"] = append_receipt.model_dump(mode="json")
    for filename, payload in payloads.items():
        _write_private_json(output / filename, payload, exclusive=True)
    files = []
    for path in sorted(
        item for item in output.rglob("*.json") if item.name != "manifest.json"
    ):
        encoded = path.read_bytes()
        files.append(
            {
                "path": str(path.relative_to(output)),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "size": len(encoded),
            }
        )
    manifest = {
        "schema_version": "reqpilotx-source-requirement-reference-manifest/v1",
        "source_sha256": artifact.source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "document_map_artifact_sha256": document_map.artifact_sha256,
        "baseline_artifact_id": artifact.artifact_id,
        "baseline_artifact_sha256": artifact.artifact_sha256,
        "baseline_revision": artifact.revision,
        "predecessor_artifact_id": artifact.predecessor_artifact_id,
        "predecessor_artifact_sha256": artifact.predecessor_artifact_sha256,
        "covered_scope_ids": list(artifact.covered_scope_ids),
        "status": artifact.status,
        "authority": "AI_REVIEWED_DEVELOPMENT_REFERENCE_NOT_GOLD",
        "is_serving_activation_gate": False,
        "append_only": args.append_only,
        "append_receipt_sha256": (
            append_receipt.receipt_sha256 if append_receipt is not None else ""
        ),
        "files": files,
    }
    _write_private_json(output / "manifest.json", manifest, exclusive=True)
    print(
        json.dumps(
            {
                "status": artifact.status,
                "document_map_artifact_id": document_map.artifact_id,
                "baseline_artifact_id": artifact.artifact_id,
                "baseline_revision": artifact.revision,
                "predecessor_artifact_id": artifact.predecessor_artifact_id,
                "covered_scope_ids": list(artifact.covered_scope_ids),
                "record_count": len(artifact.records),
                "requirement_count": validation_receipt.requirement_count,
                "non_requirement_count": validation_receipt.non_requirement_count,
                "output_directory": str(output),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _read_json_url(url: str) -> dict[str, object]:
    _require_local_api(url)
    with urlopen(url, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"ReqPilotX API returned HTTP {response.status}")
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise TypeError("ReqPilotX API response must be an object")
    return payload


def _require_local_api(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError(
            "source-requirement reference runner accepts localhost HTTP only"
        )


def _safe_scope_directory(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in value
    ).strip("-")
    if not normalized:
        raise ValueError("source-requirement scope ID is invalid")
    return normalized[:160]


if __name__ == "__main__":
    raise SystemExit(main())
    append_source_requirement_baseline,
