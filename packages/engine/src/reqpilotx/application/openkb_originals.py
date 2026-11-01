"""OpenKB native-original read tool and structured answer-envelope validation.

The tool's filesystem/knowledge generation is selected by the application.
The existing upstream REST endpoint does not supply this extension by itself.
"""
from copy import deepcopy
import json
from pathlib import Path

class OpenKBOriginalError(ValueError):
    """An explicit original evidence or generation contract failure."""

AdapterError = OpenKBOriginalError


class OriginalEvidenceBridge:
    def __init__(self, store, generation=None):
        self.store = store
        self.generation = generation or store.current_generation
        self.issued = {}

    def _current(self):
        if self.generation != self.store.current_generation:
            raise AdapterError("OpenKB session generation is stale; restart retrieval")
        return self.store.snapshot(self.generation)

    def source_index(self):
        manifest, catalog = self._current()
        return {"project_id": self.store.project_id, "kb": self.store.kb, "generation": self.generation,
            "documents": [{"document_id": d["record"]["id"], "name": d["logical_name"],
                "source_sha256": d["record"]["source_sha256"],
                "applicability": d["record"]["applicability"]} for d in manifest["documents"]],
            "native_catalog_sha256": catalog["catalog_sha256"]}

    def read_registered_original(self, document_id):
        manifest, catalog = self._current()
        documents = {d["record"]["id"]: d for d in manifest["documents"]}
        if document_id not in documents:
            raise AdapterError("Unknown or cross-KB document request")
        facts = [f for f in catalog["evidence_catalog"].values() if f["document_id"] == document_id]
        # No hidden truncation: a large document needs a future locator-aware tool.
        if len(facts) > 256 or sum(len(f["text"]) for f in facts) > 64000:
            raise AdapterError("Native original exceeds tool budget; narrower locator required")
        evidence = []
        for fact in facts:
            reference = self.store.reference_from_fact(fact["fact_id"], generation=self.generation)
            resolved = self.store.resolve_reference(reference)
            self.issued[resolved["evidence_id"]] = reference
            evidence.append({"evidence_id": resolved["evidence_id"], "reference": reference,
                "text": resolved["excerpt"], "source_ref": resolved["source_ref"],
                "applicability": resolved["applicability"], "authority": resolved["authority"],
                "claim_role": resolved["claim_role"], "semantic_coverage": "NOT_ASSESSED"})
        gaps = [deepcopy(g) for g in catalog["gaps"] if g.get("document_id") == document_id]
        return {"generation": self.generation, "document_id": document_id,
            "evidence": evidence, "gaps": gaps, "assessment": "NOT_PERFORMED"}

    def validate_model_envelope(self, envelope):
        """Model may select references previously emitted by this tool.

        An envelope proves provenance/selection only, never semantic correctness.
        It cannot manufacture a reference, locator, original text or acceptance.
        """
        self._current()
        expected = {"schema_version", "project_id", "kb", "generation", "answer", "evidence_ids",
                    "rationale", "conditions", "conflicts", "missing"}
        if (not isinstance(envelope, dict) or set(envelope) != expected
                or envelope.get("schema_version") != "openkb-evidence-answer/v1"
                or envelope.get("project_id") != self.store.project_id
                or envelope.get("kb") != self.store.kb or envelope.get("generation") != self.generation):
            raise AdapterError("Structured answer namespace/shape mismatch")
        ids = envelope["evidence_ids"]
        if (not isinstance(ids, list) or len(ids) > 32
                or any(not isinstance(i, str) or i not in self.issued for i in ids)
                or len(set(ids)) != len(ids)):
            raise AdapterError("Answer cites fabricated, duplicate or unread original evidence")
        if any(not isinstance(envelope[k], str) for k in ("answer", "rationale")):
            raise AdapterError("Answer and inference rationale must be strings")
        for key in ("conditions", "conflicts", "missing"):
            if not isinstance(envelope[key], list) or any(not isinstance(x, str) for x in envelope[key]):
                raise AdapterError("Explicit conditions/conflicts/missing list required")
        references = [deepcopy(self.issued[i]) for i in ids]
        facts = [self.store.resolve_reference(ref) for ref in references]
        return {"schema_version": "openkb-validated-answer-proposal/v1",
            "generation": self.generation, "inferred_answer": envelope["answer"],
            "inference_rationale": envelope["rationale"], "verified_original_evidence": facts,
            "references": references, "conditions": envelope["conditions"],
            "conflicts": envelope["conflicts"], "missing": envelope["missing"],
            "semantic_assessment": "NOT_VERIFIED", "human_acceptance": "NOT_ACCEPTED"}
