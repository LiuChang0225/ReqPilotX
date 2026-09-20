"""Actual task-selected reads, executable public continuations and fixed-package refusal."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_task_change_contracts import ChangeFixture, ROOT
from test_portable import item_decisions, write
from task_service import TaskService, ReadRequest
from task_bridge import TaskBridge
from app.application.agent_workflow import source_requirement_baseline as core

ENTRY = ROOT / 'plugins/reqpilot/scripts/reqpilot.py'


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def links(value):
    if isinstance(value, list):
        return [found for item in value for found in links(item)]
    if isinstance(value, dict):
        if value.get('tool') == 'source_read':
            return [value]
        return [found for item in value.values() for found in links(item)]
    return []


class TaskItemViewsTests(ChangeFixture):
    def setUp(self):
        super().setUp()
        self.service = TaskService(self.root, ENTRY, ROOT)
        opened = self.service.invoke('open', {'mode': 'create', 'source': str(self.source),
            'goal': 'Read the original in order; split independently testable obligations where needed.', 'creation_key': 'item-reading'})
        self.task = opened['task_id']; self.task_root = Path(opened['task_root'])
        self.apply('prepare')
        mapping = json.loads((self.root / 'map-input.json').read_text()); mapping.pop('source_sha256')
        mapping['schema_version'] = 'reqpilot-map-edit/v1'
        mapped = self.apply('map', decisions=mapping)
        decisions = item_decisions(Path(mapped['selected']['MAP']['output_dir']))
        for key in ('schema_version', 'source_sha256', 'document_map_artifact_sha256'): decisions.pop(key, None)
        state = self.apply('items', decisions=decisions)
        self.items = Path(state['selected']['ITEMS']['output_dir'])
        self.document_map = core.DocumentMapArtifactV1.model_validate(json.loads((self.items / 'document-map.json').read_text()))
        self.baseline = core.SourceRequirementBaselineArtifactV1.model_validate(json.loads((self.items / 'source-requirement-baseline.json').read_text()))
        self.tables = json.loads((self.items / 'table-previews.json').read_text())
        self.parent = next(r for r in self.baseline.records if r.node_id == 's1')
        self.table_item = next(r for r in self.baseline.records if r.table_fragments)

    def apply(self, action, **extra):
        revision = self.service.invoke('inspect', {'task_id': self.task})['revision']
        value = self.service.invoke(action, {'task_id': self.task, 'expected_revision': revision, **extra})
        self.assertEqual(value['operation_state'], 'COMMITTED', value)
        return value

    def read(self, **extra):
        value = self.service.invoke('read', {'task_id': self.task, **extra})
        self.assertEqual(value['status'], 'READ')
        return value

    def test_public_link_graph_and_every_continuation_are_directly_callable(self):
        seeds = [{'task_id': self.task, 'view': 'items', 'limit': 1, 'max_chars': 2048},
            {'task_id': self.task, 'view': 'item', 'source_item_id': self.parent.source_display_id, 'max_chars': 2048},
            {'task_id': self.task, 'view': 'image_scope', 'limit': 1, 'max_chars': 2048}]
        seen = set(); queue = seeds[:]; views = set(); continuations = 0
        while queue:
            request = queue.pop(0); key = json.dumps(request, sort_keys=True)
            if key in seen: continue
            seen.add(key); self.assertLess(len(seen), 180, 'Unexpected circular or unbounded read graph')
            ReadRequest.model_validate(request)
            response = self.service.invoke('read', request); self.assertEqual(response['status'], 'READ')
            if 'budget_scope' in response:
                self.assertEqual(response['budget_scope'], 'READER_PAYLOAD_BEFORE_TASK_ENVELOPE')
            views.add(request['view'])
            for link in links(response):
                self.assertEqual(link['request']['task_id'], self.task)
                self.assertEqual(link['request']['expected_map_sha256'], response.get('document_map_artifact_sha256') or
                    self.document_map.artifact_sha256)
                self.assertEqual(link['request']['expected_baseline_sha256'], self.baseline.artifact_sha256)
                self.assertFalse(set(link['request']) & {'source_path', 'bundle_dir', 'map_dir', 'baseline_dir', 'image_dir'})
                if link['request'].get('offset', 0): continuations += 1
                queue.append(link['request'])
        self.assertTrue({'items', 'item', 'item_image', 'image_scope', 'image_scope_image'} <= views)
        self.assertGreater(continuations, 5)
        # Exercise the direct read method with the exact state/request shape.
        _, state = self.service.state(self.task)
        result = self.service.read(state, ReadRequest(task_id=self.task, view='items', limit=1))
        self.assertEqual(result['items'][0]['source_number'],'1')
        self.assertEqual(result['items'][0]['source_number_role'],'EXACT_SOURCE_NUMBER')
        self.assertEqual(result['items'][0]['read']['tool'], 'source_read')

    def test_selected_overlay_outputs_are_visible_without_copying_them_into_items(self):
        scope = self.read(view='image_scope', node_id='s6')
        embedded = next(e for e in scope['entries'] if e['kind'] == 'table_image_relations')
        annotated = self.apply('annotations', changes={'visual_readings': [{'source_item_id': self.table_item.source_display_id,
            'image_anchor_ids': [embedded['image_binding']['anchor_id']], 'text': 'The small diagram is contained in the response row.',
            'association_reason': 'The measured source image placement belongs to this table row.'}]})
        self.assertFalse((self.items / 'image-annotations.json').exists())
        annotation_path = Path(annotated['selected']['ANNOTATIONS']['path']); self.assertTrue(annotation_path.is_file())
        decomposed = self.apply('decomposition', changes=self.decomposition_request())
        self.assertFalse((self.items / 'source-item-decomposition.json').exists())
        self.assertTrue(Path(decomposed['selected']['DECOMPOSITION']['path']).is_file())
        table = self.read(view='item', source_item_id=self.table_item.source_display_id)
        self.assertEqual(table['annotations']['visual_interpretation']['text'], 'The small diagram is contained in the response row.')
        self.assertNotIn('contained in the response row', ''.join(s['text'] for s in table['source']))
        parent = self.read(view='item', source_item_id=self.parent.source_display_id)
        self.assertEqual(len(parent['decomposition']['children']), 2)
        self.assertEqual(parent['baseline_sha256'], self.baseline.artifact_sha256)
        self.assertEqual(parent['decomposition']['review_scope']['remaining_source_item_ids'], [])

    def test_item_and_scope_image_links_return_exact_original_png(self):
        table = self.read(view='item', source_item_id=self.table_item.source_display_id)
        embedded = next(i for i in table['images'] if i['relation'] == 'TABLE_EMBEDDED_IMAGE')
        loaded = self.service.invoke('read', embedded['read']['request'])
        image = loaded['image']; bridge = TaskBridge(self.root, ENTRY, project_root=ROOT)
        self.assertEqual(image['source_item_id'], self.table_item.source_record_id)
        self.assertEqual(image['image_anchor_id'], embedded['anchor_id']); self.assertEqual(image['bbox'], embedded['bbox'])
        self.assertEqual((image['width'], image['height']), (100, 21)); self.assertEqual(image['mime_type'], 'image/png')
        self.assertEqual(bridge.image_bytes(image)[:8], b'\x89PNG\r\n\x1a\n')
        self.assertTrue(Path(image['path']).is_relative_to(self.task_root / 'reading' / 'item-image-cache'))
        scope = self.read(view='image_scope', node_id='s6')
        standalone = next(e for e in scope['entries'] if e['kind'] == 'images')
        image = self.service.invoke('read', standalone['read']['request'])['image']
        self.assertEqual((image['width'], image['height']), (160, 80))
        self.assertNotIn('source_item_id', image)
        self.assertEqual(image['sha256'], hashlib.sha256(bridge.image_bytes(image)).hexdigest())

    def test_reassembled_json_stream_preserves_directly_callable_public_image_links(self):
        view = self.read(view='image_scope', node_id='s6', max_chars=2048)
        self.assertEqual(view['encoding'], 'JSON_TEXT')
        chunks = []
        while True:
            chunks.append(view['text'])
            if not view['next_read']: break
            view = self.service.invoke('read', view['next_read']['request'])
        decoded = json.loads(''.join(chunks))
        found = links(decoded)
        self.assertEqual(len(found), 3)
        for link in found:
            self.assertEqual(link['tool'], 'source_read')
            self.assertEqual(link['request']['expected_baseline_sha256'], self.baseline.artifact_sha256)
            image = self.service.invoke('read', link['request'])['image']
            self.assertEqual(image['image_anchor_id'], link['request']['image_anchor_id'])
        # source_item_read JSON objects use the same pre-serialization callback.
        # The existing scope template contains no internal read links; its data
        # must still reconstruct exactly under the public continuation pins.
        whole = self.read(view='item', source_item_id=self.parent.source_display_id, part='scope', max_chars=24000)['data']
        view = self.read(view='item', source_item_id=self.parent.source_display_id, part='scope', max_chars=2048)
        chunks = []
        while True:
            chunks.append(view['text'])
            if not view['next_read']: break
            view = self.service.invoke('read', view['next_read']['request'])
        self.assertEqual(json.loads(''.join(chunks)), whole)

    def test_links_remain_pinned_and_old_baseline_cannot_be_silently_read_as_current(self):
        item_link = self.read(view='items')['items'][0]['read']['request']
        scope = self.read(view='image_scope', node_id='s6')
        choices = {'scopes': [{'node_id': 's6', 'items': [{'item_key': 'image-' + str(i), 'display_label': 'Original dimensional drawing',
            'image_anchor_ids': [e['anchor_id']], 'requirementness': 'REQUIREMENT', 'requirement_kinds': ['CONSTRAINT'],
            'confidence': 0.9, 'decision_summary': 'The drawing states a dimensional limit.', 'reason_codes': ['source_drawing']}
            for i,e in enumerate(scope['entries']) if e['kind'] == 'images']}]}
        self.apply('images', changes=choices)
        with self.assertRaisesRegex(ValueError, 'BASELINE_VERSION_CONFLICT'):
            self.service.invoke('read', item_link)
        fresh = self.read(view='items')
        self.assertNotEqual(fresh['baseline_sha256'], self.baseline.artifact_sha256)

    def test_fixed_old_package_refuses_new_capability_before_any_process_starts(self):
        # A minimal sealed legacy capability is sufficient: no child process is
        # allowed to run, so no runtime/compiler source is copied into this fixture.
        package = self.root / 'old-package'; scripts = package / 'scripts'; scripts.mkdir(parents=True)
        old_entry = scripts / 'reqpilot.py'; old_entry.write_text('raise RuntimeError("must not execute")\n')
        (scripts / 'task_service.py').write_text('# Legacy typed task API marker.\n')
        receipt = {'plugin_files': [{'path': str(p.relative_to(package)), 'sha256': sha(p)} for p in scripts.iterdir()], 'runtime_files': []}
        receipt['bundle_sha256'] = hashlib.sha256(json.dumps(receipt, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        write(package / 'build-receipt.json', receipt)
        legacy = TaskService(self.root, old_entry)
        opened = legacy.invoke('open', {'mode': 'create', 'source': str(self.source), 'goal': 'Read this original.', 'creation_key': 'legacy-fixed'})
        context = self.service.state(opened['task_id'])[1]['context']
        self.assertEqual(context['runtime_ref']['kind'], 'BUNDLED')
        self.assertEqual(context['runtime_ref']['entrypoint'], str(old_entry.resolve()))
        bridge = TaskBridge(self.root, ENTRY, project_root=ROOT)
        with patch('task_bridge.subprocess.Popen') as start:
            for action, request in [('describe', {'action':'items'}),('read', {'view':'scope','part':'template'}),
                    ('items', {'decisions':{'text_decisions':[{'scope_id':'text:s1','items':[{'label':'source'}]}]}}),
                    ('items', {'decisions':{'table_decisions':[{'logical_table_scope_id':'t','records':[{'label':'source'}]}]}}),
                    ('annotations', {}), ('decomposition', {}), ('images', {}), ('revise', {}),
                    *[('read', {'view': view}) for view in ('items', 'item', 'item_image', 'image_scope', 'image_scope_image')]]:
                result = bridge.invoke(action, {'task_id': opened['task_id'], **request})
                self.assertEqual(result['error_code'], 'UNSUPPORTED_CAPABILITY', result)
                self.assertEqual(result['status'], 'FAILED')
            start.assert_not_called()
        self.assertEqual(sha(old_entry), context['runtime_ref']['entrypoint_sha256'])


if __name__ == '__main__': unittest.main()
