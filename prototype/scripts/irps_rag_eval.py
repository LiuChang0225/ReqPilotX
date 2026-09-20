from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from app.domain.knowledge.lifecycle import (
    ACTIVE_KNOWLEDGE_STATUS,
    evaluate_knowledge_evidence_eligibility,
)
from app.services.ai_copilot import (
    AICopilotConfig,
    AzureSearchSDKEvidenceRetriever,
    _azure_filter_to_odata,
    _azure_search_credential,
    build_assessment_context,
    build_knowledge_chunks,
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
REPORT_SCHEMA_VERSION = "irps_rag_evaluation.v2"
REPORT_IDENTIFIER_NAMESPACE = "reqpilotx-irps-rag-report.v2"
REQUIRES_HUMAN_REVIEW = "REQUIRES_HUMAN_REVIEW"
QUALITY_THRESHOLD_FLOORS: dict[str, float] = {
    "recall_at_1_min": 0.80,
    "recall_at_3_min": 0.95,
    "mrr_min": 0.85,
    "wrong_evidence_rate_max": 0.0,
    "hard_negative_retrieval_rate_max": 0.0,
    "visual_only_false_positive_rate_max": 0.0,
}
MINIMUM_QUALITY_CASES = 20
MINIMUM_GOLD_RETRIEVAL_CASES = 15
MINIMUM_VISUAL_ONLY_CASES = 1
CLEANUP_VERIFICATION_ATTEMPTS = 3
CLEANUP_VERIFICATION_DELAY_SECONDS = 0.25
EXPECTED_RETRIEVAL_GRADES = {"direct", "partial", "visual_only"}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate Azure AI Search retrieval against an isolated IRPS corpus."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
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
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help="Exit non-zero unless the frozen/approved retrieval quality gate passes.",
    )
    args = parser.parse_args()
    if args.preflight_only and args.require_pass:
        parser.error("--require-pass cannot be combined with --preflight-only.")

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
    print(f"[irps-rag-eval] quality cases: {report['metrics']['quality_case_count']}")
    print(f"[irps-rag-eval] recall@1: {report['metrics']['recall_at_1']}")
    print(f"[irps-rag-eval] overall pass: {report['quality_gate']['overall_pass']}")
    print(f"[irps-rag-eval] report: {(project_dir / args.out_dir).resolve() / 'irps_rag_evaluation.json'}")
    if args.require_pass:
        _ensure_quality_pass(report)


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
    cleanup_error: Exception | None = None
    cleanup_confirmed = False
    try:
        for knowledge_doc in indexed_docs:
            _require_indexable_knowledge_document(
                project_id=project_id,
                knowledge_doc=knowledge_doc,
                phase="before indexing",
            )
            sync_result = sync_knowledge_document_index(project_id, knowledge_doc, config=selected_config)
            _require_ready_index_sync(knowledge_doc=knowledge_doc, sync_result=sync_result)

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
        metrics = _build_metrics(case_results)
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "evaluation_scope": {
                "kind": "RETRIEVAL_ONLY",
                "included": [
                    "retrieval_ranking",
                    "hard_negative_retrieval",
                    "score_distribution",
                ],
                "excluded": [
                    "assessment_decision_quality",
                    "evidence_citation_quality",
                    "llm_output_quality",
                ],
            },
            "corpus_key": _stable_report_identifier("corpus", manifest["corpus_id"]),
            "run_key": _stable_report_identifier("run", project_id),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "retrieval_backend": selected_config.retrieval_backend,
            "top_k": top_k,
            "indexed_documents": [
                {
                    "document_key": _stable_report_identifier(
                        "document",
                        doc["manifest_doc_id"],
                    ),
                    "role": doc["role"],
                }
                for doc in indexed_docs
            ],
            "cases": case_results,
            "metrics": metrics,
            "quality_gate": _build_quality_gate(manifest=manifest, metrics=metrics),
        }
    finally:
        try:
            delete_azure_search_chunks_for_source_docs(
                selected_config,
                source_doc_ids=source_doc_ids,
            )
            cleanup_confirmed = _verify_azure_search_cleanup(
                selected_config,
                source_doc_ids=source_doc_ids,
            )
            if not cleanup_confirmed:
                cleanup_error = RuntimeError(
                    "Azure cleanup verification still found isolated evaluation chunks."
                )
        except Exception as exc:  # Cleanup is a release-safety gate, not best effort.
            cleanup_error = exc
    if report is None:
        if cleanup_error is not None:
            raise RuntimeError(
                f"IRPS RAG evaluation cleanup was not confirmed: {cleanup_error}"
            ) from cleanup_error
        raise RuntimeError("IRPS RAG evaluation did not produce a report.")
    report["cleanup"] = {
        "attempted": True,
        "confirmed": cleanup_confirmed,
        "status": "CONFIRMED" if cleanup_confirmed else "UNCONFIRMED",
    }
    report["cleanup_completed"] = cleanup_confirmed
    if not cleanup_confirmed:
        report["quality_gate"]["overall_pass"] = False
        report["quality_gate"]["blockers"] = sorted(
            {
                *report["quality_gate"]["blockers"],
                "cleanup_not_confirmed",
            }
        )
    _write_report(report, out_dir)
    if cleanup_error is not None:
        raise RuntimeError(
            f"IRPS RAG evaluation cleanup was not confirmed: {cleanup_error}"
        ) from cleanup_error
    return report


def run_corpus_preflight(*, manifest_path: Path, source_root: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    report = _build_corpus_preflight(manifest=manifest, source_root=source_root)
    return _sanitize_preflight_report(report)


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
    _validate_manifest_reference_contract(payload)
    return payload


def _validate_manifest_reference_contract(manifest: dict[str, Any]) -> None:
    documents_by_id: dict[str, dict[str, Any]] = {}
    for index, document in enumerate(manifest["documents"]):
        if not isinstance(document, dict):
            raise ValueError(f"Manifest document {index} must be an object.")
        document_id = str(document.get("id") or "").strip()
        ingestion = str(document.get("ingestion") or "").strip()
        if not document_id:
            raise ValueError(f"Manifest document {index} has no id.")
        if document_id in documents_by_id:
            raise ValueError(f"Manifest document id is duplicated: {document_id}.")
        if ingestion not in SUPPORTED_INGESTIONS:
            raise ValueError(
                f"Manifest document {document_id} has unsupported ingestion: {ingestion}."
            )
        documents_by_id[document_id] = document

    searchable_ids = {
        document_id
        for document_id, document in documents_by_id.items()
        if str(document.get("ingestion") or "") in SEARCHABLE_INGESTIONS
    }
    hard_negative_ids = {
        document_id
        for document_id, document in documents_by_id.items()
        if str(document.get("ingestion") or "") == "searchable_hard_negative"
    }
    seen_case_ids: set[str] = set()
    for index, case in enumerate(manifest["cases"]):
        if not isinstance(case, dict):
            raise ValueError(f"Manifest case {index} must be an object.")
        case_id = str(case.get("id") or "").strip()
        query = str(case.get("query") or "").strip()
        grade = str(case.get("expected_retrieval_grade") or "").strip()
        status = str(case.get("status") or "").strip().upper()
        if not case_id or not query:
            raise ValueError(f"Manifest case {index} needs a non-empty id and query.")
        if case_id in seen_case_ids:
            raise ValueError(f"Manifest case id is duplicated: {case_id}.")
        seen_case_ids.add(case_id)
        if grade not in EXPECTED_RETRIEVAL_GRADES:
            raise ValueError(
                f"Manifest case {case_id} has unsupported retrieval grade: {grade}."
            )

        gold_ids = _manifest_case_document_ids(
            case,
            field="gold_source_doc_ids",
            case_id=case_id,
        )
        forbidden_ids = _manifest_case_document_ids(
            case,
            field="forbidden_source_doc_ids",
            case_id=case_id,
        )
        unknown_ids = (gold_ids | forbidden_ids) - set(documents_by_id)
        if unknown_ids:
            raise ValueError(
                f"Manifest case {case_id} references unknown documents."
            )
        if gold_ids & forbidden_ids:
            raise ValueError(
                f"Manifest case {case_id} overlaps Gold and forbidden documents."
            )
        invalid_gold_ids = {
            document_id
            for document_id in gold_ids
            if str(documents_by_id[document_id].get("ingestion") or "")
            != "searchable_knowledge"
        }
        if invalid_gold_ids:
            raise ValueError(
                f"Manifest case {case_id} uses a non-knowledge document as Gold."
            )
        invalid_forbidden_ids = forbidden_ids - searchable_ids
        if invalid_forbidden_ids:
            raise ValueError(
                f"Manifest case {case_id} forbids a document that is not indexed."
            )

        if grade == "visual_only":
            if gold_ids:
                raise ValueError(
                    f"Visual-only manifest case {case_id} must not declare text Gold."
                )
            if forbidden_ids != searchable_ids:
                raise ValueError(
                    f"Visual-only manifest case {case_id} must forbid every searchable document."
                )
        elif not gold_ids:
            raise ValueError(
                f"Manifest case {case_id} requires at least one searchable Gold document."
            )

        if status != REQUIRES_HUMAN_REVIEW:
            missing_hard_negatives = hard_negative_ids - forbidden_ids
            if missing_hard_negatives:
                raise ValueError(
                    f"Manifest quality case {case_id} does not cover every hard-negative document."
                )


def _manifest_case_document_ids(
    case: dict[str, Any],
    *,
    field: str,
    case_id: str,
) -> set[str]:
    raw_values = case.get(field)
    if not isinstance(raw_values, list):
        raise ValueError(f"Manifest case {case_id} field {field} must be an array.")
    values = [str(item or "").strip() for item in raw_values]
    if any(not item for item in values):
        raise ValueError(
            f"Manifest case {case_id} field {field} contains an empty document id."
        )
    if len(values) != len(set(values)):
        raise ValueError(
            f"Manifest case {case_id} field {field} contains duplicate document ids."
        )
    return set(values)


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
        portable_relative_path = relative_path.replace("\\", "/")
        windows_path = PureWindowsPath(relative_path)
        posix_path = PurePosixPath(portable_relative_path)
        source_path = (resolved_source_root / portable_relative_path).resolve()
        is_within_source_root = bool(
            relative_path
            and not windows_path.drive
            and not windows_path.is_absolute()
            and not posix_path.is_absolute()
            and source_path.is_relative_to(resolved_source_root)
        )
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


def _stable_report_identifier(kind: str, value: Any) -> str:
    normalized = str(value or "").strip()
    payload = f"{REPORT_IDENTIFIER_NAMESPACE}:{kind}:{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _manifest_approval_content_sha256(manifest: dict[str, Any]) -> str:
    approval_scope = {
        "schema_version": manifest.get("schema_version"),
        "corpus_id": manifest.get("corpus_id"),
        "purpose": manifest.get("purpose"),
        "evaluation_scope": manifest.get("evaluation_scope"),
        "documents": manifest.get("documents"),
        "metrics": manifest.get("metrics"),
        "thresholds": manifest.get("thresholds"),
        "cases": manifest.get("cases"),
    }
    encoded = json.dumps(
        approval_scope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sanitize_preflight_report(report: dict[str, Any]) -> dict[str, Any]:
    documents = []
    for document in report.get("documents") or []:
        document_id = str(document.get("id") or "")
        duplicate_of = str(document.get("duplicate_of") or "")
        documents.append(
            {
                "document_key": _stable_report_identifier("document", document_id),
                "ingestion": str(document.get("ingestion") or ""),
                "indexable": bool(document.get("indexable")),
                "exists": bool(document.get("exists")),
                "has_declared_duplicate": bool(duplicate_of),
                "duplicate_document_key": (
                    _stable_report_identifier("document", duplicate_of)
                    if duplicate_of
                    else ""
                ),
                "duplicate_verified": document.get("duplicate_verified"),
            }
        )
    return {
        "schema_version": "irps_rag_corpus_preflight.v2",
        "corpus_key": _stable_report_identifier("corpus", report.get("corpus_id")),
        "status": str(report.get("status") or ""),
        "documents": documents,
        "indexable_document_keys": [
            _stable_report_identifier("document", document_id)
            for document_id in report.get("indexable_document_ids") or []
        ],
        "summary": dict(report.get("summary") or {}),
        "issues": [
            {
                "code": str(issue.get("code") or "unknown"),
                "document_key": _stable_report_identifier(
                    "document",
                    issue.get("document_id"),
                ),
            }
            for issue in report.get("issues") or []
        ],
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
        knowledge_doc = {
            "id": source_doc_id,
            "source_doc_id": source_doc_id,
            "manifest_doc_id": manifest_id,
            "name": source_path.name,
            "source_doc_name": source_path.name,
            "source_doc_version": "irps-rag-eval-v1",
            "doc_version": "irps-rag-eval-v1",
            "doc_type": str(manifest_doc.get("doc_type") or "REFERENCE"),
            # searchable_* is an explicit opt-in to this isolated evaluation
            # index. Materialize that intent as lifecycle metadata instead of
            # relying on the index synchronizer's empty-chunk READY behavior.
            "status": str(manifest_doc.get("status") or ACTIVE_KNOWLEDGE_STATUS),
            "effective_from": manifest_doc.get("effective_from"),
            "effective_to": manifest_doc.get("effective_to"),
            "project_id": project_id,
            "role": str(manifest_doc.get("role") or ""),
            "relative_path": relative_path,
            "content": content,
            "extracted_text": content,
            "segments": list(parsed.get("segments") or []),
        }
        _require_indexable_knowledge_document(
            project_id=project_id,
            knowledge_doc=knowledge_doc,
            phase="before indexing",
        )
        indexed_docs.append(knowledge_doc)
    return indexed_docs, source_doc_ids


def _require_indexable_knowledge_document(
    *,
    project_id: str,
    knowledge_doc: dict[str, Any],
    phase: str,
) -> int:
    source_doc_id = str(knowledge_doc.get("source_doc_id") or knowledge_doc.get("id") or "unknown")
    lifecycle = evaluate_knowledge_evidence_eligibility(knowledge_doc)
    if not lifecycle.allowed:
        raise ValueError(
            f"Searchable document {source_doc_id} is not ACTIVE and eligible {phase}: "
            f"{lifecycle.reason}."
        )
    chunk_count = len(build_knowledge_chunks(project_id, knowledge_doc))
    if chunk_count <= 0:
        raise ValueError(
            f"Searchable document {source_doc_id} produced zero knowledge chunks {phase}."
        )
    return chunk_count


def _require_ready_index_sync(
    *,
    knowledge_doc: dict[str, Any],
    sync_result: dict[str, Any],
) -> None:
    source_doc_id = str(knowledge_doc.get("source_doc_id") or knowledge_doc.get("id") or "unknown")
    lifecycle = evaluate_knowledge_evidence_eligibility(knowledge_doc)
    if not lifecycle.allowed:
        raise RuntimeError(
            f"Searchable document {source_doc_id} is not ACTIVE and eligible after indexing: "
            f"{lifecycle.reason}."
        )
    status = str(sync_result.get("status") or "").strip().upper()
    try:
        chunk_count = int(sync_result.get("chunk_count") or 0)
    except (TypeError, ValueError):
        chunk_count = 0
    if status != "READY":
        raise RuntimeError(
            f"Azure indexing failed for {source_doc_id}: "
            f"{sync_result.get('error') or sync_result.get('status')}"
        )
    if chunk_count <= 0:
        raise RuntimeError(
            f"Azure indexing failed for {source_doc_id}: READY sync reported zero knowledge chunks."
        )


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
    quality_eligible = (
        str(case.get("status") or "").strip().upper() != REQUIRES_HUMAN_REVIEW
    )
    return {
        "case_key": _stable_report_identifier("case", case_id),
        "quality_eligible": quality_eligible,
        "exclusion_reason": "" if quality_eligible else "REQUIRES_HUMAN_REVIEW",
        "expected_retrieval_grade": str(case.get("expected_retrieval_grade") or ""),
        "gold_document_keys": [
            _stable_report_identifier("document", item)
            for item in gold_ids
        ],
        "forbidden_document_keys": [
            _stable_report_identifier("document", item)
            for item in sorted(forbidden_ids)
        ],
        "ranked_document_keys": [
            _stable_report_identifier("document", item)
            for item in ranked_source_ids
        ],
        "gold_rank": gold_rank,
        "forbidden_hit_document_keys": [
            _stable_report_identifier("document", item)
            for item in forbidden_hits
        ],
        "top1_margin": scores[0] - scores[1] if len(scores) >= 2 else None,
        "top3_score_spread": max(scores[:3]) - min(scores[:3]) if len(scores) >= 2 else None,
        "evidences": [
            {
                "document_key": _stable_report_identifier(
                    "document",
                    source_id_to_manifest_id.get(
                        str(item.get("source_doc_id") or ""),
                        "",
                    ),
                ),
                "chunk_key": _stable_report_identifier(
                    "chunk",
                    item.get("chunk_id"),
                ),
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
    quality_cases = [item for item in cases if item.get("quality_eligible") is True]
    cases_with_gold = [item for item in quality_cases if item["gold_document_keys"]]
    visual_only_cases = [
        item
        for item in quality_cases
        if item["expected_retrieval_grade"] == "visual_only"
    ]
    recall_at_1 = _ratio(sum(item["gold_rank"] == 1 for item in cases_with_gold), len(cases_with_gold))
    recall_at_3 = _ratio(
        sum(item["gold_rank"] is not None and item["gold_rank"] <= 3 for item in cases_with_gold),
        len(cases_with_gold),
    )
    mrr = _ratio(
        sum(1 / item["gold_rank"] for item in cases_with_gold if item["gold_rank"] is not None),
        len(cases_with_gold),
    )
    all_hits = [
        hit
        for item in quality_cases
        for hit in item["ranked_document_keys"]
    ]
    score_source_counts = Counter(
        evidence["score_source"]
        for item in quality_cases
        for evidence in item["evidences"]
        if evidence["score_source"]
    )
    scores_by_relevance_class: dict[str, list[float]] = {}
    for item in quality_cases:
        for evidence in item["evidences"]:
            scores_by_relevance_class.setdefault(evidence["relevance_class"], []).append(evidence["score"])
    return {
        "evaluation_kind": "RETRIEVAL_ONLY",
        "total_case_count": len(cases),
        "quality_case_count": len(quality_cases),
        "excluded_requires_human_review_count": len(cases) - len(quality_cases),
        "retrieval_cases_with_gold": len(cases_with_gold),
        "visual_only_quality_case_count": len(visual_only_cases),
        "recall_at_1": recall_at_1,
        "recall_at_3": recall_at_3,
        "mrr": mrr,
        "wrong_evidence_rate": _ratio(
            sum(bool(item["forbidden_hit_document_keys"]) for item in quality_cases),
            len(quality_cases),
        ),
        "hard_negative_retrieval_rate": _ratio(
            sum(bool(item["forbidden_hit_document_keys"]) for item in quality_cases),
            len(quality_cases),
        ),
        "visual_only_false_positive_rate": _ratio(
            sum(bool(item["ranked_document_keys"]) for item in visual_only_cases),
            len(visual_only_cases),
        ),
        "score_source_counts": dict(sorted(score_source_counts.items())),
        "score_distribution": {
            relevance_class: _score_summary(scores)
            for relevance_class, scores in sorted(scores_by_relevance_class.items())
        },
        "retrieved_source_doc_counts": dict(sorted(Counter(all_hits).items())),
        "top1_margin_median": _median(
            [
                item["top1_margin"]
                for item in quality_cases
                if item["top1_margin"] is not None
            ]
        ),
        "top3_score_spread_median": _median(
            [
                item["top3_score_spread"]
                for item in quality_cases
                if item["top3_score_spread"] is not None
            ]
        ),
    }


def _build_quality_gate(
    *,
    manifest: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    manifest_status = str(manifest.get("status") or "").strip().upper()
    approval = manifest.get("approval")
    approval_status = (
        str(approval.get("status") or "").strip().upper()
        if isinstance(approval, dict)
        else "APPROVED"
        if approval is True
        else ""
    )
    if manifest_status != "FROZEN":
        blockers.append("manifest_not_frozen")
    if approval_status != "APPROVED":
        blockers.append("manifest_not_approved")
    approval_content_sha256 = (
        str(approval.get("content_sha256") or "").strip().lower()
        if isinstance(approval, dict)
        else ""
    )
    approval_hash_valid = bool(
        approval_status == "APPROVED"
        and approval_content_sha256
        == _manifest_approval_content_sha256(manifest)
    )
    if approval_status == "APPROVED" and not approval_hash_valid:
        blockers.append("approval_content_hash_mismatch")
    if int(metrics.get("excluded_requires_human_review_count") or 0) > 0:
        blockers.append("unresolved_human_review_cases")
    if int(metrics.get("quality_case_count") or 0) < MINIMUM_QUALITY_CASES:
        blockers.append("insufficient_quality_cases")
    if (
        int(metrics.get("retrieval_cases_with_gold") or 0)
        < MINIMUM_GOLD_RETRIEVAL_CASES
    ):
        blockers.append("insufficient_gold_retrieval_cases")
    if (
        int(metrics.get("visual_only_quality_case_count") or 0)
        < MINIMUM_VISUAL_ONLY_CASES
    ):
        blockers.append("insufficient_visual_only_cases")

    declared = manifest.get("thresholds")
    if not isinstance(declared, dict):
        declared = {}
        blockers.append("thresholds_not_declared")
    effective: dict[str, float] = {}
    declared_safe: dict[str, float | None] = {}
    for key, floor in QUALITY_THRESHOLD_FLOORS.items():
        raw_value = declared.get(key)
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = floor
            blockers.append(f"threshold_missing_or_invalid:{key}")
            declared_safe[key] = None
        else:
            declared_safe[key] = value
            if not 0.0 <= value <= 1.0:
                blockers.append(f"threshold_out_of_range:{key}")
                value = floor
            if key.endswith("_min"):
                if value < floor:
                    blockers.append(f"threshold_below_floor:{key}")
                value = max(value, floor)
            else:
                if value > floor:
                    blockers.append(f"threshold_above_floor:{key}")
                value = min(value, floor)
        effective[key] = value

    metric_by_threshold = {
        "recall_at_1_min": "recall_at_1",
        "recall_at_3_min": "recall_at_3",
        "mrr_min": "mrr",
        "wrong_evidence_rate_max": "wrong_evidence_rate",
        "hard_negative_retrieval_rate_max": "hard_negative_retrieval_rate",
        "visual_only_false_positive_rate_max": "visual_only_false_positive_rate",
    }
    checks: dict[str, dict[str, Any]] = {}
    for threshold_key, metric_key in metric_by_threshold.items():
        actual = metrics.get(metric_key)
        threshold = effective[threshold_key]
        if actual is None:
            passed = False
            blockers.append(f"metric_unavailable:{metric_key}")
        elif threshold_key.endswith("_min"):
            passed = float(actual) >= threshold
        else:
            passed = float(actual) <= threshold
        checks[metric_key] = {
            "actual": actual,
            "operator": ">=" if threshold_key.endswith("_min") else "<=",
            "threshold": threshold,
            "passed": passed,
        }

    return {
        "gate_kind": "RETRIEVAL_QUALITY_RELEASE_GATE",
        "manifest_status": manifest_status or "UNDECLARED",
        "approval_status": approval_status or "UNDECLARED",
        "approval_content_hash_valid": approval_hash_valid,
        "declared_thresholds": declared_safe,
        "effective_thresholds": effective,
        "checks": checks,
        "blockers": sorted(set(blockers)),
        "overall_pass": not blockers and all(
            bool(check["passed"]) for check in checks.values()
        ),
    }


def _ensure_quality_pass(report: dict[str, Any]) -> None:
    gate = report.get("quality_gate")
    if isinstance(gate, dict) and gate.get("overall_pass") is True:
        return
    blockers = [
        str(item)
        for item in (gate.get("blockers") if isinstance(gate, dict) else []) or []
    ]
    detail = ", ".join(blockers) or "quality_threshold_failed"
    raise RuntimeError(f"IRPS RAG retrieval quality gate failed: {detail}.")


def _verify_azure_search_cleanup(
    config: AICopilotConfig,
    *,
    source_doc_ids: list[str],
) -> bool:
    normalized_source_ids = sorted(
        {
            str(source_doc_id or "").strip()
            for source_doc_id in source_doc_ids
            if str(source_doc_id or "").strip()
        }
    )
    if not normalized_source_ids:
        return True

    from azure.search.documents import SearchClient

    search_client = SearchClient(
        endpoint=config.azure_search_endpoint,
        index_name=config.azure_search_index_name,
        credential=_azure_search_credential(config),
    )
    for attempt in range(CLEANUP_VERIFICATION_ATTEMPTS):
        remaining = False
        for source_doc_id in normalized_source_ids:
            filter_text = _azure_filter_to_odata(
                {
                    "field": "source_doc_id",
                    "operator": "==",
                    "value": source_doc_id,
                }
            )
            matches = search_client.search(
                search_text="*",
                filter=filter_text,
                select=["chunk_id"],
                top=1,
            )
            if any(matches):
                remaining = True
                break
        if not remaining:
            return True
        if attempt + 1 < CLEANUP_VERIFICATION_ATTEMPTS:
            time.sleep(CLEANUP_VERIFICATION_DELAY_SECONDS)
    return False


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
