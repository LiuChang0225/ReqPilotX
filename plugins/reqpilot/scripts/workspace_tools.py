"""Task-scoped CLI adapter; existing compilers own semantics and saved bundles."""
from __future__ import annotations

from argparse import Namespace
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

OPERATIONS = ('capture', 'reading-workspace', 'read-pdf-visual', 'revise-visual-text', 'compile-map', 'compile-items', 'compile-images',
              'compile-annotations', 'compile-decomposition', 'deliver', 'review-open')
PATH_ARGUMENTS = ('input', 'source_ir', 'source', 'map_dir', 'baseline_dir', 'annotations',
                  'decomposition', 'previous_decomposition', 'knowledge_catalog', 'analysis_packet',
                  'analysis_policy', 'assessments', 'manual_decisions')
VALUE_ARGUMENTS = ('expected_sha256',)
BOOL_ARGUMENTS = ('capture_only', 'progress')
BLOCKED_STATUSES = {'WAITING_FOR_MAP_CONFIRMATION', 'WORKBENCH_NOT_BOUND', 'WORKBENCH_UNAVAILABLE'}


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _bundle_receipt(plugin_root):
    path = plugin_root / 'build-receipt.json'
    receipt = _json(path)
    digest = hashlib.sha256(json.dumps({k:v for k,v in receipt.items() if k != 'bundle_sha256'},
        sort_keys=True,ensure_ascii=False,separators=(',', ':')).encode()).hexdigest()
    if digest != receipt.get('bundle_sha256'):
        raise ValueError('Pinned capability build receipt changed')
    for entries, root in ((receipt['plugin_files'],plugin_root), (receipt['runtime_files'],plugin_root/'runtime/prototype')):
        for item in entries:
            path = (root/item['path']).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file() or _digest(path) != item['sha256']:
                raise ValueError('Pinned capability file is missing or changed: ' + item['path'])
    return receipt


def runtime_reference(entrypoint, project_root=None):
    entrypoint = Path(entrypoint).resolve(strict=True)
    plugin_root = entrypoint.parent.parent
    ref = {'entrypoint':str(entrypoint), 'entrypoint_sha256':_digest(entrypoint),
           'python_executable':sys.executable, 'bundle_sha256':None}
    if (plugin_root/'build-receipt.json').is_file():
        ref.update(kind='BUNDLED',bundle_sha256=_bundle_receipt(plugin_root)['bundle_sha256'])
    elif project_root is not None:
        project = Path(project_root).expanduser().resolve(strict=True)
        if not (project/'packages/engine/src/reqpilotx/application/agent_workflow/task_workspace.py').is_file():
            raise ValueError('Explicit development checkout has no task workspace runtime')
        ref.update(kind='DEVELOPMENT_CHECKOUT',project_root=str(project),freeze_state='EXPLICIT_CHECKOUT_NOT_FROZEN')
    else:
        raise ValueError('Task initialization needs the built capability or an explicit --project-root development checkout')
    return ref


def _runtime(context):
    ref = context.get('runtime_ref')
    if not isinstance(ref,dict) or ref.get('kind') not in {'BUNDLED','DEVELOPMENT_CHECKOUT'}:
        raise ValueError('Task has no pinned runtime entry. Initialize it with workspace init; no current installation is guessed.')
    entrypoint = Path(ref.get('entrypoint','')).expanduser()
    python = ref.get('python_executable')
    if not entrypoint.is_file() or _digest(entrypoint) != ref.get('entrypoint_sha256'):
        raise ValueError('Pinned task entrypoint is missing or changed; restore that capability version. The newest installation is not substituted.')
    if not python or not Path(python).is_file():
        raise ValueError('Pinned Python environment is unavailable; restore the recorded environment before resuming')
    command = [python,str(entrypoint)]  # Keep the virtualenv interpreter symlink intact.
    if ref['kind'] == 'BUNDLED':
        if _bundle_receipt(entrypoint.parent.parent)['bundle_sha256'] != ref.get('bundle_sha256'):
            raise ValueError('Pinned task capability bundle SHA differs')
    else:
        project = Path(ref.get('project_root','')).expanduser()
        if not (project/'packages/engine/src/reqpilotx/application/agent_workflow/task_workspace.py').is_file():
            raise ValueError('Task development checkout is no longer available')
        command += ['--project-root',str(project)]
    return command


def _task(args):
    from reqpilotx.application.agent_workflow.task_workspace import find_task_workspace
    found = find_task_workspace(explicit=getattr(args,'workspace',None))
    if found is None:
        raise ValueError('No task-context.json found. Use workspace init or provide the actual --workspace task root.')
    return found


def initialize(args, entrypoint):
    from reqpilotx.application.agent_workflow.task_workspace import initialize_workspace, workspace_status
    from workbench_tools import requested_confirmation
    project,document = getattr(args,'project_id',None),getattr(args,'document_id',None)
    url = getattr(args,'workbench_url',None)
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in {'http','https'} or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password:
            raise ValueError('--workbench-url must identify the configured local ReqPilotX application')
        query = parse_qs(parsed.query)
        for given,key in ((project,'projectId'),(document,'documentId')):
            if given and query.get(key) and given != query[key][0]:
                raise ValueError('Explicit application identity conflicts with --workbench-url')
        project = project or query.get('projectId',[None])[0]
        document = document or query.get('documentId',[None])[0]
        if not project or not document:
            raise ValueError('Workbench connection needs actual projectId/documentId; local directory keys are not application identities')
    confirmation=requested_confirmation(args)
    state = initialize_workspace(args.working_root,args.source,args.goal,
        project_key=args.project_key,document_key=args.document_key,project_id=project,document_id=document,
        require_map_confirmation=confirmation if confirmation is not None else False,runtime_ref=runtime_reference(entrypoint,args.project_root),
        creation_key=getattr(args,'creation_key',None))
    result = {'status':'WORKSPACE_INITIALIZED',**state}
    if url and state.get('creation_replayed'):
        result['workbench_sync']={'status':'NOT_REEXECUTED','url':state.get('workbench_url'),'local_artifact_preserved':True,
            'next':'The existing task was replayed. Its optional connection was not issued again; use the saved URL or explicitly reconnect this same workspace if the original connection has no receipt.'}
        return result
    if url:
        from workbench_tools import sync_workbench
        try:
            sync = sync_workbench(Namespace(workspace=Path(state['task_root']),url=url,binding_file=args.binding_file,open=args.open))
            result.update(workbench_sync=sync,**workspace_status(state['task_root']))
        except (ValueError,OSError,KeyError,TypeError) as exc:
            result['workbench_sync'] = {'status':'NOT_SYNCHRONIZED','local_artifact_preserved':True,'error':str(exc),
                'next':'The task root and original remain saved. Connect this same workspace when the application is available.'}
    return result


def status(args):
    from reqpilotx.application.agent_workflow.task_workspace import workspace_status
    return {'status':'WORKSPACE_STATUS',**workspace_status(_task(args)[0])}


def recover(args):
    from reqpilotx.application.agent_workflow.task_workspace import recover_workspace
    return {'status':'WORKSPACE_RECOVERED',**recover_workspace(_task(args)[0])}


def _path(value):
    return Path(value).expanduser().resolve(strict=True) if value is not None else None


def _inputs(args, root, state):
    """Resolve caller paths before subprocess cwd changes; pick only saved selections."""
    operation=args.operation;selected=state['selected']
    values={key:_path(getattr(args,key,None)) for key in PATH_ARGUMENTS}
    source=root/state['context']['source']['path']
    if values['source'] is not None and _digest(values['source']) != state['context']['source']['sha256']:
        raise ValueError('Explicit source differs from this task original')
    values['source']=values['source'] or source
    def saved(stage,field='path',required=False):
        value=selected.get(stage,{}).get(field)
        if value:return Path(value)
        if required:raise ValueError('No saved '+stage+' selection. Prepare it with workspace run or explicitly supply the required input.')
        return None
    if operation in {'compile-items','compile-images'}:
        values['map_dir']=values['map_dir'] or saved('MAP','output_dir',required=True)
        prior=saved('ITEMS','output_dir')
        if prior and _json(prior/'source-requirement-baseline.json')['document_map_artifact_sha256'] != _json(values['map_dir']/'document-map.json')['artifact_sha256']:
            prior=None
        values['baseline_dir']=values['baseline_dir'] or prior
    elif operation=='compile-map':
        values['map_dir']=values['map_dir'] or saved('MAP','output_dir')
    elif operation in {'compile-annotations','compile-decomposition','deliver','review-open'}:
        values['baseline_dir']=values['baseline_dir'] or saved('ITEMS','output_dir',required=True)
    if operation in {'reading-workspace','read-pdf-visual','revise-visual-text','compile-map'}:
        reading=values['source_ir'] or saved('READING')
        map_dir=values['map_dir'] or saved('MAP','output_dir')
        if reading is None and map_dir is not None and (map_dir/'map-compilation-receipt.json').is_file():
            from reqpilotx.application.agent_workflow.artifact_kind_catalog import inside
            original=_json(map_dir/'map-compilation-receipt.json').get('source_inputs',{})
            if original.get('source_ir_path'):
                reading=inside(map_dir,original['source_ir_path'])
                if not reading.is_file() or _digest(reading)!=original.get('source_ir_sha256'):
                    raise ValueError('Saved Map replay capture is missing or changed; supply the exact --source-ir')
        if reading is None:
            raise ValueError('No saved reading capture or complete Map replay capture; run capture or supply --source-ir')
        values['source_ir']=reading
    target_sha=_json(values['baseline_dir']/'source-requirement-baseline.json')['artifact_sha256'] if values['baseline_dir'] else None
    def overlay(stage):
        return saved(stage) if selected.get(stage,{}).get('baseline_sha256')==target_sha else None
    if operation in {'compile-annotations','deliver','review-open'}:
        values['annotations']=values['annotations'] or overlay('ANNOTATIONS')
    if operation=='compile-decomposition':
        owned=values['baseline_dir']/'source-item-decomposition.json'
        values['previous_decomposition']=values['previous_decomposition'] or overlay('DECOMPOSITION') or (owned if owned.is_file() else None)
    if operation in {'deliver','review-open'}:
        values['decomposition']=values['decomposition'] or overlay('DECOMPOSITION')
    if operation.startswith('compile-') and values['input'] is None:
        raise ValueError(operation+' needs semantic decisions in --input; the workspace supplies paths and source bindings, not judgments')
    # Explicit selected overlay bundles precede their baseline's older optional files.
    dependencies={key+'_bundle':values[key].parent for key in ('annotations','decomposition','previous_decomposition') if values[key] is not None}
    values={key:value for key,value in values.items() if value is not None}
    for key in VALUE_ARGUMENTS:
        if getattr(args,key,None) is not None:values[key]=getattr(args,key)
    for key in BOOL_ARGUMENTS:
        if getattr(args,key,False):values[key]=True
    if operation=='capture':
        if values.get('expected_sha256') not in (None,state['context']['source']['sha256']):
            raise ValueError('Explicit expected source SHA differs from this task original')
        values['expected_sha256']=state['context']['source']['sha256']
    if operation in {'reading-workspace','read-pdf-visual','revise-visual-text','compile-map'}:
        dependencies['reading_bundle']=values['source_ir'].parent
    return {**dependencies,**values}


def _flags(values, operation):
    if operation=='capture':allowed={'source','expected_sha256',*BOOL_ARGUMENTS}
    else:allowed=set(PATH_ARGUMENTS)
    flags=[]
    for key,value in values.items():
        if key not in allowed or value is None or value is False:continue
        flags.append('--'+key.replace('_','-'))
        if value is not True:flags.append(str(value))
    return flags


def _result(stdout):
    # Tools may print library notices before their final JSON result.
    decoder=json.JSONDecoder();result=None
    for offset,char in enumerate(stdout):
        if char!='{' or (offset and stdout[offset-1] not in '\r\n'):continue
        try:value,end=decoder.raw_decode(stdout[offset:])
        except ValueError:continue
        if isinstance(value,dict) and not stdout[offset+end:].strip():result=value
    return result


def _run_request(args, root):
    supplied=getattr(args,'request_payload',None)
    if supplied is not None:
        if not isinstance(supplied,dict):raise ValueError('request_payload must be a JSON object')
        return supplied
    request={key:_path(getattr(args,key,None)) for key in PATH_ARGUMENTS if getattr(args,key,None) is not None}
    request.update({key:getattr(args,key) for key in (*VALUE_ARGUMENTS,*BOOL_ARGUMENTS) if getattr(args,key,None) not in (None,False)})
    binding=getattr(args,'binding_file',None)
    if binding is not None:
        path=Path(binding).expanduser();request['binding_file']=str((root/path if not path.is_absolute() else path).resolve())
    request['expected_revision']=getattr(args,'expected_revision',None)
    return request


def _replay_operation(root, operation):
    """Reconcile saved facts only; an interrupted executable is never guessed idle."""
    from reqpilotx.application.agent_workflow import task_workspace as core
    current=core.workspace_status(root,refresh=False)
    state=operation['state']
    response={'status':'WORKSPACE_OPERATION_REPLAYED',**current,'operation_id':operation['operation_id'],
        'state':state,'output_dir':operation['output_dir'],'replayed':True,'execution_state':'UNKNOWN' if state=='STARTED' else 'NOT_REEXECUTED'}
    for key in ('receipt_path','receipt_sha256','error'):
        if key in operation:response[key]=operation[key]
    if operation.get('result') is not None:response['compiler_result']=operation['result']
    receipts=[]
    for entry in current['index']['host_receipts']:
        if entry['operation_id']==operation['operation_id']:
            value=_json(root/entry['path']);receipts.append({'path':str(root/entry['path']),'receipt':value['receipt']})
    if receipts:
        response['publication_receipts']=receipts
        response['workbench_sync']=receipts[-1]['receipt']
    response['publication']={'status':'NOT_REEXECUTED','saved_receipt_count':len(receipts)}
    review=root/'review';head=_json(review/'workspace-manifest.json') if (review/'workspace-manifest.json').is_file() else None
    seen=set()
    while head:
        sha=head['review_sha256']
        if sha in seen or core._seal(head,'review_sha256')!=head:raise ValueError('Review history changed while replaying saved delivery facts')
        seen.add(sha);attachment=head.get('delivery_attachment') or {}
        if attachment.get('source_operation_id')==operation['operation_id']:
            response['delivery_attachment']={'status':'CURRENT_DELIVERY_ATTACHED','review_sha256':sha,
                'delivery_path':head['delivery']['path'],'human_acceptance':'NOT_CLAIMED',
                'historical':sha!=current['current_working'].get('review_sha256'),'replayed':True,'execution_state':'NOT_REEXECUTED'}
            break
        predecessor=head.get('predecessor_review_sha256')
        head=_json(review/'reviews'/(predecessor+'.json')) if predecessor else None
    response.setdefault('delivery_attachment',{'status':'NOT_REEXECUTED','saved_receipt_available':False})
    if state=='STARTED':
        response['next']='This command already reserved an operation, but execution completion is unknown. Inspect/recover its saved state; it was not started again.'
    elif state=='SAVED_PENDING_RECOVERY':
        response['next']='A complete receipt exists. Recover this operation before publication; no compiler was run again.'
    else:response['next']='The original operation and saved receipts were replayed. Use a new command_id only for a genuinely new attempt or changed request.'
    return response


@contextmanager
def _publication_lock(root):
    """Serialize this host's automatic publication, separately from local commits."""
    with (root/'.task-publication.lock').open('a+b') as stream:
        if os.name=='nt':
            import msvcrt
            stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
            msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
        try:yield
        finally:
            if os.name=='nt':
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def _selected_publication(root,state,operation_id,args,result):
    """A committed historical output is not necessarily the selected producer."""
    entry=next((item for item in state['index']['committed'] if item['operation_id']==operation_id),None)
    if not entry or entry['state']!='COMMITTED':return False
    receipt=_json(root/entry['receipt_path']);stages=receipt['validation']['stages'];selected=state['selected']
    def same(stage):
        current=selected.get(stage,{})
        expected=stages.get(stage,{})
        expected_path=Path(expected.get('path',''))
        expected_path=(root/expected_path).resolve() if not expected_path.is_absolute() else expected_path.resolve()
        return (current.get('operation_id')==operation_id and current.get('artifact_sha256')==expected.get('artifact_sha256')
                and bool(current.get('path')) and Path(current['path']).resolve()==expected_path)
    if args.command=='review-open':
        owner=state['current_working']
        return (owner.get('owner')=='portable_review' and owner.get('review_sha256')==result.get('review_sha256')
            and bool(owner.get('review_root')) and Path(owner['review_root']).resolve()==Path(args.review_dir).resolve()
            and owner.get('baseline_sha256')==stages.get('ITEMS',{}).get('artifact_sha256')
            and owner.get('document_map_sha256')==stages.get('MAP',{}).get('artifact_sha256'))
    if state['current_working'].get('owner')!='task_workspace':return False
    if args.command=='compile-map':return same('MAP')
    if args.command in {'compile-items','compile-images'}:
        current_map=selected.get('MAP',{})
        map_dir=getattr(args,'map_dir',None)
        return (same('ITEMS') and current_map.get('artifact_sha256')==stages.get('ITEMS',{}).get('map_sha256')
            and bool(map_dir) and bool(current_map.get('compiler_output_dir'))
            and Path(map_dir).resolve()==Path(current_map['compiler_output_dir']).resolve())
    return False


def publish_committed(root,operation_id,args,result):
    """Only automatic workspace publication uses this per-task host-local lock."""
    from reqpilotx.application.agent_workflow import task_workspace as core
    from workbench_tools import sync_after_operation
    if args.command not in {'compile-map','compile-items','compile-images','review-open'}:
        return {'publication':{'status':'LOCAL_ONLY','reason':'This operation saves evidence, an overlay or a delivery; review-open gathers current overlays into the versioned workbench bundle.'}}
    root=Path(root)
    with _publication_lock(root):
        # Local compilers/commits can continue while a prior network publish runs.
        # A later publisher waits here and rechecks the selected owner afterward.
        state=core.workspace_status(root)
        if not _selected_publication(root,state,operation_id,args,result):
            return {'workbench_sync':{'status':'SKIPPED_SUPERSEDED','local_artifact_preserved':True,
                'operation_id':operation_id,'next':'This saved output is no longer the selected producer/version. Automatic publication was skipped; inspect the current task selection.'}}
        if args.command=='review-open':args.expected_review_sha256=result['review_sha256']
        synchronized=sync_after_operation(args,dict(result))
        if synchronized.get('workbench_sync') is None:
            return {'publication':{'status':'NOT_PUBLISHED','reason':'The transport did not perform automatic synchronization.'}}
        response={'workbench_sync':synchronized['workbench_sync']}
        try:
            response['host_receipt']=core.record_host_receipt(root,operation_id,synchronized['workbench_sync'])
            response.update(core.workspace_status(root))
        except (ValueError,OSError,KeyError,TypeError) as exc:
            response['host_receipt']={'status':'NOT_RECORDED','error':str(exc),'local_artifact_preserved':True,
                'next':'The local commit and actual sync result above remain valid. Recover or record this host receipt without recompiling or replacing the saved result.'}
        return response


def run(args, entrypoint):
    from reqpilotx.application.agent_workflow import task_workspace as core
    root,context=_task(args)
    request=_run_request(args,root);command_id=getattr(args,'command_id',None)
    if command_id is not None:
        prior=core.lookup_operation_command(root,command_id,args.operation,request)
        if prior is not None:return _replay_operation(root,prior)
    command=_runtime(context)
    binding=getattr(args,'binding_file',None)
    if binding is not None:
        binding=Path(binding).expanduser()
        binding=(root/binding if not binding.is_absolute() else binding).resolve()
    # Invoking a newer installation still resumes through this task's fixed entry.
    if Path(entrypoint).resolve()!=Path(command[1]).resolve():
        if getattr(args,'request_payload',None) is not None:
            raise ValueError('Typed request_payload requires the pinned task entrypoint; it cannot be dropped during CLI forwarding')
        explicit={key:_path(getattr(args,key,None)) for key in PATH_ARGUMENTS}
        explicit.update({key:getattr(args,key,None) for key in (*VALUE_ARGUMENTS,*BOOL_ARGUMENTS)})
        resumed=[*command,'workspace','run',args.operation,'--workspace',str(root),*_flags(explicit,args.operation)]
        if binding is not None:resumed+=['--binding-file',str(binding)]
        if getattr(args,'full',False):resumed.append('--full')
        if getattr(args,'expected_revision',None) is not None:resumed+=['--expected-revision',args.expected_revision]
        if command_id is not None:resumed+=['--command-id',command_id]
        completed=subprocess.run(resumed,cwd=root,text=True,capture_output=True,check=False)
        value=_result(completed.stdout)
        if completed.returncode or value is None:
            raise ValueError('Pinned task CLI could not resume: '+completed.stderr[-4000:])
        return value
    state=core.workspace_status(root)
    expected=getattr(args,'expected_revision',None)
    if expected is not None and expected!=state['revision']:
        raise ValueError('REVISION_CONFLICT: caller selected an older task state')
    inputs=_inputs(args,root,state)
    operation=core.begin_operation(root,args.operation,inputs=inputs,expected_revision=expected or state['revision'],
        command_id=command_id,request=request)
    if operation.get('replayed'):return _replay_operation(root,operation)
    output=Path(operation['output_dir']);resolved=operation['resolved_inputs']
    flags=_flags(resolved,args.operation)
    if args.operation=='review-open':flags+=['--review-dir',str(output)]
    else:flags+=['--output-dir',str(output)]
    flags+=['--workspace',str(root)]
    if binding is not None:flags+=['--binding-file',str(binding)]
    command=[*command,args.operation,*flags]
    env=os.environ.copy();env['REQPILOT_WORKSPACE_DEFER_SYNC']='1'
    logs=root/'operations';stdout_path=logs/(operation['operation_id']+'.stdout.log');stderr_path=logs/(operation['operation_id']+'.stderr.log')
    try:
        completed=subprocess.run(command,cwd=root,env=env,text=True,capture_output=True,check=False)
        stdout_path.write_text(completed.stdout,encoding='utf-8');stderr_path.write_text(completed.stderr,encoding='utf-8')
        stdout_path.chmod(0o600);stderr_path.chmod(0o600)
        if completed.returncode:
            raise ValueError('Compiler failed; inspect '+str(stderr_path)+': '+completed.stderr[-1500:])
        result=_result(completed.stdout)
        if result is None:
            raise ValueError('MALFORMED_TOOL_RESULT: successful process exit had no structured result; uncommitted output remains for inspection')
        if result.get('status') in BLOCKED_STATUSES:
            failure=core.fail_operation(root,operation['operation_id'],result.get('next',result['status']),state='BLOCKED')
            return {'status':result['status'],**failure,'compiler_result':result,'stdout_path':str(stdout_path),'stderr_path':str(stderr_path),**core.workspace_status(root)}
        committed=core.commit_operation(root,operation['operation_id'],result=result)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        failure=core.fail_operation(root,operation['operation_id'],exc)
        return {'status':'FAILED',**failure,'error':str(exc),'stdout_path':str(stdout_path),'stderr_path':str(stderr_path),
                'local_artifact_preserved':True,'next':'Inspect this operation and retry in a new operation directory; prior committed selections remain unchanged.'}
    response={'status':'WORKSPACE_OPERATION_SAVED',**committed,'compiler_result':result,
        'stdout_path':str(stdout_path),'stderr_path':str(stderr_path),'output_dir':str(output)}
    if committed['state']=='COMMITTED':
        arguments={key:Path(value) if key in PATH_ARGUMENTS else value for key,value in resolved.items()}
        arguments.update(command=args.operation,workspace=root,binding_file=binding,output_dir=output,
                         review_dir=output if args.operation=='review-open' else None)
        try:
            response.update(publish_committed(root,operation['operation_id'],Namespace(**arguments),result))
        except (ValueError,OSError,KeyError,TypeError) as exc:
            response['workbench_sync']={'status':'NOT_SYNCHRONIZED','error':str(exc),'local_artifact_preserved':True,
                'next':'The local commit above remains saved. Recheck its task selection and binding before publication.'}
        if args.operation=='deliver' and committed['current_working'].get('owner')=='portable_review':
            from revision_tools import attach_current_delivery
            try:
                response['delivery_attachment']=attach_current_delivery(Namespace(workspace=root,
                    review_dir=Path(committed['current_working']['review_root']),delivery_dir=output,
                    expected_review_sha256=operation['expected_review_sha256']))
                response.update(core.workspace_status(root))
                response['next']='The current review and same-version Excel are registered. Use review-verify for final handoff; create no candidate unless source or review state actually needs correction.'
            except (ValueError,OSError,KeyError,TypeError) as exc:
                response['delivery_attachment']={'status':'ATTACH_FAILED','error':str(exc),'local_artifact_preserved':True,
                    'next':'The COMMITTED export remains at output_dir. Inspect the current review SHA and exact state before attaching; an export alone has not advanced the review delivery pointer.'}
    else:
        response['publication']={'status':'NOT_PUBLISHED','reason':'The operation is a saved candidate; it did not replace the selected version.'}
    return response


def compact(value):
    """Keep actionable references once; complete history remains in owned files."""
    if not value.get('task_root'):return value
    result={key:item for key,item in value.items() if key not in {'index','committed_operations'}}
    root=Path(value['task_root'])
    result.update(task_path=str(root/'TASK.md'),context_path=str(root/'task-context.json'),index_path=str(root/'workspace-index.json'))
    if 'index' in value:
        index=value['index']
        result['index_summary']={'generation':index['generation'],'committed_count':len(index['committed']),
            'candidate_count':len(index['candidates']),'host_receipt_count':len(index['host_receipts'])}
    if value.get('brief'):
        result['brief']={key:item for key,item in value['brief'].items() if key not in {
            'source','current_request','request_origin','saved_artifacts','retained_stage_artifacts','review_version','workbench_path'} and item not in (None,[])}
    if value.get('current_working'):
        result['current_working']={key:value['current_working'][key] for key in
            ('owner','generation','review_root','review_sha256','version_path','baseline_sha256','document_map_sha256') if key in value['current_working']}
    for name in ('candidates','incomplete_operations'):
        if name in value:
            rows=value[name]
            result[name]=[{key:row[key] for key in ('operation_id','operation','state','reason','error','output_dir','receipt_path','receipt_sha256') if key in row} for row in rows[:20]]
            if len(rows)>20:result[name+'_omitted_count']=len(rows)-20
    if value.get('host_receipt'):
        result['host_receipt']={key:item for key,item in value['host_receipt'].items() if key!='receipt'}
    result['detail_hint']='Use --full for complete state/history; TASK.md and workspace-index.json retain the saved references.'
    return result


def dispatch(args,entrypoint):
    value={'init':lambda:initialize(args,entrypoint),'status':lambda:status(args),
            'recover':lambda:recover(args),'run':lambda:run(args,entrypoint)}[args.workspace_command]()
    return value if getattr(args,'full',False) else compact(value)
