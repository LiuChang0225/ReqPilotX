
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
from pathlib import Path
import tempfile
import unittest

import pymupdf
from app.application.agent_workflow.native_loop_tools import NativeLoopToolHost
from app.application.agent_workflow import source_requirement_baseline as core

REPO = Path(__file__).resolve().parents[2]


class PortableMapRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        pdf = pymupdf.open(); page = pdf.new_page()
        page.insert_text((40, 40), 'Contents')
        page.insert_text((40, 70), 'B Attachment B\nA Attachment A')
        page.insert_text((40, 140), '1.1 First\nA shall open.\n1.2 Second\nB shall close.')
        page = pdf.new_page()
        page.insert_text((40, 40), 'Body A')
        page.insert_text((40, 80), 'Body B')
        for x in (40, 160, 300): page.draw_line((x, 160), (x, 340))
        for y in range(160, 341, 30): page.draw_line((40, y), (300, y))
        for i, name in enumerate(('Control', 'Revision', 'Limit A', 'Limit B', 'Limit C', 'Note')):
            page.insert_text((45, 180+i*30), name)
            page.insert_text((165, 180+i*30), str(i))
        pdf.save(self.root/'source.pdf'); pdf.close()
        self.host = NativeLoopToolHost(workspace=self.root, plugin_root=REPO/'plugins/reqpilot', project_root=REPO)
        self.run_tool('capture', source='source.pdf', output_dir='capture')
        self.ir = json.loads((self.root/'capture/source_ir.json').read_text())
        self.block = next(n for n in self.ir['nodes'] if '1.1 First' in n['raw_text'] and '1.2 Second' in n['raw_text'])
        self.table = next(n for n in self.ir['nodes'] if n.get('table_payload'))
        self.serial = 0

    def tearDown(self): self.temp.cleanup()

    def run_tool(self, operation, **arguments):
        response = self.host.invoke('reqpilot_run', {'operation': operation, 'arguments': arguments})
        if not response['success']: raise ValueError(response['content_items'][0]['text'])
        return json.loads(response['content_items'][0]['text'])

    def sid(self, text): return next(n['source_node_id'] for n in self.ir['nodes'] if n['raw_text'].strip() == text)

    def decisions(self):
        raw = self.block['raw_text']; sid = self.block['source_node_id']
        mid = raw.index('1.2 Second'); first_end = raw.index('\n'); second_end = raw.index('\n', mid)
        def span(lo, hi): return {'source_node_id': sid, 'start_offset': lo, 'end_offset': hi}
        toc = self.sid('B Attachment B\nA Attachment A')
        nodes = [dict(node_id='doc',kind='DOCUMENT',parent_id=None,title='source.pdf'),
            dict(node_id='toc',kind='TABLE_OF_CONTENTS',parent_id='doc',title='Contents',
                 title_source_ids=[self.sid('Contents')],content_source_ids=[toc]),
            dict(node_id='a',kind='SECTION',parent_id='doc',title='First',label='1.1',
                 title_source_ranges=[span(0, first_end)],content_source_ranges=[span(first_end, mid)]),
            dict(node_id='b',kind='SECTION',parent_id='doc',title='Second',label='1.2',
                 title_source_ranges=[span(mid, second_end)],content_source_ranges=[span(second_end, len(raw))]),
            dict(node_id='att-a',kind='ATTACHMENT',parent_id='doc',title='Attachment A',label='A',
                 title_reference_source_ids=[toc],content_source_ids=[self.sid('Body A')]),
            dict(node_id='att-b',kind='ATTACHMENT',parent_id='doc',title='Attachment B',label='B',
                 title_reference_source_ids=[toc],content_source_ids=[self.sid('Body B')]),
            dict(node_id='control',kind='DOCUMENT_CONTROL',parent_id='doc',title='Document control')]
        used = {sid, self.table['source_node_id'], toc, self.sid('Contents'), self.sid('Body A'), self.sid('Body B')}
        return {'schema_version':'reqpilot-map-decisions/v2','source_sha256':self.ir['source_sha256'],'nodes':nodes,
            'logical_tables':[dict(scope_id='control-table',owner_node_id='control',title='Control',role='CONTROL',
                fragments=[dict(source_node_id=self.table['source_node_id'],row_range=[0,2])]),
                dict(scope_id='body-table',owner_node_id='a',title='Data',role='PARAMETERS',
                fragments=[dict(source_node_id=self.table['source_node_id'],row_range=[2,6])])],
            'source_dispositions':[dict(source_node_id=n['source_node_id'],disposition='DUPLICATE_CAPTURE',
                reason='Synthetic table words are retained in exact cells') for n in self.ir['nodes'] if n['source_node_id'] not in used]}

    def compile(self, decisions):
        self.serial += 1
        response = self.host.invoke('reqpilot_candidate', {'name':'map', 'value':decisions})
        path = json.loads(response['content_items'][0]['text'])['path']
        output=f'map-{self.serial}'
        self.run_tool('compile-map',source='source.pdf',source_ir='capture/source_ir.json',input=path,output_dir=output)
        return self.root/output

    def test_ranges_preserve_exact_source_and_reference_titles_do_not_move_body(self):
        before=(self.root/'capture/source_ir.json').read_bytes()
        output=self.compile(self.decisions()); m=json.loads((output/'document-map.json').read_text())
        nodes={n['node_id']:n for n in m['nodes']}
        self.assertLess(nodes['a']['order'], nodes['b']['order'])
        self.assertLess(nodes['att-a']['order'], nodes['att-b']['order'])
        self.assertEqual(nodes['att-a']['pages'],[2])
        self.assertEqual(nodes['att-a']['title_reference_anchors'][0]['page'],1)
        for key in ('a','b'):
            for c in nodes[key]['source_content']:
                lo,hi=c['source_text_range'];self.assertEqual(c['text'],self.block['raw_text'][lo:hi])
        self.assertLess(nodes['a']['source_anchors'][0]['bbox'][1], nodes['b']['source_anchors'][0]['bbox'][1])
        self.assertEqual((self.root/'capture/source_ir.json').read_bytes(),before)

    def test_partial_table_requests_accept_only_local_rows_and_export_without_padding(self):
        output=self.compile(self.decisions()); requests=json.loads((output/'scope-requests.json').read_text())
        tables={q['logical_table_scope_id']:q for q in requests['table_requests']}
        control,body=tables['control-table'],tables['body-table']
        self.assertEqual(control['fragments'][0]['row_count'],2)
        self.assertEqual(body['fragments'][0]['row_range'],[2,6])
        self.assertEqual(body['fragments'][0]['row_count'],4)
        self.assertEqual(sorted({c['row'] for c in body['fragments'][0]['cells']}),[0,1,2,3])
        self.assertGreater(body['fragments'][0]['bbox'][1],control['fragments'][0]['bbox'][1])
        request=core.SourceRequirementScopeRequestV1.model_validate(control)
        candidate=core.SourceRequirementSliceCandidateV1(source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,logical_table_scope_id=request.logical_table_scope_id,records=(),
            context_row_groups=(core.SourceRequirementContextRowGroupV1(role='FRONT_MATTER',
                row_group=core.SourceRequirementRowGroupV1(fragment_id=request.fragments[0].fragment_id,row_start=0,row_end=2),
                reason_codes=('whole_control',)),))
        core.validate_source_requirement_slice_candidate(request,candidate)
        from app.application.agent_workflow.source_item_delivery import _segment
        segment=_segment(body['fragments'][0],'body-table')
        self.assertEqual(segment['row_count'],4)
        self.assertEqual(segment['cells'][0]['cell_id'],self.table['table_payload']['cells'][4]['cell_id'])

    def test_overlap_and_uncovered_text_are_not_hidden(self):
        d=self.decisions(); d['nodes'][3]['title_source_ranges'][0]['start_offset']=0
        with self.assertRaisesRegex(ValueError,'overlaps'):self.compile(d)
        d=self.decisions();d['nodes'][2]['content_source_ranges'][0]['end_offset']-=3
        output=self.compile(d);r=json.loads((output/'map-compilation-receipt.json').read_text())
        self.assertEqual(r['evidence_state'],'INSUFFICIENT')
        self.assertIn(self.block['source_node_id'],r['partially_covered_text_source_ids'])

    def test_v1_rejects_new_fields_instead_of_ignoring_them(self):
        d=self.decisions();d['schema_version']='reqpilot-map-decisions/v1'
        with self.assertRaisesRegex(ValueError,'require reqpilot-map-decisions/v2'):self.compile(d)

    def test_v2_runs_through_shared_items_and_real_excel(self):
        output=self.compile(self.decisions());mapping=json.loads((output/'document-map.json').read_text())
        requests=json.loads((output/'scope-requests.json').read_text())
        decisions={'schema_version':'reqpilot-item-decisions/v1','source_sha256':self.ir['source_sha256'],
            'document_map_artifact_sha256':mapping['artifact_sha256'],'text_decisions':[],'table_decisions':[]}
        for q in requests['text_requests']:
            items=[];context=[]
            for i,c in enumerate(q['source_content']):
                span={'content_id':c['content_id'],'start_offset':0,'end_offset':len(c['text'])}
                if c['is_title'] or q.get('structural_role'):
                    context.append({'text_span':span,'reason_codes':['synthetic_structure']})
                else:
                    items.append({'item_key':f'text-{i}','label':c['text'],'source_form':'PARAGRAPH','text_spans':[span],
                        'requirementness':'NON_REQUIREMENT','requirement_kinds':[],'non_requirement_role':'REFERENCE',
                        'confidence':1.0,'decision_summary':'Explicit synthetic transport fixture, not a semantic model test','reason_codes':['fixture']})
            decisions['text_decisions'].append({'scope_id':q['scope_id'],'items':items,'structure_only_spans':context})
        for q in requests['table_requests']:
            fragment=q['fragments'][0];group={'fragment_id':fragment['fragment_id'],'row_start':0,'row_end':fragment['row_count']}
            front=bool(q.get('structural_role'))
            decisions['table_decisions'].append({'logical_table_scope_id':q['logical_table_scope_id'],
                'records':[] if front else [{'record_key':'group','label':'Synthetic data','source_form':'TABLE_BRANCH',
                    'row_groups':[group],'requirementness':'NON_REQUIREMENT','requirement_kinds':[],'non_requirement_role':'REFERENCE',
                    'confidence':1.0,'decision_summary':'Synthetic transport fixture','reason_codes':['fixture']}],
                'context_row_groups':[{'role':'FRONT_MATTER','row_group':group,'reason_codes':['whole_control']}] if front else []})
        response=self.host.invoke('reqpilot_candidate',{'name':'items','value':decisions})
        path=json.loads(response['content_items'][0]['text'])['path']
        self.run_tool('compile-items',map_dir=str(output.relative_to(self.root)),input=path,output_dir='baseline')
        self.run_tool('deliver',baseline_dir='baseline',source='source.pdf',output_dir='delivery')
        from openpyxl import load_workbook
        book=load_workbook(next((self.root/'delivery').glob('*.xlsx')))
        strings='\n'.join(str(cell.value) for sheet in book for row in sheet for cell in row if cell.value is not None)
        self.assertIn('Limit A',strings);self.assertIn('Control',strings)
        baseline=json.loads((self.root/'baseline/source-requirement-baseline.json').read_text())
        self.assertFalse(any(r['node_id']=='control' for r in baseline['records']))


if __name__=='__main__':unittest.main()
