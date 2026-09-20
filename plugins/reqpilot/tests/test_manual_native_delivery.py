"""Native graph adapter + stored user opinions + real PDF program export.

The native job envelope is synthetic. Database/job discovery are I/O doubles;
Map validation, raw graph selection, source tables, opinions and XLSX are real.
"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from io import BytesIO
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
import test_manual_review as manual_fixture
loop = manual_fixture.loop
from app import db
from app.application.agent_workflow import native_source_graph as native
from app.application.agent_workflow import native_structure_bridge as structure
from app.application.agent_workflow import source_requirement_jobs as source_jobs
from app.application.agent_workflow import source_item_agent_jobs as agent_jobs
from app.application.agent_workflow import source_item_delivery_bridge as delivery
from app.application.agent_workflow import source_item_manual_decision_api as api
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot
from app.application.errors import ApplicationError


class ManualNativeDeliveryTests(unittest.TestCase):
    setUp = manual_fixture.ManualReviewTests.setUp
    tearDown = manual_fixture.ManualReviewTests.tearDown
    prepare = manual_fixture.ManualReviewTests.prepare
    put = manual_fixture.ManualReviewTests.put

    def test_raw_native_graph_uses_repository_opinion_in_read_and_program_export(self):
        self.prepare()
        dm, base, receipt, tables = loop.bundle(self.current_dir)
        state = loop.state(self.current_dir)
        snapshot = build_delivery_snapshot(dm, base, tables,
            decomposition=state['decomposition'], assessments=state['assessments'])
        graph = native.seal_native_source_graph(source_sha256=base.source_sha256,
            document_map=dm.model_dump(mode='json'), baseline=base.model_dump(mode='json'),
            validation_receipt=receipt.model_dump(mode='json'), table_previews=tables,
            native_reference=json.loads((self.map_dir/'native-structure-reference.json').read_text()),
            capture_receipt=json.loads((self.root/'capture/manifest.json').read_text()),
            map_compilation_receipt=json.loads((self.map_dir/'map-compilation-receipt.json').read_text()),
            item_compilation_receipt=json.loads((self.baseline_dir/'item-compilation-receipt.json').read_text()),
            decomposition=state['decomposition'], assessments=state['assessments'], analysis_packet=state['analysis_packet'],
            matrix_snapshot_sha256=loop.core.canonical_json_sha256(snapshot),
            delivery_receipt={'source_sha256':base.source_sha256,'baseline_artifact_sha256':base.artifact_sha256,
                'source_document_bound_for_visual_export':True},
            producer_receipts=[{'operation':op,'success':True} for op in ('capture','compile-map','compile-items','deliver')],
            runtime_receipt={'adapter_id':'reqpilotx-codex-native-loop','status':'COMPLETED',
                'terminal_snapshot_verified':True,'thread_retained':True,'is_human_accepted':False,
                'thread_id':'SYNTHETIC-IO-TEST','turn_id':'SYNTHETIC-IO-TEST',
                'tools':[{'name':'reqpilot_publish','success':True}]})
        before = deepcopy(graph)
        document={'id':'d','project_id':'p','source_sha256':base.source_sha256,'file_name':'synthetic.pdf'}
        public=native.native_source_graph_reference(graph)
        raw_bundle=native.native_source_graph_bundle(graph,document=document)
        reasoning={'decomposition':state['decomposition'],'assessments':state['assessments'],
            'decomposition_job_id':None,'analysis_job_id':None,'notices':[],'active_jobs':[]}
        @contextmanager
        def connection():
            conn=sqlite3.connect(self.root/'manual.sqlite');conn.row_factory=sqlite3.Row
            try:yield conn;conn.commit()
            except Exception:conn.rollback();raise
            finally:conn.close()
        with connection() as conn:
            conn.execute('CREATE TABLE sor_documents(id TEXT,project_id TEXT,source_sha256 TEXT)')
            conn.execute('INSERT INTO sor_documents VALUES(?,?,?)',('d','p',base.source_sha256))
        with ExitStack() as stack:
            for obj,name,value in (
                (db,'get_conn',connection),(db,'get_project',lambda p:{'name':'Synthetic'}),
                (delivery,'_require_project_document',lambda p,d:document),
                (delivery,'_reasoning',lambda p,d,b:reasoning),
                (native,'load_native_source_graph',lambda *a,**k:graph),
                (structure,'get_native_structure_reference',lambda *a,**k:public),
                (source_jobs,'resolve_effective_source_requirement_bundle',lambda **k:k['base_bundle']),
                (agent_jobs,'_current_reference_context',lambda *a:{'source_sha256':base.source_sha256,
                    'document':document,'bundle':raw_bundle,'handoff_attempts':[]}),
                (agent_jobs,'resolve_source_record_path',lambda d:self.source)):
                stack.enter_context(patch.object(obj,name,value))
            _, loaded=delivery._current('p','d')
            self.assertEqual(loaded['manifest']['authority'],'NATIVE_AGENT_SOURCE_GRAPH')
            initial=api.get_engineer_responses('p','d',SimpleNamespace(headers={}))
            request=api.EngineerResponseRequest(command_id='native-synthetic-user',
                expected_source_sha256=base.source_sha256,expected_baseline_sha256=base.artifact_sha256,
                expected_response_revision=0,decisions=[{'unit_id':self.unit,
                    'expected_unit_context_sha256':initial['units'][self.unit]['unit_context_sha256'],
                    'decision':'NON_COMPLIANT','reason':'合成工程师原生分支意见','conditions':'仅限测试接口'}])
            saved=api.post_engineer_responses('p','d',request,SimpleNamespace(headers={}))
            read=delivery.get_source_item_delivery_state('p','d',SimpleNamespace(headers={}))
            self.assertEqual(read['engineer_responses']['response_sha256'],saved['response_sha256'])
            self.assertEqual(read['decision_counts']['NON_COMPLIANT'],1)
            artifact=delivery.export_source_item_delivery('p','d',base.artifact_sha256,
                expected_response_revision=1,expected_business_basis_sha256=read['business_basis_sha256'])
            workbook=load_workbook(BytesIO(artifact.content))
            self.assertTrue(any('工程师工作意见：不符合' in str(row[9].value)
                and '原AI建议' in str(row[10].value) for row in workbook['ComplianceMatrix'].iter_rows()))
            self.assertGreater(len(workbook.sheetnames),1)
            with self.assertRaises(ApplicationError):
                delivery.export_source_item_delivery('p','d',base.artifact_sha256,expected_response_revision=0)
        self.assertEqual(graph,before)


if __name__=='__main__':unittest.main()
