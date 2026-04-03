from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import main
from app.services.ai_copilot import (
    AssessmentRunCollector,
    _aliyun_openai_client,
    _assessment_runtime_config,
    _build_json_compatible_messages,
    _extract_json_object,
    _run_requirement_screening,
    _select_retriever,
    build_assessment_context,
    build_llm_messages,
    load_ai_copilot_config,
    reconcile_assessment_output,
)

DEFAULT_PROJECT_ID = "93bebc97-77f3-4ddd-9d87-d2c0c1790171"
DEFAULT_DOCUMENT_ID = "0c054f17-d76a-40ad-ab77-f76551700b99"
DEFAULT_CODES = ["REQ-0014", "REQ-0025"]


def main_cli() -> None:
    parser = argparse.ArgumentParser(description="A/B probe for Aliyun assessment thinking controls on real requirement prompts.")
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--document-id", default=DEFAULT_DOCUMENT_ID)
    parser.add_argument("--profile", default="aliyun")
    parser.add_argument("--codes", nargs="*", default=DEFAULT_CODES)
    parser.add_argument("--timeout", type=float, default=75.0)
    parser.add_argument("--out-dir", type=Path, default=Path("eval/aliyun_assessment_ab/latest"))
    args = parser.parse_args()

    project_dir = PROJECT_DIR
    out_dir = (project_dir / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    _project, _document, _requirements, eligible_rows, knowledge_docs, config = main._prepare_document_ai_analysis_request(
        args.project_id,
        args.document_id,
        args.profile,
    )
    config = type(config)(**{**config.__dict__, "llm_timeout_seconds": float(args.timeout or 75.0), "openai_max_retries": 0})

    rows_by_code = {str(row.get("req_code") or "").strip(): row for row in eligible_rows}
    variants = [
        {"name": "default", "extra_body": {}},
        {"name": "enable_thinking_false", "extra_body": {"enable_thinking": False}},
        {"name": "thinking_budget_50", "extra_body": {"enable_thinking": True, "thinking_budget": 50}},
    ]

    results: list[dict[str, Any]] = []
    for code in args.codes:
        row = rows_by_code.get(str(code).strip())
        if not row:
            results.append({"req_code": code, "error": "requirement_not_found"})
            continue
        results.append(_run_requirement_variants(row=row, knowledge_docs=knowledge_docs, config=config, variants=variants))

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_id": args.project_id,
        "document_id": args.document_id,
        "profile_id": config.profile_id,
        "chat_model": config.chat_model,
        "embedding_model": config.embedding_model,
        "timeout_seconds": config.llm_timeout_seconds,
        "codes": list(args.codes),
        "results": results,
    }
    json_path = out_dir / "aliyun_assessment_ab_probe.json"
    md_path = out_dir / "aliyun_assessment_ab_probe.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[aliyun-assessment-ab-probe] json: {json_path}")
    print(f"[aliyun-assessment-ab-probe] md: {md_path}")


def _run_requirement_variants(
    *,
    row: dict[str, Any],
    knowledge_docs: list[dict[str, Any]],
    config,
    variants: list[dict[str, Any]],
) -> dict[str, Any]:
    context = build_assessment_context(row)
    collector = AssessmentRunCollector(config=config, context=context, retrieval_backend="screening_only")
    screening = _run_requirement_screening(context=context, config=config, collector=collector)

    requirement_summary: dict[str, Any] = {
        "req_code": str(row.get("req_code") or ""),
        "requirement_id": str(row.get("id") or ""),
        "screening": {
            "analysis_label": screening.get("analysis_label"),
            "reason": screening.get("reason"),
            "validation_status": screening.get("validation_status"),
            "fallback_reason": screening.get("fallback_reason"),
        },
        "variants": [],
    }
    if screening.get("analysis_label") != "REQUIREMENT" or screening.get("screening_failed"):
        requirement_summary["error"] = "screening_not_requirement"
        return requirement_summary

    retriever = _select_retriever(config)
    evidences = retriever.retrieve(context, knowledge_docs, top_k=config.top_k)
    requirement_summary["retrieval"] = {
        "backend": retriever.backend_name,
        "evidence_count": len(evidences),
        "top_evidence_score": round(float(evidences[0].get("relevance") or 0.0), 4) if evidences else 0.0,
        "retrieval_gate_reason": str(context.get("retrieval_gate_reason") or ""),
        "evidence_sources": [
            {
                "source_doc_name": str(item.get("source_doc_name") or ""),
                "source_location": str(item.get("source_location") or ""),
                "content_kind": str(item.get("content_kind") or ""),
                "relevance": round(float(item.get("relevance") or 0.0), 4),
            }
            for item in evidences[:3]
        ],
    }

    messages = _build_json_compatible_messages(
        build_llm_messages(context, evidences),
        required_keys=[
            "normalized_requirement",
            "proposed_decision",
            "confidence",
            "reason",
            "risk_level",
            "clarification_question",
            "evidences",
        ],
    )
    effective_config = _assessment_runtime_config(config)

    for variant in variants:
        requirement_summary["variants"].append(
            _run_single_variant(
                context=context,
                evidences=evidences,
                config=effective_config,
                messages=messages,
                name=str(variant.get("name") or ""),
                extra_body=dict(variant.get("extra_body") or {}),
            )
        )

    return requirement_summary


def _run_single_variant(
    *,
    context: dict[str, Any],
    evidences: list[dict[str, Any]],
    config,
    messages: list[dict[str, str]],
    name: str,
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    client = _aliyun_openai_client(config)
    started = time.perf_counter()
    try:
        completion = client.chat.completions.create(
            model=config.chat_model,
            messages=messages,
            temperature=0.1,
            response_format={"type": "json_object"},
            extra_body=extra_body or None,
        )
        elapsed = time.perf_counter() - started
        message = completion.choices[0].message
        content = getattr(message, "content", "")
        if isinstance(content, list):
            content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
        raw = _extract_json_object(str(content or ""))
        reconciled = reconcile_assessment_output(
            context=context,
            candidate=raw,
            retrieved_evidences=evidences,
            fallback_reason="",
        )
        return {
            "name": name,
            "extra_body": extra_body,
            "status": "ok",
            "seconds": round(elapsed, 3),
            "validation_status": str(reconciled.get("validation_status") or ""),
            "fallback_reason": str(reconciled.get("fallback_reason") or ""),
            "ai_decision": str(reconciled.get("ai_decision") or ""),
            "risk_level": str(reconciled.get("risk_level") or ""),
            "ai_confidence": float(reconciled.get("ai_confidence") or 0.0),
            "clarification_question": str(reconciled.get("clarification_question") or ""),
            "evidence_count": len(reconciled.get("evidences") or []),
            "ai_reason_preview": str(reconciled.get("ai_reason") or "")[:240],
            "raw_keys": sorted(raw.keys()),
            "raw_decision": str(raw.get("proposed_decision") or ""),
            "raw_reason_preview": str(raw.get("reason") or "")[:240],
            "usage": _usage_to_dict(getattr(completion, "usage", None)),
        }
    except Exception as exc:
        return {
            "name": name,
            "extra_body": extra_body,
            "status": "error",
            "seconds": round(time.perf_counter() - started, 3),
            "error": str(exc),
        }


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Aliyun Assessment A/B Probe",
        "",
        f"- 生成时间：{summary.get('generated_at')}",
        f"- Profile：`{summary.get('profile_id')}`",
        f"- Chat model：`{summary.get('chat_model')}`",
        f"- Timeout：`{summary.get('timeout_seconds')}`",
        "",
    ]
    for requirement in summary.get("results") or []:
        lines.append(f"## {requirement.get('req_code')}")
        lines.append("")
        if requirement.get("error"):
            lines.append(f"- error: `{requirement.get('error')}`")
            lines.append("")
            continue
        retrieval = requirement.get("retrieval") or {}
        lines.append(f"- retrieval backend: `{retrieval.get('backend')}`")
        lines.append(f"- evidence_count: `{retrieval.get('evidence_count')}`")
        lines.append(f"- retrieval_gate_reason: `{retrieval.get('retrieval_gate_reason')}`")
        lines.append("")
        for variant in requirement.get("variants") or []:
            lines.append(f"### {variant.get('name')}")
            lines.append("")
            lines.append(f"- status: `{variant.get('status')}`")
            lines.append(f"- seconds: `{variant.get('seconds')}`")
            if variant.get("status") == "ok":
                lines.append(f"- ai_decision: `{variant.get('ai_decision')}`")
                lines.append(f"- validation_status: `{variant.get('validation_status')}`")
                lines.append(f"- evidence_count: `{variant.get('evidence_count')}`")
                lines.append(f"- fallback_reason: `{variant.get('fallback_reason')}`")
            else:
                lines.append(f"- error: `{variant.get('error')}`")
            lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main_cli()
