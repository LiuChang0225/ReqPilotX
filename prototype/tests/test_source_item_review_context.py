from __future__ import annotations
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.application.agent_workflow import source_requirement_baseline as core
from app.application.agent_workflow import source_item_review_context as review
from app.application.agent_workflow.source_item_visual import compile_source_item_visual_readings
from app.application.agent_workflow.source_item_classification_jobs import _bound_review_feedback, _carry_review_feedback
from app.application.agent_workflow.source_requirement_jobs import SourceItemScopeRevisionStartRequestV1
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.errors import ApplicationError
from .test_source_requirement_baseline import _reference_with_text_node, _text_candidate, _text_item, _public_reference, _table_preview, _fragment, _candidate, _record


def fixture(text="Submit a report in 30 days; standard No. 12345."):
    public = _reference_with_text_node(node_id="clause", label="1", title="Original", contents=(("body", text, False),))
    public["document_tree"]["nodes"][-1]["source_anchors"] = [{"anchor_id":"figure", "anchor_kind":"FIGURE", "page":1,
        "bbox":[10,30,100,60], "source_bounds":[0,0,595,842], "precision":"EXACT_IMAGE"}]
    dm = core.compile_document_map_artifact(public)
    request = core.build_source_item_text_scope_request(dm,node_id="clause")
    candidate = _text_candidate(request,items=(_text_item(request,key="body",label="Original",start=0,end=len(text)),))
    base, receipt = core.compile_source_requirement_baseline(document_map=dm,requests=(),candidates=(),text_requests=(request,),text_candidates=(candidate,))
    return dm, base, receipt


def annotation(dm, base, focus=(), context=()):
    r = base.records[0]
    return {"schema_version":review.DECISION_SCHEMA,"source_sha256":base.source_sha256,"baseline_sha256":base.artifact_sha256,
        "document_map_sha256":dm.artifact_sha256,"records":{r.source_record_id:{"source_evidence_sha256":review.review_source_evidence_sha256(r),
            "focus_areas":list(focus),"context_focus_areas":list(context),"focus_reason":"Synthetic source-specific suggestion."}},
        "provenance":{"kind":"SYNTHETIC_TEST","recorded_at":"2026-01-01T00:00:00Z","action_record":"Synthetic test only."}}


class ReviewContextTests(unittest.TestCase):
    def project(self,dm,b,previews=None,**kwargs):
        return review.project_review_context(dm,b,previews or {},project_id="p",document_id="d",**kwargs)

    def test_numbers_standard_ids_and_unreviewed_state_do_not_create_focus_or_attention(self):
        dm,b,_=fixture(); result=self.project(dm,b); item=result["records"][b.records[0].source_record_id]
        self.assertEqual(item["focus_areas"],[])
        self.assertFalse(item["needs_attention"])
        self.assertIsNone(item["changed_from_previous"])
        self.assertEqual(result["history_status"],"UNKNOWN")
        self.assertIn("12345",item["search_text"])

    def test_actual_previous_judgment_changes_and_rationale_only_updates_are_distinct(self):
        dm,b,receipt=fixture();r=b.records[0]
        for kinds,expected in [(["QUALITY"],True),(["CONSTRAINT"],False)]:
            decision=core.SourceRequirementClassificationCoordinationDecisionV1(source_record_id=r.source_record_id,
                requirementness="REQUIREMENT",requirement_kinds=kinds,confidence=0.95,decision_summary="Updated explanation.",reason_codes=["explicit_review"])
            new=core.compile_source_requirement_classification_coordination(previous_artifact=b,previous_validation_receipt=receipt,
                decisions=(decision,),model_id="synthetic")[0]
            item=self.project(dm,new,previous=b)["records"][r.source_record_id]
            self.assertEqual(item["changed_from_previous"],expected)
            self.assertTrue(item["rationale_changed"])
            self.assertEqual(item["previous_judgment"]["decision_summary"],r.decision_summary)

    def test_retained_context_cannot_be_presented_as_a_focus_requirement(self):
        dm,b,receipt=fixture();r=b.records[0]
        decision=core.SourceRequirementClassificationCoordinationDecisionV1(source_record_id=r.source_record_id,
            requirementness="NON_REQUIREMENT",non_requirement_role="REFERENCE",confidence=0.95,
            decision_summary="Reference only.",reason_codes=["reference"])
        new=core.compile_source_requirement_classification_coordination(previous_artifact=b,previous_validation_receipt=receipt,
            decisions=(decision,),model_id="synthetic")[0]
        with self.assertRaisesRegex(ValueError,"non-requirements"):
            self.project(dm,new,annotations=annotation(dm,new,["VERIFICATION"]))
        item=self.project(dm,new,annotations=annotation(dm,new,context=["VERIFICATION"]))["records"][r.source_record_id]
        self.assertEqual(item["focus_areas"],[])
        self.assertEqual(item["context_focus_areas"],["VERIFICATION"])
        self.assertIsNone(item["requirement_display_id"])

    def test_original_owned_table_rows_are_searchable_without_unrelated_rows(self):
        dm=core.compile_document_map_artifact(_public_reference())
        preview=_table_preview(scope_id="lt-bom",title="Components",fragments=(_fragment("bom-fragment","T0001",page=2,
            rows=(("Name","Value"),("Owned","hidden-current-detail"),("Owned2","shared-condition"),("Other","not-this-source"),("Other2","tail"))),))
        request=core.build_source_requirement_scope_request(dm,preview)
        groups=lambda a,b:(core.SourceRequirementRowGroupV1(fragment_id="bom-fragment",row_start=a,row_end=b),)
        candidate=_candidate(request,records=(_record("one","Branch One",core.SourceRecordForm.TABLE_BRANCH,groups(1,3)),
            _record("two","Branch Two",core.SourceRecordForm.TABLE_BRANCH,groups(3,5))),context=(core.SourceRequirementContextRowGroupV1(
                role=core.ContextRowRole.HEADER,row_group=groups(0,1)[0],reason_codes=("header",)),))
        b,_=core.compile_source_requirement_baseline(document_map=dm,requests=(request,),candidates=(candidate,))
        item=self.project(dm,b,{"lt-bom":preview})["records"][b.records[0].source_record_id]
        self.assertIn("hidden-current-detail",item["search_text"])
        self.assertIn("shared-condition",item["search_text"])
        self.assertNotIn("not-this-source",item["search_text"])
        bad=deepcopy(preview);bad["source_sha256"]="b"*64
        with self.assertRaisesRegex(ValueError,"another source"):
            self.project(dm,b,{"lt-bom":bad})

    def test_visual_text_and_explicit_unresolved_are_read_without_compliance_inference(self):
        dm,b,_=fixture();sid=b.records[0].source_record_id
        visual=compile_source_item_visual_readings(dm,b,[{"source_item_id":sid,"image_anchor_ids":["figure"],
            "text":"Figure-only coordinate Z=6789.","association_reason":"Same original node.","unresolved":["Missing legend"]}])
        item=self.project(dm,b,visual_annotations=visual)["records"][sid]
        self.assertIn("Z=6789",item["search_text"])
        self.assertTrue(item["has_visual_interpretation"])
        self.assertTrue(item["needs_attention"])
        self.assertEqual(item["focus_areas"],[])

    def test_feedback_counts_preserve_actual_status_and_reject_cross_version(self):
        dm,b,_=fixture();r=b.records[0]
        def fb(fid,status,project="p"):
            return {"feedback_id":fid,"status":status,"project_id":project,"document_id":"d","source_sha256":b.source_sha256,
                "baseline_artifact_id":b.artifact_id,"document_map_artifact_sha256":dm.artifact_sha256,"source_record_id":r.source_record_id}
        events=[fb("f1","PENDING_AGENT"),fb("f1","CONSUMED"),fb("f2","ACKNOWLEDGED"),fb("f3","PENDING_AGENT","other")]
        item=self.project(dm,b,feedbacks=events)["records"][r.source_record_id]
        self.assertEqual((item["feedback_count"],item["pending_feedback_count"]),(2,1))
        with self.assertRaisesRegex(ValueError,"current source item version"):
            self.project(dm,b,feedbacks=[{**fb("f4","PENDING_AGENT"),"baseline_artifact_id":"stale"}])

    def test_sidecar_is_versioned_and_cannot_mutate_baseline_or_accept_stale_item_evidence(self):
        dm,b,_=fixture();value=annotation(dm,b,["TECHNICAL_PARAMETERS"]);bundle={"document_map":dm,"baseline":b}
        with tempfile.TemporaryDirectory() as directory:
            store=LocalObjectStore(Path(directory));container=SimpleNamespace(object_store=store)
            with patch("app.application.agent_workflow.source_item_delivery_bridge._current",return_value=({},bundle)),patch(
                "app.application.agent_workflow.source_item_delivery_bridge._container",return_value=container):
                first=review.publish_review_annotations("p","d",value)
                self.assertTrue(review.publish_review_annotations("p","d",value)["idempotent"])
                other=deepcopy(value);other["records"][b.records[0].source_record_id]["focus_reason"]="Revised source explanation."
                with self.assertRaisesRegex(ValueError,"actual predecessor"):
                    review.publish_review_annotations("p","d",other)
                second=review.publish_review_annotations("p","d",other,expected_head_sha256=first["artifact_sha256"])
                self.assertEqual(second["revision"],2)
                loaded=review.read_review_annotations(store,"p","d",dm,b)
                self.assertEqual(loaded["predecessor_sha256"],first["artifact_sha256"])
                bad=deepcopy(other);bad["records"][b.records[0].source_record_id]["source_evidence_sha256"]="b"*64
                with self.assertRaisesRegex(ValueError,"source evidence changed"):
                    review.publish_review_annotations("p","d",bad,expected_head_sha256=second["artifact_sha256"])
            self.assertEqual(bundle["baseline"],b)

    def test_existing_scope_revision_still_rejects_tables_and_classification_feedback_is_bound(self):
        dm,b,_=fixture();r=b.records[0]
        with self.assertRaises(ValueError):
            SourceItemScopeRevisionStartRequestV1(command_id="synthetic",expected_source_sha256=b.source_sha256,
                expected_document_map_artifact_sha256=dm.artifact_sha256,expected_baseline_artifact_id=b.artifact_id,
                expected_baseline_artifact_sha256=b.artifact_sha256,expected_baseline_revision=1,affected_scope_ids=["lt-bom"])
        feedback={"feedback_id":"f1","project_id":"p","document_id":"d","source_sha256":b.source_sha256,
            "baseline_artifact_id":b.artifact_id,"document_map_artifact_sha256":dm.artifact_sha256,"source_record_id":r.source_record_id,"status":"PENDING_AGENT"}
        reader="app.application.agent_workflow.source_requirement_reference_bridge._read_feedbacks"
        with patch(reader,return_value=[feedback]):
            self.assertEqual(_bound_review_feedback("p","d",b,{r.source_record_id},["f1"]),[feedback])
            with self.assertRaises(ApplicationError) as error:
                _bound_review_feedback("p","d",b,set(),["f1"])
            self.assertEqual(error.exception.status_code,409)
        with patch(reader,return_value=[feedback,{**feedback,"feedback_id":"f2"}]),patch(
            "app.application.agent_workflow.source_requirement_reference_bridge._carry_feedbacks_to_baseline") as carry:
            _carry_review_feedback("p","d",b,b,{"f1"})
            self.assertEqual([(c.kwargs["status"],c.kwargs["feedback_ids"]) for c in carry.call_args_list],
                             [("CONSUMED",("f1",)),("ACKNOWLEDGED",("f2",))])


if __name__ == "__main__":
    unittest.main()
