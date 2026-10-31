from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from argparse import Namespace
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import hashlib
import json
import os
import sys
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from openpyxl import load_workbook
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'plugins/reqpilot/scripts'),str(ROOT/'plugins/reqpilot/tests')]
from test_portable import make_fixture,item_decisions,write
import artifact_tools
from app.application.agent_workflow import plugin_workbench as api
from app.application.agent_workflow import fixed_workflow as workflow
from app.application.errors import ApplicationError
from task_service import TaskService

class PluginWorkbenchTests(unittest.TestCase):
    def test_export_uses_registered_original_name_not_materialized_digest_filename(self):
        saved=self.sync(self.artifacts,require_map_confirmation=False)
        source=self.root/(self.sha+'.pdf');source.write_bytes(self.source.read_bytes())
        registered='客户接口需求原件.pdf';self.document['file_name']=registered
        snapshot=api.get_plugin_snapshot('p','d',self.session,self.sink())
        with patch.object(api,'resolve_source_record_path',return_value=source):
            result=api.export_plugin_draft('p','d',self.session,saved['revision_id'])
        workbook=load_workbook(BytesIO(result.content),read_only=True,data_only=True)
        try:
            subtitle=workbook['ComplianceMatrix']['A2'].value
            self.assertIn(registered,subtitle)
            self.assertNotIn(self.sha+'.pdf',subtitle)
            metadata={row[0]:row[1] for row in workbook['_ReqPilotXMeta'].iter_rows(values_only=True)}
            self.assertEqual(metadata['SnapshotSHA256'],api.core.canonical_json_sha256(snapshot))
        finally:workbook.close()
        self.assertEqual(result.headers['X-ReqPilot-Revision'],saved['revision_id'])
        self.assertEqual(result.headers['X-ReqPilot-Snapshot'],snapshot['delivery_sha256'])
        self.assertEqual(api.get_plugin_snapshot('p','d',self.session,self.sink()),snapshot)

    def test_lists_same_source_tasks_without_latest_winning_or_losing_empty_tasks(self):
        first = self.sync(self.mapping(), goal='First task')
        first_id = self.session
        self.session = 'plugin-' + 'b'*32
        empty = self.sync(goal='Second independent task')
        result = api.list_plugin_workbench_sessions('p', 'd', self.sink())
        self.assertEqual({s['session_id'] for s in result['sessions']}, {first_id, self.session})
        self.assertEqual({s['availability'] for s in result['sessions']}, {'AVAILABLE', 'NO_ARTIFACTS'})
        self.assertIsNone(result['current_session_id'])
        self.assertEqual(result['selection_policy'], 'EXPLICIT_WORK_SELECTION')
        self.assertEqual(result['unavailable'], [])
        self.assertEqual(api.get_plugin_workbench('p', 'd', first_id, self.sink())['revision_id'], first['revision_id'])
        self.assertIsNone(empty['revision_id'])

    def test_bad_session_is_reported_and_source_change_does_not_choose_another_task(self):
        self.sync(self.mapping())
        directory = api._root('p', 'd', 'plugin-' + 'b'*32)
        directory.mkdir(); (directory/'head.json').write_text('{broken')
        result = api.list_plugin_workbench_sessions('p', 'd', self.sink())
        self.assertEqual(len(result['sessions']), 1)
        self.assertEqual(result['unavailable'][0]['reason_code'], 'SAVED_TASK_UNREADABLE')
        with patch.object(api, '_require_project_document', return_value={**self.document, 'source_sha256': 'c'*64}):
            changed = api.list_plugin_workbench_sessions('p', 'd', self.sink())
        self.assertEqual(changed['sessions'], [])
        self.assertIn('SOURCE_VERSION_CHANGED', {s['reason_code'] for s in changed['unavailable']})

    def test_session_listing_does_not_follow_linked_head_or_version_metadata(self):
        import shutil
        state = self.sync(self.mapping())
        directory = api._root('p', 'd', self.session)
        outside = self.root/'outside-head.json'
        head = directory/'head.json'; head.rename(outside); head.symlink_to(outside)
        original_read = api._read
        reads = []
        def read(path):
            reads.append(Path(path))
            return original_read(path)
        with patch.object(api, '_read', side_effect=read):
            result = api.list_plugin_workbench_sessions('p', 'd', self.sink())
        self.assertEqual(reads, [])
        self.assertEqual(result['unavailable'][0]['reason_code'], 'UNSAFE_TASK_PATH')
        head.unlink(); outside.rename(head)
        version = directory/'versions'/state['revision_id']
        escaped = self.root/'outside-version'; shutil.move(version, escaped); version.symlink_to(escaped, target_is_directory=True)
        reads.clear()
        with patch.object(api, '_read', side_effect=read):
            result = api.list_plugin_workbench_sessions('p', 'd', self.sink())
        self.assertEqual(reads, [head])
        self.assertEqual(result['sessions'], [])
        self.assertEqual(result['unavailable'][0]['reason_code'], 'UNSAFE_TASK_PATH')

    def test_incumbent_reader_preserves_tree_ledger_tables_and_same_version_detail(self):
        from app.application.agent_workflow import plugin_workbench_reader as reader
        first = self.sync(self.mapping())
        map_view = reader.get_plugin_reader('p', 'd', self.session, first['revision_id'], self.sink())
        self.assertIsNone(map_view['outline'])
        self.assertEqual(map_view['reference']['structure_projection']['state'], 'DRAFT')
        self.assertFalse(map_view['reference']['continuation']['can_continue'])
        self.confirm(first)
        state = self.sync(self.artifacts, first['revision_id'])
        view = reader.get_plugin_reader('p', 'd', self.session, state['revision_id'], self.sink())
        mapping = self.artifacts['document-map.json']
        baseline = self.artifacts['source-requirement-baseline.json']
        self.assertEqual(view['reference']['document_tree']['root_ids'], mapping['root_node_ids'])
        self.assertEqual([n['node_id'] for n in view['reference']['document_tree']['nodes']], [n['node_id'] for n in mapping['nodes']])
        self.assertEqual([r['source_item_id'] for r in view['outline']['records']], [r['source_record_id'] for r in baseline['records']])
        self.assertIn('source_number', view['outline']['records'][0])
        self.assertEqual(view['scope_summaries'], view['outline']['scope_summaries'])
        self.assertEqual(view['source_findings']['complete_or_unknown'], 'UNKNOWN')
        public = api.get_plugin_workbench('p', 'd', self.session, self.sink())
        self.assertEqual(public['execution']['status'], 'UNREPORTED')
        self.assertFalse(public['capabilities']['start']['supported'])
        self.assertTrue(public['capabilities']['save_feedback']['enabled'])
        self.assertTrue(public['capabilities']['export_draft']['enabled'])
        for scope, table in self.artifacts['table-previews.json'].items():
            self.assertEqual(view['tables'][scope]['fragments'], table['fragments'])
        for item in baseline['records']:
            detail = reader.get_plugin_record('p', 'd', self.session, item['source_record_id'], state['revision_id'], self.sink())
            self.assertEqual(detail['baseline_artifact_sha256'], baseline['artifact_sha256'])
            self.assertEqual(detail['record']['table_fragments'], item['table_fragments'])
            self.assertEqual([p['text'] for p in detail['record']['text_bindings']], [p['text'] for p in item['text_bindings']])
        self.assertIsNone(reader.get_plugin_reader('p', 'd', self.session, first['revision_id'], self.sink())['outline'])
        self.assertEqual(api.get_plugin_workbench('p', 'd', self.session, self.sink())['revision_id'], state['revision_id'])

    def test_plugin_feedback_reappears_in_incumbent_source_detail(self):
        from app.application.agent_workflow import plugin_workbench_reader as reader
        state = self.sync(self.artifacts, require_map_confirmation=False)
        item = self.artifacts['source-requirement-baseline.json']['records'][0]
        self.feedback(state, node_id=item['node_id'], source_item_ids=[item['source_record_id']])
        detail = reader.get_plugin_record('p', 'd', self.session, item['source_record_id'], state['revision_id'], self.sink())
        self.assertEqual(detail['record']['feedbacks'][0]['message'], '请核对这段内容的上级条件。')
        self.assertEqual(detail['record']['feedbacks'][0]['origin_revision_id'], state['revision_id'])
        self.assertEqual(detail['record']['feedbacks'][0]['human_acceptance'], 'NOT_CLAIMED')

    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name)
        self.source,capture,decisions=make_fixture(self.root/'fixture')
        write(self.root/'map-input.json',decisions)
        mapping=self.root/'map';artifact_tools.compile_map(Namespace(source=self.source,source_ir=capture/'source_ir.json',input=self.root/'map-input.json',output_dir=mapping))
        write(self.root/'items-input.json',item_decisions(mapping))
        baseline=self.root/'baseline';artifact_tools.compile_items(Namespace(map_dir=mapping,input=self.root/'items-input.json',baseline_dir=None,output_dir=baseline))
        self.artifacts={n:json.loads((baseline/n).read_text()) for n in ('document-map.json','table-previews.json','source-requirement-baseline.json','validation-receipt.json')}
        self.sha=hashlib.sha256(self.source.read_bytes()).hexdigest();self.session='plugin-'+'a'*32
        self.document={'source_sha256':self.sha,'file_name':self.source.name};self.api=api
        self.patches=[patch.object(api,'_container',return_value=SimpleNamespace(settings=SimpleNamespace(object_root=self.root/'objects'))),
            patch.object(api,'_require_project_document',return_value=self.document),patch.object(api,'resolve_source_record_path',return_value=self.source)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def sink(self):return SimpleNamespace(headers={})
    def payload(self,artifacts=None,expected=None,**extra):
        extra.setdefault('require_map_confirmation',True) # These legacy checkpoint cases explicitly request it.
        return api.PluginDraftUpload(session_id=self.session,expected_source_sha256=self.sha,expected_revision_id=expected,artifacts=artifacts or {},**extra)
    def mapping(self):return {k:v for k,v in self.artifacts.items() if k in {'document-map.json','table-previews.json'}}
    def sync(self,artifacts=None,expected=None,**extra):return api.sync_plugin_draft('p','d',self.payload(artifacts,expected,**extra),self.sink())
    def confirm(self,state):return api.confirm_plugin_map('p','d',self.session,api.PluginMapConfirmation(command_id='confirm',expected_source_sha256=self.sha,expected_map_sha256=state['stages'][0]['artifact_sha256'],expected_revision_id=state['revision_id']),self.sink())
    def changed_map(self,summary):
        data=deepcopy(self.mapping());m=data['document-map.json'];m['document_summary']=summary
        m['artifact_sha256']=api.core.canonical_json_sha256({k:v for k,v in m.items() if k not in {'artifact_id','artifact_sha256'}});m['artifact_id']='document-map-'+m['artifact_sha256'][:32];return data
    def feedback(self,state,**overrides):
        fields={'command_id':'feedback','expected_source_sha256':self.sha,'expected_revision_id':state['revision_id'],'source_item_ids':[],'text':'请核对这段内容的上级条件。'};fields.update(overrides)
        return api.post_plugin_feedback('p','d',self.session,api.PluginFeedback(**fields),self.sink())
    def workflow_state(self, artifacts, *, source=None, map_sha=None, items_sha=None):
        spec = workflow.workflow_spec(brief_sha256='f'*64, evidence_mode='NONE')
        mapping = artifacts.get('document-map.json')
        baseline = artifacts.get('source-requirement-baseline.json')
        stages = [
            {'id': 'PREPARATION', 'label': '资料准备', 'status': 'SAVED',
             'evidence': [{'kind': 'TASK_CONTEXT', 'source_sha256': source or self.sha}]},
            {'id': 'SOURCE_MAP', 'label': '来源与结构', 'status': 'SAVED' if mapping else 'PENDING'},
            {'id': 'ITEMS_CLASSIFICATION', 'label': '条目、分类与明确派生', 'status': 'SAVED' if baseline else 'PENDING'},
            {'id': 'EVIDENCE_ANALYSIS', 'label': '依据分析', 'status': 'SKIPPED', 'reason': 'NO_INTERNAL_EVIDENCE'},
            {'id': 'SELF_CHECK', 'label': '自查与修正', 'status': 'PENDING'},
            {'id': 'DELIVERABLE', 'label': '可核对成果', 'status': 'PENDING'},
        ]
        if mapping:
            stages[1]['evidence'] = [{'kind': 'DOCUMENT_MAP', 'source_sha256': source or self.sha,
                                      'artifact_sha256': map_sha or mapping['artifact_sha256']}]
        if baseline:
            stages[2]['evidence'] = [{'kind': 'SOURCE_ITEMS', 'source_sha256': source or self.sha,
                                      'artifact_sha256': items_sha or baseline['artifact_sha256'],
                                      'map_sha256': mapping['artifact_sha256']}]
        return {'schema': workflow.STATE_SCHEMA,
                'workflow': {**spec, 'schema': workflow.WORKFLOW_SCHEMA, 'evidence_mode': 'NONE'},
                'source_sha256': source or self.sha, 'stages': stages, 'next_stage': 'SELF_CHECK',
                'ready_to_review': False, 'verification': 'SAVED_ARTIFACTS_ONLY',
                'open_items': [], 'human_acceptance': 'NOT_CLAIMED'}

    def real_task_workflow(self, partial=False):
        task_root = self.root / ('task-full' if not partial else 'task-partial')
        task_root.mkdir(parents=True, exist_ok=True)
        service = TaskService(task_root, ROOT/'plugins/reqpilot/scripts/reqpilot.py', ROOT)
        goal = 'Compile and deliver the synthetic source.'
        spec = workflow.workflow_spec(brief_sha256=workflow.brief_sha256(goal), evidence_mode='NONE')
        state = service.invoke('open', {'mode':'create','source':str(self.source),'goal':goal,
            'creation_key':'cross-full' if not partial else 'cross-partial','workflow':spec})
        task = state['task_id']; state = service.invoke('prepare', {'task_id':task,'expected_revision':state['revision']})
        edit = json.loads((self.root/'map-input.json').read_text()); edit['schema_version']='reqpilot-map-edit/v1'; edit.pop('source_sha256',None)
        mapped = service.invoke('map', {'task_id':task,'expected_revision':state['revision'],'decisions':edit})
        choices = item_decisions(Path(mapped['selected']['MAP']['output_dir']))
        for key in ('source_sha256','document_map_artifact_sha256'): choices.pop(key,None)
        if partial:
            body = [{k:v for k,v in scope.items() if k != 'structure_only_spans'}
                    for scope in choices['text_decisions'] if scope['items']]
            decisions = {'retain_map_structure':True,'text_decisions':body[:1]}
        else:
            decisions = choices
        items = service.invoke('items', {'task_id':task,'expected_revision':mapped['revision'],'decisions':decisions})
        review = service.invoke('review', {'task_id':task,'expected_revision':items['revision']})
        deliver = service.invoke('deliver', {'task_id':task,'expected_revision':review['revision']})
        check = service.invoke('check', {'task_id':task})
        inspected = service.invoke('inspect', {'task_id':task}); selected = inspected['selected']
        map_dir = Path(selected['MAP']['output_dir']); item_dir = Path(selected['ITEMS']['path']).parent
        artifacts = {name:json.loads((map_dir/name).read_text()) for name in ('document-map.json','table-previews.json')}
        for name in ('source-requirement-baseline.json','validation-receipt.json','item-compilation-receipt.json'):
            if (item_dir/name).is_file(): artifacts[name]=json.loads((item_dir/name).read_text())
        check_path = Path(inspected['task_root'])/'workflow-check.json'
        artifacts['workflow-check.json'] = json.loads(check_path.read_text())
        return check, artifacts

    def workbench_roundtrip(self, session, artifacts, workflow_state, local_review_sha256):
        state = api.sync_plugin_draft('p','d',api.PluginDraftUpload(session_id=session,
            expected_source_sha256=self.sha, artifacts=artifacts, fixed_workflow=workflow_state,
            local_review_sha256=local_review_sha256), self.sink())
        from reqpilotx_server.application.agent_workflow import plugin_workbench_delivery as delivery
        preview = delivery.preview_plugin_delivery_version('p','d',session,self.sink())
        saved = delivery.save_plugin_delivery_version('p','d',session,delivery.PluginDeliveryVersion(
            command_id='cross-delivery-'+session[-8:], expected_source_sha256=self.sha,
            expected_revision_id=preview['revision_id'], expected_version=preview['version'],
            expected_response_revision=preview['response_revision'],
            expected_delivery_sha256=preview['delivery_sha256']), self.sink())
        return state, saved


    def test_saved_pair_assessments_surface_in_workbench_reader_and_snapshot(self):
        from app.application.agent_workflow import source_item_analysis as analysis
        from app.application.agent_workflow import pair_evidence
        baseline = self.artifacts['source-requirement-baseline.json']
        unit = next(record for record in baseline['records'] if record['requirementness'] == 'REQUIREMENT')
        text = 'SYNTHETIC INTERNAL EVIDENCE: measured response was 28 ms at 12 V and 23 C.'
        fact_id = 'pair-fact-' + '1'*32
        fact = {'fact_id': fact_id, 'document_id': 'internal-1', 'kb_id': 'kb-project',
            'project_id': 'p', 'source_sha256': 'f'*64, 'role': 'PROJECT_EVIDENCE', 'text': text,
            'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'source_ref': {'kind': 'logical-section', 'location': {'page_number': None, 'section': 1}},
            'document_name': 'internal.txt', 'document_version': 'v1', 'authority': 'INTERNAL_CONTROLLED',
            'claim_role': 'PROJECT_RECORD', 'applicability': 'APPLICABLE', 'eligible': True,
            'authority_verified': True, 'capture_mode': 'PAIR_SELECTED_EVIDENCE',
            'origin': 'PAIR_SELECTED_SOURCE', 'original_revision': 'f'*64,
            'extraction_revision': 'e'*64, 'coverage': 'complete', 'location': 'logical-section',
            'url': '/reqpilotx/knowledge/tasks/x/documents/internal-1'}
        document = {'id': 'internal-1', 'kb_id': 'kb-project', 'project_id': 'p', 'file_name': 'internal.txt',
            'source_sha256': 'f'*64, 'doc_version': 'v1', 'status': 'ACTIVE', 'authority': 'INTERNAL_CONTROLLED',
            'claim_role': 'PROJECT_RECORD', 'applicability': 'APPLICABLE', 'eligible': True}
        catalog = {'schema_version': 'project-knowledge-facts/v1', 'project_id': 'p', 'knowledge_base_version': 1,
            'evidence_catalog': {fact_id: fact}, 'documents': [document], 'gaps': [], 'selection': 'EXPLICIT_TASK_EVIDENCE'}
        catalog['catalog_sha256'] = api.core.canonical_json_sha256(catalog)
        registration = {'schema_version': pair_evidence.PAIR_EVIDENCE_SCHEMA, 'binding_sha256': 'a'*64,
            'mode': 'PROJECT_REFERENCES', 'application_id': 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc',
            'project_id': 'p', 'catalog_sha256': catalog['catalog_sha256'], 'documents': [document], 'facts': [fact],
            'receipts': [{'tool': 'search', 'request_sha256': 'b'*64, 'returned_at': '2026-10-31T00:00:00Z',
                'fact_ids': [fact_id]}], 'authority': 'HOST_SELECTED_EVIDENCE_NOT_HUMAN_ACCEPTANCE',
            'human_acceptance': 'NOT_CLAIMED'}
        registration['registration_sha256'] = api.core.canonical_json_sha256(registration)
        packet = analysis.build_source_item_analysis_packet(
            api.core.DocumentMapArtifactV1.model_validate(self.artifacts['document-map.json']),
            api.core.SourceRequirementBaselineArtifactV1.model_validate(baseline),
            self.artifacts['table-previews.json'], catalog, selected_unit_ids=[unit['source_record_id']])
        decisions = [{'unit_id': unit['source_record_id'], 'decision': 'COMPLIANT',
            'reason': 'Applicability: 12 V / 23 C. The selected internal result matches the source condition.',
            'evidence': [{'fact_id': fact_id, 'quote': text}], 'clarification_question': ''}]
        assessment = analysis.compile_source_item_assessments(packet, {'decisions': decisions})
        key = 'c'*64
        payload = {'schema': 'reqpilot-pair-analysis-attestation/v1',
            'application_id': 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc',
            'project_id': 'p', 'document_id': 'd', 'task_id': 'task-x',
            'workbench_ref': self.session, 'binding_sha256': 'a'*64,
            'mode': 'PROJECT_REFERENCES', 'task_revision': 'revision-x',
            'command_id': 'command-x', 'unit_ids': [unit['source_record_id']],
            'decisions': decisions, 'decisions_sha256': api.core.canonical_json_sha256(decisions),
            'catalog_sha256': catalog['catalog_sha256'],
            'ledger_sha256': api.core.canonical_json_sha256({'fixture': 'ledger'}),
            'consumed_fact_ids': [fact_id], 'checked_at': '2026-10-31T00:00:02Z'}
        attestation = {**payload, 'signature': pair_evidence.sign_pair_attestation(payload, key)}
        artifacts = {**self.artifacts, 'source-item-assessments.json': assessment,
            'analysis-packet.json': packet, 'knowledge-catalog.json': catalog,
            'evidence-registration.json': registration, 'knowledge-attestation.json': attestation}
        old_key = os.environ.get('REQPILOT_HOST_ATTESTATION_KEY')
        os.environ['REQPILOT_HOST_ATTESTATION_KEY'] = key
        try:
            state = self.sync(artifacts, require_map_confirmation=False)
        finally:
            if old_key is None: os.environ.pop('REQPILOT_HOST_ATTESTATION_KEY', None)
            else: os.environ['REQPILOT_HOST_ATTESTATION_KEY'] = old_key
        from app.application.agent_workflow import plugin_workbench_reader as reader
        view = reader.get_plugin_reader('p', 'd', self.session, state['revision_id'], self.sink())
        self.assertEqual(view['reasoning']['assessed_unit_count'], 1)
        self.assertEqual(view['reasoning']['assessments']['records'][unit['source_record_id']]['decision'], 'COMPLIANT')
        directory = api._root('p', 'd', self.session) / 'versions' / state['revision_id']
        snapshot = json.loads((directory / 'matrix-snapshot.json').read_text())
        row = next(node for node in snapshot['nodes']
            if (node.get('source_item') or {}).get('source_item_id') == unit['source_record_id'])
        self.assertEqual(row['ai_assessment']['decision'], 'COMPLIANT')
        self.assertIn(fact_id, row['ai_assessment']['analysis_basis'])
        forged = {key: value for key, value in artifacts.items() if key != 'knowledge-attestation.json'}
        self.session = 'plugin-' + 'f'*32
        with self.assertRaisesRegex(ApplicationError, 'Host attestation'):
            self.sync(forged, require_map_confirmation=False)

    def test_fixed_workflow_analysis_coverage_requires_every_required_unit(self):
        from app.application.agent_workflow import source_item_analysis as analysis
        mapping = api.core.DocumentMapArtifactV1.model_validate(self.artifacts['document-map.json'])
        baseline = api.core.SourceRequirementBaselineArtifactV1.model_validate(
            self.artifacts['source-requirement-baseline.json'])
        required = analysis.required_analysis_unit_ids(baseline)
        self.assertGreater(len(required), 1)
        decision = {'decision': 'PENDING_EVIDENCE', 'reason': 'Evidence pending.',
            'evidence': [], 'clarification_question': 'Provide current product evidence.'}
        spec = workflow.workflow_spec(brief_sha256='f'*64, evidence_mode='PROJECT_REFERENCES')
        payload = {'schema': workflow.STATE_SCHEMA, 'source_sha256': self.sha,
            'workflow': {**spec, 'schema': workflow.WORKFLOW_SCHEMA, 'evidence_mode': 'PROJECT_REFERENCES'}}
        artifacts = {**self.artifacts,
            'source-item-assessments.json': {'baseline_sha256': baseline.artifact_sha256,
                'source_sha256': self.sha, 'decomposition_sha256': '',
                'catalog_sha256': 'a'*64, 'records': {required[0]: decision}},
            'knowledge-attestation.json': {'schema': 'reqpilot-pair-analysis-attestation/v1',
                'catalog_sha256': 'a'*64}}
        state = api._derive_fixed_workflow(payload, self.sha, mapping, baseline, artifacts,
            self.root, {}, None)
        stage = {item['id']: item for item in state['stages']}['EVIDENCE_ANALYSIS']
        self.assertEqual(stage['status'], 'PENDING')
        self.assertEqual(stage['reason'], 'INTERNAL_EVIDENCE_ANALYSIS_PARTIAL')
        self.assertFalse(state['ready_to_review'])
        artifacts['source-item-assessments.json'] = {**artifacts['source-item-assessments.json'],
            'records': {unit_id: decision for unit_id in required}}
        state = api._derive_fixed_workflow(payload, self.sha, mapping, baseline, artifacts,
            self.root, {}, None)
        stage = {item['id']: item for item in state['stages']}['EVIDENCE_ANALYSIS']
        self.assertEqual(stage['status'], 'SAVED')

    def test_public_cross_consumer_full_partial_check_and_delivery(self):
        full_check, full_artifacts = self.real_task_workflow()
        self.assertTrue(full_check['ready_to_review'], full_check)
        self.assertIsNotNone(full_check['workflow_check']['delivery_snapshot_sha256'])
        full_session='plugin-'+'c'*32
        full_state, saved = self.workbench_roundtrip(full_session, full_artifacts, full_check['workflow'],
            full_check['workflow_check']['basis']['review_sha256'])
        self.assertEqual(saved['delivery_sha256'], full_check['workflow_check']['delivery_snapshot_sha256'])
        public = api.get_plugin_workbench('p','d',full_session,self.sink())['fixed_workflow']
        self.assertTrue(public['ready_to_review'], public)
        self.assertEqual({s['id']:s for s in public['stages']}['SELF_CHECK']['status'], 'SAVED')
        self.assertTrue(api.get_plugin_workbench('p','d',full_session,self.sink())['fixed_workflow']['ready_to_review'])

        partial_check, partial_artifacts = self.real_task_workflow(partial=True)
        self.assertFalse(partial_check['ready_to_review'], partial_check)
        partial_session='plugin-'+'d'*32
        self.workbench_roundtrip(partial_session, partial_artifacts, partial_check['workflow'],
            partial_check['workflow_check']['basis']['review_sha256'])
        partial_public = api.get_plugin_workbench('p','d',partial_session,self.sink())['fixed_workflow']
        self.assertFalse(partial_public['ready_to_review'])
        self.assertEqual({s['id']:s for s in partial_public['stages']}['ITEMS_CLASSIFICATION']['status'], 'IN_PROGRESS')

        missing = dict(full_artifacts); missing.pop('workflow-check.json')
        missing_session='plugin-'+'e'*32
        self.workbench_roundtrip(missing_session, missing, full_check['workflow'],
            full_check['workflow_check']['basis']['review_sha256'])
        self.assertFalse(api.get_plugin_workbench('p','d',missing_session,self.sink())['fixed_workflow']['ready_to_review'])

        failed = dict(full_artifacts); failed['workflow-check.json'] = {**full_artifacts['workflow-check.json'], 'status':'FAILED'}
        failed_session='plugin-'+'f'*32
        self.workbench_roundtrip(failed_session, failed, full_check['workflow'],
            full_check['workflow_check']['basis']['review_sha256'])
        self.assertFalse(api.get_plugin_workbench('p','d',failed_session,self.sink())['fixed_workflow']['ready_to_review'])

        stale = dict(full_artifacts); stale['workflow-check.json'] = {**full_artifacts['workflow-check.json'],
            'basis':{**full_artifacts['workflow-check.json']['basis'], 'items_sha256':'0'*64}}
        stale_session='plugin-'+'1'*32
        self.workbench_roundtrip(stale_session, stale, full_check['workflow'],
            full_check['workflow_check']['basis']['review_sha256'])
        self.assertFalse(api.get_plugin_workbench('p','d',stale_session,self.sink())['fixed_workflow']['ready_to_review'])

    def test_fixed_workflow_claims_are_derived_from_uploaded_artifacts(self):
        claim = self.workflow_state(self.artifacts)
        claim['ready_to_review'] = True
        claim['stages'] = [{**stage, 'status': 'SAVED', 'evidence': []} for stage in claim['stages']]
        empty = self.sync(fixed_workflow=claim, require_map_confirmation=False)
        self.assertFalse(empty['fixed_workflow']['ready_to_review'])
        self.assertEqual([stage['status'] for stage in empty['fixed_workflow']['stages']],
                         ['SAVED', 'PENDING', 'BLOCKED', 'BLOCKED', 'BLOCKED', 'BLOCKED'])
        self.assertEqual(empty['fixed_workflow']['source_sha256'], self.sha)
        mapped = self.sync(self.mapping(), fixed_workflow=claim, require_map_confirmation=False)
        self.assertFalse(mapped['fixed_workflow']['ready_to_review'])
        self.assertEqual([stage['status'] for stage in mapped['fixed_workflow']['stages']],
                         ['SAVED', 'SAVED', 'PENDING', 'BLOCKED', 'BLOCKED', 'BLOCKED'])
        self.assertEqual(mapped['fixed_workflow']['stages'][1]['evidence'][0]['artifact_sha256'],
                         self.mapping()['document-map.json']['artifact_sha256'])
        items = self.sync(self.artifacts, mapped['revision_id'], fixed_workflow=claim, require_map_confirmation=False)
        self.assertFalse(items['fixed_workflow']['ready_to_review'])
        by_id = {stage['id']: stage for stage in items['fixed_workflow']['stages']}
        self.assertEqual(by_id['ITEMS_CLASSIFICATION']['status'], 'SAVED')
        self.assertEqual(by_id['EVIDENCE_ANALYSIS']['status'], 'SKIPPED')
        self.assertNotEqual(by_id['EVIDENCE_ANALYSIS']['status'], 'SAVED')
        self.assertEqual(by_id['SELF_CHECK']['status'], 'PENDING')
        self.assertEqual(by_id['DELIVERABLE']['status'], 'PENDING')

    def test_fixed_workflow_derivation_ignores_sender_stage_status(self):
        claim = self.workflow_state({})
        claim['ready_to_review'] = True
        claim['stages'] = [{**stage, 'status': 'SAVED', 'evidence': []} for stage in claim['stages']]
        head = {'source_sha256': self.sha, 'revision_id': None, 'delivery_versions': [],
                'require_map_confirmation': False}
        derived = api._derive_fixed_workflow(claim, self.sha, None, None, {}, self.root, head)
        self.assertFalse(derived['ready_to_review'])
        self.assertEqual([stage['status'] for stage in derived['stages']],
                         ['SAVED', 'PENDING', 'BLOCKED', 'BLOCKED', 'BLOCKED', 'BLOCKED'])
        self.assertTrue(all(stage.get('evidence', []) != [] or stage['status'] != 'SAVED'
                            for stage in derived['stages']))

    def test_fixed_workflow_partial_item_coverage_keeps_items_incomplete(self):
        claim = self.workflow_state(self.artifacts)
        artifacts = dict(self.artifacts)
        artifacts['item-compilation-receipt.json'] = {'source_sha256': self.sha,
            'baseline_artifact_sha256': artifacts['source-requirement-baseline.json']['artifact_sha256'],
            'unprocessed_scope_ids': ['scope-unprocessed-a', 'scope-unprocessed-b']}
        state = self.sync(artifacts, fixed_workflow=claim, require_map_confirmation=False)
        by_id = {stage['id']: stage for stage in state['fixed_workflow']['stages']}
        self.assertEqual(by_id['ITEMS_CLASSIFICATION']['status'], 'IN_PROGRESS')
        self.assertEqual(by_id['ITEMS_CLASSIFICATION']['reason'], 'UNPROCESSED_SOURCE_SCOPE')
        self.assertFalse(state['fixed_workflow']['ready_to_review'])
        self.assertIn('UNPROCESSED_SOURCE_SCOPE', state['fixed_workflow']['open_items'])

    def test_fixed_workflow_rejects_wrong_source_or_unknown_definition(self):
        claim = self.workflow_state(self.mapping())
        with self.assertRaises(ApplicationError) as wrong_source:
            self.sync(self.mapping(), fixed_workflow=self.workflow_state(self.mapping(), source='0'*64),
                      require_map_confirmation=False)
        self.assertEqual(wrong_source.exception.status_code, 409)
        with self.assertRaises(ApplicationError) as incompatible:
            self.sync(self.mapping(), fixed_workflow={**claim, 'schema': 'reqpilot-fixed-workflow-state/v999'},
                      require_map_confirmation=False)
        self.assertEqual(incompatible.exception.status_code, 422)

    def test_fixed_workflow_delivery_without_check_is_not_ready_and_goes_stale(self):
        claim = self.workflow_state(self.artifacts)
        state = self.sync(self.artifacts, fixed_workflow=claim, require_map_confirmation=False)
        self.assertFalse(state['fixed_workflow']['ready_to_review'])
        from reqpilotx_server.application.agent_workflow import plugin_workbench_delivery as delivery
        view = delivery.preview_plugin_delivery_version('p','d',self.session,self.sink())
        payload = delivery.PluginDeliveryVersion(command_id='fixed-delivery', expected_source_sha256=self.sha,
            expected_revision_id=view['revision_id'], expected_version=view['version'],
            expected_response_revision=view['response_revision'], expected_delivery_sha256=view['delivery_sha256'])
        delivery.save_plugin_delivery_version('p','d',self.session,payload,self.sink())
        completed = self.api.get_plugin_workbench('p','d',self.session,self.sink())['fixed_workflow']
        self.assertFalse(completed['ready_to_review'])
        by_id = {stage['id']: stage for stage in completed['stages']}
        self.assertNotEqual(by_id['SELF_CHECK']['status'], 'SAVED')
        self.assertEqual(by_id['DELIVERABLE']['status'], 'SAVED')
        self.assertEqual(by_id['DELIVERABLE']['evidence'][0]['kind'], 'DELIVERY_VERSION')
        changed = self.sync(self.changed_map('Changed basis'), state['revision_id'], fixed_workflow=claim,
                            require_map_confirmation=False)
        stale = changed['fixed_workflow']
        self.assertFalse(stale['ready_to_review'])
        by_id = {stage['id']: stage for stage in stale['stages']}
        self.assertIn(by_id['SELF_CHECK']['status'], {'PENDING', 'BLOCKED'})
        self.assertIn(by_id['DELIVERABLE']['status'], {'PENDING', 'BLOCKED'})
        self.assertNotEqual(by_id['SELF_CHECK']['status'], 'SAVED')
        self.assertNotEqual(by_id['DELIVERABLE']['status'], 'SAVED')

    def test_fixed_workflow_corrupt_delivery_receipt_is_unconfirmed_not_ready(self):
        claim = self.workflow_state(self.artifacts)
        state = self.sync(self.artifacts, fixed_workflow=claim, require_map_confirmation=False)
        from reqpilotx_server.application.agent_workflow import plugin_workbench_delivery as delivery
        view = delivery.preview_plugin_delivery_version('p','d',self.session,self.sink())
        delivery.save_plugin_delivery_version('p','d',self.session,delivery.PluginDeliveryVersion(
            command_id='fixed-delivery-corrupt', expected_source_sha256=self.sha,
            expected_revision_id=view['revision_id'], expected_version=view['version'],
            expected_response_revision=view['response_revision'], expected_delivery_sha256=view['delivery_sha256']), self.sink())
        root = self.api._root('p','d',self.session)
        (root/'deliveries'/'1.json').write_text('{broken')
        public = self.api.get_plugin_workbench('p','d',self.session,self.sink())['fixed_workflow']
        self.assertFalse(public['ready_to_review'])
        self.assertEqual(public['verification'], 'UNCONFIRMED')
        self.assertEqual(public['stages'], [])
        self.assertIn('unconfirmed', public)

    def test_map_confirmation_then_items_uses_real_shared_snapshot_and_excel(self):
        initial=self.sync();self.assertEqual(initial['stages'],[]);self.assertEqual(initial['model_dispatch'],'NOT_PERFORMED')
        mapping=self.sync(self.mapping());self.assertTrue(mapping['map_confirmation_required'])
        with self.assertRaises(ApplicationError):self.sync(self.artifacts,mapping['revision_id'])
        self.confirm(mapping);items=self.sync(self.artifacts,mapping['revision_id'])
        self.assertEqual([s['stage'] for s in items['stages']],['MAP','ITEMS'])
        latest=api.get_latest_plugin_workbench('p','d',self.sink())
        self.assertEqual(latest['revision_id'],items['revision_id']);self.assertEqual(latest['session_id'],self.session)
        snapshot=api.get_plugin_snapshot('p','d',self.session,self.sink());exported=api.export_plugin_draft('p','d',self.session,items['revision_id'])
        workbook=load_workbook(BytesIO(exported.content));metadata={r[0]:r[1] for r in workbook['_ReqPilotXMeta'].values}
        self.assertEqual(metadata['SnapshotSHA256'],api.core.canonical_json_sha256(snapshot))
        self.assertEqual(snapshot['source_item_baseline']['artifact_sha256'],self.artifacts['source-requirement-baseline.json']['artifact_sha256'])
        self.assertEqual(api.get_plugin_stage('p','d',self.session,'MAP',self.sink(),revision=mapping['revision_id'])['artifact_sha256'],mapping['stages'][0]['artifact_sha256'])

    def test_new_default_is_autonomous_and_existing_checkpoint_policy_is_inherited_when_omitted(self):
        automatic=api.PluginDraftUpload(session_id=self.session,expected_source_sha256=self.sha,artifacts=self.artifacts)
        self.assertFalse(automatic.require_map_confirmation)
        self.assertNotIn('require_map_confirmation',automatic.model_fields_set)
        saved=api.sync_plugin_draft('p','d',automatic,self.sink())
        self.assertFalse(saved['map_confirmation_required'])
        self.assertEqual([stage['stage'] for stage in saved['stages']],['MAP','ITEMS'])
        self.assertTrue(api.export_plugin_draft('p','d',self.session,saved['revision_id']).content)
        with self.assertRaises(ApplicationError):
            api.sync_plugin_draft('p','d',api.PluginDraftUpload(session_id=self.session,expected_source_sha256=self.sha,require_map_confirmation=True),self.sink())

        legacy='plugin-'+'b'*32
        first=api.sync_plugin_draft('p','d',api.PluginDraftUpload(session_id=legacy,expected_source_sha256=self.sha,
            require_map_confirmation=True,artifacts=self.mapping()),self.sink())
        omitted=api.PluginDraftUpload(session_id=legacy,expected_source_sha256=self.sha)
        self.assertTrue(api.sync_plugin_draft('p','d',omitted,self.sink())['map_confirmation_required'])
        with self.assertRaises(ApplicationError):
            api.sync_plugin_draft('p','d',api.PluginDraftUpload(session_id=legacy,expected_source_sha256=self.sha,
                expected_revision_id=first['revision_id'],artifacts=self.artifacts),self.sink())
        with self.assertRaises(ApplicationError):
            api.sync_plugin_draft('p','d',api.PluginDraftUpload(session_id=legacy,expected_source_sha256=self.sha,require_map_confirmation=False),self.sink())
        api.confirm_plugin_map('p','d',legacy,api.PluginMapConfirmation(command_id='explicit-confirm',expected_source_sha256=self.sha,
            expected_revision_id=first['revision_id'],expected_map_sha256=first['stages'][0]['artifact_sha256']),self.sink())
        after=api.sync_plugin_draft('p','d',api.PluginDraftUpload(session_id=legacy,expected_source_sha256=self.sha,
            expected_revision_id=first['revision_id'],artifacts=self.artifacts),self.sink())
        self.assertEqual([stage['stage'] for stage in after['stages']],['MAP','ITEMS'])
        self.assertTrue(json.loads((api._root('p','d',legacy)/'head.json').read_text())['require_map_confirmation'])
    def test_wrong_source_tampered_artifacts_and_unsupported_state_are_rejected(self):
        with self.assertRaises(ApplicationError):api.sync_plugin_draft('p','d',self.payload().model_copy(update={'expected_source_sha256':'0'*64}),self.sink())
        bad=self.mapping();bad['document-map.json']=deepcopy(bad['document-map.json']);bad['document-map.json']['document_summary']='tampered'
        with self.assertRaises(ApplicationError):self.sync(bad)
        with self.assertRaises(ApplicationError):self.sync({**self.artifacts,'manual-decisions.json':{'records':{}}})
        self.assertFalse((api._root('p','d',self.session)/'head.json').exists())
    def test_feedback_is_version_bound_idempotent_and_not_lost_on_new_revision(self):
        first=self.sync(self.mapping());saved=self.feedback(first);self.assertEqual(len(self.feedback(first)['feedback']),1)
        with self.assertRaises(ApplicationError):self.feedback(first,text='different command retry')
        second=self.sync(self.changed_map('Synthetic second draft'),first['revision_id']);self.assertEqual(second['feedback'],saved['feedback'])
        with self.assertRaises(ApplicationError):self.feedback(first,command_id='stale-feedback')
        with self.assertRaises(ApplicationError):self.confirm(first)
        with self.assertRaises(ApplicationError):self.feedback(second,command_id='bad-scope',node_id='does-not-exist')
    def test_exact_retry_does_not_grow_history_and_return_to_old_content_is_new_revision(self):
        original=self.mapping();first=self.sync(original);self.assertEqual(self.sync(original,first['revision_id'])['revision_id'],first['revision_id'])
        old_path=api._root('p','d',self.session)/'versions'/first['revision_id']/'version.json';before=old_path.read_bytes()
        second=self.sync(self.changed_map('Changed note'),first['revision_id']);third=self.sync(original,second['revision_id'])
        self.assertNotEqual(first['revision_id'],third['revision_id']);self.assertEqual(third['revision_number'],3);self.assertEqual(before,old_path.read_bytes())
    def test_two_writers_cannot_overwrite_each_other(self):
        first=self.sync(self.mapping())
        def writer(text):
            try:return self.sync(self.changed_map(text),first['revision_id'])
            except ApplicationError as exc:return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(writer,['First new note','Second new note']))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1);self.assertIn(409,results)
    def test_file_change_and_registered_source_change_are_detected(self):
        saved=self.sync(self.mapping());api.get_plugin_snapshot('p','d',self.session,self.sink())
        path=api._root('p','d',self.session)/'versions'/saved['revision_id']/'matrix-snapshot.json';path.write_text('{}')
        with self.assertRaises(ApplicationError):api.get_plugin_snapshot('p','d',self.session,self.sink())
        self.document['source_sha256']='0'*64
        with self.assertRaises(ApplicationError):api.get_plugin_workbench('p','d',self.session,self.sink())
        self.assertIsNone(api.get_latest_plugin_workbench('p','d',self.sink()))
    def test_reported_feedback_resolution_is_bound_to_a_new_draft_not_human_acceptance(self):
        first=self.sync(self.mapping());self.confirm(first);items=self.sync(self.artifacts,first['revision_id'])
        saved=self.feedback(items);feedback_id=saved['feedback'][0]['feedback_id']
        result=self.sync(self.artifacts,items['revision_id'],feedback_resolutions={feedback_id:'已按原文核对并修订工作草稿。'})
        resolution=result['feedback_resolutions'][feedback_id]
        self.assertEqual(resolution['revision_id'],result['revision_id'])
        self.assertEqual(resolution['human_acceptance'],'NOT_CLAIMED')
        self.assertEqual(result['feedback'],saved['feedback'])
        with self.assertRaises(ApplicationError):self.sync(self.artifacts,result['revision_id'],feedback_resolutions={'unknown':'假反馈'})
        # An exact confirmation retry remains acknowledged even after item publication.
        self.assertEqual(self.confirm(first)['revision_id'],result['revision_id'])
