"""Produce a source-bound generic whole-document render profile for DOCX.

The producer renders immutable, externally pinned DOCX bytes to PDF through a
network-confined LibreOffice process, captures every PDF page as PNG with
PyMuPDF, and atomically publishes the generic render-profile contract consumed
by ``document_visual_review_bundle.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from os import PathLike
from pathlib import Path
from typing import Protocol, runtime_checkable

import pymupdf

from scripts.document_visual_review_bundle import (
    PROFILE_HASH_CONTRACT,
    RENDER_PROFILE_SCHEMA_VERSION,
    _validate_render_profile,
    canonical_profile_sha256,
)
from scripts.storage_backup import BackupError, _rename_no_replace


DOCX_PDF_RENDERER_ID = "libreoffice-docx-pdf"
DOCX_RENDER_PROFILE_PRODUCER_VERSION = "1.0.0"
DOCX_EXPECTED_LIBREOFFICE_VERSION = "26.2.4.2"
DOCX_PDF_OUTPUT_FILTER = "pdf:writer_pdf_Export"
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_SOURCE_BYTES = 512 * 1024 * 1024
_MAX_DOCX_ENTRY_COUNT = 100_000
_MAX_DOCX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_CONTENT_TYPES_BYTES = 2 * 1024 * 1024
_MAX_RENDER_LOG_BYTES = 1024 * 1024
_MAX_PDF_BYTES = 1024 * 1024 * 1024
_MAX_RENDERED_PAGES = 5_000
_MAX_PAGE_PIXELS = 200_000_000
_VERSION_OUTPUT_BYTES = 16 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$")
_VERSION_PATTERN = re.compile(
    rb"^LibreOffice ([0-9]+(?:\.[0-9]+){3}) ([0-9a-f]{8,})\s*$"
)
_MACOS_NETWORK_SANDBOX_PROFILE = (
    "(version 1)(allow default)"
    "(deny network-outbound "
    "(require-all "
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


class DocxVisualRenderProfileRejected(RuntimeError):
    """The source, renderer, output, or atomic publication failed closed."""


@dataclass(frozen=True, slots=True)
class DocxVisualRenderConfig:
    png_dpi: int = 144
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if (
            isinstance(self.png_dpi, bool)
            or self.png_dpi < 72
            or self.png_dpi > 300
        ):
            raise ValueError("png_dpi must stay within 72..300")
        if (
            isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds < 1
            or self.timeout_seconds > 600
        ):
            raise ValueError("timeout_seconds must stay within 1..600")


@dataclass(frozen=True, slots=True)
class DocxPdfRenderResult:
    pdf_bytes: bytes = field(repr=False)
    pdf_sha256: str
    renderer_id: str
    renderer_version: str
    renderer_config_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.pdf_bytes, bytes) or not self.pdf_bytes:
            raise ValueError("pdf_bytes must be non-empty bytes")
        _validate_sha256(self.pdf_sha256, "pdf_sha256")
        _validate_sha256(
            self.renderer_config_sha256,
            "renderer_config_sha256",
        )
        if hashlib.sha256(self.pdf_bytes).hexdigest() != self.pdf_sha256:
            raise ValueError("pdf_sha256 must bind pdf_bytes")
        _validate_safe_id(self.renderer_id, "renderer_id")
        _validate_safe_id(self.renderer_version, "renderer_version")


@runtime_checkable
class DocxPdfRendererPort(Protocol):
    def render(self, source_bytes: bytes) -> DocxPdfRenderResult:
        ...


@dataclass(frozen=True, slots=True)
class DocxVisualRenderProfile:
    root: Path
    profile_path: Path
    profile: dict[str, object]
    profile_file_sha256: str


@dataclass(frozen=True, slots=True)
class _PinnedSource:
    path: Path
    payload: bytes = field(repr=False)
    sha256: str
    device: int
    inode: int
    size: int


@dataclass(frozen=True, slots=True)
class _ProcessOutcome:
    return_code: int
    timed_out: bool
    output_size: int


class LibreOfficeDocxPdfRenderer:
    """Render pinned DOCX bytes to one PDF under the macOS network sandbox."""

    def __init__(
        self,
        *,
        expected_executable_sha256: str,
        executable: str | PathLike[str] = "/opt/homebrew/bin/soffice",
        sandbox_executable: str | PathLike[str] = "/usr/bin/sandbox-exec",
        expected_version: str = DOCX_EXPECTED_LIBREOFFICE_VERSION,
        timeout_seconds: int = 120,
    ) -> None:
        _validate_sha256(
            expected_executable_sha256,
            "expected_executable_sha256",
        )
        if (
            not isinstance(expected_version, str)
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", expected_version) is None
        ):
            raise ValueError("expected_version must be a four-part version")
        if (
            isinstance(timeout_seconds, bool)
            or timeout_seconds < 1
            or timeout_seconds > 600
        ):
            raise ValueError("timeout_seconds must stay within 1..600")
        self._expected_executable_sha256 = expected_executable_sha256
        self._executable = Path(executable)
        self._sandbox_executable = Path(sandbox_executable)
        self._expected_version = expected_version
        self._timeout_seconds = timeout_seconds

    def render(self, source_bytes: bytes) -> DocxPdfRenderResult:
        _validate_docx_container(source_bytes)
        if platform.system() != "Darwin":
            raise DocxVisualRenderProfileRejected(
                "the configured DOCX renderer requires the macOS network sandbox"
            )
        executable = _validated_executable(
            self._executable,
            "LibreOffice",
        )
        sandbox_executable = _validated_executable(
            self._sandbox_executable,
            "network sandbox",
        )
        (
            libreoffice_version,
            libreoffice_build_id,
            executable_sha256,
        ) = _libreoffice_identity(
            executable,
            expected_version=self._expected_version,
            expected_executable_sha256=(
                self._expected_executable_sha256
            ),
            timeout_seconds=self._timeout_seconds,
        )

        with tempfile.TemporaryDirectory(
            prefix="reqpilotx-docx-visual-render-"
        ) as temporary_root:
            workspace = Path(temporary_root)
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
            registry_path = (
                profile_directory / "user" / "registrymodifications.xcu"
            )
            _write_new_file(
                registry_path,
                _PROFILE_REGISTRY.encode("utf-8"),
            )
            input_path = input_directory / "source.docx"
            _write_new_file(input_path, source_bytes)
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
                DOCX_PDF_OUTPUT_FILTER,
                "--outdir",
                str(output_directory),
                str(input_path),
            )
            outcome = _run_bounded_process(
                command,
                cwd=workspace,
                environment=_isolated_environment(runtime_tmp_directory),
                timeout_seconds=self._timeout_seconds,
                output_path=log_path,
            )
            if outcome.timed_out:
                raise DocxVisualRenderProfileRejected(
                    "DOCX visual rendering exceeded its timeout"
                )
            if (
                outcome.return_code != 0
                or outcome.output_size > _MAX_RENDER_LOG_BYTES
            ):
                raise DocxVisualRenderProfileRejected(
                    "DOCX visual rendering failed within its safe output boundary"
                )
            if _sha256_file(executable) != executable_sha256:
                raise DocxVisualRenderProfileRejected(
                    "LibreOffice executable identity changed during rendering"
                )
            output_path = output_directory / "source.pdf"
            output_files = tuple(
                item
                for item in output_directory.iterdir()
                if item.is_file() and not item.is_symlink()
            )
            if output_files != (output_path,):
                raise DocxVisualRenderProfileRejected(
                    "renderer did not produce exactly one expected PDF"
                )
            pdf_bytes = output_path.read_bytes()
            if not pdf_bytes or len(pdf_bytes) > _MAX_PDF_BYTES:
                raise DocxVisualRenderProfileRejected(
                    "renderer PDF is empty or exceeds the safe size boundary"
                )
            renderer_config_sha256 = _canonical_sha256(
                {
                    "output_filter": DOCX_PDF_OUTPUT_FILTER,
                    "libreoffice_version": libreoffice_version,
                    "libreoffice_build_id": libreoffice_build_id,
                    "libreoffice_executable_sha256": executable_sha256,
                    "isolated_temporary_directory": True,
                    "isolated_user_profile": True,
                    "macro_policy": "reject_source_and_disable_runtime",
                    "active_content_policy": "disable_runtime",
                    "network_policy": (
                        "macos_sandbox_deny_non_loopback_ip"
                    ),
                    "timeout_seconds": self._timeout_seconds,
                }
            )
            return DocxPdfRenderResult(
                pdf_bytes=pdf_bytes,
                pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
                renderer_id=DOCX_PDF_RENDERER_ID,
                renderer_version=libreoffice_version,
                renderer_config_sha256=renderer_config_sha256,
            )


def produce_docx_visual_render_profile(
    source_path: Path,
    *,
    expected_source_sha256: str,
    output_dir: Path,
    renderer: DocxPdfRendererPort,
    config: DocxVisualRenderConfig | None = None,
) -> DocxVisualRenderProfile:
    """Render one pinned DOCX and atomically publish all pages plus profile."""

    if not isinstance(renderer, DocxPdfRendererPort):
        raise TypeError("renderer must implement DocxPdfRendererPort")
    resolved_config = config or DocxVisualRenderConfig()
    source = _pin_docx_source(
        source_path,
        expected_source_sha256=expected_source_sha256,
    )
    destination = Path(output_dir).expanduser()
    try:
        destination_parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise DocxVisualRenderProfileRejected(
            "render profile output parent is unavailable"
        ) from exc
    if not destination_parent.is_dir():
        raise DocxVisualRenderProfileRejected(
            "render profile output parent is unavailable"
        )
    destination = destination_parent / destination.name
    if destination.exists() or destination.is_symlink():
        raise DocxVisualRenderProfileRejected(
            "render profile output already exists"
        )

    try:
        rendered_pdf = renderer.render(source.payload)
    except DocxVisualRenderProfileRejected:
        raise
    except Exception as exc:
        raise DocxVisualRenderProfileRejected(
            "DOCX PDF renderer failed"
        ) from exc
    if not isinstance(rendered_pdf, DocxPdfRenderResult):
        raise DocxVisualRenderProfileRejected(
            "DOCX PDF renderer returned an invalid result"
        )

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination_parent,
        )
    )
    staging.chmod(0o700)
    try:
        page_directory = staging / "pages"
        page_directory.mkdir(mode=0o700)
        page_records = _render_pdf_pages(
            rendered_pdf.pdf_bytes,
            page_directory=page_directory,
            png_dpi=resolved_config.png_dpi,
        )
        pymupdf_version = str(pymupdf.VersionBind)
        renderer_id = (
            f"{rendered_pdf.renderer_id}+pymupdf-page-png"
        )
        renderer_version = (
            f"{rendered_pdf.renderer_version}+pymupdf-{pymupdf_version}"
        )
        _validate_safe_id(renderer_id, "renderer_id")
        _validate_safe_id(renderer_version, "renderer_version")
        renderer_config_sha256 = _canonical_sha256(
            {
                "producer_version": (
                    DOCX_RENDER_PROFILE_PRODUCER_VERSION
                ),
                "pdf_renderer_config_sha256": (
                    rendered_pdf.renderer_config_sha256
                ),
                "page_renderer_id": "pymupdf-page-png",
                "pymupdf_version": pymupdf_version,
                "png_dpi": resolved_config.png_dpi,
                "alpha": False,
            }
        )
        generic_profile: dict[str, object] = {
            "schema_version": RENDER_PROFILE_SCHEMA_VERSION,
            "profile_hash_contract": PROFILE_HASH_CONTRACT,
            "source": {
                "source_sha256": source.sha256,
                "format": "docx",
            },
            "renderer": {
                "id": renderer_id,
                "version": renderer_version,
                "config_sha256": renderer_config_sha256,
            },
            "page_count": len(page_records),
            "pages": page_records,
        }
        generic_profile["profile_sha256"] = canonical_profile_sha256(
            generic_profile
        )
        _validate_render_profile(
            generic_profile,
            source_sha256=source.sha256,
            document_format="docx",
        )
        profile_path = staging / "render-profile.json"
        profile_payload = (
            json.dumps(
                generic_profile,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        _write_new_file(profile_path, profile_payload)
        _verify_source_unchanged(source)
        _fsync_directory(page_directory)
        _fsync_directory(staging)
        try:
            _rename_no_replace(staging, destination)
        except BackupError as exc:
            raise DocxVisualRenderProfileRejected(
                "render profile atomic publish failed"
            ) from exc
        _fsync_directory(destination_parent)
        published_profile_path = destination / "render-profile.json"
        return DocxVisualRenderProfile(
            root=destination,
            profile_path=published_profile_path,
            profile=generic_profile,
            profile_file_sha256=_sha256_file(published_profile_path),
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _pin_docx_source(
    source_path: Path,
    *,
    expected_source_sha256: str,
) -> _PinnedSource:
    _validate_sha256(expected_source_sha256, "expected_source_sha256")
    candidate = Path(source_path).expanduser()
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise DocxVisualRenderProfileRejected(
            "DOCX source parent is unavailable"
        ) from exc
    candidate = parent / candidate.name
    if candidate.suffix.lower() != ".docx":
        raise DocxVisualRenderProfileRejected(
            "DOCX source must use the .docx extension"
        )
    payload, source_stat = _read_regular_file_no_follow(candidate)
    if not payload or len(payload) > _MAX_SOURCE_BYTES:
        raise DocxVisualRenderProfileRejected(
            "DOCX source is empty or exceeds the safe size boundary"
        )
    source_sha256 = hashlib.sha256(payload).hexdigest()
    if source_sha256 != expected_source_sha256:
        raise DocxVisualRenderProfileRejected(
            "DOCX source hash does not match the external pin"
        )
    _validate_docx_container(payload)
    return _PinnedSource(
        path=candidate,
        payload=payload,
        sha256=source_sha256,
        device=source_stat.st_dev,
        inode=source_stat.st_ino,
        size=source_stat.st_size,
    )


def _verify_source_unchanged(source: _PinnedSource) -> None:
    payload, source_stat = _read_regular_file_no_follow(source.path)
    if (
        source_stat.st_dev != source.device
        or source_stat.st_ino != source.inode
        or source_stat.st_size != source.size
        or hashlib.sha256(payload).hexdigest() != source.sha256
    ):
        raise DocxVisualRenderProfileRejected(
            "DOCX source changed during rendering"
        )


def _read_regular_file_no_follow(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DocxVisualRenderProfileRejected(
            "DOCX source is unavailable or not a regular file"
        ) from exc
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise DocxVisualRenderProfileRejected(
                "DOCX source is unavailable or not a regular file"
            )
        chunks: list[bytes] = []
        remaining = _MAX_SOURCE_BYTES + 1
        while remaining > 0:
            chunk = os.read(
                descriptor,
                min(_HASH_CHUNK_BYTES, remaining),
            )
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if os.read(descriptor, 1):
            raise DocxVisualRenderProfileRejected(
                "DOCX source exceeds the safe size boundary"
            )
        return payload, source_stat
    finally:
        os.close(descriptor)


def _validate_docx_container(source_bytes: bytes) -> None:
    if not isinstance(source_bytes, bytes) or not source_bytes:
        raise DocxVisualRenderProfileRejected(
            "verified DOCX bytes are unavailable"
        )
    try:
        with zipfile.ZipFile(BytesIO(source_bytes)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if (
                len(entries) > _MAX_DOCX_ENTRY_COUNT
                or len(names) != len(entries)
                or sum(entry.file_size for entry in entries)
                > _MAX_DOCX_UNCOMPRESSED_BYTES
                or any(entry.flag_bits & 0x1 for entry in entries)
                or any(
                    not entry.filename
                    or entry.filename.startswith("/")
                    or "\\" in entry.filename
                    or "\x00" in entry.filename
                    or ".." in entry.filename.split("/")
                    for entry in entries
                )
                or "[Content_Types].xml" not in names
                or "word/document.xml" not in names
                or "word/vbaProject.bin" in names
            ):
                raise DocxVisualRenderProfileRejected(
                    "DOCX package violates the safe render boundary"
                )
            content_types_info = archive.getinfo("[Content_Types].xml")
            if content_types_info.file_size > _MAX_CONTENT_TYPES_BYTES:
                raise DocxVisualRenderProfileRejected(
                    "DOCX package violates the safe render boundary"
                )
            content_types = archive.read(content_types_info)
            if b"macroEnabled" in content_types:
                raise DocxVisualRenderProfileRejected(
                    "macro-enabled DOCX packages are not renderable"
                )
    except DocxVisualRenderProfileRejected:
        raise
    except (
        OSError,
        KeyError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise DocxVisualRenderProfileRejected(
            "DOCX package is invalid"
        ) from exc


def _render_pdf_pages(
    pdf_bytes: bytes,
    *,
    page_directory: Path,
    png_dpi: int,
) -> list[dict[str, object]]:
    if not pdf_bytes or len(pdf_bytes) > _MAX_PDF_BYTES:
        raise DocxVisualRenderProfileRejected(
            "renderer PDF is empty or exceeds the safe size boundary"
        )
    previous_error_display = pymupdf.TOOLS.mupdf_display_errors()
    previous_warning_display = pymupdf.TOOLS.mupdf_display_warnings()
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    document = None
    try:
        try:
            document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        except Exception as exc:
            raise DocxVisualRenderProfileRejected(
                "renderer PDF could not be opened for page capture"
            ) from exc
        if document.needs_pass or len(document) < 1:
            raise DocxVisualRenderProfileRejected(
                "renderer PDF is encrypted or has no pages"
            )
        if len(document) > _MAX_RENDERED_PAGES:
            raise DocxVisualRenderProfileRejected(
                "renderer PDF exceeds the safe page boundary"
            )
        matrix = pymupdf.Matrix(png_dpi / 72.0, png_dpi / 72.0)
        records: list[dict[str, object]] = []
        for index, page in enumerate(document):
            try:
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                if (
                    pixmap.width < 1
                    or pixmap.height < 1
                    or pixmap.width * pixmap.height > _MAX_PAGE_PIXELS
                ):
                    raise DocxVisualRenderProfileRejected(
                        "rendered page exceeds the safe pixel boundary"
                    )
                png_bytes = pixmap.tobytes("png")
            except DocxVisualRenderProfileRejected:
                raise
            except Exception as exc:
                raise DocxVisualRenderProfileRejected(
                    "renderer PDF page capture failed"
                ) from exc
            page_number = index + 1
            relative_path = f"pages/page-{page_number:04d}.png"
            _write_new_file(
                page_directory / f"page-{page_number:04d}.png",
                png_bytes,
            )
            records.append(
                {
                    "page_number": page_number,
                    "locator": {"kind": "OFFICE_PAGE"},
                    "artifact": {
                        "path": relative_path,
                        "sha256": hashlib.sha256(png_bytes).hexdigest(),
                        "media_type": "image/png",
                        "width_px": pixmap.width,
                        "height_px": pixmap.height,
                    },
                }
            )
        return records
    finally:
        if document is not None:
            document.close()
        pymupdf.TOOLS.mupdf_display_errors(previous_error_display)
        pymupdf.TOOLS.mupdf_display_warnings(previous_warning_display)


def _validated_executable(path: Path, kind: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise DocxVisualRenderProfileRejected(
            f"configured {kind} executable is unavailable"
        ) from exc
    if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
        raise DocxVisualRenderProfileRejected(
            f"configured {kind} executable is unavailable"
        )
    return resolved


def _libreoffice_identity(
    executable: Path,
    *,
    expected_version: str,
    expected_executable_sha256: str,
    timeout_seconds: int,
) -> tuple[str, str, str]:
    executable_sha256 = _sha256_file(executable)
    if executable_sha256 != expected_executable_sha256:
        raise DocxVisualRenderProfileRejected(
            "LibreOffice executable does not match the external pin"
        )
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
        raise DocxVisualRenderProfileRejected(
            "LibreOffice identity verification failed"
        ) from exc
    output = completed.stdout or b""
    if completed.returncode != 0 or len(output) > _VERSION_OUTPUT_BYTES:
        raise DocxVisualRenderProfileRejected(
            "LibreOffice identity verification failed"
        )
    match = _VERSION_PATTERN.fullmatch(output.strip())
    if match is None:
        raise DocxVisualRenderProfileRejected(
            "LibreOffice returned an unrecognized version identity"
        )
    version = match.group(1).decode("ascii")
    build_id = match.group(2).decode("ascii")
    if version != expected_version:
        raise DocxVisualRenderProfileRejected(
            "LibreOffice version does not match the external pin"
        )
    return version, build_id, executable_sha256


def _run_bounded_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    output_path: Path,
) -> _ProcessOutcome:
    with output_path.open("xb") as output:
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
            raise DocxVisualRenderProfileRejected(
                "DOCX visual renderer process could not be started"
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


def _write_new_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _validate_safe_id(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 256
        or _SAFE_ID_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(f"{name} must use the safe identity syntax")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render one externally pinned DOCX into the generic whole-document "
            "visual render-profile contract."
        )
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--libreoffice-executable",
        type=Path,
        default=Path("/opt/homebrew/bin/soffice"),
    )
    parser.add_argument(
        "--expected-libreoffice-version",
        default=DOCX_EXPECTED_LIBREOFFICE_VERSION,
    )
    parser.add_argument(
        "--expected-libreoffice-executable-sha256",
        required=True,
    )
    parser.add_argument(
        "--sandbox-executable",
        type=Path,
        default=Path("/usr/bin/sandbox-exec"),
    )
    parser.add_argument("--png-dpi", type=int, default=144)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    arguments = parser.parse_args(argv)
    try:
        config = DocxVisualRenderConfig(
            png_dpi=arguments.png_dpi,
            timeout_seconds=arguments.timeout_seconds,
        )
        renderer = LibreOfficeDocxPdfRenderer(
            executable=arguments.libreoffice_executable,
            sandbox_executable=arguments.sandbox_executable,
            expected_version=arguments.expected_libreoffice_version,
            expected_executable_sha256=(
                arguments.expected_libreoffice_executable_sha256
            ),
            timeout_seconds=config.timeout_seconds,
        )
        result = produce_docx_visual_render_profile(
            arguments.source,
            expected_source_sha256=arguments.expected_source_sha256,
            output_dir=arguments.output_dir,
            renderer=renderer,
            config=config,
        )
    except (DocxVisualRenderProfileRejected, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "profile_path": str(result.profile_path),
                "profile_file_sha256": result.profile_file_sha256,
                "profile_sha256": result.profile["profile_sha256"],
                "page_count": result.profile["page_count"],
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "DOCX_EXPECTED_LIBREOFFICE_VERSION",
    "DOCX_PDF_RENDERER_ID",
    "DOCX_RENDER_PROFILE_PRODUCER_VERSION",
    "DocxPdfRenderResult",
    "DocxPdfRendererPort",
    "DocxVisualRenderConfig",
    "DocxVisualRenderProfile",
    "DocxVisualRenderProfileRejected",
    "LibreOfficeDocxPdfRenderer",
    "produce_docx_visual_render_profile",
]


if __name__ == "__main__":
    raise SystemExit(main())
