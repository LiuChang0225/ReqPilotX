from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import sqlite3
import unittest
from datetime import datetime, timezone

from app.adapters.persistence.sqlite_semantic_runs import (
    SQLiteSemanticProviderRunRepository,
)
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.ports.semantic_run_repository import SemanticProviderRunConflictError


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class SemanticRunRepositoryTests(unittest.TestCase):
    def test_append_is_idempotent_and_payload_is_text_free(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSemanticProviderRunRepository(connection)
        run = _run()

        self.assertTrue(repository.append(run))
        self.assertFalse(repository.append(run))
        self.assertEqual(repository.get(run.provider_run_id), run)
        payload = connection.execute(
            "SELECT payload_json FROM semantic_provider_runs"
        ).fetchone()[0]
        self.assertNotIn("customer text", payload)
        columns = {
            item[1]
            for item in connection.execute("PRAGMA table_info(semantic_provider_runs)")
        }
        self.assertNotIn("prompt", columns)
        self.assertNotIn("response", columns)

    def test_same_run_identity_with_different_metadata_is_rejected(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSemanticProviderRunRepository(connection)
        repository.append(_run())

        with self.assertRaises(SemanticProviderRunConflictError):
            repository.append(_run(latency_ms=1))

    def test_stored_payload_hash_is_verified_on_read(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSemanticProviderRunRepository(connection)
        run = _run()
        repository.append(run)
        connection.execute(
            "UPDATE semantic_provider_runs SET payload_json = ? WHERE provider_run_id = ?",
            ("{}", run.provider_run_id),
        )

        with self.assertRaisesRegex(RuntimeError, "payload hash mismatch"):
            repository.get(run.provider_run_id)

    def test_skipped_run_round_trips_with_zero_remote_requests(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSemanticProviderRunRepository(connection)
        run = _skipped_run()

        self.assertTrue(repository.append(run))
        self.assertEqual(repository.get(run.provider_run_id), run)

    def test_shared_request_run_round_trips_without_double_counting_calls(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSemanticProviderRunRepository(connection)
        source = _run()
        shared = _shared_run(source.provider_run_id)

        self.assertTrue(repository.append(source))
        self.assertTrue(repository.append(shared))
        self.assertEqual(repository.get(shared.provider_run_id), shared)
        self.assertEqual(shared.request_count, 0)
        self.assertEqual(
            shared.shared_request_provider_run_id,
            source.provider_run_id,
        )

    def test_legacy_v1_payload_is_normalized_on_read(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = SQLiteSemanticProviderRunRepository(connection)
        run = _run()
        repository.append(run)
        payload = run.model_dump(mode="json")
        payload["schema_version"] = "semantic-provider-run/v1"
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        connection.execute(
            """
            UPDATE semantic_provider_runs
            SET payload_sha256 = ?, payload_json = ?
            WHERE provider_run_id = ?
            """,
            (payload_sha256, payload_json, run.provider_run_id),
        )

        self.assertEqual(repository.get(run.provider_run_id), run)
        self.assertFalse(repository.append(run))
        stored_payload = connection.execute(
            "SELECT payload_json FROM semantic_provider_runs WHERE provider_run_id = ?",
            (run.provider_run_id,),
        ).fetchone()[0]
        self.assertEqual(stored_payload, payload_json)

    def test_skipped_run_rejects_remote_response_retry_or_token_metadata(self) -> None:
        invalid_values = (
            {"request_count": 1},
            {"retry_count": 1},
            {"response_sha256": "2" * 64},
            {"input_tokens": 1},
            {"output_tokens": 1},
            {"latency_ms": 1},
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(ValueError):
                _skipped_run(**values)


def _run(*, latency_ms: int = 0) -> SemanticProviderRunRecord:
    request_sha = "1" * 64
    response_sha = "2" * 64
    run_id = stable_semantic_provider_run_id(
        operation="classify_nodes",
        provider_id="synthetic-provider",
        model_id="qwen3.7-plus",
        request_sha256=request_sha,
        response_sha256=response_sha,
        status=SemanticProviderRunStatus.SUCCESS,
        started_at=NOW,
    )
    return SemanticProviderRunRecord(
        provider_run_id=run_id,
        operation="classify_nodes",
        provider_id="synthetic-provider",
        provider_version="1.0.0",
        model_id="qwen3.7-plus",
        prompt_version="prompt/v1",
        response_schema_version="schema/v1",
        request_sha256=request_sha,
        response_sha256=response_sha,
        status=SemanticProviderRunStatus.SUCCESS,
        started_at=NOW,
        finished_at=NOW,
        latency_ms=latency_ms,
        request_count=1,
        retry_count=0,
    )


def _skipped_run(**overrides: object) -> SemanticProviderRunRecord:
    request_sha = "3" * 64
    response_sha = overrides.get("response_sha256")
    run_id = stable_semantic_provider_run_id(
        operation="assemble_requirements",
        provider_id="synthetic-provider",
        model_id="qwen3.7-plus",
        request_sha256=request_sha,
        response_sha256=response_sha if isinstance(response_sha, str) else None,
        status=SemanticProviderRunStatus.SKIPPED,
        started_at=NOW,
    )
    values: dict[str, object] = {
        "provider_run_id": run_id,
        "operation": "assemble_requirements",
        "provider_id": "synthetic-provider",
        "provider_version": "1.0.0",
        "model_id": "qwen3.7-plus",
        "prompt_version": "prompt/v1",
        "response_schema_version": "schema/v1",
        "request_sha256": request_sha,
        "response_sha256": None,
        "status": SemanticProviderRunStatus.SKIPPED,
        "started_at": NOW,
        "finished_at": NOW,
        "latency_ms": 0,
        "request_count": 0,
        "retry_count": 0,
        "error_code": "upstream_semantic_unresolved",
    }
    values.update(overrides)
    return SemanticProviderRunRecord.model_validate(values)


def _shared_run(shared_id: str) -> SemanticProviderRunRecord:
    request_sha = "4" * 64
    response_sha = "5" * 64
    run_id = stable_semantic_provider_run_id(
        operation="assemble_requirements",
        provider_id="synthetic-provider",
        model_id="qwen3.7-plus",
        request_sha256=request_sha,
        response_sha256=response_sha,
        status=SemanticProviderRunStatus.SUCCESS,
        started_at=NOW,
        shared_request_provider_run_id=shared_id,
    )
    return SemanticProviderRunRecord(
        provider_run_id=run_id,
        operation="assemble_requirements",
        provider_id="synthetic-provider",
        provider_version="1.0.0",
        model_id="qwen3.7-plus",
        prompt_version="joint/v1",
        response_schema_version="joint/v1",
        request_sha256=request_sha,
        response_sha256=response_sha,
        status=SemanticProviderRunStatus.SUCCESS,
        started_at=NOW,
        finished_at=NOW,
        latency_ms=0,
        request_count=0,
        retry_count=0,
        shared_request_provider_run_id=shared_id,
    )


if __name__ == "__main__":
    unittest.main()
