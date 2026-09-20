from copy import deepcopy
import unittest
from tests.runtime_isolation import activate_test_runtime
activate_test_runtime()
from tests.test_source_item_lineage import source_fixture
from tests.test_source_item_analysis import packet, decision
from app.application.agent_workflow import source_requirement_baseline as s
from app.application.agent_workflow import source_item_decomposition as d
from app.application.agent_workflow import source_item_analysis as a

def seal_artifact(value,prefix):
    value=deepcopy(value)
    h=s.canonical_json_sha256({k:v for k,v in value.items() if k not in {'artifact_id','artifact_sha256'}})
    value.update(artifact_id=prefix+h[:32],artifact_sha256=h)
    return value

def seal_packet(value):
    value['packet_sha256']=s.canonical_json_sha256({k:v for k,v in value.items() if k!='packet_sha256'})
    return value

class IndependentCarryAudit(unittest.TestCase):
    def setUp(self):
        self.text='Open the valve. Close the valve.'
        _,_,_,self.b,self.receipt=source_fixture(self.text)
        self.sid=self.b.records[0].source_record_id
        self.dec={'source_item_id':self.sid,'action':'SPLIT','reason':'Independent behaviors','children':[
          {'text':self.text[x:y],'source_ranges':[{'segment_index':0,'start_offset':x,'end_offset':y}]} for x,y in [(0,15),(16,len(self.text))]]}
        self.art=d.compile_source_item_decomposition(self.b,[self.dec])
    def successor(self,kinds=None):
        change=s.SourceRequirementClassificationCoordinationDecisionV1(source_record_id=self.sid,requirementness='REQUIREMENT',
          requirement_kinds=kinds or self.b.records[0].requirement_kinds,confidence=.95,decision_summary='Reviewed',reason_codes=('source_review',))
        return s.compile_source_requirement_classification_coordination(previous_artifact=self.b,previous_validation_receipt=self.receipt,decisions=(change,),model_id='synthetic')[0]
    def reserved_art(self):
        out=deepcopy(self.art)
        out['reserved_display_ids']=[{'id':'REQ-0001-S03','historical_id':'withdrawn-legacy-child','reason':'Do not reuse'}]
        return seal_artifact(out,'source-item-decomposition-')
    def test_reserved_legacy_snn_survives_noop_compile(self):
        previous=self.reserved_art()
        result=d.compile_source_item_decomposition(self.b,[],previous=previous)
        self.assertEqual(result.get('reserved_display_ids'),previous['reserved_display_ids'])
    def test_reserved_legacy_snn_is_never_reissued(self):
        changed=deepcopy(self.dec)
        for child in changed['children']:
            child['text']='The system shall '+child['text'].lower()
            child['derivation_type']='NORMALIZED_DERIVATION'
        result=d.compile_source_item_decomposition(self.b,[changed],previous=self.reserved_art())
        self.assertNotIn('REQ-0001-S03',[x['requirement_display_id'] for x in result['records'][self.sid]])
    def test_stale_split_does_not_become_complete_without_action(self):
        newer=self.successor(('QUALITY',))
        rebound=d.rebind_source_item_decomposition(self.b,newer,self.art)['artifact']
        self.assertEqual(rebound['status'],'PARTIAL')
        followup=d.compile_source_item_decomposition(newer,[],previous=rebound)
        self.assertEqual(followup['status'],'PARTIAL')
        self.assertIn(self.sid,followup.get('carry_forward',{}).get('stale_parent_ids',[]))
    def test_forged_source_req_identity_is_rejected_even_when_resealed(self):
        bad=deepcopy(self.art)
        bad['records'][self.sid][0]['source_requirement_id']='REQ-9999'
        bad=seal_artifact(bad,'source-item-decomposition-')
        with self.assertRaises(ValueError):
            d.rebind_source_item_decomposition(self.b,self.successor(),bad)
    def test_changed_unit_content_with_old_registry_is_rejected(self):
        old=packet();artifact=a.compile_source_item_assessments(old,{'decisions':[decision('a'),decision('b')]})
        new=deepcopy(old);new['baseline_sha256']='c'*64
        new['units'][0]['source_segments'][0]['text']='Standby current shall be <= 3 mA at 12 V and 25 C.'
        seal_packet(new)
        with self.assertRaises(ValueError):
            a.rebind_source_item_assessments(old,artifact,new)
    def test_changed_visual_content_with_old_registry_is_rejected(self):
        old=packet();old['units'][0]['visual_interpretation']={'visual_interpretation':{'text':'Dimension 5 mm','baseline_sha256':old['baseline_sha256']}}
        old['unit_registry']['a']['unit_sha256']=s.canonical_json_sha256(old['units'][0]);seal_packet(old)
        artifact=a.compile_source_item_assessments(old,{'decisions':[decision('a'),decision('b')]})
        new=deepcopy(old);new['baseline_sha256']='c'*64
        new['units'][0]['visual_interpretation']['visual_interpretation']['text']='Dimension not readable'
        seal_packet(new)
        with self.assertRaises(ValueError):
            a.rebind_source_item_assessments(old,artifact,new)
    def test_removed_retired_registry_cannot_reissue_history(self):
        newer=deepcopy(self.dec)
        for child in newer['children']:
            child['text']='The system shall '+child['text'].lower();child['derivation_type']='NORMALIZED_DERIVATION'
        second=d.compile_source_item_decomposition(self.b,[newer],previous=self.art)
        bad=deepcopy(second)
        for oldchild in self.art['records'][self.sid]:bad['identity_registry'][self.sid].pop(oldchild['singular_requirement_id'])
        bad=seal_artifact(bad,'source-item-decomposition-')
        with self.assertRaises(ValueError):d.validate_source_item_decomposition(self.b,bad)

if __name__=='__main__':unittest.main(verbosity=2)
