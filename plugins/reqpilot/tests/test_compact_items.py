"""Structure retention uses real captured synthetic sources and the old compilers."""
# Standalone plugin tests bootstrap their source paths before importing adapters.
# ruff: noqa: E402
from __future__ import annotations

from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'prototype'), str(ROOT / 'plugins/reqpilot/scripts')]

import artifact_tools
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.fast_source_capture import capture_fast_source
from compact_items import expand_structure_retention
from task_contracts import bind_item_decisions
from test_portable import item_decisions, make_fixture, write


class CompactItemsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='reqpilot-compact-items-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.capture, choices = make_fixture(self.root / 'source')
        self.install_map(choices, 'map')

    def install_map(self, choices, name):
        path = self.root / (name + '.json')
        write(path, choices)
        self.map_dir = self.root / name
        artifact_tools.compile_map(Namespace(source=self.source, source_ir=self.capture / 'source_ir.json',
                                             input=path, output_dir=self.map_dir, map_dir=None))
        self.mapping = json.loads((self.map_dir / 'document-map.json').read_text())
        self.requests = json.loads((self.map_dir / 'scope-requests.json').read_text())

    def full_choices(self):
        return {key: value for key, value in item_decisions(self.map_dir).items()
                if key not in {'source_sha256', 'document_map_artifact_sha256'}}

    def compact_choices(self):
        choices = self.full_choices()
        choices['retain_map_structure'] = True
        choices['text_decisions'] = [choice for choice in choices['text_decisions'] if choice['items']]
        for choice in choices['text_decisions']:
            choice.pop('structure_only_spans')
        return choices

    def expand(self, choices=None, **kwargs):
        return expand_structure_retention(choices if choices is not None else self.compact_choices(),
                                           self.mapping, self.requests, **kwargs)

    def compile_items(self, choices, name, baseline=None):
        path = self.root / (name + '.json')
        write(path, bind_item_decisions(choices, self.mapping, self.requests))
        output = self.root / name
        artifact_tools.compile_items(Namespace(map_dir=self.map_dir, input=path,
                                               baseline_dir=baseline, output_dir=output))
        return json.loads((output / 'source-requirement-baseline.json').read_text())

    def front_matter(self):
        import pymupdf

        self.source = self.root / 'front-matter.pdf'
        pdf = pymupdf.open()
        page = pdf.new_page()
        texts = [
            (30, 'Synthetic source package'),
            (65, 'Valve specification'), (90, 'Revision C | Prepared by engineering'),
            (130, 'Contents'), (155, '1 General .... 2\n2 Requirements .... 3'),
            (205, 'Document control'),
            (385, '1 General'), (415, 'The valve shall open within 30 ms.'),
            (450, 'Figure 1 Assembly overview'),
        ]
        for y, text in texts:
            page.insert_text((40, y), text)
        for x in (40, 180, 350):
            page.draw_line((x, 230), (x, 330))
        for y in (230, 260, 295, 330):
            page.draw_line((40, y), (350, y))
        for x, y, text in [(45, 250, 'Revision'), (185, 250, 'Change'),
                           (45, 282, 'A'), (185, 282, 'Initial release'),
                           (45, 317, 'C'), (185, 317, 'Editorial correction')]:
            page.insert_text((x, y), text)
        pdf.save(self.source)
        pdf.close()
        self.capture = self.root / 'front-capture'
        capture_fast_source(self.source, output_dir=self.capture)
        snapshot = json.loads((self.capture / 'source_ir.json').read_text())
        by_text = {node['raw_text']: node['source_node_id'] for node in snapshot['nodes']}
        nodes = [{'node_id': 'main', 'parent_id': None, 'kind': 'DOCUMENT',
                  'title': texts[0][1], 'label': '', 'title_source_ids': [by_text[texts[0][1]]],
                  'content_source_ids': []}]
        for node_id, role, title, bodies in (
                ('cover', 'COVER', texts[1][1], [texts[2][1]]),
                ('toc', 'TABLE_OF_CONTENTS', texts[3][1], [texts[4][1]]),
                ('control', 'DOCUMENT_CONTROL', texts[5][1], []),
                ('general', 'SECTION', texts[6][1], [texts[7][1], texts[8][1]])):
            nodes.append({'node_id': node_id, 'parent_id': 'main', 'kind': role,
                          'title': title, 'label': '', 'title_source_ids': [by_text[title]],
                          'content_source_ids': [by_text[text] for text in bodies]})
        table = next(node for node in snapshot['nodes'] if node.get('table_payload'))
        duplicates = [node['source_node_id'] for node in snapshot['nodes']
                      if node['kind'] != 'table' and 230 < node['native_locator']['bbox'][1] < 330]
        self.install_map({
            'schema_version': 'reqpilot-map-decisions/v1', 'source_sha256': snapshot['source_sha256'],
            'nodes': nodes, 'logical_tables': [{'scope_id': 'revisions', 'owner_node_id': 'control',
                'title': 'Revision history', 'role': 'DOCUMENT_CONTROL',
                'fragments': [{'source_node_id': table['source_node_id'], 'row_range': [0, 3]}]}],
            'source_dispositions': [{'source_node_id': identifier, 'disposition': 'DUPLICATE_CAPTURE',
                                    'reason': 'Native text repeats the visible table cells.'}
                                   for identifier in duplicates],
        }, 'front-map')

    def test_compact_and_explicit_items_compile_to_identical_business_artifact(self):
        compact = self.compact_choices()
        before = (deepcopy(compact), deepcopy(self.mapping), deepcopy(self.requests),
                  self.source.read_bytes(), (self.capture / 'source_ir.json').read_bytes())
        files = {path: path.read_bytes() for path in self.map_dir.rglob('*') if path.is_file()}
        first = self.expand(compact)
        self.assertEqual(first, self.expand(compact))
        self.assertEqual((compact, self.mapping, self.requests, self.source.read_bytes(),
                          (self.capture / 'source_ir.json').read_bytes()), before)
        self.assertEqual(files, {path: path.read_bytes() for path in self.map_dir.rglob('*') if path.is_file()})
        self.assertEqual(self.compile_items(self.full_choices(), 'explicit'),
                         self.compile_items(first['decisions'], 'compact'))
        self.assertEqual(first['receipt']['added_text_scope_ids'], ['text:main'])
        self.assertEqual(first['receipt']['added_structure_only_span_count'], 7)
        for choice in compact['text_decisions']:
            actual = next(item for item in first['decisions']['text_decisions']
                          if item['scope_id'] == choice['scope_id'])
            self.assertEqual(actual['items'], choice['items'])

    def test_explicit_selectors_and_custom_reason_codes_remain_exact_without_duplicates(self):
        choices = self.full_choices()
        choices['retain_map_structure'] = True
        target = choices['text_decisions'][0]['structure_only_spans'][0]
        target['text_span'] = {'content_ref': 0}
        target['reason_codes'] = ['reviewed_heading']
        result = self.expand(choices)
        expected = deepcopy(choices)
        expected.pop('retain_map_structure')
        self.assertEqual(result['decisions'], expected)
        self.assertEqual(result['receipt']['added_structure_only_span_count'], 0)

    def test_supplied_partial_heading_fills_only_uncovered_exact_title(self):
        choices = self.compact_choices()
        target = next(choice for choice in choices['text_decisions'] if choice['scope_id'] == 'text:s1')
        request = next(request for request in self.requests['text_requests'] if request['scope_id'] == 'text:s1')
        ref = next(index for index, content in enumerate(request['source_content']) if content['is_title'])
        explicit = {'text_span': {'content_ref': ref, 'quote': '1'}, 'reason_codes': ['reviewed_heading']}
        target['structure_only_spans'] = [explicit]
        result = self.expand(choices)
        actual = next(choice for choice in result['decisions']['text_decisions'] if choice['scope_id'] == 'text:s1')
        self.assertEqual(actual['structure_only_spans'][0], explicit)
        self.assertEqual(actual['structure_only_spans'][1]['text_span']['start_offset'], 1)
        self.assertEqual(actual['items'], target['items'])

    def test_arbitrary_heading_substrings_do_not_bypass_title_validator(self):
        choices = self.compact_choices()
        target = next(choice for choice in choices['text_decisions'] if choice['scope_id'] == 'text:s1')
        target['structure_only_spans'] = [{'text_span': {'content_ref': 0, 'quote': 'Technical'},
                                         'reason_codes': ['reviewed_heading']}]
        with self.assertRaisesRegex(ValueError, 'STRUCTURE_ONLY_SPAN_NOT_TITLE'):
            self.expand(choices)

    def test_unselected_ordinary_body_and_table_scopes_stay_pending(self):
        result = self.expand({'retain_map_structure': True})
        self.assertEqual([choice['scope_id'] for choice in result['decisions']['text_decisions']], ['text:main'])
        self.assertEqual(len(result['receipt']['pending_text_scope_ids']), 6)
        self.assertEqual(result['receipt']['pending_table_scope_ids'], ['technical-test'])
        self.assertEqual(result['decisions']['table_decisions'], [])

    def test_front_matter_is_retained_in_full_with_zero_source_records(self):
        self.front_matter()
        result = self.expand({'retain_map_structure': True})
        self.assertEqual(result['receipt']['pending_text_scope_ids'], ['text:general'])
        self.assertEqual(result['receipt']['added_table_scope_ids'], ['revisions'])
        baseline = self.compile_items(result['decisions'], 'front-items')
        self.assertEqual(baseline['records'], [])
        self.assertTrue({'text:cover', 'text:toc', 'text:control', 'revisions'} <= set(baseline['covered_scope_ids']))
        table = result['decisions']['table_decisions'][0]
        self.assertEqual(table['context_row_groups'][0]['role'], 'FRONT_MATTER')
        self.assertEqual(table['context_row_groups'][0]['row_group']['row_end'], 3)

    def test_explicit_front_table_header_is_preserved_and_only_remaining_rows_are_added(self):
        self.front_matter()
        header = {'role': 'HEADER', 'row_group': {'fragment_ref': 0, 'row_start': 0, 'row_end': 1},
                  'reason_codes': ['reviewed_header']}
        result = self.expand({'retain_map_structure': True, 'table_decisions': [
            {'logical_table_scope_id': 'revisions', 'records': [], 'context_row_groups': [header]}]})
        groups = result['decisions']['table_decisions'][0]['context_row_groups']
        self.assertEqual(groups[0], header)
        self.assertEqual((groups[1]['row_group']['row_start'], groups[1]['row_group']['row_end']), (1, 3))

    def test_figure_caption_is_not_automatically_structure_even_when_body_is_decided(self):
        self.front_matter()
        request = next(request for request in self.requests['text_requests'] if request['scope_id'] == 'text:general')
        body_ref = next(index for index, content in enumerate(request['source_content']) if 'shall' in content['text'])
        caption = next(content for content in request['source_content'] if content['text'].startswith('Figure'))
        self.assertFalse(caption['is_title'])
        item = self.full_choices()['text_decisions'][-1]['items'][0]
        item['text_spans'] = [{'content_ref': body_ref}]
        choices = {'retain_map_structure': True, 'text_decisions': [{'scope_id': 'text:general', 'items': [item]}]}
        with self.assertRaisesRegex(ValueError, 'COVERAGE_INCOMPLETE'):
            self.expand(choices)

    def test_existing_covered_scopes_are_skipped_only_when_not_explicit(self):
        result = self.expand({'retain_map_structure': True}, covered_scope_ids=('text:main', 'text:s1'))
        self.assertEqual(result['decisions']['text_decisions'], [])
        self.assertNotIn('text:s1', result['receipt']['pending_text_scope_ids'])
        choice = self.full_choices()['text_decisions'][0]
        result = self.expand({'retain_map_structure': True, 'text_decisions': [choice]},
                             covered_scope_ids=('text:main',))
        self.assertEqual(result['decisions']['text_decisions'], [choice])

    def test_zero_length_unknown_scope_overlap_and_uncovered_body_are_still_errors(self):
        for change, message in (
                ('zero', 'non-empty|half-open'), ('unknown_scope', 'Unknown selected text scope'),
                ('unknown_content', 'CONTENT_UNKNOWN'), ('overlap', 'SPAN_OVERLAP'),
                ('missing_body', 'COVERAGE_INCOMPLETE')):
            choices = self.compact_choices()
            target = choices['text_decisions'][0]
            span = target['items'][0]['text_spans'][0]
            if change == 'zero':
                span['start_offset'] = span['end_offset']
            elif change == 'unknown_scope':
                target['scope_id'] = 'text:unknown'
            elif change == 'unknown_content':
                span['content_id'] = 'foreign-content'
            elif change == 'overlap':
                target['items'][0]['text_spans'].append(deepcopy(span))
            else:
                span['end_offset'] -= 10
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                self.expand(choices)

    def test_duplicate_decisions_and_scope_requests_are_not_collapsed(self):
        choices = self.compact_choices()
        choices['text_decisions'].append(deepcopy(choices['text_decisions'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate scope decisions'):
            self.expand(choices)
        self.requests['text_requests'].append(deepcopy(self.requests['text_requests'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate scope requests'):
            self.expand()

    def test_unselected_foreign_and_rehashed_forged_requests_fail_before_auto_retention(self):
        for change in ('source', 'map', 'title_flag', 'role', 'text', 'omit_body'):
            requests = deepcopy(self.requests)
            request = next(request for request in requests['text_requests'] if request['scope_id'] == 'text:s1')
            if change == 'source':
                request['source_sha256'] = 'f' * 64
            elif change == 'map':
                request['document_map_artifact_sha256'] = 'f' * 64
            elif change == 'role':
                request['structural_role'] = 'COVER'
            elif change == 'title_flag':
                request['source_content'][1]['is_title'] = True
            elif change == 'omit_body':
                request['source_content'] = [content for content in request['source_content'] if content['is_title']]
            else:
                content = request['source_content'][1]
                content['text'] += ' Invented.'
                content['text_sha256'] = core.canonical_text_sha256(content['text'])
            request['request_sha256'] = core.canonical_json_sha256({key: value for key, value in request.items()
                                                                  if key != 'request_sha256'})
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'different source or Map|differs from Map'):
                expand_structure_retention({'retain_map_structure': True}, self.mapping, requests)

    def test_rehashed_table_fragment_mismatch_is_not_trusted_for_auto_context(self):
        self.front_matter()
        request = self.requests['table_requests'][0]
        request['fragments'][0]['table_token'] = 'foreign-table'
        request['request_sha256'] = core.canonical_json_sha256({key: value for key, value in request.items()
                                                              if key != 'request_sha256'})
        with self.assertRaisesRegex(ValueError, 'differs from Map'):
            self.expand({'retain_map_structure': True})

    def test_explicit_front_matter_items_and_overlapping_table_rows_remain_invalid(self):
        body = self.full_choices()['text_decisions'][1]['items'][0]
        table_record = self.full_choices()['table_decisions'][0]['records'][0]
        self.front_matter()
        body['text_spans'] = [{'content_ref': 1}]
        with self.assertRaisesRegex(ValueError, 'FRONT_MATTER_MUST_REMAIN_STRUCTURE'):
            self.expand({'retain_map_structure': True,
                         'text_decisions': [{'scope_id': 'text:cover', 'items': [body]}]})
        table_record['row_groups'] = [{'fragment_ref': 0, 'row_start': 1, 'row_end': 3}]
        with self.assertRaisesRegex(ValueError, 'FRONT_MATTER_MUST_REMAIN_STRUCTURE'):
            self.expand({'retain_map_structure': True, 'table_decisions': [
                {'logical_table_scope_id': 'revisions', 'records': [table_record]}]})
        overlap = {'role': 'FRONT_MATTER', 'row_group': {'fragment_ref': 0, 'row_start': 1, 'row_end': 3},
                   'reason_codes': ['explicit_front_matter']}
        with self.assertRaisesRegex(ValueError, 'ROW_OVERLAP'):
            self.expand({'retain_map_structure': True, 'table_decisions': [
                {'logical_table_scope_id': 'revisions', 'records': [],
                 'context_row_groups': [overlap, deepcopy(overlap)]}]})


if __name__ == '__main__':
    unittest.main()
