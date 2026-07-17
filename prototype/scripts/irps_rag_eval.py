from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.ai_copilot import (
    AICopilotConfig,
    AzureSearchSDKEvidenceRetriever,
    build_assessment_context,
    delete_azure_search_chunks_for_source_docs,
    load_ai_copilot_config,
    sync_knowledge_document_index,
)
from app.services.parser import parse_document_with_diagnostics


SEARCHABLE_INGESTIONS = {"searchable_knowledge", "searchable_hard_negative"}
SUPPORTED_INGESTIONS = SEARCHABLE_INGESTIONS | {
    "requirement_source",
    "do_not_index",
    "visual_eval_only",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate Azure AI Search retrieval against an isolated IRPS corpus."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("eval/irps_rag/irps_deepal_edh3c_v1.json"),
        help="Evaluation manifest relative to the prototype directory.",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="Root of the local IRPS source corpus. Source files never enter the repository.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/irps_rag/latest"),
        help="Directory for the JSON evaluation report.",
    )
    parser.add_argument("--top-k", type=int, default=3, help="Number of Azure Search evidence hits to evaluate.")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate corpus files, declared duplicates, and indexing scope without accessing Azure.",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parents[1]
    manifest_path = (project_dir / args.manifest).resolve()
    out_dir = (project_dir / args.out_dir).resolve()
    if args.preflight_only:
        report = run_corpus_preflight(
            manifest_path=manifest_path,
            source_root=args.source_root.resolve(),
        )
        _write_preflight_report(report, out_dir)
        print(f"[irps-rag-preflight] status: {report['status']}")
        print(f"[irps-rag-preflight] report: {out_dir / 'irps_rag_corpus_preflight.json'}")
        _ensure_corpus_ready(report)
        return

    report = run_evaluation(
        manifest_path=manifest_path,
        source_root=args.source_root.resolve(),
        out_dir=out_dir,
        top_k=args.top_k,
    )
    print(f"[irps-rag-eval] evaluated cases: {len(report['cases'])}")
    print(f"[irps-rag-eval] recall@1: {report['metrics']['recall_at_1']}")
    print(f"[irps-rag-eval] report: {(project_dir / args.out_dir).resolve() / 'irps_rag_evaluation.json'}")


def run_evaluation(
    *,
    manifest_path: Path,
    source_root: Path,
    out_dir: Path,
    top_k: int = 3,
    config: AICopilotConfig | None = None,
) -> dict[str, Any]:
    if top_k < 1:
        raise ValueError("top_k must be at least 1.")
    manifest = _load_manifest(manifest_path)
    preflight = _build_corpus_preflight(manifest=manifest, source_root=source_root)
    _ensure_corpus_ready(preflight)
    selected_config = config or load_ai_copilot_config("azure")
    _validate_azure_runtime(selected_config)

    searchable_documents = [
        item
        for item in manifest["documents"]
        if str(item.get("ingestion") or "") in SEARCHABLE_INGESTIONS
    ]
    if not searchable_documents:
        raise ValueError("Manifest does not contain searchable knowledge documents.")

    out_dir.mkdir(parents=True, exist_ok=True)
    project_id = f"irps-rag-eval-{uuid.uuid4().hex}"
    indexed_docs, source_doc_ids = _prepare_index_documents(
        searchable_documents=searchable_documents,
        source_root=source_root,
        project_id=project_id,
    )
    report: dict[str, Any] | None = None
    try:
        for knowledge_doc in indexed_docs:
            sync_result = sync_knowledge_document_index(project_id, knowledge_doc, config=selected_config)
            if str(sync_result.get("status") or "") != "READY":
                raise RuntimeError(
                    f"Azure indexing failed for {knowledge_doc['source_doc_id']}: "
                    f"{sync_result.get('error') or sync_result.get('status')}"
                )

        source_id_to_manifest_id = {
            str(doc["source_doc_id"]): str(doc["manifest_doc_id"])
            for doc in indexed_docs
        }
        retriever = AzureSearchSDKEvidenceRetriever(selected_config)
        case_results = [
            _evaluate_case(
                case=case,
                retriever=retriever,
                indexed_docs=indexed_docs,
                source_id_to_manifest_id=source_id_to_manifest_id,
                top_k=top_k,
            )
            for case in manifest["cases"]
        ]
        report = {
            "schema_version": "1.0",
            "corpus_id": manifest["corpus_id"],
            "run_id": project_id,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "manifest_path": str(manifest_path),
            "source_root": str(source_root),
            "retrieval_backend": selected_config.retrieval_backend,
            "azure_search_index_name": selected_config.azure_search_index_name,
            "top_k": top_k,
            "indexed_documents": [
                {
                    "id": doc["manifest_doc_id"],
                    "role": doc["role"],
                    "source_doc_id": doc["source_doc_id"],
                    "path": doc["relative_path"],
                    "text_length": len(doc["content"]),
                }
                for doc in indexed_docs
            ],
            "cases": case_results,
            "metrics": _build_metrics(case_results),
        }
    finally:
        delete_azure_search_chunks_for_source_docs(selected_config, source_doc_ids=source_doc_ids)
    if report is None:
        raise RuntimeError("IRPS RAG evaluation did not produce a report.")
    report["cleanup_completed"] = True
    _write_report(report, out_dir)
    return report


def run_corpus_preflight(*, manifest_path: Path, source_root: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    return _build_corpus_preflight(manifest=manifest, source_root=source_root)


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Evaluation manifest was not found: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Evaluation manifest must be a JSON object.")
    for key in ("corpus_id", "documents", "cases"):
        if not payload.get(key):
            raise ValueError(f"Evaluation manifest is missing '{key}'.")
    if not isinstance(payload["documents"], list) or not isinstance(payload["cases"], list):
        raise ValueError("Evaluation manifest documents and cases must be arrays.")
    return payload


def _validate_azure_runtime(config: AICopilotConfig) -> None:
    if config.retrieval_backend != "azure_sdk":
        raise RuntimeError("IRPS RAG evaluation requires retrieval_backend=azure_sdk.")
    if not config.azure_search_ready or not config.azure_openai_embedding_ready:
        raise RuntimeError(
            "Azure AI Search and Azure OpenAI embeddings must be configured before running IRPS RAG evaluation."
        )


def _build_corpus_preflight(*, manifest: dict[str, Any], source_root: Path) -> dict[str, Any]:
    resolved_source_root = source_root.resolve()
    documents: list[dict[str, Any]] = []
    document_ids: set[str] = set()
    issues: list[dict[str, str]] = []
    documents_by_id: dict[str, dict[str, Any]] = {}
    for manifest_doc in manifest["documents"]:
        document_id = str(manifest_doc.get("id") or "").strip()
        relative_path = str(manifest_doc.get("relative_path") or "").strip()
        ingestion = str(manifest_doc.get("ingestion") or "").strip()
        if not document_id:
            issues.append({"code": "missing_document_id", "document_id": ""})
            continue
        if document_id in document_ids:
            issues.append({"code": "duplicate_document_id", "document_id": document_id})
            continue
        document_ids.add(document_id)
        if not relative_path:
            issues.append({"code": "missing_relative_path", "document_id": document_id})
        if ingestion not in SUPPORTED_INGESTIONS:
            issues.append({"code": "unsupported_ingestion", "document_id": document_id})
        source_path = (resolved_source_root / relative_path).resolve()
        is_within_source_root = source_path.is_relative_to(resolved_source_root)
        if not is_within_source_root:
            issues.append({"code": "path_outside_source_root", "document_id": document_id})
        exists = is_within_source_root and source_path.is_file()
        document = {
            "id": document_id,
            "relative_path": relative_path,
            "ingestion": ingestion,
            "indexable": ingestion in SEARCHABLE_INGESTIONS,
            "exists": exists,
            "size_bytes": source_path.stat().st_size if exists else None,
            "sha256": _file_sha256(source_path) if exists else "",
            "duplicate_of": str(manifest_doc.get("duplicate_of") or "").strip(),
            "duplicate_verified": None,
        }
        if not exists:
            issues.append({"code": "missing_source_file", "document_id": document_id})
        documents.append(document)
        documents_by_id[document_id] = document

    for document in documents:
        duplicate_of = str(document["duplicate_of"])
        if not duplicate_of:
            continue
        source_document = documents_by_id.get(duplicate_of)
        if source_document is None:
            document["duplicate_verified"] = False
            issues.append({"code": "unknown_duplicate_target", "document_id": str(document["id"])})
            continue
        duplicate_verified = bool(
            document["exists"]
            and source_document["exists"]
            and document["sha256"] == source_document["sha256"]
        )
        document["duplicate_verified"] = duplicate_verified
        if not duplicate_verified:
            issues.append({"code": "duplicate_content_mismatch", "document_id": str(document["id"])})
        if document["indexable"]:
            issues.append({"code": "duplicate_marked_indexable", "document_id": str(document["id"])})

    indexable_document_ids = [str(document["id"]) for document in documents if document["indexable"]]
    ingestion_counts = Counter(str(document["ingestion"]) for document in documents)
    return {
        "schema_version": "1.0",
        "corpus_id": str(manifest["corpus_id"]),
        "source_root": str(source_root),
        "status": "READY" if not issues else "BLOCKED",
        "documents": documents,
        "indexable_document_ids": indexable_document_ids,
        "summary": {
            "total_documents": len(documents),
            "indexable_documents": len(indexable_document_ids),
            "do_not_index_documents": ingestion_counts["do_not_index"],
            "visual_eval_only_documents": ingestion_counts["visual_eval_only"],
            "missing_source_files": sum(not bool(document["exists"]) for document in documents),
            "declared_duplicates": sum(bool(document["duplicate_of"]) for document in documents),
            "verified_duplicates": sum(document["duplicate_verified"] is True for document in documents),
        },
        "issues": issues,
    }


def _ensure_corpus_ready(preflight: dict[str, Any]) -> None:
    if str(preflight.get("status") or "") == "READY":
        return
    issue_codes = [str(issue.get("code") or "unknown") for issue in preflight.get("issues") or []]
    detail = ", ".join(sorted(set(code.replace("_", " ") for code in issue_codes)))
    raise ValueError(f"IRPS RAG corpus preflight failed: {detail}.")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_index_documents(
    *,
    searchable_documents: list[dict[str, Any]],
    source_root: Path,
    project_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    indexed_docs: list[dict[str, Any]] = []
    source_doc_ids: list[str] = []
    for manifest_doc in searchable_documents:
        manifest_id = str(manifest_doc.get("id") or "").strip()
        relative_path = str(manifest_doc.get("relative_path") or "").strip()
        if not manifest_id or not relative_path:
            raise ValueError("Each searchable document needs an id and relative_path.")
        source_path = source_root / relative_path
        if not source_path.is_file():
            raise FileNotFoundError(f"Corpus source document was not found: {source_path}")
        parsed = parse_document_with_diagnostics(str(source_path))
        content = str(parsed.get("text") or "").strip()
        if not content:
            raise ValueError(f"No text was extracted from searchable document: {source_path}")
        source_doc_id = f"{project_id}-{manifest_id}"
        source_doc_ids.append(source_doc_id)
        indexed_docs.append(
            {
                "id": source_doc_id,
                "source_doc_id": source_doc_id,
                "manifest_doc_id": manifest_id,
                "name": source_path.name,
                "source_doc_name": source_path.name,
                "source_doc_version": "irps-rag-eval-v1",
                "doc_type": str(manifest_doc.get("doc_type") or "REFERENCE"),
                "project_id": project_id,
                "role": str(manifest_doc.get("role") or ""),
                "relative_path": relative_path,
                "content": content,
                "extracted_text": content,
                "segments": list(parsed.get("segments") or []),
            }
        )
    return indexed_docs, source_doc_ids


def _evaluate_case(
    *,
    case: dict[str, Any],
    retriever: AzureSearchSDKEvidenceRetriever,
    indexed_docs: list[dict[str, Any]],
    source_id_to_manifest_id: dict[str, str],
    top_k: int,
) -> dict[str, Any]:
    case_id = str(case.get("id") or "").strip()
    query = str(case.get("query") or "").strip()
    if not case_id or not query:
        raise ValueError("Each evaluation case needs an id and query.")
    context = build_assessment_context({"id": case_id, "raw_text": query})
    evidences = retriever.retrieve(context, indexed_docs, top_k)
    ranked_source_ids = [
        source_id_to_manifest_id.get(str(evidence.get("source_doc_id") or ""), "")
        for evidence in evidences
    ]
    ranked_source_ids = [item for item in ranked_source_ids if item]
    gold_ids = [str(item) for item in case.get("gold_source_doc_ids") or []]
    forbidden_ids = {str(item) for item in case.get("forbidden_source_doc_ids") or []}
    gold_rank = next((index + 1 for index, item in enumerate(ranked_source_ids) if item in gold_ids), None)
    forbidden_hits = [item for item in ranked_source_ids if item in forbidden_ids]
    scores = [_numeric_score(item.get("relevance")) for item in evidences]
    return {
        "id": case_id,
        "query": query,
        "expected_retrieval_grade": str(case.get("expected_retrieval_grade") or ""),
        "expected_decision": str(case.get("expected_decision") or ""),
        "gold_source_doc_ids": gold_ids,
        "forbidden_source_doc_ids": sorted(forbidden_ids),
        "ranked_source_doc_ids": ranked_source_ids,
        "gold_rank": gold_rank,
        "forbidden_hits": forbidden_hits,
        "top1_margin": scores[0] - scores[1] if len(scores) >= 2 else None,
        "top3_score_spread": max(scores[:3]) - min(scores[:3]) if len(scores) >= 2 else None,
        "evidences": [
            {
                "source_doc_id": source_id_to_manifest_id.get(str(item.get("source_doc_id") or ""), ""),
                "chunk_id": str(item.get("chunk_id") or ""),
                "score": _numeric_score(item.get("relevance")),
                "score_source": str(item.get("score_source") or ""),
                "relevance_class": _evidence_relevance_class(
                    source_id_to_manifest_id.get(str(item.get("source_doc_id") or ""), ""),
                    gold_ids=gold_ids,
                    forbidden_ids=forbidden_ids,
                ),
            }
            for item in evidences
        ],
    }


def _build_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    cases_with_gold = [item for item in cases if item["gold_source_doc_ids"]]
    visual_only_cases = [item for item in cases if item["expected_retrieval_grade"] == "visual_only"]
    recall_at_1 = _ratio(sum(item["gold_rank"] == 1 for item in cases_with_gold), len(cases_with_gold))
    recall_at_3 = _ratio(
        sum(item["gold_rank"] is not None and item["gold_rank"] <= 3 for item in cases_with_gold),
        len(cases_with_gold),
    )
    mrr = _ratio(
        sum(1 / item["gold_rank"] for item in cases_with_gold if item["gold_rank"] is not None),
        len(cases_with_gold),
    )
    all_hits = [hit for item in cases for hit in item["ranked_source_doc_ids"]]
    score_source_counts = Counter(
        evidence["score_source"]
        for item in cases
        for evidence in item["evidences"]
        if evidence["score_source"]
    )
    scores_by_relevance_class: dict[str, list[float]] = {}
    for item in cases:
        for evidence in item["evidences"]:
            scores_by_relevance_class.setdefault(evidence["relevance_class"], []).append(evidence["score"])
    return {
        "retrieval_cases_with_gold": len(cases_with_gold),
        "recall_at_1": recall_at_1,
        "recall_at_3": recall_at_3,
        "mrr": mrr,
        "wrong_evidence_rate": _ratio(sum(bool(item["forbidden_hits"]) for item in cases), len(cases)),
        "hard_negative_citation_rate": _ratio(
            sum(
                any(source_id in item["forbidden_source_doc_ids"] for source_id in item["ranked_source_doc_ids"])
                for item in cases
            ),
            len(cases),
        ),
        "visual_only_false_positive_rate": _ratio(
            sum(bool(item["ranked_source_doc_ids"]) for item in visual_only_cases),
            len(visual_only_cases),
        ),
        "score_source_counts": dict(sorted(score_source_counts.items())),
        "score_distribution": {
            relevance_class: _score_summary(scores)
            for relevance_class, scores in sorted(scores_by_relevance_class.items())
        },
        "retrieved_source_doc_counts": dict(sorted(Counter(all_hits).items())),
        "top1_margin_median": _median([item["top1_margin"] for item in cases if item["top1_margin"] is not None]),
        "top3_score_spread_median": _median(
            [item["top3_score_spread"] for item in cases if item["top3_score_spread"] is not None]
        ),
    }


def _write_report(report: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "irps_rag_evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_preflight_report(report: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "irps_rag_corpus_preflight.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _numeric_score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _evidence_relevance_class(
    source_doc_id: str,
    *,
    gold_ids: list[str],
    forbidden_ids: set[str],
) -> str:
    if source_doc_id in gold_ids:
        return "gold"
    if source_doc_id in forbidden_ids:
        return "hard_negative"
    return "other"


def _score_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "min": round(min(values), 6),
        "median": round(statistics.median(values), 6),
        "max": round(max(values), 6),
    }


def _ratio(numerator: float, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 6) if values else None


if __name__ == "__main__":
    main()
