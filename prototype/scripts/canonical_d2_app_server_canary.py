#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROTOTYPE_ROOT.parent
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.adapters.agents.codex_app_server.canonical_document_session import (  # noqa: E402
    CanonicalD3RegionStageInputRequest,
    CanonicalD3StageInputRequest,
    CanonicalProductEvidenceRequest,
    CanonicalStagePredecessorBindingV1,
    prepare_canonical_app_server_document_session,
)
from app.adapters.agents.codex_app_server.canonical_dynamic_tools import (  # noqa: E402
    CanonicalD2StructureDynamicToolHost,
    TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS,
)
from app.adapters.agents.codex_app_server.canonical_stage_driver import (  # noqa: E402
    D2CanonicalStageDriver,
    D3CanonicalStageDriver,
    canonical_sha256,
)
from app.adapters.agents.codex_app_server.canonical_structure_v3_driver import (  # noqa: E402
    D2CanonicalV3StageDriver,
)
from app.adapters.agents.codex_app_server.canonical_stage_hosts import (  # noqa: E402
    CanonicalD2CandidateCompiler,
    CanonicalD2V3CandidateCompiler,
    CanonicalD3CandidateCompiler,
    CanonicalD3DecisionAttestor,
    CanonicalD3HostValidator,
    CanonicalParentProvisionObjectBatchCompiler,
)
from app.adapters.agents.codex_app_server.canonical_stage_transport import (  # noqa: E402
    LocalCodexAppServerStructuredStageTransport,
)
from app.adapters.agents.codex_app_server.lifecycle import (  # noqa: E402
    DEFAULT_PREFERRED_MODEL,
    LifecycleProbeError,
    _canary_runtime_state,
)
from app.adapters.agents.codex_app_server.parent_provision_object_driver import (  # noqa: E402
    PARENT_PROVISION_OBJECT_EXECUTION_MODE,
)
from app.adapters.agents.codex_app_server.parent_provision_object_workflow import (  # noqa: E402
    CanonicalParentProvisionObjectWorkflowOrchestrator,
)
from app.adapters.agents.codex_app_server.parent_provision_reconciliation_driver import (  # noqa: E402
    PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (  # noqa: E402
    build_synthetic_pdf,
)
from app.adapters.agents.document_evidence_host import (  # noqa: E402
    prepare_document_evidence,
)
from app.adapters.agents.d2_evidence_host import D2EvidenceToolHost  # noqa: E402
from app.adapters.agents.portable_kit import (  # noqa: E402
    compute_agent_kit_lock,
    load_portable_agent_kit,
)
from app.adapters.agents.stage_context import compile_agent_stage_context  # noqa: E402
from app.adapters.agents.structure_failure_pattern_host import (  # noqa: E402
    StructureFailurePatternHost,
)
from app.application.agent_workflow.d2_logical_structure import (  # noqa: E402
    D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION,
    D2_LOGICAL_STRUCTURE_VALIDATOR_ID,
    D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION,
    D2LogicalStructureProposalV1,
    D2SourceHandleKind,
    D2ValidatedD1ArtifactIdentityV1,
)
from app.application.agent_workflow.d2_decision_candidate import (  # noqa: E402
    D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_SCHEMA_VERSION,
)
from app.application.agent_workflow.d3_region_stage_input import (  # noqa: E402
    build_d3_region_stage_input_lineage,
    materialize_d3_region_stage_input,
)
from app.application.agent_workflow.d3_parent_provisions import (  # noqa: E402
    D3ExactSourceSegmentV1,
    compile_validated_d2_identity,
)
from app.application.agent_workflow.extraction_runtime import (  # noqa: E402
    AgentExtractionStage,
    AgentStageArtifactStatus,
)
from app.application.agent_workflow.logical_structure_repeatability import (  # noqa: E402
    build_logical_structure_repeatability_fingerprint,
    logical_structure_repeatability_safe_summary,
)
from app.application.agent_workflow.parent_provision_object_batches import (  # noqa: E402
    ParentProvisionObjectExecutionMode,
)
from app.application.agent_workflow.pds_region_boundary_oracle import (  # noqa: E402
    PDS_PRIVATE_REGION_BOUNDARY_SOURCE_SHA256,
    evaluate_pds_private_region_boundary_oracle,
)
from app.application.agent_workflow.region_parent_runtime_adapter import (  # noqa: E402
    RegionParentRuntimeAdapterV1,
    adapt_region_parent_runtime,
)
from app.application.extraction.agent_logical_table_scope import (  # noqa: E402
    AgentD1TableMapArtifactIdentityV1,
    source_ir_canonical_sha256,
)
from app.domain.extraction.source_ir import SourceIRSnapshot, SourceNodeKind  # noqa: E402


CANARY_SCHEMA_VERSION = "reqpilotx-canonical-d2-d3-app-server-canary/v1"


class ObjectParentFlowIncompleteError(LifecycleProbeError):
    def __init__(self, reason_code: str, summary: dict[str, object]) -> None:
        super().__init__(reason_code)
        self.summary = dict(summary)


def run_canonical_d2_app_server_canary(
    *,
    codex_bin: Path,
    codex_home: Path,
    repository_root: Path,
    timeout_seconds: float = 900.0,
    through_d3: bool = False,
    object_parent_flow: bool = False,
    region_object_parent_flow: bool = False,
    object_parent_plan_only: bool = False,
    source_path: Path | None = None,
    corpus_root: Path | None = None,
    document_role: str = "SYNTHETIC_TEST",
    max_stage_attempts: int = 2,
    dynamic_tools: bool = False,
    dynamic_tool_probe: bool = False,
) -> dict[str, object]:
    _validate_canary_modes(
        through_d3=through_d3,
        object_parent_flow=object_parent_flow,
        region_object_parent_flow=region_object_parent_flow,
        object_parent_plan_only=object_parent_plan_only,
    )
    if max_stage_attempts < 1 or max_stage_attempts > 3:
        raise LifecycleProbeError("STAGE_ATTEMPT_LIMIT_INVALID")
    if dynamic_tool_probe and not dynamic_tools:
        raise LifecycleProbeError("DYNAMIC_TOOL_PROBE_REQUIRES_DYNAMIC_TOOLS")
    host_diagnostic_signals = (
        ("cell_text_contains_following_heading",) if dynamic_tool_probe else ()
    )
    repository = repository_root.resolve(strict=True)
    with _canary_runtime_state(
        codex_bin=codex_bin,
        codex_home=codex_home,
        repository_root=repository,
    ) as base_runtime:
        candidate_kit_root = base_runtime.state_path / "candidate-agent-kit"
        shutil.copytree(repository / "agent-kit", candidate_kit_root)
        _activate_candidate_stages(
            candidate_kit_root,
            through_d3=through_d3,
            region_object_parent_flow=region_object_parent_flow,
        )
        kit = load_portable_agent_kit(candidate_kit_root)
        profile = kit.runner_profile("app-server-product")
        if profile.model_id != DEFAULT_PREFERRED_MODEL:
            raise LifecycleProbeError("MODEL_NOT_PINNED")

        if source_path is None:
            corpus = base_runtime.tmp_dir / "authorized-synthetic-corpus"
            corpus.mkdir(mode=0o700)
            source = corpus / "synthetic-d2.pdf"
            build_synthetic_pdf(source, include_prompt_injection=True)
        else:
            if corpus_root is None:
                raise LifecycleProbeError("AUTHORIZED_CORPUS_ROOT_REQUIRED")
            corpus = corpus_root.resolve(strict=True)
            source = source_path.resolve(strict=True)
            try:
                source.relative_to(corpus)
            except ValueError as exc:
                raise LifecycleProbeError("SOURCE_OUTSIDE_AUTHORIZED_CORPUS") from exc
            if source_path.is_symlink() or not source.is_file():
                raise LifecycleProbeError("SOURCE_FILE_INVALID")
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        run_id = "agent-run-" + uuid4().hex
        source_handle = "source-handle-" + uuid4().hex
        d1_context = compile_agent_stage_context(
            kit,
            AgentExtractionStage.DOCUMENT_UNDERSTANDING,
        )
        prepared = prepare_document_evidence(
            source,
            expected_source_sha256=source_sha256,
            document_role=document_role,
            kit_identity=kit.identity,
            stage_context_sha256=d1_context.context_sha256,
            run_id=run_id,
            opaque_source_handle=source_handle,
        )
        d1_status = (
            AgentStageArtifactStatus.VALIDATED
            if prepared.source_ir.status.value == "COMPLETE"
            else AgentStageArtifactStatus.PARTIAL
        )
        d1_artifact = D2ValidatedD1ArtifactIdentityV1(
            artifact_id="agent-artifact-" + uuid4().hex,
            artifact_sha256=canonical_sha256(
                {"run_id": run_id, "stage": "D1", "source": source_sha256}
            ),
            run_id=run_id,
            source_sha256=source_sha256,
            status=d1_status,
            proposal_sha256=canonical_sha256(
                {"run_id": run_id, "proposal": "synthetic-d1"}
            ),
            evidence_sha256=canonical_sha256(
                {"source_ir_id": prepared.source_ir.source_ir_id}
            ),
            validator_receipt_sha256=canonical_sha256(
                {"run_id": run_id, "receipt": "synthetic-d1"}
            ),
            page_count=prepared.manifest.page_count,
            kit_bundle_sha256=kit.identity.bundle_sha256,
        )
        table_map = AgentD1TableMapArtifactIdentityV1(
            source_sha256=source_sha256,
            artifact_id="d1-table-map-" + uuid4().hex,
            artifact_sha256=canonical_sha256(
                {
                    "source_sha256": source_sha256,
                    "table_ids": sorted(prepared.tables_by_id),
                }
            ),
        )
        predecessor = CanonicalStagePredecessorBindingV1(
            stage=AgentExtractionStage.LOGICAL_STRUCTURE,
            predecessor_stage=AgentExtractionStage.DOCUMENT_UNDERSTANDING,
            run_id=run_id,
            source_sha256=source_sha256,
            artifact_id=d1_artifact.artifact_id,
            artifact_sha256=d1_artifact.artifact_sha256,
            validator_receipt_sha256=d1_artifact.validator_receipt_sha256,
            proposal_schema_id=d1_artifact.proposal_schema_id,
            status=d1_artifact.status,
        )
        session = prepare_canonical_app_server_document_session(
            base_runtime=base_runtime,
            canonical_kit_root=candidate_kit_root,
            stages=(AgentExtractionStage.LOGICAL_STRUCTURE,),
            evidence_request=CanonicalProductEvidenceRequest(
                source_path=source,
                expected_source_sha256=source_sha256,
                document_role=document_role,
                authorized_corpus_root=corpus,
                run_id=run_id,
                opaque_source_handle=source_handle,
            ),
            stage_predecessors=(predecessor,),
            repository_root=repository,
        )
        try:
            dynamic_tool_host = (
                CanonicalD2StructureDynamicToolHost(
                    evidence_host=D2EvidenceToolHost(
                        source_ir=prepared.source_ir,
                        source_handle=source_handle,
                        kit_identity=kit.identity,
                        validated_d1_artifact=d1_artifact,
                        d1_table_map_artifact=table_map,
                    ),
                    failure_pattern_host=StructureFailurePatternHost(
                        candidate_kit_root
                    ),
                )
                if dynamic_tools
                else None
            )
            transport = LocalCodexAppServerStructuredStageTransport(
                timeout_seconds=timeout_seconds,
                dynamic_tool_host=dynamic_tool_host,
            )
            if region_object_parent_flow:
                compiler = CanonicalD2V3CandidateCompiler(
                    source_ir=prepared.source_ir,
                    expected_d1_artifact=d1_artifact,
                    expected_d1_table_map_artifact=table_map,
                    kit=kit,
                )
                d2_driver = D2CanonicalV3StageDriver(
                    transport=transport,
                    candidate_compiler=compiler,
                    dynamic_tool_specs=(
                        dynamic_tool_host.specs if dynamic_tool_host is not None else ()
                    ),
                    host_diagnostic_signals=host_diagnostic_signals,
                )
                d2_retry_stage = "D2_V3"
            else:
                compiler = CanonicalD2CandidateCompiler(
                    source_ir=prepared.source_ir,
                    expected_d1_artifact=d1_artifact,
                    expected_d1_table_map_artifact=table_map,
                    kit=kit,
                )
                d2_driver = D2CanonicalStageDriver(
                    transport=transport,
                    candidate_compiler=compiler,
                    dynamic_tool_specs=(
                        dynamic_tool_host.specs if dynamic_tool_host is not None else ()
                    ),
                    host_diagnostic_signals=host_diagnostic_signals,
                )
                d2_retry_stage = "D2"
            d2_task_binding = canonical_sha256(
                {
                    "run_id": run_id,
                    "source_sha256": source_sha256,
                    "d1_artifact_sha256": d1_artifact.artifact_sha256,
                    "table_map_sha256": table_map.artifact_sha256,
                    "host_diagnostic_signals": list(host_diagnostic_signals),
                }
            )
            d2_attempt_count = 0
            while True:
                d2_attempt_count += 1
                try:
                    d2_result = d2_driver.run(
                        session,
                        task_binding_sha256=d2_task_binding,
                    )
                    break
                except LifecycleProbeError as exc:
                    if d2_attempt_count < max_stage_attempts and (
                        _is_retryable_candidate_error(d2_retry_stage, exc.reason_code)
                    ):
                        continue
                    prefix = "D2_REGION" if region_object_parent_flow else "D2"
                    raise LifecycleProbeError(f"{prefix}_{exc.reason_code}") from exc
            _validate_dynamic_tool_probe_activity(
                dynamic_tool_probe=dynamic_tool_probe,
                tool_activity_summary=d2_result.tool_activity_summary,
            )
            native_policy = session.attest().native_runtime_policy
            if native_policy is None:
                raise LifecycleProbeError("CANONICAL_NATIVE_POLICY_MISSING")
            protocol = dict(session.runtime.protocol)
        finally:
            session.close()

        receipt: dict[str, object] = {
            "schema_version": CANARY_SCHEMA_VERSION,
            "status": "PASS",
            "model_id": profile.model_id,
            "reasoning_effort": profile.reasoning_effort,
            "kit_version": kit.identity.kit_version,
            "kit_bundle_sha256": kit.identity.bundle_sha256,
            "protocol": protocol,
            "native_confinement_mode": native_policy.confinement_mode,
            "source_ir_status": prepared.source_ir.status.value,
            "page_count": prepared.manifest.page_count,
            "physical_table_count": len(prepared.tables_by_id),
            "pipeline_mode": (
                "REGION_OBJECT_PARENT_FLOW"
                if region_object_parent_flow
                else "LEGACY_CANONICAL"
            ),
            "d2": (
                _d2_region_summary(d2_result)
                if region_object_parent_flow
                else _d2_summary(d2_result)
            ),
            "d2_attempt_count": d2_attempt_count,
            "dynamic_tools_enabled": dynamic_tools,
            "dynamic_tool_probe": dynamic_tool_probe,
            "host_diagnostic_signals": list(host_diagnostic_signals),
            "dynamic_tool_activity": dict(d2_result.tool_activity_summary),
            "run_temp_cleanup": False,
        }
        if region_object_parent_flow and source_path is None:
            d2_summary = receipt["d2"]
            assert isinstance(d2_summary, dict)
            d2_summary["synthetic_structure_oracle"] = (
                _synthetic_region_structure_oracle(d2_summary)
            )
        if not through_d3:
            return receipt

        if region_object_parent_flow:
            return _run_region_object_parent_canary(
                base_runtime=base_runtime,
                candidate_kit_root=candidate_kit_root,
                repository=repository,
                source=source,
                corpus=corpus,
                document_role=document_role,
                source_handle=source_handle,
                prepared=prepared,
                kit=kit,
                d2_result=d2_result,
                receipt=receipt,
                run_id=run_id,
                source_sha256=source_sha256,
                timeout_seconds=timeout_seconds,
                max_stage_attempts=max_stage_attempts,
                plan_only=object_parent_plan_only,
            )

        source_segments = _resolve_d3_source_segments(
            d2_result.proposal,
            source_ir=prepared.source_ir,
        )
        d2_identity = compile_validated_d2_identity(
            d2_proposal=d2_result.proposal,
            d2_receipt=d2_result.host_validation_receipt,
            source_segments=source_segments,
        )
        d3_predecessor = CanonicalStagePredecessorBindingV1(
            stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
            predecessor_stage=AgentExtractionStage.LOGICAL_STRUCTURE,
            run_id=run_id,
            source_sha256=source_sha256,
            artifact_id=d2_result.stage_artifact.artifact_id,
            artifact_sha256=d2_result.stage_artifact.payload_sha256,
            validator_receipt_sha256=(
                d2_result.stage_artifact.validator_receipt_sha256
            ),
            proposal_schema_id=d2_result.stage_artifact.proposal_schema_id,
            status=d2_result.stage_artifact.status,
        )
        d3_session = prepare_canonical_app_server_document_session(
            base_runtime=base_runtime,
            canonical_kit_root=candidate_kit_root,
            stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
            evidence_request=CanonicalProductEvidenceRequest(
                source_path=source,
                expected_source_sha256=source_sha256,
                document_role=document_role,
                authorized_corpus_root=corpus,
                run_id=run_id,
                opaque_source_handle=source_handle,
            ),
            stage_predecessors=(d3_predecessor,),
            repository_root=repository,
            d3_stage_input_request=CanonicalD3StageInputRequest(
                d2_proposal=d2_result.proposal,
                d2_validation_receipt=d2_result.host_validation_receipt,
                source_segments=source_segments,
            ),
        )
        try:
            if object_parent_flow:
                try:
                    receipt["d3"] = _run_object_parent_flow(
                        d3_session=d3_session,
                        kit=kit,
                        d2_result=d2_result,
                        d2_identity=d2_identity,
                        source_segments=source_segments,
                        run_id=run_id,
                        source_sha256=source_sha256,
                        timeout_seconds=timeout_seconds,
                        max_stage_attempts=max_stage_attempts,
                        plan_only=object_parent_plan_only,
                    )
                except LifecycleProbeError:
                    raise
                except Exception as exc:
                    raise LifecycleProbeError("D3_OBJECT_PARENT_FLOW_FAILED") from exc
                if object_parent_plan_only:
                    receipt["status"] = "PLAN_ONLY"
                return receipt
            d3_driver = D3CanonicalStageDriver(
                transport=LocalCodexAppServerStructuredStageTransport(
                    timeout_seconds=timeout_seconds,
                ),
                candidate_compiler=CanonicalD3CandidateCompiler(
                    d2_identity=d2_identity,
                    source_segments=source_segments,
                    kit=kit,
                ),
                decision_attestor=CanonicalD3DecisionAttestor(kit=kit),
                host_validator=CanonicalD3HostValidator(
                    d2_identity=d2_identity,
                    source_segments=source_segments,
                ),
            )
            d3_task_binding = canonical_sha256(
                {
                    "run_id": run_id,
                    "source_sha256": source_sha256,
                    "d2_artifact_sha256": d2_result.stage_artifact.payload_sha256,
                    "source_scope_sha256": d2_identity.source_scope_sha256,
                }
            )
            d3_attempt_count = 0
            while True:
                d3_attempt_count += 1
                try:
                    d3_result = d3_driver.run(
                        d3_session,
                        task_binding_sha256=d3_task_binding,
                    )
                    break
                except LifecycleProbeError as exc:
                    if d3_attempt_count < max_stage_attempts and (
                        _is_retryable_candidate_error("D3", exc.reason_code)
                    ):
                        continue
                    raise LifecycleProbeError(f"D3_{exc.reason_code}") from exc
            receipt["d3"] = {
                "attempt_count": d3_attempt_count,
                "artifact_status": d3_result.stage_artifact.status.value,
                "artifact_id": d3_result.stage_artifact.artifact_id,
                "artifact_sha256": d3_result.stage_artifact.payload_sha256,
                "parent_provision_count": len(d3_result.proposal.entries),
                "declared_scope_count": len(
                    d3_result.proposal.declared_logical_object_ids
                ),
                "unresolved_scope_count": len(
                    d3_result.proposal.unresolved_logical_object_ids
                ),
                "validation_receipt_sha256": (
                    d3_result.host_validation_receipt.receipt_sha256
                ),
                "platform_execution_receipt_sha256": (
                    d3_result.execution_receipt.receipt_sha256
                ),
                "professional_policy_attested": (
                    d3_result.professional_policy_attestation.policy_attested
                ),
                "requirementness_counts": dict(
                    sorted(
                        Counter(
                            item.requirementness.value
                            for item in d3_result.proposal.entries
                        ).items()
                    )
                ),
                "content_role_counts": dict(
                    sorted(
                        Counter(
                            item.content_role.value
                            for item in d3_result.proposal.entries
                        ).items()
                    )
                ),
                "scope_profiles": _d3_scope_profiles(
                    d2_result.proposal,
                    declared_ids=set(d3_result.proposal.declared_logical_object_ids),
                    unresolved_ids=set(
                        d3_result.proposal.unresolved_logical_object_ids
                    ),
                ),
            }
            return receipt
        finally:
            d3_session.close()


def _d2_summary(result) -> dict[str, object]:  # type: ignore[no-untyped-def]
    repeatability_fingerprint = build_logical_structure_repeatability_fingerprint(
        result.proposal,
        result.host_validation_receipt,
    )
    return {
        "logical_object_count": len(result.proposal.logical_objects),
        "logical_table_scope_count": len(result.proposal.logical_table_scopes),
        "source_disposition_count": len(result.proposal.source_evidence_dispositions),
        "physical_table_disposition_count": len(
            result.proposal.physical_table_dispositions
        ),
        "artifact_status": result.stage_artifact.status.value,
        "artifact_id": result.stage_artifact.artifact_id,
        "artifact_sha256": result.stage_artifact.payload_sha256,
        "validation_receipt_sha256": (result.host_validation_receipt.receipt_sha256),
        "platform_execution_receipt_sha256": (result.execution_receipt.receipt_sha256),
        "repeatability_fingerprint": logical_structure_repeatability_safe_summary(
            repeatability_fingerprint
        ),
        "logical_object_kind_counts": dict(
            sorted(
                Counter(
                    item.object_kind.value for item in result.proposal.logical_objects
                ).items()
            )
        ),
        "unresolved_source_disposition_count": sum(
            item.disposition.value == "UNRESOLVED"
            for item in result.proposal.source_evidence_dispositions
        ),
        "unresolved_table_disposition_count": sum(
            item.disposition.value == "UNRESOLVED"
            for item in result.proposal.physical_table_dispositions
        ),
    }


def _d2_region_summary(result) -> dict[str, object]:  # type: ignore[no-untyped-def]
    proposal = result.proposal
    receipt = result.host_validation_receipt
    profiles = _region_structure_profiles(proposal)
    summary = {
        "mode": "REGION_AWARE_V3_TO_PROPOSAL_V2",
        "logical_object_count": len(proposal.logical_objects),
        "table_region_count": len(proposal.table_region_dispositions),
        "physical_table_evidence_count": len(proposal.physical_table_evidence),
        "base_table_replacement_count": len(proposal.base_table_replacements),
        "nested_region_count": len(proposal.nested_region_ids),
        "false_region_count": len(proposal.false_region_ids),
        "unresolved_region_count": len(proposal.unresolved_region_ids),
        "unresolved_empty_table_count": len(proposal.unresolved_empty_table_ids),
        "unresolved_supplemental_evidence_count": len(
            proposal.unresolved_supplemental_evidence_ids
        ),
        "artifact_status": result.stage_artifact.status.value,
        "artifact_id": result.stage_artifact.artifact_id,
        "artifact_sha256": result.stage_artifact.payload_sha256,
        "validation_receipt_sha256": receipt.receipt_sha256,
        "platform_execution_receipt_sha256": (result.execution_receipt.receipt_sha256),
        "logical_object_kind_counts": dict(
            sorted(
                Counter(
                    item.object_kind.value for item in proposal.logical_objects
                ).items()
            )
        ),
        "logical_object_origin_counts": dict(
            sorted(
                Counter(item.origin.value for item in proposal.logical_objects).items()
            )
        ),
        "region_disposition_counts": dict(
            sorted(
                Counter(
                    item.disposition.value
                    for item in proposal.table_region_dispositions
                ).items()
            )
        ),
        "logical_object_profiles": profiles["objects"],
        "table_profiles": profiles["physical_tables"],
        "region_profiles": profiles["regions"],
        "repeatability_fingerprint": _region_structure_repeatability_fingerprint(
            proposal,
            profiles=profiles,
        ),
    }
    if proposal.source_sha256 == PDS_PRIVATE_REGION_BOUNDARY_SOURCE_SHA256:
        private_oracle = evaluate_pds_private_region_boundary_oracle(
            result.compilation.table_region_artifact
        )
        if private_oracle is not None:
            summary["private_region_boundary_oracle"] = private_oracle
    return summary


def _region_structure_profiles(proposal) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """Return source-ordered profiles without source IDs, text, or paths."""

    object_ordinal = {
        item.logical_object_id: index
        for index, item in enumerate(proposal.logical_objects)
    }
    physical_tables = tuple(
        sorted(
            proposal.physical_table_evidence,
            key=lambda item: item.physical_table_source_order,
        )
    )
    table_ordinal = {
        item.physical_table_id: index for index, item in enumerate(physical_tables)
    }
    region_ordinal: dict[str, tuple[int, int]] = {}
    for table in physical_tables:
        for local_ordinal, region_id in enumerate(table.ordered_region_ids):
            region_ordinal[region_id] = (
                table_ordinal[table.physical_table_id],
                local_ordinal,
            )
    return {
        "algorithm_version": "region-source-ordered-content-free-profile/v2",
        "status": proposal.status.value,
        "objects": [
            {
                "ordinal": index,
                "origin": item.origin.value,
                "kind": item.object_kind.value,
                "parent_ordinal": (
                    object_ordinal[item.parent_logical_object_id]
                    if item.parent_logical_object_id is not None
                    else None
                ),
                "source_order": item.source_order,
                "pages": list(item.page_numbers),
                "segment_count": len(item.source_segments),
                "segment_kind_runs": _segment_kind_runs(item.source_segments),
                "segment_kind_counts": dict(
                    sorted(
                        Counter(
                            segment.segment_kind.value
                            for segment in item.source_segments
                        ).items()
                    )
                ),
                "nested_evidence_count": len(item.nested_evidence_region_ids),
                "supplemental_evidence_count": len(item.supplemental_evidence_ids),
            }
            for index, item in enumerate(proposal.logical_objects)
        ],
        "regions": [
            {
                "table_ordinal": region_ordinal[item.region_id][0],
                "region_ordinal": region_ordinal[item.region_id][1],
                "disposition": item.disposition.value,
                "owner_object_ordinal": (
                    object_ordinal[item.logical_object_id]
                    if item.logical_object_id is not None
                    else None
                ),
                "nested_owner_position": (
                    list(region_ordinal[item.nested_owner_region_id])
                    if item.nested_owner_region_id is not None
                    else None
                ),
            }
            for item in proposal.table_region_dispositions
        ],
        "physical_tables": [
            {
                "ordinal": index,
                "source_order": item.physical_table_source_order,
                "region_count": len(item.ordered_region_ids),
                "empty_table_disposition": (
                    item.empty_table_disposition.value
                    if item.empty_table_disposition is not None
                    else None
                ),
                "supplemental_dispositions": [
                    value.disposition.value for value in item.supplemental_dispositions
                ],
            }
            for index, item in enumerate(physical_tables)
        ],
        "base_table_replacements": [
            {
                "physical_table_ordinals": [
                    table_ordinal[table_id] for table_id in item.physical_table_ids
                ],
                "disposition": item.disposition.value,
                "region_count": len(item.region_ids),
                "replacement_object_count": len(item.replacement_logical_object_ids),
                "nested_region_count": len(item.nested_region_ids),
                "false_region_count": len(item.false_region_ids),
                "unresolved_region_count": len(item.unresolved_region_ids),
                "empty_table_count": len(item.empty_physical_table_ids),
            }
            for item in proposal.base_table_replacements
        ],
        "unresolved": {
            "code_count": len(proposal.unresolved_codes),
            "region_count": len(proposal.unresolved_region_ids),
            "empty_table_count": len(proposal.unresolved_empty_table_ids),
            "supplemental_count": len(proposal.unresolved_supplemental_evidence_ids),
        },
    }


def _region_structure_repeatability_fingerprint(
    proposal,  # type: ignore[no-untyped-def]
    *,
    profiles: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a source-text-free, path-free topology fingerprint for ProposalV2."""

    profile = profiles or _region_structure_profiles(proposal)
    return {
        "schema_version": "logical-structure-region-repeatability-fingerprint/v2",
        "algorithm_version": profile["algorithm_version"],
        "fingerprint_sha256": canonical_sha256(profile),
        "proposal_status": proposal.status.value,
        "logical_object_count": len(proposal.logical_objects),
        "table_region_count": len(proposal.table_region_dispositions),
        "physical_table_count": len(proposal.physical_table_evidence),
    }


def _segment_kind_runs(segments) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    runs: list[dict[str, object]] = []
    for segment in segments:
        kind = segment.segment_kind.value
        if runs and runs[-1]["kind"] == kind:
            runs[-1]["count"] = int(runs[-1]["count"]) + 1
        else:
            runs.append({"kind": kind, "count": 1})
    return runs


def _compare_region_structure_summaries(
    first: dict[str, object],
    second: dict[str, object],
) -> dict[str, object]:
    """Compare two safe summaries without copying their profile content."""

    first_objects = tuple(first.get("logical_object_profiles", ()))
    second_objects = tuple(second.get("logical_object_profiles", ()))
    first_tables = tuple(first.get("table_profiles", ()))
    second_tables = tuple(second.get("table_profiles", ()))
    first_regions = tuple(first.get("region_profiles", ()))
    second_regions = tuple(second.get("region_profiles", ()))
    object_profiles_match = first_objects == second_objects
    table_profiles_match = first_tables == second_tables
    region_profiles_match = first_regions == second_regions
    status_match = (
        first.get("artifact_status") == second.get("artifact_status")
        and first.get("unresolved_region_count")
        == second.get("unresolved_region_count")
        and first.get("unresolved_empty_table_count")
        == second.get("unresolved_empty_table_count")
        and first.get("unresolved_supplemental_evidence_count")
        == second.get("unresolved_supplemental_evidence_count")
    )
    reason_codes: list[str] = []
    if len(first_objects) != len(second_objects):
        reason_codes.append("LOGICAL_OBJECT_COUNT_DRIFT")
    if not object_profiles_match:
        reason_codes.append("LOGICAL_OBJECT_PROFILE_DRIFT")
    if not table_profiles_match:
        reason_codes.append("TABLE_PROFILE_DRIFT")
    if not region_profiles_match:
        reason_codes.append("REGION_PROFILE_DRIFT")
    if not status_match:
        reason_codes.append("STATUS_PROFILE_DRIFT")
    table_region_match = table_profiles_match and region_profiles_match
    overall_match = object_profiles_match and table_region_match and status_match
    return {
        "schema_version": "logical-structure-region-repeatability-comparison/v1",
        "verdict": "PASS" if overall_match else "FAIL",
        "logical_object_verdict": "PASS" if object_profiles_match else "FAIL",
        "table_region_verdict": "PASS" if table_region_match else "FAIL",
        "status_verdict": "PASS" if status_match else "FAIL",
        "first_logical_object_count": len(first_objects),
        "second_logical_object_count": len(second_objects),
        "logical_object_count_delta": len(second_objects) - len(first_objects),
        "first_table_count": len(first_tables),
        "second_table_count": len(second_tables),
        "first_region_count": len(first_regions),
        "second_region_count": len(second_regions),
        "reason_codes": tuple(sorted(reason_codes)),
    }


def _synthetic_region_structure_oracle(
    summary: dict[str, object],
) -> dict[str, object]:
    """Evaluate the fixed synthetic fixture after inference, never guide it."""

    objects = tuple(summary.get("logical_object_profiles", ()))
    tables = tuple(summary.get("table_profiles", ()))
    regions = tuple(summary.get("region_profiles", ()))
    reasons: list[str] = []
    if len(objects) != 9:
        reasons.append("SYNTHETIC_OBJECT_COUNT_MISMATCH")
    by_ordinal = {
        item.get("ordinal"): item for item in objects if isinstance(item, dict)
    }
    if len(by_ordinal) != len(objects):
        reasons.append("SYNTHETIC_OBJECT_ORDINAL_DRIFT")

    def parent_source_order(item: dict[str, object]) -> int | None:
        parent = by_ordinal.get(item.get("parent_ordinal"))
        return parent.get("source_order") if isinstance(parent, dict) else None

    document = tuple(
        item
        for item in objects
        if isinstance(item, dict) and item.get("kind") == "DOCUMENT_UNIT"
    )
    sections = tuple(
        item
        for item in objects
        if isinstance(item, dict) and item.get("kind") == "SECTION"
    )
    table_objects = tuple(
        item
        for item in objects
        if isinstance(item, dict) and item.get("kind") == "TABLE"
    )
    if (
        len(document) != 1
        or document[0].get("source_order") != 0
        or document[0].get("parent_ordinal") is not None
    ):
        reasons.append("SYNTHETIC_DOCUMENT_UNIT_DRIFT")
    native_sections = tuple(item for item in sections if item.get("pages") == [1])
    if tuple(item.get("source_order") for item in native_sections) != (
        1,
        2,
        4,
        5,
        7,
    ):
        reasons.append("SYNTHETIC_NUMBERED_HEADING_BOUNDARY_DRIFT")
    if any(item.get("source_order") == 9 for item in sections):
        reasons.append("SYNTHETIC_TABLE_CAPTION_PROMOTED_TO_SECTION")
    expected_native_parents = {1: 0, 2: 1, 4: 0, 5: 4, 7: 4}
    if any(
        parent_source_order(item)
        != expected_native_parents.get(item.get("source_order"))
        for item in native_sections
    ):
        reasons.append("SYNTHETIC_NUMBERED_HEADING_PARENT_DRIFT")
    visual_sections = tuple(item for item in sections if item.get("pages") == [2])
    empty_visual = tuple(
        item for item in visual_sections if item.get("segment_count") == 0
    )
    bound_visual = tuple(
        item for item in visual_sections if item.get("segment_count") == 1
    )
    if (
        len(visual_sections) != 2
        or len(empty_visual) != 1
        or len(bound_visual) != 1
        or empty_visual[0].get("source_order") != 15
        or bound_visual[0].get("source_order") != 15
        or parent_source_order(empty_visual[0]) != 0
        or bound_visual[0].get("parent_ordinal") != empty_visual[0].get("ordinal")
    ):
        reasons.append("SYNTHETIC_VISUAL_HEADING_BOUNDARY_DRIFT")
    if (
        len(table_objects) != 1
        or table_objects[0].get("source_order") != 14
        or parent_source_order(table_objects[0]) != 7
        or table_objects[0].get("segment_kind_counts") != {"TABLE_REGION": 1}
    ):
        reasons.append("SYNTHETIC_TABLE_OWNERSHIP_DRIFT")
    if (
        len(tables) != 1
        or not isinstance(tables[0], dict)
        or tables[0].get("source_order") != 14
        or tables[0].get("region_count") != 1
        or len(regions) != 1
        or not isinstance(regions[0], dict)
        or regions[0].get("disposition") != "LOGICAL_SCOPE_PART"
    ):
        reasons.append("SYNTHETIC_TABLE_REGION_DRIFT")
    if summary.get("artifact_status") != "PARTIAL":
        reasons.append("SYNTHETIC_VISUAL_UNRESOLVED_STATUS_DRIFT")
    return {
        "schema_version": "reqpilotx-synthetic-region-structure-oracle/v1",
        "verdict": "PASS" if not reasons else "FAIL",
        "expected_logical_object_count": 9,
        "observed_logical_object_count": len(objects),
        "reason_codes": tuple(sorted(reasons)),
    }


def _run_region_object_parent_canary(
    *,
    base_runtime,  # type: ignore[no-untyped-def]
    candidate_kit_root: Path,
    repository: Path,
    source: Path,
    corpus: Path,
    document_role: str,
    source_handle: str,
    prepared,  # type: ignore[no-untyped-def]
    kit,  # type: ignore[no-untyped-def]
    d2_result,  # type: ignore[no-untyped-def]
    receipt: dict[str, object],
    run_id: str,
    source_sha256: str,
    timeout_seconds: float,
    max_stage_attempts: int,
    plan_only: bool,
) -> dict[str, object]:
    region_request = _build_region_stage_input_request(
        source_ir=prepared.source_ir,
        d2_result=d2_result,
    )
    predecessor = CanonicalStagePredecessorBindingV1(
        stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        predecessor_stage=AgentExtractionStage.LOGICAL_STRUCTURE,
        run_id=run_id,
        source_sha256=source_sha256,
        artifact_id=d2_result.stage_artifact.artifact_id,
        artifact_sha256=d2_result.stage_artifact.payload_sha256,
        validator_receipt_sha256=(d2_result.stage_artifact.validator_receipt_sha256),
        proposal_schema_id=d2_result.stage_artifact.proposal_schema_id,
        status=d2_result.stage_artifact.status,
    )
    d3_session = prepare_canonical_app_server_document_session(
        base_runtime=base_runtime,
        canonical_kit_root=candidate_kit_root,
        stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
        evidence_request=CanonicalProductEvidenceRequest(
            source_path=source,
            expected_source_sha256=source_sha256,
            document_role=document_role,
            authorized_corpus_root=corpus,
            run_id=run_id,
            opaque_source_handle=source_handle,
        ),
        stage_predecessors=(predecessor,),
        repository_root=repository,
        d3_region_stage_input_request=region_request,
    )
    try:
        workspace = d3_session.d3_region_stage_input
        if workspace is None:
            raise LifecycleProbeError("D3_REGION_STAGE_INPUT_WORKSPACE_MISSING")
        runtime = _adapt_region_parent_runtime_for_canary(
            request=region_request,
            workspace_attestation=workspace.attestation,
            expected_kit_identity=kit.identity,
        )
        receipt["d3_region_input"] = _region_parent_runtime_safe_summary(
            runtime,
            workspace_bundle_sha256=workspace.attestation.bundle_sha256,
        )
        receipt["d3"] = _run_region_object_parent_flow(
            d3_session=d3_session,
            kit=kit,
            d2_result=d2_result,
            runtime=runtime,
            stage_input_bundle_sha256=workspace.attestation.bundle_sha256,
            run_id=run_id,
            source_sha256=source_sha256,
            timeout_seconds=timeout_seconds,
            max_stage_attempts=max_stage_attempts,
            plan_only=plan_only,
        )
        if plan_only:
            receipt["status"] = "PLAN_ONLY"
        return receipt
    finally:
        d3_session.close()


def _build_region_stage_input_request(
    *,
    source_ir: SourceIRSnapshot,
    d2_result,  # type: ignore[no-untyped-def]
) -> CanonicalD3RegionStageInputRequest:
    lineage = build_d3_region_stage_input_lineage(
        proposal=d2_result.proposal,
        validation_receipt=d2_result.host_validation_receipt,
        parent_input=d2_result.compilation.region_parent_input.stage_input,
        parent_input_receipt=d2_result.compilation.region_parent_input.receipt,
    )
    return CanonicalD3RegionStageInputRequest(
        source_ir=source_ir,
        d2_proposal=d2_result.proposal,
        d2_validation_receipt=d2_result.host_validation_receipt,
        parent_input=d2_result.compilation.region_parent_input.stage_input,
        parent_input_receipt=d2_result.compilation.region_parent_input.receipt,
        expected_lineage=lineage,
    )


def _adapt_region_parent_runtime_for_canary(
    *,
    request: CanonicalD3RegionStageInputRequest,
    workspace_attestation,  # type: ignore[no-untyped-def]
    expected_kit_identity,  # type: ignore[no-untyped-def]
) -> RegionParentRuntimeAdapterV1:
    stage_input = materialize_d3_region_stage_input(
        request.source_ir,
        proposal=request.d2_proposal,
        validation_receipt=request.d2_validation_receipt,
        parent_input=request.parent_input,
        parent_input_receipt=request.parent_input_receipt,
        expected_lineage=request.expected_lineage,
    )
    if stage_input.manifest != workspace_attestation.stage_input_manifest:
        raise LifecycleProbeError("D3_REGION_STAGE_INPUT_WORKSPACE_REPLAY_DRIFT")
    return adapt_region_parent_runtime(
        proposal=request.d2_proposal,
        validation_receipt=request.d2_validation_receipt,
        stage_input=stage_input,
        expected_kit_identity=expected_kit_identity,
        expected_source_sha256=request.d2_proposal.source_sha256,
        expected_source_ir_canonical_sha256=source_ir_canonical_sha256(
            request.source_ir
        ),
        expected_stage_input_manifest_sha256=(
            workspace_attestation.stage_input_manifest.manifest_sha256
        ),
        expected_workspace_bundle_sha256=workspace_attestation.bundle_sha256,
    )


def _region_parent_runtime_safe_summary(
    runtime: RegionParentRuntimeAdapterV1,
    *,
    workspace_bundle_sha256: str,
) -> dict[str, object]:
    return {
        "mode": "REGION_STAGE_INPUT_V1",
        "stage_input_status": runtime.stage_input_status.value,
        "stage_input_manifest_id": runtime.stage_input_manifest_id,
        "stage_input_manifest_sha256": runtime.stage_input_manifest_sha256,
        "workspace_bundle_sha256": workspace_bundle_sha256,
        "runtime_adapter_sha256": runtime.adapter_sha256,
        "logical_object_count": len(runtime.logical_objects),
        "exact_source_segment_count": len(runtime.source_segments),
        "evidence_ledger_count": len(runtime.evidence_ledger),
        "nested_evidence_count": len(runtime.nested_evidence_ledger),
        "selected_parent_assignment_count": len(
            runtime.selected_parent_assignment_ledger
        ),
        "unresolved_scope_count": len(runtime.unresolved_scopes),
        "source_scope_sha256": runtime.d2_identity.source_scope_sha256,
    }


def _validate_canary_modes(
    *,
    through_d3: bool,
    object_parent_flow: bool,
    region_object_parent_flow: bool = False,
    object_parent_plan_only: bool = False,
) -> None:
    if object_parent_flow and not through_d3:
        raise LifecycleProbeError("OBJECT_PARENT_FLOW_REQUIRES_THROUGH_D3")
    if object_parent_plan_only and not object_parent_flow:
        if not region_object_parent_flow:
            raise LifecycleProbeError("OBJECT_PARENT_PLAN_ONLY_REQUIRES_OBJECT_FLOW")
    if region_object_parent_flow and not through_d3:
        raise LifecycleProbeError("REGION_OBJECT_PARENT_FLOW_REQUIRES_THROUGH_D3")
    if region_object_parent_flow and object_parent_flow:
        raise LifecycleProbeError("OBJECT_PARENT_FLOW_MODES_ARE_MUTUALLY_EXCLUSIVE")


def _validate_dynamic_tool_probe_activity(
    *,
    dynamic_tool_probe: bool,
    tool_activity_summary: object,
) -> None:
    if not dynamic_tool_probe:
        return
    if not isinstance(tool_activity_summary, dict):
        raise LifecycleProbeError("DYNAMIC_TOOL_PROBE_NOT_OBSERVED")
    dynamic_summary = tool_activity_summary.get("dynamic_tools")
    if not isinstance(dynamic_summary, dict):
        raise LifecycleProbeError("DYNAMIC_TOOL_PROBE_NOT_OBSERVED")
    expected_counts = {TOOL_STRUCTURE_SELECT_FAILURE_PATTERNS: 1}
    if (
        dynamic_summary.get("enabled") is not True
        or dynamic_summary.get("call_count") != 1
        or dynamic_summary.get("success_count") != 1
        or dynamic_summary.get("tool_call_counts") != expected_counts
    ):
        raise LifecycleProbeError("DYNAMIC_TOOL_PROBE_NOT_OBSERVED")


def _object_parent_flow_task_bindings(
    *,
    run_id: str,
    source_sha256: str,
    d2_artifact_sha256: str,
    source_scope_sha256: str,
    stage_input_bundle_sha256: str,
    plan_id: str,
    plan_sha256: str,
) -> tuple[str, str]:
    common = {
        "run_id": run_id,
        "source_sha256": source_sha256,
        "d2_artifact_sha256": d2_artifact_sha256,
        "source_scope_sha256": source_scope_sha256,
        "stage_input_bundle_sha256": stage_input_bundle_sha256,
        "plan_id": plan_id,
        "plan_sha256": plan_sha256,
    }
    object_binding = canonical_sha256(
        {
            **common,
            "execution_mode": PARENT_PROVISION_OBJECT_EXECUTION_MODE,
        }
    )
    reconciliation_binding = canonical_sha256(
        {
            **common,
            "execution_mode": (PARENT_PROVISION_OBJECT_RECONCILIATION_EXECUTION_MODE),
            "object_task_binding_sha256": object_binding,
        }
    )
    return object_binding, reconciliation_binding


def _run_object_parent_flow(
    *,
    d3_session,  # type: ignore[no-untyped-def]
    kit,  # type: ignore[no-untyped-def]
    d2_result,  # type: ignore[no-untyped-def]
    d2_identity,  # type: ignore[no-untyped-def]
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    run_id: str,
    source_sha256: str,
    timeout_seconds: float,
    max_stage_attempts: int,
    plan_only: bool = False,
) -> dict[str, object]:
    stage_input = d3_session.d3_stage_input
    if stage_input is None:
        raise LifecycleProbeError("D3_OBJECT_PARENT_STAGE_INPUT_MISSING")
    compiler = CanonicalParentProvisionObjectBatchCompiler(
        d2_identity=d2_identity,
        logical_objects=tuple(d2_result.proposal.logical_objects),
        source_segments=source_segments,
        stage_input_bundle_sha256=stage_input.attestation.bundle_sha256,
        kit_identity=kit.identity,
    )
    plan = compiler.build_plan()
    if plan_only:
        return _object_parent_plan_summary(plan)
    object_binding, reconciliation_binding = _object_parent_flow_task_bindings(
        run_id=run_id,
        source_sha256=source_sha256,
        d2_artifact_sha256=d2_result.stage_artifact.payload_sha256,
        source_scope_sha256=d2_identity.source_scope_sha256,
        stage_input_bundle_sha256=stage_input.attestation.bundle_sha256,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
    )
    orchestrator = CanonicalParentProvisionObjectWorkflowOrchestrator(
        kit=kit,
        object_compiler=compiler,
        object_transport=LocalCodexAppServerStructuredStageTransport(
            timeout_seconds=timeout_seconds
        ),
        reconciliation_transport=LocalCodexAppServerStructuredStageTransport(
            timeout_seconds=timeout_seconds
        ),
        max_attempts_per_object=max_stage_attempts,
        max_attempts_per_group=max_stage_attempts,
    )
    try:
        result = orchestrator.run(
            d3_session,
            object_task_binding_sha256=object_binding,
            reconciliation_task_binding_sha256=reconciliation_binding,
            plan=plan,
        )
    except LifecycleProbeError as exc:
        raise LifecycleProbeError(
            _safe_object_parent_failure_code(exc.reason_code)
        ) from exc
    except Exception as exc:
        raise LifecycleProbeError("D3_OBJECT_PARENT_FLOW_FAILED") from exc
    if not result.publication_ready:
        reason = result.reason_codes[0] if result.reason_codes else "INCOMPLETE"
        raise ObjectParentFlowIncompleteError(
            _safe_object_parent_failure_code(reason),
            _object_parent_flow_incomplete_summary(result),
        )
    return _object_parent_flow_summary(result)


def _run_region_object_parent_flow(
    *,
    d3_session,  # type: ignore[no-untyped-def]
    kit,  # type: ignore[no-untyped-def]
    d2_result,  # type: ignore[no-untyped-def]
    runtime: RegionParentRuntimeAdapterV1,
    stage_input_bundle_sha256: str,
    run_id: str,
    source_sha256: str,
    timeout_seconds: float,
    max_stage_attempts: int,
    plan_only: bool = False,
) -> dict[str, object]:
    workspace = d3_session.d3_region_stage_input
    if (
        workspace is None
        or workspace.attestation.bundle_sha256 != stage_input_bundle_sha256
        or runtime.workspace_bundle_sha256 != stage_input_bundle_sha256
    ):
        raise LifecycleProbeError("D3_REGION_RUNTIME_WORKSPACE_BINDING_DRIFT")
    compiler = CanonicalParentProvisionObjectBatchCompiler.from_region_runtime(
        runtime=runtime,
        workspace_attestation=workspace.attestation,
        kit_identity=kit.identity,
    )
    plan = compiler.build_plan()
    if plan_only:
        summary = _object_parent_plan_summary(plan)
        summary["mode"] = "REGION_OBJECT_PARENT_PLAN_ONLY"
        summary["region_runtime_adapter_sha256"] = runtime.adapter_sha256
        return summary
    object_binding, reconciliation_binding = _object_parent_flow_task_bindings(
        run_id=run_id,
        source_sha256=source_sha256,
        d2_artifact_sha256=d2_result.stage_artifact.payload_sha256,
        source_scope_sha256=runtime.d2_identity.source_scope_sha256,
        stage_input_bundle_sha256=stage_input_bundle_sha256,
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
    )
    orchestrator = CanonicalParentProvisionObjectWorkflowOrchestrator(
        kit=kit,
        object_compiler=compiler,
        object_transport=LocalCodexAppServerStructuredStageTransport(
            timeout_seconds=timeout_seconds
        ),
        reconciliation_transport=LocalCodexAppServerStructuredStageTransport(
            timeout_seconds=timeout_seconds
        ),
        max_attempts_per_object=max_stage_attempts,
        max_attempts_per_group=max_stage_attempts,
    )
    try:
        result = orchestrator.run(
            d3_session,
            object_task_binding_sha256=object_binding,
            reconciliation_task_binding_sha256=reconciliation_binding,
            plan=plan,
        )
    except LifecycleProbeError as exc:
        raise LifecycleProbeError(
            _safe_object_parent_failure_code(exc.reason_code)
        ) from exc
    except Exception as exc:
        raise LifecycleProbeError("D3_REGION_OBJECT_PARENT_FLOW_FAILED") from exc
    if not result.publication_ready:
        reason = result.reason_codes[0] if result.reason_codes else "INCOMPLETE"
        summary = _object_parent_flow_incomplete_summary(result)
        summary["mode"] = "REGION_OBJECT_PARENT_FLOW"
        summary["region_runtime_adapter_sha256"] = runtime.adapter_sha256
        raise ObjectParentFlowIncompleteError(
            _safe_object_parent_failure_code(reason),
            summary,
        )
    summary = _object_parent_flow_summary(result)
    summary["mode"] = "REGION_OBJECT_PARENT_FLOW"
    summary["region_runtime_adapter_sha256"] = runtime.adapter_sha256
    return summary


def _object_parent_plan_summary(plan) -> dict[str, object]:  # type: ignore[no-untyped-def]
    plan_counts = Counter(item.execution_mode.value for item in plan.targets)
    kinds_by_mode: dict[str, dict[str, int]] = {}
    for mode in ParentProvisionObjectExecutionMode:
        kinds_by_mode[mode.value] = dict(
            sorted(
                Counter(
                    item.object_kind.value
                    for item in plan.targets
                    if item.execution_mode is mode
                ).items()
            )
        )
    return {
        "mode": "OBJECT_PARENT_PLAN_ONLY",
        "publication_ready": False,
        "plan_sha256": plan.plan_sha256,
        "planned_batch_count": len(plan.targets),
        "model_target_count": plan_counts[
            ParentProvisionObjectExecutionMode.MODEL_TARGET.value
        ],
        "host_empty_count": plan_counts[
            ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER.value
        ],
        "recovery_required_count": plan_counts[
            ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED.value
        ],
        "target_kind_counts_by_mode": kinds_by_mode,
        "source_segment_count": sum(
            len(item.segment_ordinals) for item in plan.targets
        ),
    }


def _object_parent_flow_incomplete_summary(
    result,  # type: ignore[no-untyped-def]
) -> dict[str, object]:
    object_result = result.object_result
    coordination = result.coordination_result
    plan_counts = Counter(
        item.execution_mode.value for item in object_result.plan.targets
    )
    batch_status_counts = Counter(
        item.status.value for item in object_result.batch_receipts
    )
    group_receipts = (
        coordination.group_receipts_in_completion_order
        if coordination is not None
        else ()
    )
    return {
        "mode": "OBJECT_PARENT_FLOW",
        "publication_ready": False,
        "plan_sha256": object_result.plan.plan_sha256,
        "planned_batch_count": len(object_result.plan.targets),
        "model_target_count": plan_counts[
            ParentProvisionObjectExecutionMode.MODEL_TARGET.value
        ],
        "host_empty_count": plan_counts[
            ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER.value
        ],
        "recovery_required_count": plan_counts[
            ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED.value
        ],
        "completed_batch_count": len(object_result.batch_receipts),
        "batch_status_counts": dict(sorted(batch_status_counts.items())),
        "object_attempt_count": sum(
            len(item.attempts) for item in object_result.batch_receipts
        ),
        "coordination_group_count": (
            len(coordination.reconciliation_plan.groups)
            if coordination is not None
            else 0
        ),
        "completed_coordination_group_count": len(group_receipts),
        "coordination_group_status_counts": dict(
            sorted(Counter(item.status.value for item in group_receipts).items())
        ),
        "coordination_attempt_count": sum(
            len(item.attempts) for item in group_receipts
        ),
        "reason_codes": tuple(result.reason_codes),
        "object_execution_receipt_sha256": (
            object_result.execution_receipt.receipt_sha256
        ),
        "reconciliation_execution_receipt_sha256": (
            coordination.execution_receipt.receipt_sha256
            if coordination is not None
            else None
        ),
    }


def _object_parent_flow_summary(result) -> dict[str, object]:  # type: ignore[no-untyped-def]
    professional = result.professional_result
    finalized = result.finalized_execution_receipt
    if professional is None or finalized is None:
        raise LifecycleProbeError("D3_OBJECT_PARENT_PROFESSIONAL_PROOF_MISSING")
    object_result = result.object_result
    coordination = result.coordination_result
    plan_counts = Counter(
        item.execution_mode.value for item in object_result.plan.targets
    )
    batch_status_counts = Counter(
        item.status.value for item in object_result.batch_receipts
    )
    group_receipts = (
        coordination.group_receipts_in_completion_order
        if coordination is not None
        else ()
    )
    group_status_counts = Counter(item.status.value for item in group_receipts)
    proposal = professional.proposal
    nested_closure = professional.nested_evidence_assignment_closure
    return {
        "mode": "OBJECT_PARENT_FLOW",
        "plan_sha256": object_result.plan.plan_sha256,
        "planned_batch_count": len(object_result.plan.targets),
        "model_target_count": plan_counts[
            ParentProvisionObjectExecutionMode.MODEL_TARGET.value
        ],
        "host_empty_count": plan_counts[
            ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER.value
        ],
        "recovery_required_count": plan_counts[
            ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED.value
        ],
        "batch_status_counts": dict(sorted(batch_status_counts.items())),
        "object_attempt_count": sum(
            len(item.attempts) for item in object_result.batch_receipts
        ),
        "coordination_group_count": (
            len(coordination.reconciliation_plan.groups)
            if coordination is not None
            else 0
        ),
        "coordination_group_status_counts": dict(sorted(group_status_counts.items())),
        "coordination_attempt_count": sum(
            len(item.attempts) for item in group_receipts
        ),
        "finalization_mode": finalized.finalization_mode.value,
        "artifact_status": professional.stage_artifact.status.value,
        "parent_provision_count": len(proposal.entries),
        "declared_scope_count": len(proposal.declared_logical_object_ids),
        "unresolved_scope_count": len(proposal.unresolved_logical_object_ids),
        "unresolved_entry_count": sum(
            item.requirementness.value == "UNRESOLVED" for item in proposal.entries
        ),
        "professional_policy_attested": (
            professional.professional_policy_attestation.policy_attested
        ),
        "professional_proof_sha256": (professional.professional_proof.proof_sha256),
        "professional_policy_attestation_sha256": canonical_sha256(
            professional.professional_policy_attestation.model_dump(mode="json")
        ),
        "professional_execution_receipt_sha256": canonical_sha256(
            professional.professional_execution_receipt.model_dump(mode="json")
        ),
        "nested_evidence_assignment_count": (
            len(nested_closure.assignments) if nested_closure is not None else None
        ),
        "nested_evidence_unassigned_count": (
            len(nested_closure.unassigned_evidence_ids)
            if nested_closure is not None
            else None
        ),
        "nested_evidence_assignment_closure_sha256": (
            nested_closure.closure_sha256 if nested_closure is not None else None
        ),
        "host_validation_receipt_sha256": (
            professional.host_validation_receipt.receipt_sha256
        ),
        "stage_artifact_payload_sha256": professional.stage_artifact.payload_sha256,
        "stage_artifact_validator_receipt_sha256": (
            professional.stage_artifact.validator_receipt_sha256
        ),
        "stage_final_receipt_sha256": canonical_sha256(
            professional.final_receipt.model_dump(mode="json")
        ),
        "finalized_execution_receipt_sha256": finalized.receipt_sha256,
        "object_execution_receipt_sha256": (
            object_result.execution_receipt.receipt_sha256
        ),
        "reconciliation_execution_receipt_sha256": (
            coordination.execution_receipt.receipt_sha256
            if coordination is not None
            else None
        ),
        "requirementness_counts": dict(
            sorted(
                Counter(item.requirementness.value for item in proposal.entries).items()
            )
        ),
        "content_role_counts": dict(
            sorted(
                Counter(item.content_role.value for item in proposal.entries).items()
            )
        ),
    }


def _safe_object_parent_failure_code(reason_code: str) -> str:
    safe = (
        reason_code
        if reason_code
        and reason_code.replace("_", "A").isalnum()
        and reason_code.upper() == reason_code
        else "FAILED"
    )
    return f"D3_OBJECT_PARENT_FLOW_{safe[:96]}"


def _d3_scope_profiles(
    proposal: D2LogicalStructureProposalV1,
    *,
    declared_ids: set[str],
    unresolved_ids: set[str],
) -> list[dict[str, object]]:
    profiles: list[dict[str, object]] = []
    for item in proposal.logical_objects:
        disposition = (
            "DECLARED"
            if item.logical_object_id in declared_ids
            else "UNRESOLVED"
            if item.logical_object_id in unresolved_ids
            else "MISSING"
        )
        profiles.append(
            {
                "disposition": disposition,
                "kind": item.object_kind.value,
                "first_page": item.page_numbers[0],
                "last_page": item.page_numbers[-1],
                "page_count": len(item.page_numbers),
                "source_segment_count": len(item.source_segments),
                "logical_table_scope_count": len(item.logical_table_scope_ids),
                "has_parent_container": item.parent_logical_object_id is not None,
            }
        )
    return profiles


def _is_retryable_candidate_error(stage: str, reason_code: str) -> bool:
    return reason_code.startswith(
        (
            f"CANONICAL_{stage}_CANDIDATE_INVALID_",
            f"CANONICAL_{stage}_HOST_REJECTED_",
            f"CANONICAL_{stage}_STRUCTURED_OUTPUT_INVALID",
            f"CANONICAL_{stage}_DECISION_CANDIDATE_INVALID",
        )
    )


def _resolve_d3_source_segments(
    proposal: D2LogicalStructureProposalV1,
    *,
    source_ir: SourceIRSnapshot,
) -> tuple[D3ExactSourceSegmentV1, ...]:
    nodes = {item.source_node_id: item for item in source_ir.nodes}
    cells = {
        cell.cell_id: cell
        for node in source_ir.nodes
        if node.kind is SourceNodeKind.TABLE and node.table_payload is not None
        for cell in node.table_payload.cells
    }
    resolved: list[D3ExactSourceSegmentV1] = []
    for logical_object in proposal.logical_objects:
        for segment in logical_object.source_segments:
            if segment.handle_kind is D2SourceHandleKind.SOURCE_NODE:
                source_text = nodes[segment.source_handle].raw_text
            else:
                source_text = cells[segment.source_handle].raw_text
            if segment.start_offset is not None:
                assert segment.end_offset is not None
                source_text = source_text[segment.start_offset : segment.end_offset]
            resolved.append(
                D3ExactSourceSegmentV1(
                    segment_id=segment.segment_id,
                    logical_object_id=logical_object.logical_object_id,
                    source_handle=segment.source_handle,
                    handle_kind=segment.handle_kind.value,
                    owner_source_node_id=segment.owner_source_node_id,
                    source_order=segment.source_order,
                    page_numbers=segment.page_numbers,
                    handle_start_offset=segment.start_offset,
                    handle_end_offset=segment.end_offset,
                    source_text=source_text,
                    source_text_sha256=hashlib.sha256(
                        source_text.encode("utf-8")
                    ).hexdigest(),
                    source_content_sha256=segment.source_content_sha256,
                    d2_segment_sha256=segment.segment_sha256,
                    evidence_refs=tuple(
                        sorted(
                            {
                                segment.segment_id,
                                segment.source_handle,
                                segment.owner_source_node_id,
                            }
                        )
                    ),
                )
            )
    return tuple(resolved)


def _activate_candidate_stages(
    kit_root: Path,
    *,
    through_d3: bool,
    region_object_parent_flow: bool = False,
) -> None:
    manifest_path = kit_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for stage in manifest["stages"]:
        if stage["stage"] == AgentExtractionStage.LOGICAL_STRUCTURE.value:
            stage["implementation_status"] = "AVAILABLE"
            if not region_object_parent_flow:
                stage.update(
                    {
                        "artifact_schema_id": (
                            D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION
                        ),
                        "artifact_schema_path": (
                            "schemas/logical-structure-proposal.v1.schema.json"
                        ),
                        "runner_output_schema_id": (
                            D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_SCHEMA_VERSION
                        ),
                        "runner_output_schema_path": (
                            "schemas/d2-logical-structure-decision-candidate.v2.schema.json"
                        ),
                        "validator_id": D2_LOGICAL_STRUCTURE_VALIDATOR_ID,
                        "validator_version": D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION,
                    }
                )
        elif (
            through_d3
            and stage["stage"]
            == AgentExtractionStage.PARENT_PROVISION_RECOGNITION.value
        ):
            stage["implementation_status"] = "AVAILABLE"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lock = compute_agent_kit_lock(kit_root)
    (kit_root / "agent-kit.lock.json").write_text(
        lock.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex-reqpilotx",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPOSITORY_ROOT,
    )
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--through-d3", action="store_true")
    parser.add_argument("--object-parent-flow", action="store_true")
    parser.add_argument("--region-object-parent-flow", action="store_true")
    parser.add_argument("--object-parent-plan-only", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--corpus-root", type=Path)
    parser.add_argument("--document-role", default="SYNTHETIC_TEST")
    parser.add_argument("--max-stage-attempts", type=int, default=2)
    parser.add_argument("--dynamic-tools", action="store_true")
    parser.add_argument("--dynamic-tool-probe", action="store_true")
    args = parser.parse_args()
    try:
        result = run_canonical_d2_app_server_canary(
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            repository_root=args.repository_root,
            timeout_seconds=args.timeout_seconds,
            through_d3=args.through_d3,
            object_parent_flow=args.object_parent_flow,
            region_object_parent_flow=args.region_object_parent_flow,
            object_parent_plan_only=args.object_parent_plan_only,
            source_path=args.source,
            corpus_root=args.corpus_root,
            document_role=args.document_role,
            max_stage_attempts=args.max_stage_attempts,
            dynamic_tools=args.dynamic_tools,
            dynamic_tool_probe=args.dynamic_tool_probe,
        )
    except ObjectParentFlowIncompleteError as exc:
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                    "d3_incomplete": exc.summary,
                    "run_temp_cleanup": True,
                },
                sort_keys=True,
            )
        )
        return 2
    except LifecycleProbeError as exc:
        print(
            json.dumps(
                {
                    "schema_version": CANARY_SCHEMA_VERSION,
                    "status": "FAIL",
                    "reason_code": exc.reason_code,
                },
                sort_keys=True,
            )
        )
        return 2
    result["run_temp_cleanup"] = True
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
