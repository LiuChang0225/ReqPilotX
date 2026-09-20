# PDF body review artifacts

> 2026-09-03 定位：保留的离线/兼容工具参考，不定义当前开发顺序或产品完成状态。
> 其中运行计数、版本与阶段结论按其历史语境阅读；仅在明确使用该工具时采用对应合同。
> 当前工作按 [目标态优先开发计划](../../../docs/需求提取/开发计划.md) 推进，客户数据和真实模型调用需单独授权。

This directory defines the controlled, text-free review contract used before a
`source_ir.pdf_reviewed_body_sequence/v1` profile may become `READY`.

It does **not** contain a real review, reviewer identity, customer text, page
image, SourceIR snapshot, OCR output, or approval credential. Real drafts and
approvals stay in controlled external storage, following the same boundary as
`eval/ai_gold/`.

## Why there are two artifacts

`prepare_pdf_body_review_draft(...)` may consume a candidate produced from the
current SourceIR or remote capture, but it always emits:

```text
status = DRAFT
is_human_gold = false
```

The repository intentionally provides no function that manufactures an
approval. A human-review system with a permission boundary separate from the
draft-producing agent creates an `APPROVED` artifact after reviewing every page
against the original PDF.

Finalization consumes both artifacts and an authoritative scope supplied by the
caller. It produces an existing-manifest-compatible `READY` profile only when:

- assertion, evaluator, document, source hash, page range, review mode and
  visual-provider provenance match the authoritative scope;
- the draft's canonical profile hash and the approval's canonical draft/profile
  hashes match;
- every page is present exactly once and in order;
- every observed image/figure occurrence is present exactly once and in source
  order, with its page, native locator, bounding box, final media hash and any
  composite derivation bound into the occurrence identity;
- every image occurrence is resolved by the reviewed draft as `CONTENT` or
  `NON_CONTENT` (none remains `UNRESOLVED`);
- every observed table occurrence is present exactly once and in provider/source
  order, with its page, bounding box, stable source-object hash, topology hash
  and structural counts bound into the occurrence identity;
- every table occurrence is resolved as `CONTENT` or `NON_CONTENT`;
- every human review explicitly approves the text, image and table channels on
  every page;
- reviewer IDs are distinct HMAC-SHA256 pseudonyms in an externally governed
  trusted-reviewer set;
- reviewer role, human actor type, independence attestation, UTC timestamp,
  review tool/version and review-policy version are complete.

The default minimum is one independent human reviewer. A caller may require two
or more reviewers with `minimum_human_reviewers`. This mirrors the independent
review pattern in `eval/ai_gold/` without creating a second workflow system.

## Files

- `draft.schema.json` describes the untrusted candidate and the exact profile
  subject under review.
- `approval.schema.json` describes the external human approval and its per-page,
  three-channel decisions.
- `../../scripts/pdf_body_review_artifacts.py` contains pure prepare, validate
  and finalize functions. It performs no file, network, credential or registry
  I/O.
- `../../scripts/pdf_body_review_candidate.py` turns a native/visual capture and
  explicit occurrence decisions into a text-free `DRAFT`; omitted decisions
  stay `UNRESOLVED`, and stale decision IDs fail closed.
- `../../scripts/pdf_body_review_table_capture.py` converts and strictly
  revalidates stored Bailian `document_parsing` table evidence. Stored provider
  evidence always remains `UNRESOLVED`; review decisions live only in the
  separately hashed draft.

The candidate profile reuses the current A4 page fields and adds two kinds of
review-only evidence:

- `assertion_id` and exact evaluator ID;
- `document_id`, source SHA-256 and body page range;
- for `REMOTE_VISUAL`, the approved provider alias, provider ID, each page-image
  SHA-256, and two independent hashes: `visual_text_provider_version_sha256`
  for page OCR and `visual_table_provider_version_sha256` for the table parser.
- on every page, a complete `image_occurrences` disposition inventory plus
  `image_content_multiset_sha256` and `image_sequence_sha256`.
- on every page, a complete `table_occurrences` disposition inventory plus
  `table_content_multiset_sha256` and `table_sequence_sha256`.

Both remote version hashes are required. They bind the respective provider's
model, task, implementation and parsing/config identity; one vague aggregate
hash cannot substitute for either channel. `NATIVE` profiles forbid all
visual-provider and page-image fields.

## Image occurrence inventory

The inventory preserves source evidence instead of deleting headers, logos or
other likely furniture. Every observed `image` or `figure` gets one record with:

- `source_order`, `node_kind`, `asset_kind` and lower-case `media_type`;
- `content_sha256` of the final emitted asset bytes. For an alpha-mask
  composite, `derivation` also binds the base and mask hashes;
- `native_locator` containing PDF page number, native block index when present,
  and the exact `[x0, y0, x1, y1]` bounding box;
- `occurrence_sha256`, the canonical hash of all fields above; and
- one disposition: `CONTENT`, `NON_CONTENT` or `UNRESOLVED`.

Disposition is deliberately excluded from `occurrence_sha256`, so a reviewer
can classify a stable source occurrence without changing its identity. The
draft/profile hashes still bind the resulting disposition. The draft-preparation
API accepts `UNRESOLVED`; approval validation and finalization reject it. A
reviewer's per-page `image=APPROVED` decision therefore attests the complete,
resolved inventory in the exact draft referenced by the approval hash.

`expected_image_count`, `image_content_multiset_sha256` and
`image_sequence_sha256` are recomputed exclusively from occurrences classified
as `CONTENT`; they cannot count `NON_CONTENT` or `UNRESOLVED` entries. The
projection is the existing A4 image shape:

```text
{node_kind, asset_kind, media_type, content_sha256}
```

The inventory contains hashes and geometry only. It must not contain source
text, image bytes, page renders or OCR output. Finalization strips the inventory
and the two image-only digests from the returned `READY` manifest page, keeping
the existing manifest API unchanged. Raw SourceIR remains intact.

Completeness needs one external comparison: at evaluation time the runner must
rebuild the full ordered occurrence inventory from the current SourceIR and
exact-match it to the reviewed profile before applying dispositions. That
comparison detects an omitted occurrence, changed location, changed composite
or changed ordering. The pure artifact contract can detect deletion or mutation
after review through its hash chain, but cannot infer an occurrence that was
never supplied to it. This division keeps the contract general and avoids
document-specific size, margin or logo rules.

## Table occurrence inventory

The table inventory applies to both review modes. Each observed table has one
ordered record containing:

- `provider_object_id_sha256`, a stable hash of the source table object. For a
  native profile this is the stable native source-table object hash; for a
  remote profile it is the stable object hash produced from the approved
  provider result. It is an integrity identifier, not customer text;
- zero-based `provider_index`, `page_number` and exact `[x0, y0, x1, y1]`
  `bbox`, which bind the table to its page and canonical reviewed source order.
  Native coordinates use the native PDF space; remote coordinates are mapped
  from the approved page image back into the same PDF page space. The remote
  provider's raw layout index remains bound inside `provider_object_id_sha256`
  and need not itself be spatially monotonic;
- `row_count`, `column_count`, `cell_count`, `merged_region_count` and
  `topology_sha256`; and
- `occurrence_sha256`, the canonical hash of every field above, plus one
  disposition: `CONTENT`, `NON_CONTENT` or `UNRESOLVED`.

Disposition is excluded from `occurrence_sha256`, while the enclosing profile
hash still binds the reviewer's classification. `table_occurrences` must be in
strictly increasing `provider_index` order. Duplicate occurrence identities,
source-object identifiers or page/bounding-box locators are rejected.

`topology_sha256` is the canonical hash of the normalized, text-free cell
topology (cell anchors plus row/column spans). Cell strings are not part of this
review artifact. The source/provider object and any raw table payload remain in
controlled evidence storage; only their stable hashes enter the draft.

The four existing expected table values are not independent inputs. The
contract recomputes `expected_table_count`, `expected_table_row_count`,
`expected_table_cell_count` and `expected_merged_region_count` exclusively from
occurrences classified as `CONTENT` and rejects any mismatch. The two table
digests use this text-free projection, in multiset and source order respectively:

```text
{row_count, column_count, cell_count, merged_region_count, topology_sha256}
```

Preparation permits `UNRESOLVED` so incomplete review work can be saved as a
DRAFT. Approval and finalization reject any unresolved table. Finalization
strips the inventory and both table-only digests, so the `READY` manifest page
retains exactly the existing four table counts and no new review-only fields.

For a `NATIVE` profile, evaluation rebuilds the full ordered native table
inventory, exact-matches its text-free identities to the reviewed profile, and
then measures only occurrences approved as `CONTENT`. For a `REMOTE_VISUAL`
profile, the remote table inventory is immutable review-input provenance bound
to the source and page-image hashes; A4 does not replay the table provider at
runtime. Instead it compares the approved text-bearing `CONTENT` topologies
with the current native parser result. This is intentional: using the same
remote table provider for baseline and runtime would hide native parser
omissions. Page OCR is still replayed with the approved text-provider identity.
The contract does not use document-specific table-shape or page-location rules
to infer completeness.

## Hash chain

All hashes use UTF-8 JSON with sorted object keys, compact separators and
`allow_nan=false`, as implemented by `canonical_json_bytes(...)`:

```text
reviewed profile
  <- ordered image/table occurrence identities and human dispositions
  -> profile_sha256
  -> DRAFT artifact
  -> draft_sha256
  -> APPROVED artifact
  -> review_artifact_sha256 in the READY profile
```

There is no self-hash and therefore no hash cycle. A content-addressed external
store should persist the exact canonical approval bytes under
`<review_artifact_sha256>`. The approval points to the draft hash, and the draft
contains the reviewed profile.

## Trust boundary

The schemas and hashes prove integrity and binding; they do not prove that a
person really reviewed the PDF. That trust comes from external access control or
an authenticated review service. `trusted_reviewer_ids` must therefore come
from a source the draft-producing agent cannot modify. Passing a self-authored
allowlist does not establish independent review.

The same separation applies to the content-addressed draft/approval store. A
caller that can author both the artifact bytes and the trust policy can claim a
trusted reviewer identifier without possessing a reviewer signature; that is
not an independent approval. The controlled store and policy must be governed
outside the draft-producing runtime (or replaced by an authenticated signed
review service).

Likewise, `independence_attested=true` records a governed review decision; it is
not a substitute for that governance. If the external reviewer or controlled
artifact is unavailable, the manifest profile must remain `PENDING`.

## Pure API

```python
from scripts.pdf_body_review_artifacts import (
    canonical_pdf_image_occurrence_sha256,
    canonical_pdf_table_occurrence_sha256,
    finalize_pdf_body_review_artifacts,
    pdf_content_image_projection,
    pdf_content_table_projection,
    prepare_pdf_body_review_draft,
    summarize_pdf_content_image_occurrences,
    summarize_pdf_content_table_occurrences,
)

draft = prepare_pdf_body_review_draft(candidate_profile)

# A separate controlled human-review workflow creates `approval`.
ready_profile = finalize_pdf_body_review_artifacts(
    draft=draft,
    approval=approval,
    expected_scope=scope_from_the_registered_case,
    trusted_reviewer_ids=reviewer_ids_from_external_authority,
    expected_review_policy_version="pdf-body-review-policy.v1",
)
```

The returned object contains only the current manifest fields: NATIVE returns
`status`, source/page scope, mode, `review_approved`, artifact hash and pages;
REMOTE_VISUAL adds `approved_visual_provider`. Reviewer IDs, occurrence
inventories, image/table-only review digests, provider-version hashes and
approval details remain outside the manifest.

The repository intentionally contains no real approved review artifact. A case
therefore remains `PENDING`/`GAP` until its controlled external draft and human
approval exist and the runtime resolves them successfully.
