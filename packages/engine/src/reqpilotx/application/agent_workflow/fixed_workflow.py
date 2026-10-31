"""Versioned projection of the fixed ReqPilotX workflow over saved artifacts.

This module owns stage order and program-visible completion rules only. It never
reads model prose, conversation state or an idle session to advance a stage.
Existing task/Map/items/review/delivery artifacts remain the only evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

WORKFLOW_SCHEMA = 'reqpilot-fixed-workflow/v1'
STATE_SCHEMA = 'reqpilot-fixed-workflow-state/v1'
WORKFLOW_ID = 'reqpilot-fixed-workflow'
WORKFLOW_VERSION = '1.0.0'
EVIDENCE_MODES = {'NONE', 'PROJECT_REFERENCES', 'TEAM_KNOWLEDGE', 'COMBINED'}
STAGE_ORDER = (
    ('PREPARATION', '资料准备', True),
    ('SOURCE_MAP', '来源与结构', True),
    ('ITEMS_CLASSIFICATION', '条目、分类与明确派生', True),
    ('EVIDENCE_ANALYSIS', '依据分析', True),
    ('SELF_CHECK', '自查与修正', True),
    ('DELIVERABLE', '可核对成果', True),
)
STAGE_LABELS = {stage: label for stage, label, _required in STAGE_ORDER}
STAGE_IDS = tuple(stage for stage, _label, _required in STAGE_ORDER)


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def canonical_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


DEFINITION_SHA256 = canonical_digest({
    'schema': WORKFLOW_SCHEMA,
    'id': WORKFLOW_ID,
    'version': WORKFLOW_VERSION,
    'stages': [{'id': stage, 'label': label, 'required': required}
               for stage, label, required in STAGE_ORDER],
})


FIXED_GOAL = (
    '按 ReqPilotX 固定工作流处理当前客户文档：资料准备；来源检查与原文结构（Document Map）；'
    '原文条目、分类及明确授权的必要派生；依据分析（无内部依据时明确跳过）；自查与修正；'
    '保存可核对成果。复用已有任务与已保存工件，不另建任务。阶段是否完成只以已保存工件和程序检查为准，'
    '不以文字声称、会话空闲或导出动作代替；补充说明不能取消 Map、条目或交付前置检查。'
)


def compose_goal(notes: str = '') -> str:
    if not isinstance(notes, str):
        raise ValueError('INVALID_SUPPLEMENTARY_INSTRUCTIONS')
    notes = notes.strip()
    if len(notes) > 12000:
        raise ValueError('SUPPLEMENTARY_INSTRUCTIONS_TOO_LONG')
    if not notes:
        return FIXED_GOAL
    return FIXED_GOAL + '\n\n补充说明（不得取消固定阶段和输出校验）：\n' + notes


def brief_sha256(goal: str) -> str:
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError('INVALID_WORKFLOW_BRIEF')
    return canonical_digest({'workflow': WORKFLOW_ID, 'version': WORKFLOW_VERSION, 'goal': goal})


def public_descriptor() -> dict:
    """The host/server-owned intake description; not model output and not acceptance."""
    return {
        'schema': WORKFLOW_SCHEMA,
        'id': WORKFLOW_ID,
        'version': WORKFLOW_VERSION,
        'definition_sha256': DEFINITION_SHA256,
        'label': '客户需求整理固定流程',
        'stages': [{'id': stage, 'order': index + 1, 'label': label,
                    'required': required}
                   for index, (stage, label, required) in enumerate(STAGE_ORDER)],
        'required_inputs': [
            {'id': 'customer_source', 'label': '客户需求文档', 'required': True},
        ],
        'optional_inputs': [
            {'id': 'project_references', 'label': '项目参考资料', 'available': False,
             'reason': '当前版本尚未接入项目参考资料。'},
            {'id': 'team_knowledge', 'label': '团队知识库', 'available': False,
             'reason': '当前版本尚未接入团队知识库。'},
        ],
        'expected_outputs': [
            '可回查原文与顺序的文档结构',
            '保留完整父项的原文条目、分类及明确授权的必要派生',
            '有内部依据时的引用分析；无内部依据时明确标记未执行',
            '程序自查结果与当前可核对成果版本',
        ],
        'branches': {
            'no_internal_evidence': 'SKIP_EVIDENCE_ANALYSIS',
            'structure_confirmation': 'WAIT_BEFORE_ITEMS_WHEN_REQUESTED',
        },
    }


def workflow_spec(*, brief_sha256: str, evidence_mode: str = 'NONE',
                  evidence_reason: str = 'INTERNAL_EVIDENCE_NOT_WIRED') -> dict:
    if evidence_mode not in EVIDENCE_MODES:
        raise ValueError('UNSUPPORTED_WORKFLOW_EVIDENCE_MODE')
    if not isinstance(brief_sha256, str) or len(brief_sha256) != 64:
        raise ValueError('INVALID_WORKFLOW_BRIEF')
    return {
        'schema': WORKFLOW_SCHEMA,
        'id': WORKFLOW_ID,
        'version': WORKFLOW_VERSION,
        'definition_sha256': DEFINITION_SHA256,
        'brief_sha256': brief_sha256,
        'evidence': {'mode': evidence_mode, 'reason': evidence_reason},
    }


def validate_workflow_spec(value) -> dict:
    if not isinstance(value, dict) or value.get('schema') != WORKFLOW_SCHEMA:
        raise ValueError('UNSUPPORTED_WORKFLOW_SCHEMA')
    if value.get('id') != WORKFLOW_ID or value.get('version') != WORKFLOW_VERSION:
        raise ValueError('UNSUPPORTED_WORKFLOW_VERSION')
    if value.get('definition_sha256') != DEFINITION_SHA256:
        raise ValueError('WORKFLOW_DEFINITION_CHANGED')
    brief = value.get('brief_sha256')
    if not isinstance(brief, str) or len(brief) != 64:
        raise ValueError('INVALID_WORKFLOW_BRIEF')
    evidence = value.get('evidence') or {}
    if evidence.get('mode') not in EVIDENCE_MODES:
        raise ValueError('UNSUPPORTED_WORKFLOW_EVIDENCE_MODE')
    if not isinstance(evidence.get('reason'), str):
        raise ValueError('INVALID_WORKFLOW_EVIDENCE_REASON')
    return value


def _evidence(kind: str, *, source_sha256: str, artifact_sha256=None, **extra) -> dict:
    value = {'kind': kind, 'source_sha256': source_sha256}
    if artifact_sha256 is not None:
        value['artifact_sha256'] = artifact_sha256
    for key, item in extra.items():
        if item is not None:
            value[key] = item
    return value


def _stage(stage: str, status: str, *, evidence=None, reason=None, branch=None) -> dict:
    value = {'id': stage, 'label': STAGE_LABELS[stage], 'status': status}
    if evidence:
        value['evidence'] = evidence
    if reason:
        value['reason'] = reason
    if branch:
        value['branch'] = branch
    return value


def _decomposition_remaining(selected: dict) -> int:
    reference = selected.get('DECOMPOSITION')
    if not reference:
        return 0
    try:
        artifact = json.loads(Path(reference['path']).read_text(encoding='utf-8'))
    except (OSError, ValueError, KeyError, TypeError):
        return 1
    remaining = artifact.get('review_scope', {}).get('remaining_source_item_ids', [])
    return len(remaining) if isinstance(remaining, list) else 1


def _assessments_evidence(review: dict | None, source_sha256: str):
    if not review:
        return None
    root = review.get('review_root')
    if not root:
        return None
    path = Path(root) / 'source-item-assessments.json'
    if not path.is_file():
        return None
    try:
        artifact = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(artifact, dict) or artifact.get('source_sha256') != source_sha256:
        return None
    return _evidence('ASSESSMENTS', source_sha256=source_sha256,
                     artifact_sha256=artifact.get('artifact_sha256'), path=str(path))


def project_workflow(*, context: dict, selected: dict, brief: dict,
                     review: dict | None = None, check: dict | None = None,
                     incomplete_operations=None) -> dict | None:
    """Project only program-saved artifact identities into the fixed stage order."""
    spec = (context.get('request') or {}).get('workflow')
    if not spec:
        return None
    spec = validate_workflow_spec(spec)
    source_sha256 = (context.get('source') or {}).get('sha256')
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        raise ValueError('WORKFLOW_SOURCE_BINDING_MISSING')
    selected = selected or {}
    brief = brief or {}
    confirmation = brief.get('map_confirmation')
    mapping = selected.get('MAP')
    items = selected.get('ITEMS')
    baseline_current = bool(items and mapping
                            and items.get('map_sha256') == mapping.get('artifact_sha256'))
    open_items = []
    stages = []

    stages.append(_stage('PREPARATION', 'SAVED', evidence=[_evidence(
        'TASK_CONTEXT', source_sha256=source_sha256,
        artifact_sha256=context.get('context_sha256'))]))

    map_status = 'SAVED' if mapping else 'PENDING'
    map_evidence = ([_evidence('DOCUMENT_MAP', source_sha256=source_sha256,
                               artifact_sha256=mapping.get('artifact_sha256'),
                               count=mapping.get('count'))] if mapping else None)
    if mapping and confirmation == 'REQUIRED_BEFORE_ITEMS':
        open_items.append('MAP_CONFIRMATION_REQUIRED')
    stages.append(_stage('SOURCE_MAP', map_status, evidence=map_evidence,
                         branch='STRUCTURE_CONFIRMATION' if confirmation in {
                             'REQUIRED_AFTER_MAP', 'REQUIRED_BEFORE_ITEMS'} else None))

    unprocessed = items.get('unprocessed_scope_ids') if baseline_current else None
    unprocessed = unprocessed if isinstance(unprocessed, list) else []
    derivation_remaining = _decomposition_remaining(selected)
    derivation = selected.get('DECOMPOSITION')
    if not mapping:
        item_status, item_reason = 'BLOCKED', 'SOURCE_MAP_REQUIRED'
    elif not baseline_current:
        item_status, item_reason = 'PENDING', 'ITEMS_NOT_SAVED'
    elif derivation_remaining:
        item_status, item_reason = 'IN_PROGRESS', 'SCOPED_DERIVATION_REMAINING'
        open_items.append('SCOPED_DERIVATION_REMAINING')
    elif unprocessed:
        item_status, item_reason = 'IN_PROGRESS', 'UNPROCESSED_SOURCE_SCOPE'
        open_items.append('UNPROCESSED_SOURCE_SCOPE')
    else:
        item_status, item_reason = 'SAVED', None
    item_evidence = ([_evidence('SOURCE_ITEMS', source_sha256=source_sha256,
                                artifact_sha256=items.get('artifact_sha256'),
                                map_sha256=items.get('map_sha256'),
                                count=items.get('count'),
                                unprocessed_scope_ids=unprocessed)]
                     if baseline_current else None)
    if derivation:
        item_evidence = [*(item_evidence or []), _evidence(
            'DECOMPOSITION', source_sha256=source_sha256,
            artifact_sha256=derivation.get('artifact_sha256'),
            count=derivation.get('count'),
            remaining=derivation_remaining)]
    stages.append(_stage('ITEMS_CLASSIFICATION', item_status, evidence=item_evidence,
                         reason=item_reason,
                         branch='SCOPED_DERIVATION' if derivation else 'NO_DERIVATION_REQUESTED'))

    mode = spec['evidence']['mode']
    policy = brief.get('analysis_policy') or {}
    policy_status = policy.get('status') or 'UNKNOWN'
    if not baseline_current:
        evidence_status, evidence_reason = 'BLOCKED', 'ITEMS_REQUIRED'
    elif mode == 'NONE':
        if policy_status == 'AVAILABLE':
            evidence_status, evidence_reason = 'BLOCKED', 'WORKFLOW_EVIDENCE_CONFLICT'
            open_items.append('WORKFLOW_EVIDENCE_CONFLICT')
        else:
            evidence_status = 'SKIPPED'
            evidence_reason = policy.get('reason') or 'NO_INTERNAL_EVIDENCE'
            open_items.append('EVIDENCE_ANALYSIS_SKIPPED')
    elif policy_status == 'SKIPPED':
        evidence_status, evidence_reason = 'SKIPPED', policy.get('reason') or 'USER_SKIPPED'
        open_items.append('EVIDENCE_ANALYSIS_SKIPPED')
    else:
        saved = _assessments_evidence(review, source_sha256)
        if saved:
            evidence_status, evidence_reason = 'SAVED', None
        else:
            evidence_status, evidence_reason = 'PENDING', 'INTERNAL_EVIDENCE_ANALYSIS_REQUIRED'
            open_items.append('INTERNAL_EVIDENCE_ANALYSIS_REQUIRED')
    evidence_list = []
    if evidence_status == 'SKIPPED':
        evidence_list.append(_evidence('WORKFLOW_BRANCH', source_sha256=source_sha256,
                                       branch='NO_INTERNAL_EVIDENCE' if mode == 'NONE' else 'USER_SKIPPED'))
    elif evidence_status == 'SAVED':
        evidence_list.append(_assessments_evidence(review, source_sha256))
    stages.append(_stage('EVIDENCE_ANALYSIS', evidence_status, evidence=evidence_list,
                         reason=evidence_reason,
                         branch='NO_INTERNAL_EVIDENCE' if mode == 'NONE' else 'INTERNAL_EVIDENCE'))

    review_sha = review.get('review_sha256') if review else None
    delivery = selected.get('DELIVERY')
    delivery_current = bool(delivery and delivery.get('baseline_sha256') == items.get('artifact_sha256'))
    check = check or {}
    basis = check.get('basis') or {}
    check_current = bool(check and basis.get('source_sha256') == source_sha256
                         and basis.get('map_sha256') == mapping.get('artifact_sha256')
                         and basis.get('items_sha256') == items.get('artifact_sha256')
                         and basis.get('review_sha256') == review_sha)
    check_delivery = False
    if check_current and check.get('delivery_xlsx_sha256'):
        try:
            delivery_dir = Path(delivery['path']).parent
            check_delivery = (file_sha256(delivery_dir / 'matrix.xlsx') == check.get('delivery_xlsx_sha256'))
        except (OSError, KeyError, TypeError):
            check_delivery = False
    check_passed = bool(check_current and check.get('status') == 'PASSED'
                        and check.get('review_verified') is True
                        and check.get('current_delivery_registered') is True
                        and delivery_current and check_delivery)
    if not baseline_current:
        check_status, check_reason = 'BLOCKED', 'ITEMS_REQUIRED'
    elif unprocessed:
        check_status, check_reason = 'BLOCKED', 'UNPROCESSED_SOURCE_SCOPE'
    elif not review:
        check_status, check_reason = 'PENDING', 'SELF_CHECK_NOT_RUN'
    elif check_passed:
        check_status, check_reason = 'SAVED', None
    elif check and not check_current:
        check_status, check_reason = 'BLOCKED', 'SELF_CHECK_BASIS_CHANGED'
        open_items.append('SELF_CHECK_BASIS_CHANGED')
    elif check:
        check_status, check_reason = 'BLOCKED', 'SELF_CHECK_FAILED'
        open_items.append('SELF_CHECK_FAILED')
    else:
        check_status, check_reason = 'PENDING', 'SELF_CHECK_NOT_RUN'
    check_evidence = ([_evidence('WORKFLOW_CHECK', source_sha256=source_sha256,
                                 artifact_sha256=check.get('check_sha256'))]
                      if check_status == 'SAVED' else None)
    stages.append(_stage('SELF_CHECK', check_status, evidence=check_evidence,
                         reason=check_reason))

    if not baseline_current:
        delivery_status, delivery_reason = 'BLOCKED', 'ITEMS_REQUIRED'
    elif delivery_current:
        delivery_status, delivery_reason = 'SAVED', None
    else:
        delivery_status, delivery_reason = 'PENDING', 'DELIVERABLE_NOT_SAVED'
    delivery_evidence = ([_evidence('DELIVERY', source_sha256=source_sha256,
                                    artifact_sha256=delivery.get('artifact_sha256'))]
                         if delivery_status == 'SAVED' else None)
    stages.append(_stage('DELIVERABLE', delivery_status, evidence=delivery_evidence,
                         reason=delivery_reason))

    for operation in incomplete_operations or []:
        state = operation.get('state')
        if state in {'STARTED', 'FAILED', 'BLOCKED', 'SAVED_PENDING_RECOVERY'}:
            open_items.append('INCOMPLETE_OPERATION')
            break
    blocking = [item for item in open_items if item in {
        'SOURCE_MAP_REQUIRED', 'ITEMS_REQUIRED', 'MAP_CONFIRMATION_REQUIRED',
        'SELF_CHECK_REQUIRED', 'WORKFLOW_EVIDENCE_CONFLICT', 'INCOMPLETE_OPERATION'}]
    next_stage = next((stage['id'] for stage in stages
                       if stage['status'] in {'PENDING', 'IN_PROGRESS', 'WAITING', 'BLOCKED'}), None)
    ready = (delivery_status == 'SAVED' and check_passed
             and all(stage['status'] in {'SAVED', 'SKIPPED'} for stage in stages)
             and not blocking)
    return {
        'schema': STATE_SCHEMA,
        'workflow': {'id': spec['id'], 'version': spec['version'],
                     'definition_sha256': spec['definition_sha256'],
                     'brief_sha256': spec['brief_sha256'],
                     'evidence_mode': mode},
        'source_sha256': source_sha256,
        'stages': stages,
        'next_stage': next_stage,
        'ready_to_review': ready,
        'verification': 'SAVED_ARTIFACTS_ONLY',
        'open_items': sorted(set(open_items)),
        'human_acceptance': 'NOT_CLAIMED',
    }
