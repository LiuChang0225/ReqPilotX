"""Synthetic native-candidate integration; never touch customer feedback or DB."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import http.client
from threading import Thread

import fitz
from tests.test_source_requirement_baseline import _reference_with_text_node, _text_candidate, _text_item
from app.application.agent_workflow import source_requirement_baseline as source
from app.application.agent_workflow.source_item_decomposition import compile_source_item_decomposition
from native_candidate import NativeCandidateStore
from loop_context import project_context
from server import make_server


class NativeCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        text = "The supplier shall submit a report."
        with fitz.open() as pdf:
            pdf.new_page(width=595, height=842).insert_text((30, 30), text)
            pdf.save(self.root / "source.pdf")
        public = _reference_with_text_node(node_id="clause", label="1", title="Report",
                                          contents=(("body", text, False),))
        public["source_sha256"] = hashlib.sha256((self.root / "source.pdf").read_bytes()).hexdigest()
        public["logical_tables"] = []
        public["document_tree"]["nodes"] = [n for n in public["document_tree"]["nodes"] if n["node_id"] in {"root", "clause"}]
        document_map = source.compile_document_map_artifact(public)
        request = source.build_source_item_text_scope_request(document_map, node_id="clause")
        candidate = _text_candidate(request, items=(_text_item(request, key="body", label="Report", start=0, end=len(text)),))
        old, receipt = source.compile_source_requirement_baseline(document_map=document_map,
            requests=(), candidates=(), text_requests=(request,), text_candidates=(candidate,))
        decision = source.SourceRequirementClassificationCoordinationDecisionV1(
            source_record_id=old.records[0].source_record_id, requirementness="REQUIREMENT", requirement_kinds=("PROJECT",),
            confidence=0.95, decision_summary="Report delivery is a project obligation.", reason_codes=("project_delivery",))
        new = source.compile_source_requirement_classification_coordination(previous_artifact=old,
            previous_validation_receipt=receipt, decisions=(decision,), model_id="synthetic-agent")[0]
        values = {"document-map.json": document_map.model_dump(mode="json"), "baseline-previous.json": old.model_dump(mode="json"),
            "baseline-candidate.json": new.model_dump(mode="json"), "predecessor-validation-receipt.json": receipt.model_dump(mode="json"),
            "classification-decisions.json": [decision.model_dump(mode="json")], "table-previews.json": {},
            "decomposition.json": compile_source_item_decomposition(new, []),
            "annotations.json": {"baseline_sha256": new.artifact_sha256, "records": {}}, "semantic-notes.json": {},
            "review-issues.json": {"issues": []}}
        for name, value in values.items():
            (self.root / name).write_text(json.dumps(value))
        self.manifest = {"schema_version": "native-source-item-review/v1", "case_id": "synthetic-native",
            "project_id": "project", "document_id": "document", "project_name": "Synthetic", "source_name": "Synthetic.pdf",
            "scope": "Synthetic source-preserving successor", "initial_node_id": "clause",
            "expected_predecessor_sha256": old.artifact_sha256,
            "provenance": {"kind": "CODEX_APP_DIRECT", "model_id": "synthetic-agent",
                "recorded_at": "2026-01-01T00:00:00Z", "action_record": "Synthetic test only, no actual Agent acceptance."},
            "files": {}}
        self.resign()

    def tearDown(self):
        self.temp.cleanup()

    def resign(self):
        self.manifest["files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.iterdir() if p.is_file() and p.name != "native-review.json"}
        (self.root / "native-review.json").write_text(json.dumps(self.manifest))

    def test_native_tree_diff_and_feedback_keep_real_source_identity(self):
        store = NativeCandidateStore(self.root, reviewer_kind="qa")
        snapshot = store.native_matrix()
        node = next(n for n in snapshot["nodes"] if n.get("source_item"))
        self.assertEqual(node["source_item"]["source_display_id"], "SRC-0001")
        self.assertEqual(node["classification_paths"], ["PROJECT"])
        self.assertIn("约束", node["candidate_review"]["previousDecision"])
        self.assertEqual(snapshot["root_node_ids"], store.document_map.root_node_ids and ["root"])
        self.assertEqual(store.state()["eventCount"], 0)
        payload = {"mutationId": "synthetic-native-feedback-1", "expectedRevision": 0,
            "sourceRevision": store.source_revision, "targetId": node["node_id"], "action": "comment", "note": "QA only"}
        store.append(payload)
        restored = NativeCandidateStore(self.root, reviewer_kind="qa")
        self.assertEqual(restored.events[0]["actor"]["kind"], "qa")
        self.assertEqual(restored.baseline.predecessor_artifact_sha256, restored.previous.artifact_sha256)
        feedback = {"source": store.bundle["source"], "sourceRevision": store.source_revision, "state": store.state(False)}
        context = project_context(store.bundle, feedback, review_url="http://127.0.0.1:8001", allow_qa=True)
        self.assertEqual(context["counts"]["sourceItems"], 1)
        self.assertEqual(context["nativeSourceContext"]["document_map"]["artifact_sha256"], store.document_map.artifact_sha256)

    def test_rehashed_successor_cannot_change_unapproved_fields(self):
        path = self.root / "baseline-candidate.json"
        current = source.SourceRequirementBaselineArtifactV1.model_validate(json.loads(path.read_text()))
        altered = current.model_copy(update={"records": (current.records[0].model_copy(update={"decision_summary": "Different unauthored judgment"}),)})
        digest = source.canonical_json_sha256(source._baseline_hash_payload(altered))
        altered = altered.model_copy(update={"artifact_sha256": digest, "artifact_id": "source-requirement-baseline-" + digest[:32]})
        path.write_text(json.dumps(altered.model_dump(mode="json")))
        self.resign()
        with self.assertRaisesRegex(ValueError, "source-preserving decisions"):
            NativeCandidateStore(self.root, reviewer_kind="qa")

    def test_full_native_export_uses_the_typed_graph_without_review_controls(self):
        store = NativeCandidateStore(self.root, reviewer_kind="qa")
        result = store.native_export({"sourceRevision": store.source_revision,
            "expectedRevision": 0, "nodeIds": list(store.targets)})
        self.assertTrue(result.startswith(b"PK"))
        self.assertEqual(store.state()["eventCount"], 0)
        with self.assertRaisesRegex(ValueError, "导出全部"):
            store.native_export({"sourceRevision": store.source_revision, "expectedRevision": 0, "nodeIds": []})

    def test_source_and_predecessor_pins_are_checked(self):
        self.manifest["expected_predecessor_sha256"] = "b" * 64
        self.resign()
        with self.assertRaisesRegex(ValueError, "predecessor"):
            NativeCandidateStore(self.root, reviewer_kind="qa")
        self.manifest["expected_predecessor_sha256"] = json.loads((self.root / "baseline-previous.json").read_text())["artifact_sha256"]
        with (self.root / "source.pdf").open("ab") as file:
            file.write(b"changed")
        self.resign()
        with self.assertRaisesRegex(ValueError, "supplied original"):
            NativeCandidateStore(self.root, reviewer_kind="qa")

    def test_missing_direct_host_action_record_is_not_an_appserver_receipt(self):
        del self.manifest["provenance"]["action_record"]
        self.resign()
        with self.assertRaisesRegex(ValueError, "action record"):
            NativeCandidateStore(self.root, reviewer_kind="qa")

    def test_adapter_domain_errors_remain_http_conflicts_across_host_module_identity(self):
        store = NativeCandidateStore(self.root, reviewer_kind="qa")
        class HostedConflict(Exception):
            status = 409
        def conflict(_payload):
            raise HostedConflict("Synthetic stale revision")
        store.review_error_type = HostedConflict
        store.append = conflict
        server = make_server(store, port=0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("POST", "/api/review", body="{}", headers={
                "Origin": f"http://127.0.0.1:{port}", "X-Review-Token": store.csrf_token,
                "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            self.assertEqual(json.loads(response.read())["error"], "Synthetic stale revision")
            connection.close()
            self.assertEqual(store.state()["eventCount"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
