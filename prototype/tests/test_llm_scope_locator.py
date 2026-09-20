from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import datetime, timezone
from unittest import mock

from app.application.extraction.llm_semantics import (
    extract_requirements_with_provider,
)
from app.application.extraction.build_llm_shadow_preview import (
    _scope_structure_nodes_with_ancestors,
    _serialize_failed_provision,
)
from app.application.extraction.build_shadow_preview import build_document_view
from app.application.extraction.llm_provision_batch import (
    LlmProvisionExtractionOutcome,
    LlmProvisionExtractionStatus,
    _partition_document_scope,
    _semantic_image_pages_for_scope,
)
from app.application.extraction.locate_llm_scope import (
    LLM_DOCUMENT_ROOT_SCOPE_LOCATOR_VERSION,
    LLM_EXTRACTION_SCOPE_LOCATOR_VERSION,
    LlmExtractionScopeLocator,
    LlmScopeLocationRejected,
    LlmScopeLocationRejectionCode,
    LlmScopeSourceLocatorConstraint,
    LlmSectionScopeSelector,
    build_llm_document_scope_locator,
    locate_llm_document_scope,
    locate_llm_extraction_scope,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
)
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRefKind,
    stable_structure_relation_id,
)
from tests.test_llm_semantics import _FakeProvider
from tests.test_semantics import (
    SOURCE_HASH,
    _flat_source,
    _hierarchical_source,
    _visual_source,
)


class LlmScopeLocatorTests(unittest.TestCase):
    def test_codex_scope_images_include_text_only_pdf_pages(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.PARAGRAPH, "The component shall operate."),
            )
        )
        tree = build_structure_tree(source)
        scope = tuple(
            node.structure_node_id
            for node in tree.nodes
            if node.kind is not StructureNodeKind.DOCUMENT
        )

        self.assertEqual(_semantic_image_pages_for_scope(source, tree, scope), ())
        self.assertEqual(
            _semantic_image_pages_for_scope(
                source,
                tree,
                scope,
                include_all_source_pages=True,
            ),
            (1,),
        )

    def test_document_locator_selects_every_numeric_body_clause(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.PARAGRAPH, "Introductory text."),
                (SourceNodeKind.HEADING, "1.1 Product"),
                (SourceNodeKind.PARAGRAPH, "The product shall operate."),
                (SourceNodeKind.HEADING, "2 Verification"),
                (SourceNodeKind.PARAGRAPH, "The test shall be recorded."),
            )
        )
        tree = build_structure_tree(source)

        locator = build_llm_document_scope_locator(source, tree)
        result = locate_llm_extraction_scope(source, tree, locator)

        selected_sections = tuple(
            node.explicit_label
            for node in tree.nodes
            if node.structure_node_id in result.root_section_structure_node_ids
        )
        self.assertEqual(selected_sections, ("1", "1.1", "2"))
        self.assertTrue(
            all(
                selector.source_locator is not None
                for selector in locator.section_selectors
            )
        )
        self.assertEqual(
            set(result.scope_structure_node_ids),
            {
                node.structure_node_id
                for node in tree.nodes
                if node.kind is not StructureNodeKind.DOCUMENT
            },
        )
        automatic = locate_llm_document_scope(source, tree)
        self.assertEqual(
            automatic.locator_version,
            LLM_EXTRACTION_SCOPE_LOCATOR_VERSION,
        )
        self.assertEqual(
            automatic.scope_structure_node_ids,
            result.scope_structure_node_ids,
        )

    def test_document_scope_falls_back_to_complete_root_branches(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.PARAGRAPH, "Unnumbered form metadata."),
                (SourceNodeKind.PARAGRAPH, "A table-led obligation."),
                (SourceNodeKind.PARAGRAPH, "A second table-led obligation."),
            )
        )
        tree = build_structure_tree(source)

        with self.assertRaisesRegex(
            ValueError,
            "no locatable numeric BODY clauses",
        ):
            build_llm_document_scope_locator(source, tree)

        first = locate_llm_document_scope(source, tree)
        repeated = locate_llm_document_scope(source, tree)
        expected = tuple(
            node.structure_node_id
            for node in tree.nodes
            if node.kind is not StructureNodeKind.DOCUMENT
        )

        self.assertEqual(
            first.locator_version,
            LLM_DOCUMENT_ROOT_SCOPE_LOCATOR_VERSION,
        )
        self.assertEqual(first.root_section_structure_node_ids, expected)
        self.assertEqual(first.scope_structure_node_ids, expected)
        self.assertEqual(first.locator_sha256, repeated.locator_sha256)

    def test_unresolved_root_branch_serializes_as_reviewable_source_context(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.PARAGRAPH, "Unnumbered authored context."),
                (SourceNodeKind.PARAGRAPH, "The product shall operate."),
            )
        )
        tree = build_structure_tree(source)
        located = locate_llm_document_scope(source, tree)
        provision = _partition_document_scope(source, tree, located)[0]
        outcome = LlmProvisionExtractionOutcome(
            root_section_structure_node_id=(provision.root_section_structure_node_id),
            scope_structure_node_ids=provision.scope_structure_node_ids,
            source_order=provision.source_order,
            status=LlmProvisionExtractionStatus.BLOCKED_INPUT,
            extraction=None,
            error_code="provision_input_invalid",
        )
        document_view = build_document_view(source, tree)
        groups, findings = _serialize_failed_provision(
            outcome,
            scope_nodes=_scope_structure_nodes_with_ancestors(
                tree,
                outcome.scope_structure_node_ids,
            ),
            source_catalog=document_view["source_catalog"],
            tables_by_id={item["id"]: item for item in document_view["tables"]},
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "BRANCH_REVIEW")
        self.assertEqual(groups[0]["semantic_class"], "UNRESOLVED")
        self.assertTrue(groups[0]["context_blocks"])
        self.assertEqual(findings[0]["code"], "llm_provision_blocked_input")

    def test_unique_path_selects_the_complete_non_nested_provision(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4 Verification"),
                (SourceNodeKind.HEADING, "4.12 Mechanical"),
                (SourceNodeKind.HEADING, "4.12.1 Equipment"),
                (SourceNodeKind.PARAGRAPH, "Equipment description."),
                (SourceNodeKind.HEADING, "4.12.1.1 Nested method"),
                (SourceNodeKind.PARAGRAPH, "Nested method description."),
                (SourceNodeKind.HEADING, "4.12.2 Next provision"),
            )
        )
        tree = build_structure_tree(source)

        result = locate_llm_extraction_scope(
            source,
            tree,
            _locator(("4", "4.12", "4.12.1")),
        )

        nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
        selected = tuple(nodes_by_id[item] for item in result.scope_structure_node_ids)
        self.assertEqual(result.locator_version, LLM_EXTRACTION_SCOPE_LOCATOR_VERSION)
        self.assertEqual(len(result.root_section_structure_node_ids), 1)
        self.assertEqual(selected[0].explicit_label, "4.12.1")
        self.assertEqual(
            tuple(item.kind for item in selected),
            (StructureNodeKind.SECTION, StructureNodeKind.CONTENT),
        )
        self.assertNotIn(
            "4.12.1.1",
            tuple(item.explicit_label for item in selected),
        )
        self.assertEqual(
            tuple(item.source_order for item in selected),
            tuple(sorted(item.source_order for item in selected)),
        )

    def test_same_locator_replays_across_adapter_and_resolver_versions(self) -> None:
        first_source = _versioned_source("1.0.0")
        second_source = _versioned_source("2.0.0")
        first_tree = build_structure_tree(first_source, resolver_version="1.0.0")
        second_tree = build_structure_tree(second_source, resolver_version="2.0.0")
        locator = _locator(("4", "4.12", "4.12.1"))

        first = locate_llm_extraction_scope(first_source, first_tree, locator)
        second = locate_llm_extraction_scope(second_source, second_tree, locator)

        self.assertEqual(first.locator_sha256, second.locator_sha256)
        self.assertNotEqual(
            first.root_section_structure_node_ids,
            second.root_section_structure_node_ids,
        )
        self.assertNotEqual(
            first.scope_structure_node_ids,
            second.scope_structure_node_ids,
        )
        self.assertEqual(len(first.scope_structure_node_ids), 2)
        self.assertEqual(len(second.scope_structure_node_ids), 2)

    def test_selector_order_does_not_change_the_canonical_scope_or_digest(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4 Verification"),
                (SourceNodeKind.HEADING, "4.12 Mechanical"),
                (SourceNodeKind.HEADING, "4.12.1 Equipment"),
                (SourceNodeKind.PARAGRAPH, "Equipment description."),
                (SourceNodeKind.HEADING, "4.13 Software"),
                (SourceNodeKind.HEADING, "4.13.3 Logging"),
                (SourceNodeKind.PARAGRAPH, "Logging description."),
            )
        )
        tree = build_structure_tree(source)
        selectors = (
            LlmSectionScopeSelector(("4", "4.12", "4.12.1")),
            LlmSectionScopeSelector(("4", "4.13", "4.13.3")),
        )
        forward_locator = LlmExtractionScopeLocator(
            source_sha256=SOURCE_HASH,
            section_selectors=selectors,
        )
        reverse_locator = LlmExtractionScopeLocator(
            source_sha256=SOURCE_HASH,
            section_selectors=tuple(reversed(selectors)),
        )

        forward = locate_llm_extraction_scope(source, tree, forward_locator)
        reverse = locate_llm_extraction_scope(source, tree, reverse_locator)

        self.assertEqual(forward_locator.locator_sha256, reverse_locator.locator_sha256)
        self.assertEqual(
            forward.root_section_structure_node_ids,
            reverse.root_section_structure_node_ids,
        )
        self.assertEqual(
            forward.scope_structure_node_ids,
            reverse.scope_structure_node_ids,
        )

    def test_duplicate_path_is_ambiguous_without_a_source_locator(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4 Verification"),
                (SourceNodeKind.HEADING, "4.12 Mechanical"),
                (SourceNodeKind.HEADING, "4.12.1 First occurrence"),
                (SourceNodeKind.PARAGRAPH, "First body."),
                (SourceNodeKind.HEADING, "4.12.1 Second occurrence"),
                (SourceNodeKind.PARAGRAPH, "Second body."),
            )
        )
        tree = build_structure_tree(source)

        with self.assertRaises(LlmScopeLocationRejected) as error:
            locate_llm_extraction_scope(
                source,
                tree,
                _locator(("4", "4.12", "4.12.1")),
            )

        self.assertIs(
            error.exception.code,
            LlmScopeLocationRejectionCode.SECTION_AMBIGUOUS,
        )
        constrained = locate_llm_extraction_scope(
            source,
            tree,
            _locator(
                ("4", "4.12", "4.12.1"),
                source_locator=LlmScopeSourceLocatorConstraint(
                    source_ref_kind=StructureSourceRefKind.SOURCE_NODE,
                    page_number=1,
                    block_index=4,
                ),
            ),
        )
        root = next(
            item
            for item in tree.nodes
            if item.structure_node_id == constrained.root_section_structure_node_ids[0]
        )
        self.assertEqual(root.source_order, 5)

    def test_visual_source_locator_can_disambiguate_without_source_ids(self) -> None:
        source = _visual_source(
            ("4 Verification", "4.12 Mechanical", "4.12.1 Equipment")
        )
        tree = build_structure_tree(source)

        result = locate_llm_extraction_scope(
            source,
            tree,
            _locator(
                ("4", "4.12", "4.12.1"),
                source_locator=LlmScopeSourceLocatorConstraint(
                    source_ref_kind=(StructureSourceRefKind.VISUAL_TEXT_OBSERVATION),
                    page_number=1,
                    observation_index=2,
                    provider_id="synthetic-visual-provider",
                ),
            ),
        )

        self.assertEqual(len(result.root_section_structure_node_ids), 1)

    def test_source_hash_mismatch_stops_before_any_semantic_provider_call(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Equipment",
            leaf_paragraph="Equipment description.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider()
        locator = LlmExtractionScopeLocator(
            source_sha256="a" * 64,
            section_selectors=(LlmSectionScopeSelector(("4", "4.12", "4.12.1")),),
        )

        with self.assertRaises(LlmScopeLocationRejected) as error:
            extract_requirements_with_provider(
                source,
                tree,
                provider,
                scope_locator=locator,
            )

        self.assertIs(
            error.exception.code,
            LlmScopeLocationRejectionCode.SOURCE_HASH_MISMATCH,
        )
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_missing_and_locator_mismatch_are_machine_readable(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Equipment",
            leaf_paragraph="Equipment description.",
        )
        tree = build_structure_tree(source)

        with self.assertRaises(LlmScopeLocationRejected) as missing:
            locate_llm_extraction_scope(
                source,
                tree,
                _locator(("4", "4.13", "4.13.3")),
            )
        self.assertIs(
            missing.exception.code,
            LlmScopeLocationRejectionCode.SECTION_NOT_FOUND,
        )

        with self.assertRaises(LlmScopeLocationRejected) as mismatch:
            locate_llm_extraction_scope(
                source,
                tree,
                _locator(
                    ("4", "4.12", "4.12.1"),
                    source_locator=LlmScopeSourceLocatorConstraint(
                        source_ref_kind=StructureSourceRefKind.SOURCE_NODE,
                        page_number=99,
                    ),
                ),
            )
        self.assertIs(
            mismatch.exception.code,
            LlmScopeLocationRejectionCode.SOURCE_LOCATOR_MISMATCH,
        )

    def test_different_selectors_resolving_to_one_section_are_rejected(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4 Verification"),
                (SourceNodeKind.HEADING, "4.12 Mechanical"),
                (SourceNodeKind.HEADING, "4.12.1 Equipment"),
                (SourceNodeKind.PARAGRAPH, "Equipment description."),
            )
        )
        tree = build_structure_tree(source)
        path = ("4", "4.12", "4.12.1")
        locator = LlmExtractionScopeLocator(
            source_sha256=SOURCE_HASH,
            section_selectors=(
                LlmSectionScopeSelector(path),
                LlmSectionScopeSelector(
                    path,
                    source_locator=LlmScopeSourceLocatorConstraint(
                        source_ref_kind=StructureSourceRefKind.SOURCE_NODE,
                        page_number=1,
                        block_index=2,
                    ),
                ),
            ),
        )

        with self.assertRaises(LlmScopeLocationRejected) as error:
            locate_llm_extraction_scope(source, tree, locator)

        self.assertIs(
            error.exception.code,
            LlmScopeLocationRejectionCode.DUPLICATE_SECTION,
        )

    def test_continuation_crossing_scope_is_rejected_as_incomplete(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4 Verification"),
                (SourceNodeKind.HEADING, "4.12 Mechanical"),
                (SourceNodeKind.HEADING, "4.12.1 Equipment"),
                (SourceNodeKind.PARAGRAPH, "Equipment description."),
                (SourceNodeKind.HEADING, "4.12.2 Next provision"),
            )
        )
        tree = build_structure_tree(source)
        selected_root = next(
            item for item in tree.nodes if item.explicit_label == "4.12.1"
        )
        selected_body = next(
            item
            for item in tree.nodes
            if item.parent_structure_node_id == selected_root.structure_node_id
            and item.kind is StructureNodeKind.CONTENT
        )
        outside = next(item for item in tree.nodes if item.explicit_label == "4.12.2")
        relation = StructureRelation(
            relation_id=stable_structure_relation_id(
                source_ir_id=tree.source_ir_id,
                resolver_id=tree.resolver_id,
                resolver_version=tree.resolver_version,
                kind=StructureRelationKind.CONTINUES,
                source_structure_node_id=selected_body.structure_node_id,
                target_structure_node_id=outside.structure_node_id,
                source_refs=(*selected_body.source_refs, *outside.source_refs),
            ),
            kind=StructureRelationKind.CONTINUES,
            source_structure_node_id=selected_body.structure_node_id,
            target_structure_node_id=outside.structure_node_id,
            source_refs=(*selected_body.source_refs, *outside.source_refs),
        )
        tampered = tree.model_copy(update={"relations": (*tree.relations, relation)})

        with (
            mock.patch(
                "app.application.extraction.locate_llm_scope.validate_structure_tree_against_source_ir"
            ),
            self.assertRaises(LlmScopeLocationRejected) as error,
        ):
            locate_llm_extraction_scope(
                source,
                tampered,
                _locator(("4", "4.12", "4.12.1")),
            )

        self.assertIs(
            error.exception.code,
            LlmScopeLocationRejectionCode.INCOMPLETE_PROVISION,
        )

    def test_n_dot_zero_root_uses_the_logical_numeric_path(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "5.0 Requirements"),
                (SourceNodeKind.PARAGRAPH, "General requirement."),
                (SourceNodeKind.HEADING, "5.1 Child section"),
            )
        )
        tree = build_structure_tree(source)

        result = locate_llm_extraction_scope(source, tree, _locator(("5",)))
        root = next(
            item
            for item in tree.nodes
            if item.structure_node_id == result.root_section_structure_node_ids[0]
        )

        self.assertEqual(root.explicit_label, "5.0")

    def test_automatic_document_scope_expands_incomplete_numeric_selector(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.PARAGRAPH, "Authored unnumbered lead-in."),
                (SourceNodeKind.HEADING, "1 Product requirement"),
                (SourceNodeKind.PARAGRAPH, "The product shall operate."),
            )
        )
        tree = build_structure_tree(source)
        lead_in = next(
            item
            for item in tree.nodes
            if item.kind is StructureNodeKind.CONTENT and item.source_order == 1
        )
        section = next(item for item in tree.nodes if item.explicit_label == "1")
        body = next(
            item
            for item in tree.nodes
            if item.parent_structure_node_id == section.structure_node_id
            and item.kind is StructureNodeKind.CONTENT
        )
        relation = StructureRelation(
            relation_id=stable_structure_relation_id(
                source_ir_id=tree.source_ir_id,
                resolver_id=tree.resolver_id,
                resolver_version=tree.resolver_version,
                kind=StructureRelationKind.CONTINUES,
                source_structure_node_id=lead_in.structure_node_id,
                target_structure_node_id=body.structure_node_id,
                source_refs=(*lead_in.source_refs, *body.source_refs),
            ),
            kind=StructureRelationKind.CONTINUES,
            source_structure_node_id=lead_in.structure_node_id,
            target_structure_node_id=body.structure_node_id,
            source_refs=(*lead_in.source_refs, *body.source_refs),
        )
        expanded = tree.model_copy(update={"relations": (*tree.relations, relation)})

        with (
            mock.patch(
                "app.application.extraction.locate_llm_scope.validate_structure_tree_against_source_ir"
            ),
            mock.patch(
                "app.application.extraction.locate_llm_scope.validate_structure_snapshot_against_source_ir"
            ),
        ):
            with self.assertRaises(LlmScopeLocationRejected) as manual_error:
                locate_llm_extraction_scope(source, expanded, _locator(("1",)))
            automatic = locate_llm_document_scope(source, expanded)
        expected = tuple(
            item.structure_node_id
            for item in expanded.nodes
            if item.kind is not StructureNodeKind.DOCUMENT
        )
        self.assertIs(
            manual_error.exception.code,
            LlmScopeLocationRejectionCode.INCOMPLETE_PROVISION,
        )
        self.assertEqual(
            automatic.locator_version,
            LLM_DOCUMENT_ROOT_SCOPE_LOCATOR_VERSION,
        )
        self.assertEqual(automatic.scope_structure_node_ids, expected)

    def test_locator_contract_rejects_partial_paths_and_conflicting_scope_modes(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "every numeric ancestor"):
            LlmSectionScopeSelector(("4", "4.12.1"))
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Equipment",
            leaf_paragraph="Equipment description.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider()
        root_id = next(
            item.structure_node_id
            for item in tree.nodes
            if item.explicit_label == "4.12.1"
        )
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            extract_requirements_with_provider(
                source,
                tree,
                provider,
                scope_structure_node_ids=(root_id,),
                scope_locator=_locator(("4", "4.12", "4.12.1")),
            )
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)


def _locator(
    section_path: tuple[str, ...],
    *,
    source_locator: LlmScopeSourceLocatorConstraint | None = None,
) -> LlmExtractionScopeLocator:
    return LlmExtractionScopeLocator(
        source_sha256=SOURCE_HASH,
        section_selectors=(
            LlmSectionScopeSelector(
                normalized_section_path=section_path,
                source_locator=source_locator,
            ),
        ),
    )


def _versioned_source(adapter_version: str) -> SourceIRSnapshot:
    specs = (
        (SourceNodeKind.HEADING, "4 Verification"),
        (SourceNodeKind.HEADING, "4.12 Mechanical"),
        (SourceNodeKind.HEADING, "4.12.1 Equipment"),
        (SourceNodeKind.PARAGRAPH, "Equipment description."),
    )
    nodes = []
    for index, (kind, raw_text) in enumerate(specs):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=index,
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_HASH,
                    adapter_id="scope-locator-test-pdf",
                    adapter_version=adapter_version,
                    kind=kind,
                    native_locator=locator,
                ),
                kind=kind,
                source_order=index,
                raw_text=raw_text,
                native_locator=locator,
            )
        )
    document_id = stable_document_id(SOURCE_HASH)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id="scope-locator-test-pdf",
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        adapter_id="scope-locator-test-pdf",
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
    )


if __name__ == "__main__":
    unittest.main()
