# Source-first completeness and order check

Use this after compiling a meaningful Map scope and before claiming it complete. `compile-map` saves `map-audit.json` and a separate `source-findings.json` and returns a compact `source_audit` page inventory. Account for the original range and inspect the expanded tree in source order; this does not require one parser node per business item or a perfect whole book before saving.

```text
python <plugin>/scripts/reqpilot.py audit-map --map-dir <compiled-map>
python <plugin>/scripts/reqpilot.py audit-map --map-dir <compiled-map> --page 4 --page 5
```

Use actual pages from your investigation. The first call gives page inventory and expanded-tree order; selected-page calls give exact unassigned text ranges, table rows, asset refs and explicit dispositions. Native hosts without this operation can read `source_audit.audit_read` through their reader; do not repeatedly call an unavailable operation.

For still-open ranges, decide from the original whether they need Map content/ownership, are already represented through a parallel capture, are outside the authorized scope, or remain unresolved. The helper provides facts and possible counterparts; it does not decide ownership or certify a disposition. Compare the start, internal numbered peers and trailing text with the saved Map, and establish governing ancestors from the original.

| Evidence | Correct action | Counterexample |
|---|---|---|
| One block has numbered 1, 2, 3, but compiled ranges omit 2 or the trailing note | Inspect the returned substring and restore its true owner/range | A complete numbered BOM/test may stay grouped; no SRC per number is required |
| All numbered words survive, but `numbered_body_groups` reports several original numbers inside one owner | Preserve their independently addressable original boundaries in Map now, using `numbered_body` or exact ranges | “Map only” postpones SRC/REQ and engineering decomposition, not original numbered structure. A complete-unit exception needs source support; neither regex markers nor differing numbers alone justify splitting a BOM/test |
| A page frame is unassigned but its words already exist as mapped clauses | Check `possible_parallel_map_nodes` and original cells; retain needed carrier evidence and explain parallel capture | Never add another `logical_tables` scope just to make the count zero |
| Image has no content owner | Inspect and bind its genuine clause/table/whole front matter, or report uncertainty | A page screenshot/evidence ref does not prove business images survive items and Excel |
| Expanded tree goes from a later page back to an earlier sibling | Check parentage and scope extent; repair supported errors locally | Columns or repeated context need source review; do not sort chapter strings or automatically reparent by coordinates |
| Consecutive numbered body leaves cross a page but their parent changes | Inspect `numbered_parent_change_leads`, both endpoints and the governing prefix; a page break alone does not reset a list or its conditions | A genuine new heading, explicit scope change or independently restarted unit may justify different parents; source evidence decides |
| Physical page has no capture | Inspect original pixels and account for the page | Empty extraction is not proof of a blank page |

Capture counts are not missing-requirement counts. Parallel table/text channels, dispositions and positional order remain review leads. This check cannot detect content absent from capture; original page checks remain necessary. Resolve the authorized scope, not unrelated pages. Save factual findings and local revisions/remaining gaps, not hidden reasoning.

## Original numbering leads

`source_findings` currently scans numeric body peers in the saved Map. For `2.6 → 2.10`, it reports the observed endpoints, source references and expected number range, with `PENDING_SOURCE_REVIEW` and `blocks_progress=false`. Inspect the original pages before calling it an original-document gap or extraction omission. Covers/TOC, chapter-prefix resets, figure labels and quantities are excluded from this scan; letter/table numbering and uncaptured content still need your source review. `scan_state=COMPLETED` only describes this bounded scan; `original_document_complete=null` does not claim whole-document quality.

If the original also jumps, preserve it and report the source fact. If intermediate source clauses exist but are absent from the Map/items, repair the earliest affected capture/boundary/ownership layer. Do not insert missing-number placeholder requirements. Record the factual conclusion and evidence in the current review feedback, then revise the actual affected scope; no generic finding-resolution API is advertised yet. `deliver` also writes `source-findings.json` alongside the same-version Excel for handoff, and `review-verify` reports the same scoped leads.
