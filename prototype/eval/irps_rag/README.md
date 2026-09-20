# IRPS RAG retrieval evaluation

> 2026-09-03 定位：保留的离线/兼容工具参考，不定义当前开发顺序或产品完成状态。
> 其中运行计数、版本与阶段结论按其历史语境阅读；仅在明确使用该工具时采用对应合同。
> 当前工作按 [目标态优先开发计划](../../../docs/需求提取/开发计划.md) 推进，客户数据和真实模型调用需单独授权。

This suite evaluates retrieval ranking only. It does not run an assessment
model and therefore does not measure decision accuracy, citation accuracy, or
LLM output quality. Case fields such as `expected_decision` are retained only
as non-evaluated review notes.

Customer corpus files stay outside the repository and are supplied through
`--source-root`. Generated reports are deliberately redacted: queries, source
locators, source paths, file names, manifest IDs, Azure index names, and raw
chunk IDs are never written. Stable SHA-256 report keys allow comparisons
across runs without disclosing those identifiers.

Cases marked `status: requires_human_review` may be exercised diagnostically,
but are excluded from every quality metric. Their presence blocks the release
gate until the label is independently reviewed and the marker is removed.

The release gate passes only when all of the following are true:

- the manifest is `FROZEN`;
- `approval.status` is `APPROVED`, and `approval.content_sha256` still matches
  the canonical evaluation content;
- no case still requires human review;
- at least 20 quality cases are present, including at least 15 cases with Gold
  retrieval documents and at least one visual-only negative;
- every Gold/forbidden document ID resolves to an indexed document with a
  compatible ingestion role, every quality case covers every declared hard
  negative, and a visual-only case forbids the complete searchable set;
- all code-enforced retrieval floors and any stricter declared thresholds pass;
- deletion of every isolated evaluation chunk is confirmed by a follow-up
  Azure Search query.

The customer-derived manifest is private and supplied explicitly with `--manifest`.
Approval must happen only after the retrieval labels
and hard-negative policy are reviewed and frozen.

Run a local corpus preflight without Azure:

```bash
cd prototype
python -m scripts.irps_rag_eval \
  --manifest /controlled/eval/irps_rag/manifest.json \
  --source-root /controlled/external/corpus \
  --preflight-only
```

Run the retrieval evaluation and require the release gate:

```bash
cd prototype
python -m scripts.irps_rag_eval \
  --manifest /controlled/eval/irps_rag/manifest.json \
  --source-root /controlled/external/corpus \
  --require-pass
```

`--require-pass` exits non-zero for a draft/unapproved manifest, unresolved
human-review cases, unavailable metrics, threshold failures, or unconfirmed
cleanup. The JSON report is still written on a cleanup-verification failure
with `cleanup.confirmed=false`; the run is never marked complete in that case.
