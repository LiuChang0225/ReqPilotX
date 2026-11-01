"""Public same-task policy updates over real synthetic compiler/review outputs."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'prototype'), str(ROOT / 'plugins/reqpilot/scripts'), str(Path(__file__).parent)]
from test_portable import make_fixture, item_decisions, write
from test_review_loop import args
import artifact_tools
import manual_tools
import revision_tools as review
import task_review
from task_service import TaskService
from task_service import ReviewRequest
import task_review_policy
from reqpilotx.application.agent_workflow import task_workspace as core


class TaskReviewPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        source, _, mapping = make_fixture(self.base / 'fixture')
        self.source = source
        self.service = TaskService(self.base, ROOT / 'plugins/reqpilot/scripts/reqpilot.py', ROOT)
        opened = self.service.invoke('open', {'mode': 'create', 'source': str(source),
            'goal': 'Synthetic source-only task; no internal evidence was supplied.',
            'creation_key': 'policy-case'})
        self.task = opened['task_id']
        self.call('prepare', mode='capture')
        edit = {key: value for key, value in mapping.items() if key != 'source_sha256'}
        edit['schema_version'] = 'reqpilot-map-edit/v1'
        mapped = self.call('map', decisions=edit)
        decisions = item_decisions(Path(mapped['selected']['MAP']['output_dir']))
        for key in ('source_sha256', 'document_map_artifact_sha256'):
            decisions.pop(key, None)
        self.call('items', decisions=decisions)
        self.initial_request = self.request(command_id='initial-review')
        self.initial = self.service.invoke('review', self.initial_request)
        self.root, _ = self.service.state(self.task)
        self.review = self.root / 'review'
        self.initial_head = review.load_head(self.review)
        self.baseline = review.bundle(self.review / 'versions/initial')[1]

    def request(self, **extra):
        state = self.service.invoke('inspect', {'task_id': self.task})
        return {'task_id': self.task, 'expected_revision': state['revision'], **extra}

    def call(self, action, **extra):
        result = self.service.invoke(action, self.request(**extra))
        self.assertEqual(result['operation_state'], 'COMMITTED', result)
        return result

    def policy_request(self, command='policy-once'):
        return self.request(analysis_policy='skip_no_internal_evidence', command_id=command)

    def test_public_update_keeps_feedback_user_opinion_source_and_old_delivery(self):
        sid = self.baseline.records[0].source_record_id
        feedback_path = self.base / 'feedback.json'
        write(feedback_path, {'expected_review_sha256': self.initial_head['review_sha256'],
            'feedback': [{'origin': 'USER', 'source_item_ids': [sid],
                          'text': 'Synthetic user question remains unresolved.'}]})
        review.feedback_review(args(review_dir=self.review, input=feedback_path))
        head = review.load_head(self.review)
        initial_dir = self.review / 'versions/initial'
        context = manual_tools.context_for(initial_dir)
        user_path = self.base / 'user-decisions.json'
        write(user_path, {'schema_version': 'source-item-user-decisions/v1',
            'expected_source_sha256': context['source_sha256'],
            'expected_baseline_sha256': context['baseline_sha256'],
            'user_input_reference': 'Explicit synthetic user opinion fixture',
            'decisions': [{'unit_id': sid, 'decision': 'NON_COMPLIANT',
                           'reason': 'Synthetic engineer response must be retained.'}]})
        imported = self.base / 'manual-import'
        with patch.dict('os.environ', {'REQPILOT_NATIVE_AGENT_TOOLS': '0'}):
            manual_tools.run_manual(args(command='import-manual-decisions', baseline_dir=initial_dir,
                input=user_path, output_dir=imported))
        candidate = self.review / 'versions/user-opinion'
        revision_path = self.base / 'user-revision.json'
        write(revision_path, {'schema_version': 'reqpilot-review-revision/v1',
            'expected_review_sha256': head['review_sha256'],
            'expected_baseline_sha256': head['current']['baseline_sha256']})
        review.revise_review(args(review_dir=self.review, input=revision_path, output_dir=candidate,
                                 manual_decisions=imported / 'manual-decisions.json'))
        old_delivery = self.review / 'deliveries/user-opinion'
        artifact_tools.deliver(args(baseline_dir=candidate, source=self.source, output_dir=old_delivery))
        close_path = self.base / 'user-close.json'
        write(close_path, {'expected_review_sha256': head['review_sha256'],
            'candidate_path': str(candidate.relative_to(self.review)),
            'delivery_path': str(old_delivery.relative_to(self.review))})
        review.close_review(args(review_dir=self.review, input=close_path))
        previous = review.load_head(self.review)
        original_feedback = deepcopy(previous['feedback'])
        original_source = (self.root / core.workspace_status(self.root)['context']['source']['path']).read_bytes()
        old_bytes = {path.name: path.read_bytes() for path in old_delivery.iterdir() if path.is_file()}

        result = self.service.invoke('review', self.policy_request())
        self.assertEqual(result['operation_state'], 'COMMITTED', result)
        self.assertEqual(result['task_id'], self.task)
        current = review.load_head(self.review)
        self.assertEqual(current['predecessor_review_sha256'], previous['review_sha256'])
        self.assertEqual(current['current']['baseline_sha256'], self.baseline.artifact_sha256)
        self.assertEqual(current['feedback'], original_feedback)
        self.assertEqual(current['feedback'][0]['status'], 'OPEN')
        self.assertEqual(old_bytes, {path.name: path.read_bytes() for path in old_delivery.iterdir() if path.is_file()})
        self.assertEqual((self.root / core.workspace_status(self.root)['context']['source']['path']).read_bytes(), original_source)
        policy = json.loads((Path(result['candidate']) / 'analysis-policy.json').read_text())
        self.assertEqual(policy, {'source_sha256': self.baseline.source_sha256,
                                  'status': 'SKIPPED', 'reason': 'NO_INTERNAL_EVIDENCE'})
        self.assertEqual(json.loads((Path(result['candidate']) / 'manual-decisions.json').read_text()),
                         json.loads((candidate / 'manual-decisions.json').read_text()))
        snapshot = json.loads((Path(result['delivery']) / 'matrix-snapshot.json').read_text())
        node = next(node for node in snapshot['nodes'] if node.get('requirement_id') == sid)
        self.assertEqual(node['manual_decision']['decision'], 'NON_COMPLIANT')
        self.assertEqual(snapshot['source_item_baseline']['analysis_policy'], policy)
        self.assertIn('analysis_policy', result['diff']['artifact_changes'])
        workbook = load_workbook(Path(result['delivery']) / 'matrix.xlsx')
        self.addCleanup(workbook.close)
        self.assertEqual(workbook['_ReqPilotXMeta']['B3'].value, self.baseline.artifact_sha256)
        self.assertEqual(review.verify_review(args(review_dir=self.review))['status'], 'REVIEW_VERIFIED')

    def test_exact_retry_and_initial_route_replay_never_overwrite_newer_head(self):
        request = self.policy_request()
        first = self.service.invoke('review', request)
        self.assertEqual(first['operation_state'], 'COMMITTED', first)
        path = self.base / 'later-feedback.json'
        write(path, {'expected_review_sha256': first['new_review_sha256'],
            'feedback': [{'origin': 'USER', 'source_item_ids': [], 'text': 'A later synthetic question.'}]})
        review.feedback_review(args(review_dir=self.review, input=path))
        latest = review.load_head(self.review)['review_sha256']
        with patch.object(review, 'revise_review') as compiled, patch.object(artifact_tools, 'deliver') as exported:
            replay = self.service.invoke('review', request)
            self.assertTrue(replay['replayed'])
            self.assertEqual(replay['new_review_sha256'], first['new_review_sha256'])
            self.assertFalse(replay['review_still_current'])
            compiled.assert_not_called()
            exported.assert_not_called()
            initial_replay = self.service.invoke('review', self.initial_request)
            self.assertTrue(initial_replay['replayed'])
        self.assertEqual(review.load_head(self.review)['review_sha256'], latest)
        with self.assertRaisesRegex(ValueError, 'COMMAND_ID_CONFLICT'):
            self.service.invoke('review', {**request, 'analysis_policy': 'preserve'})

    def test_preserve_is_readonly_and_stale_policy_request_is_rejected(self):
        before = review.load_head(self.review)
        result = self.service.invoke('review', self.request())
        self.assertEqual(result['operation_state'], 'NOT_NEEDED')
        self.assertFalse(result['business_revision_created'])
        self.assertEqual(review.load_head(self.review), before)
        self.assertFalse((self.review / 'versions/initial/analysis-policy.json').exists())
        stale = self.policy_request('stale-policy')
        path = self.base / 'arrival.json'
        write(path, {'expected_review_sha256': before['review_sha256'],
            'feedback': [{'origin': 'USER', 'source_item_ids': [], 'text': 'Feedback arrived before policy update.'}]})
        review.feedback_review(args(review_dir=self.review, input=path))
        with self.assertRaisesRegex(ValueError, 'REVISION_CONFLICT'):
            self.service.invoke('review', stale)
        self.assertFalse((self.review / 'versions/initial/analysis-policy.json').exists())

    def test_preserve_command_identity_cannot_be_reused_for_a_policy_update(self):
        request = self.request(command_id='preserve-once')
        first = self.service.invoke('review', request)
        self.assertEqual(first['operation_state'], 'NOT_NEEDED')
        self.assertFalse(first['replayed'])
        self.assertEqual(self.service.invoke('inspect', {'task_id': self.task})['revision'],
                         request['expected_revision'])
        with self.assertRaisesRegex(ValueError, 'COMMAND_ID_CONFLICT'):
            self.service.invoke('review', {**request, 'analysis_policy': 'skip_no_internal_evidence'})
        current = self.service.invoke('review', self.policy_request('separate-policy'))
        latest = review.load_head(self.review)
        replay = self.service.invoke('review', request)
        self.assertTrue(replay['replayed'])
        self.assertFalse(replay['review_still_current'])
        self.assertEqual(replay['review_sha256'], first['review_sha256'])
        self.assertEqual(current['new_review_sha256'], latest['review_sha256'])
        self.assertEqual(review.load_head(self.review), latest)

    def test_interrupted_compile_export_close_resumes_frozen_policy_once(self):
        for stage in ('compile', 'deliver', 'close'):
            with self.subTest(stage=stage):
                if stage != 'compile':
                    self.setUp()  # Each interruption starts before a real policy change.
                request = self.policy_request('interrupt-' + stage)
                checkpoint = task_review._checkpoint
                def interrupted(directory, actual, value):
                    if actual == stage:
                        raise RuntimeError('Synthetic exit after ' + stage)
                    return checkpoint(directory, actual, value)
                with patch.object(review, 'revise_review', wraps=review.revise_review) as compiled, \
                     patch.object(artifact_tools, 'deliver', wraps=artifact_tools.deliver) as exported, \
                     patch.object(review, 'close_review', wraps=review.close_review) as closed:
                    with patch.object(task_review, '_checkpoint', side_effect=interrupted):
                        with self.assertRaisesRegex(RuntimeError, 'Synthetic exit'):
                            self.service.invoke('review', request)
                    result = self.service.invoke('review', request)
                    self.assertEqual(result['operation_state'], 'COMMITTED', result)
                    self.assertEqual((compiled.call_count, exported.call_count, closed.call_count), (1, 1, 1))
        self.assertEqual(review.verify_review(args(review_dir=self.review))['status'], 'REVIEW_VERIFIED')

    def test_matching_explicit_policy_does_not_create_a_meaningless_successor(self):
        first = self.service.invoke('review', self.policy_request('initial-policy'))
        self.assertEqual(first['operation_state'], 'COMMITTED')
        before = review.load_head(self.review)
        repeated = self.service.invoke('review', self.policy_request('same-policy-new-key'))
        self.assertEqual(repeated['operation_state'], 'NOT_NEEDED')
        self.assertFalse(repeated['business_revision_created'])
        self.assertEqual(review.load_head(self.review), before)

    def test_preserve_reserves_task_wide_key_before_revise_or_annotations_compile(self):
        command = 'preserve-cross-action'
        preserved = self.service.invoke('review', self.request(command_id=command))
        self.assertEqual(preserved['operation_state'], 'NOT_NEEDED')
        before = review.load_head(self.review)
        index = (self.root / 'workspace-index.json').read_bytes()
        descriptors = {path.name for path in (self.root / 'operations').glob('*.json')}
        sid = self.baseline.records[0].source_record_id
        writes = {
            'revise': {'model_id': 'synthetic-model', 'decisions': [{
                'source_record_id': sid, 'requirementness': 'REQUIREMENT',
                'requirement_kinds': ['QUALITY'], 'confidence': 0.9,
                'decision_summary': 'Synthetic classification change.',
                'reason_codes': ['source_meaning']} ]},
            'annotations': {'annotation_updates': [{
                'source_item_id': sid, 'topics': ['VERIFICATION'],
                'reason': 'Synthetic source-bound tag.'}]},
        }
        with patch.object(review, 'revise_review') as compiled, \
             patch.object(artifact_tools, 'deliver') as exported:
            for action, changes in writes.items():
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'COMMAND_ID_CONFLICT'):
                    self.service.invoke(action, self.request(command_id=command, changes=changes))
            compiled.assert_not_called()
            exported.assert_not_called()
        self.assertEqual(review.load_head(self.review), before)
        self.assertEqual((self.root / 'workspace-index.json').read_bytes(), index)
        self.assertEqual({path.name for path in (self.root / 'operations').glob('*.json')}, descriptors)

    def test_saved_policy_result_survives_convenience_summary_reload_failure(self):
        request = self.policy_request('summary-failure')
        root, state = self.service.state(self.task)
        real_apply = task_review_policy.apply_review
        real_status = core.workspace_status
        completed = []
        def applied(*arguments, **keywords):
            result = real_apply(*arguments, **keywords)
            completed.append(result)
            return result
        def status(*arguments, **keywords):
            if completed:
                raise OSError('Synthetic summary reload failure after commit')
            return real_status(*arguments, **keywords)
        with patch.object(task_review_policy, 'apply_review', side_effect=applied), \
             patch.object(core, 'workspace_status', side_effect=status):
            result = task_review_policy.prepare_review(self.service, root, state,
                                                       ReviewRequest.model_validate(request))
        self.assertEqual(result['operation_state'], 'COMMITTED')
        self.assertEqual(result['receipt_sha256'], completed[0]['receipt_sha256'])
        self.assertEqual(review.load_head(self.review)['review_sha256'], result['new_review_sha256'])

    def test_feedback_arriving_after_policy_compile_blocks_close_without_losing_work(self):
        request = self.policy_request('policy-feedback-race')
        original = task_review._checkpoint
        feedback_path = self.base / 'late-policy-feedback.json'
        def arrived(directory, stage, value):
            original(directory, stage, value)
            if stage == 'compile':
                head = review.load_head(self.review)
                write(feedback_path, {'expected_review_sha256': head['review_sha256'],
                    'feedback': [{'origin': 'USER', 'source_item_ids': [],
                                  'text': 'Synthetic late feedback must not be overwritten.'}]})
                review.feedback_review(args(review_dir=self.review, input=feedback_path))
        with patch.object(task_review, '_checkpoint', side_effect=arrived):
            result = self.service.invoke('review', request)
        self.assertEqual(result['operation_state'], 'BLOCKED', result)
        self.assertEqual(result['status'], 'FAILED')
        self.assertTrue(Path(result['candidate'], 'analysis-policy.json').is_file())
        self.assertTrue(result['local_artifact_preserved'])
        head = review.load_head(self.review)
        self.assertEqual(head['current'], self.initial_head['current'])
        self.assertEqual(head['feedback'][0]['status'], 'OPEN')
        self.assertNotIn('delivery', head)
        self.assertFalse((self.review / 'versions/initial/analysis-policy.json').exists())


if __name__ == '__main__':
    unittest.main()
