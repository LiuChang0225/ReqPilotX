"""SDK-only mocks through the actual provider executors and durable joint worker.

This proves the synthetic host contract, not live model/source semantic quality.
"""
from __future__ import annotations
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from collections import Counter
from dataclasses import replace
from io import BytesIO
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import build_synthetic_pdf
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.agent_workflow import source_requirement_jobs as jobs
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot, export_delivery_xlsx
from app.ports.job_queue import NonRetryableJobError
from reqpilotx_server.adapters.agents.provider_window_executors import (
    ProviderSourceRequirementWindowExecutor, ProviderSourceItemTextExecutor,
)
from .test_joint_mixed_source_jobs import fixture
from .test_provider_window_executors import _Completions
from .test_native_source_requirement_windowed_reference import _classification_candidate
from .test_source_requirement_jobs import _current, _job, _Context

ROOT = Path(__file__).resolve().parents[2]


class _JointSdk(_Completions):
    def __init__(self, *, fail_text=False):
        super().__init__()
        self.fail_text = fail_text
        self.completed = Counter()

    def create(self, **kwargs):
        packet = json.loads(kwargs['messages'][1]['content'])
        mode = packet['execution_mode']
        files = packet['files']
        self.calls.append(kwargs)
        replies = [item for item in kwargs['messages'] if item['role'] == 'tool']
        if not replies:
            return {'choices': [{'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'read-page', 'type': 'function', 'function': {
                        'name': 'read_source_page', 'arguments': '{"page":2}'}},
                    {'id': 'read-scope', 'type': 'function', 'function': {
                        'name': 'read_stage_file', 'arguments': '{"name":"scope.full.json"}'}},
                ]}}]}
        scope = json.loads(json.loads(replies[1]['content'])['text'])
        key = scope.get('logical_table_scope_id', scope.get('scope_id'))
        self.completed[(mode, key)] += 1
        if mode.startswith('SOURCE_ITEM_TEXT') and self.fail_text:
            raise RuntimeError('Synthetic interrupted provider text stage')
        if mode == 'SOURCE_REQUIREMENT_BOUNDARY_PLAN':
            fragment = scope['fragments'][0]
            content_id = 'bom-context' if key == 'lt-bom' else 'pack-context'
            content = next(item for item in scope['source_content'] if item['content_id'] == content_id)
            payload = {'schema_version': 'source-requirement-boundary-agent-candidate/v1',
                'records': [{'label': key, 'source_form': 'TABLE_RECORD', 'shared_source_content_ids': [],
                    'row_groups': [{'fragment_id': fragment['fragment_id'], 'row_start': 1,
                                    'row_count': fragment['row_count']-1}],
                    'owning_text_spans': [{'content_id': content_id, 'start_offset': 0,
                                           'end_offset': len(content['text'])}]}],
                'context_row_groups': [{'role': 'HEADER', 'row_group': {
                    'fragment_id': fragment['fragment_id'], 'row_start': 0, 'row_count': 1},
                    'reason_codes': ['header']}], 'diagnostic_codes': []}
        elif mode == 'SOURCE_ITEM_TEXT_BOUNDARY_PLAN':
            payload = {'schema_version': 'source-item-text-boundary-agent-candidate/v1',
                'scope_id': key, 'records': [], 'structure_only_spans': [],
                'table_owned_spans': files['table-ownership-context.json']['table_owned_spans']}
        else:
            window = files['phase-input.json']['classification_window']
            payload = _classification_candidate([row['record_key'] for row in window['records']])
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps(payload)}}]}


class ProviderJointMixedSourceJobsTests(unittest.TestCase):
    def scenario(self, provider):
        temp = self.enterContext(TemporaryDirectory())
        source = Path(temp)/'synthetic.pdf'; build_synthetic_pdf(source)
        mapping, previews, requests, candidates, previous, validation = fixture(hashlib.sha256(source.read_bytes()).hexdigest())
        sdk = _JointSdk()
        params = dict(provider=provider, model_id='configured-model', client_factory=sdk.client,
                      kit_root=ROOT/'agent-kit', customer_data_enabled=True)
        container = SimpleNamespace(object_store=LocalObjectStore(Path(temp)/'objects'),
            source_requirement_window_executor=ProviderSourceRequirementWindowExecutor(**params),
            source_item_text_executor=ProviderSourceItemTextExecutor(**params))
        job = _job(mapping, requests['lt-bom'])
        executor = container.source_requirement_window_executor
        job.payload.update({key: getattr(executor, attr) for key, attr in {
            'executor_id':'adapter_id', 'executor_version':'adapter_version',
            'transport_kind':'transport_kind', 'model_id':'model_id', 'reasoning_effort':'reasoning_effort'}.items()})
        current = {scope: {**_current(mapping, request), 'source_path': source,
            'source_requirement_base_bundle': {'synthetic':True}} for scope, request in requests.items()}
        self.enterContext(patch('app.application.runtime.RUNTIME_STATE.container', container))
        self.enterContext(patch.object(jobs, '_current_scope_context', side_effect=lambda **kw: current[kw['logical_table_scope_id']]))
        self.enterContext(patch.object(jobs, 'resolve_effective_source_requirement_bundle', return_value={
            'baseline': previous, 'receipt': validation, 'product_run_receipts': ()}))
        self.enterContext(patch('app.application.agent_workflow.source_item_manual_publication.attach_rebound_baseline_manual_contexts'))
        return sdk, container, job, mapping, previews, previous, current

    def test_both_providers_publish_joint_sdk_result_and_same_version_excel(self):
        for provider in ('azure', 'aliyun'):
            with self.subTest(provider=provider):
                sdk, container, job, mapping, previews, previous, _ = self.scenario(provider)
                context = _Context(job)
                result = jobs.run_durable_source_requirement_baseline(job, context)
                self.assertEqual(context.publication_count, 1)
                with container.object_store.open(result['baseline_revision_artifact']['object_key']) as stream:
                    product = jobs.SourceRequirementProductBaselineArtifactV1.model_validate_json(stream.read())
                self.assertEqual(product.baseline.revision, previous.revision+1)
                self.assertEqual(set(product.baseline.covered_scope_ids), {'lt-bom','lt-pack','lt-dvp','text:bom-node'})
                self.assertEqual(len(product.product_run_receipts), 3)
                self.assertTrue(all(receipt.codex_version.startswith('provider-structured-stage:'+provider+':')
                                    for receipt in product.product_run_receipts))
                prior = next(item for item in product.baseline.records if item.source_record_id == previous.records[0].source_record_id)
                self.assertEqual(prior.source_display_id, previous.records[0].source_display_id)
                snapshot = build_delivery_snapshot(mapping, product.baseline, previews)
                book = load_workbook(BytesIO(export_delivery_xlsx(snapshot)))
                self.addCleanup(book.close)
                matrix = ' '.join(str(cell.value) for row in book['ComplianceMatrix'] for cell in row if cell.value)
                for record in product.baseline.records:
                    for binding in record.text_bindings:
                        self.assertIn(binding.text, matrix)
                self.assertEqual(len(sdk.completed), 5)
                self.assertTrue(any(isinstance(message['content'],list) for call in sdk.calls for message in call['messages']))

    def test_both_providers_resume_text_without_recalling_completed_tables(self):
        for provider in ('azure', 'aliyun'):
            with self.subTest(provider=provider):
                sdk, container, job, *_ = self.scenario(provider)
                sdk.fail_text = True
                context = _Context(job)
                with self.assertRaisesRegex(RuntimeError, 'PROVIDER_STAGE_RUNTIMEERROR'):
                    jobs.run_durable_source_requirement_baseline(job, context)
                self.assertEqual(context.publication_count, 0)
                self.assertEqual(set(context.current_progress['source_requirement_completed_scopes']), {'lt-bom','lt-pack'})
                tables = {key:value for key,value in sdk.completed.items() if key[0].startswith('SOURCE_REQUIREMENT')}
                sdk.fail_text = False
                resumed = replace(job, progress=context.current_progress)
                next_context = _Context(resumed)
                jobs.run_durable_source_requirement_baseline(resumed, next_context)
                self.assertEqual(next_context.publication_count, 1)
                self.assertEqual({key:value for key,value in sdk.completed.items() if key[0].startswith('SOURCE_REQUIREMENT')}, tables)

    def test_completed_provider_candidates_reject_model_and_source_drift_before_publish(self):
        sdk, container, job, _mapping, _previews, _previous, current = self.scenario('azure')
        sdk.fail_text = True
        context = _Context(job)
        with self.assertRaises(RuntimeError): jobs.run_durable_source_requirement_baseline(job, context)
        resumed = replace(job, progress=context.current_progress)
        original_calls = len(sdk.calls)
        container.source_requirement_window_executor.model_id = 'different-model'
        resumed = replace(resumed, payload={**resumed.payload, 'model_id': 'different-model'})
        failed = _Context(resumed)
        with self.assertRaises(NonRetryableJobError): jobs.run_durable_source_requirement_baseline(resumed, failed)
        self.assertEqual(failed.publication_count,0)
        self.assertEqual(len(sdk.calls), original_calls)
        container.source_requirement_window_executor.model_id = 'configured-model'
        resumed.payload['model_id'] = 'configured-model'
        current['lt-bom']['source_path'].write_bytes(b'changed synthetic source')
        failed = _Context(resumed)
        with self.assertRaises((RuntimeError, ValueError)): jobs.run_durable_source_requirement_baseline(resumed, failed)
        self.assertEqual(failed.publication_count,0)
        self.assertEqual(len(sdk.calls), original_calls)
