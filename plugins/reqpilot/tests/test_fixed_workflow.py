"""Fixed workflow projection is versioned, source-bound and artifact-driven."""
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'plugins/reqpilot/scripts'), str(ROOT/'prototype')]
from test_portable import make_fixture
from task_service import TaskService
from reqpilotx.application.agent_workflow import fixed_workflow as workflow

ENTRY = ROOT/'plugins/reqpilot/scripts/reqpilot.py'
SOURCE = 'a'*64
MAP = 'b'*64
ITEMS = 'c'*64
DELIVERY = 'd'*64


def spec(notes=''):
    return workflow.workflow_spec(brief_sha256=workflow.brief_sha256(workflow.compose_goal(notes)),
                                  evidence_mode='NONE')


def context():
    return {'context_sha256': 'e'*64, 'source': {'sha256': SOURCE},
            'request': {'workflow': spec()}}


def brief(policy='SKIPPED', confirmation='NOT_REQUIRED'):
    return {'analysis_policy': {'status': policy, 'reason': 'NO_INTERNAL_EVIDENCE'},
            'map_confirmation': confirmation, 'confirmed_map_sha256': None}


def selected(map_sha=MAP, items_sha=ITEMS, delivery_sha=DELIVERY, items_map_sha=None):
    value = {'MAP': {'path': '/private/map/document-map.json', 'artifact_sha256': map_sha, 'count': 4}}
    value['ITEMS'] = {'path': '/private/items/source-requirement-baseline.json', 'artifact_sha256': items_sha,
                      'map_sha256': items_map_sha or map_sha, 'count': 4}
    if delivery_sha:
        value['DELIVERY'] = {'path': '/private/delivery/matrix-snapshot.json', 'artifact_sha256': delivery_sha,
                             'baseline_sha256': items_sha}
    return value


class FixedWorkflowTests(unittest.TestCase):
    def test_stage_order_and_source_binding_cannot_be_shortcut_by_prose(self):
        state = workflow.project_workflow(context=context(), selected={}, brief=brief())
        self.assertEqual([item['id'] for item in state['stages']], list(workflow.STAGE_IDS))
        self.assertEqual(state['source_sha256'], SOURCE)
        self.assertEqual(state['stages'][0]['status'], 'SAVED')
        self.assertEqual(state['stages'][1]['status'], 'PENDING')
        self.assertEqual(state['stages'][2]['status'], 'BLOCKED')
        self.assertEqual(state['stages'][2]['reason'], 'SOURCE_MAP_REQUIRED')
        self.assertFalse(state['ready_to_review'])
        changed = context(); changed['source']['sha256'] = 'f'*64
        state = workflow.project_workflow(context=changed, selected=selected(), brief=brief())
        self.assertEqual(state['source_sha256'], 'f'*64)
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_WORKFLOW_VERSION'):
            workflow.validate_workflow_spec({**spec(), 'version': '999'})

    def test_model_prose_idle_or_export_claims_do_not_advance_saved_stages(self):
        claimed = {**brief(), 'model_completion_claim': 'all stages complete', 'running_state': 'IDLE',
                   'export_ready': True}
        state = workflow.project_workflow(context=context(), selected={}, brief=claimed)
        statuses = {item['id']: item['status'] for item in state['stages']}
        self.assertEqual(statuses['SOURCE_MAP'], 'PENDING')
        self.assertEqual(statuses['ITEMS_CLASSIFICATION'], 'BLOCKED')
        self.assertEqual(statuses['DELIVERABLE'], 'BLOCKED')
        self.assertFalse(state['ready_to_review'])

    def test_no_evidence_branch_is_skipped_and_ready_requires_saved_delivery(self):
        review = {'review_root': '/private/review', 'review_sha256': '1'*64,
                  'stages': {'MAP': {'artifact_sha256': MAP}, 'ITEMS': {'artifact_sha256': ITEMS}}}
        state = workflow.project_workflow(context=context(), selected=selected(delivery_sha=None),
                                          brief=brief(), review=review)
        by_id = {item['id']: item for item in state['stages']}
        self.assertEqual(by_id['ITEMS_CLASSIFICATION']['status'], 'SAVED')
        self.assertEqual(by_id['EVIDENCE_ANALYSIS']['status'], 'SKIPPED')
        self.assertEqual(by_id['EVIDENCE_ANALYSIS']['branch'], 'NO_INTERNAL_EVIDENCE')
        self.assertEqual(by_id['SELF_CHECK']['status'], 'SAVED')
        self.assertEqual(by_id['DELIVERABLE']['status'], 'PENDING')
        self.assertFalse(state['ready_to_review'])
        ready = workflow.project_workflow(context=context(), selected=selected(), brief=brief(), review=review)
        self.assertEqual(ready['stages'][-1]['status'], 'SAVED')
        self.assertTrue(ready['ready_to_review'])
        self.assertEqual(ready['human_acceptance'], 'NOT_CLAIMED')

    def test_structure_confirmation_and_wrong_item_map_block_items(self):
        waiting = workflow.project_workflow(context=context(), selected=selected(), brief=brief(confirmation='REQUIRED_BEFORE_ITEMS'))
        by_id = {item['id']: item for item in waiting['stages']}
        self.assertEqual(by_id['SOURCE_MAP']['status'], 'SAVED')
        self.assertIn('MAP_CONFIRMATION_REQUIRED', waiting['open_items'])
        stale = workflow.project_workflow(context=context(), selected=selected(map_sha='9'*64, items_map_sha=MAP), brief=brief())
        by_id = {item['id']: item for item in stale['stages']}
        self.assertEqual(by_id['ITEMS_CLASSIFICATION']['status'], 'PENDING')
        self.assertFalse(stale['ready_to_review'])

    def test_task_creation_persists_workflow_and_notes_do_not_remove_map_gate(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, mapping = make_fixture(root/'fixture')
            service = TaskService(root, ENTRY, ROOT)
            goal = workflow.compose_goal('请优先检查试验条件，但不要跳过结构。')
            opened = service.invoke('open', {'mode': 'create', 'source': str(source), 'goal': goal,
                'creation_key': 'fixed-one', 'workflow': spec('请优先检查试验条件，但不要跳过结构。')})
            self.assertEqual(opened['task_id'][:5], 'task-')
            self.assertEqual(opened['workflow']['schema'], workflow.STATE_SCHEMA)
            self.assertEqual(opened['workflow']['stages'][1]['status'], 'PENDING')
            self.assertEqual(opened['workflow']['stages'][2]['status'], 'BLOCKED')
            self.assertIn('补充说明', goal)
            prepared = service.invoke('prepare', {'task_id': opened['task_id'], 'expected_revision': opened['revision']})
            with self.assertRaisesRegex(ValueError, 'MAP_COMPILER_INPUTS_UNAVAILABLE'):
                service.invoke('items', {'task_id': opened['task_id'], 'expected_revision': prepared['revision'],
                    'decisions': {'text_decisions': [{'scope_id': 'text:missing', 'items': []}]}})
            inspected = service.invoke('inspect', {'task_id': opened['task_id']})
            self.assertEqual(inspected['workflow']['stages'][2]['reason'], 'SOURCE_MAP_REQUIRED')
            self.assertEqual((Path(opened['task_root'])/'workflow-status.json').exists(), True)

    def test_workflow_version_mismatch_is_rejected_before_task_creation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _ = make_fixture(root/'fixture')
            service = TaskService(root, ENTRY, ROOT)
            bad = {**spec(), 'definition_sha256': '0'*64}
            with self.assertRaisesRegex(ValueError, 'WORKFLOW_DEFINITION_CHANGED'):
                service.invoke('open', {'mode': 'create', 'source': str(source), 'goal': workflow.compose_goal(),
                    'creation_key': 'fixed-bad', 'workflow': bad})
            self.assertFalse((root/'runtime-data/projects').exists())


if __name__ == '__main__':
    unittest.main()
