from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
import hashlib
from io import BytesIO
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.adapters.persistence.memory_agent_structure_loop import (
    InMemoryAgentStructureLoopRepository,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.native_structure_working_revision import (
    NativeStructureWorkingRevisionError,
    build_accepted_reference_replay_revisions,
    native_structure_working_revision_bytes,
    native_structure_working_revision_object_key,
)
from app.application.agent_workflow.native_structure_working_revision_bridge import (
    get_native_structure_working_revision,
    get_native_structure_working_revision_head,
    get_native_structure_working_revision_table_preview,
    load_native_structure_working_revision,
    publish_native_structure_working_revision,
)
from app.application.agent_workflow.native_structure_working_revision_replay import (
    replay_accepted_native_structure_revisions,
)
from app.application.errors import ApplicationError

if __package__:
    from .test_native_structure_working_revision import _accepted_reference, _table_bindings, _table_previews
else:
    from test_native_structure_working_revision import _accepted_reference, _table_bindings, _table_previews


class _Response:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.status_code = 200


class _StrictLocalObjectStore(LocalObjectStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.put_count = 0

    def put(self, *, key, source, expected_sha256, media_type):  # type: ignore[no-untyped-def]
        target = self._path_for_key(key)
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() != (
            expected_sha256
        ):
            raise AssertionError("same object key received different immutable bytes")
        self.put_count += 1
        return super().put(
            key=key,
            source=source,
            expected_sha256=expected_sha256,
            media_type=media_type,
        )


class NativeStructureWorkingRevisionBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.object_store = LocalObjectStore(Path(self.temporary.name))
        self.repository = InMemoryAgentStructureLoopRepository()
        self.revisions = build_accepted_reference_replay_revisions(
            _accepted_reference(),
            run_id="agent-run-" + "a" * 32,
            evidence_artifact_id="evidence-artifact-1",
            table_preview_bindings=_table_bindings(_accepted_reference()),
        )

    def test_publisher_is_resumable_and_object_corruption_fails_closed(self) -> None:
        first_head, first_created = publish_native_structure_working_revision(
            project_id="project-1",
            document_id="document-1",
            revision=self.revisions[0],
            repository=self.repository,
            object_store=self.object_store,
        )
        replay_head, replay_created = publish_native_structure_working_revision(
            project_id="project-1",
            document_id="document-1",
            revision=self.revisions[0],
            repository=self.repository,
            object_store=self.object_store,
        )
        second_head, _ = publish_native_structure_working_revision(
            project_id="project-1",
            document_id="document-1",
            revision=self.revisions[1],
            repository=self.repository,
            object_store=self.object_store,
        )

        self.assertTrue(first_created)
        self.assertFalse(replay_created)
        self.assertEqual(first_head, replay_head)
        self.assertEqual(second_head.revision, 2)
        record = self.repository.get_working_revision_object(
            project_id="project-1",
            document_id="document-1",
            run_id=self.revisions[1].run_id,
            revision=2,
        )
        loaded = load_native_structure_working_revision(
            record=record,
            object_store=self.object_store,
        )
        self.assertEqual(loaded, self.revisions[1])

        target = self.object_store.root / record.object_key
        target.write_bytes(b"corrupt")
        with self.assertRaisesRegex(
            NativeStructureWorkingRevisionError,
            "object (size|SHA)",
        ):
            load_native_structure_working_revision(
                record=record,
                object_store=self.object_store,
            )

    def test_oversize_revision_is_rejected_before_object_put(self) -> None:
        store = Mock()
        with patch(
            "app.application.agent_workflow.native_structure_working_revision_bridge.native_structure_working_revision_bytes",
            return_value=b"x" * (16 * 1024 * 1024 + 1),
        ):
            with self.assertRaisesRegex(
                NativeStructureWorkingRevisionError, "publication budget"
            ):
                publish_native_structure_working_revision(
                    project_id="project-1", document_id="document-1",
                    revision=self.revisions[0], repository=self.repository,
                    object_store=store,
                )
        store.put.assert_not_called()

    def test_replay_can_resume_through_each_revision_without_pdf_or_agent(self) -> None:
        accepted = _accepted_reference()
        first = replay_accepted_native_structure_revisions(
            project_id="project-1",
            document_id="document-1",
            accepted_public_reference=accepted,
            table_previews=_table_previews(accepted),
            through_revision=1,
            repository=self.repository,
            object_store=self.object_store,
        )
        second = replay_accepted_native_structure_revisions(
            project_id="project-1",
            document_id="document-1",
            accepted_public_reference=accepted,
            table_previews=_table_previews(accepted),
            through_revision=2,
            repository=self.repository,
            object_store=self.object_store,
        )
        final = replay_accepted_native_structure_revisions(
            project_id="project-1",
            document_id="document-1",
            accepted_public_reference=accepted,
            table_previews=_table_previews(accepted),
            through_revision=3,
            repository=self.repository,
            object_store=self.object_store,
        )

        self.assertEqual(first["head"]["revision"], 1)
        self.assertEqual(second["head"]["revision"], 2)
        self.assertEqual(final["head"]["revision"], 3)
        self.assertEqual(final["head"]["state"], "ACCEPTED")
        self.assertEqual(final["published_revision_count"], 1)
        record = self.repository.get_working_revision_object(
            project_id="project-1",
            document_id="document-1",
            run_id=final["head"]["run_id"],
            revision=3,
        )
        loaded = load_native_structure_working_revision(
            record=record,
            object_store=self.object_store,
        )
        self.assertEqual(
            loaded.base_projection_sha256,
            accepted["structure_projection"]["projection_sha256"],
        )

    def test_replay_rejects_table_preview_identity_and_fragment_drift(self) -> None:
        accepted = _accepted_reference()
        base_preview = dict(_table_previews(accepted)[0])
        cases = {
            "schema": {**base_preview, "schema_version": "wrong-preview/v1"},
            "title": {**base_preview, "title": "Another table"},
            "fragments": {
                **base_preview,
                "fragments": [
                    {
                        **base_preview["fragments"][0],
                        "row_range": [1, 3],
                    }
                ],
            },
        }
        for label, preview in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    NativeStructureWorkingRevisionError,
                    "table preview",
                ):
                    replay_accepted_native_structure_revisions(
                        project_id="project-1",
                        document_id="document-1",
                        accepted_public_reference=accepted,
                        table_previews=[preview],
                        through_revision=1,
                        repository=InMemoryAgentStructureLoopRepository(),
                        object_store=self.object_store,
                    )

    def test_replay_rejects_table_preview_object_store_receipt_drift(self) -> None:
        stored = SimpleNamespace(
            key="wrong-key.json",
            sha256="0" * 64,
            size=1,
            media_type="application/octet-stream",
        )
        object_store = Mock()
        object_store.put.return_value = stored

        with self.assertRaisesRegex(
            NativeStructureWorkingRevisionError,
            "object-store receipt drifted",
        ):
            replay_accepted_native_structure_revisions(
                project_id="project-1",
                document_id="document-1",
                accepted_public_reference=_accepted_reference(),
                table_previews=_table_previews(_accepted_reference()),
                through_revision=1,
                repository=InMemoryAgentStructureLoopRepository(),
                object_store=object_store,
            )

    def test_general_publisher_skips_object_put_for_same_sha_new_timestamp(self) -> None:
        strict_store = _StrictLocalObjectStore(Path(self.temporary.name) / "strict")
        accepted = _accepted_reference()
        first = build_accepted_reference_replay_revisions(
            accepted,
            run_id="agent-run-" + "b" * 32,
            evidence_artifact_id="evidence-artifact-1",
            table_preview_bindings=_table_bindings(accepted),
            clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        )[0]
        later = build_accepted_reference_replay_revisions(
            accepted,
            run_id="agent-run-" + "b" * 32,
            evidence_artifact_id="evidence-artifact-1",
            table_preview_bindings=_table_bindings(accepted),
            clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
            + timedelta(hours=1),
        )[0]
        self.assertEqual(first.revision_sha256, later.revision_sha256)
        self.assertNotEqual(first.created_at_utc, later.created_at_utc)

        publish_native_structure_working_revision(
            project_id="project-1",
            document_id="document-1",
            revision=first,
            repository=self.repository,
            object_store=strict_store,
        )
        replay_head, created = publish_native_structure_working_revision(
            project_id="project-1",
            document_id="document-1",
            revision=later,
            repository=self.repository,
            object_store=strict_store,
        )

        self.assertFalse(created)
        self.assertEqual(replay_head.created_at_utc, first.created_at_utc)
        self.assertEqual(strict_store.put_count, 1)

        orphan_store = _StrictLocalObjectStore(Path(self.temporary.name) / "orphan")
        orphan_bytes = native_structure_working_revision_bytes(first)
        orphan_key = native_structure_working_revision_object_key(first)
        orphan_store.put(
            key=orphan_key,
            source=BytesIO(orphan_bytes),
            expected_sha256=hashlib.sha256(orphan_bytes).hexdigest(),
            media_type=(
                "application/vnd.reqpilotx.native-structure-working-revision+json"
            ),
        )
        later_key = native_structure_working_revision_object_key(later)
        self.assertNotEqual(orphan_key, later_key)
        fresh_repository = InMemoryAgentStructureLoopRepository()
        _, orphan_recovered = publish_native_structure_working_revision(
            project_id="project-1",
            document_id="document-1",
            revision=later,
            repository=fresh_repository,
            object_store=orphan_store,
        )
        self.assertTrue(orphan_recovered)

    def test_head_and_full_revision_serving_do_not_require_native_reference(self) -> None:
        for revision in self.revisions:
            publish_native_structure_working_revision(
                project_id="project-1",
                document_id="document-1",
                revision=revision,
                repository=self.repository,
                object_store=self.object_store,
            )
        runtime_container = SimpleNamespace(
            agent_structure_loop_repository=self.repository,
            object_store=self.object_store,
        )
        document = {"id": "document-1"}
        with (
            patch(
                "app.application.agent_workflow.native_structure_working_revision_bridge._require_project_document",
                return_value=document,
            ),
            patch(
                "app.application.agent_workflow.native_structure_working_revision_bridge._document_source_sha256",
                return_value="1" * 64,
            ),
            patch(
                "app.application.agent_workflow.native_structure_working_revision_bridge.RUNTIME_STATE.container",
                runtime_container,
            ),
            patch(
                "app.application.agent_workflow.native_structure_baseline_jobs._current_reference_context",
                side_effect=ApplicationError(
                    status_code=404,
                    detail={"code": "not_found", "message": "not found"},
                ),
            ),
        ):
            head_response = get_native_structure_working_revision_head(
                "project-1", "document-1", _Response()
            )
            self.assertIsNone(head_response["head"])
            direct_head = get_native_structure_working_revision(
                "project-1", "document-1", self.revisions[0].run_id, 3,
                _Response(),
            )
            self.assertEqual(direct_head["revision"], 3)
            # Historical immutable revisions remain explicitly addressable, but
            # a replay head cannot bootstrap the product without its accepted base.
            fetched = [
                get_native_structure_working_revision(
                    "project-1",
                    "document-1",
                    self.revisions[0].run_id,
                    revision,
                    _Response(),
                )
                for revision in (1, 2, 3)
            ]
        self.assertEqual([item["revision"] for item in fetched], [1, 2, 3])
        self.assertEqual(
            fetched[1]["previous_revision_sha256"],
            fetched[0]["revision_sha256"],
        )

    def test_head_endpoint_does_not_return_stale_base_projection(self) -> None:
        publish_native_structure_working_revision(
            project_id="project-1", document_id="document-1",
            revision=self.revisions[0], repository=self.repository,
            object_store=self.object_store,
        )
        runtime_container = SimpleNamespace(
            agent_structure_loop_repository=self.repository,
            object_store=self.object_store,
        )
        with (
            patch(
                "app.application.agent_workflow.native_structure_working_revision_bridge._require_project_document",
                return_value={"id": "document-1"},
            ),
            patch(
                "app.application.agent_workflow.native_structure_working_revision_bridge._document_source_sha256",
                return_value="1" * 64,
            ),
            patch(
                "app.application.agent_workflow.native_structure_working_revision_bridge.RUNTIME_STATE.container",
                runtime_container,
            ),
            patch(
                "app.application.agent_workflow.native_structure_baseline_jobs._current_reference_context",
                return_value={
                    "artifact_id": "new-artifact",
                    "public_reference": {
                        "structure_projection": {"projection_sha256": "9" * 64}
                    },
                },
            ),
        ):
            response = get_native_structure_working_revision_head(
                "project-1", "document-1", _Response()
            )
        self.assertIsNone(response["head"])

    def test_full_revision_sha_and_table_scope_are_revision_bound(self) -> None:
        result = replay_accepted_native_structure_revisions(
            project_id="project-1",
            document_id="document-1",
            accepted_public_reference=_accepted_reference(),
            table_previews=_table_previews(_accepted_reference()),
            through_revision=2,
            repository=self.repository,
            object_store=self.object_store,
        )
        record = self.repository.get_working_revision_object(
            project_id="project-1", document_id="document-1",
            run_id=result["head"]["run_id"], revision=2,
        )
        revision = load_native_structure_working_revision(
            record=record, object_store=self.object_store
        )
        runtime_container = SimpleNamespace(
            agent_structure_loop_repository=self.repository,
            object_store=self.object_store,
        )
        with (
            patch(
                "app.application.agent_workflow.native_structure_working_revision_bridge._require_project_document",
                return_value={"id": "document-1"},
            ),
            patch(
                "app.application.agent_workflow.native_structure_working_revision_bridge._document_source_sha256",
                return_value="1" * 64,
            ),
            patch(
                "app.application.agent_workflow.native_structure_working_revision_bridge.RUNTIME_STATE.container",
                runtime_container,
            ),
        ):
            with self.assertRaises(ApplicationError):
                get_native_structure_working_revision(
                    "project-1",
                    "document-1",
                    revision.run_id,
                    2,
                    _Response(),
                    expected_revision_sha256="f" * 64,
                )
            table = get_native_structure_working_revision_table_preview(
                "project-1",
                "document-1",
                revision.run_id,
                2,
                "table-a",
                _Response(),
                expected_revision_sha256=revision.revision_sha256,
            )
            self.assertEqual(table["logical_table_scope_id"], "table-a")
            with self.assertRaises(ApplicationError):
                get_native_structure_working_revision_table_preview(
                    "project-1",
                    "document-1",
                    revision.run_id,
                    2,
                    "table-missing",
                    _Response(),
                    expected_revision_sha256=revision.revision_sha256,
                )
        d2_payload = revision.model_dump(mode="json")
        d2_payload["validator_kind"] = "D2_HOST_COMPILER"
        d2_payload["quality_claim"] = "D2_HOST_VERIFIED"
        with patch(
            "app.application.agent_workflow.native_structure_working_revision_bridge.get_native_structure_working_revision",
            return_value=d2_payload,
        ):
            with self.assertRaises(ApplicationError) as raised:
                get_native_structure_working_revision_table_preview(
                    "project-1",
                    "document-1",
                    revision.run_id,
                    2,
                    "table-a",
                    _Response(),
                    expected_revision_sha256=revision.revision_sha256,
                )
        self.assertEqual(
            raised.exception.detail["code"],
            "working_revision_table_evidence_not_materialized",
        )


if __name__ == "__main__":
    unittest.main()
