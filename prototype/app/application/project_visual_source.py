"""Expose existing visual observations to the ordinary Map/Items source model.

Projection preserves provenance and every existing native source node. It is
not OCR correction, semantic acceptance, classification, or automatic adoption
into a Document Map.
"""
from __future__ import annotations

from app.domain.source_visual_text import VisualTextSnapshot
from app.domain.extraction.source_ir import (
    MergedRegion, NativeLocator, PdfTextLineGeometry, SourceDiagnostic,
    SourceDiagnosticSeverity, SourceIRSnapshot, SourceNode, SourceNodeKind,
    TableCellIR, TableIR, VisualTextProjectionRef, stable_cell_id, stable_node_id,
    stable_table_id,
)


def project_visual_source(
    base: SourceIRSnapshot,
    snapshots: tuple[VisualTextSnapshot, ...],
    *,
    observation_ids: frozenset[str] | None = None,
) -> SourceIRSnapshot:
    """Append exact text/table projections; repeated identical input is a no-op."""
    by_snapshot = {s.visual_text_snapshot_id: s for s in base.visual_text_snapshots}
    available_ids = {o.observation_id for s in snapshots for o in s.observations}
    if observation_ids is not None and not observation_ids.issubset(available_ids):
        raise ValueError("unknown_visual_observation_selection")
    for snapshot in snapshots:
        if snapshot.source_sha256 != base.source_sha256:
            raise ValueError("visual_source_sha256_mismatch")
        previous = by_snapshot.get(snapshot.visual_text_snapshot_id)
        if previous is not None and previous != snapshot:
            raise ValueError("visual_snapshot_identity_collision")
        by_snapshot[snapshot.visual_text_snapshot_id] = snapshot
    existing = {(n.visual_text_ref.snapshot_id, n.visual_text_ref.observation_id)
                for n in base.nodes if n.visual_text_ref is not None}
    nodes = list(base.nodes)
    order = max((n.source_order for n in nodes), default=-1)
    for snapshot in sorted(snapshots, key=lambda s: (s.page_number, s.visual_text_snapshot_id)):
        for observation in snapshot.observations:
            if observation_ids is not None and observation.observation_id not in observation_ids:
                continue
            if observation.content_type not in {"TEXT", "TABLE", "HEADER", "FOOTER", "WATERMARK"}:
                continue
            key = (snapshot.visual_text_snapshot_id, observation.observation_id)
            if key in existing:
                continue
            table = observation.layout_table
            kind = SourceNodeKind.TABLE if table is not None else SourceNodeKind.PARAGRAPH
            locator = NativeLocator(format="pdf", page_number=snapshot.page_number,
                bbox=observation.bbox,
                **({"table_index": observation.observation_index} if table is not None
                   else {"block_index": observation.observation_index}))
            identity = dict(source_sha256=base.source_sha256, adapter_id="visual-text-projection",
                            adapter_version=snapshot.visual_text_snapshot_id, native_locator=locator)
            payload = None
            if table is not None:
                table_id = stable_table_id(**identity)
                cells = tuple(TableCellIR(
                    cell_id=stable_cell_id(table_id, c.row, c.column), row=c.row, column=c.column,
                    row_span=c.row_span, column_span=c.column_span, raw_text=c.raw_text,
                    native_locator=NativeLocator(format="pdf", page_number=snapshot.page_number,
                        table_index=observation.observation_index, row_index=c.row, column_index=c.column,
                        bbox=c.bbox or observation.bbox),
                ) for c in table.cells)
                merged = tuple(MergedRegion(row=c.row, column=c.column, row_span=c.row_span,
                                           column_span=c.column_span)
                               for c in table.cells if c.row_span > 1 or c.column_span > 1)
                headers = ((MergedRegion(row=0, column=0, row_span=table.header_row_count,
                                         column_span=table.column_count),) if table.header_row_count else ())
                payload = TableIR(table_id=table_id, row_count=table.row_count,
                                  column_count=table.column_count, cells=cells,
                                  merged_regions=merged, header_regions=headers)
            order += 1
            nodes.append(SourceNode(
                source_node_id=stable_node_id(**identity, kind=kind), kind=kind, source_order=order,
                raw_text=observation.raw_text, native_locator=locator, table_payload=payload,
                visual_text_ref=VisualTextProjectionRef(snapshot_id=snapshot.visual_text_snapshot_id,
                                                        observation_id=observation.observation_id),
                pdf_text_lines=(PdfTextLineGeometry(start_offset=0, end_offset=len(observation.raw_text),
                    bbox=observation.bbox),) if table is None and "\n" not in observation.raw_text else (),
                extraction_diagnostics=(SourceDiagnostic(code="visual_source_projection",
                    severity=SourceDiagnosticSeverity.INFO,
                    message="Source text/table is projected from a visual observation. Original pixels and provider evidence remain authoritative; projection does not certify transcription accuracy.",
                    native_locator=locator),),
            ))
            existing.add(key)
    value = base.model_dump(mode="json")
    value["nodes"] = [n.model_dump(mode="json") for n in nodes]
    value["visual_text_snapshots"] = [s.model_dump(mode="json") for s in sorted(by_snapshot.values(),
        key=lambda s: (s.page_number, s.provider_id, s.provider_version, s.visual_text_snapshot_id))]
    return SourceIRSnapshot.model_validate(value)


def verify_visual_capture_extension(previous: SourceIRSnapshot, updated: SourceIRSnapshot) -> None:
    """A new reading revision may append evidence, never rewrite previous facts."""
    before, after = previous.model_dump(mode="json"), updated.model_dump(mode="json")
    fields = {"nodes", "visual_text_snapshots"}
    if ({k:v for k,v in before.items() if k not in fields} !=
        {k:v for k,v in after.items() if k not in fields}):
        raise ValueError("visual_extension_changed_native_capture")
    count = len(previous.nodes)
    if updated.nodes[:count] != previous.nodes:
        raise ValueError("visual_extension_changed_existing_nodes")
    if any(n.visual_text_ref is None for n in updated.nodes[count:]):
        raise ValueError("visual_extension_appends_only_projected_observations")
    old = {s.visual_text_snapshot_id:s for s in previous.visual_text_snapshots}
    new = {s.visual_text_snapshot_id:s for s in updated.visual_text_snapshots}
    if any(new.get(k) != v for k,v in old.items()):
        raise ValueError("visual_extension_changed_existing_observations")
    # Revalidate linkage even if a caller passed an unchecked model_copy.
    SourceIRSnapshot.model_validate(updated.model_dump(mode="json"))
