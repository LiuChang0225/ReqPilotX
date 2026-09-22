from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


WORKING_REVISION_SCHEMA_VERSION = "native-structure-working-revision/v1"
WORKING_REVISION_HEAD_SCHEMA_VERSION = (
    "native-structure-working-revision-head/v1"
)
WORKING_REVISION_MEDIA_TYPE = (
    "application/vnd.reqpilotx.native-structure-working-revision+json"
)
WORKING_REVISION_OBJECT_SCHEMA_VERSION = (
    "native-structure-working-revision-object/v1"
)
TABLE_PREVIEW_MEDIA_TYPE = (
    "application/vnd.reqpilotx.native-structure-table-preview+json"
)
D2_HOST_COMPILER_RECEIPT_SCHEMA_VERSION = (
    "reqpilotx-d2-host-compiler-receipt/v1"
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NativeStructureWorkingRevisionError(ValueError):
    pass


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class NativeStructureTablePreviewBindingV1(_StrictFrozenModel):
    schema_version: Literal["native-structure-table-preview-binding/v1"] = (
        "native-structure-table-preview-binding/v1"
    )
    scope_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_key: str = Field(min_length=1, max_length=1000)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_size: int = Field(gt=0)
    media_type: Literal[
        "application/vnd.reqpilotx.native-structure-table-preview+json"
    ] = TABLE_PREVIEW_MEDIA_TYPE
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    base_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NativeStructureWorkingRevisionHeadV1(_StrictFrozenModel):
    schema_version: Literal["native-structure-working-revision-head/v1"] = (
        WORKING_REVISION_HEAD_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    handoff_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_artifact_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    base_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_artifact_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    revision_id: str = Field(
        pattern=r"^native-structure-working-revision-[0-9a-f]{32}$"
    )
    revision: int = Field(ge=1)
    previous_revision_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    revision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_kind: Literal["ACCEPTED_REFERENCE_REPLAY", "D2_HOST_COMPILER"]
    d2_compiler_receipt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    state: Literal["WORKING", "ACCEPTED"]
    quality_claim: Literal["TRANSPORT_REPLAY_ONLY", "D2_HOST_VERIFIED"]
    created_at_utc: datetime

    @field_validator("created_at_utc", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_head(self) -> NativeStructureWorkingRevisionHeadV1:
        if self.created_at_utc.tzinfo is None or self.created_at_utc.utcoffset() is None:
            raise ValueError("working revision head timestamp must be timezone-aware")
        expected_claim = (
            "TRANSPORT_REPLAY_ONLY"
            if self.validator_kind == "ACCEPTED_REFERENCE_REPLAY"
            else "D2_HOST_VERIFIED"
        )
        if self.quality_claim != expected_claim:
            raise ValueError("working revision head quality claim mismatches validator")
        if (self.validator_kind == "D2_HOST_COMPILER") != (
            self.d2_compiler_receipt_sha256 is not None
        ):
            raise ValueError("D2 compiler receipt identity drifted on revision head")
        return self


class NativeStructureWorkingRevisionObjectV1(_StrictFrozenModel):
    schema_version: Literal["native-structure-working-revision-object/v1"] = (
        WORKING_REVISION_OBJECT_SCHEMA_VERSION
    )
    project_id: str = Field(min_length=1, max_length=200)
    document_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    handoff_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_artifact_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    base_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_artifact_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    revision_id: str = Field(
        pattern=r"^native-structure-working-revision-[0-9a-f]{32}$"
    )
    revision: int = Field(ge=1)
    previous_revision_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    revision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_kind: Literal["ACCEPTED_REFERENCE_REPLAY", "D2_HOST_COMPILER"]
    validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d2_compiler_receipt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    state: Literal["WORKING", "ACCEPTED"]
    quality_claim: Literal["TRANSPORT_REPLAY_ONLY", "D2_HOST_VERIFIED"]
    object_key: str = Field(min_length=1, max_length=1000)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_size: int = Field(gt=0)
    media_type: Literal[
        "application/vnd.reqpilotx.native-structure-working-revision+json"
    ] = WORKING_REVISION_MEDIA_TYPE
    created_at_utc: datetime

    @field_validator("created_at_utc", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_object(self) -> NativeStructureWorkingRevisionObjectV1:
        if self.created_at_utc.tzinfo is None or self.created_at_utc.utcoffset() is None:
            raise ValueError("working revision object timestamp must be timezone-aware")
        if self.object_key.startswith("/") or ".." in self.object_key.split("/"):
            raise ValueError("working revision object key is invalid")
        expected_claim = (
            "TRANSPORT_REPLAY_ONLY"
            if self.validator_kind == "ACCEPTED_REFERENCE_REPLAY"
            else "D2_HOST_VERIFIED"
        )
        if self.quality_claim != expected_claim:
            raise ValueError("working revision object quality claim mismatches validator")
        if (self.validator_kind == "D2_HOST_COMPILER") != (
            self.d2_compiler_receipt_sha256 is not None
        ):
            raise ValueError("D2 compiler receipt identity drifted on revision object")
        return self

    def head(self) -> NativeStructureWorkingRevisionHeadV1:
        return NativeStructureWorkingRevisionHeadV1(
            run_id=self.run_id,
            handoff_id=self.handoff_id,
            source_sha256=self.source_sha256,
            base_artifact_id=self.base_artifact_id,
            base_projection_sha256=self.base_projection_sha256,
            evidence_artifact_id=self.evidence_artifact_id,
            revision_id=self.revision_id,
            revision=self.revision,
            previous_revision_sha256=self.previous_revision_sha256,
            revision_sha256=self.revision_sha256,
            validator_kind=self.validator_kind,
            d2_compiler_receipt_sha256=self.d2_compiler_receipt_sha256,
            state=self.state,
            quality_claim=self.quality_claim,
            created_at_utc=self.created_at_utc,
        )


class NativeStructureWorkingRevisionV1(_StrictFrozenModel):
    schema_version: Literal["native-structure-working-revision/v1"] = (
        WORKING_REVISION_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    handoff_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_artifact_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    base_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_artifact_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
    )
    revision_id: str = Field(
        pattern=r"^native-structure-working-revision-[0-9a-f]{32}$"
    )
    revision: int = Field(ge=1)
    previous_revision_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    revision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_kind: Literal["ACCEPTED_REFERENCE_REPLAY", "D2_HOST_COMPILER"]
    validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d2_compiler_receipt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    validation_status: Literal["PASS"] = "PASS"
    state: Literal["WORKING", "ACCEPTED"]
    quality_claim: Literal["TRANSPORT_REPLAY_ONLY", "D2_HOST_VERIFIED"]
    document_tree: dict[str, Any]
    node_states: dict[str, Literal["WORKING_VERIFIED", "ACCEPTED"]]
    inserted_node_ids: tuple[str, ...] = ()
    changed_node_ids: tuple[str, ...] = ()
    retired_node_ids: tuple[str, ...] = ()
    successor_node_ids: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    page_ownership: tuple[dict[str, Any], ...] = ()
    logical_tables: tuple[dict[str, Any], ...] = ()
    table_preview_bindings: tuple[NativeStructureTablePreviewBindingV1, ...] = ()
    structure_proposals: tuple[dict[str, Any], ...] = ()
    unresolved: tuple[dict[str, Any], ...] = ()
    visual_summary: dict[str, Any]
    created_at_utc: datetime

    @field_validator("created_at_utc", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_revision(self) -> NativeStructureWorkingRevisionV1:
        if self.created_at_utc.tzinfo is None or self.created_at_utc.utcoffset() is None:
            raise ValueError("working revision timestamp must be timezone-aware")
        if self.revision == 1 and self.previous_revision_sha256 is not None:
            raise ValueError("first working revision cannot have a predecessor")
        if self.revision > 1 and self.previous_revision_sha256 is None:
            raise ValueError("later working revisions require a predecessor")
        expected_claim = (
            "TRANSPORT_REPLAY_ONLY"
            if self.validator_kind == "ACCEPTED_REFERENCE_REPLAY"
            else "D2_HOST_VERIFIED"
        )
        if self.quality_claim != expected_claim:
            raise ValueError("working revision quality claim mismatches validator")
        if (self.validator_kind == "D2_HOST_COMPILER") != (
            self.d2_compiler_receipt_sha256 is not None
        ):
            raise ValueError("D2 compiler receipt identity drifted on working revision")
        node_ids = _validate_document_tree(self.document_tree)
        _validate_support_projection(
            node_ids=node_ids,
            document_tree=self.document_tree,
            page_ownership=self.page_ownership,
            logical_tables=self.logical_tables,
            structure_proposals=self.structure_proposals,
            unresolved=self.unresolved,
            visual_summary=self.visual_summary,
        )
        binding_ids = tuple(item.scope_id for item in self.table_preview_bindings)
        table_ids = tuple(str(item.get("scope_id") or "") for item in self.logical_tables)
        if binding_ids != table_ids or len(binding_ids) != len(set(binding_ids)):
            raise ValueError("working revision table preview bindings are incomplete")
        if any(
            item.source_sha256 != self.source_sha256
            or item.base_artifact_id != self.base_artifact_id
            or item.base_projection_sha256 != self.base_projection_sha256
            for item in self.table_preview_bindings
        ):
            raise ValueError("working revision table preview binding drifted")
        if set(self.node_states) != set(node_ids):
            raise ValueError("working revision node states must follow every tree node")
        expected_state = "ACCEPTED" if self.state == "ACCEPTED" else "WORKING_VERIFIED"
        if any(value != expected_state for value in self.node_states.values()):
            raise ValueError("working revision node state mismatches revision state")
        _validate_unique_node_ids(self.inserted_node_ids, "inserted")
        _validate_unique_node_ids(self.changed_node_ids, "changed")
        _validate_unique_node_ids(self.retired_node_ids, "retired")
        if (
            set(self.inserted_node_ids) & set(self.changed_node_ids)
            or set(self.inserted_node_ids) & set(self.retired_node_ids)
            or set(self.changed_node_ids) & set(self.retired_node_ids)
        ):
            raise ValueError("working revision diff sets must be disjoint")
        if self.revision == 1 and (
            self.inserted_node_ids != node_ids
            or self.changed_node_ids
            or self.retired_node_ids
            or self.successor_node_ids
        ):
            raise ValueError("first working revision diff must cover empty to current")
        live = set(node_ids)
        retired = set(self.retired_node_ids)
        for retired_id, successors in self.successor_node_ids.items():
            if retired_id not in retired:
                raise ValueError("successor mapping must originate from a retired node")
            if not successors or len(successors) != len(set(successors)):
                raise ValueError("successor mapping must contain unique live nodes")
            if any(item not in live for item in successors):
                raise ValueError("successor mapping references a missing live node")
        if self.revision_id != stable_native_structure_working_revision_id(
            run_id=self.run_id,
            base_projection_sha256=self.base_projection_sha256,
            revision=self.revision,
        ):
            raise ValueError("working revision derived identity drifted")
        if self.validator_receipt_sha256 != canonical_json_sha256(
            _validator_receipt_payload(self)
        ):
            raise ValueError("working revision validator receipt drifted")
        if self.revision_sha256 != self.canonical_revision_sha256():
            raise ValueError("working revision SHA drifted")
        return self

    def canonical_revision_sha256(self) -> str:
        return canonical_json_sha256(_revision_hash_payload(self))

    def head(self) -> NativeStructureWorkingRevisionHeadV1:
        return NativeStructureWorkingRevisionHeadV1(
            run_id=self.run_id,
            handoff_id=self.handoff_id,
            source_sha256=self.source_sha256,
            base_artifact_id=self.base_artifact_id,
            base_projection_sha256=self.base_projection_sha256,
            evidence_artifact_id=self.evidence_artifact_id,
            revision_id=self.revision_id,
            revision=self.revision,
            previous_revision_sha256=self.previous_revision_sha256,
            revision_sha256=self.revision_sha256,
            validator_kind=self.validator_kind,
            d2_compiler_receipt_sha256=self.d2_compiler_receipt_sha256,
            state=self.state,
            quality_claim=self.quality_claim,
            created_at_utc=self.created_at_utc,
        )


class D2HostCompilerReceiptV1(_StrictFrozenModel):
    """Typed proof that a working revision was compiled by the Host from attested scopes."""

    schema_version: Literal["reqpilotx-d2-host-compiler-receipt/v1"] = (
        D2_HOST_COMPILER_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str = Field(pattern=r"^d2-compiler-receipt-[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    survey_compilation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_compilation_sha256s: tuple[str, ...] = Field(min_length=1, max_length=1024)
    compiled_node_count: int = Field(ge=1)
    compiled_table_count: int = Field(ge=0)
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> D2HostCompilerReceiptV1:
        if self.scope_compilation_sha256s != tuple(sorted(set(self.scope_compilation_sha256s))) or any(
            _SHA256.fullmatch(value) is None for value in self.scope_compilation_sha256s
        ):
            raise ValueError("D2 compiler scope identities are invalid")
        expected_id = "d2-compiler-receipt-" + canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        )[:32]
        if self.receipt_id != expected_id:
            raise ValueError("D2 compiler receipt ID drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("D2 compiler receipt SHA drifted")
        return self


def build_d2_host_compiler_receipt(
    *,
    run_id: str,
    source_sha256: str,
    source_ir_canonical_sha256: str,
    kit_bundle_sha256: str,
    survey_compilation_sha256: str,
    scope_compilation_sha256s: Sequence[str],
    compiled_node_count: int,
    compiled_table_count: int,
) -> D2HostCompilerReceiptV1:
    scope_hashes = tuple(sorted(set(scope_compilation_sha256s)))
    values = {
        "run_id": run_id,
        "source_sha256": source_sha256,
        "source_ir_canonical_sha256": source_ir_canonical_sha256,
        "kit_bundle_sha256": kit_bundle_sha256,
        "survey_compilation_sha256": survey_compilation_sha256,
        "scope_compilation_sha256s": scope_hashes,
        "compiled_node_count": compiled_node_count,
        "compiled_table_count": compiled_table_count,
    }
    draft = D2HostCompilerReceiptV1.model_construct(
        receipt_id="d2-compiler-receipt-" + "0" * 32,
        receipt_sha256="0" * 64,
        **values,
    )
    digest = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
    )
    receipt_id = f"d2-compiler-receipt-{digest[:32]}"
    draft_with_id = D2HostCompilerReceiptV1.model_construct(
        receipt_id=receipt_id,
        receipt_sha256="0" * 64,
        **values,
    )
    full_digest = canonical_json_sha256(
        draft_with_id.model_dump(mode="json", exclude={"receipt_sha256"})
    )
    return D2HostCompilerReceiptV1(
        receipt_id=receipt_id,
        receipt_sha256=full_digest,
        **values,
    )


def compile_native_structure_working_revision(
    *,
    run_id: str,
    source_sha256: str,
    base_artifact_id: str,
    base_projection_sha256: str,
    evidence_artifact_id: str,
    revision: int,
    previous: NativeStructureWorkingRevisionV1 | None,
    validator_kind: Literal["ACCEPTED_REFERENCE_REPLAY", "D2_HOST_COMPILER"],
    state: Literal["WORKING", "ACCEPTED"],
    candidate_document_tree: Mapping[str, Any],
    page_ownership: Sequence[Mapping[str, Any]],
    logical_tables: Sequence[Mapping[str, Any]],
    table_preview_bindings: Sequence[NativeStructureTablePreviewBindingV1 | Mapping[str, Any]],
    structure_proposals: Sequence[Mapping[str, Any]],
    unresolved: Sequence[Mapping[str, Any]],
    visual_summary: Mapping[str, Any],
    d2_compiler_receipt: D2HostCompilerReceiptV1 | None = None,
    accepted_public_reference: Mapping[str, Any] | None = None,
    handoff_id: str | None = None,
    successor_node_ids: Mapping[str, Sequence[str]] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> NativeStructureWorkingRevisionV1:
    if validator_kind == "D2_HOST_COMPILER":
        if d2_compiler_receipt is None:
            raise NativeStructureWorkingRevisionError(
                "D2_HOST_COMPILER_REQUIRES_TYPED_SOURCE_EVIDENCE_RECEIPT"
            )
        if (
            d2_compiler_receipt.source_sha256 != source_sha256
            or d2_compiler_receipt.run_id != run_id
        ):
            raise NativeStructureWorkingRevisionError(
                "D2_HOST_COMPILER_RECEIPT_MISMATCH"
            )
    tree = copy.deepcopy(dict(candidate_document_tree))
    node_ids = _validate_document_tree(tree)
    page_values = tuple(copy.deepcopy(dict(item)) for item in page_ownership)
    table_values = tuple(copy.deepcopy(dict(item)) for item in logical_tables)
    binding_values = tuple(
        item
        if isinstance(item, NativeStructureTablePreviewBindingV1)
        else NativeStructureTablePreviewBindingV1.model_validate(item)
        for item in table_preview_bindings
    )
    proposal_values = tuple(copy.deepcopy(dict(item)) for item in structure_proposals)
    unresolved_values = tuple(copy.deepcopy(dict(item)) for item in unresolved)
    visual_summary_value = copy.deepcopy(dict(visual_summary))
    if d2_compiler_receipt is not None and (
        d2_compiler_receipt.compiled_node_count != len(node_ids)
        or d2_compiler_receipt.compiled_table_count != len(table_values)
    ):
        raise NativeStructureWorkingRevisionError(
            "D2_HOST_COMPILER_RECEIPT_COUNT_MISMATCH"
        )
    _validate_support_projection(
        node_ids=node_ids,
        document_tree=tree,
        page_ownership=page_values,
        logical_tables=table_values,
        structure_proposals=proposal_values,
        unresolved=unresolved_values,
        visual_summary=visual_summary_value,
    )
    if previous is None:
        if revision != 1:
            raise NativeStructureWorkingRevisionError(
                "first working revision must have revision 1"
            )
        previous_nodes: tuple[dict[str, Any], ...] = ()
        previous_sha256 = None
    else:
        if (
            revision != previous.revision + 1
            or previous.run_id != run_id
            or previous.source_sha256 != source_sha256
            or previous.base_artifact_id != base_artifact_id
            or previous.base_projection_sha256 != base_projection_sha256
            or previous.evidence_artifact_id != evidence_artifact_id
            or previous.handoff_id != handoff_id
        ):
            raise NativeStructureWorkingRevisionError(
                "working revision does not continue the pinned predecessor"
            )
        previous_nodes = tuple(
            copy.deepcopy(item) for item in previous.document_tree["nodes"]
        )
        previous_sha256 = previous.revision_sha256

    if validator_kind == "ACCEPTED_REFERENCE_REPLAY":
        if accepted_public_reference is None:
            raise NativeStructureWorkingRevisionError(
                "accepted-reference replay requires the accepted public projection"
            )
        _validate_replay_subset(
            candidate_tree=tree,
            page_ownership=page_values,
            logical_tables=table_values,
            structure_proposals=proposal_values,
            unresolved=unresolved_values,
            visual_summary=visual_summary_value,
            accepted=accepted_public_reference,
            source_sha256=source_sha256,
            base_artifact_id=base_artifact_id,
            base_projection_sha256=base_projection_sha256,
            state=state,
        )

    current_nodes = tuple(copy.deepcopy(item) for item in tree["nodes"])
    inserted, changed, retired = _derive_diff(previous_nodes, current_nodes)
    successor_values = {
        str(key): tuple(str(item) for item in values)
        for key, values in (successor_node_ids or {}).items()
    }
    _validate_successors(
        successor_values,
        live_ids=set(node_ids),
        retired_ids=set(retired),
    )
    node_state = "ACCEPTED" if state == "ACCEPTED" else "WORKING_VERIFIED"
    node_states = {node_id: node_state for node_id in node_ids}
    values: dict[str, Any] = {
        "schema_version": WORKING_REVISION_SCHEMA_VERSION,
        "run_id": run_id,
        "handoff_id": handoff_id,
        "source_sha256": source_sha256,
        "base_artifact_id": base_artifact_id,
        "base_projection_sha256": base_projection_sha256,
        "evidence_artifact_id": evidence_artifact_id,
        "revision_id": stable_native_structure_working_revision_id(
            run_id=run_id,
            base_projection_sha256=base_projection_sha256,
            revision=revision,
        ),
        "revision": revision,
        "previous_revision_sha256": previous_sha256,
        "validator_kind": validator_kind,
        "validator_receipt_sha256": "0" * 64,
        "d2_compiler_receipt_sha256": (
            d2_compiler_receipt.receipt_sha256
            if d2_compiler_receipt is not None
            else None
        ),
        "validation_status": "PASS",
        "state": state,
        "quality_claim": (
            "TRANSPORT_REPLAY_ONLY"
            if validator_kind == "ACCEPTED_REFERENCE_REPLAY"
            else "D2_HOST_VERIFIED"
        ),
        "document_tree": tree,
        "node_states": node_states,
        "inserted_node_ids": inserted,
        "changed_node_ids": changed,
        "retired_node_ids": retired,
        "successor_node_ids": successor_values,
        "page_ownership": page_values,
        "logical_tables": table_values,
        "table_preview_bindings": binding_values,
        "structure_proposals": proposal_values,
        "unresolved": unresolved_values,
        "visual_summary": visual_summary_value,
        "created_at_utc": clock(),
    }
    draft = NativeStructureWorkingRevisionV1.model_construct(
        **values,
        revision_sha256="0" * 64,
    )
    values["validator_receipt_sha256"] = canonical_json_sha256(
        _validator_receipt_payload(draft)
    )
    draft = NativeStructureWorkingRevisionV1.model_construct(
        **values,
        revision_sha256="0" * 64,
    )
    return NativeStructureWorkingRevisionV1(
        **values,
        revision_sha256=canonical_json_sha256(_revision_hash_payload(draft)),
    )


def build_accepted_reference_replay_revisions(
    accepted_public_reference: Mapping[str, Any],
    *,
    run_id: str,
    evidence_artifact_id: str,
    table_preview_bindings: Sequence[NativeStructureTablePreviewBindingV1 | Mapping[str, Any]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[NativeStructureWorkingRevisionV1, ...]:
    projection = _mapping(accepted_public_reference.get("structure_projection"))
    if (
        projection.get("state") != "ACCEPTED"
        or projection.get("validation_status") != "PASS"
    ):
        raise NativeStructureWorkingRevisionError(
            "replay requires an accepted public projection"
        )
    tree = _mapping(accepted_public_reference.get("document_tree"))
    node_ids = _validate_document_tree(tree)
    roots = tuple(str(item) for item in tree["root_ids"])
    nonroots = tuple(item for item in node_ids if item not in set(roots))
    if len(nonroots) < 2:
        raise NativeStructureWorkingRevisionError(
            "accepted public projection is too small for three replay revisions"
        )
    middle_count = (len(nonroots) + 1) // 2
    included_sets = (
        set(roots),
        set(roots) | set(nonroots[:middle_count]),
        set(node_ids),
    )
    source_sha256 = str(accepted_public_reference.get("source_sha256") or "")
    base_artifact_id = str(accepted_public_reference.get("artifact_id") or "")
    base_projection_sha256 = str(projection.get("projection_sha256") or "")
    previous: NativeStructureWorkingRevisionV1 | None = None
    binding_by_scope = {
        item.scope_id: item
        for raw in table_preview_bindings
        for item in (
            raw
            if isinstance(raw, NativeStructureTablePreviewBindingV1)
            else NativeStructureTablePreviewBindingV1.model_validate(raw),
        )
    }
    revisions: list[NativeStructureWorkingRevisionV1] = []
    for ordinal, included in enumerate(included_sets, start=1):
        candidate_tree = _filter_tree(tree, included)
        support = _filter_support_projection(
            accepted_public_reference,
            candidate_tree=candidate_tree,
        )
        current = compile_native_structure_working_revision(
            run_id=run_id,
            source_sha256=source_sha256,
            base_artifact_id=base_artifact_id,
            base_projection_sha256=base_projection_sha256,
            evidence_artifact_id=evidence_artifact_id,
            revision=ordinal,
            previous=previous,
            validator_kind="ACCEPTED_REFERENCE_REPLAY",
            state="ACCEPTED" if ordinal == 3 else "WORKING",
            candidate_document_tree=candidate_tree,
            page_ownership=support["page_ownership"],
            logical_tables=support["logical_tables"],
            table_preview_bindings=tuple(
                binding_by_scope[str(item["scope_id"])]
                for item in support["logical_tables"]
            ),
            structure_proposals=support["structure_proposals"],
            unresolved=support["unresolved"],
            visual_summary=support["visual_summary"],
            accepted_public_reference=accepted_public_reference,
            clock=clock,
        )
        revisions.append(current)
        previous = current
    return tuple(revisions)


def native_structure_working_revision_bytes(
    revision: NativeStructureWorkingRevisionV1,
) -> bytes:
    verified = NativeStructureWorkingRevisionV1.model_validate(
        revision.model_dump(mode="python")
    )
    return json.dumps(
        verified.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def native_structure_working_revision_object_key(
    revision: NativeStructureWorkingRevisionV1,
) -> str:
    object_sha256 = hashlib.sha256(
        native_structure_working_revision_bytes(revision)
    ).hexdigest()
    return (
        "agent-structure/working-revisions/"
        f"{revision.source_sha256}/{revision.base_artifact_id}/"
        f"{revision.run_id}/{revision.revision:08d}-{object_sha256}.json"
    )


def build_native_structure_working_revision_object(
    *,
    project_id: str,
    document_id: str,
    revision: NativeStructureWorkingRevisionV1,
    object_key: str,
    object_sha256: str,
    object_size: int,
    media_type: str,
) -> NativeStructureWorkingRevisionObjectV1:
    verified = NativeStructureWorkingRevisionV1.model_validate(
        revision.model_dump(mode="python")
    )
    return NativeStructureWorkingRevisionObjectV1(
        project_id=project_id,
        document_id=document_id,
        run_id=verified.run_id,
        handoff_id=verified.handoff_id,
        source_sha256=verified.source_sha256,
        base_artifact_id=verified.base_artifact_id,
        base_projection_sha256=verified.base_projection_sha256,
        evidence_artifact_id=verified.evidence_artifact_id,
        revision_id=verified.revision_id,
        revision=verified.revision,
        previous_revision_sha256=verified.previous_revision_sha256,
        revision_sha256=verified.revision_sha256,
        validator_kind=verified.validator_kind,
        validator_receipt_sha256=verified.validator_receipt_sha256,
        d2_compiler_receipt_sha256=verified.d2_compiler_receipt_sha256,
        state=verified.state,
        quality_claim=verified.quality_claim,
        object_key=object_key,
        object_sha256=object_sha256,
        object_size=object_size,
        media_type=media_type,
        created_at_utc=verified.created_at_utc,
    )


def stable_native_structure_working_revision_id(
    *,
    run_id: str,
    base_projection_sha256: str,
    revision: int,
) -> str:
    digest = canonical_json_sha256(
        {
            "schema_version": WORKING_REVISION_SCHEMA_VERSION,
            "run_id": run_id,
            "base_projection_sha256": base_projection_sha256,
            "revision": revision,
        }
    )
    return f"native-structure-working-revision-{digest[:32]}"


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_json_default,
        ).encode("utf-8")
    ).hexdigest()


def validate_native_structure_table_preview(
    *,
    logical_table: Mapping[str, Any],
    preview: Mapping[str, Any],
    source_sha256: str,
    artifact_id: str,
) -> None:
    """Bind a materialized preview to the exact logical-table revision."""

    if preview.get("schema_version") != "native-structure-table-preview/v1":
        raise NativeStructureWorkingRevisionError(
            "working revision table preview schema is invalid"
        )
    scope_id = logical_table.get("scope_id")
    if (
        not isinstance(scope_id, str)
        or not scope_id
        or preview.get("logical_table_scope_id") != scope_id
        or preview.get("source_sha256") != source_sha256
        or preview.get("artifact_id") != artifact_id
        or preview.get("title") != logical_table.get("title")
    ):
        raise NativeStructureWorkingRevisionError(
            "working revision table preview identity drifted"
        )
    expected_fragments = _table_fragment_locators(
        logical_table.get("fragments"), label="logical table"
    )
    actual_fragments = _table_fragment_locators(
        preview.get("fragments"), label="table preview"
    )
    if expected_fragments != actual_fragments:
        raise NativeStructureWorkingRevisionError(
            "working revision table preview fragment lineage drifted"
        )


def _table_fragment_locators(
    raw_fragments: object,
    *,
    label: str,
) -> tuple[tuple[str, int, int, int], ...]:
    if not isinstance(raw_fragments, Sequence) or isinstance(
        raw_fragments, (str, bytes)
    ):
        raise NativeStructureWorkingRevisionError(
            f"working revision {label} fragments are invalid"
        )
    result: list[tuple[str, int, int, int]] = []
    for raw in raw_fragments:
        if not isinstance(raw, Mapping):
            raise NativeStructureWorkingRevisionError(
                f"working revision {label} fragment is invalid"
            )
        token = raw.get("table_token")
        page = raw.get("page")
        row_range = raw.get("row_range")
        if (
            not isinstance(token, str)
            or not token
            or type(page) is not int
            or page < 1
            or not isinstance(row_range, Sequence)
            or isinstance(row_range, (str, bytes))
            or len(row_range) != 2
            or any(type(value) is not int for value in row_range)
            or row_range[0] < 0
            or row_range[1] <= row_range[0]
        ):
            raise NativeStructureWorkingRevisionError(
                f"working revision {label} fragment locator is invalid"
            )
        result.append((token, page, row_range[0], row_range[1]))
    if len(set(result)) != len(result):
        raise NativeStructureWorkingRevisionError(
            f"working revision {label} fragments are duplicated"
        )
    return tuple(result)


def _revision_hash_payload(
    revision: NativeStructureWorkingRevisionV1,
) -> dict[str, Any]:
    payload = revision.model_dump(mode="json")
    payload.pop("revision_sha256", None)
    payload.pop("created_at_utc", None)
    return payload


def _validator_receipt_payload(
    revision: NativeStructureWorkingRevisionV1,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "native-structure-working-revision-validation/v1",
        "run_id": revision.run_id,
        "source_sha256": revision.source_sha256,
        "base_artifact_id": revision.base_artifact_id,
        "base_projection_sha256": revision.base_projection_sha256,
        "evidence_artifact_id": revision.evidence_artifact_id,
        "revision": revision.revision,
        "previous_revision_sha256": revision.previous_revision_sha256,
        "validator_kind": revision.validator_kind,
        "validation_status": "PASS",
        "state": revision.state,
        "document_tree_sha256": canonical_json_sha256(revision.document_tree),
        "node_count": len(revision.document_tree["nodes"]),
        "inserted_node_ids": list(revision.inserted_node_ids),
        "changed_node_ids": list(revision.changed_node_ids),
        "retired_node_ids": list(revision.retired_node_ids),
        "successor_node_ids": {
            key: list(values) for key, values in revision.successor_node_ids.items()
        },
        "table_preview_binding_sha256s": [
            item.preview_sha256 for item in revision.table_preview_bindings
        ],
    }
    if revision.d2_compiler_receipt_sha256 is not None:
        payload["d2_compiler_receipt_sha256"] = revision.d2_compiler_receipt_sha256
    return payload


def validate_native_structure_working_revision_lineage(
    current: NativeStructureWorkingRevisionV1,
    previous: NativeStructureWorkingRevisionV1 | None,
) -> None:
    previous_nodes = () if previous is None else tuple(previous.document_tree["nodes"])
    inserted, changed, retired = _derive_diff(
        previous_nodes, tuple(current.document_tree["nodes"])
    )
    if (
        current.revision != (1 if previous is None else previous.revision + 1)
        or current.previous_revision_sha256
        != (None if previous is None else previous.revision_sha256)
        or current.inserted_node_ids != inserted
        or current.changed_node_ids != changed
        or current.retired_node_ids != retired
        or (
            previous is not None
            and (
                current.run_id != previous.run_id
                or current.source_sha256 != previous.source_sha256
                or current.base_artifact_id != previous.base_artifact_id
                or current.base_projection_sha256 != previous.base_projection_sha256
                or current.evidence_artifact_id != previous.evidence_artifact_id
                or current.handoff_id != previous.handoff_id
            )
        )
    ):
        raise NativeStructureWorkingRevisionError(
            "working revision predecessor lineage is invalid"
        )


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("working revision timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _validate_document_tree(value: Mapping[str, Any]) -> tuple[str, ...]:
    if value.get("schema_version") != "native-document-structure-tree/v1":
        raise NativeStructureWorkingRevisionError(
            "working revision document tree schema is invalid"
        )
    nodes = value.get("nodes")
    roots = value.get("root_ids")
    if not isinstance(nodes, list) or not nodes:
        raise NativeStructureWorkingRevisionError(
            "working revision document tree has no nodes"
        )
    if not isinstance(roots, list) or not roots:
        raise NativeStructureWorkingRevisionError(
            "working revision document tree has no roots"
        )
    node_ids: list[str] = []
    by_id: dict[str, Mapping[str, Any]] = {}
    positions: dict[str, int] = {}
    sibling_orders: set[tuple[str, int]] = set()
    for position, raw in enumerate(nodes):
        if not isinstance(raw, Mapping):
            raise NativeStructureWorkingRevisionError(
                "working revision tree node is invalid"
            )
        node_id = str(raw.get("node_id") or "")
        if not _SAFE_ID.fullmatch(node_id) or node_id in by_id:
            raise NativeStructureWorkingRevisionError(
                "working revision stable node IDs are invalid"
            )
        order = raw.get("order")
        if type(order) is not int or order < 0:
            raise NativeStructureWorkingRevisionError(
                "working revision tree order is invalid"
            )
        parent = raw.get("parent_id")
        parent_key = "" if parent is None else str(parent)
        if parent is not None and not _SAFE_ID.fullmatch(parent_key):
            raise NativeStructureWorkingRevisionError(
                "working revision parent identity is invalid"
            )
        sibling_key = (parent_key, order)
        if sibling_key in sibling_orders:
            raise NativeStructureWorkingRevisionError(
                "working revision sibling order is duplicated"
            )
        sibling_orders.add(sibling_key)
        pages = raw.get("pages")
        if (
            not isinstance(pages, list)
            or not pages
            or pages != sorted(set(pages))
            or any(type(page) is not int or page < 1 for page in pages)
        ):
            raise NativeStructureWorkingRevisionError(
                "working revision node pages are invalid"
            )
        node_ids.append(node_id)
        by_id[node_id] = raw
        positions[node_id] = position
    expected_roots = [
        node_id for node_id in node_ids if by_id[node_id].get("parent_id") is None
    ]
    if [str(item) for item in roots] != expected_roots:
        raise NativeStructureWorkingRevisionError(
            "working revision tree roots are invalid"
        )
    for node_id in node_ids:
        parent = by_id[node_id].get("parent_id")
        if parent is not None:
            parent_id = str(parent)
            if parent_id not in by_id:
                raise NativeStructureWorkingRevisionError(
                    "working revision tree parent is missing"
                )
            if positions[parent_id] >= positions[node_id]:
                raise NativeStructureWorkingRevisionError(
                    "working revision parent must precede its child"
                )
        seen = {node_id}
        cursor = None if parent is None else str(parent)
        while cursor is not None:
            if cursor in seen:
                raise NativeStructureWorkingRevisionError(
                    "working revision tree contains a cycle"
                )
            seen.add(cursor)
            next_parent = by_id[cursor].get("parent_id")
            cursor = None if next_parent is None else str(next_parent)
    return tuple(node_ids)


def _validate_support_projection(
    *,
    node_ids: tuple[str, ...],
    document_tree: Mapping[str, Any],
    page_ownership: Sequence[Mapping[str, Any]],
    logical_tables: Sequence[Mapping[str, Any]],
    structure_proposals: Sequence[Mapping[str, Any]],
    unresolved: Sequence[Mapping[str, Any]],
    visual_summary: Mapping[str, Any],
) -> None:
    live = set(node_ids)
    table_ids: set[str] = set()
    for table in logical_tables:
        scope_id = str(table.get("scope_id") or "")
        if not _SAFE_ID.fullmatch(scope_id) or scope_id in table_ids:
            raise NativeStructureWorkingRevisionError(
                "working revision logical table identity is invalid"
            )
        table_ids.add(scope_id)
    for raw in document_tree["nodes"]:
        blocks = raw.get("content_blocks", [])
        if not isinstance(blocks, list):
            raise NativeStructureWorkingRevisionError(
                "working revision content blocks are invalid"
            )
        for block in blocks:
            if not isinstance(block, Mapping) or block.get("kind") != "LOGICAL_TABLE":
                raise NativeStructureWorkingRevisionError(
                    "working revision content block is invalid"
                )
            if str(block.get("logical_table_scope_id") or "") not in table_ids:
                raise NativeStructureWorkingRevisionError(
                    "working revision logical table support reference is missing"
                )
    proposal_ids: set[str] = set()
    for proposal in structure_proposals:
        proposal_id = str(proposal.get("proposal_id") or "")
        if not _SAFE_ID.fullmatch(proposal_id) or proposal_id in proposal_ids:
            raise NativeStructureWorkingRevisionError(
                "working revision proposal identity is invalid"
            )
        proposal_ids.add(proposal_id)
        for key in ("node_id", "recommended_target_node_id"):
            target = str(proposal.get(key) or "")
            if target and target not in live:
                raise NativeStructureWorkingRevisionError(
                    "working revision proposal references a missing node"
                )
    unit_ids: set[str] = set()
    for owner in page_ownership:
        unit_id = str(owner.get("unit_id") or "")
        if not _SAFE_ID.fullmatch(unit_id) or unit_id in unit_ids:
            raise NativeStructureWorkingRevisionError(
                "working revision page ownership identity is invalid"
            )
        unit_ids.add(unit_id)
    for table in logical_tables:
        owner = str(table.get("owner_unit_id") or "")
        if owner and owner not in unit_ids:
            raise NativeStructureWorkingRevisionError(
                "working revision logical table owner is missing"
            )
    unresolved_ids: set[str] = set()
    for item in unresolved:
        code = str(item.get("code") or "")
        if not _SAFE_ID.fullmatch(code) or code in unresolved_ids:
            raise NativeStructureWorkingRevisionError(
                "working revision unresolved identity is invalid"
            )
        unresolved_ids.add(code)
    _validate_visual_summary(visual_summary)


def _validate_replay_subset(
    *,
    candidate_tree: Mapping[str, Any],
    page_ownership: Sequence[Mapping[str, Any]],
    logical_tables: Sequence[Mapping[str, Any]],
    structure_proposals: Sequence[Mapping[str, Any]],
    unresolved: Sequence[Mapping[str, Any]],
    visual_summary: Mapping[str, Any],
    accepted: Mapping[str, Any],
    source_sha256: str,
    base_artifact_id: str,
    base_projection_sha256: str,
    state: str,
) -> None:
    projection = _mapping(accepted.get("structure_projection"))
    if (
        accepted.get("source_sha256") != source_sha256
        or accepted.get("artifact_id") != base_artifact_id
        or projection.get("state") != "ACCEPTED"
        or projection.get("validation_status") != "PASS"
        or projection.get("projection_sha256") != base_projection_sha256
    ):
        raise NativeStructureWorkingRevisionError(
            "working revision drifted from the accepted public projection"
        )
    accepted_tree = _mapping(accepted.get("document_tree"))
    if canonical_json_sha256(accepted_tree) != base_projection_sha256:
        raise NativeStructureWorkingRevisionError(
            "accepted public projection SHA is invalid"
        )
    accepted_nodes = {
        str(item.get("node_id") or ""): item for item in accepted_tree.get("nodes", [])
    }
    for candidate in candidate_tree["nodes"]:
        node_id = str(candidate.get("node_id") or "")
        if node_id not in accepted_nodes or canonical_json_sha256(
            candidate
        ) != canonical_json_sha256(accepted_nodes[node_id]):
            raise NativeStructureWorkingRevisionError(
                "replay node is not byte-identical to the accepted public projection"
            )
    for label, subset, accepted_values in (
        ("page ownership", page_ownership, accepted.get("page_ownership", [])),
        ("logical table", logical_tables, accepted.get("logical_tables", [])),
        (
            "structure proposal",
            structure_proposals,
            accepted.get("structure_proposals", []),
        ),
        ("unresolved", unresolved, accepted.get("unresolved", [])),
    ):
        allowed = {canonical_json_sha256(item) for item in accepted_values}
        if any(canonical_json_sha256(item) not in allowed for item in subset):
            raise NativeStructureWorkingRevisionError(
                f"replay {label} is not in the accepted public projection"
            )
    accepted_visual = accepted.get("visual_summary")
    if not isinstance(accepted_visual, Mapping):
        raise NativeStructureWorkingRevisionError(
            "accepted public projection visual summary is invalid"
        )
    allowed_receipts = {
        canonical_json_sha256(item)
        for item in accepted_visual.get("receipts", [])
    }
    if (
        visual_summary.get("page_count") != accepted_visual.get("page_count")
        or any(
            canonical_json_sha256(item) not in allowed_receipts
            for item in visual_summary.get("receipts", [])
        )
    ):
        raise NativeStructureWorkingRevisionError(
            "replay visual summary is not in the accepted public projection"
        )
    if state == "ACCEPTED" and canonical_json_sha256(candidate_tree) != (
        base_projection_sha256
    ):
        raise NativeStructureWorkingRevisionError(
            "accepted working revision does not match the base projection SHA"
        )


def _derive_diff(
    previous_nodes: Sequence[Mapping[str, Any]],
    current_nodes: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    previous_by_id = {str(item["node_id"]): item for item in previous_nodes}
    current_by_id = {str(item["node_id"]): item for item in current_nodes}
    inserted = tuple(
        str(item["node_id"])
        for item in current_nodes
        if str(item["node_id"]) not in previous_by_id
    )
    changed = tuple(
        str(item["node_id"])
        for item in current_nodes
        if str(item["node_id"]) in previous_by_id
        and canonical_json_sha256(item)
        != canonical_json_sha256(previous_by_id[str(item["node_id"])])
    )
    retired = tuple(
        str(item["node_id"])
        for item in previous_nodes
        if str(item["node_id"]) not in current_by_id
    )
    return inserted, changed, retired


def _validate_successors(
    values: Mapping[str, Sequence[str]],
    *,
    live_ids: set[str],
    retired_ids: set[str],
) -> None:
    for retired_id, successors in values.items():
        if retired_id not in retired_ids:
            raise NativeStructureWorkingRevisionError(
                "successor mapping must originate from a retired node"
            )
        if not successors or len(successors) != len(set(successors)):
            raise NativeStructureWorkingRevisionError(
                "successor mapping must contain unique live nodes"
            )
        if any(item not in live_ids for item in successors):
            raise NativeStructureWorkingRevisionError(
                "successor mapping references a missing live node"
            )


def _filter_tree(
    tree: Mapping[str, Any],
    included: set[str],
) -> dict[str, Any]:
    nodes = [
        copy.deepcopy(item)
        for item in tree["nodes"]
        if str(item.get("node_id") or "") in included
    ]
    return {
        "schema_version": "native-document-structure-tree/v1",
        "root_ids": [
            str(item) for item in tree["root_ids"] if str(item) in included
        ],
        "nodes": nodes,
    }


def _filter_support_projection(
    reference: Mapping[str, Any],
    *,
    candidate_tree: Mapping[str, Any],
) -> dict[str, Any]:
    live_ids = {str(item["node_id"]) for item in candidate_tree["nodes"]}
    nonroot_pages = {
        int(page)
        for item in candidate_tree["nodes"]
        if item.get("parent_id") is not None
        for page in item.get("pages", [])
    }
    all_pages = {
        int(page)
        for item in candidate_tree["nodes"]
        for page in item.get("pages", [])
    }
    table_refs = {
        str(block.get("logical_table_scope_id") or "")
        for item in candidate_tree["nodes"]
        for block in item.get("content_blocks", [])
        if isinstance(block, Mapping) and block.get("kind") == "LOGICAL_TABLE"
    }
    page_ownership = tuple(
        copy.deepcopy(dict(item))
        for item in reference.get("page_ownership", [])
        if all_pages & {int(page) for page in item.get("pages", [])}
    )
    logical_tables = tuple(
        copy.deepcopy(dict(item))
        for item in reference.get("logical_tables", [])
        if str(item.get("scope_id") or "") in table_refs
    )
    proposals = tuple(
        copy.deepcopy(dict(item))
        for item in reference.get("structure_proposals", [])
        if str(item.get("node_id") or "") in live_ids
        and (
            not str(item.get("recommended_target_node_id") or "")
            or str(item.get("recommended_target_node_id") or "") in live_ids
        )
    )
    unresolved = tuple(
        copy.deepcopy(dict(item))
        for item in reference.get("unresolved", [])
        if nonroot_pages & {int(page) for page in item.get("pages", [])}
    )
    raw_visual = reference.get("visual_summary")
    accepted_visual = raw_visual if isinstance(raw_visual, Mapping) else {}
    receipts = [
        copy.deepcopy(dict(item))
        for item in accepted_visual.get("receipts", [])
        if int(item.get("page") or 0) in all_pages
    ]
    visual_summary = {
        "opened_page_count": len(receipts),
        "page_count": int(accepted_visual.get("page_count") or 0),
        "receipts": receipts,
    }
    return {
        "page_ownership": page_ownership,
        "logical_tables": logical_tables,
        "structure_proposals": proposals,
        "unresolved": unresolved,
        "visual_summary": visual_summary,
    }


def _validate_visual_summary(value: Mapping[str, Any]) -> None:
    page_count = value.get("page_count")
    opened_page_count = value.get("opened_page_count")
    receipts = value.get("receipts")
    if (
        type(page_count) is not int
        or page_count < 1
        or type(opened_page_count) is not int
        or not 0 <= opened_page_count <= page_count
        or not isinstance(receipts, list)
        or len(receipts) != opened_page_count
    ):
        raise NativeStructureWorkingRevisionError(
            "working revision visual summary is invalid"
        )
    seen: set[int] = set()
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise NativeStructureWorkingRevisionError(
                "working revision visual receipt is invalid"
            )
        page = receipt.get("page")
        sha256 = str(receipt.get("sha256") or "")
        if (
            type(page) is not int
            or not 1 <= page <= page_count
            or page in seen
            or not _SHA256.fullmatch(sha256)
        ):
            raise NativeStructureWorkingRevisionError(
                "working revision visual receipt is invalid"
            )
        seen.add(page)


def _validate_unique_node_ids(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)) or any(
        not _SAFE_ID.fullmatch(item) for item in values
    ):
        raise ValueError(f"working revision {label} node IDs are invalid")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise NativeStructureWorkingRevisionError(
            "accepted public projection is invalid"
        )
    return {str(key): nested for key, nested in value.items()}


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


__all__ = [
    "NativeStructureWorkingRevisionError",
    "NativeStructureTablePreviewBindingV1",
    "NativeStructureWorkingRevisionHeadV1",
    "NativeStructureWorkingRevisionObjectV1",
    "NativeStructureWorkingRevisionV1",
    "WORKING_REVISION_HEAD_SCHEMA_VERSION",
    "WORKING_REVISION_MEDIA_TYPE",
    "WORKING_REVISION_OBJECT_SCHEMA_VERSION",
    "WORKING_REVISION_SCHEMA_VERSION",
    "TABLE_PREVIEW_MEDIA_TYPE",
    "build_accepted_reference_replay_revisions",
    "build_native_structure_working_revision_object",
    "canonical_json_sha256",
    "compile_native_structure_working_revision",
    "native_structure_working_revision_bytes",
    "native_structure_working_revision_object_key",
    "stable_native_structure_working_revision_id",
    "validate_native_structure_table_preview",
    "validate_native_structure_working_revision_lineage",
]
