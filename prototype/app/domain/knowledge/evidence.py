from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


EVIDENCE_INDEX_CONTRACT_VERSION = "evidence-v2"
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def canonical_evidence_bytes(content: Any) -> bytes:
    """Return canonical UTF-8 bytes used by the evidence content hash.

    Canonicalization applies Unicode NFC, LF newlines, removal of trailing
    horizontal whitespace per line, and removal of surrounding blank lines.
    Semantic whitespace inside a line is preserved.
    """

    normalized = unicodedata.normalize("NFC", str(content or ""))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip(" \t") for line in normalized.split("\n"))
    return normalized.strip("\n").encode("utf-8")


def compute_evidence_content_hash(content: Any) -> str:
    return hashlib.sha256(canonical_evidence_bytes(content)).hexdigest()


def has_current_evidence_contract(candidate: Mapping[str, Any]) -> bool:
    evidence_id = str(candidate.get("evidence_id") or "").strip()
    content_hash = str(candidate.get("content_hash") or "").strip().lower()
    contract_version = str(candidate.get("evidence_contract_version") or "").strip()
    return bool(
        evidence_id
        and contract_version == EVIDENCE_INDEX_CONTRACT_VERSION
        and SHA256_HEX_PATTERN.fullmatch(content_hash)
    )


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Server-owned evidence values used by assessment safety rules.

    Model output may select one of these references, but it is never the
    authority for the excerpt, provenance, or retrieval relevance.
    """

    source_doc_id: str
    source_doc_name: str
    source_location: str
    excerpt: str
    relevance: float
    source_doc_version: str
    evidence_id: str = ""
    content_hash: str = ""
    evidence_contract_version: str = ""

    @classmethod
    def from_server_candidate(cls, candidate: Mapping[str, Any]) -> "EvidenceRef":
        try:
            relevance = float(candidate.get("relevance") or 0.0)
        except (TypeError, ValueError):
            relevance = 0.0
        return cls(
            source_doc_id=str(candidate.get("source_doc_id") or "").strip(),
            source_doc_name=str(candidate.get("source_doc_name") or "").strip(),
            source_location=str(candidate.get("source_location") or "").strip(),
            excerpt=str(candidate.get("excerpt") or "").strip(),
            relevance=round(max(0.0, min(1.0, relevance)), 4),
            source_doc_version=str(candidate.get("source_doc_version") or "").strip(),
            evidence_id=str(candidate.get("evidence_id") or "").strip(),
            content_hash=str(candidate.get("content_hash") or "").strip().lower(),
            evidence_contract_version=str(candidate.get("evidence_contract_version") or "").strip(),
        )

    @property
    def has_current_contract(self) -> bool:
        return has_current_evidence_contract(
            {
                "evidence_id": self.evidence_id,
                "content_hash": self.content_hash,
                "evidence_contract_version": self.evidence_contract_version,
            }
        )

    def to_mapping(self) -> dict[str, Any]:
        """Project to the existing assessment evidence response contract."""

        return {
            "source_doc_id": self.source_doc_id,
            "source_doc_name": self.source_doc_name,
            "source_location": self.source_location,
            "excerpt": self.excerpt,
            "relevance": self.relevance,
            "source_doc_version": self.source_doc_version,
            "evidence_id": self.evidence_id,
            "retrieval_evidence_id": self.evidence_id,
            "content_hash": self.content_hash,
            "evidence_contract_version": self.evidence_contract_version,
        }
