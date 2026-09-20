from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest

from pydantic import ValidationError

from app.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryReferenceV1,
    NativeParentBoundaryRequestV1,
    NativeParentBoundarySourceSegmentV1,
    NativeParentBoundaryStatus,
    NativeParentContextDisposition,
    NativeParentContextRangeV1,
    NativeProvisionDisposition,
    NativeProvisionBlockV1,
    canonicalize_native_parent_boundary_reference,
    native_parent_boundary_output_schema,
    validate_native_parent_boundary_reference,
)
from app.ports.requirement_atomization_provider import RequirementAtomizationRange


TEXT = (
    "2 Supplier requirements\n"
    "2.1 Software\n"
    "1) Supplier shall maintain a security team.\n"
    "2) Supplier shall obtain customer approval."
)
FIRST_START = TEXT.index("Supplier shall maintain")


def _request() -> NativeParentBoundaryRequestV1:
    return NativeParentBoundaryRequestV1(
        source_sha256="1" * 64,
        scope_id="synthetic-parent-scope",
        source_segments=(
            NativeParentBoundarySourceSegmentV1(
                segment_index=0,
                page_number=1,
                evidence_channel="NATIVE_TEXT",
                source_ref="page-text-1",
                source_text=TEXT,
                source_text_sha256=hashlib.sha256(TEXT.encode()).hexdigest(),
            ),
        ),
    )


def _range(start: int, end: int) -> RequirementAtomizationRange:
    return RequirementAtomizationRange(
        segment_index=0,
        start_offset=start,
        end_offset=end,
    )


def _reference() -> NativeParentBoundaryReferenceV1:
    return NativeParentBoundaryReferenceV1(
        source_sha256="1" * 64,
        scope_id="synthetic-parent-scope",
        status=NativeParentBoundaryStatus.OBSERVED,
        provisions=(
            NativeProvisionBlockV1(
                provision_id="native-provision-" + "a" * 32,
                explicit_label="2.1",
                ancestor_labels=("2",),
                source_order=0,
                disposition=NativeProvisionDisposition.REQUIREMENT,
                body_ranges=(_range(FIRST_START, len(TEXT)),),
                reason_codes=("complete_parent_with_nested_list",),
            ),
        ),
        context_ranges=(
            NativeParentContextRangeV1(
                source_range=_range(0, FIRST_START),
                disposition=NativeParentContextDisposition.HEADING,
                reason_codes=("heading_and_list_marker",),
            ),
        ),
    )


class NativeParentBoundaryReferenceTests(unittest.TestCase):
    def test_host_derives_parent_identity_from_source_ranges(self) -> None:
        reference = _reference()
        alternate = reference.model_copy(
            update={
                "provisions": (
                    reference.provisions[0].model_copy(
                        update={"provision_id": "native-provision-" + "b" * 32}
                    ),
                )
            }
        )

        first = canonicalize_native_parent_boundary_reference(_request(), reference)
        second = canonicalize_native_parent_boundary_reference(_request(), alternate)

        self.assertEqual(
            first.provisions[0].provision_id,
            second.provisions[0].provision_id,
        )
        self.assertNotEqual(
            first.provisions[0].provision_id,
            reference.provisions[0].provision_id,
        )

    def test_valid_reference_exact_covers_scope(self) -> None:
        receipt = validate_native_parent_boundary_reference(
            _request(),
            _reference(),
        )

        self.assertEqual(receipt.provision_count, 1)
        self.assertEqual(receipt.context_range_count, 1)
        self.assertEqual(len(receipt.grounding_sha256), 64)
        self.assertEqual(len(receipt.decision_sha256), 64)

    def test_validator_rejects_overlapping_parent_ranges(self) -> None:
        reference = _reference()
        overlapping_context = reference.context_ranges[0].model_copy(
            update={"source_range": _range(0, FIRST_START + 2)}
        )
        invalid = reference.model_copy(
            update={"context_ranges": (overlapping_context,)}
        )

        with self.assertRaisesRegex(ValueError, "ranges overlap"):
            validate_native_parent_boundary_reference(_request(), invalid)

    def test_validator_rejects_uncovered_non_whitespace(self) -> None:
        reference = _reference().model_copy(update={"context_ranges": ()})

        with self.assertRaisesRegex(ValueError, "does not exact-cover"):
            validate_native_parent_boundary_reference(_request(), reference)

    def test_ancestor_labels_preserve_logical_order(self) -> None:
        provision = _reference().provisions[0]
        self.assertEqual(provision.ancestor_labels, ("2",))

    def test_multiple_nested_list_items_cannot_replace_the_parent_block(self) -> None:
        parent = _reference().provisions[0]
        first = parent.model_copy(
            update={
                "explicit_label": "1",
                "provision_id": "native-provision-" + "c" * 32,
            }
        )
        second = parent.model_copy(
            update={
                "explicit_label": "2",
                "provision_id": "native-provision-" + "d" * 32,
                "source_order": 1,
            }
        )

        with self.assertRaisesRegex(
            ValidationError,
            "nested list items must remain inside one complete parent provision",
        ):
            NativeParentBoundaryReferenceV1.model_validate(
                {
                    **_reference().model_dump(mode="json"),
                    "provisions": [
                        first.model_dump(mode="json"),
                        second.model_dump(mode="json"),
                    ],
                }
            )

    def test_provision_label_cannot_repeat_in_ancestor_path(self) -> None:
        payload = _reference().provisions[0].model_dump(mode="json")
        payload["ancestor_labels"] = ["2", "2.1"]

        with self.assertRaisesRegex(
            ValidationError,
            "cannot also appear in its ancestor labels",
        ):
            NativeProvisionBlockV1.model_validate(payload)

    def test_output_schema_is_strict(self) -> None:
        schema = native_parent_boundary_output_schema()
        provision = schema["$defs"]["NativeProvisionBlockV1"]
        self.assertEqual(
            provision["properties"]["reason_codes"]["items"]["pattern"],
            "^[a-z][a-z0-9_]{0,119}$",
        )
        self.assertEqual(schema["required"], list(schema["properties"]))


if __name__ == "__main__":
    unittest.main()
