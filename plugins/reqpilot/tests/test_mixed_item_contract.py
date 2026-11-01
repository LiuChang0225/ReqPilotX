"""Public task inputs and revision templates preserve a mixed BOM boundary."""
from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import load_workbook

from prototype.tests.test_mixed_source_item import fixture, compile_fixture
from reqpilotx.application.agent_workflow import source_requirement_baseline as core
from task_contracts import bind_item_decisions
from scope_tools import scope_template
from portable_fixtures import item_decisions, make_fixture
from task_service import TaskService


REPO = Path(__file__).resolve().parents[3]
ENTRY = REPO / "plugins/reqpilot/scripts/reqpilot.py"


class MixedItemPublicContractTests(unittest.TestCase):
    def test_public_task_process_persists_one_mixed_bom_and_same_version_excel(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, mapping = make_fixture(root / "fixture", mixed_bom=True)
            service = TaskService(root, ENTRY, REPO)
            state = service.invoke("open", {"mode": "create", "source": str(source),
                "goal": "Synthetic source: keep the BOM sentence and rows as one obligation.",
                "creation_key": "mixed-bom-public"})
            task_id = state["task_id"]

            def write(action, **extra):
                current = service.invoke("inspect", {"task_id": task_id})
                result = service.invoke(action, {"task_id": task_id,
                    "expected_revision": current["revision"], **extra})
                self.assertEqual(result.get("operation_state"), "COMMITTED", result)
                return result

            write("prepare", mode="capture")
            edit = {key: value for key, value in mapping.items() if key != "source_sha256"}
            edit["schema_version"] = "reqpilot-map-edit/v1"
            mapped = write("map", decisions=edit)
            map_dir = Path(mapped["selected"]["MAP"]["output_dir"])
            decisions = item_decisions(map_dir)
            for key in ("source_sha256", "document_map_artifact_sha256"):
                decisions.pop(key)
            text = next(row for row in decisions["text_decisions"] if row["scope_id"] == "text:s6")
            text["items"] = []
            request = next(row for row in json.loads((map_dir / "scope-requests.json").read_text())["text_requests"]
                           if row["scope_id"] == "text:s6")
            content_index, content = next((index, item) for index, item in enumerate(request["source_content"])
                                          if not item["is_title"])
            lead = content["text"]
            selector = {"content_ref": content_index, "quote": lead}
            text["table_owned_spans"] = [{"logical_table_scope_id": "technical-test",
                "record_key": "complete-bom", "text_span": selector}]
            table = decisions["table_decisions"][0]
            fragment = table["records"][0]["row_groups"][0]["fragment_id"]
            table["records"] = [{"record_key": "complete-bom", "label": "Product BOM",
                "source_form": "TABLE_BRANCH", "row_groups": [{"fragment_id": fragment,
                    "row_start": 1, "row_end": 3}], "owning_text_spans": [selector],
                "requirementness": "REQUIREMENT", "requirement_kinds": ["CONSTRAINT"],
                "confidence": 0.95,
                "decision_summary": "The lead sentence governs one complete component list.",
                "reason_codes": ["whole_bom"]}]
            items = write("items", decisions=decisions)
            baseline = json.loads(Path(items["selected"]["ITEMS"]["path"]).read_text())
            mixed = next(record for record in baseline["records"] if record["node_id"] == "s6"
                         and record.get("table_fragments"))
            self.assertEqual(len([record for record in baseline["records"] if record["node_id"] == "s6"]), 1)
            self.assertEqual(mixed["text_bindings"][0]["text"], lead)
            self.assertEqual(mixed["requirement_display_id"], "REQ-0006")
            write("review", analysis_policy="skip_no_internal_evidence")
            delivered = write("deliver")
            matrix = Path(delivered["result"]["output_dir"]) / "matrix.xlsx"
            workbook = load_workbook(matrix, read_only=True, data_only=True)
            try:
                values = "\n".join(str(cell.value) for sheet in workbook for row in sheet
                                    for cell in row if cell.value is not None)
                self.assertIn(lead, values)
                self.assertIn("Housing", values)
                self.assertEqual(workbook["_ReqPilotXMeta"]["B3"].value,
                                 baseline["artifact_sha256"])
            finally:
                workbook.close()
            detail = service.invoke("read", {"task_id": task_id, "view": "item",
                "source_item_id": mixed["source_display_id"], "part": "scope"})
            self.assertTrue(detail["complete"], detail)
            table_scope = detail["data"]
            self.assertEqual(table_scope["coupled_scope_ids"], ["text:s6"])
            table_candidate = table_scope["candidate_template"]
            text_candidate = table_scope["coupled_scope_templates"][0]["candidate_template"]
            table_candidate["records"][0]["decision_summary"] = (
                "The sentence and both component rows form one source obligation.")
            revised = write("revise", command_id="mixed-bom-revision",
                self_review_finding={"text": "Clarify that the lead sentence owns both BOM rows.",
                    "source_item_ids": [mixed["source_display_id"]],
                    "rationale": "The visible lead governs one complete component list."},
                changes={"scope_revision": {"affected_scope_ids": ["technical-test", "text:s6"],
                    "table_decisions": [table_candidate], "text_decisions": [text_candidate]}})
            after = json.loads(Path(revised["selected"]["ITEMS"]["path"]).read_text())
            final = next(record for record in after["records"] if record["source_record_id"] == mixed["source_record_id"])
            self.assertEqual(final["source_display_id"], mixed["source_display_id"])
            self.assertEqual(final["text_bindings"], mixed["text_bindings"])
            self.assertEqual(final["decision_summary"], table_candidate["records"][0]["decision_summary"])
            self.assertTrue(service.invoke("check", {"task_id": task_id})["current_delivery_registered"])

    def test_initial_task_decisions_resolve_explicit_owned_text(self):
        document_map, preview, table_request, table_candidate, text_request, _ = fixture()
        lead = text_request.source_content[0].text
        choices = {
            "text_decisions": [{"scope_id": text_request.scope_id,
                "table_owned_spans": [{"logical_table_scope_id": "lt-bom",
                    "record_key": "complete-bom",
                    "text_span": {"content_ref": 0, "quote": lead}}]}],
            "table_decisions": [{"logical_table_scope_id": "lt-bom",
                "records": [{**table_candidate.records[0].model_dump(mode="json"),
                    "owning_text_spans": [{"content_ref": 0, "quote": lead}]}],
                "context_row_groups": [item.model_dump(mode="json")
                    for item in table_candidate.context_row_groups]}],
        }
        bound = bind_item_decisions(choices, document_map.model_dump(mode="json"), {
            "text_requests": [text_request.model_dump(mode="json")],
            "table_requests": [table_request.model_dump(mode="json")],
        })
        self.assertEqual(bound["text_decisions"][0]["table_owned_spans"][0]["text_span"],
                         {"content_id": "bom-context", "start_offset": 0, "end_offset": len(lead)})
        self.assertEqual(bound["table_decisions"][0]["records"][0]["owning_text_spans"][0]["end_offset"], len(lead))
        with self.assertRaisesRegex(ValueError, "required|Missing|field"):
            wrong = {**choices, "text_decisions": [{"scope_id": text_request.scope_id,
                "table_owned_spans": [{"logical_table_scope_id": "lt-bom", "text_span": {"content_ref": 0}}]}]}
            bind_item_decisions(wrong, document_map.model_dump(mode="json"), {
                "text_requests": [text_request.model_dump(mode="json")],
                "table_requests": [table_request.model_dump(mode="json")],
            })

    def test_scope_read_templates_include_both_coupled_candidates(self):
        values = fixture()
        baseline, _ = compile_fixture(values)
        tables = {"lt-bom": values[1]}
        table = scope_template(values[0], baseline, tables, "lt-bom")
        text = scope_template(values[0], baseline, tables, "text:bom-node")
        self.assertEqual(table["coupled_scope_ids"], ["text:bom-node"])
        self.assertEqual(text["coupled_scope_ids"], ["lt-bom"])
        self.assertEqual(table["candidate_template"]["records"][0]["record_key"],
                         text["candidate_template"]["table_owned_spans"][0]["record_key"])
        self.assertEqual(table["candidate_template"]["records"][0]["owning_text_spans"],
                         [text["candidate_template"]["table_owned_spans"][0]["text_span"]])


if __name__ == "__main__":
    unittest.main()
