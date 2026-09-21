"""Task-bound item/feedback reading; no paths or hashes supplied by the Agent."""
from pathlib import Path
import json,hashlib

from task_changes import current_inputs,map_read_dir
from task_reading import _window,_text
from task_change_reading import source_item_read,source_item_image,image_scope_read,image_scope_image


def _overlay(state,key):
    ref=state['selected'].get(key)
    return json.loads(Path(ref['path']).read_text()) if ref else None


def read_item_view(state,request):
    root=Path(state['task_root']); selected=state['selected']; sha=state['context']['source']['sha256']
    mapping_ref=selected.get('MAP',{});baseline_ref=selected.get('ITEMS',{})
    if request.expected_map_sha256 and request.expected_map_sha256!=mapping_ref.get('artifact_sha256'):raise ValueError('MAP_VERSION_CONFLICT')
    if request.expected_baseline_sha256 and request.expected_baseline_sha256!=baseline_ref.get('artifact_sha256'):raise ValueError('BASELINE_VERSION_CONFLICT')
    budget={'offset':request.offset,'limit':request.limit,'max_chars':request.max_chars}
    if request.view=='feedback':
        from revision_tools import load_head
        owner=state['current_working']
        if owner.get('owner')!='portable_review':raise ValueError('REVIEW_REQUIRED')
        head=load_head(Path(owner['review_root']),owner['review_sha256']);rows=head['feedback']
        if request.feedback_id:
            value=next((row for row in rows if row['feedback_id']==request.feedback_id),None)
            if value is None:raise ValueError('UNKNOWN_FEEDBACK')
            base={k:v for k,v in value.items() if k!='text'}
            return _text(base,value['text'],offset=request.offset,max_chars=request.max_chars,
                reader='feedback_read',arguments={'feedback_id':request.feedback_id})
        rows=[{**{k:v for k,v in row.items() if k not in {'text','rationale'}},'preview':row['text'][:240],
            'preview_complete':len(row['text'])<=240,'read':{'reader':'feedback_read','arguments':{'feedback_id':row['feedback_id']}}} for row in rows]
        return _window({'review_sha256':head['review_sha256']},rows,field='feedback',reader='feedback_read',arguments={},**budget)
    directory,mapping,baseline,tables=current_inputs(state)
    common={'expected_source_sha256':sha,'expected_map_sha256':mapping_ref['artifact_sha256']}
    from task_service import TaskService
    link_transform=lambda value:TaskService.read_links(value,request.task_id,None,
        mapping_ref['artifact_sha256'],baseline['artifact_sha256'] if baseline is not None else None)
    if request.view=='items':
        if baseline is None:raise ValueError('ITEMS_REQUIRED')
        from reqpilotx.application.agent_workflow.source_item_workbench_metadata import project_workbench_metadata
        numbering=project_workbench_metadata(mapping,baseline,table_previews=tables,annotations=_overlay(state,'ANNOTATIONS'))['numbers']
        entries=[{**{k:row.get(k) for k in ('source_record_id','source_display_id','requirement_display_id','source_form','requirementness','requirement_kinds')},
            'source_number':numbering[row['source_record_id']]['source_number'],
            'source_number_role':numbering[row['source_record_id']]['source_number_role'],
            'label':row.get('label','')[:240], 'read':{'reader':'source_item_read','arguments':{'source_item_id':row['source_display_id']}}} for row in baseline['records']]
        return _window({**common,'baseline_sha256':baseline['artifact_sha256'],
            'numbering_note':'These are current projections, not proof that empty/UNNUMBERED items have no original number. Check actual text/heading/table-cell evidence and use the existing exact-number or number-collection annotation for missing or wrong bindings; never invent a number.'},entries,field='items',reader='items_read',arguments={},**budget)
    source=root/state['context']['source']['path']
    cache=root/'reading'/'item-image-cache'/hashlib.sha256((mapping_ref['artifact_sha256']+str(request.source_item_id)+str(request.node_id)+str(request.image_anchor_id)).encode()).hexdigest()
    if request.view.startswith('image_scope'):
        if not request.node_id:
            nodes=[{'node_id':n['node_id'],'title':n['title'],'read':{'reader':'image_scope_read','arguments':{'node_id':n['node_id']}}}
                for n in mapping['nodes'] if any(a['anchor_kind'] in {'IMAGE','FIGURE'} for a in n.get('source_anchors',[]))]
            return _window(common,nodes,field='image_nodes',reader='image_scope_read',arguments={},**budget)
        common.update(node_id=request.node_id,baseline_dir=directory if baseline is not None else None,
            expected_baseline_sha256=baseline['artifact_sha256'] if baseline else None)
        if request.view=='image_scope':return image_scope_read(map_read_dir(state),link_transform=link_transform,**common,**budget)
        if not request.image_anchor_id:raise ValueError('IMAGE_ANCHOR_REQUIRED')
        picture=image_scope_image(map_read_dir(state),image_anchor_id=request.image_anchor_id,source_path=source,image_dir=cache,**common)
    else:
        if baseline is None or not request.source_item_id:raise ValueError('SOURCE_ITEM_REQUIRED: select an actual SRC from the items view')
        common.update(source_item_id=request.source_item_id,expected_baseline_sha256=baseline['artifact_sha256'])
        if request.view=='item':return source_item_read(directory,part=request.part,annotations=_overlay(state,'ANNOTATIONS'),decomposition=_overlay(state,'DECOMPOSITION'),link_transform=link_transform,**common,**budget)
        if not request.image_anchor_id:raise ValueError('IMAGE_ANCHOR_REQUIRED')
        picture=source_item_image(directory,image_anchor_id=request.image_anchor_id,source_path=source,image_dir=cache,**common)
    return {'image':picture,'_images':[picture]}
