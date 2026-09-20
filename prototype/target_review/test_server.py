"""Synthetic-only tests: never write simulated human acceptance to case data."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from server import CLASSES, ReviewError, ReviewStore, canonical, make_server


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reqpilot-review-test-")
        self.root = Path(self.temp.name)
        source = b"synthetic PDF identity only; no customer content"
        (self.root / "source.pdf").write_bytes(source)
        self.bundle = {
            "schema": "reqpilot-target-review/v1",
            "source": {"sha256": hashlib.sha256(source).hexdigest()},
            "items": [
                {
                    "id": "s1",
                    "class": "Q",
                    "text": "synthetic original",
                    "derived": [
                        {
                            "id": "s1/d1",
                            "parentId": "s1",
                            "class": "Q",
                            "text": "synthetic derived",
                        }
                    ],
                }
            ],
        }
        self.bundle["revision"] = hashlib.sha256(canonical(self.bundle)).hexdigest()
        (self.root / "review-bundle.json").write_bytes(canonical(self.bundle))
        self.store = ReviewStore(self.root, reviewer_kind="qa")

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, action="comment", **extra):
        return {
            "mutationId": "synthetic-request-0001",
            "expectedRevision": self.store.state()["revision"],
            "sourceRevision": self.bundle["revision"],
            "targetId": "s1",
            "action": action,
            "note": "synthetic QA only",
            "patch": {},
            **extra,
        }

    def test_pending_review_is_not_inferred(self):
        self.assertEqual(self.store.state()["targets"], {})

    def test_persists_and_reloads_bound_events(self):
        self.store.append(self.payload())
        restored = ReviewStore(self.root, reviewer_kind="qa")
        self.assertEqual(restored.state()["revision"], 1)
        self.assertEqual(restored.state()["targets"]["s1"]["status"], "needs-review")
        self.assertEqual(restored.events[0]["actor"]["kind"], "qa")

    def test_idempotent_retry_does_not_duplicate(self):
        p = self.payload()
        self.store.append(p)
        result = self.store.append(p)
        self.assertTrue(result["idempotent"])
        self.assertEqual(len(self.store.events), 1)

    def test_idempotency_key_cannot_change_content(self):
        self.store.append(self.payload())
        with self.assertRaises(ReviewError):
            self.store.append(self.payload(note="different"))

    def test_stale_feedback_revision_is_rejected(self):
        p = self.payload()
        self.store.append(p)
        with self.assertRaises(ReviewError):
            self.store.append({**p, "mutationId": "synthetic-request-0002"})

    def test_source_revision_cannot_cross_bind(self):
        with self.assertRaises(ReviewError):
            self.store.append(self.payload(sourceRevision="other"))

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(ReviewError):
            self.store.append(self.payload(targetId="other"))

    def test_original_text_is_immutable(self):
        with self.assertRaises(ReviewError):
            self.store.append(self.payload("correct", patch={"text": "changed"}))

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(ReviewError):
            self.store.append(self.payload(approvedAll=True))

    def test_classification_change_requires_reason(self):
        with self.assertRaises(ReviewError):
            self.store.append(self.payload("correct", patch={"class": "C"}, note=""))

    def test_invalid_class_and_empty_derived_text(self):
        for change in [{"class": "NOT-A-CLASS"}, {"text": "  "}]:
            with self.assertRaises(ReviewError):
                self.store.append(
                    self.payload("correct", targetId="s1/d1", patch=change)
                )

    def test_revision_history_and_example_are_valid_non_requirement_roles(self):
        self.assertTrue({"REVISION_HISTORY", "EXAMPLE"}.issubset(CLASSES))
        for number, role in enumerate(("REVISION_HISTORY", "EXAMPLE"), 1):
            self.store.append(
                self.payload(
                    "correct",
                    mutationId=f"synthetic-role-{number:04d}",
                    patch={"class": role},
                )
            )

    def test_correction_does_not_mutate_ai_draft(self):
        self.store.append(
            self.payload(
                "correct", targetId="s1/d1", patch={"text": "new draft", "class": "F"}
            )
        )
        self.assertEqual(
            self.store.bundle["items"][0]["derived"][0]["text"], "synthetic derived"
        )
        self.assertEqual(
            self.store.state()["targets"]["s1/d1"]["patch"]["text"], "new draft"
        )

    def test_confirmation_does_not_confirm_child(self):
        self.store.append(self.payload("confirm", note=""))
        self.assertNotIn("s1/d1", self.store.state()["targets"])

    def test_undo_preserves_history_and_restores_state(self):
        self.store.append(self.payload("correct", patch={"class": "C"}))
        self.store.append(
            self.payload(
                "undo",
                mutationId="synthetic-request-0002",
                undoOf="synthetic-request-0001",
                note="",
            )
        )
        self.assertEqual(self.store.state()["targets"], {})
        self.assertEqual(len(self.store.events), 2)

    def test_undo_not_latest_is_rejected(self):
        self.store.append(self.payload())
        self.store.append(self.payload(mutationId="synthetic-request-0002"))
        with self.assertRaises(ReviewError):
            self.store.append(
                self.payload(
                    "undo",
                    mutationId="synthetic-request-0003",
                    undoOf="synthetic-request-0001",
                )
            )

    def test_concurrent_stale_writers_cannot_both_succeed(self):
        p1 = self.payload()
        p2 = {**p1, "mutationId": "synthetic-request-0002"}

        def write(p):
            try:
                self.store.append(p)
                return True
            except ReviewError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, [p1, p2]))
        self.assertEqual(sorted(results), [False, True])

    def test_bad_source_digest_fails_closed(self):
        (self.root / "source.pdf").write_bytes(b"different")
        with self.assertRaises(ValueError):
            ReviewStore(self.root)

    def test_tampered_bundle_fails_closed(self):
        b = {**self.bundle, "extra": "tampered"}
        (self.root / "review-bundle.json").write_bytes(canonical(b))
        with self.assertRaises(ValueError):
            ReviewStore(self.root)

    def test_corrupt_log_is_not_reset(self):
        self.store.path.write_text('{"broken":')
        with self.assertRaises(ValueError):
            ReviewStore(self.root)
        self.assertEqual(self.store.path.read_text(), '{"broken":')

    def test_confirm_cannot_smuggle_edit(self):
        with self.assertRaises(ReviewError):
            self.store.append(self.payload("confirm", patch={"class": "F"}))

    def test_long_note_rejected(self):
        with self.assertRaises(ReviewError):
            self.store.append(self.payload(note="x" * 8001))

    def test_uncertain_io_stops_writes_and_restart_replays_once(self):
        p = self.payload()
        with patch("server.os.fsync", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.store.append(p)
        with self.assertRaises(ReviewError):
            self.store.append(p)
        restored = ReviewStore(self.root, reviewer_kind="qa")
        self.assertEqual(restored.state()["revision"], 1)
        self.assertTrue(restored.append(p)["idempotent"])


class HttpTests(StoreTests):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.store, 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data, r.headers

    def post(self, headers=None, path="/api/review"):
        h = {
            "Content-Type": "application/json",
            "Origin": f"http://{self.host}",
            "X-Review-Token": self.store.csrf_token,
            **(headers or {}),
        }
        return self.request("POST", path, canonical(self.payload()), h)

    def test_http_roundtrip_and_export_without_token(self):
        status, data, _ = self.post()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["state"]["revision"], 1)
        status, data, headers = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertNotIn("writeToken", json.loads(data)["state"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_cross_origin_and_bad_token_rejected(self):
        self.assertEqual(self.post({"Origin": "http://malicious.example"})[0], 403)
        self.assertEqual(self.post({"X-Review-Token": "bad"})[0], 403)
        self.assertEqual(len(self.store.events), 0)

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(
            self.request("GET", "/api/bundle", headers={"Host": "other.example"})[0],
            403,
        )

    def test_private_and_path_traversal_resources_not_served(self):
        for path in [
            "/../source-evidence.json",
            "/%2e%2e/source.pdf",
            "/WORKLOG.md",
            "/feedback/reviews.jsonl",
        ]:
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_unknown_endpoint_does_not_write(self):
        self.assertEqual(self.post(path="/api/approve-everything")[0], 404)

    def test_source_served_only_as_pdf(self):
        status, _, headers = self.request("GET", "/source.pdf")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/pdf")

    def test_no_page_fallback_when_image_missing(self):
        status, data, _ = self.request("GET", "/pages/p-01.png")
        self.assertEqual(status, 404)
        self.assertIn("缺失", json.loads(data)["error"])

    def test_three_digit_page_image_is_served_for_long_documents(self):
        pages = self.root / "pages"
        pages.mkdir()
        (pages / "p-100.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        status, data, headers = self.request("GET", "/pages/p-100.png")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"\x89PNG\r\n\x1a\n")
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(self.request("GET", "/pages/p-1.png")[0], 404)
        self.assertEqual(self.request("GET", "/pages/p-10000.png")[0], 404)


if __name__ == "__main__":
    unittest.main()
