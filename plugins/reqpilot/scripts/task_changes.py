"""Route typed judgments through existing compilers and one review command."""
from pathlib import Path
import json

from task_change_contracts import (bind_annotation_changes, bind_decomposition_decisions,
    bind_existing_item_changes, bind_image_items, source_item_record)
from app.application.agent_workflow import task_workspace as core

INITIAL={'images':'compile-images','annotations':'compile-annotations','decomposition':'compile-decomposition'}


class ReviewRequired(ValueError):
    def __init__(self,state):
        super().__init__('REVIEW_REQUIRED: recording/resolving findings requires a working review; ordinary initial annotations take changes only')
        self.next_action={'tool':'review_prepare','request':{'task_id':state['context']['work_id'],'expected_revision':state['revision']}}
        self.retry_guidance='After review_prepare succeeds, use its returned revision for this unexecuted change. Preserve actual finding text. USER origin still needs real imported feedback; never invent it.'


def current_inputs(state):
    selected=state['selected']
    if not selected.get('MAP'):raise ValueError('MAP_REQUIRED: prepare the source Map first')
    map_dir=map_read_dir(state)
    if selected.get('ITEMS'):
        directory=Path(selected['ITEMS']['output_dir'])
        baseline=json.loads((directory/'source-requirement-baseline.json').read_text())
    else:directory=map_dir;baseline=None
    mapping=json.loads((map_dir/'document-map.json').read_text())
    tables=json.loads((directory/'table-previews.json').read_text())
    return directory,mapping,baseline,tables


def map_read_dir(state):
    reference=state['selected'].get('MAP')
    if not reference:raise ValueError('MAP_REQUIRED: prepare the source Map first')
    return Path(reference['read_bundle_dir']) if reference.get('read_bundle_dir') else Path(reference['path']).parent


def compiler_decisions(state,action,request):
    _,mapping,baseline,tables=current_inputs(state)
    if action=='images':return bind_image_items(request.changes,mapping,baseline,table_previews=tables)
    if baseline is None:raise ValueError('ITEMS_REQUIRED: prepare original source items first')
    if action=='annotations':return bind_annotation_changes(request.changes,mapping,baseline,table_previews=tables)
    if action=='decomposition':
        return bind_decomposition_decisions(request.changes,baseline,table_previews=tables,
            authorization_request=state['context']['request']['goal'])
    raise ValueError('UNSUPPORTED_CHANGE')


def execute_change(service,root,state,action,request):
    from task_service import digest,task_summary
    from task_review import apply_review
    explicit={'action':action,'request':request.model_dump(mode='json',exclude={'command_id'},exclude_unset=True)}
    command=request.command_id or 'task-api-'+digest(explicit)
    identifier=core._command_operation_id(state['context'],command)
    descriptor=root/'operations'/(identifier+'.json')
    operation=None
    if descriptor.is_file():
        operation=core._operation(root,identifier)['operation']
        if operation not in {INITIAL.get(action),'review-apply'}:
            raise ValueError('COMMAND_ID_CONFLICT: command belongs to another action')
        # Authenticate the original request before using its recorded route. A
        # replay must not change from initial compiler to review mutation merely
        # because a review has since been created.
        core.lookup_operation_command(root,command,operation,explicit)
    reviewed=state['current_working'].get('owner')=='portable_review'
    if action in INITIAL and (operation==INITIAL[action] or (operation is None and not reviewed)):
        if getattr(request,'origin','AGENT_SELF_REVIEW')!='AGENT_SELF_REVIEW' or getattr(request,'resolutions',[]) or getattr(request,'self_review_finding',None):
            raise ReviewRequired(state)
        return service.write(root,state,action,request)
    if action=='images':raise ValueError('EXISTING_REVIEW: initial independent-image recognition must precede review preparation; revise existing image items explicitly')
    if not reviewed:raise ReviewRequired(state)
    revision=None;compiled=None;finding=None
    if operation is None:
        if state['revision']!=request.expected_revision:raise ValueError('REVISION_CONFLICT: refresh the affected source before writing')
        _,mapping,baseline,tables=current_inputs(state)
        if action=='revise':revision=bind_existing_item_changes(request.changes,mapping,baseline,table_previews=tables)
        elif action=='annotations':
            envelope=compiler_decisions(state,action,request)
            revision={k:envelope[k] for k in ('visual_readings','annotation_updates') if envelope.get(k)}
        else:
            revision={};compiled={'decomposition':compiler_decisions(state,action,request)}
        addressed=list(revision.get('addressed_feedback_ids',[]))
        for resolution in request.resolutions:
            if resolution.disposition=='ADDRESSED' and resolution.feedback_id not in addressed:
                addressed.append(resolution.feedback_id)
        if addressed:revision['addressed_feedback_ids']=addressed
        revision.update(schema_version='reqpilot-review-revision/v1',
            expected_review_sha256=state['current_working']['review_sha256'],expected_baseline_sha256=baseline['artifact_sha256'])
        if request.self_review_finding:
            finding=request.self_review_finding.model_dump(mode='json')
            finding['source_item_ids']=[source_item_record(baseline,ref).source_record_id for ref in finding['source_item_ids']]
    value=apply_review(root,expected_revision=request.expected_revision,command_id=command,revision=revision,
        compile_replacements=compiled,origin=request.origin,
        resolutions=[r.model_dump(mode='json') for r in request.resolutions],
        self_review_finding=finding,request_payload=explicit)
    try:summary=task_summary(core.workspace_status(root,refresh=False))
    except Exception:summary={'task_id':request.task_id,'task_root':str(root)}
    response={**summary,**value,'human_acceptance':'NOT_CLAIMED','handoff_ready':False}
    if value.get('operation_state')=='COMMITTED' and value.get('review_still_current'):
        try:
            check=service.check(root,core.workspace_status(root,refresh=False))
            response['delivery_check']=check
            response['handoff_ready']=bool(check.get('review_verified') and check.get('current_delivery_registered') and check.get('workbench_content_verified')
                and not check.get('pending_decomposition',{}).get('remaining_count'))
        except Exception as exc:response['delivery_check']={'status':'FAILED','message':str(exc),'local_artifact_preserved':True}
    return response
