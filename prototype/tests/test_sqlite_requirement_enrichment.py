from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import db
from app.adapters.persistence.sqlite_requirement_enrichment import (
    SQLiteRequirementEnrichmentRepository,
)
from app.application.extraction.llm_requirement_review import (
    evaluate_llm_requirement_review,
    initial_llm_requirement_review,
)
from app.application.extraction.requirement_enrichment import (
    build_requirement_atomization_run,
    build_requirement_publication_preview,
    build_requirement_refinement_suggestion,
    revise_requirement_refinement_suggestion,
)
from app.domain.extraction.llm_requirement_review import (
    LlmRequirementReviewSession,
)
from app.domain.extraction.requirement_enrichment import (
    RequirementAtomizationRun,
    RequirementRefinementOperation,
    stable_atomization_run_id,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest,
    RequirementAtomizationSourceSegment,
)
from app.ports.requirement_enrichment_repository import (
    RequirementEnrichmentConflictError,
)
from tests.test_llm_requirement_review import _prediction
from tests.test_requirement_enrichment import (
    _AtomizationProvider,
    _RefinementProvider,
    _coverage,
)


class SQLiteRequirementEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        db.init_db()
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        self.project = db.create_project(
            name="Enrichment",
            customer_name="Customer",
        )
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name=source.name,
            file_type="pdf",
            file_path=str(source),
            extracted_text="",
            source_sha256="b" * 64,
            source_size=source.stat().st_size,
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )
        active = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            source_hash="b" * 64,
        )
        self.active = db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            active["id"],
        )
        prediction = _prediction()
        reviewed = initial_llm_requirement_review(prediction)
        now = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
        self.session = LlmRequirementReviewSession(
            session_id="llm-review-" + "8" * 32,
            project_id=self.project["id"],
            document_id=self.document["id"],
            prediction=prediction,
            reviewed_result=reviewed,
            delta=evaluate_llm_requirement_review(prediction, reviewed),
            created_at_utc=now,
            updated_at_utc=now,
        )
        self.repository = SQLiteRequirementEnrichmentRepository(db.DB_PATH)

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.temporary.cleanup()

    def test_atomization_checkpoint_is_exact_bound_and_idempotent(self) -> None:
        request = _atomization_checkpoint_request()
        result = _AtomizationProvider().atomize(request)
        binding = {
            "project_id": self.project["id"],
            "document_id": self.document["id"],
            "session_id": self.session.session_id,
            "review_revision": self.session.reviewed_result.revision,
            "lineage_sha256": request.lineage_sha256,
            "request": request,
        }

        self.assertIsNone(self.repository.get_atomization_checkpoint(**binding))
        first = self.repository.create_or_get_atomization_checkpoint(
            **binding,
            result=result,
        )
        second = self.repository.create_or_get_atomization_checkpoint(
            **binding,
            result=result,
        )

        self.assertEqual(first, result)
        self.assertEqual(second, result)
        self.assertEqual(
            self.repository.get_atomization_checkpoint(**binding),
            result,
        )
        self.assertEqual(
            self.repository.count_atomization_checkpoints(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
                review_revision=self.session.reviewed_result.revision,
                lineage_sha256=request.lineage_sha256,
            ),
            1,
        )
        for binding_override in (
            {"project_id": f"{self.project['id']}-other"},
            {"document_id": f"{self.document['id']}-other"},
            {"session_id": f"{self.session.session_id}-other"},
            {"review_revision": self.session.reviewed_result.revision + 1},
        ):
            with (
                self.subTest(binding_override=binding_override),
                self.assertRaisesRegex(
                    RequirementEnrichmentConflictError,
                    "binding conflicts",
                ),
            ):
                self.repository.get_atomization_checkpoint(
                    **{
                        **binding,
                        **binding_override,
                    }
                )

        changed_request = RequirementAtomizationProviderRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "parent_context_statement": "A changed parent context.",
            }
        )
        with self.assertRaisesRegex(
            RequirementEnrichmentConflictError,
            "binding conflicts",
        ):
            self.repository.get_atomization_checkpoint(
                **{
                    **binding,
                    "request": changed_request,
                }
            )

    def test_atomization_checkpoint_rejects_conflicting_provider_output(self) -> None:
        request = _atomization_checkpoint_request()
        original = _AtomizationProvider(
            atomic_statements=("The component shall satisfy the source duty.",),
        ).atomize(request)
        conflicting = _AtomizationProvider(
            atomic_statements=("The component shall satisfy a different duty.",),
        ).atomize(request)
        binding = {
            "project_id": self.project["id"],
            "document_id": self.document["id"],
            "session_id": self.session.session_id,
            "review_revision": self.session.reviewed_result.revision,
            "lineage_sha256": request.lineage_sha256,
            "request": request,
        }
        self.repository.create_or_get_atomization_checkpoint(
            **binding,
            result=original,
        )

        with self.assertRaisesRegex(
            RequirementEnrichmentConflictError,
            "different provider output",
        ):
            self.repository.create_or_get_atomization_checkpoint(
                **binding,
                result=conflicting,
            )

    def test_migrates_legacy_v2_run_and_allows_v3_for_same_revision(self) -> None:
        current_run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(),
        )
        legacy_run_id = "atomization-run-" + "a" * 32
        legacy_payload = current_run.model_dump(mode="json")
        legacy_payload["schema_version"] = "requirement-atomization-run/v2"
        legacy_payload["atomization_run_id"] = legacy_run_id
        legacy_payload["algorithm_version"] = "source-bound-provider-atomization/v2"
        for node in legacy_payload["nodes"]:
            node.pop("atomic_statement", None)
        legacy_json = json.dumps(
            legacy_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy_sha256 = hashlib.sha256(legacy_json.encode("utf-8")).hexdigest()

        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP TABLE requirement_atomization_runs")
            connection.execute(
                """
                CREATE TABLE requirement_atomization_runs (
                    atomization_run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    review_revision INTEGER NOT NULL CHECK(review_revision >= 0),
                    lineage_sha256 TEXT NOT NULL CHECK(length(lineage_sha256) = 64),
                    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    UNIQUE(project_id, document_id, session_id, review_revision),
                    FOREIGN KEY(document_id)
                        REFERENCES sor_documents(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                INSERT INTO requirement_atomization_runs (
                    atomization_run_id, project_id, document_id, session_id,
                    review_revision, lineage_sha256, payload_sha256,
                    payload_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_run_id,
                    self.project["id"],
                    self.document["id"],
                    self.session.session_id,
                    self.session.reviewed_result.revision,
                    current_run.lineage.lineage_sha256,
                    legacy_sha256,
                    legacy_json,
                    current_run.created_at_utc.isoformat(),
                ),
            )
            connection.commit()

        repository = SQLiteRequirementEnrichmentRepository(db.DB_PATH)
        self.assertIsNone(
            repository.get_latest_atomization(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
            )
        )
        self.assertIsNone(
            repository.get_atomization(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
                atomization_run_id=legacy_run_id,
            )
        )

        stored = repository.create_or_get_atomization(current_run)
        self.assertEqual(stored, current_run)
        self.assertEqual(
            repository.get_latest_atomization(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
            ),
            current_run,
        )
        with sqlite3.connect(db.DB_PATH) as connection:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(requirement_atomization_runs)"
                ).fetchall()
            }
            schema_versions = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT schema_version FROM requirement_atomization_runs
                    ORDER BY schema_version
                    """
                ).fetchall()
            ]
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        self.assertIn("schema_version", columns)
        self.assertEqual(
            schema_versions,
            [
                "requirement-atomization-run/v2",
                "requirement-atomization-run/v3",
            ],
        )
        self.assertEqual(violations, [])

    def test_persists_multiple_atomization_revisions_for_same_review(self) -> None:
        initial = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(),
        )
        revision = 1
        revised = RequirementAtomizationRun.model_validate(
            {
                **initial.model_dump(mode="json"),
                "revision": revision,
                "atomization_run_id": stable_atomization_run_id(
                    lineage_sha256=initial.lineage.lineage_sha256,
                    revision=revision,
                    algorithm_version=initial.algorithm_version,
                    nodes=initial.nodes,
                    provider_runs=initial.provider_runs,
                ),
            }
        )

        self.assertEqual(self.repository.create_or_get_atomization(initial), initial)
        self.assertEqual(self.repository.create_or_get_atomization(revised), revised)
        self.assertEqual(
            self.repository.get_latest_atomization(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
            ),
            revised,
        )
        self.assertEqual(
            self.repository.get_latest_atomization(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
                atomization_revision=0,
            ),
            initial,
        )
        with sqlite3.connect(db.DB_PATH) as connection:
            rows = connection.execute(
                """
                SELECT atomization_revision
                FROM requirement_atomization_runs
                ORDER BY atomization_revision
                """
            ).fetchall()
        self.assertEqual(rows, [(0,), (1,)])

    def test_persists_revision_history_and_idempotent_non_active_draft(
        self,
    ) -> None:
        run = self.repository.create_or_get_atomization(
            build_requirement_atomization_run(
                self.session,
                coverage=_coverage(self.session),
                provider=_AtomizationProvider(),
            )
        )
        initial = self.repository.create_or_get_refinement(
            build_requirement_refinement_suggestion(
                run=run,
                atomic_node_id=str(run.atomic_nodes[0].atomic_node_id),
                provider=_RefinementProvider(),
            )
        )
        accepted = revise_requirement_refinement_suggestion(
            initial,
            expected_revision=0,
            operation=RequirementRefinementOperation.ACCEPT,
        )
        accepted = self.repository.update_refinement(
            suggestion=accepted,
            expected_revision=0,
        )
        preview = self.repository.create_or_get_publication_preview(
            build_requirement_publication_preview(
                session=self.session,
                run=run,
                coverage=_coverage(self.session),
                refinements=(accepted,),
                base_active_requirement_set_id=self.active["id"],
            )
        )

        first = self.repository.publish_draft(preview)
        second = self.repository.publish_draft(preview)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.requirement_set_id, second.requirement_set_id)
        self.assertFalse(first.is_active)
        document = db.get_sor_document(self.document["id"])
        self.assertEqual(
            document["active_requirement_set_id"],
            self.active["id"],
        )
        sets = db.list_requirement_sets(
            self.project["id"],
            self.document["id"],
        )
        self.assertEqual(len(sets), 2)
        published_set = next(
            item for item in sets if item["id"] == first.requirement_set_id
        )
        self.assertEqual(published_set["status"], "DRAFT")
        self.assertFalse(published_set["is_active"])
        rows = db.list_requirements(
            self.project["id"],
            self.document["id"],
            requirement_set_id=first.requirement_set_id,
        )
        self.assertEqual(len(rows), 2)
        requirement = next(item for item in rows if item["entry_kind"] == "REQUIREMENT")
        heading = next(item for item in rows if item["entry_kind"] == "HEADING")
        self.assertEqual(
            requirement["requirement_text"],
            accepted.suggested_statement,
        )
        self.assertEqual(
            requirement["parent_section_code"],
            heading["req_code"],
        )
        self.assertIn("requirement-enrichment-trace/v1", str(requirement))
        heading_trace = heading["decision_reference"]
        requirement_trace = requirement["decision_reference"]
        self.assertEqual(heading_trace["publication_order"], 0)
        self.assertEqual(requirement_trace["publication_order"], 1)
        self.assertEqual(
            [heading_trace["source_order"], requirement_trace["source_order"]],
            [item.source_order for item in preview.entries],
        )
        self.assertEqual(
            requirement_trace["parent_publication_entry_id"],
            heading_trace["publication_entry_id"],
        )
        with sqlite3.connect(db.DB_PATH) as connection:
            event_count = connection.execute(
                "SELECT COUNT(*) FROM requirement_refinement_events"
            ).fetchone()[0]
            publication_count = connection.execute(
                "SELECT COUNT(*) FROM requirement_enrichment_publications"
            ).fetchone()[0]
        self.assertEqual(event_count, 2)
        self.assertEqual(publication_count, 1)

    def test_publish_rejects_active_set_change_after_preview(self) -> None:
        run = self.repository.create_or_get_atomization(
            build_requirement_atomization_run(
                self.session,
                coverage=_coverage(self.session),
                provider=_AtomizationProvider(),
            )
        )
        preview = self.repository.create_or_get_publication_preview(
            build_requirement_publication_preview(
                session=self.session,
                run=run,
                coverage=_coverage(self.session),
                refinements=(),
                base_active_requirement_set_id=self.active["id"],
            )
        )
        replacement = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
        )
        db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            replacement["id"],
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "active Requirement Set changed",
        ):
            self.repository.publish_draft(preview)


def _atomization_checkpoint_request() -> RequirementAtomizationProviderRequest:
    source_text = "The component shall satisfy the source duty."
    return RequirementAtomizationProviderRequest(
        lineage_sha256="c" * 64,
        review_node_id="review-node-checkpoint",
        requirement_subtype="FUNCTIONAL_REQUIREMENT",
        source_segments=(
            RequirementAtomizationSourceSegment(
                segment_index=0,
                semantic_unit_id="semantic-unit-" + "d" * 32,
                owner_structure_node_id="structure-node-" + "e" * 32,
                source_start_offset=10,
                source_end_offset=10 + len(source_text),
                source_text=source_text,
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
