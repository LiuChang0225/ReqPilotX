"""Provider-neutral port for source-bound AI reading actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


MAX_READING_CHAT_CONTEXT_CHUNKS = 6_000
MAX_READING_CHAT_CONTEXT_CHARACTERS = 2_000_000
MAX_READING_CHAT_OUTPUT_CITATIONS = 75
FULL_EFFECTIVE_TEXT_DISCLOSURE = (
    "基于全部可读文字层（含视觉识别），未解析无文字图像语义"
)


@dataclass(frozen=True, slots=True)
class ReadingProviderResult:
    output_text: str
    provider_id: str
    model_name: str
    prompt_version: str

    def __post_init__(self) -> None:
        for name in ("output_text", "provider_id", "model_name", "prompt_version"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be blank")


@dataclass(frozen=True, slots=True)
class ReadingChatContextChunk:
    citation_id: str
    source_node_id: str | None
    page_number: int | None
    start_offset: int
    end_offset: int
    text: str
    locator_kind: str = "SOURCE_TEXT"
    cell_id: str | None = None
    table_row: int | None = None
    table_column: int | None = None
    table_row_span: int | None = None
    table_column_span: int | None = None
    visual_snapshot_id: str | None = None
    observation_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    page_bbox: tuple[float, float, float, float] | None = None
    page_image_sha256: str | None = None
    visual_provider_id: str | None = None
    visual_provider_version: str | None = None

    def __post_init__(self) -> None:
        for name in ("citation_id", "text"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be blank")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("page_number must be positive")
        if self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise ValueError("chat context chunk offsets are invalid")
        if len(self.text) != self.end_offset - self.start_offset:
            raise ValueError("chat context text must exactly match its source range")
        if self.locator_kind not in {"SOURCE_TEXT", "TABLE_CELL", "VISUAL_TEXT"}:
            raise ValueError("chat context locator kind is invalid")
        visual_fields = (
            self.visual_snapshot_id,
            self.observation_id,
            self.bbox,
            self.page_bbox,
            self.page_image_sha256,
            self.visual_provider_id,
            self.visual_provider_version,
        )
        if self.locator_kind == "SOURCE_TEXT":
            if not str(self.source_node_id or "").strip() or self.cell_id is not None:
                raise ValueError("source-text context requires only a source node")
            if any(value is not None for value in visual_fields):
                raise ValueError("source-text context cannot claim visual provenance")
        elif self.locator_kind == "TABLE_CELL":
            if not str(self.source_node_id or "").strip() or not str(
                self.cell_id or ""
            ).strip():
                raise ValueError("table-cell context requires node and cell_id")
            if any(value is not None for value in visual_fields):
                raise ValueError("table-cell context cannot claim visual provenance")
        else:
            if self.source_node_id is not None or self.cell_id is not None:
                raise ValueError("visual context cannot claim native identities")
            if self.page_number is None or any(value is None for value in visual_fields):
                raise ValueError("visual context requires complete page provenance")
            bx0, by0, bx1, by1 = self.bbox  # type: ignore[misc]
            px0, py0, px1, py1 = self.page_bbox  # type: ignore[misc]
            if (
                bx1 <= bx0
                or by1 <= by0
                or px1 <= px0
                or py1 <= py0
                or bx0 < px0
                or by0 < py0
                or bx1 > px1
                or by1 > py1
            ):
                raise ValueError("visual context bbox must be contained by page_bbox")
        table_coordinates = (
            self.table_row,
            self.table_column,
            self.table_row_span,
            self.table_column_span,
        )
        if self.locator_kind in {"SOURCE_TEXT", "VISUAL_TEXT"} and any(
            item is not None for item in table_coordinates
        ):
            raise ValueError("non-cell context cannot contain table coordinates")
        if self.locator_kind == "TABLE_CELL":
            if self.table_row is None or self.table_column is None:
                raise ValueError("table-cell context requires row and column")
            if self.table_row < 0 or self.table_column < 0:
                raise ValueError("table-cell row and column must be non-negative")
            if self.table_row_span is None or self.table_column_span is None:
                raise ValueError("table-cell context requires row and column spans")
            if self.table_row_span < 1 or self.table_column_span < 1:
                raise ValueError("table-cell row and column spans must be positive")


@dataclass(frozen=True, slots=True)
class ReadingChatHistoryMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("chat history role is invalid")
        if not str(self.content or "").strip():
            raise ValueError("chat history content must not be blank")


@dataclass(frozen=True, slots=True)
class ReadingChatProviderResult:
    output_text: str
    citation_ids: tuple[str, ...]
    provider_id: str
    model_name: str
    prompt_version: str

    def __post_init__(self) -> None:
        for name in ("output_text", "provider_id", "model_name", "prompt_version"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be blank")
        if len(self.citation_ids) != len(set(self.citation_ids)):
            raise ValueError("provider citation IDs must be unique")
        if any(not str(item or "").strip() for item in self.citation_ids):
            raise ValueError("provider citation IDs must not be blank")


@dataclass(frozen=True, slots=True)
class ReadingProviderReadiness:
    ready: bool
    provider_id: str | None
    model_name: str | None
    configuration_source: str
    reason_code: str | None


@runtime_checkable
class ReadingProviderPort(Protocol):
    """Generate one reading-only response from an exact current-source slice.

    Implementations receive no project knowledge, Requirement Set, retrieval
    handle, or cross-document context.  They must not persist their result.
    """

    def generate(
        self,
        *,
        action: str,
        source_text: str,
        target_language: str | None,
    ) -> ReadingProviderResult:
        """Return one model result or raise an explicit provider error."""

    def generate_chat(
        self,
        *,
        question: str,
        history: tuple[ReadingChatHistoryMessage, ...],
        context_chunks: tuple[ReadingChatContextChunk, ...],
        coverage_mode: str,
    ) -> ReadingChatProviderResult:
        """Answer one free-form turn using only this reading conversation/document."""


class ReadingProviderUnavailable(RuntimeError):
    """No explicitly selected and runnable reading model is available."""

    def __init__(self, message: str, *, reason_code: str = "reading_ai_not_configured"):
        super().__init__(message)
        self.reason_code = reason_code


class ReadingProviderFailure(RuntimeError):
    """A configured reading model failed or violated its output contract."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "reading_provider_failed",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


__all__ = [
    "FULL_EFFECTIVE_TEXT_DISCLOSURE",
    "ReadingProviderPort",
    "ReadingProviderResult",
    "ReadingChatContextChunk",
    "ReadingChatHistoryMessage",
    "ReadingChatProviderResult",
    "ReadingProviderReadiness",
    "ReadingProviderFailure",
    "ReadingProviderUnavailable",
    "MAX_READING_CHAT_CONTEXT_CHUNKS",
    "MAX_READING_CHAT_CONTEXT_CHARACTERS",
    "MAX_READING_CHAT_OUTPUT_CITATIONS",
]
