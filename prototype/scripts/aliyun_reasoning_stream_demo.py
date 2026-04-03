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
    _run_requirement_screening,
    _select_retriever,
    build_assessment_context,
    build_llm_messages,
    load_ai_copilot_config,
)

DEFAULT_PROJECT_ID = "93bebc97-77f3-4ddd-9d87-d2c0c1790171"
DEFAULT_DOCUMENT_ID = "0c054f17-d76a-40ad-ab77-f76551700b99"
DEFAULT_CODES = ["REQ-0014", "REQ-0025"]


def main_cli() -> None:
    parser = argparse.ArgumentParser(description="Demonstrate streamed reasoning/content timeline for real Aliyun requirement assessment prompts.")
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--document-id", default=DEFAULT_DOCUMENT_ID)
    parser.add_argument("--profile", default="aliyun")
    parser.add_argument("--codes", nargs="*", default=DEFAULT_CODES)
    parser.add_argument("--timeout", type=float, default=75.0)
    parser.add_argument("--thinking-budget", type=int, default=0, help="0 means use model default thinking behavior.")
    parser.add_argument("--disable-thinking", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=Path("eval/aliyun_reasoning_stream/latest"))
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

    extra_body: dict[str, Any] = {}
    if args.disable_thinking:
        extra_body["enable_thinking"] = False
    elif int(args.thinking_budget or 0) > 0:
        extra_body["enable_thinking"] = True
        extra_body["thinking_budget"] = int(args.thinking_budget)

    results = []
    for code in args.codes:
        row = rows_by_code.get(str(code).strip())
        if not row:
            results.append({"req_code": code, "error": "requirement_not_found"})
            continue
        results.append(_run_stream_demo(row=row, knowledge_docs=knowledge_docs, config=config, extra_body=extra_body))

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_id": args.project_id,
        "document_id": args.document_id,
        "profile_id": config.profile_id,
        "model": config.chat_model,
        "timeout_seconds": config.llm_timeout_seconds,
        "extra_body": extra_body,
        "results": results,
    }
    json_path = out_dir / "aliyun_reasoning_stream_demo.json"
    md_path = out_dir / "aliyun_reasoning_stream_demo.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[aliyun-reasoning-stream-demo] json: {json_path}")
    print(f"[aliyun-reasoning-stream-demo] md: {md_path}")


def _run_stream_demo(*, row: dict[str, Any], knowledge_docs: list[dict[str, Any]], config, extra_body: dict[str, Any]) -> dict[str, Any]:
    context = build_assessment_context(row)
    collector = AssessmentRunCollector(config=config, context=context, retrieval_backend="screening_only")
    screening = _run_requirement_screening(context=context, config=config, collector=collector)
    if screening.get("analysis_label") != "REQUIREMENT" or screening.get("screening_failed"):
        return {
            "req_code": str(row.get("req_code") or ""),
            "requirement_id": str(row.get("id") or ""),
            "error": "screening_not_requirement",
            "screening": screening,
        }

    retriever = _select_retriever(config)
    evidences = retriever.retrieve(context, knowledge_docs, top_k=config.top_k)
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
    client = _aliyun_openai_client(effective_config)

    started = time.perf_counter()
    first_reasoning_seconds: float | None = None
    first_answer_seconds: float | None = None
    reasoning_text = ""
    answer_text = ""
    reasoning_events: list[dict[str, Any]] = []
    answer_events: list[dict[str, Any]] = []
    usage_payload: dict[str, Any] | None = None
    error = ""
    finish_reason = ""

    try:
        for chunk in client.chat.completions.create(
            model=effective_config.chat_model,
            messages=messages,
            temperature=0.1,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=extra_body or None,
        ):
            now = time.perf_counter() - started
            choices = list(getattr(chunk, "choices", []) or [])
            if not choices:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    usage_payload = _usage_to_dict(usage)
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            reasoning_piece = getattr(delta, "reasoning_content", None)
            if reasoning_piece is not None:
                text = _coerce_stream_text(reasoning_piece)
                if text:
                    if first_reasoning_seconds is None:
                        first_reasoning_seconds = now
                    reasoning_text += text
                    if len(reasoning_events) < 12:
                        reasoning_events.append({"t": round(now, 3), "text": text[:160]})
            answer_piece = getattr(delta, "content", None)
            if answer_piece is not None:
                text = _coerce_stream_text(answer_piece)
                if text:
                    if first_answer_seconds is None:
                        first_answer_seconds = now
                    answer_text += text
                    if len(answer_events) < 12:
                        answer_events.append({"t": round(now, 3), "text": text[:160]})
            finish_reason = str(getattr(choices[0], "finish_reason", "") or finish_reason)
    except Exception as exc:
        error = str(exc)

    return {
        "req_code": str(row.get("req_code") or ""),
        "requirement_id": str(row.get("id") or ""),
        "seconds_total": round(time.perf_counter() - started, 3),
        "retrieval_backend": retriever.backend_name,
        "evidence_count": len(evidences),
        "retrieval_gate_reason": str(context.get("retrieval_gate_reason") or ""),
        "first_reasoning_seconds": round(first_reasoning_seconds, 3) if first_reasoning_seconds is not None else None,
        "first_answer_seconds": round(first_answer_seconds, 3) if first_answer_seconds is not None else None,
        "reasoning_chars": len(reasoning_text),
        "answer_chars": len(answer_text),
        "finish_reason": finish_reason,
        "error": error,
        "usage": usage_payload,
        "reasoning_events": reasoning_events,
        "answer_events": answer_events,
        "reasoning_preview": reasoning_text[:600],
        "answer_preview": answer_text[:600],
    }


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _coerce_stream_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Aliyun Reasoning Stream Demo",
        "",
        f"- 生成时间：{summary.get('generated_at')}",
        f"- Model：`{summary.get('model')}`",
        f"- Extra body：`{json.dumps(summary.get('extra_body') or {}, ensure_ascii=False)}`",
        "",
    ]
    for item in summary.get("results") or []:
        lines.append(f"## {item.get('req_code')}")
        lines.append("")
        if item.get("error"):
            lines.append(f"- error: `{item.get('error')}`")
            lines.append("")
            continue
        lines.append(f"- first_reasoning_seconds: `{item.get('first_reasoning_seconds')}`")
        lines.append(f"- first_answer_seconds: `{item.get('first_answer_seconds')}`")
        lines.append(f"- seconds_total: `{item.get('seconds_total')}`")
        lines.append(f"- reasoning_chars: `{item.get('reasoning_chars')}`")
        lines.append(f"- answer_chars: `{item.get('answer_chars')}`")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main_cli()
