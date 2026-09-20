"""Real portable CLI/task/compilers; synthetic documents and private temp roots."""
from argparse import Namespace
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
ENTRY=ROOT/'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0]=[str(ROOT/'prototype'),str(ENTRY.parent)]
from test_portable import make_fixture,item_decisions,write
from app.application.agent_workflow import task_workspace as core
import workspace_tools as workspace


class WorkspaceCliTests(unittest.TestCase):
    def test_stale_revision_and_selection_to_begin_race_do_not_execute(self):
        import artifact_tools
        task=self.init();before=core.workspace_status(task,refresh=False)
        args=Namespace(workspace=task,operation='compile-map',input=self.map_input,source_ir=self.capture/'source_ir.json',
            expected_revision=before['revision'],command_id='caller-a',request_payload={'decision':'map','expected':before['revision']})
        original=workspace._inputs
        def raced(arguments,root,state):
            chosen=original(arguments,root,state)
            concurrent=core.begin_operation(task,'compile-map',{'input':self.map_input,'source_ir':self.capture/'source_ir.json'})
            artifact_tools.compile_map(Namespace(source=self.source,source_ir=self.capture/'source_ir.json',
                input=Path(concurrent['resolved_inputs']['input']),output_dir=Path(concurrent['output_dir'])))
            core.commit_operation(task,concurrent['operation_id'])
            return chosen
        with patch.object(workspace,'_inputs',side_effect=raced),patch.object(workspace.subprocess,'run') as execute:
            with self.assertRaisesRegex(ValueError,'REVISION_CONFLICT'):workspace.run(args,ENTRY)
        execute.assert_not_called()
        with patch.object(workspace.subprocess,'run') as execute:
            with self.assertRaisesRegex(ValueError,'REVISION_CONFLICT'):workspace.run(args,ENTRY)
        execute.assert_not_called()
        self.assertEqual(len(core.workspace_status(task)['index']['committed']),1)

    def test_lost_response_replays_same_operation_before_stale_expected_or_input_rebuild(self):
        task=self.init();revision=core.workspace_status(task,refresh=False)['revision']
        request={'typed_decision':{'scope':'Map'},'expected_revision':revision}
        args=Namespace(workspace=task,operation='compile-map',input=self.map_input,source_ir=self.capture/'source_ir.json',
            expected_revision=revision,command_id='lost-response',request_payload=request)
        first=workspace.run(args,ENTRY)
        self.assertEqual(first['state'],'COMMITTED')
        args.input=None;args.source_ir=None
        with patch.object(workspace.subprocess,'run') as execute:
            replay=workspace.run(args,ENTRY)
        execute.assert_not_called()
        self.assertEqual(replay['operation_id'],first['operation_id'])
        self.assertEqual(replay['receipt_sha256'],first['receipt_sha256'])
        self.assertEqual(replay['compiler_result'],first['compiler_result'])
        self.assertEqual(replay['publication']['status'],'NOT_REEXECUTED')
        self.assertTrue(replay['replayed'])
        args.request_payload={**request,'typed_decision':{'scope':'different'}}
        with self.assertRaisesRegex(ValueError,'COMMAND_ID_CONFLICT'):workspace.run(args,ENTRY)

    def test_started_command_reports_unknown_without_restarting(self):
        task=self.init();revision=core.workspace_status(task,refresh=False)['revision'];request={'phase':'capture'}
        operation=core.begin_operation(task,'capture',expected_revision=revision,command_id='reserved-command',request=request)
        with patch.object(workspace.subprocess,'run') as execute:
            replay=workspace.run(Namespace(workspace=task,operation='capture',expected_revision=revision,
                command_id='reserved-command',request_payload=request),ENTRY)
        execute.assert_not_called()
        self.assertEqual(replay['operation_id'],operation['operation_id'])
        self.assertEqual(replay['state'],'STARTED');self.assertEqual(replay['execution_state'],'UNKNOWN')
        self.assertNotEqual(replay['status'],'FAILED')

    def test_missing_structured_result_does_not_commit_even_with_valid_written_bundle(self):
        from types import SimpleNamespace
        task=self.init();actual=workspace.subprocess.run
        def malformed(*args,**kwargs):
            completed=actual(*args,**kwargs)
            self.assertEqual(completed.returncode,0)
            return SimpleNamespace(returncode=0,stdout='library completed without a JSON result',stderr=completed.stderr)
        with patch.object(workspace.subprocess,'run',side_effect=malformed):
            result=workspace.run(Namespace(workspace=task,operation='compile-map',input=self.map_input,source_ir=self.capture/'source_ir.json'),ENTRY)
        self.assertEqual(result['state'],'FAILED')
        self.assertIn('MALFORMED_TOOL_RESULT',result['error'])
        self.assertEqual(core.workspace_status(task)['index']['committed'],[])
        operation=_json(task/'operations'/(result['operation_id']+'.json'))
        self.assertTrue((task/operation['output_dir']/'document-map.json').is_file())

    def test_cross_entry_cli_forwards_version_and_command_and_creation_key_is_idempotent(self):
        init=['workspace','init','--working-root',self.root/'working-key','--source',self.source,'--goal','One request','--creation-key','same-create']
        first=self.cli(*init);again=self.cli(*init)
        self.assertEqual(first['task_root'],again['task_root']);self.assertTrue(again['replayed'])
        task=Path(first['task_root'])
        args=Namespace(workspace=task,operation='compile-map',input=self.map_input,source_ir=self.capture/'source_ir.json',
            expected_revision=first['revision'],command_id='forwarded')
        result=workspace.run(args,self.root/'other-entry.py')
        self.assertEqual(result['state'],'COMMITTED')
        with patch.object(workspace.subprocess,'run') as execute:
            replay=workspace.run(args,self.root/'other-entry.py')
        execute.assert_not_called();self.assertEqual(result['operation_id'],replay['operation_id'])

    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name)
        self.source,self.capture,self.decisions=make_fixture(self.root/'fixture')
        self.map_input=self.root/'map.json';write(self.map_input,self.decisions)

    def tearDown(self):self.temp.cleanup()

    def cli(self,*args,cwd=None,expected=0):
        completed=subprocess.run([sys.executable,str(ENTRY),'--project-root',str(ROOT),*map(str,args)],
            cwd=cwd or self.root,text=True,capture_output=True)
        value=workspace._result(completed.stdout) or workspace._result(completed.stderr)
        self.assertEqual(completed.returncode,expected,(value,completed.stderr))
        self.assertIsInstance(value,dict,completed.stdout)
        return value

    def init(self,confirmed=None):
        args=['workspace','init','--working-root',self.root/'working','--source',self.source,'--goal','完整整理合成原件，保持原文和来源顺序。',
              '--project-key','p','--document-key','d']
        if confirmed is True:args.append('--no-map-confirmation')
        elif confirmed is False:args.append('--require-map-confirmation')
        return Path(self.cli(*args)['task_root'])

    def mapped(self,task):
        return self.cli('workspace','run','compile-map','--workspace',task,'--source-ir',self.capture/'source_ir.json','--input',self.map_input)

    def test_real_cli_capture_reading_map_items_overlays_delivery_and_review(self):
        task=self.init()
        self.assertFalse(core.workspace_status(task)['context']['request']['require_map_confirmation'])
        captured=self.cli('workspace','run','capture','--workspace',task)
        self.assertEqual(captured['state'],'COMMITTED')
        reading=self.cli('workspace','run','reading-workspace','--workspace',task)
        self.assertEqual(reading['state'],'COMMITTED')
        self.assertTrue(Path(reading['selected']['READING']['manifest_path']).is_file())
        # The fixed source bytes make deterministic capture handles reusable.
        mapped=self.cli('workspace','run','compile-map','--workspace',task,'--input','map.json',cwd=self.root)
        self.assertEqual(mapped['state'],'COMMITTED')
        self.assertEqual(mapped['workbench_sync']['status'],'NOT_BOUND')
        map_dir=Path(mapped['selected']['MAP']['output_dir'])
        original_input=self.map_input.read_bytes();self.map_input.write_text('{}')
        self.assertEqual(self.cli('workspace','status','--workspace',task)['selected']['MAP']['artifact_sha256'],mapped['selected']['MAP']['artifact_sha256'])
        frozen=json.loads((task/'operations'/f"{mapped['operation_id']}.json").read_text())['inputs']['input']['path']
        self.assertEqual((task/frozen).read_bytes(),original_input)
        item_input=self.root/'items.json';write(item_input,item_decisions(map_dir))
        items=self.cli('workspace','run','compile-items','--workspace',task,'--input',item_input)
        self.assertEqual(items['state'],'COMMITTED')
        from app.application.agent_workflow.source_requirement_baseline import DocumentMapArtifactV1
        from app.application.agent_workflow.source_item_visual import build_source_item_image_scope_packet
        mapping=DocumentMapArtifactV1.model_validate_json(Path(mapped['selected']['MAP']['path']).read_text())
        packet=build_source_item_image_scope_packet(mapping,node_id='s6')
        image_input=self.root/'images.json'
        write(image_input,{'schema_version':'reqpilot-image-decisions/v1','source_sha256':mapping.source_sha256,
            'document_map_artifact_sha256':mapping.artifact_sha256,'scopes':[{'node_id':'s6','packet_sha256':packet['packet_sha256'],
            'items':[{'item_key':f'drawing-{number}','display_label':f'Dimensional drawing {number}',
                'image_anchor_ids':[image['anchor_id']],'requirementness':'REQUIREMENT','requirement_kinds':['QUALITY'],
                'non_requirement_role':None,'confidence':0.9,'decision_summary':'The original drawing specifies a dimensional limit.',
                'reason_codes':['normative_drawing'],'visual_interpretation':'Synthetic Agent reading of the visible dimension.'}
                for number,image in enumerate(packet['images'],1)]}]})
        images=self.cli('workspace','run','compile-images','--workspace',task,'--input',image_input)
        self.assertEqual(images['state'],'COMMITTED')
        baseline=json.loads(Path(images['selected']['ITEMS']['path']).read_text());sid=baseline['records'][0]['source_record_id']
        self.assertEqual(len(baseline['records']),10)
        annotation=self.root/'annotations.json'
        write(annotation,{'schema_version':'reqpilot-annotation-decisions/v1','source_sha256':baseline['source_sha256'],
            'document_map_artifact_sha256':baseline['document_map_artifact_sha256'],'baseline_sha256':baseline['artifact_sha256'],
            'annotation_updates':[{'source_item_id':sid,'topics':['VERIFICATION'],'reason':'Explicit test annotation after source review.'}]})
        annotated=self.cli('workspace','run','compile-annotations','--workspace',task,'--input',annotation)
        self.assertEqual(annotated['state'],'COMMITTED')
        decomposition=self.root/'decomposition.json'
        write(decomposition,{'schema_version':'reqpilot-decomposition-decisions/v1','baseline_sha256':baseline['artifact_sha256'],
            'authorization':{'request':'保留本条完整响应目标。','source_item_ids':[sid]},
            'decisions':[{'source_item_id':sid,'action':'KEEP','reason':'Complete response target and conditions belong together.'}]})
        decomposed=self.cli('workspace','run','compile-decomposition','--workspace',task,'--input',decomposition)
        self.assertEqual(decomposed['state'],'COMMITTED')
        delivered=self.cli('workspace','run','deliver','--workspace',task)
        self.assertEqual(delivered['state'],'COMMITTED')
        self.assertTrue((Path(delivered['output_dir'])/'matrix.xlsx').is_file())
        opened=self.cli('workspace','run','review-open','--workspace',task)
        self.assertEqual(opened['state'],'COMMITTED')
        self.assertEqual(opened['current_working']['owner'],'portable_review')
        current=Path(opened['current_working']['review_root'])/opened['current_working']['version_path']
        self.assertEqual((current/'image-annotations.json').read_bytes(),Path(annotated['selected']['ANNOTATIONS']['path']).read_bytes())
        self.assertTrue((current/'source-item-decomposition.json').is_file())
        delivery_command=['workspace','run','deliver','--workspace',task,'--command-id','final-delivery',
            '--expected-revision',opened['revision']]
        final=self.cli(*delivery_command)
        self.assertEqual(final['state'],'COMMITTED')
        self.assertEqual(final['delivery_attachment']['status'],'CURRENT_DELIVERY_ATTACHED')
        self.assertFalse(final['delivery_attachment']['business_revision_created'])
        self.assertEqual(list((task/'review/versions').iterdir()),[task/'review/versions/initial'])
        head=_json(task/'review/workspace-manifest.json')
        self.assertEqual(head['current']['version_path'],'versions/initial')
        self.assertTrue((task/'review'/head['delivery']['path']/'matrix.xlsx').is_file())
        verified=self.cli('review-verify','--workspace',task,'--review-dir',task/'review')
        self.assertEqual(verified['status'],'REVIEW_VERIFIED')
        replayed=self.cli(*delivery_command)
        self.assertTrue(replayed['replayed'])
        self.assertEqual(replayed['operation_id'],final['operation_id'])
        self.assertEqual(replayed['delivery_attachment']['status'],'CURRENT_DELIVERY_ATTACHED')
        self.assertEqual(replayed['delivery_attachment']['execution_state'],'NOT_REEXECUTED')
        self.assertEqual(replayed['index_summary']['generation'],final['index_summary']['generation'])
        retried=self.cli('review-attach-delivery','--workspace',task,'--review-dir',task/'review',
            '--delivery-dir',final['output_dir'],'--expected-review-sha256',opened['current_working']['review_sha256'])
        self.assertTrue(retried['idempotent'])
        self.assertEqual(self.cli('workspace','recover','--workspace',task)['recovered'],[])
        child=task/'notes';state=self.cli('workspace','status',cwd=child)
        self.assertEqual(state['context']['work_id'],opened['context']['work_id'])
        full=self.cli('workspace','status','--workspace',task,'--full')
        self.assertNotIn('index',state)
        self.assertNotIn('committed_operations',state)
        self.assertNotIn('saved_artifacts',state['brief'])
        self.assertNotIn('selected',state['current_working'])
        self.assertEqual(state['selected'],full['selected'])
        self.assertEqual(state['index_summary']['committed_count'],len(full['index']['committed']))
        self.assertLess(len(json.dumps(state)),len(json.dumps(full)))

    def test_actual_subprocess_checkpoint_blocks_and_failed_compile_preserves_map(self):
        task=self.init(confirmed=False);mapped=self.mapped(task)
        # Map-only resumption can use its pinned replay capture without a READING stage.
        repeated=self.cli('workspace','run','compile-map','--workspace',task,'--input',self.map_input)
        self.assertEqual(repeated['state'],'COMMITTED')
        self.assertNotIn('READING',repeated['selected'])
        decisions=self.root/'items.json';write(decisions,item_decisions(Path(mapped['selected']['MAP']['output_dir'])))
        blocked=self.cli('workspace','run','compile-items','--workspace',task,'--input',decisions)
        self.assertEqual(blocked['state'],'BLOCKED')
        self.assertEqual(blocked['status'],'WORKBENCH_NOT_BOUND')
        self.assertNotIn('ITEMS',blocked['selected'])
        bad=self.root/'bad.json';bad.write_text('{}')
        failed=self.cli('workspace','run','compile-map','--workspace',task,'--source-ir',self.capture/'source_ir.json','--input',bad,expected=2)
        self.assertEqual(failed['state'],'FAILED')
        state=self.cli('workspace','status','--workspace',task)
        self.assertEqual(state['selected']['MAP']['artifact_sha256'],mapped['selected']['MAP']['artifact_sha256'])
        self.assertTrue(Path(failed['stderr_path']).is_file())

    def test_runtime_is_pinned_and_missing_entry_does_not_select_current_installation(self):
        task=self.init(confirmed=True);context=core.find_task_workspace(explicit=task)[1]
        self.assertEqual(context['runtime_ref']['entrypoint'],str(ENTRY))
        self.assertEqual(context['runtime_ref']['python_executable'],sys.executable)
        context['runtime_ref']['entrypoint']=str(self.root/'absent.py')
        context.pop('context_sha256')
        (task/'task-context.json').write_text(json.dumps(context))
        result=self.cli('workspace','run','capture','--workspace',task,expected=2)
        self.assertIn('Pinned task entrypoint',result['error'])
        self.assertEqual(core.workspace_status(task)['index']['committed'],[])

    def test_confirmation_flags_are_explicit_and_mutually_exclusive(self):
        required=self.init(confirmed=False);compatible=self.init(confirmed=True)
        self.assertTrue(core.workspace_status(required)['context']['request']['require_map_confirmation'])
        self.assertFalse(core.workspace_status(compatible)['context']['request']['require_map_confirmation'])
        command=[sys.executable,str(ENTRY),'--project-root',str(ROOT),'workspace','init','--working-root',str(self.root/'conflict'),
            '--source',str(self.source),'--goal','Explicit flags','--require-map-confirmation','--no-map-confirmation']
        result=subprocess.run(command,text=True,capture_output=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('not allowed with argument',result.stderr)
        self.assertFalse((self.root/'conflict').exists())

    def test_conflict_candidate_never_calls_parent_publication(self):
        task=self.init(confirmed=True)
        args=Namespace(workspace=task,operation='compile-map',input=self.map_input,source_ir=self.capture/'source_ir.json')
        commit=core.commit_operation
        def concurrent(root,operation_id,**kwargs):
            # A real competing commit consumes the original generation first.
            planned=core.begin_operation(root,'compile-map',{'input':self.map_input,'source_ir':self.capture/'source_ir.json'})
            import artifact_tools
            artifact_tools.compile_map(Namespace(source=self.source,source_ir=self.capture/'source_ir.json',input=Path(planned['resolved_inputs']['input']),output_dir=Path(planned['output_dir'])))
            commit(root,planned['operation_id'])
            return commit(root,operation_id,**kwargs)
        with patch.object(core,'commit_operation',side_effect=concurrent),patch('workbench_tools.sync_after_operation') as publish:
            result=workspace.run(args,ENTRY)
        self.assertEqual(result['state'],'CANDIDATE_CONFLICT')
        self.assertEqual(result['publication']['status'],'NOT_PUBLISHED')
        publish.assert_not_called()
        self.assertTrue(Path(result['receipt_path']).is_file())

    def test_postcommit_receipt_failure_preserves_commit_and_actual_sync_result(self):
        task=self.init(confirmed=True)
        args=Namespace(workspace=task,operation='compile-map',input=self.map_input,source_ir=self.capture/'source_ir.json')
        with patch.object(core,'record_host_receipt',side_effect=OSError('receipt disk unavailable')):
            result=workspace.run(args,ENTRY)
        self.assertEqual(result['state'],'COMMITTED')
        self.assertEqual(result['workbench_sync']['status'],'NOT_BOUND')
        self.assertEqual(result['host_receipt']['status'],'NOT_RECORDED')
        self.assertTrue(Path(result['receipt_path']).is_file())
        self.assertEqual(core.workspace_status(task)['selected']['MAP']['artifact_sha256'],result['selected']['MAP']['artifact_sha256'])

    def test_automatic_delivery_attach_cas_failure_keeps_committed_export(self):
        import revision_tools
        task=self.init();mapped=self.mapped(task)
        decisions=self.root/'items.json';write(decisions,item_decisions(Path(mapped['selected']['MAP']['output_dir'])))
        self.cli('workspace','run','compile-items','--workspace',task,'--input',decisions)
        opened=self.cli('workspace','run','review-open','--workspace',task)
        with patch.object(revision_tools,'attach_current_delivery',side_effect=ValueError('Review changed before attachment')):
            result=workspace.run(Namespace(workspace=task,operation='deliver'),ENTRY)
        self.assertEqual(result['state'],'COMMITTED')
        self.assertEqual(result['delivery_attachment']['status'],'ATTACH_FAILED')
        self.assertIn('Review changed',result['delivery_attachment']['error'])
        self.assertTrue((Path(result['output_dir'])/'matrix.xlsx').is_file())
        self.assertTrue(Path(result['receipt_path']).is_file())
        self.assertNotIn('delivery',_json(task/'review/workspace-manifest.json'))
        self.assertEqual(core.workspace_status(task)['current_working']['review_sha256'],opened['current_working']['review_sha256'])

    def test_init_offline_keeps_task_and_source_for_same_task_reconnection(self):
        args=Namespace(working_root=self.root/'working',source=self.source,goal='先建立任务，明确目标来源。',
            project_key='p',document_key='d',project_id=None,document_id=None,no_map_confirmation=False,
            project_root=ROOT,binding_file=None,open=False,creation_key='offline-create',
            workbench_url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d')
        with patch('workbench_tools.request',side_effect=OSError('offline')):
            result=workspace.initialize(args,ENTRY)
        self.assertEqual(result['status'],'WORKSPACE_INITIALIZED')
        self.assertEqual(result['workbench_sync']['status'],'NOT_SYNCHRONIZED')
        state=core.workspace_status(result['task_root'])
        self.assertEqual(state['context']['document_id'],'d')
        self.assertTrue((Path(result['task_root'])/state['context']['source']['path']).is_file())
        with patch('workbench_tools.request') as request:
            replay=workspace.initialize(args,ENTRY)
        request.assert_not_called()
        self.assertEqual(replay['task_root'],result['task_root'])
        self.assertEqual(replay['workbench_sync']['status'],'NOT_REEXECUTED')

    def test_real_host_publication_occurs_only_after_local_commit(self):
        from test_workbench_transport import WorkbenchTransportTests
        from app.application.agent_workflow import plugin_workbench as api
        import workbench_tools as transport
        fixture=WorkbenchTransportTests('test_native_tools_keep_bypass_even_with_bad_explicit_binding')
        fixture.setUp()
        try:
            args=Namespace(working_root=self.root/'working',source=fixture.backend.source,goal='原文Map保存并同步',
                project_key='p',document_key='d',project_id='p',document_id='d',no_map_confirmation=None,require_map_confirmation=True,
                project_root=ROOT,binding_file=None,open=False,
                workbench_url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d')
            initialized=workspace.initialize(args,ENTRY);task=Path(initialized['task_root'])
            original_request=transport.request;observed=[]
            def request(url,payload=None):
                if payload and payload.get('artifacts'):
                    current=core.workspace_status(task)
                    committed=current['index']['committed']
                    self.assertEqual(len(committed),1)
                    self.assertEqual(committed[0]['state'],'COMMITTED')
                    self.assertTrue((task/committed[0]['receipt_path']).is_file())
                    observed.append(committed[0]['operation_id'])
                return original_request(url,payload)
            run_args=Namespace(workspace=task,operation='compile-map',input=fixture.root/'map-input.json',
                source_ir=fixture.backend.source.parent/'capture/source_ir.json')
            with patch.object(transport,'request',side_effect=request):
                result=workspace.run(run_args,ENTRY)
            self.assertEqual(observed,[result['operation_id']])
            self.assertEqual(result['state'],'COMMITTED')
            self.assertEqual(result['workbench_sync']['status'],'WAITING_FOR_MAP_CONFIRMATION')
            self.assertEqual(len(result['index']['host_receipts']),1)
            remote=api.get_plugin_workbench('p','d',result['workbench_sync']['session_id'],fixture.backend.sink())
            self.assertEqual(remote['stages'][0]['artifact_sha256'],result['selected']['MAP']['artifact_sha256'])
        finally:fixture.tearDown()

    def test_explicit_other_map_and_baseline_do_not_adopt_current_state(self):
        from copy import deepcopy
        import artifact_tools
        task=self.init(confirmed=True);mapped=self.mapped(task)
        decisions=self.root/'items.json';write(decisions,item_decisions(Path(mapped['selected']['MAP']['output_dir'])))
        items=self.cli('workspace','run','compile-items','--workspace',task,'--input',decisions)
        baseline=_json(Path(items['selected']['ITEMS']['path']))
        annotation=self.root/'current-annotation.json'
        write(annotation,{'schema_version':'reqpilot-annotation-decisions/v1','source_sha256':baseline['source_sha256'],
            'document_map_artifact_sha256':baseline['document_map_artifact_sha256'],'baseline_sha256':baseline['artifact_sha256'],
            'annotation_updates':[{'source_item_id':baseline['records'][0]['source_record_id'],'topics':['VERIFICATION'],'reason':'Current baseline topic.'}]})
        self.cli('workspace','run','compile-annotations','--workspace',task,'--input',annotation)
        other=deepcopy(self.decisions);other['nodes'][-1]['node_id']='s6-revised';other['logical_tables'][0]['owner_node_id']='s6-revised'
        other_input=self.root/'other-map.json';write(other_input,other)
        other_map=self.root/'other-map'
        artifact_tools.compile_map(Namespace(source=self.source,source_ir=self.capture/'source_ir.json',input=other_input,output_dir=other_map))
        other_items=self.root/'other-items.json';write(other_items,item_decisions(other_map))
        candidate=self.cli('workspace','run','compile-items','--workspace',task,'--map-dir',other_map,'--input',other_items)
        self.assertEqual(candidate['state'],'SAVED_CANDIDATE')
        inputs=_json(task/'operations'/f"{candidate['operation_id']}.json")['inputs']
        self.assertNotIn('baseline_dir',inputs)
        delivered=self.cli('workspace','run','deliver','--workspace',task,'--baseline-dir',candidate['output_dir'])
        self.assertEqual(delivered['state'],'SAVED_CANDIDATE')
        inputs=_json(task/'operations'/f"{delivered['operation_id']}.json")['inputs']
        self.assertNotIn('annotations',inputs)
        self.assertEqual(delivered['selected']['ITEMS']['artifact_sha256'],baseline['artifact_sha256'])

    def test_late_publication_of_prior_generation_cannot_replace_new_owner(self):
        from test_workbench_transport import WorkbenchTransportTests
        from app.application.agent_workflow import plugin_workbench as api
        import workbench_tools as transport
        import artifact_tools
        fixture=WorkbenchTransportTests('test_native_tools_keep_bypass_even_with_bad_explicit_binding')
        fixture.setUp()
        try:
            args=Namespace(working_root=self.root/'working',source=fixture.backend.source,goal='保留多次编制，发布当前选定版。',
                project_key='p',document_key='d',project_id='p',document_id='d',no_map_confirmation=True,
                project_root=ROOT,binding_file=None,open=False,
                workbench_url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d')
            initialized=workspace.initialize(args,ENTRY);task=Path(initialized['task_root'])
            saved=[]
            for _ in range(2):
                operation=core.begin_operation(task,'compile-map',{'input':fixture.root/'map-input.json',
                    'source_ir':fixture.backend.source.parent/'capture/source_ir.json'})
                result=artifact_tools.compile_map(Namespace(source=fixture.backend.source,
                    source_ir=fixture.backend.source.parent/'capture/source_ir.json',
                    input=Path(operation['resolved_inputs']['input']),output_dir=Path(operation['output_dir'])))
                committed=core.commit_operation(task,operation['operation_id'],result=result)
                self.assertEqual(committed['state'],'COMMITTED')
                saved.append((operation,result,committed))
            # Even identical Map bytes have distinct selected producer references.
            self.assertEqual(saved[0][2]['selected']['MAP']['artifact_sha256'],saved[1][2]['selected']['MAP']['artifact_sha256'])
            operation,result,_=saved[1]
            publish_args=Namespace(command='compile-map',workspace=task,output_dir=Path(operation['output_dir']),binding_file=None)
            latest=workspace.publish_committed(task,operation['operation_id'],publish_args,result)
            revision=latest['workbench_sync']['revision_id'];session=latest['workbench_sync']['session_id']
            operation,result,_=saved[0];publish_args.output_dir=Path(operation['output_dir'])
            with patch.object(transport,'request',wraps=transport.request) as request:
                late=workspace.publish_committed(task,operation['operation_id'],publish_args,result)
            self.assertEqual(late['workbench_sync']['status'],'SKIPPED_SUPERSEDED')
            request.assert_not_called()
            remote=api.get_plugin_workbench('p','d',session,fixture.backend.sink())
            self.assertEqual(remote['revision_id'],revision)
            self.assertEqual(len(remote['versions']),1)
            self.assertTrue((Path(operation['output_dir'])/'operation-receipt.json').is_file())
        finally:fixture.tearDown()

    def test_explicit_workspace_reconnection_sends_saved_map_unless_source_only_was_requested(self):
        from test_workbench_transport import WorkbenchTransportTests
        import artifact_tools
        import reqpilot as cli
        fixture=WorkbenchTransportTests('test_native_tools_keep_bypass_even_with_bad_explicit_binding')
        fixture.setUp()
        try:
            for source_only in (False,True):
                initialized=core.initialize_workspace(self.root/'working',fixture.backend.source,'断网后连接已保存的结构',
                    project_key='p',document_key='d',project_id='p',document_id='d',require_map_confirmation=True)
                task=Path(initialized['task_root'])
                operation=core.begin_operation(task,'compile-map',{'input':fixture.root/'map-input.json',
                    'source_ir':fixture.backend.source.parent/'capture/source_ir.json'})
                compiled=artifact_tools.compile_map(Namespace(source=fixture.backend.source,
                    source_ir=fixture.backend.source.parent/'capture/source_ir.json',
                    input=Path(operation['resolved_inputs']['input']),output_dir=Path(operation['output_dir'])))
                saved=core.commit_operation(task,operation['operation_id'],result=compiled)
                args=fixture.args(workspace=task,project_root=ROOT,source=fixture.backend.source if source_only else None)
                synced=cli.workbench(args)
                if source_only:self.assertEqual(synced['stages'],[])
                else:
                    self.assertEqual(synced['status'],'WAITING_FOR_MAP_CONFIRMATION')
                    self.assertEqual(synced['stages'][0]['artifact_sha256'],saved['selected']['MAP']['artifact_sha256'])
        finally:fixture.tearDown()


def _json(path):return json.loads(Path(path).read_text())
