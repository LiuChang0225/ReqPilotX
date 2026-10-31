from __future__ import annotations

import argparse
import logging
import os
import signal
import uuid
from collections.abc import Mapping
from threading import Event

from reqpilotx_server.infrastructure.runtime_container import build_runtime_container
from reqpilotx_server.infrastructure.runtime_settings import ProcessRole, RuntimeSettings
from reqpilotx_server.services.ai_copilot import reload_runtime_ai_config_from_file
from reqpilotx_server.application.extraction.source_asset_objects import (
    recover_stale_source_asset_publications,
)
from reqpilotx_server.worker.handlers import build_job_handlers
from reqpilotx_server.worker.runtime import WorkerRuntime
from reqpilotx_server.composition.trust import configure_host_truststore


def _official_local_worker_id(
    *,
    accepted_kinds: tuple[str, ...],
    claim_profile: str,
    environment: Mapping[str, str],
) -> str:
    """Fail closed when a marked local slot is started with the wrong lane."""

    raw_slot = str(environment.get("REQPILOT_LOCAL_WORKER_SLOT") or "").strip()
    if not raw_slot:
        return ""
    raw_count = str(environment.get("REQPILOT_LOCAL_WORKER_COUNT") or "").strip()
    if raw_slot not in {"1", "2"} or raw_count not in {"1", "2"}:
        raise SystemExit("Local worker slot/count markers must be 1 or 2.")
    slot = int(raw_slot)
    count = int(raw_count)
    if slot > count:
        raise SystemExit("Local worker slot exceeds the configured pool capacity.")
    expected_kinds = () if slot == 1 else ("document.extract",)
    expected_profile = "general" if slot == 1 else "local-extraction-no-llm"
    if accepted_kinds != expected_kinds or claim_profile != expected_profile:
        raise SystemExit(
            f"Local worker slot {slot} has an unsafe durable claim lane."
        )
    instance_id = str(
        environment.get("REQPILOT_LOCAL_RUNTIME_INSTANCE_ID") or ""
    ).strip()
    if not instance_id or len(instance_id) > 96:
        raise SystemExit("Local worker runtime instance marker is missing or invalid.")
    return f"local-{instance_id}-slot-{slot}-{uuid.uuid4().hex[:12]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the ReqPilotX durable worker.")
    parser.add_argument("--once", action="store_true", help="Claim at most one job.")
    parser.add_argument(
        "--job-id",
        default="",
        help="Run only this existing durable job; requires --once and skips global maintenance.",
    )
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--accepted-kind",
        action="append",
        default=[],
        help=(
            "Limit this process to a durable job kind. Repeat for multiple kinds; "
            "the default accepts the complete registered set."
        ),
    )
    parser.add_argument(
        "--claim-profile",
        choices=("general", "local-extraction-no-llm"),
        default="general",
        help="Apply a server-owned durable claim lane policy.",
    )
    return parser


def main() -> int:
    configure_host_truststore()
    args = build_parser().parse_args()
    if args.job_id and not args.once:
        raise SystemExit("--job-id requires --once.")
    logging.basicConfig(level=logging.INFO)
    accepted_kinds = tuple(dict.fromkeys(str(item) for item in args.accepted_kind))
    worker_id = _official_local_worker_id(
        accepted_kinds=accepted_kinds,
        claim_profile=args.claim_profile,
        environment=os.environ,
    )
    settings = RuntimeSettings.from_environment()
    if settings.process_role is not ProcessRole.WORKER:
        raise SystemExit(
            "Set REQPILOT_PROCESS_ROLE=worker before starting the worker."
        )
    if settings.is_sqlite:
        raise SystemExit("The durable worker requires PostgreSQL.")
    container = build_runtime_container(settings)
    if container.jobs is None:
        raise SystemExit("The durable worker requires PostgreSQL.")
    handlers = build_job_handlers(container)
    if args.job_id:
        selected_job = container.jobs.get(args.job_id)
        if selected_job is None:
            container.close()
            raise SystemExit("The selected durable job does not exist.")
        if accepted_kinds and selected_job.kind not in accepted_kinds:
            container.close()
            raise SystemExit("The selected job is outside the accepted kinds.")
        accepted_kinds = (selected_job.kind,)
    unknown_kinds = tuple(kind for kind in accepted_kinds if kind not in handlers)
    if unknown_kinds:
        container.close()
        raise SystemExit(
            "Unknown durable job kind(s): " + ", ".join(unknown_kinds)
        )
    if accepted_kinds:
        handlers = {kind: handlers[kind] for kind in accepted_kinds}
    logging.info(
        "Durable worker lane ready. accepted_kinds=%s",
        ",".join(sorted(handlers)),
    )
    runtime = WorkerRuntime(
        queue=container.jobs,
        handlers=handlers,
        worker_id=worker_id,
        lease_seconds=args.lease_seconds,
        poll_seconds=args.poll_seconds,
        maintenance=(
            recover_stale_source_asset_publications
            if args.claim_profile == "general" and not args.job_id
            else None
        ),
        claim_profile=args.claim_profile,
        worker_slot=str(os.getenv("REQPILOT_LOCAL_WORKER_SLOT") or "unassigned"),
        runtime_instance_id=str(
            os.getenv("REQPILOT_LOCAL_RUNTIME_INSTANCE_ID")
            or "runtime-unassigned"
        ),
        source_revision=str(
            os.getenv("REQPILOT_RUNTIME_SOURCE_REVISION") or "untracked"
        ),
        reload_runtime_config=reload_runtime_ai_config_from_file,
        job_id=args.job_id,
    )
    stop = Event()

    def request_stop(_signum, _frame) -> None:
        runtime.request_drain()
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        runtime.start()
        if args.once:
            runtime.run_once()
        else:
            runtime.run_forever(should_stop=stop.is_set)
    finally:
        runtime.close()
        container.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
