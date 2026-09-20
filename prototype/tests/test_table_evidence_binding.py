from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest

from pydantic import ValidationError

from app.domain.extraction.llm_requirement_review import LlmReviewSourceSpan
from app.domain.extraction.table_evidence import (
    EvidenceReferenceKind,
    EvidenceReferenceV1,
    RequirementEvidenceBindingV1,
    RequirementEvidenceBundleV1,
    RequirementEvidenceSourceRefV1,
    TableEvidenceBindingV1,
    canonical_requirement_evidence_bundle_sha256,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _span(index: int) -> LlmReviewSourceSpan:
    return LlmReviewSourceSpan(
        semantic_unit_id=f"semantic-unit-{index:032x}",
        owner_structure_node_id=f"structure-node-{index:032x}",
        start_offset=0,
        end_offset=12,
    )


def _reference(
    kind: EvidenceReferenceKind,
    reference_id: str,
    *,
    row_start: int | None = None,
    row_end: int | None = None,
) -> EvidenceReferenceV1:
    return EvidenceReferenceV1(
        kind=kind,
        reference_id=reference_id,
        resolver_key=f"source-ir/table-001/{reference_id}",
        content_sha256=_sha256(reference_id),
        page_number=3,
        row_start=row_start,
        row_end=row_end,
    )


def _table_binding(
    *,
    record_id: str | None = "table-record-001-row-0007",
) -> TableEvidenceBindingV1:
    return TableEvidenceBindingV1.create(
        logical_table_id="logical-table-001",
        source_table_ids=("table-page-003", "table-page-004"),
        projection_id="table-record-projection-001",
        projection_revision=2,
        projection_sha256=_sha256("projection-v2"),
        record_id=record_id,
        source_spans=(_span(1),),
        table_refs=(
            _reference(
                EvidenceReferenceKind.TABLE,
                "table-page-003",
                row_start=0,
                row_end=20,
            ),
            _reference(
                EvidenceReferenceKind.TABLE,
                "table-page-004",
                row_start=0,
                row_end=8,
            ),
        ),
        content_block_refs=(
            _reference(
                EvidenceReferenceKind.CONTENT_BLOCK,
                "content-block-table-001",
            ),
        ),
        header_refs=(
            _reference(
                EvidenceReferenceKind.HEADER,
                "cell-header-requirement",
                row_start=0,
                row_end=1,
            ),
        ),
        shared_value_refs=(
            _reference(
                EvidenceReferenceKind.SHARED_VALUE,
                "cell-shared-test-condition",
                row_start=2,
                row_end=9,
            ),
        ),
        note_refs=(
            _reference(EvidenceReferenceKind.NOTE, "table-note-001"),
        ),
        image_refs=(
            _reference(EvidenceReferenceKind.IMAGE, "cell-image-001"),
        ),
    )


class TableEvidenceBindingTests(unittest.TestCase):
    def test_table_evidence_round_trips_and_hashes_deterministically(self) -> None:
        binding = _table_binding()

        restored = TableEvidenceBindingV1.model_validate_json(
            binding.model_dump_json()
        )
        rebuilt = _table_binding()

        self.assertEqual(restored, binding)
        self.assertEqual(rebuilt.binding_id, binding.binding_id)
        self.assertEqual(rebuilt.evidence_sha256, binding.evidence_sha256)
        self.assertEqual(restored.record_id, "table-record-001-row-0007")
        self.assertEqual(
            restored.header_refs[0].reference_id,
            "cell-header-requirement",
        )
        self.assertEqual(
            restored.shared_value_refs[0].reference_id,
            "cell-shared-test-condition",
        )
        self.assertEqual(restored.note_refs[0].reference_id, "table-note-001")
        self.assertEqual(restored.image_refs[0].reference_id, "cell-image-001")

    def test_table_record_is_independent_and_can_yield_zero_to_many_atoms(
        self,
    ) -> None:
        table_binding = _table_binding()

        no_atoms = RequirementEvidenceBundleV1.create(
            source_ir_id="source-ir-001",
            source_sha256=_sha256("source"),
            structure_snapshot_id="effective-structure-001",
            table_bindings=(table_binding,),
        )
        self.assertEqual(no_atoms.table_bindings, (table_binding,))
        self.assertEqual(no_atoms.requirement_bindings, ())

        atom_a = RequirementEvidenceBindingV1.create(
            requirement_entry_id="requirement-entry-001",
            atomic_requirement_id="atomic-obligation-001",
            source_spans=(_span(2),),
            table_evidence_binding_id=table_binding.binding_id,
        )
        atom_b = RequirementEvidenceBindingV1.create(
            requirement_entry_id="requirement-entry-001",
            atomic_requirement_id="atomic-obligation-002",
            source_spans=(_span(3),),
            table_evidence_binding_id=table_binding.binding_id,
        )
        two_atoms = RequirementEvidenceBundleV1.create(
            source_ir_id="source-ir-001",
            source_sha256=_sha256("source"),
            structure_snapshot_id="effective-structure-001",
            table_bindings=(table_binding,),
            requirement_bindings=(atom_a, atom_b),
        )

        self.assertEqual(len(two_atoms.requirement_bindings), 2)
        self.assertEqual(
            {
                item.table_evidence_binding_id
                for item in two_atoms.requirement_bindings
            },
            {table_binding.binding_id},
        )
        self.assertFalse(hasattr(table_binding, "requirement_entry_id"))
        self.assertFalse(hasattr(table_binding, "atomic_requirement_id"))

    def test_non_table_requirement_keeps_exact_atom_source_spans(self) -> None:
        source_span = _span(4)
        requirement = RequirementEvidenceBindingV1.create(
            requirement_entry_id="requirement-entry-text-001",
            atomic_requirement_id="atomic-obligation-text-001",
            source_spans=(source_span,),
        )
        bundle = RequirementEvidenceBundleV1.create(
            source_ir_id="source-ir-001",
            source_sha256=_sha256("source"),
            structure_snapshot_id="effective-structure-001",
            requirement_bindings=(requirement,),
        )

        restored = RequirementEvidenceBundleV1.model_validate_json(
            bundle.model_dump_json()
        )

        self.assertIsNone(
            restored.requirement_bindings[0].table_evidence_binding_id
        )
        self.assertEqual(
            restored.requirement_bindings[0].source_spans,
            (source_span,),
        )
        self.assertEqual(
            canonical_requirement_evidence_bundle_sha256(restored),
            canonical_requirement_evidence_bundle_sha256(bundle),
        )

    def test_local_non_table_requirement_freezes_source_ref_without_text(
        self,
    ) -> None:
        source_ref = RequirementEvidenceSourceRefV1(
            kind="SOURCE_NODE",
            source_id="source-node-001",
            owner_structure_node_id="structure-node-" + "6" * 32,
            start_offset=3,
            end_offset=19,
            page_start=5,
            page_end=5,
        )
        requirement = RequirementEvidenceBindingV1.create(
            requirement_entry_id="requirement-entry-local-001",
            source_refs=(source_ref,),
        )
        bundle = RequirementEvidenceBundleV1.create(
            source_ir_id="source-ir-001",
            source_sha256=_sha256("source"),
            structure_snapshot_id="effective-structure-001",
            requirement_bindings=(requirement,),
        )

        restored = RequirementEvidenceBundleV1.model_validate_json(
            bundle.model_dump_json()
        )

        self.assertEqual(
            restored.requirement_bindings[0].source_refs,
            (source_ref,),
        )
        self.assertFalse(
            hasattr(restored.requirement_bindings[0].source_refs[0], "text")
        )

    def test_evidence_hash_rejects_tampering(self) -> None:
        binding = _table_binding()
        payload = binding.model_dump(mode="json")
        payload["record_id"] = "table-record-001-row-9999"

        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            TableEvidenceBindingV1.model_validate(payload)

        requirement = RequirementEvidenceBindingV1.create(
            requirement_entry_id="requirement-entry-local-002",
            source_refs=(
                RequirementEvidenceSourceRefV1(
                    kind="source_node",
                    source_id="source-node-002",
                    page_start=2,
                    page_end=2,
                ),
            ),
        )
        requirement_payload = requirement.model_dump(mode="json")
        requirement_payload["source_refs"][0]["source_id"] = (
            "source-node-tampered"
        )
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            RequirementEvidenceBindingV1.model_validate(requirement_payload)

    def test_bundle_rejects_unknown_table_binding_pointer(self) -> None:
        requirement = RequirementEvidenceBindingV1.create(
            requirement_entry_id="requirement-entry-001",
            source_spans=(_span(5),),
            table_evidence_binding_id=(
                "table-evidence-binding-"
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
        )

        with self.assertRaisesRegex(ValidationError, "unknown table binding"):
            RequirementEvidenceBundleV1.create(
                source_ir_id="source-ir-001",
                source_sha256=_sha256("source"),
                structure_snapshot_id="effective-structure-001",
                requirement_bindings=(requirement,),
            )

    def test_reference_kind_prevents_header_data_from_being_misfiled(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "header_refs accepts only HEADER",
        ):
            TableEvidenceBindingV1.create(
                logical_table_id="logical-table-001",
                source_table_ids=("table-page-003",),
                projection_id="table-record-projection-001",
                projection_revision=0,
                projection_sha256=_sha256("projection"),
                table_refs=(
                    _reference(EvidenceReferenceKind.TABLE, "table-page-003"),
                ),
                header_refs=(
                    _reference(EvidenceReferenceKind.NOTE, "not-a-header"),
                ),
            )


if __name__ == "__main__":
    unittest.main()
