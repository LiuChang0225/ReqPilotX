"""Empty image navigation explains proven bindings, without semantic guessing."""
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'plugins/reqpilot/scripts'), str(ROOT / 'plugins/reqpilot/tests')]
from test_portable import artifact_tools, make_fixture, write
from app.application.fast_source_capture import capture_fast_source
import pymupdf


class ImagePacketDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.capture, self.decisions = make_fixture(self.root / 'fixture')
        self.ir = json.loads((self.capture / 'source_ir.json').read_text())
        self.images = [n['source_node_id'] for n in self.ir['nodes'] if n['kind'] in {'image', 'figure'}]

    def tearDown(self):
        self.temp.cleanup()

    def mapping(self, name, *, evidence=False, exclusion=None):
        value = deepcopy(self.decisions)
        value['schema_version'] = 'reqpilot-map-decisions/v2'
        owner = value['nodes'][-1]
        if evidence or exclusion:
            owner['content_source_ids'] = [sid for sid in owner['content_source_ids'] if sid not in self.images]
        if evidence:
            owner['evidence_refs'] = [{'source_node_id': sid} for sid in self.images]
        if exclusion:
            value['source_dispositions'] += [{'source_node_id': sid, 'disposition': exclusion,
                'reason': 'Supporting figure to consider later; deliberately uninterpreted prose.'} for sid in self.images]
        path = self.root / (name + '.json'); write(path, value)
        output = self.root / name
        artifact_tools.compile_map(Namespace(source=self.source, source_ir=self.capture / 'source_ir.json', input=path, output_dir=output))
        return output

    def packet(self, mapping, name, **values):
        fields = {'command': 'image-packet', 'map_dir': mapping, 'baseline_dir': None,
                  'node_id': None, 'source': self.source, 'output_dir': self.root / name}
        fields.update(values)
        return artifact_tools.image_operation(Namespace(**fields))

    def test_reference_only_supporting_images_explain_zero_without_new_items(self):
        mapping = self.mapping('reference-only', evidence=True, exclusion='NON_BUSINESS')
        original = (mapping / 'document-map.json').read_bytes()
        result = self.packet(mapping, 'empty-packet')
        self.assertEqual((result['scope_count'], result['image_count'], result['rendered_source_images']), (0, 0, 0))
        note = result['image_binding_note']
        self.assertEqual(note['code'], 'NO_OWNED_IMAGE_BINDINGS')
        self.assertEqual(note['reference_only_image_count'], len(self.images))
        self.assertEqual(note['excluded_image_count'], len(self.images))
        self.assertFalse(note['blocks_progress'])
        self.assertEqual(note['semantic_judgment'], 'NOT_PERFORMED')
        self.assertEqual({entry['source_handle'] for entry in note['entries']}, set(self.images))
        self.assertEqual({node for entry in note['entries'] for node in entry['reference_node_ids']}, {'s6'})
        self.assertIn('numbered_body.continuations', note['next'])
        saved = json.loads((self.root / 'empty-packet/image-packets.json').read_text())
        self.assertEqual(saved['image_binding_note'], note)
        self.assertEqual((mapping / 'document-map.json').read_bytes(), original)
        self.assertFalse((mapping / 'source-requirement-baseline.json').exists())
        direct = self.packet(mapping, 'unused', output_dir=None)
        self.assertEqual(direct['image_binding_note']['reference_only_image_count'], len(self.images))

    def test_legitimate_exclusion_is_not_called_a_missing_supporting_figure(self):
        mapping = self.mapping('excluded', exclusion='PAGE_FURNITURE')
        note = self.packet(mapping, 'excluded-packet')['image_binding_note']
        self.assertEqual(note['reference_only_image_count'], 0)
        self.assertEqual(note['excluded_image_count'], len(self.images))
        self.assertTrue(all(entry['reference_node_ids'] == [] for entry in note['entries']))
        self.assertTrue(all(entry['disposition'] == 'PAGE_FURNITURE' for entry in note['entries']))
        self.assertIn('may correctly remain excluded', note['next'])
        self.assertNotIn('deliberately uninterpreted', json.dumps(note))

    def test_existing_owned_image_bindings_keep_the_normal_packet_and_renders(self):
        mapping = self.mapping('owned')
        result = self.packet(mapping, 'owned-packet')
        self.assertEqual(result['scope_count'], 1)
        self.assertEqual(result['image_count'], len(self.images))
        self.assertEqual(result['rendered_source_images'], len(self.images))
        self.assertNotIn('image_binding_note', result)
        payload = json.loads((self.root / 'owned-packet/image-packets.json').read_text())
        self.assertNotIn('image_binding_note', payload)
        self.assertTrue(all((self.root / 'owned-packet' / image['path']).is_file() for image in payload['source_image_renders']))

    def test_document_without_images_has_legal_zero_and_no_binding_notice(self):
        source = self.root / 'text-only.pdf'
        document = pymupdf.open(); page = document.new_page()
        page.insert_text((40, 50), 'Text only specification')
        page.insert_text((40, 90), 'The module shall retain its setting.')
        document.save(source); document.close()
        capture = self.root / 'text-capture'; capture_fast_source(source, output_dir=capture)
        ir = json.loads((capture / 'source_ir.json').read_text())
        texts = [n for n in ir['nodes'] if n['raw_text']]
        decision = {'schema_version': 'reqpilot-map-decisions/v2', 'source_sha256': ir['source_sha256'],
            'nodes': [{'node_id': 'text-doc', 'parent_id': None, 'kind': 'DOCUMENT', 'title': texts[0]['raw_text'],
                'title_source_ids': [texts[0]['source_node_id']], 'content_source_ids': [n['source_node_id'] for n in texts[1:]]}],
            'logical_tables': [], 'source_dispositions': []}
        input_path = self.root / 'text-map.json'; write(input_path, decision)
        mapping = self.root / 'text-map'
        artifact_tools.compile_map(Namespace(source=source, source_ir=capture / 'source_ir.json', input=input_path, output_dir=mapping))
        result = self.packet(mapping, 'text-packet', source=source)
        self.assertEqual(result['image_count'], 0)
        self.assertNotIn('image_binding_note', result)
        foreign = self.packet(mapping, 'foreign-capture', source=source, source_ir=self.capture / 'source_ir.json')
        self.assertNotIn('image_binding_note', foreign)

    def test_no_capture_proof_or_unrelated_selected_node_does_not_invent_notice(self):
        mapping = self.mapping('unproved', evidence=True, exclusion='NON_BUSINESS')
        selected = self.packet(mapping, 'other-scope', node_id=['s1'])
        self.assertNotIn('image_binding_note', selected)
        (mapping / 'map-source-ir.json').unlink()
        result = self.packet(mapping, 'without-capture')
        self.assertNotIn('image_binding_note', result)
        explicit = self.packet(mapping, 'explicit-capture', source_ir=self.capture / 'source_ir.json')
        self.assertEqual(explicit['image_binding_note']['reference_only_image_count'], len(self.images))


if __name__ == '__main__':
    unittest.main()
