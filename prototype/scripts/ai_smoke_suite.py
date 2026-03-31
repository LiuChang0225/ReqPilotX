from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.ai_copilot import AICopilotConfig, run_ai_copilot_assessment


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local AI Copilot smoke cases without Azure dependencies.")
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
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parents[1]
    cases_path = (project_dir / args.cases).resolve()
    out_dir = (project_dir / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    config = AICopilotConfig(
        assessment_mode=args.mode,
        retrieval_backend="local",
        top_k=5,
        trace_dir=out_dir / "traces",
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

    results: list[dict[str, Any]] = []
    for case in cases:
        row = dict(case.get("row") or {})
        knowledge_docs = list(case.get("knowledge_docs") or [])
        result = run_ai_copilot_assessment(row, knowledge_docs, config=config)
        assessment = result["assessment"]
        evidences = result.get("evidences") or []
        checks = _evaluate_case(case.get("expect") or {}, assessment, evidences)
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
            }
        )

    passed = sum(1 for item in results if item["passed"])
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases_total": len(results),
        "cases_passed": passed,
        "cases_failed": len(results) - passed,
        "overall_pass": passed == len(results),
        "mode": args.mode,
        "cases": results,
    }
    summary_json = out_dir / "ai_smoke_summary.json"
    summary_md = out_dir / "ai_smoke_summary.md"
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md.write_text(_render_md(summary), encoding="utf-8")

    print(f"[ai-smoke] cases: {passed}/{len(results)}")
    print(f"[ai-smoke] summary json: {summary_json}")
    print(f"[ai-smoke] summary md: {summary_md}")


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
