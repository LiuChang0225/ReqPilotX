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
from pathlib import Path

from app.application.cutover.sqlite_to_postgres import (
    CutoverRejected,
    build_cutover_plan,
)
from app.application.requirement_set_impact import compare_requirement_set_impact
from app.domain.requirement_set_impact import RequirementSetImpactInputNodeV1

_OLD_TREE_SHA256 = "1" * 64
_NEW_TREE_SHA256 = "2" * 64
_INTRINSIC_SHA256 = "a" * 64
_ANALYSIS_INPUT_SHA256 = "b" * 64
_ANALYSIS_INPUT_SCHEMA_VERSION = (
    "requirement-analysis-semantic-context/v1"
)
_MATRIX_STATE_SCHEMA_VERSION = "requirement-matrix-state/v1"
_UPDATED_AT = "2026-08-08T00:00:00+00:00"


def _matrix_state_sha256(
    requirement_set_id: str,
    node_states: list[dict[str, object]],
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "schema_version": _MATRIX_STATE_SCHEMA_VERSION,
                "project_id": "project-1",
                "document_id": "document-1",
                "requirement_set_id": requirement_set_id,
                "node_states": node_states,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


_OLD_MATRIX_STATE_SHA256 = _matrix_state_sha256("set-old", [])
_NEW_MATRIX_STATE_SHA256 = _matrix_state_sha256(
    "set-new",
    [
        {
            "node_id": "node-1",
            "disposition": "ANALYZE",
            "reason": "",
            "revision": 1,
        }
    ],
)


class RequirementSetImpactCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="reqpilotx-impact-cutover-"
        )
        self.root = Path(self.temporary.name)
        self.source_file = self.root / "sor.pdf"
        self.source_file.write_bytes(b"requirement-set-impact-cutover")
        self.database = self.root / "source.db"
        self.receipt = self._build_receipt()
        self._seed_database()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_plan_preserves_impact_receipts_and_matrix_states(self) -> None:
        plan = build_cutover_plan(self.database)

        self.assertEqual(plan.table_counts["requirement_set_impact_receipts"], 1)
        self.assertEqual(plan.table_counts["requirement_matrix_node_states"], 1)
        receipt_row = plan.rewritten_rows["requirement_set_impact_receipts"][0]
        self.assertEqual(receipt_row["receipt_id"], self.receipt.receipt_id)
        self.assertEqual(
            receipt_row["payload_json"],
            json.dumps(
                self.receipt.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self.assertEqual(
            plan.rewritten_rows["requirement_matrix_node_states"][0][
                "requirement_set_id"
            ],
            "set-new",
        )

        migration_0011 = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0011_requirement_matrix_node_states.sql"
        ).read_text(encoding="utf-8")
        migration_0017 = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0017_requirement_set_impact_receipts.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS requirement_matrix_node_states", migration_0011)
        self.assertIn("CREATE TABLE IF NOT EXISTS requirement_set_impact_receipts", migration_0017)
        for column in (
            "result_origin",
            "source_assessment_id",
            "impact_receipt_id",
            "analysis_input_schema_version",
            "analysis_input_sha256",
        ):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", migration_0017)

    def test_plan_rejects_noncanonical_impact_payload(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE requirement_set_impact_receipts
                SET payload_json = payload_json || ' '
                """
            )

        with self.assertRaisesRegex(CutoverRejected, "not canonical JSON"):
            build_cutover_plan(self.database)

    def test_plan_rejects_non_direct_impact_lineage(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE requirement_sets SET supersedes_id = '' WHERE id = 'set-new'"
            )

        with self.assertRaisesRegex(CutoverRejected, "directly supersede"):
            build_cutover_plan(self.database)

    def test_plan_rejects_assessment_receipt_for_another_target_set(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE assessments
                SET requirement_id = 'old-requirement'
                WHERE id = 'target-assessment'
                """
            )

        with self.assertRaisesRegex(CutoverRejected, "same target Requirement Set"):
            build_cutover_plan(self.database)

    def test_plan_rejects_illegal_native_carried_identity_combination(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE assessments
                SET result_origin = 'NATIVE'
                WHERE id = 'target-assessment'
                """
            )

        with self.assertRaisesRegex(CutoverRejected, "native assessment"):
            build_cutover_plan(self.database)

    def test_plan_rejects_stale_reference_with_human_decision(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE assessments
                SET reference_status = 'STALE_REFERENCE',
                    final_decision = 'ACCEPT'
                WHERE id = 'target-assessment'
                """
            )

        with self.assertRaisesRegex(CutoverRejected, "human decision"):
            build_cutover_plan(self.database)

    def test_plan_rejects_tampered_carried_assessment_result(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE assessments
                SET ai_reason = 'tampered carried result'
                WHERE id = 'target-assessment'
                """
            )

        with self.assertRaisesRegex(CutoverRejected, "result payload"):
            build_cutover_plan(self.database)

    def test_plan_rejects_wrong_well_formed_carried_analysis_hash(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE assessments
                SET analysis_input_sha256 = ?
                WHERE id = 'target-assessment'
                """,
                ("c" * 64,),
            )

        with self.assertRaisesRegex(CutoverRejected, "analysis input"):
            build_cutover_plan(self.database)

    def test_plan_rejects_wrong_carried_analysis_schema(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE assessments
                SET analysis_input_schema_version = 'requirement-analysis-input/v0'
                WHERE id = 'target-assessment'
                """
            )

        with self.assertRaisesRegex(CutoverRejected, "analysis input"):
            build_cutover_plan(self.database)

    def test_plan_rejects_tampered_carried_evidence(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE evidences
                SET excerpt = 'tampered evidence'
                WHERE requirement_id = 'new-requirement'
                """
            )

        with self.assertRaisesRegex(CutoverRejected, "evidence"):
            build_cutover_plan(self.database)

    def test_plan_rejects_matrix_state_outside_requirement_set_scope(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE requirement_matrix_node_states
                SET project_id = 'project-other'
                """
            )

        with self.assertRaisesRegex(CutoverRejected, "Matrix state scope"):
            build_cutover_plan(self.database)

    def test_plan_rejects_receipt_for_changed_matrix_state(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                UPDATE requirement_matrix_node_states
                SET reason = 'changed after receipt', revision = 2
                WHERE id = 'matrix-state-1'
                """
            )

        with self.assertRaisesRegex(CutoverRejected, "Matrix state hash"):
            build_cutover_plan(self.database)

    @staticmethod
    def _build_receipt():
        old_node = RequirementSetImpactInputNodeV1(
            node_id="node-1",
            requirement_id="old-requirement",
            has_current_result=True,
            intrinsic_sha256=_INTRINSIC_SHA256,
            analysis_input_sha256=_ANALYSIS_INPUT_SHA256,
            result_metadata={
                "assessment_id": "source-assessment",
                "assessment_updated_at": _UPDATED_AT,
                "result_origin": "NATIVE",
                "source_assessment_id": "",
                "has_manual_decision": False,
            },
        )
        new_node = RequirementSetImpactInputNodeV1(
            node_id="node-1",
            requirement_id="new-requirement",
            intrinsic_sha256=_INTRINSIC_SHA256,
            analysis_input_sha256=_ANALYSIS_INPUT_SHA256,
        )
        return compare_requirement_set_impact(
            project_id="project-1",
            document_id="document-1",
            old_requirement_set_id="set-old",
            new_requirement_set_id="set-new",
            old_tree_sha256=_OLD_TREE_SHA256,
            new_tree_sha256=_NEW_TREE_SHA256,
            matrix_state_schema_version=_MATRIX_STATE_SCHEMA_VERSION,
            old_matrix_state_sha256=_OLD_MATRIX_STATE_SHA256,
            new_matrix_state_sha256=_NEW_MATRIX_STATE_SHA256,
            old_nodes=[old_node],
            new_nodes=[new_node],
        )

    def _seed_database(self) -> None:
        payload_json = json.dumps(
            self.receipt.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        source_sha256 = hashlib.sha256(self.source_file.read_bytes()).hexdigest()
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY
                );
                CREATE TABLE sor_documents (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    source_size INTEGER NOT NULL
                );
                CREATE TABLE requirement_sets (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    sor_document_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    supersedes_id TEXT NOT NULL,
                    requirement_tree_sha256 TEXT NOT NULL
                );
                CREATE TABLE requirements (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    sor_document_id TEXT NOT NULL,
                    requirement_set_id TEXT NOT NULL
                );
                CREATE TABLE assessments (
                    id TEXT PRIMARY KEY,
                    requirement_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    retrieval_backend TEXT NOT NULL,
                    ai_decision TEXT NOT NULL,
                    compliance_status TEXT NOT NULL,
                    ai_reason TEXT NOT NULL,
                    ai_confidence REAL NOT NULL,
                    assessment_mode TEXT NOT NULL,
                    analysis_label TEXT NOT NULL,
                    analysis_basis TEXT NOT NULL,
                    normalized_requirement TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    clarification_question TEXT NOT NULL,
                    final_decision TEXT,
                    engineer_reason TEXT,
                    model_name TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    evidence_findings_json TEXT NOT NULL,
                    structured_information_gap_json TEXT NOT NULL,
                    sub_judgments_json TEXT NOT NULL,
                    reference_status TEXT NOT NULL,
                    source_requirement_id TEXT NOT NULL,
                    result_origin TEXT NOT NULL,
                    source_assessment_id TEXT NOT NULL,
                    impact_receipt_id TEXT NOT NULL,
                    analysis_input_schema_version TEXT NOT NULL,
                    analysis_input_sha256 TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE evidences (
                    id TEXT PRIMARY KEY,
                    requirement_id TEXT NOT NULL,
                    source_doc_id TEXT NOT NULL,
                    source_doc_name TEXT NOT NULL,
                    source_doc_version TEXT NOT NULL,
                    retrieval_evidence_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    evidence_contract_version TEXT NOT NULL,
                    content_kind TEXT NOT NULL,
                    evidence_metadata_json TEXT NOT NULL,
                    source_location TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    relevance REAL NOT NULL
                );
                CREATE TABLE requirement_matrix_node_states (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    sor_document_id TEXT NOT NULL,
                    requirement_set_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE requirement_set_impact_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    sor_document_id TEXT NOT NULL,
                    old_requirement_set_id TEXT NOT NULL,
                    new_requirement_set_id TEXT NOT NULL,
                    old_tree_sha256 TEXT NOT NULL,
                    new_tree_sha256 TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute("INSERT INTO projects VALUES ('project-1')")
            connection.execute(
                "INSERT INTO sor_documents VALUES (?, 'project-1', ?, ?, ?)",
                (
                    "document-1",
                    str(self.source_file),
                    source_sha256,
                    self.source_file.stat().st_size,
                ),
            )
            connection.executemany(
                "INSERT INTO requirement_sets VALUES (?, 'project-1', 'document-1', ?, ?, ?)",
                (
                    ("set-old", 1, "", _OLD_TREE_SHA256),
                    ("set-new", 2, "set-old", _NEW_TREE_SHA256),
                ),
            )
            connection.executemany(
                "INSERT INTO requirements VALUES (?, 'project-1', 'document-1', ?)",
                (
                    ("old-requirement", "set-old"),
                    ("new-requirement", "set-new"),
                ),
            )
            connection.executemany(
                """
                INSERT INTO assessments (
                    id, requirement_id, profile_id, provider, retrieval_backend,
                    ai_decision, compliance_status, ai_reason, ai_confidence,
                    assessment_mode, analysis_label, analysis_basis,
                    normalized_requirement, risk_level, clarification_question,
                    final_decision, engineer_reason, model_name, prompt_version,
                    evidence_findings_json, structured_information_gap_json,
                    sub_judgments_json, reference_status, source_requirement_id,
                    result_origin, source_assessment_id, impact_receipt_id,
                    analysis_input_schema_version, analysis_input_sha256,
                    updated_at
                ) VALUES (
                    ?, ?, 'profile-1', 'provider-1', 'retrieval-1',
                    'PASS', 'PASS', 'reason', 0.91,
                    'llm', 'REQUIREMENT', 'basis',
                    'normalized requirement', 'LOW', '',
                    NULL, NULL, 'model-1', 'prompt-v1',
                    '[]', '{}', '[]', ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    (
                        "source-assessment",
                        "old-requirement",
                        "CURRENT",
                        "",
                        "NATIVE",
                        "",
                        "",
                        "",
                        "",
                        _UPDATED_AT,
                    ),
                    (
                        "target-assessment",
                        "new-requirement",
                        "CURRENT",
                        "old-requirement",
                        "CARRIED",
                        "source-assessment",
                        self.receipt.receipt_id,
                        _ANALYSIS_INPUT_SCHEMA_VERSION,
                        _ANALYSIS_INPUT_SHA256,
                        _UPDATED_AT,
                    ),
                ),
            )
            connection.executemany(
                """
                INSERT INTO evidences (
                    id, requirement_id, source_doc_id, source_doc_name,
                    source_doc_version, retrieval_evidence_id, content_hash,
                    evidence_contract_version, content_kind,
                    evidence_metadata_json, source_location, excerpt, relevance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        "source-evidence",
                        "old-requirement",
                        "knowledge-1",
                        "standard.pdf",
                        "v1",
                        "retrieval-1",
                        "d" * 64,
                        "evidence-contract/v1",
                        "TEXT",
                        '{"rank":1}',
                        "p.12",
                        "verified evidence",
                        0.93,
                    ),
                    (
                        "target-evidence",
                        "new-requirement",
                        "knowledge-1",
                        "standard.pdf",
                        "v1",
                        "retrieval-1",
                        "d" * 64,
                        "evidence-contract/v1",
                        "TEXT",
                        '{"rank":1}',
                        "p.12",
                        "verified evidence",
                        0.93,
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO requirement_matrix_node_states VALUES (
                    'matrix-state-1', 'project-1', 'document-1', 'set-new',
                    'node-1', 'ANALYZE', '', 1, ?, ?
                )
                """,
                (_UPDATED_AT, _UPDATED_AT),
            )
            connection.execute(
                """
                INSERT INTO requirement_set_impact_receipts VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    self.receipt.receipt_id,
                    self.receipt.schema_version,
                    self.receipt.algorithm_version,
                    self.receipt.project_id,
                    self.receipt.document_id,
                    self.receipt.old_requirement_set_id,
                    self.receipt.new_requirement_set_id,
                    self.receipt.old_tree_sha256,
                    self.receipt.new_tree_sha256,
                    self.receipt.payload_sha256,
                    payload_json,
                    _UPDATED_AT,
                ),
            )


if __name__ == "__main__":
    unittest.main()
