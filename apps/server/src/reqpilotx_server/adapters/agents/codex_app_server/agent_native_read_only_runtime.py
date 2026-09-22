from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    LifecycleProbeError,
)
from reqpilotx_server.adapters.agents.codex_app_server.primary_runtime_pdf import (
    OpenAIPrimaryRuntimePdfDependencies,
)


AGENT_NATIVE_READ_ONLY_RUNTIME_POLICY_SCHEMA_VERSION = (
    "reqpilotx-agent-native-read-only-runtime-policy/v1"
)
AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID = "reqpilotx-document-read"
AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_DESCRIPTION = (
    "ReqPilotX document read-only session"
)

AgentNativeReadConfinementMode = Literal[
    "PERMISSION_PROFILE",
    "READ_ONLY_RESTRICTED_ROOT",
]


class AgentNativeReadOnlyRuntimePolicyV1(BaseModel):
    """Content-free proof of the capabilities needed to inspect a source workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["reqpilotx-agent-native-read-only-runtime-policy/v1"] = (
        AGENT_NATIVE_READ_ONLY_RUNTIME_POLICY_SCHEMA_VERSION
    )
    shell_tool_enabled: Literal[True] = True
    unified_exec_enabled: Literal[True] = True
    view_image_enabled: Literal[True] = True
    local_image_input_enabled: Literal[True] = True
    network_access: Literal[False] = False
    filesystem_access: Literal[
        "SESSION_ROOT_READ_ONLY",
        "SESSION_ROOT_READ_ONLY_WITH_ATTESTED_DEPENDENCIES",
    ] = "SESSION_ROOT_READ_ONLY"
    approval_policy: Literal["never"] = "never"
    login_shell_allowed: Literal[False] = False
    ambient_apps_enabled: Literal[False] = False
    ambient_plugins_enabled: Literal[False] = False
    ambient_multi_agent_enabled: Literal[False] = False
    readable_root_path_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    additional_readable_root_path_sha256s: tuple[str, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    confinement_mode: AgentNativeReadConfinementMode = "READ_ONLY_RESTRICTED_ROOT"
    permission_profile_id: Literal["reqpilotx-document-read"] | None = None

    @model_validator(mode="after")
    def _validate_confinement_identity(self) -> AgentNativeReadOnlyRuntimePolicyV1:
        if self.confinement_mode == "PERMISSION_PROFILE":
            if (
                self.permission_profile_id
                != AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
            ):
                raise ValueError(
                    "permission-profile confinement requires its pinned id"
                )
        elif self.permission_profile_id is not None:
            raise ValueError("restricted-root confinement cannot claim a profile id")
        if self.additional_readable_root_path_sha256s != tuple(
            sorted(set(self.additional_readable_root_path_sha256s))
        ) or any(
            len(value) != 64 or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in self.additional_readable_root_path_sha256s
        ):
            raise ValueError("additional readable-root identities must be sorted hashes")
        has_dependencies = bool(self.additional_readable_root_path_sha256s)
        if has_dependencies != (
            self.filesystem_access
            == "SESSION_ROOT_READ_ONLY_WITH_ATTESTED_DEPENDENCIES"
        ):
            raise ValueError("filesystem-access identity drifted from readable roots")
        return self


@dataclass(frozen=True, slots=True)
class ConfiguredAgentNativeReadOnlyRuntime:
    runtime: CanaryRuntimeState
    readable_root: Path
    additional_readable_roots: tuple[Path, ...]
    turn_sandbox_policy: Mapping[str, Any]
    policy: AgentNativeReadOnlyRuntimePolicyV1
    pdf_dependencies: OpenAIPrimaryRuntimePdfDependencies | None = None

    @property
    def confinement_mode(self) -> AgentNativeReadConfinementMode:
        return self.policy.confinement_mode

    @property
    def permission_profile_id(self) -> str | None:
        return self.policy.permission_profile_id


def configure_agent_native_read_only_runtime(
    runtime: CanaryRuntimeState,
    *,
    readable_root: Path,
    pdf_dependencies: OpenAIPrimaryRuntimePdfDependencies | None = None,
    evidence_root: Path | None = None,
) -> ConfiguredAgentNativeReadOnlyRuntime:
    """Enable native inspection while keeping commands inside one read-only root."""

    confinement_mode = agent_native_read_confinement_mode(runtime)
    if confinement_mode is None:
        raise LifecycleProbeError("AGENT_NATIVE_RESTRICTED_READ_UNSUPPORTED")
    state_path = runtime.state_path.resolve(strict=True)
    root = readable_root.resolve(strict=True)
    if readable_root.is_symlink() or not root.is_dir():
        raise LifecycleProbeError("AGENT_NATIVE_READABLE_ROOT_INVALID")
    try:
        root.relative_to(state_path)
    except ValueError as exc:
        raise LifecycleProbeError("AGENT_NATIVE_READABLE_ROOT_OUTSIDE_SESSION") from exc
    try:
        runtime.sterile_cwd.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise LifecycleProbeError("AGENT_NATIVE_CWD_ROOT_MISMATCH") from exc

    additional_roots: tuple[Path, ...] = ()
    if pdf_dependencies is not None:
        dependency_root = pdf_dependencies.dependencies_root.resolve(strict=True)
        if (
            pdf_dependencies.dependencies_root.is_symlink()
            or not dependency_root.is_dir()
            or hashlib.sha256(str(dependency_root).encode("utf-8")).hexdigest()
            != pdf_dependencies.attestation.dependency_root_path_sha256
        ):
            raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_READ_ROOT_INVALID")
        additional_roots = (dependency_root,)
    if evidence_root is not None:
        evidence = evidence_root.resolve(strict=True)
        expected_evidence = state_path.parent / 'workspace'
        # Native-loop sources are a sibling of its private runtime directory.
        # Never grant the runtime's log/SDK directories or an arbitrary parent.
        if (evidence_root.is_symlink() or expected_evidence.is_symlink() or not evidence.is_dir()
                or evidence != expected_evidence.resolve(strict=False)):
            raise LifecycleProbeError('AGENT_NATIVE_EVIDENCE_ROOT_INVALID')
        additional_roots = (*additional_roots, evidence)

    common_overrides = (
        "features.shell_tool=true",
        "features.unified_exec=true",
        "features.view_image=true",
        "features.shell_snapshot=false",
        "features.apps=false",
        "features.plugins=false",
        "features.multi_agent=false",
        'web_search="disabled"',
        'approval_policy="never"',
        "allow_login_shell=false",
    )
    if confinement_mode == "PERMISSION_PROFILE":
        profile_id = AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
        overrides = (
            *common_overrides,
            f'default_permissions="{profile_id}"',
            (
                f"permissions.{profile_id}.description="
                f"{_toml_string(AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_DESCRIPTION)}"
            ),
            (
                f"permissions.{profile_id}.workspace_roots="
                + "{"
                + ",".join(
                    f"{_toml_string(str(item))}=true"
                    for item in (root, *additional_roots)
                )
                + "}"
            ),
            (
                f"permissions.{profile_id}.filesystem="
                '{":minimal"="read",":workspace_roots"={"."="read"}}'
            ),
            f"permissions.{profile_id}.network.enabled=false",
        )
        command = _without_config_override(runtime.command, "sandbox_mode")
    else:
        profile_id = None
        overrides = (*common_overrides, 'sandbox_mode="read-only"')
        command = list(runtime.command)
    for override in overrides:
        command.extend(("--config", override))
    environment = dict(runtime.environment)
    if pdf_dependencies is not None:
        environment = pdf_dependencies.environment(environment)
        # The lifecycle runtime intentionally inherits no shell environment.
        # Supplying PATH to the server alone does not configure native commands.
        for name in ('PATH', 'PYTHONDONTWRITEBYTECODE', 'PYTHONNOUSERSITE'):
            command.extend(('--config', f'shell_environment_policy.set.{name}={_toml_string(environment[name])}'))
    configured = replace(runtime, command=tuple(command), environment=environment)
    sandbox_policy: Mapping[str, Any] = {
        "type": "readOnly",
        "access": {
            "type": "restricted",
            "includePlatformDefaults": True,
            "readableRoots": [str(item) for item in (root, *additional_roots)],
        },
    }
    policy = AgentNativeReadOnlyRuntimePolicyV1(
        confinement_mode=confinement_mode,
        permission_profile_id=profile_id,
        readable_root_path_sha256=hashlib.sha256(str(root).encode("utf-8")).hexdigest(),
        additional_readable_root_path_sha256s=tuple(
            sorted(
                hashlib.sha256(str(item).encode("utf-8")).hexdigest()
                for item in additional_roots
            )
        ),
        filesystem_access=(
            "SESSION_ROOT_READ_ONLY_WITH_ATTESTED_DEPENDENCIES"
            if additional_roots
            else "SESSION_ROOT_READ_ONLY"
        ),
    )
    return ConfiguredAgentNativeReadOnlyRuntime(
        runtime=configured,
        readable_root=root,
        additional_readable_roots=additional_roots,
        turn_sandbox_policy=sandbox_policy,
        policy=policy,
        pdf_dependencies=pdf_dependencies,
    )


def protocol_supports_restricted_read_access(runtime: CanaryRuntimeState) -> bool:
    """Whether either proved read-confinement contract can serve this runtime."""

    return agent_native_read_confinement_mode(runtime) is not None


def agent_native_read_confinement_mode(
    runtime: CanaryRuntimeState,
) -> AgentNativeReadConfinementMode | None:
    """Prefer permission profiles, retaining the future restricted-root fallback."""

    if runtime.protocol.get("permission_profiles") == "true":
        return "PERMISSION_PROFILE"
    if runtime.protocol.get("restricted_read_access") == "true":
        return "READ_ONLY_RESTRICTED_ROOT"
    return None


def validate_agent_native_effective_config(
    result: Mapping[str, Any],
    *,
    confinement_mode: AgentNativeReadConfinementMode = ("READ_ONLY_RESTRICTED_ROOT"),
    permission_profile_id: str | None = None,
    expected_readable_root: Path | None = None,
    expected_additional_readable_roots: tuple[Path, ...] = (),
    allowed_mcp_server_names: tuple[str, ...] = (),
    expected_shell_environment: Mapping[str, str] | None = None,
) -> None:
    """Fail closed unless the effective App Server config matches this profile."""

    config = result.get("config")
    if not isinstance(config, Mapping):
        raise LifecycleProbeError("AGENT_NATIVE_EFFECTIVE_CONFIG_INVALID")
    features = config.get("features")
    if not isinstance(features, Mapping):
        raise LifecycleProbeError("AGENT_NATIVE_EFFECTIVE_CONFIG_INVALID")
    if (
        features.get("shell_tool") is not True
        or features.get("unified_exec") is not True
        or features.get("view_image") is not True
    ):
        raise LifecycleProbeError("AGENT_NATIVE_INSPECTION_CAPABILITY_DISABLED")
    forbidden_enabled = {
        "apps",
        "browser_use",
        "computer_use",
        "hooks",
        "image_generation",
        "in_app_browser",
        "multi_agent",
        "plugins",
        "remote_plugin",
        "skill_search",
        "tool_suggest",
        "workspace_dependencies",
    }
    if any(features.get(name) is not False for name in forbidden_enabled):
        raise LifecycleProbeError("AGENT_NATIVE_AMBIENT_CAPABILITY_ENABLED")
    mcp_servers = config.get("mcp_servers", {})
    if not isinstance(mcp_servers, Mapping) or set(mcp_servers) != set(
        allowed_mcp_server_names
    ):
        raise LifecycleProbeError("AGENT_NATIVE_MCP_CLOSURE_MISMATCH")
    if (
        config.get("approval_policy") != "never"
        or config.get("allow_login_shell") is not False
        or config.get("web_search") not in {"disabled", None}
    ):
        raise LifecycleProbeError("AGENT_NATIVE_SECURITY_POLICY_DRIFT")
    if expected_shell_environment is not None:
        shell = config.get('shell_environment_policy')
        if (not isinstance(shell, Mapping) or shell.get('inherit') != 'none'
                or shell.get('set') != dict(expected_shell_environment)):
            raise LifecycleProbeError('AGENT_NATIVE_PDF_SHELL_ENVIRONMENT_DRIFT')
    if confinement_mode == "PERMISSION_PROFILE":
        _validate_permission_profile_effective_config(
            config,
            permission_profile_id=permission_profile_id,
            expected_readable_root=expected_readable_root,
            expected_additional_readable_roots=(
                expected_additional_readable_roots
            ),
        )
    elif confinement_mode == "READ_ONLY_RESTRICTED_ROOT":
        if (
            permission_profile_id is not None
            or config.get("sandbox_mode") != "read-only"
        ):
            raise LifecycleProbeError("AGENT_NATIVE_SECURITY_POLICY_DRIFT")
    else:
        raise LifecycleProbeError("AGENT_NATIVE_CONFINEMENT_MODE_INVALID")


def _validate_permission_profile_effective_config(
    config: Mapping[str, Any],
    *,
    permission_profile_id: str | None,
    expected_readable_root: Path | None,
    expected_additional_readable_roots: tuple[Path, ...],
) -> None:
    if permission_profile_id != AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID:
        raise LifecycleProbeError("AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT")
    profiles = config.get("permissions")
    if (
        config.get("default_permissions") != permission_profile_id
        or not isinstance(profiles, Mapping)
        or set(profiles) != {permission_profile_id}
    ):
        raise LifecycleProbeError("AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT")
    profile = profiles.get(permission_profile_id)
    if not isinstance(profile, Mapping):
        raise LifecycleProbeError("AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT")
    if expected_readable_root is None or not expected_readable_root.is_absolute():
        raise LifecycleProbeError("AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT")
    if (
        set(profile)
        != {"description", "extends", "filesystem", "network", "workspace_roots"}
        or profile.get("description")
        != AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_DESCRIPTION
        or profile.get("extends") is not None
        or profile.get("workspace_roots")
        != {
            str(item): True
            for item in (
                expected_readable_root,
                *expected_additional_readable_roots,
            )
        }
    ):
        raise LifecycleProbeError("AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT")
    filesystem = profile.get("filesystem")
    if (
        not isinstance(filesystem, Mapping)
        or set(filesystem) != {":minimal", ":workspace_roots", "glob_scan_max_depth"}
        or filesystem.get(":minimal") != "read"
        or filesystem.get(":workspace_roots") != {".": "read"}
        or filesystem.get("glob_scan_max_depth") is not None
    ):
        raise LifecycleProbeError("AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT")
    network = profile.get("network")
    if not isinstance(network, Mapping) or network.get("enabled") is not False:
        raise LifecycleProbeError("AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT")
    if set(network) != {
        "allow_local_binding",
        "allow_upstream_proxy",
        "dangerously_allow_all_unix_sockets",
        "dangerously_allow_non_loopback_proxy",
        "domains",
        "enable_socks5",
        "enable_socks5_udp",
        "enabled",
        "mitm",
        "mode",
        "proxy_url",
        "socks_url",
        "unix_sockets",
    } or any(value is not None for key, value in network.items() if key != "enabled"):
        raise LifecycleProbeError("AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT")


def _without_config_override(command: tuple[str, ...], key: str) -> list[str]:
    """Remove an obsolete CLI config pair before applying the selected profile."""

    filtered: list[str] = []
    index = 0
    while index < len(command):
        token = command[index]
        if token in {"--config", "-c"} and index + 1 < len(command):
            value = command[index + 1]
            if value.split("=", 1)[0].strip() == key:
                index += 2
                continue
            filtered.extend((token, value))
            index += 2
            continue
        filtered.append(token)
        index += 1
    return filtered


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = [
    "AGENT_NATIVE_READ_ONLY_RUNTIME_POLICY_SCHEMA_VERSION",
    "AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_DESCRIPTION",
    "AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID",
    "AgentNativeReadConfinementMode",
    "AgentNativeReadOnlyRuntimePolicyV1",
    "ConfiguredAgentNativeReadOnlyRuntime",
    "agent_native_read_confinement_mode",
    "configure_agent_native_read_only_runtime",
    "protocol_supports_restricted_read_access",
    "validate_agent_native_effective_config",
]
