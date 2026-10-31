"""ReqPilotX consumer semantics for adopted knowledge evidence.

These tests exercise the ReqPilotX side of the fixed Host contract: the frozen
library/version binding, the cumulative consumption registration and the
derived-versus-original distinction. They do not register, inspect or copy a
real knowledge package.
"""
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / 'plugins/reqpilot/scripts/reqpilot.py'
sys.path[:0] = [str(ROOT / 'plugins/reqpilot/scripts'), str(ROOT / 'prototype'),
                str(ROOT / 'packages/engine/src')]
from reqpilotx.application.agent_workflow import fixed_workflow as workflow
from reqpilotx.application.agent_workflow import knowledge_binding
from reqpilotx.application.agent_workflow import pair_evidence
from reqpilotx.application.agent_workflow import source_item_analysis
from test_portable import item_decisions, make_fixture
from task_service import TaskService


KEY = 'a' * 64


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def adopted_binding(version='b' * 64):
    value = {'schema_version': knowledge_binding.SCHEMA, 'mode': 'ADOPTED_KNOWLEDGE',
        'application_id': 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc',
        'project_id': 'p', 'created_at': '2026-10-31T00:00:00Z', 'project': None, 'team': None,
        'adopted': {'library_id': '11111111-1111-4111-8111-111111111111',
            'version_id': version, 'caller': 'reqpilotx-task:app:command:adopted',
            'name': 'Synthetic adopted library', 'package_version': '1.0.0'}}
    value['binding_sha256'] = canonical(value)
    return value


def adopted_fact(*, derived, authority_verified, original_proof=True, content='Synthetic content.'):
    revision = canonical(content)
    fact = {'fact_id': 'pair-adopted-fact-' + canonical({
        'derived': derived, 'content': content})[:32],
        'document_id': 'claim:claim-1' if derived else 'source-1',
        'kb_id': '11111111-1111-4111-8111-111111111111', 'project_id': 'p',
        'source_sha256': revision, 'source_sha256_basis': 'CONTENT_REVISION',
        'role': 'INTERNAL_KNOWLEDGE', 'text': content,
        'text_sha256': hashlib.sha256(content.encode()).hexdigest(),
        'source_ref': {'kind': 'logical-section',
            'location': {'page_number': None, 'section': '2.1'}, 'document_id':
            'claim:claim-1' if derived else 'source-1', 'coverage': 'unknown'},
        'document_name': 'Synthetic adopted record', 'document_version': revision[:12],
        'authority': 'UNKNOWN' if derived else 'INTERNAL_CONTROLLED',
        'claim_role': 'REFERENCE_CONTEXT' if derived else 'PROJECT_RECORD',
        'applicability': 'UNKNOWN', 'eligible': True,
        'authority_verified': authority_verified,
        'capture_mode': 'PAIR_ADOPTED_LIBRARY', 'origin': 'PAIR_ADOPTED_KNOWLEDGE',
        'evidence_origin': 'derived' if derived else 'original',
        'content_revision': revision, 'availability': 'derived' if derived else 'original',
        'original_read_proof': 'SOURCE_BYTES_HASH_MATCH' if not derived and original_proof else
            ('NOT_READ' if derived else 'MISSING'),
        'adopted': {'library_id': '11111111-1111-4111-8111-111111111111',
            'version_id': 'b' * 64, 'package_version': '1.0.0', 'evidence_id':
            'claim:claim-1' if derived else 'source:source-1',
            'evidence_origin': 'derived' if derived else 'original', 'content_revision': revision,
            'availability': 'derived' if derived else 'original'},
        'url': '/reqpilotx/knowledge/tasks/command/adopted/source/source-1'}
    if not derived:
        fact['original_revision'] = revision
    return fact


def adopted_catalog(facts):
    documents = [{'id': fact['document_id'], 'kb_id': fact['kb_id'], 'project_id': 'p',
        'file_name': fact['document_name'], 'source_sha256': fact['source_sha256'],
        'source_sha256_basis': 'CONTENT_REVISION', 'doc_version': fact['document_version'],
        'status': 'ACTIVE', 'authority': fact['authority'], 'claim_role': fact['claim_role'],
        'applicability': fact['applicability'], 'eligible': True,
        'adopted_library_id': fact['adopted']['library_id'],
        'adopted_library_version_id': fact['adopted']['version_id'],
        'content_revision': fact['content_revision'], 'availability': fact['availability']}
        for fact in facts]
    catalog = {'schema_version': 'project-knowledge-facts/v1', 'project_id': 'p',
        'knowledge_base_version': 1, 'evidence_catalog': {fact['fact_id']: fact for fact in facts},
        'documents': documents, 'gaps': [], 'selection': 'EXPLICIT_TASK_EVIDENCE'}
    catalog['catalog_sha256'] = canonical(catalog)
    return catalog


def task_attestation(binding, task_id, revision, command, unit_ids, decisions, catalog,
                     workbench_ref='', document_id=''):
    payload = {'schema': 'reqpilot-pair-analysis-attestation/v1',
        'application_id': binding['application_id'], 'project_id': binding['project_id'],
        'document_id': document_id, 'task_id': task_id, 'workbench_ref': workbench_ref,
        'binding_sha256': binding['binding_sha256'], 'mode': binding['mode'],
        'task_revision': revision, 'command_id': command, 'unit_ids': unit_ids,
        'decisions': decisions, 'decisions_sha256': canonical(decisions),
        'catalog_sha256': catalog['catalog_sha256'],
        'ledger_sha256': canonical({'fixture': 'adopted-ledger'}),
        'consumed_fact_ids': sorted(catalog['evidence_catalog']),
        'checked_at': '2026-10-31T00:00:02Z'}
    return {**payload, 'signature': pair_evidence.sign_pair_attestation(payload, KEY)}


def task_catalog(binding, fact):
    catalog = adopted_catalog([fact])
    registration = {'schema_version': 'reqpilot-pair-evidence-registration/v1',
        'binding_sha256': binding['binding_sha256'], 'mode': 'ADOPTED_KNOWLEDGE',
        'application_id': binding['application_id'], 'project_id': binding['project_id'],
        'catalog_sha256': catalog['catalog_sha256'], 'documents': catalog['documents'],
        'facts': [catalog['evidence_catalog'][key] for key in sorted(catalog['evidence_catalog'])],
        'receipts': [
            {'tool': 'adopted_browse', 'request_sha256': 'a' * 64,
             'returned_at': '2026-10-31T00:00:00Z', 'fact_ids': []},
            {'tool': 'adopted_evidence', 'request_sha256': 'b' * 64,
             'returned_at': '2026-10-31T00:00:01Z', 'fact_ids': [fact['fact_id']]},
        ],
        'authority': 'HOST_SELECTED_EVIDENCE_NOT_HUMAN_ACCEPTANCE',
        'human_acceptance': 'NOT_CLAIMED'}
    registration['registration_sha256'] = canonical(registration)
    return catalog, registration


def adopted_task_fact(binding):
    content = 'SYNTHETIC ADOPTED CLAIM: allowable load is 10 kN at the declared condition.'
    revision = canonical(content)
    return {'fact_id': 'pair-adopted-fact-' + canonical({'task': 'claim'})[:32],
        'document_id': 'claim:claim-1', 'kb_id': binding['adopted']['library_id'], 'project_id': 'p',
        'source_sha256': revision, 'source_sha256_basis': 'CONTENT_REVISION',
        'role': 'INTERNAL_KNOWLEDGE', 'text': content,
        'text_sha256': hashlib.sha256(content.encode()).hexdigest(),
        'returned_content_sha256': hashlib.sha256(content.encode()).hexdigest(),
        'source_ref': {'kind': 'logical-section',
            'location': {'page_number': None, 'section': '2.1'}, 'document_id': 'claim:claim-1',
            'coverage': 'unknown'},
        'document_name': 'Synthetic adopted claim', 'document_version': revision[:12],
        'authority': 'UNKNOWN', 'claim_role': 'REFERENCE_CONTEXT', 'applicability': 'UNKNOWN',
        'eligible': True, 'authority_verified': False,
        'capture_mode': 'PAIR_ADOPTED_LIBRARY', 'origin': 'PAIR_ADOPTED_KNOWLEDGE',
        'evidence_origin': 'derived', 'content_revision': revision, 'source_version': 'e' * 64,
        'original_source_version': 'd' * 64, 'availability': 'original',
        'original_read_proof': 'NOT_READ', 'scope': 'Synthetic scope', 'conditions': 'Synthetic condition',
        'review': {'method': 'manual', 'reviewStatus': 'pending', 'expertApproved': 'false'},
        'expert_approved_declared': False, 'expert_approved_type': 'string',
        'url': '/reqpilotx/knowledge/tasks/command/adopted/records?kind=claim&id=claim-1',
        'linked_source_id': 'source-1',
        'linked_source_url': '/reqpilotx/knowledge/tasks/command/adopted/source/source-1',
        'linked_source_availability': 'original',
        'adopted': {'library_id': binding['adopted']['library_id'],
            'version_id': binding['adopted']['version_id'], 'package_version': '1.0.0',
            'evidence_id': 'claim:claim-1', 'kind': 'claim', 'record_id': 'claim-1',
            'evidence_origin': 'derived', 'content_revision': revision,
            'linked_source_id': 'source-1', 'source_version': 'e' * 64,
            'original_source_version': 'd' * 64, 'availability': 'original',
            'admission': 'full_original'}}


class AdoptedTaskServiceTests(unittest.TestCase):
    def setUp(self):
        os.environ['REQPILOT_HOST_ATTESTATION_KEY'] = KEY

    def tearDown(self):
        os.environ.pop('REQPILOT_HOST_ATTESTATION_KEY', None)

    def prepare(self, root):
        source, _, mapping = make_fixture(root / 'customer')
        binding = knowledge_binding.validate_binding(adopted_binding())
        service = TaskService(root, ENTRY, ROOT)
        fact = adopted_task_fact(binding)
        catalog, registration = task_catalog(binding, fact)
        goal = 'Review the customer requirement with the frozen adopted knowledge version.'
        opened = service.invoke('open', {'mode': 'create', 'source': str(source), 'goal': goal,
            'creation_key': 'adopted-analysis', 'knowledge_evidence': binding,
            'workflow': workflow.workflow_spec(brief_sha256=workflow.brief_sha256(goal),
                evidence_mode='ADOPTED_KNOWLEDGE', evidence_reason='HOST_SELECTED_TASK_EVIDENCE')})
        task = opened['task_id']
        state = service.invoke('prepare', {'task_id': task, 'expected_revision': opened['revision']})
        edit = {key: value for key, value in mapping.items() if key != 'source_sha256'}
        edit['schema_version'] = 'reqpilot-map-edit/v1'
        state = service.invoke('map', {'task_id': task, 'expected_revision': state['revision'], 'decisions': edit})
        self.assertEqual(state['operation_state'], 'COMMITTED', state)
        decisions = item_decisions(Path(state['selected']['MAP']['output_dir']))
        for key in ('schema_version', 'source_sha256', 'document_map_artifact_sha256'):
            decisions.pop(key, None)
        state = service.invoke('items', {'task_id': task, 'expected_revision': state['revision'],
            'decisions': decisions})
        self.assertEqual(state['operation_state'], 'COMMITTED', state)
        knowledge = Path(state['task_root']) / 'inputs' / 'knowledge'
        knowledge.mkdir(parents=True, exist_ok=True)
        (knowledge / 'knowledge-catalog.json').write_text(json.dumps(catalog, ensure_ascii=False))
        (knowledge / 'evidence-registration.json').write_text(json.dumps(registration, ensure_ascii=False))
        return service, state, binding, fact, catalog, registration, knowledge

    def test_task_service_accepts_host_consumed_adopted_fact_and_rejects_tamper_stale_foreign(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, state, binding, fact, catalog, registration, knowledge = self.prepare(root)
            baseline = json.loads(Path(state['selected']['ITEMS']['path']).read_text())
            from reqpilotx.application.agent_workflow import source_requirement_baseline as source_core
            required = source_item_analysis.required_analysis_unit_ids(
                source_core.SourceRequirementBaselineArtifactV1.model_validate(baseline))
            cited = required[0]
            decisions = [{'unit_id': cited, 'decision': 'PENDING_EVIDENCE',
                'reason': 'The adopted claim is derived and does not prove the linked original.',
                'evidence': [{'fact_id': fact['fact_id'], 'quote': fact['text']}],
                'clarification_question': 'Provide the applicable controlled source.'}] + [
                {'unit_id': unit, 'decision': 'PENDING_EVIDENCE',
                 'reason': 'No suitable adopted original evidence was consumed for this unit.',
                 'evidence': [], 'clarification_question': 'Provide current applicable evidence.'}
                for unit in required[1:]]
            revision = state['revision']
            attestation = task_attestation(binding, state['task_id'], revision, 'adopted-analysis',
                required, decisions, catalog)
            saved = service.invoke('analysis', {'task_id': state['task_id'],
                'expected_revision': revision, 'command_id': 'adopted-analysis',
                'unit_ids': required, 'decisions': decisions,
                'knowledge_attestation': attestation})
            self.assertEqual(saved['status'], 'WORKSPACE_OPERATION_SAVED', saved)
            self.assertEqual(saved['operation_state'], 'COMMITTED', saved)
            self.assertTrue(saved['analysis']['complete'], saved)
            self.assertEqual(saved['analysis']['assessed_unit_count'], len(required))

            # A tampered fact no longer matches its Host registration.
            forged = json.loads(json.dumps(catalog))
            forged['evidence_catalog'][fact['fact_id']]['text_sha256'] = '0' * 64
            forged['catalog_sha256'] = canonical({key: value for key, value in forged.items() if key != 'catalog_sha256'})
            registration['catalog_sha256'] = forged['catalog_sha256']
            registration['facts'] = [forged['evidence_catalog'][fact['fact_id']]]
            registration['registration_sha256'] = canonical(
                {key: value for key, value in registration.items() if key != 'registration_sha256'})
            (knowledge / 'knowledge-catalog.json').write_text(json.dumps(forged))
            (knowledge / 'evidence-registration.json').write_text(json.dumps(registration))
            current = service.invoke('inspect', {'task_id': state['task_id']})
            bad = task_attestation(binding, state['task_id'], current['revision'], 'adopted-tamper',
                required, decisions, forged)
            with self.assertRaisesRegex(ValueError, 'integrity|differ|registration|PAIR evidence'):
                service.invoke('analysis', {'task_id': state['task_id'],
                    'expected_revision': current['revision'], 'command_id': 'adopted-tamper',
                    'unit_ids': required, 'decisions': decisions, 'knowledge_attestation': bad})

            # Restore the real pair registration before foreign/stale checks.
            (knowledge / 'knowledge-catalog.json').write_text(json.dumps(catalog))
            (knowledge / 'evidence-registration.json').write_text(json.dumps(
                task_catalog(binding, fact)[1]))
            current = service.invoke('inspect', {'task_id': state['task_id']})
            foreign = task_attestation({**binding, 'mode': 'PROJECT_REFERENCES'},
                state['task_id'], current['revision'], 'adopted-foreign', required, decisions, catalog)
            with self.assertRaisesRegex(ValueError, 'HOST_ATTESTATION_INVALID|mode'):
                service.invoke('analysis', {'task_id': state['task_id'],
                    'expected_revision': current['revision'], 'command_id': 'adopted-foreign',
                    'unit_ids': required, 'decisions': decisions, 'knowledge_attestation': foreign})
            stale = task_attestation(binding, state['task_id'], revision, 'adopted-stale',
                required, decisions, catalog)
            with self.assertRaisesRegex(ValueError, 'REVISION_CONFLICT|current review advanced|changed'):
                service.invoke('analysis', {'task_id': state['task_id'],
                    'expected_revision': revision, 'command_id': 'adopted-stale',
                    'unit_ids': required, 'decisions': decisions, 'knowledge_attestation': stale})

            current = service.invoke('inspect', {'task_id': state['task_id']})
            delivered = service.invoke('deliver', {'task_id': state['task_id'],
                'expected_revision': current['revision'], 'command_id': 'adopted-delivery'})
            self.assertEqual(delivered['operation_state'], 'COMMITTED', delivered)
            delivery_dir = Path(delivered['result']['output_dir'])
            snapshot = json.loads((delivery_dir / 'matrix-snapshot.json').read_text())
            adopted_refs = snapshot['source_item_baseline']['analysis_adopted_evidence']
            self.assertEqual(len(adopted_refs), 1)
            ref = adopted_refs[0]
            self.assertEqual(ref['record_kind'], 'claim')
            self.assertEqual(ref['content_revision'], fact['content_revision'])
            self.assertEqual(ref['source_version'], fact['source_version'])
            self.assertEqual(ref['original_source_version'], fact['original_source_version'])
            self.assertIn('/adopted/records?kind=claim&id=claim-1', ref['record_route'])
            self.assertIn('/adopted/source/source-1', ref['linked_source_route'])
            self.assertIsNone(ref['source_ref']['location']['page_number'])
            self.assertEqual(ref['source_ref']['location']['section'], '2.1')
            self.assertFalse(snapshot['source_item_baseline']['analysis_original_evidence'])
            matching = [node for node in snapshot['nodes']
                if (node.get('source_item') or {}).get('source_item_id') == cited]
            self.assertTrue(matching)
            basis = matching[0]['ai_assessment']['analysis_basis']
            self.assertIn('知识记录', basis)
            self.assertNotIn('登记原件', basis)

            from openpyxl import load_workbook
            workbook = load_workbook(delivery_dir / 'matrix.xlsx', read_only=True, data_only=True)
            self.assertNotIn('内部原件引用', workbook.sheetnames)
            self.assertIn('知识记录引用', workbook.sheetnames)
            sheet = workbook['知识记录引用']
            headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
            self.assertIn('内容修订(contentRevision)', headers)
            self.assertNotIn('原件SHA256', headers)
            self.assertNotIn('登记原件名称', headers)
            rows = list(sheet.iter_rows(min_row=2, values_only=True))
            self.assertTrue(rows)
            self.assertIn(fact['content_revision'], rows[0])
            self.assertIn('2.1', json.dumps(rows[0], ensure_ascii=False))


class AdoptedKnowledgeConsumerTests(unittest.TestCase):
    def test_binding_freezes_library_version_and_caller_and_accepts_adopted_workflow_mode(self):
        binding = knowledge_binding.validate_binding(adopted_binding())
        self.assertEqual(binding['mode'], 'ADOPTED_KNOWLEDGE')
        self.assertEqual(binding['adopted']['version_id'], 'b' * 64)
        self.assertEqual(binding['adopted']['caller'].split(':')[-1], 'adopted')
        self.assertIsNone(binding['project'])
        self.assertIsNone(binding['team'])
        spec = workflow.workflow_spec(brief_sha256='c' * 64,
            evidence_mode='ADOPTED_KNOWLEDGE', evidence_reason='HOST_SELECTED_TASK_EVIDENCE')
        self.assertEqual(spec['evidence']['mode'], 'ADOPTED_KNOWLEDGE')
        with self.assertRaisesRegex(ValueError, 'adopted.version_id'):
            knowledge_binding.validate_binding(adopted_binding(version='short'))
        wrong = adopted_binding()
        wrong['adopted']['version_id'] = 'd' * 64
        with self.assertRaisesRegex(ValueError, 'binding_sha256'):
            knowledge_binding.validate_binding(wrong)
        missing = adopted_binding()
        del missing['adopted']
        missing['binding_sha256'] = canonical({key: value for key, value in missing.items() if key != 'binding_sha256'})
        with self.assertRaisesRegex(ValueError, 'mode does not match'):
            knowledge_binding.validate_binding(missing)

    def test_derived_claim_is_logged_but_not_authority_verified_original_evidence(self):
        derived = adopted_fact(derived=True, authority_verified=False)
        original = adopted_fact(derived=False, authority_verified=False,
            content='Synthetic original bytes read and hashed.')
        catalog = adopted_catalog([derived, original])
        registration = {'schema_version': 'reqpilot-pair-evidence-registration/v1',
            'binding_sha256': adopted_binding()['binding_sha256'], 'mode': 'ADOPTED_KNOWLEDGE',
            'application_id': 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc',
            'project_id': 'p', 'catalog_sha256': catalog['catalog_sha256'],
            'documents': catalog['documents'],
            'facts': [catalog['evidence_catalog'][key] for key in sorted(catalog['evidence_catalog'])],
            'receipts': [
                {'tool': 'adopted_browse', 'request_sha256': 'a' * 64,
                 'returned_at': '2026-10-31T00:00:00Z', 'fact_ids': []},
                {'tool': 'adopted_evidence', 'request_sha256': 'b' * 64,
                 'returned_at': '2026-10-31T00:00:01Z', 'fact_ids': [derived['fact_id']]},
                {'tool': 'adopted_read', 'request_sha256': 'c' * 64,
                 'returned_at': '2026-10-31T00:00:02Z', 'fact_ids': [original['fact_id']]},
            ],
            'authority': 'HOST_SELECTED_EVIDENCE_NOT_HUMAN_ACCEPTANCE',
            'human_acceptance': 'NOT_CLAIMED'}
        registration['registration_sha256'] = canonical(registration)
        validated = pair_evidence.validate_pair_registration(registration, catalog)
        self.assertEqual(validated['catalog_sha256'], catalog['catalog_sha256'])
        self.assertFalse(source_item_analysis._credible_fact(derived, 'f' * 64))
        self.assertFalse(source_item_analysis._credible_fact(original, 'f' * 64))
        self.assertEqual(derived['content_revision'], derived['source_sha256'])
        self.assertNotIn('original_revision', derived)
        self.assertEqual(original['original_read_proof'], 'SOURCE_BYTES_HASH_MATCH')

    def test_adopted_authority_without_consumed_original_bytes_is_rejected(self):
        forged = adopted_fact(derived=False, authority_verified=True, original_proof=False)
        catalog = adopted_catalog([forged])
        registration = {'schema_version': 'reqpilot-pair-evidence-registration/v1',
            'binding_sha256': adopted_binding()['binding_sha256'], 'mode': 'ADOPTED_KNOWLEDGE',
            'application_id': 'reqpilotx-pair:12345678-1234-4234-8234-123456789abc',
            'project_id': 'p', 'catalog_sha256': catalog['catalog_sha256'],
            'documents': catalog['documents'], 'facts': [forged],
            'receipts': [{'tool': 'adopted_read', 'request_sha256': 'c' * 64,
                'returned_at': '2026-10-31T00:00:02Z', 'fact_ids': [forged['fact_id']]}],
            'authority': 'HOST_SELECTED_EVIDENCE_NOT_HUMAN_ACCEPTANCE',
            'human_acceptance': 'NOT_CLAIMED'}
        registration['registration_sha256'] = canonical(registration)
        with self.assertRaisesRegex(ValueError, 'cannot claim authority'):
            pair_evidence.validate_pair_registration(registration, catalog)


class AdoptedMcpTransportTests(unittest.TestCase):
    def test_default_adopted_browse_dispatch_omits_absent_null_options(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('content-length', '0'))
                value = json.loads(self.rfile.read(length).decode('utf-8'))
                received.append((self.path, value))
                payload = json.dumps({'ok': True, 'result': {'section': value.get('section'), 'items': []}}).encode()
                self.send_response(200)
                self.send_header('content-type', 'application/json')
                self.send_header('content-length', str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        saved = {name: sys.modules.get(name) for name in ('mcp', 'mcp.server', 'mcp.types')}
        mcp = types.ModuleType('mcp'); mcp_server = types.ModuleType('mcp.server'); mcp_types = types.ModuleType('mcp.types')
        class MCPServer:
            def __init__(self, *args, **kwargs): pass
            def run(self, *args, **kwargs): pass
        class Tool:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        class ToolAnnotations:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        class CallToolResult:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        class TextContent:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
        mcp_server.MCPServer = MCPServer
        mcp_types.Tool = Tool; mcp_types.ToolAnnotations = ToolAnnotations
        mcp_types.CallToolResult = CallToolResult; mcp_types.TextContent = TextContent
        mcp.server = mcp_server; mcp.types = mcp_types
        sys.modules['mcp'] = mcp; sys.modules['mcp.server'] = mcp_server; sys.modules['mcp.types'] = mcp_types
        try:
            path = ROOT / 'integrations/pair/host/src/knowledge-mcp.py'
            spec = importlib.util.spec_from_file_location('reqpilot_knowledge_mcp_test', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            runtime = {'schema': 'reqpilot-knowledge-runtime/v1',
                'endpoint': 'http://127.0.0.1:' + str(server.server_address[1]), 'token': 'a' * 64}
            adapter = module.create_server(runtime)
            asyncio.run(adapter.call_tool('knowledge_adopted_browse', {'section': 'claims'}))
            self.assertEqual(received[-1][1], {'section': 'claims'})
            self.assertNotIn('query', received[-1][1])
            self.assertNotIn('limit', received[-1][1])
            asyncio.run(adapter.call_tool('knowledge_adopted_read', {'kind': 'claim', 'id': 'claim-1'}))
            self.assertEqual(received[-1][1], {'kind': 'claim', 'id': 'claim-1'})
            asyncio.run(adapter.call_tool('knowledge_adopted_browse', {'section': 'claims', 'query': 'load', 'limit': 3}))
            self.assertEqual(received[-1][1], {'section': 'claims', 'query': 'load', 'limit': 3})
            asyncio.run(adapter.call_tool('knowledge_adopted_evidence', {'query': 'allowable load'}))
            self.assertEqual(received[-1][1], {'query': 'allowable load', 'limit': 8})
        finally:
            for name, value in saved.items():
                if value is None: sys.modules.pop(name, None)
                else: sys.modules[name] = value
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
