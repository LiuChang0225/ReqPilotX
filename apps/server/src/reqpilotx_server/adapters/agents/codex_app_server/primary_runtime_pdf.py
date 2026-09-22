from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import LifecycleProbeError
from reqpilotx_server.adapters.agents.portable_kit import AgentKitSkillProvenanceV1


OPENAI_PRIMARY_RUNTIME_PDF_ATTESTATION_SCHEMA_VERSION = (
    "reqpilotx-openai-primary-runtime-pdf-attestation/v1"
)
OPENAI_PRIMARY_RUNTIME_PDF_ROOT_ENV = "REQPILOTX_OPENAI_PRIMARY_RUNTIME_ROOT"
OPENAI_PRIMARY_RUNTIME_PDF_DISTRIBUTION_ID = "pdf@openai-primary-runtime"
_REQUIRED_PYTHON_PACKAGES = ("pdfplumber", "pypdf", "reportlab")
_REQUIRED_RENDER_TOOLS = ("pdfinfo", "pdftoppm")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class OpenAIPrimaryRuntimePackageV1(_StrictFrozenModel):
    name: Literal["pdfplumber", "pypdf", "reportlab"]
    version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){1,3}(?:[a-z0-9.-]*)?$")


class OpenAIPrimaryRuntimeToolV1(_StrictFrozenModel):
    name: Literal["pdfinfo", "pdftoppm"]
    version: str = Field(min_length=1, max_length=80)
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OpenAIPrimaryRuntimePdfAttestationV1(_StrictFrozenModel):
    """Content-free proof of the official PDF Skill and its bundled runtime."""

    schema_version: Literal[
        "reqpilotx-openai-primary-runtime-pdf-attestation/v1"
    ] = OPENAI_PRIMARY_RUNTIME_PDF_ATTESTATION_SCHEMA_VERSION
    attestation_id: str = Field(pattern=r"^primary-runtime-pdf-[0-9a-f]{32}$")
    distribution_id: Literal["pdf@openai-primary-runtime"] = (
        OPENAI_PRIMARY_RUNTIME_PDF_DISTRIBUTION_ID
    )
    distribution_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){2,3}$")
    runtime_bundle_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){2,3}$")
    target_platform: str = Field(pattern=r"^[a-z0-9_-]+$")
    target_arch: str = Field(pattern=r"^[a-z0-9_-]+$")
    runtime_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vendored_skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_plugin_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_root_path_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){2,3}$")
    python_executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    python_packages: tuple[OpenAIPrimaryRuntimePackageV1, ...]
    render_tools: tuple[OpenAIPrimaryRuntimeToolV1, ...]
    read_only_session: Literal[True] = True
    dependency_installation_allowed: Literal[False] = False
    network_access: Literal[False] = False

    @model_validator(mode="after")
    def validate_closure(self) -> OpenAIPrimaryRuntimePdfAttestationV1:
        if self.distribution_version != self.runtime_bundle_version:
            raise ValueError("PDF plugin and primary runtime version drifted")
        if self.vendored_skill_sha256 != self.runtime_skill_sha256:
            raise ValueError("vendored PDF Skill drifted from primary runtime")
        if tuple(item.name for item in self.python_packages) != (
            "pdfplumber",
            "pypdf",
            "reportlab",
        ):
            raise ValueError("primary runtime PDF package closure drifted")
        if tuple(item.name for item in self.render_tools) != (
            "pdfinfo",
            "pdftoppm",
        ):
            raise ValueError("primary runtime PDF render-tool closure drifted")
        payload = self.model_dump(mode="json", exclude={"attestation_id"})
        expected = "primary-runtime-pdf-" + _canonical_sha256(payload)[:32]
        if self.attestation_id != expected:
            raise ValueError("primary runtime PDF attestation identity drifted")
        return self


@dataclass(frozen=True, slots=True)
class OpenAIPrimaryRuntimePdfDependencies:
    runtime_root: Path
    dependencies_root: Path
    python_executable: Path
    pdfinfo_executable: Path
    pdftoppm_executable: Path
    attestation: OpenAIPrimaryRuntimePdfAttestationV1

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        result = dict(base)
        existing_path = result.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        path_entries = (
            self.dependencies_root / "bin/override",
            self.dependencies_root / "python/bin",
            self.dependencies_root / "bin/fallback",
        )
        result.update(
            {
                "PATH": os.pathsep.join(
                    (*[str(item) for item in path_entries], existing_path)
                ),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
            }
        )
        return result


def discover_openai_primary_runtime_pdf_dependencies(
    *,
    provenance: AgentKitSkillProvenanceV1,
    runtime_root: Path | None = None,
) -> OpenAIPrimaryRuntimePdfDependencies:
    if (
        provenance.skill_id != "pdf"
        or provenance.distribution_id != OPENAI_PRIMARY_RUNTIME_PDF_DISTRIBUTION_ID
    ):
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_PROVENANCE_INVALID")
    root = _resolve_runtime_root(runtime_root)
    runtime_manifest_path = _regular_file(root / "runtime.json", root=root)
    runtime_manifest_bytes = runtime_manifest_path.read_bytes()
    try:
        runtime_manifest = json.loads(runtime_manifest_bytes)
    except json.JSONDecodeError as exc:
        raise LifecycleProbeError("PRIMARY_RUNTIME_MANIFEST_INVALID") from exc
    if (
        not isinstance(runtime_manifest, dict)
        or runtime_manifest.get("bundleVersion") != provenance.runtime_bundle_version
        or "plugins/openai-primary-runtime"
        not in runtime_manifest.get("bundledPlugins", [])
    ):
        raise LifecycleProbeError("PRIMARY_RUNTIME_VERSION_MISMATCH")

    dependencies_root = _real_directory(root / "dependencies", root=root)
    plugin_root = _real_directory(
        root / "plugins/openai-primary-runtime/plugins/pdf",
        root=root,
    )
    runtime_skill = _regular_file(plugin_root / "skills/pdf/SKILL.md", root=root)
    runtime_plugin_manifest = _regular_file(
        plugin_root / ".codex-plugin/plugin.json",
        root=root,
    )
    runtime_skill_sha256 = _sha256_file(runtime_skill)
    runtime_plugin_manifest_sha256 = _sha256_file(runtime_plugin_manifest)
    if (
        runtime_skill_sha256 != provenance.upstream_skill_sha256
        or runtime_plugin_manifest_sha256
        != provenance.upstream_plugin_manifest_sha256
    ):
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_PLUGIN_MISMATCH")

    python_executable = _regular_file(
        dependencies_root / "python/bin/python3",
        root=dependencies_root,
        allow_internal_symlink=True,
    )
    executables = {
        name: _regular_file(
            dependencies_root / f"bin/override/{name}",
            root=dependencies_root,
        )
        for name in _REQUIRED_RENDER_TOOLS
    }
    python_probe = _run_json_probe(
        (
            str(python_executable),
            "-I",
            "-B",
            "-c",
            (
                "import json,platform,pdfplumber,pypdf,reportlab;"
                "print(json.dumps({'python':platform.python_version(),"
                "'pdfplumber':pdfplumber.__version__,'pypdf':pypdf.__version__,"
                "'reportlab':reportlab.Version},sort_keys=True))"
            ),
        )
    )
    if set(python_probe) != {"python", *_REQUIRED_PYTHON_PACKAGES}:
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_PACKAGE_CLOSURE_INVALID")
    python_packages = tuple(
        OpenAIPrimaryRuntimePackageV1(
            name=name,  # type: ignore[arg-type]
            version=str(python_probe[name]),
        )
        for name in sorted(_REQUIRED_PYTHON_PACKAGES)
    )
    render_tools = tuple(
        OpenAIPrimaryRuntimeToolV1(
            name=name,  # type: ignore[arg-type]
            version=_probe_poppler_version(executables[name], name=name),
            executable_sha256=_sha256_file(executables[name]),
        )
        for name in sorted(_REQUIRED_RENDER_TOOLS)
    )
    provenance_sha256 = _canonical_sha256(provenance.model_dump(mode="json"))
    values = {
        "distribution_id": OPENAI_PRIMARY_RUNTIME_PDF_DISTRIBUTION_ID,
        "distribution_version": provenance.distribution_version,
        "runtime_bundle_version": str(runtime_manifest["bundleVersion"]),
        "target_platform": str(runtime_manifest.get("targetPlatform") or platform.system().lower()),
        "target_arch": str(runtime_manifest.get("targetArch") or platform.machine().lower()),
        "runtime_manifest_sha256": hashlib.sha256(runtime_manifest_bytes).hexdigest(),
        "provenance_sha256": provenance_sha256,
        "vendored_skill_sha256": provenance.vendored_skill_sha256,
        "runtime_skill_sha256": runtime_skill_sha256,
        "runtime_plugin_manifest_sha256": runtime_plugin_manifest_sha256,
        "dependency_root_path_sha256": _text_sha256(str(dependencies_root)),
        "python_version": str(python_probe["python"]),
        "python_executable_sha256": _sha256_file(python_executable),
        "python_packages": python_packages,
        "render_tools": render_tools,
        "read_only_session": True,
        "dependency_installation_allowed": False,
        "network_access": False,
    }
    draft = OpenAIPrimaryRuntimePdfAttestationV1.model_construct(
        attestation_id="primary-runtime-pdf-" + "0" * 32,
        **values,
    )
    payload = draft.model_dump(mode="json", exclude={"attestation_id"})
    attestation = OpenAIPrimaryRuntimePdfAttestationV1(
        attestation_id="primary-runtime-pdf-" + _canonical_sha256(payload)[:32],
        **values,
    )
    return OpenAIPrimaryRuntimePdfDependencies(
        runtime_root=root,
        dependencies_root=dependencies_root,
        python_executable=python_executable,
        pdfinfo_executable=executables["pdfinfo"],
        pdftoppm_executable=executables["pdftoppm"],
        attestation=attestation,
    )


def validate_openai_primary_runtime_pdf_environment(
    environment: Mapping[str, str],
    *,
    dependencies_root: Path,
    attestation: OpenAIPrimaryRuntimePdfAttestationV1,
) -> None:
    root = dependencies_root.resolve(strict=True)
    if _text_sha256(str(root)) != attestation.dependency_root_path_sha256:
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_ENVIRONMENT_MISMATCH")
    path_entries = environment.get("PATH", "").split(os.pathsep)
    expected_prefix = [
        str(root / "bin/override"),
        str(root / "python/bin"),
        str(root / "bin/fallback"),
    ]
    if (
        path_entries[:3] != expected_prefix
        or environment.get("PYTHONDONTWRITEBYTECODE") != "1"
        or environment.get("PYTHONNOUSERSITE") != "1"
    ):
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_ENVIRONMENT_MISMATCH")


def _resolve_runtime_root(value: Path | None) -> Path:
    candidate = value
    if candidate is None:
        configured = os.environ.get(OPENAI_PRIMARY_RUNTIME_PDF_ROOT_ENV, "").strip()
        candidate = (
            Path(configured)
            if configured
            else Path.home()
            / ".cache/codex-runtimes/codex-primary-runtime"
        )
    try:
        if candidate.is_symlink():
            raise LifecycleProbeError("PRIMARY_RUNTIME_ROOT_INVALID")
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise LifecycleProbeError("PRIMARY_RUNTIME_ROOT_UNAVAILABLE") from exc
    if not root.is_dir():
        raise LifecycleProbeError("PRIMARY_RUNTIME_ROOT_INVALID")
    return root


def _real_directory(path: Path, *, root: Path) -> Path:
    try:
        if path.is_symlink():
            raise LifecycleProbeError("PRIMARY_RUNTIME_PATH_INVALID")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise LifecycleProbeError("PRIMARY_RUNTIME_PATH_INVALID") from exc
    if not resolved.is_dir():
        raise LifecycleProbeError("PRIMARY_RUNTIME_PATH_INVALID")
    return resolved


def _regular_file(
    path: Path,
    *,
    root: Path,
    allow_internal_symlink: bool = False,
) -> Path:
    try:
        if path.is_symlink() and not allow_internal_symlink:
            raise LifecycleProbeError("PRIMARY_RUNTIME_PATH_INVALID")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise LifecycleProbeError("PRIMARY_RUNTIME_PATH_INVALID") from exc
    if not resolved.is_file():
        raise LifecycleProbeError("PRIMARY_RUNTIME_PATH_INVALID")
    return resolved


def _run_json_probe(command: tuple[str, ...]) -> dict[str, str]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        },
    )
    if completed.returncode != 0:
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_PACKAGE_PROBE_FAILED")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_PACKAGE_PROBE_FAILED") from exc
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        for key, value in payload.items()
    ):
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_PACKAGE_PROBE_FAILED")
    return payload


def _probe_poppler_version(path: Path, *, name: str) -> str:
    try:
        completed = subprocess.run(
            (str(path), "-v"), check=False, capture_output=True, text=True,
            timeout=30,
            env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        )
    except subprocess.TimeoutExpired as exc:
        raise LifecycleProbeError('PRIMARY_RUNTIME_PDF_RENDER_TOOL_PROBE_TIMEOUT') from exc
    output = (completed.stderr or completed.stdout).splitlines()
    if completed.returncode != 0 or not output:
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_RENDER_TOOL_PROBE_FAILED")
    match = re.fullmatch(rf"{re.escape(name)} version ([0-9][0-9A-Za-z.-]*)", output[0])
    if match is None:
        raise LifecycleProbeError("PRIMARY_RUNTIME_PDF_RENDER_TOOL_PROBE_FAILED")
    return match.group(1)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "OPENAI_PRIMARY_RUNTIME_PDF_ATTESTATION_SCHEMA_VERSION",
    "OPENAI_PRIMARY_RUNTIME_PDF_DISTRIBUTION_ID",
    "OPENAI_PRIMARY_RUNTIME_PDF_ROOT_ENV",
    "OpenAIPrimaryRuntimePackageV1",
    "OpenAIPrimaryRuntimePdfAttestationV1",
    "OpenAIPrimaryRuntimePdfDependencies",
    "OpenAIPrimaryRuntimeToolV1",
    "discover_openai_primary_runtime_pdf_dependencies",
    "validate_openai_primary_runtime_pdf_environment",
]
