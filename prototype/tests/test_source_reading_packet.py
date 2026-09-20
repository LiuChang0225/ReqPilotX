
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.domain.extraction.source_ir import (
    NativeLocator, SourceIRSnapshot, SourceNodeKind, stable_cell_id,
    stable_document_id, stable_node_id, stable_source_ir_id, stable_table_id,
)
from app.application.agent_workflow.native_loop_tools import NativeLoopToolHost
from scripts.verify_source_reading_packet import verify

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('source_reading_tools', REPO / 'plugins/reqpilot/scripts/reading_tools.py')
reading = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reading)


class SourceReadingPacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        identity = {'source_sha256': 'a' * 64, 'adapter_id': 'fixture', 'adapter_version': '1'}
        def node(kind, order, locator, **extra):
            loc = NativeLocator(**locator)
            return {'source_node_id': stable_node_id(**identity, kind=SourceNodeKind(kind), native_locator=loc),
                    'source_order': order, 'kind': kind, 'native_locator': loc, **extra}
        text_loc = {'format': 'pdf', 'page_number': 1, 'block_index': 0, 'bbox': [0, 0, 10, 10]}
        table_loc = NativeLocator(format='pdf', page_number=2, table_index=0, bbox=(0, 0, 10, 10))
        tid = stable_table_id(**identity, native_locator=table_loc)
        cells = [{'cell_id': stable_cell_id(tid, row, col), 'row': row, 'column': col,
                  'row_span': span, 'raw_text': text,
                  'native_locator': {**table_loc.model_dump(), 'row_index': row, 'column_index': col}}
                 for row, col, span, text in [(0, 0, 2, '共享条件\n≤ 5'), (0, 1, 1, ''), (1, 1, 1, 'A\tB')]]
        cells[2].update(formula='=1+1', display_value='2')
        merge = {'row': 0, 'column': 0, 'row_span': 2, 'column_span': 1}
        nodes = [node('paragraph', 0, text_loc, raw_text='1.1 原文\n不得删改', visual_locator=text_loc,
                      pdf_text_lines=[{'start_offset': 0, 'end_offset': 6, 'bbox': [0, 0, 5, 5]}]),
                 node('table', 1, table_loc.model_dump(), raw_text='共享条件', table_payload={
                     'table_id': tid, 'row_count': 2, 'column_count': 2, 'cells': cells,
                     'merged_regions': [merge], 'header_regions': [merge], 'notes': ['注：共同适用'], 'visual_asset_ref': 'asset-1'})]
        for order in (2, 3):
            nodes.append(node('image', order, {**text_loc, 'block_index': order}, asset_ref='asset-1',
                              parent_source_node_id=nodes[1]['source_node_id']))
        document_id = stable_document_id(identity['source_sha256'])
        self.snapshot = SourceIRSnapshot(**identity, document_id=document_id,
            source_ir_id=stable_source_ir_id(**identity, document_id=document_id), status='COMPLETE',
            created_at='2026-09-10T00:00:00Z', nodes=nodes, assets=[{
                'asset_id': 'asset-1', 'kind': 'image', 'content_sha256': 'b' * 64,
                'media_type': 'image/png', 'storage_key': 'assets/image.png'}])
        self.source = self.root / 'source_ir.json'
        self.source.write_text(self.snapshot.model_dump_json(indent=2))

    def tearDown(self):
        self.temp.cleanup()

    def packet(self):
        return reading.reading_packet(Namespace(source_ir=self.source, output_dir=self.root / 'reading'))

    def test_full_projection_restores_merges_blanks_formulas_and_repeated_images(self):
        before = self.source.read_bytes()
        result = self.packet()
        checked = verify(Path(result['manifest_path']))
        self.assertEqual((checked['nodes'], checked['cells'], checked['assets'], checked['image_occurrences']), (4, 3, 1, 2))
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(result['semantic_authority'], 'NONE')
        with self.assertRaisesRegex(ValueError, 'Output must not exist'):
            self.packet()

    def test_large_record_remains_complete_and_is_explicitly_flagged(self):
        with patch.object(reading, 'MAX_CHARS', 100):
            result = self.packet()
        self.assertTrue(any(part['oversized_record'] for part in result['parts']))
        verify(Path(result['manifest_path']))

    def test_changed_source_or_packet_is_detected(self):
        result = self.packet()
        part = Path(result['parts'][0]['path'])
        original = part.read_bytes()
        part.write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            verify(Path(result['manifest_path']))
        part.write_bytes(original)
        self.source.write_text(self.source.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'SourceIR changed'):
            verify(Path(result['manifest_path']))

    def test_native_host_runs_shared_command_without_general_agent_shell(self):
        host = NativeLoopToolHost(workspace=self.root, plugin_root=REPO / 'plugins/reqpilot', project_root=REPO)
        response = host.invoke('reqpilot_run', {'operation': 'reading-packet', 'arguments': {
            'source_ir': 'source_ir.json', 'output_dir': 'reading'}})
        self.assertTrue(response['success'], response)
        result = json.loads(response['content_items'][0]['text'])
        for part in result['parts']:
            output = host.invoke('reqpilot_read', part['read'])
            self.assertTrue(output['success'], output)
            value = json.loads(output['content_items'][0]['text'])
            self.assertNotEqual(value.get('status'), 'FULL_RESULT_SAVED')
            self.assertEqual(value['total_lines'], part['records'])
        verify(self.root / result['manifest_path'])

    def test_old_pinned_bundle_does_not_advertise_new_reader(self):
        plugin = self.root / 'old-plugin'
        (plugin / 'scripts').mkdir(parents=True)
        (plugin / 'scripts/reqpilot.py').write_text('')
        host = NativeLoopToolHost(workspace=self.root, plugin_root=plugin)
        self.assertNotIn('reading-packet', host.operations)
        result = json.loads(host.invoke('reqpilot_context', {})['content_items'][0]['text'])
        self.assertNotIn('reading-packet', result['batch_reading'])


if __name__ == '__main__':
    unittest.main()
