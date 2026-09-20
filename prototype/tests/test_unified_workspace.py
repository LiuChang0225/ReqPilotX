from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import asyncio
import io
import json
import os
import tempfile
import time
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import patch

import fitz
from app.application.errors import ApplicationError as HTTPException
from fastapi.testclient import TestClient
from PIL import Image as PILImage
from starlette.datastructures import UploadFile

from app import db
from app.domain.extraction.requirement_tree import RequirementTreeState
from app.domain.requirement_matrix import (
    MatrixAiAssessmentV1,
    MatrixAnalysisEligibility,
    MatrixAnalysisStatus,
    MatrixCountsV1,
    MatrixManualDecisionV1,
    MatrixNodeDisposition,
    MatrixNodeKind,
    MatrixNodeStateV1,
    MatrixNodeVisibility,
    MatrixSnapshotState,
    RequirementMatrixSnapshotV1,
)
from app.api.pages import extraction_review_page
if __package__:
    from .application_under_test import main
else:
    from application_under_test import main
from app.main import app as http_app

import app.adapters.persistence.assessment_results as assessment_results
from app.services.complex_table_mode import _row_shadow_signature
from app.services import exporter, extraction_preview, preview_cache, table_assets, extractor
from app.services.ai_copilot import AICopilotConfig, clear_runtime_ai_provider_overrides


main.app = http_app


class ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self._alive = False

    def start(self) -> None:
        self._alive = True
        try:
            if self._target:
                self._target(*self._args, **self._kwargs)
        finally:
            self._alive = False

    def is_alive(self) -> bool:
        return self._alive


def _seed_human_decision(
    requirement_id: str,
    final_decision: str,
    engineer_reason: str,
) -> dict:
    if db.get_assessment(requirement_id) is None:
        db.upsert_assessment(
            requirement_id,
            "test-fixture",
            "test-fixture",
            "NEED_CLARIFICATION",
            "Fixture assessment.",
            0.0,
            "manual",
            "none",
            "REQUIREMENT",
            "",
            "",
            "HIGH",
            "",
            "test-fixture",
            "test-fixture",
        )
    with db.get_conn() as conn:
        conn.execute(
            """
            UPDATE assessments
            SET final_decision = ?, engineer_reason = ?
            WHERE requirement_id = ?
            """,
            (final_decision, engineer_reason, requirement_id),
        )
    return db.get_assessment(requirement_id) or {}


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


def _fake_preview_with_reused_req_codes() -> dict:
    return {
        "summary": {"dedupe_version": "heading_requirement_v1"},
        "sections": [],
        "complete_requirement_entries": [
            {
                "entry_id": "REQ0001",
                "req_code": "REQ0001",
                "entry_kind": "REQUIREMENT",
                "display_text": "5.2 第二条需求",
                "raw_text": "5.2 第二条需求",
                "requirement_text": "5.2 第二条需求",
                "source_page": 5,
                "source_page_end": 5,
                "source_anchor": "Main > 5.2",
                "parent_section_code": "5.2",
                "parent_section_title": "第二条需求",
                "parent_section_label": "5.2 第二条需求",
            },
            {
                "entry_id": "REQ0999",
                "req_code": "REQ0999",
                "entry_kind": "REQUIREMENT",
                "display_text": "5.1 第一条需求",
                "raw_text": "5.1 第一条需求",
                "requirement_text": "5.1 第一条需求",
                "source_page": 5,
                "source_page_end": 5,
                "source_anchor": "Main > 5.1",
                "parent_section_code": "5.1",
                "parent_section_title": "第一条需求",
                "parent_section_label": "5.1 第一条需求",
            },
        ],
        "complete_requirement_entries_raw": [],
    }


def _fake_preview_with_same_code_local_clause_in_section_six() -> dict:
    section_six_title = "投标说明"
    clause_titles = {
        1: "技术标书为简体中文",
        2: "技术标书须包含完整技术方案",
        3: "技术方案需包含关键器件选型",
        4: "技术方案需包含测试验证计划",
        5: "技术方案需包含质量控制方案",
        6: "技术标书中应包含生产工艺部分的描述，针对技术标书中提出的技术方案，论证工艺可行性",
        7: "技术标书需说明量产能力",
        8: "技术标书需说明项目进度计划",
        9: "技术标书需说明交流文件要求",
        10: "商务报价",
    }

    def _heading_row() -> dict:
        return {
            "entry_id": "H-6",
            "req_code": "",
            "entry_kind": "HEADING",
            "section_code": "6",
            "section_key": "6",
            "section_instance_key": "6",
            "section_title": section_six_title,
            "section_label": "",
            "owner_section_code": "6",
            "owner_section_title": section_six_title,
            "owner_section_label": "6",
            "parent_section_code": "",
            "parent_section_title": "",
            "parent_section_label": "",
            "structural_parent_section_code": "",
            "structural_parent_section_key": "",
            "structural_parent_section_title": "",
            "structural_parent_section_label": "",
            "display_text": "6 投标说明",
            "raw_text": "6 投标说明",
            "requirement_text": "6 投标说明",
            "source_page": 9,
            "source_page_end": 9,
            "source_anchor": "Page 9 | Section 6 投标说明",
            "text_relation": "exact_match",
            "is_deduped": True,
            "merged_from_req_codes": [],
            "merged_from_entry_ids": [],
        }

    def _requirement_row(number: int) -> dict:
        title = clause_titles[number]
        text = f"{number}）{title}"
        return {
            "entry_id": f"REQ06{number:02d}",
            "req_code": f"REQ06{number:02d}",
            "entry_kind": "REQUIREMENT",
            "section_code": str(number),
            "section_key": f"6::{number}",
            "section_instance_key": f"6::{number}",
            "section_title": title,
            "section_label": f"{number}）",
            "owner_section_code": str(number),
            "owner_section_title": title,
            "owner_section_label": f"{number}）",
            "parent_section_code": str(number),
            "parent_section_title": title,
            "parent_section_label": f"{number} {title}",
            "structural_parent_section_code": "6",
            "structural_parent_section_key": "6",
            "structural_parent_section_title": section_six_title,
            "structural_parent_section_label": "6",
            "display_text": text,
            "raw_text": text,
            "requirement_text": text,
            "source_page": 9,
            "source_page_end": 9,
            "source_anchor": f"Page 9 | Section 6 投标说明 | Clause {number} {title}",
            "requirement_type": "FUNCTIONAL",
            "priority": "MEDIUM",
            "text_relation": "distinct",
            "is_deduped": False,
            "merged_from_req_codes": [],
            "merged_from_entry_ids": [],
        }

    section_tree = [
        {
            "appendix_scope": "",
            "section_code": "6",
            "section_key": "6",
            "section_instance_key": "6",
            "section_title": section_six_title,
            "owner_section_code": "6",
            "owner_section_title": section_six_title,
            "owner_section_label": "6",
            "parent_section_code": "",
            "parent_section_key": "",
            "structural_parent_section_code": "",
            "structural_parent_section_key": "",
            "source_page_start": 9,
            "source_page_end": 9,
            "requirement_count": 10,
            "tree_order": 0,
        }
    ]
    section_tree.extend(
        {
            "appendix_scope": "",
            "section_code": str(number),
            "section_key": f"6::{number}",
            "section_instance_key": f"6::{number}",
            "section_title": clause_titles[number],
            "section_label": f"{number}）",
            "owner_section_code": str(number),
            "owner_section_title": clause_titles[number],
            "owner_section_label": f"{number}）",
            "parent_section_code": "6",
            "parent_section_key": "6",
            "structural_parent_section_code": "6",
            "structural_parent_section_key": "6",
            "source_page_start": 9,
            "source_page_end": 9,
            "requirement_count": 1,
            "tree_order": number,
        }
        for number in range(1, 11)
    )
    return {
        "summary": {"dedupe_version": "heading_requirement_v1"},
        "sections": section_tree,
        "section_tree": section_tree,
        "complete_requirement_entries": [_heading_row(), *[_requirement_row(number) for number in range(1, 11)]],
        "complete_requirement_entries_raw": [_heading_row(), *[_requirement_row(number) for number in range(1, 11)]],
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


def _fake_preview_with_single_row_slice_table() -> dict:
    table_text = (
        "1 外观检查\n"
        "|1|外观检查|-|目视外观检查|表面无划伤|DV/PV|供应商|6件/OTS|1天|"
    )
    return {
        "summary": {"dedupe_version": "heading_requirement_v1"},
        "sections": [],
        "complete_requirement_entries": [
            {
                "entry_id": "REQ0040",
                "req_code": "REQ0040",
                "entry_kind": "REQUIREMENT",
                "display_text": table_text,
                "raw_text": table_text,
                "requirement_text": table_text,
                "source_page": 20,
                "source_page_end": 20,
                "source_anchor": "Appendix SUB1 > 1",
                "parent_section_code": "SUB1.1",
                "parent_section_title": "试验大纲",
                "parent_section_label": "SUB1.1 试验大纲",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
            }
        ],
        "complete_requirement_entries_raw": [],
    }


def _fake_preview_with_complex_table_markdown() -> dict:
    table_text = (
        "5.7 整车用电器与线束匹配评估表\n\n"
        "| Group | Item |\n"
        "| --- | --- |\n"
        "| 测试分组 | Test Groups |\n"
        "| 整车用电器与线束匹配评估表 | 评估项 |"
    )
    return {
        "summary": {"dedupe_version": "heading_requirement_v1"},
        "sections": [],
        "complete_requirement_entries": [
            {
                "entry_id": "REQ0057",
                "req_code": "REQ0057",
                "entry_kind": "REQUIREMENT",
                "display_text": table_text,
                "raw_text": table_text,
                "requirement_text": table_text,
                "source_page": 1,
                "source_page_end": 2,
                "source_anchor": "Main > 5.7",
                "parent_section_code": "5.7",
                "parent_section_title": "整车用电器与线束匹配评估表",
                "parent_section_label": "5.7 整车用电器与线束匹配评估表",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
            }
        ],
        "complete_requirement_entries_raw": [],
    }


def _complex_group_review_row(*, source_snapshot_path: str, shadow_rows: list[dict] | None = None) -> dict:
    shadow_signatures = [_row_shadow_signature(row) for row in (shadow_rows or []) if _row_shadow_signature(row)]
    return {
        "entry_id": "REQ-CX-cx-merged-requirement-matrix-p17-p22-network-security",
        "req_code": "REQ-CX-cx-merged-requirement-matrix-p17-p22-network-security",
        "entry_kind": "REQUIREMENT",
        "display_text": "网络安全\n复杂跨页表分组，内部条目未自动细拆，请结合原文截图人工处理。",
        "raw_text": "网络安全\n复杂跨页表分组，内部条目未自动细拆，请结合原文截图人工处理。",
        "requirement_text": "网络安全\n复杂跨页表分组，内部条目未自动细拆，请结合原文截图人工处理。",
        "source_page": 17,
        "source_page_end": 20,
        "source_anchor": "Complex Table Group cx-merged-requirement-matrix-p17-p22-network-security",
        "parent_section_code": "网络安全",
        "parent_section_title": "网络安全",
        "parent_section_label": "网络安全",
        "requirement_type": "OTHER",
        "priority": "MEDIUM",
        "text_relation": "distinct",
        "is_deduped": False,
        "merged_from_req_codes": ["REQ-CX-cx-merged-requirement-matrix-p17-p22-network-security"],
        "merged_from_entry_ids": ["REQ-CX-cx-merged-requirement-matrix-p17-p22-network-security"],
        "dedupe_version": "heading_requirement_v1",
        "complex_table_mode": True,
        "complex_zone_id": "cx-merged-requirement-matrix-p17-p22",
        "complex_family": "merged_requirement_matrix",
        "complex_role": "group",
        "complex_group_label": "网络安全",
        "complex_split_level": "group",
        "complex_warnings": ["跨页复杂表，包含纵向合并单元格"],
        "complex_row_policy": "exportable_group",
        "table_refs": [
            {
                "table_id": "tbl_complex_group",
                "table_title": "网络安全",
                "source_snapshot_path": source_snapshot_path,
                "slice_snapshot_path": source_snapshot_path,
                "slice_render_mode": "snapshot_only",
                "inherited_headers": [],
                "slice_rows": [],
                "complex_table_mode": True,
                "complex_zone_id": "cx-merged-requirement-matrix-p17-p22",
                "complex_family": "merged_requirement_matrix",
                "complex_split_level": "group",
                "complex_group_label": "网络安全",
                "complex_shadow_signatures": shadow_signatures,
                "export_variant": "source",
                "recommended_export_variant": "source",
                "export_variant_locked": True,
            }
        ],
        "content_blocks": [
            {"kind": "paragraph", "text": "网络安全\n复杂跨页表分组，内部条目未自动细拆，请结合原文截图人工处理。"},
            {"kind": "table", "table_id": "tbl_complex_group"},
        ],
        "has_table_asset": True,
        "table_render_mode": "snapshot_only",
        "keep": True,
    }


class UnifiedWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_runtime_ai_provider_overrides()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.tmp_path / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.tmp_path))
        self.exit_stack.enter_context(patch.object(main, "EXPORTS_DIR", self.tmp_path / "exports"))
        self._matrix_patch_installed = False
        db.init_db()

    def tearDown(self) -> None:
        clear_runtime_ai_provider_overrides()
        self.exit_stack.close()
        db_file = self.tmp_path / "app.db"
        for _ in range(5):
            try:
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{db_file}{suffix}")
                    if sidecar.exists():
                        sidecar.unlink()
                self.tmpdir.cleanup()
                return
            except OSError:
                time.sleep(0.05)
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
        # Most workspace tests exercise downstream review/AI behavior, so their
        # fixture represents a completed extraction preview. Readiness-specific
        # tests explicitly reset this state to NOT_READY.
        document = preview_cache.persist_preview_cache(
            document,
            {
                "summary": {},
                "sections": [],
                "complete_requirement_entries": [],
                "complete_requirement_entries_raw": [],
            },
        ) or document
        return project, document

    def _create_project_with_pdf_document(self, suffix: str = "") -> tuple[dict, dict]:
        pdf_path = self.tmp_path / f"sample{suffix}.pdf"
        pdf = fitz.open()
        for label in ("5.7 整车用电器与线束匹配评估表", "测试分组 / Test Groups"):
            page = pdf.new_page()
            page.insert_text((72, 72), label, fontsize=18)
            page.insert_text((92, 150), f"Source snapshot page: {label}", fontsize=12)
            x_positions = [72, 180, 300, 420, 520]
            y_positions = [180, 225, 270, 315, 360, 405, 450]
            for x in x_positions:
                page.draw_line((x, y_positions[0]), (x, y_positions[-1]), color=(0.2, 0.4, 0.7), width=1)
            for y in y_positions:
                page.draw_line((x_positions[0], y), (x_positions[-1], y), color=(0.2, 0.4, 0.7), width=1)
            headers = ["Group", "Item", "Method", "Req"]
            for idx, header in enumerate(headers):
                page.insert_text((x_positions[idx] + 8, y_positions[0] + 20), header, fontsize=10)
            for row_index in range(1, 6):
                values = [
                    f"G{row_index}",
                    "测试分组" if row_index == 1 else f"条目 {row_index}",
                    "Visual",
                    "Pass",
                ]
                base_y = y_positions[row_index] + 20
                for col_index, value in enumerate(values):
                    page.insert_text((x_positions[col_index] + 8, base_y), value, fontsize=10)
        pdf.save(pdf_path)
        pdf.close()
        project = db.create_project(name=f"Workspace PDF Test{suffix}", customer_name="Customer")
        document = db.add_sor_document(
            project_id=project["id"],
            file_name=f"sample{suffix}.pdf",
            file_type="pdf",
            file_path=str(pdf_path),
            extracted_text="sample pdf",
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

    def _allow_verifiable_tree_derivation(self) -> None:
        """Downstream behavior tests assume the active version owns a verified tree.

        Immutable object persistence and cloning are exercised by the dedicated
        RequirementTreeSnapshotPersistenceTests integration contract.
        """

        snapshot = SimpleNamespace(state=RequirementTreeState.READY, nodes=())

        def inspect_tree(_requirement_set: dict, stored_rows: list[dict]) -> dict:
            included = sum(
                str(row.get("entry_kind") or "REQUIREMENT").upper()
                in {"REQUIREMENT", "HYBRID"}
                for row in stored_rows
            )
            return {
                "requirement_tree_state": "READY",
                "requirement_tree_issue": None,
                "requirement_tree_has_evidence_bundle": True,
                "tree_lock_ready": True,
                "review_candidate_entry_count": len(stored_rows),
                "structure_proxy_count": len(stored_rows),
                "table_record_count": 0,
                "semantic_requirement_count": 0,
                "stored_review_entry_count": len(stored_rows),
                "included_requirement_count": included,
                "reference_count": sum(
                    str(row.get("entry_kind") or "").upper() == "REFERENCE"
                    for row in stored_rows
                ),
                "excluded_count": sum(
                    str(row.get("entry_kind") or "").upper() == "EXCLUDED"
                    for row in stored_rows
                ),
                "count_semantics": (
                    "REVIEW_CANDIDATES_NOT_CONFIRMED_FINAL_REQUIREMENTS"
                ),
                "_business_key": "verified-test-tree",
            }

        self.exit_stack.enter_context(
            patch(
                "app.application.requirements._inspect_requirement_set_tree",
                side_effect=inspect_tree,
            )
        )
        self.exit_stack.enter_context(
            patch(
                "app.application.extraction.requirement_tree_snapshots."
                "load_requirement_set_tree_snapshot",
                return_value=snapshot,
            )
        )
        self.exit_stack.enter_context(
            patch(
                "app.application.extraction.requirement_tree_snapshots."
                "clone_requirement_set_tree_snapshot",
                return_value=snapshot,
            )
        )
        if not self._matrix_patch_installed:
            self.exit_stack.enter_context(
                patch(
                    "app.application.requirement_matrix."
                    "build_requirement_matrix_snapshot",
                    side_effect=self._build_test_requirement_matrix,
                )
            )
            self._matrix_patch_installed = True

    def _build_test_requirement_matrix(
        self,
        project_id: str,
        document_id: str,
        requirement_set_id: str,
        *,
        require_current_runtime: bool = False,
    ) -> RequirementMatrixSnapshotV1:
        del require_current_runtime
        rows = db.list_requirements(
            project_id,
            document_id,
            requirement_set_id=requirement_set_id,
        )
        from app.adapters.persistence.requirement_matrix_states import (
            list_requirement_matrix_node_states,
        )

        persisted_state_by_node_id = {
            str(item.get("node_id") or ""): item
            for item in list_requirement_matrix_node_states(
                project_id=project_id,
                document_id=document_id,
                requirement_set_id=requirement_set_id,
            )
        }

        def source_key(row: dict) -> tuple[tuple[int, ...], str]:
            code = str(row.get("parent_section_code") or "")
            parts = tuple(
                int(part) if part.isdigit() else 999_999
                for part in code.replace("-", ".").split(".")
                if part
            )
            return parts or (999_999,), str(row.get("id") or "")

        ordered_rows = sorted(rows, key=source_key)
        nodes: list[MatrixNodeStateV1] = []
        for index, row in enumerate(ordered_rows):
            requirement_id = str(row.get("id") or "")
            node_id = f"matrix-{requirement_id}"
            persisted_state = persisted_state_by_node_id.get(node_id, {})
            kind = str(row.get("entry_kind") or "REQUIREMENT").upper()
            eligible = kind in {"REQUIREMENT", "HYBRID"}
            visibility = (
                MatrixNodeVisibility.HUMAN_EXCLUDED
                if kind == "EXCLUDED"
                else MatrixNodeVisibility.VISIBLE
            )
            disposition = (
                MatrixNodeDisposition.EXCLUDE
                if visibility is MatrixNodeVisibility.HUMAN_EXCLUDED
                else (
                    MatrixNodeDisposition.ANALYZE
                    if eligible
                    else MatrixNodeDisposition.RETAIN
                )
            )
            persisted_disposition = str(
                persisted_state.get("disposition") or ""
            ).strip().upper()
            if persisted_disposition:
                disposition = MatrixNodeDisposition(persisted_disposition)
                visibility = (
                    MatrixNodeVisibility.HUMAN_EXCLUDED
                    if disposition is MatrixNodeDisposition.EXCLUDE
                    else MatrixNodeVisibility.VISIBLE
                )
            assessment_mode = str(row.get("assessment_mode") or "")
            ai_assessment = (
                MatrixAiAssessmentV1(
                    decision=str(row.get("ai_decision") or ""),
                    compliance_status=str(row.get("compliance_status") or ""),
                    reason=str(row.get("ai_reason") or ""),
                    confidence=(
                        float(row.get("ai_confidence"))
                        if row.get("ai_confidence") is not None
                        else None
                    ),
                    assessment_mode=assessment_mode,
                )
                if assessment_mode and assessment_mode.lower() != "manual"
                else None
            )
            manual_decision = (
                MatrixManualDecisionV1(
                    decision=str(row.get("final_decision") or ""),
                    reason=str(row.get("engineer_reason") or ""),
                    decided_at=str(row.get("assessment_updated_at") or ""),
                )
                if str(row.get("final_decision") or "")
                else None
            )
            status = MatrixAnalysisStatus.NOT_APPLICABLE
            if eligible and disposition is MatrixNodeDisposition.ANALYZE:
                status = (
                    MatrixAnalysisStatus.DECIDED
                    if manual_decision
                    else (
                        MatrixAnalysisStatus.ASSESSED
                        if ai_assessment
                        else MatrixAnalysisStatus.NOT_STARTED
                    )
                )
            display_code = str(row.get("display_req_code") or f"REQ{index + 1:04d}")
            nodes.append(
                MatrixNodeStateV1(
                    node_id=node_id,
                    parent_id=None,
                    depth=0,
                    source_order=index,
                    display_role=(
                        MatrixNodeKind.ATOMIC_REQUIREMENT
                        if eligible
                        else MatrixNodeKind.CONTEXT
                    ),
                    label=str(
                        row.get("raw_text")
                        or row.get("requirement_text")
                        or row.get("display_text")
                        or display_code
                    ),
                    summary=str(
                        row.get("raw_text")
                        or row.get("requirement_text")
                        or row.get("display_text")
                        or ""
                    ),
                    content_blocks=tuple(row.get("content_blocks") or ()),
                    table_refs=tuple(row.get("table_refs") or ()),
                    source_refs=(),
                    page_start=int(row.get("source_page") or 0) or None,
                    requirement_entry_id=requirement_id,
                    requirement_id=requirement_id,
                    requirement_code=display_code,
                    requirement_text=str(
                        row.get("raw_text")
                        or row.get("requirement_text")
                        or row.get("display_text")
                        or ""
                    ),
                    matrix_visibility=visibility,
                    disposition=disposition,
                    analysis_eligibility=(
                        MatrixAnalysisEligibility.ELIGIBLE
                        if eligible
                        else MatrixAnalysisEligibility.NOT_ELIGIBLE
                    ),
                    analysis_status=status,
                    ai_assessment=ai_assessment,
                    manual_decision=manual_decision,
                    compliance_status=str(row.get("compliance_status") or ""),
                    save_revision=int(persisted_state.get("revision") or 0),
                )
            )
        requirement_set = db.get_requirement_set(requirement_set_id) or {}
        return RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id=project_id,
            document_id=document_id,
            requirement_set_id=requirement_set_id,
            requirement_set_status=str(requirement_set.get("status") or "LOCKED"),
            tree_sha256="f" * 64,
            root_node_ids=tuple(node.node_id for node in nodes),
            nodes=tuple(nodes),
            counts=MatrixCountsV1(
                total_nodes=len(nodes),
                visible_nodes=sum(
                    node.matrix_visibility is MatrixNodeVisibility.VISIBLE
                    for node in nodes
                ),
                human_excluded_nodes=sum(
                    node.matrix_visibility is MatrixNodeVisibility.HUMAN_EXCLUDED
                    for node in nodes
                ),
                analyze_nodes=sum(
                    node.disposition is MatrixNodeDisposition.ANALYZE
                    for node in nodes
                ),
                retained_nodes=sum(
                    node.disposition is MatrixNodeDisposition.RETAIN
                    for node in nodes
                ),
                excluded_nodes=sum(
                    node.disposition is MatrixNodeDisposition.EXCLUDE
                    for node in nodes
                ),
                analysis_eligible_nodes=sum(
                    node.analysis_eligibility is MatrixAnalysisEligibility.ELIGIBLE
                    for node in nodes
                ),
                assessed_nodes=sum(
                    node.analysis_status
                    in {
                        MatrixAnalysisStatus.ASSESSED,
                        MatrixAnalysisStatus.DECIDED,
                    }
                    for node in nodes
                ),
                decided_nodes=sum(
                    node.analysis_status is MatrixAnalysisStatus.DECIDED
                    for node in nodes
                ),
            ),
        )
    def _active_requirement_set_id(self, document_id: str) -> str:
        requirement_set = db.get_active_requirement_set(document_id)
        self.assertIsNotNone(requirement_set)
        return str((requirement_set or {}).get("id") or "")

    def _review_entry(
        self,
        document_id: str,
        **payload: object,
    ) -> main.ReviewedRequirementEntry:
        return main.ReviewedRequirementEntry(
            requirement_set_id=self._active_requirement_set_id(document_id),
            **payload,
        )

    def _export_request(
        self,
        document_id: str,
        **payload: object,
    ) -> main.ExportRequest:
        return main.ExportRequest(
            document_id=document_id,
            requirement_set_id=self._active_requirement_set_id(document_id),
            **payload,
        )

    def _ai_start_payload(
        self,
        document_id: str,
        *,
        profile_id: str = "",
    ) -> dict[str, str]:
        payload = {
            "requirement_set_id": self._active_requirement_set_id(document_id),
        }
        if profile_id:
            payload["profile_id"] = profile_id
        return payload

    def _lock_active_requirement_set(
        self,
        project_id: str,
        document_id: str,
        *,
        waiver_reason: str = "",
    ) -> dict:
        self._allow_verifiable_tree_derivation()
        return main.lock_document_extraction(
            project_id,
            document_id,
            main.LockExtractionRequest(
                requirement_set_id=self._active_requirement_set_id(document_id),
                waiver_reason=waiver_reason,
            ),
        )

    def test_extraction_review_serves_read_only_source_workspace(self) -> None:
        response = extraction_review_page()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.path).name, "index.html")
        self.assertIn("no-store", response.headers["cache-control"])

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
        for document in (doc_a, doc_b):
            requirement_set = db.create_requirement_set(
                project["id"],
                document["id"],
            )
            db.activate_requirement_set(
                project["id"],
                document["id"],
                requirement_set["id"],
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
            self._review_entry(doc_a["id"], **payload),
        )
        res_b = main.save_review_entry(
            project["id"],
            doc_b["id"],
            self._review_entry(doc_b["id"], **payload),
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

    def test_removed_preview_routes_are_not_registered(self) -> None:
        route_paths = {
            str(getattr(route, "path", ""))
            for route in main.app.routes
        }
        root = "/api/projects/{project_id}/sor-documents/{document_id}"
        self.assertNotIn(f"{root}/extraction-preview", route_paths)
        self.assertNotIn(f"{root}/refresh-preview", route_paths)
        self.assertNotIn(f"{root}/extraction/preview", route_paths)
        self.assertNotIn("/api/projects/{project_id}/analyze", route_paths)
        self.assertNotIn(f"{root}/run-ai-analysis", route_paths)

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
            self._review_entry(
                document["id"],
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
        self.assertEqual(insert_payload["document"]["requirement_set_version"], 1)
        self.assertEqual(len(insert_payload["requirements"]), 2)

        update_heading = main.save_review_entry(
            project["id"],
            document["id"],
            self._review_entry(
                document["id"],
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
        self.assertEqual(update_payload["document"]["requirement_set_version"], 1)

        remove_heading = main.save_review_entry(
            project["id"],
            document["id"],
            self._review_entry(
                document["id"],
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
        self.assertEqual(remove_payload["document"]["requirement_set_version"], 1)
        self.assertEqual(len(remove_payload["requirements"]), 1)

    def test_review_save_uses_cached_preview_and_does_not_rebuild(self) -> None:
        project, document = self._create_project_with_document("-review-cache")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        with patch.object(main, "build_extraction_preview", side_effect=AssertionError("review save rebuilt preview")):
            saved = main.save_review_entry(
                project["id"],
                document["id"],
                self._review_entry(
                    document["id"],
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
        first_set = db.get_active_requirement_set(document["id"])
        first_requirement = db.list_requirements(project["id"], document["id"])[0]
        _seed_human_decision(first_requirement["id"], "ACCEPT", "Historical decision")

        locked = self._lock_active_requirement_set(project["id"], document["id"])
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
        self.assertEqual(str(current.get("ai_status") or ""), "NOT_RUN")
        self.assertEqual(len(db.list_requirement_sets(project["id"], document["id"])), 2)
        historical = db.list_requirements(
            project["id"], document["id"], requirement_set_id=first_set["id"]
        )
        self.assertEqual(historical[0]["id"], first_requirement["id"])
        self.assertEqual(historical[0]["final_decision"], "ACCEPT")

    def test_locked_requirement_set_rejects_review_edits_without_silent_fork(
        self,
    ) -> None:
        project, document = self._create_project_with_document("-locked-fork")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))
        self._lock_active_requirement_set(project["id"], document["id"])

        def payload(text: str) -> main.ReviewedRequirementEntry:
            return self._review_entry(
                document["id"],
                entry_id="REQ0001",
                req_code="REQ0001",
                entry_kind="REQUIREMENT",
                source_page=1,
                source_anchor="Main > 1",
                requirement_text=text,
                display_text=text,
                raw_text=text,
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                parent_section_code="1",
                parent_section_title="概述",
                parent_section_label="1 概述",
                keep=True,
            )

        with self.assertRaises(HTTPException) as blocked:
            main.save_review_entry(
                project["id"],
                document["id"],
                payload("锁定后修订"),
            )
        sets = db.list_requirement_sets(project["id"], document["id"])

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(blocked.exception.detail["code"], "requirement_set_locked")
        self.assertEqual(
            [(item["version"], item["status"]) for item in sets],
            [(1, "LOCKED")],
        )
        self.assertEqual(
            db.list_requirements(project["id"], document["id"])[0]["display_text"],
            "供应商需要满足总成试验要求。",
        )

    def test_assessment_compliance_contract_round_trips_without_overwriting_human_decision(self) -> None:
        project, document = self._create_project_with_document(
            "-assessment-compliance-contract"
        )
        requirement = db.insert_requirement(
            project["id"],
            document["id"],
            "REQ0001",
            1,
            "Page 1 | Section 1",
            "系统应支持安全诊断。",
            "FUNCTIONAL",
            "HIGH",
        )
        gap = {
            "schema_version": "assessment-information-gap/v1",
            "found_evidence": [],
            "missing_items": [
                {
                    "item": "诊断验证记录",
                    "blocking_reason": "当前没有直接验证结果。",
                    "required_input": "请补充验证报告。",
                }
            ],
            "conflicts": [],
            "clarification_questions": ["请补充验证报告。"],
        }
        sub_judgments = [
            {
                "sub_judgment_id": "sj-1",
                "source_ranges": ["atomic_source_text"],
                "statement": "系统应支持安全诊断。",
                "compliance_status": "INSUFFICIENT_INFORMATION",
                "reason": "缺少验证记录。",
                "retrieval_evidence_ids": ["stable-chunk-1"],
                "structured_information_gap": gap,
            }
        ]
        db.upsert_assessment(
            requirement["id"],
            "profile",
            "provider",
            "NEED_CLARIFICATION",
            "缺少直接证据。",
            0.4,
            "rag",
            "vector",
            "REQUIREMENT",
            "basis",
            "系统应支持安全诊断。",
            "HIGH",
            "请补充验证报告。",
            "model",
            "prompt-v3",
            compliance_status="INSUFFICIENT_INFORMATION",
            evidence_findings=[
                {
                    "retrieval_evidence_id": "stable-chunk-1",
                    "finding": "CONTEXT_ONLY",
                    "rationale": "只提供上下文。",
                }
            ],
            structured_information_gap=gap,
            sub_judgments=sub_judgments,
        )
        db.replace_evidences(
            requirement["id"],
            [
                {
                    "source_doc_id": "knowledge-1",
                    "source_doc_name": "knowledge.txt",
                    "source_doc_version": "v1",
                    "source_location": "Chunk 1",
                    "excerpt": "安全诊断章节。",
                    "relevance": 0.71,
                    "retrieval_evidence_id": "stable-chunk-1",
                    "content_hash": "a" * 64,
                    "evidence_contract_version": "evidence-v1",
                    "content_kind": "paragraph",
                    "doc_type": "REFERENCE",
                    "status": "ACTIVE",
                }
            ],
        )
        _seed_human_decision(
            requirement["id"],
            "REJECT",
            "人工确认当前版本不接受。",
        )
        db.upsert_assessment(
            requirement["id"],
            "profile",
            "provider",
            "NEED_CLARIFICATION",
            "仍缺少直接证据。",
            0.45,
            "rag",
            "vector",
            "REQUIREMENT",
            "basis",
            "系统应支持安全诊断。",
            "HIGH",
            "请补充验证报告。",
            "model",
            "prompt-v3",
            compliance_status="INSUFFICIENT_INFORMATION",
            structured_information_gap=gap,
            sub_judgments=sub_judgments,
        )

        persisted = db.get_requirement(requirement["id"])
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(
            persisted["compliance_status"],
            "INSUFFICIENT_INFORMATION",
        )
        self.assertEqual(
            persisted["structured_information_gap"],
            gap,
        )
        self.assertEqual(persisted["sub_judgments"], sub_judgments)
        self.assertEqual(persisted["final_decision"], "REJECT")
        self.assertEqual(
            persisted["engineer_reason"],
            "人工确认当前版本不接受。",
        )
        self.assertNotEqual(
            persisted["evidences"][0]["id"],
            persisted["evidences"][0]["retrieval_evidence_id"],
        )
        self.assertEqual(
            persisted["evidences"][0]["retrieval_evidence_id"],
            "stable-chunk-1",
        )
        self.assertEqual(
            persisted["evidences"][0]["evidence_metadata"]["status"],
            "ACTIVE",
        )

    def test_locked_set_keeps_human_decision_on_same_version_and_fork_exposes_references(
        self,
    ) -> None:
        project, document = self._create_project_with_document("-stale-reference")
        requirement = db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1 | Section 1",
            "需要满足验证要求。", "FUNCTIONAL", "HIGH",
        )
        db.upsert_assessment(
            requirement["id"], "profile", "provider", "ACCEPT", "AI basis", 0.91,
            "rag", "vector", "REQUIREMENT", "basis", "normalized", "LOW", "",
            "model", "prompt-v1",
        )
        db.replace_evidences(
            requirement["id"],
            [{
                "source_doc_id": "knowledge-1",
                "source_doc_name": "knowledge.txt",
                "source_doc_version": "v1",
                "source_location": "line 3",
                "excerpt": "reference excerpt",
                "relevance": 0.9,
            }],
        )
        _seed_human_decision(requirement["id"], "REJECT", "Human conclusion")
        db.add_assessment_run(
            requirement["id"], profile_id="profile", provider="provider", mode="rag",
            retrieval_backend="vector", model_name="model", prompt_version="prompt-v1",
            input_payload_json="{}", output_payload_json="{}", validation_status="VALID",
            fallback_reason="",
        )
        ai_job = db.create_ai_analysis_job(
            project["id"], document["id"], total_requirements=1, status="PENDING"
        )
        db.update_ai_analysis_job(
            ai_job["id"], status="COMPLETED", finished_at="2026-07-18T00:00:00+00:00"
        )
        db.lock_sor_document_extraction(document["id"])

        self.assertEqual(
            db.get_assessment(requirement["id"])["final_decision"],
            "REJECT",
        )
        self.assertEqual(
            db.get_active_requirement_set(document["id"])["id"],
            requirement["requirement_set_id"],
        )
        with self.assertRaises(PermissionError):
            db.delete_requirement(requirement["id"])

        draft = db.fork_requirement_set_for_edit(project["id"], document["id"])
        copied = db.get_requirement_in_set_by_prior(draft["id"], requirement["id"])
        self.assertIsNotNone(copied)
        assert copied is not None
        self.assertIsNone(copied.get("final_decision"))
        self.assertEqual(copied["decision_reference"]["final_decision"], "REJECT")
        self.assertEqual(copied["decision_reference"]["confirmation_status"], "PENDING")
        self.assertEqual(copied["ai_decision"], "ACCEPT")
        self.assertEqual(copied["assessment_reference_status"], "STALE_REFERENCE")
        self.assertEqual(copied["evidences"][0]["reference_status"], "STALE_REFERENCE")
        self.assertEqual(db.list_assessment_runs(copied["id"]), [])
        self.assertEqual(len(db.list_assessment_runs(requirement["id"])), 1)
        sets = db.list_requirement_sets(project["id"], document["id"])
        old_set = next(item for item in sets if item["version"] == 1)
        new_set = next(item for item in sets if item["version"] == 2)
        self.assertEqual(old_set["latest_ai_run_id"], ai_job["id"])
        self.assertEqual(old_set["latest_ai_run_status"], "COMPLETED")
        self.assertFalse(new_set.get("latest_ai_run_id"))
        db.lock_sor_document_extraction(
            document["id"],
            expected_requirement_set_id=draft["id"],
        )
        assessment_results.publish_serving_results(
            [(
                copied["id"],
                {
                "assessment": {
                    "ai_decision": "REJECT", "ai_reason": "fresh result", "ai_confidence": 0.88,
                    "assessment_mode": "rag", "analysis_label": "REQUIREMENT",
                    "analysis_basis": "fresh", "normalized_requirement": "normalized",
                    "risk_level": "HIGH", "clarification_question": "", "model_name": "new-model",
                    "prompt_version": "v2",
                },
                "evidences": [],
                "runs": [],
                },
            )],
            document_id=document["id"],
            requirement_set_id=draft["id"],
            requirement_set_version=int(draft["version"]),
            knowledge_base_version=int(
                (db.get_project(project["id"]) or {})[
                    "knowledge_base_version"
                ]
            ),
            profile_id="profile", provider="provider", retrieval_backend="vector",
        )
        refreshed = db.get_requirement(copied["id"])
        self.assertEqual(refreshed["assessment_reference_status"], "CURRENT")
        self.assertEqual(refreshed["assessment_source_requirement_id"], "")
        self.assertEqual(refreshed["assessment_result_origin"], "NATIVE")
        self.assertEqual(refreshed["assessment_source_assessment_id"], "")
        self.assertEqual(refreshed["assessment_impact_receipt_id"], "")
        self.assertEqual(refreshed["ai_decision"], "REJECT")

    def test_evidence_context_resolves_source_location_and_file(self) -> None:
        project, document = self._create_project_with_document("-evidence-context")
        knowledge_path = self.tmp_path / "evidence-source.txt"
        source_text = "line one\nneedle evidence text\nline three\nneedle evidence text\nline five"
        knowledge_path.write_text(source_text, encoding="utf-8")
        knowledge = db.add_knowledge_document(
            project["id"], "evidence-source.txt", "txt", str(knowledge_path),
            source_text, doc_version="v2",
            status="ACTIVE", effective_from="2026-01-01", effective_to="2027-01-01",
        )
        requirement = db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1", "need evidence",
            "FUNCTIONAL", "MEDIUM",
        )
        db.replace_evidences(
            requirement["id"],
            [{
                "source_doc_id": knowledge["id"], "source_doc_name": knowledge["file_name"],
                "source_doc_version": "v2", "source_location": "line 4",
                "excerpt": "needle evidence text", "relevance": 0.95,
            }],
        )
        evidence_payload = db.get_requirement(requirement["id"])["evidences"][0]
        self.assertEqual(evidence_payload["source_doc_status"], "ACTIVE")
        self.assertEqual(evidence_payload["source_effective_from"], "2026-01-01")
        self.assertEqual(evidence_payload["source_effective_to"], "2027-01-01")
        self.assertEqual(evidence_payload["effective_from"], "2026-01-01")
        self.assertEqual(evidence_payload["effective_to"], "2027-01-01")
        evidence_id = evidence_payload["id"]
        context = main.get_evidence_context(evidence_id)
        self.assertEqual(context["source"]["line_number"], 4)
        self.assertEqual(context["source"]["status"], "ACTIVE")
        self.assertEqual(context["location_strategy"], "source_line_exact")
        self.assertEqual(context["location_confidence"], "HIGH")
        self.assertEqual(context["location_candidate_count"], 2)
        self.assertIn("needle evidence text", context["context"])
        self.assertEqual(context["file_url"], f"/api/evidences/{evidence_id}/file")
        response = main.open_evidence_file(evidence_id)
        self.assertEqual(Path(response.path), knowledge_path)

    def test_failed_generation_keeps_active_set_and_history(self) -> None:
        project, document = self._create_project_with_document("-generation-failure")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))
        original_set = db.get_active_requirement_set(document["id"])
        original_requirement = db.list_requirements(project["id"], document["id"])[0]
        _seed_human_decision(original_requirement["id"], "ACCEPT", "Preserve me")

        rows = [
            {"req_code": "REQ1001", "entry_kind": "REQUIREMENT", "display_text": "new one"},
            {"req_code": "REQ1002", "entry_kind": "REQUIREMENT", "display_text": "new two"},
        ]
        original_insert = db.insert_requirement
        call_count = 0

        def fail_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("simulated insert failure")
            return original_insert(*args, **kwargs)

        with patch.object(db, "insert_requirement", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                main._persist_requirements(
                    project_id=project["id"], sor_document_id=document["id"], rows=rows,
                    knowledge_docs=[], workflow_stage="EXTRACTED",
                )

        current_set = db.get_active_requirement_set(document["id"])
        self.assertEqual(current_set["id"], original_set["id"])
        self.assertEqual(len(db.list_requirement_sets(project["id"], document["id"])), 1)
        preserved = db.get_requirement(original_requirement["id"])
        self.assertEqual(preserved["final_decision"], "ACCEPT")

    def test_requirement_set_publish_rolls_back_table_assets_and_activation_together(self) -> None:
        project, document = self._create_project_with_document("-asset-atomic")
        db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1", "old", "FUNCTIONAL", "MEDIUM"
        )
        original_set = db.get_active_requirement_set(document["id"])
        db.replace_document_table_assets(
            project["id"], document["id"], [{"table_id": "old-table", "value": "old"}]
        )
        candidate = db.create_requirement_set(project["id"], document["id"])
        db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1", "new", "FUNCTIONAL", "MEDIUM",
            requirement_set_id=candidate["id"],
        )
        original_replace = db._replace_document_table_assets_in_conn

        def replace_then_fail(conn, **kwargs):
            original_replace(conn, **kwargs)
            raise RuntimeError("simulated publish failure")

        with patch.object(db, "_replace_document_table_assets_in_conn", side_effect=replace_then_fail):
            with self.assertRaises(RuntimeError):
                db.activate_requirement_set(
                    project["id"], document["id"], candidate["id"],
                    table_assets=[{"table_id": "new-table", "value": "new"}],
                )

        self.assertEqual(db.get_active_requirement_set(document["id"])["id"], original_set["id"])
        assets = db.list_document_table_assets(project["id"], document["id"])
        self.assertEqual([asset["table_id"] for asset in assets], ["old-table"])
        self.assertEqual(
            db.list_document_table_assets(
                project["id"], document["id"], requirement_set_id=candidate["id"]
            ),
            [],
        )

    def test_historical_requirement_set_without_tree_stays_read_only_with_ai_projection(self) -> None:
        project, document = self._create_project_with_document("-restore-ai-projection")
        db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1",
            "Original requirement", "FUNCTIONAL", "MEDIUM",
        )
        first_set = db.get_active_requirement_set(document["id"])
        ai_job = db.create_ai_analysis_job(
            project["id"], document["id"], requirement_set_id=first_set["id"],
            total_requirements=1, status="PENDING",
        )
        db.update_ai_analysis_job(
            ai_job["id"], status="COMPLETED", finished_at="2026-07-18T08:00:00+00:00"
        )
        db.update_sor_document_ai_state(
            document["id"], ai_status="READY", ai_assessed_at="2026-07-18T08:00:00+00:00",
            ai_requirement_set_version=first_set["version"], ai_knowledge_base_version=0,
        )

        second_set = db.create_requirement_set(project["id"], document["id"])
        db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1",
            "Replacement requirement", "FUNCTIONAL", "MEDIUM",
            requirement_set_id=second_set["id"],
        )
        db.activate_requirement_set(project["id"], document["id"], second_set["id"])
        self.assertEqual(db.get_sor_document(document["id"])["ai_status"], "NOT_RUN")

        with self.assertRaises(HTTPException) as restore_error:
            main.restore_document_requirement_set(
                project["id"], document["id"], first_set["id"]
            )
        self.assertEqual(restore_error.exception.status_code, 409)
        historical = main.get_document_workspace(
            project["id"],
            document["id"],
            include_preview=False,
            requirement_set_id=first_set["id"],
        )
        self.assertEqual(historical["document"]["ai_status"], "COMPLETED")
        self.assertEqual(
            historical["document"]["ai_assessed_at"],
            "2026-07-18T08:00:00+00:00",
        )
        self.assertEqual(
            historical["document"]["ai_requirement_set_version"],
            first_set["version"],
        )
        self.assertEqual(
            db.get_active_requirement_set(document["id"])["id"],
            second_set["id"],
        )

    def test_requirement_set_switch_and_fork_are_blocked_while_ai_job_is_active(self) -> None:
        project, document = self._create_project_with_document("-set-switch-running-ai")
        db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1",
            "Original requirement", "FUNCTIONAL", "MEDIUM",
        )
        first_set = db.get_active_requirement_set(document["id"])
        second_set = db.create_requirement_set(project["id"], document["id"])
        db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1",
            "Candidate requirement", "FUNCTIONAL", "MEDIUM",
            requirement_set_id=second_set["id"],
        )
        job = db.create_ai_analysis_job(
            project["id"], document["id"], requirement_set_id=first_set["id"],
            total_requirements=1, status="PENDING",
        )

        with self.assertRaises(HTTPException) as activation_error:
            main.activate_document_requirement_set(
                project["id"], document["id"], second_set["id"]
            )
        self.assertEqual(activation_error.exception.status_code, 409)
        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.create_requirement_set(project["id"], document["id"])
        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.fork_requirement_set_for_edit(project["id"], document["id"])
        self.assertEqual(db.get_active_requirement_set(document["id"])["id"], first_set["id"])
        self.assertEqual(db.get_active_ai_analysis_job_for_document(document["id"])["id"], job["id"])

    def test_legacy_requirement_set_migration_preserves_child_rows_and_ids(self) -> None:
        legacy_path = self.tmp_path / "legacy-requirements.db"
        conn = sqlite3.connect(legacy_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, customer_name TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE sor_documents(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, file_name TEXT NOT NULL,
                file_type TEXT NOT NULL, file_path TEXT NOT NULL, extracted_text TEXT NOT NULL,
                created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            CREATE TABLE requirements(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, sor_document_id TEXT NOT NULL,
                req_code TEXT NOT NULL, source_page INTEGER, source_anchor TEXT,
                requirement_text TEXT NOT NULL, requirement_type TEXT NOT NULL, priority TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(sor_document_id, req_code),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(sor_document_id) REFERENCES sor_documents(id) ON DELETE CASCADE
            );
            CREATE TABLE assessments(
                id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL UNIQUE, ai_decision TEXT NOT NULL,
                ai_reason TEXT NOT NULL, ai_confidence REAL NOT NULL, final_decision TEXT,
                engineer_reason TEXT, model_name TEXT NOT NULL, prompt_version TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );
            CREATE TABLE evidences(
                id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL, source_doc_id TEXT NOT NULL,
                source_doc_name TEXT NOT NULL, source_location TEXT NOT NULL, excerpt TEXT NOT NULL,
                relevance REAL NOT NULL,
                FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );
            CREATE TABLE assessment_runs(
                id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL, mode TEXT NOT NULL,
                retrieval_backend TEXT NOT NULL, model_name TEXT NOT NULL, prompt_version TEXT NOT NULL,
                input_payload_json TEXT NOT NULL, output_payload_json TEXT NOT NULL,
                validation_status TEXT NOT NULL, fallback_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );
            CREATE TABLE review_logs(
                id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL, action TEXT NOT NULL,
                old_value TEXT, new_value TEXT, comment TEXT, operator TEXT NOT NULL, operated_at TEXT NOT NULL,
                FOREIGN KEY(requirement_id) REFERENCES requirements(id) ON DELETE CASCADE
            );
            CREATE TABLE ai_analysis_jobs(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, sor_document_id TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
            );
            INSERT INTO projects VALUES ('project-1', 'Legacy', 'Customer', 'created');
            INSERT INTO sor_documents VALUES ('document-1', 'project-1', 'legacy.txt', 'txt', '/tmp/legacy', 'source', 'created');
            INSERT INTO requirements VALUES (
                'requirement-1', 'project-1', 'document-1', 'REQ0001', 1, 'Page 1',
                'Legacy requirement', 'FUNCTIONAL', 'HIGH', 'IN_REVIEW', 'created', 'updated'
            );
            INSERT INTO assessments VALUES (
                'assessment-1', 'requirement-1', 'ACCEPT', 'AI reason', 0.8,
                'REJECT', 'Human reason', 'model', 'prompt', 'updated'
            );
            INSERT INTO evidences VALUES (
                'evidence-1', 'requirement-1', 'knowledge-1', 'knowledge.txt', 'line 1', 'excerpt', 0.9
            );
            INSERT INTO assessment_runs VALUES (
                'run-1', 'requirement-1', 'rag', 'vector', 'model', 'prompt', '{}', '{}', 'VALID', '', 'created'
            );
            INSERT INTO review_logs VALUES (
                'log-1', 'requirement-1', 'EDIT_DECISION', '{}', '{}', 'comment', 'engineer', 'created'
            );
            INSERT INTO ai_analysis_jobs VALUES (
                'job-1', 'project-1', 'document-1', 'COMPLETED', 'created', 'finished', 'finished'
            );
            """
        )
        conn.commit()
        conn.close()

        with patch.object(db, "DB_PATH", legacy_path):
            db.init_db()
            migrated_requirement = db.get_requirement("requirement-1")
            sets = db.list_requirement_sets("project-1", "document-1")
            self.assertEqual(migrated_requirement["id"], "requirement-1")
            self.assertEqual(migrated_requirement["final_decision"], "REJECT")
            self.assertEqual(migrated_requirement["evidences"][0]["id"], "evidence-1")
            self.assertEqual(db.list_assessment_runs("requirement-1")[0]["id"], "run-1")
            self.assertEqual(db.list_review_logs("requirement-1")[0]["action"], "EDIT_DECISION")
            self.assertEqual(len(sets), 1)
            self.assertEqual(migrated_requirement["requirement_set_id"], sets[0]["id"])
            migrated_job = db.get_ai_analysis_job("job-1")
            self.assertEqual(migrated_job["requirement_set_id"], sets[0]["id"])
            self.assertEqual(migrated_job["knowledge_base_version"], 0)

    def test_legacy_export_lineage_backfill_is_deterministic_and_never_guesses_mode(self) -> None:
        project, document = self._create_project_with_document("-legacy-export")
        requirement = db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1",
            "Legacy exported requirement", "FUNCTIONAL", "MEDIUM",
        )
        timestamp = "2026-07-18T08:00:00+00:00"
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO export_jobs
                (id, project_id, file_name, file_path, version_label, compare_export_id,
                 snapshot_json, sor_document_id, requirement_set_id, export_mode,
                 source_hash, knowledge_base_version, ai_run_id, human_coverage, created_at)
                VALUES ('legacy-scoped', ?, 'legacy.xlsx', '/tmp/legacy.xlsx', '', '', ?, '', '',
                        'DRAFT', '', 0, '', 0, ?)
                """,
                (project["id"], json.dumps([{"id": requirement["id"]}]), timestamp),
            )

        db.init_db()
        scoped = db.get_export_job("legacy-scoped")
        self.assertEqual(scoped["sor_document_id"], document["id"])
        self.assertEqual(scoped["requirement_set_id"], requirement["requirement_set_id"])
        self.assertEqual(scoped["export_mode"], "LEGACY")

        second_path = self.tmp_path / "legacy-export-second.txt"
        second_path.write_text("second", encoding="utf-8")
        db.add_sor_document(
            project["id"], "legacy-export-second.txt", "txt", str(second_path), "second"
        )
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO export_jobs
                (id, project_id, file_name, file_path, version_label, compare_export_id,
                 snapshot_json, sor_document_id, requirement_set_id, export_mode,
                 source_hash, knowledge_base_version, ai_run_id, human_coverage, created_at)
                VALUES ('legacy-ambiguous', ?, 'ambiguous.xlsx', '/tmp/ambiguous.xlsx', '', '',
                        '[]', '', '', 'DRAFT', '', 0, '', 0, ?)
                """,
                (project["id"], timestamp),
            )
        db.init_db()
        ambiguous = db.get_export_job("legacy-ambiguous")
        self.assertEqual(ambiguous["sor_document_id"], "")
        self.assertEqual(ambiguous["requirement_set_id"], "")
        self.assertEqual(ambiguous["export_mode"], "LEGACY")

    def test_review_save_cannot_mutate_or_unlock_locked_requirement_set(self) -> None:
        project, document = self._create_project_with_document("-auto-unlock")
        preview = _fake_preview()
        with patch.object(main, "build_extraction_preview", return_value=preview):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))
            locked = self._lock_active_requirement_set(
                project["id"],
                document["id"],
            )
            with self.assertRaises(HTTPException) as blocked:
                main.save_review_entry(
                    project["id"],
                    document["id"],
                    self._review_entry(
                        document["id"],
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

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertTrue(locked["summary"]["locked"])
        current = db.list_requirements(project["id"], document["id"])
        self.assertEqual(
            current[0]["raw_text"],
            "供应商需要满足总成试验要求。",
        )

    def test_lock_and_ai_gate_behavior(self) -> None:
        project, empty_document = self._create_project_with_document("-empty")
        empty_set = db.create_requirement_set(
            project["id"],
            empty_document["id"],
        )
        db.activate_requirement_set(
            project["id"],
            empty_document["id"],
            empty_set["id"],
        )
        with TestClient(main.app) as client:
            missing_selection = client.post(
                (
                    f"/api/projects/{project['id']}/sor-documents/"
                    f"{empty_document['id']}/lock-extraction"
                ),
                json={},
            )
        self.assertEqual(missing_selection.status_code, 422)
        with self.assertRaises(HTTPException) as lock_exc:
            main.lock_document_extraction(
                project["id"],
                empty_document["id"],
                main.LockExtractionRequest(
                    requirement_set_id=self._active_requirement_set_id(
                        empty_document["id"]
                    ),
                ),
            )
        self.assertEqual(lock_exc.exception.status_code, 409)

        project, document = self._create_project_with_document("-gate")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        with patch.object(main, "_build_ai_profile_catalog", return_value=[{"id": "azure", "ready": True}]):
            workspace_before_lock = main.get_document_workspace(
                project["id"], document["id"], include_preview=False
            )
            self.assertFalse(workspace_before_lock["actions"]["can_export"])

            with self.assertRaises(HTTPException) as ai_before_lock_exc:
                main.run_document_ai_analysis(
                    project["id"],
                    document["id"],
                    self._active_requirement_set_id(document["id"]),
                )
            self.assertEqual(ai_before_lock_exc.exception.status_code, 409)

            workspace_locked = self._lock_active_requirement_set(
                project["id"],
                document["id"],
            )
            self.assertTrue(workspace_locked["summary"]["locked"])
            self.assertTrue(workspace_locked["actions"]["can_export"])
            self.assertTrue(workspace_locked["actions"]["can_run_ai_analysis"])

            ai_without_kb = main.run_document_ai_analysis(
                project["id"],
                document["id"],
                self._active_requirement_set_id(document["id"]),
            )
            self.assertEqual(ai_without_kb["ai_processed_requirements"], 0)
            self.assertGreater(ai_without_kb["ai_screened_requirements"], 0)
            self.assertEqual(ai_without_kb["knowledge_docs_used"], 0)
            self.assertGreater(ai_without_kb["summary"]["ai_eligible_count"], 0)
            self.assertGreater(ai_without_kb["summary"]["assessment_target_count"], 0)

            self._add_knowledge_document(project["id"], "-gate")
            workspace = main.get_document_workspace(project["id"], document["id"], include_preview=False)
            self.assertTrue(workspace["actions"]["can_run_ai_analysis"])

    def test_zero_target_matrix_can_lock_and_export_without_starting_analysis(
        self,
    ) -> None:
        project, document = self._create_project_with_document("-zero-target")
        db.insert_requirement(
            project["id"],
            document["id"],
            "REF0001",
            1,
            "Page 1 | Reference",
            "此条仅作为分析上下文保留。",
            "REFERENCE",
            "MEDIUM",
            entry_kind="REFERENCE",
            raw_text="此条仅作为分析上下文保留。",
            display_text="此条仅作为分析上下文保留。",
        )
        self._allow_verifiable_tree_derivation()
        requirement_set_id = self._active_requirement_set_id(document["id"])
        requirement_id = db.list_requirements(
            project["id"],
            document["id"],
            requirement_set_id=requirement_set_id,
        )[0]["id"]
        draft_workspace = main.get_document_workspace(
            project["id"],
            document["id"],
            include_preview=False,
        )
        self.assertTrue(draft_workspace["actions"]["can_lock_extraction"])
        self.assertTrue(draft_workspace["actions"]["can_export_draft"])
        self.assertFalse(draft_workspace["actions"]["can_export"])
        self.assertFalse(draft_workspace["actions"]["can_run_ai_analysis"])
        self.assertNotIn(
            "EMPTY_DECISION_SET",
            {
                item["code"]
                for item in draft_workspace["readiness"]["lock"]["hard_blockers"]
            },
        )

        with TestClient(main.app) as client:
            readiness_response = client.get(
                (
                    f"/api/projects/{project['id']}/sor-documents/"
                    f"{document['id']}/lock-readiness"
                ),
                params={"requirement_set_id": requirement_set_id},
            )
            self.assertEqual(readiness_response.status_code, 200)
            readiness = readiness_response.json()
            self.assertEqual(
                readiness["schema_version"],
                "requirement-set-lock-readiness/v1",
            )
            self.assertTrue(readiness["can_lock"])
            self.assertEqual(readiness["next_destination"], "MATRIX")
            self.assertEqual(readiness["counts"]["analysis_targets"], 0)
            self.assertEqual(readiness["counts"]["retained_visible"], 1)
            self.assertEqual(readiness["counts"]["skipped_visible"], 1)

            from app.adapters.persistence.requirement_matrix_states import (
                set_requirement_matrix_node_disposition,
            )

            set_requirement_matrix_node_disposition(
                project_id=project["id"],
                document_id=document["id"],
                requirement_set_id=requirement_set_id,
                node_id=f"matrix-{requirement_id}",
                disposition="RETAIN",
                reason="显式保留上下文",
                expected_revision=0,
            )
            refreshed_response = client.get(
                (
                    f"/api/projects/{project['id']}/sor-documents/"
                    f"{document['id']}/lock-readiness"
                ),
                params={"requirement_set_id": requirement_set_id},
            )
            self.assertEqual(refreshed_response.status_code, 200)
            refreshed_readiness = refreshed_response.json()
            self.assertNotEqual(
                refreshed_readiness["readiness_sha256"],
                readiness["readiness_sha256"],
            )

            stale_lock = client.post(
                (
                    f"/api/projects/{project['id']}/sor-documents/"
                    f"{document['id']}/lock-extraction"
                ),
                json={
                    "schema_version": "requirement-set-lock-request/v1",
                    "requirement_set_id": requirement_set_id,
                    "expected_readiness_sha256": readiness["readiness_sha256"],
                },
            )
            self.assertEqual(stale_lock.status_code, 409)
            self.assertEqual(
                stale_lock.json()["detail"]["code"],
                "requirement_set_lock_readiness_changed",
            )
            self.assertEqual(
                db.get_requirement_set(requirement_set_id)["status"],
                "DRAFT",
            )

            locked_response = client.post(
                (
                    f"/api/projects/{project['id']}/sor-documents/"
                    f"{document['id']}/lock-extraction"
                ),
                json={
                    "schema_version": "requirement-set-lock-request/v1",
                    "requirement_set_id": requirement_set_id,
                    "expected_readiness_sha256": refreshed_readiness[
                        "readiness_sha256"
                    ],
                },
            )
            self.assertEqual(locked_response.status_code, 200)
            locked = locked_response.json()

        self.assertEqual(locked["next_destination"], "MATRIX")
        self.assertFalse(locked["analysis_available"])
        self.assertEqual(
            locked["lock_readiness"]["readiness_sha256"],
            refreshed_readiness["readiness_sha256"],
        )
        self.assertTrue(locked["actions"]["can_export"])
        self.assertTrue(locked["actions"]["can_export_formal"])
        self.assertFalse(locked["actions"]["can_run_ai_analysis"])
        self.assertNotIn(
            "EMPTY_DECISION_SET",
            {
                item["code"]
                for item in locked["readiness"]["lock"]["hard_blockers"]
            },
        )

        exported = main.export_project(
            project["id"],
            main.ExportRequest(
                document_id=document["id"],
                requirement_set_id=requirement_set_id,
                mode="FORMAL",
            ),
        )
        self.assertEqual(exported["decision_eligible_count"], 0)
        self.assertTrue((main.EXPORTS_DIR / exported["file_name"]).exists())

    def test_workspace_formal_readiness_uses_effective_ai_and_human_conclusions(
        self,
    ) -> None:
        project, document = self._create_project_with_document(
            "-effective-formal-readiness"
        )
        requirements = [
            db.insert_requirement(
                project["id"],
                document["id"],
                f"REQ000{index}",
                index,
                f"Page {index} | Section {index}",
                f"系统应满足第 {index} 条需求。",
                "FUNCTIONAL",
                "HIGH",
            )
            for index in (1, 2)
        ]
        self._lock_active_requirement_set(project["id"], document["id"])
        for requirement in requirements:
            db.upsert_assessment(
                requirement["id"],
                "profile",
                "provider",
                "ACCEPT",
                "AI 当前结论。",
                0.9,
                "llm_primary",
                "vector",
                "REQUIREMENT",
                "basis",
                "需求文本",
                "HIGH",
                "",
                "model",
                "prompt-v3",
                compliance_status="COMPLIANT",
            )
        _seed_human_decision(
            requirements[1]["id"],
            "REJECT",
            "工程师覆盖 AI 结论。",
        )

        workspace = main.get_document_workspace(
            project["id"],
            document["id"],
            include_preview=False,
        )

        self.assertEqual(workspace["summary"]["decision_eligible_count"], 2)
        self.assertEqual(workspace["summary"]["human_decision_count"], 1)
        self.assertEqual(workspace["summary"]["unassigned_count"], 1)
        self.assertEqual(workspace["summary"]["human_coverage"], 0.5)
        self.assertEqual(workspace["summary"]["effective_conclusion_count"], 2)
        self.assertEqual(
            workspace["summary"]["effective_conclusion_coverage"],
            1.0,
        )
        self.assertEqual(
            workspace["summary"]["missing_effective_conclusion_count"],
            0,
        )
        self.assertEqual(workspace["summary"]["ai_default_count"], 1)
        self.assertEqual(workspace["summary"]["human_override_count"], 1)
        self.assertEqual(workspace["summary"]["human_native_count"], 0)
        self.assertTrue(workspace["readiness"]["formal_export"]["ready"])
        self.assertTrue(workspace["actions"]["can_export_formal"])

    def test_workspace_formal_readiness_requires_set_preview_proof(self) -> None:
        project, document = self._create_project_with_document(
            "-missing-set-preview-proof"
        )
        requirement = db.insert_requirement(
            project["id"],
            document["id"],
            "REQ0001",
            1,
            "Page 1 | Section 1",
            "系统应满足测试需求。",
            "FUNCTIONAL",
            "HIGH",
        )
        self._lock_active_requirement_set(project["id"], document["id"])
        db.upsert_assessment(
            requirement["id"],
            "profile",
            "provider",
            "ACCEPT",
            "AI 当前结论。",
            0.9,
            "llm_primary",
            "vector",
            "REQUIREMENT",
            "basis",
            "需求文本",
            "HIGH",
            "",
            "model",
            "prompt-v3",
            compliance_status="COMPLIANT",
        )
        requirement_set_id = self._active_requirement_set_id(document["id"])
        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE requirement_sets
                SET preview_generation_id = '', preview_status_at_lock = ''
                WHERE id = ?
                """,
                (requirement_set_id,),
            )

        workspace = main.get_document_workspace(
            project["id"],
            document["id"],
            include_preview=False,
        )
        with self.assertRaises(HTTPException) as export_error:
            main.export_project(
                project["id"],
                main.ExportRequest(
                    document_id=document["id"],
                    requirement_set_id=requirement_set_id,
                    mode="FORMAL",
                ),
            )

        self.assertEqual(export_error.exception.status_code, 409)
        export_blockers = export_error.exception.detail["readiness"][
            "hard_blockers"
        ]
        self.assertIn(
            {
                "code": "PREVIEW_NOT_STABLE",
                "status": "MISSING_REQUIREMENT_SET_PREVIEW",
                "hard": True,
            },
            export_blockers,
        )
        self.assertFalse(workspace["readiness"]["formal_export"]["ready"])
        self.assertFalse(workspace["actions"]["can_export_formal"])
        self.assertIn(
            {
                "code": "PREVIEW_NOT_STABLE",
                "status": "MISSING_REQUIREMENT_SET_PREVIEW",
                "hard": True,
            },
            workspace["readiness"]["formal_export"]["hard_blockers"],
        )

    def test_workspace_human_coverage_counts_assessed_manual_decision(self) -> None:
        project, document = self._create_project_with_document(
            "-assessed-manual-coverage"
        )
        requirement = db.insert_requirement(
            project["id"],
            document["id"],
            "REQ0001",
            1,
            "Page 1 | Section 1",
            "系统应满足测试需求。",
            "FUNCTIONAL",
            "HIGH",
        )
        self._lock_active_requirement_set(project["id"], document["id"])
        requirement_set_id = self._active_requirement_set_id(document["id"])
        node = MatrixNodeStateV1(
            node_id=f"matrix-{requirement['id']}",
            parent_id=None,
            child_ids=(),
            depth=0,
            source_order=0,
            display_role=MatrixNodeKind.ATOMIC_REQUIREMENT,
            label="系统应满足测试需求。",
            source_refs=(),
            page_start=1,
            requirement_entry_id=requirement["id"],
            requirement_id=requirement["id"],
            requirement_code="REQ0001",
            requirement_text="系统应满足测试需求。",
            matrix_visibility=MatrixNodeVisibility.VISIBLE,
            disposition=MatrixNodeDisposition.ANALYZE,
            analysis_eligibility=MatrixAnalysisEligibility.ELIGIBLE,
            analysis_status=MatrixAnalysisStatus.ASSESSED,
            ai_assessment=MatrixAiAssessmentV1(
                decision="ACCEPT",
                compliance_status="COMPLIANT",
                reason="AI 当前结论。",
                confidence=0.9,
                assessment_mode="llm_primary",
            ),
            manual_decision=MatrixManualDecisionV1(
                decision="REJECT",
                reason="工程师覆盖 AI 结论。",
                decided_at="2026-08-09T00:00:00+00:00",
            ),
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id=project["id"],
            document_id=document["id"],
            requirement_set_id=requirement_set_id,
            requirement_set_status="LOCKED",
            tree_sha256="f" * 64,
            root_node_ids=(node.node_id,),
            nodes=(node,),
            counts=MatrixCountsV1(
                total_nodes=1,
                visible_nodes=1,
                analyze_nodes=1,
                analysis_eligible_nodes=1,
                assessed_nodes=1,
                decided_nodes=0,
            ),
        )

        with patch(
            "app.application.requirement_matrix.build_requirement_matrix_snapshot",
            return_value=matrix,
        ):
            workspace = main.get_document_workspace(
                project["id"],
                document["id"],
                include_preview=False,
            )

        self.assertEqual(workspace["summary"]["decision_eligible_count"], 1)
        self.assertEqual(workspace["summary"]["human_decision_count"], 1)
        self.assertEqual(workspace["summary"]["unassigned_count"], 0)
        self.assertEqual(workspace["summary"]["human_coverage"], 1.0)
        self.assertEqual(workspace["summary"]["effective_conclusion_count"], 1)
        self.assertEqual(workspace["summary"]["human_override_count"], 1)

    def test_workspace_missing_source_warning_uses_all_matrix_business_nodes(
        self,
    ) -> None:
        project, document = self._create_project_with_document(
            "-matrix-missing-source"
        )
        db.insert_requirement(
            project["id"],
            document["id"],
            "REQ0001",
            1,
            "Page 1 | Section 1",
            "用于建立已锁定测试版本。",
            "FUNCTIONAL",
            "MEDIUM",
        )
        self._lock_active_requirement_set(project["id"], document["id"])
        requirement_set_id = self._active_requirement_set_id(document["id"])

        def matrix_node(
            node_id: str,
            *,
            source_order: int,
            role: MatrixNodeKind,
            visibility: MatrixNodeVisibility,
            disposition: MatrixNodeDisposition,
        ) -> MatrixNodeStateV1:
            return MatrixNodeStateV1(
                node_id=node_id,
                parent_id=None,
                child_ids=(),
                depth=0,
                source_order=source_order,
                display_role=role,
                label=node_id,
                source_refs=(),
                page_start=None,
                matrix_visibility=visibility,
                disposition=disposition,
                analysis_eligibility=MatrixAnalysisEligibility.NOT_ELIGIBLE,
                analysis_status=MatrixAnalysisStatus.NOT_APPLICABLE,
            )

        nodes: list[MatrixNodeStateV1] = []
        nodes.extend(
            (
                matrix_node(
                    "structure-without-source",
                    source_order=0,
                    role=MatrixNodeKind.STRUCTURE,
                    visibility=MatrixNodeVisibility.VISIBLE,
                    disposition=MatrixNodeDisposition.RETAIN,
                ),
                matrix_node(
                    "context-without-source",
                    source_order=1,
                    role=MatrixNodeKind.CONTEXT,
                    visibility=MatrixNodeVisibility.VISIBLE,
                    disposition=MatrixNodeDisposition.RETAIN,
                ),
                matrix_node(
                    "excluded-without-source",
                    source_order=2,
                    role=MatrixNodeKind.CONTEXT,
                    visibility=MatrixNodeVisibility.HUMAN_EXCLUDED,
                    disposition=MatrixNodeDisposition.EXCLUDE,
                ),
                matrix_node(
                    "system-without-source",
                    source_order=3,
                    role=MatrixNodeKind.STRUCTURE,
                    visibility=MatrixNodeVisibility.SYSTEM_NON_BUSINESS,
                    disposition=MatrixNodeDisposition.RETAIN,
                ),
            )
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id=project["id"],
            document_id=document["id"],
            requirement_set_id=requirement_set_id,
            requirement_set_status="LOCKED",
            tree_sha256="f" * 64,
            root_node_ids=tuple(node.node_id for node in nodes),
            nodes=tuple(nodes),
            counts=MatrixCountsV1(
                total_nodes=4,
                visible_nodes=2,
                system_non_business_nodes=1,
                human_excluded_nodes=1,
                retained_nodes=3,
                excluded_nodes=1,
            ),
        )

        with patch(
            "app.application.requirement_matrix.build_requirement_matrix_snapshot",
            return_value=matrix,
        ):
            workspace = main.get_document_workspace(
                project["id"],
                document["id"],
                include_preview=False,
            )

        for readiness_key in ("lock", "formal_export"):
            warning = next(
                (
                    item
                    for item in workspace["readiness"][readiness_key]["warnings"]
                    if item["code"] == "MISSING_SOURCE"
                ),
                None,
            )
            self.assertEqual(
                warning,
                {"code": "MISSING_SOURCE", "count": 3, "hard": False},
            )

    def test_workspace_formal_readiness_blocks_reason_only_ai_payload(self) -> None:
        project, document = self._create_project_with_document(
            "-reason-only-formal-readiness"
        )
        requirement = db.insert_requirement(
            project["id"],
            document["id"],
            "REQ0001",
            1,
            "Page 1 | Section 1",
            "系统应满足测试需求。",
            "FUNCTIONAL",
            "HIGH",
        )
        self._lock_active_requirement_set(project["id"], document["id"])
        db.upsert_assessment(
            requirement["id"],
            "profile",
            "provider",
            "",
            "只有原因，没有结论。",
            0.4,
            "llm_primary",
            "vector",
            "REQUIREMENT",
            "basis",
            "需求文本",
            "HIGH",
            "",
            "model",
            "prompt-v3",
            compliance_status="",
        )

        workspace = main.get_document_workspace(
            project["id"],
            document["id"],
            include_preview=False,
        )

        self.assertFalse(workspace["readiness"]["formal_export"]["ready"])
        self.assertEqual(
            {
                item["code"]
                for item in workspace["readiness"]["formal_export"][
                    "hard_blockers"
                ]
            },
            {"MISSING_EFFECTIVE_CONCLUSIONS"},
        )
        self.assertEqual(workspace["summary"]["effective_conclusion_count"], 0)
        self.assertEqual(
            workspace["summary"]["missing_effective_conclusion_count"],
            1,
        )

    def test_ready_legacy_preview_cannot_lock_without_requirement_tree(self) -> None:
        project, document = self._create_project_with_document("-not-ready-lock")
        db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1 | Section 1",
            "Ready source required", "FUNCTIONAL", "MEDIUM",
        )
        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=False)
        blockers = workspace["readiness"]["lock"]["blockers"]
        self.assertEqual(workspace["summary"]["preview_status"], "READY")
        self.assertEqual(workspace["readiness"]["lock"]["basis"], "NONE")
        self.assertIn("PREVIEW_NOT_STABLE", {item["code"] for item in blockers})
        self.assertFalse(workspace["actions"]["can_lock_extraction"])
        with self.assertRaises(HTTPException) as exc:
            main.lock_document_extraction(
                project["id"],
                document["id"],
                main.LockExtractionRequest(
                    requirement_set_id=self._active_requirement_set_id(
                        document["id"]
                    ),
                ),
            )
        self.assertEqual(exc.exception.status_code, 409)

    def test_workspace_summary_counts_runnable_profiles_from_ready_flag(self) -> None:
        project, document = self._create_project_with_document("-ready-flag")
        requirements = [
            {
                "id": "req-ready-1",
                "req_code": "REQ0001",
                "entry_kind": "REQUIREMENT",
                "display_text": "供应商需要满足总成试验要求。",
                "raw_text": "供应商需要满足总成试验要求。",
                "requirement_text": "供应商需要满足总成试验要求。",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
                "source_page": 1,
                "source_anchor": "Main > 1",
                "parent_section_code": "1",
                "parent_section_title": "概述",
                "parent_section_label": "1 概述",
            }
        ]
        locked_document = {
            **document,
            "workflow_stage": "EXTRACTION_LOCKED",
            "requirement_set_version": 1,
            "extraction_locked_at": "2026-04-01T00:00:00+00:00",
        }

        with patch.dict(
            os.environ,
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "heuristic",
                "REQPILOT_RETRIEVAL_BACKEND": "local",
            },
            clear=True,
        ), patch.object(
            main,
            "_build_ai_profile_catalog",
            return_value=[
                {
                    "id": "aliyun",
                    "provider": "aliyun",
                    "label": "Aliyun",
                    "assessment_mode": "llm_shadow",
                    "retrieval_backend": "qdrant",
                    "ready": True,
                    "provider_ready": False,
                    "llm_ready": True,
                    "retrieval_ready": True,
                    "missing": [],
                    "knowledge_sync": {"status": "NOT_REQUIRED", "ready_count": 0, "total_count": 0},
                }
            ],
        ):
            workspace = main._build_document_workspace(
                project=project,
                document=locked_document,
                requirements=requirements,
                knowledge_docs_count=0,
                preview=None,
                preview_meta={},
            )

        self.assertEqual(workspace["summary"]["runnable_ai_profile_count"], 1)
        self.assertTrue(workspace["actions"]["can_run_ai_analysis"])

    def test_load_cached_preview_marks_unversioned_cache_as_stale(self) -> None:
        project, document = self._create_project_with_document("-unversioned-preview-cache")
        cache_path = preview_cache.preview_cache_path(document["id"])
        cache_path.write_text(json.dumps(_fake_preview(), ensure_ascii=False), encoding="utf-8")
        db.update_sor_document_preview_state(
            document["id"],
            preview_status="READY",
            preview_generated_at="2026-03-24T10:00:00+00:00",
            preview_file_mtime=preview_cache._source_mtime_token(document["file_path"]),
            preview_error="",
        )

        preview, meta = preview_cache.load_cached_preview(db.get_sor_document(document["id"]))

        self.assertIsNone(preview)
        self.assertEqual(meta["preview_status"], "STALE")
        self.assertFalse(meta["preview_cache_hit"])

    def test_load_cached_preview_marks_old_version_cache_as_stale(self) -> None:
        project, document = self._create_project_with_document("-old-version-preview-cache")
        cache_path = preview_cache.preview_cache_path(document["id"])
        payload = _fake_preview()
        payload["_preview_cache_version"] = "preview-cache-v3"
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        db.update_sor_document_preview_state(
            document["id"],
            preview_status="READY",
            preview_generated_at="2026-03-24T10:00:00+00:00",
            preview_file_mtime=preview_cache._source_mtime_token(document["file_path"]),
            preview_error="",
        )

        preview, meta = preview_cache.load_cached_preview(db.get_sor_document(document["id"]))

        self.assertIsNone(preview)
        self.assertEqual(meta["preview_status"], "STALE")
        self.assertFalse(meta["preview_cache_hit"])
        self.assertEqual(meta["preview_error"], "cached preview is outdated")

    def test_load_cached_preview_marks_old_build_token_as_stale(self) -> None:
        project, document = self._create_project_with_document("-old-build-token-preview-cache")
        cache_path = preview_cache.preview_cache_path(document["id"])
        payload = _fake_preview()
        payload["_preview_cache_version"] = preview_cache._PREVIEW_CACHE_VERSION
        payload["_preview_build_token"] = "preview-cache-v28-numbered-heading-anchor-fragments|source-crop-v56"
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        db.update_sor_document_preview_state(
            document["id"],
            preview_status="READY",
            preview_generated_at="2026-03-24T10:00:00+00:00",
            preview_file_mtime=preview_cache._source_mtime_token(document["file_path"]),
            preview_error="",
        )

        preview, meta = preview_cache.load_cached_preview(db.get_sor_document(document["id"]))

        self.assertIsNone(preview)
        self.assertEqual(meta["preview_status"], "STALE")
        self.assertFalse(meta["preview_cache_hit"])
        self.assertEqual(meta["preview_error"], "cached preview is outdated")

    def test_preview_refresh_purges_stale_snapshot_root_before_rebuild(self) -> None:
        project, document = self._create_project_with_document("-refresh-purge")
        cache_path = preview_cache.preview_cache_path(document["id"])
        cache_path.write_text("{}", encoding="utf-8")
        snapshot_root = preview_cache.document_snapshot_root(document["id"])
        stale_file = snapshot_root / "stale.png"
        stale_file.write_text("old", encoding="utf-8")

        def _fake_build(file_path: str, *, document_id: str = "", snapshot_root: Path | None = None) -> dict:
            self.assertEqual(document_id, document["id"])
            self.assertIsNotNone(snapshot_root)
            self.assertFalse(cache_path.exists())
            self.assertFalse(stale_file.exists())
            self.assertTrue(Path(snapshot_root or "").exists())
            return _fake_preview()

        with patch.object(preview_cache, "build_extraction_preview", side_effect=_fake_build):
            preview_cache._run_preview_refresh(document["id"], document["file_path"])

        self.assertFalse(stale_file.exists())
        self.assertTrue(cache_path.exists())

    def test_cancelled_preview_refresh_exits_before_build(self) -> None:
        project, document = self._create_project_with_document("-refresh-cancelled")
        preview_cache.cancel_preview_refresh(document["id"])

        with patch.object(preview_cache, "build_extraction_preview") as build_preview:
            preview_cache._run_preview_refresh(document["id"], document["file_path"])

        build_preview.assert_not_called()

    def test_delete_document_purges_preview_and_snapshot_artifacts(self) -> None:
        project, document = self._create_project_with_document("-delete-artifacts")
        cache_path = preview_cache.preview_cache_path(document["id"])
        cache_path.write_text("{}", encoding="utf-8")
        preview_root = preview_cache.document_snapshot_root(document["id"])
        (preview_root / "preview.png").write_text("preview", encoding="utf-8")
        persisted_root = main._table_snapshot_root(document["id"])
        (persisted_root / "persisted.png").write_text("persisted", encoding="utf-8")

        result = main.delete_sor_document(project["id"], document["id"])

        self.assertTrue(result["ok"])
        self.assertFalse(cache_path.exists())
        self.assertFalse(preview_cache.document_snapshot_root(document["id"], create=False).exists())
        self.assertFalse(main._table_snapshot_root(document["id"], create=False).exists())
        self.assertIsNone(db.get_sor_document(document["id"]))

    def test_deleted_document_preview_refresh_missing_file_does_not_mark_failed(self) -> None:
        project, document = self._create_project_with_document("-deleted-refresh-race")
        document_id = document["id"]
        file_path = document["file_path"]
        db.delete_sor_document(document_id)

        with patch.object(preview_cache, "build_extraction_preview", side_effect=FileNotFoundError("missing")), patch.object(
            preview_cache.db, "update_sor_document_preview_state"
        ) as update_preview_state:
            preview_cache._run_preview_refresh(document_id, file_path)

        update_preview_state.assert_not_called()

    def test_sync_document_table_assets_keeps_existing_snapshot_roots_intact(self) -> None:
        project, document = self._create_project_with_document("-isolated-snapshots")
        preview_root = preview_cache.document_snapshot_root(document["id"])
        persisted_root = main._table_snapshot_root(document["id"])
        preview_file = preview_root / "preview.png"
        persisted_file = persisted_root / "persisted.png"
        preview_file.write_text("preview", encoding="utf-8")
        persisted_file.write_text("persisted", encoding="utf-8")

        with patch.object(main, "_materialize_document_table_assets", return_value=([], [])):
            main._sync_document_table_assets(project["id"], document["id"])

        self.assertTrue(preview_file.exists())
        self.assertTrue(persisted_file.exists())

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

    def test_workspace_uses_cached_preview_order_even_without_preview_payload(self) -> None:
        project, document = self._create_project_with_document("-ordering-no-preview-payload")
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
        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=False)

        self.assertEqual([row["req_code"] for row in workspace["requirements"]], ["REQ0024", "REQ0023"])
        self.assertEqual(workspace["summary"]["order_source"], "review_overlay")
        self.assertEqual(workspace["summary"]["order_mismatch_count"], 0)
        self.assertNotIn("review_entries", workspace)
        self.assertNotIn("preview", workspace)

    def test_workspace_order_prefers_source_signature_over_stale_req_code(self) -> None:
        project, document = self._create_project_with_document("-ordering-stale-req")
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=5,
            source_anchor="Main > 5.1",
            requirement_text="5.1 第一条需求",
            raw_text="5.1 第一条需求",
            display_text="5.1 第一条需求",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0002",
            source_page=5,
            source_anchor="Main > 5.2",
            requirement_text="5.2 第二条需求",
            raw_text="5.2 第二条需求",
            display_text="5.2 第二条需求",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")

        preview_cache.persist_preview_cache(
            db.get_sor_document(document["id"]) or {},
            _fake_preview_with_reused_req_codes(),
        )
        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=True)

        self.assertEqual(
            [str(row.get("display_text") or row.get("requirement_text") or "") for row in workspace["requirements"]],
            ["5.2 第二条需求", "5.1 第一条需求"],
        )
        self.assertEqual([row["display_req_code"] for row in workspace["requirements"]], ["REQ0001", "REQ0002"])
        self.assertEqual(workspace["summary"]["order_source"], "review_overlay")
        self.assertEqual(workspace["summary"]["order_mismatch_count"], 0)

    def test_workspace_keeps_same_code_local_clause_in_preview_order(self) -> None:
        project, document = self._create_project_with_document("-same-code-local-clause")
        preview = _fake_preview_with_same_code_local_clause_in_section_six()
        for number in range(10, 0, -1):
            preview_row = preview["complete_requirement_entries"][number]
            db.insert_requirement(
                project_id=project["id"],
                sor_document_id=document["id"],
                req_code=str(preview_row["req_code"]),
                source_page=9,
                source_anchor=str(preview_row["source_anchor"]),
                requirement_text=str(preview_row["display_text"]),
                raw_text=str(preview_row["raw_text"]),
                display_text=str(preview_row["display_text"]),
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                parent_section_code=str(preview_row["parent_section_code"]),
                parent_section_title=str(preview_row["parent_section_title"]),
                parent_section_label=str(preview_row["parent_section_label"]),
            )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        preview_cache.persist_preview_cache(db.get_sor_document(document["id"]) or {}, preview)

        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=True)

        self.assertEqual(
            [row["section_key"] for row in workspace["requirements"]],
            [f"6::{number}" for number in range(1, 11)],
        )
        self.assertEqual(
            [row["owner_section_label"] for row in workspace["requirements"]],
            [f"{number}）" for number in range(1, 11)],
        )
        self.assertEqual(workspace["requirements"][5]["section_key"], "6::6")
        self.assertEqual(workspace["requirements"][5]["owner_section_label"], "6）")
        self.assertEqual(workspace["summary"]["order_source"], "review_overlay")
        self.assertEqual(workspace["summary"]["order_mismatch_count"], 0)

    def test_workspace_review_entries_preserve_section_six_subtree_order_and_owner_labels(self) -> None:
        project, document = self._create_project_with_document("-review-same-code-local-clause")
        preview = _fake_preview_with_same_code_local_clause_in_section_six()
        for number in range(10, 0, -1):
            preview_row = preview["complete_requirement_entries"][number]
            db.insert_requirement(
                project_id=project["id"],
                sor_document_id=document["id"],
                req_code=str(preview_row["req_code"]),
                source_page=9,
                source_anchor=str(preview_row["source_anchor"]),
                requirement_text=str(preview_row["display_text"]),
                raw_text=str(preview_row["raw_text"]),
                display_text=str(preview_row["display_text"]),
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                parent_section_code=str(preview_row["parent_section_code"]),
                parent_section_title=str(preview_row["parent_section_title"]),
                parent_section_label=str(preview_row["parent_section_label"]),
            )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        preview_cache.persist_preview_cache(db.get_sor_document(document["id"]) or {}, preview)

        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=True)

        self.assertEqual(
            [row["section_key"] for row in workspace["review_entries"]],
            ["6", *[f"6::{number}" for number in range(1, 11)]],
        )
        self.assertEqual(workspace["review_entries"][0]["owner_section_label"], "6")
        self.assertEqual(workspace["review_entries"][6]["section_key"], "6::6")
        self.assertEqual(workspace["review_entries"][6]["owner_section_label"], "6）")
        self.assertEqual(workspace["review_entries"][6]["structural_parent_section_label"], "6")

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

    def test_overlay_review_entries_skips_stale_req_code_match_when_preview_fingerprint_changed(self) -> None:
        preview_rows = [
            {
                "entry_id": "REQ0001",
                "req_code": "REQ0001",
                "entry_kind": "REQUIREMENT",
                "display_text": "新的正确内容",
                "raw_text": "新的正确内容",
                "requirement_text": "新的正确内容",
                "source_page": 11,
                "source_page_end": 11,
                "source_anchor": "Page 11 | Heading 软件开发要求",
                "parent_section_code": "",
                "parent_section_title": "软件开发要求",
                "parent_section_label": "软件开发要求",
            }
        ]
        persisted_rows = [
            {
                "id": "persisted-1",
                "req_code": "REQ0001",
                "entry_kind": "REQUIREMENT",
                "display_text": "旧的错误内容",
                "raw_text": "旧的错误内容",
                "requirement_text": "旧的错误内容",
                "source_page": 12,
                "source_page_end": 12,
                "source_anchor": "Page 12 | Section 1.1 FMEA编制与更新",
                "parent_section_code": "1.1",
                "parent_section_title": "FMEA编制与更新",
                "parent_section_label": "1.1 FMEA编制与更新",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
            }
        ]

        rows = main._overlay_review_entries(preview_rows, persisted_rows, prefer_raw=False)

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["keep"])
        self.assertEqual(rows[0]["parent_section_label"], "软件开发要求")
        self.assertEqual(rows[0]["requirement_text"], "新的正确内容")

    def test_overlay_review_entries_matches_same_source_when_req_code_changed(self) -> None:
        preview_rows = [
            {
                "entry_id": "REQ0999",
                "req_code": "REQ0999",
                "entry_kind": "REQUIREMENT",
                "display_text": "新的正确内容",
                "raw_text": "新的正确内容",
                "requirement_text": "新的正确内容",
                "source_page": 11,
                "source_page_end": 11,
                "source_anchor": "Page 11 | Heading 软件开发要求",
                "parent_section_code": "",
                "parent_section_title": "软件开发要求",
                "parent_section_label": "软件开发要求",
            }
        ]
        persisted_rows = [
            {
                "id": "persisted-1",
                "req_code": "REQ0001",
                "entry_kind": "REQUIREMENT",
                "display_text": "旧的内容",
                "raw_text": "旧的内容",
                "requirement_text": "旧的内容",
                "source_page": 11,
                "source_page_end": 11,
                "source_anchor": "Page 11 | Heading 软件开发要求",
                "parent_section_code": "",
                "parent_section_title": "软件开发要求",
                "parent_section_label": "软件开发要求",
                "status": "DRAFT",
            }
        ]

        rows = main._overlay_review_entries(preview_rows, persisted_rows, prefer_raw=True)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["keep"])
        self.assertEqual(rows[0]["id"], "persisted-1")
        self.assertEqual(rows[0]["req_code"], "REQ0999")
        self.assertEqual(rows[0]["raw_text"], "新的正确内容")

    def test_overlay_review_entries_keeps_review_state_for_same_preview_fingerprint(self) -> None:
        preview_rows = [
            {
                "entry_id": "REQ0002",
                "req_code": "REQ0002",
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
            }
        ]
        persisted_rows = [
            {
                "id": "persisted-2",
                "req_code": "REQ0002",
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
                "priority": "HIGH",
                "status": "reviewed",
            }
        ]

        rows = main._overlay_review_entries(preview_rows, persisted_rows, prefer_raw=False)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["keep"])
        self.assertEqual(rows[0]["priority"], "HIGH")
        self.assertEqual(rows[0]["parent_section_label"], "1 概述")

    def test_overlay_review_entries_keeps_preview_structure_for_matched_review_row(self) -> None:
        preview_rows = [
            {
                "entry_id": "REQ0103",
                "req_code": "REQ-0103",
                "entry_kind": "REQUIREMENT",
                "display_text": "10）商务报价应至少包含设计开发费用。",
                "raw_text": "10）商务报价应至少包含设计开发费用。",
                "requirement_text": "10）商务报价应至少包含设计开发费用。",
                "source_page": 10,
                "source_page_end": 10,
                "source_anchor": "Main > 6",
                "parent_section_code": "6",
                "parent_section_title": "投标说明",
                "parent_section_label": "6 投标说明",
            }
        ]
        persisted_rows = [
            {
                "id": "persisted-3",
                "req_code": "REQ-0103",
                "entry_kind": "REQUIREMENT",
                "display_text": "10）商务报价应至少包含设计开发费用。",
                "raw_text": "10）商务报价应至少包含设计开发费用。",
                "requirement_text": "10）商务报价应至少包含设计开发费用。",
                "source_page": 10,
                "source_page_end": 10,
                "source_anchor": "Main > 7",
                "parent_section_code": "7",
                "parent_section_title": "附件目录",
                "parent_section_label": "7 附件目录",
                "requirement_type": "FUNCTIONAL",
                "priority": "HIGH",
                "status": "reviewed",
            }
        ]

        rows = main._overlay_review_entries(preview_rows, persisted_rows, prefer_raw=False)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["keep"])
        self.assertEqual(rows[0]["priority"], "HIGH")
        self.assertEqual(rows[0]["source_anchor"], "Main > 6")
        self.assertEqual(rows[0]["parent_section_label"], "6 投标说明")

    def test_overlay_review_entries_drops_stale_visual_assets_from_previous_preview_generation(self) -> None:
        preview_rows = [
            {
                "entry_id": "REQ0201",
                "req_code": "REQ0201",
                "entry_kind": "REQUIREMENT",
                "display_text": "供应商应满足新的当前条目。",
                "raw_text": "供应商应满足新的当前条目。",
                "requirement_text": "供应商应满足新的当前条目。",
                "source_page": 6,
                "source_page_end": 6,
                "source_anchor": "Main > 2.1",
                "parent_section_code": "2.1",
                "parent_section_title": "当前条目",
                "parent_section_label": "2.1 当前条目",
            }
        ]
        persisted_rows = [
            {
                "id": "persisted-stale-assets",
                "req_code": "REQ0201",
                "entry_kind": "REQUIREMENT",
                "display_text": "供应商应满足新的当前条目。",
                "raw_text": "供应商应满足新的当前条目。",
                "requirement_text": "供应商应满足新的当前条目。",
                "source_page": 6,
                "source_page_end": 6,
                "source_anchor": "Main > 2.1",
                "parent_section_code": "2.1",
                "parent_section_title": "当前条目",
                "parent_section_label": "2.1 当前条目",
                "preview_generation_id": "gen-old",
                "table_refs": [{"table_id": "tbl_stale"}],
                "content_blocks": [{"kind": "table", "table_id": "tbl_stale"}],
                "has_table_asset": True,
                "table_render_mode": "structured",
                "status": "reviewed",
            }
        ]

        rows = main._overlay_review_entries(
            preview_rows,
            persisted_rows,
            prefer_raw=False,
            preview_generation_id="gen-current",
        )

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["keep"])
        self.assertEqual(rows[0]["table_refs"], [])
        self.assertEqual(rows[0]["content_blocks"], [])
        self.assertFalse(rows[0]["has_table_asset"])
        self.assertEqual(rows[0]["table_render_mode"], "")

    def test_enrich_requirements_with_preview_context_replaces_stale_visual_assets_from_current_preview(self) -> None:
        project, document = self._create_project_with_document("-generation-enrich")
        preview_source = self.tmp_path / "generation-current.png"
        preview_source.write_bytes(b"preview-current")
        db.update_sor_document_preview_state(document["id"], preview_generation_id="gen-current")
        preview_cache.persist_preview_cache(
            db.get_sor_document(document["id"]) or document,
            {
                "preview_generation_id": "gen-current",
                "summary": {"dedupe_version": "heading_requirement_v1"},
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
                        "table_refs": [
                            {
                                "table_id": "tbl_current",
                                "source_snapshot_path": str(preview_source),
                                "slice_snapshot_path": str(preview_source),
                                "slice_render_mode": "snapshot_only",
                                "export_variant": "source",
                                "recommended_export_variant": "source",
                                "inherited_headers": [],
                                "slice_rows": [],
                            }
                        ],
                        "content_blocks": [{"kind": "table", "table_id": "tbl_current"}],
                        "has_table_asset": True,
                        "table_render_mode": "structured",
                    }
                ],
                "complete_requirement_entries_raw": [],
            },
        )
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Main > 1",
            requirement_text="供应商需要满足总成试验要求。",
            raw_text="供应商需要满足总成试验要求。",
            display_text="供应商需要满足总成试验要求。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            table_refs=json.dumps([{"table_id": "tbl_old"}], ensure_ascii=False),
            content_blocks=json.dumps([{"kind": "table", "table_id": "tbl_old"}], ensure_ascii=False),
            has_table_asset=True,
            table_render_mode="structured",
            preview_generation_id="gen-old",
        )

        rows = main._enrich_requirements_with_preview_context(
            db.list_requirements(project["id"], document["id"]),
            str(document["file_path"]),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["preview_generation_id"], "gen-current")
        self.assertEqual(rows[0]["table_refs"][0]["table_id"], "tbl_current")
        self.assertEqual(rows[0]["content_blocks"][0]["table_id"], "tbl_current")

    def test_find_requirement_for_review_entry_preserves_unique_req_code_identity(self) -> None:
        requirements = [
            {
                "id": "persisted-a",
                "req_code": "REQ0001",
                "source_page": 11,
                "source_anchor": "Page 11 | Section 3.1",
                "raw_text": "3.1 旧条目",
                "display_text": "3.1 旧条目",
                "requirement_text": "3.1 旧条目",
            },
            {
                "id": "persisted-b",
                "req_code": "REQ0002",
                "source_page": 12,
                "source_anchor": "Page 12 | Section 3.2",
                "raw_text": "3.2 正确条目",
                "display_text": "3.2 正确条目",
                "requirement_text": "3.2 正确条目",
            },
        ]
        normalized = {
            "entry_id": "REQ0001",
            "req_code": "REQ0001",
            "source_page": 12,
            "source_anchor": "Page 12 | Section 3.2",
            "raw_text": "3.2 正确条目",
            "display_text": "3.2 正确条目",
            "requirement_text": "3.2 正确条目",
        }

        matched = main._find_requirement_for_review_entry(requirements, normalized)

        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], "persisted-a")

    def test_enrich_requirements_with_preview_context_matches_by_source_signature(self) -> None:
        project, document = self._create_project_with_document("-preview-context-source-match")
        rows = [
            {
                "id": "persisted-b",
                "project_id": project["id"],
                "sor_document_id": document["id"],
                "req_code": "REQ0002",
                "source_page": 5,
                "source_page_end": 5,
                "source_anchor": "Main > 5.2",
                "raw_text": "5.2 第二条需求",
                "display_text": "5.2 第二条需求",
                "requirement_text": "5.2 第二条需求",
                "parent_section_code": "",
                "parent_section_title": "",
                "parent_section_label": "",
            }
        ]

        with patch.object(main, "load_cached_preview", return_value=(_fake_preview_with_reused_req_codes(), {})):
            enriched = main._enrich_requirements_with_preview_context(rows, str(document["file_path"]))

        self.assertEqual(enriched[0]["parent_section_code"], "5.2")
        self.assertEqual(enriched[0]["parent_section_label"], "5.2 第二条需求")

    def test_materialize_document_table_assets_uses_preview_source_match_not_req_code(self) -> None:
        project, document = self._create_project_with_document("-materialize-source-match")
        rows = [
            {
                "id": "persisted-b",
                "project_id": project["id"],
                "sor_document_id": document["id"],
                "req_code": "REQ0002",
                "source_page": 0,
                "source_page_end": 0,
                "source_anchor": "Main > 5.2",
                "raw_text": "5.2 第二条需求",
                "display_text": "5.2 第二条需求",
                "requirement_text": "5.2 第二条需求",
                "table_refs": [],
                "content_blocks": [],
                "has_table_asset": False,
                "table_render_mode": "",
            }
        ]

        def _fake_enrich(items, **kwargs):
            return items, []

        with patch.object(main, "load_cached_preview", return_value=(_fake_preview_with_reused_req_codes(), {})):
            with patch.object(main, "enrich_entries_with_table_assets", side_effect=_fake_enrich):
                materialized, assets = main._materialize_document_table_assets(rows, sor_document_id=document["id"])

        self.assertEqual(assets, [])
        self.assertEqual(materialized[0]["source_page"], 5)
        self.assertEqual(materialized[0]["source_page_end"], 5)

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
        self.assertIn("/snapshot", requirement["table_refs"][0]["slice_snapshot_url"])
        self.assertEqual(requirement["table_refs"][0]["recommended_export_variant"], "slice")
        self.assertEqual(requirement["table_refs"][0]["export_variant"], "slice")
        self.assertEqual(requirement["table_refs"][0]["export_variant_reasons"], [])
        self.assertEqual(
            [block["kind"] for block in requirement["content_blocks"]],
            ["paragraph", "table"],
        )

    def test_table_ref_recommendation_prefers_source_for_snapshot_only_with_images(self) -> None:
        ref = table_assets._normalize_table_ref(
            {
                "table_id": "tbl_complex",
                "slice_render_mode": "snapshot_only",
                "source_snapshot_path": str(self.tmp_path / "source.png"),
                "source_page": 5,
                "source_page_end": 6,
                "source_image_block_count": 1,
                "inherited_headers": ["项目", "要求"],
                "slice_rows": [["测试分组", "含图片和复杂布局"]],
                "inherited_notes": ["跨页续表"],
            }
        )

        self.assertEqual(ref["recommended_export_variant"], "source")
        self.assertEqual(ref["export_variant"], "source")
        self.assertIn("含图片", ref["export_variant_reasons"])
        self.assertIn("结构低置信", ref["export_variant_reasons"])

    def test_table_ref_recommendation_prefers_source_for_placeholder_symbol_table(self) -> None:
        ref = table_assets._normalize_table_ref(
            {
                "table_id": "tbl_placeholder",
                "slice_render_mode": "structured",
                "source_snapshot_path": str(self.tmp_path / "source-placeholder.png"),
                "source_page": 12,
                "source_page_end": 12,
                "inherited_headers": ["项目", "内容", "备注"],
                "slice_rows": [
                    ["安全相关关键特性", "法规相关关键特性", "功能相关重要特性"],
                    ["#REF!", "#REF!", "#REF!"],
                ],
                "inherited_notes": ["2.2 特殊特性分类与标识"],
            }
        )

        self.assertEqual(ref["recommended_export_variant"], "source")
        self.assertEqual(ref["export_variant"], "source")
        self.assertIn("切片可读性差", ref["export_variant_reasons"])

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
        self.assertIn("/snapshot", asset["snapshot_url"])
        self.assertTrue(Path(snapshot.path).exists())
        self.assertEqual(snapshot.headers.get("cache-control"), "no-store, max-age=0")
        self.assertEqual(snapshot.headers.get("pragma"), "no-cache")

    def test_requirement_set_table_snapshots_survive_fork_edit_delete_and_regeneration(self) -> None:
        project, document = self._create_project_with_document("-set-table-history")
        snapshot_root = self.tmp_path / "set-table-snapshots"
        preview = _fake_preview_with_table_markdown()

        with patch.object(main, "TABLE_SNAPSHOT_DIR", snapshot_root), patch.object(
            main, "build_extraction_preview", return_value=preview
        ):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

            first_set = db.get_active_requirement_set(document["id"])
            first_row = db.list_requirements(
                project["id"], requirement_set_id=first_set["id"]
            )[0]
            first_ref = first_row["table_refs"][0]
            first_path = Path(first_ref["slice_snapshot_path"])
            table_id = first_ref["table_id"]
            self.assertTrue(first_path.exists())
            self.assertIn(first_set["id"], first_path.parts)
            workspace = main.get_document_workspace(project["id"], document["id"], include_preview=False)
            self.assertIn(
                f"requirement_set_id={first_set['id']}",
                workspace["requirements"][0]["table_refs"][0]["slice_snapshot_url"],
            )

            db.lock_sor_document_extraction(document["id"])
            with self.assertRaises(PermissionError):
                db.replace_document_table_assets(
                    project["id"], document["id"], [], requirement_set_id=first_set["id"]
                )
            draft = db.fork_requirement_set_for_edit(project["id"], document["id"])
            draft_row = db.list_requirements(
                project["id"], requirement_set_id=draft["id"]
            )[0]
            edit_payload = main.ReviewedRequirementEntry(
                requirement_set_id=draft["id"],
                entry_id=draft_row["req_code"],
                req_code=draft_row["req_code"],
                entry_kind=draft_row["entry_kind"],
                source_page=draft_row["source_page"],
                source_anchor=draft_row["source_anchor"],
                requirement_text=draft_row["requirement_text"],
                display_text=draft_row["display_text"],
                raw_text=draft_row["raw_text"],
                requirement_type=draft_row["requirement_type"],
                priority="HIGH",
                table_refs=draft_row["table_refs"],
                content_blocks=draft_row["content_blocks"],
                keep=True,
            )
            main.save_review_entry(project["id"], document["id"], edit_payload)

            edited_row = db.list_requirements(
                project["id"], requirement_set_id=draft["id"]
            )[0]
            draft_path = Path(edited_row["table_refs"][0]["slice_snapshot_path"])
            self.assertTrue(draft_path.exists())
            self.assertIn(draft["id"], draft_path.parts)
            self.assertNotEqual(draft_path, first_path)
            self.assertTrue(first_path.exists())

            delete_payload = edit_payload.model_copy(update={"keep": False})
            main.save_review_entry(project["id"], document["id"], delete_payload)
            self.assertEqual(
                db.list_document_table_assets(
                    project["id"], document["id"], requirement_set_id=draft["id"]
                ),
                [],
            )
            self.assertTrue(draft_path.exists())
            self.assertTrue(first_path.exists())
            db.init_db()
            self.assertEqual(
                db.list_document_table_assets(
                    project["id"], document["id"], requirement_set_id=draft["id"]
                ),
                [],
            )

            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))
            regenerated_set = db.get_active_requirement_set(document["id"])
            regenerated_row = db.list_requirements(
                project["id"], requirement_set_id=regenerated_set["id"]
            )[0]
            regenerated_path = Path(regenerated_row["table_refs"][0]["slice_snapshot_path"])

            self.assertNotEqual(regenerated_set["id"], first_set["id"])
            self.assertIn(regenerated_set["id"], regenerated_path.parts)
            self.assertTrue(regenerated_path.exists())
            self.assertTrue(first_path.exists())
            historical_asset = db.get_document_table_asset(
                project["id"], document["id"], table_id,
                requirement_set_id=first_set["id"],
            )
            self.assertEqual(Path(historical_asset["snapshot_path"]), first_path)
            historical_snapshot = main.get_document_table_asset_snapshot(
                project["id"], document["id"], table_id,
                requirement_set_id=first_set["id"],
            )
            self.assertEqual(Path(historical_snapshot.path), first_path)
            historical_workspace = main.get_document_workspace(
                project["id"],
                document["id"],
                include_preview=True,
                requirement_set_id=first_set["id"],
            )
            historical_ref = historical_workspace["requirements"][0]["table_refs"][0]
            self.assertEqual(Path(historical_ref["slice_snapshot_path"]), first_path)
            self.assertIn(
                f"requirement_set_id={first_set['id']}",
                historical_ref["slice_snapshot_url"],
            )
            active_snapshot = main.get_document_table_asset_snapshot(
                project["id"], document["id"], table_id
            )
            self.assertEqual(Path(active_snapshot.path), regenerated_path)

    def test_legacy_document_table_assets_backfill_into_active_requirement_set(self) -> None:
        project, document = self._create_project_with_document("-legacy-table-backfill")
        snapshot_path = self.tmp_path / "legacy-table.png"
        snapshot_path.write_bytes(b"legacy")
        db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1",
            "legacy table requirement", "FUNCTIONAL", "MEDIUM",
            table_refs=json.dumps(
                [{"table_id": "tbl_legacy", "slice_snapshot_path": str(snapshot_path)}]
            ),
            has_table_asset=True,
        )
        active_set = db.get_active_requirement_set(document["id"])
        db.replace_document_table_assets(
            project["id"],
            document["id"],
            [{"table_id": "tbl_legacy", "snapshot_path": str(snapshot_path)}],
        )

        db.init_db()
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM document_table_assets WHERE sor_document_id = ?",
                (document["id"],),
            )

        asset = db.get_document_table_asset(
            project["id"], document["id"], "tbl_legacy",
            requirement_set_id=active_set["id"],
        )
        self.assertEqual(asset["requirement_set_id"], active_set["id"])
        self.assertEqual(Path(asset["snapshot_path"]), snapshot_path)

    def test_table_asset_api_exposes_source_snapshot_for_pdf_page_span(self) -> None:
        project, document = self._create_project_with_pdf_document("-source-snapshot")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview_with_complex_table_markdown()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=False)
        table_ref = workspace["requirements"][0]["table_refs"][0]
        table_id = table_ref["table_id"]

        asset = main.get_document_table_asset(project["id"], document["id"], table_id)
        source_snapshot = main.get_document_table_asset_source_snapshot(project["id"], document["id"], table_id)

        self.assertIn("/source-snapshot", table_ref["source_snapshot_url"])
        self.assertIn("/source-snapshot", asset["source_snapshot_url"])
        self.assertTrue(Path(source_snapshot.path).exists())
        self.assertEqual(source_snapshot.headers.get("cache-control"), "no-store, max-age=0")
        self.assertEqual(source_snapshot.headers.get("pragma"), "no-cache")
        with PILImage.open(source_snapshot.path) as image:
            self.assertGreaterEqual(image.width, 900)
            self.assertGreater(image.height, image.width)
            self.assertGreater(image.height, 1500)

    def test_table_asset_api_uses_active_preview_when_indexes_are_missing(
        self,
    ) -> None:
        project, document = self._create_project_with_pdf_document(
            "-source-snapshot-recovery"
        )
        with patch.object(
            main,
            "build_extraction_preview",
            return_value=_fake_preview_with_complex_table_markdown(),
        ):
            main.analyze_document(
                project["id"],
                main.AnalyzeRequest(document_id=document["id"]),
            )

        workspace = main.get_document_workspace(
            project["id"],
            document["id"],
            include_preview=False,
        )
        table_ref = workspace["requirements"][0]["table_refs"][0]
        table_id = table_ref["table_id"]
        active_set = db.get_active_requirement_set(document["id"])
        self.assertIsNotNone(active_set)
        indexed_asset = db.get_document_table_asset(
            project["id"],
            document["id"],
            table_id,
            requirement_set_id=active_set["id"],
        )
        self.assertIsNotNone(indexed_asset)
        cached_preview, _ = preview_cache.load_cached_preview(
            db.get_sor_document(document["id"])
        )
        active_preview = dict(cached_preview or {})
        active_preview["preview_generation_id"] = active_set[
            "preview_generation_id"
        ]
        active_preview["table_assets"] = [indexed_asset]
        preview_cache.persist_preview_cache(
            db.get_sor_document(document["id"]),
            active_preview,
        )

        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM document_table_assets WHERE sor_document_id = ?",
                (document["id"],),
            )
            conn.execute(
                "DELETE FROM requirement_set_table_assets "
                "WHERE requirement_set_id = ?",
                (active_set["id"],),
            )
            conn.execute(
                "UPDATE requirements SET table_refs = '[]' "
                "WHERE requirement_set_id = ?",
                (active_set["id"],),
            )

        source_snapshot = main.get_document_table_asset_source_snapshot(
            project["id"],
            document["id"],
            table_id,
            requirement_set_id=active_set["id"],
        )
        slice_snapshot = main.get_document_table_asset_snapshot(
            project["id"],
            document["id"],
            table_id,
            requirement_set_id=active_set["id"],
        )

        self.assertTrue(Path(source_snapshot.path).exists())
        self.assertTrue(Path(slice_snapshot.path).exists())

    def test_table_asset_api_prefers_preview_asset_over_stale_db_asset(self) -> None:
        project, document = self._create_project_with_document("-preview-asset-priority")
        stale_snapshot = self.tmp_path / "stale.png"
        stale_snapshot.write_text("old", encoding="utf-8")
        fresh_snapshot = self.tmp_path / "fresh.png"
        fresh_snapshot.write_bytes(b"fresh")
        fresh_source = self.tmp_path / "fresh_source.png"
        fresh_source.write_bytes(b"fresh-source")

        db.replace_document_table_assets(
            project["id"],
            document["id"],
            [
                {
                    "table_id": "tbl_preview_first",
                    "document_id": document["id"],
                    "headers": ["Old", "Header"],
                    "rows": [["old", "row"]],
                    "render_mode": "structured",
                    "snapshot_path": str(stale_snapshot),
                    "source_snapshot_path": str(self.tmp_path / "missing_source.png"),
                }
            ],
        )
        preview_cache.persist_preview_cache(
            db.get_sor_document(document["id"]) or document,
            {
                "summary": {"dedupe_version": "heading_requirement_v1"},
                "table_assets": [
                    {
                        "table_id": "tbl_preview_first",
                        "document_id": document["id"],
                        "headers": ["Fresh", "Header"],
                        "rows": [["fresh", "row"]],
                        "render_mode": "structured",
                        "snapshot_path": str(fresh_snapshot),
                        "source_snapshot_path": str(fresh_source),
                    }
                ],
                "complete_requirement_entries": [],
                "complete_requirement_entries_raw": [],
            },
        )

        asset = main.get_document_table_asset(project["id"], document["id"], "tbl_preview_first")
        source_snapshot = main.get_document_table_asset_source_snapshot(project["id"], document["id"], "tbl_preview_first")

        self.assertEqual(asset["headers"], ["Fresh", "Header"])
        self.assertEqual(Path(source_snapshot.path), fresh_source)

    def test_table_asset_source_snapshot_falls_back_to_persisted_asset_when_preview_source_is_missing(self) -> None:
        project, document = self._create_project_with_document("-preview-source-fallback")
        preview_snapshot = self.tmp_path / "preview.png"
        preview_snapshot.write_bytes(b"preview")
        db_snapshot = self.tmp_path / "db.png"
        db_snapshot.write_bytes(b"db")
        db_source = self.tmp_path / "db_source.png"
        db_source.write_bytes(b"db-source")

        db.replace_document_table_assets(
            project["id"],
            document["id"],
            [
                {
                    "table_id": "tbl_source_fallback",
                    "document_id": document["id"],
                    "headers": ["Persisted"],
                    "rows": [["ok"]],
                    "render_mode": "structured",
                    "snapshot_path": str(db_snapshot),
                    "source_snapshot_path": str(db_source),
                }
            ],
        )
        preview_cache.persist_preview_cache(
            db.get_sor_document(document["id"]) or document,
            {
                "summary": {"dedupe_version": "heading_requirement_v1"},
                "table_assets": [
                    {
                        "table_id": "tbl_source_fallback",
                        "document_id": document["id"],
                        "headers": ["Preview"],
                        "rows": [["broken"]],
                        "render_mode": "structured",
                        "snapshot_path": str(preview_snapshot),
                        "source_snapshot_path": str(self.tmp_path / "missing_preview_source.png"),
                    }
                ],
                "complete_requirement_entries": [],
                "complete_requirement_entries_raw": [],
            },
        )

        source_snapshot = main.get_document_table_asset_source_snapshot(project["id"], document["id"], "tbl_source_fallback")

        self.assertEqual(Path(source_snapshot.path), db_source)

    def test_table_asset_source_snapshot_does_not_fall_back_to_previous_preview_generation(self) -> None:
        project, document = self._create_project_with_document("-preview-source-generation")
        preview_snapshot = self.tmp_path / "preview-generation.png"
        preview_snapshot.write_bytes(b"preview")
        db_source = self.tmp_path / "db_generation_source.png"
        db_source.write_bytes(b"db-source")

        db.update_sor_document_preview_state(document["id"], preview_generation_id="gen-current")
        db.replace_document_table_assets(
            project["id"],
            document["id"],
            [
                {
                    "table_id": "tbl_generation_guard",
                    "document_id": document["id"],
                    "headers": ["Persisted"],
                    "rows": [["stale"]],
                    "render_mode": "structured",
                    "snapshot_path": str(self.tmp_path / "db_generation.png"),
                    "source_snapshot_path": str(db_source),
                    "preview_generation_id": "gen-old",
                }
            ],
            preview_generation_id="gen-old",
        )
        preview_cache.persist_preview_cache(
            db.get_sor_document(document["id"]) or document,
            {
                "preview_generation_id": "gen-current",
                "summary": {"dedupe_version": "heading_requirement_v1"},
                "table_assets": [
                    {
                        "table_id": "tbl_generation_guard",
                        "document_id": document["id"],
                        "headers": ["Preview"],
                        "rows": [["broken"]],
                        "render_mode": "structured",
                        "snapshot_path": str(preview_snapshot),
                        "source_snapshot_path": str(self.tmp_path / "missing_generation_source.png"),
                        "preview_generation_id": "gen-current",
                    }
                ],
                "complete_requirement_entries": [],
                "complete_requirement_entries_raw": [],
            },
        )

        with self.assertRaises(HTTPException) as detail_exc:
            main.get_document_table_asset_source_snapshot(project["id"], document["id"], "tbl_generation_guard")

        self.assertEqual(detail_exc.exception.status_code, 404)

    def test_review_entry_save_persists_table_export_variant_override(self) -> None:
        project, document = self._create_project_with_pdf_document("-table-choice")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview_with_complex_table_markdown()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))

        workspace = main.get_document_workspace(project["id"], document["id"], include_preview=False)
        row = workspace["requirements"][0]
        table_ref = dict(row["table_refs"][0], export_variant="slice")
        payload = main.ReviewedRequirementEntry(
            requirement_set_id=self._active_requirement_set_id(document["id"]),
            entry_id=row.get("entry_id") or row["req_code"],
            req_code=row["req_code"],
            entry_kind=row["entry_kind"],
            source_page=row["source_page"],
            source_page_end=row.get("source_page_end"),
            source_anchor=row["source_anchor"],
            requirement_text=row["requirement_text"],
            display_text=row["display_text"],
            raw_text=row["raw_text"],
            requirement_type=row["requirement_type"],
            priority=row["priority"],
            text_relation=row.get("text_relation") or "distinct",
            is_deduped=bool(row.get("is_deduped")),
            heading_text=row.get("heading_text") or "",
            parent_section_code=row.get("parent_section_code") or "",
            parent_section_title=row.get("parent_section_title") or "",
            parent_section_label=row.get("parent_section_label") or "",
            merged_from_req_codes=row.get("merged_from_req_codes") or [],
            merged_from_entry_ids=row.get("merged_from_entry_ids") or [],
            dedupe_version=row.get("dedupe_version") or "",
            table_refs=[table_ref],
            content_blocks=row.get("content_blocks") or [],
            has_table_asset=bool(row.get("has_table_asset")),
            table_render_mode=row.get("table_render_mode") or "",
            keep=True,
        )

        main.save_review_entry(project["id"], document["id"], payload)
        refreshed = main.get_document_workspace(project["id"], document["id"], include_preview=False)
        refreshed_ref = refreshed["requirements"][0]["table_refs"][0]

        self.assertEqual(refreshed_ref["export_variant"], "slice")
        self.assertTrue(refreshed_ref["export_variant_overridden"])
        self.assertEqual(refreshed_ref["recommended_export_variant"], "source")

    def test_preview_recommendation_replaces_non_overridden_persisted_variant(self) -> None:
        merged_refs, _, _, _ = main._merge_preview_table_assets_for_review_row(
            preview_row={
                "table_refs": [
                    {
                        "table_id": "tbl_preview",
                        "recommended_export_variant": "source",
                        "export_variant": "source",
                        "export_variant_overridden": False,
                    }
                ],
                "content_blocks": [{"kind": "table", "table_id": "tbl_preview"}],
                "has_table_asset": True,
                "table_render_mode": "structured",
            },
            persisted_row={
                "table_refs": [
                    {
                        "table_id": "tbl_preview",
                        "recommended_export_variant": "slice",
                        "export_variant": "slice",
                        "export_variant_overridden": False,
                    }
                ],
                "content_blocks": [{"kind": "table", "table_id": "tbl_preview"}],
                "has_table_asset": True,
                "table_render_mode": "structured",
            },
        )

        self.assertEqual(merged_refs[0]["export_variant"], "source")
        self.assertFalse(merged_refs[0]["export_variant_overridden"])

    def test_row_slice_bbox_matches_merged_cell_slice_with_shared_prefix_and_suffix(self) -> None:
        candidate = {
            "bbox": (0.0, 0.0, 240.0, 250.0),
            "rows_text": [
                ["7", "机械振动和冲击", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "5", "C Sample"],
                ["连接器或端子插拔 Connector and/or Terminal Cycling", "USCAR-2 Rev 6 5.1.7", "完成一对连接器或端子的10次插拔", "连接器无变形等外观损伤", "PV", "5", "C Sample"],
                ["干电路电阻 Dry Circuit Resistance", "U5SC3AR12REV6", "测量和记录之间的电阻", "电阻值满足5.3.2.4的要求", "PV", "5", "C Sample"],
                ["电压降 Voltage Drop", "U5SC3AR22REV6", "测量并记录测试点之间的毫伏电压降", "电压降满足5.3.2.4的要求", "PV", "5", "C Sample"],
                ["外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "5", "C Sample"],
            ],
            "row_bboxes": [
                (0.0, 0.0, 240.0, 50.0),
                (0.0, 50.0, 240.0, 100.0),
                (0.0, 100.0, 240.0, 150.0),
                (0.0, 150.0, 240.0, 200.0),
                (0.0, 200.0, 240.0, 250.0),
            ],
        }

        bbox = table_assets._match_row_slice_bbox(
            candidate,
            headers=[],
            slice_rows=[
                ["7", "机械振动和冲击", "连接器或端子插拔 Connector and/or Terminal Cycling", "USCAR-2 Rev 6 5.1.7", "完成一对连接器或端子的10次插拔", "连接器无变形等外观损伤", "PV", "5", "C Sample"],
                ["7", "机械振动和冲击", "干电路电阻 Dry Circuit Resistance", "USCAR-2 Rev 6 5.3.1", "测量和记录之间的电阻", "电阻值满足5.3.2.4的要求", "PV", "5", "C Sample"],
                ["7", "机械振动和冲击", "电压降 Voltage Drop", "USCAR-2 Rev 6 5.3.2", "测量并记录测试点之间的毫伏电压降", "电压降满足5.3.2.4的要求", "PV", "5", "C Sample"],
                ["7", "机械振动和冲击", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "5", "C Sample"],
            ],
        )

        self.assertEqual(bbox, (0.0, 50.0, 240.0, 250.0))

    def test_row_slice_bbox_includes_first_group_row_when_slice_starts_with_item_one(self) -> None:
        candidate = {
            "bbox": (0.0, 0.0, 240.0, 200.0),
            "rows_text": [
                ["1", "连接器结合力", "外观 Visual Inspection", "USCAR-2 Rev 6 5.1.8", "目视检查", "无外观损伤", "PV", "15/OTS"],
                ["连接器插入力 Connector-Connector Mating Force", "U5SC4AR22REV6", "插拔速度50mm/Min", "插入力 / Engage force：75N Max.", "PV", "15/OTS"],
                ["连接器拔出力 Connector-Connector Unmating Force", "U5SC4AR22REV6", "破坏卡扣插拔速度50mm/Min", "拔出力 / Disengage force：15N~75N", "PV", "5/OTS"],
                ["主锁解锁力 Lock Deflection", "U5SC4AR22REV6", "向主锁施加解锁力至最大51N", "解锁力 / Unlock force：6N~51N", "PV", "5/OTS"],
            ],
            "row_bboxes": [
                (0.0, 0.0, 240.0, 50.0),
                (0.0, 50.0, 240.0, 100.0),
                (0.0, 100.0, 240.0, 150.0),
                (0.0, 150.0, 240.0, 200.0),
            ],
        }

        bbox = table_assets._match_row_slice_bbox(
            candidate,
            headers=[],
            slice_rows=[
                ["1", "连接器结合力", "连接器插入力 Connector-Connector Mating Force", "USCAR-2 Rev 6 5.4.2", "插拔速度50mm/Min", "插入力 / Engage force：75N Max.", "PV", "15/OTS"],
                ["1", "连接器结合力", "连接器拔出力 Connector-Connector Unmating Force", "USCAR-2 Rev 6 5.4.2", "破坏卡扣插拔速度50mm/Min", "拔出力 / Disengage force：15N~75N", "PV", "5/OTS"],
                ["1", "连接器结合力", "主锁解锁力 Lock Deflection", "USCAR-2 Rev 6 5.4.2", "向主锁施加解锁力至最大51N", "解锁力 / Unlock force：6N~51N", "PV", "5/OTS"],
            ],
        )

        self.assertEqual(bbox, (0.0, 0.0, 240.0, 200.0))

    def test_row_slice_bbox_uses_full_section_span_for_blank_leading_continuations(self) -> None:
        candidate = {
            "bbox": (0.0, 0.0, 240.0, 200.0),
            "rows_text": [
                ["62", "耐化学试液", "USCAR-2 Rev 6 5.1.8", "目视检查"],
                [None, None, "USCAR-2 Rev 6 5.5.1", "绝缘电阻"],
                [None, None, "USCAR-2 Rev 6 5.6.4", "耐化学试液"],
                [None, None, "USCAR-2 Rev 6 5.5.1", "绝缘电阻"],
            ],
            "row_bboxes": [
                (0.0, 0.0, 240.0, 50.0),
                (0.0, 50.0, 240.0, 100.0),
                (0.0, 100.0, 240.0, 150.0),
                (0.0, 150.0, 240.0, 200.0),
            ],
        }

        bbox = table_assets._match_row_slice_bbox(
            candidate,
            headers=["62", "耐化学试液", "USCAR-2 Rev 6 5.1.8", "目视检查"],
            slice_rows=[
                ["62", "耐化学试液", "USCAR-2 Rev 6 5.5.1", "绝缘电阻"],
                ["62", "耐化学试液", "USCAR-2 Rev 6 5.6.4", "耐化学试液"],
                ["62", "耐化学试液", "USCAR-2 Rev 6 5.5.1", "绝缘电阻"],
            ],
        )

        self.assertEqual(bbox, (0.0, 0.0, 240.0, 200.0))

    def test_row_slice_bbox_skips_full_standalone_table_sequences(self) -> None:
        candidate = {
            "bbox": (0.0, 0.0, 240.0, 250.0),
            "rows_text": [
                ["1", "基本尺寸", "要求一"],
                ["2", "电磁阀", "要求二"],
                ["3", "空簧接口", "要求三"],
                ["4", "下叉臂接口", "要求四"],
                ["5", "活塞杆接口", "要求五"],
            ],
            "row_bboxes": [
                (0.0, 0.0, 240.0, 50.0),
                (0.0, 50.0, 240.0, 100.0),
                (0.0, 100.0, 240.0, 150.0),
                (0.0, 150.0, 240.0, 200.0),
                (0.0, 200.0, 240.0, 250.0),
            ],
        }

        bbox = table_assets._match_row_slice_bbox(
            candidate,
            headers=["序号", "项目", "要求"],
            slice_rows=[
                ["1", "基本尺寸", "要求一"],
                ["2", "电磁阀", "要求二"],
                ["3", "空簧接口", "要求三"],
                ["4", "下叉臂接口", "要求四"],
                ["5", "活塞杆接口", "要求五"],
            ],
        )

        self.assertIsNone(bbox)

    def test_context_anchor_prefers_later_page_for_before_context(self) -> None:
        page_lines = {
            0: [
                {
                    "text": "[√]前减振器、蓄能器等需供应商发货",
                    "norm": "前减振器蓄能器等需供应商发货",
                    "bbox": (0.0, 500.0, 120.0, 510.0),
                }
            ],
            1: [
                {
                    "text": "前减振器",
                    "norm": "前减振器",
                    "bbox": (0.0, 180.0, 80.0, 190.0),
                }
            ],
        }

        def fake_load_lines(document, *, source_key, page_index, detection_cache):
            return page_lines.get(page_index, [])

        with patch.object(table_assets, "_load_pdf_page_line_records", side_effect=fake_load_lines):
            anchor = table_assets._find_context_anchor(
                object(),
                source_key="dummy",
                page_indices=[0, 1],
                candidates=table_assets._build_context_anchor_fragments("前减振器", from_end=True),
                detection_cache={},
                prefer_last=True,
            )

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["page_index"], 1)
        self.assertEqual(anchor["text"], "前减振器")

    def test_context_anchor_prefers_later_exact_self_anchor_over_earlier_noise(self) -> None:
        page_lines = {
            0: [
                {
                    "text": "[√]前减振器、蓄能器等需供应商发货",
                    "norm": "前减振器蓄能器等需供应商发货",
                    "bbox": (0.0, 500.0, 120.0, 510.0),
                }
            ],
            1: [
                {
                    "text": "前减振器",
                    "norm": "前减振器",
                    "bbox": (0.0, 180.0, 80.0, 190.0),
                }
            ],
        }

        def fake_load_lines(document, *, source_key, page_index, detection_cache):
            return page_lines.get(page_index, [])

        with patch.object(table_assets, "_load_pdf_page_line_records", side_effect=fake_load_lines):
            anchor = table_assets._find_context_anchor(
                object(),
                source_key="dummy",
                page_indices=[0, 1],
                candidates=table_assets._build_current_entry_anchor_fragments(
                    table_text="|1|2|2905100 X 8D1110|0.0|左前减振器总成|",
                    table_title="前减振器",
                    before_context_text="前减振器",
                ),
                detection_cache={},
                prefer_score_across_pages=True,
            )

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor["page_index"], 1)
        self.assertEqual(anchor["text"], "前减振器")

    def test_context_anchor_fragments_include_short_label_prefix(self) -> None:
        fragments = table_assets._build_context_anchor_fragments(
            "备注：0、该表格零部件号与OEM系统内零部件号一致。",
            from_end=False,
        )
        values = [value for value, _ in fragments]

        self.assertIn("备注", values)

    def test_preview_promotes_local_reset_clause_heading(self) -> None:
        self.assertTrue(
            extraction_preview._should_promote_local_clause_to_section(
                parent_section_code="3.2.2",
                clause_code="6.6",
            )
        )
        self.assertTrue(
            extraction_preview._should_promote_local_clause_to_section(
                parent_section_code="3.2",
                clause_code="3.2.1",
            )
        )

    def test_fidelity_section_count_skips_main_doc_synthetic_local_split(self) -> None:
        self.assertFalse(
            extraction_preview.counts_toward_fidelity_section(
                {
                    "section_code": "6.1",
                    "appendix_scope": "",
                    "structural_kind": "synthetic",
                }
            )
        )
        self.assertTrue(
            extraction_preview.counts_toward_fidelity_section(
                {
                    "section_code": "6",
                    "appendix_scope": "",
                    "structural_kind": "outline",
                }
            )
        )
        self.assertTrue(
            extraction_preview.counts_toward_fidelity_section(
                {
                    "section_code": "1",
                    "appendix_scope": "SUB1",
                    "structural_kind": "synthetic",
                }
            )
        )

    def test_split_section_key_preserves_local_code_for_scoped_clause_keys(self) -> None:
        self.assertEqual(extraction_preview._split_section_key("3.2.2::11.12"), ("", "11.12"))
        self.assertEqual(extraction_preview._split_section_key("SUB1:6::10"), ("SUB1", "10"))

    def test_preview_keeps_standard_ids_inside_parent_section(self) -> None:
        rows = extraction_preview._split_requirement_preview_rows(
            item={
                "req_code": "REQ-0001",
                "source_anchor": "Page 7 | Section 4.4 依据标准",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
                "source_line_index": 15,
                "source_line_index_end": 18,
            },
            source_page=7,
            source_page_end=7,
            appendix_scope="",
            section_code="4.4",
            section_title_hint="依据标准",
            explicit_section_code="4.4",
            text=(
                "4.4 依据标准\n"
                "GB/T 30512 汽车禁用物质要求\n"
                "GB/T 28046.5 道路车辆电气及电子设备的环境条件和试验第5部分：化学负荷\n"
                "GB/T 28046.4 道路车辆电气及电子设备的环境条件和试验第4部分：气候负荷"
            ),
            req_order_start=0,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["section_code"], "4.4")
        self.assertIn("28046.5", rows[0]["requirement_text"])
        self.assertIn("28046.4", rows[0]["requirement_text"])

    def test_serial_table_bare_numeric_rows_are_not_promoted_to_sections(self) -> None:
        line_entries = [
            {
                "text": "3.2.2 产品详细描述",
                "source_page": 1,
                "source_anchor": "Page 1",
                "table_count": 0,
                "source_line_index": 0,
                "global_line_index": 0,
            },
            {
                "text": "2）后减振器布置要求",
                "source_page": 1,
                "source_anchor": "Page 1",
                "table_count": 0,
                "source_line_index": 1,
                "global_line_index": 1,
            },
            {
                "text": "|序号|项目|要求|",
                "source_page": 1,
                "source_anchor": "Page 1",
                "table_count": 1,
                "source_line_index": 2,
                "global_line_index": 2,
            },
            {
                "text": "12 减振器强度要求",
                "source_page": 1,
                "source_anchor": "Page 1",
                "table_count": 1,
                "source_line_index": 3,
                "global_line_index": 3,
            },
            {
                "text": "3）减振器阻尼力基础要求",
                "source_page": 1,
                "source_anchor": "Page 1",
                "table_count": 0,
                "source_line_index": 4,
                "global_line_index": 4,
            },
        ]

        blocks = extractor._extract_section_blocks(line_entries)

        self.assertNotIn("12", [str(block.get("section_code") or "") for block in blocks])

    def test_merge_duplicate_section_block_recomputes_line_indices_from_entries(self) -> None:
        base = {
            "appendix_scope": "",
            "section_code": "6.9",
            "section_title": "商务报价",
            "section_level": 2,
            "_discovery_order": 1,
            "source_page": 9,
            "source_page_end": 9,
            "source_line_index": 0,
            "source_line_index_end": 20,
            "entries": [
                {"source_page": 9, "source_line_index": 18, "text": "6.9 商务报价"},
                {"source_page": 9, "source_line_index": 19, "text": "产过程费用"},
                {"source_page": 9, "source_line_index": 20, "text": "包含以上所有费用。"},
            ],
        }
        incoming = {
            "appendix_scope": "",
            "section_code": "6.9",
            "section_title": "商务报价",
            "section_level": 2,
            "_discovery_order": 2,
            "source_page": 9,
            "source_page_end": 9,
            "source_line_index": 18,
            "source_line_index_end": 20,
            "entries": [
                {"source_page": 9, "source_line_index": 18, "text": "6.9 商务报价"},
                {"source_page": 9, "source_line_index": 19, "text": "产过程费用"},
                {"source_page": 9, "source_line_index": 20, "text": "包含以上所有费用。"},
            ],
        }

        merged = extractor._merge_duplicate_section_block(base, incoming)

        self.assertEqual(merged["source_line_index"], 18)
        self.assertEqual(merged["source_line_index_end"], 20)

    def test_numbered_outline_rows_prefer_row_slice_snapshot(self) -> None:
        self.assertFalse(
            table_assets._should_prefer_source_clause_window(
                table_title="12 活塞杆弯曲强度",
                render_mode="snapshot_only",
                headers=["序号", "项目", "参考标准", "试验方法", "要求"],
                rows=[["12", "活塞杆弯曲强度", "-", "台架", "通过"]],
                source_page=11,
                source_page_end=11,
                notes=[],
                trailing_context_text="",
            )
        )

    def test_visual_snapshot_infers_numbered_outline_row_slice(self) -> None:
        self.assertEqual(
            table_assets._derive_visual_snapshot_slice_rows("12 活塞杆弯曲强度"),
            [["12", "活塞杆弯曲强度"]],
        )

    def test_trial_outline_single_row_header_inference_applies_to_canonical_appendix_scope(self) -> None:
        headers = table_assets._infer_contextual_single_row_header(
            lines=[
                "|12|活塞杆弯曲强度|-|台架试验|通过|DV/PV|供应商|6件/OTS|1天|"
            ],
            row={
                "appendix_scope": "SUB1",
                "appendix_title": "主动前减振器试验大纲",
                "parent_section_code": "12",
                "parent_section_title": "主动前减振器试验大纲",
                "source_anchor": "Appendix SUB1 | 主动前减振器试验大纲",
            },
            table_title="12 活塞杆弯曲强度",
        )

        self.assertEqual(
            headers,
            ["序号", "项目", "参考标准", "试验方法", "要求", "DV/PV", "责任方", "样件", "周期"],
        )

    def test_canonical_section_tree_keeps_parent_before_child_without_direct_requirements(self) -> None:
        sections, stats = extraction_preview._build_canonical_section_rows(
            [
                {
                    "appendix_scope": "",
                    "section_code": "1",
                    "section_key": "1",
                    "section_instance_key": "1",
                    "section_title": "概述",
                    "section_level": 1,
                    "parent_section_code": "",
                    "source_page_start": 1,
                    "source_page_end": 1,
                    "source_line_index_start": 13,
                    "source_line_index_end": 13,
                    "has_children": True,
                    "is_leaf": False,
                    "requirement_count": 0,
                    "first_req_order": None,
                    "outline_order": 0,
                },
                {
                    "appendix_scope": "",
                    "section_code": "1.1",
                    "section_key": "1.1",
                    "section_instance_key": "1.1",
                    "section_title": "范围",
                    "section_level": 2,
                    "parent_section_code": "1",
                    "source_page_start": 1,
                    "source_page_end": 1,
                    "source_line_index_start": 14,
                    "source_line_index_end": 14,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 0,
                    "outline_order": 1,
                },
                {
                    "appendix_scope": "",
                    "section_code": "1.5",
                    "section_key": "1.5",
                    "section_instance_key": "1.5",
                    "section_title": "其他",
                    "section_level": 2,
                    "parent_section_code": "1",
                    "source_page_start": 1,
                    "source_page_end": 1,
                    "source_line_index_start": 34,
                    "source_line_index_end": 34,
                    "has_children": True,
                    "is_leaf": False,
                    "requirement_count": 0,
                    "first_req_order": None,
                    "outline_order": 5,
                },
                {
                    "appendix_scope": "",
                    "section_code": "1.5.1",
                    "section_key": "1.5.1",
                    "section_instance_key": "1.5.1",
                    "section_title": "简体中文",
                    "section_level": 3,
                    "parent_section_code": "1.5",
                    "source_page_start": 1,
                    "source_page_end": 1,
                    "source_line_index_start": 35,
                    "source_line_index_end": 35,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 4,
                    "outline_order": 6,
                },
            ]
        )

        self.assertEqual([row["section_code"] for row in sections], ["1", "1.1", "1.5", "1.5.1"])
        self.assertEqual([row["tree_order"] for row in sections], [0, 1, 2, 3])
        self.assertEqual(sections[1]["structural_parent_key"], "1")
        self.assertEqual(sections[3]["structural_parent_key"], "1.5")
        self.assertEqual(stats["zero_direct_requirement_parent_count"], 2)

    def test_canonical_section_tree_preserves_outline_order_over_stale_line_indices(self) -> None:
        sections, _ = extraction_preview._build_canonical_section_rows(
            [
                {
                    "appendix_scope": "",
                    "section_code": "5.8",
                    "section_key": "5.8",
                    "section_instance_key": "5.8",
                    "section_title": "禁用物质要求",
                    "section_level": 2,
                    "parent_section_code": "5",
                    "source_page_start": 9,
                    "source_page_end": 9,
                    "source_line_index_start": 2,
                    "source_line_index_end": 2,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 62,
                    "outline_order": 37,
                },
                {
                    "appendix_scope": "",
                    "section_code": "6.9",
                    "section_key": "6.9",
                    "section_instance_key": "6.9",
                    "section_title": "商务报价",
                    "section_level": 2,
                    "parent_section_code": "6",
                    "source_page_start": 9,
                    "source_page_end": 9,
                    "source_line_index_start": 0,
                    "source_line_index_end": 0,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 71,
                    "outline_order": 47,
                },
            ]
        )

        self.assertEqual([row["section_code"] for row in sections], ["5.8", "6.9"])

    def test_canonical_section_tree_attaches_scoped_local_clauses_under_formal_parent(self) -> None:
        sections, _ = extraction_preview._build_canonical_section_rows(
            [
                {
                    "appendix_scope": "",
                    "section_code": "3.2.2",
                    "section_key": "3.2.2",
                    "section_instance_key": "3.2.2",
                    "section_title": "产品详细描述",
                    "section_level": 3,
                    "parent_section_code": "3.2",
                    "parent_section_key": "3.2",
                    "source_page_start": 2,
                    "source_page_end": 8,
                    "source_line_index_start": 0,
                    "source_line_index_end": 72,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 14,
                    "outline_order": 21,
                },
                {
                    "appendix_scope": "",
                    "section_code": "1",
                    "section_key": "3.2.2::1",
                    "section_instance_key": "3.2.2::1",
                    "section_title": "油泵性能要求",
                    "section_label": "1）",
                    "section_level": 1,
                    "parent_section_code": "3.2.2",
                    "parent_section_key": "3.2.2",
                    "source_page_start": 3,
                    "source_page_end": 3,
                    "source_line_index_start": 1,
                    "source_line_index_end": 1,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 15,
                    "outline_order": None,
                },
                {
                    "appendix_scope": "",
                    "section_code": "6",
                    "section_key": "3.2.2::6",
                    "section_instance_key": "3.2.2::6",
                    "section_title": "双电机控制器总成要求",
                    "section_label": "6）",
                    "section_level": 1,
                    "parent_section_code": "3.2.2",
                    "parent_section_key": "3.2.2",
                    "source_page_start": 5,
                    "source_page_end": 5,
                    "source_line_index_start": 20,
                    "source_line_index_end": 20,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 20,
                    "outline_order": None,
                },
                {
                    "appendix_scope": "",
                    "section_code": "6.1",
                    "section_key": "3.2.2::6.1",
                    "section_instance_key": "3.2.2::6.1",
                    "section_title": "简述",
                    "section_label": "6.1）",
                    "section_level": 2,
                    "parent_section_code": "6",
                    "parent_section_key": "3.2.2::6",
                    "source_page_start": 5,
                    "source_page_end": 5,
                    "source_line_index_start": 21,
                    "source_line_index_end": 21,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 21,
                    "outline_order": None,
                },
                {
                    "appendix_scope": "",
                    "section_code": "6.6",
                    "section_key": "3.2.2::6.6",
                    "section_instance_key": "3.2.2::6.6",
                    "section_title": "模式管理",
                    "section_label": "6.6）",
                    "section_level": 2,
                    "parent_section_code": "6",
                    "parent_section_key": "3.2.2::6",
                    "source_page_start": 6,
                    "source_page_end": 6,
                    "source_line_index_start": 10,
                    "source_line_index_end": 10,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 26,
                    "outline_order": None,
                },
                {
                    "appendix_scope": "",
                    "section_code": "6.6.4",
                    "section_key": "3.2.2::6.6.4",
                    "section_instance_key": "3.2.2::6.6.4",
                    "section_title": "OTA要求",
                    "section_label": "6.6.4）",
                    "section_level": 3,
                    "parent_section_code": "6.6",
                    "parent_section_key": "3.2.2::6.6",
                    "source_page_start": 7,
                    "source_page_end": 7,
                    "source_line_index_start": 1,
                    "source_line_index_end": 1,
                    "has_children": False,
                    "is_leaf": True,
                    "requirement_count": 1,
                    "first_req_order": 30,
                    "outline_order": None,
                },
            ]
        )

        by_key = {row["section_key"]: row for row in sections}
        self.assertEqual(by_key["3.2.2::1"]["structural_parent_key"], "3.2.2")
        self.assertEqual(by_key["3.2.2::6"]["structural_parent_key"], "3.2.2")
        self.assertEqual(by_key["3.2.2::6.1"]["structural_parent_key"], "3.2.2::6")
        self.assertEqual(by_key["3.2.2::6.6.4"]["structural_parent_key"], "3.2.2::6.6")
        self.assertTrue(by_key["3.2.2"]["has_children"])
        self.assertFalse(by_key["3.2.2"]["is_leaf"])

    def test_preview_section_sort_key_uses_tree_order_when_available(self) -> None:
        earlier = extraction_preview._preview_section_row_sort_key(
            {
                "section_code": "1",
                "section_key": "1",
                "tree_order": 0,
                "source_page_start": 1,
                "source_line_index_start": 13,
                "first_req_order": None,
                "outline_order": 0,
                "section_level": 1,
                "structural_kind": "outline",
            }
        )
        later = extraction_preview._preview_section_row_sort_key(
            {
                "section_code": "1.1",
                "section_key": "1.1",
                "tree_order": 1,
                "source_page_start": 1,
                "source_line_index_start": 14,
                "first_req_order": 0,
                "outline_order": 1,
                "section_level": 2,
                "structural_kind": "outline",
            }
        )

        self.assertLess(earlier, later)

    def test_table_ref_snapshot_prefers_source_variant(self) -> None:
        slice_path = self.tmp_path / "slice.png"
        source_path = self.tmp_path / "source.png"
        PILImage.new("RGB", (120, 120), "red").save(slice_path)
        PILImage.new("RGB", (280, 90), "blue").save(source_path)

        buffer, image_width, image_height, warning = exporter._table_ref_snapshot(
            {
                "export_variant": "source",
                "slice_render_mode": "snapshot_only",
                "slice_snapshot_path": str(slice_path),
                "source_snapshot_path": str(source_path),
                "inherited_headers": [],
                "slice_rows": [],
            },
            640,
        )

        self.assertIsNotNone(buffer)
        self.assertEqual((image_width, image_height), (280, 90))
        self.assertEqual(warning, "")

    def test_single_row_pipe_table_slice_is_materialized_as_table_asset(self) -> None:
        project, document = self._create_project_with_document("-single-row-slice")
        with patch.object(main, "build_extraction_preview", return_value=_fake_preview_with_single_row_slice_table()):
            main.analyze_document(project["id"], main.AnalyzeRequest(document_id=document["id"]))
            workspace = main.get_document_workspace(project["id"], document["id"], include_preview=True)

        requirement = workspace["requirements"][0]
        self.assertTrue(requirement["has_table_asset"])
        self.assertEqual(requirement["table_render_mode"], "structured")
        self.assertEqual(len(requirement["table_refs"]), 1)
        self.assertEqual(
            requirement["table_refs"][0]["inherited_headers"],
            ["序号", "项目", "参考标准", "试验方法", "要求", "DV/PV", "责任方", "样件", "周期"],
        )
        self.assertEqual(
            requirement["table_refs"][0]["slice_rows"],
            [["1", "外观检查", "-", "目视外观检查", "表面无划伤", "DV/PV", "供应商", "6件/OTS", "1天"]],
        )

    def test_build_analysis_rows_from_preview_skips_complex_review_only_summary(self) -> None:
        preview = {
            "complete_requirement_entries": [
                {
                    "entry_id": "REQ-CX-summary",
                    "req_code": "REQ-CX-summary",
                    "entry_kind": "REQUIREMENT",
                    "display_text": "复杂跨页表整表总览",
                    "raw_text": "复杂跨页表整表总览",
                    "requirement_text": "复杂跨页表整表总览",
                    "source_page": 17,
                    "source_page_end": 22,
                    "complex_table_mode": True,
                    "complex_role": "zone_summary",
                    "complex_row_policy": "review_only",
                },
                _complex_group_review_row(source_snapshot_path=str(self.tmp_path / "source.png")),
            ]
        }

        rows = main._build_analysis_rows_from_preview(preview)

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["complex_table_mode"])
        self.assertEqual(rows[0]["complex_role"], "group")

    def test_workspace_suppresses_fallback_rows_shadowed_by_complex_group(self) -> None:
        swallowed = {
            "req_code": "REQ-LEGACY",
            "entry_id": "REQ-LEGACY",
            "entry_kind": "REQUIREMENT",
            "source_page": 18,
            "source_page_end": 18,
            "source_anchor": "Page 18 | legacy",
            "display_text": "legacy split row",
            "raw_text": "legacy split row",
            "requirement_text": "legacy split row",
        }
        unrelated_same_page = {
            "req_code": "REQ-LEGIT",
            "entry_id": "REQ-LEGIT",
            "entry_kind": "REQUIREMENT",
            "source_page": 18,
            "source_page_end": 18,
            "source_anchor": "Page 18 | legit",
            "section_code": "8.1",
            "display_text": "legit boundary row",
            "raw_text": "legit boundary row",
            "requirement_text": "legit boundary row",
        }
        complex_row = _complex_group_review_row(
            source_snapshot_path=str(self.tmp_path / "source.png"),
            shadow_rows=[swallowed],
        )

        rows, _ = main._build_workspace_requirements_from_review_entries(
            [complex_row],
            fallback_requirements=[swallowed, unrelated_same_page],
            preview=None,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["req_code"], complex_row["req_code"])
        self.assertTrue(any(row["req_code"] == "REQ-LEGIT" for row in rows))

    def test_workspace_review_entries_skip_rows_marked_keep_false(self) -> None:
        excluded_heading = {
            "entry_id": "H-1",
            "req_code": "H-1",
            "entry_kind": "HEADING",
            "keep": False,
            "source_page": 1,
            "source_page_end": 1,
            "source_anchor": "Main > 1",
            "display_text": "1 概述",
            "raw_text": "1 概述",
            "requirement_text": "1 概述",
            "parent_section_code": "1",
            "parent_section_title": "概述",
            "parent_section_label": "1 概述",
        }
        kept_requirement = {
            "entry_id": "REQ0024",
            "req_code": "REQ0024",
            "entry_kind": "REQUIREMENT",
            "keep": True,
            "source_page": 1,
            "source_page_end": 1,
            "source_anchor": "Main > 1.1",
            "display_text": "5.1 首条需求",
            "raw_text": "5.1 首条需求",
            "requirement_text": "5.1 首条需求",
            "parent_section_code": "1.1",
            "parent_section_title": "首条需求",
            "parent_section_label": "1.1 首条需求",
            "requirement_type": "FUNCTIONAL",
            "priority": "MEDIUM",
        }

        rows, _ = main._build_workspace_requirements_from_review_entries(
            [excluded_heading, kept_requirement],
            fallback_requirements=[],
            preview=None,
        )

        self.assertEqual([row["req_code"] for row in rows], ["REQ0024"])
        self.assertTrue(all(bool(row.get("keep", True)) for row in rows))
        self.assertTrue(all(str(row.get("entry_kind") or "") != "HEADING" for row in rows))

    def test_save_review_entry_preserves_complex_metadata_via_table_refs(self) -> None:
        project, document = self._create_project_with_document("-complex-save")
        requirement_set = db.create_requirement_set(
            project["id"],
            document["id"],
        )
        db.activate_requirement_set(
            project["id"],
            document["id"],
            requirement_set["id"],
        )
        source_path = self.tmp_path / "complex-source.png"
        PILImage.new("RGB", (320, 180), "navy").save(source_path)

        payload = main.ReviewedRequirementEntry(
            requirement_set_id=self._active_requirement_set_id(document["id"]),
            **_complex_group_review_row(source_snapshot_path=str(source_path)),
        )
        result = main.save_review_entry(project["id"], document["id"], payload)

        self.assertEqual(result["review_save"]["action"], "inserted")
        self.assertTrue(result["saved_entry"]["complex_table_mode"])
        self.assertEqual(result["saved_entry"]["complex_zone_id"], "cx-merged-requirement-matrix-p17-p22")

        persisted = db.list_requirements(project["id"], document["id"])
        self.assertEqual(len(persisted), 1)
        self.assertTrue(persisted[0]["complex_table_mode"])
        self.assertTrue(persisted[0]["table_refs"][0]["complex_table_mode"])
        self.assertTrue(persisted[0]["table_refs"][0]["export_variant_locked"])
        self.assertEqual(len(db.list_document_table_assets(project["id"], document["id"])), 1)
        asset = main.get_document_table_asset(project["id"], document["id"], "tbl_complex_group")
        self.assertEqual(asset["table_id"], "tbl_complex_group")
        source_snapshot = main.get_document_table_asset_source_snapshot(project["id"], document["id"], "tbl_complex_group")
        self.assertTrue(Path(source_snapshot.path).exists())

    def test_run_ai_analysis_persists_extended_assessment_fields_and_runs(self) -> None:
        project, document = self._create_project_with_document("-ai-copilot")
        table_refs = json.dumps(
            [
                {
                    "table_id": "tbl_ai",
                    "slice_ai_projection": "|Temperature|85C|Voltage|12V|",
                }
            ],
            ensure_ascii=False,
        )
        content_blocks = json.dumps(
            [{"kind": "paragraph", "text": "适用于整车冷启动工况。"}],
            ensure_ascii=False,
        )
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 5.1",
            requirement_text="ECU shall support wake-up time less than 100ms.",
            raw_text="ECU shall support wake-up time less than 100ms.",
            display_text="ECU shall support wake-up time less than 100ms.",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="5.1",
            parent_section_title="Startup",
            parent_section_label="5.1 Startup",
            table_refs=table_refs,
            content_blocks=content_blocks,
            has_table_asset=True,
            table_render_mode="structured",
        )
        db.add_knowledge_document(
            project_id=project["id"],
            file_name="platform-spec.txt",
            file_type="txt",
            file_path=str(self.tmp_path / "platform-spec.txt"),
            extracted_text=(
                "5.1 Startup\n"
                "The ECU shall support wake-up time less than 100ms.\n"
                "Measured wake-up time is 80ms at -20C.\n"
            ),
            doc_version="v1.0",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        self._lock_active_requirement_set(project["id"], document["id"])

        with patch.dict(
            os.environ,
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "heuristic",
                "REQPILOT_AI_TRACE_DIR": str(self.tmp_path / "ai_traces"),
            },
            clear=False,
        ):
            result = main.run_document_ai_analysis(
                project["id"],
                document["id"],
                self._active_requirement_set_id(document["id"]),
            )

        row = next(item for item in result["requirements"] if item["id"] == requirement["id"])
        self.assertTrue(row["ai_decision"])
        self.assertEqual(row["assessment_mode"], "heuristic")
        self.assertEqual(row["analysis_label"], "REQUIREMENT")
        self.assertTrue(row["normalized_requirement"])
        self.assertIn(row["risk_level"], {"LOW", "MEDIUM", "HIGH"})
        self.assertEqual(
            row["compliance_status"],
            {
                "ACCEPT": "COMPLIANT",
                "DEVIATION_ACCEPT": "PARTIALLY_COMPLIANT",
                "REJECT": "NON_COMPLIANT",
                "NEED_CLARIFICATION": "INSUFFICIENT_INFORMATION",
            }[row["ai_decision"]],
        )
        self.assertTrue(row["sub_judgments"])
        if row["compliance_status"] == "INSUFFICIENT_INFORMATION":
            self.assertTrue(
                row["structured_information_gap"]["missing_items"]
            )
        self.assertIn("source_doc_version", row["evidences"][0])
        self.assertTrue(row["evidences"][0]["retrieval_evidence_id"])
        self.assertEqual(len(row["evidences"][0]["content_hash"]), 64)
        self.assertTrue(row["evidences"][0]["evidence_metadata"])

        runs = db.list_assessment_runs(requirement["id"])
        self.assertEqual(len(runs), 2)
        self.assertEqual({run["mode"] for run in runs}, {"screening_heuristic", "heuristic"})
        backend_by_mode = {run["mode"]: run["retrieval_backend"] for run in runs}
        self.assertEqual(backend_by_mode["screening_heuristic"], "screening_only")
        self.assertEqual(backend_by_mode["heuristic"], "local")

    def test_ai_analysis_job_api_exposes_status_events_and_requirement_detail(self) -> None:
        project, document = self._create_project_with_document("-ai-job-api")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 5.1",
            requirement_text="ECU shall support wake-up time less than 100ms.",
            raw_text="ECU shall support wake-up time less than 100ms.",
            display_text="ECU shall support wake-up time less than 100ms.",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="5.1",
            parent_section_title="Startup",
            parent_section_label="5.1 Startup",
        )
        db.add_knowledge_document(
            project_id=project["id"],
            file_name="platform-spec.txt",
            file_type="txt",
            file_path=str(self.tmp_path / "platform-spec-job-api.txt"),
            extracted_text=(
                "5.1 Startup\n"
                "The ECU shall support wake-up time less than 100ms.\n"
                "Measured wake-up time is 80ms at -20C.\n"
            ),
            doc_version="v1.0",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        self._lock_active_requirement_set(project["id"], document["id"])

        with patch.dict(
            os.environ,
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "heuristic",
                "REQPILOT_AI_TRACE_DIR": str(self.tmp_path / "ai_traces_job_api"),
            },
            clear=False,
        ):
            with patch.object(main, "Thread", ImmediateThread):
                with TestClient(main.app) as client:
                    start_response = client.post(
                        f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                        json=self._ai_start_payload(
                            document["id"],
                            profile_id="azure",
                        ),
                    )
                    self.assertEqual(start_response.status_code, 200)
                    start_payload = start_response.json()
                    self.assertTrue(start_payload["job"]["id"])
                    self.assertEqual(start_payload["job"]["profile_id"], "azure")
                    self.assertTrue(any(item["id"] == "azure" for item in start_payload["ai_profiles"]))

                    status_response = client.get(
                        f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis-status"
                    )
                    self.assertEqual(status_response.status_code, 200)
                    status_payload = status_response.json()
                    self.assertEqual(status_payload["job"]["status"], "COMPLETED")
                    self.assertEqual(status_payload["job"]["profile_id"], "azure")
                    self.assertTrue(
                        any(item["event_type"] == "requirement_completed" for item in status_payload["events"])
                    )
                    self.assertTrue(any(item["event_type"] == "job_completed" for item in status_payload["events"]))

                    requirement_set_id = self._active_requirement_set_id(
                        document["id"]
                    )
                    matrix_response = client.get(
                        f"/api/projects/{project['id']}/sor-documents/"
                        f"{document['id']}/requirement-sets/"
                        f"{requirement_set_id}/matrix"
                    )
                    self.assertEqual(matrix_response.status_code, 200)
                    matrix_node = next(
                        node
                        for node in matrix_response.json()["nodes"]
                        if node["requirement_id"] == requirement["id"]
                    )
                    self.assertEqual(
                        matrix_node["ai_assessment"]["assessment_mode"],
                        "heuristic",
                    )
                    self.assertGreaterEqual(
                        len(db.list_assessment_runs(requirement["id"])),
                        1,
                    )
                    self.assertIsInstance(
                        (db.get_requirement(requirement["id"]) or {})[
                            "evidences"
                        ],
                        list,
                    )
                    legacy = client.get(
                        f"/api/requirements/{requirement['id']}/analysis-detail"
                    )
                    self.assertEqual(legacy.status_code, 404)

    def test_ai_analysis_detail_and_events_use_display_req_codes_from_workspace_order(self) -> None:
        project, document = self._create_project_with_document("-ai-display-order")
        later_requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0023",
            source_page=5,
            source_anchor="Main > 5.7",
            requirement_text="5.7 次条需求",
            raw_text="5.7 次条需求",
            display_text="5.7 次条需求",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="5.7",
            parent_section_title="次条需求",
            parent_section_label="5.7 次条需求",
        )
        first_requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0024",
            source_page=5,
            source_anchor="Main > 5.1",
            requirement_text="5.1 首条需求",
            raw_text="5.1 首条需求",
            display_text="5.1 首条需求",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="5.1",
            parent_section_title="首条需求",
            parent_section_label="5.1 首条需求",
        )
        db.add_knowledge_document(
            project_id=project["id"],
            file_name="platform-spec.txt",
            file_type="txt",
            file_path=str(self.tmp_path / "platform-spec-display-order.txt"),
            extracted_text=(
                "5.1 首条需求\n"
                "系统应满足首条需求。\n"
                "5.7 次条需求\n"
                "系统应满足次条需求。\n"
            ),
            doc_version="v1.0",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        preview_cache.persist_preview_cache(
            db.get_sor_document(document["id"]) or {},
            _fake_preview_with_reversed_req_codes(),
        )
        self._lock_active_requirement_set(project["id"], document["id"])

        with patch.dict(
            os.environ,
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "heuristic",
                "REQPILOT_AI_TRACE_DIR": str(self.tmp_path / "ai_traces_display_order"),
            },
            clear=False,
        ):
            with patch.object(main, "Thread", ImmediateThread):
                with TestClient(main.app) as client:
                    start_response = client.post(
                        f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                        json=self._ai_start_payload(
                            document["id"],
                            profile_id="azure",
                        ),
                    )
                    self.assertEqual(start_response.status_code, 200)

                    status_response = client.get(
                        f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis-status"
                    )
                    self.assertEqual(status_response.status_code, 200)
                    status_payload = status_response.json()
                    completed_codes = {
                        str(item.get("requirement_code") or "")
                        for item in status_payload["events"]
                        if item.get("event_type") == "requirement_completed"
                    }

                    requirement_set_id = self._active_requirement_set_id(
                        document["id"]
                    )
                    matrix_response = client.get(
                        f"/api/projects/{project['id']}/sor-documents/"
                        f"{document['id']}/requirement-sets/"
                        f"{requirement_set_id}/matrix"
                    )
                    self.assertEqual(matrix_response.status_code, 200)
                    matrix_node = next(
                        node
                        for node in matrix_response.json()["nodes"]
                        if node["requirement_id"] == first_requirement["id"]
                    )
                    legacy = client.get(
                        f"/api/requirements/{first_requirement['id']}/analysis-detail"
                    )
                    self.assertEqual(legacy.status_code, 404)

        self.assertEqual(status_payload["job"]["status"], "COMPLETED")
        self.assertEqual(completed_codes, {"REQ0001", "REQ0002"})
        self.assertNotIn("REQ0023", completed_codes)
        self.assertNotIn("REQ0024", completed_codes)
        self.assertEqual(
            (db.get_requirement(first_requirement["id"]) or {})["req_code"],
            first_requirement["req_code"],
        )
        self.assertEqual(matrix_node["requirement_code"], "REQ0001")
        self.assertEqual(later_requirement["req_code"], "REQ0023")

    def test_assessment_execution_policy_requires_mode_and_primary_run_provenance(self) -> None:
        base_assessment = {
            "analysis_label": "REQUIREMENT",
            "ai_decision": "ACCEPT",
            "analysis_basis": "candidate result",
        }
        cases = (
            ("llm_primary", "heuristic", [], "cannot serve heuristic"),
            ("heuristic", "llm_primary", [], "cannot serve llm_primary"),
            ("llm_primary", "llm_primary", [], "has no primary assessment run"),
        )

        for configured_mode, assessment_mode, runs, expected_reason in cases:
            with self.subTest(configured_mode=configured_mode, assessment_mode=assessment_mode):
                verdict = main.evaluate_assessment_execution(
                    assessment={**base_assessment, "assessment_mode": assessment_mode},
                    runs=runs,
                    configured_mode=configured_mode,
                )
                self.assertFalse(verdict.serviceable)
                self.assertIn(expected_reason, verdict.failure_reason)

        hard_rule_verdict = main.evaluate_assessment_execution(
            assessment={**base_assessment, "assessment_mode": "llm_primary"},
            runs=[
                {
                    "mode": "llm_primary",
                    "validation_status": "hard_rule_override",
                    "fallback_reason": "hard_rule_negative_reject",
                }
            ],
            configured_mode="llm_primary",
        )
        self.assertTrue(hard_rule_verdict.serviceable)

        manual_screening_verdict = main.evaluate_assessment_execution(
            assessment={
                "analysis_label": "REQUIREMENT",
                "ai_decision": "",
                "assessment_mode": "screening_only",
                "analysis_basis": "manual classification; no knowledge evidence available",
            },
            runs=[
                {
                    "mode": "screening_manual_override",
                    "validation_status": "accepted",
                    "fallback_reason": "",
                }
            ],
            configured_mode="llm_primary",
        )
        self.assertTrue(manual_screening_verdict.serviceable)

    def test_assessment_execution_policy_does_not_mask_non_shadow_failure(self) -> None:
        verdict = main.evaluate_assessment_execution(
            assessment={
                "analysis_label": "REQUIREMENT",
                "ai_decision": "NEED_CLARIFICATION",
                "assessment_mode": "heuristic",
                "analysis_basis": "heuristic serving result",
            },
            runs=[
                {
                    "mode": "llm_shadow",
                    "validation_status": "failed",
                    "fallback_reason": "shadow provider timeout",
                },
                {
                    "mode": "heuristic",
                    "validation_status": "failed",
                    "fallback_reason": "serving rule execution failed",
                },
            ],
            configured_mode="llm_shadow",
        )

        self.assertFalse(verdict.serviceable)
        self.assertIn("serving rule execution failed", verdict.failure_reason)

    def test_ai_analysis_job_api_reports_failed_runs(self) -> None:
        project, document = self._create_project_with_document("-ai-job-failed")
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 5.1",
            requirement_text="ECU shall support wake-up time less than 100ms.",
            raw_text="ECU shall support wake-up time less than 100ms.",
            display_text="ECU shall support wake-up time less than 100ms.",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="5.1",
            parent_section_title="Startup",
            parent_section_label="5.1 Startup",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        self._lock_active_requirement_set(project["id"], document["id"])

        with patch.object(main, "run_ai_copilot_assessment", side_effect=RuntimeError("simulated ai failure")):
            with patch.object(main, "Thread", ImmediateThread):
                with TestClient(main.app) as client:
                    start_response = client.post(
                        f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                        json=self._ai_start_payload(document["id"]),
                    )
                    self.assertEqual(start_response.status_code, 200)

                    status_response = client.get(
                        f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis-status"
                    )
                    self.assertEqual(status_response.status_code, 200)
                    status_payload = status_response.json()
                    self.assertEqual(status_payload["job"]["status"], "FAILED")
                    self.assertIn("simulated ai failure", status_payload["job"]["error_message"])
                    self.assertEqual(status_payload["document"]["ai_status"], "STALE")
                    self.assertTrue(any(item["event_type"] == "job_failed" for item in status_payload["events"]))

    def test_ai_analysis_job_api_surfaces_screening_and_analysis_failed_statuses(self) -> None:
        project, document = self._create_project_with_document("-ai-job-fail-closed")
        screening_requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 5.1",
            requirement_text="Supplier shall provide EMC validation evidence.",
            raw_text="Supplier shall provide EMC validation evidence.",
            display_text="Supplier shall provide EMC validation evidence.",
            requirement_type="REGULATION",
            priority="MEDIUM",
            parent_section_code="5.1",
            parent_section_title="Compliance",
            parent_section_label="5.1 Compliance",
        )
        analysis_requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0002",
            source_page=2,
            source_anchor="Page 2 | Section 5.2",
            requirement_text="System shall support wake-up time less than 100ms.",
            raw_text="System shall support wake-up time less than 100ms.",
            display_text="System shall support wake-up time less than 100ms.",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="5.2",
            parent_section_title="Startup",
            parent_section_label="5.2 Startup",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        db.attach_requirement_tree_snapshot(
            self._active_requirement_set_id(document["id"]),
            object_key="test/ai-job-fail-closed/requirement-tree.json",
            tree_sha256="f" * 64,
            schema_version="requirement-tree-snapshot/v1",
            structure_snapshot_id="test-ai-job-fail-closed",
            structure_tree_sha256="e" * 64,
            structure_revision=1,
        )
        self._lock_active_requirement_set(project["id"], document["id"])
        ai_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.tmp_path / "ai_traces_fail_closed",
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.7-plus",
            screening_model="qwen3.6-flash",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="http://localhost:6333",
            qdrant_collection_name="reqpilot-knowledge-aliyun-local",
        )
        eligible_rows = db.list_requirements(project["id"], document["id"])
        screening_rows = [row for row in eligible_rows if row["id"] == screening_requirement["id"]]
        analysis_rows = [row for row in eligible_rows if row["id"] == analysis_requirement["id"]]
        screening_prepared = (project, document, screening_rows, screening_rows, [], ai_config)
        analysis_prepared = (project, document, analysis_rows, analysis_rows, [], ai_config)
        screening_failed_result = {
            "assessment": {
                "profile_id": "aliyun",
                "provider": "aliyun",
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "LLM screening failed; analysis stopped. Request timed out.",
                "ai_decision": "",
                "ai_reason": "",
                "ai_confidence": 0.0,
                "risk_level": "",
                "clarification_question": "",
                "normalized_requirement": "Supplier shall provide EMC validation evidence.",
                "assessment_mode": "screening_failed",
                "retrieval_backend": "screening_only",
                "model_name": "qwen3.6-flash",
                "prompt_version": "v2.1-requirement-screening-aliyun",
            },
            "evidences": [],
            "runs": [
                {
                    "profile_id": "aliyun",
                    "provider": "aliyun",
                    "mode": "screening_llm_shadow",
                    "retrieval_backend": "screening_only",
                    "model_name": "qwen3.6-flash",
                    "prompt_version": "v2.1-requirement-screening-aliyun",
                    "input_payload_json": "{}",
                    "output_payload_json": "",
                    "validation_status": "failed",
                    "fallback_reason": "Request timed out.",
                }
            ],
            "trace_path": str(self.tmp_path / "trace-screening-failed.json"),
        }
        analysis_failed_result = {
            "assessment": {
                "profile_id": "aliyun",
                "provider": "aliyun",
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "LLM requirement analysis failed; analysis stopped. Connection error.",
                "ai_decision": "",
                "ai_reason": "",
                "ai_confidence": 0.0,
                "risk_level": "",
                "clarification_question": "",
                "normalized_requirement": "System shall support wake-up time less than 100ms.",
                "assessment_mode": "analysis_failed",
                "retrieval_backend": "qdrant",
                "model_name": "qwen3.7-plus",
                "prompt_version": "v3.0-compliance-evidence-contract",
            },
            "evidences": [],
            "runs": [
                {
                    "profile_id": "aliyun",
                    "provider": "aliyun",
                    "mode": "screening_llm_shadow",
                    "retrieval_backend": "screening_only",
                    "model_name": "qwen3.6-flash",
                    "prompt_version": "v2.1-requirement-screening-aliyun",
                    "input_payload_json": "{}",
                    "output_payload_json": "{\"analysis_label\":\"REQUIREMENT\"}",
                    "validation_status": "accepted",
                    "fallback_reason": "",
                },
                {
                    "profile_id": "aliyun",
                    "provider": "aliyun",
                    "mode": "llm_shadow",
                    "retrieval_backend": "qdrant",
                    "model_name": "qwen3.7-plus",
                    "prompt_version": "v3.0-compliance-evidence-contract",
                    "input_payload_json": "{}",
                    "output_payload_json": "",
                    "validation_status": "failed",
                    "fallback_reason": "Connection error.",
                },
            ],
            "trace_path": str(self.tmp_path / "trace-analysis-failed.json"),
        }

        with patch.object(
            main,
            "_prepare_document_ai_analysis_request",
            side_effect=[screening_prepared, screening_prepared, analysis_prepared, analysis_prepared],
        ), patch.object(
            main,
            "run_ai_copilot_assessment",
            side_effect=[screening_failed_result, analysis_failed_result],
        ), patch.object(main, "Thread", ImmediateThread):
            with TestClient(main.app) as client:
                screening_scope_request = {
                    "command_id": "test-screening-failure",
                    "requirement_set_id": self._active_requirement_set_id(document["id"]),
                    "expected_tree_sha256": "f" * 64,
                    "scope": "CURRENT",
                    "node_ids": [f"matrix-{screening_requirement['id']}"],
                }
                screening_preview_response = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis/scope-preview",
                    json=screening_scope_request,
                )
                self.assertEqual(screening_preview_response.status_code, 200)
                screening_start_response = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                    json={
                        **screening_scope_request,
                        "profile_id": "aliyun",
                        "expected_scope_sha256": screening_preview_response.json()[
                            "scope_sha256"
                        ],
                    },
                )
                self.assertEqual(
                    screening_start_response.status_code,
                    200,
                    screening_start_response.text,
                )
                screening_status_response = client.get(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis-status"
                )
                self.assertEqual(screening_status_response.status_code, 200)
                screening_status = screening_status_response.json()

                analysis_scope_request = {
                    "command_id": "test-analysis-failure",
                    "requirement_set_id": self._active_requirement_set_id(document["id"]),
                    "expected_tree_sha256": "f" * 64,
                    "scope": "CURRENT",
                    "node_ids": [f"matrix-{analysis_requirement['id']}"],
                }
                analysis_preview_response = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis/scope-preview",
                    json=analysis_scope_request,
                )
                self.assertEqual(analysis_preview_response.status_code, 200)
                analysis_start_response = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                    json={
                        **analysis_scope_request,
                        "profile_id": "aliyun",
                        "expected_scope_sha256": analysis_preview_response.json()[
                            "scope_sha256"
                        ],
                    },
                )
                self.assertEqual(
                    analysis_start_response.status_code,
                    200,
                    analysis_start_response.text,
                )
                analysis_status_response = client.get(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis-status"
                )
                self.assertEqual(analysis_status_response.status_code, 200)
                analysis_status = analysis_status_response.json()

        for status_payload, failure_mode, requirement_code in (
            (screening_status, "screening_failed", "REQ0001"),
            (analysis_status, "analysis_failed", "REQ0002"),
        ):
            self.assertEqual(status_payload["job"]["status"], "FAILED")
            self.assertEqual(status_payload["job"]["outcome"], "FAILED")
            self.assertEqual(status_payload["job"]["completed_requirements"], 0)
            self.assertEqual(status_payload["job"]["current_requirement_code"], requirement_code)
            self.assertIn(failure_mode, status_payload["job"]["error_message"])
            event_types = {str(item.get("event_type") or "") for item in status_payload["events"]}
            self.assertIn("requirement_failed", event_types)
            self.assertIn("job_failed", event_types)
            self.assertNotIn("job_completed", event_types)
            self.assertEqual(status_payload["document"]["ai_status"], "NOT_RUN")

        screening_row = db.get_requirement(screening_requirement["id"]) or {}
        analysis_row = db.get_requirement(analysis_requirement["id"]) or {}
        self.assertFalse(screening_row.get("assessment_mode"))
        self.assertFalse(analysis_row.get("assessment_mode"))
        self.assertEqual(db.list_assessment_runs(screening_requirement["id"])[0]["validation_status"], "failed")
        self.assertEqual(db.list_assessment_runs(analysis_requirement["id"])[0]["validation_status"], "failed")

    def test_ai_analysis_job_partial_provider_failure_preserves_last_serving_results(self) -> None:
        project, document = self._create_project_with_document("-ai-job-partial-failure")
        requirements = [
            db.insert_requirement(
                project_id=project["id"],
                sor_document_id=document["id"],
                req_code=f"REQ000{index}",
                source_page=index,
                source_anchor=f"Page {index} | Section 5.{index}",
                requirement_text=f"Requirement {index} shall remain traceable.",
                raw_text=f"Requirement {index} shall remain traceable.",
                display_text=f"Requirement {index} shall remain traceable.",
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
                parent_section_code=f"5.{index}",
                parent_section_title="Traceability",
                parent_section_label=f"5.{index} Traceability",
            )
            for index in (1, 2)
        ]
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
        for index, requirement in enumerate(requirements, start=1):
            db.upsert_assessment(
                requirement_id=requirement["id"],
                profile_id="azure",
                provider="azure",
                ai_decision="ACCEPT",
                ai_reason=f"last-good-{index}",
                ai_confidence=0.88,
                assessment_mode="heuristic",
                retrieval_backend="local",
                analysis_label="REQUIREMENT",
                analysis_basis="last known good serving result",
                normalized_requirement=f"old-{index}",
                risk_level="LOW",
                clarification_question="",
                model_name="old-model",
                prompt_version="old-prompt",
            )
            db.replace_evidences(
                requirement["id"],
                [
                    {
                        "source_doc_id": f"old-doc-{index}",
                        "source_doc_name": "old-evidence.txt",
                        "source_doc_version": "v1",
                        "source_location": f"line {index}",
                        "excerpt": f"last-good-evidence-{index}",
                        "relevance": 0.91,
                    }
                ],
            )
        db.update_sor_document_ai_state(
            document["id"],
            ai_status="READY",
            ai_assessed_at="2026-07-10T00:00:00+00:00",
            ai_requirement_set_version=int(locked_document.get("requirement_set_version") or 0),
            ai_knowledge_base_version=int(project.get("knowledge_base_version") or 0),
            workflow_stage="AI_READY",
        )
        previous_assessments = {item["id"]: db.get_assessment(item["id"]) for item in requirements}
        previous_evidences = {
            item["id"]: (db.get_requirement(item["id"]) or {}).get("evidences") for item in requirements
        }

        with patch.dict(os.environ, {"REQPILOT_AI_ASSESSMENT_MODE": "heuristic"}, clear=False):
            ai_config = main.load_ai_copilot_config("azure")
        eligible_rows = db.list_requirements(project["id"], document["id"])
        prepared = (project, locked_document, eligible_rows, eligible_rows, [], ai_config)
        first_result = {
            "assessment": {
                "profile_id": "azure",
                "provider": "azure",
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "new candidate",
                "ai_decision": "REJECT",
                "ai_reason": "new result must not publish partially",
                "ai_confidence": 0.93,
                "risk_level": "HIGH",
                "clarification_question": "",
                "normalized_requirement": "new-1",
                "assessment_mode": "heuristic",
                "retrieval_backend": "local",
                "model_name": "new-model",
                "prompt_version": "new-prompt",
            },
            "evidences": [
                {
                    "source_doc_id": "new-doc",
                    "source_doc_name": "new-evidence.txt",
                    "source_doc_version": "v2",
                    "source_location": "line 10",
                    "excerpt": "new evidence",
                    "relevance": 0.99,
                }
            ],
            "runs": [],
        }

        with patch.object(main, "_prepare_document_ai_analysis_request", return_value=prepared), patch.object(
            main,
            "run_ai_copilot_assessment",
            side_effect=[first_result, RuntimeError("provider timeout on second row")],
        ), patch.object(main, "Thread", ImmediateThread):
            with TestClient(main.app) as client:
                start_response = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                    json=self._ai_start_payload(document["id"]),
                )
                self.assertEqual(start_response.status_code, 200)
                status_payload = client.get(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis-status"
                ).json()

        self.assertEqual(status_payload["job"]["status"], "FAILED")
        self.assertEqual(status_payload["job"]["completed_requirements"], 1)
        self.assertEqual(status_payload["job"]["analyzed_requirements"], 1)
        self.assertEqual(status_payload["job"]["current_requirement_code"], "REQ0002")
        self.assertIn("provider timeout on second row", status_payload["job"]["error_message"])
        self.assertEqual(status_payload["document"]["ai_status"], "READY")
        self.assertEqual(status_payload["document"]["workflow_stage"], "AI_READY")
        self.assertIn("未发布本次部分结果", status_payload["job"]["last_message"])
        for requirement in requirements:
            self.assertEqual(db.get_assessment(requirement["id"]), previous_assessments[requirement["id"]])
            self.assertEqual(
                (db.get_requirement(requirement["id"]) or {}).get("evidences"),
                previous_evidences[requirement["id"]],
            )
        event_types = [str(item.get("event_type") or "") for item in status_payload["events"]]
        self.assertIn("requirement_completed", event_types)
        self.assertIn("requirement_failed", event_types)
        self.assertIn("job_failed", event_types)
        self.assertNotIn("job_completed", event_types)

    def test_atomic_serving_result_publish_rolls_back_every_requirement_on_write_failure(self) -> None:
        import app.adapters.persistence.assessment_results as assessment_results

        project, document = self._create_project_with_document("-atomic-serving-rollback")
        requirements = [
            db.insert_requirement(
                project_id=project["id"],
                sor_document_id=document["id"],
                req_code=f"REQ000{index}",
                source_page=index,
                source_anchor=f"Page {index} | Section 6.{index}",
                requirement_text=f"Requirement {index} shall be satisfied.",
                raw_text=f"Requirement {index} shall be satisfied.",
                display_text=f"Requirement {index} shall be satisfied.",
                requirement_type="FUNCTIONAL",
                priority="MEDIUM",
            )
            for index in (1, 2)
        ]
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
        for index, requirement in enumerate(requirements, start=1):
            db.upsert_assessment(
                requirement_id=requirement["id"],
                profile_id="azure",
                provider="azure",
                ai_decision="ACCEPT",
                ai_reason=f"old-reason-{index}",
                ai_confidence=0.8,
                assessment_mode="heuristic",
                retrieval_backend="local",
                analysis_label="REQUIREMENT",
                analysis_basis="old serving result",
                normalized_requirement=f"old-{index}",
                risk_level="LOW",
                clarification_question="",
                model_name="old-model",
                prompt_version="old-prompt",
            )
            db.replace_evidences(
                requirement["id"],
                [
                    {
                        "source_doc_id": f"old-doc-{index}",
                        "source_doc_name": "old.txt",
                        "source_doc_version": "v1",
                        "source_location": f"line {index}",
                        "excerpt": f"old-evidence-{index}",
                        "relevance": 0.8,
                    }
                ],
            )
        previous_assessments = {item["id"]: db.get_assessment(item["id"]) for item in requirements}
        previous_evidences = {
            item["id"]: (db.get_requirement(item["id"]) or {}).get("evidences") for item in requirements
        }

        def result_for(index: int) -> dict:
            return {
                "assessment": {
                    "analysis_label": "REQUIREMENT",
                    "analysis_basis": "new candidate",
                    "ai_decision": "REJECT",
                    "ai_reason": f"new-reason-{index}",
                    "ai_confidence": 0.9,
                    "risk_level": "HIGH",
                    "clarification_question": "",
                    "normalized_requirement": f"new-{index}",
                    "assessment_mode": "heuristic",
                    "retrieval_backend": "local",
                    "model_name": "new-model",
                    "prompt_version": "new-prompt",
                },
                "evidences": [
                    {
                        "source_doc_id": f"new-doc-{index}",
                        "source_doc_name": "new.txt",
                        "source_doc_version": "v2",
                        "source_location": f"line {index}",
                        "excerpt": f"new-evidence-{index}",
                        "relevance": 0.95,
                    }
                ],
            }

        original_persist = assessment_results._persist_result
        call_count = 0

        def fail_during_second_result(conn, requirement_id, result, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                conn.execute(
                    "UPDATE assessments SET model_name = 'half-written' WHERE requirement_id = ?",
                    (requirement_id,),
                )
                raise RuntimeError("simulated evidence publication failure")
            return original_persist(conn, requirement_id, result, **kwargs)

        with patch.object(assessment_results, "_persist_result", side_effect=fail_during_second_result):
            with self.assertRaisesRegex(RuntimeError, "simulated evidence publication failure"):
                assessment_results.publish_serving_results(
                    [(item["id"], result_for(index)) for index, item in enumerate(requirements, start=1)],
                    document_id=document["id"],
                    requirement_set_version=int(locked_document.get("requirement_set_version") or 0),
                    knowledge_base_version=int(project.get("knowledge_base_version") or 0),
                    profile_id="azure",
                    provider="azure",
                    retrieval_backend="local",
                )

        for requirement in requirements:
            self.assertEqual(db.get_assessment(requirement["id"]), previous_assessments[requirement["id"]])
            self.assertEqual(
                (db.get_requirement(requirement["id"]) or {}).get("evidences"),
                previous_evidences[requirement["id"]],
            )

    def test_analysis_job_claim_is_atomic_and_returns_one_active_job(self) -> None:
        project, document = self._create_project_with_document("-atomic-job-claim")
        requirement = db.insert_requirement(
            project["id"],
            document["id"],
            "REQ0001",
            1,
            "Page 1",
            "Supplier shall provide evidence.",
            "REGULATION",
            "MEDIUM",
        )
        requirement_set_id = str(requirement["requirement_set_id"])
        db.lock_sor_document_extraction(
            document["id"],
            expected_requirement_set_id=requirement_set_id,
        )
        barrier = Barrier(2)

        def claim() -> tuple[dict, bool]:
            barrier.wait()
            return assessment_results.claim_analysis_job(
                project_id=project["id"],
                document_id=document["id"],
                requirement_set_id=requirement_set_id,
                profile_id="azure",
                provider="azure",
                assessment_mode="heuristic",
                retrieval_backend="local",
                chat_model="",
                embedding_model="",
                total_requirements=1,
                queued_message="queued",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [future.result() for future in (executor.submit(claim), executor.submit(claim))]

        self.assertEqual(len({result[0]["id"] for result in results}), 1)
        self.assertEqual(sorted(result[1] for result in results), [False, True])
        with db.get_conn() as connection:
            active_count = connection.execute(
                """
                SELECT COUNT(*) FROM ai_analysis_jobs
                WHERE sor_document_id = ?
                  AND status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
                """,
                (document["id"],),
            ).fetchone()[0]
            created_event_count = connection.execute(
                "SELECT COUNT(*) FROM ai_analysis_events WHERE sor_document_id = ? AND event_type = 'job_created'",
                (document["id"],),
            ).fetchone()[0]
        self.assertEqual(active_count, 1)
        self.assertEqual(created_event_count, 1)

    def test_analysis_job_claim_serializes_before_requirement_set_activation(
        self,
    ) -> None:
        project, document = self._create_project_with_document("-claim-activation")
        requirement = db.insert_requirement(
            project["id"],
            document["id"],
            "REQ0001",
            1,
            "Page 1",
            "Supplier shall provide evidence.",
            "REGULATION",
            "MEDIUM",
        )
        source_set_id = str(requirement["requirement_set_id"])
        db.lock_sor_document_extraction(
            document["id"],
            expected_requirement_set_id=source_set_id,
        )
        target = db.create_requirement_set(project["id"], document["id"])
        claim_reached_set_lock = Event()
        activation_attempted = Event()
        activation_done = Event()
        activation_errors: list[BaseException] = []
        original_set_lock = (
            assessment_results.lock_requirement_matrix_sets_in_connection
        )

        def hold_claim_after_document_lock(*args, **kwargs):
            claim_reached_set_lock.set()
            self.assertTrue(activation_attempted.wait(timeout=2))
            time.sleep(0.1)
            self.assertFalse(activation_done.is_set())
            return original_set_lock(*args, **kwargs)

        def activate_target() -> None:
            claim_reached_set_lock.wait(timeout=2)
            activation_attempted.set()
            try:
                db.activate_requirement_set(
                    project["id"],
                    document["id"],
                    target["id"],
                    expected_active_requirement_set_id=source_set_id,
                )
            except BaseException as exc:  # Asserted in the owner thread.
                activation_errors.append(exc)
            finally:
                activation_done.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(activate_target)
            with patch.object(
                assessment_results,
                "lock_requirement_matrix_sets_in_connection",
                side_effect=hold_claim_after_document_lock,
            ):
                job, claimed = assessment_results.claim_analysis_job(
                    project_id=project["id"],
                    document_id=document["id"],
                    requirement_set_id=source_set_id,
                    profile_id="azure",
                    provider="azure",
                    assessment_mode="heuristic",
                    retrieval_backend="local",
                    chat_model="",
                    embedding_model="",
                    total_requirements=1,
                    queued_message="queued",
                )
            future.result(timeout=5)

        self.assertTrue(claimed)
        self.assertEqual(job["requirement_set_id"], source_set_id)
        self.assertEqual(len(activation_errors), 1)
        self.assertIsInstance(
            activation_errors[0],
            db.RequirementSetMutationBlocked,
        )
        self.assertEqual(
            (db.get_active_requirement_set(document["id"]) or {})["id"],
            source_set_id,
        )
        self.assertFalse(bool(int((db.get_requirement_set(target["id"]) or {})["is_active"])))

    def test_analysis_job_captures_knowledge_snapshot_and_completed_set_lineage(self) -> None:
        project, document = self._create_project_with_document("-job-kb-snapshot")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1",
            requirement_text="Supplier shall provide evidence.",
            requirement_type="REGULATION",
            priority="MEDIUM",
        )
        requirement_set = db.get_requirement_set(requirement["requirement_set_id"])
        db.bump_project_knowledge_base_version(project["id"])
        db.lock_sor_document_extraction(
            document["id"],
            expected_requirement_set_id=requirement_set["id"],
        )

        job, claimed = assessment_results.claim_analysis_job(
            project_id=project["id"],
            document_id=document["id"],
            requirement_set_id=requirement_set["id"],
            profile_id="azure",
            provider="azure",
            assessment_mode="heuristic",
            retrieval_backend="local",
            chat_model="",
            embedding_model="",
            total_requirements=1,
            queued_message="queued",
        )
        self.assertTrue(claimed)
        self.assertEqual(job["requirement_set_id"], requirement_set["id"])
        self.assertEqual(job["knowledge_base_version"], 1)
        db.update_ai_analysis_job(
            job["id"], status="COMPLETED", finished_at="2026-07-18T09:00:00+00:00"
        )
        completed = db.get_latest_completed_ai_analysis_job_for_set(requirement_set["id"])
        self.assertEqual(completed["id"], job["id"])
        self.assertEqual(completed["knowledge_base_version"], 1)

    def test_analysis_publication_rejects_claimed_execution_identity_drift(
        self,
    ) -> None:
        project, document = self._create_project_with_document("-job-profile-drift")
        requirement = db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1",
            "Supplier shall provide evidence.", "REGULATION", "MEDIUM",
        )
        requirement_set = db.get_requirement_set(requirement["requirement_set_id"])
        db.lock_sor_document_extraction(
            document["id"],
            expected_requirement_set_id=requirement_set["id"],
        )
        job, claimed = assessment_results.claim_analysis_job(
            project_id=project["id"],
            document_id=document["id"],
            requirement_set_id=requirement_set["id"],
            profile_id="profile-claimed",
            provider="provider-claimed",
            assessment_mode="heuristic",
            retrieval_backend="retrieval-claimed",
            chat_model="chat-claimed",
            embedding_model="embedding-claimed",
            total_requirements=1,
            queued_message="queued",
        )
        self.assertTrue(claimed)
        db.update_ai_analysis_job(job["id"], status="RUNNING")
        result = {
            "assessment": {
                "ai_decision": "ACCEPT",
                "ai_reason": "candidate",
                "ai_confidence": 0.9,
                "assessment_mode": "heuristic",
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "FULL",
                "normalized_requirement": "candidate",
                "risk_level": "LOW",
                "clarification_question": "",
                "model_name": "model",
                "prompt_version": "prompt",
            },
            "evidences": [],
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "execution identity changed",
        ):
            assessment_results.publish_serving_results(
                [(requirement["id"], result)],
                document_id=document["id"],
                requirement_set_id=requirement_set["id"],
                requirement_set_version=int(requirement_set["version"]),
                knowledge_base_version=int(
                    (db.get_project(project["id"]) or {})[
                        "knowledge_base_version"
                    ]
                ),
                profile_id="profile-drifted",
                provider="provider-claimed",
                retrieval_backend="retrieval-claimed",
                job_completion={
                    "job_id": job["id"],
                    "total_requirements": 1,
                    "completed_requirements": 1,
                    "analyzed_requirements": 1,
                    "info_requirements": 0,
                    "screened_requirements": 0,
                    "message": "done",
                    "event_payload": {},
                },
            )

        self.assertIsNone(db.get_assessment(requirement["id"]))
        self.assertEqual((db.get_ai_analysis_job(job["id"]) or {})["status"], "RUNNING")

    def test_late_failed_worker_cannot_overwrite_newer_same_set_projection(self) -> None:
        project, document = self._create_project_with_document("-late-worker-projection")
        requirement = db.insert_requirement(
            project["id"], document["id"], "REQ0001", 1, "Page 1",
            "Supplier shall provide evidence.", "REGULATION", "MEDIUM",
        )
        requirement_set = db.get_requirement_set(requirement["requirement_set_id"])
        old_job = db.create_ai_analysis_job(
            project["id"], document["id"], requirement_set_id=requirement_set["id"],
            total_requirements=1, status="RUNNING",
        )
        db.update_ai_analysis_job(old_job["id"], status="FAILED", finished_at="old-finished")
        newer_job = db.create_ai_analysis_job(
            project["id"], document["id"], requirement_set_id=requirement_set["id"],
            total_requirements=1, status="COMPLETED",
        )
        db.update_sor_document_ai_state(
            document["id"],
            ai_status="READY",
            ai_assessed_at="newer-finished",
            ai_requirement_set_version=requirement_set["version"],
            ai_knowledge_base_version=0,
            workflow_stage="AI_READY",
        )

        projected = db.update_sor_document_ai_state_for_job(
            old_job["id"], ai_status="STALE", workflow_stage="EXTRACTION_LOCKED"
        )
        current = db.get_sor_document(document["id"])
        self.assertFalse(projected)
        self.assertEqual(current["ai_status"], "READY")
        self.assertEqual(current["ai_assessed_at"], "newer-finished")
        self.assertEqual(
            db.get_latest_completed_ai_analysis_job_for_set(requirement_set["id"])["id"],
            newer_job["id"],
        )

    def test_start_route_reuses_claimed_job_while_worker_is_live(self) -> None:
        project, document = self._create_project_with_document("-start-single-flight")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1",
            requirement_text="Supplier shall provide evidence.",
            raw_text="Supplier shall provide evidence.",
            display_text="Supplier shall provide evidence.",
            requirement_type="REGULATION",
            priority="MEDIUM",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
        with patch.dict(os.environ, {"REQPILOT_AI_ASSESSMENT_MODE": "heuristic"}, clear=False):
            ai_config = main.load_ai_copilot_config("azure")
        eligible_rows = [db.get_requirement(requirement["id"])]
        prepared = (project, locked_document, eligible_rows, eligible_rows, [], ai_config)

        class HoldingThread(ImmediateThread):
            def start(self) -> None:
                self._alive = True

        try:
            with patch.object(
                main,
                "_prepare_document_ai_analysis_request",
                return_value=prepared,
            ), patch.object(main, "Thread", HoldingThread):
                request = main.RunAIAnalysisRequest(
                    requirement_set_id=self._active_requirement_set_id(
                        document["id"]
                    ),
                )
                first = main.start_document_ai_analysis(
                    project["id"],
                    document["id"],
                    request,
                )
                second = main.start_document_ai_analysis(
                    project["id"],
                    document["id"],
                    request,
                )
        finally:
            main._clear_ai_analysis_thread(document["id"])

        self.assertEqual(first["job"]["id"], second["job"]["id"])
        with db.get_conn() as connection:
            job_count = connection.execute(
                "SELECT COUNT(*) FROM ai_analysis_jobs WHERE sor_document_id = ?",
                (document["id"],),
            ).fetchone()[0]
        self.assertEqual(job_count, 1)

    def test_status_cannot_finalize_job_between_claim_and_runtime_registration(self) -> None:
        project, document = self._create_project_with_document("-claim-register-race")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1",
            requirement_text="Supplier shall provide evidence.",
            raw_text="Supplier shall provide evidence.",
            display_text="Supplier shall provide evidence.",
            requirement_type="REGULATION",
            priority="MEDIUM",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
        with patch.dict(os.environ, {"REQPILOT_AI_ASSESSMENT_MODE": "heuristic"}, clear=False):
            ai_config = main.load_ai_copilot_config("azure")
        eligible_rows = [db.get_requirement(requirement["id"])]
        prepared = (project, locked_document, eligible_rows, eligible_rows, [], ai_config)
        request = main.RunAIAnalysisRequest(
            requirement_set_id=self._active_requirement_set_id(document["id"]),
        )
        claim_committed = Event()
        release_claim = Event()
        status_attempted = Event()
        original_claim = main.claim_analysis_job

        def delayed_claim(**kwargs):
            result = original_claim(**kwargs)
            claim_committed.set()
            self.assertTrue(release_claim.wait(timeout=2))
            return result

        class HoldingThread(ImmediateThread):
            def start(self) -> None:
                self._alive = True

        def read_status() -> dict:
            status_attempted.set()
            return main._build_ai_analysis_status_payload(project["id"], document["id"])

        try:
            with patch.object(
                main,
                "_prepare_document_ai_analysis_request",
                return_value=prepared,
            ), patch.object(main, "claim_analysis_job", side_effect=delayed_claim), patch.object(
                main,
                "Thread",
                HoldingThread,
            ), ThreadPoolExecutor(max_workers=2) as executor:
                start_future = executor.submit(
                    main.start_document_ai_analysis,
                    project["id"],
                    document["id"],
                    request,
                )
                self.assertTrue(claim_committed.wait(timeout=2))
                status_future = executor.submit(read_status)
                self.assertTrue(status_attempted.wait(timeout=2))
                self.assertFalse(status_future.done())
                release_claim.set()
                started = start_future.result(timeout=2)
                status = status_future.result(timeout=2)
        finally:
            release_claim.set()
            main._clear_ai_analysis_thread(document["id"])

        self.assertEqual(started["job"]["id"], status["job"]["id"])
        self.assertEqual((db.get_ai_analysis_job(started["job"]["id"]) or {}).get("status"), "PENDING")
        event_types = [
            item.get("event_type")
            for item in db.list_ai_analysis_events(started["job"]["id"])
        ]
        self.assertNotIn("job_failed", event_types)

    def test_old_worker_cleanup_cannot_remove_new_job_runtime(self) -> None:
        document_id = "runtime-document"
        old_thread = ImmediateThread()
        new_thread = ImmediateThread()
        main._register_ai_analysis_thread(document_id, old_thread, job_id="old-job")
        main._register_ai_analysis_thread(document_id, new_thread, job_id="new-job")

        main._clear_ai_analysis_thread(document_id, job_id="old-job")

        self.assertIs(main._get_ai_analysis_thread(document_id), new_thread)
        main._clear_ai_analysis_thread(document_id, job_id="new-job")
        self.assertIsNone(main._get_ai_analysis_thread(document_id))

    def test_worker_rejects_requirement_or_knowledge_snapshot_drift_before_publish(self) -> None:
        for drift_kind in ("requirements", "knowledge"):
            with self.subTest(drift_kind=drift_kind):
                project, document = self._create_project_with_document(f"-snapshot-{drift_kind}")
                requirement = db.insert_requirement(
                    project_id=project["id"],
                    sor_document_id=document["id"],
                    req_code="REQ0001",
                    source_page=1,
                    source_anchor="Page 1 | Section 5.1",
                    requirement_text="Supplier shall provide a validation report.",
                    raw_text="Supplier shall provide a validation report.",
                    display_text="Supplier shall provide a validation report.",
                    requirement_type="REGULATION",
                    priority="MEDIUM",
                )
                db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
                locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
                replacement_set = None
                if drift_kind == "requirements":
                    replacement_set = db.create_requirement_set(project["id"], document["id"])
                    db.insert_requirement(
                        project_id=project["id"],
                        sor_document_id=document["id"],
                        requirement_set_id=replacement_set["id"],
                        req_code="REQ0001",
                        source_page=1,
                        source_anchor="Page 1 | Section 5.1",
                        requirement_text="A newer Requirement Set owns this projection.",
                        requirement_type="REGULATION",
                        priority="MEDIUM",
                    )
                with patch.dict(os.environ, {"REQPILOT_AI_ASSESSMENT_MODE": "heuristic"}, clear=False):
                    ai_config = main.load_ai_copilot_config("azure")
                eligible_rows = db.list_requirements(project["id"], document["id"])
                job = db.create_ai_analysis_job(
                    project_id=project["id"],
                    sor_document_id=document["id"],
                    profile_id=ai_config.profile_id,
                    provider=ai_config.provider,
                    assessment_mode=ai_config.assessment_mode,
                    retrieval_backend=ai_config.retrieval_backend,
                    total_requirements=1,
                )
                runtime_thread = ImmediateThread()
                main._register_ai_analysis_thread(document["id"], runtime_thread, job_id=job["id"])
                result = {
                    "assessment": {
                        "profile_id": "azure",
                        "provider": "azure",
                        "analysis_label": "REQUIREMENT",
                        "analysis_basis": "heuristic serving result",
                        "ai_decision": "NEED_CLARIFICATION",
                        "ai_reason": "human review is required",
                        "ai_confidence": 0.6,
                        "risk_level": "MEDIUM",
                        "clarification_question": "Provide evidence.",
                        "normalized_requirement": requirement["requirement_text"],
                        "assessment_mode": "heuristic",
                        "retrieval_backend": "local",
                        "model_name": "rules-rag-prototype",
                        "prompt_version": "heuristic-v1",
                    },
                    "evidences": [],
                    "runs": [],
                }
                drift_applied = False

                def run_with_drift(*args, **kwargs):
                    nonlocal drift_applied
                    if not drift_applied:
                        drift_applied = True
                        if drift_kind == "requirements":
                            with db.get_conn() as connection:
                                connection.execute(
                                    "UPDATE requirement_sets SET is_active = 0 WHERE sor_document_id = ?",
                                    (document["id"],),
                                )
                                connection.execute(
                                    "UPDATE requirement_sets SET is_active = 1 WHERE id = ?",
                                    (replacement_set["id"],),
                                )
                                connection.execute(
                                    """
                                    UPDATE sor_documents
                                    SET active_requirement_set_id = ?, requirement_set_version = ?,
                                        ai_status = 'READY', ai_assessed_at = 'newer-projection',
                                        ai_requirement_set_version = ?, ai_knowledge_base_version = 0,
                                        workflow_stage = 'AI_READY'
                                    WHERE id = ?
                                    """,
                                    (
                                        replacement_set["id"], replacement_set["version"],
                                        replacement_set["version"], document["id"],
                                    ),
                                )
                        else:
                            db.bump_project_knowledge_base_version(project["id"])
                    return result

                prepared = (project, locked_document, eligible_rows, eligible_rows, [], ai_config)
                with patch.object(
                    main,
                    "_prepare_document_ai_analysis_request",
                    return_value=prepared,
                ), patch.object(main, "run_ai_copilot_assessment", side_effect=run_with_drift):
                    main._run_document_ai_analysis_worker(job["id"], project["id"], document["id"])

                stored_job = db.get_ai_analysis_job(job["id"]) or {}
                event_types = [item.get("event_type") for item in db.list_ai_analysis_events(job["id"])]
                self.assertEqual(stored_job.get("status"), "FAILED")
                self.assertIn("superseded", str(stored_job.get("error_message") or ""))
                expected_projection = "READY" if drift_kind == "requirements" else "STALE"
                self.assertEqual(
                    (db.get_sor_document(document["id"]) or {}).get("ai_status"),
                    expected_projection,
                )
                self.assertIsNone(db.get_assessment(requirement["id"]))
                self.assertIn("job_failed", event_types)
                self.assertNotIn("job_completed", event_types)

    def test_analysis_status_projects_ready_snapshot_mismatch_as_stale(self) -> None:
        project, document = self._create_project_with_document("-status-stale-projection")
        db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1",
            requirement_text="Supplier shall provide evidence.",
            raw_text="Supplier shall provide evidence.",
            display_text="Supplier shall provide evidence.",
            requirement_type="REGULATION",
            priority="MEDIUM",
        )
        locked = db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        db.update_sor_document_ai_state(
            document["id"],
            ai_status="READY",
            workflow_stage="AI_READY",
            ai_requirement_set_version=int(locked.get("requirement_set_version") or 0),
            ai_knowledge_base_version=0,
        )
        db.bump_project_knowledge_base_version(project["id"])

        payload = main._build_ai_analysis_status_payload(project["id"], document["id"])

        self.assertEqual(payload["document"]["ai_status"], "STALE")

    def test_analysis_status_uses_terminal_event_over_stale_job_snapshot(self) -> None:
        project, document = self._create_project_with_document("-status-terminal-event")
        job = db.create_ai_analysis_job(
            project_id=project["id"],
            sor_document_id=document["id"],
            total_requirements=1,
            status="RUNNING",
        )
        stale_job = dict(job)
        db.update_ai_analysis_job(
            job["id"],
            status="COMPLETED",
            current_stage="completed",
            completed_requirements=1,
            finished_at="2026-07-11T00:00:00+00:00",
        )
        db.add_ai_analysis_event(
            job_id=job["id"],
            sor_document_id=document["id"],
            event_type="job_completed",
            stage="completed",
            message="completed",
            payload={"outcome": "COMPLETED", "published": True},
        )

        payload = main._build_ai_analysis_status_payload(
            project["id"],
            document["id"],
            job=stale_job,
        )

        self.assertEqual(payload["job"]["status"], "COMPLETED")
        self.assertEqual(payload["job"]["current_stage"], "completed")
        self.assertEqual(payload["job"]["outcome"], "COMPLETED")

    def test_published_job_remains_completed_when_workspace_response_assembly_fails(self) -> None:
        project, document = self._create_project_with_document("-published-workspace-failure")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 7.1",
            requirement_text="Supplier shall provide a validation report.",
            raw_text="Supplier shall provide a validation report.",
            display_text="Supplier shall provide a validation report.",
            requirement_type="REGULATION",
            priority="MEDIUM",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
        with patch.dict(os.environ, {"REQPILOT_AI_ASSESSMENT_MODE": "heuristic"}, clear=False):
            ai_config = main.load_ai_copilot_config("azure")
        job = db.create_ai_analysis_job(
            project_id=project["id"],
            sor_document_id=document["id"],
            profile_id=ai_config.profile_id,
            provider=ai_config.provider,
            assessment_mode=ai_config.assessment_mode,
            retrieval_backend=ai_config.retrieval_backend,
            total_requirements=1,
        )
        runtime_thread = ImmediateThread()
        main._register_ai_analysis_thread(document["id"], runtime_thread, job_id=job["id"])
        result = {
            "assessment": {
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "heuristic serving result",
                "ai_decision": "ACCEPT",
                "ai_reason": "canonical evidence supports the requirement",
                "ai_confidence": 0.9,
                "risk_level": "LOW",
                "clarification_question": "",
                "normalized_requirement": "Supplier shall provide a validation report.",
                "assessment_mode": "heuristic",
                "retrieval_backend": "local",
                "model_name": "rules-rag-prototype",
                "prompt_version": "heuristic-v1",
            },
            "evidences": [],
            "runs": [],
        }

        try:
            eligible_rows = db.list_requirements(project["id"], document["id"])
            prepared = (project, locked_document, eligible_rows, eligible_rows, [], ai_config)
            with patch.object(main, "_prepare_document_ai_analysis_request", return_value=prepared), patch.object(
                main,
                "run_ai_copilot_assessment",
                return_value=result,
            ), patch.object(
                main,
                "_build_document_workspace",
                side_effect=RuntimeError("workspace response assembly failed"),
            ):
                main._run_document_ai_analysis_worker(job["id"], project["id"], document["id"])
        finally:
            main._clear_ai_analysis_thread(document["id"])

        stored_job = db.get_ai_analysis_job(job["id"]) or {}
        self.assertEqual(stored_job.get("status"), "COMPLETED")
        self.assertEqual(stored_job.get("current_stage"), "completed")
        self.assertEqual((db.get_sor_document(document["id"]) or {}).get("ai_status"), "READY")
        self.assertEqual((db.get_assessment(requirement["id"]) or {}).get("model_name"), "rules-rag-prototype")
        event_types = [item.get("event_type") for item in db.list_ai_analysis_events(job["id"])]
        self.assertIn("job_completed", event_types)
        self.assertNotIn("job_failed", event_types)

        with patch.object(main, "run_ai_copilot_assessment", return_value=result), patch.object(
            main,
            "_build_document_workspace",
            side_effect=RuntimeError("sync projection failed after commit"),
        ):
            sync_result = main._execute_document_ai_analysis(
                project,
                locked_document,
                eligible_rows,
                [],
                ai_config=ai_config,
            )
        self.assertTrue(sync_result["ai_analysis_published"])
        self.assertEqual(sync_result["ai_analysis_outcome"], "COMPLETED")
        self.assertEqual(sync_result["workspace_projection_status"], "FAILED")
        self.assertIn("sync projection failed", sync_result["workspace_projection_error"])
        self.assertTrue(sync_result["workspace_projection"]["published"])
        self.assertTrue(sync_result["workspace_requirements_current"])
        self.assertEqual(sync_result["requirements"][0]["model_name"], "rules-rag-prototype")

    def test_stop_rejects_cancellation_after_atomic_publication_cutoff(self) -> None:
        project, document = self._create_project_with_document("-publishing-cancel-cutoff")
        job = db.create_ai_analysis_job(
            project_id=project["id"],
            sor_document_id=document["id"],
            total_requirements=1,
            status="RUNNING",
        )
        runtime_thread = ImmediateThread()
        main._register_ai_analysis_thread(document["id"], runtime_thread, job_id=job["id"])
        self.assertTrue(main._begin_ai_analysis_publish(document["id"], job["id"]))

        try:
            with patch.object(main, "_has_live_ai_analysis_thread", return_value=True):
                with self.assertRaises(HTTPException) as raised:
                    main.stop_document_ai_analysis(project["id"], document["id"])
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("atomic result publication", str(raised.exception.detail))
            self.assertEqual((db.get_ai_analysis_job(job["id"]) or {}).get("status"), "RUNNING")
        finally:
            main._clear_ai_analysis_thread(document["id"])

    def test_ai_analysis_job_primary_fallback_is_failed_and_not_published(self) -> None:
        project, document = self._create_project_with_document("-ai-primary-fallback")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 5.1",
            requirement_text="Supplier shall provide a validation report.",
            raw_text="Supplier shall provide a validation report.",
            display_text="Supplier shall provide a validation report.",
            requirement_type="REGULATION",
            priority="MEDIUM",
            parent_section_code="5.1",
            parent_section_title="Validation",
            parent_section_label="5.1 Validation",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
        db.upsert_assessment(
            requirement_id=requirement["id"],
            profile_id="azure",
            provider="azure",
            ai_decision="ACCEPT",
            ai_reason="last good result before primary provider fallback",
            ai_confidence=0.87,
            assessment_mode="heuristic",
            retrieval_backend="local",
            analysis_label="REQUIREMENT",
            analysis_basis="last good result",
            normalized_requirement="Supplier shall provide a validation report.",
            risk_level="LOW",
            clarification_question="",
            model_name="old-model",
            prompt_version="old-prompt",
        )
        previous_assessment = db.get_assessment(requirement["id"])
        with patch.dict(os.environ, {"REQPILOT_AI_ASSESSMENT_MODE": "llm_primary"}, clear=False):
            ai_config = main.load_ai_copilot_config("azure")
        eligible_rows = db.list_requirements(project["id"], document["id"])
        prepared = (project, locked_document, eligible_rows, eligible_rows, [], ai_config)
        fallback_result = {
            "assessment": {
                "profile_id": "azure",
                "provider": "azure",
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "heuristic fallback candidate",
                "ai_decision": "NEED_CLARIFICATION",
                "ai_reason": "fallback result",
                "ai_confidence": 0.5,
                "risk_level": "MEDIUM",
                "clarification_question": "Provide evidence.",
                "normalized_requirement": "Supplier shall provide a validation report.",
                "assessment_mode": "heuristic_fallback",
                "retrieval_backend": "local",
                "model_name": "heuristic-v1",
                "prompt_version": "fallback-v1",
            },
            "evidences": [],
            "runs": [
                {
                    "profile_id": "azure",
                    "provider": "azure",
                    "mode": "llm_primary",
                    "retrieval_backend": "local",
                    "model_name": "gpt",
                    "prompt_version": "v1",
                    "input_payload_json": "{}",
                    "output_payload_json": "",
                    "validation_status": "failed",
                    "fallback_reason": "provider unavailable",
                },
                {
                    "profile_id": "azure",
                    "provider": "azure",
                    "mode": "heuristic_fallback",
                    "retrieval_backend": "local",
                    "model_name": "heuristic-v1",
                    "prompt_version": "fallback-v1",
                    "input_payload_json": "{}",
                    "output_payload_json": "{}",
                    "validation_status": "accepted",
                    "fallback_reason": "llm_primary_failed: provider unavailable",
                },
            ],
        }

        with patch.object(main, "_prepare_document_ai_analysis_request", return_value=prepared), patch.object(
            main,
            "run_ai_copilot_assessment",
            return_value=fallback_result,
        ), patch.object(main, "Thread", ImmediateThread):
            with TestClient(main.app) as client:
                status_payload = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                    json=self._ai_start_payload(document["id"]),
                ).json()

        self.assertEqual(status_payload["job"]["status"], "FAILED")
        self.assertEqual(status_payload["job"]["completed_requirements"], 0)
        self.assertIn("provider unavailable", status_payload["job"]["error_message"])
        self.assertEqual(status_payload["document"]["ai_status"], "STALE")
        self.assertEqual(db.get_assessment(requirement["id"]), previous_assessment)
        self.assertEqual(len(db.list_assessment_runs(requirement["id"])), 2)

    def test_ai_analysis_job_shadow_failure_reports_warning_with_heuristic_serving_result(self) -> None:
        project, document = self._create_project_with_document("-ai-shadow-warning")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 5.1",
            requirement_text="Supplier shall provide a validation report.",
            raw_text="Supplier shall provide a validation report.",
            display_text="Supplier shall provide a validation report.",
            requirement_type="REGULATION",
            priority="MEDIUM",
            parent_section_code="5.1",
            parent_section_title="Validation",
            parent_section_label="5.1 Validation",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
        with patch.dict(os.environ, {"REQPILOT_AI_ASSESSMENT_MODE": "llm_shadow"}, clear=False):
            ai_config = main.load_ai_copilot_config("azure")
        eligible_rows = db.list_requirements(project["id"], document["id"])
        prepared = (project, locked_document, eligible_rows, eligible_rows, [], ai_config)
        shadow_result = {
            "assessment": {
                "profile_id": "azure",
                "provider": "azure",
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "heuristic serving result",
                "ai_decision": "NEED_CLARIFICATION",
                "ai_reason": "heuristic result remains authoritative",
                "ai_confidence": 0.6,
                "risk_level": "MEDIUM",
                "clarification_question": "Provide evidence.",
                "normalized_requirement": "Supplier shall provide a validation report.",
                "assessment_mode": "heuristic",
                "retrieval_backend": "local",
                "model_name": "heuristic-v1",
                "prompt_version": "heuristic-v1",
            },
            "evidences": [],
            "runs": [
                {
                    "profile_id": "azure",
                    "provider": "azure",
                    "mode": "heuristic",
                    "retrieval_backend": "local",
                    "model_name": "heuristic-v1",
                    "prompt_version": "heuristic-v1",
                    "input_payload_json": "{}",
                    "output_payload_json": "{}",
                    "validation_status": "accepted",
                    "fallback_reason": "",
                },
                {
                    "profile_id": "azure",
                    "provider": "azure",
                    "mode": "screening_llm_shadow",
                    "retrieval_backend": "screening_only",
                    "model_name": "gpt-screening",
                    "prompt_version": "shadow-screening-v1",
                    "input_payload_json": "{}",
                    "output_payload_json": "invalid",
                    "validation_status": "fallback",
                    "fallback_reason": "screening schema invalid",
                },
                {
                    "profile_id": "azure",
                    "provider": "azure",
                    "mode": "llm_shadow",
                    "retrieval_backend": "local",
                    "model_name": "gpt",
                    "prompt_version": "shadow-v1",
                    "input_payload_json": "{}",
                    "output_payload_json": "",
                    "validation_status": "failed",
                    "fallback_reason": "shadow provider unavailable",
                },
            ],
        }

        with patch.object(main, "_prepare_document_ai_analysis_request", return_value=prepared), patch.object(
            main,
            "run_ai_copilot_assessment",
            return_value=shadow_result,
        ), patch.object(main, "Thread", ImmediateThread):
            with TestClient(main.app) as client:
                status_payload = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                    json=self._ai_start_payload(document["id"]),
                ).json()

        self.assertEqual(status_payload["job"]["status"], "COMPLETED")
        self.assertEqual(status_payload["job"]["outcome"], "COMPLETED_WITH_WARNINGS")
        self.assertEqual(status_payload["job"]["warning_count"], 1)
        self.assertIn("shadow", status_payload["job"]["last_message"])
        self.assertEqual(status_payload["document"]["ai_status"], "READY")
        self.assertEqual((db.get_assessment(requirement["id"]) or {}).get("assessment_mode"), "heuristic")
        completion_event = next(
            item for item in status_payload["events"] if item.get("event_type") == "job_completed"
        )
        self.assertEqual(completion_event["payload"]["outcome"], "COMPLETED_WITH_WARNINGS")
        self.assertEqual(completion_event["payload"]["serving_mode"], "heuristic")
        self.assertEqual(completion_event["payload"]["warning_requirement_count"], 1)

        healthy_shadow_result = {
            **shadow_result,
            "runs": [
                {
                    **shadow_result["runs"][0],
                    "mode": "screening_llm_shadow",
                    "validation_status": "accepted",
                    "fallback_reason": "",
                },
                {
                    **shadow_result["runs"][0],
                    "mode": "llm_shadow",
                    "validation_status": "accepted",
                    "fallback_reason": "",
                },
                shadow_result["runs"][0],
            ],
        }
        with patch.object(main, "_prepare_document_ai_analysis_request", return_value=prepared), patch.object(
            main,
            "run_ai_copilot_assessment",
            return_value=healthy_shadow_result,
        ), patch.object(main, "Thread", ImmediateThread):
            with TestClient(main.app) as client:
                healthy_status = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                    json=self._ai_start_payload(document["id"]),
                ).json()

        self.assertEqual(healthy_status["job"]["status"], "COMPLETED")
        self.assertEqual(healthy_status["job"]["outcome"], "COMPLETED")
        self.assertEqual(healthy_status["job"]["warning_requirement_count"], 0)
        self.assertEqual(healthy_status["job"]["serving_mode"], "heuristic")
        healthy_completion_event = next(
            item for item in healthy_status["events"] if item.get("event_type") == "job_completed"
        )
        self.assertEqual(healthy_completion_event["payload"]["serving_mode"], "heuristic")

        with patch.object(main, "run_ai_copilot_assessment", return_value=shadow_result):
            sync_result = main._execute_document_ai_analysis(
                project,
                locked_document,
                eligible_rows,
                [],
                ai_config=ai_config,
            )
        self.assertEqual(sync_result["ai_analysis_outcome"], "COMPLETED_WITH_WARNINGS")
        self.assertEqual(sync_result["ai_analysis_warning_requirement_count"], 1)
        self.assertIn("shadow provider unavailable", sync_result["ai_analysis_warning_reasons"])

    def test_run_ai_analysis_marks_info_rows_and_skips_manual_decision(self) -> None:
        project, document = self._create_project_with_document("-ai-info")
        info_requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 4.5",
            requirement_text="Functional status A",
            raw_text="Functional status A",
            display_text="Functional status A",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="4.5",
            parent_section_title="Status",
            parent_section_label="4.5 Status",
        )
        db.add_knowledge_document(
            project_id=project["id"],
            file_name="platform-spec.txt",
            file_type="txt",
            file_path=str(self.tmp_path / "platform-spec-info.txt"),
            extracted_text=(
                "Status naming reference only. "
                "This note defines the approved status labels used in engineering reports."
            ),
            doc_version="v1.0",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        self._lock_active_requirement_set(project["id"], document["id"])

        with patch.dict(
            os.environ,
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "heuristic",
                "REQPILOT_AI_TRACE_DIR": str(self.tmp_path / "ai_traces_info"),
            },
            clear=False,
        ):
            result = main.run_document_ai_analysis(
                project["id"],
                document["id"],
                self._active_requirement_set_id(document["id"]),
            )

        row = next(item for item in result["requirements"] if item["id"] == info_requirement["id"])
        self.assertEqual(row["analysis_label"], "INFO")
        self.assertTrue(row["analysis_basis"])
        self.assertEqual(row["ai_decision"], "")
        self.assertEqual(row["evidences"], [])
        self.assertEqual(result["ai_processed_requirements"], 0)
        self.assertEqual(result["ai_info_requirements"], 1)

    def test_ai_analysis_job_api_exposes_reasoning_stream_events(self) -> None:
        project, document = self._create_project_with_document("-ai-reasoning-events")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 4.5",
            requirement_text="Supplier shall submit CAMDS data before SOP.",
            raw_text="Supplier shall submit CAMDS data before SOP.",
            display_text="Supplier shall submit CAMDS data before SOP.",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="4.5",
            parent_section_title="Compliance",
            parent_section_label="4.5 Compliance",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
        ai_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.tmp_path / "ai_traces_reasoning_events",
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.7-plus",
            screening_model="qwen3.6-flash",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="http://localhost:6333",
            qdrant_collection_name="reqpilot-knowledge-aliyun-local",
        )
        eligible_rows = db.list_requirements(project["id"], document["id"])
        prepared = (project, locked_document, eligible_rows, eligible_rows, [], ai_config)

        def fake_run_ai_copilot_assessment(
            requirement_row: dict[str, object],
            knowledge_docs: list[dict[str, object]],
            *,
            config: AICopilotConfig | None = None,
            forced_analysis_label: str | None = None,
            forced_analysis_basis: str = "",
            progress_callback=None,
        ) -> dict[str, object]:
            if progress_callback:
                progress_callback(
                    {
                        "type": "stage",
                        "stage": "screening",
                        "mode": "screening_llm_shadow",
                        "model_name": "qwen3.6-flash",
                        "message": "正在进行 requirement / INFO 筛选。",
                    }
                )
                progress_callback(
                    {
                        "type": "reasoning",
                        "stage": "screening",
                        "mode": "screening_llm_shadow",
                        "model_name": "qwen3.6-flash",
                        "delta": "先判断这条是否属于真正需要合规结论的 requirement。",
                        "content": "先判断这条是否属于真正需要合规结论的 requirement。",
                        "characters_emitted": 24,
                    }
                )
                progress_callback(
                    {
                        "type": "answer",
                        "stage": "screening",
                        "mode": "screening_llm_shadow",
                        "model_name": "qwen3.6-flash",
                        "delta": "{\"analysis_label\":\"REQUIREMENT\",\"reason\":\"该条涉及 CAMDS 填报义务。\"}",
                        "content": "{\"analysis_label\":\"REQUIREMENT\",\"reason\":\"该条涉及 CAMDS 填报义务。\"}",
                        "characters_emitted": 58,
                    }
                )
                progress_callback(
                    {
                        "type": "stage",
                        "stage": "retrieval",
                        "mode": "screening_llm_shadow",
                        "model_name": "qwen3.6-flash",
                        "message": "筛选完成，正在检索候选证据。",
                    }
                )
                progress_callback(
                    {
                        "type": "stage",
                        "stage": "assessment",
                        "mode": "llm_shadow",
                        "model_name": "qwen3.7-plus",
                        "message": "候选证据已就绪，正在生成结构化结论。",
                    }
                )
                progress_callback(
                    {
                        "type": "reasoning",
                        "stage": "assessment",
                        "mode": "llm_shadow",
                        "model_name": "qwen3.7-plus",
                        "delta": "先检查 requirement 的约束条件。",
                        "content": "先检查 requirement 的约束条件。",
                        "characters_emitted": 16,
                    }
                )
                progress_callback(
                    {
                        "type": "reasoning",
                        "stage": "assessment",
                        "mode": "llm_shadow",
                        "model_name": "qwen3.7-plus",
                        "delta": " 再核对候选证据是否真正覆盖 CAMDS 义务。",
                        "content": "先检查 requirement 的约束条件。 再核对候选证据是否真正覆盖 CAMDS 义务。",
                        "characters_emitted": 40,
                    }
                )
                progress_callback(
                    {
                        "type": "answer",
                        "stage": "assessment",
                        "mode": "llm_shadow",
                        "model_name": "qwen3.7-plus",
                        "delta": "{\"normalized_requirement\":\"Supplier shall submit CAMDS data before SOP.\",",
                        "content": "{\"normalized_requirement\":\"Supplier shall submit CAMDS data before SOP.\",",
                        "characters_emitted": 72,
                    }
                )
            return {
                "assessment": {
                    "profile_id": "aliyun",
                    "provider": "aliyun",
                    "analysis_label": "REQUIREMENT",
                    "analysis_basis": "heuristic serving result; shadow output is observation only.",
                    "ai_decision": "NEED_CLARIFICATION",
                    "ai_reason": "候选证据无法直接覆盖 CAMDS 数据填报义务。",
                    "ai_confidence": 0.91,
                    "risk_level": "HIGH",
                    "clarification_question": "请补充 CAMDS 数据提交记录。",
                    "normalized_requirement": "Supplier shall submit CAMDS data before SOP.",
                    "assessment_mode": "heuristic",
                    "retrieval_backend": "qdrant",
                    "model_name": "heuristic-v1",
                    "prompt_version": "heuristic-v1",
                },
                "evidences": [],
                "runs": [
                    {
                        "profile_id": "aliyun",
                        "provider": "aliyun",
                        "mode": "screening_llm_shadow",
                        "retrieval_backend": "screening_only",
                        "model_name": "qwen3.6-flash",
                        "prompt_version": "v2.1-requirement-screening-aliyun",
                        "input_payload_json": "{}",
                        "output_payload_json": "{\"analysis_label\":\"REQUIREMENT\"}",
                        "validation_status": "accepted",
                        "fallback_reason": "",
                    },
                    {
                        "profile_id": "aliyun",
                        "provider": "aliyun",
                        "mode": "llm_shadow",
                        "retrieval_backend": "qdrant",
                        "model_name": "qwen3.7-plus",
                        "prompt_version": "v3.0-compliance-evidence-contract",
                        "input_payload_json": "{}",
                        "output_payload_json": "{\"proposed_decision\":\"NEED_CLARIFICATION\"}",
                        "validation_status": "accepted",
                        "fallback_reason": "",
                    },
                ],
                "trace_path": str(self.tmp_path / "trace-reasoning-events.json"),
            }

        with patch.object(main, "_prepare_document_ai_analysis_request", return_value=prepared), patch.object(
            main,
            "run_ai_copilot_assessment",
            side_effect=fake_run_ai_copilot_assessment,
        ), patch.object(main, "Thread", ImmediateThread):
            with TestClient(main.app) as client:
                start_response = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                    json=self._ai_start_payload(
                        document["id"],
                        profile_id="aliyun",
                    ),
                )
                self.assertEqual(start_response.status_code, 200)
                status_response = client.get(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis-status"
                )
                self.assertEqual(status_response.status_code, 200)
                status_payload = status_response.json()

        reasoning_events = [
            item for item in status_payload["events"] if item.get("event_type") == "requirement_reasoning"
        ]
        self.assertTrue(reasoning_events)
        self.assertTrue(
            any("CAMDS" in str(item.get("payload", {}).get("content_preview") or "") for item in reasoning_events)
        )
        screening_reasoning_events = [
            item for item in status_payload["events"] if item.get("event_type") == "requirement_screening_reasoning"
        ]
        self.assertTrue(screening_reasoning_events)
        self.assertTrue(
            any("REQUIREMENT" in str(item.get("payload", {}).get("content_preview") or "") for item in status_payload["events"] if item.get("event_type") == "requirement_screening_output")
        )
        self.assertTrue(
            any(item.get("event_type") == "requirement_stage_update" for item in status_payload["events"])
        )
        self.assertTrue(
            any(item.get("event_type") == "requirement_output" for item in status_payload["events"])
        )
        self.assertTrue(
            any(item.get("event_type") == "requirement_answer_started" for item in status_payload["events"])
        )
        self.assertTrue(
            any(item.get("event_type") == "requirement_completed" for item in status_payload["events"])
        )
        updated_row = db.get_requirement(requirement["id"]) or {}
        self.assertEqual(updated_row.get("assessment_mode"), "heuristic")

    def test_ai_analysis_job_can_be_stopped_from_api(self) -> None:
        project, document = self._create_project_with_document("-ai-stop")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 4.5",
            requirement_text="Supplier shall submit CAMDS data before SOP.",
            raw_text="Supplier shall submit CAMDS data before SOP.",
            display_text="Supplier shall submit CAMDS data before SOP.",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="4.5",
            parent_section_title="Compliance",
            parent_section_label="4.5 Compliance",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        locked_document = self._lock_active_requirement_set(project["id"], document["id"])["document"]
        db.upsert_assessment(
            requirement_id=requirement["id"],
            profile_id="azure",
            provider="azure",
            ai_decision="ACCEPT",
            ai_reason="last good result before cancellation",
            ai_confidence=0.87,
            assessment_mode="heuristic",
            retrieval_backend="local",
            analysis_label="REQUIREMENT",
            analysis_basis="last good result",
            normalized_requirement="Supplier shall submit CAMDS data before SOP.",
            risk_level="LOW",
            clarification_question="",
            model_name="old-model",
            prompt_version="old-prompt",
        )
        previous_assessment = db.get_assessment(requirement["id"])
        ai_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.tmp_path / "ai_traces_stop",
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.7-plus",
            screening_model="qwen3.6-flash",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="http://localhost:6333",
            qdrant_collection_name="reqpilot-knowledge-aliyun-local",
        )
        eligible_rows = db.list_requirements(project["id"], document["id"])
        prepared = (project, locked_document, eligible_rows, eligible_rows, [], ai_config)
        started = Event()

        def fake_run_ai_copilot_assessment(
            requirement_row: dict[str, object],
            knowledge_docs: list[dict[str, object]],
            *,
            config: AICopilotConfig | None = None,
            forced_analysis_label: str | None = None,
            forced_analysis_basis: str = "",
            progress_callback=None,
        ) -> dict[str, object]:
            if progress_callback:
                progress_callback(
                    {
                        "type": "stage",
                        "stage": "assessment",
                        "mode": "llm_shadow",
                        "model_name": "qwen3.7-plus",
                        "message": "候选证据已就绪，正在生成结构化结论。",
                    }
                )
                started.set()
                for index in range(200):
                    progress_callback(
                        {
                            "type": "reasoning",
                            "stage": "assessment",
                            "mode": "llm_shadow",
                            "model_name": "qwen3.7-plus",
                            "delta": f"thinking-{index}-" + ("x" * 200),
                            "content": f"thinking-{index}-" + ("x" * 200),
                            "characters_emitted": (index + 1) * 210,
                        }
                    )
                    time.sleep(0.02)
            return {
                "assessment": {
                    "profile_id": "aliyun",
                    "provider": "aliyun",
                    "analysis_label": "REQUIREMENT",
                    "analysis_basis": "已完成LLM分析。",
                    "ai_decision": "NEED_CLARIFICATION",
                    "ai_reason": "候选证据无法直接覆盖 CAMDS 数据填报义务。",
                    "ai_confidence": 0.91,
                    "risk_level": "HIGH",
                    "clarification_question": "请补充 CAMDS 数据提交记录。",
                    "normalized_requirement": "Supplier shall submit CAMDS data before SOP.",
                    "assessment_mode": "llm_shadow",
                    "retrieval_backend": "qdrant",
                    "model_name": "qwen3.7-plus",
                    "prompt_version": "v3.0-compliance-evidence-contract",
                },
                "evidences": [],
                "runs": [],
                "trace_path": str(self.tmp_path / "trace-stop.json"),
            }

        with patch.object(main, "_prepare_document_ai_analysis_request", return_value=prepared), patch.object(
            main,
            "run_ai_copilot_assessment",
            side_effect=fake_run_ai_copilot_assessment,
        ):
            with TestClient(main.app) as client:
                start_response = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/start",
                    json=self._ai_start_payload(
                        document["id"],
                        profile_id="aliyun",
                    ),
                )
                self.assertEqual(start_response.status_code, 200)
                self.assertTrue(started.wait(timeout=1.5))

                stop_response = client.post(
                    f"/api/projects/{project['id']}/sor-documents/{document['id']}/run-ai-analysis/stop"
                )
                self.assertEqual(stop_response.status_code, 200)
                self.assertEqual(stop_response.json()["job"]["status"], "CANCEL_REQUESTED")

                deadline = time.time() + 3.0
                status_payload = stop_response.json()
                while time.time() < deadline:
                    status_response = client.get(
                        f"/api/projects/{project['id']}/sor-documents/{document['id']}/ai-analysis-status"
                    )
                    self.assertEqual(status_response.status_code, 200)
                    status_payload = status_response.json()
                    if str(status_payload.get("job", {}).get("status") or "").upper() == "CANCELLED":
                        break
                    time.sleep(0.05)

        self.assertEqual(str(status_payload.get("job", {}).get("status") or "").upper(), "CANCELLED")
        self.assertEqual(str(status_payload.get("job", {}).get("current_stage") or "").lower(), "cancelled")
        self.assertEqual(status_payload["job"]["outcome"], "CANCELLED")
        self.assertEqual(status_payload["job"]["completed_requirements"], 0)
        event_types = [str(item.get("event_type") or "") for item in status_payload.get("events") or []]
        self.assertIn("job_cancel_requested", event_types)
        self.assertIn("job_cancelled", event_types)
        self.assertTrue(
            any("已停止" in str(item.get("message") or "") for item in status_payload.get("events") or [])
        )
        refreshed_document = db.get_sor_document(document["id"]) or {}
        self.assertEqual(str(refreshed_document.get("ai_status") or "").upper(), "STALE")
        self.assertEqual(db.get_assessment(requirement["id"]), previous_assessment)

    def test_requirement_import_records_but_does_not_serve_primary_fallback(self) -> None:
        project, document = self._create_project_with_document("-import-primary-fallback")
        with patch.dict(os.environ, {"REQPILOT_AI_ASSESSMENT_MODE": "llm_primary"}, clear=False):
            ai_config = main.load_ai_copilot_config("azure")
        fallback_result = {
            "assessment": {
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "provider fallback",
                "ai_decision": "NEED_CLARIFICATION",
                "ai_reason": "provider unavailable",
                "ai_confidence": 0.2,
                "risk_level": "HIGH",
                "clarification_question": "Provide evidence.",
                "normalized_requirement": "Supplier shall provide a report.",
                "assessment_mode": "heuristic_fallback",
                "retrieval_backend": "local",
                "model_name": "heuristic-v1",
                "prompt_version": "fallback-v1",
            },
            "evidences": [],
            "runs": [
                {
                    "mode": "llm_primary",
                    "validation_status": "failed",
                    "fallback_reason": "provider unavailable",
                    "profile_id": "azure",
                    "provider": "azure",
                    "retrieval_backend": "local",
                    "model_name": "gpt",
                    "prompt_version": "v1",
                    "input_payload_json": "{}",
                    "output_payload_json": "",
                }
            ],
        }
        rows = [
            {
                "req_code": "REQ0001",
                "display_text": "Supplier shall provide a report.",
                "raw_text": "Supplier shall provide a report.",
                "requirement_type": "FUNCTIONAL",
                "priority": "MEDIUM",
                "source_anchor": "Page 1 | Section 4.5",
            }
        ]

        with patch.object(main, "load_ai_copilot_config", return_value=ai_config), patch.object(
            main,
            "_ensure_profile_knowledge_syncs",
        ), patch.object(main, "run_ai_copilot_assessment", return_value=fallback_result):
            inserted = main._persist_requirements(
                project_id=project["id"],
                sor_document_id=document["id"],
                rows=rows,
                knowledge_docs=[{"id": "kb-1"}],
            )

        imported = db.list_requirements(project["id"], document["id"])
        self.assertEqual(inserted, 1)
        self.assertEqual(len(imported), 1)
        self.assertIsNone(db.get_assessment(imported[0]["id"]))
        self.assertEqual(db.list_assessment_runs(imported[0]["id"])[0]["validation_status"], "failed")

    def test_run_ai_analysis_without_knowledge_docs_only_screens_requirement_rows(self) -> None:
        project, document = self._create_project_with_document("-screen-only")
        requirement = db.insert_requirement(
            project_id=project["id"],
            sor_document_id=document["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="Page 1 | Section 4.5",
            requirement_text="ECU shall support wake-up time less than 100ms.",
            raw_text="ECU shall support wake-up time less than 100ms.",
            display_text="ECU shall support wake-up time less than 100ms.",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            parent_section_code="4.5",
            parent_section_title="Status",
            parent_section_label="4.5 Status",
        )
        db.increment_sor_document_requirement_set_version(document["id"], "REVIEWING")
        self._lock_active_requirement_set(project["id"], document["id"])

        with patch.dict(
            os.environ,
            {
                "REQPILOT_AI_ASSESSMENT_MODE": "heuristic",
                "REQPILOT_AI_TRACE_DIR": str(self.tmp_path / "ai_traces_screen_only"),
            },
            clear=False,
        ):
            result = main.run_document_ai_analysis(
                project["id"],
                document["id"],
                self._active_requirement_set_id(document["id"]),
            )

        row = next(item for item in result["requirements"] if item["id"] == requirement["id"])
        self.assertEqual(row["analysis_label"], "REQUIREMENT")
        self.assertEqual(row["assessment_mode"], "screening_only")
        self.assertTrue(row["analysis_basis"])
        self.assertEqual(row["ai_decision"], "")
        self.assertEqual(row["evidences"], [])
        self.assertEqual(result["ai_processed_requirements"], 0)
        self.assertEqual(result["ai_info_requirements"], 0)
        self.assertEqual(result["ai_screened_requirements"], 1)

    def test_upload_knowledge_document_accepts_metadata_fields(self) -> None:
        project, _document = self._create_project_with_document("-knowledge-metadata")
        knowledge_dir = self.tmp_path / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        upload = UploadFile(filename="spec.txt", file=io.BytesIO("Spec content".encode("utf-8")))

        with patch.object(main, "KNOWLEDGE_DIR", knowledge_dir), patch.object(
            main,
            "require_knowledge_upload_capability",
        ), patch.object(
            main,
            "_project_knowledge_sync_profile_ids",
            return_value=["aliyun"],
        ), patch.object(
            main,
            "_sync_knowledge_document_targets",
            side_effect=lambda _project_id, document, **_kwargs: document,
        ):
            created = asyncio.run(
                main.upload_knowledge_document(
                    project["id"],
                    file=upload,
                    doc_type="STANDARD",
                    doc_version="V1.2",
                    status="ACTIVE",
                    effective_from="2026-01-01",
                    effective_to="2026-12-31",
                    authority="CUSTOMER_APPROVED",
                    claim_role="NORMATIVE_REQUIREMENT",
                    applicability="Platform X / release V1.2",
                )
            )

        listed = main.list_knowledge_documents(project["id"])
        self.assertEqual(created["doc_type"], "STANDARD")
        self.assertEqual(created["doc_version"], "V1.2")
        self.assertEqual(created["status"], "ACTIVE")
        self.assertEqual(created["effective_from"], "2026-01-01")
        self.assertEqual(created["effective_to"], "2026-12-31")
        self.assertEqual(created["authority"], "CUSTOMER_APPROVED")
        self.assertEqual(created["claim_role"], "NORMATIVE_REQUIREMENT")
        self.assertEqual(
            created["applicability"],
            "Platform X / release V1.2",
        )
        self.assertEqual(listed[0]["doc_type"], "STANDARD")
        self.assertEqual(listed[0]["authority"], "CUSTOMER_APPROVED")
        self.assertEqual(listed[0]["doc_version"], "V1.2")

    def test_upload_knowledge_document_syncs_only_default_profile_on_upload(self) -> None:
        project, _document = self._create_project_with_document("-knowledge-default-sync")
        knowledge_dir = self.tmp_path / "knowledge_default_sync"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        upload = UploadFile(filename="spec.txt", file=io.BytesIO("Spec content".encode("utf-8")))
        captured: dict[str, object] = {}

        def fake_sync(project_id: str, document: dict, *, profile_ids: list[str] | None = None) -> dict:
            captured["project_id"] = project_id
            captured["profile_ids"] = list(profile_ids or [])
            return {
                **document,
                "retrieval_sync_status": "READY",
                "retrieval_sync_error": "",
                "retrieval_synced_at": "2026-04-02T00:00:00+00:00",
                "retrieval_syncs": [
                    {
                        "profile_id": "azure",
                        "provider": "azure",
                        "retrieval_backend": "azure_sdk",
                        "status": "READY",
                        "synced_at": "2026-04-02T00:00:00+00:00",
                        "chunk_count": 1,
                        "payload_path": str(self.tmp_path / "payload.json"),
                    }
                ],
            }

        with patch.object(main, "KNOWLEDGE_DIR", knowledge_dir), patch.object(
            main,
            "require_knowledge_upload_capability",
        ), patch.object(
            main,
            "_project_knowledge_sync_profile_ids",
            return_value=["azure"],
        ), patch.object(
            main,
            "_sync_knowledge_document_targets",
            side_effect=fake_sync,
        ):
            created = asyncio.run(
                main.upload_knowledge_document(
                    project["id"],
                    file=upload,
                    doc_type="REFERENCE",
                    doc_version="",
                    status="ACTIVE",
                    effective_from="",
                    effective_to="",
                )
            )

        self.assertEqual(captured["project_id"], project["id"])
        self.assertEqual(captured["profile_ids"], ["azure"])
        self.assertEqual(created["retrieval_syncs"][0]["profile_id"], "azure")

    def test_runtime_ai_provider_setup_endpoint_can_switch_default_profile(self) -> None:
        production_config = Path(__file__).resolve().parents[1] / "storage/config.json"
        production_before = (
            production_config.read_bytes(),
            production_config.stat().st_mtime_ns,
        ) if production_config.exists() else None
        with TestClient(main.app) as client:
            response = client.post(
                "/api/runtime/ai-provider-setup",
                json={
                    "default_profile_id": "aliyun",
                    "aliyun": {
                        "api_key": "aliyun-key",
                        "chat_model": "qwen3.7-plus",
                        "embedding_model": "text-embedding-v4",
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "qdrant_url": "http://localhost:6333",
                        "qdrant_collection": "reqpilot-knowledge-aliyun-local",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["default_profile_id"], "aliyun")
        self.assertTrue(payload["aliyun"]["api_key_configured"])
        self.assertEqual(payload["aliyun"]["embedding_model"], "text-embedding-v4")
        if production_before is None:
            self.assertFalse(production_config.exists())
        else:
            self.assertEqual(production_config.read_bytes(), production_before[0])
            self.assertEqual(production_config.stat().st_mtime_ns, production_before[1])

    def test_runtime_worker_fleet_endpoint_is_revision_aware(self) -> None:
        with TestClient(main.app) as client:
            response = client.get("/api/runtime/worker-fleet")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["schema_version"], "runtime-worker-fleet-status/v1")
        self.assertTrue(payload["revision_required"])
        self.assertEqual(payload["required_kind"], "document.extract")
        self.assertIn(payload["state"], {"READY", "SYNCING", "UNAVAILABLE"})

    def test_runtime_worker_fleet_route_forces_current_revision_ack(self) -> None:
        from app.application import runtime

        captured: dict[str, object] = {}

        class Queue:
            @staticmethod
            def worker_fleet_status(**kwargs):
                captured.update(kwargs)
                return {
                    "schema_version": "runtime-worker-fleet-status/v1",
                    "state": "SYNCING",
                    "desired_connection_revision": kwargs["desired_revision"],
                    "required_kind": kwargs["required_kind"],
                    "revision_required": True,
                    "registered_worker_count": 1,
                    "live_worker_count": 1,
                    "eligible_worker_count": 0,
                    "idle_worker_count": 1,
                    "active_worker_count": 0,
                    "draining_worker_count": 0,
                    "stale_revision_worker_count": 1,
                    "workers": [],
                }

        with (
            patch.object(
                runtime.RUNTIME_STATE,
                "container",
                SimpleNamespace(jobs=Queue()),
            ),
            patch.object(
                runtime,
                "get_runtime_connection_revision",
                return_value=27,
            ),
        ):
            payload = runtime.get_runtime_worker_fleet_route()

        self.assertEqual(payload["state"], "SYNCING")
        self.assertEqual(captured["desired_revision"], 27)
        self.assertIs(captured["require_connection_revision"], True)

    def test_runtime_ai_provider_setup_endpoint_rejects_non_ascii_aliyun_api_key(
        self,
    ) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/api/runtime/ai-provider-setup",
                json={
                    "default_profile_id": "aliyun",
                    "aliyun": {
                        "api_key": "sk-测试key",
                        "chat_model": "qwen3.7-plus",
                        "embedding_model": "text-embedding-v4",
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "qdrant_url": "http://localhost:6333",
                        "qdrant_collection": "reqpilot-knowledge-aliyun-local",
                    },
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn(
            "DashScope API Key contains non-ASCII characters", response.json()["detail"]
        )

    def test_runtime_ai_provider_setup_rejects_untrusted_aliyun_url_before_save(
        self,
    ) -> None:
        with patch(
            "app.application.runtime.apply_runtime_ai_provider_setup"
        ) as save_setup:
            with TestClient(main.app) as client:
                response = client.post(
                    "/api/runtime/ai-provider-setup",
                    json={
                        "default_profile_id": "aliyun",
                        "aliyun": {
                            "api_key": "synthetic-key",
                            "base_url": "https://evil.example/compatible-mode/v1",
                        },
                    },
                )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "Aliyun Base URL must be an approved DashScope HTTPS compatible-mode endpoint.",
        )
        save_setup.assert_not_called()

    def test_runtime_setup_readiness_uses_current_revision_capabilities(self) -> None:
        from app.application import runtime

        setup_payload = {
            "default_profile_id": "aliyun",
            "connection_revision": 9,
            "profiles": [
                {
                    "id": "aliyun",
                    "ready": True,
                    "llm_ready": True,
                    "retrieval_ready": True,
                }
            ],
            "aliyun": {"ready": True},
            "azure": {"ready": False},
        }
        configs = [SimpleNamespace(profile_id="aliyun")]
        capabilities = [
            {
                "capability_id": capability_id,
                "ready": True,
                "connection_revision": 8,
            }
            for capability_id in ("aliyun.llm", "aliyun.embedding", "qdrant")
        ]
        worker_fleet = {
            "schema_version": "runtime-worker-fleet-status/v1",
            "state": "SYNCING",
            "desired_connection_revision": 9,
            "required_kind": "document.extract",
            "revision_required": True,
            "registered_worker_count": 2,
            "live_worker_count": 2,
            "eligible_worker_count": 0,
            "idle_worker_count": 2,
            "active_worker_count": 0,
            "draining_worker_count": 0,
            "stale_revision_worker_count": 2,
            "workers": [],
        }
        with patch.object(
            runtime, "build_runtime_ai_provider_setup", return_value=setup_payload
        ), patch.object(
            runtime, "list_ai_copilot_profiles", return_value=configs
        ), patch.object(
            runtime, "build_ai_profile_check", return_value={"runnable": True}
        ), patch.object(
            runtime,
            "get_runtime_capabilities_route",
            return_value={"capabilities": capabilities},
        ), patch.object(
            runtime,
            "get_runtime_worker_fleet_route",
            return_value=worker_fleet,
            create=True,
        ):
            payload = runtime._build_runtime_ai_provider_setup_response()

        aliyun_profile = payload["profiles"][0]
        self.assertFalse(payload["aliyun"]["ready"])
        self.assertFalse(payload["aliyun"]["llm_ready"])
        self.assertFalse(payload["aliyun"]["retrieval_ready"])
        self.assertFalse(aliyun_profile["ready"])
        self.assertFalse(aliyun_profile["llm_ready"])
        self.assertFalse(aliyun_profile["retrieval_ready"])
        self.assertEqual(payload["worker_fleet"], worker_fleet)

    def test_runtime_setup_restores_the_current_revision_aliyun_probe(self) -> None:
        from app.application import runtime

        receipt = {
            "schema_version": "runtime-aliyun-connection-probe/v2",
            "provider": "aliyun",
            "connection_revision": 9,
            "checked_at": "2026-08-18T00:00:00+00:00",
            "ready": True,
            "checks": {
                "assessment": {"state": "READY", "ready": True},
                "screening": {"state": "READY", "ready": True},
                "ocr": {"state": "READY", "ready": True},
                "embedding": {"state": "READY", "ready": True},
                "qdrant": {"state": "READY", "ready": True},
            },
        }
        setup_payload = {
            "default_profile_id": "aliyun",
            "connection_revision": 9,
            "profiles": [{"id": "aliyun"}],
            "aliyun": {},
            "azure": {},
        }
        configs = [SimpleNamespace(profile_id="aliyun")]
        capabilities = [
            {
                "capability_id": capability_id,
                "ready": True,
                "connection_revision": 9,
            }
            for capability_id in ("aliyun.llm", "aliyun.embedding", "qdrant")
        ]
        with patch.object(
            runtime, "build_runtime_ai_provider_setup", return_value=setup_payload
        ), patch.object(
            runtime, "list_ai_copilot_profiles", return_value=configs
        ), patch.object(
            runtime, "build_ai_profile_check", return_value={"runnable": True}
        ), patch.object(
            runtime,
            "get_runtime_capabilities_route",
            return_value={"capabilities": capabilities},
        ), patch.object(
            runtime,
            "get_runtime_aliyun_connection_probe_receipt",
            return_value=receipt,
            create=True,
        ):
            payload = runtime._build_runtime_ai_provider_setup_response()

        self.assertEqual(payload["aliyun_probe"], receipt)

    def test_runtime_aliyun_probe_endpoint_returns_revision_bound_sanitized_checks(
        self,
    ) -> None:
        safe_payload = {
            "schema_version": "runtime-aliyun-connection-probe/v2",
            "provider": "aliyun",
            "connection_revision": 31,
            "checked_at": "2026-08-12T00:00:00+00:00",
            "ready": True,
            "checks": {
                "screening": {
                    "state": "READY",
                    "ready": True,
                    "reason_code": "",
                    "message": "",
                    "model": "qwen3.7-flash",
                    "role": "screening",
                },
                "assessment": {
                    "state": "READY",
                    "ready": True,
                    "reason_code": "",
                    "message": "",
                    "model": "qwen3.8-max",
                    "role": "assessment",
                },
                "ocr": {
                    "state": "READY",
                    "ready": True,
                    "reason_code": "",
                    "message": "",
                    "model": "qwen3.5-ocr",
                    "role": "ocr",
                    "request_count": 1,
                },
                "embedding": {
                    "state": "READY",
                    "ready": True,
                    "reason_code": "",
                    "message": "",
                    "model": "text-embedding-v4",
                    "role": "embedding",
                    "dimensions": 1024,
                },
                "qdrant": {
                    "state": "READY",
                    "ready": True,
                    "reason_code": "",
                    "message": "",
                    "role": "vector_store",
                },
            },
            "capabilities": [],
        }
        with patch(
            "app.application.runtime.probe_runtime_aliyun_connection",
            return_value=safe_payload,
        ):
            with TestClient(main.app) as client:
                response = client.post("/api/runtime/ai-provider-setup/aliyun-probe")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["connection_revision"], 31)
        self.assertEqual(
            set(payload["checks"]),
            {"screening", "assessment", "ocr", "embedding", "qdrant"},
        )
        serialized = json.dumps(payload)
        self.assertNotIn("api_key", serialized.lower())
        self.assertNotIn("dashscope.aliyuncs.com", serialized)
        self.assertNotIn("customer", serialized.lower())

    def test_upload_knowledge_document_is_saved_before_project_backend_is_ready(
        self,
    ) -> None:
        project, _document = self._create_project_with_document(
            "-knowledge-profile-gate"
        )
        upload = UploadFile(
            filename="spec.txt", file=io.BytesIO("Spec content".encode("utf-8"))
        )
        knowledge_dir = self.tmp_path / "knowledge_deferred_sync"
        knowledge_dir.mkdir(parents=True, exist_ok=True)

        with patch.dict(
            os.environ,
            {
                "REQPILOT_ALIYUN_API_KEY": "",
                "REQPILOT_DASHSCOPE_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "REQPILOT_ALIYUN_QDRANT_API_KEY": "",
                "REQPILOT_QDRANT_API_KEY": "",
                "QDRANT_API_KEY": "",
            },
            clear=False,
        ):
            with TestClient(main.app) as client:
                setup_resp = client.post(
                    "/api/runtime/ai-provider-setup",
                    json={
                        "default_profile_id": "aliyun",
                        "aliyun": {
                            "chat_model": "qwen3.7-plus",
                            "embedding_model": "text-embedding-v4",
                            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "qdrant_url": "http://localhost:6333",
                            "qdrant_collection": "reqpilot-knowledge-aliyun-local",
                        },
                    },
                )
                self.assertEqual(setup_resp.status_code, 200)

            with patch(
                "app.application.ingestion._upload_staging_dir",
                return_value=knowledge_dir,
            ):
                created = asyncio.run(
                    main.upload_knowledge_document(
                        project["id"],
                        file=upload,
                        doc_type="REFERENCE",
                        doc_version="",
                        status="ACTIVE",
                        effective_from="",
                        effective_to="",
                    )
                )

        self.assertTrue(created["retrieval_sync_deferred"])
        self.assertEqual(created["retrieval_sync_status"], "PENDING")
        self.assertEqual(len(db.list_knowledge_documents(project["id"])), 1)
        self.assertTrue(any(knowledge_dir.iterdir()))

    def test_build_ai_profile_catalog_marks_unsynced_profile_as_pending(self) -> None:
        azure_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="azure_sdk",
            top_k=5,
            trace_dir=self.tmp_path / "trace-azure",
            azure_auth_mode="rbac",
            azure_allow_interactive_login=False,
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_api_key="",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="azure",
            provider="azure",
            profile_label="Azure",
            chat_model="gpt-5.4",
            embedding_model="text-embedding-3-large",
        )
        aliyun_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.tmp_path / "trace-aliyun",
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.7-plus",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="http://localhost:6333",
            qdrant_collection_name="reqpilot-knowledge-aliyun-local",
        )

        with patch.object(main, "list_ai_copilot_profiles", return_value=[azure_config, aliyun_config]), patch.object(
            main,
            "load_ai_copilot_config",
            return_value=azure_config,
        ), patch.object(
            main,
            "build_ai_profile_check",
            side_effect=[
                {"status": "ok", "detail": "Azure ready.", "runnable": True, "missing": []},
                {"status": "ok", "detail": "Aliyun ready.", "runnable": True, "missing": []},
            ],
        ):
            payload = main._build_ai_profile_catalog(
                [
                    {
                        "id": "kb-1",
                        "retrieval_syncs": [
                            {"profile_id": "azure", "status": "READY"},
                        ],
                    }
                ]
            )

        by_id = {item["id"]: item for item in payload}
        self.assertEqual(by_id["azure"]["knowledge_sync"]["status"], "READY")
        self.assertEqual(by_id["aliyun"]["knowledge_sync"]["status"], "PENDING")
        self.assertTrue(by_id["aliyun"]["ready"])

    def test_build_ai_profile_catalog_uses_runtime_readiness_for_ready_flag(self) -> None:
        azure_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="azure_sdk",
            top_k=5,
            trace_dir=self.tmp_path / "trace-azure-runtime",
            azure_auth_mode="rbac",
            azure_allow_interactive_login=False,
            azure_search_endpoint="https://search.example.search.windows.net",
            azure_search_api_key="",
            azure_search_index_name="reqpilot-knowledge",
            azure_openai_endpoint="https://openai.example.openai.azure.com",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="gpt-5.4",
            azure_openai_embedding_deployment="text-embedding-3-large",
            azure_openai_embedding_dimensions=1536,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="azure",
            provider="azure",
            profile_label="Azure",
            chat_model="gpt-5.4",
            embedding_model="text-embedding-3-large",
        )
        aliyun_config = AICopilotConfig(
            assessment_mode="llm_shadow",
            retrieval_backend="qdrant",
            top_k=5,
            trace_dir=self.tmp_path / "trace-aliyun-runtime",
            azure_auth_mode="",
            azure_allow_interactive_login=False,
            azure_search_endpoint="",
            azure_search_api_key="",
            azure_search_index_name="",
            azure_openai_endpoint="",
            azure_openai_api_key="",
            azure_openai_api_version="2024-10-21",
            azure_openai_chat_deployment="",
            azure_openai_embedding_deployment="",
            azure_openai_embedding_dimensions=0,
            azure_search_semantic_configuration="",
            llm_timeout_seconds=45.0,
            profile_id="aliyun",
            provider="aliyun",
            profile_label="Aliyun",
            chat_model="qwen3.7-plus",
            embedding_model="text-embedding-v4",
            dashscope_api_key="aliyun-key",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            qdrant_url="http://localhost:6333",
            qdrant_collection_name="reqpilot-knowledge-aliyun-local",
        )

        with patch.object(main, "list_ai_copilot_profiles", return_value=[azure_config, aliyun_config]), patch.object(
            main,
            "load_ai_copilot_config",
            return_value=aliyun_config,
        ), patch.object(
            main,
            "build_ai_profile_check",
            side_effect=[
                {"status": "ok", "detail": "Azure ready.", "runnable": True, "missing": []},
                {
                    "status": "error",
                    "detail": "Qdrant local service is unreachable.",
                    "runnable": False,
                    "missing": ["ALIYUN_QDRANT_UNREACHABLE"],
                },
            ],
        ):
            payload = main._build_ai_profile_catalog(
                [
                    {
                        "id": "kb-1",
                        "retrieval_syncs": [],
                    }
                ]
            )

        by_id = {item["id"]: item for item in payload}
        self.assertFalse(by_id["aliyun"]["ready"])
        self.assertEqual(by_id["aliyun"]["runtime_status"], "error")
        self.assertEqual(by_id["aliyun"]["runtime_missing"], ["ALIYUN_QDRANT_UNREACHABLE"])
        self.assertEqual(by_id["aliyun"]["missing"], ["ALIYUN_QDRANT_UNREACHABLE"])

    def test_resync_knowledge_document_route_uses_default_profile(self) -> None:
        project, _document = self._create_project_with_document("-knowledge-resync")
        knowledge_path = self.tmp_path / "knowledge-resync.txt"
        knowledge_path.write_text("knowledge", encoding="utf-8")
        knowledge_document = db.add_knowledge_document(
            project["id"],
            "knowledge-resync.txt",
            "txt",
            str(knowledge_path),
            "knowledge",
        )

        captured: dict[str, object] = {}

        def fake_sync(project_id: str, document: dict, *, profile_ids: list[str] | None = None) -> dict:
            captured["project_id"] = project_id
            captured["document_id"] = document["id"]
            captured["profile_ids"] = list(profile_ids or [])
            return {
                **document,
                "retrieval_syncs": [
                    {
                        "profile_id": "aliyun",
                        "status": "READY",
                    }
                ],
            }

        with patch.object(main, "require_knowledge_upload_capability"), patch.object(
            main,
            "_project_knowledge_sync_profile_ids",
            return_value=["aliyun"],
        ), patch.object(
            main,
            "_sync_knowledge_document_targets",
            side_effect=fake_sync,
        ):
            with TestClient(main.app) as client:
                response = client.post(
                    f"/api/projects/{project['id']}/knowledge-documents/{knowledge_document['id']}/resync"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["project_id"], project["id"])
        self.assertEqual(captured["document_id"], knowledge_document["id"])
        self.assertEqual(captured["profile_ids"], ["aliyun"])
        self.assertEqual(response.json()["retrieval_syncs"][0]["status"], "READY")

    def test_upload_knowledge_document_route_accepts_multipart_payload(self) -> None:
        project, _document = self._create_project_with_document("-knowledge-route")
        knowledge_dir = self.tmp_path / "knowledge_route"
        knowledge_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(main, "KNOWLEDGE_DIR", knowledge_dir), patch.object(
            main,
            "require_knowledge_upload_capability",
        ), patch.object(
            main,
            "_project_knowledge_sync_profile_ids",
            return_value=["aliyun"],
        ), patch.object(
            main,
            "_sync_knowledge_document_targets",
            side_effect=lambda _project_id, document, **_kwargs: document,
        ):
            with TestClient(main.app) as client:
                response = client.post(
                    f"/api/projects/{project['id']}/knowledge-documents",
                    files={"file": ("spec.txt", io.BytesIO(b"Spec content"), "text/plain")},
                    data={
                        "doc_type": "STANDARD",
                        "doc_version": "V1.2",
                        "status": "ACTIVE",
                        "effective_from": "2026-01-01",
                        "effective_to": "2026-12-31",
                    },
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["doc_type"], "STANDARD")
        self.assertEqual(payload["doc_version"], "V1.2")
        self.assertEqual(payload["status"], "ACTIVE")

    def test_upload_knowledge_document_route_rejects_duplicate_file_name(self) -> None:
        project, _document = self._create_project_with_document("-knowledge-duplicate")
        knowledge_dir = self.tmp_path / "knowledge_duplicate"
        knowledge_dir.mkdir(parents=True, exist_ok=True)

        with patch.object(main, "KNOWLEDGE_DIR", knowledge_dir), patch.object(
            main,
            "require_knowledge_upload_capability",
        ), patch.object(
            main,
            "_project_knowledge_sync_profile_ids",
            return_value=["aliyun"],
        ), patch.object(
            main,
            "_sync_knowledge_document_targets",
            side_effect=lambda _project_id, document, **_kwargs: document,
        ):
            with TestClient(main.app) as client:
                first = client.post(
                    f"/api/projects/{project['id']}/knowledge-documents",
                    files={"file": ("spec.txt", io.BytesIO(b"Spec content"), "text/plain")},
                )
                duplicate = client.post(
                    f"/api/projects/{project['id']}/knowledge-documents",
                    files={"file": ("spec.txt", io.BytesIO(b"Updated spec content"), "text/plain")},
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("already exists", duplicate.json()["detail"])
        self.assertEqual(len(db.list_knowledge_documents(project["id"])), 1)

    def test_delete_knowledge_document_route_removes_files_and_bumps_version(self) -> None:
        project, _document = self._create_project_with_document("-knowledge-delete")
        knowledge_path = self.tmp_path / "knowledge-delete.txt"
        knowledge_path.write_text("knowledge", encoding="utf-8")
        payload_dir = self.tmp_path / "knowledge_payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = payload_dir / "payload-delete.json"
        payload_path.write_text('{"chunks": []}', encoding="utf-8")
        knowledge_document = db.add_knowledge_document(
            project_id=project["id"],
            file_name="knowledge-delete.txt",
            file_type="txt",
            file_path=str(knowledge_path),
            extracted_text="knowledge",
        )
        db.upsert_knowledge_document_sync(
            knowledge_document["id"],
            profile_id="azure",
            provider="azure",
            retrieval_backend="azure_sdk",
            status="READY",
            synced_at="2026-04-03T06:00:00+00:00",
            chunk_count=4,
            payload_path=str(payload_path),
        )

        with patch.object(main, "INDEX_PAYLOAD_DIR", payload_dir):
            with TestClient(main.app) as client:
                response = client.delete(
                    f"/api/projects/{project['id']}/knowledge-documents/{knowledge_document['id']}"
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["removed_count"], 1)
        self.assertEqual(payload["knowledge_base_version"], 1)
        self.assertFalse(knowledge_path.exists())
        self.assertFalse(payload_path.exists())
        self.assertEqual(db.list_knowledge_documents(project["id"]), [])
        self.assertEqual(db.list_knowledge_document_syncs(knowledge_document["id"]), [])

    def test_delete_knowledge_document_route_removes_legacy_same_name_duplicates(self) -> None:
        project, _document = self._create_project_with_document("-knowledge-delete-duplicates")
        first_path = self.tmp_path / "legacy-dup-a.txt"
        second_path = self.tmp_path / "legacy-dup-b.txt"
        first_path.write_text("alpha", encoding="utf-8")
        second_path.write_text("beta", encoding="utf-8")
        db.add_knowledge_document(
            project_id=project["id"],
            file_name="platform-spec.txt",
            file_type="txt",
            file_path=str(first_path),
            extracted_text="alpha",
        )
        latest_document = db.add_knowledge_document(
            project_id=project["id"],
            file_name="platform-spec.txt",
            file_type="txt",
            file_path=str(second_path),
            extracted_text="beta",
        )

        with TestClient(main.app) as client:
            response = client.delete(
                f"/api/projects/{project['id']}/knowledge-documents/{latest_document['id']}"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["removed_count"], 2)
        self.assertFalse(first_path.exists())
        self.assertFalse(second_path.exists())
        self.assertEqual(db.list_knowledge_documents(project["id"]), [])

    def test_list_knowledge_documents_collapses_existing_duplicate_file_names(self) -> None:
        project, _document = self._create_project_with_document("-knowledge-list-dedupe")
        first_path = self.tmp_path / "dup-a.txt"
        second_path = self.tmp_path / "dup-b.txt"
        first_path.write_text("alpha", encoding="utf-8")
        second_path.write_text("beta", encoding="utf-8")
        db.add_knowledge_document(
            project_id=project["id"],
            file_name="platform-spec.txt",
            file_type="txt",
            file_path=str(first_path),
            extracted_text="alpha",
        )
        db.add_knowledge_document(
            project_id=project["id"],
            file_name="platform-spec.txt",
            file_type="txt",
            file_path=str(second_path),
            extracted_text="beta",
        )

        listed = main.list_knowledge_documents(project["id"])

        self.assertEqual(len(db.list_knowledge_documents(project["id"])), 2)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["file_name"], "platform-spec.txt")
