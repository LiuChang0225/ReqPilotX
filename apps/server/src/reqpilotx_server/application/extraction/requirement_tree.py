from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any, Iterable

from reqpilotx_server.application.extraction.inspect_structure import (
    STRUCTURE_INSPECTION_RUNTIME_VERSION,
)
from reqpilotx_server.domain.extraction.requirement_tree import (
    RequirementTreeBilingualContextV1,
    RequirementTreeBilingualSourceSpanV1,
    RequirementTreeMode,
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeSourceIdentity,
    RequirementTreeState,
    RequirementTreeStructureIdentity,
)
from reqpilotx_server.domain.extraction.structure import (
    BilingualCounterpartGroup,
    BilingualMismatchSignal,
    BilingualSpanBoundaryBasis,
    DocumentLanguagePolicy,
)
from reqpilotx_server.domain.extraction.llm_requirement_review import LlmReviewSourceSpan
from reqpilotx_server.domain.extraction.table_records import (
    TableRecordKind,
    TableRecordProjectionMode,
    TableRecordProjectionV1,
    TableRecordV1,
)
from reqpilotx_server.domain.extraction.table_evidence import (
    RequirementEvidenceBindingV1,
    RequirementEvidenceBundleV1,
    RequirementEvidenceSourceRefV1,
    TableEvidenceBindingV1,
)


REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION = "requirement-tree-projection/v14"

_STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION = "structure-bilingual-projection/v1"


_STRUCTURE_KINDS = {
    "DOCUMENT",
    "SECTION",
    "CLAUSE",
    "GROUP",
    "LIST_GROUP",
}

_SOURCE_CONTENT_KINDS = {
    "CONTENT",
    "TABLE",
    "FIGURE",
}

# DOCUMENT and GROUP are navigation/root scopes.  Raw blocks that resolve only
# to those scopes are deliberately left to structure inspection instead of
# becoming hundreds of apparent requirement candidates (page furniture,
# covers, indexes, or otherwise unassigned content).  A direct-content review
# leaf requires a semantic clause/section owner with a stable source boundary.
_DIRECT_REVIEW_OWNER_KINDS = {
    "SECTION",
    "CLAUSE",
    "LIST_GROUP",
}

_REVIEWABLE_PROJECTION_ROLES = {
    "STRUCTURE_PROXY",
    "DIRECT_CONTENT",
    "WHOLE_TABLE_REVIEW",
    "TABLE_RECORD",
    "REQUIREMENT",
}

_STABLE_REVIEW_ENTRY_ROLES = {
    "STRUCTURE_PROXY",
    "DIRECT_CONTENT",
    "WHOLE_TABLE_REVIEW",
}


@dataclass(frozen=True, slots=True)
class _MutableProjection:
    id: str
    parent_id: str | None
    kind: str
    source_order: int
    source_offset: int
    label: str
    summary: str
    structure_node_id: str | None
    requirement_entry_id: str | None
    workflow_node_id: str | None
    page_start: int | None
    page_end: int | None
    source_refs: tuple[dict[str, Any], ...]
    status: str
    classification: str
    projection_role: str
    semantic_state: str
    deterministic_rule_evidence: tuple[dict[str, Any], ...]
    table_record_id: str | None
    table_evidence_binding_id: str | None
    include_in_view: bool
    content_blocks: tuple[dict[str, Any], ...]
    table_refs: tuple[dict[str, Any], ...]
    figure_refs: tuple[dict[str, Any], ...]
    toc_entries: tuple[dict[str, Any], ...] = ()
    attention_self: int = 0
    reviewed_self: int = 0
    review_item_self: int = 0
    bilingual_context: RequirementTreeBilingualContextV1 | None = None


@dataclass(frozen=True, slots=True)
class _DirectContentEvent:
    node_id: str
    owner_structure_node_id: str
    source_order: int
    source_offset: int
    block: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _DirectContentSegment:
    id: str
    owner_structure_node_id: str
    source_order: int
    source_offset: int
    content_blocks: tuple[dict[str, Any], ...]
    source_refs: tuple[dict[str, Any], ...]
    page_start: int | None
    page_end: int | None
    table_refs: tuple[dict[str, Any], ...]
    figure_refs: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _FigureCaptionBinding:
    media_structure_node_id: str
    caption_structure_node_id: str
    caption: str
    caption_source_refs: tuple[dict[str, Any], ...]
    caption_source_order: int
    caption_source_offset: int


@dataclass(frozen=True, slots=True)
class _ValidatedBilingualProjection:
    group: BilingualCounterpartGroup
    policy: DocumentLanguagePolicy
    mismatch_signals: tuple[BilingualMismatchSignal, ...]
    span_owner_by_id: tuple[tuple[str, str], ...]


def build_requirement_tree_snapshot(
    *,
    workbench: dict[str, Any],
    payload: dict[str, Any] | None = None,
    mode: RequirementTreeMode = RequirementTreeMode.WORKING,
    requirement_set_id: str = "",
    requirement_entries: Iterable[dict[str, Any]] | None = None,
    forced_state: RequirementTreeState | None = None,
) -> RequirementTreeSnapshotV1:
    """Project Effective Structure and its owned requirements into one tree.

    Structure is authoritative. Requirement rows may contribute leaf content and
    exact owner bindings, but they never manufacture section ancestry.
    """

    payload = payload if isinstance(payload, dict) else {}
    source = _dict(workbench.get("source"))
    structure = _dict(workbench.get("structure"))
    agent = _dict(workbench.get("agent"))
    geometry_identity = _dict(
        _dict(workbench.get("source_pages")).get("geometry_index")
    )
    raw_structure_nodes = _dict_items(structure.get("nodes"))
    all_structure_by_id = {
        str(item.get("id") or ""): item
        for item in raw_structure_nodes
        if str(item.get("id") or "")
    }
    table_record_projections = tuple(
        TableRecordProjectionV1.model_validate(item)
        for item in _dict_items(payload.get("table_record_projections"))
    )
    table_cells_by_id = _table_cells_by_id(payload)
    table_evidence_bindings = tuple(
        TableEvidenceBindingV1.model_validate(item)
        for item in _dict_items(payload.get("table_evidence_bindings"))
    )
    whole_table_evidence_by_logical_id = {
        binding.logical_table_id: binding
        for binding in table_evidence_bindings
        if binding.record_id is None
    }
    table_evidence_by_record_id = {
        str(binding.record_id): binding
        for binding in table_evidence_bindings
        if binding.record_id is not None
    }
    structure_proxy_mode = (
        requirement_entries is None
        and _uses_local_structure_projection(workbench=workbench, payload=payload)
    )
    entry_source = (
        list(requirement_entries)
        if requirement_entries is not None
        else []
        if structure_proxy_mode
        else _working_requirement_entries(workbench=workbench, payload=payload)
    )
    normalized_entries = [
        _normalize_requirement_entry(item, index=index)
        for index, item in enumerate(entry_source)
        if isinstance(item, dict)
    ]
    normalized_entries = [item for item in normalized_entries if item is not None]
    normalized_entries = _dedupe_exact_table_text_channels(normalized_entries)
    proxy_entries_by_stable_id: dict[str, _RequirementEntry] = {}
    proxy_entries_by_owner: dict[str, list[_RequirementEntry]] = defaultdict(list)
    proxy_entries_by_record: dict[str, _RequirementEntry] = {}
    semantic_entries: list[_RequirementEntry] = []
    for entry in normalized_entries:
        if entry.projection_role == "TABLE_RECORD":
            record_id = entry.table_record_id or ""
            if not record_id:
                raise ValueError("published table records require a record ID")
            if record_id in proxy_entries_by_record:
                raise ValueError("a table record can bind at most one published entry")
            proxy_entries_by_record[record_id] = entry
        elif entry.projection_role in _STABLE_REVIEW_ENTRY_ROLES:
            owner_id = entry.structure_node_id or ""
            if not owner_id:
                raise ValueError("published proxy entries require a structure owner")
            if entry.stable_id in proxy_entries_by_stable_id:
                raise ValueError("published proxy tree-node IDs must be unique")
            proxy_entries_by_stable_id[entry.stable_id] = entry
            proxy_entries_by_owner[owner_id].append(entry)
        else:
            semantic_entries.append(entry)
    normalized_entries = semantic_entries
    # Local classification is immutable suggestion evidence for the complete
    # structure projection, not only for nodes that currently become review
    # proxies.  Requirement Set publication must therefore retain it on
    # structural ancestors and table owners as well as on reviewable leaves.
    deterministic_evidence_by_owner = _deterministic_rule_evidence_by_owner(payload)

    visible_structure_ids = {
        node_id
        for node_id, item in all_structure_by_id.items()
        if str(item.get("kind") or "").upper() in _STRUCTURE_KINDS
    }
    visible_structure_ids.update(
        node_id
        for node_id, item in all_structure_by_id.items()
        if str(item.get("kind") or "").upper() in _STRUCTURE_KINDS
        and _list(item.get("health_codes"))
    )
    for entry in normalized_entries:
        assert entry is not None
        owner_id = entry.structure_node_id or ""
        current_id = owner_id
        visited: set[str] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            current = all_structure_by_id.get(current_id)
            if current is None:
                break
            if str(current.get("kind") or "").upper() in _STRUCTURE_KINDS:
                visible_structure_ids.add(current_id)
            current_id = str(current.get("parent_id") or "")
    visible_structure_ids.difference_update(
        _navigation_only_duplicate_scope_ids(
            all_structure_by_id=all_structure_by_id,
            visible_structure_ids=visible_structure_ids,
            protected_structure_ids={
                *proxy_entries_by_owner,
                *(
                    entry.structure_node_id
                    for entry in normalized_entries
                    if entry.structure_node_id
                ),
            },
        )
    )

    relations = tuple(_dict_items(workbench.get("relations")))
    validated_bilingual_projections = _validated_bilingual_projections(
        structure=structure,
        relations=relations,
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
    )
    table_ordinal_by_logical_id = {
        projection.table_fact.logical_table_id: ordinal
        for ordinal, projection in enumerate(
            sorted(
                table_record_projections,
                key=lambda item: (
                    min(
                        (part.source_order for part in item.table_fact.table_parts),
                        default=0,
                    ),
                    item.table_fact.logical_table_id,
                ),
            ),
            start=1,
        )
    }
    (
        table_caption_node_ids,
        table_caption_blocks_by_logical_id,
    ) = _table_caption_markers(
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
        table_record_projections=table_record_projections,
        table_ordinal_by_logical_id=table_ordinal_by_logical_id,
    )
    (
        figure_caption_node_ids,
        figure_caption_bindings,
    ) = _figure_caption_markers(
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
        relations=relations,
    )
    excluded_caption_node_ids = {
        *table_caption_node_ids,
        *figure_caption_node_ids,
    }
    has_semantic_structure_scope = any(
        str(all_structure_by_id[node_id].get("kind") or "").upper()
        in _DIRECT_REVIEW_OWNER_KINDS
        for node_id in visible_structure_ids
    )
    source_ir_status = str(
        _dict(_dict(payload.get("pipeline")).get("source_ir")).get("status") or ""
    ).upper()

    table_structure_by_logical_id = {
        projection.table_fact.logical_table_id: _table_structure_for_projection(
            projection,
            all_structure_by_id=all_structure_by_id,
        )
        for projection in table_record_projections
    }

    def table_owner_is_reviewable(owner_id: str | None) -> bool:
        if not owner_id:
            return False
        owner_kind = str(
            all_structure_by_id.get(owner_id, {}).get("kind") or ""
        ).upper()
        if owner_kind in _DIRECT_REVIEW_OWNER_KINDS:
            return True
        # A genuinely table-only source may use the document root as its
        # semantic container.  Once ordinary sections/clauses exist, however,
        # a root-owned table needs a separately proven body presentation owner;
        # otherwise cover, revision-log, index, and other front-matter tables
        # must not appear as requirement candidates. Partial source capture also
        # fails closed instead of promoting orphan tables.
        return (
            owner_kind == "DOCUMENT"
            and not has_semantic_structure_scope
            and source_ir_status not in {"PARTIAL", "FAILED"}
        )

    table_presentation_owner_by_logical_id: dict[str, str] = {}
    for table_projection in table_record_projections:
        logical_id = table_projection.table_fact.logical_table_id
        source_owner_id = _nearest_visible_owner(
            table_projection.table_fact.owner_structure_node_id,
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        if table_owner_is_reviewable(source_owner_id):
            assert source_owner_id is not None
            table_presentation_owner_by_logical_id[logical_id] = source_owner_id
            continue
        if (
            source_ir_status in {"PARTIAL", "FAILED"}
            or str(
                all_structure_by_id.get(source_owner_id or "", {}).get("kind") or ""
            ).upper()
            != "DOCUMENT"
        ):
            continue
        recovered_owner_id = _proven_body_owner_for_root_table(
            table_structure=table_structure_by_logical_id.get(logical_id, {}),
            all_structure_by_id=all_structure_by_id,
            visible_structure_ids=visible_structure_ids,
        )
        if recovered_owner_id:
            table_presentation_owner_by_logical_id[logical_id] = recovered_owner_id

    table_review_owner_ids = {
        visible_owner_id
        for table_projection in table_record_projections
        if (
            visible_owner_id := table_presentation_owner_by_logical_id.get(
                table_projection.table_fact.logical_table_id
            )
        )
    }
    # Keep a recovered table out of the stale document-root content roll-up;
    # its immutable Structure/TableFact owner remains unchanged.
    table_review_owner_ids.update(
        source_owner_id
        for table_projection in table_record_projections
        if table_projection.table_fact.logical_table_id
        in table_presentation_owner_by_logical_id
        and (
            source_owner_id := _nearest_visible_owner(
                table_projection.table_fact.owner_structure_node_id,
                visible_ids=visible_structure_ids,
                all_nodes=all_structure_by_id,
            )
        )
    )

    content_by_owner = _content_by_structure_owner(
        payload,
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
    )
    content_by_owner = {
        owner_id: _bind_figure_captions_in_blocks(
            _content_blocks_with_source_order(
                blocks,
                all_structure_by_id=all_structure_by_id,
                fallback_source_order=_nonnegative_int(
                    all_structure_by_id.get(owner_id, {}).get("order")
                ),
                event_scope_id=owner_id,
            ),
            bindings_by_media_id=figure_caption_bindings,
        )
        for owner_id, blocks in content_by_owner.items()
    }
    direct_source_owner_by_id = _direct_source_owner_by_id(
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
    )
    direct_segments_by_owner = _direct_content_segments_by_owner(
        workbench=workbench,
        payload=payload,
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
        fallback_content_by_owner=content_by_owner,
        direct_source_owner_by_id=direct_source_owner_by_id,
        excluded_content_node_ids=excluded_caption_node_ids,
        excluded_fallback_owner_ids=table_review_owner_ids,
    )
    direct_segments_by_owner = dict(direct_segments_by_owner)
    visible_children_by_owner = _visible_children_by_owner(
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
    )
    continuation_pairs = {
        (
            str(relation.get("from_id") or ""),
            str(relation.get("to_id") or ""),
        )
        for relation in relations
        if str(relation.get("kind") or "").upper() == "CONTINUES"
        and str(relation.get("from_id") or "")
        and str(relation.get("to_id") or "")
    }
    for owner_id in visible_structure_ids:
        owner = all_structure_by_id[owner_id]
        if not _explicit_node_has_inline_content(
            owner,
            all_structure_by_id=all_structure_by_id,
        ):
            continue
        inline_segment = _inline_structure_content_segment(owner)
        if inline_segment is not None:
            existing = direct_segments_by_owner.get(owner_id, ())
            first_segment = min(
                existing,
                key=lambda segment: (
                    segment.source_order,
                    segment.source_offset,
                    segment.id,
                ),
                default=None,
            )
            inline_is_complete = bool(
                _INLINE_NUMBERED_SENTENCE_END_PATTERN.search(
                    _explicit_node_inline_body(owner)
                )
            )
            inline_is_normative = _explicit_node_has_normative_content(owner)
            joins_first_segment = bool(
                first_segment is not None
                and _inline_segment_physically_continues_into_first_zone(
                    owner=owner,
                    inline_segment=inline_segment,
                    first_segment=first_segment,
                    all_structure_by_id=all_structure_by_id,
                    visible_structure_ids=visible_structure_ids,
                    continuation_pairs=continuation_pairs,
                )
            )
            if (
                owner_id in table_review_owner_ids
                and not inline_is_complete
                and not inline_is_normative
                and not joins_first_segment
            ):
                # A numbered noun phrase such as ``1、参数表`` is the table's
                # structural title.  Without an independently complete inline
                # statement or exact continuation evidence it must not
                # duplicate the table evidence unit.
                continue
            if joins_first_segment:
                assert first_segment is not None
                merged_source_key = min(
                    (inline_segment.source_order, inline_segment.source_offset),
                    (first_segment.source_order, first_segment.source_offset),
                )
                merged = _direct_content_segment_from_blocks(
                    owner_id=owner_id,
                    source_order=merged_source_key[0],
                    source_offset=merged_source_key[1],
                    blocks=(
                        *inline_segment.content_blocks,
                        *first_segment.content_blocks,
                    ),
                )
                direct_segments_by_owner[owner_id] = tuple(
                    sorted(
                        (
                            merged,
                            *(
                                segment
                                for segment in existing
                                if segment.id != first_segment.id
                            ),
                        ),
                        key=lambda segment: (
                            segment.source_order,
                            segment.source_offset,
                            segment.id,
                        ),
                    )
                )
            elif (
                inline_is_complete
                or inline_is_normative
                or (not existing and not visible_children_by_owner.get(owner_id))
            ):
                # A complete or normatively worded printed provision remains
                # independently reviewable. An unfinished noun label remains
                # structure context unless exact continuation evidence exists,
                # regardless of intervening table/figure/child boundaries.
                # With no direct body, an unfinished explicit container stays
                # structural when it owns visible children. A normative
                # lead-in remains reviewable context for those children, and
                # a terminal clause still becomes the single review proxy for
                # its printed text.
                direct_segments_by_owner[owner_id] = tuple(
                    sorted(
                        (*existing, inline_segment),
                        key=lambda segment: (
                            segment.source_order,
                            segment.source_offset,
                            segment.id,
                        ),
                    )
                )
    figure_blocks_by_structure_id = _source_bound_figure_blocks_by_id(
        all_structure_by_id=all_structure_by_id,
    )
    direct_segments_by_owner = _bind_explicit_figure_references_to_segments(
        direct_segments_by_owner=direct_segments_by_owner,
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
        relations=relations,
        figure_blocks_by_structure_id=figure_blocks_by_structure_id,
        excluded_source_node_ids=figure_caption_node_ids,
    )
    direct_segments_by_owner = _ensure_typed_bilingual_owner_segments(
        direct_segments_by_owner=direct_segments_by_owner,
        validated_groups=validated_bilingual_projections,
        all_structure_by_id=all_structure_by_id,
    )
    (
        direct_segments_by_owner,
        bilingual_context_by_projection_id,
    ) = _coalesce_typed_bilingual_direct_content(
        direct_segments_by_owner=direct_segments_by_owner,
        validated_groups=validated_bilingual_projections,
        all_structure_by_id=all_structure_by_id,
    )
    direct_media_owner_ids = {
        owner_id
        for media_id in figure_blocks_by_structure_id
        if (
            owner_id := _nearest_visible_owner(
                str(all_structure_by_id[media_id].get("parent_id") or ""),
                visible_ids=visible_structure_ids,
                all_nodes=all_structure_by_id,
            )
        )
    }
    # Preserve the compact leaf interaction only for one source-bound content
    # zone with no structural/table children.  A table-owning clause remains a
    # structure node and exposes prose plus table as separate evidence units;
    # deterministic semantic suggestions never add or remove tree members.
    foldable_proxy_ids = {
        owner_id
        for owner_id in visible_structure_ids
        if len(direct_segments_by_owner.get(owner_id, ())) == 1
        and not visible_children_by_owner.get(owner_id)
        and owner_id not in table_review_owner_ids
        and owner_id not in direct_media_owner_ids
        and str(all_structure_by_id[owner_id].get("kind") or "").upper()
        in {"SECTION", "CLAUSE", "LIST_GROUP"}
    }
    folded_proxy_ids: set[str] = set()
    review_direct_segment_ids: set[str] = set()
    if structure_proxy_mode:
        folded_proxy_ids.update(foldable_proxy_ids)
        review_direct_segment_ids.update(
            segment.id
            for owner_id, segments in direct_segments_by_owner.items()
            if owner_id not in folded_proxy_ids
            for segment in segments
        )
    whole_table_review_id_by_logical_id = {
        projection.table_fact.logical_table_id: _whole_table_review_node_id(projection)
        for projection in table_record_projections
        if projection.mode is TableRecordProjectionMode.WHOLE_TABLE
        and projection.table_fact.logical_table_id
        in table_presentation_owner_by_logical_id
    }
    table_record_suborder_by_id: dict[str, int] = {}
    unmatched_proxy_ids = set(proxy_entries_by_stable_id)
    for owner_id in foldable_proxy_ids:
        if owner_id in proxy_entries_by_stable_id:
            folded_proxy_ids.add(owner_id)
            unmatched_proxy_ids.discard(owner_id)
    eligible_direct_segment_ids = {
        segment.id
        for segments in direct_segments_by_owner.values()
        for segment in segments
    }
    for segment_id in eligible_direct_segment_ids:
        if segment_id in proxy_entries_by_stable_id:
            review_direct_segment_ids.add(segment_id)
            unmatched_proxy_ids.discard(segment_id)
    for node_id in whole_table_review_id_by_logical_id.values():
        if node_id in proxy_entries_by_stable_id:
            unmatched_proxy_ids.discard(node_id)
    if unmatched_proxy_ids:
        raise ValueError(
            "published proxy entries do not match current direct-content boundaries"
        )

    projections: list[_MutableProjection] = []
    for node_id in visible_structure_ids:
        item = all_structure_by_id[node_id]
        physical_source_order, physical_source_offset = _structure_physical_reading_key(
            item
        )
        parent_id = _nearest_visible_parent(
            str(item.get("parent_id") or ""),
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        direct_segments = direct_segments_by_owner.get(node_id, ())
        content = _structure_presentation_content(
            node_id=node_id,
            blocks=content_by_owner.get(node_id, ()),
            direct_source_owner_by_id=direct_source_owner_by_id,
            all_structure_by_id=all_structure_by_id,
        )
        heading_page_start = _positive_int(item.get("page_start"))
        heading_page_end = _positive_int(item.get("page_end"))
        health_codes = tuple(str(code) for code in _list(item.get("health_codes")))
        is_proxy = node_id in folded_proxy_ids
        deterministic_evidence = deterministic_evidence_by_owner.get(
            node_id,
            (),
        )
        published_proxy = proxy_entries_by_stable_id.get(node_id)
        if published_proxy is not None:
            deterministic_evidence = published_proxy.deterministic_rule_evidence
            is_proxy = True
        if is_proxy and len(direct_segments) == 1:
            # A proxy is the compact presentation of exactly one local content
            # zone.  Use that zone's own blocks instead of a descendant/table
            # roll-up so publication never duplicates evidence.  Media is
            # clause evidence rather than a second review candidate, so keep
            # directly owned images/figures beside the text zone.
            media_blocks = tuple(
                block
                for block in content
                if (
                    str(block.get("kind") or "").lower() in {"figure", "image"}
                    or str(block.get("structure_node_id") or "")
                    in figure_caption_node_ids
                )
            )
            content = tuple(
                _dedupe_dicts((*direct_segments[0].content_blocks, *media_blocks))
            )
        projection_label = str(item.get("label") or "").strip()
        if (
            is_proxy
            and len(direct_segments) == 1
            and any(
                str(block.get("structure_node_id") or "") == node_id
                for block in direct_segments[0].content_blocks
            )
        ):
            # An explicit printed clause may wrap into a second physical PDF
            # block.  The tree exposes one content-bearing clause, so its
            # display label must cover that complete source-bound unit while
            # the individual blocks remain available for exact geometry.
            projection_label = _direct_content_label(direct_segments[0].content_blocks)
        content_pages = _content_page_numbers(content)
        folded_source_refs = tuple(
            ref for segment in direct_segments for ref in segment.source_refs
        )
        owned_content_source_refs = (
            _direct_content_source_refs(owner_id=node_id, blocks=content)
            if is_proxy and content
            else ()
        )
        precise_structure_source_refs = _structure_source_span_refs(item)
        structure_heading_source_refs = (
            _structure_heading_source_refs(
                item,
                precise_refs=precise_structure_source_refs,
            )
            if is_proxy
            else ()
        )
        projected_source_refs = (
            tuple(
                _dedupe_dicts(
                    (
                        *structure_heading_source_refs,
                        *precise_structure_source_refs,
                        *folded_source_refs,
                        *owned_content_source_refs,
                    )
                )
            )
            if is_proxy
            else published_proxy.source_refs
            if published_proxy is not None and published_proxy.source_refs
            else precise_structure_source_refs
            if precise_structure_source_refs
            else tuple(_dict_items(item.get("source_refs")))
        )
        projection_bilingual_context = (
            bilingual_context_by_projection_id.get(direct_segments[0].id)
            if is_proxy and len(direct_segments) == 1
            else None
        )
        bilingual_review_required = bool(
            projection_bilingual_context is not None
            and projection_bilingual_context.mismatch_review_required
        )
        projections.append(
            _MutableProjection(
                id=node_id,
                parent_id=parent_id or None,
                kind=str(item.get("kind") or "SECTION").upper(),
                source_order=physical_source_order,
                source_offset=physical_source_offset,
                label=projection_label,
                summary=_first_non_blank(
                    _reading_stream_text(content),
                    str(item.get("label") or ""),
                ),
                structure_node_id=node_id,
                requirement_entry_id=(
                    published_proxy.requirement_entry_id
                    if published_proxy is not None
                    else None
                ),
                workflow_node_id=(
                    published_proxy.workflow_node_id
                    if published_proxy is not None
                    else None
                ),
                page_start=min(
                    (
                        page
                        for page in (heading_page_start, *content_pages)
                        if page is not None
                    ),
                    default=None,
                ),
                page_end=max(
                    (
                        page
                        for page in (heading_page_end, *content_pages)
                        if page is not None
                    ),
                    default=None,
                ),
                source_refs=projected_source_refs,
                status=(
                    "NEEDS_ATTENTION"
                    if health_codes or bilingual_review_required
                    else published_proxy.status
                    if published_proxy is not None
                    else "PENDING"
                    if is_proxy
                    else "READY"
                ),
                classification=("NOT_CLASSIFIED" if is_proxy else "STRUCTURE"),
                projection_role=(
                    published_proxy.projection_role
                    if published_proxy is not None
                    else "STRUCTURE_PROXY"
                    if is_proxy
                    else "STRUCTURE"
                ),
                semantic_state="NOT_CLASSIFIED",
                deterministic_rule_evidence=deterministic_evidence,
                table_record_id=None,
                table_evidence_binding_id=(
                    published_proxy.table_evidence_binding_id
                    if published_proxy is not None
                    else None
                ),
                include_in_view=True,
                content_blocks=content,
                table_refs=_table_refs(content),
                figure_refs=_figure_refs(content),
                toc_entries=tuple(_dict_items(item.get("toc_entries"))),
                attention_self=1 if health_codes or bilingual_review_required else 0,
                review_item_self=1 if is_proxy else 0,
                bilingual_context=projection_bilingual_context,
            )
        )

    for owner_id, segments in direct_segments_by_owner.items():
        if owner_id in folded_proxy_ids:
            continue
        for segment in segments:
            if segment.id not in review_direct_segment_ids:
                continue
            published_proxy = proxy_entries_by_stable_id.get(segment.id)
            segment_evidence = _deterministic_evidence_for_segment(
                deterministic_evidence_by_owner.get(owner_id, ()),
                segment=segment,
                owner_segment_count=len(segments),
            )
            if published_proxy is not None:
                segment_evidence = published_proxy.deterministic_rule_evidence
            segment_bilingual_context = bilingual_context_by_projection_id.get(
                segment.id
            )
            bilingual_review_required = bool(
                segment_bilingual_context is not None
                and segment_bilingual_context.mismatch_review_required
            )
            projections.append(
                _MutableProjection(
                    id=segment.id,
                    parent_id=owner_id,
                    kind="DIRECT_CONTENT",
                    source_order=segment.source_order,
                    source_offset=segment.source_offset,
                    label=_direct_content_label(segment.content_blocks),
                    summary=_direct_content_summary(segment.content_blocks),
                    structure_node_id=owner_id,
                    requirement_entry_id=(
                        published_proxy.requirement_entry_id
                        if published_proxy is not None
                        else None
                    ),
                    workflow_node_id=(
                        published_proxy.workflow_node_id
                        if published_proxy is not None
                        else None
                    ),
                    page_start=segment.page_start,
                    page_end=segment.page_end,
                    source_refs=segment.source_refs,
                    status=(
                        "NEEDS_ATTENTION"
                        if bilingual_review_required
                        else published_proxy.status
                        if published_proxy is not None
                        else "PENDING"
                    ),
                    classification=(
                        published_proxy.classification
                        if published_proxy is not None
                        else "NOT_CLASSIFIED"
                    ),
                    projection_role="DIRECT_CONTENT",
                    semantic_state="NOT_CLASSIFIED",
                    deterministic_rule_evidence=segment_evidence,
                    table_record_id=None,
                    table_evidence_binding_id=(
                        published_proxy.table_evidence_binding_id
                        if published_proxy is not None
                        else None
                    ),
                    include_in_view=True,
                    content_blocks=segment.content_blocks,
                    table_refs=segment.table_refs,
                    figure_refs=segment.figure_refs,
                    reviewed_self=(
                        1
                        if published_proxy is not None
                        and published_proxy.status == "REVIEWED"
                        and not bilingual_review_required
                        else 0
                    ),
                    attention_self=1 if bilingual_review_required else 0,
                    review_item_self=1,
                    bilingual_context=segment_bilingual_context,
                )
            )

    for table_projection in table_record_projections:
        if table_projection.mode is not TableRecordProjectionMode.WHOLE_TABLE:
            continue
        source_table_owner_id = table_projection.table_fact.owner_structure_node_id
        logical_table_id = table_projection.table_fact.logical_table_id
        table_owner_id = table_presentation_owner_by_logical_id.get(logical_table_id)
        if not table_owner_id:
            continue
        node_id = whole_table_review_id_by_logical_id[logical_table_id]
        published_table = proxy_entries_by_stable_id.get(node_id)
        table_evidence = whole_table_evidence_by_logical_id.get(
            table_projection.table_fact.logical_table_id
        )
        table_blocks = _table_blocks_for_projection(
            payload=payload,
            table_projection=table_projection,
        )
        table_structure = table_structure_by_logical_id.get(logical_table_id, {})
        table_source_order, table_source_offset = (
            _structure_physical_reading_key(table_structure)
            if table_structure
            else (
                min(
                    (
                        part.source_order
                        for part in table_projection.table_fact.table_parts
                    ),
                    default=0,
                ),
                0,
            )
        )
        table_pages = _content_page_numbers(table_blocks)
        heading_page_start = _positive_int(table_structure.get("page_start"))
        heading_page_end = _positive_int(table_structure.get("page_end"))
        missing_evidence = table_evidence is None or not table_blocks
        table_label = _whole_table_review_label(
            table_structure=table_structure,
            table_blocks=table_blocks,
            caption_blocks=table_caption_blocks_by_logical_id.get(
                logical_table_id,
                (),
            ),
            table_projection=table_projection,
            parent_structure=all_structure_by_id.get(table_owner_id, {}),
            table_ordinal=table_ordinal_by_logical_id.get(logical_table_id, 1),
        )
        projections.append(
            _MutableProjection(
                id=node_id,
                parent_id=table_owner_id,
                kind="TABLE",
                source_order=table_source_order,
                source_offset=table_source_offset,
                label=table_label,
                summary=table_label,
                structure_node_id=source_table_owner_id,
                requirement_entry_id=(
                    published_table.requirement_entry_id
                    if published_table is not None
                    else None
                ),
                workflow_node_id=(
                    published_table.workflow_node_id
                    if published_table is not None
                    else None
                ),
                page_start=min(
                    (
                        page
                        for page in (heading_page_start, *table_pages)
                        if page is not None
                    ),
                    default=None,
                ),
                page_end=max(
                    (
                        page
                        for page in (heading_page_end, *table_pages)
                        if page is not None
                    ),
                    default=None,
                ),
                source_refs=(
                    published_table.source_refs
                    if published_table is not None and published_table.source_refs
                    else tuple(
                        ref.model_dump(mode="json")
                        for ref in table_projection.table_fact.whole_table_source_refs
                    )
                ),
                status=(
                    published_table.status
                    if published_table is not None
                    else "NEEDS_ATTENTION"
                    if missing_evidence
                    else "PENDING"
                ),
                classification=(
                    published_table.classification
                    if published_table is not None
                    else "NOT_CLASSIFIED"
                ),
                projection_role="WHOLE_TABLE_REVIEW",
                semantic_state="NOT_CLASSIFIED",
                deterministic_rule_evidence=(),
                table_record_id=None,
                table_evidence_binding_id=(
                    published_table.table_evidence_binding_id
                    if published_table is not None
                    else table_evidence.binding_id
                    if table_evidence is not None
                    else None
                ),
                include_in_view=True,
                content_blocks=table_blocks,
                table_refs=table_blocks,
                figure_refs=_figure_refs(table_blocks),
                attention_self=(
                    1
                    if missing_evidence
                    or (
                        published_table is not None
                        and published_table.status == "NEEDS_ATTENTION"
                    )
                    else 0
                ),
                reviewed_self=(
                    1
                    if published_table is not None
                    and published_table.status == "REVIEWED"
                    else 0
                ),
                review_item_self=1,
            )
        )

    for table_projection in table_record_projections:
        if table_projection.mode is not TableRecordProjectionMode.RECORDS:
            continue
        source_table_owner_id = table_projection.table_fact.owner_structure_node_id
        logical_table_id = table_projection.table_fact.logical_table_id
        table_owner_id = table_presentation_owner_by_logical_id.get(logical_table_id)
        if not table_owner_id:
            continue
        owner_projection = next(
            projection for projection in projections if projection.id == table_owner_id
        )
        table_structure = table_structure_by_logical_id.get(logical_table_id, {})
        table_source_order, table_source_offset = (
            _structure_physical_reading_key(table_structure)
            if table_structure
            else (
                min(
                    (
                        part.source_order
                        for part in table_projection.table_fact.table_parts
                    ),
                    default=owner_projection.source_order,
                ),
                owner_projection.source_offset,
            )
        )
        table_blocks = _table_blocks_for_projection(
            payload=payload,
            table_projection=table_projection,
        )
        table_node_id = _whole_table_review_node_id(table_projection)
        table_label = _whole_table_review_label(
            table_structure=table_structure,
            table_blocks=table_blocks,
            caption_blocks=table_caption_blocks_by_logical_id.get(
                logical_table_id,
                (),
            ),
            table_projection=table_projection,
            parent_structure=all_structure_by_id.get(table_owner_id, {}),
            table_ordinal=table_ordinal_by_logical_id.get(logical_table_id, 1),
        )
        table_record_labels = _table_record_labels(
            table_projection.records,
            cells_by_id=table_cells_by_id,
        )
        table_pages = _content_page_numbers(table_blocks)
        projections.append(
            _MutableProjection(
                id=table_node_id,
                parent_id=table_owner_id,
                kind="TABLE",
                source_order=table_source_order,
                source_offset=table_source_offset,
                label=table_label,
                summary=table_label,
                structure_node_id=source_table_owner_id,
                requirement_entry_id=None,
                workflow_node_id=None,
                page_start=min(table_pages, default=owner_projection.page_start),
                page_end=max(table_pages, default=owner_projection.page_end),
                source_refs=tuple(
                    ref.model_dump(mode="json")
                    for ref in table_projection.table_fact.whole_table_source_refs
                ),
                status="PENDING",
                classification="NOT_CLASSIFIED",
                projection_role="TABLE_CONTAINER",
                semantic_state="NOT_CLASSIFIED",
                deterministic_rule_evidence=(),
                table_record_id=None,
                table_evidence_binding_id=None,
                include_in_view=True,
                content_blocks=table_blocks,
                table_refs=table_blocks,
                figure_refs=_figure_refs(table_blocks),
                review_item_self=0,
            )
        )
        for record in table_projection.records:
            table_record_suborder_by_id[record.record_id] = record.source_order
            structure_node_id = _table_record_structure_node_id(
                record,
                all_structure_by_id=all_structure_by_id,
            )
            published_record = proxy_entries_by_record.get(record.record_id)
            record_evidence = table_evidence_by_record_id.get(record.record_id)
            deterministic_evidence = deterministic_evidence_by_owner.get(
                structure_node_id,
                (),
            )
            if published_record is not None:
                deterministic_evidence = published_record.deterministic_rule_evidence
            record_block = {
                "kind": "table_record",
                "projection_id": table_projection.projection_id,
                "projection_sha256": table_projection.projection_sha256,
                "record_id": record.record_id,
                "logical_table_id": record.logical_table_id,
                "row_bindings": [
                    item.model_dump(mode="json") for item in record.row_bindings
                ],
                "column_bindings": [
                    item.model_dump(mode="json") for item in record.column_bindings
                ],
                "header_bindings": [
                    item.model_dump(mode="json") for item in record.header_bindings
                ],
                "shared_merged_values": [
                    item.model_dump(mode="json") for item in record.shared_merged_values
                ],
                "notes": [item.model_dump(mode="json") for item in record.notes],
                "image_refs": [
                    item.model_dump(mode="json") for item in record.image_refs
                ],
                "content_sha256": record.content_sha256,
                "record_source_order": record.source_order,
            }
            table_reference = {
                "kind": "table_reference",
                "logical_table_id": record.logical_table_id,
                "projection_id": table_projection.projection_id,
                "projection_sha256": table_projection.projection_sha256,
                "table_ids": list(record.table_ids),
                "record_id": record.record_id,
            }
            record_pages = _table_record_primary_pages(
                record,
                cells_by_id=table_cells_by_id,
            ) or _content_page_numbers(table_blocks)
            projections.append(
                _MutableProjection(
                    id=record.record_id,
                    parent_id=table_node_id,
                    kind="TABLE_RECORD",
                    # Record order is local to its logical table.  Anchor the
                    # public tree order at the table's physical reading
                    # position, then use the local row order only as a stable
                    # tie-breaker below.  Treating 0..N as document-global put
                    # late tables near the document root.
                    source_order=table_source_order,
                    source_offset=table_source_offset,
                    label=table_record_labels[record.record_id],
                    summary=_table_record_summary(
                        record,
                        cells_by_id=table_cells_by_id,
                    ),
                    structure_node_id=structure_node_id,
                    requirement_entry_id=(
                        published_record.requirement_entry_id
                        if published_record is not None
                        else None
                    ),
                    workflow_node_id=(
                        published_record.workflow_node_id
                        if published_record is not None
                        else None
                    ),
                    page_start=min(record_pages, default=owner_projection.page_start),
                    page_end=max(record_pages, default=owner_projection.page_end),
                    source_refs=tuple(
                        item.model_dump(mode="json") for item in record.source_refs
                    ),
                    status=(
                        published_record.status
                        if published_record is not None
                        else "PENDING"
                    ),
                    classification="NOT_CLASSIFIED",
                    projection_role="TABLE_RECORD",
                    semantic_state="NOT_CLASSIFIED",
                    deterministic_rule_evidence=deterministic_evidence,
                    table_record_id=record.record_id,
                    table_evidence_binding_id=(
                        published_record.table_evidence_binding_id
                        if published_record is not None
                        else record_evidence.binding_id
                        if record_evidence is not None
                        else None
                    ),
                    include_in_view=True,
                    # Keep the record-local semantic slice on the node.  The
                    # complete immutable table lives once in the projection
                    # and is loaded through the Matrix table detail endpoint.
                    # Duplicating it into every record made 500-row tables grow
                    # quadratically in snapshot and response size.
                    content_blocks=(record_block,),
                    table_refs=(table_reference,),
                    figure_refs=tuple(
                        item.model_dump(mode="json")
                        for item in (*record.figure_refs, *record.image_refs)
                    ),
                    attention_self=0,
                    reviewed_self=(
                        1
                        if published_record is not None
                        and published_record.status == "REVIEWED"
                        else 0
                    ),
                    review_item_self=1,
                )
            )

    for index, entry in enumerate(normalized_entries):
        assert entry is not None
        owner_id = entry.structure_node_id or ""
        parent_id = _nearest_visible_owner(
            owner_id,
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        if not parent_id and mode is RequirementTreeMode.REQUIREMENT_SET:
            raise ValueError(
                "published requirement entries must bind an Effective Structure owner"
            )
        if not parent_id:
            # Never invent a hierarchy from section labels or flat requirement
            # order. An unbound leaf is represented as an explicit attention root.
            parent_id = None
        inherited_table_refs = entry.table_refs or _table_refs_for_owner(
            owner_id=owner_id,
            all_nodes=all_structure_by_id,
            payload=payload,
        )
        content_blocks = entry.content_blocks
        if inherited_table_refs:
            if not content_blocks and entry.summary:
                content_blocks = ({"kind": "paragraph", "text": entry.summary},)
            existing_table_ids = {
                str(block.get("table_id") or block.get("id") or "")
                for block in content_blocks
                if str(block.get("kind") or "").lower() == "table"
            }
            content_blocks = (
                *content_blocks,
                *(
                    table_ref
                    for table_ref in inherited_table_refs
                    if str(table_ref.get("table_id") or table_ref.get("id") or "")
                    not in existing_table_ids
                ),
            )
        leaf_id = _requirement_tree_node_id(entry)
        projections.append(
            _MutableProjection(
                id=leaf_id,
                parent_id=parent_id,
                kind="REQUIREMENT",
                source_order=max(entry.source_order, index),
                source_offset=entry.source_offset,
                label=entry.label,
                summary=entry.summary,
                structure_node_id=entry.structure_node_id,
                requirement_entry_id=entry.requirement_entry_id,
                workflow_node_id=entry.workflow_node_id,
                page_start=entry.page_start,
                page_end=entry.page_end,
                source_refs=entry.source_refs,
                status=entry.status,
                classification=entry.classification,
                projection_role="REQUIREMENT",
                semantic_state=(
                    "CONFIRMED" if entry.status == "REVIEWED" else "PROPOSED"
                ),
                deterministic_rule_evidence=(),
                table_record_id=None,
                table_evidence_binding_id=entry.table_evidence_binding_id,
                include_in_view=entry.include_in_view,
                content_blocks=content_blocks,
                table_refs=inherited_table_refs,
                figure_refs=entry.figure_refs,
                attention_self=1 if entry.status == "NEEDS_ATTENTION" else 0,
                reviewed_self=1 if entry.status == "REVIEWED" else 0,
                review_item_self=1,
            )
        )

    content_blocks_by_projection_id: dict[str, tuple[dict[str, Any], ...]] = {}
    reading_key_by_projection_id: dict[str, tuple[int, int]] = {}
    for projection in projections:
        normalized_content_blocks = _bind_figure_captions_in_blocks(
            _content_blocks_with_source_order(
                projection.content_blocks,
                all_structure_by_id=all_structure_by_id,
                fallback_source_order=projection.source_order,
                event_scope_id=projection.id,
                sort_physical=projection.projection_role == "STRUCTURE_PROXY",
            ),
            bindings_by_media_id=figure_caption_bindings,
        )
        content_blocks_by_projection_id[projection.id] = normalized_content_blocks
        reading_key_by_projection_id[projection.id] = _projection_reading_key(
            projection=projection,
            content_blocks=normalized_content_blocks,
            all_structure_by_id=all_structure_by_id,
        )

    def projection_order(
        item: _MutableProjection,
    ) -> tuple[int, int, int, int, str]:
        kind_priority = (
            2 if item.kind == "REQUIREMENT" else 1 if item.kind == "TABLE_RECORD" else 0
        )
        reading_source_order, reading_source_offset = reading_key_by_projection_id[
            item.id
        ]
        return (
            reading_source_order,
            reading_source_offset,
            kind_priority,
            table_record_suborder_by_id.get(item.id, 0),
            item.id,
        )

    projections.sort(key=projection_order)
    projection_by_id = {item.id: item for item in projections}
    parent_by_id = {item.id: item.parent_id for item in projections}
    order_by_id = {item.id: projection_order(item) for item in projections}
    children_by_parent: dict[str | None, list[str]] = defaultdict(list)
    for item in projections:
        children_by_parent[item.parent_id].append(item.id)
    for child_ids in children_by_parent.values():
        child_ids.sort(key=lambda node_id: order_by_id[node_id])

    depth_by_id = _depths(parent_by_id)
    aggregate_by_id: dict[str, tuple[int, int, int, int]] = {}

    def aggregate(node_id: str, active: set[str]) -> tuple[int, int, int, int]:
        if node_id in aggregate_by_id:
            return aggregate_by_id[node_id]
        if node_id in active:
            raise ValueError("requirement tree contains a parent cycle")
        active.add(node_id)
        projection = projection_by_id[node_id]
        requirement_count = 1 if projection.kind == "REQUIREMENT" else 0
        attention_count = projection.attention_self
        reviewed_count = projection.reviewed_self
        review_item_count = projection.review_item_self
        for child_id in children_by_parent.get(node_id, ()):
            child_counts = aggregate(child_id, active)
            requirement_count += child_counts[0]
            attention_count += child_counts[1]
            reviewed_count += child_counts[2]
            review_item_count += child_counts[3]
        active.remove(node_id)
        aggregate_by_id[node_id] = (
            requirement_count,
            attention_count,
            reviewed_count,
            review_item_count,
        )
        return aggregate_by_id[node_id]

    nodes: list[RequirementTreeNodeV1] = []
    for projection in projections:
        counts = aggregate(projection.id, set())
        content_blocks = content_blocks_by_projection_id[projection.id]
        editable_structure_node_ids = (
            _ordered_editable_structure_node_ids(
                content_blocks,
                all_structure_by_id=all_structure_by_id,
                owner_structure_node_id=projection.structure_node_id,
            )
            if projection.projection_role == "DIRECT_CONTENT"
            else ()
        )
        normalized_table_refs = _content_blocks_with_source_order(
            projection.table_refs,
            all_structure_by_id=all_structure_by_id,
            fallback_source_order=projection.source_order,
            event_scope_id=f"{projection.id}:table",
        )
        normalized_figure_refs = _bind_figure_captions_in_blocks(
            _content_blocks_with_source_order(
                projection.figure_refs,
                all_structure_by_id=all_structure_by_id,
                fallback_source_order=projection.source_order,
                event_scope_id=f"{projection.id}:figure",
            ),
            bindings_by_media_id=figure_caption_bindings,
        )
        reading_source_order, reading_source_offset = reading_key_by_projection_id[
            projection.id
        ]
        summary = projection.summary
        if projection.projection_role == "STRUCTURE_PROXY":
            summary = (
                _direct_content_summary(content_blocks)
                if projection.bilingual_context is not None
                else _first_non_blank(
                    _reading_stream_text(content_blocks),
                    projection.summary,
                    projection.label,
                )
            )
        nodes.append(
            RequirementTreeNodeV1(
                id=projection.id,
                parent_id=projection.parent_id,
                kind=projection.kind,
                depth=depth_by_id[projection.id],
                source_order=reading_source_order,
                source_offset=reading_source_offset,
                label=projection.label,
                summary=summary,
                child_ids=tuple(children_by_parent.get(projection.id, ())),
                structure_node_id=projection.structure_node_id,
                requirement_entry_id=projection.requirement_entry_id,
                workflow_node_id=projection.workflow_node_id,
                page_start=projection.page_start,
                page_end=projection.page_end,
                source_refs=projection.source_refs,
                requirement_count=counts[0],
                attention_count=counts[1],
                reviewed_count=counts[2],
                status=projection.status,
                classification=projection.classification,
                projection_role=projection.projection_role,
                semantic_state=projection.semantic_state,
                deterministic_rule_evidence=(projection.deterministic_rule_evidence),
                table_record_id=projection.table_record_id,
                table_evidence_binding_id=(projection.table_evidence_binding_id),
                review_item_count=counts[3],
                include_in_view=projection.include_in_view,
                content_blocks=content_blocks,
                editable_structure_node_ids=editable_structure_node_ids,
                toc_entries=_normalized_toc_entries(
                    projection.toc_entries,
                    fallback_source_order=projection.source_order,
                    valid_target_ids=set(projection_by_id),
                ),
                table_refs=tuple(
                    _dedupe_dicts(
                        (*_table_refs(content_blocks), *normalized_table_refs)
                    )
                ),
                figure_refs=tuple(
                    _dedupe_dicts(
                        (*_figure_refs(content_blocks), *normalized_figure_refs)
                    )
                ),
                bilingual_context=projection.bilingual_context,
            )
        )

    state = forced_state or _tree_state(
        agent=agent,
        payload=payload,
        node_count=len(nodes),
        substantive_node_count=sum(
            projection.kind != "DOCUMENT" for projection in projections
        ),
        requirement_count=len(normalized_entries),
        source_capture_gap_count=(
            _ready_structure_inspection_capture_gap_count(workbench)
        ),
    )
    if state is RequirementTreeState.READY and not nodes:
        state = RequirementTreeState.NOT_STARTED
    roots = tuple(children_by_parent.get(None, ()))
    source_identity = RequirementTreeSourceIdentity(
        project_id=str(source.get("project_id") or workbench.get("project_id") or ""),
        document_id=str(
            source.get("document_id") or workbench.get("document_id") or ""
        ),
        source_ir_id=str(source.get("source_ir_id") or ""),
        source_sha256=str(source.get("source_sha256") or ""),
        preview_generation_id=str(
            source.get("preview_generation_id")
            or payload.get("preview_generation_id")
            or ""
        ),
        requirement_set_id=requirement_set_id,
        geometry_index_id=str(geometry_identity.get("geometry_index_id") or ""),
        geometry_index_sha256=str(geometry_identity.get("geometry_index_sha256") or ""),
    )
    structure_identity = RequirementTreeStructureIdentity(
        base_snapshot_id=str(structure.get("base_snapshot_id") or ""),
        effective_snapshot_id=str(structure.get("effective_snapshot_id") or ""),
        effective_tree_sha256=str(structure.get("effective_tree_sha256") or ""),
        revision=max(int(structure.get("revision") or 0), 0),
    )
    requirement_evidence_bundle: RequirementEvidenceBundleV1 | None = None
    if mode is RequirementTreeMode.REQUIREMENT_SET:
        requirement_evidence_bundle = _build_requirement_evidence_bundle(
            nodes=tuple(nodes),
            raw_requirement_entries=entry_source,
            source_identity=source_identity,
            structure_identity=structure_identity,
            table_evidence_bindings=table_evidence_bindings,
        )
        evidence_by_requirement_entry_id = {
            binding.requirement_entry_id: binding
            for binding in requirement_evidence_bundle.requirement_bindings
        }
        nodes = [
            node.model_copy(
                update={
                    "requirement_evidence_binding_id": (
                        evidence_by_requirement_entry_id[
                            node.requirement_entry_id
                        ].binding_id
                        if node.requirement_entry_id in evidence_by_requirement_entry_id
                        else None
                    )
                }
            )
            for node in nodes
        ]
    return RequirementTreeSnapshotV1(
        source_identity=source_identity,
        structure_identity=structure_identity,
        mode=mode,
        state=state,
        root_node_ids=roots,
        nodes=tuple(nodes),
        table_record_projections=table_record_projections,
        table_evidence_bindings=table_evidence_bindings,
        requirement_evidence_bundle=requirement_evidence_bundle,
    )


def _build_requirement_evidence_bundle(
    *,
    nodes: tuple[RequirementTreeNodeV1, ...],
    raw_requirement_entries: Iterable[dict[str, Any]],
    source_identity: RequirementTreeSourceIdentity,
    structure_identity: RequirementTreeStructureIdentity,
    table_evidence_bindings: tuple[TableEvidenceBindingV1, ...],
) -> RequirementEvidenceBundleV1:
    """Freeze one evidence binding for every persisted review item.

    This runs only after publication has assigned database requirement IDs.
    The resulting bundle contains pointers and hashes, never copied source
    text, and is embedded in the immutable Requirement Set tree object.
    """

    if not source_identity.source_ir_id or not source_identity.source_sha256:
        raise ValueError(
            "Requirement Set evidence requires immutable SourceIR identity"
        )
    if not structure_identity.effective_snapshot_id:
        raise ValueError(
            "Requirement Set evidence requires an Effective Structure snapshot"
        )
    raw_by_requirement_entry_id: dict[str, dict[str, Any]] = {}
    for item in raw_requirement_entries:
        if not isinstance(item, dict):
            continue
        requirement_entry_id = str(
            item.get("requirement_entry_id")
            or item.get("requirement_id")
            or item.get("database_requirement_id")
            or ""
        )
        if not requirement_entry_id:
            continue
        if requirement_entry_id in raw_by_requirement_entry_id:
            raise ValueError("published requirement entry IDs must be unique")
        raw_by_requirement_entry_id[requirement_entry_id] = item

    table_binding_ids = {binding.binding_id for binding in table_evidence_bindings}
    requirement_bindings: list[RequirementEvidenceBindingV1] = []
    for node in nodes:
        if (
            not node.include_in_view
            or node.projection_role not in _REVIEWABLE_PROJECTION_ROLES
        ):
            continue
        requirement_entry_id = node.requirement_entry_id
        if not requirement_entry_id:
            raise ValueError("Requirement Set review items require database entry IDs")
        raw_entry = raw_by_requirement_entry_id.get(
            requirement_entry_id,
            {},
        )
        source_spans = _validated_llm_source_spans(
            _dict_items(raw_entry.get("source_spans"))
        )
        source_refs = _requirement_evidence_source_refs(node)
        table_evidence_binding_id = node.table_evidence_binding_id
        if (
            node.projection_role in {"WHOLE_TABLE_REVIEW", "TABLE_RECORD"}
            and table_evidence_binding_id is None
        ):
            raise ValueError(
                "published table review items require table evidence bindings"
            )
        if (
            table_evidence_binding_id is not None
            and table_evidence_binding_id not in table_binding_ids
        ):
            raise ValueError("published review item points to unknown table evidence")
        atomic_requirement_id = (
            str(
                raw_entry.get("atomic_requirement_id")
                or raw_entry.get("atomic_node_id")
                or ""
            )
            or None
        )
        requirement_bindings.append(
            RequirementEvidenceBindingV1.create(
                requirement_entry_id=requirement_entry_id,
                atomic_requirement_id=atomic_requirement_id,
                source_spans=source_spans,
                source_refs=source_refs,
                table_evidence_binding_id=table_evidence_binding_id,
            )
        )

    return RequirementEvidenceBundleV1.create(
        source_ir_id=source_identity.source_ir_id,
        source_sha256=source_identity.source_sha256,
        structure_snapshot_id=structure_identity.effective_snapshot_id,
        table_bindings=table_evidence_bindings,
        requirement_bindings=tuple(requirement_bindings),
    )


def _validated_llm_source_spans(
    items: Iterable[dict[str, Any]],
) -> tuple[LlmReviewSourceSpan, ...]:
    spans: list[LlmReviewSourceSpan] = []
    for item in items:
        try:
            spans.append(LlmReviewSourceSpan.model_validate(item))
        except ValueError:
            # Early v1 callers used the same range shape before stable
            # semantic-unit IDs were enforced. The normalized source-ref
            # channel below still freezes those exact ranges.
            continue
    return tuple(spans)


def _requirement_evidence_source_refs(
    node: RequirementTreeNodeV1,
) -> tuple[RequirementEvidenceSourceRefV1, ...]:
    candidates: list[dict[str, Any]] = [dict(item) for item in node.source_refs]
    for evidence in node.deterministic_rule_evidence:
        candidates.extend(_dict_items(evidence.get("source_refs")))
    if not candidates:
        candidates.extend(
            block
            for block in node.content_blocks
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
            item.get("page_start") or item.get("page_number") or node.page_start
        )
        page_end = _positive_int(
            item.get("page_end")
            or item.get("page_number")
            or node.page_end
            or page_start
        )
        if page_start is None:
            page_end = None
        ref = RequirementEvidenceSourceRefV1(
            kind=str(item.get("kind") or "SOURCE_REF").strip(),
            source_id=source_id,
            owner_structure_node_id=str(
                item.get("owner_structure_node_id") or node.structure_node_id or ""
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
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return tuple(refs)


@dataclass(frozen=True, slots=True)
class _RequirementEntry:
    stable_id: str
    requirement_entry_id: str | None
    workflow_node_id: str | None
    structure_node_id: str | None
    owner_kind: str
    source_order: int
    source_offset: int
    label: str
    summary: str
    page_start: int | None
    page_end: int | None
    source_refs: tuple[dict[str, Any], ...]
    status: str
    classification: str
    projection_role: str
    table_record_id: str | None
    table_evidence_binding_id: str | None
    deterministic_rule_evidence: tuple[dict[str, Any], ...]
    include_in_view: bool
    content_blocks: tuple[dict[str, Any], ...]
    table_refs: tuple[dict[str, Any], ...]
    figure_refs: tuple[dict[str, Any], ...]


def _working_requirement_entries(
    *,
    workbench: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    result = _dict(workbench.get("result"))
    preview = _dict(result.get("publication_preview"))
    if _dict_items(preview.get("entries")):
        return _dict_items(preview.get("entries"))
    atomization = _dict(result.get("atomization_run"))
    if _dict_items(atomization.get("nodes")):
        return _dict_items(atomization.get("nodes"))
    if _dict_items(result.get("review_nodes")):
        return _dict_items(result.get("review_nodes"))
    return _dict_items(payload.get("items")) or _dict_items(
        workbench.get("requirements")
    )


def _uses_local_structure_projection(
    *,
    workbench: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    """Return true only for the formal no-LLM structure-first result.

    Deterministic semantic output is deliberately retained in ``payload`` as
    review evidence.  It must not manufacture requirement leaves before a
    model or person has made the semantic decision.
    """

    if str(payload.get("mode") or "").upper() != "LOCAL_STRUCTURE_PROJECTION":
        return False
    result = _dict(workbench.get("result"))
    return not any(
        _dict_items(_dict(result.get(name)).get("nodes"))
        for name in ("atomization_run",)
    ) and not _dict_items(result.get("review_nodes"))


def _navigation_only_duplicate_scope_ids(
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
    protected_structure_ids: set[str],
) -> set[str]:
    """Flatten only a region namespace with no independent heading fact.

    A document-region wrapper may share the first root Section's immutable
    source fact so numbering can restart inside that region. The wrapper stays
    authoritative in Structure, but showing both nodes creates duplicate
    navigation labels. It is safe to flatten only when every direct child is a
    Section, the first child starts at offset zero in the same source fact, and
    the wrapper has no span, health signal, requirement, or distinct label.
    Standalone region headings therefore remain visible.
    """

    children_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in all_structure_by_id.values():
        parent_id = str(item.get("parent_id") or "")
        if parent_id:
            children_by_parent[parent_id].append(item)

    hidden: set[str] = set()
    for node_id in visible_structure_ids:
        item = all_structure_by_id[node_id]
        if (
            node_id in protected_structure_ids
            or str(item.get("kind") or "").upper() != "SECTION"
            or str(item.get("explicit_label") or "").strip()
            or str(item.get("boundary_basis") or "").upper() != "NATIVE_BLOCK"
            or _dict_items(item.get("source_spans"))
            or _list(item.get("health_codes"))
        ):
            continue
        direct_children = children_by_parent.get(node_id, ())
        if not direct_children or any(
            str(child.get("kind") or "").upper() != "SECTION"
            for child in direct_children
        ):
            continue
        first_child = min(
            direct_children,
            key=lambda child: (
                _nonnegative_int(child.get("order")),
                str(child.get("id") or ""),
            ),
        )
        if not str(first_child.get("explicit_label") or "").strip():
            continue
        wrapper_bindings = _structure_source_bindings(item)
        child_bindings = _structure_source_bindings(first_child)
        if (
            len(wrapper_bindings) != 1
            or not child_bindings
            or wrapper_bindings[0][0] != child_bindings[0][0]
            or wrapper_bindings[0][1:] != (None, None)
            or child_bindings[0][1] != 0
        ):
            continue
        wrapper_label = " ".join(str(item.get("label") or "").split())
        child_label = " ".join(str(first_child.get("label") or "").split())
        if wrapper_label and wrapper_label != child_label:
            continue
        hidden.add(node_id)
    return hidden


def _deterministic_rule_evidence_by_owner(
    payload: dict[str, Any],
) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in _dict_items(payload.get("items")):
        owner_id = _presentation_owner_id(item)
        if not owner_id:
            continue
        grouped[owner_id].append(
            {
                "schema_version": "deterministic-rule-evidence/v1",
                "item_id": str(item.get("id") or ""),
                "semantic_class": str(
                    item.get("semantic_class") or "UNRESOLVED"
                ).upper(),
                "requirement_subtype": str(
                    item.get("requirement_subtype") or ""
                ).upper(),
                "status": str(item.get("status") or "").upper(),
                "reason_codes": tuple(
                    sorted(
                        {
                            str(code).strip().lower()
                            for code in _list(item.get("reason_codes"))
                            if str(code).strip()
                        }
                    )
                ),
                "source_refs": tuple(_dict_items(item.get("source_refs"))),
                "authority": "SUGGESTION_ONLY",
            }
        )
    return {
        owner_id: tuple(
            sorted(
                evidence,
                key=lambda item: (
                    str(item.get("item_id") or ""),
                    str(item.get("semantic_class") or ""),
                ),
            )
        )
        for owner_id, evidence in grouped.items()
    }


def _visible_children_by_owner(
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
) -> dict[str, tuple[str, ...]]:
    children: dict[str, list[str]] = defaultdict(list)
    for node_id in visible_structure_ids:
        parent_id = _nearest_visible_parent(
            str(all_structure_by_id[node_id].get("parent_id") or ""),
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        if parent_id:
            children[parent_id].append(node_id)
    return {
        owner_id: tuple(
            sorted(
                node_ids,
                key=lambda child_id: (
                    _nonnegative_int(all_structure_by_id[child_id].get("order")),
                    child_id,
                ),
            )
        )
        for owner_id, node_ids in children.items()
    }


def _direct_source_owner_by_id(
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
) -> dict[str, str]:
    owners: dict[str, str] = {}
    ambiguous: set[str] = set()
    for node in all_structure_by_id.values():
        if str(node.get("kind") or "").upper() not in _SOURCE_CONTENT_KINDS:
            continue
        owner_id = _nearest_visible_owner(
            str(node.get("parent_id") or ""),
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        if not owner_id:
            continue
        for source_id, _, _ in _structure_source_bindings(node):
            existing = owners.get(source_id)
            if existing is not None and existing != owner_id:
                ambiguous.add(source_id)
                continue
            owners[source_id] = owner_id
    for source_id in ambiguous:
        owners.pop(source_id, None)
    return owners


def _structure_presentation_content(
    *,
    node_id: str,
    blocks: tuple[dict[str, Any], ...],
    direct_source_owner_by_id: dict[str, str],
    all_structure_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Keep full subtree context while excluding proven foreign ownership."""

    retained: list[dict[str, Any]] = []
    for block in blocks:
        source_id = str(block.get("source_id") or block.get("source_node_id") or "")
        direct_owner_id = direct_source_owner_by_id.get(source_id)
        if (
            direct_owner_id
            and direct_owner_id != node_id
            and not _is_structure_descendant(
                direct_owner_id,
                ancestor_id=node_id,
                all_structure_by_id=all_structure_by_id,
            )
        ):
            continue
        retained.append(block)
    return tuple(retained)


_TABLE_CAPTION_PATTERN = re.compile(
    r"^\s*(?:表|table)\s*(?:[A-Z]{0,3}[.\-\s]*\d+(?:[.\-\s]+\d+)*|[IVXLCDM]+|[A-Z])(?P<body>.*)$",
    re.IGNORECASE,
)
_FIGURE_CAPTION_PATTERN = re.compile(
    r"^\s*(?:图|figure|fig\.)\s*(?:[A-Z]{0,3}[.\-\s]*\d+(?:[.\-\s]+\d+)*|[IVXLCDM]+|[A-Z])(?P<body>.*)$",
    re.IGNORECASE,
)

_ENGLISH_NORMATIVE_STATEMENT_PATTERN = re.compile(
    r"\b(?:shall|must|should|will|(?:is|are)\s+required\s+to|needs?\s+to)\b",
    re.IGNORECASE,
)
_CHINESE_NORMATIVE_STATEMENT_PATTERN = re.compile(
    r"(?:必须|不得|严禁|不允许|须(?:由|按|在|为|经|向|将|提供|提交|满足|符合|采用|执行)|"
    r"应(?:由|当|按|在|为|向|将|能|可|符合|满足|具有|提供|提交|保持|采用|执行|达到|确保|包括|不))"
)


def _is_table_caption_text(text: str) -> bool:
    """Return true only for a label-like table caption, never an obligation.

    Standards sometimes begin a real normative sentence with ``Table 1`` or
    ``表 1``.  The numeric prefix alone is therefore insufficient evidence for
    suppressing a review candidate.  A modal predicate keeps the text as
    independently reviewable prose.
    """

    match = _TABLE_CAPTION_PATTERN.match(" ".join(text.split()))
    if match is None:
        return False
    body = str(match.group("body") or "").strip()
    if not body:
        return True
    return not (
        _ENGLISH_NORMATIVE_STATEMENT_PATTERN.search(body)
        or _CHINESE_NORMATIVE_STATEMENT_PATTERN.search(body)
    )


def _is_figure_caption_text(text: str) -> bool:
    """Return true for a label-like figure caption, not normative prose."""

    match = _FIGURE_CAPTION_PATTERN.match(" ".join(text.split()))
    if match is None:
        return False
    body = str(match.group("body") or "").strip()
    if not body:
        return True
    return not (
        _ENGLISH_NORMATIVE_STATEMENT_PATTERN.search(body)
        or _CHINESE_NORMATIVE_STATEMENT_PATTERN.search(body)
    )


def _table_caption_markers(
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
    table_record_projections: tuple[TableRecordProjectionV1, ...],
    table_ordinal_by_logical_id: dict[str, int],
) -> tuple[set[str], dict[str, tuple[dict[str, Any], ...]]]:
    """Bind an adjacent standards-style caption to its proven table.

    The association requires both an immutable table source binding and an
    immediately preceding sibling-like CONTENT node whose text has a generic
    Table/表 caption grammar.  Prose such as "requirements are shown below"
    therefore remains independently reviewable, while captions and continued
    captions stop masquerading as requirements.
    """

    logical_ids_by_table_source_id: dict[str, set[str]] = defaultdict(set)
    projection_logical_ids = {
        projection.table_fact.logical_table_id
        for projection in table_record_projections
    }
    for projection in table_record_projections:
        logical_id = projection.table_fact.logical_table_id
        for part in projection.table_fact.table_parts:
            for ref in part.source_refs:
                if ref.source_id:
                    logical_ids_by_table_source_id[ref.source_id].add(logical_id)

    logical_id_by_table_node_id: dict[str, str] = {}
    for node_id, node in all_structure_by_id.items():
        if str(node.get("kind") or "").upper() != "TABLE":
            continue
        candidates = {
            logical_id
            for source_id, _, _ in _structure_source_bindings(node)
            for logical_id in logical_ids_by_table_source_id.get(source_id, ())
        }
        if node_id in projection_logical_ids:
            candidates.add(node_id)
        if len(candidates) == 1:
            logical_id_by_table_node_id[node_id] = next(iter(candidates))

    ordered_nodes = sorted(
        all_structure_by_id.values(),
        key=lambda item: (
            *_structure_physical_reading_key(item),
            str(item.get("id") or ""),
        ),
    )
    table_nodes = tuple(
        node for node in ordered_nodes if str(node.get("kind") or "").upper() == "TABLE"
    )
    caption_nodes_by_id = {
        str(node.get("id") or ""): node
        for node in ordered_nodes
        if str(node.get("kind") or "").upper() == "CONTENT"
        and _is_table_caption_text(str(node.get("label") or ""))
        and _source_bound_structure_content_block(node) is not None
    }
    owner_by_node_id = {
        str(node.get("id") or ""): _nearest_visible_owner(
            str(node.get("parent_id") or ""),
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        for node in (*table_nodes, *caption_nodes_by_id.values())
    }
    candidate_pairs: set[tuple[str, str]] = set()

    # Preserve the established adjacency contract, but make it one-to-one and
    # reject contradictory page evidence.  A source-bound caption immediately
    # before a TABLE is already a sufficiently local association even when a
    # standalone fixture does not contain every preceding numbered table.
    for index, table_node in enumerate(ordered_nodes):
        table_node_id = str(table_node.get("id") or "")
        logical_id = logical_id_by_table_node_id.get(table_node_id)
        if not logical_id or index == 0:
            continue
        candidate = ordered_nodes[index - 1]
        candidate_id = str(candidate.get("id") or "")
        if candidate_id not in caption_nodes_by_id:
            continue
        table_owner = owner_by_node_id.get(table_node_id)
        caption_owner = owner_by_node_id.get(candidate_id)
        if not table_owner or caption_owner != table_owner:
            continue
        if not _structure_pages_are_compatible(candidate, table_node):
            continue
        candidate_pairs.add((candidate_id, table_node_id))

    # Native PDF adapters can place TABLE_ROW children at the same physical
    # key as their TABLE parent.  Their stable IDs may sort between an otherwise
    # adjacent caption and table, so raw list adjacency is not a trustworthy
    # boundary.  Recover only a source-local, uniquely numbered association:
    # the caption and the first following physical table must share an owner
    # and page, and the explicit simple table number must agree with the stable
    # document-order table ordinal.  No document-wide text search is involved.
    for candidate_id, candidate in caption_nodes_by_id.items():
        caption_number = _simple_table_caption_number(str(candidate.get("label") or ""))
        caption_owner = owner_by_node_id.get(candidate_id)
        caption_pages = _structure_page_numbers(candidate)
        if caption_number is None or not caption_owner or not caption_pages:
            continue
        caption_key = _structure_physical_reading_key(candidate)
        following_tables = [
            table_node
            for table_node in table_nodes
            if owner_by_node_id.get(str(table_node.get("id") or "")) == caption_owner
            and _structure_physical_reading_key(table_node) > caption_key
            and bool(caption_pages & _structure_page_numbers(table_node))
        ]
        if not following_tables:
            continue
        first_key = min(
            _structure_physical_reading_key(table_node)
            for table_node in following_tables
        )
        first_tables = [
            table_node
            for table_node in following_tables
            if _structure_physical_reading_key(table_node) == first_key
        ]
        if len(first_tables) != 1:
            continue
        table_node = first_tables[0]
        table_node_id = str(table_node.get("id") or "")
        logical_id = logical_id_by_table_node_id.get(table_node_id)
        if (
            not logical_id
            or table_ordinal_by_logical_id.get(logical_id) != caption_number
        ):
            continue
        candidate_pairs.add((candidate_id, table_node_id))

    table_ids_by_caption_id: dict[str, set[str]] = defaultdict(set)
    caption_ids_by_table_id: dict[str, set[str]] = defaultdict(set)
    for candidate_id, table_node_id in candidate_pairs:
        table_ids_by_caption_id[candidate_id].add(table_node_id)
        caption_ids_by_table_id[table_node_id].add(candidate_id)

    caption_ids: set[str] = set()
    captions_by_logical_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate_id, table_node_id in sorted(candidate_pairs):
        if (
            len(table_ids_by_caption_id[candidate_id]) != 1
            or len(caption_ids_by_table_id[table_node_id]) != 1
        ):
            continue
        logical_id = logical_id_by_table_node_id[table_node_id]
        candidate = caption_nodes_by_id[candidate_id]
        caption_block = _source_bound_structure_content_block(candidate)
        if caption_block is None:
            continue
        caption_source_order, caption_source_offset = _structure_physical_reading_key(
            candidate
        )
        caption_block = {
            **caption_block,
            "source_order": caption_source_order,
            "source_offset": caption_source_offset,
        }
        caption_ids.add(candidate_id)
        captions_by_logical_id[logical_id].append(caption_block)

    return caption_ids, {
        logical_id: tuple(
            sorted(
                _dedupe_dicts(blocks),
                key=lambda block: (
                    _nonnegative_int(block.get("source_order")),
                    _nonnegative_int(block.get("source_offset")),
                    str(block.get("source_id") or ""),
                ),
            )
        )
        for logical_id, blocks in captions_by_logical_id.items()
    }


_SIMPLE_TABLE_CAPTION_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:表|table)\s*(?P<number>\d+)"
    r"(?!\d|\s*[.．\-]\s*\d)",
    re.IGNORECASE,
)


def _simple_table_caption_number(text: str) -> int | None:
    """Return one consistent simple decimal number from a pure caption."""

    numbers = {
        int(match.group("number"))
        for match in _SIMPLE_TABLE_CAPTION_NUMBER_PATTERN.finditer(text)
    }
    if len(numbers) != 1:
        return None
    number = next(iter(numbers))
    return number if number > 0 else None


def _is_standalone_note_marker(text: str) -> bool:
    normalized = re.sub(r"[\s:：;；.。()（）\-]+", "", text).casefold()
    return normalized in {
        "注",
        "note",
        "注note",
        "备注",
        "remark",
        "remarks",
        "备注remark",
        "备注remarks",
    }


def _figure_caption_markers(
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
    relations: tuple[dict[str, Any], ...],
) -> tuple[set[str], dict[str, _FigureCaptionBinding]]:
    """Bind a pure caption to proven media, preferring explicit relations.

    A caption-like normative statement is rejected by
    :func:`_is_figure_caption_text` and remains reviewable.  Pure captions are
    never requirement candidates.  A source-grounded ``REFERENCES`` edge is
    authoritative even when body text occurs between the caption and image.
    Legacy snapshots without that relation may use strict immediate adjacency.
    Every path still requires the same visible owner/page and a one-to-one
    mapping; ambiguity remains ordered reading evidence instead of being lost.
    """

    ordered_nodes = sorted(
        all_structure_by_id.values(),
        key=lambda item: (
            *_structure_physical_reading_key(item),
            str(item.get("id") or ""),
        ),
    )
    media_by_id = {
        str(node.get("id") or ""): node
        for node in ordered_nodes
        if str(node.get("id") or "") and _source_fact_kinds(node) & {"IMAGE", "FIGURE"}
    }
    caption_by_id = {
        str(node.get("id") or ""): node
        for node in ordered_nodes
        if str(node.get("id") or "")
        and str(node.get("kind") or "").upper() == "CONTENT"
        and _is_figure_caption_text(str(node.get("label") or ""))
    }

    def compatible(media: dict[str, Any], caption: dict[str, Any]) -> bool:
        media_owner = _nearest_visible_owner(
            str(media.get("parent_id") or ""),
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        caption_owner = _nearest_visible_owner(
            str(caption.get("parent_id") or ""),
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        return bool(
            media_owner
            and media_owner == caption_owner
            and _structure_pages_are_compatible(media, caption)
        )

    explicit_candidates_by_media_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    explicit_media_ids_by_caption_id: dict[str, set[str]] = defaultdict(set)
    relation_touched_media_ids: set[str] = set()
    relation_touched_caption_ids: set[str] = set()
    for relation in relations:
        if str(relation.get("kind") or "").upper() != "REFERENCES":
            continue
        caption_id = str(relation.get("from_id") or "")
        media_id = str(relation.get("to_id") or "")
        caption = caption_by_id.get(caption_id)
        media = media_by_id.get(media_id)
        if caption is None or media is None:
            continue
        relation_touched_caption_ids.add(caption_id)
        relation_touched_media_ids.add(media_id)
        if not compatible(media, caption):
            continue
        explicit_candidates_by_media_id[media_id].append(caption)
        explicit_media_ids_by_caption_id[caption_id].add(media_id)

    candidates_by_media_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidate_media_ids: dict[str, set[str]] = defaultdict(set)
    for media_id, candidates in explicit_candidates_by_media_id.items():
        candidates_by_media_id[media_id].extend(candidates)
        for candidate in candidates:
            candidate_media_ids[str(candidate.get("id") or "")].add(media_id)

    # Strict adjacency is a backwards-compatible fallback only when neither
    # endpoint participates in an explicit caption/media relation.
    index_by_id = {
        str(node.get("id") or ""): index
        for index, node in enumerate(ordered_nodes)
        if str(node.get("id") or "")
    }
    for media_id, media in media_by_id.items():
        if media_id in relation_touched_media_ids:
            continue
        media_index = index_by_id[media_id]
        for candidate_index in (media_index - 1, media_index + 1):
            if not 0 <= candidate_index < len(ordered_nodes):
                continue
            candidate = ordered_nodes[candidate_index]
            caption_id = str(candidate.get("id") or "")
            if (
                caption_id not in caption_by_id
                or caption_id in relation_touched_caption_ids
                or not compatible(media, candidate)
            ):
                continue
            candidates_by_media_id[media_id].append(candidate)
            candidate_media_ids[caption_id].add(media_id)

    bindings_by_media_id: dict[str, _FigureCaptionBinding] = {}
    for media_node_id, candidates in candidates_by_media_id.items():
        unique_candidates = {
            str(candidate.get("id") or ""): candidate
            for candidate in candidates
            if str(candidate.get("id") or "")
        }
        if len(unique_candidates) != 1:
            continue
        caption_id, candidate = next(iter(unique_candidates.items()))
        if (
            len(candidate_media_ids.get(caption_id, ())) != 1
            or len(explicit_media_ids_by_caption_id.get(caption_id, ())) > 1
        ):
            continue
        caption_block = _source_bound_structure_content_block(candidate)
        if caption_block is None:
            continue
        caption_source_order, caption_source_offset = _structure_physical_reading_key(
            candidate
        )
        bindings_by_media_id[media_node_id] = _FigureCaptionBinding(
            media_structure_node_id=media_node_id,
            caption_structure_node_id=caption_id,
            caption=str(candidate.get("label") or "").strip(),
            caption_source_refs=tuple(_dict_items(caption_block.get("source_refs"))),
            caption_source_order=caption_source_order,
            caption_source_offset=caption_source_offset,
        )

    # Every pure caption stays presentation evidence and never becomes an
    # independent semantic candidate.  Successfully bound captions are folded
    # into their media block; ambiguous/unbound captions remain as ordinary
    # source-ordered reading blocks instead of disappearing.
    return set(caption_by_id), bindings_by_media_id


def _structure_pages_are_compatible(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    """Refuse a caption association only when page evidence contradicts it."""

    left_pages = _structure_page_numbers(left)
    right_pages = _structure_page_numbers(right)
    return not left_pages or not right_pages or bool(left_pages & right_pages)


def _structure_page_numbers(node: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    start = _positive_int(node.get("page_start"))
    end = _positive_int(node.get("page_end"))
    if start is not None and end is not None and end >= start:
        result.update(range(start, end + 1))
    else:
        if start is not None:
            result.add(start)
        if end is not None:
            result.add(end)
    for ref in _dict_items(node.get("source_refs")):
        page = _positive_int(ref.get("page_number"))
        if page is not None:
            result.add(page)
    return result


def _source_bound_figure_blocks_by_id(
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for node_id, node in all_structure_by_id.items():
        if str(node.get("kind") or "").upper() not in _SOURCE_CONTENT_KINDS:
            continue
        block = _source_bound_structure_content_block(node)
        if block is None or str(block.get("kind") or "").lower() not in {
            "figure",
            "image",
        }:
            continue
        result[node_id] = block
    return result


def _bind_explicit_figure_references_to_segments(
    *,
    direct_segments_by_owner: dict[str, tuple[_DirectContentSegment, ...]],
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
    relations: tuple[dict[str, Any], ...],
    figure_blocks_by_structure_id: dict[str, dict[str, Any]],
    excluded_source_node_ids: set[str],
) -> dict[str, tuple[_DirectContentSegment, ...]]:
    """Project proven body-to-image edges without widening text evidence.

    Physical adjacency alone only makes media shared owner context.  A figure
    becomes exact evidence for a DIRECT_CONTENT zone when the relation source
    is one of that zone's source-bound text nodes, the target is proven media,
    and owner/page evidence is compatible.  Segment identity, source refs,
    editable text blocks, and page ranges remain unchanged.
    """

    segment_locations_by_source_id: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for owner_id, segments in direct_segments_by_owner.items():
        for segment_index, segment in enumerate(segments):
            for block in segment.content_blocks:
                source_structure_node_id = str(block.get("structure_node_id") or "")
                if source_structure_node_id:
                    segment_locations_by_source_id[source_structure_node_id].append(
                        (owner_id, segment_index)
                    )

    referenced_blocks_by_location: dict[tuple[str, int], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for relation in relations:
        if str(relation.get("kind") or "").upper() != "REFERENCES":
            continue
        source_id = str(relation.get("from_id") or "")
        target_id = str(relation.get("to_id") or "")
        source_node = all_structure_by_id.get(source_id)
        target_node = all_structure_by_id.get(target_id)
        target_block = figure_blocks_by_structure_id.get(target_id)
        if (
            not source_id
            or source_id in excluded_source_node_ids
            or source_node is None
            or target_node is None
            or target_block is None
            or source_id in figure_blocks_by_structure_id
            or not _structure_pages_are_compatible(source_node, target_node)
        ):
            continue
        target_owner_id = _nearest_visible_owner(
            str(target_node.get("parent_id") or ""),
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        if not target_owner_id:
            continue
        for owner_id, segment_index in segment_locations_by_source_id.get(
            source_id,
            (),
        ):
            if owner_id != target_owner_id:
                continue
            referenced_blocks_by_location[(owner_id, segment_index)].append(
                target_block
            )

    return {
        owner_id: tuple(
            replace(
                segment,
                figure_refs=tuple(
                    sorted(
                        _dedupe_dicts(
                            (
                                *segment.figure_refs,
                                *referenced_blocks_by_location.get(
                                    (owner_id, segment_index),
                                    (),
                                ),
                            )
                        ),
                        key=lambda block: (
                            _nonnegative_int(block.get("source_order")),
                            _nonnegative_int(block.get("source_offset")),
                            str(block.get("source_id") or ""),
                        ),
                    )
                ),
            )
            for segment_index, segment in enumerate(segments)
        )
        for owner_id, segments in direct_segments_by_owner.items()
    }


def _source_fact_kinds(node: dict[str, Any]) -> set[str]:
    return {
        str(ref.get("source_kind") or "").upper()
        for ref in _dict_items(node.get("source_refs"))
        if str(ref.get("source_kind") or "")
    }


_EXPLICIT_CLAUSE_PREFIX_PATTERN = re.compile(
    r"^\s*(?:[（(]?\d+(?:\.\d+)*[）)]?|[A-Za-z])\s*[、.．)）]\s*"
)


_INLINE_NUMBERED_SENTENCE_END_PATTERN = re.compile(r"[。！？.!?；;]\s*$")


def _explicit_node_inline_body(node: dict[str, Any]) -> str:
    label = " ".join(str(node.get("label") or "").split())
    explicit_label = str(node.get("explicit_label") or "").strip()
    return (
        # Remove only the punctuation that separates the printed marker from
        # its body.  Stripping the right edge would erase an English terminal
        # period and incorrectly turn a complete bilingual provision into a
        # title-only node.
        label[len(explicit_label) :].lstrip(" 、.．)）")
        if explicit_label and label.startswith(explicit_label)
        else _EXPLICIT_CLAUSE_PREFIX_PATTERN.sub("", label, count=1).strip()
    )


def _explicit_node_has_normative_content(node: dict[str, Any]) -> bool:
    """Recognize source-bound normative prose without promoting noun titles."""

    body = _explicit_node_inline_body(node)
    return bool(
        _ENGLISH_NORMATIVE_STATEMENT_PATTERN.search(body)
        or _CHINESE_NORMATIVE_STATEMENT_PATTERN.search(body)
    )


def _explicit_node_has_inline_content(
    node: dict[str, Any],
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
) -> bool:
    """Return whether an explicit structure node also owns inline prose.

    This is a source-boundary decision, not a requirement classification.  A
    numbered list clause with text after its marker owns content.  A dotted
    SECTION is admitted conservatively when its own line has sentence-ending
    form; exact Structure spans are retained below for lineage.  Bare titles,
    tables, figures, furniture and TOC entries remain navigation/evidence.
    """

    kind = str(node.get("kind") or "").upper()
    explicit_label = str(node.get("explicit_label") or "").strip()
    if (
        kind not in {"CLAUSE", "SECTION"}
        or str(node.get("boundary_basis") or "").upper() != "EXPLICIT_NUMBER"
        or not _dict_items(node.get("source_refs"))
    ):
        return False
    if kind == "CLAUSE" and (
        _optional_nonnegative_int(node.get("list_ordinal")) is None
        and not explicit_label
    ):
        return False
    if kind == "SECTION" and not explicit_label:
        return False
    if _source_fact_kinds(node) & {
        "HEADER",
        "FOOTER",
        "WATERMARK",
        "TOC_ENTRY",
        "TABLE",
        "FIGURE",
        "IMAGE",
    }:
        return False
    if _has_structure_ancestor_kind(
        str(node.get("parent_id") or ""),
        ancestor_kinds={"TABLE", "FIGURE"},
        all_structure_by_id=all_structure_by_id,
    ):
        return False

    label = " ".join(str(node.get("label") or "").split())
    if not label or _is_table_caption_text(label) or _is_figure_caption_text(label):
        return False
    body = _explicit_node_inline_body(node)
    if kind == "SECTION" and not _INLINE_NUMBERED_SENTENCE_END_PATTERN.search(body):
        return False
    return bool(body and re.search(r"[\w\u3400-\u9fff]", body))


def _inline_structure_content_segment(
    node: dict[str, Any],
) -> _DirectContentSegment | None:
    """Project one explicit node's own text as exact, non-semantic content."""

    bindings = _structure_source_bindings(node)
    if not bindings:
        return None
    source_id, start_offset, end_offset = bindings[0]
    physical_order, physical_offset = _structure_physical_reading_key(node)
    precise_refs = _structure_source_span_refs(node)
    source_refs = precise_refs or tuple(_dict_items(node.get("source_refs")))
    block: dict[str, Any] = {
        "kind": "text",
        "source_id": source_id,
        "structure_node_id": str(node.get("id") or ""),
        "source_order": physical_order,
        "source_offset": physical_offset,
        "source_refs": source_refs,
        "text": str(node.get("label") or "").strip(),
    }
    if start_offset is not None:
        block["start_offset"] = start_offset
    if end_offset is not None:
        block["end_offset"] = end_offset
    page_start = _positive_int(node.get("page_start"))
    page_end = _positive_int(node.get("page_end"))
    if page_start is not None:
        block["page_start"] = page_start
    if page_end is not None:
        block["page_end"] = page_end
    return _direct_content_segment_from_blocks(
        owner_id=str(node.get("id") or ""),
        source_order=physical_order,
        source_offset=physical_offset,
        blocks=(block,),
    )


def _inline_segment_physically_continues_into_first_zone(
    *,
    owner: dict[str, Any],
    inline_segment: _DirectContentSegment,
    first_segment: _DirectContentSegment,
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
    continuation_pairs: set[tuple[str, str]],
) -> bool:
    """Join a printed clause line to its proven physical continuation.

    Effective Structure keeps the printed numbered line as the CLAUSE/SECTION
    node itself. A PDF line-wrap may put the remainder in the first directly
    owned CONTENT node. Those two source blocks are one reading unit, not two
    semantic requirements. The join is deliberately limited to the first
    reading zone and refuses to cross tables, figures, captions, or another
    explicit structure boundary.
    """

    if (
        not inline_segment.content_blocks
        or not first_segment.content_blocks
        or (
            first_segment.source_order,
            first_segment.source_offset,
        )
        <= (
            inline_segment.source_order,
            inline_segment.source_offset,
        )
    ):
        return False
    owner_id = str(owner.get("id") or "")
    if not owner_id or _owner_has_hard_boundary_before_order(
        owner_id=owner_id,
        owner_order=inline_segment.source_order,
        target_order=first_segment.source_order,
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
    ):
        return False
    inline_block = inline_segment.content_blocks[-1]
    continuation_block = first_segment.content_blocks[0]
    if str(inline_block.get("kind") or "").lower() not in {
        "text",
        "paragraph",
    } or str(continuation_block.get("kind") or "").lower() not in {
        "text",
        "paragraph",
    }:
        return False
    continuation_node_id = str(continuation_block.get("structure_node_id") or "")
    if (
        owner_id,
        continuation_node_id,
    ) not in continuation_pairs and not _content_blocks_have_exact_adjacent_span(
        inline_block,
        continuation_block,
    ):
        return False
    return _continues_relation_proves_physical_fragment(
        _DirectContentEvent(
            node_id=owner_id,
            owner_structure_node_id=owner_id,
            source_order=inline_segment.source_order,
            source_offset=inline_segment.source_offset,
            block=inline_block,
        ),
        _DirectContentEvent(
            node_id=str(continuation_block.get("structure_node_id") or ""),
            owner_structure_node_id=owner_id,
            source_order=first_segment.source_order,
            source_offset=first_segment.source_offset,
            block=continuation_block,
        ),
    )


def _content_blocks_have_exact_adjacent_span(
    source: dict[str, Any],
    target: dict[str, Any],
) -> bool:
    source_id = str(source.get("source_id") or source.get("source_node_id") or "")
    target_id = str(target.get("source_id") or target.get("source_node_id") or "")
    source_end = _optional_nonnegative_int(source.get("end_offset"))
    target_start = _optional_nonnegative_int(target.get("start_offset"))
    return bool(
        source_id
        and source_id == target_id
        and source_end is not None
        and target_start is not None
        and source_end == target_start
    )


def _owner_has_hard_boundary_before_order(
    *,
    owner_id: str,
    owner_order: int,
    target_order: int,
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
) -> bool:
    for node in all_structure_by_id.values():
        node_id = str(node.get("id") or "")
        if node_id == owner_id:
            continue
        source_order = _nonnegative_int(node.get("order"))
        if not owner_order < source_order <= target_order:
            continue
        kind = str(node.get("kind") or "").upper()
        nearest_owner = _nearest_visible_owner(
            str(node.get("parent_id") or ""),
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        if nearest_owner != owner_id:
            continue
        if kind in {"SECTION", "CLAUSE", "GROUP", "LIST_GROUP", "TABLE", "FIGURE"}:
            return True
        if kind != "CONTENT":
            continue
        label = str(node.get("label") or "")
        if (
            _is_table_caption_text(label)
            or _is_figure_caption_text(label)
            or _source_fact_kinds(node) & {"IMAGE", "FIGURE"}
        ):
            return True
    return False


def _validated_bilingual_projections(
    *,
    structure: dict[str, Any],
    relations: tuple[dict[str, Any], ...],
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
) -> tuple[_ValidatedBilingualProjection, ...]:
    """Validate the typed Structure sidecar before candidate coalescing.

    A plain ``TRANSLATION_OF`` relation is intentionally insufficient.  The
    group must bind an explicit language policy, both exact source sides, its
    Structure members, and (for two owners) the exact materialized relation.
    Any incomplete or tampered sidecar simply yields no coalescing.
    """

    sidecar = _dict(structure.get("bilingual_projection"))
    if (
        str(sidecar.get("schema_version") or "")
        != _STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION
    ):
        return ()
    raw_policy = sidecar.get("document_language_policy")
    if not isinstance(raw_policy, dict):
        return ()
    try:
        policy = DocumentLanguagePolicy.model_validate(raw_policy)
    except ValueError:
        return ()
    if policy.authority_basis == "UNKNOWN" and policy.policy_scope_source_ir_id != str(
        structure.get("source_ir_id") or ""
    ):
        return ()

    raw_groups = _dict_items(sidecar.get("counterpart_groups"))
    try:
        groups = tuple(
            BilingualCounterpartGroup.model_validate(item) for item in raw_groups
        )
    except ValueError:
        return ()
    group_ids = tuple(group.counterpart_group_id for group in groups)
    if len(group_ids) != len(set(group_ids)) or group_ids != tuple(sorted(group_ids)):
        return ()

    raw_signals = _dict_items(sidecar.get("mismatch_signals"))
    try:
        mismatch_signals = tuple(
            BilingualMismatchSignal.model_validate(item) for item in raw_signals
        )
    except ValueError:
        return ()
    mismatch_ids = tuple(signal.mismatch_signal_id for signal in mismatch_signals)
    if len(mismatch_ids) != len(set(mismatch_ids)) or mismatch_ids != tuple(
        sorted(mismatch_ids)
    ):
        return ()

    relations_by_id = {
        str(relation.get("id") or ""): relation
        for relation in relations
        if str(relation.get("id") or "")
    }
    direct_content_ref_keys_by_parent: dict[
        str,
        set[tuple[str, str]],
    ] = defaultdict(set)
    for child in all_structure_by_id.values():
        parent_id = str(child.get("parent_id") or "")
        if not parent_id or str(child.get("kind") or "").upper() != "CONTENT":
            continue
        direct_content_ref_keys_by_parent[parent_id].update(
            key
            for ref in _dict_items(child.get("source_refs"))
            if (key := _source_ref_key(ref))[1]
        )
    source_ref_keys_by_member: dict[str, set[tuple[str, str]]] = {
        member_id: {
            _source_ref_key(ref)
            for ref in _dict_items(node.get("source_refs"))
            if _source_ref_key(ref)[1]
        }
        | direct_content_ref_keys_by_parent.get(member_id, set())
        for member_id, node in all_structure_by_id.items()
    }
    assigned_member_ids: set[str] = set()
    assigned_span_ids: set[str] = set()
    validated: list[_ValidatedBilingualProjection] = []
    for group in groups:
        if (
            group.document_language_policy_id != policy.document_language_policy_id
            or any(
                member_id not in visible_structure_ids
                or str(all_structure_by_id.get(member_id, {}).get("kind") or "").upper()
                not in _DIRECT_REVIEW_OWNER_KINDS
                for member_id in group.member_structure_node_ids
            )
            or assigned_member_ids.intersection(group.member_structure_node_ids)
        ):
            return ()
        if len(group.member_structure_node_ids) == 2:
            member_parents = {
                str(all_structure_by_id[member_id].get("parent_id") or "")
                for member_id in group.member_structure_node_ids
            }
            if len(member_parents) != 1:
                return ()

        span_owner_pairs: list[tuple[str, str]] = []
        group_spans = (*group.english_spans, *group.chinese_spans)
        for span in group_spans:
            if span.bilingual_span_id in assigned_span_ids:
                return ()
            matching_members = tuple(
                member_id
                for member_id in group.member_structure_node_ids
                if _source_ref_key(span.source_ref.model_dump(mode="json"))
                in source_ref_keys_by_member.get(member_id, set())
            )
            if len(matching_members) != 1:
                return ()
            span_owner_pairs.append((span.bilingual_span_id, matching_members[0]))

        if len(group.member_structure_node_ids) == 2:
            relation = relations_by_id.get(group.translation_relation_id or "")
            other_member = next(
                member_id
                for member_id in group.member_structure_node_ids
                if member_id != group.canonical_structure_node_id
            )
            expected_relation_refs = tuple(
                dict.fromkeys(
                    _source_ref_key(span.source_ref.model_dump(mode="json"))
                    for span in group_spans
                )
            )
            actual_relation_refs = tuple(
                _source_ref_key(ref)
                for ref in _dict_items((relation or {}).get("source_refs"))
            )
            if (
                relation is None
                or str(relation.get("kind") or "").upper() != "TRANSLATION_OF"
                or str(relation.get("from_id") or "")
                != group.canonical_structure_node_id
                or str(relation.get("to_id") or "") != other_member
                or actual_relation_refs != expected_relation_refs
            ):
                return ()

        group_span_ids = {span.bilingual_span_id for span in group_spans}
        group_signals = tuple(
            signal
            for signal in mismatch_signals
            if signal.counterpart_group_id == group.counterpart_group_id
        )
        if any(
            not {
                *signal.english_span_ids,
                *signal.chinese_span_ids,
            }.issubset(group_span_ids)
            for signal in group_signals
        ):
            return ()
        assigned_member_ids.update(group.member_structure_node_ids)
        assigned_span_ids.update(group_span_ids)
        validated.append(
            _ValidatedBilingualProjection(
                group=group,
                policy=policy,
                mismatch_signals=group_signals,
                span_owner_by_id=tuple(span_owner_pairs),
            )
        )

    if any(
        signal.counterpart_group_id not in set(group_ids) for signal in mismatch_signals
    ):
        return ()
    return tuple(validated)


def _source_ref_key(value: Any) -> tuple[str, str]:
    ref = (
        value.model_dump(mode="json") if hasattr(value, "model_dump") else _dict(value)
    )
    raw_kind = str(ref.get("kind") or "SOURCE_NODE")
    kind = raw_kind.rsplit(".", 1)[-1].upper()
    return kind, str(ref.get("source_id") or "")


def _coalesce_typed_bilingual_direct_content(
    *,
    direct_segments_by_owner: dict[str, tuple[_DirectContentSegment, ...]],
    validated_groups: tuple[_ValidatedBilingualProjection, ...],
    all_structure_by_id: dict[str, dict[str, Any]],
) -> tuple[
    dict[str, tuple[_DirectContentSegment, ...]],
    dict[str, RequirementTreeBilingualContextV1],
]:
    """Collapse only source zones selected by a validated counterpart group."""

    mutable = {
        owner_id: list(segments)
        for owner_id, segments in direct_segments_by_owner.items()
    }
    context_by_segment_id: dict[str, RequirementTreeBilingualContextV1] = {}
    claimed_segment_ids: set[str] = set()
    for validated in validated_groups:
        group = validated.group
        span_owner_by_id = dict(validated.span_owner_by_id)
        spans_by_owner: dict[str, list[Any]] = defaultdict(list)
        for span in (*group.english_spans, *group.chinese_spans):
            spans_by_owner[span_owner_by_id[span.bilingual_span_id]].append(span)

        selected_by_owner: dict[str, tuple[_DirectContentSegment, ...]] = {}
        group_is_projectable = True
        for member_id in group.member_structure_node_ids:
            member_spans = tuple(spans_by_owner.get(member_id, ()))
            matches = _segments_exactly_cover_bilingual_spans(
                tuple(
                    segment
                    for segment in mutable.get(member_id, ())
                    if segment.id not in claimed_segment_ids
                ),
                member_spans,
            )
            if not matches:
                group_is_projectable = False
                break
            selected_by_owner[member_id] = matches
        if not group_is_projectable:
            continue

        selected_segments = tuple(
            segment
            for member_id in group.member_structure_node_ids
            for segment in selected_by_owner[member_id]
        )
        context = _bilingual_context(validated)
        annotated_blocks = tuple(
            _annotate_bilingual_content_block(
                block,
                context=context,
            )
            for segment in selected_segments
            for block in segment.content_blocks
        )
        reading_key = min(
            (segment.source_order, segment.source_offset)
            for segment in selected_segments
        )
        merged = _direct_content_segment_from_blocks(
            owner_id=group.canonical_structure_node_id,
            source_order=reading_key[0],
            source_offset=reading_key[1],
            blocks=annotated_blocks,
        )
        exact_source_refs = tuple(
            {
                **span.source_ref.model_dump(mode="json"),
                "owner_structure_node_id": span_owner_by_id[span.bilingual_span_id],
                "start_offset": span.start_offset,
                "end_offset": span.end_offset,
                "bilingual_span_id": span.bilingual_span_id,
                "bilingual_language": span.language_evidence.value,
                "bilingual_counterpart_group_id": group.counterpart_group_id,
                "boundary_basis": span.boundary_basis.value,
            }
            for span in (*group.english_spans, *group.chinese_spans)
        )
        segment_identity = json.dumps(
            {
                "owner_structure_node_id": group.canonical_structure_node_id,
                "counterpart_group_id": group.counterpart_group_id,
                "ordered_bilingual_span_ids": [
                    span.bilingual_span_id
                    for span in (*group.english_spans, *group.chinese_spans)
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        merged = replace(
            merged,
            id=(f"direct-content-{hashlib.sha256(segment_identity).hexdigest()[:32]}"),
            source_refs=tuple(
                _dedupe_dicts(
                    (
                        *(
                            ref
                            for segment in selected_segments
                            for ref in segment.source_refs
                        ),
                        *exact_source_refs,
                    )
                )
            ),
            table_refs=tuple(
                _dedupe_dicts(
                    ref for segment in selected_segments for ref in segment.table_refs
                )
            ),
            figure_refs=tuple(
                _dedupe_dicts(
                    ref for segment in selected_segments for ref in segment.figure_refs
                )
            ),
        )
        for member_id, selected in selected_by_owner.items():
            selected_ids = {segment.id for segment in selected}
            mutable[member_id] = [
                segment
                for segment in mutable.get(member_id, ())
                if segment.id not in selected_ids
            ]
            claimed_segment_ids.update(selected_ids)
        mutable.setdefault(group.canonical_structure_node_id, []).append(merged)
        mutable[group.canonical_structure_node_id].sort(
            key=lambda segment: (
                segment.source_order,
                segment.source_offset,
                segment.id,
            )
        )
        context_by_segment_id[merged.id] = context

    return (
        {
            owner_id: tuple(segments)
            for owner_id, segments in mutable.items()
            if segments
        },
        context_by_segment_id,
    )


def _ensure_typed_bilingual_owner_segments(
    *,
    direct_segments_by_owner: dict[str, tuple[_DirectContentSegment, ...]],
    validated_groups: tuple[_ValidatedBilingualProjection, ...],
    all_structure_by_id: dict[str, dict[str, Any]],
) -> dict[str, tuple[_DirectContentSegment, ...]]:
    """Expose member-owned inline source only when the typed group requires it.

    Normal candidate projection may conservatively treat a short numbered
    phrase as a structural title.  A validated bilingual group is stronger
    source-bound evidence: if one of its exact spans belongs to the member's
    own printed block, that block must participate in the one logical review
    item together with its direct CONTENT counterpart.  Descendant or sibling
    refs never qualify here.
    """

    mutable = dict(direct_segments_by_owner)
    for validated in validated_groups:
        group = validated.group
        spans = (*group.english_spans, *group.chinese_spans)
        for member_id in group.member_structure_node_ids:
            member = all_structure_by_id.get(member_id)
            if member is None:
                continue
            own_source_keys = {
                _source_ref_key(ref)
                for ref in _dict_items(member.get("source_refs"))
                if _source_ref_key(ref)[1]
            }
            own_spans = tuple(
                span
                for span in spans
                if _source_ref_key(span.source_ref) in own_source_keys
            )
            if not own_spans:
                continue
            existing = mutable.get(member_id, ())
            if all(
                sum(
                    _segment_covers_bilingual_span(segment, span)
                    for segment in existing
                )
                == 1
                for span in own_spans
            ):
                continue
            inline = _inline_structure_content_segment(member)
            if inline is None or any(
                not _segment_covers_bilingual_span(inline, span) for span in own_spans
            ):
                continue
            if inline.id not in {segment.id for segment in existing}:
                mutable[member_id] = tuple(
                    sorted(
                        (*existing, inline),
                        key=lambda segment: (
                            segment.source_order,
                            segment.source_offset,
                            segment.id,
                        ),
                    )
                )
    return mutable


def _segments_exactly_cover_bilingual_spans(
    segments: tuple[_DirectContentSegment, ...],
    spans: tuple[Any, ...],
) -> tuple[_DirectContentSegment, ...]:
    if not spans:
        return ()
    selected_by_id: dict[str, _DirectContentSegment] = {}
    for span in spans:
        matches = tuple(
            segment
            for segment in segments
            if _segment_covers_bilingual_span(segment, span)
        )
        if len(matches) != 1:
            return ()
        selected_by_id[matches[0].id] = matches[0]
    return tuple(
        sorted(
            selected_by_id.values(),
            key=lambda segment: (
                segment.source_order,
                segment.source_offset,
                segment.id,
            ),
        )
    )


def _segment_covers_bilingual_span(
    segment: _DirectContentSegment,
    span: Any,
) -> bool:
    source_id = span.source_ref.source_id
    for block in segment.content_blocks:
        if (
            str(block.get("source_id") or block.get("source_node_id") or "")
            != source_id
        ):
            continue
        start = _optional_nonnegative_int(block.get("start_offset"))
        end = _optional_nonnegative_int(block.get("end_offset"))
        block_refs = tuple(
            ref
            for ref in _dict_items(block.get("source_refs"))
            if str(ref.get("source_id") or "") == source_id
        )
        # A SOURCE_BLOCK_BOUNDARY is, by contract, the complete immutable
        # SourceIR node.  Durable presentation deliberately trims boundary
        # whitespace from its display text, so its string length cannot be
        # used to re-prove the original half-open interval after restart.  The
        # exact SourceIR identity remains present in the block's source refs
        # and was already bound to this member by the validated sidecar.  Use
        # that identity only for full-block spans; partial DOCX run spans keep
        # the strict offset coverage check below.
        if (
            span.boundary_basis is BilingualSpanBoundaryBasis.SOURCE_BLOCK_BOUNDARY
            and span.start_offset == 0
            and _source_ref_key(span.source_ref)
            in {_source_ref_key(ref) for ref in block_refs}
        ):
            return True
        if start is None:
            start = min(
                (
                    value
                    for ref in block_refs
                    if (value := _optional_nonnegative_int(ref.get("start_offset")))
                    is not None
                ),
                default=0,
            )
        if end is None:
            end = max(
                (
                    value
                    for ref in block_refs
                    if (value := _optional_nonnegative_int(ref.get("end_offset")))
                    is not None
                ),
                default=len(str(block.get("text") or block.get("raw_text") or "")),
            )
        if start <= span.start_offset and end >= span.end_offset:
            return True
    return False


def _bilingual_context(
    validated: _ValidatedBilingualProjection,
) -> RequirementTreeBilingualContextV1:
    group = validated.group
    span_owner_by_id = dict(validated.span_owner_by_id)
    mismatch_ids = tuple(
        signal.mismatch_signal_id for signal in validated.mismatch_signals
    )
    return RequirementTreeBilingualContextV1(
        counterpart_group_id=group.counterpart_group_id,
        document_language_policy_id=group.document_language_policy_id,
        canonical_structure_node_id=group.canonical_structure_node_id,
        member_structure_node_ids=group.member_structure_node_ids,
        authority_language=validated.policy.conflict_precedence,
        authority_basis=validated.policy.authority_basis,
        equal_validity=validated.policy.equal_validity,
        source_spans=tuple(
            RequirementTreeBilingualSourceSpanV1(
                bilingual_span_id=span.bilingual_span_id,
                language=span.language_evidence.value,
                source_ref=span.source_ref.model_dump(mode="json"),
                owner_structure_node_id=span_owner_by_id[span.bilingual_span_id],
                start_offset=span.start_offset,
                end_offset=span.end_offset,
                boundary_basis=span.boundary_basis.value,
            )
            for span in (*group.english_spans, *group.chinese_spans)
        ),
        mismatch_signal_ids=mismatch_ids,
        mismatch_review_required=bool(mismatch_ids),
    )


def _annotate_bilingual_content_block(
    block: dict[str, Any],
    *,
    context: RequirementTreeBilingualContextV1,
) -> dict[str, Any]:
    source_id = str(block.get("source_id") or block.get("source_node_id") or "")
    matching_spans = tuple(
        span
        for span in context.source_spans
        if str(span.source_ref.get("source_id") or "") == source_id
    )
    return {
        **block,
        "bilingual_counterpart_group_id": context.counterpart_group_id,
        "bilingual_span_ids": tuple(span.bilingual_span_id for span in matching_spans),
        "bilingual_languages": tuple(
            dict.fromkeys(span.language for span in matching_spans)
        ),
        "bilingual_authority_language": context.authority_language,
        "bilingual_authority_basis": context.authority_basis,
        "bilingual_mismatch_review_required": (context.mismatch_review_required),
    }


def _direct_content_segments_by_owner(
    *,
    workbench: dict[str, Any],
    payload: dict[str, Any],
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
    fallback_content_by_owner: dict[str, tuple[dict[str, Any], ...]],
    direct_source_owner_by_id: dict[str, str],
    excluded_content_node_ids: set[str] | None = None,
    excluded_fallback_owner_ids: set[str] | None = None,
) -> dict[str, tuple[_DirectContentSegment, ...]]:
    """Return directly owned source groups without copying descendant bodies.

    Rules preserve one non-atomic content unit per directly owned reading zone.
    Explicit child structures and TABLE/FIGURE facts split zones; raw PDF text
    blocks do not.  ``CONTINUES`` remains physical-lineage evidence, but no
    longer decides semantic candidate membership.  A later model/person may
    map each source-bound zone to zero, one or many atomic requirements.
    """

    excluded_content_node_ids = excluded_content_node_ids or set()
    excluded_fallback_owner_ids = excluded_fallback_owner_ids or set()
    tables = _dict_items(payload.get("tables"))
    tables_by_id = {
        str(table.get("id") or ""): table
        for table in tables
        if str(table.get("id") or "")
    }
    tables_by_source_id = {
        str(table.get("source_node_id") or ""): table
        for table in tables
        if str(table.get("source_node_id") or "")
    }
    visible_children = _visible_children_by_owner(
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
    )
    boundary_orders_by_owner: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for owner_id, child_ids in visible_children.items():
        boundary_orders_by_owner[owner_id].extend(
            _structure_physical_reading_key(all_structure_by_id[child_id])
            for child_id in child_ids
        )

    applies_to_by_source: dict[str, set[str]] = defaultdict(set)
    translations_by_source: dict[str, set[str]] = defaultdict(set)
    translation_relations: list[tuple[str, str]] = []
    for relation in _dict_items(workbench.get("relations")):
        kind = str(relation.get("kind") or "").upper()
        source_id = str(relation.get("from_id") or "")
        target_id = str(relation.get("to_id") or "")
        if not source_id or not target_id:
            continue
        if kind == "APPLIES_TO":
            applies_to_by_source[source_id].add(target_id)
        elif kind == "TRANSLATION_OF":
            translation_relations.append((source_id, target_id))
            translations_by_source[source_id].add(target_id)
            translations_by_source[target_id].add(source_id)

    events_by_owner: dict[str, list[_DirectContentEvent]] = defaultdict(list)
    ordered_nodes = sorted(
        all_structure_by_id.values(),
        key=lambda item: (
            *_structure_physical_reading_key(item),
            str(item.get("id") or ""),
        ),
    )
    for node in ordered_nodes:
        kind = str(node.get("kind") or "").upper()
        if kind not in _SOURCE_CONTENT_KINDS:
            continue
        node_id = str(node.get("id") or "")
        if node_id in excluded_content_node_ids:
            continue
        if kind == "CONTENT" and (
            _is_table_caption_text(str(node.get("label") or ""))
            or _is_figure_caption_text(str(node.get("label") or ""))
        ):
            owner_id = _nearest_visible_owner(
                str(node.get("parent_id") or ""),
                visible_ids=visible_structure_ids,
                all_nodes=all_structure_by_id,
            )
            if owner_id:
                # Captions are evidence labels and hard reading-zone
                # boundaries.  They may still title a table/figure projection,
                # but never become an independent content candidate.
                boundary_orders_by_owner[owner_id].append(
                    _structure_physical_reading_key(node)
                )
            continue
        if kind == "CONTENT" and _is_standalone_note_marker(
            str(node.get("label") or "")
        ):
            # A bare "注 / Note / Remarks" introduces the following note
            # clauses.  It is presentation context, not an independent
            # statement to classify or publish.
            continue
        source_fact_kinds = _source_fact_kinds(node)
        if kind == "CONTENT" and source_fact_kinds & {
            "IMAGE",
            "FIGURE",
            "HEADER",
            "FOOTER",
            "WATERMARK",
            "TOC_ENTRY",
        }:
            if source_fact_kinds & {"IMAGE", "FIGURE"}:
                owner_id = _nearest_visible_owner(
                    str(node.get("parent_id") or ""),
                    visible_ids=visible_structure_ids,
                    all_nodes=all_structure_by_id,
                )
                if owner_id:
                    boundary_orders_by_owner[owner_id].append(
                        _structure_physical_reading_key(node)
                    )
            continue
        if kind == "CONTENT" and _has_structure_ancestor_kind(
            str(node.get("parent_id") or ""),
            ancestor_kinds={"TABLE", "FIGURE"},
            all_structure_by_id=all_structure_by_id,
        ):
            # Merged-cell labels and other table/figure descendants already
            # belong to their authoritative evidence projection.  Promoting
            # them again as prose candidates duplicates the same source fact
            # and makes an intact table look like a list of requirements.
            continue
        owner_id = _nearest_visible_owner(
            str(node.get("parent_id") or ""),
            visible_ids=visible_structure_ids,
            all_nodes=all_structure_by_id,
        )
        if not node_id or not owner_id:
            continue
        if (
            str(all_structure_by_id[owner_id].get("kind") or "").upper()
            not in _DIRECT_REVIEW_OWNER_KINDS
        ):
            # Root/navigation-owned raw content is still available to the
            # source/structure inspection layers.  It has no proven semantic
            # clause owner, so fail closed instead of manufacturing a review
            # candidate under the document root.
            continue
        block = _source_bound_structure_content_block(node)
        if block is None:
            continue
        hydrated = _hydrate_table_content_blocks(
            (block,),
            tables_by_id=tables_by_id,
            tables_by_source_id=tables_by_source_id,
        )[0]
        source_order, source_offset = _structure_physical_reading_key(node)
        if kind in {"TABLE", "FIGURE"}:
            # Tables and figures have their own evidence projections. They are
            # hard boundaries for prose grouping, regardless of table mode.
            boundary_orders_by_owner[owner_id].append((source_order, source_offset))
            continue
        applicable_targets = tuple(
            sorted(
                target_id
                for target_id in applies_to_by_source.get(node_id, ())
                if target_id in all_structure_by_id
            )
        )
        if applicable_targets:
            hydrated = {
                **hydrated,
                "applies_to_structure_node_ids": applicable_targets,
            }
        translation_targets = tuple(
            sorted(
                target_id
                for target_id in translations_by_source.get(node_id, ())
                if target_id in all_structure_by_id
            )
        )
        if translation_targets:
            # Preserve the explicit bilingual relation without concatenating
            # two languages into one synthetic statement.
            hydrated = {
                **hydrated,
                "translation_peer_structure_node_ids": translation_targets,
            }
        events_by_owner[owner_id].append(
            _DirectContentEvent(
                node_id=node_id,
                owner_structure_node_id=owner_id,
                source_order=source_order,
                source_offset=source_offset,
                block=hydrated,
            )
        )

    result: dict[str, tuple[_DirectContentSegment, ...]] = {}
    for owner_id in sorted(visible_structure_ids):
        if (
            str(all_structure_by_id[owner_id].get("kind") or "").upper()
            not in _DIRECT_REVIEW_OWNER_KINDS
        ):
            continue
        events = sorted(
            events_by_owner.get(owner_id, ()),
            key=lambda event: (
                event.source_order,
                event.source_offset,
                event.node_id,
            ),
        )
        if events:
            boundaries = sorted(set(boundary_orders_by_owner.get(owner_id, ())))
            zone_by_node_id = {
                event.node_id: sum(
                    boundary_order < (event.source_order, event.source_offset)
                    for boundary_order in boundaries
                )
                for event in events
            }
            grouped_events: dict[int, list[_DirectContentEvent]] = defaultdict(list)
            for event in events:
                grouped_events[zone_by_node_id[event.node_id]].append(event)
            segments = tuple(
                _direct_content_segment(owner_id=owner_id, events=group)
                for group in sorted(
                    grouped_events.values(),
                    key=lambda group: (
                        min(event.source_order for event in group),
                        min(
                            (event.source_order, event.source_offset) for event in group
                        )[1],
                        min(event.node_id for event in group),
                    ),
                )
            )
            if segments:
                result[owner_id] = segments
            continue

        # Sparse/older artifacts can lack raw CONTENT nodes. Only a leaf owns
        # an unambiguous fallback boundary. Container review context is an
        # aggregate presentation layer and must never manufacture candidates.
        child_ids = visible_children.get(owner_id, ())
        if child_ids or owner_id in excluded_fallback_owner_ids:
            continue
        fallback = _structure_presentation_content(
            node_id=owner_id,
            blocks=fallback_content_by_owner.get(owner_id, ()),
            direct_source_owner_by_id=direct_source_owner_by_id,
            all_structure_by_id=all_structure_by_id,
        )
        fallback = _without_structure_heading(
            fallback,
            structure_node=all_structure_by_id[owner_id],
        )
        fallback = tuple(
            block
            for block in fallback
            if (
                str(block.get("kind") or "").lower() not in {"table", "figure", "image"}
                and str(block.get("structure_node_id") or "")
                not in excluded_content_node_ids
            )
        )
        if fallback:
            result[owner_id] = (
                _direct_content_segment_from_blocks(
                    owner_id=owner_id,
                    source_order=_nonnegative_int(
                        all_structure_by_id[owner_id].get("order")
                    ),
                    blocks=fallback,
                ),
            )
    return result


_TERMINAL_SENTENCE_PATTERN = re.compile(r"[。！？.!?；;：:]\s*$")
_CROSS_PAGE_CONTINUATION_PREFIX = re.compile(
    r"^(?:[a-z]|的|及|并|或|且|以及|其中|同时|以|与|和|而|但)",
    re.IGNORECASE,
)


def _continues_relation_proves_physical_fragment(
    source: _DirectContentEvent,
    target: _DirectContentEvent,
) -> bool:
    """Distinguish physical wrapping from a semantic cross-page sequence.

    Structure ``CONTINUES`` is also used to preserve reading flow across a
    page break.  That broader relation must not collapse two complete
    statements into one review candidate.  Exact adjacent spans are always
    physical; otherwise the source must be syntactically unfinished and page
    geometry must be compatible with a same-page wrap or a conservative
    adjacent-page continuation.
    """

    if (target.source_order, target.source_offset) <= (
        source.source_order,
        source.source_offset,
    ):
        return False
    source_block = source.block
    target_block = target.block
    source_id = str(
        source_block.get("source_id") or source_block.get("source_node_id") or ""
    )
    target_id = str(
        target_block.get("source_id") or target_block.get("source_node_id") or ""
    )
    source_end = _optional_nonnegative_int(source_block.get("end_offset"))
    target_start = _optional_nonnegative_int(target_block.get("start_offset"))
    if (
        source_id
        and source_id == target_id
        and source_end is not None
        and target_start is not None
        and source_end == target_start
    ):
        return True

    source_text = str(source_block.get("text") or "").rstrip()
    target_text = str(target_block.get("text") or "").lstrip()
    if (
        not source_text
        or not target_text
        or _TERMINAL_SENTENCE_PATTERN.search(source_text)
    ):
        return False
    source_page = _positive_int(
        source_block.get("page_start")
        or source_block.get("page_end")
        or source_block.get("page_number")
    )
    target_page = _positive_int(
        target_block.get("page_start")
        or target_block.get("page_number")
        or target_block.get("page_end")
    )
    if source_page is None or target_page is None:
        return False
    if source_page == target_page:
        return True
    if target_page != source_page + 1:
        return False
    return bool(
        source_text.endswith(("-", "‐", "‑", "–"))
        or _CROSS_PAGE_CONTINUATION_PREFIX.match(target_text)
    )


def _direct_content_segment(
    *,
    owner_id: str,
    events: list[_DirectContentEvent],
) -> _DirectContentSegment:
    ordered = sorted(
        events,
        key=lambda event: (
            event.source_order,
            event.source_offset,
            event.node_id,
        ),
    )
    return _direct_content_segment_from_blocks(
        owner_id=owner_id,
        source_order=ordered[0].source_order,
        source_offset=ordered[0].source_offset,
        blocks=tuple(event.block for event in ordered),
    )


def _direct_content_segment_from_blocks(
    *,
    owner_id: str,
    source_order: int,
    source_offset: int = 0,
    blocks: tuple[dict[str, Any], ...],
) -> _DirectContentSegment:
    blocks = tuple(
        block
        for _, block in sorted(
            enumerate(blocks),
            key=lambda pair: (
                _nonnegative_int(pair[1].get("source_order")),
                _nonnegative_int(pair[1].get("source_offset")),
                pair[0],
            ),
        )
    )
    blocks = _direct_content_blocks_with_owned_pages(blocks)
    source_refs = _direct_content_source_refs(owner_id=owner_id, blocks=blocks)
    canonical_bindings = []
    for block in blocks:
        block_pages = _owned_content_block_page_numbers(block)
        canonical_bindings.append(
            {
                "kind": str(block.get("kind") or "").lower(),
                "source_id": str(
                    block.get("source_id") or block.get("source_node_id") or ""
                ),
                "structure_node_id": str(block.get("structure_node_id") or ""),
                "start_offset": _optional_nonnegative_int(block.get("start_offset")),
                "end_offset": _optional_nonnegative_int(block.get("end_offset")),
                "page_start": min(block_pages, default=None),
                "page_end": max(block_pages, default=None),
            }
        )
    canonical = json.dumps(
        {
            "owner_structure_node_id": owner_id,
            "source_groups": canonical_bindings,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    segment_id = f"direct-content-{hashlib.sha256(canonical).hexdigest()[:32]}"
    pages = tuple(
        sorted(
            {
                page
                for block in blocks
                for page in _owned_content_block_page_numbers(block)
            }
        )
    )
    return _DirectContentSegment(
        id=segment_id,
        owner_structure_node_id=owner_id,
        source_order=source_order,
        source_offset=source_offset,
        content_blocks=blocks,
        source_refs=source_refs,
        page_start=min(pages, default=None),
        page_end=max(pages, default=None),
        table_refs=_table_refs(blocks),
        figure_refs=_figure_refs(blocks),
    )


def _direct_content_blocks_with_owned_pages(
    blocks: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Normalize candidate blocks to their own source-bound page range.

    Presentation contexts may carry an owner's aggregate ``page_end``.  A
    DIRECT_CONTENT leaf must never inherit that adjacent-page range when its
    own source reference resolves to a narrower page.  Exact source pages win;
    the aggregate block range remains a fallback for older sparse artifacts.
    """

    normalized: list[dict[str, Any]] = []
    for block in blocks:
        pages = _owned_content_block_page_numbers(block)
        if not pages:
            normalized.append(block)
            continue
        current = dict(block)
        current["page_start"] = min(pages)
        current["page_end"] = max(pages)
        normalized.append(current)
    return tuple(normalized)


def _owned_content_block_page_numbers(block: dict[str, Any]) -> tuple[int, ...]:
    """Return pages proven by this block, excluding owner/sibling rollups."""

    source_id = str(block.get("source_id") or block.get("source_node_id") or "")
    refs = _dict_items(block.get("source_refs"))
    matching_refs = tuple(
        ref for ref in refs if str(ref.get("source_id") or "") == source_id
    )
    owned_refs = matching_refs or tuple(refs)

    # Point locations are stronger than aggregate ranges.  Multiple point
    # references still preserve a legitimate multi-page candidate.
    point_pages: set[int] = set()
    block_location = _dict(block.get("location"))
    for value in (block.get("page_number"), block_location.get("page_number")):
        page = _positive_int(value)
        if page is not None:
            point_pages.add(page)
    for ref in owned_refs:
        location = _dict(ref.get("location"))
        for value in (ref.get("page_number"), location.get("page_number")):
            page = _positive_int(value)
            if page is not None:
                point_pages.add(page)
    if point_pages:
        return tuple(sorted(point_pages))

    range_pages: set[int] = set()
    for ref in owned_refs:
        for value in (ref.get("page_start"), ref.get("page_end")):
            page = _positive_int(value)
            if page is not None:
                range_pages.add(page)
    if range_pages:
        return tuple(sorted(range_pages))

    fallback_pages = {
        page
        for value in (block.get("page_start"), block.get("page_end"))
        if (page := _positive_int(value)) is not None
    }
    return tuple(sorted(fallback_pages))


def _direct_content_source_refs(
    *,
    owner_id: str,
    blocks: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    refs: list[dict[str, Any]] = []
    for block in blocks:
        block_refs = _dict_items(block.get("source_refs"))
        source_id = str(block.get("source_id") or block.get("source_node_id") or "")
        matching_block_refs = tuple(
            ref
            for ref in block_refs
            if source_id and str(ref.get("source_id") or "") == source_id
        )
        if matching_block_refs:
            refs.extend(matching_block_refs)
        elif block_refs:
            refs.extend(block_refs)
        if source_id:
            block_pages = _owned_content_block_page_numbers(block)
            refs.append(
                {
                    "kind": str(block.get("kind") or "SOURCE_NODE").upper(),
                    "source_id": source_id,
                    "owner_structure_node_id": owner_id,
                    "start_offset": _optional_nonnegative_int(
                        block.get("start_offset")
                    ),
                    "end_offset": _optional_nonnegative_int(block.get("end_offset")),
                    "page_start": min(block_pages, default=None),
                    "page_end": max(block_pages, default=None),
                }
            )
    return tuple(_dedupe_dicts(refs))


def _without_structure_heading(
    blocks: tuple[dict[str, Any], ...],
    *,
    structure_node: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    if not blocks:
        return ()
    label = " ".join(str(structure_node.get("label") or "").split())
    start = 0
    while start < len(blocks):
        block = blocks[start]
        text = " ".join(str(block.get("text") or "").split())
        if (label and text == label) or str(
            block.get("structure_node_id") or ""
        ) == str(structure_node.get("id") or ""):
            start += 1
            continue
        break
    return tuple(blocks[start:])


def _direct_content_label(blocks: tuple[dict[str, Any], ...]) -> str:
    if any(
        block.get("translation_peer_structure_node_ids")
        or block.get("bilingual_counterpart_group_id")
        for block in blocks
    ):
        text = " ".join(_direct_content_summary(blocks).split())
    else:
        parts = [
            " ".join(str(block.get("text") or block.get("raw_text") or "").split())
            for block in blocks
            if str(block.get("text") or block.get("raw_text") or "").strip()
        ]
        text = ""
        for part in parts:
            separator = (
                ""
                if text
                and re.search(r"[\u3400-\u9fff]$", text)
                and re.match(r"^[\u3400-\u9fff]", part)
                else " "
                if text
                else ""
            )
            text = f"{text}{separator}{part}"
    if not text:
        if _table_refs(blocks):
            return "表格内容"
        if _figure_refs(blocks):
            return "图片内容"
        return "直接正文"
    return text if len(text) <= 120 else f"{text[:119]}…"


def _direct_content_summary(blocks: tuple[dict[str, Any], ...]) -> str:
    if any(
        block.get("translation_peer_structure_node_ids")
        or block.get("bilingual_counterpart_group_id")
        for block in blocks
    ):
        return next(
            (
                str(block.get("text") or block.get("raw_text") or "").strip()
                for block in blocks
                if str(block.get("text") or block.get("raw_text") or "").strip()
            ),
            "",
        )
    return _block_text(blocks)


def _deterministic_evidence_for_segment(
    evidence: tuple[dict[str, Any], ...],
    *,
    segment: _DirectContentSegment,
    owner_segment_count: int,
) -> tuple[dict[str, Any], ...]:
    if not evidence:
        return ()
    segment_source_ids = {
        str(ref.get("source_id") or "")
        for ref in segment.source_refs
        if str(ref.get("source_id") or "")
    }
    matched = tuple(
        item
        for item in evidence
        if segment_source_ids
        & {
            str(ref.get("source_id") or "")
            for ref in _dict_items(item.get("source_refs"))
            if str(ref.get("source_id") or "")
        }
    )
    if matched:
        return matched
    return evidence if owner_segment_count == 1 else ()


def _normalize_requirement_entry(
    item: dict[str, Any],
    *,
    index: int,
) -> _RequirementEntry | None:
    source_spans = _dict_items(item.get("source_spans"))
    target = _dict(item.get("target_structure"))
    owner_id = str(
        item.get("owner_structure_node_id")
        or item.get("structure_node_id")
        or target.get("id")
        or next(
            (
                span.get("owner_structure_node_id")
                for span in source_spans
                if span.get("owner_structure_node_id")
            ),
            "",
        )
        or ""
    )
    stable_id = str(
        item.get("requirement_tree_node_id")
        or _stable_requirement_leaf_id(
            owner_id=owner_id,
            source_spans=source_spans,
            source_refs=_dict_items(item.get("source_refs")),
            fallback_id=str(
                item.get("atomization_node_id")
                or item.get("publication_entry_id")
                or item.get("review_node_id")
                or item.get("id")
                or f"requirement-{index}"
            ),
        )
    )
    kind = str(item.get("kind") or item.get("entry_kind") or "").upper()
    if kind in {"ROLLUP", "HEADING"}:
        return None
    decision_mode = str(item.get("decision_mode") or "").upper()
    projection_role = str(item.get("role") or "").upper()
    if decision_mode == "NONE" or projection_role in {"CONTENT", "REFERENCE"}:
        return None
    include_in_view = item.get("include_in_view") is not False
    if not include_in_view:
        return None
    summary = _first_non_blank(
        str(item.get("published_statement") or ""),
        str(item.get("atomic_statement") or ""),
        str(item.get("source_text") or ""),
        str(item.get("text") or ""),
        str(item.get("requirement_text") or ""),
        str(item.get("display_text") or ""),
    )
    if not summary:
        return None
    label = _first_non_blank(
        str(item.get("req_code") or ""),
        str(item.get("title") or ""),
        summary,
        "需求条目",
    )
    label = _requirement_label(label, summary)
    source_refs = tuple(_dict_items(item.get("source_refs")))
    if not source_refs and source_spans:
        source_refs = tuple(
            {
                "kind": "TEXT_RANGE",
                "source_id": str(span.get("semantic_unit_id") or ""),
                "owner_structure_node_id": str(
                    span.get("owner_structure_node_id") or ""
                ),
                "start_offset": span.get("start_offset"),
                "end_offset": span.get("end_offset"),
            }
            for span in source_spans
        )
    content_blocks = tuple(_dict_items(item.get("content_blocks")))
    table_refs = tuple(_dict_items(item.get("table_refs")))
    status_token = str(
        item.get("status")
        or item.get("review_status")
        or item.get("refinement_state")
        or ""
    ).upper()
    semantic_statuses = {
        str(value).upper() for value in _list(item.get("semantic_statuses"))
    }
    reason_codes = {str(value).lower() for value in _list(item.get("reason_codes"))}
    status = (
        "REVIEWED"
        if item.get("confirmed") is True
        or status_token in {"REVIEWED", "CONFIRMED", "ACCEPTED", "REJECTED"}
        else "NEEDS_ATTENTION"
        if status_token in {"FAILED", "BLOCKED", "NEEDS_ATTENTION", "UNRESOLVED"}
        or "UNRESOLVED" in semantic_statuses
        or str(item.get("semantic_class") or "").upper() == "UNRESOLVED"
        or any(
            code
            in {
                "no_confident_rule",
                "text_resolution_failed",
                "conflicting_semantic_signals",
                "conflicting_ancestor_semantic_signals",
            }
            for code in reason_codes
        )
        else "PENDING"
    )
    requirement_entry_id = (
        str(
            item.get("requirement_entry_id")
            or item.get("requirement_id")
            or item.get("database_requirement_id")
            or ""
        )
        or None
    )
    workflow_node_id = (
        str(
            item.get("review_node_id")
            or item.get("atomization_node_id")
            or item.get("publication_entry_id")
            or item.get("workflow_node_id")
            or item.get("id")
            or ""
        )
        or None
    )
    return _RequirementEntry(
        stable_id=stable_id,
        requirement_entry_id=requirement_entry_id,
        workflow_node_id=workflow_node_id,
        structure_node_id=owner_id or None,
        owner_kind=str(target.get("kind") or "").upper(),
        source_order=_nonnegative_int(item.get("source_order"), default=index),
        source_offset=_nonnegative_int(item.get("source_offset")),
        label=label,
        summary=summary,
        page_start=_positive_int(item.get("page_start") or item.get("source_page")),
        page_end=_positive_int(
            item.get("page_end")
            or item.get("source_page_end")
            or item.get("page_start")
            or item.get("source_page")
        ),
        source_refs=source_refs,
        status=status,
        classification=str(
            item.get("classification")
            or item.get("requirement_subtype")
            or item.get("entry_type")
            or item.get("requirement_type")
            or "REQUIREMENT"
        ).upper(),
        projection_role=str(item.get("projection_role") or "REQUIREMENT").upper(),
        table_record_id=str(item.get("table_record_id") or "") or None,
        table_evidence_binding_id=str(item.get("table_evidence_binding_id") or "")
        or None,
        deterministic_rule_evidence=tuple(
            _dict_items(item.get("deterministic_rule_evidence"))
        ),
        include_in_view=True,
        content_blocks=content_blocks,
        table_refs=table_refs,
        figure_refs=tuple(_dict_items(item.get("figure_refs"))),
    )


def _dedupe_exact_table_text_channels(
    entries: list[_RequirementEntry],
) -> list[_RequirementEntry]:
    """Collapse only the proven dual PDF channels for one adjacent statement."""

    ordered = sorted(
        entries,
        key=lambda item: (item.source_order, item.source_offset, item.stable_id),
    )
    output: list[_RequirementEntry] = []
    for entry in ordered:
        if output:
            previous = output[-1]
            same_statement = (
                " ".join(previous.summary.split()) == " ".join(entry.summary.split())
                and previous.page_start == entry.page_start
                and previous.page_end == entry.page_end
                and abs(previous.source_order - entry.source_order) <= 1
            )
            dual_channels = {
                previous.owner_kind,
                entry.owner_kind,
            } == {"TABLE_ROW", "CONTENT"}
            if same_statement and dual_channels:
                if entry.owner_kind == "CONTENT":
                    output[-1] = entry
                continue
        output.append(entry)
    return output


def _requirement_label(candidate: str, summary: str) -> str:
    compact = " ".join(candidate.split())
    if not re.fullmatch(r"\d+(?:[.、）)])?", compact) and compact.upper() != "CONTENT":
        return compact
    lines = [" ".join(line.split()) for line in summary.splitlines() if line.strip()]
    for line in lines:
        without_number = re.sub(r"^\d+(?:[.、）)]|\s)+", "", line).strip()
        if without_number and without_number != compact:
            return without_number
    return compact or "需求条目"


def _requirement_tree_node_id(entry: _RequirementEntry) -> str:
    return f"requirement:{entry.stable_id}"


def _stable_requirement_leaf_id(
    *,
    owner_id: str,
    source_spans: list[dict[str, Any]],
    source_refs: list[dict[str, Any]],
    fallback_id: str,
) -> str:
    """Keep leaf identity stable across review, atomization and publication.

    Provider/run IDs are intentionally excluded. Exact owner plus source spans
    survive Partial, Final and immutable Requirement Set projections.
    """

    binding: list[dict[str, Any]] = []
    for span in source_spans:
        binding.append(
            {
                "semantic_unit_id": str(span.get("semantic_unit_id") or ""),
                "owner_structure_node_id": str(
                    span.get("owner_structure_node_id") or owner_id
                ),
                "start_offset": _nonnegative_int(span.get("start_offset")),
                "end_offset": _nonnegative_int(span.get("end_offset")),
            }
        )
    if not binding:
        for ref in source_refs:
            binding.append(
                {
                    "kind": str(ref.get("kind") or ""),
                    "source_id": str(ref.get("source_id") or ""),
                    "owner_structure_node_id": str(
                        ref.get("owner_structure_node_id") or owner_id
                    ),
                    "start_offset": _nonnegative_int(ref.get("start_offset")),
                    "end_offset": _nonnegative_int(ref.get("end_offset")),
                }
            )
    if not binding:
        return fallback_id
    canonical = json.dumps(
        {
            "owner_structure_node_id": owner_id,
            "source_binding": binding,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"source-{hashlib.sha256(canonical).hexdigest()[:32]}"


_TOC_NUMBER_CORE = (
    r"(?:\d+(?:\.\d+)*|[A-Z](?:\.\d+)+|[A-Z]|"
    r"(?:附录|附件)\s*[A-Z0-9一二三四五六七八九十]+|"
    r"(?:annex|appendix)\s+[A-Z0-9]+|"
    r"第[一二三四五六七八九十百0-9]+[章节编部分])"
)
_TOC_NUMBER_ONLY_PATTERN = re.compile(
    rf"^\s*(?P<number>{_TOC_NUMBER_CORE})\s*[.．、]?\s*$",
    re.IGNORECASE,
)
_TOC_NUMBER_TITLE_PATTERN = re.compile(
    rf"^\s*(?P<number>{_TOC_NUMBER_CORE})(?:\s+|[、．](?=\s*))(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_TOC_LEADER_TARGET_PATTERN = re.compile(
    r"^(?P<body>.*?)(?:\.{2,}|…+|·{2,}|_{2,}|\s{2,})"
    r"(?P<page>\d+|[IVXLCDM]+)\s*$",
    re.IGNORECASE,
)
_TOC_PLAIN_TARGET_PATTERN = re.compile(
    r"^(?P<body>.+?)\s+(?P<page>\d+|[IVXLCDM]+)\s*$",
    re.IGNORECASE,
)
_TOC_PAGE_ONLY_PATTERN = re.compile(r"^\s*(?P<page>\d+|[IVXLCDM]+)\s*$", re.I)


def attach_toc_navigation_entries(
    nodes: list[dict[str, Any]],
    *,
    source_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach source-bound TOC navigation without creating requirements.

    The parser accepts both ordinary one-line entries and PDF extraction where
    the printed number, title and target page arrive in separate source nodes.
    It deliberately runs only on proven TOC structure scopes.
    """

    output = [dict(node) for node in nodes]
    candidates = tuple(
        node
        for node in output
        if str(node.get("kind") or "").upper()
        in {"SECTION", "CLAUSE", "GROUP", "LIST_GROUP"}
        and not _is_toc_navigation_node(node)
    )
    for node in output:
        if not _is_toc_navigation_node(node):
            continue
        existing = _dict_items(node.get("toc_entries"))
        entries = existing or _parse_toc_navigation_entries(
            node,
            source_index=source_index,
            candidates=candidates,
        )
        resolved: list[dict[str, Any]] = []
        for entry in entries:
            current = dict(entry)
            target = _resolve_toc_target(current, candidates=candidates)
            if target is not None:
                current["target_structure_node_id"] = str(target.get("id") or "")
                # Navigation uses the physical PDF page of the proven target,
                # never the printed page label found in TOC prose.
                current["target_page"] = _positive_int(target.get("page_start"))
            else:
                current["target_structure_node_id"] = None
                current["target_page"] = None
            resolved.append(current)
        node["toc_entries"] = resolved
    return output


def _is_toc_navigation_node(node: dict[str, Any]) -> bool:
    if str(node.get("boundary_basis") or "").upper() == "TOC_BLOCK":
        return True
    return any(
        str(ref.get("source_kind") or "").upper() == "TOC_ENTRY"
        for ref in _dict_items(node.get("source_refs"))
    )


def _parse_toc_navigation_entries(
    node: dict[str, Any],
    *,
    source_index: dict[str, dict[str, Any]],
    candidates: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    fragments: list[tuple[str, tuple[dict[str, Any], ...]]] = []
    for ref in _dict_items(node.get("source_refs")):
        source_id = str(ref.get("source_id") or "")
        text = str(source_index.get(source_id, {}).get("text") or "")
        for raw_line in text.splitlines() or (text,):
            line = unicodedata.normalize("NFKC", raw_line).strip()
            if line:
                fragments.append((line, (dict(ref),)))

    entries: list[dict[str, Any]] = []
    pending_number = ""
    pending_title_parts: list[str] = []
    pending_refs: list[dict[str, Any]] = []

    def reset() -> None:
        nonlocal pending_number, pending_title_parts, pending_refs
        pending_number = ""
        pending_title_parts = []
        pending_refs = []

    def emit(
        *,
        number: str,
        title: str,
        printed_page: str,
        refs: Iterable[dict[str, Any]],
    ) -> None:
        compact_title = " ".join(title.split()).strip(" .…·_")
        if not compact_title or _is_toc_heading_text(compact_title):
            reset()
            return
        printed_page_label = " ".join(printed_page.split())
        entries.append(
            {
                "number": " ".join(number.split()),
                "title": compact_title,
                "printed_page_label": printed_page_label,
                "target_page": None,
                "source_order": _structure_physical_reading_key(node)[0] + len(entries),
                "source_refs": tuple(_dedupe_dicts(refs)),
            }
        )
        reset()

    for line, line_refs in fragments:
        if _is_toc_heading_text(line) and not pending_number:
            continue
        target_match = _TOC_LEADER_TARGET_PATTERN.match(line)
        if target_match is None and (pending_number or pending_title_parts):
            target_match = _TOC_PLAIN_TARGET_PATTERN.match(line)
        if target_match is not None:
            body = str(target_match.group("body") or "").strip()
            parsed_number, parsed_title = _toc_number_and_title(body)
            if (
                pending_number
                and pending_title_parts
                and parsed_number
                and parsed_title
                and _normalize_toc_number(parsed_number)
                != _normalize_toc_number(pending_number)
            ):
                continuation_title = " ".join((*pending_title_parts, body)).strip()
                continuation_target = _resolve_toc_target(
                    {
                        "number": pending_number,
                        "title": continuation_title,
                    },
                    candidates=candidates,
                )
                if continuation_target is None:
                    previous_number = pending_number
                    previous_title = " ".join(pending_title_parts)
                    previous_refs = tuple(pending_refs)
                    emit(
                        number=previous_number,
                        title=previous_title,
                        printed_page="",
                        refs=previous_refs,
                    )
                    emit(
                        number=parsed_number,
                        title=parsed_title,
                        printed_page=str(target_match.group("page") or ""),
                        refs=line_refs,
                    )
                    continue
            if pending_number:
                number = pending_number
                # A proven number-only fragment owns the following title line.
                # The title may itself begin with a number-shaped token such as
                # "Appendix B" or "附录 2"; do not let that token replace the
                # pending section number.  Only strip it when the line repeats
                # the exact same number.
                title = (
                    parsed_title
                    if parsed_number
                    and _normalize_toc_number(parsed_number)
                    == _normalize_toc_number(pending_number)
                    else body
                )
            else:
                number, title = parsed_number, parsed_title
            combined_title = " ".join((*pending_title_parts, title or body)).strip()
            emit(
                number=number,
                title=combined_title,
                printed_page=str(target_match.group("page") or ""),
                refs=(*pending_refs, *line_refs),
            )
            continue

        page_match = _TOC_PAGE_ONLY_PATTERN.match(line)
        if page_match is not None and pending_title_parts:
            emit(
                number=pending_number,
                title=" ".join(pending_title_parts),
                printed_page=str(page_match.group("page") or ""),
                refs=(*pending_refs, *line_refs),
            )
            continue

        number_match = _TOC_NUMBER_ONLY_PATTERN.match(line)
        if number_match is not None:
            if pending_number and pending_title_parts:
                emit(
                    number=pending_number,
                    title=" ".join(pending_title_parts),
                    printed_page="",
                    refs=pending_refs,
                )
            pending_number = str(number_match.group("number") or "").strip()
            pending_title_parts = []
            pending_refs = [*line_refs]
            continue

        if pending_number:
            parsed_number, parsed_title = _toc_number_and_title(line)
            pending_title_parts.append(
                parsed_title
                if parsed_number
                and _normalize_toc_number(parsed_number)
                == _normalize_toc_number(pending_number)
                else line
            )
            pending_refs.extend(line_refs)
            continue

        number, title = _toc_number_and_title(line)
        if number:
            pending_number = number
            pending_title_parts = [title] if title else []
            pending_refs = [*line_refs]
        elif pending_number:
            pending_title_parts.append(line)
            pending_refs.extend(line_refs)

    if pending_title_parts:
        emit(
            number=pending_number,
            title=" ".join(pending_title_parts),
            printed_page="",
            refs=pending_refs,
        )

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        key = (
            _normalize_toc_number(str(entry.get("number") or "")),
            _normalize_toc_text(str(entry.get("title") or "")),
            str(entry.get("printed_page_label") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _toc_number_and_title(text: str) -> tuple[str, str]:
    match = _TOC_NUMBER_TITLE_PATTERN.match(text)
    if match is None:
        return "", " ".join(text.split())
    return (
        " ".join(str(match.group("number") or "").split()),
        " ".join(str(match.group("title") or "").split()),
    )


def _is_toc_heading_text(text: str) -> bool:
    normalized = _normalize_toc_text(text)
    return normalized in {"目录", "contents", "tableofcontents"}


def _toc_page_number(value: str) -> int | None:
    value = value.strip().upper()
    if value.isdigit():
        return _positive_int(value)
    roman_values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    if not value or any(character not in roman_values for character in value):
        return None
    total = 0
    previous = 0
    for character in reversed(value):
        current = roman_values[character]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total or None


def _resolve_toc_target(
    entry: dict[str, Any],
    *,
    candidates: tuple[dict[str, Any], ...],
) -> dict[str, Any] | None:
    number = _normalize_toc_number(str(entry.get("number") or ""))
    title = _normalize_toc_text(str(entry.get("title") or ""))
    if number:
        numbered = tuple(
            candidate
            for candidate in candidates
            if _normalize_toc_number(
                str(candidate.get("explicit_label") or "")
                or _toc_number_and_title(str(candidate.get("label") or ""))[0]
            )
            == number
        )
        exact_title = tuple(
            candidate
            for candidate in numbered
            if _toc_candidate_title(candidate) == title
        )
        if title:
            return exact_title[0] if len(exact_title) == 1 else None
        return numbered[0] if len(numbered) == 1 else None
    titled = tuple(
        candidate
        for candidate in candidates
        if _toc_candidate_title(candidate) == title
    )
    return titled[0] if len(titled) == 1 else None


def _toc_candidate_title(node: dict[str, Any]) -> str:
    label = str(node.get("label") or "")
    explicit = str(node.get("explicit_label") or "").strip()
    if explicit and label.startswith(explicit):
        label = label[len(explicit) :].lstrip(" 、.．)）")
    else:
        _, possible_title = _toc_number_and_title(label)
        label = possible_title
    return _normalize_toc_text(label)


def _normalize_toc_number(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s、.．)）]+$", "", normalized)


def _normalize_toc_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized)


def _content_by_structure_owner(
    payload: dict[str, Any],
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
) -> dict[str, tuple[dict[str, Any], ...]]:
    tables = _dict_items(payload.get("tables"))
    tables_by_id = {
        str(table.get("id") or ""): table
        for table in tables
        if str(table.get("id") or "")
    }
    tables_by_source_id = {
        str(table.get("source_node_id") or ""): table
        for table in tables
        if str(table.get("source_node_id") or "")
    }
    exact_context_by_owner: dict[str, tuple[dict[str, Any], ...]] = {}
    # A review group is authoritative only for its own declared boundary.
    # Its complete context may intentionally cover a whole parent chapter, so
    # it must not be copied wholesale into every descendant structure node.
    for collection_name in ("review_groups", "requirement_groups"):
        for item in _dict_items(payload.get(collection_name)):
            owner_id = _presentation_owner_id(item)
            if not owner_id or owner_id in exact_context_by_owner:
                continue
            blocks = _dict_items(item.get("context_blocks")) or _dict_items(
                item.get("content_blocks")
            )
            if not blocks:
                text = _first_non_blank(
                    str(item.get("context_text") or ""),
                    str(item.get("text") or ""),
                )
                if text:
                    blocks = [{"kind": "paragraph", "text": text}]
            if blocks:
                exact_context_by_owner[owner_id] = tuple(
                    _hydrate_table_content_blocks(
                        _dedupe_dicts(blocks),
                        tables_by_id=tables_by_id,
                        tables_by_source_id=tables_by_source_id,
                    )
                )

    item_fallback_by_owner: dict[str, tuple[dict[str, Any], ...]] = {}
    for item in _dict_items(payload.get("items")):
        owner_id = _presentation_owner_id(item)
        if not owner_id or owner_id in item_fallback_by_owner:
            continue
        blocks = _dict_items(item.get("content_blocks"))
        if not blocks:
            # Item.context_text is full parent context for semantic review.  It
            # is not the selected Structure node's own display boundary.
            text = str(item.get("text") or "").strip()
            if text:
                blocks = [{"kind": "paragraph", "text": text}]
        if blocks:
            item_fallback_by_owner[owner_id] = tuple(
                _hydrate_table_content_blocks(
                    _dedupe_dicts(blocks),
                    tables_by_id=tables_by_id,
                    tables_by_source_id=tables_by_source_id,
                )
            )

    source_content_by_owner = _source_bound_structure_content_by_owner(
        all_structure_by_id=all_structure_by_id,
        visible_structure_ids=visible_structure_ids,
        tables_by_id=tables_by_id,
        tables_by_source_id=tables_by_source_id,
    )
    ordered_visible_ids = sorted(
        visible_structure_ids,
        key=lambda node_id: (
            *_structure_physical_reading_key(all_structure_by_id[node_id]),
            node_id,
        ),
    )
    collected: dict[str, tuple[dict[str, Any], ...]] = {}
    for node_id in ordered_visible_ids:
        exact = exact_context_by_owner.get(node_id)
        if exact:
            collected[node_id] = exact
            continue
        ancestor_id = _nearest_context_owner(
            node_id,
            exact_context_by_owner=exact_context_by_owner,
            all_structure_by_id=all_structure_by_id,
        )
        if ancestor_id:
            sliced = _slice_ancestor_context_for_node(
                node_id,
                context_blocks=exact_context_by_owner[ancestor_id],
                ordered_visible_ids=ordered_visible_ids,
                all_structure_by_id=all_structure_by_id,
            )
            if sliced:
                collected[node_id] = sliced
                continue
        source_content = source_content_by_owner.get(node_id)
        if source_content:
            collected[node_id] = source_content
            continue
        fallback = item_fallback_by_owner.get(node_id)
        if fallback:
            collected[node_id] = fallback
    return collected


def _source_bound_structure_content_by_owner(
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
    tables_by_id: dict[str, dict[str, Any]],
    tables_by_source_id: dict[str, dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Project source-bound raw content to its nearest visible owner.

    Semantic review groups are deliberately sparse: a structurally valid
    clause may own ordinary source content without producing a requirement
    candidate.  Walking the real parent chain preserves the Effective Structure
    boundary exactly, while stopping at the first visible chapter or clause
    prevents CONTENT, TABLE, and FIGURE from becoming navigation layers or
    being duplicated across ancestor nodes.
    """

    blocks_by_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ordered_nodes = sorted(
        all_structure_by_id.values(),
        key=lambda item: (
            *_structure_physical_reading_key(item),
            str(item.get("id") or ""),
        ),
    )
    for node in ordered_nodes:
        kind = str(node.get("kind") or "").upper()
        if kind not in _SOURCE_CONTENT_KINDS:
            continue
        if kind == "CONTENT" and _has_structure_ancestor_kind(
            str(node.get("parent_id") or ""),
            ancestor_kinds={"TABLE", "FIGURE"},
            all_structure_by_id=all_structure_by_id,
        ):
            # The authoritative TABLE/FIGURE block already carries its native
            # descendants. Re-emitting cell/figure child text as paragraphs
            # makes the middle pane display the same evidence twice and loses
            # its layout. Table-record review leaves are projected separately
            # from the table topology and are intentionally unaffected here.
            continue
        block = _source_bound_structure_content_block(node)
        if block is None:
            continue
        current_id = str(node.get("id") or "")
        visited: set[str] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            if current_id in visible_structure_ids:
                blocks_by_owner[current_id].append(block)
                break
            current_id = str(
                all_structure_by_id.get(current_id, {}).get("parent_id") or ""
            )

    return {
        owner_id: tuple(
            _hydrate_table_content_blocks(
                _dedupe_dicts(blocks),
                tables_by_id=tables_by_id,
                tables_by_source_id=tables_by_source_id,
            )
        )
        for owner_id, blocks in blocks_by_owner.items()
        if blocks
    }


def _source_bound_structure_content_block(
    node: dict[str, Any],
) -> dict[str, Any] | None:
    bindings = _structure_source_bindings(node)
    if not bindings:
        return None
    kind = str(node.get("kind") or "").upper()
    label = str(node.get("label") or "").strip()
    source_fact_kinds = _source_fact_kinds(node)
    media_kind = (
        "figure"
        if "FIGURE" in source_fact_kinds
        else "image"
        if "IMAGE" in source_fact_kinds
        else ""
    )
    if kind == "CONTENT" and not label and not media_kind:
        return None

    source_id, start_offset, end_offset = bindings[0]
    physical_order, physical_offset = _structure_physical_reading_key(node)
    block: dict[str, Any] = {
        "kind": media_kind
        or {
            "CONTENT": "text",
            "TABLE": "table",
            "FIGURE": "figure",
        }[kind],
        "source_id": source_id,
        "structure_node_id": str(node.get("id") or ""),
        "source_order": physical_order,
        "source_offset": physical_offset,
        "source_refs": _dict_items(node.get("source_refs")),
    }
    if label and not media_kind:
        block["text"] = label
    elif label and " ".join(label.split()).casefold() not in {
        "content",
        "image",
        "figure",
        "图片",
        "图像",
    }:
        block["alt"] = label
    page_start = _positive_int(node.get("page_start"))
    page_end = _positive_int(node.get("page_end"))
    if page_start is not None:
        block["page_start"] = page_start
    if page_end is not None:
        block["page_end"] = page_end
    if start_offset is not None:
        block["start_offset"] = start_offset
    if end_offset is not None:
        block["end_offset"] = end_offset
    return block


def _content_blocks_with_source_order(
    blocks: Iterable[dict[str, Any]],
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
    fallback_source_order: int,
    event_scope_id: str,
    sort_physical: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Attach stable reading keys and optionally materialize physical order."""

    structure_reading_key_by_id = {
        node_id: _structure_physical_reading_key(node)
        for node_id, node in all_structure_by_id.items()
    }
    source_reading_keys_by_id: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for node_id, node in all_structure_by_id.items():
        reading_key = structure_reading_key_by_id[node_id]
        for source_id, start_offset, _ in _structure_source_bindings(node):
            if source_id:
                source_reading_keys_by_id[source_id].append(
                    (
                        reading_key[0],
                        start_offset if start_offset is not None else reading_key[1],
                    )
                )

    annotated: list[tuple[int, int, int, dict[str, Any], str]] = []
    for index, block in enumerate(blocks):
        current = dict(block)
        structure_node_id = str(current.get("structure_node_id") or "")
        source_id = str(current.get("source_id") or current.get("source_node_id") or "")
        if structure_node_id in structure_reading_key_by_id:
            reading_key = structure_reading_key_by_id[structure_node_id]
        elif source_reading_keys_by_id.get(source_id):
            block_start = _optional_nonnegative_int(current.get("start_offset"))
            candidates = tuple(source_reading_keys_by_id[source_id])
            if block_start is not None:
                exact_candidates = tuple(
                    candidate for candidate in candidates if candidate[1] == block_start
                )
                candidates = exact_candidates or (
                    (min(order for order, _ in candidates), block_start),
                )
            reading_key = min(candidates)
        else:
            explicit_order = _optional_nonnegative_int(current.get("source_order"))
            explicit_offset = _optional_nonnegative_int(current.get("source_offset"))
            # Presentation-only fallback blocks have no immutable source
            # binding. Their tuple order is deterministic, but it is explicitly
            # a legacy fallback rather than editable Structure order authority.
            reading_key = (
                explicit_order
                if explicit_order is not None
                else _nonnegative_int(fallback_source_order) + index,
                explicit_offset or 0,
            )
        current["source_order"] = reading_key[0]
        # Zero is an authoritative exact-span offset.  Omitting it makes old
        # clients substitute tuple position and can invert two spans from the
        # same SourceIR fact after a Structure edit.
        current["source_offset"] = reading_key[1]
        stable_identity = str(
            current.get("id")
            or current.get("table_id")
            or current.get("source_id")
            or current.get("source_node_id")
            or current.get("asset_id")
            or current.get("cell_id")
            or current.get("visual_observation_id")
            or ""
        )
        event_payload = {
            "kind": str(current.get("kind") or current.get("type") or "").lower(),
            "stable_identity": stable_identity,
            "event_scope_id": "" if stable_identity else event_scope_id,
            "structure_node_id": str(current.get("structure_node_id") or ""),
            "source_order": reading_key[0],
            "source_offset": reading_key[1],
            "start_offset": _optional_nonnegative_int(current.get("start_offset")),
            "end_offset": _optional_nonnegative_int(current.get("end_offset")),
            "page": _positive_int(
                current.get("page_number")
                or current.get("page_start")
                or current.get("page")
            ),
            "text": str(current.get("text") or current.get("raw_text") or ""),
        }
        event_digest = hashlib.sha256(
            json.dumps(
                event_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]
        annotated.append(
            (
                reading_key[0],
                reading_key[1],
                index,
                current,
                event_digest,
            )
        )

    ordered: list[dict[str, Any]] = []
    event_occurrences: dict[str, int] = defaultdict(int)
    normalized = (
        sorted(annotated, key=lambda item: (item[0], item[1], item[2]))
        if sort_physical
        else annotated
    )
    for _, _, _, current, event_digest in normalized:
        occurrence = event_occurrences[event_digest]
        event_occurrences[event_digest] += 1
        current["reading_event_id"] = f"reading-{event_digest}-{occurrence}"
        ordered.append(current)
    return tuple(ordered)


def _reading_stream_text(blocks: tuple[dict[str, Any], ...]) -> str:
    """Summarize text and folded captions by their authoritative source key."""

    events: list[tuple[int, int, int, int, str]] = []
    for index, block in enumerate(blocks):
        source_order = _nonnegative_int(block.get("source_order"))
        source_offset = _nonnegative_int(block.get("source_offset"))
        text = str(block.get("text") or block.get("raw_text") or "").strip()
        if text:
            events.append((source_order, source_offset, 1, index, text))

        caption = str(block.get("caption") or "").strip()
        if not caption:
            continue
        caption_order = _optional_nonnegative_int(block.get("caption_source_order"))
        caption_offset = _optional_nonnegative_int(block.get("caption_source_offset"))
        events.append(
            (
                caption_order if caption_order is not None else source_order,
                caption_offset if caption_offset is not None else source_offset,
                0,
                index,
                caption,
            )
        )
    return "\n".join(event[-1] for event in sorted(events))


def _structure_physical_reading_key(node: dict[str, Any]) -> tuple[int, int]:
    physical_order = _optional_nonnegative_int(node.get("physical_source_order"))
    physical_offset = _optional_nonnegative_int(node.get("physical_source_offset"))
    return (
        physical_order
        if physical_order is not None
        else _nonnegative_int(node.get("order")),
        physical_offset or 0,
    )


def _projection_reading_key(
    *,
    projection: _MutableProjection,
    content_blocks: tuple[dict[str, Any], ...],
    all_structure_by_id: dict[str, dict[str, Any]],
) -> tuple[int, int]:
    block_keys = tuple(
        (
            _nonnegative_int(block.get("source_order")),
            _nonnegative_int(block.get("source_offset")),
        )
        for block in content_blocks
    )
    if (
        projection.projection_role not in {"STRUCTURE", "STRUCTURE_PROXY"}
        and block_keys
    ):
        return min(block_keys)
    structure_node = all_structure_by_id.get(projection.structure_node_id or "")
    if structure_node is not None:
        return _structure_physical_reading_key(structure_node)
    return projection.source_order, projection.source_offset


def _bind_figure_captions_in_blocks(
    blocks: Iterable[dict[str, Any]],
    *,
    bindings_by_media_id: dict[str, _FigureCaptionBinding],
) -> tuple[dict[str, Any], ...]:
    """Attach pure figure labels to media and suppress only the duplicate row."""

    current_blocks = tuple(dict(block) for block in blocks)
    present_media_ids = {
        str(block.get("structure_node_id") or "")
        for block in current_blocks
        if str(block.get("kind") or "").lower() in {"figure", "image"}
    }
    attached_caption_ids = {
        binding.caption_structure_node_id
        for media_id, binding in bindings_by_media_id.items()
        if media_id in present_media_ids
    }
    output: list[dict[str, Any]] = []
    for block in current_blocks:
        structure_node_id = str(block.get("structure_node_id") or "")
        if structure_node_id in attached_caption_ids:
            continue
        binding = bindings_by_media_id.get(structure_node_id)
        if binding is not None and str(block.get("kind") or "").lower() in {
            "figure",
            "image",
        }:
            block = {
                **block,
                "caption": binding.caption,
                "caption_source_refs": binding.caption_source_refs,
                "caption_source_order": binding.caption_source_order,
                "caption_source_offset": binding.caption_source_offset,
            }
        output.append(block)
    return tuple(output)


def _ordered_editable_structure_node_ids(
    blocks: tuple[dict[str, Any], ...],
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
    owner_structure_node_id: str | None,
) -> tuple[str, ...]:
    ordered = sorted(
        enumerate(blocks),
        key=lambda pair: (
            _nonnegative_int(pair[1].get("source_order")),
            _nonnegative_int(pair[1].get("source_offset")),
            pair[0],
        ),
    )
    output: list[str] = []
    seen: set[str] = set()
    for _, block in ordered:
        node_id = str(block.get("structure_node_id") or "")
        node = all_structure_by_id.get(node_id)
        if (
            not node_id
            or node_id == owner_structure_node_id
            or node_id in seen
            or node is None
            or str(node.get("kind") or "").upper() != "CONTENT"
        ):
            continue
        seen.add(node_id)
        output.append(node_id)
    return tuple(output)


def _normalized_toc_entries(
    entries: Iterable[dict[str, Any]],
    *,
    fallback_source_order: int,
    valid_target_ids: set[str],
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    entry_id_occurrences: dict[str, int] = defaultdict(int)
    for index, entry in enumerate(entries):
        title = " ".join(str(entry.get("title") or "").split())
        if not title:
            continue
        target_id = str(entry.get("target_structure_node_id") or "")
        number = " ".join(str(entry.get("number") or "").split())
        printed_page_label = " ".join(
            str(entry.get("printed_page_label") or "").split()
        )
        source_order = _nonnegative_int(
            entry.get("source_order"),
            default=_nonnegative_int(fallback_source_order) + index,
        )
        source_refs = tuple(_dict_items(entry.get("source_refs")))
        raw_entry_id = str(entry.get("entry_id") or "").strip()
        if raw_entry_id:
            entry_id_base = raw_entry_id
        else:
            canonical = json.dumps(
                {
                    "number": number,
                    "title": title,
                    "printed_page_label": printed_page_label,
                    "source_order": source_order,
                    "source_refs": source_refs,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            entry_id_base = f"toc-entry-{hashlib.sha256(canonical).hexdigest()[:32]}"
        occurrence = entry_id_occurrences[entry_id_base]
        entry_id_occurrences[entry_id_base] += 1
        entry_id = entry_id_base if occurrence == 0 else f"{entry_id_base}-{occurrence}"
        current: dict[str, Any] = {
            "entry_id": entry_id,
            "number": number,
            "title": title,
            "printed_page_label": printed_page_label,
            "target_page": _positive_int(entry.get("target_page")),
            "source_order": source_order,
            "source_refs": source_refs,
            "target_structure_node_id": (
                target_id if target_id in valid_target_ids else None
            ),
        }
        normalized.append(current)
    return tuple(normalized)


def _presentation_owner_id(item: dict[str, Any]) -> str:
    target = _dict(item.get("target_structure"))
    return str(
        item.get("boundary_structure_node_id")
        or item.get("owner_structure_node_id")
        or item.get("structure_node_id")
        or target.get("id")
        or ""
    )


def _nearest_context_owner(
    node_id: str,
    *,
    exact_context_by_owner: dict[str, tuple[dict[str, Any], ...]],
    all_structure_by_id: dict[str, dict[str, Any]],
) -> str:
    current_id = str(all_structure_by_id.get(node_id, {}).get("parent_id") or "")
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        if current_id in exact_context_by_owner:
            return current_id
        current_id = str(all_structure_by_id.get(current_id, {}).get("parent_id") or "")
    return ""


def _slice_ancestor_context_for_node(
    node_id: str,
    *,
    context_blocks: tuple[dict[str, Any], ...],
    ordered_visible_ids: list[str],
    all_structure_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    node = all_structure_by_id[node_id]
    start_index = _first_matching_block_index(context_blocks, node)
    if start_index is None:
        return ()
    end_index = len(context_blocks)
    # Structure order is parent-first, not necessarily physical source order.
    # A malformed or still-unresolved parent can therefore place a later child
    # before an intervening peer in the tree traversal.  Context ownership must
    # fail closed at the earliest grounded boundary in the source stream; it
    # must never let that topological ordering swallow the intervening peer.
    candidate_indexes: list[int] = []
    for candidate_id in ordered_visible_ids:
        if candidate_id == node_id:
            continue
        if _is_structure_descendant(
            candidate_id,
            ancestor_id=node_id,
            all_structure_by_id=all_structure_by_id,
        ):
            continue
        if _is_structure_descendant(
            node_id,
            ancestor_id=candidate_id,
            all_structure_by_id=all_structure_by_id,
        ):
            continue
        candidate = all_structure_by_id[candidate_id]
        candidate_index = _first_matching_block_index(
            context_blocks,
            candidate,
            minimum_index=start_index + 1,
        )
        if candidate_index is not None:
            candidate_indexes.append(candidate_index)
    if candidate_indexes:
        end_index = min(candidate_indexes)
    return tuple(context_blocks[start_index:end_index])


def _first_matching_block_index(
    blocks: tuple[dict[str, Any], ...],
    node: dict[str, Any],
    *,
    minimum_index: int = 0,
) -> int | None:
    bindings = _structure_source_bindings(node)
    if not bindings:
        return None
    for index in range(max(minimum_index, 0), len(blocks)):
        block = blocks[index]
        source_id = str(block.get("source_id") or block.get("source_node_id") or "")
        if not source_id:
            continue
        block_start = _optional_nonnegative_int(block.get("start_offset"))
        block_end = _optional_nonnegative_int(block.get("end_offset"))
        for binding_source_id, binding_start, binding_end in bindings:
            if source_id != binding_source_id:
                continue
            if (
                binding_start is not None
                and binding_end is not None
                and block_start is not None
                and block_end is not None
                and (block_end <= binding_start or block_start >= binding_end)
            ):
                continue
            return index
    return None


def _structure_source_bindings(
    node: dict[str, Any],
) -> tuple[tuple[str, int | None, int | None], ...]:
    bindings: list[tuple[str, int | None, int | None]] = []
    for span in _dict_items(node.get("source_spans")):
        source_ref = _dict(span.get("source_ref"))
        source_id = str(source_ref.get("source_id") or span.get("source_id") or "")
        if source_id:
            bindings.append(
                (
                    source_id,
                    _optional_nonnegative_int(span.get("start_offset")),
                    _optional_nonnegative_int(span.get("end_offset")),
                )
            )
    if bindings:
        return tuple(bindings)
    for ref in _dict_items(node.get("source_refs")):
        source_id = str(ref.get("source_id") or "")
        if source_id:
            bindings.append(
                (
                    source_id,
                    _optional_nonnegative_int(ref.get("start_offset")),
                    _optional_nonnegative_int(ref.get("end_offset")),
                )
            )
    return tuple(bindings)


def _structure_source_span_refs(
    node: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Expose a structure node's precise spans without sibling-level refs.

    Effective Structure may retain a broad or stale ``source_refs`` fallback
    while ``source_spans`` already identifies the exact terminal boundary.
    Review proxies consume the precise spans so their lineage cannot drift to
    the following sibling.
    """

    refs: list[dict[str, Any]] = []
    enriched_refs_by_source: dict[str, dict[str, Any]] = {}
    for ref in _dict_items(node.get("source_refs")):
        source_id = str(ref.get("source_id") or "")
        if not source_id:
            continue
        current = enriched_refs_by_source.get(source_id)
        if current is None or len(ref) > len(current):
            enriched_refs_by_source[source_id] = dict(ref)
    for span in _dict_items(node.get("source_spans")):
        span_source_ref = dict(_dict(span.get("source_ref")))
        source_id = str(span_source_ref.get("source_id") or span.get("source_id") or "")
        if not source_id:
            continue
        source_ref = {
            **enriched_refs_by_source.get(source_id, {}),
            **span_source_ref,
        }
        source_ref["source_id"] = source_id
        source_ref.setdefault("kind", "SOURCE_NODE")
        for key in (
            "start_offset",
            "end_offset",
            "page_number",
            "page_start",
            "page_end",
        ):
            if span.get(key) is not None:
                source_ref[key] = span[key]
        refs.append(source_ref)
    return tuple(_dedupe_dicts(refs))


def _structure_heading_source_refs(
    node: dict[str, Any],
    *,
    precise_refs: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Return source refs that still describe the structure node's title.

    Effective Structure can retain a broad fallback ref after an exact source
    span has isolated the real title.  In that case only fallback refs sharing
    the exact span's source are title evidence; unrelated refs must not pull a
    following sibling into the proxy.  Without exact spans, the structure
    node's own refs are the only available title lineage and must survive the
    compact ``STRUCTURE_PROXY`` fold.
    """

    refs = tuple(_dict_items(node.get("source_refs")))
    precise_source_ids = {
        str(ref.get("source_id") or "")
        for ref in precise_refs
        if str(ref.get("source_id") or "")
    }
    if not precise_source_ids:
        return refs
    return tuple(
        ref for ref in refs if str(ref.get("source_id") or "") in precise_source_ids
    )


def _is_structure_descendant(
    node_id: str,
    *,
    ancestor_id: str,
    all_structure_by_id: dict[str, dict[str, Any]],
) -> bool:
    current_id = str(all_structure_by_id.get(node_id, {}).get("parent_id") or "")
    visited: set[str] = set()
    while current_id and current_id not in visited:
        if current_id == ancestor_id:
            return True
        visited.add(current_id)
        current_id = str(all_structure_by_id.get(current_id, {}).get("parent_id") or "")
    return False


def _has_structure_ancestor_kind(
    node_id: str,
    *,
    ancestor_kinds: set[str],
    all_structure_by_id: dict[str, dict[str, Any]],
) -> bool:
    current_id = node_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        current = all_structure_by_id.get(current_id)
        if current is None:
            return False
        if str(current.get("kind") or "").upper() in ancestor_kinds:
            return True
        current_id = str(current.get("parent_id") or "")
    return False


def _content_page_numbers(
    blocks: tuple[dict[str, Any], ...],
) -> tuple[int, ...]:
    pages: set[int] = set()
    for block in blocks:
        location = _dict(block.get("location"))
        for value in (
            block.get("page_start"),
            block.get("page_end"),
            block.get("page_number"),
            location.get("page_number"),
        ):
            page = _positive_int(value)
            if page is not None:
                pages.add(page)
    return tuple(sorted(pages))


def _hydrate_table_content_blocks(
    blocks: Iterable[dict[str, Any]],
    *,
    tables_by_id: dict[str, dict[str, Any]],
    tables_by_source_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach the verified table grid to a presentation block.

    Context blocks intentionally carry only compact source bindings.  The tree
    read model is the UI boundary, though, so it must resolve that binding back
    to the authoritative table once instead of forcing the React client to
    guess across unrelated result collections.
    """

    hydrated: list[dict[str, Any]] = []
    for block in blocks:
        current = dict(block)
        if str(current.get("kind") or "").lower() != "table":
            hydrated.append(current)
            continue
        table_id = str(current.get("table_id") or current.get("id") or "")
        source_id = str(current.get("source_id") or current.get("source_node_id") or "")
        table = tables_by_id.get(table_id) or tables_by_source_id.get(source_id)
        if table is None:
            hydrated.append(current)
            continue
        resolved = {**table, **current}
        resolved["table_id"] = table_id or str(table.get("id") or "")
        resolved["source_id"] = source_id or str(table.get("source_node_id") or "")
        hydrated.append(resolved)
    return hydrated


def _table_refs_for_owner(
    *,
    owner_id: str,
    all_nodes: dict[str, dict[str, Any]],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    current_id = owner_id
    visited: set[str] = set()
    table_source_ids: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        current = all_nodes.get(current_id)
        if current is None:
            break
        if str(current.get("kind") or "").upper() == "TABLE":
            table_source_ids.update(
                str(ref.get("source_id") or "")
                for ref in _dict_items(current.get("source_refs"))
                if str(ref.get("source_id") or "")
            )
            break
        current_id = str(current.get("parent_id") or "")
    if not table_source_ids:
        return ()
    return tuple(
        {
            "kind": "table",
            "table_id": str(table.get("id") or ""),
            "source_id": str(table.get("source_node_id") or ""),
        }
        for table in _dict_items(payload.get("tables"))
        if str(table.get("source_node_id") or "") in table_source_ids
        and str(table.get("id") or "")
    )


def _table_blocks_for_projection(
    *,
    payload: dict[str, Any],
    table_projection: TableRecordProjectionV1,
) -> tuple[dict[str, Any], ...]:
    table_ids = set(table_projection.table_fact.table_ids)
    by_id = {
        str(table.get("id") or ""): table
        for table in _dict_items(payload.get("tables"))
        if str(table.get("id") or "")
    }
    return tuple(
        {"kind": "table", **by_id[table_id]}
        for table_id in table_projection.table_fact.table_ids
        if table_id in table_ids and table_id in by_id
    )


def _whole_table_review_node_id(
    table_projection: TableRecordProjectionV1,
) -> str:
    """Return a revision-independent identity for one logical whole table."""

    canonical = json.dumps(
        {
            "source_ir_id": table_projection.table_fact.source_ir_id,
            "logical_table_id": table_projection.table_fact.logical_table_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"whole-table-{hashlib.sha256(canonical).hexdigest()[:32]}"


def _table_structure_for_projection(
    table_projection: TableRecordProjectionV1,
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return the unique physical TABLE node without changing fact ownership."""

    declared_owner = all_structure_by_id.get(
        table_projection.table_fact.owner_structure_node_id,
        {},
    )
    if str(declared_owner.get("kind") or "").upper() == "TABLE":
        return declared_owner
    source_ids = {
        ref.source_id
        for part in table_projection.table_fact.table_parts
        for ref in part.source_refs
        if ref.source_id
    }
    candidates = [
        node
        for node in all_structure_by_id.values()
        if str(node.get("kind") or "").upper() == "TABLE"
        and source_ids
        and source_ids
        == {
            source_id
            for source_id, _, _ in _structure_source_bindings(node)
            if source_id in source_ids
        }
    ]
    return candidates[0] if len(candidates) == 1 else declared_owner


def _proven_body_owner_for_root_table(
    *,
    table_structure: dict[str, Any],
    all_structure_by_id: dict[str, dict[str, Any]],
    visible_structure_ids: set[str],
) -> str | None:
    """Recover only a unique, source-interval-contained body owner.

    A body scope starts at its own physical key and ends at the next visible
    peer-or-ancestor boundary. The table must fall strictly inside that
    interval, so a cover/revision/index table before the first body scope stays
    suppressed instead of being promoted merely because a derived table
    projection exists.
    """

    if str(table_structure.get("kind") or "").upper() != "TABLE":
        return None
    table_key = _structure_physical_reading_key(table_structure)
    body_nodes = [
        all_structure_by_id[node_id]
        for node_id in visible_structure_ids
        if str(all_structure_by_id[node_id].get("kind") or "").upper()
        in _DIRECT_REVIEW_OWNER_KINDS
        and not _is_toc_navigation_node(all_structure_by_id[node_id])
        and _dict_items(all_structure_by_id[node_id].get("source_refs"))
    ]
    if not body_nodes:
        return None
    depth_by_id = {
        str(node.get("id") or ""): _structure_node_depth(
            str(node.get("id") or ""),
            all_structure_by_id=all_structure_by_id,
        )
        for node in body_nodes
    }
    containing: list[dict[str, Any]] = []
    for candidate in body_nodes:
        candidate_id = str(candidate.get("id") or "")
        candidate_key = _structure_physical_reading_key(candidate)
        if not candidate_id or candidate_key >= table_key:
            continue
        candidate_depth = depth_by_id[candidate_id]
        closing_keys = [
            _structure_physical_reading_key(other)
            for other in body_nodes
            if str(other.get("id") or "") != candidate_id
            and _structure_physical_reading_key(other) > candidate_key
            and depth_by_id[str(other.get("id") or "")] <= candidate_depth
            and not _structure_node_is_descendant_of(
                str(other.get("id") or ""),
                candidate_id,
                all_structure_by_id=all_structure_by_id,
            )
        ]
        interval_end = min(closing_keys, default=None)
        if interval_end is not None and table_key < interval_end:
            containing.append(candidate)
    if not containing:
        return None
    deepest = max(
        depth_by_id[str(candidate.get("id") or "")] for candidate in containing
    )
    deepest_candidates = [
        candidate
        for candidate in containing
        if depth_by_id[str(candidate.get("id") or "")] == deepest
    ]
    latest_key = max(
        _structure_physical_reading_key(candidate) for candidate in deepest_candidates
    )
    winners = [
        candidate
        for candidate in deepest_candidates
        if _structure_physical_reading_key(candidate) == latest_key
    ]
    if len(winners) != 1:
        return None
    return str(winners[0].get("id") or "") or None


def _structure_node_depth(
    node_id: str,
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
) -> int:
    depth = 0
    current_id = node_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        current = all_structure_by_id.get(current_id)
        if current is None:
            break
        parent_id = str(current.get("parent_id") or "")
        if not parent_id:
            break
        depth += 1
        current_id = parent_id
    return depth


def _structure_node_is_descendant_of(
    node_id: str,
    ancestor_id: str,
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
) -> bool:
    current_id = node_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        current = all_structure_by_id.get(current_id)
        if current is None:
            return False
        current_id = str(current.get("parent_id") or "")
        if current_id == ancestor_id:
            return True
    return False


def _whole_table_review_label(
    *,
    table_structure: dict[str, Any],
    table_blocks: tuple[dict[str, Any], ...],
    caption_blocks: tuple[dict[str, Any], ...] = (),
    table_projection: TableRecordProjectionV1 | None = None,
    parent_structure: dict[str, Any] | None = None,
    table_ordinal: int = 1,
) -> str:
    caption = _first_non_blank(
        *(
            _meaningful_table_label(
                _first_non_blank(
                    str(block.get("text") or ""),
                    str(block.get("raw_text") or ""),
                    str(block.get("title") or ""),
                    str(block.get("caption") or ""),
                    str(block.get("label") or ""),
                )
            )
            for block in caption_blocks
        ),
        _verified_structure_table_caption(table_structure),
        *(
            _meaningful_table_label(
                _first_non_blank(
                    str(block.get("title") or ""),
                    str(block.get("caption") or ""),
                )
            )
            for block in table_blocks
        ),
    )
    schema = _proven_table_schema_label(
        table_projection=table_projection,
        table_blocks=table_blocks,
    )
    if caption:
        label = caption
    elif schema:
        label = f"{schema} · 表格 {max(table_ordinal, 1)}"
    else:
        parent_label = _meaningful_structure_scope_label(parent_structure or {})
        base = (
            f"{parent_label} · 表格 {max(table_ordinal, 1)}"
            if parent_label
            else f"表格 {max(table_ordinal, 1)}"
        )
        pages = _content_page_numbers(table_blocks)
        if not pages:
            pages = tuple(
                page
                for page in (
                    _positive_int(table_structure.get("page_start")),
                    _positive_int(table_structure.get("page_end")),
                )
                if page is not None
            )
        label = f"{base}（第{min(pages)}页）" if pages else base
    normalized = " ".join(label.split())
    if normalized:
        return normalized if len(normalized) <= 160 else f"{normalized[:159]}…"
    return f"表格 {max(table_ordinal, 1)}"


def _verified_structure_table_caption(table_structure: dict[str, Any]) -> str:
    candidate = _meaningful_table_label(str(table_structure.get("label") or ""))
    return candidate if candidate and _is_table_caption_text(candidate) else ""


def _meaningful_structure_scope_label(structure: dict[str, Any]) -> str:
    label = " ".join(str(structure.get("label") or "").split())
    if not label or label.casefold() in {"document", "section", "clause", "group"}:
        return ""
    return label if len(label) <= 100 else f"{label[:99]}…"


def _proven_table_schema_label(
    *,
    table_projection: TableRecordProjectionV1 | None,
    table_blocks: tuple[dict[str, Any], ...],
) -> str:
    """Name a table only from declared header regions/bindings."""

    values: list[str] = []
    if table_projection is not None:
        for record in table_projection.records:
            values.extend(binding.text for binding in record.header_bindings)
    for block in table_blocks:
        regions = _dict_items(block.get("header_regions"))
        if not regions:
            continue
        for cell in _dict_items(block.get("cells")):
            row = _nonnegative_int(cell.get("row"))
            column = _nonnegative_int(cell.get("column"))
            if not any(
                _table_coordinate_in_region(row=row, column=column, region=region)
                for region in regions
            ):
                continue
            values.append(
                _first_non_blank(
                    str(cell.get("text") or ""),
                    str(cell.get("raw_text") or ""),
                    str(cell.get("display_value") or ""),
                )
            )
    normalized = tuple(
        dict.fromkeys(
            compact
            for value in values
            if (compact := " ".join(str(value or "").split()))
        )
    )
    if not normalized:
        return ""
    label = " / ".join(normalized[:4])
    return label if len(label) <= 120 else f"{label[:119]}…"


def _table_coordinate_in_region(
    *,
    row: int,
    column: int,
    region: dict[str, Any],
) -> bool:
    start_row = _nonnegative_int(region.get("row"))
    start_column = _nonnegative_int(region.get("column"))
    row_span = max(_nonnegative_int(region.get("row_span"), default=1), 1)
    column_span = max(_nonnegative_int(region.get("column_span"), default=1), 1)
    return (
        start_row <= row < start_row + row_span
        and start_column <= column < start_column + column_span
    )


def _meaningful_table_label(value: str) -> str:
    """Hide parser placeholders while retaining real table captions.

    Native adapters commonly name an uncaptioned table simply ``Table``.
    That value is useful internally but is not a user-facing title and leaks
    English implementation vocabulary into the Chinese review tree.  Numbered
    labels such as ``Table 12`` and all descriptive captions remain intact.
    """

    normalized = " ".join(str(value or "").split())
    placeholder = normalized.casefold().rstrip(".:：")
    if placeholder in {"table", "表", "表格"}:
        return ""
    return normalized


def _table_record_structure_node_id(
    record: TableRecordV1,
    *,
    all_structure_by_id: dict[str, dict[str, Any]],
) -> str:
    record_source_ids = {ref.source_id for ref in record.source_refs}
    candidates = [
        node
        for node in all_structure_by_id.values()
        if str(node.get("kind") or "").upper() == "TABLE_ROW"
        and any(
            str(ref.get("source_id") or "") in record_source_ids
            for ref in _dict_items(node.get("source_refs"))
        )
    ]
    if not candidates:
        return record.owner_structure_node_id
    candidates.sort(
        key=lambda node: (
            _nonnegative_int(node.get("order")),
            str(node.get("id") or ""),
        )
    )
    return str(candidates[0].get("id") or record.owner_structure_node_id)


def _table_cells_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index immutable table cells once for all record presentation helpers."""

    return {
        cell_id: cell
        for table in _dict_items(payload.get("tables"))
        for cell in _dict_items(table.get("cells"))
        if (cell_id := str(cell.get("id") or cell.get("cell_id") or ""))
    }


def _table_record_cell_texts(
    record: TableRecordV1,
    *,
    cells_by_id: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    header_cell_ids = {
        cell_id for binding in record.header_bindings for cell_id in binding.cell_ids
    }
    ordered_cell_ids = tuple(
        dict.fromkeys(
            cell_id
            for binding in record.row_bindings
            for cell_id in binding.cell_ids
            if cell_id not in header_cell_ids
        )
    )
    return tuple(
        text
        for cell_id in ordered_cell_ids
        if (
            text := _first_non_blank(
                str(cells_by_id.get(cell_id, {}).get("text") or ""),
                str(cells_by_id.get(cell_id, {}).get("raw_text") or ""),
                str(cells_by_id.get(cell_id, {}).get("display_value") or ""),
            )
        )
    )


def _table_record_primary_pages(
    record: TableRecordV1,
    *,
    cells_by_id: dict[str, dict[str, Any]] | None = None,
    table_blocks: tuple[dict[str, Any], ...] = (),
) -> tuple[int, ...]:
    """Return pages containing the selected record's own bound row cells.

    Logical-table context may include headers or shared merged values from an
    earlier continuation page.  Those facts help interpret the record but do
    not determine the page opened when the user selects that record.
    """

    if cells_by_id is None:
        cells_by_id = _table_cells_by_id({"tables": table_blocks})

    row_cell_ids_by_table = {
        binding.table_id: set(binding.cell_ids)
        for binding in record.row_bindings
        if binding.cell_ids
    }
    if record.kind is TableRecordKind.MATRIX_INTERSECTION:
        column_cell_ids_by_table = {
            binding.table_id: set(binding.cell_ids)
            for binding in record.column_bindings
            if binding.cell_ids
        }
        cell_ids_by_table = {
            table_id: row_cell_ids & column_cell_ids_by_table.get(table_id, set())
            for table_id, row_cell_ids in row_cell_ids_by_table.items()
            if row_cell_ids & column_cell_ids_by_table.get(table_id, set())
        }
    else:
        cell_ids_by_table = row_cell_ids_by_table
    pages = {
        page
        for expected_cell_ids in cell_ids_by_table.values()
        for cell_id in expected_cell_ids
        if (page := _table_cell_page_number(cells_by_id.get(cell_id, {}))) is not None
    }
    return tuple(sorted(pages))


def _table_record_label(
    record: TableRecordV1,
    *,
    cells_by_id: dict[str, dict[str, Any]],
) -> str:
    texts = _table_record_cell_texts(record, cells_by_id=cells_by_id)
    if not texts:
        return "表格记录"
    candidate = " · ".join(" ".join(text.split()) for text in texts[:2])
    return candidate if len(candidate) <= 120 else f"{candidate[:119]}…"


def _table_record_labels(
    records: tuple[TableRecordV1, ...],
    *,
    cells_by_id: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Disambiguate display labels without changing immutable record IDs."""

    base_by_id = {
        record.record_id: _table_record_label(
            record,
            cells_by_id=cells_by_id,
        )
        for record in records
    }
    grouped: dict[str, list[TableRecordV1]] = defaultdict(list)
    for record in records:
        grouped[" ".join(base_by_id[record.record_id].split()).casefold()].append(
            record
        )
    output = dict(base_by_id)
    for duplicates in grouped.values():
        if len(duplicates) < 2:
            continue
        context_by_id = {
            record.record_id: _table_record_label_contexts(
                record,
                cells_by_id=cells_by_id,
            )
            for record in duplicates
        }
        context_counts: dict[str, int] = defaultdict(int)
        for contexts in context_by_id.values():
            for context in set(contexts):
                context_counts[context.casefold()] += 1
        for record in duplicates:
            unique_context = next(
                (
                    context
                    for context in context_by_id[record.record_id]
                    if context_counts[context.casefold()] == 1
                ),
                "",
            )
            suffix = unique_context or _table_record_location_label(
                record,
                cells_by_id=cells_by_id,
            )
            if not suffix:
                suffix = f"第{record.source_order + 1}条"
            candidate = f"{base_by_id[record.record_id]} · {suffix}"
            output[record.record_id] = (
                candidate if len(candidate) <= 160 else f"{candidate[:159]}…"
            )
    return output


def _table_record_label_contexts(
    record: TableRecordV1,
    *,
    cells_by_id: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    texts = _table_record_cell_texts(record, cells_by_id=cells_by_id)
    values: list[str] = []
    if record.kind is TableRecordKind.MATRIX_INTERSECTION:
        # The column axis is the semantic distinction between two intersections
        # on the same physical row. Keep it ahead of weaker row/shared context.
        values.extend(
            binding.text
            for binding in record.header_bindings
            if str(binding.axis.value).upper() == "COLUMN"
        )
    values.extend(texts[2:])
    values.extend(item.text for item in record.shared_merged_values)
    values.extend(
        binding.text
        for binding in record.header_bindings
        if str(binding.axis.value).upper() in {"ROW", "CONTEXT"}
    )
    base_values = {" ".join(text.split()).casefold() for text in texts[:2]}
    return tuple(
        dict.fromkeys(
            compact
            for value in values
            if (compact := " ".join(str(value or "").split()))
            and compact.casefold() not in base_values
        )
    )


def _table_record_location_label(
    record: TableRecordV1,
    *,
    cells_by_id: dict[str, dict[str, Any]],
) -> str:
    pages = {
        page
        for binding in record.row_bindings
        for cell_id in binding.cell_ids
        if (page := _table_cell_page_number(cells_by_id.get(cell_id, {}))) is not None
    }
    rows = sorted({binding.row_index + 1 for binding in record.row_bindings})
    columns = (
        sorted({binding.column_index + 1 for binding in record.column_bindings})
        if record.kind is TableRecordKind.MATRIX_INTERSECTION
        else []
    )
    page_label = f"第{min(pages)}页" if pages else ""
    row_label = (
        f"第{rows[0]}行"
        if len(rows) == 1
        else f"第{rows[0]}–{rows[-1]}行"
        if rows
        else ""
    )
    column_label = (
        f"第{columns[0]}列"
        if len(columns) == 1
        else f"第{columns[0]}–{columns[-1]}列"
        if columns
        else ""
    )
    return f"{page_label}{row_label}{column_label}"


def _table_cell_page_number(cell: dict[str, Any]) -> int | None:
    return _positive_int(
        cell.get("page_number") or _dict(cell.get("location")).get("page_number")
    )


def _table_record_summary(
    record: TableRecordV1,
    *,
    cells_by_id: dict[str, dict[str, Any]],
) -> str:
    texts = _table_record_cell_texts(record, cells_by_id=cells_by_id)
    if texts:
        return " | ".join(" ".join(text.split()) for text in texts)
    return _table_record_label(record, cells_by_id=cells_by_id)


def _nonnegative_int(value: Any, *, default: int = 0) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return max(default, 0)


def _dedupe_dicts(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        token = repr(sorted(item.items(), key=lambda pair: pair[0]))
        if token in seen:
            continue
        seen.add(token)
        output.append(item)
    return output


def _table_refs(
    blocks: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        block for block in blocks if str(block.get("kind") or "").lower() == "table"
    )


def _figure_refs(
    blocks: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        block
        for block in blocks
        if str(block.get("kind") or "").lower() in {"figure", "image"}
    )


def _nearest_visible_owner(
    owner_id: str,
    *,
    visible_ids: set[str],
    all_nodes: dict[str, dict[str, Any]],
) -> str | None:
    current_id = owner_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        visited.add(current_id)
        if current_id in visible_ids:
            return current_id
        current = all_nodes.get(current_id)
        if current is None:
            return None
        current_id = str(current.get("parent_id") or "")
    return None


def _nearest_visible_parent(
    parent_id: str,
    *,
    visible_ids: set[str],
    all_nodes: dict[str, dict[str, Any]],
) -> str:
    return (
        _nearest_visible_owner(
            parent_id,
            visible_ids=visible_ids,
            all_nodes=all_nodes,
        )
        or ""
    )


def _depths(parent_by_id: dict[str, str | None]) -> dict[str, int]:
    depths: dict[str, int] = {}
    for node_id in parent_by_id:
        current_id: str | None = node_id
        visited: set[str] = set()
        depth = 0
        while current_id is not None and parent_by_id.get(current_id) is not None:
            if current_id in visited:
                raise ValueError("requirement tree contains a parent cycle")
            visited.add(current_id)
            depth += 1
            current_id = parent_by_id.get(current_id)
        depths[node_id] = depth
    return depths


def _tree_state(
    *,
    agent: dict[str, Any],
    payload: dict[str, Any],
    node_count: int,
    substantive_node_count: int,
    requirement_count: int,
    source_capture_gap_count: int | None,
) -> RequirementTreeState:
    status = str(agent.get("status") or "IDLE").upper()
    result_state = str(agent.get("result_state") or "").upper()
    if status in {"FAILED", "INTERRUPTED"}:
        return RequirementTreeState.FAILED
    if status in {"PENDING", "QUEUED", "STARTING", "RUNNING"}:
        return RequirementTreeState.RUNNING_PARTIAL
    source_ir_status = str(
        _dict(_dict(payload.get("pipeline")).get("source_ir")).get("status") or ""
    ).upper()
    source_capture_requires_reextraction = (
        source_ir_status == "FAILED"
        or (source_capture_gap_count is not None and source_capture_gap_count > 0)
        or (source_ir_status == "PARTIAL" and source_capture_gap_count is None)
    )
    if (
        status == "COMPLETED"
        and source_capture_requires_reextraction
    ):
        # A terminal run with an unresolved source-capture gap is never safe
        # for downstream publication, even when stale or manually-created
        # requirement leaves already exist.  A READY structure-inspection
        # receipt with zero capture gaps may reconcile a PDF whose SourceIR
        # remains PARTIAL for native-fidelity truth; missing/legacy receipts
        # stay conservative instead of silently treating that status as
        # complete.
        return RequirementTreeState.REQUIRES_REEXTRACTION
    if (
        substantive_node_count
        and status == "COMPLETED"
        and result_state in {"STABLE", "READY"}
    ):
        # Structure completion is a valid no-LLM outcome. Requirement leaves
        # may be absent because reviewable structure proxies deliberately
        # precede semantic classification.
        return RequirementTreeState.READY
    if node_count:
        return RequirementTreeState.RUNNING_PARTIAL
    return RequirementTreeState.NOT_STARTED


def _ready_structure_inspection_capture_gap_count(
    workbench: dict[str, Any],
) -> int | None:
    inspection = workbench.get("structure_inspection")
    if not isinstance(inspection, dict):
        return None
    if str(inspection.get("state") or "").upper() != "READY":
        return None
    if (
        str(inspection.get("inspection_runtime_version") or "")
        != STRUCTURE_INSPECTION_RUNTIME_VERSION
    ):
        return None
    counts = inspection.get("counts")
    if not isinstance(counts, dict) or "capture_gap_count" not in counts:
        return None
    value = counts.get("capture_gap_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _block_text(blocks: tuple[dict[str, Any], ...]) -> str:
    return "\n".join(
        str(block.get("text") or "").strip()
        for block in blocks
        if str(block.get("text") or "").strip()
    )


def _first_non_blank(*values: str) -> str:
    return next((value.strip() for value in values if value.strip()), "")


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, (list, tuple))
        else []
    )


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


__all__ = [
    "REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION",
    "attach_toc_navigation_entries",
    "build_requirement_tree_snapshot",
]
