"""Expand an Agent-declared outline into existing Map edit shorthands.

The outline supplies every semantic role, boundary, parent and table owner.
This adapter only binds source selections, derives stable transport identities
and copies unambiguous original number prefixes. It never persists a second Map.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_numbering import _POSSIBLE_START, _prefix
from map_edit_tools import bind_map_edit


ROLE_KINDS = {
    'DOCUMENT': 'DOCUMENT', 'ATTACHMENT': 'ATTACHMENT',
    'HEADING': 'SECTION', 'CLAUSE': 'SECTION', 'COVER': 'COVER',
    'CONTENTS': 'TABLE_OF_CONTENTS', 'CONTROL': 'DOCUMENT_CONTROL',
}
FRONT_MATTER_TITLES = {'COVER': 'Cover', 'CONTENTS': 'Contents', 'CONTROL': 'Document control'}
MAX_OUTLINE_DEPTH = 64


def _schema_defaults(schema):
    for value in schema.get('properties', {}).values():
        if value.get('default', 'absent') is None:
            value.pop('default')


class _OutlineInput(BaseModel):
    model_config = ConfigDict(extra='forbid', json_schema_extra=_schema_defaults)


# Transport declarations mirror the existing selectors without importing
# task_contracts (which imports OutlineNode). The sole selector interpreter
# remains bind_map_edit; these models do not locate or parse source text.
_SourceRef = Annotated[StrictInt, Field(ge=0)] | Annotated[StrictStr, Field(min_length=1)]
_Nonempty = Annotated[StrictStr, Field(min_length=1)]
_RowRange = tuple[Annotated[StrictInt, Field(ge=0)], Annotated[StrictInt, Field(gt=0)]]


class _OutlineSourceSelector(_OutlineInput):
    source: _SourceRef
    quote: _Nonempty = None
    start: _Nonempty = None
    end_before: _Nonempty = None

    @model_validator(mode='after')
    def exclusive_markers(self):
        if 'quote' in self.model_fields_set and self.model_fields_set & {'start', 'end_before'}:
            raise ValueError('Use quote or start/end_before, not both')
        return self


_SourceSelection = _SourceRef | _OutlineSourceSelector


class OutlineTableSource(_OutlineInput):
    source: _SourceRef
    row_range: _RowRange = None

    @model_validator(mode='after')
    def half_open(self):
        if self.row_range is not None and self.row_range[1] <= self.row_range[0]:
            raise ValueError('Row range must be nonempty and half-open')
        return self


class OutlineEvidenceRef(OutlineTableSource):
    """An auxiliary source reference, never a claim of content ownership."""


class OutlineTable(_OutlineInput):
    sources: Annotated[tuple[_SourceRef | OutlineTableSource, ...], Field(
        min_length=1, description='Captured table refs, optionally with zero-based half-open original row_range; include all declared continuation fragments.')]
    title: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    role: Annotated[StrictStr, Field(min_length=1, max_length=120)]


class OutlineNode(_OutlineInput):
    role: Literal['DOCUMENT', 'ATTACHMENT', 'HEADING', 'CLAUSE', 'COVER', 'CONTENTS', 'CONTROL']
    ref: _SourceSelection = Field(default=None, description='Exact original heading or complete numbered clause: capture index, complete source ID, or exact text selector. A virtual DOCUMENT root may omit it. Whole COVER/CONTENTS/CONTROL blocks may omit it when content supplies their complete original evidence.')
    label: Annotated[StrictStr, Field(max_length=240)] = Field(default=None, description='Optional exact original prefix. Unambiguous HEADING/CLAUSE numbers are copied from source; ambiguous CLAUSE prefixes require this explicit choice.')
    content: tuple[_SourceSelection, ...] = Field(default=(), description='Direct original content refs; for CLAUSE these continue its complete body.')
    children: tuple[OutlineNode, ...] = Field(default=(), description='Agent-declared children. Nesting supplies parent relationships; no hierarchy is inferred from numbering.')
    tables: tuple[OutlineTable, ...] = Field(default=(), description='Complete logical tables explicitly owned by this node; the adapter does not infer table membership or ownership.')
    evidence_refs: tuple[OutlineEvidenceRef, ...] = Field(default=(), description='Auxiliary original source references, optionally with original row ranges. These do not claim content ownership or change the node identity.')

    @model_validator(mode='after')
    def source_required(self):
        if 'ref' not in self.model_fields_set and self.role != 'DOCUMENT':
            if self.role not in FRONT_MATTER_TITLES or not self.content:
                raise ValueError('Only DOCUMENT or whole COVER/CONTENTS/CONTROL with nonempty content may omit ref')
        return self


def _object(value, fields, context):
    if not isinstance(value, dict):
        raise ValueError(f'{context}: expected an object')
    if set(value) - fields:
        raise ValueError(f'{context}: unknown fields {sorted(set(value) - fields)}')


def _list(value, context, *, nonempty=False):
    if not isinstance(value, (list, tuple)) or nonempty and not value:
        raise ValueError(f'{context}: expected a {"nonempty " if nonempty else ""}list')
    return value


def expand_outline(snapshot, outline, source_path, *, source_display_name=None):
    """Return ``{nodes, logical_tables, receipt}`` for ``reqpilot-map-edit/v1``.

    No base revision is read or changed. The caller passes these shorthands to
    the ordinary binder/compiler so coverage, overlaps, order and persistence
    retain their existing contracts. All selector parsing belongs to the binder.
    """
    _list(outline, 'outline', nonempty=True)
    flat, slots, provisional = [], [], []

    def selection(value, context):
        index = len(slots)
        slots.append({'context': context, 'value': value})
        # These temporary nodes exist only in memory. Content binding accepts
        # the complete existing SourceSelection language without new parsing.
        provisional.append({'node_id': f'outline-bind-{index}', 'content': [value]})
        return index

    stack = [(node, None, f'outline[{i}]', 1) for i, node in reversed(list(enumerate(outline)))]
    virtual_root = False
    while stack:
        node, parent, context, depth = stack.pop()
        if depth > MAX_OUTLINE_DEPTH:
            raise ValueError(f'{context}: outline exceeds maximum depth {MAX_OUTLINE_DEPTH}')
        _object(node, {'role', 'ref', 'label', 'content', 'children', 'tables', 'evidence_refs'}, context)
        role = node.get('role')
        if not isinstance(role, str) or role not in ROLE_KINDS:
            raise ValueError(f'{context}: role must be one of {", ".join(ROLE_KINDS)}')
        if 'label' in node and not isinstance(node['label'], str):
            raise ValueError(f'{context}/label: expected exact original text')
        content = _list(node.get('content', []), context + '/content')
        children = _list(node.get('children', []), context + '/children')
        tables = _list(node.get('tables', []), context + '/tables')
        _list(node.get('evidence_refs', []), context + '/evidence_refs')
        if 'ref' not in node:
            if role in FRONT_MATTER_TITLES:
                if not content:
                    raise ValueError(f'{context}: a whole {role} without ref needs nonempty content')
            elif role != 'DOCUMENT' or parent is not None:
                raise ValueError(f'{context}: only a DOCUMENT root or whole front matter with content may omit ref')
            elif virtual_root:
                raise ValueError(f'{context}: duplicate virtual DOCUMENT root; cite distinct source refs')
            else:
                virtual_root = True
            if node.get('label'):
                raise ValueError(f'{context}: a node without ref cannot have a source label')
        record = {'spec': node, 'parent': parent, 'context': context,
                  'ref': selection(node['ref'], context + '/ref') if 'ref' in node else None,
                  'content': [selection(ref, context + f'/content[{i}]') for i, ref in enumerate(content)],
                  'tables': []}
        for i, table in enumerate(tables):
            tc = context + f'/tables[{i}]'
            _object(table, {'sources', 'title', 'role'}, tc)
            for field in ('title', 'role'):
                if not isinstance(table.get(field), str) or not table[field].strip():
                    raise ValueError(f'{tc}/{field}: required nonempty Agent-declared text')
            fragments = []
            for j, ref in enumerate(_list(table.get('sources'), tc + '/sources', nonempty=True)):
                fc = tc + f'/sources[{j}]'
                if isinstance(ref, dict):
                    _object(ref, {'source', 'row_range'}, fc)
                    if 'source' not in ref:
                        raise ValueError(f'{fc}: source is required')
                    if 'row_range' in ref:
                        rows = ref['row_range']
                        if (not isinstance(rows, (list, tuple)) or len(rows) != 2
                                or any(type(v) is not int for v in rows)
                                or not 0 <= rows[0] < rows[1]):
                            raise ValueError(f'{fc}: row_range must be a nonempty half-open integer range')
                    else:
                        rows = None
                    source = ref['source']
                else:
                    source, rows = ref, None
                fragments.append({'slot': selection(source, fc), 'rows': rows, 'context': fc})
            record['tables'].append({'spec': table, 'fragments': fragments, 'context': tc})
        index = len(flat)
        flat.append(record)
        stack.extend((child, index, context + f'/children[{i}]', depth + 1)
                     for i, child in reversed(list(enumerate(children))))

    bound, _ = bind_map_edit(snapshot,
        {'schema_version': 'reqpilot-map-edit/v1', 'nodes': provisional},
        source_path, source_display_name=source_display_name)
    sources = {node.source_node_id: node for node in snapshot.nodes}
    for slot, result in zip(slots, bound['nodes']):
        spans = result['content_source_ranges']
        if spans:
            span = spans[0]
            source = sources[span['source_node_id']]
            lo, hi = span['start_offset'], span['end_offset']
        else:
            source = sources[result['content_source_ids'][0]]
            lo, hi = 0, len(source.raw_text)
        slot.update(source=source, lo=lo, hi=hi, text=source.raw_text[lo:hi], binding=result)

    def identity(prefix, payload):
        return prefix + core.canonical_json_sha256([snapshot.source_sha256, payload])[:32]

    def position(slot):
        return [slot['source'].source_node_id, slot['lo'], slot['hi']]

    def bound_content(record):
        return {field: [value for index in record['content'] for value in slots[index]['binding'][field]]
                for field in ('content_source_ids', 'content_source_ranges')}

    def title_binding(slot):
        return {field.replace('content_', 'title_'): slot['binding'][field]
                for field in ('content_source_ids', 'content_source_ranges')}

    nodes, tables, node_receipts, table_receipts = [], [], [], []
    seen_nodes, seen_tables = set(), set()
    for record in flat:
        spec, context = record['spec'], record['context']
        role = spec['role']
        slot = slots[record['ref']] if record['ref'] is not None else None
        extent = (position(slot) if slot is not None else
                  ['content-group', sorted(position(slots[index]) for index in record['content'])]
                  if role in FRONT_MATTER_TITLES else ['virtual-document'])
        extent_key = core.canonical_json_sha256(extent)
        if extent_key in seen_nodes:
            raise ValueError(f'{context}: duplicate source identity; use distinct exact source selections')
        seen_nodes.add(extent_key)
        node_id = identity('outline-', [role, extent])
        record['node_id'] = node_id
        node = {'node_id': node_id,
                'parent_id': flat[record['parent']]['node_id'] if record['parent'] is not None else None,
                'kind': ROLE_KINDS[role]}
        label, automatic_prefix = spec.get('label', ''), None
        if slot is not None:
            if slot['source'].table_payload is not None or slot['source'].asset_ref or not slot['text'].strip():
                raise ValueError(f'{context}/ref: node ref must select original text; declare tables under tables')
            if 'label' not in spec and role in {'HEADING', 'CLAUSE'}:
                match = _prefix(slot['text'])
                # Do not turn a quote starting inside another numeric token or
                # a selection containing several numbered starts into a label.
                start = slot['lo'] + match.start('label') if match is not None else 0
                prior = slot['source'].raw_text[start - 1] if start else ''
                later = any(_prefix(slot['text'][boundary.end():]) is not None
                            for boundary in _POSSIBLE_START.finditer(slot['text']))
                if match is not None and not later and not (prior.isalnum() or prior and prior in '.．'):
                    label = match.group('label')
                    automatic_prefix = match
            if role == 'CLAUSE':
                if not label:
                    raise ValueError(f'{context}/label: CLAUSE needs an explicit exact label when the prefix is ambiguous')
                if automatic_prefix is not None and automatic_prefix.start('label'):
                    end = slot['lo'] + automatic_prefix.end('label')
                    if not slot['source'].raw_text[end:slot['hi']].strip():
                        raise ValueError(f'{context}: numbered_body must contain actual body after its label')
                    node.update(title=label, label=label, title_source_ids=[],
                                title_source_ranges=[{'source_node_id': slot['source'].source_node_id,
                                                      'start_offset': slot['lo'], 'end_offset': end}],
                                **bound_content(record))
                    node['content_source_ranges'].insert(0, {'source_node_id': slot['source'].source_node_id,
                                                             'start_offset': end, 'end_offset': slot['hi']})
                else:
                    node['numbered_body'] = {'source': spec['ref'], 'label': label,
                                             'continuations': list(spec.get('content', []))}
            else:
                if automatic_prefix is not None and automatic_prefix.start('label'):
                    node.update(title=slot['text'][automatic_prefix.end('label'):].strip() or label,
                                label=label, **title_binding(slot), **bound_content(record))
                else:
                    node['heading'] = {'source': spec['ref'], 'label': label}
                    node['content'] = list(spec.get('content', []))
        else:
            node.update(label='', title=FRONT_MATTER_TITLES.get(role) or source_display_name or Path(source_path).name,
                        content=list(spec.get('content', [])))
        if 'evidence_refs' in spec:
            node['evidence_refs'] = list(spec['evidence_refs'])
        nodes.append(node)
        node_receipts.append({'node_id': node_id, 'role': role, 'source_position': extent,
                              'label_source': 'EXPLICIT' if 'label' in spec else ('SOURCE_PREFIX' if label else 'EMPTY')})
        for table in record['tables']:
            fragments = []
            for fragment in table['fragments']:
                selected = slots[fragment['slot']]
                source = selected['source']
                if source.table_payload is None:
                    raise ValueError(f'{fragment["context"]}: table source must refer to an existing TableIR source node')
                rows = list(fragment['rows']) if fragment['rows'] is not None else [0, source.table_payload.row_count]
                if rows[1] > source.table_payload.row_count:
                    raise ValueError(f'{fragment["context"]}: row_range exceeds the captured table')
                fragments.append({'source_node_id': source.source_node_id, 'row_range': rows})
            # Table identity follows its exact region set; changing its owner,
            # display title or declaration order must not create a new table.
            region_set = sorted((fragment['source_node_id'], *fragment['row_range']) for fragment in fragments)
            if len(region_set) != len(set(region_set)):
                raise ValueError(f'{table["context"]}: duplicate table fragment')
            scope_id = identity('outline-table-', region_set)
            if scope_id in seen_tables:
                raise ValueError(f'{table["context"]}: duplicate table scope')
            seen_tables.add(scope_id)
            tables.append({'scope_id': scope_id, 'owner_node_id': node_id,
                           'title': table['spec']['title'], 'role': table['spec']['role'], 'fragments': fragments})
            table_receipts.append({'scope_id': scope_id, 'fragments': fragments})

    # Let the existing shorthands reject incorrect explicit prefixes and empty
    # numbered bodies before returning. The caller still runs the full compiler.
    bind_map_edit(snapshot, {'schema_version': 'reqpilot-map-edit/v1', 'nodes': nodes, 'logical_tables': tables},
                  source_path, source_display_name=source_display_name)
    return {'nodes': nodes, 'logical_tables': tables, 'receipt': {
        'schema_version': 'reqpilot-outline-expansion/v1', 'source_sha256': snapshot.source_sha256,
        'source_ir_sha256': core.canonical_json_sha256(snapshot.model_dump(mode='json')),
        'nodes': node_receipts, 'logical_tables': table_receipts,
        'elided_fields': {'node_id': len(nodes), 'parent_id': len(nodes),
                          'label': sum('label' not in record['spec'] for record in flat),
                          'scope_id': len(tables), 'owner_node_id': len(tables)},
        'explicit_identity_and_label_fields': {
            'outline': sum('label' in record['spec'] for record in flat),
            'expanded': 3 * len(nodes) + 2 * len(tables)},
        'human_acceptance': 'NOT_CLAIMED', 'semantic_verification': 'NOT_RUN'}}
