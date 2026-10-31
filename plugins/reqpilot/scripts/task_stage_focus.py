"""Project an actionable, non-authoritative focus from verified task state.

The projection is guidance only. It never writes task files, calls a model,
grants authorization or changes ``ready_to_review``. All values come from the
already projected workflow, current selections and their version hashes.
"""
from __future__ import annotations

SCHEMA = 'reqpilot-stage-focus/v1'
AUTHORITY = 'GUIDANCE_ONLY'
CAPABILITY_ROOT = 'FROZEN_CAPABILITY_ROOT'
MODES = ('WORK', 'WAIT', 'RECOVER', 'COMPLETE')
TOOL_ACTIONS = {
    'source_prepare': 'prepare',
    'source_read': 'read',
    'map_apply': 'map',
    'items_apply': 'items',
    'review_prepare': 'review',
    'analysis_save': 'analysis',
    'decomposition_apply': 'decomposition',
    'task_check': 'check',
    'task_deliver': 'deliver',
    'task_describe': 'describe',
    'task_inspect': 'inspect',
    'task_recover': 'recover',
    'workbench_delivery_read': 'delivery_read',
}
PAIR_BINDING_MODES = frozenset({
    'PROJECT_REFERENCES', 'TEAM_KNOWLEDGE', 'COMBINED', 'ADOPTED_KNOWLEDGE'})
PAIR_ANALYSIS_TOOL = 'knowledge_analysis_save'
PAIR_ANALYSIS_ROUTE = 'reqpilot_knowledge.' + PAIR_ANALYSIS_TOOL
PAIR_ADOPTED_TOOLS = (
    'knowledge_adopted_status', 'knowledge_adopted_browse',
    'knowledge_adopted_read', 'knowledge_adopted_evidence')
PAIR_DOCUMENT_TOOLS = (
    'knowledge_evidence_search', 'knowledge_evidence_read', 'knowledge_topic_read')
PAIR_KNOWLEDGE_TOOLS = frozenset(
    PAIR_ADOPTED_TOOLS + PAIR_DOCUMENT_TOOLS + (PAIR_ANALYSIS_TOOL,))
STAGE_METHOD = {
    'PREPARATION': {
        'role_id': 'source-map',
        'skill_id': 'reqpilot-source-map',
        'objective': '核对当前原件读取与任务版本，准备可回查的来源材料。',
        'recommended_tools': ['source_prepare', 'source_read', 'task_inspect', 'task_describe'],
        'exit_evidence': ['当前版本已保存的读取/捕获证据', '后续结构编制可用的稳定来源引用'],
    },
    'SOURCE_MAP': {
        'role_id': 'source-map',
        'skill_id': 'reqpilot-source-map',
        'objective': '按真实原序整理可回查的 Document Map。',
        'recommended_tools': ['source_read', 'map_apply', 'task_describe', 'task_inspect'],
        'exit_evidence': ['程序保存的当前版本 Document Map', '已说明的覆盖范围与未决来源问题'],
    },
    'ITEMS_CLASSIFICATION': {
        'role_id': 'source-items',
        'skill_id': 'reqpilot-source-items',
        'objective': '在完整原文范围内保存原文条目与分类；工程派生只按明确授权范围另行处理。',
        'recommended_tools': ['source_read', 'items_apply', 'task_describe', 'task_inspect'],
        'exit_evidence': ['程序保存的当前版本原文条目与分类联合事实',
                          '未覆盖原文范围及仍待明确授权的派生'],
    },
    'EVIDENCE_ANALYSIS': {
        'role_id': 'review',
        'skill_id': 'reqpilot-review',
        'objective': '基于当前冻结依据给出可核对的分析建议、适用条件与缺口。',
        'recommended_tools': ['source_read', 'analysis_save', 'review_prepare', 'task_describe'],
        'exit_evidence': ['绑定当前 Map、条目与冻结依据的分析工件',
                          '每个分析单元的真实引用或明确缺口'],
    },
    'SELF_CHECK': {
        'role_id': 'verify',
        'skill_id': 'reqpilot-verify',
        'objective': '按当前版本核对来源、条目、分析与交付，并修正可修问题。',
        'recommended_tools': ['task_check', 'source_read', 'task_inspect', 'task_describe'],
        'exit_evidence': ['程序保存的当前版本自查结果', '已修正问题与明确保留的未决范围'],
    },
    'DELIVERABLE': {
        'role_id': 'verify',
        'skill_id': 'reqpilot-verify',
        'objective': '从当前版本导出一致的可核对成果并报告剩余范围。',
        'recommended_tools': ['task_check', 'task_deliver', 'workbench_delivery_read', 'task_inspect'],
        'exit_evidence': ['当前版本程序导出工件', '覆盖范围、自查结论与剩余事项'],
    },
}
DERIVATION_METHOD = {
    'role_id': 'review',
    'skill_id': 'reqpilot-review',
    'objective': '完成已授权但未决的原文派生，保留父项、依据与剩余授权范围。',
    'recommended_tools': ['source_read', 'decomposition_apply', 'task_describe', 'task_inspect'],
    'exit_evidence': ['程序保存的当前版本派生决策与父项',
                      '已授权范围完成或明确保留的未决项'],
}
UNRESOLVED_STATES = {'STARTED', 'SAVED_PENDING_RECOVERY', 'UNKNOWN'}


def _request(tool, payload):
    from task_service import REQUESTS
    REQUESTS[TOOL_ACTIONS[tool]].model_validate(payload)
    return {'tool': tool, 'request': payload}


def _stage_ids(workflow):
    values = [item.get('id') for item in workflow.get('stages') or []
              if isinstance(item, dict) and item.get('id') in STAGE_METHOD]
    return tuple(values) or tuple(STAGE_METHOD)


def _current_stage(workflow, stage_ids):
    candidate = workflow.get('next_stage')
    if candidate in stage_ids:
        return candidate
    for item in workflow.get('stages') or []:
        if item.get('status') in {'PENDING', 'IN_PROGRESS', 'WAITING', 'BLOCKED'}:
            if item.get('id') in stage_ids:
                return item['id']
    return stage_ids[-1]


def _unresolved_operations(state):
    operations = state.get('incomplete_operations') or state.get('unresolved_operations') or []
    return [item for item in operations
            if isinstance(item, dict) and item.get('state') in UNRESOLVED_STATES]


def _stage_record(workflow, stage):
    return next((item for item in workflow.get('stages') or []
                 if isinstance(item, dict) and item.get('id') == stage), {})


def _review_sha(state):
    working = state.get('current_working') or {}
    return (working.get('review_sha256')
            if working.get('owner') == 'portable_review' else None)


def _check_matches_current(check, state, selected):
    basis = (check or {}).get('basis') or {}
    mapping = selected.get('MAP') or {}
    items = selected.get('ITEMS') or {}
    source = ((state.get('context') or {}).get('source') or {}).get('sha256')
    return bool(check) and all((
        basis.get('source_sha256') == source,
        basis.get('map_sha256') == mapping.get('artifact_sha256'),
        basis.get('items_sha256') == items.get('artifact_sha256'),
        basis.get('review_sha256') == _review_sha(state),
    ))


def _basis(state, workflow):
    context = state.get('context') or {}
    source = (context.get('source') or {}).get('sha256') or workflow.get('source_sha256')
    selected = state.get('selected') or {}
    mapping = selected.get('MAP') or {}
    items = selected.get('ITEMS') or {}
    value = {'task_revision': state.get('revision'), 'source_sha256': source}
    if mapping.get('artifact_sha256'):
        value['map_sha256'] = mapping['artifact_sha256']
    if (items.get('artifact_sha256') and mapping.get('artifact_sha256')
            and items.get('map_sha256') == mapping.get('artifact_sha256')):
        value['items_sha256'] = items['artifact_sha256']
    working = state.get('current_working') or {}
    if working.get('owner') == 'portable_review' and working.get('review_sha256'):
        value['review_sha256'] = working['review_sha256']
    binding = state.get('knowledge_evidence') or {}
    if binding.get('binding_sha256'):
        value['knowledge_binding_sha256'] = binding['binding_sha256']
    return value


def _knowledge_binding(state):
    summary = state.get('knowledge_evidence') or {}
    binding = (
        (state.get('context') or {}).get('request') or {}
    ).get('knowledge_evidence')
    if not isinstance(binding, dict):
        return None
    summary_sha = summary.get('binding_sha256')
    if summary_sha and summary_sha != binding.get('binding_sha256'):
        return None
    return binding


def _knowledge_mode(state):
    summary = state.get('knowledge_evidence') or {}
    if summary.get('mode') in PAIR_BINDING_MODES:
        return summary['mode']
    binding = _knowledge_binding(state) or {}
    return binding.get('mode') if binding.get('mode') in PAIR_BINDING_MODES else None


def _first_document_id(binding):
    project = (binding or {}).get('project') or {}
    documents = project.get('documents') or []
    if documents:
        return documents[0].get('documentId')
    team = (binding or {}).get('team') or {}
    documents = team.get('documents') or []
    if documents:
        return documents[0].get('documentId')
    return None


def _external_action(tool, request):
    return {'tool': tool, 'request': request}


def _knowledge_analysis_route(state):
    mode = _knowledge_mode(state)
    if mode is None:
        return None
    binding = _knowledge_binding(state) or {}
    status = (state.get('knowledge_evidence') or {}).get('status') or ''
    if mode == 'ADOPTED_KNOWLEDGE':
        objective = ('仅通过 Host 的已注册知识库冻结版本读取事实，并用 '
                     + PAIR_ANALYSIS_ROUTE + ' 保存带 Host attestation 的分析建议；'
                     '不读取包路径、不切换库或版本，不以普通 analysis_save、'
                     '自造 attestation 或放宽守卫替代。')
        tools = ['source_read', *PAIR_ADOPTED_TOOLS, PAIR_ANALYSIS_TOOL]
        action = _external_action('knowledge_adopted_status', {})
    else:
        objective = ('仅通过 Host 的项目/团队冻结依据工具读取事实，并用 '
                     + PAIR_ANALYSIS_ROUTE + ' 保存带 Host attestation 的分析建议；'
                     '不搜索范围外库，不以普通 analysis_save、自造 attestation '
                     '或放宽守卫替代。')
        tools = ['source_read', *PAIR_DOCUMENT_TOOLS, PAIR_ANALYSIS_TOOL]
        team = binding.get('team') or {}
        document_id = _first_document_id(binding)
        if team.get('kind') == 'TOPIC':
            action = _external_action('knowledge_topic_read', {})
        elif document_id:
            action = _external_action('knowledge_evidence_read', {
                'documentId': document_id, 'firstPage': 1, 'count': 4})
        else:
            action = _external_action('knowledge_evidence_search', {
                'query': 'current task evidence', 'limit': 8})
    if status and status != 'CURRENT':
        objective += ' 当前绑定状态为 ' + str(status) + '；证据不可用时保持未决，不伪造通过。'
    return {
        'objective': objective,
        'recommended_tools': tools,
        'exit_evidence': [
            'Host 证据工具返回且与当前冻结绑定一致的真实 fact_id/quote',
            '带 Host attestation、绑定当前 revision 的已保存分析工件',
        ],
        'next_action': action,
    }


def _read_requests(state, selected, task_id):
    mapping = selected.get('MAP') or {}
    items = selected.get('ITEMS') or {}
    result = []
    if mapping.get('artifact_sha256'):
        result.append(_request('source_read', {
            'task_id': task_id, 'view': 'scope',
            'expected_map_sha256': mapping['artifact_sha256'], 'limit': 100}))
    if (items.get('artifact_sha256') and mapping.get('artifact_sha256')
            and items.get('map_sha256') == mapping.get('artifact_sha256')):
        result.append(_request('source_read', {
            'task_id': task_id, 'view': 'items',
            'expected_map_sha256': mapping['artifact_sha256'],
            'expected_baseline_sha256': items['artifact_sha256']}))
    if not mapping.get('artifact_sha256') and selected.get('READING'):
        result.append(_request('source_read', {
            'task_id': task_id, 'view': 'source', 'limit': 20}))
    return result


def _next_action(mode, stage, state, selected, task_id, revision, derivation=False,
                 analysis_action=None):
    if mode == 'RECOVER':
        return _request('task_recover', {'task_id': task_id})
    if mode == 'WAIT':
        return _request('task_inspect', {'task_id': task_id})
    if mode == 'COMPLETE':
        return None
    mapping = selected.get('MAP') or {}
    if stage in {'PREPARATION', 'SOURCE_MAP'} and not mapping.get('artifact_sha256'):
        if selected.get('READING'):
            return _request('source_read', {'task_id': task_id, 'view': 'source', 'limit': 20})
        return _request('source_prepare', {'task_id': task_id, 'expected_revision': revision})
    if stage == 'ITEMS_CLASSIFICATION':
        return _request('task_describe', {
            'task_id': task_id, 'action': 'decomposition' if derivation else 'items',
            'expected_revision': revision})
    if stage == 'EVIDENCE_ANALYSIS':
        if analysis_action is not None:
            return analysis_action
        return _request('task_describe', {
            'task_id': task_id, 'action': 'analysis', 'expected_revision': revision})
    if stage == 'SELF_CHECK':
        if _review_sha(state) is None:
            return _request('review_prepare', {
                'task_id': task_id, 'expected_revision': revision})
        check = state.get('workflow_check') or {}
        if (_check_matches_current(check, state, selected)
                and check.get('delivery_status') == 'NOT_REGISTERED'):
            return _request('task_deliver', {
                'task_id': task_id, 'expected_revision': revision})
        return _request('task_check', {'task_id': task_id})
    if stage == 'DELIVERABLE':
        return _request('task_deliver', {'task_id': task_id, 'expected_revision': revision})
    return _request('task_describe', {
        'task_id': task_id, 'action': 'read', 'expected_revision': revision})


def build_stage_focus(state):
    """Return the optional focus package for a fixed-workflow task, else None."""
    workflow = state.get('workflow')
    if not isinstance(workflow, dict) or not workflow.get('stages'):
        return None
    stage_ids = _stage_ids(workflow)
    selected = state.get('selected') or {}
    task_id = (state.get('context') or {}).get('work_id')
    revision = state.get('revision')
    open_items = [item for item in workflow.get('open_items') or [] if isinstance(item, str)]
    unresolved = _unresolved_operations(state)
    derivation = False
    analysis_action = None
    if unresolved:
        mode = 'RECOVER'
        stage = _current_stage(workflow, stage_ids)
        method = dict(STAGE_METHOD[stage])
        method.update(
            objective='先核对未完成操作的真实状态，避免重复执行未知写入。',
            recommended_tools=['task_recover', 'task_inspect', 'task_describe'],
            exit_evidence=['按原冲突检查完成的操作对账结果',
                           '未完成操作不再处于未知/待恢复状态'])
    elif 'MAP_CONFIRMATION_REQUIRED' in open_items:
        mode = 'WAIT'
        stage = 'SOURCE_MAP'
        method = dict(STAGE_METHOD[stage])
        method.update(
            objective='当前 Map 已保存且确认被要求；等待已保存的确认，不提前进入条目。',
            recommended_tools=['task_inspect', 'source_read', 'task_describe'],
            exit_evidence=['宿主保存的当前 Map 确认记录', '确认前的工作流等待事实'])
    elif workflow.get('ready_to_review') is True:
        mode = 'COMPLETE'
        stage = stage_ids[-1]
        method = dict(STAGE_METHOD[stage])
        method.update(
            objective='固定工作流已有可审阅成果，可以停止；不代表人工接受。',
            recommended_tools=['task_inspect', 'workbench_delivery_read'],
            exit_evidence=['当前版本 ready_to_review 由保存工件和程序检查成立',
                           '可停止；人工接受为 NOT_CLAIMED'])
        derivation = False
    else:
        mode = 'WORK'
        stage = _current_stage(workflow, stage_ids)
        stage_record = _stage_record(workflow, stage)
        derivation = bool(
            stage == 'ITEMS_CLASSIFICATION'
            and ('SCOPED_DERIVATION_REMAINING' in open_items
                 or stage_record.get('reason') == 'SCOPED_DERIVATION_REMAINING'))
        method = dict(DERIVATION_METHOD if derivation else STAGE_METHOD[stage])
        if stage == 'EVIDENCE_ANALYSIS' and not derivation:
            route = _knowledge_analysis_route(state)
            if route:
                method.update(
                    objective=route['objective'],
                    recommended_tools=route['recommended_tools'],
                    exit_evidence=route['exit_evidence'])
                analysis_action = route['next_action']
    return {
        'schema': SCHEMA,
        'authority': AUTHORITY,
        'mode': mode,
        'stage_id': stage,
        'role_id': method['role_id'],
        'objective': method['objective'],
        'basis': _basis(state, workflow),
        'recommended_tools': list(method['recommended_tools']),
        'skill': {'id': method['skill_id'],
                  'path': 'skills/' + method['skill_id'] + '/SKILL.md',
                  'relative_to': CAPABILITY_ROOT},
        'read_requests': _read_requests(state, selected, task_id),
        'exit_evidence': list(method['exit_evidence']),
        'remaining_reasons': open_items[:8],
        'next_action': _next_action(mode, stage, state, selected, task_id, revision,
                                    derivation, analysis_action),
    }
