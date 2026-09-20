from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.application.extraction.native_atomization_reference import (
    NativeAtomizationContextDisposition,
    NativeAtomizationContextRangeV1,
    NativeAtomizationReferenceStatus,
    NativeAtomizationReferenceV1,
)
from app.application.extraction.native_atomization_repeatability import (
    NativeAtomizationRepeatabilityStatus,
    build_native_atomization_consensus,
    evaluate_native_atomization_repeatability,
    select_native_atomization_consensus_canonical,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationRange,
    RequirementAtomizationSourceSegment,
)


def _request(text: str, suffix: str) -> RequirementAtomizationProviderRequest:
    return RequirementAtomizationProviderRequest(
        lineage_sha256=suffix * 64,
        review_node_id=f"review-{suffix}",
        requirement_subtype="TECHNICAL_REQUIREMENT",
        source_segments=(
            RequirementAtomizationSourceSegment(
                segment_index=0,
                semantic_unit_id="semantic-unit-" + suffix * 32,
                owner_structure_node_id="structure-node-" + suffix * 32,
                source_start_offset=0,
                source_end_offset=len(text),
                source_text=text,
            ),
        ),
    )


def _obligation(
    start: int, end: int, statement: str
) -> RequirementAtomizationObligation:
    return RequirementAtomizationObligation(
        source_ranges=(
            RequirementAtomizationRange(
                segment_index=0,
                start_offset=start,
                end_offset=end,
            ),
        ),
        atomic_statement=statement,
        split_basis="independent_obligation",
        reason_codes=("independent_obligation",),
    )


def _reference(
    request: RequirementAtomizationProviderRequest,
    ranges: tuple[tuple[int, int], ...],
) -> NativeAtomizationReferenceV1:
    return NativeAtomizationReferenceV1(
        lineage_sha256=request.lineage_sha256,
        review_node_id=request.review_node_id,
        status=NativeAtomizationReferenceStatus.OBSERVED,
        obligations=tuple(
            _obligation(start, end, f"obligation {index}")
            for index, (start, end) in enumerate(ranges, 1)
        ),
    )


class NativeAtomizationRepeatabilityTests(unittest.TestCase):
    def test_same_source_bound_skeleton_is_stable_across_whitespace(self) -> None:
        first_text = "A shall X; B shall Y."
        second_text = "A shall X;  B shall Y."
        first = _request(first_text, "1")
        second = _request(second_text, "2")
        first_split = first_text.index("B")
        second_split = second_text.index("B")

        report = evaluate_native_atomization_repeatability(
            (
                (
                    "first",
                    first,
                    _reference(
                        first, ((0, first_split), (first_split, len(first_text)))
                    ),
                ),
                (
                    "second",
                    second,
                    _reference(
                        second,
                        ((0, second_split), (second_split, len(second_text))),
                    ),
                ),
            )
        )

        self.assertEqual(report.status, NativeAtomizationRepeatabilityStatus.STABLE)
        self.assertTrue(report.exact_decision_skeleton_match)
        self.assertEqual(report.stable_obligation_count, 2)
        self.assertEqual(report.consensus_obligation_count, 2)

    def test_different_granularity_is_reported_as_unstable(self) -> None:
        text = "A shall X; B shall Y."
        first = _request(text, "1")
        second = _request(text, "2")
        split = text.index("B")

        report = evaluate_native_atomization_repeatability(
            (
                (
                    "split",
                    first,
                    _reference(first, ((0, split), (split, len(text)))),
                ),
                (
                    "combined",
                    second,
                    _reference(second, ((0, len(text)),)),
                ),
            )
        )

        self.assertEqual(report.status, NativeAtomizationRepeatabilityStatus.UNSTABLE)
        self.assertFalse(report.exact_decision_skeleton_match)
        self.assertEqual(report.union_obligation_count, 3)
        self.assertEqual(len(report.unstable_obligation_source_sha256s), 3)
        self.assertEqual(report.consensus_obligation_count, 0)

    def test_three_runs_form_strict_majority_consensus(self) -> None:
        text = "A shall X; B shall Y."
        split = text.index("B")
        first = _request(text, "1")
        second = _request(text, "2")
        third = _request(text, "3")

        report = evaluate_native_atomization_repeatability(
            (
                ("split_1", first, _reference(first, ((0, split), (split, len(text))))),
                ("combined", second, _reference(second, ((0, len(text)),))),
                ("split_2", third, _reference(third, ((0, split), (split, len(text))))),
            )
        )

        self.assertEqual(report.status, NativeAtomizationRepeatabilityStatus.UNSTABLE)
        self.assertEqual(report.consensus_threshold, 2)
        self.assertEqual(report.consensus_obligation_count, 2)
        self.assertEqual(len(report.non_consensus_obligation_source_sha256s), 1)

    def test_canonical_selection_does_not_anchor_to_first_outlier(self) -> None:
        text = "A shall X; B shall Y."
        split = text.index("B")
        request = _request(text, "1")
        combined = _reference(request, ((0, len(text)),))
        split_reference = _reference(
            request,
            ((0, split), (split, len(text))),
        )
        references = (combined, split_reference, split_reference)
        report = evaluate_native_atomization_repeatability(
            tuple(
                (f"run_{index}", request, reference)
                for index, reference in enumerate(references, 1)
            )
        )

        canonical = select_native_atomization_consensus_canonical(
            request,
            references,
            report,
        )
        consensus = build_native_atomization_consensus(
            request,
            canonical,
            report,
        )

        self.assertIs(canonical, split_reference)
        self.assertEqual(len(consensus.obligations), 2)
        self.assertEqual(consensus.status, NativeAtomizationReferenceStatus.OBSERVED)

    def test_consensus_keeps_majority_obligations_and_marks_rest_unresolved(
        self,
    ) -> None:
        text = "A shall X; B shall Y."
        split = text.index("B")
        first = _request(text, "1")
        second = _request(text, "2")
        third = _request(text, "3")
        canonical = _reference(first, ((0, split), (split, len(text))))
        second_reference = NativeAtomizationReferenceV1(
            lineage_sha256=second.lineage_sha256,
            review_node_id=second.review_node_id,
            status=NativeAtomizationReferenceStatus.OBSERVED,
            obligations=(_obligation(0, split, "A shall X."),),
            context_ranges=(
                NativeAtomizationContextRangeV1(
                    source_range=RequirementAtomizationRange(
                        segment_index=0,
                        start_offset=split,
                        end_offset=len(text),
                    ),
                    disposition=NativeAtomizationContextDisposition.CONTEXT,
                    reason_codes=("non_obligation_context",),
                ),
            ),
        )
        report = evaluate_native_atomization_repeatability(
            (
                ("canonical", first, canonical),
                ("first_only", second, second_reference),
                ("combined", third, _reference(third, ((0, len(text)),))),
            )
        )

        consensus = build_native_atomization_consensus(first, canonical, report)

        self.assertEqual(len(consensus.obligations), 1)
        self.assertEqual(consensus.status, NativeAtomizationReferenceStatus.PARTIAL)
        self.assertIn(
            "non_consensus_ranges_unresolved",
            consensus.diagnostic_codes,
        )
        self.assertTrue(
            any(
                item.disposition is NativeAtomizationContextDisposition.UNRESOLVED
                for item in consensus.context_ranges
            )
        )

    def test_different_normalized_sources_are_rejected(self) -> None:
        first = _request("A shall X.", "1")
        second = _request("A shall Y.", "2")

        with self.assertRaisesRegex(ValueError, "same normalized source"):
            evaluate_native_atomization_repeatability(
                (
                    ("first", first, _reference(first, ((0, 10),))),
                    ("second", second, _reference(second, ((0, 10),))),
                )
            )


if __name__ == "__main__":
    unittest.main()
