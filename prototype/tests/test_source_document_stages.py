import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow import source_document_stages as stages
from app.application.agent_workflow.source_document_loop_jobs import SourceDocumentToolHost

ROOT = Path(__file__).resolve().parents[2]

class SourceDocumentStageTests(unittest.TestCase):
    def setup_host(self, root, confirmed=False):
        workspace=root/'workspace';(workspace/'map').mkdir(parents=True)
        from .test_source_front_matter import FrontMatterTests
        mapping,_,_=FrontMatterTests().prepare()
        (workspace/'map/document-map.json').write_text(mapping.model_dump_json())
        meta={'source_sha256':mapping.source_sha256,'workflow_version':1,
              'request':{'require_map_confirmation':True},'analysis_policy':{'status':'SKIPPED','reason':'NO_INTERNAL_EVIDENCE'},
              'plugin_bundle_sha256':'a'*64}
        host=SourceDocumentToolHost(task_root=root,meta=meta,workspace=workspace,plugin_root=ROOT/'plugins/reqpilot',project_root=ROOT)
        pointer=stages.publish_stage(root,workspace,'compile-map',{'output_dir':'map'},mapping.source_sha256)
        if confirmed:(root/'map-confirmation.json').write_text(json.dumps({'map_sha256':mapping.artifact_sha256}))
        return host,pointer,mapping

    def test_map_is_readable_before_any_source_item_or_final_review(self):
        with TemporaryDirectory() as tmp:
            host,pointer,mapping=self.setup_host(Path(tmp))
            saved,artifact=stages.read_stage(Path(tmp),'MAP')
            self.assertEqual(saved['artifact_sha256'],mapping.artifact_sha256)
            self.assertEqual(artifact['nodes'][-1]['source_content'][0]['text'],mapping.nodes[-1].source_content[0].text)
            self.assertFalse((host.workspace/'review').exists())
            self.assertEqual(list(stages.stage_index(Path(tmp))),['MAP'])

    def test_unconfirmed_map_and_empty_knowledge_do_not_run_dependent_tools(self):
        with TemporaryDirectory() as tmp:
            host,_,_=self.setup_host(Path(tmp))
            with patch('app.application.agent_workflow.native_loop_tools.subprocess.run') as run:
                blocked=host.invoke('reqpilot_run',{'operation':'compile-items','arguments':{'map_dir':'map'}})
                skipped=host.invoke('reqpilot_run',{'operation':'analysis-packet','arguments':{}})
            self.assertEqual(json.loads(blocked['content_items'][0]['text'])['status'],'WAITING_FOR_MAP_CONFIRMATION')
            self.assertEqual(json.loads(skipped['content_items'][0]['text'])['status'],'SKIPPED')
            run.assert_not_called()

    def test_confirmation_must_match_the_map_actually_used(self):
        with TemporaryDirectory() as tmp:
            host,_,mapping=self.setup_host(Path(tmp),confirmed=True)
            changed=host.workspace/'old';changed.mkdir()
            value=mapping.model_dump(mode='json');value['artifact_sha256']='b'*64
            (changed/'document-map.json').write_text(json.dumps(value))
            with patch('app.application.agent_workflow.native_loop_tools.subprocess.run') as run:
                result=host.invoke('reqpilot_run',{'operation':'compile-items','arguments':{'map_dir':'old'}})
            self.assertEqual(json.loads(result['content_items'][0]['text'])['status'],'WAITING_FOR_MAP_CONFIRMATION')
            run.assert_not_called()

    def test_stage_content_tampering_and_cross_source_publication_are_rejected(self):
        with TemporaryDirectory() as tmp:
            host,pointer,mapping=self.setup_host(Path(tmp))
            with self.assertRaisesRegex(ValueError,'another original'):
                stages.publish_stage(Path(tmp),host.workspace,'compile-map',{'output_dir':'map'},'b'*64)
            (host.workspace/'map/document-map.json').write_text('{}')
            # The producer can reuse its output directory without changing a
            # saved stage. Only mutation of the registered snapshot is tampering.
            self.assertEqual(stages.read_stage(Path(tmp),'MAP')[0]['artifact_sha256'],mapping.artifact_sha256)
            (host.workspace/pointer['path']).write_text('{}')
            with self.assertRaisesRegex(ValueError,'changed after publication'):
                stages.read_stage(Path(tmp),'MAP')

    def test_analysis_branch_is_explicit_and_does_not_invent_an_attempt(self):
        self.assertEqual(stages.analysis_policy({})['status'],'SKIPPED')
        self.assertEqual(stages.analysis_policy({'evidence_catalog':{'e1':{}}})['status'],'AVAILABLE')
        self.assertEqual(stages.analysis_policy({'documents':[{}]},'SKIP')['reason'],'USER_SKIPPED')


class CompiledStagePairTests(unittest.TestCase):
    """Exercise production compilers, stage reads/API and briefs with a real PDF."""
    def setUp(self):
        from argparse import Namespace
        from copy import deepcopy
        from .test_plugin_workbench import make_fixture, item_decisions, artifact_tools, write
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root/'workspace'
        self.workspace.mkdir()
        self.source, capture, decisions = make_fixture(self.workspace/'fixture')
        self.maps, self.items = {}, {}
        for suffix in ('a', 'b'):
            chosen = deepcopy(decisions)
            if suffix == 'b':
                # Same source, independently compiled valid Map with different
                # structure identities; do not forge an artifact hash.
                chosen['nodes'][0]['node_id'] = 'main-b'
                for node in chosen['nodes'][1:]:
                    node['parent_id'] = 'main-b'
            inputs = self.workspace/f'map-{suffix}-input.json'
            write(inputs, chosen)
            output = self.workspace/f'map-{suffix}'
            artifact_tools.compile_map(Namespace(source=self.source, source_ir=capture/'source_ir.json', input=inputs, output_dir=output))
            self.maps[suffix] = json.loads((output/'document-map.json').read_text())
            inputs = self.workspace/f'items-{suffix}-input.json'
            write(inputs, item_decisions(output))
            result = self.workspace/f'items-{suffix}'
            artifact_tools.compile_items(Namespace(map_dir=output, input=inputs, baseline_dir=None, output_dir=result))
            self.items[suffix] = json.loads((result/'source-requirement-baseline.json').read_text())
        self.sha = self.maps['a']['source_sha256']
        self.assertEqual(len(self.items['a']['records']), 8)
        self.assertNotEqual(self.maps['a']['artifact_sha256'], self.maps['b']['artifact_sha256'])
        self.meta = {'source_sha256': self.sha, 'workflow_version': 1,
            'task_id': 'loop-'+'a'*32, 'origin': 'USER_REQUEST',
            'project_id': 'p', 'document_id': 'd',
            'request': {'require_map_confirmation': False, 'goal': 'Prepare source-faithful items.'},
            'plugin_bundle_sha256': 'a'*64}
        (self.root/'task.json').write_text(json.dumps(self.meta))
        self.host = SourceDocumentToolHost(task_root=self.root, meta=self.meta,
            workspace=self.workspace, plugin_root=ROOT/'plugins/reqpilot', project_root=ROOT)
        self.host.set_task_context(goal=self.meta['request']['goal'], origin='USER_REQUEST', mode='SOURCE')

    def publish(self, stage, suffix):
        return stages.publish_stage(self.root, self.workspace, 'compile-'+stage,
            {'output_dir': f'{stage}-{suffix}'}, self.sha)

    def get(self, stage, **kwargs):
        from app.application.agent_workflow import source_document_loop_jobs as api
        with patch.object(api, '_raw_existing', return_value=(None, self.root, self.meta)):
            return api.get_source_document_stage('p', 'd', self.meta['task_id'], stage,
                SimpleNamespace(headers={}), **kwargs)

    def test_items_before_map_are_saved_then_uniquely_paired_when_map_arrives(self):
        saved = self.publish('items', 'a')
        self.assertFalse(saved['current'])
        self.assertEqual(saved['retention_reason'], 'MAP_NOT_REGISTERED')
        self.assertEqual(stages.stage_index(self.root), {})
        self.assertEqual(self.host.task_brief()['work_state'], 'SOURCE_TO_MAP')
        brief = self.host.task_brief()
        self.assertEqual(brief['retained_stage_artifacts'][0]['artifact_sha256'], saved['artifact_sha256'])
        self.assertEqual(brief['retained_stage_artifacts'][0]['retention_reason'], 'MAP_NOT_REGISTERED')
        from app.application.agent_workflow.native_task_brief import render_task_goal
        self.assertIn('非当前阶段', render_task_goal(brief['current_request'], origin='USER_REQUEST', brief=brief))
        explicit = self.get('ITEMS', artifact_sha256=saved['artifact_sha256'])
        self.assertEqual(explicit['total'], 8)
        self.assertFalse(explicit['current'])
        self.publish('map', 'a')
        self.assertEqual(self.get('ITEMS')['pairing_status'], 'PAIRED')
        self.assertEqual(self.host.task_brief()['work_state'], 'ITEMS_SAVED')

    def test_old_map_items_cannot_replace_current_map_pair_but_remain_readable(self):
        from app.application.errors import ApplicationError
        self.publish('map', 'a'); original = self.publish('items', 'a')
        self.publish('map', 'b')
        late = self.publish('items', 'a')
        self.assertEqual(late['retention_reason'], 'MAP_MISMATCH')
        self.assertFalse(late['current'])
        self.assertEqual(list(stages.stage_index(self.root)), ['MAP'])
        with self.assertRaises(ApplicationError) as caught:
            self.get('ITEMS')
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.host.task_brief()['work_state'], 'MAP_SAVED')
        self.assertEqual([item['stage'] for item in self.host.task_brief()['saved_artifacts']], ['MAP'])
        retained = self.get('ITEMS', artifact_sha256=original['artifact_sha256'])
        self.assertEqual(retained['total'], 8)
        self.assertEqual(retained['map_sha256'], self.maps['a']['artifact_sha256'])
        self.assertEqual(retained['retention_reason'], 'MAP_MISMATCH')
        historical_map = self.get('MAP', artifact_sha256=self.maps['a']['artifact_sha256'])
        self.assertFalse(historical_map['current'])
        self.publish('items', 'b')
        self.publish('items', 'a')
        self.assertEqual(self.get('ITEMS')['artifact_sha256'], self.items['b']['artifact_sha256'])
        from app.application.agent_workflow.source_document_loop_jobs import _public
        public = _public(SimpleNamespace(), self.root, self.meta)
        self.assertEqual([p['stage'] for p in public['stages']], ['MAP', 'ITEMS'])
        self.assertTrue(all(p['current'] for p in public['stages']))
        self.assertEqual(len(public['retained_stages']), 2)

    def test_multiple_items_for_unregistered_map_are_not_arbitrarily_selected(self):
        from argparse import Namespace
        from .test_plugin_workbench import artifact_tools, write
        decisions = json.loads((self.workspace/'items-a-input.json').read_text())
        decisions['text_decisions'][1]['items'][0]['decision_summary'] = 'Independent review retains the complete customer obligation.'
        inputs = self.workspace/'items-alternate-input.json'; write(inputs, decisions)
        output = self.workspace/'items-alternate'
        artifact_tools.compile_items(Namespace(map_dir=self.workspace/'map-a', input=inputs, baseline_dir=None, output_dir=output))
        first = self.publish('items', 'a'); second = self.publish('items', 'alternate')
        self.assertNotEqual(first['artifact_sha256'], second['artifact_sha256'])
        self.publish('map', 'a')
        self.assertEqual(list(stages.stage_index(self.root)), ['MAP'])
        self.assertEqual({p['retention_reason'] for p in stages.stage_inventory(self.root)['retained']}, {'UNSELECTED'})
        chosen = self.publish('items', 'a')
        self.assertTrue(chosen['current'])
        self.assertEqual(self.get('ITEMS')['artifact_sha256'], first['artifact_sha256'])
        self.assertEqual(len(stages.stage_inventory(self.root)['retained']), 1)

    def test_saved_versions_survive_output_reuse_and_exact_retries_do_not_grow_history(self):
        import shutil
        mapping = self.publish('map', 'a'); items = self.publish('items', 'a')
        before = stages.stage_inventory(self.root)
        self.assertEqual(self.publish('items', 'a')['stage_version_id'], items['stage_version_id'])
        self.assertEqual(stages.stage_inventory(self.root), before)
        # Reusing producer directories cannot rewrite published bytes or table cells.
        shutil.rmtree(self.workspace/'map-a'); shutil.rmtree(self.workspace/'items-a')
        self.assertEqual(self.get('MAP')['artifact_sha256'], mapping['artifact_sha256'])
        records = self.get('ITEMS')['rows']
        self.assertEqual([r['source_record_id'] for r in records], [r['source_record_id'] for r in self.items['a']['records']])
        self.assertIn('Apply 12 V', next(r['source_text'] for r in records if r['label'] == 'Response'))

    def test_legacy_mismatched_index_is_filtered_on_read_and_migrated_without_loss(self):
        import hashlib
        def legacy(stage, suffix):
            filename = stages._FILENAMES[stage]
            directory = ('map-' if stage == 'MAP' else 'items-')+suffix
            path = self.workspace/directory/filename
            data = json.loads(path.read_text())
            pointer = {'stage': stage, 'source_sha256': self.sha,
                'artifact_sha256': data['artifact_sha256'], 'file_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'path': str(path.relative_to(self.workspace)),
                'map_sha256': data['artifact_sha256'] if stage == 'MAP' else data['document_map_artifact_sha256'],
                'count': len(data['nodes'] if stage == 'MAP' else data['records'])}
            if stage == 'ITEMS':
                tables = path.parent/'table-previews.json'
                pointer['table_previews'] = {'path': str(tables.relative_to(self.workspace)),
                    'sha256': hashlib.sha256(tables.read_bytes()).hexdigest()}
            return pointer
        raw = {'MAP': legacy('MAP', 'b'), 'ITEMS': legacy('ITEMS', 'a')}
        index = self.root/'stage-results.json'; index.write_text(json.dumps(raw))
        untouched = index.read_bytes()
        self.assertEqual(list(stages.stage_index(self.root)), ['MAP'])
        self.assertEqual(self.host.task_brief()['work_state'], 'MAP_SAVED')
        explicit = self.get('ITEMS', artifact_sha256=self.items['a']['artifact_sha256'])
        self.assertFalse(explicit['current'])
        self.assertEqual(index.read_bytes(), untouched)
        self.publish('items', 'b')
        retained = self.get('ITEMS', artifact_sha256=self.items['a']['artifact_sha256'])
        self.assertFalse(retained['current'])
        self.assertEqual(retained['total'], 8)
        pointer = next(p for p in stages.stage_inventory(self.root)['retained'] if p['stage'] == 'ITEMS')
        self.assertTrue(pointer['path'].startswith('.stage-artifacts/'))
        # Both same-source Map mismatch and impossible cross-source legacy pair
        # stay out of current, even when Map hash strings happen to match.
        raw['ITEMS']['map_sha256'] = raw['MAP']['map_sha256']
        raw['ITEMS']['source_sha256'] = 'f'*64
        index.write_text(json.dumps(raw))
        self.assertEqual(list(stages.stage_index(self.root)), ['MAP'])

    def test_source_pinning_rejects_another_real_original_without_changing_saved_stages(self):
        from argparse import Namespace
        from .test_plugin_workbench import make_fixture, artifact_tools, write
        self.publish('map', 'a')
        before = stages.stage_inventory(self.root)
        source, capture, decisions = make_fixture(self.workspace/'other-source')
        inputs = self.workspace/'other-map-input.json'; write(inputs, decisions)
        output = self.workspace/'map-other'
        artifact_tools.compile_map(Namespace(source=source, source_ir=capture/'source_ir.json', input=inputs, output_dir=output))
        foreign = json.loads((output/'document-map.json').read_text())['source_sha256']
        self.assertNotEqual(foreign, self.sha)
        with self.assertRaisesRegex(ValueError, 'bound to another original'):
            stages.publish_stage(self.root, self.workspace, 'compile-map', {'output_dir': 'map-other'}, foreign)
        self.assertEqual(stages.stage_inventory(self.root), before)

    def test_concurrent_stage_registration_retains_all_outputs_and_has_only_a_matching_pair(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(4)
        def publish(pair):
            barrier.wait(timeout=10)
            return self.publish(*pair)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(publish, [('map', 'a'), ('items', 'a'), ('map', 'b'), ('items', 'b')]))
        inventory = stages.stage_inventory(self.root)
        all_versions = [*inventory['current'].values(), *inventory['retained']]
        self.assertEqual(len(all_versions), 4)
        self.assertEqual({p['stage_version_id'] for p in all_versions}, {p['stage_version_id'] for p in results})
        self.assertEqual(inventory['current']['MAP']['map_sha256'], inventory['current']['ITEMS']['map_sha256'])
        self.assertTrue(self.get('ITEMS')['current'])

    def test_failed_current_index_write_keeps_old_pair_and_retry_recovers_saved_candidate(self):
        from app.adapters.agents.codex_app_server import native_loop_driver
        self.publish('map', 'a'); self.publish('items', 'a')
        write_json = native_loop_driver._write_json
        def interrupted(path, value):
            if path == self.root/'stage-results.json':
                raise OSError('simulated interruption before current index replacement')
            return write_json(path, value)
        with patch.object(native_loop_driver, '_write_json', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'simulated interruption'):
                self.publish('map', 'b')
        self.assertEqual(self.get('ITEMS')['artifact_sha256'], self.items['a']['artifact_sha256'])
        self.assertEqual(len(stages.stage_inventory(self.root)['retained']), 1)
        self.assertFalse(self.get('MAP', artifact_sha256=self.maps['b']['artifact_sha256'])['current'])
        self.publish('map', 'b')
        self.assertEqual(list(stages.stage_index(self.root)), ['MAP'])
        self.assertEqual(len(stages.stage_inventory(self.root)['retained']), 2)
        self.assertFalse(list((self.workspace/'.stage-artifacts').glob('.pending-*')))

    def test_real_native_tool_compiles_and_reports_retained_output_without_failing(self):
        self.publish('map', 'b')
        result = self.host.invoke('reqpilot_run', {'operation': 'compile-items',
            'arguments': {'map_dir': 'map-a', 'input': 'items-a-input.json', 'output_dir': 'host-items-a'}})
        self.assertTrue(result['success'], result)
        compiled = json.loads(result['content_items'][0]['text'])
        self.assertEqual(compiled['stage_result']['retention_reason'], 'MAP_MISMATCH')
        self.assertFalse(compiled['stage_result']['current'])
        self.assertEqual(compiled['source_item_count'], 8)
        self.assertEqual(compiled['stage_artifact_sha256'], self.items['a']['artifact_sha256'])
        retained = next(p for p in stages.stage_inventory(self.root)['retained'] if p['stage'] == 'ITEMS')
        readable = self.host.invoke('reqpilot_read', {'path': retained['path'], 'json_pointer': '/records/0/source_display_id'})
        self.assertTrue(readable['success'], readable)
        self.assertEqual(self.host.task_brief()['work_state'], 'MAP_SAVED')
        brief = self.host.task_brief()
        self.assertEqual(brief['saved_artifacts'][0]['producer_path'], 'map-b/document-map.json')
        self.assertEqual(brief['saved_artifacts'][0]['path_role'], 'READABLE_STAGE_SNAPSHOT')
        from app.application.agent_workflow.native_task_brief import render_task_goal
        self.assertIn('不能把阶段快照目录当成完整编制包', render_task_goal(brief['current_request'], origin='USER_REQUEST', brief=brief))

    def test_real_router_binds_historical_stage_query_and_does_not_change_current(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.errors import application_error_handler
        from app.api.routers import source_item_delivery
        from app.application.errors import ApplicationError
        from app.application.agent_workflow import source_document_loop_jobs as api
        self.publish('map', 'a'); saved = self.publish('items', 'a'); self.publish('map', 'b')
        app = FastAPI()
        app.add_exception_handler(ApplicationError, application_error_handler)
        app.include_router(source_item_delivery.build())
        route = source_item_delivery.ROOT + '/extraction-loop/{task_id}/stages/{stage}'
        query = app.openapi()['paths'][route]['get']['parameters']
        self.assertTrue({'artifact_sha256', 'stage_version_id'} <= {p['name'] for p in query if p['in'] == 'query'})
        url = route.format(project_id='p', document_id='d', task_id=self.meta['task_id'], stage='ITEMS')
        with patch.object(api, '_raw_existing', return_value=(None, self.root, self.meta)), TestClient(app) as client:
            self.assertEqual(client.get(url).status_code, 409)
            historical = client.get(url, params={'artifact_sha256': saved['artifact_sha256']})
            self.assertEqual(historical.status_code, 200, historical.text)
            self.assertFalse(historical.json()['current'])
            self.assertEqual(historical.json()['retention_reason'], 'MAP_MISMATCH')
            self.assertEqual(historical.json()['total'], 8)
            exact = client.get(url, params={'stage_version_id': saved['stage_version_id'], 'offset': 2, 'limit': 2})
            self.assertEqual(exact.status_code, 200, exact.text)
            self.assertEqual(exact.json()['stage_version_id'], saved['stage_version_id'])
            self.assertEqual(exact.json()['next_offset'], 4)
            self.assertEqual(client.get(url, params={'artifact_sha256': 'f'*64}).status_code, 409)
        self.assertEqual(self.get('MAP')['artifact_sha256'], self.maps['b']['artifact_sha256'])

    def test_resumed_host_reads_stage_json_without_promoting_it_to_a_compiler_bundle(self):
        # The fixture host already owns a trust ledger. Simulate interruption
        # after stage registration but before its final _record_artifacts call.
        mapping_a = self.publish('map', 'a')
        items_a = self.publish('items', 'a')
        mapping_b = self.publish('map', 'b')
        (self.workspace/mapping_a['path']).write_text('{}')
        unregistered = self.workspace/'.stage-artifacts/unregistered.json'
        unregistered.write_text('{"unregistered": true}')
        resumed = SourceDocumentToolHost(task_root=self.root, meta=self.meta,
            workspace=self.workspace, plugin_root=ROOT/'plugins/reqpilot', project_root=ROOT)
        for pointer in (mapping_b, items_a):
            result = resumed.invoke('reqpilot_read', {'path': pointer['path'], 'json_pointer': '/artifact_sha256'})
            self.assertTrue(result['success'], result)
            self.assertNotIn(pointer['path'], resumed.trusted)
        self.assertTrue(resumed.invoke('reqpilot_read', {'path': items_a['table_previews']['path'], 'json_pointer': ''})['success'])
        # Generic task JSON remains readable for investigation. Stage/API use
        # still validates persisted hashes and cannot accept the tampered Map.
        self.assertTrue(resumed.invoke('reqpilot_read', {'path': str(unregistered.relative_to(self.workspace)), 'json_pointer': ''})['success'])
        self.assertNotIn(str(unregistered.relative_to(self.workspace)), resumed.trusted)
        with self.assertRaisesRegex(ValueError, 'changed after publication'):
            stages.read_stage(self.root, 'MAP', artifact_sha256=mapping_a['artifact_sha256'])
