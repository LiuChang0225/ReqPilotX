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

from app.services.ai_copilot import AICopilotConfig, _aliyun_openai_client, load_ai_copilot_config


DEFAULT_PROMPT = """你是需求合规分析助手。请基于 requirement 和 evidence 做出谨慎判断。

请分析：
Requirement：供应商必须在量产前提交 CAMDS 数据，并确保禁限用物质申报真实完整。
Evidence 1：EMC 文档仅说明测试范围，不包含 CAMDS 或材料合规内容。
Evidence 2：当前知识库中无法规类文档，无法直接验证法规符合性。

请先思考，再给出最终结论，回答要包含：判断、原因、风险。"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe whether qwen3.5-plus exposes reasoning content and how thinking controls affect latency.")
    parser.add_argument("--profile", default="aliyun")
    parser.add_argument("--timeout", type=float, default=75.0, help="Client timeout seconds for probe calls.")
    parser.add_argument("--out-dir", type=Path, default=Path("eval/aliyun_thinking/latest"))
    parser.add_argument("--prompt", type=str, default=DEFAULT_PROMPT)
    args = parser.parse_args()

    base_config = load_ai_copilot_config(str(args.profile or "aliyun").strip() or "aliyun")
    config = AICopilotConfig(**{**base_config.__dict__, "llm_timeout_seconds": float(args.timeout or 75.0), "openai_max_retries": 0})
    out_dir = (PROJECT_DIR / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    experiments = [
        {"name": "default", "extra_body": None},
        {"name": "enable_thinking_false", "extra_body": {"enable_thinking": False}},
        {"name": "enable_thinking_true_budget_50", "extra_body": {"enable_thinking": True, "thinking_budget": 50}},
        {"name": "enable_thinking_true_budget_20", "extra_body": {"enable_thinking": True, "thinking_budget": 20}},
    ]

    results = [
        _run_stream_probe(config=config, prompt=args.prompt, name=item["name"], extra_body=item["extra_body"])
        for item in experiments
    ]

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile_id": config.profile_id,
        "model": config.chat_model,
        "base_url": config.dashscope_base_url,
        "timeout_seconds": config.llm_timeout_seconds,
        "results": results,
    }
    json_path = out_dir / "aliyun_thinking_probe.json"
    md_path = out_dir / "aliyun_thinking_probe.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[aliyun-thinking-probe] json: {json_path}")
    print(f"[aliyun-thinking-probe] md: {md_path}")


def _run_stream_probe(*, config: AICopilotConfig, prompt: str, name: str, extra_body: dict[str, Any] | None) -> dict[str, Any]:
    client = _aliyun_openai_client(config)
    started = time.perf_counter()
    first_reasoning_at: float | None = None
    first_answer_at: float | None = None
    reasoning_parts: list[str] = []
    answer_parts: list[str] = []
    finish_reason = ""
    usage_payload: dict[str, Any] | None = None
    error = ""

    payload: dict[str, Any] = {
        "model": config.chat_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.1,
    }
    if extra_body:
        payload["extra_body"] = extra_body

    try:
        for chunk in client.chat.completions.create(**payload):
            now = time.perf_counter()
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
            answer_piece = getattr(delta, "content", None)
            if reasoning_piece is not None:
                text = _coerce_stream_text(reasoning_piece)
                if text:
                    if first_reasoning_at is None:
                        first_reasoning_at = now - started
                    reasoning_parts.append(text)
            if answer_piece is not None:
                text = _coerce_stream_text(answer_piece)
                if text:
                    if first_answer_at is None:
                        first_answer_at = now - started
                    answer_parts.append(text)
            finish_reason = str(getattr(choices[0], "finish_reason", "") or finish_reason)
    except Exception as exc:
        error = str(exc)

    total_seconds = time.perf_counter() - started
    reasoning_text = "".join(reasoning_parts)
    answer_text = "".join(answer_parts)
    return {
        "name": name,
        "extra_body": extra_body or {},
        "total_seconds": round(total_seconds, 3),
        "first_reasoning_seconds": round(first_reasoning_at, 3) if first_reasoning_at is not None else None,
        "first_answer_seconds": round(first_answer_at, 3) if first_answer_at is not None else None,
        "reasoning_detected": bool(reasoning_text),
        "reasoning_chars": len(reasoning_text),
        "answer_chars": len(answer_text),
        "finish_reason": finish_reason,
        "error": error,
        "usage": usage_payload,
        "answer_preview": answer_text[:200],
        "reasoning_preview": reasoning_text[:200],
    }


def _usage_to_dict(usage: Any) -> dict[str, Any]:
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
        "# Aliyun Thinking Probe",
        "",
        f"- 生成时间：{summary.get('generated_at')}",
        f"- Model：`{summary.get('model')}`",
        f"- Timeout：`{summary.get('timeout_seconds')}`",
        "",
    ]
    for item in summary.get("results") or []:
        lines.extend(
            [
                f"## {item.get('name')}",
                "",
                f"- total_seconds: `{item.get('total_seconds')}`",
                f"- first_reasoning_seconds: `{item.get('first_reasoning_seconds')}`",
                f"- first_answer_seconds: `{item.get('first_answer_seconds')}`",
                f"- reasoning_detected: `{item.get('reasoning_detected')}`",
                f"- reasoning_chars: `{item.get('reasoning_chars')}`",
                f"- answer_chars: `{item.get('answer_chars')}`",
                f"- finish_reason: `{item.get('finish_reason')}`",
                f"- error: `{item.get('error')}`",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
