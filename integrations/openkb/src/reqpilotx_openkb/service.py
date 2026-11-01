"""Host-bound optional bridge into actual ReqPilotX portable public operations.

The host binds the project, baseline and original before requests arrive. Models
can propose decisions and select issued original evidence; they cannot replace
facts, mutate a source baseline, set authority or accept engineering decisions.
"""
from __future__ import annotations

from argparse import Namespace
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from threading import RLock
import uuid

from .errors import AdapterError
from .knowledge_store import digest, sealed, unseal, write_json
from .originals import OriginalEvidenceBridge

BASELINE_FILES = ("document-map.json", "source-requirement-baseline.json", "table-previews.json")
OPTIONAL_FILES = ("image-annotations.json", "source-item-decomposition.json", "manual-decisions.json", "analysis-policy.json")
DECISIONS = {"COMPLIANT", "PARTIALLY_COMPLIANT", "NON_COMPLIANT", "PENDING_EVIDENCE", "NEEDS_CLARIFICATION", "NOT_APPLICABLE"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class OpenKBAnalysisService:
    """Single-host, optional application adapter; no network/model calls.

    This candidate uses the installed engine and the existing portable plugin
    scripts, not a second business compiler. Authentication must be supplied by
    the host when mounting the router. Folder ingestion is host-only.
    """
    def __init__(self, store, baseline_dir, source_file, work_root):
        self.store = store
        self.baseline_dir = Path(baseline_dir).resolve(strict=True)
        self.source_file = Path(source_file).resolve(strict=True)
        raw_root = Path(work_root).absolute()
        if raw_root.is_symlink():
            raise AdapterError("Analysis root cannot be a symbolic link")
        raw_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = raw_root.resolve()
        if self.root == self.baseline_dir or self.root.is_relative_to(self.baseline_dir):
            raise AdapterError("Analysis outputs must be separate from source baseline")
        self._mutex = RLock()
        self._source_binding()

    def _inside(self, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise AdapterError("Analysis path escapes host root")
        for parent in (path, *path.parents):
            if parent == self.root:
                break
            if parent.is_symlink():
                raise AdapterError("Symbolic link in analysis storage")
        return path

    def _source_binding(self):
        for name in BASELINE_FILES:
            if not (self.baseline_dir / name).is_file():
                raise AdapterError("Host baseline is incomplete")
        files = {}
        for name in (*BASELINE_FILES, *OPTIONAL_FILES):
            path = self.baseline_dir / name
            if path.is_symlink():
                raise AdapterError("Symbolic link in host baseline")
            if path.is_file():
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        baseline = read(self.baseline_dir / "source-requirement-baseline.json")
        sha = hashlib.sha256(self.source_file.read_bytes()).hexdigest()
        if sha != baseline["source_sha256"]:
            raise AdapterError("Host original differs from the current baseline")
        return {"files": files, "source_sha256": sha, "baseline_sha256": baseline["artifact_sha256"]}

    def _session(self, session_id):
        if not isinstance(session_id, str) or len(session_id) != 32 or any(c not in "0123456789abcdef" for c in session_id):
            raise AdapterError("Unknown session")
        path = self._inside("sessions/" + session_id)
        session = unseal(read(path / "session.json"))
        if session["project_id"] != self.store.project_id or session["kb"] != self.store.kb:
            raise AdapterError("Cross-project or KB session")
        return path, session

    def _assert_current(self, session):
        if session["generation"] != self.store.current_generation:
            raise AdapterError("Knowledge generation changed; start a new analysis session")
        self.store.snapshot(session["generation"])
        if session["source_binding"] != self._source_binding():
            raise AdapterError("Source baseline or review version changed; start a new analysis session")

    def upload(self, files, *, metadata, declaration):
        with self._mutex:
            return self.store.upload_files(files, metadata=metadata, declaration=declaration)

    def import_folder(self, folder, *, metadata, declaration, mirror=True):
        with self._mutex:
            return self.store.import_folder(folder, metadata=metadata, declaration=declaration, mirror=mirror)

    def begin(self, *, expected_generation, expected_baseline_sha256, unit_ids=None):
        from artifact_tools import analysis_operation
        with self._mutex, self.store.lock():
            binding = self._source_binding()
            if expected_generation != self.store.current_generation or expected_baseline_sha256 != binding["baseline_sha256"]:
                raise AdapterError("The requested knowledge or baseline version is not current")
            _, catalog = self.store.snapshot(expected_generation)
            identifier = uuid.uuid4().hex
            directory = self._inside("sessions/" + identifier)
            directory.mkdir(parents=True, mode=0o700)
            try:
                result = analysis_operation(Namespace(command="analysis-packet", baseline_dir=self.baseline_dir,
                    knowledge_catalog=self.store.inside("snapshots/" + expected_generation + "/native/knowledge-catalog.json"),
                    decomposition=self.baseline_dir / "source-item-decomposition.json" if (self.baseline_dir / "source-item-decomposition.json").is_file() else None,
                    annotations=None, unit_id=unit_ids, output_dir=directory / "packet"))
                packet = read(directory / "packet/analysis-packet.json")
                session = {"schema_version": "reqpilotx-openkb-session/v1", "session_id": identifier,
                    "project_id": self.store.project_id, "kb": self.store.kb, "generation": expected_generation,
                    "source_binding": binding, "packet_sha256": packet["packet_sha256"],
                    "catalog_sha256": catalog["catalog_sha256"], "unit_ids": packet["selected_unit_ids"]}
                self._assert_current(session)
                write_json(directory / "session.json", sealed(session))
                write_json(directory / "issued.json", sealed({"references": {}}))
                return {**session, "unit_count": result["unit_count"], "registered_fact_count": result["registered_fact_count"],
                    "catalog_gaps": catalog["gaps"], "human_acceptance": "NOT_CLAIMED", "live_openkb_run": "NOT_PERFORMED"}
            except Exception:
                shutil.rmtree(directory)
                raise

    def source_index(self, session_id):
        with self._mutex:
            _, session = self._session(session_id)
            self._assert_current(session)
            return OriginalEvidenceBridge(self.store, session["generation"]).source_index()

    def read_original(self, session_id, document_id):
        with self._mutex, self.store.lock():
            directory, session = self._session(session_id)
            self._assert_current(session)
            bridge = OriginalEvidenceBridge(self.store, session["generation"])
            result = bridge.read_registered_original(document_id)
            issued = unseal(read(directory / "issued.json"))
            issued["references"].update(bridge.issued)
            self._assert_current(session)
            write_json(directory / "issued.json", sealed({"references": issued["references"]}), replace=True)
            return result

    def _candidate(self, session, issued, envelope):
        fields = {"schema_version", "project_id", "kb", "generation", "packet_sha256", "decisions"}
        if not isinstance(envelope, dict) or set(envelope) != fields or envelope.get("schema_version") != "reqpilotx-openkb-assessment/v1":
            raise AdapterError("Model envelope must contain only the bound decision fields")
        for key in ("project_id", "kb", "generation", "packet_sha256"):
            if envelope.get(key) != session[key]:
                raise AdapterError("Cross-project, stale or foreign packet envelope")
        values = envelope["decisions"]
        if not isinstance(values, list) or len(values) != len(session["unit_ids"]):
            raise AdapterError("Every requested analysis unit must be covered")
        allowed = {"unit_id", "decision", "reason", "evidence_ids", "conditions", "conflicts", "missing", "clarification_question"}
        candidate, records, seen = [], {}, set()
        for value in values:
            if not isinstance(value, dict) or set(value) != allowed:
                raise AdapterError("Unexpected decision fields or model-controlled provenance")
            uid = value["unit_id"]
            if uid not in session["unit_ids"] or uid in seen:
                raise AdapterError("Duplicate or foreign analysis unit")
            seen.add(uid)
            if not isinstance(value["decision"], str) or value["decision"] not in DECISIONS or not isinstance(value["reason"], str) or not value["reason"].strip():
                raise AdapterError("Explicit decision and reason required")
            if len(value["reason"]) > 8000 or not isinstance(value["clarification_question"], str) or len(value["clarification_question"]) > 8000:
                raise AdapterError("Clarification question must be text")
            for key in ("conditions", "conflicts", "missing", "evidence_ids"):
                if not isinstance(value[key], list) or len(value[key]) > 32 or any(not isinstance(x, str) or not x.strip() or len(x) > 8000 for x in value[key]):
                    raise AdapterError("Explicit bounded conditions, conflicts, missing and evidence lists required")
            ids = value["evidence_ids"]
            if len(ids) > 32 or len(set(ids)) != len(ids) or any(x not in issued for x in ids):
                raise AdapterError("Fabricated, duplicate or unread original evidence")
            if value["decision"] == "COMPLIANT" and (value["conflicts"] or value["missing"]):
                raise AdapterError("Unresolved conflicts or missing coverage cannot be full compliance")
            references = [deepcopy(issued[x]) for x in ids]
            resolved = [self.store.resolve_reference(x) for x in references]
            context = "\n".join(label + ": " + "; ".join(value[key]) for key, label in
                (("conditions", "适用条件"), ("conflicts", "冲突"), ("missing", "缺失依据")) if value[key])
            reason = value["reason"] + ("\n" + context if context else "")
            candidate.append({"unit_id": uid, "decision": value["decision"], "reason": reason,
                "clarification_question": value["clarification_question"],
                "evidence": [{"fact_id": x["fact_id"], "quote": x["excerpt"]} for x in resolved]})
            records[uid] = {**deepcopy(value), "references": references, "resolved_originals": resolved}
        return {"decisions": candidate}, records

    def compile(self, session_id, envelope):
        from artifact_tools import analysis_operation
        with self._mutex, self.store.lock():
            directory, session = self._session(session_id)
            self._assert_current(session)
            issued = unseal(read(directory / "issued.json"))["references"]
            candidate, records = self._candidate(session, issued, envelope)
            stage = directory / (".compile-" + uuid.uuid4().hex)
            stage.mkdir(mode=0o700)
            try:
                write_json(stage / "candidate.json", candidate)
                result = analysis_operation(Namespace(command="compile-assessments", input=stage / "candidate.json",
                    analysis_packet=directory / "packet/analysis-packet.json", baseline_dir=self.baseline_dir,
                    previous_assessments=None, output_dir=stage / "native"))
                assessments = read(stage / "native/source-item-assessments.json")
                receipt = sealed({"schema_version": "reqpilotx-openkb-analysis-receipt/v1",
                    "session_id": session_id, "project_id": session["project_id"], "kb": session["kb"],
                    "generation": session["generation"], "packet_sha256": session["packet_sha256"],
                    "baseline_sha256": session["source_binding"]["baseline_sha256"],
                    "assessment_sha256": assessments["artifact_sha256"], "decisions": records,
                    "producer": "OPENKB_STRUCTURED_PROPOSAL_INPUT", "semantic_validation": "REQUIRES_ENGINEER_REVIEW",
                    "live_openkb_run": "NOT_ATTESTED_BY_INPUT", "human_acceptance": "NOT_CLAIMED"})
                write_json(stage / "native/openkb-analysis-receipt.json", receipt)
                self._assert_current(session)
                output = directory / "results" / assessments["artifact_sha256"]
                output.parent.mkdir(mode=0o700, exist_ok=True)
                if output.exists():
                    if read(output / "openkb-analysis-receipt.json") != receipt:
                        raise AdapterError("Existing immutable result differs")
                else:
                    (stage / "native").rename(output)
                return {"assessment_sha256": assessments["artifact_sha256"], "status": result["status"],
                    "pending_unit_count": result["pending_unit_count"], "human_acceptance": "NOT_CLAIMED",
                    "semantic_validation": result["semantic_validation"], "generation": session["generation"]}
            finally:
                shutil.rmtree(stage)

    def export(self, session_id, assessment_sha256):
        from artifact_tools import deliver
        if not isinstance(assessment_sha256, str) or len(assessment_sha256) != 64 or any(c not in "0123456789abcdef" for c in assessment_sha256):
            raise AdapterError("Invalid assessment version")
        with self._mutex, self.store.lock():
            directory, session = self._session(session_id)
            self._assert_current(session)
            result = self._inside("sessions/" + session_id + "/results/" + assessment_sha256)
            receipt = unseal(read(result / "openkb-analysis-receipt.json"))
            if receipt["assessment_sha256"] != assessment_sha256 or receipt["packet_sha256"] != session["packet_sha256"]:
                raise AdapterError("Result version or packet changed")
            from evidence_tools import verify_packet_evidence, verify_frozen_analysis_source
            from reqpilotx.application.agent_workflow.source_item_analysis import validate_source_item_assessments
            packet = read(result / "analysis-packet.json")
            _, current_catalog = self.store.snapshot(session["generation"])
            verify_packet_evidence(packet, result / "analysis-packet.json")
            verify_frozen_analysis_source(result / "analysis-packet.json", packet, current_catalog, baseline_dir=self.baseline_dir)
            artifact = read(result / "source-item-assessments.json")
            validate_source_item_assessments(packet, artifact)
            if artifact["artifact_sha256"] != assessment_sha256:
                raise AdapterError("Assessment bytes differ from the requested export version")
            exported = result / "delivery"
            if not exported.exists():
                stage = result / (".export-" + uuid.uuid4().hex)
                try:
                    deliver(Namespace(baseline_dir=self.baseline_dir, source=self.source_file, input=None,
                        assessments=result / "source-item-assessments.json", analysis_packet=result / "analysis-packet.json",
                        output_dir=stage, annotations=None, decomposition=None))
                    snapshot = read(stage / "matrix-snapshot.json")
                    if snapshot["source_item_baseline"]["artifact_sha256"] != receipt["baseline_sha256"]:
                        raise AdapterError("Export baseline differs from analysis")
                    from .export import add_original_evidence_annex
                    add_original_evidence_annex(stage, receipt)
                    write_json(stage / "openkb-export-receipt.json", sealed({
                        "schema_version": "reqpilotx-openkb-export-receipt/v1", "session_id": session_id,
                        "generation": session["generation"], "baseline_sha256": receipt["baseline_sha256"],
                        "assessment_sha256": assessment_sha256, "catalog_sha256": session["catalog_sha256"],
                        "matrix_sha256": hashlib.sha256((stage / "matrix-snapshot.json").read_bytes()).hexdigest(),
                        "xlsx_sha256": hashlib.sha256((stage / "matrix.xlsx").read_bytes()).hexdigest(),
                        "native_xlsx_sha256": hashlib.sha256((stage / "matrix-native.xlsx").read_bytes()).hexdigest(),
                        "original_evidence_receipt_sha256": digest(receipt), "human_acceptance": "NOT_CLAIMED"}))
                    self._assert_current(session)
                    stage.rename(exported)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            exported_receipt = unseal(read(exported / "openkb-export-receipt.json"))
            for name, key in (("matrix.xlsx", "xlsx_sha256"), ("matrix-native.xlsx", "native_xlsx_sha256"), ("matrix-snapshot.json", "matrix_sha256")):
                if hashlib.sha256((exported / name).read_bytes()).hexdigest() != exported_receipt[key]:
                    raise AdapterError("Immutable export bytes changed")
            self._assert_current(session)
            return exported, exported_receipt
