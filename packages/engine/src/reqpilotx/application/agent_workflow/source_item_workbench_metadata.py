"""Lightweight common numbering, scope counts and source-review leads.

Findings concern the saved scope, never prove completeness of the original, and
never rewrite source content or allocate SRC/REQ identities.
"""
from collections import defaultdict
from copy import deepcopy
import re

from reqpilotx.application.agent_workflow import source_requirement_baseline as core
from reqpilotx.application.agent_workflow.source_item_ledger import project_source_item_ledger
from reqpilotx.application.agent_workflow.source_item_delivery import project_source_item_number, _derived_source_number


def _value(model):
    return model.model_dump(mode='json') if hasattr(model, 'model_dump') else model


def numbering_findings(document_map):
    mapping = _value(document_map)
    groups, findings, checked = defaultdict(list), [], []
    # A Map body's peer numbering is distinct from TOC entries, quantities and
    # table/figure numbering. Other numbering families stay outside this scan.
    eligible = {'CLAUSE', 'SECTION', 'CHAPTER', 'LIST_ITEM', 'NUMBERED_CLAUSE'}
    nodes = {n['node_id']: n for n in mapping['nodes']}
    for node in mapping['nodes']:
        ancestor, seen, front_matter = node, set(), False
        while ancestor and ancestor['node_id'] not in seen:
            seen.add(ancestor['node_id'])
            if ancestor['kind'] in {'FRONT_MATTER', 'TABLE_OF_CONTENTS'}:
                front_matter = True
            ancestor = nodes.get(ancestor.get('parent_id'))
        label = str(node.get('label') or '').strip()
        if front_matter or node['kind'] not in eligible or not re.fullmatch(r'\d+(?:\.\d+)*[.)、）]?', label):
            continue
        sequence = tuple(int(n) for n in re.findall(r'\d+', label))
        groups[(node.get('parent_id'), sequence[:-1])].append((node, sequence[-1]))
        checked.append(node['node_id'])
    for (parent, prefix), entries in groups.items():
        entries.sort(key=lambda entry: entry[0]['order'])
        for (before, lo), (after, hi) in zip(entries, entries[1:]):
            if hi == lo + 1:
                continue
            kind = 'NUMBER_SEQUENCE_GAP' if hi > lo + 1 else 'NUMBER_SEQUENCE_DUPLICATE' if hi == lo else 'NUMBER_SEQUENCE_REVERSED'
            code = {'NUMBER_SEQUENCE_GAP': '编号间断', 'NUMBER_SEQUENCE_DUPLICATE': '编号重复', 'NUMBER_SEQUENCE_REVERSED': '编号回退'}[kind]
            expected = ('.'.join(map(str, (*prefix, lo + 1))), '.'.join(map(str, (*prefix, hi - 1)))) if hi > lo + 1 else None
            def endpoint(node):
                anchors = node.get('source_anchors') or []
                return {'node_id': node['node_id'], 'source_number': node['label'], 'pages': node['pages'],
                        'source_refs': deepcopy(anchors or node.get('source_content', []))}
            evidence = {'before': endpoint(before), 'after': endpoint(after)}
            identity = {'source_sha256': mapping['source_sha256'], 'kind': kind,
                        'node_ids': [before['node_id'], after['node_id']], 'labels': [before['label'], after['label']]}
            findings.append({'finding_id': 'finding-' + core.canonical_json_sha256(identity)[:32], 'kind': kind, 'code': kind,
                'source_sha256': mapping['source_sha256'], 'document_map_sha256': mapping['artifact_sha256'],
                'node_id': after['node_id'], 'parent_node_id': parent, 'node_ids': identity['node_ids'], 'source_item_ids': [],
                'pages': sorted(set(before['pages']) | set(after['pages'])), 'evidence': evidence,
                'observed': f"{before['label']} → {after['label']}", 'expected_number_range': list(expected) if expected else None,
                'message': f"当前成果在 {before['label']} → {after['label']} 出现{code}，待回查原件。已保留原编号；不据此补号或新增需求。",
                'severity': 'NOTICE', 'status': 'PENDING_SOURCE_REVIEW', 'source_review_state': 'NOT_REVIEWED',
                'blocks_progress': False, 'suggested_action': '核对两端原页与中间范围，区分原件编号现象、列表重置及提取遗漏。'})
    return {'schema_version': 'source-findings/v1', 'source_sha256': mapping['source_sha256'],
        'document_map_sha256': mapping['artifact_sha256'], 'scan_state': 'COMPLETED' if checked else 'NOT_APPLICABLE',
        'checked_scope': {'kind': 'SAVED_MAP_NUMERIC_BODY_PEERS', 'node_ids': checked},
        'complete_or_unknown': 'UNKNOWN', 'original_document_complete': None,
        'limits': ['Only saved numeric body peers are scanned; table/figure/letter/OCR numbering and uncaptured source require separate review.',
                   'A completed scoped scan is not semantic or original-document completeness.'], 'findings': findings}


def project_workbench_metadata(document_map, baseline, *, annotations=None, decomposition=None,
                               table_previews=None, decomposition_known=True, ledger=None):
    mapping = core.DocumentMapArtifactV1.model_validate(document_map)
    baseline = core.SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if baseline.document_map_artifact_sha256 != mapping.artifact_sha256 or baseline.source_sha256 != mapping.source_sha256:
        raise ValueError('Workbench metadata requires the same source and Map')
    if decomposition and decomposition.get('baseline_sha256') != baseline.artifact_sha256:
        raise ValueError('Workbench decomposition belongs to another baseline')
    if annotations:
        from reqpilotx.application.agent_workflow.source_item_visual import rebind_source_item_image_annotations
        annotations = rebind_source_item_image_annotations(mapping, baseline, annotations)
    anns = (annotations or {}).get('records', {})
    ledger = ledger or project_source_item_ledger(document_map=mapping, artifact=baseline)
    source_rows = {r['source_item_id']: r for r in ledger['rows'] if r['source_item_id']}
    nodes = {n.node_id: n for n in mapping.nodes}
    numbers, derived_numbers, own, descendants = {}, {}, defaultdict(set), defaultdict(set)
    children = (decomposition or {}).get('records', {})
    for record in baseline.records:
        row = source_rows[record.source_record_id]
        number, entries = project_source_item_number(mapping, baseline, nodes[record.node_id], record,
            row['is_one_to_one_item'], anns.get(record.source_record_id), table_previews or {},
            '\n'.join(b.text for b in record.text_bindings))
        role = 'SOURCE_NUMBER_COLLECTION' if len(entries) > 1 else 'EXACT_SOURCE_NUMBER' if number else 'UNNUMBERED'
        numbers[record.source_record_id] = {'source_number': number, 'source_numbers': entries, 'source_number_role': role}
        for child in children.get(record.source_record_id, []):
            value, projection = _derived_source_number(child, entries, number)
            derived_numbers[child['singular_requirement_id']] = {'source_number': value, 'source_number_projection': projection}
        own[record.node_id].add(record.source_record_id)
        cursor, visited = nodes[record.node_id], set()
        while cursor and cursor.node_id not in visited:
            visited.add(cursor.node_id); descendants[cursor.node_id].add(record.source_record_id)
            cursor = nodes.get(cursor.parent_id)
    records = {r.source_record_id: r for r in baseline.records}
    summaries = {}
    for node in mapping.nodes:
        ids = descendants[node.node_id]
        required = {sid for sid in ids if records[sid].requirementness.value == 'REQUIREMENT'}
        summaries[node.node_id] = {'direct_source_item_count': len(own[node.node_id]), 'subtree_source_item_count': len(ids),
            'subtree_requirement_count': len(required), 'subtree_non_requirement_count': len(ids)-len(required),
            'derived_count': sum(len(children.get(sid, [])) for sid in ids) if decomposition_known else None,
            'analysis_unit_count': sum(len(children.get(sid) or [sid]) for sid in required) if decomposition_known else None,
            'coverage': {'state': 'SAVED_SCOPE_ONLY', 'original_complete': None, 'filter_applied': False},
            'basis_sha256': baseline.artifact_sha256, 'decomposition_sha256': (decomposition or {}).get('artifact_sha256')}
    findings = numbering_findings(mapping)
    for finding in findings['findings']:
        finding['source_item_ids'] = [r.source_record_id for r in baseline.records if r.node_id in finding['node_ids']]
        finding['baseline_sha256'] = baseline.artifact_sha256
    return {'numbers': numbers, 'derived_numbers': derived_numbers, 'scope_summaries': summaries, 'source_findings': findings}
