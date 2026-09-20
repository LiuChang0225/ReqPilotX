
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import unittest

from tests import test_portable_map_ranges as fixtures


class SourceAuthoringViewTests(unittest.TestCase):
    setUp = fixtures.PortableMapRangeTests.setUp
    tearDown = fixtures.PortableMapRangeTests.tearDown
    run_tool = fixtures.PortableMapRangeTests.run_tool

    def inspect(self, **args):
        return self.run_tool('inspect-source', source_ir='capture/source_ir.json', view='authoring', **args)

    def test_page_and_query_return_complete_text_and_direct_compilable_refs(self):
        view = self.inspect(page=[1], query='1.1 First')
        self.assertEqual(view['count'], 1)
        item = view['nodes'][0]
        self.assertEqual(item['raw_text'], self.block['raw_text'])
        self.assertEqual(item['source_node_id'], self.ir['nodes'][item['ref']]['source_node_id'])
        full = self.host.invoke('reqpilot_read', item['full_source_read'])
        self.assertTrue(full['success'], full)
        self.assertEqual(json.loads(full['content_items'][0]['text'])['raw_text'], item['raw_text'])
        ref = item['ref']
        candidate = {'schema_version': 'reqpilot-map-edit/v1', 'nodes': [
            {'node_id':'doc', 'kind':'DOCUMENT', 'parent_id':None},
            {'node_id':'clause','kind':'SECTION','parent_id':'doc',
             'heading':{'source':{'source':ref,'quote':'1.1 First'},'label':'1.1'},
             'content':[{'source':ref,'start':'A shall open.','end_before':'1.2 Second'}]}]}
        value = self.host.invoke('reqpilot_candidate', {'name':'authoring','value':candidate})
        path = json.loads(value['content_items'][0]['text'])['path']
        self.run_tool('compile-map',source='source.pdf',source_ir='capture/source_ir.json',input=path,output_dir='map')
        mapping=json.loads((self.root/'map/document-map.json').read_text())
        clause=next(n for n in mapping['nodes'] if n['node_id']=='clause')
        self.assertEqual(clause['label'],'1.1')
        self.assertIn('A shall open.', ''.join(c['text'] for c in clause['source_content'] if not c['is_title']))

    def test_table_query_keeps_parallel_text_candidates_and_actual_cell_fields(self):
        view=self.inspect(query='Limit A')
        self.assertTrue(any(n.get('table') for n in view['nodes']))
        self.assertTrue(any(not n.get('table') for n in view['nodes']))
        table=next(n for n in view['nodes'] if n.get('table'))
        self.assertEqual(table['table']['map_fragment']['source_node_id'],table['ref'])
        for got,want in zip(table['table']['cells'],self.table['table_payload']['cells'],strict=True):
            self.assertEqual(got, {key:want[key] for key in ['row','column','row_span','column_span','raw_text']})
        self.assertNotIn('owner',table)

    def test_pagination_and_source_index_selection_are_lossless(self):
        result=self.inspect(limit=2);nodes=list(result['nodes'])
        while result['next_read']:
            nxt=result['next_read'];result=self.run_tool(nxt['operation'],**nxt['arguments']);nodes+=result['nodes']
        self.assertEqual([n['ref'] for n in nodes],list(range(len(self.ir['nodes']))))
        selected=self.inspect(capture_index=[0,len(nodes)-1])
        self.assertEqual([n['ref'] for n in selected['nodes']],[0,len(nodes)-1])
        self.assertIsNone(self.inspect(query='This text does not occur')['next_read'])
        self.assertEqual(self.inspect(query='This text does not occur')['nodes'],[])

    def test_invalid_selection_fails_without_silently_selecting_all_sources(self):
        for args in [{'capture_index':[-1]}, {'capture_index':[999999]}, {'page':[999]},
                     {'source_id':['missing']}, {'limit':0}, {'query':'  '}]:
            with self.subTest(args=args), self.assertRaises(ValueError):self.inspect(**args)
        with self.assertRaisesRegex(ValueError,'require --view authoring'):
            self.run_tool('inspect-source',source_ir='capture/source_ir.json',query='First')

    def test_context_supplies_executable_read_and_native_evidence_directory(self):
        result=self.host.invoke('reqpilot_context',{})
        context=json.loads(result['content_items'][0]['text'])
        self.assertEqual(context['native_execution']['evidence_directory'],str(self.root.resolve()))
        command=context['source_authoring']['reads'][0]
        result=self.run_tool(command['operation'],**command['arguments'])
        self.assertEqual(result['count'],min(40,len(self.ir['nodes'])))

    def test_capture_accepts_explicit_pin_and_rejects_a_wrong_pin(self):
        # setUp already uses capture without a caller-copied SHA; host pins it.
        result=self.run_tool('capture',source='source.pdf',output_dir='explicit-capture',expected_sha256=self.ir['source_sha256'])
        self.assertEqual(result['source_sha256'],self.ir['source_sha256'])
        with self.assertRaises(ValueError):
            self.run_tool('capture',source='source.pdf',output_dir='wrong-capture',expected_sha256='0'*64)

    def test_limit_alias_preserves_results_and_conflicting_values_are_explicit(self):
        expected=self.inspect(limit=2)
        self.assertEqual(self.inspect(max_results=2),expected)
        self.assertEqual(self.inspect(limit=2,max_results=2),expected)
        with self.assertRaisesRegex(ValueError,'alias of limit'):
            self.inspect(limit=2,max_results=3)

    def test_unsupported_context_fields_are_reported_together_without_ignoring_intent(self):
        with self.assertRaises(ValueError) as error:
            self.inspect(query='First',context_before=2,context_after=3,max_results=20)
        self.assertIn('context_after, context_before',str(error.exception))
        self.assertIn('capture_index',str(error.exception))
        self.assertNotIn('Unsupported portable argument: max_results',str(error.exception))

    def test_missing_json_pointer_returns_executable_root_read_and_preserves_empty_keys(self):
        missing=self.host.invoke('reqpilot_read',{'path':'capture/source_ir.json','json_pointer':'/'})
        self.assertFalse(missing['success'])
        detail=json.loads(missing['content_items'][0]['text'])
        root=self.host.invoke('reqpilot_read',detail['next_read'])
        self.assertTrue(root['success'])
        # A real empty-name member retains JSON Pointer semantics.
        (self.root/'empty-key.json').write_text(json.dumps({'':'actual empty key'}))
        result=self.host.invoke('reqpilot_read',{'path':'empty-key.json','json_pointer':'/'})
        self.assertTrue(result['success'])
        self.assertEqual(result['content_items'][0]['text'],'actual empty key')


if __name__=='__main__':unittest.main()
