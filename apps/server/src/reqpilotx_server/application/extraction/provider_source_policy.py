from __future__ import annotations

from enum import Enum

from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot, SourceIRStatus


class ProviderSourceIRRejectionCode(str, Enum):
    SOURCE_IR_NOT_COMPLETE = "source_ir_not_complete"


class ProviderSourceIRRejected(RuntimeError):
    """Reject model work without exposing captured source content."""

    def __init__(
        self,
        code: ProviderSourceIRRejectionCode,
        *,
        operation: str,
        source_ir_status: SourceIRStatus,
    ) -> None:
        self.code = code
        self.operation = operation
        self.source_ir_status = source_ir_status
        super().__init__(
            f"{operation} requires COMPLETE SourceIR; "
            f"received {source_ir_status.value}."
        )


def require_complete_source_ir_for_provider(
    source_ir: SourceIRSnapshot,
    *,
    operation: str,
) -> None:
    """Permit provider-backed reasoning only over complete captured facts."""

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    normalized_operation = str(operation or "").strip()
    if not normalized_operation:
        raise ValueError("operation must be a non-blank string")
    if source_ir.status is SourceIRStatus.COMPLETE:
        return
    raise ProviderSourceIRRejected(
        ProviderSourceIRRejectionCode.SOURCE_IR_NOT_COMPLETE,
        operation=normalized_operation,
        source_ir_status=source_ir.status,
    )


__all__ = [
    "ProviderSourceIRRejected",
    "ProviderSourceIRRejectionCode",
    "require_complete_source_ir_for_provider",
]
