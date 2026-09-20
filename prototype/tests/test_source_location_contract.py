
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from copy import deepcopy
from pathlib import Path
import json
import unittest

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_locations import source_location_ref, source_position
from . import test_source_requirement_baseline as fixtures


class SourceLocationContractTests(unittest.TestCase):
    def test_native_text_keeps_real_locator_without_pdf_geometry(self):
        text = 'The controller shall report readiness.'
        item = core.SourceItemTextBindingV1(content_id='paragraph-0', page=None, bbox=None, source_bounds=None,
            start_offset=0, end_offset=len(text), text=text, text_sha256=core.canonical_text_sha256(text),
            precision='NATIVE_CONTAINER_PATH', native_locator={'format':'docx','body_element_index':0,'paragraph_index':0}, source_sequence=0)
        self.assertEqual(source_position(item), (0,0,0,0,-1,0))
        self.assertEqual(source_location_ref(item), {'format':'docx','body_element_index':0,'paragraph_index':0})
        with self.assertRaises(ValueError):
            core.SourceItemTextBindingV1.model_validate({**item.model_dump(mode='json'),'page':1,'bbox':[0.,0.,1.,1.],'source_bounds':[0.,0.,1.,1.]})

    def test_pdf_optional_native_fields_do_not_change_legacy_serialization(self):
        fixture = fixtures.SourceItemScopeRevisionTests();fixture.setUp()
        for value in fixture.document_map.nodes:
            for content in value.source_content:
                payload=content.model_dump(mode='json')
                self.assertNotIn('native_locator',payload)
                self.assertNotIn('source_sequence',payload)
        self.assertNotIn('source_format',fixture.document_map.model_dump(mode='json'))

    def test_unknown_content_reports_its_id_and_nearby_legal_id_without_replacing_it(self):
        fixture = fixtures.SourceItemScopeRevisionTests();fixture.setUp()
        actual = fixture.request.source_content[0].content_id
        wrong = actual[:-1]
        for role in ('item','structure'):
            data=fixture.initial_candidate.model_dump(mode='json')
            if role=='item':
                data['items'][0]['text_spans'][0]['content_id']=wrong
            else:
                data['structure_only_spans']=[{'text_span':{'content_id':wrong,'start_offset':0,'end_offset':1},'reason_codes':['source_title']}]
            candidate=core.SourceItemTextSliceCandidateV1.model_validate(data)
            with self.subTest(role=role), self.assertRaises(core.SourceRequirementBaselineError) as failed:
                core.validate_source_item_text_slice_candidate(fixture.request,candidate)
            self.assertIn(wrong,str(failed.exception));self.assertIn(actual,str(failed.exception))
            self.assertIn('request.source_content[].content_id',str(failed.exception))
            self.assertNotIn(fixture.scope_text,str(failed.exception))

    def test_native_position_uses_real_rows_columns_and_offsets_for_same_sequence(self):
        from types import SimpleNamespace
        def pos(fmt, **fields):
            return source_position(SimpleNamespace(native_locator={'format':fmt,**fields},source_sequence=7,row_start=0))
        self.assertLess(pos('xlsx',workbook='xl/workbook.xml',sheet='S',cell_range='A2:B2'),pos('xlsx',workbook='xl/workbook.xml',sheet='S',cell_range='E2:F2'))
        self.assertLess(pos('xlsx',workbook='xl/workbook.xml',sheet='S',cell_range='E2:F2'),pos('xlsx',workbook='xl/workbook.xml',sheet='S',cell_range='A3:B3'))
        self.assertLess(pos('docx',body_element_index=2,table_index=0,row_index=1,column_index=1),pos('docx',body_element_index=2,table_index=0,row_index=2,column_index=0))
