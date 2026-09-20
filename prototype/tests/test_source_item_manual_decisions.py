import unittest
from copy import deepcopy
from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow.source_item_manual_decisions import (
    compile_manual_decisions, rebind_manual_decisions, manual_decision_projection, validate_manual_decisions, ManualDecisionError)
from app.application.agent_workflow.source_requirement_baseline import canonical_json_sha256
from app.domain.requirement_matrix import MatrixNodeStateV1, matrix_conclusion_contract


def context(value='a',baseline='b'):
    body={'schema_version':'source-item-manual-context/v1','source_sha256':'c'*64,'baseline_sha256':baseline*64,
        'units':{'u':{'unit_id':'u','source_item_id':'src','requirement_display_id':'REQ-0001','unit_context_sha256':value*64}}}
    return {**body,'context_sha256':canonical_json_sha256(body)}


class ManualDecisionTests(unittest.TestCase):
    def write(self,ctx=None,previous=None,action='SET'):
        return compile_manual_decisions(ctx or context(),[{'unit_id':'u','action':action,'decision':'NON_COMPLIANT',
            'reason':'工程师工作意见。','conditions':'当前产品版本','evidence_requests':'请补充试验记录。'}],
            previous=previous,origin='USER_INPUT',decided_at='2026-09-08T00:00:00+00:00')

    def test_manual_has_effective_priority_and_keeps_ai(self):
        opinion=self.write();projection=manual_decision_projection(opinion,context())['u']
        node=MatrixNodeStateV1(node_id='u',parent_id=None,depth=0,source_order=0,display_role='CLAUSE',label='原文',
            requirement_id='u',requirement_entry_id='u',
            matrix_visibility='VISIBLE',disposition='ANALYZE',analysis_eligibility='ELIGIBLE',analysis_status='DECIDED',
            analysis_result_state='CURRENT',ai_assessment={'decision':'COMPLIANT','reason':'原有AI建议'},manual_decision=projection['manual_decision'])
        self.assertEqual(matrix_conclusion_contract(node).effective_conclusion,'NON_COMPLIANT')
        self.assertEqual(node.ai_assessment.reason,'原有AI建议')
        self.assertIn('适用条件：当前产品版本',node.manual_decision.reason)
        self.assertEqual(opinion['human_acceptance'],'NOT_CLAIMED')

    def test_withdrawal_keeps_history_without_current_decision(self):
        first=self.write();later=self.write(previous=first,action='WITHDRAW')
        self.assertEqual(later['records']['u']['state'],'WITHDRAWN')
        self.assertNotIn('manual_decision',manual_decision_projection(later,context())['u'])
        self.assertEqual(later['history'][0],first['records']['u'])

    def test_changed_source_is_stale_and_does_not_revive_when_restored(self):
        first=self.write();changed=rebind_manual_decisions(first,context('d','e'),previous_context=context())
        self.assertEqual(changed['records']['u']['state'],'STALE')
        restored=rebind_manual_decisions(changed,context())
        self.assertEqual(restored['records']['u']['state'],'STALE')
        self.assertNotIn('manual_decision',manual_decision_projection(restored,context())['u'])
        self.assertEqual(self.write(previous=restored)['records']['u']['state'],'CURRENT')

    def test_unaffected_context_carries_and_unknown_previous_is_conservative(self):
        first=self.write();carried=rebind_manual_decisions(first,context(baseline='f'),previous_context=context())
        self.assertEqual(carried['records']['u'],first['records']['u'])
        uncertain=rebind_manual_decisions(first,context(),require_previous_context=True)
        self.assertEqual(uncertain['records']['u']['stale_reason'],'MISSING_PREVIOUS_CONTEXT')

    def test_model_origin_and_tampered_artifact_are_rejected(self):
        with self.assertRaises(ManualDecisionError):compile_manual_decisions(context(),[{}],origin='AGENT',decided_at='2026-09-08T00:00:00Z')
        changed=deepcopy(self.write());changed['records']['u']['reason']='偷偷改变'
        with self.assertRaises(ManualDecisionError):validate_manual_decisions(changed)

    def test_user_write_on_new_context_has_one_real_direct_predecessor(self):
        first=self.write();updated=self.write(context('d','e'),previous=first)
        self.assertEqual(updated['revision'],first['revision']+1)
        self.assertEqual(updated['predecessor_sha256'],first['artifact_sha256'])
        self.assertEqual(updated['records']['u']['state'],'CURRENT')
        self.assertIn(first['records']['u'],updated['history'])
        self.assertTrue(any(r['state']=='STALE' for r in updated['history']))


if __name__=='__main__':unittest.main()
