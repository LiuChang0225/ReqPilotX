"""Live request-model schema discovery, budgeted continuations and stage hints."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'plugins/reqpilot/scripts'),str(ROOT/'prototype')]
from task_service import REQUESTS
import task_schema


class TaskSchemaTests(unittest.TestCase):
    def collect(self,**request):
        budget=request.get('max_chars',12000);parts=[];response=task_schema.describe(**request);seen=set()
        digest=response['request_schema_sha256']
        while True:
            self.assertLessEqual(len(json.dumps(response,ensure_ascii=False,separators=(',',':'))),budget)
            self.assertEqual(response['request_schema_sha256'],digest)
            if 'data' in response:
                self.assertFalse(parts);return response['data']
            self.assertNotIn(response['offset'],seen);seen.add(response['offset']);parts.append(response['text'])
            if response['complete']:
                self.assertIsNone(response['next_read']);return json.loads(''.join(parts))
            next_read=response['next_read']
            self.assertEqual(next_read['reader'],'task_schema.describe')
            self.assertEqual(next_read['arguments']['offset'],response['offset']+response['returned_characters'])
            response=task_schema.describe(**next_read['arguments'])

    def test_catalog_and_all_action_fields_are_from_actual_request_models(self):
        catalog=self.collect(max_chars=2048)
        self.assertEqual([item['action'] for item in catalog['actions']],list(REQUESTS))
        for name,model in REQUESTS.items():
            with self.subTest(action=name):
                fields=self.collect(action=name,part='fields',max_chars=2048)['fields']
                schema=model.model_json_schema()
                self.assertEqual([field['name'] for field in fields],list(schema['properties']))
                self.assertEqual({field['name'] for field in fields if field['required']},set(schema.get('required',[])))
                self.assertTrue(all(field['type']!='unknown' and 'array<unknown>' not in field['type'] for field in fields))
                for field in fields:
                    for definition in field.get('definitions',[]):
                        actual=self.collect(action=name,part=definition['part'],max_chars=2048)
                        self.assertEqual(actual['type'],definition['type'])

    def test_schema_roundtrips_losslessly_with_real_budget_including_json_escaping(self):
        for action in ('map','items','revise','decomposition'):
            with self.subTest(action=action):
                self.assertEqual(self.collect(action=action,part='schema',max_chars=2048),REQUESTS[action].model_json_schema())

    def test_nested_definition_resolves_enums_and_optional_is_not_nullable(self):
        node=self.collect(action='map',part='#/$defs/MapNodeEdit')
        fields={field['name']:field for field in node['fields']}
        self.assertIn('TABLE_OF_CONTENTS',fields['kind']['enum'])
        self.assertFalse(fields['kind']['required']);self.assertFalse(fields['kind']['null_allowed'])
        self.assertTrue(fields['parent_id']['null_allowed'])
        self.assertIn('MapHeading',fields['heading']['type'])
        heading=self.collect(action='map',part=fields['heading']['definitions'][0]['part'])
        source=next(field for field in heading['fields'] if field['name']=='source')
        self.assertIn('integer',source['type']);self.assertIn('MapSourceSelector',source['type'])
        self.assertTrue(source['required'])
        schema=REQUESTS['revise'].model_json_schema()
        # Every nested definition is inspectable; none are replaced by unknown.
        for name in schema.get('$defs',{}):
            data=self.collect(action='revise',part='#/$defs/'+name,max_chars=2048)
            self.assertNotEqual(data['type'],'unknown')

    def test_initial_annotations_explains_finding_rule_and_real_review_prepare_next_request(self):
        context={'task_id':'task-'+'1'*32,'revision':'workspace-revision-'+'a'*64,
                 'current_owner':'task_workspace','selected':{'ITEMS':{'artifact_sha256':'b'*64}}}
        original=deepcopy(context)
        with patch('task_service.TaskService.invoke',side_effect=AssertionError('describe must not run an operation')):
            description=self.collect(action='annotations',context=context)
        self.assertEqual(context,original)
        rules='\n'.join(description['constraints']['rules'])
        self.assertIn('尚无 review',rules);self.assertIn('不能带 resolutions/self_review_finding',rules)
        next_call=next(example for example in description['examples'] if example.get('tool')=='review_prepare')
        self.assertEqual(next_call['request'],{'task_id':context['task_id'],'expected_revision':context['revision']})
        self.assertTrue(next_call['ready_to_call']);self.assertTrue(next_call['request_schema_valid'])
        REQUESTS['review'].model_validate(next_call['request'])
        self.assertFalse(next_call['stage_changed_by_describe'])
        existing=self.collect(action='annotations',context={**context,'current_owner':'portable_review'},part='examples')
        self.assertFalse(next(e for e in existing['examples'] if e.get('tool')=='review_prepare')['ready_to_call'])

    def test_skeletons_never_invent_source_ids_semantic_classes_or_feedback(self):
        for action in ('map','items','images','annotations','decomposition','revise'):
            data=self.collect(action=action,part='examples')
            first=data['examples'][0]
            self.assertEqual(first['status'],'TEMPLATE_REQUIRES_REAL_INPUT')
            self.assertTrue(first['missing_inputs'])
            self.assertFalse(first['semantic_decisions_generated'])
            serialized=json.dumps(first['request'])
            self.assertNotIn('SRC-0001',serialized);self.assertNotIn('feedback-',serialized)
            self.assertNotIn('"QUALITY"',serialized);self.assertNotIn('"VERIFICATION"',serialized)
        context={'context':{'work_id':'task-'+'2'*32},'revision':'workspace-revision-'+'c'*64,
                 'current_working':{'owner':'portable_review'},'selected':{'ITEMS':{}}}
        scope={'source_item_id':'SRC-provided-by-caller'}
        data=self.collect(action='revise',part='examples',context=context,scope=scope,max_chars=2048)
        self.assertEqual(data['examples'][0]['request']['changes']['decisions'][0]['source_record_id'],scope['source_item_id'])
        self.assertEqual(data['examples'][0]['request']['expected_revision'],context['revision'])

    def test_table_scope_uses_live_record_schema_without_choosing_table_groups(self):
        scope={'scope_id':'actual-returned-table-scope','logical_table_scope_id':'actual-returned-table-scope','scope_kind':'TABLE'}
        response=self.collect(action='items',part='examples',scope=scope,max_chars=2048)
        decisions=response['examples'][0]['request']['decisions']
        self.assertNotIn('text_decisions',decisions)
        table=decisions['table_decisions'][0]
        self.assertEqual(table['logical_table_scope_id'],scope['logical_table_scope_id'])
        record=table['records'][0]
        self.assertNotIn('record_key',record);self.assertNotIn('item_key',record)
        self.assertIsNone(record['row_groups'][0]['row_start']);self.assertIsNone(record['row_groups'][0]['row_end'])
        self.assertIsNone(record['row_groups'][0]['fragment_ref']);self.assertNotIn('fragment_id',record['row_groups'][0])
        constraints=self.collect(action='items',part='constraints')
        text='\n'.join(constraints['rules'])
        self.assertIn('record_key',text);self.assertIn('前导 0',text);self.assertIn('二选一',text)
        definition=self.collect(action='items',part='#/$defs/TableRowSelection')
        fields={f['name']:f for f in definition['fields']}
        self.assertEqual(fields['fragment_ref']['type'],'integer')
        self.assertTrue(fields['row_start']['required'])

    def test_every_overview_read_link_is_a_callable_schema_part(self):
        for action in REQUESTS:
            data=self.collect(action=action)
            for part in data['read_parts']:
                response=task_schema.describe(action,part=part,max_chars=2048)
                self.assertEqual(response['status'],'DESCRIBED')

    def test_invalid_action_ref_and_budget_fail_explicitly(self):
        boolean_schema=self.collect(action='map',part='#/additionalProperties')
        self.assertEqual(boolean_schema['type'],'no value allowed')
        self.assertIs(boolean_schema['json_schema'],False)
        with self.assertRaisesRegex(ValueError,'actual action catalog'):task_schema.describe('not-an-action')
        with self.assertRaisesRegex(ValueError,'does not exist'):task_schema.describe('map',part='#/$defs/missing')
        with self.assertRaisesRegex(ValueError,'local JSON pointer'):task_schema._kind({'$defs':{}},{'$ref':'https://example.invalid/schema'})
        with self.assertRaisesRegex(ValueError,'max_chars'):task_schema.describe('map',max_chars=500)
        with self.assertRaisesRegex(ValueError,'offset'):task_schema.describe('map',part='schema',offset=999999)


if __name__=='__main__':unittest.main()
