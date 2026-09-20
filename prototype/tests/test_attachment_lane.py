from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from PIL import Image as PILImage
from pypdf import PdfWriter

from app.adapters.documents.local_attachment_store import LocalAttachmentArtifactStore
from app.adapters.documents.preflight import preflight_document
from app.application.extraction.preserve_attachment import (
    AttachmentPreservationRejected,
    AttachmentPreservationRejectionCode,
    preserve_opaque_attachment,
    preserve_opaque_attachment_pipeline,
)
from app.domain.extraction.attachment import (
    AttachmentDecisionMode,
    AttachmentReferenceRole,
    DeepRecognitionMode,
    ExtractionDocumentContext,
    ExtractionDocumentRole,
    ExtractionIngressRoute,
)
from app.domain.extraction.preflight import DocumentFormat
from app.domain.extraction.semantics import SemanticClass
from app.domain.extraction.source_ir import SourceAssetKind, SourceNodeKind


CREATED_AT = datetime(2026, 7, 21, 9, 30, tzinfo=timezone.utc)


class OpaqueAttachmentLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.store = LocalAttachmentArtifactStore(self.root / "store")

    def test_pdf_is_preserved_as_one_non_decision_attachment_without_ocr(
        self,
    ) -> None:
        source = self._write_pdf("customer-drawing.pdf")
        context = self._context(source, revision="UPLOAD-REV-DRAWING-001")

        pipeline = preserve_opaque_attachment_pipeline(
            source,
            context=context,
            artifact_store=self.store,
            created_at=CREATED_AT,
        )
        snapshot = pipeline.disposition

        self.assertEqual(snapshot.detected_format, DocumentFormat.PDF)
        self.assertEqual(snapshot.page_count, 1)
        self.assertEqual(snapshot.semantic_class, SemanticClass.ATTACHMENT)
        self.assertEqual(snapshot.attachment_artifact_count, 1)
        self.assertEqual(snapshot.attachment_reference_count, 1)
        self.assertEqual(snapshot.reference.role, AttachmentReferenceRole.REFERENCE)
        self.assertEqual(snapshot.reference.decision_mode, AttachmentDecisionMode.NONE)
        self.assertEqual(snapshot.decision_requirement_count, 0)
        self.assertEqual(snapshot.ocr_provider_ids, ())
        self.assertEqual(snapshot.visual_text_snapshot_count, 0)
        self.assertEqual(snapshot.deep_recognition_mode, DeepRecognitionMode.BACKLOG)
        self.assertFalse(snapshot.blocks_requirement_source_extraction)
        self.assertTrue(self.store.verify(snapshot.artifact))
        stored = self.store.root / snapshot.artifact.storage_key
        self.assertEqual(stored.read_bytes(), source.read_bytes())
        self.assertNotIn(source.name, snapshot.artifact.storage_key)
        self.assertEqual(pipeline.source_ir.status.value, "COMPLETE")
        self.assertEqual(
            tuple(node.kind for node in pipeline.source_ir.nodes),
            (SourceNodeKind.ATTACHMENT,),
        )
        self.assertEqual(
            tuple(asset.kind for asset in pipeline.source_ir.assets),
            (SourceAssetKind.ATTACHMENT,),
        )
        self.assertEqual(
            tuple(item.semantic_class for item in pipeline.semantics.annotations),
            (SemanticClass.ATTACHMENT,),
        )
        self.assertEqual(len(pipeline.requirement_view.nodes), 0)

    def test_tif_is_attachment_acceptable_without_becoming_extractable(self) -> None:
        source = self.root / "drawing.tif"
        PILImage.new("1", (2, 2), color=1).save(source, format="TIFF")
        report = preflight_document(source)
        context = self._context(source, revision="UPLOAD-REV-DRAWING-002")

        self.assertFalse(report.processable)
        self.assertTrue(report.attachment_acceptable)
        pipeline = preserve_opaque_attachment_pipeline(
            source,
            context=context,
            artifact_store=self.store,
            preflight_report=report,
            created_at=CREATED_AT,
        )
        snapshot = pipeline.disposition

        self.assertEqual(snapshot.detected_format, DocumentFormat.TIF)
        self.assertEqual(snapshot.page_count, 1)
        self.assertTrue(self.store.verify(snapshot.artifact))
        self.assertEqual(snapshot.decision_requirement_count, 0)
        self.assertEqual(
            tuple(item.semantic_class for item in pipeline.semantics.annotations),
            (SemanticClass.ATTACHMENT,),
        )
        self.assertEqual(pipeline.requirement_view.nodes, ())

    def test_source_identity_mismatch_fails_before_any_artifact_is_written(
        self,
    ) -> None:
        source = self._write_pdf("source.pdf")
        report = preflight_document(source)
        context = ExtractionDocumentContext(
            logical_document_id="DOC-DRAWING-TEST",
            upload_revision_id="UPLOAD-REV-DRAWING-003",
            source_sha256="f" * 64,
            source_size=report.source_size,
            document_role=ExtractionDocumentRole.CUSTOMER_DRAWING,
            ingress_route=ExtractionIngressRoute.ATTACHMENT,
        )

        with self.assertRaises(AttachmentPreservationRejected) as caught:
            preserve_opaque_attachment(
                source,
                context=context,
                artifact_store=self.store,
                preflight_report=report,
                created_at=CREATED_AT,
            )

        self.assertEqual(
            caught.exception.code,
            AttachmentPreservationRejectionCode.SOURCE_IDENTITY_MISMATCH,
        )
        self.assertFalse((self.root / "store").exists())

    def test_non_attachment_format_and_untrusted_revision_fail_closed(self) -> None:
        text = self.root / "requirements.txt"
        text.write_text("The controller shall start.", encoding="utf-8")
        context = self._context(text, revision="UPLOAD-REV-DRAWING-004")

        with self.assertRaises(AttachmentPreservationRejected) as caught:
            preserve_opaque_attachment(
                text,
                context=context,
                artifact_store=self.store,
                created_at=CREATED_AT,
            )
        self.assertEqual(
            caught.exception.code,
            AttachmentPreservationRejectionCode.PREFLIGHT_FAILED,
        )

        with self.assertRaises(ValidationError):
            ExtractionDocumentContext(
                logical_document_id="DOC-DRAWING-TEST",
                upload_revision_id="revision from title block",
                source_sha256=context.source_sha256,
                source_size=context.source_size,
                document_role=ExtractionDocumentRole.CUSTOMER_DRAWING,
                ingress_route=ExtractionIngressRoute.ATTACHMENT,
            )

    def test_upload_revision_changes_disposition_but_reuses_exact_artifact(
        self,
    ) -> None:
        source = self._write_pdf("same-bytes.pdf")
        first = preserve_opaque_attachment(
            source,
            context=self._context(source, revision="UPLOAD-REV-DRAWING-005"),
            artifact_store=self.store,
            created_at=CREATED_AT,
        )
        second = preserve_opaque_attachment(
            source,
            context=self._context(source, revision="UPLOAD-REV-DRAWING-006"),
            artifact_store=self.store,
            created_at=CREATED_AT,
        )

        self.assertEqual(first.artifact, second.artifact)
        self.assertNotEqual(first.attachment_snapshot_id, second.attachment_snapshot_id)
        stored_files = tuple(
            path for path in self.store.root.rglob("*") if path.is_file()
        )
        self.assertEqual(len(stored_files), 1)

    def _context(self, source: Path, *, revision: str) -> ExtractionDocumentContext:
        payload = source.read_bytes()
        return ExtractionDocumentContext(
            logical_document_id="DOC-DRAWING-TEST",
            upload_revision_id=revision,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            source_size=len(payload),
            document_role=ExtractionDocumentRole.CUSTOMER_DRAWING,
            ingress_route=ExtractionIngressRoute.ATTACHMENT,
        )

    def _write_pdf(self, name: str) -> Path:
        path = self.root / name
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with path.open("wb") as handle:
            writer.write(handle)
        return path


if __name__ == "__main__":
    unittest.main()
