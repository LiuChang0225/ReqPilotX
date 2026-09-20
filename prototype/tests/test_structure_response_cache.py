from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Response

from app.application.extraction import (
    document_jobs,
    review_support,
    runtime_read_models,
    structure_workspace,
)
from app.application.extraction.structure_response_cache import (
    StructureResponseCacheEntry,
    build_complete_structure_response_identity,
    build_static_structure_response_identity,
    cache_structure_response,
    canonical_json_bytes,
    canonical_json_sha256,
    clear_structure_response_cache,
    get_structure_response_cache_candidate,
    if_none_match_matches,
    strong_etag,
)
from app.infrastructure.runtime_settings import DatabaseBackend
from app.ports.job_queue import JobStatus


class StructureResponseCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_structure_response_cache()

    def tearDown(self) -> None:
        clear_structure_response_cache()

    def _static_identity(self, **updates: object) -> str:
        values = {
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": "a" * 64,
            "artifact_receipt": {
                "job_id": "job-1",
                "job_generation": 1,
                "artifact_sha256": "b" * 64,
                "artifact_size": 17,
                "storage_key": "runtime/result.json",
                "latest_attempt": {
                    "job_id": "job-1",
                    "job_generation": 1,
                    "job_status": "SUCCEEDED",
                },
            },
            "document_identity": {
                "file_name": "source.pdf",
                "preview_generation_id": "preview-1",
                "page_count": 4,
            },
            "source_ir_adapter_version": "pdf-1",
            "structure_resolver_version": "resolver-1",
            "structure_presentation_schema_version": "presentation-v6",
            "workbench_schema_version": "workbench-1",
            "requirement_tree_projection_runtime_version": "tree-v11",
            "requirement_mode": "WORKING",
            "requirement_set_id": "",
        }
        values.update(updates)
        return build_static_structure_response_identity(**values)

    def test_static_identity_binds_source_receipt_runtime_mode_and_document(
        self,
    ) -> None:
        baseline = self._static_identity()
        variants = (
            self._static_identity(source_sha256="c" * 64),
            self._static_identity(
                artifact_receipt={
                    "job_id": "job-2",
                    "job_generation": 2,
                    "artifact_sha256": "b" * 64,
                    "artifact_size": 17,
                    "storage_key": "runtime/result.json",
                }
            ),
            self._static_identity(
                artifact_receipt={
                    "job_id": "job-1",
                    "job_generation": 1,
                    "artifact_sha256": "b" * 64,
                    "artifact_size": 17,
                    "storage_key": "runtime/result.json",
                    "latest_attempt": {
                        "job_id": "job-2",
                        "job_generation": 2,
                        "job_status": "FAILED",
                    },
                }
            ),
            self._static_identity(structure_resolver_version="resolver-2"),
            self._static_identity(source_ir_adapter_version="pdf-2"),
            self._static_identity(
                structure_presentation_schema_version="presentation-v5"
            ),
            self._static_identity(
                requirement_tree_projection_runtime_version="tree-v10"
            ),
            self._static_identity(requirement_mode="REQUIREMENT_SET"),
            self._static_identity(requirement_set_id="set-1"),
            self._static_identity(
                document_identity={
                    "file_name": "renamed.pdf",
                    "preview_generation_id": "preview-1",
                    "page_count": 4,
                }
            ),
        )
        self.assertTrue(all(value != baseline for value in variants))

    def test_static_identity_invalidates_when_persisted_sidecar_artifact_changes(
        self,
    ) -> None:
        empty_artifact_sha256 = canonical_json_sha256(
            {
                "structure_presentation_index": {
                    "bilingual_projection": {
                        "schema_version": "structure-bilingual-projection/v1",
                        "document_language_policy": None,
                        "counterpart_groups": [],
                        "mismatch_signals": [],
                    }
                }
            }
        )
        bilingual_artifact_sha256 = canonical_json_sha256(
            {
                "structure_presentation_index": {
                    "bilingual_projection": {
                        "schema_version": "structure-bilingual-projection/v1",
                        "document_language_policy": {
                            "document_language_policy_id": (
                                "document-language-policy-" + "1" * 32
                            )
                        },
                        "counterpart_groups": [
                            {"counterpart_group_id": "bilingual-group-" + "2" * 32}
                        ],
                        "mismatch_signals": [],
                    }
                }
            }
        )

        def identity(artifact_sha256: str) -> str:
            return self._static_identity(
                artifact_receipt={
                    "job_id": "job-1",
                    "job_generation": 1,
                    "artifact_sha256": artifact_sha256,
                    "artifact_size": 17,
                    "storage_key": "runtime/result.json",
                    "latest_attempt": {
                        "job_id": "job-1",
                        "job_generation": 1,
                        "job_status": "SUCCEEDED",
                    },
                }
            )

        self.assertNotEqual(
            identity(empty_artifact_sha256),
            identity(bilingual_artifact_sha256),
        )

    def test_complete_identity_binds_structure_job_table_and_enrichment(self) -> None:
        values = {
            "static_identity_sha256": self._static_identity(),
            "base_structure_identity": {
                "snapshot_id": "base-1",
                "canonical_sha256": "d" * 64,
            },
            "effective_structure_identity": {
                "snapshot_id": "effective-1",
                "tree_sha256": "e" * 64,
                "revision_chain_sha256": "f" * 64,
                "revision_number": 2,
            },
            "revision_chain_sha256": "1" * 64,
            "job_identity": {"job_id": "job-1", "status": "COMPLETED"},
            "payload_runtime_identity": {
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "structure": {"version": "resolver-1"},
            },
            "table_projection_revision_sha256": "2" * 64,
            "enrichment_state_sha256": "3" * 64,
        }
        baseline = build_complete_structure_response_identity(**values)
        for key, replacement in (
            ("base_structure_identity", {"snapshot_id": "base-2"}),
            (
                "effective_structure_identity",
                {"snapshot_id": "effective-2", "revision_number": 3},
            ),
            ("revision_chain_sha256", "4" * 64),
            ("job_identity", {"job_id": "job-2", "status": "COMPLETED"}),
            (
                "payload_runtime_identity",
                {
                    "mode": "LOCAL_STRUCTURE_PROJECTION",
                    "structure": {"version": "resolver-2"},
                },
            ),
            ("table_projection_revision_sha256", "5" * 64),
            ("enrichment_state_sha256", "6" * 64),
        ):
            variant = dict(values)
            variant[key] = replacement
            self.assertNotEqual(
                build_complete_structure_response_identity(**variant),
                baseline,
                key,
            )

    def test_cache_keeps_immutable_bytes_and_strong_validator(self) -> None:
        content = canonical_json_bytes({"text": "中文", "count": 2})
        etag = strong_etag(content)
        static_identity = self._static_identity()
        entry = StructureResponseCacheEntry(
            static_identity_sha256=static_identity,
            complete_identity_sha256="7" * 64,
            content=content,
            etag=etag,
            effective_structure_snapshot_id="effective-1",
            review_session_id="session-1",
            table_projection_revision_sha256="8" * 64,
            enrichment_state_sha256="9" * 64,
        )
        self.assertIs(cache_structure_response(entry), entry)
        self.assertIs(get_structure_response_cache_candidate(static_identity), entry)
        self.assertEqual(
            etag,
            f'"{hashlib.sha256(content).hexdigest()}"',
        )
        self.assertTrue(if_none_match_matches(etag, etag))
        self.assertTrue(if_none_match_matches(f"W/{etag}", etag))
        self.assertTrue(if_none_match_matches(f'"other", {etag}', etag))
        self.assertFalse(if_none_match_matches('"other"', etag))

    def test_durable_receipt_identity_does_not_load_artifact_bytes(self) -> None:
        now = datetime.now(timezone.utc)
        durable = SimpleNamespace(
            job_id="job-1",
            generation=4,
            updated_at=now,
            status=JobStatus.SUCCEEDED,
            payload={
                "project_id": "project-1",
                "source_sha256": "a" * 64,
            },
            result={
                "schema_version": "document-extraction-result-artifact/v1",
                "storage_key": "runtime/result.json",
                "sha256": "b" * 64,
                "size": 1024,
                "media_type": "application/json",
            },
        )
        queue = SimpleNamespace(get_latest=lambda **_kwargs: durable)
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)
        with (
            patch("app.application.runtime.RUNTIME_SETTINGS", settings),
            patch("app.application.runtime._durable_jobs", return_value=queue),
        ):
            receipt = (
                document_jobs._document_extraction_result_artifact_receipt_identity(
                    "project-1",
                    "document-1",
                    source_sha256="a" * 64,
                )
            )
        self.assertEqual(receipt["job_id"], "job-1")
        self.assertEqual(receipt["job_generation"], 4)
        self.assertEqual(receipt["artifact_sha256"], "b" * 64)
        self.assertEqual(
            canonical_json_sha256(receipt),
            canonical_json_sha256(dict(receipt)),
        )

    def test_failed_retry_keeps_previous_successful_artifact_receipt(self) -> None:
        now = datetime.now(timezone.utc)
        failed = SimpleNamespace(
            job_id="job-failed",
            generation=8,
            updated_at=now,
            status=JobStatus.FAILED,
        )
        succeeded = SimpleNamespace(
            job_id="job-succeeded",
            generation=7,
            updated_at=now,
            status=JobStatus.SUCCEEDED,
            payload={
                "project_id": "project-1",
                "source_sha256": "a" * 64,
            },
            result={
                "schema_version": "document-extraction-result-artifact/v1",
                "storage_key": "runtime/previous-result.json",
                "sha256": "b" * 64,
                "size": 2048,
                "media_type": "application/json",
            },
        )
        queue = SimpleNamespace(
            get_latest=lambda **_kwargs: failed,
            get_latest_succeeded=lambda **_kwargs: succeeded,
        )
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)

        with (
            patch("app.application.runtime.RUNTIME_SETTINGS", settings),
            patch("app.application.runtime._durable_jobs", return_value=queue),
        ):
            receipt = (
                document_jobs._document_extraction_result_artifact_receipt_identity(
                    "project-1",
                    "document-1",
                    source_sha256="a" * 64,
                )
            )

        self.assertEqual(receipt["job_id"], "job-succeeded")
        self.assertEqual(receipt["artifact_sha256"], "b" * 64)
        self.assertEqual(
            receipt["latest_attempt"],
            {
                "job_id": "job-failed",
                "job_generation": 8,
                "job_status": "FAILED",
                "job_updated_at_utc": now.isoformat(),
            },
        )

    def test_active_retry_does_not_reuse_previous_artifact_receipt(self) -> None:
        def unexpected_success_lookup(**_kwargs):
            self.fail("An active retry must not query an older successful receipt.")

        queue = SimpleNamespace(
            get_latest=lambda **_kwargs: SimpleNamespace(status=JobStatus.RUNNING),
            get_latest_succeeded=unexpected_success_lookup,
        )
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)

        with (
            patch("app.application.runtime.RUNTIME_SETTINGS", settings),
            patch("app.application.runtime._durable_jobs", return_value=queue),
        ):
            receipt = (
                document_jobs._document_extraction_result_artifact_receipt_identity(
                    "project-1",
                    "document-1",
                    source_sha256="a" * 64,
                )
            )

        self.assertEqual(receipt, {})

    def test_previous_receipt_remains_source_and_project_bound(self) -> None:
        now = datetime.now(timezone.utc)
        succeeded = SimpleNamespace(
            job_id="job-other-source",
            generation=2,
            updated_at=now,
            status=JobStatus.SUCCEEDED,
            payload={
                "project_id": "project-other",
                "source_sha256": "c" * 64,
            },
            result={
                "schema_version": "document-extraction-result-artifact/v1",
                "storage_key": "runtime/other-result.json",
                "sha256": "d" * 64,
                "size": 2048,
                "media_type": "application/json",
            },
        )
        queue = SimpleNamespace(
            get_latest=lambda **_kwargs: SimpleNamespace(status=JobStatus.CANCELLED),
            get_latest_succeeded=lambda **_kwargs: succeeded,
        )
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)

        with (
            patch("app.application.runtime.RUNTIME_SETTINGS", settings),
            patch("app.application.runtime._durable_jobs", return_value=queue),
        ):
            receipt = (
                document_jobs._document_extraction_result_artifact_receipt_identity(
                    "project-1",
                    "document-1",
                    source_sha256="a" * 64,
                )
            )

        self.assertEqual(receipt, {})

    def test_latest_successful_job_restores_only_the_bound_snapshot(self) -> None:
        durable = SimpleNamespace(status=JobStatus.SUCCEEDED)
        queue = SimpleNamespace(
            get_latest_succeeded=lambda **_kwargs: durable,
        )
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)
        snapshot = {
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": "a" * 64,
            "status": "COMPLETED",
            "result": {"structure_agent": {}},
        }

        with (
            patch("app.application.runtime.RUNTIME_SETTINGS", settings),
            patch("app.application.runtime._durable_jobs", return_value=queue),
            patch.object(
                document_jobs,
                "_llm_extraction_snapshot_from_durable",
                return_value=snapshot,
            ),
        ):
            restored = document_jobs._get_latest_successful_llm_extraction_job(
                "project-1",
                "document-1",
                source_sha256="a" * 64,
            )
            rejected = document_jobs._get_latest_successful_llm_extraction_job(
                "project-other",
                "document-1",
                source_sha256="a" * 64,
            )

        self.assertEqual(restored, snapshot)
        self.assertIsNone(rejected)

    def test_structure_http_read_hits_bytes_cache_then_returns_304(self) -> None:
        source_sha256 = "a" * 64
        document = {
            "id": "document-1",
            "source_sha256": source_sha256,
            "file_name": "source.pdf",
            "preview_generation_id": "preview-1",
            "page_count": 3,
        }
        receipt = {
            "schema_version": "document-extraction-result-artifact/v1",
            "job_id": "job-1",
            "job_generation": 1,
            "job_updated_at_utc": "2026-07-31T00:00:00+00:00",
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": source_sha256,
            "storage_key": "runtime/result.json",
            "artifact_sha256": "b" * 64,
            "artifact_size": 1024,
            "media_type": "application/json",
        }
        agent = {
            "editable": True,
            "base_structure_identity": {
                "snapshot_id": "base-1",
                "canonical_sha256": "c" * 64,
            },
            "effective_structure_identity": {
                "snapshot_id": "effective-1",
                "tree_sha256": "d" * 64,
                "revision_chain_sha256": "e" * 64,
                "revision_number": 0,
            },
            "revision_chain": [],
        }
        payload = {
            "source_sha256": source_sha256,
            "structure_agent": agent,
        }
        job = {
            "job_id": "job-1",
            "status": "COMPLETED",
            "stage": "completed",
            "source_sha256": source_sha256,
        }
        workbench = {
            "schema_version": "structure-workbench/v0",
            "requirement_tree": {"mode": "WORKING", "nodes": []},
        }
        table_identity = canonical_json_sha256([])
        request = SimpleNamespace(headers={})
        conditional_request: SimpleNamespace

        with (
            patch.object(
                review_support,
                "_require_project_document",
                return_value=document,
            ),
            patch.object(
                document_jobs,
                "_document_extraction_result_artifact_receipt_identity",
                return_value=receipt,
            ),
            patch.object(
                document_jobs,
                "_llm_extraction_result_matches_runtime",
                return_value=True,
            ),
            patch.object(
                structure_workspace,
                "_structure_agent_job_payload",
                return_value=(job, payload, agent),
            ) as payload_loader,
            patch.object(
                structure_workspace,
                "_structure_table_projection_revision_state",
                return_value=([], table_identity),
            ),
            patch.object(
                structure_workspace,
                "_structure_workbench_enrichment_state",
                return_value={},
            ),
            patch.object(
                structure_workspace,
                "_serialize_structure_workbench_for_api",
                return_value=workbench,
            ),
            patch.object(
                runtime_read_models,
                "cache_document_extraction_runtime_read_model",
            ),
        ):
            first = structure_workspace.get_document_structure_agent(
                "project-1",
                "document-1",
                Response(),
                request=request,
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.media_type, "application/json")
            self.assertTrue(first.headers["etag"].startswith('"'))

            second = structure_workspace.get_document_structure_agent(
                "project-1",
                "document-1",
                Response(),
                request=request,
            )
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.content, first.content)
            self.assertEqual(payload_loader.call_count, 1)

            conditional_request = SimpleNamespace(
                headers={"if-none-match": first.headers["etag"]}
            )
            third = structure_workspace.get_document_structure_agent(
                "project-1",
                "document-1",
                Response(),
                request=conditional_request,
            )
            self.assertEqual(third.status_code, 304)
            self.assertEqual(third.content, b"")
            self.assertEqual(payload_loader.call_count, 1)


if __name__ == "__main__":
    unittest.main()
