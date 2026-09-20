from __future__ import annotations

import re
from typing import Any


TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_\-/]+|\d+(?:\.\d+)?|[\u4e00-\u9fff]{1,4}")
NUMBER_UNIT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|v|kv|a|ma|ohm|hz|khz|mhz|%|°c|c)\b", re.I)
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "are",
    "was",
    "were",
    "shall",
    "must",
    "should",
    "need",
    "项目",
    "要求",
    "测试",
    "试验",
    "规范",
}
SYNONYM_MAP = {
    # Existing automotive terms
    "wake-up": ["startup", "start-up", "cold start"],
    "startup": ["wake-up", "cold start"],
    "ecu": ["electronic control unit", "dut", "device under test"],
    "can": ["can bus", "can fd"],
    "latency": ["response", "delay"],
    # Throttle / ETC domain
    "etc": ["electronic throttle control", "throttle body"],
    "throttle": ["etc", "electronic throttle", "throttle body"],
    "etc12": ["etc", "electronic throttle control"],
    # Protection & safety
    "emc": ["electromagnetic compatibility", "电磁兼容"],
    "esd": ["electrostatic discharge", "静电放电"],
    "ip67": ["ip6k9k", "protection class", "防护等级", "ingress protection"],
    "asil": ["automotive safety integrity level", "功能安全", "iso 26262"],
    # Electrical
    "hv": ["high voltage", "高压"],
    "lv": ["low voltage", "低压"],
    "supply voltage": ["供电电压", "电源电压", "operating voltage"],
    "current consumption": ["静态电流", "power consumption", "quiescent current"],
    # Environmental
    "temperature range": ["温度范围", "operating temperature", "工作温度"],
    "thermal shock": ["温度冲击", "thermal cycling"],
    "salt spray": ["盐雾", "salt fog", "corrosion test"],
    # Document types
    "sor": ["statement of requirements", "需求规格"],
    "pds": ["product design specification", "产品设计规格"],
    "cts": ["component technical specification", "零部件技术规范"],
    # Sensor / iRPS domain
    "irps": ["inductive rotor position sensor", "电感式转子位置传感器"],
    "airgap": ["air gap", "气隙"],
    "linearity": ["线性度", "linearity error"],
    # Response / performance
    "response time": ["响应时间", "reaction time", "step response"],
    "hysteresis": ["迟滞", "hysteresis error"],
    "vibration": ["振动", "vibration test", "sinusoidal vibration"],
}


def retrieve_evidence(
    requirement_text: str, knowledge_docs: list[dict[str, Any]], top_k: int = 3
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for doc in knowledge_docs:
        chunks = _chunk_text(doc.get("extracted_text", ""))
        for idx, chunk in enumerate(chunks, start=1):
            candidates.append(
                {
                    "source_doc_id": doc["id"],
                    "source_doc_name": doc["file_name"],
                    "source_location": f"Chunk {idx}",
                    "excerpt": chunk[:380],
                    "content": chunk,
                }
            )
    return score_candidate_evidences(requirement_text, candidates, top_k=top_k)


def score_candidate_evidences(
    requirement_text: str,
    candidates: list[dict[str, Any]],
    *,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    req_tokens = _expand_tokens(_tokenize(requirement_text))
    if not req_tokens:
        return []
    req_numbers = _extract_number_units(requirement_text)

    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        content = str(candidate.get("content") or candidate.get("excerpt") or "").strip()
        chunk_tokens = _expand_tokens(_tokenize(content))
        if not chunk_tokens:
            continue
        lexical = _f1_overlap(req_tokens, chunk_tokens)
        coverage = _coverage(req_tokens, chunk_tokens)
        numeric = _number_match(req_numbers, _extract_number_units(content))
        score = 0.60 * lexical + 0.25 * coverage + 0.15 * numeric
        boost = _coerce_relevance_boost(candidate.get("relevance_boost"))
        final_score = max(0.0, min(0.99, score * boost))
        if final_score <= 0:
            continue
        scored.append(
            {
                "source_doc_id": str(candidate.get("source_doc_id") or ""),
                "source_doc_name": str(candidate.get("source_doc_name") or ""),
                "source_doc_version": str(candidate.get("source_doc_version") or ""),
                "source_location": str(candidate.get("source_location") or ""),
                "excerpt": str(candidate.get("excerpt") or content[:380]),
                "relevance": round(final_score, 4),
                "content_kind": str(candidate.get("content_kind") or ""),
                "doc_type": str(candidate.get("doc_type") or ""),
                "status": str(candidate.get("status") or ""),
                "effective_from": str(candidate.get("effective_from") or ""),
                "effective_to": str(candidate.get("effective_to") or ""),
                "evidence_id": str(candidate.get("evidence_id") or ""),
                "content_hash": str(candidate.get("content_hash") or ""),
                "evidence_contract_version": str(candidate.get("evidence_contract_version") or ""),
                "evidence_contract_verified": bool(candidate.get("evidence_contract_verified")),
            }
        )

    scored.sort(key=lambda item: item["relevance"], reverse=True)
    return scored[:top_k]


def _chunk_text(text: str, lines_per_chunk: int = 6, overlap: int = 2) -> list[str]:
    lines = [_normalize_line(line) for line in text.splitlines()]
    lines = [line for line in lines if line and len(line) >= 6]
    if not lines:
        return []

    chunks: list[str] = []
    start = 0
    step = max(1, lines_per_chunk - overlap)
    while start < len(lines):
        block = lines[start : start + lines_per_chunk]
        if not block:
            break
        chunk = " ".join(block)
        if len(chunk) >= 35:
            chunks.append(chunk)
        start += step
    return chunks


def _normalize_line(line: str) -> str:
    value = line.strip()
    if not value:
        return ""
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    value = re.sub(r"\s+", " ", value)
    return value


def _tokenize(text: str) -> set[str]:
    tokens = set()
    for token in TOKEN_RE.findall(text.lower()):
        token = token.strip()
        if not token or token in STOPWORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        tokens.add(token)
    return tokens


def _expand_tokens(tokens: set[str]) -> set[str]:
    expanded = set(tokens)
    for token in list(tokens):
        for key, variants in SYNONYM_MAP.items():
            if token == key:
                expanded.update(variants)
            if token in variants:
                expanded.add(key)
    return expanded


def _extract_number_units(text: str) -> set[str]:
    return {match.group(0).replace(" ", "").lower() for match in NUMBER_UNIT_RE.finditer(text)}


def _f1_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left.intersection(right))
    if overlap == 0:
        return 0.0
    precision = overlap / len(right)
    recall = overlap / len(left)
    return 2 * precision * recall / (precision + recall)


def _coverage(left: set[str], right: set[str]) -> float:
    if not left:
        return 0.0
    return len(left.intersection(right)) / len(left)


def _number_match(req_numbers: set[str], chunk_numbers: set[str]) -> float:
    if not req_numbers:
        return 0.5
    if not chunk_numbers:
        return 0.0
    overlap = len(req_numbers.intersection(chunk_numbers))
    return overlap / len(req_numbers)


def _coerce_relevance_boost(value: object) -> float:
    try:
        number = float(value or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.5, number))
