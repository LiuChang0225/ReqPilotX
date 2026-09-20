"""Program-owned SRC allocation across immutable legacy review history."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import patch
import unittest

import test_review_loop as review_fixture
from test_portable import artifact_tools, item_decisions
args, loop = review_fixture.args, review_fixture.loop


class SourceIdentityAllocatorTests(unittest.TestCase):
    setUp = review_fixture.ReviewLoopTests.setUp
    tearDown = review_fixture.ReviewLoopTests.tearDown
    put = review_fixture.ReviewLoopTests.put

    def current(self):
        head = loop.load_head(self.review)
        directory, (_, baseline, _, _) = loop.current_bundle(self.review, head)
        return head, directory, baseline

    def feedback_for(self, sid):
        head = self.current()[0]
        result = loop.feedback_review(args(review_dir=self.review, input=self.put('feedback.json', {
            'expected_review_sha256': head['review_sha256'], 'feedback': [{'origin': 'AGENT_SELF_REVIEW',
            'source_item_ids': [sid] if sid else [], 'text': 'Review the complete original obligations in this synthetic fixture.'}]})))
        return result['added_feedback_ids'][0]

    def split(self, sid):
        packet = loop.packet_review(args(review_dir=self.review, source_item_id=[sid]))
        template = deepcopy(packet['scope_revision_templates'][0]['candidate_template'])
        record = next(r for r in self.current()[2].records if r.source_record_id == sid)
        binding = record.text_bindings[0]
        cut = binding.start_offset + binding.text.index('\n') + 1
        first, second = deepcopy(template['items'][0]), deepcopy(template['items'][0])
        first.update(item_key='opening', label='Complete opening obligation')
        second.update(item_key='closing', label='Complete closing obligation')
        first['text_spans'][0]['end_offset'] = cut; second['text_spans'][0]['start_offset'] = cut
        template['items'] = [first, second]
        return {'operation': 'REPLACE', 'affected_scope_ids': [template['scope_id']], 'text_decisions': [template]}

    def advance(self, name, *, close=True, **fields):
        head = self.current()[0]
        value = {'schema_version': 'reqpilot-review-revision/v1', 'expected_review_sha256': head['review_sha256'],
            'expected_baseline_sha256': head['current']['baseline_sha256'], **fields}
        directory = self.review / 'versions' / name
        loop.revise_review(args(review_dir=self.review, input=self.put(name+'.json', value), output_dir=directory))
        if close:
            delivery = self.review / 'deliveries' / name
            artifact_tools.deliver(args(baseline_dir=directory, source=self.source, output_dir=delivery))
            loop.close_review(args(review_dir=self.review, input=self.put(name+'-close.json', {
                'expected_review_sha256': head['review_sha256'], 'candidate_path': str(directory.relative_to(self.review)),
                'delivery_path': str(delivery.relative_to(self.review))})))
        return directory

    def classification(self):
        return {'model_id': 'synthetic-explicit-review', 'decisions': [{'source_record_id': self.sid,
            'requirementness': 'REQUIREMENT', 'requirement_kinds': ['CONSTRAINT', 'QUALITY'], 'confidence': .9,
            'decision_summary': 'The original response target includes the operating supply.', 'reason_codes': ['complete_target']}]}

    def legacy_head(self):
        # Generate old policy through the public tools, without editing/re-signing
        # any artifact. None is exactly the pre-policy producer behavior.
        self.assertIsNone(self.current()[2].source_display_high_watermark)
        with patch.object(loop, '_new_source_identity_allocation', return_value=None):
            fid = self.feedback_for(self.sid); selection = self.split(self.sid)
            split = self.advance('legacy-split', addressed_feedback_ids=[fid], scope_revision=selection)
            proof = json.loads((split/'scope-revision-receipt.json').read_text())
            self.retired = {i['source_display_id'] for i in proof['created_identities']}
            self.retired_max = proof['source_display_id_high_watermark_after']
            self.advance('legacy-undo', addressed_feedback_ids=[fid], scope_revision={
                'operation': 'UNDO', 'affected_scope_ids': selection['affected_scope_ids'], 'undo_target_path': 'versions/initial'})
            self.advance('legacy-classification', **self.classification())
        self.assertIsNone(self.current()[2].source_display_high_watermark)
        self.assertFalse((self.current()[1]/'scope-revision-receipt.json').exists())

    def test_legacy_hash_replay_and_new_split_do_not_reuse_retired_numbers(self):
        self.legacy_head()
        frozen = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.review.rglob('*.json') if p.name != 'workspace-manifest.json'}
        self.assertEqual(loop.verify_review(args(review_dir=self.review))['status'], 'REVIEW_VERIFIED')
        # No-op classification still creates a legitimate successor, with the
        # recovered high watermark persisted rather than a copied old proof.
        classified = self.advance('new-noop-classification', **self.classification())
        self.assertEqual(loop.bundle(classified)[1].source_display_high_watermark, self.retired_max)
        self.assertFalse((classified/'scope-revision-receipt.json').exists())
        fid = self.feedback_for(self.sid)
        compiled = self.advance('new-split', addressed_feedback_ids=[fid], scope_revision=self.split(self.sid))
        proof = json.loads((compiled/'scope-revision-receipt.json').read_text())
        created = {i['source_display_id'] for i in proof['created_identities']}
        self.assertFalse(self.retired & created)
        self.assertEqual(proof['source_display_id_high_watermark_before'], self.retired_max)
        self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in frozen.items()))
        with patch.object(loop, '_verified_source_identity_allocation', wraps=loop._verified_source_identity_allocation) as replay:
            self.assertEqual(loop.verify_review(args(review_dir=self.review))['status'], 'REVIEW_VERIFIED')
            self.assertEqual(replay.call_count, 0, 'Full verification must scan its prefix once, not recursively replay it')

    def test_model_cannot_supply_allocation_and_resigned_lower_context_is_rejected(self):
        self.legacy_head()
        with self.assertRaisesRegex(ValueError, 'computed by the program'):
            self.advance('caller-floor', source_identity_allocation={'high_watermark': 1}, **self.classification())
        before_dir, previous = self.current()[1:]
        candidate = self.advance('candidate', close=False, **self.classification())
        value = json.loads((candidate/'revision-input.json').read_text())
        value['source_identity_allocation']['high_watermark'] = loop.core._baseline_display_high_watermark(previous)
        context = value['source_identity_allocation']
        context['allocation_sha256'] = loop.core.canonical_json_sha256({k:v for k,v in context.items() if k!='allocation_sha256'})
        (candidate/'revision-input.json').write_text(json.dumps(value))
        document_map, current, _, tables = loop.bundle(candidate)
        with self.assertRaisesRegex(ValueError, 'immutable review history'):
            loop.verify_transition(self.review, before_dir, candidate, document_map, previous, current, tables)

    def test_map_and_text_recovery_consumes_legacy_history_floor(self):
        original = json.loads((self.root/'map.json').read_text()); wrong = deepcopy(original)
        owner = next(n for n in wrong['nodes'] if n['node_id']=='s4'); ids=owner['content_source_ids'];owner['content_source_ids']=[]
        wrong['nodes'].append({'node_id':'misread-clause','parent_id':'s4','kind':'SECTION','label':'',
            'title':'The housing shall use aluminium.','title_source_ids':ids,'content_source_ids':[]})
        wrong_map, wrong_base = self.root/'wrong-map',self.root/'wrong-base'
        artifact_tools.compile_map(args(source=self.source,source_ir=self.root/'capture/source_ir.json',input=self.put('wrong-map.json',wrong),output_dir=wrong_map))
        artifact_tools.compile_items(args(map_dir=wrong_map,input=self.put('wrong-items.json',item_decisions(wrong_map)),output_dir=wrong_base))
        self.review=self.root/'map-review';loop.open_review(args(review_dir=self.review,baseline_dir=wrong_base,source=self.source))
        self.sid=next(r.source_record_id for r in loop.bundle(wrong_base)[1].records if r.node_id=='s1')
        self.legacy_head()
        candidate_map=self.review/'map-candidates/fixed'
        artifact_tools.compile_map(args(source=self.source,source_ir=self.root/'capture/source_ir.json',input=self.put('fixed-map.json',original),output_dir=candidate_map))
        selection=next(i for i in item_decisions(candidate_map)['text_decisions'] if i['scope_id']=='text:s4')
        before=self.current()[2];fid=self.feedback_for(None)
        candidate=self.advance('map-recovered', addressed_feedback_ids=[fid], map_revision={'candidate_map_path':str(candidate_map.relative_to(self.review))},
            scope_revision={'operation':'REPLACE','affected_scope_ids':['text:s4'],'text_decisions':[selection]})
        after=loop.bundle(candidate)[1];old_ids={r.source_record_id for r in before.records}
        added=[r for r in after.records if r.source_record_id not in old_ids]
        self.assertEqual([r.source_display_id for r in added],[f'SRC-{self.retired_max+1:04d}'])
        proof=json.loads((candidate/'map-rebind-receipt.json').read_text())
        self.assertEqual(proof['source_identity_allocation']['high_watermark'],self.retired_max)
        self.assertEqual(proof['text_scope_recovery']['scope_receipt']['source_display_id_high_watermark_before'],self.retired_max)
        self.assertEqual(loop.verify_review(args(review_dir=self.review))['status'],'REVIEW_VERIFIED')

    def test_missing_ancestor_rejects_new_allocation_instead_of_guessing_active_max(self):
        self.legacy_head()
        head=self.current()[0];ancestor=self.review/'reviews'/(head['predecessor_review_sha256']+'.json')
        ancestor.unlink()
        with self.assertRaises(FileNotFoundError):self.advance('missing-history',**self.classification())
        self.assertFalse((self.review/'versions/missing-history').exists())


if __name__=='__main__':unittest.main()
