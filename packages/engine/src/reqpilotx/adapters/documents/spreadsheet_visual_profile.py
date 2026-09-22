from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import signal
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from os import PathLike
from pathlib import Path
from typing import Protocol, runtime_checkable

import pymupdf
from pypdf import PdfReader, PdfWriter

from reqpilotx.domain.extraction.preflight import DocumentFormat
from reqpilotx.domain.extraction.source_ir import SourceAssetKind, SourceAssetRef
from reqpilotx.adapters.documents.xlsx_cell_images import (
    XlsxCellImageAudit,
    audit_xlsx_cell_images,
    materialize_xlsx_cell_image_render_overlay,
)


SPREADSHEET_VISUAL_PROFILE_SCHEMA_VERSION = "spreadsheet-visible-profile/v1"
LIBREOFFICE_DIRECT_VISUAL_RENDERER_ID = (
    "libreoffice-direct-pdf+pymupdf-page-png"
)
LIBREOFFICE_DIRECT_VISUAL_RENDERER_VERSION = "1.0.0"
LIBREOFFICE_CELL_IMAGE_VISUAL_RENDERER_VERSION = "1.1.0"
LIBREOFFICE_DIRECT_VISUAL_EXPECTED_VERSION = "26.2.4.2"
_PDF_OUTPUT_FILTER = "pdf:calc_pdf_Export"
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_RENDER_LOG_BYTES = 1024 * 1024
_MAX_RENDERED_PAGES = 5_000
_VERSION_OUTPUT_BYTES = 16 * 1024
_VERSION_PATTERN = re.compile(
    rb"^LibreOffice ([0-9]+(?:\.[0-9]+){3}) ([0-9a-f]{8,})\s*$"
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
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SpreadsheetVisualProfileRejected(RuntimeError):
    pass


@runtime_checkable
class SpreadsheetVisualRendererPort(Protocol):
    def render(
        self,
        source_bytes: bytes,
        *,
        source_format: DocumentFormat,
        expected_sheets: tuple[SpreadsheetExpectedSheet, ...],
        external_visual_relationships: tuple[
            SpreadsheetExternalVisualRelationship, ...
        ] = (),
    ) -> SpreadsheetVisualProfile:
        ...


@dataclass(frozen=True, slots=True)
class SpreadsheetVisualRenderConfig:
    output_filter: str = _PDF_OUTPUT_FILTER
    png_dpi: int = 144
    isolated_temporary_directory: bool = True
    isolated_user_profile: bool = True
    macro_policy: str = "reject_source_and_disable_runtime"
    external_link_policy: str = (
        "reject_external_data_and_use_only_embedded_cid_cache"
    )
    network_policy: str = "macos_sandbox_deny_non_loopback_ip"
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if self.output_filter != _PDF_OUTPUT_FILTER:
            raise ValueError("spreadsheet visual output_filter is fixed")
        if self.png_dpi < 72 or self.png_dpi > 300:
            raise ValueError("png_dpi must stay within the bounded review range")
        if self.timeout_seconds < 1 or self.timeout_seconds > 600:
            raise ValueError("timeout_seconds must stay within 1..600")
        if not (
            self.isolated_temporary_directory and self.isolated_user_profile
        ):
            raise ValueError("spreadsheet visual rendering requires full isolation")

    def hash_only_fact(self) -> dict[str, object]:
        return {
            "output_filter": self.output_filter,
            "png_dpi": self.png_dpi,
            "isolated_temporary_directory": self.isolated_temporary_directory,
            "isolated_user_profile": self.isolated_user_profile,
            "macro_policy": self.macro_policy,
            "external_link_policy": self.external_link_policy,
            "network_policy": self.network_policy,
            "timeout_seconds": self.timeout_seconds,
        }

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.hash_only_fact())


@dataclass(frozen=True, slots=True)
class SpreadsheetExpectedSheet:
    sheet_index: int
    sheet_name_sha256: str
    sheet_state: str
    expects_rendered_pages: bool
    render_outline_name_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.sheet_index < 0:
            raise ValueError("sheet_index must not be negative")
        _validate_sha256(self.sheet_name_sha256, "sheet_name_sha256")
        if self.render_outline_name_sha256 is not None:
            _validate_sha256(
                self.render_outline_name_sha256,
                "render_outline_name_sha256",
            )
        if self.sheet_state not in {"visible", "hidden", "veryHidden"}:
            raise ValueError("sheet_state is invalid")
        if self.sheet_state != "visible" and self.expects_rendered_pages:
            raise ValueError("hidden sheets cannot expect rendered pages")

    def hash_only_fact(self) -> dict[str, object]:
        return {
            "sheet_index": self.sheet_index,
            "sheet_name_sha256": self.sheet_name_sha256,
            "sheet_state": self.sheet_state,
            "expects_rendered_pages": self.expects_rendered_pages,
            "render_outline_name_sha256": self.render_outline_name_sha256,
        }


@dataclass(frozen=True, slots=True)
class SpreadsheetExternalVisualRelationship:
    relationship_scope_sha256: str
    target_sha256: str
    embedded_cache_sha256: str
    relationship_type: str = "image"
    target_scheme: str = "cid"
    disposition: str = "paired_internal_cache_no_external_fetch"

    def __post_init__(self) -> None:
        for name in (
            "relationship_scope_sha256",
            "target_sha256",
            "embedded_cache_sha256",
        ):
            _validate_sha256(getattr(self, name), name)
        if self.relationship_type != "image" or self.target_scheme != "cid":
            raise ValueError("only blocked cid image relationships are supported")
        if self.disposition != "paired_internal_cache_no_external_fetch":
            raise ValueError("external visual relationship disposition is invalid")

    def hash_only_fact(self) -> dict[str, object]:
        return {
            "relationship_scope_sha256": self.relationship_scope_sha256,
            "target_sha256": self.target_sha256,
            "embedded_cache_sha256": self.embedded_cache_sha256,
            "relationship_type": self.relationship_type,
            "target_scheme": self.target_scheme,
            "disposition": self.disposition,
        }


@dataclass(frozen=True, slots=True)
class SpreadsheetRenderedPage:
    page_number: int
    sheet_index: int
    sheet_name_sha256: str
    width_pixels: int
    height_pixels: int
    png_sha256: str
    standalone_pdf_sha256: str
    png_bytes: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.page_number < 1 or self.sheet_index < 0:
            raise ValueError("page_number and sheet_index must be positive/non-negative")
        if self.width_pixels < 1 or self.height_pixels < 1:
            raise ValueError("rendered page dimensions must be positive")
        for name in (
            "sheet_name_sha256",
            "png_sha256",
            "standalone_pdf_sha256",
        ):
            _validate_sha256(getattr(self, name), name)
        if hashlib.sha256(self.png_bytes).hexdigest() != self.png_sha256:
            raise ValueError("png_sha256 must bind png_bytes")

    def hash_only_fact(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "sheet_index": self.sheet_index,
            "sheet_name_sha256": self.sheet_name_sha256,
            "width_pixels": self.width_pixels,
            "height_pixels": self.height_pixels,
            "png_sha256": self.png_sha256,
            "standalone_pdf_sha256": self.standalone_pdf_sha256,
        }

    def asset_ref(self, *, source_sha256: str) -> SourceAssetRef:
        asset_id = "asset-" + hashlib.sha256(
            (
                f"{source_sha256}|{LIBREOFFICE_DIRECT_VISUAL_RENDERER_ID}|"
                f"{self.page_number}|{self.png_sha256}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        return SourceAssetRef(
            asset_id=asset_id,
            kind=SourceAssetKind.PAGE_RENDER,
            content_sha256=self.png_sha256,
            media_type="image/png",
            storage_key=(
                f"spreadsheet-direct-render/{source_sha256[:2]}/{source_sha256}/"
                f"page-{self.page_number:04d}-{self.png_sha256}.png"
            ),
        )


@dataclass(frozen=True, slots=True)
class SpreadsheetSheetPageBinding:
    sheet_index: int
    sheet_name_sha256: str
    sheet_state: str
    page_start: int | None
    page_end: int | None

    def __post_init__(self) -> None:
        if self.sheet_index < 0:
            raise ValueError("sheet_index must not be negative")
        _validate_sha256(self.sheet_name_sha256, "sheet_name_sha256")
        if self.sheet_state not in {"visible", "hidden", "veryHidden"}:
            raise ValueError("sheet_state is invalid")
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("page_start and page_end must both be present or absent")
        if self.page_start is not None and (
            self.page_start < 1 or self.page_end is None or self.page_end < self.page_start
        ):
            raise ValueError("sheet page range is invalid")
        if self.sheet_state != "visible" and self.page_start is not None:
            raise ValueError("hidden sheets cannot bind rendered pages")

    def hash_only_fact(self) -> dict[str, object]:
        return {
            "sheet_index": self.sheet_index,
            "sheet_name_sha256": self.sheet_name_sha256,
            "sheet_state": self.sheet_state,
            "page_start": self.page_start,
            "page_end": self.page_end,
        }


@dataclass(frozen=True, slots=True)
class SpreadsheetVisualProfile:
    source_sha256: str
    source_size: int
    source_format: DocumentFormat
    renderer_id: str
    renderer_version: str
    libreoffice_version: str
    libreoffice_build_id: str
    libreoffice_executable_sha256: str
    pymupdf_version: str
    config: SpreadsheetVisualRenderConfig
    render_output_sha256: str
    pdf_sha256: str
    pdf_bytes: bytes = field(repr=False, compare=False)
    sheets: tuple[SpreadsheetSheetPageBinding, ...]
    pages: tuple[SpreadsheetRenderedPage, ...]
    external_visual_relationships: tuple[
        SpreadsheetExternalVisualRelationship, ...
    ] = ()
    cell_image_audit_sha256: str | None = None
    cell_image_reference_count: int = 0
    resolved_cell_image_reference_count: int = 0
    unresolved_cell_image_reference_count: int = 0
    render_input_sha256: str | None = None
    render_input_derivation: str = "original_source"
    visual_fidelity_status: str = "complete"
    source_render_issue_page_numbers: tuple[int, ...] = ()
    schema_version: str = SPREADSHEET_VISUAL_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SPREADSHEET_VISUAL_PROFILE_SCHEMA_VERSION:
            raise ValueError("spreadsheet visual profile schema is invalid")
        if self.source_format not in {DocumentFormat.XLSX, DocumentFormat.XLSB}:
            raise ValueError("spreadsheet visual profile supports XLSX/XLSB")
        if self.source_size < 1:
            raise ValueError("source_size must be positive")
        for name in (
            "source_sha256",
            "libreoffice_executable_sha256",
            "render_output_sha256",
            "pdf_sha256",
        ):
            _validate_sha256(getattr(self, name), name)
        if hashlib.sha256(self.pdf_bytes).hexdigest() != self.pdf_sha256:
            raise ValueError("pdf_sha256 must bind pdf_bytes")
        if not self.renderer_id.strip() or not self.renderer_version.strip():
            raise ValueError("renderer identity must not be blank")
        if self.render_input_sha256 is not None:
            _validate_sha256(self.render_input_sha256, "render_input_sha256")
        if self.cell_image_audit_sha256 is not None:
            _validate_sha256(
                self.cell_image_audit_sha256,
                "cell_image_audit_sha256",
            )
        if self.render_input_derivation not in {
            "original_source",
            "embedded_cell_image_ooxml_overlay",
        }:
            raise ValueError("render_input_derivation is invalid")
        if self.visual_fidelity_status not in {
            "complete",
            "partial_source_render_missing",
        }:
            raise ValueError("visual_fidelity_status is invalid")
        counts = (
            self.cell_image_reference_count,
            self.resolved_cell_image_reference_count,
            self.unresolved_cell_image_reference_count,
        )
        if any(item < 0 for item in counts):
            raise ValueError("cell-image reference counts must not be negative")
        if self.cell_image_reference_count != (
            self.resolved_cell_image_reference_count
            + self.unresolved_cell_image_reference_count
        ):
            raise ValueError("cell-image reference counts are inconsistent")
        if self.cell_image_reference_count:
            if (
                self.cell_image_audit_sha256 is None
                or self.render_input_sha256 is None
            ):
                raise ValueError(
                    "cell-image render integrity requires audit/input hashes"
                )
        elif any(
            (
                self.cell_image_audit_sha256,
                self.render_input_sha256,
                self.source_render_issue_page_numbers,
            )
        ):
            raise ValueError(
                "cell-image render fields require at least one reference"
            )
        if self.resolved_cell_image_reference_count and (
            self.render_input_derivation
            != "embedded_cell_image_ooxml_overlay"
        ):
            raise ValueError("embedded cell images require a render overlay")
        if (
            self.unresolved_cell_image_reference_count
            and self.visual_fidelity_status
            != "partial_source_render_missing"
        ):
            raise ValueError("unresolved cell images must block visual completeness")
        if (
            not self.unresolved_cell_image_reference_count
            and self.visual_fidelity_status != "complete"
        ):
            raise ValueError("complete source media must not be marked partial")
        expected_sheet_indexes = list(range(len(self.sheets)))
        if [item.sheet_index for item in self.sheets] != expected_sheet_indexes:
            raise ValueError("sheet bindings must be complete and ordered")
        page_numbers = [item.page_number for item in self.pages]
        if page_numbers != list(range(1, len(self.pages) + 1)):
            raise ValueError("rendered pages must cover exact range 1..N")
        page_to_sheet: dict[int, int] = {}
        for sheet in self.sheets:
            if sheet.page_start is None:
                continue
            assert sheet.page_end is not None
            for page_number in range(sheet.page_start, sheet.page_end + 1):
                if page_number in page_to_sheet:
                    raise ValueError("sheet page ranges must not overlap")
                page_to_sheet[page_number] = sheet.sheet_index
        if sorted(page_to_sheet) != page_numbers:
            raise ValueError("sheet page bindings must cover every rendered page")
        for page in self.pages:
            if page_to_sheet.get(page.page_number) != page.sheet_index:
                raise ValueError("rendered page sheet binding is inconsistent")
            sheet = self.sheets[page.sheet_index]
            if page.sheet_name_sha256 != sheet.sheet_name_sha256:
                raise ValueError("rendered page sheet hash is inconsistent")
        if tuple(sorted(set(self.source_render_issue_page_numbers))) != (
            self.source_render_issue_page_numbers
        ):
            raise ValueError("source render issue pages must be unique and ordered")
        if any(
            item not in page_numbers
            for item in self.source_render_issue_page_numbers
        ):
            raise ValueError("source render issue page is outside the profile")
        if bool(self.unresolved_cell_image_reference_count) != bool(
            self.source_render_issue_page_numbers
        ):
            raise ValueError(
                "unresolved cell images and source render issue pages must agree"
            )

    @property
    def profile_sha256(self) -> str:
        return _canonical_sha256(self.hash_only_fact(include_profile_sha256=False))

    def hash_only_fact(
        self,
        *,
        include_profile_sha256: bool = True,
    ) -> dict[str, object]:
        fact: dict[str, object] = {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "source_size": self.source_size,
            "source_format": self.source_format.value,
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "libreoffice_version": self.libreoffice_version,
            "libreoffice_build_id": self.libreoffice_build_id,
            "libreoffice_executable_sha256": self.libreoffice_executable_sha256,
            "pymupdf_version": self.pymupdf_version,
            "config": self.config.hash_only_fact(),
            "config_sha256": self.config.sha256,
            "render_output_sha256": self.render_output_sha256,
            "pdf_sha256": self.pdf_sha256,
            "pdf_page_count": len(self.pages),
            "sheets": [item.hash_only_fact() for item in self.sheets],
            "pages": [item.hash_only_fact() for item in self.pages],
            "external_visual_relationships": [
                item.hash_only_fact()
                for item in self.external_visual_relationships
            ],
            "temporary_artifacts_retained": False,
        }
        if include_profile_sha256:
            fact["profile_sha256"] = self.profile_sha256
        if self.cell_image_reference_count:
            fact["cell_image_render_integrity"] = {
                "audit_sha256": self.cell_image_audit_sha256,
                "reference_count": self.cell_image_reference_count,
                "resolved_reference_count": (
                    self.resolved_cell_image_reference_count
                ),
                "unresolved_reference_count": (
                    self.unresolved_cell_image_reference_count
                ),
                "render_input_sha256": self.render_input_sha256,
                "render_input_derivation": self.render_input_derivation,
                "visual_fidelity_status": self.visual_fidelity_status,
                "source_render_issue_page_numbers": list(
                    self.source_render_issue_page_numbers
                ),
            }
        return fact

    def page_asset_refs(self) -> tuple[SourceAssetRef, ...]:
        return tuple(
            page.asset_ref(source_sha256=self.source_sha256)
            for page in self.pages
        )


@dataclass(frozen=True, slots=True)
class _ProcessOutcome:
    return_code: int
    timed_out: bool
    output_sha256: str
    output_size: int


class LibreOfficeSpreadsheetVisualRenderer:
    """Render verified spreadsheet bytes directly to a source-bound page profile."""

    def __init__(
        self,
        *,
        executable: str | PathLike[str] = "/opt/homebrew/bin/soffice",
        sandbox_executable: str | PathLike[str] = "/usr/bin/sandbox-exec",
        expected_version: str = LIBREOFFICE_DIRECT_VISUAL_EXPECTED_VERSION,
        config: SpreadsheetVisualRenderConfig | None = None,
    ) -> None:
        self._executable = Path(executable)
        self._sandbox_executable = Path(sandbox_executable)
        self._expected_version = expected_version
        self._config = config or SpreadsheetVisualRenderConfig()

    def render(
        self,
        source_bytes: bytes,
        *,
        source_format: DocumentFormat,
        expected_sheets: tuple[SpreadsheetExpectedSheet, ...],
        external_visual_relationships: tuple[
            SpreadsheetExternalVisualRelationship, ...
        ] = (),
    ) -> SpreadsheetVisualProfile:
        if not isinstance(source_bytes, bytes) or not source_bytes:
            raise SpreadsheetVisualProfileRejected(
                "Verified spreadsheet bytes are unavailable."
            )
        if source_format not in {DocumentFormat.XLSX, DocumentFormat.XLSB}:
            raise SpreadsheetVisualProfileRejected(
                "Direct visual rendering supports XLSX and XLSB only."
            )
        if [item.sheet_index for item in expected_sheets] != list(
            range(len(expected_sheets))
        ):
            raise SpreadsheetVisualProfileRejected(
                "Expected sheets must be complete and ordered."
            )
        executable = _validated_executable(self._executable, "LibreOffice")
        sandbox_executable = _validated_executable(
            self._sandbox_executable,
            "network sandbox",
        )
        if platform.system() != "Darwin":
            raise SpreadsheetVisualProfileRejected(
                "The configured renderer requires the macOS network sandbox."
            )
        (
            libreoffice_version,
            libreoffice_build_id,
            executable_sha256,
        ) = _libreoffice_identity(
            executable,
            expected_version=self._expected_version,
            timeout_seconds=self._config.timeout_seconds,
        )
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        cell_image_audit: XlsxCellImageAudit | None = None
        render_input_bytes = source_bytes
        render_input_derivation = "original_source"
        if source_format is DocumentFormat.XLSX:
            try:
                cell_image_audit = audit_xlsx_cell_images(source_bytes)
                if cell_image_audit.resolved_references:
                    render_input_bytes = (
                        materialize_xlsx_cell_image_render_overlay(
                            source_bytes,
                            audit=cell_image_audit,
                        )
                    )
                    render_input_derivation = (
                        "embedded_cell_image_ooxml_overlay"
                    )
            except Exception as exc:
                raise SpreadsheetVisualProfileRejected(
                    "XLSX cell-image references could not be rendered safely."
                ) from exc

        with tempfile.TemporaryDirectory(
            prefix="reqpilotx-spreadsheet-visual-"
        ) as root:
            workspace = Path(root)
            input_directory = workspace / "input"
            output_directory = workspace / "output"
            profile_directory = workspace / "profile"
            runtime_tmp_directory = workspace / "tmp"
            log_path = workspace / "render-output.log"
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
            extension = ".xlsb" if source_format is DocumentFormat.XLSB else ".xlsx"
            input_path = input_directory / f"source{extension}"
            input_path.write_bytes(render_input_bytes)
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
                _PDF_OUTPUT_FILTER,
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
                raise SpreadsheetVisualProfileRejected(
                    "Spreadsheet visual rendering exceeded its timeout."
                )
            if (
                outcome.return_code != 0
                or outcome.output_size > _MAX_RENDER_LOG_BYTES
            ):
                raise SpreadsheetVisualProfileRejected(
                    "Spreadsheet visual rendering failed within its safe output boundary."
                )
            if _sha256_file(executable) != executable_sha256:
                raise SpreadsheetVisualProfileRejected(
                    "LibreOffice executable identity changed during rendering."
                )
            output_path = output_directory / "source.pdf"
            output_files = tuple(
                item
                for item in output_directory.iterdir()
                if item.is_file() and not item.is_symlink()
            )
            if output_files != (output_path,):
                raise SpreadsheetVisualProfileRejected(
                    "Renderer did not produce exactly one expected PDF."
                )
            pdf_bytes = output_path.read_bytes()
            if not pdf_bytes:
                raise SpreadsheetVisualProfileRejected(
                    "Renderer produced an empty PDF."
                )
            sheets, page_sheet_indexes = _sheet_page_bindings(
                pdf_bytes,
                expected_sheets=expected_sheets,
            )
            pages = _render_pages(
                pdf_bytes,
                page_sheet_indexes=page_sheet_indexes,
                sheets=sheets,
                dpi=self._config.png_dpi,
            )
            source_render_issue_page_numbers = (
                _source_render_issue_pages(
                    cell_image_audit,
                    sheets=sheets,
                )
                if cell_image_audit is not None
                else ()
            )
            return SpreadsheetVisualProfile(
                source_sha256=source_sha256,
                source_size=len(source_bytes),
                source_format=source_format,
                renderer_id=LIBREOFFICE_DIRECT_VISUAL_RENDERER_ID,
                renderer_version=(
                    LIBREOFFICE_CELL_IMAGE_VISUAL_RENDERER_VERSION
                    if cell_image_audit is not None
                    and cell_image_audit.references
                    else LIBREOFFICE_DIRECT_VISUAL_RENDERER_VERSION
                ),
                libreoffice_version=libreoffice_version,
                libreoffice_build_id=libreoffice_build_id,
                libreoffice_executable_sha256=executable_sha256,
                pymupdf_version=str(pymupdf.VersionBind),
                config=self._config,
                render_output_sha256=outcome.output_sha256,
                pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
                pdf_bytes=pdf_bytes,
                sheets=sheets,
                pages=pages,
                external_visual_relationships=external_visual_relationships,
                cell_image_audit_sha256=(
                    cell_image_audit.audit_sha256
                    if cell_image_audit is not None
                    and cell_image_audit.references
                    else None
                ),
                cell_image_reference_count=(
                    len(cell_image_audit.references)
                    if cell_image_audit is not None
                    else 0
                ),
                resolved_cell_image_reference_count=(
                    len(cell_image_audit.resolved_references)
                    if cell_image_audit is not None
                    else 0
                ),
                unresolved_cell_image_reference_count=(
                    len(cell_image_audit.unresolved_references)
                    if cell_image_audit is not None
                    else 0
                ),
                render_input_sha256=(
                    hashlib.sha256(render_input_bytes).hexdigest()
                    if cell_image_audit is not None
                    and cell_image_audit.references
                    else None
                ),
                render_input_derivation=render_input_derivation,
                visual_fidelity_status=(
                    "partial_source_render_missing"
                    if cell_image_audit is not None
                    and cell_image_audit.unresolved_references
                    else "complete"
                ),
                source_render_issue_page_numbers=(
                    source_render_issue_page_numbers
                ),
            )


def _source_render_issue_pages(
    audit: XlsxCellImageAudit,
    *,
    sheets: tuple[SpreadsheetSheetPageBinding, ...],
) -> tuple[int, ...]:
    affected_sheet_indexes = {
        item.sheet_index for item in audit.unresolved_references
    }
    issue_pages: set[int] = set()
    for sheet_index in affected_sheet_indexes:
        if sheet_index >= len(sheets):
            raise SpreadsheetVisualProfileRejected(
                "Cell-image audit sheet is outside the rendered workbook."
            )
        sheet = sheets[sheet_index]
        if sheet.page_start is None or sheet.page_end is None:
            raise SpreadsheetVisualProfileRejected(
                "Unresolved cell-image reference has no rendered sheet page."
            )
        issue_pages.update(range(sheet.page_start, sheet.page_end + 1))
    return tuple(sorted(issue_pages))


def _sheet_page_bindings(
    pdf_bytes: bytes,
    *,
    expected_sheets: tuple[SpreadsheetExpectedSheet, ...],
) -> tuple[tuple[SpreadsheetSheetPageBinding, ...], tuple[int, ...]]:
    reader = PdfReader(BytesIO(pdf_bytes))
    page_count = len(reader.pages)
    if page_count < 1 or page_count > _MAX_RENDERED_PAGES:
        raise SpreadsheetVisualProfileRejected(
            "Rendered PDF page count exceeds the safe profile boundary."
        )
    destinations = _flatten_outline_destinations(reader.outline)
    outline: list[tuple[int, str]] = []
    for destination in destinations:
        try:
            page_number = reader.get_destination_page_number(destination) + 1
        except Exception as exc:
            raise SpreadsheetVisualProfileRejected(
                "Rendered PDF sheet outline cannot be resolved."
            ) from exc
        title = getattr(destination, "title", None)
        if not isinstance(title, str) or not title:
            raise SpreadsheetVisualProfileRejected(
                "Rendered PDF sheet outline has no title."
            )
        outline.append(
            (page_number, hashlib.sha256(title.encode("utf-8")).hexdigest())
        )
    if not outline or outline[0][0] != 1:
        raise SpreadsheetVisualProfileRejected(
            "Rendered PDF sheet outline does not start at page 1."
        )
    if [page for page, _ in outline] != sorted(
        {page for page, _ in outline}
    ):
        raise SpreadsheetVisualProfileRejected(
            "Rendered PDF sheet outline pages are not unique and ordered."
        )

    expected_by_hash = {
        item.render_outline_name_sha256 or item.sheet_name_sha256: item
        for item in expected_sheets
    }
    if len(expected_by_hash) != len(expected_sheets):
        raise SpreadsheetVisualProfileRejected(
            "Expected sheet names must be unique."
        )
    seen_indexes: set[int] = set()
    outlined: list[tuple[int, int]] = []
    for page_number, sheet_hash in outline:
        expected = expected_by_hash.get(sheet_hash)
        if (
            expected is None
            or expected.sheet_state != "visible"
            or not expected.expects_rendered_pages
            or expected.sheet_index in seen_indexes
        ):
            raise SpreadsheetVisualProfileRejected(
                "Rendered PDF sheet outline does not match expected visible sheets."
            )
        seen_indexes.add(expected.sheet_index)
        outlined.append((page_number, expected.sheet_index))
    expected_rendered_indexes = {
        item.sheet_index
        for item in expected_sheets
        if item.sheet_state == "visible" and item.expects_rendered_pages
    }
    if seen_indexes != expected_rendered_indexes:
        raise SpreadsheetVisualProfileRejected(
            "Rendered PDF outline does not cover every printable visible sheet."
        )
    if [index for _, index in outlined] != sorted(index for _, index in outlined):
        raise SpreadsheetVisualProfileRejected(
            "Rendered PDF sheet order differs from the source workbook."
        )

    ranges: dict[int, tuple[int, int]] = {}
    for position, (start_page, sheet_index) in enumerate(outlined):
        end_page = (
            outlined[position + 1][0] - 1
            if position + 1 < len(outlined)
            else page_count
        )
        ranges[sheet_index] = (start_page, end_page)
    bindings = tuple(
        SpreadsheetSheetPageBinding(
            sheet_index=item.sheet_index,
            sheet_name_sha256=item.sheet_name_sha256,
            sheet_state=item.sheet_state,
            page_start=(
                ranges[item.sheet_index][0]
                if item.sheet_index in ranges
                else None
            ),
            page_end=(
                ranges[item.sheet_index][1]
                if item.sheet_index in ranges
                else None
            ),
        )
        for item in expected_sheets
    )
    page_sheet_indexes = tuple(
        next(
            item.sheet_index
            for item in bindings
            if item.page_start is not None
            and item.page_end is not None
            and item.page_start <= page_number <= item.page_end
        )
        for page_number in range(1, page_count + 1)
    )
    return bindings, page_sheet_indexes


def _flatten_outline_destinations(items: object) -> list[object]:
    if not isinstance(items, list):
        return []
    result: list[object] = []
    for item in items:
        if isinstance(item, list):
            result.extend(_flatten_outline_destinations(item))
        else:
            result.append(item)
    return result


def _render_pages(
    pdf_bytes: bytes,
    *,
    page_sheet_indexes: tuple[int, ...],
    sheets: tuple[SpreadsheetSheetPageBinding, ...],
    dpi: int,
) -> tuple[SpreadsheetRenderedPage, ...]:
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise SpreadsheetVisualProfileRejected(
            "Rendered PDF could not be opened for page capture."
        ) from exc
    reader = PdfReader(BytesIO(pdf_bytes))
    try:
        if len(document) != len(reader.pages) or len(document) != len(
            page_sheet_indexes
        ):
            raise SpreadsheetVisualProfileRejected(
                "Rendered PDF page readers disagree on page count."
            )
        pages: list[SpreadsheetRenderedPage] = []
        matrix = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
        for index, page in enumerate(document):
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            png_bytes = pixmap.tobytes("png")
            one_page = PdfWriter()
            one_page.add_page(reader.pages[index])
            one_page_output = BytesIO()
            one_page.write(one_page_output)
            sheet_index = page_sheet_indexes[index]
            pages.append(
                SpreadsheetRenderedPage(
                    page_number=index + 1,
                    sheet_index=sheet_index,
                    sheet_name_sha256=sheets[sheet_index].sheet_name_sha256,
                    width_pixels=pixmap.width,
                    height_pixels=pixmap.height,
                    png_sha256=hashlib.sha256(png_bytes).hexdigest(),
                    standalone_pdf_sha256=hashlib.sha256(
                        one_page_output.getvalue()
                    ).hexdigest(),
                    png_bytes=png_bytes,
                )
            )
        return tuple(pages)
    finally:
        document.close()


def _validated_executable(path: Path, kind: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise SpreadsheetVisualProfileRejected(
            f"The configured {kind} executable is unavailable."
        ) from exc
    if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
        raise SpreadsheetVisualProfileRejected(
            f"The configured {kind} executable is unavailable."
        )
    return resolved


def _libreoffice_identity(
    executable: Path,
    *,
    expected_version: str,
    timeout_seconds: int,
) -> tuple[str, str, str]:
    try:
        completed = subprocess.run(
            (str(executable), "--version"),
            check=False,
            cwd=str(executable.parent),
            env=_isolated_environment(Path(tempfile.gettempdir())),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=min(timeout_seconds, 10),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpreadsheetVisualProfileRejected(
            "LibreOffice identity verification failed."
        ) from exc
    output = completed.stdout or b""
    if completed.returncode != 0 or len(output) > _VERSION_OUTPUT_BYTES:
        raise SpreadsheetVisualProfileRejected(
            "LibreOffice identity verification failed."
        )
    match = _VERSION_PATTERN.fullmatch(output.strip())
    if match is None:
        raise SpreadsheetVisualProfileRejected(
            "LibreOffice returned an unrecognized version identity."
        )
    version = match.group(1).decode("ascii")
    build_id = match.group(2).decode("ascii")
    if version != expected_version:
        raise SpreadsheetVisualProfileRejected(
            "LibreOffice version does not match the pinned visual profile."
        )
    return version, build_id, _sha256_file(executable)


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
            raise SpreadsheetVisualProfileRejected(
                "Spreadsheet visual renderer process could not be started."
            ) from exc
        timed_out = False
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            return_code = process.wait()
    return _ProcessOutcome(
        return_code=return_code,
        timed_out=timed_out,
        output_sha256=_sha256_file(output_path),
        output_size=output_path.stat().st_size,
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


__all__ = [
    "LIBREOFFICE_DIRECT_VISUAL_RENDERER_ID",
    "LIBREOFFICE_DIRECT_VISUAL_RENDERER_VERSION",
    "SPREADSHEET_VISUAL_PROFILE_SCHEMA_VERSION",
    "LibreOfficeSpreadsheetVisualRenderer",
    "SpreadsheetExpectedSheet",
    "SpreadsheetExternalVisualRelationship",
    "SpreadsheetRenderedPage",
    "SpreadsheetSheetPageBinding",
    "SpreadsheetVisualProfile",
    "SpreadsheetVisualProfileRejected",
    "SpreadsheetVisualRenderConfig",
    "SpreadsheetVisualRendererPort",
]
