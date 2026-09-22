from __future__ import annotations

import re
from typing import Any


MODEL_NAME = "rules-rag-prototype"
PROMPT_VERSION = "v2.1-authority-outcome-proof-gate"
MANDATORY_HINTS = ["must", "shall", "mandatory", "必须", "不得", "应当", "safety", "asil", "法规", "comply"]
NEGATIVE_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"\bcannot\b",
        r"\bnot\s+(?:support|comply|meet|allow|possible)\b",
        r"\bfail(?:ed|ure)?\b",
        r"不满足|不支持|无法|不符合|不通过|失败",
    ]
]
COMPLIANCE_HINTS = ["comply", "conform", "meet", "follow", "符合", "满足", "遵循", "通过"]
PARTIAL_COMPLIANCE_HINTS = [
    "partially",
    "partial compliance",
    "with deviation",
    "except for",
    "部分满足",
    "部分符合",
    "存在偏差",
    "例外",
]
DECISION_PROOF_CLAIM_ROLES = {
    "VALIDATION_RESULT",
    "TEST_RESULT",
    "CERTIFICATION",
    "COMPLIANCE_RECORD",
    "RELEASED_DESIGN_EVIDENCE",
}


def _has_outcome_proof(evidence: dict[str, Any]) -> bool:
    authority = str(evidence.get("authority") or "UNKNOWN").strip().upper()
    if authority in {"", "UNKNOWN", "UNVERIFIED"}:
        return False
    applicability = str(evidence.get("applicability") or "UNKNOWN").strip().upper()
    if applicability in {"NOT_APPLICABLE", "OUT_OF_SCOPE"}:
        return False
    claim_role = str(evidence.get("claim_role") or "UNKNOWN").strip().upper()
    if claim_role in DECISION_PROOF_CLAIM_ROLES:
        return True
    value = str(evidence.get("excerpt") or "")
    lowered = value.lower()
    return bool(
        re.search(
            r"\b(?:passed|failed|verified|validated|certified|approved|"
            r"measured|observed|demonstrated|recorded)\b",
            lowered,
        )
        or re.search(
            r"\b(?:test|validation|inspection|verification|measurement)"
            r"\s+(?:result|record|report|evidence)s?\b",
            lowered,
        )
        or re.search(
            r"(?:测试|试验|验证|检验|检测|测量)(?:结果|记录|报告|证明)"
            r"|(?:报告|记录|结果)(?:显示|表明|确认)"
            r"|(?:已|经)(?:完成)?(?:测试|试验|验证|检验|检测)"
            r"|(?:已|未)(?:通过|批准|认证)"
            r"|(?:实测|测得|验证通过|测试通过|试验通过|验证失败|测试失败|试验失败)",
            value,
        )
    )


def assess_requirement(
    requirement_text: str,
    requirement_type: str,
    evidences: list[dict[str, Any]],
) -> dict[str, Any]:
    text_lower = requirement_text.lower()
    has_mandatory = any(word in text_lower for word in MANDATORY_HINTS)

    if not evidences:
        return {
            "ai_decision": "NEED_CLARIFICATION",
            "ai_reason": "未检索到可直接覆盖该需求的内部证据。",
            "ai_confidence": 0.28,
            "risk_level": "HIGH",
            "model_name": MODEL_NAME,
            "prompt_version": PROMPT_VERSION,
        }

    ranked = sorted(evidences, key=lambda item: float(item["relevance"]), reverse=True)
    top = ranked[0]
    score = float(top["relevance"])
    top_doc = top.get("source_doc_name", "knowledge doc")
    excerpt = (top.get("excerpt") or "").lower()
    has_outcome_proof = _has_outcome_proof(top)

    has_negative = has_outcome_proof and any(
        pattern.search(excerpt) for pattern in NEGATIVE_PATTERNS
    )
    claim_role = str(top.get("claim_role") or "UNKNOWN").strip().upper()
    has_compliance = has_outcome_proof and not has_negative and (
        claim_role in DECISION_PROOF_CLAIM_ROLES
        or any(hint in excerpt for hint in COMPLIANCE_HINTS)
    )
    has_partial_compliance = has_outcome_proof and any(
        hint in excerpt for hint in PARTIAL_COMPLIANCE_HINTS
    )

    decision: str
    reason: str
    risk: str

    if has_negative and score >= 0.55:
        decision = "REJECT"
        reason = f"主要证据（{top_doc}）显示存在不满足或不支持迹象（相关度 {score:.2f}）。"
        risk = "HIGH"
    elif requirement_type == "REGULATION":
        if score >= 0.62 and has_compliance and not has_partial_compliance:
            decision = "ACCEPT"
            reason = f"法规类条目在 {top_doc} 中检索到直接符合性证据（相关度 {score:.2f}）。"
            risk = "LOW"
        elif score >= 0.62 and has_partial_compliance:
            decision = "DEVIATION_ACCEPT"
            reason = f"法规类条目在 {top_doc} 中检索到明确的部分符合或偏差信号（相关度 {score:.2f}）。"
            risk = "HIGH"
        else:
            decision = "NEED_CLARIFICATION"
            reason = f"法规类条目只有相关候选，缺少可直接核查的符合性事实（最佳相关度 {score:.2f}）。"
            risk = "HIGH"
    else:
        if score >= 0.65 and has_compliance and not has_partial_compliance:
            decision = "ACCEPT"
            reason = f"主要证据（{top_doc}）包含直接符合性事实（相关度 {score:.2f}）。"
            risk = "LOW"
        elif score >= 0.60 and has_partial_compliance:
            decision = "DEVIATION_ACCEPT"
            reason = f"主要证据（{top_doc}）明确显示部分覆盖或偏差（相关度 {score:.2f}）。"
            risk = "MEDIUM"
        else:
            decision = "NEED_CLARIFICATION"
            reason = (
                f"候选证据与需求存在关联（{top_doc}, {score:.2f}），"
                "但相关度本身不能证明符合性。"
            )
            risk = "HIGH"

    base = 0.45 if decision in {"ACCEPT", "REJECT"} else 0.38
    confidence = min(0.96, base + score * 0.5)
    if has_mandatory:
        confidence = min(0.98, confidence + 0.04)
    if decision == "NEED_CLARIFICATION":
        confidence = min(confidence, 0.72)

    return {
        "ai_decision": decision,
        "ai_reason": reason,
        "ai_confidence": round(confidence, 4),
        "risk_level": risk,
        "model_name": MODEL_NAME,
        "prompt_version": PROMPT_VERSION,
    }
