from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import db
from app.adapters.storage.local_object_store import LocalObjectStore
from app.adapters.documents.pdf_source_ir_adapter import (
    PageStagingAuthorityLostError,
)
from app.application.extraction.source_page_facts import (
    DOCUMENT_EXTRACTION_PAGE_STAGING_SCHEMA_VERSION,
    SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION,
    build_document_extraction_staged_page,
    get_document_extraction_staged_source_page,
    get_document_source_page,
    persist_document_extraction_staged_page,
    persist_source_page_preview,
    public_document_extraction_page_staging,
    public_source_page_snapshot,
)
from app.application.extraction.geometry_evidence import (
    PDF_PAGE_GEOMETRY_PREVIEW_KEY,
)
from app.application.runtime import RUNTIME_STATE
from app.domain.extraction.page_geometry import (
    PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
    PDF_VISIBLE_COORDINATE_SPACE,
    PdfPageGeometryLineV1,
    PdfPageGeometrySourceIdentityV1,
    create_pdf_page_geometry_index,
)


class SourcePageFactSnapshotTests(unittest.TestCase):
    def test_staged_page_is_explicitly_nonformal_source_only(self) -> None:
        payload = build_document_extraction_staged_page(
            _staging_candidate(),
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            job_id="job-1",
            attempt_generation=7,
            processing_plan_sha256="b" * 64,
            result_runtime_sha256="c" * 64,
            completed_at_utc="2026-08-18T14:00:00+00:00",
        )

        self.assertEqual(
            payload["schema_version"],
            DOCUMENT_EXTRACTION_PAGE_STAGING_SCHEMA_VERSION,
        )
        self.assertFalse(payload["formal"])
        self.assertFalse(payload["editable"])
        self.assertEqual(payload["serving_impact"], "NONE")
        self.assertEqual(payload["structure_state"], "NOT_BUILT")
        page = payload["page_comparison"]["pages"][0]
        self.assertEqual(page["blocks"][0]["provenance"]["channel"], "NATIVE")
        self.assertEqual(page["physical_tables"][0]["channel"], "NATIVE")

    def test_staged_page_keeps_header_asset_and_separates_page_render_support(
        self,
    ) -> None:
        candidate = _staging_candidate()
        candidate["nodes"] = [
            {
                "source_node_id": "node-header-" + "1" * 24,
                "kind": "image",
                "source_order": 0,
                "raw_text": "",
                "native_locator": {
                    "format": "pdf",
                    "page_number": 1,
                    "block_index": 0,
                    "bbox": [48, 16, 128, 56],
                },
                "visual_locator": {
                    "format": "pdf",
                    "page_number": 1,
                    "block_index": 0,
                    "bbox": [48, 16, 128, 56],
                },
                "asset_ref": "asset-header",
                "extraction_diagnostics": [
                    {
                        "code": "pdf_repeated_header_image_furniture",
                        "severity": "info",
                        "message": "Repeated header image.",
                        "retryable": False,
                    }
                ],
            },
            {
                "source_node_id": "node-raster-" + "2" * 24,
                "kind": "image",
                "source_order": 1,
                "raw_text": "",
                "native_locator": {
                    "format": "pdf",
                    "page_number": 1,
                    "block_index": 1,
                    "bbox": [0, 0, 595, 842],
                },
                "visual_locator": {
                    "format": "pdf",
                    "page_number": 1,
                    "block_index": 1,
                    "bbox": [0, 0, 595, 842],
                },
                "asset_ref": "asset-page-render",
                "extraction_diagnostics": [],
            },
        ]
        candidate["assets"] = [
            {
                "asset_id": "asset-header",
                "kind": "image",
                "content_sha256": "d" * 64,
                "media_type": "image/png",
                "storage_key": "source-assets/dd/header.png",
            },
            {
                "asset_id": "asset-page-render",
                "kind": "page_render",
                "content_sha256": "e" * 64,
                "media_type": "image/png",
                "storage_key": "source-assets/ee/page.png",
            },
        ]

        payload = build_document_extraction_staged_page(
            candidate,
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            job_id="job-1",
            attempt_generation=7,
            processing_plan_sha256="b" * 64,
            result_runtime_sha256="c" * 64,
        )

        blocks = {
            block["id"]: block
            for block in payload["page_comparison"]["pages"][0]["blocks"]
        }
        header = blocks["node-header-" + "1" * 24]
        self.assertEqual(header["type"], "HEADER")
        self.assertEqual(header["source_kind"], "image")
        self.assertEqual(header["asset"]["id"], "asset-header")
        raster = blocks["node-raster-" + "2" * 24]
        self.assertEqual(raster["type"], "PAGE_RASTER")
        self.assertEqual(raster["role"], "SCAN_LAYER")
        self.assertFalse(raster["default_visible"])
        self.assertFalse(raster["overlay_default"])
        self.assertEqual(raster["asset"]["kind"], "page_render")

    def test_staged_page_separates_proven_physical_scan_layers_from_content(
        self,
    ) -> None:
        candidate = _staging_candidate()
        candidate["nodes"] = [
            {
                "source_node_id": f"node-raster-{index}-" + str(index) * 24,
                "kind": "image",
                "source_order": index,
                "raw_text": "",
                "native_locator": {
                    "format": "pdf",
                    "page_number": 1,
                    "block_index": index,
                    "bbox": bbox,
                },
                "visual_locator": {
                    "format": "pdf",
                    "page_number": 1,
                    "block_index": index,
                    "bbox": bbox,
                },
                "asset_ref": f"asset-raster-{index}",
                "extraction_diagnostics": [],
            }
            for index, bbox in enumerate(
                ([0, 0, 595, 842], [-2, -1, 597, 843]),
                start=1,
            )
        ]
        candidate["assets"] = [
            {
                "asset_id": f"asset-raster-{index}",
                "kind": "image",
                "content_sha256": str(index) * 64,
                "media_type": "image/png",
                "storage_key": f"source-assets/{index}/page.png",
            }
            for index in (1, 2)
        ]
        candidate["diagnostics"] = [
            {
                "code": "pdf_full_page_image_with_invisible_text_layer",
                "severity": "warning",
                "message": "Page raster with hidden text.",
                "retryable": False,
                "native_locator": {
                    "format": "pdf",
                    "page_number": 1,
                    "bbox": [0, 0, 595, 842],
                },
            }
        ]

        payload = build_document_extraction_staged_page(
            candidate,
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            job_id="job-1",
            attempt_generation=7,
            processing_plan_sha256="b" * 64,
            result_runtime_sha256="c" * 64,
        )

        page = payload["page_comparison"]["pages"][0]
        self.assertEqual(
            [block["type"] for block in page["blocks"]],
            ["PAGE_RASTER"],
        )
        self.assertTrue(
            all(block["role"] == "SCAN_LAYER" for block in page["blocks"])
        )
        self.assertTrue(
            all(block["default_visible"] is False for block in page["blocks"])
        )
        self.assertEqual(page["counts"]["content_blocks"], 0)
        self.assertEqual(page["blocks"][0]["physical_layer_count"], 2)
        self.assertEqual(
            len(page["blocks"][0]["physical_layer_source_refs"]),
            2,
        )
        self.assertEqual(page["counts"]["support_blocks"], 1)

    def test_staged_page_does_not_hide_full_page_figure_without_raster_proof(
        self,
    ) -> None:
        candidate = _staging_candidate()
        candidate["nodes"] = [
            {
                "source_node_id": "node-figure-" + "3" * 24,
                "kind": "figure",
                "source_order": 0,
                "raw_text": "",
                "native_locator": {
                    "format": "pdf",
                    "page_number": 1,
                    "block_index": 0,
                    "bbox": [0, 0, 595, 842],
                },
                "asset_ref": "asset-figure",
                "extraction_diagnostics": [],
            }
        ]
        candidate["assets"] = [
            {
                "asset_id": "asset-figure",
                "kind": "figure",
                "content_sha256": "d" * 64,
                "media_type": "image/png",
                "storage_key": "source-assets/dd/figure.png",
            }
        ]

        payload = build_document_extraction_staged_page(
            candidate,
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            job_id="job-1",
            attempt_generation=7,
            processing_plan_sha256="b" * 64,
            result_runtime_sha256="c" * 64,
        )

        page = payload["page_comparison"]["pages"][0]
        self.assertEqual(page["blocks"][0]["type"], "FIGURE")
        self.assertEqual(page["counts"]["content_blocks"], 1)
        self.assertEqual(page["counts"]["support_blocks"], 0)

    def test_staged_page_source_fence_mismatch_is_fail_closed(self) -> None:
        with self.assertRaises(PageStagingAuthorityLostError):
            build_document_extraction_staged_page(
                _staging_candidate(),
                project_id="project-1",
                document_id="document-1",
                source_sha256="f" * 64,
                job_id="job-1",
                attempt_generation=7,
                processing_plan_sha256="b" * 64,
                result_runtime_sha256="c" * 64,
            )

    def test_exact_staged_page_endpoint_verifies_live_attempt_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            object_store = LocalObjectStore(Path(temporary))
            page_payload = build_document_extraction_staged_page(
                _staging_candidate(),
                project_id="project-1",
                document_id="document-1",
                source_sha256="a" * 64,
                job_id="job-1",
                attempt_generation=7,
                processing_plan_sha256="b" * 64,
                result_runtime_sha256="c" * 64,
                completed_at_utc="2026-08-18T14:00:00+00:00",
            )
            encoded = json.dumps(
                page_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            stored = object_store.put(
                key="runtime/extraction-page-staging/job-1/7/1.json",
                source=io.BytesIO(encoded),
                expected_sha256=hashlib.sha256(encoded).hexdigest(),
                media_type="application/json",
            )
            previous_container = RUNTIME_STATE.container
            RUNTIME_STATE.container = SimpleNamespace(object_store=object_store)
            try:
                with (
                    patch(
                        "app.application.extraction.review_support."
                        "_require_project_document",
                        return_value={"source_sha256": "a" * 64},
                    ),
                    patch(
                        "app.db.get_document_extraction_page_reference",
                        return_value={
                            "storage_key": stored.key,
                            "artifact_sha256": stored.sha256,
                            "size_bytes": stored.size,
                        },
                    ) as get_reference,
                ):
                    artifact = get_document_extraction_staged_source_page(
                        "project-1",
                        "document-1",
                        "job-1",
                        7,
                        1,
                    )
            finally:
                RUNTIME_STATE.container = previous_container

        self.assertEqual(json.loads(artifact.content), page_payload)
        self.assertEqual(artifact.headers["ETag"], f'"{stored.sha256}"')
        get_reference.assert_called_once_with(
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            job_id="job-1",
            attempt_generation=7,
            page_number=1,
        )

    def test_staged_page_registers_before_put_and_confirms_exact_fence(self) -> None:
        events: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as temporary:
            object_store = LocalObjectStore(Path(temporary))
            previous_container = RUNTIME_STATE.container
            RUNTIME_STATE.container = SimpleNamespace(object_store=object_store)
            try:
                with (
                    patch(
                        "app.db.stage_document_extraction_page_reference",
                        side_effect=lambda **values: events.append(("stage", values)),
                    ),
                    patch(
                        "app.db.confirm_document_extraction_page_reference",
                        side_effect=lambda **values: (
                            events.append(("confirm", values)) or True
                        ),
                    ),
                ):
                    descriptor = persist_document_extraction_staged_page(
                        _staging_candidate(),
                        project_id="project-1",
                        document_id="document-1",
                        source_sha256="a" * 64,
                        job_id="job-1",
                        attempt_generation=7,
                        worker_id="worker-1",
                        processing_plan_sha256="b" * 64,
                        result_runtime_sha256="c" * 64,
                    )
            finally:
                RUNTIME_STATE.container = previous_container

        self.assertEqual([event[0] for event in events], ["stage", "confirm"])
        self.assertEqual(events[0][1]["attempt_generation"], 7)
        self.assertEqual(events[0][1]["worker_id"], "worker-1")
        self.assertEqual(events[1][1]["storage_key"], descriptor["storage_key"])
        self.assertEqual(events[1][1]["artifact_sha256"], descriptor["sha256"])

    def test_staged_page_translates_generation_fence_to_authority_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            object_store = LocalObjectStore(Path(temporary))
            previous_container = RUNTIME_STATE.container
            RUNTIME_STATE.container = SimpleNamespace(object_store=object_store)
            try:
                with (
                    patch(
                        "app.db.stage_document_extraction_page_reference",
                        side_effect=(
                            db.DocumentExtractionPageStagingFenceError(
                                "generation changed"
                            )
                        ),
                    ),
                    self.assertRaises(PageStagingAuthorityLostError),
                ):
                    persist_document_extraction_staged_page(
                        _staging_candidate(),
                        project_id="project-1",
                        document_id="document-1",
                        source_sha256="a" * 64,
                        job_id="job-1",
                        attempt_generation=7,
                        worker_id="worker-1",
                        processing_plan_sha256="b" * 64,
                        result_runtime_sha256="c" * 64,
                    )
            finally:
                RUNTIME_STATE.container = previous_container

    def test_public_staging_summary_compacts_ready_page_ranges(self) -> None:
        with patch(
            "app.db.get_document_extraction_page_staging_summary",
            return_value={
                "attempt_generation": 7,
                "processing_plan_sha256": "b" * 64,
                "result_runtime_sha256": "c" * 64,
                "total_pages": 9,
                "ready_page_count": 5,
                "page_numbers": [1, 2, 3, 6, 8],
            },
        ):
            summary = public_document_extraction_page_staging(
                project_id="project-1",
                document_id="document-1",
                source_sha256="a" * 64,
                job_id="job-1",
            )

        self.assertEqual(summary["state"], "STAGED_READ_ONLY")
        self.assertEqual(
            summary["ready_page_ranges"],
            [
                {"start_page": 1, "end_page": 3},
                {"start_page": 6, "end_page": 6},
                {"start_page": 8, "end_page": 8},
            ],
        )
        self.assertIn("/attempts/job-1/generations/7/", summary["page_url_template"])

    def test_public_staging_status_failure_is_reported_as_degraded(self) -> None:
        with patch(
            "app.db.get_document_extraction_page_staging_summary",
            side_effect=OSError("database unavailable"),
        ):
            summary = public_document_extraction_page_staging(
                project_id="project-1",
                document_id="document-1",
                source_sha256="a" * 64,
                job_id="job-1",
            )

        self.assertEqual(summary["state"], "DEGRADED")
        self.assertEqual(summary["error_code"], "page_staging_status_unavailable")

    def test_preview_is_split_into_immutable_page_descriptors(self) -> None:
        preview = _preview()
        written: dict[str, dict] = {}

        def put(*, storage_key: str, payload: dict) -> dict:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            written[storage_key] = payload
            return {
                "storage_key": storage_key,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "size": len(encoded),
            }

        with patch("app.application.ingestion._put_runtime_json", side_effect=put):
            snapshot = persist_source_page_preview(
                preview,
                project_id="project-1",
                document_id="document-1",
            )

        assert snapshot is not None
        self.assertEqual(
            snapshot["schema_version"],
            SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION,
        )
        self.assertEqual(snapshot["state"], "READY")
        self.assertEqual(snapshot["page_count"], 1)
        self.assertIn("1", snapshot["pages"])
        page_key = snapshot["pages"]["1"]["storage_key"]
        self.assertEqual(written[page_key]["page_number"], 1)
        self.assertEqual(
            written[page_key]["page_comparison"]["pages"][0]["blocks"][0]["id"],
            "source-node-" + "1" * 32,
        )

    def test_formal_page_endpoint_verifies_and_returns_snapshot_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            object_store = LocalObjectStore(Path(temporary))
            page_payload = {
                "schema_version": "extraction-page/v4",
                "page_number": 1,
                "page_comparison": {
                    "pages": [
                        {
                            "page_number": 1,
                            "bounds": [0, 0, 595, 842],
                            "blocks": [
                                {
                                    "id": "source-node-" + "1" * 32,
                                    "type": "TEXT",
                                    "text": "Supplier shall provide a pump.",
                                    "bbox": [72, 60, 260, 78],
                                    "locatable": True,
                                }
                            ],
                        }
                    ]
                },
                "tables": [],
            }
            encoded = json.dumps(
                page_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            stored = object_store.put(
                key="runtime/source-page-facts/test/pages/1.json",
                source=io.BytesIO(encoded),
                expected_sha256=hashlib.sha256(encoded).hexdigest(),
                media_type="application/json",
            )
            source_sha256 = "a" * 64
            result = {
                "source_sha256": source_sha256,
                "source_page_snapshot": {
                    "schema_version": SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION,
                    "state": "READY",
                    "snapshot_id": "b" * 64,
                    "project_id": "project-1",
                    "document_id": "document-1",
                    "source_sha256": source_sha256,
                    "source_ir_id": "source-ir-1",
                    "page_schema_version": "extraction-page/v4",
                    "page_count": 1,
                    "pages": {
                        "1": {
                            "storage_key": stored.key,
                            "sha256": stored.sha256,
                            "size": stored.size,
                        }
                    },
                },
            }
            previous_container = RUNTIME_STATE.container
            RUNTIME_STATE.container = SimpleNamespace(object_store=object_store)
            try:
                with (
                    patch(
                        "app.application.extraction.review_support."
                        "_require_project_document",
                        return_value={
                            "id": "document-1",
                            "project_id": "project-1",
                            "source_sha256": source_sha256,
                        },
                    ),
                    patch(
                        "app.application.extraction.structure_workspace."
                        "_structure_agent_job_payload",
                        return_value=({}, result, {}),
                    ),
                ):
                    artifact = get_document_source_page(
                        "project-1",
                        "document-1",
                        1,
                    )
            finally:
                RUNTIME_STATE.container = previous_container

        self.assertEqual(json.loads(artifact.content), page_payload)
        self.assertEqual(artifact.media_type, "application/json")

    def test_public_contract_does_not_expose_object_keys(self) -> None:
        source_sha256 = "a" * 64
        public = public_source_page_snapshot(
            {
                "source_sha256": source_sha256,
                "source_page_snapshot": {
                    "schema_version": SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION,
                    "state": "READY",
                    "snapshot_id": "b" * 64,
                    "project_id": "project-1",
                    "document_id": "document-1",
                    "source_sha256": source_sha256,
                    "source_ir_id": "source-ir-1",
                    "page_schema_version": "extraction-page/v4",
                    "page_count": 1,
                    "pages": {
                        "1": {
                            "storage_key": "private/key.json",
                            "sha256": "c" * 64,
                            "size": 10,
                        }
                    },
                },
            },
            project_id="project-1",
            document_id="document-1",
        )

        self.assertEqual(public["state"], "READY")
        self.assertNotIn("pages", public)
        self.assertNotIn("storage_key", public)

    def test_geometry_indexes_are_split_out_before_page_facts_are_persisted(
        self,
    ) -> None:
        preview = _preview()
        preview[PDF_PAGE_GEOMETRY_PREVIEW_KEY] = _geometry_preview()
        written: dict[str, dict] = {}

        def put(*, storage_key: str, payload: dict) -> dict:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            written[storage_key] = payload
            return {
                "storage_key": storage_key,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "size": len(encoded),
                "media_type": "application/json",
            }

        with patch("app.application.ingestion._put_runtime_json", side_effect=put):
            snapshot = persist_source_page_preview(
                preview,
                project_id="project-1",
                document_id="document-1",
            )

        assert snapshot is not None
        geometry = snapshot["geometry_index"]
        self.assertEqual(geometry["state"], "READY")
        self.assertEqual(geometry["page_count"], 1)
        page_fact_key = snapshot["pages"]["1"]["storage_key"]
        self.assertNotIn(PDF_PAGE_GEOMETRY_PREVIEW_KEY, written[page_fact_key])
        public = public_source_page_snapshot(
            {
                "source_sha256": "a" * 64,
                "source_page_snapshot": snapshot,
            },
            project_id="project-1",
            document_id="document-1",
        )
        self.assertEqual(public["geometry_index"]["state"], "READY")
        self.assertNotIn("pages", public["geometry_index"])
        self.assertNotIn("storage_key", json.dumps(public))


def _preview() -> dict:
    source_sha256 = "a" * 64
    return {
        "schema_version": "source-page-fact-preview/v1",
        "source_identity": {
            "source_sha256": source_sha256,
            "source_ir_id": "source-ir-1",
            "adapter_version": "test",
        },
        "page_comparison": {
            "schema_version": "document-page-comparison/v5",
            "coordinate_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
            "interaction": "PERSISTENT_LABELED_BOXES",
            "content_type_contract": ["TEXT", "TABLE"],
            "coverage": {},
            "counts": {},
            "pages": [
                {
                    "page_number": 1,
                    "bounds": [0, 0, 595, 842],
                    "blocks": [
                        {
                            "id": "source-node-" + "1" * 32,
                            "type": "TEXT",
                            "text": "Supplier shall provide a pump.",
                            "bbox": [72, 60, 260, 78],
                            "locatable": True,
                        }
                    ],
                    "issues": [],
                    "coverage": {},
                    "counts": {},
                }
            ],
            "unassigned_blocks": [],
            "unassigned_issues": [],
        },
        "tables": [],
        "findings": [],
        "diagnostics": [],
    }


def _staging_candidate() -> dict:
    return {
        "schema_version": "pdf-source-page-staging-candidate/v1",
        "source_sha256": "a" * 64,
        "adapter": {"id": "pdf-test", "version": "1.0"},
        "page_number": 1,
        "total_pages": 2,
        "bounds": [0, 0, 595, 842],
        "nodes": [
            {
                "source_node_id": "node-" + "1" * 32,
                "kind": "table",
                "source_order": 0,
                "raw_text": "Threshold | 10",
                "native_locator": {
                    "format": "pdf",
                    "page_number": 1,
                    "table_index": 0,
                    "bbox": [72, 60, 300, 120],
                },
                "table_payload": {
                    "table_id": "table-1",
                    "row_count": 1,
                    "column_count": 2,
                    "cells": [],
                },
                "extraction_diagnostics": [],
            }
        ],
        "assets": [],
        "visual_text_snapshots": [],
        "visual_table_snapshots": [],
        "diagnostics": [],
    }


def _geometry_preview() -> dict:
    identity = PdfPageGeometrySourceIdentityV1(
        source_sha256="a" * 64,
        source_ir_id="source-ir-1",
        source_ir_schema_version="source-ir/v1",
        adapter_id="pdf-test",
        adapter_version="test",
    )
    page = create_pdf_page_geometry_index(
        source_identity=identity,
        page_number=1,
        page_width=595,
        page_height=842,
        rotation_cw_degrees=0,
        text_lines=(
            PdfPageGeometryLineV1(
                source_node_id="source-node-" + "1" * 32,
                source_text_length=10,
                start_offset=0,
                end_offset=10,
                bbox=(72, 60, 260, 78),
                source_order=1,
            ),
        ),
    )
    return {
        "schema_version": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
        "source_identity": identity.model_dump(mode="json"),
        "coordinate_space": PDF_VISIBLE_COORDINATE_SPACE,
        "page_count": 1,
        "pages": {"1": page.model_dump(mode="json")},
    }


if __name__ == "__main__":
    unittest.main()
