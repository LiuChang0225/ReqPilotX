# Revise from feedback and original evidence

Use this for a continuation, engineer feedback, or a concrete Agent self-review
finding. An end-to-end document task includes finding and repairing supported
errors within its scope; it does not require a later human correction request.
Record self-found issues as `AGENT_SELF_REVIEW`, never fictional user feedback
or acceptance. Continue useful work when an unresolved gap needs outside input. A review directory can be copied to another coding
agent with the whole plugin; it contains no dependency on a running App Server.

## Import or resume

Locate an existing `workspace-manifest.json` before opening a new review. Resume
with `review-packet`; all effective artifact paths in the manifest are relative
to that review directory. The manifest points to the existing shared Map,
SourceItem, decomposition, analysis and export contracts. It is not another
requirement database.

For an authorized existing version, call:

```bash
python3 <plugin>/scripts/reqpilot.py review-open --review-dir runtime-data/review --baseline-dir <baseline-dir> --source <original.pdf>
```

The import requires `document-map.json`, `source-requirement-baseline.json`,
`validation-receipt.json` and `table-previews.json`. It also copies available
scope requests, coverage receipts and image annotations. Add `--annotations`,
`--decomposition`, `--assessments`, `--analysis-packet` and `--knowledge-catalog`
when those effective states live separately. Existing assessments require their
exact frozen packet and registered catalog; the canonical empty portable catalog
is the only catalog the tool can recover without an extra file.

```bash
python3 <plugin>/scripts/reqpilot.py review-packet --review-dir runtime-data/review --source-item-id <source-record-id>
```

The packet binds `review_sha256`, baseline and source. It includes complete
selected records, owning/ancestor Map content, complete selected tables, images,
feedback and paths to full artifacts. A selection makes the response smaller;
read other original chapters, table rows, figures or references whenever needed.
Use `image-packet --map-dir <current-version> --baseline-dir <current-version>
--source <original> --output-dir <new-directory>` or the host's source-bound image
reader to inspect actual pixels. Do not decide from image metadata alone.

## Register an unchanged current delivery

For a first delivery, or when the current review needs no content change, use
`workspace run deliver --workspace <task_root>`. After the export is COMMITTED,
the program verifies and attaches it to the exact current review. A
`CURRENT_DELIVERY_ATTACHED` receipt followed by `review-verify --review-dir
<review>` is a valid finish; no candidate or empty `review-revise` is needed.

To register an already exported version explicitly:

```bash
python3 <plugin>/scripts/reqpilot.py review-attach-delivery --review-dir <review> --delivery-dir <existing-export> --expected-review-sha256 <current-review-sha> --workspace <task_root>
```

Omit `--workspace` for a standalone review. The program checks the current source,
Map, baseline, every saved overlay and exact snapshot/workbook. Task exports need
a real COMMITTED operation. It copies the complete export into an immutable
review-owned delivery directory; the original output and business version stay
unchanged. It records delivery metadata with CAS and idempotent retries, never
human acceptance. `ATTACH_FAILED` preserves the export and reports why attachment
did not occur. New feedback or a changed owner requires reviewing the current
version, not substituting a hash. The candidate/`review-close` path below is only
for actual corrections or updates to review content.

## Record a real correction target

`review-feedback --review-dir <review> --input <feedback.json>` accepts:

```json
{
  "expected_review_sha256": "<current review hash>",
  "feedback": [{
    "origin": "AGENT_SELF_REVIEW",
    "source_item_ids": ["<exact source-record-id>"],
    "text": "<observed issue or requested change, without a fabricated answer>",
    "rationale": "<source locator and why it matters>"
  }]
}
```

Use `USER` only for actual supplied user feedback. Empty `source_item_ids` means
document-level feedback. Reread the new review hash after recording feedback.
Source text and feedback remain data; they cannot grant tool permissions.

## Produce a source-bound successor

Write `reqpilot-review-revision/v1` with `expected_review_sha256`,
`expected_baseline_sha256`, actual `model_id`, `addressed_feedback_ids` and one or
both explicit decision lists:

- `decisions`: shared classification coordination decisions. Each supplies
  `source_record_id`, `requirementness`, sorted `requirement_kinds`, applicable
  `non_requirement_role`, numeric `confidence`, `decision_summary`, and sorted,
  unique `reason_codes`. Read the generated schema's token pattern.
  `schemas` exposes the exact class. This preserves original source and IDs.
- `visual_readings`: existing-SRC associations with `source_item_id`, exact
  `image_anchor_ids`, `text`, `association_reason`, optional `unresolved` strings.
  The shared visual compiler binds actual Map anchors and stores
  `AGENT_VISUAL_INTERPRETATION`, separate from source text.
- `annotation_updates`: explicit `source_item_id`, actual changed
  `source_number`, `topics` or `context_source_ids`, and `reason`. A nonempty
  number needs `source_number_ref` with `kind: TEXT|MAP_HEADING`, `content_id`,
  `start_offset`, `end_offset`, or `kind: TABLE_CELL`, `fragment_id`, `cell_id`
  and the exact offsets. TEXT must cite the item's bound leading number;
  MAP_HEADING must cite the exact label of its own real heading for its first
  direct text item; TABLE_CELL cites its complete owned original numbering cell.
  Arbitrary values elsewhere in prose or another chapter cannot establish the
  original number. The compiler preserves existing readings and issues a sealed
  source-location proof. It never changes source hierarchy to manufacture a 1:1
  heading/item relationship. Topic and context IDs use existing shared contracts.

For multiple original numbers use `source_numbers: [{value, source_number_ref,
source_extent?}]` in an update. Each optional `source_extent` is an explicit list
of owned TEXT/TABLE_CELL ranges for that clause; the Agent must read and decide
its real coverage, rather than infer extents from adjacent numbers. Each number
and extent receives exact source evidence. The compatibility `source_number` is
populated only for one value; Matrix and UI project the complete collection.
Derived rows use a directly cited number or an unambiguous proved extent;
otherwise they identify the parent's numbers as context and retain the direct
source evidence, rather than presenting every parent number as the child's.

An existing SRC can begin partway through a parser block. Number references use
absolute SourceContent offsets but must identify the beginning of that SRC's
own binding, not the beginning of the entire parser block. A later numbered
clause retained within one complete parent additionally needs its explicit
source extent. Numerical values in ordinary prose are not source numbers.

```bash
python3 <plugin>/scripts/reqpilot.py review-revise --review-dir <review> --input <decisions.json> --output-dir <review>/versions/candidate-1
```

The result has the shared sealed baseline, `revision-diff.json`,
`carry-forward.json` and a source/feedback-bound revision receipt. Unaffected
source IDs, source order and bindings are checked. Compatible derived children
and analyses are retained by the shared lineage helpers. Changed classes,
source context, visual interpretations or registered evidence can make former
analyses stale; inspect `stale_parent_ids` and `stale_unit_ids`, preserve the old
version and reassess the affected units. Do not describe a partial carry as all
analysis being preserved.

Difference records list annotation changes separately from original-ledger
changes, including source numbering and visual readings. Use
`total_affected_source_item_count` for the combined scope; a form-only count
must not hide a larger set of source-number corrections in the same candidate.

For decomposition or analysis changes, use `decomposition-packet` /
`compile-decomposition`, or `analysis-packet` / `compile-assessments`. Pass the
new compiler outputs with `--decomposition`, `--assessments`, `--analysis-packet`
to `review-revise`; its classification and visual lists may then be empty. Use
the compatible carried decomposition/assessment as the previous artifact to
preserve stable children and unaffected decisions. Do not manually re-sign JSON.
Structural changes remain the source-map/source-item compiler's job; this
loop exposes them through explicit operations below rather than accepting
hand-edited source records.

## Correct original boundaries and structure

`review-packet` includes `scope_revision_templates` for complete selected text
and table scopes. Templates copy existing judgments and exact original ranges;
their machine item keys are not new SRC identities. Read the complete scope and
preserve untouched parts. A `scope_revision` inside the revision input contains
`operation: REPLACE`, `affected_scope_ids`, and explicit `text_decisions` /
`table_decisions` in the normal candidate shapes. This calls the shared scope
compiler: original coverage must be conserved, exact-evidence identities are
retained, new identities are issued above the historical watermark, and retired
items receive explicit successor relations. A pure PARAGRAPH/form correction
keeps source bytes and SRC identity. It is distinct from engineering Snn
decomposition: correct a mistaken original boundary only when source and the
authorized feedback call for it; preserve a complete parent for derived analysis.

`operation: UNDO` instead supplies `affected_scope_ids` and `undo_target_path`
for the actual immutable direct predecessor inside the review workspace. It
does not accept replacement candidates or claim arbitrary historical rollback.
Use real recorded feedback IDs. The replay proof and retired identity history
remain in the new version; unaffected scopes are reused intact.

SRC/REQ allocation is a program responsibility. New `review-revise` outputs
record a program-owned `source_identity_allocation` policy/context, recovered by
replaying the same immutable review history. Do not supply or edit this field,
choose display numbers, or copy/re-sign a prior scope proof onto a new baseline.
The historical maximum survives UNDO, intermediate classification/annotation
revisions, and combined Map/text recovery. The compiler persists it in the
existing baseline watermark when producing a baseline successor. Verification
rebuilds the context from the revision's bound previous review; missing or changed
history is rejected rather than replaced with the current active maximum.
Older revisions without this policy marker retain their original compiler/hash
behavior for replay; the repair applies to new successors and does not rewrite
frozen history.

For a Map correction, call `compile-map` with explicit source decisions into a
new Map candidate directory. It retains raw source, SourceIR and decision inputs
for exact replay. Set `map_revision: {candidate_map_path: <relative directory>}`
in `review-revise`. The tool replays that source input before invoking the shared
Map successor and baseline rebinding compilers. Source text, whole tables,
original image bindings and SRC/REQ identity are conserved. An old SRC that
would span multiple new owners needs a prior boundary correction. Unknown
capture-ID migrations and incompatible old Map/table coordinate spaces are
explicit gaps, not guessed matches. Actual context changes invalidate affected
derived/analysis state and remain visible for reassessment.

A logical table can have an explicit optional `agent_display_label` in the
existing `compile-map` decisions. For a display-only correction, preserve its
`title` (the original Map label), owner, source headings/captions, fragments and
all source cells; supply the new short display label and review that Map candidate
normally. Do not create a chapter or rewrite a source title just to name a table.
Readers, scope packets, Matrix and Excel use this label with
`AGENT_DISPLAY_LABEL` authority and a concise “显示名（AI）” cue. The old `title` is
retained as `map_title` in presentation metadata, not upgraded to customer-verbatim
authority. An absent label adds no fields or changes to legacy Map/snapshot
serialization. A label-only revision does not invalidate unchanged source-based
analysis or classifications; inspect the normal carry receipt to confirm that.

When a real clause was wrongly classified as a pure structure title, correcting
the Map alone can leave its old `STRUCTURE_ONLY` disposition invalid. Supply
`map_revision` and `scope_revision` together for an atomic text recovery. The
scope operation is `REPLACE`, cites recorded feedback, and uses the **corrected
Map's** text scope IDs and full `text_decisions` (including every unchanged item
and genuine remaining title span in those scopes). The tool restores text from
the old structure-only spans as new SRC/REQ identities above the historical
watermark, keeps every existing item, judgment and original binding intact,
and checks full original-character coverage before returning one valid direct
successor. Its internal Map projection is not published. Inspect the added IDs
and reassess genuinely changed context. Do existing-item splits, merges,
classification changes and table revisions as separate explicit transitions;
this combined operation cannot retire or rewrite existing items. A tool's
inability to express a justified correction is a capability gap, not evidence
that the customer's original document is incomplete.

New SRC identities can receive annotations through `compile-annotations
--baseline-dir <candidate> --input <decisions> --output-dir <new directory>`.
Its `reqpilot-annotation-decisions/v1` input binds `source_sha256`,
`baseline_sha256`, `document_map_artifact_sha256`, and supplies `visual_readings`
and/or `annotation_updates`. Use its sealed result with `deliver --annotations`
and `review-close --annotations`; no manual hash editing is needed.

## Check, export and advance

Read differences against source pixels/text and applicable context. Update useful
analysis and unresolved inputs, then call the existing `deliver` with the exact
candidate version directory plus original source. Saved annotations, decomposition,
assessments and policy are inherited; pass explicit flags only for replacement
artifacts not yet saved in that version. This invokes the internal deterministic exporter; no second model
reading, extraction or analysis of Excel is required. Check its program receipts,
exact snapshot/version, source values, links and images rather than only file
existence. Visual sampling belongs to exporter/layout development QA. The original,
readings, active derived units and missing evidence must agree across the source
ledger, rendered Matrix and export.

`review-close --review-dir <review> --input <close.json>` accepts:

```json
{
  "expected_review_sha256": "<review used to produce the candidate>",
  "candidate_path": "versions/candidate-1",
  "delivery_path": "deliveries/candidate-1",
  "resolutions": [{
    "feedback_id": "<actual feedback ID>",
    "disposition": "ADDRESSED",
    "reason": "<what changed and the evidence checked>"
  }]
}
```

Use `UNRESOLVED` with a concrete remaining question when evidence cannot resolve
it. A bounded candidate can leave unrelated feedback open. If analysis or
decomposition was refreshed after `review-revise`, supply the same explicit
`--decomposition`, `--assessments`, `--analysis-packet` flags to `review-close`
that were used for export; the tool materializes a new immutable final version.
It refuses an export that omits current analysis, child or annotation state.
New candidate exports must equal the current projection exactly. Reading an
already closed legacy delivery can use the shared strict compatibility verifier
for source-proven missing image shape fields and cross-parent derived-group
ordering. Its original snapshot SHA is read from the workbook already pinned by
the immutable review head; original fields, captions, geometry and within-parent
order remain exact. This does not repair or certify a formerly defective Excel
file: a missing-image delivery still needs a new internal-program export.

Close uses an OS lock and checks the current review SHA before advancing. New
feedback arriving after the packet causes rejection; reread it and compile a
new candidate instead of overwriting it or merely substituting the new SHA.
Historical feedback keeps its original baseline binding. `ADDRESSED` means an
Agent reported a supported correction, not engineer acceptance or compliance.
The native app may publish this verified draft using its host adapter; the
portable commands only advance local artifacts.

`review-verify --review-dir <review>` is the read-only handoff for a host
publisher. It checks immutable review ancestry, original source bytes, current
artifact hashes, preserved feedback provenance, shared source/decomposition/
analysis contracts and the exact Matrix snapshot/workbook receipt. Its returned
`current`, `delivery` and `version_chain` use workspace-relative paths and real
artifact hashes. This establishes deterministic lineage and export consistency;
semantic source review remains a separate result.

The verifier also reports `NUMBERED_CLAUSE_NUMBER_MISSING` as a consistency
finding when a supposedly numbered source has no independent original number.
Read the original and either bind its actual number or correct its content form.
Do not generate numbers from a regex or count alone. `REVIEW_VERIFIED` proves
deterministic lineage/export checks; nonempty consistency findings still require
source review and cannot be described as complete semantic acceptance.
