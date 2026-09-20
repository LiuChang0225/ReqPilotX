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
from datetime import datetime, timezone
from pathlib import Path

from app.application.extraction.requirement_enrichment_publication_v2 import (
    build_requirement_enrichment_publication_builder_entries_v2,
    build_requirement_enrichment_publication_projection_v2,
    overlay_requirement_enrichment_publication_tree_v2,
)
from app.application.extraction.requirement_tree import build_requirement_tree_snapshot
from app.application.requirement_matrix import (
    _default_disposition,
    _is_analysis_candidate,
)
from app.domain.atomization_scope import (
    AtomizationScopeOperation,
    AtomizationScopeReceiptV1,
    AtomizationTargetScope,
    canonical_atomization_scope_receipt_sha256,
    stable_atomization_scope_receipt_id,
    stable_effective_entry_classification_sha256,
)
from app.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    EntryClassificationStatus,
    EntryClassificationType,
    accept_entry_classification_snapshot,
    build_initial_entry_classification_snapshot,
    canonical_entry_classification_payload_sha256,
    revise_entry_classification_snapshot,
)
from app.domain.extraction.llm_requirement_review import LlmReviewSourceSpan
from app.domain.extraction.requirement_enrichment import (
    RequirementAtomizationNode,
    RequirementAtomizationNodeKind,
    RequirementAtomizationRun,
    RequirementEnrichmentLineage,
    RequirementPublicationEntry,
    RequirementPublicationEntryKind,
    RequirementPublicationPreview,
    RequirementRefinementBinding,
    RequirementRefinementStatus,
    stable_atomic_node_id,
    stable_atomization_node_id,
    stable_atomization_run_id,
    stable_publication_entry_id,
    stable_publication_key,
    stable_publication_lineage_sha256,
    stable_publication_preview_id,
    stable_requirement_enrichment_lineage_sha256,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeSourceIdentity,
    RequirementTreeState,
    RequirementTreeStructureIdentity,
)
from app.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionRole,
)
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.domain.extraction.semantics import RequirementSubtype
from app.domain.extraction.table_evidence import (
    EvidenceReferenceKind,
    EvidenceReferenceV1,
    RequirementEvidenceBindingV1,
    RequirementEvidenceBundleV1,
    RequirementEvidenceSourceRefV1,
    TableEvidenceBindingV1,
)
from app.domain.requirement_enrichment_publication_v2 import (
    RequirementEnrichmentAnalysisDispositionV2,
    RequirementEnrichmentAtomizationStateV2,
    RequirementEnrichmentPublicationBuilderKindV2,
    RequirementEnrichmentPublicationEntryKindV2,
)
from app.domain.requirement_matrix import MatrixNodeDisposition


_PROJECT_ID = "project-publication-v2"
_DOCUMENT_ID = "document-publication-v2"
_SOURCE_SHA256 = "a" * 64
_SOURCE_IR_ID = "source-ir-publication-v2"
_STRUCTURE_ID = f"effective-structure-{'b' * 32}"
_STRUCTURE_NODE_ID = f"structure-node-{'1' * 32}"
_TITLE_NODE_ID = f"structure-node-{'2' * 32}"
_TECHNICAL_NODE_ID = "requirement:technical-one"
_NON_TECHNICAL_NODE_ID = "requirement:non-technical-one"
_INFORMATION_NODE_ID = "requirement:information-one"
_TABLE_RECORD_NODE_ID = "table-record-one"
_TECHNICAL_REVIEW_ID = "review-technical-one"
_SECOND_TECHNICAL_NODE_ID = "requirement:technical-two"
_SECOND_TECHNICAL_REVIEW_ID = "review-technical-two"
_PUBLICATION_BUILDER_ROLES = (
    "STRUCTURE_PROXY",
    "DIRECT_CONTENT",
    "WHOLE_TABLE_REVIEW",
    "TABLE_RECORD",
    "REQUIREMENT",
)


class RequirementEnrichmentPublicationProjectionV2Tests(unittest.TestCase):
    def test_frontend_canonical_hash_fixture_matches_python_contract(self) -> None:
        fixture_path = (
            Path(__file__).resolve().parents[1]
            / "contracts"
            / "fixtures"
            / "semantic_publication_v2_canonical_hash.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        canonical = json.dumps(
            fixture["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), fixture["sha256"])
        numeric_canonical = json.dumps(
            fixture["numeric_payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(numeric_canonical, fixture["numeric_canonical_json"])
        self.assertEqual(
            hashlib.sha256(numeric_canonical.encode("utf-8")).hexdigest(),
            fixture["numeric_sha256"],
        )
        unicode_key_canonical = json.dumps(
            fixture["unicode_key_payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            unicode_key_canonical,
            fixture["unicode_key_canonical_json"],
        )
        self.assertEqual(
            hashlib.sha256(unicode_key_canonical.encode("utf-8")).hexdigest(),
            fixture["unicode_key_sha256"],
        )

    def test_accepted_classification_drives_db_and_analysis_semantics(self) -> None:
        working_tree = _accepted_working_tree()
        classification = _accepted_classification(working_tree)

        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
        )

        rows = {row.requirement_tree_node_id: row for row in projection.rows}
        technical = rows[_TECHNICAL_NODE_ID]
        self.assertEqual(
            technical.entry_kind,
            RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT,
        )
        self.assertTrue(technical.intrinsic_analysis_eligible)
        self.assertEqual(
            technical.default_disposition,
            RequirementEnrichmentAnalysisDispositionV2.ANALYZE,
        )
        self.assertEqual(technical.final_projection_role, "REQUIREMENT")
        self.assertEqual(technical.builder_binding_role, "REQUIREMENT")

        non_technical = rows[_NON_TECHNICAL_NODE_ID]
        self.assertEqual(
            non_technical.entry_kind,
            RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT,
        )
        self.assertTrue(non_technical.intrinsic_analysis_eligible)
        self.assertEqual(
            non_technical.default_disposition,
            RequirementEnrichmentAnalysisDispositionV2.RETAIN,
        )
        self.assertEqual(non_technical.final_projection_role, "DIRECT_CONTENT")
        self.assertEqual(non_technical.decision_mode, RequirementDecisionMode.DIRECT)

        information = rows[_INFORMATION_NODE_ID]
        self.assertEqual(
            information.entry_kind,
            RequirementEnrichmentPublicationEntryKindV2.REFERENCE,
        )
        self.assertFalse(information.intrinsic_analysis_eligible)
        self.assertEqual(information.final_projection_role, "DIRECT_CONTENT")
        self.assertEqual(information.builder_binding_role, "DIRECT_CONTENT")

        technical_table = rows[_TABLE_RECORD_NODE_ID]
        self.assertEqual(
            technical_table.entry_kind,
            RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT,
        )
        self.assertTrue(technical_table.intrinsic_analysis_eligible)
        self.assertEqual(
            technical_table.default_disposition,
            RequirementEnrichmentAnalysisDispositionV2.ANALYZE,
        )
        self.assertEqual(technical_table.final_projection_role, "REQUIREMENT")
        self.assertEqual(technical_table.builder_binding_role, "TABLE_RECORD")

    def test_projection_defaults_follow_existing_matrix_rules(self) -> None:
        working_tree = _accepted_working_tree()
        classification = _accepted_classification(working_tree)
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
        )
        source_by_id = {node.id: node for node in working_tree.nodes}

        for row in projection.rows:
            source = source_by_id[row.requirement_tree_node_id]
            if row.final_projection_role == "REQUIREMENT":
                final_kind = "REQUIREMENT"
            elif row.final_projection_role == "DIRECT_CONTENT":
                final_kind = "DIRECT_CONTENT"
            else:
                final_kind = source.kind
            final_node = source.model_copy(
                update={
                    "kind": final_kind,
                    "projection_role": row.final_projection_role,
                    "classification": row.classification,
                    "editable_structure_node_ids": (),
                }
            )
            requirement = {
                "id": f"database-{row.requirement_tree_node_id}",
                "entry_kind": row.entry_kind.value,
                "decision_reference": {
                    "decision_mode": row.decision_mode.value,
                },
            }
            candidate = _is_analysis_candidate(
                tree_node=final_node,
                requirement=requirement,
            )
            disposition = _default_disposition(
                entry_kind=row.entry_kind.value,
                projection_role=row.final_projection_role,
                analysis_candidate=candidate,
                persisted_state=None,
            )
            self.assertEqual(
                disposition,
                MatrixNodeDisposition(row.default_disposition.value),
            )
            if row.intrinsic_analysis_eligible:
                self.assertTrue(candidate)

    def test_every_builder_role_obeys_accepted_classification_matrix(self) -> None:
        working_tree = _role_classification_working_tree()
        classification = _accepted_role_classification(working_tree)
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
        )
        source_by_id = {node.id: node for node in working_tree.nodes}

        for row in projection.rows:
            effective_type = EntryClassificationType(row.classification)
            with self.subTest(
                builder_role=row.builder_binding_role,
                classification=effective_type.value,
            ):
                self.assertIn(
                    row.builder_binding_role,
                    _PUBLICATION_BUILDER_ROLES,
                )
                if effective_type is EntryClassificationType.TECHNICAL:
                    expected_kind = (
                        RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
                    )
                    expected_role = "REQUIREMENT"
                    expected_disposition = (
                        RequirementEnrichmentAnalysisDispositionV2.ANALYZE
                    )
                    expected_eligible = True
                elif effective_type is EntryClassificationType.NON_TECHNICAL:
                    expected_kind = (
                        RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
                    )
                    expected_role = "DIRECT_CONTENT"
                    expected_disposition = (
                        RequirementEnrichmentAnalysisDispositionV2.RETAIN
                    )
                    expected_eligible = True
                else:
                    expected_kind = (
                        RequirementEnrichmentPublicationEntryKindV2.REFERENCE
                    )
                    expected_role = row.builder_binding_role
                    expected_disposition = (
                        RequirementEnrichmentAnalysisDispositionV2.RETAIN
                    )
                    expected_eligible = False
                self.assertEqual(row.entry_kind, expected_kind)
                self.assertEqual(row.final_projection_role, expected_role)
                self.assertEqual(row.default_disposition, expected_disposition)
                self.assertEqual(
                    row.intrinsic_analysis_eligible,
                    expected_eligible,
                )

                source = source_by_id[row.requirement_tree_node_id]
                final_node = source.model_copy(
                    update={
                        "kind": (
                            "REQUIREMENT"
                            if expected_role == "REQUIREMENT"
                            else "DIRECT_CONTENT"
                            if expected_role == "DIRECT_CONTENT"
                            else source.kind
                        ),
                        "projection_role": expected_role,
                        "classification": effective_type.value,
                        "child_ids": (),
                        "structure_node_id": (
                            source.parent_id
                            if expected_role == "DIRECT_CONTENT"
                            else source.structure_node_id
                        ),
                        "table_record_id": (
                            source.table_record_id
                            if expected_role == "TABLE_RECORD"
                            else None
                        ),
                    }
                )
                candidate = _is_analysis_candidate(
                    tree_node=final_node,
                    requirement={
                        "id": f"database-{row.requirement_tree_node_id}",
                        "entry_kind": row.entry_kind.value,
                        "decision_reference": {
                            "decision_mode": row.decision_mode.value,
                        },
                    },
                )
                if effective_type is not EntryClassificationType.INFORMATION:
                    self.assertTrue(candidate)
                self.assertEqual(
                    _default_disposition(
                        entry_kind=row.entry_kind.value,
                        projection_role=row.final_projection_role,
                        analysis_candidate=candidate,
                        persisted_state=None,
                    ),
                    MatrixNodeDisposition(expected_disposition.value),
                )

    def test_overlay_materializes_role_classification_matrix_semantics(self) -> None:
        working_tree = _role_classification_working_tree()
        classification = _accepted_role_classification(working_tree)
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
        )
        requirement_entry_ids = {
            row.requirement_tree_node_id: f"role-matrix-database-{index}"
            for index, row in enumerate(projection.rows, start=1)
        }
        materialized_entries = (
            build_requirement_enrichment_publication_builder_entries_v2(
                projection,
                requirement_entry_ids=requirement_entry_ids,
            )
        )
        builder_tree = _builder_like_tree(
            working_tree=working_tree,
            projection=projection,
            requirement_entry_ids=requirement_entry_ids,
        )

        published = overlay_requirement_enrichment_publication_tree_v2(
            accepted_working_tree=working_tree,
            builder_tree=builder_tree,
            projection=projection,
            materialized_entries=materialized_entries,
        )

        published_by_id = {node.id: node for node in published.nodes}
        for row in projection.rows:
            node = published_by_id[row.requirement_tree_node_id]
            with self.subTest(
                builder_role=row.builder_binding_role,
                classification=row.classification,
            ):
                self.assertEqual(
                    node.projection_role,
                    row.final_projection_role,
                )
                self.assertEqual(node.classification, row.classification)
                if row.final_projection_role == "REQUIREMENT":
                    self.assertEqual(node.kind, "REQUIREMENT")
                    self.assertIsNone(node.table_record_id)
                elif row.final_projection_role == "DIRECT_CONTENT":
                    self.assertEqual(node.kind, "DIRECT_CONTENT")
                    self.assertEqual(node.parent_id, node.structure_node_id)
                    self.assertEqual(node.child_ids, ())

    def test_classified_structure_proxy_becomes_analyzable_leaf_without_loss(
        self,
    ) -> None:
        for effective_type in (
            EntryClassificationType.TECHNICAL,
            EntryClassificationType.NON_TECHNICAL,
        ):
            with self.subTest(classification=effective_type.value):
                working_tree = _accepted_working_tree()
                classification = _accepted_classification(
                    working_tree,
                    structure_proxy_type=effective_type,
                )
                projection = build_requirement_enrichment_publication_projection_v2(
                    accepted_working_tree=working_tree,
                    accepted_entry_classification=classification,
                    atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
                )
                requirement_entry_ids = {
                    row.requirement_tree_node_id: f"proxy-database-{index}"
                    for index, row in enumerate(projection.rows, start=1)
                }
                entries = build_requirement_enrichment_publication_builder_entries_v2(
                    projection,
                    requirement_entry_ids=requirement_entry_ids,
                )
                builder_tree = _builder_like_tree(
                    working_tree=working_tree,
                    projection=projection,
                    requirement_entry_ids=requirement_entry_ids,
                )

                published = overlay_requirement_enrichment_publication_tree_v2(
                    accepted_working_tree=working_tree,
                    builder_tree=builder_tree,
                    projection=projection,
                    materialized_entries=entries,
                )

                node_by_id = {node.id: node for node in published.nodes}
                source_by_id = {node.id: node for node in working_tree.nodes}
                proxy = node_by_id[_TITLE_NODE_ID]
                self.assertEqual(proxy.child_ids, ())
                self.assertEqual(
                    node_by_id[_STRUCTURE_NODE_ID].child_ids,
                    (_TITLE_NODE_ID, *source_by_id[_TITLE_NODE_ID].child_ids),
                )
                self.assertTrue(
                    all(
                        node_by_id[child_id].parent_id == _STRUCTURE_NODE_ID
                        for child_id in source_by_id[_TITLE_NODE_ID].child_ids
                    )
                )
                self.assertTrue(
                    _is_analysis_candidate(
                        tree_node=proxy,
                        requirement={
                            "id": requirement_entry_ids[_TITLE_NODE_ID],
                            "entry_kind": "REQUIREMENT",
                            "decision_reference": {"decision_mode": "DIRECT"},
                        },
                    )
                )

    def test_rejects_working_or_stale_classification_and_not_run_sidecars(self) -> None:
        working_tree = _accepted_working_tree()
        accepted = _accepted_classification(working_tree)
        working = build_initial_entry_classification_snapshot(working_tree)

        with self.assertRaisesRegex(ValueError, "ACCEPTED"):
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=working,
                atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
            )
        stale = accepted.model_copy(update={"working_tree_sha256": "f" * 64})
        with self.assertRaisesRegex(ValueError, "working tree|classification"):
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=stale,
                atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
            )
        with self.assertRaisesRegex(ValueError, "NOT_RUN"):
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=accepted,
                atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
                atomization_run=_multi_atom_run(),
            )

    def test_not_run_preserves_every_business_row_one_to_one(self) -> None:
        working_tree = _accepted_working_tree()
        classification = _accepted_classification(working_tree)

        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
        )

        expected_ids = (
            _TITLE_NODE_ID,
            _TECHNICAL_NODE_ID,
            _NON_TECHNICAL_NODE_ID,
            _INFORMATION_NODE_ID,
            _TABLE_RECORD_NODE_ID,
        )
        self.assertEqual(projection.source_business_tree_node_ids, expected_ids)
        self.assertEqual(
            tuple(row.requirement_tree_node_id for row in projection.rows),
            expected_ids,
        )
        self.assertTrue(all(row.atomization_node_id is None for row in projection.rows))
        self.assertEqual(projection.atomization_run_id, "")
        self.assertEqual(projection.applied_review_node_ids, ())

    def test_applied_multi_atom_keeps_parent_and_uses_atom_ids_for_children(
        self,
    ) -> None:
        working_tree = _accepted_working_tree()
        classification = _accepted_classification(working_tree)
        receipt, run, preview = _applied_publication_inputs(
            working_tree,
            classification,
        )

        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=preview,
        )

        family = tuple(
            row
            for row in projection.rows
            if row.source_working_tree_node_id == _TECHNICAL_NODE_ID
        )
        self.assertEqual(len(family), 4)
        parent, *children = family
        self.assertEqual(parent.requirement_tree_node_id, _TECHNICAL_NODE_ID)
        self.assertEqual(
            parent.entry_kind,
            RequirementEnrichmentPublicationEntryKindV2.HEADING,
        )
        self.assertEqual(
            parent.builder_kind,
            RequirementEnrichmentPublicationBuilderKindV2.REQUIREMENT_CONTAINER,
        )
        self.assertEqual(parent.decision_mode, RequirementDecisionMode.ROLLUP)
        self.assertEqual(
            tuple(child.requirement_tree_node_id for child in children),
            tuple(node.node_id for node in run.atomic_nodes),
        )
        self.assertTrue(
            all(
                child.parent_requirement_tree_node_id == _TECHNICAL_NODE_ID
                and child.entry_kind
                is RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
                and child.builder_kind
                is RequirementEnrichmentPublicationBuilderKindV2.REQUIREMENT
                and child.decision_mode is RequirementDecisionMode.DIRECT
                for child in children
            )
        )
        self.assertEqual(len({child.requirement_tree_node_id for child in children}), 3)

    def test_builder_entries_require_exact_database_row_cover(self) -> None:
        working_tree = _accepted_working_tree()
        classification = _accepted_classification(working_tree)
        receipt, run, preview = _applied_publication_inputs(
            working_tree,
            classification,
        )
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=preview,
        )
        row_ids = tuple(row.requirement_tree_node_id for row in projection.rows)
        requirement_entry_ids = {
            row_id: f"database-row-{index}"
            for index, row_id in enumerate(row_ids, start=1)
        }

        entries = build_requirement_enrichment_publication_builder_entries_v2(
            projection,
            requirement_entry_ids=requirement_entry_ids,
        )

        self.assertEqual(
            {entry["requirement_entry_id"] for entry in entries},
            set(requirement_entry_ids.values()),
        )
        parent_entry = next(
            entry
            for entry in entries
            if entry["final_requirement_tree_node_id"] == _TECHNICAL_NODE_ID
        )
        self.assertEqual(parent_entry["kind"], "REQUIREMENT_CONTAINER")
        self.assertEqual(parent_entry["entry_kind"], "HEADING")
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            build_requirement_enrichment_publication_builder_entries_v2(
                projection,
                requirement_entry_ids={
                    key: value
                    for key, value in requirement_entry_ids.items()
                    if key != row_ids[-1]
                },
            )

    def test_applied_requires_exact_receipt_run_preview_and_refined_statement(
        self,
    ) -> None:
        working_tree = _accepted_working_tree()
        classification = _accepted_classification(working_tree)
        receipt, run, preview = _applied_publication_inputs(
            working_tree,
            classification,
            refined_first_atom=True,
        )

        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=preview,
        )

        first_atom = run.atomic_nodes[0]
        projected = next(
            row
            for row in projection.rows
            if row.atomization_node_id == first_atom.node_id
        )
        self.assertEqual(projected.published_statement, "Refined atomic obligation 1")
        self.assertEqual(projection.atomization_scope_receipt_id, receipt.receipt_id)
        self.assertEqual(projection.publication_preview_id, preview.preview_id)

        stale_receipt = receipt.model_copy(
            update={"entry_classification_sha256": "9" * 64}
        )
        with self.assertRaisesRegex(ValueError, "receipt|classification"):
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=classification,
                atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
                atomization_scope_receipt=stale_receipt,
                atomization_run=run,
                publication_preview=preview,
            )
        crossed_classification = _receipt_with_updates(
            receipt,
            entry_classification_content_sha256="9" * 64,
        )
        with self.assertRaisesRegex(ValueError, "classification|content"):
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=classification,
                atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
                atomization_scope_receipt=crossed_classification,
                atomization_run=run,
                publication_preview=preview,
            )
        crossed_entries = list(preview.entries)
        crossed_entries[1] = crossed_entries[1].model_copy(
            update={"review_node_id": "review-non-technical-one"}
        )
        crossed_preview = preview.model_copy(update={"entries": tuple(crossed_entries)})
        with self.assertRaisesRegex(ValueError, "preview|family|exact"):
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=classification,
                atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
                atomization_scope_receipt=receipt,
                atomization_run=run,
                publication_preview=crossed_preview,
            )
        inactive_receipt = _receipt_with_updates(receipt, active_revision=0)
        with self.assertRaisesRegex(ValueError, "active|NOT_RUN"):
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=classification,
                atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
                atomization_scope_receipt=inactive_receipt,
                atomization_run=run,
                publication_preview=preview,
            )

    def test_applied_rejects_missing_and_extra_run_families(self) -> None:
        working_tree = _accepted_working_tree_with_second_technical()
        classification = _accepted_classification(working_tree)
        receipt, run, _ = _applied_publication_inputs(
            working_tree,
            classification,
            include_shared_one_to_one=True,
        )

        missing_nodes = tuple(
            node
            for node in run.nodes
            if node.review_node_id != _SECOND_TECHNICAL_REVIEW_ID
        )
        missing_run = _run_with_nodes(run, missing_nodes)
        missing_receipt = _receipt_with_updates(
            receipt,
            atomization_run_id=missing_run.atomization_run_id,
        )
        missing_preview = _publication_preview(
            missing_run,
            refined_first_atom=False,
        )
        with self.assertRaisesRegex(ValueError, "families|exact"):
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=classification,
                atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
                atomization_scope_receipt=missing_receipt,
                atomization_run=missing_run,
                publication_preview=missing_preview,
            )

        extra_receipt = _receipt_with_updates(
            receipt,
            cumulative_review_node_ids=(_TECHNICAL_REVIEW_ID,),
        )
        extra_preview = _publication_preview(
            run,
            refined_first_atom=False,
        )
        with self.assertRaisesRegex(ValueError, "families|exact"):
            build_requirement_enrichment_publication_projection_v2(
                accepted_working_tree=working_tree,
                accepted_entry_classification=classification,
                atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
                atomization_scope_receipt=extra_receipt,
                atomization_run=run,
                publication_preview=extra_preview,
            )

    def test_applied_allows_content_equivalent_working_receipt_binding(self) -> None:
        working_tree = _accepted_working_tree()
        accepted = _accepted_classification(working_tree)
        equivalent_working = accepted.model_copy(
            update={
                "snapshot_id": "classification-equivalent-working",
                "payload_sha256": "0" * 64,
                "revision": accepted.revision - 1,
                "status": EntryClassificationStatus.WORKING,
                "supersedes_id": "",
                "accepted_at": "",
                "accepted_by": "",
            }
        )
        equivalent_working = equivalent_working.model_copy(
            update={
                "payload_sha256": canonical_entry_classification_payload_sha256(
                    equivalent_working
                )
            }
        )
        equivalent_working = EntryClassificationSnapshotV1.model_validate(
            equivalent_working.model_dump(mode="json")
        )
        receipt, run, preview = _applied_publication_inputs(
            working_tree,
            accepted,
            receipt_classification=equivalent_working,
        )

        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=accepted,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=preview,
        )

        self.assertEqual(
            projection.entry_classification_snapshot_id,
            accepted.snapshot_id,
        )
        self.assertEqual(
            projection.atomization_classification_content_sha256,
            stable_effective_entry_classification_sha256(accepted),
        )

    def test_one_atom_stays_one_to_one_and_shared_spans_do_not_collide(self) -> None:
        working_tree = _accepted_working_tree_with_second_technical()
        classification = _accepted_classification(working_tree)
        receipt, run, preview = _applied_publication_inputs(
            working_tree,
            classification,
            include_shared_one_to_one=True,
        )

        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=preview,
        )

        direct = next(
            row
            for row in projection.rows
            if row.source_working_tree_node_id == _SECOND_TECHNICAL_NODE_ID
        )
        self.assertEqual(direct.requirement_tree_node_id, _SECOND_TECHNICAL_NODE_ID)
        self.assertIsNotNone(direct.atomization_node_id)
        first_family_child = next(
            row
            for row in projection.rows
            if row.parent_requirement_tree_node_id == _TECHNICAL_NODE_ID
        )
        self.assertEqual(direct.source_spans, first_family_child.source_spans)
        self.assertNotEqual(
            direct.atomization_node_id,
            first_family_child.atomization_node_id,
        )
        self.assertNotEqual(
            direct.requirement_tree_node_id,
            first_family_child.requirement_tree_node_id,
        )

    def test_pass_through_run_family_is_proven_but_never_fans_out(self) -> None:
        working_tree = _accepted_working_tree_with_second_technical()
        classification = _accepted_classification(working_tree)
        receipt, run, preview = _applied_publication_inputs(
            working_tree,
            classification,
            include_shared_one_to_one=True,
        )
        receipt = _receipt_with_updates(
            receipt,
            cumulative_review_node_ids=(_TECHNICAL_REVIEW_ID,),
            pass_through_review_node_ids=(_SECOND_TECHNICAL_REVIEW_ID,),
        )

        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=preview,
        )

        pass_through_rows = tuple(
            row
            for row in projection.rows
            if row.workflow_node_id == _SECOND_TECHNICAL_REVIEW_ID
        )
        self.assertEqual(len(pass_through_rows), 1)
        self.assertEqual(
            pass_through_rows[0].requirement_tree_node_id,
            _SECOND_TECHNICAL_NODE_ID,
        )
        source = next(
            node for node in working_tree.nodes if node.id == _SECOND_TECHNICAL_NODE_ID
        )
        self.assertIsNone(pass_through_rows[0].atomization_node_id)
        self.assertIsNone(pass_through_rows[0].atomic_requirement_id)
        self.assertEqual(pass_through_rows[0].source_spans, ())
        self.assertEqual(
            pass_through_rows[0].published_statement,
            source.summary,
        )
        self.assertEqual(
            projection.applied_review_node_ids,
            (_TECHNICAL_REVIEW_ID,),
        )

    def test_real_protected_builder_consumes_every_w3_anchor_role(self) -> None:
        workbench, payload, working_tree = _real_builder_working_tree()
        classification = _accepted_classification(working_tree)
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
        )
        requirement_entry_ids = {
            row.requirement_tree_node_id: f"real-database-row-{index}"
            for index, row in enumerate(projection.rows, start=1)
        }
        entries = build_requirement_enrichment_publication_builder_entries_v2(
            projection,
            requirement_entry_ids=requirement_entry_ids,
        )

        builder_tree = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
            mode=RequirementTreeMode.REQUIREMENT_SET,
            requirement_set_id="real-builder-publication-v2",
            requirement_entries=entries,
            forced_state=RequirementTreeState.READY,
        )

        builder_roles = {
            node.projection_role
            for node in builder_tree.nodes
            if node.requirement_entry_id is not None
        }
        self.assertTrue(
            {
                "STRUCTURE_PROXY",
                "DIRECT_CONTENT",
                "WHOLE_TABLE_REVIEW",
                "TABLE_RECORD",
                "REQUIREMENT",
            }.issubset(builder_roles)
        )
        published = overlay_requirement_enrichment_publication_tree_v2(
            accepted_working_tree=working_tree,
            builder_tree=builder_tree,
            projection=projection,
            materialized_entries=entries,
        )
        published_by_entry_id = {
            node.requirement_entry_id: node
            for node in published.nodes
            if node.requirement_entry_id is not None
        }
        self.assertEqual(
            set(published_by_entry_id),
            set(requirement_entry_ids.values()),
        )
        technical_direct = next(
            row
            for row in projection.rows
            if row.builder_binding_role == "DIRECT_CONTENT"
            and row.classification == EntryClassificationType.TECHNICAL.value
        )
        final_direct = published_by_entry_id[
            requirement_entry_ids[technical_direct.requirement_tree_node_id]
        ]
        self.assertEqual(final_direct.kind, "REQUIREMENT")
        self.assertEqual(final_direct.projection_role, "REQUIREMENT")
        self.assertEqual(final_direct.editable_structure_node_ids, ())
        technical_table = next(
            row for row in projection.rows if row.builder_binding_role == "TABLE_RECORD"
        )
        table_record = published_by_entry_id[
            requirement_entry_ids[technical_table.requirement_tree_node_id]
        ]
        self.assertEqual(table_record.kind, "REQUIREMENT")
        self.assertEqual(table_record.projection_role, "REQUIREMENT")
        self.assertIsNone(table_record.table_record_id)
        self.assertIsNotNone(table_record.table_evidence_binding_id)

    def test_overlay_reparents_atoms_and_recomputes_all_aggregate_counts(self) -> None:
        working_tree = _accepted_working_tree(reviewed=True)
        classification = _accepted_classification(working_tree)
        receipt, run, preview = _applied_publication_inputs(
            working_tree,
            classification,
        )
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=preview,
        )
        requirement_entry_ids = {
            row.requirement_tree_node_id: f"database-row-{index}"
            for index, row in enumerate(projection.rows, start=1)
        }
        builder_tree = _builder_like_tree(
            working_tree=working_tree,
            projection=projection,
            requirement_entry_ids=requirement_entry_ids,
        )
        materialized_entries = (
            build_requirement_enrichment_publication_builder_entries_v2(
                projection,
                requirement_entry_ids=requirement_entry_ids,
            )
        )

        published = overlay_requirement_enrichment_publication_tree_v2(
            accepted_working_tree=working_tree,
            builder_tree=builder_tree,
            projection=projection,
            materialized_entries=materialized_entries,
        )

        node_by_id = {node.id: node for node in published.nodes}
        parent = node_by_id[_TECHNICAL_NODE_ID]
        atom_ids = tuple(node.node_id for node in run.atomic_nodes)
        self.assertEqual(parent.kind, "REQUIREMENT_CONTAINER")
        self.assertEqual(parent.child_ids, atom_ids)
        self.assertEqual(
            (
                parent.requirement_count,
                parent.attention_count,
                parent.reviewed_count,
                parent.review_item_count,
            ),
            (4, 0, 4, 4),
        )
        for atom_id in atom_ids:
            child = node_by_id[atom_id]
            self.assertEqual(child.parent_id, _TECHNICAL_NODE_ID)
            self.assertEqual(child.depth, parent.depth + 1)
            self.assertEqual(
                (
                    child.requirement_count,
                    child.attention_count,
                    child.reviewed_count,
                    child.review_item_count,
                ),
                (1, 0, 1, 1),
            )
        title = node_by_id[_TITLE_NODE_ID]
        root = node_by_id[_STRUCTURE_NODE_ID]
        self.assertEqual(
            (title.requirement_count, title.reviewed_count, title.review_item_count),
            (8, 8, 8),
        )
        self.assertEqual(
            (root.requirement_count, root.reviewed_count, root.review_item_count),
            (8, 8, 8),
        )
        expected_database_ids = set(requirement_entry_ids.values())
        self.assertEqual(
            {
                node.requirement_entry_id
                for node in published.nodes
                if node.requirement_entry_id is not None
            },
            expected_database_ids,
        )
        assert published.requirement_evidence_bundle is not None
        self.assertEqual(
            {
                binding.requirement_entry_id
                for binding in published.requirement_evidence_bundle.requirement_bindings
            },
            expected_database_ids,
        )

    def test_overlay_fails_closed_for_extra_rows_and_tampered_parent_cycles(
        self,
    ) -> None:
        working_tree = _accepted_working_tree()
        classification = _accepted_classification(working_tree)
        receipt, run, preview = _applied_publication_inputs(
            working_tree,
            classification,
        )
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=preview,
        )
        requirement_entry_ids = {
            row.requirement_tree_node_id: f"database-row-{index}"
            for index, row in enumerate(projection.rows, start=1)
        }
        builder_tree = _builder_like_tree(
            working_tree=working_tree,
            projection=projection,
            requirement_entry_ids=requirement_entry_ids,
        )
        materialized_entries = (
            build_requirement_enrichment_publication_builder_entries_v2(
                projection,
                requirement_entry_ids=requirement_entry_ids,
            )
        )
        first_child = next(
            row for row in projection.rows if row.atomization_node_id is not None
        )
        tampered_child = first_child.model_copy(
            update={
                "parent_requirement_tree_node_id": first_child.requirement_tree_node_id
            }
        )
        tampered = projection.model_copy(
            update={
                "rows": tuple(
                    tampered_child if row is first_child else row
                    for row in projection.rows
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "cycle|parent"):
            overlay_requirement_enrichment_publication_tree_v2(
                accepted_working_tree=working_tree,
                builder_tree=builder_tree,
                projection=tampered,
                materialized_entries=materialized_entries,
            )

        extra_id = "database-row-extra"
        extra_node = RequirementTreeNodeV1(
            id="requirement:unexpected-extra",
            parent_id=_TITLE_NODE_ID,
            kind="REQUIREMENT",
            depth=2,
            source_order=99,
            label="unexpected",
            summary="unexpected",
            structure_node_id=_TITLE_NODE_ID,
            requirement_entry_id=extra_id,
            workflow_node_id="unexpected-extra",
            projection_role="REQUIREMENT",
            source_refs=({"kind": "TEXT_RANGE", "source_id": "extra"},),
            requirement_count=1,
            review_item_count=1,
        )
        tree_by_id = {node.id: node for node in builder_tree.nodes}
        title = tree_by_id[_TITLE_NODE_ID]
        extra_tree = builder_tree.model_copy(
            update={
                "nodes": (
                    *(
                        node.model_copy(
                            update={"child_ids": (*node.child_ids, extra_node.id)}
                        )
                        if node.id == title.id
                        else node
                        for node in builder_tree.nodes
                    ),
                    extra_node,
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "extra|exact"):
            overlay_requirement_enrichment_publication_tree_v2(
                accepted_working_tree=working_tree,
                builder_tree=extra_tree,
                projection=projection,
                materialized_entries=materialized_entries,
            )

    def test_overlay_rejects_cross_family_db_and_evidence_metadata_swaps(self) -> None:
        working_tree = _accepted_working_tree()
        classification = _accepted_classification(working_tree)
        receipt, run, preview = _applied_publication_inputs(
            working_tree,
            classification,
        )
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=preview,
        )
        requirement_entry_ids = {
            row.requirement_tree_node_id: f"database-row-{index}"
            for index, row in enumerate(projection.rows, start=1)
        }
        materialized = list(
            build_requirement_enrichment_publication_builder_entries_v2(
                projection,
                requirement_entry_ids=requirement_entry_ids,
            )
        )
        builder_tree = _builder_like_tree(
            working_tree=working_tree,
            projection=projection,
            requirement_entry_ids=requirement_entry_ids,
        )
        atom_indexes = [
            index
            for index, entry in enumerate(materialized)
            if entry["atomic_requirement_id"] is not None
        ]
        first_index, second_index = atom_indexes[:2]
        first = materialized[first_index]
        second = materialized[second_index]
        mutations = (
            {"atomic_requirement_id": second["atomic_requirement_id"]},
            {"source_spans": second["source_spans"]},
            {"classification": EntryClassificationType.INFORMATION.value},
            {"entry_kind": "REFERENCE"},
            {"decision_mode": "ROLLUP"},
            {
                "parent_requirement_tree_node_id": second[
                    "final_requirement_tree_node_id"
                ]
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tampered = list(materialized)
                tampered[first_index] = {**first, **mutation}
                with self.assertRaisesRegex(ValueError, "row|exact|evidence"):
                    overlay_requirement_enrichment_publication_tree_v2(
                        accepted_working_tree=working_tree,
                        builder_tree=builder_tree,
                        projection=projection,
                        materialized_entries=tuple(tampered),
                    )

        first_row, second_row = tuple(
            row
            for row in projection.rows
            if row.parent_requirement_tree_node_id == _TECHNICAL_NODE_ID
        )[:2]
        first_database_id = requirement_entry_ids[first_row.requirement_tree_node_id]
        second_database_id = requirement_entry_ids[second_row.requirement_tree_node_id]
        assert builder_tree.requirement_evidence_bundle is not None
        bundle = builder_tree.requirement_evidence_bundle
        binding_by_entry_id = {
            binding.requirement_entry_id: binding
            for binding in bundle.requirement_bindings
        }
        second_binding = binding_by_entry_id[second_database_id]
        crossed_binding = RequirementEvidenceBindingV1.create(
            requirement_entry_id=first_database_id,
            atomic_requirement_id=second_binding.atomic_requirement_id,
            source_spans=second_binding.source_spans,
            source_refs=second_binding.source_refs,
            table_evidence_binding_id=second_binding.table_evidence_binding_id,
        )
        crossed_bundle = RequirementEvidenceBundleV1.create(
            source_ir_id=bundle.source_ir_id,
            source_sha256=bundle.source_sha256,
            structure_snapshot_id=bundle.structure_snapshot_id,
            table_bindings=bundle.table_bindings,
            requirement_bindings=tuple(
                crossed_binding
                if binding.requirement_entry_id == first_database_id
                else binding
                for binding in bundle.requirement_bindings
            ),
        )
        crossed_tree = builder_tree.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(
                        update={
                            "requirement_evidence_binding_id": crossed_binding.binding_id
                        }
                    )
                    if node.requirement_entry_id == first_database_id
                    else node
                    for node in builder_tree.nodes
                ),
                "requirement_evidence_bundle": crossed_bundle,
            }
        )
        with self.assertRaisesRegex(ValueError, "evidence|exact"):
            overlay_requirement_enrichment_publication_tree_v2(
                accepted_working_tree=working_tree,
                builder_tree=crossed_tree,
                projection=projection,
                materialized_entries=tuple(materialized),
            )


def _role_classification_working_tree() -> RequirementTreeSnapshotV1:
    root_id = "role-classification-root"
    leaves: list[RequirementTreeNodeV1] = []
    source_order = 1
    for role in _PUBLICATION_BUILDER_ROLES:
        for effective_type in EntryClassificationType:
            node_id = f"role-matrix:{role.lower()}:{effective_type.value.lower()}"
            if role == "REQUIREMENT":
                node_id = f"requirement:{node_id}"
            if role == "STRUCTURE_PROXY":
                kind = "SECTION"
                structure_node_id = node_id
            elif role == "DIRECT_CONTENT":
                kind = "DIRECT_CONTENT"
                structure_node_id = root_id
            elif role == "WHOLE_TABLE_REVIEW":
                kind = "TABLE"
                structure_node_id = f"source-{node_id}"
            elif role == "TABLE_RECORD":
                kind = "TABLE_RECORD"
                structure_node_id = root_id
            else:
                kind = "REQUIREMENT"
                structure_node_id = root_id
            leaves.append(
                RequirementTreeNodeV1(
                    id=node_id,
                    parent_id=root_id,
                    kind=kind,
                    depth=1,
                    source_order=source_order,
                    label=f"{role} {effective_type.value}",
                    summary=f"{role} {effective_type.value} business row",
                    structure_node_id=structure_node_id,
                    workflow_node_id=f"workflow:{role}:{effective_type.value}",
                    source_refs=(
                        {
                            "kind": "TEXT_RANGE",
                            "source_id": f"source-ref-{source_order}",
                        },
                    ),
                    requirement_count=1,
                    classification="STALE_TREE_VALUE",
                    projection_role=role,
                    table_record_id=(
                        f"role-matrix-record-{source_order}"
                        if role == "TABLE_RECORD"
                        else None
                    ),
                    review_item_count=1,
                )
            )
            source_order += 1
    root = RequirementTreeNodeV1(
        id=root_id,
        kind="DOCUMENT",
        depth=0,
        source_order=0,
        label="Role classification matrix",
        child_ids=tuple(node.id for node in leaves),
        structure_node_id=root_id,
        requirement_count=len(leaves),
        projection_role="STRUCTURE",
        review_item_count=len(leaves),
    )
    return RequirementTreeSnapshotV1(
        source_identity=RequirementTreeSourceIdentity(
            project_id=_PROJECT_ID,
            document_id=_DOCUMENT_ID,
            source_ir_id=_SOURCE_IR_ID,
            source_sha256=_SOURCE_SHA256,
        ),
        structure_identity=RequirementTreeStructureIdentity(
            base_snapshot_id=_STRUCTURE_ID,
            effective_snapshot_id=_STRUCTURE_ID,
            effective_tree_sha256="c" * 64,
            revision=2,
        ),
        mode=RequirementTreeMode.WORKING,
        state=RequirementTreeState.READY,
        root_node_ids=(root_id,),
        nodes=(root, *leaves),
    )


def _accepted_role_classification(
    tree: RequirementTreeSnapshotV1,
) -> EntryClassificationSnapshotV1:
    initial = build_initial_entry_classification_snapshot(tree)
    working = revise_entry_classification_snapshot(
        initial,
        overrides={
            entry.workflow_node_id: EntryClassificationType(
                entry.workflow_node_id.rsplit(":", 1)[1]
            )
            for entry in initial.entries
        },
        snapshot_id="classification-role-matrix-working",
        created_at="2026-08-07T08:00:00+00:00",
    )
    return accept_entry_classification_snapshot(
        working,
        snapshot_id="classification-role-matrix-accepted",
        accepted_at="2026-08-07T08:01:00+00:00",
        accepted_by="publication-v2-role-matrix-test",
    )


def _accepted_working_tree(*, reviewed: bool = False) -> RequirementTreeSnapshotV1:
    status = "REVIEWED" if reviewed else "PENDING"
    reviewed_self = 1 if reviewed else 0
    leaf_specs = (
        (
            _TECHNICAL_NODE_ID,
            _TECHNICAL_REVIEW_ID,
            EntryClassificationType.TECHNICAL,
            "REQUIREMENT",
            "Technical requirement",
        ),
        (
            _NON_TECHNICAL_NODE_ID,
            "review-non-technical-one",
            EntryClassificationType.NON_TECHNICAL,
            "REQUIREMENT",
            "Non-technical requirement",
        ),
        (
            _INFORMATION_NODE_ID,
            "review-information-one",
            EntryClassificationType.INFORMATION,
            "DIRECT_CONTENT",
            "Information and reference",
        ),
        (
            _TABLE_RECORD_NODE_ID,
            "review-table-record-one",
            EntryClassificationType.TECHNICAL,
            "TABLE_RECORD",
            "Parameter table row",
        ),
    )
    leaves = tuple(
        RequirementTreeNodeV1(
            id=node_id,
            parent_id=_TITLE_NODE_ID,
            kind="TABLE_RECORD" if role == "TABLE_RECORD" else "REQUIREMENT",
            depth=2,
            source_order=index + 2,
            label=label,
            summary=label,
            structure_node_id=_TITLE_NODE_ID,
            workflow_node_id=workflow_id,
            page_start=1,
            page_end=1,
            source_refs=(
                {
                    "kind": "TEXT_RANGE",
                    "source_id": f"source-{index}",
                    "owner_structure_node_id": _TITLE_NODE_ID,
                    "start_offset": 0,
                    "end_offset": len(label),
                },
            ),
            requirement_count=1,
            reviewed_count=reviewed_self,
            status=status,
            classification=classification.value,
            projection_role=role,
            semantic_state="CONFIRMED" if reviewed else "PROPOSED",
            review_item_count=1,
            table_record_id=("record-one" if role == "TABLE_RECORD" else None),
        )
        for index, (node_id, workflow_id, classification, role, label) in enumerate(
            leaf_specs
        )
    )
    title = RequirementTreeNodeV1(
        id=_TITLE_NODE_ID,
        parent_id=_STRUCTURE_NODE_ID,
        kind="SECTION",
        depth=1,
        source_order=1,
        label="System requirements",
        summary="System requirements",
        child_ids=tuple(node.id for node in leaves),
        structure_node_id=_TITLE_NODE_ID,
        workflow_node_id="workflow-title-one",
        source_refs=({"kind": "TEXT_RANGE", "source_id": "source-title"},),
        requirement_count=5,
        reviewed_count=5 if reviewed else 0,
        status=status,
        classification=EntryClassificationType.INFORMATION.value,
        projection_role="STRUCTURE_PROXY",
        semantic_state="CONFIRMED" if reviewed else "PROPOSED",
        review_item_count=5,
    )
    root = RequirementTreeNodeV1(
        id=_STRUCTURE_NODE_ID,
        kind="DOCUMENT",
        depth=0,
        source_order=0,
        label="Document",
        child_ids=(_TITLE_NODE_ID,),
        structure_node_id=_STRUCTURE_NODE_ID,
        requirement_count=5,
        reviewed_count=5 if reviewed else 0,
        projection_role="STRUCTURE",
        review_item_count=5,
    )
    return RequirementTreeSnapshotV1(
        source_identity=RequirementTreeSourceIdentity(
            project_id=_PROJECT_ID,
            document_id=_DOCUMENT_ID,
            source_ir_id=_SOURCE_IR_ID,
            source_sha256=_SOURCE_SHA256,
        ),
        structure_identity=RequirementTreeStructureIdentity(
            base_snapshot_id=_STRUCTURE_ID,
            effective_snapshot_id=_STRUCTURE_ID,
            effective_tree_sha256="c" * 64,
            revision=2,
        ),
        mode=RequirementTreeMode.WORKING,
        state=RequirementTreeState.READY,
        root_node_ids=(_STRUCTURE_NODE_ID,),
        nodes=(root, title, *leaves),
    )


def _real_builder_working_tree() -> tuple[dict, dict, RequirementTreeSnapshotV1]:
    from tests.test_requirement_tree import (
        CLAUSE_ID,
        ROOT_ID,
        SECTION_ID,
        _records_projection,
        _whole_table_projection_with_evidence,
        _workbench,
    )

    whole_table_node_id = "real-whole-table-node"
    record_table_node_id = "real-record-table-node"
    whole_table_id = "real-whole-table"
    record_table_id = "real-record-table"
    whole_source_id = f"node-{'a' * 32}"
    record_source_id = f"node-{'b' * 32}"
    whole_projection, whole_evidence = _whole_table_projection_with_evidence(
        owner_id=whole_table_node_id,
        table_id=whole_table_id,
        source_id=whole_source_id,
        logical_table_id="real-whole-logical-table",
    )
    record_projection = _records_projection(
        owner_id=record_table_node_id,
        table_id=record_table_id,
        source_id=record_source_id,
        table_source_order=40,
        record_source_order=0,
    )
    record = record_projection.records[0]
    record_evidence_ref = EvidenceReferenceV1(
        kind=EvidenceReferenceKind.TABLE,
        reference_id=record_table_id,
        resolver_key=f"source-ir-1/{record_table_id}",
        content_sha256="9" * 64,
        page_number=2,
    )
    record_evidence = TableEvidenceBindingV1.create(
        logical_table_id=record_projection.table_fact.logical_table_id,
        source_table_ids=(record_table_id,),
        projection_id=record_projection.projection_id,
        projection_revision=record_projection.projection_revision,
        projection_sha256=record_projection.projection_sha256,
        record_id=record.record_id,
        table_refs=(record_evidence_ref,),
    )
    workbench = _workbench()
    workbench["structure"]["nodes"] = [
        workbench["structure"]["nodes"][0],
        {
            "id": SECTION_ID,
            "parent_id": ROOT_ID,
            "kind": "SECTION",
            "order": 10,
            "label": "1 Complete business skeleton",
            "source_refs": [{"source_id": "real-section-source"}],
        },
        {
            "id": "real-direct-content",
            "parent_id": SECTION_ID,
            "kind": "CONTENT",
            "order": 11,
            "label": "The supplier shall preserve this direct content.",
            "source_refs": [{"source_id": "real-direct-source"}],
        },
        {
            "id": "real-structure-proxy-clause",
            "parent_id": SECTION_ID,
            "kind": "CLAUSE",
            "order": 12,
            "label": "1.0 Source-bound proxy clause",
            "boundary_basis": "EXPLICIT_NUMBER",
            "source_refs": [{"source_id": "real-proxy-heading-source"}],
        },
        {
            "id": "real-structure-proxy-content",
            "parent_id": "real-structure-proxy-clause",
            "kind": "CONTENT",
            "order": 13,
            "label": "This clause remains a compact Structure proxy.",
            "source_refs": [{"source_id": "real-proxy-content-source"}],
        },
        {
            "id": CLAUSE_ID,
            "parent_id": SECTION_ID,
            "kind": "CLAUSE",
            "order": 20,
            "label": "1.1 Technical clause",
            "boundary_basis": "EXPLICIT_NUMBER",
            "source_refs": [{"source_id": "real-clause-source"}],
        },
        {
            "id": whole_table_node_id,
            "parent_id": SECTION_ID,
            "kind": "TABLE",
            "order": 30,
            "label": "Whole table",
            "source_refs": [{"source_id": whole_source_id}],
        },
        {
            "id": record_table_node_id,
            "parent_id": SECTION_ID,
            "kind": "TABLE",
            "order": 40,
            "label": "Record table",
            "source_refs": [{"source_id": record_source_id}],
        },
    ]
    payload = {
        "mode": "LOCAL_STRUCTURE_PROJECTION",
        "tables": [
            {
                "id": whole_table_id,
                "source_node_id": whole_source_id,
                "cells": [
                    {"id": f"whole-cell-{index:028x}", "text": str(index)}
                    for index in range(1, 5)
                ],
            },
            {
                "id": record_table_id,
                "source_node_id": record_source_id,
                "cells": [
                    {"id": f"cell-{index:032x}", "text": str(index)}
                    for index in range(1, 5)
                ],
            },
        ],
        "table_record_projections": [
            whole_projection.model_dump(mode="json"),
            record_projection.model_dump(mode="json"),
        ],
        "table_evidence_bindings": [
            whole_evidence.model_dump(mode="json"),
            record_evidence.model_dump(mode="json"),
        ],
    }
    semantic_entry = {
        "requirement_tree_node_id": "real-semantic-requirement",
        "requirement_entry_id": "real-working-semantic-entry",
        "review_node_id": "real-semantic-review",
        "owner_structure_node_id": CLAUSE_ID,
        "source_order": 21,
        "display_text": "The product shall withstand the specified load.",
        "source_refs": [
            {
                "kind": "TEXT_RANGE",
                "source_id": "real-semantic-source",
                "owner_structure_node_id": CLAUSE_ID,
                "start_offset": 0,
                "end_offset": 48,
            }
        ],
        "projection_role": "REQUIREMENT",
    }
    local_tree = build_requirement_tree_snapshot(
        workbench=workbench,
        payload=payload,
    )
    proxy_entries = tuple(
        {
            "requirement_tree_node_id": node.id,
            "review_node_id": node.workflow_node_id or node.id,
            "owner_structure_node_id": node.structure_node_id,
            "source_order": node.source_order,
            "source_offset": node.source_offset,
            "published_statement": node.summary or node.label,
            "source_refs": list(node.source_refs),
            "page_start": node.page_start,
            "page_end": node.page_end,
            "classification": node.classification,
            "projection_role": node.projection_role,
            "table_record_id": node.table_record_id,
            "table_evidence_binding_id": node.table_evidence_binding_id,
            "deterministic_rule_evidence": list(node.deterministic_rule_evidence),
            "content_blocks": list(node.content_blocks),
            "table_refs": list(node.table_refs),
            "figure_refs": list(node.figure_refs),
        }
        for node in local_tree.nodes
        if node.projection_role
        in {
            "STRUCTURE_PROXY",
            "DIRECT_CONTENT",
            "WHOLE_TABLE_REVIEW",
            "TABLE_RECORD",
        }
    )
    working = build_requirement_tree_snapshot(
        workbench=workbench,
        payload=payload,
        requirement_entries=(*proxy_entries, semantic_entry),
    )
    return workbench, payload, working


def _accepted_working_tree_with_second_technical() -> RequirementTreeSnapshotV1:
    tree = _accepted_working_tree()
    node_by_id = {node.id: node for node in tree.nodes}
    second = RequirementTreeNodeV1(
        id=_SECOND_TECHNICAL_NODE_ID,
        parent_id=_TITLE_NODE_ID,
        kind="REQUIREMENT",
        depth=2,
        source_order=20,
        label="Second technical requirement",
        summary="Second technical requirement",
        structure_node_id=_TITLE_NODE_ID,
        workflow_node_id=_SECOND_TECHNICAL_REVIEW_ID,
        source_refs=(
            {
                "kind": "TEXT_RANGE",
                "source_id": "source-second-technical",
                "owner_structure_node_id": _TITLE_NODE_ID,
                "start_offset": 0,
                "end_offset": 10,
            },
        ),
        requirement_count=1,
        classification="STALE_TREE_VALUE",
        projection_role="REQUIREMENT",
        review_item_count=1,
    )
    title = node_by_id[_TITLE_NODE_ID].model_copy(
        update={
            "child_ids": (*node_by_id[_TITLE_NODE_ID].child_ids, second.id),
            "requirement_count": node_by_id[_TITLE_NODE_ID].requirement_count + 1,
            "review_item_count": node_by_id[_TITLE_NODE_ID].review_item_count + 1,
        }
    )
    root = node_by_id[_STRUCTURE_NODE_ID].model_copy(
        update={
            "requirement_count": node_by_id[_STRUCTURE_NODE_ID].requirement_count + 1,
            "review_item_count": node_by_id[_STRUCTURE_NODE_ID].review_item_count + 1,
        }
    )
    return RequirementTreeSnapshotV1.model_validate(
        tree.model_copy(
            update={
                "nodes": tuple(
                    root
                    if node.id == root.id
                    else title
                    if node.id == title.id
                    else node
                    for node in tree.nodes
                )
                + (second,)
            }
        ).model_dump(mode="json")
    )


def _accepted_classification(
    tree: RequirementTreeSnapshotV1,
    *,
    structure_proxy_type: EntryClassificationType = EntryClassificationType.INFORMATION,
) -> EntryClassificationSnapshotV1:
    initial = build_initial_entry_classification_snapshot(tree)
    node_by_id = {node.id: node for node in tree.nodes}
    overrides: dict[str, EntryClassificationType] = {}
    for entry in initial.entries:
        if entry.tree_node_id == _TITLE_NODE_ID:
            entry_type = structure_proxy_type
        elif entry.workflow_node_id in {
            _TECHNICAL_REVIEW_ID,
            _SECOND_TECHNICAL_REVIEW_ID,
            "review-table-record-one",
        }:
            entry_type = EntryClassificationType.TECHNICAL
        elif entry.workflow_node_id == "review-non-technical-one":
            entry_type = EntryClassificationType.NON_TECHNICAL
        elif entry.workflow_node_id == "review-information-one":
            entry_type = EntryClassificationType.INFORMATION
        elif node_by_id[entry.tree_node_id].projection_role in {
            "DIRECT_CONTENT",
            "TABLE_RECORD",
            "REQUIREMENT",
        }:
            entry_type = EntryClassificationType.TECHNICAL
        else:
            entry_type = EntryClassificationType.INFORMATION
        overrides[entry.workflow_node_id] = entry_type
    working = revise_entry_classification_snapshot(
        initial,
        overrides=overrides,
        snapshot_id="classification-working-publication-v2",
        created_at="2026-08-07T08:00:00+00:00",
    )
    return accept_entry_classification_snapshot(
        working,
        snapshot_id="classification-accepted-publication-v2",
        accepted_at="2026-08-07T08:01:00+00:00",
        accepted_by="publication-v2-test",
    )


def _multi_atom_run(
    *,
    include_shared_one_to_one: bool = False,
    project_id: str = _PROJECT_ID,
    document_id: str = _DOCUMENT_ID,
    source_sha256: str = _SOURCE_SHA256,
    structure_snapshot_id: str = _STRUCTURE_ID,
) -> RequirementAtomizationRun:
    lineage_fields = {
        "project_id": project_id,
        "document_id": document_id,
        "session_id": f"llm-review-{'3' * 32}",
        "prediction_id": f"llm-prediction-{'4' * 32}",
        "review_revision": 3,
        "source_sha256": source_sha256,
        "structure_snapshot_id": structure_snapshot_id,
        "reviewed_result_sha256": "d" * 64,
        "coverage_sha256": "e" * 64,
        "coverage_total_units": 1,
        "semantic_planning_runtime_version": "planning-v1",
        "semantic_classifier_runtime_version": "classifier-v1",
        "semantic_assembler_runtime_version": "assembler-v1",
        "semantic_document_runtime_version": "document-v1",
    }
    lineage = RequirementEnrichmentLineage(
        **lineage_fields,
        lineage_sha256=stable_requirement_enrichment_lineage_sha256(**lineage_fields),
    )
    atom_spans = tuple(
        (
            LlmReviewSourceSpan(
                semantic_unit_id=f"semantic-unit-{index:032x}",
                owner_structure_node_id=_TITLE_NODE_ID,
                start_offset=(index - 1) * 10,
                end_offset=index * 10,
            ),
        )
        for index in range(1, 4)
    )
    all_spans = tuple(span for group in atom_spans for span in group)
    parent_node_id = stable_atomization_node_id(
        lineage_sha256=lineage.lineage_sha256,
        review_node_id=_TECHNICAL_REVIEW_ID,
        source_spans=all_spans,
        decision_mode=RequirementDecisionMode.ROLLUP,
    )
    parent = RequirementAtomizationNode(
        node_id=parent_node_id,
        kind=RequirementAtomizationNodeKind.ROLLUP,
        review_node_id=_TECHNICAL_REVIEW_ID,
        source_semantic_unit_ids=tuple(span.semantic_unit_id for span in all_spans),
        source_owner_structure_node_ids=tuple(
            span.owner_structure_node_id for span in all_spans
        ),
        source_spans=all_spans,
        source_order=10,
        role=RequirementProjectionRole.PRIMARY,
        decision_mode=RequirementDecisionMode.ROLLUP,
        requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
        source_text="The product shall satisfy A, B, and C.",
    )
    atoms: list[RequirementAtomizationNode] = []
    for index, spans in enumerate(atom_spans, start=1):
        node_id = stable_atomization_node_id(
            lineage_sha256=lineage.lineage_sha256,
            review_node_id=_TECHNICAL_REVIEW_ID,
            source_spans=spans,
            decision_mode=RequirementDecisionMode.DIRECT,
        )
        atoms.append(
            RequirementAtomizationNode(
                node_id=node_id,
                atomic_node_id=stable_atomic_node_id(atomization_node_id=node_id),
                kind=RequirementAtomizationNodeKind.ATOMIC_OBLIGATION,
                parent_node_id=parent_node_id,
                review_node_id=_TECHNICAL_REVIEW_ID,
                source_semantic_unit_ids=tuple(span.semantic_unit_id for span in spans),
                source_owner_structure_node_ids=tuple(
                    span.owner_structure_node_id for span in spans
                ),
                source_spans=spans,
                source_order=10 + index,
                role=RequirementProjectionRole.CHILD,
                decision_mode=RequirementDecisionMode.DIRECT,
                requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                source_text=f"Source segment {index}",
                atomic_statement=f"Atomic obligation {index}",
            )
        )
    shared_direct: RequirementAtomizationNode | None = None
    if include_shared_one_to_one:
        shared_spans = atom_spans[0]
        shared_node_id = stable_atomization_node_id(
            lineage_sha256=lineage.lineage_sha256,
            review_node_id=_SECOND_TECHNICAL_REVIEW_ID,
            source_spans=shared_spans,
            decision_mode=RequirementDecisionMode.DIRECT,
        )
        shared_direct = RequirementAtomizationNode(
            node_id=shared_node_id,
            atomic_node_id=stable_atomic_node_id(atomization_node_id=shared_node_id),
            kind=RequirementAtomizationNodeKind.ATOMIC_OBLIGATION,
            review_node_id=_SECOND_TECHNICAL_REVIEW_ID,
            source_semantic_unit_ids=tuple(
                span.semantic_unit_id for span in shared_spans
            ),
            source_owner_structure_node_ids=tuple(
                span.owner_structure_node_id for span in shared_spans
            ),
            source_spans=shared_spans,
            source_order=20,
            role=RequirementProjectionRole.PRIMARY,
            decision_mode=RequirementDecisionMode.DIRECT,
            requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
            source_text="Shared source segment",
            atomic_statement="Shared one-to-one obligation",
        )
    started_at = datetime(2026, 8, 7, tzinfo=timezone.utc)
    request_sha256 = "5" * 64
    response_sha256 = "6" * 64
    provider_run = SemanticProviderRunRecord(
        provider_run_id=stable_semantic_provider_run_id(
            operation="atomize_requirement",
            provider_id="test-provider",
            model_id="test-model",
            request_sha256=request_sha256,
            response_sha256=response_sha256,
            status=SemanticProviderRunStatus.SUCCESS,
            started_at=started_at,
        ),
        operation="atomize_requirement",
        provider_id="test-provider",
        provider_version="test-provider-v1",
        model_id="test-model",
        prompt_version="test-prompt-v1",
        response_schema_version="test-response-v1",
        request_sha256=request_sha256,
        response_sha256=response_sha256,
        status=SemanticProviderRunStatus.SUCCESS,
        started_at=started_at,
        finished_at=started_at,
        latency_ms=1,
        request_count=1,
        retry_count=0,
    )
    nodes = (parent, *atoms, *((shared_direct,) if shared_direct is not None else ()))
    run_id = stable_atomization_run_id(
        lineage_sha256=lineage.lineage_sha256,
        revision=1,
        algorithm_version="explicit-scope-test-v1",
        nodes=nodes,
        provider_runs=(provider_run,),
    )
    return RequirementAtomizationRun(
        atomization_run_id=run_id,
        revision=1,
        algorithm_version="explicit-scope-test-v1",
        lineage=lineage,
        nodes=nodes,
        provider_runs=(provider_run,),
        created_at_utc=started_at,
    )


def _applied_publication_inputs(
    tree: RequirementTreeSnapshotV1,
    classification: EntryClassificationSnapshotV1,
    *,
    refined_first_atom: bool = False,
    include_shared_one_to_one: bool = False,
    receipt_classification: EntryClassificationSnapshotV1 | None = None,
    base_active_requirement_set_id: str = "",
) -> tuple[
    AtomizationScopeReceiptV1,
    RequirementAtomizationRun,
    RequirementPublicationPreview,
]:
    run = _multi_atom_run(
        include_shared_one_to_one=include_shared_one_to_one,
        project_id=tree.source_identity.project_id,
        document_id=tree.source_identity.document_id,
        source_sha256=tree.source_identity.source_sha256,
        structure_snapshot_id=tree.structure_identity.effective_snapshot_id,
    )
    receipt_classification = receipt_classification or classification
    cumulative = (_TECHNICAL_REVIEW_ID,)
    if include_shared_one_to_one:
        cumulative = (*cumulative, _SECOND_TECHNICAL_REVIEW_ID)
    command_sha256 = "7" * 64
    receipt_fields = {
        "receipt_id": "",
        "payload_sha256": "0" * 64,
        "project_id": tree.source_identity.project_id,
        "document_id": tree.source_identity.document_id,
        "session_id": run.lineage.session_id,
        "review_revision": run.lineage.review_revision,
        "lineage_sha256": run.lineage.lineage_sha256,
        "entry_classification_snapshot_id": receipt_classification.snapshot_id,
        "entry_classification_revision": receipt_classification.revision,
        "entry_classification_sha256": receipt_classification.payload_sha256,
        "entry_classification_content_sha256": (
            stable_effective_entry_classification_sha256(receipt_classification)
        ),
        "scope_revision": run.revision,
        "active_revision": run.revision,
        "previous_active_revision": 0,
        "supersedes_receipt_id": "",
        "command_id": "publication-v2-test-command",
        "command_sha256": command_sha256,
        "operation": AtomizationScopeOperation.APPLY,
        "scope": AtomizationTargetScope.SELECTED,
        "target_scope_sha256": command_sha256,
        "requested_review_node_ids": cumulative,
        "executed_review_node_ids": cumulative,
        "new_review_node_ids": cumulative,
        "reused_review_node_ids": (),
        "skipped_targets": (),
        "succeeded_review_node_ids": cumulative,
        "failed_review_node_ids": (),
        "pass_through_review_node_ids": (),
        "cumulative_review_node_ids": cumulative,
        "provider_call_count": len(cumulative),
        "checkpoint_created_count": len(cumulative),
        "checkpoint_reuse_count": 0,
        "resumable": False,
        "atomization_run_id": run.atomization_run_id,
        "atomization_run_revision": run.revision,
        "created_at_utc": run.created_at_utc,
    }
    receipt_fields["receipt_id"] = stable_atomization_scope_receipt_id(
        lineage_sha256=run.lineage.lineage_sha256,
        scope_revision=run.revision,
        command_sha256=command_sha256,
        atomization_run_id=run.atomization_run_id,
    )
    receipt_draft = AtomizationScopeReceiptV1.model_construct(**receipt_fields)
    receipt_fields["payload_sha256"] = canonical_atomization_scope_receipt_sha256(
        receipt_draft
    )
    receipt = AtomizationScopeReceiptV1.model_validate(receipt_fields)
    preview = _publication_preview(
        run,
        refined_first_atom=refined_first_atom,
        base_active_requirement_set_id=base_active_requirement_set_id,
    )
    return receipt, run, preview


def _publication_preview(
    run: RequirementAtomizationRun,
    *,
    refined_first_atom: bool,
    base_active_requirement_set_id: str = "",
) -> RequirementPublicationPreview:
    refinement_bindings: tuple[RequirementRefinementBinding, ...] = ()
    refined_atomic_id = ""
    if refined_first_atom:
        refined_atomic_id = run.atomic_nodes[0].atomic_node_id or ""
        refinement_bindings = (
            RequirementRefinementBinding(
                suggestion_id=f"refinement-suggestion-{'8' * 32}",
                atomic_node_id=refined_atomic_id,
                revision=1,
                status=RequirementRefinementStatus.ACCEPTED,
                lineage_sha256="8" * 64,
            ),
        )
    lineage_sha256 = stable_publication_lineage_sha256(
        project_id=run.lineage.project_id,
        document_id=run.lineage.document_id,
        session_id=run.lineage.session_id,
        atomization_run_id=run.atomization_run_id,
        atomization_revision=run.revision,
        base_active_requirement_set_id=base_active_requirement_set_id,
        refinement_bindings=refinement_bindings,
    )
    entry_id_by_node_id = {
        node.node_id: stable_publication_entry_id(
            publication_lineage_sha256=lineage_sha256,
            atomization_node_id=node.node_id,
        )
        for node in run.nodes
    }
    entries: list[RequirementPublicationEntry] = []
    for index, node in enumerate(run.nodes, start=1):
        refined = node.atomic_node_id == refined_atomic_id
        statement = (
            "Refined atomic obligation 1"
            if refined
            else node.atomic_statement
            if node.atomic_statement is not None
            else node.source_text
        )
        entries.append(
            RequirementPublicationEntry(
                publication_entry_id=entry_id_by_node_id[node.node_id],
                atomization_node_id=node.node_id,
                atomic_node_id=node.atomic_node_id,
                parent_publication_entry_id=(
                    entry_id_by_node_id[node.parent_node_id]
                    if node.parent_node_id is not None
                    else None
                ),
                review_node_id=node.review_node_id,
                entry_kind=(
                    RequirementPublicationEntryKind.HEADING
                    if node.kind is RequirementAtomizationNodeKind.ROLLUP
                    else RequirementPublicationEntryKind.REQUIREMENT
                ),
                req_code=f"AI-{index:04d}",
                requirement_type=node.requirement_subtype.value,
                decision_mode=node.decision_mode,
                original_statement=node.source_text,
                published_statement=statement,
                source_spans=node.source_spans,
                source_order=node.source_order,
                table_evidence_binding_id=node.table_evidence_binding_id,
                refinement_suggestion_id=(
                    refinement_bindings[0].suggestion_id if refined else None
                ),
                refinement_revision=1 if refined else None,
            )
        )
    frozen_entries = tuple(entries)
    publication_key = stable_publication_key(
        lineage_sha256=lineage_sha256,
        entries=frozen_entries,
    )
    return RequirementPublicationPreview(
        preview_id=stable_publication_preview_id(publication_key=publication_key),
        project_id=run.lineage.project_id,
        document_id=run.lineage.document_id,
        session_id=run.lineage.session_id,
        atomization_run_id=run.atomization_run_id,
        atomization_revision=run.revision,
        base_active_requirement_set_id=base_active_requirement_set_id,
        lineage_sha256=lineage_sha256,
        publication_key=publication_key,
        refinement_bindings=refinement_bindings,
        entries=frozen_entries,
        created_at_utc=run.created_at_utc,
    )


def _receipt_with_updates(
    receipt: AtomizationScopeReceiptV1,
    **updates,
) -> AtomizationScopeReceiptV1:
    fields = receipt.model_dump(mode="python")
    fields.update(updates)
    fields["receipt_id"] = stable_atomization_scope_receipt_id(
        lineage_sha256=fields["lineage_sha256"],
        scope_revision=fields["scope_revision"],
        command_sha256=fields["command_sha256"],
        atomization_run_id=fields["atomization_run_id"],
    )
    fields["payload_sha256"] = "0" * 64
    draft = AtomizationScopeReceiptV1.model_construct(**fields)
    fields["payload_sha256"] = canonical_atomization_scope_receipt_sha256(draft)
    return AtomizationScopeReceiptV1.model_validate(fields)


def _run_with_nodes(
    run: RequirementAtomizationRun,
    nodes: tuple[RequirementAtomizationNode, ...],
) -> RequirementAtomizationRun:
    return RequirementAtomizationRun(
        atomization_run_id=stable_atomization_run_id(
            lineage_sha256=run.lineage.lineage_sha256,
            revision=run.revision,
            algorithm_version=run.algorithm_version,
            nodes=nodes,
            provider_runs=run.provider_runs,
        ),
        revision=run.revision,
        algorithm_version=run.algorithm_version,
        lineage=run.lineage,
        nodes=nodes,
        provider_runs=run.provider_runs,
        created_at_utc=run.created_at_utc,
    )


def _builder_like_tree(
    *,
    working_tree: RequirementTreeSnapshotV1,
    projection,
    requirement_entry_ids: dict[str, str],
) -> RequirementTreeSnapshotV1:
    business_ids = set(projection.source_business_tree_node_ids)
    row_by_id = {row.requirement_tree_node_id: row for row in projection.rows}

    def evidence_source_refs(row):
        return tuple(
            RequirementEvidenceSourceRefV1(
                kind=str(item.get("kind") or "SOURCE_REF"),
                source_id=str(item["source_id"]),
                owner_structure_node_id=str(
                    item.get("owner_structure_node_id") or row.structure_node_id or ""
                )
                or None,
                start_offset=item.get("start_offset"),
                end_offset=item.get("end_offset"),
                page_start=item.get("page_start") or row.page_start,
                page_end=(
                    item.get("page_end")
                    or row.page_end
                    or item.get("page_start")
                    or row.page_start
                ),
            )
            for item in row.source_refs
        )

    nodes: list[RequirementTreeNodeV1] = []
    bindings: list[RequirementEvidenceBindingV1] = []
    binding_id_by_entry_id: dict[str, str] = {}
    for source in working_tree.nodes:
        requirement_entry_id = requirement_entry_ids.get(source.id)
        if requirement_entry_id is not None:
            row = row_by_id[source.id]
            binding = RequirementEvidenceBindingV1.create(
                requirement_entry_id=requirement_entry_id,
                atomic_requirement_id=row.atomic_requirement_id,
                source_spans=row.source_spans,
                source_refs=evidence_source_refs(row),
                table_evidence_binding_id=row.table_evidence_binding_id,
            )
            bindings.append(binding)
            binding_id_by_entry_id[requirement_entry_id] = binding.binding_id
        nodes.append(
            source.model_copy(
                update={
                    "requirement_entry_id": requirement_entry_id,
                    "requirement_evidence_binding_id": (
                        binding_id_by_entry_id.get(requirement_entry_id or "")
                    ),
                }
            )
        )
    atom_nodes: list[RequirementTreeNodeV1] = []
    for row in projection.rows:
        if row.requirement_tree_node_id in business_ids:
            continue
        requirement_entry_id = requirement_entry_ids[row.requirement_tree_node_id]
        binding = RequirementEvidenceBindingV1.create(
            requirement_entry_id=requirement_entry_id,
            atomic_requirement_id=row.atomic_requirement_id,
            source_spans=row.source_spans,
            source_refs=evidence_source_refs(row),
            table_evidence_binding_id=row.table_evidence_binding_id,
        )
        bindings.append(binding)
        binding_id_by_entry_id[requirement_entry_id] = binding.binding_id
        atom_nodes.append(
            RequirementTreeNodeV1(
                id=f"requirement:{row.requirement_tree_node_id}",
                parent_id=_TITLE_NODE_ID,
                kind="REQUIREMENT",
                depth=2,
                source_order=row.source_order,
                source_offset=row.source_offset,
                label=row.label,
                summary=row.published_statement,
                structure_node_id=row.structure_node_id,
                requirement_entry_id=requirement_entry_id,
                workflow_node_id=row.workflow_node_id,
                source_refs=tuple(row.source_refs),
                requirement_count=1,
                reviewed_count=1 if row.status == "REVIEWED" else 0,
                status=row.status,
                classification=row.classification,
                projection_role="REQUIREMENT",
                semantic_state="CONFIRMED" if row.status == "REVIEWED" else "PROPOSED",
                requirement_evidence_binding_id=binding.binding_id,
                review_item_count=1,
                content_blocks=tuple(row.content_blocks),
                table_refs=tuple(row.table_refs),
                figure_refs=tuple(row.figure_refs),
            )
        )
    atom_builder_ids = tuple(node.id for node in atom_nodes)
    rebuilt_nodes = tuple(
        node.model_copy(update={"child_ids": (*node.child_ids, *atom_builder_ids)})
        if node.id == _TITLE_NODE_ID
        else node
        for node in nodes
    )
    total = len(requirement_entry_ids)
    rebuilt_nodes = tuple(
        node.model_copy(
            update={
                "requirement_count": total,
                "reviewed_count": total
                if node.id in {_STRUCTURE_NODE_ID, _TITLE_NODE_ID}
                else node.reviewed_count,
                "review_item_count": total
                if node.id in {_STRUCTURE_NODE_ID, _TITLE_NODE_ID}
                else node.review_item_count,
            }
        )
        for node in rebuilt_nodes
    )
    bundle = RequirementEvidenceBundleV1.create(
        source_ir_id=_SOURCE_IR_ID,
        source_sha256=_SOURCE_SHA256,
        structure_snapshot_id=_STRUCTURE_ID,
        requirement_bindings=tuple(bindings),
    )
    return RequirementTreeSnapshotV1(
        source_identity=working_tree.source_identity.model_copy(
            update={"requirement_set_id": "requirement-set-publication-v2"}
        ),
        structure_identity=working_tree.structure_identity,
        mode=RequirementTreeMode.REQUIREMENT_SET,
        state=RequirementTreeState.READY,
        root_node_ids=working_tree.root_node_ids,
        nodes=(*rebuilt_nodes, *atom_nodes),
        requirement_evidence_bundle=bundle,
    )


if __name__ == "__main__":
    unittest.main()
