"""Complete selected source text with cheap, reusable authoring references.

This view does not classify nodes, infer headings, discard duplicates or choose
business boundaries. The full original SourceIR remains directly addressable.
"""
from pathlib import Path


def authoring_view(snapshot, args):
    pages = set(args.page or [])
    ids = set(args.source_id or [])
    indexes = set(getattr(args, 'capture_index', None) or [])
    query = getattr(args, 'query', None)
    offset = getattr(args, 'offset', 0)
    limit = getattr(args, 'limit', 40)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Authoring view needs offset >= 0 and 1 <= limit <= 100')
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise ValueError('query must be a nonempty source substring')
    if any(type(i) is not int or not 0 <= i < len(snapshot.nodes) for i in indexes):
        raise ValueError('Unknown capture_index; use the zero-based index returned by this view')
    known_ids = {n.source_node_id for n in snapshot.nodes}
    if ids - known_ids:
        raise ValueError('Unknown source_id: ' + ', '.join(sorted(ids - known_ids)))
    known_pages = {n.native_locator.page_number for n in snapshot.nodes}
    if pages - known_pages:
        raise ValueError('Selected page has no captured source nodes; inspect the original page for missing evidence')
    def matches(node):
        if query is None: return True
        texts = [node.raw_text]
        if node.table_payload:
            texts += [cell.raw_text for cell in node.table_payload.cells]
        return any(query.casefold() in text.casefold() for text in texts)
    selected = [(i, n) for i, n in enumerate(snapshot.nodes)
                if (not pages or n.native_locator.page_number in pages)
                and (not ids or n.source_node_id in ids) and (not indexes or i in indexes) and matches(n)]
    rows = []
    ir_path = str(Path(args.source_ir).resolve())
    for i, node in selected[offset:offset + limit]:
        row = {'ref': i, 'capture_index': i, 'source_node_id': node.source_node_id,
            'captured_kind': node.kind.value, 'page': node.native_locator.page_number,
            'bbox': node.native_locator.bbox, 'raw_text': node.raw_text,
            'full_source_read': {'path': ir_path, 'json_pointer': f'/nodes/{i}'}}
        if node.asset_ref:
            row['asset_ref'] = node.asset_ref
        if node.pdf_text_trace:
            row['drawing_text_observation'] = node.pdf_text_trace.model_dump(mode='json')
        if node.visual_text_ref:
            row['visual_text_observation'] = node.visual_text_ref.model_dump(mode='json')
            row['transcription_status'] = 'SOURCE_IMAGE_REVIEW_REQUIRED'
        if node.table_payload:
            table = node.table_payload
            row['table'] = {'row_count': table.row_count, 'column_count': table.column_count,
                'cells': [{k: getattr(c, k) for k in ('row', 'column', 'row_span', 'column_span', 'raw_text')}
                          for c in table.cells], 'notes': list(table.notes),
                'map_fragment': {'source_node_id': i, 'row_range': [0, table.row_count]}}
        rows.append(row)
    next_offset = offset + len(rows) if offset + len(rows) < len(selected) else None
    parameters = {'source_ir': ir_path, 'view': 'authoring', 'limit': limit}
    if pages: parameters['page'] = sorted(pages)
    if ids: parameters['source_id'] = sorted(ids)
    if indexes: parameters['capture_index'] = sorted(indexes)
    if query is not None: parameters['query'] = query
    return {'schema_version': 'reqpilot-source-authoring/v1', 'source_sha256': snapshot.source_sha256,
        'source_ir_id': snapshot.source_ir_id, 'source_ir': ir_path, 'capture_status': snapshot.status.value,
        'mode': 'AUTHORING_EVIDENCE', 'semantic_authority': 'NONE',
        'total': len(selected), 'offset': offset, 'count': len(rows), 'nodes': rows,
        'next_read': {'operation': 'inspect-source', 'arguments': {**parameters, 'offset': next_offset}} if next_offset is not None else None,
        'diagnostics': [d.model_dump(mode='json') for d in snapshot.diagnostics
                        if not pages or d.native_locator is None or d.native_locator.page_number in pages],
        'authoring': 'ref is a zero-based index in this pinned SourceIR, usable directly in reqpilot-map-edit/v1. raw_text is complete; exact unique quotes/start/end_before bind portions. Table cells use row/column/raw_text, not row_index/column_index. Captured text and tables may overlap; determine actual ownership from the source. A map_fragment is a whole physical-table reference, not a recommendation to keep the table whole.',
        'next': 'Once this evidence and necessary original/context checks resolve a meaningful scope, save its Map edit and compile-map. Keep unresolved and out-of-scope content explicit; expand reading when a concrete source question remains, rather than reload unrelated stage guides.'}
