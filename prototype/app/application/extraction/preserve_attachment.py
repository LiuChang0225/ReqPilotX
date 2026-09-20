from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from os import PathLike

from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction.classify_content import classify_content
from app.application.extraction.project_requirements import project_requirements
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.attachment import (
    AttachmentDecisionMode,
    AttachmentDispositionSnapshot,
    AttachmentReference,
    AttachmentReferenceRole,
    DeepRecognitionMode,
    ExtractionDocumentContext,
    stable_attachment_reference_id,
    stable_attachment_snapshot_id,
)
from app.domain.extraction.preflight import DocumentPreflightReport
from app.domain.extraction.requirement_view import RequirementViewSnapshot
from app.domain.extraction.semantics import SemanticClass
from app.domain.extraction.semantics import SemanticClassificationSnapshot
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceAssetKind,
    SourceAssetRef,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
)
from app.domain.extraction.structure import StructureTreeSnapshot
from app.ports.attachment_store import AttachmentArtifactStorePort
from app.ports.document_preflight import DocumentPreflightPort


class AttachmentPreservationRejectionCode(str, Enum):
    PREFLIGHT_FAILED = "preflight_failed"
    SOURCE_IDENTITY_MISMATCH = "source_identity_mismatch"
    STORE_FAILED = "store_failed"
    STORE_VERIFICATION_FAILED = "store_verification_failed"
    PIPELINE_INVARIANT_FAILED = "pipeline_invariant_failed"


OPAQUE_ATTACHMENT_ADAPTER_ID = "opaque-attachment-source-ir"
OPAQUE_ATTACHMENT_ADAPTER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class OpaqueAttachmentExtraction:
    disposition: AttachmentDispositionSnapshot
    source_ir: SourceIRSnapshot
    structure_tree: StructureTreeSnapshot
    semantics: SemanticClassificationSnapshot
    requirement_view: RequirementViewSnapshot


class AttachmentPreservationRejected(ValueError):
    def __init__(
        self,
        code: AttachmentPreservationRejectionCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code

    def safe_detail(self) -> dict[str, str]:
        return {
            "code": self.code.value,
            "message": "The document could not enter the opaque attachment lane.",
        }


def preserve_opaque_attachment(
    source_path: str | PathLike[str],
    *,
    context: ExtractionDocumentContext,
    artifact_store: AttachmentArtifactStorePort,
    preflight: DocumentPreflightPort | None = None,
    preflight_report: DocumentPreflightReport | None = None,
    created_at: datetime | None = None,
) -> AttachmentDispositionSnapshot:
    """Preserve one trusted attachment without content extraction or OCR."""

    if not isinstance(context, ExtractionDocumentContext):
        raise TypeError("context must be an ExtractionDocumentContext")
    if preflight is not None and preflight_report is not None:
        raise ValueError("provide a preflight port or a verified report, not both")
    report = preflight_report or (preflight or LocalDocumentPreflight()).inspect(
        source_path
    )
    if not isinstance(report, DocumentPreflightReport):
        raise TypeError("preflight must return a DocumentPreflightReport")
    if not report.attachment_acceptable:
        raise AttachmentPreservationRejected(
            AttachmentPreservationRejectionCode.PREFLIGHT_FAILED,
            "Document preflight does not permit opaque attachment preservation.",
        )
    if (
        report.source_sha256 != context.source_sha256
        or report.source_size != context.source_size
    ):
        raise AttachmentPreservationRejected(
            AttachmentPreservationRejectionCode.SOURCE_IDENTITY_MISMATCH,
            "Trusted attachment context does not match the source bytes.",
        )
    try:
        artifact = artifact_store.preserve(
            source_path,
            expected_sha256=context.source_sha256,
            expected_size=context.source_size,
            media_type=report.identity.mime_type,
        )
    except Exception as exc:
        raise AttachmentPreservationRejected(
            AttachmentPreservationRejectionCode.STORE_FAILED,
            "The attachment artifact store rejected the source bytes.",
        ) from exc
    if not artifact_store.verify(artifact):
        raise AttachmentPreservationRejected(
            AttachmentPreservationRejectionCode.STORE_VERIFICATION_FAILED,
            "The attachment store receipt could not be verified.",
        )

    reference = AttachmentReference(
        attachment_reference_id=stable_attachment_reference_id(
            artifact_id=artifact.artifact_id
        ),
        artifact_id=artifact.artifact_id,
        semantic_class=SemanticClass.ATTACHMENT,
        role=AttachmentReferenceRole.REFERENCE,
        decision_mode=AttachmentDecisionMode.NONE,
    )
    return AttachmentDispositionSnapshot(
        attachment_snapshot_id=stable_attachment_snapshot_id(
            context=context,
            artifact_id=artifact.artifact_id,
        ),
        context=context,
        detected_format=report.identity.detected_format,
        preflight_schema_version=report.schema_version,
        page_count=report.page_count,
        artifact=artifact,
        reference=reference,
        semantic_class=SemanticClass.ATTACHMENT,
        deep_recognition_mode=DeepRecognitionMode.BACKLOG,
        blocks_requirement_source_extraction=False,
        ocr_provider_ids=(),
        visual_text_snapshot_count=0,
        decision_requirement_count=0,
        created_at=created_at or datetime.now(timezone.utc),
    )


def preserve_opaque_attachment_pipeline(
    source_path: str | PathLike[str],
    *,
    context: ExtractionDocumentContext,
    artifact_store: AttachmentArtifactStorePort,
    preflight: DocumentPreflightPort | None = None,
    preflight_report: DocumentPreflightReport | None = None,
    created_at: datetime | None = None,
) -> OpaqueAttachmentExtraction:
    """Build the real four-layer opaque attachment outcome.

    The preserved artifact becomes one whole-document SourceIR fact. The
    existing deterministic resolver and classifier then derive one ATTACHMENT
    semantic annotation, while the ordinary requirement projector must produce
    no decision requirement. Deep drawing recognition and OCR remain outside
    this lane.
    """

    disposition = preserve_opaque_attachment(
        source_path,
        context=context,
        artifact_store=artifact_store,
        preflight=preflight,
        preflight_report=preflight_report,
        created_at=created_at,
    )
    native_format = NativeDocumentFormat(disposition.detected_format.value)
    locator = NativeLocator(format=native_format, whole_document=True)
    artifact = SourceAssetRef(
        asset_id=disposition.artifact.artifact_id,
        kind=SourceAssetKind.ATTACHMENT,
        content_sha256=disposition.artifact.source_sha256,
        media_type=disposition.artifact.media_type,
        storage_key=disposition.artifact.storage_key,
        native_locator=locator,
    )
    node = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=context.source_sha256,
            adapter_id=OPAQUE_ATTACHMENT_ADAPTER_ID,
            adapter_version=OPAQUE_ATTACHMENT_ADAPTER_VERSION,
            kind=SourceNodeKind.ATTACHMENT,
            native_locator=locator,
        ),
        kind=SourceNodeKind.ATTACHMENT,
        source_order=0,
        raw_text="",
        native_locator=locator,
        asset_ref=artifact.asset_id,
    )
    document_id = stable_document_id(context.source_sha256)
    source_ir = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=context.source_sha256,
            adapter_id=OPAQUE_ATTACHMENT_ADAPTER_ID,
            adapter_version=OPAQUE_ATTACHMENT_ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=context.source_sha256,
        adapter_id=OPAQUE_ATTACHMENT_ADAPTER_ID,
        adapter_version=OPAQUE_ATTACHMENT_ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=(node,),
        assets=(artifact,),
        visual_text_snapshots=(),
        diagnostics=(),
        created_at=disposition.created_at,
    )
    structure_tree = build_structure_tree(source_ir)
    semantics = classify_content(source_ir, structure_tree)
    requirement_view = project_requirements(
        source_ir,
        structure_tree,
        semantics,
    )
    semantic_classes = tuple(
        annotation.semantic_class for annotation in semantics.annotations
    )
    if (
        semantic_classes != (SemanticClass.ATTACHMENT,)
        or requirement_view.nodes
        or source_ir.visual_text_snapshots
    ):
        raise AttachmentPreservationRejected(
            AttachmentPreservationRejectionCode.PIPELINE_INVARIANT_FAILED,
            "Opaque attachment derivation violated its zero-decision policy.",
        )
    return OpaqueAttachmentExtraction(
        disposition=disposition,
        source_ir=source_ir,
        structure_tree=structure_tree,
        semantics=semantics,
        requirement_view=requirement_view,
    )


__all__ = [
    "AttachmentPreservationRejected",
    "AttachmentPreservationRejectionCode",
    "OPAQUE_ATTACHMENT_ADAPTER_ID",
    "OPAQUE_ATTACHMENT_ADAPTER_VERSION",
    "OpaqueAttachmentExtraction",
    "preserve_opaque_attachment",
    "preserve_opaque_attachment_pipeline",
]
