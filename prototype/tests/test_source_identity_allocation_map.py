"""History allocation contexts keep the existing Map/source proof boundary."""

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from copy import deepcopy
import unittest

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.document_map_revision import rebind_baseline_for_document_map_revision, map_rebind_added_source_ids
from app.application.agent_workflow.source_item_decomposition import _map_rebind_context_changes
from . import test_document_map_text_recovery as fixtures


class HistoryAllocationMapTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.MapTextRecoveryTests();self.case.setUp()
        c=self.case
        records=tuple(r.model_copy(update={'decision_origin':'USER_OVERRIDE','review_state':'REVIEWED',
            'decision_summary':'Existing human judgment.'}) if r.text_bindings else r for r in c.before.records)
        draft=c.before.model_copy(update={'records':records,'source_display_high_watermark':None})
        digest=core.canonical_json_sha256(core._baseline_hash_payload(draft))
        c.before=core.SourceRequirementBaselineArtifactV1.model_validate({**draft.model_dump(mode='json'),
            'artifact_id':'source-requirement-baseline-'+digest[:32],'artifact_sha256':digest})
        c.validation=core._compile_validation_receipt_from_artifact(document_map=c.before_map,artifact=c.before)
        value={'policy':core.SOURCE_IDENTITY_ALLOCATION_POLICY,'review_sha256':'a'*64,'history_prefix_sha256':'b'*64,
            'source_sha256':c.before.source_sha256,'previous_baseline_artifact_id':c.before.artifact_id,
            'previous_baseline_sha256':c.before.artifact_sha256,'high_watermark':200}
        self.allocation={**value,'allocation_sha256':core.canonical_json_sha256(value)}

    def recover(self,allocation):
        c=self.case
        return rebind_baseline_for_document_map_revision(c.before_map,c.after_map,c.before,c.validation,c.tables,
            text_scope_recovery=c.selection,feedback_ids=('synthetic-review',),source_identity_allocation=allocation)

    def test_context_floor_and_manual_judgments_survive_exact_scope_output_validation(self):
        c=self.case;result=self.recover(self.allocation);final=core.SourceRequirementBaselineArtifactV1.model_validate(result['baseline'])
        old={r.source_record_id:r for r in c.before.records};new=[r for r in final.records if r.source_record_id not in old]
        self.assertEqual([r.source_display_id for r in new],['SRC-0201'])
        self.assertEqual(final.source_display_high_watermark,201)
        for r in final.records:
            if r.source_record_id in old:
                prior=old[r.source_record_id]
                self.assertEqual((r.decision_origin,r.review_state,r.decision_summary),(prior.decision_origin,prior.review_state,prior.decision_summary))
        self.assertEqual(map_rebind_added_source_ids(result['receipt'],current_baseline=final),{new[0].source_record_id})
        _map_rebind_context_changes(c.before,final,result['receipt'],c.tables,result['table_previews'])
        forged=deepcopy(result['receipt']);scope=forged['text_scope_recovery']['scope_receipt']
        scope.update(artifact_id='source-requirement-baseline-'+'f'*32,artifact_sha256='f'*64)
        scope['receipt_sha256']=core.canonical_json_sha256({k:v for k,v in scope.items() if k!='receipt_sha256'})
        forged['receipt_sha256']=core.canonical_json_sha256({k:v for k,v in forged.items() if k!='receipt_sha256'})
        with self.assertRaises(ValueError):map_rebind_added_source_ids(forged,current_baseline=final)

    def test_wrong_predecessor_context_is_rejected_even_when_self_consistent(self):
        value=deepcopy(self.allocation);value['previous_baseline_sha256']='f'*64
        value['allocation_sha256']=core.canonical_json_sha256({k:v for k,v in value.items() if k!='allocation_sha256'})
        with self.assertRaisesRegex(ValueError,'ALLOCATION_CONTEXT_DRIFT'):self.recover(value)
        with self.assertRaises(ValueError):core._validate_source_display_high_watermark_floor(self.case.before,True)


if __name__=='__main__':unittest.main()
