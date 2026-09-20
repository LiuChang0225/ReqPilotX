"""Real source and task checks for candidate versus selected source context."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "prototype"), str(ROOT / "plugins/reqpilot/scripts")]
from task_service import TaskService  # noqa: E402
from test_portable import item_decisions  # noqa: E402


class ContextReadingTests(unittest.TestCase):
    def setUp(self):
        import pymupdf

        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "context.pdf"
        lines = [
            "Context fixture",
            "1 Terms",
            "Response duration means valid edge to stable output.",
            "2 Applicability",
            "Unless stated otherwise, limits apply at nominal voltage.",
            "3 Performance",
            "Response duration shall be less than 30 ms.",
            "4 Note",
            "The sketch is illustrative only.",
        ]
        pdf = pymupdf.open()
        page = pdf.new_page()
        for i, text in enumerate(lines):
            page.insert_text((40, 55 + 40 * i), text, fontsize=11)
        pdf.save(source)
        pdf.close()
        self.service = TaskService(
            self.root, ROOT / "plugins/reqpilot/scripts/reqpilot.py", ROOT
        )
        state = self.service.invoke(
            "open",
            {
                "mode": "create",
                "source": str(source),
                "goal": "Read source context.",
                "creation_key": "context",
            },
        )
        self.task = state["task_id"]
        state = self.apply("prepare")
        capture = json.loads(Path(state["selected"]["READING"]["path"]).read_text())
        by_text = {n["raw_text"]: n["source_node_id"] for n in capture["nodes"]}
        nodes = [
            {
                "node_id": "document",
                "kind": "DOCUMENT",
                "title": lines[0],
                "title_source_ids": [by_text[lines[0]]],
            }
        ]
        for n in range(1, 5):
            nodes.append(
                {
                    "node_id": "s" + str(n),
                    "parent_id": "document",
                    "kind": "SECTION",
                    "heading": {"source": by_text[lines[2 * n - 1]], "label": str(n)},
                    "content": [by_text[lines[2 * n]]],
                }
            )
        state = self.apply("map", decisions={"nodes": nodes})
        decisions = item_decisions(Path(state["selected"]["MAP"]["output_dir"]))
        roles = {"text:s1": "DEFINITION", "text:s2": "CONTEXT", "text:s4": "NOTE"}
        for scope in decisions["text_decisions"]:
            for item in scope["items"]:
                item["source_form"] = "PARAGRAPH"
                role = roles.get(scope["scope_id"])
                item.update(
                    requirementness="NON_REQUIREMENT" if role else "REQUIREMENT",
                    requirement_kinds=[] if role else ["QUALITY"],
                    non_requirement_role=role,
                )
        for key in ("schema_version", "source_sha256", "document_map_artifact_sha256"):
            decisions.pop(key)
        state = self.apply("items", decisions=decisions)
        records = json.loads(Path(state["selected"]["ITEMS"]["path"]).read_text())[
            "records"
        ]
        self.records = {r["node_id"]: r for r in records}
        self.parent = self.records["s3"]["source_display_id"]

    def apply(self, action, **kwargs):
        state = self.service.invoke("inspect", {"task_id": self.task})
        result = self.service.invoke(
            action,
            {"task_id": self.task, "expected_revision": state["revision"], **kwargs},
        )
        self.assertEqual(result["operation_state"], "COMMITTED", result)
        return result

    def read(self, **kwargs):
        return self.service.invoke(
            "read",
            {
                "task_id": self.task,
                "view": "item",
                "source_item_id": self.parent,
                **kwargs,
            },
        )

    def link(self, node_ids):
        return self.apply(
            "annotations",
            changes={
                "annotation_updates": [
                    {
                        "source_item_id": self.parent,
                        "context_source_ids": [
                            self.records[n]["source_display_id"] for n in node_ids
                        ],
                        "reason": "Source scope review for this synthetic fixture.",
                    }
                ]
            },
        )

    def complete(self, request):
        result = self.service.invoke("read", request)
        if "data" in result:
            return result["data"]
        chunks = [result["text"]]
        while result.get("next_read"):
            result = self.service.invoke("read", result["next_read"]["request"])
            chunks.append(result["text"])
        return json.loads("".join(chunks))

    def test_candidates_are_role_based_source_evidence_and_do_not_write_links(self):
        state = self.service.state(self.task)[1]
        index = Path(state["task_root"]) / "workspace-index.json"
        before = index.read_bytes()
        value = self.read()
        self.assertEqual(value["context_review"]["candidate_count"], 2)
        self.assertEqual(value["context_review"]["linked_source_ids"], [])
        candidates = self.service.invoke(
            "read", value["context_review"]["read_candidates"]["request"]
        )
        self.assertEqual(
            {c["source_role"] for c in candidates["candidates"]},
            {"DEFINITION", "CONTEXT"},
        )
        self.assertTrue(
            all(c["applicability"] == "NOT_INFERRED" for c in candidates["candidates"])
        )
        definition = next(
            c for c in candidates["candidates"] if c["source_role"] == "DEFINITION"
        )
        source = self.service.invoke("read", definition["read"]["request"])
        self.assertIn("valid edge", source["entries"][0]["text"])
        self.assertEqual(before, index.read_bytes())

    def test_explicit_context_is_visible_in_analysis_without_becoming_parent_ranges(
        self,
    ):
        before = self.complete(self.read()["reads"]["decomposition_packet"]["request"])
        self.link(["s1", "s2"])
        after = self.complete(
            self.read(max_chars=24000)["reads"]["decomposition_packet"]["request"]
        )
        self.assertEqual(before["segments"], after["segments"])
        self.assertEqual(
            before["parent_evidence_sha256"], after["parent_evidence_sha256"]
        )
        self.assertEqual(len(after["linked_source_context"]), 2)
        self.assertIn(
            "valid edge", after["linked_source_context"][0]["source"][0]["text"]
        )
        self.assertIn("not parent segment_index", after["linked_context_namespace"])

    def test_context_replacement_invalidates_pinned_reads_with_same_baseline(self):
        old = self.read()
        self.link(["s1"])
        for request in (
            old["context_review"]["read_candidates"]["request"],
            old["reads"]["decomposition_packet"]["request"],
        ):
            with self.assertRaisesRegex(ValueError, "CONTEXT_VERSION_CONFLICT"):
                self.service.invoke("read", request)
        fresh = self.read()
        self.assertEqual(old["baseline_sha256"], fresh["baseline_sha256"])
        self.assertNotEqual(
            old["context_review"]["context_bindings_sha256"],
            fresh["context_review"]["context_bindings_sha256"],
        )

    def test_candidate_pagination_and_linked_note_preserve_explicit_selection(self):
        first = self.read(part="context_candidates", limit=1)
        self.assertFalse(first["complete"])
        second = self.service.invoke("read", first["next_read"]["request"])
        self.assertTrue(second["complete"])
        self.assertNotEqual(
            first["candidates"][0]["source_item_id"],
            second["candidates"][0]["source_item_id"],
        )
        self.link(["s4"])
        current = self.read(part="context_candidates")
        note = next(c for c in current["candidates"] if c["source_role"] == "NOTE")
        self.assertEqual(note["relation"], "EXPLICIT_CONTEXT_LINK")
        linked = self.complete(
            self.read()["context_review"]["read_linked_context"]["request"]
        )
        self.assertEqual(len(linked), 1)
        self.assertIn("illustrative", linked[0]["source"][0]["text"])


if __name__ == "__main__":
    unittest.main()
