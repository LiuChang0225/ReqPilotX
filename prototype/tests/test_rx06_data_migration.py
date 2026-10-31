"""RX06 synthetic export, migration, and backup/restore contract tests."""

from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
from reqpilotx_server.tools import rx06_backup as backup
from reqpilotx_server.tools import rx06_migration as migration
from reqpilotx_server.tools.rx06_common import Rx06Error, sha256_bytes

NOW = "2026-10-31T12:00:00+00:00"


def _workbook_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("[Content_Types].xml", "<Types/>")
    return stream.getvalue()


def _manual_artifact(source_sha256: str) -> dict[str, Any]:
    body = {
        "schema_version": "source-item-manual-decisions/v1",
        "revision": 1,
        "predecessor_sha256": "",
        "source_sha256": source_sha256,
        "baseline_sha256": "b" * 64,
        "context_sha256": "c" * 64,
        "records": {},
        "history": [],
        "operation": "SET",
        "human_acceptance": "NOT_CLAIMED",
    }
    return {**body, "artifact_sha256": canonical_json_sha256(body)}


def _delivery_artifact(source_sha256: str, export_key: str, export_sha256: str) -> dict[str, Any]:
    snapshot = {"source_item_baseline": {"source_sha256": source_sha256}}
    snapshot["delivery_sha256"] = canonical_json_sha256(snapshot)
    artifact = {
        "schema_version": "source-item-delivery-version/v1",
        "revision": 1,
        "mode": "DRAFT",
        "created_at": NOW,
        "snapshot": snapshot,
        "export_key": export_key,
        "export_sha256": export_sha256,
        "filename": "synthetic-delivery.xlsx",
    }
    artifact["artifact_sha256"] = canonical_json_sha256(artifact)
    return artifact


class SyntheticSource:
    def __init__(self, rows: dict[str, list[dict[str, Any]]], migrations: list[dict[str, str]] | None = None) -> None:
        self.rows = rows
        self._migrations = migrations

    def applied_migrations(self) -> list[dict[str, Any]]:
        return self._migrations if self._migrations is not None else migration._packaged_migrations()

    def base_tables(self) -> set[str]:
        return set(self.rows) | {"schema_migrations"}

    def fetch_rows(self, table: str, filter_sql: str | None, params: tuple[Any, ...], order_by: tuple[str, ...]) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.rows.get(table, [])]
        if table == "projects":
            selected = set(params[0])
            rows = [row for row in rows if row.get("id") in selected]
        elif filter_sql and filter_sql.startswith("project_id = ANY(%s)"):
            selected = set(params[0])
            rows = [row for row in rows if row.get("project_id") in selected]
        return sorted(rows, key=lambda row: tuple(str(row.get(column)) for column in order_by))

    def fetch_by_keys(self, table: str, key_column: str, keys: list[str]) -> list[dict[str, Any]]:
        return sorted(
            [dict(row) for row in self.rows.get(table, []) if row.get(key_column) in set(keys)],
            key=lambda row: str(row.get(key_column)),
        )


class Rx06MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rx06-synthetic-", ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def _fixture(self) -> tuple[SyntheticSource, Path, dict[str, Any]]:
        object_root = self.root / "source-objects"
        object_root.mkdir()
        source_bytes = b"%PDF-1.4 synthetic source\n"
        source_sha = sha256_bytes(source_bytes)
        knowledge_bytes = b"synthetic knowledge"
        knowledge_sha = sha256_bytes(knowledge_bytes)
        workbook = _workbook_bytes()
        workbook_sha = sha256_bytes(workbook)
        document_key = f"sor-documents/p1/{source_sha}/spec.pdf"
        knowledge_key = f"knowledge/p1/{knowledge_sha}/reference.txt"
        export_key = "exports/p1/matrix.xlsx"
        delivery_key = f"source-item-deliveries/p1/d1/{workbook_sha}.xlsx"
        for key, payload in (
            (document_key, source_bytes),
            (knowledge_key, knowledge_bytes),
            (export_key, workbook),
            (delivery_key, workbook),
        ):
            target = object_root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        manual = _manual_artifact(source_sha)
        delivery = _delivery_artifact(source_sha, delivery_key, workbook_sha)
        rows: dict[str, list[dict[str, Any]]] = {
            "projects": [
                {"id": "p1", "name": "Synthetic project", "customer_name": "Synthetic customer", "knowledge_base_version": 1, "created_at": NOW},
                {"id": "p2", "name": "Other project", "customer_name": "Other customer", "knowledge_base_version": 0, "created_at": NOW},
            ],
            "stored_objects": [
                {"storage_key": document_key, "sha256": source_sha, "size_bytes": len(source_bytes), "media_type": "application/pdf", "object_kind": "source-document", "created_at": NOW},
                {"storage_key": knowledge_key, "sha256": knowledge_sha, "size_bytes": len(knowledge_bytes), "media_type": "text/plain", "object_kind": "knowledge-document", "created_at": NOW},
                {"storage_key": export_key, "sha256": workbook_sha, "size_bytes": len(workbook), "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "object_kind": "export", "created_at": NOW},
            ],
            "sor_documents": [
                {"id": "d1", "project_id": "p1", "file_name": "spec.pdf", "file_type": "PDF", "storage_key": document_key, "extracted_text": "Synthetic requirement text", "workflow_stage": "UPLOADED", "requirement_set_version": 1, "extraction_locked_at": NOW, "ai_status": "COMPLETED", "ai_assessed_at": NOW, "ai_requirement_set_version": 1, "ai_knowledge_base_version": 1, "preview_status": "READY", "preview_generated_at": NOW, "preview_file_mtime": "", "preview_error": "", "preview_generation_id": "pg1", "active_requirement_set_id": "rs1", "source_sha256": source_sha, "source_size": len(source_bytes), "declared_extension": ".pdf", "detected_format": "pdf", "preflight_schema_version": "v1", "created_at": NOW, "updated_at": NOW},
            ],
            "knowledge_documents": [
                {"id": "k1", "project_id": "p1", "file_name": "reference.txt", "file_type": "TXT", "storage_key": knowledge_key, "extracted_text": "Knowledge", "source_sha256": knowledge_sha, "source_size": len(knowledge_bytes), "declared_extension": ".txt", "detected_format": "txt", "preflight_schema_version": "v1", "doc_type": "REFERENCE", "doc_version": "1", "status": "ACTIVE", "effective_from": "", "effective_to": "", "retrieval_sync_status": "PENDING", "retrieval_sync_error": "", "retrieval_synced_at": "", "created_at": NOW},
            ],
            "requirement_sets": [
                {"id": "rs1", "project_id": "p1", "sor_document_id": "d1", "version": 1, "status": "LOCKED", "is_active": 1, "source_hash": "a" * 64, "preview_generation_id": "pg1", "supersedes_id": "", "created_at": NOW, "activated_at": NOW, "locked_at": NOW, "lock_waiver_reason": "", "preview_status_at_lock": "READY"},
            ],
            "requirements": [
                {"id": "r1", "project_id": "p1", "sor_document_id": "d1", "requirement_set_id": "rs1", "req_code": "REQ-001", "source_page": 1, "source_anchor": "a1", "requirement_text": "The system shall work.", "requirement_type": "FUNCTIONAL", "priority": "HIGH", "status": "ACTIVE", "entry_kind": "REQUIREMENT", "text_relation": "distinct", "is_deduped": 0, "heading_text": "", "raw_text": "", "display_text": "", "merged_from_req_codes": "[]", "merged_from_entry_ids": "[]", "dedupe_version": "", "parent_section_code": "", "parent_section_title": "", "parent_section_label": "", "table_refs": "[]", "content_blocks": "[]", "has_table_asset": 0, "table_render_mode": "", "preview_generation_id": "pg1", "prior_requirement_id": "", "decision_reference_json": "{}", "created_at": NOW, "updated_at": NOW},
            ],
            "assessments": [
                {"id": "a1", "requirement_id": "r1", "profile_id": "profile", "provider": "source", "retrieval_backend": "source", "ai_decision": "COMPLIANT", "compliance_status": "COMPLIANT", "ai_reason": "Synthetic reason", "ai_confidence": 0.9, "assessment_mode": "heuristic", "analysis_label": "REQUIREMENT", "analysis_basis": "", "normalized_requirement": "", "risk_level": "LOW", "clarification_question": "", "final_decision": "COMPLIANT", "engineer_reason": "Source author decision", "model_name": "model", "prompt_version": "v1", "reference_status": "CURRENT", "source_requirement_id": "", "updated_at": NOW},
            ],
            "evidences": [
                {"id": "e1", "requirement_id": "r1", "source_doc_id": "k1", "source_doc_name": "reference.txt", "source_doc_version": "1", "source_location": "line 1", "excerpt": "Knowledge", "relevance": 0.8},
            ],
            "review_logs": [
                {"id": "log1", "requirement_id": "r1", "action": "SET_DECISION", "old_value": "", "new_value": "COMPLIANT", "comment": "source decision", "operator": "source-author", "operated_at": NOW},
            ],
            "export_jobs": [
                {"id": "x1", "project_id": "p1", "sor_document_id": "d1", "requirement_set_id": "rs1", "file_name": "matrix.xlsx", "storage_key": export_key, "version_label": "v1", "compare_export_id": "", "snapshot_json": "[]", "export_mode": "FORMAL", "source_hash": "a" * 64, "knowledge_base_version": 1, "ai_run_id": "", "human_coverage": 0.5, "created_at": NOW},
            ],
            "source_item_manual_decision_versions": [
                {"project_id": "p1", "sor_document_id": "d1", "revision": 1, "artifact_sha256": manual["artifact_sha256"], "artifact_json": json.dumps(manual, ensure_ascii=False, sort_keys=True, separators=(",", ":")), "command_id": "USER/one", "request_sha256": "d" * 64, "operation": "SET", "created_at": NOW},
            ],
            "source_item_delivery_versions": [
                {"project_id": "p1", "sor_document_id": "d1", "revision": 1, "command_id": "delivery-one", "request_sha256": "e" * 64, "artifact_json": json.dumps({key: value for key, value in delivery.items() if key != "artifact_sha256"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")), "artifact_sha256": delivery["artifact_sha256"], "created_at": NOW},
            ],
            "ai_analysis_jobs": [
                {"id": "j1", "project_id": "p1", "sor_document_id": "d1", "requirement_set_id": "rs1", "status": "RUNNING", "created_at": NOW, "updated_at": NOW, "started_at": NOW, "finished_at": "", "error_message": ""},
            ],
            "requirement_matrix_node_states": [],
            "requirement_set_impact_receipts": [],
        }
        knowledge_map = {
            "schema_version": migration.KNOWLEDGE_MAPPING_SCHEMA_VERSION,
            "mappings": [
                {"source_kind": "knowledge_document", "source_id": "k1", "status": "RESOLVED", "target_kind": "pair_document", "target_id": "pair-k1", "target_revision": "1"},
            ],
        }
        map_path = self.root / "knowledge-map.json"
        map_path.write_text(json.dumps(knowledge_map), encoding="utf-8")
        return SyntheticSource(rows), object_root, {"map_path": map_path, "source_sha": source_sha, "document_key": document_key, "knowledge_key": knowledge_key, "export_key": export_key, "delivery_key": delivery_key, "workbook_sha": workbook_sha, "workbook": workbook, "manual": manual, "delivery": delivery}

    def _export(self, source: SyntheticSource, object_root: Path, name: str = "export", map_path: Path | None = None) -> Path:
        output = self.root / name
        migration._export_selected(source, project_ids=["p1"], object_root=object_root, output_dir=output, knowledge_map_path=map_path)
        return output


    def test_credential_filter_rejects_secrets_but_not_execution_fences(self) -> None:
        self.assertEqual(migration._secret_columns("ai_analysis_node_attempts", ["fence_token", "status"]), [])
        self.assertEqual(migration._secret_columns("requirement_sets", ["publication_owner_token"]), [])
        self.assertEqual(migration._secret_columns("saas_accounts", ["password_digest"]), ["saas_accounts.password_digest"])

    def test_export_import_verify_preserves_identity_bytes_and_decisions(self) -> None:
        source, object_root, expected = self._fixture()
        export_dir = self._export(source, object_root, map_path=expected["map_path"])
        manifest, table_entries = migration.validate_export(export_dir)
        self.assertEqual(table_entries["projects"]["row_count"], 1)
        self.assertNotIn("p2", [row["id"] for row in migration._iter_export_rows(export_dir, table_entries["projects"])])
        self.assertEqual(table_entries["source_item_manual_decision_versions"]["row_count"], 1)

        target = self.root / "target"
        receipt = migration.import_export(export_dir, target)
        self.assertEqual(receipt["status"], "COMPLETE")
        self.assertTrue(receipt["activation_ready"])
        self.assertEqual(receipt["activation_blockers"], [])
        self.assertEqual(receipt["active_jobs_quarantined"][0]["to_status"], "FAILED")
        self.assertTrue((target / "rx06" / "receipt.json").is_file())

        conn = sqlite3.connect(target / "app.db")
        conn.row_factory = sqlite3.Row
        try:
            document = conn.execute("SELECT * FROM sor_documents WHERE id='d1'").fetchone()
            self.assertEqual(document["source_sha256"], expected["source_sha"])
            self.assertEqual(Path(document["file_path"]), (target / "objects" / expected["document_key"]).resolve())
            self.assertEqual(Path(document["file_path"]).read_bytes(), b"%PDF-1.4 synthetic source\n")
            self.assertEqual(conn.execute("SELECT operator FROM review_logs WHERE id='log1'").fetchone()[0], "source-author")
            job = conn.execute("SELECT status, error_message FROM ai_analysis_jobs WHERE id='j1'").fetchone()
            self.assertEqual(job["status"], "FAILED")
            self.assertIn("RX06", job["error_message"])
            manual = conn.execute("SELECT artifact_json FROM source_item_manual_decision_versions").fetchone()[0]
            self.assertEqual(json.loads(manual)["source_sha256"], expected["source_sha"])
            delivery = conn.execute("SELECT artifact_json FROM source_item_delivery_versions").fetchone()[0]
            artifact = json.loads(delivery)
            self.assertEqual(artifact["export_sha256"], expected["workbook_sha"])
            self.assertEqual((target / "objects" / artifact["export_key"]).read_bytes(), expected["workbook"])
        finally:
            conn.close()

        verification = migration.verify_target(target)
        self.assertTrue(verification["ok"])
        self.assertEqual(verification["verification"]["domain_records"]["manual_decision_versions"], 1)
        self.assertEqual(verification["verification"]["domain_records"]["delivery_versions"], 1)

        before = (target / "app.db").read_bytes()
        with self.assertRaisesRegex(Rx06Error, "already exists"):
            migration.import_export(export_dir, target)
        self.assertEqual((target / "app.db").read_bytes(), before)

    def test_missing_knowledge_map_blocks_activation_but_keeps_originals(self) -> None:
        source, object_root, expected = self._fixture()
        export_dir = self._export(source, object_root, name="export-no-map", map_path=None)
        manifest = json.loads((export_dir / migration.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest["knowledge_mapping"]["report"]["status"], "MISSING")
        target = self.root / "target-no-map"
        receipt = migration.import_export(export_dir, target)
        self.assertFalse(receipt["activation_ready"])
        self.assertIn("KNOWLEDGE_MAPPING_MISSING", receipt["activation_blockers"])
        self.assertEqual(receipt["knowledge"]["status"], "MISSING")
        conn = sqlite3.connect(target / "app.db")
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM knowledge_documents WHERE id='k1'").fetchone()[0], 1)
            self.assertTrue((target / "objects" / expected["knowledge_key"]).is_file())
        finally:
            conn.close()

    def test_unknown_schema_unknown_table_and_changed_object_are_refused(self) -> None:
        source, object_root, expected = self._fixture()
        bad_migrations = [dict(item) for item in migration._packaged_migrations()]
        bad_migrations[-1]["sha256"] = "0" * 64
        with self.assertRaisesRegex(Rx06Error, "exact packaged version"):
            migration._export_selected(SyntheticSource(source.rows, bad_migrations), project_ids=["p1"], object_root=object_root, output_dir=self.root / "bad-schema", knowledge_map_path=expected["map_path"])

        unknown_rows = {**source.rows, "unknown_business_table": [{"id": "x"}]}
        with self.assertRaisesRegex(Rx06Error, "unknown tables"):
            migration._export_selected(SyntheticSource(unknown_rows), project_ids=["p1"], object_root=object_root, output_dir=self.root / "unknown-table", knowledge_map_path=expected["map_path"])

        (object_root / expected["document_key"]).write_bytes(b"changed")
        with self.assertRaisesRegex(Rx06Error, "hash mismatch"):
            migration._export_selected(source, project_ids=["p1"], object_root=object_root, output_dir=self.root / "changed-object", knowledge_map_path=expected["map_path"])

    def test_export_validation_rejects_tampering(self) -> None:
        source, object_root, expected = self._fixture()
        export_dir = self._export(source, object_root, name="tamper-export", map_path=expected["map_path"])
        table_path = export_dir / "tables" / "requirements.jsonl"
        table_path.write_text(table_path.read_text(encoding="utf-8").replace("The system shall work.", "Tampered."), encoding="utf-8")
        with self.assertRaisesRegex(Rx06Error, "was changed"):
            migration.validate_export(export_dir)


    def test_export_only_business_table_blocks_activation_without_losing_bytes(self) -> None:
        source, object_root, expected = self._fixture()
        source.rows["source_asset_references"] = [
            {"project_id": "p1", "document_id": "d1", "source_sha256": expected["source_sha"], "storage_key": expected["document_key"], "created_at": NOW},
        ]
        export_dir = self._export(source, object_root, name="export-only", map_path=expected["map_path"])
        target = self.root / "target-export-only"
        receipt = migration.import_export(export_dir, target)
        self.assertFalse(receipt["activation_ready"])
        self.assertIn("UNSUPPORTED_BUSINESS_TABLES_RETAINED_EXPORT_ONLY", receipt["activation_blockers"])
        manifest = json.loads((export_dir / migration.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(
            next(item for item in manifest["tables"] if item["name"] == "source_asset_references")["disposition"],
            "export_only",
        )
        self.assertTrue((target / "objects" / expected["document_key"]).is_file())
        conn = sqlite3.connect(target / "app.db")
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM sor_documents WHERE id='d1'").fetchone()[0], 1)
        finally:
            conn.close()

    def test_failed_import_keeps_isolated_incomplete_staging_and_never_publishes_target(self) -> None:
        source, object_root, expected = self._fixture()
        duplicate = dict(source.rows["requirements"][0])
        duplicate["req_code"] = "REQ-002"
        source.rows["requirements"].append(duplicate)
        export_dir = self._export(source, object_root, name="duplicate-export", map_path=expected["map_path"])
        migration.validate_export(export_dir)
        target = self.root / "target-duplicate"
        with self.assertRaisesRegex(Rx06Error, "incomplete staging retained"):
            migration.import_export(export_dir, target)
        self.assertFalse(target.exists())
        incomplete = sorted(self.root.glob(".target-duplicate.rx06-*.tmp"))
        self.assertEqual(len(incomplete), 1)
        self.assertTrue((incomplete[0] / ".RX06-INCOMPLETE").is_file())

    def test_backup_restore_verifies_wal_files_hashes_and_relocated_paths(self) -> None:
        source, object_root, expected = self._fixture()
        export_dir = self._export(source, object_root, name="backup-export", map_path=expected["map_path"])
        target = self.root / "backup-target"
        migration.import_export(export_dir, target)
        conn = sqlite3.connect(target / "app.db")
        self.addCleanup(conn.close)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO parsing_risk_acknowledgements (id, project_id, sor_document_id, source_sha256, parse_artifact_id, parse_identity_sha256, severe_warning_count, acknowledged_at) VALUES ('ack1','p1','d1',?, 'parse1', ?, 1, ?)", (expected["source_sha"], "f" * 64, NOW))
        conn.commit()
        self.assertTrue((target / "app.db-wal").exists())

        backup_dir = self.root / "backup"
        report = backup.create_backup(target, backup_dir)
        self.assertTrue(report["ok"])
        conn = sqlite3.connect(backup_dir / "data" / "app.db")
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM parsing_risk_acknowledgements WHERE id='ack1'").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertFalse((backup_dir / "data" / "app.db-wal").exists())

        restored = self.root / "restored"
        restored_report = backup.restore_backup(backup_dir, restored)
        self.assertTrue(restored_report["ok"])
        conn = sqlite3.connect(restored / "app.db")
        conn.row_factory = sqlite3.Row
        try:
            document = conn.execute("SELECT file_path FROM sor_documents WHERE id='d1'").fetchone()
            self.assertTrue(Path(document["file_path"]).resolve().is_relative_to(restored.resolve()))
            self.assertEqual(conn.execute("SELECT count(*) FROM parsing_risk_acknowledgements WHERE id='ack1'").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertTrue(migration.verify_target(restored)["ok"])

        tampered = backup_dir / "data" / "objects" / expected["document_key"]
        tampered.write_bytes(b"tampered")
        with self.assertRaisesRegex(Rx06Error, "hash mismatch"):
            backup.verify_backup(backup_dir)
        with self.assertRaisesRegex(Rx06Error, "already exists"):
            backup.create_backup(target, backup_dir)
        with self.assertRaisesRegex(Rx06Error, "already exists"):
            backup.restore_backup(backup_dir, restored)


if __name__ == "__main__":
    unittest.main()
