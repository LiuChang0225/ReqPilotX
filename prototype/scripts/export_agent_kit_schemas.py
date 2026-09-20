#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_ROOT))

from app.application.agent_workflow.extraction_runtime import (  # noqa: E402
    AgentExecutionRequestV1,
    AgentExecutionResultV1,
    AgentKitIdentityV1,
    AgentRunEventV1,
    AgentStageArtifactV1,
    AgentStageFinalReceiptV1,
)
from app.application.agent_workflow.document_understanding import (  # noqa: E402
    DocumentAssetLedgerV1,
    DocumentPageFactsChunkV1,
    DocumentPageInputV1,
    DocumentPageLedgerV2,
    DocumentPageTextChunkV1,
    DocumentPageTextWindowInputV1,
    DocumentPageWindowInputV1,
    DocumentRenderPageInputV1,
    DocumentRenderReceiptV1,
    DocumentRenderRegionInputV1,
    DocumentRunManifestV2,
    DocumentRunManifestInputV2,
    DocumentSourceHandleInputV1,
    DocumentTableMapChunkV1,
    DocumentTableMapInputV1,
    DocumentUnderstandingProposalV2,
    DocumentUnderstandingValidationReceiptV2,
    DocumentValidateUnderstandingInputV1,
)
from app.application.agent_workflow.document_map_exploration import (  # noqa: E402
    DocumentMapSurveyCandidateV1,
)
from app.application.agent_workflow.document_map_scope import (  # noqa: E402
    DocumentMapHeadingScopeCandidateV1,
    DocumentMapHeadingScopeCandidateV2,
)
from app.application.agent_workflow.document_map_replacement import (  # noqa: E402
    DocumentMapScopeReplacementCandidateV1,
    DocumentMapScopeReplacementCandidateV2,
)
from app.application.agent_workflow.d2_logical_structure import (  # noqa: E402
    D2LogicalStructureProposalV1,
    D2LogicalStructureValidationReceiptV1,
)
from app.application.agent_workflow.d2_logical_structure_v2 import (  # noqa: E402
    D2LogicalStructureProposalV2,
)
from app.application.agent_workflow.d2_decision_candidate import (  # noqa: E402
    D2LogicalStructureDecisionCandidateV2,
)
from app.application.agent_workflow.d2_decision_candidate_v3 import (  # noqa: E402
    D2LogicalStructureDecisionCandidateV3,
)
from app.application.agent_workflow.d3_parent_provisions import (  # noqa: E402
    ParentProvisionProposalV3,
    ParentProvisionValidationReceiptV3,
)
from app.application.agent_workflow.d3_parent_provision_compiler import (  # noqa: E402
    D3ParentBoundaryClassificationCandidateV2,
)
from app.application.agent_workflow.parent_provision_object_batches import (  # noqa: E402
    ParentProvisionObjectCandidateV1,
    ParentProvisionObjectWindowCandidateV1,
)
from app.application.agent_workflow.parent_provision_object_reconciliation import (  # noqa: E402
    ParentProvisionObjectReconciliationCandidateV1,
)
from app.application.agent_workflow.source_requirement_baseline import (  # noqa: E402
    SourceItemTextSliceCandidateV1,
    SourceRequirementSliceCandidateV1,
)
from app.application.agent_workflow.source_item_visual import (  # noqa: E402
    SourceItemImageScopeCandidateV1,
)
from app.adapters.agents.codex_app_server.native_source_item_text_reference import (  # noqa: E402
    SourceItemTextBatchCandidateV1,
)
from app.adapters.agents.codex_app_server.native_source_item_text_windowed import (  # noqa: E402
    SourceItemTextBoundaryAgentCandidateV1,
    SourceItemTextClassificationAgentCandidateV1,
)
from app.adapters.agents.codex_app_server.native_source_requirement_windowed_reference import (  # noqa: E402
    SourceRequirementBoundaryAgentCandidateV1,
    SourceRequirementClassificationAgentCandidateV1,
)
from app.application.agent_workflow.runner_output_schema import (  # noqa: E402
    compile_strict_runner_output_schema,
)


SCHEMAS = {
    "source-item-image-scope-candidate.v1.schema.json": SourceItemImageScopeCandidateV1,
    "source-requirement-boundary-agent-candidate.v1.schema.json": SourceRequirementBoundaryAgentCandidateV1,
    "source-requirement-classification-window-candidate.v1.schema.json": SourceRequirementClassificationAgentCandidateV1,
    "agent-execution-request.v1.schema.json": AgentExecutionRequestV1,
    "agent-execution-result.v1.schema.json": AgentExecutionResultV1,
    "agent-kit-identity.v1.schema.json": AgentKitIdentityV1,
    "agent-run-event.v1.schema.json": AgentRunEventV1,
    "agent-stage-artifact.v1.schema.json": AgentStageArtifactV1,
    "agent-stage-final-receipt.v1.schema.json": AgentStageFinalReceiptV1,
    "document-asset-ledger.v1.schema.json": DocumentAssetLedgerV1,
    "document-page-facts.v1.schema.json": DocumentPageFactsChunkV1,
    "document-page-input.v1.schema.json": DocumentPageInputV1,
    "document-page-ledger.v2.schema.json": DocumentPageLedgerV2,
    "document-page-text-window-input.v1.schema.json": DocumentPageTextWindowInputV1,
    "document-page-text.v1.schema.json": DocumentPageTextChunkV1,
    "document-page-window-input.v1.schema.json": DocumentPageWindowInputV1,
    "document-render-page-input.v1.schema.json": DocumentRenderPageInputV1,
    "document-render-receipt.v1.schema.json": DocumentRenderReceiptV1,
    "document-render-region-input.v1.schema.json": DocumentRenderRegionInputV1,
    "document-run-manifest.v2.schema.json": DocumentRunManifestV2,
    "document-run-manifest-input.v2.schema.json": DocumentRunManifestInputV2,
    "document-source-handle-input.v1.schema.json": DocumentSourceHandleInputV1,
    "document-table-map-input.v1.schema.json": DocumentTableMapInputV1,
    "document-table-map.v1.schema.json": DocumentTableMapChunkV1,
    "document-understanding-proposal.v2.schema.json": DocumentUnderstandingProposalV2,
    "document-understanding-validation-receipt.v2.schema.json": (
        DocumentUnderstandingValidationReceiptV2
    ),
    "document-map-survey-candidate.v1.schema.json": (
        DocumentMapSurveyCandidateV1
    ),
    "document-map-scope-replacement-candidate.v1.schema.json": (
        DocumentMapScopeReplacementCandidateV1
    ),
    "document-map-scope-replacement-candidate.v2.schema.json": DocumentMapScopeReplacementCandidateV2,
    "document-map-heading-scope-candidate.v1.schema.json": (
        DocumentMapHeadingScopeCandidateV1
    ),
    "document-map-heading-scope-candidate.v2.schema.json": (
        DocumentMapHeadingScopeCandidateV2
    ),
    "logical-structure-proposal.v1.schema.json": D2LogicalStructureProposalV1,
    "logical-structure-proposal.v2.schema.json": D2LogicalStructureProposalV2,
    "logical-structure-validation-receipt.v1.schema.json": (
        D2LogicalStructureValidationReceiptV1
    ),
    "parent-provision-proposal.v3.schema.json": ParentProvisionProposalV3,
    "parent-provision-validation-receipt.v3.schema.json": (
        ParentProvisionValidationReceiptV3
    ),
    "document-validate-understanding-input.v1.schema.json": (
        DocumentValidateUnderstandingInputV1
    ),
    "d2-logical-structure-decision-candidate.v2.schema.json": (
        D2LogicalStructureDecisionCandidateV2
    ),
    "d2-logical-structure-decision-candidate.v3.schema.json": (
        D2LogicalStructureDecisionCandidateV3
    ),
    "d3-parent-boundary-classification-candidate.v2.schema.json": (
        D3ParentBoundaryClassificationCandidateV2
    ),
    "parent-provision-object-candidate.v1.schema.json": (
        ParentProvisionObjectCandidateV1
    ),
    "parent-provision-object-window-candidate.v1.schema.json": (
        ParentProvisionObjectWindowCandidateV1
    ),
    "parent-provision-object-reconciliation-candidate.v1.schema.json": (
        ParentProvisionObjectReconciliationCandidateV1
    ),
    "source-item-text-batch-candidate.v1.schema.json": (
        SourceItemTextBatchCandidateV1
    ),
    "source-item-text-boundary-agent-candidate.v1.schema.json": (
        SourceItemTextBoundaryAgentCandidateV1
    ),
    "source-item-text-classification-window-candidate.v1.schema.json": (
        SourceItemTextClassificationAgentCandidateV1
    ),
    "source-item-text-slice-candidate.v1.schema.json": (
        SourceItemTextSliceCandidateV1
    ),
    "source-requirement-slice-candidate.v1.schema.json": (
        SourceRequirementSliceCandidateV1
    ),
}

STRICT_RUNNER_SCHEMAS = {
    "source-item-image-scope-candidate.v1.schema.json": SourceItemImageScopeCandidateV1,
    "source-requirement-boundary-agent-candidate.v1.schema.json": SourceRequirementBoundaryAgentCandidateV1,
    "source-requirement-classification-window-candidate.v1.schema.json": SourceRequirementClassificationAgentCandidateV1,
    "document-map-heading-scope-candidate.v1.schema.json": (
        DocumentMapHeadingScopeCandidateV1
    ),
    "document-map-heading-scope-candidate.v2.schema.json": (
        DocumentMapHeadingScopeCandidateV2
    ),
    "document-map-survey-candidate.v1.schema.json": (
        DocumentMapSurveyCandidateV1
    ),
    "document-map-scope-replacement-candidate.v1.schema.json": (
        DocumentMapScopeReplacementCandidateV1
    ),
    "document-map-scope-replacement-candidate.v2.schema.json": DocumentMapScopeReplacementCandidateV2,
    "d2-logical-structure-decision-candidate.v2.schema.json": (
        D2LogicalStructureDecisionCandidateV2
    ),
    "d2-logical-structure-decision-candidate.v3.schema.json": (
        D2LogicalStructureDecisionCandidateV3
    ),
    "d3-parent-boundary-classification-candidate.v2.schema.json": (
        D3ParentBoundaryClassificationCandidateV2
    ),
    "parent-provision-object-candidate.v1.schema.json": (
        ParentProvisionObjectCandidateV1
    ),
    "parent-provision-object-window-candidate.v1.schema.json": (
        ParentProvisionObjectWindowCandidateV1
    ),
    "parent-provision-object-reconciliation-candidate.v1.schema.json": (
        ParentProvisionObjectReconciliationCandidateV1
    ),
    "source-item-text-batch-candidate.v1.schema.json": (
        SourceItemTextBatchCandidateV1
    ),
    "source-item-text-boundary-agent-candidate.v1.schema.json": (
        SourceItemTextBoundaryAgentCandidateV1
    ),
    "source-item-text-classification-window-candidate.v1.schema.json": (
        SourceItemTextClassificationAgentCandidateV1
    ),
    "source-item-text-slice-candidate.v1.schema.json": (
        SourceItemTextSliceCandidateV1
    ),
    "source-requirement-slice-candidate.v1.schema.json": (
        SourceRequirementSliceCandidateV1
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--kit-root",
        type=Path,
        default=PROTOTYPE_ROOT.parent / "agent-kit",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schema_root = args.kit_root.resolve() / "schemas"
    expected = {
        name: json.dumps(
            (
                compile_strict_runner_output_schema(model)
                if name in STRICT_RUNNER_SCHEMAS
                else model.model_json_schema()
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
        for name, model in SCHEMAS.items()
    }
    if args.check:
        failures = [
            name
            for name, payload in expected.items()
            if not (schema_root / name).is_file()
            or (schema_root / name).read_text(encoding="utf-8") != payload
        ]
        if failures:
            raise SystemExit("Agent Kit schemas drifted: " + ", ".join(failures))
        return 0
    schema_root.mkdir(parents=True, exist_ok=True)
    for name, payload in expected.items():
        (schema_root / name).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
