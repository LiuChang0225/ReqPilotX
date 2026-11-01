"""Independent public no-op command identity acceptance on synthetic tasks."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch

import test_task_review_policy as fixtures


class PolicyCommandAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.TaskReviewPolicyTests("test_preserve_is_readonly_and_stale_policy_request_is_rejected")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def preserve(self):
        request = self.case.request(command_id="independent-reserved-key")
        result = self.case.service.invoke("review", request)
        self.assertEqual(result["operation_state"], "NOT_NEEDED")
        return request

    def classification_request(self, command):
        return self.case.request(command_id=command, changes={
            "model_id": "synthetic-model",
            "decisions": [{"source_record_id": self.case.baseline.records[0].source_record_id,
                "requirementness": "REQUIREMENT", "requirement_kinds": ["QUALITY"],
                "confidence": 0.9, "decision_summary": "Synthetic independent source classification revision.",
                "reason_codes": ["source_meaning"]}],
        })

    def test_preserved_key_rejects_public_classification_revision_before_writes(self):
        request = self.preserve()
        before = deepcopy(fixtures.review.load_head(self.case.review))
        changed = self.classification_request(request["command_id"])
        with patch.object(fixtures.review, "revise_review", wraps=fixtures.review.revise_review) as compile_revision, \
             patch.object(fixtures.artifact_tools, "deliver", wraps=fixtures.artifact_tools.deliver) as export, \
             patch.object(fixtures.review, "close_review", wraps=fixtures.review.close_review) as close:
            with self.assertRaisesRegex(ValueError, "COMMAND_ID_CONFLICT"):
                self.case.service.invoke("revise", changed)
            compile_revision.assert_not_called(); export.assert_not_called(); close.assert_not_called()
        self.assertEqual(fixtures.review.load_head(self.case.review), before)

    def test_preserved_key_rejects_public_annotation_change_before_writes(self):
        request = self.preserve()
        before = deepcopy(fixtures.review.load_head(self.case.review))
        changed = self.case.request(command_id=request["command_id"], changes={"annotation_updates": [
            {"source_item_id": self.case.baseline.records[0].source_record_id,
             "topics": ["VERIFICATION"], "reason": "Synthetic annotation must require a distinct command identity."},
        ]})
        with patch.object(fixtures.review, "revise_review", wraps=fixtures.review.revise_review) as compile_revision, \
             patch.object(fixtures.artifact_tools, "deliver", wraps=fixtures.artifact_tools.deliver) as export:
            with self.assertRaisesRegex(ValueError, "COMMAND_ID_CONFLICT"):
                self.case.service.invoke("annotations", changed)
            compile_revision.assert_not_called(); export.assert_not_called()
        self.assertEqual(fixtures.review.load_head(self.case.review), before)

    def test_a_distinct_command_still_allows_actual_public_revision(self):
        self.preserve()
        before = fixtures.review.load_head(self.case.review)["review_sha256"]
        result = self.case.service.invoke("revise", self.classification_request("independent-fresh-revision"))
        self.assertEqual(result["operation_state"], "COMMITTED", result)
        self.assertNotEqual(result["new_review_sha256"], before)

    def test_concurrent_preserve_and_revision_can_claim_the_key_only_once(self):
        key = "independent-racing-key"
        preserved = self.case.request(command_id=key)
        revision = self.classification_request(key)
        barrier = Barrier(2)
        def invoke(item):
            action, payload = item
            barrier.wait()
            try:
                return {"action": action, "result": self.case.service.invoke(action, payload)}
            except ValueError as error:
                return {"action": action, "error": str(error)}
        before = fixtures.review.load_head(self.case.review)["review_sha256"]
        with patch.object(fixtures.review, "revise_review", wraps=fixtures.review.revise_review) as compile_revision, \
             patch.object(fixtures.artifact_tools, "deliver", wraps=fixtures.artifact_tools.deliver) as export, \
             ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(invoke, [("review", preserved), ("revise", revision)]))
        successes = [reply for reply in replies if "result" in reply]
        conflicts = [reply for reply in replies if "error" in reply]
        self.assertEqual(len(successes), 1, replies)
        self.assertEqual(len(conflicts), 1, replies)
        self.assertIn("COMMAND_ID_CONFLICT", conflicts[0]["error"])
        if successes[0]["action"] == "review":
            self.assertEqual(successes[0]["result"]["operation_state"], "NOT_NEEDED")
            compile_revision.assert_not_called(); export.assert_not_called()
            self.assertEqual(fixtures.review.load_head(self.case.review)["review_sha256"], before)
        else:
            self.assertEqual(successes[0]["result"]["operation_state"], "COMMITTED")
            self.assertEqual(compile_revision.call_count, 1)
            self.assertEqual(export.call_count, 1)
            self.assertNotEqual(fixtures.review.load_head(self.case.review)["review_sha256"], before)


if __name__ == "__main__":
    unittest.main()
