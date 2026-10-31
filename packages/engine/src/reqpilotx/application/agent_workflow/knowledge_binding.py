"""Frozen task evidence binding shared by the Task and Host projections.

ReqPilotX owns the task/material binding. PAIR owns originals, extraction and
retrieval. This module validates only the detached record persisted with the
task request; it never calls a provider or reads a provider data directory.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

SCHEMA = 'reqpilot-task-evidence/v1'
STATUS_SCHEMA = 'reqpilot-task-evidence-status/v1'
MODES = {'NONE', 'PROJECT_REFERENCES', 'TEAM_KNOWLEDGE', 'COMBINED'}
TEAM_KINDS = {'LIVE', 'TOPIC'}
COVERAGE = {'complete', 'partial', 'unknown'}
LOCATIONS = {'physical-page', 'logical-section'}
WIKI_STATES = {'not-requested', 'pending', 'ready', 'failed', 'cancelled'}
STATUS_STATES = {'CURRENT', 'UNAVAILABLE', 'CHANGED', 'INVALID'}
SHA = re.compile(r'^[0-9a-f]{64}$')


def canonical_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def _text(value, field, *, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError('EVIDENCE_BINDING_INVALID: ' + field)
    return value


def _sha(value, field):
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise ValueError('EVIDENCE_BINDING_INVALID: ' + field)
    return value


def _document(value):
    if not isinstance(value, dict):
        raise ValueError('EVIDENCE_BINDING_INVALID: document')
    allowed = {'kbId', 'documentId', 'name', 'mime', 'originalRevision',
               'extractionRevision', 'coverage', 'location', 'pages',
               'expectedPages', 'wiki'}
    if set(value) != allowed:
        raise ValueError('EVIDENCE_BINDING_INVALID: document fields')
    result = {
        'kbId': _text(value['kbId'], 'document.kbId'),
        'documentId': _text(value['documentId'], 'document.documentId'),
        'name': _text(value['name'], 'document.name'),
        'mime': _text(value['mime'], 'document.mime'),
        'originalRevision': _sha(value['originalRevision'], 'document.originalRevision'),
        'extractionRevision': _sha(value['extractionRevision'], 'document.extractionRevision'),
        'coverage': value['coverage'],
        'location': value['location'],
        'pages': value['pages'],
        'expectedPages': value['expectedPages'],
        'wiki': value['wiki'],
    }
    if result['coverage'] not in COVERAGE or result['location'] not in LOCATIONS:
        raise ValueError('EVIDENCE_BINDING_INVALID: document coverage/location')
    if result['wiki'] not in WIKI_STATES:
        raise ValueError('EVIDENCE_BINDING_INVALID: document wiki')
    if type(result['pages']) is not int or result['pages'] < 0:
        raise ValueError('EVIDENCE_BINDING_INVALID: document pages')
    if result['expectedPages'] is not None and (
            type(result['expectedPages']) is not int or result['expectedPages'] < 0):
        raise ValueError('EVIDENCE_BINDING_INVALID: document expectedPages')
    return result


def _role(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {
            'authority', 'claimRole', 'applicability', 'label'}:
        raise ValueError('EVIDENCE_BINDING_INVALID: role')
    result = {}
    for key in ('authority', 'claimRole', 'applicability', 'label'):
        item = value.get(key)
        if item is not None:
            result[key] = _text(item, 'role.' + key, allow_empty=True)
    return result


def _documents(values, field):
    if not isinstance(values, list):
        raise ValueError('EVIDENCE_BINDING_INVALID: ' + field)
    result = []
    seen = set()
    for item in values:
        document = _document(item)
        key = (document['kbId'], document['documentId'])
        if key in seen:
            raise ValueError('EVIDENCE_BINDING_INVALID: duplicate document')
        seen.add(key)
        result.append(document)
    return result


def validate_binding(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError('EVIDENCE_BINDING_INVALID: object')
    allowed = {'schema_version', 'mode', 'application_id', 'project_id',
               'created_at', 'project', 'team', 'binding_sha256'}
    if set(value) != allowed:
        raise ValueError('EVIDENCE_BINDING_INVALID: fields')
    if value.get('schema_version') != SCHEMA:
        raise ValueError('EVIDENCE_BINDING_INVALID: schema')
    mode = value.get('mode')
    if mode not in MODES:
        raise ValueError('EVIDENCE_BINDING_INVALID: mode')
    result = {
        'schema_version': SCHEMA,
        'mode': mode,
        'application_id': _text(value.get('application_id'), 'application_id'),
        'project_id': _text(value.get('project_id'), 'project_id'),
        'created_at': _text(value.get('created_at'), 'created_at'),
        'project': None,
        'team': None,
    }
    project = value.get('project')
    if project is not None:
        if not isinstance(project, dict) or set(project) - {
                'library_id', 'caller', 'documents', 'roles'}:
            raise ValueError('EVIDENCE_BINDING_INVALID: project')
        documents = _documents(project.get('documents'), 'project.documents')
        kb_ids = {item['kbId'] for item in documents}
        if len(kb_ids) > 1:
            raise ValueError('EVIDENCE_BINDING_INVALID: project spans libraries')
        result['project'] = {
            'library_id': _text(project.get('library_id'), 'project.library_id'),
            'caller': _text(project.get('caller'), 'project.caller'),
            'documents': documents,
        }
        roles = project.get('roles') or {}
        if not isinstance(roles, dict):
            raise ValueError('EVIDENCE_BINDING_INVALID: project.roles')
        result['project']['roles'] = {key: _role(item) for key, item in roles.items()}
    team = value.get('team')
    if team is not None:
        if not isinstance(team, dict) or set(team) - {
                'kind', 'library_id', 'caller', 'documents', 'topic', 'roles'}:
            raise ValueError('EVIDENCE_BINDING_INVALID: team')
        kind = team.get('kind')
        if kind not in TEAM_KINDS:
            raise ValueError('EVIDENCE_BINDING_INVALID: team.kind')
        result['team'] = {
            'kind': kind,
            'library_id': _text(team.get('library_id'), 'team.library_id'),
            'caller': _text(team.get('caller'), 'team.caller'),
            'roles': {key: _role(item) for key, item in (team.get('roles') or {}).items()},
        }
        if not isinstance(result['team']['roles'], dict):
            raise ValueError('EVIDENCE_BINDING_INVALID: team.roles')
        if kind == 'LIVE':
            if team.get('topic') is not None:
                raise ValueError('EVIDENCE_BINDING_INVALID: live team has topic')
            result['team']['documents'] = _documents(team.get('documents'), 'team.documents')
            if any(item['kbId'] != result['team']['library_id'] for item in result['team']['documents']):
                raise ValueError('EVIDENCE_BINDING_INVALID: team spans libraries')
        else:
            if team.get('documents') is not None:
                raise ValueError('EVIDENCE_BINDING_INVALID: topic has live documents')
            topic = team.get('topic')
            if not isinstance(topic, dict) or set(topic) - {
                    'version_id', 'label', 'review_state', 'availability', 'authors',
                    'content_digest'}:
                raise ValueError('EVIDENCE_BINDING_INVALID: team.topic')
            result['team']['topic'] = {
                'version_id': _text(topic.get('version_id'), 'team.topic.version_id'),
                'label': _text(topic.get('label', ''), 'team.topic.label', allow_empty=True),
                'review_state': _text(topic.get('review_state', ''), 'team.topic.review_state', allow_empty=True),
                'availability': _text(topic.get('availability', ''), 'team.topic.availability', allow_empty=True),
                'authors': [str(item) for item in (topic.get('authors') or [])],
            }
            if topic.get('content_digest') is not None:
                result['team']['topic']['content_digest'] = _sha(
                    topic.get('content_digest'), 'team.topic.content_digest')
    has_project = bool(result.get('project') and result['project']['documents'])
    has_team = bool(result.get('team') and (
        result['team'].get('documents') if result['team']['kind'] == 'LIVE'
        else result['team'].get('topic')))
    expected = {'NONE': (False, False), 'PROJECT_REFERENCES': (True, False),
                'TEAM_KNOWLEDGE': (False, True), 'COMBINED': (True, True)}[mode]
    if (has_project, has_team) != expected:
        raise ValueError('EVIDENCE_BINDING_INVALID: mode does not match selected evidence')
    body = deepcopy(result)
    expected_sha = canonical_digest(body)
    if value.get('binding_sha256') != expected_sha:
        raise ValueError('EVIDENCE_BINDING_INVALID: binding_sha256')
    result['binding_sha256'] = expected_sha
    return result


def binding_from_request(request) -> dict | None:
    value = (request or {}).get('knowledge_evidence')
    return validate_binding(value) if value is not None else None


def validate_status(value, binding) -> dict:
    if not isinstance(value, dict) or set(value) != {
            'schema_version', 'binding_sha256', 'task_id', 'state',
            'message', 'checked_at'}:
        raise ValueError('EVIDENCE_STATUS_INVALID: fields')
    if value.get('schema_version') != STATUS_SCHEMA:
        raise ValueError('EVIDENCE_STATUS_INVALID: schema')
    if value.get('state') not in STATUS_STATES:
        raise ValueError('EVIDENCE_STATUS_INVALID: state')
    result = {
        'schema_version': STATUS_SCHEMA,
        'binding_sha256': _sha(value.get('binding_sha256'), 'status.binding_sha256'),
        'task_id': _text(value.get('task_id'), 'status.task_id'),
        'state': value['state'],
        'message': _text(value.get('message', ''), 'status.message', allow_empty=True),
        'checked_at': _text(value.get('checked_at'), 'status.checked_at'),
    }
    if binding and result['binding_sha256'] != binding['binding_sha256']:
        raise ValueError('EVIDENCE_STATUS_INVALID: binding differs')
    return result
