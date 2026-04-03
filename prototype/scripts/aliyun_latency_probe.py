from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.services.ai_copilot import (
    DEFAULT_ALIYUN_PROFILE_ID,
    _assessment_runtime_config,
    _dashscope_json_completion,
    _embed_texts,
    _screening_runtime_config,
    load_ai_copilot_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure Aliyun DashScope chat and embedding latency for the current ReqPilotX profile.")
    parser.add_argument("--profile", default=DEFAULT_ALIYUN_PROFILE_ID, help="AI profile id. Defaults to aliyun.")
    parser.add_argument("--attempts", type=int, default=3, help="Attempts per probe. Defaults to 3.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/aliyun_latency/latest"),
        help="Directory for JSON and Markdown summaries relative to prototype/.",
    )
    args = parser.parse_args()

    out_dir = (PROJECT_DIR / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    config = load_ai_copilot_config(str(args.profile or DEFAULT_ALIYUN_PROFILE_ID).strip() or DEFAULT_ALIYUN_PROFILE_ID)
    screening_config = _screening_runtime_config(config)
    assessment_config = _assessment_runtime_config(config)

    probes = [
        _run_probe(
            name="llm_screening_json",
            attempts=max(int(args.attempts or 1), 1),
            runner=lambda: _probe_screening_call(screening_config),
        ),
        _run_probe(
            name="llm_assessment_json",
            attempts=max(int(args.attempts or 1), 1),
            runner=lambda: _probe_assessment_call(assessment_config),
        ),
        _run_probe(
            name="embedding_single",
            attempts=max(int(args.attempts or 1), 1),
            runner=lambda: _probe_embedding_call(
                assessment_config,
                ["供应商必须在量产前提交 CAMDS 数据，并保证申报内容真实准确。"],
            ),
        ),
        _run_probe(
            name="embedding_batch_3",
            attempts=max(int(args.attempts or 1), 1),
            runner=lambda: _probe_embedding_call(
                assessment_config,
                [
                    "供应商必须在量产前提交 CAMDS 数据，并保证申报内容真实准确。",
                    "零件标识应满足图纸和企业标准要求，确保追溯性。",
                    "本章节用于说明文件编制背景，不构成供应商执行义务。",
                ],
            ),
        ),
    ]

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile_id": config.profile_id,
        "provider": config.provider,
        "base_url": config.dashscope_base_url,
        "screening_model": screening_config.screening_model or screening_config.chat_model,
        "assessment_model": assessment_config.chat_model,
        "embedding_model": assessment_config.embedding_model,
        "screening_timeout_seconds": screening_config.llm_timeout_seconds,
        "assessment_timeout_seconds": assessment_config.llm_timeout_seconds,
        "embedding_batch_size": assessment_config.embedding_batch_size,
        "probes": probes,
    }
    summary["all_passed"] = all(item["success_count"] == item["attempts"] for item in probes)

    json_path = out_dir / "aliyun_latency_probe.json"
    md_path = out_dir / "aliyun_latency_probe.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(summary), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[aliyun-latency-probe] json: {json_path}")
    print(f"[aliyun-latency-probe] md: {md_path}")


def _run_probe(*, name: str, attempts: int, runner: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for index in range(attempts):
        started = time.perf_counter()
        try:
            payload = runner()
            runs.append(
                {
                    "attempt": index + 1,
                    "status": "ok",
                    "duration_seconds": round(time.perf_counter() - started, 3),
                    **payload,
                }
            )
        except Exception as exc:
            runs.append(
                {
                    "attempt": index + 1,
                    "status": "error",
                    "duration_seconds": round(time.perf_counter() - started, 3),
                    "error": str(exc),
                }
            )

    durations = [float(item["duration_seconds"]) for item in runs if item.get("status") == "ok"]
    return {
        "name": name,
        "attempts": attempts,
        "success_count": len(durations),
        "failure_count": attempts - len(durations),
        "avg_seconds": round(statistics.mean(durations), 3) if durations else None,
        "median_seconds": round(statistics.median(durations), 3) if durations else None,
        "min_seconds": round(min(durations), 3) if durations else None,
        "max_seconds": round(max(durations), 3) if durations else None,
        "runs": runs,
    }


def _probe_screening_call(config) -> dict[str, Any]:
    model_name = config.screening_model or config.chat_model
    payload = _dashscope_json_completion(
        config,
        [
            {
                "role": "system",
                "content": "你是需求筛选助手。请判断条目是 REQUIREMENT 还是 INFO，并返回严格 JSON。",
            },
            {
                "role": "user",
                "content": (
                    "请输出 JSON，字段必须包含 analysis_label 和 analysis_basis。\n"
                    "文本：供应商必须在量产前提交 CAMDS 数据，并确保禁限用物质申报真实完整。"
                ),
            },
        ],
        model_name=model_name,
    )
    return {
        "model": model_name,
        "response_keys": sorted(payload.keys()),
        "analysis_label": str(payload.get("analysis_label") or ""),
    }


def _probe_assessment_call(config) -> dict[str, Any]:
    model_name = config.chat_model
    payload = _dashscope_json_completion(
        config,
        [
            {
                "role": "system",
                "content": "你是需求合规分析助手。请结合 requirement 与证据返回严格 JSON，不要附加解释。",
            },
            {
                "role": "user",
                "content": (
                    "请输出 JSON，字段必须包含 ai_decision、ai_reason、risk_level、clarification_question。\n"
                    "Requirement：供应商必须在量产前提交 CAMDS 数据，并确保禁限用物质申报真实完整。\n"
                    "Evidence 1：EMC 文档仅说明测试范围，不包含 CAMDS 或材料合规内容。\n"
                    "Evidence 2：当前知识库中无法规类文档，无法直接验证法规符合性。\n"
                    "请基于以上信息给出谨慎结论。"
                ),
            },
        ],
        model_name=model_name,
    )
    return {
        "model": model_name,
        "response_keys": sorted(payload.keys()),
        "ai_decision": str(payload.get("ai_decision") or ""),
        "risk_level": str(payload.get("risk_level") or ""),
    }


def _probe_embedding_call(config, texts: list[str]) -> dict[str, Any]:
    vectors = _embed_texts(texts, config=config)
    first_dimension = len(vectors[0]) if vectors else 0
    return {
        "model": config.embedding_model,
        "input_count": len(texts),
        "vector_count": len(vectors),
        "vector_dimension": first_dimension,
    }


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Aliyun Latency Probe",
        "",
        f"- 生成时间：{summary.get('generated_at')}",
        f"- Profile：`{summary.get('profile_id')}`",
        f"- Screening model：`{summary.get('screening_model')}`",
        f"- Assessment model：`{summary.get('assessment_model')}`",
        f"- Embedding model：`{summary.get('embedding_model')}`",
        f"- Screening timeout：`{summary.get('screening_timeout_seconds')}`",
        f"- Assessment timeout：`{summary.get('assessment_timeout_seconds')}`",
        "",
    ]
    for probe in summary.get("probes") or []:
        lines.extend(
            [
                f"## {probe.get('name')}",
                "",
                f"- attempts: `{probe.get('attempts')}`",
                f"- success_count: `{probe.get('success_count')}`",
                f"- failure_count: `{probe.get('failure_count')}`",
                f"- avg_seconds: `{probe.get('avg_seconds')}`",
                f"- median_seconds: `{probe.get('median_seconds')}`",
                f"- min_seconds: `{probe.get('min_seconds')}`",
                f"- max_seconds: `{probe.get('max_seconds')}`",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
