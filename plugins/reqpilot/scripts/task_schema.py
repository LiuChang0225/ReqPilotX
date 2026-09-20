"""Bounded, read-only discovery derived from the exact task request models.

JSON Schema owns field types. Small workflow notes describe validator conditions
that JSON Schema alone cannot express; this module never advances a task.
"""
from copy import deepcopy
import hashlib
import json

PURPOSES = {
    'open':'创建原件绑定任务，或凭 task_id 接续现有任务。', 'inspect':'读取当前任务版本、成果与待办。',
    'prepare':'保存原件捕获、逐页阅读材料或有界本地OCR；数字重读保留差异，表格保留单元格。', 'read':'读取原文、页图、条目、scope 与真实反馈。',
    'visual_revise':'回查原图后纠正视觉转写或表格单元格；保留原始OCR和旧结果，返回可编制来源。',
    'map':'按来源证据编制或修改 Document Map。', 'items':'为尚未覆盖的原文 scope 保存条目判断。',
    'images':'识别真正独立的图像条目；辅助图仍随所属条款或表格。',
    'annotations':'保存原编号、主题、上下文或图片释读。', 'decomposition':'编制已授权的 SPLIT/KEEP/UNDO。',
    'review':'形成可修订工作版；不需要先造一个无意义 candidate。',
    'revise':'修订已有条目的分类、scope、释读或 annotation，并交付同版 Excel。',
    'check':'核对来源、当前工作版、已登记交付与工作台；不代替语义审阅。',
    'deliver':'程序导出当前版本并登记首次同版交付。', 'sync':'取回反馈，或显式补同步已保存的当前成果。',
    'recover':'恢复已有工件登记；不盲目重跑未知执行。',
}
NOTES = {
    'task_id':'使用 task_open 返回的持久 task_id；不猜测 latest 任务。',
    'expected_revision':'使用本任务实际回执中的完整 revision；发生变化时重新读取，不拼接 token。',
    'command_id':'同一逻辑请求重试保持同键同内容；省略时服务可按请求生成键。',
    'source_item_id':'接受当前基线的 SRC 显示号或稳定 source-record ID；不接受 REQ、行号或数组序号。',
    'source_record_id':'接受当前基线的 SRC 显示号或稳定 source-record ID，由 binder 解析。',
    'source_item_ids':'仅使用当前基线实际返回的 SRC 显示号或稳定 source-record ID。',
    'source_node_id':'使用当前 capture 的零基 ref 或真实 source_node_id；这是原件证据引用，不是 SRC 业务号。',
    'source':'在 Map 选择器中使用当前 capture 的 ref/source_node_id；open.source 则是实际原件路径。',
    'content_ref':'使用该精确 scope 内的 content_ref 索引或 content_id；quote/start/end_before 必须唯一匹配原文。',
    'scope_id':'使用 source_read 返回的真实 scope_id；不把原始章节编号当作 scope_id。',
    'logical_table_scope_id':'使用当前 Map 的真实逻辑表 scope；保留表头、合并关系和范围。',
    'node_id':'Map 结构对象 ID；读取 image_scope 时使用已存在的 Map node_id，不是 capture ref 或 SRC。',
    'content_id':'使用该 scope 实际返回的内容块 ID，不把 source_node_id 或章节编号当作 content_id。',
    'fragment_id':'使用逻辑表请求中的真实 fragment_id；保留原表片段归属。',
    'cell_id':'使用实际表格片段内的 cell_id；不是可见行号或 Excel 导出坐标。',
    'row_start':'采用实际表格请求的零基行位置，半开范围起点包含；不要由原文序号猜行索引。',
    'row_end':'半开范围终点不包含，并且必须大于 row_start。',
    'row_range':'二元零基半开范围 [start,end)；表头和数据行依据真实表格请求定位。',
    'fragment_ref':'当前逻辑表请求内的短整数片段索引，与 fragment_id 二选一；不是原文行号。',
    'record_key':'初次编制可省略，程序按解析后的表格行范围生成。修订已有条目必须保留原 record_key；显式值和冲突别名不被覆盖。',
    'item_key':'初次编制可省略，程序按解析后的原文范围生成。修订已有条目必须保留原 item_key；键不决定分类或颗粒度。',
    'reason_codes':'使用小写短码；连字符由程序统一为下划线并排序去重。自然语言理由写入 decision_summary。',
    'segment_index':'使用 decomposition_packet 的真实零基 segment_index。',
    'quote':'必须在选定的原文块/segment 内精确且唯一匹配；不改写后再反查。',
    'image_anchor_ids':'使用 source_read 返回的真实 image_anchor_id；文件名或显示号不能替代。',
    'image_anchor_id':'使用真实阅读回执中的图像 anchor；请求图像不等于已完成视觉解读。',
    'feedback_id':'必须引用真实已登记反馈；不编造 USER 意见。',
    'addressed_feedback_ids':'引用真实已登记反馈/自查发现；显式 self_review_finding 可由修订命令先登记再绑定。',
    'source_ranges':'引用 decomposition_packet 中实际 segment 和原文区间；程序解析引文与偏移。',
    'source_number_ref':'原编号必须绑定实际正文、Map 标题或表格单元格中的原文位置。',
}
COMMON_RULES = [
    '必填表示 JSON 字段必需；可省略不等于可以传 null。Map 中省略表示保留，空列表等显式值可能表示清空。',
    '字段、枚举、嵌套类型来自同一个 REQUESTS.model_json_schema；下列业务条件还由现有 validator/compiler 验证。',
    '请求骨架中的 null 是明确待填位置，不是假造的原件 ID 或判断；不要把未填骨架直接提交。',
]


def _dump(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))
def _sha(value):return hashlib.sha256(_dump(value).encode()).hexdigest()
def _pointer(schema, pointer):
    if not isinstance(pointer,str) or not pointer.startswith('#/'):
        if pointer=='#':return schema
        raise ValueError('Schema part must use a local JSON pointer beginning #/')
    value=schema
    for token in pointer[2:].split('/'):
        token=token.replace('~1','/').replace('~0','~')
        try:value=value[int(token)] if isinstance(value,list) else value[token]
        except (KeyError,ValueError,IndexError,TypeError) as exc:raise ValueError('Schema pointer does not exist: '+pointer) from exc
    return value
def _resolve(schema,node,seen=()):
    if not isinstance(node,dict):return node
    if '$ref' not in node:return node
    ref=node['$ref']
    if ref in seen:return {'recursive_reference':ref}
    resolved=_resolve(schema,_pointer(schema,ref),(*seen,ref))
    return {**resolved,**{k:v for k,v in node.items() if k!='$ref'},'_definition_ref':ref}
def _kind(schema,node):
    resolved=_resolve(schema,node)
    if resolved is True:return 'any JSON value'
    if resolved is False:return 'no value allowed'
    if 'recursive_reference' in resolved:return 'recursive '+resolved['recursive_reference']
    if 'const' in resolved:return 'literal '+_dump(resolved['const'])
    if 'enum' in resolved:return 'enum '+_dump(resolved['enum'])
    for key,separator in (('anyOf',' | '),('oneOf',' exactly one of '),('allOf',' & ')):
        if key in resolved:return separator.join(_kind(schema,part) for part in resolved[key])
    kind=resolved.get('type')
    if kind=='array':
        if 'prefixItems' in resolved:return 'tuple['+', '.join(_kind(schema,part) for part in resolved['prefixItems'])+']'
        return 'array<'+_kind(schema,resolved.get('items',True))+'>'
    if kind=='object' or 'properties' in resolved:
        return 'object'+(' '+resolved['_definition_ref'].rsplit('/',1)[-1] if '_definition_ref' in resolved else '')
    if isinstance(kind,list):return ' | '.join(kind)
    if isinstance(kind,str):return kind
    # Keep unusual JSON Schema compositions honest and directly inspectable.
    return 'JSON value constrained by '+', '.join(sorted(k for k in resolved if k not in {'title','description'}))
def _nullable(schema,node):
    node=_resolve(schema,node)
    if not isinstance(node,dict):return bool(node)
    if node.get('type')=='null' or 'null' in (node.get('type') if isinstance(node.get('type'),list) else []):return True
    return any(_nullable(schema,part) for part in node.get('anyOf',[])+node.get('oneOf',[]))
def _ref_links(node):
    refs=[]
    if isinstance(node,dict):
        if '$ref' in node:refs.append(node['$ref'])
        for key,value in node.items():
            if key!='$defs':refs.extend(_ref_links(value))
    elif isinstance(node,list):
        for value in node:refs.extend(_ref_links(value))
    return list(dict.fromkeys(refs))
def _fields(schema,node):
    resolved=_resolve(schema,node)
    if not isinstance(resolved,dict):return []
    required=set(resolved.get('required',[]));rows=[]
    for name,value in resolved.get('properties',{}).items():
        actual=_resolve(schema,value)
        field={'name':name,'required':name in required,'type':_kind(schema,value),'null_allowed':_nullable(schema,value)}
        for key in ('description','default','enum','const','minimum','maximum','exclusiveMinimum','exclusiveMaximum',
                    'minLength','maxLength','pattern','minItems','maxItems','uniqueItems'):
            if key in actual:field[key]=actual[key]
        if name in NOTES:field['reference_or_input_rule']=NOTES[name]
        refs=_ref_links(value)
        if refs:field['definitions']=[{'part':ref,'type':_kind(schema,_pointer(schema,ref))} for ref in refs]
        rows.append(field)
    return rows
def _facts(context,scope):
    context=context or {};inner=context.get('context') or {}
    task=context.get('task_id') or inner.get('work_id') or context.get('work_id')
    owner=context.get('current_owner') or (context.get('current_working') or {}).get('owner')
    facts={key:value for key,value in {'task_id':task,'revision':context.get('revision'),'current_owner':owner}.items() if value is not None}
    if context.get('selected') is not None:facts['available_stages']=list(context['selected'])
    elif context.get('available_stages') is not None:facts['available_stages']=context['available_stages']
    actual_scope={key:value for key,value in (scope or {}).items() if key in {'scope_id','scope_kind','logical_table_scope_id','source_item_id','node_id','page','content_ref'}}
    return facts,actual_scope


def _rules(action,facts):
    rules=list(COMMON_RULES)
    additions={
        'open':['create 需要实际 source、完整 goal 与稳定 creation_key；resume 只使用已有 task_id，保留旧策略。'],
        'map':['初次结构优先outline，用role/ref/content/children/tables表达原文层级，程序补稳定ID、父引用和明确编号；CLAUSE为编号正文，无独立标题段落放父content。', 'outline与非空nodes/logical_tables不混用；精确修改仍用nodes，省略不删除。', 'heading/content/numbered_body 与对应显式字段互斥；原文选择器用唯一引文，半开范围不能倒置。',
            'Map 表片段 row_range 已支持将规范十进制整数字符串无损转成整数；bool、小数、前导 0 不接受，实际范围仍由原校验决定。'],
        'items':['retain_map_structure=true由程序保留Map已标定标题及完整封面/目录/文控；不用逐段抄结构span。普通正文/图注/表格分组仍需Agent判断，已覆盖结构不会重复提交。', '只添加尚未覆盖的 scope；已有条目通过 review/revise 修订，不静默覆盖。',
            'TEXT 使用 text_decisions/items；TABLE 使用 table_decisions/records。初次编制 item_key/record_key 可省略或 null，由程序按来源范围生成；修订需保留已有键。表格可兼容单独的 item_key；两字段同时出现必须同值。',
            '表格行组的 fragment_ref（短整数）与 fragment_id 二选一。row_start/row_end 优先直接用整数；也接受规范十进制整数字符串的无损转换，拒绝 bool、小数和前导 0；半开范围仍校验。',
            '请求骨架不决定表格应分几组、哪些行组成需求；依据原表完整语义自行判断。'],
        'images':['独立图像条目的初次识别应在 review 形成前完成；已有图像用释读或已有条目修订。'],
        'annotations':['尚无 review 时可以直接编制 annotations，但 origin 必须是 AGENT_SELF_REVIEW，且不能带 resolutions/self_review_finding。',
            '如需登记 self_review_finding，先调用 review_prepare（下方给出当前 task/revision 的请求），再使用其实际新 revision；不会自动切换业务阶段。',
            '已有 review 时 annotations 走持久修订/导出/close；同一 command 的重放保留首次路线，不因后来形成 review 而换路。'],
        'decomposition':['SPLIT/KEEP/UNDO 必须落在实际用户授权的 SRC 范围；schema 说明不会授予拆分权限。',
            '无 review 时初次编制不能带 self_review_finding/resolutions/USER origin；需记录发现时先形成 review。'],
        'revise':['需要已有 review；scope replacement 必须提供真实 addressed_feedback_ids 或明确的 self_review_finding。',
            'self_review_finding 只允许 AGENT_SELF_REVIEW；程序先登记真实发现，再以该精确中间版本修订，丢响应重放不重复登记。',
            'USER origin 必须引用已登记的真实 USER 反馈；不把 Agent 自查写成用户意见。',
            '分类修改需要实际 model_id；scope replacement 自带分类，不和另一组 classification decisions 混用。'],
        'review':['需要已保存 ITEMS；首次形成 review 无需人为造 candidate，默认不等人工批准；明确要求的既有确认策略仍保留。',
            'analysis_policy 由请求真实情况选择；没有内部依据可明确 skip_no_internal_evidence，不能虚构符合性分析。'],
        'read':['view=part 可复制返回的read请求/handle；只有一个已登记capture时短整数ref可由程序补版本，多个版本仍需明确绑定。',
            'page/image 按页码或准确capture图像位置读取，缺页文/高清图会自动准备登记；继续写入使用这次返回的revision。',
            'max_chars超过24000时按24000返回并保留continuation；item需要当前SRC，长结果按next_read续读。'],
        'sync':['publish_current_review 需要当前 expected_revision；retry_publication 需要真实 operation_id，旧 producer 不会强行补发。'],
    }
    rules.extend(additions.get(action,[]))
    return {'current_owner':facts.get('current_owner'),'rules':rules,'stage_changed_by_describe':False}


def _skeleton(schema,node,path='',facts=None,scope=None,depth=0):
    facts=facts or {};scope=scope or {};node=_resolve(schema,node)
    field=path.rsplit('.',1)[-1]
    if field=='task_id':return facts.get('task_id')
    if field=='expected_revision':return facts.get('revision')
    if field in {'source_item_id','source_record_id'}:return scope.get('source_item_id')
    if field=='scope_id':return scope.get('scope_id')
    if field=='logical_table_scope_id':return scope.get('logical_table_scope_id') or (scope.get('scope_id') if scope.get('scope_kind')=='TABLE' else None)
    if 'const' in node:return node['const']
    if depth>5:return None
    if node.get('type')=='object' or 'properties' in node:
        return {key:_skeleton(schema,value,path+'.'+key,facts,scope,depth+1)
                for key,value in node.get('properties',{}).items() if key in node.get('required',[]) or 'const' in value}
    if node.get('type')=='array':
        return [_skeleton(schema,node.get('items',{}),path+'[]',facts,scope,depth+1)] if node.get('minItems',0)>0 else []
    return None
def _template(action,schema,facts,scope):
    from task_service import REQUESTS
    request=_skeleton(schema,schema,facts=facts,scope=scope)
    # Only choose transport/mechanical variants; semantic decisions stay blank.
    if action=='open':request={'mode':'create','source':None,'goal':None,'creation_key':None}
    if action=='prepare':request['mode']='capture'
    if action=='read':request['view']='source'
    recipes={'map':('decisions','outline'),'items':('decisions','text_decisions'),
        'annotations':('changes','annotation_updates'),'decomposition':('changes','decisions'),
        'revise':('changes','decisions'),'images':('changes','scopes')}
    if action in recipes:
        outer,collection=recipes[action]
        if action=='items' and scope.get('scope_kind')=='TABLE':collection='table_decisions'
        parent=_resolve(schema,schema['properties'][outer]);field=parent.get('properties',{}).get(collection)
        if field is not None:
            array=_resolve(schema,field)
            request.setdefault(outer,{})[collection]=[_skeleton(schema,array['items'],outer+'.'+collection+'[]',facts,scope)]
            if action=='items':
                decision=_resolve(schema,array['items']);records='records' if collection=='table_decisions' else 'items'
                record_array=_resolve(schema,decision['properties'][records])
                record=_skeleton(schema,record_array['items'],outer+'.'+collection+'[].'+records+'[]',facts,scope)
                if records=='records' and record.get('row_groups'):
                    record['row_groups'][0]['fragment_ref']=None
                request[outer][collection][0][records]=[record]
        if action=='revise':request['changes']['model_id']=None
        if action=='decomposition':request['changes']['authorization']={'source_item_ids':[scope.get('source_item_id')]}
        if action=='annotations':request['changes']['annotation_updates'][0]['topics']=None
        if action=='map':request['decisions']['outline']=[{'role':'DOCUMENT','children':[{'role':'HEADING','ref':None,'content':[],'children':[{'role':'CLAUSE','ref':None}]}]}]
    missing=[]
    def visit(value,path):
        if value is None:missing.append(path)
        elif isinstance(value,dict):
            for key,child in value.items():visit(child,path+'.'+key if path else key)
        elif isinstance(value,list):
            for index,child in enumerate(value):visit(child,f'{path}[{index}]')
    visit(request,'')
    schema_valid=False
    if not missing:
        try:REQUESTS[action].model_validate(request);schema_valid=True
        except ValueError:pass
    examples=[{'task_action':action,'request':request,'status':'REQUEST_SHAPE_VALID_CHECK_STAGE_BEFORE_CALL' if schema_valid else 'TEMPLATE_REQUIRES_REAL_INPUT',
               'missing_inputs':missing,'semantic_decisions_generated':False}]
    if action in {'annotations','decomposition','revise'}:
        prepared={'task_id':facts.get('task_id'),'expected_revision':facts.get('revision')}
        bound=all(prepared.values())
        if bound:REQUESTS['review'].model_validate(prepared)
        examples.append({'tool':'review_prepare','task_action':'review','request':prepared,
            'request_schema_valid':bound,'stage_precondition':'A matching ITEMS baseline must already be saved.',
            'ready_to_call':bound and 'ITEMS' in facts.get('available_stages',[]) and facts.get('current_owner')!='portable_review',
            'when':'Need self_review_finding/resolutions but no review yet. Read the returned new revision before continuing.',
            'stage_changed_by_describe':False})
    return examples


def _bounded(action,part,data,facts,scope,offset,max_chars,schema_sha):
    def ready_links(value):
        if isinstance(value,list):return [ready_links(v) for v in value]
        if not isinstance(value,dict):return value
        result={k:ready_links(v) for k,v in value.items()}
        if isinstance(value.get('part'),str) and value['part'].startswith('#'):
            request={'action':action,'part':value['part'],'expected_schema_sha256':schema_sha}
            if facts.get('task_id'):request.update(task_id=facts['task_id'],expected_revision=facts.get('revision'))
            if scope.get('scope_id'):request['scope_id']=scope['scope_id']
            result['read']={'tool':'task_describe','request':request}
        return result
    data=ready_links(data)
    serialized=_dump(data)
    if offset>len(serialized):raise ValueError('offset is past this exact schema part')
    arguments={'action':action,'part':part,'max_chars':max_chars}
    if facts:arguments['context']=facts
    if scope:arguments['scope']=scope
    base={'schema_version':'reqpilot-task-description/v1','status':'DESCRIBED','action':action,'part':part,
          'request_schema_sha256':schema_sha,'budget_scope':'ENTIRE_DESCRIPTION_JSON_CHARACTERS','total_characters':len(serialized)}
    whole={**base,'data':data,'offset':0,'returned_characters':len(serialized),'complete':True,'next_read':None}
    if offset==0 and len(_dump(whole))<=max_chars:return whole
    def piece(count):
        end=offset+count;more=end<len(serialized)
        return {**base,'encoding':'JSON_TEXT_PART; concatenate text in offset order then JSON-parse',
            'text':serialized[offset:end],'offset':offset,'returned_characters':count,'complete':not more,
            'next_read':{'reader':'task_schema.describe','arguments':{**arguments,'offset':end}} if more else None}
    lo=0;hi=len(serialized)-offset
    while lo<hi:
        middle=(lo+hi+1)//2
        if len(_dump(piece(middle)))<=max_chars:lo=middle
        else:hi=middle-1
    result=piece(lo)
    if len(_dump(result))>max_chars or lo==0 and offset<len(serialized):raise ValueError('max_chars cannot fit schema continuation metadata')
    return result


def describe(action: str | None = None, *, context: dict | None = None, scope: dict | None = None,
             part: str | None = None, offset: int = 0, max_chars: int = 12000) -> dict:
    from task_service import REQUESTS
    if type(offset) is not int or offset<0:raise ValueError('offset must be a nonnegative integer')
    if type(max_chars) is not int or not 2048<=max_chars<=24000:raise ValueError('max_chars must be between 2048 and 24000')
    facts,actual_scope=_facts(context,scope)
    if action is None:
        if part not in (None,'catalog'):raise ValueError('Choose an explicit action before reading a schema part')
        catalog={'actions':[{'action':name,'purpose':PURPOSES.get(name,model.__name__),
                    'describe':{'action':name}} for name,model in REQUESTS.items()],
                 'usage':'Query one action, then fields/schema/examples/constraints or a returned #/$defs/... part. No task operation is performed.'}
        return _bounded(None,'catalog',catalog,facts,actual_scope,offset,max_chars,_sha({k:v.model_json_schema() for k,v in REQUESTS.items()}))
    if action not in REQUESTS:raise ValueError('Unknown task action; call describe() for the actual action catalog')
    schema=REQUESTS[action].model_json_schema();part=part or 'overview'
    if part=='schema':data=schema
    elif part=='fields':data={'fields':_fields(schema,schema),'extra_fields_allowed':schema.get('additionalProperties',True)}
    elif part=='constraints':data=_rules(action,facts)
    elif part=='examples':data={'examples':_template(action,schema,facts,actual_scope)}
    elif part.startswith('#'):
        node=_pointer(schema,part);resolved=_resolve(schema,node)
        data={'pointer':part,'type':_kind(schema,node),'description':resolved.get('description') if isinstance(resolved,dict) else None,
              'fields':_fields(schema,node),'json_schema':{k:v for k,v in resolved.items() if not k.startswith('_')} if isinstance(resolved,dict) else resolved,
              'referenced_parts':[{'part':ref,'type':_kind(schema,_pointer(schema,ref))} for ref in _ref_links(node)]}
    elif part=='overview':
        references=_ref_links(schema)
        data={'purpose':PURPOSES.get(action,REQUESTS[action].__name__),'required_fields':schema.get('required',[]),
            'fields':_fields(schema,schema),'constraints':_rules(action,facts),'examples':_template(action,schema,facts,actual_scope),
            'read_parts':['fields','schema','examples','constraints',*references],
            'definition_reads':[{'part':ref,'type':_kind(schema,_pointer(schema,ref))} for ref in references]}
    else:raise ValueError('Unsupported describe part; use overview/fields/schema/examples/constraints or a local #/ JSON pointer')
    return _bounded(action,part,data,facts,actual_scope,offset,max_chars,_sha(schema))


__all__=['describe']
