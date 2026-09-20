from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from app.adapters.agents.codex_app_server.agent_native_read_only_runtime import (
    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_DESCRIPTION,
    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID,
    AgentNativeReadOnlyRuntimePolicyV1,
    agent_native_read_confinement_mode,
    configure_agent_native_read_only_runtime,
    validate_agent_native_effective_config,
)
from app.adapters.agents.codex_app_server.lifecycle import (
    CanaryRuntimeState,
    LifecycleProbeError,
)


_EFFECTIVE_READABLE_ROOT = Path("/private/reqpilotx/session-root")


class AgentNativeReadOnlyRuntimeTests(unittest.TestCase):
    def test_prefers_permission_profile_and_removes_legacy_sandbox_override(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            runtime = replace(
                _runtime(state),
                command=(
                    "codex",
                    "app-server",
                    "--config",
                    'sandbox_mode="read-only"',
                ),
            )

            configured = configure_agent_native_read_only_runtime(
                runtime,
                readable_root=runtime.sterile_cwd,
            )

            command = configured.runtime.command
            self.assertIn("features.shell_tool=true", command)
            self.assertIn("features.unified_exec=true", command)
            self.assertIn("features.view_image=true", command)
            self.assertNotIn('sandbox_mode="read-only"', command)
            self.assertIn('web_search="disabled"', command)
            self.assertIn(
                'default_permissions="reqpilotx-document-read"',
                command,
            )
            self.assertIn(
                "permissions.reqpilotx-document-read.description="
                '"ReqPilotX document read-only session"',
                command,
            )
            self.assertIn(
                "permissions.reqpilotx-document-read.workspace_roots="
                f'{{"{runtime.sterile_cwd.resolve()}"=true}}',
                command,
            )
            self.assertIn(
                "permissions.reqpilotx-document-read.filesystem="
                '{":minimal"="read",":workspace_roots"={"."="read"}}',
                command,
            )
            self.assertIn(
                "permissions.reqpilotx-document-read.network.enabled=false",
                command,
            )
            self.assertEqual(
                configured.turn_sandbox_policy,
                {
                    "type": "readOnly",
                    "access": {
                        "type": "restricted",
                        "includePlatformDefaults": True,
                        "readableRoots": [str(runtime.sterile_cwd.resolve())],
                    },
                },
            )
            self.assertTrue(configured.policy.shell_tool_enabled)
            self.assertTrue(configured.policy.unified_exec_enabled)
            self.assertTrue(configured.policy.view_image_enabled)
            self.assertFalse(configured.policy.network_access)
            self.assertEqual(configured.confinement_mode, "PERMISSION_PROFILE")
            self.assertEqual(
                configured.permission_profile_id,
                AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID,
            )
            serialized = configured.policy.model_dump_json()
            self.assertNotIn(str(runtime.sterile_cwd), serialized)

    def test_falls_back_to_future_restricted_root_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(
                Path(raw),
                permission_profiles=False,
                restricted_read_access=True,
            )

            configured = configure_agent_native_read_only_runtime(
                runtime,
                readable_root=runtime.sterile_cwd,
            )

            self.assertEqual(
                configured.confinement_mode,
                "READ_ONLY_RESTRICTED_ROOT",
            )
            self.assertIsNone(configured.permission_profile_id)
            self.assertIn('sandbox_mode="read-only"', configured.runtime.command)
            self.assertNotIn(
                'default_permissions="reqpilotx-document-read"',
                configured.runtime.command,
            )

    def test_rejects_readable_root_outside_private_session_or_cwd_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            runtime = _runtime(state)
            outside = state.parent / f"outside-{state.name}"
            outside.mkdir()
            try:
                with self.assertRaisesRegex(
                    LifecycleProbeError,
                    "AGENT_NATIVE_READABLE_ROOT_OUTSIDE_SESSION",
                ):
                    configure_agent_native_read_only_runtime(
                        runtime,
                        readable_root=outside,
                    )
                nested = runtime.sterile_cwd / "nested"
                nested.mkdir()
                with self.assertRaisesRegex(
                    LifecycleProbeError,
                    "AGENT_NATIVE_CWD_ROOT_MISMATCH",
                ):
                    configure_agent_native_read_only_runtime(
                        runtime,
                        readable_root=nested,
                    )
            finally:
                outside.rmdir()

    def test_protocol_without_restricted_read_capability_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            runtime = replace(
                runtime,
                protocol={
                    "codex_version": "current-without-read-access",
                    "permission_profiles": "false",
                    "restricted_read_access": "false",
                },
            )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "AGENT_NATIVE_RESTRICTED_READ_UNSUPPORTED",
            ):
                configure_agent_native_read_only_runtime(
                    runtime,
                    readable_root=runtime.sterile_cwd,
                )

    def test_effective_config_requires_native_inspection_and_closed_ambient_tools(
        self,
    ) -> None:
        payload = _effective_config(permission_profile=True)
        validate_agent_native_effective_config(
            payload,
            confinement_mode="PERMISSION_PROFILE",
            permission_profile_id=AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID,
            expected_readable_root=_EFFECTIVE_READABLE_ROOT,
        )

        disabled = _effective_config(permission_profile=True)
        disabled["config"]["features"]["unified_exec"] = False
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "AGENT_NATIVE_INSPECTION_CAPABILITY_DISABLED",
        ):
            validate_agent_native_effective_config(
                disabled,
                confinement_mode="PERMISSION_PROFILE",
                permission_profile_id=(
                    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
                ),
                expected_readable_root=_EFFECTIVE_READABLE_ROOT,
            )

        ambient = _effective_config(permission_profile=True)
        ambient["config"]["features"]["apps"] = True
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "AGENT_NATIVE_AMBIENT_CAPABILITY_ENABLED",
        ):
            validate_agent_native_effective_config(
                ambient,
                confinement_mode="PERMISSION_PROFILE",
                permission_profile_id=(
                    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
                ),
                expected_readable_root=_EFFECTIVE_READABLE_ROOT,
            )

        unexpected_mcp = _effective_config(permission_profile=True)
        unexpected_mcp["config"]["mcp_servers"] = {"unknown": {}}
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "AGENT_NATIVE_MCP_CLOSURE_MISMATCH",
        ):
            validate_agent_native_effective_config(
                unexpected_mcp,
                confinement_mode="PERMISSION_PROFILE",
                permission_profile_id=(
                    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
                ),
                expected_readable_root=_EFFECTIVE_READABLE_ROOT,
            )

    def test_effective_permission_profile_is_exact_and_read_only(self) -> None:
        valid = _effective_config(permission_profile=True)
        validate_agent_native_effective_config(
            valid,
            confinement_mode="PERMISSION_PROFILE",
            permission_profile_id=AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID,
            expected_readable_root=_EFFECTIVE_READABLE_ROOT,
        )

        wrong_default = _effective_config(permission_profile=True)
        wrong_default["config"]["default_permissions"] = ":workspace"
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT",
        ):
            validate_agent_native_effective_config(
                wrong_default,
                confinement_mode="PERMISSION_PROFILE",
                permission_profile_id=(
                    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
                ),
                expected_readable_root=_EFFECTIVE_READABLE_ROOT,
            )

        write_access = _effective_config(permission_profile=True)
        profile = write_access["config"]["permissions"][
            AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
        ]
        profile["filesystem"][":workspace_roots"] = {".": "write"}
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT",
        ):
            validate_agent_native_effective_config(
                write_access,
                confinement_mode="PERMISSION_PROFILE",
                permission_profile_id=(
                    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
                ),
                expected_readable_root=_EFFECTIVE_READABLE_ROOT,
            )

        network_access = _effective_config(permission_profile=True)
        profile = network_access["config"]["permissions"][
            AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
        ]
        profile["network"]["enabled"] = True
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "AGENT_NATIVE_PERMISSION_PROFILE_CONFIG_DRIFT",
        ):
            validate_agent_native_effective_config(
                network_access,
                confinement_mode="PERMISSION_PROFILE",
                permission_profile_id=(
                    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
                ),
                expected_readable_root=_EFFECTIVE_READABLE_ROOT,
            )

    def test_effective_restricted_root_fallback_remains_supported(self) -> None:
        validate_agent_native_effective_config(
            _effective_config(permission_profile=False),
            confinement_mode="READ_ONLY_RESTRICTED_ROOT",
        )
        with self.assertRaisesRegex(
            LifecycleProbeError,
            "AGENT_NATIVE_SECURITY_POLICY_DRIFT",
        ):
            validate_agent_native_effective_config(
                _effective_config(permission_profile=True),
                confinement_mode="READ_ONLY_RESTRICTED_ROOT",
                expected_readable_root=_EFFECTIVE_READABLE_ROOT,
            )

    def test_policy_rejects_confinement_identity_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            AgentNativeReadOnlyRuntimePolicyV1(
                readable_root_path_sha256="a" * 64,
                confinement_mode="PERMISSION_PROFILE",
            )
        with self.assertRaises(ValueError):
            AgentNativeReadOnlyRuntimePolicyV1(
                readable_root_path_sha256="a" * 64,
                confinement_mode="READ_ONLY_RESTRICTED_ROOT",
                permission_profile_id=(
                    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID
                ),
            )

    def test_protocol_selection_is_capability_driven_and_profile_first(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _runtime(Path(raw))
            self.assertEqual(
                agent_native_read_confinement_mode(runtime),
                "PERMISSION_PROFILE",
            )


def _runtime(
    state: Path,
    *,
    permission_profiles: bool = True,
    restricted_read_access: bool = True,
) -> CanaryRuntimeState:
    workspace = state / "session-root"
    workspace.mkdir(mode=0o700)
    codex_home = state / "codex-home"
    codex_home.mkdir(mode=0o700)
    sqlite_home = state / "sqlite"
    sqlite_home.mkdir(mode=0o700)
    log_dir = state / "logs"
    log_dir.mkdir(mode=0o700)
    tmp_dir = state / "tmp"
    tmp_dir.mkdir(mode=0o700)
    return CanaryRuntimeState(
        codex_home=codex_home,
        command=("codex", "app-server"),
        environment={},
        log_dir=log_dir,
        protocol={
            "codex_version": "test",
            "permission_profiles": str(permission_profiles).lower(),
            "restricted_read_access": str(restricted_read_access).lower(),
        },
        sqlite_home=sqlite_home,
        state_path=state,
        sterile_cwd=workspace,
        tmp_dir=tmp_dir,
    )


def _effective_config(*, permission_profile: bool) -> dict[str, object]:
    disabled = {
        "apps": False,
        "browser_use": False,
        "computer_use": False,
        "hooks": False,
        "image_generation": False,
        "in_app_browser": False,
        "multi_agent": False,
        "plugins": False,
        "remote_plugin": False,
        "skill_search": False,
        "tool_suggest": False,
        "workspace_dependencies": False,
    }
    config = {
        "features": {
            **disabled,
            "shell_tool": True,
            "unified_exec": True,
            "view_image": True,
        },
        "mcp_servers": {},
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access" if permission_profile else "read-only",
        "allow_login_shell": False,
        "web_search": "disabled",
    }
    if permission_profile:
        config.update(
            {
                "default_permissions": AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID,
                "permissions": {
                    AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_ID: {
                        "description": (
                            AGENT_NATIVE_DOCUMENT_READ_PERMISSION_PROFILE_DESCRIPTION
                        ),
                        "extends": None,
                        "workspace_roots": {str(_EFFECTIVE_READABLE_ROOT): True},
                        "filesystem": {
                            ":minimal": "read",
                            ":workspace_roots": {".": "read"},
                            "glob_scan_max_depth": None,
                        },
                        "network": {
                            "allow_local_binding": None,
                            "allow_upstream_proxy": None,
                            "dangerously_allow_all_unix_sockets": None,
                            "dangerously_allow_non_loopback_proxy": None,
                            "domains": None,
                            "enable_socks5": None,
                            "enable_socks5_udp": None,
                            "enabled": False,
                            "mitm": None,
                            "mode": None,
                            "proxy_url": None,
                            "socks_url": None,
                            "unix_sockets": None,
                        },
                    }
                },
            }
        )
    return {
        "config": {
            **config,
        }
    }


if __name__ == "__main__":
    unittest.main()
