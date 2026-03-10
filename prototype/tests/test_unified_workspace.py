from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from openpyxl import load_workbook

from app import db, main
from app.services import preview_cache
from app.services.exporter import export_compliance_matrix


def _fake_preview() -> dict:
    return {
        "summary": {
            "dedupe_version": "heading_requirement_v1",
            "dedup_exact_count": 1,
            "dedup_prefix_count": 0,
            "hybrid_entries_count": 0,
        },
        "sections": [
            {
                "scope": "Main",
                "section_code": "1",
                "title": "概述",
                "page_span": "1-1",
                "requirement_count": 1,
            }
        ],
        "complete_requirement_entries": [
            {
                "entry_id": "REQ0001",
                "req_code": "REQ0001",
                "entry_kind": "REQUIREMENT",
                "display_text": "供应商需要满足总成试验要求。",
                "raw_text": "供应商需要满足总成试验要求。",
                "requirement_text": "供应商需要满足总成试验要求。",
                "source_page": 1,
                "source_page_end": 1,
                "source_anchor": "Main > 1",
                "parent_section_code": "1",
                "parent_section_title": "概述",
                "parent_section_label": "1 概述",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
                "text_relation": "distinct",
                "is_deduped": False,
                "merged_from_req_codes": [],
                "merged_from_entry_ids": [],
            }
        ],
        "complete_requirement_entries_raw": [
            {
                "entry_id": "H-1",
                "req_code": "",
                "entry_kind": "HEADING",
                "display_text": "1 概述",
                "raw_text": "1 概述",
                "requirement_text": "1 概述",
                "source_page": 1,
                "source_page_end": 1,
                "source_anchor": "Main > 1",
                "parent_section_code": "1",
                "parent_section_title": "概述",
                "parent_section_label": "1 概述",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
                "text_relation": "exact_match",
                "is_deduped": True,
                "merged_from_req_codes": [],
                "merged_from_entry_ids": [],
            },
            {
                "entry_id": "REQ0001",
                "req_code": "REQ0001",
                "entry_kind": "REQUIREMENT",
                "display_text": "供应商需要满足总成试验要求。",
                "raw_text": "供应商需要满足总成试验要求。",
                "requirement_text": "供应商需要满足总成试验要求。",
                "source_page": 1,
                "source_page_end": 1,
                "source_anchor": "Main > 1",
                "parent_section_code": "1",
                "parent_section_title": "概述",
                "parent_section_label": "1 概述",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
                "text_relation": "distinct",
                "is_deduped": False,
                "merged_from_req_codes": [],
                "merged_from_entry_ids": [],
            },
        ],
    }


def _fake_preview_with_reversed_req_codes() -> dict:
    return {
        "summary": {"dedupe_version": "heading_requirement_v1"},
        "sections": [],
        "complete_requirement_entries": [
            {
                "entry_id": "REQ0024",
                "req_code": "REQ0024",
                "entry_kind": "REQUIREMENT",
                "display_text": "5.1 首条需求",
                "raw_text": "5.1 首条需求",
                "requirement_text": "5.1 首条需求",
                "source_page": 5,
                "source_page_end": 5,
                "source_anchor": "Main > 5.1",
                "parent_section_code": "5.1",
                "parent_section_title": "首条需求",
                "parent_section_label": "5.1 首条需求",
            },
            {
                "entry_id": "REQ0023",
                "req_code": "REQ0023",
                "entry_kind": "REQUIREMENT",
                "display_text": "5.7 次条需求",
                "raw_text": "5.7 次条需求",
                "requirement_text": "5.7 次条需求",
                "source_page": 5,
                "source_page_end": 5,
                "source_anchor": "Main > 5.7",
                "parent_section_code": "5.7",
                "parent_section_title": "次条需求",
                "parent_section_label": "5.7 次条需求",
            },
        ],
        "complete_requirement_entries_raw": [],
    }


def _fake_preview_with_table_markdown() -> dict:
    table_text = (
        "试验条件如下：\n\n"
        "| Item | Condition |\n"
        "| --- | --- |\n"
        "| Voltage | 12V |\n"
        "| Current | 3A |"
    )
    return {
        "summary": {
            "dedupe_version": "heading_requirement_v1",
            "dedup_exact_count": 0,
            "dedup_prefix_count": 0,
            "hybrid_entries_count": 0,
        },
        "sections": [
            {
                "scope": "Main",
                "section_code": "5.1",
                "title": "Test Setup",
                "page_span": "5-5",
                "requirement_count": 1,
            }
        ],
        "complete_requirement_entries": [
            {
                "entry_id": "REQ0008",
                "req_code": "REQ0008",
                "entry_kind": "REQUIREMENT",
                "display_text": table_text,
                "raw_text": table_text,
                "requirement_text": table_text,
                "source_page": 5,
                "source_page_end": 5,
                "source_anchor": "Main > 5.1",
                "parent_section_code": "5.1",
                "parent_section_title": "Test Setup",
                "parent_section_label": "5.1 Test Setup",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
                "text_relation": "distinct",
                "is_deduped": False,
                "merged_from_req_codes": [],
                "merged_from_entry_ids": [],
            }
        ],
        "complete_requirement_entries_raw": [],
    }


class UnifiedWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.tmp_path / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.tmp_path))
        self.exit_stack.enter_context(patch.object(main, "EXPORTS_DIR", self.tmp_path / "exports"))
        db.init_db()

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.tmpdir.cleanup()

    def _create_project_with_document(self, suffix: str = "") -> tuple[dict, dict]:
        sample_path = self.tmp_path / f"sample{suffix}.txt"
        sample_path.write_text("sample", encoding="utf-8")
        project = db.create_project(name=f"Workspace Test{suffix}", customer_name="Customer")
        document = db.add_sor_document(
            project_id=project["id"],
            file_name=f"sample{suffix}.txt",
            file_type="txt",
            file_path=str(sample_path),
            extracted_text="sample",
        )
        return project, document

    def _add_knowledge_document(self, project_id: str, suffix: str = "") -> dict:
        knowledge_path = self.tmp_path / f"knowledge{suffix}.txt"
        knowledge_path.write_text("knowledge", encoding="utf-8")
        return db.add_knowledge_document(
            project_id=project_id,
            file_name=f"knowledge{suffix}.txt",
            file_type="txt",
            file_path=str(knowledge_path),
            extracted_text="knowledge",
        )

    def _version_meta_values(self, workbook) -> dict[str, object]:
        sheet = workbook["VersionMeta"]
        return {
            str(sheet.cell(row=row, column=1).value): sheet.cell(row=row, column=2).value
            for row in range(1, sheet.max_row + 1)
            if sheet.cell(row=row, column=1).value is not None
        }

    def test_extraction_review_redirects_to_workspace_review(self) -> None:
        response = main.extraction_review_page()
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers.get("location"), "/workspace?stage=review")

    def test_review_entry_req_codes_are_unique_per_document(self) -> None:
        project = db.create_project(name="ReqCode Test", customer_name="Customer")
        file_a = self.tmp_path / "a.txt"
        file_b = self.tmp_path / "b.txt"
        file_a.write_text("a", encoding="utf-8")
        file_b.write_text("b", encoding="utf-8")
        doc_a = db.add_sor_document(
            project_id=project["id"],
            file_name="a.txt",
            file_type="txt",
            file_path=str(file_a),
            extracted_text="a",
        )
        doc_b = db.add_sor_document(
            project_id=project["id"],
            file_name="b.txt",
            file_type="txt",
            file_path=str(file_b),
            extracted_text="b",
        )
        payload = {
            "entry_id": "REQ0001",
            "req_code": "REQ0001",
            "entry_kind": "REQUIREMENT",
            "source_page": 1,
            "source_anchor": "Main > 1",
            "requirement_text": "同一项目下的两个文档都可以复用相同 req_code。",
            "display_text": "同一项目下的两个文档都可以复用相同 req_code。",
            "raw_text": "同一项目下的两个文档都可以复用相同 req_code。",
            "requirement_type": "FUNCTIONAL",
            "priority": "MEDIUM",
            "keep": True,
        }

        res_a = main.save_review_entry(
            project["id"],
            doc_a["id"],
            main.ReviewedRequirementEntry(**payload),
        )
        res_b = main.save_review_entry(
            project["id"],
            doc_b["id"],
            main.ReviewedRequirementEntry(**payload),
        )

        self.assertEqual(res_a["review_save"]["action"], "inserted")
        self.assertEqual(res_b["review_save"]["action"], "inserted")
        self.assertEqual(res_a["saved_entry"]["req_code"], "REQ0001")
        self.assertEqual(res_b["saved_entry"]["req_code"], "REQ0001")
        rows_a = db.list_requirements(project["id"], doc_a["id"])
        rows_b = db.list_requirements(project["id"], doc_b["id"])
        self.assertEqual([row["req_code"] for row in rows_a], ["REQ0001"])
        self.assertEqual([row["req_code"] for row in rows_b], ["REQ0001"])

    def test_workspace_include_preview_returns_review_entries(self) -> None:
        project, document = self._create_project_with_document("-preview")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            analyze = main.analyze_document(
                project["id"],
                main.AnalyzeRequest(document_id=document["id"]),
            )
            workspace = main.get_document_workspace(
                project["id"],
                document["id"],
                include_preview=True,
            )

        self.assertEqual(analyze["requirements_generated"], 1)
        self.assertEqual(workspace["document"]["workflow_stage"], "EXTRACTED")
        self.assertEqual(workspace["document"]["requirement_set_version"], 1)
        self.assertEqual(len(workspace["requirements"]), 1)
        self.assertEqual(len(workspace["review_entries"]), 1)
        self.assertEqual(len(workspace["review_entries_raw"]), 2)
        self.assertTrue(workspace["review_entries"][0]["keep"])
        self.assertFalse(workspace["review_entries_raw"][0]["keep"])
        self.assertEqual(workspace["summary"]["order_source"], "review_overlay")
        self.assertEqual(workspace["summary"]["order_mismatch_count"], 0)
        self.assertEqual(workspace["summary"]["preview_status"], "READY")
        self.assertTrue(workspace["summary"]["preview_cache_hit"])

    def test_workspace_without_preview_does_not_rebuild_preview(self) -> None:
        project, document = self._create_project_with_document("-no-preview-build")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        with patch.object(main, "build_extraction_preview", side_effect=AssertionError("workspace rebuilt preview")):
            workspace = main.get_document_workspace(project["id"], document["id"], include_preview=False)

        self.assertEqual(len(workspace["requirements"]), 1)
        self.assertEqual(workspace["summary"]["preview_status"], "READY")
        self.assertTrue(workspace["summary"]["preview_cache_hit"])

    def test_extraction_preview_reads_cached_preview(self) -> None:
        project, document = self._create_project_with_document("-cached-preview")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        with patch.object(main, "build_extraction_preview", side_effect=AssertionError("preview rebuilt")):
            payload = main.get_sor_extraction_preview(project["id"], document["id"], response=main.Response())

        self.assertEqual(payload["preview_status"], "READY")
        self.assertTrue(payload["preview_cache_hit"])
        self.assertEqual(len(payload["review_entries"]), 1)
        self.assertEqual(len(payload["review_entries_raw"]), 2)

    def test_review_entry_roundtrip_updates_workspace_and_version(self) -> None:
        project, document = self._create_project_with_document("-roundtrip")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            analyze = main.analyze_document(
                project["id"],
                main.AnalyzeRequest(document_id=document["id"]),
            )
            self.assertEqual(analyze["requirements_generated"], 1)

        insert_heading = main.save_review_entry(
            project["id"],
            document["id"],
            main.ReviewedRequirementEntry(
                entry_id="H-1",
                req_code="",
                entry_kind="HEADING",
                source_page=1,
                source_anchor="Main > 1",
                requirement_text="1 概述",
                display_text="1 概述",
                raw_text="1 概述",
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                text_relation="exact_match",
                is_deduped=True,
                parent_section_code="1",
                parent_section_title="概述",
                parent_section_label="1 概述",
                keep=True,
            ),
        )
        insert_payload = insert_heading
        self.assertEqual(insert_payload["review_save"]["action"], "inserted")
        self.assertEqual(insert_payload["review_save"]["req_code"], "H-1")
        self.assertEqual(insert_payload["saved_entry"]["req_code"], "H-1")
        self.assertEqual(insert_payload["saved_entry"]["keep"], True)
        self.assertEqual(insert_payload["document"]["requirement_set_version"], 2)
        self.assertEqual(len(insert_payload["requirements"]), 2)

        update_heading = main.save_review_entry(
            project["id"],
            document["id"],
            main.ReviewedRequirementEntry(
                entry_id="H-1",
                req_code="",
                entry_kind="HYBRID",
                source_page=1,
                source_anchor="Main > 1",
                requirement_text="1 概述",
                display_text="1 概述",
                raw_text="1 概述",
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                text_relation="exact_match",
                is_deduped=True,
                parent_section_code="1",
                parent_section_title="概述",
                parent_section_label="1 概述",
                keep=True,
            ),
        )
        update_payload = update_heading
        self.assertEqual(update_payload["review_save"]["action"], "updated")
        self.assertEqual(update_payload["saved_entry"]["entry_kind"], "HYBRID")
        self.assertEqual(update_payload["saved_entry"]["req_code"], "H-1")
        self.assertEqual(update_payload["document"]["requirement_set_version"], 3)

        remove_heading = main.save_review_entry(
            project["id"],
            document["id"],
            main.ReviewedRequirementEntry(
                entry_id="H-1",
                req_code="",
                entry_kind="HEADING",
                source_page=1,
                source_anchor="Main > 1",
                requirement_text="1 概述",
                display_text="1 概述",
                raw_text="1 概述",
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                text_relation="exact_match",
                is_deduped=True,
                parent_section_code="1",
                parent_section_title="概述",
                parent_section_label="1 概述",
                keep=False,
            ),
        )
        remove_payload = remove_heading
        self.assertEqual(remove_payload["review_save"]["action"], "removed")
        self.assertEqual(remove_payload["review_save"]["req_code"], "H-1")
        self.assertEqual(remove_payload["saved_entry"]["keep"], False)
        self.assertEqual(remove_payload["document"]["requirement_set_version"], 4)
        self.assertEqual(len(remove_payload["requirements"]), 1)

    def test_review_save_uses_cached_preview_and_does_not_rebuild(self) -> None:
        project, document = self._create_project_with_document("-review-cache")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        with patch.object(main, "build_extraction_preview", side_effect=AssertionError("review save rebuilt preview")):
            saved = main.save_review_entry(
                project["id"],
                document["id"],
                main.ReviewedRequirementEntry(
                    entry_id="REQ0001",
                    req_code="REQ0001",
                    entry_kind="REQUIREMENT",
                    source_page=1,
                    source_page_end=1,
                    source_anchor="Main > 1",
                    requirement_text="供应商需要满足总成试验要求（缓存更新）。",
                    display_text="供应商需要满足总成试验要求（缓存更新）。",
                    raw_text="供应商需要满足总成试验要求（缓存更新）。",
                    requirement_type="FUNCTIONAL",
                    priority="MEDIUM",
                    parent_section_code="1",
                    parent_section_title="概述",
                    parent_section_label="1 概述",
                    keep=True,
                ),
            )

        self.assertEqual(saved["document"]["workflow_stage"], "REVIEWING")
        self.assertEqual(saved["summary"]["preview_status"], "READY")
        self.assertTrue(saved["summary"]["preview_cache_hit"])
        self.assertEqual(saved["saved_entry"]["raw_text"], "供应商需要满足总成试验要求（缓存更新）。")

    def test_regenerate_requirement_set_clears_lock_and_invalidates_ai(self) -> None:
        project, document = self._create_project_with_document("-regen")
        preview = _fake_preview()
        with patch.object(main, "build_extraction_preview", return_value=preview):
            first = main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))
        self.assertEqual(first["requirement_set_version"], 1)

        locked = main.lock_document_extraction(project["id"], document["id"])
        self.assertEqual(locked["document"]["workflow_stage"], "EXTRACTION_LOCKED")

        db.update_sor_document_ai_state(
            document["id"],
            ai_status="READY",
            ai_assessed_at="2026-03-08T00:00:00+00:00",
            ai_requirement_set_version=1,
            ai_knowledge_base_version=0,
            workflow_stage="AI_READY",
        )

        with patch.object(main, "build_extraction_preview", return_value=preview):
            second = main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        current = db.get_sor_document(document["id"]) or {}
        self.assertEqual(second["requirement_set_version"], 2)
        self.assertEqual(current.get("workflow_stage"), "EXTRACTED")
        self.assertEqual(str(current.get("extraction_locked_at") or ""), "")
        self.assertEqual(str(current.get("ai_status") or ""), "STALE")

    def test_review_save_auto_unlocks_and_disables_export(self) -> None:
        project, document = self._create_project_with_document("-auto-unlock")
        preview = _fake_preview()
        with patch.object(main, "build_extraction_preview", return_value=preview):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))
            main.lock_document_extraction(project["id"], document["id"])
            saved = main.save_review_entry(
                project["id"],
                document["id"],
                main.ReviewedRequirementEntry(
                    entry_id="REQ0001",
                    req_code="REQ0001",
                    entry_kind="REQUIREMENT",
                    source_page=1,
                    source_page_end=1,
                    source_anchor="Main > 1",
                    requirement_text="供应商需要满足总成试验要求（更新）。",
                    display_text="供应商需要满足总成试验要求（更新）。",
                    raw_text="供应商需要满足总成试验要求（更新）。",
                    requirement_type="FUNCTIONAL",
                    priority="MEDIUM",
                    parent_section_code="1",
                    parent_section_title="概述",
                    parent_section_label="1 概述",
                    keep=True,
                ),
            )

        self.assertEqual(saved["document"]["workflow_stage"], "REVIEWING")
        self.assertFalse(saved["summary"]["locked"])
        self.assertFalse(saved["actions"]["can_export"])
        self.assertEqual(saved["saved_entry"]["raw_text"], "供应商需要满足总成试验要求（更新）。")

    def test_lock_and_ai_gate_behavior(self) -> None:
        project, empty_document = self._create_project_with_document("-empty")
        with self.assertRaises(HTTPException) as lock_exc:
            main.lock_document_extraction(project["id"], empty_document["id"])
        self.assertEqual(lock_exc.exception.status_code, 409)

        project, document = self._create_project_with_document("-gate")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        workspace_before_lock = main.get_document_workspace(project["id"], document["id"], include_preview=False)
        self.assertFalse(workspace_before_lock["actions"]["can_export"])

        with self.assertRaises(HTTPException) as ai_before_lock_exc:
            main.run_document_ai_analysis(project["id"], document["id"])
        self.assertEqual(ai_before_lock_exc.exception.status_code, 409)

        workspace_locked = main.lock_document_extraction(project["id"], document["id"])
        self.assertTrue(workspace_locked["summary"]["locked"])
        self.assertTrue(workspace_locked["actions"]["can_export"])
        self.assertFalse(workspace_locked["actions"]["can_run_ai_analysis"])

        with self.assertRaises(HTTPException) as ai_without_kb_exc:
            main.run_document_ai_analysis(project["id"], document["id"])
        self.assertEqual(ai_without_kb_exc.exception.status_code, 422)

        self._add_knowledge_document(project["id"], "-gate")
        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=False)
        self.assertTrue(workspace["actions"]["can_run_ai_analysis"])

    def test_refresh_preview_endpoint_builds_and_persists_cache(self) -> None:
        project, document = self._create_project_with_document("-refresh-preview")
        cache_path = preview_cache.preview_cache_path(document["id"])
        if cache_path.exists():
            cache_path.unlink()

        with patch.object(preview_cache._PREVIEW_EXECUTOR, "submit", return_value=None):
            response = main.refresh_document_preview(project["id"], document["id"])
        self.assertEqual(response["preview_status"], "BUILDING")

        with patch.object(preview_cache, "build_extraction_preview", return_value=_fake_preview()):
            preview_cache._run_preview_refresh(document["id"], document["file_path"])

        refreshed = main.get_sor_extraction_preview(project["id"], document["id"], response=main.Response())
        self.assertEqual(refreshed["preview_status"], "READY")
        self.assertTrue(refreshed["preview_cache_hit"])
        self.assertTrue(cache_path.exists())

    def test_workspace_reorders_matrix_display_ids_by_preview_order(self) -> None:
        project, document = self._create_project_with_document("-ordering")
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0023",
            source_page=5,
            source_anchor="Main > 5.7",
            requirement_text="5.7 次条需求",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0024",
            source_page=5,
            source_anchor="Main > 5.1",
            requirement_text="5.1 首条需求",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")

        preview_cache.persist_preview_cache(
            db.get_sor_document(document["id"]) or {},
            _fake_preview_with_reversed_req_codes(),
        )
        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=True)

        self.assertEqual([row["req_code"] for row in workspace["requirements"]], ["REQ0024", "REQ0023"])
        self.assertEqual([row["display_req_code"] for row in workspace["requirements"]], ["REQ0001", "REQ0002"])
        self.assertEqual([row["display_sort_index"] for row in workspace["requirements"]], [1, 2])
        self.assertEqual([row["source_order"] for row in workspace["requirements"]], [1, 2])
        self.assertEqual(workspace["summary"]["order_source"], "review_overlay")
        self.assertEqual(workspace["summary"]["order_mismatch_count"], 0)

    def test_workspace_matrix_rows_prefer_current_review_text_over_stale_persisted_text(self) -> None:
        project, document = self._create_project_with_document("-matrix-preview")
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 1",
            requirement_text="旧的错误内容",
            raw_text="旧的错误内容",
            display_text="旧的错误内容",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")

        preview = {
            "summary": {"dedupe_version": "heading_requirement_v1"},
            "sections": [],
            "complete_requirement_entries": [
                {
                    "entry_id": "REQ0001",
                    "req_code": "REQ0001",
                    "entry_kind": "REQUIREMENT",
                    "display_text": "新的正确内容",
                    "raw_text": "新的正确内容",
                    "requirement_text": "新的正确内容",
                    "source_page": 1,
                    "source_page_end": 1,
                    "source_anchor": "Page 1 | Section 1",
                    "parent_section_code": "1",
                    "parent_section_title": "概述",
                    "parent_section_label": "1 概述",
                }
            ],
            "complete_requirement_entries_raw": [],
        }

        workspace = main._build_document_workspace(
            project=project,
            document=db.get_sor_document(document["id"]) or {},
            requirements=db.list_requirements(project["id"], document["id"]),
            knowledge_docs_count=0,
            preview=preview,
        )

        self.assertEqual(len(workspace["requirements"]), 1)
        self.assertEqual(workspace["requirements"][0]["raw_text"], "新的正确内容")
        self.assertEqual(workspace["requirements"][0]["req_code"], "REQ0001")

    def test_workspace_exposes_table_refs_and_snapshot_urls(self) -> None:
        project, document = self._create_project_with_document("-table-assets")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview_with_table_markdown()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))
            workspace = main.get_document_workspace(project["id"], document["id"], include_preview=True)

        requirement = workspace["requirements"][0]
        review_entry = workspace["review_entries"][0]
        self.assertTrue(requirement["has_table_asset"])
        self.assertEqual(requirement["table_render_mode"], "structured")
        self.assertEqual(len(requirement["table_refs"]), 1)
        self.assertEqual(requirement["table_refs"][0]["table_id"], review_entry["table_refs"][0]["table_id"])
        self.assertEqual(requirement["table_refs"][0]["row_range"], [1, 2])
        self.assertTrue(requirement["table_refs"][0]["slice_snapshot_url"].endswith("/snapshot"))
        self.assertEqual(
            [block["kind"] for block in requirement["content_blocks"]],
            ["paragraph", "table"],
        )

    def test_table_asset_api_returns_asset_and_snapshot(self) -> None:
        project, document = self._create_project_with_document("-table-api")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview_with_table_markdown()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=False)
        table_id = workspace["requirements"][0]["table_refs"][0]["table_id"]
        asset = main.get_document_table_asset(project["id"], document["id"], table_id)
        snapshot = main.get_document_table_asset_snapshot(project["id"], document["id"], table_id)

        self.assertEqual(asset["table_id"], table_id)
        self.assertEqual(asset["headers"], ["Item", "Condition"])
        self.assertEqual(asset["rows"], [["Voltage", "12V"], ["Current", "3A"]])
        self.assertTrue(asset["snapshot_url"].endswith("/snapshot"))
        self.assertTrue(Path(snapshot.path).exists())

    def test_exporter_uses_table_refs_without_markdown_roundtrip(self) -> None:
        _, file_path = export_compliance_matrix(
            project_name="Workspace Test",
            customer_name="Customer",
            requirements=[
                {
                    "display_req_code": "REQ0100",
                    "req_code": "REQ0100",
                    "parent_section_label": "9.1 Asset Render",
                    "raw_text": "试验条件如下：",
                    "content_blocks": [
                        {"kind": "paragraph", "text": "试验条件如下："},
                        {"kind": "table", "table_id": "tbl_asset_render"},
                    ],
                    "table_refs": [
                        {
                            "table_id": "tbl_asset_render",
                            "row_range": [1, 2],
                            "inherited_headers": ["Item", "Condition"],
                            "slice_rows": [["Voltage", "12V"], ["Current", "3A"]],
                            "slice_render_mode": "structured",
                            "slice_ai_projection": "table_id: tbl_asset_render",
                        }
                    ],
                    "has_table_asset": True,
                    "table_render_mode": "structured",
                    "final_decision": "ACCEPT",
                    "engineer_reason": "Asset render",
                }
            ],
            export_dir=self.tmp_path / "exports",
        )

        workbook = load_workbook(file_path)
        sheet = workbook["ComplianceMatrix"]
        meta = self._version_meta_values(workbook)

        self.assertEqual(sheet["C4"].value, "试验条件如下：")
        self.assertIsNone(sheet["C5"].value)
        self.assertEqual(len(sheet._images), 1)
        self.assertEqual(meta["TableImageCount"], 1)

    def test_export_requires_locked_extraction(self) -> None:
        project, document = self._create_project_with_document("-export-lock")
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 1",
            requirement_text="需要先锁定条目集后才能导出。",
            raw_text="需要先锁定条目集后才能导出。",
            display_text="需要先锁定条目集后才能导出。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")

        with self.assertRaises(HTTPException) as export_exc:
            main.export_project(project["id"], main.ExportRequest(document_id=document["id"]))

        self.assertEqual(export_exc.exception.status_code, 409)
        self.assertIn("locked extraction", str(export_exc.exception.detail))

        main.lock_document_extraction(project["id"], document["id"])
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            result = main.export_project(project["id"], main.ExportRequest(document_id=document["id"]))
        self.assertTrue((self.tmp_path / "exports" / result["file_name"]).exists())

    def test_export_works_without_cached_preview(self) -> None:
        project, document = self._create_project_with_document("-export-no-preview")
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 1",
            requirement_text="无 preview 缓存时仍可导出。",
            raw_text="无 preview 缓存时仍可导出。",
            display_text="无 preview 缓存时仍可导出。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        main.lock_document_extraction(project["id"], document["id"])

        result = main.export_project(project["id"], main.ExportRequest(document_id=document["id"]))
        self.assertTrue((self.tmp_path / "exports" / result["file_name"]).exists())

    def test_export_translates_database_lock_to_retryable_http_error(self) -> None:
        project, document = self._create_project_with_document("-export-busy")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))
            main.lock_document_extraction(project["id"], document["id"])
            with patch.object(db, "create_export_job", side_effect=sqlite3.OperationalError("database is locked")):
                with self.assertRaises(HTTPException) as export_exc:
                    main.export_project(project["id"], main.ExportRequest(document_id=document["id"]))

        self.assertEqual(export_exc.exception.status_code, 503)
        self.assertIn("busy", str(export_exc.exception.detail).lower())

    def test_exporter_renders_markdown_tables_as_images(self) -> None:
        file_name, file_path = export_compliance_matrix(
            project_name="Workspace Test",
            customer_name="Customer",
            requirements=[
                {
                    "display_req_code": "REQ0001",
                    "req_code": "REQ0001",
                    "parent_section_label": "5.1 Test Setup",
                    "raw_text": (
                        "试验条件如下：\n\n"
                        "| Item | Condition |\n"
                        "| --- | --- |\n"
                        "| Voltage | 12V |\n"
                        "| Current | 3A |"
                    ),
                    "final_decision": "ACCEPT",
                    "engineer_reason": "Reviewed",
                }
            ],
            export_dir=self.tmp_path / "exports",
        )

        self.assertTrue(file_name.endswith(".xlsx"))
        workbook = load_workbook(file_path)
        sheet = workbook["ComplianceMatrix"]
        meta = self._version_meta_values(workbook)

        self.assertEqual(sheet["A4"].value, "REQ0001")
        self.assertEqual(sheet["C4"].value, "试验条件如下：")
        self.assertIsNone(sheet["C5"].value)
        self.assertEqual(sheet["D4"].value, "ACCEPT")
        self.assertEqual(sheet["E4"].value, "Reviewed")
        self.assertEqual(len(sheet._images), 1)
        self.assertEqual(sheet._images[0].anchor._from.col, 2)
        self.assertEqual(sheet._images[0].anchor._from.row, 4)
        merged_ranges = {str(item) for item in sheet.merged_cells.ranges}
        self.assertNotIn("C3:D3", merged_ranges)
        self.assertEqual(meta["TableImageCount"], 1)
        self.assertEqual(meta["TableFallbackTextCount"], 0)

    def test_exporter_preserves_paragraph_table_paragraph_order(self) -> None:
        _, file_path = export_compliance_matrix(
            project_name="Workspace Test",
            customer_name="Customer",
            requirements=[
                {
                    "display_req_code": "REQ0002",
                    "req_code": "REQ0002",
                    "parent_section_label": "5.2 Sequence",
                    "raw_text": (
                        "第一段说明。\n\n"
                        "| Step | Value |\n"
                        "| --- | --- |\n"
                        "| Torque | 25Nm |\n"
                        "| Time | 30s |\n\n"
                        "第二段说明。"
                    ),
                    "final_decision": "CHECK",
                    "engineer_reason": "Ordered",
                }
            ],
            export_dir=self.tmp_path / "exports",
        )

        workbook = load_workbook(file_path)
        sheet = workbook["ComplianceMatrix"]

        self.assertEqual(sheet["A4"].value, "REQ0002")
        self.assertEqual(sheet["C4"].value, "第一段说明。")
        self.assertIsNone(sheet["C5"].value)
        self.assertEqual(sheet["C6"].value, "第二段说明。")
        self.assertEqual(len(sheet._images), 1)
        self.assertEqual(sheet._images[0].anchor._from.col, 2)
        self.assertEqual(sheet._images[0].anchor._from.row, 4)
        merged_ranges = {str(item) for item in sheet.merged_cells.ranges}
        self.assertIn("A4:A6", merged_ranges)
        self.assertIn("B4:B6", merged_ranges)
        self.assertIn("D4:D6", merged_ranges)
        self.assertIn("E4:E6", merged_ranges)

    def test_exporter_invalid_markdown_table_falls_back_to_text(self) -> None:
        _, file_path = export_compliance_matrix(
            project_name="Workspace Test",
            customer_name="Customer",
            requirements=[
                {
                    "display_req_code": "REQ0003",
                    "req_code": "REQ0003",
                    "parent_section_label": "5.3 Fallback",
                    "raw_text": "| Item | Condition |\n| --- | --- |\n| Too | Many | Cells |",
                    "final_decision": "REVIEW",
                    "engineer_reason": "Fallback",
                }
            ],
            export_dir=self.tmp_path / "exports",
        )

        workbook = load_workbook(file_path)
        sheet = workbook["ComplianceMatrix"]
        meta = self._version_meta_values(workbook)

        self.assertEqual(len(sheet._images), 0)
        self.assertEqual(
            sheet["C4"].value,
            "| Item | Condition |\n| --- | --- |\n| Too | Many | Cells |",
        )
        self.assertEqual(meta["TableImageCount"], 0)
        self.assertEqual(meta["TableFallbackTextCount"], 1)

    def test_exporter_renders_pipe_tables_without_separator_as_images(self) -> None:
        _, file_path = export_compliance_matrix(
            project_name="Workspace Test",
            customer_name="Customer",
            requirements=[
                {
                    "display_req_code": "REQ0004",
                    "req_code": "REQ0004",
                    "parent_section_label": "3.2.2 产品详细描述",
                    "raw_text": (
                        "1）前减振器布置要求\n"
                        "|序号|项目|要求|\n"
                        "|1|基本尺寸|活塞桶直径Φ32-48mm|\n"
                        "|2|电磁阀|前后电磁阀处于同一高度|\n"
                        "表格后说明。"
                    ),
                    "final_decision": "ACCEPT",
                    "engineer_reason": "Pipe table",
                }
            ],
            export_dir=self.tmp_path / "exports",
        )

        workbook = load_workbook(file_path)
        sheet = workbook["ComplianceMatrix"]
        meta = self._version_meta_values(workbook)

        self.assertEqual(sheet["C4"].value, "1）前减振器布置要求")
        self.assertIsNone(sheet["C5"].value)
        self.assertEqual(sheet["C6"].value, "表格后说明。")
        self.assertEqual(len(sheet._images), 1)
        self.assertEqual(sheet._images[0].anchor._from.col, 2)
        self.assertEqual(sheet._images[0].anchor._from.row, 4)
        self.assertEqual(meta["TableImageCount"], 1)
        self.assertEqual(meta["TableFallbackTextCount"], 0)

    def test_exporter_keeps_multiple_table_images_in_requirement_order(self) -> None:
        _, file_path = export_compliance_matrix(
            project_name="Workspace Test",
            customer_name="Customer",
            requirements=[
                {
                    "display_req_code": "REQ0005",
                    "req_code": "REQ0005",
                    "parent_section_label": "6.1 多表顺序",
                    "raw_text": (
                        "第一段说明。\n"
                        "|项|值|\n"
                        "|A|1|\n"
                        "|B|2|\n"
                        "中间说明。\n"
                        "|项|值|\n"
                        "|C|3|\n"
                        "|D|4|\n"
                        "最后说明。"
                    ),
                    "final_decision": "ACCEPT",
                    "engineer_reason": "Multi table",
                }
            ],
            export_dir=self.tmp_path / "exports",
        )

        workbook = load_workbook(file_path)
        sheet = workbook["ComplianceMatrix"]

        self.assertEqual(sheet.max_column, 5)
        self.assertEqual(sheet["C4"].value, "第一段说明。")
        self.assertEqual(sheet["C6"].value, "中间说明。")
        self.assertEqual(sheet["C8"].value, "最后说明。")
        self.assertEqual(len(sheet._images), 2)
        self.assertEqual(sheet._images[0].anchor._from.col, 2)
        self.assertEqual(sheet._images[0].anchor._from.row, 4)
        self.assertEqual(sheet._images[1].anchor._from.col, 2)
        self.assertEqual(sheet._images[1].anchor._from.row, 6)


if __name__ == "__main__":
    unittest.main()
