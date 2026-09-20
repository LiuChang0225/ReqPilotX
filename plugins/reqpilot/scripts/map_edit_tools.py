"""Bind small Agent edits to existing Map v2 decisions; never infer ownership."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from app.application.agent_workflow import source_requirement_baseline as core
from app.domain.extraction.source_ir import SourceIRSnapshot


NODE_FIELDS = {'node_id', 'parent_id', 'kind', 'title', 'label', 'title_source_ids',
    'content_source_ids', 'title_source_ranges', 'content_source_ranges', 'title_reference_source_ids', 'evidence_refs'}
TABLE_FIELDS = {'scope_id', 'owner_node_id', 'title', 'role', 'fragments', 'agent_display_label'}


def _fields(value, allowed, context):
    if not isinstance(value, dict):
        raise ValueError(f'{context}: expected an object')
    extra = set(value) - allowed
    if extra:
        raise ValueError(f'{context}: unknown fields {sorted(extra)}')


def bind_map_edit(snapshot, request, source_path, base_dir=None, *, source_display_name=None):
    """Return ordinary v2 decisions and a mechanical binding receipt.

    capture_index is the zero-based SourceIR nodes index already exposed by
    native navigation. Exact quotes/markers must resolve uniquely within it.
    """
    _fields(request, {'schema_version', 'nodes', 'logical_tables', 'source_dispositions',
        'remove_node_ids', 'remove_table_ids'}, 'Map edit')
    if request.get('schema_version') != 'reqpilot-map-edit/v1':
        raise ValueError('Expected reqpilot-map-edit/v1')
    decisions = {'schema_version': 'reqpilot-map-decisions/v2', 'source_sha256': snapshot.source_sha256,
        'nodes': [], 'logical_tables': [], 'source_dispositions': []}
    base_sha = None
    if base_dir is not None:
        base = Path(base_dir).resolve(strict=True)
        receipt = json.loads((base / 'map-compilation-receipt.json').read_text())
        raw = (base / 'map-decisions.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt['source_inputs']['decisions_sha256']:
            raise ValueError('Base Map decisions changed after compilation')
        previous = SourceIRSnapshot.model_validate_json((base / 'map-source-ir.json').read_text())
        if previous.model_dump(mode='json') != snapshot.model_dump(mode='json'):
            from pdf_text_tools import verify_capture_extension
            verify_capture_extension(previous, snapshot, source_path)
        artifact = core.DocumentMapArtifactV1.model_validate_json((base / 'document-map.json').read_text())
        if artifact.artifact_sha256 != receipt['document_map_artifact_sha256'] or artifact.source_sha256 != snapshot.source_sha256:
            raise ValueError('Base Map receipt/source does not match')
        decisions = json.loads(raw)
        if decisions.get('schema_version') != 'reqpilot-map-decisions/v2' or decisions.get('source_sha256') != snapshot.source_sha256:
            raise ValueError('Map edits require a same-source v2 base; historical v1 stays unchanged')
        base_sha = artifact.artifact_sha256
    source_ids = {n.source_node_id: n for n in snapshot.nodes}
    bindings = []

    def source(value, context):
        if type(value) is int and 0 <= value < len(snapshot.nodes):
            return snapshot.nodes[value]
        if isinstance(value, str) and value in source_ids:
            return source_ids[value]
        raise ValueError(f'{context}: unknown source reference {value!r}; use capture_index or complete source ID')

    def occurrence(raw, marker, context):
        if not isinstance(marker, str) or not marker:
            raise ValueError(f'{context}: marker must be nonempty exact source text')
        first = raw.find(marker)
        if first < 0 or raw.find(marker, first + 1) >= 0:
            raise ValueError(f'{context}: source marker must match exactly once; choose a longer unique quote')
        return first

    def select(value, context):
        if isinstance(value, dict):
            _fields(value, {'source', 'quote', 'start', 'end_before'}, context)
            if set(value) == {'source'}:
                return select(value['source'], context)
            node = source(value.get('source'), context)
            if node.table_payload is not None or node.asset_ref:
                raise ValueError(f'{context}: text selectors require captured text; use whole image/table references')
            raw = node.raw_text
            if 'quote' in value:
                if set(value) != {'source', 'quote'}:
                    raise ValueError(f'{context}: use quote or start/end_before, not both')
                lo = occurrence(raw, value['quote'], context); hi = lo + len(value['quote'])
            else:
                if not (set(value) & {'start', 'end_before'}):
                    raise ValueError(f'{context}: select whole source with its index, or supply an exact text selector')
                lo = occurrence(raw, value['start'], context) if 'start' in value else 0
                hi = occurrence(raw, value['end_before'], context) if 'end_before' in value else len(raw)
                if hi <= lo:
                    raise ValueError(f'{context}: end_before must follow start')
            span = {'source_node_id': node.source_node_id, 'start_offset': lo, 'end_offset': hi}
            bindings.append({'field': context, **span})
            return node, span, raw[lo:hi]
        node = source(value, context)
        bindings.append({'field': context, 'source_node_id': node.source_node_id})
        return node, None, node.raw_text

    def bind_content(values, prefix, context):
        if not isinstance(values, list):
            raise ValueError(f'{context}: expected a list')
        ids, ranges = [], []
        for i, value in enumerate(values):
            node, span, _ = select(value, f'{context}[{i}]')
            if span: ranges.append(span)
            else: ids.append(node.source_node_id)
        return {prefix + '_source_ids': ids, prefix + '_source_ranges': ranges}

    def update_node(patch, old):
        key = patch['node_id']; context = f'nodes/{key}'
        _fields(patch, NODE_FIELDS | {'heading', 'content', 'numbered_body', 'unset'}, context)
        out = deepcopy(old)
        unset = patch.get('unset', [])
        if not isinstance(unset, list) or any(k not in NODE_FIELDS - {'node_id', 'parent_id', 'kind'} for k in unset):
            raise ValueError(f'{context}: unset may remove optional Map fields only')
        for field in unset: out.pop(field, None)
        for field, value in patch.items():
            if field in NODE_FIELDS: out[field] = deepcopy(value)
        if 'evidence_refs' in patch:
            if not isinstance(patch['evidence_refs'], list):
                raise ValueError(f'{context}/evidence_refs: expected a list')
            out['evidence_refs'] = []
            for entry in patch['evidence_refs']:
                _fields(entry, {'source', 'row_range'}, context + '/evidence_refs')
                out['evidence_refs'].append({'source_node_id': source(entry.get('source'), context + '/evidence_refs').source_node_id,
                    **({'row_range': entry['row_range']} if 'row_range' in entry else {})})
        for field in ('title_source_ids', 'content_source_ids', 'title_reference_source_ids'):
            if field in patch:
                if not isinstance(patch[field], list): raise ValueError(f'{context}/{field}: expected a list')
                out[field] = [source(v, context + '/' + field).source_node_id for v in patch[field]]
        for field in ('title_source_ranges', 'content_source_ranges'):
            if field in patch:
                for entry in out[field]:
                    _fields(entry, {'source_node_id', 'start_offset', 'end_offset'}, context + '/' + field)
                    entry['source_node_id'] = source(entry.get('source_node_id'), context + '/' + field).source_node_id
        if 'heading' in patch:
            if set(patch) & {'title', 'label', 'title_source_ids', 'title_source_ranges', 'title_reference_source_ids'}:
                raise ValueError(f'{context}: heading replaces title, label and title evidence together')
            heading = patch['heading']
            _fields(heading, {'source', 'label'}, context + '/heading')
            node, span, raw = select(heading.get('source'), context + '/heading')
            if node.table_payload is not None or node.asset_ref or not raw.strip():
                raise ValueError(f'{context}: heading must select original text')
            label = heading.get('label', '')
            if not isinstance(label, str) or label and not raw.startswith(label):
                raise ValueError(f'{context}: label must be the exact heading prefix; no number is inferred')
            title = raw[len(label):].strip() if label else raw.strip()
            out.update(title=title or label, label=label, title_source_ids=[] if span else [node.source_node_id],
                title_source_ranges=[span] if span else [])
            out.pop('title_reference_source_ids', None)
        if 'content' in patch:
            if set(patch) & {'content_source_ids', 'content_source_ranges'}:
                raise ValueError(f'{context}: use content or explicit content sources, not both')
            out.update(bind_content(patch['content'], 'content', context + '/content'))
        if 'numbered_body' in patch:
            conflicts = {'heading', 'content', 'title', 'label', 'title_source_ids', 'title_source_ranges',
                         'title_reference_source_ids', 'content_source_ids', 'content_source_ranges'}
            if set(patch) & conflicts:
                raise ValueError(f'{context}: numbered_body binds the label and complete body together; do not also supply heading/content fields')
            body = patch['numbered_body']
            _fields(body, {'source', 'label', 'continuations'}, context + '/numbered_body')
            node, span, raw = select(body.get('source'), context + '/numbered_body')
            label = body.get('label')
            if node.table_payload is not None or node.asset_ref or not isinstance(label, str) or not label or not raw.startswith(label):
                raise ValueError(f'{context}: numbered_body needs text starting with the exact explicit label')
            if not raw[len(label):].strip():
                raise ValueError(f'{context}: numbered_body must contain actual body after its label')
            lo, hi = (span['start_offset'], span['end_offset']) if span else (0, len(raw))
            def bound(a, b): return {'source_node_id': node.source_node_id, 'start_offset': a, 'end_offset': b}
            out.update(title=label, label=label, title_source_ids=[], content_source_ids=[],
                title_source_ranges=[bound(lo, lo + len(label))],
                content_source_ranges=[bound(lo + len(label), hi)])
            if 'continuations' in body:
                continued = bind_content(body['continuations'], 'content', context + '/numbered_body/continuations')
                out['content_source_ids'].extend(continued['content_source_ids'])
                out['content_source_ranges'].extend(continued['content_source_ranges'])
            out.pop('title_reference_source_ids', None)
        if not old and out.get('kind') == 'DOCUMENT' and not out.get('title'):
            out['title'] = source_display_name or Path(source_path).name
        return out

    def update_table(patch, old):
        _fields(patch, TABLE_FIELDS, 'logical_tables/' + patch['scope_id'])
        out = {**deepcopy(old), **deepcopy(patch)}
        if 'fragments' in patch:
            for fragment in out['fragments']:
                _fields(fragment, {'source_node_id', 'row_range'}, 'table fragment')
                fragment['source_node_id'] = source(fragment.get('source_node_id'), 'table fragment').source_node_id
        return out

    def merge(collection, id_field, removals, binder):
        changes = request.get(collection, []); deleted = request.get(removals, [])
        if not isinstance(changes, list) or not isinstance(deleted, list) or any(not isinstance(v, str) for v in deleted):
            raise ValueError(f'{collection}: changes/removals must be lists of objects/IDs')
        values = {v[id_field]: v for v in decisions[collection]}
        seen = set()
        for patch in changes:
            if not isinstance(patch, dict) or not isinstance(patch.get(id_field), str) or not patch[id_field]:
                raise ValueError(f'{collection}: each edit needs a nonempty {id_field}')
            key = patch[id_field]
            if key in seen or key in deleted: raise ValueError(f'{collection}: repeated/conflicting edit {key}')
            seen.add(key); values[key] = binder(patch, values.get(key, {}))
        for key in deleted:
            if key not in values: raise ValueError(f'{collection}: cannot remove unknown {key}')
            del values[key]
        decisions[collection] = list(values.values())

    merge('nodes', 'node_id', 'remove_node_ids', update_node)
    merge('logical_tables', 'scope_id', 'remove_table_ids', update_table)
    if 'source_dispositions' in request:
        entries = deepcopy(request['source_dispositions'])
        if not isinstance(entries, list): raise ValueError('source_dispositions replaces the complete disposition list')
        for entry in entries:
            _fields(entry, {'source_node_id', 'disposition', 'reason', 'start_offset', 'end_offset'}, 'disposition')
            entry['source_node_id'] = source(entry.get('source_node_id'), 'disposition').source_node_id
        decisions['source_dispositions'] = entries
    return decisions, {'schema_version': 'reqpilot-map-edit-binding/v1', 'source_sha256': snapshot.source_sha256,
        'source_ir_sha256': core.canonical_json_sha256(snapshot.model_dump(mode='json')),
        'base_map_sha256': base_sha, 'bindings': bindings,
        'changed_node_ids': [v['node_id'] for v in request.get('nodes', [])],
        'changed_table_ids': [v['scope_id'] for v in request.get('logical_tables', [])],
        'human_acceptance': 'NOT_CLAIMED', 'semantic_verification': 'NOT_RUN'}
