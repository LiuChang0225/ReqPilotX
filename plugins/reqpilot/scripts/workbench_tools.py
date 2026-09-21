"""Local HTTP transport; semantic decisions stay in the shared compilers."""
from argparse import Namespace
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ARTIFACTS=('document-map.json','table-previews.json','source-requirement-baseline.json','validation-receipt.json',
           'image-annotations.json','source-item-decomposition.json','analysis-policy.json')


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');temporary.chmod(0o600);os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Workbench transport does not follow redirects')


def request(url,payload=None):
    parsed=urlsplit(url)
    if parsed.scheme not in {'http','https'} or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password:
        raise ValueError('Workbench transport requires the configured local application URL')
    data=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
    try:
        with build_opener(NoRedirect()).open(Request(url,data=data,headers={'Content-Type':'application/json'}),timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        body=exc.read(8000).decode('utf-8',errors='replace')
        try:body=json.loads(body).get('detail',body)
        except ValueError:pass
        raise ValueError(f'Workbench HTTP {exc.code}: {body}') from exc


def binding_path(sha):return Path.cwd()/'runtime-data/workbench-bindings'/f'{sha}.json'


def _task_workspace(args):
    from reqpilotx.application.agent_workflow.task_workspace import find_task_workspace
    explicit=getattr(args,'workspace',None)
    found=find_task_workspace(explicit=explicit)
    if explicit is not None and found is None:
        raise ValueError('Explicit --workspace has no task-context.json; initialize a task or omit --workspace for a legacy directory')
    return found


def _selected_source(args):
    return any(getattr(args,key,None) for key in ('source','map_dir','baseline_dir','review_dir'))


def requested_confirmation(args):
    """None means inherit a saved policy; False is the default only for new work."""
    required=getattr(args,'require_map_confirmation',None)
    bypass=getattr(args,'no_map_confirmation',None)
    if required is not None and type(required) is not bool:
        raise ValueError('require_map_confirmation must be boolean when supplied')
    if bypass is not None and type(bypass) is not bool:
        raise ValueError('no_map_confirmation must be boolean when supplied')
    if required is not None and bypass:
        raise ValueError('Choose only one structure confirmation policy')
    return required if required is not None else False if bypass else None


def _workspace_path(root,value):
    path=Path(value).expanduser()
    return (root/path if not path.is_absolute() else path).resolve()


def _validate_binding(binding,sha,task=None):
    required=('session_id','base_url','project_id','document_id','source_sha256','require_map_confirmation','revision_id','imported_feedback')
    if (not isinstance(binding,dict) or binding.get('schema_version')!='reqpilot-workbench-binding/v1'
            or any(key not in binding for key in required) or not isinstance(binding['require_map_confirmation'],bool)
            or not isinstance(binding['imported_feedback'],dict)):
        raise ValueError('Workbench binding is invalid; restore the saved binding rather than creating another session')
    if sha is not None and binding['source_sha256']!=sha:
        raise ValueError('Binding file belongs to a different source')
    if task:
        _,context=task
        if binding.get('work_id') not in (None,context['work_id']):
            raise ValueError('Binding file belongs to another task workspace')
        for field in ('project_id','document_id'):
            if context.get(field) is not None and binding[field]!=context[field]:
                raise ValueError(f'Binding {field} conflicts with the task workspace')
        if context['request'].get('require_map_confirmation') and not binding['require_map_confirmation']:
            raise ValueError('Task requires Map confirmation but this binding disables it')


def resolve_binding(args,sha=None,*,require_existing=False,allow_create=False):
    """Resolve one task/source/target binding for both explicit tools and hooks."""
    task=_task_workspace(args);args=_task_args(args,task)
    if sha is None and _selected_source(args):sha,_,_=source_and_artifacts(args)
    if task:
        root,context=task
        if sha is not None and sha!=context['source']['sha256']:
            raise ValueError('Selected original/artifacts belong to a different task source')
        sha=context['source']['sha256']
    explicit=getattr(args,'binding_file',None)
    canonical=_workspace_path(task[0],task[1]['binding_file']) if task else None
    if explicit:
        path=_workspace_path(task[0] if task else Path.cwd(),explicit)
    elif canonical is not None:path=canonical
    elif sha is not None:path=binding_path(sha).resolve()
    else:raise ValueError('Supply a source, task workspace or explicit --binding-file to find the saved connection')
    binding=read(path) if path.is_file() else None
    if binding is not None:
        _validate_binding(binding,sha,task)
        if sha is None:sha=binding['source_sha256']
        # Explicit target arguments also apply to pull and hooks; none may silently redirect.
        _target(args,binding)
    elif require_existing or (explicit and not allow_create):
        raise ValueError(f'Workbench binding does not exist: {path}. Connect this task with workbench first.')
    if task and canonical!=path and canonical.is_file():
        current=read(canonical);_validate_binding(current,sha,task)
        if binding is None or any(binding[key]!=current[key] for key in ('session_id','base_url','project_id','document_id','source_sha256')):
            raise ValueError('Task already has another canonical workbench session; resume it or create a separate task')
    return path,sha,task


def _task_args(args,task):
    """Use declared task facts only; local keys never become application IDs."""
    if not task:return args
    root,context=task;values=vars(args).copy()
    for key in ('project_id','document_id'):
        declared=context.get(key);explicit=values.get(key)
        if declared is not None and explicit is not None and explicit!=declared:
            raise ValueError(f'Explicit {key} conflicts with the task workspace')
        if explicit is None and declared is not None:values[key]=declared
    query=parse_qs(urlsplit(values.get('url') or '').query)
    for field,query_key in (('project_id','projectId'),('document_id','documentId')):
        if context.get(field) is not None and query.get(query_key,[context[field]])[0]!=context[field]:
            raise ValueError(f'Workbench URL {field} conflicts with the task workspace')
    if not values.get('goal'):values['goal']=context['request'].get('goal','')
    requested=requested_confirmation(args)
    if requested is not None and requested!=context['request']['require_map_confirmation']:
        raise ValueError('Explicit Map confirmation policy conflicts with the saved task; it cannot be silently changed')
    if not _selected_source(args):values['source']=_workspace_path(root,context['source']['path'])
    return Namespace(**values)


def _validate_state(state,binding):
    for field in ('source_sha256','session_id','project_id','document_id'):
        if state.get(field)!=binding[field]:raise ValueError(f'Workbench receipt {field} differs from the saved binding')


def workbench_result_url(binding, revision_id):
    """Name an observed external result explicitly; never use a current alias.

    A source-only session has no resultRevision. The URL is reconstructed from
    the validated binding so old generic paths cannot silently select native work.
    """
    base=urlsplit(binding['base_url'])
    if (base.scheme not in {'http','https'} or base.hostname not in {'127.0.0.1','localhost','::1'}
            or base.username or base.password or base.query or base.fragment or base.path not in {'','/'}):
        raise ValueError('Workbench result link requires the configured local application origin')
    if not re.fullmatch(r'plugin-[a-f0-9]{32}',binding.get('session_id','')):
        raise ValueError('Workbench result link requires a concrete plugin session')
    if revision_id is not None and (not isinstance(revision_id,str) or not re.fullmatch(r'[a-f0-9]{64}',revision_id)):
        raise ValueError('Workbench result link requires an exact saved revision, never current or latest')
    for key in ('project_id','document_id'):
        if not isinstance(binding.get(key),str) or not binding[key]:
            raise ValueError('Workbench result link requires a concrete '+key)
    params={'task':'extraction','mode':'organize','checkpoint':'structure',
        'projectId':binding['project_id'],'documentId':binding['document_id'],'pluginSessionId':binding['session_id']}
    if revision_id is not None:params['resultRevision']=revision_id
    return urlunsplit((base.scheme,base.netloc,'/workspace',urlencode(params),''))


def _remember_result_url(binding,state):
    url=workbench_result_url(binding,state['revision_id'])
    binding.update(revision_id=state['revision_id'],url=url,workbench_url=url,
        workbench_path=urlsplit(url).path+'?'+urlsplit(url).query)
    return url


def _remember_task_binding(task,path,binding):
    if not task:return
    from reqpilotx.application.agent_workflow.task_workspace import bind_task_workspace
    _validate_binding(binding,task[1]['source']['sha256'],task)
    binding['work_id']=task[1]['work_id']
    save(path,binding)
    bind_task_workspace(task[0],path,binding['source_sha256'],binding['project_id'],binding['document_id'])


def source_and_artifacts(args):
    review=getattr(args,'review_dir',None)
    folder=getattr(args,'baseline_dir',None) or getattr(args,'map_dir',None)
    local_review_sha=None;source=getattr(args,'source',None)
    if review:
        from revision_tools import load_head, current_bundle
        review=review.resolve(strict=True);head=load_head(review,getattr(args,'expected_review_sha256',None))
        folder,_=current_bundle(review,head);local_review_sha=head['review_sha256']
        source=review/head['source']['path']
    artifacts={}
    if folder:
        folder=folder.resolve(strict=True)
        if not getattr(args,'map_dir',None):
            unsupported=[n for n in ('source-item-assessments.json','manual-decisions.json') if (folder/n).is_file()]
            if unsupported:
                raise ValueError('This draft includes analysis/engineer-opinion evidence not yet supported by this transport; do not omit it: '+', '.join(unsupported))
        names=ARTIFACTS[:2] if getattr(args,'map_dir',None) else ARTIFACTS
        artifacts={n:read(folder/n) for n in names if (folder/n).is_file()}
        if 'document-map.json' not in artifacts:raise ValueError('The selected directory has no compiled Document Map')
        sha=artifacts['document-map.json']['source_sha256']
        if source and hashlib.sha256(source.read_bytes()).hexdigest()!=sha:raise ValueError('Original differs from the artifact source')
    elif source:
        sha=hashlib.sha256(source.resolve(strict=True).read_bytes()).hexdigest()
    else:raise ValueError('Supply --source, --map-dir, --baseline-dir or --review-dir to bind a document')
    return sha,artifacts,local_review_sha


def _target(args,previous):
    raw=getattr(args,'url',None) or (previous or {}).get('base_url') or os.environ.get('REQPILOT_WORKBENCH_URL','http://127.0.0.1:8000/workspace')
    parsed=urlsplit(raw);query=parse_qs(parsed.query)
    base=urlunsplit((parsed.scheme,parsed.netloc,'','',''))
    project=getattr(args,'project_id',None) or query.get('projectId',[None])[0] or (previous or {}).get('project_id')
    document=getattr(args,'document_id',None) or query.get('documentId',[None])[0] or (previous or {}).get('document_id')
    if not project or not document:
        raise ValueError('Select the project/document in ReqPilotX and supply its actual --url (with projectId/documentId), or --project-id and --document-id. No target is guessed from this folder.')
    if previous and (base,project,document)!=(previous['base_url'],previous['project_id'],previous['document_id']):
        raise ValueError('This source already has a different workbench binding; use a separate workspace or explicit --binding-file for the other target')
    return base,project,document


def sync_workbench(args):
    path,sha,task=resolve_binding(args,allow_create=True)
    args=_task_args(args,task)
    selected_sha,artifacts,review_sha=source_and_artifacts(args)
    if selected_sha!=sha:raise ValueError('Selected source changed while resolving the task binding')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    from revision_tools import locked
    with locked(path.parent):
        if getattr(args,'review_dir',None):
            # Validate the captured owner again after acquiring the write lock,
            # even if this publish must first expose only its unconfirmed Map.
            from revision_tools import load_head
            load_head(args.review_dir.resolve(strict=True),review_sha)
        previous=read(path) if path.is_file() else None
        if previous:_validate_binding(previous,sha,task)
        base,project,document=_target(args,previous)
        requested=requested_confirmation(args)
        binding=previous or {'schema_version':'reqpilot-workbench-binding/v1','session_id':'plugin-'+uuid.uuid4().hex,
            'base_url':base,'project_id':project,'document_id':document,'source_sha256':sha,'revision_id':None,
            'require_map_confirmation':task[1]['request']['require_map_confirmation'] if task else requested if requested is not None else False,'imported_feedback':{}}
        if task:binding['work_id']=task[1]['work_id']
        if previous and requested is not None and requested!=previous['require_map_confirmation']:
            raise ValueError('The existing connection has a different Map confirmation policy; it cannot be silently changed')
        api=base+'/api/projects/'+quote(project,safe='')+'/sor-documents/'+quote(document,safe='')+'/agent-structure/delivery/plugin-workbench'
        state=request(api+'/'+binding['session_id']) if previous else None
        if state:_validate_state(state,binding)
        # Refresh confirmations/feedback, but never silently adopt a newer source revision for a writer.
        if state and state['revision_id']!=binding['revision_id']:
            raise ValueError('The workbench has another revision. Run workbench-pull, inspect it and decide how to continue.')
        if state is None:
            state=request(api,{'session_id':binding['session_id'],'expected_source_sha256':sha,
                'require_map_confirmation':binding['require_map_confirmation'],'goal':getattr(args,'goal','') or ''})
            _validate_state(state,binding)
            save(path,binding)
        _remember_task_binding(task,path,binding)
        requested_baseline='source-requirement-baseline.json' in artifacts
        waiting=False
        if artifacts:
            map_sha=artifacts['document-map.json']['artifact_sha256']
            if requested_baseline and binding['require_map_confirmation'] and state.get('confirmed_map_sha256')!=map_sha:
                artifacts={name:artifacts[name] for name in ARTIFACTS[:2]};review_sha=None;waiting=True
            resolutions={}
            if requested_baseline and not waiting and getattr(args,'review_dir',None):
                from revision_tools import load_head
                local=load_head(args.review_dir.resolve(strict=True),review_sha)
                for remote_id,local_id in binding.get('imported_feedback',{}).items():
                    item=next((f for f in local['feedback'] if f['feedback_id']==local_id and f['status']=='ADDRESSED'),None)
                    if item:resolutions[remote_id]=item['resolution_reason']
            state=request(api,{'session_id':binding['session_id'],'expected_source_sha256':sha,
                'expected_revision_id':binding['revision_id'],'require_map_confirmation':binding['require_map_confirmation'],
                'artifacts':artifacts,'local_review_sha256':review_sha,'feedback_resolutions':resolutions})
            _validate_state(state,binding)
        url=_remember_result_url(binding,state)
        if artifacts:
            binding['synchronized_local_review_sha256']=review_sha
            binding['synchronized_result_revision']=state['revision_id']
        save(path,binding);save(path.with_name(path.stem+'-inbox.json'),state)
    opened=False
    if getattr(args,'open',False):
        import webbrowser
        opened=webbrowser.open(url)
    return {'status':'WAITING_FOR_MAP_CONFIRMATION' if state['map_confirmation_required'] else 'WORKBENCH_SYNCHRONIZED',
        'url':url,'binding_file':str(path),'session_id':binding['session_id'],'revision_id':state['revision_id'],
        'link_target':'SAVED_REMOTE_RESULT' if state['revision_id'] else 'SOURCE_WORK',
        'revision_number':state['revision_number'],'stages':state['stages'],'opened':opened,
        'pending_items_sync':waiting,'feedback_count':len(state['feedback']),
        'next':'Inspect this source-bound draft in the workbench. Use workbench-pull to retrieve confirmation/feedback; after required Map confirmation synchronize the saved item/review version.',
        'model_dispatch':'NOT_PERFORMED','human_acceptance':'NOT_CLAIMED'}


def pull_workbench(args):
    path,sha,task=resolve_binding(args,require_existing=True)
    args=_task_args(args,task)
    from revision_tools import locked
    with locked(path.parent):
        binding=read(path);_validate_binding(binding,sha,task)
        result=_pull_locked(args,path)
        _remember_task_binding(task,path,read(path))
        return result


def _pull_locked(args,path):
    binding=read(path)
    api=binding['base_url']+'/api/projects/'+quote(binding['project_id'],safe='')+'/sor-documents/'+quote(binding['document_id'],safe='')+'/agent-structure/delivery/plugin-workbench/'+binding['session_id']
    state=request(api)
    _validate_state(state,binding)
    inbox=path.with_name(path.stem+'-inbox.json');save(inbox,state)
    imported=[];pending=[]
    review=getattr(args,'review_dir',None)
    if review:
        from revision_tools import load_head,feedback_review
        from artifact_tools import save as immutable_save
        review=review.resolve(strict=True);head=load_head(review)
        if head['source']['sha256']!=binding['source_sha256']:raise ValueError('Review belongs to another original')
        for event in state['feedback']:
            marker='ReqPilotX workbench feedback '+event['feedback_id']
            existing=next((f for f in head['feedback'] if f.get('rationale','').startswith(marker+';')),None)
            if existing:
                binding['imported_feedback'][event['feedback_id']]=existing['feedback_id'];continue
            if event['baseline_sha256']!=head['current']['baseline_sha256']:
                pending.append(event['feedback_id']);continue
            content={'expected_review_sha256':head['review_sha256'],'feedback':[{'origin':'USER',
                'source_item_ids':event['source_item_ids'],'text':event['text'],
                'rationale':marker+'; original workbench revision '+event['expected_revision_id']}]}
            inputs=review/'workbench-inputs';inputs.mkdir(exist_ok=True)
            file=inputs/(event['feedback_id']+'-'+head['review_sha256'][:12]+'.json')
            if not file.exists():immutable_save(inputs,file.name,content)
            result=feedback_review(Namespace(review_dir=review,input=file));head=load_head(review)
            binding['imported_feedback'][event['feedback_id']]=result['added_feedback_ids'][0];imported.append(event['feedback_id'])
    url=_remember_result_url(binding,state);save(path,binding)
    return {'status':'WORKBENCH_FEEDBACK_READ','inbox_file':str(inbox),'revision_id':state['revision_id'],
        'url':url,'session_id':binding['session_id'],
        'link_target':'SAVED_REMOTE_RESULT' if state['revision_id'] else 'SOURCE_WORK',
        'confirmed_map_sha256':state.get('confirmed_map_sha256'),'map_confirmation_required':state['map_confirmation_required'],
        'feedback':state['feedback'],'imported_feedback_ids':imported,'needs_source_version_review':pending,
        'local_review_sha256':head['review_sha256'] if review else None,
        'next':'Read source-bound feedback and update only the affected draft. Map-only or changed-baseline feedback remains in the inbox for explicit source review; it is not silently applied.'}


def sync_after_operation(args,result):
    # The workspace wrapper publishes only after its local commit/CAS succeeds.
    # This switch must not bypass before_operation's source-bound Map checkpoint.
    if os.environ.get('REQPILOT_WORKSPACE_DEFER_SYNC')=='1' or os.environ.get('REQPILOT_NATIVE_AGENT_TOOLS')=='1' or args.command not in {'compile-map','compile-items','compile-images','review-open','review-close'}:
        return result
    folder=getattr(args,'output_dir',None);review=getattr(args,'review_dir',None) if args.command.startswith('review-') else None
    if not folder and not review:return result
    selection=Namespace(**{**vars(args),'review_dir':review,'map_dir':folder if args.command=='compile-map' else None,
        'baseline_dir':folder if args.command in {'compile-items','compile-images'} else None,'source':None})
    try:
        sha,_,_=source_and_artifacts(selection)
        path,_,task=resolve_binding(selection,sha)
        if not path.is_file():
            if task:result['workbench_sync']={'status':'NOT_BOUND','local_artifact_preserved':True,
                'binding_file':str(path),'next':'Connect this task to its explicit application project/document with workbench; the local artifact is saved.'}
            return result
        selection.binding_file=path
        result['workbench_sync']=sync_workbench(selection)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        result['workbench_sync']={'status':'NOT_SYNCHRONIZED','local_artifact_preserved':True,'error':str(exc),
            'next':'The local artifact remains saved. Repair the workbench binding and synchronize this version explicitly.'}
    return result


def before_operation(args):
    """Honor a bound Map checkpoint before creating downstream artifacts."""
    if os.environ.get('REQPILOT_NATIVE_AGENT_TOOLS')=='1' or args.command not in {'compile-items','compile-images','review-open'}:
        return None
    folder=getattr(args,'map_dir',None) or getattr(args,'baseline_dir',None)
    if not folder or not (folder/'document-map.json').is_file():return None
    mapping=read(folder/'document-map.json');path,_,task=resolve_binding(args,mapping['source_sha256'])
    if not path.is_file():
        if task and task[1]['request']['require_map_confirmation']:
            return {'status':'WORKBENCH_NOT_BOUND','requested_operation':args.command,'downstream_artifact_created':False,
                'binding_file':str(path),'next':'This task requires Map confirmation. Connect its saved Map to the explicit application project/document with workbench before continuing.'}
        return None
    binding=read(path);_validate_binding(binding,mapping['source_sha256'],task)
    if not binding['require_map_confirmation']:return None
    api=binding['base_url']+'/api/projects/'+quote(binding['project_id'],safe='')+'/sor-documents/'+quote(binding['document_id'],safe='')+'/agent-structure/delivery/plugin-workbench/'+binding['session_id']
    try:
        state=request(api)
    except OSError:
        cached=path.with_name(path.stem+'-inbox.json')
        receipt=read(cached) if cached.is_file() else {}
        same_binding=all(receipt.get(field)==binding[field] for field in ('source_sha256','session_id','project_id','document_id'))
        if same_binding and receipt.get('confirmed_map_sha256')==mapping['artifact_sha256']:
            return None
        return {'status':'WORKBENCH_UNAVAILABLE','requested_operation':args.command,'downstream_artifact_created':False,
            'next':'The saved source remains available, but this Map has no cached confirmation. Restore the workbench connection before proceeding past this checkpoint.'}
    _validate_state(state,binding)
    save(path.with_name(path.stem+'-inbox.json'),state)
    if state.get('confirmed_map_sha256')==mapping['artifact_sha256']:return None
    if os.environ.get('REQPILOT_WORKSPACE_DEFER_SYNC')=='1':
        # A workspace candidate is not publishable before its local commit/CAS.
        # The wrapper may read real confirmations here, but cannot advance Map.
        return {'status':'WAITING_FOR_MAP_CONFIRMATION','requested_operation':args.command,
            'downstream_artifact_created':False,'session_id':binding['session_id'],'binding_file':str(path),
            'expected_map_sha256':mapping['artifact_sha256'],
            'next':'This exact Map has no recorded confirmation. Synchronize the selected committed Map with workbench and confirm it before retrying; this checkpoint did not publish a candidate.'}
    # Save/show this exact Map if an earlier automatic synchronization failed.
    result=sync_workbench(Namespace(**{**vars(args),'map_dir':folder,'baseline_dir':None,'review_dir':None,'source':None,'binding_file':path}))
    return {**result,'status':'WAITING_FOR_MAP_CONFIRMATION','requested_operation':args.command,
        'downstream_artifact_created':False,'next':'This exact structure must be confirmed in the workbench before this operation. Continue source review or collect feedback; no item artifact was created.'}
