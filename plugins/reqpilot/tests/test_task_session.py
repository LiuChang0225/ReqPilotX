"""Observed context binding must preserve task isolation and exact retry identity."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'plugins/reqpilot/scripts'), str(ROOT/'prototype')]
from task_service import REQUESTS
from task_session import SessionContext


class Bridge:
    def command_revision(self, task, command):
        return 'original' if command == 'recorded' else None


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.session = SessionContext()

    def observe(self, task='one', revision='r1'):
        self.session.observe({'status':'READY', 'task_id':task, 'revision':revision},
                             (task, self.session.epochs.get(task, 0)))

    def bind(self, action='prepare', **request):
        return self.session.bind(action, request, REQUESTS[action], Bridge())

    def test_missing_context_and_multitask_never_choose_latest(self):
        with self.assertRaisesRegex(ValueError, 'TASK_CONTEXT_REQUIRED'): self.bind()
        with self.assertRaisesRegex(ValueError, 'REVISION_CONTEXT_REQUIRED'): self.bind(task_id='one')
        self.observe()
        self.assertEqual(self.bind()[0]['task_id'], 'one')
        self.observe('two', 'r2')
        with self.assertRaisesRegex(ValueError, 'TASK_CONTEXT_REQUIRED'): self.bind()
        self.assertEqual(self.bind(task_id='one')[0]['expected_revision'], 'r1')

    def test_same_implicit_request_reuses_original_revision_and_command(self):
        self.observe()
        first, _ = self.bind()
        self.observe(revision='r2')
        self.assertEqual(self.bind()[0], first)
        self.assertEqual(self.bind(command_id='new')[0]['expected_revision'], 'r2')
        self.assertEqual(self.bind(command_id='recorded')[0]['expected_revision'], 'original')

    def test_explicit_values_nulls_and_semantic_errors_are_not_repaired(self):
        self.observe()
        self.assertEqual(self.bind(expected_revision='old')[0]['expected_revision'], 'old')
        for args in ({'task_id':None}, {'expected_revision':None}, {'mode':'invented'}):
            with self.assertRaises(ValueError): self.bind(**args)

    def test_overlapping_replies_require_fresh_observation(self):
        self.observe()
        _, first = self.bind(command_id='first')
        _, second = self.bind(command_id='second')
        self.session.observe({'status':'READY','task_id':'one','revision':'r3'}, first)
        self.session.observe({'status':'READY','task_id':'one','revision':'r2'}, second)
        with self.assertRaisesRegex(ValueError, 'REVISION_CONTEXT_REQUIRED'): self.bind(command_id='third')
        self.observe(revision='r3')
        self.assertEqual(self.bind(command_id='third')[0]['expected_revision'], 'r3')

    def test_failure_does_not_refresh_stale_write_context(self):
        self.observe()
        self.session.observe({'status':'FAILED','task_id':'one','revision':'external'}, ('one',1))
        self.assertEqual(self.bind()[0]['expected_revision'], 'r1')

    def test_only_transport_schema_changes(self):
        core = REQUESTS['items'].model_json_schema()
        public = self.session.schema(REQUESTS['items'])
        self.assertIn('task_id', core['required'])
        self.assertIn('expected_revision', core['required'])
        self.assertNotIn('task_id', public['required'])
        self.assertNotIn('expected_revision', public['required'])
        self.assertIn('decisions', public['required'])
        self.assertEqual(core['$defs'], public['$defs'])


if __name__ == '__main__': unittest.main()
