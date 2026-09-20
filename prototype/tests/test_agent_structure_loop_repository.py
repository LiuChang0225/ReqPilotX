from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import unittest
from datetime import UTC, datetime
from inspect import getsource

from app.adapters.agents.structure_failure_pattern_host import (
    StructureFailurePatternHost,
)
from app.adapters.persistence.memory_agent_structure_loop import (
    InMemoryAgentStructureLoopRepository,
)
from app.adapters.persistence.postgres_agent_structure_loop import (
    PostgresAgentStructureLoopRepository,
)
from app.application.agent_workflow.activity import (
    AgentActivityActor,
    AgentActivityFamily,
)
from app.application.agent_workflow.structure_loop_persistence import (
    AgentStructureLoopActivityDraftV1,
)
from app.application.agent_workflow.native_structure_working_revision import (
    build_accepted_reference_replay_revisions,
    canonical_json_sha256,
    native_structure_working_revision_bytes,
    native_structure_working_revision_object_key,
)
from app.application.agent_workflow.structure_tool_loop import (
    NativeStructureHandoffAttemptReceiptV1,
    NativeStructureHandoffAttemptRequestV1,
    active_structure_projection_attempt,
    build_native_structure_handoff_attempt_receipt,
)
from app.ports.agent_structure_loop_repository import AgentStructureLoopConflict

if __package__:
    from .test_structure_tool_loop import _closed_observation, _sha
    from .test_native_structure_working_revision import _accepted_reference, _table_bindings
else:
    from test_structure_tool_loop import _closed_observation, _sha
    from test_native_structure_working_revision import _accepted_reference, _table_bindings


class AgentStructureLoopRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryAgentStructureLoopRepository()
        self.request = NativeStructureHandoffAttemptRequestV1(
            command_id="attempt-command-1",
            expected_source_sha256=_sha("1"),
            expected_artifact_id="artifact-1",
            handoff_id="handoff-1",
            attempt_ordinal=1,
            affected_scope_ids=("scope-1",),
            materialized_candidate_sha256=_sha("a"),
            rendered_output_sha256=_sha("2"),
            materialization_observation=_closed_observation(),
            current_action="正在复验局部结构。",
            public_summary="局部候选已物化并等待 Host 确认。",
        )
        host = StructureFailurePatternHost().validate_and_select(
            self.request.materialization_observation
        )
        self.receipt = build_native_structure_handoff_attempt_receipt(
            self.request,
            validation_report=host.validation_report,
            selected_failure_patterns=host.selected_failure_patterns,
            kit_version=host.kit_version,
            kit_bundle_sha256=host.kit_bundle_sha256,
        )

    def test_attempt_is_immutable_idempotent_and_stream_scoped(self) -> None:
        first, created = self.repository.append_attempt(
            **self._scope(),
            request=self.request,
            receipt=self.receipt,
        )
        replay, replay_created = self.repository.append_attempt(
            **self._scope(),
            request=self.request,
            receipt=self.receipt,
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first, replay)
        self.assertEqual(
            self.repository.list_attempts(**self._scope()),
            (self.receipt,),
        )
        self.assertEqual(
            self.repository.list_attempts(
                **{**self._scope(), "document_id": "another-document"}
            ),
            (),
        )

        changed = self.request.model_copy(update={"public_summary": "changed"})
        with self.assertRaisesRegex(
            AgentStructureLoopConflict,
            "replay changed payload",
        ):
            self.repository.append_attempt(
                **self._scope(),
                request=changed,
                receipt=self.receipt,
            )

    def test_attempt_compare_and_swap_rejects_a_stale_global_head(self) -> None:
        self.repository.append_attempt(
            **self._scope(),
            request=self.request,
            receipt=self.receipt,
        )
        second_request, second_receipt = self._attempt(
            command_id="attempt-command-2",
            handoff_id="handoff-2",
            candidate_marker="b",
        )

        with self.assertRaisesRegex(
            AgentStructureLoopConflict,
            "global projection head changed",
        ):
            self.repository.append_attempt(
                **self._scope(),
                request=second_request,
                receipt=second_receipt,
                expected_global_head_attempt_id="stale-attempt-id",
            )

    def test_exact_replay_precedes_global_head_compare_and_swap(self) -> None:
        persisted, _ = self.repository.append_attempt(
            **self._scope(),
            request=self.request,
            receipt=self.receipt,
        )
        second_request, second_receipt = self._attempt(
            command_id="attempt-command-2",
            handoff_id="handoff-2",
            candidate_marker="b",
        )
        self.repository.append_attempt(
            **self._scope(),
            request=second_request,
            receipt=second_receipt,
        )

        replay, created = self.repository.append_attempt(
            **self._scope(),
            request=self.request,
            receipt=self.receipt,
            expected_global_head_attempt_id=self.receipt.attempt_id,
            expected_global_projection_attempts_sha256="0" * 64,
        )

        self.assertFalse(created)
        self.assertEqual(replay, persisted)

    def test_attempt_set_cas_rejects_delayed_older_adopted_write(self) -> None:
        handoff_2_request, handoff_2_receipt = self._attempt(
            command_id="handoff-2-current-head",
            handoff_id="handoff-2",
            candidate_marker="d",
        )
        handoff_2_receipt = handoff_2_receipt.model_copy(
            update={"created_at_utc": datetime(2026, 8, 31, 9, 0, tzinfo=UTC)}
        )
        self.repository.append_attempt(
            **self._scope(),
            request=handoff_2_request,
            receipt=handoff_2_receipt,
        )
        snapshot_digest = self._projection_attempt_set_digest(
            (handoff_2_receipt,)
        )

        delayed_request, delayed_receipt = self._attempt(
            command_id="handoff-1-delayed-older",
            handoff_id="handoff-1",
            candidate_marker="e",
        )
        delayed_receipt = delayed_receipt.model_copy(
            update={"created_at_utc": datetime(2026, 8, 31, 8, 0, tzinfo=UTC)}
        )
        self.repository.append_attempt(
            **self._scope(),
            request=delayed_request,
            receipt=delayed_receipt,
        )
        current_attempts = self.repository.list_attempts(**self._scope())
        current_head = active_structure_projection_attempt(current_attempts)
        self.assertEqual(current_head.attempt_id, handoff_2_receipt.attempt_id)

        next_request, next_receipt = self._attempt(
            command_id="handoff-3-cas",
            handoff_id="handoff-3",
            candidate_marker="f",
        )
        with self.assertRaisesRegex(
            AgentStructureLoopConflict,
            "projection attempt set changed",
        ):
            self.repository.append_attempt(
                **self._scope(),
                request=next_request,
                receipt=next_receipt,
                expected_global_head_attempt_id=handoff_2_receipt.attempt_id,
                expected_global_projection_attempts_sha256=snapshot_digest,
            )

    def test_active_head_uses_latest_interleaved_attempt_per_handoff(self) -> None:
        def working_attempt(
            *,
            command_id: str,
            handoff_id: str,
            candidate_marker: str,
            created_at: datetime,
            previous: NativeStructureHandoffAttemptReceiptV1 | None = None,
        ) -> NativeStructureHandoffAttemptReceiptV1:
            request = NativeStructureHandoffAttemptRequestV1(
                command_id=command_id,
                expected_source_sha256=_sha("1"),
                expected_artifact_id="artifact-1",
                handoff_id=handoff_id,
                attempt_ordinal=(
                    previous.attempt_ordinal + 1 if previous is not None else 1
                ),
                previous_attempt_receipt_sha256=(
                    previous.receipt_sha256 if previous is not None else None
                ),
                affected_scope_ids=("scope-1",),
                materialized_candidate_sha256=_sha(candidate_marker),
                rendered_output_sha256=_sha("2"),
                materialization_observation=_closed_observation(),
                current_action="正在复验局部结构。",
                public_summary="局部候选已物化并等待 Host 确认。",
            )
            receipt = build_native_structure_handoff_attempt_receipt(
                request,
                validation_report={
                    "status": "FAIL",
                    "findings": [
                        {"pattern_id": "confirmed_proposal_not_verified"}
                    ],
                },
                selected_failure_patterns={"selected_patterns": []},
                kit_version="0.8.10",
                kit_bundle_sha256=_sha("8"),
                previous_attempt=previous,
            )
            return receipt.model_copy(update={"created_at_utc": created_at})

        handoff_1_first = working_attempt(
            command_id="handoff-1-first",
            handoff_id="handoff-1",
            candidate_marker="a",
            created_at=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
        )
        handoff_2 = working_attempt(
            command_id="handoff-2-first",
            handoff_id="handoff-2",
            candidate_marker="b",
            created_at=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
        )
        handoff_1_latest = working_attempt(
            command_id="handoff-1-latest",
            handoff_id="handoff-1",
            candidate_marker="c",
            created_at=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
            previous=handoff_1_first,
        )

        head = active_structure_projection_attempt(
            (handoff_1_first, handoff_2, handoff_1_latest)
        )

        self.assertIsNotNone(head)
        self.assertEqual(head.attempt_id, handoff_1_latest.attempt_id)

    def test_postgres_attempt_lock_is_document_scoped_not_handoff_scoped(
        self,
    ) -> None:
        source = getsource(PostgresAgentStructureLoopRepository.append_attempt)

        self.assertIn("_lock_stream(connection, scope)", source)
        self.assertNotIn(
            "_lock_stream(connection, (*scope, request.handoff_id))",
            source,
        )

    def test_activity_assigns_contiguous_sequence_and_incremental_cursor(self) -> None:
        first_draft = self._draft("STRUCTURE_CANDIDATE_REVISION_PUBLISHED")
        first, created = self.repository.append_activity(
            **self._scope(),
            handoff_id="handoff-1",
            command_id="activity-1",
            draft=first_draft,
        )
        replay, replay_created = self.repository.append_activity(
            **self._scope(),
            handoff_id="handoff-1",
            command_id="activity-1",
            draft=first_draft,
        )
        second, _ = self.repository.append_activity(
            **self._scope(),
            handoff_id="handoff-1",
            command_id="activity-2",
            draft=self._draft("STRUCTURE_MATERIALIZATION_VERIFIED"),
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first, replay)
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual(
            self.repository.list_activity(
                **self._scope(),
                handoff_id="handoff-1",
                after_sequence=1,
            ),
            (second,),
        )

    def test_activity_revalidates_public_parameters(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret or CoT"):
            self.repository.append_activity(
                **self._scope(),
                handoff_id="handoff-1",
                command_id="activity-secret",
                draft=AgentStructureLoopActivityDraftV1(
                    actor=AgentActivityActor.AGENT,
                    family=AgentActivityFamily.ACTIVITY,
                    kind="STRUCTURE_ACTIVITY",
                    title="Unsafe",
                    authorized_parameters={"api_key": "secret"},
                ),
            )

    def test_working_revision_is_immutable_idempotent_and_head_cas_bound(self) -> None:
        revisions = self._working_revisions()
        first = revisions[0]
        first_bytes = native_structure_working_revision_bytes(first)
        first_key = native_structure_working_revision_object_key(first)
        head, created = self.repository.append_working_revision(
            **self._working_scope(),
            revision=first,
            object_key=first_key,
            object_sha256=hashlib.sha256(first_bytes).hexdigest(),
            object_size=len(first_bytes),
            media_type=(
                "application/vnd.reqpilotx.native-structure-working-revision+json"
            ),
            expected_head_revision_sha256=None,
        )
        replay, replay_created = self.repository.append_working_revision(
            **self._working_scope(),
            revision=first,
            object_key=first_key,
            object_sha256=hashlib.sha256(first_bytes).hexdigest(),
            object_size=len(first_bytes),
            media_type=(
                "application/vnd.reqpilotx.native-structure-working-revision+json"
            ),
            expected_head_revision_sha256="f" * 64,
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay, head)
        self.assertEqual(
            self.repository.get_working_revision_head(
                project_id="project-1",
                document_id="document-1",
                source_sha256="1" * 64,
            ),
            head,
        )

        second = revisions[1]
        second_bytes = native_structure_working_revision_bytes(second)
        second_key = native_structure_working_revision_object_key(second)
        with self.assertRaisesRegex(
            AgentStructureLoopConflict,
            "working revision head changed",
        ):
            self.repository.append_working_revision(
                **self._working_scope(),
                revision=second,
                object_key=second_key,
                object_sha256=hashlib.sha256(second_bytes).hexdigest(),
                object_size=len(second_bytes),
                media_type=(
                    "application/vnd.reqpilotx.native-structure-working-revision+json"
                ),
                expected_head_revision_sha256="e" * 64,
            )
        second_head, second_created = self.repository.append_working_revision(
            **self._working_scope(),
            revision=second,
            object_key=second_key,
            object_sha256=hashlib.sha256(second_bytes).hexdigest(),
            object_size=len(second_bytes),
            media_type=(
                "application/vnd.reqpilotx.native-structure-working-revision+json"
            ),
            expected_head_revision_sha256=first.revision_sha256,
        )
        self.assertTrue(second_created)
        self.assertEqual(second_head.revision, 2)
        stored = self.repository.get_working_revision_object(
            project_id="project-1",
            document_id="document-1",
            run_id=second.run_id,
            revision=2,
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored.revision_sha256, second.revision_sha256)
        self.assertEqual(stored.object_size, len(second_bytes))

    def test_working_revision_replay_with_changed_object_metadata_is_rejected(self) -> None:
        first = self._working_revisions()[0]
        encoded = native_structure_working_revision_bytes(first)
        arguments = {
            **self._working_scope(),
            "revision": first,
            "object_key": native_structure_working_revision_object_key(first),
            "object_sha256": hashlib.sha256(encoded).hexdigest(),
            "object_size": len(encoded),
            "media_type": (
                "application/vnd.reqpilotx.native-structure-working-revision+json"
            ),
            "expected_head_revision_sha256": None,
        }
        self.repository.append_working_revision(**arguments)

        with self.assertRaisesRegex(
            AgentStructureLoopConflict,
            "working revision replay changed payload",
        ):
            self.repository.append_working_revision(
                **{**arguments, "object_sha256": "a" * 64}
            )

    def test_working_head_filters_stale_base_and_new_base_can_start(self) -> None:
        first = self._working_revisions()[0]
        encoded = native_structure_working_revision_bytes(first)
        self.repository.append_working_revision(
            **self._working_scope(), revision=first,
            object_key=native_structure_working_revision_object_key(first),
            object_sha256=hashlib.sha256(encoded).hexdigest(),
            object_size=len(encoded),
            media_type="application/vnd.reqpilotx.native-structure-working-revision+json",
            expected_head_revision_sha256=None,
        )
        self.assertIsNone(self.repository.get_working_revision_head(
            project_id="project-1", document_id="document-1",
            source_sha256="1" * 64, base_artifact_id="new-artifact",
            base_projection_sha256="9" * 64,
        ))
        new = _accepted_reference()
        new_tree = {**new["document_tree"], "nodes": [
            ({**item, "title": "new root"} if item["node_id"] == "root-a" else item)
            for item in new["document_tree"]["nodes"]
        ]}
        new["document_tree"] = new_tree
        new["artifact_id"] = "new-artifact"
        new["structure_projection"] = {
            **new["structure_projection"], "base_artifact_id": "new-artifact",
            "projection_sha256": canonical_json_sha256(new_tree),
        }
        new_revision = build_accepted_reference_replay_revisions(
            new, run_id="agent-run-" + "9" * 32,
            evidence_artifact_id="new-evidence",
            table_preview_bindings=_table_bindings(new),
        )[0]
        new_encoded = native_structure_working_revision_bytes(new_revision)
        self.repository.append_working_revision(
            project_id="project-1", document_id="document-1",
            source_sha256="1" * 64, base_artifact_id="new-artifact",
            base_projection_sha256=new["structure_projection"]["projection_sha256"],
            revision=new_revision,
            object_key=native_structure_working_revision_object_key(new_revision),
            object_sha256=hashlib.sha256(new_encoded).hexdigest(),
            object_size=len(new_encoded),
            media_type="application/vnd.reqpilotx.native-structure-working-revision+json",
            expected_head_revision_sha256=None,
        )
        latest = self.repository.get_working_revision_head(
            project_id="project-1", document_id="document-1",
            source_sha256="1" * 64,
        )
        self.assertEqual(latest.base_artifact_id, "new-artifact")

    @staticmethod
    def _draft(kind: str) -> AgentStructureLoopActivityDraftV1:
        return AgentStructureLoopActivityDraftV1(
            actor=AgentActivityActor.AGENT,
            family=AgentActivityFamily.ARTIFACT,
            kind=kind,
            title="结构工作流更新",
            summary="当前范围已更新。",
            scope_kind="STRUCTURE_NODE",
            scope_ref_id="scope-1",
            status="IN_PROGRESS",
            authorized_parameters={"revision_count": 1},
        )

    def _attempt(
        self,
        *,
        command_id: str,
        handoff_id: str,
        candidate_marker: str,
    ) -> tuple[
        NativeStructureHandoffAttemptRequestV1,
        NativeStructureHandoffAttemptReceiptV1,
    ]:
        request = NativeStructureHandoffAttemptRequestV1(
            command_id=command_id,
            expected_source_sha256=_sha("1"),
            expected_artifact_id="artifact-1",
            handoff_id=handoff_id,
            attempt_ordinal=1,
            affected_scope_ids=("scope-1",),
            materialized_candidate_sha256=_sha(candidate_marker),
            rendered_output_sha256=_sha("2"),
            materialization_observation=_closed_observation(),
            current_action="正在复验局部结构。",
            public_summary="局部候选已物化并等待 Host 确认。",
        )
        host = StructureFailurePatternHost().validate_and_select(
            request.materialization_observation
        )
        return request, build_native_structure_handoff_attempt_receipt(
            request,
            validation_report=host.validation_report,
            selected_failure_patterns=host.selected_failure_patterns,
            kit_version=host.kit_version,
            kit_bundle_sha256=host.kit_bundle_sha256,
        )

    @staticmethod
    def _projection_attempt_set_digest(
        attempts: tuple[NativeStructureHandoffAttemptReceiptV1, ...],
    ) -> str:
        payload = {
            "schema_version": "native-structure-projection-attempt-set/v1",
            "attempts": [
                {
                    "attempt_id": attempt.attempt_id,
                    "receipt_sha256": attempt.receipt_sha256,
                    "status": attempt.status.value,
                }
                for attempt in sorted(
                    attempts,
                    key=lambda item: (
                        item.created_at_utc,
                        item.handoff_id,
                        item.attempt_ordinal,
                        item.attempt_id,
                    ),
                )
                if attempt.status.value
                in {"VERIFIED", "VERIFICATION_REQUIRED"}
            ],
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _working_revisions():
        return build_accepted_reference_replay_revisions(
            _accepted_reference(),
            run_id="agent-run-" + "a" * 32,
            evidence_artifact_id="evidence-artifact-1",
            table_preview_bindings=_table_bindings(_accepted_reference()),
            clock=lambda: datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        )

    @staticmethod
    def _working_scope() -> dict[str, str]:
        accepted = _accepted_reference()
        return {
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": "1" * 64,
            "base_artifact_id": "accepted-artifact-1",
            "base_projection_sha256": canonical_json_sha256(
                accepted["document_tree"]
            ),
        }

    @staticmethod
    def _scope() -> dict[str, str]:
        return {
            "project_id": "project-1",
            "document_id": "document-1",
            "source_sha256": _sha("1"),
            "artifact_id": "artifact-1",
        }


if __name__ == "__main__":
    unittest.main()
