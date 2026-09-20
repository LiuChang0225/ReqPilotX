from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum

from app.application.extraction.structure_snapshot import (
    StructureSnapshot,
    require_structure_snapshot,
    structure_snapshot_id,
    validate_structure_snapshot_against_source_ir,
)
from app.application.extraction.resolve_structure import (
    validate_structure_tree_against_source_ir,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
)
from app.domain.extraction.structure import (
    StructureBoundaryBasis,
    StructureNode,
    StructureNodeKind,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)


LLM_EXTRACTION_SCOPE_LOCATOR_VERSION = "llm-extraction-scope-locator/v1"
LLM_DOCUMENT_ROOT_SCOPE_LOCATOR_VERSION = "llm-document-root-scope-locator/v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECTION_LABEL_PATTERN = re.compile(
    r"^[1-9][0-9]{0,2}(?:\.(?:0|[1-9][0-9]{0,2})){0,7}$"
)


class LlmScopeLocationRejectionCode(str, Enum):
    INPUT_BINDING_INVALID = "llm_scope_input_binding_invalid"
    SOURCE_HASH_MISMATCH = "llm_scope_source_hash_mismatch"
    SECTION_NOT_FOUND = "llm_scope_section_not_found"
    SECTION_AMBIGUOUS = "llm_scope_section_ambiguous"
    SOURCE_LOCATOR_MISMATCH = "llm_scope_source_locator_mismatch"
    DUPLICATE_SECTION = "llm_scope_duplicate_section"
    INCOMPLETE_PROVISION = "llm_scope_provision_incomplete"


class LlmScopeLocationRejected(RuntimeError):
    """Fail-closed scope selection error without customer text in the message."""

    def __init__(
        self,
        code: LlmScopeLocationRejectionCode,
        *,
        selector_index: int | None = None,
    ) -> None:
        self.code = code
        self.selector_index = selector_index
        super().__init__(code.value)


@dataclass(frozen=True)
class LlmScopeSourceLocatorConstraint:
    """Optional exact PDF source-location discriminator for one section heading.

    The constraint deliberately carries no source ID or text.  Stable source IDs
    are adapter-version bound, while page/container coordinates can be reused as
    a fail-closed discriminator when a normalized section path is not unique.
    """

    source_ref_kind: StructureSourceRefKind
    page_number: int
    block_index: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    table_index: int | None = None
    row_index: int | None = None
    column_index: int | None = None
    observation_index: int | None = None
    provider_id: str | None = None
    provider_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref_kind, StructureSourceRefKind):
            raise ValueError("source_ref_kind must be a StructureSourceRefKind")
        _require_non_negative_integer(self.page_number, "page_number", minimum=1)
        for field_name in (
            "block_index",
            "table_index",
            "row_index",
            "column_index",
            "observation_index",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_non_negative_integer(value, field_name)
        if self.bbox is not None:
            _validate_bbox(self.bbox)
            object.__setattr__(self, "bbox", tuple(float(item) for item in self.bbox))
        for field_name in ("provider_id", "provider_version"):
            value = getattr(self, field_name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{field_name} must be a non-blank string")
                object.__setattr__(self, field_name, value.strip())

        if self.source_ref_kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION:
            if any(
                value is not None
                for value in (
                    self.block_index,
                    self.table_index,
                    self.row_index,
                    self.column_index,
                )
            ):
                raise ValueError("visual locators cannot contain native indexes")
            return

        if self.observation_index is not None:
            raise ValueError("native locators cannot contain observation_index")
        if self.provider_id is not None or self.provider_version is not None:
            raise ValueError("native locators cannot contain provider identity")
        if self.block_index is not None and self.table_index is not None:
            raise ValueError("native locators cannot combine block and table indexes")
        if (self.row_index is None) != (self.column_index is None):
            raise ValueError("row_index and column_index must be supplied together")
        if self.source_ref_kind is StructureSourceRefKind.TABLE_CELL:
            if any(
                value is None
                for value in (self.table_index, self.row_index, self.column_index)
            ):
                raise ValueError("table-cell locators require table, row and column")
            if self.block_index is not None:
                raise ValueError("table-cell locators cannot contain block_index")
        elif self.table_index is None and (
            self.row_index is not None or self.column_index is not None
        ):
            raise ValueError("native row/column coordinates require table_index")


@dataclass(frozen=True)
class LlmSectionScopeSelector:
    """One version-independent, fully qualified numeric section path."""

    normalized_section_path: tuple[str, ...]
    source_locator: LlmScopeSourceLocatorConstraint | None = None

    def __post_init__(self) -> None:
        path = self.normalized_section_path
        if not isinstance(path, tuple) or not path:
            raise ValueError("normalized_section_path must be a non-empty tuple")
        parts = tuple(_section_label_parts(label) for label in path)
        if len(parts[0]) != 1:
            raise ValueError("normalized_section_path must start at a top-level label")
        for parent, child in zip(parts, parts[1:], strict=False):
            if len(child) != len(parent) + 1 or child[:-1] != parent:
                raise ValueError(
                    "normalized_section_path must contain every numeric ancestor"
                )
        if self.source_locator is not None and not isinstance(
            self.source_locator,
            LlmScopeSourceLocatorConstraint,
        ):
            raise ValueError("source_locator has an invalid type")


@dataclass(frozen=True)
class LlmExtractionScopeLocator:
    """Source-bound review intent that can be replayed across pipeline versions."""

    source_sha256: str
    section_selectors: tuple[LlmSectionScopeSelector, ...]
    version: str = LLM_EXTRACTION_SCOPE_LOCATOR_VERSION

    def __post_init__(self) -> None:
        if not _SHA256_PATTERN.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        if self.version != LLM_EXTRACTION_SCOPE_LOCATOR_VERSION:
            raise ValueError("scope locator version is unsupported")
        if not isinstance(self.section_selectors, tuple) or not self.section_selectors:
            raise ValueError("scope locator requires at least one section selector")
        if any(
            not isinstance(selector, LlmSectionScopeSelector)
            for selector in self.section_selectors
        ):
            raise ValueError("section_selectors contain an invalid value")
        if len(self.section_selectors) != len(set(self.section_selectors)):
            raise ValueError("section_selectors must not contain duplicates")

    @property
    def locator_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                _canonical_locator_payload(self),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class LocatedLlmExtractionScope:
    locator_version: str
    locator_sha256: str
    source_sha256: str
    root_section_structure_node_ids: tuple[str, ...]
    scope_structure_node_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ResolvedPdfLocator:
    source_ref_kind: StructureSourceRefKind
    page_number: int
    block_index: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    table_index: int | None = None
    row_index: int | None = None
    column_index: int | None = None
    observation_index: int | None = None
    provider_id: str | None = None
    provider_version: str | None = None


def locate_llm_extraction_scope(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    locator: LlmExtractionScopeLocator,
) -> LocatedLlmExtractionScope:
    """Resolve complete section provisions before any semantic provider call."""

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if not isinstance(locator, LlmExtractionScopeLocator):
        raise TypeError("locator must be an LlmExtractionScopeLocator")
    if source_ir.source_sha256 != locator.source_sha256:
        raise LlmScopeLocationRejected(
            LlmScopeLocationRejectionCode.SOURCE_HASH_MISMATCH
        )
    try:
        if isinstance(tree, StructureTreeSnapshot):
            validate_structure_tree_against_source_ir(tree, source_ir)
        else:
            validate_structure_snapshot_against_source_ir(tree, source_ir)
    except (TypeError, ValueError) as exc:
        raise LlmScopeLocationRejected(
            LlmScopeLocationRejectionCode.INPUT_BINDING_INVALID
        ) from exc

    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    source_locators = _source_locators(source_ir)
    section_nodes = tuple(
        node
        for node in tree.nodes
        if node.kind is StructureNodeKind.SECTION and node.explicit_label is not None
    )
    roots: list[StructureNode] = []
    for selector_index, selector in enumerate(locator.section_selectors):
        path_matches = tuple(
            node
            for node in section_nodes
            if _normalized_section_path(node, nodes_by_id)
            == selector.normalized_section_path
        )
        if not path_matches:
            raise LlmScopeLocationRejected(
                LlmScopeLocationRejectionCode.SECTION_NOT_FOUND,
                selector_index=selector_index,
            )
        matches = (
            path_matches
            if selector.source_locator is None
            else tuple(
                node
                for node in path_matches
                if _node_matches_source_locator(
                    node,
                    selector.source_locator,
                    source_locators,
                )
            )
        )
        if not matches:
            raise LlmScopeLocationRejected(
                LlmScopeLocationRejectionCode.SOURCE_LOCATOR_MISMATCH,
                selector_index=selector_index,
            )
        if len(matches) != 1:
            raise LlmScopeLocationRejected(
                LlmScopeLocationRejectionCode.SECTION_AMBIGUOUS,
                selector_index=selector_index,
            )
        roots.append(matches[0])

    resolved_root_ids = tuple(node.structure_node_id for node in roots)
    if len(resolved_root_ids) != len(set(resolved_root_ids)):
        raise LlmScopeLocationRejected(LlmScopeLocationRejectionCode.DUPLICATE_SECTION)
    resolved_root_id_set = set(resolved_root_ids)
    roots = [
        node for node in tree.nodes if node.structure_node_id in resolved_root_id_set
    ]
    root_ids = tuple(node.structure_node_id for node in roots)
    selected_ids = set(root_ids)
    for root in roots:
        selected_ids.update(_owned_provision_ids(root, tree.nodes, nodes_by_id))
    if any(
        relation.kind is StructureRelationKind.CONTINUES
        and (
            (relation.source_structure_node_id in selected_ids)
            != (relation.target_structure_node_id in selected_ids)
        )
        for relation in tree.relations
    ):
        raise LlmScopeLocationRejected(
            LlmScopeLocationRejectionCode.INCOMPLETE_PROVISION
        )
    ordered_scope = tuple(
        node.structure_node_id
        for node in tree.nodes
        if node.structure_node_id in selected_ids
    )
    return LocatedLlmExtractionScope(
        locator_version=locator.version,
        locator_sha256=locator.locator_sha256,
        source_sha256=source_ir.source_sha256,
        root_section_structure_node_ids=root_ids,
        scope_structure_node_ids=ordered_scope,
    )


def build_llm_document_scope_locator(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
) -> LlmExtractionScopeLocator:
    """Select every server-recognized numeric BODY clause for semantic review.

    Source coordinates are attached to every selector so repeated numbering in
    appendices or sibling branches remains fail-closed and deterministic.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if tree.source_ir_id != source_ir.source_ir_id:
        raise ValueError("document scope inputs are not bound to one SourceIR")

    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    source_locators = _source_locators(source_ir)
    selectors: list[LlmSectionScopeSelector] = []
    for node in tree.nodes:
        if node.kind is not StructureNodeKind.SECTION or node.explicit_label is None:
            continue
        try:
            normalized_path = _normalized_section_path(node, nodes_by_id)
            constraint = _section_source_locator_constraint(node, source_locators)
            selectors.append(
                LlmSectionScopeSelector(
                    normalized_section_path=normalized_path,
                    source_locator=constraint,
                )
            )
        except ValueError:
            continue
    if not selectors:
        raise ValueError("document contains no locatable numeric BODY clauses")
    return LlmExtractionScopeLocator(
        source_sha256=source_ir.source_sha256,
        section_selectors=tuple(selectors),
    )


def locate_llm_document_scope(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
) -> LocatedLlmExtractionScope:
    """Locate one complete automatic-document scope.

    Numbered sections retain the replayable selector contract used by manual
    scope selection. Documents whose native structure contains no numbered
    sections fall back to complete, source-ordered branches directly owned by
    the document root. This keeps table-led and form-led specifications inside
    the same source-bound pipeline instead of rejecting them before semantic
    review.
    """

    try:
        locator = build_llm_document_scope_locator(source_ir, tree)
    except ValueError as exc:
        if str(exc) != "document contains no locatable numeric BODY clauses":
            raise
        return _locate_document_root_branches(source_ir, tree)
    try:
        return locate_llm_extraction_scope(source_ir, tree, locator)
    except LlmScopeLocationRejected as exc:
        if exc.code is not LlmScopeLocationRejectionCode.INCOMPLETE_PROVISION:
            raise
        # Automatic whole-document extraction must not drop an authored
        # continuation merely because its peer is outside the numeric-section
        # selector set. Expand to every non-furniture root branch and close all
        # CONTINUES components. Manual/scoped extraction keeps the strict
        # rejection above so a selected section can never absorb adjacent text.
        return _locate_document_root_branches(source_ir, tree)


def _locate_document_root_branches(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
) -> LocatedLlmExtractionScope:
    try:
        validate_structure_snapshot_against_source_ir(tree, source_ir)
    except (TypeError, ValueError) as exc:
        raise LlmScopeLocationRejected(
            LlmScopeLocationRejectionCode.INPUT_BINDING_INVALID
        ) from exc

    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    document_roots = tuple(
        node
        for node in tree.nodes
        if node.parent_structure_node_id is None
        and node.kind is StructureNodeKind.DOCUMENT
    )
    if len(document_roots) != 1:
        raise LlmScopeLocationRejected(
            LlmScopeLocationRejectionCode.INPUT_BINDING_INVALID
        )
    document_root = document_roots[0]
    direct_branches = tuple(
        node
        for node in tree.nodes
        if node.parent_structure_node_id == document_root.structure_node_id
    )
    selected_branch_ids = {
        node.structure_node_id
        for node in direct_branches
        if node.boundary_basis
        not in {
            StructureBoundaryBasis.PAGE_FURNITURE,
            StructureBoundaryBasis.TOC_BLOCK,
        }
    }
    if not selected_branch_ids:
        raise ValueError("document contains no locatable BODY branches")

    branch_by_node: dict[str, str] = {}
    for node in tree.nodes:
        if node.kind is StructureNodeKind.DOCUMENT:
            continue
        current = node
        visited: set[str] = set()
        while current.parent_structure_node_id != document_root.structure_node_id:
            parent_id = current.parent_structure_node_id
            if (
                parent_id is None
                or parent_id in visited
                or parent_id not in nodes_by_id
            ):
                raise LlmScopeLocationRejected(
                    LlmScopeLocationRejectionCode.INPUT_BINDING_INVALID
                )
            visited.add(parent_id)
            current = nodes_by_id[parent_id]
        branch_by_node[node.structure_node_id] = current.structure_node_id

    changed = True
    while changed:
        changed = False
        for relation in tree.relations:
            if relation.kind is not StructureRelationKind.CONTINUES:
                continue
            source_branch = branch_by_node.get(relation.source_structure_node_id)
            target_branch = branch_by_node.get(relation.target_structure_node_id)
            if source_branch is None or target_branch is None:
                raise LlmScopeLocationRejected(
                    LlmScopeLocationRejectionCode.INPUT_BINDING_INVALID
                )
            if (
                source_branch in selected_branch_ids
                or target_branch in selected_branch_ids
            ):
                before = len(selected_branch_ids)
                selected_branch_ids.update((source_branch, target_branch))
                changed = changed or len(selected_branch_ids) != before

    root_ids = tuple(
        node.structure_node_id
        for node in direct_branches
        if node.structure_node_id in selected_branch_ids
    )
    scope_ids = tuple(
        node.structure_node_id
        for node in tree.nodes
        if branch_by_node.get(node.structure_node_id) in selected_branch_ids
    )
    payload = {
        "version": LLM_DOCUMENT_ROOT_SCOPE_LOCATOR_VERSION,
        "source_sha256": source_ir.source_sha256,
        "structure_snapshot_id": structure_snapshot_id(tree),
        "root_structure_node_ids": root_ids,
        "scope_structure_node_ids": scope_ids,
    }
    locator_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return LocatedLlmExtractionScope(
        locator_version=LLM_DOCUMENT_ROOT_SCOPE_LOCATOR_VERSION,
        locator_sha256=locator_sha256,
        source_sha256=source_ir.source_sha256,
        root_section_structure_node_ids=root_ids,
        scope_structure_node_ids=scope_ids,
    )


def _section_source_locator_constraint(
    node: StructureNode,
    source_locators: dict[StructureSourceRef, _ResolvedPdfLocator],
) -> LlmScopeSourceLocatorConstraint | None:
    for source_ref in node.source_refs:
        resolved = source_locators.get(source_ref)
        if resolved is None:
            continue
        return LlmScopeSourceLocatorConstraint(
            source_ref_kind=resolved.source_ref_kind,
            page_number=resolved.page_number,
            block_index=resolved.block_index,
            bbox=resolved.bbox,
            table_index=resolved.table_index,
            row_index=resolved.row_index,
            column_index=resolved.column_index,
            observation_index=resolved.observation_index,
            provider_id=resolved.provider_id,
            provider_version=resolved.provider_version,
        )
    return None


def _normalized_section_path(
    node: StructureNode,
    nodes_by_id: dict[str, StructureNode],
) -> tuple[str, ...]:
    labels: list[str] = []
    current: StructureNode | None = node
    while current is not None:
        if (
            current.kind is StructureNodeKind.SECTION
            and current.explicit_label is not None
        ):
            labels.append(_logical_section_label(current.explicit_label))
        parent_id = current.parent_structure_node_id
        current = nodes_by_id.get(parent_id) if parent_id is not None else None
    return tuple(reversed(labels))


def _owned_provision_ids(
    root: StructureNode,
    nodes: tuple[StructureNode, ...],
    nodes_by_id: dict[str, StructureNode],
) -> tuple[str, ...]:
    owned: list[str] = [root.structure_node_id]
    for candidate in nodes:
        if candidate.structure_node_id == root.structure_node_id:
            continue
        if candidate.kind is StructureNodeKind.SECTION:
            continue
        parent_id = candidate.parent_structure_node_id
        while parent_id is not None:
            parent = nodes_by_id[parent_id]
            if parent.kind is StructureNodeKind.SECTION:
                if parent.structure_node_id == root.structure_node_id:
                    owned.append(candidate.structure_node_id)
                break
            parent_id = parent.parent_structure_node_id
    return tuple(owned)


def _source_locators(
    source_ir: SourceIRSnapshot,
) -> dict[StructureSourceRef, _ResolvedPdfLocator]:
    result: dict[StructureSourceRef, _ResolvedPdfLocator] = {}
    for node in source_ir.nodes:
        native = node.native_locator
        if native.format is NativeDocumentFormat.PDF:
            result[
                StructureSourceRef(
                    kind=StructureSourceRefKind.SOURCE_NODE,
                    source_id=node.source_node_id,
                )
            ] = _from_native_locator(
                StructureSourceRefKind.SOURCE_NODE,
                native,
            )
        if node.table_payload is not None:
            for cell in node.table_payload.cells:
                native = cell.native_locator
                if native.format is NativeDocumentFormat.PDF:
                    result[
                        StructureSourceRef(
                            kind=StructureSourceRefKind.TABLE_CELL,
                            source_id=cell.cell_id,
                        )
                    ] = _from_native_locator(
                        StructureSourceRefKind.TABLE_CELL,
                        native,
                    )
    for snapshot in source_ir.visual_text_snapshots:
        for observation in snapshot.observations:
            result[
                StructureSourceRef(
                    kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
                    source_id=observation.observation_id,
                )
            ] = _ResolvedPdfLocator(
                source_ref_kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
                page_number=observation.page_number,
                bbox=observation.bbox,
                observation_index=observation.observation_index,
                provider_id=observation.provider_id,
                provider_version=observation.provider_version,
            )
    return result


def _from_native_locator(
    source_ref_kind: StructureSourceRefKind,
    native: NativeLocator,
) -> _ResolvedPdfLocator:
    if native.page_number is None:
        raise LlmScopeLocationRejected(
            LlmScopeLocationRejectionCode.INPUT_BINDING_INVALID
        )
    return _ResolvedPdfLocator(
        source_ref_kind=source_ref_kind,
        page_number=native.page_number,
        block_index=native.block_index,
        bbox=native.bbox,
        table_index=native.table_index,
        row_index=native.row_index,
        column_index=native.column_index,
    )


def _node_matches_source_locator(
    node: StructureNode,
    constraint: LlmScopeSourceLocatorConstraint,
    source_locators: dict[StructureSourceRef, _ResolvedPdfLocator],
) -> bool:
    return any(
        resolved is not None and _locator_matches(resolved, constraint)
        for source_ref in node.source_refs
        if source_ref.kind is constraint.source_ref_kind
        for resolved in (source_locators.get(source_ref),)
    )


def _locator_matches(
    resolved: _ResolvedPdfLocator,
    constraint: LlmScopeSourceLocatorConstraint,
) -> bool:
    if (
        resolved.source_ref_kind is not constraint.source_ref_kind
        or resolved.page_number != constraint.page_number
    ):
        return False
    for field_name in (
        "block_index",
        "bbox",
        "table_index",
        "row_index",
        "column_index",
        "observation_index",
        "provider_id",
        "provider_version",
    ):
        expected = getattr(constraint, field_name)
        if expected is not None and getattr(resolved, field_name) != expected:
            return False
    return True


def _canonical_locator_payload(locator: LlmExtractionScopeLocator) -> object:
    sections = []
    for selector in locator.section_selectors:
        source_locator = selector.source_locator
        sections.append(
            {
                "normalized_section_path": list(selector.normalized_section_path),
                "source_locator": (
                    {
                        "source_ref_kind": source_locator.source_ref_kind.value,
                        "page_number": source_locator.page_number,
                        "block_index": source_locator.block_index,
                        "bbox": (
                            list(source_locator.bbox)
                            if source_locator.bbox is not None
                            else None
                        ),
                        "table_index": source_locator.table_index,
                        "row_index": source_locator.row_index,
                        "column_index": source_locator.column_index,
                        "observation_index": source_locator.observation_index,
                        "provider_id": source_locator.provider_id,
                        "provider_version": source_locator.provider_version,
                    }
                    if source_locator is not None
                    else None
                ),
            }
        )
    sections.sort(
        key=lambda value: json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return {
        "version": locator.version,
        "source_sha256": locator.source_sha256,
        "section_selectors": sections,
    }


def _section_label_parts(label: str) -> tuple[int, ...]:
    if not isinstance(label, str) or not _SECTION_LABEL_PATTERN.fullmatch(label):
        raise ValueError("section labels must use normalized numeric outlines")
    return tuple(int(part) for part in label.split("."))


def _logical_section_label(label: str) -> str:
    parts = _section_label_parts(label)
    if len(parts) == 2 and parts[1] == 0:
        return str(parts[0])
    return label


def _require_non_negative_integer(
    value: int,
    field_name: str,
    *,
    minimum: int = 0,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")


def _validate_bbox(value: tuple[float, float, float, float]) -> None:
    if not isinstance(value, tuple) or len(value) != 4:
        raise ValueError("bbox must be a four-number tuple")
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in value
    ):
        raise ValueError("bbox must contain finite numbers")
    x0, y0, x1, y1 = (float(item) for item in value)
    if x1 < x0 or y1 < y0:
        raise ValueError("bbox coordinates must be ordered")


__all__ = [
    "LLM_DOCUMENT_ROOT_SCOPE_LOCATOR_VERSION",
    "LLM_EXTRACTION_SCOPE_LOCATOR_VERSION",
    "LlmExtractionScopeLocator",
    "LlmScopeLocationRejected",
    "LlmScopeLocationRejectionCode",
    "LlmScopeSourceLocatorConstraint",
    "LlmSectionScopeSelector",
    "LocatedLlmExtractionScope",
    "build_llm_document_scope_locator",
    "locate_llm_document_scope",
    "locate_llm_extraction_scope",
]
