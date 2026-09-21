from __future__ import annotations

import logging
import re
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, RLock, Thread
from time import sleep
from typing import Any

from reqpilotx_server.ports.job_queue import (
    DurableJob,
    JobLeaseFence,
    JobQueue,
    NonRetryableJobError,
    WorkerFleetState,
)
from reqpilotx_server.ports.job_queue import (
    JobExecutionContext as JobExecutionContextPort,
)

logger = logging.getLogger(__name__)
JobHandler = Callable[[DurableJob, "JobExecutionContext"], dict[str, Any]]


@dataclass(slots=True)
class JobExecutionContext(JobExecutionContextPort):
    queue: JobQueue
    job: DurableJob
    worker_id: str
    lease_seconds: int
    _lock: RLock = field(default_factory=RLock, repr=False)

    def heartbeat(self, progress: dict[str, Any] | None = None) -> DurableJob:
        with self._lock:
            self.job = self.queue.heartbeat(
                job_id=self.job.job_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                progress=progress,
            )
            return self.job

    def cancellation_requested(self) -> bool:
        return bool(self.heartbeat().cancel_requested)

    def lease_fence(self) -> JobLeaseFence:
        with self._lock:
            return JobLeaseFence(
                job_id=self.job.job_id,
                generation=self.job.generation,
                worker_id=self.worker_id,
            )

    def begin_publication(self) -> DurableJob:
        with self._lock:
            self.job = self.queue.mark_non_cancellable(
                job_id=self.job.job_id,
                worker_id=self.worker_id,
            )
            return self.job


class WorkerRuntime:
    FLEET_HEARTBEAT_SECONDS = 5.0

    def __init__(
        self,
        *,
        queue: JobQueue,
        handlers: dict[str, JobHandler],
        worker_id: str = "",
        lease_seconds: int = 120,
        poll_seconds: float = 1.0,
        maintenance: Callable[[], None] | None = None,
        claim_profile: str = "general",
        worker_slot: str = "unassigned",
        runtime_instance_id: str = "runtime-unassigned",
        source_revision: str = "untracked",
        reload_runtime_config: Callable[[], int] | None = None,
        fleet_heartbeat_seconds: float = FLEET_HEARTBEAT_SECONDS,
        job_id: str = "",
        required_entrypoint: str = "",
    ) -> None:
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600.")
        if not 0.1 <= poll_seconds <= 30:
            raise ValueError("poll_seconds must be between 0.1 and 30.")
        if not handlers:
            raise ValueError("At least one durable job handler must be registered.")
        self.queue = queue
        if job_id and not re.fullmatch(r"job-[0-9a-f]{32}", job_id):
            raise ValueError("job_id must be one exact durable job identity.")
        self.job_id = job_id
        self.required_entrypoint = required_entrypoint
        self.handlers = dict(handlers)
        self.worker_id = worker_id or (
            f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        )
        self.lease_seconds = lease_seconds
        self.poll_seconds = float(poll_seconds)
        self.maintenance = maintenance
        if claim_profile not in {"general", "local-extraction-no-llm"}:
            raise ValueError("Unsupported durable worker claim profile.")
        self.claim_profile = claim_profile
        if not 0.01 <= fleet_heartbeat_seconds <= 60.0:
            raise ValueError("fleet_heartbeat_seconds must be between 0.01 and 60.")
        self.worker_slot = str(worker_slot or "unassigned").strip()
        self.runtime_instance_id = str(
            runtime_instance_id or "runtime-unassigned"
        ).strip()
        self.source_revision = str(source_revision or "untracked").strip()
        self.accepted_kinds = tuple(sorted(self.handlers))
        self.reload_runtime_config = reload_runtime_config
        self.fleet_heartbeat_seconds = float(fleet_heartbeat_seconds)
        self.observed_connection_revision = 0
        self._fleet_lock = RLock()
        self._fleet_state = WorkerFleetState.IDLE
        self._fleet_started = False
        self._fleet_draining = False
        self._fleet_stop = Event()
        self._fleet_thread: Thread | None = None

    def _worker_presence(self) -> dict[str, Any]:
        with self._fleet_lock:
            return {
                "worker_id": self.worker_id,
                "slot": self.worker_slot,
                "claim_profile": self.claim_profile,
                "accepted_kinds": self.accepted_kinds,
                "connection_revision": self.observed_connection_revision,
                "source_revision": self.source_revision,
                "runtime_instance_id": self.runtime_instance_id,
                "state": self._fleet_state.value,
            }

    def _publish_worker_heartbeat(self) -> None:
        if not self._fleet_started:
            return
        try:
            self.queue.heartbeat_worker(**self._worker_presence())
        except Exception:
            logger.exception("Durable worker fleet heartbeat was deferred.")

    def _set_worker_state(self, state: WorkerFleetState) -> None:
        with self._fleet_lock:
            if self._fleet_draining and state is not WorkerFleetState.DRAINING:
                state = WorkerFleetState.DRAINING
            self._fleet_state = state
        self._publish_worker_heartbeat()

    def _reload_at_idle_boundary(self) -> bool:
        if self.reload_runtime_config is None:
            revision = self.observed_connection_revision
        else:
            try:
                revision = self.reload_runtime_config()
            except Exception:
                logger.exception("Durable worker runtime config reload was deferred.")
                return False
        if isinstance(revision, bool):
            logger.error("Durable worker observed an invalid connection revision.")
            return False
        try:
            normalized_revision = int(revision)
        except (TypeError, ValueError):
            logger.error("Durable worker observed an invalid connection revision.")
            return False
        if normalized_revision < 0:
            logger.error("Durable worker observed an invalid connection revision.")
            return False
        with self._fleet_lock:
            if self._fleet_state is WorkerFleetState.ACTIVE:
                raise RuntimeError("Active durable handlers cannot reload runtime config.")
            self.observed_connection_revision = normalized_revision
        self._publish_worker_heartbeat()
        return True

    def start(self) -> None:
        if self._fleet_started:
            return
        if not self._reload_at_idle_boundary():
            raise RuntimeError("Durable worker runtime config is not reloadable.")
        with self._fleet_lock:
            self._fleet_state = (
                WorkerFleetState.DRAINING
                if self._fleet_draining
                else WorkerFleetState.IDLE
            )
        self.queue.register_worker(**self._worker_presence())
        self._fleet_started = True
        self._fleet_stop.clear()

        def heartbeat_loop() -> None:
            while not self._fleet_stop.wait(self.fleet_heartbeat_seconds):
                self._publish_worker_heartbeat()

        self._fleet_thread = Thread(
            target=heartbeat_loop,
            name="reqpilotx-worker-fleet-heartbeat",
            daemon=True,
        )
        self._fleet_thread.start()

    def request_drain(self) -> None:
        with self._fleet_lock:
            self._fleet_draining = True
            self._fleet_state = WorkerFleetState.DRAINING
        self._publish_worker_heartbeat()

    def close(self) -> None:
        if not self._fleet_started:
            return
        self.request_drain()
        self._fleet_stop.set()
        if self._fleet_thread is not None:
            self._fleet_thread.join(timeout=2.0)
        try:
            self.queue.unregister_worker(worker_id=self.worker_id)
        except Exception:
            logger.exception("Durable worker fleet unregister was deferred.")
        finally:
            self._fleet_started = False
            self._fleet_thread = None

    def run_once(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> bool:
        stop_requested = should_stop or (lambda: False)
        if stop_requested():
            self.request_drain()
            return False
        if self._fleet_draining or not self._reload_at_idle_boundary():
            return False
        if self.maintenance is not None:
            try:
                self.maintenance()
            except Exception:
                # Maintenance is recoverable and must not stop durable job
                # delivery. Its own durable inventory is retried next poll.
                logger.exception("Durable worker maintenance was deferred.")
        # SIGTERM may arrive while maintenance is running. Check again at the
        # last application boundary before entering the atomic DB claim.
        if stop_requested():
            self.request_drain()
            return False
        try:
            job = self.queue.claim(
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                accepted_kinds=tuple(sorted(self.handlers)),
                claim_profile=self.claim_profile,
                observed_connection_revision=self.observed_connection_revision,
                **({"job_id": self.job_id} if self.job_id else {}),
                **({"required_entrypoint": self.required_entrypoint} if self.required_entrypoint else {}),
            )
        except Exception:
            # A temporary database/socket failure while polling must not stop
            # the process and strand every pending delivery. Treat it like an
            # empty poll so run_forever applies the normal retry delay.
            logger.exception("Durable job claim was deferred.")
            return False
        if job is None:
            return False
        if stop_requested():
            self.request_drain()
            # A drain may race with the short PostgreSQL claim. Return that
            # untouched lease immediately so a new runtime can claim it; do
            # not begin a potentially hours-long handler after SIGTERM.
            try:
                self.queue.release_claim(
                    job_id=job.job_id,
                    worker_id=self.worker_id,
                )
            except Exception:
                # The lease fence may already have moved. Never execute work
                # after drain in this branch; expiry/retry owns recovery.
                logger.exception(
                    "Durable claim release was deferred during drain. "
                    "job_id=%s kind=%s",
                    job.job_id,
                    job.kind,
                )
            return False
        self._set_worker_state(WorkerFleetState.ACTIVE)
        handler = self.handlers[job.kind]
        context = JobExecutionContext(
            queue=self.queue,
            job=job,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        keepalive_stop = Event()
        keepalive_error: list[BaseException] = []

        def keep_lease_alive() -> None:
            interval = min(max(self.lease_seconds / 3, 1.0), 30.0)
            while not keepalive_stop.wait(interval):
                try:
                    context.heartbeat()
                except BaseException as exc:
                    keepalive_error.append(exc)
                    keepalive_stop.set()
                    return

        keepalive = Thread(
            target=keep_lease_alive,
            name=f"reqpilotx-job-heartbeat-{job.job_id[-12:]}",
            daemon=True,
        )
        keepalive.start()
        try:
            result = handler(job, context)
        except Exception as exc:
            if isinstance(exc, NonRetryableJobError):
                error_code = exc.error_code
                retry_delay_seconds = None
                logger.warning(
                    "Durable job was rejected as non-retryable. "
                    "job_id=%s kind=%s error_code=%s",
                    job.job_id,
                    job.kind,
                    error_code,
                )
            else:
                reason_code = getattr(exc, "reason_code", "")
                error_code = reason_code if isinstance(reason_code, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", reason_code) else type(exc).__name__
                retry_delay_seconds = 30
                logger.exception(
                    "Durable job failed. job_id=%s kind=%s error_type=%s",
                    job.job_id,
                    job.kind,
                    type(exc).__name__,
                )
            try:
                self.queue.fail(
                    job_id=job.job_id,
                    worker_id=self.worker_id,
                    error_code=error_code,
                    error_message=str(exc),
                    retry_delay_seconds=retry_delay_seconds,
                )
            except Exception:
                # Failure publication is also lease-bound. If this delivery
                # lost its lease, a newer generation owns the job; if transport
                # failed, the current lease will expire and make it claimable.
                logger.exception(
                    "Durable job failure publication was deferred. "
                    "job_id=%s kind=%s",
                    job.job_id,
                    job.kind,
                )
        else:
            if keepalive_error:
                try:
                    context.heartbeat()
                except BaseException:
                    logger.error(
                        "Durable job completion was deferred after lease heartbeat "
                        "failure. job_id=%s kind=%s",
                        job.job_id,
                        job.kind,
                    )
                    return True
            try:
                self.queue.succeed(
                    job_id=job.job_id,
                    worker_id=self.worker_id,
                    result=result,
                )
            except BaseException:
                # Completion publication is the final lease-bound operation.
                # A transient database/socket failure must not terminate the
                # durable worker; the still-running job becomes claimable
                # again after its lease expires and the handler is idempotent.
                logger.exception(
                    "Durable job completion publication was deferred. "
                    "job_id=%s kind=%s",
                    job.job_id,
                    job.kind,
                )
        finally:
            keepalive_stop.set()
            keepalive.join(timeout=2.0)
            self._set_worker_state(
                WorkerFleetState.DRAINING
                if self._fleet_draining
                else WorkerFleetState.IDLE
            )
        return True

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            if not self.run_once(should_stop=should_stop):
                if should_stop():
                    break
                sleep(self.poll_seconds)


__all__ = ["JobExecutionContext", "JobHandler", "WorkerRuntime"]
