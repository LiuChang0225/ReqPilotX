"""Evidence-bound proposal/review ledger, never automatic human acceptance.

The application owns this state. OpenKB tools cannot call accept(). Human
identity here is a host assertion, not an implemented authentication provider.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from .errors import AdapterError
from .knowledge_store import checked_id, digest, sealed, unseal, write_json

VERDICTS = {"COMPLIANT", "PARTIALLY_COMPLIANT", "NON_COMPLIANT", "NOT_APPLICABLE",
            "PENDING_EVIDENCE", "NEEDS_CLARIFICATION"}
EVIDENTIARY = {"COMPLIANT", "PARTIALLY_COMPLIANT", "NON_COMPLIANT", "NOT_APPLICABLE"}
PRODUCT_CLAIMS = {"DESIGN_DECLARATION", "VALIDATION_RESULT", "TEST_RESULT", "CERTIFICATION",
                  "COMPLIANCE_RECORD", "RELEASED_DESIGN_EVIDENCE", "PROJECT_RECORD",
                  "DELIVERY_RECORD", "APPROVAL_RECORD", "PROCESS_RECORD"}


class JudgmentLedger:
    def __init__(self, root, store):
        self.root = Path(root)
        if self.root.is_symlink():
            raise AdapterError("Judgment root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = self.root.resolve()
        self.store = store

    def _path(self, judgment_id):
        checked_id(judgment_id)
        path = self.root / (judgment_id + ".json")
        if path.is_symlink():
            raise AdapterError("Judgment path cannot be a symlink")
        return path

    def _record(self, judgment_id):
        try:
            value = unseal(json.loads(self._path(judgment_id).read_text()))
        except (OSError, ValueError) as exc:
            raise AdapterError("Judgment record cannot be verified") from exc
        if value["project_id"] != self.store.project_id or value["kb"] != self.store.kb:
            raise AdapterError("Judgment belongs to another project/KB")
        return value

    def submit(self, requirement_id, requirement_text, verdict, rationale, references, *,
               conditions=None, conflicts=None, missing=None, producer="SYNTHETIC_PROPOSAL"):
        checked_id(requirement_id)
        if verdict not in VERDICTS:
            raise AdapterError("Unknown compliance verdict")
        if (not isinstance(requirement_text, str) or not requirement_text.strip()
                or not isinstance(rationale, str) or not rationale.strip()):
            raise AdapterError("Requirement and inference rationale must be nonempty")
        if not isinstance(references, list) or len(references) > 32:
            raise AdapterError("Invalid or over-budget judgment references")
        if any(not isinstance(items, list) or any(not isinstance(x, str) or not x.strip() for x in items)
               for items in (conditions or [], conflicts or [], missing or [])):
            raise AdapterError("Conditions, conflicts and gaps must be explicit text lists")
        # A failed/fabricated reference rejects the proposal, never silently drops it.
        evidence = [self.store.resolve_reference(ref) for ref in references]
        if len({e["evidence_id"] for e in evidence}) != len(evidence):
            raise AdapterError("Duplicate evidence references")
        blockers = []
        if not evidence:
            blockers.append("NO_ORIGINAL_EVIDENCE")
        if conflicts:
            blockers.append("UNRESOLVED_CONFLICTS")
        if missing:
            blockers.append("MISSING_EVIDENCE_OR_COVERAGE")
        for e in evidence:
            if not e["authority_verified"]:
                blockers.append("UNKNOWN_AUTHORITY")
            if not e["applicability"].strip():
                blockers.append("UNKNOWN_APPLICABILITY")
            if e["claim_role"] not in PRODUCT_CLAIMS:
                blockers.append("NOT_PRODUCT_OR_PROJECT_EVIDENCE")
            if e["native_capture_gaps"]:
                blockers.append("UNREAD_OR_INCOMPLETE_ORIGINAL")
        effective = verdict
        if conflicts:
            effective = "NEEDS_CLARIFICATION"
        elif blockers:
            effective = "PENDING_EVIDENCE"
        record = sealed({"schema_version": "openkb-evidence-judgment/v1",
            "judgment_id": "judgment-" + uuid.uuid4().hex,
            "project_id": self.store.project_id, "kb": self.store.kb,
            "generation": self.store.current_generation,
            "requirement": {"id": requirement_id, "text": requirement_text,
                "sha256": digest(requirement_text)},
            "proposed_verdict": verdict, "effective_verdict": effective,
            "inference": {"rationale": rationale, "producer": producer,
                "kind": "PROPOSAL_NOT_SOURCE_FACT"},
            "original_observations": evidence,
            "evidence_ids": [e["evidence_id"] for e in evidence],
            "references": deepcopy(references), "conditions": conditions or [],
            "conflicts": conflicts or [], "missing": missing or [],
            "blockers": sorted(set(blockers)), "human_acceptance": "NOT_ACCEPTED",
            "created_at": datetime.now(timezone.utc).isoformat()})
        write_json(self._path(record["judgment_id"]), record)
        return self.status(record["judgment_id"])

    def status(self, judgment_id):
        record = self._record(judgment_id)
        stale = record["generation"] != self.store.current_generation
        # Current status must also recapture bytes, even if generation is unchanged.
        integrity_failure = None
        if not stale:
            try:
                for ref in record["references"]:
                    self.store.resolve_reference(ref)
            except AdapterError as exc:
                integrity_failure = str(exc)
        approval_path = self.root / (judgment_id + "-acceptance.json")
        acceptance = None
        if approval_path.exists():
            if approval_path.is_symlink():
                raise AdapterError("Acceptance path cannot be symlink")
            acceptance = unseal(json.loads(approval_path.read_text()))
            if (acceptance.get("judgment_sha256") != record["record_sha256"]
                    or acceptance.get("generation") != record["generation"]):
                raise AdapterError("Acceptance does not bind this judgment revision")
        state = ("REVIEW_REQUIRED" if stale or integrity_failure else
                 "BLOCKED" if record["blockers"] else
                 "HUMAN_ACCEPTED" if acceptance else "DRAFT_REQUIRES_HUMAN_REVIEW")
        return {"judgment_id": judgment_id, "status": state,
            "effective_verdict": "PENDING_EVIDENCE" if state == "REVIEW_REQUIRED" else record["effective_verdict"],
            "evidence_ids": record["evidence_ids"], "generation": record["generation"],
            "current_generation": self.store.current_generation, "blockers": record["blockers"],
            "human_acceptance": "CURRENT_HOST_ASSERTED_ACCEPTANCE" if state == "HUMAN_ACCEPTED" else "NOT_CURRENTLY_ACCEPTED",
            "historical_acceptance_exists": acceptance is not None,
            "integrity_failure": integrity_failure}

    def accept(self, judgment_id, *, reviewer, expected_generation, requirement_sha256,
               review):
        """Host engineer action only; never expose this method as a model tool.

        review must confirm scope, complete requirement coverage, applicability,
        provenance and conflict review. This records the review, not its correctness.
        """
        checked_id(reviewer)
        record = self._record(judgment_id)
        current = self.status(judgment_id)
        required = {"scope_and_conditions_reviewed", "complete_requirement_coverage_reviewed",
                    "originals_and_authority_reviewed", "conflicts_and_missing_reviewed"}
        if (not isinstance(review, dict) or set(review) != required
                or any(review[key] is not True for key in required)):
            raise AdapterError("Explicit complete human review confirmations required")
        if (current["status"] not in {"DRAFT_REQUIRES_HUMAN_REVIEW", "HUMAN_ACCEPTED"}
                or expected_generation != self.store.current_generation
                or expected_generation != record["generation"]
                or requirement_sha256 != record["requirement"]["sha256"]):
            raise AdapterError("Stale, blocked or mismatched judgment cannot be accepted")
        value = sealed({"schema_version": "openkb-evidence-human-acceptance/v1",
            "judgment_id": judgment_id, "judgment_sha256": record["record_sha256"],
            "generation": expected_generation, "reviewer": reviewer, "review": deepcopy(review),
            "requirement_sha256": requirement_sha256,
            "accepted_at": datetime.now(timezone.utc).isoformat(),
            "identity_validation": "HOST_ASSERTED_HUMAN_ACTION_ONLY"})
        target = self.root / (judgment_id + "-acceptance.json")
        # Duplicate acceptance is idempotent, and never overwrites another review.
        if target.exists():
            old = unseal(json.loads(target.read_text()))
            if old["reviewer"] != reviewer or old["review"] != review:
                raise AdapterError("Existing immutable acceptance cannot be replaced")
        else:
            write_json(target, value)
        return self.status(judgment_id)

    def reproduce(self, judgment_id):
        record = self._record(judgment_id)
        evidence = [self.store.resolve_reference(ref, require_current=False) for ref in record["references"]]
        if evidence != record["original_observations"]:
            raise AdapterError("Historical evidence differs from original judgment")
        return {"record": record, "verified_original_evidence": evidence,
            "current_status": self.status(judgment_id),
            "historical_reproduction": "VERIFIED_ORIGINAL_BYTES_AND_LOCATORS"}
