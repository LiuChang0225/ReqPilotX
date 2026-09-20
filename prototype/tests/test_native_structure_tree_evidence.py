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
import tempfile
import unittest
from pathlib import Path

import fitz

from scripts.native_structure_tree_evidence import build_evidence_binding


class NativeStructureTreeEvidenceTests(unittest.TestCase):
    def test_native_lines_refine_shared_source_block_and_table_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pages").mkdir()
            (root / "tables").mkdir()
            (root / "source").mkdir()
            document = fitz.open()
            page = document.new_page(width=300, height=400)
            page.insert_text((20, 40), "5.1 First section")
            page.insert_text((20, 70), "5.2 Second section")
            document.save(root / "source" / "document.pdf")
            document.close()

            source_text = "5.1 First section\n5.2 Second section"
            (root / "pages" / "P0001.md").write_text(
                "\n".join(
                    [
                        "# Page 1",
                        "",
                        "- Bounds: `[0, 0, 300, 400]`",
                        "",
                        "## Ordered non-table text",
                        "",
                        "### `source-shared` · `paragraph` · `[10, 10, 220, 90]`",
                        "",
                        "```text",
                        source_text,
                        "```",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "pages" / "source-map.jsonl").write_text(
                json.dumps(
                    {
                        "source_handle": "source-shared",
                        "page_number": 1,
                        "bbox": [10, 10, 220, 90],
                        "raw_text_sha256": hashlib.sha256(
                            source_text.encode("utf-8")
                        ).hexdigest(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            table = {
                "table_token": "T0001",
                "page_number": 1,
                "bbox": [20, 110, 280, 180],
                "row_count": 2,
                "column_count": 1,
                "cells": [
                    {
                        "cell_id": "cell-1",
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "raw_text": "Header",
                        "native_locator": {"bbox": [20, 110, 100, 130]},
                    },
                    {
                        "cell_id": "cell-2",
                        "row": 1,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "raw_text": "Value",
                        "native_locator": {"bbox": [20, 130, 280, 180]},
                    },
                ],
            }
            (root / "tables" / "T0001.json").write_text(
                json.dumps(table),
                encoding="utf-8",
            )
            tree = {
                "schema_version": "native-document-structure-tree/v1",
                "root_ids": ["section-5.1", "section-5.2", "derived-table"],
                "nodes": [
                    {
                        "node_id": "section-5.1",
                        "parent_id": None,
                        "order": 0,
                        "kind": "SECTION",
                        "label": "5.1",
                        "title": "First section",
                        "title_authority": "VISIBLE_TITLE",
                        "pages": [1],
                        "content_blocks": [],
                        "finding_codes": [],
                    },
                    {
                        "node_id": "section-5.2",
                        "parent_id": None,
                        "order": 1,
                        "kind": "SECTION",
                        "label": "5.2",
                        "title": "Second section",
                        "title_authority": "VISIBLE_TITLE",
                        "pages": [1],
                        "content_blocks": [],
                        "finding_codes": [],
                    },
                    {
                        "node_id": "derived-table",
                        "parent_id": None,
                        "order": 2,
                        "kind": "DOCUMENT_UNIT",
                        "label": "",
                        "title": "Derived table",
                        "title_authority": "DERIVED_FROM_VISIBLE_TABLE_ROLE",
                        "pages": [1],
                        "content_blocks": [
                            {
                                "kind": "LOGICAL_TABLE",
                                "logical_table_scope_id": "table-scope",
                            }
                        ],
                        "finding_codes": [],
                    },
                ],
            }
            reference = {
                "source": {"sha256": "a" * 64, "page_count": 1},
                "logical_tables": [
                    {
                        "scope_id": "table-scope",
                        "fragments": [
                            {
                                "table_token": "T0001",
                                "page": 1,
                                "row_range": [0, 2],
                            }
                        ],
                    }
                ],
            }

            result = build_evidence_binding(
                tree=tree,
                reference=reference,
                workspace_root=root,
                source_sha256="a" * 64,
            )

            self.assertEqual(result["summary"]["anchored_node_count"], 3)
            self.assertEqual(result["summary"]["exact_native_span_count"], 2)
            self.assertEqual(result["summary"]["shared_block_approximate_count"], 0)
            self.assertEqual(result["summary"]["exact_table_region_count"], 1)
            self.assertEqual(result["summary"]["table_preview_fragment_count"], 1)
            self.assertEqual(result["summary"]["table_preview_cell_count"], 2)
            self.assertEqual(result["summary"]["content_node_count"], 2)
            bindings = {item["node_id"]: item for item in result["nodes"]}
            first = bindings["section-5.1"]["anchors"][0]
            second = bindings["section-5.2"]["anchors"][0]
            self.assertEqual(first["precision"], "EXACT_NATIVE_SPAN")
            self.assertEqual(second["precision"], "EXACT_NATIVE_SPAN")
            self.assertNotEqual(first["bbox"], second["bbox"])
            self.assertIn(
                "5.1 First section",
                bindings["section-5.1"]["source_content"][0]["text"],
            )
            derived = bindings["derived-table"]["anchors"]
            self.assertEqual(len(derived), 1)
            self.assertEqual(derived[0]["anchor_kind"], "TABLE_REGION")
            self.assertEqual(derived[0]["bbox"], [20.0, 110.0, 280.0, 180.0])

    def test_cross_page_direct_content_stops_at_next_heading_and_skips_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pages").mkdir()
            (root / "tables").mkdir()
            (root / "source").mkdir()
            document = fitz.open()
            first_page = document.new_page(width=300, height=400)
            first_page.insert_text((20, 40), "5.7 Integration validation")
            first_page.insert_text((20, 70), "Intro paragraph")
            first_page.insert_text((20, 150), "Table cell text")
            first_page.insert_text((20, 250), "After table starts")
            second_page = document.new_page(width=300, height=400)
            second_page.insert_text((20, 40), "continued on next page")
            second_page.insert_text((20, 80), "5.8 Restricted substances")
            second_page.insert_text((20, 110), "Restricted substance body")
            document.save(root / "source" / "document.pdf")
            document.close()

            blocks = {
                1: (
                    "source-57",
                    [10, 20, 290, 280],
                    "5.7 Integration validation\nIntro paragraph\nTable cell text\nAfter table starts",
                ),
                2: (
                    "source-58",
                    [10, 10, 290, 140],
                    "continued on next page\n5.8 Restricted substances\nRestricted substance body",
                ),
            }
            source_map = []
            for page, (handle, bbox, text) in blocks.items():
                (root / "pages" / f"P{page:04d}.md").write_text(
                    "\n".join(
                        [
                            f"# Page {page}",
                            "",
                            "- Bounds: `[0, 0, 300, 400]`",
                            "",
                            "## Ordered non-table text",
                            "",
                            f"### `{handle}` · `paragraph` · `[{', '.join(str(value) for value in bbox)}]`",
                            "",
                            "```text",
                            text,
                            "```",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                source_map.append(
                    {
                        "source_handle": handle,
                        "page_number": page,
                        "bbox": bbox,
                        "source_order": page,
                        "raw_text_sha256": hashlib.sha256(
                            text.encode("utf-8")
                        ).hexdigest(),
                    }
                )
            (root / "pages" / "source-map.jsonl").write_text(
                "\n".join(json.dumps(item) for item in source_map) + "\n",
                encoding="utf-8",
            )
            table = {
                "table_token": "T0001",
                "page_number": 1,
                "bbox": [20, 120, 280, 180],
                "row_count": 1,
                "column_count": 1,
                "cells": [
                    {
                        "cell_id": "cell-1",
                        "row": 0,
                        "column": 0,
                        "row_span": 1,
                        "column_span": 1,
                        "raw_text": "Table cell text",
                        "native_locator": {"bbox": [20, 120, 280, 180]},
                    }
                ],
            }
            (root / "tables" / "T0001.json").write_text(
                json.dumps(table),
                encoding="utf-8",
            )
            tree = {
                "schema_version": "native-document-structure-tree/v1",
                "root_ids": ["section-5.7", "section-5.8"],
                "nodes": [
                    {
                        "node_id": "section-5.7",
                        "parent_id": None,
                        "order": 0,
                        "kind": "SECTION",
                        "label": "5.7",
                        "title": "Integration validation",
                        "title_authority": "VISIBLE_TITLE",
                        "pages": [1, 2],
                        "content_blocks": [
                            {
                                "kind": "LOGICAL_TABLE",
                                "logical_table_scope_id": "table-scope",
                            }
                        ],
                        "finding_codes": [],
                    },
                    {
                        "node_id": "section-5.8",
                        "parent_id": None,
                        "order": 1,
                        "kind": "SECTION",
                        "label": "5.8",
                        "title": "Restricted substances",
                        "title_authority": "VISIBLE_TITLE",
                        "pages": [2],
                        "content_blocks": [],
                        "finding_codes": [],
                    },
                ],
            }
            reference = {
                "source": {"sha256": "b" * 64, "page_count": 2},
                "logical_tables": [
                    {
                        "scope_id": "table-scope",
                        "fragments": [
                            {
                                "table_token": "T0001",
                                "page": 1,
                                "row_range": [0, 1],
                            }
                        ],
                    }
                ],
            }

            result = build_evidence_binding(
                tree=tree,
                reference=reference,
                workspace_root=root,
                source_sha256="b" * 64,
            )

            bindings = {item["node_id"]: item for item in result["nodes"]}
            section_57 = "\n".join(
                item["text"] for item in bindings["section-5.7"]["source_content"]
            )
            section_58 = "\n".join(
                item["text"] for item in bindings["section-5.8"]["source_content"]
            )
            self.assertIn("After table starts", section_57)
            self.assertIn("continued on next page", section_57)
            self.assertNotIn("Table cell text", section_57)
            self.assertNotIn("5.8 Restricted substances", section_57)
            self.assertNotIn("continued on next page", section_58)
            self.assertIn("Restricted substance body", section_58)
            self.assertEqual(
                result["table_previews"][0]["cells"][0]["text"], "Table cell text"
            )


if __name__ == "__main__":
    unittest.main()
