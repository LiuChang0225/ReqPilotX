from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.agent_workflow.extraction_runtime import (
    AGENT_EXTRACTION_STAGE_ORDER,
    AgentExtractionStage,
    AgentKitIdentityV1,
    AgentRunnerKind,
)


AGENT_KIT_MANIFEST_SCHEMA_VERSION = "reqpilotx-portable-agent-kit/v1"
AGENT_KIT_LOCK_SCHEMA_VERSION = "reqpilotx-portable-agent-kit-lock/v1"
AGENT_RUNNER_PROFILE_SCHEMA_VERSION = "reqpilotx-agent-runner-profile/v1"


class AgentKitValidationError(RuntimeError):
    pass


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AgentKitSkillSpecV1(_StrictFrozenModel):
    skill_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    stage: AgentExtractionStage | None = None
    capability_for_stages: tuple[AgentExtractionStage, ...] = ()
    path: str = Field(pattern=r"^\.agents/skills/[a-z0-9-]+/SKILL\.md$")
    provenance_path: str | None = Field(
        default=None,
        pattern=r"^provenance/[a-z0-9.-]+\.json$",
    )

    @model_validator(mode="after")
    def validate_role(self) -> AgentKitSkillSpecV1:
        indices = tuple(
            AGENT_EXTRACTION_STAGE_ORDER.index(stage)
            for stage in self.capability_for_stages
        )
        if indices != tuple(sorted(set(indices))):
            raise ValueError("capability Skill stages must be unique and ordered")
        if (self.stage is None) == (not self.capability_for_stages):
            raise ValueError(
                "a Skill must be either one stage owner or a declared capability"
            )
        if self.stage is not None and self.provenance_path is not None:
            raise ValueError("stage-owner Skills cannot claim upstream provenance")
        return self


class AgentKitSkillProvenanceV1(_StrictFrozenModel):
    """Auditable origin for a vendored third-party capability Skill."""

    schema_version: Literal["reqpilotx-agent-kit-skill-provenance/v1"] = (
        "reqpilotx-agent-kit-skill-provenance/v1"
    )
    skill_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    distribution_id: str = Field(pattern=r"^[a-z0-9-]+@[a-z0-9-]+$")
    distribution_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){2,3}$")
    source_url: str = Field(pattern=r"^https://chatgpt\.com/plugins/share/[a-z0-9@-]+$")
    author: str = Field(min_length=1, max_length=120)
    license: str = Field(min_length=1, max_length=80)
    runtime_bundle_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){2,3}$")
    vendored_skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upstream_skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upstream_plugin_manifest_path: str = Field(
        pattern=r"^provenance/[a-z0-9.-]+\.json$"
    )
    upstream_plugin_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upstream_openai_yaml_path: str = Field(
        pattern=r"^\.agents/skills/[a-z0-9-]+/agents/openai\.yaml$"
    )
    upstream_openai_yaml_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upstream_operation_helper_path: str = Field(
        pattern=(
            r"^\.agents/skills/[a-z0-9-]+/container_tools/"
            r"mark_artifact_operation_started\.mjs$"
        )
    )
    upstream_operation_helper_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> AgentKitSkillProvenanceV1:
        if self.vendored_skill_sha256 != self.upstream_skill_sha256:
            raise ValueError("vendored Skill must exactly match its upstream digest")
        return self


class AgentKitRunnerOutputSchemaVariantV1(_StrictFrozenModel):
    execution_mode: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    schema_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}/v[0-9]+$")
    schema_path: str = Field(pattern=r"^schemas/[a-z0-9.-]+\.schema\.json$")


class AgentKitStageSpecV1(_StrictFrozenModel):
    stage: AgentExtractionStage
    owner: Literal["HOST", "PRIMARY_AGENT", "COLD_VERIFIER"]
    implementation_status: Literal["AVAILABLE", "PLANNED"]
    artifact_schema_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}/v[0-9]+$")
    artifact_schema_path: str | None = Field(
        default=None,
        pattern=r"^schemas/[a-z0-9.-]+\.schema\.json$",
    )
    runner_output_schema_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,119}/v[0-9]+$",
    )
    runner_output_schema_path: str | None = Field(
        default=None,
        pattern=r"^schemas/[a-z0-9.-]+\.schema\.json$",
    )
    runner_output_schema_variants: tuple[AgentKitRunnerOutputSchemaVariantV1, ...] = ()
    skill_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,119}$",
    )
    capability_skill_ids: tuple[str, ...] = ()
    reference_paths: tuple[str, ...] = ()
    validator_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,119}$",
    )
    validator_version: str | None = Field(
        default=None,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$",
    )

    @field_validator("reference_paths")
    @classmethod
    def validate_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            re.fullmatch(r"references/[a-z0-9-]+\.md", item) is None for item in value
        ):
            raise ValueError("stage references must be unique sorted Kit references")
        return value

    @field_validator("capability_skill_ids")
    @classmethod
    def validate_capability_skill_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            re.fullmatch(r"[a-z][a-z0-9-]{0,119}", item) is None for item in value
        ):
            raise ValueError("stage capability Skill IDs must be unique and sorted")
        return value

    @field_validator("runner_output_schema_variants")
    @classmethod
    def validate_runner_output_schema_variants(
        cls,
        value: tuple[AgentKitRunnerOutputSchemaVariantV1, ...],
    ) -> tuple[AgentKitRunnerOutputSchemaVariantV1, ...]:
        modes = tuple(item.execution_mode for item in value)
        schema_ids = tuple(item.schema_id for item in value)
        schema_paths = tuple(item.schema_path for item in value)
        if modes != tuple(sorted(set(modes))):
            raise ValueError(
                "runner-output schema variants require unique sorted modes"
            )
        if len(schema_ids) != len(set(schema_ids)) or len(schema_paths) != len(
            set(schema_paths)
        ):
            raise ValueError("runner-output schema variant identities must be unique")
        return value

    @model_validator(mode="after")
    def validate_stage_implementation(self) -> AgentKitStageSpecV1:
        implementation_fields = (
            self.artifact_schema_path,
            self.validator_id,
            self.validator_version,
        )
        runner_fields = (
            self.runner_output_schema_id,
            self.runner_output_schema_path,
        )
        if any(runner_fields) and not all(runner_fields):
            raise ValueError("stage runner-output schema identity must be complete")
        if self.skill_id is not None and self.skill_id in self.capability_skill_ids:
            raise ValueError("stage owner Skill cannot also be a capability Skill")
        if self.owner == "HOST" and self.capability_skill_ids:
            raise ValueError("Host-owned stages cannot bind Agent capability Skills")
        if self.runner_output_schema_variants and (
            self.owner == "HOST" or not all(runner_fields)
        ):
            raise ValueError(
                "runner-output schema variants require an Agent stage default schema"
            )
        if self.runner_output_schema_id in {
            item.schema_id for item in self.runner_output_schema_variants
        } or self.runner_output_schema_path in {
            item.schema_path for item in self.runner_output_schema_variants
        }:
            raise ValueError(
                "runner-output schema variants must differ from the default"
            )
        if self.implementation_status == "AVAILABLE" and not all(implementation_fields):
            raise ValueError("available stages require schema and validator identity")
        if (
            self.implementation_status == "AVAILABLE"
            and self.owner != "HOST"
            and not all(runner_fields)
        ):
            raise ValueError("available Agent stages require runner-output schema")
        if (
            self.implementation_status == "PLANNED"
            and any(implementation_fields)
            and not all(implementation_fields)
        ):
            raise ValueError(
                "planned stage contracts must be absent or fully specified"
            )
        return self


class AgentKitParitySpecV1(_StrictFrozenModel):
    reference_profile_id: Literal["desktop-reference"] = "desktop-reference"
    product_profile_id: Literal["app-server-product"] = "app-server-product"
    policy_path: Literal["runtime/parity-policy.json"] = "runtime/parity-policy.json"


class AgentKitToolSpecV2(_StrictFrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
    stages: tuple[AgentExtractionStage, ...]
    required_by_stages: tuple[AgentExtractionStage, ...]
    effect: Literal["READ_ONLY", "TEMPORARY_ASSET", "VALIDATION_ONLY"]
    authority: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    implementation_status: Literal["AVAILABLE", "PLANNED"]
    implementation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,119}$",
    )
    implementation_version: str | None = Field(
        default=None,
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$",
    )
    input_schema_path: str | None = Field(
        default=None,
        pattern=r"^schemas/[a-z0-9.-]+\.schema\.json$",
    )
    output_schema_path: str | None = Field(
        default=None,
        pattern=r"^schemas/[a-z0-9.-]+\.schema\.json$",
    )
    idempotency: Literal[
        "SOURCE_BOUND_READ",
        "SOURCE_BOUND_RENDER",
        "SOURCE_BOUND_VALIDATION",
    ]
    budget_policy: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    error_codes: tuple[str, ...]

    @field_validator("stages")
    @classmethod
    def validate_stage_order(
        cls,
        value: tuple[AgentExtractionStage, ...],
    ) -> tuple[AgentExtractionStage, ...]:
        indices = tuple(AGENT_EXTRACTION_STAGE_ORDER.index(stage) for stage in value)
        if not value or indices != tuple(sorted(set(indices))):
            raise ValueError("tool stages must be non-empty, unique, and ordered")
        return value

    @field_validator("required_by_stages")
    @classmethod
    def validate_required_stage_order(
        cls,
        value: tuple[AgentExtractionStage, ...],
    ) -> tuple[AgentExtractionStage, ...]:
        indices = tuple(AGENT_EXTRACTION_STAGE_ORDER.index(stage) for stage in value)
        if indices != tuple(sorted(set(indices))):
            raise ValueError("required tool stages must be unique and ordered")
        return value

    @field_validator("error_codes")
    @classmethod
    def validate_error_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("tool error codes must be unique and sorted")
        if any(re.fullmatch(r"[a-z][a-z0-9_]{0,159}", item) is None for item in value):
            raise ValueError("tool error codes must be content-free snake_case")
        return value

    @model_validator(mode="after")
    def validate_implementation(self) -> AgentKitToolSpecV2:
        if not set(self.required_by_stages).issubset(self.stages):
            raise ValueError("required tool stages must stay inside declared stages")
        available_fields = (
            self.implementation_id,
            self.implementation_version,
            self.input_schema_path,
            self.output_schema_path,
        )
        if self.implementation_status == "AVAILABLE" and not all(available_fields):
            raise ValueError(
                "available tools require implementation and schema identity"
            )
        if self.implementation_status == "PLANNED" and any(available_fields):
            raise ValueError("planned tools cannot claim an implementation")
        return self


class AgentKitToolRegistryV2(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-tool-registry/v2"] = "reqpilotx-tool-registry/v2"
    tool_registry_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    tool_registry_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    raw_paths_exposed: Literal[False] = False
    business_writes_exposed: Literal[False] = False
    tools: tuple[AgentKitToolSpecV2, ...]

    @field_validator("tools")
    @classmethod
    def validate_tools(
        cls,
        value: tuple[AgentKitToolSpecV2, ...],
    ) -> tuple[AgentKitToolSpecV2, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(set(names))):
            raise ValueError("Agent Kit tools must be unique and sorted")
        return value


class AgentKitManifestV1(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-portable-agent-kit/v1"] = (
        AGENT_KIT_MANIFEST_SCHEMA_VERSION
    )
    kit_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    kit_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    policy_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    policy_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    policy_path: str = Field(pattern=r"^policy/[a-z0-9.-]+\.json$")
    tool_registry_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    tool_registry_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    tool_registry_path: str = Field(pattern=r"^tool-contracts/[a-z0-9.-]+\.json$")
    agents: tuple[str, ...]
    stages: tuple[AgentKitStageSpecV1, ...]
    skills: tuple[AgentKitSkillSpecV1, ...]
    runner_profiles: tuple[str, ...]
    parity: AgentKitParitySpecV1

    @field_validator("agents", "runner_profiles")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("Agent Kit paths must be unique and sorted")
        if any(_unsafe_relative_path(item) for item in value):
            raise ValueError("Agent Kit paths must stay inside the bundle")
        return value

    @model_validator(mode="after")
    def validate_topology(self) -> AgentKitManifestV1:
        if tuple(item.stage for item in self.stages) != AGENT_EXTRACTION_STAGE_ORDER:
            raise ValueError("Agent Kit stages must cover D0-D6 in canonical order")
        skill_ids = tuple(item.skill_id for item in self.skills)
        if skill_ids != tuple(sorted(set(skill_ids))):
            raise ValueError("Agent Kit skills must be unique and sorted")
        stage_skill_ids = tuple(
            item.skill_id for item in self.stages if item.skill_id is not None
        )
        capability_skill_ids = tuple(
            skill_id
            for stage in self.stages
            for skill_id in stage.capability_skill_ids
        )
        if set(stage_skill_ids).intersection(capability_skill_ids):
            raise ValueError("stage owner and capability Skill identities must differ")
        if set(stage_skill_ids).union(capability_skill_ids) != set(skill_ids):
            raise ValueError("every declared Skill must belong to a stage closure")
        skills_by_id = {item.skill_id: item for item in self.skills}
        for stage in self.stages:
            if stage.skill_id is not None:
                owner = skills_by_id.get(stage.skill_id)
                if owner is None or owner.stage is not stage.stage:
                    raise ValueError("stage owner Skill identity drifted")
            for capability_id in stage.capability_skill_ids:
                capability = skills_by_id.get(capability_id)
                if (
                    capability is None
                    or capability.stage is not None
                    or stage.stage not in capability.capability_for_stages
                ):
                    raise ValueError("stage capability Skill identity drifted")
        for skill in self.skills:
            if skill.stage is not None:
                if skill.capability_for_stages:
                    raise ValueError("stage owner Skill cannot declare capability stages")
                continue
            referenced_stages = tuple(
                stage.stage
                for stage in self.stages
                if skill.skill_id in stage.capability_skill_ids
            )
            if referenced_stages != skill.capability_for_stages:
                raise ValueError("capability Skill stage closure drifted")
        if set(self.runner_profiles) != {
            "runtime/app-server-product.json",
            "runtime/desktop-discovery.json",
            "runtime/desktop-reference.json",
        }:
            raise ValueError(
                "Agent Kit requires discovery, reference, and product profiles"
            )
        return self


class AgentKitLockedFileV1(_StrictFrozenModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if _unsafe_relative_path(value) or value == "agent-kit.lock.json":
            raise ValueError("invalid locked Agent Kit path")
        return value


class AgentKitLockV1(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-portable-agent-kit-lock/v1"] = (
        AGENT_KIT_LOCK_SCHEMA_VERSION
    )
    kit_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    kit_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[AgentKitLockedFileV1, ...]

    @field_validator("files")
    @classmethod
    def validate_files(
        cls,
        value: tuple[AgentKitLockedFileV1, ...],
    ) -> tuple[AgentKitLockedFileV1, ...]:
        paths = tuple(item.path for item in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("Agent Kit lock paths must be unique and sorted")
        return value


class AgentRunnerProfileV1(_StrictFrozenModel):
    schema_version: Literal["reqpilotx-agent-runner-profile/v1"] = (
        AGENT_RUNNER_PROFILE_SCHEMA_VERSION
    )
    runner_profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    runner_kind: AgentRunnerKind
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    adapter_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    kit_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    kit_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    skill_ids: tuple[str, ...]
    tool_registry_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    tool_registry_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    domain_logic_source: Literal["PORTABLE_AGENT_KIT"] = "PORTABLE_AGENT_KIT"
    load_ambient_user_skills: bool
    load_conversation_history: bool
    allow_manual_intervention: bool
    quality_gate_eligible: bool

    @field_validator("skill_ids")
    @classmethod
    def validate_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or not value:
            raise ValueError("runner skill IDs must be non-empty, unique, and sorted")
        return value

    @model_validator(mode="after")
    def validate_gate_isolation(self) -> AgentRunnerProfileV1:
        if self.quality_gate_eligible and (
            self.load_ambient_user_skills
            or self.load_conversation_history
            or self.allow_manual_intervention
        ):
            raise ValueError("quality-gate runners must be hermetic cold runs")
        return self


@dataclass(frozen=True, slots=True)
class PortableAgentKit:
    root: Path
    manifest: AgentKitManifestV1
    lock: AgentKitLockV1
    identity: AgentKitIdentityV1
    tool_registry: AgentKitToolRegistryV2
    tool_schema_sha256: dict[str, tuple[str, str]]
    runner_profiles: dict[str, AgentRunnerProfileV1]
    runner_profile_sha256: dict[str, str]
    skill_provenance: dict[str, AgentKitSkillProvenanceV1]

    def runner_profile(self, profile_id: str) -> AgentRunnerProfileV1:
        try:
            return self.runner_profiles[profile_id]
        except KeyError as exc:
            raise AgentKitValidationError(
                f"unknown Agent Kit runner profile: {profile_id}"
            ) from exc

    def required_tools_for_stage(
        self,
        stage: AgentExtractionStage,
    ) -> tuple[AgentKitToolSpecV2, ...]:
        return tuple(
            tool
            for tool in self.tool_registry.tools
            if stage in tool.required_by_stages
        )

    def capability_skills_for_stage(
        self,
        stage: AgentExtractionStage,
    ) -> tuple[AgentKitSkillSpecV1, ...]:
        stage_spec = next(
            (item for item in self.manifest.stages if item.stage is stage),
            None,
        )
        if stage_spec is None:
            raise AgentKitValidationError(f"unknown Agent Kit stage: {stage.value}")
        skills_by_id = {item.skill_id: item for item in self.manifest.skills}
        return tuple(
            skills_by_id[skill_id]
            for skill_id in stage_spec.capability_skill_ids
        )


def load_portable_agent_kit(root: Path) -> PortableAgentKit:
    root = _validated_root(root)
    manifest_path = root / "manifest.json"
    lock_path = root / "agent-kit.lock.json"
    manifest_bytes = _read_regular_file(root, manifest_path)
    lock_bytes = _read_regular_file(root, lock_path)
    try:
        manifest = AgentKitManifestV1.model_validate_json(manifest_bytes)
        lock = AgentKitLockV1.model_validate_json(lock_bytes)
    except Exception as exc:
        raise AgentKitValidationError("Agent Kit manifest or lock is invalid") from exc
    if (lock.kit_id, lock.kit_version) != (manifest.kit_id, manifest.kit_version):
        raise AgentKitValidationError("Agent Kit manifest and lock identity drifted")
    computed_lock = compute_agent_kit_lock(root, manifest=manifest)
    if computed_lock != lock:
        raise AgentKitValidationError("Agent Kit files do not match the exact lock")
    skill_provenance = _validate_declared_files(root, manifest)
    tool_registry, tool_schema_sha256 = _load_tool_registry(root, manifest)
    profiles, profile_hashes = _load_runner_profiles(root, manifest)
    _validate_runner_parity(manifest, profiles)
    identity = AgentKitIdentityV1(
        kit_id=manifest.kit_id,
        kit_version=manifest.kit_version,
        manifest_sha256=_sha256(manifest_bytes),
        bundle_sha256=lock.bundle_sha256,
        policy_id=manifest.policy_id,
        policy_version=manifest.policy_version,
        tool_registry_id=manifest.tool_registry_id,
        tool_registry_version=manifest.tool_registry_version,
    )
    return PortableAgentKit(
        root=root,
        manifest=manifest,
        lock=lock,
        identity=identity,
        tool_registry=tool_registry,
        tool_schema_sha256=tool_schema_sha256,
        runner_profiles=profiles,
        runner_profile_sha256=profile_hashes,
        skill_provenance=skill_provenance,
    )


def compute_agent_kit_lock(
    root: Path,
    *,
    manifest: AgentKitManifestV1 | None = None,
) -> AgentKitLockV1:
    root = _validated_root(root)
    if manifest is None:
        try:
            manifest = AgentKitManifestV1.model_validate_json(
                _read_regular_file(root, root / "manifest.json")
            )
        except Exception as exc:
            raise AgentKitValidationError("Agent Kit manifest is invalid") from exc
    locked_files: list[AgentKitLockedFileV1] = []
    for path in sorted(root.rglob("*")):
        if path.name in {".DS_Store", "agent-kit.lock.json"}:
            continue
        if path.is_symlink():
            raise AgentKitValidationError(
                "Agent Kit cannot contain symlinks or devices"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise AgentKitValidationError(
                "Agent Kit cannot contain symlinks or devices"
            )
        relative = path.relative_to(root).as_posix()
        locked_files.append(
            AgentKitLockedFileV1(path=relative, sha256=_sha256(path.read_bytes()))
        )
    if not locked_files:
        raise AgentKitValidationError("Agent Kit cannot be empty")
    bundle_sha256 = _bundle_sha256(tuple(locked_files))
    return AgentKitLockV1(
        kit_id=manifest.kit_id,
        kit_version=manifest.kit_version,
        bundle_sha256=bundle_sha256,
        files=tuple(locked_files),
    )


def _validated_root(root: Path) -> Path:
    root = root.expanduser()
    if root.is_symlink():
        raise AgentKitValidationError("Agent Kit root must be a real directory")
    root = root.resolve()
    if not root.is_dir():
        raise AgentKitValidationError("Agent Kit root must be a real directory")
    return root


def _read_regular_file(root: Path, path: Path) -> bytes:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise AgentKitValidationError(
            "Agent Kit file is missing or escapes its root"
        ) from exc
    if path.is_symlink() or not resolved.is_file():
        raise AgentKitValidationError("Agent Kit files must be regular files")
    return resolved.read_bytes()


def _validate_declared_files(
    root: Path,
    manifest: AgentKitManifestV1,
) -> dict[str, AgentKitSkillProvenanceV1]:
    declared_paths = {
        "AGENTS.md",
        manifest.policy_path,
        manifest.tool_registry_path,
        manifest.parity.policy_path,
        *manifest.agents,
        *manifest.runner_profiles,
        *(skill.path for skill in manifest.skills),
        *(
            skill.provenance_path
            for skill in manifest.skills
            if skill.provenance_path is not None
        ),
        *(
            stage.artifact_schema_path
            for stage in manifest.stages
            if stage.artifact_schema_path is not None
        ),
        *(
            stage.runner_output_schema_path
            for stage in manifest.stages
            if stage.runner_output_schema_path is not None
        ),
        *(
            variant.schema_path
            for stage in manifest.stages
            for variant in stage.runner_output_schema_variants
        ),
        *(
            reference
            for stage in manifest.stages
            for reference in stage.reference_paths
        ),
    }
    for relative in declared_paths:
        _read_regular_file(root, root / relative)
    provenances: dict[str, AgentKitSkillProvenanceV1] = {}
    for skill in manifest.skills:
        text = (root / skill.path).read_text(encoding="utf-8")
        match = re.search(r"(?m)^name:\s*([^\r\n]+?)\s*$", text)
        declared_name = (
            match.group(1).strip().strip("\"'") if match is not None else None
        )
        if declared_name != skill.skill_id:
            raise AgentKitValidationError(
                f"Skill frontmatter identity drifted: {skill.skill_id}"
            )
        if skill.provenance_path is None:
            continue
        provenance_bytes = _read_regular_file(root, root / skill.provenance_path)
        try:
            provenance = AgentKitSkillProvenanceV1.model_validate_json(
                provenance_bytes
            )
        except Exception as exc:
            raise AgentKitValidationError(
                f"Skill provenance is invalid: {skill.skill_id}"
            ) from exc
        if provenance.skill_id != skill.skill_id:
            raise AgentKitValidationError("Skill provenance identity drifted")
        checks = (
            (skill.path, provenance.vendored_skill_sha256),
            (
                provenance.upstream_plugin_manifest_path,
                provenance.upstream_plugin_manifest_sha256,
            ),
            (
                provenance.upstream_openai_yaml_path,
                provenance.upstream_openai_yaml_sha256,
            ),
            (
                provenance.upstream_operation_helper_path,
                provenance.upstream_operation_helper_sha256,
            ),
        )
        for relative, expected_sha256 in checks:
            payload = _read_regular_file(root, root / relative)
            if _sha256(payload) != expected_sha256:
                raise AgentKitValidationError(
                    f"Skill provenance file drifted: {skill.skill_id}"
                )
        provenances[skill.skill_id] = provenance
    return provenances


def _load_runner_profiles(
    root: Path,
    manifest: AgentKitManifestV1,
) -> tuple[dict[str, AgentRunnerProfileV1], dict[str, str]]:
    profiles: dict[str, AgentRunnerProfileV1] = {}
    hashes: dict[str, str] = {}
    declared_skills = tuple(sorted(skill.skill_id for skill in manifest.skills))
    for relative in manifest.runner_profiles:
        payload = _read_regular_file(root, root / relative)
        try:
            profile = AgentRunnerProfileV1.model_validate_json(payload)
        except Exception as exc:
            raise AgentKitValidationError(
                f"Agent runner profile is invalid: {relative}"
            ) from exc
        if profile.runner_profile_id in profiles:
            raise AgentKitValidationError("duplicate Agent runner profile ID")
        if (profile.kit_id, profile.kit_version) != (
            manifest.kit_id,
            manifest.kit_version,
        ):
            raise AgentKitValidationError("runner profile Agent Kit identity drifted")
        if profile.skill_ids != declared_skills:
            raise AgentKitValidationError("runner profile skill bundle drifted")
        if profile.tool_registry_id != manifest.tool_registry_id:
            raise AgentKitValidationError("runner profile tool registry drifted")
        if profile.tool_registry_version != manifest.tool_registry_version:
            raise AgentKitValidationError(
                "runner profile tool registry version drifted"
            )
        profiles[profile.runner_profile_id] = profile
        hashes[profile.runner_profile_id] = _sha256(payload)
    return profiles, hashes


def _validate_runner_parity(
    manifest: AgentKitManifestV1,
    profiles: dict[str, AgentRunnerProfileV1],
) -> None:
    reference = profiles.get(manifest.parity.reference_profile_id)
    product = profiles.get(manifest.parity.product_profile_id)
    if reference is None or product is None:
        raise AgentKitValidationError("parity runner profiles are missing")
    if reference.runner_kind is not AgentRunnerKind.DESKTOP_REFERENCE:
        raise AgentKitValidationError("reference profile has the wrong runner kind")
    if product.runner_kind is not AgentRunnerKind.APP_SERVER_PRODUCT:
        raise AgentKitValidationError("product profile has the wrong runner kind")
    parity_fields = (
        "kit_id",
        "kit_version",
        "model_id",
        "reasoning_effort",
        "skill_ids",
        "tool_registry_id",
        "tool_registry_version",
        "domain_logic_source",
    )
    if any(
        getattr(reference, name) != getattr(product, name) for name in parity_fields
    ):
        raise AgentKitValidationError("reference and product runner capability drifted")
    if not reference.quality_gate_eligible or not product.quality_gate_eligible:
        raise AgentKitValidationError(
            "both parity runners must be quality-gate eligible"
        )


def _load_tool_registry(
    root: Path,
    manifest: AgentKitManifestV1,
) -> tuple[AgentKitToolRegistryV2, dict[str, tuple[str, str]]]:
    payload = _read_regular_file(root, root / manifest.tool_registry_path)
    try:
        registry = AgentKitToolRegistryV2.model_validate_json(payload)
    except Exception as exc:
        raise AgentKitValidationError("Agent Kit tool registry is invalid") from exc
    if (
        registry.tool_registry_id != manifest.tool_registry_id
        or registry.tool_registry_version != manifest.tool_registry_version
    ):
        raise AgentKitValidationError("Agent Kit tool registry identity drifted")
    schema_hashes: dict[str, tuple[str, str]] = {}
    for tool in registry.tools:
        if tool.implementation_status != "AVAILABLE":
            continue
        assert tool.input_schema_path is not None
        assert tool.output_schema_path is not None
        input_payload = _read_regular_file(root, root / tool.input_schema_path)
        output_payload = _read_regular_file(root, root / tool.output_schema_path)
        try:
            input_schema = json.loads(input_payload)
            output_schema = json.loads(output_payload)
        except json.JSONDecodeError as exc:
            raise AgentKitValidationError(
                "Agent Kit tool schema is invalid JSON"
            ) from exc
        if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
            raise AgentKitValidationError("Agent Kit tool schemas must be JSON objects")
        schema_hashes[tool.name] = (_sha256(input_payload), _sha256(output_payload))
    d1_tools = tuple(
        tool
        for tool in registry.tools
        if AgentExtractionStage.DOCUMENT_UNDERSTANDING in tool.required_by_stages
    )
    if not d1_tools or any(
        tool.implementation_status != "AVAILABLE" for tool in d1_tools
    ):
        raise AgentKitValidationError("D1 required tool closure is incomplete")
    return registry, schema_hashes


def _unsafe_relative_path(value: str) -> bool:
    path = Path(value)
    return path.is_absolute() or ".." in path.parts or value in {"", "."}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bundle_sha256(files: tuple[AgentKitLockedFileV1, ...]) -> str:
    payload = json.dumps(
        [item.model_dump(mode="json") for item in files],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "AGENT_KIT_LOCK_SCHEMA_VERSION",
    "AGENT_KIT_MANIFEST_SCHEMA_VERSION",
    "AGENT_RUNNER_PROFILE_SCHEMA_VERSION",
    "AgentKitLockV1",
    "AgentKitManifestV1",
    "AgentKitRunnerOutputSchemaVariantV1",
    "AgentKitSkillSpecV1",
    "AgentKitSkillProvenanceV1",
    "AgentKitToolRegistryV2",
    "AgentKitToolSpecV2",
    "AgentKitValidationError",
    "AgentRunnerProfileV1",
    "PortableAgentKit",
    "compute_agent_kit_lock",
    "load_portable_agent_kit",
]
