from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import os
import unittest
from unittest.mock import patch
from pathlib import Path

from app.adapters.agents.codex_app_server.probe import (
    build_command,
    configured_auth_store,
    ProbeError,
)
from app.adapters.persistence.postgres_jobs import PostgresJobQueue
from app.adapters.persistence.postgres_jobs import (
    _replace_succeeded_extraction_result_artifact_reference,
)
from app.worker.runtime import WorkerRuntime
from tests.test_durable_job_retry_contract import _RecordingQueue, _durable_job
from app.application.agent_workflow.source_requirement_jobs import _latest_job


class AgentWorkflowRuntimeTests(unittest.TestCase):
    def test_published_revision_wins_over_a_later_recovery_diagnostic_update(self):
        original = _durable_job(payload={})
        v3 = replace(
            original, result={"baseline_revision_artifact": {"baseline_revision": 3}}
        )
        v2 = replace(
            original,
            updated_at=original.updated_at + timedelta(seconds=20),
            result={"baseline_revision_artifact": {"baseline_revision": 2}},
        )
        self.assertIs(_latest_job(v2, v3, prefer_baseline_revision=True), v3)
        self.assertIs(_latest_job(v2, v3), v2)

    def test_other_durable_results_do_not_use_document_extraction_artifact_cas(self):
        _replace_succeeded_extraction_result_artifact_reference(
            None,
            {"kind": "agent.document-map"},
            {},
            {"status": "COMPLETE"},
            "observability-receipt",
        )

    def test_explicit_authorized_file_store_does_not_force_keyring(self):
        with patch.dict(os.environ, {"REQPILOT_CODEX_AUTH_STORE": "file"}):
            command = build_command(Path("/opt/codex"))
            self.assertIn('cli_auth_credentials_store="file"', command)
            self.assertNotIn('cli_auth_credentials_store="keyring"', command)
        with patch.dict(
            os.environ, {"REQPILOT_CODEX_AUTH_STORE": 'file"; forged=true'}
        ):
            with self.assertRaises(ProbeError):
                configured_auth_store()

    def test_worker_passes_exact_job_identity_to_atomic_claim(self):
        identity = "job-" + "9" * 32
        queue = _RecordingQueue(replace(_durable_job(payload={}), job_id=identity))
        original = queue.claim
        calls = []

        def claim(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        queue.claim = claim
        runtime = WorkerRuntime(
            queue=queue,
            handlers={"document.extract": lambda *_: {"status": "COMPLETE"}},
            job_id=identity,
        )
        self.assertTrue(runtime.run_once())
        self.assertEqual(calls[0]["job_id"], identity)
        self.assertEqual(queue.succeeded["job_id"], identity)

    def test_targeted_queue_claim_and_expiry_reap_are_both_scoped(self):
        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, statement, params):
                self.calls.append((statement, params))
                return self

            def fetchone(self):
                return None

        class Database:
            def __init__(self):
                self.conn = Connection()

            @contextmanager
            def connection(self):
                yield self.conn

        database = Database()
        identity = "job-" + "8" * 32
        self.assertIsNone(
            PostgresJobQueue(database).claim(
                worker_id="test-worker", lease_seconds=120, job_id=identity
            )
        )
        reap, _, claim = database.conn.calls
        self.assertEqual(reap[1][:2], (identity, identity))
        self.assertIn("queued.id = %s", claim[0])
        self.assertEqual(claim[1][:2], (identity, identity))
        self.assertIn("queued.kind = 'agent.document-map'", claim[0])
        self.assertIn("queued.kind <> 'agent.source-item.images'", claim[0])
        self.assertIn("dependency.status <> 'SUCCEEDED'", claim[0])
        self.assertIn("dependency.id IS NULL", claim[0])
        self.assertIn("jsonb_typeof(prerequisite.job_id) <> 'string'", claim[0])

    def test_new_workbench_lane_filters_claim_and_expiry_without_touching_legacy(self):
        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, sql, args):
                self.calls.append((sql, args))
                return self

            def fetchone(self):
                return None

        class Database:
            def __init__(self):
                self.conn = Connection()

            @contextmanager
            def connection(self):
                yield self.conn

        database = Database()
        marker = "source-item-workbench-v1"
        PostgresJobQueue(database).claim(
            worker_id="new-workbench", lease_seconds=120, required_entrypoint=marker
        )
        self.assertEqual(database.conn.calls[0][1][-2:], (marker, marker))
        self.assertIn("payload->>'application_entrypoint'", database.conn.calls[0][0])
        self.assertEqual(database.conn.calls[-1][1][2:4], (marker, marker))


if __name__ == "__main__":
    unittest.main()
