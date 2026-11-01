"""Opt-in persisted continuation proof in a fresh Unix-socket PostgreSQL DB."""
from __future__ import annotations

from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from reqpilotx_server.adapters.persistence.postgres_jobs import PostgresJobQueue
from reqpilotx_server.adapters.storage.local_object_store import LocalObjectStore
from reqpilotx_server.infrastructure.postgres import PostgresDatabase
from reqpilotx_server.application.agent_workflow.provider_continuation import (
    build_continuation_handoff, saved_predecessor,
)
from reqpilotx_server.application.agent_workflow.source_item_agent_jobs import _write_private
from . import test_provider_continuation as fixtures
from .test_provider_tool_loop import FakeCompletions, tool_choice, final_choice


@unittest.skipUnless(os.environ.get("REQPILOT_RUN_CONTINUATION_POSTGRES_E2E") == "1",
                     "Opt-in isolated PostgreSQL continuation proof")
class ProviderContinuationPostgresTests(unittest.TestCase):
    def test_saved_predecessor_and_interrupted_chain_survive_pool_reopen(self):
        # No configured app/database fallback: only an explicit local socket.
        params = conninfo_to_dict(os.environ["REQPILOT_POSTGRES_INTEGRATION_URL"])
        if not params.get("host", "").startswith("/") or not Path(params["host"]).is_dir():
            raise ValueError("Continuation proof requires an explicit Unix socket directory")
        name = "reqpilotx_continuation_" + uuid4().hex[:20]
        admin_dsn = make_conninfo(**{**params, "dbname": "postgres"})
        dsn = make_conninfo(**{**params, "dbname": name})
        proof = {"schema_version": "reqpilotx-continuation-postgres-proof/v1", "test_database": name,
                 "database_deleted": False, "production_database_migrated": False,
                 "real_model_called": False, "status": "STARTED"}
        created = False
        database = None
        fixture = fixtures.ProviderContinuationTests()
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
            database = PostgresDatabase(dsn, max_pool_size=2)
            proof["migration_count"] = len(database.apply_migrations(
                Path(__file__).resolve().parents[1] / "migrations/postgresql"))
            queue = PostgresJobQueue(database)
            fixture.setUp()
            container = SimpleNamespace(jobs=queue, object_store=LocalObjectStore(fixture.root / "objects"))
            meta = fixture.meta
            now = datetime.now(UTC)
            with database.connection() as connection:
                connection.execute("INSERT INTO projects(id,name,customer_name,created_at) VALUES (%s,%s,%s,%s)",
                                   (meta["project_id"], "Synthetic continuation", "Synthetic", now))
                connection.execute("INSERT INTO stored_objects(storage_key,sha256,size_bytes,media_type,object_kind) VALUES (%s,%s,%s,%s,%s)",
                                   ("synthetic/source.pdf", meta["source_sha256"], fixture.source.stat().st_size,
                                    "application/pdf", "sor-source"))
                connection.execute("""INSERT INTO sor_documents(id,project_id,file_name,file_type,storage_key,
                    extracted_text,source_sha256,source_size,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (meta["document_id"], meta["project_id"], "source.pdf", "pdf", "synthetic/source.pdf", "",
                     meta["source_sha256"], fixture.source.stat().st_size, now, now))
            payload = {**fixture.binding, "application_entrypoint": "source-item-workbench-v1"}

            def enqueue(key, value):
                return container.jobs.enqueue(kind=meta["job_kind"], aggregate_type="sor-document",
                    aggregate_id=meta["document_id"], idempotency_key=key, payload=value)

            def save(job, result, *, cancelled=False):
                claimed = container.jobs.claim(worker_id="continuation-test", lease_seconds=60,
                    accepted_kinds=(meta["job_kind"],), job_id=job.job_id)
                self.assertIsNotNone(claimed)
                if cancelled:
                    self.assertEqual(container.jobs.request_cancel(job.job_id).status.value, "CANCEL_REQUESTED")
                return container.jobs.succeed(job_id=job.job_id, worker_id="continuation-test", result=result)

            def reopen():
                nonlocal database
                database.close()
                database = PostgresDatabase(dsn, max_pool_size=2)
                container.jobs = PostgresJobQueue(database)
                container.object_store = LocalObjectStore(fixture.root / "objects")

            initial = enqueue("saved-initial", payload)
            pointer = _write_private(container, project_id=meta["project_id"], document_id=meta["document_id"],
                namespace="working", stage="continuation-proof", value=fixture.graph)
            saved_result = {**fixture.saved.result, "native_source_graph_artifact": pointer,
                "source_sha256": meta["source_sha256"], "expected_no_source_graph": True,
                "document_map_artifact_sha256": fixture.graph["baseline"]["document_map_artifact_sha256"]}
            self.assertEqual(save(initial, saved_result).status.value, "SUCCEEDED")
            meta["latest_job_id"] = initial.job_id
            reopen()
            pin = saved_predecessor(container, meta)
            self.assertEqual(pin, {"continuation_predecessor_job_id": initial.job_id})
            feedback_payload = {**payload, **pin, "baseline_sha256": fixture.baseline.artifact_sha256}
            feedback = enqueue("feedback", feedback_payload)
            self.assertEqual(enqueue("feedback", feedback_payload).job_id, feedback.job_id)

            def handoff(job):
                return build_continuation_handoff(container, fixture.root, meta, job,
                    verify_review=lambda path: fixtures.review_tools.verify_review(fixtures.args(review_dir=path)))

            original_snapshot = (fixture.root / "runtime" / fixture.completed.receipt["run_state_reference"]).read_bytes()
            packet = handoff(feedback)
            reopen()
            self.assertEqual(handoff(container.jobs.get(feedback.job_id)), packet)
            response = tool_choice("postgres-pending")
            response["choices"][0]["message"]["reasoning_content"] = "Synthetic active reasoning"
            cancel = {"value": False}
            fake = FakeCompletions([response], on_call=lambda: cancel.update(value=True))
            fixture.driver.client_factory = fake.client
            interrupted = fixture.driver.run(session_dir=fixture.root / "runtime", goal="Persisted feedback",
                skill_paths={"reqpilot": fixture.skill}, tool_host=fixture.host, binding=fixture.binding,
                continuation_handoff=packet, cancellation_requested=lambda: cancel["value"])
            self.assertEqual(interrupted.status, "INTERRUPTED")
            self.assertEqual(save(feedback, {"application_saved": False,
                "runtime_receipt": interrupted.receipt}, cancelled=True).status.value, "CANCELLED")
            meta["latest_job_id"] = feedback.job_id
            reopen()
            self.assertEqual(saved_predecessor(container, meta), {})
            resume = enqueue("resume", payload)
            self.assertIsNone(handoff(resume))  # Existing context stays; no new terminal projection.
            original = fixture.source.read_bytes()
            fixture.source.write_bytes(b"changed synthetic source")
            with self.assertRaises(ValueError):
                handoff(resume)
            fixture.source.write_bytes(original)
            resumed, fake = fixture.run_model([final_choice()], goal="Persisted feedback")
            self.assertEqual(resumed.status, "COMPLETED")
            request = fake.calls[0]["messages"]
            self.assertTrue(fixtures._paired_history(request))
            self.assertEqual(request[-2]["reasoning_content"], "Synthetic active reasoning")
            self.assertEqual(request[-1]["tool_call_id"], "postgres-pending")
            self.assertEqual((fixture.root / "runtime" / fixture.completed.receipt["run_state_reference"]).read_bytes(), original_snapshot)
            self.assertEqual(container.jobs.get(initial.job_id).result, saved_result)
            self.assertEqual(save(resume, {"application_saved": False,
                "runtime_receipt": resumed.receipt}).status.value, "SUCCEEDED")
            proof.update(status="PASS", durable_predecessor_pinned=True, idempotent_enqueue=True,
                         object_pointer_integrity_verified=True, pool_reopened=True,
                         durable_cancellation_persisted=True, markerless_active_recovery=True, source_drift_rejected=True,
                         paired_tool_id_and_qwen_reasoning_preserved=True, original_audit_unchanged=True,
                         saved_predecessor_unchanged=True)
        finally:
            if database is not None:
                database.close()
            fixture.doCleanups()
            if created:
                with psycopg.connect(admin_dsn, autocommit=True) as admin:
                    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                    proof["database_deleted"] = admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone() is None
            output = os.environ.get("REQPILOT_CONTINUATION_POSTGRES_RECEIPT")
            if output:
                Path(output).write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
        self.assertTrue(proof["database_deleted"])
