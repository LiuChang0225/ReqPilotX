"""Typed adapters exercise the unchanged compilers with an invented source PDF."""
from argparse import Namespace
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import unittest

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'prototype'),str(ROOT/'plugins/reqpilot/scripts')]
from test_portable import make_fixture,item_decisions,write
import artifact_tools
from task_contracts import MapEditRequest,ItemDecisionsRequest,TextItemInput,bind_item_decisions
from app.application.agent_workflow import source_requirement_baseline as core


class TaskContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source,self.capture,self.decisions=make_fixture(self.root/'fixture',numbered_targets=True)
        self.mapping=self.root/'map'
        payload={key:value for key,value in self.decisions.items() if key not in {'source_sha256','schema_version'}}
        typed=MapEditRequest.model_validate(payload)
        write(self.root/'map-input.json',typed.to_payload())
        artifact_tools.compile_map(Namespace(source=self.source,source_ir=self.capture/'source_ir.json',input=self.root/'map-input.json',output_dir=self.mapping,map_dir=None))
        self.map_value=json.loads((self.mapping/'document-map.json').read_text())
        self.scopes=json.loads((self.mapping/'scope-requests.json').read_text())

    def choices(self):
        value=item_decisions(self.mapping)
        return {key:item for key,item in value.items() if key not in {'source_sha256','document_map_artifact_sha256'}}

    def test_map_edit_serialization_preserves_null_root_and_patch_omission(self):
        value=MapEditRequest.model_validate({'nodes':[{'node_id':'doc','parent_id':None,'kind':'DOCUMENT'},{'node_id':'s1','content':[]}]}).to_payload()
        self.assertIsNone(value['nodes'][0]['parent_id'])
        self.assertNotIn('parent_id',value['nodes'][1])
        self.assertEqual(value['nodes'][1]['content'],[])
        self.assertNotIn('source_dispositions',value)
        self.assertEqual(value['schema_version'],'reqpilot-map-edit/v1')
        self.assertNotIn('heading',value['nodes'][1])
        self.assertEqual(MapEditRequest.model_validate({'source_dispositions':[]}).to_payload()['source_dispositions'],[])
        self.assertIsNone(MapEditRequest.model_validate({'logical_tables':[{'scope_id':'t','agent_display_label':None}]}).to_payload()['logical_tables'][0]['agent_display_label'])
        schema=MapEditRequest.model_json_schema()
        self.assertNotIn('default',schema['$defs']['MapSourceSelector']['properties']['quote'])

    def test_typed_numbered_body_quote_edit_reuses_source_binding_and_keeps_other_nodes(self):
        snapshot=json.loads((self.capture/'source_ir.json').read_text())
        ref=next(index for index,node in enumerate(snapshot['nodes']) if node['raw_text'].startswith('1.1 The valve'))
        edit={'nodes':[{'node_id':'s1','content':[]},
            {'node_id':'open','parent_id':'s1','kind':'SECTION','numbered_body':{'source':{'source':ref,'start':'1.1','end_before':'1.2'},'label':'1.1'}},
            {'node_id':'close','parent_id':'s1','kind':'SECTION','numbered_body':{'source':{'source':ref,'start':'1.2'},'label':'1.2'}}]}
        write(self.root/'edit.json',MapEditRequest.model_validate(edit).to_payload())
        output=self.root/'updated'
        artifact_tools.compile_map(Namespace(source=self.source,source_ir=self.capture/'source_ir.json',input=self.root/'edit.json',output_dir=output,map_dir=self.mapping))
        result=json.loads((output/'document-map.json').read_text())
        nodes={node['node_id']:node for node in result['nodes']}
        self.assertEqual(nodes['open']['label'],'1.1')
        self.assertIn('open within 30 ms',' '.join(c['text'] for c in nodes['open']['source_content'] if not c['is_title']))
        self.assertIn('close within 40 ms',' '.join(c['text'] for c in nodes['close']['source_content'] if not c['is_title']))
        self.assertTrue({'s2','s3','s4','s5','s6'}<=nodes.keys())

    def test_map_typed_surface_rejects_conflicting_shorthands_bool_refs_and_unknown_fields(self):
        cases=[{'source_sha256':'a'*64},
            {'nodes':[{'node_id':'x','kind':'CLAUSE'}]},
            {'nodes':[{'node_id':'x','content':[True]}]},
            {'nodes':[{'node_id':'x','heading':{'source':1},'title':'invented'}]},
            {'nodes':[{'node_id':'x','numbered_body':{'source':1,'label':'1'},'content':[]}]},
            {'nodes':[{'node_id':'x','content':[{'source':0,'quote':'one','start':'one'}]}]},
            {'nodes':[{'node_id':'x','heading':None}]},
            {'nodes':[{'node_id':'x','unset':['parent_id']}]},
            {'logical_tables':[{'scope_id':'t','fragments':[{'source_node_id':0,'row_range':[3,3]}]}]}]
        for value in cases:
            with self.subTest(value=value),self.assertRaises(ValueError):MapEditRequest.model_validate(value)

    def test_item_selectors_remove_hash_arithmetic_without_changing_source_identity(self):
        legacy=item_decisions(self.mapping)
        typed=self.choices();scope_by_id={s['scope_id']:s for s in self.scopes['text_requests']}
        for scope in typed['text_decisions']:
            contents=scope_by_id[scope['scope_id']]['source_content']
            index={part['content_id']:i for i,part in enumerate(contents)}
            for item in scope['items']:
                item['text_spans']=[{'content_ref':index[span['content_id']],'quote':contents[index[span['content_id']]]['text']} for span in item['text_spans']]
            for part in scope['structure_only_spans']:
                part['text_span']={'content_ref':index[part['text_span']['content_id']]}
        result=bind_item_decisions(typed,self.map_value,self.scopes)
        self.assertEqual(result['source_sha256'],self.map_value['source_sha256'])
        self.assertNotIn('request_sha256',result['text_decisions'][0])
        for label,value in [('legacy',legacy),('typed',result)]:
            inputs=self.root/(label+'.json');write(inputs,value)
            artifact_tools.compile_items(Namespace(map_dir=self.mapping,input=inputs,baseline_dir=None,output_dir=self.root/label))
        first=json.loads((self.root/'legacy/source-requirement-baseline.json').read_text())
        second=json.loads((self.root/'typed/source-requirement-baseline.json').read_text())
        self.assertEqual(first,second)
        self.assertEqual(len(second['records']),8)

    def test_initial_keys_are_optional_but_explicit_values_and_revisions_stay_strict(self):
        schema=TextItemInput.model_json_schema()
        self.assertEqual(schema['properties']['source_form']['enum'],['NUMBERED_CLAUSE','NATIVE_LIST','PARAGRAPH'])
        choices=self.choices();choices['text_decisions'][1]['items'][0].pop('item_key')
        self.assertNotIn('item_key',schema['required'])
        ItemDecisionsRequest.model_validate(choices)
        omitted=bind_item_decisions(choices,self.map_value,self.scopes)
        with self.assertRaisesRegex(ValueError,'REVISION_ITEM_KEY_REQUIRED'):
            bind_item_decisions(choices,self.map_value,self.scopes,allow_generated_keys=False)
        choices['text_decisions'][1]['items'][0]['item_key']=None
        self.assertEqual(bind_item_decisions(choices,self.map_value,self.scopes),omitted)
        for value in ('TABLE_RECORD','IMAGE'):
            choices=self.choices();choices['text_decisions'][1]['items'][0]['source_form']=value
            with self.assertRaises(ValueError):ItemDecisionsRequest.model_validate(choices)
        choices=self.choices();choices['table_decisions'][0]['records'][0].pop('record_key')
        ItemDecisionsRequest.model_validate(choices)
        with self.assertRaisesRegex(ValueError,'REVISION_RECORD_KEY_REQUIRED'):
            bind_item_decisions(choices,self.map_value,self.scopes,allow_generated_keys=False)
        with self.assertRaises(ValueError):ItemDecisionsRequest.model_validate({**self.choices(),'source_sha256':'a'*64})

    def test_program_keys_follow_resolved_source_not_transport_spelling_or_classification(self):
        exact=self.choices()
        for scope in exact['text_decisions']:
            for item in scope['items']:item.pop('item_key')
        for scope in exact['table_decisions']:
            for item in scope['records']:item.pop('record_key')
        short=deepcopy(exact)
        for scope in short['text_decisions']:
            request=next(r for r in self.scopes['text_requests'] if r['scope_id']==scope['scope_id'])
            contents=request['source_content'];refs={c['content_id']:i for i,c in enumerate(contents)}
            for item in scope['items']:
                item['text_spans']=[{'content_ref':refs[s['content_id']], 'quote':contents[refs[s['content_id']]]['text'][s['start_offset']:s['end_offset']]} for s in item['text_spans']]
        for scope in short['table_decisions']:
            request=next(r for r in self.scopes['table_requests'] if r['logical_table_scope_id']==scope['logical_table_scope_id'])
            refs={f['fragment_id']:i for i,f in enumerate(request['fragments'])}
            for item in scope['records']:
                for group in item['row_groups']:group['fragment_ref']=refs[group.pop('fragment_id')]
        a=bind_item_decisions(exact,self.map_value,self.scopes)
        b=bind_item_decisions(short,self.map_value,self.scopes)
        self.assertEqual(a,b)
        changed=deepcopy(exact);changed['text_decisions'][1]['items'][0]['decision_summary']='A different explanation of the same source.'
        c=bind_item_decisions(changed,self.map_value,self.scopes)
        self.assertEqual(a['text_decisions'][1]['items'][0]['item_key'],c['text_decisions'][1]['items'][0]['item_key'])
        for label,payload in [('auto-exact',a),('auto-short',b)]:
            path=self.root/(label+'.json');write(path,payload)
            artifact_tools.compile_items(Namespace(map_dir=self.mapping,input=path,baseline_dir=None,output_dir=self.root/label))
        self.assertEqual(json.loads((self.root/'auto-exact/source-requirement-baseline.json').read_text()),json.loads((self.root/'auto-short/source-requirement-baseline.json').read_text()))

    def test_reason_code_conventions_normalize_without_touching_source_or_judgment(self):
        choices=self.choices();item=choices['text_decisions'][1]['items'][0]
        item['reason_codes']=['source-read','source_read','agent-choice']
        result=bind_item_decisions(choices,self.map_value,self.scopes)
        out=result['text_decisions'][1]['items'][0]
        self.assertEqual(out['reason_codes'],['agent_choice','source_read'])
        for key in ['text_spans','label','requirementness','decision_summary']:self.assertEqual(out[key],item[key])
        pattern=TextItemInput.model_json_schema()['properties']['reason_codes']['items']['pattern']
        self.assertIn('-',pattern)
        for bad in ['Human words','SOURCE_READ','中文原因','source/read']:
            item['reason_codes']=[bad]
            with self.subTest(bad=bad),self.assertRaises(ValueError):ItemDecisionsRequest.model_validate(choices)
        from task_change_contracts import ExistingTextItemInput,ExistingTableRecordInput
        self.assertIn('item_key',ExistingTextItemInput.model_json_schema()['required'])
        self.assertIn('record_key',ExistingTableRecordInput.model_json_schema()['required'])

    def test_table_refs_and_lossless_transport_forms_compile_the_same_source_items(self):
        exact=self.choices();short=deepcopy(exact)
        for scope in short['table_decisions']:
            request=next(r for r in self.scopes['table_requests'] if r['logical_table_scope_id']==scope['logical_table_scope_id'])
            refs={f['fragment_id']:i for i,f in enumerate(request['fragments'])}
            for record in scope['records']:
                record['item_key']=record.pop('record_key')
                for group in record['row_groups']:
                    group['fragment_ref']=refs[group.pop('fragment_id')]
                    group['row_start']=str(group['row_start']);group['row_end']=str(group['row_end'])
            for context in scope['context_row_groups']:
                group=context['row_group'];group['fragment_ref']=refs[group.pop('fragment_id')]
        self.assertEqual(bind_item_decisions(short,self.map_value,self.scopes),bind_item_decisions(exact,self.map_value,self.scopes))
        mapped=MapEditRequest.model_validate({'logical_tables':[{'scope_id':'t','fragments':[{'source_node_id':0,'row_range':['0','3']}]}]})
        self.assertEqual(mapped.logical_tables[0].fragments[0].row_range,(0,3))
        for bad_value in (True,'1.5','01','-1'):
            bad=deepcopy(short);bad['table_decisions'][0]['records'][0]['row_groups'][0]['row_start']=bad_value
            with self.subTest(value=bad_value),self.assertRaises(ValueError):ItemDecisionsRequest.model_validate(bad)
        bad=deepcopy(short);bad['table_decisions'][0]['records'][0]['record_key']='another'
        with self.assertRaisesRegex(ValueError,'Conflicting'):ItemDecisionsRequest.model_validate(bad)
        for field,value in [('fragment_ref',999),('row_end',999)]:
            bad=deepcopy(short);bad['table_decisions'][0]['records'][0]['row_groups'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):bind_item_decisions(bad,self.map_value,self.scopes)

    def test_ambiguous_quote_and_foreign_scope_are_not_silently_bound(self):
        choices=self.choices()
        target=next(scope for scope in choices['text_decisions'] if scope['scope_id']=='text:s1')
        request=next(scope for scope in self.scopes['text_requests'] if scope['scope_id']=='text:s1')
        idx=next(i for i,c in enumerate(request['source_content']) if not c['is_title'])
        target['items'][0]['text_spans']=[{'content_ref':idx,'quote':'at 12 V and 23 C.'}]
        with self.assertRaisesRegex(ValueError,'exactly once'):bind_item_decisions(choices,self.map_value,self.scopes)
        target['items'][0]['text_spans']=[{'content_ref':'another-scope-content'}]
        with self.assertRaisesRegex(ValueError,'selected scope'):bind_item_decisions(choices,self.map_value,self.scopes)
        scopes=deepcopy(self.scopes);raw=scopes['text_requests'][0]
        raw['document_map_artifact_sha256']='f'*64
        raw['request_sha256']=core.canonical_json_sha256({k:v for k,v in raw.items() if k!='request_sha256'})
        with self.assertRaisesRegex(ValueError,'different source or Map'):bind_item_decisions(self.choices(),self.map_value,scopes)

    def test_core_coverage_rules_still_run_and_only_setlike_metadata_is_normalized(self):
        choices=self.choices();target=next(scope for scope in choices['text_decisions'] if scope['items'])
        target['items'][0]['reason_codes']=['z_reason','a_reason','a_reason']
        parsed=ItemDecisionsRequest.model_validate(choices)
        actual=next(scope for scope in parsed.text_decisions if scope.items).items[0]
        self.assertEqual(actual.reason_codes,('a_reason','z_reason'))
        span=target['items'][0]['text_spans'][0];span['end_offset']-=10
        with self.assertRaisesRegex(ValueError,'COVERAGE|covered|coverage'):bind_item_decisions(choices,self.map_value,self.scopes)
