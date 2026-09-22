"""Append reproducible PDF drawing-text observations; preserve every prior source node.

These are original text-object observations, not proof of visibility, OCR, or
semantic ownership. The Agent must compare the original before choosing them.
"""
import hashlib
from pathlib import Path

from reqpilotx.domain.extraction.source_ir import (
    SourceIRSnapshot, SourceNode, SourceNodeKind, NativeLocator, stable_node_id,
)


def drawing_lines(path, pages):
    import pymupdf
    records = []
    with pymupdf.open(path) as document:
        for page_number in sorted(set(pages)):
            if type(page_number) is not int or not 1 <= page_number <= len(document):
                raise ValueError('PDF text page is outside the original')
            page = document[page_number - 1]
            groups = []
            for index, span in enumerate(page.get_texttrace()):
                if span.get('type') not in {0, 1} or span.get('opacity', 1) <= 0 or not span['chars']:
                    continue
                direction = tuple(span['dir']); chars = span['chars']; runs=[]; begin=0
                normal = lambda char: -direction[1]*char[2][0]+direction[0]*char[2][1]
                for k in range(1,len(chars)):
                    if abs(normal(chars[k])-normal(chars[begin])) >= .1:
                        runs.append((begin,k));begin=k
                runs.append((begin,len(chars)))
                for lo,hi in runs:
                    part=chars[lo:hi];text=''.join(chr(char[0]) for char in part)
                    boxes=[c[3] for c in part]
                    box=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
                    baseline=tuple(part[0][2]);old=groups[-1] if groups else None
                    # Do not concatenate separate lines or distant columns.
                    adjacent=(old and direction==(1.0,0.0) and old['direction']==direction
                        and abs(old['baseline'][1]-baseline[1])<.1 and abs(box[0]-old['unrotated_bbox'][2])<.5)
                    if adjacent:
                        old['spans'].append([index,lo,hi]);old['raw_text']+=text
                        b=old['unrotated_bbox'];old['unrotated_bbox']=[min(b[0],box[0]),min(b[1],box[1]),max(b[2],box[2]),max(b[3],box[3])]
                    else:
                        groups.append({'page':page_number,'spans':[[index,lo,hi]],'raw_text':text,
                            'unrotated_bbox':box,'baseline':baseline,'direction':direction})
            for group in groups:
                if not group['raw_text'].strip():
                    continue
                box = pymupdf.Rect(group['unrotated_bbox']) * page.rotation_matrix
                records.append({'page':page_number,'spans':group['spans'],
                    'raw_text':group['raw_text'],'bbox':[float(x) for x in box],
                    'visibility':'REQUIRES_ORIGINAL_REVIEW'})
    return records


def trace_node(record, source_sha256, order):
    text = record['raw_text']
    locator=NativeLocator(format='pdf',page_number=record['page'],block_index=record['spans'][0][0],bbox=record['bbox'])
    identity=stable_node_id(source_sha256=source_sha256,adapter_id='pdf-texttrace',adapter_version='1',
        kind=SourceNodeKind.PARAGRAPH,native_locator=locator)
    return SourceNode.model_validate({'source_node_id':identity,
        'kind':'paragraph','source_order':order,'raw_text':text,
        'native_locator':locator.model_dump(mode='json'),
        'pdf_text_trace':{'page_number':record['page'],'spans':record['spans'],
            'text_sha256':hashlib.sha256(text.encode()).hexdigest()},
        'pdf_text_lines':([{'start_offset':0,'end_offset':len(text),'bbox':record['bbox']}] if '\n' not in text else [])})


def verify_trace_nodes(snapshot, source_path):
    observed = [n for n in snapshot.nodes if n.pdf_text_trace is not None]
    if not observed:
        return
    if hashlib.sha256(Path(source_path).read_bytes()).hexdigest() != snapshot.source_sha256:
        raise ValueError('Drawing-text observations belong to another original')
    records = {(r['page'],tuple(tuple(s) for s in r['spans'])):r for r in drawing_lines(source_path,[n.pdf_text_trace.page_number for n in observed])}
    for node in observed:
        key = (node.pdf_text_trace.page_number,node.pdf_text_trace.spans)
        if key not in records or trace_node(records[key],snapshot.source_sha256,node.source_order) != node:
            raise ValueError('PDF drawing-text observation no longer matches the exact original text objects')


def verify_capture_extension(previous, snapshot, source_path):
    if (previous.visual_text_snapshots != snapshot.visual_text_snapshots
        or any(n.visual_text_ref is not None for n in snapshot.nodes[len(previous.nodes):])):
        from reqpilotx.application.project_visual_source import verify_visual_capture_extension
        if hashlib.sha256(Path(source_path).read_bytes()).hexdigest() != previous.source_sha256:
            raise ValueError('Visual observations belong to another original')
        verify_visual_capture_extension(previous, snapshot)
        verify_trace_nodes(snapshot, source_path)
        return
    before, after = previous.model_dump(mode='json'), snapshot.model_dump(mode='json')
    n = len(previous.nodes)
    comparable = lambda value:{k:v for k,v in value.items() if k != 'nodes'}
    if (len(snapshot.nodes) <= n or snapshot.nodes[:n] != previous.nodes
        or comparable(before) != comparable(after)
        or any(node.pdf_text_trace is None for node in snapshot.nodes[n:])):
        raise ValueError('Base Map uses a different capture; only verified appended PDF text observations may retain source indexes')
    verify_trace_nodes(snapshot, source_path)


def capture_pdf_text(args):
    from artifact_tools import required, new_output, save
    source = required(args.source, '--source')
    ir_path = required(args.source_ir, '--source-ir')
    snapshot = SourceIRSnapshot.model_validate_json(ir_path.read_text())
    if source.suffix.lower() != '.pdf' or hashlib.sha256(source.read_bytes()).hexdigest() != snapshot.source_sha256:
        raise ValueError('capture-pdf-text requires the same original PDF and SourceIR')
    if not args.page or any(type(p) is not int for p in args.page):
        raise ValueError('capture-pdf-text requires explicit page numbers')
    query = getattr(args, 'query', None)
    if query is not None and (not isinstance(query,str) or not query.strip()):
        raise ValueError('query must be a nonempty substring of original drawing text')
    verify_trace_nodes(snapshot, source)
    records = [r for r in drawing_lines(source,args.page) if query is None or query.casefold() in r['raw_text'].casefold()]
    if not records:
        return {'status':'NO_MATCH','observations':[], 'next':'Try a shorter source substring or another original page. No text was invented and no output was saved.'}
    nodes = list(snapshot.nodes); by_id = {n.source_node_id:i for i,n in enumerate(nodes)}; observations=[]
    order = max((n.source_order for n in nodes),default=-1)
    for record in records:
        candidate = trace_node(record,snapshot.source_sha256,order+1)
        if candidate.source_node_id not in by_id:
            order += 1; by_id[candidate.source_node_id]=len(nodes);nodes.append(candidate)
        index=by_id[candidate.source_node_id]
        observations.append({**record,'ref':index,'source_node_id':candidate.source_node_id,
            'origin':'PDF_TEXT_DRAWING_STREAM'})
    updated = snapshot.model_dump(mode='json')
    updated['nodes']=[n.model_dump(mode='json') for n in nodes]
    augmented=SourceIRSnapshot.model_validate(updated)
    verify_trace_nodes(augmented,source)
    output=new_output(args.output_dir)
    save(output,'source_ir.json',augmented.model_dump(mode='json'))
    receipt={'schema_version':'reqpilot-pdf-text-capture/v1','source_sha256':snapshot.source_sha256,
        'base_source_ir_sha256':hashlib.sha256(ir_path.read_bytes()).hexdigest(),
        'base_node_count':len(snapshot.nodes),'appended_node_count':len(nodes)-len(snapshot.nodes),
        'source_ir_sha256':hashlib.sha256((output/'source_ir.json').read_bytes()).hexdigest(),
        'observations':observations,'visibility':'REQUIRES_ORIGINAL_REVIEW',
        'source_and_prior_capture_unchanged':True,'semantic_authority':'NONE'}
    save(output,'pdf-text-capture.json',receipt)
    return {'status':'CAPTURED','source_ir':str(output/'source_ir.json'),'output_dir':str(output),**receipt,
        'next':'Compare the original crop and drawing text. Use this source_ir and returned refs in a local Map edit; old indexes are preserved. Replace only the previously bound overlapping text ranges, not the whole capture. New observations do not prove visibility or semantic ownership.'}
