from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from datetime import UTC, datetime
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from app.adapters.agents.structure_failure_pattern_host import (
    StructureFailurePatternHost,
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
from app.application.agent_workflow.structure_tool_loop import (
    NativeStructureHandoffAttemptRequestV1,
    build_native_structure_handoff_attempt_receipt,
)
from app.application.agent_workflow.native_structure_working_revision import (
    build_accepted_reference_replay_revisions,
    native_structure_working_revision_bytes,
    native_structure_working_revision_object_key,
)
import hashlib
from app.infrastructure.postgres import PostgresDatabase
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow.document_map_builder import (
    assemble_and_publish_document_map_working_revision,
    project_document_map_scope_replacement,
)
from app.application.agent_workflow.document_map_replacement import (
    compile_document_map_scope_replacement,
)
from tests.document_map_replacement_support import build_replacement_fixture

if __package__:
    from .test_structure_tool_loop import _closed_observation, _sha
    from .test_native_structure_working_revision import _accepted_reference, _table_bindings
else:
    from test_structure_tool_loop import _closed_observation, _sha
    from test_native_structure_working_revision import _accepted_reference, _table_bindings


_POSTGRES_E2E_ENABLED = os.environ.get("REQPILOT_RUN_POSTGRES_INTEGRATION") == "1"


@unittest.skipUnless(
    _POSTGRES_E2E_ENABLED,
    "set REQPILOT_RUN_POSTGRES_INTEGRATION=1 for the local PostgreSQL E2E",
)
class AgentStructureLoopPostgresE2ETests(unittest.TestCase):
    def test_attempt_activity_replay_cursor_and_document_cascade(self) -> None:
        database = PostgresDatabase(
            os.environ.get(
                "REQPILOT_POSTGRES_INTEGRATION_URL",
                "postgresql://localhost/reqpilotx",
            )
        )
        database.apply_migrations(
            Path(__file__).resolve().parents[1] / "migrations" / "postgresql"
        )
        repository = PostgresAgentStructureLoopRepository(database)
        suffix = uuid.uuid4().hex
        project_id = f"structure-loop-e2e-project-{suffix}"
        document_id = f"structure-loop-e2e-document-{suffix}"
        source_sha256 = _sha("1")
        artifact_id = "structure-loop-e2e-artifact"
        storage_key = f"synthetic/{document_id}.pdf"
        now = datetime.now(UTC)
        request = NativeStructureHandoffAttemptRequestV1(
            command_id="structure-loop-e2e-attempt",
            expected_source_sha256=source_sha256,
            expected_artifact_id=artifact_id,
            handoff_id="structure-loop-e2e-handoff",
            attempt_ordinal=1,
            affected_scope_ids=("scope-1",),
            materialized_candidate_sha256=_sha("a"),
            rendered_output_sha256=_sha("2"),
            materialization_observation=_closed_observation(),
            current_action="正在复验局部结构。",
            public_summary="局部候选已经物化。",
        )
        host = StructureFailurePatternHost().validate_and_select(
            request.materialization_observation
        )
        receipt = build_native_structure_handoff_attempt_receipt(
            request,
            validation_report=host.validation_report,
            selected_failure_patterns=host.selected_failure_patterns,
            kit_version=host.kit_version,
            kit_bundle_sha256=host.kit_bundle_sha256,
        )
        draft = AgentStructureLoopActivityDraftV1(
            actor=AgentActivityActor.AGENT,
            family=AgentActivityFamily.ARTIFACT,
            kind="STRUCTURE_CANDIDATE_REVISION_PUBLISHED",
            title="Agent 已提交局部修订",
            summary="局部候选已经物化。",
            scope_kind="STRUCTURE_NODE",
            scope_ref_id="scope-1",
            occurred_at_utc=now,
        )

        try:
            with database.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO projects (id, name, customer_name, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (project_id, "Structure Loop E2E", "Synthetic", now),
                )
                connection.execute(
                    """
                    INSERT INTO stored_objects (
                        storage_key, sha256, size_bytes, media_type, object_kind
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        storage_key,
                        source_sha256,
                        1,
                        "application/pdf",
                        "sor-source",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sor_documents (
                        id, project_id, file_name, file_type, storage_key,
                        extracted_text, source_sha256, source_size, created_at,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        document_id,
                        project_id,
                        "synthetic.pdf",
                        "pdf",
                        storage_key,
                        "",
                        source_sha256,
                        1,
                        now,
                        now,
                    ),
                )

            persisted, created = repository.append_attempt(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                artifact_id=artifact_id,
                request=request,
                receipt=receipt,
            )
            replay, replay_created = repository.append_attempt(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                artifact_id=artifact_id,
                request=request,
                receipt=receipt,
            )
            event, event_created = repository.append_activity(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                artifact_id=artifact_id,
                handoff_id=request.handoff_id,
                command_id="structure-loop-e2e-activity",
                draft=draft,
            )
            event_replay, event_replay_created = repository.append_activity(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
                artifact_id=artifact_id,
                handoff_id=request.handoff_id,
                command_id="structure-loop-e2e-activity",
                draft=draft,
            )

            self.assertTrue(created)
            self.assertFalse(replay_created)
            self.assertEqual(persisted, replay)
            self.assertTrue(event_created)
            self.assertFalse(event_replay_created)
            self.assertEqual(event, event_replay)
            self.assertEqual(
                repository.list_attempts(
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    artifact_id=artifact_id,
                ),
                (receipt,),
            )
            self.assertEqual(
                repository.get_attempt_request(
                    project_id=project_id,
                    document_id=document_id,
                    attempt_id=receipt.attempt_id,
                ),
                request,
            )
            self.assertEqual(
                repository.list_activity(
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=source_sha256,
                    artifact_id=artifact_id,
                    handoff_id=request.handoff_id,
                    after_sequence=0,
                ),
                (event,),
            )
            revisions = build_accepted_reference_replay_revisions(
                _accepted_reference(),
                run_id="agent-run-" + "a" * 32,
                evidence_artifact_id="evidence-artifact-1",
                table_preview_bindings=_table_bindings(_accepted_reference()),
            )
            for working_revision in revisions:
                encoded = native_structure_working_revision_bytes(working_revision)
                repository.append_working_revision(
                    project_id=project_id,
                    document_id=document_id,
                    source_sha256=working_revision.source_sha256,
                    base_artifact_id=working_revision.base_artifact_id,
                    base_projection_sha256=(
                        working_revision.base_projection_sha256
                    ),
                    revision=working_revision,
                    object_key=(
                        f"synthetic/{document_id}/"
                        + native_structure_working_revision_object_key(
                            working_revision
                        )
                    ),
                    object_sha256=hashlib.sha256(encoded).hexdigest(),
                    object_size=len(encoded),
                    media_type=(
                        "application/vnd.reqpilotx."
                        "native-structure-working-revision+json"
                    ),
                    expected_head_revision_sha256=(
                        working_revision.previous_revision_sha256
                    ),
                )
            working_head = repository.get_working_revision_head(
                project_id=project_id,
                document_id=document_id,
                source_sha256=source_sha256,
            )
            self.assertIsNotNone(working_head)
            self.assertEqual(working_head.revision, 3)
            final_object = repository.get_working_revision_object(
                project_id=project_id,
                document_id=document_id,
                run_id=revisions[-1].run_id,
                revision=3,
            )
            self.assertEqual(
                final_object.created_at_utc,
                revisions[-1].created_at_utc,
            )
            with tempfile.TemporaryDirectory() as raw:
                fixture = build_replacement_fixture(
                    Path(raw) / "evidence",
                    run_id="agent-run-" + "b" * 32,
                )
                compilation = compile_document_map_scope_replacement(
                    request=fixture.request,
                    candidate=fixture.candidate,
                    evidence=fixture.evidence,
                    opened_high_resolution_page_numbers=(1, 2),
                    visual_receipt_root_sha256="f" * 64,
                )
                projection = project_document_map_scope_replacement(
                    compilation=compilation,
                    evidence=fixture.evidence,
                )
                d2_revision = assemble_and_publish_document_map_working_revision(
                    project_id=project_id,
                    document_id=document_id,
                    run_id=fixture.request.run_id,
                    source_sha256=source_sha256,
                    source_ir_canonical_sha256=fixture.request.scope_request.source_ir_canonical_sha256,
                    evidence_workspace_bundle_sha256=fixture.request.scope_request.evidence_workspace_bundle_sha256,
                    survey=fixture.survey,
                    scope_replacements=(projection,),
                    repository=repository,
                    object_store=LocalObjectStore(Path(raw) / "objects"),
                    kit_bundle_sha256="3" * 64,
                )
                persisted_d2 = repository.get_working_revision_object(
                    project_id=project_id,
                    document_id=document_id,
                    run_id=d2_revision.run_id,
                    revision=1,
                )
                self.assertEqual(
                    persisted_d2.d2_compiler_receipt_sha256,
                    d2_revision.d2_compiler_receipt_sha256,
                )
        finally:
            with database.connection() as connection:
                connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))
                remaining = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM agent_structure_handoff_attempts
                         WHERE project_id = %s) AS attempts,
                        (SELECT COUNT(*) FROM agent_structure_activity_events
                         WHERE project_id = %s) AS activity,
                        (SELECT COUNT(*) FROM agent_structure_working_revisions
                         WHERE project_id = %s) AS working_revisions,
                        (SELECT COUNT(*) FROM agent_structure_working_revision_heads
                         WHERE project_id = %s) AS working_heads
                    """,
                    (project_id, project_id, project_id, project_id),
                ).fetchone()
            database.close()
        self.assertEqual(int(remaining["attempts"]), 0)
        self.assertEqual(int(remaining["activity"]), 0)
        self.assertEqual(int(remaining["working_revisions"]), 0)
        self.assertEqual(int(remaining["working_heads"]), 0)


if __name__ == "__main__":
    unittest.main()
