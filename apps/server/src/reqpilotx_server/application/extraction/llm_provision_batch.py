from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import re
from threading import BoundedSemaphore

from reqpilotx_server.application.extraction.classify_content import (
    SemanticClassificationRejected,
    validate_semantic_classification_against_inputs,
)
from reqpilotx_server.application.extraction.document_parent_boundaries import (
    DocumentParentBoundaryProviderPort,
    DocumentParentBoundaryProviderResult,
    build_document_parent_boundary_plans,
    build_parent_boundary_target_windows,
)
from reqpilotx_server.application.extraction.assemble_requirements import (
    RequirementAssemblyRejected,
    validate_requirement_assembly_against_inputs,
)
from reqpilotx_server.application.extraction.llm_semantics import (
    DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST,
    LlmRequirementExtractionResult,
    extract_requirements_with_provider,
    plan_content_classification_provider_requests,
    plan_requirement_assembly_provider_requests,
)
from reqpilotx_server.application.extraction.locate_llm_scope import (
    LLM_DOCUMENT_ROOT_SCOPE_LOCATOR_VERSION,
    LlmExtractionScopeLocator,
    LocatedLlmExtractionScope,
    locate_llm_extraction_scope,
)
from reqpilotx_server.application.extraction.project_requirements import (
    RequirementProjectionRejected,
    validate_assembled_requirement_view_against_inputs,
)
from reqpilotx_server.application.extraction.resolve_structure import (
    validate_structure_tree_against_source_ir,
)
from reqpilotx_server.application.extraction.structure_snapshot import (
    StructureSnapshot,
    require_structure_snapshot,
    structure_snapshot_id,
    validate_structure_snapshot_against_source_ir,
)
from reqpilotx_server.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from reqpilotx_server.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
)
from reqpilotx_server.domain.extraction.semantics import (
    SemanticAnnotationStatus,
    SemanticCandidateSource,
    SemanticClass,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot, SourceNodeKind
from reqpilotx_server.domain.extraction.structure import (
    StructureNode,
    StructureNodeKind,
    StructureRelationKind,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)
from reqpilotx_server.ports.semantic_provider import (
    DocumentSessionSemanticProviderPort,
    PreflightSemanticProviderPort,
    SemanticImageInput,
    SemanticProviderPort,
    SemanticProviderRequestRejected,
    SemanticProviderRequestRejectionCode,
)
from reqpilotx_server.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderPort,
)


_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_BLOCKED_INPUT_ERROR_CODES = {
    "provision_input_invalid",
    *(item.value for item in SemanticProviderRequestRejectionCode),
}
MAX_LLM_PROVISIONS_PER_BATCH = 12
# Real qwen3.7-plus PDF runs showed that 64-target requests could complete
# transport while still producing schema-invalid output. Sixteen targets kept
# both semantic classification and requirement assembly within a reliable
# structured-output envelope. Larger provisions are now planned only across
# existing source-bound semantic units while indivisible continuation groups
# remain fail-closed.
MAX_LLM_DOCUMENT_CHUNK_NODES = 16
MAX_LLM_DOCUMENT_CHUNK_IMAGE_PAGES = 12
MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS = 4
LLM_DOCUMENT_PROGRESS_HEARTBEAT_SECONDS = 15.0
LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION = "llm-requirement-document-runtime/v4"
_LLM_DOCUMENT_REMOTE_CHUNK_SEMAPHORE = BoundedSemaphore(
    MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS
)


@contextmanager
def llm_document_remote_chunk_lease():
    """Share one process-wide remote-call ceiling across semantic and audit work."""

    with _LLM_DOCUMENT_REMOTE_CHUNK_SEMAPHORE:
        yield


class LlmProvisionExtractionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    UNRESOLVED = "UNRESOLVED"
    BLOCKED_INPUT = "BLOCKED_INPUT"


class LlmRequirementBatchStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"


class LlmProvisionBatchRejectionCode(str, Enum):
    INPUT_BINDING_INVALID = "llm_provision_batch_input_binding_invalid"
    LOCATED_SCOPE_INVALID = "llm_provision_batch_located_scope_invalid"
    INCOMPLETE_PROVISION = "llm_provision_batch_incomplete_provision"
    OVERLAPPING_PROVISIONS = "llm_provision_batch_overlapping_provisions"
    CROSS_PROVISION_CONTINUATION = "llm_provision_batch_cross_provision_continuation"
    CROSS_PROVISION_PARENT = "llm_provision_batch_cross_provision_parent"
    DUPLICATE_VIEW_TARGET = "llm_provision_batch_duplicate_view_target"
    DUPLICATE_VIEW_NODE = "llm_provision_batch_duplicate_view_node"
    DUPLICATE_PROVIDER_RUN = "llm_provision_batch_duplicate_provider_run"
    PROVIDER_IDENTITY_MUTATED = "llm_provision_batch_provider_identity_mutated"
    PROVISION_BUDGET_EXCEEDED = "llm_provision_batch_provision_budget_exceeded"


class LlmProvisionBatchRejected(RuntimeError):
    """Fail-closed batch-boundary rejection without source text."""

    def __init__(self, code: LlmProvisionBatchRejectionCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class LlmRequirementViewNodeRef:
    """Text-free reference to one node in a provision-owned View snapshot."""

    root_section_structure_node_id: str
    requirement_view_snapshot_id: str
    requirement_view_node_id: str
    semantic_unit_id: str
    owner_structure_node_id: str
    source_order: int

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.root_section_structure_node_id,
                self.requirement_view_snapshot_id,
                self.requirement_view_node_id,
                self.semantic_unit_id,
                self.owner_structure_node_id,
            )
        ):
            raise ValueError("requirement view references require non-blank identities")
        if (
            isinstance(self.source_order, bool)
            or not isinstance(self.source_order, int)
            or self.source_order < 0
        ):
            raise ValueError("requirement view references require a source order")

    @property
    def target_structure_node_id(self) -> str:
        return self.owner_structure_node_id


@dataclass(frozen=True)
class LlmProvisionExtractionOutcome:
    """One independently executable provision and its immutable child result."""

    root_section_structure_node_id: str
    scope_structure_node_ids: tuple[str, ...]
    source_order: int
    status: LlmProvisionExtractionStatus
    extraction: LlmRequirementExtractionResult | None
    error_code: str | None
    completed_provider_runs: tuple[SemanticProviderRunRecord, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.root_section_structure_node_id, str)
            or not self.root_section_structure_node_id.strip()
            or not isinstance(self.scope_structure_node_ids, tuple)
            or not self.scope_structure_node_ids
            or self.scope_structure_node_ids[0] != self.root_section_structure_node_id
            or len(self.scope_structure_node_ids)
            != len(set(self.scope_structure_node_ids))
        ):
            raise ValueError("provision outcome has an invalid scope")
        if (
            isinstance(self.source_order, bool)
            or not isinstance(self.source_order, int)
            or self.source_order < 0
        ):
            raise ValueError("provision outcome has an invalid source order")
        if not isinstance(self.status, LlmProvisionExtractionStatus):
            raise TypeError("provision outcome status is invalid")
        if self.error_code is not None and not _ERROR_CODE_PATTERN.fullmatch(
            self.error_code
        ):
            raise ValueError("provision outcome error code is invalid")
        if not isinstance(self.completed_provider_runs, tuple) or any(
            not isinstance(run, SemanticProviderRunRecord)
            for run in self.completed_provider_runs
        ):
            raise TypeError("provision outcome provider run receipts are invalid")
        if len({run.provider_run_id for run in self.completed_provider_runs}) != len(
            self.completed_provider_runs
        ):
            raise ValueError("provision outcome provider run receipts are duplicated")
        if self.status is LlmProvisionExtractionStatus.BLOCKED_INPUT:
            if (
                self.extraction is not None
                or self.error_code not in _BLOCKED_INPUT_ERROR_CODES
                or any(
                    run.operation not in {"classify_nodes", "assemble_requirements"}
                    for run in self.completed_provider_runs
                )
            ):
                raise ValueError("blocked provision outcomes cannot contain extraction")
            return
        if self.completed_provider_runs:
            raise ValueError(
                "extracted provision outcomes cannot duplicate run receipts"
            )
        if not isinstance(self.extraction, LlmRequirementExtractionResult):
            raise TypeError("resolved provision outcomes require an extraction")
        if (
            self.extraction.semantics.scope_structure_node_ids
            != self.scope_structure_node_ids
        ):
            raise ValueError("provision outcome scope does not match its extraction")
        local_binding_error = _local_extraction_binding_error(self.extraction)
        unresolved = _extraction_has_unresolved(self.extraction)
        if self.status is LlmProvisionExtractionStatus.SUCCESS:
            if (
                self.error_code is not None
                or local_binding_error is not None
                or unresolved
            ):
                raise ValueError("successful provision outcome is inconsistent")
            return
        if self.error_code is None:
            raise ValueError("unresolved provision outcomes require an error code")
        expected_error = _expected_local_outcome_error(
            self.extraction,
            local_binding_error=local_binding_error,
            unresolved=unresolved,
        )
        if self.error_code not in {
            expected_error,
            "provision_result_binding_invalid",
        }:
            raise ValueError("unresolved provision outcome error is inconsistent")


@dataclass(frozen=True)
class LlmRequirementBatchResult:
    """Ordered references to provision results, never a forged global snapshot."""

    located_scope: LocatedLlmExtractionScope
    provider_binding: tuple[str, str, str]
    status: LlmRequirementBatchStatus
    provisions: tuple[LlmProvisionExtractionOutcome, ...]
    requirement_view_node_refs: tuple[LlmRequirementViewNodeRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.located_scope, LocatedLlmExtractionScope):
            raise TypeError("batch result requires a located scope")
        if (
            not isinstance(self.provider_binding, tuple)
            or len(self.provider_binding) != 3
            or any(
                not isinstance(item, str) or not item.strip()
                for item in self.provider_binding
            )
        ):
            raise ValueError("batch result provider binding is invalid")
        if not isinstance(self.provisions, tuple) or not self.provisions:
            raise ValueError("batch result requires provision outcomes")
        if any(
            not isinstance(item, LlmProvisionExtractionOutcome)
            for item in self.provisions
        ):
            raise TypeError("batch result provisions are invalid")
        roots = tuple(item.root_section_structure_node_id for item in self.provisions)
        if roots != self.located_scope.root_section_structure_node_ids:
            raise ValueError("batch provision roots do not match the located scope")
        source_orders = tuple(item.source_order for item in self.provisions)
        if source_orders != tuple(sorted(source_orders)) or len(source_orders) != len(
            set(source_orders)
        ):
            raise ValueError("batch provisions are not in unique source order")
        scoped_targets = tuple(
            target_id
            for item in self.provisions
            for target_id in item.scope_structure_node_ids
        )
        if len(scoped_targets) != len(set(scoped_targets)) or set(
            scoped_targets
        ) != set(self.located_scope.scope_structure_node_ids):
            raise ValueError("batch provision scopes do not exactly partition scope")
        successful = tuple(
            item
            for item in self.provisions
            if item.status is LlmProvisionExtractionStatus.SUCCESS
        )
        if any(
            _result_provider_binding(item.extraction) != self.provider_binding
            for item in successful
        ):
            raise ValueError("successful batch provisions drifted provider binding")
        if any(
            (run.provider_id, run.provider_version, run.model_id)
            != self.provider_binding
            for item in self.provisions
            for run in item.completed_provider_runs
        ):
            raise ValueError("blocked batch run receipts drifted provider binding")
        expected_status = (
            LlmRequirementBatchStatus.COMPLETE
            if len(successful) == len(self.provisions)
            else LlmRequirementBatchStatus.PARTIAL
            if successful
            else LlmRequirementBatchStatus.UNRESOLVED
        )
        if self.status is not expected_status:
            raise ValueError("batch result status does not match provision outcomes")
        expected_refs = _ordered_view_node_refs(self.provisions)
        if self.requirement_view_node_refs != expected_refs:
            raise ValueError("batch view references do not match successful outcomes")

    @property
    def successful_provision_count(self) -> int:
        return sum(
            item.status is LlmProvisionExtractionStatus.SUCCESS
            for item in self.provisions
        )

    @property
    def unresolved_provision_count(self) -> int:
        return sum(
            item.status is LlmProvisionExtractionStatus.UNRESOLVED
            for item in self.provisions
        )

    @property
    def blocked_provision_count(self) -> int:
        return sum(
            item.status is LlmProvisionExtractionStatus.BLOCKED_INPUT
            for item in self.provisions
        )


@dataclass(frozen=True)
class LlmRequirementDocumentResult:
    """One automatic full-document run partitioned into semantic budget chunks."""

    located_scope: LocatedLlmExtractionScope
    provider_binding: tuple[str, str, str]
    status: LlmRequirementBatchStatus
    chunks: tuple[LlmProvisionExtractionOutcome, ...]
    requirement_view_node_refs: tuple[LlmRequirementViewNodeRef, ...]
    semantic_image_page_numbers: tuple[int, ...]
    parent_boundary_results: tuple[DocumentParentBoundaryProviderResult, ...] = ()
    runtime_version: str = LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION

    def __post_init__(self) -> None:
        if self.runtime_version != LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION:
            raise ValueError("document extraction runtime identity is incompatible")
        if not isinstance(self.located_scope, LocatedLlmExtractionScope):
            raise TypeError("document result requires a located scope")
        if (
            not isinstance(self.provider_binding, tuple)
            or len(self.provider_binding) != 3
            or any(
                not isinstance(item, str) or not item.strip()
                for item in self.provider_binding
            )
        ):
            raise ValueError("document result provider binding is invalid")
        if not isinstance(self.chunks, tuple) or not self.chunks:
            raise ValueError("document result requires semantic chunks")
        if any(
            not isinstance(item, LlmProvisionExtractionOutcome) for item in self.chunks
        ):
            raise TypeError("document result chunks are invalid")
        source_orders = tuple(item.source_order for item in self.chunks)
        if source_orders != tuple(sorted(source_orders)) or len(source_orders) != len(
            set(source_orders)
        ):
            raise ValueError("document chunks are not in unique source order")
        scoped_targets = tuple(
            target_id
            for chunk in self.chunks
            for target_id in chunk.scope_structure_node_ids
        )
        if len(scoped_targets) != len(set(scoped_targets)) or set(
            scoped_targets
        ) != set(self.located_scope.scope_structure_node_ids):
            raise ValueError("document chunks do not exactly partition scope")
        successful = tuple(
            item
            for item in self.chunks
            if item.status is LlmProvisionExtractionStatus.SUCCESS
        )
        if any(
            _result_provider_binding(item.extraction) != self.provider_binding
            for item in successful
        ):
            raise ValueError("successful document chunks drifted provider binding")
        if any(
            (run.provider_id, run.provider_version, run.model_id)
            != self.provider_binding
            for item in self.chunks
            for run in item.completed_provider_runs
        ):
            raise ValueError("blocked document chunk receipts drifted provider binding")
        expected_status = (
            LlmRequirementBatchStatus.COMPLETE
            if len(successful) == len(self.chunks)
            else LlmRequirementBatchStatus.PARTIAL
            if successful
            else LlmRequirementBatchStatus.UNRESOLVED
        )
        if self.status is not expected_status:
            raise ValueError("document result status does not match chunk outcomes")
        if self.requirement_view_node_refs != _ordered_view_node_refs(self.chunks):
            raise ValueError("document view references do not match successful chunks")
        if (
            not isinstance(self.semantic_image_page_numbers, tuple)
            or self.semantic_image_page_numbers
            != tuple(sorted(set(self.semantic_image_page_numbers)))
            or any(
                isinstance(page, bool) or not isinstance(page, int) or page < 1
                for page in self.semantic_image_page_numbers
            )
        ):
            raise ValueError("document semantic image pages are invalid")
        if (
            not isinstance(self.parent_boundary_results, tuple)
            or any(
                not isinstance(item, DocumentParentBoundaryProviderResult)
                for item in self.parent_boundary_results
            )
            or len({item.run.provider_run_id for item in self.parent_boundary_results})
            != len(self.parent_boundary_results)
            or any(
                (
                    item.run.provider_id,
                    item.run.provider_version,
                    item.run.model_id,
                )
                != self.provider_binding
                for item in self.parent_boundary_results
            )
        ):
            raise ValueError("document parent-boundary results are invalid")

    @property
    def provisions(self) -> tuple[LlmProvisionExtractionOutcome, ...]:
        """Compatibility alias for the shared presenter/review contract."""

        return self.chunks

    @property
    def successful_provision_count(self) -> int:
        return sum(
            item.status is LlmProvisionExtractionStatus.SUCCESS for item in self.chunks
        )

    @property
    def unresolved_provision_count(self) -> int:
        return sum(
            item.status is LlmProvisionExtractionStatus.UNRESOLVED
            for item in self.chunks
        )

    @property
    def blocked_provision_count(self) -> int:
        return sum(
            item.status is LlmProvisionExtractionStatus.BLOCKED_INPUT
            for item in self.chunks
        )


@dataclass(frozen=True)
class LlmRequirementDocumentProgress:
    """Text-free progress emitted while one automatic document run executes."""

    stage: str
    total_clauses: int
    total_targets: int
    total_chunks: int
    completed_chunks: int
    completed_targets: int
    successful_chunks: int
    unresolved_chunks: int
    blocked_chunks: int
    current_chunk_index: int | None = None
    current_chunk_target_count: int = 0
    progress_basis: str = "SOURCE_ANCHORS"

    def __post_init__(self) -> None:
        if self.stage not in {"planned", "running", "completed"}:
            raise ValueError("document progress stage is invalid")
        counts = (
            self.total_clauses,
            self.total_targets,
            self.total_chunks,
            self.completed_chunks,
            self.completed_targets,
            self.successful_chunks,
            self.unresolved_chunks,
            self.blocked_chunks,
            self.current_chunk_target_count,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise ValueError("document progress counts must be non-negative integers")
        if self.completed_chunks > self.total_chunks:
            raise ValueError("completed document chunks exceed the planned total")
        if self.completed_targets > self.total_targets:
            raise ValueError("completed document targets exceed the planned total")
        if (
            self.successful_chunks + self.unresolved_chunks + self.blocked_chunks
            != self.completed_chunks
        ):
            raise ValueError("document progress outcome counts are inconsistent")
        if self.current_chunk_index is not None and (
            isinstance(self.current_chunk_index, bool)
            or not 1 <= self.current_chunk_index <= self.total_chunks
        ):
            raise ValueError("current document chunk index is invalid")
        if self.progress_basis not in {"SOURCE_ANCHORS", "PAGE_EVIDENCE"}:
            raise ValueError("document progress basis is invalid")


@dataclass(frozen=True)
class LlmRequirementDocumentCheckpoint:
    """One validated terminal chunk emitted before the document is complete."""

    sequence: int
    chunk_index: int
    progress: LlmRequirementDocumentProgress
    outcome: LlmProvisionExtractionOutcome

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
        ):
            raise ValueError("document checkpoint sequence is invalid")
        if (
            isinstance(self.chunk_index, bool)
            or not isinstance(self.chunk_index, int)
            or self.chunk_index < 1
        ):
            raise ValueError("document checkpoint chunk_index is invalid")
        if not isinstance(self.progress, LlmRequirementDocumentProgress):
            raise TypeError("document checkpoint progress is invalid")
        if not isinstance(self.outcome, LlmProvisionExtractionOutcome):
            raise TypeError("document checkpoint outcome is invalid")
        if self.progress.stage != "running":
            raise ValueError("document checkpoint requires running progress")
        if self.sequence != self.progress.completed_chunks:
            raise ValueError("document checkpoint sequence drifted progress")
        if self.chunk_index > self.progress.total_chunks:
            raise ValueError("document checkpoint index exceeds planned chunks")


@dataclass(frozen=True)
class _ProvisionScope:
    root_section_structure_node_id: str
    scope_structure_node_ids: tuple[str, ...]
    source_order: int


@dataclass(frozen=True)
class _PreparedDocumentChunk:
    provision: _ProvisionScope
    images: tuple[SemanticImageInput, ...]
    error_code: str | None = None


class _LockedSemanticProvider:
    """Delegate calls while keeping one immutable batch provider identity."""

    def __init__(
        self,
        provider: SemanticProviderPort,
        binding: tuple[str, str, str],
        target_progress: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self._provider = provider
        self.provider_id, self.provider_version, self.model_id = binding
        self._binding = binding
        self._target_progress = target_progress
        self.completed_runs: list[SemanticProviderRunRecord] = []

    def classify_nodes(self, request):  # type: ignore[no-untyped-def]
        self._assert_provider_identity()
        result = self._provider.classify_nodes(request)
        self._assert_provider_identity()
        self.completed_runs.append(result.run)
        if self._target_progress is not None:
            self._target_progress(request.scope_structure_node_ids)
        return result

    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        self._assert_provider_identity()
        result = self._provider.assemble_requirements(request)
        self._assert_provider_identity()
        self.completed_runs.append(result.run)
        return result

    def preflight_classification_request(
        self,
        request,
    ) -> None:  # type: ignore[no-untyped-def]
        self._assert_provider_identity()
        preflight = getattr(
            self._provider,
            "preflight_classification_request",
            None,
        )
        if callable(preflight):
            preflight(request)
        self._assert_provider_identity()

    def preflight_assembly_request(
        self,
        request,
    ) -> None:  # type: ignore[no-untyped-def]
        self._assert_provider_identity()
        preflight = getattr(
            self._provider,
            "preflight_assembly_request",
            None,
        )
        if callable(preflight):
            preflight(request)
        self._assert_provider_identity()

    def _assert_provider_identity(self) -> None:
        if _provider_binding(self._provider) != self._binding:
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.PROVIDER_IDENTITY_MUTATED
            )


def extract_requirement_provisions_with_provider(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: PreflightSemanticProviderPort,
    scope_locator: LlmExtractionScopeLocator,
    *,
    images: tuple[SemanticImageInput, ...] = (),
    max_images_per_classification_request: int = (
        DEFAULT_MAX_SEMANTIC_IMAGES_PER_REQUEST
    ),
) -> LlmRequirementBatchResult:
    """Execute each located section provision behind an isolation boundary.

    Scope resolution and partition validation complete before the first remote
    semantic call.  Each provision retains its own SemanticClassification,
    RequirementAssembly and RequirementView snapshots.  The batch exposes only
    ordered text-free references across those child snapshots, so a partial run
    can never masquerade as one globally validated extraction snapshot.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if not isinstance(provider, PreflightSemanticProviderPort):
        raise TypeError("provider must implement PreflightSemanticProviderPort")
    if not isinstance(scope_locator, LlmExtractionScopeLocator):
        raise TypeError("scope_locator must be an LlmExtractionScopeLocator")
    if not isinstance(images, tuple) or any(
        not isinstance(item, SemanticImageInput) for item in images
    ):
        raise TypeError("images must be a tuple of SemanticImageInput values")
    if (
        isinstance(max_images_per_classification_request, bool)
        or not isinstance(max_images_per_classification_request, int)
        or max_images_per_classification_request < 1
    ):
        raise ValueError(
            "max_images_per_classification_request must be a positive integer"
        )

    provider_binding = _provider_binding(provider)
    located_scope = locate_llm_extraction_scope(source_ir, tree, scope_locator)
    provisions = _partition_located_scope(source_ir, tree, located_scope)
    if len(provisions) > MAX_LLM_PROVISIONS_PER_BATCH:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.PROVISION_BUDGET_EXCEEDED
        )

    blocked_roots: dict[str, str] = {}
    for provision in provisions:
        try:
            plan = plan_content_classification_provider_requests(
                source_ir,
                tree,
                scope_structure_node_ids=provision.scope_structure_node_ids,
                images=images,
                max_images_per_request=max_images_per_classification_request,
            )
        except SemanticClassificationRejected:
            blocked_roots[provision.root_section_structure_node_id] = (
                "provision_input_invalid"
            )
            continue
        except SemanticProviderRequestRejected as exc:
            blocked_roots[provision.root_section_structure_node_id] = exc.code.value
            continue
        if (
            plan.grounding_scaffold.scope_structure_node_ids
            != provision.scope_structure_node_ids
        ):
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
            )
        for plan_item in plan.items:
            if plan_item.local_rejection_code is not None:
                continue
            try:
                provider.preflight_classification_request(plan_item.request)
            except SemanticProviderRequestRejected:
                # Runtime adaptive planning bisects at source-bound unit edges
                # and terminally receipts only an indivisible rejected unit.
                continue
        try:
            assembly_plan = plan_requirement_assembly_provider_requests(
                source_ir,
                tree,
                plan.grounding_scaffold,
                target_windows=tuple(
                    item.request.scope_structure_node_ids for item in plan.items
                ),
                max_nodes_per_request=80,
            )
        except SemanticProviderRequestRejected as exc:
            blocked_roots[provision.root_section_structure_node_id] = exc.code.value
            continue
        for plan_item in assembly_plan:
            if plan_item.local_rejection_code is not None:
                continue
            try:
                provider.preflight_assembly_request(plan_item.request)
            except SemanticProviderRequestRejected:
                continue

    if _provider_binding(provider) != provider_binding:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.PROVIDER_IDENTITY_MUTATED
        )
    locked_provider = _LockedSemanticProvider(provider, provider_binding)

    outcomes: list[LlmProvisionExtractionOutcome] = []
    for provision in provisions:
        if provision.root_section_structure_node_id in blocked_roots:
            outcomes.append(
                _blocked_outcome(
                    provision,
                    blocked_roots[provision.root_section_structure_node_id],
                )
            )
            continue
        completed_run_start = len(locked_provider.completed_runs)
        try:
            extraction = extract_requirements_with_provider(
                source_ir,
                tree,
                locked_provider,
                scope_structure_node_ids=provision.scope_structure_node_ids,
                images=images,
                max_images_per_classification_request=(
                    max_images_per_classification_request
                ),
                max_nodes_per_classification_request=80,
            )
        except SemanticProviderRequestRejected as exc:
            outcomes.append(
                _blocked_outcome(
                    provision,
                    exc.code.value,
                    completed_provider_runs=tuple(
                        locked_provider.completed_runs[completed_run_start:]
                    ),
                )
            )
            continue

        resolution_error = _extraction_resolution_error(
            source_ir,
            tree,
            provider_binding,
            provision,
            extraction,
        )
        if resolution_error is None:
            outcomes.append(
                LlmProvisionExtractionOutcome(
                    root_section_structure_node_id=(
                        provision.root_section_structure_node_id
                    ),
                    scope_structure_node_ids=provision.scope_structure_node_ids,
                    source_order=provision.source_order,
                    status=LlmProvisionExtractionStatus.SUCCESS,
                    extraction=extraction,
                    error_code=None,
                )
            )
        else:
            outcomes.append(
                LlmProvisionExtractionOutcome(
                    root_section_structure_node_id=(
                        provision.root_section_structure_node_id
                    ),
                    scope_structure_node_ids=provision.scope_structure_node_ids,
                    source_order=provision.source_order,
                    status=LlmProvisionExtractionStatus.UNRESOLVED,
                    extraction=extraction,
                    error_code=resolution_error,
                )
            )

    outcome_tuple = tuple(outcomes)
    view_refs = _ordered_view_node_refs(outcome_tuple)
    successful_count = sum(
        item.status is LlmProvisionExtractionStatus.SUCCESS for item in outcome_tuple
    )
    batch_status = (
        LlmRequirementBatchStatus.COMPLETE
        if successful_count == len(outcome_tuple)
        else LlmRequirementBatchStatus.PARTIAL
        if successful_count
        else LlmRequirementBatchStatus.UNRESOLVED
    )
    return LlmRequirementBatchResult(
        located_scope=located_scope,
        provider_binding=provider_binding,
        status=batch_status,
        provisions=outcome_tuple,
        requirement_view_node_refs=view_refs,
    )


def extract_requirement_document_with_provider(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: PreflightSemanticProviderPort,
    located_scope: LocatedLlmExtractionScope,
    *,
    image_loader: Callable[[tuple[int, ...]], tuple[SemanticImageInput, ...]],
    max_chunk_nodes: int | None = None,
    max_images_per_classification_request: int = (MAX_LLM_DOCUMENT_CHUNK_IMAGE_PAGES),
    max_concurrent_chunks: int = 1,
    progress_callback: Callable[[LlmRequirementDocumentProgress], None] | None = None,
    checkpoint_callback: Callable[[LlmRequirementDocumentCheckpoint], None]
    | None = None,
) -> LlmRequirementDocumentResult:
    """Automatically analyze every located BODY clause in bounded semantic chunks.

    All chunks are locally planned, rendered and preflighted before the first
    provider call. Adjacent complete clauses share one classification request
    and one assembly request. The product path may run four remote chunks at
    once, then restores exact source order before the consolidated review is
    built.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if not isinstance(provider, PreflightSemanticProviderPort):
        raise TypeError("provider must implement PreflightSemanticProviderPort")
    if not isinstance(located_scope, LocatedLlmExtractionScope):
        raise TypeError("located_scope must be a LocatedLlmExtractionScope")
    if not callable(image_loader):
        raise TypeError("image_loader must be callable")
    if progress_callback is not None and not callable(progress_callback):
        raise TypeError("progress_callback must be callable")
    if checkpoint_callback is not None and not callable(checkpoint_callback):
        raise TypeError("checkpoint_callback must be callable")
    if max_chunk_nodes is None:
        max_chunk_nodes = getattr(
            provider,
            "document_chunk_node_limit",
            MAX_LLM_DOCUMENT_CHUNK_NODES,
        )
    if (
        isinstance(max_chunk_nodes, bool)
        or not isinstance(max_chunk_nodes, int)
        or max_chunk_nodes < 1
        or max_chunk_nodes > 80
    ):
        raise ValueError("max_chunk_nodes must be an integer between 1 and 80")
    if (
        isinstance(max_images_per_classification_request, bool)
        or not isinstance(max_images_per_classification_request, int)
        or max_images_per_classification_request < 1
    ):
        raise ValueError(
            "max_images_per_classification_request must be a positive integer"
        )
    if (
        isinstance(max_concurrent_chunks, bool)
        or not isinstance(max_concurrent_chunks, int)
        or not 1 <= max_concurrent_chunks <= MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS
    ):
        raise ValueError(
            "max_concurrent_chunks must be an integer between 1 and "
            f"{MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS}"
        )
    provider_binding = _provider_binding(provider)
    is_codex_document_session = provider_binding[0].startswith("codex-app-server-")
    if is_codex_document_session or getattr(
        provider, "document_session_requires_serial_order", False
    ):
        max_concurrent_chunks = 1
    include_all_scope_pages = bool(
        is_codex_document_session
        or getattr(provider, "requires_scope_page_images", False)
    )
    units = _partition_document_scope(source_ir, tree, located_scope)
    atomic_groups = _document_atomic_unit_groups(tree, units)
    candidate_groups = (
        (atomic_groups,)
        if is_codex_document_session
        else _pack_document_unit_groups(
            source_ir,
            tree,
            atomic_groups,
            max_chunk_nodes=max_chunk_nodes,
            max_image_pages=max_images_per_classification_request,
            include_all_source_pages=include_all_scope_pages,
        )
    )
    prepared: list[_PreparedDocumentChunk] = []
    for groups in candidate_groups:
        prepared.extend(
            _prepare_document_chunk_groups(
                source_ir,
                tree,
                provider,
                groups,
                image_loader=image_loader,
                max_chunk_nodes=max_chunk_nodes,
                max_images_per_classification_request=(
                    max_images_per_classification_request
                ),
            )
        )
    if _provider_binding(provider) != provider_binding:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.PROVIDER_IDENTITY_MUTATED
        )
    if isinstance(provider, DocumentSessionSemanticProviderPort):
        images_by_page: dict[int, SemanticImageInput] = {}
        for item in prepared:
            for image in item.images:
                previous = images_by_page.setdefault(image.page_number, image)
                if previous.image_sha256 != image.image_sha256:
                    raise ValueError("document session page image identity drifted")
        provider.prepare_document_session(
            source_ir_id=source_ir.source_ir_id,
            structure_snapshot_id=structure_snapshot_id(tree),
            images=tuple(images_by_page[page] for page in sorted(images_by_page)),
        )

    outcomes: list[LlmProvisionExtractionOutcome] = []
    parent_boundary_results: tuple[DocumentParentBoundaryProviderResult, ...] = ()
    classification_target_windows: tuple[tuple[str, ...], ...] | None = None
    _emit_document_progress(
        progress_callback,
        stage="planned",
        total_clauses=len(units),
        total_targets=len(located_scope.scope_structure_node_ids),
        prepared=prepared,
        outcomes=outcomes,
    )
    if (
        is_codex_document_session
        and len(prepared) == 1
        and isinstance(provider, DocumentParentBoundaryProviderPort)
    ):
        _emit_document_progress(
            progress_callback,
            stage="running",
            total_clauses=len(units),
            total_targets=len(located_scope.scope_structure_node_ids),
            prepared=prepared,
            outcomes=outcomes,
            current_chunk_index=1,
            current_chunk_target_count=len(located_scope.scope_structure_node_ids),
        )
        parent_plans = build_document_parent_boundary_plans(
            source_ir,
        )
        parent_boundary_results = tuple(
            provider.identify_parent_boundaries(plan.request) for plan in parent_plans
        )
        classification_target_windows = build_parent_boundary_target_windows(
            parent_plans,
            parent_boundary_results,
            tree,
            located_scope.scope_structure_node_ids,
        )
        if _provider_binding(provider) != provider_binding:
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.PROVIDER_IDENTITY_MUTATED
            )
    if max_concurrent_chunks > 1:
        outcomes = _extract_prepared_document_chunks_concurrently(
            source_ir,
            tree,
            provider,
            provider_binding=provider_binding,
            prepared=prepared,
            max_images_per_classification_request=(
                max_images_per_classification_request
            ),
            max_nodes_per_classification_request=max_chunk_nodes,
            max_concurrent_chunks=max_concurrent_chunks,
            progress_callback=progress_callback,
            checkpoint_callback=checkpoint_callback,
            total_clauses=len(units),
            total_targets=len(located_scope.scope_structure_node_ids),
        )
    else:
        classified_target_ids: set[str] = set()

        def record_classified_targets(target_ids: tuple[str, ...]) -> None:
            classified_target_ids.update(target_ids)
            _emit_document_progress(
                progress_callback,
                stage="running",
                total_clauses=len(units),
                total_targets=len(located_scope.scope_structure_node_ids),
                prepared=prepared,
                outcomes=outcomes,
                current_chunk_index=1,
                current_chunk_target_count=len(located_scope.scope_structure_node_ids),
                in_progress_completed_targets=len(classified_target_ids),
            )

        locked_provider = _LockedSemanticProvider(
            provider,
            provider_binding,
            target_progress=(
                record_classified_targets if is_codex_document_session else None
            ),
        )
        for chunk_index, item in enumerate(prepared, start=1):
            provision = item.provision
            _emit_document_progress(
                progress_callback,
                stage="running",
                total_clauses=len(units),
                total_targets=len(located_scope.scope_structure_node_ids),
                prepared=prepared,
                outcomes=outcomes,
                current_chunk_index=chunk_index,
                current_chunk_target_count=len(provision.scope_structure_node_ids),
            )
            if item.error_code is not None:
                outcomes.append(_blocked_outcome(provision, item.error_code))
                terminal_progress = _emit_document_progress(
                    progress_callback,
                    stage="running",
                    total_clauses=len(units),
                    total_targets=len(located_scope.scope_structure_node_ids),
                    prepared=prepared,
                    outcomes=outcomes,
                    current_chunk_index=chunk_index,
                )
                _emit_document_checkpoint(
                    checkpoint_callback,
                    sequence=len(outcomes),
                    chunk_index=chunk_index,
                    progress=terminal_progress,
                    outcome=outcomes[-1],
                )
                continue
            completed_run_start = len(locked_provider.completed_runs)
            try:
                with llm_document_remote_chunk_lease():
                    extraction = extract_requirements_with_provider(
                        source_ir,
                        tree,
                        locked_provider,
                        scope_structure_node_ids=provision.scope_structure_node_ids,
                        images=item.images,
                        max_images_per_classification_request=(
                            max_images_per_classification_request
                        ),
                        max_nodes_per_classification_request=max_chunk_nodes,
                        pack_adjacent_provisions=True,
                        classification_target_windows=(classification_target_windows),
                        atomization_provider=(
                            provider
                            if isinstance(
                                provider,
                                RequirementAtomizationProviderPort,
                            )
                            else None
                        ),
                    )
            except SemanticProviderRequestRejected as exc:
                outcomes.append(
                    _blocked_outcome(
                        provision,
                        exc.code.value,
                        completed_provider_runs=tuple(
                            locked_provider.completed_runs[completed_run_start:]
                        ),
                    )
                )
                terminal_progress = _emit_document_progress(
                    progress_callback,
                    stage="running",
                    total_clauses=len(units),
                    total_targets=len(located_scope.scope_structure_node_ids),
                    prepared=prepared,
                    outcomes=outcomes,
                    current_chunk_index=chunk_index,
                )
                _emit_document_checkpoint(
                    checkpoint_callback,
                    sequence=len(outcomes),
                    chunk_index=chunk_index,
                    progress=terminal_progress,
                    outcome=outcomes[-1],
                )
                continue

            resolution_error = _extraction_resolution_error(
                source_ir,
                tree,
                provider_binding,
                provision,
                extraction,
            )
            outcomes.append(
                LlmProvisionExtractionOutcome(
                    root_section_structure_node_id=(
                        provision.root_section_structure_node_id
                    ),
                    scope_structure_node_ids=provision.scope_structure_node_ids,
                    source_order=provision.source_order,
                    status=(
                        LlmProvisionExtractionStatus.SUCCESS
                        if resolution_error is None
                        else LlmProvisionExtractionStatus.UNRESOLVED
                    ),
                    extraction=extraction,
                    error_code=resolution_error,
                )
            )
            terminal_progress = _emit_document_progress(
                progress_callback,
                stage="running",
                total_clauses=len(units),
                total_targets=len(located_scope.scope_structure_node_ids),
                prepared=prepared,
                outcomes=outcomes,
                current_chunk_index=chunk_index,
            )
            _emit_document_checkpoint(
                checkpoint_callback,
                sequence=len(outcomes),
                chunk_index=chunk_index,
                progress=terminal_progress,
                outcome=outcomes[-1],
            )

    outcome_tuple = tuple(outcomes)
    successful_count = sum(
        item.status is LlmProvisionExtractionStatus.SUCCESS for item in outcome_tuple
    )
    status = (
        LlmRequirementBatchStatus.COMPLETE
        if successful_count == len(outcome_tuple)
        else LlmRequirementBatchStatus.PARTIAL
        if successful_count
        else LlmRequirementBatchStatus.UNRESOLVED
    )
    result = LlmRequirementDocumentResult(
        located_scope=located_scope,
        provider_binding=provider_binding,
        status=status,
        chunks=outcome_tuple,
        requirement_view_node_refs=_ordered_view_node_refs(outcome_tuple),
        semantic_image_page_numbers=tuple(
            sorted({image.page_number for item in prepared for image in item.images})
        ),
        parent_boundary_results=parent_boundary_results,
    )
    _emit_document_progress(
        progress_callback,
        stage="completed",
        total_clauses=len(units),
        total_targets=len(located_scope.scope_structure_node_ids),
        prepared=prepared,
        outcomes=outcomes,
    )
    return result


def _extract_prepared_document_chunks_concurrently(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: PreflightSemanticProviderPort,
    *,
    provider_binding: tuple[str, str, str],
    prepared: list[_PreparedDocumentChunk],
    max_images_per_classification_request: int,
    max_nodes_per_classification_request: int,
    max_concurrent_chunks: int,
    progress_callback: Callable[[LlmRequirementDocumentProgress], None] | None,
    checkpoint_callback: Callable[[LlmRequirementDocumentCheckpoint], None] | None,
    total_clauses: int,
    total_targets: int,
) -> list[LlmProvisionExtractionOutcome]:
    """Run at most four remote chunks at once and restore exact source order."""

    outcomes_by_index: dict[int, LlmProvisionExtractionOutcome] = {}
    pending: dict[Future[LlmProvisionExtractionOutcome], int] = {}
    next_index = 0
    with ThreadPoolExecutor(
        max_workers=max_concurrent_chunks,
        thread_name_prefix="llm-document-chunk",
    ) as executor:
        while next_index < min(max_concurrent_chunks, len(prepared)):
            future = executor.submit(
                _extract_prepared_document_chunk,
                source_ir,
                tree,
                provider,
                provider_binding=provider_binding,
                item=prepared[next_index],
                max_images_per_classification_request=(
                    max_images_per_classification_request
                ),
                max_nodes_per_classification_request=(
                    max_nodes_per_classification_request
                ),
            )
            pending[future] = next_index
            next_index += 1

        _emit_document_progress(
            progress_callback,
            stage="running",
            total_clauses=total_clauses,
            total_targets=total_targets,
            prepared=prepared,
            outcomes=[],
            current_chunk_index=1 if prepared else None,
            current_chunk_target_count=(
                len(prepared[0].provision.scope_structure_node_ids) if prepared else 0
            ),
        )
        try:
            while pending:
                completed, _ = wait(
                    tuple(pending),
                    timeout=LLM_DOCUMENT_PROGRESS_HEARTBEAT_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                if not completed:
                    ordered_outcomes = [
                        outcomes_by_index[index] for index in sorted(outcomes_by_index)
                    ]
                    current_index = min(pending.values())
                    _emit_document_progress(
                        progress_callback,
                        stage="running",
                        total_clauses=total_clauses,
                        total_targets=total_targets,
                        prepared=prepared,
                        outcomes=ordered_outcomes,
                        current_chunk_index=current_index + 1,
                        current_chunk_target_count=len(
                            prepared[current_index].provision.scope_structure_node_ids
                        ),
                    )
                    continue
                for future in sorted(completed, key=lambda item: pending[item]):
                    chunk_index = pending.pop(future)
                    outcomes_by_index[chunk_index] = future.result()
                    ordered_outcomes = [
                        outcomes_by_index[index] for index in sorted(outcomes_by_index)
                    ]
                    next_unfinished_index = next(
                        (
                            index
                            for index in range(len(prepared))
                            if index not in outcomes_by_index
                        ),
                        None,
                    )
                    terminal_progress = _emit_document_progress(
                        progress_callback,
                        stage="running",
                        total_clauses=total_clauses,
                        total_targets=total_targets,
                        prepared=prepared,
                        outcomes=ordered_outcomes,
                        current_chunk_index=(
                            next_unfinished_index + 1
                            if next_unfinished_index is not None
                            else None
                        ),
                        current_chunk_target_count=(
                            len(
                                prepared[
                                    next_unfinished_index
                                ].provision.scope_structure_node_ids
                            )
                            if next_unfinished_index is not None
                            else 0
                        ),
                    )
                    _emit_document_checkpoint(
                        checkpoint_callback,
                        sequence=len(outcomes_by_index),
                        chunk_index=chunk_index + 1,
                        progress=terminal_progress,
                        outcome=outcomes_by_index[chunk_index],
                    )
                while (
                    next_index < len(prepared) and len(pending) < max_concurrent_chunks
                ):
                    future = executor.submit(
                        _extract_prepared_document_chunk,
                        source_ir,
                        tree,
                        provider,
                        provider_binding=provider_binding,
                        item=prepared[next_index],
                        max_images_per_classification_request=(
                            max_images_per_classification_request
                        ),
                        max_nodes_per_classification_request=(
                            max_nodes_per_classification_request
                        ),
                    )
                    pending[future] = next_index
                    next_index += 1
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    return [outcomes_by_index[index] for index in range(len(prepared))]


def _extract_prepared_document_chunk(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: PreflightSemanticProviderPort,
    *,
    provider_binding: tuple[str, str, str],
    item: _PreparedDocumentChunk,
    max_images_per_classification_request: int,
    max_nodes_per_classification_request: int,
) -> LlmProvisionExtractionOutcome:
    provision = item.provision
    if item.error_code is not None:
        return _blocked_outcome(provision, item.error_code)

    locked_provider = _LockedSemanticProvider(provider, provider_binding)
    try:
        with llm_document_remote_chunk_lease():
            extraction = extract_requirements_with_provider(
                source_ir,
                tree,
                locked_provider,
                scope_structure_node_ids=provision.scope_structure_node_ids,
                images=item.images,
                max_images_per_classification_request=(
                    max_images_per_classification_request
                ),
                max_nodes_per_classification_request=(
                    max_nodes_per_classification_request
                ),
                pack_adjacent_provisions=True,
            )
    except SemanticProviderRequestRejected as exc:
        return _blocked_outcome(
            provision,
            exc.code.value,
            completed_provider_runs=tuple(locked_provider.completed_runs),
        )

    resolution_error = _extraction_resolution_error(
        source_ir,
        tree,
        provider_binding,
        provision,
        extraction,
    )
    return LlmProvisionExtractionOutcome(
        root_section_structure_node_id=provision.root_section_structure_node_id,
        scope_structure_node_ids=provision.scope_structure_node_ids,
        source_order=provision.source_order,
        status=(
            LlmProvisionExtractionStatus.SUCCESS
            if resolution_error is None
            else LlmProvisionExtractionStatus.UNRESOLVED
        ),
        extraction=extraction,
        error_code=resolution_error,
    )


def _emit_document_progress(
    callback: Callable[[LlmRequirementDocumentProgress], None] | None,
    *,
    stage: str,
    total_clauses: int,
    total_targets: int,
    prepared: list[_PreparedDocumentChunk],
    outcomes: list[LlmProvisionExtractionOutcome],
    current_chunk_index: int | None = None,
    current_chunk_target_count: int = 0,
    in_progress_completed_targets: int | None = None,
) -> LlmRequirementDocumentProgress:
    terminal_completed_targets = sum(
        len(item.scope_structure_node_ids) for item in outcomes
    )
    completed_targets = (
        terminal_completed_targets
        if in_progress_completed_targets is None
        else max(terminal_completed_targets, in_progress_completed_targets)
    )
    progress = LlmRequirementDocumentProgress(
        stage=stage,
        total_clauses=total_clauses,
        total_targets=total_targets,
        total_chunks=len(prepared),
        completed_chunks=len(outcomes),
        completed_targets=completed_targets,
        successful_chunks=sum(
            item.status is LlmProvisionExtractionStatus.SUCCESS for item in outcomes
        ),
        unresolved_chunks=sum(
            item.status is LlmProvisionExtractionStatus.UNRESOLVED for item in outcomes
        ),
        blocked_chunks=sum(
            item.status is LlmProvisionExtractionStatus.BLOCKED_INPUT
            for item in outcomes
        ),
        current_chunk_index=current_chunk_index,
        current_chunk_target_count=current_chunk_target_count,
    )
    if callback is not None:
        callback(progress)
    return progress


def _emit_document_checkpoint(
    callback: Callable[[LlmRequirementDocumentCheckpoint], None] | None,
    *,
    sequence: int,
    chunk_index: int,
    progress: LlmRequirementDocumentProgress,
    outcome: LlmProvisionExtractionOutcome,
) -> None:
    if callback is None:
        return
    callback(
        LlmRequirementDocumentCheckpoint(
            sequence=sequence,
            chunk_index=chunk_index,
            progress=progress,
            outcome=outcome,
        )
    )


def _validate_structure_binding(
    tree: StructureSnapshot,
    source_ir: SourceIRSnapshot,
) -> None:
    if isinstance(tree, StructureTreeSnapshot):
        validate_structure_tree_against_source_ir(tree, source_ir)
        return
    validate_structure_snapshot_against_source_ir(tree, source_ir)


def _partition_document_scope(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    located_scope: LocatedLlmExtractionScope,
) -> tuple[_ProvisionScope, ...]:
    try:
        _validate_structure_binding(tree, source_ir)
    except (TypeError, ValueError) as exc:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.INPUT_BINDING_INVALID
        ) from exc
    if located_scope.source_sha256 != source_ir.source_sha256:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
        )

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    root_ids = located_scope.root_section_structure_node_ids
    scope_ids = located_scope.scope_structure_node_ids
    root_set = set(root_ids)
    scope_set = set(scope_ids)
    document_roots = tuple(
        node
        for node in tree.nodes
        if node.kind is StructureNodeKind.DOCUMENT
        and node.parent_structure_node_id is None
    )
    root_branch_mode = (
        located_scope.locator_version == LLM_DOCUMENT_ROOT_SCOPE_LOCATOR_VERSION
    )
    if (
        not root_ids
        or not scope_ids
        or len(root_ids) != len(root_set)
        or len(scope_ids) != len(scope_set)
        or any(root_id not in nodes_by_id for root_id in root_ids)
    ):
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
        )
    if root_branch_mode:
        if len(document_roots) != 1 or any(
            nodes_by_id[root_id].kind is StructureNodeKind.DOCUMENT
            or nodes_by_id[root_id].parent_structure_node_id
            != document_roots[0].structure_node_id
            for root_id in root_ids
        ):
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
            )
    elif any(
        nodes_by_id[root_id].kind is not StructureNodeKind.SECTION
        for root_id in root_ids
    ):
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
        )
    expected_roots = tuple(
        node.structure_node_id
        for node in tree.nodes
        if node.structure_node_id in root_set
    )
    expected_scope = tuple(
        node.structure_node_id
        for node in tree.nodes
        if node.structure_node_id in scope_set
    )
    if root_ids != expected_roots or scope_ids != expected_scope:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
        )

    if root_branch_mode:
        document_root_id = document_roots[0].structure_node_id
        owner_by_target = {}
        for node in tree.nodes:
            if node.kind is StructureNodeKind.DOCUMENT:
                continue
            owner_id = _document_root_branch_owner(
                node,
                nodes_by_id=nodes_by_id,
                document_root_id=document_root_id,
            )
            if owner_id in root_set:
                owner_by_target[node.structure_node_id] = owner_id
    else:
        owner_by_target = {root_id: root_id for root_id in root_ids}
        for node in tree.nodes:
            if node.kind is StructureNodeKind.SECTION:
                continue
            owner_id = _nearest_section_ancestor(node, nodes_by_id)
            if owner_id in root_set and node.structure_node_id in scope_set:
                owner_by_target[node.structure_node_id] = owner_id
    if set(owner_by_target) != scope_set:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.INCOMPLETE_PROVISION
        )

    return tuple(
        _ProvisionScope(
            root_section_structure_node_id=root_id,
            scope_structure_node_ids=tuple(
                node.structure_node_id
                for node in tree.nodes
                if owner_by_target.get(node.structure_node_id) == root_id
            ),
            source_order=nodes_by_id[root_id].source_order,
        )
        for root_id in root_ids
    )


def _document_atomic_unit_groups(
    tree: StructureTreeSnapshot,
    units: tuple[_ProvisionScope, ...],
) -> tuple[tuple[_ProvisionScope, ...], ...]:
    owner_by_target = {
        target_id: index
        for index, unit in enumerate(units)
        for target_id in unit.scope_structure_node_ids
    }
    interval_ends = list(range(len(units)))
    for relation in tree.relations:
        if relation.kind is not StructureRelationKind.CONTINUES:
            continue
        left = owner_by_target.get(relation.source_structure_node_id)
        right = owner_by_target.get(relation.target_structure_node_id)
        if left is None and right is None:
            continue
        if left is None or right is None:
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.INCOMPLETE_PROVISION
            )
        start, end = sorted((left, right))
        interval_ends[start] = max(interval_ends[start], end)

    groups: list[tuple[_ProvisionScope, ...]] = []
    index = 0
    while index < len(units):
        end = interval_ends[index]
        cursor = index
        while cursor <= end:
            end = max(end, interval_ends[cursor])
            cursor += 1
        groups.append(units[index : end + 1])
        index = end + 1
    return tuple(groups)


def _pack_document_unit_groups(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    groups: tuple[tuple[_ProvisionScope, ...], ...],
    *,
    max_chunk_nodes: int,
    max_image_pages: int,
    include_all_source_pages: bool = False,
) -> tuple[tuple[tuple[_ProvisionScope, ...], ...], ...]:
    packed: list[tuple[tuple[_ProvisionScope, ...], ...]] = []
    current: list[tuple[_ProvisionScope, ...]] = []
    current_targets: set[str] = set()
    current_pages: set[int] = set()
    for group in groups:
        group_targets = {
            target_id for unit in group for target_id in unit.scope_structure_node_ids
        }
        group_pages = set(
            _semantic_image_pages_for_scope(
                source_ir,
                tree,
                tuple(group_targets),
                include_all_source_pages=include_all_source_pages,
            )
        )
        exceeds = current and (
            len(current_targets | group_targets) > max_chunk_nodes
            or len(current_pages | group_pages) > max_image_pages
        )
        if exceeds:
            packed.append(tuple(current))
            current = []
            current_targets = set()
            current_pages = set()
        current.append(group)
        current_targets.update(group_targets)
        current_pages.update(group_pages)
    if current:
        packed.append(tuple(current))
    return tuple(packed)


def _prepare_document_chunk_groups(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider: PreflightSemanticProviderPort,
    groups: tuple[tuple[_ProvisionScope, ...], ...],
    *,
    image_loader: Callable[[tuple[int, ...]], tuple[SemanticImageInput, ...]],
    max_chunk_nodes: int,
    max_images_per_classification_request: int,
) -> tuple[_PreparedDocumentChunk, ...]:
    provision = _document_chunk_scope(tree, groups)
    page_numbers = _semantic_image_pages_for_scope(
        source_ir,
        tree,
        provision.scope_structure_node_ids,
        include_all_source_pages=bool(
            str(getattr(provider, "provider_id", "")).startswith("codex-app-server-")
            or getattr(provider, "requires_scope_page_images", False)
        ),
    )
    images = image_loader(page_numbers)
    if (
        not isinstance(images, tuple)
        or any(not isinstance(item, SemanticImageInput) for item in images)
        or tuple(item.page_number for item in images) != page_numbers
    ):
        raise TypeError("image_loader returned invalid semantic images")

    error_code: str | None = None
    try:
        plan = plan_content_classification_provider_requests(
            source_ir,
            tree,
            scope_structure_node_ids=provision.scope_structure_node_ids,
            images=images,
            max_nodes_per_request=max_chunk_nodes,
            max_images_per_request=max_images_per_classification_request,
            pack_adjacent_provisions=True,
        )
    except SemanticClassificationRejected:
        error_code = "provision_input_invalid"
    except SemanticProviderRequestRejected as exc:
        error_code = exc.code.value
    if error_code is None:
        for plan_item in plan.items:
            if plan_item.local_rejection_code is not None:
                continue
            try:
                provider.preflight_classification_request(plan_item.request)
            except SemanticProviderRequestRejected:
                continue
        try:
            assembly_plan = plan_requirement_assembly_provider_requests(
                source_ir,
                tree,
                plan.grounding_scaffold,
                target_windows=tuple(
                    item.request.scope_structure_node_ids for item in plan.items
                ),
                max_nodes_per_request=max_chunk_nodes,
            )
        except SemanticProviderRequestRejected as exc:
            error_code = exc.code.value
        else:
            for plan_item in assembly_plan:
                if plan_item.local_rejection_code is not None:
                    continue
                try:
                    provider.preflight_assembly_request(plan_item.request)
                except SemanticProviderRequestRejected:
                    continue

    if error_code is None:
        return (_PreparedDocumentChunk(provision=provision, images=images),)
    if len(groups) == 1:
        return (
            _PreparedDocumentChunk(
                provision=provision,
                images=images,
                error_code=error_code,
            ),
        )
    midpoint = len(groups) // 2
    return (
        *_prepare_document_chunk_groups(
            source_ir,
            tree,
            provider,
            groups[:midpoint],
            image_loader=image_loader,
            max_chunk_nodes=max_chunk_nodes,
            max_images_per_classification_request=(
                max_images_per_classification_request
            ),
        ),
        *_prepare_document_chunk_groups(
            source_ir,
            tree,
            provider,
            groups[midpoint:],
            image_loader=image_loader,
            max_chunk_nodes=max_chunk_nodes,
            max_images_per_classification_request=(
                max_images_per_classification_request
            ),
        ),
    )


def _document_chunk_scope(
    tree: StructureTreeSnapshot,
    groups: tuple[tuple[_ProvisionScope, ...], ...],
) -> _ProvisionScope:
    target_ids = {
        target_id
        for group in groups
        for unit in group
        for target_id in unit.scope_structure_node_ids
    }
    ordered = tuple(
        node.structure_node_id
        for node in tree.nodes
        if node.structure_node_id in target_ids
    )
    if not ordered:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
        )
    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    root_id = groups[0][0].root_section_structure_node_id
    root = nodes_by_id.get(root_id)
    if root is None or root_id != ordered[0]:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
        )
    if root.kind is not StructureNodeKind.SECTION:
        parent = nodes_by_id.get(root.parent_structure_node_id or "")
        if (
            root.kind is StructureNodeKind.DOCUMENT
            or parent is None
            or parent.kind is not StructureNodeKind.DOCUMENT
        ):
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
            )
    return _ProvisionScope(
        root_section_structure_node_id=root_id,
        scope_structure_node_ids=ordered,
        source_order=nodes_by_id[root_id].source_order,
    )


def _semantic_image_pages_for_scope(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    scope_structure_node_ids: tuple[str, ...],
    *,
    include_all_source_pages: bool = False,
) -> tuple[int, ...]:
    scope_set = set(scope_structure_node_ids)
    refs = {
        source_ref
        for node in tree.nodes
        if node.structure_node_id in scope_set
        for source_ref in node.source_refs
    }
    source_node_ids = {
        item.source_id
        for item in refs
        if item.kind is StructureSourceRefKind.SOURCE_NODE
    }
    table_cell_ids = {
        item.source_id
        for item in refs
        if item.kind is StructureSourceRefKind.TABLE_CELL
    }
    pages = {
        node.native_locator.page_number
        for node in source_ir.nodes
        if node.source_node_id in source_node_ids
        and (
            include_all_source_pages
            or node.kind
            in {SourceNodeKind.TABLE, SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
        )
        and node.native_locator.page_number is not None
    }
    pages.update(
        cell.native_locator.page_number
        for node in source_ir.nodes
        if node.table_payload is not None
        for cell in node.table_payload.cells
        if cell.cell_id in table_cell_ids
        and cell.native_locator.page_number is not None
    )
    return tuple(sorted(pages))


def _partition_located_scope(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    located_scope: LocatedLlmExtractionScope,
) -> tuple[_ProvisionScope, ...]:
    try:
        _validate_structure_binding(tree, source_ir)
    except (TypeError, ValueError) as exc:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.INPUT_BINDING_INVALID
        ) from exc
    if located_scope.source_sha256 != source_ir.source_sha256:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
        )

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    root_ids = located_scope.root_section_structure_node_ids
    scope_ids = located_scope.scope_structure_node_ids
    if (
        not root_ids
        or len(root_ids) != len(set(root_ids))
        or not scope_ids
        or len(scope_ids) != len(set(scope_ids))
        or any(
            root_id not in nodes_by_id
            or nodes_by_id[root_id].kind is not StructureNodeKind.SECTION
            for root_id in root_ids
        )
    ):
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
        )
    expected_root_order = tuple(
        item.structure_node_id
        for item in tree.nodes
        if item.structure_node_id in set(root_ids)
    )
    expected_scope_order = tuple(
        item.structure_node_id
        for item in tree.nodes
        if item.structure_node_id in set(scope_ids)
    )
    if root_ids != expected_root_order or scope_ids != expected_scope_order:
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.LOCATED_SCOPE_INVALID
        )

    root_id_set = set(root_ids)
    for root_id in root_ids:
        parent_id = nodes_by_id[root_id].parent_structure_node_id
        while parent_id is not None:
            if parent_id in root_id_set:
                raise LlmProvisionBatchRejected(
                    LlmProvisionBatchRejectionCode.CROSS_PROVISION_PARENT
                )
            parent_id = nodes_by_id[parent_id].parent_structure_node_id

    members_by_root: dict[str, list[str]] = {root_id: [root_id] for root_id in root_ids}
    owner_by_target: dict[str, str] = {root_id: root_id for root_id in root_ids}
    for node in tree.nodes:
        if node.kind is StructureNodeKind.SECTION:
            continue
        owner_id = _nearest_section_ancestor(node, nodes_by_id)
        if owner_id not in root_id_set:
            continue
        if node.structure_node_id in owner_by_target:
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.OVERLAPPING_PROVISIONS
            )
        owner_by_target[node.structure_node_id] = owner_id
        members_by_root[owner_id].append(node.structure_node_id)

    if set(owner_by_target) != set(scope_ids):
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.INCOMPLETE_PROVISION
        )
    for relation in tree.relations:
        if relation.kind is not StructureRelationKind.CONTINUES:
            continue
        source_owner = owner_by_target.get(relation.source_structure_node_id)
        target_owner = owner_by_target.get(relation.target_structure_node_id)
        if (source_owner is None) != (target_owner is None):
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.INCOMPLETE_PROVISION
            )
        if (
            source_owner is not None
            and target_owner is not None
            and source_owner != target_owner
        ):
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.CROSS_PROVISION_CONTINUATION
            )

    return tuple(
        _ProvisionScope(
            root_section_structure_node_id=root_id,
            scope_structure_node_ids=tuple(members_by_root[root_id]),
            source_order=nodes_by_id[root_id].source_order,
        )
        for root_id in root_ids
    )


def _nearest_section_ancestor(
    node: StructureNode,
    nodes_by_id: dict[str, StructureNode],
) -> str | None:
    parent_id = node.parent_structure_node_id
    visited: set[str] = set()
    while parent_id is not None:
        if parent_id in visited or parent_id not in nodes_by_id:
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.INPUT_BINDING_INVALID
            )
        visited.add(parent_id)
        parent = nodes_by_id[parent_id]
        if parent.kind is StructureNodeKind.SECTION:
            return parent_id
        parent_id = parent.parent_structure_node_id
    return None


def _document_root_branch_owner(
    node: StructureNode,
    *,
    nodes_by_id: dict[str, StructureNode],
    document_root_id: str,
) -> str:
    current = node
    visited: set[str] = set()
    while current.parent_structure_node_id != document_root_id:
        parent_id = current.parent_structure_node_id
        if parent_id is None or parent_id in visited or parent_id not in nodes_by_id:
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.INPUT_BINDING_INVALID
            )
        visited.add(parent_id)
        current = nodes_by_id[parent_id]
        if current.kind is StructureNodeKind.DOCUMENT:
            raise LlmProvisionBatchRejected(
                LlmProvisionBatchRejectionCode.INPUT_BINDING_INVALID
            )
    return current.structure_node_id


def _extraction_resolution_error(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    provider_binding: tuple[str, str, str],
    provision: _ProvisionScope,
    extraction: LlmRequirementExtractionResult,
) -> str | None:
    try:
        validate_semantic_classification_against_inputs(
            extraction.semantics,
            source_ir,
            tree,
        )
        validate_requirement_assembly_against_inputs(
            extraction.assembly,
            source_ir,
            tree,
            extraction.semantics,
        )
        validate_assembled_requirement_view_against_inputs(
            extraction.requirement_view,
            source_ir,
            tree,
            extraction.semantics,
            extraction.assembly,
        )
    except (
        SemanticClassificationRejected,
        RequirementAssemblyRejected,
        RequirementProjectionRejected,
    ):
        return "provision_result_binding_invalid"

    if (
        extraction.semantics.scope_structure_node_ids
        != provision.scope_structure_node_ids
        or _local_extraction_binding_error(extraction) is not None
    ):
        return "provision_result_binding_invalid"
    runs = extraction.provider_runs
    if any(
        (run.provider_id, run.provider_version, run.model_id) != provider_binding
        for run in runs
    ):
        return "provision_result_binding_invalid"
    if any(
        (
            item.result.run.provider_id,
            item.result.run.provider_version,
            item.result.run.model_id,
        )
        != provider_binding
        for item in extraction.atomization_results
    ):
        return "provision_result_binding_invalid"

    if _extraction_has_unresolved(extraction):
        return _terminal_extraction_error(extraction)
    return None


def _local_extraction_binding_error(
    extraction: LlmRequirementExtractionResult,
) -> str | None:
    runs = extraction.provider_runs
    classification_runs = tuple(
        run for run in runs if run.operation == "classify_nodes"
    )
    assembly_runs = tuple(
        run for run in runs if run.operation == "assemble_requirements"
    )
    if (
        not classification_runs
        or not assembly_runs
        or runs != (*classification_runs, *assembly_runs)
        or len({run.provider_run_id for run in runs}) != len(runs)
        or len({(run.provider_id, run.provider_version, run.model_id) for run in runs})
        != 1
    ):
        return "provision_result_binding_invalid"
    classification_run_ids = {run.provider_run_id for run in classification_runs}
    annotation_run_ids = {
        annotation.provider_run_id for annotation in extraction.semantics.annotations
    }
    receipt_run_ids = {
        receipt.provider_run_id for receipt in extraction.semantic_terminal_receipts
    }
    if (
        extraction.semantics.classifier_id != classification_runs[0].provider_id
        or not annotation_run_ids.issubset(classification_run_ids)
        or receipt_run_ids != annotation_run_ids
        or tuple(
            receipt.semantic_unit_id
            for receipt in extraction.semantic_terminal_receipts
        )
        != extraction.semantics.scope_semantic_unit_ids
        or any(
            receipt.owner_structure_node_id != annotation.owner_structure_node_id
            or receipt.provider_run_id != annotation.provider_run_id
            for receipt, annotation in zip(
                extraction.semantic_terminal_receipts,
                extraction.semantics.annotations,
                strict=True,
            )
        )
        or any(
            annotation.candidate_source is not SemanticCandidateSource.MODEL
            or annotation.status
            not in {
                SemanticAnnotationStatus.CANDIDATE,
                SemanticAnnotationStatus.UNRESOLVED,
            }
            for annotation in extraction.semantics.annotations
        )
        or extraction.assembly.assembler_id != assembly_runs[0].provider_id
        or extraction.assembly.provider_run_id != assembly_runs[0].provider_run_id
        or extraction.assembly.source_ir_id != extraction.semantics.source_ir_id
        or extraction.assembly.structure_snapshot_id
        != extraction.semantics.structure_snapshot_id
        or extraction.assembly.semantic_snapshot_id
        != extraction.semantics.semantic_snapshot_id
        or extraction.requirement_view.source_ir_id != extraction.semantics.source_ir_id
        or extraction.requirement_view.structure_snapshot_id
        != extraction.semantics.structure_snapshot_id
        or extraction.requirement_view.semantic_snapshot_id
        != extraction.semantics.semantic_snapshot_id
        or extraction.requirement_view.requirement_set_id is not None
    ):
        return "provision_result_binding_invalid"
    return None


def _extraction_has_unresolved(
    extraction: LlmRequirementExtractionResult,
) -> bool:
    return (
        any(
            receipt.status.value == "UNRESOLVED"
            for receipt in extraction.semantic_terminal_receipts
        )
        or any(
            annotation.status is SemanticAnnotationStatus.UNRESOLVED
            or annotation.semantic_class is SemanticClass.UNRESOLVED
            for annotation in extraction.semantics.annotations
        )
        or any(
            item.status is RequirementAssemblyStatus.UNRESOLVED
            for item in extraction.assembly.items
        )
        or any(
            item.result.run.status is not SemanticProviderRunStatus.SUCCESS
            for item in extraction.atomization_results
        )
    )


def _expected_local_outcome_error(
    extraction: LlmRequirementExtractionResult,
    *,
    local_binding_error: str | None,
    unresolved: bool,
) -> str | None:
    if local_binding_error is not None:
        return local_binding_error
    if unresolved:
        return _terminal_extraction_error(extraction)
    return None


def _terminal_extraction_error(
    extraction: LlmRequirementExtractionResult,
) -> str:
    for receipt in extraction.semantic_terminal_receipts:
        if receipt.status.value == "UNRESOLVED" and receipt.reason_codes:
            return receipt.reason_codes[0]
    for item in extraction.assembly.items:
        if item.status is RequirementAssemblyStatus.UNRESOLVED and item.reason_codes:
            return item.reason_codes[0]
    for item in extraction.atomization_results:
        if item.result.run.error_code:
            return item.result.run.error_code
    return "provision_result_unresolved"


def _result_provider_binding(
    extraction: LlmRequirementExtractionResult | None,
) -> tuple[str, str, str] | None:
    if extraction is None or not extraction.provider_runs:
        return None
    first = extraction.provider_runs[0]
    return (first.provider_id, first.provider_version, first.model_id)


def _provider_binding(provider: SemanticProviderPort) -> tuple[str, str, str]:
    values = (
        provider.provider_id,
        provider.provider_version,
        provider.model_id,
    )
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError("provider identity values must be non-blank strings")
    return values


def _blocked_outcome(
    provision: _ProvisionScope,
    error_code: str,
    *,
    completed_provider_runs: tuple[SemanticProviderRunRecord, ...] = (),
) -> LlmProvisionExtractionOutcome:
    return LlmProvisionExtractionOutcome(
        root_section_structure_node_id=provision.root_section_structure_node_id,
        scope_structure_node_ids=provision.scope_structure_node_ids,
        source_order=provision.source_order,
        status=LlmProvisionExtractionStatus.BLOCKED_INPUT,
        extraction=None,
        error_code=error_code,
        completed_provider_runs=completed_provider_runs,
    )


def _ordered_view_node_refs(
    outcomes: tuple[LlmProvisionExtractionOutcome, ...],
) -> tuple[LlmRequirementViewNodeRef, ...]:
    provider_run_ids = tuple(
        run.provider_run_id
        for outcome in outcomes
        for run in (
            outcome.extraction.provider_runs
            if outcome.extraction is not None
            else outcome.completed_provider_runs
        )
    )
    if len(provider_run_ids) != len(set(provider_run_ids)):
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.DUPLICATE_PROVIDER_RUN
        )
    refs = tuple(
        LlmRequirementViewNodeRef(
            root_section_structure_node_id=(outcome.root_section_structure_node_id),
            requirement_view_snapshot_id=(
                outcome.extraction.requirement_view.requirement_view_snapshot_id
            ),
            requirement_view_node_id=node.requirement_view_node_id,
            semantic_unit_id=node.semantic_unit_id,
            owner_structure_node_id=node.owner_structure_node_id,
            source_order=node.source_order,
        )
        for outcome in outcomes
        if outcome.status is LlmProvisionExtractionStatus.SUCCESS
        and outcome.extraction is not None
        for node in outcome.extraction.requirement_view.nodes
    )
    targets = tuple(item.semantic_unit_id for item in refs)
    if len(targets) != len(set(targets)):
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.DUPLICATE_VIEW_TARGET
        )
    view_node_ids = tuple(item.requirement_view_node_id for item in refs)
    if len(view_node_ids) != len(set(view_node_ids)):
        raise LlmProvisionBatchRejected(
            LlmProvisionBatchRejectionCode.DUPLICATE_VIEW_NODE
        )
    return tuple(
        sorted(
            refs,
            key=lambda item: (
                item.source_order,
                item.semantic_unit_id,
                item.root_section_structure_node_id,
            ),
        )
    )


__all__ = [
    "LlmProvisionBatchRejected",
    "LlmProvisionBatchRejectionCode",
    "LlmProvisionExtractionOutcome",
    "LlmProvisionExtractionStatus",
    "LlmRequirementBatchResult",
    "LlmRequirementBatchStatus",
    "LlmRequirementDocumentCheckpoint",
    "LlmRequirementDocumentProgress",
    "LlmRequirementDocumentResult",
    "LlmRequirementViewNodeRef",
    "LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION",
    "MAX_LLM_DOCUMENT_CONCURRENT_CHUNKS",
    "MAX_LLM_DOCUMENT_CHUNK_IMAGE_PAGES",
    "MAX_LLM_DOCUMENT_CHUNK_NODES",
    "MAX_LLM_PROVISIONS_PER_BATCH",
    "extract_requirement_document_with_provider",
    "extract_requirement_provisions_with_provider",
    "llm_document_remote_chunk_lease",
]
