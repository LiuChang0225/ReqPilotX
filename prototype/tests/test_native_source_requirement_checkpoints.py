from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
from pathlib import Path

from app.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (
    run_native_source_requirement_windowed_reference,
)
from app.adapters.persistence.native_source_requirement_checkpoints import (
    NativeSourceRequirementCheckpointProgress,
    NativeSourceRequirementDurableCheckpointStore,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from tests.test_native_source_requirement_windowed_reference import (
    _FakeWindowedCommandRunner,
    _windowed_fixture,
)


class NativeSourceRequirementCheckpointTests(unittest.TestCase):
    def test_durable_progress_retries_only_the_failed_window(self) -> None:
        document_map, request = _windowed_fixture()

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            object_store = LocalObjectStore(root / "objects")
            first_context = _FakeJobContext()
            store = NativeSourceRequirementDurableCheckpointStore(
                object_store=object_store,
                project_id="project-1",
                document_id="document-1",
            )
            first_progress = NativeSourceRequirementCheckpointProgress(
                context=first_context,
                store=store,
            )
            first_runner = _FakeWindowedCommandRunner(
                always_invalid_window_id="window-02"
            )
            with self.assertRaisesRegex(RuntimeError, "window-02"):
                run_native_source_requirement_windowed_reference(
                    document_map=document_map,
                    request=request,
                    output_directory=root / "failed",
                    max_concurrency=2,
                    command_runner=first_runner,
                    checkpoint_sink=first_progress.save,
                )

            self.assertEqual(first_context.progress["completed_window_count"], 1)
            second_context = _FakeJobContext()
            resumed_progress = NativeSourceRequirementCheckpointProgress(
                context=second_context,
                store=store,
                initial=first_context.progress,
            )
            checkpoint = resumed_progress.load()
            resumed_runner = _FakeWindowedCommandRunner()
            result = run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=root / "resumed",
                max_concurrency=2,
                command_runner=resumed_runner,
                resume_checkpoint=checkpoint,
                checkpoint_sink=resumed_progress.save,
            )

        self.assertEqual(len(result.candidate.records), 6)
        self.assertEqual(resumed_runner.boundary_calls, 0)
        self.assertNotIn("window-01", resumed_runner.window_call_counts)
        self.assertEqual(resumed_runner.window_call_counts["window-02"], 1)
        self.assertEqual(second_context.progress["completed_window_count"], 2)

    def test_progress_persists_cumulative_checkpoint_and_resumes_without_agent(
        self,
    ) -> None:
        document_map, request = _windowed_fixture()

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            object_store = LocalObjectStore(root / "objects")
            context = _FakeJobContext()
            store = NativeSourceRequirementDurableCheckpointStore(
                object_store=object_store,
                project_id="project-1",
                document_id="document-1",
            )
            progress = NativeSourceRequirementCheckpointProgress(
                context=context,
                store=store,
            )
            first_runner = _FakeWindowedCommandRunner(delayed_window_id="window-01")
            first = run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=root / "first",
                max_concurrency=2,
                command_runner=first_runner,
                checkpoint_sink=progress.save,
            )

            self.assertEqual(len(context.heartbeats), 3)
            self.assertEqual(context.progress["completed_window_count"], 2)
            receipt_payload = context.progress["source_requirement_window_checkpoint"]
            self.assertTrue(object_store.stat(receipt_payload["object_key"]))

            restored_progress = NativeSourceRequirementCheckpointProgress(
                context=_FakeJobContext(),
                store=store,
                initial=context.progress,
            )
            checkpoint = restored_progress.load()
            self.assertIsNotNone(checkpoint)
            resumed_runner = _FakeWindowedCommandRunner()
            resumed = run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=root / "resumed",
                max_concurrency=2,
                command_runner=resumed_runner,
                resume_checkpoint=checkpoint,
            )

        self.assertEqual(resumed.candidate, first.candidate)
        self.assertEqual(resumed_runner.boundary_calls, 0)
        self.assertEqual(resumed_runner.classification_calls, 0)

    def test_checkpoint_object_tamper_is_rejected(self) -> None:
        document_map, request = _windowed_fixture()

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            object_store = LocalObjectStore(root / "objects")
            context = _FakeJobContext()
            store = NativeSourceRequirementDurableCheckpointStore(
                object_store=object_store,
                project_id="project-1",
                document_id="document-1",
            )
            progress = NativeSourceRequirementCheckpointProgress(
                context=context,
                store=store,
            )
            run_native_source_requirement_windowed_reference(
                document_map=document_map,
                request=request,
                output_directory=root / "run",
                max_concurrency=2,
                command_runner=_FakeWindowedCommandRunner(),
                checkpoint_sink=progress.save,
            )
            receipt = progress.receipt
            self.assertIsNotNone(receipt)
            target = object_store.root / receipt.object_key
            target.write_bytes(b"{}")

            with self.assertRaisesRegex(ValueError, "object SHA drifted"):
                progress.load()


class _FakeJobContext:
    def __init__(self) -> None:
        self.heartbeats: list[dict[str, object]] = []
        self.progress: dict[str, object] = {}

    def heartbeat(self, progress=None):  # type: ignore[no-untyped-def]
        if progress is not None:
            self.progress = dict(progress)
            self.heartbeats.append(self.progress)
        return None

    def cancellation_requested(self) -> bool:
        return False

    def lease_fence(self):  # type: ignore[no-untyped-def]
        raise AssertionError("lease fence is not used by checkpoint persistence")

    def begin_publication(self):  # type: ignore[no-untyped-def]
        raise AssertionError("publication is not used by checkpoint persistence")


if __name__ == "__main__":
    unittest.main()
