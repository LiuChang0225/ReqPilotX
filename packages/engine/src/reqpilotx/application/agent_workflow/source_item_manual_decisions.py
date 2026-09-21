"""Explicit user working opinions over unchanged SourceItem/Snn evidence.

This module never infers an engineer decision. Native tools may carry existing
opinions, but new/withdrawn opinions require the separate user input boundary.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re

from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256

DECISIONS = frozenset({'COMPLIANT', 'PARTIALLY_COMPLIANT', 'NON_COMPLIANT', 'PENDING_EVIDENCE', 'NEEDS_CLARIFICATION', 'NOT_APPLICABLE'})
ORIGINS = frozenset({'USER_INPUT', 'USER_PROVIDED_FILE'})
TEXT_LIMITS = {'reason': 8000, 'conditions': 4000, 'evidence_requests': 4000}


class ManualDecisionError(ValueError):
    pass


def _seal(value, field):
    body = {k: v for k, v in value.items() if k != field}
    return {**body, field: canonical_json_sha256(body)}


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def validate_manual_context(value):
    if (not isinstance(value, dict) or value.get('schema_version') != 'source-item-manual-context/v1'
            or not _sha(value.get('source_sha256')) or not _sha(value.get('baseline_sha256'))
            or value.get('context_sha256') != _seal(value, 'context_sha256')['context_sha256']
            or not isinstance(value.get('units'), dict)):
        raise ManualDecisionError('MANUAL_CONTEXT_INVALID')
    for uid, unit in value['units'].items():
        if (unit.get('unit_id') != uid or not unit.get('source_item_id')
                or not unit.get('requirement_display_id') or not _sha(unit.get('unit_context_sha256'))):
            raise ManualDecisionError('MANUAL_UNIT_CONTEXT_INVALID')
    return value


def build_manual_decision_context(document_map, baseline, table_previews, *, decomposition=None, annotations=None):
    from reqpilotx.application.agent_workflow.source_item_analysis import build_source_item_analysis_packet, _unit_carry_semantics
    catalog = {'schema_version': 'project-knowledge-facts/v1', 'project_id': 'source-manual-context',
        'knowledge_base_version': 0, 'documents': [], 'evidence_catalog': {}, 'gaps': []}
    packet = build_source_item_analysis_packet(document_map, baseline, table_previews,
        _seal(catalog, 'catalog_sha256'), decomposition=decomposition, image_annotations=annotations)

    def original_identity(value):
        # Logical scope/node names are presentation identities. Real evidence,
        # source text, classifications, parent context and image readings remain.
        if isinstance(value, dict):
            return {k: original_identity(v) for k, v in value.items()
                    if k not in {'logical_table_scope_id', 'table_scope_id', 'scope_id'}}
        if isinstance(value, list):
            return [original_identity(v) for v in value]
        return value

    units = {}
    for unit in packet['units']:
        uid = unit['unit_id']
        semantics = original_identity(_unit_carry_semantics(unit, allow_map_identity_change=True))
        units[uid] = {'unit_id': uid, 'source_item_id': unit['source_item_id'],
            'requirement_display_id': unit['requirement_display_id'],
            'unit_context_sha256': canonical_json_sha256({'source_sha256': packet['source_sha256'], 'unit': semantics})}
    return _seal({'schema_version': 'source-item-manual-context/v1', 'source_sha256': packet['source_sha256'],
        'baseline_sha256': packet['baseline_sha256'], 'units': units}, 'context_sha256')


def _record_id(record):
    authored = {k: v for k, v in record.items() if k not in {'record_id', 'state', 'stale_reason'}}
    return 'manual-response-' + canonical_json_sha256(authored)[:32]


def validate_manual_decisions(artifact, context=None):
    if (not isinstance(artifact, dict) or artifact.get('schema_version') != 'source-item-manual-decisions/v1'
            or artifact.get('artifact_sha256') != _seal(artifact, 'artifact_sha256')['artifact_sha256']
            or type(artifact.get('revision')) is not int or artifact['revision'] < 1
            or not isinstance(artifact.get('records'), dict) or not isinstance(artifact.get('history'), list)
            or artifact.get('human_acceptance') != 'NOT_CLAIMED'):
        raise ManualDecisionError('MANUAL_ARTIFACT_INVALID')
    for key in ('source_sha256', 'baseline_sha256', 'context_sha256'):
        if not _sha(artifact.get(key)):
            raise ManualDecisionError('MANUAL_ARTIFACT_BINDING_INVALID')
    if artifact.get('predecessor_sha256') and not _sha(artifact['predecessor_sha256']):
        raise ManualDecisionError('MANUAL_PREDECESSOR_INVALID')
    for uid, record in artifact['records'].items():
        if record.get('unit_id') != uid:
            raise ManualDecisionError('MANUAL_RECORD_IDENTITY_INVALID')
    for record in [*artifact['records'].values(), *artifact['history']]:
        if (record.get('record_id') != _record_id(record) or record.get('origin') not in ORIGINS
                or record.get('state') not in {'CURRENT', 'STALE', 'WITHDRAWN'}
                or record.get('action') not in {'SET', 'WITHDRAW'}
                or not record.get('unit_id') or not record.get('source_item_id')
                or any(not _sha(record.get(k)) for k in ('source_sha256', 'baseline_sha256', 'unit_context_sha256'))):
            raise ManualDecisionError('MANUAL_RECORD_INVALID')
        if record['action'] == 'SET' and record.get('decision') not in DECISIONS:
            raise ManualDecisionError('MANUAL_DECISION_INVALID')
        if record['action'] == 'WITHDRAW' and record['state'] != 'WITHDRAWN':
            raise ManualDecisionError('MANUAL_WITHDRAWAL_INVALID')
        for field, limit in TEXT_LIMITS.items():
            if not isinstance(record.get(field), str) or len(record[field]) > limit:
                raise ManualDecisionError('MANUAL_TEXT_INVALID: ' + field)
        try:
            stamp = datetime.fromisoformat(record['decided_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None: raise ValueError('timezone required')
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ManualDecisionError('MANUAL_TIMESTAMP_INVALID') from exc
    if context is not None:
        validate_manual_context(context)
        if any(artifact[k] != context[k] for k in ('source_sha256', 'baseline_sha256', 'context_sha256')):
            raise ManualDecisionError('MANUAL_CONTEXT_REBIND_REQUIRED')
        for uid, record in artifact['records'].items():
            if record['state'] == 'CURRENT':
                unit = context['units'].get(uid)
                if (unit is None or unit['source_item_id'] != record['source_item_id']
                        or unit['unit_context_sha256'] != record['unit_context_sha256']):
                    raise ManualDecisionError('MANUAL_CURRENT_CONTEXT_DRIFT')
    return artifact


def _next(previous, context, records, history, operation):
    value = {'schema_version': 'source-item-manual-decisions/v1', 'revision': (previous or {}).get('revision', 0) + 1,
        'predecessor_sha256': (previous or {}).get('artifact_sha256', ''),
        'source_sha256': context['source_sha256'], 'baseline_sha256': context['baseline_sha256'],
        'context_sha256': context['context_sha256'], 'records': records, 'history': history,
        'operation': operation, 'human_acceptance': 'NOT_CLAIMED'}
    return validate_manual_decisions(_seal(value, 'artifact_sha256'), context)


def rebind_manual_decisions(previous, context, *, previous_context=None, require_previous_context=False):
    validate_manual_context(context)
    if previous is None:
        return None
    validate_manual_decisions(previous)
    if previous_context is not None:
        validate_manual_context(previous_context)
    records = deepcopy(previous['records']); history = deepcopy(previous['history'])
    changed = False
    for uid, record in records.items():
        if record['state'] != 'CURRENT':
            continue  # No automatic revival when source content later returns.
        unit = context['units'].get(uid)
        old_unit = previous_context['units'].get(uid) if previous_context else None
        reason = ''
        if require_previous_context and previous_context is None:
            reason = 'MISSING_PREVIOUS_CONTEXT'
        elif previous_context is not None and (old_unit is None or old_unit['unit_context_sha256'] != record['unit_context_sha256']):
            reason = 'PREVIOUS_CONTEXT_NOT_CURRENT'
        elif (unit is None or record['source_sha256'] != context['source_sha256']
              or unit['source_item_id'] != record['source_item_id'] or unit['unit_context_sha256'] != record['unit_context_sha256']):
            reason = 'SOURCE_OR_CONTEXT_CHANGED'
        if reason:
            history.append(deepcopy(record)); record.update(state='STALE', stale_reason=reason); changed = True
    if not changed and previous['context_sha256'] == context['context_sha256']:
        return deepcopy(previous)
    return _next(previous, context, records, history, 'SOURCE_REBIND')


def compile_manual_decisions(context, decisions, *, previous=None, origin, decided_at):
    validate_manual_context(context)
    if origin not in ORIGINS:
        raise ManualDecisionError('ONLY_EXPLICIT_USER_DECISIONS_CAN_BE_RECORDED')
    if not isinstance(decisions, list) or not decisions:
        raise ManualDecisionError('EXPLICIT_USER_DECISIONS_REQUIRED')
    base = rebind_manual_decisions(previous, context)
    records = deepcopy((base or {}).get('records', {})); history = deepcopy((base or {}).get('history', [])); seen = set()
    for choice in decisions:
        if not isinstance(choice, dict) or set(choice) - {'unit_id', 'action', 'decision', *TEXT_LIMITS}:
            raise ManualDecisionError('MANUAL_INPUT_FIELDS_INVALID')
        uid = choice.get('unit_id'); action = choice.get('action', 'SET')
        if uid not in context['units'] or uid in seen or action not in {'SET', 'WITHDRAW'}:
            raise ManualDecisionError('MANUAL_UNIT_NOT_CURRENT_OR_DUPLICATED')
        seen.add(uid); unit = context['units'][uid]
        if action == 'WITHDRAW' and uid not in records:
            raise ManualDecisionError('MANUAL_WITHDRAWAL_HAS_NO_PREDECESSOR')
        if action == 'SET' and (choice.get('decision') not in DECISIONS or not str(choice.get('reason', '')).strip()):
            raise ManualDecisionError('MANUAL_DECISION_AND_REASON_REQUIRED')
        record = {**unit, 'source_sha256': context['source_sha256'], 'baseline_sha256': context['baseline_sha256'],
            'action': action, 'decision': choice.get('decision', '') if action == 'SET' else '',
            **{key: choice.get(key, '') for key in TEXT_LIMITS}, 'origin': origin, 'decided_at': decided_at,
            'state': 'CURRENT' if action == 'SET' else 'WITHDRAWN', 'stale_reason': ''}
        record['record_id'] = _record_id(record)
        if uid in records: history.append(deepcopy(records[uid]))
        records[uid] = record
    # Rebinding above is an in-memory preparation, not a persisted version.
    # One user write must point directly to the actual stored predecessor.
    return _next(previous, context, records, history, 'USER_WRITE')


def manual_decision_projection(artifact, context):
    if artifact is None:
        return {}
    validate_manual_decisions(artifact, context)
    result = {}
    for uid, record in artifact['records'].items():
        if uid not in context['units']:
            continue
        value = {'record': deepcopy(record), 'state': record['state']}
        if record['state'] == 'CURRENT':
            parts = [record['reason']]
            if record['conditions']: parts.append('适用条件：' + record['conditions'])
            if record['evidence_requests']: parts.append('证据请求：' + record['evidence_requests'])
            value['manual_decision'] = {'decision': record['decision'], 'reason': '\n'.join(parts), 'decided_at': record['decided_at']}
        result[uid] = value
    return result
