from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from reqpilotx_server.adapters.agents.document_evidence_host import (
    DocumentEvidenceToolError,
    DocumentEvidenceToolHost,
    RenderedDocumentEvidence,
)
from reqpilotx_server.application.agent_workflow.document_understanding import (
    DocumentAssetLedgerV1,
    DocumentPageFactsChunkV1,
    DocumentPageTextChunkV1,
    DocumentRenderDetail,
    DocumentTableMapChunkV1,
    canonical_json_sha256,
)
from reqpilotx_server.application.agent_workflow.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryOutcome,
    ParentProvisionEvidenceRecoveryState,
    ParentProvisionEvidenceRecoveryStrategy,
)
from reqpilotx_server.ports.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryExecutionRequestV1,
    ParentProvisionEvidenceRecoveryExecutionResultV1,
    ParentProvisionEvidenceRecoveryExecutorError,
)


@dataclass(frozen=True, slots=True)
class DocumentParentProvisionEvidenceAcquisition:
    """Object-scoped evidence read by the Host before publishing a new revision."""

    page_facts: tuple[DocumentPageFactsChunkV1, ...] = ()
    page_text: tuple[DocumentPageTextChunkV1, ...] = ()
    page_renders: tuple[RenderedDocumentEvidence, ...] = ()
    table_maps: tuple[DocumentTableMapChunkV1, ...] = ()
    asset_ledgers: tuple[DocumentAssetLedgerV1, ...] = ()

    @property
    def evidence_sha256(self) -> str:
        return canonical_json_sha256(
            {
                "page_facts": [
                    {
                        "page_number": item.page_number,
                        "page_facts_sha256": item.page_facts_sha256,
                        "offset": item.offset,
                        "item_count": len(item.items),
                    }
                    for item in self.page_facts
                ],
                "page_text": [
                    {
                        "page_number": item.page_number,
                        "page_text_sha256": item.page_text_sha256,
                        "offset": item.offset,
                        "item_count": len(item.items),
                    }
                    for item in self.page_text
                ],
                "page_renders": [
                    item.receipt.model_dump(mode="json") for item in self.page_renders
                ],
                "table_maps": [
                    {
                        "table_id": item.table_id,
                        "table_map_sha256": item.table_map_sha256,
                        "offset": item.offset,
                        "cell_count": len(item.cells),
                    }
                    for item in self.table_maps
                ],
                "asset_ledgers": [
                    {
                        "page_number": item.page_number,
                        "assets_sha256": item.assets_sha256,
                        "asset_count": len(item.assets),
                    }
                    for item in self.asset_ledgers
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class PublishedParentProvisionEvidenceRevision:
    new_stage_input_bundle_sha256: str
    new_target_evidence_sha256: str


DocumentParentProvisionEvidencePublisher = Callable[
    [
        ParentProvisionEvidenceRecoveryExecutionRequestV1,
        DocumentParentProvisionEvidenceAcquisition,
    ],
    PublishedParentProvisionEvidenceRevision,
]


class DocumentParentProvisionEvidenceRecoveryExecutor:
    """Run native/render/table evidence tools for one exact logical object.

    The publisher is responsible for committing an immutable stage-input
    revision.  Tool reads alone never count as recovered evidence.
    """

    def __init__(
        self,
        *,
        host: DocumentEvidenceToolHost,
        publish_revision: DocumentParentProvisionEvidencePublisher,
    ) -> None:
        self._host = host
        self._publish_revision = publish_revision
        self._initialized_source_handle: str | None = None

    def execute(
        self,
        request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
        *,
        cancellation_requested: Callable[[], bool],
    ) -> ParentProvisionEvidenceRecoveryExecutionResultV1:
        if cancellation_requested():
            return _terminal_failure(
                request,
                state=ParentProvisionEvidenceRecoveryState.CANCELLED,
                outcome=ParentProvisionEvidenceRecoveryOutcome.CANCELLED,
                failure_code="RECOVERY_CANCELLED",
            )
        try:
            self._ensure_prelude(request)
            acquisition = self._acquire(request)
        except DocumentEvidenceToolError as exc:
            return _terminal_failure(
                request,
                state=ParentProvisionEvidenceRecoveryState.FAILED,
                outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
                failure_code=_document_tool_failure_code(exc.reason_code),
            )
        except ParentProvisionEvidenceRecoveryExecutorError:
            raise
        except Exception as exc:
            raise ParentProvisionEvidenceRecoveryExecutorError(
                "RECOVERY_EVIDENCE_ACQUISITION_FAILED"
            ) from exc
        if isinstance(acquisition, ParentProvisionEvidenceRecoveryExecutionResultV1):
            return acquisition
        if cancellation_requested():
            return _terminal_failure(
                request,
                state=ParentProvisionEvidenceRecoveryState.CANCELLED,
                outcome=ParentProvisionEvidenceRecoveryOutcome.CANCELLED,
                failure_code="RECOVERY_CANCELLED",
            )
        try:
            published = self._publish_revision(request, acquisition)
        except Exception as exc:
            raise ParentProvisionEvidenceRecoveryExecutorError(
                "RECOVERY_REVISION_PUBLICATION_FAILED"
            ) from exc
        if (
            published.new_stage_input_bundle_sha256
            == request.session.plan.original_stage_input_bundle_sha256
        ):
            return _terminal_failure(
                request,
                state=ParentProvisionEvidenceRecoveryState.FAILED,
                outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
                failure_code="RECOVERY_REVISION_UNCHANGED",
            )
        return ParentProvisionEvidenceRecoveryExecutionResultV1(
            request_sha256=request.request_sha256,
            attempt_id=request.attempt_id,
            strategy=request.strategy,
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            new_stage_input_bundle_sha256=(published.new_stage_input_bundle_sha256),
            new_target_evidence_sha256=published.new_target_evidence_sha256,
        )

    def _ensure_prelude(
        self,
        request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    ) -> None:
        source_handle = request.target.source_handle
        if self._initialized_source_handle is not None:
            if self._initialized_source_handle != source_handle:
                raise ParentProvisionEvidenceRecoveryExecutorError(
                    "RECOVERY_SOURCE_HANDLE_DRIFT"
                )
            return
        manifest = self._host.get_run_manifest(source_handle=source_handle)
        self._host.get_page_ledger(source_handle=source_handle)
        if manifest.source_sha256 != request.session.plan.source_sha256:
            raise ParentProvisionEvidenceRecoveryExecutorError(
                "RECOVERY_SOURCE_IDENTITY_MISMATCH"
            )
        self._initialized_source_handle = source_handle

    def _acquire(
        self,
        request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    ) -> (
        DocumentParentProvisionEvidenceAcquisition
        | ParentProvisionEvidenceRecoveryExecutionResultV1
    ):
        strategy = request.strategy
        if strategy is ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT:
            chunks = self._read_all_page_text(request)
            text_by_page = {
                page_number: "".join(
                    item.text
                    for chunk in chunks
                    if chunk.page_number == page_number
                    for item in chunk.items
                )
                for page_number in request.target.page_numbers
            }
            if any(not value.strip() for value in text_by_page.values()):
                return _terminal_failure(
                    request,
                    state=ParentProvisionEvidenceRecoveryState.FAILED,
                    outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
                    failure_code="NATIVE_TEXT_STILL_EMPTY",
                )
            return DocumentParentProvisionEvidenceAcquisition(page_text=chunks)

        if strategy is ParentProvisionEvidenceRecoveryStrategy.RERENDER_SOURCE_PAGES:
            renders = tuple(
                self._host.render_page(
                    source_handle=request.target.source_handle,
                    page_number=page_number,
                    detail=DocumentRenderDetail.HIGH,
                )
                for page_number in request.target.page_numbers
            )
            return DocumentParentProvisionEvidenceAcquisition(page_renders=renders)

        if strategy is ParentProvisionEvidenceRecoveryStrategy.REBUILD_TABLE_EVIDENCE:
            if not request.target.table_ids:
                return _terminal_failure(
                    request,
                    state=ParentProvisionEvidenceRecoveryState.FAILED,
                    outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
                    failure_code="RECOVERY_TABLE_BINDINGS_MISSING",
                )
            return DocumentParentProvisionEvidenceAcquisition(
                table_maps=self._read_all_table_maps(request)
            )

        if strategy is ParentProvisionEvidenceRecoveryStrategy.REBIND_EXISTING_EVIDENCE:
            facts = self._read_all_page_facts(request)
            text = self._read_all_page_text(request)
            tables = (
                self._read_all_table_maps(request) if request.target.table_ids else ()
            )
            assets = tuple(
                self._host.get_assets(
                    source_handle=request.target.source_handle,
                    page_number=page_number,
                )
                for page_number in request.target.page_numbers
            )
            observed_asset_ids = {
                item.asset_id for ledger in assets for item in ledger.assets
            }
            if not set(request.target.asset_ids).issubset(observed_asset_ids):
                return _terminal_failure(
                    request,
                    state=ParentProvisionEvidenceRecoveryState.FAILED,
                    outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
                    failure_code="RECOVERY_ASSET_BINDING_MISSING",
                )
            return DocumentParentProvisionEvidenceAcquisition(
                page_facts=facts,
                page_text=text,
                table_maps=tables,
                asset_ledgers=assets,
            )

        if (
            strategy
            is ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE
        ):
            return DocumentParentProvisionEvidenceAcquisition()

        if strategy is ParentProvisionEvidenceRecoveryStrategy.REBUILD_ASSET_PLACEMENTS:
            return _terminal_failure(
                request,
                state=ParentProvisionEvidenceRecoveryState.FAILED,
                outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
                failure_code="RECOVERY_ASSET_PLACEMENT_CAPABILITY_UNAVAILABLE",
            )

        if strategy in {
            ParentProvisionEvidenceRecoveryStrategy.AUTHORIZED_VISUAL_TEXT_OCR,
            ParentProvisionEvidenceRecoveryStrategy.VISUAL_RECONSTRUCT_TABLE,
            ParentProvisionEvidenceRecoveryStrategy.RECONSTRUCT_FIGURE_EVIDENCE,
        }:
            return _terminal_failure(
                request,
                state=ParentProvisionEvidenceRecoveryState.FAILED,
                outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
                failure_code="RECOVERY_VISUAL_CAPABILITY_UNAVAILABLE",
            )

        return _terminal_failure(
            request,
            state=ParentProvisionEvidenceRecoveryState.BLOCKED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.NOT_RECOVERABLE,
            failure_code="RECOVERY_STRATEGY_NOT_EXECUTABLE",
        )

    def _read_all_page_text(
        self,
        request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    ) -> tuple[DocumentPageTextChunkV1, ...]:
        chunks: list[DocumentPageTextChunkV1] = []
        for page_number in request.target.page_numbers:
            offset = 0
            while True:
                chunk = self._host.get_page_text(
                    source_handle=request.target.source_handle,
                    page_number=page_number,
                    offset=offset,
                    limit=512,
                )
                chunks.append(chunk)
                if chunk.next_offset is None:
                    break
                offset = chunk.next_offset
        return tuple(chunks)

    def _read_all_page_facts(
        self,
        request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    ) -> tuple[DocumentPageFactsChunkV1, ...]:
        chunks: list[DocumentPageFactsChunkV1] = []
        for page_number in request.target.page_numbers:
            offset = 0
            while True:
                chunk = self._host.get_page_facts(
                    source_handle=request.target.source_handle,
                    page_number=page_number,
                    offset=offset,
                    limit=1024,
                )
                chunks.append(chunk)
                if chunk.next_offset is None:
                    break
                offset = chunk.next_offset
        return tuple(chunks)

    def _read_all_table_maps(
        self,
        request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    ) -> tuple[DocumentTableMapChunkV1, ...]:
        chunks: list[DocumentTableMapChunkV1] = []
        for table_id in request.target.table_ids:
            offset = 0
            while True:
                chunk = self._host.get_table_map(
                    source_handle=request.target.source_handle,
                    table_id=table_id,
                    include_cells=True,
                    offset=offset,
                    limit=200,
                )
                if chunk.page_number not in request.target.page_numbers:
                    raise ParentProvisionEvidenceRecoveryExecutorError(
                        "RECOVERY_TABLE_SCOPE_MISMATCH"
                    )
                chunks.append(chunk)
                if chunk.next_offset is None:
                    break
                offset = chunk.next_offset
        return tuple(chunks)


def _terminal_failure(
    request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    *,
    state: ParentProvisionEvidenceRecoveryState,
    outcome: ParentProvisionEvidenceRecoveryOutcome,
    failure_code: str,
) -> ParentProvisionEvidenceRecoveryExecutionResultV1:
    return ParentProvisionEvidenceRecoveryExecutionResultV1(
        request_sha256=request.request_sha256,
        attempt_id=request.attempt_id,
        strategy=request.strategy,
        state=state,
        outcome=outcome,
        failure_code=failure_code,
    )


def _document_tool_failure_code(reason_code: str) -> str:
    normalized = "".join(
        character if character.isalnum() else "_" for character in reason_code.upper()
    ).strip("_")
    value = f"DOCUMENT_EVIDENCE_{normalized}"[:120]
    return value or "DOCUMENT_EVIDENCE_TOOL_FAILED"


__all__ = [
    "DocumentParentProvisionEvidenceAcquisition",
    "DocumentParentProvisionEvidencePublisher",
    "DocumentParentProvisionEvidenceRecoveryExecutor",
    "PublishedParentProvisionEvidenceRevision",
]
