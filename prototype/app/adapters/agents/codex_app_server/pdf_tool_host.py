"""Controlled, deterministic PDF host for the B1-C2 synthetic Skill canary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from app.adapters.documents.pdf_source_crop_renderer import (
    inspect_pdf_page_geometry,
)
from app.adapters.documents.pdf_source_ir_adapter import (
    PdfSourceIRAdapter,
    render_pdf_visual_page_input,
)
from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction.build_shadow_preview import (
    PAGE_COMPARISON_SCHEMA_VERSION,
    build_page_comparison_view,
)
from app.application.extraction.build_source_ir import build_source_ir
from app.application.extraction.resolve_structure import (
    build_structure_tree,
    validate_structure_tree_against_source_ir,
)
from app.domain.extraction.structure import StructureNodeKind
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    EXPECTED_NATIVE_SECTION_LABELS,
    EXPECTED_VISUAL_HEADINGS,
    EXPECTED_VISUAL_REQUIREMENT,
    VISUAL_CANARY_CODE,
    SyntheticPdfFixture,
    build_synthetic_pdf,
)


PDF_CANARY_TASK_MARKER = "B1_C2_PDF_SKILL"
PDF_CANARY_FACTS_SCHEMA = "reqpilotx-pdf-canary-facts/v1"
PAGE_LEDGER_SCHEMA = "document-page-ledger/v1"
COVERAGE_RECEIPT_SCHEMA = "document-coverage-receipt/v1"
_FIXED_CLOCK = datetime(2026, 8, 21, tzinfo=timezone.utc)
_PROPOSAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_EXPECTED_VISUAL_RELATION = "HARNESS -> SENSOR"


@dataclass(frozen=True)
class PdfCanaryContext:
    allowed_fact_refs: frozenset[str]
    deterministic_tree: Any
    expected_canonical_nodes: tuple[Mapping[str, Any], ...]
    facts_payload: Mapping[str, Any]
    facts_sha256: str
    fixture: SyntheticPdfFixture
    page_image_path: Path
    page_image_sha256: str
    page_ledger: Mapping[str, Any]
    page_ledger_sha256: str
    source_ir: Any
    source_path: Path
    visual_image_source_ref: str


def prepare_pdf_canary(
    *,
    server_private_dir: Path,
    model_workspace: Path,
    include_prompt_injection: bool = False,
) -> PdfCanaryContext:
    """Create and parse one exact fixture without DB, network, or providers."""

    _require_private_directory(server_private_dir, "server_private_dir")
    _require_private_directory(model_workspace, "model_workspace")
    source_path = server_private_dir / "synthetic-sensor-spec.pdf"
    fixture = build_synthetic_pdf(
        source_path,
        include_prompt_injection=include_prompt_injection,
    )
    os.chmod(source_path, 0o600)
    preflight = LocalDocumentPreflight().inspect(source_path)
    if not preflight.processable or preflight.page_count != fixture.page_count:
        raise RuntimeError("synthetic PDF preflight failed")
    if preflight.identity.source_sha256 != fixture.source_sha256:
        raise RuntimeError("synthetic PDF preflight identity drifted")

    source_ir = build_source_ir(
        source_path,
        preflight_report=preflight,
        adapters=(PdfSourceIRAdapter(),),
        clock=lambda: _FIXED_CLOCK,
    )
    if source_ir.source_sha256 != fixture.source_sha256:
        raise RuntimeError("SourceIR identity drifted")
    deterministic_tree = build_structure_tree(source_ir)
    validate_structure_tree_against_source_ir(deterministic_tree, source_ir)
    geometry = inspect_pdf_page_geometry(
        source_path,
        expected_source_sha256=source_ir.source_sha256,
    )
    comparison = build_page_comparison_view(
        source_ir,
        deterministic_tree,
        page_bounds=tuple(page.bounds for page in geometry),
    )
    if comparison.get("schema_version") != PAGE_COMPARISON_SCHEMA_VERSION:
        raise RuntimeError("page comparison schema drifted")
    section_labels = tuple(
        node.explicit_label
        for node in deterministic_tree.nodes
        if node.kind is StructureNodeKind.SECTION
    )
    if section_labels != EXPECTED_NATIVE_SECTION_LABELS:
        raise RuntimeError("synthetic native section structure drifted")

    evidence_dir = model_workspace / "evidence"
    evidence_dir.mkdir(mode=0o700)
    os.chmod(evidence_dir, 0o700)
    rendered = render_pdf_visual_page_input(
        source_path,
        page_number=fixture.visual_page_number,
        scale=2.0,
    )
    if rendered.source_sha256 != source_ir.source_sha256:
        raise RuntimeError("rendered page source identity drifted")
    if tuple(rendered.page_bbox) != tuple(geometry[1].bounds):
        raise RuntimeError("rendered page geometry drifted")
    page_image_path = evidence_dir / "visual-page-2.png"
    page_image_path.write_bytes(rendered.page_image_bytes)
    os.chmod(page_image_path, 0o600)

    facts, allowed_refs = _source_fact_catalog(source_ir)
    facts_payload = {
        "schema_version": PDF_CANARY_FACTS_SCHEMA,
        "fixture_id": fixture.fixture_id,
        "source_sha256": source_ir.source_sha256,
        "source_ir_status": source_ir.status.value,
        "adapter": {
            "id": source_ir.adapter_id,
            "version": source_ir.adapter_version,
        },
        "facts": facts,
    }
    facts_sha256 = canonical_sha256(facts_payload)
    visual_image_nodes = [
        node
        for node in source_ir.nodes
        if node.native_locator.page_number == fixture.visual_page_number
        and node.kind.value == "image"
    ]
    if len(visual_image_nodes) != 1:
        raise RuntimeError("synthetic visual page must contain one image fact")
    visual_image_source_ref = visual_image_nodes[0].source_node_id
    risk_codes = sorted(
        {
            diagnostic.code
            for diagnostic in source_ir.diagnostics
            if diagnostic.native_locator is not None
            and diagnostic.native_locator.page_number == fixture.visual_page_number
        }
    )
    required_risks = {
        "pdf_full_page_image_without_visible_text",
        "pdf_visual_text_provider_not_configured",
    }
    if not required_risks.issubset(risk_codes):
        raise RuntimeError("synthetic visual risk contract drifted")
    page_one_refs = [
        node.source_node_id
        for node in source_ir.nodes
        if node.native_locator.page_number == 1
    ]
    native_char_count = sum(
        len(node.raw_text)
        for node in source_ir.nodes
        if node.native_locator.page_number == 1
    )
    page_ledger = {
        "schema_version": PAGE_LEDGER_SCHEMA,
        "source_sha256": source_ir.source_sha256,
        "page_count": fixture.page_count,
        "pages": [
            {
                "physical_page": 1,
                "canonical_fact_status": "NATIVE_OK",
                "native_char_count": native_char_count,
                "source_refs": page_one_refs,
                "risk_codes": [],
                "page_image_sha256": None,
                "agent_inspection_status": "NOT_REQUIRED",
            },
            {
                "physical_page": 2,
                "canonical_fact_status": "IMAGE_REQUIRED",
                "native_char_count": 0,
                "source_refs": [visual_image_source_ref],
                "risk_codes": risk_codes,
                "page_image_sha256": rendered.page_image_sha256,
                "agent_inspection_status": "NOT_SUPPLIED",
            },
        ],
    }
    page_ledger_sha256 = canonical_sha256(page_ledger)
    return PdfCanaryContext(
        allowed_fact_refs=frozenset(allowed_refs),
        deterministic_tree=deterministic_tree,
        expected_canonical_nodes=_expected_canonical_nodes(deterministic_tree),
        facts_payload=facts_payload,
        facts_sha256=facts_sha256,
        fixture=fixture,
        page_image_path=page_image_path,
        page_image_sha256=rendered.page_image_sha256,
        page_ledger=page_ledger,
        page_ledger_sha256=page_ledger_sha256,
        source_ir=source_ir,
        source_path=source_path,
        visual_image_source_ref=visual_image_source_ref,
    )


def pdf_understanding_task_payload(context: PdfCanaryContext) -> dict[str, Any]:
    """Return model-visible facts without raw PDF path/bytes or visual golden."""

    payload = {
        "task_marker": PDF_CANARY_TASK_MARKER,
        "instruction": (
            "Cover every page. On page 1, emit exactly one NATIVE HEADING observation "
            "for each supplied SECTION candidate and one NATIVE TABLE observation for "
            "the TABLE candidate; copy each candidate's source refs exactly and use null "
            "exact_label. Cite only native fact refs actually used. Inspect the supplied "
            "image for page 2 and report visible headings, requirement, canary code, "
            "latch orientation, directed-diagram endpoints, and marker position exactly "
            "as visible. Every visual observation must cite only the supplied page image "
            "SHA. Populate visual_canary only from the pixels; use concise direction and "
            "endpoint tokens without guessing or negation. "
            "For page 2, copy the ledger source_refs into used_fact_refs and put "
            "the page image digest only in used_page_image_sha256. "
            "Include CANONICAL_VISUAL_CAPTURE_NOT_PERSISTED in page-2 unresolved_codes. "
            "Keep canonical status PARTIAL because visual capture is not persisted."
        ),
        "facts": context.facts_payload,
        "facts_sha256": context.facts_sha256,
        "native_observation_targets": [
            {
                "kind": "HEADING" if node["node_kind"] == "SECTION" else "TABLE",
                "evidence_refs": node["source_refs"],
                "exact_label": None,
            }
            for node in context.expected_canonical_nodes
            if node["node_kind"] in {"SECTION", "TABLE"}
        ],
        "page_ledger": context.page_ledger,
        "page_ledger_sha256": context.page_ledger_sha256,
        "visual_page_image_ref": {
            "physical_page": context.fixture.visual_page_number,
            "page_image_sha256": context.page_image_sha256,
        },
    }
    serialized = canonical_json(payload)
    for golden in _visual_golden_markers():
        if golden.casefold() in serialized.casefold():
            raise RuntimeError("visual golden leaked into model task JSON")
    return payload


def validate_understanding_proposal(
    proposal: Mapping[str, Any],
    context: PdfCanaryContext,
) -> tuple[dict[str, Any], str]:
    _require_exact_keys(
        proposal,
        {
            "agent_claimed_status",
            "facts_sha256",
            "page_count",
            "page_ledger_sha256",
            "pages",
            "schema_version",
            "source_sha256",
            "visual_canary",
        },
        "understanding proposal",
    )
    if (
        proposal["schema_version"] != "document-understanding-proposal/v1"
        or proposal["source_sha256"] != context.fixture.source_sha256
        or proposal["facts_sha256"] != context.facts_sha256
        or proposal["page_ledger_sha256"] != context.page_ledger_sha256
        or proposal["page_count"] != context.fixture.page_count
        or proposal["agent_claimed_status"] != "PARTIAL"
    ):
        raise ValueError("understanding proposal identity or status mismatch")
    pages = proposal["pages"]
    if not isinstance(pages, list) or [page.get("physical_page") for page in pages] != [
        1,
        2,
    ]:
        raise ValueError("understanding proposal page coverage mismatch")
    page_one, page_two = pages
    expected_page_one_refs = set(context.page_ledger["pages"][0]["source_refs"])
    if page_one.get("disposition") != "NATIVE_ACCEPTED":
        raise ValueError("native page disposition mismatch")
    if page_one.get("used_page_image_sha256") is not None:
        raise ValueError("native page image hash must be null")
    used_page_one_refs = page_one.get("used_fact_refs")
    if (
        not isinstance(used_page_one_refs, list)
        or not used_page_one_refs
        or len(used_page_one_refs) != len(set(used_page_one_refs))
        or not set(used_page_one_refs).issubset(expected_page_one_refs)
    ):
        raise ValueError("native page fact refs mismatch")
    if page_one.get("unresolved_codes") != []:
        raise ValueError("native page unresolved codes mismatch")
    _validate_native_observations(
        page_one.get("observations"),
        context=context,
        used_refs=set(used_page_one_refs),
    )
    if page_two.get("disposition") != "VISUAL_OBSERVED":
        raise ValueError("visual page disposition mismatch")
    if page_two.get("used_page_image_sha256") != context.page_image_sha256:
        raise ValueError("visual page image hash mismatch")
    if not isinstance(page_two.get("used_fact_refs"), list) or set(
        page_two["used_fact_refs"]
    ) != {context.visual_image_source_ref}:
        raise ValueError("visual page fact refs mismatch")
    unresolved_codes = page_two.get("unresolved_codes")
    allowed_unresolved_codes = {
        "CANONICAL_VISUAL_CAPTURE_NOT_PERSISTED",
        *context.page_ledger["pages"][1]["risk_codes"],
    }
    if (
        not isinstance(unresolved_codes, list)
        or "CANONICAL_VISUAL_CAPTURE_NOT_PERSISTED" not in unresolved_codes
        or len(unresolved_codes) != len(set(unresolved_codes))
        or not set(unresolved_codes).issubset(allowed_unresolved_codes)
    ):
        raise ValueError("visual page unresolved codes mismatch")
    _validate_visual_observations(
        page_two.get("observations"),
        page_image_sha256=context.page_image_sha256,
    )
    visual_canary = proposal["visual_canary"]
    if not isinstance(visual_canary, Mapping):
        raise ValueError("visual canary result is invalid")
    if visual_canary.get("page_number") != 2:
        raise ValueError("visual canary page mismatch")
    code_match = re.search(r"VIS-[0-9]{4}", str(visual_canary.get("code", "")).upper())
    if code_match is None or code_match.group(0) != VISUAL_CANARY_CODE:
        raise ValueError("visual canary code mismatch")
    latch_tokens = set(
        _normalized_token(visual_canary.get("latch_orientation")).split("_")
    )
    if not {"UP", "UPWARD"}.intersection(latch_tokens) or {
        "NOT",
        "DOWN",
        "DOWNWARD",
    }.intersection(latch_tokens):
        raise ValueError("visual latch orientation mismatch")
    if _normalized_token(visual_canary.get("flow_from")) != "HARNESS":
        raise ValueError("visual flow origin mismatch")
    if _normalized_token(visual_canary.get("flow_to")) != "SENSOR":
        raise ValueError("visual flow target mismatch")
    pin_tokens = set(_normalized_token(visual_canary.get("pin_a_position")).split("_"))
    if not {"ABOVE", "TOP", "UPPER", "UP"}.intersection(pin_tokens) or {
        "NOT",
        "BELOW",
        "UNDER",
        "LOWER",
        "DOWN",
    }.intersection(pin_tokens):
        raise ValueError("visual PIN A position mismatch")
    proposal_dict = dict(proposal)
    proposal_sha256 = canonical_sha256(proposal_dict)
    coverage_receipt = {
        "schema_version": COVERAGE_RECEIPT_SCHEMA,
        "source_sha256": context.fixture.source_sha256,
        "facts_sha256": context.facts_sha256,
        "page_ledger_sha256": context.page_ledger_sha256,
        "proposal_sha256": proposal_sha256,
        "page_count": 2,
        "pages": [
            {
                **context.page_ledger["pages"][0],
                "agent_inspection_status": "NOT_REQUIRED",
            },
            {
                **context.page_ledger["pages"][1],
                "agent_inspection_status": "INSPECTED",
            },
        ],
        "uncovered_pages": [],
        "unresolved_pages": [2],
        "status": "PARTIAL",
        "apply_eligible": False,
    }
    return coverage_receipt, proposal_sha256


def build_synthetic_understanding_proposal(
    context: PdfCanaryContext,
) -> dict[str, Any]:
    """Build the already-validated B2 input used to isolate the B3 structure gate."""

    native_observations = [
        {
            "kind": "HEADING" if node["node_kind"] == "SECTION" else "TABLE",
            "evidence_channel": "NATIVE",
            "evidence_refs": node["source_refs"],
            "exact_label": None,
        }
        for node in context.expected_canonical_nodes
        if node["node_kind"] in {"SECTION", "TABLE"}
    ]
    return {
        "schema_version": "document-understanding-proposal/v1",
        "source_sha256": context.fixture.source_sha256,
        "facts_sha256": context.facts_sha256,
        "page_ledger_sha256": context.page_ledger_sha256,
        "page_count": context.fixture.page_count,
        "pages": [
            {
                "physical_page": 1,
                "disposition": "NATIVE_ACCEPTED",
                "used_fact_refs": context.page_ledger["pages"][0]["source_refs"],
                "used_page_image_sha256": None,
                "observations": native_observations,
                "unresolved_codes": [],
            },
            {
                "physical_page": 2,
                "disposition": "VISUAL_OBSERVED",
                "used_fact_refs": [context.visual_image_source_ref],
                "used_page_image_sha256": context.page_image_sha256,
                "observations": [
                    {
                        "kind": "HEADING",
                        "evidence_channel": "VISUAL",
                        "evidence_refs": [context.page_image_sha256],
                        "exact_label": EXPECTED_VISUAL_HEADINGS[0],
                    },
                    {
                        "kind": "HEADING",
                        "evidence_channel": "VISUAL",
                        "evidence_refs": [context.page_image_sha256],
                        "exact_label": EXPECTED_VISUAL_HEADINGS[1],
                    },
                    {
                        "kind": "REQUIREMENT_TEXT",
                        "evidence_channel": "VISUAL",
                        "evidence_refs": [context.page_image_sha256],
                        "exact_label": EXPECTED_VISUAL_REQUIREMENT,
                    },
                    {
                        "kind": "VISUAL_LABEL",
                        "evidence_channel": "VISUAL",
                        "evidence_refs": [context.page_image_sha256],
                        "exact_label": f"CANARY CODE: {VISUAL_CANARY_CODE}",
                    },
                    {
                        "kind": "DIAGRAM_RELATION",
                        "evidence_channel": "VISUAL",
                        "evidence_refs": [context.page_image_sha256],
                        "exact_label": "HARNESS -> SENSOR",
                    },
                ],
                "unresolved_codes": ["CANONICAL_VISUAL_CAPTURE_NOT_PERSISTED"],
            },
        ],
        "visual_canary": {
            "page_number": 2,
            "code": VISUAL_CANARY_CODE,
            "latch_orientation": "UPWARD",
            "flow_from": "HARNESS",
            "flow_to": "SENSOR",
            "pin_a_position": "ABOVE_SENSOR_BODY",
        },
        "agent_claimed_status": "PARTIAL",
    }


def structure_task_payload(
    *,
    context: PdfCanaryContext,
    understanding_proposal: Mapping[str, Any],
    understanding_proposal_sha256: str,
    coverage_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_counts: dict[str, int] = {}
    for candidate in context.expected_canonical_nodes:
        kind = str(candidate["node_kind"])
        candidate_counts[kind] = candidate_counts.get(kind, 0) + 1
    structural_candidates = [
        {
            "candidate_ref": f"candidate-{index + 1:03d}",
            "node_kind": candidate["node_kind"],
            "source_order": candidate["source_order"],
            "source_refs": candidate["source_refs"],
            "boundary_basis": candidate["boundary_basis"],
            "explicit_label": candidate["explicit_label"],
        }
        for index, candidate in enumerate(context.expected_canonical_nodes)
    ]
    return {
        "task_marker": PDF_CANARY_TASK_MARKER,
        "instruction": (
            "Generate canonical nodes only from native facts. Preserve exact source "
            "refs/order and parentage. Keep page-2 headings advisory and unresolved; "
            "the PARTIAL coverage receipt is never Apply-eligible. Emit every table "
            "row as a TABLE_ROW. Emit each structural_candidate exactly once and "
            "copy its kind, order, refs, basis, and label exactly; derive only the "
            "proposal-local parent graph. Include unresolved code "
            "CANONICAL_PAGE_2_VISUAL_CAPTURE_REQUIRED with the page-2 image source ref."
        ),
        "source_sha256": context.fixture.source_sha256,
        "facts_sha256": context.facts_sha256,
        "fact_catalog": context.facts_payload["facts"],
        "expected_candidate_counts": candidate_counts,
        "structural_candidate_catalog": structural_candidates,
        "understanding_proposal": understanding_proposal,
        "understanding_proposal_sha256": understanding_proposal_sha256,
        "coverage_receipt": coverage_receipt,
        "coverage_receipt_sha256": canonical_sha256(coverage_receipt),
    }


def validate_structure_proposal(
    proposal: Mapping[str, Any],
    *,
    context: PdfCanaryContext,
    understanding_proposal_sha256: str,
    coverage_receipt: Mapping[str, Any],
) -> str:
    expected_coverage_sha256 = canonical_sha256(coverage_receipt)
    _require_exact_keys(
        proposal,
        {
            "agent_claimed_status",
            "canonical_nodes",
            "coverage_receipt_sha256",
            "facts_sha256",
            "schema_version",
            "source_sha256",
            "understanding_proposal_sha256",
            "unresolved",
            "visual_advisory_nodes",
        },
        "structure proposal",
    )
    if (
        proposal["schema_version"] != "structure-proposal/v1"
        or proposal["source_sha256"] != context.fixture.source_sha256
        or proposal["facts_sha256"] != context.facts_sha256
        or proposal["understanding_proposal_sha256"] != understanding_proposal_sha256
        or proposal["coverage_receipt_sha256"] != expected_coverage_sha256
        or proposal["agent_claimed_status"] != "PARTIAL"
    ):
        raise ValueError("structure proposal identity or status mismatch")
    nodes = proposal["canonical_nodes"]
    if not isinstance(nodes, list) or len(nodes) != len(
        context.expected_canonical_nodes
    ):
        raise ValueError("canonical structure node count mismatch")
    proposal_ids: set[str] = set()
    actual_by_key: dict[tuple[str, tuple[str, ...]], Mapping[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, Mapping):
            raise ValueError("canonical structure node is invalid")
        _require_exact_keys(
            node,
            {
                "proposal_node_id",
                "node_kind",
                "parent_proposal_node_id",
                "source_order",
                "source_refs",
                "boundary_basis",
                "explicit_label",
            },
            "canonical structure node",
        )
        proposal_id = str(node.get("proposal_node_id") or "")
        if (
            not _PROPOSAL_ID_PATTERN.fullmatch(proposal_id)
            or proposal_id in proposal_ids
        ):
            raise ValueError("proposal node ID is invalid or duplicated")
        proposal_ids.add(proposal_id)
        refs = node.get("source_refs")
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or ref not in context.allowed_fact_refs
            for ref in refs
        ):
            raise ValueError("proposal node contains an unknown source ref")
        key = (str(node.get("node_kind")), tuple(refs))
        if key in actual_by_key:
            raise ValueError("proposal node source identity is duplicated")
        actual_by_key[key] = node
    expected_by_key = {
        (str(node["node_kind"]), tuple(node["source_refs"])): node
        for node in context.expected_canonical_nodes
    }
    if set(actual_by_key) != set(expected_by_key):
        missing = set(expected_by_key) - set(actual_by_key)
        extra = set(actual_by_key) - set(expected_by_key)
        missing_kinds = sorted(key[0] for key in missing)
        extra_kinds = sorted(key[0] for key in extra)
        raise ValueError(
            "proposal node catalog differs "
            f"missing {','.join(missing_kinds) or 'none'} "
            f"extra {','.join(extra_kinds) or 'none'}"
        )
    expected_key_by_node_id = {
        str(node["expected_node_id"]): key for key, node in expected_by_key.items()
    }
    actual_key_by_proposal_id = {
        str(node["proposal_node_id"]): key for key, node in actual_by_key.items()
    }
    for node in nodes:
        proposal_id = str(node["proposal_node_id"])
        parent_id = node.get("parent_proposal_node_id")
        if parent_id is None:
            continue
        if not isinstance(parent_id, str) or parent_id not in actual_key_by_proposal_id:
            raise ValueError("proposal parent ID is dangling")
        if parent_id == proposal_id:
            raise ValueError("proposal node cannot parent itself")
        parent = actual_by_key[actual_key_by_proposal_id[parent_id]]
        if not isinstance(parent.get("source_order"), int) or parent[
            "source_order"
        ] >= node.get("source_order", -1):
            raise ValueError("proposal parent must precede child")
    for key, expected in expected_by_key.items():
        actual = actual_by_key[key]
        if (
            actual.get("source_order") != expected["source_order"]
            or actual.get("boundary_basis") != expected["boundary_basis"]
            or actual.get("explicit_label") != expected["explicit_label"]
        ):
            raise ValueError("proposal node boundary or order mismatch")
        actual_parent_id = actual.get("parent_proposal_node_id")
        actual_parent_key = (
            None
            if actual_parent_id is None
            else actual_key_by_proposal_id[str(actual_parent_id)]
        )
        expected_parent_key = (
            None
            if expected["expected_parent_node_id"] is None
            else expected_key_by_node_id[str(expected["expected_parent_node_id"])]
        )
        if actual_parent_key != expected_parent_key:
            raise ValueError("proposal parent graph mismatch")
    advisories = proposal["visual_advisory_nodes"]
    if not isinstance(advisories, list) or len(advisories) != 2:
        raise ValueError("visual advisory node count mismatch")
    advisory_by_label = {item.get("explicit_label"): item for item in advisories}
    expected_advisories = {
        "3": (None, EXPECTED_VISUAL_HEADINGS[0]),
        "3.1": ("3", EXPECTED_VISUAL_HEADINGS[1]),
    }
    for label, (parent_label, heading) in expected_advisories.items():
        item = advisory_by_label.get(label)
        if (
            not isinstance(item, Mapping)
            or item.get("parent_explicit_label") != parent_label
            or item.get("heading_text") != heading
            or item.get("page_number") != 2
            or item.get("evidence_channel") != "VISUAL"
        ):
            raise ValueError("visual advisory node mismatch")
        _require_exact_keys(
            item,
            {
                "explicit_label",
                "parent_explicit_label",
                "heading_text",
                "page_number",
                "evidence_channel",
            },
            "visual advisory node",
        )
    unresolved = proposal["unresolved"]
    required_unresolved = {
        "code": "CANONICAL_PAGE_2_VISUAL_CAPTURE_REQUIRED",
        "source_refs": [context.visual_image_source_ref],
    }
    allowed_unresolved_codes = {
        "CANONICAL_PAGE_2_VISUAL_CAPTURE_REQUIRED",
        "CANONICAL_VISUAL_CAPTURE_NOT_PERSISTED",
        *context.page_ledger["pages"][1]["risk_codes"],
    }
    if (
        not isinstance(unresolved, list)
        or required_unresolved not in unresolved
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"code", "source_refs"}
            or item.get("code") not in allowed_unresolved_codes
            or not isinstance(item.get("source_refs"), list)
            or any(ref not in context.allowed_fact_refs for ref in item["source_refs"])
            for item in unresolved
        )
    ):
        raise ValueError("structure unresolved coverage mismatch")
    return canonical_sha256(dict(proposal))


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _source_fact_catalog(source_ir: Any) -> tuple[list[dict[str, Any]], set[str]]:
    facts: list[dict[str, Any]] = []
    allowed_refs: set[str] = set()
    for node in sorted(source_ir.nodes, key=lambda item: item.source_order):
        allowed_refs.add(node.source_node_id)
        table_payload = None
        if node.table_payload is not None:
            cells = []
            for cell in node.table_payload.cells:
                allowed_refs.add(cell.cell_id)
                cells.append(
                    {
                        "fact_ref": cell.cell_id,
                        "row": cell.row,
                        "column": cell.column,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                        "text": cell.raw_text,
                    }
                )
            table_payload = {
                "table_id": node.table_payload.table_id,
                "row_count": node.table_payload.row_count,
                "column_count": node.table_payload.column_count,
                "cells": cells,
            }
        facts.append(
            {
                "fact_ref": node.source_node_id,
                "kind": node.kind.value,
                "source_order": node.source_order,
                "physical_page": node.native_locator.page_number,
                "text": node.raw_text,
                "table": table_payload,
            }
        )
    return facts, allowed_refs


def _expected_canonical_nodes(tree: Any) -> tuple[Mapping[str, Any], ...]:
    accepted_kinds = {
        StructureNodeKind.SECTION,
        StructureNodeKind.TABLE,
        StructureNodeKind.TABLE_ROW,
    }
    accepted_nodes = [node for node in tree.nodes if node.kind in accepted_kinds]
    node_by_id = {node.structure_node_id: node for node in tree.nodes}
    accepted_ids = {node.structure_node_id for node in accepted_nodes}
    result: list[Mapping[str, Any]] = []
    for node in accepted_nodes:
        parent_id = node.parent_structure_node_id
        while parent_id is not None and parent_id not in accepted_ids:
            parent_id = node_by_id[parent_id].parent_structure_node_id
        result.append(
            {
                "expected_node_id": node.structure_node_id,
                "expected_parent_node_id": parent_id,
                "node_kind": node.kind.value.upper(),
                "source_order": node.source_order,
                "source_refs": [ref.source_id for ref in node.source_refs],
                "boundary_basis": node.boundary_basis.value.upper(),
                "explicit_label": node.explicit_label,
            }
        )
    return tuple(result)


def _normalized_token(value: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").upper()).strip("_")


def _visual_golden_markers() -> tuple[str, ...]:
    return (
        *EXPECTED_VISUAL_HEADINGS,
        EXPECTED_VISUAL_REQUIREMENT,
        f"CANARY CODE: {VISUAL_CANARY_CODE}",
        VISUAL_CANARY_CODE,
        _EXPECTED_VISUAL_RELATION,
        "UPWARD",
        "ABOVE_SENSOR_BODY",
    )


def _validate_native_observations(
    observations: object,
    *,
    context: PdfCanaryContext,
    used_refs: set[str],
) -> None:
    if not isinstance(observations, list):
        raise ValueError("native observations are missing")
    expected = {
        (
            "HEADING" if node["node_kind"] == "SECTION" else "TABLE",
            tuple(node["source_refs"]),
            None,
        )
        for node in context.expected_canonical_nodes
        if node["node_kind"] in {"SECTION", "TABLE"}
    }
    observed: set[tuple[str, tuple[str, ...], None]] = set()
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise ValueError("native observation is invalid")
        refs = observation.get("evidence_refs")
        if (
            observation.get("evidence_channel") != "NATIVE"
            or observation.get("kind") not in {"HEADING", "TABLE"}
            or observation.get("exact_label") is not None
            or not isinstance(refs, list)
            or not refs
            or len(refs) != len(set(refs))
            or not set(refs).issubset(used_refs)
        ):
            raise ValueError("native observation evidence binding mismatch")
        signature = (str(observation["kind"]), tuple(refs), None)
        if signature in observed:
            raise ValueError("native observation is duplicated")
        observed.add(signature)
    if observed != expected:
        raise ValueError("native structural observations are incomplete")


def _validate_visual_observations(
    observations: object,
    *,
    page_image_sha256: str,
) -> None:
    if not isinstance(observations, list):
        raise ValueError("visual observations are missing")
    required = {("HEADING", heading) for heading in EXPECTED_VISUAL_HEADINGS}
    required.update(
        {
            ("REQUIREMENT_TEXT", EXPECTED_VISUAL_REQUIREMENT),
            ("VISUAL_LABEL", f"CANARY CODE: {VISUAL_CANARY_CODE}"),
        }
    )
    observed: set[tuple[str, str]] = set()
    directed_relation_observed = False
    for observation in observations:
        if not isinstance(observation, Mapping):
            raise ValueError("visual observation is invalid")
        refs = observation.get("evidence_refs")
        kind = observation.get("kind")
        label = observation.get("exact_label")
        if (
            observation.get("evidence_channel") != "VISUAL"
            or refs != [page_image_sha256]
            or not isinstance(kind, str)
            or not isinstance(label, str)
            or not label
        ):
            raise ValueError("visual observation evidence binding mismatch")
        signature = (kind, label)
        if signature in observed:
            raise ValueError("visual observation is duplicated")
        observed.add(signature)
        if kind == "DIAGRAM_RELATION":
            relation_tokens = _normalized_token(label).split("_")
            if (
                "HARNESS" in relation_tokens
                and "SENSOR" in relation_tokens
                and relation_tokens.index("HARNESS") < relation_tokens.index("SENSOR")
            ):
                directed_relation_observed = True
    missing = required - observed
    if missing:
        missing_kinds = sorted({kind for kind, _label in missing})
        raise ValueError(
            "visual observations missing required kinds " + ",".join(missing_kinds)
        )
    if not directed_relation_observed:
        raise ValueError("visual observations missing directed diagram relation")


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError(f"{label} fields are invalid")


def _require_private_directory(path: Path, label: str) -> None:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_dir()
        or path.stat().st_uid != os.getuid()
        or path.stat().st_mode & 0o077
    ):
        raise ValueError(f"{label} must be a private owned directory")


__all__ = [
    "COVERAGE_RECEIPT_SCHEMA",
    "PDF_CANARY_TASK_MARKER",
    "PdfCanaryContext",
    "canonical_json",
    "canonical_sha256",
    "build_synthetic_understanding_proposal",
    "pdf_understanding_task_payload",
    "prepare_pdf_canary",
    "structure_task_payload",
    "validate_structure_proposal",
    "validate_understanding_proposal",
]
