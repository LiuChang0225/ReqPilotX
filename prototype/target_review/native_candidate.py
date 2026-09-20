"""Review a typed, source-preserving successor from a directly hosted Agent.

The native delivery builder owns the tree. This adapter never converts an outline
into source structure, writes a production pointer, or invents App Server runs.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from app.application.agent_workflow.source_requirement_baseline import (
    DocumentMapArtifactV1, SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineValidationReceiptV1,
    SourceRequirementClassificationCoordinationDecisionV1,
    compile_source_requirement_classification_coordination, canonical_json_sha256,
)
from app.application.agent_workflow.source_item_decomposition import validate_source_item_decomposition
from app.application.agent_workflow.source_item_delivery import build_delivery_snapshot, export_delivery_xlsx
from app.application.agent_workflow.source_item_reading import baseline_reading_projections

try:
    from .server import ReviewStore, ReviewError, canonical
except ImportError:
    from server import ReviewStore, ReviewError, canonical

TOPICS = {"REGULATORY": "法规", "VERIFICATION": "验证", "LOGISTICS_PACKAGING": "物流包装", "STANDARDS": "标准"}
LABELS = {"FUNCTIONAL": "功能", "QUALITY": "质量", "CONSTRAINT": "约束", "PROJECT": "项目要求", "PROCESS": "过程要求"}


def decision_text(record):
    if record.requirementness.value == "NON_REQUIREMENT":
        return "非需求 · " + record.non_requirement_role.value
    return "需求 · " + "；".join(LABELS[k.value] for k in record.requirement_kinds)


class NativeCandidateStore(ReviewStore):
    review_error_type = ReviewError

    def __init__(self, run_dir: Path, review_dir=None, reviewer_kind="engineer"):
        self.run_dir = run_dir.resolve(strict=True)
        manifest = json.loads((self.run_dir / "native-review.json").read_text())
        if manifest.get("schema_version") != "native-source-item-review/v1":
            raise ValueError("Unsupported native candidate manifest")
        if manifest.get("provenance", {}).get("kind") != "CODEX_APP_DIRECT":
            raise ValueError("Native import requires explicit direct-host provenance")
        if not manifest["provenance"].get("action_record") or not manifest["provenance"].get("recorded_at"):
            raise ValueError("Direct-host provenance needs an actual action record and timestamp")
        self.manifest = manifest
        self.source_revision = canonical_json_sha256(manifest)

        def read(name):
            path = (self.run_dir / name).resolve(strict=True)
            if not path.is_relative_to(self.run_dir):
                raise ValueError("Candidate input must remain inside its frozen package")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest["files"].get(name):
                raise ValueError("Native candidate input digest mismatch: " + name)
            return json.loads(raw) if path.suffix == ".json" else raw

        source = read("source.pdf")
        self.document_map = DocumentMapArtifactV1.model_validate(read("document-map.json"))
        self.previous = SourceRequirementBaselineArtifactV1.model_validate(read("baseline-previous.json"))
        self.baseline = SourceRequirementBaselineArtifactV1.model_validate(read("baseline-candidate.json"))
        receipt = SourceRequirementBaselineValidationReceiptV1.model_validate(read("predecessor-validation-receipt.json"))
        decisions = tuple(SourceRequirementClassificationCoordinationDecisionV1.model_validate(d)
                          for d in read("classification-decisions.json"))
        if hashlib.sha256(source).hexdigest() != self.document_map.source_sha256:
            raise ValueError("Native candidate does not belong to the supplied original")
        if (manifest["expected_predecessor_sha256"] != self.previous.artifact_sha256
                or self.previous.document_map_artifact_sha256 != self.document_map.artifact_sha256):
            raise ValueError("Native candidate predecessor or accepted Document Map changed")
        # Recompile the exact authored changes; self-consistent hashes alone are insufficient.
        rebuilt = compile_source_requirement_classification_coordination(
            previous_artifact=self.previous, previous_validation_receipt=receipt,
            decisions=decisions, model_id=manifest["provenance"]["model_id"])[0]
        if rebuilt != self.baseline:
            raise ValueError("Native successor differs from its source-preserving decisions")
        self.table_previews = read("table-previews.json")
        self.decomposition = read("decomposition.json")
        validate_source_item_decomposition(self.baseline, self.decomposition, table_previews=self.table_previews)
        self.annotations = read("annotations.json")
        self.notes = read("semantic-notes.json")
        self.issues = read("review-issues.json")
        self.snapshot = build_delivery_snapshot(self.document_map, self.baseline, self.table_previews,
            annotations=self.annotations, decomposition=self.decomposition,
            project_id=manifest["project_id"], document_id=manifest["document_id"])
        reading = baseline_reading_projections(self.document_map, self.baseline.records, self.run_dir / "source.pdf")
        records = {r.source_record_id: r for r in self.baseline.records}
        for node in self.snapshot["nodes"]:
            item = node.get("source_item") or {}
            source_id = item.get("source_item_id")
            if source_id in reading:
                projections = reading[source_id]
                node["display_text"] = "\n".join(projections[b.content_id]["display_text"]
                    if b.content_id in projections else b.text for b in records[source_id].text_bindings)
                item["reading_projections"] = projections
        self.native_identity = (manifest["project_id"], manifest["document_id"], self.baseline.artifact_id)
        self.targets = {}
        items = []
        cells = {c["cell_id"]: c for table in self.table_previews.values()
                 for fragment in table["fragments"] for c in fragment["cells"]}
        for node in self.snapshot["nodes"]:
            if not node.get("source_item") and not node.get("decomposition"):
                continue
            kind = "derived" if node.get("decomposition") else "source"
            item, child = node.get("source_item") or {}, node.get("decomposition") or {}
            source_id = item.get("source_item_id") or child["source_item_id"]
            record = records[source_id]
            table_text = "\n".join(cells[c]["text"] for f in record.table_fragments for c in f.cell_ids)
            target = {"id": node["node_id"], "text": node["display_text"] or table_text, "derived": [],
                "sectionId": record.node_id, "sourceExcerpts": child.get("source_excerpts", []),
                "analysisContext": self.notes.get(record.source_display_id, {}).get("complete_source_context"),
                "reviewMetadata": {"number": node["source_number"], "title": node["display_title"],
                    "requirementness": item.get("requirementness", "REQUIREMENT"),
                    "classifications": node["classification_paths"], "topics": node["topic_tags"],
                    "contentRole": item.get("non_requirement_role"),
                    "reason": item.get("decision_summary") or child.get("reason"),
                    "classificationStatus": "AGENT_REVIEWED", "granularityStatus": "AGENT_REVIEWED"},
                "sourceRefs": [{**r, "page": r.get("page", r.get("page_number")), "rects": [r["bbox"]],
                                "precision": r.get("precision", "source-binding")}
                               for r in node["source_refs"] if r.get("bbox")]}
            self.targets[target["id"]] = (kind, target)
            if kind == "source":
                items.append(target)
        for node in self.snapshot["nodes"]:
            if node.get("decomposition"):
                self.targets[node["parent_id"]][1]["derived"].append(self.targets[node["node_id"]][1])
        import fitz
        with fitz.open(self.run_dir / "source.pdf") as pdf:
            pages = [{"number": i + 1, "width": page.rect.width, "height": page.rect.height}
                     for i, page in enumerate(pdf)]
        self.bundle = {"schema": "native-source-item-review/v1", "manifest_sha256": self.source_revision,
            "source": {"sha256": self.baseline.source_sha256, "name": manifest["source_name"], "pageCount": len(pages)},
            "pages": pages, "items": items, "ui": {"caseId": manifest["case_id"]},
            "provenance": manifest["provenance"], "baseline_sha256": self.baseline.artifact_sha256,
            "predecessor_sha256": self.previous.artifact_sha256, "production_pointer_changed": False, "issues": self.issues["issues"],
            "nativeSourceContext": {"document_map": self.document_map.model_dump(mode="json"),
                                    "table_previews": self.table_previews}}
        self.source_revision = canonical_json_sha256(self.bundle)
        self.bundle["revision"] = self.source_revision
        self.bundle_bytes = canonical(self.bundle)
        self._initialize_feedback(review_dir, reviewer_kind)

    def native_matrix(self):
        snapshot = deepcopy(self.snapshot)
        before = {r.source_record_id: r for r in self.previous.records}
        state = self.state(False)
        for node in snapshot["nodes"]:
            item, derived = node.get("source_item"), node.get("decomposition")
            if not item and not derived:
                continue
            source_id = item["source_item_id"] if item else derived["source_item_id"]
            old = before[source_id]
            notes = self.notes.get(old.source_display_id, {})
            feedback = state["targets"].get(node["node_id"], {})
            node["candidate_review"] = {
                "sourceRevision": self.source_revision, "reviewRevision": state["revision"],
                "status": feedback.get("status", "unreviewed"), "notes": feedback.get("notes", []),
                "requirementness": item["requirementness"] if item else "REQUIREMENT",
                "classifications": node["classification_paths"], "topics": node["topic_tags"],
                "contentRole": item.get("non_requirement_role", "") if item else "",
                "reason": item["decision_summary"] if item else derived["reason"],
                "previousDecision": decision_text(old), "previousReason": old.decision_summary,
                "sourceId": old.source_display_id, "previousRequirementId": old.requirement_display_id,
                "boundaryAction": notes.get("boundary_action", "KEEP_SOURCE_BOUNDARY"),
                "feedbackSources": notes.get("feedback_refs", []),
                "openIssues": [issue for issue in self.issues["issues"] if old.source_display_id in issue["source_ids"]],
                "derivedFromBaseline": self.previous.revision if derived else None,
                "provenanceKind": "CODEX_APP_DIRECT", "humanAccepted": False,
            }
            node["source_excerpts"] = ([{"id": f"{node['node_id']}-quote-{i}", "role": "DIRECT", "pages": [e["page"]], **e}
                for i, e in enumerate(derived["source_excerpts"])] if derived else node.get("source_excerpts", []))
        snapshot["tree_sha256"] = self.source_revision
        snapshot["candidate_review"] = {"caseId": self.manifest["case_id"],
            "sourceCount": len(self.baseline.records),
            "derivedCount": sum(len(v) for v in self.decomposition["records"].values()),
            "topics": TOPICS, "scope": self.manifest["scope"], "initialNodeId": self.manifest["initial_node_id"],
            "reviewRevision": state["revision"], "provenanceKind": "CODEX_APP_DIRECT", "exportScope": "COMPLETE_SOURCE"}
        snapshot["delivery_sha256"] = canonical_json_sha256({k: v for k, v in snapshot.items() if k != "delivery_sha256"})
        return snapshot

    def native_bootstrap(self):
        project, document, baseline = self.native_identity
        return {"caseId": self.manifest["case_id"], "project": {"id": project,
            "name": self.manifest["project_name"], "customer_name": self.manifest.get("customer_name", "")},
            "document": {"id": document, "project_id": project, "file_name": self.manifest["source_name"],
                "source_sha256": self.baseline.source_sha256, "page_count": len(self.bundle["pages"]), "status": "DRAFT"},
            "requirementSets": {"active_requirement_set_id": baseline, "requirement_sets": [{"id": baseline,
                "status": "DRAFT", "version": self.baseline.revision,
                "version_label": f"第{self.baseline.revision}版语义候选 · 当前基线保持第{self.previous.revision}版",
                "source_kind": "AGENT_CANDIDATE"}]}, "scope": self.manifest["scope"], "sourceRevision": self.source_revision}

    def native_export(self, payload):
        with self.lock:
            if payload.get("sourceRevision") != self.source_revision or payload.get("expectedRevision") != len(self.events):
                raise ValueError("Candidate feedback/version changed before export")
            ids = payload.get("nodeIds")
            if not isinstance(ids, list) or len(ids) != len(set(ids)) or set(ids) - set(self.targets):
                raise ValueError("Invalid candidate export identities")
            if set(ids) != set(self.targets):
                raise ValueError("此来源候选请使用导出全部，保留完整原文与关联试验上下文。")
            # The durable Matrix contract is the native projection. Review-only
            # controls/feedback are an overlay and are not exporter domain fields.
            snapshot = deepcopy(self.snapshot)
            snapshot["delivery_sha256"] = canonical_json_sha256({k: v for k, v in snapshot.items() if k != "delivery_sha256"})
        return export_delivery_xlsx(snapshot, source_file_path=str(self.run_dir / "source.pdf"),
            source_sha256=self.baseline.source_sha256, project_name=self.manifest["project_name"],
            source_file_name=self.manifest["source_name"])
