# PDF visual AI pre-review artifact

> 2026-09-03 定位：保留的离线/兼容工具参考，不定义当前开发顺序或产品完成状态。
> 其中运行计数、版本与阶段结论按其历史语境阅读；仅在明确使用该工具时采用对应合同。
> 当前工作按 [目标态优先开发计划](../../../docs/需求提取/开发计划.md) 推进，客户数据和真实模型调用需单独授权。

This directory defines a deliberately non-Gold, AI-only visual pre-review
artifact. It records whether an AI reviewer visited every PDF page in one
bound range and what it decided about text, images, tables, and layout.

It is side evidence only:

- `schema_version` is always `pdf_visual_ai_pre_review.v1`.
- `status` is always `AI_PRE_REVIEW`.
- `location` is always `CONTROLLED_EXTERNAL`.
- `is_human_gold` and `is_formal_approval` are always `false`.
- Even an all-`PASS` ledger remains AI pre-review evidence. It never becomes
  `READY`, Gold, or formal approval.
- The shape is intentionally incompatible with
  `pdf_body_review_artifacts.finalize_pdf_body_review_artifacts`. That
  finalizer still requires its independent external `HUMAN` approval,
  trusted-reviewer policy, and formal draft/profile binding.

## Contract

`binding` pins the opaque document ID, source SHA-256, inclusive page range,
and the ID/version of the PDF adapter, structure resolver, preview, and page
presenter.

`review_bundle_sha256` is derived from a bundle that has passed
`verify_pdf_visual_review_bundle(..., require_full_document=True)` and must
also match an independently supplied expected hash. The API derives the
document/pipeline binding and every page artifact/render/annotation hash from
that verified directory; callers cannot supply or shrink those fields. The
root `artifact_sha256` is separate and must also be pinned externally.

`reviewer` is fixed to `actor_type: AI` and records model ID/version, tool
ID/version, and an RFC 3339 UTC review time.

`pages` is an exact, complete, ascending ledger for every page from
`page_start` through `page_end`. Each page must:

- carry the bundle's `artifact_sha256`, `render_sha256`, and
  `annotated_sha256`;
- set `visited` to `true`;
- use `FULL_PAGE` or `HIGH_RISK_DETAIL`;
- decide `text`, `image`, `table`, and `layout` with `PASS`, `ISSUE`, or
  `NOT_EVALUATED`;
- use only safe, text-free identifiers in `finding_codes`.

An all-`PASS` page must have no finding codes. Any `ISSUE` or
`NOT_EVALUATED` decision must have at least one safe finding code, so the
ledger cannot silently report an unreviewed dimension or attach a finding to
an otherwise all-pass page.

The validator rejects missing, duplicate, out-of-order, and out-of-range
pages. It also verifies the actual bundle files, requires an exact
full-document range, cross-checks the bundle/document/pipeline/page bindings,
deterministically recomputes `summary`, and rejects summary drift. The root
hash is computed over canonical JSON with only that self-referential field
omitted.

For replacement detection, pin the exported bundle manifest hash and returned
pre-review artifact hash in a separate controlled record. Both pins and the
bundle directory are mandatory during validation. Self-hashes alone are
integrity evidence, not signatures or trust decisions.

## Customer-text boundary

The artifact is metadata only. Every object has an exact field set; raw PDF
text, OCR text, excerpts, notes, filenames, paths, prompts, and free-form
finding descriptions are not valid fields. Opaque identifiers and finding
codes must match restricted safe-identifier patterns.

Keep any sensitive visual material and detailed findings in the separately
controlled review system. Refer to them only with the hashes and safe codes
allowed here.

## Python API

From the `prototype` directory:

```python
from scripts.pdf_visual_ai_pre_review import (
    canonical_artifact_sha256,
    prepare_pdf_visual_ai_pre_review,
    validate_pdf_visual_ai_pre_review,
)

artifact = prepare_pdf_visual_ai_pre_review(
    bundle_dir=review_bundle_dir,
    expected_review_bundle_sha256=review_bundle_manifest["bundle_sha256"],
    reviewer=reviewer,
    page_reviews=page_reviews,
)

validated_copy = validate_pdf_visual_ai_pre_review(
    artifact,
    bundle_dir=review_bundle_dir,
    expected_review_bundle_sha256=review_bundle_manifest["bundle_sha256"],
    expected_artifact_sha256=artifact["artifact_sha256"],
)
assert artifact["artifact_sha256"] == canonical_artifact_sha256(
    artifact
)
```

Short aliases `prepare_ai_pre_review`, `validate_ai_pre_review`, and
`canonical_hash` are also exported. The Python validator is authoritative for
cross-field invariants that JSON Schema cannot express, including contiguous
page ordering, summary equality, and canonical artifact hashing.
