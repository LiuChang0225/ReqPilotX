from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
import json
from typing import Any, Mapping, Sequence

from reqpilotx_server.application.agent_workflow.document_map_exploration import (
    DocumentMapSurveyCompilationV1,
    DocumentMapSurveyRangeKind,
)
from reqpilotx_server.application.agent_workflow.document_map_replacement import (
    DocumentMapCompiledLogicalTableV1,
    DocumentMapCompiledTableFragmentV1,
    DocumentMapContentDisposition,
    DocumentMapScopeEvidenceIndex,
    DocumentMapScopeEvidenceRecord,
    DocumentMapScopeReplacementCompilationV1,
)
from reqpilotx_server.application.agent_workflow.native_structure_working_revision import (
    NativeStructureTablePreviewBindingV1,
    NativeStructureWorkingRevisionV1,
    TABLE_PREVIEW_MEDIA_TYPE,
    build_d2_host_compiler_receipt,
    canonical_json_sha256,
    compile_native_structure_working_revision,
    validate_native_structure_table_preview,
)
from reqpilotx_server.application.agent_workflow.native_structure_working_revision_bridge import (
    publish_native_structure_working_revision,
)
from reqpilotx_server.ports.agent_structure_loop_repository import AgentStructureLoopRepository
from reqpilotx_server.ports.object_store import ObjectStore


@dataclass(frozen=True, slots=True)
class ScopeReplacementProjection:
    compilation: DocumentMapScopeReplacementCompilationV1
    nodes: tuple[dict[str, Any], ...]
    logical_tables: tuple[dict[str, Any], ...]
    table_previews: tuple[dict[str, Any], ...]
    unresolved: tuple[dict[str, Any], ...]


def restore_document_map_scope_projections(
    *,
    revision: NativeStructureWorkingRevisionV1,
    compilations: Sequence[DocumentMapScopeReplacementCompilationV1],
    survey: DocumentMapSurveyCompilationV1,
    kit_bundle_sha256: str,
    object_store: ObjectStore,
) -> tuple[ScopeReplacementProjection, ...]:
    """Resume accepted scopes from their verified product projection, without an Agent."""
    revision = NativeStructureWorkingRevisionV1.model_validate(
        revision.model_dump(mode="json")
    )
    compilations = tuple(
        DocumentMapScopeReplacementCompilationV1.model_validate(
            item.model_dump(mode="json")
        )
        for item in compilations
    )
    if not compilations or any(
        item.source_sha256 != revision.source_sha256
        or item.source_ir_canonical_sha256 != compilations[0].source_ir_canonical_sha256
        or item.survey_compilation_sha256 != survey.compilation_sha256
        for item in compilations
    ):
        raise ValueError("restored scope source lineage drifted")
    receipt = build_d2_host_compiler_receipt(
        run_id=revision.run_id,
        source_sha256=revision.source_sha256,
        source_ir_canonical_sha256=compilations[0].source_ir_canonical_sha256,
        kit_bundle_sha256=kit_bundle_sha256,
        survey_compilation_sha256=survey.compilation_sha256,
        scope_compilation_sha256s=[item.compilation_sha256 for item in compilations],
        compiled_node_count=len(revision.document_tree["nodes"]),
        compiled_table_count=len(revision.logical_tables),
    )
    if (
        receipt.receipt_sha256 != revision.d2_compiler_receipt_sha256
        or revision.run_id != survey.run_id
    ):
        raise ValueError("restored scope compiler receipt drifted")
    heading_ids = [
        heading.stable_node_id
        for item in compilations
        for heading in item.heading_nodes
    ]
    nodes_by_id = {node["node_id"]: node for node in revision.document_tree["nodes"]}
    if len(heading_ids) != len(set(heading_ids)) or set(heading_ids) != {
        node_id
        for node_id, node in nodes_by_id.items()
        if node_id not in revision.document_tree["root_ids"]
    }:
        raise ValueError("restored scopes do not cover the published tree")
    previews: dict[str, dict[str, Any]] = {}
    tables_by_id = {table["scope_id"]: table for table in revision.logical_tables}
    for binding in revision.table_preview_bindings:
        with object_store.open(binding.object_key) as stream:
            payload = stream.read()
        if (
            len(payload) != binding.object_size
            or hashlib.sha256(payload).hexdigest() != binding.object_sha256
        ):
            raise ValueError("restored scope table bytes drifted")
        preview = json.loads(payload)
        validate_native_structure_table_preview(
            logical_table=tables_by_id[binding.scope_id],
            preview=preview,
            source_sha256=revision.source_sha256,
            artifact_id=revision.base_artifact_id,
        )
        previews[binding.scope_id] = preview
    restored = []
    for compilation in compilations:
        scope_ids = {heading.stable_node_id for heading in compilation.heading_nodes}
        nodes = []
        non_business_ids = {
            item.source_node_id
            for item in compilation.text_ownership
            if item.disposition is DocumentMapContentDisposition.NON_BUSINESS
        }
        for heading in compilation.heading_nodes:
            node = _copy_json(nodes_by_id[heading.stable_node_id])
            node["source_order"] = heading.source_order
            node["source_content"] = [
                content
                for content in node.get("source_content", [])
                if not content.get("source_handles")
                or not set(content["source_handles"]).issubset(non_business_ids)
            ]
            nodes.append(node)
        tables = [
            table
            for table in revision.logical_tables
            if table.get("owner_node_id") in scope_ids
        ]
        restored.append(
            ScopeReplacementProjection(
                compilation=compilation,
                nodes=tuple(nodes),
                logical_tables=tuple(_copy_json(table) for table in tables),
                table_previews=tuple(
                    _copy_json(previews[table["scope_id"]]) for table in tables
                ),
                unresolved=tuple(
                    _copy_json(item)
                    for item in revision.unresolved
                    if item.get("scope_id") == compilation.scope_id
                ),
            )
        )
    return tuple(restored)


def project_document_map_scope_replacement(
    *,
    compilation: DocumentMapScopeReplacementCompilationV1,
    evidence: DocumentMapScopeEvidenceIndex,
) -> ScopeReplacementProjection:
    """Project one Agent-owned replacement without inventing source evidence."""

    compilation = DocumentMapScopeReplacementCompilationV1.model_validate(
        compilation.model_dump(mode="json")
    )
    if (
        compilation.source_sha256 != evidence.source_sha256
        or compilation.source_ir_canonical_sha256 != evidence.source_ir_canonical_sha256
        or compilation.scope_id != evidence.scope_id
    ):
        raise ValueError("scope projection source evidence identity drifted")
    for table in compilation.logical_tables:
        for fragment in table.fragments:
            source_table = evidence.tables_by_id.get(fragment.physical_table_id)
            if source_table is None or (
                canonical_json_sha256(source_table) != fragment.table_sha256
                or source_table["row_count"] != fragment.row_count
                or source_table["column_count"] != fragment.column_count
            ):
                raise ValueError("scope projection table source drifted")
    records_by_id = {item.source_node_id: item for item in evidence.records}
    represented = (
        {item.source_node_id for item in compilation.heading_nodes}
        | {item.source_node_id for item in compilation.text_ownership}
        | {item.placement_source_node_id for item in compilation.asset_ownership}
        | {
            item.source_node_id
            for item in evidence.records
            if item.table_id is not None
        }
    )
    if represented != set(records_by_id):
        raise ValueError("scope projection source ownership coverage drifted")
    row_coverage = {
        table_id: [0] * int(table["row_count"])
        for table_id, table in evidence.tables_by_id.items()
    }
    fragments = [
        fragment for table in compilation.logical_tables for fragment in table.fragments
    ]
    fragments.extend(item.fragment for item in compilation.table_remainders)
    for fragment in fragments:
        if (
            fragment.physical_table_id not in row_coverage
            or not 0
            <= fragment.start_row
            < fragment.end_row
            <= len(row_coverage[fragment.physical_table_id])
        ):
            raise ValueError("scope projection table range drifted")
        for row in range(fragment.start_row, fragment.end_row):
            row_coverage[fragment.physical_table_id][row] += 1
    if any(count != 1 for counts in row_coverage.values() for count in counts):
        raise ValueError("scope projection table ownership coverage drifted")
    headings = tuple(
        sorted(compilation.heading_nodes, key=lambda item: item.source_order)
    )
    heading_ids = {item.stable_node_id for item in headings}
    heading_by_source = {item.source_node_id: item for item in headings}
    node_payloads: dict[str, dict[str, Any]] = {}
    direct_pages: dict[str, set[int]] = {}
    table_text_aliases: dict[str, list[dict[str, Any]]] = {}

    for heading in headings:
        record = records_by_id.get(heading.source_node_id)
        if record is None:
            raise ValueError("replacement heading source evidence is missing")
        direct_pages[heading.stable_node_id] = {record.page_number}
        node_payloads[heading.stable_node_id] = {
            "node_id": heading.stable_node_id,
            "parent_id": heading.parent_stable_node_id,
            "order": 0,
            "source_order": heading.source_order,
            "kind": "CHAPTER" if heading.level == 1 else "SECTION",
            "label": heading.number or "",
            "title": heading.title,
            "title_authority": "SOURCE_VISIBLE",
            "pages": [record.page_number],
            "content_blocks": [],
            "source_anchors": [_title_anchor(record)],
            "source_content": [_source_content(record, is_title=True)],
            "asset_placements": [],
            "content_sequence": [
                {
                    "source_order": record.source_order,
                    "kind": "TITLE",
                    "source_node_id": record.source_node_id,
                }
            ],
            "non_business_source_refs": [],
            "finding_codes": [],
        }

    for ownership in compilation.text_ownership:
        if ownership.display_heading_node_id not in heading_ids:
            raise ValueError("replacement text owner is missing")
        record = records_by_id.get(ownership.source_node_id)
        if record is None or record.source_node_id in heading_by_source:
            raise ValueError("replacement text source evidence is invalid")
        if ownership.table_evidence_scope_id is not None:
            table_text_aliases.setdefault(ownership.table_evidence_scope_id, []).append(
                {
                    "source_node_id": record.source_node_id,
                    "source_order": record.source_order,
                    "page": record.page_number,
                    "bbox": list(record.bbox),
                    "source_text_sha256": record.source_text_sha256,
                }
            )
            continue
        target = node_payloads[ownership.display_heading_node_id]
        if ownership.disposition is DocumentMapContentDisposition.NON_BUSINESS:
            target["non_business_source_refs"].append(
                {
                    "source_node_id": record.source_node_id,
                    "source_order": record.source_order,
                    "source_text_sha256": record.source_text_sha256,
                }
            )
            continue
        target["source_content"].append(_source_content(record, is_title=False))
        target["content_sequence"].append(
            {
                "source_order": record.source_order,
                "kind": "TEXT",
                "source_node_id": record.source_node_id,
                "disposition": ownership.disposition.value,
            }
        )
        direct_pages[ownership.display_heading_node_id].add(record.page_number)

    logical_tables: list[dict[str, Any]] = []
    previews: list[dict[str, Any]] = []
    for table in compilation.logical_tables:
        logical, preview, source_order = _project_logical_table(
            table,
            evidence=evidence,
            source_sha256=compilation.source_sha256,
        )
        logical["source_text_aliases"] = table_text_aliases.get(
            table.logical_table_scope_id,
            [],
        )
        preview["source_text_aliases"] = logical["source_text_aliases"]
        owner = node_payloads.get(table.owner_heading_node_id)
        if owner is None:
            raise ValueError("replacement logical table owner is missing")
        owner["content_blocks"].append(
            {
                "kind": "LOGICAL_TABLE",
                "logical_table_scope_id": table.logical_table_scope_id,
                "source_order": source_order,
            }
        )
        owner["content_sequence"].append(
            {
                "source_order": source_order,
                "kind": "LOGICAL_TABLE",
                "logical_table_scope_id": table.logical_table_scope_id,
            }
        )
        for fragment in table.fragments:
            record = _table_record(fragment.physical_table_id, evidence=evidence)
            owner["source_anchors"].append(
                _table_anchor(
                    record,
                    table_scope_id=table.logical_table_scope_id,
                    fragment=fragment,
                )
            )
            direct_pages[table.owner_heading_node_id].add(fragment.page_number)
        logical_tables.append(logical)
        previews.append(preview)

    unresolved: list[dict[str, Any]] = []
    for index, remainder in enumerate(compilation.table_remainders, start=1):
        title = (
            "待判断的原文表格"
            if remainder.disposition is DocumentMapContentDisposition.UNRESOLVED
            else "版面表格内容"
        )
        synthetic_table = DocumentMapCompiledLogicalTableV1(
            logical_table_scope_id=(
                "document-map-table-"
                + canonical_json_sha256(
                    {
                        "source_sha256": compilation.source_sha256,
                        "remainder": remainder.model_dump(mode="json"),
                    }
                )[:32]
            ),
            logical_table_key=f"remainder-{index}",
            owner_heading_node_id=remainder.display_heading_node_id,
            title=title,
            title_authority="AGENT_LABEL",
            fragments=(remainder.fragment,),
            public_summary=remainder.public_summary,
        )
        logical, preview, source_order = _project_logical_table(
            synthetic_table,
            evidence=evidence,
            source_sha256=compilation.source_sha256,
            role=(
                "UNRESOLVED_SOURCE_TABLE"
                if remainder.disposition is DocumentMapContentDisposition.UNRESOLVED
                else "NON_BUSINESS_SOURCE_TABLE"
            ),
        )
        owner = node_payloads.get(remainder.display_heading_node_id)
        if owner is None:
            raise ValueError("replacement table remainder owner is missing")
        owner["content_blocks"].append(
            {
                "kind": "LOGICAL_TABLE",
                "logical_table_scope_id": synthetic_table.logical_table_scope_id,
                "source_order": source_order,
            }
        )
        owner["content_sequence"].append(
            {
                "source_order": source_order,
                "kind": "LOGICAL_TABLE",
                "logical_table_scope_id": synthetic_table.logical_table_scope_id,
                "disposition": remainder.disposition.value,
            }
        )
        record = _table_record(
            remainder.fragment.physical_table_id,
            evidence=evidence,
        )
        owner["source_anchors"].append(
            _table_anchor(
                record,
                table_scope_id=synthetic_table.logical_table_scope_id,
                fragment=remainder.fragment,
            )
        )
        direct_pages[remainder.display_heading_node_id].add(
            remainder.fragment.page_number
        )
        logical_tables.append(logical)
        previews.append(preview)
        if remainder.disposition is DocumentMapContentDisposition.UNRESOLVED:
            unresolved.append(
                {
                    "code": f"scope-{compilation.scope_id[-12:]}-table-unresolved-{index}",
                    "scope_id": compilation.scope_id,
                    "pages": [remainder.fragment.page_number],
                    "public_summary": remainder.public_summary,
                }
            )

    for index, asset in enumerate(compilation.asset_ownership, start=1):
        record = records_by_id.get(asset.placement_source_node_id)
        owner = node_payloads.get(asset.display_heading_node_id)
        if record is None or owner is None or record.asset_id != asset.asset_id:
            raise ValueError("replacement asset projection evidence is invalid")
        owner["source_anchors"].append(_asset_anchor(record, role=asset.role.value))
        owner["asset_placements"].append(
            {
                "placement_source_node_id": record.source_node_id,
                "asset_id": asset.asset_id,
                "role": asset.role.value,
                "disposition": asset.disposition.value,
                "public_summary": asset.public_summary,
                "source_order": record.source_order,
            }
        )
        owner["content_sequence"].append(
            {
                "source_order": record.source_order,
                "kind": "ASSET",
                "placement_source_node_id": record.source_node_id,
                "asset_id": asset.asset_id,
                "disposition": asset.disposition.value,
            }
        )
        direct_pages[asset.display_heading_node_id].add(record.page_number)
        if asset.disposition is DocumentMapContentDisposition.UNRESOLVED:
            unresolved.append(
                {
                    "code": f"scope-{compilation.scope_id[-12:]}-asset-unresolved-{index}",
                    "scope_id": compilation.scope_id,
                    "pages": [record.page_number],
                    "public_summary": asset.public_summary,
                }
            )

    for index, ownership in enumerate(compilation.text_ownership, start=1):
        if ownership.disposition is not DocumentMapContentDisposition.UNRESOLVED:
            continue
        record = records_by_id[ownership.source_node_id]
        unresolved.append(
            {
                "code": f"scope-{compilation.scope_id[-12:]}-text-unresolved-{index}",
                "scope_id": compilation.scope_id,
                "pages": [record.page_number],
                "public_summary": "原文内容归属仍保留工作判断，可后续修订。",
            }
        )

    nodes = list(node_payloads.values())
    _propagate_pages(nodes, direct_pages=direct_pages)
    for node in nodes:
        node["source_content"].sort(key=lambda item: item["source_order"])
        node["source_anchors"].sort(
            key=lambda item: (item["page"], item.get("source_order", -1))
        )
        node["asset_placements"].sort(key=lambda item: item["source_order"])
        node["content_sequence"].sort(
            key=lambda item: (item["source_order"], item["kind"])
        )
        node["content_blocks"].sort(key=lambda item: item["source_order"])
    return ScopeReplacementProjection(
        compilation=compilation,
        nodes=tuple(sorted(nodes, key=lambda item: item["source_order"])),
        logical_tables=tuple(logical_tables),
        table_previews=tuple(previews),
        unresolved=tuple(unresolved),
    )


def assemble_and_publish_document_map_working_revision(
    *,
    project_id: str,
    document_id: str,
    run_id: str,
    source_sha256: str,
    source_ir_canonical_sha256: str,
    evidence_workspace_bundle_sha256: str,
    survey: DocumentMapSurveyCompilationV1,
    scope_replacements: Sequence[ScopeReplacementProjection],
    repository: AgentStructureLoopRepository,
    object_store: ObjectStore,
    kit_bundle_sha256: str,
    revision: int = 1,
    previous: NativeStructureWorkingRevisionV1 | None = None,
    document_context: Mapping[str, Sequence[DocumentMapScopeEvidenceRecord]] | None = None,
    unresolved_scopes: Sequence[Mapping[str, Any]] = (),
    complete_source_node_ids: Sequence[str] | None = None,
    complete_physical_table_ids: Sequence[str] | None = None,
) -> NativeStructureWorkingRevisionV1:
    """Publish verified partial scopes into one honest working Document Map."""

    if (
        survey.source_sha256 != source_sha256
        or survey.run_id != run_id
        or not scope_replacements
        or any(
            item.compilation.source_sha256 != source_sha256
            or item.compilation.survey_compilation_sha256 != survey.compilation_sha256
            or item.compilation.source_ir_canonical_sha256 != source_ir_canonical_sha256
            for item in scope_replacements
        )
    ):
        raise ValueError("working Document Map source lineage drifted")
    root_nodes, page_ownership, root_by_survey_scope = _survey_roots(survey)
    all_nodes = [*root_nodes]
    all_tables: list[dict[str, Any]] = []
    all_previews: list[dict[str, Any]] = []
    all_unresolved: list[dict[str, Any]] = []
    compilation_hashes: list[str] = []
    visual_page_roots: dict[int, str] = {}
    all_unresolved.extend(_copy_json(item) for item in unresolved_scopes)
    represented_source_ids: list[str] = []
    represented_table_rows: list[tuple[str, int]] = []
    for scope_id, records in (document_context or {}).items():
        if scope_id not in root_by_survey_scope:
            raise ValueError("document context leaves its surveyed source unit")
        root = next(item for item in root_nodes if item["node_id"] == root_by_survey_scope[scope_id])
        for record in records:
            if record.table_id is not None or record.asset_id is not None:
                raise ValueError("document context must not flatten tables or images")
            represented_source_ids.append(record.source_node_id)
            root["source_content"].append(_source_content(record, is_title=False))
            root["source_anchors"].append(_title_anchor(record))
            root["content_sequence"].append({"source_order": record.source_order, "kind": "TEXT", "source_node_id": record.source_node_id})

    for replacement in sorted(
        scope_replacements,
        key=lambda item: item.compilation.heading_nodes[0].source_order,
    ):
        compilation = replacement.compilation
        fragments = [fragment for table in compilation.logical_tables for fragment in table.fragments]
        fragments.extend(item.fragment for item in compilation.table_remainders)
        represented_table_rows.extend((fragment.physical_table_id, row) for fragment in fragments for row in range(fragment.start_row, fragment.end_row))
        represented_source_ids.extend(item.source_node_id for item in compilation.heading_nodes)
        represented_source_ids.extend(item.source_node_id for item in compilation.text_ownership)
        represented_source_ids.extend(item.placement_source_node_id for item in compilation.asset_ownership)
        survey_scope = next(
            (
                item
                for item in survey.scopes
                if compilation.heading_nodes[0].page_number in item.page_numbers
            ),
            None,
        )
        if survey_scope is None:
            raise ValueError("replacement heading starts outside surveyed pages")
        root_id = root_by_survey_scope[survey_scope.scope_id]
        # Survey page ranges are navigation hypotheses, not exact source
        # boundaries. A chapter may continue onto a shared transition page or
        # open neighboring pages for inspection without changing its parent.
        root = next(item for item in root_nodes if item["node_id"] == root_id)
        root["pages"] = sorted(
            set(root["pages"]).union(
                page for node in replacement.nodes for page in node["pages"]
            )
        )
        for node in replacement.nodes:
            copied = _copy_json(node)
            if copied["parent_id"] is None:
                copied["parent_id"] = root_id
            all_nodes.append(copied)
        for table in replacement.logical_tables:
            copied = _copy_json(table)
            copied["owner_unit_id"] = root_id
            all_tables.append(copied)
        all_previews.extend(_copy_json(item) for item in replacement.table_previews)
        all_unresolved.extend(_copy_json(item) for item in replacement.unresolved)
        compilation_hashes.append(compilation.compilation_sha256)
        for page in compilation.opened_high_resolution_page_numbers:
            existing = visual_page_roots.get(page)
            if (
                existing is not None
                and existing != compilation.visual_receipt_root_sha256
            ):
                visual_page_roots[page] = canonical_json_sha256(
                    sorted((existing, compilation.visual_receipt_root_sha256))
                )
            else:
                visual_page_roots[page] = compilation.visual_receipt_root_sha256

    # Every scope owns its text and placements once; a set equality without this
    # multiplicity check would hide overlapping ownership across parallel scopes.
    if len(represented_source_ids) != len(set(represented_source_ids)):
        raise ValueError("Document Map scopes claim the same source node more than once")
    if len(represented_table_rows) != len(set(represented_table_rows)):
        raise ValueError("Document Map scopes claim the same physical table row more than once")
    is_complete = complete_source_node_ids is not None and not all_unresolved
    if is_complete and set(represented_source_ids) != set(complete_source_node_ids):
        raise ValueError("complete Document Map does not cover its exact source ledger")
    if is_complete and complete_physical_table_ids is not None and {table_id for table_id, _ in represented_table_rows} != set(complete_physical_table_ids):
        raise ValueError("complete Document Map does not cover its physical tables")
    if is_complete:
        for node in all_nodes:
            node["coverage_state"] = "COMPLETE"

    ordered_nodes = _topological_nodes(all_nodes)
    document_tree = {
        "schema_version": "native-document-structure-tree/v1",
        "root_ids": [item["node_id"] for item in root_nodes],
        "nodes": ordered_nodes,
    }
    base_artifact_id = (
        "document-map-artifact-"
        + canonical_json_sha256(
            {
                "run_id": run_id,
                "source_sha256": source_sha256,
                "survey_compilation_sha256": survey.compilation_sha256,
            }
        )[:32]
    )
    base_projection_sha256 = (
        previous.base_projection_sha256
        if previous is not None
        else canonical_json_sha256(
            {
                "schema_version": "document-map-survey-working-basis/v1",
                "source_sha256": source_sha256,
                "survey_compilation_sha256": survey.compilation_sha256,
                "root_ids": [item["node_id"] for item in root_nodes],
            }
        )
    )
    bindings = _store_table_previews(
        previews=all_previews,
        logical_tables=all_tables,
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        base_artifact_id=base_artifact_id,
        base_projection_sha256=base_projection_sha256,
        object_store=object_store,
    )
    visual_summary = {
        "opened_page_count": len(visual_page_roots),
        "page_count": survey.page_count,
        "receipts": [
            {
                "page": page,
                "sha256": receipt_root,
                "receipt_kind": "SCOPE_VISUAL_RECEIPT_ROOT",
            }
            for page, receipt_root in sorted(visual_page_roots.items())
        ],
    }
    d2_receipt = build_d2_host_compiler_receipt(
        run_id=run_id,
        source_sha256=source_sha256,
        source_ir_canonical_sha256=source_ir_canonical_sha256,
        kit_bundle_sha256=kit_bundle_sha256,
        survey_compilation_sha256=survey.compilation_sha256,
        scope_compilation_sha256s=compilation_hashes,
        compiled_node_count=len(ordered_nodes),
        compiled_table_count=len(all_tables),
    )
    working = compile_native_structure_working_revision(
        run_id=run_id,
        source_sha256=source_sha256,
        base_artifact_id=base_artifact_id,
        base_projection_sha256=base_projection_sha256,
        evidence_artifact_id=(
            previous.evidence_artifact_id
            if previous is not None
            else "source-ir-evidence-" + source_ir_canonical_sha256[:32]
        ),
        revision=revision,
        previous=previous,
        validator_kind="D2_HOST_COMPILER",
        state="ACCEPTED" if is_complete else "WORKING",
        candidate_document_tree=document_tree,
        page_ownership=page_ownership,
        logical_tables=all_tables,
        table_preview_bindings=bindings,
        structure_proposals=(),
        unresolved=all_unresolved,
        visual_summary=visual_summary,
        d2_compiler_receipt=d2_receipt,
    )
    publish_native_structure_working_revision(
        project_id=project_id,
        document_id=document_id,
        revision=working,
        repository=repository,
        object_store=object_store,
    )
    return working


def _survey_roots(
    survey: DocumentMapSurveyCompilationV1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    nodes: list[dict[str, Any]] = []
    page_ownership: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for order, scope in enumerate(survey.scopes):
        node_id = (
            "document-map-unit-"
            + canonical_json_sha256(
                {
                    "source_sha256": survey.source_sha256,
                    "survey_scope_id": scope.scope_id,
                }
            )[:32]
        )
        mapping[scope.scope_id] = node_id
        kind = (
            "FRONT_MATTER"
            if scope.kind is DocumentMapSurveyRangeKind.FRONT_MATTER
            else "DOCUMENT_ROOT"
            if scope.kind is DocumentMapSurveyRangeKind.MAIN_DOCUMENT
            else "DOCUMENT_UNIT"
        )
        nodes.append(
            {
                "node_id": node_id,
                "parent_id": None,
                "order": order,
                "source_order": min(scope.page_numbers) - 1,
                "kind": kind,
                "label": "",
                "title": scope.label,
                "title_authority": "AGENT_SURVEY_LABEL",
                "pages": list(scope.page_numbers),
                "content_blocks": [],
                "source_anchors": [],
                "source_content": [],
                "asset_placements": [],
                "content_sequence": [],
                "finding_codes": [],
                "coverage_state": "PARTIAL",
            }
        )
        page_ownership.append(
            {
                "unit_id": node_id,
                "title": scope.label,
                "pages": list(scope.page_numbers),
                "title_authority": "AGENT_SURVEY_LABEL",
                "confidence": "WORKING",
                "survey_scope_id": scope.scope_id,
            }
        )
    return nodes, page_ownership, mapping


def _project_logical_table(
    table: DocumentMapCompiledLogicalTableV1,
    *,
    evidence: DocumentMapScopeEvidenceIndex,
    source_sha256: str,
    role: str = "SOURCE_TABLE",
) -> tuple[dict[str, Any], dict[str, Any], int]:
    source_orders = [
        _table_record(item.physical_table_id, evidence=evidence).source_order
        for item in table.fragments
    ]
    fragments = [
        {
            "physical_table_id": item.physical_table_id,
            "table_token": item.table_token,
            "page": item.page_number,
            "row_range": [item.start_row, item.end_row],
        }
        for item in table.fragments
    ]
    logical = {
        "scope_id": table.logical_table_scope_id,
        "title": table.title,
        "title_authority": table.title_authority.value,
        "owner_unit_id": "",
        "owner_node_id": table.owner_heading_node_id,
        "role": role,
        "pages": sorted({item.page_number for item in table.fragments}),
        "fragments": fragments,
        "source_numbering_anomaly": "",
        "capture_gap": "",
        "public_summary": table.public_summary,
    }
    preview = {
        "schema_version": "native-structure-table-preview/v1",
        "artifact_id": "PENDING",
        "source_sha256": source_sha256,
        "logical_table_scope_id": table.logical_table_scope_id,
        "title": table.title,
        "fragments": [
            _preview_fragment(
                item,
                table_scope_id=table.logical_table_scope_id,
                evidence=evidence,
            )
            for item in table.fragments
        ],
        "presentation": {
            "schema_version": "native-structure-table-presentation/v1",
            "mode": "STRUCTURED_CELLS",
            "canonical_column_count": None,
            "canonical_projection": None,
            "floating_images": [],
        },
        "summary": {
            "fragment_count": len(table.fragments),
            "row_count": sum(item.end_row - item.start_row for item in table.fragments),
            "cell_count": 0,
        },
    }
    preview["summary"]["cell_count"] = sum(
        len(item["cells"]) for item in preview["fragments"]
    )
    return logical, preview, min(source_orders)


def _preview_fragment(
    fragment: DocumentMapCompiledTableFragmentV1,
    *,
    table_scope_id: str,
    evidence: DocumentMapScopeEvidenceIndex,
) -> dict[str, Any]:
    table = evidence.tables_by_id[fragment.physical_table_id]
    record = _table_record(fragment.physical_table_id, evidence=evidence)
    cells: list[dict[str, Any]] = []
    for raw in table["cells"]:
        row = int(raw["row"])
        row_span = int(raw["row_span"])
        intersection_start = max(row, fragment.start_row)
        intersection_end = min(row + row_span, fragment.end_row)
        if intersection_end <= intersection_start:
            continue
        locator = raw.get("native_locator")
        raw_bbox = locator.get("bbox") if isinstance(locator, Mapping) else None
        bbox = list(record.bbox if raw_bbox is None else raw_bbox)
        text = str(raw.get("raw_text") or raw.get("display_value") or "")
        cells.append(
            {
                "cell_id": str(raw["cell_id"]),
                "row": intersection_start - fragment.start_row,
                "column": int(raw["column"]),
                "row_span": intersection_end - intersection_start,
                "column_span": int(raw["column_span"]),
                "text": text,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "bbox": bbox,
                "source_bounds": list(record.page_bounds),
            }
        )
    return {
        "fragment_id": "document-map-fragment-"
        + canonical_json_sha256(
            {
                "table_scope_id": table_scope_id,
                "physical_table_id": fragment.physical_table_id,
                "start_row": fragment.start_row,
                "end_row": fragment.end_row,
            }
        )[:32],
        "logical_table_scope_id": table_scope_id,
        "physical_table_id": fragment.physical_table_id,
        "table_token": fragment.table_token,
        "page": fragment.page_number,
        "row_range": [fragment.start_row, fragment.end_row],
        "row_count": fragment.end_row - fragment.start_row,
        "column_count": fragment.column_count,
        "bbox": list(record.bbox),
        "source_bounds": list(record.page_bounds),
        "cells": cells,
        "source_header_regions": [
            {
                **region,
                "row": max(int(region["row"]), fragment.start_row) - fragment.start_row,
                "row_span": (
                    min(int(region["row"]) + int(region["row_span"]), fragment.end_row)
                    - max(int(region["row"]), fragment.start_row)
                ),
            }
            for region in table.get("header_regions", [])
            if int(region["row"]) < fragment.end_row
            and int(region["row"]) + int(region["row_span"]) > fragment.start_row
        ],
        "source_merged_regions": table.get("merged_regions", []),
        "source_notes": table.get("notes", []),
        "source_table_sha256": fragment.table_sha256,
    }


def _store_table_previews(
    *,
    previews: Sequence[Mapping[str, Any]],
    logical_tables: Sequence[Mapping[str, Any]],
    project_id: str,
    document_id: str,
    source_sha256: str,
    base_artifact_id: str,
    base_projection_sha256: str,
    object_store: ObjectStore,
) -> tuple[NativeStructureTablePreviewBindingV1, ...]:
    logical_by_id = {str(item["scope_id"]): item for item in logical_tables}
    bindings: list[NativeStructureTablePreviewBindingV1] = []
    for raw in previews:
        preview = _copy_json(raw)
        preview["artifact_id"] = base_artifact_id
        preview["source_sha256"] = source_sha256
        scope_id = str(preview["logical_table_scope_id"])
        if scope_id not in logical_by_id:
            raise ValueError("table preview has no logical table")
        payload = json.dumps(
            preview,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        stored = object_store.put(
            key=(
                f"runtime/agent-table-previews/{project_id}/{document_id}/"
                f"{source_sha256}/{scope_id}/{digest}.json"
            ),
            source=BytesIO(payload),
            expected_sha256=digest,
            media_type=TABLE_PREVIEW_MEDIA_TYPE,
        )
        bindings.append(
            NativeStructureTablePreviewBindingV1(
                scope_id=scope_id,
                preview_sha256=digest,
                object_key=stored.key,
                object_sha256=stored.sha256,
                object_size=stored.size,
                source_sha256=source_sha256,
                base_artifact_id=base_artifact_id,
                base_projection_sha256=base_projection_sha256,
            )
        )
    return tuple(bindings)


def _table_record(
    table_id: str,
    *,
    evidence: DocumentMapScopeEvidenceIndex,
):  # type: ignore[no-untyped-def]
    values = [item for item in evidence.records if item.table_id == table_id]
    if len(values) != 1:
        raise ValueError("physical table source record is not unique")
    return values[0]


def _title_anchor(record) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "anchor_id": "anchor-"
        + canonical_json_sha256(
            {"kind": "TITLE", "source_node_id": record.source_node_id}
        )[:24],
        "page": record.page_number,
        "bbox": list(record.bbox),
        "source_bounds": list(record.page_bounds),
        "anchor_kind": "TITLE",
        "precision": "EXACT_NATIVE_SPAN",
        "source_handle": record.source_node_id,
        "source_text_sha256": record.source_text_sha256,
        "source_order": record.source_order,
    }


def _source_content(
    record,  # type: ignore[no-untyped-def]
    *,
    is_title: bool,
) -> dict[str, Any]:
    return {
        "content_id": "content-"
        + canonical_json_sha256(
            {
                "source_node_id": record.source_node_id,
                "text": record.source_text_sha256,
            }
        )[:24],
        "page": record.page_number,
        "bbox": list(record.bbox),
        "source_bounds": list(record.page_bounds),
        "source_handles": [record.source_node_id],
        "text": record.raw_text,
        "text_sha256": record.source_text_sha256,
        "precision": "EXACT_SOURCE_RANGE",
        "is_title": is_title,
        "source_order": record.source_order,
    }


def _table_anchor(
    record,  # type: ignore[no-untyped-def]
    *,
    table_scope_id: str,
    fragment: DocumentMapCompiledTableFragmentV1,
) -> dict[str, Any]:
    return {
        "anchor_id": "anchor-"
        + canonical_json_sha256(
            {
                "kind": "TABLE_REGION",
                "scope_id": table_scope_id,
                "physical_table_id": fragment.physical_table_id,
                "row_range": [fragment.start_row, fragment.end_row],
            }
        )[:24],
        "page": record.page_number,
        "bbox": list(record.bbox),
        "source_bounds": list(record.page_bounds),
        "anchor_kind": "TABLE_REGION",
        "precision": "EXACT_TABLE_REGION",
        "logical_table_scope_id": table_scope_id,
        "table_token": fragment.table_token,
        "physical_table_id": fragment.physical_table_id,
        "row_range": [fragment.start_row, fragment.end_row],
        "source_order": record.source_order,
    }


def _asset_anchor(
    record,  # type: ignore[no-untyped-def]
    *,
    role: str,
) -> dict[str, Any]:
    return {
        "anchor_id": "anchor-"
        + canonical_json_sha256(
            {"kind": "ASSET", "source_node_id": record.source_node_id}
        )[:24],
        "page": record.page_number,
        "bbox": list(record.bbox),
        "source_bounds": list(record.page_bounds),
        "anchor_kind": "IMAGE",
        "precision": "EXACT_IMAGE",
        "placement_id": record.source_node_id,
        "asset_id": record.asset_id,
        "asset_role": role,
        "source_order": record.source_order,
    }


def _propagate_pages(
    nodes: list[dict[str, Any]],
    *,
    direct_pages: Mapping[str, set[int]],
) -> None:
    children: dict[str, list[str]] = {}
    for node in nodes:
        parent = node.get("parent_id")
        if isinstance(parent, str):
            children.setdefault(parent, []).append(str(node["node_id"]))
    page_cache: dict[str, set[int]] = {}

    def pages(node_id: str) -> set[int]:
        if node_id in page_cache:
            return page_cache[node_id]
        result = set(direct_pages[node_id])
        for child in children.get(node_id, []):
            result.update(pages(child))
        page_cache[node_id] = result
        return result

    for node in nodes:
        node["pages"] = sorted(pages(str(node["node_id"])))


def _topological_nodes(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    copied = [_copy_json(item) for item in values]
    by_id = {str(item["node_id"]): item for item in copied}
    if len(by_id) != len(copied):
        raise ValueError("working Document Map node IDs are duplicated")
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for item in copied:
        parent = item.get("parent_id")
        if parent is None:
            roots.append(item)
        elif str(parent) in by_id:
            children.setdefault(str(parent), []).append(item)
        else:
            raise ValueError("working Document Map parent is missing")
    for siblings in (roots, *children.values()):
        siblings.sort(key=lambda item: int(item.get("source_order", 0)))
        for order, item in enumerate(siblings):
            item["order"] = order
    result: list[dict[str, Any]] = []

    def visit(item: dict[str, Any]) -> None:
        result.append(item)
        for child in children.get(str(item["node_id"]), []):
            visit(child)

    for root in roots:
        visit(root)
    for item in result:
        item.pop("source_order", None)
    return result


def _copy_json(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


__all__ = [
    "ScopeReplacementProjection",
    "assemble_and_publish_document_map_working_revision",
    "project_document_map_scope_replacement",
]
