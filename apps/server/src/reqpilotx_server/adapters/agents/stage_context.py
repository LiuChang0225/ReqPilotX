from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reqpilotx_server.adapters.agents.portable_kit import (
    AgentKitValidationError,
    PortableAgentKit,
)
from reqpilotx_server.application.agent_workflow.document_understanding import (
    canonical_json_sha256,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import AgentExtractionStage


@dataclass(frozen=True, slots=True)
class CompiledStageReference:
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CompiledAgentStageContext:
    stage: AgentExtractionStage
    skill_id: str
    skill_path: Path
    skill_sha256: str
    policy_path: str
    policy_sha256: str
    references: tuple[CompiledStageReference, ...]
    artifact_schema_path: Path
    artifact_schema_sha256: str
    validator_id: str
    validator_version: str
    context_text: str
    context_sha256: str


def compile_agent_stage_context(
    kit: PortableAgentKit,
    stage: AgentExtractionStage,
) -> CompiledAgentStageContext:
    stage_spec = next(
        (item for item in kit.manifest.stages if item.stage is stage),
        None,
    )
    if stage_spec is None or stage_spec.implementation_status != "AVAILABLE":
        raise AgentKitValidationError("Agent Kit stage is not available")
    if (
        stage_spec.skill_id is None
        or stage_spec.artifact_schema_path is None
        or stage_spec.validator_id is None
        or stage_spec.validator_version is None
    ):
        raise AgentKitValidationError("Agent Kit Agent stage is incomplete")
    skill_spec = next(
        (item for item in kit.manifest.skills if item.skill_id == stage_spec.skill_id),
        None,
    )
    if skill_spec is None or skill_spec.stage is not stage:
        raise AgentKitValidationError("Agent Kit stage Skill is missing")
    skill_path = (kit.root / skill_spec.path).resolve(strict=True)
    artifact_schema_path = (kit.root / stage_spec.artifact_schema_path).resolve(
        strict=True
    )
    policy_path = (kit.root / kit.manifest.policy_path).resolve(strict=True)
    skill_sha256 = _sha256(skill_path)
    artifact_schema_sha256 = _sha256(artifact_schema_path)
    policy_sha256 = _sha256(policy_path)
    sections = [
        "REQPILOTX_PORTABLE_STAGE_CONTEXT_V1",
        f"stage={stage.value}",
        f"kit_id={kit.identity.kit_id}",
        f"kit_version={kit.identity.kit_version}",
        f"kit_bundle_sha256={kit.identity.bundle_sha256}",
        f"skill_id={stage_spec.skill_id}",
        f"skill_sha256={skill_sha256}",
        f"artifact_schema_sha256={artifact_schema_sha256}",
        f"validator={stage_spec.validator_id}@{stage_spec.validator_version}",
        "",
        f"BEGIN POLICY {kit.manifest.policy_path} {policy_sha256}",
        policy_path.read_text(encoding="utf-8"),
        "END POLICY",
    ]
    references: list[CompiledStageReference] = []
    for relative in stage_spec.reference_paths:
        path = (kit.root / relative).resolve(strict=True)
        sha256 = _sha256(path)
        references.append(CompiledStageReference(path=relative, sha256=sha256))
        sections.extend(
            [
                "",
                f"BEGIN REFERENCE {relative} {sha256}",
                path.read_text(encoding="utf-8"),
                "END REFERENCE",
            ]
        )
    context_text = "\n".join(sections).rstrip() + "\n"
    context_sha256 = canonical_json_sha256(
        {
            "stage": stage.value,
            "kit_bundle_sha256": kit.identity.bundle_sha256,
            "skill_sha256": skill_sha256,
            "policy_sha256": policy_sha256,
            "references": [
                {"path": item.path, "sha256": item.sha256} for item in references
            ],
            "artifact_schema_sha256": artifact_schema_sha256,
            "validator_id": stage_spec.validator_id,
            "validator_version": stage_spec.validator_version,
            "context_text": context_text,
        }
    )
    return CompiledAgentStageContext(
        stage=stage,
        skill_id=stage_spec.skill_id,
        skill_path=skill_path,
        skill_sha256=skill_sha256,
        policy_path=kit.manifest.policy_path,
        policy_sha256=policy_sha256,
        references=tuple(references),
        artifact_schema_path=artifact_schema_path,
        artifact_schema_sha256=artifact_schema_sha256,
        validator_id=stage_spec.validator_id,
        validator_version=stage_spec.validator_version,
        context_text=context_text,
        context_sha256=context_sha256,
    )


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "CompiledAgentStageContext",
    "CompiledStageReference",
    "compile_agent_stage_context",
]
