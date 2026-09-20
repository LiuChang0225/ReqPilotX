"""Real review/export/CAS recovery using invented PDFs; no host/model access."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from test_portable import make_fixture, item_decisions, write
from test_review_loop import args
import artifact_tools
import revision_tools
import task_review
from app.application.agent_workflow import task_workspace as core


class TaskReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.base = Path(self.temp.name)
        source, capture, mapping = make_fixture(self.base / 'fixture')
        map_input = self.base / 'map.json'; write(map_input, mapping)
        map_dir = self.base / 'map'
        artifact_tools.compile_map(args(source=source, source_ir=capture / 'source_ir.json', input=map_input, output_dir=map_dir))
        decisions = self.base / 'items.json'; write(decisions, item_decisions(map_dir))
        baseline_dir = self.base / 'baseline'
        artifact_tools.compile_items(args(map_dir=map_dir, input=decisions, output_dir=baseline_dir))
        initial = core.initialize_workspace(self.base / 'working', source, '按原文核对修订并交付。')
        self.root = Path(initial['task_root']); self.review = self.root / 'review'
        op = core.begin_operation(self.root, 'review-open', {'baseline_dir': baseline_dir})
        revision_tools.open_review(args(review_dir=self.review, source=self.root / initial['context']['source']['path'], baseline_dir=baseline_dir))
        core.commit_operation(self.root, op['operation_id'])
        self.sid = revision_tools.bundle(self.review / 'versions/initial')[1].records[0].source_record_id

    def tearDown(self): self.temp.cleanup()

    def request(self, *, annotation=False):
        state = core.workspace_status(self.root, refresh=False); head = revision_tools.load_head(self.review)
        revision = {'schema_version': 'reqpilot-review-revision/v1', 'expected_review_sha256': head['review_sha256'],
            'expected_baseline_sha256': head['current']['baseline_sha256'], 'model_id': 'deterministic-test-not-model',
            'decisions': [{'source_record_id': self.sid, 'requirementness': 'REQUIREMENT', 'requirement_kinds': ['CONSTRAINT', 'QUALITY'],
                'confidence': 0.9, 'decision_summary': 'Original voltage condition accompanies the response target.', 'reason_codes': ['source_target']}]}
        if annotation:
            revision.pop('decisions')
            revision['annotation_updates'] = [{'source_item_id': self.sid, 'topics': ['VERIFICATION'], 'reason': 'An explicitly source-grounded review topic.'}]
        return {'expected_revision': state['revision'], 'revision': revision}

    def test_success_replay_and_historical_success_never_overwrites_current(self):
        request = self.request()
        first = task_review.apply_review(self.root, command_id='classify-once', **request)
        self.assertEqual(first['operation_state'], 'COMMITTED', first)
        self.assertTrue(first['review_still_current']); self.assertEqual(first['sync']['status'], 'NOT_BOUND')
        self.assertTrue(Path(first['delivery'], 'matrix.xlsx').is_file())
        self.assertEqual(first['origin'], 'AGENT_SELF_REVIEW')
        self.assertEqual(revision_tools.load_head(self.review)['feedback'], [])
        second = task_review.apply_review(self.root, command_id='annotate-next', **self.request(annotation=True))
        self.assertEqual(second['operation_state'], 'COMMITTED', second)
        with patch.object(revision_tools, 'revise_review') as compile_again, patch.object(artifact_tools, 'deliver') as export_again:
            replay = task_review.apply_review(self.root, command_id='classify-once', **request)
            self.assertEqual(replay['new_review_sha256'], first['new_review_sha256'])
            self.assertFalse(replay['review_still_current']); self.assertTrue(replay['historical_success'])
            compile_again.assert_not_called(); export_again.assert_not_called()
        self.assertEqual(revision_tools.load_head(self.review)['review_sha256'], second['new_review_sha256'])

    def test_resume_saved_compile_export_and_close_after_process_interruption(self):
        for stage in ('compile', 'deliver', 'close'):
            with self.subTest(stage=stage):
                request = self.request(annotation=stage != 'compile')
                original = task_review._checkpoint
                def stop(directory, actual, value):
                    if actual == stage: raise RuntimeError('simulated process exit after saved ' + actual)
                    original(directory, actual, value)
                with patch.object(revision_tools, 'revise_review', wraps=revision_tools.revise_review) as compiled, patch.object(artifact_tools, 'deliver', wraps=artifact_tools.deliver) as exported, patch.object(revision_tools, 'close_review', wraps=revision_tools.close_review) as closed:
                    with patch.object(task_review, '_checkpoint', side_effect=stop), self.assertRaisesRegex(RuntimeError, 'process exit'):
                        task_review.apply_review(self.root, command_id='interrupt-' + stage, **request)
                    result = task_review.apply_review(self.root, command_id='interrupt-' + stage, **request)
                    self.assertEqual(result['operation_state'], 'COMMITTED', result)
                    self.assertEqual(compiled.call_count, 1); self.assertEqual(exported.call_count, 1); self.assertEqual(closed.call_count, 1)

    def test_history_saved_before_manifest_can_resume_same_close_cas(self):
        request = self.request(); initial = revision_tools.load_head(self.review)
        replace = revision_tools.os.replace
        def stop(source, target):
            if Path(target) == self.review / 'workspace-manifest.json': raise OSError('manifest replace interrupted')
            return replace(source, target)
        with patch.object(revision_tools.os, 'replace', side_effect=stop):
            result = task_review.apply_review(self.root, command_id='history-write-lost', **request)
        self.assertEqual(result['status'], 'UNKNOWN', result)
        self.assertEqual(revision_tools.load_head(self.review)['review_sha256'], initial['review_sha256'])
        self.assertEqual(len(list((self.review / 'reviews').glob('*.json'))), 2)
        resumed = task_review.apply_review(self.root, command_id='history-write-lost', **request)
        self.assertEqual(resumed['operation_state'], 'COMMITTED', resumed)
        self.assertEqual(len(list((self.review / 'reviews').glob('*.json'))), 2)

    def test_conflicting_command_and_competing_review_do_not_force_publish(self):
        request = self.request(); original = revision_tools.close_review
        other_request = self.request(annotation=True); outcomes = []
        def competing(call_args):
            if not outcomes:
                outcomes.append(None)
                outcomes[0] = task_review.apply_review(self.root, command_id='winner', **other_request)
            return original(call_args)
        with patch.object(revision_tools, 'close_review', side_effect=competing):
            loser = task_review.apply_review(self.root, command_id='loser', **request)
        self.assertEqual(outcomes[0]['operation_state'], 'COMMITTED', outcomes)
        self.assertEqual(loser['operation_state'], 'BLOCKED', loser)
        self.assertEqual(revision_tools.load_head(self.review)['review_sha256'], outcomes[0]['new_review_sha256'])
        self.assertTrue(Path(loser['delivery'], 'matrix.xlsx').is_file())
        with self.assertRaisesRegex(ValueError, 'COMMAND_ID_CONFLICT'):
            task_review.apply_review(self.root, command_id='winner', **request)
        with self.assertRaisesRegex(ValueError, 'REVISION_CONFLICT'):
            task_review.apply_review(self.root, command_id='stale-new', **request)

    def test_post_commit_sync_failure_preserves_review_excel_and_replay(self):
        request = self.request()
        with patch('task_workbench.sync_current_review', side_effect=OSError('offline host')):
            result = task_review.apply_review(self.root, command_id='offline', **request)
        self.assertEqual(result['operation_state'], 'COMMITTED', result)
        self.assertEqual(result['verification']['status'], 'POST_COMMIT_CHECK_FAILED')
        self.assertTrue(Path(result['delivery'], 'matrix.xlsx').is_file())
        with patch('task_workbench.sync_current_review') as synchronize:
            replay = task_review.apply_review(self.root, command_id='offline', **request)
            synchronize.assert_not_called(); self.assertEqual(replay['operation_state'], 'COMMITTED')

    def test_compiled_annotation_substage_recovers_without_recompilation(self):
        request = self.request(); request['revision'].pop('decisions')
        head = revision_tools.load_head(self.review)
        envelope = {'schema_version': 'reqpilot-annotation-decisions/v1', 'source_sha256': head['source']['sha256'],
            'baseline_sha256': head['current']['baseline_sha256'], 'document_map_artifact_sha256': head['current']['document_map_sha256'],
            'annotation_updates': [{'source_item_id': self.sid, 'topics': ['VERIFICATION'], 'reason': 'Explicit verification topic.'}]}
        checkpoint = task_review._checkpoint
        def stop(directory, stage, value):
            if stage == 'annotations': raise RuntimeError('compiler response lost')
            checkpoint(directory, stage, value)
        with patch.object(artifact_tools, 'annotation_operation', wraps=artifact_tools.annotation_operation) as compile_overlay:
            with patch.object(task_review, '_checkpoint', side_effect=stop), self.assertRaisesRegex(RuntimeError, 'response lost'):
                task_review.apply_review(self.root, command_id='compiled-overlay', compile_replacements={'annotations': envelope}, **request)
            result = task_review.apply_review(self.root, command_id='compiled-overlay', compile_replacements={'annotations': envelope}, **request)
            self.assertEqual(result['operation_state'], 'COMMITTED', result); self.assertEqual(compile_overlay.call_count, 1)
        annotations = json.loads((Path(result['candidate']) / 'image-annotations.json').read_text())
        self.assertEqual(annotations['records'][self.sid]['topics'], ['VERIFICATION'])

    def test_compiled_decomposition_is_carried_into_same_version_delivery(self):
        request = self.request(); request['revision'].pop('decisions')
        envelope = {'schema_version': 'reqpilot-decomposition-decisions/v1',
            'baseline_sha256': request['revision']['expected_baseline_sha256'],
            'authorization': {'request': 'Review this complete synthetic target; keep it intact.', 'source_item_ids': [self.sid]},
            'decisions': [{'source_item_id': self.sid, 'action': 'KEEP', 'reason': 'One complete response target.'}]}
        result = task_review.apply_review(self.root, command_id='keep-complete-target', compile_replacements={'decomposition': envelope}, **request)
        self.assertEqual(result['operation_state'], 'COMMITTED', result)
        self.assertIn('decomposition', result['diff']['artifact_changes'])
        current = revision_tools.load_head(self.review)
        self.assertIn('source-item-decomposition.json', current['current']['files'])
        self.assertEqual(revision_tools.verify_review(args(review_dir=self.review))['status'], 'REVIEW_VERIFIED')

    def test_scope_replacement_uses_existing_lineage_compiler_and_preserves_unaffected_items(self):
        previous = revision_tools.bundle(self.review / 'versions/initial')[1]
        finding_path = self.base / 'self-review-finding.json'
        write(finding_path, {'expected_review_sha256': revision_tools.load_head(self.review)['review_sha256'],
            'feedback': [{'origin': 'AGENT_SELF_REVIEW', 'source_item_ids': [self.sid],
                'text': 'Synthetic scope-form test: preserve the exact captured target while changing its declared source form.'}]})
        finding = revision_tools.feedback_review(args(review_dir=self.review, input=finding_path))
        request = self.request(); request['revision'].pop('decisions')
        request['revision']['addressed_feedback_ids'] = finding['added_feedback_ids']
        packet = revision_tools.packet_review(args(review_dir=self.review, source_item_id=[self.sid]))
        candidate = packet['scope_revision_templates'][0]['candidate_template']
        candidate['items'][0]['source_form'] = 'PARAGRAPH'
        request['revision']['scope_revision'] = {'operation': 'REPLACE', 'affected_scope_ids': [candidate['scope_id']], 'text_decisions': [candidate]}
        result = task_review.apply_review(self.root, command_id='scope-replacement', **request)
        self.assertEqual(result['operation_state'], 'COMMITTED', result)
        current = revision_tools.bundle(Path(result['candidate']))[1]
        self.assertEqual(current.records[0].source_record_id, self.sid)
        self.assertEqual(current.records[0].text_bindings, previous.records[0].text_bindings)
        self.assertEqual(current.records[1:], previous.records[1:])
        self.assertEqual(current.records[0].source_form.value, 'PARAGRAPH')

    def test_scope_self_finding_is_one_command_and_lost_finding_response_does_not_duplicate(self):
        request = self.request(); request['revision'].pop('decisions')
        packet = revision_tools.packet_review(args(review_dir=self.review, source_item_id=[self.sid]))
        candidate = packet['scope_revision_templates'][0]['candidate_template']
        candidate['items'][0]['source_form'] = 'PARAGRAPH'
        request['revision']['scope_revision'] = {'operation': 'REPLACE', 'affected_scope_ids': [candidate['scope_id']], 'text_decisions': [candidate]}
        finding = {'text': 'The exact synthetic target should use paragraph source form without changing source coverage.',
            'source_item_ids': [self.sid], 'rationale': 'Self-review of the complete captured target.'}
        checkpoint = task_review._checkpoint
        def stop(directory, stage, value):
            if stage == 'finding': raise RuntimeError('finding response lost after real save')
            checkpoint(directory, stage, value)
        with patch.object(revision_tools, 'feedback_review', wraps=revision_tools.feedback_review) as record_finding:
            with patch.object(task_review, '_checkpoint', side_effect=stop), self.assertRaisesRegex(RuntimeError, 'response lost'):
                task_review.apply_review(self.root, command_id='self-finding-and-scope', self_review_finding=finding, **request)
            result = task_review.apply_review(self.root, command_id='self-finding-and-scope', self_review_finding=finding, **request)
            self.assertEqual(result['operation_state'], 'COMMITTED', result)
            self.assertEqual(record_finding.call_count, 1)
        self.assertEqual(result['old_review_sha256'], request['revision']['expected_review_sha256'])
        middle = json.loads((self.review / 'reviews' / (result['finding_review_sha256'] + '.json')).read_text())
        head = revision_tools.load_head(self.review)
        self.assertEqual(middle['predecessor_review_sha256'], result['old_review_sha256'])
        self.assertEqual(head['predecessor_review_sha256'], result['finding_review_sha256'])
        self.assertEqual(len(head['feedback']), 1)
        self.assertEqual(head['feedback'][0]['origin'], 'AGENT_SELF_REVIEW')
        self.assertEqual(head['feedback'][0]['text'], finding['text'])
        self.assertEqual(head['feedback'][0]['status'], 'ADDRESSED')
        self.assertEqual(revision_tools.bundle(Path(result['candidate']))[1].records[0].source_form.value, 'PARAGRAPH')
        with patch.object(revision_tools, 'feedback_review') as repeated:
            replay = task_review.apply_review(self.root, command_id='self-finding-and-scope', self_review_finding=finding, **request)
            self.assertTrue(replay['replayed']); repeated.assert_not_called()

    def test_user_origin_requires_real_feedback_and_resolution_preserves_its_origin(self):
        with self.assertRaisesRegex(ValueError, 'actual recorded USER'):
            task_review.apply_review(self.root, command_id='invented-user', origin='USER', **self.request())
        feedback_path = self.base / 'user-feedback.json'
        write(feedback_path, {'expected_review_sha256': revision_tools.load_head(self.review)['review_sha256'],
            'feedback': [{'origin': 'USER', 'source_item_ids': [self.sid], 'text': 'Keep the voltage constraint with the timing requirement.'}]})
        feedback = revision_tools.feedback_review(args(review_dir=self.review, input=feedback_path))
        fid = feedback['added_feedback_ids'][0]
        request = self.request(); request['revision']['addressed_feedback_ids'] = [fid]
        result = task_review.apply_review(self.root, command_id='real-user', origin='USER',
            resolutions=[{'feedback_id': fid, 'disposition': 'ADDRESSED', 'reason': 'Preserved the complete target and corrected its classes.'}], **request)
        self.assertEqual(result['operation_state'], 'COMMITTED', result)
        self.assertEqual(result['origin'], 'USER')
        actual = revision_tools.load_head(self.review)['feedback']
        self.assertEqual(len(actual), 1); self.assertEqual(actual[0]['origin'], 'USER'); self.assertEqual(actual[0]['status'], 'ADDRESSED')

    def test_unknown_partial_candidate_is_retained_without_blind_reexecution(self):
        request = self.request()
        def partial(args):
            args.output_dir.mkdir(); (args.output_dir / 'partial.txt').write_text('partial compiler result')
            raise RuntimeError('process stopped during candidate write')
        with patch.object(revision_tools, 'revise_review', side_effect=partial), self.assertRaisesRegex(RuntimeError, 'process stopped'):
            task_review.apply_review(self.root, command_id='unknown-partial', **request)
        with patch.object(revision_tools, 'revise_review') as rerun:
            result = task_review.apply_review(self.root, command_id='unknown-partial', **request)
            self.assertEqual(result['status'], 'UNKNOWN', result); rerun.assert_not_called()
        self.assertEqual((Path(result['candidate']) / 'partial.txt').read_text(), 'partial compiler result')

    def test_unadopted_history_is_not_recorded_as_committed(self):
        request = self.request(); replace = revision_tools.os.replace
        def stop(source, target):
            if Path(target) == self.review / 'workspace-manifest.json': raise OSError('interrupt CAS')
            return replace(source, target)
        with patch.object(revision_tools.os, 'replace', side_effect=stop):
            result = task_review.apply_review(self.root, command_id='unadopted-history', **request)
        operation = core._operation(self.root, result['operation_id'])
        orphan = next(json.loads(p.read_text()) for p in (self.review / 'reviews').glob('*.json')
            if json.loads(p.read_text()).get('predecessor_review_sha256'))
        output = core.inside(self.root, operation['output_dir'])
        write(output / 'review-application.json', {'schema_version': 'reqpilot-review-application/v1',
            'operation_id': operation['operation_id'], 'source_sha256': orphan['source']['sha256'],
            'old_review_sha256': request['revision']['expected_review_sha256'], 'new_review_sha256': orphan['review_sha256'],
            'candidate_path': orphan['current']['version_path'], 'delivery_path': orphan['delivery']['path']})
        with self.assertRaisesRegex(ValueError, 'not adopted'):
            core.commit_operation(self.root, operation['operation_id'])
        self.assertEqual(len(core.workspace_status(self.root)['index']['committed']), 1)


if __name__ == '__main__': unittest.main()
