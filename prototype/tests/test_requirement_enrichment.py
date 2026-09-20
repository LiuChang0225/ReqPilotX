from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import threading
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.adapters.documents.bailian_requirement_atomization_provider import (
    REQUIREMENT_ATOMIZATION_PROMPT_VERSION,
    REQUIREMENT_ATOMIZATION_RESPONSE_SCHEMA_VERSION,
    _AtomizationPayload,
    _atomization_system_prompt,
    _atomization_user_payload,
)
from app.adapters.documents.local_requirement_atomization_provider import (
    LOCAL_REQUIREMENT_ATOMIZATION_PROVIDER_ID,
    LocalPassThroughRequirementAtomizationProvider,
)
from app.application.extraction.llm_requirement_review import (
    evaluate_llm_requirement_review,
    initial_llm_requirement_review,
)
from app.application.extraction.llm_provision_batch import (
    LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION,
)
from app.application.extraction.llm_semantics import (
    MODEL_REQUIREMENT_ASSEMBLER_VERSION,
    MODEL_SEMANTIC_CLASSIFIER_VERSION,
    SEMANTIC_PLANNING_RUNTIME_VERSION,
)
from app.application.extraction.requirement_enrichment import (
    REQUIREMENT_ATOMIZATION_ALGORITHM_VERSION,
    _validated_atomization_obligation_spans,
    build_requirement_atomization_run,
    build_requirement_publication_preview,
    build_requirement_refinement_suggestion,
    revise_requirement_refinement_suggestion,
)
from app.application.extraction.serialize_structure_workbench import (
    _serialize_enrichment_workflow,
)
from app.domain.extraction.llm_requirement_review import (
    LlmRequirementReviewSession,
    LlmRequirementReviewedResult,
    LlmReviewSourceSpan,
)
from app.domain.extraction.requirement_enrichment import (
    REQUIREMENT_ATOMIZATION_RUN_SCHEMA_VERSION,
    RequirementAtomizationNodeKind,
    RequirementEnrichmentCoverageReceipt,
    RequirementRefinementOperation,
    RequirementRefinementStatus,
    stable_requirement_enrichment_coverage_sha256,
)
from app.domain.extraction.semantic_terminal_receipts import (
    SemanticUnitTerminalReceipt,
    SemanticUnitTerminalStatus,
    stable_semantic_unit_terminal_receipt_id,
)
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.domain.extraction.table_evidence import (
    EvidenceReferenceKind,
    EvidenceReferenceV1,
    TableEvidenceBindingV1,
)
from app.ports.requirement_refinement_provider import (
    RequirementRefinementProviderRequest,
    RequirementRefinementProviderResult,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationContextDisposition,
    RequirementAtomizationContextRange,
    RequirementAtomizationObligation,
    RequirementAtomizationProviderError,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationProviderResult,
    RequirementAtomizationRange,
    RequirementAtomizationSourceSegment,
    RequirementAtomizationTableContext,
)
from tests.test_llm_requirement_review import _prediction


class _RefinementProvider:
    def __init__(self) -> None:
        self.requests: list[RequirementRefinementProviderRequest] = []

    def suggest(
        self,
        request: RequirementRefinementProviderRequest,
    ) -> RequirementRefinementProviderResult:
        self.requests.append(request)
        started_at = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
        request_sha256 = "7" * 64
        response_sha256 = "8" * 64
        status = SemanticProviderRunStatus.SUCCESS
        run = SemanticProviderRunRecord(
            provider_run_id=stable_semantic_provider_run_id(
                operation="refine_atomic_obligation",
                provider_id="fake-refinement-provider",
                model_id="fake-model",
                request_sha256=request_sha256,
                response_sha256=response_sha256,
                status=status,
                started_at=started_at,
            ),
            operation="refine_atomic_obligation",
            provider_id="fake-refinement-provider",
            provider_version="1.0.0",
            model_id="fake-model",
            prompt_version="fake-refinement/v1",
            response_schema_version="fake-refinement-response/v1",
            request_sha256=request_sha256,
            response_sha256=response_sha256,
            status=status,
            started_at=started_at,
            finished_at=started_at,
            latency_ms=0,
            request_count=1,
            retry_count=0,
        )
        return RequirementRefinementProviderResult(
            suggested_statement=(
                "The component shall complete the second review target "
                "without changing its source-bound limits."
            ),
            rationale="Makes the obligation independently readable.",
            reason_codes=("independent_readability",),
            run=run,
        )


class _AtomizationProvider:
    def __init__(
        self,
        *,
        split_at: int | None = None,
        invalid_ranges: tuple[RequirementAtomizationRange, ...] | None = None,
        atomic_statements: tuple[str, ...] | None = None,
        context_ranges: tuple[RequirementAtomizationContextRange, ...] = (),
    ) -> None:
        self.split_at = split_at
        self.invalid_ranges = invalid_ranges
        self.atomic_statements = atomic_statements
        self.context_ranges = context_ranges
        self.call_count = 0
        self.requests: list[RequirementAtomizationProviderRequest] = []

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        self.call_count += 1
        self.requests.append(request)
        started_at = datetime(2026, 7, 27, 7, 30, tzinfo=timezone.utc)
        status = SemanticProviderRunStatus.SUCCESS
        run = SemanticProviderRunRecord(
            provider_run_id=stable_semantic_provider_run_id(
                operation="atomize_requirement_obligation",
                provider_id="fake-atomization-provider",
                model_id="fake-model",
                request_sha256="5" * 64,
                response_sha256="6" * 64,
                status=status,
                started_at=started_at,
            ),
            operation="atomize_requirement_obligation",
            provider_id="fake-atomization-provider",
            provider_version="1.0.0",
            model_id="fake-model",
            prompt_version="fake-atomization/v1",
            response_schema_version="fake-atomization-response/v1",
            request_sha256="5" * 64,
            response_sha256="6" * 64,
            status=status,
            started_at=started_at,
            finished_at=started_at,
            latency_ms=0,
            request_count=1,
            retry_count=0,
        )
        if self.invalid_ranges is not None:
            obligations = (
                RequirementAtomizationObligation(
                    source_ranges=self.invalid_ranges,
                    atomic_statement=(
                        self.atomic_statements[0]
                        if self.atomic_statements is not None
                        else " ".join(
                            item.source_text for item in request.source_segments
                        ).strip()
                    ),
                    split_basis="single_or_descriptive",
                    reason_codes=("single_obligation",),
                ),
            )
        else:
            ranges = tuple(
                RequirementAtomizationRange(
                    segment_index=item.segment_index,
                    start_offset=0,
                    end_offset=len(item.source_text),
                )
                for item in request.source_segments
            )
            if self.split_at is None:
                atomic_statement = (
                    self.atomic_statements[0]
                    if self.atomic_statements is not None
                    else " ".join(
                        " ".join(item.source_text.split())
                        for item in request.source_segments
                    )
                )
                obligations = (
                    RequirementAtomizationObligation(
                        source_ranges=ranges,
                        atomic_statement=atomic_statement,
                        split_basis="single_or_descriptive",
                        reason_codes=("single_obligation",),
                    ),
                )
            else:
                if len(request.source_segments) != 1:
                    raise AssertionError("split fixture expects one source segment")
                text_length = len(request.source_segments[0].source_text)
                atomic_statements = self.atomic_statements or (
                    "The component shall satisfy the first source-bound obligation.",
                    "The component shall satisfy the second source-bound obligation.",
                )
                obligations = (
                    RequirementAtomizationObligation(
                        source_ranges=(
                            RequirementAtomizationRange(
                                segment_index=0,
                                start_offset=0,
                                end_offset=self.split_at,
                            ),
                        ),
                        atomic_statement=atomic_statements[0],
                        split_basis="independent_obligation",
                        reason_codes=("independent_obligation",),
                    ),
                    RequirementAtomizationObligation(
                        source_ranges=(
                            RequirementAtomizationRange(
                                segment_index=0,
                                start_offset=self.split_at,
                                end_offset=text_length,
                            ),
                        ),
                        atomic_statement=atomic_statements[1],
                        split_basis="independent_obligation",
                        reason_codes=("independent_obligation",),
                    ),
                )
        return RequirementAtomizationProviderResult(
            obligations=obligations,
            context_ranges=self.context_ranges,
            run=run,
        )


class _ConcurrentAtomizationProvider:
    def __init__(
        self,
        *,
        first_review_node_id: str,
        fail_review_node_id: str | None = None,
    ) -> None:
        self.first_review_node_id = first_review_node_id
        self.fail_review_node_id = fail_review_node_id
        self._peer_started = threading.Event()
        self._peer_completed = threading.Event()
        self._lock = threading.Lock()
        self.active_call_count = 0
        self.max_active_call_count = 0
        self.completion_order: list[str] = []

    def atomize(
        self,
        request: RequirementAtomizationProviderRequest,
    ) -> RequirementAtomizationProviderResult:
        with self._lock:
            self.active_call_count += 1
            self.max_active_call_count = max(
                self.max_active_call_count,
                self.active_call_count,
            )
        try:
            if request.review_node_id == self.first_review_node_id:
                if not self._peer_started.wait(timeout=2):
                    raise AssertionError("concurrent peer did not start")
                if not self._peer_completed.wait(timeout=2):
                    raise AssertionError("concurrent peer did not complete")
            else:
                self._peer_started.set()
            if request.review_node_id == self.fail_review_node_id:
                self._peer_completed.set()
                raise RuntimeError("provider call failed")
            with self._lock:
                self.completion_order.append(request.review_node_id)
            if request.review_node_id != self.first_review_node_id:
                self._peer_completed.set()
            request_sha256 = hashlib.sha256(
                request.review_node_id.encode("utf-8")
            ).hexdigest()
            response_sha256 = hashlib.sha256(
                f"response:{request.review_node_id}".encode("utf-8")
            ).hexdigest()
            started_at = datetime(2026, 7, 27, 7, 30, tzinfo=timezone.utc)
            status = SemanticProviderRunStatus.SUCCESS
            run = SemanticProviderRunRecord(
                provider_run_id=stable_semantic_provider_run_id(
                    operation="atomize_requirement_obligation",
                    provider_id="concurrent-atomization-provider",
                    model_id="fake-model",
                    request_sha256=request_sha256,
                    response_sha256=response_sha256,
                    status=status,
                    started_at=started_at,
                ),
                operation="atomize_requirement_obligation",
                provider_id="concurrent-atomization-provider",
                provider_version="1.0.0",
                model_id="fake-model",
                prompt_version="fake-atomization/v2",
                response_schema_version="fake-atomization-response/v2",
                request_sha256=request_sha256,
                response_sha256=response_sha256,
                status=status,
                started_at=started_at,
                finished_at=started_at,
                latency_ms=0,
                request_count=1,
                retry_count=0,
            )
            return RequirementAtomizationProviderResult(
                obligations=(
                    RequirementAtomizationObligation(
                        source_ranges=tuple(
                            RequirementAtomizationRange(
                                segment_index=item.segment_index,
                                start_offset=0,
                                end_offset=len(item.source_text),
                            )
                            for item in request.source_segments
                        ),
                        atomic_statement=" ".join(
                            " ".join(item.source_text.split())
                            for item in request.source_segments
                        ),
                        split_basis="single_or_descriptive",
                        reason_codes=("single_obligation",),
                    ),
                ),
                run=run,
            )
        finally:
            with self._lock:
                self.active_call_count -= 1


def _coverage(
    session: LlmRequirementReviewSession,
    *,
    unresolved: bool = False,
    blocked_unit_count: int = 0,
) -> RequirementEnrichmentCoverageReceipt:
    planning_runtime_version = SEMANTIC_PLANNING_RUNTIME_VERSION
    receipts = []
    for index, node in enumerate(session.prediction.nodes):
        status = (
            SemanticUnitTerminalStatus.UNRESOLVED
            if unresolved and index == 0
            else SemanticUnitTerminalStatus.RESOLVED
        )
        reason_codes = (
            ("provider_output_unresolved",)
            if status is SemanticUnitTerminalStatus.UNRESOLVED
            else ("provider_output_resolved",)
        )
        values = {
            "semantic_unit_plan_id": session.prediction.semantic_unit_plan_ids[0],
            "semantic_unit_id": node.semantic_unit_id,
            "owner_structure_node_id": node.owner_structure_node_id,
            "unit_order": node.unit_order,
            "source_coverage_sha256": f"{index + 1:064x}",
            "planning_runtime_version": planning_runtime_version,
            "status": status,
            "provider_run_id": "semantic-run-" + f"{index + 1:032x}",
            "reason_codes": reason_codes,
        }
        receipts.append(
            SemanticUnitTerminalReceipt(
                **values,
                terminal_receipt_id=stable_semantic_unit_terminal_receipt_id(**values),
            )
        )
    frozen = tuple(receipts)
    values = {
        "total_units": len(frozen) + blocked_unit_count,
        "blocked_unit_count": blocked_unit_count,
        "planning_runtime_version": planning_runtime_version,
        "classifier_runtime_version": MODEL_SEMANTIC_CLASSIFIER_VERSION,
        "assembler_runtime_version": MODEL_REQUIREMENT_ASSEMBLER_VERSION,
        "document_runtime_version": LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION,
        "terminal_receipts": frozen,
    }
    return RequirementEnrichmentCoverageReceipt(
        **values,
        coverage_sha256=stable_requirement_enrichment_coverage_sha256(**values),
    )


def _table_evidence_binding(
    *,
    source_spans: tuple[LlmReviewSourceSpan, ...],
    record_id: str,
) -> TableEvidenceBindingV1:
    def reference(
        kind: EvidenceReferenceKind,
        name: str,
        digit: str,
    ) -> EvidenceReferenceV1:
        return EvidenceReferenceV1(
            kind=kind,
            reference_id=name,
            resolver_key=f"source-ir://document-a/{name}",
            content_sha256=digit * 64,
            page_number=1,
        )

    record_digit = "1" if record_id.endswith("1") else "2"
    return TableEvidenceBindingV1.create(
        logical_table_id="logical-table-a",
        source_table_ids=("table-a",),
        projection_id=f"table-projection-{record_id}",
        projection_revision=0,
        projection_sha256=record_digit * 64,
        record_id=record_id,
        source_spans=source_spans,
        table_refs=(reference(EvidenceReferenceKind.TABLE, "table-a", "3"),),
        header_refs=(reference(EvidenceReferenceKind.HEADER, "header-a", "4"),),
        shared_value_refs=(
            reference(EvidenceReferenceKind.SHARED_VALUE, "shared-a", "5"),
        ),
        note_refs=(reference(EvidenceReferenceKind.NOTE, "note-a", "6"),),
        image_refs=(reference(EvidenceReferenceKind.IMAGE, "image-a", "7"),),
    )


class RequirementEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        prediction = _prediction()
        reviewed = initial_llm_requirement_review(prediction)
        now = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
        self.session = LlmRequirementReviewSession(
            session_id="llm-review-" + "9" * 32,
            project_id="project-a",
            document_id="document-a",
            prediction=prediction,
            reviewed_result=reviewed,
            delta=evaluate_llm_requirement_review(prediction, reviewed),
            created_at_utc=now,
            updated_at_utc=now,
        )

    def test_atomization_preserves_rollup_and_emits_only_direct_atoms(
        self,
    ) -> None:
        run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(),
        )

        self.assertEqual(run.revision, 0)
        self.assertEqual(len(run.nodes), 2)
        self.assertEqual(
            run.nodes[0].kind,
            RequirementAtomizationNodeKind.ROLLUP,
        )
        self.assertIsNone(run.nodes[0].atomic_node_id)
        self.assertEqual(
            run.nodes[1].kind,
            RequirementAtomizationNodeKind.ATOMIC_OBLIGATION,
        )
        self.assertIsNotNone(run.nodes[1].atomic_node_id)
        self.assertEqual(run.nodes[1].parent_node_id, run.nodes[0].node_id)
        self.assertEqual(len(run.atomic_nodes), 1)
        self.assertEqual(run.lineage.review_revision, 0)
        self.assertEqual(
            run.schema_version,
            REQUIREMENT_ATOMIZATION_RUN_SCHEMA_VERSION,
        )
        self.assertEqual(
            run.algorithm_version,
            REQUIREMENT_ATOMIZATION_ALGORITHM_VERSION,
        )
        self.assertIsNotNone(run.atomic_nodes[0].atomic_statement)

    def test_provider_can_split_one_compound_direct_into_two_exact_atoms(
        self,
    ) -> None:
        direct = next(
            item
            for item in self.session.reviewed_result.nodes
            if item.decision_mode.value == "DIRECT"
        )
        source_length = sum(
            item.end_offset - item.start_offset for item in direct.source_spans
        )
        self.assertGreater(source_length, 1)

        run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(split_at=source_length // 2),
        )

        self.assertEqual(len(run.atomic_nodes), 2)
        self.assertEqual(
            "".join(item.source_text for item in run.atomic_nodes),
            next(
                item.source_text
                for item in build_requirement_atomization_run(
                    self.session,
                    coverage=_coverage(self.session),
                    provider=_AtomizationProvider(),
                ).atomic_nodes
            ),
        )
        self.assertEqual(
            run.atomic_nodes[0].source_spans[-1].end_offset,
            run.atomic_nodes[1].source_spans[0].start_offset,
        )
        self.assertNotEqual(
            run.atomic_nodes[0].atomic_statement,
            run.atomic_nodes[1].atomic_statement,
        )

    def test_table_record_evidence_survives_two_atoms_and_publication(
        self,
    ) -> None:
        direct = next(
            item
            for item in self.session.reviewed_result.nodes
            if item.decision_mode.value == "DIRECT"
        )
        source_length = sum(
            item.end_offset - item.start_offset for item in direct.source_spans
        )
        evidence = _table_evidence_binding(
            source_spans=direct.source_spans,
            record_id="record-1",
        )
        provider = _AtomizationProvider(split_at=source_length // 2)

        run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=provider,
            table_evidence_by_review_node={
                direct.review_node_id: evidence,
            },
        )

        self.assertEqual(len(run.atomic_nodes), 2)
        self.assertEqual(
            {item.table_evidence_binding_id for item in run.atomic_nodes},
            {evidence.binding_id},
        )
        self.assertEqual(len(provider.requests), 1)
        request_context = provider.requests[0].table_context
        self.assertIsNotNone(request_context)
        assert request_context is not None
        self.assertEqual(
            request_context.table_evidence_binding_id,
            evidence.binding_id,
        )
        self.assertEqual(request_context.record_id, "record-1")
        self.assertEqual(
            request_context.header_resolver_refs,
            ("source-ir://document-a/header-a",),
        )
        self.assertEqual(
            request_context.shared_value_resolver_refs,
            ("source-ir://document-a/shared-a",),
        )
        self.assertEqual(
            request_context.note_resolver_refs,
            ("source-ir://document-a/note-a",),
        )
        self.assertEqual(
            request_context.image_resolver_refs,
            ("source-ir://document-a/image-a",),
        )

        preview = build_requirement_publication_preview(
            session=self.session,
            run=run,
            coverage=_coverage(self.session),
            refinements=(),
            base_active_requirement_set_id="active-set-a",
        )
        table_entries = tuple(
            item for item in preview.entries if item.entry_kind.value == "REQUIREMENT"
        )
        self.assertEqual(len(table_entries), 2)
        self.assertEqual(
            {item.table_evidence_binding_id for item in table_entries},
            {evidence.binding_id},
        )

        changed_evidence = _table_evidence_binding(
            source_spans=direct.source_spans,
            record_id="record-2",
        )
        changed_run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(split_at=source_length // 2),
            table_evidence_by_review_node={
                direct.review_node_id: changed_evidence,
            },
        )
        self.assertNotEqual(run.atomization_run_id, changed_run.atomization_run_id)
        self.assertNotEqual(
            tuple(item.node_id for item in run.atomic_nodes),
            tuple(item.node_id for item in changed_run.atomic_nodes),
        )

    def test_table_atomization_rejects_a_review_span_crossing_record_evidence(
        self,
    ) -> None:
        direct = next(
            item
            for item in self.session.reviewed_result.nodes
            if item.decision_mode.value == "DIRECT"
        )
        first_span = direct.source_spans[0]
        partial_end = first_span.start_offset + max(
            1,
            (first_span.end_offset - first_span.start_offset) // 2,
        )
        partial_evidence = _table_evidence_binding(
            source_spans=(first_span.model_copy(update={"end_offset": partial_end}),),
            record_id="record-1",
        )

        with self.assertRaisesRegex(
            ValueError,
            "cannot cross table record evidence boundaries",
        ):
            build_requirement_atomization_run(
                self.session,
                coverage=_coverage(self.session),
                provider=_AtomizationProvider(),
                table_evidence_by_review_node={
                    direct.review_node_id: partial_evidence,
                },
            )

    def test_non_table_atomization_and_publication_keep_empty_table_lineage(
        self,
    ) -> None:
        provider = _AtomizationProvider()
        run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=provider,
        )

        self.assertIsNone(provider.requests[0].table_context)
        self.assertTrue(
            all(item.table_evidence_binding_id is None for item in run.nodes)
        )
        preview = build_requirement_publication_preview(
            session=self.session,
            run=run,
            coverage=_coverage(self.session),
            refinements=(),
            base_active_requirement_set_id="active-set-a",
        )
        self.assertTrue(
            all(item.table_evidence_binding_id is None for item in preview.entries)
        )

    def test_single_or_descriptive_output_is_not_mechanically_split(self) -> None:
        run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(),
        )
        self.assertEqual(len(run.atomic_nodes), 1)

    def test_local_pass_through_is_exact_and_audited_as_skipped(self) -> None:
        source_texts = ("  The component shall start.", " It shall stop.  ")
        request = RequirementAtomizationProviderRequest(
            lineage_sha256="a" * 64,
            review_node_id="review-node-local",
            requirement_subtype="FUNCTIONAL",
            source_segments=tuple(
                RequirementAtomizationSourceSegment(
                    segment_index=index,
                    semantic_unit_id=f"semantic-unit-{index + 1:032x}",
                    owner_structure_node_id=f"structure-node-{index + 1:032x}",
                    source_start_offset=0,
                    source_end_offset=len(source_text),
                    source_text=source_text,
                )
                for index, source_text in enumerate(source_texts)
            ),
        )

        result = LocalPassThroughRequirementAtomizationProvider().atomize(request)

        self.assertEqual(len(result.obligations), 1)
        self.assertEqual(
            result.obligations[0].atomic_statement,
            "".join(source_texts),
        )
        self.assertEqual(
            result.obligations[0].source_ranges,
            tuple(
                RequirementAtomizationRange(
                    segment_index=index,
                    start_offset=0,
                    end_offset=len(source_text),
                )
                for index, source_text in enumerate(source_texts)
            ),
        )
        self.assertEqual(
            result.run.provider_id,
            LOCAL_REQUIREMENT_ATOMIZATION_PROVIDER_ID,
        )
        self.assertEqual(result.run.status, SemanticProviderRunStatus.SKIPPED)
        self.assertEqual(result.run.request_count, 0)
        self.assertEqual(result.run.retry_count, 0)
        self.assertEqual(result.run.latency_ms, 0)
        self.assertIsNone(result.run.response_sha256)
        self.assertIsNone(result.run.input_tokens)
        self.assertIsNone(result.run.output_tokens)
        self.assertEqual(
            result.run.error_code,
            "remote_provider_not_configured",
        )

    def test_split_atom_refinement_receives_full_source_and_parent_context(
        self,
    ) -> None:
        direct = next(
            item
            for item in self.session.reviewed_result.nodes
            if item.decision_mode.value == "DIRECT"
        )
        source_length = sum(
            item.end_offset - item.start_offset for item in direct.source_spans
        )
        run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(split_at=source_length // 2),
        )
        provider = _RefinementProvider()

        build_requirement_refinement_suggestion(
            run=run,
            atomic_node_id=str(run.atomic_nodes[0].atomic_node_id),
            provider=provider,
        )

        request = provider.requests[0]
        self.assertEqual(
            request.original_statement,
            run.atomic_nodes[0].atomic_statement,
        )
        self.assertEqual(
            request.source_context_statement,
            "".join(item.source_text for item in run.atomic_nodes),
        )
        self.assertNotEqual(
            request.source_context_statement,
            request.original_statement,
        )
        self.assertTrue(request.parent_context_statement)

    def test_multi_atom_output_requires_distinct_independent_statements(
        self,
    ) -> None:
        direct = next(
            item
            for item in self.session.reviewed_result.nodes
            if item.decision_mode.value == "DIRECT"
        )
        source_length = sum(
            item.end_offset - item.start_offset for item in direct.source_spans
        )

        with self.assertRaisesRegex(ValueError, "independent atomic statements"):
            build_requirement_atomization_run(
                self.session,
                coverage=_coverage(self.session),
                provider=_AtomizationProvider(
                    split_at=source_length // 2,
                    atomic_statements=(
                        "Duplicate statement.",
                        " duplicate statement. ",
                    ),
                ),
            )

    def test_provider_payload_requires_atomic_statement_and_v2_contract(
        self,
    ) -> None:
        missing_statement = {
            "obligations": [
                {
                    "source_ranges": [
                        {"segment_index": 0, "start_offset": 0, "end_offset": 1}
                    ],
                    "split_basis": "single_or_descriptive",
                    "reason_codes": ["single_obligation"],
                }
            ]
        }
        with self.assertRaises(ValidationError):
            _AtomizationPayload.model_validate(missing_statement)

        self.assertTrue(REQUIREMENT_ATOMIZATION_PROMPT_VERSION.endswith("/v3"))
        self.assertTrue(REQUIREMENT_ATOMIZATION_RESPONSE_SCHEMA_VERSION.endswith("/v2"))
        self.assertIn("atomic_statement", _atomization_system_prompt())
        self.assertIn("independently readable", _atomization_system_prompt())
        self.assertIn("never cross that record boundary", _atomization_system_prompt())

    def test_remote_atomization_payload_preserves_compact_table_context(self) -> None:
        request = RequirementAtomizationProviderRequest(
            lineage_sha256="a" * 64,
            review_node_id="review-node-table",
            requirement_subtype="FUNCTIONAL",
            parent_context_statement="共享条款上下文",
            source_segments=(
                RequirementAtomizationSourceSegment(
                    segment_index=0,
                    semantic_unit_id="semantic-unit-" + "1" * 32,
                    owner_structure_node_id="structure-node-" + "2" * 32,
                    source_start_offset=0,
                    source_end_offset=4,
                    source_text="制动力矩",
                ),
            ),
            table_context=RequirementAtomizationTableContext(
                table_evidence_binding_id=("table-evidence-binding-" + "3" * 32),
                logical_table_id="logical-table-a",
                projection_id="table-projection-a",
                projection_revision=0,
                projection_sha256="4" * 64,
                record_id="record-a",
                header_resolver_refs=("source-ir://document-a/header-a",),
                shared_value_resolver_refs=("source-ir://document-a/shared-a",),
                note_resolver_refs=("source-ir://document-a/note-a",),
                image_resolver_refs=("source-ir://document-a/image-a",),
            ),
        )

        payload = _atomization_user_payload(request)

        self.assertEqual(payload["source_segments"][0]["source_text"], "制动力矩")
        self.assertEqual(payload["table_context"]["record_id"], "record-a")
        self.assertEqual(
            payload["table_context"]["image_resolver_refs"],
            ["source-ir://document-a/image-a"],
        )
        self.assertNotIn("source_ir", payload)

    def test_atomization_rejects_overlap_out_of_bounds_and_incomplete_coverage(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            build_requirement_atomization_run(
                self.session,
                coverage=_coverage(self.session),
                provider=_AtomizationProvider(
                    invalid_ranges=(
                        RequirementAtomizationRange(
                            segment_index=0,
                            start_offset=1,
                            end_offset=2,
                        ),
                    )
                ),
            )
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            build_requirement_atomization_run(
                self.session,
                coverage=_coverage(self.session),
                provider=_AtomizationProvider(
                    invalid_ranges=(
                        RequirementAtomizationRange(
                            segment_index=0,
                            start_offset=0,
                            end_offset=999_999,
                        ),
                    )
                ),
            )
        with self.assertRaisesRegex(ValueError, "complete resolved"):
            build_requirement_atomization_run(
                self.session,
                coverage=_coverage(self.session, unresolved=True),
                provider=_AtomizationProvider(),
            )

    def test_atomization_context_ranges_cover_source_without_becoming_atoms(
        self,
    ) -> None:
        source_text = "ABCDE12345"
        segments = (
            RequirementAtomizationSourceSegment(
                segment_index=0,
                semantic_unit_id="semantic-unit-" + "1" * 32,
                owner_structure_node_id="structure-node-" + "2" * 32,
                source_start_offset=0,
                source_end_offset=len(source_text),
                source_text=source_text,
            ),
        )
        request = RequirementAtomizationProviderRequest(
            lineage_sha256="3" * 64,
            review_node_id="review-context-range",
            requirement_subtype="FUNCTIONAL",
            source_segments=segments,
        )
        provider = _AtomizationProvider(
            invalid_ranges=(
                RequirementAtomizationRange(
                    segment_index=0,
                    start_offset=0,
                    end_offset=5,
                ),
            ),
            context_ranges=(
                RequirementAtomizationContextRange(
                    source_range=RequirementAtomizationRange(
                        segment_index=0,
                        start_offset=5,
                        end_offset=10,
                    ),
                    disposition=RequirementAtomizationContextDisposition.CONTEXT,
                    reason_codes=("non_obligation_context",),
                ),
            ),
        )

        spans = _validated_atomization_obligation_spans(
            provider.atomize(request),
            source_segments=segments,
        )

        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0][0].start_offset, 0)
        self.assertEqual(spans[0][0].end_offset, 5)

    def test_atomization_unresolved_context_blocks_publication(self) -> None:
        source_text = "ABCDE12345"
        segments = (
            RequirementAtomizationSourceSegment(
                segment_index=0,
                semantic_unit_id="semantic-unit-" + "1" * 32,
                owner_structure_node_id="structure-node-" + "2" * 32,
                source_start_offset=0,
                source_end_offset=len(source_text),
                source_text=source_text,
            ),
        )
        request = RequirementAtomizationProviderRequest(
            lineage_sha256="3" * 64,
            review_node_id="review-unresolved-context",
            requirement_subtype="FUNCTIONAL",
            source_segments=segments,
        )
        provider = _AtomizationProvider(
            invalid_ranges=(
                RequirementAtomizationRange(
                    segment_index=0,
                    start_offset=0,
                    end_offset=5,
                ),
            ),
            context_ranges=(
                RequirementAtomizationContextRange(
                    source_range=RequirementAtomizationRange(
                        segment_index=0,
                        start_offset=5,
                        end_offset=10,
                    ),
                    disposition=RequirementAtomizationContextDisposition.UNRESOLVED,
                    reason_codes=("non_consensus_range",),
                ),
            ),
        )

        with self.assertRaisesRegex(
            RequirementAtomizationProviderError,
            "left source-bound context unresolved",
        ):
            _validated_atomization_obligation_spans(
                provider.atomize(request),
                source_segments=segments,
            )
        with self.assertRaisesRegex(ValueError, "complete resolved"):
            build_requirement_atomization_run(
                self.session,
                coverage=_coverage(self.session, blocked_unit_count=1),
                provider=_AtomizationProvider(),
            )

    def test_refinement_is_independent_and_only_accept_applies_to_preview(
        self,
    ) -> None:
        run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(),
        )
        atomic_node_id = str(run.atomic_nodes[0].atomic_node_id)
        suggestion = build_requirement_refinement_suggestion(
            run=run,
            atomic_node_id=atomic_node_id,
            provider=_RefinementProvider(),
            now=datetime(2026, 7, 27, 8, 1, tzinfo=timezone.utc),
        )

        draft_preview = build_requirement_publication_preview(
            session=self.session,
            run=run,
            coverage=_coverage(self.session),
            refinements=(suggestion,),
            base_active_requirement_set_id="active-set-a",
            now=datetime(2026, 7, 27, 8, 2, tzinfo=timezone.utc),
        )
        accepted = revise_requirement_refinement_suggestion(
            suggestion,
            expected_revision=0,
            operation=RequirementRefinementOperation.ACCEPT,
            decision_note="Approved for the formal draft.",
            now=datetime(2026, 7, 27, 8, 3, tzinfo=timezone.utc),
        )
        accepted_preview = build_requirement_publication_preview(
            session=self.session,
            run=run,
            coverage=_coverage(self.session),
            refinements=(accepted,),
            base_active_requirement_set_id="active-set-a",
            now=datetime(2026, 7, 27, 8, 4, tzinfo=timezone.utc),
        )

        draft_requirement = next(
            item for item in draft_preview.entries if item.atomic_node_id is not None
        )
        accepted_requirement = next(
            item for item in accepted_preview.entries if item.atomic_node_id is not None
        )
        self.assertEqual(
            draft_requirement.published_statement,
            run.atomic_nodes[0].atomic_statement,
        )
        self.assertEqual(
            draft_requirement.original_statement,
            run.atomic_nodes[0].source_text,
        )
        self.assertNotEqual(
            draft_requirement.published_statement,
            draft_requirement.original_statement,
        )
        self.assertEqual(
            accepted_requirement.published_statement,
            suggestion.suggested_statement,
        )
        self.assertEqual(accepted.status, RequirementRefinementStatus.ACCEPTED)
        self.assertEqual(accepted.revision, 1)
        self.assertNotEqual(
            draft_preview.publication_key,
            accepted_preview.publication_key,
        )
        self.assertEqual(
            accepted_preview.refinement_bindings[0].revision,
            1,
        )

    def test_human_revision_remains_separate_from_original_statement(self) -> None:
        run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(),
        )
        suggestion = build_requirement_refinement_suggestion(
            run=run,
            atomic_node_id=str(run.atomic_nodes[0].atomic_node_id),
            provider=_RefinementProvider(),
        )

        revised = revise_requirement_refinement_suggestion(
            suggestion,
            expected_revision=0,
            operation=RequirementRefinementOperation.REVISE,
            suggested_statement=(
                "The component shall complete target two while retaining "
                "the exact source-bound constraint."
            ),
            rationale="Reviewer wording revision.",
        )

        self.assertEqual(revised.status, RequirementRefinementStatus.DRAFT)
        self.assertEqual(revised.revision, 1)
        self.assertEqual(
            revised.original_statement,
            suggestion.original_statement,
        )
        self.assertIn("human_revision", revised.reason_codes)

    def test_workbench_only_advertises_real_enrichment_actions(self) -> None:
        progressive = _serialize_enrichment_workflow(
            project_id="project-a",
            document_id="document-a",
            editable=False,
            state={},
        )
        self.assertEqual(progressive["issues"], [])
        self.assertEqual(
            progressive["readiness"]["blocking_issue_codes"],
            [],
        )
        self.assertEqual(progressive["workflow"]["review"]["state"], "WAITING")
        self.assertEqual(progressive["workflow"]["coverage"]["state"], "WAITING")
        self.assertEqual(
            progressive["workflow"]["atomization"]["state"],
            "WAITING",
        )
        self.assertEqual(
            progressive["workflow"]["refinement"]["state"],
            "WAITING",
        )
        self.assertEqual(
            progressive["workflow"]["publication"]["state"],
            "WAITING",
        )

        unavailable = _serialize_enrichment_workflow(
            project_id="project-a",
            document_id="document-a",
            editable=True,
            state={},
        )
        self.assertFalse(unavailable["readiness"]["can_atomize"])
        self.assertFalse(unavailable["readiness"]["can_refine"])
        self.assertNotIn("atomization_run_url", unavailable["actions"])
        self.assertNotIn("refinement_suggestion_url", unavailable["actions"])
        self.assertEqual(
            unavailable["readiness"]["blocking_issue_codes"],
            ["REVIEW_SESSION_UNAVAILABLE"],
        )
        self.assertEqual(unavailable["workflow"]["review"]["state"], "BLOCKED")

        local_working_tree = _serialize_enrichment_workflow(
            project_id="project-a",
            document_id="document-a",
            editable=True,
            state={},
            review_session_required=False,
        )
        self.assertEqual(local_working_tree["issues"], [])
        self.assertEqual(
            local_working_tree["readiness"]["blocking_issue_codes"],
            [],
        )
        self.assertEqual(
            local_working_tree["workflow"]["review"]["state"],
            "WAITING",
        )
        self.assertEqual(
            local_working_tree["workflow"]["publication"]["state"],
            "WAITING",
        )

        incomplete_coverage = _coverage(self.session, unresolved=True)
        incomplete = _serialize_enrichment_workflow(
            project_id="project-a",
            document_id="document-a",
            editable=True,
            state={
                "session_id": self.session.session_id,
                "review_revision": 0,
                "eligible_direct_node_count": 1,
                "provider_available": True,
                "atomization_provider_available": True,
                "coverage": {
                    **incomplete_coverage.model_dump(mode="json"),
                    "unresolved_unit_count": 1,
                    "eligible": False,
                },
            },
        )
        self.assertFalse(incomplete["readiness"]["can_atomize"])
        self.assertNotIn("atomization_run_url", incomplete["actions"])
        self.assertIn(
            "SEMANTIC_COVERAGE_INCOMPLETE",
            incomplete["readiness"]["blocking_issue_codes"],
        )

        run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=_AtomizationProvider(),
        )
        ready = _serialize_enrichment_workflow(
            project_id="project-a",
            document_id="document-a",
            editable=True,
            state={
                "session_id": self.session.session_id,
                "review_revision": 0,
                "eligible_direct_node_count": 1,
                "provider_available": True,
                "atomization_provider_available": True,
                "coverage": {
                    **_coverage(self.session).model_dump(mode="json"),
                    "unresolved_unit_count": 0,
                    "eligible": True,
                },
                "atomization_run": run.model_dump(mode="json"),
                "refinements": [],
            },
        )
        self.assertTrue(ready["readiness"]["can_atomize"])
        self.assertTrue(ready["readiness"]["can_refine"])
        self.assertTrue(ready["readiness"]["can_preview_publication"])
        self.assertIn("atomization_run_url", ready["actions"])
        self.assertIn("refinement_suggestion_url", ready["actions"])
        self.assertIn("publication_preview_url", ready["actions"])

        local_ready = _serialize_enrichment_workflow(
            project_id="project-a",
            document_id="document-a",
            editable=True,
            state={
                "session_id": self.session.session_id,
                "review_revision": 0,
                "eligible_direct_node_count": 1,
                "provider_available": False,
                "atomization_provider_available": False,
                "coverage": {
                    **_coverage(self.session).model_dump(mode="json"),
                    "unresolved_unit_count": 0,
                    "eligible": True,
                },
            },
        )
        self.assertTrue(local_ready["readiness"]["can_atomize"])
        self.assertIn("atomization_run_url", local_ready["actions"])
        self.assertNotIn(
            "ATOMIZATION_PROVIDER_UNAVAILABLE",
            local_ready["readiness"]["blocking_issue_codes"],
        )
        fallback_issue = next(
            item
            for item in local_ready["issues"]
            if item["code"] == "ATOMIZATION_PROVIDER_UNAVAILABLE"
        )
        self.assertEqual(fallback_issue["severity"], "INFO")
        self.assertEqual(
            local_ready["workflow"]["atomization"]["execution_mode"],
            "LOCAL_PASS_THROUGH",
        )
        local_run = build_requirement_atomization_run(
            self.session,
            coverage=_coverage(self.session),
            provider=LocalPassThroughRequirementAtomizationProvider(),
        )
        local_completed = _serialize_enrichment_workflow(
            project_id="project-a",
            document_id="document-a",
            editable=True,
            state={
                **local_ready["result"],
                "session_id": self.session.session_id,
                "review_revision": 0,
                "eligible_direct_node_count": 1,
                "provider_available": True,
                "atomization_provider_available": True,
                "coverage": {
                    **_coverage(self.session).model_dump(mode="json"),
                    "unresolved_unit_count": 0,
                    "eligible": True,
                },
                "atomization_run": local_run.model_dump(mode="json"),
            },
        )
        self.assertEqual(
            local_completed["workflow"]["atomization"]["execution_mode"],
            "LOCAL_PASS_THROUGH",
        )

    def test_direct_provider_calls_can_run_concurrently_with_stable_output_order(
        self,
    ) -> None:
        session = _session_with_split_direct_nodes(self.session, count=3)
        expected_review_ids = tuple(
            item.review_node_id
            for item in session.reviewed_result.nodes
            if item.decision_mode.value == "DIRECT"
        )
        provider = _ConcurrentAtomizationProvider(
            first_review_node_id=expected_review_ids[0]
        )

        run = build_requirement_atomization_run(
            session,
            coverage=_coverage(session),
            provider=provider,
            max_concurrent_provider_calls=2,
        )

        self.assertEqual(
            tuple(item.review_node_id for item in run.atomic_nodes),
            expected_review_ids,
        )
        self.assertEqual(
            tuple(item.request_sha256 for item in run.provider_runs),
            tuple(
                hashlib.sha256(item.encode("utf-8")).hexdigest()
                for item in expected_review_ids
            ),
        )
        self.assertEqual(provider.max_active_call_count, 2)
        self.assertNotEqual(provider.completion_order[0], expected_review_ids[0])

    def test_provider_concurrency_limit_is_bounded_and_failures_propagate(
        self,
    ) -> None:
        for invalid_limit in (0, 5, True, 1.5):
            with self.subTest(limit=invalid_limit):
                with self.assertRaisesRegex(ValueError, "between 1 and 4"):
                    build_requirement_atomization_run(
                        self.session,
                        coverage=_coverage(self.session),
                        provider=_AtomizationProvider(),
                        max_concurrent_provider_calls=invalid_limit,  # type: ignore[arg-type]
                    )

        session = _session_with_split_direct_nodes(self.session, count=3)
        direct_ids = tuple(
            item.review_node_id
            for item in session.reviewed_result.nodes
            if item.decision_mode.value == "DIRECT"
        )
        provider = _ConcurrentAtomizationProvider(
            first_review_node_id=direct_ids[0],
            fail_review_node_id=direct_ids[1],
        )
        with self.assertRaisesRegex(RuntimeError, "provider call failed"):
            build_requirement_atomization_run(
                session,
                coverage=_coverage(session),
                provider=provider,
                max_concurrent_provider_calls=2,
            )


def _session_with_split_direct_nodes(
    session: LlmRequirementReviewSession,
    *,
    count: int,
) -> LlmRequirementReviewSession:
    direct = next(
        item
        for item in session.reviewed_result.nodes
        if item.decision_mode.value == "DIRECT"
    )
    if len(direct.source_spans) != 1:
        raise AssertionError("concurrency fixture expects one source span")
    span = direct.source_spans[0]
    length = span.end_offset - span.start_offset
    if length < count:
        raise AssertionError("concurrency fixture source is too short")
    split_nodes = []
    for index in range(count):
        start_offset = span.start_offset + (length * index // count)
        end_offset = span.start_offset + (length * (index + 1) // count)
        split_nodes.append(
            direct.model_copy(
                update={
                    "review_node_id": f"{direct.review_node_id}-split-{index + 1}",
                    "source_spans": (
                        span.model_copy(
                            update={
                                "start_offset": start_offset,
                                "end_offset": end_offset,
                            }
                        ),
                    ),
                }
            )
        )
    nodes = tuple(
        node
        for item in session.reviewed_result.nodes
        for node in (
            tuple(split_nodes)
            if item.review_node_id == direct.review_node_id
            else (item,)
        )
    )
    reviewed = LlmRequirementReviewedResult.model_validate(
        {
            **session.reviewed_result.model_dump(mode="json"),
            "nodes": [item.model_dump(mode="json") for item in nodes],
        }
    )
    return session.model_copy(
        update={
            "reviewed_result": reviewed,
            "delta": evaluate_llm_requirement_review(
                session.prediction,
                reviewed,
            ),
        }
    )


if __name__ == "__main__":
    unittest.main()
