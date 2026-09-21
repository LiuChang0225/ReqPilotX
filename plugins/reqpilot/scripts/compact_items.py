"""Retain structure already established by Map through the existing Items contract.

This adapter supplies source selections only. Item identities, classification and
business grouping remain explicit decisions, and the existing binder validates
the expanded result before it can leave this module.
"""
from __future__ import annotations

from copy import deepcopy

from reqpilotx.application.agent_workflow import source_requirement_baseline as core


def _requests(document_map, scope_requests):
    """Check every request before using any of its structural evidence."""
    mapping = core.DocumentMapArtifactV1.model_validate(document_map)
    texts = [core.SourceItemTextScopeRequestV1.model_validate(value)
             for value in scope_requests['text_requests']]
    tables = [core.SourceRequirementScopeRequestV1.model_validate(value)
              for value in scope_requests['table_requests']]
    ids = [value.scope_id for value in texts] + [value.logical_table_scope_id for value in tables]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate scope requests')
    for request in [*texts, *tables]:
        if (request.source_sha256, request.document_map_artifact_id,
                request.document_map_artifact_sha256) != (
                mapping.source_sha256, mapping.artifact_id, mapping.artifact_sha256):
            raise ValueError('Scope requests belong to a different source or Map')
    for request in texts:
        expected = core.build_source_item_text_scope_request(
            mapping, node_id=request.node_id)
        if request != expected:
            raise ValueError('Text scope request content or structure differs from Map: ' + request.scope_id)
    for request in tables:
        expected = core.build_source_requirement_scope_request(mapping, {
            'logical_table_scope_id': request.logical_table_scope_id,
            'fragments': [fragment.model_dump(mode='json') for fragment in request.fragments],
        })
        table = next(table for table in mapping.logical_tables
                     if table.scope_id == request.logical_table_scope_id)
        fragment_fields = ('table_token', 'page', 'row_range', 'native_locator', 'source_sequence')
        fragments_match = len(request.fragments) == len(table.fragments) and all(
            all(getattr(actual, field) == getattr(mapped, field) for field in fragment_fields)
            and actual.row_count == mapped.row_range[1] - mapped.row_range[0]
            for actual, mapped in zip(request.fragments, table.fragments))
        if request != expected or not fragments_match:
            raise ValueError('Table scope request content or structure differs from Map: '
                             + request.logical_table_scope_id)
    return mapping, texts, tables


def _gaps(length, covered):
    """Subtract selections without repairing, merging or removing explicit input."""
    cursor = 0
    for lo, hi in sorted(covered):
        if lo > cursor:
            yield cursor, lo
        cursor = max(cursor, hi)
    if cursor < length:
        yield cursor, length


def expand_structure_retention(value: dict, document_map: dict, scope_requests: dict,
                               *, covered_scope_ids=()) -> dict:
    """Expand optional ``retain_map_structure`` without mutating inputs or artifacts.

    Unselected ordinary body scopes remain pending. Explicit decisions keep their
    exact input representation, including short selectors. A title fragment must
    still satisfy the established core title validator; arbitrary leftover title
    substrings do not acquire new semantic permission here.
    """
    # The binder imports this adapter on demand, so transport imports stay local.
    from task_contracts import ItemDecisionsRequest, _text_span, bind_item_decisions

    decisions = deepcopy(value)
    enabled = decisions.pop('retain_map_structure', False)
    if type(enabled) is not bool:
        raise ValueError('retain_map_structure must be a boolean')
    typed = ItemDecisionsRequest.model_validate(decisions)
    receipt = {
        'schema_version': 'reqpilot-items-structure-retention-receipt/v1',
        'enabled': enabled,
        'source_sha256': document_map['source_sha256'],
        'document_map_artifact_sha256': document_map['artifact_sha256'],
        'added_text_scope_ids': [],
        'added_table_scope_ids': [],
        'added_structure_only_spans': [],
        'added_context_row_groups': [],
        'pending_text_scope_ids': [],
        'pending_table_scope_ids': [],
        'already_covered_scope_ids': sorted(set(covered_scope_ids)),
        'added_text_scope_count': 0,
        'added_table_scope_count': 0,
        'added_structure_only_span_count': 0,
        'added_context_row_group_count': 0,
        'semantic_verification': 'NOT_RUN',
    }
    if not enabled:
        bind_item_decisions(decisions, document_map, scope_requests)
        return {'decisions': decisions, 'receipt': receipt}

    mapping, texts, tables = _requests(document_map, scope_requests)
    known_text = {request.scope_id for request in texts}
    known_tables = {request.logical_table_scope_id for request in tables}
    for choice in typed.text_decisions:
        if choice.scope_id not in known_text:
            raise ValueError('Unknown selected text scope: ' + choice.scope_id)
    for choice in typed.table_decisions:
        if choice.logical_table_scope_id not in known_tables:
            raise ValueError('Unknown selected table scope: ' + choice.logical_table_scope_id)
    decisions.setdefault('text_decisions', [])
    decisions.setdefault('table_decisions', [])
    text_choices = {choice['scope_id']: choice for choice in decisions['text_decisions']}
    typed_texts = {choice.scope_id: choice for choice in typed.text_decisions}
    table_choices = {choice['logical_table_scope_id']: choice for choice in decisions['table_decisions']}
    typed_tables = {choice.logical_table_scope_id: choice for choice in typed.table_decisions}
    nodes = {node.node_id: node for node in mapping.nodes}
    covered_scopes = set(covered_scope_ids)

    for request in texts:
        supplied = request.scope_id in text_choices
        if not supplied and request.scope_id in covered_scopes:
            continue
        whole_front_matter = bool(request.structural_role) and (
            request.source_content == nodes[request.node_id].source_content)
        pure_title = all(content.is_title for content in request.source_content)
        if not supplied and not (whole_front_matter or pure_title):
            receipt['pending_text_scope_ids'].append(request.scope_id)
            continue
        choice = text_choices.get(request.scope_id)
        if choice is None:
            choice = {'scope_id': request.scope_id, 'items': [], 'structure_only_spans': []}
            decisions['text_decisions'].append(choice)
            receipt['added_text_scope_ids'].append(request.scope_id)
        covered = {content.content_id: [] for content in request.source_content}
        explicit = typed_texts.get(request.scope_id)
        if explicit is not None:
            selections = [span for item in explicit.items for span in item.text_spans]
            selections.extend(part.text_span for part in explicit.structure_only_spans)
            for selection in selections:
                span = _text_span(selection, request)
                if span.content_id not in covered:
                    raise ValueError('SOURCE_ITEM_TEXT_CONTENT_UNKNOWN: ' + span.content_id)
                content = next(content for content in request.source_content
                               if content.content_id == span.content_id)
                if span.end_offset > len(content.text):
                    raise ValueError('SOURCE_ITEM_TEXT_SPAN_OUT_OF_RANGE')
                covered[span.content_id].append((span.start_offset, span.end_offset))
        for content in request.source_content:
            if not (whole_front_matter or content.is_title):
                continue
            if not content.text.strip():
                raise ValueError('SOURCE_ITEM_TEXT_SPAN_EMPTY: ' + request.scope_id)
            for lo, hi in _gaps(len(content.text), covered[content.content_id]):
                if not content.text[lo:hi].strip():
                    continue
                span = {'content_id': content.content_id, 'start_offset': lo, 'end_offset': hi}
                choice.setdefault('structure_only_spans', []).append({
                    'text_span': span,
                    'reason_codes': ['whole_front_matter' if whole_front_matter else 'source_heading'],
                })
                receipt['added_structure_only_spans'].append({'scope_id': request.scope_id, **span})

    for request in tables:
        supplied = request.logical_table_scope_id in table_choices
        if not supplied and request.logical_table_scope_id in covered_scopes:
            continue
        if not request.structural_role:
            if not supplied:
                receipt['pending_table_scope_ids'].append(request.logical_table_scope_id)
            continue
        choice = table_choices.get(request.logical_table_scope_id)
        if choice is None:
            choice = {'logical_table_scope_id': request.logical_table_scope_id,
                      'records': [], 'context_row_groups': []}
            decisions['table_decisions'].append(choice)
            receipt['added_table_scope_ids'].append(request.logical_table_scope_id)
        covered = {fragment.fragment_id: [] for fragment in request.fragments}
        explicit = typed_tables.get(request.logical_table_scope_id)
        if explicit is not None:
            selections = [group for record in explicit.records for group in record.row_groups]
            selections.extend(context.row_group for context in explicit.context_row_groups)
            for group in selections:
                if group.fragment_ref is not None:
                    if group.fragment_ref >= len(request.fragments):
                        raise ValueError('fragment_ref is outside this selected table scope')
                    identifier = request.fragments[group.fragment_ref].fragment_id
                else:
                    identifier = group.fragment_id
                if identifier not in covered:
                    raise ValueError('SOURCE_REQUIREMENT_FRAGMENT_UNKNOWN: ' + identifier)
                fragment = next(fragment for fragment in request.fragments if fragment.fragment_id == identifier)
                if group.row_end > fragment.row_count:
                    raise ValueError('SOURCE_REQUIREMENT_ROW_OUT_OF_RANGE')
                covered[identifier].append((group.row_start, group.row_end))
        for fragment in request.fragments:
            for lo, hi in _gaps(fragment.row_count, covered[fragment.fragment_id]):
                group = {'fragment_id': fragment.fragment_id, 'row_start': lo, 'row_end': hi}
                choice.setdefault('context_row_groups', []).append({
                    'role': 'FRONT_MATTER', 'row_group': group, 'reason_codes': ['whole_front_matter'],
                })
                receipt['added_context_row_groups'].append({
                    'logical_table_scope_id': request.logical_table_scope_id, **group,
                })

    # No exception is suppressed: explicit overlaps, body coverage, false title
    # selections, invalid row groups and front-matter records all remain errors.
    bind_item_decisions(decisions, document_map, scope_requests)
    for count_key, entries in (
            ('added_text_scope_count', 'added_text_scope_ids'),
            ('added_table_scope_count', 'added_table_scope_ids'),
            ('added_structure_only_span_count', 'added_structure_only_spans'),
            ('added_context_row_group_count', 'added_context_row_groups')):
        receipt[count_key] = len(receipt[entries])
    return {'decisions': decisions, 'receipt': receipt}
