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
import workbench_tools as transport
import revision_tools
import artifact_tools
from app.application.agent_workflow import task_workspace

class WorkbenchTransportTests(unittest.TestCase):
    def setUp(self):
        self.backend=backend_fixture.PluginWorkbenchTests('test_two_writers_cannot_overwrite_each_other');self.backend.setUp()
        self.root=self.backend.root;self.cwd=Path.cwd();os.chdir(self.root)
        def request(url,payload=None):
            parts=urlsplit(url).path.split('/')
            project=unquote(parts[parts.index('projects')+1]);document=unquote(parts[parts.index('sor-documents')+1])
            if payload is not None:return api.sync_plugin_draft(project,document,api.PluginDraftUpload(**payload),self.backend.sink())
            return api.get_plugin_workbench(project,document,parts[-1],self.backend.sink())
        self.request_patch=patch.object(transport,'request',side_effect=request);self.request_patch.start()
    def tearDown(self):
        self.request_patch.stop();os.chdir(self.cwd);self.backend.tearDown()
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
