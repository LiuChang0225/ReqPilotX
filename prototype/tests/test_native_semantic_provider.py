from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import subprocess
import unittest
from pathlib import Path

from app.adapters.agents.codex_app_server.native_semantic_provider import (
    CodexConsensusSemanticProvider,
)
from app.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from app.domain.extraction.requirement_view import RequirementProjectionRole
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.semantics import RequirementSubtype, SemanticClass
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.ports.semantic_provider import (
    RequirementAssemblyDecision,
    RequirementAssemblyNodeInput,
    RequirementAssemblyRequest,
    PreflightSemanticProviderPort,
    SemanticClassificationRequest,
    SemanticDecision,
    SemanticEvidenceInput,
    SemanticNodeInput,
)


TARGET = "structure-node-" + "1" * 32
REF = StructureSourceRef(
    kind=StructureSourceRefKind.SOURCE_NODE,
    source_id="node-" + "2" * 32,
)


def _classification_request() -> SemanticClassificationRequest:
    text = "The component shall satisfy the limit."
    evidence = SemanticEvidenceInput(
        source_ref=REF,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    return SemanticClassificationRequest(
        source_ir_id="source-ir-test",
        source_sha256="3" * 64,
        structure_snapshot_id="structure-test",
        scope_structure_node_ids=(TARGET,),
        nodes=(
            SemanticNodeInput(
                target_structure_node_id=TARGET,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                target_source_refs=(REF,),
                evidence=(evidence,),
            ),
        ),
    )


def _semantic_decision(semantic_class: SemanticClass) -> SemanticDecision:
    return SemanticDecision(
        target_structure_node_id=TARGET,
        semantic_class=semantic_class,
        requirement_subtype=(
            RequirementSubtype.PRODUCT_REQUIREMENT
            if semantic_class is SemanticClass.REQUIREMENT
            else None
        ),
        evidence_source_refs=(REF,),
        reason_codes=("source_bound_decision",),
    )


def _assembly_request() -> RequirementAssemblyRequest:
    text = "The component shall satisfy the limit."
    return RequirementAssemblyRequest(
        source_ir_id="source-ir-test",
        structure_snapshot_id="structure-test",
        semantic_snapshot_id="semantic-test",
        scope_structure_node_ids=(TARGET,),
        nodes=(
            RequirementAssemblyNodeInput(
                target_structure_node_id=TARGET,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                semantic_annotation_ids=("semantic-annotation-1",),
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                evidence=(
                    SemanticEvidenceInput(
                        source_ref=REF,
                        text=text,
                        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    ),
                ),
            ),
        ),
    )


def _assembly_decision(include: bool) -> RequirementAssemblyDecision:
    return RequirementAssemblyDecision(
        target_structure_node_id=TARGET,
        include_in_view=include,
        role=RequirementProjectionRole.PRIMARY if include else None,
        status=RequirementAssemblyStatus.CANDIDATE,
        reason_codes=("source_bound_assembly",),
    )


class _FakeRunner:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = payloads

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        payload = self.payloads.pop(0)
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class NativeSemanticProviderTests(unittest.TestCase):
    def test_classification_uses_strict_majority(self) -> None:
        requirement = _semantic_decision(SemanticClass.REQUIREMENT)
        heading = _semantic_decision(SemanticClass.HEADING)
        provider = CodexConsensusSemanticProvider(
            command_runner=_FakeRunner(
                [
                    {"decisions": [requirement.model_dump(mode="json")]},
                    {"decisions": [heading.model_dump(mode="json")]},
                    {"decisions": [requirement.model_dump(mode="json")]},
                ]
            )
        )

        result = provider.classify_nodes(_classification_request())

        self.assertIsInstance(provider, PreflightSemanticProviderPort)
        self.assertEqual(result.decisions[0].semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(result.run.request_count, 3)

    def test_no_classification_majority_is_partial_unresolved(self) -> None:
        provider = CodexConsensusSemanticProvider(
            command_runner=_FakeRunner(
                [
                    {
                        "decisions": [
                            _semantic_decision(SemanticClass.REQUIREMENT).model_dump(
                                mode="json"
                            )
                        ]
                    },
                    {
                        "decisions": [
                            _semantic_decision(SemanticClass.HEADING).model_dump(
                                mode="json"
                            )
                        ]
                    },
                    {
                        "decisions": [
                            _semantic_decision(SemanticClass.BACKGROUND).model_dump(
                                mode="json"
                            )
                        ]
                    },
                ]
            )
        )

        result = provider.classify_nodes(_classification_request())

        self.assertEqual(result.decisions[0].semantic_class, SemanticClass.UNRESOLVED)
        self.assertEqual(result.run.status, SemanticProviderRunStatus.PARTIAL)

    def test_assembly_uses_strict_majority(self) -> None:
        included = _assembly_decision(True)
        excluded = _assembly_decision(False)
        provider = CodexConsensusSemanticProvider(
            command_runner=_FakeRunner(
                [
                    {"decisions": [included.model_dump(mode="json")]},
                    {"decisions": [excluded.model_dump(mode="json")]},
                    {"decisions": [included.model_dump(mode="json")]},
                ]
            )
        )

        result = provider.assemble_requirements(_assembly_request())

        self.assertTrue(result.decisions[0].include_in_view)
        self.assertEqual(result.decisions[0].role, RequirementProjectionRole.PRIMARY)
        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)


if __name__ == "__main__":
    unittest.main()
