
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import tempfile
from pathlib import Path
import unittest

from app.application.agent_workflow.native_task_brief import build_task_brief,render_task_goal,save_task_brief


class TaskBriefTests(unittest.TestCase):
    def test_new_map_has_real_policy_and_no_invented_saved_work(self):
        brief=build_task_brief(goal='Only map the authorized range.',origin='USER_REQUEST',
            source={'path':'source.pdf'},require_map_confirmation=True,
            analysis_policy={'status':'SKIPPED','reason':'NO_INTERNAL_EVIDENCE'},timeout_seconds=900)
        self.assertEqual(brief['work_state'],'SOURCE_TO_MAP')
        self.assertEqual(brief['saved_artifacts'],[])
        self.assertEqual(brief['map_confirmation'],'REQUIRED_AFTER_MAP')
        self.assertEqual(brief['run_limit_seconds'],900)
        self.assertIsNone(brief['workbench_path'])

    def test_saved_draft_is_not_acceptance_and_changed_map_invalidates_old_confirmation(self):
        args={'goal':'Revise the map only.','origin':'AGENT_SELF_REVIEW','require_map_confirmation':True,
            'stages':{'MAP':{'artifact_sha256':'a'*64,'count':5,'path':'map/document-map.json'}}}
        draft=build_task_brief(**args)
        self.assertEqual(draft['map_confirmation'],'REQUIRED_BEFORE_ITEMS')
        confirmed=build_task_brief(**args,confirmed_map_sha256='a'*64)
        self.assertEqual(confirmed['map_confirmation'],'CONFIRMED')
        args['stages']['MAP']['artifact_sha256']='b'*64
        args['stages']['ITEMS']={'map_sha256':'a'*64,'artifact_sha256':'c'*64,'count':10}
        changed=build_task_brief(**args,confirmed_map_sha256='a'*64)
        self.assertEqual(changed['map_confirmation'],'REQUIRED_BEFORE_ITEMS')
        self.assertEqual([x['stage'] for x in changed['saved_artifacts']],['MAP'])
        self.assertIsNone(changed['confirmed_map_sha256'])

    def test_review_preserves_request_focus_and_reports_unknown_instead_of_fabricating_policy(self):
        brief=build_task_brief(goal='Correct only this classification.',initial_goal='Prepare a full Matrix.',
            origin='USER_REQUEST',mode='REVIEW',review={'version_path':'versions/v2','baseline_sha256':'b'*64},
            focus_ids=['SRC-7'],project_id='project',document_id='document')
        self.assertEqual(brief['work_state'],'WORKING_REVIEW')
        self.assertEqual(brief['current_request'],'Correct only this classification.')
        self.assertEqual(brief['focus_source_item_ids'],['SRC-7'])
        self.assertEqual(brief['analysis_policy'],{'status':'UNKNOWN'})
        self.assertIn('checkpoint=list',brief['workbench_path'])
        rendered=render_task_goal(brief['current_request'],origin='USER_REQUEST',brief=brief)
        self.assertLess(rendered.index(brief['current_request']),rendered.index(brief['initial_request']))

    def test_source_host_context_refreshes_confirmation_without_restarting(self):
        from app.application.agent_workflow.source_document_loop_jobs import SourceDocumentToolHost
        repo=Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);workspace=root/'workspace';workspace.mkdir()
            source_bytes=b'%PDF-synthetic task context fixture'
            (workspace/'source.pdf').write_bytes(source_bytes)
            (workspace/'task-input.json').write_text(json.dumps({'source':{'path':'source.pdf','sha256':hashlib.sha256(source_bytes).hexdigest()}}))
            (root/'stage-results.json').write_text(json.dumps({'MAP':{'artifact_sha256':'a'*64,'count':5,'path':'map.json'}}))
            host=SourceDocumentToolHost(task_root=root,meta={'request':{'require_map_confirmation':True}},
                workspace=workspace,plugin_root=repo/'plugins/reqpilot',project_root=repo)
            host.set_task_context(goal='Review this structure.',origin='USER_REQUEST',mode='INITIAL_SOURCE')
            def read():
                result=host.invoke('reqpilot_context',{});self.assertTrue(result['success'],result)
                return json.loads(result['content_items'][0]['text'])['task_brief']
            self.assertEqual(read()['map_confirmation'],'REQUIRED_BEFORE_ITEMS')
            (root/'map-confirmation.json').write_text(json.dumps({'map_sha256':'a'*64}))
            self.assertEqual(read()['map_confirmation'],'CONFIRMED')

    def test_first_assignment_persists_exact_input_in_a_new_task_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)/'new-task'
            brief=build_task_brief(goal='Review this source.',origin='USER_REQUEST')
            goal=render_task_goal(brief['current_request'],origin='USER_REQUEST',brief=brief)
            path=save_task_brief(root,brief,goal)
            self.assertEqual(json.loads(path.read_text()),{'task_brief':brief,'sent_goal':goal})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_unconfirmed_revised_map_routes_to_structure_even_with_an_older_review(self):
        brief=build_task_brief(goal='Fix this structure.',origin='USER_REQUEST',
            stages={'MAP':{'artifact_sha256':'b'*64}},review={'version_path':'versions/old'},
            require_map_confirmation=True,confirmed_map_sha256='a'*64,project_id='p',document_id='d')
        self.assertEqual(brief['work_state'],'MAP_DRAFT')
        self.assertIn('checkpoint=structure',brief['workbench_path'])


if __name__=='__main__':unittest.main()
