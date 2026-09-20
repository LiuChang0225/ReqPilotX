from __future__ import annotations
# ruff: noqa: E402 -- test runtime must be isolated before importing the application.

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.adapters.persistence import postgres_jobs as queue
from app.application.agent_workflow import source_item_loop_jobs as loop
from app.application.agent_workflow.source_requirement_jobs import (
    SourceItemNativeLoopRunReceiptV1,
)
from app.application.agent_workflow.source_requirement_baseline import (
    canonical_json_sha256,
)
from .test_source_item_review_context import fixture


def runtime_receipt():
    return {
        "adapter_id": "reqpilotx-codex-native-loop",
        "status": "COMPLETED",
        "terminal_snapshot_verified": True,
        "thread_retained": True,
        "thread_id": "thread",
        "turn_id": "turn",
        "identity": {"model_id": "model"},
        "is_human_accepted": False,
        "tools": [{"name": "reqpilot_publish", "success": True}],
    }


class ReceiptTests(unittest.TestCase):
    def test_disposable_or_unfinished_runtime_is_not_native_publication(self):
        value = dict(
            source_sha256="1" * 64,
            document_map_artifact_id="map",
            document_map_artifact_sha256="2" * 64,
            predecessor_sha256="3" * 64,
            model_id="model",
            runtime_thread_id="thread",
            runtime_turn_id="turn",
            review_sha256="4" * 64,
            request_sha256="5" * 64,
            candidate_sha256="6" * 64,
            runtime_receipt=runtime_receipt(),
        )
        self.assertEqual(
            SourceItemNativeLoopRunReceiptV1(**value).transport_kind,
            "CODEX_APP_SERVER_NATIVE_LOOP",
        )
        for change in (
            {"status": "CANDIDATE_RETURNED", "thread_cleanup_verified": True},
            {"status": "UNKNOWN"},
            {"terminal_snapshot_verified": False},
            {"thread_id": "another"},
            {"is_human_accepted": True},
            {"tools": []},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                SourceItemNativeLoopRunReceiptV1(
                    **{**value, "runtime_receipt": {**runtime_receipt(), **change}}
                )


class Connection:
    def __init__(self, *, latest=None, source="1" * 64, head=None):
        self.latest, self.source, self.head = latest, source, head
        self.statements = []
        self.value = None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "SELECT * FROM runtime_jobs WHERE id" in sql:
            self.value = {
                "id": "job",
                "kind": "agent.source-item.loop",
                "aggregate_id": "document",
                "payload": {"project_id": "project", "source_sha256": "1" * 64},
            }
        elif "SELECT source_sha256 FROM sor_documents" in sql:
            self.value = {"source_sha256": self.source}
        elif (
            "SELECT revision_sha256 FROM agent_structure_working_revision_heads" in sql
        ):
            self.value = {"revision_sha256": self.head} if self.head else None
        elif "AS baseline_sha256" in sql:
            self.value = self.latest
        elif "SET status = 'FAILED'" in sql:
            self.value = {
                "status": "FAILED",
                "error_code": "source_item_publication_conflict",
            }
        elif "UPDATE runtime_jobs" in sql:
            self.value = {"status": "SUCCEEDED"}
        else:
            self.value = None
        return self

    def fetchone(self):
        return self.value


class PublicationFenceTests(unittest.TestCase):
    def result(self):
        return {
            "source_sha256": "1" * 64,
            "document_map_artifact_sha256": "2" * 64,
            "expected_live_baseline_sha256": "3" * 64,
            "baseline_revision_artifact": {"baseline_artifact_sha256": "4" * 64},
        }

    def test_checks_actual_predecessor_inside_shared_map_lock(self):
        connection = Connection(
            latest={
                "source_sha256": "1" * 64,
                "map_sha256": "2" * 64,
                "baseline_sha256": "3" * 64,
            }
        )
        self.assertIsNone(
            queue._source_item_publication_conflict(
                connection, job_id="job", worker_id="worker", result=self.result()
            )
        )
        statements = [sql for sql, _ in connection.statements]
        self.assertLess(
            next(
                i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql
            ),
            next(i for i, sql in enumerate(statements) if "AS baseline_sha256" in sql),
        )
        connection.latest["baseline_sha256"] = "9" * 64
        self.assertIn(
            "另一轮",
            queue._source_item_publication_conflict(
                connection, job_id="job", worker_id="worker", result=self.result()
            ),
        )

    def test_source_and_map_head_changes_are_rejected(self):
        connection = Connection(source="9" * 64)
        self.assertIn(
            "原文件",
            queue._source_item_publication_conflict(
                connection, job_id="job", worker_id="worker", result=self.result()
            ),
        )
        connection = Connection(head="9" * 64)
        value = {
            **self.result(),
            "expected_structure_head_verified": True,
            "expected_structure_head_revision_sha256": "8" * 64,
        }
        self.assertIn(
            "原文组织",
            queue._source_item_publication_conflict(
                connection, job_id="job", worker_id="worker", result=value
            ),
        )

    def test_conflict_commits_failed_job_without_making_result_visible(self):
        connection = Connection(
            latest={
                "source_sha256": "1" * 64,
                "map_sha256": "2" * 64,
                "baseline_sha256": "9" * 64,
            }
        )

        class Database:
            @contextmanager
            def connection(self):
                yield connection

        with (
            patch.object(queue, "_required_leased_job", side_effect=lambda row: row),
            patch.object(
                queue, "_publish_document_source_asset_references"
            ) as publish_source,
            patch.object(
                queue, "_publish_extraction_result_artifact_reference"
            ) as publish_extract,
        ):
            result = queue.PostgresJobQueue(Database()).succeed(
                job_id="job", worker_id="worker", result=self.result()
            )
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(publish_source.called)
        self.assertFalse(publish_extract.called)
        self.assertFalse(
            any("SET status = CASE" in sql for sql, _ in connection.statements)
        )
        self.assertTrue(
            any("unpublished_result" in sql for sql, _ in connection.statements)
        )


class TaskTests(unittest.TestCase):
    def test_resume_command_idempotency_survives_baseline_advance(self):
        jobs = {}

        class Jobs:
            def get_by_idempotency(self, *, kind, idempotency_key):
                return jobs.get(idempotency_key)

            def enqueue(self, **kwargs):
                value = SimpleNamespace(
                    job_id="job-" + str(len(jobs)), payload=kwargs["payload"]
                )
                jobs[kwargs["idempotency_key"]] = value
                return value

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            meta = {
                "task_id": "task",
                "project_id": "project",
                "document_id": "document",
                "source_sha256": "1" * 64,
                "origin": "AGENT_SELF_REVIEW",
            }
            container = SimpleNamespace(jobs=Jobs())
            first = loop._enqueue(
                container,
                root,
                meta,
                command_id="once",
                goal="review",
                expected_baseline_sha256="2" * 64,
            )
            replay = loop._enqueue(
                container,
                root,
                meta,
                command_id="once",
                goal="review",
                expected_baseline_sha256="3" * 64,
            )
            self.assertIs(first, replay)
            self.assertEqual(len(jobs), 1)

    def test_public_events_are_incremental_and_remove_source_arguments(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "runtime").mkdir()
            (root / "runtime/events.jsonl").write_bytes(
                (
                    json.dumps(
                        {
                            "type": "tool_started",
                            "name": "reqpilot_read",
                            "arguments": {"source": "large evidence"},
                        }
                    )
                    + "\n"
                ).encode()
                + b'{"partial":'
            )
            with patch.object(loop, "_existing", return_value=(None, root, {})):
                result = loop.get_source_item_loop_events(
                    "p", "d", "task", SimpleNamespace(headers={})
                )
                self.assertEqual(
                    result["events"],
                    [{"type": "tool_started", "name": "reqpilot_read", "message": "正在查阅原文与成果", "event_cursor": result["next_cursor"]}],
                )
                replay = loop.get_source_item_loop_events(
                    "p",
                    "d",
                    "task",
                    SimpleNamespace(headers={}),
                    after=result["next_cursor"],
                )
                self.assertEqual(replay["events"], [])
                self.assertEqual(replay["next_cursor"], result["next_cursor"])

    def test_no_source_change_still_saves_analysis_and_annotations(self):
        document_map, baseline, validation = fixture()
        bundle = {
            "document_map": document_map,
            "baseline": baseline,
            "receipt": validation,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            version = root / "review/versions/initial"
            version.mkdir(parents=True)
            head = {
                "schema_version": "reqpilot-review-workspace/v1",
                "current": {
                    "version_path": "versions/initial",
                    "baseline_sha256": baseline.artifact_sha256,
                },
                "predecessor_review_sha256": "",
                "delivery": {"path": "delivery"},
            }
            head["review_sha256"] = canonical_json_sha256(head)
            loop._write_json(root / "review/workspace-manifest.json", head)
            loop._write_json(
                version / "source-item-assessments.json",
                {
                    "baseline_sha256": baseline.artifact_sha256,
                    "source_sha256": baseline.source_sha256,
                    "records": {},
                },
            )
            loop._write_json(
                version / "image-annotations.json",
                {
                    "baseline_sha256": baseline.artifact_sha256,
                    "source_sha256": baseline.source_sha256,
                    "records": {},
                },
            )
            (root / "review/delivery").mkdir()
            loop._write_json(
                root / "review/delivery/delivery-receipt.json",
                {"xlsx_sha256": "9" * 64},
            )
            job = SimpleNamespace(
                payload={"baseline_sha256": baseline.artifact_sha256},
                idempotency_key="8" * 64,
            )
            meta = {"project_id": "p", "document_id": "d", "task_id": "task"}
            context = SimpleNamespace(begin_publication=lambda: None)
            with (
                patch.object(
                    loop,
                    "_portable",
                    return_value={"review_sha256": head["review_sha256"]},
                ),
                patch.object(
                    loop, "_source_context", return_value=(None, bundle, None, {})
                ),
                patch.object(
                    loop, "publish_agent_source_item_revision"
                ) as publish_baseline,
                patch.object(
                    loop,
                    "_write_private",
                    side_effect=lambda *args, **kwargs: {"object_key": kwargs["stage"]},
                ),
                patch.object(loop, "_carry_review_feedback"),
            ):
                result = loop._publish_loop(
                    SimpleNamespace(),
                    root,
                    meta,
                    job,
                    context,
                    runtime_receipt(),
                    {"review_sha256": head["review_sha256"]},
                )
            self.assertFalse(publish_baseline.called)
            self.assertTrue(result["application_saved"])
            self.assertEqual(result["status"], "REVIEW_COMPLETE")
            self.assertEqual(result["baseline_sha256"], baseline.artifact_sha256)
            self.assertIn("assessments_artifact", result)
            self.assertIn("annotations_artifact", result)
            self.assertNotIn("baseline_revision_artifact", result)
