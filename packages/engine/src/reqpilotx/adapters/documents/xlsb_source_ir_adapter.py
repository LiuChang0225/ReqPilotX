from __future__ import annotations

import hashlib
import json
import os
import platform
import posixpath
import re
import signal
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from io import BytesIO
from os import PathLike
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable
from urllib.parse import urlsplit

from lxml import etree
from openpyxl import load_workbook

from reqpilotx.adapters.documents.preflight import LocalDocumentPreflight
from reqpilotx.adapters.documents.spreadsheet_visual_profile import (
    LibreOfficeSpreadsheetVisualRenderer,
    SpreadsheetExpectedSheet,
    SpreadsheetExternalVisualRelationship,
    SpreadsheetVisualProfile,
    SpreadsheetVisualProfileRejected,
    SpreadsheetVisualRendererPort,
)
from reqpilotx.adapters.documents.xlsx_source_ir_adapter import (
    XlsxSourceIRAdapter,
    _XlsxResourceBudgetExceeded,
)
from reqpilotx.domain.extraction.preflight import DocumentFormat, DocumentPreflightReport
from reqpilotx.domain.extraction.source_ir import (
    SourceConversionProvenance,
    SourceConversionSecurityConfig,
    SourceAssetRef,
    SourceDiagnostic,
    SourceDiagnosticSeverity,
    SourceIRSnapshot,
    SourceIRStatus,
    stable_document_id,
    stable_source_ir_id,
)
from reqpilotx.ports.source_ir_adapter import (
    SourceIRAdapterRejected,
    SourceIRAdapterRejectionCode,
)


XLSB_SOURCE_IR_ADAPTER_ID = "controlled-xlsb-derived-xlsx"
XLSB_SOURCE_IR_ADAPTER_VERSION = "1.0.1"
LIBREOFFICE_XLSB_CONVERTER_ID = "libreoffice-headless"
LIBREOFFICE_XLSB_CONVERTER_VERSION = "26.2.4.2"
_LIBREOFFICE_OUTPUT_FILTER = "xlsx:Calc MS Excel 2007 XML"
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_CONVERTER_LOG_BYTES = 1024 * 1024
_VERSION_OUTPUT_BYTES = 16 * 1024
_VERSION_PATTERN = re.compile(
    rb"^LibreOffice ([0-9]+(?:\.[0-9]+){3}) ([0-9a-f]{8,})\s*$"
)
_SAFE_CID_TARGET_PATTERN = re.compile(
    r"^cid:[A-Za-z0-9][A-Za-z0-9._@+-]{0,252}$",
    flags=re.ASCII,
)
_RELATIONSHIP_ATTRIBUTE_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_DRAWINGML_NAMESPACE = (
    "http://schemas.openxmlformats.org/drawingml/2006/main"
)
_MACOS_NETWORK_SANDBOX_PROFILE = (
    '(version 1)(allow default)'
    '(deny network-outbound '
    '(require-all '
    '(require-any (remote tcp "*:*") (remote udp "*:*")) '
    '(require-not (remote ip "localhost:*"))))'
)
_PROFILE_REGISTRY = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
    <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisablePythonRuntime" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>
  </item>
</oor:items>
"""


class XlsbConversionRejectionCode(str, Enum):
    POLICY_REJECTED = "policy_rejected"
    CONVERTER_UNAVAILABLE = "converter_unavailable"
    VERSION_MISMATCH = "version_mismatch"
    TIMEOUT = "timeout"
    PROCESS_FAILED = "process_failed"
    OUTPUT_INVALID = "output_invalid"


class XlsbConversionRejected(RuntimeError):
    def __init__(
        self,
        code: XlsbConversionRejectionCode,
        message: str,
    ) -> None:
        if not isinstance(code, XlsbConversionRejectionCode):
            raise TypeError("code must be an XlsbConversionRejectionCode")
        if not message.strip():
            raise ValueError("message must not be blank")
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class XlsbConversionArtifact:
    derived_bytes: bytes
    derived_source_sha256: str
    converter_id: str
    converter_version: str
    converter_build_id: str
    converter_executable_sha256: str
    converter_config: SourceConversionSecurityConfig
    converter_output_sha256: str

    def __post_init__(self) -> None:
        if not self.derived_bytes:
            raise ValueError("derived_bytes must not be empty")
        if (
            hashlib.sha256(self.derived_bytes).hexdigest()
            != self.derived_source_sha256
        ):
            raise ValueError("derived_source_sha256 must bind derived_bytes")
        for name in (
            "converter_id",
            "converter_version",
            "converter_build_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be blank")
        for name in (
            "converter_executable_sha256",
            "converter_output_sha256",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if not isinstance(self.converter_config, SourceConversionSecurityConfig):
            raise TypeError(
                "converter_config must be a SourceConversionSecurityConfig"
            )


@runtime_checkable
class XlsbToXlsxConverterPort(Protocol):
    def convert(self, source_bytes: bytes) -> XlsbConversionArtifact:
        ...


@dataclass(frozen=True, slots=True)
class _ProcessOutcome:
    return_code: int
    timed_out: bool
    output_sha256: str
    output_size: int


class LibreOfficeXlsbConverter:
    """Run one pinned LibreOffice conversion in an isolated, no-remote-IP lane.

    The source package is screened for executable/active content and external
    relationships before this class is invoked.  The macOS sandbox permits
    local IPC needed by LibreOffice but denies non-loopback TCP/UDP.  Production
    runtimes on other operating systems must provide an equivalent converter
    implementation; this implementation fails closed outside macOS.
    """

    def __init__(
        self,
        *,
        executable: str | PathLike[str] = "/opt/homebrew/bin/soffice",
        sandbox_executable: str | PathLike[str] = "/usr/bin/sandbox-exec",
        expected_version: str = LIBREOFFICE_XLSB_CONVERTER_VERSION,
        timeout_seconds: int = 120,
    ) -> None:
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise TypeError("timeout_seconds must be an integer")
        self._executable = Path(executable)
        self._sandbox_executable = Path(sandbox_executable)
        self._expected_version = expected_version
        self._config = SourceConversionSecurityConfig(
            output_filter=_LIBREOFFICE_OUTPUT_FILTER,
            isolated_temporary_directory=True,
            isolated_user_profile=True,
            macro_policy="reject_source_and_disable_runtime",
            external_link_policy=(
                "reject_external_data_and_block_unresolved_cid_visual"
            ),
            network_policy="macos_sandbox_deny_non_loopback_ip",
            timeout_seconds=timeout_seconds,
        )

    @property
    def config(self) -> SourceConversionSecurityConfig:
        return self._config

    def convert(self, source_bytes: bytes) -> XlsbConversionArtifact:
        if not isinstance(source_bytes, bytes) or not source_bytes:
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.OUTPUT_INVALID,
                "The verified XLSB byte stream is unavailable.",
            )
        executable = self._validated_executable(
            self._executable,
            kind="LibreOffice",
        )
        sandbox_executable = self._validated_executable(
            self._sandbox_executable,
            kind="network sandbox",
        )
        if platform.system() != "Darwin":
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.CONVERTER_UNAVAILABLE,
                "The configured converter requires the macOS network sandbox.",
            )
        converter_version, converter_build_id, executable_sha256 = (
            self._converter_identity(executable)
        )

        with tempfile.TemporaryDirectory(prefix="reqpilotx-xlsb-convert-") as root:
            workspace = Path(root)
            input_directory = workspace / "input"
            output_directory = workspace / "output"
            profile_directory = workspace / "profile"
            runtime_tmp_directory = workspace / "tmp"
            log_path = workspace / "converter-output.log"
            for directory in (
                input_directory,
                output_directory,
                profile_directory / "user",
                runtime_tmp_directory,
            ):
                directory.mkdir(parents=True, mode=0o700)

            registry_path = profile_directory / "user" / "registrymodifications.xcu"
            registry_path.write_text(_PROFILE_REGISTRY, encoding="utf-8")
            registry_path.chmod(0o600)
            input_path = input_directory / "source.xlsb"
            input_path.write_bytes(source_bytes)
            input_path.chmod(0o600)

            command = (
                str(sandbox_executable),
                "-p",
                _MACOS_NETWORK_SANDBOX_PROFILE,
                str(executable),
                f"-env:UserInstallation={profile_directory.as_uri()}",
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--nofirststartwizard",
                "--norestore",
                "--convert-to",
                _LIBREOFFICE_OUTPUT_FILTER,
                "--outdir",
                str(output_directory),
                str(input_path),
            )
            outcome = _run_bounded_process(
                command,
                cwd=workspace,
                environment=_isolated_environment(runtime_tmp_directory),
                timeout_seconds=self._config.timeout_seconds,
                output_path=log_path,
            )
            if outcome.timed_out:
                raise XlsbConversionRejected(
                    XlsbConversionRejectionCode.TIMEOUT,
                    "The XLSB conversion exceeded its configured timeout.",
                )
            if (
                outcome.return_code != 0
                or outcome.output_size > _MAX_CONVERTER_LOG_BYTES
            ):
                raise XlsbConversionRejected(
                    XlsbConversionRejectionCode.PROCESS_FAILED,
                    "The XLSB converter did not complete within its safe output boundary.",
                )
            if _sha256_file(executable) != executable_sha256:
                raise XlsbConversionRejected(
                    XlsbConversionRejectionCode.VERSION_MISMATCH,
                    "LibreOffice executable identity changed during conversion.",
                )

            expected_output = output_directory / "source.xlsx"
            output_files = tuple(
                item
                for item in output_directory.iterdir()
                if item.is_file() and not item.is_symlink()
            )
            if output_files != (expected_output,):
                raise XlsbConversionRejected(
                    XlsbConversionRejectionCode.OUTPUT_INVALID,
                    "The XLSB converter did not produce exactly one expected XLSX package.",
                )
            derived_bytes = expected_output.read_bytes()
            if not derived_bytes:
                raise XlsbConversionRejected(
                    XlsbConversionRejectionCode.OUTPUT_INVALID,
                    "The XLSB converter produced an empty derived package.",
                )
            return XlsbConversionArtifact(
                derived_bytes=derived_bytes,
                derived_source_sha256=hashlib.sha256(derived_bytes).hexdigest(),
                converter_id=LIBREOFFICE_XLSB_CONVERTER_ID,
                converter_version=converter_version,
                converter_build_id=converter_build_id,
                converter_executable_sha256=executable_sha256,
                converter_config=self._config,
                converter_output_sha256=outcome.output_sha256,
            )

    def _validated_executable(self, path: Path, *, kind: str) -> Path:
        try:
            resolved = path.resolve(strict=True)
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.CONVERTER_UNAVAILABLE,
                f"The configured {kind} executable is unavailable.",
            ) from exc
        if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.CONVERTER_UNAVAILABLE,
                f"The configured {kind} executable is unavailable.",
            )
        return resolved

    def _converter_identity(self, executable: Path) -> tuple[str, str, str]:
        try:
            completed = subprocess.run(
                (str(executable), "--version"),
                check=False,
                cwd=str(executable.parent),
                env=_isolated_environment(Path(tempfile.gettempdir())),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=min(self._config.timeout_seconds, 10),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.CONVERTER_UNAVAILABLE,
                "LibreOffice identity verification failed.",
            ) from exc
        output = completed.stdout or b""
        if completed.returncode != 0 or len(output) > _VERSION_OUTPUT_BYTES:
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.CONVERTER_UNAVAILABLE,
                "LibreOffice identity verification failed.",
            )
        match = _VERSION_PATTERN.fullmatch(output.strip())
        if match is None:
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.CONVERTER_UNAVAILABLE,
                "LibreOffice returned an unrecognized version identity.",
            )
        version = match.group(1).decode("ascii")
        build_id = match.group(2).decode("ascii")
        if version != self._expected_version:
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.VERSION_MISMATCH,
                "LibreOffice version does not match the pinned conversion profile.",
            )
        return version, build_id, _sha256_file(executable)


class _DerivedXlsxMapper(XlsxSourceIRAdapter):
    def __init__(
        self,
        *,
        adapter_version: str,
        source_asset_publisher: Callable[[SourceAssetRef, bytes], None]
        | None = None,
    ) -> None:
        super().__init__(source_asset_publisher=source_asset_publisher)
        self.adapter_id = XLSB_SOURCE_IR_ADAPTER_ID
        self.adapter_version = adapter_version


class XlsbSourceIRAdapter:
    """Controlled XLSB-to-derived-XLSX SourceIR adapter.

    Original XLSB bytes remain the immutable source identity.  XLSX locators
    refer to the explicitly recorded derived package, never to a native XLSB
    parse, and the resulting snapshot remains PARTIAL until an independent
    visible-content fidelity profile is approved.
    """

    adapter_id = XLSB_SOURCE_IR_ADAPTER_ID
    adapter_version = XLSB_SOURCE_IR_ADAPTER_VERSION
    document_format = DocumentFormat.XLSB
    supports_controlled_conversion = True

    def __init__(
        self,
        *,
        converter: XlsbToXlsxConverterPort | None = None,
        visual_renderer: SpreadsheetVisualRendererPort | None = None,
        source_asset_publisher: Callable[[SourceAssetRef, bytes], None]
        | None = None,
    ) -> None:
        if source_asset_publisher is not None and not callable(
            source_asset_publisher
        ):
            raise TypeError("source_asset_publisher must be callable")
        self._converter = converter or LibreOfficeXlsbConverter()
        self._visual_renderer = (
            visual_renderer or LibreOfficeSpreadsheetVisualRenderer()
        )
        self._source_asset_publisher = source_asset_publisher

    def extract(
        self,
        source_path: str | PathLike[str],
        *,
        preflight: DocumentPreflightReport,
        created_at: datetime,
    ) -> SourceIRSnapshot:
        if (
            not isinstance(created_at, datetime)
            or created_at.tzinfo is None
            or created_at.utcoffset() is None
        ):
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.INVALID_TIMESTAMP,
                "created_at must be timezone-aware before XLSB conversion.",
            )

        path = Path(source_path)
        source_bytes, source_size, source_sha256 = _read_source_bytes(path)
        self._validate_preflight(
            path=path,
            source_size=source_size,
            source_sha256=source_sha256,
            preflight=preflight,
        )
        try:
            external_visual_relationships = _validate_spreadsheet_package_policy(
                source_bytes,
                expected_format=DocumentFormat.XLSB,
            )
        except XlsbConversionRejected as exc:
            raise _adapter_rejection(exc) from exc

        try:
            artifact = self._converter.convert(source_bytes)
        except XlsbConversionRejected as exc:
            raise _adapter_rejection(exc) from exc
        if not isinstance(artifact, XlsbConversionArtifact):
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.CONVERSION_FAILED,
                "The XLSB converter returned an invalid product contract.",
            )

        try:
            _validate_spreadsheet_package_policy(
                artifact.derived_bytes,
                expected_format=DocumentFormat.XLSX,
            )
            _validate_derived_preflight(artifact)
            expected_sheets = _expected_sheets_from_derived_xlsx(
                artifact.derived_bytes
            )
        except XlsbConversionRejected as exc:
            raise _adapter_rejection(exc) from exc

        try:
            visual_profile = self._visual_renderer.render(
                source_bytes,
                source_format=DocumentFormat.XLSB,
                expected_sheets=expected_sheets,
                external_visual_relationships=external_visual_relationships,
            )
        except SpreadsheetVisualProfileRejected as exc:
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.VISUAL_PROFILE_FAILED,
                str(exc),
            ) from exc
        if (
            not isinstance(visual_profile, SpreadsheetVisualProfile)
            or visual_profile.source_sha256 != source_sha256
            or visual_profile.source_size != source_size
            or visual_profile.source_format is not DocumentFormat.XLSB
            or visual_profile.external_visual_relationships
            != external_visual_relationships
        ):
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.VISUAL_PROFILE_FAILED,
                "The XLSB visual renderer returned an invalid source-bound profile.",
            )

        current_size, current_sha256 = _source_identity(path)
        if current_size != source_size or current_sha256 != source_sha256:
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.SOURCE_CHANGED,
                "XLSB source bytes changed during controlled conversion or rendering.",
            )

        config_sha256 = _converter_config_sha256(artifact.converter_config)
        effective_adapter_version = (
            f"{XLSB_SOURCE_IR_ADAPTER_VERSION}"
            f"+lo-{artifact.converter_version}"
            f"+cfg-{config_sha256[:16]}"
            f"+derived-{artifact.derived_source_sha256}"
            f"+visual-{visual_profile.profile_sha256[:40]}"
        )
        provenance = SourceConversionProvenance(
            original_format="xlsb",
            derived_format="xlsx",
            original_source_sha256=source_sha256,
            derived_source_sha256=artifact.derived_source_sha256,
            converter_id=artifact.converter_id,
            converter_version=artifact.converter_version,
            converter_build_id=artifact.converter_build_id,
            converter_executable_sha256=artifact.converter_executable_sha256,
            converter_config=artifact.converter_config,
            converter_config_sha256=config_sha256,
            converter_output_sha256=artifact.converter_output_sha256,
            source_truth="original_bytes",
            fidelity_profile="derived_spreadsheet_structure_and_values_only",
        )
        mapper = _DerivedXlsxMapper(
            adapter_version=effective_adapter_version,
            source_asset_publisher=self._source_asset_publisher,
        )
        direct_page_assets = visual_profile.page_asset_refs()
        try:
            nodes, assets, mapping_diagnostics = mapper._extract_bytes(
                artifact.derived_bytes,
                source_sha256=source_sha256,
            )
        except _XlsxResourceBudgetExceeded:
            return self._snapshot(
                source_sha256=source_sha256,
                adapter_version=effective_adapter_version,
                created_at=created_at,
                status=SourceIRStatus.FAILED,
                provenance=provenance,
                assets=direct_page_assets,
                diagnostics=(
                    SourceDiagnostic(
                        code="xlsb_derived_xlsx_resource_budget_exceeded",
                        severity=SourceDiagnosticSeverity.ERROR,
                        message=(
                            "The derived XLSX package exceeds the native XLSX mapping budget."
                        ),
                    ),
                ),
            )
        except Exception as exc:
            return self._snapshot(
                source_sha256=source_sha256,
                adapter_version=effective_adapter_version,
                created_at=created_at,
                status=SourceIRStatus.FAILED,
                provenance=provenance,
                assets=direct_page_assets,
                diagnostics=(
                    SourceDiagnostic(
                        code="xlsb_derived_xlsx_parse_failed",
                        severity=SourceDiagnosticSeverity.ERROR,
                        message=(
                            "The verified derived XLSX package could not be mapped to "
                            f"SourceIR ({type(exc).__name__})."
                        ),
                    ),
                ),
            )

        if self._source_asset_publisher is not None:
            for asset, page in zip(
                direct_page_assets,
                visual_profile.pages,
                strict=True,
            ):
                if hashlib.sha256(page.png_bytes).hexdigest() != asset.content_sha256:
                    raise RuntimeError(
                        "source asset bytes do not match SourceIR identity"
                    )
                self._source_asset_publisher(asset, page.png_bytes)

        diagnostics = (
            SourceDiagnostic(
                code="xlsb_controlled_conversion_provenance_recorded",
                severity=SourceDiagnosticSeverity.INFO,
                message=(
                    "Original XLSB bytes remain source truth; the derived XLSX identity "
                    "and pinned converter profile are recorded separately."
                ),
                evidence_sha256=artifact.derived_source_sha256,
            ),
            SourceDiagnostic(
                code="xlsb_derived_xlsx_not_native_fidelity",
                severity=SourceDiagnosticSeverity.WARNING,
                message=(
                    "XLSX facts came from a controlled format conversion and do not claim "
                    "native XLSB package fidelity."
                ),
            ),
            SourceDiagnostic(
                code="xlsb_direct_visual_profile_recorded",
                severity=SourceDiagnosticSeverity.INFO,
                message=(
                    "Original XLSB bytes were rendered directly into a source-bound "
                    "sheet/page visual profile."
                ),
                evidence_sha256=visual_profile.profile_sha256,
            ),
            SourceDiagnostic(
                code="xlsb_direct_visual_profile_ai_review_pending",
                severity=SourceDiagnosticSeverity.WARNING,
                message=(
                    "The direct visual profile is recorded but remains PARTIAL until "
                    "its complete page set receives an independent AI Silver review."
                ),
            ),
            *(
                (
                    SourceDiagnostic(
                        code="xlsb_external_cid_visual_relationship_uses_embedded_cache",
                        severity=SourceDiagnosticSeverity.INFO,
                        message=(
                            "Paired cid image relationships use their package-embedded "
                            f"cache ({len(external_visual_relationships)}); external "
                            "fetch remains blocked."
                        ),
                    ),
                )
                if external_visual_relationships
                else ()
            ),
            *mapping_diagnostics,
        )
        return self._snapshot(
            source_sha256=source_sha256,
            adapter_version=effective_adapter_version,
            created_at=created_at,
            status=SourceIRStatus.PARTIAL,
            provenance=provenance,
            nodes=nodes,
            assets=(*assets, *direct_page_assets),
            diagnostics=diagnostics,
        )

    def _validate_preflight(
        self,
        *,
        path: Path,
        source_size: int,
        source_sha256: str,
        preflight: DocumentPreflightReport,
    ) -> None:
        if not isinstance(preflight, DocumentPreflightReport):
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.INVALID_PREFLIGHT,
                "A DocumentPreflightReport is required before XLSB conversion.",
            )
        identity = preflight.identity
        if (
            not preflight.controlled_conversion_eligible
            or identity.detected_format is not self.document_format
            or identity.source_name != path.name
            or identity.declared_extension != path.suffix.lower()
        ):
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.INVALID_PREFLIGHT,
                "The preflight report does not authorize this XLSB conversion.",
            )
        if identity.source_sha256 != source_sha256 or identity.source_size != source_size:
            raise SourceIRAdapterRejected(
                SourceIRAdapterRejectionCode.SOURCE_CHANGED,
                "XLSB source bytes changed after preflight.",
            )

    def _snapshot(
        self,
        *,
        source_sha256: str,
        adapter_version: str,
        created_at: datetime,
        status: SourceIRStatus,
        provenance: SourceConversionProvenance,
        nodes: tuple = (),
        assets: tuple = (),
        diagnostics: tuple[SourceDiagnostic, ...] = (),
    ) -> SourceIRSnapshot:
        document_id = stable_document_id(source_sha256)
        return SourceIRSnapshot(
            source_ir_id=stable_source_ir_id(
                document_id=document_id,
                source_sha256=source_sha256,
                adapter_id=self.adapter_id,
                adapter_version=adapter_version,
            ),
            document_id=document_id,
            source_sha256=source_sha256,
            adapter_id=self.adapter_id,
            adapter_version=adapter_version,
            status=status,
            nodes=nodes,
            assets=assets,
            source_conversion=provenance,
            diagnostics=diagnostics,
            created_at=created_at,
        )


def _adapter_rejection(exc: XlsbConversionRejected) -> SourceIRAdapterRejected:
    code = {
        XlsbConversionRejectionCode.POLICY_REJECTED: (
            SourceIRAdapterRejectionCode.CONVERSION_POLICY_REJECTED
        ),
        XlsbConversionRejectionCode.CONVERTER_UNAVAILABLE: (
            SourceIRAdapterRejectionCode.CONVERTER_UNAVAILABLE
        ),
        XlsbConversionRejectionCode.VERSION_MISMATCH: (
            SourceIRAdapterRejectionCode.CONVERTER_VERSION_MISMATCH
        ),
        XlsbConversionRejectionCode.TIMEOUT: (
            SourceIRAdapterRejectionCode.CONVERSION_TIMEOUT
        ),
        XlsbConversionRejectionCode.PROCESS_FAILED: (
            SourceIRAdapterRejectionCode.CONVERSION_FAILED
        ),
        XlsbConversionRejectionCode.OUTPUT_INVALID: (
            SourceIRAdapterRejectionCode.DERIVED_PREFLIGHT_REJECTED
        ),
    }[exc.code]
    return SourceIRAdapterRejected(code, str(exc))


def _validate_spreadsheet_package_policy(
    payload: bytes,
    *,
    expected_format: DocumentFormat,
) -> tuple[SpreadsheetExternalVisualRelationship, ...]:
    external_visual_relationships: list[
        SpreadsheetExternalVisualRelationship
    ] = []
    package_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            members = tuple(archive.infolist())
            member_names = frozenset(item.filename for item in members)
            if len(member_names) != len(members):
                raise XlsbConversionRejected(
                    XlsbConversionRejectionCode.OUTPUT_INVALID,
                    "Spreadsheet package contains duplicate members.",
                )
            lowercase_names = {name.lower() for name in member_names}
            if len(lowercase_names) != len(member_names):
                raise XlsbConversionRejected(
                    XlsbConversionRejectionCode.OUTPUT_INVALID,
                    "Spreadsheet package contains case-colliding members.",
                )
            workbook_bin = "xl/workbook.bin" in member_names
            workbook_xml = "xl/workbook.xml" in member_names
            if expected_format is DocumentFormat.XLSB:
                signature_matches = workbook_bin and not workbook_xml
            elif expected_format is DocumentFormat.XLSX:
                signature_matches = workbook_xml and not workbook_bin
            else:
                raise AssertionError("spreadsheet policy supports XLSB and XLSX only")
            if not signature_matches:
                raise XlsbConversionRejected(
                    XlsbConversionRejectionCode.OUTPUT_INVALID,
                    "Spreadsheet extension and package signature do not match.",
                )

            prohibited_prefixes = (
                "customui/",
                "xl/externallinks/",
                "xl/querytables/",
                "xl/connections",
                "xl/embeddings/",
                "xl/activex/",
                "xl/ctrlprops/",
            )
            prohibited_exact = {
                "xl/vbaproject.bin",
                "xl/vbaprojectsignature.bin",
            }
            if any(
                name in prohibited_exact
                or any(name.startswith(prefix) for prefix in prohibited_prefixes)
                for name in lowercase_names
            ):
                raise XlsbConversionRejected(
                    XlsbConversionRejectionCode.POLICY_REJECTED,
                    "Spreadsheet active content or external data parts are not allowed.",
                )
            for member in members:
                if not member.filename.endswith(".rels"):
                    continue
                root = _parse_relationships(archive.read(member))
                relationships_by_id = {
                    item.get("Id"): item
                    for item in root
                    if item.get("Id")
                }
                if len(relationships_by_id) != len(
                    [item for item in root if item.get("Id")]
                ):
                    raise XlsbConversionRejected(
                        XlsbConversionRejectionCode.OUTPUT_INVALID,
                        "Spreadsheet relationship identifiers are ambiguous.",
                    )
                for item in root:
                    if (item.get("TargetMode") or "").casefold() != "external":
                        continue
                    relationship_type = (item.get("Type") or "").rsplit("/", 1)[-1]
                    target = item.get("Target") or ""
                    if (
                        expected_format is DocumentFormat.XLSB
                        and relationship_type == "image"
                        and urlsplit(target).scheme.casefold() == "cid"
                        and _SAFE_CID_TARGET_PATTERN.fullmatch(target) is not None
                    ):
                        external_visual_relationships.append(
                            _paired_cid_visual_relationship(
                                archive=archive,
                                member_names=member_names,
                                relationship_part=member.filename,
                                relationships_by_id=relationships_by_id,
                                external_relationship=item,
                                package_sha256=package_sha256,
                            )
                        )
                        continue
                    raise XlsbConversionRejected(
                        XlsbConversionRejectionCode.POLICY_REJECTED,
                        "External spreadsheet relationships are not allowed.",
                    )
    except XlsbConversionRejected:
        raise
    except (OSError, EOFError, zipfile.BadZipFile, zipfile.LargeZipFile, etree.XMLSyntaxError) as exc:
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.OUTPUT_INVALID,
            "The spreadsheet package could not be validated safely.",
        ) from exc
    return tuple(external_visual_relationships)


def _paired_cid_visual_relationship(
    *,
    archive: zipfile.ZipFile,
    member_names: frozenset[str],
    relationship_part: str,
    relationships_by_id: dict[str | None, etree._Element],
    external_relationship: etree._Element,
    package_sha256: str,
) -> SpreadsheetExternalVisualRelationship:
    owner_part = _relationship_owner_part(relationship_part)
    if owner_part not in member_names or not owner_part.endswith(".xml"):
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.POLICY_REJECTED,
            "A cid image relationship has no inspectable drawing owner.",
        )
    external_id = external_relationship.get("Id") or ""
    drawing = _parse_xml(archive.read(owner_part))
    link_attribute = f"{{{_RELATIONSHIP_ATTRIBUTE_NAMESPACE}}}link"
    embed_attribute = f"{{{_RELATIONSHIP_ATTRIBUTE_NAMESPACE}}}embed"
    linked_blips = [
        item
        for item in drawing.iter(f"{{{_DRAWINGML_NAMESPACE}}}blip")
        if item.get(link_attribute) == external_id
    ]
    if len(linked_blips) != 1:
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.POLICY_REJECTED,
            "A cid image relationship is not paired to exactly one drawing image.",
        )
    embedded_id = linked_blips[0].get(embed_attribute) or ""
    embedded_relationship = relationships_by_id.get(embedded_id)
    if (
        not embedded_id
        or embedded_relationship is None
        or (embedded_relationship.get("TargetMode") or "").casefold() == "external"
        or (embedded_relationship.get("Type") or "").rsplit("/", 1)[-1]
        != "image"
    ):
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.POLICY_REJECTED,
            "A cid image relationship has no paired internal image cache.",
        )
    embedded_target = embedded_relationship.get("Target") or ""
    embedded_part = _resolve_package_target(
        owner_part=owner_part,
        target=embedded_target,
    )
    if embedded_part not in member_names:
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.POLICY_REJECTED,
            "A cid image relationship references a missing internal image cache.",
        )
    embedded_cache = archive.read(embedded_part)
    if not embedded_cache:
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.POLICY_REJECTED,
            "A cid image relationship references an empty internal image cache.",
        )
    target = external_relationship.get("Target") or ""
    relationship_scope_sha256 = hashlib.sha256(
        (
            f"{package_sha256}|{owner_part}|{external_id}|{embedded_id}"
        ).encode("utf-8")
    ).hexdigest()
    return SpreadsheetExternalVisualRelationship(
        relationship_scope_sha256=relationship_scope_sha256,
        target_sha256=hashlib.sha256(target.encode("utf-8")).hexdigest(),
        embedded_cache_sha256=hashlib.sha256(embedded_cache).hexdigest(),
    )


def _relationship_owner_part(relationship_part: str) -> str:
    parent, marker, filename = relationship_part.rpartition("/_rels/")
    if not marker or not filename.endswith(".rels"):
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.POLICY_REJECTED,
            "A cid image relationship has an invalid package scope.",
        )
    owner_part = f"{parent}/{filename[:-5]}"
    if owner_part.startswith("/") or ".." in owner_part.split("/"):
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.POLICY_REJECTED,
            "A cid image relationship has an invalid package scope.",
        )
    return owner_part


def _resolve_package_target(*, owner_part: str, target: str) -> str:
    parsed = urlsplit(target)
    if (
        not target
        or parsed.scheme
        or parsed.netloc
        or target.startswith(("/", "\\"))
        or "\\" in target
        or parsed.query
        or parsed.fragment
    ):
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.POLICY_REJECTED,
            "An internal image cache target is invalid.",
        )
    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(owner_part), target)
    )
    if resolved.startswith("../") or resolved in {"", ".", ".."}:
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.POLICY_REJECTED,
            "An internal image cache target escapes the package.",
        )
    return resolved


def _parse_relationships(payload: bytes) -> etree._Element:
    return _parse_xml(payload)


def _parse_xml(payload: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        huge_tree=False,
    )
    return etree.fromstring(payload, parser=parser)


def _expected_sheets_from_derived_xlsx(
    payload: bytes,
) -> tuple[SpreadsheetExpectedSheet, ...]:
    try:
        workbook = load_workbook(
            BytesIO(payload),
            read_only=False,
            data_only=False,
            keep_links=False,
        )
    except Exception as exc:
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.OUTPUT_INVALID,
            "The derived XLSX workbook metadata could not be inspected safely.",
        ) from exc
    try:
        return tuple(
            SpreadsheetExpectedSheet(
                sheet_index=index,
                sheet_name_sha256=hashlib.sha256(
                    worksheet.title.encode("utf-8")
                ).hexdigest(),
                render_outline_name_sha256=hashlib.sha256(
                    worksheet.title.strip().encode("utf-8")
                ).hexdigest(),
                sheet_state=worksheet.sheet_state,
                expects_rendered_pages=(
                    worksheet.sheet_state == "visible"
                    and _worksheet_expects_rendered_pages(worksheet)
                ),
            )
            for index, worksheet in enumerate(workbook.worksheets)
        )
    except Exception as exc:
        raise XlsbConversionRejected(
            XlsbConversionRejectionCode.OUTPUT_INVALID,
            "The derived XLSX sheet profile could not be inspected safely.",
        ) from exc
    finally:
        workbook.close()


def _worksheet_expects_rendered_pages(worksheet: object) -> bool:
    cells = getattr(worksheet, "_cells", {})
    if any(
        cell.value is not None
        or cell.has_style
        or cell.comment is not None
        for cell in cells.values()
    ):
        return True
    if tuple(worksheet.merged_cells.ranges):
        return True
    if tuple(getattr(worksheet, "_images", ())):
        return True
    if tuple(getattr(worksheet, "_charts", ())):
        return True
    print_area = getattr(worksheet, "print_area", None)
    return bool(print_area)


def _validate_derived_preflight(artifact: XlsbConversionArtifact) -> None:
    with tempfile.TemporaryDirectory(
        prefix="reqpilotx-xlsb-derived-preflight-"
    ) as root:
        path = Path(root) / "derived.xlsx"
        path.write_bytes(artifact.derived_bytes)
        path.chmod(0o600)
        report = LocalDocumentPreflight().inspect(path)
        if (
            not report.processable
            or report.identity.detected_format is not DocumentFormat.XLSX
            or report.identity.source_sha256 != artifact.derived_source_sha256
            or report.identity.source_size != len(artifact.derived_bytes)
        ):
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.OUTPUT_INVALID,
                "The derived XLSX package did not pass native preflight.",
            )


def _run_bounded_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    output_path: Path,
) -> _ProcessOutcome:
    with output_path.open("wb") as output:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise XlsbConversionRejected(
                XlsbConversionRejectionCode.CONVERTER_UNAVAILABLE,
                "The XLSB converter process could not be started.",
            ) from exc
        timed_out = False
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
            return_code = process.wait()
    output_size = output_path.stat().st_size
    return _ProcessOutcome(
        return_code=return_code,
        timed_out=timed_out,
        output_sha256=_sha256_file(output_path),
        output_size=output_size,
    )


def _isolated_environment(runtime_tmp_directory: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "ALL_PROXY",
        "FTP_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "ftp_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "URE_BOOTSTRAP",
        "UNO_PATH",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "LANG": "C",
            "LC_ALL": "C",
            "NO_PROXY": "*",
            "SAL_DISABLE_OPENCL": "1",
            "TMPDIR": str(runtime_tmp_directory),
        }
    )
    return environment


def _read_source_bytes(path: Path) -> tuple[bytes, int, str]:
    digest = hashlib.sha256()
    payload = bytearray()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            payload.extend(chunk)
    return bytes(payload), len(payload), digest.hexdigest()


def _source_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _converter_config_sha256(config: SourceConversionSecurityConfig) -> str:
    canonical = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "LIBREOFFICE_XLSB_CONVERTER_ID",
    "LIBREOFFICE_XLSB_CONVERTER_VERSION",
    "XLSB_SOURCE_IR_ADAPTER_ID",
    "XLSB_SOURCE_IR_ADAPTER_VERSION",
    "LibreOfficeXlsbConverter",
    "XlsbConversionArtifact",
    "XlsbConversionRejected",
    "XlsbConversionRejectionCode",
    "XlsbSourceIRAdapter",
    "XlsbToXlsxConverterPort",
]
