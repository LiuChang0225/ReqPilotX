"""Real stdio interoperability, including legacy startup and process restart."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT=Path(os.environ.get('REQPILOT_TEST_PROJECT_ROOT', Path(__file__).resolve().parents[3]))
SCRIPTS=Path(os.environ.get('REQPILOT_TEST_PLUGIN_SCRIPTS', ROOT/'plugins/reqpilot/scripts'))
RUNTIME=Path(os.environ.get('REQPILOT_TEST_RUNTIME', ROOT/'prototype'))
sys.path[:0]=[str(SCRIPTS),str(RUNTIME)]
HAS_MCP=importlib.util.find_spec('mcp') is not None


@unittest.skipUnless(HAS_MCP, 'Install the optional requirements-mcp.txt to run actual stdio tests')
class McpTaskAdapterTests(unittest.TestCase):
    def test_region_pixels_reuse_native_geometry_without_rebuilding_all_page_views(self):
        import base64,hashlib
        import pymupdf
        from mcp import Client, StdioServerParameters
        from app.adapters.documents.pdf_source_crop_renderer import render_pdf_source_crop
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source.pdf';doc=pymupdf.open()
            page=doc.new_page(width=300,height=400)
            page.draw_rect(pymupdf.Rect(40,70,180,130),color=(0,0,0),fill=(0,1,0))
            page.add_rect_annot(pymupdf.Rect(35,65,185,135));page.set_rotation(90)
            doc.save(source);doc.close();sha=hashlib.sha256(source.read_bytes()).hexdigest()
            params=StdioServerParameters(command=sys.executable,args=[str(SCRIPTS/'mcp_server.py'),
                '--project-root',str(ROOT),'--working-root',str(root)])
            async def exercise():
                async with Client(params) as client:
                    async def call(name,request):
                        reply=await client.call_tool(name,request);self.assertFalse(reply.is_error,reply)
                        return reply
                    state=(await call('task_open',{'mode':'create','source':str(source),'goal':'Inspect original detail','creation_key':'crop'})).structured_content
                    task=state['task_id']
                    state=(await call('source_prepare',{'task_id':task,'expected_revision':state['revision']})).structured_content
                    before_revision=state['revision'];before_capture=Path(state['selected']['READING']['path']).read_bytes()
                    args={'task_id':task,'view':'image','page':1,'bbox':[260,30,340,190],'scale':3}
                    reply=await call('source_read',args);value=reply.structured_content;meta=value['image']
                    pixels=base64.b64decode(next(c.data for c in reply.content if c.type=='image'))
                    expected=render_pdf_source_crop(source,page_number=1,bbox=tuple(args['bbox']),scale=3,expected_source_sha256=sha,annotations=False)
                    self.assertEqual(pixels,expected)
                    self.assertEqual(meta['sha256'],hashlib.sha256(pixels).hexdigest())
                    self.assertEqual((meta['width'],meta['height']),(240,480))
                    self.assertEqual(meta['rotation_cw_degrees'],90)
                    self.assertEqual(value['revision'],before_revision)
                    self.assertNotIn('reading_preparation',value)
                    self.assertEqual(Path(state['selected']['READING']['path']).read_bytes(),before_capture)
                    repeat=(await call('source_read',args)).structured_content
                    self.assertEqual(repeat['image']['path'],meta['path'])
                    for invalid in ({'bbox':[0,0,450,500]},{'scale':5},{'view':'source'},{'bbox':[0,0,True,10]}):
                        failure=await client.call_tool('source_read',{**args,**invalid})
                        self.assertTrue(failure.is_error,failure)
                    current=(await call('task_inspect',{'task_id':task})).structured_content
                    self.assertEqual(current['revision'],before_revision)
                    self.assertFalse(list(Path(state['task_root']).glob('reading/*/reading-workspace.json')))
            asyncio.run(exercise())

    def test_visual_revision_is_callable_through_real_stdio_and_fixed_cli(self):
        import shutil
        if not shutil.which('tesseract'):self.skipTest('local OCR binary required')
        from mcp import Client, StdioServerParameters
        from task_service import REQUESTS
        import pymupdf
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source.pdf';doc=pymupdf.open()
            doc.new_page().insert_text((40,80),'Temperature shall be 150 C.',fontsize=18)
            doc.save(source);doc.close();original=source.read_bytes()
            params=StdioServerParameters(command=sys.executable,args=[str(SCRIPTS/'mcp_server.py'),
                '--project-root',str(ROOT),'--working-root',str(root)])
            async def exercise():
                async with Client(params) as client:
                    listing=await client.list_tools()
                    self.assertEqual(len(listing.tools),len(REQUESTS))
                    self.assertIn('source_transcription_revise',{t.name for t in listing.tools})
                    async def call(name,request):
                        reply=await client.call_tool(name,request)
                        self.assertFalse(reply.is_error,reply)
                        return reply.structured_content
                    state=await call('task_open',{'mode':'create','source':str(source),'goal':'Read original','creation_key':'stdio-visual'})
                    task=state['task_id']
                    state=await call('source_prepare',{'task_id':task,'expected_revision':state['revision'],'profile':'page'})
                    before=Path(state['selected']['READING']['path']).read_bytes()
                    state=await call('source_prepare',{'task_id':task,'expected_revision':state['revision'],'mode':'ocr','pages':'[1]'})
                    captured=json.loads(Path(state['selected']['READING']['path']).read_text())
                    ref=next(i for i,n in enumerate(captured['nodes']) if n.get('visual_text_ref') and '150' in n['raw_text'])
                    request={'task_id':task,'expected_revision':state['revision'],'command_id':'visual-correction',
                        'changes':[{'ref':ref,'text':'Temperature shall be 150 C.','reason':'Source image review for transport fixture.'}]}
                    updated=await call('source_transcription_revise',request)
                    self.assertEqual(updated['operation_state'],'COMMITTED',updated)
                    replay=await call('source_transcription_revise',request)
                    self.assertTrue(replay['replayed'])
                    current=json.loads(Path(updated['selected']['READING']['path']).read_text())
                    self.assertEqual(current['nodes'][:len(captured['nodes'])],captured['nodes'])
                    self.assertGreater(len(current['nodes']),len(captured['nodes']))
                    self.assertEqual(source.read_bytes(),original)
                    self.assertEqual(json.loads(before)['source_sha256'],current['source_sha256'])
            asyncio.run(exercise())

    def test_typed_cli_and_stdio_mcp_produce_equal_business_artifacts(self):
        from mcp import Client, StdioServerParameters
        from task_bridge import TaskBridge
        from portable_fixtures import make_fixture,item_decisions
        import base64,hashlib
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source,_,mapping=make_fixture(root/'fixture',table_image=True)
            bridge=TaskBridge(root,SCRIPTS/'reqpilot.py',project_root=ROOT)
            params=StdioServerParameters(command=sys.executable,args=[str(SCRIPTS/'mcp_server.py'),
                '--project-root',str(ROOT),'--working-root',str(root)])
            names={'open':'task_open','inspect':'task_inspect','prepare':'source_prepare','read':'source_read',
                   'map':'map_apply','items':'items_apply','review':'review_prepare','deliver':'task_deliver',
                   'annotations':'annotations_apply','decomposition':'decomposition_apply','revise':'review_apply'}
            async def complete(call,key):
                state=await call('open',{'mode':'create','source':str(source),'goal':'忠实整理同一合成原件。','creation_key':key})
                task=state['task_id']
                async def write(action,**extra):
                    nonlocal state
                    state=await call(action,{'task_id':task,'expected_revision':state['revision'],**extra})
                    self.assertEqual(state['operation_state'],'COMMITTED',state)
                    return state
                await write('prepare',mode='capture')
                page=await call('read',{'task_id':task,'view':'page','page':1,'max_chars':2048})
                self.assertEqual(page['status'],'READ',page)
                self.assertEqual(page['reading_preparation']['status'],'PREPARED')
                state['revision']=page['revision']
                source_view=await call('read',{'task_id':task,'limit':100,'max_chars':50000})
                self.assertEqual(source_view['read_budget']['applied_max_chars'],24000)
                table_ref=next(n for n in source_view['nodes'] if n.get('table'))
                cells=await call('read',table_ref['read']['request'])
                self.assertEqual(cells['source_binding']['binding'],'CAPTURE_BOUND_HANDLE')
                self.assertTrue(cells['cells'])
                # The public continuation must be a ready-to-call tool request.
                continuation=page.get('next_read') or page.get('continuation')
                if continuation:
                    self.assertEqual(continuation['tool'],'source_read')
                    self.assertEqual((await call('read',continuation['request']))['status'],'READ')
                await call('read',{'task_id':task,'view':'image','page':1})
                edit={k:v for k,v in mapping.items() if k!='source_sha256'};edit['schema_version']='reqpilot-map-edit/v1'
                await write('map',decisions=edit)
                scopes=await call('read',state['scope_read']['request'])
                table_scope=next(row for row in scopes['scopes'] if row['scope_kind']=='TABLE')
                self.assertTrue(table_scope['logical_table_role'])
                template=await call('read',{'task_id':task,'view':'scope','scope_id':table_scope['scope_id'],'part':'template'})
                self.assertTrue(template['data']['template_only'])
                decisions=item_decisions(Path(state['selected']['MAP']['output_dir']))
                for choice in decisions['table_decisions']:
                    for record in choice['records']:
                        for group in record['row_groups']:
                            group.pop('fragment_id');group['fragment_ref']=0
                    for context in choice['context_row_groups']:
                        context['row_group'].pop('fragment_id');context['row_group']['fragment_ref']=0
                for k in ('schema_version','source_sha256','document_map_artifact_sha256'):decisions.pop(k,None)
                # Initial authoring supplies source judgments, not local machine IDs.
                for scope in decisions['text_decisions']:
                    for item in scope['items']:
                        item.pop('item_key')
                        item['reason_codes']=[code.replace('_','-') for code in item['reason_codes']]
                for scope in decisions['table_decisions']:
                    for item in scope['records']:
                        item.pop('record_key')
                        item['reason_codes']=[code.replace('_','-') for code in item['reason_codes']]
                decisions['retain_map_structure']=True
                decisions['text_decisions']=[{k:v for k,v in scope.items() if k!='structure_only_spans'} for scope in decisions['text_decisions'] if scope['items']]
                await write('items',decisions=decisions)
                roster=await call('read',state['source_items_read']['request'])
                parent=roster['items'][0]
                packet=await call('read',parent['read']['request'])
                table=next(row for row in roster['items'] if row['source_form']=='TABLE_RECORD')
                table_packet=await call('read',table['read']['request'])
                embedded=next(pic for pic in table_packet['images'] if pic['relation']=='TABLE_EMBEDDED_IMAGE')
                await call('read',embedded['read']['request'])
                await write('review')
                candidates=await call('read',packet['context_review']['read_candidates']['request'])
                self.assertEqual(candidates['status'],'READ')
                note=next(row for row in roster['items'] if row['requirementness']=='NON_REQUIREMENT')
                await write('annotations',changes={'annotation_updates':[{'source_item_id':parent['source_display_id'],
                    'context_source_ids':[note['source_display_id']], 'reason':'Synthetic same-source link for transport verification.'}]})
                with_context=await call('read',{'task_id':task,'view':'item','source_item_id':parent['source_display_id']})
                linked=await call('read',with_context['context_review']['read_linked_context']['request'])
                self.assertEqual(linked['data'][0]['source_item_id'],note['source_record_id'])
                await write('annotations',changes={'visual_readings':[{'source_item_id':table['source_display_id'],
                    'image_anchor_ids':[embedded['anchor_id']], 'text':'The drawing belongs to the response test row.',
                    'association_reason':'Original image lies inside this table row.'}]})
                await write('decomposition',changes={'authorization':{'request':'Split independently testable limits in this fixture.',
                    'source_item_ids':[parent['source_display_id']]},'decisions':[{'source_item_id':parent['source_display_id'],
                    'action':'SPLIT','reason':'Opening and closing have independent limits.',
                    'children':[{'text':line,'derivation_type':'VERBATIM_SPAN','source_ranges':[{'segment_index':0,'quote':line}]}
                        for line in packet['source'][0]['text'].splitlines()]}]})
                latest=await call('read',{'task_id':task,'view':'item','source_item_id':parent['source_display_id']})
                self.assertEqual(len(latest['decomposition']['children']),2)
                latest_table=await call('read',{'task_id':task,'view':'item','source_item_id':table['source_display_id']})
                self.assertEqual(latest_table['identity']['source_form'],'TABLE_RECORD')
                self.assertEqual(latest_table['annotations']['visual_interpretation']['text'],'The drawing belongs to the response test row.')
                await write('revise',changes={'model_id':'deterministic-fixture-not-model','decisions':[{
                    'source_record_id':parent['source_display_id'],'requirementness':'REQUIREMENT',
                    'requirement_kinds':['QUALITY','FUNCTIONAL'],'confidence':0.9,
                    'decision_summary':'Both operating action and measured limits occur in the source.',
                    'reason_codes':['source_meaning']}]})
                self.assertEqual(state['pending_decomposition']['remaining_count'],1)
                affected=await call('read',state['pending_decomposition']['items'][0]['read']['request'])
                self.assertEqual(affected['source_item_id'],parent['source_record_id'])
                self.assertFalse(state['handoff_ready'])
                await write('decomposition',changes={'authorization':{'source_item_ids':[parent['source_display_id']]},
                    'decisions':[{'source_item_id':parent['source_display_id'],'action':'SPLIT',
                    'reason':'The classification changed; both original independently testable limits still apply.',
                    'children':[{'text':line,'derivation_type':'VERBATIM_SPAN','source_ranges':[{'segment_index':0,'quote':line}]}
                        for line in affected['source'][0]['text'].splitlines()]}]})
                self.assertEqual(state['pending_decomposition']['remaining_count'],0)
                restored=await call('read',{'task_id':task,'view':'item','source_item_id':parent['source_display_id']})
                self.assertEqual([c['requirement_display_id'] for c in restored['decomposition']['children']],
                    [parent['requirement_display_id']+'-S01',parent['requirement_display_id']+'-S02'])
                result=await write('deliver',command_id='final-export')
                self.assertTrue(result['local_delivery_ready'],result)
                self.assertFalse(result['handoff_ready'])
                folder=Path(result['result']['output_dir'])
                return json.loads((folder/'matrix-snapshot.json').read_text()),result
            async def exercise():
                async def cli(action,request):return bridge.invoke(action,request)
                control,_=await complete(cli,'typed-cli-control')
                async with Client(params) as client:
                    async def mcp(action,request):
                        response=await client.call_tool(names[action],request)
                        self.assertFalse(response.is_error,response)
                        if action=='read' and request.get('view') in {'image','item_image'}:
                            images=[c for c in response.content if c.type=='image'];self.assertEqual(len(images),1)
                            meta=response.structured_content['image']
                            self.assertEqual(hashlib.sha256(base64.b64decode(images[0].data)).hexdigest(),meta['sha256'])
                        return response.structured_content
                    candidate,_=await complete(mcp,'mcp-candidate')
                for key in ('root_node_ids','nodes','logical_tables','source_item_baseline'):
                    self.assertEqual(control[key],candidate[key],key)
            asyncio.run(exercise())

    def test_actual_stdio_discovery_create_capture_replay_and_restart(self):
        from mcp import Client, StdioServerParameters
        from portable_fixtures import make_fixture
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source,_,mapping=make_fixture(root/'fixture')
            params=StdioServerParameters(command=sys.executable,args=[str(SCRIPTS/'mcp_server.py'),
                '--project-root',str(ROOT),'--working-root',str(root)])
            async def exercise():
                async with Client(params,mode='auto') as client:
                    listing=await client.list_tools()
                    tools={t.name:t for t in listing.tools}
                    self.assertTrue({'task_describe','task_open','source_prepare','map_apply','items_apply','task_deliver','task_recover'} <= set(tools))
                    description=await client.call_tool('task_describe',{'action':'items','part':'fields'})
                    self.assertFalse(description.is_error,description)
                    self.assertEqual(description.structured_content['status'],'DESCRIBED')
                    self.assertNotIn('run',tools)
                    self.assertIn('task_id',tools['source_read'].input_schema['properties'])
                    self.assertNotIn('request',tools['source_read'].input_schema['properties'])
                    self.assertFalse(tools['source_read'].annotations.read_only_hint)
                    self.assertIn('MapNodeEdit',tools['map_apply'].input_schema['$defs'])
                    req={'mode':'create','source':str(source),'goal':'检查合成PDF。','creation_key':'same-create'}
                    created=(await client.call_tool('task_open',{'request':req})).structured_content
                    self.assertEqual(created['status'],'WORKSPACE_INITIALIZED',created)
                    task=created['task_id']
                    repeated=(await client.call_tool('task_open',{'request':req})).structured_content
                    self.assertEqual(repeated['task_id'],task)
                    capture={'task_id':task,'expected_revision':created['revision'],'command_id':'capture-once'}
                    saved=(await client.call_tool('source_prepare',{'request':capture})).structured_content
                    self.assertEqual(saved['operation_state'],'COMMITTED',saved)
                    old_revision=saved['revision']; operation=saved['operation_id']
                # A fresh server and a legacy client both recover the same business task.
                async with Client(params,mode='legacy') as client:
                    state=(await client.call_tool('task_inspect',{'request':{'task_id':task}})).structured_content
                    self.assertEqual(state['revision'],old_revision)
                    replay=(await client.call_tool('source_prepare',{'request':capture})).structured_content
                    self.assertTrue(replay['replayed'],replay)
                    self.assertEqual(replay['operation_id'],operation)
                    bad=await client.call_tool('task_inspect',{'request':{'task_id':task,'guess_latest':True}})
                    self.assertTrue(bad.is_error)
                    nodes=mapping['nodes'];outline=[{'role':'DOCUMENT','ref':nodes[0]['title_source_ids'][0],'children':[
                        {'role':'HEADING','ref':n['title_source_ids'][0],'content':n['content_source_ids']} for n in nodes[1:]]}]
                    table=mapping['logical_tables'][0]
                    outline[0]['children'][-1]['tables']=[{'sources':[{'source':f['source_node_id'],'row_range':f['row_range']} for f in table['fragments']],
                        'title':table['title'],'role':table['role']}]
                    mapped=await client.call_tool('map_apply',{'task_id':task,'expected_revision':old_revision,
                        'decisions':{'outline':outline,'source_dispositions':mapping['source_dispositions']}})
                    self.assertFalse(mapped.is_error,mapped)
                    self.assertEqual(mapped.structured_content['operation_state'],'COMMITTED')
            asyncio.run(exercise())

    def test_actual_image_content_matches_verified_page_bytes(self):
        import base64,hashlib
        from mcp_server import create_server
        from mcp import Client
        from PIL import Image
        from io import BytesIO
        buffer=BytesIO();Image.new('RGB',(13,11),(83,117,151)).save(buffer,format='PNG');data=buffer.getvalue()
        class Bridge:
            def invoke(self,action,request):
                return {'status':'READ','_images':[{'path':'test-only','sha256':hashlib.sha256(data).hexdigest(),'mime_type':'image/png'}]}
            def image_bytes(self,picture):return data
        async def exercise():
            async with Client(create_server(Bridge())) as client:
                result=await client.call_tool('source_read',{'request':{'task_id':'task-'+'0'*32,'view':'image','page':1}})
                pictures=[item for item in result.content if item.type=='image']
                self.assertEqual(len(pictures),1)
                self.assertEqual(base64.b64decode(pictures[0].data),data)
                self.assertNotIn('_images',result.structured_content)
        asyncio.run(exercise())


if __name__ == '__main__':unittest.main()
