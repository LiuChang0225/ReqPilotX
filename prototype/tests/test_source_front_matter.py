"""Whole front matter remains readable without creating source-item identities."""
import unittest

from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_ledger import project_source_item_ledger
from app.services.response_matrix import structure_label
from .test_source_requirement_baseline import _reference_with_text_node, _text_candidate, _text_item


class FrontMatterTests(unittest.TestCase):
    def prepare(self, kind='TABLE_OF_CONTENTS'):
        text = '1 Scope ........ 3\n1.1 Interfaces .... 4\n2 Verification .... 5'
        ref = _reference_with_text_node(node_id='front', label='', title='Contents',
            contents=(('toc', text, False),))
        ref['document_tree']['nodes'][-1]['kind'] = kind
        mapping = core.compile_document_map_artifact(ref)
        request = core.build_source_item_text_scope_request(mapping, node_id='front')
        candidate = _text_candidate(request, structure_only=(core.SourceItemStructureOnlySpanV1(
            text_span=core.SourceItemTextSpanV1(content_id='toc', start_offset=0, end_offset=len(text)),
            reason_codes=('whole_front_matter',)),))
        return mapping, request, candidate

    def test_whole_cover_and_contents_preserve_every_character_without_src(self):
        for kind in sorted(core.FRONT_MATTER_KINDS):
            with self.subTest(kind=kind):
                mapping, request, candidate = self.prepare(kind)
                baseline, receipt = core.compile_source_requirement_baseline(document_map=mapping,
                    requests=(), candidates=(), text_requests=(request,), text_candidates=(candidate,))
                self.assertEqual(baseline.records, ())
                self.assertEqual(len(baseline.text_context_dispositions), 1)
                ledger = project_source_item_ledger(document_map=mapping, artifact=baseline)
                row = next(r for r in ledger['rows'] if r['structure_node_id'] == 'front')
                self.assertIsNone(row['source_item_id'])
                self.assertEqual(row['child_row_ids'], [])
                self.assertEqual(next(n for n in mapping.nodes if n.node_id == 'front').source_content[0].text,
                                 request.source_content[0].text)

    def test_numbered_directory_entries_cannot_be_source_items(self):
        mapping, request, candidate = self.prepare()
        item = _text_item(request, key='entry', label='1 Scope', start=0, end=len(request.source_content[0].text))
        with self.assertRaisesRegex(ValueError, 'FRONT_MATTER_MUST_REMAIN_STRUCTURE'):
            core.compile_source_requirement_baseline(document_map=mapping, requests=(), candidates=(),
                text_requests=(request,), text_candidates=(_text_candidate(request, items=(item,)),))

    def test_body_scope_cannot_hide_under_front_matter_disposition(self):
        mapping, request, candidate = self.prepare('SECTION')
        self.assertNotIn('structural_role', request.model_dump(mode='json'))
        with self.assertRaisesRegex(ValueError, 'STRUCTURE_ONLY_SPAN_NOT_TITLE'):
            core.validate_source_item_text_slice_candidate(request, candidate)
        payload = request.model_dump(mode='json', exclude={'request_sha256'})
        payload['structural_role'] = 'COVER'
        forged = core.SourceItemTextScopeRequestV1(**payload, request_sha256=core.canonical_json_sha256(payload))
        forged_candidate = candidate.model_copy(update={'request_sha256': forged.request_sha256})
        with self.assertRaisesRegex(ValueError, 'STRUCTURAL_ROLE_MAP_DRIFT'):
            core.compile_source_requirement_baseline(document_map=mapping, requests=(), candidates=(),
                text_requests=(forged,), text_candidates=(forged_candidate,))

    def test_partial_retention_does_not_erase_directory_text(self):
        mapping, request, candidate = self.prepare()
        span = candidate.structure_only_spans[0].text_span.model_copy(update={'end_offset': 9})
        partial = candidate.model_copy(update={'structure_only_spans': (
            candidate.structure_only_spans[0].model_copy(update={'text_span': span}),)})
        with self.assertRaisesRegex(ValueError, 'COVERAGE_INCOMPLETE'):
            core.validate_source_item_text_slice_candidate(request, partial)

    def test_table_context_requires_explicit_whole_block_map_role(self):
        from .test_empty_source_table import EmptySourceTableTests
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            mapping, request, values = EmptySourceTableTests().prepare(Path(tmp), 'Revision A')
            values['context_row_groups'][1]['role'] = 'FRONT_MATTER'
            candidate = core.SourceRequirementSliceCandidateV1.model_validate(values)
            with self.assertRaisesRegex(ValueError, 'FRONT_MATTER_REQUIRES_MAP_ROLE'):
                core.validate_source_requirement_slice_candidate(request, candidate)
            payload = request.model_dump(mode='json', exclude={'request_sha256'})
            payload['structural_role'] = 'DOCUMENT_CONTROL'
            request = core.SourceRequirementScopeRequestV1(**payload, request_sha256=core.canonical_json_sha256(payload))
            candidate = candidate.model_copy(update={'request_sha256': request.request_sha256})
            core.validate_source_requirement_slice_candidate(request, candidate)

    def test_table_owner_wins_over_embedded_image_medium(self):
        self.assertEqual(structure_label({'source_kind':'SOURCE_ITEM', 'display_role':'TABLE',
            'source_item': {'source_form':'IMAGE'}}), '表格')
        self.assertEqual(structure_label({'source_kind':'SOURCE_ITEM', 'display_role':'TABLE_RECORD',
            'source_item': {'source_form':'IMAGE'}}), '表格条目')
        self.assertEqual(structure_label({'source_kind':'SOURCE_ITEM', 'display_role':'CLAUSE',
            'source_item': {'source_form':'IMAGE'}}), '图片')
