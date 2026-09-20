"""Opt-in real concurrent publication proof in a newly created disposable DB."""

from __future__ import annotations
# ruff: noqa: E402 -- isolate application storage before importing its adapters.

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from threading import Barrier
import time
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.adapters.persistence.postgres_jobs import PostgresJobQueue
from app.adapters.storage.local_object_store import LocalObjectStore
from app.infrastructure.postgres import PostgresDatabase


def run_publication_fence_probe() -> dict:
    """Create/drop only our UUID-named local DB; never migrate the supplied DB."""
    configured = (
        os.environ.get("REQPILOT_POSTGRES_INTEGRATION_URL")
        or os.environ.get("REQPILOT_DATABASE_URL")
        or "postgresql://localhost/reqpilotx"
    )
    params = conninfo_to_dict(configured)
    host = params.get("host", "")
    if host not in {"", "localhost", "127.0.0.1", "::1"} and not host.startswith("/"):
        raise ValueError(
            "The disposable publication proof only permits a local PostgreSQL server"
        )
    database_name = "reqpilotx_loop_fence_" + uuid4().hex[:20]
    admin_dsn = make_conninfo(**{**params, "dbname": "postgres"})
    test_dsn = make_conninfo(**{**params, "dbname": database_name})
    started = time.monotonic()
    receipt = {
        "schema_version": "reqpilotx-native-publication-postgres-proof/v1",
        "test_database": database_name,
        "production_database_migrated": False,
        "semantic_validation": "NOT_CLAIMED",
        "database_deleted": False,
    }
    created = False
    database = None
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )
        created = True
        database = PostgresDatabase(test_dsn, min_pool_size=1, max_pool_size=4)
        migrations = database.apply_migrations(
            Path(__file__).resolve().parents[1] / "migrations/postgresql"
        )
        receipt["migration_count"] = len(migrations)
        queue = PostgresJobQueue(database)
        project_id, document_id = "native-fence-project", "native-fence-document"
        raw_source = (
            b"Authorized synthetic source for the PostgreSQL publication fence only."
        )
        source_sha = hashlib.sha256(raw_source).hexdigest()
        map_sha, predecessor_sha = "2" * 64, "3" * 64
        now = datetime.now(UTC)
        with database.connection() as connection:
            connection.execute(
                "INSERT INTO projects(id, name, customer_name, created_at) VALUES (%s,%s,%s,%s)",
                (project_id, "Native Loop Publication Fence", "Synthetic", now),
            )
            connection.execute(
                "INSERT INTO stored_objects(storage_key, sha256, size_bytes, media_type, object_kind) VALUES (%s,%s,%s,%s,%s)",
                (
                    "synthetic/source.pdf",
                    source_sha,
                    len(raw_source),
                    "application/pdf",
                    "sor-source",
                ),
            )
            connection.execute(
                """INSERT INTO sor_documents(id, project_id, file_name, file_type, storage_key,
                extracted_text, source_sha256, source_size, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    document_id,
                    project_id,
                    "source.pdf",
                    "pdf",
                    "synthetic/source.pdf",
                    "",
                    source_sha,
                    len(raw_source),
                    now,
                    now,
                ),
            )
        base_payload = {
            "application_entrypoint": "source-item-workbench-v1",
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": source_sha,
        }
        historical = queue.enqueue(
            kind="agent.source-item.direct-revision",
            aggregate_type="sor-document",
            aggregate_id=document_id,
            idempotency_key="historical",
            payload=base_payload,
        )
        jobs = [
            queue.enqueue(
                kind=kind,
                aggregate_type="sor-document",
                aggregate_id=document_id,
                idempotency_key="race-" + str(index),
                payload=base_payload,
            )
            for index, kind in enumerate(
                ("agent.source-item.loop", "agent.source-item.classify")
            )
        ]
        with tempfile.TemporaryDirectory(
            prefix="reqpilotx-publication-objects-"
        ) as raw:
            object_root = Path(raw)
            store = LocalObjectStore(object_root)
            artifact_bytes = {
                "synthetic/previous.json": b'{"version":"N","immutable":true}',
                "synthetic/candidate-a.json": b'{"version":"N+1-a","private":true}',
                "synthetic/candidate-b.json": b'{"version":"N+1-b","private":true}',
            }
            objects = {
                key: store.put(
                    key=key,
                    source=BytesIO(content),
                    expected_sha256=hashlib.sha256(content).hexdigest(),
                    media_type="application/json",
                )
                for key, content in artifact_bytes.items()
            }

            def pointer(key, baseline_sha, revision):
                obj = objects[key]
                return {
                    "object_key": obj.key,
                    "object_sha256": obj.sha256,
                    "size": obj.size,
                    "media_type": obj.media_type,
                    "baseline_artifact_sha256": baseline_sha,
                    "baseline_revision": revision,
                }

            previous_pointer = pointer("synthetic/previous.json", predecessor_sha, 10)
            historical_result = {
                "source_sha256": source_sha,
                "document_map_artifact_sha256": map_sha,
                "baseline_revision_artifact": previous_pointer,
            }
            with database.connection() as connection:
                connection.execute(
                    "UPDATE runtime_jobs SET status='SUCCEEDED', result=%s::jsonb WHERE id=%s",
                    (json.dumps(historical_result), historical.job_id),
                )
                for index, job in enumerate(jobs):
                    connection.execute(
                        """UPDATE runtime_jobs SET status='RUNNING', lease_owner=%s,
                        lease_expires_at=now()+interval '2 minutes', attempts=1, generation=1, started_at=now()
                        WHERE id=%s""",
                        ("fence-worker-" + str(index), job.job_id),
                    )
            results = [
                {
                    "source_sha256": source_sha,
                    "document_map_artifact_sha256": map_sha,
                    "expected_live_baseline_sha256": predecessor_sha,
                    "expected_structure_head_verified": True,
                    "expected_structure_head_revision_sha256": None,
                    "predecessor_baseline_revision_artifact": previous_pointer,
                    "baseline_revision_artifact": pointer(key, str(index + 4) * 64, 11),
                }
                for index, key in enumerate(
                    ("synthetic/candidate-a.json", "synthetic/candidate-b.json")
                )
            ]
            barrier = Barrier(2)

            def publish(index):
                barrier.wait(timeout=10)
                begin = time.monotonic()
                outcome = queue.succeed(
                    job_id=jobs[index].job_id,
                    worker_id="fence-worker-" + str(index),
                    result=results[index],
                )
                return {
                    "job_id": outcome.job_id,
                    "status": outcome.status.value,
                    "error_code": outcome.error_code,
                    "elapsed_milliseconds": round((time.monotonic() - begin) * 1000),
                }

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(publish, index) for index in range(2)]
                outcomes = [future.result(timeout=30) for future in futures]
            states = [queue.get(job.job_id) for job in jobs]
            winners = [job for job in states if job.status.value == "SUCCEEDED"]
            losers = [job for job in states if job.status.value == "FAILED"]
            assert len(winners) == len(losers) == 1, (
                "Exactly one concurrent successor must become visible"
            )
            assert losers[0].error_code == "source_item_publication_conflict"
            assert losers[0].result is None
            assert (
                losers[0].progress["unpublished_result"]["baseline_revision_artifact"][
                    "object_key"
                ]
                in objects
            )
            assert queue.get(historical.job_id).result == historical_result
            with database.connection() as connection:
                visible = connection.execute(
                    """SELECT id, result #>> '{baseline_revision_artifact,baseline_artifact_sha256}' AS sha
                    FROM runtime_jobs WHERE status='SUCCEEDED' AND aggregate_id=%s
                    ORDER BY (result #>> '{baseline_revision_artifact,baseline_revision}')::integer DESC LIMIT 1""",
                    (document_id,),
                ).fetchone()
            assert visible["id"] == winners[0].job_id
            preserved = {}
            for key, content in artifact_bytes.items():
                with store.open(key) as stream:
                    actual = stream.read()
                assert actual == content, (
                    "Historical and unpublished candidate bytes must remain intact"
                )
                preserved[key] = hashlib.sha256(actual).hexdigest()
            receipt.update(
                status="PASS",
                outcomes=outcomes,
                winning_job_id=winners[0].job_id,
                rejected_job_id=losers[0].job_id,
                visible_baseline_sha256=visible["sha"],
                historical_result_unchanged=True,
                rejected_result_not_visible=True,
                preserved_artifact_sha256=preserved,
                concurrent_threads=2,
            )
            # A fresh graph publishes Map + baseline as one envelope. Two
            # producers that both observed "no graph" must not both win.
            raw_document_id = "native-fence-raw-document"
            with database.connection() as connection:
                connection.execute(
                    """INSERT INTO sor_documents(id, project_id, file_name, file_type, storage_key,
                        extracted_text, source_sha256, source_size, created_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        raw_document_id,
                        project_id,
                        "source.pdf",
                        "pdf",
                        "synthetic/source.pdf",
                        "",
                        source_sha,
                        len(raw_source),
                        now,
                        now,
                    ),
                )
            raw_jobs = [
                queue.enqueue(
                    kind=kind,
                    aggregate_type="sor-document",
                    aggregate_id=raw_document_id,
                    idempotency_key="initial-race-" + str(index),
                    payload={
                        **base_payload,
                        "document_id": raw_document_id,
                        "phase": "INITIAL_SOURCE",
                    },
                )
                for index, kind in enumerate(
                    ("agent.source-item.extract-loop", "agent.source-item.loop")
                )
            ]
            with database.connection() as connection:
                for index, job in enumerate(raw_jobs):
                    connection.execute(
                        """UPDATE runtime_jobs SET status='RUNNING', lease_owner=%s,
                        lease_expires_at=now()+interval '2 minutes', attempts=1, generation=1, started_at=now()
                        WHERE id=%s""",
                        ("initial-worker-" + str(index), job.job_id),
                    )
            initial_barrier = Barrier(2)

            def publish_initial(index):
                initial_barrier.wait(timeout=10)
                result = {
                    "source_sha256": source_sha,
                    "document_map_artifact_sha256": map_sha,
                    "baseline_sha256": str(index + 6) * 64,
                    "baseline_revision": 1,
                    "native_source_graph_artifact": pointer(
                        "synthetic/candidate-a.json"
                        if index == 0
                        else "synthetic/candidate-b.json",
                        str(index + 6) * 64,
                        1,
                    ),
                    "expected_no_source_graph": True,
                    "expected_structure_head_verified": True,
                    "expected_structure_head_revision_sha256": None,
                }
                return queue.succeed(
                    job_id=raw_jobs[index].job_id,
                    worker_id="initial-worker-" + str(index),
                    result=result,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(publish_initial, index) for index in range(2)]
                raw_outcomes = [future.result(timeout=30) for future in futures]
            assert sorted(outcome.status.value for outcome in raw_outcomes) == [
                "FAILED",
                "SUCCEEDED",
            ]
            raw_winner = next(
                outcome
                for outcome in raw_outcomes
                if outcome.status.value == "SUCCEEDED"
            )
            raw_loser = next(
                outcome for outcome in raw_outcomes if outcome.status.value == "FAILED"
            )
            assert (
                raw_loser.result is None
                and raw_loser.error_code == "source_item_publication_conflict"
            )
            stored = queue.get_latest_succeeded(
                kind=raw_winner.kind,
                aggregate_type="sor-document",
                aggregate_id=raw_document_id,
                require_result_key="native_source_graph_artifact",
            )
            assert stored.job_id == raw_winner.job_id
            assert (
                queue.get_latest_succeeded(
                    kind=raw_winner.kind,
                    aggregate_type="sor-document",
                    aggregate_id=raw_document_id,
                    require_result_key="nonexistent_result_pointer",
                )
                is None
            )
            receipt["initial_source_graph_race"] = {
                "status": "PASS",
                "winning_job_id": raw_winner.job_id,
                "rejected_job_id": raw_loser.job_id,
                "rejected_error_code": raw_loser.error_code,
                "only_one_initial_graph_visible": True,
                "result_key_filter_verified": True,
            }
            # A graph/Map successor and a conventional baseline revision share
            # the same current version and must compete under the same fence.
            predecessor = raw_winner.result
            mixed_jobs = [
                queue.enqueue(
                    kind=kind,
                    aggregate_type="sor-document",
                    aggregate_id=raw_document_id,
                    idempotency_key="post-published-" + str(index),
                    payload={
                        **base_payload,
                        "document_id": raw_document_id,
                        "phase": "REVIEW",
                    },
                )
                for index, kind in enumerate(
                    ("agent.source-item.extract-loop", "agent.source-item.classify")
                )
            ]
            mixed_results = [
                {
                    "source_sha256": source_sha,
                    "document_map_artifact_sha256": "8" * 64,
                    "baseline_sha256": "9" * 64,
                    "baseline_revision": 2,
                    "native_source_graph_artifact": pointer(
                        "synthetic/candidate-a.json", "9" * 64, 2
                    ),
                    "expected_native_source_graph_object_sha256": predecessor[
                        "native_source_graph_artifact"
                    ]["object_sha256"],
                    "expected_live_baseline_sha256": predecessor["baseline_sha256"],
                    "expected_document_map_artifact_sha256": map_sha,
                },
                {
                    "source_sha256": source_sha,
                    "document_map_artifact_sha256": map_sha,
                    "baseline_revision_artifact": pointer(
                        "synthetic/candidate-b.json", "a" * 64, 2
                    ),
                    "expected_native_source_graph_object_sha256": predecessor[
                        "native_source_graph_artifact"
                    ]["object_sha256"],
                    "expected_live_baseline_sha256": predecessor["baseline_sha256"],
                },
            ]
            with database.connection() as connection:
                for index, job in enumerate(mixed_jobs):
                    connection.execute(
                        "UPDATE runtime_jobs SET status='RUNNING', lease_owner=%s, lease_expires_at=now()+interval '2 minutes', attempts=1,generation=1 WHERE id=%s",
                        ("mixed-worker-" + str(index), job.job_id),
                    )
            mixed_barrier = Barrier(2)

            def publish_mixed(index):
                mixed_barrier.wait(timeout=10)
                return queue.succeed(
                    job_id=mixed_jobs[index].job_id,
                    worker_id="mixed-worker-" + str(index),
                    result=mixed_results[index],
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                mixed = list(pool.map(publish_mixed, range(2)))
            assert sorted(job.status.value for job in mixed) == ["FAILED", "SUCCEEDED"]
            winning = next(job for job in mixed if job.status.value == "SUCCEEDED")
            rejected = next(job for job in mixed if job.status.value == "FAILED")
            assert (
                rejected.error_code == "source_item_publication_conflict"
                and rejected.result is None
            )
            actual = winning.result
            actual_base = (actual.get("baseline_revision_artifact") or {}).get(
                "baseline_artifact_sha256"
            ) or actual["baseline_sha256"]
            actual_graph = (
                actual.get("native_source_graph_artifact")
                or predecessor["native_source_graph_artifact"]
            )
            successor = queue.enqueue(
                kind="agent.source-item.extract-loop",
                aggregate_type="sor-document",
                aggregate_id=raw_document_id,
                idempotency_key="post-published-final-graph",
                payload={
                    **base_payload,
                    "document_id": raw_document_id,
                    "phase": "REVIEW",
                },
            )
            graph_result = {
                "source_sha256": source_sha,
                "document_map_artifact_sha256": "b" * 64,
                "baseline_sha256": "c" * 64,
                "baseline_revision": 3,
                "native_source_graph_artifact": pointer(
                    "synthetic/previous.json", "c" * 64, 3
                ),
                "expected_native_source_graph_object_sha256": actual_graph[
                    "object_sha256"
                ],
                "expected_live_baseline_sha256": actual_base,
                "expected_document_map_artifact_sha256": actual[
                    "document_map_artifact_sha256"
                ],
            }
            with database.connection() as connection:
                connection.execute(
                    "UPDATE runtime_jobs SET status='RUNNING',lease_owner='successor-worker',lease_expires_at=now()+interval '2 minutes',attempts=1,generation=1 WHERE id=%s",
                    (successor.job_id,),
                )
            final_graph = queue.succeed(
                job_id=successor.job_id,
                worker_id="successor-worker",
                result=graph_result,
            )
            assert final_graph.status.value == "SUCCEEDED"
            latest_graph = queue.get_latest_succeeded(
                kind="agent.source-item.extract-loop",
                aggregate_type="sor-document",
                aggregate_id=raw_document_id,
                require_result_key="native_source_graph_artifact",
            )
            assert latest_graph.job_id == final_graph.job_id
            assert queue.get(raw_winner.job_id).result == predecessor
            receipt["post_publication_graph_and_baseline_race"] = {
                "status": "PASS",
                "winning_job_id": winning.job_id,
                "winning_kind": winning.kind,
                "rejected_job_id": rejected.job_id,
                "error_code": rejected.error_code,
                "one_unified_visible_head": True,
                "final_graph_job_id": final_graph.job_id,
                "graph_can_follow_either_graph_or_product_baseline": True,
                "old_graph_result_unchanged": True,
            }
            same_base_jobs = []
            for index in range(2):
                same_base_job = queue.enqueue(
                    kind=("agent.source-item.extract-loop", "agent.source-item.loop")[
                        index
                    ],
                    aggregate_type="sor-document",
                    aggregate_id=raw_document_id,
                    idempotency_key="same-baseline-graph-" + str(index),
                    payload={
                        **base_payload,
                        "document_id": raw_document_id,
                        "phase": "REVIEW",
                    },
                )
                same_base_jobs.append(same_base_job)
            with database.connection() as connection:
                for index, job in enumerate(same_base_jobs):
                    connection.execute(
                        "UPDATE runtime_jobs SET status='RUNNING',lease_owner=%s,lease_expires_at=now()+interval '2 minutes',attempts=1,generation=1 WHERE id=%s",
                        ("same-base-" + str(index), job.job_id),
                    )
            same_barrier = Barrier(2)

            def publish_same_base(index):
                same_barrier.wait(timeout=10)
                result = {
                    **graph_result,
                    "expected_native_source_graph_object_sha256": graph_result[
                        "native_source_graph_artifact"
                    ]["object_sha256"],
                    "expected_live_baseline_sha256": graph_result["baseline_sha256"],
                    "expected_document_map_artifact_sha256": graph_result[
                        "document_map_artifact_sha256"
                    ],
                    "native_source_graph_artifact": pointer(
                        "synthetic/candidate-a.json"
                        if index == 0
                        else "synthetic/candidate-b.json",
                        graph_result["baseline_sha256"],
                        3,
                    ),
                }
                return queue.succeed(
                    job_id=same_base_jobs[index].job_id,
                    worker_id="same-base-" + str(index),
                    result=result,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                same_results = list(pool.map(publish_same_base, range(2)))
            assert sorted(job.status.value for job in same_results) == [
                "FAILED",
                "SUCCEEDED",
            ]
            receipt["same_baseline_graph_review_race"] = {
                "status": "PASS",
                "exactly_one_saved": True,
                "baseline_sha256": graph_result["baseline_sha256"],
                "graph_object_cas_required": True,
            }
            from .test_source_item_manual_publication import run_manual_publication_integration
            receipt["manual_publication_hook"] = run_manual_publication_integration(database, queue, project_id)
    finally:
        if database is not None:
            database.close()
        if created:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
                )
                missing = (
                    admin.execute(
                        "SELECT 1 FROM pg_database WHERE datname=%s", (database_name,)
                    ).fetchone()
                    is None
                )
                receipt["database_deleted"] = missing
                assert missing, "The probe must remove only its own disposable database"
        receipt["elapsed_milliseconds"] = round((time.monotonic() - started) * 1000)
    return receipt


@unittest.skipUnless(
    os.environ.get("REQPILOT_RUN_NATIVE_LOOP_POSTGRES_E2E") == "1",
    "Set REQPILOT_RUN_NATIVE_LOOP_POSTGRES_E2E=1 for the disposable local PostgreSQL proof",
)
class NativeLoopPublicationPostgresTests(unittest.TestCase):
    def test_concurrent_same_predecessor_has_one_visible_successor(self):
        receipt = run_publication_fence_probe()
        self.assertEqual(receipt["status"], "PASS")
        self.assertTrue(receipt["database_deleted"])
        output = os.environ.get("REQPILOT_NATIVE_LOOP_POSTGRES_RECEIPT")
        if output:
            destination = Path(output)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
            )
            destination.chmod(0o600)
