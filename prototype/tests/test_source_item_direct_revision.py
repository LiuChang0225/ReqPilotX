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
from app.application.agent_workflow.source_item_direct_revision import compile_direct_revision, load_direct_decomposition
from app.application.agent_workflow.source_item_visual import (
    compile_source_item_visual_readings, rebind_source_item_image_annotations, _seal_annotations,
)
from app.application.agent_workflow.source_requirement_jobs import publish_agent_source_item_revision, _load_product_baseline_artifact
from app.adapters.storage.local_object_store import LocalObjectStore
from .test_source_requirement_baseline import _reference_with_text_node, _text_candidate, _text_item
from .test_source_item_image_jobs import Context


class DirectRevisionTests(unittest.TestCase):
    def setUp(self):
        text = "The device shall meet the drawing."
        reference = _reference_with_text_node(node_id="clause", label="1", title="Drawing", contents=(("body", text, False),))
        reference["document_tree"]["nodes"] = [n for n in reference["document_tree"]["nodes"] if n["node_id"] in {"root", "clause"}]
        reference["logical_tables"] = []
        reference["document_tree"]["nodes"][-1]["source_anchors"] = [{"anchor_id":"figure", "anchor_kind":"FIGURE",
            "page":1,"bbox":[10,30,100,60],"source_bounds":[0,0,595,842],"precision":"EXACT_IMAGE"}]
        self.dm = core.compile_document_map_artifact(reference)
        request = core.build_source_item_text_scope_request(self.dm, node_id="clause")
        candidate = _text_candidate(request, items=(_text_item(request, key="body", label="Drawing", start=0, end=len(text)),))
        self.base,self.receipt = core.compile_source_requirement_baseline(document_map=self.dm, requests=(),candidates=(),text_requests=(request,),text_candidates=(candidate,))
        self.sid = self.base.records[0].source_record_id
        self.decision = {"source_record_id":self.sid,"requirementness":"REQUIREMENT","requirement_kinds":["QUALITY"],
            "confidence":0.95,"decision_summary":"Synthetic quality target.","reason_codes":["quality_target"]}
        self.new,self.validation,_,_ = core.compile_source_requirement_classification_coordination(previous_artifact=self.base,
            previous_validation_receipt=self.receipt,decisions=(core.SourceRequirementClassificationCoordinationDecisionV1.model_validate(self.decision),),model_id="synthetic")
        self.reading = {"source_item_id":self.sid,"image_anchor_ids":["figure"],"text":"Synthetic interpretation of original drawing.","association_reason":"The picture and text belong to the same original node."}
        self.provenance = {"transport_kind":"CODEX_APP_DIRECT","model_id":"synthetic","recorded_at":"2026-01-01T00:00:00Z",
            "action_record":"Synthetic fixture, no real Agent execution claimed.","authorization_scope":"Synthetic test only."}
        self.bundle = {"document_map":self.dm,"baseline":self.base,"receipt":self.receipt}

    def test_visual_reading_associates_with_existing_text_source_without_creating_ids(self):
        annotations = compile_source_item_visual_readings(self.dm,self.new,[self.reading])
        checked = rebind_source_item_image_annotations(self.dm,self.new,annotations)
        self.assertEqual(set(checked["records"]),{self.sid})
        self.assertEqual(len(self.new.records),1)
        self.assertEqual(checked["records"][self.sid]["visual_interpretation"]["authority"],"AGENT_VISUAL_INTERPRETATION")
        self.assertEqual(self.base.records[0].text_bindings,self.new.records[0].text_bindings)

    def test_rehashed_visual_geometry_or_unknown_anchor_is_rejected(self):
        annotations=compile_source_item_visual_readings(self.dm,self.new,[self.reading])
        changed=deepcopy(annotations);changed["records"][self.sid]["related_images"][0]["bbox"][0]=11
        with self.assertRaisesRegex(ValueError,"pixels/bindings"):
            rebind_source_item_image_annotations(self.dm,self.new,_seal_annotations(changed))
        with self.assertRaisesRegex(ValueError,"actual unique"):
            compile_source_item_visual_readings(self.dm,self.new,[{**self.reading,"image_anchor_ids":["missing"]}])

    def test_direct_compilation_requires_reviewed_candidate_and_predecessor(self):
        value={"expected_source_sha256":self.base.source_sha256,"expected_document_map_sha256":self.dm.artifact_sha256,
            "expected_baseline_sha256":self.base.artifact_sha256,"expected_candidate_sha256":self.new.artifact_sha256,
            "decisions":[self.decision],"visual_readings":[self.reading],"provenance":self.provenance}
        result=compile_direct_revision(self.bundle,value,{})
        self.assertEqual(result[0],self.new)
        with self.assertRaisesRegex(ValueError,"predecessor"):
            compile_direct_revision(self.bundle,{**value,"expected_baseline_sha256":"b"*64},{})
        with self.assertRaisesRegex(ValueError,"reviewed decisions"):
            compile_direct_revision(self.bundle,{**value,"expected_candidate_sha256":"b"*64},{})

    def test_visual_nested_source_and_authority_cannot_be_resigned_into_truth(self):
        original=compile_source_item_visual_readings(self.dm,self.new,[self.reading])
        for field,value in [("source_sha256","b"*64),("authority","CUSTOMER_VERBATIM"),("source_item_id","another-source")]:
            changed=deepcopy(original);changed["records"][self.sid]["visual_interpretation"][field]=value
            with self.assertRaisesRegex(ValueError,"identity or authority"):
                rebind_source_item_image_annotations(self.dm,self.new,_seal_annotations(changed))

    def test_direct_children_can_feed_later_analysis_but_not_another_baseline(self):
        artifact={"source_sha256":self.new.source_sha256,"baseline_sha256":self.new.artifact_sha256,"records":{self.sid:[]}}
        job=SimpleNamespace(payload={"project_id":"project"},result={"baseline_revision_artifact":{"baseline_artifact_sha256":self.new.artifact_sha256},"decomposition_artifact":{"synthetic":"pointer"}})
        container=SimpleNamespace(jobs=SimpleNamespace(get_latest_succeeded=lambda **kwargs:job))
        with patch("app.application.agent_workflow.source_item_direct_revision._read_private",return_value=artifact):
            self.assertEqual(load_direct_decomposition(container,"project","doc",self.new),artifact)
            self.assertIsNone(load_direct_decomposition(container,"project","doc",self.base))

    def test_direct_publication_records_own_host_and_keeps_appserver_strict(self):
        with tempfile.TemporaryDirectory() as root:
            container=SimpleNamespace(object_store=LocalObjectStore(Path(root)/"objects"))
            args=dict(project_id="project",document_id="doc",document_map=self.dm,previous_bundle=self.bundle,
                baseline=self.new,validation_receipt=self.validation,operation="CLASSIFICATION_REVISION",
                affected_scope_ids=("text:clause",),runtime_receipts=(),request_sha256="c"*64,candidate_sha256="d"*64,
                domain_proof={"synthetic":True},context=Context(),container=container)
            with patch("app.application.agent_workflow.source_requirement_reference_bridge._load_effective_reference",return_value=self.bundle):
                with self.assertRaisesRegex(ValueError,"App Server receipts"):
                    publish_agent_source_item_revision(**args)
                result=publish_agent_source_item_revision(**args,direct_provenance=self.provenance)
            stored=_load_product_baseline_artifact(container.object_store,result["baseline_revision_artifact"],project_id="project",document_id="doc")
            self.assertEqual(stored.product_run_receipts[-1].transport_kind,"CODEX_APP_DIRECT")
            self.assertNotIn("runtime_receipts",stored.product_run_receipts[-1].model_dump())
            self.assertEqual(stored.baseline.predecessor_artifact_sha256,self.base.artifact_sha256)
            self.assertFalse(result["is_human_accepted"])


if __name__ == "__main__":
    unittest.main()
