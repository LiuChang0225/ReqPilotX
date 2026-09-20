#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable


CATALOG_SCHEMA_VERSION = "reqpilotx-structure-failure-pattern-catalog/v1"
SELECTION_SCHEMA_VERSION = "reqpilotx-structure-failure-pattern-selection/v1"
_SIGNAL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_PATTERN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_MAX_PATTERNS = 3


class FailurePatternCatalogError(RuntimeError):
    pass


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FailurePatternCatalogError("failure_pattern_catalog_invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != (
        CATALOG_SCHEMA_VERSION
    ):
        raise FailurePatternCatalogError("failure_pattern_catalog_schema_invalid")
    if value.get("max_selected_patterns") != _MAX_PATTERNS:
        raise FailurePatternCatalogError("failure_pattern_catalog_limit_invalid")
    patterns = value.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        raise FailurePatternCatalogError("failure_pattern_catalog_empty")
    identifiers: set[str] = set()
    previous_order: tuple[int, str] | None = None
    for item in patterns:
        if not isinstance(item, dict):
            raise FailurePatternCatalogError("failure_pattern_item_invalid")
        pattern_id = str(item.get("pattern_id") or "")
        priority = item.get("priority")
        signals = item.get("trigger_signals")
        if (
            _PATTERN_ID_PATTERN.fullmatch(pattern_id) is None
            or pattern_id in identifiers
            or not isinstance(priority, int)
            or priority < 0
            or not isinstance(signals, list)
            or not signals
            or signals != sorted(set(signals))
            or any(_SIGNAL_PATTERN.fullmatch(str(signal)) is None for signal in signals)
        ):
            raise FailurePatternCatalogError("failure_pattern_item_identity_invalid")
        order = (priority, pattern_id)
        if previous_order is not None and order <= previous_order:
            raise FailurePatternCatalogError("failure_pattern_catalog_order_invalid")
        previous_order = order
        identifiers.add(pattern_id)
        for field in (
            "decision_reminder",
            "forbidden_shortcut",
        ):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise FailurePatternCatalogError("failure_pattern_item_text_invalid")
        for field in (
            "evidence_queries",
            "required_host_checks",
            "tool_intents",
        ):
            values = item.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(entry, str) or not entry.strip() for entry in values)
            ):
                raise FailurePatternCatalogError("failure_pattern_item_list_invalid")
    return value


def select_patterns(
    catalog: dict[str, Any],
    signals: Iterable[str],
    *,
    max_patterns: int = _MAX_PATTERNS,
) -> dict[str, Any]:
    if not 1 <= max_patterns <= _MAX_PATTERNS:
        raise FailurePatternCatalogError("failure_pattern_selection_limit_invalid")
    normalized = tuple(sorted(set(str(signal).strip() for signal in signals)))
    if any(_SIGNAL_PATTERN.fullmatch(signal) is None for signal in normalized):
        raise FailurePatternCatalogError("failure_pattern_signal_invalid")
    selected = [
        item
        for item in catalog["patterns"]
        if set(item["trigger_signals"]).intersection(normalized)
    ][:max_patterns]
    matched_signals = {
        signal
        for item in selected
        for signal in item["trigger_signals"]
        if signal in normalized
    }
    compact = [
        {
            "pattern_id": item["pattern_id"],
            "matched_signals": sorted(
                set(item["trigger_signals"]).intersection(normalized)
            ),
            "decision_reminder": item["decision_reminder"],
            "evidence_queries": item["evidence_queries"],
            "required_host_checks": item["required_host_checks"],
            "tool_intents": item["tool_intents"],
            "forbidden_shortcut": item["forbidden_shortcut"],
        }
        for item in selected
    ]
    payload = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "catalog_version": catalog["catalog_version"],
        "catalog_sha256": _canonical_sha256(catalog),
        "requested_signals": list(normalized),
        "selected_patterns": compact,
        "unmatched_signals": sorted(set(normalized).difference(matched_signals)),
        "selection_truncated": len(
            [
                item
                for item in catalog["patterns"]
                if set(item["trigger_signals"]).intersection(normalized)
            ]
        )
        > len(selected),
    }
    return {
        **payload,
        "selection_sha256": _canonical_sha256(payload),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--catalog",
        type=Path,
        default=(
            Path(__file__).resolve().parents[1]
            / "references"
            / "structure-failure-patterns.json"
        ),
    )
    parser.add_argument("--signal", action="append", default=[])
    parser.add_argument("--max-patterns", type=int, default=_MAX_PATTERNS)
    args = parser.parse_args()
    try:
        result = select_patterns(
            load_catalog(args.catalog),
            args.signal,
            max_patterns=args.max_patterns,
        )
    except FailurePatternCatalogError as exc:
        print(json.dumps({"reason_code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
