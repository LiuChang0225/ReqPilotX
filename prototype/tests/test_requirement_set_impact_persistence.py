from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app import db
from app.adapters.persistence.requirement_matrix_states import (
    REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION,
    requirement_matrix_state_sha256,
)
from app.adapters.persistence.requirement_set_impacts import (
    RequirementSetImpactConflictError,
    RequirementSetImpactPersistenceError,
    RequirementSetImpactStateChangedError,
    apply_requirement_set_impact_receipt,
    get_requirement_set_impact_receipt,
    list_requirement_set_impact_receipts,
    save_requirement_set_impact_receipt,
)
from app.application.requirement_set_impact import compare_requirement_set_impact
from app.domain.requirement_set_impact import RequirementSetImpactInputNodeV1

_OLD_TREE = "1" * 64
_NEW_TREE = "2" * 64
_INPUT_SCHEMA = "requirement-assessment-context/v1"


def _input_node(
    node_id: str,
    requirement_id: str,
    *,
    has_result: bool = False,
    intrinsic: str = "a" * 64,
    analysis_input: str = "b" * 64,
    assessment_id: str = "",
    result_metadata: dict | None = None,
) -> RequirementSetImpactInputNodeV1:
    return RequirementSetImpactInputNodeV1(
        node_id=node_id,
        requirement_id=requirement_id,
        has_current_result=has_result,
        intrinsic_sha256=intrinsic,
        analysis_input_sha256=analysis_input,
        result_metadata=(
            result_metadata
            if result_metadata is not None
            else (
                {"assessment_id": assessment_id}
                if has_result and assessment_id
                else {}
            )
        ),
    )


class RequirementSetImpactPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="reqpilotx-impact-persistence-"
        )
        self.root = Path(self.temporary.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        self.stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        db.init_db()
        self._seed_lineage()

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def test_apply_is_atomic_idempotent_and_preserves_only_valid_results(
        self,
    ) -> None:
        receipt = self._receipt()
        before_old = self._old_state()

        stored, created = save_requirement_set_impact_receipt(receipt)
        self.assertTrue(created)
        self.assertEqual(stored, receipt)
        self.assertEqual(self._assessment("new-retained")["reference_status"], "STALE_REFERENCE")

        result = apply_requirement_set_impact_receipt(
            receipt,
            analysis_input_schema_version=_INPUT_SCHEMA,
        )
        self.assertFalse(result.created)
        self.assertTrue(result.applied)
        self.assertEqual(result.retained_count, 1)
        self.assertEqual(result.stale_count, 1)
        self.assertEqual(result.cleared_count, 1)

        retained = self._assessment("new-retained")
        self.assertEqual(retained["reference_status"], "CURRENT")
        self.assertEqual(retained["result_origin"], "CARRIED")
        self.assertEqual(retained["source_assessment_id"], "assessment-retained")
        self.assertEqual(retained["source_requirement_id"], "old-retained")
        self.assertEqual(retained["impact_receipt_id"], receipt.receipt_id)
        self.assertEqual(retained["analysis_input_schema_version"], _INPUT_SCHEMA)
        self.assertEqual(retained["analysis_input_sha256"], "b" * 64)
        self.assertEqual(retained["final_decision"], "ACCEPT")
        self.assertEqual(retained["engineer_reason"], "verified")

        stale = self._assessment("new-stale")
        self.assertEqual(stale["reference_status"], "STALE_REFERENCE")
        self.assertEqual(stale["result_origin"], "CARRIED")
        self.assertEqual(stale["source_assessment_id"], "assessment-stale")
        self.assertEqual(stale["analysis_input_sha256"], "c" * 64)
        self.assertIsNone(stale["final_decision"])
        self.assertIsNone(stale["engineer_reason"])

        self.assertIsNone(self._assessment("new-no-result"))
        self.assertEqual(self._evidence_count("new-no-result"), 0)
        self.assertEqual(self._evidence_signature("new-retained"), self._evidence_signature("old-retained"))
        self.assertEqual(self._evidence_signature("new-stale"), self._evidence_signature("old-stale"))
        self.assertEqual(self._run_count("new-retained"), 0)
        self.assertEqual(self._run_count("new-stale"), 0)
        self.assertEqual(self._old_state(), before_old)

        evidence_ids = self._evidence_ids("new-retained")
        repeated = apply_requirement_set_impact_receipt(
            receipt,
            analysis_input_schema_version=_INPUT_SCHEMA,
        )
        self.assertFalse(repeated.created)
        self.assertFalse(repeated.applied)
        self.assertEqual(self._evidence_ids("new-retained"), evidence_ids)
        self.assertEqual(get_requirement_set_impact_receipt(receipt.receipt_id), receipt)
        self.assertEqual(
            list_requirement_set_impact_receipts(
                project_id="project-1",
                document_id="document-1",
                new_requirement_set_id="set-new",
            ),
            (receipt,),
        )

    def test_new_receipt_for_same_target_can_replace_retained_with_stale(self) -> None:
        first = self._receipt()
        apply_requirement_set_impact_receipt(
            first,
            analysis_input_schema_version=_INPUT_SCHEMA,
        )
        with db.get_conn() as connection:
            connection.execute(
                "UPDATE requirement_sets SET requirement_tree_sha256 = ? WHERE id = 'set-new'",
                ("3" * 64,),
            )
        second = self._receipt(
            new_tree="3" * 64,
            retained_analysis_input="d" * 64,
        )

        result = apply_requirement_set_impact_receipt(
            second,
            analysis_input_schema_version=_INPUT_SCHEMA,
        )

        self.assertTrue(result.created)
        self.assertTrue(result.applied)
        self.assertEqual(self._assessment("new-retained")["reference_status"], "STALE_REFERENCE")
        self.assertIsNone(self._assessment("new-retained")["final_decision"])
        receipts = list_requirement_set_impact_receipts(
            project_id="project-1",
            document_id="document-1",
            new_requirement_set_id="set-new",
        )
        self.assertEqual(
            {item.receipt_id for item in receipts},
            {first.receipt_id, second.receipt_id},
        )

    def test_older_matrix_bound_receipt_cannot_overwrite_newer_projection(self) -> None:
        # H1 and H2 can have identical visible labels after undo/redo; revision
        # and reason remain part of the canonical ordering boundary.
        self._matrix_state_sha256("set-new")
        with db.get_conn() as connection:
            connection.execute(
                """
                INSERT INTO requirement_matrix_node_states (
                    id, project_id, sor_document_id, requirement_set_id,
                    node_id, disposition, reason, revision,
                    created_at, updated_at
                ) VALUES (
                    'matrix-h1', 'project-1', 'document-1', 'set-new',
                    'control-node', 'ANALYZE', 'H1', 1,
                    '2026-08-08T00:00:00+00:00',
                    '2026-08-08T00:00:00+00:00'
                )
                """
            )
        receipt_h1 = self._receipt()
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_matrix_node_states
                SET disposition = 'RETAIN', reason = 'H2', revision = 2,
                    updated_at = '2026-08-08T00:02:00+00:00'
                WHERE id = 'matrix-h1'
                """
            )
        receipt_h2 = self._receipt(retained_analysis_input="c" * 64)

        apply_requirement_set_impact_receipt(
            receipt_h2,
            analysis_input_schema_version=_INPUT_SCHEMA,
        )
        self.assertEqual(
            self._assessment("new-retained")["reference_status"],
            "STALE_REFERENCE",
        )
        with self.assertRaises(RequirementSetImpactStateChangedError):
            apply_requirement_set_impact_receipt(
                receipt_h1,
                analysis_input_schema_version=_INPUT_SCHEMA,
            )
        self.assertEqual(
            self._assessment("new-retained")["reference_status"],
            "STALE_REFERENCE",
        )

    def test_lineage_tree_and_node_binding_conflicts_are_rejected(self) -> None:
        wrong_hash = self._receipt(new_tree="f" * 64)
        with self.assertRaisesRegex(
            RequirementSetImpactConflictError,
            "target tree hash",
        ):
            save_requirement_set_impact_receipt(wrong_hash)
        self.assertIsNone(get_requirement_set_impact_receipt(wrong_hash.receipt_id))

        invalid_target = compare_requirement_set_impact(
            project_id="project-1",
            document_id="document-1",
            old_requirement_set_id="set-old",
            new_requirement_set_id="set-unrelated",
            old_tree_sha256=_OLD_TREE,
            new_tree_sha256="4" * 64,
            matrix_state_schema_version=(
                REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION
            ),
            old_matrix_state_sha256=self._matrix_state_sha256("set-old"),
            new_matrix_state_sha256=self._matrix_state_sha256("set-unrelated"),
            old_nodes=[],
            new_nodes=[],
        )
        with self.assertRaisesRegex(
            RequirementSetImpactPersistenceError,
            "directly supersede",
        ):
            save_requirement_set_impact_receipt(invalid_target)

        missing_source = compare_requirement_set_impact(
            project_id="project-1",
            document_id="document-1",
            old_requirement_set_id="set-old",
            new_requirement_set_id="set-new",
            old_tree_sha256=_OLD_TREE,
            new_tree_sha256=_NEW_TREE,
            matrix_state_schema_version=(
                REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION
            ),
            old_matrix_state_sha256=self._matrix_state_sha256("set-old"),
            new_matrix_state_sha256=self._matrix_state_sha256("set-new"),
            old_nodes=[
                _input_node(
                    "missing",
                    "old-missing",
                    has_result=True,
                    assessment_id="assessment-missing",
                )
            ],
            new_nodes=[_input_node("missing", "new-added")],
        )
        with self.assertRaisesRegex(
            RequirementSetImpactPersistenceError,
            "source requirement binding",
        ):
            apply_requirement_set_impact_receipt(
                missing_source,
                analysis_input_schema_version=_INPUT_SCHEMA,
            )
        self.assertIsNone(get_requirement_set_impact_receipt(missing_source.receipt_id))

    def test_native_target_result_and_tampered_stored_payload_fail_closed(self) -> None:
        receipt = self._receipt()
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE assessments
                SET reference_status = 'CURRENT', result_origin = 'NATIVE'
                WHERE requirement_id = 'new-retained'
                """
            )
        with self.assertRaisesRegex(
            RequirementSetImpactConflictError,
            "native target result",
        ):
            apply_requirement_set_impact_receipt(
                receipt,
                analysis_input_schema_version=_INPUT_SCHEMA,
            )
        self.assertIsNone(get_requirement_set_impact_receipt(receipt.receipt_id))

        empty = compare_requirement_set_impact(
            project_id="project-1",
            document_id="document-1",
            old_requirement_set_id="set-old",
            new_requirement_set_id="set-new",
            old_tree_sha256=_OLD_TREE,
            new_tree_sha256=_NEW_TREE,
            matrix_state_schema_version=(
                REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION
            ),
            old_matrix_state_sha256=self._matrix_state_sha256("set-old"),
            new_matrix_state_sha256=self._matrix_state_sha256("set-new"),
            old_nodes=[],
            new_nodes=[],
        )
        save_requirement_set_impact_receipt(empty)
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_set_impact_receipts
                SET payload_json = payload_json || ' '
                WHERE receipt_id = ?
                """,
                (empty.receipt_id,),
            )
        with self.assertRaisesRegex(
            RequirementSetImpactPersistenceError,
            "not canonical",
        ):
            get_requirement_set_impact_receipt(empty.receipt_id)

    def test_source_assessment_metadata_is_a_compare_and_swap_boundary(self) -> None:
        receipt = compare_requirement_set_impact(
            project_id="project-1",
            document_id="document-1",
            old_requirement_set_id="set-old",
            new_requirement_set_id="set-new",
            old_tree_sha256=_OLD_TREE,
            new_tree_sha256=_NEW_TREE,
            matrix_state_schema_version=(
                REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION
            ),
            old_matrix_state_sha256=self._matrix_state_sha256("set-old"),
            new_matrix_state_sha256=self._matrix_state_sha256("set-new"),
            old_nodes=[
                _input_node(
                    "retained",
                    "old-retained",
                    has_result=True,
                    result_metadata={
                        "assessment_id": "assessment-retained",
                        "assessment_updated_at": "2026-08-08T00:00:00+00:00",
                        "result_origin": "NATIVE",
                        "source_assessment_id": "",
                        "has_manual_decision": True,
                    },
                )
            ],
            new_nodes=[_input_node("retained", "new-retained")],
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE assessments
                SET final_decision = 'REJECT',
                    updated_at = '2026-08-08T00:05:00+00:00'
                WHERE id = 'assessment-retained'
                """
            )

        with self.assertRaisesRegex(
            RequirementSetImpactConflictError,
            "metadata changed",
        ):
            apply_requirement_set_impact_receipt(
                receipt,
                analysis_input_schema_version=_INPUT_SCHEMA,
            )
        self.assertIsNone(get_requirement_set_impact_receipt(receipt.receipt_id))

    def _seed_lineage(self) -> None:
        with db.get_conn() as connection:
            connection.execute(
                """
                INSERT INTO projects (id, name, customer_name, created_at)
                VALUES ('project-1', 'Impact', 'Customer', '2026-08-08T00:00:00+00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO sor_documents (
                    id, project_id, file_name, file_type, file_path,
                    extracted_text, created_at
                ) VALUES (
                    'document-1', 'project-1', 'sor.pdf', 'pdf', 'sor.pdf',
                    'source', '2026-08-08T00:00:00+00:00'
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO requirement_sets (
                    id, project_id, sor_document_id, version, status, is_active,
                    supersedes_id, requirement_tree_sha256, created_at
                ) VALUES (?, 'project-1', 'document-1', ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        "set-old", 1, "LOCKED", 1, "", _OLD_TREE,
                        "2026-08-08T00:00:00+00:00",
                    ),
                    (
                        "set-new", 2, "DRAFT", 0, "set-old", _NEW_TREE,
                        "2026-08-08T00:01:00+00:00",
                    ),
                    (
                        "set-unrelated", 3, "DRAFT", 0, "", "4" * 64,
                        "2026-08-08T00:02:00+00:00",
                    ),
                ),
            )
            for requirement_id, requirement_set_id in (
                ("old-retained", "set-old"),
                ("old-stale", "set-old"),
                ("old-no-result", "set-old"),
                ("old-removed", "set-old"),
                ("new-retained", "set-new"),
                ("new-stale", "set-new"),
                ("new-no-result", "set-new"),
                ("new-added", "set-new"),
            ):
                self._insert_requirement(connection, requirement_id, requirement_set_id)

            self._insert_assessment(
                connection,
                "assessment-retained",
                "old-retained",
                final_decision="ACCEPT",
                engineer_reason="verified",
            )
            self._insert_assessment(
                connection,
                "assessment-stale",
                "old-stale",
                final_decision="REJECT",
                engineer_reason="old context",
            )
            self._insert_assessment(
                connection,
                "assessment-removed",
                "old-removed",
            )
            self._insert_assessment(
                connection,
                "assessment-old-no-stale",
                "old-no-result",
                reference_status="STALE_REFERENCE",
            )
            for suffix in ("retained", "stale", "no-result"):
                self._insert_assessment(
                    connection,
                    f"clone-{suffix}",
                    f"new-{suffix}",
                    reference_status="STALE_REFERENCE",
                )
            self._insert_evidence(connection, "evidence-retained", "old-retained", "retained")
            self._insert_evidence(connection, "evidence-stale", "old-stale", "stale")
            self._insert_evidence(connection, "clone-evidence", "new-retained", "clone")
            self._insert_evidence(connection, "clone-no-result", "new-no-result", "clone")
            for requirement_id in ("old-retained", "old-stale"):
                connection.execute(
                    """
                    INSERT INTO assessment_runs (
                        id, requirement_id, mode, retrieval_backend, model_name,
                        prompt_version, input_payload_json, output_payload_json,
                        validation_status, created_at
                    ) VALUES (?, ?, 'ai', 'local', 'model', 'prompt-v1', '{}', '{}',
                              'VALID', '2026-08-08T00:00:00+00:00')
                    """,
                    (f"run-{requirement_id}", requirement_id),
                )

    def _receipt(
        self,
        *,
        new_tree: str = _NEW_TREE,
        retained_analysis_input: str = "b" * 64,
    ):
        return compare_requirement_set_impact(
            project_id="project-1",
            document_id="document-1",
            old_requirement_set_id="set-old",
            new_requirement_set_id="set-new",
            old_tree_sha256=_OLD_TREE,
            new_tree_sha256=new_tree,
            matrix_state_schema_version=(
                REQUIREMENT_MATRIX_STATE_SCHEMA_VERSION
            ),
            old_matrix_state_sha256=self._matrix_state_sha256("set-old"),
            new_matrix_state_sha256=self._matrix_state_sha256("set-new"),
            old_nodes=(
                _input_node(
                    "added-later-retained",
                    "old-retained",
                    has_result=True,
                    assessment_id="assessment-retained",
                ),
                _input_node(
                    "dependency-stale",
                    "old-stale",
                    has_result=True,
                    assessment_id="assessment-stale",
                ),
                _input_node("no-result", "old-no-result"),
                _input_node(
                    "removed",
                    "old-removed",
                    has_result=True,
                    assessment_id="assessment-removed",
                ),
            ),
            new_nodes=(
                _input_node(
                    "added-later-retained",
                    "new-retained",
                    analysis_input=retained_analysis_input,
                ),
                _input_node(
                    "dependency-stale",
                    "new-stale",
                    analysis_input="c" * 64,
                ),
                _input_node("no-result", "new-no-result"),
                _input_node("new", "new-added"),
            ),
        )

    @staticmethod
    def _matrix_state_sha256(requirement_set_id: str) -> str:
        return requirement_matrix_state_sha256(
            project_id="project-1",
            document_id="document-1",
            requirement_set_id=requirement_set_id,
        )

    @staticmethod
    def _insert_requirement(connection, requirement_id: str, set_id: str) -> None:
        connection.execute(
            """
            INSERT INTO requirements (
                id, project_id, sor_document_id, requirement_set_id, req_code,
                requirement_text, requirement_type, priority, status,
                created_at, updated_at
            ) VALUES (?, 'project-1', 'document-1', ?, ?, ?, 'TECHNICAL', 'P1',
                      'OPEN', '2026-08-08T00:00:00+00:00',
                      '2026-08-08T00:00:00+00:00')
            """,
            (requirement_id, set_id, requirement_id, f"Text for {requirement_id}"),
        )

    @staticmethod
    def _insert_assessment(
        connection,
        assessment_id: str,
        requirement_id: str,
        *,
        final_decision: str | None = None,
        engineer_reason: str | None = None,
        reference_status: str = "CURRENT",
    ) -> None:
        connection.execute(
            """
            INSERT INTO assessments (
                id, requirement_id, ai_decision, ai_reason, ai_confidence,
                final_decision, engineer_reason, model_name, prompt_version,
                reference_status, result_origin, updated_at
            ) VALUES (?, ?, 'PASS', 'reason', 0.9, ?, ?, 'model', 'prompt-v1',
                      ?, 'NATIVE', '2026-08-08T00:00:00+00:00')
            """,
            (
                assessment_id,
                requirement_id,
                final_decision,
                engineer_reason,
                reference_status,
            ),
        )

    @staticmethod
    def _insert_evidence(connection, evidence_id: str, requirement_id: str, label: str) -> None:
        connection.execute(
            """
            INSERT INTO evidences (
                id, requirement_id, source_doc_id, source_doc_name,
                source_location, excerpt, relevance
            ) VALUES (?, ?, ?, ?, ?, ?, 0.8)
            """,
            (
                evidence_id,
                requirement_id,
                f"source-{label}",
                f"{label}.pdf",
                "p.1",
                f"Evidence {label}",
            ),
        )

    @staticmethod
    def _assessment(requirement_id: str):
        with db.get_conn() as connection:
            row = connection.execute(
                "SELECT * FROM assessments WHERE requirement_id = ?",
                (requirement_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _evidence_count(requirement_id: str) -> int:
        with db.get_conn() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM evidences WHERE requirement_id = ?",
                    (requirement_id,),
                ).fetchone()[0]
            )

    @staticmethod
    def _evidence_ids(requirement_id: str) -> tuple[str, ...]:
        with db.get_conn() as connection:
            rows = connection.execute(
                "SELECT id FROM evidences WHERE requirement_id = ? ORDER BY id",
                (requirement_id,),
            ).fetchall()
        return tuple(str(row["id"]) for row in rows)

    @staticmethod
    def _evidence_signature(requirement_id: str) -> tuple[tuple, ...]:
        with db.get_conn() as connection:
            rows = connection.execute(
                """
                SELECT source_doc_id, source_doc_name, source_location, excerpt, relevance
                FROM evidences WHERE requirement_id = ?
                ORDER BY source_doc_id, excerpt
                """,
                (requirement_id,),
            ).fetchall()
        return tuple(tuple(row) for row in rows)

    @staticmethod
    def _run_count(requirement_id: str) -> int:
        with db.get_conn() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM assessment_runs WHERE requirement_id = ?",
                    (requirement_id,),
                ).fetchone()[0]
            )

    @staticmethod
    def _old_state() -> tuple:
        with db.get_conn() as connection:
            assessments = connection.execute(
                """
                SELECT * FROM assessments
                WHERE requirement_id LIKE 'old-%'
                ORDER BY requirement_id
                """
            ).fetchall()
            evidences = connection.execute(
                """
                SELECT * FROM evidences
                WHERE requirement_id LIKE 'old-%'
                ORDER BY requirement_id, id
                """
            ).fetchall()
            runs = connection.execute(
                """
                SELECT * FROM assessment_runs
                WHERE requirement_id LIKE 'old-%'
                ORDER BY requirement_id, id
                """
            ).fetchall()
        return (
            tuple(tuple(row) for row in assessments),
            tuple(tuple(row) for row in evidences),
            tuple(tuple(row) for row in runs),
        )


if __name__ == "__main__":
    unittest.main()
