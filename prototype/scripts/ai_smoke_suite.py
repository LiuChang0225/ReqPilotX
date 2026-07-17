from __future__ import annotations

import argparse
import dataclasses
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.ai_copilot import (
    AICopilotConfig,
    delete_azure_search_chunks_for_source_docs,
    load_ai_copilot_config,
    run_ai_copilot_assessment,
    sync_knowledge_document_index,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AI Copilot smoke cases.")
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("eval/ai_smoke/ai_smoke_cases.json"),
        help="JSON file describing local smoke cases.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/ai_smoke/latest"),
        help="Directory for smoke summary artifacts.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="heuristic",
        choices=["heuristic", "llm_shadow", "llm_primary"],
        help="Assessment mode. Default keeps the suite fully local.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default="local",
        choices=["local", "azure"],
        help="Runtime profile. Use azure for an environment-gated Azure Search/OpenAI smoke.",
    )
    parser.add_argument(
        "--allow-skip",
        action="store_true",
        help="Write a skipped summary instead of failing when the selected runtime is not configured.",
    )
    parser.add_argument(
        "--sync-knowledge",
        action="store_true",
        help="Use isolated temporary knowledge documents for a full Azure RAG smoke, then remove their search chunks.",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parents[1]
    cases_path = (project_dir / args.cases).resolve()
    out_dir = (project_dir / args.out_dir).resolve()
    summary = run_smoke_suite(
        cases_path=cases_path,
        out_dir=out_dir,
        mode=args.mode,
        profile=args.profile,
        allow_skip=args.allow_skip,
        sync_knowledge=args.sync_knowledge,
    )

    print(f"[ai-smoke] cases: {summary.get('cases_passed', 0)}/{summary.get('cases_total', 0)}")
    print(f"[ai-smoke] summary json: {out_dir / 'ai_smoke_summary.json'}")
    print(f"[ai-smoke] summary md: {out_dir / 'ai_smoke_summary.md'}")
    if not summary.get("skipped") and not summary.get("overall_pass"):
        raise SystemExit(1)


def run_smoke_suite(
    *,
    cases_path: Path,
    out_dir: Path,
    mode: str,
    profile: str = "local",
    allow_skip: bool = False,
    sync_knowledge: bool = False,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    config = _build_smoke_config(mode=mode, profile=profile, trace_dir=out_dir / "traces")
    if profile == "azure" and mode in {"llm_shadow", "llm_primary"} and not config.provider_ready:
        summary = _build_summary(
            mode=mode,
            profile=profile,
            config=config,
            cases=[],
            cases_total=len(cases),
            skipped=True,
            skip_reason="Azure runtime is not configured for AI smoke. Configure Azure OpenAI chat/embedding and Azure AI Search.",
        )
        _write_summary(summary, out_dir)
        if not allow_skip:
            raise RuntimeError(summary["skip_reason"])
        return summary

    if sync_knowledge and profile != "azure":
        raise ValueError("--sync-knowledge is only supported with the azure profile.")

    source_doc_ids: list[str] = []
    results: list[dict[str, Any]] = []
    try:
        if sync_knowledge:
            cases = _sync_azure_smoke_knowledge(cases=cases, config=config, source_doc_ids=source_doc_ids)
        for case in cases:
            row = dict(case.get("row") or {})
            knowledge_docs = list(case.get("knowledge_docs") or [])
            result = run_ai_copilot_assessment(row, knowledge_docs, config=config)
            assessment = result["assessment"]
            evidences = result.get("evidences") or []
            checks = _evaluate_case(
                _expect_for_execution(case.get("expect") or {}, profile=profile, mode=mode),
                assessment,
                evidences,
            )
            results.append(
                {
                    "id": case.get("id"),
                    "title": case.get("title"),
                    "passed": all(item["passed"] for item in checks),
                    "checks": checks,
                    "analysis_label": assessment.get("analysis_label"),
                    "ai_decision": assessment.get("ai_decision"),
                    "assessment_mode": assessment.get("assessment_mode"),
                    "evidence_count": len(evidences),
                    "heuristic_run_detected": any(
                        "heuristic" in str(run.get("mode") or "") for run in result.get("runs") or []
                    ),
                }
            )
    finally:
        if source_doc_ids:
            delete_azure_search_chunks_for_source_docs(config, source_doc_ids=source_doc_ids)

    summary = _build_summary(
        mode=mode,
        profile=profile,
        config=config,
        cases=results,
        cases_total=len(results),
        skipped=False,
        skip_reason="",
    )
    _write_summary(summary, out_dir)
    return summary


def _sync_azure_smoke_knowledge(
    *,
    cases: list[dict[str, Any]],
    config: AICopilotConfig,
    source_doc_ids: list[str],
) -> list[dict[str, Any]]:
    project_id = f"ai-smoke-{uuid.uuid4().hex}"
    prepared_cases: list[dict[str, Any]] = []
    for case in cases:
        prepared_case = dict(case)
        prepared_case["project_id"] = project_id
        prepared_docs: list[dict[str, Any]] = []
        for position, document in enumerate(case.get("knowledge_docs") or [], start=1):
            source_doc_id = f"{project_id}-{case['id']}-{position}"
            prepared_doc = {**document, "id": source_doc_id, "project_id": project_id}
            source_doc_ids.append(source_doc_id)
            sync_result = sync_knowledge_document_index(project_id, prepared_doc, config=config)
            if sync_result.get("status") != "READY":
                raise RuntimeError(
                    f"Azure smoke knowledge sync failed for {case['id']} ({source_doc_id}): {sync_result}"
                )
            prepared_docs.append(prepared_doc)
        prepared_case["knowledge_docs"] = prepared_docs
        prepared_cases.append(prepared_case)
    return prepared_cases


def _build_smoke_config(*, mode: str, profile: str, trace_dir: Path) -> AICopilotConfig:
    if profile == "azure":
        config = load_ai_copilot_config("azure")
        retrieval_backend = config.retrieval_backend
        if mode in {"llm_shadow", "llm_primary"} and retrieval_backend == "local":
            retrieval_backend = "azure_sdk"
        return dataclasses.replace(
            config,
            assessment_mode=mode,
            retrieval_backend=retrieval_backend,
            trace_dir=trace_dir,
        )
    return AICopilotConfig(
        assessment_mode=mode,
        retrieval_backend="local",
        top_k=5,
        trace_dir=trace_dir,
        azure_auth_mode="auto",
        azure_allow_interactive_login=False,
        azure_search_endpoint="",
        azure_search_api_key="",
        azure_search_index_name="reqpilot-knowledge",
        azure_openai_endpoint="",
        azure_openai_api_key="",
        azure_openai_api_version="2024-10-21",
        azure_openai_chat_deployment="",
        azure_openai_embedding_deployment="",
        azure_openai_embedding_dimensions=1536,
        azure_search_semantic_configuration="",
        llm_timeout_seconds=45.0,
    )


def _build_summary(
    *,
    mode: str,
    profile: str,
    config: AICopilotConfig,
    cases: list[dict[str, Any]],
    cases_total: int,
    skipped: bool,
    skip_reason: str,
) -> dict[str, Any]:
    passed = sum(1 for item in cases if item["passed"])
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile": profile,
        "provider": config.provider,
        "retrieval_backend": config.retrieval_backend,
        "skipped": skipped,
        "skip_reason": skip_reason,
        "cases_total": cases_total,
        "cases_passed": passed,
        "cases_failed": 0 if skipped else cases_total - passed,
        "overall_pass": (not skipped) and passed == cases_total,
        "mode": mode,
        "heuristic_run_detected": any(bool(item.get("heuristic_run_detected")) for item in cases),
        "cases": cases,
    }


def _write_summary(summary: dict[str, Any], out_dir: Path) -> None:
    summary_json = out_dir / "ai_smoke_summary.json"
    summary_md = out_dir / "ai_smoke_summary.md"
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md.write_text(_render_md(summary), encoding="utf-8")


def _evaluate_case(expect: dict[str, Any], assessment: dict[str, Any], evidences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for key in ("analysis_label", "ai_decision", "assessment_mode"):
        if key not in expect:
            continue
        actual = str(assessment.get(key) or "")
        expected = str(expect.get(key) or "")
        checks.append({"field": key, "expected": expected, "actual": actual, "passed": actual == expected})
    if "requires_evidence" in expect:
        expected_bool = bool(expect.get("requires_evidence"))
        actual_bool = bool(evidences)
        checks.append(
            {
                "field": "requires_evidence",
                "expected": expected_bool,
                "actual": actual_bool,
                "passed": actual_bool == expected_bool,
            }
        )
    return checks


def _expect_for_execution(expect: dict[str, Any], *, profile: str, mode: str) -> dict[str, Any]:
    resolved = dict(expect)
    if profile == "azure" and mode in {"llm_shadow", "llm_primary"} and resolved.get("assessment_mode") == "heuristic":
        resolved["assessment_mode"] = mode
    return resolved


def _render_md(summary: dict[str, Any]) -> str:
    lines = [
        "# AI Smoke Summary",
        "",
        f"- generated_at: `{summary.get('generated_at', '')}`",
        f"- mode: `{summary.get('mode', '')}`",
        f"- cases: `{summary.get('cases_passed', 0)}/{summary.get('cases_total', 0)}`",
        f"- overall_pass: `{summary.get('overall_pass', False)}`",
        "",
        "## Cases",
        "",
    ]
    for case in summary.get("cases") or []:
        lines.append(f"### {case.get('id', '')} {case.get('title', '')}")
        lines.append(f"- passed: `{case.get('passed', False)}`")
        lines.append(f"- analysis_label: `{case.get('analysis_label', '')}`")
        lines.append(f"- ai_decision: `{case.get('ai_decision', '')}`")
        lines.append(f"- evidence_count: `{case.get('evidence_count', 0)}`")
        for check in case.get("checks") or []:
            lines.append(
                f"- {check.get('field')}: expected `{check.get('expected')}` / actual `{check.get('actual')}` / pass `{check.get('passed')}`"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    main()
