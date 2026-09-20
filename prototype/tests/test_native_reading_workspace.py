
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image
import pymupdf

from app.application.agent_workflow.native_loop_tools import NativeLoopToolHost
from app.adapters.documents.pdf_reading_views import _page_markdown, _write_contact_sheet
from scripts.native_structure_tree_evidence import _parse_page_markdown

REPO = Path(__file__).resolve().parents[2]


class NativeReadingWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        pdf = pymupdf.open()
        page = pdf.new_page(width=300, height=400)
        page.insert_text((30, 40), '1.1 Native requirement')
        page.draw_rect(pymupdf.Rect(20, 60, 80, 100), color=(1, 0, 0), fill=(1, 0, 0))
        page.add_text_annot((100, 100), 'Source review comment')
        page = pdf.new_page(width=300, height=400)
        page.insert_text((30, 40), '2.1 Rotated source')
        page.set_rotation(90)
        pdf.new_page(width=300, height=400)
        pdf.save(self.root / 'source.pdf')
        pdf.close()
        self.host = NativeLoopToolHost(workspace=self.root, plugin_root=REPO / 'plugins/reqpilot', project_root=REPO)

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, operation, **arguments):
        result = self.host.invoke('reqpilot_run', {'operation': operation, 'arguments': arguments})
        self.assertTrue(result['success'], result)
        return json.loads(result['content_items'][0]['text'])

    def test_capture_reuse_has_all_pages_real_paths_and_no_source_change(self):
        original = (self.root / 'source.pdf').read_bytes()
        self.run_tool('capture', source='source.pdf', output_dir='capture')
        ir_bytes = (self.root / 'capture/source_ir.json').read_bytes()
        result = self.run_tool('reading-workspace', source='source.pdf', source_ir='capture/source_ir.json', output_dir='reading')
        manifest = json.loads((self.root / result['manifest_path']).read_text())
        pages = json.loads((self.root / result['pages']).read_text())
        self.assertEqual(len(pages), 3)
        self.assertEqual(pages[1]['bounds'], [0, 0, 400, 300])
        self.assertEqual(pages[2]['native_text_characters'], 0)
        self.assertEqual((self.root / manifest['source_path']).read_bytes(), original)
        self.assertEqual((self.root / manifest['source_ir_path']).read_bytes(), ir_bytes)
        self.assertIn('pdf_annotations', manifest['capture_sidecars'])
        for page in pages:
            self.assertTrue((self.root / page['markdown_path']).is_file())
            render = self.root / page['high_render']['path']
            self.assertEqual(hashlib.sha256(render.read_bytes()).hexdigest(), page['high_render']['sha256'])
            self.assertEqual(page['reading_status'], 'NOT_RECORDED')
        context = json.loads(self.host.invoke('reqpilot_context', {})['content_items'][0]['text'])
        self.assertEqual(context['reading_workspaces'][0]['manifest_path'], result['manifest_path'])

    def test_native_crop_geometry_annotations_and_changed_source(self):
        args = {'path': 'source.pdf', 'page': 1, 'bbox': [20, 60, 80, 100], 'scale': 4}
        response = self.host.invoke('reqpilot_read', args)
        self.assertTrue(response['success'], response)
        metadata = json.loads(response['content_items'][0]['text'])
        self.assertEqual(metadata['bbox'], args['bbox'])
        image_data = base64.b64decode(response['content_items'][1]['imageUrl'].split(',', 1)[1])
        with Image.open(BytesIO(image_data)) as image:
            self.assertEqual(image.size, (240, 160))
            self.assertEqual(image.convert('RGB').getpixel((120, 80)), (255, 0, 0))
        self.assertFalse(self.host.invoke('reqpilot_read', {**args, 'bbox': [0, 0, 900, 900]})['success'])
        before = self.host.invoke('reqpilot_read', {'path': 'source.pdf', 'page': 1})
        marked = self.host.invoke('reqpilot_read', {'path': 'source.pdf', 'page': 1, 'annotations': True})
        self.assertTrue(marked['success'], marked)
        self.assertNotEqual(before['content_items'][1]['imageUrl'], marked['content_items'][1]['imageUrl'])
        with (self.root / 'source.pdf').open('ab') as stream:
            stream.write(b'\n')
        self.assertFalse(self.host.invoke('reqpilot_read', args)['success'])

    def test_example_full_inputs_html_and_annotations_are_reachable(self):
        example = self.run_tool('worked-example', output_dir='example')
        self.assertTrue(example['example_only'])
        self.assertEqual(example['front_matter_source_items'], 0)
        for name in ('map-decisions.json', 'item-decisions.json'):
            result = self.host.invoke('reqpilot_read', {'path': 'example/' + name, 'json_pointer': ''})
            self.assertTrue(result['success'], result)
        reading = self.run_tool('reading-workspace', source='example/example.pdf', source_ir='example/capture/source_ir.json', output_dir='example-reading')
        table = json.loads((self.root / reading['tables']).read_text())[0]
        result = self.host.invoke('reqpilot_read', {'path': table['html_path']})
        self.assertTrue(result['success'], result)
        self.assertIn('Housing', result['content_items'][0]['text'])
        baseline = json.loads((self.root / 'example/baseline/source-requirement-baseline.json').read_text())
        candidate = {'schema_version': 'reqpilot-annotation-decisions/v1',
            'source_sha256': baseline['source_sha256'], 'baseline_sha256': baseline['artifact_sha256'],
            'document_map_artifact_sha256': baseline['document_map_artifact_sha256'],
            'annotation_updates': [{'source_item_id': baseline['records'][0]['source_record_id'],
                                    'topics': [], 'reason': 'Synthetic clear-topic operation'}]}
        response = self.host.invoke('reqpilot_candidate', {'name': 'annotation', 'value': candidate})
        path = json.loads(response['content_items'][0]['text'])['path']
        result = self.run_tool('compile-annotations', baseline_dir='example/baseline', input=path, output_dir='annotations')
        self.assertEqual(result['status'], 'ANNOTATIONS_COMPILED')

    def test_source_backticks_survive_and_contact_sheet_labels_reach_second_row(self):
        wording = 'Native ``` literal\nnext line'
        page = _page_markdown({'page_number': 1, 'bounds': [0, 0, 20, 20], 'evidence_requirement': 'NATIVE',
                              'table_ids': [], 'asset_ids': []},
            (SimpleNamespace(source_handle='node-test', kind='paragraph', bbox=[0, 0, 10, 10], text=wording),))
        path = self.root / 'page.md'; path.write_text(page)
        self.assertEqual(_parse_page_markdown(path, 1)['blocks'][0]['text'], wording)
        paths = []
        for i in range(5):
            path = self.root / f'page-{i}.png'
            Image.new('RGB', (40, 60), 'white').save(path); paths.append(path)
        path = self.root / 'contact.png'; _write_contact_sheet(path, paths)
        with Image.open(path) as image:
            strip = image.convert('RGB').crop((0, 370, 260, 386))
            self.assertTrue(any(max(pixel) < 100 for pixel in strip.getdata()), 'Page 5 label missing from its own row')


if __name__ == '__main__':
    unittest.main()
