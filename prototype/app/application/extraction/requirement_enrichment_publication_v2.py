from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Mapping

from app.domain.atomization_scope import (
    AtomizationScopeReceiptV1,
    stable_effective_entry_classification_sha256,
)
from app.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    EntryClassificationStatus,
    EntryClassificationType,
    canonical_entry_classification_payload_sha256,
    entry_classification_nodes_in_tree_order,
)
from app.domain.extraction.requirement_enrichment import (
    RequirementAtomizationNode,
    RequirementAtomizationNodeKind,
    RequirementAtomizationRun,
    RequirementPublicationEntry,
    RequirementPublicationEntryKind,
    RequirementPublicationPreview,
    RequirementRefinementStatus,
    canonical_model_sha256,
    stable_publication_entry_id,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeState,
    canonical_requirement_tree_sha256,
)
from app.domain.extraction.requirement_view import RequirementDecisionMode
from app.domain.extraction.table_evidence import RequirementEvidenceSourceRefV1
from app.domain.requirement_enrichment_publication_v2 import (
    RequirementEnrichmentAnalysisDispositionV2,
    RequirementEnrichmentAtomizationStateV2,
    RequirementEnrichmentPublicationBuilderKindV2,
    RequirementEnrichmentPublicationEntryKindV2,
    RequirementEnrichmentPublicationProjectionV2,
    RequirementEnrichmentPublicationRowV2,
    canonical_publication_projection_v2_sha256,
)


_STABLE_BUILDER_BINDING_ROLES = frozenset(
    {
        "STRUCTURE_PROXY",
        "DIRECT_CONTENT",
        "WHOLE_TABLE_REVIEW",
        "TABLE_RECORD",
    }
)


def build_requirement_enrichment_publication_projection_v2(
    *,
    accepted_working_tree: RequirementTreeSnapshotV1,
    accepted_entry_classification: EntryClassificationSnapshotV1,
    atomization_state: RequirementEnrichmentAtomizationStateV2 | str,
    atomization_scope_receipt: AtomizationScopeReceiptV1 | None = None,
    atomization_run: RequirementAtomizationRun | None = None,
    publication_preview: RequirementPublicationPreview | None = None,
) -> RequirementEnrichmentPublicationProjectionV2:
    """Project accepted W3 truth into immutable W4 logical rows.

    This is deliberately a pure boundary.  It consumes an independently
    accepted three-class snapshot and, only for APPLIED, the exact persisted
    scope receipt, atomization run and publication preview.  It neither invokes
    a model nor creates database identities.
    """

    tree = _validated_working_tree(accepted_working_tree)
    classification = _validated_accepted_classification(
        tree,
        accepted_entry_classification,
    )
    state = RequirementEnrichmentAtomizationStateV2(atomization_state)
    business_nodes = entry_classification_nodes_in_tree_order(tree)
    if not business_nodes:
        raise ValueError("accepted W3 working tree has no business rows")
    classifications_by_tree_id = {
        item.tree_node_id: item for item in classification.entries
    }
    workflow_ids = tuple(node.workflow_node_id or node.id for node in business_nodes)
    node_by_workflow_id = dict(zip(workflow_ids, business_nodes, strict=True))

    receipt: AtomizationScopeReceiptV1 | None = None
    run: RequirementAtomizationRun | None = None
    preview: RequirementPublicationPreview | None = None
    families_by_review_id: dict[str, tuple[RequirementAtomizationNode, ...]] = {}
    preview_by_node_id: dict[str, RequirementPublicationEntry] = {}
    applied_review_node_ids: tuple[str, ...] = ()

    if state is RequirementEnrichmentAtomizationStateV2.NOT_RUN:
        if any(
            item is not None
            for item in (
                atomization_scope_receipt,
                atomization_run,
                publication_preview,
            )
        ):
            raise ValueError("NOT_RUN publication cannot consume atomization sidecars")
    else:
        if (
            atomization_scope_receipt is None
            or atomization_run is None
            or publication_preview is None
        ):
            raise ValueError(
                "APPLIED publication requires the exact receipt, run, and preview"
            )
        receipt = _validated_scope_receipt(atomization_scope_receipt)
        run = _validated_atomization_run(atomization_run)
        preview = _validated_publication_preview(publication_preview)
        families_by_review_id, preview_by_node_id = _validate_applied_sidecars(
            tree=tree,
            classification=classification,
            node_by_workflow_id=node_by_workflow_id,
            receipt=receipt,
            run=run,
            preview=preview,
        )
        applied_review_node_ids = receipt.cumulative_review_node_ids

    applied = set(applied_review_node_ids)
    rows: list[RequirementEnrichmentPublicationRowV2] = []
    for node in business_nodes:
        classification_entry = classifications_by_tree_id[node.id]
        assert classification_entry.effective_type is not None
        effective_type = classification_entry.effective_type
        workflow_node_id = node.workflow_node_id or node.id
        family = families_by_review_id.get(workflow_node_id, ())
        if workflow_node_id not in applied:
            # Pass-through families prove that an unselected source row remains
            # compatible with the persisted run.  They are not applied output:
            # only cumulative scope may replace the accepted W3 coarse row.
            rows.append(_base_row(node, effective_type))
            continue

        rollups = tuple(
            item
            for item in family
            if item.kind is RequirementAtomizationNodeKind.ROLLUP
        )
        atoms = tuple(
            item
            for item in family
            if item.kind is RequirementAtomizationNodeKind.ATOMIC_OBLIGATION
        )
        if not rollups:
            atom = atoms[0]
            rows.append(
                _one_to_one_atom_row(
                    node,
                    effective_type,
                    atom,
                    preview_by_node_id[atom.node_id],
                )
            )
            continue
        parent = rollups[0]
        rows.append(
            _multi_atom_parent_row(
                node,
                parent,
                preview_by_node_id[parent.node_id],
            )
        )
        rows.extend(
            _atom_child_row(
                node,
                atom,
                preview_by_node_id[atom.node_id],
            )
            for atom in atoms
        )

    tree_sha256 = canonical_requirement_tree_sha256(tree)
    fields: dict[str, Any] = {
        "project_id": tree.source_identity.project_id,
        "document_id": tree.source_identity.document_id,
        "source_sha256": tree.source_identity.source_sha256,
        "effective_structure_id": tree.structure_identity.effective_snapshot_id,
        "accepted_working_tree_id": classification.working_tree_id,
        "accepted_working_tree_sha256": tree_sha256,
        "entry_classification_snapshot_id": classification.snapshot_id,
        "entry_classification_revision": classification.revision,
        "entry_classification_sha256": classification.payload_sha256,
        "entry_classification_content_sha256": (
            stable_effective_entry_classification_sha256(classification)
        ),
        "atomization_state": state,
        "atomization_scope_receipt_id": receipt.receipt_id if receipt else "",
        "atomization_scope_receipt_sha256": (receipt.payload_sha256 if receipt else ""),
        "atomization_scope_revision": receipt.scope_revision if receipt else 0,
        "atomization_active_revision": receipt.active_revision if receipt else 0,
        "atomization_classification_content_sha256": (
            receipt.entry_classification_content_sha256 if receipt else ""
        ),
        "atomization_run_id": run.atomization_run_id if run else "",
        "atomization_revision": run.revision if run else 0,
        "atomization_run_sha256": canonical_model_sha256(run) if run else "",
        "publication_preview_id": preview.preview_id if preview else "",
        "publication_preview_sha256": (
            canonical_model_sha256(preview) if preview else ""
        ),
        "publication_key": preview.publication_key if preview else "",
        "publication_lineage_sha256": preview.lineage_sha256 if preview else "",
        "applied_review_node_ids": applied_review_node_ids,
        "source_business_tree_node_ids": tuple(node.id for node in business_nodes),
        "rows": tuple(rows),
        "projection_sha256": "0" * 64,
    }
    draft = RequirementEnrichmentPublicationProjectionV2.model_construct(**fields)
    fields["projection_sha256"] = canonical_publication_projection_v2_sha256(draft)
    return RequirementEnrichmentPublicationProjectionV2.model_validate(fields)


def build_requirement_enrichment_publication_builder_entries_v2(
    projection: RequirementEnrichmentPublicationProjectionV2,
    *,
    requirement_entry_ids: Mapping[str, str],
) -> tuple[dict[str, Any], ...]:
    """Serialize exact rows for the protected v1 tree builder.

    ``projection_role`` intentionally remains the W3 binding role so the
    existing builder can find its Structure/table anchor.  The final logical
    role is carried separately and applied only after exact tree/evidence
    verification.
    """

    plan = _validated_projection(projection)
    entry_ids = _validated_requirement_entry_ids(plan, requirement_entry_ids)
    entries: list[dict[str, Any]] = []
    for row in plan.rows:
        item: dict[str, Any] = {
            "requirement_tree_node_id": _builder_stable_id(row),
            "final_requirement_tree_node_id": row.requirement_tree_node_id,
            "parent_requirement_tree_node_id": (row.parent_requirement_tree_node_id),
            "source_working_tree_node_id": row.source_working_tree_node_id,
            "requirement_entry_id": entry_ids[row.requirement_tree_node_id],
            "workflow_node_id": row.workflow_node_id,
            "review_node_id": row.workflow_node_id,
            "atomization_node_id": row.atomization_node_id,
            "atomic_requirement_id": row.atomic_requirement_id,
            "atomic_node_id": row.atomic_requirement_id,
            "kind": row.builder_kind.value,
            "entry_kind": row.entry_kind.value,
            "final_decision_mode": row.decision_mode.value,
            "owner_structure_node_id": row.structure_node_id,
            "source_order": row.source_order,
            "source_offset": row.source_offset,
            "title": row.label,
            "source_text": row.original_statement,
            "published_statement": row.published_statement,
            "source_spans": [item.model_dump(mode="json") for item in row.source_spans],
            "page_start": row.page_start,
            "page_end": row.page_end,
            "source_refs": list(row.source_refs),
            "status": row.status,
            "classification": row.classification,
            "projection_role": row.builder_binding_role,
            "builder_binding_role": row.builder_binding_role,
            "final_projection_role": row.final_projection_role,
            "intrinsic_analysis_eligible": row.intrinsic_analysis_eligible,
            "default_disposition": row.default_disposition.value,
            "table_record_id": row.table_record_id,
            "table_evidence_binding_id": row.table_evidence_binding_id,
            "deterministic_rule_evidence": list(row.deterministic_rule_evidence),
            "include_in_view": row.include_in_view,
            "content_blocks": list(row.content_blocks),
            "editable_structure_node_ids": list(row.editable_structure_node_ids),
            "table_refs": list(row.table_refs),
            "figure_refs": list(row.figure_refs),
        }
        if row.decision_mode is not RequirementDecisionMode.NONE:
            item["decision_mode"] = row.decision_mode.value
        entries.append(item)
    return tuple(entries)


def overlay_requirement_enrichment_publication_tree_v2(
    *,
    accepted_working_tree: RequirementTreeSnapshotV1,
    builder_tree: RequirementTreeSnapshotV1,
    projection: RequirementEnrichmentPublicationProjectionV2,
    materialized_entries: tuple[Mapping[str, Any], ...],
) -> RequirementTreeSnapshotV1:
    """Verify database/tree/evidence cover, then apply final W4 row semantics."""

    working = _validated_working_tree(accepted_working_tree)
    built = _validated_builder_tree(builder_tree)
    plan = _validated_projection(projection)
    entry_ids = _validated_materialized_entries(plan, materialized_entries)
    if canonical_requirement_tree_sha256(working) != plan.accepted_working_tree_sha256:
        raise ValueError("accepted W3 tree changed after the publication projection")
    if (
        working.source_identity.project_id != plan.project_id
        or working.source_identity.document_id != plan.document_id
        or working.source_identity.source_sha256 != plan.source_sha256
        or working.structure_identity.effective_snapshot_id
        != plan.effective_structure_id
    ):
        raise ValueError("publication projection binding does not match the W3 tree")
    if (
        built.mode is not RequirementTreeMode.REQUIREMENT_SET
        or built.state is not RequirementTreeState.READY
        or built.requirement_evidence_bundle is None
        or built.structure_identity != working.structure_identity
        or built.source_identity.project_id != plan.project_id
        or built.source_identity.document_id != plan.document_id
        or built.source_identity.source_sha256 != plan.source_sha256
    ):
        raise ValueError("builder tree is not the bound READY Requirement Set skeleton")

    working_by_id = {node.id: node for node in working.nodes}
    built_by_id = {node.id: node for node in built.nodes}
    business_ids = set(plan.source_business_tree_node_ids)
    if business_ids != {
        node.id for node in entry_classification_nodes_in_tree_order(working)
    }:
        raise ValueError("publication projection no longer exactly covers W3 rows")

    built_id_by_final_id = {
        row.requirement_tree_node_id: _expected_builder_tree_node_id(row)
        for row in plan.rows
    }
    if len(set(built_id_by_final_id.values())) != len(built_id_by_final_id):
        raise ValueError("builder tree row identities are ambiguous")
    if any(built_id not in built_by_id for built_id in built_id_by_final_id.values()):
        raise ValueError("builder tree is missing an exact publication row")
    expected_built_ids = set(working_by_id) | {
        built_id
        for final_id, built_id in built_id_by_final_id.items()
        if final_id not in business_ids
    }
    if set(built_by_id) != expected_built_ids:
        raise ValueError(
            "builder tree contains extra nodes outside the exact projection"
        )

    bundle = built.requirement_evidence_bundle
    assert bundle is not None
    binding_by_entry_id = {
        binding.requirement_entry_id: binding for binding in bundle.requirement_bindings
    }
    expected_database_ids = set(entry_ids.values())
    tree_database_ids = {
        node.requirement_entry_id
        for node in built.nodes
        if node.requirement_entry_id is not None
    }
    if (
        tree_database_ids != expected_database_ids
        or set(binding_by_entry_id) != expected_database_ids
    ):
        raise ValueError("database, tree, and evidence do not exactly cover the plan")

    table_binding_ids = {item.binding_id for item in built.table_evidence_bindings}
    for row in plan.rows:
        built_node = built_by_id[built_id_by_final_id[row.requirement_tree_node_id]]
        requirement_entry_id = entry_ids[row.requirement_tree_node_id]
        binding = binding_by_entry_id[requirement_entry_id]
        _validate_built_row(
            row=row,
            built_node=built_node,
            requirement_entry_id=requirement_entry_id,
            evidence_binding=binding,
            table_binding_ids=table_binding_ids,
        )

    final_id_by_built_id = {
        built_id: final_id for final_id, built_id in built_id_by_final_id.items()
    }
    final_id_by_built_id.update(
        (node_id, node_id)
        for node_id in working_by_id
        if node_id not in final_id_by_built_id
    )
    row_by_final_id = {row.requirement_tree_node_id: row for row in plan.rows}
    publication_parent_by_id, publication_children_by_id = _publication_topology(
        working=working,
        plan=plan,
    )

    projected: dict[str, RequirementTreeNodeV1] = {}
    for built_node in built.nodes:
        final_id = final_id_by_built_id[built_node.id]
        row = row_by_final_id.get(final_id)
        if final_id not in publication_parent_by_id:
            raise ValueError("publication topology is missing an exact logical row")
        parent_id = publication_parent_by_id[final_id]
        child_ids = publication_children_by_id[final_id]

        if row is None:
            projected[final_id] = built_node.model_copy(
                update={
                    "id": final_id,
                    "parent_id": parent_id,
                    "child_ids": child_ids,
                }
            )
            continue
        updates = _final_tree_row_updates(
            row=row,
            built_node=built_node,
            final_id=final_id,
            parent_id=parent_id,
            child_ids=child_ids,
        )
        projected[final_id] = built_node.model_copy(update=updates)

    parent_by_id = {node_id: node.parent_id for node_id, node in projected.items()}
    child_ids_by_id = {node_id: node.child_ids for node_id, node in projected.items()}
    depths = _validated_depths(
        root_ids=working.root_node_ids,
        parent_by_id=parent_by_id,
        child_ids_by_id=child_ids_by_id,
    )
    self_counts = _working_self_counts(working)
    for row in plan.rows:
        if row.requirement_tree_node_id in working_by_id:
            continue
        self_counts[row.requirement_tree_node_id] = (
            1,
            1 if row.status == "NEEDS_ATTENTION" else 0,
            1 if row.status == "REVIEWED" else 0,
            1,
        )
    aggregate_counts = _aggregate_counts(
        root_ids=working.root_node_ids,
        child_ids_by_id=child_ids_by_id,
        self_counts=self_counts,
    )
    ordered_ids = _preorder_ids(working.root_node_ids, child_ids_by_id)
    nodes = tuple(
        projected[node_id].model_copy(
            update={
                "depth": depths[node_id],
                "requirement_count": aggregate_counts[node_id][0],
                "attention_count": aggregate_counts[node_id][1],
                "reviewed_count": aggregate_counts[node_id][2],
                "review_item_count": aggregate_counts[node_id][3],
            }
        )
        for node_id in ordered_ids
    )
    try:
        result = RequirementTreeSnapshotV1.model_validate(
            built.model_copy(
                update={
                    "root_node_ids": working.root_node_ids,
                    "nodes": nodes,
                }
            ).model_dump(mode="json")
        )
    except ValueError as exc:
        raise ValueError(
            f"publication tree overlay failed exact validation: {exc}"
        ) from exc
    _validate_final_tree_rows(
        result=result,
        plan=plan,
        entry_ids=entry_ids,
    )
    return result


def validate_requirement_enrichment_publication_tree_v2(
    *,
    tree: RequirementTreeSnapshotV1,
    projection: RequirementEnrichmentPublicationProjectionV2,
    requirement_entry_ids: Mapping[str, str],
) -> None:
    """Fail closed when a persisted final tree no longer matches its W4 plan."""

    result = _validated_builder_tree(tree)
    plan = _validated_projection(projection)
    entry_ids = _validated_requirement_entry_ids(plan, requirement_entry_ids)
    if (
        result.mode is not RequirementTreeMode.REQUIREMENT_SET
        or result.state is not RequirementTreeState.READY
        or result.requirement_evidence_bundle is None
        or result.source_identity.project_id != plan.project_id
        or result.source_identity.document_id != plan.document_id
        or result.source_identity.source_sha256 != plan.source_sha256
        or result.structure_identity.effective_snapshot_id
        != plan.effective_structure_id
    ):
        raise ValueError("persisted W4 tree does not match the publication lineage")
    expected_database_ids = set(entry_ids.values())
    tree_database_ids = {
        node.requirement_entry_id
        for node in result.nodes
        if node.requirement_entry_id is not None
    }
    bundle = result.requirement_evidence_bundle
    assert bundle is not None
    binding_by_entry_id = {
        binding.requirement_entry_id: binding for binding in bundle.requirement_bindings
    }
    if (
        tree_database_ids != expected_database_ids
        or set(binding_by_entry_id) != expected_database_ids
    ):
        raise ValueError("persisted W4 tree/evidence does not exactly cover database rows")
    node_by_id = {node.id: node for node in result.nodes}
    for row in plan.rows:
        node = node_by_id.get(row.requirement_tree_node_id)
        if node is None or node.requirement_evidence_binding_id is None:
            raise ValueError("persisted W4 tree is missing exact row evidence")
        binding = binding_by_entry_id[entry_ids[row.requirement_tree_node_id]]
        if (
            node.requirement_evidence_binding_id != binding.binding_id
            or binding.atomic_requirement_id != row.atomic_requirement_id
            or binding.source_spans != row.source_spans
            or binding.source_refs != _expected_evidence_source_refs(row)
            or binding.table_evidence_binding_id != row.table_evidence_binding_id
        ):
            raise ValueError("persisted W4 evidence does not match the publication plan")
    _validate_final_tree_rows(result=result, plan=plan, entry_ids=entry_ids)


def _validated_working_tree(
    tree: RequirementTreeSnapshotV1,
) -> RequirementTreeSnapshotV1:
    try:
        validated = RequirementTreeSnapshotV1.model_validate(
            tree.model_dump(mode="json")
        )
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"accepted W3 tree failed exact validation: {exc}") from exc
    if (
        validated.mode is not RequirementTreeMode.WORKING
        or validated.state is not RequirementTreeState.READY
    ):
        raise ValueError("accepted W3 working tree must be a READY WORKING tree")
    return validated


def _validated_builder_tree(
    tree: RequirementTreeSnapshotV1,
) -> RequirementTreeSnapshotV1:
    try:
        return RequirementTreeSnapshotV1.model_validate(tree.model_dump(mode="json"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"builder tree failed exact validation: {exc}") from exc


def _validated_accepted_classification(
    tree: RequirementTreeSnapshotV1,
    snapshot: EntryClassificationSnapshotV1,
) -> EntryClassificationSnapshotV1:
    try:
        validated = EntryClassificationSnapshotV1.model_validate(
            snapshot.model_dump(mode="json")
        )
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            f"entry classification failed exact validation: {exc}"
        ) from exc
    if validated.status is not EntryClassificationStatus.ACCEPTED:
        raise ValueError("publication requires an ACCEPTED entry classification")
    if (
        canonical_entry_classification_payload_sha256(validated)
        != validated.payload_sha256
    ):
        raise ValueError("accepted entry classification payload hash is invalid")
    tree_sha256 = canonical_requirement_tree_sha256(tree)
    if (
        validated.project_id != tree.source_identity.project_id
        or validated.sor_document_id != tree.source_identity.document_id
        or validated.source_sha256 != tree.source_identity.source_sha256
        or validated.working_tree_sha256 != tree_sha256
        or validated.effective_structure_id
        != tree.structure_identity.effective_snapshot_id
    ):
        raise ValueError(
            "entry classification does not match the accepted working tree"
        )
    business_nodes = entry_classification_nodes_in_tree_order(tree)
    expected = tuple(
        (
            node.id,
            node.workflow_node_id or node.id,
            node.projection_role,
        )
        for node in business_nodes
    )
    actual = tuple(
        (entry.tree_node_id, entry.workflow_node_id, entry.projection_role)
        for entry in validated.entries
    )
    if actual != expected:
        raise ValueError(
            "accepted entry classification does not exactly cover the W3 rows"
        )
    return validated


def _validated_scope_receipt(
    receipt: AtomizationScopeReceiptV1,
) -> AtomizationScopeReceiptV1:
    try:
        return AtomizationScopeReceiptV1.model_validate(receipt.model_dump(mode="json"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            f"atomization scope receipt failed exact validation: {exc}"
        ) from exc


def _validated_atomization_run(
    run: RequirementAtomizationRun,
) -> RequirementAtomizationRun:
    try:
        return RequirementAtomizationRun.model_validate(run.model_dump(mode="json"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"atomization run failed exact validation: {exc}") from exc


def _validated_publication_preview(
    preview: RequirementPublicationPreview,
) -> RequirementPublicationPreview:
    try:
        return RequirementPublicationPreview.model_validate(
            preview.model_dump(mode="json")
        )
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"publication preview failed exact validation: {exc}") from exc


def _validate_applied_sidecars(
    *,
    tree: RequirementTreeSnapshotV1,
    classification: EntryClassificationSnapshotV1,
    node_by_workflow_id: Mapping[str, RequirementTreeNodeV1],
    receipt: AtomizationScopeReceiptV1,
    run: RequirementAtomizationRun,
    preview: RequirementPublicationPreview,
) -> tuple[
    dict[str, tuple[RequirementAtomizationNode, ...]],
    dict[str, RequirementPublicationEntry],
]:
    if receipt.active_revision < 1:
        raise ValueError(
            "APPLIED publication requires an active scope; use NOT_RUN after undo"
        )
    lineage = run.lineage
    if (
        lineage.project_id != tree.source_identity.project_id
        or lineage.document_id != tree.source_identity.document_id
        or lineage.source_sha256 != tree.source_identity.source_sha256
        or lineage.structure_snapshot_id
        != tree.structure_identity.effective_snapshot_id
    ):
        raise ValueError("atomization run does not match the accepted W3 tree")
    if (
        receipt.project_id != lineage.project_id
        or receipt.document_id != lineage.document_id
        or receipt.session_id != lineage.session_id
        or receipt.review_revision != lineage.review_revision
        or receipt.lineage_sha256 != lineage.lineage_sha256
        or receipt.atomization_run_id != run.atomization_run_id
        or receipt.atomization_run_revision != run.revision
        or receipt.scope_revision != run.revision
    ):
        raise ValueError("scope receipt does not match the exact atomization run")

    current_content_sha256 = stable_effective_entry_classification_sha256(
        classification
    )
    content_equivalent = (
        receipt.entry_classification_content_sha256 == current_content_sha256
    )
    if not content_equivalent:
        raise ValueError(
            "scope receipt classification is not content-equivalent to accepted truth"
        )

    expected_family_ids = {
        *receipt.cumulative_review_node_ids,
        *receipt.pass_through_review_node_ids,
    }
    known_workflow_ids = set(node_by_workflow_id)
    if not expected_family_ids.issubset(known_workflow_ids):
        raise ValueError("scope receipt contains an extra W3 business family")
    grouped: dict[str, list[RequirementAtomizationNode]] = defaultdict(list)
    for node in run.nodes:
        if node.review_node_id not in known_workflow_ids:
            raise ValueError("atomization run contains an extra W3 business family")
        grouped[node.review_node_id].append(node)
    if set(grouped) != expected_family_ids:
        raise ValueError(
            "atomization run families do not exactly match cumulative and pass-through scope"
        )
    families = {key: tuple(value) for key, value in grouped.items()}

    classification_by_workflow_id = {
        item.workflow_node_id: item for item in classification.entries
    }
    cumulative = set(receipt.cumulative_review_node_ids)
    for review_node_id, family in families.items():
        allow_rollup = review_node_id in cumulative
        _validated_atomization_family(
            review_node_id,
            family,
            allow_rollup=allow_rollup,
        )
        if allow_rollup:
            classification_entry = classification_by_workflow_id[review_node_id]
            source_node = node_by_workflow_id[review_node_id]
            if (
                classification_entry.effective_type
                is not EntryClassificationType.TECHNICAL
                or not _row_semantics(
                    source_node,
                    EntryClassificationType.TECHNICAL,
                )[2]
            ):
                raise ValueError(
                    "only directly eligible technical rows may be atomized"
                )

    if (
        preview.project_id != lineage.project_id
        or preview.document_id != lineage.document_id
        or preview.session_id != lineage.session_id
        or preview.atomization_run_id != run.atomization_run_id
        or preview.atomization_revision != run.revision
    ):
        raise ValueError("publication preview does not match the exact run")
    preview_by_node_id = _validate_preview_entries(run=run, preview=preview)
    return families, preview_by_node_id


def _validated_atomization_family(
    review_node_id: str,
    family: tuple[RequirementAtomizationNode, ...],
    *,
    allow_rollup: bool,
) -> None:
    if not family:
        raise ValueError("atomization family is missing")
    rollups = tuple(
        item for item in family if item.kind is RequirementAtomizationNodeKind.ROLLUP
    )
    atoms = tuple(
        item
        for item in family
        if item.kind is RequirementAtomizationNodeKind.ATOMIC_OBLIGATION
    )
    if not atoms:
        raise ValueError("atomization family has no atomic obligation")
    if not rollups:
        if len(atoms) != 1 or atoms[0].parent_node_id is not None:
            raise ValueError("one-to-one atomization must contain one direct root")
        return
    if not allow_rollup:
        raise ValueError("pass-through atomization families cannot fan out")
    if len(rollups) != 1 or len(atoms) < 2:
        raise ValueError("multi-atom family requires one parent and at least two atoms")
    parent = rollups[0]
    if parent.review_node_id != review_node_id or parent.parent_node_id is not None:
        raise ValueError("multi-atom evidence parent binding is invalid")
    if any(atom.parent_node_id != parent.node_id for atom in atoms):
        raise ValueError("multi-atom children must bind the exact evidence parent")


def _validate_preview_entries(
    *,
    run: RequirementAtomizationRun,
    preview: RequirementPublicationPreview,
) -> dict[str, RequirementPublicationEntry]:
    if tuple(item.atomization_node_id for item in preview.entries) != tuple(
        item.node_id for item in run.nodes
    ):
        raise ValueError("publication preview does not exactly cover run node order")
    publication_id_by_node_id = {
        node.node_id: entry.publication_entry_id
        for node, entry in zip(run.nodes, preview.entries, strict=True)
    }
    bindings_by_atomic_id = {
        binding.atomic_node_id: binding for binding in preview.refinement_bindings
    }
    if len(bindings_by_atomic_id) != len(preview.refinement_bindings):
        raise ValueError("publication preview refinement atoms must be unique")
    if any(
        binding.status is not RequirementRefinementStatus.ACCEPTED
        for binding in preview.refinement_bindings
    ):
        raise ValueError("publication preview may use only accepted refinements")
    run_atomic_ids = {
        node.atomic_node_id for node in run.atomic_nodes if node.atomic_node_id
    }
    if not set(bindings_by_atomic_id).issubset(run_atomic_ids):
        raise ValueError("publication preview contains an extra refinement family")

    used_refinement_ids: set[str] = set()
    for node, entry in zip(run.nodes, preview.entries, strict=True):
        expected_kind = (
            RequirementPublicationEntryKind.HEADING
            if node.kind is RequirementAtomizationNodeKind.ROLLUP
            else RequirementPublicationEntryKind.REQUIREMENT
        )
        expected_parent = (
            publication_id_by_node_id[node.parent_node_id]
            if node.parent_node_id is not None
            else None
        )
        expected_entry_id = stable_publication_entry_id(
            publication_lineage_sha256=preview.lineage_sha256,
            atomization_node_id=node.node_id,
        )
        if (
            entry.publication_entry_id != expected_entry_id
            or entry.atomic_node_id != node.atomic_node_id
            or entry.parent_publication_entry_id != expected_parent
            or entry.review_node_id != node.review_node_id
            or entry.entry_kind is not expected_kind
            or entry.decision_mode is not node.decision_mode
            or entry.original_statement != node.source_text
            or entry.source_spans != node.source_spans
            or entry.source_order != node.source_order
            or entry.table_evidence_binding_id != node.table_evidence_binding_id
        ):
            raise ValueError(
                "publication preview row does not exactly match its run node"
            )
        binding = (
            bindings_by_atomic_id.get(node.atomic_node_id)
            if node.atomic_node_id is not None
            else None
        )
        if binding is None:
            if (
                entry.refinement_suggestion_id is not None
                or entry.refinement_revision is not None
            ):
                raise ValueError("publication preview refinement metadata is crossed")
            expected_statement = node.atomic_statement or node.source_text
            if entry.published_statement != expected_statement:
                raise ValueError(
                    "unrefined publication statement changed without proof"
                )
        else:
            if (
                entry.refinement_suggestion_id != binding.suggestion_id
                or entry.refinement_revision != binding.revision
            ):
                raise ValueError("publication preview refinement binding is crossed")
            used_refinement_ids.add(binding.suggestion_id)
    if used_refinement_ids != {
        binding.suggestion_id for binding in preview.refinement_bindings
    }:
        raise ValueError("publication preview contains an unused refinement binding")
    return {item.atomization_node_id: item for item in preview.entries}


def _row_semantics(
    node: RequirementTreeNodeV1,
    effective_type: EntryClassificationType,
) -> tuple[
    RequirementEnrichmentPublicationEntryKindV2,
    RequirementDecisionMode,
    bool,
    RequirementEnrichmentAnalysisDispositionV2,
    str,
]:
    role = node.projection_role
    if effective_type is EntryClassificationType.INFORMATION:
        return (
            RequirementEnrichmentPublicationEntryKindV2.REFERENCE,
            RequirementDecisionMode.NONE,
            False,
            RequirementEnrichmentAnalysisDispositionV2.RETAIN,
            role,
        )
    if effective_type is EntryClassificationType.TECHNICAL:
        return (
            RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT,
            RequirementDecisionMode.DIRECT,
            True,
            RequirementEnrichmentAnalysisDispositionV2.ANALYZE,
            "REQUIREMENT",
        )
    return (
        RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT,
        RequirementDecisionMode.DIRECT,
        True,
        RequirementEnrichmentAnalysisDispositionV2.RETAIN,
        "DIRECT_CONTENT",
    )


def _base_row(
    node: RequirementTreeNodeV1,
    effective_type: EntryClassificationType,
) -> RequirementEnrichmentPublicationRowV2:
    statement = node.summary.strip() or node.label.strip()
    if not statement:
        raise ValueError("accepted W3 business row has no publishable statement")
    entry_kind, decision_mode, eligible, disposition, final_role = _row_semantics(
        node,
        effective_type,
    )
    return RequirementEnrichmentPublicationRowV2(
        requirement_tree_node_id=node.id,
        source_working_tree_node_id=node.id,
        workflow_node_id=node.workflow_node_id or node.id,
        entry_kind=entry_kind,
        builder_kind=RequirementEnrichmentPublicationBuilderKindV2.REQUIREMENT,
        decision_mode=decision_mode,
        classification=effective_type.value,
        builder_binding_role=node.projection_role,
        final_projection_role=final_role,
        intrinsic_analysis_eligible=eligible,
        default_disposition=disposition,
        source_order=node.source_order,
        source_offset=node.source_offset,
        label=node.label or statement,
        original_statement=statement,
        published_statement=statement,
        structure_node_id=node.structure_node_id,
        page_start=node.page_start,
        page_end=node.page_end,
        source_refs=node.source_refs,
        status=node.status,
        table_record_id=node.table_record_id,
        table_evidence_binding_id=node.table_evidence_binding_id,
        deterministic_rule_evidence=node.deterministic_rule_evidence,
        include_in_view=node.include_in_view,
        content_blocks=node.content_blocks,
        editable_structure_node_ids=node.editable_structure_node_ids,
        table_refs=node.table_refs,
        figure_refs=node.figure_refs,
    )


def _updated_row(
    row: RequirementEnrichmentPublicationRowV2,
    **updates: Any,
) -> RequirementEnrichmentPublicationRowV2:
    fields = row.model_dump(mode="python")
    fields.update(updates)
    return RequirementEnrichmentPublicationRowV2.model_validate(fields)


def _one_to_one_atom_row(
    source: RequirementTreeNodeV1,
    effective_type: EntryClassificationType,
    atom: RequirementAtomizationNode,
    preview: RequirementPublicationEntry,
) -> RequirementEnrichmentPublicationRowV2:
    base = _base_row(source, effective_type)
    if (
        base.entry_kind is not RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
        or not base.intrinsic_analysis_eligible
    ):
        raise ValueError("one-to-one atomization requires an eligible requirement row")
    assert atom.atomic_node_id is not None
    return _updated_row(
        base,
        atomization_node_id=atom.node_id,
        atomic_requirement_id=atom.atomic_node_id,
        original_statement=preview.original_statement,
        published_statement=preview.published_statement,
        source_spans=preview.source_spans,
        source_order=preview.source_order,
        source_offset=min(span.start_offset for span in preview.source_spans),
        label=preview.published_statement,
        table_evidence_binding_id=preview.table_evidence_binding_id,
    )


def _multi_atom_parent_row(
    source: RequirementTreeNodeV1,
    parent: RequirementAtomizationNode,
    preview: RequirementPublicationEntry,
) -> RequirementEnrichmentPublicationRowV2:
    base = _base_row(source, EntryClassificationType.TECHNICAL)
    if not base.intrinsic_analysis_eligible:
        raise ValueError("multi-atom publication requires an eligible technical row")
    return _updated_row(
        base,
        atomization_node_id=parent.node_id,
        atomic_requirement_id=None,
        entry_kind=RequirementEnrichmentPublicationEntryKindV2.HEADING,
        builder_kind=(
            RequirementEnrichmentPublicationBuilderKindV2.REQUIREMENT_CONTAINER
        ),
        decision_mode=RequirementDecisionMode.ROLLUP,
        final_projection_role="REQUIREMENT",
        intrinsic_analysis_eligible=False,
        default_disposition=RequirementEnrichmentAnalysisDispositionV2.RETAIN,
        original_statement=preview.original_statement,
        published_statement=preview.published_statement,
        source_spans=preview.source_spans,
        source_order=preview.source_order,
        source_offset=min(span.start_offset for span in preview.source_spans),
        table_evidence_binding_id=None,
    )


def _atom_child_row(
    source: RequirementTreeNodeV1,
    atom: RequirementAtomizationNode,
    preview: RequirementPublicationEntry,
) -> RequirementEnrichmentPublicationRowV2:
    assert atom.atomic_node_id is not None
    structure_node_id = (
        atom.source_owner_structure_node_ids[0]
        if atom.source_owner_structure_node_ids
        else source.structure_node_id
    )
    return RequirementEnrichmentPublicationRowV2(
        requirement_tree_node_id=atom.node_id,
        source_working_tree_node_id=source.id,
        parent_requirement_tree_node_id=source.id,
        workflow_node_id=atom.review_node_id,
        atomization_node_id=atom.node_id,
        atomic_requirement_id=atom.atomic_node_id,
        entry_kind=RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT,
        builder_kind=RequirementEnrichmentPublicationBuilderKindV2.REQUIREMENT,
        decision_mode=RequirementDecisionMode.DIRECT,
        classification=EntryClassificationType.TECHNICAL.value,
        builder_binding_role="REQUIREMENT",
        final_projection_role="REQUIREMENT",
        intrinsic_analysis_eligible=True,
        default_disposition=RequirementEnrichmentAnalysisDispositionV2.ANALYZE,
        source_order=preview.source_order,
        source_offset=min(span.start_offset for span in preview.source_spans),
        label=preview.published_statement,
        original_statement=preview.original_statement,
        published_statement=preview.published_statement,
        structure_node_id=structure_node_id,
        page_start=source.page_start,
        page_end=source.page_end,
        source_spans=preview.source_spans,
        source_refs=_source_refs_from_spans(preview.source_spans),
        status=source.status,
        table_evidence_binding_id=preview.table_evidence_binding_id,
        include_in_view=True,
    )


def _source_refs_from_spans(
    source_spans: tuple[Any, ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "kind": "TEXT_RANGE",
            "source_id": span.semantic_unit_id,
            "owner_structure_node_id": span.owner_structure_node_id,
            "start_offset": span.start_offset,
            "end_offset": span.end_offset,
        }
        for span in source_spans
    )


def _validated_projection(
    projection: RequirementEnrichmentPublicationProjectionV2,
) -> RequirementEnrichmentPublicationProjectionV2:
    try:
        return RequirementEnrichmentPublicationProjectionV2.model_validate(
            projection.model_dump(mode="json")
        )
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            f"publication projection failed exact validation: {exc}"
        ) from exc


def _validated_requirement_entry_ids(
    projection: RequirementEnrichmentPublicationProjectionV2,
    requirement_entry_ids: Mapping[str, str],
) -> dict[str, str]:
    normalized = {
        str(key): str(value).strip() for key, value in requirement_entry_ids.items()
    }
    expected = {row.requirement_tree_node_id for row in projection.rows}
    if set(normalized) != expected:
        raise ValueError("database row identities must exactly cover the projection")
    if any(not value for value in normalized.values()) or len(normalized) != len(
        set(normalized.values())
    ):
        raise ValueError("database row identities must be non-blank and unique")
    return normalized


def _validated_materialized_entries(
    projection: RequirementEnrichmentPublicationProjectionV2,
    materialized_entries: tuple[Mapping[str, Any], ...],
) -> dict[str, str]:
    if len(materialized_entries) != len(projection.rows) or any(
        not isinstance(item, Mapping) for item in materialized_entries
    ):
        raise ValueError("materialized database rows must exactly cover the projection")
    final_ids = tuple(
        str(item.get("final_requirement_tree_node_id") or "")
        for item in materialized_entries
    )
    expected_final_ids = tuple(row.requirement_tree_node_id for row in projection.rows)
    if final_ids != expected_final_ids:
        raise ValueError(
            "materialized database row order does not exactly match the plan"
        )
    entry_ids = {
        final_id: str(item.get("requirement_entry_id") or "").strip()
        for final_id, item in zip(final_ids, materialized_entries, strict=True)
    }
    entry_ids = _validated_requirement_entry_ids(projection, entry_ids)
    expected_entries = build_requirement_enrichment_publication_builder_entries_v2(
        projection,
        requirement_entry_ids=entry_ids,
    )
    for row, actual, expected in zip(
        projection.rows,
        materialized_entries,
        expected_entries,
        strict=True,
    ):
        for key, value in expected.items():
            if key not in actual or actual[key] != value:
                raise ValueError(
                    f"materialized database row metadata is not exact: {key}"
                )
        if (
            row.decision_mode is RequirementDecisionMode.NONE
            and "decision_mode" in actual
        ):
            raise ValueError("reference database rows cannot claim a decision mode")
    return entry_ids


def _builder_stable_id(row: RequirementEnrichmentPublicationRowV2) -> str:
    if row.requirement_tree_node_id != row.source_working_tree_node_id:
        assert row.atomization_node_id is not None
        return row.atomization_node_id
    if row.builder_binding_role != "REQUIREMENT":
        return row.requirement_tree_node_id
    prefix = "requirement:"
    return (
        row.requirement_tree_node_id[len(prefix) :]
        if row.requirement_tree_node_id.startswith(prefix)
        else row.requirement_tree_node_id
    )


def _expected_builder_tree_node_id(
    row: RequirementEnrichmentPublicationRowV2,
) -> str:
    stable_id = _builder_stable_id(row)
    if row.builder_binding_role in _STABLE_BUILDER_BINDING_ROLES:
        return stable_id
    return f"requirement:{stable_id}"


def _validate_built_row(
    *,
    row: RequirementEnrichmentPublicationRowV2,
    built_node: RequirementTreeNodeV1,
    requirement_entry_id: str,
    evidence_binding: Any,
    table_binding_ids: set[str],
) -> None:
    if (
        built_node.requirement_entry_id != requirement_entry_id
        or built_node.workflow_node_id != row.workflow_node_id
        or built_node.projection_role != row.builder_binding_role
        or built_node.table_record_id != row.table_record_id
        or built_node.table_evidence_binding_id != row.table_evidence_binding_id
    ):
        raise ValueError("builder tree row metadata does not exactly match the plan")
    if (
        row.builder_binding_role == "DIRECT_CONTENT"
        and built_node.editable_structure_node_ids != row.editable_structure_node_ids
    ):
        raise ValueError("builder direct-content edit scope does not match the plan")
    if (
        row.table_evidence_binding_id is not None
        and row.table_evidence_binding_id not in table_binding_ids
    ):
        raise ValueError("publication row references missing table evidence")
    if (
        built_node.requirement_evidence_binding_id != evidence_binding.binding_id
        or evidence_binding.requirement_entry_id != requirement_entry_id
        or evidence_binding.atomic_requirement_id != row.atomic_requirement_id
        or evidence_binding.source_spans != row.source_spans
        or evidence_binding.source_refs != _expected_evidence_source_refs(row)
        or evidence_binding.table_evidence_binding_id != row.table_evidence_binding_id
    ):
        raise ValueError("builder evidence row does not exactly match the plan")


def _expected_evidence_source_refs(
    row: RequirementEnrichmentPublicationRowV2,
) -> tuple[RequirementEvidenceSourceRefV1, ...]:
    candidates: list[dict[str, Any]] = [dict(item) for item in row.source_refs]
    for evidence in row.deterministic_rule_evidence:
        candidates.extend(
            item for item in evidence.get("source_refs", ()) if isinstance(item, dict)
        )
    if not candidates:
        candidates.extend(
            block
            for block in row.content_blocks
            if isinstance(block, dict)
            and (
                block.get("source_id")
                or block.get("semantic_unit_id")
                or block.get("cell_id")
                or block.get("visual_observation_id")
            )
        )
    refs: list[RequirementEvidenceSourceRefV1] = []
    seen: set[str] = set()
    for item in candidates:
        source_id = str(
            item.get("source_id")
            or item.get("semantic_unit_id")
            or item.get("cell_id")
            or item.get("visual_observation_id")
            or ""
        ).strip()
        if not source_id:
            continue
        start_offset = _optional_nonnegative_int(item.get("start_offset"))
        end_offset = _optional_nonnegative_int(item.get("end_offset"))
        if (start_offset is None) != (end_offset is None):
            start_offset = None
            end_offset = None
        page_start = _positive_int(
            item.get("page_start") or item.get("page_number") or row.page_start
        )
        page_end = _positive_int(
            item.get("page_end")
            or item.get("page_number")
            or row.page_end
            or page_start
        )
        if page_start is None:
            page_end = None
        ref = RequirementEvidenceSourceRefV1(
            kind=str(item.get("kind") or "SOURCE_REF").strip(),
            source_id=source_id,
            owner_structure_node_id=str(
                item.get("owner_structure_node_id") or row.structure_node_id or ""
            )
            or None,
            start_offset=start_offset,
            end_offset=end_offset,
            page_start=page_start,
            page_end=page_end,
        )
        key = json.dumps(
            ref.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if key not in seen:
            seen.add(key)
            refs.append(ref)
    return tuple(refs)


def _optional_nonnegative_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _publication_topology(
    *,
    working: RequirementTreeSnapshotV1,
    plan: RequirementEnrichmentPublicationProjectionV2,
) -> tuple[dict[str, str | None], dict[str, tuple[str, ...]]]:
    """Build final parentage while retaining every accepted W3 node.

    Classification can turn a coarse structural/table projection into one
    directly analyzable row.  Matrix requires such rows to be leaves.  Their
    former children therefore move one level up, immediately after the coarse
    row, rather than being deleted or silently making the row ineligible.
    """

    parent_by_id = {node.id: node.parent_id for node in working.nodes}
    children_by_id = {node.id: list(node.child_ids) for node in working.nodes}
    for row in plan.rows:
        if row.requirement_tree_node_id == row.source_working_tree_node_id:
            continue
        parent_id = row.parent_requirement_tree_node_id
        if parent_id is None or parent_id not in children_by_id:
            raise ValueError("atom publication row is missing its accepted W3 parent")
        if row.requirement_tree_node_id in parent_by_id:
            raise ValueError("atom publication row collides with the W3 skeleton")
        parent_by_id[row.requirement_tree_node_id] = parent_id
        children_by_id[row.requirement_tree_node_id] = []
        children_by_id[parent_id].append(row.requirement_tree_node_id)

    for row in plan.rows:
        row_id = row.requirement_tree_node_id
        if (
            row.entry_kind
            is not RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
            or not row.intrinsic_analysis_eligible
            or not children_by_id[row_id]
        ):
            continue
        parent_id = parent_by_id[row_id]
        if parent_id is None:
            raise ValueError("an analyzable publication row cannot be a tree root")
        siblings = children_by_id[parent_id]
        try:
            row_index = siblings.index(row_id)
        except ValueError as exc:
            raise ValueError(
                "publication parent is missing its analyzable child"
            ) from exc
        moved_children = children_by_id[row_id]
        siblings[row_index + 1 : row_index + 1] = moved_children
        children_by_id[row_id] = []
        for child_id in moved_children:
            parent_by_id[child_id] = parent_id

    return parent_by_id, {
        node_id: tuple(child_ids) for node_id, child_ids in children_by_id.items()
    }


def _final_tree_row_updates(
    *,
    row: RequirementEnrichmentPublicationRowV2,
    built_node: RequirementTreeNodeV1,
    final_id: str,
    parent_id: str | None,
    child_ids: tuple[str, ...],
) -> dict[str, Any]:
    if row.entry_kind is RequirementEnrichmentPublicationEntryKindV2.HEADING:
        final_kind = "REQUIREMENT_CONTAINER"
    elif row.final_projection_role == "REQUIREMENT":
        final_kind = "REQUIREMENT"
    elif row.final_projection_role == "DIRECT_CONTENT":
        final_kind = "DIRECT_CONTENT"
    else:
        final_kind = built_node.kind
    editable_structure_node_ids = (
        row.editable_structure_node_ids
        if row.final_projection_role == "DIRECT_CONTENT"
        else ()
    )
    if final_kind == "DIRECT_CONTENT" and parent_id is None:
        raise ValueError("direct-content publication rows require a tree owner")
    semantic_state = (
        "CONFIRMED"
        if row.status == "REVIEWED"
        else "PROPOSED"
        if row.entry_kind is RequirementEnrichmentPublicationEntryKindV2.REQUIREMENT
        else built_node.semantic_state
    )
    return {
        "id": final_id,
        "parent_id": parent_id,
        "child_ids": child_ids,
        "kind": final_kind,
        "source_order": row.source_order,
        "source_offset": row.source_offset,
        "label": row.label,
        "summary": row.published_statement,
        "structure_node_id": (
            parent_id if final_kind == "DIRECT_CONTENT" else row.structure_node_id
        ),
        "workflow_node_id": row.workflow_node_id,
        "page_start": row.page_start,
        "page_end": row.page_end,
        "source_refs": row.source_refs,
        "status": row.status,
        "classification": row.classification,
        "projection_role": row.final_projection_role,
        "semantic_state": semantic_state,
        "deterministic_rule_evidence": row.deterministic_rule_evidence,
        "table_record_id": (
            row.table_record_id if final_kind == "TABLE_RECORD" else None
        ),
        "table_evidence_binding_id": row.table_evidence_binding_id,
        "include_in_view": row.include_in_view,
        "content_blocks": row.content_blocks,
        "editable_structure_node_ids": editable_structure_node_ids,
        "table_refs": row.table_refs,
        "figure_refs": row.figure_refs,
    }


def _validate_final_tree_rows(
    *,
    result: RequirementTreeSnapshotV1,
    plan: RequirementEnrichmentPublicationProjectionV2,
    entry_ids: Mapping[str, str],
) -> None:
    node_by_id = {node.id: node for node in result.nodes}
    for row in plan.rows:
        node = node_by_id.get(row.requirement_tree_node_id)
        if node is None:
            raise ValueError("final tree is missing an exact logical row")
        expected_parent = row.parent_requirement_tree_node_id
        if expected_parent is None:
            expected_parent = node.parent_id
        expected_kind = (
            "REQUIREMENT_CONTAINER"
            if row.entry_kind is RequirementEnrichmentPublicationEntryKindV2.HEADING
            else "REQUIREMENT"
            if row.final_projection_role == "REQUIREMENT"
            else "DIRECT_CONTENT"
            if row.final_projection_role == "DIRECT_CONTENT"
            else node.kind
        )
        if (
            node.requirement_entry_id != entry_ids[row.requirement_tree_node_id]
            or node.parent_id != expected_parent
            or node.kind != expected_kind
            or node.workflow_node_id != row.workflow_node_id
            or node.classification != row.classification
            or node.projection_role != row.final_projection_role
            or node.table_record_id
            != (
                row.table_record_id
                if row.final_projection_role == "TABLE_RECORD"
                else None
            )
            or node.table_evidence_binding_id != row.table_evidence_binding_id
        ):
            raise ValueError("final tree row metadata does not exactly match the plan")
        if row.intrinsic_analysis_eligible and node.child_ids:
            raise ValueError(
                "an intrinsically analyzable publication row must be a leaf"
            )
        if row.final_projection_role == "DIRECT_CONTENT" and (
            node.parent_id != node.structure_node_id
        ):
            raise ValueError("final direct-content row is not bound to its tree owner")


def _working_self_counts(
    tree: RequirementTreeSnapshotV1,
) -> dict[str, tuple[int, int, int, int]]:
    node_by_id = {node.id: node for node in tree.nodes}
    result: dict[str, tuple[int, int, int, int]] = {}
    for node in tree.nodes:
        aggregate = (
            node.requirement_count,
            node.attention_count,
            node.reviewed_count,
            node.review_item_count,
        )
        child_totals = tuple(
            sum(
                (getattr(node_by_id[child_id], field) for child_id in node.child_ids),
                0,
            )
            for field in (
                "requirement_count",
                "attention_count",
                "reviewed_count",
                "review_item_count",
            )
        )
        own = tuple(
            total - child_total
            for total, child_total in zip(aggregate, child_totals, strict=True)
        )
        if any(value < 0 for value in own):
            raise ValueError("accepted W3 aggregate counts are internally inconsistent")
        result[node.id] = own
    return result


def _validated_depths(
    *,
    root_ids: tuple[str, ...],
    parent_by_id: Mapping[str, str | None],
    child_ids_by_id: Mapping[str, tuple[str, ...]],
) -> dict[str, int]:
    if set(root_ids) != {
        node_id for node_id, parent_id in parent_by_id.items() if parent_id is None
    }:
        raise ValueError("publication overlay roots do not match parentless nodes")
    depths: dict[str, int] = {}
    pending = [(root_id, 0) for root_id in reversed(root_ids)]
    while pending:
        node_id, depth = pending.pop()
        if node_id in depths:
            raise ValueError("publication overlay hierarchy contains a cycle")
        if node_id not in parent_by_id:
            raise ValueError("publication overlay references a missing child")
        depths[node_id] = depth
        for child_id in reversed(child_ids_by_id[node_id]):
            if parent_by_id.get(child_id) != node_id:
                raise ValueError("publication overlay parent and child disagree")
            pending.append((child_id, depth + 1))
    if set(depths) != set(parent_by_id):
        raise ValueError("publication overlay contains disconnected nodes")
    return depths


def _aggregate_counts(
    *,
    root_ids: tuple[str, ...],
    child_ids_by_id: Mapping[str, tuple[str, ...]],
    self_counts: Mapping[str, tuple[int, int, int, int]],
) -> dict[str, tuple[int, int, int, int]]:
    result: dict[str, tuple[int, int, int, int]] = {}

    def visit(node_id: str, active: set[str]) -> tuple[int, int, int, int]:
        if node_id in result:
            return result[node_id]
        if node_id in active:
            raise ValueError("publication overlay hierarchy contains a cycle")
        if node_id not in self_counts:
            raise ValueError("publication overlay node has no self-count basis")
        active.add(node_id)
        counts = list(self_counts[node_id])
        for child_id in child_ids_by_id[node_id]:
            child = visit(child_id, active)
            counts = [
                value + child_value
                for value, child_value in zip(counts, child, strict=True)
            ]
        active.remove(node_id)
        result[node_id] = tuple(counts)  # type: ignore[assignment]
        return result[node_id]

    for root_id in root_ids:
        visit(root_id, set())
    return result


def _preorder_ids(
    root_ids: tuple[str, ...],
    child_ids_by_id: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    ordered: list[str] = []
    pending = list(reversed(root_ids))
    while pending:
        node_id = pending.pop()
        ordered.append(node_id)
        pending.extend(reversed(child_ids_by_id[node_id]))
    return tuple(ordered)


__all__ = [
    "build_requirement_enrichment_publication_builder_entries_v2",
    "build_requirement_enrichment_publication_projection_v2",
    "overlay_requirement_enrichment_publication_tree_v2",
    "validate_requirement_enrichment_publication_tree_v2",
]
