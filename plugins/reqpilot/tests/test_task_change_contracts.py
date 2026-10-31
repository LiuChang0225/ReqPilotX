"""Typed choices replay real core compilers against an invented captured PDF."""
from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'prototype'), str(ROOT / 'plugins/reqpilot/scripts')]
from test_portable import make_fixture, item_decisions, write
import artifact_tools
import task_change_contracts as contract
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_decomposition import build_source_item_decomposition_packet, compile_source_item_decomposition
from app.application.agent_workflow.source_item_visual import build_source_item_image_scope_packet, compile_source_item_images, compile_source_item_visual_readings
from scope_tools import scope_template, compile_scope


class ChangeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.source, self.capture, decisions = make_fixture(self.root / 'fixture', table_image=True)
        write(self.root / 'map-input.json', decisions); self.mapping = self.root / 'map'
        artifact_tools.compile_map(Namespace(source=self.source, source_ir=self.capture / 'source_ir.json', input=self.root / 'map-input.json', output_dir=self.mapping))
        write(self.root / 'items-input.json', item_decisions(self.mapping)); self.items = self.root / 'items'
        artifact_tools.compile_items(Namespace(map_dir=self.mapping, baseline_dir=None, input=self.root / 'items-input.json', output_dir=self.items))
        self.document_map = core.DocumentMapArtifactV1.model_validate(json.loads((self.items / 'document-map.json').read_text()))
        self.baseline = core.SourceRequirementBaselineArtifactV1.model_validate(json.loads((self.items / 'source-requirement-baseline.json').read_text()))
        self.receipt = core.SourceRequirementBaselineValidationReceiptV1.model_validate(json.loads((self.items / 'validation-receipt.json').read_text()))
        self.tables = json.loads((self.items / 'table-previews.json').read_text())
        self.parent = next(r for r in self.baseline.records if r.node_id == 's1')
        self.table_item = next(r for r in self.baseline.records if r.table_fragments)

    def decomposition_request(self):
        packet = build_source_item_decomposition_packet(self.baseline, self.parent.source_record_id, table_previews=self.tables)
        text = packet['segments'][0]['text']
        return {'authorization': {'source_item_ids': [self.parent.source_display_id]}, 'decisions': [{
            'source_item_id': self.parent.source_display_id, 'action': 'SPLIT', 'reason': 'Opening and closing have independent measured limits.',
            'children': [{'text': line, 'derivation_type': 'VERBATIM_SPAN', 'source_ranges': [{'segment_index': 0, 'quote': line}]} for line in text.splitlines()]}]}


class TaskChangeContractsTests(ChangeFixture):
    def test_compact_number_refs_bind_same_original_coordinates(self):
        from task_change_reading import _facts
        from app.application.agent_workflow.source_item_visual import compile_source_item_annotation_updates
        heading=next(c for n in self.document_map.nodes if n.node_id=='s1' for c in n.source_content if c.is_title)
        choices={'annotation_updates':[{'source_item_id':self.parent.source_display_id,'source_number':'1',
            'source_number_ref':{'kind':'ITEM_HEADING','heading_ref':0},'reason':'Read the original owning heading.'}]}
        compact=contract.bind_annotation_changes(choices,self.document_map,self.baseline,table_previews=self.tables)
        exact=deepcopy(choices)
        exact['annotation_updates'][0]['source_number_ref']={'kind':'MAP_HEADING','content_id':heading.content_id,'start_offset':0,'end_offset':1}
        self.assertEqual(compact,contract.bind_annotation_changes(exact,self.document_map,self.baseline,table_previews=self.tables))
        facts,_=_facts(self.table_item,self.document_map,self.baseline,self.tables)
        # Test location equivalence only: the adapter does not declare a cell's
        # engineering role or decide whether the Agent should number from it.
        choice={'source_item_id':self.table_item.source_display_id,'source_numbers':[
            {'value':facts[0]['text'],'source_number_ref':{'kind':'ITEM_SOURCE','segment_index':0}}],
            'reason':'Synthetic locator equivalence, not number-role inference.'}
        compact=contract.bind_annotation_changes({'annotation_updates':[choice]},self.document_map,self.baseline,table_previews=self.tables)
        ref=compact['annotation_updates'][0]['source_numbers'][0]['source_number_ref']
        self.assertEqual(ref,{'kind':'TABLE_CELL','fragment_id':facts[0]['source_ref']['table_id'],
            'cell_id':facts[0]['source_ref']['cell_id'],'start_offset':0,'end_offset':len(facts[0]['text'])})
        compiled=compile_source_item_annotation_updates(self.document_map,self.baseline,compact['annotation_updates'],table_previews=self.tables)
        self.assertEqual(compiled['records'][self.table_item.source_record_id]['source_numbers'][0]['value'],facts[0]['text'])

    def test_compact_number_refs_do_not_guess_ranges_or_replace_explicit_clear(self):
        for value,ref in [('1',{'kind':'ITEM_SOURCE','segment_index':999}),
                ('12',{'kind':'ITEM_SOURCE','segment_index':1}),
                ('Resp',{'kind':'ITEM_SOURCE','segment_index':0}),
                ('1',{'kind':'ITEM_HEADING','heading_ref':999})]:
            with self.assertRaises(ValueError):
                contract.bind_annotation_changes({'annotation_updates':[{'source_item_id':self.table_item.source_display_id,
                    'source_number':value,'source_number_ref':ref,'reason':'Reject unsupported source claim.'}]},
                    self.document_map,self.baseline,table_previews=self.tables)
        clear=contract.bind_annotation_changes({'annotation_updates':[{'source_item_id':self.parent.source_display_id,
            'source_number':'','reason':'Explicitly clear mistaken number.'}]},self.document_map,self.baseline,table_previews=self.tables)
        self.assertEqual(clear['annotation_updates'][0]['source_number'],'')
        self.assertNotIn('source_number_ref',clear['annotation_updates'][0])

    def test_exact_heading_number_does_not_depend_on_inaccurate_map_label(self):
        from app.application.agent_workflow.source_item_visual import _source_number_evidence
        node=next(n for n in self.document_map.nodes if n.node_id=='s1')
        heading=next(c for c in node.source_content if c.is_title)
        mapping=self.document_map.model_copy(update={'nodes':tuple(
            n.model_copy(update={'label':heading.text}) if n.node_id==node.node_id else n for n in self.document_map.nodes)})
        ref={'kind':'MAP_HEADING','content_id':heading.content_id,'start_offset':0,'end_offset':1}
        self.assertEqual(_source_number_evidence(mapping,self.baseline,self.parent.source_record_id,'1',ref,self.tables)['text'],'1')
        with self.assertRaises(ValueError):
            _source_number_evidence(mapping,self.baseline,self.parent.source_record_id,'2',ref,self.tables)

    def test_typed_and_explicit_ranges_compile_identical_children_and_retries_preserve_snn(self):
        request = self.decomposition_request()
        bound = contract.bind_decomposition_decisions(request, self.baseline, table_previews=self.tables, authorization_request='Split independent obligations where necessary.')
        self.assertEqual(bound['authorization']['source_item_ids'], [self.parent.source_record_id])
        self.assertEqual(bound['baseline_sha256'], self.baseline.artifact_sha256)
        self.assertNotIn('quote', bound['decisions'][0]['children'][0]['source_ranges'][0])
        first = compile_source_item_decomposition(self.baseline, bound['decisions'], table_previews=self.tables,
            authorized_source_item_ids=bound['authorization']['source_item_ids'])
        repeated = compile_source_item_decomposition(self.baseline, bound['decisions'], table_previews=self.tables,
            previous=first, authorized_source_item_ids=bound['authorization']['source_item_ids'])
        self.assertEqual(first['records'], repeated['records'])
        self.assertEqual([c['requirement_display_id'] for c in first['records'][self.parent.source_record_id]],
            [self.parent.requirement_display_id + '-S01', self.parent.requirement_display_id + '-S02'])
        exact = deepcopy(request)
        for child, compiled in zip(exact['decisions'][0]['children'], bound['decisions'][0]['children']):
            child['source_ranges'] = compiled['source_ranges']
        self.assertEqual(contract.bind_decomposition_decisions(exact, self.baseline, table_previews=self.tables,
            authorization_request='Split independent obligations where necessary.'), bound)
        undo = {'authorization': request['authorization'], 'decisions': [{'source_item_id': self.parent.source_display_id,
            'action': 'UNDO', 'reason': 'Restore complete parent analysis.'}]}
        resolved = contract.bind_decomposition_decisions(undo, self.baseline, table_previews=self.tables, authorization_request='Undo the split.')
        undone = compile_source_item_decomposition(self.baseline, resolved['decisions'], table_previews=self.tables,
            previous=first, authorized_source_item_ids=resolved['authorization']['source_item_ids'])
        self.assertEqual(undone['records'], {})
        self.assertEqual(len(undone['retired_singular_ids']), 2)

    def test_authorization_is_real_and_remaining_scope_is_not_erased(self):
        value = self.decomposition_request()
        with self.assertRaisesRegex(ValueError, 'actual recorded user authorization'):
            contract.bind_decomposition_decisions(value, self.baseline, table_previews=self.tables)
        other = next(r for r in self.baseline.records if r.node_id == 's2')
        value['authorization']['source_item_ids'].append(other.source_display_id)
        bound = contract.bind_decomposition_decisions(value, self.baseline, table_previews=self.tables, authorization_request='Review both parents.')
        artifact = compile_source_item_decomposition(self.baseline, bound['decisions'], table_previews=self.tables,
            authorized_source_item_ids=bound['authorization']['source_item_ids'])
        self.assertEqual(artifact['status'], 'PARTIAL')
        self.assertIn(other.source_record_id, artifact['review_scope']['remaining_source_item_ids'])
        value['authorization']['source_item_ids'] = [other.source_display_id]
        with self.assertRaisesRegex(ValueError, 'authorized'):
            contract.bind_decomposition_decisions(value, self.baseline, table_previews=self.tables, authorization_request='Review another parent.')

    def test_missing_source_is_rejected_and_explicit_unresolved_range_remains_partial(self):
        value = self.decomposition_request()
        child = value['decisions'][0]['children'][0]
        omitted = child['text'][:len('The valve shall ')]
        child['text'] = child['text'][len(omitted):]; child['source_ranges'][0]['quote'] = child['text']
        with self.assertRaisesRegex(ValueError, 'cover'):
            contract.bind_decomposition_decisions(value, self.baseline, table_previews=self.tables, authorization_request='Review this parent.')
        value['decisions'][0]['context_ranges'] = [{'source_range': {'segment_index': 0, 'start_offset': 0, 'end_offset': len(omitted)}, 'disposition': 'UNRESOLVED'}]
        bound = contract.bind_decomposition_decisions(value, self.baseline, table_previews=self.tables, authorization_request='Review this parent.')
        self.assertEqual(bound['decisions'][0]['context_ranges'][0]['disposition'], 'UNRESOLVED')
        artifact = compile_source_item_decomposition(self.baseline, bound['decisions'], table_previews=self.tables,
            authorized_source_item_ids=bound['authorization']['source_item_ids'])
        self.assertEqual(artifact['status'], 'PARTIAL')
        value['decisions'][0]['context_ranges'][0]['disposition'] = 'CONTEXT'
        value['decisions'][0]['logic'] = 'ANY_OF'
        bound = contract.bind_decomposition_decisions(value, self.baseline, table_previews=self.tables, authorization_request='Review alternatives.')
        artifact = compile_source_item_decomposition(self.baseline, bound['decisions'], table_previews=self.tables,
            authorized_source_item_ids=bound['authorization']['source_item_ids'])
        self.assertEqual({c['group_logic'] for c in artifact['records'][self.parent.source_record_id]}, {'ANY_OF'})

    def test_ambiguous_quote_unknown_range_and_silent_keep_payload_are_rejected(self):
        value = self.decomposition_request(); value['decisions'][0]['children'][0]['source_ranges'][0]['quote'] = 'The valve shall'
        with self.assertRaisesRegex(ValueError, 'exactly once'):
            contract.bind_decomposition_decisions(value, self.baseline, table_previews=self.tables, authorization_request='Split.')
        value = self.decomposition_request(); value['decisions'][0]['children'][0]['source_ranges'][0]['segment_index'] = 100
        with self.assertRaisesRegex(ValueError, 'outside'):
            contract.bind_decomposition_decisions(value, self.baseline, table_previews=self.tables, authorization_request='Split.')
        value = self.decomposition_request(); value['decisions'][0]['action'] = 'KEEP'
        with self.assertRaisesRegex(ValueError, 'KEEP/UNDO'):
            contract.DecompositionRequest.model_validate(value)
        value = self.decomposition_request(); value['baseline_sha256'] = self.baseline.artifact_sha256
        with self.assertRaises(ValueError): contract.DecompositionRequest.model_validate(value)

    def test_visual_source_number_and_context_binding_do_not_change_original_table(self):
        packet = build_source_item_image_scope_packet(self.document_map, node_id='s6', baseline=self.baseline, table_previews=self.tables)
        relation = packet['table_image_relations'][0]
        target = next(r for r in self.baseline.records if r.source_record_id == relation['table_source_record_id'])
        heading = next(c for n in self.document_map.nodes if n.node_id == 's1' for c in n.source_content if c.is_title)
        value = {'visual_readings': [{'source_item_id': target.source_display_id, 'image_anchor_ids': [relation['image_binding']['anchor_id']],
            'text': 'The original diagram is embedded within the response test row.', 'association_reason': 'Its source bounding box is inside that row.', 'unresolved': ['Drawing labels need visual review.']}],
            'annotation_updates': [{'source_item_id': self.parent.source_display_id, 'source_number': '1',
                'source_number_ref': {'kind': 'MAP_HEADING', 'content_id': heading.content_id, 'start_offset': 0, 'end_offset': 1},
                'topics': ['VERIFICATION'], 'context_source_ids': [target.source_display_id], 'reason': 'Exact heading supplies the original clause number.'}]}
        bound = contract.bind_annotation_changes(value, self.document_map, self.baseline, table_previews=self.tables)
        self.assertEqual(bound['annotation_updates'][0]['context_source_ids'], [target.source_record_id])
        annotations = compile_source_item_visual_readings(self.document_map, self.baseline, bound['visual_readings'])
        self.assertEqual(annotations['records'][target.source_record_id]['visual_interpretation']['authority'], 'AGENT_VISUAL_INTERPRETATION')
        self.assertEqual(target.source_form.value, 'TABLE_RECORD')
        bad = deepcopy(value); bad['visual_readings'][0]['source_item_id'] = self.parent.source_display_id
        with self.assertRaisesRegex(ValueError, 'owned'):
            contract.bind_annotation_changes(bad, self.document_map, self.baseline, table_previews=self.tables)

    def test_classification_preserves_field_name_and_resolves_current_src(self):
        value = {'model_id': 'test-model', 'decisions': [{'source_record_id': self.parent.source_display_id, 'requirementness': 'REQUIREMENT',
            'requirement_kinds': ['QUALITY', 'FUNCTIONAL'], 'confidence': 0.9, 'decision_summary': 'The complete target combines a function and its measured limit.',
            'reason_codes': ['explicit_source']}], 'addressed_feedback_ids': ['feedback-1']}
        bound = contract.bind_existing_item_changes(value, self.document_map, self.baseline, table_previews=self.tables)
        decision = bound['decisions'][0]
        self.assertEqual(decision['source_record_id'], self.parent.source_record_id)
        self.assertNotIn('source_item_id', decision)
        baseline, _, _, _ = core.compile_source_requirement_classification_coordination(previous_artifact=self.baseline,
            previous_validation_receipt=self.receipt, decisions=(core.SourceRequirementClassificationCoordinationDecisionV1.model_validate(decision),),
            model_id='test-model', coordinator_role='PORTABLE_AGENT')
        self.assertEqual([r.source_record_id for r in baseline.records], [r.source_record_id for r in self.baseline.records])
        with self.assertRaises(ValueError): contract.ExistingItemsChangeRequest.model_validate({k:v for k,v in value.items() if k != 'model_id'})
        bad = deepcopy(value); bad['decisions'][0]['source_record_id'] = self.parent.requirement_display_id
        with self.assertRaisesRegex(ValueError, 'exactly one current SRC'):
            contract.bind_existing_item_changes(bad, self.document_map, self.baseline)

    def test_scope_replacement_reuses_local_selectors_keys_and_real_coverage(self):
        template = scope_template(self.document_map, self.baseline, self.tables, 'text:s1')
        candidate = deepcopy(template['candidate_template']); item = candidate['items'][0]
        original_key = item['item_key']; item['source_form'] = 'PARAGRAPH'
        content_ref = next(i for i,c in enumerate(template['request']['source_content']) if not c['is_title'])
        item['text_spans'] = [{'content_ref': content_ref}]
        request = {'scope_revision': {'affected_scope_ids': ['text:s1'], 'text_decisions': [candidate]}, 'addressed_feedback_ids': ['finding-1']}
        bound = contract.bind_existing_item_changes(request, self.document_map, self.baseline, table_previews=self.tables)
        self.assertEqual(bound['scope_revision']['text_decisions'][0]['items'][0]['item_key'], original_key)
        baseline, _, _, _ = compile_scope(self.document_map, self.baseline, self.receipt, self.tables,
            bound['scope_revision'], feedback_ids=['finding-1'])
        self.assertEqual([r.source_record_id for r in baseline.records], [r.source_record_id for r in self.baseline.records])
        self.assertEqual(next(r for r in baseline.records if r.source_record_id == self.parent.source_record_id).source_form.value, 'PARAGRAPH')
        bad = deepcopy(request); bad['scope_revision']['affected_scope_ids'].append('text:s2')
        with self.assertRaisesRegex(ValueError, 'complete explicit affected'):
            contract.bind_existing_item_changes(bad, self.document_map, self.baseline, table_previews=self.tables)
        from task_service import ExistingItemsRequest
        with self.assertRaisesRegex(ValueError, 'recorded feedback'):
            ExistingItemsRequest.model_validate({'task_id': 'task-' + 'a' * 32, 'expected_revision': 'current',
                'changes': {'scope_revision': request['scope_revision']}})
        supplied = ExistingItemsRequest.model_validate({'task_id': 'task-' + 'a' * 32, 'expected_revision': 'current',
            'changes': {'scope_revision': request['scope_revision']},
            'self_review_finding': {'text': 'The unchanged original is a paragraph, not a numbered clause.',
                'source_item_ids': [self.parent.source_display_id]}})
        self.assertIsNotNone(supplied.self_review_finding)

    def test_standalone_image_input_uses_true_packet_and_rejects_embedded_duplicate(self):
        packet = build_source_item_image_scope_packet(self.document_map, node_id='s6', baseline=self.baseline, table_previews=self.tables)
        value = {'scopes': [{'node_id': 's6', 'items': [{'item_key': 'standalone-' + str(i), 'display_label': 'Original size diagram',
            'image_anchor_ids': [image['anchor_id']], 'requirementness': 'REQUIREMENT', 'requirement_kinds': ['CONSTRAINT'],
            'confidence': 0.9, 'decision_summary': 'An independently placed original drawing states a dimensional limit.', 'reason_codes': ['original_figure']}
            for i,image in enumerate(packet['images'])]}]}
        bound = contract.bind_image_items(value, self.document_map, self.baseline, table_previews=self.tables)
        self.assertEqual(bound['scopes'][0]['packet_sha256'], packet['packet_sha256'])
        baseline, _, _ = compile_source_item_images(self.document_map, bound['scopes'], previous_baseline=self.baseline, table_previews=self.tables)
        self.assertEqual(sum(bool(r.image_bindings) for r in baseline.records), 2)
        self.assertEqual(len(baseline.records), len(self.baseline.records) + 2)
        bad = deepcopy(value); bad['scopes'][0]['items'][0]['image_anchor_ids'] = [packet['table_image_relations'][0]['image_binding']['anchor_id']]
        with self.assertRaisesRegex(ValueError, 'table-owned'):
            contract.bind_image_items(bad, self.document_map, self.baseline, table_previews=self.tables)


if __name__ == '__main__': unittest.main()
