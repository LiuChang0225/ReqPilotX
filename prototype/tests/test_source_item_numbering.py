"""Source-prefix observation reuses exact compiler bindings, never annotations."""
from __future__ import annotations

from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from copy import deepcopy
from io import BytesIO
import unittest

from openpyxl import load_workbook
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_numbering import observed_source_item_number
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot, export_delivery_xlsx
from app.application.agent_workflow.source_item_workbench_metadata import project_workbench_metadata
from app.application.agent_workflow.source_item_visual import compile_source_item_annotation_updates
from app.application.agent_workflow.source_item_decomposition import compile_source_item_decomposition
from .test_source_requirement_baseline import _reference_with_text_node, _candidate, _record


def compiled(texts, *, spans=None, grouped=False, forms=None, requirement=False, heading=False):
    contents = [('heading', '1 Scope', True)] if heading else []
    contents += [(f'body-{i}', text, False) for i,text in enumerate(texts)]
    reference = _reference_with_text_node(node_id='scope', label='1', title='Scope', contents=tuple(contents))
    reference['document_tree']['nodes'] = [n for n in reference['document_tree']['nodes'] if n['node_id'] in {'root','scope'}]
    reference['logical_tables'] = []
    mapping = core.compile_document_map_artifact(reference)
    request = core.build_source_item_text_scope_request(mapping, node_id='scope')
    if spans is None:
        spans = [[(i,0,len(text))] for i,text in enumerate(texts)]
        if grouped: spans = [[part for group in spans for part in group]]
    items = tuple(core.SourceItemTextBoundaryCandidateV1(item_key='item-'+str(i), label='Source item '+str(i),
        source_form=(forms[i] if forms else 'NUMBERED_CLAUSE'),
        text_spans=tuple(core.SourceItemTextSpanV1(content_id='body-'+str(index),start_offset=lo,end_offset=hi) for index,lo,hi in group),
        requirementness='REQUIREMENT' if requirement else 'NON_REQUIREMENT', requirement_kinds=('FUNCTIONAL',) if requirement else (),
        non_requirement_role=None if requirement else 'CONTEXT', confidence=0.9,
        decision_summary='Explicit original source boundaries.', reason_codes=('source_read',)) for i,group in enumerate(spans))
    candidate = core.SourceItemTextSliceCandidateV1(source_sha256=mapping.source_sha256, document_map_artifact_id=mapping.artifact_id,
        document_map_artifact_sha256=mapping.artifact_sha256, request_sha256=request.request_sha256, scope_id=request.scope_id,
        items=items, structure_only_spans=tuple(core.SourceItemStructureOnlySpanV1(
            text_span=core.SourceItemTextSpanV1(content_id=c.content_id,start_offset=0,end_offset=len(c.text)), reason_codes=('source_heading',))
            for c in request.source_content if c.is_title))
    baseline,_ = core.compile_source_requirement_baseline(document_map=mapping,requests=(),candidates=(),
        text_requests=(request,),text_candidates=(candidate,))
    return mapping,baseline


def originals(snapshot): return [node for node in snapshot['nodes'] if node.get('source_item')]


class ObservedSourceNumberTests(unittest.TestCase):
    def test_same_owner_nonrequirements_get_independent_proved_numbers_in_ui_and_excel(self):
        texts = ['1.1 This document defines the quotation scope.', '1.2 Response time means the enable-edge interval.']
        mapping,baseline = compiled(texts)
        before = mapping.model_dump(mode='json'),baseline.model_dump(mode='json')
        metadata = project_workbench_metadata(mapping,baseline)
        snapshot = build_delivery_snapshot(mapping,baseline,{})
        rows = originals(snapshot)
        self.assertEqual([n['source_number'] for n in rows],['1.1','1.2'])
        self.assertEqual([n['display_text'] for n in rows],texts)
        for node,record in zip(rows,baseline.records):
            self.assertEqual(node['source_item']['requirementness'],'NON_REQUIREMENT')
            self.assertIsNone(record.requirement_display_id)
            self.assertEqual(metadata['numbers'][record.source_record_id]['source_numbers'],node['source_item']['source_numbers'])
            evidence = node['source_item']['source_numbers'][0]['evidence']
            self.assertEqual(evidence['authority'],'ORIGINAL_SOURCE_NUMBER_LOCATION')
            self.assertEqual(evidence['source_sha256'],baseline.source_sha256)
            self.assertEqual(evidence['document_map_artifact_sha256'],mapping.artifact_sha256)
            self.assertEqual(evidence['reference']['start_offset'],0)
            self.assertEqual(evidence['reference']['end_offset'],3)
            self.assertEqual(evidence['bbox'],list(record.text_bindings[0].bbox))
        workbook = load_workbook(BytesIO(export_delivery_xlsx(snapshot)))
        try:
            exported = [r for r in workbook['ComplianceMatrix'].values if r and str(r[0]).startswith('SRC-')]
            self.assertEqual([r[2] for r in exported],['1.1','1.2'])
            self.assertEqual([r[3] for r in exported],texts)
        finally: workbook.close()
        self.assertEqual((mapping.model_dump(mode='json'),baseline.model_dump(mode='json')),before)

    def test_nonzero_binding_offsets_point_to_the_actual_original_content(self):
        text = 'Context.\n1.1 First numbered provision.\n1.2 Another numbered provision.'
        lo = text.index('1.1'); mid = text.index('1.2')
        mapping,baseline = compiled([text],spans=[[(0,0,lo)],[(0,lo,mid)],[(0,mid,len(text))]],
            forms=['PARAGRAPH','NUMBERED_CLAUSE','NUMBERED_CLAUSE'])
        rows = originals(build_delivery_snapshot(mapping,baseline,{}))
        self.assertEqual([r['source_number'] for r in rows],['','1.1','1.2'])
        refs = [r['source_item']['source_numbers'][0]['evidence']['reference'] for r in rows[1:]]
        self.assertEqual([(r['start_offset'],r['end_offset']) for r in refs],[(lo,lo+3),(mid,mid+3)])
        self.assertTrue(all(r['content_id']=='body-0' for r in refs))

    def test_explicit_scalar_and_collection_clears_override_both_prefix_and_heading(self):
        mapping,baseline = compiled(['1.1 The module shall open.'],requirement=True,heading=True)
        sid = baseline.records[0].source_record_id
        for fields in ({'source_number':''},{'source_numbers':[]}):
            with self.subTest(fields=fields):
                annotation = compile_source_item_annotation_updates(mapping,baseline,[{'source_item_id':sid,
                    **fields,'reason':'An explicit source-number display clear.'}])
                unchanged = deepcopy(annotation)
                rows = originals(build_delivery_snapshot(mapping,baseline,{},annotations=annotation))
                self.assertEqual(rows[0]['source_number'],'')
                self.assertNotIn('source_numbers',rows[0]['source_item'])
                self.assertEqual(project_workbench_metadata(mapping,baseline,annotations=annotation)['numbers'][sid]['source_number'],'')
                self.assertEqual(annotation,unchanged)
        override = compile_source_item_annotation_updates(mapping,baseline,[{'source_item_id':sid,'source_number':'1',
            'source_number_ref':{'kind':'MAP_HEADING','content_id':'heading','start_offset':0,'end_offset':1},
            'reason':'Explicitly select the real owning heading number.'}])
        self.assertEqual(originals(build_delivery_snapshot(mapping,baseline,{},annotations=override))[0]['source_number'],'1')

    def test_measurements_and_unnumbered_forms_do_not_become_numbered_source_claims(self):
        for value in ('12 V supply voltage','1.2 mA standby current','20 ms response','23 C ambient temperature',
                '3 MHz clock','5000 cycles of operation','3件零件','1.2 mm clearance','1.2 Ω resistance'):
            with self.subTest(value=value):
                mapping,baseline = compiled([value,'Ordinary context.'])
                self.assertIsNone(observed_source_item_number(mapping,baseline,baseline.records[0]))
                self.assertEqual(originals(build_delivery_snapshot(mapping,baseline,{}))[0]['source_number'],'')
        mapping,baseline = compiled(['1.1 Context retained as prose.','Other context.'],forms=['PARAGRAPH','PARAGRAPH'])
        self.assertIsNone(observed_source_item_number(mapping,baseline,baseline.records[0]))

    def test_multiple_prefixes_and_truncated_tokens_need_explicit_number_evidence(self):
        for texts in (['1.1 Open.','1.2 Close.'], ['1.1 Open.\n1.2 Close.'], ['1.1 Open. 1.2 Close.'],
                ['1.1 – 1.3 Combined clauses.'], ['1.1 / 1.2 Combined clauses.'], ['1.1 and 1.2 Combined clauses.']):
            with self.subTest(texts=texts):
                mapping,baseline = compiled(texts,grouped=True)
                self.assertIsNone(observed_source_item_number(mapping,baseline,baseline.records[0]))
                self.assertNotIn('source_numbers',originals(build_delivery_snapshot(mapping,baseline,{}))[0]['source_item'])
        text = '11.1 Source provision.'
        mapping,baseline = compiled([text],spans=[[(0,0,1)],[(0,1,len(text))]],forms=['PARAGRAPH','NUMBERED_CLAUSE'])
        self.assertIsNone(observed_source_item_number(mapping,baseline,baseline.records[1]))

    def test_leading_whitespace_and_wrapped_measurements_keep_the_exact_clause_prefix(self):
        text = '\n  1.1 The response limit is\n20 ms at\n23 C.'
        mapping,baseline = compiled([text,'1.2 Other clause.'])
        observed = observed_source_item_number(mapping,baseline,baseline.records[0])
        self.assertEqual(observed['value'],'1.1')
        self.assertEqual(observed['evidence']['reference']['start_offset'],3)
        mapping,baseline = compiled(['1 . 1 Exact spaced original number.','Other text.'])
        self.assertEqual(observed_source_item_number(mapping,baseline,baseline.records[0])['value'],'1 . 1')

    def test_bom_table_branch_and_record_numbers_are_never_text_prefix_numbers(self):
        from .test_source_requirement_baseline import SourceRequirementBaselineTests
        fixture = SourceRequirementBaselineTests(); fixture.setUp()
        for form in (core.SourceRecordForm.TABLE_BRANCH,core.SourceRecordForm.TABLE_RECORD):
            candidate = _candidate(fixture.bom_request, records=(_record('bom','1 Complete BOM',form,
                (core.SourceRequirementRowGroupV1(fragment_id='bom-fragment',row_start=1,row_end=5),)),),
                context=(core.SourceRequirementContextRowGroupV1(role='HEADER',
                    row_group=core.SourceRequirementRowGroupV1(fragment_id='bom-fragment',row_start=0,row_end=1),reason_codes=('shared_header',)),))
            baseline,_ = core.compile_source_requirement_baseline(document_map=fixture.document_map,
                requests=(fixture.bom_request,),candidates=(candidate,))
            self.assertIsNone(observed_source_item_number(fixture.document_map,baseline,baseline.records[0]))

    def test_inferred_parent_number_uses_existing_child_provenance_and_keeps_snn_identity(self):
        text = '1.1 Open the valve.\nClose the valve.'
        mapping,baseline = compiled([text],requirement=True)
        split = text.index('\n'); sid = baseline.records[0].source_record_id
        decomposition = compile_source_item_decomposition(baseline,[{'source_item_id':sid,'action':'SPLIT',
            'reason':'Each action is independent.','children':[
                {'text':text[4:split],'source_ranges':[{'segment_index':0,'start_offset':4,'end_offset':split}]},
                {'text':text[split+1:],'source_ranges':[{'segment_index':0,'start_offset':split+1,'end_offset':len(text)}]}],
            'context_ranges':[{'source_range':{'segment_index':0,'start_offset':0,'end_offset':4},'disposition':'CONTEXT'}]}])
        before = deepcopy(decomposition)
        snapshot = build_delivery_snapshot(mapping,baseline,{},decomposition=decomposition)
        children = [n for n in snapshot['nodes'] if n.get('decomposition')]
        self.assertEqual([n['source_number'] for n in children],['1.1','1.1'])
        self.assertEqual([n['node_id'] for n in children],[c['singular_requirement_id'] for c in decomposition['records'][sid]])
        metadata = project_workbench_metadata(mapping,baseline,decomposition=decomposition)
        self.assertEqual([metadata['derived_numbers'][n['node_id']]['source_number'] for n in children],['1.1','1.1'])
        self.assertEqual(decomposition,before)


if __name__=='__main__': unittest.main()
