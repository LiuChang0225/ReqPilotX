from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from app.domain.extraction.table_evidence import (
    EvidenceReferenceKind,
    EvidenceReferenceV1,
    TableEvidenceBindingV1,
)
from app.domain.extraction.table_records import (
    TableRecordProjectionV1,
    TableRecordV1,
)

TABLE_EVIDENCE_PROJECTION_RUNTIME_VERSION = "1.1.0"


def build_table_evidence_bindings(
    *,
    table_record_projections: Iterable[TableRecordProjectionV1],
    tables: Iterable[dict[str, Any]],
) -> tuple[TableEvidenceBindingV1, ...]:
    """Freeze small, resolvable evidence pointers for whole tables and records.

    SourceIR and reconstructed grids remain authoritative and are not copied
    into this contract.  Each reference is content-addressed against the exact
    serialized table payload that Review and Excel already resolve.
    """

    tables_by_id = {
        str(table.get("id") or ""): dict(table)
        for table in tables
        if str(table.get("id") or "")
    }
    bindings: list[TableEvidenceBindingV1] = []
    for projection in sorted(
        table_record_projections,
        key=lambda item: (
            item.table_fact.table_parts[0].source_order,
            item.table_fact.logical_table_id,
        ),
    ):
        table_refs = _table_refs(projection, tables_by_id=tables_by_id)
        whole_notes = _whole_table_note_refs(
            projection,
            tables_by_id=tables_by_id,
        )
        whole_images = _whole_table_image_refs(
            projection,
            tables_by_id=tables_by_id,
        )
        bindings.append(
            TableEvidenceBindingV1.create(
                logical_table_id=projection.table_fact.logical_table_id,
                source_table_ids=projection.table_fact.table_ids,
                projection_id=projection.projection_id,
                projection_revision=projection.projection_revision,
                projection_sha256=projection.projection_sha256,
                table_refs=table_refs,
                note_refs=whole_notes,
                image_refs=whole_images,
            )
        )
        bindings.extend(
            _record_binding(
                projection=projection,
                record=record,
                table_refs=table_refs,
            )
            for record in projection.records
        )
    return tuple(bindings)


def _record_binding(
    *,
    projection: TableRecordProjectionV1,
    record: TableRecordV1,
    table_refs: tuple[EvidenceReferenceV1, ...],
) -> TableEvidenceBindingV1:
    return TableEvidenceBindingV1.create(
        logical_table_id=projection.table_fact.logical_table_id,
        source_table_ids=projection.table_fact.table_ids,
        projection_id=projection.projection_id,
        projection_revision=projection.projection_revision,
        projection_sha256=projection.projection_sha256,
        record_id=record.record_id,
        table_refs=table_refs,
        content_block_refs=(
            _reference(
                kind=EvidenceReferenceKind.CONTENT_BLOCK,
                reference_id=record.record_id,
                resolver_key=(
                    f"table-record:{projection.projection_id}:{record.record_id}"
                ),
                value=record.model_dump(mode="json"),
            ),
        ),
        header_refs=tuple(
            _reference(
                kind=EvidenceReferenceKind.HEADER,
                reference_id=(
                    f"{record.record_id}:header:{index}"
                ),
                resolver_key=(
                    f"table-record:{record.record_id}:header:{index}"
                ),
                value=header.model_dump(mode="json"),
            )
            for index, header in enumerate(record.header_bindings)
        ),
        shared_value_refs=tuple(
            _reference(
                kind=EvidenceReferenceKind.SHARED_VALUE,
                reference_id=(
                    f"{record.record_id}:shared:{value.table_id}:"
                    f"{value.cell_id}"
                ),
                resolver_key=(
                    f"table-record:{record.record_id}:shared:"
                    f"{value.table_id}:{value.cell_id}"
                ),
                value=value.model_dump(mode="json"),
                row_start=value.row,
                row_end=value.row + value.row_span - 1,
                column_start=value.column,
                column_end=value.column + value.column_span - 1,
            )
            for value in record.shared_merged_values
        ),
        note_refs=tuple(
            _reference(
                kind=EvidenceReferenceKind.NOTE,
                reference_id=f"{record.record_id}:note:{index}",
                resolver_key=f"table-record:{record.record_id}:note:{index}",
                value=note.model_dump(mode="json"),
            )
            for index, note in enumerate(record.notes)
        ),
        image_refs=_record_image_refs(record),
    )


def _record_image_refs(
    record: TableRecordV1,
) -> tuple[EvidenceReferenceV1, ...]:
    """Keep every distinct visual placement exactly once.

    Source adapters may classify the same visual as both a figure and an
    image, and the same asset may legitimately appear in several cells.  An
    asset-only identity therefore either duplicates a reference or collapses
    distinct evidence.  Bind the canonical placement payload instead: exact
    duplicates collapse, while different table/cell/source placements remain
    independently resolvable.
    """

    visuals_by_sha256: dict[str, dict[str, Any]] = {}
    for visual in (*record.figure_refs, *record.image_refs):
        payload = visual.model_dump(mode="json")
        visual_sha256 = _sha256(payload)
        previous = visuals_by_sha256.get(visual_sha256)
        if previous is not None and previous != payload:
            raise ValueError("table visual evidence SHA-256 collision")
        visuals_by_sha256[visual_sha256] = payload

    return tuple(
        _reference(
            kind=EvidenceReferenceKind.IMAGE,
            reference_id=f"{record.record_id}:image:{visual_sha256}",
            resolver_key=f"table-record:{record.record_id}:image:{visual_sha256}",
            value=payload,
        )
        for visual_sha256, payload in sorted(visuals_by_sha256.items())
    )


def _table_refs(
    projection: TableRecordProjectionV1,
    *,
    tables_by_id: dict[str, dict[str, Any]],
) -> tuple[EvidenceReferenceV1, ...]:
    refs: list[EvidenceReferenceV1] = []
    for table_id in projection.table_fact.table_ids:
        table = tables_by_id.get(table_id)
        if table is None:
            raise ValueError(
                "table evidence cannot resolve an immutable table fact"
            )
        refs.append(
            _reference(
                kind=EvidenceReferenceKind.TABLE,
                reference_id=table_id,
                resolver_key=f"table:{table_id}",
                value=table,
                page_number=_positive_int(
                    table.get("page_start") or table.get("page_number")
                ),
            )
        )
    return tuple(refs)


def _whole_table_note_refs(
    projection: TableRecordProjectionV1,
    *,
    tables_by_id: dict[str, dict[str, Any]],
) -> tuple[EvidenceReferenceV1, ...]:
    refs: list[EvidenceReferenceV1] = []
    for table_id in projection.table_fact.table_ids:
        for index, note in enumerate(
            _list(tables_by_id.get(table_id, {}).get("notes"))
        ):
            refs.append(
                _reference(
                    kind=EvidenceReferenceKind.NOTE,
                    reference_id=f"{table_id}:note:{index}",
                    resolver_key=f"table:{table_id}:note:{index}",
                    value=note,
                )
            )
    return tuple(refs)


def _whole_table_image_refs(
    projection: TableRecordProjectionV1,
    *,
    tables_by_id: dict[str, dict[str, Any]],
) -> tuple[EvidenceReferenceV1, ...]:
    refs: list[EvidenceReferenceV1] = []
    seen: set[str] = set()
    for table_id in projection.table_fact.table_ids:
        for cell in _dict_items(
            tables_by_id.get(table_id, {}).get("cells")
        ):
            for image in _dict_items(cell.get("images")):
                image_id = str(
                    image.get("id")
                    or image.get("asset_id")
                    or image.get("content_sha256")
                    or ""
                )
                if not image_id or image_id in seen:
                    continue
                seen.add(image_id)
                refs.append(
                    _reference(
                        kind=EvidenceReferenceKind.IMAGE,
                        reference_id=f"{table_id}:image:{image_id}",
                        resolver_key=f"table:{table_id}:image:{image_id}",
                        value=image,
                        page_number=_positive_int(
                            _dict(image.get("location")).get("page_number")
                        ),
                    )
                )
    return tuple(refs)


def _reference(
    *,
    kind: EvidenceReferenceKind,
    reference_id: str,
    resolver_key: str,
    value: object,
    page_number: int | None = None,
    row_start: int | None = None,
    row_end: int | None = None,
    column_start: int | None = None,
    column_end: int | None = None,
) -> EvidenceReferenceV1:
    return EvidenceReferenceV1(
        kind=kind,
        reference_id=reference_id,
        resolver_key=resolver_key,
        content_sha256=_sha256(value),
        page_number=page_number,
        row_start=row_start,
        row_end=row_end,
        column_start=column_start,
        column_end=column_end,
    )


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _list(value)
        if isinstance(item, dict)
    ]


__all__ = [
    "TABLE_EVIDENCE_PROJECTION_RUNTIME_VERSION",
    "build_table_evidence_bindings",
]
