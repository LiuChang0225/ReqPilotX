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
from pathlib import Path
import tempfile
import unittest

import pymupdf

from app.adapters.agents.document_evidence_host import (
    DocumentEvidenceToolError,
    DocumentEvidenceToolHost,
    prepare_document_evidence,
)
from app.adapters.agents.portable_kit import load_portable_agent_kit
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.application.agent_workflow.document_understanding import (
    DocumentEvidenceAuthority,
    DocumentEvidenceRequirement,
    DocumentRenderDetail,
    DocumentUnderstandingPageV2,
    DocumentUnderstandingProposalV2,
    DocumentUnderstandingStatus,
    DocumentVisualObservationKind,
    DocumentVisualObservationV2,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class DocumentEvidenceHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        two_page = root / "two-page.pdf"
        build_synthetic_pdf(two_page, include_prompt_injection=True)
        self.source = root / "portable-d1-three-page.pdf"
        source_document = pymupdf.open(two_page)
        combined = pymupdf.open()
        combined.insert_pdf(source_document)
        combined.new_page()
        combined.save(self.source)
        combined.close()
        source_document.close()
        self.source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.kit = load_portable_agent_kit(REPOSITORY_ROOT / "agent-kit")
        self.stage_context_sha256 = "e" * 64
        self.prepared = prepare_document_evidence(
            self.source,
            expected_source_sha256=self.source_sha256,
            document_role="CUSTOMER_SOR",
            kit_identity=self.kit.identity,
            stage_context_sha256=self.stage_context_sha256,
            run_id="agent-run-" + "a" * 32,
            opaque_source_handle="source-handle-" + "b" * 32,
            intentional_blank_pages=(3,),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepared_ledger_preserves_native_visual_and_blank_authority(self) -> None:
        ledger = self.prepared.ledger

        self.assertEqual(ledger.page_count, 3)
        self.assertEqual(
            ledger.pages[2].evidence_requirement,
            DocumentEvidenceRequirement.INTENTIONAL_BLANK,
        )
        self.assertIn(
            ledger.pages[1].evidence_requirement,
            {
                DocumentEvidenceRequirement.VISUAL_REQUIRED,
                DocumentEvidenceRequirement.NATIVE_AND_VISUAL,
            },
        )
        serialized = json.dumps(
            {
                "manifest": self.prepared.manifest.model_dump(mode="json"),
                "ledger": ledger.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        self.assertNotIn(str(self.source), serialized)
        self.assertNotIn(self.source.name, serialized)

    def test_complete_d1_proposal_requires_full_reads_and_visual_receipts(self) -> None:
        host = DocumentEvidenceToolHost(self.prepared)
        proposal = self._complete_proposal(host)

        receipt = host.validate_understanding(proposal)

        self.assertEqual(receipt.status, DocumentUnderstandingStatus.COMPLETE)
        self.assertEqual(receipt.page_count, 3)
        self.assertEqual(receipt.unresolved_pages, ())
        self.assertEqual(receipt.kit_bundle_sha256, self.kit.identity.bundle_sha256)

    def test_table_assets_and_region_render_are_real_source_bound_tools(self) -> None:
        host = DocumentEvidenceToolHost(self.prepared)
        handle = self.prepared.manifest.opaque_source_handle
        host.get_run_manifest(
            source_handle=handle,
            stage_context_sha256=self.stage_context_sha256,
        )
        ledger = host.get_page_ledger(source_handle=handle)
        page_one = ledger.pages[0]
        assets = host.get_assets(source_handle=handle, page_number=1)
        self.assertEqual(
            tuple(item.asset_id for item in assets.assets),
            page_one.asset_ids,
        )
        if page_one.table_ids:
            table = host.get_table_map(
                source_handle=handle,
                table_id=page_one.table_ids[0],
                include_cells=True,
            )
            self.assertGreaterEqual(table.row_count, 1)
            self.assertGreaterEqual(table.column_count, 1)
            self.assertTrue(table.cells)
        x0, y0, x1, y1 = page_one.bounds
        rendered = host.render_region(
            source_handle=handle,
            page_number=1,
            bbox=(x0, y0, x0 + (x1 - x0) / 2, y0 + (y1 - y0) / 2),
            scale=2.0,
        )
        self.assertEqual(
            hashlib.sha256(rendered.image_bytes).hexdigest(),
            rendered.receipt.image_sha256,
        )
        self.assertEqual(rendered.receipt.detail, DocumentRenderDetail.REGION)

    def test_validation_fails_closed_on_unread_pages_and_unknown_handle(self) -> None:
        complete_host = DocumentEvidenceToolHost(self.prepared)
        proposal = self._complete_proposal(complete_host)
        incomplete_host = DocumentEvidenceToolHost(self.prepared)
        handle = self.prepared.manifest.opaque_source_handle
        incomplete_host.get_run_manifest(
            source_handle=handle,
            stage_context_sha256=self.stage_context_sha256,
        )
        incomplete_host.get_page_ledger(source_handle=handle)

        with self.assertRaisesRegex(
            DocumentEvidenceToolError,
            "document_understanding_page_facts_incomplete",
        ):
            incomplete_host.validate_understanding(proposal)
        with self.assertRaisesRegex(
            DocumentEvidenceToolError,
            "document_source_handle_unknown",
        ):
            incomplete_host.get_page_facts(
                source_handle="source-handle-" + "f" * 32,
                page_number=1,
            )

    def _complete_proposal(
        self,
        host: DocumentEvidenceToolHost,
    ) -> DocumentUnderstandingProposalV2:
        handle = self.prepared.manifest.opaque_source_handle
        manifest = host.get_run_manifest(
            source_handle=handle,
            stage_context_sha256=self.stage_context_sha256,
        )
        ledger = host.get_page_ledger(source_handle=handle)
        pages = []
        unresolved = False
        for entry in ledger.pages:
            if entry.evidence_requirement is not DocumentEvidenceRequirement.INTENTIONAL_BLANK:
                self._read_all_facts(host, entry.page_number)
                self._read_all_text(host, entry.page_number)
            observations = ()
            authority = {
                DocumentEvidenceRequirement.NATIVE: DocumentEvidenceAuthority.NATIVE,
                DocumentEvidenceRequirement.VISUAL_REQUIRED: DocumentEvidenceAuthority.VISUAL,
                DocumentEvidenceRequirement.NATIVE_AND_VISUAL: (
                    DocumentEvidenceAuthority.NATIVE_AND_VISUAL
                ),
                DocumentEvidenceRequirement.INTENTIONAL_BLANK: (
                    DocumentEvidenceAuthority.INTENTIONAL_BLANK
                ),
                DocumentEvidenceRequirement.UNRESOLVED: (
                    DocumentEvidenceAuthority.UNRESOLVED
                ),
            }[entry.evidence_requirement]
            unresolved_codes = ()
            if entry.evidence_requirement in {
                DocumentEvidenceRequirement.VISUAL_REQUIRED,
                DocumentEvidenceRequirement.NATIVE_AND_VISUAL,
            }:
                rendered = host.render_page(
                    source_handle=handle,
                    page_number=entry.page_number,
                    detail=DocumentRenderDetail.HIGH,
                )
                observations = (
                    DocumentVisualObservationV2(
                        kind=DocumentVisualObservationKind.OTHER,
                        visible_text="visual evidence inspected",
                        image_sha256=rendered.receipt.image_sha256,
                    ),
                )
            elif entry.evidence_requirement is DocumentEvidenceRequirement.UNRESOLVED:
                unresolved = True
                unresolved_codes = ("page_evidence_unresolved",)
            pages.append(
                DocumentUnderstandingPageV2(
                    page_number=entry.page_number,
                    authority=authority,
                    page_facts_sha256=entry.page_facts_sha256,
                    page_text_sha256=entry.page_text_sha256,
                    table_ids=entry.table_ids,
                    asset_ids=entry.asset_ids,
                    visual_observations=observations,
                    unresolved_codes=unresolved_codes,
                )
            )
        return DocumentUnderstandingProposalV2(
            run_id=manifest.run_id,
            source_sha256=manifest.source_sha256,
            page_count=manifest.page_count,
            ledger_sha256=ledger.ledger_sha256,
            status=(
                DocumentUnderstandingStatus.PARTIAL
                if unresolved
                else DocumentUnderstandingStatus.COMPLETE
            ),
            pages=tuple(pages),
        )

    def _read_all_facts(self, host: DocumentEvidenceToolHost, page_number: int) -> None:
        handle = self.prepared.manifest.opaque_source_handle
        offset = 0
        while True:
            chunk = host.get_page_facts(
                source_handle=handle,
                page_number=page_number,
                offset=offset,
                limit=32,
            )
            if chunk.next_offset is None:
                return
            offset = chunk.next_offset

    def _read_all_text(self, host: DocumentEvidenceToolHost, page_number: int) -> None:
        handle = self.prepared.manifest.opaque_source_handle
        offset = 0
        while True:
            chunk = host.get_page_text(
                source_handle=handle,
                page_number=page_number,
                offset=offset,
                limit=16,
            )
            if chunk.next_offset is None:
                return
            offset = chunk.next_offset


if __name__ == "__main__":
    unittest.main()
