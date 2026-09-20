"""Immutable native delivery snapshots using the existing matrix/export contract."""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from app import db
from app.adapters.persistence.source_item_delivery_versions import ensure_delivery_versions_schema as _schema
from app.adapters.persistence.source_item_manual_decisions import (
    _LOCK, _execute, lock_manual_document, ManualDecisionConflict,
)
from app.application.agent_workflow import source_item_delivery_bridge as bridge
from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
from app.application.errors import ApplicationError
from app.application.transport import BinaryArtifact, ResponseSink
from app.domain.requirement_matrix import summarize_effective_conclusions

TABLE = 'source_item_delivery_versions'
MEDIA = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class SaveDeliveryVersion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command_id: str = Field(min_length=1, max_length=160)
    expected_source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_delivery_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_business_basis_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    mode: Literal['DRAFT', 'FORMAL'] = 'DRAFT'
    accept_snapshot: bool = False


def readiness(snapshot, reasoning):
    summary = summarize_effective_conclusions(snapshot['nodes']).model_dump(mode='json')
    blockers = []
    if snapshot.get('state') != 'READY':
        blockers.append('当前矩阵尚未就绪。')
    if summary['missing_effective_conclusion_count']:
        blockers.append(f"仍有 {summary['missing_effective_conclusion_count']} 个分析单元缺少本版有效响应。")
    if reasoning.get('active_jobs'):
        blockers.append('当前分析任务仍在更新，请完成后同步。')
    if not snapshot.get('nodes'):
        blockers.append('当前版本没有可交付条目。')
    return {**summary, 'human_count': summary['human_override_count'] + summary['human_native_count'], 'can_lock': not blockers, 'blockers': blockers}


def _verified(row):
    value = json.loads(row['artifact_json'])
    if canonical_json_sha256(value) != row['artifact_sha256']:
        raise ApplicationError(status_code=409, detail='交付版本校验失败，请重新同步。')
    snapshot = value['snapshot']
    if canonical_json_sha256({k:v for k,v in snapshot.items() if k != 'delivery_sha256'}) != snapshot['delivery_sha256']:
        raise ApplicationError(status_code=409, detail='交付快照校验失败。')
    return value


def _public(value):
    source = value['snapshot']['source_item_baseline']
    return {k:v for k,v in value.items() if k not in {'snapshot', 'export_key'}} | {
        'baseline_revision': source['revision'], 'source_sha256': source['source_sha256'],
        'delivery_sha256': value['snapshot']['delivery_sha256'],
        'response_revision': source.get('manual_decisions_revision', 0),
    }


def preview_delivery_version(project_id: str, document_id: str, response: ResponseSink):
    response.headers.update(bridge._no_store_headers())
    snapshot, _, _, reasoning = bridge._build_current_delivery(project_id, document_id)
    source = snapshot['source_item_baseline']
    return {'source_sha256': source['source_sha256'], 'delivery_sha256': snapshot['delivery_sha256'],
            'business_basis_sha256': reasoning['business_basis_sha256'], 'baseline_revision': source['revision'],
            'response_revision': source.get('manual_decisions_revision', 0),
            'readiness': readiness(snapshot, reasoning)}


def list_delivery_versions(project_id: str, document_id: str, response: ResponseSink):
    response.headers.update(bridge._no_store_headers())
    bridge._require_project_document(project_id, document_id)
    with db.get_conn() as conn:
        _schema(conn)
        rows = _execute(conn, f'SELECT * FROM {TABLE} WHERE project_id=? AND sor_document_id=? ORDER BY revision DESC',
                        (project_id, document_id)).fetchall()
    return {'versions': [_public(_verified(row)) for row in rows]}


def save_delivery_version(project_id: str, document_id: str, payload: SaveDeliveryVersion, response: ResponseSink):
    response.headers.update(bridge._no_store_headers())
    bridge._require_project_document(project_id, document_id)
    request_sha = canonical_json_sha256(payload.model_dump(mode='json'))
    try:
        with _LOCK, db.get_conn() as conn:
            _schema(conn)
            # Match an uncertain retry even if a newer source has since been uploaded.
            row = _execute(conn, f'SELECT * FROM {TABLE} WHERE project_id=? AND sor_document_id=? AND command_id=?',
                           (project_id, document_id, payload.command_id)).fetchone()
            if row:
                if row['request_sha256'] != request_sha:
                    raise ApplicationError(status_code=409, detail='相同保存命令不能替换交付内容。')
                return _public(_verified(row))
            lock_manual_document(conn, project_id, document_id, payload.expected_source_sha256)
            # Repeat under the cross-process document lock for simultaneous retries.
            row = _execute(conn, f'SELECT * FROM {TABLE} WHERE project_id=? AND sor_document_id=? AND command_id=?',
                           (project_id, document_id, payload.command_id)).fetchone()
            if row:
                if row['request_sha256'] != request_sha:
                    raise ApplicationError(status_code=409, detail='相同保存命令不能替换交付内容。')
                return _public(_verified(row))
            snapshot, source_path, document, reasoning = bridge._build_current_delivery(project_id, document_id)
            source = snapshot['source_item_baseline']
            if (snapshot['delivery_sha256'] != payload.expected_delivery_sha256
                    or source['source_sha256'] != payload.expected_source_sha256
                    or reasoning['business_basis_sha256'] != payload.expected_business_basis_sha256):
                raise ApplicationError(status_code=409, detail='原文或响应已更新，请同步并核对这一版后重新保存。')
            ready = readiness(snapshot, reasoning)
            if payload.mode == 'FORMAL' and (not payload.accept_snapshot or not ready['can_lock']):
                raise ApplicationError(status_code=409, detail='请完成本版有效响应并明确确认锁定后，再保存正式交付版。')
            previous = _execute(conn, f'SELECT MAX(revision) AS revision FROM {TABLE} WHERE project_id=? AND sor_document_id=?',
                                (project_id, document_id)).fetchone()
            revision = (previous['revision'] or 0) + 1
            project = db.get_project(project_id) or {}
            data = bridge.export_delivery_xlsx(snapshot, source_file_path=str(source_path), source_sha256=source['source_sha256'],
                source_file_name=document.get('file_name') or '', project_name=project.get('name') or 'ReqPilotX',
                customer_name=project.get('customer_name') or '', export_mode=payload.mode, version_label=f'Delivery-{revision}')
            digest = sha256(data).hexdigest()
            key = f'source-item-deliveries/{project_id}/{document_id}/{digest}.xlsx'
            bridge._container().object_store.put(key=key, source=BytesIO(data), expected_sha256=digest, media_type=MEDIA)
            value = {'schema_version': 'source-item-delivery-version/v1', 'revision': revision, 'mode': payload.mode,
                     'created_at': datetime.now(timezone.utc).isoformat(), 'snapshot': snapshot, 'readiness': ready,
                     'business_basis_sha256': reasoning['business_basis_sha256'], 'export_key': key, 'export_sha256': digest,
                     'filename': f"{document.get('file_name') or 'SourceItems'}-Delivery-v{revision}-{payload.mode}.xlsx",
                     'acceptance': {'origin': 'USER_INPUT', 'accepted_delivery_sha256': snapshot['delivery_sha256']} if payload.mode == 'FORMAL' else None}
            _execute(conn, f'INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?)',
                     (project_id, document_id, revision, payload.command_id, request_sha,
                      json.dumps(value, ensure_ascii=False, sort_keys=True), canonical_json_sha256(value), value['created_at']))
            return _public(value)
    except ManualDecisionConflict as exc:
        raise ApplicationError(status_code=409, detail='来源版本已更新，请同步当前文档。') from exc


def _version(project_id, document_id, revision):
    bridge._require_project_document(project_id, document_id)
    with db.get_conn() as conn:
        _schema(conn)
        row = _execute(conn, f'SELECT * FROM {TABLE} WHERE project_id=? AND sor_document_id=? AND revision=?',
                       (project_id, document_id, revision)).fetchone()
    if not row:
        raise ApplicationError(status_code=404, detail='当前文档没有这个交付版本。')
    return _verified(row)


def get_delivery_version(project_id: str, document_id: str, revision: int, response: ResponseSink):
    response.headers.update(bridge._no_store_headers())
    value = _version(project_id, document_id, revision)
    return {**_public(value), 'snapshot': value['snapshot']}


def download_delivery_version(project_id: str, document_id: str, revision: int):
    value = _version(project_id, document_id, revision)
    with bridge._container().object_store.open(value['export_key']) as stream:
        data = stream.read()
    if sha256(data).hexdigest() != value['export_sha256']:
        raise ApplicationError(status_code=409, detail='交付文件校验失败。')
    return BinaryArtifact(content=data, media_type=MEDIA, headers={**bridge._no_store_headers(),
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(value['filename']),
        'X-ReqPilotX-Delivery-SHA256': value['snapshot']['delivery_sha256']})
