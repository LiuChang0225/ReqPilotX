"""Scoped checks and explicit current-review synchronization for the task facade."""
from __future__ import annotations

from argparse import Namespace
import hashlib
import os
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from reqpilotx.application.agent_workflow import task_workspace as core
from reqpilotx.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
from workbench_tools import download, read, request, resolve_binding, sync_workbench, workbench_result_url, _validate_binding, _validate_state
from workspace_tools import _publication_lock


def _same_local_revision(root, revision):
    current=core.workspace_status(root,refresh=False)
    if current['revision']!=revision:
        raise ValueError('REVISION_CONFLICT: local task changed during workbench verification')
    return current


def _remote_current(binding, expected_revision, source_sha256):
    """Read the bound remote head and apply the existing exact revision/source rule."""
    current=request(_api(binding))
    _validate_state(current,binding)
    return current, (current.get('revision_id')==expected_revision
        and current.get('source_sha256')==source_sha256)


def _bound_scope(state):
    root=Path(state['task_root']).resolve(strict=True)
    actual=_same_local_revision(root,state['revision'])
    context=actual['context'];sha=context['source']['sha256']
    if state['context']['work_id']!=context['work_id'] or state['context']['source']['sha256']!=sha:
        raise ValueError('WORKBENCH_SCOPE_MISMATCH: the supplied state belongs to another task/source')
    path,_,task=resolve_binding(Namespace(workspace=root),sha)
    if not path.is_file():return root,actual,None
    binding=read(path);_validate_binding(binding,sha,task)
    # Old source-only bindings without a work owner cannot prove a task match.
    if binding.get('work_id')!=context['work_id']:
        raise ValueError('WORKBENCH_SCOPE_MISMATCH: binding has no matching persistent task owner')
    for field in ('project_id','document_id'):
        if not context.get(field) or binding.get(field)!=context[field]:
            raise ValueError('WORKBENCH_SCOPE_MISMATCH: binding differs from the task '+field)
    if Path(state['binding_file']).resolve()!=path:
        raise ValueError('WORKBENCH_SCOPE_MISMATCH: binding reference changed')
    selected_session=parse_qs(urlsplit(state.get('workbench_url') or '').query).get('pluginSessionId',[None])[0]
    if selected_session and selected_session!=binding['session_id']:
        raise ValueError('WORKBENCH_SCOPE_MISMATCH: selected session differs from the binding')
    return root,actual,binding


def _api(binding):
    return (binding['base_url']+'/api/projects/'+quote(binding['project_id'],safe='')+
        '/sor-documents/'+quote(binding['document_id'],safe='')+'/agent-structure/delivery/plugin-workbench/'+
        quote(binding['session_id'],safe=''))


def _validate_snapshot(value, source_sha256):
    if not isinstance(value,dict) or value.get('source_item_baseline',{}).get('source_sha256')!=source_sha256:
        raise ValueError('Snapshot source differs from this task')
    if any(key not in value for key in ('root_node_ids','nodes','logical_tables')):
        raise ValueError('Snapshot has no complete source-ledger content')
    if value.get('delivery_sha256')!=canonical_json_sha256({k:v for k,v in value.items() if k!='delivery_sha256'}):
        raise ValueError('Snapshot digest no longer matches its content')


def workbench_link(state: dict) -> dict:
    """Project a saved binding into an exact remote link without network or writes.

    This is deliberately separate from verifying current server content. A
    successful old synchronization remains linkable when local work advances.
    """
    unavailable={'url':None,'url_role':'NOT_AVAILABLE','workbench_entry':None,'chat_entry':None,
        'local_content_match':None,'remote_current_verified':False}
    try:
        context=state['context'];root=Path(state['task_root']).resolve(strict=True)
        path=Path(state['binding_file']).resolve()
        declared=Path(context['binding_file'])
        expected=(declared if declared.is_absolute() else root/declared).resolve()
        if path!=expected:raise ValueError('Binding reference differs from this task')
        if not path.is_file():return {**unavailable,'observation':'NOT_BOUND'}
        binding=read(path);_validate_binding(binding,context['source']['sha256'],(root,context))
        revision=binding['revision_id'];url=workbench_result_url(binding,revision)
        local=state.get('selected',{});current_review=state.get('current_working',{}).get('review_sha256')
        match=False if revision is None and local.get('MAP') else None
        inbox=path.with_name(path.stem+'-inbox.json')
        if inbox.is_file():
            remote=read(inbox);_validate_state(remote,binding)
            if remote.get('revision_id')==revision:
                stages={v['stage']:v['artifact_sha256'] for v in remote.get('stages',[])}
                if any(stages.get(key)!=local.get(key,{}).get('artifact_sha256') for key in ('MAP','ITEMS')):
                    match=False
                elif (current_review and binding.get('synchronized_local_review_sha256')==current_review
                        and binding.get('synchronized_result_revision')==revision):
                    match=True
        coordinates={'task_id':context['work_id'],'work_ref':binding.get('session_id'),
            'result_revision':revision,
            'project_id':binding.get('project_id'),'document_id':binding.get('document_id'),
            'source_sha256':binding.get('source_sha256'),
            'selection':{'projectId':binding.get('project_id'),'documentId':binding.get('document_id'),
                'pluginSessionId':binding.get('session_id'),
                **({'resultRevision':revision} if revision else {})}}
        return {'url':url,'url_role':'MANAGED_SERVICE_TRANSPORT_EPHEMERAL',
            'workbench_entry':{'kind':'PAIR_REQPILOTX_WORKBENCH','action':'OPEN_ORIGINAL_WORKBENCH',
                'user_action':'Open ReqPilotX in PAIR and select this saved work.',
                'url':None,'coordinates':coordinates},
            # This binding proves the ReqPilotX work/session identity, not a
            # Host-owned PAIR Session UUID. Standalone cannot invent chat identity.
            'chat_entry':None,
            'session_id':binding['session_id'],'result_revision':revision,
            'link_target':'SAVED_REMOTE_RESULT' if revision else 'SOURCE_WORK',
            'local_content_match':match,'local_revision':state['revision'],
            'observation':'SAVED_BINDING','remote_current_verified':False}
    except (ValueError,OSError,KeyError,TypeError) as exc:
        return {**unavailable,'observation':'BINDING_UNAVAILABLE','error':str(exc)}


def check_workbench(state: dict, local_snapshot: dict) -> dict:
    """Verify one exact local basis and one project/document/session-bound view."""
    unavailable={'workbench_content_verified':False,'rendered_ui_verified':False}
    try:
        root,actual,binding=_bound_scope(state)
    except (ValueError,KeyError,TypeError) as exc:
        return {**unavailable,'workbench_status':'LOCAL_REVISION_CHANGED' if str(exc).startswith('REVISION_CONFLICT:') else 'SCOPE_MISMATCH','workbench_error':str(exc)}
    except (OSError,RuntimeError) as exc:
        return {**unavailable,'workbench_status':'UNAVAILABLE','workbench_error':str(exc)}
    if binding is None:return {**unavailable,'workbench_status':'NOT_BOUND'}
    try:
        _validate_snapshot(local_snapshot,actual['context']['source']['sha256'])
        api=_api(binding)
        remote=request(api);_validate_state(remote,binding)
        if not remote.get('revision_id'):return {**unavailable,'workbench_status':'NO_ARTIFACTS'}
        remote_revision=remote['revision_id']
        pinned=parse_qs(urlsplit(state.get('workbench_url') or '').query).get('resultRevision',[None])[0]
        revision=pinned or remote_revision
        url=workbench_result_url(binding,revision)
        snapshot=request(api+'/snapshot?revision='+quote(revision,safe=''))
        _validate_snapshot(snapshot,binding['source_sha256'])
        observed=request(api);_validate_state(observed,binding)
        if observed.get('revision_id')!=remote_revision:
            return {**unavailable,'workbench_status':'REMOTE_REVISION_CHANGED','workbench_revision':revision}
        _same_local_revision(root,actual['revision'])
        same=all(snapshot[key]==local_snapshot[key] for key in ('root_node_ids','nodes','logical_tables','source_item_baseline'))
        return {'workbench_content_verified':same,'workbench_status':'CONTENT_MATCH' if same else 'CONTENT_DIFFERS',
            'workbench_revision':revision,'checked_local_revision':actual['revision'],
            'workbench_url':url,'workbench_is_current':revision==remote_revision,
            'remote_current_revision':remote_revision,
            'checked_scope':{key:binding[key] for key in ('work_id','project_id','document_id','session_id','source_sha256')},
            'rendered_ui_verified':False}
    except (ValueError,KeyError,TypeError) as exc:
        message=str(exc)
        status=('LOCAL_REVISION_CHANGED' if message.startswith('REVISION_CONFLICT:') else
                'SCOPE_MISMATCH' if 'receipt' in message and 'binding' in message else 'UNAVAILABLE')
        return {**unavailable,'workbench_status':status,'workbench_error':message}
    except (OSError,RuntimeError) as exc:
        return {**unavailable,'workbench_status':'UNAVAILABLE','workbench_error':str(exc)}


def materialize_authoritative_draft(root: Path, state: dict, binding: dict, draft: dict) -> dict:
    """Download the exact retained DRAFT export into a task-owned durable copy."""
    delivery = draft.get('delivery') or {}
    identity = {'result_revision': delivery.get('revision_id'),
        'delivery_revision': delivery.get('revision'),
        'delivery_sha256': delivery.get('delivery_sha256'),
        'export_sha256': delivery.get('export_sha256'),
        'filename': delivery.get('filename'),
        'work_id': state['context']['work_id'],
        'work_ref': binding.get('session_id'),
        'project_id': binding.get('project_id'),
        'document_id': binding.get('document_id'),
        'source_sha256': binding.get('source_sha256')}
    if (draft.get('status') not in {'DRAFT_REGISTERED', 'DRAFT_REUSED'}
            or not draft.get('download') or not isinstance(identity['export_sha256'], str)
            or not isinstance(identity['delivery_sha256'], str)):
        return {'status': 'AUTHORITATIVE_DRAFT_UNAVAILABLE', 'reason': 'CURRENT_DRAFT_EXPORT_NOT_REGISTERED',
            'durable': False, 'exact_download': False, **identity}
    try:
        data, headers = download(draft['download'])
        actual_export = hashlib.sha256(data).hexdigest()
        if actual_export != identity['export_sha256']:
            raise ValueError('Downloaded DRAFT bytes differ from the registered export SHA')
        expected_headers = {'x-reqpilot-revision': identity['result_revision'],
            'x-reqpilotx-delivery-sha256': identity['delivery_sha256'],
            'x-reqpilotx-export-sha256': identity['export_sha256']}
        for name, expected in expected_headers.items():
            if headers.get(name) not in (None, expected):
                raise ValueError('Downloaded DRAFT response belongs to another revision/source')
        if delivery.get('source_sha256') not in (None, identity['source_sha256']):
            raise ValueError('Downloaded DRAFT belongs to another source')
        directory = root / 'authoritative-deliveries'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / (actual_export + '.xlsx')
        if target.is_file():
            if core.file_sha256(target) != actual_export:
                raise ValueError('Saved authoritative DRAFT copy changed')
        else:
            temporary = target.with_name('.' + target.name + '.tmp')
            try:
                temporary.write_bytes(data); temporary.chmod(0o600)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        receipt = {**identity, 'schema_version': 'reqpilot-authoritative-draft-copy/v1',
            'origin': 'RETAINED_DRAFT_DOWNLOAD', 'human_acceptance': 'NOT_CLAIMED',
            'local_path': str(target.relative_to(root))}
        receipt['receipt_sha256'] = canonical_json_sha256(receipt)
        core._atomic_json(directory / (actual_export + '.json'), receipt)
        return {'status': 'AUTHORITATIVE_DRAFT_READY', 'reason': None,
            'path': str(target), 'receipt_path': str(directory / (actual_export + '.json')),
            'durable': True, 'exact_download': True, 'url_role': 'LOCAL_MATERIALIZED_COPY',
            **identity}
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        return {'status': 'AUTHORITATIVE_DRAFT_UNAVAILABLE', 'reason': str(exc),
            'durable': False, 'exact_download': False, **identity}


def sync_current_review(root: Path, expected_revision: str, *, materialize_authoritative: bool = False) -> dict:
    """Explicitly publish the current review, without impersonating its old op."""
    found=core.find_task_workspace(explicit=Path(root))
    if found is None:raise ValueError('INVALID_TASK: no persistent task context for current-review synchronization')
    root=found[0]
    with _publication_lock(root):
        state=core.workspace_status(root,refresh=False)
        if expected_revision!=state['revision']:
            raise ValueError('REVISION_CONFLICT: refresh the current review before synchronization')
        owner=state['current_working']
        if owner.get('owner')!='portable_review':
            raise ValueError('CURRENT_REVIEW_REQUIRED: synchronize an existing current review')
        _,_,binding=_bound_scope(state)
        if binding is None:
            return {'status':'NOT_BOUND','source_and_model_not_recompiled':True,'local_artifact_preserved':True}
        basis={'revision':state['revision'],'work_id':state['context']['work_id'],
            'source_sha256':state['context']['source']['sha256'],'review_sha256':owner['review_sha256'],
            'document_map_sha256':owner['document_map_sha256'],'baseline_sha256':owner['baseline_sha256']}
        try:
            # No outer review flock: pull imports take binding -> review locks.
            # sync_workbench serializes POST under its binding lock and rechecks
            # this exact review SHA there, including a pending Map-only publish.
            result=sync_workbench(Namespace(workspace=root,review_dir=Path(owner['review_root']),
                expected_review_sha256=owner['review_sha256']))
        except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:
            return {'status':'NOT_SYNCHRONIZED','synchronized_basis':basis,'error':str(exc),
                'source_and_model_not_recompiled':True,'local_artifact_preserved':True}
        current=core.workspace_status(root,refresh=False)
        unchanged=current['revision']==state['revision']
        draft=None; authoritative=None
        if unchanged and current.get('workflow') and (current.get('workflow_check') or {}).get('status')=='PASSED':
            draft=_ensure_current_draft(current,binding,result.get('revision_id'))
            if draft and draft.get('status') in {'DRAFT_REGISTERED','DRAFT_REUSED'}:
                final=core.workspace_status(root,refresh=False)
                current=final
                if final['revision']!=state['revision']:
                    draft={'status':'CONFLICT','reason':'LOCAL_REVISION_CHANGED','local_artifact_preserved':True,
                           'committed_delivery':draft.get('delivery')}
                    unchanged=False
                elif materialize_authoritative:
                    authoritative=materialize_authoritative_draft(root,current,binding,draft)
                    if authoritative.get('status')=='AUTHORITATIVE_DRAFT_READY':
                        # The download is frozen to this exact revision/hash, but
                        # the handoff may only claim currentness after rechecking
                        # both owners because a public manual response can land
                        # while the export is in flight.
                        final_local=core.workspace_status(root,refresh=False)
                        local_ok=(final_local['revision']==state['revision']
                            and final_local['current_working'].get('review_sha256')==owner['review_sha256'])
                        try:
                            _, remote_ok=_remote_current(binding,result.get('revision_id'),
                                state['context']['source']['sha256'])
                            remote_error=None
                        except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:
                            remote_ok=False; remote_error=str(exc)
                        if not local_ok or not remote_ok:
                            authoritative={**authoritative,'status':'AUTHORITATIVE_DRAFT_STALE',
                                'currentness':'STALE','stale':True,'refresh_required':True,
                                'reason':'LOCAL_OR_REMOTE_HEAD_CHANGED_DURING_DOWNLOAD',
                                **({'remote_error':remote_error} if remote_error else {})}
                            draft={**draft,'status':'CONFLICT','reason':'CURRENTNESS_CHANGED_DURING_DOWNLOAD',
                                'remote_current':False,'local_artifact_preserved':True}
                            unchanged=False
                        else:
                            authoritative={**authoritative,'currentness':'CURRENT','stale':False,'refresh_required':False}
        status=('CURRENT_REVIEW_SYNCHRONIZED' if result['status']=='WORKBENCH_SYNCHRONIZED' else result['status']) if unchanged else (
            'CURRENTNESS_CHANGED' if authoritative and authoritative.get('status')=='AUTHORITATIVE_DRAFT_STALE'
            else 'SYNCHRONIZED_LOCAL_ADVANCED')
        return {'status':status,'workbench_sync':result,'draft_delivery':draft,'authoritative_draft':authoritative,
            'synchronized_basis':basis,'current_revision':current['revision'],
            'still_current':unchanged,'source_and_model_not_recompiled':True,'local_artifact_preserved':True}



def _ensure_current_draft(state, binding, expected_revision):
    """Register or reuse the exact current DRAFT; never formal or latest-fallback."""
    check=state.get('workflow_check') or {}
    selected=state.get('selected') or {}; review=state.get('current_working') or {}
    basis=check.get('basis') or {}
    if (not state.get('workflow') or check.get('status')!='PASSED'
            or basis.get('source_sha256')!=state['context']['source']['sha256']
            or basis.get('map_sha256')!=selected.get('MAP',{}).get('artifact_sha256')
            or basis.get('items_sha256')!=selected.get('ITEMS',{}).get('artifact_sha256')
            or basis.get('review_sha256')!=review.get('review_sha256')):
        return {'status':'NOT_PUBLISHED','reason':'CURRENT_PASSED_CHECK_REQUIRED','local_artifact_preserved':True}
    def remote_current():
        return _remote_current(binding, expected_revision, state['context']['source']['sha256'])
    api=_api(binding)
    try:
        preview=request(api+'/versions/preview')
        _validate_state(preview,binding)
        if preview.get('revision_id')!=expected_revision:
            return {'status':'CONFLICT','reason':'WORKBENCH_REVISION_CHANGED','local_artifact_preserved':True}
        if check.get('delivery_snapshot_sha256')!=preview.get('delivery_sha256') or preview.get('source_sha256')!=state['context']['source']['sha256']:
            return {'status':'CONFLICT','reason':'DELIVERY_SNAPSHOT_MISMATCH','local_artifact_preserved':True}
        versions=request(api+'/versions')
        _validate_state(versions,binding)
        for item in versions.get('versions',[]):
            if (item.get('source_sha256')==state['context']['source']['sha256']
                    and item.get('revision_id')==expected_revision
                    and item.get('delivery_sha256')==check['delivery_snapshot_sha256']
                    and item.get('mode')=='DRAFT' and item.get('acceptance') is None):
                current, is_current = remote_current()
                if not is_current:
                    return {'status':'CONFLICT','reason':'WORKBENCH_REVISION_CHANGED',
                        'remote_revision':current.get('revision_id'),'local_artifact_preserved':True}
                return {'status':'DRAFT_REUSED','delivery':item,'remote_current':True,
                    'download':api+'/versions/'+str(item['revision'])+'/export','local_artifact_preserved':True}
        current, is_current = remote_current()
        if not is_current:
            return {'status':'CONFLICT','reason':'WORKBENCH_REVISION_CHANGED',
                'remote_revision':current.get('revision_id'),'local_artifact_preserved':True}
        command='fixed-workflow-draft-'+canonical_json_sha256([state['context']['work_id'],
            state['context']['source']['sha256'], expected_revision, preview.get('response_revision',0),
            check['delivery_snapshot_sha256']])[:32]
        saved=request(api+'/versions',{'command_id':command,
            'expected_source_sha256':state['context']['source']['sha256'],
            'expected_revision_id':expected_revision,'expected_version':preview['version'],
            'expected_response_revision':preview.get('response_revision',0),
            'expected_delivery_sha256':preview['delivery_sha256'],'mode':'DRAFT','accept_snapshot':False})
        _validate_state(saved,binding)
        if (saved.get('mode')!='DRAFT' or saved.get('acceptance') is not None
                or saved.get('delivery_sha256')!=check['delivery_snapshot_sha256']):
            return {'status':'CONFLICT','reason':'DRAFT_DELIVERY_NOT_CONFIRMED','local_artifact_preserved':True}
        current_versions=request(api+'/versions'); _validate_state(current_versions,binding)
        if not any(item.get('revision')==saved.get('revision') and item.get('delivery_sha256')==check['delivery_snapshot_sha256']
                   and item.get('acceptance') is None for item in current_versions.get('versions',[])):
            return {'status':'CONFLICT','reason':'DRAFT_DELIVERY_NOT_READABLE','local_artifact_preserved':True}
        current, is_current = remote_current()
        if not is_current:
            return {'status':'CONFLICT','reason':'WORKBENCH_REVISION_CHANGED',
                'remote_revision':current.get('revision_id'),'committed_delivery':saved,
                'local_artifact_preserved':True}
        return {'status':'DRAFT_REGISTERED','delivery':saved,'remote_current':True,
            'download':api+'/versions/'+str(saved['revision'])+'/export','local_artifact_preserved':True}
    except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:
        return {'status':'UNAVAILABLE','reason':'WORKBENCH_DRAFT_NOT_PUBLISHED','error':str(exc),
            'local_artifact_preserved':True,'next':'Keep the local delivery and retry publish_current_review for this exact revision.'}


def read_workbench_delivery(state, selection):
    """Read only this task's saved remote binding, including immutable versions.

    No latest-task fallback, opinion authoring, formal acceptance or local
    import occurs. Existing local review artifacts keep their own provenance.
    """
    root, actual, binding = _bound_scope(state)
    if binding is None:
        return {'status':'NOT_BOUND', 'task_id':actual['context']['work_id'], 'revision':actual['revision']}
    suffix = {'responses':'/engineer-responses', 'preview':'/versions/preview', 'versions':'/versions',
              'version':'/versions/'+str(selection.delivery_revision)}[selection.view]
    if selection.result_revision:
        suffix += '?revision='+quote(selection.result_revision, safe='')
    value = request(_api(binding)+suffix)
    _validate_state(value, binding)
    if selection.result_revision and value.get('revision_id') != selection.result_revision:
        raise ValueError('WORKBENCH_VERSION_MISMATCH: response is not the selected result')
    if selection.delivery_revision is not None and value.get('revision') != selection.delivery_revision:
        raise ValueError('WORKBENCH_VERSION_MISMATCH: response is not the selected delivery')
    if selection.view in {'preview','version'}:
        _validate_snapshot(value['snapshot'],binding['source_sha256'])
        if value.get('delivery_sha256') != value['snapshot']['delivery_sha256']:
            raise ValueError('WORKBENCH_VERSION_MISMATCH: snapshot differs from the delivery')
    _same_local_revision(root, actual['revision'])
    return {'status':'READY', 'task_id':actual['context']['work_id'], 'revision':actual['revision'],
        'view':selection.view, 'delivery':value, 'local_artifacts_changed':False, 'human_acceptance':'NOT_CLAIMED'}


__all__=['check_workbench','sync_current_review','workbench_link','read_workbench_delivery']
