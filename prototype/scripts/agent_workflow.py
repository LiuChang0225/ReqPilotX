#!/usr/bin/env python3
"""Local host CLI for the same durable Agent workflow used by the HTTP app."""

from __future__ import annotations
import argparse
from contextlib import redirect_stdout
import json
import os
import signal
from threading import Event
from pathlib import Path
import sys
from types import SimpleNamespace

PROTOTYPE = Path(__file__).resolve().parents[1]
if str(PROTOTYPE) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser(
        "serve",
        help="Run only marked new SourceItem workbench jobs; leave historical queues untouched.",
    )
    for name in (
        "start",
        "status",
        "source-items-start",
        "decompose-start",
        "analysis-start",
    ):
        p = sub.add_parser(name)
        p.add_argument("--project-id", required=True)
        p.add_argument("--document-id", required=True)
        if name == "start":
            p.add_argument("--source-sha256", required=True)
            p.add_argument("--command-id", required=True)
            p.add_argument("--resume-job-id")
            p.add_argument("--timeout-seconds", type=int, default=1200)
        if name == "source-items-start":
            p.add_argument("--source-sha256", required=True)
            p.add_argument("--artifact-id", required=True)
            p.add_argument("--command-id", required=True)
        if name == "decompose-start":
            p.add_argument("--source-sha256", required=True)
            p.add_argument("--baseline-sha256", required=True)
            p.add_argument("--source-item-id", required=True, action="append")
            p.add_argument("--command-id", required=True)
            p.add_argument("--namespace", default="working")
            p.add_argument("--isolated-test", action="store_true")
            p.add_argument("--without-images", action="store_true")
        if name == "analysis-start":
            p.add_argument("--source-sha256", required=True)
            p.add_argument("--baseline-sha256", required=True)
            p.add_argument("--unit-id", action="append")
            p.add_argument("--knowledge-document-id", action="append")
            p.add_argument("--no-knowledge", action="store_true")
            p.add_argument("--decomposition-job-id")
            p.add_argument("--command-id", required=True)
            p.add_argument("--namespace", default="working")
            p.add_argument("--isolated-test", action="store_true")
            p.add_argument("--without-images", action="store_true")
    p = sub.add_parser("run-job")
    p.add_argument("--job-id", required=True)
    p = sub.add_parser("direct-revision-start", help="Validate and queue an explicitly authored direct-host revision; no model call")
    p.add_argument("--project-id", required=True)
    p.add_argument("--document-id", required=True)
    p.add_argument("--input", type=Path, required=True)
    p = sub.add_parser("review-annotations-put", help="Save explicit source-bound focus/attention annotations without changing a baseline")
    p.add_argument("--project-id", required=True)
    p.add_argument("--document-id", required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--expected-head-sha256")
    p = sub.add_parser("review-context", help="Read the same source-bound search and review index as the native HTTP workbench")
    p.add_argument("--project-id", required=True)
    p.add_argument("--document-id", required=True)
    p.add_argument("--expected-baseline-sha256")
    args = parser.parse_args(argv)
    os.environ.setdefault("REQPILOT_PROCESS_ROLE", "worker")
    os.environ.setdefault("REQPILOT_AGENT_RUNTIME", "codex-app-server")
    os.environ.setdefault(
        "REQPILOT_CODEX_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex"
    )
    os.environ.setdefault("REQPILOT_CODEX_HOME", str(Path.home() / ".codex-reqpilotx"))
    # No login is performed and no credentials are copied. The host must select
    # file/auto explicitly when the existing authorized login uses that store.
    with redirect_stdout(sys.stderr):
        from app.infrastructure.runtime_settings import RuntimeSettings
        from app.infrastructure.runtime_container import build_runtime_container
        from app.worker.handlers import build_job_handlers
        from app.worker.runtime import WorkerRuntime
        from app.application.agent_workflow.document_map_jobs import (
            DocumentMapStartRequestV1,
            start_document_map,
            get_document_map_status,
            _projection,
            SourceItemDocumentStartRequestV1,
            start_document_source_items,
        )

    with redirect_stdout(sys.stderr):
        container = build_runtime_container(
            RuntimeSettings.from_environment(), apply_migrations=False
        )
    try:
        with redirect_stdout(sys.stderr):
            handlers = build_job_handlers(container)
        response = SimpleNamespace(headers={}, status_code=200)
        if args.command == "serve":
            current_handlers = {
                key: handler
                for key, handler in handlers.items()
                if key == "agent.document-map"
                or key.startswith("agent.source-item")
                or key == "agent.source-requirement.baseline"
            }
            runtime = WorkerRuntime(
                queue=container.jobs,
                handlers=current_handlers,
                required_entrypoint="source-item-workbench-v1",
                worker_slot="agent-workbench",
                runtime_instance_id=os.environ.get(
                    "REQPILOT_LOCAL_RUNTIME_INSTANCE_ID", "runtime-unassigned"
                ),
                source_revision=os.environ.get(
                    "REQPILOT_RUNTIME_SOURCE_REVISION", "untracked"
                ),
                maintenance=None,
            )
            stop = Event()
            worker_pid_file = PROTOTYPE / "runtime-data/agent-workbench-worker.pid"

            def drain(_signal, _frame):
                runtime.request_drain()
                stop.set()

            signal.signal(signal.SIGTERM, drain)
            signal.signal(signal.SIGINT, drain)
            try:
                runtime.start()
                worker_pid_file.write_text(str(os.getpid()) + "\n")
                print(
                    json.dumps(
                        {
                            "schema_version": "agent-workbench-worker/v1",
                            "status": "READY",
                            "worker_id": runtime.worker_id,
                            "pid": os.getpid(),
                            "required_entrypoint": runtime.required_entrypoint,
                            "accepted_kinds": sorted(current_handlers),
                        }
                    ),
                    flush=True,
                )
                runtime.run_forever(should_stop=stop.is_set)
            finally:
                runtime.close()
                if (
                    worker_pid_file.is_file()
                    and worker_pid_file.read_text().strip() == str(os.getpid())
                ):
                    worker_pid_file.unlink()
            return 0
        if args.command == "doctor":
            from app.adapters.agents.codex_app_server.probe import run_probe
            from app.adapters.agents.codex_app_server.lifecycle import (
                load_protocol_lock,
                verify_protocol_binary_identity,
            )

            verify_protocol_binary_identity(
                container.settings.codex_bin, lock=load_protocol_lock()
            )
            value = run_probe(
                codex_bin=container.settings.codex_bin,
                codex_home=container.settings.codex_home,
                preferred_model=container.settings.agent_model_id,
                timeout_seconds=20,
            )
            value["registered_job_kinds"] = sorted(handlers)
            print(json.dumps(value, ensure_ascii=False))
            return 0 if value["account"].get("account_type") == "chatgpt" else 2
        if args.command == "review-context":
            from app.application.agent_workflow.source_item_review_context import get_source_item_review_context
            value = get_source_item_review_context(args.project_id, args.document_id, response,
                expected_baseline_sha256=args.expected_baseline_sha256)
        elif args.command == "review-annotations-put":
            from app.application.agent_workflow.source_item_review_context import publish_review_annotations
            value = publish_review_annotations(args.project_id, args.document_id, json.loads(args.input.read_text()),
                expected_head_sha256=args.expected_head_sha256)
        elif args.command == "direct-revision-start":
            from app.application.agent_workflow.source_item_direct_revision import start_direct_revision
            value = start_direct_revision(args.project_id, args.document_id, json.loads(args.input.read_text()))
        elif args.command == "start":
            value = start_document_map(
                args.project_id,
                args.document_id,
                DocumentMapStartRequestV1(
                    command_id=args.command_id,
                    expected_source_sha256=args.source_sha256,
                    resume_job_id=args.resume_job_id,
                    timeout_seconds=args.timeout_seconds,
                ),
                response,
            )
        elif args.command == "source-items-start":
            value = start_document_source_items(
                args.project_id,
                args.document_id,
                SourceItemDocumentStartRequestV1(
                    command_id=args.command_id,
                    expected_source_sha256=args.source_sha256,
                    expected_artifact_id=args.artifact_id,
                ),
                response,
            )
        elif args.command == "decompose-start":
            from app.application.agent_workflow.source_item_agent_jobs import (
                SourceItemDecompositionStartRequestV1,
                start_source_item_decomposition,
            )

            value = start_source_item_decomposition(
                args.project_id,
                args.document_id,
                SourceItemDecompositionStartRequestV1(
                    command_id=args.command_id,
                    expected_source_sha256=args.source_sha256,
                    expected_baseline_sha256=args.baseline_sha256,
                    source_item_ids=tuple(args.source_item_id),
                    explicit_trigger=True,
                    operation_namespace=args.namespace,
                    publication_mode="ISOLATED_TEST"
                    if args.isolated_test
                    else "WORKING_PROPOSAL",
                    visual_evidence=not args.without_images,
                ),
                response,
            )
        elif args.command == "analysis-start":
            from app.application.agent_workflow.source_item_agent_jobs import (
                SourceItemAnalysisStartRequestV1,
                start_source_item_analysis,
            )

            value = start_source_item_analysis(
                args.project_id,
                args.document_id,
                SourceItemAnalysisStartRequestV1(
                    command_id=args.command_id,
                    expected_source_sha256=args.source_sha256,
                    expected_baseline_sha256=args.baseline_sha256,
                    selected_unit_ids=tuple(args.unit_id) if args.unit_id else None,
                    knowledge_document_ids=()
                    if args.no_knowledge
                    else tuple(args.knowledge_document_id)
                    if args.knowledge_document_id
                    else None,
                    decomposition_job_id=args.decomposition_job_id,
                    operation_namespace=args.namespace,
                    publication_mode="ISOLATED_TEST"
                    if args.isolated_test
                    else "WORKING_PROPOSAL",
                    visual_evidence=not args.without_images,
                ),
                response,
            )
        elif args.command == "status":
            value = get_document_map_status(args.project_id, args.document_id, response)
        else:
            job = container.jobs.get(args.job_id)
            if job is None or job.kind not in handlers:
                raise ValueError("Selected job is absent or has no production handler")
            runtime = WorkerRuntime(
                queue=container.jobs,
                handlers={job.kind: handlers[job.kind]},
                job_id=job.job_id,
                worker_slot="explicit-job",
                runtime_instance_id="agent-workflow-cli",
                maintenance=None,
            )
            try:
                runtime.start()
                with redirect_stdout(sys.stderr):
                    claimed = runtime.run_once()
            finally:
                runtime.close()
            value = _projection(container.jobs.get(job.job_id))
            value["claimed"] = claimed
        print(json.dumps(value, ensure_ascii=False, default=str))
        if args.command != "run-job":
            return 0
        return (
            0
            if value["status"] == "SUCCEEDED"
            and (value.get("result") or {}).get("status") != "PARTIAL"
            else 3
        )
    finally:
        container.close()


if __name__ == "__main__":
    raise SystemExit(main())
