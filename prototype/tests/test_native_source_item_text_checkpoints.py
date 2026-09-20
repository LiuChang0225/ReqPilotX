from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import tempfile
import unittest

from app.adapters.persistence.native_source_item_text_checkpoints import (
    NativeSourceItemTextCheckpointProgress,
    NativeSourceItemTextDurableCheckpointStore,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from tests.test_source_item_text_app_server_executor import (
    _WindowedTextTransportFactory,
    _scenario,
)


class NativeSourceItemTextCheckpointTests(unittest.TestCase):
    def test_object_store_progress_restores_complete_run_with_zero_agent_calls(
        self,
    ) -> None:
        with _scenario() as scenario, tempfile.TemporaryDirectory() as raw:
            object_store = LocalObjectStore(Path(raw) / "objects")
            context = _FakeJobContext()
            store = NativeSourceItemTextDurableCheckpointStore(
                object_store=object_store,
                project_id="project-1",
                document_id="document-1",
            )
            progress = NativeSourceItemTextCheckpointProgress(
                context=context,
                store=store,
            )
            first = scenario.executor(_WindowedTextTransportFactory()).execute(
                scenario.task,
                cancellation_requested=lambda: False,
                checkpoint_sink=progress.save,
            )

            self.assertEqual(context.progress["completed_scope_count"], 2)
            self.assertEqual(context.progress["completed_window_count"], 2)
            self.assertEqual(
                [item["stage"] for item in context.heartbeats[:3]],
                [
                    "SOURCE_ITEM_TEXT_BOUNDARY_CHECKPOINTED",
                    "SOURCE_ITEM_TEXT_BOUNDARIES_FROZEN",
                    "SOURCE_ITEM_TEXT_CLASSIFICATION_WINDOW_CHECKPOINTED",
                ],
            )
            receipt = context.progress["source_item_text_window_checkpoint"]
            self.assertTrue(object_store.stat(receipt["object_key"]))
            restored = NativeSourceItemTextCheckpointProgress(
                context=_FakeJobContext(),
                store=store,
                initial=context.progress,
            ).load()
            self.assertIsNotNone(restored)
            transport = _WindowedTextTransportFactory()
            resumed = scenario.executor(transport).execute(
                scenario.task,
                cancellation_requested=lambda: False,
                resume_checkpoint=restored.model_dump(mode="json"),
            )

        self.assertEqual(resumed.batch, first.batch)
        self.assertEqual(transport.total_calls, 0)

    def test_receipt_is_content_free_and_contains_no_object_payload(self) -> None:
        with _scenario() as scenario, tempfile.TemporaryDirectory() as raw:
            context = _FakeJobContext()
            progress = NativeSourceItemTextCheckpointProgress(
                context=context,
                store=NativeSourceItemTextDurableCheckpointStore(
                    object_store=LocalObjectStore(Path(raw) / "objects"),
                    project_id="project-1",
                    document_id="document-1",
                ),
            )
            scenario.executor(_WindowedTextTransportFactory()).execute(
                scenario.task,
                cancellation_requested=lambda: False,
                checkpoint_sink=progress.save,
            )
            receipt = context.progress["source_item_text_window_checkpoint"]
            encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)

        self.assertTrue(receipt["content_free"])
        for request in scenario.requests:
            for content in request.source_content:
                self.assertNotIn(content.text, encoded)
        for forbidden in ('"prompt"', '"candidate"', '"payload"', '"records"'):
            self.assertNotIn(forbidden, encoded)
        self.assertNotIn("object_payload", encoded)

    def test_checkpoint_object_tamper_fails_closed(self) -> None:
        with _scenario() as scenario, tempfile.TemporaryDirectory() as raw:
            object_store = LocalObjectStore(Path(raw) / "objects")
            context = _FakeJobContext()
            progress = NativeSourceItemTextCheckpointProgress(
                context=context,
                store=NativeSourceItemTextDurableCheckpointStore(
                    object_store=object_store,
                    project_id="project-1",
                    document_id="document-1",
                ),
            )
            scenario.executor(_WindowedTextTransportFactory()).execute(
                scenario.task,
                cancellation_requested=lambda: False,
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
