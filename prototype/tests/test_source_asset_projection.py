from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.application.extraction.requirement_tree import (
    _source_bound_structure_content_block,
)
from app.application.extraction.serialize_structure_workbench import (
    _persisted_structure_presentation,
)
from app.application.extraction.structure_presentation import (
    STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
    build_structure_bilingual_projection_sidecar,
)


class SourceAssetProjectionTests(unittest.TestCase):
    def test_durable_figure_ref_keeps_object_identity_and_exact_pdf_locator(
        self,
    ) -> None:
        source_sha256 = "a" * 64
        content_sha256 = "b" * 64
        source_ref = {
            "kind": "source_node",
            "source_id": "source-figure-1",
            "source_kind": "figure",
            "page_number": 9,
            "asset_id": f"asset-{'c' * 32}",
            "asset_kind": "figure",
            "content_sha256": content_sha256,
            "media_type": "image/jpeg",
            "storage_key": (f"source-assets/{content_sha256[:2]}/{content_sha256}.jpg"),
            "location": {
                "format": "pdf",
                "page_number": 9,
                "block_index": 4,
                "bbox": [72.0, 120.0, 420.0, 318.0],
            },
        }
        payload = {
            "source_sha256": source_sha256,
            "structure_presentation_index": {
                "schema_version": STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
                "source_ir_id": "source-ir-1",
                "source_sha256": source_sha256,
                "structure_snapshot_id": "effective-1",
                "page_count": 9,
                "bilingual_projection": (
                    build_structure_bilingual_projection_sidecar(None)
                ),
                "nodes": {
                    "structure-figure-1": {
                        "label": "图片",
                        "owned_text": "",
                        "source_refs": [source_ref],
                        "toc_source_text_by_id": {},
                        "physical_source_order": 12,
                        "physical_source_offset": 0,
                        "page_start": 9,
                        "page_end": 9,
                    }
                },
            },
        }

        presentations, source_index, _ = _persisted_structure_presentation(
            payload,
            structure_agent={
                "effective_structure_identity": {"snapshot_id": "effective-1"}
            },
        )
        restored_ref = presentations["structure-figure-1"]["source_refs"][0]
        self.assertEqual(restored_ref, source_ref)
        self.assertEqual(
            source_index["source-figure-1"]["storage_key"],
            source_ref["storage_key"],
        )

        block = _source_bound_structure_content_block(
            {
                "id": "structure-figure-1",
                "kind": "CONTENT",
                "label": "图片",
                "source_refs": [restored_ref],
                "page_start": 9,
                "page_end": 9,
                "physical_source_order": 12,
                "physical_source_offset": 0,
            }
        )
        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block["kind"], "figure")
        self.assertEqual(block["source_refs"], [source_ref])


if __name__ == "__main__":
    unittest.main()
