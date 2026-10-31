"""Client handoff exercises the real shared draft API in an isolated file store."""
from argparse import Namespace
from pathlib import Path
import json
import os
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'prototype'),str(ROOT/'plugins/reqpilot/scripts')]
from tests import test_plugin_workbench as backend_fixture
from app.application.agent_workflow import plugin_workbench as api
from reqpilotx_server.application.agent_workflow import plugin_workbench_delivery as delivery_api
from task_service import TaskService
from test_portable import item_decisions
from app.application.agent_workflow import fixed_workflow as workflow
import workbench_tools as transport
import revision_tools
import artifact_tools
from app.application.agent_workflow import task_workspace

class WorkbenchTransportTests(unittest.TestCase):
    def setUp(self):
        self.backend=backend_fixture.PluginWorkbenchTests('test_two_writers_cannot_overwrite_each_other');self.backend.setUp()
        self.root=self.backend.root;self.cwd=Path.cwd();os.chdir(self.root)
        def request(url,payload=None):
            path=urlsplit(url).path;parts=path.split('/')
            project=unquote(parts[parts.index('projects')+1]);document=unquote(parts[parts.index('sor-documents')+1])
            if 'plugin-workbench' in parts and parts.index('plugin-workbench')+1 < len(parts):
                session=unquote(parts[parts.index('plugin-workbench')+1])
                if path.endswith('/snapshot'):
                    revision=parse_qs(urlsplit(url).query).get('revision',[None])[0]
                    return api.get_plugin_snapshot(project,document,session,self.backend.sink(),revision=revision)
            if 'plugin-workbench' in parts and 'versions' in parts:
                session=unquote(parts[parts.index('plugin-workbench')+1])
                if path.endswith('/versions/preview'):
                    return delivery_api.preview_plugin_delivery_version(project,document,session,self.backend.sink())
                if path.endswith('/versions') and payload is None:
                    return delivery_api.list_plugin_delivery_versions(project,document,session,self.backend.sink())
                if path.endswith('/versions') and payload is not None:
                    return delivery_api.save_plugin_delivery_version(project,document,session,
                        delivery_api.PluginDeliveryVersion(**payload),self.backend.sink())
            if payload is not None:return api.sync_plugin_draft(project,document,api.PluginDraftUpload(**payload),self.backend.sink())
            return api.get_plugin_workbench(project,document,parts[-1],self.backend.sink())
        self.request_impl=request
        import task_workbench as task_workbench_module
        self.task_workbench_module=task_workbench_module
        self.request_patch=patch.object(transport,'request',side_effect=request);self.request_patch.start()
        self.task_workbench_patch=patch.object(task_workbench_module,'request',side_effect=request);self.task_workbench_patch.start()
        def download(url):
            path=urlsplit(url).path;parts=path.split('/')
            project=unquote(parts[parts.index('projects')+1]);document=unquote(parts[parts.index('sor-documents')+1])
            session=unquote(parts[parts.index('plugin-workbench')+1]);revision=int(parts[parts.index('versions')+1])
            artifact=delivery_api.download_plugin_delivery_version(project,document,session,revision)
            return artifact.content,{key.lower():value for key,value in artifact.headers.items()}
        self.download_impl=download
        self.download_patch=patch.object(task_workbench_module,'download',side_effect=download);self.download_patch.start()
    def tearDown(self):
        self.download_patch.stop();self.request_patch.stop();self.task_workbench_patch.stop();os.chdir(self.cwd);self.backend.tearDown()
    def args(self,**values):
        fields=dict(url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d',source=None,review_dir=None,map_dir=None,baseline_dir=None,
            binding_file=None,workspace=None,require_map_confirmation=True,no_map_confirmation=None,open=False,goal='Synthetic client test')
        if values.get('workspace') is not None or values.get('no_map_confirmation'):
            fields['require_map_confirmation']=None
        fields.update(values);return Namespace(**fields)
    def test_items_wait_for_actual_map_confirmation_and_compiler_gate_creates_nothing(self):
        first=transport.sync_workbench(self.args(baseline_dir=self.root/'baseline'))
        self.assertEqual(first['status'],'WAITING_FOR_MAP_CONFIRMATION')
        waiting=transport.before_operation(Namespace(command='compile-items',map_dir=self.root/'map',baseline_dir=None))
        self.assertFalse(waiting['downstream_artifact_created'])
        state=api.get_plugin_workbench('p','d',first['session_id'],self.backend.sink())
        api.confirm_plugin_map('p','d',first['session_id'],api.PluginMapConfirmation(command_id='confirm',expected_source_sha256=self.backend.sha,
            expected_map_sha256=state['stages'][0]['artifact_sha256'],expected_revision_id=state['revision_id']),self.backend.sink())
        self.assertIsNone(transport.before_operation(Namespace(command='compile-items',map_dir=self.root/'map',baseline_dir=None)))
        with patch.object(transport,'request',side_effect=OSError('temporarily offline')):
            self.assertIsNone(transport.before_operation(Namespace(command='compile-items',map_dir=self.root/'map',baseline_dir=None)))
        second=transport.sync_workbench(self.args(baseline_dir=self.root/'baseline'))
        self.assertEqual(second['status'],'WORKBENCH_SYNCHRONIZED');self.assertEqual(len(second['stages']),2)
    def test_real_feedback_import_is_idempotent_and_other_baselines_stay_in_inbox(self):
        review=self.root/'review'
        revision_tools.open_review(Namespace(review_dir=review,baseline_dir=self.root/'baseline',source=self.backend.source))
        synced=transport.sync_workbench(self.args(review_dir=review,no_map_confirmation=True))
        state=api.get_plugin_workbench('p','d',synced['session_id'],self.backend.sink())
        sid=self.backend.artifacts['source-requirement-baseline.json']['records'][0]['source_record_id']
        api.post_plugin_feedback('p','d',synced['session_id'],api.PluginFeedback(command_id='real-feedback',expected_source_sha256=self.backend.sha,
            expected_revision_id=state['revision_id'],source_item_ids=[sid],text='请保留这个完整目标与其条件。'),self.backend.sink())
        first=transport.pull_workbench(self.args(review_dir=review));second=transport.pull_workbench(self.args(review_dir=review))
        self.assertEqual(len(first['imported_feedback_ids']),1);self.assertEqual(second['imported_feedback_ids'],[])
        head=revision_tools.load_head(review);self.assertEqual(len(head['feedback']),1);self.assertEqual(head['feedback'][0]['origin'],'USER')
        self.assertEqual(head['feedback'][0]['source_item_ids'],[sid])
    def test_sync_error_keeps_local_artifact_and_opinions_are_never_silently_omitted(self):
        transport.sync_workbench(self.args(map_dir=self.root/'map'))
        result={'status':'COMPILED','output_dir':str(self.root/'map')}
        with patch.object(transport,'request',side_effect=OSError('service unavailable')):
            actual=transport.sync_after_operation(Namespace(command='compile-map',output_dir=self.root/'map',review_dir=None),result)
        self.assertTrue(actual['workbench_sync']['local_artifact_preserved']);self.assertTrue((self.root/'map/document-map.json').is_file())
        (self.root/'baseline/manual-decisions.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'do not omit'):
            transport.source_and_artifacts(self.args(baseline_dir=self.root/'baseline'))

    def test_sync_links_pin_each_saved_result_and_open_the_uploaded_version(self):
        with patch('webbrowser.open',return_value=True) as opened:
            first=transport.sync_workbench(self.args(map_dir=self.root/'map',no_map_confirmation=True,open=True))
        opened.assert_called_once_with(first['url'])
        second=transport.sync_workbench(self.args(baseline_dir=self.root/'baseline',no_map_confirmation=True))
        self.assertNotEqual(first['revision_id'],second['revision_id'])
        for result in (first,second):
            query=parse_qs(urlsplit(result['url']).query)
            self.assertEqual(query['projectId'],['p']);self.assertEqual(query['documentId'],['d'])
            self.assertEqual(query['pluginSessionId'],[result['session_id']])
            self.assertEqual(query['resultRevision'],[result['revision_id']])
            self.assertEqual(result['link_target'],'SAVED_REMOTE_RESULT')
        first_snapshot=api.get_plugin_snapshot('p','d',first['session_id'],self.backend.sink(),revision=first['revision_id'])
        second_snapshot=api.get_plugin_snapshot('p','d',second['session_id'],self.backend.sink(),revision=second['revision_id'])
        self.assertNotEqual(first_snapshot['source_item_baseline'],second_snapshot['source_item_baseline'])
        binding=transport.read(second['binding_file'])
        self.assertEqual(binding['url'],second['url']);self.assertEqual(binding['workbench_url'],second['url'])
        self.assertEqual(binding['base_url']+binding['workbench_path'],second['url'])

    def test_source_only_link_is_explicit_work_and_pull_pins_new_remote_revision(self):
        connected=transport.sync_workbench(self.args(source=self.backend.source,no_map_confirmation=True))
        self.assertEqual(connected['link_target'],'SOURCE_WORK')
        self.assertIsNone(connected['revision_id'])
        query=parse_qs(urlsplit(connected['url']).query)
        self.assertEqual(query['pluginSessionId'],[connected['session_id']]);self.assertNotIn('resultRevision',query)
        remote=api.sync_plugin_draft('p','d',api.PluginDraftUpload(session_id=connected['session_id'],
            expected_source_sha256=self.backend.sha,expected_revision_id=None,require_map_confirmation=False,
            artifacts=self.backend.artifacts),self.backend.sink())
        pulled=transport.pull_workbench(self.args(source=self.backend.source,no_map_confirmation=True))
        self.assertEqual(pulled['revision_id'],remote['revision_id'])
        self.assertEqual(parse_qs(urlsplit(pulled['url']).query)['resultRevision'],[remote['revision_id']])
        self.assertEqual(transport.read(connected['binding_file'])['url'],pulled['url'])

    def test_unpinned_alias_cannot_be_presented_as_saved_result(self):
        first=transport.sync_workbench(self.args(map_dir=self.root/'map'))
        binding=transport.read(first['binding_file'])
        for value in ('current','latest',''):
            with self.subTest(revision=value),self.assertRaisesRegex(ValueError,'exact saved revision'):
                transport.workbench_result_url(binding,value)

    def confirm(self,synced,project='p',document='d'):
        state=api.get_plugin_workbench(project,document,synced['session_id'],self.backend.sink())
        api.confirm_plugin_map(project,document,synced['session_id'],api.PluginMapConfirmation(command_id='confirm',
            expected_source_sha256=self.backend.sha,expected_map_sha256=state['stages'][0]['artifact_sha256'],
            expected_revision_id=state['revision_id']),self.backend.sink())

    def test_custom_binding_enforces_checkpoint_and_real_compile_automatically_syncs(self):
        custom=self.root/'connections/custom.json'
        first=transport.sync_workbench(self.args(map_dir=self.root/'map',binding_file=custom))
        self.assertFalse(transport.binding_path(self.backend.sha).exists())
        compile_args=Namespace(command='compile-items',map_dir=self.root/'map',baseline_dir=None,
            input=self.root/'items-input.json',output_dir=self.root/'custom-items',binding_file=custom)
        waiting=transport.before_operation(compile_args)
        self.assertEqual(waiting['status'],'WAITING_FOR_MAP_CONFIRMATION')
        self.assertFalse(compile_args.output_dir.exists())
        self.confirm(first)
        self.assertIsNone(transport.before_operation(compile_args))
        result=artifact_tools.compile_items(compile_args)
        synced=transport.sync_after_operation(compile_args,result)['workbench_sync']
        self.assertEqual(synced['status'],'WORKBENCH_SYNCHRONIZED')
        self.assertEqual(synced['session_id'],first['session_id'])
        self.assertEqual(synced['binding_file'],str(custom.resolve()))
        self.assertEqual(len(synced['stages']),2)

    def test_missing_custom_binding_and_conflicting_source_fail_explainably(self):
        missing=self.root/'missing/custom.json'
        args=Namespace(command='compile-items',map_dir=self.root/'map',binding_file=missing)
        with self.assertRaisesRegex(ValueError,'binding does not exist'):
            transport.before_operation(args)
        result=transport.sync_after_operation(Namespace(command='compile-map',output_dir=self.root/'map',binding_file=missing),{'status':'COMPILED'})
        self.assertTrue(result['workbench_sync']['local_artifact_preserved'])
        self.assertIn('binding does not exist',result['workbench_sync']['error'])
        with self.assertRaisesRegex(ValueError,'binding does not exist'):
            transport.pull_workbench(self.args(binding_file=missing))
        custom=self.root/'custom.json'
        transport.sync_workbench(self.args(map_dir=self.root/'map',binding_file=custom))
        other=self.root/'other.pdf';other.write_bytes(b'another original')
        with self.assertRaisesRegex(ValueError,'different source'):
            transport.pull_workbench(self.args(source=other,binding_file=custom))
        with self.assertRaisesRegex(ValueError,'different workbench binding'):
            transport.pull_workbench(self.args(source=self.backend.source,binding_file=custom,document_id='other'))

    def test_native_tools_keep_bypass_even_with_bad_explicit_binding(self):
        args=Namespace(command='compile-items',map_dir=self.root/'map',binding_file=self.root/'missing.json')
        with patch.dict(os.environ,{'REQPILOT_NATIVE_AGENT_TOOLS':'1'}):
            self.assertIsNone(transport.before_operation(args))
            args.output_dir=self.root/'baseline'
            result={'status':'COMPILED'}
            self.assertIs(transport.sync_after_operation(args,result),result)
            self.assertNotIn('workbench_sync',result)

    def test_workspace_defer_sync_keeps_map_confirmation_and_defers_only_publication(self):
        first=transport.sync_workbench(self.args(map_dir=self.root/'map'))
        args=Namespace(command='compile-items',map_dir=self.root/'map',output_dir=self.root/'baseline')
        with patch.dict(os.environ,{'REQPILOT_WORKSPACE_DEFER_SYNC':'1'}):
            with patch.object(transport,'request',wraps=transport.request) as calls:
                waiting=transport.before_operation(args)
            self.assertEqual(len(calls.call_args_list),1)
            self.assertEqual(len(calls.call_args_list[0].args),1) # Read confirmation only, no POST.
            self.assertEqual(waiting['status'],'WAITING_FOR_MAP_CONFIRMATION')
            self.assertFalse(waiting['downstream_artifact_created'])
            result={'status':'COMPILED'}
            with patch.object(transport,'sync_workbench') as publish:
                self.assertIs(transport.sync_after_operation(args,result),result)
                publish.assert_not_called()
            self.assertNotIn('workbench_sync',result)
            candidate=self.root/'candidate-map';candidate.mkdir()
            changed=self.backend.changed_map('Uncommitted candidate Map')
            for name,value in changed.items():(candidate/name).write_text(json.dumps(value))
            pending=transport.before_operation(Namespace(command='compile-items',map_dir=candidate))
            self.assertEqual(pending['status'],'WAITING_FOR_MAP_CONFIRMATION')
            remote=api.get_plugin_workbench('p','d',first['session_id'],self.backend.sink())
            self.assertEqual(remote['revision_id'],first['revision_id'])
        self.confirm(first)
        with patch.dict(os.environ,{'REQPILOT_WORKSPACE_DEFER_SYNC':'1'}):
            self.assertIsNone(transport.before_operation(args))
        synced=transport.sync_after_operation(args,result)['workbench_sync']
        self.assertEqual(synced['session_id'],first['session_id'])
        self.assertEqual(len(synced['stages']),2)

    def task(self,document='d',require_confirmation=True):
        state=task_workspace.initialize_workspace(self.root/'working',self.backend.source,'整理这份合成原件并保留确认点',
            project_key='p',document_key=document,project_id='p',document_id=document,require_map_confirmation=require_confirmation)
        return Path(state['task_root'])

    def test_task_custom_binding_survives_child_cwd_and_explicit_workspace(self):
        task=self.task();custom=Path('connections/custom.json')
        first=transport.sync_workbench(self.args(workspace=task,map_dir=self.root/'map',binding_file=custom))
        context=task_workspace.find_task_workspace(explicit=task)[1]
        self.assertEqual(context['binding_file'],str(custom))
        self.assertEqual(json.loads((task/custom).read_text())['work_id'],context['work_id'])
        child=task/'reading/pages';child.mkdir(parents=True)
        os.chdir(child)
        args=Namespace(command='compile-items',map_dir=self.root/'map')
        waiting=transport.before_operation(args)
        self.assertEqual(waiting['session_id'],first['session_id'])
        self.assertFalse(waiting['downstream_artifact_created'])
        after=transport.sync_after_operation(Namespace(command='compile-map',output_dir=self.root/'map'),{'status':'COMPILED'})
        self.assertEqual(after['workbench_sync']['session_id'],first['session_id'])
        self.assertEqual(transport.pull_workbench(Namespace())['revision_id'],after['workbench_sync']['revision_id'])
        os.chdir(self.root)
        args.workspace=task
        self.assertEqual(transport.before_operation(args)['session_id'],first['session_id'])
        self.confirm(first)
        self.assertIsNone(transport.before_operation(args))
        with patch.object(transport,'request',side_effect=OSError('offline')):
            self.assertIsNone(transport.before_operation(args))
        after=transport.sync_after_operation(Namespace(command='compile-items',workspace=task,output_dir=self.root/'baseline'),{'status':'COMPILED'})
        self.assertEqual(after['workbench_sync']['session_id'],first['session_id'])
        self.assertEqual(len(after['workbench_sync']['stages']),2)
        with patch.object(transport,'request',side_effect=OSError('offline')):
            offline=transport.sync_after_operation(Namespace(command='compile-items',workspace=task,output_dir=self.root/'baseline'),{'status':'COMPILED'})
        self.assertTrue(offline['workbench_sync']['local_artifact_preserved'])
        self.assertEqual(json.loads((task/custom).read_text())['session_id'],first['session_id'])

    def test_same_original_tasks_and_business_documents_never_share_sessions_or_confirmations(self):
        a=self.task();b=self.task();c=self.task('other-doc')
        first=transport.sync_workbench(self.args(workspace=a,map_dir=self.root/'map'))
        second=transport.sync_workbench(self.args(workspace=b,map_dir=self.root/'map'))
        third=transport.sync_workbench(self.args(workspace=c,map_dir=self.root/'map',url='http://127.0.0.1:18080/workspace?projectId=p&documentId=other-doc'))
        self.assertEqual(len({first['session_id'],second['session_id'],third['session_id']}),3)
        self.confirm(first)
        self.assertIsNone(transport.before_operation(Namespace(command='compile-items',workspace=a,map_dir=self.root/'map')))
        self.assertEqual(transport.before_operation(Namespace(command='compile-items',workspace=b,map_dir=self.root/'map'))['status'],'WAITING_FOR_MAP_CONFIRMATION')
        self.assertEqual(transport.before_operation(Namespace(command='compile-items',workspace=c,map_dir=self.root/'map'))['session_id'],third['session_id'])
        # A cached confirmation from another same-source task is not permission for B.
        a_path=Path(first['binding_file']);b_path=Path(second['binding_file'])
        a_inbox=a_path.with_name(a_path.stem+'-inbox.json');b_inbox=b_path.with_name(b_path.stem+'-inbox.json')
        b_inbox.write_bytes(a_inbox.read_bytes())
        with patch.object(transport,'request',side_effect=OSError('offline')):
            blocked=transport.before_operation(Namespace(command='compile-items',workspace=b,map_dir=self.root/'map'))
        self.assertEqual(blocked['status'],'WORKBENCH_UNAVAILABLE')
        with self.assertRaisesRegex(ValueError,'another task workspace'):
            transport.pull_workbench(self.args(workspace=b,binding_file=a_path))
        with self.assertRaisesRegex(ValueError,'conflicts with the task workspace'):
            transport.sync_workbench(self.args(workspace=c,map_dir=self.root/'map'))
        # Actual feedback stays scoped to its session, even with identical source bytes.
        state=api.get_plugin_workbench('p','d',first['session_id'],self.backend.sink())
        api.post_plugin_feedback('p','d',first['session_id'],api.PluginFeedback(command_id='only-a',
            expected_source_sha256=self.backend.sha,expected_revision_id=state['revision_id'],text='仅此任务核对结构。'),self.backend.sink())
        self.assertEqual(len(transport.pull_workbench(Namespace(workspace=a))['feedback']),1)
        self.assertEqual(transport.pull_workbench(Namespace(workspace=b))['feedback'],[])
        self.assertEqual(transport.pull_workbench(Namespace(workspace=c))['feedback'],[])

    def test_unbound_task_preserves_local_results_and_only_required_checkpoint_blocks(self):
        task=self.task()
        args=Namespace(command='compile-items',workspace=task,map_dir=self.root/'map')
        blocked=transport.before_operation(args)
        self.assertEqual(blocked['status'],'WORKBENCH_NOT_BOUND')
        self.assertFalse(blocked['downstream_artifact_created'])
        after=transport.sync_after_operation(Namespace(command='compile-map',workspace=task,output_dir=self.root/'map'),{'status':'COMPILED'})
        self.assertEqual(after['workbench_sync']['status'],'NOT_BOUND')
        self.assertTrue(after['workbench_sync']['local_artifact_preserved'])
        authorized=self.task(require_confirmation=False)
        self.assertIsNone(transport.before_operation(Namespace(command='compile-items',workspace=authorized,map_dir=self.root/'map')))

    def test_task_conflicting_source_and_broken_marker_do_not_fall_back_to_legacy(self):
        task=self.task()
        other=self.root/'another.pdf';other.write_bytes(b'another source')
        with self.assertRaisesRegex(ValueError,'different task source'):
            transport.sync_workbench(self.args(workspace=task,source=other))
        marker=task/'task-context.json';context=json.loads(marker.read_text());context['work_id']='tampered'
        marker.write_text(json.dumps(context))
        with self.assertRaises(ValueError):
            transport.before_operation(Namespace(command='compile-items',workspace=task,map_dir=self.root/'map'))
        result=transport.sync_after_operation(Namespace(command='compile-map',workspace=task,output_dir=self.root/'map'),{'status':'COMPILED'})
        self.assertEqual(result['workbench_sync']['status'],'NOT_SYNCHRONIZED')
        self.assertTrue(result['workbench_sync']['local_artifact_preserved'])
        with self.assertRaisesRegex(ValueError,'no task-context.json'):
            transport.pull_workbench(self.args(workspace=self.root))

    def test_remote_revision_conflict_keeps_task_session_and_local_artifact(self):
        task=self.task(require_confirmation=False)
        first=transport.sync_workbench(self.args(workspace=task,map_dir=self.root/'map'))
        changed=api.sync_plugin_draft('p','d',api.PluginDraftUpload(session_id=first['session_id'],
            expected_source_sha256=self.backend.sha,expected_revision_id=first['revision_id'],require_map_confirmation=False,
            artifacts=self.backend.changed_map('Explicit independent saved revision')),self.backend.sink())
        result=transport.sync_after_operation(Namespace(command='compile-map',workspace=task,output_dir=self.root/'map'),{'status':'COMPILED'})
        self.assertEqual(result['workbench_sync']['status'],'NOT_SYNCHRONIZED')
        self.assertIn('another revision',result['workbench_sync']['error'])
        binding=json.loads(Path(first['binding_file']).read_text())
        self.assertEqual(binding['revision_id'],first['revision_id'])
        self.assertEqual(binding['session_id'],first['session_id'])
        self.assertEqual(api.get_plugin_workbench('p','d',first['session_id'],self.backend.sink())['revision_id'],changed['revision_id'])
        self.assertTrue((self.root/'map/document-map.json').is_file())

    def test_new_connection_defaults_autonomous_but_saved_true_binding_is_not_changed(self):
        automatic=transport.sync_workbench(self.args(baseline_dir=self.root/'baseline',require_map_confirmation=None))
        self.assertEqual(automatic['status'],'WORKBENCH_SYNCHRONIZED')
        self.assertEqual(len(automatic['stages']),2)
        self.assertFalse(transport.read(automatic['binding_file'])['require_map_confirmation'])
        legacy=self.root/'legacy-required.json'
        waiting=transport.sync_workbench(self.args(map_dir=self.root/'map',binding_file=legacy,require_map_confirmation=True))
        saved=transport.read(legacy)
        continued=transport.sync_workbench(self.args(map_dir=self.root/'map',binding_file=legacy,require_map_confirmation=None))
        self.assertEqual(continued['status'],'WAITING_FOR_MAP_CONFIRMATION')
        self.assertEqual(continued['session_id'],waiting['session_id'])
        self.assertEqual(transport.read(legacy),saved)
        self.assertIsNotNone(transport.before_operation(Namespace(command='compile-items',map_dir=self.root/'map',binding_file=legacy)))
        with self.assertRaisesRegex(ValueError,'cannot be silently changed'):
            transport.sync_workbench(self.args(map_dir=self.root/'map',binding_file=legacy,no_map_confirmation=True))
        self.confirm(waiting)
        self.assertIsNone(transport.before_operation(Namespace(command='compile-items',map_dir=self.root/'map',binding_file=legacy)))

    def workflow_task(self, partial=False, failed_check=False, missing_check=False, stale_check=False, deliver=True, fixed_workflow=True):
        import hashlib
        from pathlib import Path
        task_root = self.root / ('transport-full' if not any((partial, failed_check, missing_check, stale_check)) else
                                 'transport-partial' if partial else 'transport-failed' if failed_check else 'transport-missing' if missing_check else 'transport-stale')
        task_root.mkdir(parents=True, exist_ok=True)
        service = TaskService(task_root, ROOT/'plugins/reqpilot/scripts/reqpilot.py', ROOT)
        goal = 'Publish exact fixed workflow draft.'
        spec = workflow.workflow_spec(brief_sha256=workflow.brief_sha256(goal), evidence_mode='NONE')
        state = service.invoke('open', {'mode':'create','source':str(self.backend.source),'goal':goal,
            'creation_key':task_root.name, **({'workflow':spec} if fixed_workflow else {})})
        task = state['task_id']; state = service.invoke('prepare', {'task_id':task,'expected_revision':state['revision']})
        edit = json.loads((self.root/'map-input.json').read_text()); edit['schema_version']='reqpilot-map-edit/v1'; edit.pop('source_sha256',None)
        state = service.invoke('map', {'task_id':task,'expected_revision':state['revision'],'decisions':edit})
        choices = item_decisions(Path(state['selected']['MAP']['output_dir']))
        for key in ('source_sha256','document_map_artifact_sha256'): choices.pop(key,None)
        if partial:
            body = [{k:v for k,v in scope.items() if k != 'structure_only_spans'}
                    for scope in choices['text_decisions'] if scope['items']]
            decisions = {'retain_map_structure':True,'text_decisions':body[:1]}
        else:
            decisions = choices
        state = service.invoke('items', {'task_id':task,'expected_revision':state['revision'],'decisions':decisions})
        state = service.invoke('review', {'task_id':task,'expected_revision':state['revision']})
        if not deliver:
            inspected = service.invoke('inspect', {'task_id':task})
            local_state = service.state(task)[1]
            return task, service, inspected, Path(local_state['current_working']['review_root'])
        state = service.invoke('deliver', {'task_id':task,'expected_revision':state['revision']})
        if failed_check:
            with patch.object(__import__('revision_tools'), 'verify_review', side_effect=ValueError('synthetic failed check')):
                state = service.check(Path(state['task_root']), service.state(task)[1])
        else:
            state = service.invoke('check', {'task_id':task})
        inspected = service.invoke('inspect', {'task_id':task})
        local_state = service.state(task)[1]
        check_path = Path(inspected['task_root'])/'workflow-check.json'
        if missing_check and check_path.is_file(): check_path.unlink()
        elif stale_check:
            value=json.loads(check_path.read_text()); value['basis']['items_sha256']='0'*64; check_path.write_text(json.dumps(value))
        return task, service, inspected, Path(local_state['current_working']['review_root'])

    def test_public_task_service_publish_current_review_registers_exact_draft(self):
        import hashlib
        full_task, full_service, full_inspect, full_review = self.workflow_task()
        check = full_service.invoke('check', {'task_id':full_task})
        self.assertTrue(check['ready_to_review'])
        bound = transport.sync_workbench(self.args(workspace=Path(full_inspect['task_root']), review_dir=full_review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        session = bound['session_id']
        current_revision = full_service.invoke('inspect', {'task_id':full_task})['revision']
        synced = full_service.invoke('sync', {'task_id':full_task,'expected_revision':current_revision,'direction':'publish_current_review'})
        draft = synced['sync']['draft_delivery']; self.assertEqual(draft['status'], 'DRAFT_REGISTERED', synced)
        public = api.get_plugin_workbench('p','d',session,self.backend.sink())['fixed_workflow']
        self.assertTrue(public['ready_to_review'], public)
        versions = delivery_api.list_plugin_delivery_versions('p','d',session,self.backend.sink())['versions']
        self.assertEqual(len(versions),1); self.assertEqual(versions[0]['mode'],'DRAFT'); self.assertIsNone(versions[0]['acceptance'])
        self.assertEqual(versions[0]['delivery_sha256'], check['workflow_check']['delivery_snapshot_sha256'])
        artifact = delivery_api.download_plugin_delivery_version('p','d',session,1)
        self.assertEqual(hashlib.sha256(artifact.content).hexdigest(), versions[0]['export_sha256'])
        retry = full_service.invoke('sync', {'task_id':full_task,'expected_revision':current_revision,'direction':'publish_current_review'})
        self.assertEqual(retry['sync']['draft_delivery']['status'],'DRAFT_REUSED')
        self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',session,self.backend.sink())['versions']),1)

        # A real manual response advances the remote head; old local publication must not reset or target latest.
        view = delivery_api.get_plugin_engineer_responses('p','d',session,self.backend.sink())
        unit_id, unit = next(iter(view['units'].items()))
        delivery_api.post_plugin_engineer_responses('p','d',session,delivery_api.PluginEngineerResponse(
            command_id='transport-manual', expected_source_sha256=self.backend.sha,
            expected_revision_id=view['revision_id'], expected_version=view['version'],
            expected_response_revision=view['response_revision'], expected_baseline_sha256=view['baseline_sha256'],
            decisions=[{'unit_id':unit_id,'expected_unit_context_sha256':unit['unit_context_sha256'],
                'decision':'COMPLIANT','reason':'synthetic manual change'}]), self.backend.sink())
        stale = full_service.invoke('sync', {'task_id':full_task,'expected_revision':current_revision,'direction':'publish_current_review'})
        self.assertEqual(stale['sync']['status'],'NOT_SYNCHRONIZED')
        self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',session,self.backend.sink())['versions']),1)

        partial_task, partial_service, partial_inspect, partial_review = self.workflow_task(partial=True)
        partial_check = partial_service.invoke('check', {'task_id':partial_task})
        self.assertFalse(partial_check['ready_to_review'])
        partial_bound = transport.sync_workbench(self.args(workspace=Path(partial_inspect['task_root']), review_dir=partial_review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        partial_revision = partial_service.invoke('inspect', {'task_id':partial_task})['revision']
        lost = {'count': 0}
        def lose_draft_ack(url, payload=None):
            if url.split('?')[0].endswith('/versions') and payload is not None and lost['count'] == 0:
                self.request_impl(url, payload); lost['count'] += 1; raise OSError('draft acknowledgement lost')
            return self.request_impl(url, payload)
        import task_workbench as task_workbench_module
        with patch.object(transport, 'request', side_effect=lose_draft_ack), \
             patch.object(task_workbench_module, 'request', side_effect=lose_draft_ack):
            partial_sync = partial_service.invoke('sync', {'task_id':partial_task,'expected_revision':partial_revision,'direction':'publish_current_review'})
        self.assertEqual((partial_sync['sync']['draft_delivery'] or {}).get('status'), 'UNAVAILABLE')
        retry_partial = partial_service.invoke('sync', {'task_id':partial_task,'expected_revision':partial_revision,'direction':'publish_current_review'})
        self.assertEqual(retry_partial['sync']['draft_delivery']['status'], 'DRAFT_REUSED')
        partial_public = api.get_plugin_workbench('p','d',partial_bound['session_id'],self.backend.sink())['fixed_workflow']
        self.assertFalse(partial_public['ready_to_review'])
        self.assertEqual({s['id']:s for s in partial_public['stages']}['ITEMS_CLASSIFICATION']['status'],'IN_PROGRESS')
        self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',partial_bound['session_id'],self.backend.sink())['versions']),1)

        for name, options in (('failed', {'failed_check':True}), ('missing', {'missing_check':True}), ('stale', {'stale_check':True})):
            task, service, inspected, review = self.workflow_task(**options)
            check = service.invoke('inspect', {'task_id':task})
            bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']), review_dir=review,
                url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
            current_revision = service.invoke('inspect', {'task_id':task})['revision']
            result = service.invoke('sync', {'task_id':task,'expected_revision':current_revision,'direction':'publish_current_review'})
            self.assertNotEqual(((result.get('sync') or {}).get('draft_delivery') or {}).get('status'),'DRAFT_REGISTERED')
            self.assertFalse(service.invoke('inspect', {'task_id':task})['workflow']['ready_to_review'], name)
            public = api.get_plugin_workbench('p','d',bound['session_id'],self.backend.sink())['fixed_workflow']
            self.assertFalse(public['ready_to_review'], name)
            self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',bound['session_id'],self.backend.sink())['versions']),0)

    def test_malformed_map_history_survives_reload_without_blocking_valid_successor(self):
        import hashlib
        from copy import deepcopy
        task_root = self.root / 'transport-failed-map-successor'
        task_root.mkdir(parents=True, exist_ok=True)
        service = TaskService(task_root, ROOT/'plugins/reqpilot/scripts/reqpilot.py', ROOT)
        goal = 'Recover from one malformed Map attempt and publish the fixed workflow draft.'
        spec = workflow.workflow_spec(brief_sha256=workflow.brief_sha256(goal), evidence_mode='NONE')
        state = service.invoke('open', {'mode':'create','source':str(self.backend.source),'goal':goal,
            'creation_key':'failed-map-successor','workflow':spec})
        task = state['task_id']; state = service.invoke('prepare', {'task_id':task,'expected_revision':state['revision']})
        edit = json.loads((self.root/'map-input.json').read_text())
        edit['schema_version'] = 'reqpilot-map-edit/v1'; edit.pop('source_sha256', None)
        malformed = deepcopy(edit); malformed['nodes'][1]['content_source_ids'] = ['guessed-source']
        failed = service.invoke('map', {'task_id':task,'expected_revision':state['revision'],'decisions':malformed})
        self.assertEqual(failed['status'], 'FAILED')
        self.assertEqual(failed['operation_state'], 'FAILED')
        failed_id = failed['operation_id']
        inspected = service.invoke('inspect', {'task_id':task})
        self.assertEqual(inspected['incomplete_operations'], [])
        self.assertNotIn('INCOMPLETE_OPERATION', inspected['workflow']['open_items'])
        self.assertEqual(inspected['failed_operations'][0]['operation_id'], failed_id)
        self.assertIn('guessed-source', inspected['failed_operations'][0]['error'])

        state = service.invoke('map', {'task_id':task,'expected_revision':inspected['revision'],'decisions':edit})
        self.assertEqual(state['operation_state'], 'COMMITTED')
        choices = item_decisions(Path(state['selected']['MAP']['output_dir']))
        for key in ('source_sha256','document_map_artifact_sha256'): choices.pop(key,None)
        state = service.invoke('items', {'task_id':task,'expected_revision':state['revision'],'decisions':choices})
        state = service.invoke('review', {'task_id':task,'expected_revision':state['revision']})
        state = service.invoke('deliver', {'task_id':task,'expected_revision':state['revision']})
        check = service.invoke('check', {'task_id':task})
        self.assertTrue(check['ready_to_review'])
        inspected = service.invoke('inspect', {'task_id':task})
        self.assertEqual(inspected['incomplete_operations'], [])
        self.assertEqual(inspected['failed_operations'][0]['operation_id'], failed_id)
        bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']),
            review_dir=Path(service.state(task)[1]['current_working']['review_root']),
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        revision = service.invoke('inspect', {'task_id':task})['revision']
        synced = service.invoke('sync', {'task_id':task,'expected_revision':revision,'direction':'publish_current_review'})
        self.assertEqual(synced['sync']['draft_delivery']['status'], 'DRAFT_REGISTERED')
        public = api.get_plugin_workbench('p','d',bound['session_id'],self.backend.sink())['fixed_workflow']
        self.assertTrue(public['ready_to_review'], public)
        reloaded = TaskService(task_root, ROOT/'plugins/reqpilot/scripts/reqpilot.py', ROOT)
        after = reloaded.invoke('inspect', {'task_id':task})
        self.assertTrue(after['workflow']['ready_to_review'])
        self.assertEqual(after['incomplete_operations'], [])
        self.assertEqual(after['failed_operations'][0]['operation_id'], failed_id)
        self.assertIn('guessed-source', after['failed_operations'][0]['error'])
        self.assertTrue((Path(after['task_root'])/'operations'/(failed_id+'.json')).is_file())

    def test_partial_draft_remains_available_without_full_workflow_handoff(self):
        task, service, inspected, review = self.workflow_task(partial=True, deliver=False)
        bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']), review_dir=review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        revision = service.invoke('inspect', {'task_id':task})['revision']
        result = service.invoke('deliver', {'task_id':task,'expected_revision':revision,'command_id':'partial-draft'})
        self.assertTrue(result['local_artifact_ready'])
        self.assertFalse(result['ready_to_review'])
        self.assertFalse(result['handoff_ready'])
        self.assertFalse(result['workbench_handoff']['ready'])
        self.assertEqual(result['workbench_handoff']['draft_status'], 'DRAFT_REGISTERED')
        self.assertFalse(api.get_plugin_workbench('p','d',bound['session_id'],self.backend.sink())['fixed_workflow']['ready_to_review'])
        self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',bound['session_id'],self.backend.sink())['versions']),1)

    def test_non_workflow_task_preserves_existing_verified_handoff(self):
        task, service, inspected, review = self.workflow_task(deliver=False, fixed_workflow=False)
        bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']), review_dir=review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        revision = service.invoke('inspect', {'task_id':task})['revision']
        result = service.invoke('deliver', {'task_id':task,'expected_revision':revision,'command_id':'portable-delivery'})
        self.assertTrue(result['verification']['workbench_content_verified'], result)
        self.assertTrue(result['handoff_ready'], result)
        self.assertNotIn('workbench_handoff', result)
        self.assertEqual(delivery_api.list_plugin_delivery_versions('p','d',bound['session_id'],self.backend.sink())['versions'], [])

    def test_bound_deliver_replay_and_check_publish_do_not_mint_new_revisions(self):
        task, service, inspected, review = self.workflow_task(deliver=False)
        bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']), review_dir=review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        revision = service.invoke('inspect', {'task_id':task})['revision']
        request = {'task_id':task,'expected_revision':revision,'command_id':'bound-deliver-one'}
        first = service.invoke('deliver', request)
        self.assertTrue(first['handoff_ready'], first)
        self.assertEqual(first['workbench_handoff']['draft_status'],'DRAFT_REGISTERED')
        session = bound['session_id']
        remote_before = api.get_plugin_workbench('p','d',session,self.backend.sink())
        versions_before = delivery_api.list_plugin_delivery_versions('p','d',session,self.backend.sink())['versions']
        self.assertEqual(len(versions_before),1); self.assertIsNone(versions_before[0]['acceptance'])
        local_before = service.invoke('inspect', {'task_id':task})['revision']
        replay = service.invoke('deliver', request)
        self.assertEqual(replay['revision'], first['revision'])
        self.assertEqual(service.invoke('inspect', {'task_id':task})['revision'], local_before)
        self.assertEqual(api.get_plugin_workbench('p','d',session,self.backend.sink())['revision_id'], remote_before['revision_id'])
        self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',session,self.backend.sink())['versions']),1)
        checked = service.invoke('check', {'task_id':task})
        self.assertTrue(checked['ready_to_review'])
        synced = service.invoke('sync', {'task_id':task,'expected_revision':local_before,'direction':'publish_current_review'})
        self.assertEqual(synced['sync']['draft_delivery']['status'],'DRAFT_REUSED')
        self.assertEqual(synced['sync']['draft_delivery']['delivery']['delivery_sha256'],versions_before[0]['delivery_sha256'])
        self.assertEqual(api.get_plugin_workbench('p','d',session,self.backend.sink())['revision_id'], remote_before['revision_id'])
        self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',session,self.backend.sink())['versions']),1)

    def test_deliver_handoff_materializes_exact_retained_draft_bytes_for_restart(self):
        import hashlib
        task, service, inspected, review = self.workflow_task(deliver=False)
        bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']), review_dir=review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        revision = service.invoke('inspect', {'task_id':task})['revision']
        result = service.invoke('deliver', {'task_id':task,'expected_revision':revision,'command_id':'authoritative-draft'})
        handoff = result['delivery_handoff']
        self.assertEqual(handoff['status'], 'AUTHORITATIVE_DRAFT_READY', result)
        self.assertEqual(handoff['currentness'], 'CURRENT')
        self.assertTrue(handoff['durable']); self.assertTrue(handoff['exact_download']); self.assertTrue(result['exact_download_ready'])
        self.assertTrue(result['workbench_handoff']['still_current']); self.assertTrue(result['workbench_handoff']['ready'])
        path = Path(handoff['path']); self.assertTrue(path.is_file())
        versions = delivery_api.list_plugin_delivery_versions('p','d',bound['session_id'],self.backend.sink())['versions']
        authoritative = delivery_api.download_plugin_delivery_version('p','d',bound['session_id'],versions[0]['revision'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), versions[0]['export_sha256'])
        self.assertEqual(path.read_bytes(), authoritative.content)
        self.assertEqual(handoff['result_revision'], versions[0]['revision_id'])
        self.assertEqual(handoff['delivery_sha256'], versions[0]['delivery_sha256'])
        self.assertEqual(handoff['source_sha256'], self.backend.sha)
        receipt = json.loads(Path(handoff['receipt_path']).read_text())
        self.assertEqual(receipt['origin'], 'RETAINED_DRAFT_DOWNLOAD')
        self.assertEqual(receipt['human_acceptance'], 'NOT_CLAIMED')
        # The local export is the program's own workbook; it is not silently labelled as the remote retained bytes.
        self.assertEqual(handoff['local_export_matches_authoritative'], hashlib.sha256((Path(result['result']['output_dir'])/'matrix.xlsx').read_bytes()).hexdigest() == versions[0]['export_sha256'])
        with patch.object(self.task_workbench_module,'download',side_effect=OSError('managed backend restarted')):
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), authoritative.content)

    def test_deliver_handoff_rechecks_currentness_after_download(self):
        import hashlib
        task, service, inspected, review = self.workflow_task(deliver=False)
        bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']), review_dir=review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        revision = service.invoke('inspect', {'task_id':task})['revision']
        captured = {}
        def download_then_manual(url):
            captured['versions'] = delivery_api.list_plugin_delivery_versions('p','d',bound['session_id'],self.backend.sink())['versions']
            data, headers = self.download_impl(url)
            view = delivery_api.get_plugin_engineer_responses('p','d',bound['session_id'],self.backend.sink())
            unit_id, unit = next(iter(view['units'].items()))
            delivery_api.post_plugin_engineer_responses('p','d',bound['session_id'],delivery_api.PluginEngineerResponse(
                command_id='midflight-manual', expected_source_sha256=self.backend.sha,
                expected_revision_id=view['revision_id'], expected_version=view['version'],
                expected_response_revision=view['response_revision'], expected_baseline_sha256=view['baseline_sha256'],
                decisions=[{'unit_id':unit_id,'expected_unit_context_sha256':unit['unit_context_sha256'],
                    'decision':'COMPLIANT','reason':'synthetic mid-download response'}]), self.backend.sink())
            return data, headers
        with patch.object(self.task_workbench_module,'download',side_effect=download_then_manual):
            result = service.invoke('deliver', {'task_id':task,'expected_revision':revision,'command_id':'authoritative-race'})
        handoff = result['delivery_handoff']
        self.assertEqual(handoff['status'], 'AUTHORITATIVE_DRAFT_STALE', result)
        self.assertEqual(handoff['currentness'], 'STALE'); self.assertTrue(handoff['stale']); self.assertTrue(handoff['refresh_required'])
        self.assertTrue(handoff['durable']); self.assertTrue(handoff['exact_download'])
        self.assertFalse(result['exact_download_ready']); self.assertFalse(result['handoff_ready'])
        self.assertFalse(result['workbench_handoff']['still_current']); self.assertFalse(result['workbench_handoff']['remote_current'])
        self.assertFalse(result['workbench_handoff']['ready']); self.assertFalse(result['workbench_handoff']['exact_download_ready'])
        path = Path(handoff['path']); self.assertTrue(path.is_file())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), captured['versions'][0]['export_sha256'])
        self.assertEqual(handoff['export_sha256'], captured['versions'][0]['export_sha256'])

    def test_manual_change_after_draft_commit_preserves_history_without_current_handoff(self):
        task, service, inspected, review = self.workflow_task(deliver=False)
        bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']), review_dir=review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        revision = service.invoke('inspect', {'task_id':task})['revision']
        lists = {'count':0}
        def advance_during_final_list(url, payload=None):
            if payload is None and url.split('?')[0].endswith('/versions'):
                lists['count'] += 1
                if lists['count'] == 2:
                    view = delivery_api.get_plugin_engineer_responses('p','d',bound['session_id'],self.backend.sink())
                    unit_id, unit = next(iter(view['units'].items()))
                    delivery_api.post_plugin_engineer_responses('p','d',bound['session_id'],delivery_api.PluginEngineerResponse(
                        command_id='after-draft-manual', expected_source_sha256=self.backend.sha,
                        expected_revision_id=view['revision_id'], expected_version=view['version'],
                        expected_response_revision=view['response_revision'], expected_baseline_sha256=view['baseline_sha256'],
                        decisions=[{'unit_id':unit_id,'expected_unit_context_sha256':unit['unit_context_sha256'],
                            'decision':'COMPLIANT','reason':'synthetic concurrent response'}]),self.backend.sink())
            return self.request_impl(url,payload)
        with patch.object(self.task_workbench_module,'request',side_effect=advance_during_final_list):
            result = service.invoke('deliver', {'task_id':task,'expected_revision':revision,'command_id':'draft-then-manual'})
        self.assertFalse(result['handoff_ready'], result)
        self.assertEqual(result['workbench_handoff']['draft_status'], 'CONFLICT')
        versions = delivery_api.list_plugin_delivery_versions('p','d',bound['session_id'],self.backend.sink())['versions']
        self.assertEqual(len(versions),1)
        self.assertIsNone(versions[0]['acceptance'])
        current = api.get_plugin_workbench('p','d',bound['session_id'],self.backend.sink())
        self.assertNotEqual(versions[0]['revision_id'],current['revision_id'])

    def test_precommit_publication_failure_is_not_handoff_and_retry_recovers(self):
        task, service, inspected, review = self.workflow_task(deliver=False)
        bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']), review_dir=review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        revision = service.invoke('inspect', {'task_id':task})['revision']
        import task_workbench as task_workbench_module
        def fail_before_commit(url, payload=None):
            if payload is not None and url.split('?')[0].endswith('/versions'):
                raise OSError('precommit publication unavailable')
            return self.request_impl(url, payload)
        with patch.object(task_workbench_module,'request',side_effect=fail_before_commit):
            failed = service.invoke('deliver', {'task_id':task,'expected_revision':revision,'command_id':'bound-deliver-fail'})
        self.assertFalse(failed['handoff_ready'], failed)
        self.assertEqual(failed['local_delivery_ready'], True)
        self.assertEqual(failed['workbench_handoff']['draft_status'],'UNAVAILABLE')
        self.assertFalse(api.get_plugin_workbench('p','d',bound['session_id'],self.backend.sink())['fixed_workflow']['ready_to_review'])
        self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',bound['session_id'],self.backend.sink())['versions']),0)
        current_revision = service.invoke('inspect', {'task_id':task})['revision']
        recovered = service.invoke('sync', {'task_id':task,'expected_revision':current_revision,'direction':'publish_current_review'})
        self.assertEqual(recovered['sync']['draft_delivery']['status'],'DRAFT_REGISTERED')
        self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',bound['session_id'],self.backend.sink())['versions']),1)

    def test_manual_change_between_preview_and_list_is_conflict_not_reuse(self):
        task, service, inspected, review = self.workflow_task(deliver=False)
        bound = transport.sync_workbench(self.args(workspace=Path(inspected['task_root']), review_dir=review,
            url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d', no_map_confirmation=True))
        import task_workbench as task_workbench_module
        def fail_publication(url, payload=None):
            if payload is not None and url.split('?')[0].endswith('/versions'):
                raise OSError('precommit race setup')
            return self.request_impl(url, payload)
        revision = service.invoke('inspect', {'task_id':task})['revision']
        with patch.object(task_workbench_module,'request',side_effect=fail_publication):
            failed = service.invoke('deliver', {'task_id':task,'expected_revision':revision,'command_id':'race-deliver'})
        self.assertFalse(failed['handoff_ready'], failed)
        self.assertEqual(failed['local_delivery_ready'], True)
        checked = service.invoke('check', {'task_id':task})
        self.assertTrue(checked['ready_to_review'], checked)
        revision = service.invoke('inspect', {'task_id':task})['revision']
        armed = {'value': True}
        def race(url, payload=None):
            if armed['value'] and url.split('?')[0].endswith('/versions') and payload is None:
                armed['value'] = False
                view = delivery_api.get_plugin_engineer_responses('p','d',bound['session_id'],self.backend.sink())
                unit_id, unit = next(iter(view['units'].items()))
                delivery_api.post_plugin_engineer_responses('p','d',bound['session_id'],delivery_api.PluginEngineerResponse(
                    command_id='race-manual', expected_source_sha256=self.backend.sha,
                    expected_revision_id=view['revision_id'], expected_version=view['version'],
                    expected_response_revision=view['response_revision'], expected_baseline_sha256=view['baseline_sha256'],
                    decisions=[{'unit_id':unit_id,'expected_unit_context_sha256':unit['unit_context_sha256'],
                        'decision':'COMPLIANT','reason':'synthetic race change'}]), self.backend.sink())
            return self.request_impl(url, payload)
        with patch.object(task_workbench_module,'request',side_effect=race):
            result = service.invoke('sync', {'task_id':task,'expected_revision':revision,'direction':'publish_current_review'})
        draft = (result.get('sync') or {}).get('draft_delivery') or {}
        self.assertEqual(draft.get('status'),'CONFLICT', result)
        self.assertEqual(draft.get('reason'),'WORKBENCH_REVISION_CHANGED')
        self.assertEqual(len(delivery_api.list_plugin_delivery_versions('p','d',bound['session_id'],self.backend.sink())['versions']),0)
