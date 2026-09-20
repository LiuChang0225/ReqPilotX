"""Decision-level deterministic baseline evaluation.

Measures: decision_accuracy, false_accept_rate, false_reject_rate,
clarification_rate, comparison_accuracy against gold-labeled cases.

This script does not run the product RAG/LLM pipeline. Unsupported comparisons
must abstain; Gold labels are never copied into predictions. Use
``scripts/ai_gold_eval.py`` for the release-quality product-path gate.

Usage:
    python scripts/decision_eval.py --corpus eval/public/decision_cases.json
    python scripts/decision_eval.py --corpus /controlled/private/cases.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


EVAL_ROOT = Path(__file__).resolve().parent.parent / "eval"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_corpus(corpus_path: str) -> dict[str, Any]:
    path = Path(corpus_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def evaluate_case(case: dict[str, Any], *, simulated_decision: str) -> dict[str, Any]:
    """Evaluate a single case against a simulated or real decision."""
    expected = str(case.get("expected_decision") or "").strip().upper()
    actual = str(simulated_decision or "").strip().upper()

    is_correct = expected == actual
    is_false_accept = expected != "ACCEPT" and actual == "ACCEPT"
    is_false_reject = expected not in {"REJECT", "NEED_CLARIFICATION"} and actual == "REJECT"

    return {
        "case_id": case.get("id", ""),
        "category": case.get("category", ""),
        "expected": expected,
        "actual": actual,
        "is_correct": is_correct,
        "is_false_accept": is_false_accept,
        "is_false_reject": is_false_reject,
        "decision_rationale": case.get("decision_rationale", ""),
    }


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        return {"total": 0}

    correct = sum(1 for r in results if r["is_correct"])
    false_accepts = sum(1 for r in results if r["is_false_accept"])
    false_rejects = sum(1 for r in results if r["is_false_reject"])
    clarifications = sum(1 for r in results if r["actual"] == "NEED_CLARIFICATION")

    return {
        "total": total,
        "correct": correct,
        "decision_accuracy": round(correct / total, 4),
        "false_accept_rate": round(false_accepts / total, 4),
        "false_reject_rate": round(false_rejects / total, 4),
        "clarification_rate": round(clarifications / total, 4),
        "per_category": _compute_per_category(results),
    }


def _compute_per_category(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    categories: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        cat = r.get("category", "unknown")
        categories.setdefault(cat, []).append(r)

    per_cat: dict[str, dict[str, Any]] = {}
    for cat, items in sorted(categories.items()):
        total = len(items)
        correct = sum(1 for r in items if r["is_correct"])
        per_cat[cat] = {
            "total": total,
            "correct": correct,
            "accuracy": round(correct / total, 4) if total > 0 else 0.0,
        }
    return per_cat


def simulate_decisions_from_values(cases: list[dict[str, Any]]) -> list[str]:
    """Simulate decisions from supplier and requirement value comparisons.

    This is a deterministic simulation for offline evaluation without
    requiring a live AI backend.
    """
    decisions: list[str] = []
    for case in cases:
        supplier = str(case.get("supplier_value", case.get("vt_value")) or "").strip()
        requirement = str(case.get("requirement_value", case.get("byd_value")) or "").strip()
        if not supplier or not requirement:
            decisions.append("NEED_CLARIFICATION")
            continue

        # Normalize for comparison
        supplier_norm = supplier.lower().replace(" ", "").replace("°", "").replace("±", "+/-")
        requirement_norm = requirement.lower().replace(" ", "").replace("°", "").replace("±", "+/-")

        # Exact or near-exact match → ACCEPT
        if supplier_norm == requirement_norm or _values_are_equivalent(supplier, requirement):
            decisions.append("ACCEPT")
            continue

        # The deterministic baseline does not understand direction, tolerance,
        # authority or applicability for a non-equivalent pair. Copying the
        # Gold label here would leak the answer and produce a fake 100% score.
        decisions.append("NEED_CLARIFICATION")
    return decisions


def _values_are_equivalent(supplier: str, requirement: str) -> bool:
    """Check if two spec values are equivalent despite formatting differences."""
    # Handle unit conversions: hPa vs kPa
    supplier_clean = supplier.lower().strip()
    requirement_clean = requirement.lower().strip()

    # Strip standard references
    for prefix in ("equal to ", "per ", "din ", "iec"):
        idx = supplier_clean.find(prefix)
        if idx > 0:
            supplier_clean = supplier_clean[:idx].strip()
        idx = requirement_clean.find(prefix)
        if idx > 0:
            requirement_clean = requirement_clean[:idx].strip()

    # Remove parenthetical notes
    import re
    supplier_clean = re.sub(r"\(.*?\)", "", supplier_clean).strip()
    requirement_clean = re.sub(r"\(.*?\)", "", requirement_clean).strip()

    return supplier_clean == requirement_clean


def run_evaluation(corpus: dict[str, Any], *, output_dir: Path | None = None) -> dict[str, Any]:
    cases = corpus.get("cases", [])
    corpus_id = corpus.get("corpus_id", "unknown")

    print(f"Evaluating corpus: {corpus_id} ({len(cases)} cases)")
    print("Mode: deterministic_baseline (unsupported comparisons abstain)")
    print("=" * 60)

    # Simulate decisions (offline mode)
    decisions = simulate_decisions_from_values(cases)

    # Evaluate each case
    results = []
    for case, decision in zip(cases, decisions):
        result = evaluate_case(case, simulated_decision=decision)
        results.append(result)
        status = "PASS" if result["is_correct"] else "FAIL"
        flag = ""
        if result["is_false_accept"]:
            flag = " [FALSE ACCEPT]"
        elif result["is_false_reject"]:
            flag = " [FALSE REJECT]"
        print(f"  [{status}] {result['case_id']:20s} expected={result['expected']:20s} actual={result['actual']:20s}{flag}")

    # Compute metrics
    metrics = compute_metrics(results)
    print()
    print("=" * 60)
    print(f"Total cases:          {metrics['total']}")
    print(f"Decision accuracy:    {metrics['decision_accuracy']:.1%}")
    print(f"False accept rate:    {metrics['false_accept_rate']:.1%}")
    print(f"False reject rate:    {metrics['false_reject_rate']:.1%}")
    print(f"Clarification rate:   {metrics['clarification_rate']:.1%}")
    print()
    print("Per-category breakdown:")
    for cat, cat_metrics in metrics.get("per_category", {}).items():
        print(f"  {cat:25s} accuracy={cat_metrics['accuracy']:.1%} ({cat_metrics['correct']}/{cat_metrics['total']})")

    # Quality gate check
    print()
    gate_pass = True
    if metrics["decision_accuracy"] < 0.70:
        print(f"GATE FAIL: decision_accuracy {metrics['decision_accuracy']:.1%} < 70%")
        gate_pass = False
    if metrics["false_accept_rate"] > 0.10:
        print(f"GATE FAIL: false_accept_rate {metrics['false_accept_rate']:.1%} > 10%")
        gate_pass = False
    if gate_pass:
        print("GATE PASS: All quality gates met.")

    # Save results
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        result_file = output_dir / "decision_eval_result.json"
        output_payload = {
            "evaluation_mode": "deterministic_baseline",
            "uses_gold_for_prediction": False,
            "corpus_id": corpus_id,
            "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "metrics": metrics,
            "gate_pass": gate_pass,
            "results": results,
        }
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(output_payload, f, ensure_ascii=False, indent=2)
        print(f"\nResults saved to: {result_file}")

    return {
        "evaluation_mode": "deterministic_baseline",
        "uses_gold_for_prediction": False,
        "metrics": metrics,
        "gate_pass": gate_pass,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Decision-level RAG evaluation")
    parser.add_argument("--corpus", required=True, help="Path to evaluation corpus JSON")
    parser.add_argument("--output-dir", default="", help="Directory to save results")
    args = parser.parse_args()

    corpus = load_corpus(args.corpus)
    output_dir = Path(args.output_dir) if args.output_dir else None
    result = run_evaluation(corpus, output_dir=output_dir)

    sys.exit(0 if result["gate_pass"] else 1)


if __name__ == "__main__":
    main()
