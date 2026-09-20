from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.extraction.requirement_tree import (
    RequirementTreeSnapshotV1,
    canonical_requirement_tree_sha256,
)


ENTRY_CLASSIFICATION_SCHEMA_VERSION = "entry-classification-snapshot/v1"
ENTRY_CLASSIFICATION_POLICY_VERSION = "entry-classification-policy/v1"
ENTRY_CLASSIFICATION_SUGGESTION_SCHEMA_VERSION = (
    "entry-classification-agent-suggestions/v1"
)

ENTRY_CLASSIFICATION_PROJECTION_ROLES = frozenset({
    "STRUCTURE_PROXY",
    "DIRECT_CONTENT",
    "WHOLE_TABLE_REVIEW",
    "TABLE_RECORD",
    "REQUIREMENT",
})


class EntryClassificationType(str, Enum):
    TECHNICAL = "TECHNICAL"
    NON_TECHNICAL = "NON_TECHNICAL"
    INFORMATION = "INFORMATION"


class EntryClassificationSuggestionSource(str, Enum):
    MODEL = "MODEL"
    RULE_EVIDENCE = "RULE_EVIDENCE"
    NONE = "NONE"


class EntryClassificationStatus(str, Enum):
    WORKING = "WORKING"
    ACCEPTED = "ACCEPTED"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntryClassificationEntryV1(_FrozenModel):
    tree_node_id: str = Field(min_length=1, max_length=260)
    workflow_node_id: str = Field(min_length=1, max_length=260)
    projection_role: str = Field(min_length=1, max_length=80)
    suggested_type: EntryClassificationType | None = None
    suggestion_source: EntryClassificationSuggestionSource = (
        EntryClassificationSuggestionSource.NONE
    )
    effective_type: EntryClassificationType | None = None
    human_override: EntryClassificationType | None = None
    reason_codes: tuple[str, ...] = Field(default=(), max_length=32)
    suggestion_evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_suggestion(self) -> EntryClassificationEntryV1:
        if self.suggestion_source is EntryClassificationSuggestionSource.NONE:
            if self.suggested_type is not None or self.confidence is not None:
                raise ValueError("NONE suggestions cannot carry a type or confidence")
        elif self.suggested_type is None:
            raise ValueError("a suggestion source requires a suggested type")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("classification reason codes must be unique")
        if len(self.suggestion_evidence_refs) != len(
            set(self.suggestion_evidence_refs)
        ) or any(not value.strip() for value in self.suggestion_evidence_refs):
            raise ValueError("classification evidence refs must be unique and non-empty")
        if any(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None
            for code in self.reason_codes
        ):
            raise ValueError("classification reasons must be bounded machine codes")
        return self


class EntryClassificationAgentSuggestionV1(_FrozenModel):
    workflow_node_id: str = Field(min_length=1, max_length=260)
    suggested_type: EntryClassificationType
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_reasoning_contract(self) -> EntryClassificationAgentSuggestionV1:
        if len(self.reason_codes) != len(set(self.reason_codes)) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is None
            for code in self.reason_codes
        ):
            raise ValueError("agent classification reasons must be unique machine codes")
        if len(self.evidence_refs) != len(set(self.evidence_refs)) or any(
            not value.strip() for value in self.evidence_refs
        ):
            raise ValueError("agent classification evidence must be unique and non-empty")
        return self


class EntryClassificationAgentSuggestionSetV1(_FrozenModel):
    schema_version: Literal["entry-classification-agent-suggestions/v1"] = (
        ENTRY_CLASSIFICATION_SUGGESTION_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    working_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_structure_id: str = Field(min_length=1, max_length=200)
    policy_version: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1, max_length=160)
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[EntryClassificationAgentSuggestionV1, ...]

    @model_validator(mode="after")
    def validate_entry_identities(self) -> EntryClassificationAgentSuggestionSetV1:
        identities = tuple(item.workflow_node_id for item in self.entries)
        if len(identities) != len(set(identities)):
            raise ValueError("agent classification workflow IDs must be unique")
        return self


class EntryClassificationSnapshotV1(_FrozenModel):
    schema_version: Literal["entry-classification-snapshot/v1"] = (
        ENTRY_CLASSIFICATION_SCHEMA_VERSION
    )
    snapshot_id: str = Field(min_length=1, max_length=200)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str = Field(min_length=1, max_length=160)
    sor_document_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    working_tree_id: str = Field(min_length=1, max_length=200)
    working_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_structure_id: str = Field(min_length=1, max_length=200)
    semantic_snapshot_id: str = Field(default="", max_length=200)
    policy_version: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=0)
    status: EntryClassificationStatus
    entries: tuple[EntryClassificationEntryV1, ...]
    supersedes_id: str = Field(default="", max_length=200)
    created_at: str = Field(default="", max_length=80)
    accepted_at: str = Field(default="", max_length=80)
    accepted_by: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_snapshot(self) -> EntryClassificationSnapshotV1:
        tree_ids = [entry.tree_node_id for entry in self.entries]
        workflow_ids = [entry.workflow_node_id for entry in self.entries]
        if len(tree_ids) != len(set(tree_ids)):
            raise ValueError("classification tree node IDs must be unique")
        if len(workflow_ids) != len(set(workflow_ids)):
            raise ValueError("classification workflow node IDs must be unique")
        if self.status is EntryClassificationStatus.WORKING:
            if self.accepted_at or self.accepted_by:
                raise ValueError("a working snapshot cannot have an acceptance receipt")
            if any(
                entry.effective_type != entry.human_override
                for entry in self.entries
            ):
                raise ValueError("working effective types come only from human overrides")
        else:
            if not self.accepted_at or not self.accepted_by:
                raise ValueError("an accepted snapshot requires an acceptance receipt")
            if any(entry.effective_type is None for entry in self.entries):
                raise ValueError("accepted entries must all have an effective type")
            if any(
                entry.effective_type
                != (entry.human_override or entry.suggested_type)
                for entry in self.entries
            ):
                raise ValueError(
                    "accepted effective types must match their override or suggestion"
                )
        return self


class UnclassifiedEntriesError(ValueError):
    def __init__(self, workflow_node_ids: tuple[str, ...]) -> None:
        super().__init__("all entries must be classified before acceptance")
        self.workflow_node_ids = workflow_node_ids


def build_initial_entry_classification_snapshot(
    tree: RequirementTreeSnapshotV1,
    *,
    policy_version: str = ENTRY_CLASSIFICATION_POLICY_VERSION,
    semantic_snapshot_id: str = "",
    agent_suggestions: EntryClassificationAgentSuggestionSetV1 | None = None,
) -> EntryClassificationSnapshotV1:
    """Build the deterministic, non-persisted revision-zero classification view.

    W3 deliberately has no dedicated three-class suggestion artifact.  Existing
    semantic labels and deterministic rule evidence therefore never become a
    classification suggestion here.
    """

    working_tree_sha256 = canonical_requirement_tree_sha256(tree)
    ordered_nodes = entry_classification_nodes_in_tree_order(tree)
    suggestion_by_id: dict[str, EntryClassificationAgentSuggestionV1] = {}
    if agent_suggestions is not None:
        expected_workflow_ids = tuple(
            node.workflow_node_id or node.id for node in ordered_nodes
        )
        actual_workflow_ids = tuple(
            entry.workflow_node_id for entry in agent_suggestions.entries
        )
        if (
            agent_suggestions.source_sha256 != tree.source_identity.source_sha256
            or agent_suggestions.working_tree_sha256 != working_tree_sha256
            or agent_suggestions.effective_structure_id
            != tree.structure_identity.effective_snapshot_id
            or agent_suggestions.policy_version != policy_version
            or actual_workflow_ids != expected_workflow_ids
        ):
            raise ValueError("agent classification suggestions do not match the tree")
        suggestion_by_id = {
            item.workflow_node_id: item for item in agent_suggestions.entries
        }
    entries = tuple(
        EntryClassificationEntryV1(
            tree_node_id=node.id,
            workflow_node_id=node.workflow_node_id or node.id,
            projection_role=node.projection_role,
            suggested_type=(
                suggestion_by_id[node.workflow_node_id or node.id].suggested_type
                if suggestion_by_id
                else None
            ),
            suggestion_source=(
                EntryClassificationSuggestionSource.MODEL
                if suggestion_by_id
                else EntryClassificationSuggestionSource.NONE
            ),
            effective_type=None,
            human_override=None,
            reason_codes=(
                suggestion_by_id[node.workflow_node_id or node.id].reason_codes
                if suggestion_by_id
                else ("NO_DEDICATED_CLASSIFICATION_SUGGESTION",)
            ),
            suggestion_evidence_refs=(
                suggestion_by_id[node.workflow_node_id or node.id].evidence_refs
                if suggestion_by_id
                else ()
            ),
            confidence=(
                suggestion_by_id[node.workflow_node_id or node.id].confidence
                if suggestion_by_id
                else None
            ),
        )
        for node in ordered_nodes
    )
    initial_identity = _sha256_json(
        {
            "schema_version": ENTRY_CLASSIFICATION_SCHEMA_VERSION,
            "project_id": tree.source_identity.project_id,
            "sor_document_id": tree.source_identity.document_id,
            "source_sha256": tree.source_identity.source_sha256,
            "working_tree_sha256": working_tree_sha256,
            "effective_structure_id": (
                tree.structure_identity.effective_snapshot_id
            ),
            "semantic_snapshot_id": semantic_snapshot_id,
            "policy_version": policy_version,
            "entries": [entry.model_dump(mode="json") for entry in entries],
        }
    )
    return _with_payload_sha256(
        EntryClassificationSnapshotV1(
            snapshot_id=f"initial-{initial_identity}",
            payload_sha256="0" * 64,
            project_id=tree.source_identity.project_id,
            sor_document_id=tree.source_identity.document_id,
            source_sha256=tree.source_identity.source_sha256,
            working_tree_id=working_tree_sha256,
            working_tree_sha256=working_tree_sha256,
            effective_structure_id=(
                tree.structure_identity.effective_snapshot_id
            ),
            semantic_snapshot_id=semantic_snapshot_id,
            policy_version=policy_version,
            revision=0,
            status=EntryClassificationStatus.WORKING,
            entries=entries,
        )
    )


def revise_entry_classification_snapshot(
    current: EntryClassificationSnapshotV1,
    *,
    overrides: Mapping[
        str,
        EntryClassificationType | str | None,
    ],
    snapshot_id: str,
    created_at: str,
) -> EntryClassificationSnapshotV1:
    """Append one WORKING revision with only explicit human overrides changed."""

    known_ids = {entry.workflow_node_id for entry in current.entries}
    unknown_ids = set(overrides) - known_ids
    if unknown_ids:
        raise KeyError(sorted(unknown_ids)[0])
    normalized = {
        workflow_node_id: (
            None if value is None else EntryClassificationType(value)
        )
        for workflow_node_id, value in overrides.items()
    }
    if all(
        normalized.get(entry.workflow_node_id, entry.human_override)
        == entry.human_override
        for entry in current.entries
    ):
        return current
    if snapshot_id == current.snapshot_id:
        raise ValueError("a classification revision cannot supersede itself")
    entries = tuple(
        entry.model_copy(
            update={
                "human_override": normalized.get(
                    entry.workflow_node_id,
                    entry.human_override,
                ),
                "effective_type": normalized.get(
                    entry.workflow_node_id,
                    entry.human_override,
                ),
            }
        )
        for entry in current.entries
    )
    return _with_payload_sha256(
        EntryClassificationSnapshotV1(
            **_lineage_fields(current),
            snapshot_id=snapshot_id,
            payload_sha256="0" * 64,
            revision=current.revision + 1,
            status=EntryClassificationStatus.WORKING,
            entries=entries,
            supersedes_id=current.snapshot_id,
            created_at=created_at,
        )
    )


def accept_entry_classification_snapshot(
    current: EntryClassificationSnapshotV1,
    *,
    snapshot_id: str,
    accepted_at: str,
    accepted_by: str,
) -> EntryClassificationSnapshotV1:
    """Append an immutable ACCEPTED revision after full classification."""

    if current.status is not EntryClassificationStatus.WORKING:
        raise ValueError("only a working classification snapshot can be accepted")
    if snapshot_id == current.snapshot_id:
        raise ValueError("a classification revision cannot supersede itself")
    effective_types = tuple(
        entry.human_override or entry.suggested_type
        for entry in current.entries
    )
    missing = tuple(
        entry.workflow_node_id
        for entry, effective_type in zip(
            current.entries,
            effective_types,
            strict=True,
        )
        if effective_type is None
    )
    if missing:
        raise UnclassifiedEntriesError(missing)
    entries = tuple(
        entry.model_copy(update={"effective_type": effective_type})
        for entry, effective_type in zip(
            current.entries,
            effective_types,
            strict=True,
        )
    )
    return _with_payload_sha256(
        EntryClassificationSnapshotV1(
            **_lineage_fields(current),
            snapshot_id=snapshot_id,
            payload_sha256="0" * 64,
            revision=current.revision + 1,
            status=EntryClassificationStatus.ACCEPTED,
            entries=entries,
            supersedes_id=current.snapshot_id,
            created_at=accepted_at,
            accepted_at=accepted_at,
            accepted_by=accepted_by,
        )
    )


def canonical_entry_classification_payload_bytes(
    snapshot: EntryClassificationSnapshotV1,
) -> bytes:
    """Canonical persisted payload; the digest itself is intentionally excluded."""

    return json.dumps(
        snapshot.model_dump(mode="json", exclude={"payload_sha256"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_entry_classification_payload_sha256(
    snapshot: EntryClassificationSnapshotV1,
) -> str:
    return hashlib.sha256(
        canonical_entry_classification_payload_bytes(snapshot)
    ).hexdigest()


def entry_classification_nodes_in_tree_order(
    tree: RequirementTreeSnapshotV1,
):
    """Return the exact reviewable entry projection in canonical tree order."""

    node_by_id = {node.id: node for node in tree.nodes}
    ordered = []
    visited: set[str] = set()
    pending = list(reversed(tree.root_node_ids))
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        node = node_by_id[node_id]
        if (
            node.include_in_view
            and node.projection_role in ENTRY_CLASSIFICATION_PROJECTION_ROLES
        ):
            ordered.append(node)
        pending.extend(reversed(node.child_ids))
    return tuple(ordered)


def _lineage_fields(snapshot: EntryClassificationSnapshotV1) -> dict[str, str]:
    return {
        "project_id": snapshot.project_id,
        "sor_document_id": snapshot.sor_document_id,
        "source_sha256": snapshot.source_sha256,
        "working_tree_id": snapshot.working_tree_id,
        "working_tree_sha256": snapshot.working_tree_sha256,
        "effective_structure_id": snapshot.effective_structure_id,
        "semantic_snapshot_id": snapshot.semantic_snapshot_id,
        "policy_version": snapshot.policy_version,
    }


def _with_payload_sha256(
    snapshot: EntryClassificationSnapshotV1,
) -> EntryClassificationSnapshotV1:
    return snapshot.model_copy(
        update={
            "payload_sha256": canonical_entry_classification_payload_sha256(
                snapshot
            )
        }
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ENTRY_CLASSIFICATION_PROJECTION_ROLES",
    "ENTRY_CLASSIFICATION_POLICY_VERSION",
    "ENTRY_CLASSIFICATION_SCHEMA_VERSION",
    "ENTRY_CLASSIFICATION_SUGGESTION_SCHEMA_VERSION",
    "EntryClassificationAgentSuggestionSetV1",
    "EntryClassificationAgentSuggestionV1",
    "EntryClassificationEntryV1",
    "EntryClassificationSnapshotV1",
    "EntryClassificationStatus",
    "EntryClassificationSuggestionSource",
    "EntryClassificationType",
    "UnclassifiedEntriesError",
    "accept_entry_classification_snapshot",
    "build_initial_entry_classification_snapshot",
    "canonical_entry_classification_payload_bytes",
    "canonical_entry_classification_payload_sha256",
    "entry_classification_nodes_in_tree_order",
    "revise_entry_classification_snapshot",
]
