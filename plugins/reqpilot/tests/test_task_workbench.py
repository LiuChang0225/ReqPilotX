"""Current-review facade checks against the real synthetic in-process API."""
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
import json
import unittest
from unittest.mock import patch

import test_workbench_transport as transport_fixture
from test_portable import item_decisions, write
import artifact_tools
import revision_tools
import workbench_tools as transport
import workspace_tools
import task_workbench as workbench
from app.application.agent_workflow import task_workspace as core
from app.application.agent_workflow import plugin_workbench as api


class TaskWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.fixture=transport_fixture.WorkbenchTransportTests('test_native_tools_keep_bypass_even_with_bad_explicit_binding');self.fixture.setUp()
        self.root=self.fixture.root;self.backend=self.fixture.backend
        def request(url,payload=None):
            parsed=urlsplit(url);parts=parsed.path.split('/')
            project=unquote(parts[parts.index('projects')+1]);document=unquote(parts[parts.index('sor-documents')+1])
            if payload is not None:return api.sync_plugin_draft(project,document,api.PluginDraftUpload(**payload),self.backend.sink())
            if parts[-1]=='snapshot':return api.get_plugin_snapshot(project,document,parts[-2],self.backend.sink(),revision=parse_qs(parsed.query)['revision'][0])
            return api.get_plugin_workbench(project,document,parts[-1],self.backend.sink())
        self.request=request
        self.patches=[patch.object(transport,'request',side_effect=request),patch.object(workbench,'request',side_effect=request)]
        self.calls=[p.start() for p in self.patches]
        self.task,self.open_operation,self.open_result=self.prepare_task('default')

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.fixture.tearDown()

    def prepare_task(self,key,required=False):
        initialized=core.initialize_workspace(self.root/'working',self.backend.source,'Prepare the source-bound review',
            project_key='p',document_key=key,project_id='p',document_id='d',require_map_confirmation=required)
        task=Path(initialized['task_root']);source=task/initialized['context']['source']['path']
        transport.sync_workbench(Namespace(workspace=task,url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d'))
        op=core.begin_operation(task,'compile-map',{'source_ir':self.backend.source.parent/'capture/source_ir.json','input':self.root/'map-input.json'})
        result=artifact_tools.compile_map(Namespace(source=source,source_ir=self.backend.source.parent/'capture/source_ir.json',
            input=Path(op['resolved_inputs']['input']),output_dir=Path(op['output_dir'])))
        mapped=core.commit_operation(task,op['operation_id'],result=result);mapping=Path(mapped['selected']['MAP']['output_dir'])
        decisions=task/'notes/items.json';write(decisions,item_decisions(mapping))
        op=core.begin_operation(task,'compile-items',{'map_dir':mapping,'input':decisions})
        result=artifact_tools.compile_items(Namespace(map_dir=mapping,baseline_dir=None,input=Path(op['resolved_inputs']['input']),output_dir=Path(op['output_dir'])))
        items=core.commit_operation(task,op['operation_id'],result=result);baseline=Path(items['selected']['ITEMS']['output_dir'])
        op=core.begin_operation(task,'review-open',{'source':source,'baseline_dir':baseline})
        opened=revision_tools.open_review(Namespace(source=source,baseline_dir=baseline,review_dir=Path(op['output_dir'])))
        core.commit_operation(task,op['operation_id'],result=opened)
        return task,op,opened

    def attach_export(self,task):
        state=core.workspace_status(task,refresh=False)
        source=task/state['context']['source']['path'];baseline=Path(state['selected']['ITEMS']['output_dir'])
        op=core.begin_operation(task,'deliver',{'source':source,'baseline_dir':baseline})
        result=artifact_tools.deliver(Namespace(source=source,baseline_dir=baseline,input=None,output_dir=Path(op['output_dir'])))
        core.commit_operation(task,op['operation_id'],result=result)
        revision_tools.attach_current_delivery(Namespace(workspace=task,review_dir=task/'review',delivery_dir=Path(op['output_dir']),
            expected_review_sha256=op['expected_review_sha256']))
        return json.loads((Path(op['output_dir'])/'matrix-snapshot.json').read_text())

    def feedback(self,task):
        head=revision_tools.load_head(task/'review');path=task/'notes/feedback.json'
        write(path,{'expected_review_sha256':head['review_sha256'],'feedback':[{'origin':'AGENT_SELF_REVIEW',
            'source_item_ids':[],'text':'An independently observed source question.'}]})
        return revision_tools.feedback_review(Namespace(review_dir=task/'review',input=path))

    def test_first_binding_reuses_remote_work_after_lost_acknowledgement(self):
        initialized=core.initialize_workspace(self.root/'lost-ack',self.backend.source,'Prepare a task',
            project_key='p',document_key='lost-ack',project_id='p',document_id='d')
        task=Path(initialized['task_root']); session_ids=[]
        def lost_reply(url,payload=None):
            result=self.request(url,payload)
            if payload is not None:
                session_ids.append(payload['session_id'])
                if len(session_ids)==1: raise OSError('reply lost after server committed')
            return result
        args=Namespace(workspace=task,url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d')
        with patch.object(transport,'request',side_effect=lost_reply):
            with self.assertRaises(OSError): transport.sync_workbench(args)
            result=transport.sync_workbench(args)
        self.assertEqual(len(session_ids),2)
        self.assertEqual(session_ids[0],session_ids[1])
        self.assertEqual(result['session_id'],session_ids[0])
        self.assertEqual(core.workspace_status(task,refresh=False)['context']['work_id'],initialized['context']['work_id'])

    def test_same_content_cannot_hide_wrong_task_project_document_or_session(self):
        snapshot=self.attach_export(self.task);state=core.workspace_status(self.task,refresh=False)
        workbench.sync_current_review(self.task,state['revision']);state=core.workspace_status(self.task,refresh=False)
        self.assertTrue(workbench.check_workbench(state,snapshot)['workbench_content_verified'])
        path=Path(state['binding_file']);original=transport.read(path)
        for field,value in (('work_id','task-'+'f'*32),('project_id','other-project'),('document_id','other-document'),('session_id','plugin-'+'b'*32)):
            with self.subTest(field=field):
                transport.save(path,{**original,field:value})
                with patch.object(workbench,'request',side_effect=self.request) as calls:
                    checked=workbench.check_workbench(state,snapshot)
                self.assertFalse(checked['workbench_content_verified']);self.assertEqual(checked['workbench_status'],'SCOPE_MISMATCH')
                calls.assert_not_called();transport.save(path,original)
        with patch.object(workbench,'request',side_effect=lambda url: {**self.request(url),'session_id':'plugin-'+'c'*32}):
            checked=workbench.check_workbench(state,snapshot)
        self.assertFalse(checked['workbench_content_verified']);self.assertEqual(checked['workbench_status'],'SCOPE_MISMATCH')

    def test_current_review_can_sync_after_offline_export_and_old_producer_still_skips(self):
        args=Namespace(command='review-open',workspace=self.task,review_dir=self.task/'review',output_dir=self.task/'review',binding_file=None)
        with patch.object(transport,'request',side_effect=OSError('offline')):
            failed=workspace_tools.publish_committed(self.task,self.open_operation['operation_id'],args,self.open_result)
        self.assertEqual(failed['workbench_sync']['status'],'NOT_SYNCHRONIZED')
        snapshot=self.attach_export(self.task);state=core.workspace_status(self.task,refresh=False)
        head_before=(self.task/'review/workspace-manifest.json').read_bytes();count=len(state['index']['committed'])
        with patch.object(artifact_tools,'compile_map',side_effect=AssertionError('no recompile')),patch.object(artifact_tools,'deliver',side_effect=AssertionError('no reexport')):
            synced=workbench.sync_current_review(self.task/'reading',state['revision'])
        self.assertEqual(synced['status'],'CURRENT_REVIEW_SYNCHRONIZED');self.assertTrue(synced['still_current'])
        self.assertEqual((self.task/'review/workspace-manifest.json').read_bytes(),head_before)
        self.assertEqual(len(core.workspace_status(self.task)['index']['committed']),count)
        self.assertTrue(workbench.check_workbench(core.workspace_status(self.task,refresh=False),snapshot)['workbench_content_verified'])
        with patch.object(transport,'request',side_effect=self.request) as calls:
            old=workspace_tools.publish_committed(self.task,self.open_operation['operation_id'],args,self.open_result)
        self.assertEqual(old['workbench_sync']['status'],'SKIPPED_SUPERSEDED');calls.assert_not_called()

    def test_stale_local_token_rejects_current_sync_without_network(self):
        before=core.workspace_status(self.task,refresh=False);self.attach_export(self.task)
        with patch.object(transport,'request',side_effect=self.request) as calls,self.assertRaisesRegex(ValueError,'REVISION_CONFLICT'):
            workbench.sync_current_review(self.task,before['revision'])
        calls.assert_not_called()

    def test_pending_map_review_is_rechecked_under_binding_lock_before_any_post(self):
        task,_,_=self.prepare_task('confirmation',required=True)
        state=core.workspace_status(task,refresh=False);parent=Path(state['binding_file']).parent
        original=revision_tools.locked;changed=False
        @contextmanager
        def lock(path):
            nonlocal changed
            if Path(path)==parent and not changed:
                changed=True;self.feedback(task)
            with original(path):yield
        with patch.object(revision_tools,'locked',side_effect=lock),patch.object(transport,'request',side_effect=self.request) as calls:
            value=workbench.sync_current_review(task,state['revision'])
        self.assertEqual(value['status'],'NOT_SYNCHRONIZED');self.assertIn('Review changed',value['error']);calls.assert_not_called()

    def test_read_check_rejects_local_or_remote_advance_and_sync_reports_post_advance(self):
        snapshot=self.attach_export(self.task);state=core.workspace_status(self.task,refresh=False)
        workbench.sync_current_review(self.task,state['revision']);state=core.workspace_status(self.task,refresh=False)
        def changed_local(url):
            value=self.request(url)
            if '/snapshot?' in url:self.feedback(self.task)
            return value
        with patch.object(workbench,'request',side_effect=changed_local):checked=workbench.check_workbench(state,snapshot)
        self.assertEqual(checked['workbench_status'],'LOCAL_REVISION_CHANGED')
        state=core.workspace_status(self.task,refresh=False);gets=0
        def changed_remote(url):
            nonlocal gets
            value=self.request(url)
            if '/snapshot?' not in url:
                gets+=1
                if gets==2:value={**value,'revision_id':'0'*64}
            return value
        with patch.object(workbench,'request',side_effect=changed_remote):checked=workbench.check_workbench(state,snapshot)
        self.assertEqual(checked['workbench_status'],'REMOTE_REVISION_CHANGED')
        def advance_on_post(url,payload=None):
            if payload and payload.get('artifacts'):self.feedback(self.task)
            return self.request(url,payload)
        with patch.object(transport,'request',side_effect=advance_on_post):synced=workbench.sync_current_review(self.task,state['revision'])
        self.assertEqual(synced['status'],'SYNCHRONIZED_LOCAL_ADVANCED');self.assertFalse(synced['still_current'])

    def test_summary_link_is_read_only_and_does_not_claim_old_remote_is_current_local(self):
        state=core.workspace_status(self.task,refresh=False)
        source_only=workbench.workbench_link(state)
        self.assertEqual(source_only['link_target'],'SOURCE_WORK')
        self.assertFalse(source_only['local_content_match'])
        synced=workbench.sync_current_review(self.task,state['revision'])
        state=core.workspace_status(self.task,refresh=False)
        binding_file=Path(state['binding_file']);before=binding_file.read_bytes()
        with patch.object(workbench,'request',side_effect=AssertionError('summary is read-only without network')):
            link=workbench.workbench_link(state)
        self.assertTrue(link['local_content_match']);self.assertFalse(link['remote_current_verified'])
        self.assertEqual(link['url'],synced['workbench_sync']['url'])
        self.assertEqual(binding_file.read_bytes(),before)
        self.feedback(self.task)
        advanced=workbench.workbench_link(core.workspace_status(self.task,refresh=False))
        self.assertEqual(advanced['url'],link['url'])
        self.assertNotEqual(advanced['local_revision'],link['local_revision'])
        self.assertIsNot(advanced['local_content_match'],True)

    def test_legacy_generic_binding_gets_exact_link_without_upgrading_or_rewriting_task(self):
        state=core.workspace_status(self.task,refresh=False)
        synced=workbench.sync_current_review(self.task,state['revision'])
        state=core.workspace_status(self.task,refresh=False);path=Path(state['binding_file'])
        binding=transport.read(path)
        for field in ('synchronized_local_review_sha256','synchronized_result_revision','url','workbench_url','work_id'):
            binding.pop(field,None)
        binding['workbench_path']='/workspace?projectId=p&documentId=d'
        transport.save(path,binding)
        before={name:(self.task/name).read_bytes() for name in ('task-context.json','review/workspace-manifest.json')}
        binding_before=path.read_bytes()
        state=core.workspace_status(self.task,refresh=False)
        link=workbench.workbench_link(state)
        self.assertEqual(link['url'],synced['workbench_sync']['url'])
        self.assertIsNone(link['local_content_match'])
        self.assertEqual(path.read_bytes(),binding_before)
        self.assertEqual({name:(self.task/name).read_bytes() for name in before},before)

    def test_content_check_respects_pinned_history_instead_of_silently_checking_current(self):
        snapshot=self.attach_export(self.task);state=core.workspace_status(self.task,refresh=False)
        first=workbench.sync_current_review(self.task,state['revision'])['workbench_sync']
        state=core.workspace_status(self.task,refresh=False)
        self.feedback(self.task)
        second=workbench.sync_current_review(self.task,core.workspace_status(self.task,refresh=False)['revision'])['workbench_sync']
        self.assertNotEqual(first['revision_id'],second['revision_id'])
        state=core.workspace_status(self.task,refresh=False);state['workbench_url']=first['url']
        checked=workbench.check_workbench(state,snapshot)
        self.assertTrue(checked['workbench_content_verified'])
        self.assertEqual(checked['workbench_revision'],first['revision_id'])
        self.assertEqual(checked['remote_current_revision'],second['revision_id'])
        self.assertFalse(checked['workbench_is_current'])
        self.assertEqual(checked['workbench_url'],first['url'])

    def test_delivery_read_uses_bound_task_and_same_version_without_changing_local_review(self):
        from task_service import DeliveryReadRequest, TaskService
        from reqpilotx_server.application.agent_workflow import plugin_workbench_delivery as delivery
        self.attach_export(self.task)
        workbench.sync_current_review(self.task,core.workspace_status(self.task,refresh=False)['revision'])
        state=core.workspace_status(self.task,refresh=False);binding=transport.read(state['binding_file'])
        root=workbench._api(binding)
        view=delivery.preview_plugin_delivery_version('p','d',binding['session_id'],self.backend.sink())
        request=DeliveryReadRequest(task_id=state['context']['work_id'],view='preview',result_revision=view['revision_id'])
        before=(self.task/'review/workspace-manifest.json').read_bytes()
        with patch.object(workbench,'request',return_value=view) as http:
            result=workbench.read_workbench_delivery(state,request)
        http.assert_called_once_with(root+'/versions/preview?revision='+view['revision_id'])
        self.assertEqual(result['delivery']['snapshot'],view['snapshot'])
        self.assertFalse(result['local_artifacts_changed'])
        self.assertEqual((self.task/'review/workspace-manifest.json').read_bytes(),before)
        service=TaskService(self.root/'working',Path(__file__).resolve().parents[1]/'scripts/reqpilot.py')
        with patch.object(workbench,'request',return_value=view):
            result=service.invoke('delivery_read',request.model_dump())
        self.assertEqual(result['task_id'],state['context']['work_id'])
        for altered in ({**view,'session_id':'plugin-'+'f'*32},{**view,'revision_id':'e'*64}):
            with patch.object(workbench,'request',return_value=altered),self.assertRaises(ValueError):
                workbench.read_workbench_delivery(state,request)
        with self.assertRaises(ValueError):DeliveryReadRequest(task_id=request.task_id,view='version')
        with self.assertRaises(ValueError):DeliveryReadRequest(task_id=request.task_id,view='versions',delivery_revision=1)

    def test_delivery_read_rejects_wrong_local_binding_before_network(self):
        from task_service import DeliveryReadRequest
        state=core.workspace_status(self.task,refresh=False)
        path=Path(state['binding_file']);binding=transport.read(path)
        transport.save(path,{**binding,'work_id':'task-'+'e'*32})
        with patch.object(workbench,'request') as http,self.assertRaisesRegex(ValueError,'task'):
            workbench.read_workbench_delivery(state,DeliveryReadRequest(task_id=state['context']['work_id']))
        http.assert_not_called()
