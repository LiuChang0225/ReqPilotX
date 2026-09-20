"""User-only working-opinion boundary for the native SourceItem workbench."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app import db
from app.application.errors import ApplicationError
from app.application.transport import ResponseSink
from app.application.agent_workflow.native_structure_bridge import _no_store_headers
from app.application.agent_workflow.source_item_manual_decisions import build_manual_decision_context, validate_manual_decisions
from app.adapters.persistence import source_item_manual_decisions as repository


class EngineerResponseChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    unit_id: str = Field(min_length=1, max_length=260)
    expected_unit_context_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    action: Literal['SET', 'WITHDRAW'] = 'SET'
    decision: Literal['COMPLIANT', 'PARTIALLY_COMPLIANT', 'NON_COMPLIANT', 'PENDING_EVIDENCE', 'NEEDS_CLARIFICATION', 'NOT_APPLICABLE'] | None = None
    reason: str = Field(default='', max_length=8000)
    conditions: str = Field(default='', max_length=4000)
    evidence_requests: str = Field(default='', max_length=4000)


class EngineerResponseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str = Field(min_length=1, max_length=200)
    expected_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_baseline_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_response_revision: StrictInt = Field(ge=0)
    decisions: list[EngineerResponseChoice] = Field(min_length=1, max_length=100)


def _resolve_current_manual_read(project_id, document_id):
    from app.application.agent_workflow.source_item_delivery_bridge import _read_current_delivery_inputs
    _, bundle, _, previews, reasoning = _read_current_delivery_inputs(project_id, document_id)
    context = build_manual_decision_context(bundle['document_map'], bundle['baseline'], previews,
        decomposition=reasoning['decomposition'], annotations=bundle.get('image_annotations'))
    return context, reasoning['business_basis_sha256']


def resolve_current_manual_context(project_id, document_id):
    return _resolve_current_manual_read(project_id, document_id)[0]


def manual_response_view(project_id, document_id, context):
    with db.get_conn() as conn:
        available = repository.manual_storage_available(conn)
        stored = repository.current_manual_decisions_in_transaction(conn, project_id, document_id) if available else None
    if stored is not None:
        try:
            validate_manual_decisions(stored, context)
        except ValueError as exc:
            raise ApplicationError(status_code=409, detail='工程师意见与当前业务版本尚未同步，请刷新后重试。') from exc
    return {'schema_version': 'source-item-engineer-response-view/v1', 'available': available,
        'baseline_sha256': context['baseline_sha256'], 'source_sha256': context['source_sha256'],
        'response_revision': (stored or {}).get('revision', 0), 'response_sha256': (stored or {}).get('artifact_sha256', ''),
        'units': context['units'], 'records': (stored or {}).get('records', {}), 'history': (stored or {}).get('history', []),
        'human_acceptance': 'NOT_CLAIMED'}


def get_engineer_responses(project_id: str, document_id: str, response: ResponseSink) -> dict:
    response.headers.update(_no_store_headers())
    context, basis_sha = _resolve_current_manual_read(project_id, document_id)
    return {**manual_response_view(project_id, document_id, context), 'business_basis_sha256': basis_sha}


def post_engineer_responses(project_id: str, document_id: str, payload: EngineerResponseRequest, response: ResponseSink) -> dict:
    response.headers.update(_no_store_headers())
    try:
        repository.save_manual_decisions(project_id, document_id, source_sha256=payload.expected_source_sha256,
            expected_baseline_sha256=payload.expected_baseline_sha256, expected_response_revision=payload.expected_response_revision,
            command_id=payload.command_id, decisions=[d.model_dump(exclude_none=True) for d in payload.decisions],
            context_resolver=lambda: resolve_current_manual_context(project_id, document_id))
    except ValueError as exc:
        code = str(exc)
        messages = {'MANUAL_SOURCE_CHANGED': '原文件已更新，请同步后重新填写。',
            'MANUAL_BASELINE_CHANGED': '原文工作版已更新，请核对新版本后保存。',
            'MANUAL_RESPONSE_CHANGED': '工程师意见已有更新，请同步后核对再保存。',
            'MANUAL_UNIT_CONTEXT_CHANGED': '本条依据或派生内容已更新，请重新阅读后保存。',
            'MANUAL_STORAGE_MIGRATION_REQUIRED': '工程师响应存储尚未启用，请完成应用更新。',
            'MANUAL_COMMAND_REUSED_WITH_DIFFERENT_INPUT': '这次保存请求已经使用，请刷新后重试。'}
        raise ApplicationError(status_code=409, detail={'code': code,
            'message': messages.get(code, '工程师工作意见未保存，请检查填写内容后重试。')}) from exc
    return get_engineer_responses(project_id, document_id, response)
