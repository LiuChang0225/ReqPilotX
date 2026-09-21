"""Host facts for an Agent's assignment; never infer authorization from artifacts."""
from urllib.parse import urlencode


def build_task_brief(*, goal, origin, source=None, stages=None, review=None,
        require_map_confirmation=None, confirmed_map_sha256=None, analysis_policy=None,
        mode='SOURCE', initial_goal=None, timeout_seconds=None, focus_ids=(),
        project_id=None, document_id=None, retained_stages=()):
    stages, review = stages or {}, review or {}
    mapping = stages.get('MAP'); items = stages.get('ITEMS')
    if mapping and items and items.get('map_sha256') != mapping.get('artifact_sha256'):
        items = None  # An older item snapshot does not advance this Map.
    confirmed = bool(mapping and mapping.get('artifact_sha256') and confirmed_map_sha256 == mapping.get('artifact_sha256'))
    confirmation = ('NOT_CONFIGURED' if require_map_confirmation is None else
        'NOT_REQUIRED' if not require_map_confirmation else
        'REQUIRED_AFTER_MAP' if not mapping else 'CONFIRMED' if confirmed else 'REQUIRED_BEFORE_ITEMS')
    if mapping and confirmation == 'REQUIRED_BEFORE_ITEMS':
        state, label = 'MAP_DRAFT', '结构草稿已保存，仍可自查/局部修订；进入条目前需确认这一版'
    elif review or mode == 'REVIEW':
        state, label = 'WORKING_REVIEW', '已有工作版；自主核对并完成本轮交付，有实际修改时再修订'
    elif items:
        state, label = 'ITEMS_SAVED', '条目草稿已保存；核对后处理本轮已授权的拆分、分析或交付'
    elif mapping:
        state, label = 'MAP_SAVED', '结构已保存；自查后按本轮授权继续条目、必要拆分和交付'
    else:
        state, label = 'SOURCE_TO_MAP', '从原件恢复结构，首个交付是有实际内容的可核对Map'
    artifacts=[]
    for stage, value in [('MAP',mapping),('ITEMS',items)]:
        if value:
            artifacts.append({'stage':stage, **{key:value[key] for key in
                ('path','producer_path','stage_version_id','count','artifact_sha256','map_sha256') if key in value},
                **({'path_role':'READABLE_STAGE_SNAPSHOT',
                    'producer_path_role':'ORIGINAL_COMPILER_OUTPUT; verify its Map/source before reuse'}
                   if value.get('producer_path') else {})})
    retained_artifacts = [{key:value[key] for key in
        ('stage','path','producer_path','stage_version_id','artifact_sha256','map_sha256',
         'count','current','pairing_status','retention_reason') if key in value}
        for value in retained_stages]
    path = None
    if project_id and document_id:
        path='/workspace?'+urlencode({'task':'extraction','mode':'organize','checkpoint':'list' if (review or items) and state != 'MAP_DRAFT' else 'structure',
            'projectId':project_id,'documentId':document_id})
    return {'schema_version':'reqpilot-task-brief/v1','current_request':goal,'request_origin':origin,
        **({'initial_request':initial_goal} if initial_goal and initial_goal != goal else {}),
        'source':source or {},'work_state':state,'work_state_label':label,'saved_artifacts':artifacts,
        'retained_stage_artifacts':retained_artifacts,
        'review_version':{k:review[k] for k in ('version_path','baseline_sha256','document_map_sha256') if k in review},
        'map_confirmation':confirmation,'confirmed_map_sha256':confirmed_map_sha256 if confirmed else None,
        'analysis_policy':analysis_policy or {'status':'UNKNOWN'},
        'focus_source_item_ids':list(focus_ids),'workbench_path':path,
        'run_limit_seconds':timeout_seconds,
        'authorization_rule':'已有成果和可用工具不扩张授权；以本轮请求及其后新反馈为准。只要求Map/局部修订时，完成该范围即交接。',
        'handoff':['实际保存的成果及可读取位置','本轮覆盖/修正与未处理范围','有来源依据的自查结论及未决事项','仅在必要时列出缺少的输入与受影响范围；人工反馈不是默认完成前提'],
        'ownership':'阶段流程、职责与反馈规则见已显式提供的reqpilot主skill；本简报只报告宿主掌握的任务事实。'}


def save_task_brief(task_root, brief, goal):
    """Retain the exact assignment, including for the first turn in a new task."""
    import json
    import os
    from uuid import uuid4
    from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
    directory = task_root / 'task-briefs'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (canonical_json_sha256([brief, goal]) + '.json')
    # Assignment facts are shared with the portable CLI. Persisting them must
    # not pull the App Server driver and its model-host dependencies into it.
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump({'task_brief': brief, 'sent_goal': goal}, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def render_task_goal(goal, *, origin, brief=None, reading_v1=True):
    """The core mission is supplied once through the explicitly mounted skill."""
    lines=['# 本轮任务简报','先依据已提供的reqpilot主skill理解业务用途、角色分工和流程；本轮只执行下面的授权范围。',
        '## 本轮请求（完整原话）',goal, '目标来源：'+origin]
    if brief:
        lines += ['## 宿主已知事实（不是语义验收）',brief['work_state_label']]
        source=brief.get('source',{})
        if source.get('path'):lines.append('原件：'+source['path']+'；精确版本与内部依据目录见task-input或当前review。')
        confirmation_text = {
            'NOT_CONFIGURED':'宿主未提供结构化确认设置；遵守本轮明确要求，不自行新增审批或声称已确认。',
            'NOT_REQUIRED':'未设置强制结构确认；核对后仍只执行本轮已授权的后续工作。',
            'REQUIRED_AFTER_MAP':'先编制和自查结构；进入条目前需要对这一版Map的确认记录。',
            'REQUIRED_BEFORE_ITEMS':'当前Map尚未确认；仍可自查和局部修订，范围达到可审阅状态后交接。',
            'CONFIRMED':'当前Map已有匹配确认；后续仍按本轮授权执行。'}
        lines.append('结构确认：'+confirmation_text[brief['map_confirmation']])
        policy=brief['analysis_policy'];status=policy.get('status','UNKNOWN')
        policy_text = ('已登记内部资料，可在授权后检索适用依据；不等于每项都有依据或已经分析。' if status=='AVAILABLE' else
            '本轮跳过符合性分析（'+str(policy.get('reason') or '按已保存策略')+'）；原文整理及其他授权工作继续。' if status=='SKIPPED' else
            '简报中尚无依据策略；读取task-input或当前review，不能推断资料存在或分析已完成。')
        lines.append('内部依据与分析：'+policy_text)
        for item in brief['saved_artifacts']:
            lines.append(f"已保存{item['stage']}：{item.get('count','未知数量')}项，位置{item.get('path','见reqpilot_context')}；保存不等于内容已正确。")
            if item.get('producer_path'):
                lines.append('上面的位置是只读阶段快照；原编制输出：'+item['producer_path']+
                    '。编译所需完整依赖仍在原编制目录；复用前核对原件与Map摘要，不能把阶段快照目录当成完整编制包。')
        for item in brief.get('retained_stage_artifacts', []):
            lines.append(f"另存{item['stage']}（非当前阶段）：{item.get('count','未知数量')}项，位置{item.get('path','见任务历史')}；"
                f"配套Map {item.get('map_sha256','未登记')}，保留原因{item.get('retention_reason','未说明')}。"
                '这些成果仍可回查，不因其非当前而重做或丢弃；也不代表当前阶段已完成。')
        if brief['review_version']:lines.append('工作版：'+str(brief['review_version'].get('version_path','见当前review manifest')))
        if brief['focus_source_item_ids']:lines.append('修改焦点：'+', '.join(brief['focus_source_item_ids'])+'。这是修改范围，不是原件读取白名单。')
        if brief.get('initial_request'):lines += ['原任务目标仅供背景识别，本轮请求及随后反馈优先：',brief['initial_request']]
        if brief['workbench_path']:lines.append('应用工作台路由：'+brief['workbench_path']+'。应用负责展示；不要声称自己已操作浏览器。')
        if brief['run_limit_seconds'] is not None:
            lines.append(f"宿主本轮运行上限：{brief['run_limit_seconds']}秒（不是剩余时间，也不是应耗满的目标）。先保存有意义范围，不在无新证据的自查中耗尽预算。")
    lines += ['## 本轮执行与交接',
        '从reqpilot_context开始核对当前材料、版本与任务状态；新原件再读task-input.json，已有工作版读当前review与反馈。',
        '按当前阶段选择专用skill和工具；读取、回查、补读及局部修订方法由你决定。结构阶段先用reqpilot-source-map，后续手册按实际进入的阶段读取。',
        '若本轮只要求结构或局部成果，完成并自查该范围后停止；全流程路线不构成额外授权。',
        '结构确认已被要求时，自查达到可审阅状态后交接准确版本；等待宿主保存的确认再进入条目。WAITING_FOR_MAP_CONFIRMATION不是重试指令。',
        '默认按授权自主阅读、编制、自查、修正并完成草稿交付，不等待用户指出错误。渐进保存和展示供用户随时介入，不等同要求人工批准每阶段。',
        '实质歧义先回查原件及已提供资料；仍无法解决时保留定位、影响和未决项，继续完成其余可用范围。只对无法自行取得且确实阻断的输入提出具体请求，不把无KB答复变成源整理停工条件。用户新反馈优先，保护未受影响结果及已有人工意见。',
        '分析策略SKIPPED时跳过符合性分析，不造空证据评估。只有获准交付时才使用程序导出同版本Matrix；不重新让模型读取Excel。',
        '结束前报告实际成果/位置、覆盖与变化、自查及未决项、需要的具体反馈和之后的动作；程序成功、源语义核对与人工接受分别表达。用简体中文报告，保留客户原文语言。']
    if reading_v1:
        lines.append('PDF优先复用逐页MD、原页/局部图像及完整表格。解析候选不限制观察；出现实际读取/表达问题时按工具指引处理，不编造原文或定位。')
    return '\n\n'.join(lines)
