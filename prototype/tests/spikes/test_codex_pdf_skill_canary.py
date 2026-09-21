from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from spikes.codex_app_server.pdf_tool_host import (
    PDF_CANARY_TASK_MARKER,
    canonical_json,
    canonical_sha256,
    pdf_understanding_task_payload,
    prepare_pdf_canary,
    validate_structure_proposal,
    validate_understanding_proposal,
)
from spikes.codex_app_server.synthetic_pdf_fixture import (
    EXPECTED_VISUAL_HEADINGS,
    EXPECTED_VISUAL_REQUIREMENT,
    VISUAL_CANARY_CODE,
)


class CodexPdfSkillCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="reqpilotx-pdf-skill-")
        root = Path(self._tmp.name).resolve()
        os.chmod(root, 0o700)
        self.server_dir = root / "server"
        self.model_dir = root / "model"
        for directory in (self.server_dir, self.model_dir):
            directory.mkdir(mode=0o700)
        self.context = prepare_pdf_canary(
            server_private_dir=self.server_dir,
            model_workspace=self.model_dir,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fixture_is_native_plus_visual_and_task_hides_visual_golden(self) -> None:
        context = self.context
        self.assertEqual(context.fixture.page_count, 2)
        self.assertEqual(context.source_ir.status.value, "PARTIAL")
        self.assertEqual(len(context.facts_payload["facts"]), 16)
        self.assertEqual(len(context.expected_canonical_nodes), 9)
        self.assertEqual(
            {diagnostic.code for diagnostic in context.source_ir.diagnostics},
            {
                "pdf_full_page_image_without_visible_text",
                "pdf_visual_text_provider_not_configured",
            },
        )
        page_two_text = "".join(
            node.raw_text
            for node in context.source_ir.nodes
            if node.native_locator.page_number == 2
        )
        self.assertEqual(page_two_text, "")
        tables = [
            node.table_payload for node in context.source_ir.nodes if node.table_payload
        ]
        self.assertEqual(len(tables), 1)
        self.assertEqual((tables[0].row_count, tables[0].column_count), (3, 3))
        self.assertEqual(len(tables[0].cells), 9)
        self.assertTrue(context.page_image_path.read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(
            context.page_ledger["pages"][1]["canonical_fact_status"],
            "IMAGE_REQUIRED",
        )

        task = pdf_understanding_task_payload(context)
        serialized = canonical_json(task)
        self.assertIn(PDF_CANARY_TASK_MARKER, serialized)
        for golden in (
            *EXPECTED_VISUAL_HEADINGS,
            EXPECTED_VISUAL_REQUIREMENT,
            f"CANARY CODE: {VISUAL_CANARY_CODE}",
            VISUAL_CANARY_CODE,
            "HARNESS -> SENSOR",
            "UPWARD",
            "ABOVE_SENSOR_BODY",
        ):
            self.assertNotIn(golden.casefold(), serialized.casefold())
        self.assertNotIn(str(context.source_path), serialized)

    def test_understanding_validator_keeps_visual_page_canonically_partial(
        self,
    ) -> None:
        proposal = _valid_understanding_proposal(self.context)
        coverage, proposal_sha256 = validate_understanding_proposal(
            proposal,
            self.context,
        )

        self.assertEqual(proposal_sha256, canonical_sha256(proposal))
        self.assertEqual(coverage["status"], "PARTIAL")
        self.assertFalse(coverage["apply_eligible"])
        self.assertEqual(coverage["unresolved_pages"], [2])
        self.assertEqual(
            coverage["pages"][1]["agent_inspection_status"],
            "INSPECTED",
        )
        self.assertEqual(
            coverage["pages"][1]["canonical_fact_status"],
            "IMAGE_REQUIRED",
        )

        forged = copy.deepcopy(proposal)
        forged["visual_canary"]["code"] = "VIS-0000"
        with self.assertRaisesRegex(ValueError, "visual canary code mismatch"):
            validate_understanding_proposal(forged, self.context)
        stale = copy.deepcopy(proposal)
        stale["facts_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "identity or status mismatch"):
            validate_understanding_proposal(stale, self.context)
        reordered = copy.deepcopy(proposal)
        reordered["pages"].reverse()
        with self.assertRaisesRegex(ValueError, "page coverage mismatch"):
            validate_understanding_proposal(reordered, self.context)

        unbound_visual = copy.deepcopy(proposal)
        unbound_visual["pages"][1]["observations"][0]["evidence_refs"] = []
        with self.assertRaisesRegex(ValueError, "visual observation evidence binding"):
            validate_understanding_proposal(unbound_visual, self.context)
        incomplete_native = copy.deepcopy(proposal)
        incomplete_native["pages"][0]["observations"].pop()
        with self.assertRaisesRegex(ValueError, "native structural observations"):
            validate_understanding_proposal(incomplete_native, self.context)
        opposite_latch = copy.deepcopy(proposal)
        opposite_latch["visual_canary"]["latch_orientation"] = "NOT UP"
        with self.assertRaisesRegex(ValueError, "latch orientation mismatch"):
            validate_understanding_proposal(opposite_latch, self.context)
        opposite_pin = copy.deepcopy(proposal)
        opposite_pin["visual_canary"]["pin_a_position"] = "NOT ABOVE SENSOR BODY"
        with self.assertRaisesRegex(ValueError, "PIN A position mismatch"):
            validate_understanding_proposal(opposite_pin, self.context)

    def test_structure_validator_accepts_native_graph_and_rejects_visual_apply(
        self,
    ) -> None:
        understanding = _valid_understanding_proposal(self.context)
        coverage, understanding_sha256 = validate_understanding_proposal(
            understanding,
            self.context,
        )
        proposal = _valid_structure_proposal(
            self.context,
            understanding_sha256=understanding_sha256,
            coverage=coverage,
        )

        proposal_sha256 = validate_structure_proposal(
            proposal,
            context=self.context,
            understanding_proposal_sha256=understanding_sha256,
            coverage_receipt=coverage,
        )
        self.assertEqual(proposal_sha256, canonical_sha256(proposal))

        forged = copy.deepcopy(proposal)
        forged["canonical_nodes"][0]["source_refs"] = [
            self.context.visual_image_source_ref
        ]
        with self.assertRaisesRegex(ValueError, "catalog differs"):
            validate_structure_proposal(
                forged,
                context=self.context,
                understanding_proposal_sha256=understanding_sha256,
                coverage_receipt=coverage,
            )
        unknown = copy.deepcopy(proposal)
        unknown["canonical_nodes"][0]["source_refs"] = ["node-" + ("0" * 32)]
        with self.assertRaisesRegex(ValueError, "unknown source ref"):
            validate_structure_proposal(
                unknown,
                context=self.context,
                understanding_proposal_sha256=understanding_sha256,
                coverage_receipt=coverage,
            )

        dangling = copy.deepcopy(proposal)
        dangling["canonical_nodes"][0]["parent_proposal_node_id"] = "missing-parent"
        with self.assertRaisesRegex(ValueError, "parent ID is dangling"):
            validate_structure_proposal(
                dangling,
                context=self.context,
                understanding_proposal_sha256=understanding_sha256,
                coverage_receipt=coverage,
            )
        self_parent = copy.deepcopy(proposal)
        self_parent["canonical_nodes"][1]["parent_proposal_node_id"] = self_parent[
            "canonical_nodes"
        ][1]["proposal_node_id"]
        with self.assertRaisesRegex(ValueError, "cannot parent itself"):
            validate_structure_proposal(
                self_parent,
                context=self.context,
                understanding_proposal_sha256=understanding_sha256,
                coverage_receipt=coverage,
            )
        backward_parent = copy.deepcopy(proposal)
        backward_parent["canonical_nodes"][0]["parent_proposal_node_id"] = (
            backward_parent["canonical_nodes"][1]["proposal_node_id"]
        )
        with self.assertRaisesRegex(ValueError, "parent must precede child"):
            validate_structure_proposal(
                backward_parent,
                context=self.context,
                understanding_proposal_sha256=understanding_sha256,
                coverage_receipt=coverage,
            )

    def test_machine_schemas_are_strict_json_objects(self) -> None:
        schema_dir = (
            Path(__file__).resolve().parents[3]
            / "experiments/codex-app-server/schemas"
        )
        for name in (
            "document_agent_facts_v1.schema.json",
            "document_understanding_proposal_v1.schema.json",
            "shadow_structure_proposal_v1.schema.json",
            "structure_proposal_v1.schema.json",
        ):
            payload = json.loads((schema_dir / name).read_text(encoding="utf-8"))
            self.assertEqual(payload["type"], "object")
            self.assertFalse(payload["additionalProperties"])
        understanding = json.loads(
            (schema_dir / "document_understanding_proposal_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        evidence_refs = understanding["properties"]["pages"]["items"]["properties"][
            "observations"
        ]["items"]["properties"]["evidence_refs"]
        self.assertEqual(evidence_refs["minItems"], 1)


def _valid_understanding_proposal(context):
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
        "page_count": 2,
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


def _valid_structure_proposal(context, *, understanding_sha256, coverage):
    proposal_id_by_expected_id = {
        node["expected_node_id"]: f"proposal-{index + 1}"
        for index, node in enumerate(context.expected_canonical_nodes)
    }
    canonical_nodes = []
    for index, expected in enumerate(context.expected_canonical_nodes):
        parent_id = expected["expected_parent_node_id"]
        canonical_nodes.append(
            {
                "proposal_node_id": f"proposal-{index + 1}",
                "node_kind": expected["node_kind"],
                "parent_proposal_node_id": (
                    None if parent_id is None else proposal_id_by_expected_id[parent_id]
                ),
                "source_order": expected["source_order"],
                "source_refs": expected["source_refs"],
                "boundary_basis": expected["boundary_basis"],
                "explicit_label": expected["explicit_label"],
            }
        )
    return {
        "schema_version": "structure-proposal/v1",
        "source_sha256": context.fixture.source_sha256,
        "facts_sha256": context.facts_sha256,
        "understanding_proposal_sha256": understanding_sha256,
        "coverage_receipt_sha256": canonical_sha256(coverage),
        "canonical_nodes": canonical_nodes,
        "visual_advisory_nodes": [
            {
                "explicit_label": "3",
                "parent_explicit_label": None,
                "heading_text": EXPECTED_VISUAL_HEADINGS[0],
                "page_number": 2,
                "evidence_channel": "VISUAL",
            },
            {
                "explicit_label": "3.1",
                "parent_explicit_label": "3",
                "heading_text": EXPECTED_VISUAL_HEADINGS[1],
                "page_number": 2,
                "evidence_channel": "VISUAL",
            },
        ],
        "unresolved": [
            {
                "code": "CANONICAL_PAGE_2_VISUAL_CAPTURE_REQUIRED",
                "source_refs": [context.visual_image_source_ref],
            }
        ],
        "agent_claimed_status": "PARTIAL",
    }


if __name__ == "__main__":
    unittest.main()
