"""Version-bound item packets and actual original image payloads, no model."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from test_task_change_contracts import ChangeFixture
import task_change_reading as reader
import task_change_contracts as contract
from test_portable import write
from app.application.agent_workflow.source_item_decomposition import compile_source_item_decomposition
from app.application.agent_workflow.source_item_visual import compile_source_item_visual_readings, build_source_item_image_scope_packet


def size(value): return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


class TaskChangeReadingTests(ChangeFixture):
    def pins(self):
        return {'expected_source_sha256': self.baseline.source_sha256, 'expected_map_sha256': self.document_map.artifact_sha256,
            'expected_baseline_sha256': self.baseline.artifact_sha256}

    def read(self, source=None, **kwargs):
        return reader.source_item_read(self.items, source_item_id=source or self.parent.source_display_id, **self.pins(), **kwargs)

    def test_ordinary_parent_is_complete_in_one_read_with_exact_packet_ranges(self):
        view = self.read()
        self.assertTrue(view['complete']); self.assertLessEqual(size(view), 12000)
        self.assertEqual(view['identity']['source_form'], self.parent.source_form.value)
        self.assertEqual(''.join(s['text'] for s in view['source']), ''.join(b.text for b in self.parent.text_bindings))
        self.assertEqual(view['source_item_id'], self.parent.source_record_id)
        self.assertEqual(view['identity']['original_number'], '1')
        packet = self.read(part='decomposition_packet')['data']
        self.assertEqual(packet['segments'], view['source'])
        template = self.read(part='scope')['data']
        self.assertEqual(template['candidate_template']['items'][0]['item_key'], 'existing-' + self.parent.source_display_id.lower())
        self.assertEqual(view['semantic_acceptance'], 'NOT_CLAIMED')
        number=self.read(part='numbering')['data']
        self.assertEqual(number['source_segments'][0]['text'],view['source'][0]['text'])
        self.assertEqual(number['source_segments'][0]['source_number_ref'],{'kind':'ITEM_SOURCE','segment_index':0})
        self.assertTrue(number['heading_sources'][0]['text'].startswith('1 '))
        self.assertEqual(number['heading_sources'][0]['source_number_ref'],{'kind':'ITEM_HEADING','heading_ref':0})
        self.assertEqual(view['numbering_review']['read_sources']['arguments']['part'],'numbering')

    def test_table_retains_full_cells_shared_headers_and_exact_embedded_relation(self):
        view = self.read(self.table_item.source_display_id)
        self.assertEqual(view['identity']['source_form'], 'TABLE_RECORD')
        self.assertEqual([s['text'] for s in view['source']], ['Response', 'Apply 12 V at 23 C.', 'Open <= 30 ms'])
        self.assertEqual([s['text'] for s in view['shared_context']], ['Test', 'Method and conditions', 'Criterion'])
        image = next(i for i in view['images'] if i['relation'] == 'TABLE_EMBEDDED_IMAGE')
        self.assertEqual(image['table_source_item_id'], self.table_item.source_record_id)
        self.assertTrue(image['cell_ids']); self.assertEqual(image['read']['reader'], 'source_item_image')
        payload = reader.source_item_image(self.items, source_item_id=self.table_item.source_display_id,
            image_anchor_id=image['anchor_id'], source_path=self.source, image_dir=self.root / 'images', **self.pins())
        self.assertEqual(payload['mime_type'], 'image/png')
        self.assertEqual(payload['sha256'], sha(Path(payload['path'])))
        self.assertEqual((payload['width'], payload['height']), (100, 21))
        self.assertEqual(payload['bbox'], image['bbox'])
        self.assertEqual(payload['relation'], 'TABLE_EMBEDDED_IMAGE')
        self.assertEqual(payload['source_item_id'], self.table_item.source_record_id)

    def test_annotation_and_decomposition_are_current_and_separate_from_verbatim_source(self):
        request = self.decomposition_request()
        bound = contract.bind_decomposition_decisions(request, self.baseline, table_previews=self.tables, authorization_request='Split independent clauses.')
        decomposition = compile_source_item_decomposition(self.baseline, bound['decisions'], table_previews=self.tables,
            authorized_source_item_ids=bound['authorization']['source_item_ids'])
        view = self.read(decomposition=decomposition)
        self.assertEqual(len(view['decomposition']['children']), 2)
        self.assertEqual(''.join(s['text'] for s in view['source']), self.parent.text_bindings[0].text)
        packet = build_source_item_image_scope_packet(self.document_map, node_id='s6', baseline=self.baseline, table_previews=self.tables)
        relation = packet['table_image_relations'][0]
        annotations = compile_source_item_visual_readings(self.document_map, self.baseline, [{'source_item_id': self.table_item.source_record_id,
            'image_anchor_ids': [relation['image_binding']['anchor_id']], 'text': 'Agent interpretation only.', 'association_reason': 'Actual row association.'}])
        view = self.read(self.table_item.source_display_id, annotations=annotations)
        self.assertEqual(view['annotations']['visual_interpretation']['text'], 'Agent interpretation only.')
        self.assertNotIn('Agent interpretation only.', ''.join(s['text'] for s in view['source']))
        bad = deepcopy(annotations); bad['baseline_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'exact baseline'):
            self.read(self.table_item.source_display_id, annotations=bad)

    def test_large_selected_metadata_is_lossless_and_budgeted(self):
        # Real compiled scope metadata is larger than 2k; streamed chunks must
        # reconstruct its exact scope template, including keys and local ranges.
        whole = self.read(part='scope', max_chars=100000)['data']
        first = self.read(part='scope', max_chars=2048)
        self.assertEqual(first['encoding'], 'JSON_TEXT')
        chunks = [first['text']]; view = first
        while view['next_read']:
            arguments = view['next_read']['arguments']
            view = reader.source_item_read(self.items, **self.pins(), **arguments)
            self.assertLessEqual(size(view), 2048); chunks.append(view['text'])
        self.assertEqual(json.loads(''.join(chunks)), whole)
        index = self.read(max_chars=2048)
        self.assertLessEqual(size(index), 2048)
        self.assertEqual(index['completeness_scope'], 'section index only; follow content_complete=false reads')

    def test_bounded_source_and_nonrequirement_reading_have_no_fake_decomposition(self):
        facts = []; offset = 0
        while True:
            view = self.read(self.table_item.source_display_id, part='source', limit=1, max_chars=2048, offset=offset)
            self.assertLessEqual(size(view), 2048); facts.extend(view['entries'])
            if not view['next_read']: break
            offset = view['next_read']['arguments']['offset']
        self.assertEqual(len(facts), 3)
        note = next(r for r in self.baseline.records if r.requirementness.value == 'NON_REQUIREMENT')
        view = self.read(note.source_display_id)
        self.assertFalse(view['decomposition']['packet_available'])
        self.assertNotIn('decomposition_packet', view['reads'])
        self.assertTrue(view['source'])
        with self.assertRaises(ValueError): self.read(note.source_display_id, part='decomposition_packet')

    def test_wrong_task_version_foreign_image_and_cache_tamper_fail(self):
        before = {p: sha(p) for p in self.items.iterdir() if p.is_file()}
        for key in self.pins():
            pins = self.pins(); pins[key] = 'f' * 64
            with self.assertRaisesRegex(ValueError, 'selected'):
                reader.source_item_read(self.items, source_item_id=self.parent.source_display_id, **pins)
        image = next(i for i in self.read(self.table_item.source_display_id)['images'] if i['relation'] == 'TABLE_EMBEDDED_IMAGE')
        with self.assertRaisesRegex(ValueError, 'owning Map node'):
            reader.source_item_image(self.items, source_item_id=self.parent.source_display_id, image_anchor_id=image['anchor_id'],
                source_path=self.source, image_dir=self.root / 'images', **self.pins())
        result = reader.source_item_image(self.items, source_item_id=self.table_item.source_display_id, image_anchor_id=image['anchor_id'],
            source_path=self.source, image_dir=self.root / 'images', **self.pins())
        Path(result['path']).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'cache payload changed'):
            reader.source_item_image(self.items, source_item_id=self.table_item.source_display_id, image_anchor_id=image['anchor_id'],
                source_path=self.source, image_dir=self.root / 'images', **self.pins())
        self.assertEqual(before, {p: sha(p) for p in self.items.iterdir() if p.is_file()})

    def test_pre_src_image_scope_keeps_standalone_and_table_images_distinct(self):
        pins = self.pins(); pins.pop('expected_baseline_sha256')
        view = reader.image_scope_read(self.mapping, node_id='s6', baseline_dir=self.items,
            expected_baseline_sha256=self.baseline.artifact_sha256, **pins)
        standalone = [e for e in view['entries'] if e['kind'] == 'images']
        embedded = [e for e in view['entries'] if e['kind'] == 'table_image_relations']
        self.assertEqual(len(standalone), 2); self.assertEqual(len(embedded), 1)
        result = reader.image_scope_image(self.mapping, node_id='s6', image_anchor_id=standalone[0]['anchor_id'],
            baseline_dir=self.items, expected_baseline_sha256=self.baseline.artifact_sha256,
            source_path=self.source, image_dir=self.root / 'images', **pins)
        self.assertNotIn('source_item_id', result)
        self.assertEqual(result['relation'], 'STANDALONE_IMAGE_CANDIDATE')
        self.assertEqual((result['width'], result['height']), (160, 80))
        self.assertEqual(result['sha256'], sha(Path(result['path'])))
        with self.assertRaisesRegex(ValueError, 'table'):
            reader.image_scope_read(self.mapping, node_id='s6', **pins)


if __name__ == '__main__': unittest.main()
