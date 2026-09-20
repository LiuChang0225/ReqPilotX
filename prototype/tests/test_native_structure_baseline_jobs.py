from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from datetime import UTC, datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.native_structure_baseline import (
    NativeStructureBaselineArtifactV1,
    native_structure_baseline_object_key,
)
from app.application.agent_workflow.native_structure_baseline_jobs import (
    NATIVE_STRUCTURE_BASELINE_JOB_KIND,
    run_durable_native_structure_baseline_acceptance,
)
from app.ports.job_queue import DurableJob, JobExecutionContext, JobStatus

if __package__:
    from .test_native_structure_baseline import (
        NativeStructureAcceptedBaselineTests,
    )
else:
    from test_native_structure_baseline import NativeStructureAcceptedBaselineTests


class NativeStructureBaselineJobTests(unittest.TestCase):
    def test_worker_persists_private_artifact_and_enqueues_parent_job(self) -> None:
        fixture = NativeStructureAcceptedBaselineTests(methodName="runTest")
        fixture.setUp()
        compilation = fixture.compile()
        identity = compilation.identity
        current = {
            "document": {
                "id": identity.document_id,
                "project_id": identity.project_id,
                "source_sha256": identity.source_sha256,
            },
            "source_sha256": identity.source_sha256,
            "artifact_id": identity.native_artifact_id,
            "public_reference": fixture.reference,
            "table_previews": fixture.previews,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.pdf"
            source_path.write_bytes(b"synthetic")
            store = LocalObjectStore(root / "objects")
            container = SimpleNamespace(object_store=store)
            context = _Context()
            with (
                patch(
                    "app.application.runtime.RUNTIME_STATE.container",
                    container,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_baseline_jobs._current_reference_context",
                    return_value=current,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_baseline_jobs.load_portable_agent_kit",
                    return_value=SimpleNamespace(identity=fixture.kit),
                ),
                patch(
                    "app.application.agent_workflow.native_structure_baseline_jobs.prepare_document_evidence",
                    return_value=SimpleNamespace(source_ir=fixture.source),
                ),
                patch(
                    "app.application.agent_workflow.native_structure_baseline_jobs.compile_native_structure_accepted_baseline",
                    return_value=compilation,
                ),
                patch(
                    "app.application.agent_workflow.native_structure_baseline_jobs.resolve_source_record_path",
                    return_value=source_path,
                ),
                patch(
                    "app.application.agent_workflow.native_parent_provision_jobs.enqueue_native_parent_provision_recognition",
                    return_value={"job_id": "job-parent", "status": "PENDING"},
                ) as enqueue_parent,
            ):
                result = run_durable_native_structure_baseline_acceptance(
                    _job(identity),
                    context,
                )

            self.assertEqual(result["status"], "ACCEPTED")
            self.assertEqual(result["baseline"], identity.model_dump(mode="json"))
            self.assertEqual(result["parent_job"]["job_id"], "job-parent")
            self.assertEqual(context.publication_count, 1)
            self.assertEqual(
                [item["stage"] for item in context.progress],
                [
                    "REBUILDING_SOURCE_EVIDENCE",
                    "VALIDATING_NATIVE_BASELINE",
                    "PERSISTING_ACCEPTED_BASELINE",
                ],
            )
            key = native_structure_baseline_object_key(identity)
            with store.open(key) as stream:
                stored = NativeStructureBaselineArtifactV1.model_validate_json(
                    stream.read()
                )
            self.assertEqual(stored.identity, identity)
            enqueue_parent.assert_called_once()


class _Context(JobExecutionContext):
    def __init__(self) -> None:
        self.publication_count = 0
        self.progress: list[dict[str, object]] = []

    def heartbeat(self, progress=None):  # type: ignore[no-untyped-def]
        if progress:
            self.progress.append(dict(progress))
        return SimpleNamespace(progress=progress or {})

    def cancellation_requested(self) -> bool:
        return False

    def lease_fence(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(job_id="job-baseline", generation=1, worker_id="worker")

    def begin_publication(self):  # type: ignore[no-untyped-def]
        self.publication_count += 1
        return SimpleNamespace()


def _job(identity) -> DurableJob:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    return DurableJob(
        job_id="job-baseline",
        kind=NATIVE_STRUCTURE_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=identity.document_id,
        idempotency_key="key-baseline",
        payload={
            "schema_version": "native-structure-baseline-job/v1",
            "project_id": identity.project_id,
            "document_id": identity.document_id,
            "source_sha256": identity.source_sha256,
            "native_artifact_id": identity.native_artifact_id,
            "native_projection_sha256": identity.native_projection_sha256,
            "kit_bundle_sha256": identity.kit_identity.bundle_sha256,
            "command_id": "accept-baseline:1",
        },
        status=JobStatus.RUNNING,
        priority=24,
        attempts=1,
        max_attempts=2,
        generation=1,
        cancel_requested=False,
        lease_owner="worker",
        lease_expires_at=now,
        available_at=now,
        created_at=now,
        updated_at=now,
        progress={},
        result=None,
        error_code="",
        error_message="",
    )


if __name__ == "__main__":
    unittest.main()
