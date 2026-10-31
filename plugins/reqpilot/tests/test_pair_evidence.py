"""Task-bound PAIR evidence catalogs, typed analysis and workflow refusals."""
import hashlib
import json
import os
import re
from argparse import Namespace
from pathlib import Path
import sys
from unittest.mock import patch
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / 'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0] = [str(ENTRY.parent), str(ROOT / 'prototype'), str(ROOT / 'packages/engine/src')]
from test_portable import item_decisions, make_fixture
import workbench_tools as workbench
from reqpilotx.application.agent_workflow import fixed_workflow as workflow
from reqpilotx.application.agent_workflow import knowledge_binding as binding_module
from reqpilotx.application.agent_workflow import pair_evidence
from reqpilotx.application.agent_workflow import source_item_analysis
from reqpilotx.application.agent_workflow import source_requirement_baseline as source_core
from task_service import TaskService

KEY = 'a' * 64


TEXT = 'SYNTHETIC INTERNAL EVIDENCE: the measured standby current was 4 mA at 12 V and 25 C.'


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def attestation(binding, task_id, revision, command, unit_ids, decisions, catalog, workbench_ref='', document_id=''):
    payload = {'schema': 'reqpilot-pair-analysis-attestation/v1',
        'application_id': binding['application_id'], 'project_id': binding['project_id'],
        'document_id': document_id, 'task_id': task_id, 'workbench_ref': workbench_ref,
        'binding_sha256': binding['binding_sha256'], 'mode': binding['mode'],
        'task_revision': revision, 'command_id': command, 'unit_ids': unit_ids,
        'decisions': decisions, 'decisions_sha256': canonical(decisions),
        'catalog_sha256': catalog['catalog_sha256'],
        'ledger_sha256': canonical({'fixture': 'host-ledger'}),
        'consumed_fact_ids': sorted(catalog['evidence_catalog']),
        'checked_at': '2026-10-31T00:00:02Z'}
    return {**payload, 'signature': pair_evidence.sign_pair_attestation(payload, KEY)}


def task_binding(source_sha, *, mode='PROJECT_REFERENCES'):
    document = {'kbId': 'kb-project', 'documentId': 'internal-1', 'name': 'internal.txt',
        'mime': 'text/plain', 'originalRevision': 'f' * 64,
        'extractionRevision': 'e' * 64, 'coverage': 'complete',
        'location': 'logical-section', 'pages': 1, 'expectedPages': 1,
        'wiki': 'not-requested'}
    value = {'schema_version': binding_module.SCHEMA, 'mode': mode,
        'application_id': 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc',
        'project_id': 'p', 'created_at': '2026-10-31T00:00:00Z', 'project': None, 'team': None}
    if mode in {'PROJECT_REFERENCES', 'COMBINED'}:
        value['project'] = {'library_id': 'kb-project', 'caller': 'reqpilotx-task:project',
            'documents': [document], 'roles': {'internal-1': {
                'authority': 'INTERNAL_CONTROLLED', 'claimRole': 'PROJECT_RECORD',
                'applicability': 'APPLICABLE', 'label': 'Synthetic internal test result'}}}
    if mode in {'TEAM_KNOWLEDGE', 'COMBINED'}:
        value['team'] = {'kind': 'LIVE', 'library_id': 'kb-team', 'caller': 'reqpilotx-task:team',
            'documents': [{**document, 'kbId': 'kb-team', 'documentId': 'team-1'}],
            'roles': {'team-1': {'authority': 'INTERNAL_CONTROLLED', 'claimRole': 'PROJECT_RECORD',
                'applicability': 'APPLICABLE', 'label': 'Synthetic team evidence'}}}
    value['binding_sha256'] = canonical(value)
    return value


def pair_catalog(binding_sha):
    document = {'id': 'internal-1', 'kb_id': 'kb-project', 'project_id': 'p',
        'file_name': 'internal.txt', 'source_sha256': 'f' * 64, 'doc_version': 'e' * 12,
        'status': 'ACTIVE', 'authority': 'INTERNAL_CONTROLLED',
        'claim_role': 'PROJECT_RECORD', 'applicability': 'APPLICABLE', 'eligible': True}
    fact = {'fact_id': 'pair-fact-' + '1' * 32, 'document_id': 'internal-1',
        'kb_id': 'kb-project', 'project_id': 'p', 'source_sha256': 'f' * 64,
        'role': 'PROJECT_EVIDENCE', 'text': TEXT,
        'text_sha256': hashlib.sha256(TEXT.encode()).hexdigest(),
        'source_ref': {'kind': 'logical-section', 'location': {'page_number': None, 'section': 1},
            'document_id': 'internal-1', 'coverage': 'complete'},
        'document_name': 'internal.txt', 'document_version': 'e' * 12,
        'authority': 'INTERNAL_CONTROLLED', 'claim_role': 'PROJECT_RECORD',
        'applicability': 'APPLICABLE', 'eligible': True, 'authority_verified': True,
        'capture_mode': 'PAIR_SELECTED_EVIDENCE', 'origin': 'PAIR_SELECTED_SOURCE',
        'original_revision': 'f' * 64, 'extraction_revision': 'e' * 64,
        'coverage': 'complete', 'location': 'logical-section',
        'url': '/reqpilotx/knowledge/tasks/command/documents/internal-1'}
    catalog = {'schema_version': 'project-knowledge-facts/v1', 'project_id': 'p',
        'knowledge_base_version': 1, 'evidence_catalog': {fact['fact_id']: fact},
        'documents': [document], 'gaps': [], 'selection': 'EXPLICIT_TASK_EVIDENCE'}
    catalog['catalog_sha256'] = canonical(catalog)
    registration = {'schema_version': 'reqpilot-pair-evidence-registration/v1',
        'binding_sha256': binding_sha, 'mode': 'PROJECT_REFERENCES',
        'application_id': 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc',
        'project_id': 'p', 'catalog_sha256': catalog['catalog_sha256'],
        'documents': [document], 'facts': [fact],
        'receipts': [{'tool': 'search', 'request_sha256': 'a' * 64,
            'returned_at': '2026-10-31T00:00:01Z', 'fact_ids': [fact['fact_id']]}],
        'authority': 'HOST_SELECTED_EVIDENCE_NOT_HUMAN_ACCEPTANCE',
        'human_acceptance': 'NOT_CLAIMED'}
    registration['registration_sha256'] = canonical(registration)
    return catalog, registration


class PairEvidenceTaskTests(unittest.TestCase):
    def setUp(self):
        os.environ['REQPILOT_HOST_ATTESTATION_KEY'] = KEY

    def tearDown(self):
        os.environ.pop('REQPILOT_HOST_ATTESTATION_KEY', None)

    def analyze(self, service, state, catalog, decisions, *, command, unit_ids=None, revision=None,
                workbench_ref='', document_id=''):
        binding = json.loads((Path(state['task_root']) / 'task-context.json').read_text())['request']['knowledge_evidence']
        revision = revision or state['revision']
        signed = attestation(binding, state['task_id'], revision, command, unit_ids, decisions, catalog,
            workbench_ref, document_id)
        return service.invoke('analysis', {'task_id': state['task_id'],
            'expected_revision': revision, 'command_id': command,
            'unit_ids': unit_ids, 'decisions': decisions, 'knowledge_attestation': signed})

    def requirements(self, baseline):
        return source_item_analysis.required_analysis_unit_ids(
            source_core.SourceRequirementBaselineArtifactV1.model_validate(baseline))

    def test_retained_topic_binding_uses_exact_provider_fields_and_source_refs(self):
        value = task_binding('f'*64, mode='TEAM_KNOWLEDGE')
        source = {'documentId': '11111111-1111-4111-8111-111111111111', 'title': 'design.md',
            'originalRevision': 'd'*64, 'extractionRevision': 'e'*64, 'coverage': 'complete',
            'location': {'kind': 'physical-page', 'number': 2}}
        value['team'] = {'kind': 'TOPIC', 'library_id': 'kb-team', 'caller': 'task:team',
            'roles': {'topic-v1': {'authority': 'INTERNAL_CONTROLLED',
                'claimRole': 'PROJECT_RECORD', 'applicability': 'APPLICABLE', 'label': 'topic'}},
            'topic': {'version_id': 'topic-v1', 'topic_id': 'topic-id', 'title': 'Retained topic',
                'availability': {'state': 'available', 'acceptedBy': 'curator'},
                'verification': {'state': 'verified', 'confirmedBy': 'reviewer'},
                'origin': 'human-authored',
                'applicability': {'state': 'known', 'description': 'Applicable to the controller.'},
                'authors': ['engineer'], 'topic_revision': 'a'*64,
                'sources_digest': canonical([source]), 'sources': [source]}}
        value['binding_sha256'] = canonical({key: item for key, item in value.items() if key != 'binding_sha256'})
        binding = binding_module.validate_binding(value)
        self.assertEqual(binding['team']['topic']['sources'][0]['documentId'], source['documentId'])
        self.assertEqual(binding['team']['topic']['verification']['state'], 'verified')
        self.assertNotIn('source_sha256', binding['team']['topic']['sources'][0])

    def prepare(self, root):
        source, _, mapping = make_fixture(root / 'customer')
        binding = task_binding(hashlib.sha256(source.read_bytes()).hexdigest())
        service = TaskService(root, ENTRY, ROOT)
        goal = 'Review the customer requirement against the selected internal evidence.'
        opened = service.invoke('open', {'mode': 'create', 'source': str(source), 'goal': goal,
            'creation_key': 'pair-analysis', 'knowledge_evidence': binding,
            'workflow': workflow.workflow_spec(brief_sha256=workflow.brief_sha256(goal),
                evidence_mode='PROJECT_REFERENCES', evidence_reason='HOST_SELECTED_TASK_EVIDENCE')})
        task = opened['task_id']
        state = service.invoke('prepare', {'task_id': task, 'expected_revision': opened['revision']})
        edit = {key: value for key, value in mapping.items() if key != 'source_sha256'}
        edit['schema_version'] = 'reqpilot-map-edit/v1'
        state = service.invoke('map', {'task_id': task, 'expected_revision': state['revision'],
            'decisions': edit})
        self.assertEqual(state['operation_state'], 'COMMITTED', state)
        decisions = item_decisions(Path(state['selected']['MAP']['output_dir']))
        for key in ('schema_version', 'source_sha256', 'document_map_artifact_sha256'):
            decisions.pop(key, None)
        state = service.invoke('items', {'task_id': task, 'expected_revision': state['revision'],
            'decisions': decisions})
        self.assertEqual(state['operation_state'], 'COMMITTED', state)
        knowledge = Path(state['task_root']) / 'inputs' / 'knowledge'
        knowledge.mkdir(parents=True, exist_ok=True)
        catalog, registration = pair_catalog(binding['binding_sha256'])
        (knowledge / 'knowledge-catalog.json').write_text(json.dumps(catalog, ensure_ascii=False))
        (knowledge / 'evidence-registration.json').write_text(json.dumps(registration, ensure_ascii=False))
        return service, state, catalog, registration

    def test_consumed_pair_facts_save_partial_then_full_analysis_and_same_version_excel(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, state, catalog, _ = self.prepare(root)
            baseline = json.loads(Path(state['selected']['ITEMS']['path']).read_text())
            required = self.requirements(baseline)
            unit = required[0]
            fact = next(iter(catalog['evidence_catalog'].values()))
            compliant = {'unit_id': unit, 'decision': 'COMPLIANT',
                'reason': 'Applicability: 12 V / 25 C. The selected internal test result matches the source condition and criterion.',
                'evidence': [{'fact_id': fact['fact_id'], 'quote': TEXT}],
                'clarification_question': ''}
            partial = self.analyze(service, state, catalog, [compliant], command='analysis-partial', unit_ids=[unit])
            self.assertEqual(partial['analysis']['assessed_unit_count'], 1, partial)
            self.assertFalse(partial['analysis']['complete'], partial)
            self.assertEqual(set(partial['analysis']['remaining_unit_ids']), set(required[1:]))
            by_id = {item['id']: item for item in partial['workflow']['stages']}
            self.assertEqual(by_id['EVIDENCE_ANALYSIS']['status'], 'PENDING', partial['workflow'])
            self.assertEqual(by_id['EVIDENCE_ANALYSIS']['reason'], 'INTERNAL_EVIDENCE_ANALYSIS_PARTIAL')
            self.assertFalse(partial['workflow']['ready_to_review'])
            replay = self.analyze(service, state, catalog, [compliant], command='analysis-partial',
                unit_ids=[unit], revision=state['revision'])
            self.assertEqual(replay['status'], 'ANALYSIS_ALREADY_SAVED', replay)
            decisions = [compliant] + [{'unit_id': item, 'decision': 'PENDING_EVIDENCE',
                'reason': 'No suitable internal evidence has been read for this unit.',
                'evidence': [], 'clarification_question': 'Provide the current product evidence for this requirement.'}
                for item in required[1:]]
            current = service.invoke('inspect', {'task_id': state['task_id']})
            complete = self.analyze(service, current, catalog, decisions, command='analysis-complete')
            self.assertTrue(complete['analysis']['complete'], complete)
            self.assertEqual(complete['analysis']['remaining_unit_ids'], [])
            by_id = {item['id']: item for item in complete['workflow']['stages']}
            self.assertEqual(by_id['EVIDENCE_ANALYSIS']['status'], 'SAVED', complete['workflow'])
            review_root = Path(complete['task_root']) / 'review'
            head = json.loads((review_root / 'workspace-manifest.json').read_text())
            review_values = json.loads((review_root / head['current']['version_path']
                / 'source-item-assessments.json').read_text())
            self.assertEqual(review_values['authority'], 'AGENT_WORKING_PROPOSAL')
            self.assertEqual(review_values['records'][unit]['decision'], 'COMPLIANT')
            self.assertEqual(set(review_values['records']), set(required))
            current = service.invoke('inspect', {'task_id': state['task_id']})
            partial_signed = attestation(json.loads(
                (Path(state['task_root']) / 'task-context.json').read_text())['request']['knowledge_evidence'],
                state['task_id'], current['revision'], 'incomplete-cumulative', [unit], [compliant], catalog)
            with self.assertRaisesRegex(ValueError, 'cumulative assessment records'):
                service.invoke('analysis', {'task_id': state['task_id'],
                    'expected_revision': current['revision'], 'command_id': 'incomplete-cumulative',
                    'unit_ids': [unit], 'decisions': [compliant], 'knowledge_attestation': partial_signed})
            delivered = service.invoke('deliver', {'task_id': state['task_id'],
                'expected_revision': current['revision'], 'command_id': 'pair-analysis-delivery'})
            self.assertEqual(delivered['operation_state'], 'COMMITTED', delivered)
            delivery_dir = Path(delivered['result']['output_dir'])
            snapshot = json.loads((delivery_dir / 'matrix-snapshot.json').read_text())
            matching = [node for node in snapshot['nodes']
                if (node.get('source_item') or {}).get('source_item_id') == unit]
            self.assertTrue(matching)
            assessment = matching[0]['ai_assessment']
            self.assertEqual(assessment['decision'], 'COMPLIANT')
            self.assertIn('Applicability: 12 V / 25 C.', assessment['reason'])
            self.assertIn(fact['fact_id'], assessment['analysis_basis'])
            self.assertTrue((delivery_dir / 'matrix.xlsx').is_file())

    def test_existing_review_gains_full_analysis_without_replacing_source(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, state, catalog, _ = self.prepare(root)
            review = service.invoke('review', {'task_id': state['task_id'],
                'expected_revision': state['revision']})
            self.assertEqual(review['operation_state'], 'COMMITTED', review)
            self.assertEqual({item['id']: item for item in review['workflow']['stages']}['EVIDENCE_ANALYSIS']['status'], 'PENDING')
            baseline = json.loads(Path(review['selected']['ITEMS']['path']).read_text())
            required = self.requirements(baseline)
            fact = next(iter(catalog['evidence_catalog'].values()))
            decisions = [{'unit_id': required[0], 'decision': 'COMPLIANT',
                'reason': 'The selected internal evidence covers the declared operating condition.',
                'evidence': [{'fact_id': fact['fact_id'], 'quote': TEXT}],
                'clarification_question': ''}] + [
                {'unit_id': item, 'decision': 'PENDING_EVIDENCE',
                 'reason': 'No suitable internal evidence has been read for this unit.',
                 'evidence': [], 'clarification_question': 'Provide the current product evidence for this requirement.'}
                for item in required[1:]]
            result = self.analyze(service, review, catalog, decisions, command='existing-review-analysis')
            self.assertEqual({item['id']: item for item in result['workflow']['stages']}['EVIDENCE_ANALYSIS']['status'], 'SAVED', result)
            self.assertEqual(result['operation_state'], 'COMMITTED', result)
            current = service.invoke('inspect', {'task_id': state['task_id']})
            self.assertEqual(result['selected']['ITEMS']['artifact_sha256'], baseline['artifact_sha256'])
            self.assertEqual(result['selected']['MAP']['artifact_sha256'], review['selected']['MAP']['artifact_sha256'])
            self.assertRegex(result['new_review_sha256'], r'^[0-9a-f]{64}$')
            replay = self.analyze(service, current, catalog, decisions, command='existing-review-analysis')
            self.assertEqual(replay['status'], 'ANALYSIS_ALREADY_SAVED', replay)

    def test_forged_catalog_without_host_attestation_and_unknown_authority_are_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, state, catalog, registration = self.prepare(root)
            baseline = json.loads(Path(state['selected']['ITEMS']['path']).read_text())
            unit = self.requirements(baseline)[0]
            fact = next(iter(catalog['evidence_catalog'].values()))
            decisions = [{'unit_id': unit, 'decision': 'COMPLIANT', 'reason': 'Unsupported.',
                'evidence': [{'fact_id': fact['fact_id'], 'quote': TEXT}], 'clarification_question': ''}]
            # Public-path attack: a model can rewrite catalog/registration but
            # cannot obtain the Host key, so no valid attestation is produced.
            os.environ.pop('REQPILOT_HOST_ATTESTATION_KEY', None)
            with self.assertRaisesRegex(ValueError, 'HOST_EVIDENCE_ATTESTATION_REQUIRED'):
                service.invoke('analysis', {'task_id': state['task_id'],
                    'expected_revision': state['revision'], 'command_id': 'forged-no-provider',
                    'unit_ids': [unit], 'decisions': decisions})
            os.environ['REQPILOT_HOST_ATTESTATION_KEY'] = KEY
            # A copied/valid attestation cannot authorize a different catalog.
            signed = attestation(json.loads((Path(state['task_root']) / 'task-context.json').read_text())['request']['knowledge_evidence'],
                state['task_id'], state['revision'], 'forged-catalog', [unit], decisions, catalog)
            catalog['evidence_catalog'][fact['fact_id']]['text'] = 'FORGED TEXT'
            catalog['evidence_catalog'][fact['fact_id']]['text_sha256'] = hashlib.sha256(b'FORGED TEXT').hexdigest()
            catalog['catalog_sha256'] = canonical({key: value for key, value in catalog.items() if key != 'catalog_sha256'})
            knowledge = Path(state['task_root']) / 'inputs' / 'knowledge'
            registration['catalog_sha256'] = catalog['catalog_sha256']
            registration['facts'] = [catalog['evidence_catalog'][fact['fact_id']]]
            registration['registration_sha256'] = canonical({key: value for key, value in registration.items() if key != 'registration_sha256'})
            (knowledge / 'knowledge-catalog.json').write_text(json.dumps(catalog))
            (knowledge / 'evidence-registration.json').write_text(json.dumps(registration))
            with self.assertRaisesRegex(ValueError, 'HOST_ATTESTATION_INVALID'):
                service.invoke('analysis', {'task_id': state['task_id'],
                    'expected_revision': state['revision'], 'command_id': 'forged-catalog',
                    'unit_ids': [unit], 'decisions': decisions, 'knowledge_attestation': signed})
            catalog, registration = pair_catalog(state['knowledge_evidence']['binding_sha256']
                if state.get('knowledge_evidence') else
                json.loads((Path(state['task_root']) / 'task-context.json').read_text())['request']['knowledge_evidence']['binding_sha256'])
            fact = next(iter(catalog['evidence_catalog'].values()))
            fact['authority'] = 'UNKNOWN'; fact['authority_verified'] = False
            catalog['catalog_sha256'] = canonical({key: value for key, value in catalog.items() if key != 'catalog_sha256'})
            registration['facts'] = [fact]; registration['catalog_sha256'] = catalog['catalog_sha256']
            registration['registration_sha256'] = canonical({key: value for key, value in registration.items() if key != 'registration_sha256'})
            (knowledge / 'knowledge-catalog.json').write_text(json.dumps(catalog))
            (knowledge / 'evidence-registration.json').write_text(json.dumps(registration))
            signed = attestation(json.loads((Path(state['task_root']) / 'task-context.json').read_text())['request']['knowledge_evidence'],
                state['task_id'], state['revision'], 'unknown-authority', [unit], decisions, catalog)
            with self.assertRaisesRegex(ValueError, 'authority-verified'):
                service.invoke('analysis', {'task_id': state['task_id'],
                    'expected_revision': state['revision'], 'command_id': 'unknown-authority',
                    'unit_ids': [unit], 'decisions': decisions, 'knowledge_attestation': signed})

    def test_stale_observed_revision_is_rejected_not_retargeted(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, state, catalog, _ = self.prepare(root)
            baseline = json.loads(Path(state['selected']['ITEMS']['path']).read_text())
            unit = self.requirements(baseline)[0]
            fact = next(iter(catalog['evidence_catalog'].values()))
            decisions = [{'unit_id': unit, 'decision': 'COMPLIANT',
                'reason': 'The selected internal evidence covers the declared condition.',
                'evidence': [{'fact_id': fact['fact_id'], 'quote': TEXT}],
                'clarification_question': ''}]
            stale_revision = state['revision']
            first = self.analyze(service, state, catalog, decisions, command='stale-first', unit_ids=[unit])
            self.assertEqual(first['operation_state'], 'COMMITTED', first)
            signed = attestation(json.loads((Path(state['task_root']) / 'task-context.json').read_text())['request']['knowledge_evidence'],
                state['task_id'], stale_revision, 'stale-old', [unit], decisions, catalog)
            with self.assertRaisesRegex(ValueError, 'REVISION_CONFLICT|current review advanced|selected task state changed'):
                service.invoke('analysis', {'task_id': state['task_id'],
                    'expected_revision': stale_revision, 'command_id': 'stale-old',
                    'unit_ids': [unit], 'decisions': decisions, 'knowledge_attestation': signed})

    def test_actual_workbench_binding_positive_and_wrong_ref_rejects(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, state, catalog, _ = self.prepare(root)
            baseline = json.loads(Path(state['selected']['ITEMS']['path']).read_text())
            unit = self.requirements(baseline)[0]
            fact = next(iter(catalog['evidence_catalog'].values()))
            decisions = [{'unit_id': unit, 'decision': 'COMPLIANT',
                'reason': 'The selected internal evidence covers the declared condition.',
                'evidence': [{'fact_id': fact['fact_id'], 'quote': TEXT}],
                'clarification_question': ''}]
            def fake_request(url, payload=None):
                session = url.rstrip('/').split('/')[-1]
                session = session if re.fullmatch(r'plugin-[a-f0-9]{32}', session) else payload['session_id']
                return {'session_id': session, 'source_sha256': state['source']['sha256'],
                    'project_id': 'p', 'document_id': 'd', 'revision_id': None,
                    'revision_number': 0, 'stages': [], 'feedback': [],
                    'map_confirmation_required': False, 'confirmed_map_sha256': None}
            with patch.object(workbench, 'request', side_effect=fake_request):
                synced = workbench.sync_workbench(Namespace(
                    workspace=Path(state['task_root']),
                    url='http://127.0.0.1:18080/workspace?projectId=p&documentId=d',
                    source=None, map_dir=None,
                    baseline_dir=Path(state['selected']['ITEMS']['output_dir']), review_dir=None,
                    binding_file=None, require_map_confirmation=False, no_map_confirmation=None,
                    open=False, goal='Synthetic client test', project_id=None, document_id=None))
            session_id = synced['session_id']
            from reqpilotx.application.agent_workflow.task_workspace import bind_task_workspace
            task_id = state['task_id']
            state = {**bind_task_workspace(state['task_root'], Path(synced['binding_file']),
                state['source']['sha256'], project_id='p', document_id='d'), 'task_id': task_id}
            with patch.object(workbench, 'sync_after_operation', side_effect=lambda args, result: result):
                partial = self.analyze(service, state, catalog, decisions, command='bound-analysis',
                    unit_ids=[unit], workbench_ref=session_id, document_id='d')
            self.assertEqual(partial['operation_state'], 'COMMITTED', partial)
            with self.assertRaisesRegex(ValueError, 'workbench_ref mismatch'):
                self.analyze(service, state, catalog, decisions, command='wrong-workbench',
                    unit_ids=[unit], workbench_ref='plugin-' + 'b'*32, document_id='d')

    def test_unavailable_binding_is_visible_and_does_not_retarget(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, state, _, _ = self.prepare(root)
            binding = json.loads((Path(state['task_root']) / 'task-context.json').read_text())['request']['knowledge_evidence']
            status = service.invoke('evidence_status', {'task_id': state['task_id'],
                'binding_sha256': binding['binding_sha256'], 'state': 'UNAVAILABLE',
                'message': 'The selected internal source is currently unavailable.'})
            by_id = {item['id']: item for item in status['workflow']['stages']}
            self.assertEqual(by_id['EVIDENCE_ANALYSIS']['status'], 'PENDING')
            self.assertEqual(by_id['EVIDENCE_ANALYSIS']['reason'], 'EVIDENCE_BINDING_UNAVAILABLE')
            self.assertIn('EVIDENCE_BINDING_UNAVAILABLE', status['workflow']['open_items'])
            with self.assertRaisesRegex(ValueError, 'binding'):
                service.invoke('evidence_status', {'task_id': state['task_id'],
                    'binding_sha256': '0' * 64, 'state': 'CURRENT', 'message': ''})


if __name__ == '__main__':
    unittest.main()
