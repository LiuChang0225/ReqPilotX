"""local migration synthetic export, migration, and backup/restore contract tests."""

from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
from reqpilotx_server.tools import local_backup as backup
from reqpilotx_server.tools import local_migration as migration
from reqpilotx_server.tools import local_migration_common as common
from reqpilotx_server.tools.local_migration_common import LocalMigrationError, sha256_bytes

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

    def close(self) -> None:
        return None


class LocalMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="local-migration-synthetic-", ignore_cleanup_errors=True)
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
            "knowledge_document_syncs": [
                {"id": "sync1", "knowledge_document_id": "k1", "profile_id": "profile", "provider": "source", "retrieval_backend": "local", "status": "READY", "error_message": "", "synced_at": NOW, "chunk_count": 1, "payload_storage_key": "", "updated_at": NOW},
            ],
            "requirement_sets": [
                {"id": "rs1", "project_id": "p1", "sor_document_id": "d1", "version": 1, "status": "LOCKED", "is_active": 1, "source_hash": "a" * 64, "preview_generation_id": "pg1", "supersedes_id": "", "created_at": NOW, "activated_at": NOW, "locked_at": NOW, "lock_waiver_reason": "", "preview_status_at_lock": "READY", "requirement_tree_object_key": "", "requirement_tree_sha256": "", "requirement_tree_schema_version": "", "structure_snapshot_id": "", "structure_tree_sha256": "", "structure_revision": 0, "publication_key": "", "publication_state": "", "publication_owner_token": "", "publication_lease_expires_at": "", "publication_heartbeat_at": "", "entry_classification_snapshot_id": "", "entry_classification_snapshot_sha256": "", "enrichment_publication_binding_id": "", "enrichment_publication_binding_sha256": ""},
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
        self.assertTrue((target / "local-migration" / "receipt.json").is_file())

        conn = sqlite3.connect(target / "app.db")
        conn.row_factory = sqlite3.Row
        try:
            document = conn.execute("SELECT * FROM sor_documents WHERE id='d1'").fetchone()
            self.assertEqual(document["source_sha256"], expected["source_sha"])
            self.assertEqual(Path(document["file_path"]), (target / "objects" / expected["document_key"]).resolve())
            self.assertEqual(Path(document["file_path"]).read_bytes(), b"%PDF-1.4 synthetic source\n")
            self.assertEqual(conn.execute("SELECT operator FROM review_logs WHERE id='log1'").fetchone()[0], "source-author")
            self.assertEqual(conn.execute("SELECT payload_path FROM knowledge_document_syncs WHERE id='sync1'").fetchone()[0], "")
            self.assertEqual(conn.execute("SELECT requirement_tree_object_key FROM requirement_sets WHERE id='rs1'").fetchone()[0], "")
            job = conn.execute("SELECT status, error_message FROM ai_analysis_jobs WHERE id='j1'").fetchone()
            self.assertEqual(job["status"], "FAILED")
            self.assertIn("local migration", job["error_message"])
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
        with self.assertRaisesRegex(LocalMigrationError, "already exists"):
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
        with self.assertRaisesRegex(LocalMigrationError, "exact packaged version"):
            migration._export_selected(SyntheticSource(source.rows, bad_migrations), project_ids=["p1"], object_root=object_root, output_dir=self.root / "bad-schema", knowledge_map_path=expected["map_path"])

        unknown_rows = {**source.rows, "unknown_business_table": [{"id": "x"}]}
        with self.assertRaisesRegex(LocalMigrationError, "unknown tables"):
            migration._export_selected(SyntheticSource(unknown_rows), project_ids=["p1"], object_root=object_root, output_dir=self.root / "unknown-table", knowledge_map_path=expected["map_path"])

        (object_root / expected["document_key"]).write_bytes(b"changed")
        with self.assertRaisesRegex(LocalMigrationError, "hash mismatch"):
            migration._export_selected(source, project_ids=["p1"], object_root=object_root, output_dir=self.root / "changed-object", knowledge_map_path=expected["map_path"])

    def test_export_validation_rejects_tampering(self) -> None:
        source, object_root, expected = self._fixture()
        export_dir = self._export(source, object_root, name="tamper-export", map_path=expected["map_path"])
        table_path = export_dir / "tables" / "requirements.jsonl"
        table_path.write_text(table_path.read_text(encoding="utf-8").replace("The system shall work.", "Tampered."), encoding="utf-8")
        with self.assertRaisesRegex(LocalMigrationError, "was changed"):
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
        with self.assertRaisesRegex(LocalMigrationError, "incomplete staging retained"):
            migration.import_export(export_dir, target)
        self.assertFalse(target.exists())
        incomplete = sorted(self.root.glob(".target-duplicate.local-migration-*.tmp"))
        self.assertEqual(len(incomplete), 1)
        self.assertTrue((incomplete[0] / ".LOCAL-MIGRATION-INCOMPLETE").is_file())


    def test_inventory_cli_never_overwrites_or_writes_into_source_objects(self) -> None:
        source, object_root, _expected = self._fixture()
        original = object_root / "original.pdf"
        original.write_bytes(b"ORIGINAL SOURCE BYTES")
        original.chmod(0o640)
        original_mode = stat.S_IMODE(original.stat().st_mode)
        output = self.root / "inventory.json"
        with mock.patch.dict(os.environ, {"REQPILOT_DATABASE_URL": "postgresql://not-used"}, clear=False),              mock.patch.object(migration, "_PsycopgSource", return_value=source),              mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(migration.main(["inventory", "--project-id", "p1", "--object-root", str(object_root), "--output", str(original)]), 2)
            self.assertEqual(original.read_bytes(), b"ORIGINAL SOURCE BYTES")
            self.assertEqual(stat.S_IMODE(original.stat().st_mode), original_mode)
            inside = object_root / "new-inventory.json"
            self.assertEqual(migration.main(["inventory", "--project-id", "p1", "--object-root", str(object_root), "--output", str(inside)]), 2)
            self.assertFalse(inside.exists())
            self.assertEqual(migration.main(["inventory", "--project-id", "p1", "--object-root", str(object_root), "--output", str(output)]), 0)
            first = output.read_bytes()
            self.assertEqual(migration.main(["inventory", "--project-id", "p1", "--object-root", str(object_root), "--output", str(output)]), 2)
            self.assertEqual(output.read_bytes(), first)


    def test_atomic_new_json_output_race_never_overwrites_the_racer(self) -> None:
        target = self.root / "race.json"
        real_rename = common.rename_no_replace

        def race(source: Path, destination: Path) -> None:
            destination.write_text("RACER BYTES", encoding="utf-8")
            real_rename(source, destination)

        with mock.patch.object(common, "rename_no_replace", side_effect=race):
            with self.assertRaisesRegex(LocalMigrationError, "already exists"):
                common.write_new_json(target, {"ok": True})
        self.assertEqual(target.read_text(encoding="utf-8"), "RACER BYTES")
        self.assertEqual(list(self.root.glob(".race.json.*.tmp")), [])

    def test_nested_object_symlink_outside_source_root_is_refused(self) -> None:
        source, object_root, expected = self._fixture()
        outside = self.root / "outside-sentinel"
        outside.mkdir()
        sentinel = outside / "private.bin"
        sentinel.write_bytes(b"OUTSIDE SENTINEL BYTES")
        (object_root / "link").symlink_to(outside, target_is_directory=True)
        source.rows["export_jobs"][0]["storage_key"] = "link/private.bin"
        source.rows["stored_objects"].append(
            {"storage_key": "link/private.bin", "sha256": sha256_bytes(sentinel.read_bytes()), "size_bytes": sentinel.stat().st_size, "media_type": "application/octet-stream", "object_kind": "export", "created_at": NOW}
        )
        output = self.root / "symlink-export"
        with self.assertRaisesRegex(LocalMigrationError, "missing or unsafe"):
            migration._export_selected(source, project_ids=["p1"], object_root=object_root, output_dir=output, knowledge_map_path=expected["map_path"])
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "symlink-export" / "objects" / "link" / "private.bin").exists())

    def test_decoded_jsonb_and_string_json_references_both_export_objects(self) -> None:
        source, object_root, expected = self._fixture()
        object_key = "agent-structure/shared-artifact.json"
        payload = b'{"artifact":"synthetic"}'
        digest = sha256_bytes(payload)
        target_file = object_root / object_key
        target_file.parent.mkdir(parents=True)
        target_file.write_bytes(payload)
        source.rows["agent_structure_proposals"] = [
            {
                "id": "proposal-1",
                "project_id": "p1",
                "sor_document_id": "d1",
                "source_sha256": expected["source_sha"],
                "proposal_payload": {"artifact": {"storage_key": object_key, "sha256": digest}},
                "runtime_receipt": json.dumps({"artifact": {"storage_key": object_key, "sha256": digest}}),
            }
        ]
        export_dir = self._export(source, object_root, name="jsonb-export", map_path=expected["map_path"])
        manifest = json.loads((export_dir / migration.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertIn(object_key, {entry["storage_key"] for entry in manifest["objects"]})
        self.assertEqual((export_dir / "objects" / object_key).read_bytes(), payload)
        keys, expected_hashes = migration._row_object_references("agent_structure_proposals", source.rows["agent_structure_proposals"][0])
        self.assertIn(object_key, keys)
        self.assertEqual(expected_hashes[object_key], {digest})
        self.assertTrue(migration.validate_export(export_dir))

    def test_project_ownership_evidence_survives_export_import_backup_restore(self) -> None:
        source, object_root, expected = self._fixture()
        source.rows["saas_project_owners"] = [{"project_id": "p1", "account_id": "owner-original"}]
        export_dir = self._export(source, object_root, name="owner-export", map_path=expected["map_path"])
        manifest = json.loads((export_dir / migration.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertIn("saas_project_owners", {entry["name"] for entry in manifest["tables"]})
        self.assertNotIn("saas_accounts", {entry["name"] for entry in manifest["tables"]})
        target = self.root / "owner-target"
        receipt = migration.import_export(export_dir, target)
        self.assertFalse(receipt["activation_ready"])
        self.assertIn("SOURCE_PROJECT_OWNERSHIP_RETAINED_AS_EVIDENCE", receipt["activation_blockers"])
        ownership_path = target / "local-migration" / "project-ownership.json"
        self.assertTrue(ownership_path.is_file())
        ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        self.assertEqual(ownership["rows"], [{"project_id": "p1", "account_id": "owner-original"}])
        backup_dir = self.root / "owner-backup"
        backup.create_backup(target, backup_dir)
        restored = self.root / "owner-restored"
        backup.restore_backup(backup_dir, restored)
        restored_ownership = json.loads((restored / "local-migration" / "project-ownership.json").read_text(encoding="utf-8"))
        self.assertEqual(restored_ownership["rows"], [{"project_id": "p1", "account_id": "owner-original"}])
        verification = migration.verify_target(restored)
        self.assertTrue(verification["ok"])
        self.assertEqual(verification["verification"]["ownership"]["rows"], [{"project_id": "p1", "account_id": "owner-original"}])

    def test_knowledge_mapping_entries_are_retained_and_verified_across_backup_restore(self) -> None:
        source, object_root, expected = self._fixture()
        export_dir = self._export(source, object_root, name="mapping-export", map_path=expected["map_path"])
        target = self.root / "mapping-target"
        receipt = migration.import_export(export_dir, target)
        self.assertTrue(receipt["activation_ready"])
        mapping_path = target / "local-migration" / "knowledge-mapping.json"
        self.assertTrue(mapping_path.is_file())
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(mapping["mappings"][0]["target_id"], "pair-k1")
        self.assertEqual(receipt["knowledge"]["mapping"]["mappings"][0]["target_id"], "pair-k1")
        backup_dir = self.root / "mapping-backup"
        backup.create_backup(target, backup_dir)
        restored = self.root / "mapping-restored"
        backup.restore_backup(backup_dir, restored)
        restored_mapping = json.loads((restored / "local-migration" / "knowledge-mapping.json").read_text(encoding="utf-8"))
        self.assertEqual(restored_mapping["mappings"][0]["target_id"], "pair-k1")
        self.assertTrue(migration.verify_target(restored)["ok"])
        mapping_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(LocalMigrationError, "mapping"):
            migration.verify_target(target)
        corrupt_backup = self.root / "corrupt-mapping-backup"
        with self.assertRaisesRegex(LocalMigrationError, "mapping"):
            backup.create_backup(target, corrupt_backup)
        self.assertFalse(corrupt_backup.exists())



    def test_public_cli_export_import_verify_backup_restore_paths(self) -> None:
        source, object_root, expected = self._fixture()
        export_dir = self.root / "cli-export"
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"REQPILOT_DATABASE_URL": "postgresql://not-used"}, clear=False), \
             mock.patch.object(migration, "_PsycopgSource", return_value=source), \
             mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(migration.main(["export", "--project-id", "p1", "--object-root", str(object_root), "--output", str(export_dir), "--knowledge-map", str(expected["map_path"])]), 0)
            self.assertEqual(migration.main(["validate-export", "--export", str(export_dir)]), 0)
            target = self.root / "cli-target"
            self.assertEqual(migration.main(["import", "--export", str(export_dir), "--target-root", str(target)]), 0)
            self.assertEqual(migration.main(["verify-target", "--target-root", str(target)]), 0)
            backup_dir = self.root / "cli-backup"
            self.assertEqual(backup.main(["backup", "--root", str(target), "--output", str(backup_dir)]), 0)
            self.assertEqual(backup.main(["verify", "--backup", str(backup_dir)]), 0)
            restored = self.root / "cli-restored"
            self.assertEqual(backup.main(["restore", "--backup", str(backup_dir), "--target-root", str(restored)]), 0)
            self.assertEqual(migration.main(["verify-target", "--target-root", str(restored)]), 0)
        self.assertTrue((target / "local-migration" / "knowledge-mapping.json").is_file())
        self.assertTrue((restored / "local-migration" / "knowledge-mapping.json").is_file())

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
        with self.assertRaisesRegex(LocalMigrationError, "hash mismatch"):
            backup.verify_backup(backup_dir)
        with self.assertRaisesRegex(LocalMigrationError, "already exists"):
            backup.create_backup(target, backup_dir)
        with self.assertRaisesRegex(LocalMigrationError, "already exists"):
            backup.restore_backup(backup_dir, restored)


if __name__ == "__main__":
    unittest.main()
