from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.extraction.llm_requirement_review import LlmReviewSourceSpan
from app.domain.extraction.semantic_provider import SemanticProviderRunRecord
from app.domain.extraction.semantic_terminal_receipts import (
    SemanticUnitTerminalReceipt,
    SemanticUnitTerminalStatus,
)
from app.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionRole,
)
from app.domain.extraction.semantics import RequirementSubtype


REQUIREMENT_ENRICHMENT_LINEAGE_SCHEMA_VERSION = "requirement-enrichment-lineage/v2"
REQUIREMENT_ENRICHMENT_COVERAGE_SCHEMA_VERSION = "requirement-enrichment-coverage/v1"
REQUIREMENT_ATOMIZATION_RUN_SCHEMA_VERSION = "requirement-atomization-run/v3"
REQUIREMENT_REFINEMENT_SUGGESTION_SCHEMA_VERSION = (
    "requirement-refinement-suggestion/v1"
)
REQUIREMENT_PUBLICATION_PREVIEW_SCHEMA_VERSION = (
    "requirement-enrichment-publication-preview/v1"
)
REQUIREMENT_PUBLICATION_RESULT_SCHEMA_VERSION = (
    "requirement-enrichment-publication-result/v1"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_PATTERN = re.compile(r"^llm-review-[0-9a-f]{32}$")
_PREDICTION_ID_PATTERN = re.compile(r"^llm-prediction-[0-9a-f]{32}$")
_STRUCTURE_SNAPSHOT_ID_PATTERN = re.compile(
    r"^(?:structure|effective-structure)-[0-9a-f]{32}$"
)
_ATOMIZATION_RUN_ID_PATTERN = re.compile(r"^atomization-run-[0-9a-f]{32}$")
_ATOMIZATION_NODE_ID_PATTERN = re.compile(r"^atomization-node-[0-9a-f]{32}$")
_ATOMIC_NODE_ID_PATTERN = re.compile(r"^atomic-obligation-[0-9a-f]{32}$")
_TABLE_EVIDENCE_BINDING_ID_PATTERN = re.compile(
    r"^table-evidence-binding-[0-9a-f]{32}$"
)
_REFINEMENT_ID_PATTERN = re.compile(r"^refinement-suggestion-[0-9a-f]{32}$")
_PUBLICATION_PREVIEW_ID_PATTERN = re.compile(
    r"^requirement-publication-preview-[0-9a-f]{32}$"
)
_PUBLICATION_ENTRY_ID_PATTERN = re.compile(
    r"^requirement-publication-entry-[0-9a-f]{32}$"
)
_PROVIDER_RUN_ID_PATTERN = re.compile(r"^semantic-run-[0-9a-f]{32}$")
_MACHINE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RequirementAtomizationNodeKind(str, Enum):
    ROLLUP = "ROLLUP"
    ATOMIC_OBLIGATION = "ATOMIC_OBLIGATION"


class RequirementRefinementStatus(str, Enum):
    DRAFT = "DRAFT"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class RequirementRefinementOperation(str, Enum):
    REVISE = "REVISE"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class RequirementPublicationEntryKind(str, Enum):
    HEADING = "HEADING"
    REQUIREMENT = "REQUIREMENT"


class RequirementEnrichmentCoverageReceipt(_StrictFrozenModel):
    schema_version: Literal["requirement-enrichment-coverage/v1"] = (
        REQUIREMENT_ENRICHMENT_COVERAGE_SCHEMA_VERSION
    )
    total_units: int = Field(ge=1)
    blocked_unit_count: int = Field(default=0, ge=0)
    planning_runtime_version: str = Field(min_length=1, max_length=160)
    classifier_runtime_version: str = Field(min_length=1, max_length=160)
    assembler_runtime_version: str = Field(min_length=1, max_length=160)
    document_runtime_version: str = Field(min_length=1, max_length=160)
    terminal_receipts: tuple[SemanticUnitTerminalReceipt, ...]
    coverage_sha256: str

    @field_validator("coverage_sha256")
    @classmethod
    def validate_coverage_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("coverage receipt hash must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_coverage(self) -> RequirementEnrichmentCoverageReceipt:
        if len(self.terminal_receipts) + self.blocked_unit_count != self.total_units:
            raise ValueError(
                "coverage receipt denominator does not match terminal units"
            )
        unit_ids = tuple(item.semantic_unit_id for item in self.terminal_receipts)
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("coverage receipt semantic units must be unique")
        if any(
            item.planning_runtime_version != self.planning_runtime_version
            for item in self.terminal_receipts
        ):
            raise ValueError("coverage receipt planning runtime is inconsistent")
        expected = stable_requirement_enrichment_coverage_sha256(
            total_units=self.total_units,
            blocked_unit_count=self.blocked_unit_count,
            planning_runtime_version=self.planning_runtime_version,
            classifier_runtime_version=self.classifier_runtime_version,
            assembler_runtime_version=self.assembler_runtime_version,
            document_runtime_version=self.document_runtime_version,
            terminal_receipts=self.terminal_receipts,
        )
        if self.coverage_sha256 != expected:
            raise ValueError("coverage_sha256 does not match immutable coverage")
        return self

    @property
    def unresolved_unit_count(self) -> int:
        return sum(
            item.status is SemanticUnitTerminalStatus.UNRESOLVED
            for item in self.terminal_receipts
        )

    @property
    def eligible(self) -> bool:
        return bool(
            self.blocked_unit_count == 0
            and self.unresolved_unit_count == 0
            and len(self.terminal_receipts) == self.total_units
        )


class RequirementEnrichmentLineage(_StrictFrozenModel):
    schema_version: Literal["requirement-enrichment-lineage/v2"] = (
        REQUIREMENT_ENRICHMENT_LINEAGE_SCHEMA_VERSION
    )
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    session_id: str
    prediction_id: str
    review_revision: int = Field(ge=0)
    source_sha256: str
    structure_snapshot_id: str
    reviewed_result_sha256: str
    coverage_sha256: str
    coverage_total_units: int = Field(ge=1)
    semantic_planning_runtime_version: str = Field(min_length=1, max_length=160)
    semantic_classifier_runtime_version: str = Field(min_length=1, max_length=160)
    semantic_assembler_runtime_version: str = Field(min_length=1, max_length=160)
    semantic_document_runtime_version: str = Field(min_length=1, max_length=160)
    lineage_sha256: str

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not _SESSION_ID_PATTERN.fullmatch(value):
            raise ValueError("enrichment lineage session_id is invalid")
        return value

    @field_validator("prediction_id")
    @classmethod
    def validate_prediction_id(cls, value: str) -> str:
        if not _PREDICTION_ID_PATTERN.fullmatch(value):
            raise ValueError("enrichment lineage prediction_id is invalid")
        return value

    @field_validator("structure_snapshot_id")
    @classmethod
    def validate_structure_snapshot_id(cls, value: str) -> str:
        if not _STRUCTURE_SNAPSHOT_ID_PATTERN.fullmatch(value):
            raise ValueError("enrichment lineage Structure snapshot is invalid")
        return value

    @field_validator(
        "source_sha256",
        "reviewed_result_sha256",
        "coverage_sha256",
        "lineage_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("enrichment lineage hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_lineage_hash(self) -> RequirementEnrichmentLineage:
        expected = stable_requirement_enrichment_lineage_sha256(
            project_id=self.project_id,
            document_id=self.document_id,
            session_id=self.session_id,
            prediction_id=self.prediction_id,
            review_revision=self.review_revision,
            source_sha256=self.source_sha256,
            structure_snapshot_id=self.structure_snapshot_id,
            reviewed_result_sha256=self.reviewed_result_sha256,
            coverage_sha256=self.coverage_sha256,
            coverage_total_units=self.coverage_total_units,
            semantic_planning_runtime_version=(self.semantic_planning_runtime_version),
            semantic_classifier_runtime_version=(
                self.semantic_classifier_runtime_version
            ),
            semantic_assembler_runtime_version=(
                self.semantic_assembler_runtime_version
            ),
            semantic_document_runtime_version=self.semantic_document_runtime_version,
        )
        if self.lineage_sha256 != expected:
            raise ValueError("enrichment lineage_sha256 does not match its binding")
        return self


class RequirementAtomizationNode(_StrictFrozenModel):
    node_id: str
    atomic_node_id: str | None = None
    kind: RequirementAtomizationNodeKind
    parent_node_id: str | None = None
    review_node_id: str = Field(min_length=1, max_length=200)
    source_semantic_unit_ids: tuple[str, ...]
    source_owner_structure_node_ids: tuple[str, ...]
    source_spans: tuple[LlmReviewSourceSpan, ...]
    source_order: int = Field(ge=0)
    role: RequirementProjectionRole
    decision_mode: RequirementDecisionMode
    requirement_subtype: RequirementSubtype
    source_text: str = Field(min_length=1, max_length=500_000)
    atomic_statement: str | None = Field(default=None, max_length=500_000)
    table_evidence_binding_id: str | None = Field(default=None, max_length=260)

    @field_validator("node_id", "parent_node_id")
    @classmethod
    def validate_node_id(cls, value: str | None) -> str | None:
        if value is not None and not _ATOMIZATION_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("atomization node identity is invalid")
        return value

    @field_validator("atomic_node_id")
    @classmethod
    def validate_atomic_node_id(cls, value: str | None) -> str | None:
        if value is not None and not _ATOMIC_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("atomic obligation identity is invalid")
        return value

    @field_validator("table_evidence_binding_id")
    @classmethod
    def validate_table_evidence_binding_id(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not _TABLE_EVIDENCE_BINDING_ID_PATTERN.fullmatch(
            value
        ):
            raise ValueError("atomization table evidence binding is invalid")
        return value

    @field_validator("atomic_statement")
    @classmethod
    def validate_atomic_statement(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("atomic statements must not be blank")
        return value

    @model_validator(mode="after")
    def validate_atomization_node(self) -> RequirementAtomizationNode:
        if not self.source_semantic_unit_ids or not self.source_spans:
            raise ValueError("atomization nodes require exact source grounding")
        if len(self.source_semantic_unit_ids) != len(
            self.source_owner_structure_node_ids
        ):
            raise ValueError("atomization source units must retain Structure owners")
        if self.kind is RequirementAtomizationNodeKind.ROLLUP:
            if self.atomic_node_id is not None:
                raise ValueError("ROLLUP nodes are not atomic obligations")
            if self.atomic_statement is not None:
                raise ValueError("ROLLUP nodes cannot carry atomic statements")
            if self.table_evidence_binding_id is not None:
                raise ValueError("ROLLUP nodes cannot claim table record evidence")
            if self.decision_mode is not RequirementDecisionMode.ROLLUP:
                raise ValueError("ROLLUP nodes must retain ROLLUP decision mode")
        else:
            if self.atomic_node_id is None:
                raise ValueError("atomic obligations require atomic_node_id")
            if self.atomic_statement is None:
                raise ValueError("atomic obligations require atomic_statement")
            if self.decision_mode is not RequirementDecisionMode.DIRECT:
                raise ValueError("atomic obligations must retain DIRECT decision mode")
        return self


class RequirementAtomizationRun(_StrictFrozenModel):
    schema_version: Literal["requirement-atomization-run/v3"] = (
        REQUIREMENT_ATOMIZATION_RUN_SCHEMA_VERSION
    )
    atomization_run_id: str
    revision: int = Field(default=0, ge=0)
    algorithm_version: str = Field(min_length=1, max_length=160)
    lineage: RequirementEnrichmentLineage
    nodes: tuple[RequirementAtomizationNode, ...]
    provider_runs: tuple[SemanticProviderRunRecord, ...]
    created_at_utc: datetime

    @field_validator("atomization_run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not _ATOMIZATION_RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("atomization_run_id is invalid")
        return value

    @model_validator(mode="after")
    def validate_run(self) -> RequirementAtomizationRun:
        if self.created_at_utc.tzinfo is None:
            raise ValueError("atomization timestamp must be timezone-aware")
        node_ids = tuple(item.node_id for item in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("atomization node IDs must be unique")
        atomic_ids = tuple(
            item.atomic_node_id
            for item in self.nodes
            if item.atomic_node_id is not None
        )
        if len(atomic_ids) != len(set(atomic_ids)):
            raise ValueError("atomic obligation IDs must be unique")
        atomic_statements_by_review: dict[str, list[str]] = {}
        for item in self.atomic_nodes:
            assert item.atomic_statement is not None
            atomic_statements_by_review.setdefault(item.review_node_id, []).append(
                " ".join(item.atomic_statement.split()).casefold()
            )
        if any(
            len(statements) > 1 and len(statements) != len(set(statements))
            for statements in atomic_statements_by_review.values()
        ):
            raise ValueError(
                "multi-atom review nodes require independent atomic statements"
            )
        table_bindings_by_review: dict[str, set[str]] = {}
        for item in self.atomic_nodes:
            if item.table_evidence_binding_id is not None:
                table_bindings_by_review.setdefault(item.review_node_id, set()).add(
                    item.table_evidence_binding_id
                )
        if any(
            len(binding_ids) > 1 for binding_ids in table_bindings_by_review.values()
        ):
            raise ValueError(
                "atoms from one review node cannot cross table record evidence"
            )
        if not self.provider_runs or len(
            {item.provider_run_id for item in self.provider_runs}
        ) != len(self.provider_runs):
            raise ValueError("atomization provider runs must be non-empty and unique")
        if tuple(item.source_order for item in self.nodes) != tuple(
            sorted(item.source_order for item in self.nodes)
        ):
            raise ValueError("atomization nodes must retain source order")
        seen: set[str] = set()
        for node in self.nodes:
            if node.parent_node_id is not None and node.parent_node_id not in seen:
                raise ValueError("atomization parents must precede their children")
            expected_node_id = stable_atomization_node_id(
                lineage_sha256=self.lineage.lineage_sha256,
                review_node_id=node.review_node_id,
                source_spans=node.source_spans,
                decision_mode=node.decision_mode,
                table_evidence_binding_id=node.table_evidence_binding_id,
            )
            if node.node_id != expected_node_id:
                raise ValueError(
                    "atomization node identity does not match its evidence binding"
                )
            seen.add(node.node_id)
        expected = stable_atomization_run_id(
            lineage_sha256=self.lineage.lineage_sha256,
            revision=self.revision,
            algorithm_version=self.algorithm_version,
            nodes=self.nodes,
            provider_runs=self.provider_runs,
        )
        if self.atomization_run_id != expected:
            raise ValueError("atomization_run_id does not match immutable output")
        return self

    @property
    def atomic_nodes(self) -> tuple[RequirementAtomizationNode, ...]:
        return tuple(
            item
            for item in self.nodes
            if item.kind is RequirementAtomizationNodeKind.ATOMIC_OBLIGATION
        )


class RequirementRefinementSuggestion(_StrictFrozenModel):
    schema_version: Literal["requirement-refinement-suggestion/v1"] = (
        REQUIREMENT_REFINEMENT_SUGGESTION_SCHEMA_VERSION
    )
    suggestion_id: str
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    session_id: str
    atomization_run_id: str
    atomization_revision: int = Field(ge=0)
    atomic_node_id: str
    revision: int = Field(ge=0)
    status: RequirementRefinementStatus
    original_statement: str = Field(min_length=1, max_length=500_000)
    suggested_statement: str = Field(min_length=1, max_length=500_000)
    rationale: str = Field(default="", max_length=20_000)
    decision_note: str = Field(default="", max_length=20_000)
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=160)
    prompt_version: str = Field(min_length=1, max_length=160)
    provider_run_id: str
    reason_codes: tuple[str, ...] = ()
    lineage_sha256: str
    created_at_utc: datetime
    updated_at_utc: datetime

    @field_validator("suggestion_id")
    @classmethod
    def validate_suggestion_id(cls, value: str) -> str:
        if not _REFINEMENT_ID_PATTERN.fullmatch(value):
            raise ValueError("refinement suggestion identity is invalid")
        return value

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not _SESSION_ID_PATTERN.fullmatch(value):
            raise ValueError("refinement session_id is invalid")
        return value

    @field_validator("atomization_run_id")
    @classmethod
    def validate_atomization_run_id(cls, value: str) -> str:
        if not _ATOMIZATION_RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("refinement atomization_run_id is invalid")
        return value

    @field_validator("atomic_node_id")
    @classmethod
    def validate_atomic_node_id(cls, value: str) -> str:
        if not _ATOMIC_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("refinement atomic_node_id is invalid")
        return value

    @field_validator("provider_run_id")
    @classmethod
    def validate_provider_run_id(cls, value: str) -> str:
        if not _PROVIDER_RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("refinement provider_run_id is invalid")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _MACHINE_KEY_PATTERN.fullmatch(item) for item in value):
            raise ValueError("refinement reason codes must be machine-readable")
        if value != tuple(sorted(set(value))):
            raise ValueError("refinement reason codes must be unique and sorted")
        return value

    @field_validator("lineage_sha256")
    @classmethod
    def validate_lineage_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("refinement lineage hash is invalid")
        return value

    @model_validator(mode="after")
    def validate_suggestion(self) -> RequirementRefinementSuggestion:
        if self.created_at_utc.tzinfo is None or self.updated_at_utc.tzinfo is None:
            raise ValueError("refinement timestamps must be timezone-aware")
        if self.updated_at_utc < self.created_at_utc:
            raise ValueError("refinement update cannot precede creation")
        if self.suggested_statement.strip() == self.original_statement.strip():
            raise ValueError(
                "refinement suggestions must be independent from source wording"
            )
        expected_lineage = stable_refinement_lineage_sha256(
            project_id=self.project_id,
            document_id=self.document_id,
            session_id=self.session_id,
            atomization_run_id=self.atomization_run_id,
            atomization_revision=self.atomization_revision,
            atomic_node_id=self.atomic_node_id,
        )
        if self.lineage_sha256 != expected_lineage:
            raise ValueError("refinement lineage_sha256 does not match its binding")
        expected_id = stable_refinement_suggestion_id(
            lineage_sha256=self.lineage_sha256
        )
        if self.suggestion_id != expected_id:
            raise ValueError("refinement suggestion identity does not match lineage")
        return self


class RequirementRefinementBinding(_StrictFrozenModel):
    suggestion_id: str
    atomic_node_id: str
    revision: int = Field(ge=0)
    status: RequirementRefinementStatus
    lineage_sha256: str

    @field_validator("suggestion_id")
    @classmethod
    def validate_suggestion_id(cls, value: str) -> str:
        if not _REFINEMENT_ID_PATTERN.fullmatch(value):
            raise ValueError("publication refinement suggestion ID is invalid")
        return value

    @field_validator("atomic_node_id")
    @classmethod
    def validate_atomic_node_id(cls, value: str) -> str:
        if not _ATOMIC_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("publication atomic node ID is invalid")
        return value

    @field_validator("lineage_sha256")
    @classmethod
    def validate_lineage_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("publication refinement lineage hash is invalid")
        return value


class RequirementPublicationEntry(_StrictFrozenModel):
    publication_entry_id: str
    atomization_node_id: str
    atomic_node_id: str | None = None
    parent_publication_entry_id: str | None = None
    review_node_id: str = Field(min_length=1, max_length=200)
    entry_kind: RequirementPublicationEntryKind
    req_code: str = Field(min_length=1, max_length=120)
    requirement_type: str = Field(min_length=1, max_length=120)
    decision_mode: RequirementDecisionMode
    original_statement: str = Field(min_length=1, max_length=500_000)
    published_statement: str = Field(min_length=1, max_length=500_000)
    source_spans: tuple[LlmReviewSourceSpan, ...]
    source_order: int = Field(ge=0)
    table_evidence_binding_id: str | None = Field(default=None, max_length=260)
    refinement_suggestion_id: str | None = None
    refinement_revision: int | None = Field(default=None, ge=0)

    @field_validator("publication_entry_id", "parent_publication_entry_id")
    @classmethod
    def validate_publication_entry_id(cls, value: str | None) -> str | None:
        if value is not None and not _PUBLICATION_ENTRY_ID_PATTERN.fullmatch(value):
            raise ValueError("publication entry identity is invalid")
        return value

    @field_validator("atomization_node_id")
    @classmethod
    def validate_atomization_node_id(cls, value: str) -> str:
        if not _ATOMIZATION_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("publication atomization node identity is invalid")
        return value

    @field_validator("atomic_node_id")
    @classmethod
    def validate_atomic_node_id(cls, value: str | None) -> str | None:
        if value is not None and not _ATOMIC_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("publication atomic node identity is invalid")
        return value

    @field_validator("table_evidence_binding_id")
    @classmethod
    def validate_table_evidence_binding_id(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None and not _TABLE_EVIDENCE_BINDING_ID_PATTERN.fullmatch(
            value
        ):
            raise ValueError("publication table evidence binding is invalid")
        return value

    @field_validator("refinement_suggestion_id")
    @classmethod
    def validate_refinement_id(cls, value: str | None) -> str | None:
        if value is not None and not _REFINEMENT_ID_PATTERN.fullmatch(value):
            raise ValueError("publication refinement identity is invalid")
        return value

    @model_validator(mode="after")
    def validate_entry(self) -> RequirementPublicationEntry:
        if not self.source_spans:
            raise ValueError("publication entries require exact source spans")
        if self.entry_kind is RequirementPublicationEntryKind.HEADING:
            if self.atomic_node_id is not None:
                raise ValueError("publication headings cannot be atomic obligations")
            if self.table_evidence_binding_id is not None:
                raise ValueError(
                    "publication headings cannot claim table record evidence"
                )
            if self.decision_mode is not RequirementDecisionMode.ROLLUP:
                raise ValueError("publication headings must retain ROLLUP mode")
        else:
            if self.atomic_node_id is None:
                raise ValueError("published requirements require atomic_node_id")
            if self.decision_mode is not RequirementDecisionMode.DIRECT:
                raise ValueError("published requirements must retain DIRECT mode")
        if (self.refinement_suggestion_id is None) != (
            self.refinement_revision is None
        ):
            raise ValueError("published refinement identity requires its revision")
        return self


class RequirementPublicationPreview(_StrictFrozenModel):
    schema_version: Literal["requirement-enrichment-publication-preview/v1"] = (
        REQUIREMENT_PUBLICATION_PREVIEW_SCHEMA_VERSION
    )
    preview_id: str
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    session_id: str
    atomization_run_id: str
    atomization_revision: int = Field(ge=0)
    base_active_requirement_set_id: str = Field(default="", max_length=160)
    lineage_sha256: str
    publication_key: str
    refinement_bindings: tuple[RequirementRefinementBinding, ...]
    entries: tuple[RequirementPublicationEntry, ...]
    created_at_utc: datetime

    @field_validator("preview_id")
    @classmethod
    def validate_preview_id(cls, value: str) -> str:
        if not _PUBLICATION_PREVIEW_ID_PATTERN.fullmatch(value):
            raise ValueError("publication preview identity is invalid")
        return value

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not _SESSION_ID_PATTERN.fullmatch(value):
            raise ValueError("publication session_id is invalid")
        return value

    @field_validator("atomization_run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not _ATOMIZATION_RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("publication atomization run identity is invalid")
        return value

    @field_validator("lineage_sha256", "publication_key")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("publication hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_preview(self) -> RequirementPublicationPreview:
        if self.created_at_utc.tzinfo is None:
            raise ValueError("publication preview timestamp must be timezone-aware")
        if not self.entries:
            raise ValueError("publication preview requires at least one entry")
        binding_ids = tuple(item.suggestion_id for item in self.refinement_bindings)
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("publication refinement bindings must be unique")
        entry_ids = tuple(item.publication_entry_id for item in self.entries)
        if len(entry_ids) != len(set(entry_ids)):
            raise ValueError("publication entry IDs must be unique")
        table_bindings_by_review: dict[str, set[str]] = {}
        for item in self.entries:
            if item.table_evidence_binding_id is not None:
                table_bindings_by_review.setdefault(item.review_node_id, set()).add(
                    item.table_evidence_binding_id
                )
        if any(
            len(binding_ids) > 1 for binding_ids in table_bindings_by_review.values()
        ):
            raise ValueError(
                "published atoms from one review node cannot cross table evidence"
            )
        expected_lineage = stable_publication_lineage_sha256(
            project_id=self.project_id,
            document_id=self.document_id,
            session_id=self.session_id,
            atomization_run_id=self.atomization_run_id,
            atomization_revision=self.atomization_revision,
            base_active_requirement_set_id=self.base_active_requirement_set_id,
            refinement_bindings=self.refinement_bindings,
        )
        if self.lineage_sha256 != expected_lineage:
            raise ValueError("publication lineage_sha256 does not match its binding")
        expected_key = stable_publication_key(
            lineage_sha256=self.lineage_sha256,
            entries=self.entries,
        )
        if self.publication_key != expected_key:
            raise ValueError("publication_key does not match preview contents")
        expected_id = stable_publication_preview_id(
            publication_key=self.publication_key
        )
        if self.preview_id != expected_id:
            raise ValueError("publication preview ID does not match publication key")
        return self


class RequirementPublicationResult(_StrictFrozenModel):
    schema_version: Literal["requirement-enrichment-publication-result/v1"] = (
        REQUIREMENT_PUBLICATION_RESULT_SCHEMA_VERSION
    )
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    preview_id: str
    publication_key: str
    lineage_sha256: str
    requirement_set_id: str = Field(min_length=1, max_length=160)
    requirement_set_version: int = Field(ge=1)
    requirement_set_status: Literal["DRAFT"] = "DRAFT"
    is_active: Literal[False] = False
    active_requirement_set_id: str = Field(default="", max_length=160)
    created: bool
    created_at_utc: datetime

    @field_validator("preview_id")
    @classmethod
    def validate_preview_id(cls, value: str) -> str:
        if not _PUBLICATION_PREVIEW_ID_PATTERN.fullmatch(value):
            raise ValueError("publication result preview ID is invalid")
        return value

    @field_validator("publication_key", "lineage_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("publication result hashes are invalid")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> RequirementPublicationResult:
        if self.created_at_utc.tzinfo is None:
            raise ValueError("publication result timestamp must be timezone-aware")
        return self


def stable_requirement_enrichment_lineage_sha256(
    *,
    project_id: str,
    document_id: str,
    session_id: str,
    prediction_id: str,
    review_revision: int,
    source_sha256: str,
    structure_snapshot_id: str,
    reviewed_result_sha256: str,
    coverage_sha256: str,
    coverage_total_units: int,
    semantic_planning_runtime_version: str,
    semantic_classifier_runtime_version: str,
    semantic_assembler_runtime_version: str,
    semantic_document_runtime_version: str,
) -> str:
    return _sha256(
        {
            "document_id": document_id,
            "prediction_id": prediction_id,
            "project_id": project_id,
            "review_revision": review_revision,
            "reviewed_result_sha256": reviewed_result_sha256,
            "coverage_sha256": coverage_sha256,
            "coverage_total_units": coverage_total_units,
            "semantic_planning_runtime_version": semantic_planning_runtime_version,
            "semantic_classifier_runtime_version": semantic_classifier_runtime_version,
            "semantic_assembler_runtime_version": semantic_assembler_runtime_version,
            "semantic_document_runtime_version": semantic_document_runtime_version,
            "session_id": session_id,
            "source_sha256": source_sha256,
            "structure_snapshot_id": structure_snapshot_id,
        }
    )


def stable_requirement_enrichment_coverage_sha256(
    *,
    total_units: int,
    blocked_unit_count: int,
    planning_runtime_version: str,
    classifier_runtime_version: str,
    assembler_runtime_version: str,
    document_runtime_version: str,
    terminal_receipts: tuple[SemanticUnitTerminalReceipt, ...],
) -> str:
    return _sha256(
        {
            "assembler_runtime_version": assembler_runtime_version,
            "blocked_unit_count": blocked_unit_count,
            "classifier_runtime_version": classifier_runtime_version,
            "document_runtime_version": document_runtime_version,
            "planning_runtime_version": planning_runtime_version,
            "terminal_receipts": [
                item.model_dump(mode="json") for item in terminal_receipts
            ],
            "total_units": total_units,
        }
    )


def stable_atomization_node_id(
    *,
    lineage_sha256: str,
    review_node_id: str,
    source_spans: tuple[LlmReviewSourceSpan, ...],
    decision_mode: RequirementDecisionMode,
    table_evidence_binding_id: str | None = None,
) -> str:
    identity = {
        "decision_mode": decision_mode.value,
        "lineage_sha256": lineage_sha256,
        "review_node_id": review_node_id,
        "source_spans": [item.model_dump(mode="json") for item in source_spans],
    }
    if table_evidence_binding_id is not None:
        identity["table_evidence_binding_id"] = table_evidence_binding_id
    digest = _sha256(identity)
    return f"atomization-node-{digest[:32]}"


def stable_atomic_node_id(*, atomization_node_id: str) -> str:
    digest = _sha256({"atomization_node_id": atomization_node_id})
    return f"atomic-obligation-{digest[:32]}"


def stable_atomization_run_id(
    *,
    lineage_sha256: str,
    revision: int,
    algorithm_version: str,
    nodes: tuple[RequirementAtomizationNode, ...],
    provider_runs: tuple[SemanticProviderRunRecord, ...],
) -> str:
    serialized_nodes = []
    for item in nodes:
        serialized = item.model_dump(mode="json")
        if serialized.get("table_evidence_binding_id") is None:
            serialized.pop("table_evidence_binding_id", None)
        serialized_nodes.append(serialized)
    digest = _sha256(
        {
            "algorithm_version": algorithm_version,
            "lineage_sha256": lineage_sha256,
            "nodes": serialized_nodes,
            "provider_runs": [item.model_dump(mode="json") for item in provider_runs],
            "revision": revision,
        }
    )
    return f"atomization-run-{digest[:32]}"


def stable_refinement_lineage_sha256(
    *,
    project_id: str,
    document_id: str,
    session_id: str,
    atomization_run_id: str,
    atomization_revision: int,
    atomic_node_id: str,
) -> str:
    return _sha256(
        {
            "atomic_node_id": atomic_node_id,
            "atomization_revision": atomization_revision,
            "atomization_run_id": atomization_run_id,
            "document_id": document_id,
            "project_id": project_id,
            "session_id": session_id,
        }
    )


def stable_refinement_suggestion_id(*, lineage_sha256: str) -> str:
    digest = _sha256({"lineage_sha256": lineage_sha256})
    return f"refinement-suggestion-{digest[:32]}"


def stable_publication_entry_id(
    *,
    publication_lineage_sha256: str,
    atomization_node_id: str,
) -> str:
    digest = _sha256(
        {
            "atomization_node_id": atomization_node_id,
            "publication_lineage_sha256": publication_lineage_sha256,
        }
    )
    return f"requirement-publication-entry-{digest[:32]}"


def stable_publication_lineage_sha256(
    *,
    project_id: str,
    document_id: str,
    session_id: str,
    atomization_run_id: str,
    atomization_revision: int,
    base_active_requirement_set_id: str,
    refinement_bindings: tuple[RequirementRefinementBinding, ...],
) -> str:
    return _sha256(
        {
            "atomization_revision": atomization_revision,
            "atomization_run_id": atomization_run_id,
            "base_active_requirement_set_id": base_active_requirement_set_id,
            "document_id": document_id,
            "project_id": project_id,
            "refinement_bindings": [
                item.model_dump(mode="json") for item in refinement_bindings
            ],
            "session_id": session_id,
        }
    )


def stable_publication_key(
    *,
    lineage_sha256: str,
    entries: tuple[RequirementPublicationEntry, ...],
) -> str:
    serialized_entries = []
    for item in entries:
        serialized = item.model_dump(mode="json")
        if serialized.get("table_evidence_binding_id") is None:
            serialized.pop("table_evidence_binding_id", None)
        serialized_entries.append(serialized)
    return _sha256(
        {
            "entries": serialized_entries,
            "lineage_sha256": lineage_sha256,
        }
    )


def stable_publication_preview_id(*, publication_key: str) -> str:
    digest = _sha256({"publication_key": publication_key})
    return f"requirement-publication-preview-{digest[:32]}"


def canonical_model_sha256(model: BaseModel) -> str:
    return _sha256(model.model_dump(mode="json"))


def _sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "REQUIREMENT_ATOMIZATION_RUN_SCHEMA_VERSION",
    "REQUIREMENT_ENRICHMENT_LINEAGE_SCHEMA_VERSION",
    "REQUIREMENT_ENRICHMENT_COVERAGE_SCHEMA_VERSION",
    "REQUIREMENT_PUBLICATION_PREVIEW_SCHEMA_VERSION",
    "REQUIREMENT_PUBLICATION_RESULT_SCHEMA_VERSION",
    "REQUIREMENT_REFINEMENT_SUGGESTION_SCHEMA_VERSION",
    "RequirementAtomizationNode",
    "RequirementAtomizationNodeKind",
    "RequirementAtomizationRun",
    "RequirementEnrichmentLineage",
    "RequirementEnrichmentCoverageReceipt",
    "RequirementPublicationEntry",
    "RequirementPublicationEntryKind",
    "RequirementPublicationPreview",
    "RequirementPublicationResult",
    "RequirementRefinementBinding",
    "RequirementRefinementOperation",
    "RequirementRefinementStatus",
    "RequirementRefinementSuggestion",
    "canonical_model_sha256",
    "stable_atomic_node_id",
    "stable_atomization_node_id",
    "stable_atomization_run_id",
    "stable_publication_entry_id",
    "stable_publication_key",
    "stable_publication_lineage_sha256",
    "stable_publication_preview_id",
    "stable_refinement_lineage_sha256",
    "stable_refinement_suggestion_id",
    "stable_requirement_enrichment_lineage_sha256",
    "stable_requirement_enrichment_coverage_sha256",
]
