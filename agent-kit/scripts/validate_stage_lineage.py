#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

sys.dont_write_bytecode = True

from validate_bundle_references import (  # noqa: E402
    ReferenceValidationError,
    validate_artifact_references,
)


class StageLineageError(RuntimeError):
    pass


def validate_stage_lineage(
    workspace: Path,
    logical_structure: Path,
    parent_provisions: Path,
    *,
    expected_source_sha256: str,
    expected_bundle_sha256: str,
) -> dict[str, Any]:
    d2_refs = validate_artifact_references(
        workspace,
        logical_structure,
        expected_source_sha256=expected_source_sha256,
        expected_bundle_sha256=expected_bundle_sha256,
    )
    d3_refs = validate_artifact_references(
        workspace,
        parent_provisions,
        expected_source_sha256=expected_source_sha256,
        expected_bundle_sha256=expected_bundle_sha256,
    )
    findings: list[dict[str, str]] = []
    if d2_refs["status"] != "PASS":
        findings.append({"code": "d2_bundle_reference_invalid", "scope": "D2"})
    if d3_refs["status"] != "PASS":
        findings.append({"code": "d3_bundle_reference_invalid", "scope": "D3"})

    root = workspace.expanduser().resolve(strict=True)
    d2 = _read_artifact(root, logical_structure)
    d3 = _read_artifact(root, parent_provisions)
    manifest = _read_json(root / "manifest.json")
    page_count = _positive_int(_mapping(manifest, "document").get("page_count"))
    table_index = _read_json(root / "tables/index.json").get("tables")
    if not isinstance(table_index, list):
        raise StageLineageError("workspace_table_index_invalid")
    table_pages = {
        str(item["table_token"]): _positive_int(item.get("page_number"))
        for item in table_index
        if isinstance(item, dict) and isinstance(item.get("table_token"), str)
    }

    units = _records(d2, "document_units")
    objects = _records(d2, "logical_objects")
    families = _records(d3, "parent_families")
    unit_ids = _unique_ids(units, "unit_id", findings, "D2")
    object_ids = _unique_ids(objects, "object_id", findings, "D2")
    _unique_ids(families, "family_id", findings, "D3")

    unit_pages: dict[str, set[int]] = {}
    page_owners: Counter[int] = Counter()
    for unit in units:
        unit_id = str(unit.get("unit_id") or "")
        pages = _pages(unit, findings, unit_id)
        unit_pages[unit_id] = pages
        page_owners.update(pages)
    for page in range(1, page_count + 1):
        if page_owners[page] != 1:
            findings.append(
                {
                    "code": "document_unit_page_ownership_invalid",
                    "scope": f"P{page:04d}",
                }
            )

    object_pages: dict[str, set[int]] = {}
    object_tables: dict[str, set[str]] = {}
    table_owners: Counter[str] = Counter()
    for item in objects:
        object_id = str(item.get("object_id") or "")
        unit_id = str(item.get("unit_id") or "")
        pages = _pages(item, findings, object_id)
        tables = _strings(item, "physical_table_tokens", findings, object_id)
        object_pages[object_id] = pages
        object_tables[object_id] = tables
        table_owners.update(tables)
        if unit_id not in unit_ids or not pages.issubset(unit_pages.get(unit_id, set())):
            findings.append({"code": "logical_object_unit_scope_invalid", "scope": object_id})
        if not str(item.get("business_role") or ""):
            findings.append({"code": "logical_object_role_missing", "scope": object_id})
        title_refs = item.get("title_evidence_refs")
        if not isinstance(title_refs, list) or not title_refs:
            findings.append({"code": "logical_object_title_evidence_missing", "scope": object_id})
        for token in tables:
            if token not in table_pages or table_pages[token] not in pages:
                findings.append({"code": "logical_object_table_page_invalid", "scope": token})
    for token in sorted(table_pages):
        if table_owners[token] != 1:
            findings.append({"code": "physical_table_ownership_invalid", "scope": token})

    for family in families:
        family_id = str(family.get("family_id") or "")
        linked_objects = _strings(
            family,
            "logical_object_ids",
            findings,
            family_id,
        )
        if not linked_objects or not linked_objects.issubset(object_ids):
            findings.append({"code": "parent_family_object_link_invalid", "scope": family_id})
            continue
        allowed_pages = set().union(*(object_pages[item] for item in linked_objects))
        allowed_tables = set().union(*(object_tables[item] for item in linked_objects))
        claimed_pages = _pages(family, findings, family_id)
        claimed_tables = _strings(
            family,
            "physical_table_tokens",
            findings,
            family_id,
        )
        if not claimed_pages.issubset(allowed_pages):
            findings.append({"code": "parent_family_page_scope_conflict", "scope": family_id})
        if not claimed_tables.issubset(allowed_tables):
            findings.append({"code": "parent_family_table_scope_conflict", "scope": family_id})

    unique_findings = [
        dict(code=code, scope=scope)
        for code, scope in sorted({(item["code"], item["scope"]) for item in findings})
    ]
    return {
        "schema_version": "reqpilotx-stage-lineage-validation/v1",
        "status": "PASS" if not unique_findings else "FAIL",
        "source_sha256": expected_source_sha256,
        "bundle_sha256": expected_bundle_sha256,
        "document_unit_count": len(units),
        "logical_object_count": len(objects),
        "parent_family_count": len(families),
        "finding_count": len(unique_findings),
        "findings": unique_findings,
        "d2_reference_receipt": d2_refs,
        "d3_reference_receipt": d3_refs,
    }


def _read_artifact(root: Path, path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise StageLineageError("artifact_outside_workspace") from exc
    return _read_json(resolved)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StageLineageError("artifact_json_invalid") from exc
    if not isinstance(value, dict):
        raise StageLineageError("artifact_json_invalid")
    return value


def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise StageLineageError(f"{key}_mapping_required")
    return nested


def _records(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    nested = value.get(key)
    if not isinstance(nested, list) or not nested or not all(
        isinstance(item, dict) for item in nested
    ):
        raise StageLineageError(f"{key}_records_required")
    return nested


def _unique_ids(
    values: list[dict[str, Any]],
    key: str,
    findings: list[dict[str, str]],
    stage: str,
) -> set[str]:
    identifiers = [str(item.get(key) or "") for item in values]
    if any(not item for item in identifiers) or len(identifiers) != len(set(identifiers)):
        findings.append({"code": f"{key}_identity_invalid", "scope": stage})
    return {item for item in identifiers if item}


def _pages(
    value: dict[str, Any],
    findings: list[dict[str, str]],
    scope: str,
) -> set[int]:
    raw = value.get("page_numbers")
    if not isinstance(raw, list) or not raw or any(
        type(item) is not int or item < 1 for item in raw
    ):
        findings.append({"code": "page_numbers_invalid", "scope": scope})
        return set()
    pages = set(raw)
    if len(pages) != len(raw):
        findings.append({"code": "page_numbers_invalid", "scope": scope})
    return pages


def _strings(
    value: dict[str, Any],
    key: str,
    findings: list[dict[str, str]],
    scope: str,
) -> set[str]:
    raw = value.get(key)
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        findings.append({"code": f"{key}_invalid", "scope": scope})
        return set()
    values = set(raw)
    if len(values) != len(raw):
        findings.append({"code": f"{key}_invalid", "scope": scope})
    return values


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise StageLineageError("positive_integer_required")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--logical-structure", required=True, type=Path)
    parser.add_argument("--parent-provisions", required=True, type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-bundle-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = validate_stage_lineage(
            args.workspace,
            args.logical_structure,
            args.parent_provisions,
            expected_source_sha256=args.expected_source_sha256,
            expected_bundle_sha256=args.expected_bundle_sha256,
        )
    except (OSError, ReferenceValidationError, StageLineageError) as exc:
        print(json.dumps({"schema_version": "reqpilotx-stage-lineage-validation/v1", "status": "FAIL", "reason_code": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
