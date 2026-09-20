from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from pydantic import ValidationError

from app.application.extraction.native_atomization_reference import (
    NativeAtomizationContextDisposition,
    NativeAtomizationContextRangeV1,
    NativeAtomizationReferenceStatus,
    NativeAtomizationReferenceV1,
    native_atomization_reference_output_schema,
    validate_native_atomization_reference,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationProviderRequest,
    RequirementAtomizationRange,
    RequirementAtomizationSourceSegment,
)


LINEAGE_SHA256 = "1" * 64
TEXT = "The unit shall operate at 24 V; test method per Standard X."
METHOD_START = TEXT.index("test method")


def _request() -> RequirementAtomizationProviderRequest:
    return RequirementAtomizationProviderRequest(
        lineage_sha256=LINEAGE_SHA256,
        review_node_id="review-node-1",
        requirement_subtype="TECHNICAL_REQUIREMENT",
        parent_context_statement="",
        source_segments=(
            RequirementAtomizationSourceSegment(
                segment_index=0,
                semantic_unit_id="semantic-unit-" + "a" * 32,
                owner_structure_node_id="structure-node-" + "b" * 32,
                source_start_offset=0,
                source_end_offset=len(TEXT),
                source_text=TEXT,
            ),
        ),
    )


def _obligation(start: int, end: int) -> RequirementAtomizationObligation:
    return RequirementAtomizationObligation(
        source_ranges=(
            RequirementAtomizationRange(
                segment_index=0,
                start_offset=start,
                end_offset=end,
            ),
        ),
        atomic_statement="The unit shall operate at 24 V.",
        split_basis="independent_obligation",
        reason_codes=("independent_obligation",),
    )


def _context(start: int, end: int) -> NativeAtomizationContextRangeV1:
    return NativeAtomizationContextRangeV1(
        source_range=RequirementAtomizationRange(
            segment_index=0,
            start_offset=start,
            end_offset=end,
        ),
        disposition=NativeAtomizationContextDisposition.METHOD,
        reason_codes=("verification_method",),
    )


class NativeAtomizationReferenceTests(unittest.TestCase):
    def test_valid_reference_exact_covers_source_and_builds_hash_receipt(self) -> None:
        reference = NativeAtomizationReferenceV1(
            lineage_sha256=LINEAGE_SHA256,
            review_node_id="review-node-1",
            status=NativeAtomizationReferenceStatus.OBSERVED,
            obligations=(_obligation(0, METHOD_START),),
            context_ranges=(_context(METHOD_START, len(TEXT)),),
        )

        receipt = validate_native_atomization_reference(_request(), reference)

        self.assertEqual(receipt.obligation_count, 1)
        self.assertEqual(receipt.context_range_count, 1)
        self.assertEqual(len(receipt.grounding_sha256), 64)
        self.assertEqual(len(receipt.decision_sha256), 64)
        self.assertFalse(receipt.is_human_gold)

    def test_validator_rejects_overlapping_ranges(self) -> None:
        reference = NativeAtomizationReferenceV1(
            lineage_sha256=LINEAGE_SHA256,
            review_node_id="review-node-1",
            status=NativeAtomizationReferenceStatus.OBSERVED,
            obligations=(_obligation(0, METHOD_START + 2),),
            context_ranges=(_context(METHOD_START, len(TEXT)),),
        )

        with self.assertRaisesRegex(ValueError, "ranges overlap"):
            validate_native_atomization_reference(_request(), reference)

    def test_validator_rejects_missing_non_whitespace_source(self) -> None:
        reference = NativeAtomizationReferenceV1(
            lineage_sha256=LINEAGE_SHA256,
            review_node_id="review-node-1",
            status=NativeAtomizationReferenceStatus.OBSERVED,
            obligations=(_obligation(0, METHOD_START),),
            context_ranges=(_context(METHOD_START + 5, len(TEXT)),),
        )

        with self.assertRaisesRegex(ValueError, "does not exact-cover"):
            validate_native_atomization_reference(_request(), reference)

    def test_partial_status_requires_an_unresolved_range(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "status does not match unresolved ranges",
        ):
            NativeAtomizationReferenceV1(
                lineage_sha256=LINEAGE_SHA256,
                review_node_id="review-node-1",
                status=NativeAtomizationReferenceStatus.PARTIAL,
                obligations=(_obligation(0, METHOD_START),),
                context_ranges=(_context(METHOD_START, len(TEXT)),),
            )

    def test_output_schema_requires_every_property(self) -> None:
        schema = native_atomization_reference_output_schema()

        def assert_required(value: object) -> None:
            if isinstance(value, list):
                for item in value:
                    assert_required(item)
                return
            if not isinstance(value, dict):
                return
            properties = value.get("properties")
            if isinstance(properties, dict):
                self.assertEqual(value["required"], list(properties))
                self.assertFalse(value["additionalProperties"])
            for item in value.values():
                assert_required(item)

        assert_required(schema)
        obligation = schema["$defs"]["RequirementAtomizationObligation"]
        self.assertEqual(
            obligation["properties"]["split_basis"]["enum"],
            ["single_or_descriptive", "independent_obligation"],
        )
        self.assertEqual(
            obligation["properties"]["reason_codes"]["items"]["pattern"],
            "^[a-z][a-z0-9_]{0,119}$",
        )


if __name__ == "__main__":
    unittest.main()
