"""Session-bound human responses and immutable external-task deliveries.

The shared engine owns response validation and Matrix/Excel semantics. Human
writes create a new result revision; external uploads cannot supply opinions.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import os
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from reqpilotx.application.agent_workflow.source_item_manual_decisions import (
    build_manual_decision_context, compile_manual_decisions, rebind_manual_decisions,
    validate_manual_decisions,
)
from reqpilotx_server.application.agent_workflow import plugin_workbench as work
from reqpilotx_server.application.agent_workflow.source_item_manual_decision_api import EngineerResponseChoice
from reqpilotx_server.application.agent_workflow.source_item_delivery_versions import readiness
from reqpilotx_server.application.transport import BinaryArtifact, ResponseSink

MEDIA = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class BoundWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str = Field(min_length=1, max_length=160)
    expected_source_sha256: str = Field(pattern=work.SHA)
    expected_revision_id: str = Field(pattern=work.SHA)
    expected_version: str = Field(pattern=work.SHA, description='Opaque CAS token from this session/revision response or delivery preview.')
    expected_response_revision: StrictInt = Field(ge=0)


class PluginEngineerResponse(BoundWrite):
    expected_baseline_sha256: str = Field(pattern=work.SHA)
    decisions: list[EngineerResponseChoice] = Field(min_length=1, max_length=100)


class PluginDeliveryVersion(BoundWrite):
    expected_delivery_sha256: str = Field(pattern=work.SHA)
    mode: Literal['DRAFT', 'FORMAL'] = 'DRAFT'
    accept_snapshot: bool = False


def _conflict(code):
    raise work.ApplicationError(status_code=409, detail={'code': code, 'message': '成果或响应已变化，请读取此任务的当前版本后重试。'})


def _inputs(root, head, revision=None):
    directory, version = work._version(root, head, revision)
    artifacts = {name: work._artifact(directory, version, name) for name in version['files']
                 if name in work.ARTIFACT_NAMES or name == 'manual-decisions.json'}
    mapping = work.core.DocumentMapArtifactV1.model_validate(artifacts['document-map.json'])
    baseline = (work.core.SourceRequirementBaselineArtifactV1.model_validate(artifacts['source-requirement-baseline.json'])
                if version['baseline_sha256'] else work.core.create_empty_source_requirement_baseline(mapping)[0])
    context = build_manual_decision_context(mapping, baseline, artifacts['table-previews.json'],
        decomposition=artifacts.get('source-item-decomposition.json'), annotations=artifacts.get('image-annotations.json'))
    manual = artifacts.get('manual-decisions.json')
    if manual is not None:
        validate_manual_decisions(manual, context)
    snapshot = work._artifact(directory, version, 'matrix-snapshot.json')
    if (snapshot.get('delivery_sha256') != version['snapshot_sha256']
            or snapshot.get('delivery_sha256') != work.core.canonical_json_sha256({k:v for k,v in snapshot.items() if k != 'delivery_sha256'})
            or snapshot['source_item_baseline'].get('manual_decisions_revision', 0) != (manual or {}).get('revision', 0)):
        _conflict('PLUGIN_SNAPSHOT_CHANGED')
    return version, artifacts, mapping, baseline, context, manual, snapshot


def carry_manual_responses(root, head, mapping, baseline, artifacts):
    if not head.get('revision_id'):
        return None
    _, version = work._version(root, head)
    if 'manual-decisions.json' not in version['files']:
        return None
    *_, previous_context, previous, _snapshot = _inputs(root, head)
    context = build_manual_decision_context(mapping, baseline or work.core.create_empty_source_requirement_baseline(mapping)[0],
        artifacts['table-previews.json'], decomposition=artifacts.get('source-item-decomposition.json'),
        annotations=artifacts.get('image-annotations.json'))
    return rebind_manual_decisions(previous, context, previous_context=previous_context, require_previous_context=True)


def _scope(head, version, snapshot):
    identity = {key: head[key] for key in ('project_id', 'document_id', 'session_id', 'source_sha256')}
    identity.update(revision_id=version['revision_id'], delivery_sha256=snapshot['delivery_sha256'])
    return {**identity, 'version': work.core.canonical_json_sha256(identity)}


def _view(head, version, context, manual, snapshot):
    return {'schema_version': 'source-item-engineer-response-view/v1', 'available': bool(version['baseline_sha256']),
        **_scope(head, version, snapshot), 'current_revision_id': head['revision_id'],
        'baseline_sha256': context['baseline_sha256'], 'response_revision': (manual or {}).get('revision', 0),
        'response_sha256': (manual or {}).get('artifact_sha256', ''), 'units': context['units'],
        'records': (manual or {}).get('records', {}), 'history': (manual or {}).get('history', []),
        'human_acceptance': 'NOT_CLAIMED'}


def get_plugin_engineer_responses(project_id: str, document_id: str, session_id: str, response: ResponseSink, revision: str | None = None):
    response.headers.update(work._no_store_headers())
    root, head = work._load(project_id, document_id, session_id)
    version, _, _, _, context, manual, snapshot = _inputs(root, head, revision)
    return _view(head, version, context, manual, snapshot)


def _check(head, payload, version, manual, snapshot):
    work._check_action(head, payload.expected_source_sha256, payload.expected_revision_id)
    if payload.expected_version != _scope(head, version, snapshot)['version']:
        _conflict('PLUGIN_VERSION_CHANGED_OR_WRONG_TASK')
    if payload.expected_response_revision != (manual or {}).get('revision', 0):
        _conflict('MANUAL_RESPONSE_CHANGED')
    if not version['baseline_sha256']:
        _conflict('SOURCE_ITEMS_REQUIRED')


def post_plugin_engineer_responses(project_id: str, document_id: str, session_id: str, payload: PluginEngineerResponse, response: ResponseSink):
    response.headers.update(work._no_store_headers())
    root, _ = work._load(project_id, document_id, session_id)
    request_sha = work.core.canonical_json_sha256(payload.model_dump(mode='json'))
    with work._locked(root):
        _, head = work._load(project_id, document_id, session_id)
        commands = head.setdefault('response_commands', {})
        if payload.command_id in commands:
            receipt = commands[payload.command_id]
            if receipt['request_sha256'] != request_sha:
                _conflict('MANUAL_COMMAND_REUSED_WITH_DIFFERENT_INPUT')
            version, _, _, _, context, manual, snapshot = _inputs(root, head, receipt['revision_id'])
            return _view(head, version, context, manual, snapshot)
        version, artifacts, mapping, baseline, context, previous, snapshot = _inputs(root, head)
        _check(head, payload, version, previous, snapshot)
        if context['baseline_sha256'] != payload.expected_baseline_sha256:
            _conflict('MANUAL_BASELINE_CHANGED')
        choices = []
        for choice in payload.decisions:
            unit = context['units'].get(choice.unit_id)
            if unit is None or unit['unit_context_sha256'] != choice.expected_unit_context_sha256:
                _conflict('MANUAL_UNIT_CONTEXT_CHANGED')
            choices.append(choice.model_dump(exclude={'expected_unit_context_sha256'}, exclude_none=True))
        try:
            manual = compile_manual_decisions(context, choices, previous=previous, origin='USER_INPUT', decided_at=work._now())
            snapshot = work.build_delivery_snapshot(mapping, baseline, artifacts['table-previews.json'],
                annotations=artifacts.get('image-annotations.json'), decomposition=artifacts.get('source-item-decomposition.json'),
                analysis_policy=artifacts.get('analysis-policy.json'), manual_decisions=manual)
        except ValueError as exc:
            raise work.ApplicationError(status_code=422, detail=str(exc)) from exc
        revision = work.core.canonical_json_sha256({'predecessor_revision_id': head['revision_id'],
            'request_sha256': request_sha, 'manual_decisions_sha256': manual['artifact_sha256'],
            'session_id': session_id, 'source_sha256': head['source_sha256']})
        directory = root/'versions'/revision
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        files = {}
        for name, value in {**artifacts, 'manual-decisions.json': manual, 'matrix-snapshot.json': snapshot}.items():
            work._write_json(directory/name, value)
            files[name] = sha256((directory/name).read_bytes()).hexdigest()
        saved = {**version, 'revision_id': revision, 'predecessor_revision_id': head['revision_id'],
                 'snapshot_sha256': snapshot['delivery_sha256'], 'files': files, 'created_at': work._now(),
                 'origin': 'USER_RESPONSE', 'local_review_sha256': None}
        work._write_json(directory/'version.json', saved)
        head['revision_id'] = revision
        head['versions'].append({'revision_id': revision, 'number': len(head['versions'])+1,
            'stages': saved['stages'], 'created_at': saved['created_at'], 'origin': 'USER_RESPONSE'})
        commands[payload.command_id] = {'request_sha256': request_sha, 'revision_id': revision}
        head['updated_at'] = saved['created_at']
        work._write_json(root/'head.json', head)
        work._remember_latest(root, head)
        return _view(head, saved, context, manual, snapshot)


def preview_plugin_delivery_version(project_id: str, document_id: str, session_id: str, response: ResponseSink, revision: str | None = None):
    response.headers.update(work._no_store_headers())
    root, head = work._load(project_id, document_id, session_id)
    version, _, _, baseline, _, manual, snapshot = _inputs(root, head, revision)
    return {**_scope(head, version, snapshot), 'baseline_revision': baseline.revision,
        'response_revision': (manual or {}).get('revision', 0), 'readiness': readiness(snapshot, {}),
        'snapshot': snapshot, 'current_revision_id': head['revision_id']}


def _saved_head(project_id, document_id, session_id):
    # Frozen deliveries remain readable after a replacement source is uploaded.
    work._require_project_document(project_id, document_id)
    root = work._root(project_id, document_id, session_id)
    try:
        head = work._read(root/'head.json')
    except FileNotFoundError as exc:
        raise work.ApplicationError(status_code=404, detail='未找到这个任务的交付版本。') from exc
    if any(head.get(key) != value for key, value in [('project_id', project_id), ('document_id', document_id), ('session_id', session_id)]):
        _conflict('PLUGIN_TASK_BINDING_CHANGED')
    return root, head


def _saved(root, head, revision):
    entry = next((item for item in head.get('delivery_versions', []) if item['revision'] == revision), None)
    if entry is None:
        raise work.ApplicationError(status_code=404, detail='当前任务没有这个交付版本。')
    value = work._read(root/'deliveries'/f'{revision}.json')
    if work.core.canonical_json_sha256(value) != entry['artifact_sha256']:
        _conflict('PLUGIN_DELIVERY_CHANGED')
    if any(value.get(key) != head.get(key) for key in ('project_id', 'document_id', 'session_id', 'source_sha256')):
        _conflict('PLUGIN_TASK_BINDING_CHANGED')
    snapshot = value['snapshot']
    if value['delivery_sha256'] != snapshot.get('delivery_sha256') or snapshot['delivery_sha256'] != work.core.canonical_json_sha256({k:v for k,v in snapshot.items() if k != 'delivery_sha256'}):
        _conflict('PLUGIN_SNAPSHOT_CHANGED')
    return value


def _public(value):
    return {key: value for key, value in value.items() if key != 'snapshot'}


def list_plugin_delivery_versions(project_id: str, document_id: str, session_id: str, response: ResponseSink):
    response.headers.update(work._no_store_headers())
    root, head = _saved_head(project_id, document_id, session_id)
    return {'project_id': project_id, 'document_id': document_id, 'session_id': session_id, 'source_sha256': head['source_sha256'],
        'versions': [_public(_saved(root, head, item['revision'])) for item in reversed(head.get('delivery_versions', []))]}


def save_plugin_delivery_version(project_id: str, document_id: str, session_id: str, payload: PluginDeliveryVersion, response: ResponseSink):
    response.headers.update(work._no_store_headers())
    root, _ = _saved_head(project_id, document_id, session_id)
    request_sha = work.core.canonical_json_sha256(payload.model_dump(mode='json'))
    with work._locked(root):
        _, head = _saved_head(project_id, document_id, session_id)
        commands = head.setdefault('delivery_commands', {})
        if payload.command_id in commands:
            receipt = commands[payload.command_id]
            if receipt['request_sha256'] != request_sha:
                _conflict('DELIVERY_COMMAND_REUSED_WITH_DIFFERENT_INPUT')
            return _public(_saved(root, head, receipt['revision']))
        document = work._source(project_id, document_id, head['source_sha256'])
        version, _, _, baseline, _, manual, snapshot = _inputs(root, head)
        _check(head, payload, version, manual, snapshot)
        if payload.expected_delivery_sha256 != snapshot['delivery_sha256']:
            _conflict('PLUGIN_DELIVERY_PREVIEW_CHANGED')
        ready = readiness(snapshot, {})
        if payload.mode == 'FORMAL' and (not payload.accept_snapshot or not ready['can_lock']):
            _conflict('FORMAL_REQUIRES_EFFECTIVE_RESPONSES_AND_ACCEPTANCE')
        source = Path(work.resolve_source_record_path(document)).resolve(strict=True)
        if sha256(source.read_bytes()).hexdigest() != head['source_sha256']:
            _conflict('PLUGIN_SOURCE_BYTES_CHANGED')
        revision = len(head.get('delivery_versions', [])) + 1
        data = work.export_delivery_xlsx(snapshot, source_file_path=str(source), source_sha256=head['source_sha256'],
            source_file_name=document.get('file_name') or '原始文档', export_mode=payload.mode, version_label=f'Delivery-{revision}')
        export_sha = sha256(data).hexdigest()
        directory = root/'deliveries'; directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory/(export_sha+'.xlsx')
        # Content-addressed bytes may be reused after an interrupted pre-head write.
        if target.exists():
            if sha256(target.read_bytes()).hexdigest() != export_sha:
                _conflict('PLUGIN_EXPORT_CHANGED')
        else:
            temporary = None
            try:
                with NamedTemporaryFile(dir=directory, prefix='.export-', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        value = {'schema_version': 'plugin-delivery-version/v1', **_scope(head, version, snapshot),
            'revision': revision, 'baseline_revision': baseline.revision, 'response_revision': (manual or {}).get('revision', 0),
            'mode': payload.mode, 'created_at': work._now(), 'snapshot': deepcopy(snapshot), 'readiness': ready,
            'export_sha256': export_sha, 'filename': f'ReqPilotX-Delivery-v{revision}-{payload.mode}.xlsx',
            'acceptance': {'origin': 'USER_INPUT', 'accepted_delivery_sha256': snapshot['delivery_sha256']} if payload.mode == 'FORMAL' else None}
        work._write_json(directory/f'{revision}.json', value)
        head.setdefault('delivery_versions', []).append({'revision': revision, 'artifact_sha256': work.core.canonical_json_sha256(value)})
        commands[payload.command_id] = {'request_sha256': request_sha, 'revision': revision}
        work._write_json(root/'head.json', head)
        return _public(value)


def get_plugin_delivery_version(project_id: str, document_id: str, session_id: str, revision: int, response: ResponseSink):
    response.headers.update(work._no_store_headers())
    root, head = _saved_head(project_id, document_id, session_id)
    return _saved(root, head, revision)


def download_plugin_delivery_version(project_id: str, document_id: str, session_id: str, revision: int):
    root, head = _saved_head(project_id, document_id, session_id)
    value = _saved(root, head, revision)
    data = (root/'deliveries'/(value['export_sha256']+'.xlsx')).read_bytes()
    if sha256(data).hexdigest() != value['export_sha256']:
        _conflict('PLUGIN_EXPORT_CHANGED')
    return BinaryArtifact(content=data, media_type=MEDIA, headers={**work._no_store_headers(),
        'Content-Disposition': "attachment; filename*=UTF-8''"+quote(value['filename']),
        'X-ReqPilot-Revision': value['revision_id'], 'X-ReqPilot-Snapshot': value['delivery_sha256'],
        'X-ReqPilotX-Delivery-SHA256': value['delivery_sha256'], 'X-ReqPilotX-Export-SHA256': value['export_sha256']})
