"""Source-bound transport of external plugin drafts into the existing workbench.

No semantic extraction or model dispatch happens here. Immutable drafts use the
same Map, source baseline and Matrix bridge as native application tasks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field
from reqpilotx_server.adapters.agents.codex_app_server.native_loop_driver import _write_json
from reqpilotx_server.application.agent_workflow import source_requirement_baseline as core
from reqpilotx_server.application.agent_workflow.document_map_jobs import _runtime_container as _container
from reqpilotx_server.application.agent_workflow.native_structure_bridge import _require_project_document, _document_source_sha256, _no_store_headers
from reqpilotx_server.application.agent_workflow.source_item_loop_jobs import _locked
from reqpilotx_server.application.agent_workflow.source_item_delivery import build_delivery_snapshot, export_delivery_xlsx
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.source_runtime import resolve_source_record_path
from reqpilotx_server.application.transport import BinaryArtifact, ResponseSink
from reqpilotx.application.agent_workflow import fixed_workflow as workflow_contract
from reqpilotx.application.agent_workflow import source_item_analysis as analysis_core
from reqpilotx.application.agent_workflow import pair_evidence

SHA = r'^[0-9a-f]{64}$'
SESSION = r'^plugin-[0-9a-f]{32}$'
ARTIFACT_NAMES = {
    'document-map.json', 'source-requirement-baseline.json', 'validation-receipt.json',
    'table-previews.json', 'item-compilation-receipt.json', 'workflow-check.json',
    'image-annotations.json', 'source-item-decomposition.json', 'analysis-policy.json',
    'source-item-assessments.json', 'analysis-packet.json', 'knowledge-catalog.json',
    'evidence-registration.json', 'knowledge-attestation.json',
}


class PluginDraftUpload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(pattern=SESSION)
    expected_source_sha256: str = Field(pattern=SHA)
    expected_revision_id: str | None = Field(default=None, pattern=SHA)
    artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    local_review_sha256: str | None = Field(default=None, pattern=SHA)
    require_map_confirmation: bool = False
    goal: str = Field(default='', max_length=12000)
    feedback_resolutions: dict[str, str] = Field(default_factory=dict)
    fixed_workflow: dict[str, Any] | None = None


class PluginFeedback(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str = Field(min_length=1, max_length=160)
    expected_source_sha256: str = Field(pattern=SHA)
    expected_revision_id: str = Field(pattern=SHA)
    source_item_ids: list[str] = Field(default_factory=list, max_length=100)
    node_id: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1, max_length=8000)


class PluginMapConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str = Field(min_length=1, max_length=160)
    expected_source_sha256: str = Field(pattern=SHA)
    expected_map_sha256: str = Field(pattern=SHA)
    expected_revision_id: str = Field(pattern=SHA)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _root(project_id, document_id, session_id):
    if not re.fullmatch(SESSION, session_id):
        raise ApplicationError(status_code=404, detail='未找到这个插件审阅任务。')
    scope = core.canonical_json_sha256({'project_id':project_id, 'document_id':document_id})
    return _container().settings.object_root / 'runtime/plugin-workbench' / scope / session_id


def _saved_delivery(root, head, baseline):
    """Return only a retained delivery receipt for the current basis, or None."""
    if baseline is None:
        return None
    for entry in reversed(head.get('delivery_versions') or []):
        revision = entry.get('revision')
        path = root / 'deliveries' / f'{revision}.json'
        if not path.is_file():
            raise ApplicationError(status_code=409, detail='已登记的交付版本内容缺失，请保留现场并核对。')
        value = _read(path)
        if core.canonical_json_sha256(value) != entry.get('artifact_sha256'):
            raise ApplicationError(status_code=409, detail='已登记的交付版本内容发生变化，请保留现场并核对。')
        if value.get('revision_id') != head.get('revision_id'):
            continue
        if value.get('source_sha256') != head.get('source_sha256'):
            continue
        snapshot = value.get('snapshot') or {}
        if (snapshot.get('source_item_baseline') or {}).get('artifact_sha256') != baseline.artifact_sha256:
            continue
        return value
    return None


def _derive_fixed_workflow(value, source_sha, mapping, baseline, artifacts, root, head, local_review_sha256=None):
    """Derive stage status from validated uploads and retained delivery receipts.

    Sender-provided stage status and ready_to_review are deliberately ignored.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('schema') != workflow_contract.STATE_SCHEMA:
        raise ApplicationError(status_code=422, detail='固定工作流回执格式不受支持。')
    inner = value.get('workflow') or {}
    try:
        spec = workflow_contract.workflow_spec(
            brief_sha256=inner.get('brief_sha256'),
            evidence_mode=inner.get('evidence_mode', 'NONE'),
            evidence_reason='BOUND_BY_TASK')
        if inner.get('id') != spec['id'] or inner.get('version') != spec['version']:
            raise ValueError('UNSUPPORTED_WORKFLOW_VERSION')
        if inner.get('definition_sha256') != spec['definition_sha256']:
            raise ValueError('WORKFLOW_DEFINITION_CHANGED')
    except ValueError as exc:
        raise ApplicationError(status_code=422, detail='固定工作流版本或定义不兼容：' + str(exc)) from exc
    if value.get('source_sha256') != source_sha:
        raise ApplicationError(status_code=409, detail='固定工作流回执属于另一份原件。')

    delivery = _saved_delivery(root, head, baseline)
    check = artifacts.get('workflow-check.json') or {}
    check_basis = check.get('basis') or {}
    check_current = bool(check and check_basis.get('source_sha256') == source_sha
        and check_basis.get('map_sha256') == (mapping.artifact_sha256 if mapping else None)
        and check_basis.get('items_sha256') == (baseline.artifact_sha256 if baseline else None)
        and (not local_review_sha256 or check_basis.get('review_sha256') == local_review_sha256))
    check_passed = bool(check_current and check.get('status') == 'PASSED'
        and check.get('review_verified') is True and check.get('current_delivery_registered') is True
        and delivery is not None and check.get('delivery_snapshot_sha256') == delivery.get('delivery_sha256'))
    confirmed = bool(mapping and head.get('confirmed_map_sha256') == mapping.artifact_sha256)
    confirmation_required = bool(mapping and head.get('require_map_confirmation') and not confirmed)
    decomposition = artifacts.get('source-item-decomposition.json') or {}
    coverage = artifacts.get('item-compilation-receipt.json') or {}
    unprocessed = coverage.get('unprocessed_scope_ids') if isinstance(coverage.get('unprocessed_scope_ids'), list) else []
    remaining = decomposition.get('review_scope', {}).get('remaining_source_item_ids', [])
    remaining = len(remaining) if isinstance(remaining, list) else 1
    policy = artifacts.get('analysis-policy.json') or {}
    assessments = artifacts.get('source-item-assessments.json') or {}
    analysis_scope = (analysis_core.assessment_scope_status(baseline, decomposition, assessments)
                      if baseline and assessments and artifacts.get('knowledge-attestation.json') else None)
    mode = spec['evidence']['mode']
    open_items = []
    blocking = []

    stages = [{'id': 'PREPARATION', 'label': '资料准备', 'status': 'SAVED',
               'evidence': [{'kind': 'TASK_BINDING', 'source_sha256': source_sha}]}]

    if mapping:
        stages.append({'id': 'SOURCE_MAP', 'label': '来源与结构', 'status': 'SAVED',
                       'evidence': [{'kind': 'DOCUMENT_MAP', 'source_sha256': source_sha,
                                     'artifact_sha256': mapping.artifact_sha256, 'count': len(mapping.nodes)}]})
    else:
        stages.append({'id': 'SOURCE_MAP', 'label': '来源与结构', 'status': 'PENDING'})

    if not mapping:
        item_status, item_reason = 'BLOCKED', 'SOURCE_MAP_REQUIRED'; blocking.append('SOURCE_MAP_REQUIRED')
    elif baseline is None:
        item_status, item_reason = 'PENDING', 'ITEMS_NOT_SAVED'
    elif unprocessed:
        item_status, item_reason = 'IN_PROGRESS', 'UNPROCESSED_SOURCE_SCOPE'; open_items.append('UNPROCESSED_SOURCE_SCOPE')
    elif remaining:
        item_status, item_reason = 'IN_PROGRESS', 'SCOPED_DERIVATION_REMAINING'; open_items.append('SCOPED_DERIVATION_REMAINING')
    elif confirmation_required:
        item_status, item_reason = 'IN_PROGRESS', 'MAP_CONFIRMATION_REQUIRED'; open_items.append('MAP_CONFIRMATION_REQUIRED'); blocking.append('MAP_CONFIRMATION_REQUIRED')
    else:
        item_status, item_reason = 'SAVED', None
    item_evidence = ([{'kind': 'SOURCE_ITEMS', 'source_sha256': source_sha,
                       'artifact_sha256': baseline.artifact_sha256, 'count': len(baseline.records),
                       'map_sha256': mapping.artifact_sha256, 'unprocessed_scope_ids': unprocessed}]
                     if mapping and baseline is not None else None)
    if decomposition:
        item_evidence = [*(item_evidence or []), {'kind': 'DECOMPOSITION', 'source_sha256': source_sha,
                          'artifact_sha256': decomposition.get('artifact_sha256'), 'remaining': remaining}]
    stages.append({'id': 'ITEMS_CLASSIFICATION', 'label': '条目、分类与明确派生', 'status': item_status,
                   **({'evidence': item_evidence} if item_evidence else {}),
                   **({'reason': item_reason} if item_reason else {})})

    if mapping is None or baseline is None:
        evidence_status, evidence_reason = 'BLOCKED', 'ITEMS_REQUIRED'; blocking.append('ITEMS_REQUIRED')
    elif mode == 'NONE':
        evidence_status, evidence_reason = 'SKIPPED', 'NO_INTERNAL_EVIDENCE'
        open_items.append('EVIDENCE_ANALYSIS_SKIPPED')
    elif analysis_scope and analysis_scope['complete']:
        evidence_status, evidence_reason = 'SAVED', None
    elif analysis_scope and analysis_scope['required']:
        evidence_status = 'PENDING'
        evidence_reason = ('INTERNAL_EVIDENCE_ANALYSIS_PARTIAL' if analysis_scope.get('current')
                           else 'INTERNAL_EVIDENCE_ANALYSIS_REQUIRED')
        open_items.append('INTERNAL_EVIDENCE_ANALYSIS_REQUIRED')
    elif policy.get('status') == 'SKIPPED' and mode == 'NONE':
        evidence_status, evidence_reason = 'SKIPPED', policy.get('reason') or 'USER_SKIPPED'
        open_items.append('EVIDENCE_ANALYSIS_SKIPPED')
    else:
        evidence_status, evidence_reason = 'PENDING', 'INTERNAL_EVIDENCE_ANALYSIS_REQUIRED'
        open_items.append('INTERNAL_EVIDENCE_ANALYSIS_REQUIRED')
    stages.append({'id': 'EVIDENCE_ANALYSIS', 'label': '依据分析', 'status': evidence_status,
                   **({'reason': evidence_reason} if evidence_reason else {})})

    if baseline is None:
        check_status, check_reason = 'BLOCKED', 'ITEMS_REQUIRED'
        delivery_status, delivery_reason = 'BLOCKED', 'ITEMS_REQUIRED'
    elif check_passed:
        check_status, check_reason = 'SAVED', None
        delivery_status, delivery_reason = 'SAVED', None
    elif check:
        check_status, check_reason = 'BLOCKED', 'SELF_CHECK_FAILED'
        delivery_status, delivery_reason = ('SAVED', None) if delivery is not None else ('PENDING', 'DELIVERABLE_NOT_SAVED')
    else:
        check_status, check_reason = 'PENDING', 'SELF_CHECK_NOT_RUN'
        delivery_status, delivery_reason = ('SAVED', None) if delivery is not None else ('PENDING', 'DELIVERABLE_NOT_SAVED')
    check_evidence = ([{'kind': 'WORKFLOW_CHECK' if check_passed else 'DELIVERY_VERSION', 'source_sha256': source_sha,
                        'artifact_sha256': check.get('check_sha256') if check_passed else delivery.get('delivery_sha256')}]
                      if check_status == 'SAVED' else None)
    delivery_evidence = ([{'kind': 'DELIVERY_VERSION', 'source_sha256': source_sha,
                          'artifact_sha256': delivery.get('delivery_sha256')}] if delivery is not None else None)
    stages.append({'id': 'SELF_CHECK', 'label': '自查与修正', 'status': check_status,
                   **({'evidence': check_evidence} if check_evidence else {}),
                   **({'reason': check_reason} if check_reason else {})})
    stages.append({'id': 'DELIVERABLE', 'label': '可核对成果', 'status': delivery_status,
                   **({'evidence': delivery_evidence} if delivery_evidence else {}),
                   **({'reason': delivery_reason} if delivery_reason else {})})

    ready = delivery is not None and check_passed and not blocking and all(
        stage['status'] in {'SAVED', 'SKIPPED'} for stage in stages)
    next_stage = next((stage['id'] for stage in stages
                       if stage['status'] in {'PENDING', 'IN_PROGRESS', 'BLOCKED'}), None)
    return {
        'schema': workflow_contract.STATE_SCHEMA,
        'workflow': {'id': spec['id'], 'version': spec['version'],
                     'definition_sha256': spec['definition_sha256'],
                     'brief_sha256': spec['brief_sha256'], 'evidence_mode': mode},
        'source_sha256': source_sha, 'stages': stages, 'next_stage': next_stage,
        'ready_to_review': ready, 'verification': 'SAVED_ARTIFACTS_AND_DELIVERY_RECEIPTS',
        'open_items': sorted(set(open_items)), 'human_acceptance': 'NOT_CLAIMED',
    }

def _remember_latest(root,head):
    _write_json(root.parent/'latest.json',{'session_id':head['session_id'],'source_sha256':head['source_sha256']})


def _source(project_id, document_id, expected):
    document = _require_project_document(project_id, document_id)
    if _document_source_sha256(document) != expected:
        raise ApplicationError(status_code=409, detail='插件成果与当前文档的原件版本不同。')
    return document


def _load(project_id, document_id, session_id):
    root = _root(project_id, document_id, session_id)
    try:
        head = _read(root/'head.json')
    except FileNotFoundError as exc:
        raise ApplicationError(status_code=404, detail='尚未连接这个插件审阅任务。') from exc
    if (head['project_id'],head['document_id'],head['session_id']) != (project_id,document_id,session_id):
        raise ApplicationError(status_code=409, detail='插件审阅任务身份不一致。')
    _source(project_id,document_id,head['source_sha256'])
    return root,head


def _version(root, head, revision=None):
    revision = revision or head.get('revision_id')
    if not revision or not re.fullmatch(SHA,revision):
        raise ApplicationError(status_code=404, detail='尚无已保存的插件成果。')
    try:
        version = _read(root/'versions'/revision/'version.json')
    except FileNotFoundError as exc:
        raise ApplicationError(status_code=404, detail='未找到这个成果版本。') from exc
    if version['source_sha256'] != head['source_sha256'] or version['revision_id'] != revision:
        raise ApplicationError(status_code=409, detail='成果版本身份发生变化。')
    return root/'versions'/revision,version


def _artifact(directory, version, name):
    digest=version['files'].get(name)
    if not digest:
        raise ApplicationError(status_code=404, detail='当前版本尚无这项成果。')
    # Versions are immutable; replacing a file invalidates the cache identity.
    path=directory/name;stat=path.stat()
    return _cached_with_stat(str(path),digest,stat.st_mtime_ns,stat.st_size)


@lru_cache(maxsize=24)
def _cached_with_stat(path,digest,mtime,size):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:
        raise ApplicationError(status_code=409, detail='保存的成果内容发生变化，请重新同步。')
    return json.loads(raw)



def _current_fixed_workflow(root, head):
    stored = head.get('fixed_workflow')
    if not stored:
        return None
    try:
        if head.get('revision_id'):
            directory, version = _version(root, head)
            names = {name: _artifact(directory, version, name) for name in
                     ('document-map.json', 'table-previews.json') if name in version['files']}
            mapping = core.DocumentMapArtifactV1.model_validate(names['document-map.json']) if 'document-map.json' in names else None
            if mapping is None or mapping.source_sha256 != head['source_sha256']:
                raise ValueError('Map source differs from this task')
            baseline = None
            if version.get('baseline_sha256'):
                baseline = core.SourceRequirementBaselineArtifactV1.model_validate(
                    _artifact(directory, version, 'source-requirement-baseline.json'))
                if baseline.source_sha256 != head['source_sha256'] or baseline.document_map_artifact_sha256 != mapping.artifact_sha256:
                    raise ValueError('Source items differ from this Map/source')
            artifacts = {name: _artifact(directory, version, name) for name in
                         ('source-item-decomposition.json', 'analysis-policy.json',
                          'item-compilation-receipt.json', 'workflow-check.json',
                          'source-item-assessments.json', 'analysis-packet.json',
                          'knowledge-catalog.json', 'evidence-registration.json',
                          'knowledge-attestation.json')
                         if name in version['files']}
        else:
            mapping = None; baseline = None; artifacts = {}
        payload = {'schema': workflow_contract.STATE_SCHEMA, 'source_sha256': head['source_sha256'],
                   'workflow': stored.get('workflow') or {}}
        return _derive_fixed_workflow(payload, head['source_sha256'], mapping, baseline, artifacts, root, head,
                                      (version.get('local_review_sha256') if head.get('revision_id') else None))
    except (ApplicationError, ValueError, KeyError, TypeError, OSError) as exc:
        detail = exc.detail if isinstance(exc, ApplicationError) and isinstance(exc.detail, str) else str(exc)
        return {'schema': workflow_contract.STATE_SCHEMA,
                'workflow': stored.get('workflow') or {}, 'source_sha256': head.get('source_sha256'),
                'stages': [], 'next_stage': None, 'ready_to_review': False,
                'verification': 'UNCONFIRMED', 'open_items': ['WORKFLOW_STATE_UNCONFIRMED'],
                'unconfirmed': {'reason': detail}, 'human_acceptance': 'NOT_CLAIMED'}

def _public(root,head):
    stages=[];baseline=None;policy=None
    if head.get('revision_id'):
        _,version=_version(root,head)
        stages=version['stages'];baseline=version.get('baseline_sha256');policy=version.get('analysis_policy')
    map_sha=next((s['artifact_sha256'] for s in stages if s['stage']=='MAP'),None)
    confirmed=head.get('confirmed_map_sha256')==map_sha and bool(map_sha)
    def capability(enabled, effect, reason='', supported=True):
        return {'supported': supported, 'enabled': bool(enabled), 'reason': reason,
                'effect': effect, 'execution_target': 'EXTERNAL_INBOX' if effect in {'FEEDBACK_SAVED', 'MAP_CONFIRMED'} else 'PROGRAM_EXPORT',
                'target_scope_kinds': ['DOCUMENT', 'STRUCTURE_NODE', 'SOURCE_ITEM']}
    unavailable = {'supported': False, 'enabled': False, 'reason': '外部运行未报告控制能力；请在原任务中继续。',
                   'effect': 'NOT_SUPPORTED', 'execution_target': 'EXTERNAL_HOST'}
    capabilities = {'save_feedback': capability(head.get('revision_id'), 'FEEDBACK_SAVED', '尚无可绑定的成果版本。' if not head.get('revision_id') else ''),
        'confirm_map': capability(map_sha and not confirmed and head['require_map_confirmation'], 'MAP_CONFIRMED',
                                 '当前没有待确认的结构版本。' if not map_sha or confirmed else ''),
        'export_draft': capability(baseline, 'PROGRAM_EXPORT', '当前版本尚无原文条目。' if not baseline else ''),
        'save_engineer_response': capability(baseline, 'USER_RESPONSE_SAVED', '当前版本尚无原文条目。' if not baseline else ''),
        'save_delivery_version': capability(baseline, 'DELIVERY_VERSION_SAVED', '当前版本尚无原文条目。' if not baseline else '')}
    capabilities.update({name: dict(unavailable) for name in ('start', 'steer', 'interrupt', 'resume', 'open_external_task')})
    receipts = [{'feedback_id': f['feedback_id'], 'revision_id': f['expected_revision_id'],
                 'state': 'REVISION_REPORTED' if f['feedback_id'] in head.get('feedback_resolutions', {}) else 'SAVED',
                 'saved_at': f['created_at'], 'retrieved_at': None,
                 'retrieval_state': 'UNREPORTED', 'resolution': head.get('feedback_resolutions', {}).get(f['feedback_id'])}
                for f in head['feedback']]
    return {'schema_version':'reqpilot-plugin-workbench/v1','session_id':head['session_id'],
        'work_ref': head['session_id'], 'producer_ref': {'kind': 'EXTERNAL_PLUGIN', 'id': head['session_id']},
        **({'fixed_workflow':_current_fixed_workflow(root,head)} if head.get('fixed_workflow') else {}),
        'task_ref': None, 'goal': head.get('goal') or None,
        'execution': {'status': 'UNREPORTED', 'source': 'EXTERNAL_PLUGIN', 'last_reported_at': None},
        'last_synced_at': head['versions'][-1]['created_at'] if head['versions'] else head.get('updated_at'),
        'capabilities': capabilities, 'feedback_receipts': receipts,
        'project_id':head['project_id'],'document_id':head['document_id'],'source_sha256':head['source_sha256'],
        'revision_id':head.get('revision_id'),'revision_number':len(head['versions']),
        'versions':head['versions'],'stages':stages,'baseline_sha256':baseline,
        'map_confirmation_required':bool(head['require_map_confirmation'] and map_sha and not confirmed),
        'confirmed_map_sha256':head.get('confirmed_map_sha256'),'analysis_policy':policy,
        'feedback':head['feedback'],'updated_at':head['updated_at'],
        'feedback_resolutions':head.get('feedback_resolutions',{}),
        'workbench_path':'/workspace?'+urlencode({'task':'extraction','mode':'organize','checkpoint':'structure',
            'projectId':head['project_id'],'documentId':head['document_id'],'pluginSessionId':head['session_id']}),
        'producer':'EXTERNAL_PLUGIN','model_dispatch':'NOT_PERFORMED','human_acceptance':'NOT_CLAIMED'}


def _compile_view(artifacts, source_sha, *, project_id, document_id, workbench_ref):
    unknown=set(artifacts)-ARTIFACT_NAMES
    if unknown:
        raise ValueError('Unsupported proof-bearing artifacts: '+', '.join(sorted(unknown))+'. Existing analysis or engineer opinions must not be omitted; their evidence transport is not yet supported.')
    if not artifacts:
        return None,None,None,None
    if 'document-map.json' not in artifacts or 'table-previews.json' not in artifacts:
        raise ValueError('A saved Map and its complete table previews are required')
    mapping=core.DocumentMapArtifactV1.model_validate(artifacts['document-map.json'])
    if mapping.source_sha256!=source_sha:
        raise ValueError('Map belongs to another original')
    if mapping.source_format:
        raise ValueError('This workbench bridge currently verifies PDF originals; preserve native Office artifacts separately')
    baseline=None
    if 'source-requirement-baseline.json' in artifacts:
        baseline=core.SourceRequirementBaselineArtifactV1.model_validate(artifacts['source-requirement-baseline.json'])
        receipt=core.SourceRequirementBaselineValidationReceiptV1.model_validate(artifacts.get('validation-receipt.json'))
        if baseline.source_sha256!=source_sha or baseline.document_map_artifact_sha256!=mapping.artifact_sha256 or receipt.artifact_sha256!=baseline.artifact_sha256:
            raise ValueError('Map, source baseline and validation receipt differ')
        coverage=artifacts.get('item-compilation-receipt.json')
        if coverage is not None:
            if (coverage.get('source_sha256')!=source_sha or coverage.get('baseline_artifact_sha256')!=baseline.artifact_sha256
                    or not isinstance(coverage.get('unprocessed_scope_ids'), list)):
                raise ValueError('Item coverage receipt differs from the source baseline')
        check=artifacts.get('workflow-check.json')
        if check is not None:
            basis=check.get('basis') or {}
            if check.get('schema')!='reqpilot-workflow-check/v1' or basis.get('source_sha256')!=source_sha:
                raise ValueError('Workflow check receipt differs from the uploaded source')
    elif set(artifacts)-{'document-map.json','table-previews.json'}:
        raise ValueError('Annotations and review state require their actual source baseline')
    view_baseline=baseline or core.create_empty_source_requirement_baseline(mapping)[0]
    assessment_values = (artifacts.get('source-item-assessments.json'),
        artifacts.get('analysis-packet.json'), artifacts.get('knowledge-catalog.json'),
        artifacts.get('evidence-registration.json'), artifacts.get('knowledge-attestation.json'))
    if any(value is not None for value in assessment_values):
        if not all(value is not None for value in assessment_values) or baseline is None:
            raise ValueError('Saved analysis requires its assessments, packet, knowledge catalog, evidence registration and Host attestation')
        catalog = pair_evidence.validate_pair_registration(assessment_values[3], assessment_values[2])
        key = os.environ.get('REQPILOT_HOST_ATTESTATION_KEY')
        if not key:
            raise ValueError('Host evidence attestation key is unavailable')
        attestation = pair_evidence.verify_pair_attestation(assessment_values[4], key, catalog=catalog)
        if (attestation.get('project_id') != project_id
                or attestation.get('document_id') != document_id
                or attestation.get('workbench_ref') != workbench_ref
                or catalog.get('project_id') != project_id
                or attestation.get('catalog_sha256') != catalog.get('catalog_sha256')):
            raise ValueError('Host evidence attestation belongs to another project, document or workbench work')
        records = assessment_values[0].get('records') or {}
        expected_records = {decision['unit_id']: {key: decision[key] for key in
            ('decision', 'reason', 'evidence', 'clarification_question')}
            for decision in attestation.get('decisions', [])}
        if records != expected_records:
            raise ValueError('Host attestation does not authenticate every cumulative assessment record')
        packet = assessment_values[1]
        expected = analysis_core.build_source_item_analysis_packet(mapping, baseline,
            artifacts['table-previews.json'], catalog,
            decomposition=artifacts.get('source-item-decomposition.json'),
            image_annotations=artifacts.get('image-annotations.json'),
            selected_unit_ids=packet.get('selected_unit_ids'))
        if packet != expected:
            raise ValueError('Analysis packet differs from the actual source, Map, items, derivation or selected evidence')
        analysis_core.validate_source_item_assessments(packet, assessment_values[0])
        if assessment_values[0].get('baseline_sha256') != baseline.artifact_sha256:
            raise ValueError('Saved analysis belongs to another source baseline')
    snapshot=build_delivery_snapshot(mapping,view_baseline,artifacts['table-previews.json'],
        annotations=artifacts.get('image-annotations.json'),decomposition=artifacts.get('source-item-decomposition.json'),
        assessments=artifacts.get('source-item-assessments.json'),
        analysis_policy=artifacts.get('analysis-policy.json'))
    return mapping,baseline,snapshot,artifacts.get('analysis-policy.json')


def sync_plugin_draft(project_id: str,document_id: str,payload: PluginDraftUpload,response: ResponseSink):
    response.headers.update(_no_store_headers())
    document=_source(project_id,document_id,payload.expected_source_sha256)
    declared_format=str(document.get('detected_format') or document.get('file_type') or '').lower()
    if declared_format and declared_format!='pdf':
        raise ApplicationError(status_code=422,detail='本轮插件工作台承接PDF；Office成果请保留，当前不能省略原生定位强行导入。')
    # Decode/validate before creating or advancing the user-visible version.
    if len(json.dumps(payload.artifacts,ensure_ascii=False).encode())>64*1024*1024:
        raise ApplicationError(status_code=413,detail='本次成果过大，请按已保存的完整业务范围分批同步。')
    try:
        mapping,baseline,snapshot,policy=_compile_view(payload.artifacts,payload.expected_source_sha256,
            project_id=project_id, document_id=document_id, workbench_ref=payload.session_id)
        source=Path(resolve_source_record_path(document)).resolve(strict=True)
        if hashlib.sha256(source.read_bytes()).hexdigest()!=payload.expected_source_sha256:
            raise ValueError('Registered original bytes changed')
        import pymupdf
        with pymupdf.open(source) as pdf:
            if not pdf.is_pdf:raise ValueError('This workbench bridge requires the original PDF')
            if mapping and any(page>len(pdf) for node in mapping.nodes for page in node.pages):
                raise ValueError('Map points outside the original PDF')
    except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:
        raise ApplicationError(status_code=422,detail=str(exc)) from exc
    root=_root(project_id,document_id,payload.session_id);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with _locked(root):
        if (root/'head.json').exists():
            head=_read(root/'head.json')
            policy_changed=('require_map_confirmation' in payload.model_fields_set and head['require_map_confirmation']!=payload.require_map_confirmation)
            if head['source_sha256']!=payload.expected_source_sha256 or policy_changed:
                raise ApplicationError(status_code=409,detail='连接的来源或结构确认策略已不同，请使用原连接。')
        else:
            head={'project_id':project_id,'document_id':document_id,'session_id':payload.session_id,
                'source_sha256':payload.expected_source_sha256,'require_map_confirmation':payload.require_map_confirmation,
                'revision_id':None,'versions':[],'feedback':[],'commands':{},'goal':payload.goal,'updated_at':_now()}
        if set(payload.feedback_resolutions)-{f['feedback_id'] for f in head['feedback']} or any(not reason.strip() or len(reason)>8000 for reason in payload.feedback_resolutions.values()):
            raise ApplicationError(status_code=422,detail='反馈处理结果必须引用本任务的真实意见，并说明如何处理。')
        identity={'source_sha256':payload.expected_source_sha256,'artifacts':payload.artifacts,'local_review_sha256':payload.local_review_sha256,
            'feedback_resolutions':payload.feedback_resolutions}
        content_digest=core.canonical_json_sha256(identity)
        workflow_head = head if content_digest == head.get('content_digest') else {**head, 'revision_id': None}
        fixed_workflow = _derive_fixed_workflow(payload.fixed_workflow, payload.expected_source_sha256,
            mapping, baseline, payload.artifacts, root, workflow_head, payload.local_review_sha256)
        if fixed_workflow is not None:
            head['fixed_workflow']=fixed_workflow
        if not mapping:
            _write_json(root/'head.json',head);_remember_latest(root,head);return _public(root,head)
        if content_digest==head.get('content_digest'):
            if fixed_workflow is not None and head.get('fixed_workflow')!=fixed_workflow:
                head['fixed_workflow']=fixed_workflow;head['updated_at']=_now();_write_json(root/'head.json',head)
            _remember_latest(root,head);return _public(root,head)
        if payload.expected_revision_id!=head.get('revision_id'):
            raise ApplicationError(status_code=409,detail='工作台已有新成果，请取回当前版本后再同步；旧成果保留。')
        if baseline and head['require_map_confirmation'] and head.get('confirmed_map_sha256')!=mapping.artifact_sha256:
            raise ApplicationError(status_code=409,detail='请先同步并在工作台确认这一版结构，再同步条目。')
        # User opinions belong to this session, not to the external Agent upload.
        # Carry them through the shared evidence/context validator under the same
        # head lock so a concurrent human write cannot be silently overwritten.
        from .plugin_workbench_delivery import carry_manual_responses
        manual = carry_manual_responses(root, head, mapping, baseline, payload.artifacts)
        saved_artifacts = dict(payload.artifacts)
        if manual is not None:
            saved_artifacts['manual-decisions.json'] = manual
            snapshot = build_delivery_snapshot(mapping, baseline or core.create_empty_source_requirement_baseline(mapping)[0],
                payload.artifacts['table-previews.json'], annotations=payload.artifacts.get('image-annotations.json'),
                decomposition=payload.artifacts.get('source-item-decomposition.json'),
                assessments=payload.artifacts.get('source-item-assessments.json'),
                analysis_policy=policy, manual_decisions=manual)
        revision=core.canonical_json_sha256({**identity,'predecessor_revision_id':head.get('revision_id')})
        directory=root/'versions'/revision;directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        # Replaying an interrupted write is safe because this directory is content-addressed.
        files={}
        for name,value in {**saved_artifacts,'matrix-snapshot.json':snapshot}.items():
            _write_json(directory/name,value);files[name]=hashlib.sha256((directory/name).read_bytes()).hexdigest()
        stages=[{'stage':'MAP','artifact_sha256':mapping.artifact_sha256,'count':len(mapping.nodes)}]
        if baseline:stages.append({'stage':'ITEMS','artifact_sha256':baseline.artifact_sha256,'count':len(baseline.records)})
        version={'revision_id':revision,'source_sha256':payload.expected_source_sha256,'predecessor_revision_id':head.get('revision_id'),
            'local_review_sha256':payload.local_review_sha256,'stages':stages,'baseline_sha256':baseline.artifact_sha256 if baseline else None,
            'snapshot_sha256':snapshot['delivery_sha256'],'analysis_policy':policy,'files':files,'created_at':_now(),
            **({'fixed_workflow':fixed_workflow} if fixed_workflow else {})}
        _write_json(directory/'version.json',version)
        head['revision_id']=revision;head['content_digest']=content_digest;head['versions'].append({'revision_id':revision,'number':len(head['versions'])+1,'stages':stages,'created_at':version['created_at']})
        resolutions=head.setdefault('feedback_resolutions',{})
        for feedback_id,reason in payload.feedback_resolutions.items():
            if feedback_id not in resolutions or resolutions[feedback_id]['reason']!=reason:
                resolutions[feedback_id]={'revision_id':revision,'reason':reason,'authority':'PLUGIN_REPORTED_CHANGE','human_acceptance':'NOT_CLAIMED'}
        head['updated_at']=_now();_write_json(root/'head.json',head)
        _remember_latest(root,head)
        return _public(root,head)


def get_plugin_workbench(project_id: str,document_id: str,session_id: str,response: ResponseSink):
    response.headers.update(_no_store_headers());root,head=_load(project_id,document_id,session_id)
    return _public(root,head)


def get_latest_plugin_workbench(project_id: str,document_id: str,response: ResponseSink):
    response.headers.update(_no_store_headers())
    document=_require_project_document(project_id,document_id)
    pointer=_root(project_id,document_id,'plugin-'+'0'*32).parent/'latest.json'
    if not pointer.is_file():return None
    latest=_read(pointer)
    if latest['source_sha256']!=_document_source_sha256(document):return None
    root,head=_load(project_id,document_id,latest['session_id']);state=_public(root,head)
    summary={key:state[key] for key in ('session_id','source_sha256','revision_id','revision_number','stages','workbench_path','updated_at','map_confirmation_required')}
    if state.get('fixed_workflow'):summary['fixed_workflow']=state['fixed_workflow']
    return summary


def list_plugin_workbench_sessions(project_id: str, document_id: str, response: ResponseSink):
    """List this source's real task owners without electing a current result."""
    response.headers.update(_no_store_headers())
    document = _require_project_document(project_id, document_id)
    source_sha = _document_source_sha256(document)
    parent = _root(project_id, document_id, 'plugin-' + '0'*32).parent
    sessions, unavailable = [], []
    keys = ('session_id', 'work_ref', 'producer_ref', 'project_id', 'document_id', 'source_sha256',
            'goal', 'revision_id', 'revision_number', 'stages', 'execution', 'last_synced_at',
            'updated_at', 'capabilities', 'workbench_path', 'map_confirmation_required')
    for directory in sorted(parent.iterdir() if parent.is_dir() else [], key=lambda p: p.name):
        if not re.fullmatch(SESSION, directory.name):
            continue
        if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != parent.resolve():
            unavailable.append({'session_id': directory.name, 'reason_code': 'UNSAFE_TASK_PATH', 'reason': '任务目录无法安全读取。'})
            continue
        try:
            if not _session_metadata_owned(directory, 'head.json'):
                unavailable.append({'session_id': directory.name, 'reason_code': 'UNSAFE_TASK_PATH', 'reason': '任务索引不在本任务目录内。'})
                continue
            head = _read(directory/'head.json')
            if (head['project_id'], head['document_id'], head['session_id']) != (project_id, document_id, directory.name):
                raise ValueError('Task identity differs from its document scope')
            if head['source_sha256'] != source_sha:
                unavailable.append({'session_id': directory.name, 'source_sha256': head['source_sha256'],
                    'reason_code': 'SOURCE_VERSION_CHANGED', 'reason': '该任务属于其他原件版本，当前未自动替换来源。'})
                continue
            revision = head.get('revision_id')
            if revision and (not isinstance(revision, str) or not re.fullmatch(SHA, revision)):
                raise ValueError('Invalid revision identity')
            if revision and not _session_metadata_owned(directory, f'versions/{revision}/version.json'):
                unavailable.append({'session_id': directory.name, 'reason_code': 'UNSAFE_TASK_PATH', 'reason': '成果版本索引不在本任务目录内。'})
                continue
            state = _public(directory, head)
            summary = {key: state[key] for key in keys}
            if state.get('fixed_workflow'):summary['fixed_workflow']=state['fixed_workflow']
            summary.update(availability='AVAILABLE' if head.get('revision_id') else 'NO_ARTIFACTS',
                title=head.get('goal') or '外部任务 ' + directory.name[-8:],
                title_origin='USER_GOAL' if head.get('goal') else 'GENERATED_IDENTIFIER')
            sessions.append(summary)
        except (ApplicationError, OSError, ValueError, KeyError, TypeError):
            unavailable.append({'session_id': directory.name, 'reason_code': 'SAVED_TASK_UNREADABLE',
                                'reason': '保存的任务索引或当前版本暂不可读取；其它任务仍可打开。'})
    return {'schema_version': 'reqpilot-plugin-workbench-sessions/v1', 'project_id': project_id,
            'document_id': document_id, 'source_sha256': source_sha, 'sessions': sessions,
            'unavailable': unavailable, 'selection_policy': 'EXPLICIT_WORK_SELECTION',
            'current_session_id': None, 'order': 'STABLE_SESSION_ID'}


def _session_metadata_owned(root, relative):
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            return False
    return path.resolve().is_relative_to(root.resolve())


def get_plugin_snapshot(project_id: str,document_id: str,session_id: str,response: ResponseSink,revision: str | None=None):
    response.headers.update(_no_store_headers());root,head=_load(project_id,document_id,session_id);directory,version=_version(root,head,revision)
    return _artifact(directory,version,'matrix-snapshot.json')


def get_plugin_stage(project_id: str,document_id: str,session_id: str,stage: str,response: ResponseSink,offset: int=0,limit: int=100,revision: str | None=None):
    response.headers.update(_no_store_headers());root,head=_load(project_id,document_id,session_id);directory,version=_version(root,head,revision)
    if stage not in {'MAP','ITEMS'} or offset<0 or not 1<=limit<=100:
        raise ApplicationError(status_code=400,detail='阶段或分页范围无效。')
    name='document-map.json' if stage=='MAP' else 'source-requirement-baseline.json'
    artifact=_artifact(directory,version,name);rows=artifact['nodes' if stage=='MAP' else 'records']
    return {'stage':stage,'source_sha256':head['source_sha256'],'artifact_sha256':artifact['artifact_sha256'],
        'total':len(rows),'offset':offset,'rows':rows[offset:offset+limit],
        'next_offset':offset+limit if offset+limit<len(rows) else None,'status':'DRAFT','human_acceptance':'NOT_CLAIMED'}


def _check_action(head, source, revision):
    if source!=head['source_sha256'] or revision!=head.get('revision_id'):
        raise ApplicationError(status_code=409,detail='当前成果已更新，请查看新版本后再提交。')


def confirm_plugin_map(project_id: str,document_id: str,session_id: str,payload: PluginMapConfirmation,response: ResponseSink):
    response.headers.update(_no_store_headers());root,_=_load(project_id,document_id,session_id)
    with _locked(root):
        head=_read(root/'head.json')
        key='confirm:'+payload.command_id;digest=core.canonical_json_sha256(payload.model_dump(mode='json'))
        if key in head['commands']:
            if head['commands'][key]!=digest:raise ApplicationError(status_code=409,detail='确认重试内容不同，请重新核对。')
            return _public(root,head)
        _check_action(head,payload.expected_source_sha256,payload.expected_revision_id)
        _,version=_version(root,head);mapping=next(s for s in version['stages'] if s['stage']=='MAP')
        if mapping['artifact_sha256']!=payload.expected_map_sha256:
            raise ApplicationError(status_code=409,detail='结构版本已改变，不能沿用旧确认。')
        head['confirmed_map_sha256']=payload.expected_map_sha256;head['updated_at']=_now()
        head['map_confirmation']={'map_sha256':payload.expected_map_sha256,'revision_id':payload.expected_revision_id,
            'command_id':payload.command_id,'scope':'STRUCTURE_ONLY','created_at':head['updated_at']}
        head['commands'][key]=digest
        _write_json(root/'head.json',head);return _public(root,head)


def post_plugin_feedback(project_id: str,document_id: str,session_id: str,payload: PluginFeedback,response: ResponseSink):
    response.headers.update(_no_store_headers());root,_=_load(project_id,document_id,session_id)
    with _locked(root):
        head=_read(root/'head.json');digest=core.canonical_json_sha256(payload.model_dump(mode='json'))
        prior=head['commands'].get(payload.command_id)
        if prior:
            if prior!=digest:raise ApplicationError(status_code=409,detail='反馈重试内容不同，请重新提交。')
            return _public(root,head)
        _check_action(head,payload.expected_source_sha256,payload.expected_revision_id)
        directory,version=_version(root,head);mapping=_artifact(directory,version,'document-map.json')
        if payload.node_id and payload.node_id not in {n['node_id'] for n in mapping['nodes']}:
            raise ApplicationError(status_code=422,detail='反馈的原文结构不存在于当前版本。')
        known=set()
        if version['baseline_sha256']:
            baseline=_artifact(directory,version,'source-requirement-baseline.json');known={r['source_record_id'] for r in baseline['records']}
        if set(payload.source_item_ids)-known or not payload.text.strip():
            raise ApplicationError(status_code=422,detail='反馈条目或内容无效。')
        selected_node=next((n for n in mapping['nodes'] if n['node_id']==payload.node_id),None)
        target_label=(selected_node.get('title') or selected_node.get('label')) if selected_node else '整个文档'
        if payload.source_item_ids:
            target_label='、'.join(r['source_display_id']+' '+r['label'] for r in baseline['records'] if r['source_record_id'] in payload.source_item_ids)
        head['feedback'].append({'feedback_id':'plugin-feedback-'+digest[:32],**payload.model_dump(mode='json'),
            'target_label':target_label,
            'baseline_sha256':version['baseline_sha256'],'map_sha256':mapping['artifact_sha256'],
            'origin':'USER_REQUEST','created_at':_now()})
        head['commands'][payload.command_id]=digest;head['updated_at']=_now();_write_json(root/'head.json',head)
        return _public(root,head)


def export_plugin_draft(project_id: str,document_id: str,session_id: str,revision: str):
    root,head=_load(project_id,document_id,session_id);directory,version=_version(root,head,revision)
    if not version['baseline_sha256']:
        raise ApplicationError(status_code=409,detail='当前只有结构草稿，原文条目尚未同步。')
    snapshot=_artifact(directory,version,'matrix-snapshot.json');document=_source(project_id,document_id,head['source_sha256'])
    source=Path(resolve_source_record_path(document)).resolve(strict=True)
    if hashlib.sha256(source.read_bytes()).hexdigest()!=head['source_sha256']:
        raise ApplicationError(status_code=409,detail='原件内容已变化，停止导出。')
    data=export_delivery_xlsx(snapshot,source_file_path=str(source),source_sha256=head['source_sha256'],
        source_file_name=str(document.get('file_name') or '').strip() or '原始文档')
    return BinaryArtifact(content=data,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={**_no_store_headers(),'Content-Disposition':'attachment; filename="ReqPilotX-plugin-draft.xlsx"',
            'X-ReqPilot-Revision':revision,'X-ReqPilot-Snapshot':snapshot['delivery_sha256']})
