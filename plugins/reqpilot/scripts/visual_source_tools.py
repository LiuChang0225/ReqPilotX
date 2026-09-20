"""Project a source-bound visual sidecar through the existing Map/Items path."""
import hashlib
import json


def _progress(output, value):
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=output,delete=False) as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.write('\n')
        path=stream.name
    os.replace(path,output/'reading-progress.json')


def _save_projection(base, snapshots, output, source_path, input_sha256=None):
    from artifact_tools import save
    from app.application.project_visual_source import project_visual_source
    updated = project_visual_source(base, snapshots)
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != base.source_sha256:
        raise ValueError('Original changed during visual reading')
    save(output, 'source_ir.json', updated.model_dump(mode='json'))
    save(output, 'visual-snapshots.json', [s.model_dump(mode='json') for s in snapshots])
    snapshot_ids={s.visual_text_snapshot_id for s in snapshots}
    refs=[{'ref':i,'source_node_id':n.source_node_id,'page':n.native_locator.page_number,
           'bbox':n.native_locator.bbox,'kind':n.kind.value,
           'observation_id':n.visual_text_ref.observation_id,
           **({'row_count':n.table_payload.row_count,'column_count':n.table_payload.column_count} if n.table_payload else {})}
          for i,n in enumerate(updated.nodes) if n.visual_text_ref and n.visual_text_ref.snapshot_id in snapshot_ids]
    result={'schema_version':'reqpilot-visual-source-capture/v1','status':'CAPTURED',
            'source_sha256':base.source_sha256,'source_ir':str(output/'source_ir.json'),
            'source_ir_sha256':hashlib.sha256((output/'source_ir.json').read_bytes()).hexdigest(),
            'input_sha256':input_sha256,'appended_node_count':len(updated.nodes)-len(base.nodes),
            'refs':refs,'source_and_prior_capture_unchanged':True,'semantic_authority':'NONE',
            'next':'Inspect original images and returned authoring refs. Compile a bounded Map with this source_ir, then use the ordinary item compiler. Alternative captures do not decide business ownership.'}
    return result


def read_pdf_visual(args):
    import os
    import time
    from artifact_tools import required,new_output,save
    from app.domain.extraction.source_ir import SourceIRSnapshot
    from app.adapters.documents.pdf_source_ir_adapter import render_pdf_visual_page_input
    from app.adapters.documents.tesseract_reading import read_page
    source=required(args.source,'--source');ir_path=required(args.source_ir,'--source-ir')
    base=SourceIRSnapshot.model_validate_json(ir_path.read_bytes())
    if hashlib.sha256(source.read_bytes()).hexdigest()!=base.source_sha256:
        raise ValueError('Visual reading requires the same original')
    config=json.loads(args.input.read_text()) if args.input else {}
    allowed={'profile','pages','tessdata_dir','table_regions'}
    if not isinstance(config,dict) or set(config)-allowed:
        raise ValueError('Visual reading config supports profile, tessdata_dir and table_regions')
    pages=args.page or config.get('pages')
    if not pages or len(set(pages))!=len(pages) or any(type(p) is not int or p<1 for p in pages):
        raise ValueError('Visual reading requires unique positive original page numbers')
    profile=config.get('profile','page');regions=config.get('table_regions',[])
    if not isinstance(regions,list):raise ValueError('table_regions must be an array')
    resolved=[]
    for region in regions:
        if not isinstance(region,dict) or set(region)-{'page','bbox','source_ref','header_row_count'}:
            raise ValueError('A table region requires page/bbox or an existing table source_ref')
        region=dict(region)
        if 'source_ref' in region:
            ref=region.pop('source_ref')
            candidates=[n for i,n in enumerate(base.nodes) if (type(ref) is int and i==ref) or n.source_node_id==ref]
            if len(candidates)!=1 or candidates[0].table_payload is None:
                raise ValueError('table source_ref must identify one actual captured table')
            node=candidates[0]
            if 'page' in region or 'bbox' in region:raise ValueError('Use source_ref or page/bbox, not both')
            region.update(page=node.native_locator.page_number,bbox=list(node.native_locator.bbox))
            region['expand_existing_table']=True
        if region.get('page') not in pages:raise ValueError('Table region must belong to selected pages')
        resolved.append(region)
    output=new_output(args.output_dir);snapshots=[];receipts=[]
    for page in sorted(pages):
        start=time.perf_counter()
        frame=render_pdf_visual_page_input(source,page_number=page,scale=300/72)
        page_regions=[]
        for region in resolved:
            if region['page']!=page:continue
            box=list(region['bbox'])
            if region.get('expand_existing_table'):
                box=[max(frame.page_bbox[0],box[0]-12),max(frame.page_bbox[1],box[1]-12),
                     min(frame.page_bbox[2],box[2]+12),min(frame.page_bbox[3],box[3]+12)]
            page_regions.append({'bbox':box,'header_row_count':region.get('header_row_count',0)})
        snapshot,audit=read_page(frame,profile=profile,
            tessdata_dir=config.get('tessdata_dir') or os.environ.get('REQPILOT_TESSDATA_DIR'),table_regions=page_regions)
        snapshots.append(snapshot)
        (output/f'page-{page:04d}.png').write_bytes(frame.page_image_bytes)
        save(output,f'page-{page:04d}.ocr-audit.json',audit)
        save(output,f'page-{page:04d}.visual-text.json',snapshot.model_dump(mode='json'))
        (output/f'page-{page:04d}.md').write_text('\n'.join(o.raw_text for o in snapshot.observations if o.raw_text)+'\n',encoding='utf-8')
        receipts.append({'page':page,'elapsed_s':round(time.perf_counter()-start,3),
                         'image_sha256':frame.page_image_sha256,'observations':len(snapshot.observations),
                         'diagnostics':audit['diagnostics']})
        _progress(output,{'pages':receipts,'requested_pages':pages,'complete':False})
    result=_save_projection(base,tuple(snapshots),output,source,
                            hashlib.sha256(args.input.read_bytes()).hexdigest() if args.input else None)
    result['pages']=receipts
    ref_by_observation={entry['observation_id']:entry['ref'] for entry in result['refs']}
    result['numeric_review_leads']=[]
    for snapshot in snapshots:
        audit=json.loads((output/f'page-{snapshot.page_number:04d}.ocr-audit.json').read_text())
        observations={o.observation_index:o for o in snapshot.observations}
        for lead in audit.get('numeric_rechecks',[]):
            if lead['disagrees']:
                observation=observations[lead['observation_index']]
                result['numeric_review_leads'].append({'page':snapshot.page_number,
                    'ref':ref_by_observation[observation.observation_id],
                    'original':lead['original'],'candidate':lead['candidate'],'bbox':lead['bbox'],
                    'applied':False,'next':'Compare the original crop; revise-visual-text records only supported corrections.'})
    if any(p['diagnostics'] for p in receipts):result['status']='PARTIAL'
    result['base_source_ir_sha256']=hashlib.sha256(ir_path.read_bytes()).hexdigest()
    save(output,'visual-source-capture.json',result)
    _progress(output,{'pages':receipts,'requested_pages':pages,'complete':True})
    return result


def revise_visual_text(args):
    """Record Agent visual corrections separately; never overwrite recognizer output."""
    from artifact_tools import required,new_output,save
    from app.domain.extraction.source_ir import SourceIRSnapshot
    from app.domain.source_visual_text import VisualTextSnapshot,VisualTextObservation,stable_visual_text_observation_id,stable_visual_text_snapshot_id
    source=required(args.source,'--source');ir_path=required(args.source_ir,'--source-ir');input_path=required(args.input,'--input')
    base=SourceIRSnapshot.model_validate_json(ir_path.read_bytes())
    if hashlib.sha256(source.read_bytes()).hexdigest()!=base.source_sha256:raise ValueError('Visual correction original mismatch')
    value=json.loads(input_path.read_text())
    if not isinstance(value,dict) or set(value)!={'changes'} or not isinstance(value['changes'],list) or not value['changes']:
        raise ValueError('Visual corrections require a nonempty changes array')
    groups={};lineage=[];seen=set();snapshot_by_id={s.visual_text_snapshot_id:s for s in base.visual_text_snapshots}
    version='agent1-'+hashlib.sha256(input_path.read_bytes()).hexdigest()[:32]
    for change in value['changes']:
        if (not isinstance(change,dict) or set(change)-{'ref','refs','text','cells','reason'}
            or not isinstance(change.get('reason'),str) or not change['reason'].strip()):
            raise ValueError('Correction requires ref, text or cells, and a source-review reason')
        if ('ref' in change)==('refs' in change):raise ValueError('Use one ref or a list of refs for a same-page transcription group')
        refs=change.get('refs',[change.get('ref')])
        if not isinstance(refs,list) or not refs:raise ValueError('Correction refs must be a nonempty list')
        nodes=[]
        for ref in refs:
            selected=[n for i,n in enumerate(base.nodes) if (type(ref) is int and i==ref) or n.source_node_id==ref]
            if len(selected)!=1 or selected[0].visual_text_ref is None:raise ValueError('Correction ref must identify a visual source node')
            if selected[0].source_node_id in seen:raise ValueError('Duplicate visual correction ref')
            seen.add(selected[0].source_node_id);nodes.append(selected[0])
        node=nodes[0];old=snapshot_by_id[node.visual_text_ref.snapshot_id]
        if any((snapshot_by_id[n.visual_text_ref.snapshot_id].page_image_sha256,
                snapshot_by_id[n.visual_text_ref.snapshot_id].page_number)!=(old.page_image_sha256,old.page_number) for n in nodes):
            raise ValueError('Grouped transcription must stay on one exact source page image')
        if len(nodes)>1 and any(n.table_payload is not None for n in nodes):raise ValueError('Table corrections preserve one existing table')
        observation=next(o for o in old.observations if o.observation_id==node.visual_text_ref.observation_id)
        payload=observation.model_dump(mode='json')
        if len(nodes)>1:
            boxes=[n.native_locator.bbox for n in nodes]
            payload['bbox']=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
            payload.pop('geometry_receipt',None)
        if observation.layout_table is not None:
            if 'text' in change or not isinstance(change.get('cells'),list) or not change['cells']:raise ValueError('Table correction needs cells, retaining topology and geometry')
            keys=set()
            for edit in change['cells']:
                if (set(edit)!={'row','column','text'} or not isinstance(edit['text'],str)
                    or type(edit['row']) is not int or type(edit['column']) is not int):raise ValueError('Cell correction needs integer row/column and text')
                key=(edit['row'],edit['column'])
                cells=[c for c in payload['layout_table']['cells'] if (c['row'],c['column'])==key]
                if len(cells)!=1 or key in keys:raise ValueError('Cell correction must address one unique existing anchor')
                keys.add(key);cells[0]['raw_text']=edit['text']
        else:
            if 'cells' in change or not isinstance(change.get('text'),str) or not change['text'].strip():raise ValueError('Text correction needs nonempty text')
            payload['raw_text']=change['text']
        key=(old.page_number,old.page_image_sha256,old.page_bbox)
        entries=groups.setdefault(key,{'snapshot':old,'observations':[]})['observations']
        payload.update(provider_id='agent-visual-transcription',provider_version=version,observation_index=len(entries),confidence=None)
        common={k:payload[k] for k in ('source_sha256','page_image_sha256','provider_id','provider_version','page_number','observation_index','bbox')}
        common['bbox']=tuple(common['bbox'])
        payload['observation_id']=stable_visual_text_observation_id(**common)
        entries.append(VisualTextObservation.model_validate(payload))
        lineage.append({'previous_source_node_id':node.source_node_id,'previous_observation_id':observation.observation_id,
                        'previous_source_node_ids':[n.source_node_id for n in nodes],
                        'observation_id':payload['observation_id'],'reason':change['reason']})
    snapshots=[]
    for group in groups.values():
        value=group['snapshot'].model_dump(mode='json');value.update(provider_id='agent-visual-transcription',provider_version=version,
            observations=[o.model_dump(mode='json') for o in group['observations']],
            evaluated_content_types=[k for k in ('TEXT','TABLE','FIGURE','HEADER','FOOTER','WATERMARK') if any(o.content_type==k for o in group['observations'])])
        identity={k:value[k] for k in ('source_sha256','page_image_sha256','provider_id','provider_version','page_number','page_bbox','page_image_width_px','page_image_height_px')}
        identity['page_bbox']=tuple(identity['page_bbox'])
        value['visual_text_snapshot_id']=stable_visual_text_snapshot_id(**identity);snapshots.append(VisualTextSnapshot.model_validate(value))
    output=new_output(args.output_dir)
    result=_save_projection(base,tuple(snapshots),output,source,hashlib.sha256(input_path.read_bytes()).hexdigest())
    result.update(correction_origin='AGENT_VISUAL_REVIEW',lineage=lineage,raw_recognizer_observations_preserved=True)
    result['base_source_ir_sha256']=hashlib.sha256(ir_path.read_bytes()).hexdigest()
    save(output,'visual-correction.json',result)
    save(output,'visual-source-capture.json',result)
    return result


def capture_visual_source(args):
    from artifact_tools import required, new_output, save
    from app.domain.extraction.source_ir import SourceIRSnapshot
    from app.domain.source_visual_text import VisualTextSnapshot
    from app.application.project_visual_source import project_visual_source

    source = required(args.source, '--source')
    ir_path = required(args.source_ir, '--source-ir')
    input_path = required(args.input, '--input')
    base = SourceIRSnapshot.model_validate_json(ir_path.read_bytes())
    if hashlib.sha256(source.read_bytes()).hexdigest() != base.source_sha256:
        raise ValueError('Visual source capture requires the same original bytes')
    value = json.loads(input_path.read_text())
    snapshots = tuple(VisualTextSnapshot.model_validate(v) for v in (value if isinstance(value, list) else [value]))
    pages = set(args.page or [])
    if pages:
        if pages - {s.page_number for s in snapshots}:
            raise ValueError('Requested page is absent from visual candidates')
        snapshots = tuple(s for s in snapshots if s.page_number in pages)
    requested = frozenset(args.source_id) if args.source_id else None
    updated = project_visual_source(base, snapshots, observation_ids=requested)
    output = new_output(args.output_dir)
    save(output, 'source_ir.json', updated.model_dump(mode='json'))
    keys = {(s.visual_text_snapshot_id,o.observation_id) for s in snapshots for o in s.observations
            if requested is None or o.observation_id in requested}
    refs = [{'ref':i,'source_node_id':n.source_node_id,'page':n.native_locator.page_number,
             'bbox':n.native_locator.bbox,'kind':n.kind.value,
             'observation_id':n.visual_text_ref.observation_id}
            for i,n in enumerate(updated.nodes) if n.visual_text_ref and
            (n.visual_text_ref.snapshot_id,n.visual_text_ref.observation_id) in keys]
    result = {'schema_version':'reqpilot-visual-source-capture/v1', 'status':'CAPTURED',
              'source_sha256':base.source_sha256, 'base_source_ir_sha256':hashlib.sha256(ir_path.read_bytes()).hexdigest(),
              'source_ir':str(output/'source_ir.json'),
              'source_ir_sha256':hashlib.sha256((output/'source_ir.json').read_bytes()).hexdigest(),
              'input_sha256':hashlib.sha256(input_path.read_bytes()).hexdigest(),
              'appended_node_count':len(updated.nodes)-len(base.nodes), 'refs':refs,
              'source_and_prior_capture_unchanged':True, 'semantic_authority':'NONE',
              'next':'Read the selected source refs and original pixels. Use this capture in a bounded Map edit and ordinary item compilation. OCR and Agent transcription remain visual observations; do not infer classification from their origin.'}
    save(output,'visual-source-capture.json',result)
    return result
