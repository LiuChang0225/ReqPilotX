
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import sys
from types import SimpleNamespace as Obj
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'plugins/reqpilot/scripts'))
from map_review_tools import map_review, review_response


class MapReviewTests(unittest.TestCase):
    def fixture(self, *, page=1, title=False):
        text='The module shall retain its setting.'
        content=Obj(text=text,page=page,bbox=(10,20,180,35),source_handles=('source-a',),source_text_range=(0,len(text)),is_title=title,content_id='content-a')
        parent=Obj(node_id='parent',parent_id=None,kind='DOCUMENT',label='',title='Original',pages=(1,),source_content=(),source_anchors=())
        child=Obj(node_id='child',parent_id='parent',kind='SECTION',label='1',title='1',pages=(page,),source_content=(content,),source_anchors=())
        artifact=Obj(source_sha256='a'*64,artifact_sha256='b'*64,nodes=(parent,child))
        cells=[dict(row=0,column=0,row_span=1,column_span=1,text=text)]
        previews={'table':{'fragments':[dict(page=1,bbox=[0,10,200,40],row_range=[0,1],cells=cells)]}}
        receipt=dict(evidence_state='INSUFFICIENT',uncovered_source_node_ids=['private-unused-'+str(i) for i in range(3000)],
                     partially_covered_text_source_ids=[],partially_covered_table_source_ids=[])
        snapshot=Obj(nodes=[Obj(source_node_id='source-a')])
        decisions={'logical_tables':[{'scope_id':'table','owner_node_id':'parent'}]}
        return artifact,previews,receipt,snapshot,decisions

    def test_same_location_parallel_capture_is_a_review_lead_without_deletion(self):
        args=self.fixture();review=map_review(*args)
        self.assertEqual(review['review_leads'][0]['code'],'POSSIBLE_PARALLEL_CAPTURE')
        self.assertEqual(review['nodes'][1]['body'][0]['text'],args[0].nodes[1].source_content[0].text)
        self.assertEqual(review['tables'][0]['fragments'][0]['cells'][0]['text'],args[0].nodes[1].source_content[0].text)
        self.assertEqual(review['nodes'][1]['ancestor_context'][0]['node_id'],'parent')
        self.assertEqual(review['semantic_verification'],'NOT_RUN')
        self.assertEqual(review['coverage']['unassigned_capture_nodes'],3000)
        self.assertNotIn('private-unused-',json.dumps(review))

    def test_same_words_elsewhere_and_shared_title_do_not_trigger_body_overlap(self):
        self.assertEqual(map_review(*self.fixture(page=2))['review_leads'],[])
        self.assertEqual(map_review(*self.fixture(title=True))['review_leads'],[])

    def test_large_review_returns_exact_saved_read_instead_of_truncating_source(self):
        review=map_review(*self.fixture());review['nodes'][1]['body'][0]['text']='原文'*20000
        response=review_response(review,Path('/private/result'))
        self.assertNotIn('review',response)
        self.assertEqual(response['review_read'],{'path':'/private/result/map-review.json','json_pointer':''})
        self.assertEqual(len(review['nodes'][1]['body'][0]['text']),40000)


if __name__=='__main__':unittest.main()
