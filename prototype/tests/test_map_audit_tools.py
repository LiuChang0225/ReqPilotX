
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins/reqpilot/scripts'))
from map_audit_tools import build_map_audit


class MapAuditTests(unittest.TestCase):
    def source(self, handle, text, page=1, top=10, **extra):
        return dict(source_node_id=handle, raw_text=text, native_locator={'format':'pdf','page_number':page,'bbox':[10,top,200,top+20]}, **extra)

    def node(self, key, order, parent=None, text='', page=1, top=10, handle='s', span=None):
        return dict(node_id=key,parent_id=parent,title=key,label=key,kind='SECTION',pages=[page],order=order,
            source_content=[dict(text=text,page=page,bbox=[10,top,200,top+20],source_handles=[handle],source_text_range=span)] if text else [],source_anchors=[])

    def test_missing_middle_and_tail_are_exact_ranges_not_a_total_count(self):
        text='1 First\n2 Middle\n3 Last\nTail note'
        snapshot={'nodes':[self.source('s',text)]}
        decisions={'nodes':[{'title_source_ranges':[{'source_node_id':'s','start_offset':0,'end_offset':7}],
            'content_source_ranges':[{'source_node_id':'s','start_offset':17,'end_offset':23}]}]}
        report=build_map_audit(snapshot,{'source_sha256':'s','artifact_sha256':'a','nodes':[]},{},decisions,page_count=2)
        gaps=report['gaps']
        self.assertEqual([g['text'].strip() for g in gaps],['2 Middle','Tail note'])
        for gap in gaps:self.assertEqual(text[slice(*gap['range'])],gap['text'])
        self.assertEqual(report['pages'][1]['capture_nodes'],0)
        self.assertEqual(report['pages'][1]['visual_review'],'NOT_ESTABLISHED_BY_THIS_AUDIT')

    def test_table_rows_assets_and_unresolved_dispositions_stay_visible(self):
        table=self.source('table','',table_payload={'row_count':4,'cells':[]})
        snapshot={'nodes':[table,self.source('image','',asset_ref='asset'),self.source('s','Unresolved original')]}
        decisions={'nodes':[], 'logical_tables':[{'fragments':[{'source_node_id':'table','row_range':[1,3]}]}],
            'source_dispositions':[{'source_node_id':'s','disposition':'UNRESOLVED','reason':'Need original review'}]}
        before=copy.deepcopy(snapshot)
        report=build_map_audit(snapshot,{'source_sha256':'s','artifact_sha256':'a','nodes':[]},{},decisions)
        self.assertEqual([gap['row_range'] for gap in report['gaps'] if 'row_range' in gap],[[0,1],[3,4]])
        self.assertEqual(report['summary']['gap_counts']['UNASSIGNED_ASSET'],1)
        self.assertEqual(report['summary']['unresolved_dispositions'],1)
        self.assertEqual(snapshot,before)

    def test_reference_only_page_frame_does_not_hide_unassigned_capture_or_demand_another_table(self):
        text='The module shall retain configuration.'
        snapshot={'nodes':[self.source('t','',table_payload={'row_count':1,'cells':[{'row':0,'raw_text':text}]}),self.source('s',text)]}
        node=self.node('body',0,text=text);node['evidence_anchors']=[{'source_handle':'t'}]
        report=build_map_audit(snapshot,{'source_sha256':'s','artifact_sha256':'a','nodes':[node]}, {}, {'nodes':[{'content_source_ids':['s']}]})
        self.assertEqual(len(report['gaps']),1)
        self.assertTrue(report['gaps'][0]['reference_only'])
        self.assertEqual(report['gaps'][0]['possible_parallel_map_nodes'],['body'])
        self.assertEqual(report['semantic_verification'],'NOT_RUN')

    def test_expanded_tree_detects_a_backtrack_even_when_the_flat_array_is_source_sorted(self):
        nodes=[self.node('root',0),self.node('a',1,'root','Heading A',1),self.node('b',2,'root','Heading B',2),
            self.node('late_child',3,'a','Wrongly under A',3)]
        snapshot={'nodes':[self.source('s','source')]}
        report=build_map_audit(snapshot,{'source_sha256':'s','artifact_sha256':'a','nodes':nodes},{},{'nodes':[]})
        self.assertEqual([n['node_id'] for n in report['expanded_tree']],['root','a','late_child','b'])
        self.assertEqual(report['order_leads'][0]['before']['node_id'],'late_child')
        self.assertEqual(report['order_leads'][0]['after']['node_id'],'b')
        self.assertEqual([n['node_id'] for n in nodes],['root','a','b','late_child'])

    def test_same_block_offsets_define_order_not_lexical_labels(self):
        nodes=[self.node('root',0),self.node('1.2',1,'root','First',span=[0,5]),self.node('1.10',2,'root','Second',span=[6,12])]
        report=build_map_audit({'nodes':[self.source('s','First Second')]},
            {'source_sha256':'s','artifact_sha256':'a','nodes':nodes},{},{'nodes':[]})
        self.assertEqual(report['order_leads'],[])
        nodes[1]['order'],nodes[2]['order']=2,1
        report=build_map_audit({'nodes':[self.source('s','First Second')]},
            {'source_sha256':'s','artifact_sha256':'a','nodes':nodes},{},{'nodes':[]})
        self.assertEqual(len(report['order_leads']),1)

    def test_preserved_words_can_still_hide_original_numbered_boundaries(self):
        text='1、First obligation\n2、Second obligation\n3、Third obligation'
        nodes=[self.node('scope',0,text=text)]
        artifact={'source_sha256':'s','artifact_sha256':'a','nodes':nodes}
        original=copy.deepcopy(artifact)
        report=build_map_audit({'nodes':[self.source('s',text)]},artifact,{}, {'nodes':[{'content_source_ids':['s']}]})
        self.assertEqual(report['gaps'],[])
        group=report['numbered_body_groups'][0]
        self.assertEqual([m['label'] for m in group['markers']],['1、','2、','3、'])
        for marker in group['markers']:self.assertEqual(text[slice(*marker['range'])],marker['label'])
        self.assertEqual(artifact,original)
        self.assertIn('complete unit',group['action'])

    def test_decimal_prefix_is_not_truncated_into_a_numbered_clause(self):
        text='0.8 mA\n1.5 V\n2.0 ms'
        report=build_map_audit({'nodes':[self.source('s',text)]},
            {'source_sha256':'s','artifact_sha256':'a','nodes':[self.node('scope',0,text=text)]},{},{'nodes':[]})
        self.assertEqual(report['numbered_body_groups'],[])

    def test_cross_page_numbered_body_parent_change_is_visible_without_reparenting(self):
        nodes=[self.node('root',0),self.node('group',1,'root','Governing prefix'),self.node('3、',2,'group','Third',1),self.node('4、',3,'root','Fourth',2)]
        original=copy.deepcopy(nodes)
        artifact={'source_sha256':'s','artifact_sha256':'a','nodes':nodes}
        report=build_map_audit({'nodes':[self.source('s','source')]},artifact,{},{'nodes':[]})
        self.assertEqual(len(report['numbered_parent_change_leads']),1)
        self.assertEqual(nodes,original)
        nodes[-1]['parent_id']='group'
        self.assertEqual(build_map_audit({'nodes':[self.source('s','source')]},artifact,{},{'nodes':[]})['numbered_parent_change_leads'],[])
        nodes[-1]['parent_id']='root';nodes[-1]['title']='A distinct chapter heading'
        self.assertEqual(build_map_audit({'nodes':[self.source('s','source')]},artifact,{},{'nodes':[]})['numbered_parent_change_leads'],[])


if __name__=='__main__':unittest.main()
