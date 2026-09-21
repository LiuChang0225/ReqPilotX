"""Knowledge and retrieval domain contracts."""

from reqpilotx.domain.knowledge.evidence import (
    EVIDENCE_INDEX_CONTRACT_VERSION,
    EvidenceRef,
    canonical_evidence_bytes,
    compute_evidence_content_hash,
    has_current_evidence_contract,
)
from reqpilotx.domain.knowledge.lifecycle import (
    ACTIVE_KNOWLEDGE_STATUS,
    ALLOWED_KNOWLEDGE_DOC_TYPES,
    KnowledgeEvidenceEligibility,
    evaluate_knowledge_evidence_eligibility,
)

__all__ = [
    "ACTIVE_KNOWLEDGE_STATUS",
    "ALLOWED_KNOWLEDGE_DOC_TYPES",
    "EVIDENCE_INDEX_CONTRACT_VERSION",
    "EvidenceRef",
    "KnowledgeEvidenceEligibility",
    "canonical_evidence_bytes",
    "compute_evidence_content_hash",
    "evaluate_knowledge_evidence_eligibility",
    "has_current_evidence_contract",
]
