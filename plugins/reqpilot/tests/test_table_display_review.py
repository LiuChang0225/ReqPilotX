"""Explicit Agent table labels through the real public review/export tools."""
from copy import deepcopy
import json
import unittest

from openpyxl import load_workbook
import test_review_loop as fixture
from test_portable import artifact_tools
args,loop=fixture.args,fixture.loop


class TableDisplayReviewTests(unittest.TestCase):
    setUp=fixture.ReviewLoopTests.setUp
    tearDown=fixture.ReviewLoopTests.tearDown
    put=fixture.ReviewLoopTests.put

    def test_display_only_review_keeps_analysis_and_exports_one_marked_table_name(self):
        packet_dir=self.root/'analysis'
        artifact_tools.analysis_operation(args(command='analysis-packet',baseline_dir=self.baseline_dir,unit_id=None,output_dir=packet_dir))
        packet=json.loads((packet_dir/'analysis-packet.json').read_text())
        decisions={'decisions':[{'unit_id':u['unit_id'],'decision':'PENDING_EVIDENCE','reason':'Synthetic verification has no supplied internal result.',
            'evidence':[],'clarification_question':'Provide the corresponding internal test record.'} for u in packet['units']]}
        assessed=self.root/'assessed'
        artifact_tools.analysis_operation(args(command='compile-assessments',analysis_packet=packet_dir/'analysis-packet.json',
            input=self.put('assessment-input.json',decisions),previous_assessments=None,output_dir=assessed))
        self.review=self.root/'label-review'
        head=loop.open_review(args(review_dir=self.review,baseline_dir=self.baseline_dir,source=self.source,
            assessments=assessed/'source-item-assessments.json',analysis_packet=assessed/'analysis-packet.json'))
        head=loop.feedback_review(args(review_dir=self.review,input=self.put('label-feedback.json',{'expected_review_sha256':head['review_sha256'],
            'feedback':[{'origin':'AGENT_SELF_REVIEW','source_item_ids':[],
            'text':'Use an explicit short Agent display label for this synthetic table; preserve its source title and cells.'}]})))
        original=json.loads((self.root/'map.json').read_text());decisions=deepcopy(original)
        decisions['logical_tables'][0]['agent_display_label']='合成模块测试清单'
        candidate_map=self.review/'map-candidates/display'
        artifact_tools.compile_map(args(source=self.source,source_ir=self.root/'capture/source_ir.json',
            input=self.put('label-map.json',decisions),output_dir=candidate_map))
        candidate=self.review/'versions/display'
        result=loop.revise_review(args(review_dir=self.review,input=self.put('label-revision.json',{
            'schema_version':'reqpilot-review-revision/v1','expected_review_sha256':head['review_sha256'],
            'expected_baseline_sha256':head['baseline_sha256'],'addressed_feedback_ids':head['added_feedback_ids'],
            'map_revision':{'candidate_map_path':str(candidate_map.relative_to(self.review))}}),output_dir=candidate))
        self.assertEqual(result['carry_forward']['stale_unit_ids'],[])
        after=loop.bundle(candidate)[1]
        self.assertEqual([r.source_display_id for r in after.records],[r.source_display_id for r in self.baseline.records])
        self.assertEqual([r.requirement_display_id for r in after.records],[r.requirement_display_id for r in self.baseline.records])
        carried=json.loads((candidate/'source-item-assessments.json').read_text())
        old=json.loads((assessed/'source-item-assessments.json').read_text())
        self.assertEqual(set(carried['records']),set(old['records']))
        self.assertEqual({k:v['decision'] for k,v in carried['records'].items()},{k:v['decision'] for k,v in old['records'].items()})
        rebound_packet=json.loads((candidate/'analysis-packet.json').read_text())
        self.assertEqual(rebound_packet['units'],packet['units'], 'Display metadata must not rewrite analysis inputs')
        delivery=self.review/'deliveries/display'
        artifact_tools.deliver(args(baseline_dir=candidate,source=self.source,output_dir=delivery,
            assessments=candidate/'source-item-assessments.json',analysis_packet=candidate/'analysis-packet.json'))
        snapshot=json.loads((delivery/'matrix-snapshot.json').read_text());table=snapshot['logical_tables'][0]
        self.assertEqual(table['title'],'合成模块测试清单')
        self.assertEqual(table['projection']['title_presentation'],{'display_title':'合成模块测试清单','authority':'AGENT_DISPLAY_LABEL',
            'map_title':original['logical_tables'][0]['title']})
        wb=load_workbook(delivery/'matrix.xlsx');sheet=next(s for s in wb if s.title.startswith('T001'))
        self.assertIn('合成模块测试清单',sheet.title)
        self.assertEqual(sheet['A2'].value,'合成模块测试清单')
        self.assertIn('显示名（AI）',sheet['A3'].value)
        self.assertIn('Open <= 30 ms',{c.value for row in sheet for c in row})
        loop.close_review(args(review_dir=self.review,input=self.put('label-close.json',{'expected_review_sha256':head['review_sha256'],
            'candidate_path':str(candidate.relative_to(self.review)),'delivery_path':str(delivery.relative_to(self.review))})))
        self.assertEqual(loop.verify_review(args(review_dir=self.review))['status'],'REVIEW_VERIFIED')
        reading=loop.packet_review(args(review_dir=self.review,source_item_id=[]))
        self.assertEqual(reading['table_presentations']['technical-test'],table['projection']['title_presentation'])


if __name__=='__main__':unittest.main()
