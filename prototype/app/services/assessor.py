from __future__ import annotations

import re
from typing import Any


MODEL_NAME = "rules-rag-prototype"
PROMPT_VERSION = "v1.1-real-samples"
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
            "ai_reason": "未检索到可用内部证据，建议补充对应平台规范或历史案例后再判定。",
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

    has_negative = any(pattern.search(excerpt) for pattern in NEGATIVE_PATTERNS)
    has_compliance = any(hint in excerpt for hint in COMPLIANCE_HINTS)

    decision: str
    reason: str
    risk: str

    if has_negative and score >= 0.55:
        decision = "REJECT"
        reason = f"主要证据（{top_doc}）显示存在不满足/不支持迹象（相关度 {score:.2f}），当前不建议接受。"
        risk = "HIGH"
    elif requirement_type == "REGULATION":
        if score >= 0.62 and has_compliance:
            decision = "ACCEPT"
            reason = f"法规类条目在 {top_doc} 中存在直接符合性证据（相关度 {score:.2f}），建议接受。"
            risk = "LOW"
        elif score >= 0.45:
            decision = "DEVIATION_ACCEPT"
            reason = f"法规类条目仅有部分匹配证据（{top_doc}, {score:.2f}），建议偏差接受并补齐合规证明。"
            risk = "HIGH"
        else:
            decision = "NEED_CLARIFICATION"
            reason = f"法规类条目证据不足（最佳相关度 {score:.2f}），建议先澄清法规适用边界。"
            risk = "HIGH"
    else:
        if score >= 0.65:
            decision = "ACCEPT"
            reason = f"内部证据与需求高匹配（{top_doc}, 相关度 {score:.2f}），建议接受。"
            risk = "LOW"
        elif score >= 0.46:
            decision = "DEVIATION_ACCEPT"
            reason = f"存在可复用证据但非完全覆盖（{top_doc}, {score:.2f}），建议偏差接受并约束边界。"
            risk = "MEDIUM"
        elif score >= 0.30:
            decision = "NEED_CLARIFICATION"
            reason = f"证据相关度有限（{top_doc}, {score:.2f}），建议补充输入条件后再判定。"
            risk = "HIGH"
        else:
            decision = "NEED_CLARIFICATION"
            reason = f"检索到的证据关联度过低（{score:.2f}），无法形成可靠判定。"
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
