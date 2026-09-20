from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.extraction.table_records import TableRecordProjectionV1
from app.domain.extraction.table_evidence import (
    RequirementEvidenceBundleV1,
    TableEvidenceBindingV1,
)


REQUIREMENT_TREE_SCHEMA_VERSION = "requirement-tree-snapshot/v1"

_REVIEWABLE_PROJECTION_ROLES = {
    "REQUIREMENT",
    "STRUCTURE_PROXY",
    "DIRECT_CONTENT",
    "WHOLE_TABLE_REVIEW",
    "TABLE_RECORD",
}


class RequirementTreeMode(str, Enum):
    WORKING = "WORKING"
    REQUIREMENT_SET = "REQUIREMENT_SET"


class RequirementTreeState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING_PARTIAL = "RUNNING_PARTIAL"
    READY = "READY"
    FAILED = "FAILED"
    REQUIRES_REEXTRACTION = "REQUIRES_REEXTRACTION"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequirementTreeSourceIdentity(_FrozenModel):
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    source_ir_id: str = Field(default="", max_length=200)
    source_sha256: str = Field(default="", max_length=64)
    preview_generation_id: str = Field(default="", max_length=200)
    requirement_set_id: str = Field(default="", max_length=200)
    geometry_index_id: str = Field(default="", max_length=64)
    geometry_index_sha256: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_geometry_identity(self) -> RequirementTreeSourceIdentity:
        if bool(self.geometry_index_id) != bool(self.geometry_index_sha256):
            raise ValueError(
                "geometry index ID and SHA-256 must be recorded together"
            )
        for value in (self.geometry_index_id, self.geometry_index_sha256):
            if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("geometry identities must be lowercase SHA-256")
        return self


class RequirementTreeStructureIdentity(_FrozenModel):
    base_snapshot_id: str = Field(default="", max_length=200)
    effective_snapshot_id: str = Field(default="", max_length=200)
    effective_tree_sha256: str = Field(default="", max_length=64)
    revision: int = Field(default=0, ge=0)


class RequirementTreeTocEntryV1(_FrozenModel):
    """One source-bound table-of-contents navigation entry.

    TOC text is presentation/navigation evidence rather than a requirement
    candidate.  ``target_structure_node_id`` is populated only when the
    structure target can be proven; the printed page remains useful when it
    cannot.
    """

    entry_id: str = Field(default="", max_length=200)
    number: str = Field(default="", max_length=120)
    title: str = Field(min_length=1, max_length=2_000)
    # Printed pagination is document text and may differ from the physical PDF
    # page.  It is display-only until a Structure target proves target_page.
    printed_page_label: str = Field(default="", max_length=80)
    target_page: int | None = Field(default=None, ge=1)
    source_order: int = Field(ge=0)
    source_refs: tuple[dict[str, Any], ...] = ()
    target_structure_node_id: str | None = Field(default=None, max_length=260)


class RequirementTreeBilingualSourceSpanV1(_FrozenModel):
    """One exact language-side interval retained by a logical candidate."""

    bilingual_span_id: str = Field(
        pattern=r"^bilingual-span-[0-9a-f]{32}$"
    )
    language: Literal["en", "zh-CN"]
    source_ref: dict[str, Any]
    owner_structure_node_id: str = Field(min_length=1, max_length=260)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    boundary_basis: Literal["DOCX_RUN_BOUNDARY", "SOURCE_BLOCK_BOUNDARY"]

    @model_validator(mode="after")
    def validate_interval(self) -> RequirementTreeBilingualSourceSpanV1:
        if self.end_offset <= self.start_offset:
            raise ValueError("bilingual source spans use non-empty intervals")
        if not str(self.source_ref.get("source_id") or "").strip():
            raise ValueError("bilingual source spans require an exact source ref")
        return self


class RequirementTreeBilingualContextV1(_FrozenModel):
    """Typed proof that several source languages form one review candidate.

    The context never contains translated or replacement text.  Authority is
    either copied from an explicit document policy or left ``UNKNOWN``; it is
    never inferred from language, source order, or the canonical member.
    """

    counterpart_group_id: str = Field(
        pattern=r"^bilingual-group-[0-9a-f]{32}$"
    )
    document_language_policy_id: str | None = Field(
        default=None,
        pattern=r"^document-language-policy-[0-9a-f]{32}$",
    )
    canonical_structure_node_id: str = Field(min_length=1, max_length=260)
    member_structure_node_ids: tuple[str, ...] = Field(min_length=1, max_length=2)
    authority_language: Literal["en", "zh-CN", "UNKNOWN"] = "UNKNOWN"
    authority_basis: Literal["DECLARED_DOCUMENT_POLICY", "UNKNOWN"] = "UNKNOWN"
    equal_validity: bool | None = None
    source_spans: tuple[RequirementTreeBilingualSourceSpanV1, ...] = Field(
        min_length=2
    )
    mismatch_signal_ids: tuple[str, ...] = ()
    mismatch_review_required: bool = False

    @model_validator(mode="after")
    def validate_context(self) -> RequirementTreeBilingualContextV1:
        if (
            len(self.member_structure_node_ids)
            != len(set(self.member_structure_node_ids))
            or self.canonical_structure_node_id
            not in self.member_structure_node_ids
        ):
            raise ValueError(
                "bilingual context requires unique members and one canonical member"
            )
        span_ids = tuple(span.bilingual_span_id for span in self.source_spans)
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("bilingual context source spans must be unique")
        if {span.language for span in self.source_spans} != {"en", "zh-CN"}:
            raise ValueError("bilingual context requires exact spans on both sides")
        if any(
            span.owner_structure_node_id not in self.member_structure_node_ids
            for span in self.source_spans
        ):
            raise ValueError("bilingual spans must remain inside group members")
        if self.authority_basis == "UNKNOWN":
            if self.authority_language != "UNKNOWN":
                raise ValueError("unknown authority cannot select a language")
        elif (
            self.authority_language == "UNKNOWN"
            or self.document_language_policy_id is None
        ):
            raise ValueError(
                "declared authority requires a policy and selected language"
            )
        if len(self.mismatch_signal_ids) != len(set(self.mismatch_signal_ids)) or any(
            re.fullmatch(r"bilingual-mismatch-[0-9a-f]{32}", signal_id) is None
            for signal_id in self.mismatch_signal_ids
        ):
            raise ValueError("bilingual mismatch signal IDs must be unique and stable")
        if self.mismatch_review_required != bool(self.mismatch_signal_ids):
            raise ValueError(
                "bilingual mismatch review state must match its grounded signals"
            )
        return self


class RequirementTreeNodeV1(_FrozenModel):
    id: str = Field(min_length=1, max_length=260)
    parent_id: str | None = Field(default=None, max_length=260)
    kind: str = Field(min_length=1, max_length=80)
    depth: int = Field(ge=0)
    source_order: int = Field(ge=0)
    # Offset inside one SourceIR fact.  Together with source_order this is the
    # immutable physical reading key; editable Structure order is not reused.
    source_offset: int = Field(default=0, ge=0)
    label: str = Field(default="", max_length=500_000)
    summary: str = Field(default="", max_length=500_000)
    child_ids: tuple[str, ...] = ()
    structure_node_id: str | None = Field(default=None, max_length=260)
    requirement_entry_id: str | None = Field(default=None, max_length=260)
    workflow_node_id: str | None = Field(default=None, max_length=260)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    source_refs: tuple[dict[str, Any], ...] = ()
    requirement_count: int = Field(default=0, ge=0)
    attention_count: int = Field(default=0, ge=0)
    reviewed_count: int = Field(default=0, ge=0)
    status: str = Field(default="PENDING", max_length=80)
    classification: str = Field(default="", max_length=120)
    projection_role: str = Field(default="", max_length=80)
    semantic_state: str = Field(default="", max_length=80)
    deterministic_rule_evidence: tuple[dict[str, Any], ...] = ()
    table_record_id: str | None = Field(default=None, max_length=260)
    table_evidence_binding_id: str | None = Field(default=None, max_length=260)
    requirement_evidence_binding_id: str | None = Field(
        default=None,
        max_length=260,
    )
    review_item_count: int = Field(default=0, ge=0)
    include_in_view: bool = True
    content_blocks: tuple[dict[str, Any], ...] = ()
    editable_structure_node_ids: tuple[str, ...] = ()
    toc_entries: tuple[RequirementTreeTocEntryV1, ...] = ()
    table_refs: tuple[dict[str, Any], ...] = ()
    figure_refs: tuple[dict[str, Any], ...] = ()
    bilingual_context: RequirementTreeBilingualContextV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_page_range(self) -> RequirementTreeNodeV1:
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_end < self.page_start
        ):
            raise ValueError("tree node page_end cannot precede page_start")
        if (
            self.requirement_entry_id
            and self.projection_role not in _REVIEWABLE_PROJECTION_ROLES
        ):
            raise ValueError(
                "only reviewable projections can bind a requirement entry"
            )
        if self.kind == "DIRECT_CONTENT" and (
            self.projection_role != "DIRECT_CONTENT"
            or not self.structure_node_id
            or self.parent_id != self.structure_node_id
            or self.child_ids
        ):
            raise ValueError(
                "direct content must be a leaf review projection of its owner"
            )
        if len(self.editable_structure_node_ids) != len(
            set(self.editable_structure_node_ids)
        ):
            raise ValueError("editable structure node IDs must be unique")
        toc_entry_ids = [entry.entry_id for entry in self.toc_entries if entry.entry_id]
        if len(toc_entry_ids) != len(set(toc_entry_ids)):
            raise ValueError("TOC entry IDs must be unique within one node")
        if self.editable_structure_node_ids:
            if self.projection_role != "DIRECT_CONTENT":
                raise ValueError(
                    "only direct content can expose editable structure nodes"
                )
            content_structure_ids = {
                str(block.get("structure_node_id") or "")
                for block in self.content_blocks
                if str(block.get("structure_node_id") or "")
            }
            if not set(self.editable_structure_node_ids).issubset(
                content_structure_ids
            ):
                raise ValueError(
                    "editable structure nodes must come from direct content blocks"
                )
        if self.projection_role == "WHOLE_TABLE_REVIEW" and (
            self.kind != "TABLE"
            or not self.structure_node_id
            or not self.parent_id
            or self.child_ids
            or self.table_record_id is not None
        ):
            raise ValueError(
                "whole-table review content must be a leaf table projection"
            )
        if self.table_record_id and self.kind != "TABLE_RECORD":
            raise ValueError("only table-record nodes can bind a table record")
        if self.bilingual_context is not None and self.projection_role not in {
            "STRUCTURE_PROXY",
            "DIRECT_CONTENT",
        }:
            raise ValueError(
                "bilingual context belongs only to a source-bound review candidate"
            )
        return self


class RequirementTreeSnapshotV1(_FrozenModel):
    schema_version: Literal["requirement-tree-snapshot/v1"] = (
        REQUIREMENT_TREE_SCHEMA_VERSION
    )
    source_identity: RequirementTreeSourceIdentity
    structure_identity: RequirementTreeStructureIdentity
    mode: RequirementTreeMode
    state: RequirementTreeState
    root_node_ids: tuple[str, ...]
    nodes: tuple[RequirementTreeNodeV1, ...]
    table_record_projections: tuple[TableRecordProjectionV1, ...] = ()
    table_evidence_bindings: tuple[TableEvidenceBindingV1, ...] = ()
    requirement_evidence_bundle: RequirementEvidenceBundleV1 | None = None

    @model_validator(mode="after")
    def validate_tree(self) -> RequirementTreeSnapshotV1:
        node_by_id = {node.id: node for node in self.nodes}
        if len(node_by_id) != len(self.nodes):
            raise ValueError("requirement tree node IDs must be unique")
        if len(self.root_node_ids) != len(set(self.root_node_ids)):
            raise ValueError("requirement tree root IDs must be unique")
        if any(root_id not in node_by_id for root_id in self.root_node_ids):
            raise ValueError("requirement tree roots must reference existing nodes")
        expected_roots = tuple(node.id for node in self.nodes if node.parent_id is None)
        if set(expected_roots) != set(self.root_node_ids):
            raise ValueError("requirement tree roots do not match parentless nodes")

        requirement_entry_ids: set[str] = set()
        bilingual_group_ids: set[str] = set()
        for node in self.nodes:
            if node.parent_id is not None and node.parent_id not in node_by_id:
                raise ValueError("requirement tree parent does not exist")
            if node.parent_id is not None:
                parent = node_by_id[node.parent_id]
                if node.id not in parent.child_ids:
                    raise ValueError(
                        "requirement tree parent must reference every child"
                    )
            if len(node.child_ids) != len(set(node.child_ids)):
                raise ValueError("requirement tree child IDs must be unique")
            for child_id in node.child_ids:
                child = node_by_id.get(child_id)
                if child is None or child.parent_id != node.id:
                    raise ValueError("requirement tree child relation is inconsistent")
            if node.requirement_entry_id:
                if node.requirement_entry_id in requirement_entry_ids:
                    raise ValueError(
                        "a requirement entry can appear only once in the tree"
                    )
                requirement_entry_ids.add(node.requirement_entry_id)
            if node.bilingual_context is not None:
                group_id = node.bilingual_context.counterpart_group_id
                if group_id in bilingual_group_ids:
                    raise ValueError(
                        "a bilingual counterpart group can project only once"
                    )
                bilingual_group_ids.add(group_id)
            for toc_entry in node.toc_entries:
                target_id = toc_entry.target_structure_node_id
                if target_id is not None:
                    target = node_by_id.get(target_id)
                    if (
                        target is None
                        or target.id != target.structure_node_id
                        or target.projection_role
                        not in {"STRUCTURE_PROXY", "STRUCTURE"}
                        or target.kind
                        not in {"SECTION", "CLAUSE", "GROUP", "LIST_GROUP"}
                    ):
                        raise ValueError(
                            "table-of-contents entry requires a structural navigation target"
                        )

        visited_node_ids: set[str] = set()
        pending_node_ids = list(self.root_node_ids)
        while pending_node_ids:
            node_id = pending_node_ids.pop()
            if node_id in visited_node_ids:
                raise ValueError("requirement tree hierarchy contains a cycle")
            visited_node_ids.add(node_id)
            pending_node_ids.extend(node_by_id[node_id].child_ids)
        if visited_node_ids != set(node_by_id):
            raise ValueError("requirement tree contains disconnected nodes")

        if self.state is RequirementTreeState.READY and not self.nodes:
            raise ValueError("a ready requirement tree cannot be empty")
        projection_ids = tuple(
            projection.projection_id
            for projection in self.table_record_projections
        )
        logical_table_ids = tuple(
            projection.table_fact.logical_table_id
            for projection in self.table_record_projections
        )
        if len(projection_ids) != len(set(projection_ids)):
            raise ValueError("table record projection IDs must be unique")
        if len(logical_table_ids) != len(set(logical_table_ids)):
            raise ValueError(
                "a requirement tree can bind one projection per logical table"
            )
        evidence_ids = tuple(
            binding.binding_id for binding in self.table_evidence_bindings
        )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("table evidence binding IDs must be unique")
        evidence_by_id = {
            binding.binding_id: binding
            for binding in self.table_evidence_bindings
        }
        projection_by_id = {
            projection.projection_id: projection
            for projection in self.table_record_projections
        }
        for binding in self.table_evidence_bindings:
            projection = projection_by_id.get(binding.projection_id)
            if projection is None:
                raise ValueError(
                    "table evidence references an unknown table projection"
                )
            if (
                binding.projection_revision
                != projection.projection_revision
                or binding.projection_sha256
                != projection.projection_sha256
                or binding.logical_table_id
                != projection.table_fact.logical_table_id
            ):
                raise ValueError(
                    "table evidence lineage does not match its table projection"
                )
            if binding.record_id is not None and binding.record_id not in {
                record.record_id for record in projection.records
            }:
                raise ValueError(
                    "table evidence references an unknown projected record"
                )
        for node in self.nodes:
            if node.table_evidence_binding_id is None:
                continue
            binding = evidence_by_id.get(node.table_evidence_binding_id)
            if binding is None:
                raise ValueError(
                    "tree node references an unknown table evidence binding"
                )
            if (
                node.projection_role == "WHOLE_TABLE_REVIEW"
                and binding.record_id is not None
            ):
                raise ValueError(
                    "whole-table review nodes require whole-table evidence"
                )
            if (
                node.table_record_id is not None
                and binding.record_id != node.table_record_id
            ):
                raise ValueError(
                    "tree table record points to different table evidence"
                )
        bundle = self.requirement_evidence_bundle
        if bundle is None:
            # Older v1 snapshots predate the evidence-freeze extension. They
            # remain readable, while all new persisted READY snapshots are
            # rejected by the persistence boundary unless a bundle is present.
            if any(
                node.requirement_evidence_binding_id is not None
                for node in self.nodes
            ):
                raise ValueError(
                    "tree requirement evidence pointers require an evidence bundle"
                )
            return self
        if (
            bundle.source_ir_id != self.source_identity.source_ir_id
            or bundle.source_sha256 != self.source_identity.source_sha256
            or bundle.structure_snapshot_id
            != self.structure_identity.effective_snapshot_id
        ):
            raise ValueError(
                "requirement evidence bundle lineage does not match the tree"
            )
        if bundle.table_bindings != self.table_evidence_bindings:
            raise ValueError(
                "requirement evidence bundle must freeze the tree table bindings"
            )
        requirement_binding_by_id = {
            binding.binding_id: binding
            for binding in bundle.requirement_bindings
        }
        reviewable_nodes = tuple(
            node
            for node in self.nodes
            if node.include_in_view
            and node.projection_role in _REVIEWABLE_PROJECTION_ROLES
            and node.requirement_entry_id is not None
        )
        bound_requirement_entry_ids: set[str] = set()
        for node in reviewable_nodes:
            pointer = node.requirement_evidence_binding_id
            if pointer is None:
                raise ValueError(
                    "published review item requires requirement evidence"
                )
            binding = requirement_binding_by_id.get(pointer)
            if binding is None:
                raise ValueError(
                    "tree node references unknown requirement evidence"
                )
            if binding.requirement_entry_id != node.requirement_entry_id:
                raise ValueError(
                    "tree node points to evidence for a different requirement"
                )
            if (
                binding.table_evidence_binding_id
                != node.table_evidence_binding_id
            ):
                raise ValueError(
                    "tree node and requirement evidence disagree on table evidence"
                )
            bound_requirement_entry_ids.add(binding.requirement_entry_id)
        if bound_requirement_entry_ids != {
            binding.requirement_entry_id
            for binding in bundle.requirement_bindings
        }:
            raise ValueError(
                "requirement evidence bundle contains a dangling requirement binding"
            )
        return self


def canonical_requirement_tree_bytes(
    snapshot: RequirementTreeSnapshotV1,
) -> bytes:
    return json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_requirement_tree_sha256(
    snapshot: RequirementTreeSnapshotV1,
) -> str:
    return hashlib.sha256(canonical_requirement_tree_bytes(snapshot)).hexdigest()


__all__ = [
    "REQUIREMENT_TREE_SCHEMA_VERSION",
    "RequirementTreeBilingualContextV1",
    "RequirementTreeBilingualSourceSpanV1",
    "RequirementTreeMode",
    "RequirementTreeNodeV1",
    "RequirementTreeSnapshotV1",
    "RequirementTreeSourceIdentity",
    "RequirementTreeState",
    "RequirementTreeStructureIdentity",
    "RequirementTreeTocEntryV1",
    "canonical_requirement_tree_bytes",
    "canonical_requirement_tree_sha256",
]
