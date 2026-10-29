"""Page-local reuse of immutable native PDF detection results.

PyMuPDF's table extractor reads module-global character data. A cached live
``Table`` cannot safely survive another detector call (including another page).
Freeze its rows and text while its own detector result is current. This cache
never survives the current source page and does not change parser decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any


@dataclass(frozen=True, slots=True)
class _CapturedRow:
    bbox: Any
    cells: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _CapturedTable:
    bbox: Any
    row_count: int
    col_count: int
    rows: tuple[_CapturedRow, ...]
    matrix: tuple[tuple[str | None, ...], ...]

    def extract(self) -> tuple[tuple[str | None, ...], ...]:
        return self.matrix


@dataclass(frozen=True, slots=True)
class _CapturedFinder:
    tables: tuple[Any, ...]


def _freeze_table(candidate: Any, *, max_cell_slots: int) -> Any:
    """Freeze supported results; retain malformed results for existing checks."""

    try:
        rows, columns = candidate.row_count, candidate.col_count
        if (
            isinstance(rows, bool)
            or isinstance(columns, bool)
            or not isinstance(rows, int)
            or not isinstance(columns, int)
            or rows < 1
            or columns < 1
            or rows * columns > max_cell_slots
        ):
            return candidate
        native_rows = candidate.rows
        if not isinstance(native_rows, (tuple, list)) or len(native_rows) != rows:
            return candidate
        matrix = candidate.extract()
        if (
            not isinstance(matrix, (tuple, list))
            or len(matrix) != rows
            or any(not isinstance(row, (tuple, list)) or len(row) != columns for row in matrix)
        ):
            return candidate
        return _CapturedTable(
            bbox=candidate.bbox,
            row_count=rows,
            col_count=columns,
            rows=tuple(_CapturedRow(row.bbox, tuple(row.cells)) for row in native_rows),
            matrix=tuple(tuple(row) for row in matrix),
        )
    except MemoryError:
        raise
    except Exception:
        # The adapter owns malformed-candidate diagnostics. Do not turn a
        # cache miss into a new fallback, new geometry or discarded source.
        return candidate


class PdfPageEvidenceCache:
    """Delegate page operations, reusing only the adapter's read-only calls."""

    def __init__(
        self,
        page: Any,
        *,
        max_table_candidates: int = 2_000,
        max_table_cell_slots: int = 500_000,
        evidence_observer: Callable[[str, dict[str, Any], Any], None] | None = None,
    ) -> None:
        self._page = page
        self._max_table_candidates = max_table_candidates
        self._max_table_cell_slots = max_table_cell_slots
        self._cache: dict[tuple[str, str], Any] = {}
        self._evidence_observer = evidence_observer

    def __getattr__(self, name: str) -> Any:
        # Unsupported methods stay unsupported; getattr(..., None) therefore
        # continues to drive the adapter's original missing-capability checks.
        method = getattr(self._page, name)
        if name == "get_text" and callable(method) and self._evidence_observer:
            def observed_text(*args: Any, **kwargs: Any) -> Any:
                result = method(*args, **kwargs)
                self._evidence_observer(name, {"args": args, **kwargs}, result)
                return result
            return observed_text
        if name not in {"find_tables", "get_drawings", "get_bboxlog"} or not callable(method):
            return method

        def cached(*args: Any, **kwargs: Any) -> Any:
            strategy = kwargs.get("strategy", "lines")
            supported = not args and (
                not kwargs
                or (name == "find_tables" and set(kwargs) == {"strategy"} and strategy == "lines_strict")
            )
            if not supported:
                return method(*args, **kwargs)
            key = (name, str(strategy) if name == "find_tables" else "default")
            if key not in self._cache:
                result = method(*args, **kwargs)
                if self._evidence_observer:
                    self._evidence_observer(name, kwargs, result)
                if name == "find_tables":
                    tables = getattr(result, "tables", None)
                    if isinstance(tables, (list, tuple)) and len(tables) <= self._max_table_candidates:
                        result = _CapturedFinder(tuple(
                            _freeze_table(table, max_cell_slots=self._max_table_cell_slots)
                            for table in tables
                        ))
                self._cache[key] = result
            return self._cache[key]

        return cached
