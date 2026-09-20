"""Readable projection of a freshly compiled Map; warnings are not decisions."""
import re


def map_review(artifact, previews, receipt, snapshot, decisions, source_ir_path=None):
    source_indexes = {n.source_node_id: i for i, n in enumerate(snapshot.nodes)}
    by_id = {n.node_id: n for n in artifact.nodes}
    owner = {t['scope_id']: t['owner_node_id'] for t in decisions.get('logical_tables', [])}
    normalize = lambda text: re.sub(r'\s+', '', text)
    by_page = {}
    tables = []
    for scope, table in previews.items():
        fragments = []
        for f in table['fragments']:
            cells = [{k: c[k] for k in ('row', 'column', 'row_span', 'column_span', 'text')} for c in f['cells']]
            fragments.append({'page': f['page'], 'row_range': f['row_range'], 'cells': cells})
            by_page.setdefault(f['page'], []).append((scope, f, normalize('\n'.join(c['text'] for c in f['cells']))))
        tables.append({'scope_id': scope, 'owner_node_id': owner[scope], 'fragments': fragments,
            'effect':'CREATES_TABLE_EXTRACTION_SCOPE',
            'note':'The role label does not turn this into evidence-only storage. Use node evidence_refs for carrier-only inspection; preserve real business tables and their item relationships.'})
    nodes, warnings = [], []
    for node in artifact.nodes:
        def text_view(content):
            provenance = [getattr(snapshot.nodes[source_indexes[h]], 'pdf_text_trace', None) for h in content.source_handles if h in source_indexes]
            return {'text': content.text, 'page': content.page, 'bbox': content.bbox,
                    'refs': [source_indexes[h] for h in content.source_handles if h in source_indexes],
                    'source_text_range': content.source_text_range,
                    **({'drawing_text_observations':[p.model_dump(mode='json') for p in provenance if p]} if any(provenance) else {})}
        parents, pid = [], node.parent_id
        while pid:
            parent = by_id[pid]
            parents.append({'node_id': parent.node_id, 'label': parent.label, 'title': parent.title,
                            'body_context': 'Read the body of this node in the same review; parent text is stored once.'})
            pid = parent.parent_id
        nodes.append({'node_id': node.node_id, 'parent_id': node.parent_id, 'kind': node.kind,
            'label': node.label, 'title': node.title, 'pages': node.pages,
            'ancestor_context': list(reversed(parents)),
            'headings': [text_view(c) for c in node.source_content if c.is_title],
            'body': [text_view(c) for c in node.source_content if not c.is_title],
            'evidence_refs': [{'ref':source_indexes.get(a.source_handle),'page':a.page,'bbox':a.bbox,'row_range':a.row_range,
                'effect':'REFERENCE_ONLY_NO_EXTRACTION_SCOPE',
                **({'full_source_read':{'path':str(source_ir_path),'json_pointer':'/nodes/'+str(source_indexes[a.source_handle])}} if source_ir_path and a.source_handle in source_indexes else {})}
                for a in getattr(node,'evidence_anchors',())],
            'images': [{'ref': source_indexes.get(a.source_handle), 'page': a.page, 'bbox': a.bbox,
                        'asset_id': a.asset_id} for a in node.source_anchors if a.asset_id]})
        for content in node.source_content:
            if content.is_title or content.page is None or content.bbox is None:
                continue
            text = normalize(content.text)
            # Geometric and textual overlap is a review lead, never permission
            # to delete content; repeated values alone are not duplicate proof.
            if len(text) < 12:
                continue
            for scope, fragment, table_text in by_page.get(content.page, []):
                a, b = content.bbox, fragment['bbox']
                intersects = min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])
                if intersects and text in table_text:
                    warnings.append({'code': 'POSSIBLE_PARALLEL_CAPTURE', 'node_id': node.node_id,
                        'content_id': content.content_id, 'table_scope_id': scope, 'page': content.page,
                        'message': 'The same body text also occurs in a mapped table at the same location. Check whether this is one source in two capture channels or two intentional business representations. Do not create duplicate items just to retain the original table evidence.'})
    return {'schema_version': 'reqpilot-map-review/v1', 'source_sha256': artifact.source_sha256,
        'map_sha256': artifact.artifact_sha256, 'nodes': nodes, 'tables': tables, 'review_leads': warnings,
        'compiler_checks': 'Source identity, text hashes, selected ranges and structural references passed compilation; no model recheck of their raw hashes is needed.',
        'coverage': {'state': receipt['evidence_state'], 'unassigned_capture_nodes': len(receipt['uncovered_source_node_ids']),
            'partial_text_refs': [source_indexes[s] for s in receipt.get('partially_covered_text_source_ids', [])],
            'partial_table_refs': [source_indexes[s] for s in receipt['partially_covered_table_source_ids']]},
        'semantic_verification': 'NOT_RUN', 'human_acceptance': 'NOT_CLAIMED',
        'self_check': 'Establish the governing chain and end boundary from the original, then compare this saved view. ancestor_context projects the authored Map; it is not an independently recovered source outline. Empty review_leads is not a semantic pass. Check body/heading roles, tables/images and continued clauses; look earlier than the adjacent page when governing context remains unresolved. Full source and compiler receipts remain available; do not reread raw hashes/unassigned IDs as semantic review.'}


def review_response(review, output):
    import json
    result = {'review_read': {'path': str(output / 'map-review.json'), 'json_pointer': ''},
              'review_node_count': len(review['nodes']), 'review_lead_count': len(review['review_leads'])}
    if 'source_audit' in review:
        result['source_audit'] = review['source_audit']
    if len(json.dumps(review, ensure_ascii=False)) <= 24000:
        result['review'] = review
    else:
        result['review_inline_omitted'] = 'Complete review saved; use review_read or JSON pointers to nodes/tables. No source text was truncated in the saved review.'
    return result
