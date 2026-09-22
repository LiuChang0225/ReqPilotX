from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkerFleetState(StrEnum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"


class JobLeaseLostError(RuntimeError):
    """Raised when a durable delivery no longer owns its worker lease."""


class NonRetryableJobError(RuntimeError):
    """Reject a deterministic delivery without consuming further attempts."""

    def __init__(self, *, error_code: str, message: str) -> None:
        normalized_code = str(error_code or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", normalized_code):
            raise ValueError("error_code must be a stable non-blank token.")
        normalized_message = str(message or "").strip()
        if not normalized_message:
            raise ValueError("message must be non-blank.")
        super().__init__(normalized_message)
        self.error_code = normalized_code


@dataclass(frozen=True, slots=True)
class DurableJob:
    job_id: str
    kind: str
    aggregate_type: str
    aggregate_id: str
    idempotency_key: str
    payload: dict[str, Any]
    status: JobStatus
    priority: int
    attempts: int
    max_attempts: int
    generation: int
    cancel_requested: bool
    lease_owner: str
    lease_expires_at: datetime | None
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str
    error_message: str


@dataclass(frozen=True, slots=True)
class JobLeaseFence:
    """Immutable owner identity used by business writes under a durable lease."""

    job_id: str
    generation: int
    worker_id: str


class JobExecutionContext(ABC):
    """Application-facing control surface for one leased durable job."""

    @abstractmethod
    def heartbeat(self, progress: dict[str, Any] | None = None) -> DurableJob:
        raise NotImplementedError

    @abstractmethod
    def cancellation_requested(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def lease_fence(self) -> JobLeaseFence:
        raise NotImplementedError

    @abstractmethod
    def begin_publication(self) -> DurableJob:
        raise NotImplementedError


@runtime_checkable
class JobQueue(Protocol):
    def enqueue(
        self,
        *,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        priority: int = 0,
        max_attempts: int = 3,
    ) -> DurableJob: ...

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        accepted_kinds: tuple[str, ...] = (),
        claim_profile: str = "general",
        observed_connection_revision: int = 0,
        job_id: str = "",
        required_entrypoint: str = "",
    ) -> DurableJob | None: ...

    def register_worker(
        self,
        *,
        worker_id: str,
        slot: str,
        claim_profile: str,
        accepted_kinds: tuple[str, ...],
        connection_revision: int,
        source_revision: str,
        runtime_instance_id: str,
        state: str,
    ) -> None: ...

    def heartbeat_worker(
        self,
        *,
        worker_id: str,
        slot: str,
        claim_profile: str,
        accepted_kinds: tuple[str, ...],
        connection_revision: int,
        source_revision: str,
        runtime_instance_id: str,
        state: str,
    ) -> None: ...

    def unregister_worker(self, *, worker_id: str) -> None: ...

    def worker_fleet_status(
        self,
        *,
        desired_revision: int,
        required_kind: str,
        plan: dict[str, Any] | None = None,
        require_connection_revision: bool | None = None,
    ) -> dict[str, Any]: ...

    def release_claim(
        self,
        *,
        job_id: str,
        worker_id: str,
    ) -> DurableJob: ...

    def heartbeat(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
        progress: dict[str, Any] | None = None,
    ) -> DurableJob: ...

    def mark_non_cancellable(
        self,
        *,
        job_id: str,
        worker_id: str,
    ) -> DurableJob: ...

    def succeed(
        self,
        *,
        job_id: str,
        worker_id: str,
        result: dict[str, Any],
    ) -> DurableJob: ...

    def fail(
        self,
        *,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str,
        retry_delay_seconds: int | None,
    ) -> DurableJob: ...

    def request_cancel(self, job_id: str) -> DurableJob | None: ...

    def supersede_pending(
        self,
        job_id: str,
        *,
        reason_code: str,
    ) -> DurableJob | None: ...

    def get(self, job_id: str) -> DurableJob | None: ...

    def get_by_idempotency(
        self,
        *,
        kind: str,
        idempotency_key: str,
    ) -> DurableJob | None: ...

    def get_latest(
        self,
        *,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
    ) -> DurableJob | None: ...

    def get_latest_succeeded(
        self,
        *,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload_filters: dict[str, Any] | None = None,
        require_result_key: str = "",
    ) -> DurableJob | None: ...

    def compare_and_set_succeeded_result(
        self,
        *,
        job_id: str,
        expected_result: dict[str, Any],
        result: dict[str, Any],
        artifact_operation_id: str = "",
    ) -> DurableJob | None: ...


__all__ = [
    "DurableJob",
    "JobExecutionContext",
    "JobLeaseFence",
    "JobLeaseLostError",
    "JobQueue",
    "JobStatus",
    "NonRetryableJobError",
    "WorkerFleetState",
]
