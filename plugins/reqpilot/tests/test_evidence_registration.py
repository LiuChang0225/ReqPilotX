"""Actual synthetic files are required for portable evidence-backed decisions."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from test_review_loop import args
from test_portable import artifact_tools, item_decisions, make_fixture, write
import evidence_tools
from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256


class EvidenceRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reqpilot-evidence-unit-"); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "test-result.txt"
        self.text = "SYNTHETIC TEST ONLY: This valve opened in 28 ms and closed in 35 ms at 12 V and 23 C.\n"
        self.source.write_text(self.text)
        self.record = {"id": "test-result-1", "project_id": "test-project", "file_name": self.source.name, "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "doc_version": "1", "doc_type": "CASE", "status": "ACTIVE", "authority": "INTERNAL_CONTROLLED", "claim_role": "TEST_RESULT"}
        self.input = self.root / "registration-input.json"
        self.value = {"schema_version": "reqpilot-evidence-input/v1", "project_id": "test-project", "knowledge_base_version": 1,
            "declaration": {"origin": "HOST_REGISTERED", "reference": "Invented test fixture metadata, not a real approval."}, "documents": [{"source_path": self.source.name, "record": self.record}]}

    def register(self):
        write(self.input, self.value); output = self.root / "registered"
        evidence_tools.register(args(input=self.input, output_dir=output))
        return output

    def test_source_bytes_and_exact_fact_survive_then_changed_original_is_rejected(self):
        output = self.register(); catalog = evidence_tools.verify_catalog(output / "knowledge-catalog.json")
        fact = next(iter(catalog["evidence_catalog"].values()))
        self.assertEqual(fact["text"], self.text)
        self.assertEqual(fact["source_ref"]["end_offset"], len(self.text))
        source = next((output / "knowledge-sources").iterdir()); source.write_text("Different actual source")
        with self.assertRaisesRegex(ValueError, "registered SHA256"):
            evidence_tools.verify_catalog(output / "knowledge-catalog.json")

    def test_resealed_fake_fact_is_rejected_against_actual_source(self):
        output = self.register(); path = output / "knowledge-catalog.json"; catalog = json.loads(path.read_text())
        fact = next(iter(catalog["evidence_catalog"].values())); fact["text"] = "Fabricated lower timing"; fact["text_sha256"] = hashlib.sha256(fact["text"].encode()).hexdigest()
        catalog["catalog_sha256"] = canonical_json_sha256({k: v for k, v in catalog.items() if k != "catalog_sha256"}); write(path, catalog)
        registration_path = output / "evidence-registration.json"; registration = json.loads(registration_path.read_text()); registration["catalog_sha256"] = catalog["catalog_sha256"]
        registration["registration_sha256"] = canonical_json_sha256({k: v for k, v in registration.items() if k != "registration_sha256"}); write(registration_path, registration)
        with self.assertRaisesRegex(ValueError, "actual registered original"):
            evidence_tools.verify_catalog(path)

    def test_nonempty_catalog_without_actual_registration_is_rejected(self):
        output = self.register(); catalog = evidence_tools.verify_catalog(output / "knowledge-catalog.json")
        path = self.root / "self-signed.json"; write(path, catalog)
        with self.assertRaisesRegex(ValueError, "self-signed catalog"):
            evidence_tools.verify_catalog(path)

    def test_registered_actual_file_supports_cited_assessment_and_export(self):
        evidence = self.register()
        raw, capture, map_input = make_fixture(self.root / "customer")
        map_path = self.root / "map-input.json"; write(map_path, map_input)
        map_dir = self.root / "map"
        artifact_tools.compile_map(args(source=raw, source_ir=capture / "source_ir.json", input=map_path, output_dir=map_dir))
        item_path = self.root / "items.json"; write(item_path, item_decisions(map_dir))
        baseline_dir = self.root / "baseline"
        artifact_tools.compile_items(args(map_dir=map_dir, input=item_path, output_dir=baseline_dir))
        baseline = json.loads((baseline_dir / "source-requirement-baseline.json").read_text()); sid = baseline["records"][0]["source_record_id"]
        packet_dir = self.root / "packet"
        artifact_tools.analysis_operation(args(command="analysis-packet", baseline_dir=baseline_dir, knowledge_catalog=evidence / "knowledge-catalog.json", unit_id=[sid], output_dir=packet_dir))
        packet = json.loads((packet_dir / "analysis-packet.json").read_text()); fact = next(iter(packet["evidence_catalog"].values()))
        candidate = {"decisions": [{"unit_id": sid, "decision": "COMPLIANT", "reason": "The invented fixture result satisfies the specified two timings at the specified conditions.", "evidence": [{"fact_id": fact["fact_id"], "quote": self.text.strip()}], "clarification_question": ""}]}
        candidate_path = self.root / "candidate.json"; write(candidate_path, candidate)
        assessments = self.root / "assessments"
        artifact_tools.analysis_operation(args(command="compile-assessments", analysis_packet=packet_dir / "analysis-packet.json", input=candidate_path, previous_assessments=None, output_dir=assessments))
        final = self.root / "delivery"
        artifact_tools.deliver(args(baseline_dir=baseline_dir, source=raw, assessments=assessments / "source-item-assessments.json", output_dir=final))
        snapshot = json.loads((final / "matrix-snapshot.json").read_text())
        self.assertEqual(next(node for node in snapshot["nodes"] if (node.get("source_item") or {}).get("source_item_id") == sid)["ai_assessment"]["decision"], "COMPLIANT")
        original = next((assessments / "knowledge-sources").iterdir()); original.write_text("Changed after assessment compilation")
        with self.assertRaisesRegex(ValueError, "registered SHA256"):
            artifact_tools.deliver(args(baseline_dir=baseline_dir, source=raw, assessments=assessments / "source-item-assessments.json", output_dir=self.root / "bad-export"))


if __name__ == "__main__":
    unittest.main()
