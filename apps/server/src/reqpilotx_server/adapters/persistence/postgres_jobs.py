from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any

from reqpilotx_server.infrastructure.postgres import PostgresDatabase
from reqpilotx_server.ports.job_queue import (
    DurableJob,
    JobLeaseLostError,
    JobStatus,
    WorkerFleetState,
)


_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class JobConflictError(RuntimeError):
    """Raised when an idempotency identity is reused with different input."""


class PostgresJobQueue:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

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
    ) -> DurableJob:
        kind = _validated_token(kind, "kind")
        aggregate_type = _validated_token(aggregate_type, "aggregate_type")
        aggregate_id = _validated_token(aggregate_id, "aggregate_id")
        idempotency_key = _validated_token(idempotency_key, "idempotency_key")
        if not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20.")
        canonical_payload = _canonical_json(payload)
        payload_sha256 = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        job_id = f"job-{uuid.uuid4().hex}"
        with self.database.connection() as connection:
            # A new workbench enqueue must not reap unrelated legacy leases.
            entrypoint = (
                "source-item-workbench-v1"
                if payload.get("application_entrypoint") == "source-item-workbench-v1"
                else ""
            )
            _reap_expired_exhausted_jobs(connection, required_entrypoint=entrypoint)
            row = connection.execute(
                """
                INSERT INTO runtime_jobs (
                    id, kind, aggregate_type, aggregate_id, idempotency_key,
                    payload, payload_sha256, priority, max_attempts
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    job_id,
                    kind,
                    aggregate_type,
                    aggregate_id,
                    idempotency_key,
                    canonical_payload,
                    payload_sha256,
                    int(priority),
                    max_attempts,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM runtime_jobs
                    WHERE kind = %s AND idempotency_key = %s
                    """,
                    (kind, idempotency_key),
                ).fetchone()
                if row is None:
                    active = connection.execute(
                        """
                        SELECT *
                        FROM runtime_jobs
                        WHERE kind = %s
                          AND aggregate_type = %s
                          AND aggregate_id = %s
                          AND status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (kind, aggregate_type, aggregate_id),
                    ).fetchone()
                    if active is not None:
                        raise JobConflictError(
                            "Another active job already owns this aggregate."
                        )
                    raise RuntimeError("Job insert conflicted without an existing row.")
                if str(row["payload_sha256"]) != payload_sha256:
                    raise JobConflictError(
                        "Job idempotency key is already bound to different input."
                    )
        return _job_from_row(row)

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
    ) -> None:
        self._write_worker_presence(
            worker_id=worker_id,
            slot=slot,
            claim_profile=claim_profile,
            accepted_kinds=accepted_kinds,
            connection_revision=connection_revision,
            source_revision=source_revision,
            runtime_instance_id=runtime_instance_id,
            state=state,
            reset_registration=True,
        )

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
    ) -> None:
        self._write_worker_presence(
            worker_id=worker_id,
            slot=slot,
            claim_profile=claim_profile,
            accepted_kinds=accepted_kinds,
            connection_revision=connection_revision,
            source_revision=source_revision,
            runtime_instance_id=runtime_instance_id,
            state=state,
            reset_registration=False,
        )

    def _write_worker_presence(
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
        reset_registration: bool,
    ) -> None:
        identity = _validated_worker_identity(
            worker_id=worker_id,
            slot=slot,
            claim_profile=claim_profile,
            accepted_kinds=accepted_kinds,
            connection_revision=connection_revision,
            source_revision=source_revision,
            runtime_instance_id=runtime_instance_id,
            state=state,
        )
        registered_at_update = (
            "registered_at = now()," if reset_registration else ""
        )
        with self.database.connection() as connection:
            connection.execute(
                f"""
                INSERT INTO runtime_worker_fleet (
                    worker_id, slot, claim_profile, accepted_kinds,
                    connection_revision, source_revision, runtime_instance_id,
                    state, registered_at, last_heartbeat_at, updated_at
                )
                VALUES (%s, %s, %s, %s::text[], %s, %s, %s, %s, now(), now(), now())
                ON CONFLICT (worker_id) DO UPDATE
                SET slot = EXCLUDED.slot,
                    claim_profile = EXCLUDED.claim_profile,
                    accepted_kinds = EXCLUDED.accepted_kinds,
                    connection_revision = EXCLUDED.connection_revision,
                    source_revision = EXCLUDED.source_revision,
                    runtime_instance_id = EXCLUDED.runtime_instance_id,
                    state = EXCLUDED.state,
                    {registered_at_update}
                    last_heartbeat_at = now(),
                    updated_at = now()
                """,
                (
                    identity["worker_id"],
                    identity["slot"],
                    identity["claim_profile"],
                    list(identity["accepted_kinds"]),
                    identity["connection_revision"],
                    identity["source_revision"],
                    identity["runtime_instance_id"],
                    identity["state"],
                ),
            )

    def unregister_worker(self, *, worker_id: str) -> None:
        worker_id = _validated_token(worker_id, "worker_id")
        with self.database.connection() as connection:
            connection.execute(
                "DELETE FROM runtime_worker_fleet WHERE worker_id = %s",
                (worker_id,),
            )

    def worker_fleet_status(
        self,
        *,
        desired_revision: int,
        required_kind: str,
        plan: dict[str, Any] | None = None,
        require_connection_revision: bool | None = None,
    ) -> dict[str, Any]:
        desired_revision = _validated_revision(desired_revision)
        required_kind = _validated_token(required_kind, "required_kind")
        frozen_plan = dict(plan or {})
        if require_connection_revision is None:
            revision_required = _plan_requires_connection_revision(frozen_plan)
        elif type(require_connection_revision) is bool:
            revision_required = require_connection_revision
        else:
            raise ValueError("require_connection_revision must be a boolean or null.")
        desired_source_revision = str(
            frozen_plan.get("source_revision")
            or frozen_plan.get("runtime_source_revision")
            or os.getenv("REQPILOT_RUNTIME_SOURCE_REVISION")
            or ""
        ).strip()
        desired_runtime_instance = str(
            frozen_plan.get("runtime_instance_id")
            or os.getenv("REQPILOT_LOCAL_RUNTIME_INSTANCE_ID")
            or ""
        ).strip()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT worker_id, slot, claim_profile, accepted_kinds,
                       connection_revision, source_revision,
                       runtime_instance_id, state,
                       last_heartbeat_at >= now() - interval '15 seconds' AS fresh,
                       GREATEST(
                           0,
                           FLOOR(EXTRACT(EPOCH FROM (now() - last_heartbeat_at)))
                       )::bigint AS last_seen_age_seconds
                FROM runtime_worker_fleet
                ORDER BY slot ASC, claim_profile ASC, registered_at ASC
                """
            ).fetchall()
        workers: list[dict[str, Any]] = []
        for raw_row in rows:
            row = dict(raw_row)
            accepted = tuple(str(item) for item in (row.get("accepted_kinds") or ()))
            fresh = bool(row.get("fresh"))
            state = str(row.get("state") or "")
            revision_match = int(row.get("connection_revision") or 0) == desired_revision
            source_revision_match = bool(
                not desired_source_revision
                or str(row.get("source_revision") or "")
                == desired_source_revision
            )
            runtime_instance_match = bool(
                not desired_runtime_instance
                or str(row.get("runtime_instance_id") or "")
                == desired_runtime_instance
            )
            claim_profile_match = _claim_profile_accepts_plan(
                claim_profile=str(row.get("claim_profile") or ""),
                required_kind=required_kind,
                plan=frozen_plan,
            )
            eligible = bool(
                fresh
                and required_kind in accepted
                and state != WorkerFleetState.DRAINING.value
                and (not revision_required or revision_match)
                and source_revision_match
                and runtime_instance_match
                and claim_profile_match
            )
            workers.append(
                {
                    "slot": str(row.get("slot") or ""),
                    "claim_profile": str(row.get("claim_profile") or ""),
                    "accepted_kinds": list(accepted),
                    "connection_revision": int(
                        row.get("connection_revision") or 0
                    ),
                    "state": state,
                    "fresh": fresh,
                    "revision_match": revision_match,
                    "eligible": eligible,
                    "last_seen_age_seconds": max(
                        0, int(row.get("last_seen_age_seconds") or 0)
                    ),
                    "source_revision_match": source_revision_match,
                    "runtime_instance_match": runtime_instance_match,
                    "claim_profile_match": claim_profile_match,
                }
            )
        live = [item for item in workers if item["fresh"]]
        eligible_workers = [item for item in live if item["eligible"]]
        required_kind_workers = [
            item
            for item in live
            if required_kind in item["accepted_kinds"]
            and item["state"] != WorkerFleetState.DRAINING.value
            and item["claim_profile_match"]
        ]
        if eligible_workers:
            fleet_state = "READY"
        elif required_kind_workers:
            fleet_state = "SYNCING"
        else:
            fleet_state = "UNAVAILABLE"
        return {
            "schema_version": "runtime-worker-fleet-status/v1",
            "state": fleet_state,
            "desired_connection_revision": desired_revision,
            "required_kind": required_kind,
            "revision_required": revision_required,
            "registered_worker_count": len(workers),
            "live_worker_count": len(live),
            "eligible_worker_count": len(eligible_workers),
            "idle_worker_count": sum(
                item["state"] == WorkerFleetState.IDLE.value for item in live
            ),
            "active_worker_count": sum(
                item["state"] == WorkerFleetState.ACTIVE.value for item in live
            ),
            "draining_worker_count": sum(
                item["state"] == WorkerFleetState.DRAINING.value for item in live
            ),
            "stale_revision_worker_count": sum(
                revision_required
                and required_kind in item["accepted_kinds"]
                and item["claim_profile_match"]
                and not item["revision_match"]
                for item in live
            ),
            "workers": workers,
        }

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
    ) -> DurableJob | None:
        worker_id = _validated_token(worker_id, "worker_id")
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600.")
        kinds = tuple(
            _validated_token(item, "accepted_kind") for item in accepted_kinds
        )
        claim_profile = _validated_token(claim_profile, "claim_profile")
        if claim_profile not in {"general", "local-extraction-no-llm"}:
            raise ValueError("Unsupported durable worker claim profile.")
        observed_connection_revision = _validated_revision(
            observed_connection_revision
        )
        selected_job_id = _validated_token(job_id, "job_id") if job_id else ""
        entrypoint = _validated_token(required_entrypoint, "required_entrypoint") if required_entrypoint else ""
        with self.database.connection() as connection:
            _reap_expired_exhausted_jobs(connection, job_id=selected_job_id, required_entrypoint=entrypoint)
            # Claim selection is serialized only for this short transaction.
            # The mutex closes the race where two workers could otherwise pick
            # different job kinds for the same document before either update
            # became visible. Job execution remains fully concurrent after the
            # transaction commits.
            connection.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (785361021117,),
            )
            row = connection.execute(
                """
                WITH candidate AS (
                    SELECT queued.id
                    FROM runtime_jobs AS queued
                    WHERE (
                        (queued.status = 'PENDING' AND queued.available_at <= now())
                        OR (
                            queued.status IN ('RUNNING', 'CANCEL_REQUESTED')
                            AND queued.lease_expires_at < now()
                        )
                    )
                    AND queued.attempts < queued.max_attempts
                    AND (%s = '' OR queued.id = %s)
                    AND (%s = '' OR queued.payload->>'application_entrypoint' = %s)
                    AND (
                        queued.kind <> 'agent.source-item.images'
                        OR queued.payload->>'application_entrypoint'
                            IS DISTINCT FROM 'source-item-workbench-v1'
                        OR (
                            jsonb_typeof(COALESCE(
                                queued.payload->'dependency_job_ids', '[]'::jsonb
                            )) = 'array'
                            AND NOT EXISTS (
                                SELECT 1
                                FROM jsonb_array_elements(
                                    CASE WHEN jsonb_typeof(
                                        queued.payload->'dependency_job_ids'
                                    ) = 'array'
                                    THEN queued.payload->'dependency_job_ids'
                                    ELSE '[]'::jsonb END
                                ) AS prerequisite(job_id)
                                LEFT JOIN runtime_jobs AS dependency
                                    ON dependency.id = prerequisite.job_id #>> '{}'
                                WHERE jsonb_typeof(prerequisite.job_id) <> 'string'
                                   OR dependency.id IS NULL
                                   OR dependency.status <> 'SUCCEEDED'
                            )
                        )
                    )
                    AND (
                        %s::text[] = '{}'::text[]
                        OR queued.kind = ANY(%s::text[])
                    )
                    AND (
                        %s = 'general'
                        OR (
                            %s = 'local-extraction-no-llm'
                            AND queued.kind = 'document.extract'
                            AND queued.payload #>> '{processing_plan,llm_provider}' = 'none'
                        )
                    )
                    AND (
                        queued.kind <> 'document.extract'
                        OR CASE
                            WHEN jsonb_typeof(
                                queued.payload->'execution_requirements'
                            ) = 'object'
                            THEN CASE
                                WHEN queued.payload #>> '{execution_requirements,schema_version}'
                                    IS DISTINCT FROM 'document-extraction-execution-requirements/v1'
                                THEN FALSE
                                WHEN queued.payload #>> '{execution_requirements,connection_revision_required}' = 'false'
                                THEN TRUE
                                WHEN queued.payload #>> '{execution_requirements,connection_revision_required}' = 'true'
                                THEN queued.payload #>> '{execution_requirements,required_connection_revision}' = %s::text
                                ELSE FALSE
                            END
                            ELSE (
                                NOT (
                                    COALESCE(
                                        queued.payload #>> '{processing_plan,provider_bindings,primary_parser_provider}',
                                        ''
                                    ) <> ''
                                    AND COALESCE(
                                        queued.payload #>> '{processing_plan,provider_bindings,primary_parser_provider}',
                                        ''
                                    ) NOT LIKE 'local:%%'
                                    OR CASE WHEN jsonb_typeof(
                                        queued.payload #> '{processing_plan,provider_bindings,enhancement_providers}'
                                    ) = 'array' THEN jsonb_array_length(
                                        queued.payload #> '{processing_plan,provider_bindings,enhancement_providers}'
                                    ) ELSE 0 END > 0
                                    OR COALESCE(
                                        queued.payload #>> '{processing_plan,provider_bindings,complex_vision_provider}',
                                        ''
                                    ) <> ''
                                    OR COALESCE(
                                        queued.payload #>> '{processing_plan,provider_bindings,llm_transport_provider}',
                                        ''
                                    ) <> ''
                                    OR COALESCE(
                                        queued.payload #>> '{processing_plan,document,primary_parser}',
                                        ''
                                    ) <> 'pymupdf'
                                    OR CASE WHEN jsonb_typeof(
                                        queued.payload #> '{processing_plan,document,enhancement_chain}'
                                    ) = 'array' THEN jsonb_array_length(
                                        queued.payload #> '{processing_plan,document,enhancement_chain}'
                                    ) ELSE 0 END > 0
                                    OR COALESCE(
                                        queued.payload #>> '{processing_plan,document,complex_vision}',
                                        ''
                                    ) <> ''
                                    OR COALESCE(
                                        queued.payload #>> '{processing_plan,llm_provider}',
                                        'none'
                                    ) <> 'none'
                                )
                                OR queued.payload #>> '{processing_plan,connection_revision}' = %s::text
                            )
                        END
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM runtime_jobs AS active
                        WHERE active.id <> queued.id
                          AND active.status IN ('RUNNING', 'CANCEL_REQUESTED')
                          AND active.lease_expires_at >= now()
                          AND (
                              (
                                  (queued.kind = 'agent.document-map' OR active.kind = 'agent.document-map')
                                  AND active.aggregate_type = queued.aggregate_type
                                  AND active.aggregate_id = queued.aggregate_id
                              )
                              OR
                              (
                                  queued.kind IN (
                                      'document.extract',
                                      'document.restore',
                                      'structure.optimize',
                                      'agent.source-requirement.baseline',
                                      'agent.source-item-text.baseline',
                                      'agent.source-item.scope-revision',
                                      'agent.source-item.decompose'
                                      ,'agent.source-item.classify', 'agent.source-item.images'
                                  )
                                  AND active.kind IN (
                                      'document.extract',
                                      'document.restore',
                                      'structure.optimize',
                                      'agent.source-requirement.baseline',
                                      'agent.source-item-text.baseline',
                                      'agent.source-item.scope-revision',
                                      'agent.source-item.decompose'
                                      ,'agent.source-item.classify', 'agent.source-item.images'
                                  )
                                  AND active.aggregate_type = queued.aggregate_type
                                  AND active.aggregate_id = queued.aggregate_id
                              )
                              OR (
                                  queued.kind = 'knowledge.sync'
                                  AND active.kind = 'knowledge.sync'
                              )
                          )
                    )
                    ORDER BY queued.priority DESC, queued.created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE runtime_jobs AS job
                SET status = CASE
                        WHEN job.cancel_requested THEN 'CANCEL_REQUESTED'
                        ELSE 'RUNNING'
                    END,
                    attempts = job.attempts + 1,
                    lease_owner = %s,
                    lease_expires_at = now() + make_interval(secs => %s),
                    started_at = COALESCE(job.started_at, now()),
                    updated_at = now(),
                    generation = job.generation + 1
                FROM candidate
                WHERE job.id = candidate.id
                RETURNING job.*
                """,
                (
                    selected_job_id,
                    selected_job_id,
                    entrypoint,
                    entrypoint,
                    list(kinds),
                    list(kinds),
                    claim_profile,
                    claim_profile,
                    observed_connection_revision,
                    observed_connection_revision,
                    worker_id,
                    lease_seconds,
                ),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def release_claim(
        self,
        *,
        job_id: str,
        worker_id: str,
    ) -> DurableJob:
        """Return a drain-raced lease without consuming an execution attempt."""

        job_id = _validated_token(job_id, "job_id")
        worker_id = _validated_token(worker_id, "worker_id")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                UPDATE runtime_jobs
                SET status = CASE
                        WHEN cancel_requested THEN 'CANCELLED'
                        ELSE 'PENDING'
                    END,
                    attempts = GREATEST(attempts - 1, 0),
                    lease_owner = '',
                    lease_expires_at = NULL,
                    started_at = CASE
                        WHEN attempts <= 1 THEN NULL
                        ELSE started_at
                    END,
                    finished_at = CASE
                        WHEN cancel_requested THEN now()
                        ELSE NULL
                    END,
                    updated_at = now()
                WHERE id = %s
                  AND lease_owner = %s
                  AND status IN ('RUNNING', 'CANCEL_REQUESTED')
                  AND lease_expires_at >= now()
                RETURNING *
                """,
                (job_id, worker_id),
            ).fetchone()
        return _required_leased_job(row)

    def heartbeat(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
        progress: dict[str, Any] | None = None,
    ) -> DurableJob:
        job_id = _validated_token(job_id, "job_id")
        worker_id = _validated_token(worker_id, "worker_id")
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600.")
        progress_json = _canonical_json(progress) if progress is not None else None
        with self.database.connection() as connection:
            row = connection.execute(
                """
                UPDATE runtime_jobs
                SET lease_expires_at = now() + make_interval(secs => %s),
                    progress = COALESCE(%s::jsonb, progress),
                    updated_at = now(),
                    status = CASE
                        WHEN cancel_requested THEN 'CANCEL_REQUESTED'
                        ELSE status
                    END
                WHERE id = %s
                  AND lease_owner = %s
                  AND status IN ('RUNNING', 'CANCEL_REQUESTED')
                  AND lease_expires_at >= now()
                RETURNING *
                """,
                (lease_seconds, progress_json, job_id, worker_id),
            ).fetchone()
        return _required_leased_job(row)

    def succeed(
        self,
        *,
        job_id: str,
        worker_id: str,
        result: dict[str, Any],
    ) -> DurableJob:
        public_result = {key:value for key,value in result.items() if key != "_manual_decision_contexts"}
        result_json = _canonical_json(public_result)
        with self.database.connection() as connection:
            conflict = _source_item_publication_conflict(connection, job_id=job_id,
                worker_id=worker_id, result=result)
            if conflict is not None:
                row = connection.execute(
                    """
                    UPDATE runtime_jobs
                    SET status = 'FAILED', result = NULL, error_code = 'source_item_publication_conflict',
                        error_message = %s, finished_at = now(), updated_at = now(),
                        lease_owner = '', lease_expires_at = NULL,
                        progress = progress || jsonb_build_object('unpublished_result', %s::jsonb)
                    WHERE id = %s AND lease_owner = %s
                      AND status IN ('RUNNING', 'CANCEL_REQUESTED') AND lease_expires_at >= now()
                    RETURNING *
                    """, (conflict, result_json, job_id, worker_id),
                ).fetchone()
                return _required_leased_job(row)
            row = connection.execute(
                """
                UPDATE runtime_jobs
                SET status = CASE
                        WHEN cancel_requested THEN 'CANCELLED'
                        ELSE 'SUCCEEDED'
                    END,
                    result = CASE
                        WHEN cancel_requested THEN NULL
                        ELSE %s::jsonb
                    END,
                    progress = CASE WHEN error_code <> ''
                        THEN progress || jsonb_build_object('last_failure', jsonb_build_object(
                            'error_code', error_code, 'error_message', error_message))
                        ELSE progress END,
                    error_code = '',
                    error_message = '',
                    lease_owner = '',
                    lease_expires_at = NULL,
                    finished_at = now(),
                    updated_at = now()
                WHERE id = %s
                  AND lease_owner = %s
                  AND status IN ('RUNNING', 'CANCEL_REQUESTED')
                  AND lease_expires_at >= now()
                RETURNING *
                """,
                (result_json, job_id, worker_id),
            ).fetchone()
            if row is not None and str(row["status"]) == JobStatus.SUCCEEDED.value:
                manual_receipt = _rebind_manual_opinions_after_publication(connection, row, result)
                if manual_receipt and manual_receipt.get("status") not in {"NO_MANUAL_STORAGE", "NO_MANUAL_DECISIONS"}:
                    public_result["manual_decision_rebind"] = manual_receipt
                    row = connection.execute("UPDATE runtime_jobs SET result=%s::jsonb WHERE id=%s RETURNING *",
                        (_canonical_json(public_result),job_id)).fetchone()
                _publish_document_source_asset_references(connection, row)
                _publish_extraction_result_artifact_reference(connection, row)
        return _required_leased_job(row)

    def mark_non_cancellable(
        self,
        *,
        job_id: str,
        worker_id: str,
    ) -> DurableJob:
        job_id = _validated_token(job_id, "job_id")
        worker_id = _validated_token(worker_id, "worker_id")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                UPDATE runtime_jobs
                SET progress = progress || '{"non_cancellable": true}'::jsonb,
                    updated_at = now()
                WHERE id = %s
                  AND lease_owner = %s
                  AND status = 'RUNNING'
                  AND lease_expires_at >= now()
                  AND cancel_requested = FALSE
                RETURNING *
                """,
                (job_id, worker_id),
            ).fetchone()
        return _required_leased_job(row)

    def fail(
        self,
        *,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str,
        retry_delay_seconds: int | None,
    ) -> DurableJob:
        error_code = _validated_token(error_code, "error_code")
        if retry_delay_seconds is not None and not 0 <= retry_delay_seconds <= 86400:
            raise ValueError("retry_delay_seconds must be between 0 and 86400.")
        retry = retry_delay_seconds is not None
        with self.database.connection() as connection:
            row = connection.execute(
                """
                UPDATE runtime_jobs
                SET status = CASE
                        WHEN cancel_requested THEN 'CANCELLED'
                        WHEN %s AND attempts < max_attempts THEN 'PENDING'
                        ELSE 'FAILED'
                    END,
                    available_at = CASE
                        WHEN %s AND attempts < max_attempts
                        THEN now() + make_interval(secs => %s)
                        ELSE available_at
                    END,
                    error_code = %s,
                    error_message = %s,
                    lease_owner = '',
                    lease_expires_at = NULL,
                    finished_at = CASE
                        WHEN cancel_requested OR NOT %s OR attempts >= max_attempts
                        THEN now()
                        ELSE NULL
                    END,
                    updated_at = now()
                WHERE id = %s
                  AND lease_owner = %s
                  AND status IN ('RUNNING', 'CANCEL_REQUESTED')
                  AND lease_expires_at >= now()
                RETURNING *
                """,
                (
                    retry,
                    retry,
                    int(retry_delay_seconds or 0),
                    error_code,
                    str(error_message or "")[:4000],
                    retry,
                    job_id,
                    worker_id,
                ),
            ).fetchone()
        return _required_leased_job(row)

    def request_cancel(self, job_id: str) -> DurableJob | None:
        job_id = _validated_token(job_id, "job_id")
        with self.database.connection() as connection:
            _reap_expired_exhausted_jobs(connection, job_id=job_id)
            row = connection.execute(
                """
                UPDATE runtime_jobs
                SET cancel_requested = TRUE,
                    status = CASE
                        WHEN status = 'PENDING' THEN 'CANCELLED'
                        WHEN status = 'RUNNING' THEN 'CANCEL_REQUESTED'
                        ELSE status
                    END,
                    finished_at = CASE
                        WHEN status = 'PENDING' THEN now()
                        ELSE finished_at
                    END,
                    updated_at = now()
                WHERE id = %s
                  AND status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
                  AND COALESCE(progress->>'non_cancellable', 'false') <> 'true'
                RETURNING *
                """,
                (job_id,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def supersede_pending(
        self,
        job_id: str,
        *,
        reason_code: str,
    ) -> DurableJob | None:
        """Cancel only an unclaimed delivery; never race-cancel active work."""

        job_id = _validated_token(job_id, "job_id")
        reason_code = _validated_token(reason_code, "reason_code")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                UPDATE runtime_jobs
                SET status = 'CANCELLED',
                    cancel_requested = TRUE,
                    error_code = %s,
                    error_message = '',
                    finished_at = now(),
                    updated_at = now()
                WHERE id = %s
                  AND status = 'PENDING'
                  AND lease_owner = ''
                RETURNING *
                """,
                (reason_code, job_id),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def get(self, job_id: str) -> DurableJob | None:
        job_id = _validated_token(job_id, "job_id")
        with self.database.connection() as connection:
            _reap_expired_exhausted_jobs(connection, job_id=job_id)
            row = connection.execute(
                "SELECT * FROM runtime_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def get_by_idempotency(
        self,
        *,
        kind: str,
        idempotency_key: str,
    ) -> DurableJob | None:
        kind = _validated_token(kind, "kind")
        idempotency_key = _validated_token(idempotency_key, "idempotency_key")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM runtime_jobs
                WHERE kind = %s AND idempotency_key = %s
                """,
                (kind, idempotency_key),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def queue_diagnostics(self, job_id: str) -> dict[str, int | None]:
        """Return bounded queue facts for one durable job's eligible lane.

        Position counts runnable PENDING jobs in the lane that can actually
        delay this target. The local second slot skips semantic-LLM plans, so
        those plans do not appear ahead of a no-LLM extraction in that lane.
        Running count is eligible occupancy: both slots for a no-LLM extract,
        but only the general slot for semantic-LLM work.
        """

        job_id = _validated_token(job_id, "job_id")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                WITH target AS (
                    SELECT id, kind, payload, priority, created_at, status
                    FROM runtime_jobs
                    WHERE id = %s
                ), active AS (
                    SELECT lease_owner
                    FROM runtime_jobs
                    WHERE status IN ('RUNNING', 'CANCEL_REQUESTED')
                      AND lease_expires_at >= now()
                )
                SELECT
                    CASE WHEN EXISTS (
                        SELECT 1 FROM target WHERE status = 'PENDING'
                    )
                        THEN 1 + (
                            SELECT COUNT(*)
                            FROM runtime_jobs AS queued, target
                            WHERE target.status = 'PENDING'
                              AND queued.status = 'PENDING'
                              AND queued.available_at <= now()
                              AND queued.id <> target.id
                              AND (
                                  target.kind <> 'document.extract'
                                  OR COALESCE(
                                      target.payload #>> '{processing_plan,llm_provider}',
                                      ''
                                  ) <> 'none'
                                  OR (
                                      queued.kind = 'document.extract'
                                      AND queued.payload #>> '{processing_plan,llm_provider}' = 'none'
                                  )
                              )
                              AND (
                                  queued.priority > target.priority
                                  OR (
                                      queued.priority = target.priority
                                      AND (
                                          queued.created_at < target.created_at
                                          OR (
                                              queued.created_at = target.created_at
                                              AND queued.id < target.id
                                          )
                                      )
                                  )
                              )
                        )
                        ELSE NULL
                    END AS position,
                    CASE WHEN EXISTS (
                        SELECT 1
                        FROM target
                        WHERE kind = 'document.extract'
                          AND payload #>> '{processing_plan,llm_provider}' = 'none'
                    ) THEN (SELECT COUNT(*) FROM active)
                    ELSE (
                        SELECT COUNT(*)
                        FROM active
                        WHERE lease_owner NOT LIKE 'local-%%-slot-2-%%'
                    ) END AS running_count
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return {"position": None, "ahead_count": 0, "running_count": 0}
        position_value = row["position"]
        position = int(position_value) if position_value is not None else None
        return {
            "position": position,
            "ahead_count": max(0, (position or 1) - 1),
            "running_count": max(0, int(row["running_count"] or 0)),
        }

    def get_latest(
        self,
        *,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
    ) -> DurableJob | None:
        kind = _validated_token(kind, "kind")
        aggregate_type = _validated_token(aggregate_type, "aggregate_type")
        aggregate_id = _validated_token(aggregate_id, "aggregate_id")
        with self.database.connection() as connection:
            _reap_expired_exhausted_jobs(connection)
            row = connection.execute(
                """
                SELECT *
                FROM runtime_jobs
                WHERE kind = %s
                  AND aggregate_type = %s
                  AND aggregate_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (kind, aggregate_type, aggregate_id),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def get_latest_succeeded(
        self,
        *,
        kind: str,
        aggregate_type: str,
        aggregate_id: str,
        payload_filters: dict[str, Any] | None = None,
        require_result_key: str = "",
    ) -> DurableJob | None:
        """Return the newest completed publication for an aggregate.

        A later retry is a separate attempt.  Its failure must not erase the
        immutable result that was published by an earlier successful job.
        """

        kind = _validated_token(kind, "kind")
        aggregate_type = _validated_token(aggregate_type, "aggregate_type")
        aggregate_id = _validated_token(aggregate_id, "aggregate_id")
        result_key = _validated_token(require_result_key, "require_result_key") if require_result_key else ""
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM runtime_jobs
                WHERE kind = %s
                  AND aggregate_type = %s
                  AND aggregate_id = %s
                  AND status = 'SUCCEEDED'
                  AND payload @> %s::jsonb
                  AND (%s = '' OR result ? %s)
                ORDER BY CASE WHEN kind IN (
                    'agent.source-requirement.baseline',
                    'agent.source-item-text.baseline',
                    'agent.source-item.scope-revision'
                    ,'agent.source-item.classify', 'agent.source-item.images',
                    'agent.source-item.direct-revision', 'agent.source-item.loop', 'agent.source-item.extract-loop'
                ) THEN COALESCE((result #>> '{baseline_revision_artifact,baseline_revision}')::bigint,
                                (result->>'baseline_revision')::bigint, 0)
                ELSE 0 END DESC, created_at DESC
                LIMIT 1
                """,
                (kind, aggregate_type, aggregate_id, _canonical_json(payload_filters or {}), result_key, result_key),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def compare_and_set_succeeded_result(
        self,
        *,
        job_id: str,
        expected_result: dict[str, Any],
        result: dict[str, Any],
        artifact_operation_id: str = "",
    ) -> DurableJob | None:
        """Replace a published result only when its complete prior value matches."""

        job_id = _validated_token(job_id, "job_id")
        operation_id = _validated_token(
            artifact_operation_id,
            "artifact_operation_id",
        )
        expected_json = _canonical_json(expected_result)
        result_json = _canonical_json(result)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                UPDATE runtime_jobs
                SET result = %s::jsonb,
                    progress = CASE WHEN error_code <> ''
                        THEN progress || jsonb_build_object('last_failure', jsonb_build_object(
                            'error_code', error_code, 'error_message', error_message))
                        ELSE progress END,
                    error_code = '',
                    error_message = '',
                    updated_at = now(),
                    generation = generation + 1
                WHERE id = %s
                  AND status = 'SUCCEEDED'
                  AND result = %s::jsonb
                RETURNING *
                """,
                (result_json, job_id, expected_json),
            ).fetchone()
            if row is not None:
                _replace_succeeded_extraction_result_artifact_reference(
                    connection,
                    row,
                    expected_result,
                    result,
                    operation_id,
                )
        return _job_from_row(row) if row is not None else None


def _source_item_publication_conflict(connection: Any, *, job_id: str, worker_id: str,
                                      result: dict[str, Any]) -> str | None:
    """Compare the source/Map/predecessor in the same transaction as visibility.

    SourceItem objects may already exist privately. Only a successful job makes
    them current, so this fence must be here, rather than only in the handler.
    """
    if (not result.get("baseline_revision_artifact") and not result.get("expected_live_baseline_sha256")
            and not result.get("native_source_graph_artifact")):
        return None
    job = connection.execute(
        """SELECT * FROM runtime_jobs WHERE id = %s AND lease_owner = %s
           AND status IN ('RUNNING', 'CANCEL_REQUESTED') AND lease_expires_at >= now()
           FOR UPDATE""", (job_id, worker_id),
    ).fetchone()
    if job is None:
        raise JobLeaseLostError("SourceItem publication no longer owns its durable lease.")
    if not str(job["kind"]).startswith("agent.source-"):
        return None
    payload = _json_mapping(job["payload"])
    project_id, document_id = str(payload.get("project_id") or ""), str(job["aggregate_id"])
    source_sha = str(result.get("source_sha256") or "")
    map_sha = str(result.get("document_map_artifact_sha256") or "")
    if not project_id or not _SHA256_RE.fullmatch(source_sha) or not _SHA256_RE.fullmatch(map_sha):
        return "原文发布的项目、来源或文档组织身份缺失。"
    # Use the same source lock as Document Map head publication, so a new Map
    # cannot be made current between the comparison and this job's commit.
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("\x1f".join(("agent-structure-loop/v1", project_id, document_id, source_sha)),))
    document = connection.execute(
        "SELECT source_sha256 FROM sor_documents WHERE id = %s AND project_id = %s FOR UPDATE",
        (document_id, project_id),
    ).fetchone()
    if document is None or document["source_sha256"] != source_sha or payload.get("source_sha256") != source_sha:
        return "原文件已改变，本轮候选保留为未发布工件。"
    if "expected_source_item_semantic_head_job_id" in result:
        if source_item_semantic_head_job_id(connection, project_id, document_id, source_sha) != result["expected_source_item_semantic_head_job_id"]:
            return "原文、派生或图片释读已有更新，本轮候选保留为未发布工件。"
    if result.get("expected_structure_head_verified") is True:
        head = connection.execute(
            """SELECT revision_sha256 FROM agent_structure_working_revision_heads
               WHERE project_id = %s AND sor_document_id = %s AND source_sha256 = %s
               ORDER BY updated_at DESC, revision DESC LIMIT 1""",
            (project_id, document_id, source_sha),
        ).fetchone()
        if (head["revision_sha256"] if head else None) != result.get("expected_structure_head_revision_sha256"):
            return "原文组织已改变，本轮候选保留为未发布工件。"
    if result.get("expected_no_source_graph") is True:
        existing = connection.execute(
            """SELECT id FROM runtime_jobs WHERE status = 'SUCCEEDED' AND aggregate_type = 'sor-document'
               AND aggregate_id = %s AND payload->>'project_id' = %s
               AND result->>'source_sha256' = %s
               AND (result ? 'native_source_graph_artifact' OR result ? 'baseline_revision_artifact')
               LIMIT 1""", (document_id, project_id, source_sha),
        ).fetchone()
        if existing is not None:
            return "原文件已有另一份工作图谱，本轮初始候选保留为未发布工件。"
    expected_graph = result.get("expected_native_source_graph_object_sha256")
    if expected_graph is not None:
        if not isinstance(expected_graph, str) or not _SHA256_RE.fullmatch(expected_graph):
            return "图谱发布缺少有效的前版对象绑定。"
        current_graph = connection.execute(
            """SELECT result #>> '{native_source_graph_artifact,object_sha256}' AS object_sha256
               FROM runtime_jobs WHERE status = 'SUCCEEDED' AND aggregate_type = 'sor-document'
                 AND aggregate_id = %s AND payload->>'project_id' = %s
                 AND result->>'source_sha256' = %s
                 AND jsonb_typeof(result->'native_source_graph_artifact') = 'object'
               ORDER BY updated_at DESC LIMIT 1""", (document_id, project_id, source_sha),
        ).fetchone()
        if current_graph is None or current_graph["object_sha256"] != expected_graph:
            return "原文图谱已被另一轮更新，本轮候选保留为未发布工件。"
    expected = result.get("expected_live_baseline_sha256") or (result.get("predecessor_baseline_revision_artifact") or {}).get("baseline_artifact_sha256")
    if expected is None:
        return None
    if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
        return "原文发布缺少有效的前版绑定。"
    expected_map = result.get("expected_document_map_artifact_sha256") or map_sha
    if not isinstance(expected_map, str) or not _SHA256_RE.fullmatch(expected_map):
        return "原文发布缺少有效的组织前版绑定。"
    latest = connection.execute(
        """SELECT result->>'source_sha256' AS source_sha256,
                  result->>'document_map_artifact_sha256' AS map_sha256,
                  COALESCE(result #>> '{baseline_revision_artifact,baseline_artifact_sha256}', result->>'baseline_sha256') AS baseline_sha256
           FROM runtime_jobs
           WHERE aggregate_type = 'sor-document' AND aggregate_id = %s
             AND payload->>'project_id' = %s AND status = 'SUCCEEDED'
             AND kind LIKE 'agent.source-%%'
             AND (jsonb_typeof(result->'baseline_revision_artifact') = 'object'
                  OR jsonb_typeof(result->'native_source_graph_artifact') = 'object')
           ORDER BY COALESCE((result #>> '{baseline_revision_artifact,baseline_revision}')::bigint,
                             (result->>'baseline_revision')::bigint, 0) DESC,
                    updated_at DESC LIMIT 1""", (document_id, project_id),
    ).fetchone()
    # An accepted imported initial ledger may have no durable SourceItem job;
    # its host-validated predecessor is retained until the first publication.
    if expected_graph is not None and latest is None:
        return "原文图谱的工作基线前版已不存在。"
    if latest is not None and (latest["source_sha256"] != source_sha or latest["map_sha256"] != expected_map
                               or latest["baseline_sha256"] != expected):
        return "原文工作版已被另一轮修订更新，本轮候选保留为未发布工件。"
    return None


def source_item_semantic_head_job_id(connection, project_id, document_id, source_sha256):
    row = connection.execute(
        """SELECT id FROM runtime_jobs WHERE status='SUCCEEDED' AND aggregate_type='sor-document'
           AND aggregate_id=%s AND payload->>'project_id'=%s AND result->>'source_sha256'=%s
           AND (result ? 'baseline_revision_artifact' OR result ? 'native_source_graph_artifact'
                OR result ? 'annotations_artifact' OR result ? 'image_annotations_artifact'
                OR result ? 'decomposition_artifact'
                OR (kind='agent.source-item.decompose' AND result->>'publication_mode'='WORKING_PROPOSAL'))
           ORDER BY updated_at DESC,id DESC LIMIT 1""", (document_id, project_id, source_sha256),
    ).fetchone()
    return str(row["id"]) if row else None


def _rebind_manual_opinions_after_publication(connection, row, result):
    contexts = result.get("_manual_decision_contexts")
    if contexts is None:
        return None
    from reqpilotx_server.application.agent_workflow.source_item_manual_decisions import validate_manual_context
    from reqpilotx_server.adapters.persistence.source_item_manual_decisions import rebind_manual_decisions_in_transaction
    if not isinstance(contexts, dict) or set(contexts) != {"previous", "current"}:
        raise ValueError("Invalid Host manual publication contexts")
    current = validate_manual_context(contexts["current"])
    previous = validate_manual_context(contexts["previous"]) if contexts["previous"] is not None else None
    baseline_sha = (result.get("baseline_revision_artifact") or {}).get("baseline_artifact_sha256") or result.get("baseline_sha256")
    if current["source_sha256"] != result.get("source_sha256") or current["baseline_sha256"] != baseline_sha:
        raise ValueError("Manual response context does not bind the actual published source baseline")
    if previous is not None and previous["baseline_sha256"] != result.get("expected_live_baseline_sha256"):
        raise ValueError("Manual response previous context does not bind the checked predecessor")
    payload = _json_mapping(row["payload"])
    return rebind_manual_decisions_in_transaction(connection, project_id=payload["project_id"],
        document_id=str(row["aggregate_id"]), previous_context=previous, current_context=current,
        publication_id=str(row["id"]))


def _required_leased_job(row: Any | None) -> DurableJob:
    if row is None:
        raise JobLeaseLostError(
            "The job lease is missing, expired, or owned by another worker."
        )
    return _job_from_row(row)


def _publish_document_source_asset_references(
    connection: Any,
    job_row: Any,
) -> None:
    """Promote one extraction generation in the same transaction as success."""

    if (
        str(job_row["kind"]) != "document.extract"
        or str(job_row["aggregate_type"]) != "sor-document"
    ):
        return
    payload = _json_mapping(job_row["payload"])
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    source_sha256 = str(payload.get("source_sha256") or "")
    progress = _json_mapping(job_row["progress"])
    if (
        document_id != str(job_row["aggregate_id"])
        or not project_id
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        or int(progress.get("source_asset_publication_generation") or 0)
        != int(job_row["generation"])
    ):
        raise RuntimeError("Document extraction asset publication binding is invalid.")
    document = connection.execute(
        """
        SELECT 1 AS present
        FROM sor_documents
        WHERE id = %s AND project_id = %s AND source_sha256 = %s
        FOR UPDATE
        """,
        (document_id, project_id, source_sha256),
    ).fetchone()
    if document is None:
        raise RuntimeError("Document extraction asset publication source changed.")
    generation = int(job_row["generation"])
    connection.execute(
        """
        DELETE FROM source_asset_references AS committed
        WHERE committed.document_id = %s
          AND committed.source_sha256 = %s
          AND NOT EXISTS (
              SELECT 1
              FROM source_asset_staging_references AS staging
              WHERE staging.job_id = %s
                AND staging.attempt_generation = %s
                AND staging.storage_key = committed.storage_key
          )
        """,
        (document_id, source_sha256, str(job_row["id"]), generation),
    )
    connection.execute(
        """
        INSERT INTO source_asset_references (
            project_id, document_id, source_sha256, storage_key
        )
        SELECT project_id, document_id, source_sha256, storage_key
        FROM source_asset_staging_references
        WHERE job_id = %s AND attempt_generation = %s
        ON CONFLICT (document_id, source_sha256, storage_key) DO NOTHING
        """,
        (str(job_row["id"]), generation),
    )
    connection.execute(
        """
        DELETE FROM source_asset_staging_references
        WHERE job_id = %s AND attempt_generation = %s
        """,
        (str(job_row["id"]), generation),
    )


def _publish_extraction_result_artifact_reference(
    connection: Any,
    job_row: Any,
) -> None:
    """Promote the result artifact in the same transaction as job success."""

    if (
        str(job_row["kind"]) != "document.extract"
        or str(job_row["aggregate_type"]) != "sor-document"
    ):
        return
    result = _json_mapping(job_row["result"])
    generation = int(job_row["generation"])
    staged = connection.execute(
        """
        SELECT project_id, document_id, source_sha256, storage_key,
               artifact_sha256, size_bytes, media_type
        FROM extraction_result_artifact_staging_references
        WHERE job_id = %s AND attempt_generation = %s
        FOR UPDATE
        """,
        (str(job_row["id"]), generation),
    ).fetchone()
    if staged is None:
        raise RuntimeError("Extraction result artifact staging reference is missing.")
    if (
        str(result.get("storage_key") or "") != str(staged["storage_key"])
        or str(result.get("sha256") or "") != str(staged["artifact_sha256"])
        or int(result.get("size") or 0) != int(staged["size_bytes"])
    ):
        raise RuntimeError("Extraction result artifact receipt binding is invalid.")
    _artifact_object_advisory_lock(connection, str(staged["storage_key"]))
    connection.execute(
        """
        INSERT INTO extraction_result_artifact_references (
            job_id, project_id, document_id, source_sha256, storage_key,
            artifact_sha256, size_bytes, media_type
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (job_id) DO UPDATE
        SET storage_key = EXCLUDED.storage_key,
            artifact_sha256 = EXCLUDED.artifact_sha256,
            size_bytes = EXCLUDED.size_bytes,
            media_type = EXCLUDED.media_type
        """,
        (
            str(job_row["id"]),
            str(staged["project_id"]),
            str(staged["document_id"]),
            str(staged["source_sha256"]),
            str(staged["storage_key"]),
            str(staged["artifact_sha256"]),
            int(staged["size_bytes"]),
            str(staged["media_type"]),
        ),
    )
    connection.execute(
        """
        DELETE FROM extraction_result_artifact_staging_references
        WHERE job_id = %s AND attempt_generation = %s
        """,
        (str(job_row["id"]), generation),
    )


def _replace_succeeded_extraction_result_artifact_reference(
    connection: Any,
    job_row: Any,
    expected_result: dict[str, Any],
    result: dict[str, Any],
    operation_id: str,
) -> None:
    if str(job_row["kind"]) != "document.extract":
        return
    payload = _json_mapping(job_row["payload"])
    storage_key = str(result.get("storage_key") or "")
    artifact_sha256 = str(result.get("sha256") or "")
    size_bytes = int(result.get("size") or 0)
    if (
        str(result.get("schema_version") or "")
        != "document-extraction-result-artifact/v1"
        or not storage_key
        or _SHA256_RE.fullmatch(artifact_sha256) is None
        or size_bytes < 1
    ):
        raise RuntimeError("Extraction result artifact CAS receipt is invalid.")
    operation = connection.execute(
        """
        SELECT *
        FROM extraction_result_artifact_cas_operations
        WHERE operation_id = %s AND job_id = %s
        FOR UPDATE
        """,
        (operation_id, str(job_row["id"])),
    ).fetchone()
    if (
        operation is None
        or str(operation["expected_storage_key"])
        != str(expected_result.get("storage_key") or "")
        or str(operation["new_storage_key"]) != storage_key
        or str(operation["new_artifact_sha256"]) != artifact_sha256
        or int(operation["new_size_bytes"]) != size_bytes
    ):
        raise RuntimeError("Extraction result artifact CAS ownership is missing.")
    _artifact_object_advisory_lock(connection, storage_key)
    connection.execute(
        """
        INSERT INTO extraction_result_artifact_references (
            job_id, project_id, document_id, source_sha256, storage_key,
            artifact_sha256, size_bytes, media_type
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (job_id) DO UPDATE
        SET storage_key = EXCLUDED.storage_key,
            artifact_sha256 = EXCLUDED.artifact_sha256,
            size_bytes = EXCLUDED.size_bytes,
            media_type = EXCLUDED.media_type
        """,
        (
            str(job_row["id"]),
            str(payload.get("project_id") or ""),
            str(payload.get("document_id") or ""),
            str(payload.get("source_sha256") or ""),
            storage_key,
            artifact_sha256,
            size_bytes,
            str(result.get("media_type") or "application/json"),
        ),
    )
    connection.execute(
        """
        DELETE FROM extraction_result_artifact_cas_operations
        WHERE operation_id = %s
        """,
        (operation_id,),
    )


def _artifact_object_advisory_lock(connection: Any, storage_key: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 90422))",
        (storage_key,),
    )


def _reap_expired_exhausted_jobs(
    connection: Any,
    *,
    job_id: str = "",
    required_entrypoint: str = "",
) -> None:
    """Terminalize deliveries that no worker can claim again."""

    normalized_job_id = str(job_id or "").strip()
    connection.execute(
        """
        UPDATE runtime_jobs
        SET status = CASE
                WHEN cancel_requested OR status = 'CANCEL_REQUESTED'
                THEN 'CANCELLED'
                ELSE 'FAILED'
            END,
            error_code = CASE
                WHEN cancel_requested OR status = 'CANCEL_REQUESTED'
                THEN ''
                ELSE 'job_attempts_exhausted'
            END,
            error_message = CASE
                WHEN cancel_requested OR status = 'CANCEL_REQUESTED'
                THEN ''
                ELSE 'The worker lease expired after the final delivery attempt.'
            END,
            lease_owner = '',
            lease_expires_at = NULL,
            finished_at = COALESCE(finished_at, now()),
            updated_at = now(),
            generation = generation + 1
        WHERE status IN ('RUNNING', 'CANCEL_REQUESTED')
          AND lease_expires_at < now()
          AND attempts >= max_attempts
          AND (%s::text = '' OR id = %s)
          AND (%s::text = '' OR payload->>'application_entrypoint' = %s)
        """,
        (normalized_job_id, normalized_job_id, required_entrypoint, required_entrypoint),
    )


def _job_from_row(row: Any) -> DurableJob:
    payload = _json_mapping(row["payload"])
    progress = _json_mapping(row["progress"])
    result_value = row.get("result") if hasattr(row, "get") else row["result"]
    result = None if result_value is None else _json_mapping(result_value)
    return DurableJob(
        job_id=str(row["id"]),
        kind=str(row["kind"]),
        aggregate_type=str(row["aggregate_type"]),
        aggregate_id=str(row["aggregate_id"]),
        idempotency_key=str(row["idempotency_key"]),
        payload=payload,
        status=JobStatus(str(row["status"])),
        priority=int(row["priority"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        generation=int(row["generation"]),
        cancel_requested=bool(row["cancel_requested"]),
        lease_owner=str(row["lease_owner"] or ""),
        lease_expires_at=row["lease_expires_at"],
        available_at=row["available_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        progress=progress,
        result=result,
        error_code=str(row["error_code"] or ""),
        error_message=str(row["error_message"] or ""),
    )


def _validated_token(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _TOKEN_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a stable non-blank token.")
    return normalized


def _validated_revision(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("connection_revision must be a non-negative integer.")
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "connection_revision must be a non-negative integer."
        ) from exc
    if revision < 0:
        raise ValueError("connection_revision must be a non-negative integer.")
    return revision


def _validated_worker_identity(
    *,
    worker_id: str,
    slot: str,
    claim_profile: str,
    accepted_kinds: tuple[str, ...],
    connection_revision: int,
    source_revision: str,
    runtime_instance_id: str,
    state: str,
) -> dict[str, Any]:
    kinds = tuple(
        dict.fromkeys(
            _validated_token(item, "accepted_kind") for item in accepted_kinds
        )
    )
    if not kinds:
        raise ValueError("accepted_kinds must contain at least one job kind.")
    try:
        worker_state = WorkerFleetState(str(state).strip().upper()).value
    except ValueError as exc:
        raise ValueError("Unsupported worker fleet state.") from exc
    return {
        "worker_id": _validated_token(worker_id, "worker_id"),
        "slot": _validated_token(slot, "slot"),
        "claim_profile": _validated_token(claim_profile, "claim_profile"),
        "accepted_kinds": kinds,
        "connection_revision": _validated_revision(connection_revision),
        "source_revision": _validated_token(source_revision, "source_revision"),
        "runtime_instance_id": _validated_token(
            runtime_instance_id, "runtime_instance_id"
        ),
        "state": worker_state,
    }


def _plan_requires_connection_revision(plan: dict[str, Any]) -> bool:
    requirements = plan.get("execution_requirements")
    if isinstance(requirements, dict):
        if (
            requirements.get("schema_version")
            != "document-extraction-execution-requirements/v1"
            or type(requirements.get("connection_revision_required")) is not bool
        ):
            return True
        return bool(requirements["connection_revision_required"])
    processing_plan = (
        plan.get("processing_plan")
        if isinstance(plan.get("processing_plan"), dict)
        else plan
    )
    if not isinstance(processing_plan, dict) or not processing_plan:
        return False
    bindings = (
        processing_plan.get("provider_bindings")
        if isinstance(processing_plan.get("provider_bindings"), dict)
        else {}
    )
    primary_provider = str(bindings.get("primary_parser_provider") or "").strip()
    if primary_provider and not primary_provider.startswith("local:"):
        return True
    if tuple(bindings.get("enhancement_providers") or ()):
        return True
    if str(bindings.get("complex_vision_provider") or "").strip():
        return True
    if str(bindings.get("llm_transport_provider") or "").strip():
        return True
    document = (
        processing_plan.get("document")
        if isinstance(processing_plan.get("document"), dict)
        else {}
    )
    return bool(
        str(document.get("primary_parser") or "") != "pymupdf"
        or tuple(document.get("enhancement_chain") or ())
        or str(document.get("complex_vision") or "").strip()
        or str(processing_plan.get("llm_provider") or "none") != "none"
    )


def _claim_profile_accepts_plan(
    *,
    claim_profile: str,
    required_kind: str,
    plan: dict[str, Any],
) -> bool:
    if claim_profile == "general":
        return True
    if claim_profile != "local-extraction-no-llm":
        return False
    if required_kind != "document.extract":
        return False
    processing_plan = plan.get("processing_plan")
    if not isinstance(processing_plan, dict):
        # The global fleet route is a revision/source/runtime ACK, not a
        # preflight for one frozen plan. A restricted extraction slot is
        # healthy there as long as it accepts the requested durable kind.
        return True
    return str(processing_plan.get("llm_provider") or "none") == "none"


def _canonical_json(value: dict[str, Any]) -> str:
    if not isinstance(value, dict):
        raise TypeError("Job payloads must be JSON objects.")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    payload = json.loads(str(value or "{}"))
    if not isinstance(payload, dict):
        raise ValueError("Stored job JSON must be an object.")
    return payload


__all__ = ["JobConflictError", "JobLeaseLostError", "PostgresJobQueue"]
