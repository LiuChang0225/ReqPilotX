from __future__ import annotations
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import pymupdf

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_reading import source_item_reading_projections
from .test_source_requirement_baseline import _reference_with_text_node, _text_candidate, _text_item


class SourceItemReadingTests(unittest.TestCase):
    def test_projects_full_native_binding_without_changing_baseline_or_partial_bindings(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "original.pdf"
            pdf = pymupdf.open(); page = pdf.new_page()
            for dx, dy in ((0, 0), (.24, 0), (0, .24), (.24, .24)):
                page.insert_text((50 + dx, 60 + dy), "Part A 123", fontsize=10)
            pdf.save(path); pdf.close()
            original = path.read_bytes()
            with pymupdf.open(path) as pdf:
                block = next(b for b in pdf[0].get_text("rawdict")["blocks"] if b["type"] == 0)
                text = "\n".join("".join(c["c"] for s in line["spans"] for c in s["chars"]) for line in block["lines"])
            reference = _reference_with_text_node(node_id="test", label="1", title="Part", contents=(("content", text, False),))
            reference["source_sha256"] = hashlib.sha256(original).hexdigest()
            node = next(n for n in reference["document_tree"]["nodes"] if n["node_id"] == "test")
            node["source_content"][0]["bbox"] = list(block["bbox"])
            document_map = core.compile_document_map_artifact(reference)
            request = core.build_source_item_text_scope_request(document_map, node_id="test")
            candidate = _text_candidate(request, items=(_text_item(request, key="body", label="Part", start=0, end=len(text)),))
            baseline, _ = core.compile_source_requirement_baseline(document_map=document_map, requests=(), candidates=(),
                text_requests=(request,), text_candidates=(candidate,))
            before = baseline.model_dump_json()
            record = baseline.records[0]
            reading = source_item_reading_projections(document_map, record, path)
            self.assertEqual(reading["content"]["display_text"], "Part A 123")
            self.assertEqual(before, baseline.model_dump_json())
            self.assertEqual(original, path.read_bytes())
            binding = record.text_bindings[0]
            partial_text = text[:10]
            partial = binding.model_copy(update={"text": partial_text, "end_offset": len(partial_text),
                "text_sha256": hashlib.sha256(partial_text.encode()).hexdigest()})
            self.assertEqual({}, source_item_reading_projections(document_map, record.model_copy(update={"text_bindings": (partial,)}), path))


if __name__ == "__main__":
    unittest.main()
