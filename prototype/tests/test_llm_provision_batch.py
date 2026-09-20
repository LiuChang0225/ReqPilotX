from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from unittest import mock
import hashlib
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from dataclasses import replace
from threading import Barrier, BrokenBarrierError, Event, Lock
from time import sleep

from app.application.extraction.llm_provision_batch import (
    LlmProvisionBatchRejected,
    LlmProvisionBatchRejectionCode,
    LlmProvisionExtractionStatus,
    LlmRequirementBatchStatus,
    LlmRequirementDocumentCheckpoint,
    LlmRequirementDocumentProgress,
    LlmRequirementDocumentResult,
    MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS,
    MAX_LLM_DOCUMENT_CHUNK_NODES,
    MAX_LLM_PROVISIONS_PER_BATCH,
    extract_requirement_document_with_provider,
    extract_requirement_provisions_with_provider,
)
from app.application.extraction.llm_semantics import classify_content_with_provider
from app.application.extraction.document_parent_boundaries import (
    DocumentParentBoundaryProviderResult,
)
from app.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryReferenceV1,
    NativeParentBoundaryStatus,
    NativeProvisionDisposition,
    NativeProvisionBlockV1,
    validate_native_parent_boundary_reference,
)
from app.application.extraction.locate_llm_scope import (
    LlmExtractionScopeLocator,
    LlmScopeLocationRejected,
    LlmSectionScopeSelector,
    locate_llm_document_scope,
    locate_llm_extraction_scope,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import get_base_effective_structure
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from app.domain.extraction.requirement_view import RequirementProjectionRole
from app.domain.extraction.semantics import RequirementSubtype, SemanticClass
from app.domain.extraction.source_ir import SourceNodeKind
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    stable_structure_relation_id,
)
from app.ports.semantic_provider import (
    RequirementAssemblyDecision,
    RequirementAssemblyProviderResult,
    SemanticClassificationProviderResult,
    SemanticDecision,
    SemanticImageInput,
    SemanticProviderRequestRejected,
    SemanticProviderRequestRejectionCode,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationProviderResult,
    RequirementAtomizationRange,
)
from tests.test_llm_semantics import _FakeProvider, _run
from tests.test_semantics import _flat_source
from tests.test_structure_tree import _continued_pdf_table_source


class LlmProvisionBatchTests(unittest.TestCase):
    def test_document_default_chunk_budget_matches_real_provider_boundary(
        self,
    ) -> None:
        self.assertEqual(MAX_LLM_DOCUMENT_CHUNK_NODES, 16)
        self.assertEqual(MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS, 4)

    def test_document_run_packs_adjacent_clauses_into_one_semantic_chunk(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _DocumentProvisionProvider()
        located_scope = locate_llm_extraction_scope(source, tree, locator)

        result = extract_requirement_document_with_provider(
            source,
            tree,
            provider,
            located_scope,
            image_loader=lambda _pages: (),
        )

        self.assertIsInstance(result, LlmRequirementDocumentResult)
        self.assertIs(result.status, LlmRequirementBatchStatus.COMPLETE)
        self.assertEqual(len(result.chunks), 1)
        self.assertEqual(result.provisions, result.chunks)
        self.assertEqual(
            result.chunks[0].scope_structure_node_ids,
            located_scope.scope_structure_node_ids,
        )
        self.assertEqual(provider.classification_call_count, 1)
        self.assertEqual(provider.assembly_call_count, 1)
        with self.assertRaisesRegex(ValueError, "runtime identity"):
            replace(result, runtime_version="llm-requirement-document-runtime/v1")

    def test_document_run_emits_text_free_planning_and_chunk_progress(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _DocumentProvisionProvider()
        located_scope = locate_llm_extraction_scope(source, tree, locator)
        progress: list[LlmRequirementDocumentProgress] = []

        result = extract_requirement_document_with_provider(
            source,
            tree,
            provider,
            located_scope,
            image_loader=lambda _pages: (),
            progress_callback=progress.append,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.COMPLETE)
        self.assertEqual(
            [item.stage for item in progress],
            ["planned", "running", "running", "completed"],
        )
        self.assertEqual(progress[0].total_chunks, 1)
        self.assertEqual(progress[0].completed_chunks, 0)
        self.assertEqual(progress[1].current_chunk_index, 1)
        self.assertGreater(progress[1].current_chunk_target_count, 0)
        self.assertEqual(progress[2].completed_chunks, 1)
        self.assertEqual(progress[2].successful_chunks, 1)
        self.assertEqual(progress[-1].completed_targets, progress[-1].total_targets)
        self.assertIsNone(progress[-1].current_chunk_index)

    def test_document_concurrency_four_overlaps_and_reverse_completion_restores_source_order(
        self,
    ) -> None:
        source, tree, locator = _many_provision_input(4)
        reverse_completion_order = tuple(f"4.12.{index}" for index in range(4, 0, -1))
        provider = _ConcurrentDocumentProvisionProvider(
            completion_order=reverse_completion_order
        )
        located_scope = locate_llm_extraction_scope(source, tree, locator)
        progress: list[LlmRequirementDocumentProgress] = []
        checkpoints: list[LlmRequirementDocumentCheckpoint] = []

        result = extract_requirement_document_with_provider(
            source,
            tree,
            provider,
            located_scope,
            image_loader=lambda _pages: (),
            max_chunk_nodes=2,
            max_concurrent_chunks=4,
            progress_callback=progress.append,
            checkpoint_callback=checkpoints.append,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.COMPLETE)
        self.assertEqual(len(result.chunks), 4)
        self.assertEqual(provider.max_concurrent_classifications, 4)
        self.assertEqual(
            tuple(provider.classification_completion_labels),
            reverse_completion_order,
        )
        self.assertEqual(
            tuple(item.root_section_structure_node_id for item in result.chunks),
            located_scope.root_section_structure_node_ids,
        )
        self.assertEqual(
            tuple(item.source_order for item in result.chunks),
            tuple(sorted(item.source_order for item in result.chunks)),
        )
        self.assertEqual(
            tuple(item.completed_chunks for item in progress),
            tuple(sorted(item.completed_chunks for item in progress)),
        )
        self.assertEqual(progress[-1].completed_targets, progress[-1].total_targets)
        self.assertEqual([item.sequence for item in checkpoints], [1, 2, 3, 4])
        self.assertEqual(
            sorted(item.chunk_index for item in checkpoints),
            [1, 2, 3, 4],
        )
        # Checkpoints cover completion, not a strict wall-clock ordering
        # contract: FIRST_COMPLETED may return several already-finished futures
        # and the application then emits that ready set deterministically.
        self.assertEqual(
            [item.progress.completed_chunks for item in checkpoints],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            tuple(
                sorted(
                    (item.outcome for item in checkpoints),
                    key=lambda outcome: outcome.source_order,
                )
            ),
            result.chunks,
        )

    def test_codex_document_session_uses_one_whole_document_execution_boundary(
        self,
    ) -> None:
        source, tree, locator = _many_provision_input(4)
        effective = get_base_effective_structure(source, tree)
        provider = _PreparedCodexDocumentProvider()
        located_scope = locate_llm_extraction_scope(source, effective, locator)
        progress: list[LlmRequirementDocumentProgress] = []
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (1).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
        )

        def image_loader(pages):  # type: ignore[no-untyped-def]
            return tuple(
                SemanticImageInput(
                    source_sha256=source.source_sha256,
                    page_number=page,
                    width_px=1,
                    height_px=1,
                    image_bytes=png,
                    image_sha256=hashlib.sha256(png).hexdigest(),
                )
                for page in pages
            )

        with mock.patch(
            "app.application.extraction.llm_provision_batch."
            "_extract_prepared_document_chunks_concurrently",
            side_effect=AssertionError("Codex document session must stay ordered"),
        ) as concurrent:
            result = extract_requirement_document_with_provider(
                source,
                effective,
                provider,
                located_scope,
                image_loader=image_loader,
                max_chunk_nodes=2,
                max_concurrent_chunks=4,
                progress_callback=progress.append,
            )

        self.assertEqual(len(result.chunks), 1)
        self.assertEqual(
            len(result.chunks[0].scope_structure_node_ids),
            len(located_scope.scope_structure_node_ids),
        )
        self.assertTrue(
            any(0 < item.completed_targets < item.total_targets for item in progress)
        )
        self.assertEqual(progress[-1].completed_targets, progress[-1].total_targets)
        self.assertEqual(provider.prepared_binding[0], source.source_ir_id)
        self.assertEqual(provider.prepared_binding[1], effective.effective_snapshot_id)
        self.assertEqual(
            tuple(item.page_number for item in provider.prepared_images),
            (1,),
        )
        self.assertEqual(len(result.parent_boundary_results), 1)
        self.assertEqual(
            result.parent_boundary_results[0].validation_receipt.provision_count,
            1,
        )
        self.assertTrue(result.chunks[0].extraction.atomization_results)
        concurrent.assert_not_called()

    def test_document_concurrency_process_limit_is_shared_across_two_tasks(
        self,
    ) -> None:
        probe = _ConcurrentClassificationProbe(
            MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS,
            hold_seconds=0.05,
        )
        start_barrier = Barrier(2)
        runs = []
        for _ in range(2):
            source, tree, locator = _many_provision_input(4)
            runs.append(
                (
                    source,
                    tree,
                    locate_llm_extraction_scope(source, tree, locator),
                    _ConcurrentDocumentProvisionProvider(probe=probe),
                )
            )

        def run_document(index: int) -> LlmRequirementDocumentResult:
            source, tree, located_scope, provider = runs[index]
            try:
                start_barrier.wait(timeout=5)
            except BrokenBarrierError as exc:
                raise AssertionError(
                    "the two document tasks did not start together"
                ) from exc
            return extract_requirement_document_with_provider(
                source,
                tree,
                provider,
                located_scope,
                image_loader=lambda _pages: (),
                max_chunk_nodes=2,
                max_concurrent_chunks=4,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(executor.submit(run_document, index) for index in range(2))
            results = tuple(future.result(timeout=20) for future in futures)

        self.assertTrue(
            all(
                result.status is LlmRequirementBatchStatus.COMPLETE
                for result in results
            )
        )
        self.assertEqual(probe.max_active, MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS)
        self.assertEqual(
            sum(run[3].classification_call_count for run in runs),
            8,
        )

    def test_document_concurrency_emits_progress_heartbeat_while_waiting(
        self,
    ) -> None:
        source, tree, locator = _many_provision_input(4)
        provider = _ConcurrentDocumentProvisionProvider()
        located_scope = locate_llm_extraction_scope(source, tree, locator)
        progress: list[LlmRequirementDocumentProgress] = []
        wait_timeouts: list[float | None] = []
        first_wait = True

        def wait_with_initial_heartbeat(futures, *, timeout=None, return_when=None):
            nonlocal first_wait
            wait_timeouts.append(timeout)
            if first_wait:
                first_wait = False
                return set(), set(futures)
            return futures_wait(
                futures,
                timeout=timeout,
                return_when=return_when,
            )

        with mock.patch(
            "app.application.extraction.llm_provision_batch.wait",
            side_effect=wait_with_initial_heartbeat,
        ):
            result = extract_requirement_document_with_provider(
                source,
                tree,
                provider,
                located_scope,
                image_loader=lambda _pages: (),
                max_chunk_nodes=2,
                max_concurrent_chunks=4,
                progress_callback=progress.append,
            )

        self.assertIs(result.status, LlmRequirementBatchStatus.COMPLETE)
        self.assertEqual(wait_timeouts[0], 15.0)
        self.assertGreaterEqual(
            sum(
                item.stage == "running" and item.completed_chunks == 0
                for item in progress
            ),
            2,
        )
        self.assertEqual(progress[-1].completed_targets, progress[-1].total_targets)

    def test_document_concurrency_rejects_more_than_process_limit(self) -> None:
        source, tree, locator = _many_provision_input(5)
        located_scope = locate_llm_extraction_scope(source, tree, locator)

        with self.assertRaisesRegex(
            ValueError,
            "max_concurrent_chunks must be an integer between 1 and 4",
        ):
            extract_requirement_document_with_provider(
                source,
                tree,
                _DocumentProvisionProvider(),
                located_scope,
                image_loader=lambda _pages: (),
                max_concurrent_chunks=5,
            )

    def test_document_concurrency_stops_submitting_after_progress_cancellation(
        self,
    ) -> None:
        source, tree, locator = _many_provision_input(5)
        provider = _ConcurrentDocumentProvisionProvider()
        located_scope = locate_llm_extraction_scope(source, tree, locator)

        def cancel_after_first(
            progress: LlmRequirementDocumentProgress,
        ) -> None:
            if progress.completed_chunks == 1:
                raise RuntimeError("synthetic cancellation")

        with self.assertRaisesRegex(RuntimeError, "synthetic cancellation"):
            extract_requirement_document_with_provider(
                source,
                tree,
                provider,
                located_scope,
                image_loader=lambda _pages: (),
                max_chunk_nodes=2,
                max_concurrent_chunks=4,
                progress_callback=cancel_after_first,
            )

        self.assertEqual(provider.classification_call_count, 4)

    def test_document_preflight_terminally_receipts_only_rejected_units(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _DocumentProvisionProvider(preflight_block_labels={"4.12.1"})
        located_scope = locate_llm_extraction_scope(source, tree, locator)

        result = extract_requirement_document_with_provider(
            source,
            tree,
            provider,
            located_scope,
            image_loader=lambda _pages: (),
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.UNRESOLVED)
        self.assertEqual(len(result.chunks), 1)
        self.assertIs(
            result.chunks[0].status,
            LlmProvisionExtractionStatus.UNRESOLVED,
        )
        self.assertEqual(
            result.chunks[0].error_code,
            SemanticProviderRequestRejectionCode.EVIDENCE_BUDGET_EXCEEDED.value,
        )
        extraction = result.chunks[0].extraction
        self.assertIsNotNone(extraction)
        assert extraction is not None
        self.assertEqual(
            sum(
                receipt.status.value == "UNRESOLVED"
                for receipt in extraction.semantic_terminal_receipts
            ),
            2,
        )
        self.assertEqual(
            sum(
                receipt.status.value == "RESOLVED"
                for receipt in extraction.semantic_terminal_receipts
            ),
            4,
        )
        self.assertEqual(provider.classification_call_count, 2)
        self.assertEqual(provider.assembly_call_count, 1)

    def test_document_run_partitions_complete_non_section_root_branches(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.PARAGRAPH, "First root branch."),
                (SourceNodeKind.PARAGRAPH, "Second root branch."),
                (SourceNodeKind.PARAGRAPH, "Third root branch."),
            )
        )
        tree = build_structure_tree(source)
        located_scope = locate_llm_document_scope(source, tree)
        provider = _RootBranchDocumentProvider()

        result = extract_requirement_document_with_provider(
            source,
            tree,
            provider,
            located_scope,
            image_loader=lambda _pages: (),
            max_chunk_nodes=1,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.COMPLETE)
        self.assertEqual(len(result.chunks), 3)
        self.assertEqual(
            tuple(item.root_section_structure_node_id for item in result.chunks),
            located_scope.root_section_structure_node_ids,
        )
        self.assertEqual(
            tuple(
                target_id
                for item in result.chunks
                for target_id in item.scope_structure_node_ids
            ),
            located_scope.scope_structure_node_ids,
        )
        self.assertTrue(
            all(
                item.scope_structure_node_ids == (item.root_section_structure_node_id,)
                for item in result.chunks
            )
        )
        self.assertEqual(provider.classification_call_count, 3)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_complete_batch_keeps_independent_snapshots_and_ordered_refs(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider()

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.COMPLETE)
        self.assertEqual(result.successful_provision_count, 3)
        self.assertEqual(result.unresolved_provision_count, 0)
        self.assertEqual(provider.classification_call_count, 3)
        self.assertEqual(provider.assembly_call_count, 3)
        self.assertEqual(
            [item.status for item in result.provisions],
            [LlmProvisionExtractionStatus.SUCCESS] * 3,
        )
        self.assertTrue(all(item.extraction is not None for item in result.provisions))
        for item in result.provisions:
            assert item.extraction is not None
            self.assertEqual(
                item.extraction.semantics.scope_structure_node_ids,
                item.scope_structure_node_ids,
            )
            self.assertEqual(
                item.extraction.assembly.scope_structure_node_ids,
                item.scope_structure_node_ids,
            )
            self.assertIsNone(item.extraction.requirement_view.requirement_set_id)
        self.assertEqual(len(result.requirement_view_node_refs), 6)
        self.assertEqual(
            tuple(item.source_order for item in result.requirement_view_node_refs),
            tuple(
                sorted(item.source_order for item in result.requirement_view_node_refs)
            ),
        )
        self.assertEqual(
            len(
                {
                    item.requirement_view_snapshot_id
                    for item in result.requirement_view_node_refs
                }
            ),
            3,
        )
        self.assertFalse(hasattr(result, "semantics"))
        self.assertFalse(hasattr(result, "assembly"))
        self.assertFalse(hasattr(result, "requirement_view"))

    def test_one_timeout_yields_partial_batch_and_other_provisions_continue(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(timeout_labels={"4.12.2"})

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.PARTIAL)
        self.assertEqual(
            [item.status for item in result.provisions],
            [
                LlmProvisionExtractionStatus.SUCCESS,
                LlmProvisionExtractionStatus.UNRESOLVED,
                LlmProvisionExtractionStatus.SUCCESS,
            ],
        )
        self.assertEqual(result.successful_provision_count, 2)
        self.assertEqual(result.unresolved_provision_count, 1)
        self.assertEqual(provider.classification_call_count, 3)
        self.assertEqual(provider.assembly_call_count, 2)
        failed = result.provisions[1]
        self.assertEqual(failed.error_code, "provider_timeout")
        self.assertIsNotNone(failed.extraction)
        assert failed.extraction is not None
        self.assertFalse(failed.extraction.requirement_view.nodes)
        self.assertEqual(
            {
                item.root_section_structure_node_id
                for item in result.requirement_view_node_refs
            },
            {
                result.provisions[0].root_section_structure_node_id,
                result.provisions[2].root_section_structure_node_id,
            },
        )

    def test_one_assembly_timeout_yields_partial_and_later_provisions_continue(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(assembly_timeout_labels={"4.12.2"})

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.PARTIAL)
        self.assertEqual(
            [item.status for item in result.provisions],
            [
                LlmProvisionExtractionStatus.SUCCESS,
                LlmProvisionExtractionStatus.UNRESOLVED,
                LlmProvisionExtractionStatus.SUCCESS,
            ],
        )
        self.assertEqual(result.provisions[1].error_code, "provider_timeout")
        self.assertEqual(provider.classification_call_count, 3)
        self.assertEqual(provider.assembly_call_count, 3)

    def test_unexpected_provider_exception_is_not_hidden_as_unresolved(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(raise_labels={"4.12.2"})

        with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
            extract_requirement_provisions_with_provider(
                source,
                tree,
                provider,
                locator,
            )

        self.assertEqual(provider.classification_call_count, 2)
        self.assertEqual(provider.assembly_call_count, 1)

    def test_selector_order_does_not_change_provision_or_view_ref_order(self) -> None:
        source, tree, locator = _three_provision_input()
        reversed_locator = LlmExtractionScopeLocator(
            source_sha256=source.source_sha256,
            section_selectors=tuple(reversed(locator.section_selectors)),
        )

        first = extract_requirement_provisions_with_provider(
            source,
            tree,
            _ProvisionProvider(),
            locator,
        )
        second = extract_requirement_provisions_with_provider(
            source,
            tree,
            _ProvisionProvider(),
            reversed_locator,
        )

        self.assertEqual(
            first.located_scope.root_section_structure_node_ids,
            second.located_scope.root_section_structure_node_ids,
        )
        self.assertEqual(
            tuple(item.scope_structure_node_ids for item in first.provisions),
            tuple(item.scope_structure_node_ids for item in second.provisions),
        )
        self.assertEqual(
            first.requirement_view_node_refs,
            second.requirement_view_node_refs,
        )

    def test_all_timeouts_are_explicitly_unresolved(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(timeout_labels={"4.12.1", "4.12.2", "4.12.3"})

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.UNRESOLVED)
        self.assertEqual(result.successful_provision_count, 0)
        self.assertEqual(result.unresolved_provision_count, 3)
        self.assertFalse(result.requirement_view_node_refs)
        self.assertEqual(provider.classification_call_count, 3)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_successful_all_excluded_provisions_allow_an_empty_merge(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(exclude_all=True)

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.COMPLETE)
        self.assertEqual(result.successful_provision_count, 3)
        self.assertFalse(result.requirement_view_node_refs)
        self.assertTrue(
            all(
                item.extraction is not None
                and not item.extraction.requirement_view.nodes
                for item in result.provisions
            )
        )

    def test_provider_identity_drift_cannot_enter_merged_refs(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(drift_labels={"4.12.2"})

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.PARTIAL)
        drifted = result.provisions[1]
        self.assertIs(drifted.status, LlmProvisionExtractionStatus.UNRESOLVED)
        self.assertEqual(
            drifted.error_code,
            "provision_result_binding_invalid",
        )
        self.assertNotIn(
            drifted.root_section_structure_node_id,
            {
                item.root_section_structure_node_id
                for item in result.requirement_view_node_refs
            },
        )

    def test_provider_identity_is_locked_once_for_the_entire_batch(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(mutate_after_labels={"4.12.1"})

        with self.assertRaises(LlmProvisionBatchRejected) as caught:
            extract_requirement_provisions_with_provider(
                source,
                tree,
                provider,
                locator,
            )

        self.assertIs(
            caught.exception.code,
            LlmProvisionBatchRejectionCode.PROVIDER_IDENTITY_MUTATED,
        )
        self.assertEqual(provider.classification_call_count, 1)
        self.assertEqual(provider.assembly_call_count, 1)

    def test_provider_identity_mutation_after_classification_stops_before_assembly(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(mutate_after_classification_labels={"4.12.1"})

        with self.assertRaises(LlmProvisionBatchRejected) as caught:
            extract_requirement_provisions_with_provider(
                source,
                tree,
                provider,
                locator,
            )

        self.assertIs(
            caught.exception.code,
            LlmProvisionBatchRejectionCode.PROVIDER_IDENTITY_MUTATED,
        )
        self.assertEqual(provider.classification_call_count, 1)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_known_provider_preflight_rejection_receipts_only_that_provision(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(preflight_block_labels={"4.12.2"})

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.PARTIAL)
        self.assertEqual(
            [item.status for item in result.provisions],
            [
                LlmProvisionExtractionStatus.SUCCESS,
                LlmProvisionExtractionStatus.UNRESOLVED,
                LlmProvisionExtractionStatus.SUCCESS,
            ],
        )
        self.assertEqual(
            result.provisions[1].error_code,
            SemanticProviderRequestRejectionCode.EVIDENCE_BUDGET_EXCEEDED.value,
        )
        blocked_extraction = result.provisions[1].extraction
        self.assertIsNotNone(blocked_extraction)
        assert blocked_extraction is not None
        self.assertTrue(
            all(
                receipt.status.value == "UNRESOLVED"
                for receipt in blocked_extraction.semantic_terminal_receipts
            )
        )
        self.assertTrue(
            all(
                run.status is SemanticProviderRunStatus.SKIPPED
                and run.request_count == 0
                for run in blocked_extraction.provider_runs
            )
        )
        self.assertEqual(
            set(provider.preflight_labels),
            {"4.12.1", "4.12.2", "4.12.3"},
        )
        self.assertEqual(provider.classification_call_count, 2)
        self.assertEqual(provider.assembly_call_count, 2)

    def test_known_assembly_preflight_rejection_receipts_only_that_provision(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(assembly_preflight_block_labels={"4.12.2"})

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.PARTIAL)
        self.assertEqual(
            [item.status for item in result.provisions],
            [
                LlmProvisionExtractionStatus.SUCCESS,
                LlmProvisionExtractionStatus.UNRESOLVED,
                LlmProvisionExtractionStatus.SUCCESS,
            ],
        )
        self.assertEqual(
            result.provisions[1].error_code,
            SemanticProviderRequestRejectionCode.WIRE_BUDGET_EXCEEDED.value,
        )
        blocked_extraction = result.provisions[1].extraction
        self.assertIsNotNone(blocked_extraction)
        assert blocked_extraction is not None
        self.assertTrue(
            all(
                receipt.status.value == "RESOLVED"
                for receipt in blocked_extraction.semantic_terminal_receipts
            )
        )
        self.assertTrue(
            any(
                run.operation == "assemble_requirements"
                and run.status is SemanticProviderRunStatus.SKIPPED
                and run.request_count == 0
                and run.error_code
                == SemanticProviderRequestRejectionCode.WIRE_BUDGET_EXCEEDED.value
                for run in blocked_extraction.provider_runs
            )
        )
        self.assertEqual(
            set(provider.assembly_preflight_labels),
            {"4.12.1", "4.12.2", "4.12.3"},
        )
        self.assertEqual(provider.classification_call_count, 3)
        self.assertEqual(provider.assembly_call_count, 2)

    def test_late_typed_assembly_rejection_is_receipted_and_batch_continues(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(assembly_runtime_block_labels={"4.12.2"})

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.PARTIAL)
        self.assertEqual(
            [item.status for item in result.provisions],
            [
                LlmProvisionExtractionStatus.SUCCESS,
                LlmProvisionExtractionStatus.UNRESOLVED,
                LlmProvisionExtractionStatus.SUCCESS,
            ],
        )
        self.assertEqual(provider.classification_call_count, 3)
        self.assertGreater(provider.assembly_call_count, 3)
        blocked_extraction = result.provisions[1].extraction
        self.assertIsNotNone(blocked_extraction)
        assert blocked_extraction is not None
        self.assertTrue(
            any(
                run.operation == "assemble_requirements"
                and run.status is SemanticProviderRunStatus.SKIPPED
                and run.request_count == 0
                for run in blocked_extraction.provider_runs
            )
        )

    def test_batch_requires_mandatory_provider_preflight_capability(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _FakeProvider()

        with self.assertRaisesRegex(TypeError, "PreflightSemanticProviderPort"):
            extract_requirement_provisions_with_provider(
                source,
                tree,
                provider,  # type: ignore[arg-type]
                locator,
            )

        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_provision_count_budget_rejects_before_provider_preflight(self) -> None:
        source, tree, locator = _many_provision_input(MAX_LLM_PROVISIONS_PER_BATCH + 1)
        provider = _ProvisionProvider()

        with self.assertRaises(LlmProvisionBatchRejected) as caught:
            extract_requirement_provisions_with_provider(
                source,
                tree,
                provider,
                locator,
            )

        self.assertIs(
            caught.exception.code,
            LlmProvisionBatchRejectionCode.PROVISION_BUDGET_EXCEEDED,
        )
        self.assertFalse(provider.preflight_labels)
        self.assertFalse(provider.assembly_preflight_labels)
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_node_budget_splits_oversized_provision_on_source_bound_units(
        self,
    ) -> None:
        source, tree, locator = _node_budget_input()
        provider = _ProvisionProvider()

        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        self.assertIs(result.status, LlmRequirementBatchStatus.COMPLETE)
        self.assertEqual(
            [item.status for item in result.provisions],
            [
                LlmProvisionExtractionStatus.SUCCESS,
                LlmProvisionExtractionStatus.SUCCESS,
            ],
        )
        oversized = result.provisions[1].extraction
        self.assertIsNotNone(oversized)
        assert oversized is not None
        self.assertEqual(len(oversized.semantics.annotations), 290)
        oversized_requests = tuple(
            request
            for request in provider.classification_requests
            if _classification_provision_root(request).explicit_label == "4.12.2"
        )
        self.assertEqual(
            sum(
                len(request.scope_structure_node_ids) for request in oversized_requests
            ),
            290,
        )
        self.assertTrue(
            all(
                len(request.scope_structure_node_ids) <= 80
                for request in oversized_requests
            )
        )
        self.assertGreater(
            sum(run.operation == "classify_nodes" for run in oversized.provider_runs),
            1,
        )
        self.assertGreater(
            sum(
                run.operation == "assemble_requirements"
                for run in oversized.provider_runs
            ),
            1,
        )
        self.assertEqual(
            len(oversized.semantic_terminal_receipts),
            len(oversized.semantics.annotations),
        )
        self.assertTrue(
            all(
                receipt.status.value == "RESOLVED"
                for receipt in oversized.semantic_terminal_receipts
            )
        )
        self.assertEqual(set(provider.preflight_labels), {"4.12.1", "4.12.2"})
        self.assertGreater(len(provider.preflight_labels), 2)
        self.assertGreater(provider.classification_call_count, 2)
        self.assertGreater(provider.assembly_call_count, 2)

    def test_oversized_continuation_interval_is_windowed_with_complete_receipts(
        self,
    ) -> None:
        source, _first_table, _second_table, _overlay = _continued_pdf_table_source()
        tree = build_structure_tree(source)
        provider = _FakeProvider(parameter_rows=True)
        continued_ids = {
            target_id
            for relation in tree.relations
            if relation.kind is StructureRelationKind.CONTINUES
            for target_id in (
                relation.source_structure_node_id,
                relation.target_structure_node_id,
            )
        }
        nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
        continued_orders = {
            nodes_by_id[target_id].source_order for target_id in continued_ids
        }
        interval_ids = {
            node.structure_node_id
            for node in tree.nodes
            if min(continued_orders) <= node.source_order <= max(continued_orders)
        }

        classified = classify_content_with_provider(
            source,
            tree,
            provider,
            max_nodes_per_request=2,
        )

        receipts_by_owner = {
            receipt.owner_structure_node_id: receipt
            for receipt in classified.terminal_receipts
        }
        self.assertTrue(continued_ids.issubset(interval_ids))
        self.assertTrue(
            all(
                receipts_by_owner[target_id].status.value == "RESOLVED"
                for target_id in interval_ids
            )
        )
        peer_ids = set(receipts_by_owner).difference(interval_ids)
        self.assertTrue(peer_ids)
        self.assertTrue(
            all(
                receipts_by_owner[target_id].status.value == "RESOLVED"
                for target_id in peer_ids
            )
        )
        remotely_requested_ids = {
            target_id
            for request in provider.classification_requests
            for target_id in request.scope_structure_node_ids
        }
        self.assertEqual(remotely_requested_ids, set(receipts_by_owner))
        self.assertTrue(
            all(
                len(request.scope_structure_node_ids) <= 2
                for request in provider.classification_requests
            )
        )
        self.assertEqual(
            tuple(receipts_by_owner),
            classified.semantics.scope_structure_node_ids,
        )

    def test_public_result_statuses_cannot_be_replaced_inconsistently(self) -> None:
        source, tree, locator = _three_provision_input()
        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            _ProvisionProvider(timeout_labels={"4.12.2"}),
            locator,
        )

        with self.assertRaises(ValueError):
            replace(
                result.provisions[1],
                status=LlmProvisionExtractionStatus.SUCCESS,
                error_code=None,
            )
        with self.assertRaises(ValueError):
            replace(result, status=LlmRequirementBatchStatus.COMPLETE)

    def test_public_outcome_scope_cannot_be_detached_from_its_extraction(self) -> None:
        source, tree, locator = _three_provision_input()
        result = extract_requirement_provisions_with_provider(
            source,
            tree,
            _ProvisionProvider(),
            locator,
        )
        first, second = result.provisions[:2]

        with self.assertRaisesRegex(ValueError, "scope does not match"):
            replace(
                first,
                scope_structure_node_ids=(
                    first.root_section_structure_node_id,
                    *second.scope_structure_node_ids[1:],
                ),
            )

    def test_locator_failure_stops_before_any_provider_call(self) -> None:
        source, tree, _locator = _three_provision_input()
        provider = _ProvisionProvider()
        missing = LlmExtractionScopeLocator(
            source_sha256=source.source_sha256,
            section_selectors=(LlmSectionScopeSelector(("4", "4.99", "4.99.1")),),
        )

        with self.assertRaises(LlmScopeLocationRejected):
            extract_requirement_provisions_with_provider(
                source,
                tree,
                provider,
                missing,
            )

        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_parent_and_child_provisions_are_rejected_before_provider_calls(
        self,
    ) -> None:
        source, tree, _locator = _three_provision_input()
        provider = _ProvisionProvider()
        nested = LlmExtractionScopeLocator(
            source_sha256=source.source_sha256,
            section_selectors=(
                LlmSectionScopeSelector(("4", "4.12")),
                LlmSectionScopeSelector(("4", "4.12", "4.12.1")),
            ),
        )

        with self.assertRaises(LlmProvisionBatchRejected) as caught:
            extract_requirement_provisions_with_provider(
                source,
                tree,
                provider,
                nested,
            )

        self.assertIs(
            caught.exception.code,
            LlmProvisionBatchRejectionCode.CROSS_PROVISION_PARENT,
        )
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_continuation_cannot_cross_two_selected_provisions(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider()
        roots = {
            item.explicit_label: item
            for item in tree.nodes
            if item.explicit_label in {"4.12.1", "4.12.2"}
        }
        first_body = next(
            item
            for item in tree.nodes
            if item.parent_structure_node_id == roots["4.12.1"].structure_node_id
            and item.kind is StructureNodeKind.CONTENT
        )
        second_body = next(
            item
            for item in tree.nodes
            if item.parent_structure_node_id == roots["4.12.2"].structure_node_id
            and item.kind is StructureNodeKind.CONTENT
        )
        continuation = StructureRelation(
            relation_id=stable_structure_relation_id(
                source_ir_id=tree.source_ir_id,
                resolver_id=tree.resolver_id,
                resolver_version=tree.resolver_version,
                kind=StructureRelationKind.CONTINUES,
                source_structure_node_id=first_body.structure_node_id,
                target_structure_node_id=second_body.structure_node_id,
                source_refs=(*first_body.source_refs, *second_body.source_refs),
            ),
            kind=StructureRelationKind.CONTINUES,
            source_structure_node_id=first_body.structure_node_id,
            target_structure_node_id=second_body.structure_node_id,
            source_refs=(*first_body.source_refs, *second_body.source_refs),
        )
        tampered_tree = tree.model_copy(
            update={"relations": (*tree.relations, continuation)}
        )

        with (
            mock.patch(
                "app.application.extraction.locate_llm_scope."
                "validate_structure_tree_against_source_ir"
            ),
            mock.patch(
                "app.application.extraction.llm_provision_batch."
                "validate_structure_tree_against_source_ir"
            ),
            self.assertRaises(LlmProvisionBatchRejected) as caught,
        ):
            extract_requirement_provisions_with_provider(
                source,
                tampered_tree,
                provider,
                locator,
            )

        self.assertIs(
            caught.exception.code,
            LlmProvisionBatchRejectionCode.CROSS_PROVISION_CONTINUATION,
        )
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_duplicate_provider_run_ids_fail_closed(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider(reuse_run_ids=True)

        with self.assertRaises(LlmProvisionBatchRejected) as caught:
            extract_requirement_provisions_with_provider(
                source,
                tree,
                provider,
                locator,
            )

        self.assertIs(
            caught.exception.code,
            LlmProvisionBatchRejectionCode.DUPLICATE_PROVIDER_RUN,
        )


def _classification_provision_root(request):  # type: ignore[no-untyped-def]
    first_target = request.nodes[0]
    candidates = tuple(
        item
        for item in (*request.context_nodes, *request.nodes)
        if item.kind is StructureNodeKind.SECTION
        and (
            item.target_structure_node_id == first_target.target_structure_node_id
            or item.target_structure_node_id in first_target.ancestor_structure_node_ids
        )
    )
    if not candidates:
        raise AssertionError("classification request requires provision context")
    return max(candidates, key=lambda item: len(item.ancestor_structure_node_ids))


class _ProvisionProvider(_FakeProvider):
    def __init__(
        self,
        *,
        timeout_labels: set[str] | None = None,
        assembly_timeout_labels: set[str] | None = None,
        raise_labels: set[str] | None = None,
        drift_labels: set[str] | None = None,
        mutate_after_classification_labels: set[str] | None = None,
        mutate_after_labels: set[str] | None = None,
        preflight_block_labels: set[str] | None = None,
        assembly_preflight_block_labels: set[str] | None = None,
        assembly_runtime_block_labels: set[str] | None = None,
        exclude_all: bool = False,
        reuse_run_ids: bool = False,
        mark_content_continuation: bool = False,
        schema_invalid_content_labels: set[str] | None = None,
    ) -> None:
        super().__init__(mark_content_continuation=mark_content_continuation)
        self.timeout_labels = timeout_labels or set()
        self.assembly_timeout_labels = assembly_timeout_labels or set()
        self.raise_labels = raise_labels or set()
        self.drift_labels = drift_labels or set()
        self.mutate_after_classification_labels = (
            mutate_after_classification_labels or set()
        )
        self.mutate_after_labels = mutate_after_labels or set()
        self.preflight_block_labels = preflight_block_labels or set()
        self.preflight_labels: list[str] = []
        self.assembly_preflight_block_labels = assembly_preflight_block_labels or set()
        self.assembly_runtime_block_labels = assembly_runtime_block_labels or set()
        self.assembly_preflight_labels: list[str] = []
        self.exclude_all = exclude_all
        self.reuse_run_ids = reuse_run_ids
        self.schema_invalid_content_labels = schema_invalid_content_labels or set()

    def preflight_classification_request(self, request):  # type: ignore[no-untyped-def]
        root = _classification_provision_root(request)
        assert root.explicit_label is not None
        self.preflight_labels.append(root.explicit_label)
        if root.explicit_label in self.preflight_block_labels:
            raise SemanticProviderRequestRejected(
                SemanticProviderRequestRejectionCode.EVIDENCE_BUDGET_EXCEEDED,
                "synthetic provider request budget",
            )

    def preflight_assembly_request(self, request):  # type: ignore[no-untyped-def]
        root = next(
            item for item in request.nodes if item.kind is StructureNodeKind.SECTION
        )
        assert root.explicit_label is not None
        self.assembly_preflight_labels.append(root.explicit_label)
        if root.explicit_label in self.assembly_preflight_block_labels:
            raise SemanticProviderRequestRejected(
                SemanticProviderRequestRejectionCode.WIRE_BUDGET_EXCEEDED,
                "synthetic assembly request budget",
            )

    def classify_nodes(self, request):  # type: ignore[no-untyped-def]
        self.classification_call_count += 1
        self.classification_requests.append(request)
        root = _classification_provision_root(request)
        if root.explicit_label in self.raise_labels:
            raise RuntimeError("synthetic provider failure")
        if root.explicit_label in self.timeout_labels:
            return SemanticClassificationProviderResult(
                decisions=(),
                run=_run(
                    "classify_nodes",
                    SemanticProviderRunStatus.TIMEOUT,
                    nonce=self.classification_call_count,
                ),
            )
        if root.explicit_label in self.schema_invalid_content_labels and any(
            node.kind is StructureNodeKind.CONTENT for node in request.nodes
        ):
            run = _run(
                "classify_nodes",
                SemanticProviderRunStatus.INVALID_OUTPUT,
                nonce=self.classification_call_count,
            ).model_copy(
                update={
                    "provider_version": self.provider_version,
                    "error_code": "provider_output_schema_invalid",
                }
            )
            return SemanticClassificationProviderResult(decisions=(), run=run)

        run = _run(
            "classify_nodes",
            SemanticProviderRunStatus.SUCCESS,
            nonce=(1 if self.reuse_run_ids else self.classification_call_count),
        )
        run = run.model_copy(update={"provider_version": self.provider_version})
        decisions = []
        for node in request.nodes:
            if node.kind is StructureNodeKind.SECTION:
                semantic_class = SemanticClass.REQUIREMENT
                subtype = RequirementSubtype.PRODUCT_REQUIREMENT
                evidence_items = node.evidence[-1:]
            elif (
                self.mark_content_continuation
                and node.kind is StructureNodeKind.CONTENT
            ):
                semantic_class = SemanticClass.REQUIREMENT
                subtype = RequirementSubtype.PRODUCT_REQUIREMENT
                evidence_items = node.evidence[:1]
            else:
                semantic_class = SemanticClass.TECHNICAL_INFORMATION
                subtype = None
                evidence_items = node.evidence[:1]
            decisions.append(
                SemanticDecision(
                    target_structure_node_id=node.target_structure_node_id,
                    semantic_class=semantic_class,
                    requirement_subtype=subtype,
                    is_continuation_fragment=(
                        self.mark_content_continuation
                        and node.kind is StructureNodeKind.CONTENT
                    ),
                    evidence_source_refs=tuple(
                        item.source_ref for item in evidence_items
                    ),
                    reason_codes=("synthetic_model",),
                )
            )
        result = SemanticClassificationProviderResult(
            decisions=tuple(decisions),
            run=run,
        )
        if root.explicit_label in self.mutate_after_classification_labels:
            self.provider_version = "mutated-provider-version"
        return result

    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        self.assembly_call_count += 1
        primary = next(
            item
            for item in request.nodes
            if item.semantic_class is SemanticClass.REQUIREMENT
        )
        if primary.explicit_label in self.assembly_runtime_block_labels:
            raise SemanticProviderRequestRejected(
                SemanticProviderRequestRejectionCode.WIRE_BUDGET_EXCEEDED,
                "synthetic late assembly request budget",
            )
        if primary.explicit_label in self.assembly_timeout_labels:
            run = _run(
                "assemble_requirements",
                SemanticProviderRunStatus.TIMEOUT,
                nonce=self.assembly_call_count,
            )
            run = run.model_copy(update={"provider_version": self.provider_version})
            return RequirementAssemblyProviderResult(decisions=(), run=run)
        run = _run(
            "assemble_requirements",
            SemanticProviderRunStatus.SUCCESS,
            nonce=(1 if self.reuse_run_ids else self.assembly_call_count),
        )
        run = run.model_copy(update={"provider_version": self.provider_version})
        if primary.explicit_label in self.drift_labels:
            run = run.model_copy(update={"provider_version": "drifted-version"})
        decisions = tuple(
            RequirementAssemblyDecision(
                target_structure_node_id=node.target_structure_node_id,
                include_in_view=(
                    not self.exclude_all and not node.is_continuation_fragment
                ),
                role=(
                    None
                    if self.exclude_all or node.is_continuation_fragment
                    else RequirementProjectionRole.PRIMARY
                    if node.target_structure_node_id == primary.target_structure_node_id
                    else RequirementProjectionRole.CONTENT
                ),
                parent_structure_node_id=(
                    None
                    if self.exclude_all
                    or node.is_continuation_fragment
                    or node.target_structure_node_id == primary.target_structure_node_id
                    else primary.target_structure_node_id
                ),
                status=RequirementAssemblyStatus.CANDIDATE,
                reason_codes=("synthetic_assembly",),
            )
            for node in request.nodes
        )
        result = RequirementAssemblyProviderResult(decisions=decisions, run=run)
        if primary.explicit_label in self.mutate_after_labels:
            self.provider_version = "mutated-provider-version"
        return result


class _DocumentProvisionProvider(_ProvisionProvider):
    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        self.assembly_call_count += 1
        run = _run(
            "assemble_requirements",
            SemanticProviderRunStatus.SUCCESS,
            nonce=self.assembly_call_count,
        ).model_copy(update={"provider_version": self.provider_version})
        current_primary = None
        decisions = []
        for node in request.nodes:
            if node.semantic_class is SemanticClass.REQUIREMENT:
                current_primary = node.target_structure_node_id
                role = RequirementProjectionRole.PRIMARY
                parent_id = None
                decision_mode_status = RequirementAssemblyStatus.CANDIDATE
            else:
                role = RequirementProjectionRole.CONTENT
                parent_id = current_primary
                decision_mode_status = RequirementAssemblyStatus.CANDIDATE
            decisions.append(
                RequirementAssemblyDecision(
                    target_structure_node_id=node.target_structure_node_id,
                    include_in_view=current_primary is not None,
                    role=role if current_primary is not None else None,
                    parent_structure_node_id=parent_id,
                    status=decision_mode_status,
                    reason_codes=("synthetic_document_assembly",),
                )
            )
        return RequirementAssemblyProviderResult(decisions=tuple(decisions), run=run)


class _PreparedCodexDocumentProvider(_DocumentProvisionProvider):
    provider_id = "codex-app-server-test-semantic"

    def __init__(self) -> None:
        super().__init__()
        self.prepared_binding: tuple[str, str] = ("", "")
        self.prepared_images: tuple[SemanticImageInput, ...] = ()
        self.atomization_call_count = 0

    def prepare_document_session(
        self,
        *,
        source_ir_id: str,
        structure_snapshot_id: str,
        images: tuple[SemanticImageInput, ...],
    ) -> None:
        self.prepared_binding = (source_ir_id, structure_snapshot_id)
        self.prepared_images = images

    def close_document_session(self) -> None:
        return None

    def identify_parent_boundaries(self, request):  # type: ignore[no-untyped-def]
        reference = NativeParentBoundaryReferenceV1(
            source_sha256=request.source_sha256,
            scope_id=request.scope_id,
            status=NativeParentBoundaryStatus.OBSERVED,
            provisions=(
                NativeProvisionBlockV1(
                    provision_id="native-provision-" + "9" * 32,
                    explicit_label=None,
                    source_order=0,
                    disposition=NativeProvisionDisposition.REQUIREMENT,
                    body_ranges=tuple(
                        RequirementAtomizationRange(
                            segment_index=segment.segment_index,
                            start_offset=0,
                            end_offset=len(segment.source_text),
                        )
                        for segment in request.source_segments
                    ),
                    reason_codes=("complete_parent_provision",),
                ),
            ),
        )
        run = self._bound_run("identify_parent_boundaries", nonce=99)
        return DocumentParentBoundaryProviderResult(
            reference=reference,
            validation_receipt=validate_native_parent_boundary_reference(
                request,
                reference,
            ),
            run=run,
        )

    def atomize(self, request):  # type: ignore[no-untyped-def]
        self.atomization_call_count += 1
        ranges = tuple(
            RequirementAtomizationRange(
                segment_index=segment.segment_index,
                start_offset=0,
                end_offset=len(segment.source_text),
            )
            for segment in request.source_segments
        )
        return RequirementAtomizationProviderResult(
            obligations=(
                RequirementAtomizationObligation(
                    source_ranges=ranges,
                    atomic_statement=" ".join(
                        segment.source_text for segment in request.source_segments
                    ),
                    split_basis="single_or_descriptive",
                    reason_codes=("synthetic_exact_parent",),
                ),
            ),
            run=self._bound_run(
                "atomize_requirement_obligation",
                nonce=100 + self.atomization_call_count,
            ),
        )

    def _bound_run(self, operation: str, *, nonce: int):
        run = _run(
            operation,
            SemanticProviderRunStatus.SUCCESS,
            nonce=nonce,
        ).model_copy(
            update={
                "provider_id": self.provider_id,
                "provider_version": self.provider_version,
                "model_id": self.model_id,
            }
        )
        run = run.model_copy(
            update={
                "provider_run_id": stable_semantic_provider_run_id(
                    operation=run.operation,
                    provider_id=run.provider_id,
                    model_id=run.model_id,
                    request_sha256=run.request_sha256,
                    response_sha256=run.response_sha256,
                    status=run.status,
                    started_at=run.started_at,
                )
            }
        )
        return run


class _RootBranchDocumentProvider(_FakeProvider):
    def preflight_classification_request(self, _request):  # type: ignore[no-untyped-def]
        return None

    def preflight_assembly_request(self, _request):  # type: ignore[no-untyped-def]
        return None


class _ConcurrentClassificationProbe:
    def __init__(self, participants: int, *, hold_seconds: float = 0.0) -> None:
        self._barrier = Barrier(participants)
        self._hold_seconds = hold_seconds
        self._lock = Lock()
        self._active = 0
        self.max_active = 0

    def enter(self) -> None:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            self._barrier.wait(timeout=5)
        except BrokenBarrierError as exc:
            self.exit()
            raise AssertionError(
                "the expected document chunks did not overlap classification"
            ) from exc
        if self._hold_seconds:
            sleep(self._hold_seconds)

    def exit(self) -> None:
        with self._lock:
            self._active -= 1


class _ConcurrentDocumentProvisionProvider(_DocumentProvisionProvider):
    def __init__(
        self,
        *,
        probe: _ConcurrentClassificationProbe | None = None,
        completion_order: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self._probe = probe or _ConcurrentClassificationProbe(
            MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS
        )
        self._result_lock = Lock()
        self._completion_lock = Lock()
        self._completion_turns = {label: Event() for label in completion_order}
        self._next_completion_label = {
            label: (
                completion_order[index + 1]
                if index + 1 < len(completion_order)
                else None
            )
            for index, label in enumerate(completion_order)
        }
        if completion_order:
            self._completion_turns[completion_order[0]].set()
        self.classification_completion_labels: list[str] = []

    @property
    def max_concurrent_classifications(self) -> int:
        return self._probe.max_active

    def classify_nodes(self, request):  # type: ignore[no-untyped-def]
        root = next(
            item for item in request.nodes if item.kind is StructureNodeKind.SECTION
        )
        label = str(root.explicit_label or "")
        self._probe.enter()
        try:
            turn = self._completion_turns.get(label)
            if turn is not None and not turn.wait(timeout=5):
                raise AssertionError(
                    f"classification completion order stalled at {label}"
                )
            with self._result_lock:
                result = super().classify_nodes(request)
            with self._completion_lock:
                self.classification_completion_labels.append(label)
            next_label = self._next_completion_label.get(label)
            if next_label is not None:
                self._completion_turns[next_label].set()
            return result
        finally:
            self._probe.exit()

    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        with self._result_lock:
            return super().assemble_requirements(request)


def _three_provision_input():  # type: ignore[no-untyped-def]
    source, _ = _flat_source(
        (
            (SourceNodeKind.HEADING, "4 Verification"),
            (SourceNodeKind.HEADING, "4.12 Mechanical characteristics"),
            (SourceNodeKind.HEADING, "4.12.1 First requirement"),
            (SourceNodeKind.PARAGRAPH, "The first product shall operate."),
            (SourceNodeKind.HEADING, "4.12.2 Second requirement"),
            (SourceNodeKind.PARAGRAPH, "The second product shall operate."),
            (SourceNodeKind.HEADING, "4.12.3 Third requirement"),
            (SourceNodeKind.PARAGRAPH, "The third product shall operate."),
        )
    )
    tree = build_structure_tree(source)
    locator = LlmExtractionScopeLocator(
        source_sha256=source.source_sha256,
        section_selectors=tuple(
            LlmSectionScopeSelector(("4", "4.12", f"4.12.{index}"))
            for index in (1, 2, 3)
        ),
    )
    return source, tree, locator


def _node_budget_input():  # type: ignore[no-untyped-def]
    source, _ = _flat_source(
        (
            (SourceNodeKind.HEADING, "4 Verification"),
            (SourceNodeKind.HEADING, "4.12 Mechanical characteristics"),
            (SourceNodeKind.HEADING, "4.12.1 Small requirement"),
            (SourceNodeKind.PARAGRAPH, "The small product shall operate."),
            (SourceNodeKind.HEADING, "4.12.2 Oversized requirement"),
            *tuple(
                (SourceNodeKind.PARAGRAPH, f"Oversized provision line {index}.")
                for index in range(289)
            ),
        )
    )
    tree = build_structure_tree(source)
    locator = LlmExtractionScopeLocator(
        source_sha256=source.source_sha256,
        section_selectors=(
            LlmSectionScopeSelector(("4", "4.12", "4.12.1")),
            LlmSectionScopeSelector(("4", "4.12", "4.12.2")),
        ),
    )
    return source, tree, locator


def _many_provision_input(count):  # type: ignore[no-untyped-def]
    source, _ = _flat_source(
        (
            (SourceNodeKind.HEADING, "4 Verification"),
            (SourceNodeKind.HEADING, "4.12 Mechanical characteristics"),
            *tuple(
                pair
                for index in range(1, count + 1)
                for pair in (
                    (
                        SourceNodeKind.HEADING,
                        f"4.12.{index} Requirement {index}",
                    ),
                    (
                        SourceNodeKind.PARAGRAPH,
                        f"The product requirement {index} shall operate.",
                    ),
                )
            ),
        )
    )
    tree = build_structure_tree(source)
    locator = LlmExtractionScopeLocator(
        source_sha256=source.source_sha256,
        section_selectors=tuple(
            LlmSectionScopeSelector(("4", "4.12", f"4.12.{index}"))
            for index in range(1, count + 1)
        ),
    )
    return source, tree, locator


if __name__ == "__main__":
    unittest.main()
