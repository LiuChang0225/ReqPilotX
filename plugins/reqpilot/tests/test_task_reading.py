"""Read-only bounded views against real captured PDF/Map/reading workspaces."""
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json
import shutil
import sys
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'prototype'),str(ROOT/'plugins/reqpilot/scripts')]
from test_portable import make_fixture,write
import artifact_tools
from reading_workspace_tools import reading_workspace
import task_reading as reader
from app.application.fast_source_capture import capture_fast_source


def size(value):return len(json.dumps(value,ensure_ascii=False,separators=(',',':')))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


class TaskReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source,self.capture,self.decisions=make_fixture(self.root/'fixture',table_image=True)
        self.ir=self.capture/'source_ir.json';self.source_sha=sha(self.source)
        write(self.root/'map-input.json',self.decisions)
        self.mapping=self.root/'map'
        artifact_tools.compile_map(Namespace(source=self.source,source_ir=self.ir,input=self.root/'map-input.json',output_dir=self.mapping))
        self.reading=self.root/'reading'
        reading_workspace(Namespace(source=self.source,source_ir=self.ir,output_dir=self.reading))

    def test_source_index_is_compact_and_mixed_refs_select_union(self):
        snapshot=json.loads(self.ir.read_text())
        result=reader.source_authoring(self.ir,expected_source_sha256=self.source_sha,refs=[0,snapshot['nodes'][1]['source_node_id']],max_chars=2048)
        self.assertEqual([node['ref'] for node in result['nodes']],[0,1])
        self.assertEqual(result['capture_sha256'],sha(self.ir))
        self.assertTrue(size(result)<=2048)
        self.assertEqual(result['nodes'][0]['raw_text'],snapshot['nodes'][0]['raw_text'])
        self.assertTrue(result['nodes'][0]['text_complete'])
        self.assertNotIn('read',result['nodes'][0])
        read=[];offset=0
        while True:
            page=reader.source_authoring(self.ir,expected_source_sha256=self.source_sha,offset=offset,limit=1,max_chars=2048)
            self.assertTrue(size(page)<=2048);read.extend(n['ref'] for n in page['nodes'])
            if not page['next_read']:break
            self.assertGreater(page['next_read']['arguments']['offset'],offset)
            offset=page['next_read']['arguments']['offset']
        self.assertEqual(read,list(range(len(snapshot['nodes']))))

    def test_source_tables_are_paged_as_cells_without_losing_merge_geometry(self):
        snapshot=json.loads(self.ir.read_text())
        ref=next(i for i,n in enumerate(snapshot['nodes']) if n.get('table_payload'))
        original=snapshot['nodes'][ref]['table_payload']['cells']
        cells=[];offset=0
        while True:
            value=reader.source_part(self.ir,expected_source_sha256=self.source_sha,capture_sha256=sha(self.ir),ref=ref,part='table',offset=offset,limit=2,max_chars=2048)
            self.assertLessEqual(size(value),2048);cells.extend(value['cells'])
            if not value['next_read']:break
            offset=value['next_read']['arguments']['offset']
        self.assertEqual(len(cells),len(original))
        self.assertEqual([(c['row'],c['column'],c['row_span'],c['column_span'],c['text']) for c in cells],
            [(c['row'],c['column'],c['row_span'],c['column_span'],c['raw_text']) for c in original])
        self.assertEqual(value['map_fragment']['row_range'],[0,snapshot['nodes'][ref]['table_payload']['row_count']])

    def test_long_real_source_text_has_lossless_capture_and_scope_continuations(self):
        import pymupdf
        source=self.root/'long.pdf';pdf=pymupdf.open();page=pdf.new_page()
        words=' '.join('word%04d'%i for i in range(900))
        result=page.insert_textbox(pymupdf.Rect(35,35,560,800),words,fontsize=7)
        self.assertGreaterEqual(result,0)
        pdf.save(source);pdf.close()
        capture=self.root/'long-capture';capture_fast_source(source,output_dir=capture,include_structure=False)
        ir=capture/'source_ir.json';data=json.loads(ir.read_text());ref=max(range(len(data['nodes'])),key=lambda i:len(data['nodes'][i]['raw_text']))
        raw=data['nodes'][ref]['raw_text'];self.assertGreater(len(raw),4000)
        ordinary=reader.source_authoring(ir,expected_source_sha256=sha(source),refs=[ref],max_chars=12000)
        self.assertTrue(ordinary['nodes'][0]['text_complete'])
        self.assertEqual(ordinary['nodes'][0]['raw_text'],raw)
        self.assertNotIn('next_text_read',ordinary['nodes'][0])
        oversized=reader.source_authoring(ir,expected_source_sha256=sha(source),refs=[ref],max_chars=2048)
        self.assertFalse(oversized['nodes'][0]['text_complete'])
        self.assertLessEqual(size(oversized),2048)
        self.assertEqual(oversized['nodes'][0]['next_text_read']['arguments']['offset'],len(oversized['nodes'][0]['raw_text']))
        arguments={'capture_sha256':sha(ir),'ref':ref,'part':'text','offset':0,'max_chars':2048};chunks=[]
        while True:
            view=reader.source_part(ir,expected_source_sha256=sha(source),**arguments)
            self.assertLessEqual(size(view),2048);chunks.append(view['text'])
            if not view['next_read']:break
            self.assertGreater(view['next_read']['arguments']['offset'],arguments['offset'])
            arguments=view['next_read']['arguments']
        self.assertEqual(''.join(chunks),raw)
        decisions={'schema_version':'reqpilot-map-decisions/v1','source_sha256':sha(source),
            'nodes':[{'node_id':'document','parent_id':None,'kind':'DOCUMENT','title':source.name,'label':'',
                      'title_source_ids':[],'content_source_ids':[node['source_node_id'] for node in data['nodes']]}],
            'logical_tables':[],'source_dispositions':[]}
        write(self.root/'long-map-input.json',decisions);mapping=self.root/'long-map'
        artifact_tools.compile_map(Namespace(source=source,source_ir=ir,input=self.root/'long-map-input.json',output_dir=mapping))
        scope=reader.scope_read(mapping,expected_source_sha256=sha(source),scope_id='text:document',max_chars=2048)
        item=next(item for item in scope['entries'] if item['text_total_characters']>4000)
        self.assertFalse(item['text_complete'])
        chunks=[item['text']];arguments=item['next_text_read']['arguments']
        while True:
            view=reader.scope_read(mapping,expected_source_sha256=sha(source),**arguments)
            self.assertLessEqual(size(view),2048);chunks.append(view['text'])
            if not view['next_read']:break
            arguments=view['next_read']['arguments']
        self.assertEqual(''.join(chunks),raw)

    def test_scope_reads_keep_actual_local_content_and_fragment_id_namespaces(self):
        index=reader.scope_read(self.mapping,expected_source_sha256=self.source_sha,limit=100,max_chars=100000)
        text=next(s for s in index['scopes'] if s['scope_kind']=='TEXT')
        value=reader.scope_read(self.mapping,expected_source_sha256=self.source_sha,**text['read']['arguments'])
        raw=json.loads((self.mapping/'scope-requests.json').read_text())
        request=next(r for r in raw['text_requests'] if r['scope_id']==text['scope_id'])
        self.assertEqual([c['content_id'] for c in value['entries']],[c['content_id'] for c in request['source_content']])
        table=next(s for s in index['scopes'] if s['scope_kind']=='TABLE')
        expected=next(r for r in raw['table_requests'] if r['logical_table_scope_id']==table['scope_id'])
        self.assertEqual(table['logical_table_role'],expected['logical_table_role'])
        self.assertEqual(table['logical_table_title'],expected['logical_table_title'])
        value=reader.scope_read(self.mapping,expected_source_sha256=self.source_sha,**table['read']['arguments'])
        self.assertEqual(value['logical_table_role'],expected['logical_table_role'])
        self.assertTrue(value['authoring']['template_only'])
        self.assertEqual(value['authoring']['decisions']['table_decisions'][0]['records'],[])
        fragment=next(v for v in value['entries'] if v['kind']=='TABLE_FRAGMENT')
        view=reader.scope_read(self.mapping,expected_source_sha256=self.source_sha,**fragment['read']['arguments'])
        self.assertEqual(view['fragment_id'],fragment['fragment_id'])
        self.assertEqual(view['fragment_ref'],fragment['fragment_ref'])
        self.assertIn('LOCAL_TO_COMPILED_FRAGMENT',view['row_coordinates'])
        self.assertTrue(all({'row','column','row_span','column_span'}<=set(cell) for cell in view['cells']))

    def test_scope_authoring_templates_are_lossless_and_do_not_decide_granularity(self):
        index=reader.scope_read(self.mapping,expected_source_sha256=self.source_sha,limit=100,max_chars=100000)
        for scope in index['scopes']:
            whole=reader.scope_read(self.mapping,expected_source_sha256=self.source_sha,scope_id=scope['scope_id'],part='template',max_chars=100000)['data']
            self.assertTrue(whole['template_only']);self.assertFalse(whole['submit_unchanged'])
            key='text_decisions' if scope['scope_kind']=='TEXT' else 'table_decisions'
            record_key='items' if scope['scope_kind']=='TEXT' else 'records'
            self.assertEqual(whole['decisions'][key][0][record_key],[])
            page=reader.scope_read(self.mapping,expected_source_sha256=self.source_sha,scope_id=scope['scope_id'],part='template',max_chars=2048)
            self.assertLessEqual(size(page),2048)
            if 'data' in page:self.assertEqual(page['data'],whole);continue
            chunks=[page['text']]
            while page['next_read']:
                page=reader.scope_read(self.mapping,expected_source_sha256=self.source_sha,**page['next_read']['arguments'])
                self.assertLessEqual(size(page),2048);chunks.append(page['text'])
            self.assertEqual(json.loads(''.join(chunks)),whole)

    def test_page_markdown_and_png_are_relocatable_and_read_only(self):
        before={str(p.relative_to(self.reading)):sha(p) for p in self.reading.rglob('*') if p.is_file()}
        moved=self.root/'moved';shutil.copytree(self.reading,moved)
        reading=reader.page_read(moved,expected_source_sha256=self.source_sha,page=1,max_chars=2048)
        self.assertLessEqual(size(reading),2048)
        image=reader.page_image(moved,expected_source_sha256=self.source_sha,page=1)
        self.assertTrue(Path(image['path']).is_relative_to(moved.resolve()))
        self.assertEqual(image['mime_type'],'image/png')
        self.assertEqual(image['sha256'],sha(Path(image['path'])))
        self.assertFalse(image['annotations_rendered'])
        self.assertEqual(before,{str(p.relative_to(moved)):sha(p) for p in moved.rglob('*') if p.is_file()})
        self.assertEqual(before,{str(p.relative_to(self.reading)):sha(p) for p in self.reading.rglob('*') if p.is_file()})

    def test_stale_refs_foreign_sources_and_tampered_images_fail_clearly(self):
        with self.assertRaisesRegex(ValueError,'another task original'):
            reader.source_authoring(self.ir,expected_source_sha256='0'*64)
        with self.assertRaisesRegex(ValueError,'capture changed'):
            reader.source_part(self.ir,expected_source_sha256=self.source_sha,capture_sha256='0'*64,ref=0)
        with self.assertRaisesRegex(ValueError,'Map changed'):
            reader.scope_read(self.mapping,expected_source_sha256=self.source_sha,expected_map_sha256='0'*64)
        with self.assertRaisesRegex(ValueError,'outside'):
            reader.page_read(self.reading,expected_source_sha256=self.source_sha,page=2)
        image=reader.page_image(self.reading,expected_source_sha256=self.source_sha,page=1)
        Path(image['path']).write_bytes(b'not a page image')
        with self.assertRaisesRegex(ValueError,'page image changed'):
            reader.page_image(self.reading,expected_source_sha256=self.source_sha,page=1)

    def test_large_cell_text_is_explicit_and_reassembled_without_truncation_loss(self):
        import pymupdf
        source=self.root/'large-table.pdf';pdf=pymupdf.open();page=pdf.new_page()
        for x in (35,300,560):page.draw_line((x,35),(x,780))
        for y in (35,65,780):page.draw_line((35,y),(560,y))
        page.insert_text((42,54),'Long cell',fontsize=9);page.insert_text((307,54),'Other',fontsize=9)
        words=' '.join('cell%04d'%i for i in range(700))
        self.assertGreaterEqual(page.insert_textbox(pymupdf.Rect(42,72,293,770),words,fontsize=5),0)
        page.insert_text((307,85),'Static neighbour',fontsize=9)
        pdf.save(source);pdf.close();capture=self.root/'large-table-capture'
        capture_fast_source(source,output_dir=capture,include_structure=False)
        ir=capture/'source_ir.json';data=json.loads(ir.read_text())
        ref=next(i for i,node in enumerate(data['nodes']) if node.get('table_payload'))
        cells=data['nodes'][ref]['table_payload']['cells'];index=max(range(len(cells)),key=lambda i:len(cells[i]['raw_text']))
        value=reader.source_part(ir,expected_source_sha256=sha(source),capture_sha256=sha(ir),ref=ref,part='table',offset=index,limit=1,max_chars=2048)
        entry=value['cells'][0];self.assertFalse(entry['text_complete']);self.assertLessEqual(size(value),2048)
        chunks=[entry['text']];args=entry['next_text_read']['arguments']
        while True:
            part=reader.source_part(ir,expected_source_sha256=sha(source),**args);chunks.append(part['text']);self.assertLessEqual(size(part),2048)
            if not part['next_read']:break
            args=part['next_read']['arguments']
        self.assertEqual(''.join(chunks),cells[index]['raw_text'])
