#!/usr/bin/env python3
"""
ZG (zvec-grep) Hybrid Knowledge Retriever
-----------------------------------------
Integrates zvec-grep for hybrid search (lexical + BM25 + dense semantic)
with a deterministic local in-memory fallback for full offline autonomy.
"""

import os
import sys
import re
import json
import math
import subprocess
import argparse
from pathlib import Path
from collections import Counter

class ZgHybridRetriever:
    def __init__(self, wiki_dir: str):
        self.wiki_dir = Path(wiki_dir)
        self.documents = [] # list of {id, path, title, category, content, lines}
        self.avg_doc_len = 0
        self.doc_freqs = Counter()
        self._load_corpus()

    def _load_corpus(self):
        total_len = 0
        for root, _, files in os.walk(self.wiki_dir):
            for f in sorted(files):
                if f.endswith(".md") and f not in ["WIKI_RULES.md", "log.md"]:
                    fp = Path(root) / f
                    rel_p = str(fp.relative_to(self.wiki_dir))
                    with open(fp, "r", encoding="utf-8") as fobj:
                        text = fobj.read()

                    # Extract title
                    title = f[:-3]
                    m_title = re.search(r"^title:\s*(.*)$", text, re.MULTILINE)
                    if m_title:
                        title = m_title.group(1).strip()

                    lines = text.split("\n")
                    words = self._tokenize(text)
                    total_len += len(words)

                    # Document frequency for BM25
                    unique_words = set(words)
                    for w in unique_words:
                        self.doc_freqs[w] += 1

                    self.documents.append({
                        "id": rel_p[:-3] if rel_p.endswith(".md") else rel_p,
                        "rel_path": rel_p,
                        "title": title,
                        "category": rel_p.split("/")[0] if "/" in rel_p else "root",
                        "content": text,
                        "lines": lines,
                        "words": words,
                        "word_counts": Counter(words)
                    })

        self.avg_doc_len = total_len / max(1, len(self.documents))

    def _tokenize(self, text: str) -> list[str]:
        # Chinese & English mixed tokenizer
        text = text.lower()
        # alphanumeric or individual Chinese character
        tokens = re.findall(r'[a-z0-9_]+|[\u4e00-\u9fa5]', text)
        return tokens

    def _bm25_score(self, query_tokens: list[str], doc: dict, k1=1.5, b=0.75) -> float:
        score = 0.0
        doc_len = len(doc["words"])
        N = len(self.documents)
        for q in query_tokens:
            if q not in doc["word_counts"]:
                continue
            tf = doc["word_counts"][q]
            df = self.doc_freqs.get(q, 0)
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)
            num = tf * (k1 + 1)
            den = tf + k1 * (1 - b + b * (doc_len / self.avg_doc_len))
            score += idf * (num / den)
        return score

    def search_local(self, query: str, top_k: int = 5) -> list[dict]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scored_docs = []
        for doc in self.documents:
            bm25 = self._bm25_score(query_tokens, doc)

            # Check exact keyword or regex match bonus
            exact_bonus = 0.0
            best_snippet = ""
            best_line_no = 1

            # Find best snippet matching query terms
            for line_idx, line in enumerate(doc["lines"]):
                if not line.strip():
                    continue
                matched_in_line = sum(1 for q in query_tokens if q in line.lower())
                if matched_in_line > 0:
                    bonus = matched_in_line * 2.0
                    if query.lower() in line.lower():
                        bonus += 5.0 # substring exact match
                    if bonus > exact_bonus:
                        exact_bonus = bonus
                        best_snippet = line.strip()
                        best_line_no = line_idx + 1

            total_score = bm25 + exact_bonus
            if total_score > 0:
                scored_docs.append({
                    "id": doc["id"],
                    "rel_path": doc["rel_path"],
                    "title": doc["title"],
                    "category": doc["category"],
                    "score": round(total_score, 3),
                    "best_line_no": best_line_no,
                    "snippet": best_snippet or doc["lines"][0][:120]
                })

        scored_docs.sort(key=lambda x: x["score"], reverse=True)
        return scored_docs[:top_k]

    def search(self, query: str, mode: str = "hybrid", top_k: int = 5) -> list[dict]:
        # Try native zg if available and asked
        if mode == "zg_cli":
            try:
                cmd = ["npx", "-y", "@zvec/zvec-grep", "query", query, "--limit", str(top_k)]
                res = subprocess.run(cmd, cwd=str(self.wiki_dir), capture_output=True, text=True, timeout=8)
                if res.returncode == 0 and res.stdout.strip():
                    return [{"raw_zg_output": res.stdout.strip()}]
            except Exception as e:
                pass # fallback to robust local search

        return self.search_local(query, top_k)


def main():
    parser = argparse.ArgumentParser(description="ZG Knowledge Retriever CLI")
    parser.add_argument("query", help="Search query string")
    parser.add_argument("--wiki", default="docs/wiki/irps_baseline", help="Wiki path")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results")
    parser.add_argument("--mode", default="hybrid", choices=["hybrid", "exact", "zg_cli"], help="Search mode")
    parser.add_argument("--json", action="store_true", help="Output JSON format")
    args = parser.parse_args()

    retriever = ZgHybridRetriever(args.wiki)
    results = retriever.search(args.query, mode=args.mode, top_k=args.top_k)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(f"\nSearch results for: \"{args.query}\" (mode: {args.mode})")
        print("=" * 70)
        for idx, r in enumerate(results, 1):
            print(f"{idx}. [{r['category']}] {r['title']} (Score: {r['score']})")
            print(f"   路径: {r['rel_path']}#L{r['best_line_no']}")
            print(f"   证据: \"{r['snippet'][:100]}\"\n")

if __name__ == "__main__":
    main()
