"""Relate original figures to the table cells that already own their content.

A figure inside a table requirement is evidence for that existing SRC. Geometry
establishes a source relationship, not applicability or a new requirement.
"""
from __future__ import annotations

from reqpilotx.application.agent_workflow.source_locations import source_location_ref, original_format

from reqpilotx.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1, SourceRequirementBaselineArtifactV1,
)


def _intersection(a, b):
    return [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]


def _area(rect):
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _union_area(rectangles):
    rectangles = [r for r in rectangles if _area(r) > 0]
    xs = sorted({x for r in rectangles for x in (r[0], r[2])})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((r[1], r[3]) for r in rectangles if r[0] <= left and r[2] >= right)
        end, height = None, 0.0
        for low, high in intervals:
            height += max(0.0, high - max(low, end if end is not None else low))
            end = max(high, end if end is not None else high)
        area += (right - left) * height
    return area


def table_source_figure_relations(document_map, baseline, table_previews):
    """Return exact source refs plus conservative, source-bound containment edges."""
    document_map = DocumentMapArtifactV1.model_validate(document_map)
    baseline = SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if (baseline.document_map_artifact_sha256 != document_map.artifact_sha256
            or baseline.source_sha256 != document_map.source_sha256):
        raise ValueError("Table figure relations require the same source and Map")
    previews = table_previews or {}
    if isinstance(previews, list):
        previews = {p['logical_table_scope_id']: p for p in previews}
    by_record, by_fragment, covered = {}, {}, set()
    anchors = [(node.node_id, anchor) for node in document_map.nodes
               for anchor in node.source_anchors if anchor.anchor_kind in {'IMAGE', 'FIGURE'}]
    for record in baseline.records:
        preview = previews.get(record.logical_table_scope_id)
        if not record.table_fragments or preview is None:
            continue
        if preview.get('source_sha256') != baseline.source_sha256:
            raise ValueError("Table figure preview belongs to a different source")
        fragments = {f['fragment_id']: f for f in preview['fragments']}
        for binding in record.table_fragments:
            fragment = fragments[binding.fragment_id]
            cells = [c for c in fragment['cells'] if c['cell_id'] in binding.cell_ids]
            for owner, anchor in anchors:
                if owner != record.node_id or anchor.page != binding.page:
                    continue
                floating = (preview.get('presentation') or {}).get('floating_images', [])
                if any((value.get('placement_id') or value.get('anchor_id')) == (anchor.placement_id or anchor.anchor_id)
                       for value in floating):
                    continue
                if original_format(anchor.native_locator) != 'pdf':
                    loc = anchor.native_locator
                    # Only DOCX's real cell containment is an ownership fact.
                    # A floating spreadsheet image positioned near cells is not
                    # itself evidence that those cells own the image.
                    if loc.format.value != 'docx' or loc.table_index is None or loc.row_index is None:
                        continue
                    owned = [cell for cell in cells if (native := cell.get('native_locator'))
                        and native.get('body_element_index') == loc.body_element_index
                        and native.get('table_index') == loc.table_index
                        and cell['row'] <= loc.row_index < cell['row'] + cell.get('row_span', 1)
                        and cell['column'] <= loc.column_index < cell['column'] + cell.get('column_span', 1)]
                    if len(owned) != 1:
                        continue
                    best = owned[0]
                else:
                    if list(anchor.source_bounds) != list(fragment['source_bounds']):
                        continue
                    if _area(anchor.bbox) <= 0:
                        continue
                    intersections = [(c, _intersection(anchor.bbox, c['bbox'])) for c in cells]
                    ratio = _union_area([r for _, r in intersections]) / _area(anchor.bbox)
                    if ratio < 1.0 - 1e-6:
                        continue
                    owned = [c for c, rect in intersections if _area(rect) > 0]
                    best = max(owned, key=lambda c: _area(_intersection(anchor.bbox, c['bbox'])))
                ref = {**anchor.model_dump(mode='json'), 'kind': 'image',
                       'source_sha256': baseline.source_sha256,
                       'page_number': anchor.page, 'source_bbox': list(anchor.bbox) if anchor.bbox is not None else None,
                       'location': source_location_ref(anchor),
                       'rendering': {'mode': 'SOURCE_PDF_CROP' if original_format(anchor.native_locator) == 'pdf' else 'SOURCE_OFFICE_PACKAGE', 'available': True}, 'caption': '原文图像',
                       'relation': 'IMAGE_WITHIN_OWNED_TABLE_CELLS',
                       'logical_table_scope_id': record.logical_table_scope_id,
                       'table_fragment_id': binding.fragment_id,
                       'source_cell_ids': [c['cell_id'] for c in owned],
                       'cell_id': best['cell_id'], 'row': best['row'], 'column': best['column']}
                by_record.setdefault(record.source_record_id, []).append(ref)
                target = by_fragment.setdefault(binding.fragment_id, [])
                if not any(old['anchor_id'] == anchor.anchor_id for old in target):
                    target.append(ref)
                covered.add(anchor.anchor_id)
    return {'records': by_record, 'fragments': by_fragment, 'table_owned_anchor_ids': sorted(covered)}
