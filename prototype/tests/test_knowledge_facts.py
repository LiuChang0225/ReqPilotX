from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()


import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.application.knowledge_facts import capture_knowledge_facts, KnowledgeFactsError


class KnowledgeFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "evidence.txt"
        self.text = "SYNTHETIC TEST ONLY\nAt 12 V and 25 C, controller A standby current was 6 mA.\n"
        self.path.write_text(self.text)
        self.record = {"id": "knowledge-a", "project_id": "project-a",
                       "source_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                       "file_name": "evidence.txt", "doc_version": "1",
                       "doc_type": "CASE", "status": "ACTIVE",
                       "authority": "INTERNAL_CONTROLLED", "claim_role": "TEST_RESULT"}

    def test_exact_text_and_source_identity_survive_capture(self):
        captured = capture_knowledge_facts(self.record, self.path, project_id="project-a")
        fact = next(iter(captured["facts"].values()))
        self.assertEqual(self.text, fact["text"])
        self.assertEqual(self.record["source_sha256"], fact["source_sha256"])
        self.assertEqual("PRODUCT_EVIDENCE", fact["role"])
        self.assertEqual(len(self.text), fact["source_ref"]["end_offset"])
        self.assertEqual(captured, capture_knowledge_facts(self.record, self.path, project_id="project-a"))

    def test_different_project_is_rejected(self):
        with self.assertRaises(KnowledgeFactsError):
            capture_knowledge_facts(self.record, self.path, project_id="project-b")

    def test_changed_source_is_rejected(self):
        self.path.write_text(self.text.replace("6 mA", "4 mA"))
        with self.assertRaises(KnowledgeFactsError):
            capture_knowledge_facts(self.record, self.path, project_id="project-a")

    def test_superseded_knowledge_is_visible_but_not_current_evidence(self):
        self.record["status"] = "SUPERSEDED"
        captured = capture_knowledge_facts(self.record, self.path, project_id="project-a")
        self.assertEqual({}, captured["facts"])
        self.assertEqual("status_not_active", captured["document"]["eligibility_reason"])

    def test_unknown_authority_is_not_promoted_by_test_filename(self):
        self.record.update(authority="UNKNOWN", claim_role="REFERENCE_CONTEXT")
        captured = capture_knowledge_facts(self.record, self.path, project_id="project-a")
        fact = next(iter(captured["facts"].values()))
        self.assertFalse(fact["authority_verified"])
        self.assertEqual("INTERNAL_KNOWLEDGE", fact["role"])


if __name__ == "__main__":
    unittest.main()
