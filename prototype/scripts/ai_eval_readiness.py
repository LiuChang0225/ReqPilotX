#!/usr/bin/env python3
"""Report whether existing product data is ready to act as AI Gold evidence.

The inventory is intentionally aggregate-only: it never emits requirement text,
evidence excerpts, file names, project names, or raw identifiers.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


FINAL_DECISIONS = ("ACCEPT", "REJECT", "DEVIATION_ACCEPT", "NEED_CLARIFICATION")
EXPLICIT_LABELS = ("REQUIREMENT", "INFO")
MIN_EXPLICIT_POSITIVES = 100
MIN_EXPLICIT_NEGATIVES = 50
MIN_DECISION_CASES = 100
MIN_DECISION_CLASS_CASES = 20
MIN_LABELED_DOCUMENTS = 5
MIN_LABELED_PROJECTS = 2
EXPLICIT_GOLD_COLUMN_NAMES = {
    "expected_analysis_label",
    "gold_analysis_label",
    "gold_is_requirement",
    "is_explicit_requirement",
}


def build_readiness_report(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    required_tables = {"requirements", "assessments", "review_logs"}
    missing_tables = sorted(required_tables - tables)
    if missing_tables:
        return {
            "schema_version": "ai_eval_readiness.v1",
            "overall_ready": False,
            "blockers": [f"missing_table:{name}" for name in missing_tables],
        }

    requirement_count = _scalar(connection, "SELECT COUNT(*) FROM requirements")
    assessment_count = _scalar(connection, "SELECT COUNT(*) FROM assessments")
    all_decision_distribution = {
        str(row[0]).strip().upper(): int(row[1])
        for row in connection.execute(
            """
            SELECT UPPER(TRIM(final_decision)), COUNT(*)
            FROM assessments
            WHERE TRIM(COALESCE(final_decision, '')) <> ''
            GROUP BY UPPER(TRIM(final_decision))
            """
        )
    }
    decision_distribution = {
        decision: all_decision_distribution.get(decision, 0) for decision in FINAL_DECISIONS
    }
    unknown_decision_distribution = {
        decision: count
        for decision, count in all_decision_distribution.items()
        if decision not in FINAL_DECISIONS
    }
    labeled_decisions = sum(decision_distribution.values())
    labeled_documents = _scalar(
        connection,
        """
        SELECT COUNT(DISTINCT r.sor_document_id)
        FROM assessments a
        JOIN requirements r ON r.id = a.requirement_id
        WHERE UPPER(TRIM(COALESCE(a.final_decision, ''))) IN
              ('ACCEPT', 'REJECT', 'DEVIATION_ACCEPT', 'NEED_CLARIFICATION')
        """,
    )
    labeled_projects = _scalar(
        connection,
        """
        SELECT COUNT(DISTINCT r.project_id)
        FROM assessments a
        JOIN requirements r ON r.id = a.requirement_id
        WHERE UPPER(TRIM(COALESCE(a.final_decision, ''))) IN
              ('ACCEPT', 'REJECT', 'DEVIATION_ACCEPT', 'NEED_CLARIFICATION')
        """,
    )
    labeled_with_reason = _scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM assessments
        WHERE UPPER(TRIM(COALESCE(final_decision, ''))) IN
              ('ACCEPT', 'REJECT', 'DEVIATION_ACCEPT', 'NEED_CLARIFICATION')
          AND TRIM(COALESCE(engineer_reason, '')) <> ''
        """,
    )
    decision_audit_rows = _scalar(
        connection,
        "SELECT COUNT(*) FROM review_logs WHERE action = 'EDIT_DECISION'",
    )
    distinct_decision_operators = _scalar(
        connection,
        "SELECT COUNT(DISTINCT operator) FROM review_logs WHERE action = 'EDIT_DECISION'",
    )
    dual_reviewed_decisions = _current_dual_reviewed_decisions(connection)
    explicit_inventory = _explicit_gold_inventory(connection)
    explicit_gold_fields = explicit_inventory["fields"]

    missing_decision_classes = [
        decision for decision in FINAL_DECISIONS if decision_distribution.get(decision, 0) == 0
    ]
    decision_blockers: list[str] = []
    if labeled_decisions == 0:
        decision_blockers.append("no_human_final_decisions")
    if missing_decision_classes:
        decision_blockers.append("missing_decision_classes:" + ",".join(missing_decision_classes))
    if dual_reviewed_decisions == 0:
        decision_blockers.append("no_dual_reviewed_decisions")
    if dual_reviewed_decisions < labeled_decisions:
        decision_blockers.append(
            f"insufficient_dual_reviewed_decisions:{dual_reviewed_decisions}/{labeled_decisions}"
        )
    if unknown_decision_distribution:
        decision_blockers.append(
            "unknown_decision_classes:"
            + ",".join(
                f"{decision}={count}"
                for decision, count in sorted(unknown_decision_distribution.items())
            )
        )
    if labeled_with_reason < labeled_decisions:
        decision_blockers.append("missing_engineer_reason")
    if labeled_decisions < MIN_DECISION_CASES:
        decision_blockers.append(f"insufficient_decision_cases:{labeled_decisions}/{MIN_DECISION_CASES}")
    for decision in FINAL_DECISIONS:
        count = decision_distribution.get(decision, 0)
        if count < MIN_DECISION_CLASS_CASES:
            decision_blockers.append(
                f"insufficient_decision_class:{decision}:{count}/{MIN_DECISION_CLASS_CASES}"
            )
    if labeled_documents < MIN_LABELED_DOCUMENTS:
        decision_blockers.append(
            f"insufficient_labeled_documents:{labeled_documents}/{MIN_LABELED_DOCUMENTS}"
        )
    if labeled_projects < MIN_LABELED_PROJECTS:
        decision_blockers.append(
            f"insufficient_labeled_projects:{labeled_projects}/{MIN_LABELED_PROJECTS}"
        )

    explicit_blockers: list[str] = []
    if not explicit_gold_fields:
        explicit_blockers.append("no_independent_explicit_requirement_gold_field")
    if explicit_inventory["labeled"] == 0:
        explicit_blockers.append("no_nonempty_explicit_requirement_labels")
    requirement_labels = explicit_inventory["distribution"]["REQUIREMENT"]
    info_labels = explicit_inventory["distribution"]["INFO"]
    if requirement_labels < MIN_EXPLICIT_POSITIVES:
        explicit_blockers.append(
            f"insufficient_explicit_positive_labels:{requirement_labels}/{MIN_EXPLICIT_POSITIVES}"
        )
    if info_labels < MIN_EXPLICIT_NEGATIVES:
        explicit_blockers.append(
            f"insufficient_explicit_negative_labels:{info_labels}/{MIN_EXPLICIT_NEGATIVES}"
        )
    if explicit_inventory["document_count"] < MIN_LABELED_DOCUMENTS:
        explicit_blockers.append(
            "insufficient_explicit_documents:"
            f"{explicit_inventory['document_count']}/{MIN_LABELED_DOCUMENTS}"
        )
    if explicit_inventory["project_count"] < MIN_LABELED_PROJECTS:
        explicit_blockers.append(
            "insufficient_explicit_projects:"
            f"{explicit_inventory['project_count']}/{MIN_LABELED_PROJECTS}"
        )

    return {
        "schema_version": "ai_eval_readiness.v1",
        "inventory": {
            "requirements": requirement_count,
            "assessments": assessment_count,
        },
        "explicit_requirement_gold": {
            "ready": not explicit_blockers,
            "independent_gold_fields": explicit_gold_fields,
            "labeled": explicit_inventory["labeled"],
            "distribution": explicit_inventory["distribution"],
            "labeled_documents": explicit_inventory["document_count"],
            "labeled_projects": explicit_inventory["project_count"],
            "proposed_minimums": {
                "REQUIREMENT": MIN_EXPLICIT_POSITIVES,
                "INFO": MIN_EXPLICIT_NEGATIVES,
                "documents": MIN_LABELED_DOCUMENTS,
                "projects": MIN_LABELED_PROJECTS,
            },
            "blockers": explicit_blockers,
        },
        "decision_gold": {
            "ready": not decision_blockers,
            "human_labeled": labeled_decisions,
            "distribution": {
                decision: decision_distribution.get(decision, 0) for decision in FINAL_DECISIONS
            },
            "unknown_distribution": unknown_decision_distribution,
            "labeled_documents": labeled_documents,
            "labeled_projects": labeled_projects,
            "labels_with_engineer_reason": labeled_with_reason,
            "decision_audit_rows": decision_audit_rows,
            "distinct_decision_operators": distinct_decision_operators,
            "dual_reviewed": dual_reviewed_decisions,
            "proposed_minimums": {
                "total": MIN_DECISION_CASES,
                "per_class": MIN_DECISION_CLASS_CASES,
                "documents": MIN_LABELED_DOCUMENTS,
                "projects": MIN_LABELED_PROJECTS,
            },
            "blockers": decision_blockers,
        },
        "synthetic_regression_is_gold": False,
        "overall_ready": not explicit_blockers and not decision_blockers,
    }


def _explicit_gold_inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    columns = {
        str(row[1]).strip().lower()
        for row in connection.execute('PRAGMA table_info("requirements")')
    }
    fields = sorted(columns & EXPLICIT_GOLD_COLUMN_NAMES)
    best = {
        "fields": [f"requirements.{column}" for column in fields],
        "labeled": 0,
        "distribution": {label: 0 for label in EXPLICIT_LABELS},
        "document_count": 0,
        "project_count": 0,
    }
    for column in fields:
        rows = connection.execute(
            f'SELECT "{column}", sor_document_id, project_id FROM requirements '
            f'WHERE TRIM(COALESCE(CAST("{column}" AS TEXT), \'\')) <> \'\''
        )
        distribution = {label: 0 for label in EXPLICIT_LABELS}
        documents: set[str] = set()
        projects: set[str] = set()
        for raw_label, document_id, project_id in rows:
            label = _normalize_explicit_label(raw_label)
            if not label:
                continue
            distribution[label] += 1
            documents.add(str(document_id))
            projects.add(str(project_id))
        labeled = sum(distribution.values())
        if labeled > best["labeled"]:
            best.update(
                {
                    "labeled": labeled,
                    "distribution": distribution,
                    "document_count": len(documents),
                    "project_count": len(projects),
                }
            )
    return best


def _normalize_explicit_label(value: Any) -> str:
    normalized = str("" if value is None else value).strip().upper()
    if normalized in {"REQUIREMENT", "TRUE", "1", "YES"}:
        return "REQUIREMENT"
    if normalized in {"INFO", "FALSE", "0", "NO"}:
        return "INFO"
    return ""


def _current_dual_reviewed_decisions(connection: sqlite3.Connection) -> int:
    review_columns = {
        str(row[1]).strip().lower()
        for row in connection.execute('PRAGMA table_info("review_logs")')
    }
    if "new_value" not in review_columns:
        return 0
    current = {
        str(requirement_id): str(decision or "").strip().upper()
        for requirement_id, decision in connection.execute(
            "SELECT requirement_id, final_decision FROM assessments "
            "WHERE TRIM(COALESCE(final_decision, '')) <> ''"
        )
    }
    latest_by_reviewer: dict[tuple[str, str], str] = {}
    rows = connection.execute(
        "SELECT requirement_id, operator, new_value FROM review_logs "
        "WHERE action = 'EDIT_DECISION' ORDER BY operated_at, id"
    )
    for requirement_id, operator, new_value in rows:
        try:
            payload = json.loads(str(new_value or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        decision = str(payload.get("final_decision") or "").strip().upper()
        reviewer = str(operator or "").strip().casefold()
        if reviewer and decision in FINAL_DECISIONS:
            latest_by_reviewer[(str(requirement_id), reviewer)] = decision
    agreed: dict[str, set[str]] = {}
    for (requirement_id, operator), decision in latest_by_reviewer.items():
        if decision == current.get(requirement_id):
            agreed.setdefault(requirement_id, set()).add(operator)
    return sum(1 for operators in agreed.values() if len(operators) >= 2)


def _scalar(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    return int((row or (0,))[0] or 0)


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inventory aggregate AI Gold readiness.")
    parser.add_argument("--db", type=Path, default=Path("storage/app.db"))
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Exit non-zero unless both explicit-recall and decision Gold are structurally ready.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with _read_only_connection(args.db) as connection:
            report = build_readiness_report(connection)
    except sqlite3.Error as exc:
        report = {
            "schema_version": "ai_eval_readiness.v1",
            "overall_ready": False,
            "blockers": [f"database_error:{type(exc).__name__}"],
        }

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 2 if args.require_ready and not report.get("overall_ready") else 0


if __name__ == "__main__":
    raise SystemExit(main())
