"""Compact Map choices compile to the existing source-faithful business model."""
from __future__ import annotations

from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLUGIN / 'scripts'), str(PLUGIN.parents[1] / 'prototype'), str(PLUGIN / 'tests')]

import artifact_tools
from app.application.fast_source_capture import capture_fast_source
from app.domain.extraction.source_ir import SourceIRSnapshot
from compact_map import OutlineNode, expand_outline
from map_edit_tools import bind_map_edit
from test_portable import make_fixture, write


class CompactMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='reqpilot-outline-')
        self.root = Path(self.temp.name)
        self.source, self.capture, self.original = make_fixture(self.root)
        self.snapshot = SourceIRSnapshot.model_validate_json((self.capture / 'source_ir.json').read_text())
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def ref(self, text):
        return next(i for i, node in enumerate(self.snapshot.nodes) if node.raw_text == text)

    def outline(self):
        nodes = self.original['nodes']
        root = {'role': 'DOCUMENT', 'ref': nodes[0]['title_source_ids'][0], 'children': []}
        for node in nodes[1:]:
            root['children'].append({'role': 'HEADING', 'ref': node['title_source_ids'][0],
                                      'content': node['content_source_ids'][:]})
        table = self.original['logical_tables'][0]
        root['children'][-1]['tables'] = [{'sources': [
            {'source': fragment['source_node_id'], 'row_range': fragment['row_range']}
            for fragment in table['fragments']], 'title': table['title'], 'role': table['role']}]
        return [root]

    def expand(self, outline):
        return expand_outline(self.snapshot, outline, self.source)

    def compile(self, expanded, *, dispositions=None):
        self.serial += 1
        candidate = self.root / f'candidate-{self.serial}.json'
        request = {'schema_version': 'reqpilot-map-edit/v1', 'nodes': expanded['nodes'],
                   'logical_tables': expanded['logical_tables']}
        if dispositions is not None:
            request['source_dispositions'] = dispositions
        write(candidate, request)
        output = self.root / f'map-{self.serial}'
        artifact_tools.compile_map(Namespace(source=self.source, source_ir=self.capture / 'source_ir.json',
                                             input=candidate, output_dir=output))
        return output

    def custom(self, pages):
        import pymupdf
        source = self.root / 'custom.pdf'
        pdf = pymupdf.open()
        for text in pages:
            page = pdf.new_page()
            page.insert_text((40, 70), text)
            for x in (40, 150, 290):
                page.draw_line((x, 200), (x, 290))
            for y in (200, 230, 260, 290):
                page.draw_line((40, y), (290, y))
            for row, label in enumerate(('Part', 'Valve', 'Housing')):
                page.insert_text((45, 220 + row * 30), label)
                page.insert_text((155, 220 + row * 30), str(row))
        pdf.save(source)
        pdf.close()
        self.source, self.capture = source, self.root / 'custom-capture'
        capture_fast_source(source, output_dir=self.capture)
        self.snapshot = SourceIRSnapshot.model_validate_json((self.capture / 'source_ir.json').read_text())

    def test_manual_decisions_and_outline_preserve_same_business_content(self):
        outline = self.outline()
        before = (self.source.read_bytes(), (self.capture / 'source_ir.json').read_bytes(), deepcopy(outline))
        expanded = self.expand(outline)
        compact_dir = self.compile(expanded, dispositions=self.original['source_dispositions'])
        original = deepcopy(self.original)
        original['schema_version'] = 'reqpilot-map-decisions/v2'
        path = self.root / 'manual.json'
        write(path, original)
        manual_dir = self.root / 'manual-map'
        artifact_tools.compile_map(Namespace(source=self.source, source_ir=self.capture / 'source_ir.json',
                                             input=path, output_dir=manual_dir))
        def business(directory):
            artifact = json.loads((directory / 'document-map.json').read_text())
            titles = {node['node_id']: node['title'] for node in artifact['nodes']}
            return [{'kind': node['kind'], 'title': node['title'], 'label': node['label'],
                     'parent': titles.get(node['parent_id']), 'pages': node['pages'],
                     'source_content': node['source_content']}
                    for node in artifact['nodes']]
        self.assertEqual(business(compact_dir), business(manual_dir))
        compact_tables = json.loads((compact_dir / 'table-previews.json').read_text())
        manual_tables = json.loads((manual_dir / 'table-previews.json').read_text())
        self.assertEqual([t['fragments'] for t in compact_tables.values()],
                         [t['fragments'] for t in manual_tables.values()])
        receipt = expanded['receipt']
        self.assertEqual(receipt['elided_fields'], {'node_id': 7, 'parent_id': 7, 'label': 7,
                                                   'scope_id': 1, 'owner_node_id': 1})
        self.assertEqual(receipt['explicit_identity_and_label_fields'], {'outline': 0, 'expanded': 23})
        self.assertEqual(receipt['semantic_verification'], 'NOT_RUN')
        self.assertEqual((self.source.read_bytes(), (self.capture / 'source_ir.json').read_bytes(), outline), before)
        self.assertFalse(any(self.root.rglob('*outline-bind*')))

    def test_sibling_insert_reparent_and_reference_spelling_keep_existing_ids(self):
        outline = self.outline()
        removed = outline[0]['children'].pop(0)
        first = self.expand(outline)
        outline[0]['children'].insert(0, removed)
        second = self.expand(outline)
        self.assertEqual([n['node_id'] for n in first['nodes']],
                         [n['node_id'] for n in second['nodes'] if n['node_id'] != second['nodes'][1]['node_id']])
        moved = outline[0]['children'].pop()
        outline[0]['children'][0]['children'] = [moved]
        third = self.expand(outline)
        self.assertEqual({n['node_id'] for n in second['nodes']}, {n['node_id'] for n in third['nodes']})
        self.assertEqual(second['logical_tables'][0]['scope_id'], third['logical_tables'][0]['scope_id'])
        source_id = outline[0]['ref']
        index = next(i for i, node in enumerate(self.snapshot.nodes) if node.source_node_id == source_id)
        outline[0]['ref'] = {'source': index, 'quote': self.snapshot.nodes[index].raw_text}
        fourth = self.expand(outline)
        self.assertEqual(third['nodes'][0]['node_id'], fourth['nodes'][0]['node_id'])

    def test_split_source_clauses_keep_body_and_leading_whitespace_exact(self):
        self.custom(['  1.1 Valve shall open.\n  1.2 Valve shall close.'])
        index = next(i for i, n in enumerate(self.snapshot.nodes) if '1.1' in n.raw_text)
        raw = self.snapshot.nodes[index].raw_text
        self.assertTrue(raw.startswith('  '), raw)
        outline = [{'role': 'DOCUMENT', 'children': [
            {'role': 'CLAUSE', 'ref': {'source': index, 'end_before': '  1.2'}},
            {'role': 'CLAUSE', 'ref': {'source': index, 'start': '  1.2'}}]}]
        expanded = self.expand(outline)
        output = self.compile(expanded)
        artifact = json.loads((output / 'document-map.json').read_text())
        clauses = [n for n in artifact['nodes'] if n['kind'] == 'SECTION']
        self.assertEqual([n['label'] for n in clauses], ['1.1', '1.2'])
        self.assertNotEqual(clauses[0]['node_id'], clauses[1]['node_id'])
        for node in clauses:
            self.assertIn('Valve shall', ''.join(c['text'] for c in node['source_content'] if not c['is_title']))
        spans = sorted((c for n in clauses for c in n['source_content']), key=lambda c: c['source_text_range'][0])
        self.assertEqual(''.join(c['text'] for c in spans), raw)

    def test_cross_page_table_fragments_and_multiple_document_roots(self):
        self.custom(['Specification A', 'Attachment B'])
        indices = [i for i, n in enumerate(self.snapshot.nodes) if n.table_payload is not None]
        outline = [{'role': 'DOCUMENT', 'ref': self.ref('Specification A'), 'tables': [
            {'sources': indices, 'title': 'Parts', 'role': 'BOM'}]},
            {'role': 'ATTACHMENT', 'ref': self.ref('Attachment B')}]
        expanded = self.expand(outline)
        output = self.compile(expanded)
        artifact = json.loads((output / 'document-map.json').read_text())
        self.assertEqual(sum(n['parent_id'] is None for n in artifact['nodes']), 2)
        table = next(iter(json.loads((output / 'table-previews.json').read_text()).values()))
        self.assertEqual([f['page'] for f in table['fragments']], [1, 2])
        self.assertEqual([f['row_range'] for f in table['fragments']], [[0, 3], [0, 3]])
        swapped = deepcopy(outline)
        swapped[1]['tables'] = swapped[0].pop('tables')
        swapped[1]['tables'][0]['sources'].reverse()
        self.assertEqual(expanded['logical_tables'][0]['scope_id'], self.expand(swapped)['logical_tables'][0]['scope_id'])

    def test_heading_prefix_with_whitespace_keeps_complete_original_evidence(self):
        self.custom(['  1.4 Technical limits'])
        index = self.ref('  1.4 Technical limits')
        expanded = self.expand([{'role': 'DOCUMENT', 'children': [{'role': 'HEADING', 'ref': index}]}])
        output = self.compile(expanded)
        artifact = json.loads((output / 'document-map.json').read_text())
        heading = next(node for node in artifact['nodes'] if node['kind'] == 'SECTION')
        self.assertEqual((heading['label'], heading['title']), ('1.4', 'Technical limits'))
        self.assertEqual(heading['source_content'][0]['text'], '  1.4 Technical limits')

    def test_existing_compiler_still_rejects_overlapping_source_claims(self):
        outline = self.outline()
        table = outline[0]['children'][-1]['tables'][0]
        source = table['sources'][0]['source']
        table['sources'] = [{'source': source, 'row_range': [0, 2]}, {'source': source, 'row_range': [1, 3]}]
        expanded = self.expand(outline)
        with self.assertRaisesRegex(ValueError, 'Physical table regions overlap'):
            self.compile(expanded)
        self.assertFalse((self.root / f'map-{self.serial}').exists())

    def test_no_automatic_dispositions_or_hidden_missing_sources(self):
        expanded = self.expand([{'role': 'DOCUMENT', 'content': [self.outline()[0]['ref']]}])
        output = self.compile(expanded)
        receipt = json.loads((output / 'map-compilation-receipt.json').read_text())
        self.assertEqual(receipt['evidence_state'], 'INSUFFICIENT')
        self.assertTrue(receipt['uncovered_source_node_ids'])
        decisions = json.loads((output / 'map-decisions.json').read_text())
        self.assertEqual(decisions['source_dispositions'], [])

    def test_whole_front_matter_preserves_content_without_inventing_heading(self):
        refs = [self.original['nodes'][0]['title_source_ids'][0], self.original['nodes'][1]['title_source_ids'][0]]
        for role, title in [('COVER', 'Cover'), ('CONTENTS', 'Contents'), ('CONTROL', 'Document control')]:
            outline = [{'role': 'DOCUMENT', 'children': [{'role': role, 'content': refs}]}]
            typed = OutlineNode.model_validate(outline[0]).model_dump(mode='json', exclude_unset=True)
            expanded = self.expand([typed])
            output = self.compile(expanded)
            artifact = json.loads((output / 'document-map.json').read_text())
            node = next(n for n in artifact['nodes'] if n['kind'] != 'DOCUMENT')
            self.assertEqual((node['title'], node['label']), (title, ''))
            self.assertEqual(node['title_authority'], 'AGENT_DISPLAY_LABEL')
            self.assertTrue(all(not content['is_title'] for content in node['source_content']))
            self.assertEqual([c['text'] for c in node['source_content']],
                             [next(n.raw_text for n in self.snapshot.nodes if n.source_node_id == ref) for ref in refs])
            reordered = deepcopy(outline)
            reordered[0]['children'][0]['content'].reverse()
            self.assertEqual(expanded['nodes'][1]['node_id'], self.expand(reordered)['nodes'][1]['node_id'])
            changed = deepcopy(outline)
            changed[0]['children'][0]['content'].pop()
            self.assertNotEqual(expanded['nodes'][1]['node_id'], self.expand(changed)['nodes'][1]['node_id'])
        with self.assertRaisesRegex(ValueError, 'nonempty content'):
            self.expand([{'role': 'COVER'}])

    def test_auxiliary_evidence_refs_preserve_existing_binding_without_ownership(self):
        outline = self.outline()
        table_source = self.original['logical_tables'][0]['fragments'][0]['source_node_id']
        evidence = [{'source': self.original['nodes'][2]['content_source_ids'][0]},
                    {'source': table_source, 'row_range': [0, 2]}]
        original_expanded = self.expand(outline)
        outline[0]['children'][0]['evidence_refs'] = evidence
        typed = OutlineNode.model_validate(outline[0]).model_dump(mode='json', exclude_unset=True)
        expanded = self.expand([typed])
        self.assertEqual(original_expanded['nodes'][1]['node_id'], expanded['nodes'][1]['node_id'])
        self.assertEqual(expanded['nodes'][1]['evidence_refs'], evidence)
        compact_dir = self.compile(expanded)
        manual = deepcopy(original_expanded)
        manual['nodes'][1]['evidence_refs'] = evidence
        manual_dir = self.compile(manual)
        self.assertEqual((compact_dir / 'document-map.json').read_bytes(), (manual_dir / 'document-map.json').read_bytes())
        artifact = json.loads((compact_dir / 'document-map.json').read_text())
        node = next(n for n in artifact['nodes'] if n['node_id'] == expanded['nodes'][1]['node_id'])
        self.assertEqual(len(node['evidence_anchors']), 2)
        claimed = {source for content in node['source_content'] for source in content['source_handles']}
        self.assertTrue(claimed.isdisjoint(entry['source'] for entry in evidence))

    def test_duplicate_virtual_and_source_identities_are_rejected(self):
        source = self.outline()[0]['ref']
        index = next(i for i, n in enumerate(self.snapshot.nodes) if n.source_node_id == source)
        for outline, message in [
            ([{'role': 'DOCUMENT'}, {'role': 'DOCUMENT'}], 'duplicate virtual'),
            ([{'role': 'DOCUMENT', 'ref': source}, {'role': 'DOCUMENT', 'ref': index}], 'duplicate source identity'),
            ([{'role': 'HEADING'}], 'only a DOCUMENT root'),
            ([{'role': 'DOCUMENT', 'ref': source, 'children': [{'role': 'DOCUMENT'}]}], 'only a DOCUMENT root')]:
            with self.subTest(outline=outline), self.assertRaisesRegex(ValueError, message):
                self.expand(outline)

    def test_table_non_table_duplicate_and_bad_row_ranges_are_rejected(self):
        outline = self.outline()
        table = outline[0]['children'][-1]['tables'][0]
        original = deepcopy(table)
        for sources, message in [
            ([outline[0]['ref']], 'existing TableIR'),
            ([*original['sources'], *original['sources']], 'duplicate table fragment'),
            ([{'source': original['sources'][0]['source'], 'row_range': [0, 999]}], 'exceeds'),
            ([{'source': original['sources'][0]['source'], 'row_range': [True, 2]}], 'integer range'),
            ([{'source': original['sources'][0]['source'], 'row_range': [2, 2]}], 'integer range')]:
            table['sources'] = sources
            with self.subTest(sources=sources), self.assertRaisesRegex(ValueError, message):
                self.expand(outline)
        table['sources'] = original['sources']
        outline[0]['children'][0]['tables'] = [deepcopy(table)]
        with self.assertRaisesRegex(ValueError, 'duplicate table scope'):
            self.expand(outline)

    def test_ambiguous_or_invented_labels_and_unlocatable_quotes_are_rejected(self):
        self.custom(['20 ms response time\n1 and 2 apply\n1.1 Valve opens\n1.2 Valve closes'])
        index = next(i for i, n in enumerate(self.snapshot.nodes) if '20 ms' in n.raw_text)
        for quote in ('20 ms response time', '1 and 2 apply', '1.1 Valve opens\n1.2 Valve closes'):
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, 'explicit exact label'):
                self.expand([{'role': 'CLAUSE', 'ref': {'source': index, 'quote': quote}}])
        unnumbered = self.expand([{'role': 'HEADING', 'ref': {'source': index, 'quote': '20 ms response time'}}])
        self.assertEqual(unnumbered['nodes'][0]['heading']['label'], '')
        for ref, label, message in [
            ({'source': index, 'quote': '1.1 Valve opens'}, '9', 'exact explicit label'),
            ({'source': index, 'quote': 'missing'}, '1', 'exactly once'),
            ({'source': index, 'quote': '1'}, '1', 'exactly once')]:
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, message):
                self.expand([{'role': 'CLAUSE', 'ref': ref, 'label': label}])

    def test_typed_outline_advertises_roles_selectors_and_forbids_ignored_fields(self):
        model = OutlineNode.model_validate(self.outline()[0])
        encoded = model.model_dump(mode='json', exclude_unset=True)
        self.assertEqual(self.expand([encoded])['nodes'], self.expand(self.outline())['nodes'])
        schema = OutlineNode.model_json_schema()
        self.assertIn('CLAUSE', json.dumps(schema))
        self.assertIn('end_before', json.dumps(schema))
        with self.assertRaises(ValueError):
            OutlineNode.model_validate({'role': 'HEADING', 'ref': 0, 'parent_id': 'guessed'})
        with self.assertRaises(ValueError):
            OutlineNode.model_validate({'role': 'CLAUSE', 'ref': {'source': 0, 'quote': 'x', 'start': 'x'}})


if __name__ == '__main__':
    unittest.main()
