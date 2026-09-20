from __future__ import annotations

from collections import defaultdict
import math
import re
from typing import Any

from app.application.extraction.structure_snapshot import (
    StructureSnapshot,
    structure_snapshot_id,
    validate_structure_snapshot_against_source_ir,
)
from app.application.extraction.resolve_structure import (
    visual_text_authority_pages,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceAssetKind,
    SourceIRSnapshot,
    SourceNode,
    SourceNodeKind,
)
from app.domain.extraction.structure import (
    BilingualCounterpartGroup,
    BilingualMismatchSignal,
    DocumentLanguagePolicy,
    StructureBoundaryBasis,
)


STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION = "structure-presentation-index/v7"
STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION = "structure-bilingual-projection/v1"
_STRUCTURE_LABEL_LIMIT = 180
_PERSISTED_SOURCE_FACT_KINDS = frozenset(
    {item.value for item in SourceNodeKind} | {"visual_text"}
)
_EMPTY_SOURCE_FACT_LABELS = {
    SourceNodeKind.IMAGE.value: "图片",
    SourceNodeKind.FIGURE.value: "图片",
    SourceNodeKind.TABLE.value: "表格",
    SourceNodeKind.ATTACHMENT.value: "附件",
}
_DETACHED_PDF_FACT_KINDS = frozenset(
    {
        SourceNodeKind.IMAGE,
        SourceNodeKind.FIGURE,
        SourceNodeKind.TABLE,
    }
)
_PDF_FURNITURE_KINDS = frozenset(
    {
        SourceNodeKind.HEADER,
        SourceNodeKind.FOOTER,
        SourceNodeKind.WATERMARK,
    }
)


def physical_source_reading_order_index(
    source_ir: SourceIRSnapshot,
) -> dict[str, int]:
    """Return immutable presentation ranks for SourceIR node facts.

    PDF adapters can expose text blocks in reading order while appending image
    and table facts through separate extraction channels.  Their immutable
    ``source_order`` therefore remains the identity/order tie-breaker, but it
    is not always the visual insertion point for those detached facts.  Insert
    only image/figure/table facts after the nearest earlier non-furniture text
    anchor whose top edge precedes them, then rank the resulting stream.

    This deliberately preserves the adapter order of ordinary text (including
    multi-column decisions already made by the adapter) and never uses a
    caption string, figure number, page number, or customer-specific rule.
    Non-PDF and geometry-free facts retain their original relative order.
    """

    anchors_by_page: dict[int, list[SourceNode]] = defaultdict(list)
    for node in source_ir.nodes:
        locator = node.visual_locator or node.native_locator
        if (
            locator.format is NativeDocumentFormat.PDF
            and locator.page_number is not None
            and locator.bbox is not None
            and node.kind not in _DETACHED_PDF_FACT_KINDS
            and node.kind not in _PDF_FURNITURE_KINDS
        ):
            anchors_by_page[locator.page_number].append(node)

    def reading_key(node: SourceNode) -> tuple[Any, ...]:
        locator = node.visual_locator or node.native_locator
        page_number = locator.page_number
        bbox = locator.bbox
        if (
            locator.format is not NativeDocumentFormat.PDF
            or page_number is None
            or bbox is None
            or node.kind not in _DETACHED_PDF_FACT_KINDS
        ):
            return (
                page_number if page_number is not None else 1_000_000_000,
                node.source_order,
                1,
                0.0,
                0.0,
                node.source_order,
                node.source_node_id,
            )

        def anchor_geometry_key(
            candidate: SourceNode,
        ) -> tuple[float, float, int, str]:
            candidate_locator = candidate.visual_locator or candidate.native_locator
            candidate_bbox = candidate_locator.bbox
            assert candidate_bbox is not None
            return (
                candidate_bbox[1],
                candidate_bbox[0],
                candidate.source_order,
                candidate.source_node_id,
            )

        earlier_anchors: list[SourceNode] = []
        for candidate in anchors_by_page.get(page_number, ()):
            candidate_locator = candidate.visual_locator or candidate.native_locator
            candidate_bbox = candidate_locator.bbox
            if (
                candidate.source_order < node.source_order
                and candidate_bbox is not None
                and candidate_bbox[1] <= bbox[1]
            ):
                earlier_anchors.append(candidate)
        if earlier_anchors:
            anchor = max(
                earlier_anchors,
                key=anchor_geometry_key,
            )
            return (
                page_number,
                anchor.source_order,
                2,
                bbox[1],
                bbox[0],
                node.source_order,
                node.source_node_id,
            )

        page_anchors = anchors_by_page.get(page_number, ())
        if page_anchors:
            first_anchor = min(
                page_anchors,
                key=lambda candidate: (
                    candidate.source_order,
                    candidate.source_node_id,
                ),
            )
            return (
                page_number,
                first_anchor.source_order,
                0,
                bbox[1],
                bbox[0],
                node.source_order,
                node.source_node_id,
            )
        return (
            page_number,
            node.source_order,
            1,
            bbox[1],
            bbox[0],
            node.source_order,
            node.source_node_id,
        )

    ordered_nodes = sorted(source_ir.nodes, key=reading_key)
    return {node.source_node_id: rank for rank, node in enumerate(ordered_nodes)}


def build_structure_presentation_index(
    source_ir: SourceIRSnapshot,
    *,
    structure: StructureSnapshot,
) -> dict[str, Any]:
    """Build the minimum durable projection needed to display Structure.

    The projection deliberately excludes full-page line geometry.  For an
    immutable image asset it retains only the exact native crop locator and
    content identity needed to verify or re-render that figure after restart.
    """

    validate_structure_snapshot_against_source_ir(structure, source_ir)
    source_facts: dict[str, dict[str, Any]] = {}
    assets_by_id = {asset.asset_id: asset for asset in source_ir.assets}
    physical_orders = physical_source_reading_order_index(source_ir)
    authority_pages = visual_text_authority_pages(source_ir)
    visual_cells_by_id = {
        cell.visual_table_cell_id: cell
        for visual_snapshot in source_ir.visual_table_snapshots
        for cell in visual_snapshot.cells
    }
    adopted_visual_text_by_native_cell_id = {
        binding.table_cell_id: visual_cells_by_id[binding.visual_table_cell_id].raw_text
        for binding in structure.visual_table_cell_text_bindings
        if binding.visual_table_cell_id in visual_cells_by_id
    }
    page_count = 0

    def add_fact(
        source_id: str,
        *,
        page_number: int | None,
        text: str,
        source_kind: str,
        source_order: int | None,
        asset_id: str = "",
        asset_kind: str = "",
        content_sha256: str = "",
        media_type: str = "",
        storage_key: str = "",
        location: dict[str, Any] | None = None,
    ) -> None:
        nonlocal page_count
        if page_number is not None:
            page_count = max(page_count, page_number)
        source_facts[source_id] = {
            "page_number": page_number,
            "text": text,
            "source_kind": source_kind,
            "source_order": source_order,
            "asset_id": asset_id,
            "asset_kind": asset_kind,
            "content_sha256": content_sha256,
            "media_type": media_type,
            "storage_key": storage_key,
            "location": location or {},
        }

    for node in source_ir.nodes:
        asset = assets_by_id.get(node.asset_ref or "")
        locator = node.visual_locator or node.native_locator
        # Only native PDF assets are durably materialized by the application
        # asset publisher introduced with presentation v5.  Office adapters
        # intentionally keep package-local ``ooxml/...`` identities; exposing
        # those as object-store claims would make a valid DOCX/XLSX
        # presentation fail verification after restart.
        durable_pdf_asset = (
            asset
            if asset is not None and locator.format is NativeDocumentFormat.PDF
            else None
        )
        add_fact(
            node.source_node_id,
            page_number=node.native_locator.page_number,
            text=(
                ""
                if node.kind is SourceNodeKind.TABLE
                and node.native_locator.page_number in authority_pages
                else node.raw_text
            ),
            source_kind=node.kind.value,
            source_order=physical_orders.get(node.source_node_id, node.source_order),
            asset_id=(
                durable_pdf_asset.asset_id if durable_pdf_asset is not None else ""
            ),
            asset_kind=(
                durable_pdf_asset.kind.value if durable_pdf_asset is not None else ""
            ),
            content_sha256=(
                durable_pdf_asset.content_sha256
                if durable_pdf_asset is not None
                else ""
            ),
            media_type=(
                durable_pdf_asset.media_type if durable_pdf_asset is not None else ""
            ),
            storage_key=(
                durable_pdf_asset.storage_key if durable_pdf_asset is not None else ""
            ),
            location=(
                locator.model_dump(mode="json")
                if durable_pdf_asset is not None
                else None
            ),
        )
        if node.table_payload is not None:
            for cell in node.table_payload.cells:
                cell_page = (
                    cell.native_locator.page_number
                    or node.native_locator.page_number
                )
                add_fact(
                    cell.cell_id,
                    page_number=cell.native_locator.page_number,
                    text=(
                        adopted_visual_text_by_native_cell_id.get(cell.cell_id, "")
                        if cell_page in authority_pages
                        else cell.raw_text
                    ),
                    source_kind=SourceNodeKind.CELL.value,
                    source_order=physical_orders.get(
                        node.source_node_id,
                        node.source_order,
                    ),
                )
    for snapshot in source_ir.visual_text_snapshots:
        page_count = max(page_count, snapshot.page_number)
        for observation in snapshot.observations:
            add_fact(
                observation.observation_id,
                page_number=observation.page_number,
                text=observation.raw_text,
                source_kind=(
                    "visual_text"
                    if observation.content_type == "TEXT"
                    else observation.content_type.lower()
                ),
                # Visual observations have a page-local provider order, not a
                # SourceIR-wide immutable order.  Do not manufacture one.
                source_order=None,
            )

    nodes: dict[str, dict[str, Any]] = {}
    for node in structure.nodes:
        serialized_refs: list[dict[str, Any]] = []
        pages: list[int] = []
        texts: list[str] = []
        source_kinds: list[str] = []
        physical_source_orders: list[int] = []
        for ref in node.source_refs:
            fact = source_facts.get(ref.source_id, {})
            page_number = fact.get("page_number")
            source_kind = str(fact.get("source_kind") or "")
            if source_kind not in _PERSISTED_SOURCE_FACT_KINDS:
                raise ValueError(
                    "Structure presentation source refs require a known source kind."
                )
            serialized_ref: dict[str, Any] = {
                "kind": ref.kind.value,
                "source_id": ref.source_id,
                "source_kind": source_kind,
            }
            asset_id = str(fact.get("asset_id") or "")
            if asset_id:
                serialized_ref.update(
                    {
                        "asset_id": asset_id,
                        "asset_kind": str(fact.get("asset_kind") or ""),
                        "content_sha256": str(fact.get("content_sha256") or ""),
                        "media_type": str(fact.get("media_type") or ""),
                        "storage_key": str(fact.get("storage_key") or ""),
                        "location": dict(fact.get("location") or {}),
                    }
                )
            if (
                isinstance(page_number, int)
                and not isinstance(page_number, bool)
                and page_number > 0
            ):
                serialized_ref["page_number"] = page_number
                pages.append(page_number)
            serialized_refs.append(serialized_ref)
            source_kinds.append(source_kind)
            source_order = fact.get("source_order")
            if (
                isinstance(source_order, int)
                and not isinstance(source_order, bool)
                and source_order >= 0
            ):
                physical_source_orders.append(source_order)
            text = str(fact.get("text") or "").strip()
            if text:
                texts.append(text)
        exact_span_texts: list[str] = []
        for span in node.semantic_source_spans:
            fact = source_facts.get(span.source_ref.source_id, {})
            source_text = str(fact.get("text") or "")
            if not source_text or not (
                0 <= span.start_offset < span.end_offset <= len(source_text)
            ):
                continue
            exact_text = source_text[span.start_offset : span.end_offset].strip()
            if exact_text:
                exact_span_texts.append(exact_text)
        label = _structure_node_label(
            explicit_label=node.explicit_label,
            texts=exact_span_texts or texts,
            source_kinds=source_kinds,
        )
        entry: dict[str, Any] = {
            "label": label,
            # This is the node's directly owned source text only.  It is not
            # descendant roll-up context and it carries no requirement
            # semantics.  Persisting it is necessary for exact-span Structure
            # nodes and raw CONTENT facts to survive a process restart without
            # falling back to a truncated or neighbouring presentation label.
            "owned_text": "\n".join(exact_span_texts)
            if exact_span_texts
            else "\n".join(value for value in texts if value.strip()),
            "source_refs": serialized_refs,
            # TOC navigation is derived from every bound source fact, while
            # ``owned_text`` stores every directly owned source-ref text in
            # canonical ref order, never descendant roll-up.  Persist the
            # per-ref text only for proven TOC scopes so a
            # process restart can reproduce the same source-bound navigation
            # without retaining all document prose in this compact index.
            "toc_source_text_by_id": (
                {
                    ref.source_id: str(
                        source_facts.get(ref.source_id, {}).get("text") or ""
                    )
                    for ref in node.source_refs
                }
                if node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK
                else {}
            ),
        }
        if physical_source_orders:
            entry["physical_source_order"] = min(physical_source_orders)
            span_offsets = tuple(
                span.start_offset
                for span in node.semantic_source_spans
                if source_facts.get(span.source_ref.source_id, {}).get("source_order")
                == entry["physical_source_order"]
            )
            entry["physical_source_offset"] = min(span_offsets, default=0)
        if pages:
            entry["page_start"] = min(pages)
            entry["page_end"] = max(pages)
        nodes[node.structure_node_id] = entry

    return {
        "schema_version": STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
        "source_ir_id": source_ir.source_ir_id,
        "source_sha256": source_ir.source_sha256,
        "structure_snapshot_id": structure_snapshot_id(structure),
        "page_count": page_count,
        "nodes": nodes,
        # Bilingual grouping is a typed Structure fact, not a requirement-tree
        # heuristic.  Persist it beside the compact node projection so a
        # completed result can reproduce the same candidate topology without
        # reparsing SourceIR after a process restart.
        "bilingual_projection": build_structure_bilingual_projection_sidecar(structure),
    }


def validated_structure_presentation_index(
    value: Any,
    *,
    expected_source_sha256: str,
    expected_structure_snapshot_id: str,
) -> dict[str, Any] | None:
    """Validate the persisted projection without accepting legacy shapes."""

    index = value if isinstance(value, dict) else {}
    if (
        str(index.get("schema_version") or "")
        != STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION
        or str(index.get("source_sha256") or "") != expected_source_sha256
        or str(index.get("structure_snapshot_id") or "")
        != expected_structure_snapshot_id
        or not str(index.get("source_ir_id") or "")
        or not isinstance(index.get("nodes"), dict)
    ):
        return None
    page_count = index.get("page_count")
    if (
        not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 0
    ):
        return None
    source_facts: dict[str, tuple[Any, ...]] = {}
    toc_source_texts: dict[str, str] = {}
    for node_id, raw_node in index["nodes"].items():
        if (
            not isinstance(node_id, str)
            or not node_id
            or not isinstance(raw_node, dict)
        ):
            return None
        if not isinstance(raw_node.get("label"), str):
            return None
        if not isinstance(raw_node.get("owned_text"), str):
            return None
        physical_source_order = raw_node.get("physical_source_order")
        physical_source_offset = raw_node.get("physical_source_offset")
        if (physical_source_order is None) != (physical_source_offset is None):
            return None
        if physical_source_order is not None and (
            not isinstance(physical_source_order, int)
            or isinstance(physical_source_order, bool)
            or physical_source_order < 0
            or not isinstance(physical_source_offset, int)
            or isinstance(physical_source_offset, bool)
            or physical_source_offset < 0
        ):
            return None
        refs = raw_node.get("source_refs")
        if not isinstance(refs, list):
            return None
        ref_source_ids: set[str] = set()
        for ref in refs:
            if (
                not isinstance(ref, dict)
                or not str(ref.get("kind") or "")
                or not str(ref.get("source_id") or "")
            ):
                return None
            page_number = ref.get("page_number")
            source_kind = str(ref.get("source_kind") or "")
            if source_kind not in _PERSISTED_SOURCE_FACT_KINDS:
                return None
            if page_number is not None and (
                not isinstance(page_number, int)
                or isinstance(page_number, bool)
                or page_number < 1
                or page_number > page_count
            ):
                return None
            source_id = str(ref["source_id"])
            ref_source_ids.add(source_id)
            asset_id = str(ref.get("asset_id") or "")
            asset_kind = str(ref.get("asset_kind") or "")
            content_sha256 = str(ref.get("content_sha256") or "")
            media_type = str(ref.get("media_type") or "")
            storage_key = str(ref.get("storage_key") or "")
            location = ref.get("location")
            if asset_id:
                if (
                    source_kind
                    not in {
                        SourceNodeKind.IMAGE.value,
                        SourceNodeKind.FIGURE.value,
                    }
                    or re.fullmatch(r"asset-[0-9a-f]{32}", asset_id) is None
                    or asset_kind
                    not in {
                        SourceAssetKind.IMAGE.value,
                        SourceAssetKind.FIGURE.value,
                        SourceAssetKind.PAGE_RENDER.value,
                    }
                    or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
                    or not media_type
                    # Unknown native PDF encodings are still exact immutable
                    # assets. ``application/octet-stream`` is the adapter's
                    # explicit fallback; export may reject object decoding and
                    # use the exact PDF crop without invalidating the complete
                    # presentation index.
                    or media_type != "application/octet-stream"
                    and not media_type.startswith("image/")
                    or not _valid_asset_storage_key(
                        storage_key,
                        asset_kind=asset_kind,
                        content_sha256=content_sha256,
                        source_sha256=expected_source_sha256,
                        page_number=page_number,
                    )
                    or not _valid_pdf_asset_location(
                        location,
                        expected_page_number=page_number,
                    )
                ):
                    return None
            elif (
                asset_kind
                or content_sha256
                or media_type
                or storage_key
                or location not in (None, {})
            ):
                return None
            source_fact = (
                source_kind,
                page_number,
                asset_id,
                asset_kind,
                content_sha256,
                media_type,
                storage_key,
                repr(location or {}),
            )
            previous_source_fact = source_facts.setdefault(source_id, source_fact)
            if previous_source_fact != source_fact:
                return None
        toc_source_text_by_id = raw_node.get("toc_source_text_by_id")
        if not isinstance(toc_source_text_by_id, dict):
            return None
        for source_id, source_text in toc_source_text_by_id.items():
            if (
                not isinstance(source_id, str)
                or not source_id
                or source_id not in ref_source_ids
                or not isinstance(source_text, str)
            ):
                return None
            previous_source_text = toc_source_texts.setdefault(source_id, source_text)
            if previous_source_text != source_text:
                return None
        page_start = raw_node.get("page_start")
        page_end = raw_node.get("page_end")
        if (page_start is None) != (page_end is None):
            return None
        if page_start is not None and (
            not isinstance(page_start, int)
            or isinstance(page_start, bool)
            or not isinstance(page_end, int)
            or isinstance(page_end, bool)
            or page_start < 1
            or page_end < page_start
            or page_end > page_count
        ):
            return None
    if (
        validated_structure_bilingual_projection_sidecar(
            index.get("bilingual_projection"),
            expected_source_ir_id=str(index.get("source_ir_id") or ""),
            available_structure_node_ids=frozenset(index["nodes"]),
            available_source_ids=frozenset(source_facts),
        )
        is None
    ):
        return None
    return index


def build_structure_bilingual_projection_sidecar(
    structure: StructureSnapshot | None,
) -> dict[str, Any]:
    """Serialize only Core-validated bilingual facts, including the empty case."""

    policy = structure.document_language_policy if structure is not None else None
    groups = structure.bilingual_counterpart_groups if structure is not None else ()
    mismatch_signals = (
        structure.bilingual_mismatch_signals if structure is not None else ()
    )
    return {
        "schema_version": STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION,
        "document_language_policy": (
            policy.model_dump(mode="json") if policy is not None else None
        ),
        "counterpart_groups": [group.model_dump(mode="json") for group in groups],
        "mismatch_signals": [
            signal.model_dump(mode="json") for signal in mismatch_signals
        ],
    }


def validated_structure_bilingual_projection_sidecar(
    value: Any,
    *,
    expected_source_ir_id: str,
    available_structure_node_ids: frozenset[str],
    available_source_ids: frozenset[str],
) -> dict[str, Any] | None:
    """Validate a persisted bilingual sidecar without reconstructing Structure.

    Stable IDs on the typed models bind policy, exact spans and mismatch
    evidence.  This additional cross-check binds those facts to the enclosing
    presentation's SourceIR and Structure catalogs.  Relation grounding is
    checked again by the RequirementTree projector against the persisted
    effective relations before any candidates are coalesced.
    """

    sidecar = value if isinstance(value, dict) else {}
    if set(sidecar) != {
        "schema_version",
        "document_language_policy",
        "counterpart_groups",
        "mismatch_signals",
    } or (
        str(sidecar.get("schema_version") or "")
        != STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION
    ):
        return None
    raw_policy = sidecar.get("document_language_policy")
    raw_groups = sidecar.get("counterpart_groups")
    raw_signals = sidecar.get("mismatch_signals")
    if not isinstance(raw_groups, list) or not isinstance(raw_signals, list):
        return None
    if raw_policy is None:
        if raw_groups or raw_signals:
            return None
        return sidecar
    if not isinstance(raw_policy, dict):
        return None
    try:
        policy = DocumentLanguagePolicy.model_validate(raw_policy)
        groups = tuple(
            BilingualCounterpartGroup.model_validate(item) for item in raw_groups
        )
        mismatch_signals = tuple(
            BilingualMismatchSignal.model_validate(item) for item in raw_signals
        )
    except (TypeError, ValueError):
        return None
    if (
        policy.authority_basis == "UNKNOWN"
        and policy.policy_scope_source_ir_id != expected_source_ir_id
    ):
        return None

    group_ids = tuple(group.counterpart_group_id for group in groups)
    signal_ids = tuple(signal.mismatch_signal_id for signal in mismatch_signals)
    if group_ids != tuple(sorted(set(group_ids))) or signal_ids != tuple(
        sorted(set(signal_ids))
    ):
        return None

    assigned_member_ids: set[str] = set()
    assigned_span_ids: set[str] = set()
    groups_by_id: dict[str, BilingualCounterpartGroup] = {}
    for group in groups:
        group_spans = (*group.english_spans, *group.chinese_spans)
        group_span_ids = {span.bilingual_span_id for span in group_spans}
        if (
            group.document_language_policy_id != policy.document_language_policy_id
            or not set(group.member_structure_node_ids).issubset(
                available_structure_node_ids
            )
            or assigned_member_ids.intersection(group.member_structure_node_ids)
            or any(
                span.source_ref.source_id not in available_source_ids
                for span in group_spans
            )
            or assigned_span_ids.intersection(group_span_ids)
        ):
            return None
        assigned_member_ids.update(group.member_structure_node_ids)
        assigned_span_ids.update(group_span_ids)
        groups_by_id[group.counterpart_group_id] = group

    policy_spans = (
        *policy.english_declaration_spans,
        *policy.chinese_declaration_spans,
    )
    if any(
        span.source_ref.source_id not in available_source_ids for span in policy_spans
    ):
        return None
    for signal in mismatch_signals:
        group = groups_by_id.get(signal.counterpart_group_id)
        if (
            group is None
            or not set(signal.english_span_ids).issubset(
                span.bilingual_span_id for span in group.english_spans
            )
            or not set(signal.chinese_span_ids).issubset(
                span.bilingual_span_id for span in group.chinese_spans
            )
        ):
            return None
    return sidecar


def _valid_pdf_asset_location(
    value: Any,
    *,
    expected_page_number: int | None,
) -> bool:
    location = value if isinstance(value, dict) else {}
    bbox = location.get("bbox")
    return bool(
        str(location.get("format") or "").lower() == "pdf"
        and location.get("page_number") == expected_page_number
        and isinstance(bbox, list)
        and len(bbox) == 4
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in bbox
        )
        and float(bbox[2]) >= float(bbox[0])
        and float(bbox[3]) >= float(bbox[1])
    )


def _valid_asset_storage_key(
    value: Any,
    *,
    asset_kind: str,
    content_sha256: str,
    source_sha256: str,
    page_number: int | None,
) -> bool:
    storage_key = str(value or "")
    if asset_kind == SourceAssetKind.PAGE_RENDER.value:
        return bool(
            page_number is not None
            and storage_key
            == (
                f"source-page-renders/{source_sha256[:2]}/{source_sha256}/"
                f"page-{page_number:06d}-{content_sha256}.png"
            )
        )
    match = re.fullmatch(
        rf"source-assets/{content_sha256[:2]}/{content_sha256}\.([a-z0-9]{{1,12}})",
        storage_key,
    )
    return match is not None


def _structure_node_label(
    *,
    explicit_label: str | None,
    texts: list[str],
    source_kinds: list[str],
) -> str:
    label = str(explicit_label or "").strip()
    text = next((value for value in texts if value.strip()), "")
    if text:
        compact = _compact_label(text)
        return (
            compact if not label or compact.startswith(label) else f"{label} {compact}"
        )
    if label:
        return label
    return next(
        (
            _EMPTY_SOURCE_FACT_LABELS[source_kind]
            for source_kind in source_kinds
            if source_kind in _EMPTY_SOURCE_FACT_LABELS
        ),
        "",
    )


def _compact_label(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) <= _STRUCTURE_LABEL_LIMIT:
        return compact
    return f"{compact[: _STRUCTURE_LABEL_LIMIT - 1]}…"


__all__ = [
    "STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION",
    "STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION",
    "build_structure_bilingual_projection_sidecar",
    "build_structure_presentation_index",
    "validated_structure_bilingual_projection_sidecar",
    "validated_structure_presentation_index",
]
