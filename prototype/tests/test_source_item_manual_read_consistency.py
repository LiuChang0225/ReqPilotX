from types import SimpleNamespace
import unittest
from unittest.mock import patch
from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow import source_item_delivery_bridge as bridge
from app.application.agent_workflow import source_item_manual_decision_api as api
from app.application.agent_workflow.source_item_manual_decisions import compile_manual_decisions
from app.application.errors import ApplicationError
from .test_source_item_manual_decisions import context


def bundle(sha='a'):
    return {'baseline':SimpleNamespace(artifact_sha256=sha*64,records=[]),
        'document_map':SimpleNamespace(artifact_sha256='b'*64),'image_annotations':None}

def reasoning(sha='c'):
    return {'decomposition':{'artifact_sha256':sha*64},'assessments':None}


class ManualReadConsistencyTests(unittest.TestCase):
    def test_new_baseline_between_two_business_reads_is_rejected(self):
        old,new=bundle(),bundle('d')
        with patch.object(bridge,'_current',side_effect=[({},old),({},new)]), patch.object(bridge,'_source_context',return_value=({},old,'source',{})), patch.object(bridge,'_reasoning',return_value=reasoning()):
            with self.assertRaises(ApplicationError):bridge._read_current_delivery_inputs('p','d')

    def test_same_baseline_decomposition_change_between_reads_is_rejected(self):
        old=bundle()
        with patch.object(bridge,'_current',return_value=({},old)), patch.object(bridge,'_source_context',return_value=({},old,'source',{})), patch.object(bridge,'_reasoning',side_effect=[reasoning(),reasoning('e')]):
            with self.assertRaises(ApplicationError):bridge._read_current_delivery_inputs('p','d')

    def test_stored_manual_version_is_not_replaced_by_an_unpersisted_read_projection(self):
        from contextlib import nullcontext
        saved=compile_manual_decisions(context(),[{'unit_id':'u','decision':'PENDING_EVIDENCE','reason':'Await evidence'}],origin='USER_INPUT',decided_at='2026-09-08T00:00:00Z')
        with patch.object(api.db,'get_conn',return_value=nullcontext(object())), patch.object(api.repository,'manual_storage_available',return_value=True), patch.object(api.repository,'current_manual_decisions_in_transaction',return_value=saved):
            with self.assertRaises(ApplicationError):api.manual_response_view('p','d',context('d','e'))
            view=api.manual_response_view('p','d',context())
            self.assertEqual(view['response_revision'],saved['revision'])
            self.assertEqual(view['response_sha256'],saved['artifact_sha256'])


if __name__=='__main__':unittest.main()
