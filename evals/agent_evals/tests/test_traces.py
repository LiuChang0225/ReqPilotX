"""Regression contracts for public trace identity, privacy and save evidence."""

import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from evals.agent_evals.traces import analyze


def digest(value):
    return hashlib.sha256(value).hexdigest()


def seal(value, key):
    value = {name: item for name, item in value.items() if name != key}
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return {**value, key: digest(encoded)}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def event(kind, *, time=None, **values):
    result = {"type": kind, **values}
    if time is not None:
        result["_host_elapsed_seconds"] = time
    return result


def call(kind="item.completed", identifier="item_1", **fields):
    return event(
        kind,
        item={
            "id": identifier,
            "type": "mcp_tool_call",
            "server": "reqpilot",
            "tool": "source_read",
            **fields,
        },
    )


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root / "events.jsonl"

    def analyze(self, rows, workspace=None, start=None):
        self.events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return analyze(self.events, workspace, start)

    def test_call_identity_includes_every_explicit_turn_occurrence(self):
        rows = [event("thread.started", thread_id="thread-a", time=0)]
        for number in range(2):
            rows += [
                event("turn.started", turn_id="reused-turn", time=number * 10 + 1),
                call("item.started"),
                call(),
                call(),
                event("turn.completed", usage={"input_tokens": 2, "output_tokens": 3}),
            ]
        result = self.analyze(rows)
        self.assertEqual(result["events"]["unique_tool_calls"], 2)
        self.assertEqual(result["events"]["groups"]["mcp_calls"]["event_records"], 6)
        self.assertEqual(
            {value["turn_occurrence"] for value in result["events"]["calls"]}, {1, 2}
        )
        self.assertEqual(
            result["events"]["usage"]["totals"], {"input_tokens": 4, "output_tokens": 6}
        )
        self.assertEqual(result["events"]["usage"]["status"], "REPORTED")

    def test_continuations_reset_item_ids_and_clock_without_summing_timing(self):
        rows = [
            event("thread.started", thread_id="thread-a", time=0),
            event("turn.started", time=1),
            {**call(), "_host_elapsed_seconds": 20},
            event("turn.completed", time=21),
            event("turn.started", time=0),
            {**call(), "_host_elapsed_seconds": 2},
            event("turn.completed", time=3),
            event("thread.started", thread_id="thread-b", time=0),
            event("turn.started", time=1),
            {**call(), "_host_elapsed_seconds": 2},
            event("turn.completed", time=3),
        ]
        result = self.analyze(rows)["events"]
        self.assertEqual(result["unique_tool_calls"], 3)
        self.assertEqual(
            [item["boundary_reason"] for item in result["segments"]],
            ["THREAD_STARTED", "HOST_ELAPSED_RESET", "THREAD_STARTED"],
        )
        self.assertEqual(result["first_tool_seconds"], 20)
        self.assertEqual(result["usage"]["status"], "NOT_RECORDED")
        self.assertIsNone(result["usage"]["totals"])

    def test_late_repeated_item_terminal_does_not_invent_another_turn(self):
        result = self.analyze(
            [
                event("turn.started"),
                call("item.started"),
                call(),
                event("turn.completed"),
                call(),
            ]
        )["events"]
        self.assertEqual(result["unique_tool_calls"], 1)
        self.assertEqual(len(result["turns"]), 1)
        self.assertEqual(result["terminal_integrity"], "COMPLETE")

    def test_incomplete_trace_stays_unknown_and_does_not_copy_private_text(self):
        private = "private-source-command-error-reasoning-sentinel"
        result = self.analyze(
            [
                event("turn.started"),
                event(
                    "item.started",
                    item={
                        "id": "command-1",
                        "type": "command_execution",
                        "command": "echo " + private,
                    },
                ),
                event(
                    "item.completed",
                    item={"id": "thought", "type": "reasoning", "text": private},
                ),
            ]
        )
        self.assertEqual(result["events"]["terminal_integrity"], "UNKNOWN")
        self.assertEqual(result["events"]["calls"][0]["terminal_state"], "unknown")
        self.assertEqual(result["events"]["reasoning_events_omitted"], 1)
        self.assertIn("TURN_TERMINAL_NOT_RECORDED", result["error_codes"])
        self.assertNotIn(private, json.dumps(result))
        self.assertNotIn("command_preview", json.dumps(result))

    def test_partial_usage_deduplicates_terminal_snapshots_and_discards_unknown_fields(
        self,
    ):
        rows = [
            event("turn.started"),
            call(),
            event(
                "turn.completed",
                usage={"input_tokens": 7, "output_tokens": 2, "secret": "do-not-copy"},
            ),
            event("turn.completed", usage={"input_tokens": 7, "output_tokens": 2}),
            event("turn.started"),
            call(),
            event("turn.completed", usage={"output_tokens": 4}),
        ]
        result = self.analyze(rows)["events"]
        self.assertEqual(result["usage"]["status"], "PARTIAL")
        self.assertEqual(
            result["usage"]["totals"], {"input_tokens": 7, "output_tokens": 6}
        )
        self.assertNotIn("do-not-copy", json.dumps(result))

    def test_host_approval_rejection_remains_an_environment_failure(self):
        private = "private-customer-fragment"
        result = self.analyze(
            [
                event("turn.started"),
                call(error={"message": "Approval required but rejected: " + private}),
                event(
                    "turn.failed",
                    error={"message": "Tool permission denied: " + private},
                ),
            ]
        )
        self.assertIn("HOST_APPROVAL_DENIED", result["error_codes"])
        observation = result["events"]["calls"][0]["terminal_observations"][0]
        self.assertEqual(observation["error_category"], "ENVIRONMENT")
        self.assertEqual(result["events"]["calls"][0]["terminal_state"], "failed")
        self.assertNotIn(private, json.dumps(result))
        self.assertNotIn("MODEL_CAPABILITY_FAILURE", json.dumps(result))

    def test_normalized_host_idle_timeout_is_not_a_model_capability_grade(self):
        result = self.analyze(
            [
                event("turn.started"),
                event(
                    "turn.failed",
                    error_code="HOST_LLM_STREAM_IDLE_TIMEOUT",
                    idle_timeout_ms=300000,
                ),
            ]
        )
        self.assertIn("HOST_LLM_STREAM_IDLE_TIMEOUT", result["error_codes"])
        self.assertNotIn("MODEL_CAPABILITY_FAILURE", json.dumps(result))

    def test_never_policy_host_error_is_distinct_from_approval_text_in_a_result(self):
        message = "MCP tool call requires approval, but approval policy is never"
        result = self.analyze(
            [
                event("turn.started"),
                call(identifier="host", status="failed", error={"message": message}),
                call(
                    identifier="source-text",
                    result={
                        "isError": True,
                        "content": [{"type": "text", "text": message}],
                    },
                ),
                event("turn.completed"),
            ]
        )
        host, source_text = result["events"]["calls"]
        self.assertEqual(
            host["terminal_observations"][0]["error_code"], "HOST_APPROVAL_DENIED"
        )
        self.assertEqual(
            host["terminal_observations"][0]["error_category"], "ENVIRONMENT"
        )
        self.assertEqual(
            source_text["terminal_observations"][0]["error_code"],
            "TOOL_EXECUTION_FAILURE",
        )
        self.assertNotIn(message, json.dumps(result))

    def test_image_bytes_and_path_only_results_are_distinct(self):
        raw = b"synthetic-image-content"
        image_result = {
            "content": [
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": base64.b64encode(raw).decode(),
                }
            ]
        }
        result = self.analyze(
            [
                event("turn.started"),
                call(identifier="one", result=image_result),
                call(
                    identifier="two",
                    arguments={"view": "image"},
                    result={"structuredContent": {"path": "/private/image.png"}},
                ),
                call(
                    identifier="three",
                    result={"content": [{"type": "image", "data": "%%%"}]},
                ),
                event("turn.completed"),
            ]
        )["events"]
        self.assertEqual(result["visual_evidence"]["calls_with_native_image_bytes"], 1)
        self.assertEqual(result["visual_evidence"]["mcp_image_requests"], 1)
        self.assertEqual(result["calls"][0]["image_results"][0]["sha256"], digest(raw))
        self.assertEqual(result["calls"][1]["image_results"], [])
        self.assertEqual(
            result["calls"][2]["image_results"][0]["evidence"],
            "INVALID_IMAGECONTENT_ENCODING",
        )
        self.assertNotIn(base64.b64encode(raw).decode(), json.dumps(result))
        self.assertNotIn("/private/image.png", json.dumps(result))

    def task(self, states=("COMMITTED",), artifact_kind="DOCUMENT_MAP", operation="compile-map"):
        workspace = self.root / "model-workspace"
        task = workspace / "tasks/task-a"
        source = b"private-customer-original"
        task.mkdir(parents=True)
        (task / "source.pdf").write_bytes(source)
        context = seal(
            {
                "schema_version": "reqpilot-task-workspace/v1",
                "work_id": "task-a",
                "source": {"path": "source.pdf", "sha256": digest(source)},
            },
            "context_sha256",
        )
        write(task / "task-context.json", context)
        write(
            workspace / "runtime-data/input-manifest.json",
            {"inputs": [{"sha256": digest(source)}]},
        )
        entries = []
        for number, state in enumerate(states, 1):
            output = f"artifacts/map-{number}"
            artifact = {"synthetic": "private-artifact-body"}
            write(task / output / "document-map.json", artifact)
            file_hash = digest((task / output / "document-map.json").read_bytes())
            receipt = seal(
                {
                    "schema_version": "reqpilot-operation-receipt/v1",
                    "work_id": "task-a",
                    "operation_id": f"op-{number}",
                    "operation": operation,
                    "source_sha256": digest(source),
                    "output_dir": output,
                    "files": {"document-map.json": file_hash},
                    "validation": {
                        "artifacts": [
                            {
                                "kind": artifact_kind,
                                "path": output + "/document-map.json",
                                "file_sha256": file_hash,
                            }
                        ]
                    },
                    "saved_at": f"2026-09-13T00:00:{number * 10:02d}+00:00",
                },
                "receipt_sha256",
            )
            write(task / output / "operation-receipt.json", receipt)
            entry = {
                "operation_id": f"op-{number}",
                "operation": operation,
                "state": state,
                "receipt_path": output + "/operation-receipt.json",
                "receipt_sha256": receipt["receipt_sha256"],
            }
            entries.append(entry)
            write(
                task / "operations" / f"op-{number}.json",
                {"operation_id": f"op-{number}", "state": state},
            )
        index = seal(
            {
                "schema_version": "reqpilot-workspace-index/v1",
                "work_id": "task-a",
                "source_sha256": digest(source),
                "committed": entries,
                "candidates": [
                    entry for entry in entries if entry["state"] != "COMMITTED"
                ],
            },
            "index_sha256",
        )
        write(task / "workspace-index.json", index)
        start = self.root / "start.json"
        write(start, {"started_at": "2026-09-13T00:00:00+00:00"})
        return workspace, task, start

    def test_visual_reading_receipts_are_verified_and_still_reject_tampered_bytes(self):
        workspace,task,start=self.task(artifact_kind='VISUAL_SOURCE',operation='read-pdf-visual')
        verified=self.analyze([],workspace,start)['saved_artifacts']
        self.assertEqual(verified['status'],'VERIFIED')
        self.assertEqual(verified['first_effective_artifact_seconds'],{'VISUAL_SOURCE':10})
        self.assertEqual(verified['operations'][0]['operation'],'read-pdf-visual')
        self.assertEqual(verified['semantic_acceptance'],'NOT_CLAIMED')
        (task/'artifacts/map-1/document-map.json').write_text('changed bytes')
        broken=self.analyze([],workspace,start)['saved_artifacts']
        self.assertIn('SAVED_ARTIFACT_HASH_MISMATCH',broken['error_codes'])

    def test_runtime_artifact_and_operation_catalogs_are_supported_by_the_evaluator(self):
        import ast
        from evals.agent_evals.traces import ARTIFACT_KINDS,OPERATION_KINDS
        path=(
            Path(__file__).resolve().parents[3]
            / 'packages/engine/src/reqpilotx/application/agent_workflow/artifact_kind_catalog.py'
        )
        module=ast.parse(path.read_text())
        catalogs={}
        for node in module.body:
            if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id in {'ARTIFACT_KINDS','OPERATION_KINDS'}:
                catalogs[node.targets[0].id]=ast.literal_eval(node.value)
        self.assertLessEqual(set(catalogs['ARTIFACT_KINDS']),ARTIFACT_KINDS)
        self.assertLessEqual(set(catalogs['OPERATION_KINDS']),OPERATION_KINDS)

    def test_effective_save_requires_bytes_receipt_index_and_source_to_match(self):
        workspace, task, start = self.task()
        result = self.analyze([], workspace, start)["saved_artifacts"]
        self.assertEqual(
            result["first_effective_artifact_seconds"], {"DOCUMENT_MAP": 10}
        )
        self.assertEqual(result["counts"]["effective_saved_operations"], 1)
        self.assertEqual(result["semantic_acceptance"], "NOT_CLAIMED")
        artifact = task / "artifacts/map-1/document-map.json"
        artifact.write_text("tampered-private-body")
        before = {
            str(path): path.read_bytes()
            for path in workspace.rglob("*")
            if path.is_file()
        }
        result = self.analyze([], workspace, start)["saved_artifacts"]
        self.assertEqual(result["first_effective_artifact_seconds"], {})
        self.assertIn("SAVED_ARTIFACT_HASH_MISMATCH", result["error_codes"])
        self.assertFalse(result["operations"][0]["effective_saved"])
        self.assertNotIn("tampered-private-body", json.dumps(result))
        self.assertEqual(
            before,
            {
                str(path): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            },
        )

    def test_candidates_conflicts_and_unindexed_receipts_do_not_win_first_save(self):
        workspace, task, start = self.task(
            ("SAVED_CANDIDATE", "CANDIDATE_CONFLICT", "COMMITTED")
        )
        write(
            task / "operations/op-unfinished.json",
            {"operation_id": "op-unfinished", "state": "RUNNING"},
        )
        result = self.analyze([], workspace, start)["saved_artifacts"]
        self.assertEqual(
            result["first_effective_artifact_seconds"], {"DOCUMENT_MAP": 30}
        )
        self.assertEqual(result["counts"]["candidate_operations"], 2)
        self.assertEqual(result["counts"]["conflict_operations"], 1)
        self.assertEqual(result["counts"]["incomplete_operations"], 1)
        self.assertEqual(result["counts"]["effective_saved_operations"], 1)

    def test_source_tamper_and_receipt_path_escape_are_reported_as_codes(self):
        workspace, task, start = self.task()
        (task / "source.pdf").write_bytes(b"changed")
        result = self.analyze([], workspace, start)["saved_artifacts"]
        self.assertIn("SOURCE_BYTES_CHANGED", result["error_codes"])
        self.assertEqual(result["first_effective_artifact_seconds"], {})
        (task / "source.pdf").write_bytes(b"private-customer-original")
        index = json.loads((task / "workspace-index.json").read_bytes())
        index["committed"][0]["receipt_path"] = "../external-private-receipt.json"
        write(task / "workspace-index.json", seal(index, "index_sha256"))
        result = self.analyze([], workspace, start)["saved_artifacts"]
        self.assertIn("ARTIFACT_PATH_OUTSIDE_BUNDLE", result["error_codes"])
        self.assertNotIn("external-private-receipt", json.dumps(result))

    def test_resealed_receipt_cannot_claim_an_unbound_artifact(self):
        workspace, task, start = self.task()
        receipt_path = task / "artifacts/map-1/operation-receipt.json"
        receipt = json.loads(receipt_path.read_bytes())
        receipt["validation"]["artifacts"][0]["path"] = "unbound.json"
        receipt = seal(receipt, "receipt_sha256")
        write(receipt_path, receipt)
        index = json.loads((task / "workspace-index.json").read_bytes())
        index["committed"][0]["receipt_sha256"] = receipt["receipt_sha256"]
        write(task / "workspace-index.json", seal(index, "index_sha256"))
        result = self.analyze([], workspace, start)["saved_artifacts"]
        self.assertIn("ARTIFACT_NOT_BOUND_TO_RECEIPT_FILES", result["error_codes"])
        self.assertEqual(result["first_effective_artifact_seconds"], {})

    def test_resealed_duplicate_index_is_rejected_before_counting_any_save(self):
        workspace, task, start = self.task()
        index = json.loads((task / "workspace-index.json").read_bytes())
        index["committed"].append(index["committed"][0])
        write(task / "workspace-index.json", seal(index, "index_sha256"))
        result = self.analyze([], workspace, start)["saved_artifacts"]
        self.assertIn("DUPLICATE_INDEX_OPERATION", result["error_codes"])
        self.assertEqual(result["first_effective_artifact_seconds"], {})
        self.assertEqual(result["counts"]["effective_saved_operations"], 0)

    def test_malformed_line_and_invalid_start_do_not_fabricate_usage_or_time(self):
        workspace, _, start = self.task()
        write(start, {"started_at": "2026-09-13T00:00:00"})
        self.events.write_bytes(b'{"type":"turn.started"}\n{"type":')
        result = analyze(self.events, workspace, start)
        self.assertIn("TIMESTAMP_INVALID_OR_TIMEZONE_MISSING", result["error_codes"])
        self.assertIn("MALFORMED_EVENT_LINE", result["error_codes"])
        self.assertEqual(
            result["saved_artifacts"]["first_effective_artifact_seconds"], {}
        )
        self.assertEqual(result["events"]["usage"]["status"], "NOT_RECORDED")


if __name__ == "__main__":
    unittest.main()
