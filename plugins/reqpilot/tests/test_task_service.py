"""Same typed application requests over real fixed CLI; no model or private case."""
from pathlib import Path
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT/'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0] = [str(ENTRY.parent), str(ROOT/'prototype')]
from test_portable import make_fixture, item_decisions
from task_service import TaskService


class TypedTaskTests(unittest.TestCase):
    def test_structure_retention_incremental_items_and_replay_keep_existing_identity(self):
        from argparse import Namespace
        from copy import deepcopy
        import artifact_tools
        from test_portable import write as save_input
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source,_,mapping=make_fixture(root/'fixture')
            service=TaskService(root,ENTRY,ROOT)
            state=service.invoke('open',{'mode':'create','source':str(source),'goal':'Preserve source items.','creation_key':'retention'})
            task=state['task_id'];state=service.invoke('prepare',{'task_id':task,'expected_revision':state['revision']})
            edit={k:v for k,v in mapping.items() if k!='source_sha256'};edit['schema_version']='reqpilot-map-edit/v1'
            state=service.invoke('map',{'task_id':task,'expected_revision':state['revision'],'decisions':edit})
            map_dir=Path(state['selected']['MAP']['output_dir']);legacy=item_decisions(map_dir)
            save_input(root/'legacy-input.json',legacy)
            artifact_tools.compile_items(Namespace(map_dir=map_dir,input=root/'legacy-input.json',baseline_dir=None,output_dir=root/'legacy'))
            choices=deepcopy(legacy)
            for key in ('source_sha256','document_map_artifact_sha256'):choices.pop(key,None)
            body=[{k:v for k,v in s.items() if k!='structure_only_spans'} for s in choices['text_decisions'] if s['items']]
            request={'task_id':task,'expected_revision':state['revision'],'command_id':'retained-first',
                'decisions':{'retain_map_structure':True,'text_decisions':body[:1]}}
            first=service.invoke('items',request)
            self.assertEqual(first['operation_state'],'COMMITTED',first)
            item_view=service.invoke('read',first['source_items_read']['request'])
            self.assertEqual(item_view['status'],'READ',item_view)
            self.assertEqual(item_view['baseline_sha256'],first['selected']['ITEMS']['artifact_sha256'])
            binding=first['structure_retention'];before=Path(binding['binding_path']).read_bytes()
            replay=service.invoke('items',request)
            self.assertTrue(replay['replayed']);self.assertEqual(replay['structure_retention'],binding)
            self.assertEqual(before,Path(binding['binding_path']).read_bytes())
            first_baseline=json.loads(Path(first['selected']['ITEMS']['path']).read_text())
            second=service.invoke('items',{'task_id':task,'expected_revision':first['revision'],
                'decisions':{'retain_map_structure':True,'text_decisions':body[1:],'table_decisions':choices['table_decisions']}})
            self.assertEqual(second['operation_state'],'COMMITTED',second)
            actual=json.loads(Path(second['selected']['ITEMS']['path']).read_text())
            expected=json.loads((root/'legacy/source-requirement-baseline.json').read_text())
            self.assertEqual(actual['records'],expected['records'])
            self.assertEqual(actual['covered_scope_ids'],expected['covered_scope_ids'])
            self.assertEqual(actual['records'][:len(first_baseline['records'])],first_baseline['records'])
            self.assertFalse(second['selected']['ITEMS'].get('unprocessed_scope_ids'))

    def test_outline_reuses_task_commit_replay_and_records_mechanical_binding(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source,_,mapping=make_fixture(root/'fixture')
            service=TaskService(root,ENTRY,ROOT)
            state=service.invoke('open',{'mode':'create','source':str(source),'goal':'Organize original structure.','creation_key':'outline'})
            task=state['task_id'];state=service.invoke('prepare',{'task_id':task,'expected_revision':state['revision']})
            nodes=mapping['nodes'];outline=[{'role':'DOCUMENT','ref':nodes[0]['title_source_ids'][0],'children':[
                {'role':'HEADING','ref':n['title_source_ids'][0],'content':n['content_source_ids']} for n in nodes[1:]]}]
            t=mapping['logical_tables'][0]
            outline[0]['children'][-1]['tables']=[{'sources':[{'source':f['source_node_id'],'row_range':f['row_range']} for f in t['fragments']],
                'title':t['title'],'role':t['role']}]
            request={'task_id':task,'expected_revision':state['revision'],'command_id':'outline-once',
                'decisions':{'outline':outline,'source_dispositions':mapping['source_dispositions']}}
            saved=service.invoke('map',request);self.assertEqual(saved['operation_state'],'COMMITTED',saved)
            scoped=service.invoke('read',saved['scope_read']['request'])
            self.assertEqual(scoped['status'],'READ',scoped)
            self.assertTrue(scoped['scopes'])
            folder=Path(saved['selected']['MAP']['output_dir']);receipt=json.loads((folder/'map-edit-binding.json').read_text())
            self.assertIn('outline',receipt)
            actual=json.loads((folder/'document-map.json').read_text())
            self.assertEqual(len(actual['nodes']),len(mapping['nodes']))
            self.assertEqual([n['label'] for n in actual['nodes'][1:]],['1','2','3','4','5','6'])
            replay=service.invoke('map',request);self.assertTrue(replay['replayed'])
            self.assertEqual(saved['operation_id'],replay['operation_id']);self.assertEqual(saved['revision'],replay['revision'])

    def test_description_uses_actual_scope_and_public_continuations_without_writes(self):
        from task_bridge import TaskBridge
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source,_,mapping=make_fixture(root/'fixture')
            service=TaskService(root,ENTRY,ROOT);bridge=TaskBridge(root,ENTRY,project_root=ROOT)
            catalog=bridge.invoke('describe',{})
            self.assertEqual(catalog['status'],'DESCRIBED',catalog)
            self.assertIn('items',[r['action'] for r in catalog['data']['actions']])
            opened=service.invoke('open',{'mode':'create','source':str(source),'goal':'Read-only description test.','creation_key':'describe-case'})
            task=opened['task_id'];state=service.invoke('prepare',{'task_id':task,'expected_revision':opened['revision']})
            edit={k:v for k,v in mapping.items() if k!='source_sha256'};edit['schema_version']='reqpilot-map-edit/v1'
            state=service.invoke('map',{'task_id':task,'expected_revision':state['revision'],'decisions':edit})
            task_root=Path(state['task_root']);before=(task_root/'workspace-index.json').read_bytes()
            for action,payload in [('describe',{'action':'items','scope_id':'text:missing'}),
                    ('items',{'expected_revision':state['revision'],'decisions':{'text_decisions':[{'scope_id':'text:missing','items':[]}]}})]:
                rejected=bridge.invoke(action,{'task_id':task,**payload})
                self.assertEqual(rejected['error_code'],'UNKNOWN_SCOPE',rejected)
                recovered=bridge.invoke('read',rejected['next_action']['request'])
                self.assertEqual(recovered['status'],'READ',recovered)
                self.assertTrue(recovered['scopes'])
                self.assertEqual(before,(task_root/'workspace-index.json').read_bytes())
            scopes=json.loads((Path(state['selected']['MAP']['output_dir'])/'scope-requests.json').read_text())
            scope_id=scopes['table_requests'][0]['logical_table_scope_id']
            def complete(request):
                result=bridge.invoke('describe',request);chunks=[]
                while 'data' not in result:
                    self.assertEqual(result['status'],'DESCRIBED',result);chunks.append(result['text'])
                    if not result['next_read']:return json.loads(''.join(chunks))
                    self.assertEqual(result['next_read']['tool'],'task_describe')
                    self.assertNotIn('context',result['next_read']['request'])
                    result=bridge.invoke('describe',result['next_read']['request'])
                return result['data']
            example=complete({'task_id':task,'action':'items','scope_id':scope_id,'part':'examples','max_chars':2048})['examples'][0]
            record=example['request']['decisions']['table_decisions'][0]['records'][0]
            self.assertNotIn('record_key',record);self.assertNotIn('item_key',record)
            schema=complete({'task_id':task,'action':'items','part':'schema','max_chars':2048})
            from task_service import REQUESTS
            self.assertEqual(schema,REQUESTS['items'].model_json_schema())
            overview=complete({'task_id':task,'action':'items'})
            link=overview['definition_reads'][0]['read']
            self.assertEqual(link['tool'],'task_describe')
            self.assertEqual(link['request']['action'],'items')
            self.assertEqual(bridge.invoke('describe',link['request'])['status'],'DESCRIBED')
            self.assertEqual(before,(task_root/'workspace-index.json').read_bytes())
            with self.assertRaisesRegex(ValueError,'DESCRIPTION_SCHEMA_CHANGED'):
                service.invoke('describe',{'task_id':task,'action':'items','expected_schema_sha256':'0'*64})

    def test_original_filename_is_valid_in_a_task_map_and_self_contained_replay(self):
        from argparse import Namespace
        from artifact_tools import compile_map
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source,_,mapping=make_fixture(root/'fixture')
            service=TaskService(root,ENTRY,ROOT)
            state=service.invoke('open',{'mode':'create','source':str(source),'goal':'保留原文件名。','creation_key':'filename'})
            task=state['task_id']
            state=service.invoke('prepare',{'task_id':task,'expected_revision':state['revision']})
            edit={k:v for k,v in mapping.items() if k!='source_sha256'};edit['schema_version']='reqpilot-map-edit/v1'
            edit['nodes'][0].update(title=source.name,title_source_ids=[])
            state=service.invoke('map',{'task_id':task,'expected_revision':state['revision'],'decisions':edit})
            self.assertEqual(state['operation_state'],'COMMITTED',state)
            folder=Path(state['selected']['MAP']['output_dir'])
            receipt=json.loads((folder/'map-compilation-receipt.json').read_text());inputs=receipt['source_inputs']
            self.assertEqual(inputs['source_display_name'],source.name)
            replay=root/'detached-replay'
            compile_map(Namespace(source=folder/inputs['source_path'],source_ir=folder/inputs['source_ir_path'],
                input=folder/inputs['decisions_path'],output_dir=replay,_source_display_name=inputs['source_display_name']))
            self.assertEqual(json.loads((replay/'document-map.json').read_text()),json.loads((folder/'document-map.json').read_text()))

    def test_real_source_map_items_review_delivery_replay_and_stale_request(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, capture, mapping = make_fixture(root/'fixture')
            service = TaskService(root, ENTRY, ROOT)
            create = {'mode':'create','source':str(source),'goal':'按原序整理合成原件。','creation_key':'create-case'}
            opened = service.invoke('open', create)
            self.assertEqual(service.invoke('open', create)['task_id'], opened['task_id'])
            task = opened['task_id']; initial = opened['revision']
            def write(action, **extra):
                revision = service.invoke('inspect', {'task_id':task})['revision']
                return service.invoke(action, {'task_id':task,'expected_revision':revision, **extra})
            captured = write('prepare')
            self.assertEqual(captured['operation_state'], 'COMMITTED', captured)
            read = service.invoke('read', {'task_id':task,'pages':[1], 'limit':2})
            self.assertEqual(read['status'], 'READ')
            edit = {k:v for k,v in mapping.items() if k != 'source_sha256'}
            edit['schema_version'] = 'reqpilot-map-edit/v1'
            mapped = write('map', decisions=edit)
            self.assertEqual(mapped['operation_state'], 'COMMITTED', mapped)
            state = service.invoke('inspect', {'task_id':task})
            decisions = item_decisions(Path(state['selected']['MAP']['output_dir']))
            for key in ('schema_version','source_sha256','document_map_artifact_sha256'): decisions.pop(key, None)
            from copy import deepcopy
            from task_bridge import TaskBridge
            bad=deepcopy(decisions);scope=next(s for s in bad['text_decisions'] if s['items'])
            body=scope['items'].pop();scope['structure_only_spans'].extend(
                {'text_span':span,'reason_codes':['test_wrong_structure']} for span in body['text_spans'])
            rejected=TaskBridge(root,ENTRY,project_root=ROOT).invoke('items',{'task_id':task,'expected_revision':state['revision'],'decisions':bad})
            self.assertEqual(rejected['error_code'],'SOURCE_ITEM_STRUCTURE_ONLY_SPAN_NOT_TITLE',rejected)
            self.assertEqual(rejected['scope_context']['scope_id'],scope['scope_id'])
            self.assertTrue(rejected['scope_context']['structure_only_candidates'])
            reread=service.invoke('read',rejected['inspect']['request'])
            self.assertEqual(reread['scope_id'],scope['scope_id'])
            self.assertEqual(service.invoke('inspect',{'task_id':task})['revision'],state['revision'])
            request = {'task_id':task,'expected_revision':state['revision'],'command_id':'items-once','decisions':decisions}
            items = service.invoke('items', request)
            self.assertEqual(items['operation_state'], 'COMMITTED', items)
            replay = service.invoke('items', request)
            self.assertTrue(replay['replayed']); self.assertEqual(replay['operation_id'], items['operation_id'])
            self.assertEqual(replay['revision'], items['revision'])
            review = write('review')
            self.assertEqual(review['operation_state'], 'COMMITTED', review)
            self.assertEqual(service.invoke('check',{'task_id':task})['delivery_status'],'NOT_REGISTERED')
            with patch.object(service, 'check', side_effect=ValueError('injected verification failure')):
                delivery = write('deliver', command_id='deliver-once')
            self.assertEqual(delivery['operation_state'],'COMMITTED',delivery)
            self.assertEqual(delivery['verification']['status'],'FAILED')
            self.assertFalse(delivery['local_delivery_ready'])
            repeated=service.invoke('deliver',{'task_id':task,'expected_revision':delivery['base_revision'],'command_id':'deliver-once'})
            self.assertTrue(repeated['local_delivery_ready'],repeated)
            self.assertEqual(repeated['operation_id'],delivery['operation_id'])
            review_root,current=service.state(task)
            with patch('revision_tools.verify_review',return_value={'status':'REVIEW_VERIFIED','review_sha256':'newer-head','delivery':{'path':'another-delivery'}}), patch.object(service,'workbench_check') as host_check:
                changed=service.check(review_root,current)
                self.assertEqual(changed['status'],'REVISION_CHANGED_DURING_CHECK')
                self.assertFalse(changed['review_verified'])
                host_check.assert_not_called()
            self.assertFalse(delivery['handoff_ready'])  # No workbench was bound.
            self.assertTrue(Path(delivery['result']['output_dir'],'matrix.xlsx').is_file())
            from openpyxl import load_workbook
            workbook=load_workbook(Path(delivery['result']['output_dir'],'matrix.xlsx'),read_only=True)
            self.assertIn(source.name,workbook['ComplianceMatrix']['A2'].value)
            with self.assertRaisesRegex(ValueError,'REVISION_CONFLICT'):
                service.invoke('prepare', {'task_id':task,'expected_revision':initial,'command_id':'stale'})
            other = service.invoke('open', {**create, 'creation_key':'distinct-task'})
            self.assertNotEqual(other['task_id'], task)

    def test_bridge_rejects_context_damage_before_starting_a_process(self):
        from task_bridge import TaskBridge
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source,_,_=make_fixture(root/'fixture')
            service=TaskService(root,ENTRY,ROOT)
            task=service.invoke('open',{'mode':'create','source':str(source),'goal':'source','creation_key':'case'})
            context=Path(task['task_root'])/'task-context.json'
            value=json.loads(context.read_text());value['runtime_ref']['entrypoint']='/tmp/not-authorized.py'
            context.write_text(json.dumps(value))
            bridge=TaskBridge(root,ENTRY,project_root=ROOT)
            with patch('task_bridge.subprocess.Popen') as start:
                with self.assertRaises(ValueError):bridge.invoke('inspect',{'task_id':task['task_id']})
                start.assert_not_called()

    def test_typed_cli_rejects_unknown_fields_with_structured_diagnostics(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary); request=root/'request.json'
            request.write_text(json.dumps({'task_id':'task-'+'0'*32,'guess_latest':True}))
            result=subprocess.run([sys.executable,str(ENTRY),'--project-root',str(ROOT),'task','inspect',
                '--working-root',str(root),'--input','-'],input=request.read_text(),capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            payload=json.loads(result.stdout)
            self.assertEqual(payload['error_code'],'INVALID_DECISION')
            self.assertEqual(payload['errors'][0]['loc'],['guess_latest'])


if __name__ == '__main__': unittest.main()
