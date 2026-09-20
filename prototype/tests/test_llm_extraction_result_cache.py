from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.llm_extraction_result_cache import (
    LlmExtractionJobCheckpointCache,
    LlmExtractionResultCache,
    LlmExtractionResultCachePathError,
)

PROJECT_ID = "project-a"
DOCUMENT_ID = "document-a"
SOURCE_SHA256 = hashlib.sha256(b"source-a").hexdigest()


class LlmExtractionResultCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temporary.name) / "storage"
        self.cache = LlmExtractionResultCache(self.storage_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_trip_is_bound_to_project_document_and_source(self) -> None:
        payload = {
            "mode": "EDITABLE_LLM_REVIEW",
            "nodes": [{"id": "r-1", "text": "制动系统应满足响应时间要求。"}],
        }

        path = self.cache.save(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            payload=payload,
        )

        self.assertEqual(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            ),
            payload,
        )
        self.assertEqual(path, self.cache.cache_path(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
        ))
        self.assertTrue(path.resolve().is_relative_to(self.storage_root.resolve()))
        envelope = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            envelope["binding"],
            {
                "project_id": PROJECT_ID,
                "document_id": DOCUMENT_ID,
                "source_sha256": SOURCE_SHA256,
            },
        )
        canonical_payload = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            envelope["payload_sha256"],
            hashlib.sha256(canonical_payload).hexdigest(),
        )

    def test_source_versions_and_documents_do_not_alias(self) -> None:
        other_source = hashlib.sha256(b"source-b").hexdigest()
        self.cache.save(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            payload={"version": "a"},
        )
        self.cache.save(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=other_source,
            payload={"version": "b"},
        )

        self.assertEqual(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            ),
            {"version": "a"},
        )
        self.assertEqual(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=other_source,
            ),
            {"version": "b"},
        )
        self.assertIsNone(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id="document-b",
                source_sha256=SOURCE_SHA256,
            )
        )

    def test_job_checkpoint_namespace_does_not_alias_completed_results(
        self,
    ) -> None:
        checkpoint_cache = LlmExtractionJobCheckpointCache(self.storage_root)
        self.cache.save(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            payload={"kind": "completed"},
        )
        checkpoint_cache.save(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            payload={"kind": "checkpoint"},
        )

        self.assertNotEqual(
            self.cache.cache_path(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            ),
            checkpoint_cache.cache_path(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            ),
        )
        self.assertTrue(
            checkpoint_cache.purge_document(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
            )
        )
        self.assertEqual(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            ),
            {"kind": "completed"},
        )

    def test_corrupt_payload_or_binding_fails_closed(self) -> None:
        path = self.cache.save(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            payload={"nodes": [{"id": "r-1"}]},
        )
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"]["nodes"][0]["id"] = "tampered"
        path.write_text(json.dumps(envelope), encoding="utf-8")

        self.assertIsNone(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            )
        )

        envelope["binding"]["document_id"] = "document-b"
        envelope["payload_sha256"] = hashlib.sha256(
            json.dumps(
                envelope["payload"],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        path.write_text(json.dumps(envelope), encoding="utf-8")
        self.assertIsNone(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            )
        )

        path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            )
        )

    def test_failed_atomic_replace_keeps_previous_payload_and_cleans_temp(self) -> None:
        path = self.cache.save(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            payload={"revision": 1},
        )

        with patch(
            "app.services.llm_extraction_result_cache.os.replace",
            side_effect=OSError("synthetic replace failure"),
        ):
            with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                self.cache.save(
                    project_id=PROJECT_ID,
                    document_id=DOCUMENT_ID,
                    source_sha256=SOURCE_SHA256,
                    payload={"revision": 2},
                )

        self.assertEqual(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            ),
            {"revision": 1},
        )
        self.assertEqual(
            [child for child in path.parent.iterdir() if child.suffix == ".tmp"],
            [],
        )

    def test_hostile_identifiers_are_hashed_and_cannot_escape_storage(self) -> None:
        path = self.cache.save(
            project_id="../../outside-project",
            document_id="/tmp/outside-document",
            source_sha256=SOURCE_SHA256,
            payload={"safe": True},
        )

        self.assertTrue(path.resolve().is_relative_to(self.storage_root.resolve()))
        self.assertNotIn("outside-project", path.parts)
        self.assertNotIn("outside-document", path.parts)

    @unittest.skipIf(os.name == "nt", "symlink creation requires elevated Windows rights")
    def test_symlinked_document_directory_is_rejected_without_outside_write(
        self,
    ) -> None:
        path = self.cache.cache_path(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
        )
        path.parent.parent.mkdir(parents=True, exist_ok=True)
        outside = self.storage_root.parent / "outside"
        outside.mkdir()
        path.parent.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(LlmExtractionResultCachePathError):
            self.cache.save(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
                payload={"unsafe": True},
            )
        self.assertIsNone(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
            )
        )
        self.assertEqual(list(outside.iterdir()), [])

    def test_purge_document_removes_all_sources_and_keeps_siblings(self) -> None:
        other_source = hashlib.sha256(b"source-b").hexdigest()
        for source in (SOURCE_SHA256, other_source):
            self.cache.save(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=source,
                payload={"source": source},
            )
        self.cache.save(
            project_id=PROJECT_ID,
            document_id="document-b",
            source_sha256=SOURCE_SHA256,
            payload={"sibling": True},
        )

        self.assertTrue(
            self.cache.purge_document(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
            )
        )
        for source in (SOURCE_SHA256, other_source):
            self.assertIsNone(
                self.cache.load(
                    project_id=PROJECT_ID,
                    document_id=DOCUMENT_ID,
                    source_sha256=source,
                )
            )
        self.assertEqual(
            self.cache.load(
                project_id=PROJECT_ID,
                document_id="document-b",
                source_sha256=SOURCE_SHA256,
            ),
            {"sibling": True},
        )
        self.assertFalse(
            self.cache.purge_document(
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
            )
        )

    def test_invalid_binding_is_rejected_before_path_access(self) -> None:
        for invalid_source in ("A" * 64, "short", "../digest"):
            with self.subTest(invalid_source=invalid_source):
                with self.assertRaises(ValueError):
                    self.cache.load(
                        project_id=PROJECT_ID,
                        document_id=DOCUMENT_ID,
                        source_sha256=invalid_source,
                    )
        with self.assertRaises(ValueError):
            self.cache.save(
                project_id=" project-a",
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
                payload={},
            )


if __name__ == "__main__":
    unittest.main()
