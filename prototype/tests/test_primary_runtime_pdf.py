from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import shutil
import tempfile
import unittest

import fitz

from app.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from app.adapters.agents.codex_app_server.lifecycle import CanaryRuntimeState
from app.adapters.agents.codex_app_server.agent_native_read_only_runtime import (
    configure_agent_native_read_only_runtime,
)
from app.adapters.agents.codex_app_server.primary_runtime_pdf import (
    discover_openai_primary_runtime_pdf_dependencies,
    validate_openai_primary_runtime_pdf_environment,
)
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    SyntheticPdfCapabilityMode,
    build_pdf_capability_matrix,
)
from app.adapters.agents.portable_kit import AgentKitSkillProvenanceV1


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_KIT_ROOT = _REPOSITORY_ROOT / "agent-kit"


class OpenAIPrimaryRuntimePdfTests(unittest.TestCase):
    def test_capability_matrix_separates_native_scanned_and_hybrid_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture_root = Path(raw) / "fixtures"
            fixture_root.mkdir()
            fixtures = build_pdf_capability_matrix(fixture_root.resolve())

            self.assertEqual(
                tuple(item.mode for item in fixtures),
                (
                    SyntheticPdfCapabilityMode.NATIVE,
                    SyntheticPdfCapabilityMode.SCANNED,
                    SyntheticPdfCapabilityMode.HYBRID,
                ),
            )
            for fixture in fixtures:
                with fitz.open(fixture.path) as document:
                    native_text = "\n".join(page.get_text() for page in document)
                    self.assertEqual(len(document), 1)
                if fixture.mode is SyntheticPdfCapabilityMode.SCANNED:
                    self.assertFalse(native_text.strip())
                else:
                    assert fixture.native_marker is not None
                    self.assertIn(fixture.native_marker, native_text)
                if fixture.mode is SyntheticPdfCapabilityMode.HYBRID:
                    self.assertNotIn(fixture.visual_marker, native_text)

    def test_discovers_exact_official_plugin_and_dependency_closure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = _runtime_fixture(Path(raw))

            result = discover_openai_primary_runtime_pdf_dependencies(
                provenance=_provenance(),
                runtime_root=root,
            )

            self.assertEqual(
                result.attestation.distribution_id,
                "pdf@openai-primary-runtime",
            )
            self.assertEqual(result.attestation.runtime_bundle_version, _provenance().runtime_bundle_version)
            self.assertEqual(
                result.attestation.vendored_skill_sha256,
                result.attestation.runtime_skill_sha256,
            )
            self.assertEqual(
                {item.name: item.version for item in result.attestation.python_packages},
                {
                    "pdfplumber": "0.11.9",
                    "pypdf": "6.10.0",
                    "reportlab": "4.4.9",
                },
            )
            self.assertEqual(
                {item.name: item.version for item in result.attestation.render_tools},
                {"pdfinfo": "26.05.0", "pdftoppm": "26.05.0"},
            )
            environment = result.environment({"PATH": "/usr/bin:/bin"})
            validate_openai_primary_runtime_pdf_environment(
                environment,
                dependencies_root=result.dependencies_root,
                attestation=result.attestation,
            )
            self.assertNotIn(str(result.dependencies_root), result.attestation.model_dump_json())

    def test_rejects_runtime_plugin_drift_before_agent_start(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = _runtime_fixture(Path(raw))
            skill = (
                root
                / "plugins/openai-primary-runtime/plugins/pdf/skills/pdf/SKILL.md"
            )
            skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

            with self.assertRaisesRegex(
                LifecycleProbeError,
                "PRIMARY_RUNTIME_PDF_PLUGIN_MISMATCH",
            ):
                discover_openai_primary_runtime_pdf_dependencies(
                    provenance=_provenance(),
                    runtime_root=root,
                )

    def test_rejects_environment_that_falls_back_to_system_python(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = discover_openai_primary_runtime_pdf_dependencies(
                provenance=_provenance(),
                runtime_root=_runtime_fixture(Path(raw)),
            )
            with self.assertRaisesRegex(
                LifecycleProbeError,
                "PRIMARY_RUNTIME_PDF_ENVIRONMENT_MISMATCH",
            ):
                validate_openai_primary_runtime_pdf_environment(
                    {
                        "PATH": "/usr/bin:/bin",
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONNOUSERSITE": "1",
                    },
                    dependencies_root=result.dependencies_root,
                    attestation=result.attestation,
                )

    def test_configures_only_session_and_attested_dependency_read_roots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            dependency_root = state / "external-runtime"
            result = discover_openai_primary_runtime_pdf_dependencies(
                provenance=_provenance(),
                runtime_root=_runtime_fixture(dependency_root),
            )
            session_state = state / "session-state"
            session_root = session_state / "session"
            workspace = session_root / "agent-kit"
            for path in (
                workspace,
                session_state / "codex-home",
                session_state / "logs",
                session_state / "sqlite",
                session_state / "tmp",
            ):
                path.mkdir(parents=True)
            runtime = CanaryRuntimeState(
                codex_home=session_state / "codex-home",
                command=("codex", "app-server"),
                environment={"PATH": "/usr/bin:/bin"},
                log_dir=session_state / "logs",
                protocol={
                    "codex_version": "test",
                    "permission_profiles": "true",
                    "restricted_read_access": "true",
                },
                sqlite_home=session_state / "sqlite",
                state_path=session_state,
                sterile_cwd=workspace,
                tmp_dir=session_state / "tmp",
            )

            configured = configure_agent_native_read_only_runtime(
                runtime,
                readable_root=session_root,
                pdf_dependencies=result,
            )

            self.assertEqual(
                configured.turn_sandbox_policy["access"]["readableRoots"],
                [str(session_root.resolve()), str(result.dependencies_root)],
            )
            self.assertEqual(
                configured.policy.filesystem_access,
                "SESSION_ROOT_READ_ONLY_WITH_ATTESTED_DEPENDENCIES",
            )
            self.assertEqual(len(configured.policy.additional_readable_root_path_sha256s), 1)
            validate_openai_primary_runtime_pdf_environment(
                configured.runtime.environment,
                dependencies_root=result.dependencies_root,
                attestation=result.attestation,
            )


def _provenance() -> AgentKitSkillProvenanceV1:
    return AgentKitSkillProvenanceV1.model_validate_json(
        (_KIT_ROOT / "provenance/openai-primary-runtime-pdf.v1.json").read_bytes()
    )


def _runtime_fixture(parent: Path) -> Path:
    root = parent / "codex-primary-runtime"
    plugin = root / "plugins/openai-primary-runtime/plugins/pdf"
    skill = plugin / "skills/pdf"
    dependencies = root / "dependencies"
    (skill / "agents").mkdir(parents=True)
    (skill / "container_tools").mkdir()
    (plugin / ".codex-plugin").mkdir()
    (dependencies / "python/bin").mkdir(parents=True)
    (dependencies / "bin/override").mkdir(parents=True)
    (dependencies / "bin/fallback").mkdir(parents=True)
    shutil.copyfile(_KIT_ROOT / ".agents/skills/pdf/SKILL.md", skill / "SKILL.md")
    shutil.copyfile(
        _KIT_ROOT / ".agents/skills/pdf/agents/openai.yaml",
        skill / "agents/openai.yaml",
    )
    shutil.copyfile(
        _KIT_ROOT / ".agents/skills/pdf/container_tools/mark_artifact_operation_started.mjs",
        skill / "container_tools/mark_artifact_operation_started.mjs",
    )
    shutil.copyfile(
        _KIT_ROOT / "provenance/openai-primary-runtime-pdf.plugin.json",
        plugin / ".codex-plugin/plugin.json",
    )
    (root / "runtime.json").write_text(
        json.dumps(
            {
                "bundleVersion": _provenance().runtime_bundle_version,
                "bundledPlugins": ["plugins/openai-primary-runtime"],
                "targetPlatform": "test",
                "targetArch": "test",
            }
        ),
        encoding="utf-8",
    )
    python = dependencies / "python/bin/python3"
    python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"pdfplumber\":\"0.11.9\",\"pypdf\":\"6.10.0\","
        "\"python\":\"3.12.13\",\"reportlab\":\"4.4.9\"}'\n",
        encoding="utf-8",
    )
    python.chmod(0o700)
    for name in ("pdfinfo", "pdftoppm"):
        tool = dependencies / f"bin/override/{name}"
        tool.write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{name} version 26.05.0' >&2\n",
            encoding="utf-8",
        )
        tool.chmod(0o700)
    return root


if __name__ == "__main__":
    unittest.main()
