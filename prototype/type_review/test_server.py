from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from server import DecisionError, DecisionStore, canonical


class DecisionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "renders").mkdir()
        types = []
        for number in range(1, 18):
            type_id = f"T{number:02d}"
            preview = self.root / "renders" / f"{type_id}.png"
            preview.write_bytes(b"\x89PNG\r\n\x1a\n")
            types.append(
                {
                    "id": type_id,
                    "title": f"Type {number}",
                    "format": "PDF",
                    "role": "test",
                    "source": "fixture",
                    "route": "requirement_source",
                    "coverage": "CANDIDATE_READY",
                    "recommendation": "ACCEPT",
                    "outcome": "fixture",
                    "judgment": "fixture",
                    "problems": [],
                    "decision": "fixture?",
                    "preview": f"renders/{type_id}.png",
                    "evidence": [],
                    "ownerDecision": "pending",
                    "proxyActor": "agent-proxy",
                }
            )
        artifact = {
            "schema": "reqpilotx-all-types-owner-review/v1",
            "createdAt": "2026-09-04T00:00:00+00:00",
            "status": "ready-for-owner-decision",
            "summary": {},
            "decisions": [
                {"id": f"D{number:02d}", "title": "Decision", "recommendation": "ACCEPT", "detail": "fixture"}
                for number in range(1, 7)
            ],
            "types": types,
            "evidence": {},
        }
        artifact["revision"] = __import__("hashlib").sha256(canonical(artifact)).hexdigest()
        (self.root / "type-review.json").write_bytes(canonical(artifact))
        (self.root / "REPORT.md").write_text("# fixture\n")
        self.store = DecisionStore(self.root, self.root / "feedback")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, *, action: str = "accept", note: str = "", mutation: str = "1234567890abcdef", expected: int = 0, target: str = "D01") -> dict:
        return {"mutationId": mutation,"expectedRevision": expected,"sourceRevision": self.store.source_revision,"targetId": target,"action": action,"note": note}

    def test_artifact_and_preview_scope_are_validated(self) -> None:
        self.assertEqual(len(self.store.targets), 23)
        self.assertEqual(len(self.store.previews), 17)

    def test_accept_changes_only_one_target(self) -> None:
        self.store.append(self.payload())
        state = self.store.state(False)
        self.assertEqual(state["targets"]["D01"]["status"], "accepted")
        self.assertNotIn("D02", state["targets"])

    def test_adjust_and_defer_require_a_note(self) -> None:
        for action in ("adjust", "defer", "comment"):
            with self.assertRaises(DecisionError):
                self.store.append(self.payload(action=action, mutation=f"1234567890abc{action[0]}"))

    def test_stale_state_and_source_revision_are_rejected(self) -> None:
        self.store.append(self.payload())
        with self.assertRaises(DecisionError):
            self.store.append(self.payload(expected=0, mutation="2234567890abcdef", target="D02"))
        payload = self.payload(expected=1, mutation="3234567890abcdef", target="D02")
        payload["sourceRevision"] = "old"
        with self.assertRaises(DecisionError):
            self.store.append(payload)

    def test_idempotent_retry_and_collision(self) -> None:
        payload = self.payload()
        self.store.append(payload)
        self.assertTrue(self.store.append(payload)["idempotent"])
        changed = dict(payload, note="different")
        with self.assertRaises(DecisionError):
            self.store.append(changed)

    def test_undo_restores_previous_state(self) -> None:
        first = self.store.append(self.payload())
        undo = self.payload(action="undo", mutation="2234567890abcdef", expected=1)
        undo["undoOf"] = first["receipt"]
        self.store.append(undo)
        self.assertNotIn("D01", self.store.state(False)["targets"])

    def test_wrong_artifact_revision_is_rejected(self) -> None:
        artifact_path = self.root / "type-review.json"
        artifact = json.loads(artifact_path.read_text())
        artifact["status"] = "tampered"
        artifact_path.write_bytes(canonical(artifact))
        with self.assertRaises(ValueError):
            DecisionStore(self.root, self.root / "other-feedback")

    def test_preview_cannot_escape_campaign(self) -> None:
        artifact_path = self.root / "type-review.json"
        artifact = json.loads(artifact_path.read_text())
        outside = self.root.parent / "outside.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\n")
        artifact["types"][0]["preview"] = "../outside.png"
        artifact.pop("revision")
        artifact["revision"] = __import__("hashlib").sha256(canonical(artifact)).hexdigest()
        artifact_path.write_bytes(canonical(artifact))
        with self.assertRaises(ValueError):
            DecisionStore(self.root, self.root / "other-feedback")
        outside.unlink()


if __name__ == "__main__":
    unittest.main()
