"""Versioned feedback loop checks using invented source, never customer answers."""
from argparse import Namespace
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from test_portable import artifact_tools, item_decisions, make_fixture, write
import revision_tools as loop


def args(**values):
    fields = dict(review_dir=None, baseline_dir=None, source=None, annotations=None, decomposition=None,
                  assessments=None, analysis_packet=None, knowledge_catalog=None, source_item_id=None,
                  output_dir=None, input=None)
    fields.update(values)
    return Namespace(**fields)


class ReviewLoopTests(unittest.TestCase):
    def current_delivery(self,name='current-export'):
        output=self.root/name
        artifact_tools.deliver(args(baseline_dir=self.review/'versions/initial',source=self.source,output_dir=output))
        return output

    def test_first_current_delivery_attaches_and_verifies_without_a_candidate(self):
        delivery=self.current_delivery();before={p.name:p.read_bytes() for p in delivery.iterdir() if p.is_file()}
        initial=loop.load_head(self.review)
        request=args(review_dir=self.review,delivery_dir=delivery,expected_review_sha256=initial['review_sha256'])
        attached=loop.attach_current_delivery(request)
        self.assertEqual(attached['status'],'CURRENT_DELIVERY_ATTACHED')
        self.assertFalse(attached['business_revision_created'])
        self.assertEqual(loop.load_head(self.review)['current'],initial['current'])
        self.assertEqual([p.name for p in (self.review/'versions').iterdir()],['initial'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in delivery.iterdir() if p.is_file()})
        owned=self.review/attached['delivery_path']
        self.assertEqual(before,{p.name:p.read_bytes() for p in owned.iterdir() if p.is_file()})
        replay=loop.attach_current_delivery(request)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(replay['review_sha256'],attached['review_sha256'])
        verified=loop.verify_review(args(review_dir=self.review))
        self.assertEqual(verified['status'],'REVIEW_VERIFIED')
        self.assertEqual(len(verified['version_chain']),2) # Delivery metadata only; one unchanged business bundle.
        self.assertEqual(verified['human_acceptance'],'NOT_CLAIMED')
        loop.feedback_review(args(review_dir=self.review,input=self.put('later-feedback.json',{
            'expected_review_sha256':attached['review_sha256'],'feedback':[{'origin':'AGENT_SELF_REVIEW','source_item_ids':[],
                'text':'A later source question needs review.'}]})))
        with self.assertRaisesRegex(ValueError,'Review changed'):
            loop.attach_current_delivery(request)

    def test_current_delivery_attach_rejects_wrong_snapshot_and_workbook_pin(self):
        from openpyxl import load_workbook
        delivery=self.current_delivery()
        snapshot=delivery/'matrix-snapshot.json';value=json.loads(snapshot.read_text());value['document_title']='Not the current draft'
        write(snapshot,value)
        with self.assertRaisesRegex(ValueError,'Delivery differs'):
            loop.attach_current_delivery(args(review_dir=self.review,delivery_dir=delivery,expected_review_sha256=self.initial['review_sha256']))
        self.assertNotIn('delivery',loop.load_head(self.review))
        second=self.current_delivery('wrong-workbook-pin');workbook=load_workbook(second/'matrix.xlsx')
        metadata=workbook['_ReqPilotXMeta']
        for row in metadata:
            if row[0].value=='SnapshotSHA256':row[1].value='0'*64
        workbook.save(second/'matrix.xlsx');workbook.close()
        receipt=loop.read(second/'delivery-receipt.json');receipt['xlsx_sha256']=loop.digest_file(second/'matrix.xlsx');write(second/'delivery-receipt.json',receipt)
        with self.assertRaisesRegex(ValueError,'Workbook snapshot metadata'):
            loop.attach_current_delivery(args(review_dir=self.review,delivery_dir=second,expected_review_sha256=self.initial['review_sha256']))
        self.assertNotIn('delivery',loop.load_head(self.review))

    def test_current_delivery_attach_rejects_stale_review_even_if_business_bytes_are_same(self):
        delivery=self.current_delivery()
        loop.feedback_review(args(review_dir=self.review,input=self.put('new-feedback.json',{
            'expected_review_sha256':self.initial['review_sha256'],
            'feedback':[{'origin':'AGENT_SELF_REVIEW','source_item_ids':[],'text':'A newly observed source question remains open.'}]})))
        with self.assertRaisesRegex(ValueError,'Review changed'):
            loop.attach_current_delivery(args(review_dir=self.review,delivery_dir=delivery,expected_review_sha256=self.initial['review_sha256']))
        self.assertNotIn('delivery',loop.load_head(self.review))

    def test_real_revision_close_after_first_delivery_registration_keeps_history_valid(self):
        delivery=self.current_delivery()
        attached=loop.attach_current_delivery(args(review_dir=self.review,delivery_dir=delivery,expected_review_sha256=self.initial['review_sha256']))
        candidate,_=self.revise(attached)
        exported=self.export(candidate)
        closed=loop.close_review(args(review_dir=self.review,input=self.put('close-after-first.json',{
            'expected_review_sha256':attached['review_sha256'],'candidate_path':str(candidate.relative_to(self.review)),
            'delivery_path':str(exported.relative_to(self.review))})))
        self.assertEqual(closed['status'],'REVIEW_ADVANCED')
        self.assertNotEqual(closed['baseline_sha256'],self.initial['baseline_sha256'])
        self.assertNotIn('delivery_attachment',loop.load_head(self.review))
        verified=loop.verify_review(args(review_dir=self.review))
        self.assertEqual(verified['status'],'REVIEW_VERIFIED')
        self.assertEqual(len(verified['version_chain']),3)
        self.assertTrue((self.review/attached['delivery_path']/'matrix.xlsx').is_file())

    def test_cli_reports_partial_authorized_scope_and_preserves_keep_reason(self):
        ids = [r.source_record_id for r in self.baseline.records if r.requirementness.value == 'REQUIREMENT']
        value = {'schema_version': 'reqpilot-decomposition-decisions/v1', 'baseline_sha256': self.baseline.artifact_sha256,
            'authorization': {'request': 'Review this whole authorized scope.', 'source_item_ids': ids},
            'decisions': [{'source_item_id': ids[0], 'action': 'KEEP', 'reason': 'Retain this complete operating condition.'}]}
        output = self.root / 'partial-decisions'
        result = artifact_tools.decomposition_operation(args(command='compile-decomposition', baseline_dir=self.baseline_dir,
            input=self.put('partial-input.json', value), output_dir=output))
        saved = json.loads((output/'source-item-decomposition.json').read_text())
        self.assertEqual(result['scope_status'], 'PARTIAL')
        self.assertEqual(result['review_scope']['remaining_count'], len(ids)-1)
        self.assertEqual(saved['dispositions'][ids[0]]['reason'], value['decisions'][0]['reason'])

    def test_explicit_analysis_policy_import_and_versioned_revision_preserve_source_records(self):
        policy=self.put('policy.json',{'source_sha256':self.baseline.source_sha256,'status':'SKIPPED','reason':'NO_INTERNAL_EVIDENCE'})
        imported=self.root/'policy-review'
        result=loop.open_review(args(review_dir=imported,baseline_dir=self.baseline_dir,source=self.source,analysis_policy=policy))
        self.assertEqual(result['analysis_policy_status'],'SKIPPED')
        self.assertTrue((imported/'versions/initial/analysis-policy.json').is_file())
        self.assertFalse((self.review/'versions/initial/analysis-policy.json').exists())
        value={'schema_version':'reqpilot-review-revision/v1','expected_review_sha256':self.initial['review_sha256'],'expected_baseline_sha256':self.initial['baseline_sha256']}
        candidate=self.review/'versions/policy-candidate'
        loop.revise_review(args(review_dir=self.review,input=self.put('policy-revision.json',value),output_dir=candidate,analysis_policy=policy))
        self.assertEqual(loop.bundle(candidate)[1],self.baseline)
        exported=self.export(candidate)
        snapshot=json.loads((exported/'matrix-snapshot.json').read_text())
        self.assertEqual(snapshot['source_item_baseline']['analysis_policy']['status'],'SKIPPED')
        self.assertFalse((self.review/'versions/initial/analysis-policy.json').exists())
        close={'expected_review_sha256':self.initial['review_sha256'],'candidate_path':str(candidate.relative_to(self.review)),'delivery_path':str(exported.relative_to(self.review))}
        loop.close_review(args(review_dir=self.review,input=self.put('policy-close.json',close)))
        self.assertEqual(loop.verify_review(args(review_dir=self.review))['status'],'REVIEW_VERIFIED')

    def test_direct_export_uses_explicit_policy_and_rejects_a_different_source(self):
        policy=self.put('direct-policy.json',{'source_sha256':self.baseline.source_sha256,'status':'SKIPPED','reason':'USER_SKIPPED'})
        result=artifact_tools.deliver(args(baseline_dir=self.baseline_dir,source=self.source,output_dir=self.root/'direct-policy-export',analysis_policy=policy))
        self.assertEqual(result['analysis_policy_status'],'SKIPPED')
        bad=self.put('wrong-policy.json',{'source_sha256':'0'*64,'status':'SKIPPED','reason':'USER_SKIPPED'})
        with self.assertRaisesRegex(ValueError,'Analysis policy must bind'):
            artifact_tools.deliver(args(baseline_dir=self.baseline_dir,source=self.source,output_dir=self.root/'bad-policy-export',analysis_policy=bad))
        self.assertFalse((self.root/'bad-policy-export').exists())

    def test_explicit_policy_reason_code_alias_is_normalized_without_mutating_input(self):
        value={'source_sha256':self.baseline.source_sha256,'status':'SKIPPED','reason_code':'NO_INTERNAL_EVIDENCE','reason':'No internal evidence was supplied.','human_acceptance':'NOT_CLAIMED','compliance_claims':0}
        before=deepcopy(value)
        policy=artifact_tools.analysis_policy_input(value,self.baseline.source_sha256)
        self.assertEqual(policy['reason'],'NO_INTERNAL_EVIDENCE')
        self.assertEqual(policy['note'],'No internal evidence was supplied.')
        self.assertEqual(value,before)
        with self.assertRaisesRegex(ValueError,'conflict'):
            artifact_tools.analysis_policy_input({**value,'reason':'USER_SKIPPED'},self.baseline.source_sha256)
        for field in ('status','reason','reason_code','note'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,'must be text'):
                artifact_tools.analysis_policy_input({**value,field:[]},self.baseline.source_sha256)
        with self.assertRaisesRegex(ValueError,'baseline differs'):
            artifact_tools.analysis_policy_input({**value,'baseline_sha256':'0'*64},self.baseline.source_sha256,self.baseline.artifact_sha256)
        bound=artifact_tools.analysis_policy_input({**value,'baseline_sha256':self.baseline.artifact_sha256,'note':'Source-only draft.'},self.baseline.source_sha256,self.baseline.artifact_sha256)
        self.assertIn('Source-only draft.',bound['note'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reqpilot-review-unit-")
        self.root = Path(self.temp.name)
        self.source, capture, map_input = make_fixture(self.root)
        write(self.root / "map.json", map_input)
        self.map_dir = self.root / "map"
        artifact_tools.compile_map(args(source=self.source, source_ir=capture / "source_ir.json", input=self.root / "map.json", output_dir=self.map_dir))
        self.baseline_dir = self.root / "baseline"
        write(self.root / "items.json", item_decisions(self.map_dir))
        artifact_tools.compile_items(args(map_dir=self.map_dir, input=self.root / "items.json", output_dir=self.baseline_dir))
        self.review = self.root / "review"
        self.initial = loop.open_review(args(review_dir=self.review, baseline_dir=self.baseline_dir, source=self.source))
        self.baseline = loop.bundle(self.baseline_dir)[1]
        self.sid = self.baseline.records[0].source_record_id

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, value):
        path = self.root / name; write(path, value); return path

    def feedback(self):
        path = self.put("feedback.json", {"expected_review_sha256": self.initial["review_sha256"], "feedback": [{"origin": "AGENT_SELF_REVIEW", "source_item_ids": [self.sid], "text": "The complete response target also restricts the operating supply; read the original and review its class."}]})
        result = loop.feedback_review(args(review_dir=self.review, input=path))
        return result, result["added_feedback_ids"][0]

    def revise(self, head=None, feedback_id=None):
        head = head or self.initial
        decisions = [{"source_record_id": self.sid, "requirementness": "REQUIREMENT", "requirement_kinds": ["CONSTRAINT", "QUALITY"], "confidence": 0.9, "decision_summary": "The complete target includes response timing and a specified operating supply.", "reason_codes": ["complete_target"]}]
        value = {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": head["review_sha256"], "expected_baseline_sha256": head["baseline_sha256"], "model_id": "test-explicit-decisions", "decisions": decisions, "addressed_feedback_ids": [feedback_id] if feedback_id else []}
        output = self.review / "versions/candidate"
        result = loop.revise_review(args(review_dir=self.review, input=self.put("revision.json", value), output_dir=output))
        return output, result

    def export(self, candidate):
        output = self.review / "deliveries/new"
        artifact_tools.deliver(args(baseline_dir=candidate, source=self.source, output_dir=output))
        return output

    def test_feedback_revision_export_preserves_original_and_unaffected_records(self):
        head, feedback_id = self.feedback()
        packet = loop.packet_review(args(review_dir=self.review, source_item_id=[self.sid]))
        self.assertEqual(packet["feedback"][0]["origin"], "AGENT_SELF_REVIEW")
        self.assertEqual(len(packet["records"]), 1)
        self.assertTrue(packet["map_nodes"][0]["source_content"])
        candidate, result = self.revise(head, feedback_id)
        after = loop.bundle(candidate)[1]
        self.assertEqual(self.baseline.records[1:], after.records[1:])
        self.assertEqual(self.baseline.records[0].text_bindings, after.records[0].text_bindings)
        self.assertEqual(result["changed_source_item_count"], 1)
        output = self.export(candidate)
        close_input = {"expected_review_sha256": head["review_sha256"], "candidate_path": str(candidate.relative_to(self.review)), "delivery_path": str(output.relative_to(self.review)), "resolutions": [{"feedback_id": feedback_id, "disposition": "ADDRESSED", "reason": "The actual source-bound classification and its exported row were updated."}]}
        result = loop.close_review(args(review_dir=self.review, input=self.put("close.json", close_input)))
        current = loop.load_head(self.review)
        self.assertEqual(result["status"], "REVIEW_ADVANCED")
        self.assertEqual(current["feedback"][0]["status"], "ADDRESSED")
        self.assertEqual(current["human_acceptance"], "NOT_CLAIMED")
        self.assertTrue((self.review / "reviews" / (head["review_sha256"] + ".json")).is_file())
        self.assertEqual(loop.bundle(self.review / "versions/initial")[1], self.baseline)
        verified = loop.verify_review(args(review_dir=self.review))
        self.assertEqual(verified["status"], "REVIEW_VERIFIED")
        self.assertEqual(len(verified["version_chain"]), 3)
        self.assertEqual(verified["current"]["baseline_sha256"], after.artifact_sha256)
        history_path = self.review / "reviews" / (head["review_sha256"] + ".json")
        broken = json.loads(history_path.read_text()); broken["feedback"][0]["origin"] = "USER"; write(history_path, broken)
        with self.assertRaisesRegex(ValueError, "history integrity"):
            loop.verify_review(args(review_dir=self.review))

    def test_arriving_feedback_rejects_old_candidate_without_losing_new_feedback(self):
        candidate, _ = self.revise()
        output = self.export(candidate)
        head, fid = self.feedback()
        value = {"expected_review_sha256": self.initial["review_sha256"], "candidate_path": str(candidate.relative_to(self.review)), "delivery_path": str(output.relative_to(self.review))}
        with self.assertRaisesRegex(ValueError, "Review changed"):
            loop.close_review(args(review_dir=self.review, input=self.put("close.json", value)))
        self.assertEqual(loop.load_head(self.review)["feedback"][0]["feedback_id"], fid)
        self.assertEqual(loop.load_head(self.review)["current"]["baseline_sha256"], self.baseline.artifact_sha256)

    def test_manifest_and_original_tampering_are_rejected(self):
        manifest = loop.load_head(self.review)
        source = self.review / manifest["source"]["path"]
        source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Original source bytes changed"):
            loop.packet_review(args(review_dir=self.review))

    def test_current_artifact_tampering_is_rejected(self):
        path = self.review / "versions/initial/table-previews.json"
        tables = json.loads(path.read_text()); tables.clear(); write(path, tables)
        with self.assertRaisesRegex(ValueError, "Current version artifact changed"):
            loop.packet_review(args(review_dir=self.review))

    def test_candidate_tampering_and_forged_feedback_resolution_are_rejected(self):
        head, fid = self.feedback(); candidate, _ = self.revise(head)
        output = self.export(candidate)
        value = {"expected_review_sha256": head["review_sha256"], "candidate_path": str(candidate.relative_to(self.review)), "delivery_path": str(output.relative_to(self.review)), "resolutions": [{"feedback_id": fid, "disposition": "ADDRESSED", "reason": "Claim without bound revision"}]}
        with self.assertRaisesRegex(ValueError, "bound to the compiled revision"):
            loop.close_review(args(review_dir=self.review, input=self.put("close.json", value)))
        path = candidate / "revision-diff.json"; diff = json.loads(path.read_text()); diff["changed_source_item_count"] = 0; write(path, diff)
        with self.assertRaisesRegex(ValueError, "Candidate changed"):
            loop.close_review(args(review_dir=self.review, input=self.put("close.json", value)))

    def test_original_path_escape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "inside the review workspace"):
            loop.inside(self.review, "../synthetic.pdf")

    def test_analysis_carries_unchanged_units_and_marks_changed_unit_stale(self):
        packet_dir = self.root / "analysis"
        artifact_tools.analysis_operation(args(command="analysis-packet", baseline_dir=self.baseline_dir, unit_id=None, output_dir=packet_dir))
        packet = json.loads((packet_dir / "analysis-packet.json").read_text())
        decisions = {"decisions": [{"unit_id": unit["unit_id"], "decision": "PENDING_EVIDENCE", "reason": "The product's response has no supplied verification result.", "evidence": [], "clarification_question": "Provide product evidence for the specified conditions."} for unit in packet["units"]]}
        tampered = deepcopy(packet)
        tampered["units"][0]["source_segments"][0]["text"] = "This was never in the original source."
        unit = tampered["units"][0]
        tampered["unit_registry"][unit["unit_id"]]["unit_sha256"] = loop.core.canonical_json_sha256(unit)
        tampered["packet_sha256"] = loop.core.canonical_json_sha256({k: v for k, v in tampered.items() if k != "packet_sha256"})
        write(packet_dir / "analysis-packet.json", tampered)
        with self.assertRaisesRegex(ValueError, "actual current source"):
            artifact_tools.analysis_operation(args(command="compile-assessments", analysis_packet=packet_dir / "analysis-packet.json", input=self.put("assessments.json", decisions), previous_assessments=None, output_dir=self.root / "tampered-assessments"))
        write(packet_dir / "analysis-packet.json", packet)
        assessment_dir = self.root / "assessments"
        artifact_tools.analysis_operation(args(command="compile-assessments", analysis_packet=packet_dir / "analysis-packet.json", input=self.put("assessments.json", decisions), previous_assessments=None, output_dir=assessment_dir))
        self.review = self.root / "review-with-analysis"
        self.initial = loop.open_review(args(review_dir=self.review, baseline_dir=self.baseline_dir, source=self.source, assessments=assessment_dir / "source-item-assessments.json", analysis_packet=assessment_dir / "analysis-packet.json"))
        inherited = self.review / self.initial['current_version_path']
        exported = self.export(inherited)
        actual = json.loads((exported / 'matrix-snapshot.json').read_text())
        self.assertEqual(sum(bool(n.get('ai_assessment')) for n in actual['nodes']), len(decisions['decisions']))
        packet_file = inherited / 'analysis-packet.json'
        packet_bytes = packet_file.read_bytes(); packet_file.unlink()
        try:
            with self.assertRaises(FileNotFoundError):
                artifact_tools.deliver(args(baseline_dir=inherited, source=self.source, output_dir=self.root / 'missing-inherited-packet'))
            self.assertFalse((self.root / 'missing-inherited-packet').exists())
        finally:
            packet_file.write_bytes(packet_bytes)
        candidate, result = self.revise()
        self.assertEqual(result["carry_forward"]["stale_unit_ids"], [self.sid])
        carried = json.loads((candidate / "source-item-assessments.json").read_text())
        self.assertEqual(len(carried["records"]), len(decisions["decisions"]) - 1)
        self.assertNotIn(self.sid, carried["records"])
        fresh_packet_dir = self.review / "analysis/refresh"
        artifact_tools.analysis_operation(args(command="analysis-packet", baseline_dir=candidate, unit_id=[self.sid], output_dir=fresh_packet_dir))
        revised_decision = {"decisions": [next(item for item in decisions["decisions"] if item["unit_id"] == self.sid)]}
        fresh_dir = self.review / "assessments/refresh"
        artifact_tools.analysis_operation(args(command="compile-assessments", analysis_packet=fresh_packet_dir / "analysis-packet.json", input=self.put("new-assessment.json", revised_decision), previous_assessments=candidate / "source-item-assessments.json", output_dir=fresh_dir))
        fresh = json.loads((fresh_dir / "source-item-assessments.json").read_text())
        self.assertEqual(len(fresh["records"]), len(decisions["decisions"]))
        delivery = self.review / "delivery/refreshed"
        artifact_tools.deliver(args(baseline_dir=candidate, source=self.source, assessments=fresh_dir / "source-item-assessments.json", output_dir=delivery))
        close_input = {"expected_review_sha256": self.initial["review_sha256"], "candidate_path": str(candidate.relative_to(self.review)), "delivery_path": str(delivery.relative_to(self.review))}
        with self.assertRaisesRegex(ValueError, "omits or changes"):
            loop.close_review(args(review_dir=self.review, input=self.put("close.json", close_input)))
        result = loop.close_review(args(review_dir=self.review, input=self.put("close.json", close_input), assessments=fresh_dir / "source-item-assessments.json", analysis_packet=fresh_dir / "analysis-packet.json"))
        self.assertEqual(result["status"], "REVIEW_ADVANCED")
        self.assertNotEqual(result["current_version_path"], str(candidate.relative_to(self.review)))
        self.assertEqual(len(json.loads((candidate / "source-item-assessments.json").read_text())["records"]), len(decisions["decisions"]) - 1,
                         "Composing fresh analysis must not overwrite the immutable candidate")

    def test_visual_reading_attaches_to_existing_table_src_without_duplicate_source(self):
        document_map, previous, _, _ = loop.bundle(self.baseline_dir)
        record = next(r for r in previous.records if r.table_fragments)
        node = next(n for n in document_map.nodes if n.node_id == record.node_id)
        anchors = [a.anchor_id for a in node.source_anchors if a.anchor_kind in {"IMAGE", "FIGURE"}]
        value = {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": self.initial["review_sha256"], "expected_baseline_sha256": previous.artifact_sha256,
                 "visual_readings": [{"source_item_id": record.source_record_id, "image_anchor_ids": [anchors[0]], "text": "The visible sketch marks a width upper limit; verify how it applies to this test.",
                    "association_reason": "Explicit synthetic same-section context association for this test.", "unresolved": ["Applicability needs source-driven review."]}]}
        candidate = self.review / "versions/visual"
        result = loop.revise_review(args(review_dir=self.review, input=self.put("visual.json", value), output_dir=candidate))
        after = loop.bundle(candidate)[1]
        self.assertEqual(previous, after)
        self.assertEqual(result["changed_source_item_count"], 1)
        annotations = json.loads((candidate / "image-annotations.json").read_text())
        self.assertEqual(annotations["records"][record.source_record_id]["visual_interpretation"]["authority"], "AGENT_VISUAL_INTERPRETATION")
        self.assertEqual(annotations["records"][record.source_record_id]["visual_interpretation"]["unresolved"], value["visual_readings"][0]["unresolved"])

    def test_original_heading_number_has_exact_owner_proof_and_survives_revision(self):
        from app.application.agent_workflow.source_item_visual import compile_source_item_annotation_updates, validate_source_item_number_evidence, rebind_source_item_image_annotations
        document_map, baseline, _, tables = loop.bundle(self.baseline_dir)
        record = baseline.records[0]; node = next(n for n in document_map.nodes if n.node_id == record.node_id)
        content = next(c for c in node.source_content if c.is_title)
        update = {"source_item_id": record.source_record_id, "source_number": node.label, "source_number_ref": {"kind": "MAP_HEADING", "content_id": content.content_id, "start_offset": 0, "end_offset": len(node.label)},
            "topics": ["VERIFICATION"], "reason": "The first directly owned provision uses this exact original heading number."}
        annotations = compile_source_item_annotation_updates(document_map, baseline, [update], table_previews=tables)
        evidence = annotations["records"][record.source_record_id]["source_number_evidence"]
        validate_source_item_number_evidence(document_map, baseline, record.source_record_id, node.label, evidence, table_previews=tables)
        candidate, _ = self.revise(); revised = loop.bundle(candidate)[1]
        rebound = rebind_source_item_image_annotations(document_map, revised, annotations)
        self.assertEqual(rebound["records"][record.source_record_id]["source_number_evidence"], evidence)
        self.assertEqual(rebound["baseline_sha256"], revised.artifact_sha256)
        altered = deepcopy(evidence); altered["reference"]["content_id"] = document_map.nodes[0].source_content[0].content_id
        with self.assertRaisesRegex(ValueError, "actual Map node"):
            validate_source_item_number_evidence(document_map, baseline, record.source_record_id, node.label, altered, table_previews=tables)

    def test_source_number_rejects_arbitrary_numeric_value_inside_bound_prose(self):
        from app.application.agent_workflow.source_item_visual import compile_source_item_annotation_updates
        document_map, baseline, _, tables = loop.bundle(self.baseline_dir)
        record = baseline.records[0]; binding = record.text_bindings[0]; start = binding.start_offset + binding.text.index("30")
        update = {"source_item_id": record.source_record_id, "source_number": "30", "source_number_ref": {"kind": "TEXT", "content_id": binding.content_id, "start_offset": start, "end_offset": start + 2}, "reason": "Invalid attempt to call a timing value an original number."}
        with self.assertRaisesRegex(ValueError, "arbitrary value in prose"):
            compile_source_item_annotation_updates(document_map, baseline, [update], table_previews=tables)
        first = {"source_item_id": baseline.records[1].source_record_id, "topics": ["VERIFICATION"], "reason": "Explicit topic update before the invalid original-number claim."}
        with self.assertRaises(ValueError) as failed:
            compile_source_item_annotation_updates(document_map, baseline, [first, update], table_previews=tables)
        self.assertIn(f"annotation_updates[1] source_item_id={record.source_record_id}.source_number_ref:", str(failed.exception))
        self.assertIn("arbitrary value in prose", str(failed.exception))

    def test_number_evidence_uses_the_source_item_boundary_not_parser_block_start(self):
        from app.application.agent_workflow.source_item_visual import compile_source_item_annotation_updates
        raw, capture, mapping = make_fixture(self.root / "numbered", numbered_targets=True)
        map_dir = self.root / "numbered-map"
        artifact_tools.compile_map(args(source=raw, source_ir=capture / "source_ir.json", input=self.put("numbered-map.json", mapping), output_dir=map_dir))
        decisions = item_decisions(map_dir)
        scope = next(item for item in decisions["text_decisions"] if item["scope_id"] == "text:s1")
        first = deepcopy(scope["items"][0]); second = deepcopy(first)
        content = next(item for item in json.loads((map_dir / "scope-requests.json").read_text())["text_requests"] if item["scope_id"] == "text:s1")["source_content"]
        body = next(item for item in content if not item["is_title"])
        start = body["text"].index("1.2")
        first.update(item_key="opening"); second.update(item_key="closing")
        first["text_spans"][0]["end_offset"] = start; second["text_spans"][0]["start_offset"] = start
        scope["items"] = [first, second]
        directory = self.root / "numbered-baseline"
        artifact_tools.compile_items(args(map_dir=map_dir, input=self.put("numbered-items.json", decisions), output_dir=directory))
        document_map, baseline, _, tables = loop.bundle(directory)
        later = next(record for record in baseline.records if record.text_bindings and record.text_bindings[0].text.startswith("1.2"))
        annotated = compile_source_item_annotation_updates(document_map, baseline, [{"source_item_id": later.source_record_id, "source_number": "1.2",
            "source_number_ref": {"kind": "TEXT", "content_id": body["content_id"], "start_offset": start, "end_offset": start + 3}, "reason": "Exact leading number of this separately compiled original clause."}], table_previews=tables)
        self.assertEqual(annotated["records"][later.source_record_id]["source_number"], "1.2")

    def test_multiple_original_numbers_require_explicit_owned_clause_extents(self):
        from app.application.agent_workflow.source_item_visual import compile_source_item_annotation_updates, validate_source_item_numbers
        raw, capture, mapping = make_fixture(self.root / "multi-numbered", numbered_targets=True)
        map_dir = self.root / "multi-map"
        artifact_tools.compile_map(args(source=raw, source_ir=capture / "source_ir.json", input=self.put("multi-map.json", mapping), output_dir=map_dir))
        directory = self.root / "multi-baseline"
        artifact_tools.compile_items(args(map_dir=map_dir, input=self.put("multi-items.json", item_decisions(map_dir)), output_dir=directory))
        document_map, baseline, _, tables = loop.bundle(directory)
        parent = baseline.records[0]; binding = parent.text_bindings[0]
        second = binding.start_offset + binding.text.index("1.2")
        declarations = []
        for number, start, end in (("1.1", binding.start_offset, second), ("1.2", second, binding.end_offset)):
            declarations.append({"value": number, "source_number_ref": {"kind": "TEXT", "content_id": binding.content_id, "start_offset": start, "end_offset": start + len(number)},
                "source_extent": [{"kind": "TEXT", "content_id": binding.content_id, "start_offset": start, "end_offset": end}]})
        update = {"source_item_id": parent.source_record_id, "source_numbers": declarations, "reason": "Two explicit original clauses are retained in this complete source parent."}
        compiled = compile_source_item_annotation_updates(document_map, baseline, [update], table_previews=tables)
        annotation = compiled["records"][parent.source_record_id]
        verified = validate_source_item_numbers(document_map, baseline, parent.source_record_id, annotation, table_previews=tables)
        self.assertEqual([item["value"] for item in verified], ["1.1", "1.2"])
        self.assertEqual(annotation["source_number"], "")
        self.assertEqual(verified[1]["evidence"]["source_extent"][0]["text"], binding.text[second:])
        unsupported = deepcopy(update); unsupported["source_numbers"][1].pop("source_extent")
        with self.assertRaisesRegex(ValueError, "leading numbering position"):
            compile_source_item_annotation_updates(document_map, baseline, [unsupported], table_previews=tables)
        escaped = deepcopy(update); escaped["source_numbers"][0]["source_extent"][0]["end_offset"] = binding.end_offset + 1
        with self.assertRaisesRegex(ValueError, "escapes the parent's actual"):
            compile_source_item_annotation_updates(document_map, baseline, [escaped], table_previews=tables)

    def test_annotation_revision_preserves_visual_pixels_and_changes_no_source_records(self):
        from app.application.agent_workflow.source_item_visual import compile_source_item_visual_readings, compile_source_item_annotation_updates, rebind_source_item_image_annotations
        document_map, baseline, _, tables = loop.bundle(self.baseline_dir)
        table_record = next(r for r in baseline.records if r.table_fragments); node = next(n for n in document_map.nodes if n.node_id == table_record.node_id)
        anchor = next(a for a in node.source_anchors if a.anchor_kind in {"IMAGE", "FIGURE"})
        old = compile_source_item_visual_readings(document_map, baseline, [{"source_item_id": table_record.source_record_id, "image_anchor_ids": [anchor.anchor_id], "text": "Existing source-bound visual reading.", "association_reason": "Existing same-node graphical context."}])
        updated = compile_source_item_annotation_updates(document_map, baseline, [{"source_item_id": self.sid, "topics": ["VERIFICATION"], "reason": "Explicit declared verification topic after source review."}], base_annotations=old, table_previews=tables)
        rebound = rebind_source_item_image_annotations(document_map, baseline, updated)
        self.assertEqual(old["records"][table_record.source_record_id], rebound["records"][table_record.source_record_id])
        self.assertEqual(updated["schema_version"], old["schema_version"])

    def test_image_packet_contains_readable_original_anchor_payloads(self):
        import pymupdf
        output = self.root / "image-evidence"
        result = artifact_tools.image_operation(args(command="image-packet", map_dir=self.map_dir, baseline_dir=self.baseline_dir, source=self.source, node_id=["s6"], output_dir=output))
        self.assertEqual(result["rendered_source_images"], 2)
        packet = json.loads((output / "image-packets.json").read_text())
        with pymupdf.open(self.source) as pdf:
            originals = {pdf.extract_image(item["xref"])["image"] for item in pdf[0].get_image_info(xrefs=True)}
        for image in packet["source_image_renders"]:
            self.assertEqual((image["width"], image["height"]), (160, 80))
            self.assertIn((output / image["path"]).read_bytes(), originals)

    def test_map_and_scope_recover_a_hidden_original_clause_then_export_and_replay(self):
        original = json.loads((self.root / "map.json").read_text())
        wrong = deepcopy(original)
        construction = next(node for node in wrong["nodes"] if node["node_id"] == "s4")
        body_ids = construction["content_source_ids"]
        construction["content_source_ids"] = []
        wrong["nodes"].append({"node_id": "misread-clause", "parent_id": "s4", "kind": "SECTION",
            "label": "", "title": "The housing shall use aluminium.", "title_source_ids": body_ids, "content_source_ids": []})
        wrong_map, wrong_base = self.root / "hidden-map", self.root / "hidden-base"
        artifact_tools.compile_map(args(source=self.source, source_ir=self.root / "capture/source_ir.json",
            input=self.put("hidden-map-input.json", wrong), output_dir=wrong_map))
        artifact_tools.compile_items(args(map_dir=wrong_map, input=self.put("hidden-items.json", item_decisions(wrong_map)), output_dir=wrong_base))
        self.review = self.root / "hidden-review"
        initial = loop.open_review(args(review_dir=self.review, baseline_dir=wrong_base, source=self.source))
        feedback = {"expected_review_sha256": initial["review_sha256"], "feedback": [{"origin": "AGENT_SELF_REVIEW",
            "source_item_ids": [], "text": "The material obligation was misread as a heading; restore its original clause and actual owner."}]}
        head = loop.feedback_review(args(review_dir=self.review, input=self.put("hidden-feedback.json", feedback)))
        candidate_map = self.review / "map-candidates/recovered"
        artifact_tools.compile_map(args(source=self.source, source_ir=self.root / "capture/source_ir.json",
            input=self.put("recovered-map-input.json", original), output_dir=candidate_map))
        selection = next(item for item in item_decisions(candidate_map)["text_decisions"] if item["scope_id"] == "text:s4")
        value = {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": head["review_sha256"],
            "expected_baseline_sha256": head["baseline_sha256"], "addressed_feedback_ids": head["added_feedback_ids"],
            "map_revision": {"candidate_map_path": str(candidate_map.relative_to(self.review))},
            "scope_revision": {"operation": "REPLACE", "affected_scope_ids": ["text:s4"], "text_decisions": [selection]}}
        candidate = self.review / "versions/recovered"
        result = loop.revise_review(args(review_dir=self.review, input=self.put("hidden-revision.json", value), output_dir=candidate))
        before, after = loop.bundle(wrong_base)[1], loop.bundle(candidate)[1]
        self.assertEqual(len(after.records), len(before.records) + 1)
        self.assertEqual(after.predecessor_artifact_sha256, before.artifact_sha256)
        existing_ids = {record.source_record_id for record in before.records}
        recovered = next(record for record in after.records if record.source_record_id not in existing_ids)
        self.assertEqual(recovered.text_bindings[0].text, "The housing shall use aluminium.")
        self.assertEqual(recovered.node_id, "s4")
        self.assertEqual(result["changed_source_item_count"], 1)
        delivery = self.export(candidate)
        close = {"expected_review_sha256": head["review_sha256"], "candidate_path": str(candidate.relative_to(self.review)),
            "delivery_path": str(delivery.relative_to(self.review)), "resolutions": [{"feedback_id": head["added_feedback_ids"][0],
            "disposition": "ADDRESSED", "reason": "Restored original obligation through shared atomic Map and scope compilers."}]}
        loop.close_review(args(review_dir=self.review, input=self.put("hidden-close.json", close)))
        self.assertEqual(loop.verify_review(args(review_dir=self.review))["status"], "REVIEW_VERIFIED")
        self.assertFalse(any(item["status"] == "OPEN" for item in loop.load_head(self.review)["feedback"]))

    def test_scope_form_revision_preserves_source_identity_and_replays_before_advance(self):
        head, fid = self.feedback()
        packet = loop.packet_review(args(review_dir=self.review, source_item_id=[self.sid]))
        template = packet["scope_revision_templates"][0]["candidate_template"]
        template["items"][0]["source_form"] = "PARAGRAPH"
        value = {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": head["review_sha256"], "expected_baseline_sha256": head["baseline_sha256"],
            "addressed_feedback_ids": [fid], "scope_revision": {"operation": "REPLACE", "affected_scope_ids": [template["scope_id"]], "text_decisions": [template]}}
        output = self.review / "versions/form-correction"
        loop.revise_review(args(review_dir=self.review, input=self.put("form.json", value), output_dir=output))
        after = loop.bundle(output)[1]
        self.assertEqual(after.records[0].source_record_id, self.sid)
        self.assertEqual(after.records[0].source_display_id, self.baseline.records[0].source_display_id)
        self.assertEqual(after.records[0].text_bindings, self.baseline.records[0].text_bindings)
        self.assertEqual(after.records[0].source_form.value, "PARAGRAPH")
        self.assertEqual(after.records[1:], self.baseline.records[1:])
        changed_record = after.records[0].model_copy(update={"source_form": loop.core.SourceRecordForm.NUMBERED_CLAUSE})
        draft = after.model_copy(update={"records": (changed_record, *after.records[1:])})
        digest = loop.core.canonical_json_sha256(loop.core._baseline_hash_payload(draft))
        forged = loop.core.SourceRequirementBaselineArtifactV1.model_validate({**draft.model_dump(mode="json"), "artifact_id": "source-requirement-baseline-" + digest[:32], "artifact_sha256": digest})
        document_map, _, _, tables = loop.bundle(output)
        with self.assertRaisesRegex(ValueError, "replaying the actual bound decisions"):
            loop.verify_transition(self.review.resolve(), self.review / "versions/initial", output, document_map, self.baseline, forged, tables)
        delivery = self.export(output)
        close_input = {"expected_review_sha256": head["review_sha256"], "candidate_path": str(output.relative_to(self.review)), "delivery_path": str(delivery.relative_to(self.review))}
        loop.close_review(args(review_dir=self.review, input=self.put("close.json", close_input)))
        self.assertEqual(loop.verify_review(args(review_dir=self.review))["status"], "REVIEW_VERIFIED")

    def test_scope_split_then_undo_preserves_source_coverage_and_retired_numbers(self):
        head, fid = self.feedback()
        packet = loop.packet_review(args(review_dir=self.review, source_item_id=[self.sid]))
        template = packet["scope_revision_templates"][0]["candidate_template"]
        old_item = template["items"][0]; binding = self.baseline.records[0].text_bindings[0]
        split_at = binding.start_offset + binding.text.index("\n") + 1
        first, second = deepcopy(old_item), deepcopy(old_item)
        first.update(item_key="opening", label="Complete opening target")
        second.update(item_key="closing", label="Complete closing target")
        first["text_spans"][0]["end_offset"] = split_at; second["text_spans"][0]["start_offset"] = split_at
        template["items"] = [first, second]
        value = {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": head["review_sha256"], "expected_baseline_sha256": head["baseline_sha256"],
            "addressed_feedback_ids": [fid], "scope_revision": {"operation": "REPLACE", "affected_scope_ids": [template["scope_id"]], "text_decisions": [template]}}
        split_dir = self.review / "versions/split"
        loop.revise_review(args(review_dir=self.review, input=self.put("split.json", value), output_dir=split_dir))
        split = loop.bundle(split_dir)[1]
        self.assertEqual(len(split.records), len(self.baseline.records) + 1)
        self.assertNotIn(self.sid, {record.source_record_id for record in split.records})
        proof = json.loads((split_dir / "scope-revision-receipt.json").read_text())
        self.assertEqual(len(proof["created_identities"]), 2)
        self.assertEqual(proof["tombstoned_identities"][0]["source_record_id"], self.sid)
        self.assertEqual(proof["unaffected_stable_records_before_sha256"], proof["unaffected_stable_records_after_sha256"])
        delivered = self.export(split_dir)
        close_input = {"expected_review_sha256": head["review_sha256"], "candidate_path": str(split_dir.relative_to(self.review)), "delivery_path": str(delivered.relative_to(self.review))}
        advanced = loop.close_review(args(review_dir=self.review, input=self.put("close.json", close_input)))
        undo = {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": advanced["review_sha256"], "expected_baseline_sha256": advanced["baseline_sha256"],
            "addressed_feedback_ids": [fid], "scope_revision": {"operation": "UNDO", "affected_scope_ids": [template["scope_id"]], "undo_target_path": "versions/initial"}}
        undo_dir = self.review / "versions/restored"
        loop.revise_review(args(review_dir=self.review, input=self.put("undo.json", undo), output_dir=undo_dir))
        restored = loop.bundle(undo_dir)[1]
        self.assertEqual(restored.records, self.baseline.records)
        self.assertEqual(restored.source_display_high_watermark, split.source_display_high_watermark)
        diff = json.loads((undo_dir / "revision-diff.json").read_text())
        self.assertEqual(diff["restored_source_item_ids"], [self.sid])
        self.assertEqual(diff["created_source_item_ids"], [])
        self.assertEqual(len(diff["retired_source_item_ids"]), 2)
        final_delivery = self.review / "deliveries/restored"
        artifact_tools.deliver(args(baseline_dir=undo_dir, source=self.source, output_dir=final_delivery))
        final_close = {"expected_review_sha256": advanced["review_sha256"], "candidate_path": str(undo_dir.relative_to(self.review)), "delivery_path": str(final_delivery.relative_to(self.review))}
        loop.close_review(args(review_dir=self.review, input=self.put("undo-close.json", final_close)))
        self.assertEqual(loop.verify_review(args(review_dir=self.review))["status"], "REVIEW_VERIFIED")

    def test_scope_revision_cannot_drop_any_part_of_the_original(self):
        head, fid = self.feedback()
        template = loop.packet_review(args(review_dir=self.review, source_item_id=[self.sid]))["scope_revision_templates"][0]["candidate_template"]
        template["items"][0]["text_spans"][0]["end_offset"] -= 12
        value = {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": head["review_sha256"], "expected_baseline_sha256": head["baseline_sha256"],
            "addressed_feedback_ids": [fid], "scope_revision": {"operation": "REPLACE", "affected_scope_ids": [template["scope_id"]], "text_decisions": [template]}}
        with self.assertRaises(ValueError):
            loop.revise_review(args(review_dir=self.review, input=self.put("source-loss.json", value), output_dir=self.review / "versions/source-loss"))
        self.assertEqual(loop.load_head(self.review)["current"]["baseline_sha256"], self.baseline.artifact_sha256)

    def test_map_feedback_replays_source_decisions_and_preserves_unaffected_derived_items(self):
        from app.application.agent_workflow.source_item_decomposition import build_source_item_decomposition_packet, compile_source_item_decomposition
        original_mapping = json.loads((self.root / "map.json").read_text())
        wrong_mapping = deepcopy(original_mapping)
        next(node for node in wrong_mapping["nodes"] if node["node_id"] == "s4")["parent_id"] = "s2"
        wrong_map = self.root / "wrong-map"
        artifact_tools.compile_map(args(source=self.source, source_ir=self.root / "capture/source_ir.json", input=self.put("wrong-map-input.json", wrong_mapping), output_dir=wrong_map))
        wrong_base = self.root / "wrong-baseline"
        artifact_tools.compile_items(args(map_dir=wrong_map, input=self.put("wrong-items.json", item_decisions(wrong_map)), output_dir=wrong_base))
        _, baseline, _, tables = loop.bundle(wrong_base)
        parent = baseline.records[0]; packet = build_source_item_decomposition_packet(baseline, parent.source_record_id, table_previews=tables)
        text = packet["segments"][0]["text"]; cut = text.index("\n") + 1
        decision = {"source_item_id": parent.source_record_id, "action": "SPLIT", "reason": "Opening and closing are distinct response outcomes in this invented fixture.", "children": [
            {"text": text[:cut].strip(), "source_ranges": [{"segment_index": 0, "start_offset": 0, "end_offset": cut}], "requirement_kinds": ["QUALITY"]},
            {"text": text[cut:].strip(), "source_ranges": [{"segment_index": 0, "start_offset": cut, "end_offset": len(text)}], "requirement_kinds": ["QUALITY"]}]}
        decomposed = compile_source_item_decomposition(baseline, [decision], table_previews=tables)
        self.review = self.root / "map-review"
        self.initial = loop.open_review(args(review_dir=self.review, baseline_dir=wrong_base, source=self.source, decomposition=self.put("map-decomposition.json", decomposed)))
        fid_value = {"expected_review_sha256": self.initial["review_sha256"], "feedback": [{"origin": "AGENT_SELF_REVIEW", "source_item_ids": [], "text": "The Construction source heading is a document-level sibling, not owned by Project delivery."}]}
        head = loop.feedback_review(args(review_dir=self.review, input=self.put("map-feedback.json", fid_value)))
        candidate_map = self.review / "map-candidates/corrected"
        artifact_tools.compile_map(args(source=self.source, source_ir=self.root / "capture/source_ir.json", input=self.put("correct-map.json", original_mapping), output_dir=candidate_map))
        value = {"schema_version": "reqpilot-review-revision/v1", "expected_review_sha256": head["review_sha256"], "expected_baseline_sha256": head["baseline_sha256"],
            "addressed_feedback_ids": head["added_feedback_ids"], "map_revision": {"candidate_map_path": str(candidate_map.relative_to(self.review))}}
        output = self.review / "versions/corrected-map"
        loop.revise_review(args(review_dir=self.review, input=self.put("map-revision.json", value), output_dir=output))
        after_map, after, _, _ = loop.bundle(output)
        self.assertEqual(next(node for node in after_map.nodes if node.node_id == "s4").parent_id, "main")
        self.assertEqual([(record.source_record_id, record.source_display_id, record.requirement_display_id) for record in after.records],
            [(record.source_record_id, record.source_display_id, record.requirement_display_id) for record in baseline.records])
        carried = json.loads((output / "source-item-decomposition.json").read_text())
        self.assertEqual(carried["records"], decomposed["records"])
        delivery = self.review / "deliveries/map-corrected"
        artifact_tools.deliver(args(baseline_dir=output, source=self.source, output_dir=delivery))
        actual = json.loads((delivery / 'matrix-snapshot.json').read_text())
        self.assertEqual(sum(n.get('source_kind') == 'SINGULAR_REQUIREMENT' for n in actual['nodes']), 2)
        close = {"expected_review_sha256": head["review_sha256"], "candidate_path": str(output.relative_to(self.review)), "delivery_path": str(delivery.relative_to(self.review))}
        loop.close_review(args(review_dir=self.review, input=self.put("map-close.json", close)))
        self.assertEqual(loop.verify_review(args(review_dir=self.review))["status"], "REVIEW_VERIFIED")
        corrupted = json.loads((candidate_map / "map-decisions.json").read_text()); corrupted["nodes"][0]["title"] = "Invented heading"
        write(candidate_map / "map-decisions.json", corrupted)
        with self.assertRaisesRegex(ValueError, "Map source inputs changed"):
            loop.verify_review(args(review_dir=self.review))


if __name__ == "__main__":
    unittest.main()
