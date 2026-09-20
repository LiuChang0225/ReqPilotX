
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import unittest

from tests import test_map_edit_tools as fixtures


class MapEvidenceAndPdfTextTests(unittest.TestCase):
    setUp = fixtures.MapEditTests.setUp
    tearDown = fixtures.MapEditTests.tearDown
    run_tool = fixtures.MapEditTests.run_tool
    request = fixtures.MapEditTests.request
    write = fixtures.MapEditTests.write
    edit = fixtures.MapEditTests.edit

    def test_evidence_is_addressable_but_does_not_create_or_cover_business_scope(self):
        request=self.request()
        ref=next(i for i,n in enumerate(self.ir['nodes']) if n['source_node_id']==self.table['source_node_id'])
        request['logical_tables']=[{'scope_id':'business','owner_node_id':'first','title':'Data','role':'PARAMETERS',
            'fragments':[{'source_node_id':ref,'row_range':[2,6]}]}]
        request['nodes'][2]['evidence_refs']=[{'source':ref,'row_range':[0,2]}]
        self.edit(request,'with-evidence')
        mapping=json.loads((self.root/'with-evidence/document-map.json').read_text())
        second=next(n for n in mapping['nodes'] if n['node_id']=='second')
        self.assertEqual(second['pages'],[1])
        self.assertEqual(second['evidence_anchors'][0]['row_range'],[0,2])
        scopes=json.loads((self.root/'with-evidence/scope-requests.json').read_text())
        self.assertEqual([q['logical_table_scope_id'] for q in scopes['table_requests']],['business'])
        receipt=json.loads((self.root/'with-evidence/map-compilation-receipt.json').read_text())
        self.assertIn(self.table['source_node_id'],receipt['partially_covered_table_source_ids'])
        review=json.loads((self.root/'with-evidence/map-review.json').read_text())
        evidence=next(n for n in review['nodes'] if n['node_id']=='second')['evidence_refs'][0]
        self.assertEqual(evidence['effect'],'REFERENCE_ONLY_NO_EXTRACTION_SCOPE')
        self.assertTrue(self.host.invoke('reqpilot_read',evidence['full_source_read'])['success'])
        before=(self.root/'with-evidence/document-map.json').read_bytes()
        self.edit({'schema_version':'reqpilot-map-edit/v1','remove_table_ids':['business'],
            'nodes':[{'node_id':'first','evidence_refs':[{'source':ref,'row_range':[2,6]}]}]},'reference-only','with-evidence')
        self.assertEqual(json.loads((self.root/'reference-only/scope-requests.json').read_text())['table_requests'],[])
        self.assertEqual((self.root/'with-evidence/document-map.json').read_bytes(),before)

    def test_invalid_evidence_keeps_previous_map(self):
        self.edit(self.request(),'base');before=(self.root/'base/document-map.json').read_bytes()
        for ref in [{'source':999999},{'source':self.table['source_node_id'],'row_range':[0,999]}]:
            with self.assertRaises(ValueError):
                self.edit({'schema_version':'reqpilot-map-edit/v1','nodes':[{'node_id':'first','evidence_refs':[ref]}]},'bad','base')
            self.assertFalse((self.root/'bad').exists())
        self.assertEqual((self.root/'base/document-map.json').read_bytes(),before)

    def test_pdf_text_append_preserves_old_indexes_and_can_revise_old_map(self):
        self.edit(self.request(),'base')
        before=(self.root/'capture/source_ir.json').read_bytes();original=(self.root/'source.pdf').read_bytes()
        result=self.run_tool('capture-pdf-text',source='source.pdf',source_ir='capture/source_ir.json',page=[1],query='1.1 First',output_dir='trace')
        observation=result['observations'][0]
        self.assertEqual(observation['raw_text'],'1.1 First')
        self.assertGreaterEqual(observation['ref'],len(self.ir['nodes']))
        snapshot=json.loads((self.root/'trace/source_ir.json').read_text())
        self.assertEqual(snapshot['nodes'][:len(self.ir['nodes'])],self.ir['nodes'])
        candidate=self.write({'schema_version':'reqpilot-map-edit/v1','nodes':[{'node_id':'first',
            'heading':{'source':observation['ref'],'label':'1.1'}}]})
        self.run_tool('compile-map',source='source.pdf',source_ir='trace/source_ir.json',input=candidate,map_dir='base',output_dir='updated')
        updated=json.loads((self.root/'updated/document-map.json').read_text())
        node=next(n for n in updated['nodes'] if n['node_id']=='first')
        self.assertEqual(node['title_authority'],'PDF_TEXT_OBJECT_REVIEW_REQUIRED')
        self.assertIn('PDF_DRAWING_TEXT_VISIBILITY_REQUIRES_REVIEW',node['finding_codes'])
        self.assertEqual((self.root/'capture/source_ir.json').read_bytes(),before)
        self.assertEqual((self.root/'source.pdf').read_bytes(),original)
        # Replaying identical observations adds no duplicate nodes.
        again=self.run_tool('capture-pdf-text',source='source.pdf',source_ir='trace/source_ir.json',page=[1],query='1.1 First',output_dir='trace-again')
        self.assertEqual(again['appended_node_count'],0)
        self.assertEqual((self.root/'trace/source_ir.json').read_bytes(),(self.root/'trace-again/source_ir.json').read_bytes())

    def test_forged_drawing_text_is_rejected_by_original_replay(self):
        self.run_tool('capture-pdf-text',source='source.pdf',source_ir='capture/source_ir.json',page=[1],query='First',output_dir='trace')
        from app.domain.extraction.source_ir import SourceIRSnapshot
        from pdf_text_tools import verify_trace_nodes
        value=json.loads((self.root/'trace/source_ir.json').read_text());node=value['nodes'][-1]
        node['raw_text']=node['raw_text'].replace('First','False')
        node['pdf_text_trace']['text_sha256']=hashlib.sha256(node['raw_text'].encode()).hexdigest()
        with self.assertRaisesRegex(ValueError,'exact original'):
            verify_trace_nodes(SourceIRSnapshot.model_validate(value),self.root/'source.pdf')

    def test_rotated_drawing_text_uses_visible_page_coordinates(self):
        import pymupdf
        from pdf_text_tools import drawing_lines
        doc=pymupdf.open();page=doc.new_page(width=300,height=500);page.insert_text((30,80),'Visible text');page.set_rotation(90)
        path=self.root/'rotated.pdf';doc.save(path);doc.close()
        record=next(r for r in drawing_lines(path,[1]) if r['raw_text']=='Visible text')
        with pymupdf.open(path) as doc:
            self.assertGreaterEqual(record['bbox'][0],0)
            self.assertLessEqual(record['bbox'][2],doc[0].rect.width)
            self.assertLessEqual(record['bbox'][3],doc[0].rect.height)
            self.assertGreater(record['bbox'][0],300)


if __name__=='__main__':unittest.main()
