import unittest

from .runtime_isolation import activate_test_runtime
activate_test_runtime()
from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow.source_item_ledger import project_source_item_ledger
from .test_source_requirement_baseline import _reference_with_text_node, _text_candidate, _text_item


class UnnumberedParagraphTests(unittest.TestCase):
    def test_plain_prose_retains_its_form_without_inventing_a_source_number_or_list(self):
        text = 'The controller shall report a sensor fault within 20 ms.'
        reference = _reference_with_text_node(node_id='plain', label='', title='Controller behavior', contents=(('body', text, False),))
        document_map = core.compile_document_map_artifact(reference)
        request = core.build_source_item_text_scope_request(document_map, node_id='plain')
        original = _text_item(request, key='paragraph', label='Controller behavior', start=0, end=len(text))
        item = core.SourceItemTextBoundaryCandidateV1.model_validate(original.model_copy(update={'source_form': core.SourceRecordForm.PARAGRAPH}).model_dump(mode='json'))
        candidate = _text_candidate(request, items=(item,))
        baseline, _ = core.compile_source_requirement_baseline(document_map=document_map,
            requests=(), candidates=(), text_requests=(request,), text_candidates=(candidate,))
        record = baseline.records[0]
        self.assertEqual(record.source_form.value, 'PARAGRAPH')
        self.assertEqual(''.join(binding.text for binding in record.text_bindings), text)
        self.assertEqual(next(node for node in document_map.nodes if node.node_id == 'plain').label, '')
        restored = core.SourceRequirementBaselineArtifactV1.model_validate_json(baseline.model_dump_json())
        self.assertEqual(restored.artifact_sha256, baseline.artifact_sha256)
        ledger = project_source_item_ledger(document_map=document_map, artifact=baseline)
        self.assertEqual(len([row for row in ledger['rows'] if row['source_item_id'] == record.source_record_id]), 1)
