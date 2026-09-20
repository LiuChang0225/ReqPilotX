"""Real task checks for lazy evidence, short refs, and retained stale-version guards."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT=Path(__file__).resolve().parents[3]
SCRIPTS=ROOT/'plugins/reqpilot/scripts'
sys.path[:0]=[str(SCRIPTS),str(ROOT/'prototype')]
from test_portable import make_fixture
from task_service import TaskService,ReadRequest
from task_read_resolution import resolve_source_request,source_handle


class TaskReadResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source,_,self.mapping=make_fixture(self.root/'fixture',table_image=True)
        self.service=TaskService(self.root,SCRIPTS/'reqpilot.py',ROOT)
        state=self.service.invoke('open',{'mode':'create','source':str(self.source),'goal':'Read the original.', 'creation_key':'reading'})
        self.task=state['task_id']
        self.saved=self.service.invoke('prepare',{'task_id':self.task,'expected_revision':state['revision']})
        self.task_root=Path(self.saved['task_root'])
        self.ir=Path(self.saved['selected']['READING']['path'])
        self.capture=hashlib.sha256(self.ir.read_bytes()).hexdigest()
        self.snapshot=json.loads(self.ir.read_text())

    def read(self,**kwargs):return self.service.invoke('read',{'task_id':self.task,**kwargs})

    def test_lazy_page_and_image_share_registered_evidence_under_concurrent_reads(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.read,view=view,page=1) for view in ('page','image')]
            results=[f.result() for f in futures]
        self.assertEqual({r['reading_preparation']['status'] for r in results},{'PREPARED','REUSED'})
        self.assertTrue(results[0]['text']);self.assertTrue(results[1]['_images'])
        state=self.service.state(self.task)[1]
        ops=[o for o in state['index']['committed'] if o['operation']=='reading-workspace']
        self.assertEqual(len(ops),1)
        self.assertEqual({r['revision'] for r in results},{state['revision']})
        before=(self.task_root/'workspace-index.json').read_bytes()
        repeated=self.read(view='page',page=1)
        self.assertEqual(repeated['reading_preparation']['status'],'REUSED')
        self.assertEqual(before,(self.task_root/'workspace-index.json').read_bytes())
        self.assertEqual(self.ir.read_bytes(),Path(state['selected']['READING']['path']).read_bytes())

    def test_returned_table_handle_and_single_capture_short_ref_bind_same_cells(self):
        result=self.read(view='source',limit=100,max_chars=50000)
        self.assertEqual(result['read_budget']['applied_max_chars'],24000)
        table=next(n for n in result['nodes'] if n.get('table'))
        self.assertEqual(table['handle'],source_handle(self.capture,table['ref']))
        bound=self.service.invoke('read',table['read']['request'])
        short=self.read(view='part',ref=table['ref'],part='table')
        self.assertEqual(bound['cells'],short['cells'])
        self.assertEqual(short['source_binding']['binding'],'ONLY_REGISTERED_CAPTURE')
        self.assertEqual(bound['source_binding']['binding'],'CAPTURE_BOUND_HANDLE')
        self.assertEqual(bound['capture_sha256'],self.capture)

    def test_stale_capture_or_page_conflict_fails_before_preparation(self):
        before=(self.task_root/'workspace-index.json').read_bytes()
        for request in ({'view':'image','page':1,'capture_sha256':'0'*64},
                        {'view':'part','ref':source_handle('0'*64,0),'part':'text'}):
            with self.assertRaisesRegex(ValueError,'CAPTURE_VERSION_CONFLICT'):self.read(**request)
        image=next(n for n in self.snapshot['nodes'] if n.get('asset_ref'))
        with self.assertRaisesRegex(ValueError,'SOURCE_LOCATION_CONFLICT'):
            self.read(view='image',image_anchor_id=image['source_node_id'],page=99)
        with self.assertRaisesRegex(ValueError,'INVALID_PAGE'):self.read(view='page',page=99)
        self.assertEqual(before,(self.task_root/'workspace-index.json').read_bytes())
        self.assertEqual(len(list((self.task_root/'operations').glob('*.json'))),1)

    def test_image_occurrence_supplies_page_without_changing_image_identity(self):
        image=next(n for n in self.snapshot['nodes'] if n.get('asset_ref'))
        actual=self.read(view='image',image_anchor_id=image['source_node_id'])
        by_page=self.read(view='image',page=image['native_locator']['page_number'])
        self.assertEqual(actual['image']['sha256'],by_page['image']['sha256'])
        self.assertEqual(actual['image']['page'],image['native_locator']['page_number'])
        self.assertFalse(actual['image']['annotations_rendered'])

    def test_multiple_capture_identities_require_a_bound_handle(self):
        state=deepcopy(self.service.state(self.task)[1])
        # Exercise the resolution rule directly; do not fabricate a committed task.
        state['index']['committed'].append({'artifacts':[{'kind':'SOURCE_IR','file_sha256':'0'*64}]})
        with self.assertRaisesRegex(ValueError,'CAPTURE_VERSION_REQUIRED'):
            resolve_source_request(state,ReadRequest(task_id=self.task,view='part',ref=0))
        request,binding=resolve_source_request(state,ReadRequest(task_id=self.task,view='part',ref=source_handle(self.capture,0)))
        self.assertEqual(request.capture_sha256,self.capture)
        self.assertEqual(binding['binding'],'CAPTURE_BOUND_HANDLE')

    def test_first_page_read_after_review_retains_business_head_and_delivery(self):
        from test_portable import item_decisions
        from revision_tools import load_head
        edit={k:v for k,v in self.mapping.items() if k!='source_sha256'}
        edit['schema_version']='reqpilot-map-edit/v1'
        state=self.service.invoke('map',{'task_id':self.task,'expected_revision':self.saved['revision'],'decisions':edit})
        decisions=item_decisions(Path(state['selected']['MAP']['output_dir']))
        for key in ('schema_version','source_sha256','document_map_artifact_sha256'):decisions.pop(key,None)
        state=self.service.invoke('items',{'task_id':self.task,'expected_revision':state['revision'],'decisions':decisions})
        state=self.service.invoke('review',{'task_id':self.task,'expected_revision':state['revision']})
        review=Path(self.service.state(self.task)[1]['current_working']['review_root'])
        head=load_head(review)
        before={k:state['selected'][k]['artifact_sha256'] for k in ('MAP','ITEMS')}
        page=self.read(view='page',page=1)
        self.assertEqual(page['reading_preparation']['status'],'PREPARED')
        current=self.service.state(self.task)[1]
        self.assertEqual(head,load_head(review))
        self.assertEqual(before,{k:current['selected'][k]['artifact_sha256'] for k in ('MAP','ITEMS')})
        self.assertEqual(current['candidates'],[])
        delivered=self.service.invoke('deliver',{'task_id':self.task,'expected_revision':page['revision']})
        self.assertTrue(delivered['local_delivery_ready'],delivered)


if __name__=='__main__':unittest.main()
