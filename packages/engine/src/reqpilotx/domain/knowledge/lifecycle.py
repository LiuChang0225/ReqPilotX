from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping


ACTIVE_KNOWLEDGE_STATUS = "ACTIVE"
ALLOWED_KNOWLEDGE_DOC_TYPES = frozenset(
    {
        "REFERENCE",
        "STANDARD",
        "SPEC",
        "REGULATION",
        "CASE",
    }
)


@dataclass(frozen=True, slots=True)
class KnowledgeEvidenceEligibility:
    allowed: bool
    reason: str


def evaluate_knowledge_evidence_eligibility(
    knowledge_document: Mapping[str, Any],
    *,
    as_of: date | None = None,
) -> KnowledgeEvidenceEligibility:
    """Decide whether a knowledge document may supply assessment evidence.

    Missing or invalid lifecycle metadata fails closed. Requirement-specific
    authority policy remains outside this general lifecycle gate.
    """

    status = str(knowledge_document.get("status") or "").strip().upper()
    if status != ACTIVE_KNOWLEDGE_STATUS:
        return KnowledgeEvidenceEligibility(False, "status_not_active")

    doc_type = str(knowledge_document.get("doc_type") or "").strip().upper()
    if doc_type not in ALLOWED_KNOWLEDGE_DOC_TYPES:
        return KnowledgeEvidenceEligibility(False, "doc_type_not_allowed")

    effective_from, from_valid = _parse_optional_date(knowledge_document.get("effective_from"))
    if not from_valid:
        return KnowledgeEvidenceEligibility(False, "invalid_effective_from")
    effective_to, to_valid = _parse_optional_date(knowledge_document.get("effective_to"))
    if not to_valid:
        return KnowledgeEvidenceEligibility(False, "invalid_effective_to")
    if effective_from and effective_to and effective_from > effective_to:
        return KnowledgeEvidenceEligibility(False, "invalid_effective_window")

    current_date = as_of or date.today()
    if effective_from and current_date < effective_from:
        return KnowledgeEvidenceEligibility(False, "not_yet_effective")
    if effective_to and current_date > effective_to:
        return KnowledgeEvidenceEligibility(False, "expired")
    return KnowledgeEvidenceEligibility(True, "eligible")


def _parse_optional_date(value: Any) -> tuple[date | None, bool]:
    if value is None:
        return None, True
    if isinstance(value, datetime):
        return value.date(), True
    if isinstance(value, date):
        return value, True
    text = str(value).strip()
    if not text:
        return None, True
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return None, False
    try:
        return date.fromisoformat(text), True
    except ValueError:
        return None, False
