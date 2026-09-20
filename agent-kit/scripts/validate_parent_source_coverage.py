#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

sys.dont_write_bytecode = True

from validate_bundle_references import (  # noqa: E402
    ReferenceValidationError,
    validate_artifact_references,
)


_DISPOSITIONS = {"ASSERTION", "FACET", "CONTEXT", "UNRESOLVED"}


class ParentCoverageError(RuntimeError):
    pass


def validate_parent_source_coverage(
    workspace: Path,
    parent_provisions: Path,
    requirements: Path,
    *,
    expected_source_sha256: str,
    expected_bundle_sha256: str,
) -> dict[str, Any]:
    d3_refs = validate_artifact_references(
        workspace,
        parent_provisions,
        expected_source_sha256=expected_source_sha256,
        expected_bundle_sha256=expected_bundle_sha256,
    )
    d4_refs = validate_artifact_references(
        workspace,
        requirements,
        expected_source_sha256=expected_source_sha256,
        expected_bundle_sha256=expected_bundle_sha256,
    )
    root = workspace.expanduser().resolve(strict=True)
    d3 = _read_artifact(root, parent_provisions)
    d4 = _read_artifact(root, requirements)
    source_text = _source_text_index(root / "pages/text-index.jsonl")
    parents = _records(d3, "parent_provisions")
    decompositions = _records(d4, "decompositions")

    parent_by_id = _unique_records(parents, "parent_id", "D3")
    decomposition_by_id = _unique_records(decompositions, "parent_id", "D4")
    findings: list[dict[str, Any]] = []
    if d3_refs["status"] != "PASS":
        findings.append({"code": "d3_bundle_reference_invalid", "scope": "D3"})
    if d4_refs["status"] != "PASS":
        findings.append({"code": "d4_bundle_reference_invalid", "scope": "D4"})
    if set(parent_by_id) != set(decomposition_by_id):
        for parent_id in sorted(set(parent_by_id).difference(decomposition_by_id)):
            findings.append({"code": "parent_decomposition_missing", "scope": parent_id})
        for parent_id in sorted(set(decomposition_by_id).difference(parent_by_id)):
            findings.append({"code": "decomposition_parent_unknown", "scope": parent_id})

    covered_parent_count = 0
    for parent_id in sorted(set(parent_by_id).intersection(decomposition_by_id)):
        parent = parent_by_id[parent_id]
        decomposition = decomposition_by_id[parent_id]
        handles = _source_handles(parent, findings, parent_id)
        coverage = decomposition.get("source_coverage")
        if not isinstance(coverage, list):
            findings.append(
                {"code": "source_coverage_contract_missing", "scope": parent_id}
            )
            continue
        by_handle: dict[str, list[dict[str, Any]]] = {}
        coverage_contract_valid = True
        for item in coverage:
            if not isinstance(item, dict) or not isinstance(
                item.get("source_handle"), str
            ):
                coverage_contract_valid = False
                continue
            handle = item["source_handle"]
            ranges = item.get("ranges")
            if handle in by_handle or not isinstance(ranges, list):
                coverage_contract_valid = False
                continue
            by_handle[handle] = ranges
        if not coverage_contract_valid or set(by_handle) != handles:
            findings.append({"code": "source_coverage_handles_invalid", "scope": parent_id})
            continue
        parent_valid = True
        for handle in sorted(handles):
            text = source_text.get(handle)
            if text is None:
                findings.append(
                    {"code": "source_handle_text_unavailable", "scope": f"{parent_id}:{handle}"}
                )
                parent_valid = False
                continue
            if not _ranges_cover_text(
                text,
                by_handle[handle],
                findings=findings,
                scope=f"{parent_id}:{handle}",
            ):
                parent_valid = False
        if parent_valid:
            covered_parent_count += 1

    unique_findings = _unique_findings(findings)
    return {
        "schema_version": "reqpilotx-parent-source-coverage-validation/v1",
        "status": "PASS" if not unique_findings else "FAIL",
        "source_sha256": expected_source_sha256,
        "bundle_sha256": expected_bundle_sha256,
        "parent_count": len(parent_by_id),
        "decomposition_count": len(decomposition_by_id),
        "exactly_covered_parent_count": covered_parent_count,
        "finding_count": len(unique_findings),
        "findings": unique_findings,
        "d3_reference_receipt": d3_refs,
        "d4_reference_receipt": d4_refs,
    }


def _ranges_cover_text(
    text: str,
    ranges: list[object],
    *,
    findings: list[dict[str, Any]],
    scope: str,
) -> bool:
    normalized: list[tuple[int, int, str, str]] = []
    valid = True
    for item in ranges:
        if not isinstance(item, dict):
            valid = False
            continue
        start = item.get("start")
        end = item.get("end")
        disposition = item.get("disposition")
        target_id = item.get("target_id")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(text)
            or disposition not in _DISPOSITIONS
            or not isinstance(target_id, str)
            or not target_id
        ):
            valid = False
            continue
        normalized.append((start, end, disposition, target_id))
    normalized.sort()
    if not valid or not normalized:
        findings.append({"code": "source_ranges_invalid", "scope": scope})
        return False
    previous_end = 0
    for start, end, _, _ in normalized:
        if start < previous_end:
            findings.append({"code": "source_ranges_overlap", "scope": scope})
            return False
        if any(not character.isspace() for character in text[previous_end:start]):
            findings.append({"code": "source_nonwhitespace_gap", "scope": scope})
            return False
        previous_end = end
    if any(not character.isspace() for character in text[previous_end:]):
        findings.append({"code": "source_nonwhitespace_gap", "scope": scope})
        return False
    return True


def _source_handles(
    parent: dict[str, Any],
    findings: list[dict[str, Any]],
    parent_id: str,
) -> set[str]:
    raw = parent.get("source_handles")
    if not isinstance(raw, list) or not raw or any(
        not isinstance(item, str) or not item for item in raw
    ):
        findings.append({"code": "parent_source_handles_invalid", "scope": parent_id})
        return set()
    values = set(raw)
    if len(values) != len(raw):
        findings.append({"code": "parent_source_handles_invalid", "scope": parent_id})
    return values


def _source_text_index(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if not line:
                continue
            item = json.loads(line)
            handle = item.get("source_handle")
            text = item.get("text")
            if not isinstance(handle, str) or not isinstance(text, str) or handle in values:
                raise ParentCoverageError("workspace_text_index_invalid")
            values[handle] = text
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ParentCoverageError("workspace_text_index_invalid") from exc
    return values


def _read_artifact(root: Path, path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ParentCoverageError("artifact_outside_workspace") from exc
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ParentCoverageError("artifact_json_invalid") from exc
    if not isinstance(value, dict):
        raise ParentCoverageError("artifact_json_invalid")
    return value


def _records(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = value.get(key)
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ParentCoverageError(f"{key}_records_required")
    return raw


def _unique_records(
    values: list[dict[str, Any]], key: str, stage: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        identifier = item.get(key)
        if not isinstance(identifier, str) or not identifier or identifier in result:
            raise ParentCoverageError(f"{stage.lower()}_{key}_identity_invalid")
        result[identifier] = item
    return result


def _unique_findings(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        identity = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if identity not in seen:
            seen.add(identity)
            result.append(value)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--parent-provisions", required=True, type=Path)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-bundle-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = validate_parent_source_coverage(
            args.workspace,
            args.parent_provisions,
            args.requirements,
            expected_source_sha256=args.expected_source_sha256,
            expected_bundle_sha256=args.expected_bundle_sha256,
        )
    except (OSError, ReferenceValidationError, ParentCoverageError) as exc:
        print(json.dumps({"schema_version": "reqpilotx-parent-source-coverage-validation/v1", "status": "FAIL", "reason_code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
