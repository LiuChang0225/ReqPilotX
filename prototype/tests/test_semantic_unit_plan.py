from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import unittest

from pydantic import ValidationError

from app.application.extraction.adapt_semantic_unit_decisions import (
    CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION,
    ControlledSemanticUnitAdapterRejected,
    ControlledSemanticUnitAdapterRejectionCode,
    build_controlled_semantic_unit_plan,
    classify_controlled_semantic_units,
    plan_controlled_semantic_unit_decisions,
)
from app.application.extraction.adapt_semantic_unit_downstream import (
    ControlledSemanticUnitAssemblyDirective,
    ControlledSemanticUnitDownstreamExecution,
    ControlledSemanticUnitDownstreamRejected,
    ControlledSemanticUnitDownstreamRejectionCode,
    assemble_controlled_semantic_units,
    build_controlled_semantic_classification,
    build_controlled_semantic_unit_downstream,
)
from app.application.extraction.adapt_semantic_unit_review import (
    CONTROLLED_SEMANTIC_UNIT_REVIEW_ADAPTER_VERSION,
    ControlledSemanticUnitReviewRejected,
    ControlledSemanticUnitReviewRejectionCode,
    build_controlled_semantic_unit_review_prediction,
)
from app.application.extraction.build_semantic_unit_plan import (
    DEFAULT_SEMANTIC_UNITIZER_VERSION,
    SemanticUnitPlanRejected,
    SemanticUnitPlanRejectionCode,
    build_semantic_unit_plan,
    validate_semantic_unit_plan_against_inputs,
)
from app.application.extraction.classify_content import classify_content
from app.application.extraction.llm_provision_batch import (
    extract_requirement_provisions_with_provider,
)
from app.application.extraction.llm_requirement_review import (
    build_llm_requirement_model_prediction,
    evaluate_llm_requirement_review,
    initial_llm_requirement_review,
    validate_llm_requirement_review,
)
from app.application.extraction.llm_semantics import (
    plan_content_classification_provider_requests,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.unitize_explicit_list import (
    CONTINUOUS_EXPLICIT_LIST_CANDIDATE_UNITIZER_VERSION,
    propose_continuous_explicit_list_partition,
)
from app.domain.extraction.semantic_units import (
    SemanticUnit,
    SemanticUnitKind,
    SemanticUnitMarkerFamily,
    SemanticUnitPartitionDisposition,
    SemanticUnitPartitionReason,
    SemanticUnitPlan,
    stable_semantic_unit_id,
    stable_semantic_unit_plan_id,
)
from app.domain.extraction.semantic_unit_decisions import (
    SemanticUnitDecision,
    SemanticUnitDecisionRequest,
)
from app.domain.extraction.llm_requirement_review import (
    LlmRequirementModelPrediction,
    LlmRequirementReviewedResult,
    LlmReviewSourceSpan,
    LlmReviewedRequirementNode,
)
from app.domain.extraction.requirement_assembly import (
    RequirementAssemblyStatus,
    RequirementAssemblySnapshot,
)
from app.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionRole,
    RequirementViewSnapshot,
)
from app.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticClass,
    SemanticClassificationSnapshot,
    SemanticGroundingSpan,
    stable_semantic_grounding_span_id,
)
from app.domain.extraction.source_ir import SourceNodeKind
from app.domain.extraction.structure import StructureNodeKind
from tests.test_llm_provision_batch import (
    _ProvisionProvider,
    _three_provision_input,
)
from tests.test_semantics import _flat_source


# Frozen against the source-bound planning v4 window/context contract and the
# resolver 2.48.1 / classifier 1.15.1 / projector 1.7.2 runtime identities.
# Provider request keys remain Structure-addressed; SemanticUnit identities stay
# local to the deterministic classification/assembly/review chain.
# Resolver 2.48.1 separates repeated diagnostic occurrences. These fixtures have
# no such gap; replaying with explicit 2.48.0 reproduces both previous digests.
# Only the declared resolver identity changes their newly frozen wire/chain IDs.
_FROZEN_FULL_CHAIN_REPLAY_SHA256 = (
    "fa3fbf6fe657f21925ab30774be12f37491fd79cc80ba372ca0e4ad6ae6b0409"
)
_FROZEN_PROVIDER_WIRE_SHA256 = (
    "698aaadd8d9b56fcd947688b7e73dbe1780bba48fb5e09a64dd8647b9e3f6f46"
)


class SemanticUnitPlanTests(unittest.TestCase):
    def test_full_span_plan_is_stable_source_bound_and_semantics_neutral(
        self,
    ) -> None:
        source, tree, scaffold = _unicode_input()

        first = build_semantic_unit_plan(source, tree, scaffold)
        second = build_semantic_unit_plan(source, tree, scaffold)

        self.assertEqual(first, second)
        self.assertEqual(
            first.scope_owner_structure_node_ids,
            scaffold.scope_structure_node_ids,
        )
        self.assertEqual(
            tuple(item.unit_order for item in first.units),
            tuple(range(len(first.units))),
        )
        self.assertEqual(
            tuple(item.owner_unit_index for item in first.units),
            (0,) * len(first.units),
        )

        nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
        annotations_by_target = {
            item.target_structure_node_id: item for item in scaffold.annotations
        }
        for unit in first.units:
            owner = nodes_by_id[unit.owner_structure_node_id]
            annotation = annotations_by_target[unit.owner_structure_node_id]
            self.assertEqual(unit.source_order, owner.source_order)
            self.assertEqual(
                {item.source_ref for item in unit.source_spans},
                set(owner.source_refs),
            )
            self.assertLessEqual(
                len(unit.source_spans),
                len(annotation.grounding_spans),
            )

        content_unit = first.units[-1]
        content_annotation = scaffold.annotations[-1]
        content_text = source.nodes[-1].raw_text
        self.assertGreater(
            len(content_annotation.grounding_spans),
            len(content_unit.source_spans),
        )
        self.assertEqual(content_unit.source_spans[0].end_offset, len(content_text))
        utf16_code_units = len(content_text.encode("utf-16-le")) // 2
        self.assertNotEqual(content_unit.source_spans[0].end_offset, utf16_code_units)
        self.assertEqual(
            content_unit.parent_semantic_unit_id,
            first.units[0].semantic_unit_id,
        )

        changed_annotation = scaffold.annotations[0].model_copy(
            update={
                "semantic_class": SemanticClass.BACKGROUND,
                "requirement_subtype": None,
                "reason_codes": ("synthetic_semantic_change",),
            }
        )
        changed_scaffold = scaffold.model_copy(
            update={
                "annotations": (
                    changed_annotation,
                    *scaffold.annotations[1:],
                )
            }
        )
        changed = build_semantic_unit_plan(source, tree, changed_scaffold)
        self.assertEqual(changed, first)

    def test_explicit_descendant_scope_retains_out_of_scope_parent_owner(
        self,
    ) -> None:
        source, tree, _scaffold = _unicode_input()
        content = tree.nodes[-1]
        scaffold = classify_content(
            source,
            tree,
            scope_structure_node_ids=(content.structure_node_id,),
        )

        plan = build_semantic_unit_plan(source, tree, scaffold)

        self.assertEqual(len(plan.units), 1)
        self.assertEqual(
            plan.units[0].parent_owner_structure_node_id,
            content.parent_structure_node_id,
        )
        self.assertIsNone(plan.units[0].parent_semantic_unit_id)

    def test_forged_unit_identity_is_rejected_by_domain_contract(self) -> None:
        source, tree, scaffold = _unicode_input()
        plan = build_semantic_unit_plan(source, tree, scaffold)
        units = list(plan.units)
        units[-1] = units[-1].model_copy(
            update={"semantic_unit_id": "semantic-unit-" + "f" * 32}
        )
        payload = plan.model_dump(mode="python")
        payload["units"] = tuple(units)
        payload["semantic_unit_plan_id"] = stable_semantic_unit_plan_id(
            source_ir_id=plan.source_ir_id,
            structure_snapshot_id=plan.structure_snapshot_id,
            unitizer_version=plan.unitizer_version,
            scope_owner_structure_node_ids=plan.scope_owner_structure_node_ids,
            semantic_unit_ids=tuple(item.semantic_unit_id for item in units),
        )

        with self.assertRaisesRegex(
            ValidationError,
            "semantic_unit_id must be derived",
        ):
            SemanticUnitPlan.model_validate(payload)

    def test_owner_and_unit_order_drift_are_rejected_by_domain_contract(
        self,
    ) -> None:
        source, tree, scaffold = _unicode_input()
        plan = build_semantic_unit_plan(source, tree, scaffold)
        units = list(plan.units)

        duplicate_owner_payload = plan.model_dump(mode="python")
        duplicate_owner_payload["units"] = (
            units[0],
            units[1].model_copy(
                update={
                    "owner_structure_node_id": units[0].owner_structure_node_id,
                    "parent_owner_structure_node_id": (tree.nodes[0].structure_node_id),
                    "parent_semantic_unit_id": None,
                }
            ),
        )
        with self.assertRaisesRegex(
            ValidationError,
            "declared scope order",
        ):
            SemanticUnitPlan.model_validate(duplicate_owner_payload)

        duplicate_order_payload = plan.model_dump(mode="python")
        duplicate_order_payload["units"] = (
            units[0],
            units[1].model_copy(update={"unit_order": 0}),
        )
        with self.assertRaisesRegex(
            ValidationError,
            "unit_order must be contiguous",
        ):
            SemanticUnitPlan.model_validate(duplicate_order_payload)

    def test_partial_owner_span_is_rejected_against_verified_inputs(self) -> None:
        source, tree, scaffold = _unicode_input()
        plan = build_semantic_unit_plan(source, tree, scaffold)
        original = plan.units[-1]
        original_span = original.source_spans[0]
        shortened_span = SemanticGroundingSpan(
            grounding_span_id=stable_semantic_grounding_span_id(
                source_ref=original_span.source_ref,
                start_offset=original_span.start_offset,
                end_offset=original_span.end_offset - 1,
            ),
            source_ref=original_span.source_ref,
            start_offset=original_span.start_offset,
            end_offset=original_span.end_offset - 1,
        )
        forged = _replace_unit(
            plan,
            len(plan.units) - 1,
            source_spans=(shortened_span,),
        )

        with self.assertRaises(SemanticUnitPlanRejected) as caught:
            validate_semantic_unit_plan_against_inputs(
                forged,
                source,
                tree,
                scaffold,
            )

        self.assertIs(
            caught.exception.code,
            SemanticUnitPlanRejectionCode.INVALID_GROUNDING,
        )

    def test_parent_drift_is_rejected_against_structure_tree(self) -> None:
        source, tree, scaffold = _unicode_input()
        plan = build_semantic_unit_plan(source, tree, scaffold)
        document = tree.nodes[0]
        forged = _replace_unit(
            plan,
            len(plan.units) - 1,
            parent_owner_structure_node_id=document.structure_node_id,
            parent_semantic_unit_id=None,
        )

        with self.assertRaises(SemanticUnitPlanRejected) as caught:
            validate_semantic_unit_plan_against_inputs(
                forged,
                source,
                tree,
                scaffold,
            )

        self.assertIs(
            caught.exception.code,
            SemanticUnitPlanRejectionCode.INVALID_PARENT,
        )

    def test_classification_plan_bridges_unit_ids_to_unchanged_owner_scope(
        self,
    ) -> None:
        source, tree, _scaffold = _unicode_input()

        classification_plan = plan_content_classification_provider_requests(
            source,
            tree,
        )

        units_by_id = {
            item.semantic_unit_id: item
            for item in classification_plan.semantic_unit_plan.units
        }
        for item in classification_plan.items:
            self.assertEqual(
                tuple(
                    units_by_id[unit_id].owner_structure_node_id
                    for unit_id in item.semantic_unit_ids
                ),
                item.request.scope_structure_node_ids,
            )

    def test_parent_boundary_windows_must_exact_partition_semantic_scope(self) -> None:
        source, tree, _scaffold = _unicode_input()
        baseline = plan_content_classification_provider_requests(source, tree)
        owners = baseline.semantic_unit_plan.scope_owner_structure_node_ids
        windows = (owners[:1], owners[1:]) if len(owners) > 1 else (owners,)

        planned = plan_content_classification_provider_requests(
            source,
            tree,
            target_windows=windows,
        )

        self.assertEqual(
            tuple(
                target
                for item in planned.items
                for target in item.request.scope_structure_node_ids
            ),
            owners,
        )
        self.assertTrue(all(len(item.request.nodes) <= 80 for item in planned.items))
        with self.assertRaisesRegex(ValueError, "exact-partition"):
            plan_content_classification_provider_requests(
                source,
                tree,
                target_windows=(owners[:-1],),
            )

    def test_continuous_explicit_list_candidate_exactly_covers_original_unicode(
        self,
    ) -> None:
        text = "\nＡ） 第一项😀\r\nb) Second item\n（Ｃ） Dritte Position\t\n"
        source, tree, plan, unit = _content_unitization_input(text)

        candidate = propose_continuous_explicit_list_partition(
            source,
            tree,
            unit,
        )

        self.assertIs(
            candidate.disposition,
            SemanticUnitPartitionDisposition.CANDIDATE,
        )
        self.assertIs(
            candidate.reason_code,
            SemanticUnitPartitionReason.SAFE_CONTINUOUS_EXPLICIT_LIST,
        )
        self.assertIs(candidate.marker_family, SemanticUnitMarkerFamily.LATIN_ALPHA)
        self.assertEqual(
            candidate.candidate_unitizer_version,
            CONTINUOUS_EXPLICIT_LIST_CANDIDATE_UNITIZER_VERSION,
        )
        self.assertEqual(len(candidate.proposed_unit_spans), 3)
        self.assertEqual(
            "".join(
                text[span.start_offset : span.end_offset]
                for span in candidate.proposed_unit_spans
            ),
            text,
        )
        self.assertNotEqual(
            candidate.proposed_unit_spans[-1].end_offset,
            len(text.encode("utf-16-le")) // 2,
        )
        self.assertTrue(
            text[
                candidate.proposed_unit_spans[
                    0
                ].start_offset : candidate.proposed_unit_spans[0].end_offset
            ]
            .lstrip()
            .startswith("Ａ）")
        )
        self.assertTrue(
            text[
                candidate.proposed_unit_spans[
                    -1
                ].start_offset : candidate.proposed_unit_spans[-1].end_offset
            ].startswith("（Ｃ）")
        )
        self.assertEqual(
            candidate.parent_owner_structure_node_id,
            unit.parent_owner_structure_node_id,
        )
        self.assertEqual(
            candidate.parent_semantic_unit_id,
            unit.parent_semantic_unit_id,
        )
        self.assertEqual(plan.unitizer_version, DEFAULT_SEMANTIC_UNITIZER_VERSION)
        self.assertEqual(
            tuple(item.unit_kind for item in plan.units),
            (SemanticUnitKind.OWNER_FULL_SPAN,) * len(plan.units),
        )

        second = candidate.proposed_unit_spans[1]
        gap_span = SemanticGroundingSpan(
            grounding_span_id=stable_semantic_grounding_span_id(
                source_ref=second.source_ref,
                start_offset=second.start_offset + 1,
                end_offset=second.end_offset,
            ),
            source_ref=second.source_ref,
            start_offset=second.start_offset + 1,
            end_offset=second.end_offset,
        )
        gap_payload = candidate.model_dump(mode="python")
        gap_payload["proposed_unit_spans"] = (
            candidate.proposed_unit_spans[0],
            gap_span,
            candidate.proposed_unit_spans[2],
        )
        with self.assertRaisesRegex(ValidationError, "exact cover"):
            type(candidate).model_validate(gap_payload)

    def test_mixed_empty_nonconsecutive_and_nonlist_sequences_block(
        self,
    ) -> None:
        cases = (
            (
                "mixed_marker",
                "a) First item\n2) Second item\nc) Third item",
                SemanticUnitPartitionReason.MIXED_MARKER_FAMILY,
            ),
            (
                "empty_body",
                "a) First item\nb) \t\nc) Third item",
                SemanticUnitPartitionReason.EMPTY_ITEM_BODY,
            ),
            (
                "nonconsecutive",
                "a) First item\nc) Third item\nd) Fourth item",
                SemanticUnitPartitionReason.NON_CONSECUTIVE_MARKERS,
            ),
            (
                "surrounding_prose",
                "Introductory context\na) First item\nb) Second item\nc) Third item",
                SemanticUnitPartitionReason.NON_LIST_LINE,
            ),
            (
                "formula_like",
                "a) + x\nb) = y\nc) - z",
                SemanticUnitPartitionReason.FORMULA_LIKE_ITEM,
            ),
        )
        for label, text, reason in cases:
            with self.subTest(label=label):
                source, tree, _plan, unit = _content_unitization_input(text)
                candidate = propose_continuous_explicit_list_partition(
                    source,
                    tree,
                    unit,
                )

                self.assertIs(
                    candidate.disposition,
                    SemanticUnitPartitionDisposition.BLOCKED,
                )
                self.assertIs(candidate.reason_code, reason)
                self.assertEqual(candidate.proposed_unit_spans, ())

    def test_single_inline_reference_and_formula_markers_are_noop(
        self,
    ) -> None:
        cases = (
            (
                "single_marker",
                "a) Only one marked item",
                SemanticUnitPartitionReason.INSUFFICIENT_CONSECUTIVE_ITEMS,
            ),
            (
                "prose_reference",
                "See a) through c) for the referenced alternatives.",
                SemanticUnitPartitionReason.NO_HARD_LINE_MARKER,
            ),
            (
                "formula_variables",
                "Result = a) + b) + c).",
                SemanticUnitPartitionReason.NO_HARD_LINE_MARKER,
            ),
            (
                "inline_markers",
                "Items include a) alpha; b) beta; c) gamma.",
                SemanticUnitPartitionReason.NO_HARD_LINE_MARKER,
            ),
        )
        for label, text, reason in cases:
            with self.subTest(label=label):
                source, tree, _plan, unit = _content_unitization_input(text)
                candidate = propose_continuous_explicit_list_partition(
                    source,
                    tree,
                    unit,
                )

                self.assertIs(
                    candidate.disposition,
                    SemanticUnitPartitionDisposition.NO_OP,
                )
                self.assertIs(candidate.reason_code, reason)
                self.assertEqual(candidate.proposed_unit_spans, ())

    def test_non_content_owner_is_noop_and_cross_source_owner_blocks(self) -> None:
        source, tree, plan, content_unit = _content_unitization_input(
            "a) First item\nb) Second item\nc) Third item"
        )
        section_unit = plan.units[0]

        section_candidate = propose_continuous_explicit_list_partition(
            source,
            tree,
            section_unit,
        )
        self.assertIs(
            section_candidate.disposition,
            SemanticUnitPartitionDisposition.NO_OP,
        )
        self.assertIs(
            section_candidate.reason_code,
            SemanticUnitPartitionReason.OWNER_NOT_CONTENT,
        )

        cross_source_spans = (
            section_unit.source_spans[0],
            content_unit.source_spans[0],
        )
        cross_source_unit = SemanticUnit(
            semantic_unit_id=stable_semantic_unit_id(
                source_ir_id=plan.source_ir_id,
                structure_snapshot_id=plan.structure_snapshot_id,
                owner_structure_node_id=content_unit.owner_structure_node_id,
                unitizer_version=plan.unitizer_version,
                ordered_source_spans=cross_source_spans,
            ),
            owner_structure_node_id=content_unit.owner_structure_node_id,
            parent_owner_structure_node_id=(
                content_unit.parent_owner_structure_node_id
            ),
            parent_semantic_unit_id=content_unit.parent_semantic_unit_id,
            source_order=content_unit.source_order,
            unit_order=content_unit.unit_order,
            owner_unit_index=content_unit.owner_unit_index,
            unit_kind=content_unit.unit_kind,
            source_spans=cross_source_spans,
        )
        cross_source_candidate = propose_continuous_explicit_list_partition(
            source,
            tree,
            cross_source_unit,
        )

        self.assertIs(
            cross_source_candidate.disposition,
            SemanticUnitPartitionDisposition.BLOCKED,
        )
        self.assertIs(
            cross_source_candidate.reason_code,
            SemanticUnitPartitionReason.CROSS_SOURCE_REF_OWNER,
        )
        self.assertEqual(cross_source_candidate.proposed_unit_spans, ())

        resolver_failure = propose_continuous_explicit_list_partition(
            source,
            tree,
            content_unit,
            text_resolver=lambda _ref, _source: 1 / 0,
        )
        self.assertIs(
            resolver_failure.disposition,
            SemanticUnitPartitionDisposition.BLOCKED,
        )
        self.assertIs(
            resolver_failure.reason_code,
            SemanticUnitPartitionReason.TEXT_RESOLUTION_FAILED,
        )
        self.assertEqual(resolver_failure.proposed_unit_spans, ())

        native_text = source.nodes[-1].raw_text
        same_length_mismatch = propose_continuous_explicit_list_partition(
            source,
            tree,
            content_unit,
            text_resolver=lambda _ref, _source: "x" * len(native_text),
        )
        self.assertIs(
            same_length_mismatch.disposition,
            SemanticUnitPartitionDisposition.BLOCKED,
        )
        self.assertIs(
            same_length_mismatch.reason_code,
            SemanticUnitPartitionReason.OWNER_TEXT_VIEW_MISMATCH,
        )
        self.assertEqual(same_length_mismatch.proposed_unit_spans, ())

    def test_controlled_candidate_builds_stable_multi_unit_plan_and_requests(
        self,
    ) -> None:
        text = "a) First item😀\nb) Second item\nc) Third item"
        source, tree, current_plan, owner_unit = _content_unitization_input(text)
        candidate = propose_continuous_explicit_list_partition(
            source,
            tree,
            owner_unit,
        )

        first = build_controlled_semantic_unit_plan(
            source,
            tree,
            current_plan,
            candidate,
        )
        second = build_controlled_semantic_unit_plan(
            source,
            tree,
            current_plan,
            candidate,
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first.unitizer_version,
            CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION,
        )
        self.assertEqual(
            first.scope_owner_structure_node_ids,
            current_plan.scope_owner_structure_node_ids,
        )
        self.assertNotEqual(
            first.semantic_unit_plan_id,
            current_plan.semantic_unit_plan_id,
        )
        split_units = tuple(
            item
            for item in first.units
            if item.owner_structure_node_id == owner_unit.owner_structure_node_id
        )
        self.assertEqual(len(split_units), 3)
        self.assertEqual(
            tuple(item.owner_unit_index for item in split_units),
            (0, 1, 2),
        )
        self.assertEqual(
            tuple(item.unit_kind for item in split_units),
            (SemanticUnitKind.CONTINUOUS_EXPLICIT_LIST_ITEM,) * 3,
        )
        self.assertEqual(
            tuple(item.source_order for item in split_units),
            (owner_unit.source_order,) * 3,
        )
        parent_units = tuple(
            item
            for item in first.units
            if item.owner_structure_node_id == owner_unit.parent_owner_structure_node_id
        )
        expected_parent_unit_id = (
            parent_units[0].semantic_unit_id if parent_units else None
        )
        self.assertEqual(
            tuple(item.parent_semantic_unit_id for item in split_units),
            (expected_parent_unit_id,) * 3,
        )
        self.assertEqual(
            tuple(item.unit_order for item in first.units),
            tuple(range(len(first.units))),
        )

        decision_plan = plan_controlled_semantic_unit_decisions(
            source,
            tree,
            first,
        )
        self.assertEqual(len(decision_plan.requests), len(first.units))
        self.assertEqual(
            tuple(item.semantic_unit_id for item in decision_plan.requests),
            first.semantic_unit_ids,
        )
        split_requests = tuple(
            item
            for item in decision_plan.requests
            if item.owner_structure_node_id == owner_unit.owner_structure_node_id
        )
        self.assertEqual(len(split_requests), 3)
        self.assertEqual(
            "".join(
                evidence.text
                for request in split_requests
                for evidence in request.evidence
            ),
            text,
        )
        self.assertEqual(
            tuple(request.evidence[0].grounding_span for request in split_requests),
            candidate.proposed_unit_spans,
        )

        drift_payload = first.model_dump(mode="python")
        drift_units = list(first.units)
        split_index = drift_units.index(split_units[1])
        drift_units[split_index] = split_units[1].model_copy(
            update={"owner_unit_index": 0}
        )
        drift_payload["units"] = tuple(drift_units)
        with self.assertRaisesRegex(ValidationError, "owner_unit_index"):
            SemanticUnitPlan.model_validate(drift_payload)

    def test_controlled_provider_returns_one_decision_per_unit(self) -> None:
        source, tree, decision_plan = _controlled_decision_input()
        provider = _ControlledUnitProvider()

        execution = classify_controlled_semantic_units(
            decision_plan,
            provider,
        )

        self.assertEqual(tuple(provider.requests), decision_plan.requests)
        self.assertEqual(len(execution.decisions), len(decision_plan.requests))
        self.assertEqual(
            tuple(item.semantic_unit_id for item in execution.decisions),
            decision_plan.semantic_unit_plan.semantic_unit_ids,
        )
        self.assertEqual(
            tuple(item.decision_request_id for item in execution.decisions),
            tuple(item.decision_request_id for item in decision_plan.requests),
        )
        for request, decision in zip(
            decision_plan.requests,
            execution.decisions,
            strict=True,
        ):
            self.assertEqual(
                decision.evidence_grounding_span_ids,
                tuple(
                    item.grounding_span.grounding_span_id for item in request.evidence
                ),
            )

        rebuilt = plan_controlled_semantic_unit_decisions(
            source,
            tree,
            decision_plan.semantic_unit_plan,
        )
        self.assertEqual(rebuilt, decision_plan)

    def test_controlled_adapter_fails_closed_on_ineligible_or_mismatched_result(
        self,
    ) -> None:
        source, tree, current_plan, owner_unit = _content_unitization_input(
            "a) Only one item"
        )
        no_op = propose_continuous_explicit_list_partition(
            source,
            tree,
            owner_unit,
        )
        with self.assertRaises(ControlledSemanticUnitAdapterRejected) as caught:
            build_controlled_semantic_unit_plan(
                source,
                tree,
                current_plan,
                no_op,
            )
        self.assertIs(
            caught.exception.code,
            ControlledSemanticUnitAdapterRejectionCode.CANDIDATE_NOT_ELIGIBLE,
        )

        _source, _tree, decision_plan = _controlled_decision_input()
        for mode in ("unit", "request", "evidence"):
            with self.subTest(mode=mode):
                with self.assertRaises(
                    ControlledSemanticUnitAdapterRejected
                ) as result_caught:
                    classify_controlled_semantic_units(
                        decision_plan,
                        _MismatchedControlledUnitProvider(mode),
                    )
                self.assertIs(
                    result_caught.exception.code,
                    ControlledSemanticUnitAdapterRejectionCode.PROVIDER_RESULT_MISMATCH,
                )

    def test_controlled_multi_unit_identity_reaches_semantics_assembly_and_view(
        self,
    ) -> None:
        source, tree, decision_plan = _controlled_decision_input()
        execution = classify_controlled_semantic_units(
            decision_plan,
            _ControlledUnitProvider(),
        )
        unit_plan = decision_plan.semantic_unit_plan
        directives = _controlled_downstream_directives(unit_plan)

        first = build_controlled_semantic_unit_downstream(
            source,
            tree,
            execution,
            directives,
        )
        second = build_controlled_semantic_unit_downstream(
            source,
            tree,
            execution,
            directives,
        )

        self.assertEqual(first, second)
        self.assertEqual(first.semantics.schema_version, "semantic-classification/v3")
        self.assertEqual(first.assembly.schema_version, "requirement-assembly/v3")
        self.assertEqual(first.requirement_view.schema_version, "requirement-view/v3")
        self.assertEqual(
            first.semantics.scope_semantic_unit_ids,
            unit_plan.semantic_unit_ids,
        )
        self.assertEqual(
            first.semantics.scope_owner_structure_node_ids,
            tuple(item.owner_structure_node_id for item in unit_plan.units),
        )
        self.assertEqual(
            tuple(item.grounding_spans for item in first.semantics.annotations),
            tuple(item.source_spans for item in unit_plan.units),
        )

        split_units = tuple(
            item
            for item in unit_plan.units
            if item.unit_kind is SemanticUnitKind.CONTINUOUS_EXPLICIT_LIST_ITEM
        )
        self.assertEqual(len(split_units), 3)
        split_owner_ids = tuple(item.owner_structure_node_id for item in split_units)
        self.assertEqual(len(set(split_owner_ids)), 1)
        self.assertEqual(
            first.semantics.scope_owner_structure_node_ids.count(split_owner_ids[0]),
            3,
        )

        included_items = tuple(
            item for item in first.assembly.items if item.include_in_view
        )
        self.assertEqual(
            tuple(item.semantic_unit_id for item in included_items),
            tuple(item.semantic_unit_id for item in split_units),
        )
        self.assertEqual(
            tuple(item.unit_order for item in included_items),
            tuple(item.unit_order for item in split_units),
        )
        self.assertEqual(
            tuple(item.source_order for item in included_items),
            (split_units[0].source_order,) * 3,
        )
        self.assertEqual(
            tuple(item.owner_structure_node_id for item in included_items),
            split_owner_ids,
        )
        self.assertEqual(
            tuple(item.role for item in included_items),
            (
                RequirementProjectionRole.PRIMARY,
                RequirementProjectionRole.CHILD,
                RequirementProjectionRole.CHILD,
            ),
        )
        self.assertEqual(
            tuple(item.parent_semantic_unit_id for item in included_items),
            (
                None,
                split_units[0].semantic_unit_id,
                split_units[0].semantic_unit_id,
            ),
        )

        view_nodes = first.requirement_view.nodes
        self.assertEqual(
            tuple(item.semantic_unit_id for item in view_nodes),
            tuple(item.semantic_unit_id for item in split_units),
        )
        self.assertEqual(
            tuple(item.unit_order for item in view_nodes),
            tuple(item.unit_order for item in split_units),
        )
        self.assertEqual(
            tuple(item.decision_mode for item in view_nodes),
            (
                RequirementDecisionMode.ROLLUP,
                RequirementDecisionMode.DIRECT,
                RequirementDecisionMode.DIRECT,
            ),
        )
        self.assertEqual(
            tuple(item.source_span_ids for item in view_nodes),
            tuple(
                tuple(span.grounding_span_id for span in unit.source_spans)
                for unit in split_units
            ),
        )
        self.assertEqual(
            tuple(item.parent_requirement_view_node_id for item in view_nodes),
            (
                None,
                view_nodes[0].requirement_view_node_id,
                view_nodes[0].requirement_view_node_id,
            ),
        )

    def test_controlled_multi_unit_identity_reaches_prediction_and_review(
        self,
    ) -> None:
        source, tree, execution, downstream, prediction, review = (
            _controlled_review_chain()
        )
        repeated = build_controlled_semantic_unit_review_prediction(
            source,
            tree,
            execution,
            downstream,
        )
        plan = execution.plan.semantic_unit_plan
        split_units = tuple(
            unit
            for unit in plan.units
            if unit.unit_kind is SemanticUnitKind.CONTINUOUS_EXPLICIT_LIST_ITEM
        )
        split_unit_ids = tuple(unit.semantic_unit_id for unit in split_units)
        split_owner_id = split_units[0].owner_structure_node_id
        prediction_by_unit = {node.semantic_unit_id: node for node in prediction.nodes}
        review_by_unit = {
            node.source_semantic_unit_ids[0]: node
            for node in review.nodes
            if len(node.source_semantic_unit_ids) == 1
        }
        requests_by_unit = {
            request.semantic_unit_id: request for request in execution.plan.requests
        }

        self.assertEqual(prediction, repeated)
        self.assertEqual(prediction.schema_version, "llm-requirement-prediction/v5")
        self.assertEqual(review.schema_version, "llm-requirement-review/v4")
        self.assertEqual(prediction.semantic_unit_ids, plan.semantic_unit_ids)
        self.assertEqual(
            tuple(node.semantic_unit_id for node in prediction.nodes),
            plan.semantic_unit_ids,
        )
        self.assertEqual(
            tuple(
                prediction_by_unit[unit_id].source_order for unit_id in split_unit_ids
            ),
            (split_units[0].source_order,) * 3,
        )
        self.assertEqual(
            tuple(prediction_by_unit[unit_id].unit_order for unit_id in split_unit_ids),
            tuple(unit.unit_order for unit in split_units),
        )
        self.assertEqual(
            tuple(
                prediction_by_unit[unit_id].owner_structure_node_id
                for unit_id in split_unit_ids
            ),
            (split_owner_id,) * 3,
        )
        self.assertEqual(
            tuple(
                prediction_by_unit[unit_id].review_text for unit_id in split_unit_ids
            ),
            tuple(
                "".join(
                    evidence.text for evidence in requests_by_unit[unit_id].evidence
                )
                for unit_id in split_unit_ids
            ),
        )
        self.assertEqual(
            "".join(
                prediction_by_unit[unit_id].review_text for unit_id in split_unit_ids
            ),
            "a) First item😀\nb) Second item\nc) Third item",
        )
        self.assertEqual(
            tuple(review_by_unit[unit_id].review_node_id for unit_id in split_unit_ids),
            split_unit_ids,
        )
        self.assertEqual(
            tuple(
                review_by_unit[unit_id].parent_review_node_id
                for unit_id in split_unit_ids
            ),
            (None, split_unit_ids[0], split_unit_ids[0]),
        )
        self.assertEqual(
            tuple(
                review_by_unit[unit_id].source_spans[0].end_offset
                for unit_id in split_unit_ids
            ),
            tuple(
                len(prediction_by_unit[unit_id].review_text)
                for unit_id in split_unit_ids
            ),
        )
        self.assertEqual(
            prediction.classification_prompt_version,
            "semantic-unit-decision/v1",
        )
        self.assertEqual(
            CONTROLLED_SEMANTIC_UNIT_REVIEW_ADAPTER_VERSION,
            "controlled-semantic-unit-review-adapter/v1",
        )

    def test_controlled_same_owner_review_can_split_and_merge_without_losing_lineage(
        self,
    ) -> None:
        _source, _tree, execution, _downstream, prediction, initial = (
            _controlled_review_chain()
        )
        split_units = tuple(
            unit
            for unit in execution.plan.semantic_unit_plan.units
            if unit.unit_kind is SemanticUnitKind.CONTINUOUS_EXPLICIT_LIST_ITEM
        )
        child_unit_ids = tuple(unit.semantic_unit_id for unit in split_units[1:])
        initial_by_unit = {
            node.source_semantic_unit_ids[0]: node
            for node in initial.nodes
            if len(node.source_semantic_unit_ids) == 1
        }

        original = initial_by_unit[child_unit_ids[0]]
        original_span = original.source_spans[0]
        split_at = max(1, original_span.end_offset // 2)
        first_piece = LlmReviewedRequirementNode.model_validate(
            {
                **original.model_dump(mode="python"),
                "review_node_id": "controlled-same-owner-split-first",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=original_span.semantic_unit_id,
                        owner_structure_node_id=(original_span.owner_structure_node_id),
                        start_offset=0,
                        end_offset=split_at,
                    ),
                ),
            }
        )
        second_piece = LlmReviewedRequirementNode.model_validate(
            {
                **original.model_dump(mode="python"),
                "review_node_id": "controlled-same-owner-split-second",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=original_span.semantic_unit_id,
                        owner_structure_node_id=(original_span.owner_structure_node_id),
                        start_offset=split_at,
                        end_offset=original_span.end_offset,
                    ),
                ),
            }
        )
        original_index = initial.nodes.index(original)
        split_review = LlmRequirementReviewedResult.model_validate(
            {
                **initial.model_dump(mode="python"),
                "revision": 1,
                "nodes": (
                    *initial.nodes[:original_index],
                    first_piece,
                    second_piece,
                    *initial.nodes[original_index + 1 :],
                ),
            }
        )

        validate_llm_requirement_review(prediction, split_review)
        split_delta = evaluate_llm_requirement_review(prediction, split_review)
        self.assertEqual(split_delta.metrics.split_target_count, 1)
        self.assertEqual(
            next(
                operation
                for operation in split_delta.operations
                if operation.operation.value == "SPLIT"
            ).source_semantic_unit_ids,
            (child_unit_ids[0],),
        )

        left = initial_by_unit[child_unit_ids[0]]
        right = initial_by_unit[child_unit_ids[1]]
        shared_owner_id = left.source_owner_structure_node_ids[0]
        merged = LlmReviewedRequirementNode.model_validate(
            {
                **left.model_dump(mode="python"),
                "source_semantic_unit_ids": child_unit_ids,
                "source_owner_structure_node_ids": (
                    shared_owner_id,
                    shared_owner_id,
                ),
                "source_spans": (*left.source_spans, *right.source_spans),
            }
        )
        left_index = initial.nodes.index(left)
        right_index = initial.nodes.index(right)
        merged_review = LlmRequirementReviewedResult.model_validate(
            {
                **initial.model_dump(mode="python"),
                "revision": 1,
                "nodes": (
                    *initial.nodes[:left_index],
                    merged,
                    *initial.nodes[right_index + 1 :],
                ),
            }
        )

        validate_llm_requirement_review(prediction, merged_review)
        merged_delta = evaluate_llm_requirement_review(prediction, merged_review)
        merge_operation = next(
            operation
            for operation in merged_delta.operations
            if operation.operation.value == "MERGED"
        )
        self.assertEqual(merged.source_semantic_unit_ids, child_unit_ids)
        self.assertEqual(
            merged.source_owner_structure_node_ids,
            (shared_owner_id, shared_owner_id),
        )
        self.assertEqual(merge_operation.source_semantic_unit_ids, child_unit_ids)
        self.assertEqual(
            merge_operation.source_owner_structure_node_ids,
            (shared_owner_id, shared_owner_id),
        )

    def test_controlled_review_adapter_rejects_absorbed_multi_unit_lineage(
        self,
    ) -> None:
        source, tree, execution, downstream, _prediction, _review = (
            _controlled_review_chain()
        )
        split_items = tuple(
            item for item in downstream.assembly.items if item.include_in_view
        )
        first, second = split_items[:2]
        tampered_first = first.model_copy(
            update={
                "source_semantic_unit_ids": (
                    first.semantic_unit_id,
                    second.semantic_unit_id,
                ),
                "source_owner_structure_node_ids": (
                    first.owner_structure_node_id,
                    second.owner_structure_node_id,
                ),
            }
        )
        tampered_assembly = downstream.assembly.model_copy(
            update={
                "items": tuple(
                    tampered_first if item is first else item
                    for item in downstream.assembly.items
                )
            }
        )
        tampered = ControlledSemanticUnitDownstreamExecution(
            semantic_unit_plan=downstream.semantic_unit_plan,
            semantics=downstream.semantics,
            assembly=tampered_assembly,
            requirement_view=downstream.requirement_view,
        )

        with self.assertRaises(ControlledSemanticUnitReviewRejected) as caught:
            build_controlled_semantic_unit_review_prediction(
                source,
                tree,
                execution,
                tampered,
            )

        self.assertIs(
            caught.exception.code,
            ControlledSemanticUnitReviewRejectionCode.UNSUPPORTED_LINEAGE,
        )

    def test_controlled_downstream_rejects_unproved_lineage_and_parentage(
        self,
    ) -> None:
        source, tree, decision_plan = _controlled_decision_input()
        execution = classify_controlled_semantic_units(
            decision_plan,
            _ControlledUnitProvider(),
        )
        unit_plan = decision_plan.semantic_unit_plan
        semantics = build_controlled_semantic_classification(
            source,
            tree,
            execution,
        )
        directives = _controlled_downstream_directives(unit_plan)
        split_directives = tuple(item for item in directives if item.include_in_view)
        excluded = tuple(item for item in directives if not item.include_in_view)

        invalid_directive_sets = {
            "reordered": tuple(reversed(directives)),
            "unproved_absorption": (
                *excluded,
                split_directives[0].model_copy(
                    update={
                        "absorbed_semantic_unit_ids": (
                            split_directives[1].semantic_unit_id,
                        )
                    }
                ),
                split_directives[2],
            ),
            "interleaved_absorption": (
                *excluded,
                split_directives[0].model_copy(
                    update={
                        "absorbed_semantic_unit_ids": (
                            split_directives[2].semantic_unit_id,
                        )
                    }
                ),
                split_directives[1],
            ),
            "excluded_parent": (
                *excluded,
                split_directives[0].model_copy(
                    update={
                        "role": RequirementProjectionRole.CHILD,
                        "parent_semantic_unit_id": excluded[0].semantic_unit_id,
                    }
                ),
                *split_directives[1:],
            ),
        }
        for case, invalid_directives in invalid_directive_sets.items():
            with self.subTest(case=case):
                with self.assertRaises(
                    ControlledSemanticUnitDownstreamRejected
                ) as caught:
                    assemble_controlled_semantic_units(
                        source,
                        tree,
                        unit_plan,
                        semantics,
                        invalid_directives,
                    )
                self.assertIs(
                    caught.exception.code,
                    ControlledSemanticUnitDownstreamRejectionCode.INVALID_ASSEMBLY,
                )

    def test_provider_wire_remains_structure_addressed_and_frozen(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _WireCaptureProvider()

        extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )

        payload = {
            "classification_requests": [
                item.model_dump(mode="json")
                for item in provider.classification_requests
            ],
            "assembly_requests": [
                item.model_dump(mode="json") for item in provider.assembly_requests
            ],
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            _FROZEN_PROVIDER_WIRE_SHA256,
        )
        self.assertNotIn("semantic_unit_id", serialized)
        self.assertIn("target_structure_node_id", serialized)
        self.assertIn("scope_structure_node_ids", serialized)

    def test_legacy_identity_schemas_require_regeneration(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider()
        batch = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )
        extraction = next(
            item.extraction for item in batch.provisions if item.extraction is not None
        )
        prediction = build_llm_requirement_model_prediction(
            source,
            tree,
            batch.located_scope,
            batch,
        )
        review = initial_llm_requirement_review(prediction)
        contracts = (
            (
                extraction.semantics,
                SemanticClassificationSnapshot,
                "semantic-classification/v2",
            ),
            (
                extraction.assembly,
                RequirementAssemblySnapshot,
                "requirement-assembly/v2",
            ),
            (
                extraction.requirement_view,
                RequirementViewSnapshot,
                "requirement-view/v2",
            ),
            (
                prediction,
                LlmRequirementModelPrediction,
                "llm-requirement-prediction/v4",
            ),
            (
                review,
                LlmRequirementReviewedResult,
                "llm-requirement-review/v3",
            ),
        )
        for value, model_type, legacy_version in contracts:
            with self.subTest(legacy_version=legacy_version):
                payload = value.model_dump(mode="json")
                payload["schema_version"] = legacy_version
                with self.assertRaises(ValidationError):
                    model_type.model_validate(payload)

    def test_semantic_unit_identity_flows_through_the_full_review_chain(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        batch = extract_requirement_provisions_with_provider(
            source,
            tree,
            _ProvisionProvider(),
            locator,
        )
        extractions = tuple(
            item.extraction for item in batch.provisions if item.extraction is not None
        )
        prediction = build_llm_requirement_model_prediction(
            source,
            tree,
            batch.located_scope,
            batch,
        )
        review = initial_llm_requirement_review(prediction)

        expected_unit_ids = tuple(
            unit_id
            for extraction in extractions
            for unit_id in extraction.semantics.scope_semantic_unit_ids
        )
        expected_plan_ids = tuple(
            extraction.semantics.semantic_unit_plan_id for extraction in extractions
        )
        self.assertEqual(prediction.semantic_unit_ids, expected_unit_ids)
        self.assertEqual(prediction.semantic_unit_plan_ids, expected_plan_ids)

        for extraction in extractions:
            semantics = extraction.semantics
            assembly = extraction.assembly
            view = extraction.requirement_view
            self.assertEqual(
                assembly.semantic_unit_plan_id,
                semantics.semantic_unit_plan_id,
            )
            self.assertEqual(
                {
                    unit_id
                    for item in assembly.items
                    for unit_id in item.source_semantic_unit_ids
                },
                set(semantics.scope_semantic_unit_ids),
            )
            self.assertEqual(
                view.semantic_unit_plan_id,
                semantics.semantic_unit_plan_id,
            )
            self.assertEqual(
                set(view.semantic_unit_ids),
                set(semantics.scope_semantic_unit_ids),
            )

        owner_by_unit_id = {
            unit_id: owner_id
            for node in prediction.nodes
            for unit_id, owner_id in zip(
                node.source_semantic_unit_ids,
                node.source_owner_structure_node_ids,
                strict=True,
            )
        }
        reviewed_unit_ids = tuple(
            unit_id
            for node in review.nodes
            for unit_id in node.source_semantic_unit_ids
        )
        self.assertEqual(reviewed_unit_ids, prediction.semantic_unit_ids)
        for node in review.nodes:
            for span in node.source_spans:
                self.assertEqual(
                    span.owner_structure_node_id,
                    owner_by_unit_id[span.semantic_unit_id],
                )

    def test_full_span_insertion_preserves_frozen_full_chain_replay(self) -> None:
        source, tree, locator = _three_provision_input()
        provider = _ProvisionProvider()

        batch = extract_requirement_provisions_with_provider(
            source,
            tree,
            provider,
            locator,
        )
        prediction = build_llm_requirement_model_prediction(
            source,
            tree,
            batch.located_scope,
            batch,
        )
        review = initial_llm_requirement_review(prediction)
        payload = {
            "classification_requests": [
                item.model_dump(mode="json")
                for item in provider.classification_requests
            ],
            "extractions": [
                {
                    "semantics": item.extraction.semantics.model_dump(mode="json"),
                    "assembly": item.extraction.assembly.model_dump(mode="json"),
                    "requirement_view": (
                        item.extraction.requirement_view.model_dump(mode="json")
                    ),
                }
                for item in batch.provisions
                if item.extraction is not None
            ],
            "prediction": prediction.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        self.assertEqual(digest, _FROZEN_FULL_CHAIN_REPLAY_SHA256)


class _WireCaptureProvider(_ProvisionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.assembly_requests: list[object] = []

    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        self.assembly_requests.append(request)
        return super().assemble_requirements(request)


class _ControlledUnitProvider:
    provider_id = "synthetic-controlled-unit-provider"
    provider_version = "1.0.0"
    model_id = "synthetic-controlled-unit-model"

    def __init__(self) -> None:
        self.requests: list[SemanticUnitDecisionRequest] = []

    def classify_semantic_unit(
        self,
        request: SemanticUnitDecisionRequest,
    ) -> SemanticUnitDecision:
        self.requests.append(request)
        return SemanticUnitDecision(
            decision_request_id=request.decision_request_id,
            semantic_unit_id=request.semantic_unit_id,
            owner_structure_node_id=request.owner_structure_node_id,
            semantic_class=SemanticClass.REQUIREMENT,
            requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
            evidence_grounding_span_ids=tuple(
                item.grounding_span.grounding_span_id for item in request.evidence
            ),
            reason_codes=("synthetic_unit_decision",),
        )


class _MismatchedControlledUnitProvider(_ControlledUnitProvider):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def classify_semantic_unit(
        self,
        request: SemanticUnitDecisionRequest,
    ) -> SemanticUnitDecision:
        decision = super().classify_semantic_unit(request)
        updates: dict[str, object] = {}
        if self.mode == "unit":
            updates["semantic_unit_id"] = "semantic-unit-" + "f" * 32
        elif self.mode == "request":
            updates["decision_request_id"] = (
                "semantic-unit-decision-request-" + "f" * 32
            )
        else:
            updates["evidence_grounding_span_ids"] = ("semantic-span-" + "f" * 32,)
        return decision.model_copy(update=updates)


def _unicode_input():  # type: ignore[no-untyped-def]
    source, _ = _flat_source(
        (
            (SourceNodeKind.HEADING, "1 语义😀"),
            (
                SourceNodeKind.PARAGRAPH,
                "a) 温度😀应保持。\nb) Pressure shall remain.",
            ),
        )
    )
    tree = build_structure_tree(source)
    scaffold = classify_content(source, tree)
    return source, tree, scaffold


def _content_unitization_input(text):  # type: ignore[no-untyped-def]
    # High-confidence paragraph/list runs are now exact Structure CLAUSE peers.
    # This helper deliberately uses a nested native paragraph to exercise the
    # separate controlled semantic-unit fallback without freezing the old
    # top-level parser miss or changing the unitizer's native-text contract.
    source, _ = _flat_source(
        (
            (SourceNodeKind.HEADING, "1 Synthetic list"),
            (SourceNodeKind.PARAGRAPH, text),
        )
    )
    source = source.model_copy(
        update={
            "nodes": (
                source.nodes[0],
                source.nodes[1].model_copy(
                    update={
                        "parent_source_node_id": source.nodes[0].source_node_id,
                    }
                ),
            )
        }
    )
    tree = build_structure_tree(source)
    scaffold = classify_content(source, tree)
    plan = build_semantic_unit_plan(source, tree, scaffold)
    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    content_unit = next(
        item
        for item in plan.units
        if nodes_by_id[item.owner_structure_node_id].kind is StructureNodeKind.CONTENT
    )
    return source, tree, plan, content_unit


def _controlled_decision_input():  # type: ignore[no-untyped-def]
    source, tree, current_plan, owner_unit = _content_unitization_input(
        "a) First item😀\nb) Second item\nc) Third item"
    )
    candidate = propose_continuous_explicit_list_partition(
        source,
        tree,
        owner_unit,
    )
    controlled_plan = build_controlled_semantic_unit_plan(
        source,
        tree,
        current_plan,
        candidate,
    )
    return (
        source,
        tree,
        plan_controlled_semantic_unit_decisions(
            source,
            tree,
            controlled_plan,
        ),
    )


def _controlled_review_chain():  # type: ignore[no-untyped-def]
    source, tree, decision_plan = _controlled_decision_input()
    execution = classify_controlled_semantic_units(
        decision_plan,
        _ControlledUnitProvider(),
    )
    downstream = build_controlled_semantic_unit_downstream(
        source,
        tree,
        execution,
        _controlled_downstream_directives(decision_plan.semantic_unit_plan),
    )
    prediction = build_controlled_semantic_unit_review_prediction(
        source,
        tree,
        execution,
        downstream,
    )
    review = initial_llm_requirement_review(prediction)
    return source, tree, execution, downstream, prediction, review


def _controlled_downstream_directives(
    plan: SemanticUnitPlan,
) -> tuple[ControlledSemanticUnitAssemblyDirective, ...]:
    split_units = tuple(
        item
        for item in plan.units
        if item.unit_kind is SemanticUnitKind.CONTINUOUS_EXPLICIT_LIST_ITEM
    )
    primary_unit_id = split_units[0].semantic_unit_id
    directives: list[ControlledSemanticUnitAssemblyDirective] = []
    for unit in plan.units:
        if unit not in split_units:
            directives.append(
                ControlledSemanticUnitAssemblyDirective(
                    semantic_unit_id=unit.semantic_unit_id,
                    include_in_view=False,
                    status=RequirementAssemblyStatus.CANDIDATE,
                    reason_codes=("synthetic_context_excluded",),
                )
            )
            continue
        role = (
            RequirementProjectionRole.PRIMARY
            if unit.semantic_unit_id == primary_unit_id
            else RequirementProjectionRole.CHILD
        )
        directives.append(
            ControlledSemanticUnitAssemblyDirective(
                semantic_unit_id=unit.semantic_unit_id,
                include_in_view=True,
                role=role,
                parent_semantic_unit_id=(
                    None
                    if role is RequirementProjectionRole.PRIMARY
                    else primary_unit_id
                ),
                status=RequirementAssemblyStatus.CANDIDATE,
                reason_codes=("synthetic_list_projection",),
            )
        )
    return tuple(directives)


def _replace_unit(
    plan: SemanticUnitPlan,
    unit_index: int,
    **updates: object,
) -> SemanticUnitPlan:
    units = list(plan.units)
    original = units[unit_index]
    updated_spans = updates.get("source_spans", original.source_spans)
    assert isinstance(updated_spans, tuple)
    updates["semantic_unit_id"] = stable_semantic_unit_id(
        source_ir_id=plan.source_ir_id,
        structure_snapshot_id=plan.structure_snapshot_id,
        owner_structure_node_id=original.owner_structure_node_id,
        unitizer_version=plan.unitizer_version,
        ordered_source_spans=updated_spans,
    )
    units[unit_index] = original.model_copy(update=updates)
    payload = plan.model_dump(mode="python")
    payload["units"] = tuple(units)
    payload["semantic_unit_plan_id"] = stable_semantic_unit_plan_id(
        source_ir_id=plan.source_ir_id,
        structure_snapshot_id=plan.structure_snapshot_id,
        unitizer_version=plan.unitizer_version,
        scope_owner_structure_node_ids=plan.scope_owner_structure_node_ids,
        semantic_unit_ids=tuple(item.semantic_unit_id for item in units),
    )
    return SemanticUnitPlan.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
