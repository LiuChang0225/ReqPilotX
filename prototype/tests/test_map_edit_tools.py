
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import json
import unittest

from tests import test_portable_map_ranges as fixtures


class MapEditTests(unittest.TestCase):
    setUp = fixtures.PortableMapRangeTests.setUp
    tearDown = fixtures.PortableMapRangeTests.tearDown
    run_tool = fixtures.PortableMapRangeTests.run_tool

    def test_cli_audit_replays_current_map_and_rejects_changed_decisions(self):
        from argparse import Namespace
        from map_audit_tools import audit_map
        self.edit(self.request(), 'audit-base')
        args=Namespace(map_dir=self.root/'audit-base',page=[1])
        report=audit_map(args)
        self.assertEqual(report['map_sha256'],json.loads((args.map_dir/'document-map.json').read_text())['artifact_sha256'])
        path=args.map_dir/'map-decisions.json'
        path.write_text(path.read_text()+'\n')
        with self.assertRaisesRegex(ValueError,'source input changed'):
            audit_map(args)

    def write(self, value):
        r = self.host.invoke('reqpilot_candidate', {'name': 'edit', 'value': value})
        self.assertTrue(r['success'], r)
        return json.loads(r['content_items'][0]['text'])['path']

    def edit(self, value, output, base=None):
        args = dict(source='source.pdf', source_ir='capture/source_ir.json', input=self.write(value), output_dir=output)
        if base: args['map_dir'] = base
        return self.run_tool('compile-map', **args)

    def request(self):
        index = next(i for i, n in enumerate(self.ir['nodes']) if n['source_node_id'] == self.block['source_node_id'])
        return {'schema_version': 'reqpilot-map-edit/v1', 'nodes': [
            {'node_id': 'doc', 'parent_id': None, 'kind': 'DOCUMENT'},
            {'node_id': 'first', 'parent_id': 'doc', 'kind': 'SECTION',
             'heading': {'source': {'source': index, 'quote': '1.1 First'}, 'label': '1.1'},
             'content': [{'source': index, 'start': 'A shall open.', 'end_before': '1.2 Second'}]},
            {'node_id': 'second', 'parent_id': 'doc', 'kind': 'SECTION',
             'heading': {'source': {'source': index, 'quote': '1.2 Second'}, 'label': '1.2'},
             'content': [{'source': index, 'start': 'B shall close.'}]}]}

    def test_compact_binding_and_local_revision_preserve_source_and_resume(self):
        source_before = (self.root/'source.pdf').read_bytes()
        self.edit(self.request(), 'v1')
        first_bytes = (self.root/'v1/document-map.json').read_bytes()
        m = json.loads(first_bytes); nodes = {n['node_id']: n for n in m['nodes']}
        self.assertEqual(nodes['first']['label'], '1.1')
        self.assertEqual(nodes['first']['title'], 'First')
        for c in nodes['first']['source_content'] + nodes['second']['source_content']:
            lo, hi = c['source_text_range']
            self.assertEqual(c['text'], self.block['raw_text'][lo:hi])
        patch = {'schema_version': 'reqpilot-map-edit/v1', 'nodes': [{'node_id': 'second', 'parent_id': 'first'}]}
        self.edit(patch, 'v2', 'v1')
        n2 = {n['node_id']: n for n in json.loads((self.root/'v2/document-map.json').read_text())['nodes']}
        self.assertEqual(n2['second']['parent_id'], 'first')
        self.assertEqual(n2['second']['source_content'], nodes['second']['source_content'])
        self.assertEqual(n2['first']['source_content'], nodes['first']['source_content'])
        self.assertEqual((self.root/'v1/document-map.json').read_bytes(), first_bytes)
        binding = json.loads((self.root/'v2/map-edit-binding.json').read_text())
        self.assertEqual(binding['base_map_sha256'], m['artifact_sha256'])
        self.assertEqual(binding['changed_node_ids'], ['second'])
        # Ordinary v2 compiler replay gives exactly the same Map; no new durable model.
        self.run_tool('compile-map', source='source.pdf', source_ir='capture/source_ir.json',
                      input='v2/map-decisions.json', output_dir='replay')
        self.assertEqual((self.root/'v2/document-map.json').read_bytes(), (self.root/'replay/document-map.json').read_bytes())
        self.assertEqual((self.root/'source.pdf').read_bytes(), source_before)

    def test_ambiguous_quotes_and_bad_edits_leave_previous_result_intact(self):
        self.edit(self.request(), 'v1')
        before = (self.root/'v1/document-map.json').read_bytes()
        req = self.request(); req['nodes'][1]['heading']['source']['quote'] = 'shall'
        with self.assertRaisesRegex(ValueError, 'exactly once'): self.edit(req, 'bad')
        with self.assertRaisesRegex(ValueError, 'parent does not resolve'):
            self.edit({'schema_version': 'reqpilot-map-edit/v1', 'remove_node_ids': ['doc']}, 'bad2', 'v1')
        with self.assertRaisesRegex(ValueError, 'heading replaces'):
            req = self.request(); req['nodes'][1]['title'] = 'changed'; self.edit(req, 'bad3')
        with self.assertRaisesRegex(ValueError, 'unknown fields'):
            self.edit({'schema_version': 'reqpilot-map-edit/v1', 'path': 'old.json', 'text': '{}'}, 'bad4')
        self.assertFalse(any((self.root/name).exists() for name in ['bad', 'bad2', 'bad3', 'bad4']))
        self.assertEqual((self.root/'v1/document-map.json').read_bytes(), before)

    def test_table_fragments_use_index_and_keep_real_rows(self):
        req = self.request()
        index = next(i for i, n in enumerate(self.ir['nodes']) if n['source_node_id'] == self.table['source_node_id'])
        req['logical_tables'] = [{'scope_id': 'data', 'owner_node_id': 'first', 'title': 'Data', 'role': 'PARAMETERS',
            'fragments': [{'source_node_id': index, 'row_range': [2, 6]}]}]
        self.edit(req, 'table-v1')
        cells = json.loads((self.root/'table-v1/table-previews.json').read_text())['data']['fragments'][0]
        self.assertEqual(cells['row_range'], [2, 6]); self.assertEqual(cells['row_count'], 4)
        self.edit({'schema_version': 'reqpilot-map-edit/v1', 'logical_tables': [{'scope_id': 'data', 'owner_node_id': 'second'}]}, 'table-v2', 'table-v1')
        after = json.loads((self.root/'table-v2/table-previews.json').read_text())['data']['fragments'][0]
        self.assertEqual(after, cells)

    def test_missing_node_and_table_fields_identify_both_owners_without_new_output(self):
        self.edit(self.request(), 'saved')
        previous = (self.root/'saved/document-map.json').read_bytes()
        index = next(i for i, n in enumerate(self.ir['nodes']) if n['source_node_id'] == self.table['source_node_id'])
        patch = {'schema_version': 'reqpilot-map-edit/v1',
            'nodes': [{'node_id': 'control', 'parent_id': 'doc', 'kind': 'DOCUMENT_CONTROL'}],
            'logical_tables': [{'scope_id': 'control-table', 'owner_node_id': 'control', 'role': 'CONTROL',
                'fragments': [{'source_node_id': index, 'row_range': [0, 2]}]}]}
        with self.assertRaises(ValueError) as caught:
            self.edit(patch, 'bad-fields', 'saved')
        self.assertIn('nodes/control/title: required', str(caught.exception))
        self.assertIn('logical_tables/control-table/title: required', str(caught.exception))
        self.assertFalse((self.root/'bad-fields').exists())
        self.assertEqual((self.root/'saved/document-map.json').read_bytes(), previous)
        patch['nodes'][0]['title'] = 'Document control'
        patch['logical_tables'][0]['title'] = 'Control'
        self.edit(patch, 'corrected-fields', 'saved')
        self.assertEqual((self.root/'saved/document-map.json').read_bytes(), previous)

    def test_numbered_body_keeps_obligation_out_of_title_and_preserves_all_text(self):
        index=next(i for i,n in enumerate(self.ir['nodes']) if n['source_node_id']==self.block['source_node_id'])
        request={'schema_version':'reqpilot-map-edit/v1','nodes':[
            {'node_id':'doc','kind':'DOCUMENT','parent_id':None},
            {'node_id':'first','kind':'SECTION','parent_id':'doc',
             'numbered_body':{'source':{'source':index,'start':'1.1','end_before':'1.2'},'label':'1.1'}},
            {'node_id':'second','kind':'SECTION','parent_id':'doc',
             'numbered_body':{'source':{'source':index,'start':'1.2'},'label':'1.2'}}]}
        self.edit(request,'numbered')
        m=json.loads((self.root/'numbered/document-map.json').read_text())
        by={n['node_id']:n for n in m['nodes']}
        for name,label,obligation in [('first','1.1','A shall open.'),('second','1.2','B shall close.')]:
            node=by[name]
            self.assertEqual(node['label'],label)
            self.assertEqual(''.join(c['text'] for c in node['source_content'] if c['is_title']),label)
            self.assertIn(obligation,''.join(c['text'] for c in node['source_content'] if not c['is_title']))
        spans=[c for n in m['nodes'] for c in n['source_content']]
        reconstructed=''.join(c['text'] for c in sorted(spans,key=lambda c:c['source_text_range'][0]))
        self.assertEqual(reconstructed,self.block['raw_text'])
        request['nodes'][1]['heading']={'source':index}
        with self.assertRaisesRegex(ValueError,'numbered_body binds'):self.edit(request,'conflicting')

    def test_changed_base_capture_cannot_silently_rebind_index(self):
        self.edit(self.request(), 'v1')
        # Direct CLI adapter is also strict even without the native host hash ledger.
        import sys
        from tests.test_portable_map_ranges import REPO
        sys.path.insert(0, str(REPO/'plugins/reqpilot/scripts'))
        from map_edit_tools import bind_map_edit
        from app.domain.extraction.source_ir import SourceIRSnapshot
        ir = copy.deepcopy(self.ir); ir['nodes'][0], ir['nodes'][1] = ir['nodes'][1], ir['nodes'][0]
        ir['nodes'][0]['source_order'], ir['nodes'][1]['source_order'] = ir['nodes'][1]['source_order'], ir['nodes'][0]['source_order']
        with self.assertRaisesRegex(ValueError, 'different capture'):
            bind_map_edit(SourceIRSnapshot.model_validate(ir), {'schema_version':'reqpilot-map-edit/v1'}, self.root/'source.pdf', self.root/'v1')

    def test_numbered_body_can_continue_in_another_captured_block(self):
        index=next(i for i,n in enumerate(self.ir['nodes']) if n['source_node_id']==self.block['source_node_id'])
        tail=next(i for i,n in enumerate(self.ir['nodes']) if n['raw_text'].strip()=='Body A')
        request={'schema_version':'reqpilot-map-edit/v1','nodes':[
            {'node_id':'doc','kind':'DOCUMENT','parent_id':None},
            {'node_id':'one','kind':'SECTION','parent_id':'doc','numbered_body':{
                'source':{'source':index,'start':'1.1','end_before':'1.2'},'label':'1.1','continuations':[{'source':tail}]}}]}
        self.edit(request,'continued')
        node=next(n for n in json.loads((self.root/'continued/document-map.json').read_text())['nodes'] if n['node_id']=='one')
        self.assertEqual(node['pages'],[1,2])
        self.assertTrue(any(c['text'].strip()=='Body A' and not c['is_title'] for c in node['source_content']))


if __name__ == '__main__': unittest.main()
