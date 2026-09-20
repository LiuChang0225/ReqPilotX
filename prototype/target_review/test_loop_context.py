from copy import deepcopy
import hashlib
import json
import unittest

from loop_context import origin, project_context


class LoopContextTests(unittest.TestCase):
    def setUp(self):
        self.bundle = {
            "source": {"sha256": "synthetic-source"},
            "items": [{"id": "clause", "sectionId": "section", "text": "Original condition",
                       "sourceRefs": [{"page": 1}], "derived": [{"id": "child", "text": "Derived",
                                                                 "sourceRefs": [{"page": 1}]}]}],
            "ui": {"ownerAcceptances": [{"sourceRevision": "older", "dimension": "classification"}]},
        }
        self.bundle["revision"] = hashlib.sha256(json.dumps(self.bundle, ensure_ascii=False, sort_keys=True,
                                                            separators=(",", ":")).encode()).hexdigest()
        self.feedback = {"source": self.bundle["source"], "sourceRevision": self.bundle["revision"],
                         "state": {"sourceRevision": self.bundle["revision"], "revision": 2,
                                   "reviewerKind": "engineer", "writeToken": "do-not-leak",
                                   "targets": {"child": {"status": "needs-review", "notes": [{"text": "Keep the condition"}],
                                                         "actor": {"kind": "engineer"}, "lastEventId": "event-2"}}}}

    def context(self, **kwargs):
        return project_context(self.bundle, self.feedback, review_url="http://127.0.0.1:9000/workspace", **kwargs)

    def test_feedback_is_version_bound_without_approval_propagation_or_token(self):
        before = deepcopy((self.bundle, self.feedback))
        result = self.context()
        self.assertEqual(result["targets"][0]["feedback"]["status"], "unreviewed")
        self.assertEqual(result["targets"][1]["feedback"]["status"], "needs-review")
        self.assertEqual(result["guidance"]["ownerAcceptances"][0]["sourceRevision"], "older")
        self.assertNotIn("do-not-leak", json.dumps(result))
        self.assertEqual(before, (self.bundle, self.feedback))

    def test_selected_scope_keeps_full_document_access_and_parent_identity(self):
        result = self.context(target_ids=["child"])
        self.assertEqual(result["targets"][0]["parentId"], "clause")
        self.assertTrue(result["evidence"]["bundle"].endswith("/api/bundle"))
        self.assertEqual(result["counts"]["selectedTargets"], 1)
        with self.assertRaises(ValueError):
            self.context(target_ids=["invented"])

    def test_derived_focus_retains_test_relations_and_versioned_history(self):
        self.bundle['items'][0]['analysisAssociations'] = {'sourceItemIds': ['equipment', 'method']}
        self.bundle['issues'] = [{'id': 'boundary', 'targetId': 'clause'}]
        self.bundle['inheritedReviewState'] = {'ownerAcceptances': [{'reviewedRevision': 'prior'}]}
        self.bundle.pop('revision')
        revision = hashlib.sha256(json.dumps(self.bundle, ensure_ascii=False, sort_keys=True,
                                            separators=(',', ':')).encode()).hexdigest()
        self.bundle['revision'] = revision
        self.feedback['sourceRevision'] = self.feedback['state']['sourceRevision'] = revision
        result = self.context(target_ids=['child'])
        self.assertEqual(result['targets'][0]['analysisAssociations']['sourceItemIds'], ['equipment', 'method'])
        self.assertEqual(result['sourceIssues'][0]['id'], 'boundary')
        self.assertEqual(result['inheritedGuidance']['ownerAcceptances'][0]['reviewedRevision'], 'prior')
        self.assertEqual(result['targets'][0]['feedback']['status'], 'needs-review')

    def test_mixed_source_revisions_and_modified_bundle_are_rejected(self):
        for field, value in [("sourceRevision", "stale"), ("source", {"sha256": "another-pdf"})]:
            original = self.feedback[field]
            self.feedback[field] = value
            with self.assertRaises(ValueError):
                self.context()
            self.feedback[field] = original
        self.bundle["items"][0]["text"] = "Changed in place"
        with self.assertRaises(ValueError):
            self.context()

    def test_qa_requires_explicit_testing_mode(self):
        self.feedback["state"]["reviewerKind"] = "qa"
        with self.assertRaises(ValueError):
            self.context()
        self.assertEqual(self.context(allow_qa=True)["reviewerKind"], "qa")
        self.feedback["state"]["reviewerKind"] = "engineer"
        self.feedback["state"]["targets"]["child"]["actor"] = {"kind": "qa"}
        with self.assertRaises(ValueError):
            self.context()

    def test_local_origin_does_not_follow_supplied_external_or_credentialed_url(self):
        self.assertEqual(origin("http://localhost:9000/workspace?case=x"), "http://localhost:9000")
        for url in ["https://example.com:9000", "http://user:pass@localhost:9000", "file:///tmp/data", "http://localhost"]:
            with self.assertRaises(ValueError):
                origin(url)


if __name__ == "__main__":
    unittest.main()
