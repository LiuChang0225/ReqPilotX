
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import sys
import tempfile
from argparse import Namespace
import unittest

import pymupdf
from app.application.fast_source_capture import capture_fast_source

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'plugins/reqpilot/scripts'))
import artifact_tools


class PluginCaptionRepairTests(unittest.TestCase):
    def test_caption_mismatch_is_locatable_and_repair_preserves_table_and_body_without_extra_src(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);source=root/'source.pdf';pdf=pymupdf.open();page=pdf.new_page()
            page.insert_text((40,40),'4 Verification')
            page.insert_text((40,82),'Table 7 - States')
            for x in [40,230,510]:page.draw_line((x,110),(x,200))
            for y in [110,145,200]:page.draw_line((40,y),(510,y))
            for x,y,text in [(45,130,'State'),(235,130,'Meaning'),(45,170,'OFF'),(235,170,'No output')]:page.insert_text((x,y),text,fontsize=10)
            page.insert_text((40,250),'4.1 The output shall stop when disable is received.')
            pdf.save(source);pdf.close();original=source.read_bytes()
            capture_fast_source(source,output_dir=root/'capture');ir=json.loads((root/'capture/source_ir.json').read_text())
            find=lambda text:next(i for i,n in enumerate(ir['nodes']) if n['raw_text']==text)
            heading,caption,body=find('4 Verification'),find('Table 7 - States'),find('4.1 The output shall stop when disable is received.')
            table=next(i for i,n in enumerate(ir['nodes']) if n.get('table_payload'))
            dispositions=[{'source_node_id':i,'disposition':'DUPLICATE_CAPTURE','reason':'These synthetic words remain in mapped table cells.'} for i,n in enumerate(ir['nodes']) if i not in {heading,caption,body,table}]
            request={'schema_version':'reqpilot-map-edit/v1','nodes':[{'node_id':'doc','parent_id':None,'kind':'DOCUMENT'},
                {'node_id':'verification','parent_id':'doc','kind':'SECTION','heading':{'source':heading,'label':'4'},'content':[caption]},
                {'node_id':'clause','parent_id':'verification','kind':'SECTION','numbered_body':{'source':body,'label':'4.1'}}],
                'logical_tables':[{'scope_id':'states','owner_node_id':'verification','title':'Table 7 - States','role':'REFERENCE','fragments':[{'source_node_id':table}]}],
                'source_dispositions':dispositions}
            def save(name,value):
                path=root/name;path.write_text(json.dumps(value));return path
            def mapping(value,name,base=None):
                artifact_tools.compile_map(Namespace(source=source,source_ir=root/'capture/source_ir.json',input=save(name+'.json',value),map_dir=base,output_dir=root/name))
                return root/name
            def item_input(directory):
                mapping=json.loads((directory/'document-map.json').read_text());requests=json.loads((directory/'scope-requests.json').read_text())
                result={'schema_version':'reqpilot-item-decisions/v1','source_sha256':ir['source_sha256'],'document_map_artifact_sha256':mapping['artifact_sha256'],'text_decisions':[],'table_decisions':[]}
                for req in requests['text_requests']:
                    items=[];structural=[]
                    for content in req['source_content']:
                        span={'content_id':content['content_id'],'start_offset':0,'end_offset':len(content['text'])}
                        if req['node_id']=='verification' or content['is_title']:
                            structural.append({'text_span':span,'reason_codes':['source_label']})
                        else:
                            items.append({'item_key':'body','label':content['text'],'source_form':'NUMBERED_CLAUSE','text_spans':[span],
                                'requirementness':'REQUIREMENT','requirement_kinds':['FUNCTIONAL'],'confidence':.9,'decision_summary':'Original response behavior.','reason_codes':['source_meaning']})
                    result['text_decisions'].append({'scope_id':req['scope_id'],'items':items,'structure_only_spans':structural})
                req=requests['table_requests'][0];fid=req['fragments'][0]['fragment_id']
                result['table_decisions']=[{'logical_table_scope_id':'states','records':[{'record_key':'state','label':'OFF state','source_form':'TABLE_RECORD',
                    'row_groups':[{'fragment_id':fid,'row_start':1,'row_end':2}],'requirementness':'NON_REQUIREMENT','requirement_kinds':[],
                    'non_requirement_role':'DEFINITION','confidence':.9,'decision_summary':'State definition.','reason_codes':['source_meaning']}],
                    'context_row_groups':[{'role':'HEADER','row_group':{'fragment_id':fid,'row_start':0,'row_end':1},'reason_codes':['table_header']}]}]
                return result
            bad=mapping(request,'map-before');before=(bad/'document-map.json').read_bytes()
            with self.assertRaisesRegex(ValueError,'SOURCE_ITEM_STRUCTURE_ONLY_SPAN_NOT_TITLE.*node=verification.*content_id=.*pure table caption'):
                artifact_tools.compile_items(Namespace(map_dir=bad,input=save('items-before.json',item_input(bad)),baseline_dir=None,output_dir=root/'bad-items',source=source))
            self.assertFalse((root/'bad-items').exists())
            patch={'schema_version':'reqpilot-map-edit/v1','nodes':[{'node_id':'verification','content':[],'evidence_refs':[{'source':caption}]}],
                'source_dispositions':dispositions+[{'source_node_id':caption,'disposition':'NON_BUSINESS','reason':'Pure caption kept in table title and exact reference.'}]}
            good=mapping(patch,'map-after',bad)
            artifact_tools.compile_items(Namespace(map_dir=good,input=save('items-after.json',item_input(good)),baseline_dir=None,output_dir=root/'items',source=source))
            baseline=json.loads((root/'items/source-requirement-baseline.json').read_text())
            self.assertEqual(len(baseline['records']),2)
            self.assertEqual(sum(r['requirementness']=='REQUIREMENT' for r in baseline['records']),1)
            self.assertTrue(any('output shall stop' in b['text'] for r in baseline['records'] for b in r['text_bindings']))
            self.assertEqual((bad/'document-map.json').read_bytes(),before)
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual((good/'table-previews.json').read_bytes(),(bad/'table-previews.json').read_bytes())


if __name__=='__main__':unittest.main()
