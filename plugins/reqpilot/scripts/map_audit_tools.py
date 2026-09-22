"""Source-first coverage and tree-order leads; never a semantic completion claim."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re
from pathlib import Path


def uncovered_ranges(length, spans):
    cursor = 0
    for lo, hi in sorted(spans):
        if cursor < lo:
            yield cursor, lo
        cursor = max(cursor, hi)
    if cursor < length:
        yield cursor, length


def build_map_audit(snapshot, artifact, previews, decisions, *, page_count=None):
    from reqpilotx.application.agent_workflow.source_item_workbench_metadata import numbering_findings
    sources = snapshot['nodes']
    indexes = {node['source_node_id']: i for i, node in enumerate(sources)}
    by_source = {node['source_node_id']: node for node in sources}
    spans, rows = defaultdict(list), defaultdict(set)
    assets, evidence_only = set(), set()
    disposition_records = []
    table_locations = defaultdict(list)
    for table in previews.values():
        for fragment in table['fragments']:
            words = ''.join(''.join(cell.get('text', '').split()) for cell in fragment['cells'])
            table_locations[fragment.get('page')].append((fragment, words))
    for spec in decisions.get('nodes', []):
        for key in ('title_source_ids', 'content_source_ids'):
            for handle in spec.get(key, []):
                source = by_source[handle]
                spans[handle].append((0, len(source.get('raw_text', ''))))
                if source.get('asset_ref'):
                    assets.add(handle)
        for key in ('title_source_ranges', 'content_source_ranges'):
            for part in spec.get(key, []):
                spans[part['source_node_id']].append((part['start_offset'], part['end_offset']))
    for node in artifact['nodes']:
        for anchor in node.get('source_anchors', []):
            if anchor.get('asset_id'):
                assets.add(anchor['source_handle'])
        for anchor in node.get('evidence_anchors', []):
            evidence_only.add(anchor['source_handle'])
    for table in decisions.get('logical_tables', []):
        for part in table['fragments']:
            source = by_source[part['source_node_id']]
            lo, hi = part.get('row_range', [0, source['table_payload']['row_count']])
            rows[source['source_node_id']].update(range(lo, hi))
    for part in decisions.get('source_dispositions', []):
        source = by_source[part['source_node_id']]
        lo, hi = part.get('start_offset', 0), part.get('end_offset', len(source.get('raw_text', '')))
        disposition_records.append({'ref': indexes[source['source_node_id']], 'page': source['native_locator'].get('page_number'),
            'range': [lo, hi], 'disposition': part['disposition'], 'reason': part['reason']})
        if part['disposition'] != 'UNRESOLVED':
            spans[source['source_node_id']].append((lo, hi))
            if source.get('table_payload'):
                rows[source['source_node_id']].update(range(source['table_payload']['row_count']))
            if source.get('asset_ref'):
                assets.add(source['source_node_id'])

    gaps = []
    mapped_text = [(node['node_id'], content) for node in artifact['nodes'] for content in node.get('source_content', [])]
    def located(source, index, **extra):
        loc = source['native_locator']
        return {'ref': index, 'page': loc.get('page_number'), 'locator': loc,
            'reference_only': source['source_node_id'] in evidence_only, **extra}
    for index, source in enumerate(sources):
        handle, text = source['source_node_id'], source.get('raw_text', '')
        if source.get('table_payload'):
            payload = source['table_payload']
            selected = [(row, row + 1) for row in rows[handle]]
            for lo, hi in uncovered_ranges(payload['row_count'], selected):
                # A page frame may be a second capture of independently mapped
                # text. Keep the unassigned capture visible without demanding
                # a second logical table just to silence this audit.
                candidates = set()
                words = ''.join(''.join(cell.get('raw_text', '').split()) for cell in payload['cells'] if lo <= cell['row'] < hi)
                box = source['native_locator'].get('bbox')
                for node_id, content in mapped_text:
                    other = content.get('bbox')
                    text = ''.join(content['text'].split())
                    if content.get('page') == source['native_locator'].get('page_number') and box and other and text and len(text) >= 8:
                        if min(box[2], other[2]) > max(box[0], other[0]) and min(box[3], other[3]) > max(box[1], other[1]) and text in words:
                            candidates.add(node_id)
                gaps.append(located(source, index, kind='UNASSIGNED_TABLE_ROWS', row_range=[lo, hi],
                    possible_parallel_map_nodes=sorted(candidates)))
        elif source.get('asset_ref'):
            if handle not in assets:
                gaps.append(located(source, index, kind='UNASSIGNED_ASSET', asset_id=source['asset_ref']))
        else:
            # Native Office cell paragraphs are a second view of the same mapped
            # cells; they remain auditable evidence, not another business item.
            parent = by_source.get(source.get('parent_source_node_id'))
            loc = source['native_locator']
            alias = bool(parent and parent.get('table_payload') and loc.get('format') == 'docx'
                and loc.get('row_index') in rows[parent['source_node_id']]
                and any(cell['row'] == loc.get('row_index') and cell['column'] == loc.get('column_index')
                    and text in cell.get('raw_text', '') for cell in parent['table_payload']['cells']))
            if alias:
                continue
            for lo, hi in uncovered_ranges(len(text), spans[handle]):
                if not text[lo:hi].strip():
                    continue
                parallel = []
                box = loc.get('bbox')
                words = ''.join(text[lo:hi].split())
                for fragment, table_text in table_locations.get(loc.get('page_number'), []):
                    other = fragment.get('bbox')
                    overlap = box and other and min(box[2], other[2]) > max(box[0], other[0]) and min(box[3], other[3]) > max(box[1], other[1])
                    if overlap and words and words in table_text:
                        parallel.append(fragment['fragment_id'])
                gaps.append(located(source, index, kind='UNASSIGNED_TEXT_RANGE', range=[lo, hi], text=text[lo:hi],
                    possible_parallel_table_fragments=parallel))

    # Reuse measured source locations, including offsets within one block.
    # Compare the expanded tree, not candidate array order or chapter strings.
    nodes = {node['node_id']: node for node in artifact['nodes']}
    children = defaultdict(list)
    for node in nodes.values():
        children[node.get('parent_id')].append(node['node_id'])
    positions, active = {}, set()
    def node_position(key):
        if key in positions:
            return positions[key]
        if key in active:
            raise ValueError('Map audit found a parent cycle')
        active.add(key)
        points = []
        node = nodes[key]
        for content in node.get('source_content', []):
            if content.get('page') is not None and content.get('bbox'):
                refs = [indexes[h] for h in content.get('source_handles', []) if h in indexes]
                points.append((content['page'], content['bbox'][1], content['bbox'][0],
                    min(refs) if refs else 0, (content.get('source_text_range') or [0])[0]))
        for anchor in node.get('source_anchors', []):
            if anchor.get('page') is not None and anchor.get('bbox') and (anchor.get('asset_id') or anchor.get('logical_table_scope_id')):
                points.append((anchor['page'], anchor['bbox'][1], anchor['bbox'][0], indexes.get(anchor.get('source_handle'), 0), 0))
        for child in children.get(key, []):
            point = node_position(child)
            if point is not None:
                points.append(point)
        positions[key] = min(points) if points else None
        active.remove(key)
        return positions[key]
    expanded, backtracks = [], []
    numbered_groups = []
    marker_pattern = re.compile(r'(?m)^[ \t]*((?:\d+(?:\.\d+)*[、.)）．](?!\d)|[（(]\d+[)）]))(?=\S|[ \t])')
    for node in artifact['nodes']:
        markers = []
        for content in node.get('source_content', []):
            if content.get('is_title'):
                continue
            for match in marker_pattern.finditer(content['text']):
                offset = (content.get('source_text_range') or [0])[0]
                markers.append({'label':match.group(1), 'page':content.get('page'),
                    'refs':[indexes[h] for h in content.get('source_handles', []) if h in indexes],
                    'range':[offset+match.start(1),offset+match.end(1)]})
        if len(markers) >= 2:
            numbered_groups.append({'code':'NUMBERED_BODY_GROUP_NEEDS_BOUNDARY_REVIEW','node_id':node['node_id'],
                'markers':markers,'action':'Original numbered body defaults to addressable source leaves. Inspect whether these need separate Map nodes or are one evidenced complete unit such as a BOM/test; explain a valid grouping. Do not defer original boundaries to engineering Snn or create children mechanically from this pattern.'})
    def visit(parent):
        for key in sorted(children.get(parent, []), key=lambda key: nodes[key]['order']):
            node = nodes[key]
            position = node_position(key)
            entry = {'node_id': key, 'parent_id': parent, 'label': node.get('label', ''), 'title': node['title'],
                'position': list(position) if position else None}
            if expanded and position and expanded[-1]['position'] and position < tuple(expanded[-1]['position']):
                backtracks.append({'code': 'EXPANDED_TREE_POSITION_BACKTRACK', 'before': expanded[-1], 'after': entry})
            expanded.append(entry)
            visit(key)
    visit(None)
    if len(expanded) != len(nodes):
        raise ValueError('Map audit requires a complete acyclic parent tree')
    parent_changes, previous_numbered = [], None
    for entry in sorted((entry for entry in expanded if entry['position']), key=lambda entry: entry['position']):
        node = nodes[entry['node_id']]
        label = node.get('label', '').strip()
        match = re.fullmatch(r'(?:[（(])?(\d+)[、.)）．]?', label)
        # Restrict the lead to numbered body leaves, not named chapter headings.
        if not match or node.get('title', '').strip() != label:
            continue
        number = int(match.group(1))
        if previous_numbered:
            previous, prior_number = previous_numbered
            if number == prior_number + 1 and entry['position'][0] == previous['position'][0] + 1 and entry['parent_id'] != previous['parent_id']:
                parent_changes.append({'code':'NUMBERED_CONTINUATION_PARENT_CHANGE','before':previous,'after':entry,
                    'action':'Consecutive original body numbers cross a page and change parent. Inspect both endpoints and the governing prefix. A page break alone does not reset the list or its conditions; retain a justified change or repair the parent locally. This is a source-review lead, not automatic reparenting.'})
        previous_numbered = (entry, number)
    captured_pages = {node['native_locator'].get('page_number') for node in sources}
    all_pages = set(range(1, page_count + 1)) if page_count is not None else captured_pages - {None}
    inventory = []
    for page in sorted(all_pages):
        page_gaps = [gap for gap in gaps if gap['page'] == page]
        inventory.append({'page': page, 'capture_nodes': sum(node['native_locator'].get('page_number') == page for node in sources),
            'gap_counts': dict(Counter(gap['kind'] for gap in page_gaps)),
            'possible_parallel_text_ranges': sum(bool(gap.get('possible_parallel_table_fragments')) for gap in page_gaps),
            'visual_review': 'NOT_ESTABLISHED_BY_THIS_AUDIT'})
    return {'schema_version': 'reqpilot-map-audit/v1', 'source_sha256': artifact['source_sha256'],
        'source_findings': numbering_findings(artifact),
        'map_sha256': artifact['artifact_sha256'], 'scope': 'ENTIRE_CAPTURE_WITH_OPTIONAL_PAGE_DETAIL',
        'pages': inventory, 'gaps': gaps, 'dispositions': disposition_records,
        'expanded_tree': expanded, 'order_leads': backtracks, 'numbered_body_groups': numbered_groups,
        'numbered_parent_change_leads': parent_changes,
        'summary': {'gap_counts': dict(Counter(gap['kind'] for gap in gaps)), 'order_leads': len(backtracks),
            'unresolved_dispositions': sum(part['disposition'] == 'UNRESOLVED' for part in disposition_records),
            'numbered_body_groups':len(numbered_groups),'numbered_parent_changes':len(parent_changes)},
        'limits': 'Capture coverage is not original-document semantic completeness. Parallel text/table leads remain unassigned until source review resolves ownership. Evidence references do not create content ownership. Position backtracks need source review, especially for columns or repeated contextual structures. Dispositions are Agent decisions, not independently accepted facts. No automatic deletion, reparenting, renumbering or semantic completion.',
        'semantic_verification': 'NOT_RUN'}


def audit_map(args):
    if args.map_dir is None:
        raise ValueError('audit-map requires --map-dir')
    root = args.map_dir.resolve(strict=True)
    read = lambda name: json.loads((root / name).read_text())
    artifact, receipt = read('document-map.json'), read('map-compilation-receipt.json')
    snapshot, decisions = read('map-source-ir.json'), read('map-decisions.json')
    if not (artifact['source_sha256'] == snapshot['source_sha256'] == receipt['source_sha256'] == decisions['source_sha256']):
        raise ValueError('Map audit inputs do not bind the same source')
    from reqpilotx.application.agent_workflow.source_requirement_baseline import DocumentMapArtifactV1
    DocumentMapArtifactV1.model_validate(artifact)
    if artifact['artifact_sha256'] != receipt['document_map_artifact_sha256']:
        raise ValueError('Map audit receipt belongs to another Map version')
    inputs = receipt.get('source_inputs', {})
    for name, key in [('map-source-ir.json','source_ir_sha256'),('map-decisions.json','decisions_sha256')]:
        if inputs.get(key) != hashlib.sha256((root / name).read_bytes()).hexdigest():
            raise ValueError('Map audit source input changed or lacks a compiler receipt: ' + name)
    source = (root / inputs['source_path']).resolve(strict=True)
    if not source.is_relative_to(root) or hashlib.sha256(source.read_bytes()).hexdigest() != artifact['source_sha256']:
        raise ValueError('Map audit original does not match its source binding')
    page_count = None
    if source.suffix.lower() == '.pdf':
        import pymupdf
        with pymupdf.open(source) as pdf:
            page_count = len(pdf)
    # The ordinary compiler owns integrity validation. Audit never accepts or
    # republishes this directory; rerender actual current inputs, not a stale report.
    report = build_map_audit(snapshot, artifact, read('table-previews.json'), decisions, page_count=page_count)
    pages = set(args.page or [])
    result = {key: value for key, value in report.items() if key not in {'gaps', 'dispositions'}}
    if pages:
        result['detail_pages'] = sorted(pages)
        result['gaps'] = [gap for gap in report['gaps'] if gap['page'] in pages]
        result['dispositions'] = [part for part in report['dispositions'] if part['page'] in pages]
    else:
        result['detail_instruction'] = 'Use audit-map --map-dir <same-dir> --page <page> for exact unresolved text/row/asset ranges. The complete audit is also saved as map-audit.json on compilation.'
    return result
