from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
from dataclasses import replace
import hashlib
import json
import tempfile
import unittest

from app.adapters.agents.reference_workspace import (
    PRODUCT_EVIDENCE_WORKSPACE_LOCK_SCHEMA_VERSION,
    PRODUCT_EVIDENCE_WORKSPACE_SCHEMA_VERSION,
    _inventory,
)
from app.adapters.persistence.native_document_map_scope_checkpoints import (
    NativeDocumentMapScopeCheckpointStore,
)
from app.adapters.storage.local_object_store import LocalObjectStore

from app.application.agent_workflow.document_map_replacement import (
    DocumentMapScopeReplacementCandidateV1,
    compile_document_map_scope_replacement,
    load_document_map_scope_evidence,
)
from app.application.agent_workflow.native_structure_working_revision import (
    canonical_json_sha256,
)
from tests.document_map_replacement_support import build_replacement_fixture


class DocumentMapScopeReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = build_replacement_fixture(Path(self.temporary.name))

    def _compile(self, candidate=None, pages=(1, 2)):
        return compile_document_map_scope_replacement(
            request=self.fixture.request,
            candidate=candidate or self.fixture.candidate,
            evidence=self.fixture.evidence,
            opened_high_resolution_page_numbers=pages,
            visual_receipt_root_sha256="f" * 64,
        )

    def test_agent_grouping_joins_physical_tables_without_column_count_gate(self) -> None:
        result = self._compile()
        self.assertEqual(result.status, "COMPLETE")
        self.assertFalse(result.document_map_complete)
        self.assertEqual(len(result.logical_tables), 1)
        self.assertEqual(len(result.logical_tables[0].fragments), 2)
        self.assertEqual(
            tuple(item.column_count for item in result.logical_tables[0].fragments),
            (3, 4),
        )
        self.assertEqual(len(result.asset_ownership), 1)

    def test_host_does_not_invent_logical_tables_when_agent_omits_them(self) -> None:
        candidate = self.fixture.candidate.model_copy(update={"logical_tables": ()})
        with self.assertRaisesRegex(ValueError, "exact one-owner coverage"):
            self._compile(candidate)

    def test_overlapping_table_rows_are_rejected(self) -> None:
        payload = self.fixture.candidate.model_dump(mode="json")
        duplicate = dict(payload["logical_tables"][0])
        duplicate["logical_table_key"] = "duplicate"
        duplicate["fragments"] = [duplicate["fragments"][0]]
        payload["logical_tables"].append(duplicate)
        candidate = DocumentMapScopeReplacementCandidateV1.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "exact one-owner coverage"):
            self._compile(candidate)

    def test_missing_asset_placement_is_not_silently_attached_to_nearest_heading(self) -> None:
        candidate = self.fixture.candidate.model_copy(update={"asset_ownership": ()})
        with self.assertRaisesRegex(ValueError, "asset placements require exact coverage"):
            self._compile(candidate)

    def test_agent_can_assign_trailing_text_back_to_parent_heading(self) -> None:
        result = self._compile()
        closing = next(
            item for item in result.text_ownership
            if item.source_node_id == self.fixture.evidence.records[5].source_node_id
        )
        self.assertEqual(
            closing.display_heading_node_id,
            self.fixture.request.heading_compilation.heading_nodes[0].stable_node_id,
        )

    def test_complete_scope_reuses_the_existing_durable_checkpoint_store(self) -> None:
        result = self._compile()
        store = NativeDocumentMapScopeCheckpointStore(
            object_store=LocalObjectStore(Path(self.temporary.name) / "objects"),
            project_id="project-synthetic",
            document_id="document-synthetic",
        )
        receipt = store.save(
            request=self.fixture.request,
            compilation=result,
            kit_bundle_sha256="3" * 64,
        )
        self.assertEqual(receipt.checkpoint_kind, "COMPLETE_SCOPE_REPLACEMENT")
        self.assertEqual(receipt.logical_table_count, 1)
        self.assertEqual(receipt.asset_placement_count, 1)
        self.assertEqual(store.load(receipt), result)

    def test_incomplete_visual_coverage_cannot_be_filled_with_synthetic_receipts(self) -> None:
        with self.assertRaisesRegex(ValueError, "visual lineage"):
            self._compile(pages=(1,))

    def test_unknown_text_source_is_rejected(self) -> None:
        payload = self.fixture.candidate.model_dump(mode="json")
        payload["text_ownership_overrides"][0]["source_node_ids"] = ["node-" + "f" * 32]
        candidate = DocumentMapScopeReplacementCandidateV1.model_validate(payload)
        with self.assertRaisesRegex(ValueError, "text ownership override is invalid"):
            self._compile(candidate)

    def test_table_text_alias_requires_both_exact_text_and_geometry(self) -> None:
        original = self.fixture.evidence.records[1]
        text = "row-0-column-0"
        alias_record = replace(
            original,
            raw_text=text,
            source_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            bbox=(55.0, 152.0, 125.0, 168.0),
        )
        records = list(self.fixture.evidence.records)
        records[1] = alias_record
        evidence = replace(self.fixture.evidence, records=tuple(records))
        result = compile_document_map_scope_replacement(
            request=self.fixture.request,
            candidate=self.fixture.candidate,
            evidence=evidence,
            opened_high_resolution_page_numbers=(1, 2),
            visual_receipt_root_sha256="f" * 64,
        )
        alias = next(item for item in result.text_ownership if item.source_node_id == original.source_node_id)
        self.assertEqual(alias.table_evidence_scope_id, result.logical_tables[0].logical_table_scope_id)
        records[1] = replace(alias_record, bbox=(55.0, 40.0, 125.0, 60.0))
        outside = compile_document_map_scope_replacement(
            request=self.fixture.request,
            candidate=self.fixture.candidate,
            evidence=replace(evidence, records=tuple(records)),
            opened_high_resolution_page_numbers=(1, 2),
            visual_receipt_root_sha256="f" * 64,
        )
        self.assertIsNone(next(item for item in outside.text_ownership if item.source_node_id == original.source_node_id).table_evidence_scope_id)

    def test_scope_evidence_is_loaded_from_locked_workspace_without_reparsing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source_bytes = b"%PDF-synthetic-workspace-only\n"
            source_sha = hashlib.sha256(source_bytes).hexdigest()
            fixture = build_replacement_fixture(root, source_sha=source_sha)
            (root / "source").mkdir()
            (root / "pages").mkdir()
            (root / "tables").mkdir()
            (root / "assets").mkdir()
            (root / "source/document.pdf").write_bytes(source_bytes)
            _write_jsonl(root / "pages/index.jsonl", [
                {"page_number": page, "bounds": [0.0, 0.0, 612.0, 792.0]}
                for page in (1, 2)
            ])
            _write_jsonl(root / "pages/source-map.jsonl", [
                {
                    "source_handle": item.source_node_id,
                    "source_order": item.source_order,
                    "kind": item.source_kind,
                    "bbox": list(item.bbox),
                    "table_id": item.table_id,
                    "asset_id": item.asset_id,
                }
                for item in fixture.evidence.records
            ])
            _write_jsonl(root / "pages/text-index.jsonl", [
                {"source_handle": item.source_node_id, "text": item.raw_text, "text_sha256": item.source_text_sha256}
                for item in fixture.evidence.records if item.raw_text
            ])
            _write_jsonl(root / "pages/source-ordinal-ledger.jsonl", [
                {
                    "source_node_id": item.source_node_id,
                    "source_order": item.source_order,
                    "source_kind": item.source_kind,
                    "page_number": item.page_number,
                    "source_text_sha256": item.source_text_sha256,
                    "table_id": item.table_id,
                    "asset_ref": item.asset_id,
                }
                for item in fixture.evidence.records
            ])
            table_indexes = []
            for table_id, table in fixture.evidence.tables_by_id.items():
                path = f"tables/{table['table_token']}.json"
                _write_json(root / path, table)
                table_indexes.append({
                    **fixture.evidence.table_index_by_id[table_id],
                    "json_path": path,
                })
            _write_json(root / "tables/index.json", {"tables": table_indexes})
            _write_json(root / "assets/index.json", {"unique_assets": list(fixture.evidence.assets_by_id.values())})
            manifest = {
                "schema_version": PRODUCT_EVIDENCE_WORKSPACE_SCHEMA_VERSION,
                "workspace_kind": "APP_SERVER_PRODUCT_READ_ONLY",
                "document": {"source_sha256": source_sha, "page_count": 2},
                "capture": {
                    "source_ir_id": fixture.request.scope_request.source_ir_id,
                    "source_ir_canonical_sha256": fixture.request.scope_request.source_ir_canonical_sha256,
                },
                "evidence_inventory_sha256": canonical_json_sha256(_inventory(root, include_manifest=False)),
            }
            _write_json(root / "manifest.json", manifest)
            files = _inventory(root, include_manifest=True)
            bundle_sha = canonical_json_sha256(files)
            _write_json(root / "workspace.lock.json", {
                "schema_version": PRODUCT_EVIDENCE_WORKSPACE_LOCK_SCHEMA_VERSION,
                "source_sha256": source_sha,
                "bundle_sha256": bundle_sha,
                "files": files,
            })
            bound = build_replacement_fixture(root, source_sha=source_sha, workspace_bundle_sha256=bundle_sha)
            loaded = load_document_map_scope_evidence(root=root, request=bound.request)
            self.assertEqual(loaded.records, bound.evidence.records)
            self.assertEqual(set(loaded.tables_by_id), set(bound.evidence.tables_by_id))


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, payloads) -> None:
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in payloads) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
