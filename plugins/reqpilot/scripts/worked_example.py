"""Generate a complete invented front-matter/body/BOM example using shared tools.

Run with --project-root when using repository source, or inside a built bundle.
The decisions are an explicit worked answer, not an automatic PDF extractor.
"""
from argparse import ArgumentParser, Namespace
from pathlib import Path
import json
import shutil
import sys


def generate(root, output, source_input=None):
    from reqpilot import load_runtime
    load_runtime(root)
    import artifact_tools as tools
    from app.application.fast_source_capture import capture_fast_source
    import pymupdf
    output.mkdir(parents=True, exist_ok=False)
    source = output / 'example.pdf'
    lines = [(60, 'Demo specification'), (100, 'Document ID: DEMO-01'), (125, 'Revision: A'),
             (200, 'Contents'), (240, '1.1 Opening response ........ 2'),
             (265, '1.2 Closing response ........ 2'), (290, '2 Reference BOM ............. 2')]
    body = [(60, '1.1 Opening response'), (95, 'The valve shall open within 30 ms at 12 V.'),
            (140, '1.2 Closing response'), (175, 'The valve shall close within 40 ms at 12 V.'),
            (220, '2 Reference BOM')]
    if source_input is None:
        pdf = pymupdf.open()
        page = pdf.new_page()
        for y, text in lines:
            page.insert_text((40, y), text, fontsize=11)
        page = pdf.new_page()
        page.insert_text((40,60), '\n'.join(text for _,text in body[:4]), fontsize=11)
        page.insert_text((40,body[4][0]),body[4][1],fontsize=11)
        for x in (40, 100, 280, 440):
            page.draw_line((x, 250), (x, 370))
        for y in (250, 290, 330, 370):
            page.draw_line((40, y), (440, y))
        for x,y,text in [(45,275,'No.'),(105,275,'Component'),(285,275,'Quantity'),
                         (45,315,'1'),(105,315,'Housing'),(285,315,'1'),
                         (45,355,'2'),(105,355,'Seal'),(285,355,'1')]:
            page.insert_text((x,y),text,fontsize=10)
        pdf.save(source); pdf.close()
    else:
        source_input = Path(source_input).expanduser().resolve(strict=True)
        if not source_input.is_file() or source_input.is_symlink():
            raise ValueError('Worked example comparison source must be a regular file')
        shutil.copyfile(source_input, source)
    capture_fast_source(source, output_dir=output/'capture')
    ir = json.loads((output/'capture/source_ir.json').read_text())
    by_text = {n['raw_text']:n for n in ir['nodes']}
    sid = lambda text: by_text[text]['source_node_id']
    nodes = [{'node_id':'document','kind':'DOCUMENT','parent_id':None,'title':source.name}]
    nodes += [dict(node_id='cover',kind='COVER',parent_id='document',title='Demo specification',
                   title_source_ids=[sid(lines[0][1])],content_source_ids=[sid(t) for _,t in lines[1:3]]),
              dict(node_id='contents',kind='TABLE_OF_CONTENTS',parent_id='document',title='Contents',
                   title_source_ids=[sid(lines[3][1])],content_source_ids=[sid(t) for _,t in lines[4:]])]
    for index, name in [(0,'opening'),(2,'closing')]:
        number,title=body[index][1].split(' ',1)
        block=next(n for n in ir['nodes'] if body[0][1] in n['raw_text'] and body[2][1] in n['raw_text'])
        raw=block['raw_text'];start=raw.index(body[index][1]);title_end=start+len(body[index][1])
        end=raw.index(body[2][1]) if index==0 else len(raw)
        span=lambda lo,hi:dict(source_node_id=block['source_node_id'],start_offset=lo,end_offset=hi)
        nodes.append(dict(node_id=name,kind='SECTION',parent_id='document',title=title,label=number,
            title_source_ranges=[span(start,title_end)],content_source_ranges=[span(title_end,end)]))
    nodes.append(dict(node_id='bom',kind='SECTION',parent_id='document',title='Reference BOM',label='2',
        title_source_ids=[sid(body[4][1])]))
    table=next(n for n in ir['nodes'] if n.get('table_payload'))
    duplicates=[n['source_node_id'] for n in ir['nodes'] if n['kind'] not in {'table','image','figure'}
        and n['native_locator'].get('page_number')==2 and 250<n['native_locator']['bbox'][1]<370]
    decision={'schema_version':'reqpilot-map-decisions/v2','source_sha256':ir['source_sha256'],
        'nodes':nodes,'logical_tables':[dict(scope_id='bom-table',owner_node_id='bom',title='Reference BOM',
        role='REFERENCE_BOM',fragments=[dict(source_node_id=table['source_node_id'])])],
        'source_dispositions':[dict(source_node_id=i,disposition='DUPLICATE_CAPTURE',
        reason='The same visible words are retained in their captured table cells.') for i in duplicates]}
    save=lambda path,value:path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    save(output/'map-decisions.json',decision)
    tools.compile_map(Namespace(source=source,source_ir=output/'capture/source_ir.json',input=output/'map-decisions.json',output_dir=output/'map'))
    requests=json.loads((output/'map/scope-requests.json').read_text())
    mapping=json.loads((output/'map/document-map.json').read_text())
    items={'schema_version':'reqpilot-item-decisions/v1','source_sha256':ir['source_sha256'],
        'document_map_artifact_sha256':mapping['artifact_sha256'],'text_decisions':[],'table_decisions':[]}
    for request in requests['text_requests']:
        rows=[];contexts=[]
        for content in request['source_content']:
            span=dict(content_id=content['content_id'],start_offset=0,end_offset=len(content['text']))
            if request.get('structural_role') or content['is_title']:
                contexts.append(dict(text_span=span,reason_codes=['whole_front_matter' if request.get('structural_role') else 'source_heading']))
            else:
                rows.append(dict(item_key='body',label=content['text'],source_form='NUMBERED_CLAUSE',text_spans=[span],
                    requirementness='REQUIREMENT',requirement_kinds=['QUALITY'],confidence=0.95,
                    decision_summary='A complete original numbered response-time target with its voltage condition.',reason_codes=['source_meaning']))
        items['text_decisions'].append(dict(scope_id=request['scope_id'],items=rows,structure_only_spans=contexts))
    for request in requests['table_requests']:
        fragment=request['fragments'][0]; fid=fragment['fragment_id']
        items['table_decisions'].append(dict(logical_table_scope_id=request['logical_table_scope_id'],
            records=[dict(record_key='whole-bom',label='Reference BOM',source_form='TABLE_BRANCH',row_groups=[dict(fragment_id=fid,row_start=1,row_end=3)],
                requirementness='NON_REQUIREMENT',requirement_kinds=[],non_requirement_role='REFERENCE',confidence=0.95,
                decision_summary='One complete reference composition, retaining both numbered component rows together.',reason_codes=['whole_bom'])],
            context_row_groups=[dict(role='HEADER',row_group=dict(fragment_id=fid,row_start=0,row_end=1),reason_codes=['table_header'])]))
    save(output/'item-decisions.json',items)
    tools.compile_items(Namespace(map_dir=output/'map',input=output/'item-decisions.json',output_dir=output/'baseline',baseline_dir=None,source=None))
    save(output/'baseline/analysis-policy.json', {'source_sha256':ir['source_sha256'],'status':'SKIPPED','reason':'NO_INTERNAL_EVIDENCE'})
    tools.deliver(Namespace(baseline_dir=output/'baseline',output_dir=output/'delivery',source=source,input=None))
    baseline=json.loads((output/'baseline/source-requirement-baseline.json').read_text())
    result={'status':'GENERATED','source_items':len(baseline['records']),
        'requirements':sum(r['requirementness']=='REQUIREMENT' for r in baseline['records']),
        'front_matter_source_items':sum(r['node_id'] in {'cover','contents'} for r in baseline['records']),
        'analysis':'SKIPPED_NO_INTERNAL_EVIDENCE','output_dir':str(output.resolve())}
    save(output/'example-receipt.json',result)
    return result


if __name__=='__main__':
    parser=ArgumentParser(description=__doc__)
    parser.add_argument('--project-root',type=Path)
    parser.add_argument('--source',type=Path,help='Reuse one exact synthetic source for release comparison')
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(generate(args.project_root,args.output_dir,args.source),ensure_ascii=False))
