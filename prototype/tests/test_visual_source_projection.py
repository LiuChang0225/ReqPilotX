
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import unittest

from app.adapters.documents.pdf_source_ir_adapter import render_pdf_visual_page_input
from app.adapters.documents.visual_text_provider_support import GroundedVisualTextCandidate, build_visual_text_snapshot
from app.application.project_visual_source import project_visual_source, verify_visual_capture_extension
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.source_visual_text import (
    VisualLayoutTable, VisualLayoutTableCell, VisualTextObservation,
    stable_visual_text_observation_id,
)
from tests import test_map_edit_tools as fixtures


class VisualSourceProjectionTests(unittest.TestCase):
    setUp = fixtures.MapEditTests.setUp
    tearDown = fixtures.MapEditTests.tearDown
    run_tool = fixtures.MapEditTests.run_tool
    request = fixtures.MapEditTests.request
    write = fixtures.MapEditTests.write
    edit = fixtures.MapEditTests.edit

    def text_snapshot(self):
        frame = render_pdf_visual_page_input(self.root/'source.pdf', page_number=1)
        return build_visual_text_snapshot(page_input=frame, provider_id='fixture-visual', provider_version='1',
            candidates=(GroundedVisualTextCandidate(self.block['raw_text'],
                tuple(self.block['native_locator']['bbox']), None),))

    def test_projection_retains_native_refs_and_rejects_changed_text(self):
        base = SourceIRSnapshot.model_validate(self.ir)
        snapshot = self.text_snapshot()
        result = project_visual_source(base, (snapshot,))
        verify_visual_capture_extension(base, result)
        self.assertEqual(result.nodes[:-1], base.nodes)
        self.assertEqual(project_visual_source(result,(snapshot,)), result)
        raw = result.model_dump(mode='json')
        raw['nodes'][-1]['raw_text'] = 'changed'
        with self.assertRaisesRegex(ValueError,'must match its observation'):
            SourceIRSnapshot.model_validate(raw)
        raw = result.model_dump(mode='json')
        raw['visual_text_snapshots'] = []
        with self.assertRaisesRegex(ValueError,'captured observation'):
            SourceIRSnapshot.model_validate(raw)

    def test_table_projection_preserves_complete_grid_and_cell_geometry(self):
        base = SourceIRSnapshot.model_validate(self.ir)
        native = next(n for n in base.nodes if n.table_payload)
        table = native.table_payload
        frame = render_pdf_visual_page_input(self.root/'source.pdf', page_number=2)
        snapshot = build_visual_text_snapshot(page_input=frame, provider_id='fixture-visual',
                                              provider_version='table1', candidates=())
        common = {k:getattr(snapshot,k) for k in ('source_sha256','page_image_sha256','provider_id','provider_version','page_number')}
        observation = VisualTextObservation(**common, observation_index=0, bbox=native.native_locator.bbox,
            observation_id=stable_visual_text_observation_id(**common,observation_index=0,bbox=native.native_locator.bbox),
            content_type='TABLE', confidence=None,
            layout_table=VisualLayoutTable(row_count=table.row_count,column_count=table.column_count,
                cells=tuple(VisualLayoutTableCell(row=c.row,column=c.column,row_span=c.row_span,
                    column_span=c.column_span,raw_text=c.raw_text,bbox=c.native_locator.bbox) for c in table.cells)))
        snapshot = snapshot.model_copy(update={'observations':(observation,),'evaluated_content_types':('TABLE',)})
        result = project_visual_source(base,(snapshot,))
        projected = result.nodes[-1]
        self.assertEqual([(c.row,c.column,c.raw_text,c.native_locator.bbox) for c in projected.table_payload.cells],
                         [(c.row,c.column,c.raw_text,c.native_locator.bbox) for c in table.cells])
        raw = result.model_dump(mode='json')
        raw['nodes'][-1]['table_payload']['cells'][0]['raw_text'] = 'false value'
        with self.assertRaisesRegex(ValueError,'observed cells and spans'):
            SourceIRSnapshot.model_validate(raw)

    def test_visual_capture_revises_existing_map_without_rewriting_baseline(self):
        self.edit(self.request(),'base-map')
        before = (self.root/'base-map/document-map.json').read_bytes()
        snapshot = self.text_snapshot()
        path = self.write(snapshot.model_dump(mode='json'))
        result = self.run_tool('capture-visual-source',source='source.pdf',source_ir='capture/source_ir.json',
                               input=path,output_dir='visual-capture')
        ref = result['refs'][0]['ref']
        edit = {'schema_version':'reqpilot-map-edit/v1','nodes':[{'node_id':'first',
            'heading':{'source':{'source':ref,'quote':'1.1 First'},'label':'1.1'},
            'content':[{'source':ref,'start':'A shall open.','end_before':'1.2 Second'}]}]}
        self.run_tool('compile-map',source='source.pdf',source_ir='visual-capture/source_ir.json',
                      input=self.write(edit),map_dir='base-map',output_dir='visual-map')
        updated=json.loads((self.root/'visual-map/document-map.json').read_text())
        node=next(n for n in updated['nodes'] if n['node_id']=='first')
        self.assertEqual(node['title_authority'],'VISUAL_TRANSCRIPTION_REVIEW_REQUIRED')
        self.assertIn('VISUAL_TRANSCRIPTION_REQUIRES_REVIEW',node['finding_codes'])
        self.assertIn('A shall open.', [c['text'].strip() for c in node['source_content']])
        self.assertEqual((self.root/'base-map/document-map.json').read_bytes(),before)
        source = json.loads((self.root/'visual-capture/source_ir.json').read_text())
        self.assertEqual(source['nodes'][:len(self.ir['nodes'])],self.ir['nodes'])


if __name__=='__main__':
    unittest.main()
