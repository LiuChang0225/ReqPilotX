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
    "wake-up": ["startup", "start-up", "cold start"],
    "startup": ["wake-up", "cold start"],
    "ecu": ["dut", "device under test"],
    "can": ["can bus"],
    "latency": ["response", "delay"],
}


def retrieve_evidence(
    requirement_text: str, knowledge_docs: list[dict[str, Any]], top_k: int = 3
) -> list[dict[str, Any]]:
    req_tokens = _expand_tokens(_tokenize(requirement_text))
    if not req_tokens:
        return []
    req_numbers = _extract_number_units(requirement_text)

    candidates: list[dict[str, Any]] = []
    for doc in knowledge_docs:
        chunks = _chunk_text(doc.get("extracted_text", ""))
        for idx, chunk in enumerate(chunks, start=1):
            chunk_tokens = _expand_tokens(_tokenize(chunk))
            if not chunk_tokens:
                continue
            lexical = _f1_overlap(req_tokens, chunk_tokens)
            coverage = _coverage(req_tokens, chunk_tokens)
            numeric = _number_match(req_numbers, _extract_number_units(chunk))
            score = 0.60 * lexical + 0.25 * coverage + 0.15 * numeric
            if score <= 0:
                continue
            candidates.append(
                {
                    "source_doc_id": doc["id"],
                    "source_doc_name": doc["file_name"],
                    "source_location": f"Chunk {idx}",
                    "excerpt": chunk[:380],
                    "relevance": round(min(0.99, score), 4),
                }
            )

    candidates.sort(key=lambda item: item["relevance"], reverse=True)
    return candidates[:top_k]


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
