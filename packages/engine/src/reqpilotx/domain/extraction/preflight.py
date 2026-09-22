from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


PREFLIGHT_SCHEMA_VERSION = "document-preflight/v2"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DocumentFormat(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    XLSB = "xlsb"
    LEGACY_DOC = "legacy_doc"
    TIF = "tif"
    TXT = "txt"
    UNKNOWN = "unknown"


class SupportStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class AuthenticityStatus(str, Enum):
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    UNVERIFIABLE = "unverifiable"


class IntegrityStatus(str, Enum):
    VALID = "valid"
    EMPTY = "empty"
    CORRUPT = "corrupt"
    UNVERIFIED = "unverified"


class AccessStatus(str, Enum):
    READABLE = "readable"
    READABLE_WITH_EMPTY_PASSWORD = "readable_with_empty_password"
    PASSWORD_REQUIRED = "password_required"
    UNSUPPORTED_ENCRYPTION = "unsupported_encryption"
    NOT_INSPECTED = "not_inspected"


class DiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticCode(str, Enum):
    EMPTY_FILE = "empty_file"
    EXTENSION_SIGNATURE_MISMATCH = "extension_signature_mismatch"
    UNKNOWN_BINARY_FORMAT = "unknown_binary_format"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CORRUPT_DOCUMENT = "corrupt_document"
    PASSWORD_REQUIRED = "password_required"
    PDF_SECURITY_RESTRICTIONS = "pdf_security_restrictions"
    UNSUPPORTED_PDF_ENCRYPTION = "unsupported_pdf_encryption"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"


@dataclass(frozen=True, slots=True)
class PreflightDiagnostic:
    code: DiagnosticCode
    severity: DiagnosticSeverity
    message: str

    def __post_init__(self) -> None:
        _require_enum(self.code, DiagnosticCode, "code")
        _require_enum(self.severity, DiagnosticSeverity, "severity")
        if not self.message.strip():
            raise ValueError("A preflight diagnostic must have a message.")


@dataclass(frozen=True, slots=True)
class RawDocumentIdentity:
    """Identity derived only from the original file bytes and its declaration."""

    source_name: str
    source_sha256: str
    source_size: int
    declared_extension: str
    detected_format: DocumentFormat
    mime_type: str

    def __post_init__(self) -> None:
        _require_enum(self.detected_format, DocumentFormat, "detected_format")
        if not self.source_name.strip():
            raise ValueError("source_name must not be empty")
        if not _SHA256_PATTERN.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        if self.source_size < 0:
            raise ValueError("source_size must not be negative")
        if self.declared_extension != self.declared_extension.lower():
            raise ValueError("declared_extension must be lowercase")
        if self.declared_extension and not self.declared_extension.startswith("."):
            raise ValueError("declared_extension must be empty or start with a dot")
        if not self.mime_type.strip():
            raise ValueError("mime_type must not be empty")


@dataclass(frozen=True, slots=True)
class DocumentPreflightReport:
    identity: RawDocumentIdentity
    authenticity_status: AuthenticityStatus
    support_status: SupportStatus
    integrity_status: IntegrityStatus
    access_status: AccessStatus
    diagnostics: tuple[PreflightDiagnostic, ...] = ()
    page_count: int | None = None
    schema_version: str = field(default=PREFLIGHT_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _require_enum(
            self.authenticity_status, AuthenticityStatus, "authenticity_status"
        )
        _require_enum(self.support_status, SupportStatus, "support_status")
        _require_enum(self.integrity_status, IntegrityStatus, "integrity_status")
        _require_enum(self.access_status, AccessStatus, "access_status")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, PreflightDiagnostic) for item in self.diagnostics
        ):
            raise TypeError("diagnostics must be a tuple of PreflightDiagnostic values")
        if self.page_count is not None and self.page_count < 0:
            raise ValueError("page_count must not be negative")
        if (
            self.identity.source_size == 0
            and self.integrity_status is not IntegrityStatus.EMPTY
        ):
            raise ValueError("A zero-byte document must have EMPTY integrity status")
        if (
            self.access_status is AccessStatus.PASSWORD_REQUIRED
            and self.identity.detected_format is not DocumentFormat.PDF
        ):
            raise ValueError("PASSWORD_REQUIRED is currently valid only for PDF")
        if (
            self.access_status is AccessStatus.READABLE_WITH_EMPTY_PASSWORD
            and self.identity.detected_format is not DocumentFormat.PDF
        ):
            raise ValueError(
                "READABLE_WITH_EMPTY_PASSWORD is currently valid only for PDF"
            )

    @property
    def processable(self) -> bool:
        return bool(
            self.authenticity_status is AuthenticityStatus.MATCHED
            and self.support_status is SupportStatus.SUPPORTED
            and self.integrity_status is IntegrityStatus.VALID
            and self.access_status
            in {AccessStatus.READABLE, AccessStatus.READABLE_WITH_EMPTY_PASSWORD}
        )

    @property
    def controlled_conversion_eligible(self) -> bool:
        """Whether an exact source may enter an explicitly configured converter.

        This is deliberately narrower than general format support.  XLSB stays
        unsupported for native parsing and for the legacy serving path; an
        application use case may authorize only a matching adapter that
        advertises a fail-closed conversion boundary.
        """

        return bool(
            self.identity.detected_format is DocumentFormat.XLSB
            and self.identity.declared_extension == ".xlsb"
            and self.authenticity_status is AuthenticityStatus.MATCHED
            and self.support_status is SupportStatus.UNSUPPORTED
            and self.integrity_status is IntegrityStatus.VALID
            and self.access_status is AccessStatus.NOT_INSPECTED
            and tuple(item.code for item in self.diagnostics)
            == (DiagnosticCode.UNSUPPORTED_FORMAT,)
        )

    @property
    def attachment_acceptable(self) -> bool:
        """Whether exact bytes may enter the opaque attachment lane.

        This is deliberately separate from ``processable``. A TIF is not a
        supported requirement-extraction input, but a signature-matched,
        non-empty TIF may still be retained without interpretation or OCR.
        """

        if (
            self.authenticity_status is not AuthenticityStatus.MATCHED
            or self.integrity_status is not IntegrityStatus.VALID
            or self.identity.source_size <= 0
            or self.identity.detected_format
            not in {DocumentFormat.PDF, DocumentFormat.TIF}
        ):
            return False
        if self.identity.detected_format is DocumentFormat.TIF:
            return self.access_status is AccessStatus.READABLE
        return self.access_status in {
            AccessStatus.READABLE,
            AccessStatus.READABLE_WITH_EMPTY_PASSWORD,
        }

    @property
    def password_required(self) -> bool:
        return self.access_status is AccessStatus.PASSWORD_REQUIRED

    @property
    def source_sha256(self) -> str:
        return self.identity.source_sha256

    @property
    def source_size(self) -> int:
        return self.identity.source_size


def _require_enum(value: object, expected_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(f"{field_name} must be a {expected_type.__name__} value")
