"""Explicit SourceItem decomposition using the existing exact-cover validator.

This creates a versioned analysis view. It never edits a Document Map, source
record, source numbering or customer obligation. The compiler owns identities
and hashes; Agent decisions own proposed semantic boundaries.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re

from app.application.agent_workflow.source_requirement_baseline import (
    SourceRequirementBaselineArtifactV1, Requirementness, RequirementKind,
    canonical_json_sha256,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest, RequirementAtomizationSourceSegment,
    RequirementAtomizationObligation,
)
from app.application.extraction.native_atomization_reference import (
    NativeAtomizationReferenceV1, validate_native_atomization_reference,
)


class SourceItemDecompositionError(ValueError):
    pass


def _hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def build_source_item_decomposition_packet(baseline, source_item_id, *, table_previews=None):
    baseline = SourceRequirementBaselineArtifactV1.model_validate(baseline)
    record = next((r for r in baseline.records if r.source_record_id == source_item_id), None)
    if record is None or record.requirementness is not Requirementness.REQUIREMENT:
        raise SourceItemDecompositionError('Decomposition requires an existing requirement SourceItem')
    facts, shared = [], []
    for binding in record.text_bindings:
        facts.append({'text': binding.text, 'page': binding.page, 'bbox': list(binding.bbox) if binding.bbox is not None else None,
                      'source_ref': {'kind': 'TEXT', 'content_id': binding.content_id,
                                     'start_offset': binding.start_offset, 'end_offset': binding.end_offset,
                                     **({'native_locator': binding.native_locator.model_dump(mode='json', exclude_none=True), 'source_sequence': binding.source_sequence} if binding.native_locator is not None else {})},
                      'precision': binding.precision})
    if record.table_fragments:
        previews = table_previews or {}
        if isinstance(previews, list):
            previews = {p['logical_table_scope_id']: p for p in previews}
        preview = previews.get(record.logical_table_scope_id)
        if not preview or preview.get('source_sha256') != baseline.source_sha256:
            raise SourceItemDecompositionError('Missing same-source table facts')
        fragments = {p['fragment_id']: p for p in preview['fragments']}
        seen = set()
        for binding in record.table_fragments:
            fragment = fragments.get(binding.fragment_id)
            if fragment is None:
                raise SourceItemDecompositionError('Missing table fragment')
            cells = {c.get('cell_id', c.get('id')): c for c in fragment['cells']}
            if set(binding.cell_ids) - set(cells):
                raise SourceItemDecompositionError('Missing bound table cells')
            for cell_id in binding.cell_ids:
                if (binding.fragment_id, cell_id) in seen:
                    continue
                seen.add((binding.fragment_id, cell_id))
                cell = cells[cell_id]
                if cell['text'].strip():
                    facts.append({'text': cell['text'], 'page': binding.page, 'bbox': list(cell['bbox']) if cell['bbox'] is not None else None,
                        'source_ref': {'kind': 'TABLE_CELL', 'table_id': binding.fragment_id,
                                       'cell_id': cell_id, 'start_offset': 0, 'end_offset': len(cell['text']),
                                       **({key: cell[key] for key in ('native_locator', 'source_sequence')} if cell.get('native_locator') else {})},
                        'precision': 'CELL_TEXT_RANGE'})
        for fragment in fragments.values():
            for cell in fragment['cells']:
                cid = cell.get('cell_id', cell.get('id'))
                if cid in set(record.shared_header_cell_ids) | set(record.shared_table_cell_ids):
                    shared.append({'text': cell['text'], 'page': fragment['page'],
                                   'source_ref': {'kind': 'TABLE_CELL', 'table_id': fragment['fragment_id'], 'cell_id': cid, **({key: cell[key] for key in ('native_locator', 'source_sequence')} if cell.get('native_locator') else {})}})
    if not facts:
        raise SourceItemDecompositionError('SourceItem has no readable source segments')
    for index, fact in enumerate(facts):
        fact.update(segment_index=index, text_sha256=_hash_text(fact['text']))
    evidence_sha = canonical_json_sha256({'facts': facts, 'shared': shared})
    return {'schema_version': 'source-item-decomposition-packet/v1', 'baseline_sha256': baseline.artifact_sha256,
            'source_sha256': baseline.source_sha256, 'source_item_id': record.source_record_id,
            'source_display_id': record.source_display_id, 'requirement_display_id': record.requirement_display_id,
            'ancestor_path': list(record.ancestor_path), 'source_form': record.source_form.value,
            'requirement_kinds': [k.value for k in record.requirement_kinds],
            'segments': facts, 'shared_source_context': shared, 'parent_evidence_sha256': evidence_sha}


def _provider_request(packet):
    parent_id = packet['source_item_id']
    lineage = canonical_json_sha256({'baseline': packet['baseline_sha256'], 'parent': parent_id,
                                    'evidence': packet['parent_evidence_sha256']})
    segments = tuple(RequirementAtomizationSourceSegment(
        segment_index=f['segment_index'], semantic_unit_id='semantic-unit-' + canonical_json_sha256(f)[:32],
        owner_structure_node_id='structure-node-' + _hash_text(parent_id)[:32],
        source_start_offset=0, source_end_offset=len(f['text']), source_text=f['text']) for f in packet['segments'])
    return RequirementAtomizationProviderRequest(lineage_sha256=lineage, review_node_id=parent_id,
        requirement_subtype=' / '.join(packet['requirement_kinds']),
        parent_context_statement=' › '.join(packet['ancestor_path']), source_segments=segments)


def _source_excerpt(packet, value):
    index, start, end = value['segment_index'], value['start_offset'], value['end_offset']
    if type(index) is not int or not 0 <= index < len(packet['segments']):
        raise SourceItemDecompositionError('Unknown source segment')
    fact = packet['segments'][index]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(fact['text']):
        raise SourceItemDecompositionError('Invalid source character range')
    return {'text': fact['text'][start:end], 'page': fact['page'], 'bbox': fact['bbox'],
            'source_ref': {**fact['source_ref'], 'start_offset': fact['source_ref']['start_offset'] + start,
                           'end_offset': fact['source_ref']['start_offset'] + end},
            'precision': 'PARENT_BINDING_TEXT_RANGE' if fact['source_ref']['kind'] == 'TEXT' else 'CELL_TEXT_RANGE'}


def _explicit_limits(text):
    """Recognize only unambiguous numeric comparator spellings; not all semantics."""
    number = r'[+-]?\d+(?:\.\d+)?'
    result = set()
    operators = {'≥': '>=', '≤': '<=', '=': '=', '>': '>', '<': '<', '>=': '>=', '<=': '<='}
    for match in re.finditer(r'(>=|<=|≥|≤|>|<|=)\s*(' + number + ')', text):
        result.add((operators[match[1]], match[2]))
    words = ((r'不小于|不少于|至少|at\s+least|not\s+less\s+than', '>='),
             (r'不大于|不超过|至多|at\s+most|not\s+exceed|no\s+more\s+than', '<='),
             (r'大于|more\s+than', '>'), (r'小于|less\s+than', '<'))
    matched_spans = []
    for pattern, operator in words:
        for match in re.finditer(r'(?<!不)(?:' + pattern + r')\s*(' + number + ')', text, re.I):
            if any(match.start() < end and match.end() > start for start, end in matched_spans):
                continue
            result.add((operator, match[1]))
            matched_spans.append(match.span())
    for match in re.finditer('(' + number + r')\s*(?:[A-Za-z%℃°μµ/]+\s*)?(以上|以下)', text):
        result.add(('>=' if match[2] == '以上' else '<=', match[1]))
    return result


def _validate_cross_child_alternatives(packet, children, logic):
    if logic == 'ANY_OF':
        return
    for segment in packet['segments']:
        text = segment['text']
        owned = []
        for child_index, child in enumerate(children):
            owned.extend((r['start_offset'], r['end_offset'], child_index)
                         for r in child['source_ranges'] if r['segment_index'] == segment['segment_index'])
        for token in re.finditer(r'\bor\b|或者|或', text, re.I):
            left = [(min(end, token.start()) - 1, owner) for start, end, owner in owned if start < token.start()]
            right = [(max(start, token.end()), owner) for start, end, owner in owned if end > token.end()]
            if not left or not right:
                continue
            before, after = max(left), min(right)
            if before[1] == after[1]:
                continue
            bridge = text[before[0]:after[0] + 1]
            if len(bridge) < 160 and not re.search(r'[。；;.!?]', bridge):
                raise SourceItemDecompositionError('An original alternative crosses children: keep it together or use ANY_OF, not ALL_OF')


def compile_source_item_decomposition(baseline, decisions, *, table_previews=None, previous=None, authorized_source_item_ids=None):
    """Compile [{source_item_id, action: SPLIT|KEEP|UNDO, reason, children, context_ranges}].

    Children contain text, derivation_type (VERBATIM_SPAN|NORMALIZED_DERIVATION),
    source_ranges (segment_index/start_offset/end_offset), requirement_kinds.
    Context ranges contain source_range and disposition; they remain parent
    context, not an assertion that every condition applies to every child.
    """
    baseline = SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if not isinstance(decisions, list):
        raise SourceItemDecompositionError('Decisions must be an explicit list')
    records, receipts, kept, retired = {}, {}, [], []
    identity_registry, reserved, stale_parents = {}, [], set()
    if previous is not None:
        validate_source_item_decomposition(baseline, previous, table_previews=table_previews)
        records = deepcopy(previous['records'])
        receipts = deepcopy(previous['validation_receipts'])
        kept = list(previous.get('kept_source_ids', []))
        retired = list(previous.get('retired_singular_ids', []))
        identity_registry = deepcopy(previous.get('identity_registry', {}))
        reserved = deepcopy(previous.get('reserved_display_ids', []))
        stale_parents = set(previous.get('carry_forward', {}).get('stale_parent_ids', []))
        if previous.get('retired_singular_ids') and not identity_registry:
            raise SourceItemDecompositionError('Missing historical Snn registry; cannot safely reuse retired numbers')
        for parent, children in records.items():
            identity_registry.setdefault(parent, {}).update({c['singular_requirement_id']: c['requirement_display_id'] for c in children})
    dispositions = deepcopy((previous or {}).get('dispositions', {}))
    prior_scope = (previous or {}).get('review_scope', {}).get('authorized_source_item_ids', [])
    requested = list(authorized_source_item_ids) if authorized_source_item_ids is not None else [d['source_item_id'] for d in decisions]
    required_ids = {r.source_record_id for r in baseline.records if r.requirementness is Requirementness.REQUIREMENT}
    if len(requested) != len(set(requested)) or set(requested) - required_ids:
        raise SourceItemDecompositionError('Authorized decomposition scope must contain unique current requirement SourceItems')
    authorized = set(prior_scope) | set(requested)
    if {d['source_item_id'] for d in decisions} - set(requested):
        raise SourceItemDecompositionError('Decomposition decisions exceed the authorized scope')
    seen = set()
    for decision in decisions:
        source_id = decision['source_item_id']
        if source_id in seen:
            raise SourceItemDecompositionError('A SourceItem may be decided only once per revision')
        seen.add(source_id)
        packet = build_source_item_decomposition_packet(baseline, source_id, table_previews=table_previews)
        action = decision.get('action', 'SPLIT')
        stale_parents.discard(source_id)
        if not str(decision.get('reason', '')).strip():
            raise SourceItemDecompositionError('A decomposition decision requires its material reason')
        dispositions[source_id] = {'action': action, 'reason': decision['reason'],
            'parent_evidence_sha256': packet['parent_evidence_sha256'], 'baseline_sha256': baseline.artifact_sha256,
            'source_display_id': packet['source_display_id'], 'requirement_display_id': packet['requirement_display_id'],
            'authority': 'AGENT_WORKING_PROPOSAL'}
        if action in {'KEEP', 'UNDO'}:
            retired.extend(child['singular_requirement_id'] for child in records.pop(source_id, []))
            receipts.pop(source_id, None)
            if source_id not in kept:
                kept.append(source_id)
            continue
        if action != 'SPLIT' or len(decision.get('children', [])) < 2:
            raise SourceItemDecompositionError('No pseudo S01: a split must contain at least two useful children')
        request = _provider_request(packet)
        children = decision['children']
        logic = decision.get('logic', 'ALL_OF')
        if logic not in {'ALL_OF', 'ANY_OF'}:
            raise SourceItemDecompositionError('Decomposition logic must be ALL_OF or ANY_OF')
        _validate_cross_child_alternatives(packet, children, logic)
        obligations = [RequirementAtomizationObligation(
            source_ranges=child['source_ranges'], atomic_statement=child['text'],
            split_basis='independent_obligation', reason_codes=('explicit_source_decomposition',)) for child in children]
        contexts = [{'source_range': context['source_range'], 'disposition': context.get('disposition', 'CONTEXT'),
                     'reason_codes': ['preserved_parent_context']} for context in decision.get('context_ranges', [])]
        reference = NativeAtomizationReferenceV1(lineage_sha256=request.lineage_sha256, review_node_id=source_id,
            status='PARTIAL' if any(c['disposition']=='UNRESOLVED' for c in contexts) else 'OBSERVED',
            obligations=obligations, context_ranges=contexts)
        proof = validate_native_atomization_reference(request, reference)
        context_excerpts = [_source_excerpt(packet, c['source_range']) for c in contexts]
        compiled = []
        statements = set()
        for number, child in enumerate(children, 1):
            text = child['text'].strip()
            if text in statements:
                raise SourceItemDecompositionError('Duplicate child statements')
            statements.add(text)
            excerpts = [_source_excerpt(packet, r) for r in child['source_ranges']]
            kind = child.get('derivation_type', 'VERBATIM_SPAN')
            if kind == 'VERBATIM_SPAN':
                if ''.join(text.split()) != ''.join(''.join(e['text'] for e in excerpts).split()):
                    raise SourceItemDecompositionError('Verbatim child differs from its cited source')
            elif kind == 'NORMALIZED_DERIVATION':
                # This is a deterministic invention signal, not a proof of semantic equivalence.
                numbers = lambda value: set(re.findall(r'[+-]?\d+(?:\.\d+)?', value))
                available = '\n'.join(e['text'] for e in excerpts + context_excerpts + packet['shared_source_context'])
                if numbers(text) - numbers(available):
                    raise SourceItemDecompositionError('Normalized child introduces an unsupported numeric value')
                source_limits = _explicit_limits(available)
                for operator, number in _explicit_limits(text):
                    same_value = {op for op, value in source_limits if value == number}
                    if same_value and operator not in same_value:
                        raise SourceItemDecompositionError('Normalized child changes an explicit numeric comparator')
            else:
                raise SourceItemDecompositionError('Unsupported derivation type')
            kinds = [RequirementKind(k).value for k in child.get('requirement_kinds', packet['requirement_kinds'])]
            if not kinds or len(kinds) != len(set(kinds)):
                raise SourceItemDecompositionError('Child requirement classes must be explicit and unique')
            identity_payload = {'parent': source_id, 'text': text, 'source_ranges': child['source_ranges'], 'type': kind}
            child_id = 'singular-requirement-' + canonical_json_sha256(identity_payload)[:32]
            registry = identity_registry.setdefault(source_id, {})
            if child_id in registry:
                display_id = registry[child_id]
                if not display_id.startswith(packet['requirement_display_id'] + '-S'):
                    raise SourceItemDecompositionError('Parent REQ identity changed; explicit identity migration is required before reusing historical Snn')
            else:
                reserved_numbers = [int(item['id'].rsplit('-S', 1)[1]) for item in reserved
                                    if item['id'].startswith(packet['requirement_display_id'] + '-S')]
                maximum = max([0, *reserved_numbers, *[int(display.rsplit('-S', 1)[1]) for display in registry.values()]])
                display_id = packet['requirement_display_id'] + f'-S{maximum+1:02d}'
                registry[child_id] = display_id
            compiled.append({'singular_requirement_id': child_id, 'requirement_display_id': display_id,
                'source_item_id': source_id, 'source_display_id': packet['source_display_id'],
                'source_requirement_id': packet['requirement_display_id'], 'relation': 'DECOMPOSED_FROM_SOURCE',
                'text': text, 'derivation_type': kind, 'group_logic': logic, 'source_ranges': deepcopy(child['source_ranges']),
                'source_excerpts': excerpts, 'requirement_kinds': kinds,
                'parent_evidence_sha256': packet['parent_evidence_sha256'],
                'parent_context': context_excerpts, 'shared_source_context': packet['shared_source_context'],
                'reason': decision['reason'], 'authority': 'AGENT_WORKING_PROPOSAL'})
        retired.extend(c['singular_requirement_id'] for c in records.get(source_id, [])
                       if c['singular_requirement_id'] not in {x['singular_requirement_id'] for x in compiled})
        records[source_id] = compiled
        kept = [value for value in kept if value != source_id]
        active_ids = {child['singular_requirement_id'] for child in compiled}
        retired = [value for value in retired if value not in active_ids]
        receipts[source_id] = proof.model_dump(mode='json')
    payload = {'schema_version': 'source-item-decomposition/v1', 'baseline_sha256': baseline.artifact_sha256,
               'source_sha256': baseline.source_sha256, 'revision': 1 if previous is None else previous['revision']+1,
               'predecessor_sha256': previous['artifact_sha256'] if previous else '', 'records': records,
               'validation_receipts': receipts, 'kept_source_ids': kept, 'retired_singular_ids': retired,
               'identity_registry': identity_registry,
               'status': 'PARTIAL' if stale_parents or any(r['status'] == 'PARTIAL' for r in receipts.values()) else 'COMPLETE',
               'human_accepted': False}
    if reserved:
        payload['reserved_display_ids'] = reserved
    if previous and previous.get('carry_forward'):
        payload['carry_forward'] = {**deepcopy(previous['carry_forward']), 'stale_parent_ids': sorted(stale_parents),
            'retained_parent_ids': [sid for sid in previous['carry_forward'].get('retained_parent_ids', []) if sid not in seen]}
    payload['dispositions'] = dispositions
    payload['review_scope'] = decomposition_review_scope(baseline, authorized, dispositions, stale_parents, receipts)
    if payload['review_scope']['remaining_source_item_ids']:
        payload['status'] = 'PARTIAL'
    digest = canonical_json_sha256(payload)
    return {**payload, 'artifact_id': 'source-item-decomposition-' + digest[:32], 'artifact_sha256': digest}


def decomposition_review_scope(baseline, authorized, dispositions, stale, receipts):
    ordered = [r.source_record_id for r in baseline.records if r.source_record_id in set(authorized)]
    completed = [sid for sid in ordered if sid in dispositions and sid not in stale
                 and (sid not in receipts or receipts[sid]['status'] != 'PARTIAL')]
    return {'scope_basis': 'EXPLICIT_REQUESTS_IN_THIS_LINEAGE', 'authorized_source_item_ids': ordered,
            'decided_source_item_ids': completed, 'remaining_source_item_ids': [sid for sid in ordered if sid not in completed],
            'authorized_count': len(ordered), 'decided_count': len(completed), 'remaining_count': len(ordered)-len(completed),
            'complete': len(completed) == len(ordered)}


def _stored_decision(packet, children, status):
    context_ranges = []
    for excerpt in children[0].get('parent_context', []):
        matches = []
        for fact in packet['segments']:
            source_ref, excerpt_ref = fact['source_ref'], excerpt['source_ref']
            if {k: v for k, v in source_ref.items() if k not in {'start_offset', 'end_offset'}} != {
                    k: v for k, v in excerpt_ref.items() if k not in {'start_offset', 'end_offset'}}:
                continue
            start, end = excerpt_ref['start_offset'] - source_ref['start_offset'], excerpt_ref['end_offset'] - source_ref['start_offset']
            if 0 <= start < end <= len(fact['text']):
                value = {'segment_index': fact['segment_index'], 'start_offset': start, 'end_offset': end}
                if _source_excerpt(packet, value) == excerpt:
                    matches.append(value)
        if len(matches) != 1:
            raise SourceItemDecompositionError('Stored parent context is not an exact source range')
        context_ranges.append({'source_range': matches[0], 'disposition': 'UNRESOLVED' if status == 'PARTIAL' else 'CONTEXT'})
    return {'source_item_id': packet['source_item_id'], 'action': 'SPLIT', 'reason': children[0]['reason'],
            'logic': children[0]['group_logic'], 'children': children, 'context_ranges': context_ranges}


def validate_source_item_decomposition(baseline, artifact, *, table_previews=None):
    baseline = SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if artifact.get('baseline_sha256') != baseline.artifact_sha256 or artifact.get('source_sha256') != baseline.source_sha256:
        raise SourceItemDecompositionError('Decomposition is stale for this source baseline')
    digest = canonical_json_sha256({k:v for k,v in artifact.items() if k not in {'artifact_id','artifact_sha256'}})
    if artifact.get('artifact_sha256') != digest or artifact.get('artifact_id') != 'source-item-decomposition-' + digest[:32]:
        raise SourceItemDecompositionError('Decomposition artifact was modified after compilation')
    if 'review_scope' in artifact or 'dispositions' in artifact:
        if not isinstance(artifact.get('dispositions'), dict) or not isinstance(artifact.get('review_scope'), dict):
            raise SourceItemDecompositionError('Decomposition review scope and dispositions must be saved together')
        dispositions = artifact['dispositions']
        known = {r.source_record_id for r in baseline.records if r.requirementness is Requirementness.REQUIREMENT}
        authorized = artifact['review_scope'].get('authorized_source_item_ids', [])
        if len(authorized) != len(set(authorized)) or set(authorized) - known or set(dispositions) - known:
            raise SourceItemDecompositionError('Saved decomposition scope refers to an unknown source item')
        for sid, disposition in dispositions.items():
            packet = build_source_item_decomposition_packet(baseline, sid, table_previews=table_previews)
            if (disposition.get('action') not in {'KEEP', 'UNDO', 'SPLIT'} or not str(disposition.get('reason', '')).strip()
                    or disposition.get('baseline_sha256') != baseline.artifact_sha256
                    or disposition.get('parent_evidence_sha256') != packet['parent_evidence_sha256']
                    or disposition.get('authority') != 'AGENT_WORKING_PROPOSAL'):
                raise SourceItemDecompositionError('Saved decomposition disposition lost its reason or source evidence')
            if (disposition['action'] == 'SPLIT') != bool(artifact['records'].get(sid)):
                raise SourceItemDecompositionError('Saved disposition differs from its actual decomposition')
            if disposition['action'] != 'SPLIT' and sid not in artifact.get('kept_source_ids', []):
                raise SourceItemDecompositionError('Kept disposition is missing its retained parent')
        expected = decomposition_review_scope(baseline, authorized, dispositions,
            artifact.get('carry_forward', {}).get('stale_parent_ids', []), artifact['validation_receipts'])
        if artifact['review_scope'] != expected or (not expected['complete'] and artifact.get('status') != 'PARTIAL'):
            raise SourceItemDecompositionError('Saved review scope falsely reports completion')
    registry = artifact.get('identity_registry', {})
    registered = {sid: display for values in registry.values() for sid, display in values.items()}
    if set(artifact.get('retired_singular_ids', [])) - set(registered):
        raise SourceItemDecompositionError('Retired Snn identity is missing from the historical registry')
    reserved = artifact.get('reserved_display_ids', [])
    if any(not isinstance(item, dict) or re.fullmatch(r'REQ-\d+-S\d+', str(item.get('id', ''))) is None for item in reserved):
        raise SourceItemDecompositionError('Historical reserved Snn identity is malformed')
    reserved_ids = {item['id'] for item in reserved}
    for source_id, children in artifact['records'].items():
        packet = build_source_item_decomposition_packet(baseline, source_id, table_previews=table_previews)
        if len(children) < 2:
            raise SourceItemDecompositionError('A stored split must retain at least two children')
        decision = _stored_decision(packet, children, artifact['validation_receipts'][source_id]['status'])
        # Re-run source coverage and invention checks, not merely a re-signable hash.
        rebuilt = compile_source_item_decomposition(baseline, [decision], table_previews=table_previews)
        for child in children:
            if (child.get('source_requirement_id') != packet['requirement_display_id']
                    or child.get('source_display_id') != packet['source_display_id']
                    or child.get('source_item_id') != source_id
                    or child.get('relation') != 'DECOMPOSED_FROM_SOURCE'
                    or child.get('authority') != 'AGENT_WORKING_PROPOSAL'
                    or not str(child.get('requirement_display_id', '')).startswith(packet['requirement_display_id'] + '-S')
                    or child.get('requirement_display_id') in reserved_ids):
                raise SourceItemDecompositionError('Stored child source identity or authority changed')
            if child['parent_evidence_sha256'] != packet['parent_evidence_sha256']:
                raise SourceItemDecompositionError('Decomposition parent evidence changed')
            fresh = next((r for r in rebuilt['records'][source_id]
                          if r['singular_requirement_id'] == child['singular_requirement_id']), None)
            if fresh is None or any(child.get(key) != fresh.get(key) for key in (
                    'text', 'source_item_id', 'source_ranges', 'source_excerpts', 'parent_context',
                    'shared_source_context', 'group_logic', 'requirement_kinds', 'derivation_type')):
                raise SourceItemDecompositionError('Stored child content or evidence was altered after compilation')
            registered = artifact.get('identity_registry', {}).get(source_id, {}).get(child['singular_requirement_id'])
            if registered is not None and child['requirement_display_id'] != registered:
                raise SourceItemDecompositionError('Stored child does not match its stable Snn identity')
    return artifact


def _preview_index(table_previews):
    if isinstance(table_previews, list):
        result = {value['logical_table_scope_id']: value for value in table_previews}
        if len(result) != len(table_previews):
            raise SourceItemDecompositionError('Table preview scopes are duplicated')
        return result
    if table_previews is None:
        return {}
    if not isinstance(table_previews, dict):
        raise SourceItemDecompositionError('Table previews must be a scope mapping or list')
    return table_previews


def _map_rebind_context_changes(previous, current, receipt, previous_previews, previews):
    """Validate the shared Map-rebind receipt at this downstream trust boundary."""
    if not isinstance(receipt, dict) or receipt.get('schema_version') != 'source-item-document-map-rebind-receipt/v1':
        raise SourceItemDecompositionError('Cross-Map carry requires the shared Map rebind receipt')
    expected_hash = canonical_json_sha256({k: v for k, v in receipt.items() if k != 'receipt_sha256'})
    if receipt.get('receipt_sha256') != expected_hash:
        raise SourceItemDecompositionError('Map rebind receipt hash changed')
    from app.application.agent_workflow.document_map_revision import map_rebind_added_source_ids
    try:
        added_ids = map_rebind_added_source_ids(receipt, current_baseline=current)
    except ValueError as exc:
        raise SourceItemDecompositionError('Map text recovery proof is invalid') from exc
    expected = {
        'source_sha256': previous.source_sha256,
        'previous_document_map_artifact_id': previous.document_map_artifact_id,
        'previous_document_map_sha256': previous.document_map_artifact_sha256,
        'document_map_artifact_id': current.document_map_artifact_id,
        'document_map_sha256': current.document_map_artifact_sha256,
        'previous_baseline_artifact_id': previous.artifact_id,
        'previous_baseline_sha256': previous.artifact_sha256,
        'baseline_artifact_id': current.artifact_id,
        'baseline_sha256': current.artifact_sha256,
        'preserved_source_item_ids': [r.source_record_id for r in previous.records],
        'preserved_src_ids': [r.source_display_id for r in previous.records],
        'preserved_req_ids': [r.requirement_display_id for r in previous.records if r.requirement_display_id],
        'source_display_high_watermark': previous.source_display_high_watermark,
        'table_previews_sha256': canonical_json_sha256(previous_previews),
        'record_count': len(previous.records) + len(added_ids),
        'human_acceptance': 'NOT_CLAIMED',
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise SourceItemDecompositionError('Map rebind receipt source, Map, baseline or preserved identity binding differs')
    if (receipt.get('classification_unchanged') is not True
            or receipt.get('original_bindings_unchanged') is not True
            or type(receipt.get('record_count')) is not int
            or any(re.fullmatch(r'[0-9a-f]{64}', str(receipt.get(key, ''))) is None
                   for key in ('previous_validation_receipt_sha256', 'validation_receipt_sha256'))
            or current.predecessor_artifact_id != previous.artifact_id
            or current.predecessor_artifact_sha256 != previous.artifact_sha256
            or current.revision != previous.revision + 1):
        raise SourceItemDecompositionError('Map rebind receipt is not a source-preserving direct successor')
    before = {r.source_record_id: r for r in previous.records}
    after = {r.source_record_id: r for r in current.records}
    retained = [r for r in current.records if r.source_record_id not in added_ids]
    from app.application.agent_workflow.source_requirement_baseline import validate_source_identity_allocation
    allocation_floor = validate_source_identity_allocation(previous, receipt.get('source_identity_allocation'))
    expected_watermark = (receipt.get('final_source_display_high_watermark') if added_ids
                          else allocation_floor if allocation_floor is not None else previous.source_display_high_watermark)
    if (list(before) != [r.source_record_id for r in retained]
            or set(after) != set(before) | added_ids or set(before) & added_ids
            or [r.source_display_id for r in retained] != expected['preserved_src_ids']
            or [r.requirement_display_id for r in retained if r.requirement_display_id] != expected['preserved_req_ids']
            or current.source_display_high_watermark != expected_watermark):
        raise SourceItemDecompositionError('Map rebind did not preserve the registered source/REQ identities')
    if added_ids:
        from app.application.agent_workflow.source_requirement_baseline import _artifact_text_positions, _revision_identity, SourceItemScopeRevisionReceiptV1
        recovery = receipt['text_scope_recovery']
        added = [after[sid] for sid in recovery['added_source_item_ids']]
        identities = {item.source_record_id: item for item in SourceItemScopeRevisionReceiptV1.model_validate(recovery['scope_receipt']).created_identities}
        positions = {(c.content_id, offset) for c in previous.text_context_dispositions for offset in range(c.start_offset, c.end_offset)}
        if (_artifact_text_positions(previous) != _artifact_text_positions(current)
                or canonical_json_sha256(sorted(_artifact_text_positions(current))) != recovery['source_text_positions_sha256']
                or [r.source_display_id for r in added] != recovery['added_src_ids']
                or [r.requirement_display_id for r in added if r.requirement_display_id] != recovery['added_req_ids']
                or any(_revision_identity(r) != identities[r.source_record_id] for r in added)
                or any(not r.text_bindings or r.table_fragments or r.image_bindings
                       or not {(b.content_id, offset) for b in r.text_bindings for offset in range(b.start_offset, b.end_offset)} <= positions for r in added)):
            raise SourceItemDecompositionError('Recovered text identity or original structure-span evidence differs')
        ignored = {'document_map_artifact_id', 'document_map_artifact_sha256', 'node_id', 'ancestor_path', 'logical_table_scope_id', 'source_order'}
        if any(old.model_dump(exclude=ignored) != after[sid].model_dump(exclude=ignored) for sid, old in before.items()):
            raise SourceItemDecompositionError('Map text recovery changed an existing source item')
    scopes = receipt.get('table_scope_successors')
    if (not isinstance(scopes, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in scopes.items())
            or set(scopes) != set(previous_previews)
            or len(set(scopes.values())) != len(scopes) or set(scopes.values()) != set(previews)):
        raise SourceItemDecompositionError('Map rebind table scope successors do not match old/new previews')
    rebound = []
    path_changes = set()
    for source_id, old in before.items():
        new = after[source_id]
        if old.logical_table_scope_id and new.logical_table_scope_id != scopes.get(old.logical_table_scope_id):
            raise SourceItemDecompositionError('Map rebind source table scope mapping differs')
        if old.ancestor_path != new.ancestor_path:
            path_changes.add(source_id)
        if (old.node_id, old.ancestor_path, old.logical_table_scope_id) != (new.node_id, new.ancestor_path, new.logical_table_scope_id):
            rebound.append(source_id)
    if receipt.get('rebound_source_item_ids') != rebound:
        raise SourceItemDecompositionError('Map rebind receipt owner/ancestry changes differ')
    changed = receipt.get('context_changed_source_item_ids')
    nodes = receipt.get('changed_node_ids')
    if (not isinstance(changed, list) or any(not isinstance(sid, str) for sid in changed)
            or len(set(changed)) != len(changed) or not set(changed) <= set(before)
            or not path_changes <= set(changed)
            or not isinstance(nodes, list) or any(not isinstance(node, str) for node in nodes)
            or len(set(nodes)) != len(nodes)):
        raise SourceItemDecompositionError('Map rebind receipt context changes are invalid or incomplete')
    return set(changed)


def _table_rebind_evidence(baseline, source_id, previews):
    """Compare exact owned/shared cell geometry without changing packet v1."""
    record = next(r for r in baseline.records if r.source_record_id == source_id)
    if not record.table_fragments:
        return None
    preview = previews.get(record.logical_table_scope_id)
    if not preview:
        raise SourceItemDecompositionError('Missing same-source table facts')
    ids = set(record.shared_header_cell_ids) | set(record.shared_table_cell_ids)
    ids.update(cid for binding in record.table_fragments for cid in binding.cell_ids)
    result = []
    for fragment in preview['fragments']:
        cells = [cell for cell in fragment['cells'] if cell.get('cell_id', cell.get('id')) in ids]
        if not cells:
            continue
        result.append({key: deepcopy(fragment.get(key)) for key in
                       ('fragment_id', 'table_token', 'page', 'row_range', 'row_count', 'column_count', 'bbox', 'source_bounds')})
        result[-1]['cells'] = [{key: deepcopy(cell.get(key)) for key in
                               ('cell_id', 'id', 'row', 'column', 'row_span', 'column_span', 'text', 'bbox')} for cell in cells]
    return result


def rebind_source_item_decomposition(previous_baseline, baseline, artifact, *, table_previews=None,
                                     previous_table_previews=None, map_revision_receipt=None):
    """Carry unchanged source judgments across a baseline or verified Map revision.

    A changed parent is explicitly stale, never silently rewritten. Historical
    child numbers remain reserved even when their parent is no longer analyzable.
    Cross-Map callers supply the exact shared Map-rebind receipt and old previews;
    table_previews always describes the successor. Context changes force review.
    This is a compiler operation, not a new Agent or human decision.
    """
    previous_baseline = SourceRequirementBaselineArtifactV1.model_validate(previous_baseline)
    baseline = SourceRequirementBaselineArtifactV1.model_validate(baseline)
    if previous_baseline.source_sha256 != baseline.source_sha256:
        raise SourceItemDecompositionError('Decomposition carry requires the same original source')
    map_changed = (previous_baseline.document_map_artifact_id, previous_baseline.document_map_artifact_sha256) != (
        baseline.document_map_artifact_id, baseline.document_map_artifact_sha256)
    previews = _preview_index(table_previews)
    if map_changed and previous_table_previews is None and any(r.table_fragments for r in previous_baseline.records):
        raise SourceItemDecompositionError('Cross-Map carry requires previous_table_previews for the original tables')
    old_previews = _preview_index(previous_table_previews) if previous_table_previews is not None else ({} if map_changed else previews)
    context_changed = set()
    if map_changed:
        context_changed = _map_rebind_context_changes(previous_baseline, baseline, map_revision_receipt, old_previews, previews)
    elif map_revision_receipt is not None:
        raise SourceItemDecompositionError('A Map rebind receipt cannot authorize an unrelated same-Map revision')
    validate_source_item_decomposition(previous_baseline, artifact, table_previews=old_previews)
    if baseline.artifact_sha256 == previous_baseline.artifact_sha256 and old_previews == previews:
        return {'artifact': deepcopy(artifact), 'retained_parent_ids': sorted(artifact['records']),
                'stale_parent_ids': sorted(set(artifact.get('carry_forward', {}).get('stale_parent_ids', [])))}
    retained, stale, receipts = {}, set(artifact.get('carry_forward', {}).get('stale_parent_ids', [])), {}
    for source_id, children in artifact['records'].items():
        before = build_source_item_decomposition_packet(previous_baseline, source_id, table_previews=old_previews)
        if source_id in context_changed or source_id in stale:
            stale.add(source_id)
            continue
        try:
            after = build_source_item_decomposition_packet(baseline, source_id, table_previews=previews)
            same_table_evidence = _table_rebind_evidence(previous_baseline, source_id, old_previews) == _table_rebind_evidence(baseline, source_id, previews)
        except SourceItemDecompositionError:
            stale.add(source_id)
            continue
        if ({key: value for key, value in before.items() if key not in {'baseline_sha256', 'source_form'}}
                != {key: value for key, value in after.items() if key not in {'baseline_sha256', 'source_form'}}
                or not same_table_evidence):
            stale.add(source_id)
            continue
        retained[source_id] = deepcopy(children)
        # Existing semantic validation is rerun below. The validation receipt is
        # regenerated against the new baseline lineage, not attributed to a model.
        decision = _stored_decision(after, children, artifact['validation_receipts'][source_id]['status'])
        rebuilt = compile_source_item_decomposition(baseline, [decision], table_previews=previews)
        receipts[source_id] = rebuilt['validation_receipts'][source_id]
    registry = deepcopy(artifact.get('identity_registry', {}))
    for source_id, children in artifact['records'].items():
        registry.setdefault(source_id, {}).update({c['singular_requirement_id']: c['requirement_display_id'] for c in children})
    retired = set(artifact.get('retired_singular_ids', []))
    for source_id in stale:
        retired.update(child['singular_requirement_id'] for child in artifact['records'].get(source_id, []))
    current_ids = {r.source_record_id for r in baseline.records if r.requirementness is Requirementness.REQUIREMENT}
    values = {**deepcopy(artifact), 'baseline_sha256': baseline.artifact_sha256,
              'revision': artifact['revision'] + 1, 'predecessor_sha256': artifact['artifact_sha256'],
              'records': retained, 'validation_receipts': receipts, 'identity_registry': registry,
              'kept_source_ids': [sid for sid in artifact.get('kept_source_ids', []) if sid in current_ids],
              'retired_singular_ids': sorted(retired), 'human_accepted': False,
              'status': 'PARTIAL' if stale or any(r['status'] == 'PARTIAL' for r in receipts.values()) else 'COMPLETE',
              'carry_forward': {'operation': 'SOURCE_VALIDATED_REBIND', 'stale_parent_ids': sorted(stale),
                               'retained_parent_ids': sorted(retained),
                               'from_baseline_sha256': previous_baseline.artifact_sha256}}
    if map_changed:
        values['carry_forward'].update(map_revision_receipt_sha256=map_revision_receipt['receipt_sha256'],
            from_document_map_sha256=previous_baseline.document_map_artifact_sha256,
            context_changed_source_item_ids=sorted(context_changed))
    if 'dispositions' in artifact:
        carried = {}
        for sid, disposition in artifact['dispositions'].items():
            if sid not in current_ids or sid in context_changed or sid in stale:
                continue
            try:
                before = build_source_item_decomposition_packet(previous_baseline, sid, table_previews=old_previews)
                after = build_source_item_decomposition_packet(baseline, sid, table_previews=previews)
                same = ({k: v for k, v in before.items() if k not in {'baseline_sha256', 'source_form'}}
                        == {k: v for k, v in after.items() if k not in {'baseline_sha256', 'source_form'}})
            except SourceItemDecompositionError:
                same = False
            if same:
                carried[sid] = {**deepcopy(disposition), 'baseline_sha256': baseline.artifact_sha256}
            else:
                stale.add(sid)
        values['dispositions'] = carried
        values['kept_source_ids'] = [sid for sid in values['kept_source_ids'] if sid in carried]
        values['carry_forward']['stale_parent_ids'] = sorted(stale)
        authorized = set(artifact['review_scope']['authorized_source_item_ids']) & current_ids
        values['review_scope'] = decomposition_review_scope(baseline, authorized, carried, stale, receipts)
        if not values['review_scope']['complete'] or stale:
            values['status'] = 'PARTIAL'
    values = {key: value for key, value in values.items() if key not in {'artifact_id', 'artifact_sha256'}}
    digest = canonical_json_sha256(values)
    rebound = {**values, 'artifact_id': 'source-item-decomposition-' + digest[:32], 'artifact_sha256': digest}
    validate_source_item_decomposition(baseline, rebound, table_previews=previews)
    return {'artifact': rebound, 'retained_parent_ids': sorted(retained), 'stale_parent_ids': sorted(stale)}
