"""Stage focus is a bounded, non-authoritative projection of saved task facts."""
from pathlib import Path
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / 'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0] = [str(ENTRY.parent), str(ROOT / 'prototype')]
from test_portable import make_fixture, item_decisions
from task_service import REQUESTS, TaskService
from task_stage_focus import (AUTHORITY, CAPABILITY_ROOT, PAIR_ADOPTED_TOOLS,
                              PAIR_ANALYSIS_TOOL, PAIR_DOCUMENT_TOOLS,
                              PAIR_KNOWLEDGE_TOOLS, SCHEMA, STAGE_METHOD,
                              TOOL_ACTIONS, build_stage_focus)
import build_bundle
from reqpilotx.application.agent_workflow import fixed_workflow as workflow
from reqpilotx.application.agent_workflow import knowledge_binding

SOURCE = 'a' * 64
MAP = 'b' * 64
ITEMS = 'c' * 64
DELIVERY = 'd' * 64
TASK = 'task-' + 'e' * 32
EXTERNAL_READ_ACTIONS = {
    'knowledge_adopted_status': (set(), set()),
    'knowledge_adopted_browse': ({'section'}, {'section', 'query', 'limit'}),
    'knowledge_adopted_read': ({'kind'}, {'kind', 'path', 'sourceId', 'id'}),
    'knowledge_adopted_evidence': ({'query'}, {'query', 'limit'}),
    'knowledge_evidence_search': ({'query'}, {'query', 'limit'}),
    'knowledge_evidence_read': ({'documentId'}, {'documentId', 'firstPage', 'count'}),
    'knowledge_topic_read': (set(), set()),
}


def spec(mode='NONE'):
    return workflow.workflow_spec(
        brief_sha256=workflow.brief_sha256('整理当前客户文档。'), evidence_mode=mode)


def context(mode='NONE'):
    return {'context_sha256': 'f' * 64, 'source': {'sha256': SOURCE},
            'request': {'workflow': spec(mode)}, 'work_id': TASK}


def brief(policy='SKIPPED', confirmation='NOT_REQUIRED'):
    return {'analysis_policy': {'status': policy, 'reason': 'NO_INTERNAL_EVIDENCE'},
            'map_confirmation': confirmation, 'confirmed_map_sha256': None}


def selected(map_sha=MAP, items_sha=ITEMS, items_map_sha=None, delivery=True):
    value = {'MAP': {'path': '/private/map/document-map.json',
                     'artifact_sha256': map_sha, 'count': 4}}
    value['ITEMS'] = {'path': '/private/items/source-requirement-baseline.json',
                      'artifact_sha256': items_sha,
                      'map_sha256': items_map_sha or map_sha, 'count': 4}
    if delivery:
        value['DELIVERY'] = {'path': '/private/delivery/matrix-snapshot.json',
                             'artifact_sha256': DELIVERY,
                             'baseline_sha256': items_sha}
    return value


def evidence_binding():
    document = {
        'kbId': 'kb-1', 'documentId': 'doc-1', 'name': '产品资料.md',
        'mime': 'text/markdown', 'originalRevision': '1' * 64,
        'extractionRevision': '2' * 64, 'coverage': 'complete',
        'location': 'physical-page', 'pages': 2, 'expectedPages': 2,
        'wiki': 'not-requested',
    }
    body = {'schema_version': knowledge_binding.SCHEMA, 'mode': 'PROJECT_REFERENCES',
            'application_id': 'app-1', 'project_id': 'project-1',
            'created_at': '2026-11-01T00:00:00+00:00',
            'project': {'library_id': 'kb-1', 'caller': 'test',
                        'documents': [document], 'roles': {}},
            'team': None}
    body['binding_sha256'] = knowledge_binding.canonical_digest(body)
    return knowledge_binding.validate_binding(body)


def adopted_binding():
    body = {'schema_version': knowledge_binding.SCHEMA, 'mode': 'ADOPTED_KNOWLEDGE',
            'application_id': 'app-1', 'project_id': 'project-1',
            'created_at': '2026-11-01T00:00:00+00:00',
            'project': None, 'team': None,
            'adopted': {'library_id': 'lib-1', 'version_id': 'a' * 64,
                        'caller': 'reqpilotx-task:app-1:command-1:adopted',
                        'name': 'adopted fixture', 'package_version': '1.0.0'}}
    body['binding_sha256'] = knowledge_binding.canonical_digest(body)
    return knowledge_binding.validate_binding(body)


def make_state(projected, *, chosen=None, binding=None, operations=None,
               revision='1' * 64, working=None, extra=None):
    context_value = context(binding['mode'] if binding else 'NONE')
    if binding is not None:
        context_value['request']['knowledge_evidence'] = binding
    value = {
        'revision': revision,
        'context': context_value,
        'workflow': projected,
        'selected': chosen or {},
        'current_working': working or {'owner': 'task_workspace'},
        'incomplete_operations': operations or [],
    }
    if binding is not None:
        value['knowledge_evidence'] = binding
    value.update(extra or {})
    return value


def map_only(map_sha=MAP):
    return {'MAP': {'path': '/private/map/document-map.json',
                    'artifact_sha256': map_sha, 'count': 4}}


def assert_requests(test, focus):
    targets = list(focus['read_requests'])
    if focus.get('next_action') is not None:
        targets.append(focus['next_action'])
    for item in targets:
        tool, request = item['tool'], item['request']
        if tool in TOOL_ACTIONS:
            REQUESTS[TOOL_ACTIONS[tool]].model_validate(request)
            continue
        test.assertIn(tool, EXTERNAL_READ_ACTIONS)
        required, allowed = EXTERNAL_READ_ACTIONS[tool]
        test.assertTrue(required <= set(request), item)
        test.assertLessEqual(set(request), allowed, item)


class StageFocusProjectionTests(unittest.TestCase):
    def test_modes_work_wait_recover_complete_follow_saved_facts(self):
        projected = workflow.project_workflow(context=context(), selected={}, brief=brief())
        focus = build_stage_focus(make_state(projected))
        self.assertEqual(focus['mode'], 'WORK')
        self.assertEqual(focus['stage_id'], 'SOURCE_MAP')
        self.assertEqual(focus['authority'], AUTHORITY)
        self.assertEqual(focus['schema'], SCHEMA)
        self.assertEqual(focus['next_action']['tool'], 'source_prepare')
        assert_requests(self, focus)

        mapped = workflow.project_workflow(
            context=context(), selected=map_only(), brief=brief())
        focus = build_stage_focus(make_state(mapped, chosen=map_only()))
        self.assertEqual(focus['stage_id'], 'ITEMS_CLASSIFICATION')
        self.assertEqual(focus['basis']['map_sha256'], MAP)
        self.assertNotIn('items_sha256', focus['basis'])
        self.assertIn(focus['next_action']['tool'], {'task_describe', 'source_read'})
        assert_requests(self, focus)

        waiting = workflow.project_workflow(
            context=context(), selected=map_only(),
            brief=brief(confirmation='REQUIRED_BEFORE_ITEMS'))
        focus = build_stage_focus(make_state(waiting, chosen=map_only()))
        self.assertEqual(focus['mode'], 'WAIT')
        self.assertEqual(focus['stage_id'], 'SOURCE_MAP')
        self.assertIn('MAP_CONFIRMATION_REQUIRED', focus['remaining_reasons'])
        self.assertEqual(focus['next_action']['tool'], 'task_inspect')
        assert_requests(self, focus)

        recovered = build_stage_focus(make_state(
            projected, operations=[{'operation_id': 'op-1', 'state': 'STARTED'}]))
        self.assertEqual(recovered['mode'], 'RECOVER')
        self.assertEqual(recovered['next_action']['tool'], 'task_recover')
        assert_requests(self, recovered)
        terminal = build_stage_focus(make_state(
            projected, operations=[{'operation_id': 'op-2', 'state': 'FAILED'}]))
        self.assertEqual(terminal['mode'], 'WORK')

        with TemporaryDirectory() as temporary:
            delivery = Path(temporary) / 'delivery'
            delivery.mkdir()
            (delivery / 'matrix-snapshot.json').write_text('{}')
            (delivery / 'matrix.xlsx').write_bytes(b'synthetic workbook')
            chosen = selected()
            chosen['DELIVERY'] = {'path': str(delivery / 'matrix-snapshot.json'),
                                  'artifact_sha256': DELIVERY,
                                  'baseline_sha256': ITEMS}
            review = {'review_root': str(Path(temporary) / 'review'),
                      'review_sha256': '1' * 64,
                      'stages': {'MAP': {'artifact_sha256': MAP},
                                 'ITEMS': {'artifact_sha256': ITEMS}}}
            check = {'status': 'PASSED', 'review_verified': True,
                     'current_delivery_registered': True,
                     'delivery_xlsx_sha256': workflow.file_sha256(delivery / 'matrix.xlsx'),
                     'basis': {'source_sha256': SOURCE, 'map_sha256': MAP,
                               'items_sha256': ITEMS, 'review_sha256': '1' * 64}}
            ready = workflow.project_workflow(
                context=context(), selected=chosen, brief=brief(),
                review=review, check=check)
            self.assertTrue(ready['ready_to_review'])
            focus = build_stage_focus(make_state(
                ready, chosen=chosen,
                working={'owner': 'portable_review', 'review_sha256': '1' * 64}))
            self.assertEqual(focus['mode'], 'COMPLETE')
            self.assertEqual(focus['stage_id'], 'DELIVERABLE')
            self.assertEqual(focus['basis']['review_sha256'], '1' * 64)
            self.assertIsNone(focus['next_action'])
            self.assertEqual(focus['skill']['relative_to'], CAPABILITY_ROOT)
            self.assertLessEqual(set(focus['recommended_tools']),
                                 {'task_inspect', 'workbench_delivery_read'})
            self.assertNotIn('仍待人工接受', focus['objective'] + ' '.join(focus['exit_evidence']))
            assert_requests(self, focus)

    def test_upstream_change_knowledge_and_authorized_derivation_branches(self):
        changed = workflow.project_workflow(
            context=context(), selected=selected(items_map_sha='9' * 64, delivery=False),
            brief=brief())
        focus = build_stage_focus(make_state(
            changed, chosen=selected(items_map_sha='9' * 64, delivery=False)))
        self.assertEqual(focus['mode'], 'WORK')
        self.assertEqual(focus['stage_id'], 'ITEMS_CLASSIFICATION')
        self.assertNotIn('items_sha256', focus['basis'])
        self.assertEqual([item['request']['view'] for item in focus['read_requests']], ['scope'])
        assert_requests(self, focus)

        binding = evidence_binding()
        required = workflow.project_workflow(
            context=context('PROJECT_REFERENCES'), selected=selected(delivery=False),
            brief=brief(policy='UNKNOWN'), bound_evidence=binding)
        focus = build_stage_focus(make_state(
            required, chosen=selected(delivery=False), binding=binding))
        self.assertEqual(focus['mode'], 'WORK')
        self.assertEqual(focus['stage_id'], 'EVIDENCE_ANALYSIS')
        self.assertEqual(focus['role_id'], 'review')
        self.assertEqual(focus['basis']['knowledge_binding_sha256'], binding['binding_sha256'])
        self.assertEqual(focus['next_action'], {'tool': 'knowledge_evidence_read',
            'request': {'documentId': 'doc-1', 'firstPage': 1, 'count': 4}})
        self.assertIn(PAIR_ANALYSIS_TOOL, focus['recommended_tools'])
        self.assertNotIn('analysis_save', focus['recommended_tools'])
        self.assertNotIn('task_describe', focus['recommended_tools'])
        self.assertTrue(set(PAIR_DOCUMENT_TOOLS) <= set(focus['recommended_tools']))
        assert_requests(self, focus)

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source-item-decomposition.json'
            path.write_text(json.dumps({
                'review_scope': {'remaining_source_item_ids': ['SRC-1']},
                'carry_forward': {'stale_parent_ids': []}}))
            derived_selected = selected(delivery=False)
            derived_selected['DECOMPOSITION'] = {
                'path': str(path), 'artifact_sha256': '3' * 64, 'count': 1,
                'baseline_sha256': ITEMS}
            derived = workflow.project_workflow(
                context=context(), selected=derived_selected, brief=brief())
            focus = build_stage_focus(make_state(derived, chosen=derived_selected))
            self.assertEqual(focus['mode'], 'WORK')
            self.assertEqual(focus['stage_id'], 'ITEMS_CLASSIFICATION')
            self.assertEqual(focus['role_id'], 'review')
            self.assertEqual(focus['skill']['id'], 'reqpilot-review')
            self.assertIn('decomposition_apply', focus['recommended_tools'])
            self.assertEqual(focus['next_action']['tool'], 'task_describe')
            self.assertEqual(focus['next_action']['request']['action'], 'decomposition')
            self.assertIn('SCOPED_DERIVATION_REMAINING', focus['remaining_reasons'])
            assert_requests(self, focus)

        no_evidence = workflow.project_workflow(
            context=context(), selected=selected(delivery=False),
            brief=brief(policy='SKIPPED'))
        focus = build_stage_focus(make_state(no_evidence, chosen=selected(delivery=False)))
        self.assertEqual(focus['stage_id'], 'SELF_CHECK')
        self.assertNotIn('knowledge_binding_sha256', focus['basis'])
        self.assertNotIn('analysis_save', focus['recommended_tools'])
        assert_requests(self, focus)

        self.assertIsNone(build_stage_focus({'workflow': None, 'selected': {}}))

    def test_pair_adopted_binding_routes_to_host_evidence_and_signed_save(self):
        binding = adopted_binding()
        projected = workflow.project_workflow(
            context=context('ADOPTED_KNOWLEDGE'), selected=selected(delivery=False),
            brief=brief(policy='UNKNOWN'), bound_evidence=binding)
        focus = build_stage_focus(make_state(
            projected, chosen=selected(delivery=False), binding=binding))
        self.assertEqual(focus['authority'], AUTHORITY)
        self.assertEqual(focus['mode'], 'WORK')
        self.assertEqual(focus['stage_id'], 'EVIDENCE_ANALYSIS')
        self.assertEqual(focus['basis']['knowledge_binding_sha256'],
                         binding['binding_sha256'])
        self.assertEqual(focus['next_action'],
                         {'tool': 'knowledge_adopted_status', 'request': {}})
        self.assertIn(PAIR_ANALYSIS_TOOL, focus['recommended_tools'])
        self.assertTrue(set(PAIR_ADOPTED_TOOLS) <= set(focus['recommended_tools']))
        self.assertNotIn('analysis_save', focus['recommended_tools'])
        self.assertNotIn('task_describe', focus['recommended_tools'])
        self.assertNotIn(PAIR_ANALYSIS_TOOL, TOOL_ACTIONS)
        self.assertTrue(PAIR_ANALYSIS_TOOL in PAIR_KNOWLEDGE_TOOLS)
        self.assertIn('Host attestation', focus['objective'])
        self.assertIn('reqpilot_knowledge.knowledge_analysis_save', focus['objective'])
        assert_requests(self, focus)

    def test_standalone_analysis_keeps_ordinary_task_action_without_binding(self):
        projected = {'stages': [{'id': 'EVIDENCE_ANALYSIS', 'status': 'IN_PROGRESS'}],
                     'next_stage': 'EVIDENCE_ANALYSIS'}
        focus = build_stage_focus(make_state(projected, chosen=selected(delivery=False)))
        self.assertEqual(focus['stage_id'], 'EVIDENCE_ANALYSIS')
        self.assertEqual(focus['next_action']['tool'], 'task_describe')
        self.assertEqual(focus['next_action']['request']['action'], 'analysis')
        self.assertIn('analysis_save', focus['recommended_tools'])
        self.assertNotIn(PAIR_ANALYSIS_TOOL, focus['recommended_tools'])
        assert_requests(self, focus)

    def test_focus_is_bounded_has_no_bodies_and_skill_paths_are_package_relative(self):
        projected = workflow.project_workflow(context=context(), selected={}, brief=brief())
        ordinary = build_stage_focus(make_state(projected))
        huge = 'SECRET_SOURCE_BODY' * 20000
        secret_knowledge = 'SECRET_KNOWLEDGE_BODY' * 20000
        body_state = make_state(
            projected, extra={
                'index': {'committed': [{'operation_id': 'op-' + str(i), 'note': huge}
                                        for i in range(200)]},
                'failed_operations': [{'error': huge} for _ in range(200)],
            })
        body_state['context']['source']['body'] = huge
        body_state['knowledge_evidence'] = {'binding_sha256': '2' * 64,
                                            'body': secret_knowledge}
        bounded = build_stage_focus(body_state)
        self.assertEqual(bounded['mode'], ordinary['mode'])
        self.assertEqual(bounded['stage_id'], ordinary['stage_id'])
        payload = json.dumps(bounded, ensure_ascii=False, sort_keys=True)
        self.assertLess(len(payload.encode('utf-8')), 6000)
        self.assertNotIn('SECRET_SOURCE_BODY', payload)
        self.assertNotIn('SECRET_KNOWLEDGE_BODY', payload)
        self.assertLess(len(json.dumps(ordinary, ensure_ascii=False).encode('utf-8')), 6000)

        for stage, method in STAGE_METHOD.items():
            self.assertEqual(method['skill_id'], 'reqpilot-' + method['role_id']
                             if method['role_id'] != 'review' else 'reqpilot-review')
            relative = Path('skills') / method['skill_id'] / 'SKILL.md'
            self.assertFalse(relative.is_absolute())
            self.assertTrue((ROOT / 'plugins/reqpilot' / relative).is_file(), (stage, relative))
            source_relative = Path(method['skill_id']) / 'SKILL.md'
            self.assertTrue(
                (ROOT / 'agent-assets/current/plugin-skills' / source_relative).is_file(),
                (stage, source_relative))
            for tool in method['recommended_tools']:
                self.assertTrue(tool in TOOL_ACTIONS or tool in PAIR_KNOWLEDGE_TOOLS)


class StageFocusTaskServiceTests(unittest.TestCase):
    def test_real_fixed_workflow_open_prepare_map_items_check_deliver_and_focus_modes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _capture, mapping = make_fixture(root / 'fixture')
            service = TaskService(root, ENTRY, ROOT)
            goal = '按原序整理合成原件。'
            create = {'mode': 'create', 'source': str(source), 'goal': goal,
                      'creation_key': 'stage-focus-flow',
                      'workflow': workflow.workflow_spec(
                          brief_sha256=workflow.brief_sha256(goal), evidence_mode='NONE')}
            opened = service.invoke('open', create)
            task = opened['task_id']
            self.assertEqual(opened['stage_focus']['mode'], 'WORK')
            self.assertEqual(opened['stage_focus']['stage_id'], 'SOURCE_MAP')
            self.assertEqual(opened['stage_focus']['next_action']['tool'], 'source_prepare')
            assert_requests(self, opened['stage_focus'])

            def write(action, **extra):
                revision = service.invoke('inspect', {'task_id': task})['revision']
                return service.invoke(action, {'task_id': task,
                                               'expected_revision': revision, **extra})

            prepared = write('prepare')
            self.assertEqual(prepared['operation_state'], 'COMMITTED', prepared)
            focus = service.invoke('inspect', {'task_id': task})['stage_focus']
            self.assertEqual(focus['stage_id'], 'SOURCE_MAP')
            self.assertEqual(focus['next_action']['tool'], 'source_read')
            self.assertIn('source', [item['request']['view'] for item in focus['read_requests']])
            assert_requests(self, focus)

            edit = {key: value for key, value in mapping.items() if key != 'source_sha256'}
            edit['schema_version'] = 'reqpilot-map-edit/v1'
            mapped = write('map', decisions=edit)
            self.assertEqual(mapped['operation_state'], 'COMMITTED', mapped)
            focus = service.invoke('inspect', {'task_id': task})['stage_focus']
            self.assertEqual(focus['stage_id'], 'ITEMS_CLASSIFICATION')
            self.assertEqual(focus['basis']['map_sha256'],
                             mapped['selected']['MAP']['artifact_sha256'])
            self.assertNotIn('items_sha256', focus['basis'])
            self.assertEqual(focus['next_action']['tool'], 'task_describe')
            assert_requests(self, focus)

            state = service.invoke('inspect', {'task_id': task})
            decisions = item_decisions(Path(state['selected']['MAP']['output_dir']))
            for key in ('schema_version', 'source_sha256', 'document_map_artifact_sha256'):
                decisions.pop(key, None)
            items = write('items', command_id='stage-focus-items', decisions=decisions)
            self.assertEqual(items['operation_state'], 'COMMITTED', items)
            focus = service.invoke('inspect', {'task_id': task})['stage_focus']
            self.assertEqual(focus['stage_id'], 'SELF_CHECK')
            self.assertEqual(focus['basis']['items_sha256'],
                             items['selected']['ITEMS']['artifact_sha256'])
            self.assertNotIn('knowledge_binding_sha256', focus['basis'])
            assert_requests(self, focus)

            followed = []
            for _ in range(8):
                inspected = service.invoke('inspect', {'task_id': task})
                focus = inspected['stage_focus']
                assert_requests(self, focus)
                if focus['mode'] == 'COMPLETE':
                    break
                self.assertIsNotNone(focus['next_action'], focus)
                action = focus['next_action']
                followed.append(action['tool'])
                service.invoke(TOOL_ACTIONS[action['tool']], action['request'])
            else:
                self.fail('focus next_action did not reach COMPLETE within 8 steps')
            self.assertEqual(followed, ['review_prepare', 'task_check', 'task_deliver'])
            inspected = service.invoke('inspect', {'task_id': task})
            self.assertEqual(inspected['stage_focus']['mode'], 'COMPLETE')
            self.assertEqual(inspected['stage_focus']['stage_id'], 'DELIVERABLE')
            self.assertIsNone(inspected['stage_focus']['next_action'])
            self.assertEqual(inspected['workflow']['ready_to_review'], True)
            self.assertEqual(inspected['human_acceptance'], 'NOT_CLAIMED')
            assert_requests(self, inspected['stage_focus'])

            waiting_goal = '仅整理结构，进入条目前等待确认。'
            waiting = service.invoke('open', {
                'mode': 'create', 'source': str(source), 'goal': waiting_goal,
                'creation_key': 'stage-focus-wait', 'require_map_confirmation': True,
                'workflow': workflow.workflow_spec(
                    brief_sha256=workflow.brief_sha256(waiting_goal), evidence_mode='NONE')})
            waiting_task = waiting['task_id']
            waiting_prepare = service.invoke('prepare', {
                'task_id': waiting_task, 'expected_revision': waiting['revision']})
            waiting_map = service.invoke('map', {
                'task_id': waiting_task, 'expected_revision': waiting_prepare['revision'],
                'decisions': edit})
            self.assertEqual(waiting_map['operation_state'], 'COMMITTED', waiting_map)
            waiting_focus = service.invoke('inspect', {'task_id': waiting_task})['stage_focus']
            self.assertEqual(waiting_focus['mode'], 'WAIT')
            self.assertEqual(waiting_focus['stage_id'], 'SOURCE_MAP')
            self.assertIn('MAP_CONFIRMATION_REQUIRED', waiting_focus['remaining_reasons'])
            assert_requests(self, waiting_focus)

            legacy = service.invoke('open', {
                'mode': 'create', 'source': str(source), 'goal': '旧任务没有固定工作流。',
                'creation_key': 'stage-focus-legacy'})
            self.assertNotIn('stage_focus', legacy)
            self.assertNotIn('stage_focus', service.invoke(
                'inspect', {'task_id': legacy['task_id']}))


    @staticmethod
    def _run_frozen(bundle, work, action, payload):
        request = work / (action + '-request.json')
        request.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        script = bundle / 'scripts/reqpilot.py'
        bootstrap = (
            "import os,runpy,sys;"
            "script=sys.argv[1];"
            "sys.path.insert(0,os.path.dirname(script));"
            "sys.argv=[script]+sys.argv[2:];"
            "runpy.run_path(script,run_name='__main__')"
        )
        process = subprocess.run(
            [sys.executable, '-I', '-c', bootstrap, str(script), 'task', action,
             '--working-root', str(work), '--input', str(request)],
            cwd=work, capture_output=True, text=True)
        if process.returncode != 0:
            raise AssertionError(process.stderr or process.stdout)
        start = process.stdout.find('{')
        if start < 0:
            raise AssertionError('frozen task output has no JSON: ' + process.stdout)
        value, _end = json.JSONDecoder().raw_decode(process.stdout[start:])
        return value

    def test_frozen_bundle_receipt_runtime_roles_and_focus_without_development_paths(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, _capture, _mapping = make_fixture(base / 'fixture')
            bundle = base / 'bundle'
            built = build_bundle.build(ROOT, bundle)
            self.assertEqual(built['status'], 'BUILT')
            receipt = json.loads((bundle / 'build-receipt.json').read_text(encoding='utf-8'))
            plugin_paths = {entry['path'] for entry in receipt['plugin_files']}
            self.assertIn('scripts/task_stage_focus.py', plugin_paths)
            for method in STAGE_METHOD.values():
                relative = Path('skills') / method['skill_id'] / 'SKILL.md'
                self.assertTrue((bundle / relative).is_file(), relative)

            work = base / 'work'
            work.mkdir()
            goal = '冻结包内的阶段聚焦任务。'
            opened = self._run_frozen(bundle, work, 'open', {
                'mode': 'create', 'source': str(source), 'goal': goal,
                'creation_key': 'frozen-stage-focus',
                'workflow': workflow.workflow_spec(
                    brief_sha256=workflow.brief_sha256(goal), evidence_mode='NONE')})
            focus = opened['stage_focus']
            self.assertEqual(focus['mode'], 'WORK')
            self.assertEqual(focus['schema'], SCHEMA)
            self.assertEqual(focus['authority'], AUTHORITY)
            self.assertEqual(focus['skill']['relative_to'], CAPABILITY_ROOT)
            self.assertFalse(Path(focus['skill']['path']).is_absolute())
            self.assertTrue((bundle / focus['skill']['path']).is_file())
            self.assertNotIn('packages/engine/src', focus['skill']['path'])
            assert_requests(self, focus)

            inspected = self._run_frozen(bundle, work, 'inspect', {
                'task_id': opened['task_id']})
            self.assertEqual(inspected['stage_focus'], focus)
            self.assertEqual(inspected['workflow']['ready_to_review'], False)


if __name__ == '__main__':
    unittest.main()
