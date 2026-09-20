from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping

from app.adapters.agents.codex_app_server.native_reference import (
    _canonical_json_sha256,
    _write_private_json,
)
from app.adapters.agents.codex_app_server.semantic_app_server import (
    CodexAppServerConsensusSemanticProvider,
    LocalCodexAppServerSemanticDriver,
)
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.ports.semantic_provider import (
    SemanticClassificationRequest,
    SemanticEvidenceInput,
    SemanticImageInput,
    SemanticNodeInput,
)


SUMMARY_SCHEMA = "five-pdf-codex-app-server-semantic-reference/v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation-root", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
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
    source_dir = args.source_dir.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(output_dir, 0o700)
    driver = LocalCodexAppServerSemanticDriver(
        codex_bin=args.codex_bin.resolve(strict=True),
        codex_home=args.codex_home.resolve(strict=True),
        repository_root=Path(__file__).resolve().parents[2],
        model_id=args.model,
        timeout_seconds=args.timeout_seconds,
    )
    provider = CodexAppServerConsensusSemanticProvider(
        driver,
        repetition_count=args.repetition_count,
    )
    selected = set(args.slug)
    summaries: list[dict[str, Any]] = []
    for document_dir in sorted(
        item for item in observation_root.iterdir() if item.is_dir()
    ):
        if selected and document_dir.name not in selected:
            continue
        summary = _run_document(
            document_dir,
            source_dir=source_dir,
            output_dir=output_dir,
            provider=provider,
        )
        summaries.append(summary)
        print(json.dumps(summary, sort_keys=True), flush=True)
    if selected - {item["slug"] for item in summaries}:
        raise ValueError("one or more selected slugs were not found")
    aggregate = {
        "schema_version": SUMMARY_SCHEMA,
        "status": (
            "PASS"
            if summaries and all(item["status"] == "PASS" for item in summaries)
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
    source_dir: Path,
    output_dir: Path,
    provider: CodexAppServerConsensusSemanticProvider,
) -> dict[str, Any]:
    metadata = _load_object(document_dir / "metadata.json")
    filename = str(metadata["filename"])
    source_path = (source_dir / filename).resolve(strict=True)
    source_sha256 = _sha256_file(source_path)
    if source_sha256 != metadata["source_sha256"]:
        raise ValueError("source identity mismatch")
    parent_requests = tuple(
        sorted(document_dir.glob("parent_boundary_references/*/request.json"))
    )
    parent_references = tuple(
        sorted(
            document_dir.glob(
                "parent_boundary_references/*/native_parent_boundary_reference.json"
            )
        )
    )
    target_dirs = tuple(
        sorted(document_dir.glob("atomization_references/*-native-parent"))
    )
    if (
        len(parent_requests) != 1
        or len(parent_references) != 1
        or len(target_dirs) != 1
    ):
        raise ValueError("document semantic scope is not unique")
    parent_request = _load_object(parent_requests[0])
    parent_reference = _load_object(parent_references[0])
    target_label = target_dirs[0].name.removesuffix("-native-parent")
    request = _semantic_request(
        source_path=source_path,
        source_sha256=source_sha256,
        parent_request=parent_request,
        parent_reference=parent_reference,
    )
    result = provider.classify_nodes(request)
    decisions_by_target = {
        item.target_structure_node_id: item for item in result.decisions
    }
    provision_by_target = {
        _structure_id(source_sha256, str(item["provision_id"])): item
        for item in parent_reference["provisions"]
    }
    target = next(
        item
        for item in parent_reference["provisions"]
        if item["explicit_label"] == target_label
    )
    target_id = _structure_id(source_sha256, str(target["provision_id"]))
    target_decision = decisions_by_target[target_id]
    decomposition_path = target_dirs[0] / "consensus_reference.json"
    decomposition = _load_object(decomposition_path)
    semantic_counts = Counter(item.semantic_class.value for item in result.decisions)
    payload = {
        "schema_version": "codex-app-server-semantic-document-reference/v1",
        "slug": document_dir.name,
        "source_sha256": source_sha256,
        "scope_id": parent_reference["scope_id"],
        "parent_boundary_status": parent_reference["status"],
        "page_numbers": [item.page_number for item in request.images],
        "classification_status": result.run.status.value,
        "classification_request_sha256": result.run.request_sha256,
        "classification_response_sha256": result.run.response_sha256,
        "classification_provider_id": result.run.provider_id,
        "classification_provider_version": result.run.provider_version,
        "classification_prompt_version": result.run.prompt_version,
        "target_label": target_label,
        "target_structure_node_id": target_id,
        "target_semantic_class": target_decision.semantic_class.value,
        "target_requirement_subtype": (
            target_decision.requirement_subtype.value
            if target_decision.requirement_subtype is not None
            else None
        ),
        "target_evidence_ref_count": len(target_decision.evidence_source_refs),
        "scope_decision_count": len(result.decisions),
        "scope_semantic_class_counts": dict(sorted(semantic_counts.items())),
        "scope_decisions": [
            {
                "target_structure_node_id": item.target_structure_node_id,
                "explicit_label": provision_by_target[item.target_structure_node_id][
                    "explicit_label"
                ],
                "semantic_class": item.semantic_class.value,
                "requirement_subtype": (
                    item.requirement_subtype.value
                    if item.requirement_subtype is not None
                    else None
                ),
                "is_continuation_fragment": item.is_continuation_fragment,
                "evidence_ref_count": len(item.evidence_source_refs),
                "reason_codes": list(item.reason_codes),
            }
            for item in result.decisions
        ],
        "decomposition_reference_status": decomposition["status"],
        "decomposition_obligation_count": len(decomposition["obligations"]),
        "decomposition_context_count": len(decomposition["context_ranges"]),
        "decomposition_decision_sha256": _canonical_json_sha256(decomposition),
        "decomposition_transport": "codex-exec-consensus-reference",
        "decomposition_app_server_observed_in_this_run": False,
        "is_human_gold": False,
        "is_serving_activation_gate": False,
    }
    target_output = output_dir / document_dir.name
    target_output.mkdir(mode=0o700)
    os.chmod(target_output, 0o700)
    _write_private_json(
        target_output / "semantic_reference.json",
        payload,
        exclusive=True,
    )
    return {
        "slug": document_dir.name,
        "status": (
            "PASS"
            if target_decision.semantic_class.value == "REQUIREMENT"
            else "PARTIAL"
        ),
        "source_sha256": source_sha256,
        "page_count": len(request.images),
        "scope_decision_count": len(result.decisions),
        "target_label": target_label,
        "target_semantic_class": target_decision.semantic_class.value,
        "target_requirement_subtype": (
            target_decision.requirement_subtype.value
            if target_decision.requirement_subtype is not None
            else None
        ),
        "decomposition_reference_status": decomposition["status"],
        "decomposition_obligation_count": len(decomposition["obligations"]),
        "decomposition_app_server_observed_in_this_run": False,
        "classification_response_sha256": result.run.response_sha256,
    }


def _semantic_request(
    *,
    source_path: Path,
    source_sha256: str,
    parent_request: Mapping[str, Any],
    parent_reference: Mapping[str, Any],
) -> SemanticClassificationRequest:
    segments = parent_request["source_segments"]
    if not isinstance(segments, list) or not segments:
        raise ValueError("parent source segments missing")
    source_texts = [str(item["source_text"]) for item in segments]
    provisions = sorted(
        parent_reference["provisions"],
        key=lambda item: int(item["source_order"]),
    )
    nodes: list[SemanticNodeInput] = []
    for provision in provisions:
        target_id = _structure_id(source_sha256, str(provision["provision_id"]))
        ancestor_ids = tuple(
            _structure_id(source_sha256, "ancestor:" + str(label))
            for label in provision["ancestor_labels"]
        )
        evidence: list[SemanticEvidenceInput] = []
        refs: list[StructureSourceRef] = []
        for range_index, source_range in enumerate(provision["body_ranges"]):
            segment_index = int(source_range["segment_index"])
            start = int(source_range["start_offset"])
            end = int(source_range["end_offset"])
            text = source_texts[segment_index][start:end]
            source_ref = StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id="node-"
                + hashlib.sha256(
                    (
                        source_sha256
                        + str(provision["provision_id"])
                        + str(range_index)
                        + str(segment_index)
                        + str(start)
                        + str(end)
                    ).encode()
                ).hexdigest()[:32],
            )
            refs.append(source_ref)
            evidence.append(
                SemanticEvidenceInput(
                    source_ref=source_ref,
                    text=text,
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                )
            )
        nodes.append(
            SemanticNodeInput(
                target_structure_node_id=target_id,
                source_order=int(provision["source_order"]),
                kind=StructureNodeKind.CLAUSE,
                parent_structure_node_id=(ancestor_ids[-1] if ancestor_ids else None),
                ancestor_structure_node_ids=ancestor_ids,
                explicit_label=str(provision["explicit_label"]),
                image_page_numbers=tuple(
                    sorted({int(item["page_number"]) for item in segments})
                ),
                target_source_refs=tuple(refs),
                evidence=tuple(evidence),
            )
        )
    images = _render_pages(
        source_path,
        source_sha256=source_sha256,
        page_numbers=tuple(sorted({int(item["page_number"]) for item in segments})),
    )
    scope_ids = tuple(item.target_structure_node_id for item in nodes)
    scope_hash = _canonical_json_sha256(
        {
            "source_sha256": source_sha256,
            "scope_id": parent_reference["scope_id"],
            "targets": scope_ids,
        }
    )
    return SemanticClassificationRequest(
        source_ir_id="source-ir-" + source_sha256[:32],
        source_sha256=source_sha256,
        structure_snapshot_id="native-parent-scope-" + scope_hash[:32],
        scope_structure_node_ids=scope_ids,
        nodes=tuple(nodes),
        images=images,
    )


def _render_pages(
    source_path: Path,
    *,
    source_sha256: str,
    page_numbers: tuple[int, ...],
) -> tuple[SemanticImageInput, ...]:
    output: list[SemanticImageInput] = []
    with tempfile.TemporaryDirectory(prefix="reqpilotx-semantic-pages-") as raw:
        root = Path(raw).resolve()
        os.chmod(root, 0o700)
        for page_number in page_numbers:
            prefix = root / f"page-{page_number}"
            completed = subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-singlefile",
                    "-r",
                    "120",
                    "-png",
                    str(source_path),
                    str(prefix),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=120,
            )
            if completed.returncode != 0:
                raise RuntimeError("PDF_PAGE_RENDER_FAILED")
            image_path = prefix.with_suffix(".png")
            image_bytes = image_path.read_bytes()
            if len(image_bytes) < 24 or image_bytes[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("rendered page is not a PNG")
            output.append(
                SemanticImageInput(
                    source_sha256=source_sha256,
                    page_number=page_number,
                    width_px=int.from_bytes(image_bytes[16:20], "big"),
                    height_px=int.from_bytes(image_bytes[20:24], "big"),
                    image_bytes=image_bytes,
                    image_sha256=hashlib.sha256(image_bytes).hexdigest(),
                )
            )
    return tuple(output)


def _structure_id(source_sha256: str, value: str) -> str:
    return (
        "structure-node-"
        + hashlib.sha256((source_sha256 + "\0" + value).encode()).hexdigest()[:32]
    )


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
