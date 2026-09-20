"""Bounded, source-pinned reading views for a caller's explicitly selected task.

TaskService authorizes/validates task-owned operation paths. This module never
finds a current task, selects a new version, writes evidence, or records reading
as semantic acceptance. Every truncated value has an exact continuation.
"""
from __future__ import annotations
from argparse import Namespace
import hashlib
import json
from pathlib import Path
import re
import struct

from app.application.agent_workflow import source_requirement_baseline as core
from app.domain.extraction.source_ir import SourceIRSnapshot
from source_authoring_tools import authoring_view


def _read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def _hash(raw):return hashlib.sha256(raw).hexdigest()
def _size(value):return len(json.dumps(value,ensure_ascii=False,separators=(',',':')))


def _limits(offset,limit,max_chars):
    if type(offset) is not int or offset<0:raise ValueError('offset must be a nonnegative integer')
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be between 1 and 100')
    if type(max_chars) is not int or not 2048<=max_chars<=100000:raise ValueError('max_chars must be between 2048 and 100000')


def _next(reader,arguments):return {'reader':reader,'arguments':arguments}


def _window(base,items,*,field,offset,limit,max_chars,reader,arguments):
    _limits(offset,limit,max_chars)
    if offset>len(items):raise ValueError('offset is past this exact result')
    chosen=[]
    def payload(values):
        end=offset+len(values);more=end<len(items)
        return {**base,field:values,'offset':offset,'count':len(values),'total':len(items),'complete':not more,
            'budget_scope':'RESULT_JSON_CHARACTERS','next_read':_next(reader,{**arguments,'offset':end,'limit':limit,'max_chars':max_chars}) if more else None}
    for item in items[offset:offset+limit]:
        if _size(payload([*chosen,item]))>max_chars:break
        chosen.append(item)
    if not chosen and offset<len(items):raise ValueError('max_chars cannot fit this entry metadata; increase the requested budget')
    return payload(chosen)


def _text(base,text,*,offset,max_chars,reader,arguments):
    _limits(offset,1,max_chars)
    if offset>len(text):raise ValueError('offset is past this exact text')
    def payload(count):
        end=offset+count;more=end<len(text)
        return {**base,'text':text[offset:end],'offset':offset,'returned_characters':count,'total_characters':len(text),
            'complete':not more,'budget_scope':'RESULT_JSON_CHARACTERS',
            'next_read':_next(reader,{**arguments,'offset':end,'max_chars':max_chars}) if more else None}
    lo=0;hi=min(max_chars,len(text)-offset)
    while lo<hi:
        middle=(lo+hi+1)//2
        if _size(payload(middle))<=max_chars:lo=middle
        else:hi=middle-1
    value=payload(lo)
    if _size(value)>max_chars or (not lo and offset<len(text)):raise ValueError('max_chars cannot fit text metadata; increase the requested budget')
    return value


def _source(source_ir,expected_source_sha256,capture_sha256=None):
    raw=Path(source_ir).read_bytes();digest=_hash(raw)
    if capture_sha256 is not None and digest!=capture_sha256:raise ValueError('Source capture changed; reload its current references')
    snapshot=SourceIRSnapshot.model_validate_json(raw)
    if snapshot.source_sha256!=expected_source_sha256:raise ValueError('SourceIR belongs to another task original')
    if any(node.native_locator.format.value!='pdf' for node in snapshot.nodes):raise ValueError('This first task reader supports PDF sources')
    return snapshot,digest


def _source_header(snapshot,digest):
    return {'schema_version':'reqpilot-task-reading/v1','source_sha256':snapshot.source_sha256,
        'capture_sha256':digest,'source_ir_id':snapshot.source_ir_id,'capture_status':snapshot.status.value,
        'semantic_authority':'NONE','ref_namespace':'ZERO_BASED_NODE_IN_THIS_EXACT_CAPTURE'}


def source_authoring(source_ir,*,expected_source_sha256,pages=None,refs=None,query=None,offset=0,limit=20,max_chars=12000):
    """Return ordinary text in full; only oversized blocks need part reads."""
    _limits(offset,limit,max_chars)
    snapshot,digest=_source(source_ir,expected_source_sha256)
    if any(type(page) is not int or page<1 for page in pages or []):raise ValueError('pages must contain positive original page numbers')
    ids={node.source_node_id:index for index,node in enumerate(snapshot.nodes)}
    indexes=[]
    for value in refs or []:
        if type(value) is int and 0<=value<len(snapshot.nodes):indexes.append(value)
        elif isinstance(value,str) and value in ids:indexes.append(ids[value])
        else:raise ValueError('Unknown source ref for this capture')
    # Reuse the established filtering/ref semantics. Table cells remain a
    # separate paged view; ordinary paragraphs need no redundant per-ref read.
    selected=authoring_view(snapshot,Namespace(source_ir=Path(source_ir),page=pages or [],source_id=[],
        capture_index=indexes,query=query,offset=offset,limit=limit))
    if offset>selected['total']:raise ValueError('offset is past this exact source selection')
    arguments={'pages':pages,'refs':refs,'query':query}
    entries=[]
    for row in selected['nodes']:
        item={key:row[key] for key in ('ref','source_node_id','captured_kind','page','bbox')}
        text=row['raw_text'][:160] if row.get('table') else row['raw_text']
        item.update(raw_text=text,text_complete=len(text)==len(row['raw_text']),text_total_characters=len(row['raw_text']))
        if row.get('asset_ref'):item['asset_ref']=row['asset_ref'];item['page_image_read']=_next('page_image',{'page':row['page']})
        if row.get('table'):
            item['read']=_next('source_part',{'capture_sha256':digest,'ref':row['ref'],'part':'table'})
            table=row['table'];item['table']={key:table[key] for key in ('row_count','column_count','map_fragment')}
            item['table']['cell_count']=len(table['cells'])
        entries.append(item)
    base={**_source_header(snapshot,digest),'view':'AUTHORING_EVIDENCE','total':selected['total'],
          'completeness_scope':'complete describes the node window; individual text_complete/table reads describe content coverage'}
    chosen=[]
    def payload(values):
        end=offset+len(values);more=end<selected['total']
        return {**base,'nodes':values,'offset':offset,'count':len(values),'complete':not more,
            'budget_scope':'RESULT_JSON_CHARACTERS',
            'next_read':_next('source_authoring',{**arguments,'offset':end,'limit':limit,'max_chars':max_chars}) if more else None}
    for item in entries:
        if _size(payload([item]))>max_chars:
            item={**item,'raw_text':item['raw_text'][:min(512,max_chars//6)],'text_complete':False}
        if not item['text_complete']:
            item['next_text_read']=_next('source_part',{'capture_sha256':digest,'ref':item['ref'],'part':'text',
                'offset':len(item['raw_text']),'max_chars':max_chars})
        if _size(payload([*chosen,item]))>max_chars:break
        chosen.append(item)
    if not chosen and entries:raise ValueError('max_chars cannot fit source metadata; increase the requested budget')
    return payload(chosen)


def _cell_entries(cells,*,text_field,reader,arguments,part_prefix,max_chars):
    preview_limit=min(512,max_chars//6)
    entries=[]
    for index,cell in enumerate(cells):
        text=str(cell.get(text_field,''));part=part_prefix+str(index)
        item={key:cell[key] for key in ('cell_id','row','column','row_span','column_span','bbox') if key in cell}
        item.update(cell_ref=part,text=text[:preview_limit],text_total_characters=len(text),text_complete=len(text)<=preview_limit)
        if len(text)>preview_limit:item['next_text_read']=_next(reader,{**arguments,'part':part,'offset':preview_limit,'max_chars':max_chars})
        entries.append(item)
    return entries


def source_part(source_ir,*,expected_source_sha256,capture_sha256,ref,part='text',offset=0,limit=20,max_chars=12000):
    _limits(offset,limit,max_chars)
    snapshot,digest=_source(source_ir,expected_source_sha256,capture_sha256)
    if type(ref) is not int or not 0<=ref<len(snapshot.nodes):raise ValueError('ref is outside this exact capture')
    node=snapshot.nodes[ref]
    base={**_source_header(snapshot,digest),'ref':ref,'source_node_id':node.source_node_id,
          'captured_kind':node.kind.value,'page':node.native_locator.page_number,'bbox':node.native_locator.bbox}
    arguments={'capture_sha256':digest,'ref':ref,'part':part}
    if part=='text':
        return _text({**base,'view':'CAPTURE_TEXT','page_image_read':_next('page_image',{'page':node.native_locator.page_number})},node.raw_text,
            offset=offset,max_chars=max_chars,reader='source_part',arguments=arguments)
    if node.table_payload is None:raise ValueError('This source ref is not a captured table')
    cells=[cell.model_dump(mode='json') for cell in node.table_payload.cells]
    if part=='table':
        entries=_cell_entries(cells,text_field='raw_text',reader='source_part',arguments={'capture_sha256':digest,'ref':ref},part_prefix='cell:',max_chars=max_chars)
        return _window({**base,'view':'CAPTURE_TABLE_CELLS','row_count':node.table_payload.row_count,
            'column_count':node.table_payload.column_count,'offset_unit':'CELLS; row indices are separate source coordinates',
            'map_fragment':{'source_node_id':ref,'row_range':[0,node.table_payload.row_count]}},entries,
            field='cells',offset=offset,limit=limit,max_chars=max_chars,reader='source_part',arguments=arguments)
    match=re.fullmatch(r'cell:(\d+)',part)
    if not match or int(match[1])>=len(cells):raise ValueError('Unknown source part; use a part returned by the reader')
    cell=cells[int(match[1])]
    return _text({**base,'view':'CAPTURE_CELL_TEXT','cell_ref':part,
        **{key:cell[key] for key in ('row','column','row_span','column_span') if key in cell}},str(cell.get('raw_text','')),
        offset=offset,max_chars=max_chars,reader='source_part',arguments=arguments)


def _scope_requests(map_dir,source_sha256,expected_map_sha256):
    directory=Path(map_dir)
    mapping=core.DocumentMapArtifactV1.model_validate(_read(directory/'document-map.json'))
    if mapping.source_sha256!=source_sha256:raise ValueError('Map belongs to another task original')
    if expected_map_sha256 is not None and mapping.artifact_sha256!=expected_map_sha256:raise ValueError('Map changed; reload its scope references')
    raw=_read(directory/'scope-requests.json');requests=[]
    for kind,name,cls in [('TEXT','text_requests',core.SourceItemTextScopeRequestV1),('TABLE','table_requests',core.SourceRequirementScopeRequestV1)]:
        for value in raw[name]:
            request=cls.model_validate(value)
            if (request.source_sha256,request.document_map_artifact_id,request.document_map_artifact_sha256)!=(mapping.source_sha256,mapping.artifact_id,mapping.artifact_sha256):
                raise ValueError('Scope request belongs to another source or Map')
            requests.append((kind,request))
    return mapping,requests


def scope_read(map_dir,*,expected_source_sha256,scope_id=None,expected_map_sha256=None,part=None,offset=0,limit=20,max_chars=12000):
    _limits(offset,limit,max_chars)
    mapping,requests=_scope_requests(map_dir,expected_source_sha256,expected_map_sha256)
    base={'schema_version':'reqpilot-task-reading/v1','source_sha256':mapping.source_sha256,
          'map_sha256':mapping.artifact_sha256,'semantic_authority':'NONE','authoring_part':'template'}
    arguments={'scope_id':scope_id,'expected_map_sha256':mapping.artifact_sha256,'part':part}
    if scope_id is None:
        if part is not None:raise ValueError('A scope part requires its explicit scope_id')
        entries=[]
        for kind,request in requests:
            identifier=request.scope_id if kind=='TEXT' else request.logical_table_scope_id
            pages=sorted({value.page for value in request.source_content if value.page is not None}|
                ({value.page for value in request.fragments if value.page is not None} if kind=='TABLE' else set()))
            entries.append({'scope_id':identifier,'scope_kind':kind,'scope_ref':request.request_sha256,
                'node_id':request.node_id,'node_label':request.node_label,'title_preview':request.node_title[:160],
                **({'logical_table_role':request.logical_table_role,'logical_table_title':request.logical_table_title} if kind=='TABLE' else {}),
                'title_complete':len(request.node_title)<=160,'pages_preview':pages[:12],'page_count':len(pages),
                'content_count':len(request.source_content),'fragment_count':len(request.fragments) if kind=='TABLE' else 0,
                'read':_next('scope_read',{'scope_id':identifier,'expected_map_sha256':mapping.artifact_sha256})})
        return _window({**base,'view':'SCOPE_INDEX'},entries,field='scopes',offset=offset,limit=limit,max_chars=max_chars,
            reader='scope_read',arguments=arguments)
    selected=next(((kind,request) for kind,request in requests if (request.scope_id if kind=='TEXT' else request.logical_table_scope_id)==scope_id),None)
    if selected is None:raise ValueError('Unknown scope_id for this Map')
    kind,request=selected
    base.update(scope_id=scope_id,scope_kind=kind,scope_ref=request.request_sha256,node_id=request.node_id,
        structural_role=request.structural_role,ref_namespace='CONTENT_REFS_ARE_LOCAL_TO_THIS_SCOPE; not capture refs')
    if kind=='TABLE':base.update(logical_table_role=request.logical_table_role,logical_table_title=request.logical_table_title)
    if part=='template':
        from task_scope_templates import scope_template
        data=scope_template(kind,request)
        whole={**base,'view':'SCOPE_AUTHORING_TEMPLATE','data':data,'complete':True,'next_read':None,'budget_scope':'RESULT_JSON_CHARACTERS'}
        if offset==0 and _size(whole)<=max_chars:return whole
        return _text({**base,'view':'SCOPE_AUTHORING_TEMPLATE','encoding':'JSON_TEXT'},
            json.dumps(data,ensure_ascii=False,separators=(',',':')),offset=offset,max_chars=max_chars,reader='scope_read',arguments=arguments)
    if part is None:
        from task_scope_templates import scope_template
        template=scope_template(kind,request)
        # Common short scopes carry their blank form beside the actual source.
        # Larger scopes retain the explicit template part and reading budget.
        if _size(template)<=max_chars//3:base['authoring']=template
        entries=[];preview_limit=min(512,max_chars//6)
        for index,content in enumerate(request.source_content):
            item={'kind':'TEXT_CONTENT','content_ref':index,'content_id':content.content_id,'is_title':content.is_title,
                'page':content.page,'bbox':content.bbox,'text':content.text[:preview_limit],
                'text_total_characters':len(content.text),'text_complete':len(content.text)<=preview_limit}
            if len(content.text)>preview_limit:item['next_text_read']=_next('scope_read',{**arguments,'part':'content:'+str(index),'offset':preview_limit,'max_chars':max_chars})
            entries.append(item)
        if kind=='TABLE':
            for index,fragment in enumerate(request.fragments):
                entries.append({'kind':'TABLE_FRAGMENT','fragment_id':fragment.fragment_id,'page':fragment.page,
                    'fragment_ref':index,
                    'row_count':fragment.row_count,'column_count':fragment.column_count,'source_row_range':fragment.row_range,
                    'cell_count':len(fragment.cells),'row_coordinates':'LOCAL_TO_COMPILED_FRAGMENT; use 0-based half-open row_groups',
                    'read':_next('scope_read',{**arguments,'part':'table:'+str(index)})})
        return _window({**base,'view':'SCOPE_CONTENT_INDEX'},entries,field='entries',offset=offset,limit=limit,max_chars=max_chars,
            reader='scope_read',arguments=arguments)
    text_match=re.fullmatch(r'content:(\d+)',part)
    if text_match:
        index=int(text_match[1])
        if index>=len(request.source_content):raise ValueError('Unknown scope content part')
        content=request.source_content[index]
        return _text({**base,'view':'SCOPE_TEXT','content_ref':index,'content_id':content.content_id,'is_title':content.is_title,
            'page':content.page,'bbox':content.bbox},content.text,offset=offset,max_chars=max_chars,reader='scope_read',arguments=arguments)
    table_match=re.fullmatch(r'table:(\d+)(?::cell:(\d+))?',part)
    if not table_match or kind!='TABLE' or int(table_match[1])>=len(request.fragments):raise ValueError('Unknown scope part; use a part returned by the reader')
    fragment_index=int(table_match[1]);fragment=request.fragments[fragment_index]
    base.update(fragment_id=fragment.fragment_id,fragment_ref=fragment_index,page=fragment.page,row_count=fragment.row_count,column_count=fragment.column_count,
        source_row_range=fragment.row_range,row_coordinates='LOCAL_TO_COMPILED_FRAGMENT; 0-based half-open')
    if table_match[2] is not None:
        index=int(table_match[2])
        if index>=len(fragment.cells):raise ValueError('Unknown scope cell part')
        cell=fragment.cells[index]
        return _text({**base,'view':'SCOPE_CELL_TEXT','cell_id':cell.cell_id,'row':cell.row,'column':cell.column,
            'row_span':cell.row_span,'column_span':cell.column_span},cell.text,offset=offset,max_chars=max_chars,reader='scope_read',arguments=arguments)
    entries=_cell_entries([cell.model_dump(mode='json') for cell in fragment.cells],text_field='text',reader='scope_read',
        arguments={'scope_id':scope_id,'expected_map_sha256':mapping.artifact_sha256},part_prefix='table:'+str(fragment_index)+':cell:',max_chars=max_chars)
    return _window({**base,'view':'SCOPE_TABLE_CELLS','offset_unit':'CELLS; not business row_groups'},entries,field='cells',
        offset=offset,limit=limit,max_chars=max_chars,reader='scope_read',arguments=arguments)


def _reading_path(directory,manifest,value):
    base=Path(manifest['source_path']).parent
    try:relative=Path(value).relative_to(base)
    except ValueError as exc:raise ValueError('Reading address leaves its bound bundle') from exc
    path=(directory/relative).resolve(strict=True)
    if not path.is_relative_to(directory):raise ValueError('Reading resource escaped its bound bundle')
    return path


def _reading_page(reading_dir,source_sha256,page):
    directory=Path(reading_dir).resolve(strict=True)
    manifest=_read(directory/'reading-workspace.json')
    if manifest.get('schema_version')!='reqpilot-reading-workspace/v1' or manifest.get('source_sha256')!=source_sha256:
        raise ValueError('Reading workspace belongs to another original')
    source=_reading_path(directory,manifest,manifest['source_path'])
    if _hash(source.read_bytes())!=source_sha256:raise ValueError('Reading original changed')
    ir=_reading_path(directory,manifest,manifest['source_ir_path'])
    capture_sha=_hash(ir.read_bytes())
    if capture_sha!=manifest['source_ir_sha256']:raise ValueError('Reading capture changed')
    if type(page) is not int or page<1:raise ValueError('page must be a positive original page number')
    pages=_read(_reading_path(directory,manifest,manifest['pages']))
    selected=next((entry for entry in pages if entry['page']==page),None)
    if selected is None:raise ValueError('Page is outside this original reading workspace')
    return directory,manifest,selected,capture_sha


def page_read(reading_dir,*,expected_source_sha256,page,offset=0,max_chars=12000):
    directory,manifest,selected,capture_sha=_reading_page(reading_dir,expected_source_sha256,page)
    path=_reading_path(directory,manifest,selected['markdown_path'])
    return _text({'schema_version':'reqpilot-task-reading/v1','view':'PAGE_MARKDOWN','source_sha256':expected_source_sha256,
        'capture_sha256':capture_sha,'page':page,'bounds':selected['bounds'],'semantic_authority':'NONE',
        'table_ids':selected['table_ids'],'image_source_ids':selected['image_source_ids'],
        'page_image_read':_next('page_image',{'page':page})},path.read_text(encoding='utf-8'),offset=offset,max_chars=max_chars,
        reader='page_read',arguments={'page':page})


def page_image(reading_dir,*,expected_source_sha256,page):
    directory,manifest,selected,capture_sha=_reading_page(reading_dir,expected_source_sha256,page)
    path=_reading_path(directory,manifest,selected['high_render']['path']);raw=path.read_bytes();digest=_hash(raw)
    if digest!=selected['high_render']['sha256']:raise ValueError('Saved page image changed')
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('Saved page render is not a PNG image')
    width,height=struct.unpack('>II',raw[16:24])
    return {'schema_version':'reqpilot-task-reading-image/v1','path':str(path),'mime_type':'image/png','sha256':digest,
        'source_sha256':expected_source_sha256,'capture_sha256':capture_sha,'page':page,'width':width,'height':height,
        'annotations_rendered':False,'semantic_authority':'NONE'}
